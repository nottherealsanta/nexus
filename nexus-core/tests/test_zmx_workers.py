from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from nexus.workers import antigravity, base
from nexus.workers.antigravity import AntigravityWorker, StreamParser
from nexus.workers.base import WorkerError, WorkerPolicy, child_env, diagnostic_text

pytestmark = pytest.mark.skipif(os.name != "posix", reason="ZMX process groups require POSIX")
MODEL = "gemini-3.8-flash-high"


def record(kind: str, data: object, **extra) -> bytes:
    return json.dumps({"event": kind, kind: data, **extra}).encode()


def initialized(tmp_path: Path) -> StreamParser:
    parser = StreamParser(MODEL, tmp_path, WorkerPolicy())
    parser.feed(record("init", {"cwd": str(tmp_path), "model": MODEL, "permission_mode": "auto"}, conversation_id="c1"))
    assert parser.initialized
    return parser


def fake_agy(tmp_path: Path, body: str, *, version="1.4.12", help_text=None, help_stderr=False) -> AntigravityWorker:
    help_text = help_text if help_text is not None else (
        "--input-format --output-format stream-json --model --mode auto accept-edits plan --print-timeout")
    cli = tmp_path / "fake-agy"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time, signal, subprocess, select\n"
        f"if '--version' in sys.argv:\n print({version!r}); sys.exit(0)\n"
        f"if '--help' in sys.argv:\n print({help_text!r}, file=sys.{'stderr' if help_stderr else 'stdout'}); sys.exit(0)\n"
        "def send(kind, data, **extra):\n"
        " print(json.dumps({'event': kind, kind: data, **extra}), flush=True)\n"
        "def init():\n"
        " send('init', {'cwd': os.getcwd(), 'model': 'gemini-3.8-flash-high', 'permission_mode': 'auto'}, conversation_id='c1')\n"
        + body)
    cli.chmod(0o700)
    return AntigravityWorker(str(cli), environ={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "OPENAI_API_KEY": "secret"})


async def run(worker: AntigravityWorker, tmp_path: Path, **kwargs):
    return await asyncio.wait_for(worker.run(prompt="test", model=MODEL, workspace=tmp_path, **kwargs), 10)


def test_no_import_or_registry_coupling():
    root = Path(__file__).parents[1] / "nexus"
    allowed = {"runtime.py", "config.py", "server.py", "recovery.py", "doctor.py", "cli.py", "provider_usage.py", "llmhub/hub.py", "llmhub/catalog.py"}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if "workers" not in relative.parts and relative.as_posix() not in allowed:
            assert "nexus.workers" not in path.read_text(), path
            assert "from .workers" not in path.read_text(), path
            assert "from ..workers" not in path.read_text(), path
    code = "import sys; import nexus.workers; assert 'nexus.workers.antigravity' not in sys.modules; assert 'nexus.workers.claude_code' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_environment_is_allowlisted():
    assert child_env({"HOME": "/h", "PATH": "/p", "PYTHONPATH": "bad", "ANTHROPIC_API_KEY": "bad",
                      "OPENAI_API_KEY": "bad", "AGY_TOKEN": "bad"}) == {"HOME": "/h", "PATH": "/p"}


def test_diagnostics_do_not_redact_long_paths():
    path = "/tmp/" + "a" * 120 + "/file.py"
    raw = f"\x1b[31m{path}\x1b[0m\napi_key=secret\nauthorization: Bearer abc\x00".encode()
    text = diagnostic_text(raw)
    assert path in text
    assert "secret" not in text and " abc" not in text
    assert "\x1b" not in text and "\x00" not in text


@pytest.mark.parametrize("mode", ["always-proceed", "bypassPermissions", "dontAsk", ""])
def test_policy_cannot_bypass(mode):
    with pytest.raises(ValueError, match="bypass is not supported"):
        WorkerPolicy(mode)


def test_authority_is_explicit(tmp_path):
    authority = WorkerPolicy().authority(tmp_path, vendor="Antigravity")
    assert "ZMX" in authority and "own tools" in authority
    assert "do not apply" in authority and "context Nexus cannot see" in authority


def test_raw_before_interpretation_and_unknown_lines(tmp_path):
    parser = initialized(tmp_path)
    for line in (b"bad json", b"[]", b'{"event": "future", "future": {"x": 1}}'):
        events = parser.feed(line)
        assert events[0].kind == "wire" and events[0].raw == line
        assert events[1].kind == "notice"
    assert len(parser.notes) == 3


@pytest.mark.parametrize("kind,data", [("result", {"status": "SUCCESS", "response": "ok"}),
                                       ("step_update", {"step_index": 0})])
def test_work_before_init_fails(tmp_path, kind, data):
    parser = StreamParser(MODEL, tmp_path, WorkerPolicy())
    parser.feed(record(kind, data))
    assert "before init" in parser.error


@pytest.mark.parametrize("data", [{"permission_mode": "always-proceed"}, {"model": "other"}, {"cwd": "/wrong"}])
def test_init_refuses_wrong_authority_or_target(tmp_path, data):
    parser = StreamParser(MODEL, tmp_path, WorkerPolicy())
    parser.feed(record("init", data))
    assert parser.error and not parser.initialized


@pytest.mark.parametrize("kind", ["init", "result", "step_update"])
@pytest.mark.parametrize("data", [None, [], "wrong"])
def test_malformed_payload_fails(tmp_path, kind, data):
    parser = StreamParser(MODEL, tmp_path, WorkerPolicy())
    parser.feed(record(kind, data))
    assert parser.error == f"malformed {kind} payload"


@pytest.mark.parametrize("state", ["DONE", "ERROR", "CANCELED", "CANCELLED"])
def test_tool_lifecycle_closes_once_including_denial(tmp_path, state):
    parser = initialized(tmp_path)
    step = {"step_index": 2, "step_type": "tool", "state": "RUNNING", "tool_name": "Read",
            "tool_info": {"parameters": {"path": "a"}}}
    events = parser.feed(record("step_update", step))
    assert [e.kind for e in events] == ["wire", "tool.start"]
    assert events[1].data["name"] == "Read"
    step.update(state=state, tool_info={"output": "contents"})
    events = parser.feed(record("step_update", step))
    assert [e.kind for e in events] == ["wire", "tool.end"]
    assert events[1].data["input"] == {"path": "a"}
    assert not parser.tools
    assert [e.kind for e in parser.feed(record("step_update", step))] == ["wire"]


def test_partial_answer_and_usage_unknown(tmp_path):
    parser = initialized(tmp_path)
    parser.feed(record("step_update", {"step_index": 0, "step_type": "agent_response", "text_delta": "partial"}))
    assert parser.text == "partial" and parser.usage is None
    parser.feed(record("result", {"status": "SUCCESS", "response": "final", "conversation_id": "resumed-id",
                                  "usage": {"input_tokens": 5, "output_tokens": 0, "bad": True, "negative": -1}}))
    assert parser.text == "final" and parser.session_id == "resumed-id"
    assert parser.usage == {"input_tokens": 5, "output_tokens": 0}
    assert parser.finished and not parser.error


@pytest.mark.parametrize("answer,denied", [("", []), ("", [{"display_name": "Bash", "action": "command"}]),
                                         ("I could not run it", [{"display_name": "Bash"}])])
def test_soft_denials_are_attention_not_success(tmp_path, answer, denied):
    parser = initialized(tmp_path)
    parser.feed(record("result", {"status": "SUCCESS", "response": answer, "denied_actions": denied}))
    assert parser.attention and "Nexus approved nothing" in parser.attention


def test_success_does_not_replace_empty_final_with_partial_prose(tmp_path):
    parser = initialized(tmp_path)
    parser.text = "I will run the tool now"
    parser.feed(record("result", {"status": "SUCCESS", "response": ""}))
    assert parser.attention and parser.text == "I will run the tool now"


def test_success_with_unfinished_tool_is_failure(tmp_path):
    parser = initialized(tmp_path)
    parser.feed(record("step_update", {"step_index": 0, "step_type": "tool", "state": "RUNNING"}))
    parser.feed(record("result", {"status": "SUCCESS", "response": "ok"}))
    assert "unfinished tool" in parser.error


async def test_success_stdin_lifetime_argv_env_cwd_and_raw_order(tmp_path):
    worker = fake_agy(tmp_path, """
message = json.loads(sys.stdin.readline())
assert message == {'event': 'user', 'message': {'content': 'test'}}
assert not select.select([sys.stdin], [], [], .05)[0], 'stdin closed early'
assert '--print' not in sys.argv and '-p' not in sys.argv and 'test' not in sys.argv
assert sys.argv[sys.argv.index('--print-timeout') + 1] == '0s'
assert 'OPENAI_API_KEY' not in os.environ and 'PYTHONPATH' not in os.environ
init()
send('result', {'status': 'SUCCESS', 'response': 'ok'})
assert sys.stdin.read() == ''
""", help_stderr=True)
    events = []

    async def emit(event):
        events.append(event)

    result = await run(worker, tmp_path, emit=emit)
    assert result.status == "completed" and result.text == "ok" and result.usage is None
    assert [e.kind for e in events] == ["launch", "wire", "init", "wire", "result"]


@pytest.mark.parametrize("body,status,match", [
    ("init()\n", "failed", "without a terminal"),
    ("send('result', {'status': 'SUCCESS', 'response': 'ok'})\n", "failed", "before init"),
    ("init(); send('result', {'status': 'SUCCESS', 'response': 'ok'}); sys.exit(2)\n", "failed", "code 2"),
    ("init(); send('permission_request', {'tool': 'Bash'}); time.sleep(60)\n", "needs_attention", "Nexus approved nothing"),
    ("print('Please sign in', file=sys.stderr)\n", "needs_attention", "interactively"),
    ("init(); send('result', {'status': 'FAILED', 'response': 'partial'})\n", "failed", "did not report SUCCESS"),
])
async def test_completion_is_never_inferred(tmp_path, body, status, match):
    result = await run(fake_agy(tmp_path, body), tmp_path)
    assert result.status == status and match in result.error


async def test_mode_fallback_and_denied_actions_are_retained(tmp_path):
    worker = fake_agy(tmp_path, """
init()
print('--mode auto is not available for this account', file=sys.stderr, flush=True)
send('step_update', {'step_index': 0, 'step_type': 'tool', 'state': 'ERROR', 'tool_name': 'Bash'})
send('result', {'status': 'SUCCESS', 'response': '', 'denied_actions': [{'display_name': 'Bash', 'action': 'command'}]})
""")
    result = await run(worker, tmp_path)
    assert result.status == "needs_attention"
    assert "Bash" in result.denied_actions[0]
    assert any("mode fallback" in note for note in result.notes)


@pytest.mark.parametrize("limit", ["MAX_LINE_BYTES", "MAX_STDOUT_BYTES"])
async def test_output_limit_fails_and_is_announced(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(antigravity, limit, 256)
    result = await run(fake_agy(tmp_path, "init(); print('x' * 1000, flush=True); time.sleep(60)\n"), tmp_path)
    assert result.status == "failed" and "limit" in result.error
    assert result.notes


async def test_stderr_is_bounded_and_sanitized(tmp_path):
    result = await run(fake_agy(tmp_path, """
init()
sys.stderr.write('x' * 40000 + '\\napi_key=verysecret\\n'); sys.stderr.flush()
send('result', {'status': 'SUCCESS', 'response': 'ok'})
"""), tmp_path)
    assert result.status == "completed"
    assert len(result.diagnostics) <= base.MAX_DIAGNOSTIC_BYTES
    assert "verysecret" not in result.diagnostics
    assert any("stderr clipped" in note for note in result.notes)


@pytest.mark.parametrize("version,help_text,match", [("1.3.6", None, "1.4.12"), ("unknown", None, "verified"),
                                                   ("1.4.12", "--input-format-x", "lacks required")])
async def test_preflight_fails_without_running_assignment(tmp_path, version, help_text, match):
    worker = fake_agy(tmp_path, "raise AssertionError('must not run')\n", version=version, help_text=help_text)
    with pytest.raises(WorkerError, match=match):
        await run(worker, tmp_path)


async def test_probe_timeout_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "PROBE_TIMEOUT_SECONDS", .05)
    worker = fake_agy(tmp_path, "time.sleep(60)\n")
    with pytest.raises(WorkerError, match="timed out"):
        await base.probe(worker.executable, "--unknown", env=worker.env, cwd=tmp_path)


@pytest.mark.parametrize("prompt,model", [("x" * (base.MAX_PROMPT_BYTES + 1), MODEL), ("x\0", MODEL), ("x", "--bad")])
async def test_bad_input_is_refused_before_probe(tmp_path, prompt, model):
    worker = AntigravityWorker("/does/not/exist")
    with pytest.raises(ValueError):
        await worker.run(prompt=prompt, model=model, workspace=tmp_path)


async def wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.01)
    return path.read_text()


async def test_cancel_is_scoped_to_owned_group(tmp_path):
    worker = fake_agy(tmp_path, """
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open('pids', 'w').write(f'{os.getpid()} {child.pid}')
def stop(*args):
 child.wait(); sys.exit(0)
signal.signal(signal.SIGTERM, stop)
init()
time.sleep(60)
""")
    unrelated = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    task = asyncio.create_task(worker.run(prompt="test", model=MODEL, workspace=tmp_path))
    try:
        pids = [int(v) for v in (await wait_file(tmp_path / "pids")).split()]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert unrelated.returncode is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        unrelated.terminate()
        await unrelated.wait()


async def test_reaped_process_is_not_signalled(monkeypatch):
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", start_new_session=True)
    await process.wait()

    def forbidden(*args):
        pytest.fail("must not signal a reaped process group")

    monkeypatch.setattr(os, "killpg", forbidden)
    await base.stop_process(process)


async def test_event_consumer_failure_stops_worker(tmp_path):
    worker = fake_agy(tmp_path, "open('pid', 'w').write(str(os.getpid())); init(); time.sleep(60)\n")

    async def emit(event):
        if event.kind == "wire":
            raise RuntimeError("consumer failed")

    with pytest.raises(RuntimeError, match="consumer failed"):
        await run(worker, tmp_path, emit=emit)
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("state", [None, [], {}])
def test_malformed_tool_state_is_a_failure_not_an_exception(tmp_path, state):
    parser = initialized(tmp_path)
    parser.feed(record("step_update", {"step_index": 0, "step_type": "tool", "state": state}))
    assert parser.error == "malformed tool step state"


def test_duplicate_init_cannot_replace_session(tmp_path):
    parser = initialized(tmp_path)
    parser.feed(record("init", {}, conversation_id="other"))
    assert parser.error == "duplicate init event" and parser.session_id == "c1"


def test_clipping_keeps_raw_record_and_announces_exact_count(tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "MAX_RESULT_CHARS", 4)
    parser = initialized(tmp_path)
    raw = record("result", {"status": "SUCCESS", "response": "123456789"})
    events = parser.feed(raw)
    assert events[0].raw == raw and parser.text == "1234"
    assert parser.notes == ["answer clipped 5 characters"]


def test_terminal_cursor_spacing_and_large_numbers_are_safe():
    assert diagnostic_text(b"permission\x1b[1Crequired") == "permission required"
    assert diagnostic_text(b"a\x1b[" + b"9" * 5000 + b"Cb") == "a" + " " * 200 + "b"


def test_quoted_credentials_are_redacted():
    assert "secret" not in diagnostic_text(b'{"api_key": "secret", "access_token": "secret"}')


async def test_stderr_attention_does_not_hang_without_stdout(tmp_path):
    worker = fake_agy(tmp_path, "print('Authentication required', file=sys.stderr, flush=True); time.sleep(60)\n")
    result = await run(worker, tmp_path)
    assert result.status == "needs_attention" and "interactively" in result.error


async def test_unknown_and_malformed_wire_lines_are_not_silently_lost(tmp_path):
    worker = fake_agy(tmp_path, "init(); print('bad json', flush=True); send('future', {}); send('result', {'status': 'SUCCESS', 'response': 'ok'})\n")
    events = []

    async def emit(event):
        events.append(event)

    result = await run(worker, tmp_path, emit=emit)
    assert result.status == "completed" and len(result.notes) == 2
    assert len([e for e in events if e.kind == "wire"]) == 4
    assert len([e for e in events if e.kind == "notice"]) == 2


async def test_probe_output_is_bounded(tmp_path):
    worker = fake_agy(tmp_path, "print('x' * 100000, flush=True); time.sleep(60)\n")
    with pytest.raises(WorkerError, match="output exceeds limit"):
        await base.probe(worker.executable, "--unknown", env=worker.env, cwd=tmp_path)


async def test_bounded_lines_accepts_final_line_without_newline():
    reader = asyncio.StreamReader()
    reader.feed_data(b"a\nb")
    reader.feed_eof()
    assert [line async for line in base.bounded_lines(reader, line_limit=1, total_limit=3)] == [b"a", b"b"]


async def test_hanging_after_success_is_not_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "EXIT_GRACE_SECONDS", .05)
    worker = fake_agy(tmp_path, "init(); send('result', {'status': 'SUCCESS', 'response': 'ok'}); time.sleep(60)\n")
    result = await run(worker, tmp_path)
    assert result.status == "failed" and result.text == "ok"


async def test_open_tool_is_closed_as_incomplete_after_eof(tmp_path):
    worker = fake_agy(tmp_path, "init(); send('step_update', {'step_index': 0, 'step_type': 'tool', 'state': 'RUNNING'})\n")
    events = []

    async def emit(event):
        events.append(event)

    result = await run(worker, tmp_path, emit=emit)
    assert result.status == "failed"
    end = [e for e in events if e.kind == "tool.end"]
    assert len(end) == 1 and end[0].data["state"] == "CANCELED"


async def test_kill_escalation_for_live_group(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "TERM_GRACE_SECONDS", .05)
    worker = fake_agy(tmp_path, "signal.signal(signal.SIGTERM, signal.SIG_IGN); open('pid', 'w').write(str(os.getpid())); init(); time.sleep(60)\n")
    task = asyncio.create_task(worker.run(prompt="test", model=MODEL, workspace=tmp_path))
    try:
        pid = int(await wait_file(tmp_path / "pid"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_denied_actions_with_unpaired_surrogate_are_safe(tmp_path):
    parser = initialized(tmp_path)
    raw = b'{"event":"result","result":{"status":"SUCCESS","response":"ok","denied_actions":["\\ud800"]}}'
    events = parser.feed(raw)
    assert events[0].raw == raw and parser.attention
    assert parser.denied == ['"?"']


async def test_wire_is_delivered_even_if_interpretation_raises(tmp_path, monkeypatch):
    worker = fake_agy(tmp_path, "init(); time.sleep(60)\n")
    events = []

    async def emit(event):
        events.append(event)

    def fail(self, raw):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(StreamParser, "feed", fail)
    with pytest.raises(RuntimeError, match="parser bug"):
        await run(worker, tmp_path, emit=emit)
    assert [e.kind for e in events] == ["launch", "wire"]


async def test_trailing_stdout_is_drained_and_retained_after_result(tmp_path):
    worker = fake_agy(tmp_path, """
init()
send('result', {'status': 'SUCCESS', 'response': 'ok'})
for i in range(100):
 send('cleanup', {'text': 'x' * 10000})
""")
    events = []

    async def emit(event):
        events.append(event)

    result = await run(worker, tmp_path, emit=emit)
    assert result.status == "completed"
    assert len([e for e in events if e.kind == "wire"]) == 102
    assert len([e for e in events if e.kind == "result"]) == 1


async def test_terminal_control_expansion_is_bounded(tmp_path):
    worker = fake_agy(tmp_path, """
init()
sys.stderr.write('x\\x1b[200C' * 2000); sys.stderr.flush()
send('result', {'status': 'SUCCESS', 'response': 'ok'})
""")
    result = await run(worker, tmp_path)
    assert result.status == "completed"
    assert len(result.diagnostics) <= base.MAX_DIAGNOSTIC_CHARS
    assert any("diagnostics clipped" in note for note in result.notes)


async def test_repeated_cancellation_does_not_abandon_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "TERM_GRACE_SECONDS", .2)
    worker = fake_agy(tmp_path, """
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open('pid', 'w').write(str(os.getpid()))
init()
time.sleep(60)
""")
    task = asyncio.create_task(worker.run(prompt="test", model=MODEL, workspace=tmp_path))
    try:
        pid = int(await wait_file(tmp_path / "pid"))
        task.cancel()
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_reaped_leader_with_inherited_pipes_does_not_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "EXIT_GRACE_SECONDS", .05)
    worker = fake_agy(tmp_path, """
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open('descendant', 'w').write(str(child.pid))
init()
""")
    try:
        result = await run(worker, tmp_path)
        assert result.status == "failed"
        assert "descendants" in result.error
    finally:
        path = tmp_path / "descendant"
        if path.exists():
            with suppress(ProcessLookupError):
                os.kill(int(path.read_text()), signal.SIGTERM)


async def test_probe_with_reaped_leader_and_inherited_pipes_does_not_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "PROBE_TIMEOUT_SECONDS", .1)
    worker = fake_agy(tmp_path, """
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open('descendant', 'w').write(str(child.pid))
""")
    try:
        with pytest.raises(WorkerError, match="timed out"):
            await asyncio.wait_for(base.probe(worker.executable, "--unknown", env=worker.env, cwd=tmp_path), 5)
    finally:
        path = tmp_path / "descendant"
        if path.exists():
            with suppress(ProcessLookupError):
                os.kill(int(path.read_text()), signal.SIGTERM)


async def test_closed_stdin_stops_worker_instead_of_waiting_forever(tmp_path, monkeypatch):
    # Exceed even dynamically enlarged OS pipe buffers to exercise a failed drain.
    monkeypatch.setattr(antigravity, "MAX_PROMPT_BYTES", 4 * 1024 * 1024)
    worker = fake_agy(tmp_path, "os.close(0); init(); time.sleep(60)\n")
    result = await asyncio.wait_for(worker.run(prompt="x" * antigravity.MAX_PROMPT_BYTES, model=MODEL, workspace=tmp_path), 5)
    assert result.status == "failed" and "prompt delivery" in result.error


async def test_unexpected_prompt_writer_failure_propagates_and_cleans_up(tmp_path, monkeypatch):
    worker = fake_agy(tmp_path, "open('pid', 'w').write(str(os.getpid())); init(); time.sleep(60)\n")

    async def fail(self):
        await wait_file(tmp_path / "pid")
        raise RuntimeError("writer failed")

    monkeypatch.setattr(asyncio.StreamWriter, "drain", fail)
    with pytest.raises(RuntimeError, match="writer failed"):
        await run(worker, tmp_path)
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


async def test_daemon_with_zmx_off_probes_no_cli_reads_no_credentials_and_registers_no_provider(tmp_path, monkeypatch):
    """ZMX off (the default): building the daemon app and listing providers/models touch no vendor CLI or credential."""
    import builtins
    import shutil

    import httpx

    from nexus import server
    from nexus.store import Store
    from nexus.workers import registry

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    probed = []
    real_which, real_run, real_popen, real_exec = shutil.which, subprocess.run, subprocess.Popen, asyncio.create_subprocess_exec

    def vendor(*parts):
        return any(w in str(p) for p in parts for w in ("claude", "agy", "antigravity"))

    def which(name, *a, **k):
        probed.append(name) if vendor(name) else None
        return real_which(name, *a, **k)

    def run(args, *a, **k):
        probed.append(args) if vendor(args) else None
        return real_run(args, *a, **k)

    def popen(args, *a, **k):
        probed.append(args) if vendor(args) else None
        return real_popen(args, *a, **k)

    async def execute(*args, **k):
        probed.append(args) if vendor(*args) else None
        return await real_exec(*args, **k)
    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", execute)
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    runtime = server.make_runtime(Store(tmp_path / "nexus.db"))
    app = server.build_app(runtime, token="t", url="http://test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"authorization": "Bearer t"}) as client:
        providers = (await client.get("/api/provider")).json()
    assert not any(p.get("zmx") or p["id"] in ("claude-code", "agy") for p in providers)
    assert registry.visible() == {} and registry.connected_ids() == []
    # The pre-existing `claude` SDK provider checks its own sign-in (`claude auth status`); that is not ZMX.
    # ZMX probes would be `--version`/`--help`/`models` on claude, or anything at all on agy.
    flat = [" ".join(map(str, p)) if isinstance(p, (list, tuple)) else str(p) for p in probed]
    assert not [x for x in flat if "agy" in x or "antigravity" in x or "--version" in x or "--help" in x or " models" in x], flat
    assert not any(path.endswith("auth.json") and "claude-code" in path for path in opened)
    assert not [p for p in opened if "oauth_creds" in p or ".claude/.credentials" in p]
