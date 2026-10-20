"""Offline code-mode evaluation (plan 03, E1): direct tool calls vs the ``code`` tool.

Every scenario runs through the real ``Runtime`` (real tools, ``PathGuard``,
permissions, and for MCP a real stdio subprocess) with a ``ScriptedProvider``.
The "model" is a fixed script, so the numbers are **mechanical context-volume and
call-count measurements of two hand-written strategies**. They say nothing about
whether a live model would pick, write or debug either strategy. See
``docs/devtools.md`` ("Code-mode evaluation").

    .venv/bin/python benchmark/codemode_eval.py [--json] [--scenario NAME]

Code-mode variants need the optional ``pydantic-monty`` and are reported as
``skipped`` when it is absent. Nothing here touches the network or a provider.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nexus.config import Config  # noqa: E402
from nexus.config.schema import (  # noqa: E402
    AgentSection, CodeModeSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection)
from nexus.mcp.client import MCPClient  # noqa: E402
from nexus.model.message import Text, ToolResult, ToolUse  # noqa: E402
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response  # noqa: E402
from nexus.runtime import Runtime  # noqa: E402

FILES = 30
TODO_EVERY = 4
RECORD_IDS = (51, 85, 136, 170)  # open + critical in benchmark/mcp_records.py
NEEDLE = "NEEDLE-7731"
CHILD_FILES = 24
SKIP_NO_MONTY = "pydantic-monty unavailable (install nexus[codemode])"


def code_mode_available() -> bool:
    from nexus.tools.codemode.tool import availability
    return bool(availability()["available"])


def make_config(*, code: bool, context: bool = False, mode: str = "allow") -> Config:
    return Config(model="scripted/m", version=2, v2=ConfigV2(
        model=ModelSection(default="scripted/m"),
        agent=AgentSection(profile="coding"),
        permissions=PermissionsSection(mode=mode, on_unattended="deny"),
        tools=ToolsSection(),
        code_mode=CodeModeSection(enabled=code, context=context)))


# -- fixtures ------------------------------------------------------------------

def seed_files(workspace: Path) -> None:
    src = workspace / "src"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(FILES):
        lines = [f"line {k} of module {i}" for k in range(60)]
        if i % TODO_EVERY == 0:
            lines[30] = f"# TODO(o{i}): fix thing {i}"
        (src / f"m{i:02d}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def seed_mcp(workspace: Path) -> None:
    agents = workspace / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "mcp.json").write_text(json.dumps({"servers": {"records": {
        "transport": "stdio", "tool_loading": "all", "command": sys.executable,
        "args": [str(ROOT / "benchmark" / "mcp_records.py")],
        "connect_timeout_s": 10, "init_timeout_s": 10, "list_timeout_s": 10,
        "call_timeout_s": 10}}}), encoding="utf-8")


def seed_child_data(workspace: Path) -> None:
    data = workspace / "data"
    data.mkdir(parents=True, exist_ok=True)
    for i in range(CHILD_FILES):
        body = "\n".join(f"record {i}.{k}: nothing interesting here" for k in range(80))
        if i == CHILD_FILES // 2:
            body += f"\nfinding: {NEEDLE} cache key collision"
        (data / f"d{i:02d}.txt").write_text(body + "\n", encoding="utf-8")


# -- measurement ---------------------------------------------------------------

def _result_text(block: ToolResult) -> str:
    return "".join(p.text for p in block.content if isinstance(p, Text))


def finished_nested_calls(events) -> list:
    """Final ``code.call`` rows. The store truncates oversized payloads to
    ``{"_truncated": ...}`` (no ``status``); only finished rows carry a result,
    so a running row (small, never truncated) always keeps its ``status``."""
    return [e for e in events if e.type == "code.call"
            and (e.data.get("status") != "running" if "status" in e.data else "_truncated" in e.data)]


def measure(session, provider: ScriptedProvider, events, expected, forbidden=()) -> dict:
    """Model-visible volume of a finished session, plus a content grade."""
    results, call_bytes, top_calls = [], 0, 0
    for message in session.messages:
        for block in message.content:
            if isinstance(block, ToolResult):
                results.append(_result_text(block))
            elif isinstance(block, ToolUse):
                top_calls += 1
                call_bytes += len(json.dumps(block.input).encode())
    joined = "\n".join(results)
    nested = finished_nested_calls(events)
    import msgspec
    tool_defs = len(msgspec.json.encode(provider.requests[0].tools))
    return {
        "model_requests": provider.calls,
        "top_level_tool_calls": top_calls,
        "nested_tool_calls": len(nested),
        "tool_result_bytes": len(joined.encode()),
        "tool_call_input_bytes": call_bytes,
        "tool_definition_bytes": tool_defs,
        "tool_errors": sum(1 for m in session.messages for b in m.content
                           if isinstance(b, ToolResult) and b.is_error),
        "expected_found": [s for s in expected if s in joined],
        "expected_missing": [s for s in expected if s not in joined],
        "forbidden_present": [s for s in forbidden if s in joined],
        "passed": all(s in joined for s in expected) and not any(s in joined for s in forbidden),
    }


async def drain(session, text: str, decide=None) -> list:
    events = []
    async for event in session.send(text):
        events.append(event)
        if event.type == "permission.requested" and decide is not None:
            assert session.resolve_permission(event.data["id"], decide)
    return events


async def run_single(name, workspace, scripts, config, prompt, expected, forbidden=()):
    provider = ScriptedProvider(*scripts)
    runtime = Runtime(workspace, config=config, providers={"scripted": provider},
                      mcp_client_factory=lambda cfg: MCPClient(cfg, backend="native"))
    try:
        session = runtime.session(name)
        events = await drain(session, prompt)
        ok = bool(events) and events[-1].type == "turn.completed"
        out = measure(session, provider, events, expected, forbidden)
        out["turn_completed"] = ok
        out["passed"] = out["passed"] and ok
        if provider.remaining:
            out["passed"] = False
            out["unused_scripts"] = provider.remaining
        return out
    finally:
        await runtime.aclose()


# -- scenarios -----------------------------------------------------------------

def _paths():
    return [f"src/m{i:02d}.py" for i in range(FILES)]


def _todo_markers():
    return [f"TODO(o{i})" for i in range(0, FILES, TODO_EVERY)]


FANOUT_READ_PROGRAM = (
    "import asyncio\n"
    f"paths = {_paths()!r}\n"
    "texts = await asyncio.gather(*[read(path=p) for p in paths])\n"
    "todos = []\n"
    "for n in range(len(paths)):\n"
    "    for line in texts[n].splitlines():\n"
    "        if 'TODO' in line:\n"
    "            todos.append(paths[n] + ': ' + line.strip())\n"
    "print(len(texts), 'files read')\n"
    "print('\\n'.join(todos))\n")

# Eight narrow patterns (one TODO each) and four broad ones (hundreds of lines).
GREP_PATTERNS = [f"TODO\\(o{i}\\)" for i in range(0, FILES, TODO_EVERY)] + [
    f"line {d} of module" for d in (1, 2, 3, 4)]

FANOUT_GREP_PROGRAM = (
    "import asyncio\n"
    f"patterns = {GREP_PATTERNS!r}\n"
    "outs = await asyncio.gather(*[grep(pattern=p, path='src') for p in patterns])\n"
    "for n in range(len(patterns)):\n"
    "    lines = [x for x in outs[n].splitlines() if x.strip()]\n"
    "    print(patterns[n], len(lines), 'lines;', lines[0][:120] if lines else '-')\n")

MCP_PROGRAM = (
    "import asyncio\n"
    "import json\n"
    "text = await mcp__records__list_records()\n"
    "rows = json.loads(text[text.index('['):text.rindex(']') + 1])\n"
    "hot = [r for r in rows if r['state'] == 'open' and r['severity'] == 'critical']\n"
    "full = await asyncio.gather(*[mcp__records__get_record(id=r['id']) for r in hot])\n"
    "print(len(rows), 'records,', len(hot), 'open critical')\n"
    "for text in full:\n"
    "    r = json.loads(text[text.index('{'):text.rindex('}') + 1])\n"
    "    print(r['id'], r['title'].strip(), '|', r['body'][:40])\n")


def scenario_fanout_read(variant):
    if variant == "direct":
        scripts = [tool_response(*[(f"r{i}", "read", {"path": p}) for i, p in enumerate(_paths())]),
                   text_response("done")]
    else:
        scripts = [tool_response(("c1", "code", {"program": FANOUT_READ_PROGRAM,
                                                  "purpose": "read all modules, list TODOs"})),
                   text_response("done")]
    return scripts, "List every TODO in src/.", _todo_markers(), ()


def scenario_fanout_grep(variant):
    if variant == "direct":
        scripts = [tool_response(*[(f"g{i}", "grep", {"pattern": p, "path": "src"})
                                   for i, p in enumerate(GREP_PATTERNS)]),
                   text_response("done")]
    else:
        scripts = [tool_response(("c1", "code", {"program": FANOUT_GREP_PROGRAM,
                                                  "purpose": "twelve greps, summarised"})),
                   text_response("done")]
    return scripts, "Count matches for twelve patterns.", _todo_markers(), ()


def scenario_mcp_list_filter(variant):
    ids = list(RECORD_IDS)
    if variant == "direct":
        scripts = [tool_response(("l1", "mcp__records__list_records", {})),
                   tool_response(*[(f"g{i}", "mcp__records__get_record", {"id": i}) for i in ids]),
                   text_response("done")]
    else:
        scripts = [tool_response(("c1", "code", {"program": MCP_PROGRAM,
                                                  "purpose": "list, filter, fetch"})),
                   text_response("done")]
    expected = [f"Issue {i}:" for i in ids]
    # Closed critical issues (17, 34) must not leak through the program's filter.
    # The direct arm legitimately sees them in the full list, so it has no check.
    forbidden = ("Issue 17:", "Issue 34:") if variant == "code" else ()
    return scripts, "Show open critical issues in full.", expected, forbidden


SIMPLE = {
    "fanout_read": (scenario_fanout_read, seed_files),
    "fanout_grep": (scenario_fanout_grep, seed_files),
    "mcp_list_filter": (scenario_mcp_list_filter, lambda w: seed_mcp(w)),
}


async def run_simple(name: str, variant: str, workspace: Path) -> dict:
    build, seed = SIMPLE[name]
    seed(workspace)
    scripts, prompt, expected, forbidden = build(variant)
    return await run_single(f"{name}-{variant}", workspace, scripts,
                            make_config(code=variant == "code"), prompt, expected, forbidden)


CHILD_PROGRAM = (
    f"hits = await context_search(needle={NEEDLE!r}, target=targets[0])\n"
    "calls = await context_tool_calls(target=targets[0], stop=1)\n"
    "meta = await context_meta(target=targets[0])\n"
    "report = await context_report(target=targets[0])\n"
    "print(meta)\n"
    "print(str(hits)[:600])\n"
    "print(str(report)[:300])\n")


def _child_name(runtime) -> str:
    project = runtime.sessions.store.project_id
    rows = runtime.sessions.store.db._connection().execute(
        "SELECT name FROM sessions WHERE project_id=? AND namespace='agents' AND kind='child'",
        (project,),
    ).fetchall()
    assert len(rows) == 1, rows
    return rows[0][0]


async def run_long_child(workspace: Path) -> dict:
    """A child with a long transcript; the root queries it through ``context_*``.

    Reports the child's full transcript size against what reaches the root's
    model. There is no direct-tool equivalent (a root has no tool that reads a
    child transcript), so this is a single arm, not a comparison.
    """
    seed_child_data(workspace)
    names: list[str] = []
    reads = tool_response(*[(f"d{i}", "read", {"path": f"data/d{i:02d}.txt"})
                            for i in range(CHILD_FILES)])

    def program_step(request):
        return list(tool_response(("k1", "code", {
            "program": CHILD_PROGRAM, "purpose": "query child transcript", "targets": list(names)})))

    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "scan data/ for the finding",
                                          "subagent_type": "task"})),
        reads,
        text_response(f"CHILD-REPORT found {NEEDLE} in the middle file"),
        text_response("delegated"),
        [program_step],
        text_response("done"))
    runtime = Runtime(workspace, config=make_config(code=True, context=True, mode="ask"),
                      providers={"scripted": provider})
    try:
        session = runtime.session("long-child").mark_attended()
        await drain(session, "delegate", decide="allow_once")
        names.append(_child_name(runtime))
        events = await drain(session, "query the child", decide="allow_once")
        # The child's last model request (script 3) carries its whole transcript so far.
        child_request = provider.requests[2].messages
        transcript = sum(len(_result_text(b).encode()) for m in child_request
                         for b in m.content if isinstance(b, ToolResult))
        child_records = len(child_request) + 1  # plus its final report
        finished = [e for e in events if e.type == "code.finished"]
        surfaced = ""
        for message in session.messages:
            for block in message.content:
                if isinstance(block, ToolResult):
                    surfaced = _result_text(block)  # the last one is the code result
        nested = finished_nested_calls(events)
        ok = (bool(finished) and finished[0].data["status"] == "ok"
              and NEEDLE in surfaced and "CHILD-REPORT" in surfaced and events[-1].type == "turn.completed")
        return {
            "model_requests": provider.calls,
            "child_messages": child_records,
            "child_tool_result_bytes": transcript,
            "nested_tool_calls": len(nested),
            "surfaced_to_root_bytes": len(surfaced.encode()),
            "reduction_ratio": round(transcript / max(1, len(surfaced.encode())), 1),
            "expected_found": [s for s in (NEEDLE, "CHILD-REPORT") if s in surfaced],
            "passed": ok,
        }
    finally:
        await runtime.aclose()


# -- driver --------------------------------------------------------------------

SCENARIOS = [*SIMPLE, "long_child_context"]


async def run_scenario(name: str, base: Path, *, monty: bool | None = None) -> dict:
    monty = code_mode_available() if monty is None else monty
    out: dict = {"scenario": name}
    if name == "long_child_context":
        if not monty:
            out["code"] = {"skipped": SKIP_NO_MONTY}
            return out
        workspace = base / f"{name}-code"
        workspace.mkdir(parents=True)
        out["code"] = await run_long_child(workspace)
        return out
    for variant in ("direct", "code"):
        if variant == "code" and not monty:
            out[variant] = {"skipped": SKIP_NO_MONTY}
            continue
        workspace = base / f"{name}-{variant}"
        workspace.mkdir(parents=True)
        out[variant] = await run_simple(name, variant, workspace)
    d, c = out["direct"], out["code"]
    if "skipped" not in c:
        out["ratio"] = {
            "tool_result_bytes": round(d["tool_result_bytes"] / max(1, c["tool_result_bytes"]), 1),
            "model_requests": f"{d['model_requests']} vs {c['model_requests']}",
        }
    return out


async def run_all(names=None, base: Path | None = None) -> list[dict]:
    selected = list(names or SCENARIOS)
    unknown = [n for n in selected if n not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}; choose from {', '.join(SCENARIOS)}")
    own = base is None
    base = base or Path(tempfile.mkdtemp(prefix="nexus-codemode-eval-"))
    try:
        return [await run_scenario(n, base) for n in selected]
    finally:
        if own:
            shutil.rmtree(base, ignore_errors=True)


def render(results: list[dict]) -> str:
    cols = ("model_requests", "top_level_tool_calls", "nested_tool_calls", "tool_result_bytes",
            "tool_call_input_bytes", "tool_definition_bytes")
    lines = []
    for r in results:
        lines.append(f"== {r['scenario']}")
        for variant in ("direct", "code"):
            if variant not in r:
                continue
            m = r[variant]
            if "skipped" in m:
                lines.append(f"  {variant:7} skipped: {m['skipped']}")
            elif r["scenario"] == "long_child_context":
                lines.append("  " + f"{variant:7} " + " ".join(
                    f"{k}={m[k]}" for k in ("child_messages", "child_tool_result_bytes",
                                            "surfaced_to_root_bytes", "reduction_ratio",
                                            "nested_tool_calls")) + f" pass={m['passed']}")
            else:
                lines.append("  " + f"{variant:7} " + " ".join(
                    f"{k}={m[k]}" for k in cols if k in m) + f" pass={m['passed']}")
        if "ratio" in r:
            lines.append(f"  direct/code tool_result_bytes = {r['ratio']['tool_result_bytes']}x")
    lines.append("scripted strategies, mechanical measurements only; not evidence of live model quality")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, help="repeatable; default all")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)
    results = asyncio.run(run_all(args.scenario))
    print(json.dumps(results, indent=2, sort_keys=True) if args.json else render(results))
    failed = any(not v.get("passed", True) for r in results for k, v in r.items()
                 if isinstance(v, dict) and k in ("direct", "code") and "skipped" not in v)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
