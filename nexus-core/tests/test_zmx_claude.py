"""Claude Code ZMX worker against a fake `claude` PTY executable; no real CLI, account or model is used."""
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.workers import claude_code
from nexus.workers.base import WorkerError
from nexus.workers.claude_code import ClaudeCodeWorker, ClaudePolicy, attention_for, private_directory, transcript_path
from nexus.workers.claude_transcript import TranscriptReader

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Claude ZMX requires a POSIX PTY")
MODEL = "claude-haiku-4-5"

SCRIPT = r'''#!%(python)s
import json, os, re, subprocess, sys, time, tty
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
if args == ["--version"]: print("2.1.318 (Claude Code)"); sys.exit()
if args == ["--help"]: print("--model --session-id --resume --permission-mode --settings --disallowedTools --effort"); sys.exit()
cfg = json.load(open(os.path.join(here, "fake.json")))
mode = cfg["mode"]
open(os.path.join(here, "argv.jsonl"), "a").write(json.dumps({"args": args, "cwd": os.getcwd(), "tty": all(os.isatty(i) for i in (0, 1, 2)),
    "env": sorted(os.environ)}) + "\n")
tty.setraw(0)
resumed = "--resume" in args
session = args[(args.index("--resume") if resumed else args.index("--session-id")) + 1]
if resumed and cfg.get("replacement"): session = cfg["replacement"]
settings = json.load(open(args[args.index("--settings") + 1]))
tpath = os.path.join(cfg["config"], "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.getcwd())), session + ".jsonl")
os.makedirs(os.path.dirname(tpath), exist_ok=True)
n = [0]
def entry(kind, content, mid="msg_1", **extra):
    n[0] += 1
    return {"type": kind, "sessionId": extra.pop("sid", session), "uuid": "%%s-%%d" %% (session, n[0]), "isSidechain": False,
            "message": {"role": kind, "id": mid, "content": content, **extra.pop("message", {})}, **extra}
def write(*entries, partial=None):
    with open(tpath, "a") as f:
        for e in entries: f.write(json.dumps(e) + "\n")
        if partial: f.write(partial)
def hook(event, **extra):
    value = {"session_id": cfg.get("hook_session", session), "hook_event_name": event, **extra}
    command = settings["hooks"][event][0]["hooks"][0]["command"]
    return subprocess.run(command, shell=True, input=json.dumps(value).encode()).returncode
def start(): hook("SessionStart", transcript_path=tpath)
if cfg.get("dsr"):
    os.write(1, b"\x1b[6n"); time.sleep(.3)
if mode == "success":
    start()
    write(entry("assistant", [{"type": "text", "text": "Looking."}, {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}},
                              {"type": "tool_use", "id": "t2", "name": "Grep", "input": {"pattern": "x"}}], message={"usage": {"input_tokens": 11, "output_tokens": 2}}))
    write(entry("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "none"}], mid=None))
    write(entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "print(1)"}]}], mid=None))
    write(entry("assistant", [{"type": "text", "text": "All done for " + args[-1][-5:]}], mid="msg_2"))
    hook("Stop", transcript_path=tpath, last_assistant_message="All done")
    if cfg.get("duplicate_stop"): hook("Stop", transcript_path=tpath, last_assistant_message="All done again")
    time.sleep(cfg.get("sleep", 30))
elif mode == "subagent":
    start()
    write(entry("assistant", [{"type": "tool_use", "id": "ta", "name": "Agent", "input": {"description": "look", "prompt": "read a"}}]))
    side = os.path.join(tpath[:-6], "subagents"); os.makedirs(side, exist_ok=True)
    sub = lambda kind, content, **x: {"type": kind, "isSidechain": True, "agentId": "ab12", "sessionId": session, "uuid": "s-%%s" %% content.__repr__()[:20], "message": {"role": kind, "content": content}}
    with open(os.path.join(side, "agent-ab12.jsonl"), "w") as f:
        for e in (sub("user", "read a please"), sub("assistant", [{"type": "tool_use", "id": "tb", "name": "Read", "input": {"file_path": "a"}}]),
                  sub("user", [{"type": "tool_result", "tool_use_id": "tb", "content": "print(1)"}]), sub("assistant", [{"type": "text", "text": "a prints 1"}]),
                  {"type": "attachment", "isSidechain": True, "agentId": "ab12", "sessionId": session, "uuid": "att", "attachment": {"type": "date"}}):
            f.write(json.dumps(e) + "\n")
    json.dump({"agentType": "general-purpose", "description": "look", "toolUseId": "ta", "finished": True}, open(os.path.join(side, "agent-ab12.meta.json"), "w"))
    write(entry("user", [{"type": "tool_result", "tool_use_id": "ta", "content": "a prints 1"}], mid=None))
    write(entry("assistant", [{"type": "text", "text": "done"}], mid="msg_2"))
    hook("Stop", transcript_path=tpath, last_assistant_message="done")
    time.sleep(30)
elif mode == "late":
    start(); write(entry("assistant", [{"type": "text", "text": "first"}]), partial='{"type": "assistant", "sessionId": "' + session + '", "mess')
    hook("Stop", transcript_path=tpath, last_assistant_message="late")
    time.sleep(.5)
    open(tpath, "a").write('age": {"role": "assistant", "id": "msg_2", "content": [{"type": "text", "text": "late"}]}, "uuid": "late"}\n')
    time.sleep(30)
elif mode == "failure":
    start(); hook("StopFailure", transcript_path=tpath, error="rate_limit sk-ant-abcdefghijklmnop1234"); time.sleep(30)
elif mode == "nohook":
    start(); write(entry("assistant", [{"type": "text", "text": "answer but no hook"}])); time.sleep(cfg.get("sleep", 30))
elif mode == "trust":
    os.write(1, b"Yes, I trust this folder\r\n"); time.sleep(30)
elif mode == "permission":
    start(); os.write(1, b"Do you want to proceed?\r\n \xe2\x9d\xaf 1. Yes\r\n   2. No\r\n"); time.sleep(30)
elif mode == "echo":
    prompt = args[-1]
    os.write(1, ("\r\n".join("| " + prompt[i:i+30] for i in range(0, len(prompt), 30)) + "\r\n").encode()); time.sleep(.4)
    start(); write(entry("assistant", [{"type": "text", "text": "echo ok"}])); hook("Stop", transcript_path=tpath, last_assistant_message="echo ok"); time.sleep(30)
elif mode == "badhook":
    start(); write(entry("assistant", [{"type": "text", "text": "x"}])); hook("Stop", transcript_path="/etc/passwd"); time.sleep(30)
elif mode == "exit":
    sys.exit(3)
'''


@pytest.fixture
def fake(tmp_path):
    cli = tmp_path / "fake-claude"
    cli.write_text(SCRIPT % {"python": sys.executable})
    cli.chmod(0o700)
    config = tmp_path / "claude-config"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def configure(**values):
        (tmp_path / "fake.json").write_text(json.dumps({"mode": "success", "config": str(config), **values}))
        return ClaudeCodeWorker(str(cli), state_dir=state, config_dir=config,
                                environ={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": "secret",
                                         "OPENAI_API_KEY": "secret", "PYTHONPATH": "bad"})
    workspace = tmp_path / "work"
    workspace.mkdir()
    return configure, workspace, tmp_path, config


async def run(worker, workspace, **kwargs):
    events = []

    async def emit(event):
        events.append(event)
    result = await asyncio.wait_for(worker.run(prompt="assignment", model=MODEL, workspace=workspace, emit=emit, **kwargs), 20)
    return result, events


def fix_fast(monkeypatch):
    monkeypatch.setattr(claude_code, "RECONCILE_SECONDS", .5)


async def test_success_groups_parallel_results_and_never_sends_approval(fake, monkeypatch):
    fix_fast(monkeypatch)
    configure, workspace, tmp, _ = fake
    result, events = await run(configure(dsr=True), workspace)
    assert result.status == "completed" and result.text == "All done for nment" and result.usage == {"input_tokens": 11, "output_tokens": 2}
    kinds = [e.kind for e in events]
    assert kinds.count("assistant") == 2 and kinds.count("tool.result") == 2
    assert kinds.index("wire") < kinds.index("assistant")
    invocation = json.loads((tmp / "argv.jsonl").read_text().splitlines()[0])
    assert invocation["tty"] and "ANTHROPIC_API_KEY" not in invocation["env"] and "PYTHONPATH" not in invocation["env"]
    args = invocation["args"]
    assert args[-2:] == ["--", "assignment"] and "-p" not in args and "--print" not in args
    assert args[args.index("--permission-mode") + 1] == "auto" and "AskUserQuestion" in args
    assert "bypassPermissions" not in args and "--dangerously-skip-permissions" not in args
    assert Path(result.transcript_path).is_relative_to(tmp / "claude-config")


async def test_late_transcript_flush_is_reconciled_after_stop(fake, monkeypatch):
    fix_fast(monkeypatch)
    configure, workspace, *_ = fake
    monkeypatch.setattr(claude_code, "RECONCILE_SECONDS", 1.5)
    result, events = await run(configure(mode="late"), workspace)
    assert result.status == "completed" and result.text == "late"
    assert any(e.kind == "assistant" and e.data["parts"][0]["text"] == "late" for e in events)


@pytest.mark.parametrize("mode,status,match", [
    ("failure", "failed", "StopFailure"), ("nohook", "failed", "without a Stop hook"), ("exit", "failed", "without a Stop hook"),
    ("trust", "needs_attention", "trust"), ("permission", "needs_attention", "approval"), ("badhook", "failed", "outside"),
])
async def test_completion_is_never_inferred(fake, monkeypatch, mode, status, match):
    fix_fast(monkeypatch)
    configure, workspace, *_ = fake
    result, _ = await run(configure(mode=mode, sleep=1), workspace)
    assert result.status == status and match in result.error
    assert "sk-ant-abcdefghijklmnop1234" not in result.error + result.diagnostics


async def test_prompt_echo_does_not_cause_false_attention(fake, monkeypatch):
    fix_fast(monkeypatch)
    configure, workspace, *_ = fake
    result, _ = await asyncio.wait_for(worker_run_long(configure(mode="echo"), workspace), 20)
    assert result.status == "completed" and result.text == "echo ok"


async def worker_run_long(worker, workspace):
    prompt = "Explain the sentence: Do you want to proceed? 1. Yes 2. No. Also Yes, I trust this folder."
    events = []
    return await worker.run(prompt=prompt, model=MODEL, workspace=workspace, emit=lambda e: _append(events, e)), events


async def _append(events, event):
    events.append(event)


async def test_resume_uses_recorded_session_baseline_and_replacement_id(fake, monkeypatch):
    fix_fast(monkeypatch)
    configure, workspace, tmp, _ = fake
    first, _ = await run(configure(), workspace)
    replacement = str(uuid4())
    worker = configure(replacement=replacement)
    second, events = await run(worker, workspace, resume=first.session_id, previous_fingerprint=first.policy_fingerprint,
                               previous_transcript=first.transcript_path)
    lines = [json.loads(x) for x in (tmp / "argv.jsonl").read_text().splitlines()]
    assert "--resume" in lines[1]["args"] and "--session-id" not in lines[1]["args"]
    assert second.session_id == replacement and second.status == "completed"
    assert all("msg_1" not in json.dumps(e.data) for e in events if e.kind == "assistant" and e.data.get("message_id") == "msg_1"
               and first.session_id in json.dumps(e.data))


async def test_resume_requires_exact_policy(fake):
    configure, workspace, *_ = fake
    worker = configure()
    with pytest.raises(ValueError, match="unchanged"):
        await worker.run(prompt="x", model=MODEL, workspace=workspace, resume=str(uuid4()), previous_fingerprint="other")
    with pytest.raises(ValueError):
        await worker.run(prompt="x", model=MODEL, workspace=workspace, resume="not-a-uuid", previous_fingerprint="other")


@pytest.mark.parametrize("mode", ["bypassPermissions", "plan", "acceptEdits"])
def test_policy_never_bypasses(mode):
    with pytest.raises(ValueError):
        ClaudePolicy(mode)


async def test_prompt_limits_and_cancellation(fake):
    configure, workspace, tmp, _ = fake
    worker = configure(mode="nohook")
    with pytest.raises(ValueError, match="64 KiB"):
        await worker.run(prompt="x" * 70000, model=MODEL, workspace=workspace)
    task = asyncio.create_task(worker.run(prompt="x", model=MODEL, workspace=workspace))
    for _ in range(300):
        if (tmp / "argv.jsonl").exists():
            break
        await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


def test_hook_script_validates_and_pins_session(tmp_path):
    import subprocess
    root = tmp_path / "run"
    root.mkdir()
    hook = tmp_path / "hook.py"
    hook.write_text(claude_code.HOOK)
    session, other = str(uuid4()), str(uuid4())

    def call(event, sid, resumed="no", expected=session):
        value = {"session_id": sid, "hook_event_name": event, "last_assistant_message": "x" * 100000}
        return subprocess.run([sys.executable, "-I", str(hook), str(root), expected, resumed], check=False, input=json.dumps(value).encode()).returncode
    assert call("Stop", "bad") == 1 and call("Stop", other) == 1 and call("Unknown", session) == 1
    assert call("Stop", session) == 0 and len(json.loads((root / "Stop.json").read_text())["last_assistant_message"]) == 65536
    assert not list(root.glob("*.part")) and oct((root / "Stop.json").stat().st_mode & 0o777) == "0o600"
    assert call("SessionStart", other, "yes", session) == 0 and call("Stop", session, "yes", session) == 1
    assert (root / "session").read_text() == other


def test_private_state_and_transcript_confinement(tmp_path):
    state = tmp_path / "ok"
    assert private_directory(state) == state
    state.chmod(0o755)
    with pytest.raises(WorkerError):
        private_directory(state)
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(WorkerError):
        private_directory(link)
    config = tmp_path / "config"
    (config / "p").mkdir(parents=True)
    assert transcript_path(str(config / "p" / "a.jsonl"), config)
    for bad in ("relative.jsonl", str(tmp_path / "outside.jsonl"), str(config / "p" / "a.txt"), str(config / ".." / "x.jsonl")):
        with pytest.raises(WorkerError):
            transcript_path(bad, config)


def test_transcript_reader_raw_first_dedup_sidechain_and_rewrites(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    session = "s1"
    good = {"type": "assistant", "sessionId": session, "uuid": "a", "message": {"id": "m", "content": [{"type": "text", "text": "hi"}]}}
    lines = [good, good, {**good, "uuid": "b", "isSidechain": True}, {**good, "uuid": "c", "sessionId": "other"},
             {"type": "weird", "sessionId": session, "uuid": "d"}]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n[]\n")
    events = TranscriptReader(path, session).poll()
    assert [e.kind for e in events].count("wire") == 7
    assert [e.kind for e in events].count("assistant") == 1 and sum(e.kind == "notice" for e in events) == 3
    reader = TranscriptReader(path, session)
    reader.poll()
    path.write_text("short\n")
    with pytest.raises(WorkerError, match="rewritten or truncated"):
        reader.poll()
    monkeypatch.setattr("nexus.workers.claude_transcript.MAX_LINE_BYTES", 64)
    path.write_text("x" * 200)
    with pytest.raises(WorkerError, match="line limit"):
        TranscriptReader(path, session).poll()
    monkeypatch.setattr("nexus.workers.claude_transcript.MAX_TOTAL_BYTES", 10)
    path.write_text("y\n" * 20)
    with pytest.raises(WorkerError, match="size limit"):
        TranscriptReader(path, session).poll()


@pytest.mark.parametrize("terminal,expected", [
    ("Yes, I trust this folder", "trust"), ("Please login", "Sign-in"), ("Do you want to proceed?\n1. Yes", "approval"),
    ("quoted: Do you want to proceed? 1. Yes", None), ("ordinary output", None)])
def test_attention_detection_removes_prompt_echo(terminal, expected):
    got = attention_for(terminal, "quoted: Do you want to proceed? 1. Yes", [])
    assert (expected is None and got is None) or (expected and expected in got)


async def test_claude_own_subagent_is_read_only_and_linked_to_the_calling_tool(fake, monkeypatch):
    fix_fast(monkeypatch)
    configure, workspace, *_ = fake
    result, events = await run(configure(mode="subagent"), workspace)
    assert result.status == "completed"
    side = [e for e in events if e.kind == "subagent"]
    assert side and side[-1].data["tool_use_id"] == "ta" and side[-1].data["finished"] is True
    kinds = [(x["kind"], x.get("name") or x.get("text") or x.get("output")) for x in side[-1].data["entries"]]
    assert kinds == [("prompt", "read a please"), ("tool", "Read"), ("result", "print(1)"), ("text", "a prints 1")]
    # The sidechain never leaks into the main transcript's events as assistant text.
    assert not any(e.kind == "assistant" and "a prints 1" in json.dumps(e.data["parts"]) for e in events)


async def test_external_agent_attaches_the_sidechain_to_the_agent_call_result(tmp_path, monkeypatch):
    from nexus.loop import ToolEnd
    from nexus.workers import agent as agent_module
    from nexus.workers.base import WorkerEvent, WorkerResult
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    from nexus.config import ConfigFile
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.claude_backend": "zmx"}, [])
    monkeypatch.setattr(agent_module, "cli_path", lambda pid: "/bin/true")

    class Stub:
        def __init__(self, *a, **k): ...

        async def run(self, *, emit, **k):
            await emit(WorkerEvent("assistant", {"message_id": "m1", "parts": [
                {"type": "tool_use", "id": "ta", "name": "Agent", "input": {"description": "d"}}], "usage": None, "raw": {}}))
            await emit(WorkerEvent("subagent", {"tool_use_id": "ta", "agent_id": "x", "agent_type": "general-purpose",
                                                "description": "d", "finished": True, "clipped": False,
                                                "entries": [{"kind": "text", "text": "inner"}]}))
            await emit(WorkerEvent("tool.result", {"id": "ta", "output": "inner", "is_error": False, "raw": {}}))
            await emit(WorkerEvent("assistant", {"message_id": "m2", "parts": [{"type": "text", "text": "ok"}], "usage": None, "raw": {}}))
            return WorkerResult("completed", "ok", "x", session_id="s")

    monkeypatch.setattr(agent_module, "ClaudeCodeWorker", Stub)
    agent = agent_module.ExternalAgent(ref="claude-code/claude-haiku-5-5", workspace=tmp_path, messages=[], instructions="",
                                       permission="allow", effort=None)
    ends = [e async for e in agent.turn("go") if isinstance(e, ToolEnd)]
    assert len(ends) == 1 and ends[0].result.raw == {"sidechain": {
        "agent_id": "x", "agent_type": "general-purpose", "description": "d", "finished": True, "clipped": False,
        "entries": [{"kind": "text", "text": "inner"}]}}
