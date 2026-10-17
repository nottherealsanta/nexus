"""Offline tests of the Antigravity worker against a fake ``agy`` executable.

The stream events reproduce the documented schema (antigravity.google/docs/cli/headless:
``init`` / ``step_update`` / ``result``); no real agy, login or model turn is ever involved.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.agents.antigravity_worker import (
    AntigravityWorker,
    WorkerConfig,
    resolve_policy,
)
from nexus.errors import ConfigError, ProviderError

FAKE = r'''#!{python}
import json, os, signal, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
scenario = json.load(open(os.path.join(here, "scenario.json")))
args = sys.argv[1:]
if args == ["--version"]:
    print(scenario.get("version", "1.3.4")); sys.exit(0)
if args == ["--help"]:
    print(scenario.get("help", "--print --input-format --output-format stream-json --model --mode auto|accept-edits|plan --print-timeout"),
          file=sys.stderr if scenario.get("help_stderr") else sys.stdout)
    sys.exit(0)
if scenario.get("session"):
    # Real streaming mode: stdin EOF ends the session, aborting a turn that is still running.
    import select
    first = sys.stdin.readline()
    json.dump({{"argv": args, "stdin": first, "cwd": os.getcwd()}}, open(os.path.join(here, "record.json"), "w"))
    lines = scenario.get("lines", [])
    sys.stdout.write(json.dumps(lines[0]) + "\n"); sys.stdout.flush()
    ready, _, _ = select.select([sys.stdin], [], [], 0.5)
    if ready and sys.stdin.readline() == "":
        sys.stderr.write("stdin closed mid-turn\n"); sys.exit(1)
    for line in lines[1:]:
        sys.stdout.write(json.dumps(line) + "\n"); sys.stdout.flush()
    sys.stdin.read()  # session stays open until the client closes stdin
    sys.exit(0)
stdin = sys.stdin.readline()  # one user event; stdin stays open until the terminal result
json.dump({{"argv": args, "stdin": stdin, "cwd": os.getcwd()}}, open(os.path.join(here, "record.json"), "w"))
for line in scenario.get("lines", []):
    sys.stdout.write((line if isinstance(line, str) else json.dumps(line)) + "\n"); sys.stdout.flush()
if scenario.get("stderr"):
    sys.stderr.write(scenario["stderr"]); sys.stderr.flush()
if scenario.get("hang"):
    child = subprocess.Popen(["sleep", "300"])
    open(os.path.join(here, "pids.json"), "w").write(json.dumps([os.getpid(), child.pid]))
    time.sleep(300)
sys.exit(scenario.get("exit", 0))
'''

MODEL = "gemini-3.8-flash-high"


def init(**fields):
    body = {"cwd": None, "tools": ["run_command", "edit_file"], "permission_mode": "request-review",
            "model": MODEL, **fields}
    body = {k: v for k, v in body.items() if v is not None}
    return {"event": "init", "conversation_id": "c1", "init": body}


def result(status="SUCCESS", response="done", **fields):
    body = {"conversation_id": "c1", "status": status, "duration_seconds": 1.5, "num_turns": 1, **fields}
    if response is not None:
        body["response"] = response
    return {"event": "result", "result": body}


def step(index, state, step_type, **fields):
    return {"event": "step_update", "step_update": {"conversation_id": "c1", "step_index": index,
                                                    "state": state, "step_type": step_type, **fields}}


INIT = init()
OK = result()
USAGE = {"input_tokens": 10418, "output_tokens": 589, "thinking_tokens": 551, "cache_read_tokens": 8113,
         "total_tokens": 11007}


def make_fake(tmp_path: Path, **scenario) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "agy"
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (bin_dir / "scenario.json").write_text(json.dumps(scenario))
    return script


def worker_for(script: Path, tmp_path: Path, **config) -> tuple[AntigravityWorker, object]:
    cfg = WorkerConfig(executable=str(script), **config)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    return AntigravityWorker(cfg, environ=env), resolve_policy(cfg, read_only=False)


def run_worker(tmp_path: Path, scenario: dict, *, prompt="do it", cancel=None, max_seconds=0.0, **config):
    script = make_fake(tmp_path, **scenario)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    worker, policy = worker_for(script, tmp_path, **config)
    events: list[tuple[str, dict]] = []

    async def emit(kind, data):
        events.append((kind, data))

    result = asyncio.run(worker.run(prompt=prompt, model=MODEL, workspace=workspace, policy=policy,
                                    max_seconds=max_seconds, cancel=cancel, emit=emit))
    return result, events, tmp_path / "bin"


# -- policy -------------------------------------------------------------------


def test_default_inheritance_and_explicit_opt_out():
    assert resolve_policy(WorkerConfig(), read_only=False).permission_mode == "auto"
    for value in (None, False):
        with pytest.raises(ConfigError, match="worker_inherits_agy_settings"):
            resolve_policy(WorkerConfig(worker_inherits_agy_settings=value), read_only=False)


def test_unenforceable_settings_are_refused_but_read_only_is_not():
    ok = WorkerConfig()
    # Read-only is prompt guidance only (2026-10-17): never a refusal or restriction.
    policy = resolve_policy(ok, read_only=True)
    assert not policy.read_only and policy.tools is None
    for override in ({"tools": ("Read",)}, {"allowed_tools": ("Bash",)}, {"disallowed_tools": ("Bash",)},
                     {"mcp_config": "{}"}, {"setting_sources": "user"}, {"restricted": True}):
        with pytest.raises(ConfigError, match="cannot be enforced"):
            resolve_policy(WorkerConfig(**override), read_only=False)
    for mode in ("dontAsk", "bypassPermissions", "yolo"):
        with pytest.raises(ConfigError, match="auto, accept-edits or plan"):
            resolve_policy(WorkerConfig(worker_inherits_agy_settings=True, permission_mode=mode), read_only=False)


def test_from_section_and_authority_disclosure():
    section = SimpleNamespace(executable=None, timeout_seconds=30, worker_permission_mode="acceptEdits",
                              worker_inherits_agy_settings=True)
    policy = resolve_policy(WorkerConfig.from_section(section), read_only=False)
    assert (policy.executable, policy.permission_mode, policy.timeout_seconds) == ("agy", "accept-edits", 30.0)
    assert resolve_policy(WorkerConfig.from_section(SimpleNamespace()), read_only=False).permission_mode == "auto"
    text = policy.authority("/w")
    assert "did not mediate" in text and "no sandbox requested" in text and "read-only" not in text
    with pytest.raises(ConfigError):
        WorkerConfig(timeout_seconds=0)


@pytest.mark.parametrize("section", [None, SimpleNamespace(), SimpleNamespace(worker_inherits_agy_settings=None)])
def test_omitted_section_option_uses_default(section):
    assert WorkerConfig.from_section(section).worker_inherits_agy_settings is True
    assert resolve_policy(WorkerConfig.from_section(section), read_only=False).permission_mode == "auto"


def test_section_explicit_opt_out_is_not_defaulted():
    with pytest.raises(ConfigError, match="worker_inherits_agy_settings"):
        resolve_policy(WorkerConfig.from_section(SimpleNamespace(worker_inherits_agy_settings=False)), read_only=False)


def test_preflight_help_on_stderr(tmp_path):
    res, _, bin_dir = run_worker(tmp_path, {"help_stderr": True, "lines": [INIT, OK]})
    assert res.status == "completed"
    assert "--print" not in json.loads((bin_dir / "record.json").read_text())["argv"]


def test_streaming_preflight_does_not_require_prompt_flag(tmp_path):
    res, _, _ = run_worker(tmp_path, {
        "help": "--input-format --output-format stream-json --model --mode auto --print-timeout",
        "lines": [INIT, OK],
    })
    assert res.status == "completed"


@pytest.mark.parametrize("flag", ["--input-format", "--output-format", "--model", "--mode", "--print-timeout"])
def test_preflight_requires_headless_flags(tmp_path, flag):
    help_text = "--print --input-format --output-format stream-json --model --mode auto|accept-edits|plan --print-timeout"
    # Token replacement must not accidentally remove --print-timeout too.
    help_text = " ".join(token for token in help_text.split() if token != flag)
    with pytest.raises(ProviderError, match="required headless flags") as refused:
        run_worker(tmp_path, {"help": help_text, "help_stderr": True})
    assert flag in str(refused.value)
    assert not (tmp_path / "bin" / "record.json").exists()


# -- runs ---------------------------------------------------------------------


def test_stdin_stays_open_until_result_in_streaming_session(tmp_path):
    """Regression: closing stdin right after the prompt ends agy's session and aborts the turn."""
    res, _, bin_dir = run_worker(tmp_path, {"session": True, "lines": [INIT, OK]}, timeout_seconds=20)
    assert res.status == "completed", (res.status, res.error, res.diagnostics)
    record = json.loads((bin_dir / "record.json").read_text())
    assert json.loads(record["stdin"])["message"]["content"] == "do it"
    assert "--print" not in record["argv"] and "-p" not in record["argv"]


def test_success_argv_stdin_events(tmp_path):
    lines = [INIT,
             step(1, "ACTIVE", "agent_response", text_delta="work"),
             step(1, "DONE", "agent_response", text_delta="ing"),
             step(2, "ACTIVE", "tool", tool_name="run_command",
                  tool_info={"name": "run_command", "parameters": {"CommandLine": "echo hi"}}),
             step(2, "DONE", "tool", tool_name="run_command", tool_info={
                 "name": "run_command", "parameters": {"CommandLine": "echo hi"},
                 "output": "ok sk-ant-api03-" + "x" * 40,
                 "subagent_info": [{"type_name": "explorer", "role": "scan", "conversation_id": "c2",
                                    "log_uri": "file:///l", "workspace_uris": ["file:///w"]}]}),
             step(3, "DONE", "tool", tool_name="bad", tool_info={
                 "name": "bad", "error": {"type": "Denied", "message": "nope"}}),
             {"event": "future_event", "x": 1}, "not json", result(usage=USAGE)]
    res, events, bin_dir = run_worker(tmp_path, {"lines": lines, "stderr": "diag sk-ant-api03-" + "y" * 40}, prompt="hello é")
    assert res.status == "completed" and not res.is_error and res.text.startswith("done")
    assert "malformed" in res.text  # announced, not fatal
    record = json.loads((bin_dir / "record.json").read_text())
    assert json.loads(record["stdin"]) == {"event": "user", "message": {"content": "hello é"}}
    assert "hello" not in " ".join(record["argv"])
    assert Path(record["cwd"]).resolve() == (tmp_path / "ws").resolve()
    argv = record["argv"]
    assert argv[:9] == ["--input-format", "stream-json", "--output-format", "stream-json", "--model", MODEL,
                        "--mode", "auto", "--print-timeout"]
    assert argv[9].endswith("s") and argv[9][:-1].isdigit() and "-p" not in argv and "--print" not in argv
    assert not any("dangerous" in a or "yolo" in a or "sandbox" in a or "always-proceed" in a or a == "--yes"
                   for a in argv)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "external.started" and kinds[-1] == "external.finished"
    assert [d["text"] for k, d in events if k == "text"] == ["working"]
    tool = [d for k, d in events if k == "external.tool"]
    assert [t["phase"] for t in tool] == ["requested", "completed", "requested", "completed"]
    assert tool[0]["tool"] == "run_command" and tool[0]["input"]["CommandLine"] == "echo hi"
    assert "subagents:" in tool[1]["content"][0]["text"] and "explorer" in tool[1]["content"][0]["text"]
    assert tool[3]["is_error"] and "Denied" in tool[3]["content"][0]["text"] and not tool[1]["is_error"]
    assert all(t["provenance"] == "Antigravity (own tools)" and t["provider"] == "antigravity" for t in tool)
    assert "sk-ant" not in json.dumps(tool)
    finished = events[-1][1]
    assert finished["usage"] == USAGE and finished["cost"] == "unknown" and res.tool_calls == 2
    assert finished["agy_tools"] == ["run_command", "edit_file"]
    assert finished["agy_permission_mode"] == "request-review" and finished["agy_session"] == "c1"
    assert "sk-ant" not in res.diagnostics


def test_no_usage_is_unknown_not_fabricated(tmp_path):
    res, events, _ = run_worker(tmp_path, {"lines": [INIT, OK]})
    assert res.status == "completed" and events[-1][1]["usage"] == "unknown"


def test_eof_without_terminal_event_is_not_success(tmp_path):
    res, *_ = run_worker(tmp_path, {"lines": [INIT, step(1, "ACTIVE", "agent_response", text_delta="half done")]})
    assert res.status == "failed" and res.is_error
    assert "without a terminal result" in res.error and "half done" in res.text


@pytest.mark.parametrize("lines", [
    [INIT, result("FAILED", "x")],
    [INIT, result("success", "x")],                  # exact SUCCESS only
    [INIT, result("MAX_TURNS", None)],
    [INIT, result("SUCCESS", None)],                 # no response string
    [INIT, {"event": "result", "result": "SUCCESS"}],
    [result()],                                      # success without init
    [step(1, "DONE", "agent_response", text_delta="x"), INIT, OK],  # work before init
])
def test_unrecognised_terminal_fails_closed(tmp_path, lines):
    res, *_ = run_worker(tmp_path, {"lines": lines})
    assert res.status == "failed" and res.is_error


@pytest.mark.parametrize("response", ["", "   \n"])
def test_success_with_empty_answer_needs_attention(tmp_path, response):
    lines = [INIT, step(1, "DONE", "tool", tool_name="run_command",
                        tool_info={"name": "run_command", "error": {"type": "Denied", "message": "soft-denied"}}),
             result("SUCCESS", response)]
    res, events, _ = run_worker(tmp_path, {"lines": lines})
    assert res.status == "needs_attention" and res.is_error
    assert "permissions.allow" in res.attention and "approved nothing" in res.text
    assert events[-1][1]["status"] == "needs_attention"


def test_denied_tool_step_closes_and_names_the_denial_and_mode_fallback(tmp_path):
    # Shapes recorded from agy 1.3.6 when --mode auto is unavailable for the account.
    denial = "permission check failed for unsandboxed \"echo hi\": user denied permission to run command"
    lines = [INIT,
             step(2, "ACTIVE", "tool", tool_name="run_command",
                  tool_info={"name": "run_command", "parameters": {"CommandLine": "echo hi"}}),
             step(2, "ERROR", "tool", tool_name="run_command",
                  tool_info={"name": "run_command", "parameters": {"CommandLine": "echo hi"},
                             "error": {"type": "TOOL_ERROR", "message": denial}}),
             result("SUCCESS", "", denied_actions=[{"action": "command", "display_name": "RunCommand"}])]
    stderr = "warning: --mode auto is not available for this account; continuing in the default mode.\n"
    res, events, _ = run_worker(tmp_path, {"lines": lines, "stderr": stderr})
    tools = [d for k, d in events if k == "external.tool"]
    assert [t["phase"] for t in tools] == ["requested", "completed"]
    assert tools[1]["is_error"] and "user denied permission" in tools[1]["content"][0]["text"]
    assert res.status == "needs_attention"
    assert "RunCommand (command permission)" in res.attention and "command(<target>)" in res.attention
    assert "ignored the requested --mode auto" in res.attention and "request-review" in res.attention
    finished = events[-1][1]
    assert finished["denied_actions"] == ["RunCommand (command permission)"]
    assert "--mode auto is not available" in finished["mode_note"]


def test_error_tool_step_without_error_payload_is_marked_failed(tmp_path):
    lines = [INIT, step(1, "ERROR", "tool", tool_name="view_file", tool_info={"name": "view_file"}), OK]
    res, events, _ = run_worker(tmp_path, {"lines": lines})
    done = [d for k, d in events if k == "external.tool" and d["phase"] == "completed"]
    assert done and done[0]["is_error"] and "ERROR" in done[0]["content"][0]["text"]
    assert res.status == "completed"


def test_success_with_empty_response_uses_finished_agent_text(tmp_path):
    lines = [INIT, step(1, "DONE", "agent_response", text_delta="the sentinel"), result("SUCCESS", "")]
    res, *_ = run_worker(tmp_path, {"lines": lines})
    assert res.status == "completed" and res.text.startswith("the sentinel")


def test_non_success_status_keeps_partial_answer_and_usage(tmp_path):
    lines = [INIT, step(1, "ACTIVE", "agent_response", text_delta="partway"), result("TIMEOUT", "", usage=USAGE)]
    res, events, _ = run_worker(tmp_path, {"lines": lines})
    assert res.status == "failed" and "TIMEOUT" in res.error and "partway" in res.text
    assert events[-1][1]["usage"] == USAGE


def test_sibling_subagent_metadata_is_emitted_without_raw_json(tmp_path):
    metadata = [{"type_name": "Research", "conversation_id": "child-1"}]
    lines = [INIT, step(1, "ACTIVE", "thinking", subagent_info=metadata), OK]
    res, events, _ = run_worker(tmp_path, {"lines": lines})
    assert res.status == "completed"
    tools = [body for name, body in events if name == "external.tool"]
    assert len(tools) == 1 and tools[0]["tool"] == "subagents"
    text = tools[0]["content"][0]["text"]
    assert "type_name=\"Research\"" in text and "conversation_id=\"child-1\"" in text
    assert "{" not in text


def test_always_proceed_is_refused_even_if_inherited(tmp_path):
    res, *_ = run_worker(tmp_path, {"lines": [init(permission_mode="always-proceed"), OK]})
    assert res.status == "failed" and "always-proceed" in res.error and "done" not in res.text


def test_nonzero_exit_after_success_is_failure(tmp_path):
    res, *_ = run_worker(tmp_path, {"lines": [INIT, OK], "exit": 3})
    assert res.status == "failed" and "code 3" in res.error


@pytest.mark.parametrize("scenario,needle", [
    ({"lines": [], "stderr": "Error: not logged in", "exit": 1}, "authentication required"),
    ({"lines": [], "stderr": "Do you trust this folder?", "exit": 1}, "trust"),
    ({"lines": [{"event": "permission_request", "tool": "run_command"}]}, "permission"),
    ({"lines": [INIT, result("ERROR", "please sign in")]}, "authentication required"),
])
def test_attention_cases(tmp_path, scenario, needle):
    res, *_ = run_worker(tmp_path, scenario)
    assert res.status == "needs_attention" and res.attention
    assert needle in res.attention.lower() or needle in res.text.lower()
    assert "approved nothing" in res.text


def test_init_cwd_or_model_mismatch_is_refused(tmp_path):
    res, *_ = run_worker(tmp_path, {"lines": [init(cwd="/somewhere/else"), OK]})
    assert res.status == "failed" and "cwd" in res.error
    res, *_ = run_worker(tmp_path, {"lines": [init(model="other"), OK]})
    assert res.status == "failed" and "model" in res.error
    res, *_ = run_worker(tmp_path, {"lines": [init(cwd=str(tmp_path / "ws")), OK]})
    assert res.status == "completed"


def test_oversized_line_dropped_and_bounded(tmp_path):
    big = "x" * (5 * 1024 * 1024)
    lines = ["{" + big, INIT, step(1, "DONE", "agent_response", text_delta="a" * 100_000), OK]
    res, *_ = run_worker(tmp_path, {"lines": lines})
    assert res.status == "completed"
    assert "over-long" in res.text


def test_deadline_kills_whole_process_group(tmp_path):
    started = time.monotonic()
    res, _, bin_dir = run_worker(tmp_path, {"lines": [INIT], "hang": True}, max_seconds=1.0)
    assert res.status == "interrupted" and time.monotonic() - started < 15
    pids = json.loads((bin_dir / "pids.json").read_text())
    time.sleep(0.3)
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_cancel_terminates_and_reports_cancelled(tmp_path):
    cancel = SimpleNamespace(cancelled=False)

    async def go():
        script = make_fake(tmp_path, lines=[INIT], hang=True)
        (tmp_path / "ws").mkdir(exist_ok=True)
        worker, policy = worker_for(script, tmp_path)
        task = asyncio.create_task(worker.run(prompt="p", model=MODEL, workspace=tmp_path / "ws",
                                              policy=policy, cancel=cancel))
        await asyncio.sleep(1.0)
        cancel.cancelled = True
        return await asyncio.wait_for(task, 15)

    res = asyncio.run(go())
    assert res.status == "cancelled"
    for pid in json.loads((tmp_path / "bin" / "pids.json").read_text()):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# -- refusals before launch -----------------------------------------------------


def _expect(tmp_path, exc, **kwargs):
    script = make_fake(tmp_path, lines=[OK], **kwargs.pop("scenario", {}))
    (tmp_path / "ws").mkdir(exist_ok=True)
    worker, policy = worker_for(script, tmp_path)
    args = {"prompt": "p", "model": MODEL, "workspace": tmp_path / "ws", "policy": policy, **kwargs}
    with pytest.raises(exc):
        asyncio.run(worker.run(**args))
    return tmp_path / "bin"


def test_prompt_model_and_workspace_refusals_never_launch(tmp_path):
    for bad in ({"prompt": "x" * (64 * 1024 + 1)}, {"prompt": "a\x00b"}, {"model": "--dangerously-skip"},
                {"model": "sonnet latest"}, {"workspace": tmp_path / "missing"}):
        bin_dir = _expect(tmp_path, ConfigError, **bad)
        assert not (bin_dir / "record.json").exists()


def test_preflight_rejects_missing_flags_and_bad_version(tmp_path):
    bin_dir = _expect(tmp_path, ProviderError, scenario={"help": "--model --output-format stream-json"})
    assert not (bin_dir / "record.json").exists()
    _expect(tmp_path, ProviderError, scenario={"version": "garbage"})


def test_closed_worker_refuses(tmp_path):
    script = make_fake(tmp_path, lines=[OK])
    worker, policy = worker_for(script, tmp_path)

    async def go():
        await worker.aclose()
        await worker.run(prompt="p", model=MODEL, workspace=tmp_path, policy=policy)

    with pytest.raises(ProviderError, match="closed"):
        asyncio.run(go())
