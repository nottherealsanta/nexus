"""Code mode kernel end to end: real Runtime, scripted provider, real Monty (plan 03 section 7, kernel plan §6)."""
from __future__ import annotations


import pytest

pytest.importorskip("pydantic_monty")

from nexus.config import Config  # noqa: E402
from nexus.config.schema import (  # noqa: E402
    AgentSection, CodeModeSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection)
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response  # noqa: E402
from nexus.runtime import Runtime  # noqa: E402
from nexus.view import fold  # noqa: E402


def make_config(*, context=False, enabled=True, mode="allow", unattended="deny", max_nested=64, deny=(), read_deny=()):
    return Config(model="scripted/m", version=2, v2=ConfigV2(
        model=ModelSection(default="scripted/m"),
        agent=AgentSection(profile="coding"),
        permissions=PermissionsSection(mode=mode, on_unattended=unattended, deny=list(deny),
                                      read_denyroots=list(read_deny)),
        tools=ToolsSection(),
        code_mode=CodeModeSection(enabled=enabled, context=context, max_nested_calls=max_nested)))


async def drain(session, text, decide=None):
    events = []
    async for event in session.send(text):
        events.append(event)
        if event.type == "permission.requested" and decide is not None:
            assert session.resolve_permission(event.data["id"], decide)
    return events


def of(events, kind):
    return [e for e in events if e.type == kind]


async def run_code(tmp_path, cell, *, config=None, extra=(), start=None, **kw):
    """``code_start`` then one ``code_run`` cell, as a scripted model would."""
    provider = ScriptedProvider(
        tool_response(("s1", "code_start", {"purpose": "test", **(start or {})})),
        tool_response(("k1", "code_run", {"cell": cell, "purpose": "test"})),
        *extra, text_response("done"))
    runtime = Runtime(tmp_path, config=config or make_config(), providers={"scripted": provider})
    session = runtime.session("e2e")
    events = await drain(session, "go", **kw)
    return runtime, session, provider, events


def result_text(session):
    """The last tool result (the cell's, after ``code_start``'s manual)."""
    texts = []
    for message in session.messages:
        for block in message.content:
            if type(block).__name__ == "ToolResult":
                texts.append("".join(getattr(p, "text", "") for p in block.content))
    return texts[-1] if texts else ""


async def test_grep_and_parallel_reads(tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text(f"needle {i}\nother\n", encoding="utf-8")
    program = (
        "import asyncio\n"
        "hits = await grep(pattern='needle', path='.')\n"
        "paths = [f'f{i}.txt' for i in range(10)]\n"
        "texts = await asyncio.gather(*[read(path=p) for p in paths])\n"
        "len([t for t in texts if 'needle' in t])\n")
    runtime, session, _, events = await run_code(tmp_path, program)
    try:
        assert events[-1].type == "turn.completed"
        assert of(events, "code.kernel_started") and of(events, "code.cell_started")
        calls = of(events, "code.call")
        assert len(calls) >= 22  # 11 calls, a running and a final event each
        done = [c for c in calls if c.data["status"] == "ok"]
        assert len(done) == 11
        assert all(c.data["args"] and c.data["result"] for c in done)
        assert all(c.data["parent_call_id"] == "k1" for c in calls)
        finished = of(events, "code.cell_finished")[0].data
        assert finished["status"] == "ok" and finished["n"] == 1 and finished["calls"] == 11
        assert "Out [1]: 10" in result_text(session)
        view = fold(events)
        row = next(t for t in view.tools if t.name == "code_run")
        assert len(row.nested_calls) == 11
        assert row.code_mode["n"] == 1 and row.status == "completed"
    finally:
        await runtime.aclose()


async def test_nested_denied_read_matches_direct_and_is_catchable(tmp_path):
    (tmp_path / "vault").mkdir()
    outside = tmp_path / "vault" / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    config = make_config(read_deny=["./vault"])
    program = (
        "try:\n"
        f"    await read(path={str(outside)!r})\n"
        "    r = 'no error'\n"
        "except ToolError as e:\n"
        "    r = str(e)\n"
        "r\n")
    provider_direct = ScriptedProvider(
        tool_response(("d1", "read", {"path": str(outside)})), text_response("x"))
    direct_rt = Runtime(tmp_path, config=config, providers={"scripted": provider_direct})
    direct = direct_rt.session("direct")
    await drain(direct, "go")
    direct_text = result_text(direct)
    await direct_rt.aclose()

    runtime, session, _, events = await run_code(tmp_path, program, config=config)
    try:
        assert of(events, "code.cell_finished")[0].data["status"] == "ok"
        call = [c for c in of(events, "code.call") if c.data["status"] != "running"][0]
        assert call.data["status"] in ("denied", "error")
        assert direct_text.strip()
        assert direct_text.strip()[:60] in result_text(session)
    finally:
        await runtime.aclose()


async def test_gather_of_max_nested_calls_succeeds_and_one_more_hits_limit(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    ok = "import asyncio\nawait asyncio.gather(*[read(path='a.txt') for _ in range(8)])\n'fine'\n"
    runtime, session, _, events = await run_code(tmp_path, ok, config=make_config(max_nested=8))
    try:
        assert of(events, "code.cell_finished")[0].data["status"] == "ok"
    finally:
        await runtime.aclose()
    over = ok.replace("range(8)", "range(9)")
    runtime, session, _, events = await run_code(tmp_path, over, config=make_config(max_nested=8))
    try:
        finished = of(events, "code.cell_finished")[0].data
        assert finished["status"] == "limit" and finished["limit"] == "calls"
    finally:
        await runtime.aclose()


async def test_unavailable_monty_runs_without_code_tool(tmp_path, monkeypatch):
    import nexus.tools.codemode.tool as code_tool
    unavailable = {"available": False, "reason": "reinstall nexus-harness: pydantic-monty==1.1.0 is missing", "version": None}
    monkeypatch.setattr(code_tool, "availability", lambda: unavailable)
    provider = ScriptedProvider(text_response("hello"))
    runtime = Runtime(tmp_path, config=make_config(), providers={"scripted": provider})
    try:
        session = runtime.session("nomonty")
        events = await drain(session, "hi")
        assert events[-1].type == "turn.completed"
        assert not {"code_start", "code_attach", "code_run", "code_reset"} & {t.name for t in provider.requests[0].tools}
        from nexus.host_support.doctor import doctor_report
        report = doctor_report(runtime, list_sessions=lambda: [], list_extensions=lambda: [])
        assert report["code_mode"]["enabled"] and not report["code_mode"]["available"]
        assert "unavailable" in report["code_mode"]["note"]
        assert report["code_mode"]["reason"]
        assert "replaced by code_start" in report["code_mode"]["replaces"]
        assert "grants code_run" in report["code_mode"]["replaces"]
    finally:
        await runtime.aclose()


async def test_agent_definition_turn_keeps_the_kernel_tools(tmp_path):
    """An inheriting root agent (``build``) must not lose the kernel tools at turn start."""
    runtime = Runtime(tmp_path, config=make_config(),
                      providers={"scripted": ScriptedProvider(text_response("x"))})
    try:
        session = runtime.session("agent-code")
        session._turn_agent_definition = runtime.agents.resolve("build", context="root")
        turn = runtime._make_tool_turn(
            config=make_config(), session=session, turn_id="t1", attended=False)
        kernel = {"code_start", "code_attach", "code_run", "code_reset"}
        assert kernel <= set(turn.manager.names)
        assert kernel <= {schema["name"] if isinstance(schema, dict) else schema.name
                          for schema in turn.schemas}
    finally:
        await runtime.aclose()


def child_name(runtime):
    rows = runtime.sessions.store.db._connection().execute(
        "SELECT name FROM sessions WHERE namespace='agents' AND kind='child'").fetchall()
    assert len(rows) == 1
    return rows[0][0]


def lazy_start(targets, call_id="s1"):
    """A scripted ``code_start`` whose targets are known only when the request arrives."""
    def step(request):
        return list(tool_response((call_id, "code_start", {"purpose": "read context", "targets": targets()})))
    return [step]


def cell(program, call_id="k1"):
    return tool_response((call_id, "code_run", {"cell": program, "purpose": "read context"}))


def last_result(session):
    return result_text(session)


def start_result(session):
    for message in session.messages:
        for block in message.content:
            if type(block).__name__ == "ToolResult" and getattr(block, "tool_use_id", "") == "s1":
                return "".join(getattr(p, "text", "") for p in block.content)
    return ""


async def test_root_reads_its_child_context_after_approval(tmp_path):
    names: list[str] = []
    program = (
        "meta = await context_meta(target=targets[1])\n"
        "msgs = await context_messages(target=targets[1])\n"
        "report = await context_report(target=targets[1])\n"
        "print(str(msgs))\n"
        "print(str(report))\n"
        "meta\n")
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "investigate flaky test", "subagent_type": "task"})),
        text_response("CHILD-FINAL-REPORT the flake is a race"),
        text_response("delegated"),
        lazy_start(lambda: names),
        cell(program),
        text_response("read it"))
    runtime = Runtime(tmp_path, config=make_config(context=True, mode="ask"),
                      providers={"scripted": provider})
    try:
        session = runtime.session("root1").mark_attended()
        await drain(session, "delegate", decide="allow_once")
        names.append(child_name(runtime))
        events = await drain(session, "now read the child", decide="allow_once")
        asks = [e for e in of(events, "permission.requested") if e.data["tool"] == "ContextQuery"]
        assert asks and asks[0].data["key"] == names[0]
        assert asks[0].data.get("parent_call_id") == "s1"  # asked while starting the kernel
        assert of(events, "permission.resolved")
        started = of(events, "code.kernel_started")[0].data
        assert [t["name"] for t in started["attached"]["targets"]][-1] == names[0]
        finished = of(events, "code.cell_finished")[0].data
        assert finished["status"] == "ok", finished
        calls = [c.data for c in of(events, "code.call") if c.data["status"] == "ok"]
        assert {c["tool"] for c in calls} == {"context_meta", "context_messages", "context_report"}
        text = last_result(session)
        assert "CHILD-FINAL-REPORT" in text and "investigate flaky test" in text
    finally:
        await runtime.aclose()


async def test_root_cannot_read_sibling_root_or_manager(tmp_path):
    from nexus.model.message import Message, Text
    refs: list[str] = []
    program = (
        "try:\n"
        "    await context_messages(target=%r)\n"
        "    r = 'read'\n"
        "except ToolError as e:\n"
        "    r = str(e)\n"
        "r\n")

    def attach(request):
        return list(tool_response(("a1", "code_attach", {"purpose": "peek", "targets": ["nexus-manager"]})))

    def read(index):
        def step(request):
            return list(tool_response(("k%d" % index, "code_run", {
                "cell": program % refs[index], "purpose": "peek"})))
        return [step]

    provider = ScriptedProvider(
        lazy_start(lambda: [refs[0]]), read(0), text_response("ok1"),
        [attach], read(1), text_response("ok2"))
    runtime = Runtime(tmp_path, config=make_config(context=True, unattended="allow"),
                      providers={"scripted": provider})
    try:
        other = runtime.session("root2")
        other.append_message(Message(role="user", content=[Text("sibling private text")]))
        name = runtime.sessions.store.db._connection().execute(
            "SELECT name FROM sessions WHERE id='root2'").fetchone()[0]
        refs.extend([name, "nexus-manager"])
        manager = runtime.sessions.open("nexus-manager", recover=False, ownership={"kind": "manager"})
        manager.append_message(Message(role="user", content=[Text("manager private text")]))
        root = runtime.session("root1")
        first = await drain(root, "peek at a sibling")
        started = of(first, "code.kernel_started")[0].data
        assert [t["tool"] for t in started["skipped"] if t["tool"].startswith("target ")]
        assert [t["ref"] for t in started["attached"]["targets"]] == ["self"]
        second = await drain(root, "peek at the manager")
        attached = of(second, "code.kernel_attached")[0].data
        assert [t["tool"] for t in attached["skipped"]] == ["target nexus-manager"]
        for events in (first, second):
            assert of(events, "code.cell_finished")[0].data["status"] == "ok"
            assert not [c for c in of(events, "code.call") if c.data["status"] == "ok"]
        text = " ".join("".join(getattr(p, "text", "") for p in block.content)
                        for message in root.messages for block in message.content
                        if type(block).__name__ == "ToolResult")
        assert "sibling private text" not in text and "manager private text" not in text
        assert text.count("was not approved") >= 2
    finally:
        await runtime.aclose()
