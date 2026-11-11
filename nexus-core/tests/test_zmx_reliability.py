"""ZMX reliability (zmx-design M4): recovery wording, crash transcript read, worker records, doctor, races. No real CLI."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_recovery import Crash, restart, texts, tool_results
from test_zmx_claude import fake, fix_fast, run  # noqa: F401  (fixtures and helpers shared with the Claude worker tests)

from nexus import doctor
from nexus.llmhub.types import ToolCall
from nexus.workers import records

pytestmark = pytest.mark.skipif(os.name != "posix", reason="ZMX requires POSIX")
CLAUDE_SESSION = "9aa2490a-afd1-4a95-9e89-6009078623a8"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(mode=0o700)
    return tmp_path


def external_crash(tmp_path, model="claude-code/claude-haiku-5-5"):
    workspace = tmp_path / "proj"
    workspace.mkdir(exist_ok=True)
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    crash.store.update_session(session.id, model=model)
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("c1", "Bash", '{"command": "touch changed"}', "claude-code")
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    return crash, session, turn, workspace


def test_recovery_text_says_the_cli_ran_the_tool_and_may_still_be_running(home):
    crash, session, *_ = external_crash(home)
    crash.close()
    rt = restart(home / "nexus.db", model="claude-code/claude-haiku-5-5")
    try:
        [result] = tool_results(rt.store, session.id)
        assert result.is_error and "the CLI ran this tool itself" in result.output and "may still be running" in result.output
        assert "may have changed the workspace" not in result.output
        [notice] = texts(rt.store, session.id, "user")
        assert "ran on a vendor CLI (ZMX)" in notice and "never kills a process by a stored PID" in notice
        assert "it was not retried".replace("it was", "It was") in notice or "not retried" in notice
        assert rt.recovered[0]["tools"][0]["outcome"] == "external"
    finally:
        rt.store.close()


def test_non_external_recovery_text_is_unchanged(home):
    crash, session, *_ = external_crash(home, model="")
    crash.close()
    rt = restart(home / "nexus.db")
    try:
        [result] = tool_results(rt.store, session.id)
        assert "may have changed the workspace" in result.output
        assert "ZMX" not in texts(rt.store, session.id, "user")[0]
    finally:
        rt.store.close()


def write_transcript(config: Path, workspace: Path, lines: list[dict]) -> Path:
    path = config / "projects" / "-tmp-x" / f"{CLAUDE_SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_crash_recovery_reads_the_rest_of_the_claude_transcript_once_and_labels_it(home, monkeypatch):
    config = home / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    crash, session, turn, workspace = external_crash(home)
    path = write_transcript(config, workspace, [
        {"type": "user", "message": {"role": "user", "content": "earlier prompt"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "OLD answer"}]}},
        {"type": "user", "message": {"role": "user", "content": "the assignment"}},
        {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [{"type": "text", "text": "SIDE"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "finished after the daemon died"}]}},
    ])
    records.write(turn["id"], {"pid": 999999, "started": "gone", "provider": "claude-code", "transcript_path": str(path)})
    crash.close()
    rt = restart(home / "nexus.db", model="claude-code/claude-haiku-5-5")
    try:
        [notice] = texts(rt.store, session.id, "user")
        assert "Recovered from the CLI's transcript after the crash" in notice and "finished after the daemon died" in notice
        assert "OLD answer" not in notice and "SIDE" not in notice
        assert records.read(turn["id"]) is None  # the process is gone, so the record is dropped
    finally:
        rt.store.close()


def test_transcript_outside_the_cli_config_is_never_read(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude-config"))
    outside = home / "outside.jsonl"
    outside.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "SECRET"}]}}))
    crash, session, turn, _ = external_crash(home)
    records.write(turn["id"], {"pid": 1, "started": "x", "provider": "claude-code", "transcript_path": str(outside)})
    crash.close()
    rt = restart(home / "nexus.db", model="claude-code/claude-haiku-5-5")
    try:
        assert "SECRET" not in texts(rt.store, session.id, "user")[0]
    finally:
        rt.store.close()


def test_orphan_record_needs_pid_and_start_time_and_cleanup_is_explicit(home):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        started = records.process_start(child.pid)
        assert started
        records.write("t-live", {"pid": child.pid, "started": started, "provider": "agy"})
        records.write("t-reused", {"pid": child.pid, "started": "Mon Jan  1 00:00:00 1990", "provider": "agy"})
        records.write("t-dead", {"pid": 999999, "started": started, "provider": "agy"})
        assert [r["session_id"] for r in records.orphans()] == ["t-live"]  # listing kills nothing
        assert child.poll() is None
        assert records.terminate(records.read("t-reused")) is False and child.poll() is None  # start time mismatch
        assert records.terminate(records.read("t-live")) is True
        assert child.wait(5) is not None
    finally:
        child.kill()
        child.wait()


def test_record_directory_is_private_and_ids_are_validated(home):
    records.write("turn-1", {"pid": 1})
    assert oct(records.records_dir().stat().st_mode & 0o777) == "0o700"
    with pytest.raises(Exception, match="invalid session id"):
        records.write("../escape", {"pid": 1})


def test_doctor_reports_zmx_off_without_probing_and_on_with_a_missing_cli(home, monkeypatch):
    rows = {r["name"]: r for r in doctor.check_zmx()}
    assert "off (no CLI probed)" in rows["zmx agy"]["detail"] and rows["zmx claude-code"]["status"] == "ok"
    from nexus.config import ConfigFile
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.antigravity": True}, [])
    monkeypatch.setenv("NEXUS10_AGY_CLI", str(home / "missing-agy"))
    rows = {r["name"]: r for r in doctor.check_zmx()}
    assert rows["zmx agy"]["status"] == "fail" and "missing-agy" in rows["zmx agy"]["detail"]


def test_doctor_reports_a_cli_version_and_flags(home, monkeypatch):
    fake_agy = home / "agy"
    fake_agy.write_text(f"#!{sys.executable}\nimport sys\n"
                        "if '--version' in sys.argv: print('1.5.0')\n"
                        "else: print('--input-format --output-format stream-json --model --mode auto accept-edits plan --print-timeout')\n")
    fake_agy.chmod(0o700)
    from nexus.config import ConfigFile
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.antigravity": True}, [])
    monkeypatch.setenv("NEXUS10_AGY_CLI", str(fake_agy))
    row = {r["name"]: r for r in doctor.check_zmx()}["zmx agy"]
    assert row["status"] == "ok" and "version 1.5.0" in row["detail"] and "sign-in not verified" in row["detail"]


# -- races (Claude PTY worker, fake CLI) -------------------------------------------------------------------------------


async def test_cancel_during_stop_leaves_no_process_and_no_record(fake, monkeypatch):  # noqa: F811
    fix_fast(monkeypatch)
    configure, workspace, tmp, _ = fake
    worker = configure(mode="success", sleep=30)
    stopped = asyncio.Event()

    async def emit(event):
        if event.kind == "assistant" and "All done" in json.dumps(event.data):
            stopped.set()

    task = asyncio.create_task(worker.run(prompt="assignment", model="claude-haiku-4-5", workspace=workspace, emit=emit))
    await asyncio.wait_for(stopped.wait(), 20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(.3)
    leftover = await asyncio.to_thread(lambda: subprocess.run(["pgrep", "-f", str(tmp / "fake-claude")], capture_output=True, text=True, check=False).stdout.strip())
    assert leftover == ""


async def test_duplicate_stop_hook_is_ignored_and_completes_once(fake, monkeypatch):  # noqa: F811
    fix_fast(monkeypatch)
    configure, workspace, *_ = fake
    result, events = await run(configure(mode="success", sleep=1, duplicate_stop=True), workspace)
    assert result.status == "completed" and [e.kind for e in events].count("assistant") == 2


async def test_exit_before_flush_waits_for_the_late_transcript_line(fake, monkeypatch):  # noqa: F811
    fix_fast(monkeypatch)
    monkeypatch.setattr("nexus.workers.claude_code.RECONCILE_SECONDS", 1.5)
    configure, workspace, *_ = fake
    result, _ = await run(configure(mode="late"), workspace)
    assert result.status == "completed" and result.text == "late"


async def test_crash_between_entries_keeps_the_partial_line_out(fake, monkeypatch):  # noqa: F811
    fix_fast(monkeypatch)
    configure, workspace, *_ = fake
    result, events = await run(configure(mode="nohook", sleep=1), workspace)
    assert result.status == "failed" and "without a Stop hook" in result.error
    assert [e.kind for e in events].count("assistant") == 1


def test_agy_usage_parses_the_cli_usage_command_and_stays_off_while_zmx_is_off(home):
    from nexus import provider_usage
    data = json.loads((Path(__file__).parent / "fixtures/workers/agy-1.5.0/usage.json").read_text())
    row = provider_usage.parse_agy(data)
    labels = {w["label"]: w for w in row["windows"]}
    assert labels["Gemini Models · Five Hour Limit"]["used_percent"] == 50.0
    assert labels["Gemini Models · Five Hour Limit"]["kind"] == "five_hour"
    assert labels["Gemini Models · Weekly Limit"]["kind"] == "weekly" and labels["Gemini Models · Weekly Limit"]["resets_at"] > 1.7e9
    assert labels["Claude and GPT models · Five Hour Limit"]["used_percent"] == 0.0
    with pytest.raises(provider_usage.UsageError):
        provider_usage.parse_agy({})
    assert "agy" not in provider_usage.connected_providers()


FIXTURES = Path(__file__).parent / "fixtures/workers"


def test_recorded_claude_2_1_322_transcript_parses_without_misreporting():
    """Sanitized real transcript (Haiku 5.5, 2.1.322): the tool call, its result and the answer come out; wire-only entries notice."""
    from nexus.workers.claude_transcript import TranscriptReader
    events = TranscriptReader(FIXTURES / "claude-2.1.322/transcript.jsonl", CLAUDE_SESSION).poll()
    kinds = [e.kind for e in events]
    assistants = [e for e in events if e.kind == "assistant"]
    assert kinds.count("assistant") == 3 and kinds.count("tool.result") == 1
    assert assistants[0].data["parts"][0]["name"] == "Read" and assistants[0].data["usage"]["output_tokens"] > 0
    assert assistants[1].data["parts"][-1]["text"] == "hello zmx"
    # api-request-* entries are present but only wire-announced: the context stays declared incomplete.
    notices = " ".join(e.data["text"] for e in events if e.kind == "notice")
    assert "api-request" in notices and "context is incomplete" in notices


def test_recorded_claude_subagent_files_link_to_the_agent_call():
    from nexus.workers.claude_transcript import SubagentReader
    [event] = SubagentReader(FIXTURES / "claude-2.1.322.jsonl").poll()  # reads claude-2.1.322/subagents/.poll()
    assert event.data["tool_use_id"] == "toolu_01RVKhmeCADzDbXuLJGPf4Gv" and event.data["agent_type"] == "general-purpose"
    names = [e.get("name") for e in event.data["entries"] if e["kind"] == "tool"]
    assert names == ["Read", "SubagentHandback"] and event.data["entries"][0]["kind"] == "prompt"


@pytest.mark.parametrize("name,text,tools", [("turn1", "hello zmx", ["read_file"]), ("turn2", None, [])])
def test_recorded_agy_1_5_0_streams_parse_with_no_unknown_step_notices(name, text, tools):
    from nexus.workers.antigravity import StreamParser
    from nexus.workers.base import WorkerPolicy
    lines = (FIXTURES / f"agy-1.5.0/{name}.ndjson").read_bytes().splitlines()
    cwd = json.loads(lines[0])["init"]["cwd"]
    parser = StreamParser("gemini-3.8-flash-low", Path(cwd), WorkerPolicy("auto"))
    events = [e for line in lines for e in parser.feed(line)]
    assert parser.finished and not parser.error and not parser.attention and not parser.notes
    assert [e.data["name"] for e in events if e.kind == "tool.start"] == tools
    assert parser.session_id and parser.usage["total_tokens"] > 0 and (text is None or text in parser.text)


def test_recorded_agy_plan_mode_write_request_ends_as_a_plan_not_a_denial():
    """agy 1.5.0 --mode plan: no file written, SUCCESS with a plan text, no denied_actions (so no needs_attention)."""
    from nexus.workers.antigravity import StreamParser
    from nexus.workers.base import WorkerPolicy
    lines = (FIXTURES / "agy-1.5.0/denial.ndjson").read_bytes().splitlines()
    parser = StreamParser("gemini-3.8-flash-low", Path(json.loads(lines[0])["init"]["cwd"]), WorkerPolicy("plan"))
    for line in lines:
        parser.feed(line)
    assert parser.finished and not parser.denied and not parser.attention and "implementation plan" in parser.text
