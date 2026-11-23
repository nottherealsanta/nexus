"""ZMX child sessions through the real runtime/store with a fake ACP `agy`; no real CLI or model is used."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus.config import ConfigFile
from nexus.llmhub.hub import provider_for as real_provider_for
from nexus.llmhub.types import Done, Message, PartDone, Text, ToolCall
from nexus.runtime import Conflict, _worker_attention_why
from nexus.server import make_runtime
from nexus.store import Store
from nexus.workers import records

pytestmark = pytest.mark.skipif(os.name != "posix", reason="ZMX requires POSIX")
MODEL = "agy/gemini-3.8-flash-high"


class Normal:
    name = "normal"

    def body(self, req):
        return {"normal": True}

    async def stream(self, req):
        yield PartDone(Text("normal"))
        yield Done("end_turn")


FIXTURE = Path(__file__).parent / "fixtures" / "workers" / "fake_agy_acp.py"


def build_env(tmp_path, monkeypatch, scenario="tool"):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "acp-scenario.txt").write_text(scenario, encoding="utf-8")
    cli = tmp_path / "agy"
    cli.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FIXTURE)!r}, run_name='__main__')\n")
    cli.chmod(0o700)
    monkeypatch.setenv("NEXUS10_AGY_CLI", str(cli))
    monkeypatch.setattr(runtime_module, "provider_for",
                        lambda ref: (Normal(), "m") if ref.startswith("fake/") else real_provider_for(ref))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "fake/m"))
    return rt, workspace, cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    return build_env(tmp_path, monkeypatch)


async def until(check, timeout=5):
    async with asyncio.timeout(timeout):
        while not (value := check()):
            await asyncio.sleep(.01)
    return value


def acp_log(workspace):
    path = Path(workspace) / "acp-log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def enable():
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.antigravity": True}, [])


async def idle(rt):
    for _ in range(500):
        if not rt.turns:
            return
        await asyncio.sleep(.02)
    raise AssertionError("turn did not finish")


def test_off_by_default_and_roots_refused(env):
    rt, workspace, _ = env
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    with pytest.raises(ValueError, match="disabled"):
        rt.create_session(project, title=None, model=MODEL, permission=None, parent=root)
    enable()
    with pytest.raises(ValueError, match="subagent-only"):
        rt.create_session(project, title=None, model=MODEL, permission=None)
    with pytest.raises(Conflict, match="Cannot switch"):
        rt.update_session(root.id, model=MODEL)
    assert rt.session(root.id).model == "fake/m"
    assert not (workspace / "acp-log.jsonl").exists()


async def test_a_new_agy_child_stays_acp_when_its_first_turn_fails_before_the_marker(env, monkeypatch):
    """A new child is ACP from creation; a failure before the first ACP prompt must not relabel it as legacy CLI."""
    rt, workspace, _ = env
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    worker = rt.session_json(rt.session(child.id))["worker"]
    assert worker["label"] == "ACP · Antigravity" and worker["transport"] == "acp-v1"
    from nexus.workers import agent as worker_agent

    monkeypatch.setattr(worker_agent, "cli_path", lambda pid: None)  # fail before the first ACP marker write
    rt.prompt(child.id, "first")
    await idle(rt)
    worker = rt.session_json(rt.session(child.id))["worker"]
    assert worker["label"] == "ACP · Antigravity" and worker["transport"] == "acp-v1"


async def test_child_runs_stores_standard_records_and_resumes(env):
    rt, workspace, _ = env
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "first request")
    await idle(rt)
    messages = [m.message for m in rt.store.messages(child.id)]
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].parts[0].name == "read_file" and messages[2].parts[0].output == "contents"
    assert messages[3].parts[0].text == "done"
    data = rt.session_json(rt.session(child.id))
    assert data["worker"]["session_id"] == "acp-1" and data["worker"]["context_complete"] is False
    assert data["worker"]["transport"] == "acp-v1" and data["worker"]["permission_mode"] == "default"
    assert data["worker"]["policy_fingerprint"] and data["worker"]["label"] == "ACP · Antigravity"
    assert "Nexus permission rules" in data["worker"]["not_applied"]
    assert data["tools"] == [] and data["tokens"] is None
    # A client rebuilding from the state snapshot (start, reconnect) gets the same facts as `session.updated`.
    rows = {r["id"]: r for r in rt.store.state_snapshot()["sessions"]}
    assert rows[child.id]["worker"] == data["worker"] and rows[root.id]["worker"] is None
    instructions = rt.snapshot_for(rt.session(child.id))["agent"]["prompt"]
    log = [json.loads(line) for line in (workspace / "acp-log.jsonl").read_text().splitlines()]
    first_prompt = next(entry["params"]["prompt"][0]["text"] for entry in log if entry.get("method") == "session/prompt")
    assert first_prompt.endswith("first request")
    if instructions:
        assert first_prompt.startswith("[Nexus assignment instructions]\n")
    rt.prompt(child.id, "second request")
    await idle(rt)
    log = [json.loads(line) for line in (workspace / "acp-log.jsonl").read_text().splitlines()]
    methods = [entry["method"] for entry in log if "method" in entry]
    assert methods.count("session/new") == 1 and methods.count("session/resume") == 1
    resume = next(entry["params"] for entry in log if entry.get("method") == "session/resume")
    assert resume == {"sessionId": "acp-1", "cwd": str(workspace.resolve()), "mcpServers": []}
    second_prompt = [entry["params"]["prompt"][0]["text"] for entry in log if entry.get("method") == "session/prompt"][-1]
    assert second_prompt == "second request"


async def test_guards_change_nothing(env):
    rt, workspace, _ = env
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    before = rt.session(child.id)
    for call in (lambda: rt.continue_session(child.id), lambda: rt.update_session(child.id, model="fake/m"),
                 lambda: rt.update_session(child.id, permission="ask")):
        with pytest.raises((Conflict, ValueError)):
            call()
    with pytest.raises(Conflict):
        await rt.btw(child.id, "side")
    with pytest.raises(Conflict):
        await rt.handoff(child.id)
    with pytest.raises(ValueError, match="text-only"):
        rt.prompt(child.id, "x", images=[{"name": "a.png", "data_url": "data:image/png;base64,AA=="}])
    assert rt.session(child.id).revision == before.revision and not rt.store.messages(child.id)
    ConfigFile().patch(ConfigFile().read()["revision"], {"zmx.antigravity": False}, [])
    with pytest.raises(ValueError, match="disabled"):
        rt.prompt(child.id, "off")
    assert not (workspace / "acp-log.jsonl").exists()
    assert rt.session_json(rt.session(child.id))["worker"]["label"]  # still readable


async def test_normal_children_are_unchanged(env):
    rt, workspace, _ = env
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model="fake/m", permission=None, parent=root)
    assert rt.session_json(rt.session(child.id))["worker"] is None
    rt.prompt(child.id, "ordinary")
    await idle(rt)
    assert [p.text for m in rt.store.messages(child.id) for p in m.message.parts if isinstance(p, Text)][-1] == "normal"


@pytest.fixture
def offline_workers_app(tmp_path, monkeypatch):
    import httpx

    from nexus.llmhub.catalog import cache_path
    from nexus.server import build_app
    from nexus.workers import registry

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEXUS10_AGY_CLI", str(tmp_path / "must-not-run"))
    catalog = cache_path()
    catalog.parent.parent.mkdir(mode=0o700)
    catalog.parent.mkdir()
    catalog.write_text("{}")

    def forbidden(*_a, **_k):
        raise AssertionError("ZMX touched a CLI while switched off")

    monkeypatch.setattr(registry, "cli_path", forbidden)
    monkeypatch.setattr(registry, "probe", forbidden)
    real_exec = asyncio.create_subprocess_exec

    async def guarded_exec(program, *args, **kwargs):
        assert "must-not-run" not in str(program) and os.path.basename(str(program)) not in ("agy", "claude"), program
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "mock/m"))
    app = build_app(rt, token="t", url="http://test")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                             headers={"authorization": "Bearer t"})


async def test_daemon_with_zmx_off_probes_no_cli_and_registers_no_provider(offline_workers_app):
    """The provider, model and config routes with ZMX off never look for a CLI or list ZMX."""
    from nexus.llmhub.hub import connected
    from nexus.workers import registry

    async with offline_workers_app as client:
        providers = (await client.get("/api/provider")).json()
        response = await client.get("/api/model")
        assert response.status_code == 200, response.text
        models = response.json()["models"]
        config = (await client.get("/api/config")).json()
    assert not [p for p in providers if p.get("zmx") or p["id"] in registry.IDS]
    assert not [m for m in models if m.get("zmx") or m["ref"].partition("/")[0] in registry.IDS]
    assert config["values"]["workers"] == {"claude_backend": "sdk", "antigravity": False}
    assert config["values"]["zmx"] == {"claude_backend": "sdk", "antigravity": False}
    assert "workers_unavailable" in config and "zmx_unavailable" in config
    assert not set(connected()) & set(registry.IDS)
    assert registry.visible() == {}


def test_claude_pool_child_maps_to_claude_code_and_keeps_its_configured_effort(env, monkeypatch):
    """`[agents.models] build = ["claude/<id>"]` with `[agents.efforts.build]` on that ref: with the Claude backend set
    to ZMX the child runs `claude-code/<id>` at the configured effort, not the agent's or the provider's."""
    rt, workspace, _ = env
    monkeypatch.setattr(runtime_module, "connected", lambda: ["claude", "fake"])
    monkeypatch.setattr("nexus.workers.registry.cli_path", lambda pid: "/bin/true")
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.claude_backend": "zmx",
                                             "agents.models": {"build": ["claude/claude-sonnet-5-5"]},
                                             "agents.efforts": {"build": {"claude/claude-sonnet-5-5": "medium"}}}, [])
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=None, permission="allow", agent="build", parent=root)
    data = rt.session_json(rt.session(child.id))
    assert data["model"] == "claude-code/claude-sonnet-5-5" and data["worker"]["label"] == "PTY · Claude Code"
    assert (data["effort"], data["effort_source"]) == ("medium", "config")


async def test_acp_permission_prompts_under_allow_policy_and_honors_allow(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "permission")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "do it")
    [pending] = await until(lambda: rt.pending_approvals(child.id))
    assert pending["tool"] == "Antigravity · read_file — Read a.txt"
    assert pending["args"] == {"path": "a.txt"}
    assert pending["call_id"] == "agy:t1" and pending["external"] == "agy"
    assert pending["grant"].startswith("acp:")
    mode = next(entry["params"] for entry in acp_log(workspace) if entry.get("method") == "session/set_mode")
    assert mode["modeId"] == "default"  # prompt; accept-edits/auto can bypass edits
    rt.reply(child.id, pending["id"], True)
    await idle(rt)
    assert [event["data"]["outcome"] for event in rt.store.events(child.id, types=["permission.replied"])] == ["allowed"]
    assert {"permission": {"outcome": "selected", "optionId": "allow-once"}} in acp_log(workspace)
    assert any(part.text == "permission:allow-once" for message in rt.store.messages(child.id)
               for part in message.message.parts if isinstance(part, Text))
    calls = [part for message in rt.store.messages(child.id) for part in message.message.parts if isinstance(part, ToolCall)]
    assert calls[0].id == pending["call_id"] == "agy:t1"  # the card joins the tool timeline row
    assert rt.session_json(rt.session(child.id))["worker"]["status"] == "completed"


async def test_acp_permission_denied_is_honored_and_never_allowed(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "permission")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "do it")
    [pending] = await until(lambda: rt.pending_approvals(child.id))
    rt.reply(child.id, pending["id"], False)
    await idle(rt)
    assert [event["data"]["outcome"] for event in rt.store.events(child.id, types=["permission.replied"])] == ["denied"]
    assert {"permission": {"outcome": "selected", "optionId": "reject-once"}} in acp_log(workspace)
    [turn] = rt.store.turn_summaries(child.id)["turns"]
    assert turn["status"] == "failed" and "declined" in turn["error"]["message"]
    worker = rt.session_json(rt.session(child.id))["worker"]
    assert worker["status"] == "needs_attention" and worker["denied_actions"]


async def test_acp_permission_interrupt_cancels_the_wait_and_the_turn(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "permission")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "do it")
    [_pending] = await until(lambda: rt.pending_approvals(child.id))
    assert await rt.interrupt(child.id) is True
    assert rt.pending_approvals(child.id) == []
    replies = [event["data"] for event in rt.store.events(child.id, types=["permission.replied"])]
    assert len(replies) == 1 and replies[0]["outcome"] == "cancelled"
    [turn] = rt.store.turn_summaries(child.id)["turns"]
    assert turn["status"] == "interrupted"


async def test_acp_reject_only_permission_does_not_show_an_unusable_card(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "permission-reject-only")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "do it")
    await idle(rt)
    assert rt.store.events(child.id, types=["permission.asked"]) == []
    assert {"permission": {"outcome": "selected", "optionId": "reject-once"}} in acp_log(workspace)
    [turn] = rt.store.turn_summaries(child.id)["turns"]
    assert turn["status"] == "failed" and "declined" in turn["error"]["message"]


async def test_acp_persistent_permission_never_reaches_a_card(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "permission-persistent")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "do it")
    await idle(rt)
    assert rt.store.events(child.id, types=["permission.asked"]) == []
    assert {"permission": {"outcome": "cancelled"}} in acp_log(workspace)
    [turn] = rt.store.turn_summaries(child.id)["turns"]
    assert turn["status"] == "failed" and "could not obtain an approval" in turn["error"]["message"]


async def test_acp_session_grant_only_covers_the_exact_payload(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "permission")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "first")
    [pending] = await until(lambda: rt.pending_approvals(child.id))
    rt.reply(child.id, pending["id"], True, scope="session")
    await idle(rt)
    rt.prompt(child.id, "second")
    await idle(rt)
    assert len(rt.store.events(child.id, types=["permission.asked"])) == 1
    outcomes = [entry["permission"]["optionId"] for entry in acp_log(workspace) if "permission" in entry]
    assert outcomes == ["allow-once", "allow-once"]


async def test_acp_long_answer_is_persisted_once_and_clipping_is_announced(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "long-text")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "write a long answer")
    await idle(rt)
    stored = [part.text for message in rt.store.messages(child.id) if message.message.role == "assistant"
              for part in message.message.parts if isinstance(part, Text)]
    assert stored == ["x" * 66_000]  # full streamed answer once; no clipped duplicate
    worker = rt.session_json(rt.session(child.id))["worker"]
    assert worker["status"] == "completed"
    assert any("final result summary clipped" in note and "full streamed answer" in note
               for note in worker["notes"])


async def test_acp_process_record_has_transport_and_session_for_recovery(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "cancel")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "do it")
    await until(lambda: (workspace / "acp-marker.txt").exists())
    [turn] = [item for item in rt.store.running_turns() if item["session_id"] == child.id]
    record = records.read(turn["id"])
    assert record and record["provider"] == "agy" and record["transport"] == "acp-v1"
    assert record["cli_session"] == "acp-1" and isinstance(record["pid"], int) and record["started"]
    assert await rt.interrupt(child.id) is True
    assert records.read(turn["id"]) is None


@pytest.mark.parametrize("marker", [None, "stream-json-v1"])
async def test_legacy_or_unmarked_agy_is_readable_but_refused(tmp_path, monkeypatch, marker):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "text")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.store.append_message(child.id, Message("user", (Text("old context"),)))
    snapshot = rt.snapshot_for(rt.session(child.id))
    snapshot["worker"] = {**snapshot["worker"], "session_id": "conv-1", "status": "completed",
                          "policy_fingerprint": "old"}
    if marker is None:
        snapshot["worker"].pop("transport", None)
    else:
        snapshot["worker"]["transport"] = marker
    rt.store.set_snapshot(child.id, snapshot)
    before = [message.message for message in rt.store.messages(child.id)]
    rt.prompt(child.id, "continue")
    await idle(rt)
    assert [message.message for message in rt.store.messages(child.id)] == before  # no new prompt committed
    assert not (workspace / "acp-log.jsonl").exists()  # no CLI was spawned
    worker = rt.session_json(rt.session(child.id))["worker"]
    assert worker["status"] == "failed" and "legacy transport" in worker["error"]
    assert worker["transport"] == "stream-json-v1"
    [turn] = rt.store.turn_summaries(child.id)["turns"]
    assert turn["status"] == "failed" and "legacy transport" in turn["error"]["message"]


@pytest.fixture
def acp_rule_session(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "permission")
    enable()
    config = ConfigFile()
    config.patch(config.read()["revision"], {"tools.permission": {"read_file": "allow"}}, [])
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission="allow")
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    return rt, child


async def test_acp_allow_rule_cannot_bypass_the_approval_card(acp_rule_session):
    rt, child = acp_rule_session
    rt.prompt(child.id, "do it")
    [pending] = await until(lambda: rt.pending_approvals(child.id))
    assert pending["tool"] == "Antigravity · read_file — Read a.txt" and "rule" not in pending
    rt.reply(child.id, pending["id"], True)
    await idle(rt)
    assert len(rt.store.events(child.id, types=["permission.asked"])) == 1


async def test_early_interrupted_acp_turn_without_a_session_id_refuses_the_next_prompt(env, monkeypatch):
    rt, workspace, _ = env
    enable()
    from nexus.workers import agent as agent_module

    monkeypatch.setattr(agent_module, "cli_path", lambda pid: "/bin/true")
    started = asyncio.Event()
    calls = []

    class Hang:
        def __init__(self, *args, **kwargs): ...

        async def run(self, *, emit, **kwargs):
            calls.append(1)
            started.set()
            await asyncio.sleep(60)

    monkeypatch.setattr(agent_module, "AntigravityAcpWorker", Hang)
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "first")
    await asyncio.wait_for(started.wait(), 5)
    saved = await until(lambda: (worker := rt.session_json(rt.session(child.id))["worker"]).get("transport") == "acp-v1"
                        and not worker.get("session_id") and worker)
    assert saved["transport"] == "acp-v1" and not saved.get("session_id")
    assert await rt.interrupt(child.id) is True
    [first_turn] = rt.store.turn_summaries(child.id)["turns"]
    assert first_turn["status"] == "interrupted"
    before = [message.message for message in rt.store.messages(child.id)]
    assert before and before[0].role == "user"
    rt.prompt(child.id, "second")
    await idle(rt)
    assert len(calls) == 1  # no new ACP session was started
    worker = rt.session_json(rt.session(child.id))["worker"]
    assert worker["status"] == "failed" and "no saved CLI session id" in worker["error"]
    turns = rt.store.turn_summaries(child.id)["turns"]
    assert turns[-1]["status"] == "failed" and "no saved CLI session id" in turns[-1]["error"]["message"]
    assert [message.message for message in rt.store.messages(child.id)] == before  # refused prompt not committed


def test_worker_attention_why_distinguishes_acp_from_claude_and_legacy():
    acp_denied = _worker_attention_why({"transport": "acp-v1", "denied_actions": ["write_file"]})
    assert "Nexus denied the ACP permission request" in acp_denied
    assert "no approval was given" in acp_denied and "may have continued" in acp_denied
    assert "cannot answer CLI prompts" not in acp_denied
    acp_other = _worker_attention_why({"transport": "acp-v1", "denied_actions": []})
    assert "no ACP permission was bypassed" in acp_other and "cannot answer CLI prompts" not in acp_other
    assert _worker_attention_why({"denied_actions": ["read"]}) == "The CLI prompt cannot be answered by Nexus; Nexus approved nothing"
    assert _worker_attention_why({"transport": "stream-json-v1", "denied_actions": ["read"]}) == \
        "The CLI prompt cannot be answered by Nexus; Nexus approved nothing"


async def test_live_acp_permission_deny_usage_and_timeline(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "live")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "read and write")
    [pending] = await until(lambda: rt.pending_approvals(child.id))
    assert pending["tool"] == "Antigravity · write_file — File probe-write.txt"
    assert pending["args"] == {"Content": "ok", "Path": "/workspace/probe-write.txt"}
    assert pending["call_id"] == "agy:call-write" and pending["external"] == "agy"
    rt.reply(child.id, pending["id"], False)
    await idle(rt)
    assert [event["data"]["outcome"] for event in rt.store.events(child.id, types=["permission.replied"])] == ["denied"]
    assert {"permission": {"outcome": "selected", "optionId": "deny"}} in acp_log(workspace)
    usage = rt.store.session_usage(child.id)["models"]
    assert sum(item["input"] for item in usage.values()) == 10404
    assert sum(item["cached"] for item in usage.values()) == 0
    assert sum(item["output"] for item in usage.values()) == 74
    worker = rt.session_json(rt.session(child.id))["worker"]
    assert worker["status"] == "needs_attention" and worker["denied_actions"]
    [escalation] = rt.escalations[child.id]
    assert "Nexus denied the ACP permission request" in escalation
    assert "cannot answer CLI prompts" not in escalation
    [turn] = rt.store.turn_summaries(child.id)["turns"]
    assert turn["status"] == "failed" and "declined" in turn["error"]["message"]
    assert not [part for message in rt.store.messages(child.id) if message.message.role == "assistant"
               for part in message.message.parts if isinstance(part, Text)]
    calls = [part for message in rt.store.messages(child.id) for part in message.message.parts if isinstance(part, ToolCall)]
    assert {call.id for call in calls} == {"agy:call-read", "agy:call-write"}
    assert {call.name: call.raw["display"] for call in calls} == {"read_file": "read", "write_file": "write"}


async def test_live_acp_resume_does_not_replay_load_and_adds_turn_usage(tmp_path, monkeypatch):
    rt, workspace, _ = build_env(tmp_path, monkeypatch, "live")
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "first")
    [pending] = await until(lambda: rt.pending_approvals(child.id))
    rt.reply(child.id, pending["id"], False)
    await idle(rt)
    rt.prompt(child.id, "second")
    await idle(rt)
    methods = [entry["method"] for entry in acp_log(workspace) if "method" in entry]
    assert methods.count("session/new") == 1 and methods.count("session/resume") == 1
    assert "session/load" not in methods
    assert any(part.text == "resumed" for message in rt.store.messages(child.id)
               for part in message.message.parts if isinstance(part, Text))
    usage = rt.store.session_usage(child.id)["models"]
    assert sum(item["input"] for item in usage.values()) == 10404 + 2547
    assert sum(item["cached"] for item in usage.values()) == 8350
    assert sum(item["output"] for item in usage.values()) == 74 + 2
