"""Offline tests of the external Claude Code worker; the real ``claude`` is never run."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from types import SimpleNamespace

import pytest
from claude_code_fakes import install, invocation, stdin_log

from nexus.agents import claude_code_worker as cw
from nexus.config.schema import ProviderSection
from nexus.errors import ConfigError, ProviderError

MODEL = "claude-haiku-4-5"


@pytest.fixture(autouse=True)
def machine_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "nexus_home", lambda: tmp_path / ".nexus")


@pytest.fixture
def fake(tmp_path):
    directory = tmp_path / "bin"

    def make(**config):
        return directory, install(directory, **config)

    return make


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "work space"
    path.mkdir()
    return path


def worker_for(tmp_path, executable, *, read_only=False, **config):
    cfg = cw.WorkerConfig(executable=executable, config_dir=tmp_path / "claude-home", **config)
    policy = cw.resolve_policy(cfg, read_only=read_only)
    return cw.ClaudeCodeWorker(cfg, environ={"PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": "secret"}), policy


async def run(tmp_path, workspace, executable, *, prompt="do it", cancel=None,
              read_only=False, **config):
    worker, policy = worker_for(tmp_path, executable, read_only=read_only, **config)
    events: list[tuple[str, dict]] = []

    async def emit(kind, data):
        events.append((kind, data))

    result = await worker.run(prompt=prompt, model=MODEL, workspace=workspace, policy=policy,
                              cancel=cancel, emit=emit)
    return result, events


def kinds(events):
    return [kind for kind, _ in events]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_own_tool_activity_is_recorded_as_external_events(tmp_path, workspace, fake):
    directory, exe = fake(mode="success", dsr=True)
    result, events = await run(tmp_path, workspace, exe, prompt="inspect a.py")
    assert result.status == "completed" and not result.is_error
    assert result.text == "All done." and result.stop_event == "Stop"
    assert result.tool_calls == 1 and result.transcript_path.endswith(f"{result.claude_session}.jsonl")
    # Own tools: external.* events with provenance, never disguised as Nexus tool.* events.
    assert "tool.requested" not in kinds(events) and "tool.completed" not in kinds(events)
    tool_events = [d for k, d in events if k == "external.tool"]
    assert [d["phase"] for d in tool_events] == ["requested", "completed"]
    assert {d["provenance"] for d in tool_events} == {cw.PROVENANCE}
    assert tool_events[0]["tool"] == "Read" and tool_events[0]["input"] == {"file_path": "a.py"}
    assert tool_events[1]["content"] == [{"type": "text", "text": "print(1)"}] and tool_events[1]["is_error"] is False
    texts = [d for k, d in events if k == "text"]
    assert [d["text"] for d in texts] == ["Looking around.", "All done."]
    assert texts[0]["provider"] == "claude-code" and texts[0]["model"] == "claude-haiku-4-5"
    started = next(d for k, d in events if k == "external.started")
    assert "did not mediate" in started["authority"] and started["permission_mode"] == "dontAsk"
    finished = next(d for k, d in events if k == "external.finished")
    assert finished["status"] == "completed" and finished["usage"] == "unknown"
    assert kinds(events)[0] == "external.started" and kinds(events)[-1] == "external.finished"
    # The process was launched in the assigned workspace, interactively, and then killed.
    seen = invocation(directory)
    assert seen["cwd"] == str(workspace.resolve()) and seen["tty"]
    assert not alive(seen["pid"])
    # Only a cursor-position reply was ever written to the terminal.
    assert stdin_log(directory) == b"\x1b[1;1R"


async def test_argv_has_no_bypass_flags_and_isolates_extensions(tmp_path, workspace, fake):
    directory, exe = fake(mode="success")
    await run(tmp_path, workspace, exe, prompt="-x tricky prompt")
    args = invocation(directory)["args"]
    flags = [a for a in args[:-2] if a.startswith("-")]
    assert not any("bypass" in a.lower() or "dangerously" in a for a in flags)
    assert args[args.index("--permission-mode") + 1] != "bypassPermissions"
    assert "-p" not in args and "--print" not in args
    assert "--tools" not in args  # default set; never `--tools ""`
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--setting-sources") + 1] == ""
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--strict-mcp-config" in args and "--restricted" in args and "--disable-slash-commands" in args
    # Nexus never answers a question menu, so Claude may not open one.
    assert args[args.index("--disallowedTools") + 1] == "AskUserQuestion"
    assert args[args.index("--model") + 1] == MODEL
    assert args[-2:] == ["--", "-x tricky prompt"]
    settings = json.loads(open(args[args.index("--settings") + 1]).read()) if os.path.exists(
        args[args.index("--settings") + 1]) else None
    assert settings is None  # the temporary hook directory is removed afterwards


async def test_explicit_policy_flags(tmp_path, workspace, fake):
    directory, exe = fake(mode="success")
    await run(tmp_path, workspace, exe, tools=("Read", "Edit"), allowed_tools=("Edit",),
              disallowed_tools=("Bash(rm *)",), permission_mode="acceptEdits", restricted=False,
              setting_sources="user", mcp_config='{"mcpServers": {"x": {}}}')
    args = invocation(directory)["args"]
    assert args[args.index("--tools") + 1] == "Read,Edit"
    assert args[args.index("--allowedTools") + 1] == "Edit"
    assert args[args.index("--disallowedTools") + 1] == "Bash(rm *),AskUserQuestion"
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert args[args.index("--setting-sources") + 1] == "user"
    assert "--restricted" not in args and "--disable-slash-commands" not in args
    assert "x" in json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]


async def test_read_only_role_gets_claude_read_only_tools(tmp_path, workspace, fake):
    directory, exe = fake(mode="success")
    result, events = await run(tmp_path, workspace, exe, read_only=True, permission_mode="acceptEdits")
    args = invocation(directory)["args"]
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"  # narrowed, not widened
    assert result.policy.read_only and "read-only role" in result.policy.authority(workspace)


@pytest.mark.parametrize("config", [
    {"tools": ("Read", "Bash")},
    {"tools": ("default",)},
    {"allowed_tools": ("Edit",)},
    {"allowed_tools": ("Bash(git status)",)},
    {"mcp_config": '{"mcpServers":{"x":{"command":"x"}}}'},
    {"setting_sources": "user,project"},
])
def test_read_only_role_refuses_wider_configuration(config):
    with pytest.raises(ConfigError, match="read-only"):
        cw.resolve_policy(cw.WorkerConfig(**config), read_only=True)


def test_read_only_detection_from_nexus_tools():
    assert cw.nexus_child_is_read_only(("read", "grep", "glob", "question"))
    assert cw.nexus_child_is_read_only(("write",), flagged=True)
    assert not cw.nexus_child_is_read_only(("read", "edit"))
    assert not cw.nexus_child_is_read_only(("read", "bash"))
    assert not cw.nexus_child_is_read_only(("read", "some_extension_tool"))


def test_config_rejects_bypass_but_accepts_auto_mode():
    with pytest.raises(ValueError, match="never accepted"):
        ProviderSection(kind="claude-code", worker_permission_mode="bypassPermissions")
    with pytest.raises(ConfigError):
        cw.WorkerConfig(permission_mode="bypassPermissions")
    section = ProviderSection(kind="claude-code", worker_permission_mode="auto")
    config = cw.WorkerConfig.from_section(section)
    assert cw.resolve_policy(config, read_only=False).permission_mode == "auto"
    # A read-only role is narrowed to dontAsk, never widened by auto.
    assert cw.resolve_policy(config, read_only=True).permission_mode == "dontAsk"
    section = ProviderSection(kind="claude-code", worker_permission_mode="default", worker_tools=["Read"])
    config = cw.WorkerConfig.from_section(section)
    assert cw.resolve_policy(config, read_only=False).permission_mode == "manual"
    with pytest.raises(ConfigError):
        cw.WorkerConfig(tools=())


async def test_trust_prompt_needs_attention_and_writes_no_input(tmp_path, workspace, fake):
    directory, exe = fake(mode="trust")
    result, events = await run(tmp_path, workspace, exe)
    assert result.status == "needs_attention" and result.is_error
    assert "claude" in result.text and str(workspace) in result.text.replace("'", "")
    assert "trust" in result.attention
    assert stdin_log(directory) == b""
    assert not alive(invocation(directory)["pid"])
    assert next(d for k, d in events if k == "external.finished")["status"] == "needs_attention"


async def test_permission_prompt_needs_attention_unless_dont_ask(tmp_path, workspace, fake):
    directory, exe = fake(mode="permission")
    result, _ = await run(tmp_path, workspace, exe, permission_mode="manual")
    assert result.status == "needs_attention" and "permission prompt" in result.attention
    assert stdin_log(directory) == b""


def test_prompt_detection_ignores_plain_prose():
    menu = b"Do you want to proceed?\r\n \xe2\x9d\xaf 1. Yes\r\n 2. No"
    assert cw.permission_prompt(menu)
    assert not cw.permission_prompt(b"The assistant asks: Do you want to proceed? Say yes.")
    # Claude's own reply (read from the transcript) is not mistaken for a menu.
    reply = "Options:\n1. Yes, refactor it\n2. No\nDo you want to proceed?"
    shown = reply.replace("\n", "\r\n").encode()
    assert cw.permission_prompt(shown)
    assert not cw.permission_prompt(shown, replies=[reply])


def test_exact_model_ids_accept_any_family_but_not_aliases():
    for model in ("claude-haiku-4-5", "claude-fable-5-1", "claude-opus-5.5"):
        assert cw._MODEL_RE.fullmatch(model)
    for alias in ("sonnet", "claude-sonnet-latest", "opus[1m]", "claude-code"):
        assert not cw._MODEL_RE.fullmatch(alias)


async def test_exit_without_stop_is_failed_and_redacted(tmp_path, workspace, fake):
    _, exe = fake(mode="exit")
    result, events = await run(tmp_path, workspace, exe)
    assert result.status == "failed" and result.is_error and result.stop_event is None
    assert "without a Stop result" in result.error
    assert "sk-ant" not in result.text and "sk-ant" not in result.diagnostics
    assert "\x1b" not in result.text and "boom" in result.diagnostics
    assert next(d for k, d in events if k == "external.finished")["status"] == "failed"


def test_no_default_deadline_and_legacy_timeout_is_ignored():
    assert not hasattr(cw, "DEFAULT_TIMEOUT_SECONDS") and not hasattr(cw, "MAX_TIMEOUT_SECONDS")
    config = cw.WorkerConfig()
    assert config.timeout_seconds is None
    assert not hasattr(cw.resolve_policy(config, read_only=False), "timeout_seconds")
    legacy = cw.WorkerConfig.from_section(SimpleNamespace(timeout_seconds=30))
    assert not hasattr(cw.resolve_policy(legacy, read_only=False), "timeout_seconds")
    assert "max_seconds" not in inspect.signature(cw.ClaudeCodeWorker.run).parameters


async def test_hung_worker_outlives_legacy_timeout_until_cancelled(tmp_path, workspace, fake):
    directory, exe = fake(mode="hang")
    cancel = SimpleNamespace(cancelled=False)
    task = asyncio.create_task(run(tmp_path, workspace, exe, cancel=cancel, timeout_seconds=1))
    await asyncio.sleep(2.5)  # past the legacy 1s timeout: still running, no deadline
    assert not task.done()
    cancel.cancelled = True
    result, _ = await asyncio.wait_for(task, 10)
    assert result.status == "cancelled" and result.is_error
    assert "partial answer" in result.text and "working" in result.text
    assert not alive(invocation(directory)["pid"])


async def test_hook_loss_is_never_success(tmp_path, workspace, fake):
    directory, exe = fake(mode="hookloss")
    cancel = SimpleNamespace(cancelled=False)
    task = asyncio.create_task(run(tmp_path, workspace, exe, cancel=cancel))
    # Wait until the fake has written its partial answer, then give the worker's
    # tailer a moment to ingest it before cancelling. A fixed sleep alone is not
    # deterministic here: under parallel CI load the 1.5s window sometimes closed
    # before the reader saw the line, leaving only "cancelled" as the result text.
    for _ in range(500):
        if task.done():
            break
        if any("answer but no hook" in p.read_text(errors="ignore")
               for p in directory.glob("transcripts/*.jsonl")):
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)  # let the streaming reader ingest the line
    assert not task.done()  # a missing Stop hook is never success
    cancel.cancelled = True
    result, events = await asyncio.wait_for(task, 10)
    assert result.status == "cancelled" and result.stop_event is None
    assert "answer but no hook" in result.text  # the partial answer is still surfaced
    assert next(d for k, d in events if k == "external.finished")["stop_event"] is None


async def test_polled_cancellation_kills_the_process_group(tmp_path, workspace, fake):
    directory, exe = fake(mode="hang")
    cancel = SimpleNamespace(cancelled=False)

    async def later():
        await asyncio.sleep(1.0)
        cancel.cancelled = True

    task = asyncio.create_task(later())
    result, _ = await run(tmp_path, workspace, exe, cancel=cancel)
    await task
    assert result.status == "cancelled" and result.is_error
    assert not alive(invocation(directory)["pid"])


async def test_asyncio_cancellation_kills_the_process_group_and_reraises(tmp_path, workspace, fake):
    directory, exe = fake(mode="hang")
    worker, policy = worker_for(tmp_path, exe)
    events = []

    async def emit(kind, data):
        events.append(kind)

    task = asyncio.create_task(worker.run(prompt="x", model=MODEL, workspace=workspace, policy=policy, emit=emit))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if (directory / "pid").exists():
            break
    pid = invocation(directory)["pid"]
    assert alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert not alive(pid)
    assert events[-1] == "external.finished"


async def test_stale_entries_and_partial_trailing_line(tmp_path, workspace, fake):
    _, exe = fake(mode="stale")
    result, events = await run(tmp_path, workspace, exe)
    assert result.status == "completed"
    texts = [d["text"] for k, d in events if k == "text"]
    assert texts == ["mine", "late line"]  # foreign session ignored; the split line is read once complete
    assert "FOREIGN" not in json.dumps(events)


async def test_multiple_assistant_messages_and_tool_error(tmp_path, workspace, fake):
    _, exe = fake(mode="multi")
    result, events = await run(tmp_path, workspace, exe)
    assert [d["text"] for k, d in events if k == "text"] == ["first", "second", "third"]
    order = [(k, d.get("phase")) for k, d in events if k in ("text", "external.tool")]
    assert order[0] == ("text", None) and order[1] == ("external.tool", "requested")
    completed = [d for k, d in events if k == "external.tool" and d["phase"] == "completed"]
    assert [d["is_error"] for d in completed] == [False, True] and completed[1]["tool"] == "Bash"
    assert result.tool_calls == 2 and result.text.startswith("third")


async def test_oversized_output_is_clipped_and_announced(tmp_path, workspace, fake):
    _, exe = fake(mode="big")
    result, events = await run(tmp_path, workspace, exe)
    assert result.status == "completed"
    assert "clipped: showing the first 64000 of 70000" in result.text
    assert len(result.text) < 66_000 and result.clip_notes
    done = next(d for k, d in events if k == "external.tool" and d["phase"] == "completed")
    assert len(done["content"][0]["text"]) == 8000 and "20000" in done["clip_note"]
    requested = next(d for k, d in events if k == "external.tool" and d["phase"] == "requested")
    assert len(requested["input"]["extra"]) == 4000 and "9000" in requested["clip_note"]


async def test_prompt_echo_with_startup_patterns_is_not_attention(tmp_path, workspace, fake):
    _, exe = fake(mode="echo")
    prompt = ("Explain what 'please run claude auth login' and 'not logged in' mean, and "
              "'Do you want to proceed? 1. Yes' prompts, in our docs.")
    for mode in ("manual", "dontAsk"):
        result, _ = await run(tmp_path, workspace, exe, prompt=prompt, permission_mode=mode)
        assert result.status == "completed" and result.text == "echo ok" and result.attention is None


def test_startup_and_prompt_detection_ignore_the_assignment_echo():
    prompt = "check why we are not logged in"
    echo = b"\xe2\x94\x82 check why we are\r\n\xe2\x94\x82 not logged in\r\n"
    assert cw.startup_block(echo) is not None
    assert cw.startup_block(echo, prompt=prompt) is None
    assert cw.startup_block(echo + b"Not logged in", prompt=prompt) is not None


async def test_unexpected_supervision_error_finishes_failed_redacted_and_cleans_up(
        tmp_path, workspace, fake, monkeypatch):
    directory, exe = fake(mode="hang")
    worker, policy = worker_for(tmp_path, exe)
    events: list[tuple[str, dict]] = []

    async def emit(kind, data):
        events.append((kind, data))

    async def broken(self, run, *, final=False):
        if (directory / "pid").exists():
            raise RuntimeError("boom sk-ant-abcdefghijklmnopqrstu")

    monkeypatch.setattr(cw.ClaudeCodeWorker, "_drain", broken)
    result = await worker.run(prompt="x", model=MODEL, workspace=workspace, policy=policy, emit=emit)
    assert result.status == "failed" and result.is_error
    assert "RuntimeError" in result.error and "sk-ant" not in result.error and "sk-ant" not in result.text
    finished = [d for k, d in events if k == "external.finished"]
    assert len(finished) == 1 and finished[0]["status"] == "failed"
    assert "sk-ant" not in json.dumps(events) and "boom" in finished[0]["error"]
    assert not alive(invocation(directory)["pid"]) and not worker._active


async def test_nested_tool_input_and_result_secrets_are_redacted(tmp_path, workspace):
    secret = "sk-ant-abcdefghijklmnopqrstu"
    session = "s1"
    entries = [
        {"type": "assistant", "sessionId": session, "uuid": "u1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {
                "command": f"curl -H 'x-api-key: {secret}'",
                "nested": {"list": [f"token {secret}", 7, True, None, {"deep": secret}]}}}]}},
        {"type": "user", "sessionId": session, "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": f"key={secret}"}]}]}},
    ]
    path = tmp_path / f"{session}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    events: list[tuple[str, dict]] = []

    async def emit(kind, data):
        events.append((kind, data))

    reader = cw.TranscriptReader(session)
    reader.set_path(path)
    cfg = cw.WorkerConfig()
    run_state = cw._Run(session=session, model=MODEL, workspace=workspace,
                        policy=cw.resolve_policy(cfg, read_only=False), emit=emit, reader=reader)
    await cw.ClaudeCodeWorker(cfg)._drain(run_state, final=True)
    assert secret not in json.dumps(events)
    requested, completed = (d for k, d in events if k == "external.tool")
    assert requested["input"]["nested"]["list"][1:4] == [7, True, None]
    assert isinstance(requested["input"]["nested"]["list"][4], dict)
    assert completed["content"][0]["type"] == "text" and "key=" in completed["content"][0]["text"]


async def test_probe_is_time_bounded_and_kills_the_process(tmp_path, monkeypatch):
    script = tmp_path / "slow"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(cw, "PROBE_TIMEOUT_SECONDS", 0.3)
    worker = cw.ClaudeCodeWorker(cw.WorkerConfig())
    with pytest.raises(ProviderError, match="timed out"):
        await worker._probe(str(script), "--version", {"PATH": os.environ["PATH"]}, str(tmp_path))
    assert not worker._active


async def test_stop_does_not_signal_a_reaped_process(monkeypatch):
    calls = []
    monkeypatch.setattr(cw.os, "killpg", lambda *a: calls.append(a))

    class Reaped:
        pid, returncode = 1, 0

        async def wait(self):
            return 0

    await cw.ClaudeCodeWorker(cw.WorkerConfig())._stop(Reaped())
    assert calls == []


async def test_stop_failure_is_failed_with_redacted_detail(tmp_path, workspace, fake):
    _, exe = fake(mode="failure")
    result, _ = await run(tmp_path, workspace, exe)
    assert result.status == "failed" and result.stop_event == "StopFailure"
    assert "rate_limit" in result.text and "sk-ant-abcdefghijklmnop1234" not in result.text
    assert "partial before failure" in result.text


async def test_hook_text_is_recorded_when_transcript_lacks_it(tmp_path, workspace, fake, monkeypatch):
    monkeypatch.setattr(cw, "RECONCILE_SECONDS", 0.3)
    _, exe = fake(mode="stoponly")
    result, events = await run(tmp_path, workspace, exe)
    assert result.status == "completed" and result.text == "from hook only"
    assert [(d["text"], d["source"]) for k, d in events if k == "text"] == [("from hook only", "claude-code-hook")]


async def test_derived_transcript_path_is_used_without_session_start(tmp_path, workspace, fake):
    _, exe = fake(mode="success", derived=True, config_dir=str(tmp_path / "claude-home"))
    result, events = await run(tmp_path, workspace, exe)
    assert result.status == "completed" and result.tool_calls == 1
    assert [d["text"] for k, d in events if k == "text"] == ["Looking around.", "All done."]


async def test_model_alias_and_oversized_prompt_are_refused(tmp_path, workspace, fake):
    _, exe = fake(mode="success")
    worker, policy = worker_for(tmp_path, exe)
    for model in ("sonnet", "opus", "claude-sonnet", ""):
        with pytest.raises(ConfigError, match="exact Claude model ID"):
            await worker.run(prompt="x", model=model, workspace=workspace, policy=policy)
    with pytest.raises(ConfigError, match="64 KiB"):
        await worker.run(prompt="x" * (64 * 1024 + 1), model=MODEL, workspace=workspace, policy=policy)
    with pytest.raises(ConfigError, match="not a directory"):
        await worker.run(prompt="x", model=MODEL, workspace=workspace / "missing", policy=policy)


async def test_old_cli_is_refused(tmp_path, workspace):
    path = tmp_path / "old-claude"
    path.write_text("#!/bin/sh\necho '2.0.1 (Claude Code)'\n")
    path.chmod(0o700)
    worker, policy = worker_for(tmp_path, str(path))
    with pytest.raises(ProviderError, match=">= 2.1.296"):
        await worker.run(prompt="x", model=MODEL, workspace=workspace, policy=policy)


def test_transcript_reader_is_incremental_and_bounded(tmp_path):
    path = tmp_path / "s.jsonl"
    reader = cw.TranscriptReader("s")
    assert reader.poll() == []  # no path yet
    reader.set_path(path)
    assert reader.poll() == []  # missing file
    line = json.dumps({"type": "assistant", "sessionId": "s", "uuid": "a",
                       "message": {"content": [{"type": "text", "text": "hi"}]}})
    path.write_text(line + "\n" + line[:20])
    assert [i["text"] for i in reader.poll()] == ["hi"]
    with path.open("a") as handle:
        handle.write(line[20:] + "\n")
    assert reader.poll() == []  # same uuid: de-duplicated, never replayed
    assert reader.poll(final=True) == []
    # A trailing line without newline is only read on the final reconcile.
    other = line.replace('"a"', '"b"')
    with path.open("a") as handle:
        handle.write(other)
    assert reader.poll() == []
    assert [i["text"] for i in reader.poll(final=True)] == ["hi"]
    # Switching to the hook-provided path re-reads without duplicating events.
    other_path = tmp_path / "t.jsonl"
    other_path.write_text(path.read_text())
    reader.set_path(other_path)
    assert reader.poll(final=True) == []


def test_transcript_reader_skips_overlong_lines_and_caps_total(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "MAX_TRANSCRIPT_LINE", 100)
    monkeypatch.setattr(cw, "MAX_TRANSCRIPT_CHUNK", 64)
    path = tmp_path / "s.jsonl"
    good = json.dumps({"type": "assistant", "sessionId": "s", "uuid": "g",
                       "message": {"content": [{"type": "text", "text": "ok"}]}})
    path.write_text("x" * 300 + "\n" + good + "\n")
    reader = cw.TranscriptReader("s")
    reader.set_path(path)
    assert [i["text"] for i in reader.poll()] == ["ok"] and reader.skipped_lines == 1
    monkeypatch.setattr(cw, "MAX_TRANSCRIPT_TOTAL", 10)
    capped = cw.TranscriptReader("s")
    capped.set_path(path)
    capped.poll()
    assert capped.truncated


def test_hook_transcript_path_must_match_the_session(tmp_path):
    session = "11111111-2222-3333-4444-555555555555"
    ok = {"transcript_path": f"/x/{session}.jsonl"}
    assert str(cw.ClaudeCodeWorker._hook_transcript(ok, session)) == f"/x/{session}.jsonl"
    for bad in ({"transcript_path": "/x/other.jsonl"}, {"transcript_path": f"x/{session}.jsonl"},
                {"transcript_path": 5}, {}):
        assert cw.ClaudeCodeWorker._hook_transcript(bad, session) is None


def test_derived_path_mirrors_claude_project_naming(tmp_path):
    work = tmp_path / "my proj.v1"
    work.mkdir()
    derived = cw.derive_transcript_path(tmp_path / "home", work, "sid")
    assert derived.name == "sid.jsonl" and derived.parent.parent == tmp_path / "home" / "projects"
    assert derived.parent.name.replace("-", "").isalnum() and " " not in derived.parent.name


def test_terminal_text_strips_ansi_osc_and_controls():
    raw = b"\x1b]0;title\x07\x1b[31mred\x1b[0m\r\nline\x08\x00 end\x1b[?25l"
    assert cw.terminal_text(raw) == "red\nline end"


def test_changed_paths_detects_edits_to_already_dirty_files(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run_git("init", "-q")
    (repo / "a.txt").write_text("1")
    run_git("add", "a.txt")
    run_git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    (repo / "dirty.txt").write_text("a")
    before = cw.snapshot_workspace(repo)
    assert before["kind"] == "git"
    time.sleep(0.01)
    (repo / "dirty.txt").write_text("bb")  # already untracked before: size differs
    (repo / "a.txt").write_text("22")
    (repo / "new.txt").write_text("n")
    assert cw.changed_paths(before, cw.snapshot_workspace(repo)) == ["a.txt", "dirty.txt", "new.txt"]
    assert cw.changed_paths(before, before) == []


def test_changed_paths_include_files_committed_during_the_run(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("1")
    git("add", "a.txt")
    git("commit", "-qm", "one")
    before = cw.snapshot_workspace(repo)
    # Edited and committed: clean in both `git status` snapshots, found by the HEAD diff.
    (repo / "a.txt").write_text("2")
    git("commit", "-qam", "two")
    after = cw.snapshot_workspace(repo, before)
    assert after["committed"] == ["a.txt"]
    assert cw.changed_paths(before, after) == ["a.txt"]
    assert cw.changed_paths(before, cw.snapshot_workspace(repo)) == []  # no `since`, no commit diff


def test_changed_paths_outside_git_uses_mtimes(tmp_path):
    (tmp_path / "keep.txt").write_text("k")
    before = cw.snapshot_workspace(tmp_path)
    assert before["kind"] == "mtime"
    (tmp_path / "added.txt").write_text("a")
    assert cw.changed_paths(before, cw.snapshot_workspace(tmp_path)) == ["added.txt"]


async def test_per_child_cancel_token_kills_the_external_process_group(tmp_path, workspace, fake):
    """The runner's child-scoped token (host ``AgentCancel``) stops a Claude Code worker."""
    from nexus.agents import runner

    directory, exe = fake(mode="hang")
    token = runner._ChildCancel(SimpleNamespace(cancelled=False))
    runner._LIVE_CHILDREN["root/sub/1"] = token

    async def later():
        await asyncio.sleep(1.0)
        assert runner.cancel_child("root/sub/1", "stop this one")

    task = asyncio.create_task(later())
    try:
        result, _ = await run(tmp_path, workspace, exe, cancel=token)
    finally:
        runner._LIVE_CHILDREN.pop("root/sub/1", None)
    await task
    assert result.status == "cancelled" and result.is_error
    assert token.reason == "stop this one"
    assert not alive(invocation(directory)["pid"])


def test_terminal_text_renders_cursor_forward_as_spaces():
    from nexus.agents.claude_code_worker import terminal_text

    assert terminal_text(b"Accessing\x1b[1Cworkspace:\x1b[3Cok") == "Accessing workspace:   ok"


async def test_auto_mode_classifier_denial_is_reported_as_attention(tmp_path, workspace, fake):
    _, exe = fake(mode="classifier")
    result, events = await run(tmp_path, workspace, exe, permission_mode="auto")
    assert result.status == "completed"  # the child finished; the block is for the root to see
    assert result.attention == "Claude's auto-mode classifier denied: Bash: Git Destructive. Nexus approved nothing"
    finished = next(d for k, d in events if k == "external.finished")
    assert finished["attention"] == result.attention
    argv = invocation(tmp_path / "bin")["args"]
    assert argv[argv.index("--permission-mode") + 1] == "auto"


async def test_no_attention_without_a_denial(tmp_path, workspace, fake):
    _, exe = fake(mode="success")
    result, _ = await run(tmp_path, workspace, exe)
    assert result.attention is None


# -- resume (subagent_continue) -------------------------------------------------


UUID_NEW = "11111111-2222-4333-8444-555555555555"


async def run_resume(tmp_path, workspace, exe, session, *, prompt="and the tests?", **config):
    worker, policy = worker_for(tmp_path, exe, **config)
    events: list[tuple[str, dict]] = []

    async def emit(kind, data):
        events.append((kind, data))

    result = await worker.run(prompt=prompt, model=MODEL, workspace=workspace, policy=policy,
                              emit=emit, resume=session)
    return result, events


async def test_resume_argv_resumes_the_session_under_the_same_policy(tmp_path, workspace, fake):
    directory, exe = fake(mode="success", derived=True, config_dir=str(tmp_path / "claude-home"))
    first, _ = await run(tmp_path, workspace, exe)
    first_args = invocation(directory)["args"]
    cfgfile = directory / "fake.json"
    cfgfile.write_text(json.dumps({"mode": "multi", "derived": True, "config_dir": str(tmp_path / "claude-home")}))
    second, events = await run_resume(tmp_path, workspace, exe, first.claude_session)
    args = invocation(directory)["args"]
    assert args[args.index("--resume") + 1] == first.claude_session
    assert "--session-id" not in args and "--fork-session" not in args
    assert args[-2:] == ["--", "and the tests?"]
    # Every policy flag is the one the first run used.
    def policy_flags(argv):
        return [a for i, a in enumerate(argv) if a not in ("--session-id", "--resume", "--settings")
                and argv[i - 1] not in ("--session-id", "--resume", "--settings") and a != "--"][:-1]
    assert policy_flags(args) == policy_flags(first_args)
    # Same session, and the first run's transcript is not replayed as this run's activity.
    assert second.status == "completed" and second.claude_session == first.claude_session
    texts = [d["text"] for k, d in events if k == "text"]
    assert texts == ["first", "second", "third"]
    assert all(d["session"] == first.claude_session for k, d in events if k == "external.started")


async def test_resume_follows_a_replacement_session_id(tmp_path, workspace, fake):
    # SessionStart pins the id Claude actually reports; the Stop hook must carry the same one.
    directory, exe = fake(mode="success")
    first, _ = await run(tmp_path, workspace, exe)
    (directory / "fake.json").write_text(json.dumps({"mode": "multi", "new_session_on_resume": UUID_NEW}))
    second, _ = await run_resume(tmp_path, workspace, exe, first.claude_session)
    assert second.status == "completed" and second.claude_session == UUID_NEW
    assert second.transcript_path.endswith(f"{UUID_NEW}.jsonl")


async def test_resume_needs_a_session_uuid_and_does_not_widen_the_preflight(tmp_path, workspace, fake):
    _, exe = fake(mode="success")
    for bad in ("s1", "../x", "--fork-session", ""):
        with pytest.raises(ConfigError, match="UUID"):
            await run_resume(tmp_path, workspace, exe, bad)


# -- effort ---------------------------------------------------------------------


async def test_effort_is_passed_as_the_cli_flag_only_when_given(tmp_path, workspace, fake):
    directory, exe = fake(mode="success")
    worker, policy = worker_for(tmp_path, exe)
    await worker.run(prompt="do it", model=MODEL, workspace=workspace, policy=policy, effort="high")
    args = invocation(directory)["args"]
    assert args[args.index("--effort") + 1] == "high"
    await run(tmp_path, workspace, exe)
    assert "--effort" not in invocation(directory)["args"]


async def test_unsupported_effort_is_refused_before_starting(tmp_path, workspace, fake):
    _, exe = fake(mode="success")
    worker, policy = worker_for(tmp_path, exe)
    with pytest.raises(cw.ConfigError, match="effort"):
        await worker.run(prompt="x", model=MODEL, workspace=workspace, policy=policy, effort="none")
