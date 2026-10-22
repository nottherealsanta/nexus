"""Combined per-job logs, bounded and never silent (SHELL_PLAN P2)."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ToolsSection
from nexus.model.message import Text
from nexus.tools.builtin import _jobs, _shell_logs, shell
from nexus.tools.spec import ToolContext

JOB_ID_RE = re.compile(r"job_id: (job_[0-9a-f]{12})")


def body(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, Text))


def make_ctx(workspace: Path, *, session_id: str = "logs", **tools) -> ToolContext:
    config = Config(v2=ConfigV2(tools=ToolsSection(**tools))) if tools else Config()
    return ToolContext(workspace=workspace, session_id=session_id, turn_id="t", config=config)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
async def registry():
    value = _jobs.JobRegistry()
    _jobs.set_default_registry(value)
    try:
        yield value
    finally:
        await value.aclose()
        _jobs.set_default_registry(None)


def only_job(registry, ctx) -> _jobs.ShellJob:
    return registry.jobs(session_id=ctx.session_id)[-1]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -- JobLog unit ------------------------------------------------------------------


def test_joblog_keeps_head_and_tail_and_counts_everything(tmp_path):
    log = _shell_logs.JobLog(tmp_path / "x" / "j.log", max_bytes=0)
    log.open()
    chunk = b"0123456789\n" * 1000  # 11,000 bytes
    for _ in range(100):
        log.append(chunk)
    log.append(b"err\n", stderr=True)
    log.append(b"last-line-no-newline")
    log.close()
    assert log.total == 100 * 11_000 + 4 + 20
    assert log.stderr_bytes == 4
    assert log.saved == log.total and log.unsaved == 0 and log.notices() == []
    assert (tmp_path / "x" / "j.log").read_bytes()[: len(chunk)] == chunk
    head, omitted, tail = log.view(0)
    assert head == (chunk * 8)[: _shell_logs.JobLog.HEAD_CAP]
    assert len(tail) == _shell_logs.JobLog.TAIL_CAP and tail.endswith(b"last-line-no-newline")
    assert len(head) + omitted + len(tail) == log.total
    # From inside the tail the view is contiguous.
    start = log.total - 100
    head, omitted, tail = log.view(start)
    assert (head, omitted) == (b"", 0) and len(tail) == 100
    assert log.lines == log.newlines + 1


def test_joblog_small_stream_view_is_the_whole_stream(tmp_path):
    log = _shell_logs.JobLog(None)
    log.append(b"a\nb\n")
    assert log.view(0) == (b"", 0, b"a\nb\n")
    assert log.lines == 2 and log.unsaved == 0 and log.notices() == []


def test_joblog_limit_counts_unsaved_bytes_exactly(tmp_path):
    log = _shell_logs.JobLog(tmp_path / "j.log", max_bytes=100)
    log.append(b"x" * 60)
    log.append(b"y" * 60)
    log.append(b"z" * 30)
    log.close()
    assert log.saved == 100 and log.total == 150 and log.unsaved == 50
    assert (tmp_path / "j.log").read_bytes() == b"x" * 60 + b"y" * 40
    assert log.notices() == ["[log truncated at 100 bytes; 50 bytes not saved]"]
    mode = (tmp_path / "j.log").stat().st_mode & 0o777
    assert mode == 0o600 and (tmp_path.stat().st_mode & 0o777) != 0  # file is private


def test_log_directory_is_private(tmp_path):
    path = tmp_path / "shell" / "s" / "job_aaaaaaaaaaaa.log"
    log = _shell_logs.JobLog(path)
    log.append(b"hi")
    log.close()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_prune_logs_enforces_retention_and_total_and_spares_protected(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    base = _shell_logs.session_dir(workspace, "s")
    base.mkdir(parents=True)
    now = time.time()

    def make(name: str, age_days: float, size: int) -> Path:
        path = base / f"{name}.log"
        path.write_bytes(b"x" * size)
        stamp = now - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    old = make("job_000000000001", 10, 10)
    mid = make("job_000000000002", 2, 100)
    new = make("job_000000000003", 1, 100)
    running = make("job_000000000004", 20, 100)  # old, but protected
    removed = _shell_logs.prune_logs(
        workspace, max_total_bytes=0, retention_days=7, protected=[running], now=now, force=True
    )
    assert removed == 1 and not old.exists() and mid.exists() and new.exists() and running.exists()
    removed = _shell_logs.prune_logs(
        workspace, max_total_bytes=250, retention_days=0, protected=[running], now=now, force=True
    )
    # 300 bytes on disk > 250: the oldest unprotected log goes first.
    assert removed == 1 and not mid.exists() and new.exists() and running.exists()


def test_log_path_rejects_odd_job_ids_and_hashes_odd_session_ids(tmp_path):
    with pytest.raises(ValueError):
        _shell_logs.log_path(tmp_path, "s", "../../etc/passwd")
    directory = _shell_logs.session_dir(tmp_path, "../weird/session")
    assert directory.parent == _shell_logs.shell_root(tmp_path)
    assert ".." not in directory.name and "/" not in directory.name


# -- the tool ------------------------------------------------------------------------


async def test_big_output_shows_the_last_line_and_the_log_is_complete(registry, workspace):
    ctx = make_ctx(workspace)
    command = "yes 0123456789abcdef | head -c 5000000; echo; echo FINAL-SUMMARY"
    result = await shell.run({"command": command}, ctx)
    assert result.is_error is False
    text = body(result)
    assert text.rstrip().endswith("FINAL-SUMMARY")
    job = only_job(registry, ctx)
    assert job.log.path is not None and str(job.log.path) in text and "action=log" in text
    data = job.log.path.read_bytes()
    assert len(data) == job.log.total == 5_000_000 + 1 + len("FINAL-SUMMARY\n")
    assert data.endswith(b"FINAL-SUMMARY\n") and job.log.unsaved == 0
    assert len(text.encode()) < 60 * 1024  # the conversation copy is bounded

    first = await shell.run(
        {"action": "log", "job_id": job.job_id, "offset": 0, "limit": 64}, ctx
    )
    assert data[:64].decode() in body(first) and "next offset 64" in body(first)
    mid = await shell.run(
        {"action": "log", "job_id": job.job_id, "offset": 2_000_000, "limit": 100}, ctx
    )
    assert data[2_000_000:2_000_100].decode() in body(mid)
    tail = await shell.run({"action": "log", "job_id": job.job_id, "tail_lines": 2}, ctx)
    assert "FINAL-SUMMARY" in body(tail) and "start of log" not in body(tail)


async def test_stdout_summary_is_not_hidden_behind_stderr(registry, workspace):
    ctx = make_ctx(workspace)
    command = "seq 1 5000; echo 'warning: late' >&2; sleep 0.3; echo SUMMARY-LAST"
    result = await shell.run({"command": command}, ctx)
    text = body(result)
    assert text.rstrip().endswith("SUMMARY-LAST")
    assert "warning: late" in text and "\n1\n" in text
    assert "Full log:" in text


async def test_job_over_the_log_limit_reports_the_exact_unsaved_count(registry, workspace):
    ctx = make_ctx(workspace, shell_log_max_bytes=1000)
    result = await shell.run({"command": "seq 1 5000"}, ctx)
    job = only_job(registry, ctx)
    assert job.log.saved == 1000 and job.log.total > 1000
    assert f"[log truncated at 1000 bytes; {job.log.total - 1000} bytes not saved]" in body(result)
    assert job.log.path.stat().st_size == 1000
    read = await shell.run({"action": "log", "job_id": job.job_id, "tail_lines": 3}, ctx)
    assert f"{job.log.total - 1000} bytes not saved" in body(read)


async def test_evicted_job_is_still_readable_and_deleted_log_is_reported(workspace):
    local = _jobs.JobRegistry(max_completed_jobs=1)
    _jobs.set_default_registry(local)
    try:
        ctx = make_ctx(workspace)
        first = await shell.run({"command": "echo first-output"}, ctx)
        first_id = JOB_ID_RE.search(body(first)).group(1)
        await shell.run({"command": "echo second-output"}, ctx)
        assert local.job(first_id, session_id=ctx.session_id) is None  # evicted from memory
        read = await shell.run({"action": "log", "job_id": first_id}, ctx)
        assert read.is_error is False and "first-output" in body(read)

        _shell_logs.log_path(workspace, ctx.session_id, first_id).unlink()
        gone = await shell.run({"action": "log", "job_id": first_id}, ctx)
        assert gone.is_error is True and "no longer available" in body(gone)

        live = local.jobs(session_id=ctx.session_id)[0]
        live.log.path.unlink()
        missing = await shell.run({"action": "log", "job_id": live.job_id}, ctx)
        assert missing.is_error is True and "no longer available" in body(missing)
    finally:
        await local.aclose()
        _jobs.set_default_registry(None)


async def test_log_is_scoped_to_the_session_and_validates_input(registry, workspace):
    ctx = make_ctx(workspace)
    await shell.run({"command": "echo private"}, ctx)
    job = only_job(registry, ctx)
    other = make_ctx(workspace, session_id="someone-else")
    foreign = await shell.run({"action": "log", "job_id": job.job_id}, other)
    assert foreign.is_error is True and "private" not in body(foreign)
    for args in (
        {"action": "log"},
        {"action": "log", "job_id": job.job_id, "tail_lines": 5, "offset": 0},
        {"action": "log", "job_id": job.job_id, "tail_lines": 0},
        {"action": "log", "job_id": job.job_id, "offset": -1},
        {"action": "log", "job_id": job.job_id, "limit": 5},
        {"action": "log", "job_id": job.job_id, "grep": ""},
        {"action": "log", "job_id": job.job_id, "grep": "x" * 300},
        {"action": "log", "job_id": "../../etc/passwd"},
        {"action": "log", "job_id": job.job_id, "command": "ls"},
    ):
        assert (await shell.run(args, ctx)).is_error is True, args


async def test_log_grep_returns_numbered_matching_lines(registry, workspace):
    ctx = make_ctx(workspace)
    await shell.run({"command": "printf 'ok\\nFAILED a\\nok\\nFAILED b\\n'"}, ctx)
    job = only_job(registry, ctx)
    found = await shell.run({"action": "log", "job_id": job.job_id, "grep": "FAILED"}, ctx)
    assert "2: FAILED a" in body(found) and "4: FAILED b" in body(found)
    none = await shell.run({"action": "log", "job_id": job.job_id, "grep": "nothing-here"}, ctx)
    assert "(no matches)" in body(none)
    limited = await shell.run(
        {"action": "log", "job_id": job.job_id, "grep": "FAILED", "limit": 1}, ctx
    )
    assert "FAILED a" in body(limited) and "FAILED b" not in body(limited)


async def test_write_error_stops_logging_but_never_the_job(registry, workspace):
    ctx = make_ctx(workspace)
    started = await shell.run(
        {"command": "echo one; sleep 0.4; echo two", "background": True}, ctx
    )
    job = only_job(registry, ctx)
    real = job.log._fd
    assert real is not None
    job.log._fd = os.open(job.log.path, os.O_RDONLY)  # writes now fail with EBADF
    await job.wait(10)
    os.close(real)
    assert job.exit_code == 0 and job.log.error is not None
    text = _jobs.format_job_output(job)
    assert "two" in text and "[log write failed" in text
    assert "job_id" in body(started)


async def test_processes_left_by_a_finished_command_are_stopped(registry, workspace):
    ctx = make_ctx(workspace)
    started = time.monotonic()
    result = await shell.run({"command": "sleep 30 & echo CHILD:$!; echo done"}, ctx)
    assert time.monotonic() - started < 10
    text = body(result)
    assert "done" in text and "processes left running by the command were stopped" in text
    pid = int(re.search(r"CHILD:(\d+)", text).group(1))
    deadline = time.monotonic() + 3
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pid_alive(pid)


async def test_environment_values_never_reach_the_log_or_result(registry, workspace):
    ctx = make_ctx(workspace)
    result = await shell.run(
        {"command": "echo visible", "env": {"NEXUS_LOG_SECRET": "s3cr3t-env-value"}}, ctx
    )
    job = only_job(registry, ctx)
    assert "s3cr3t-env-value" not in body(result)
    assert "s3cr3t-env-value" not in job.log.path.read_text()
    assert "s3cr3t-env-value" not in repr(job)


async def test_background_result_names_cwd_and_log(registry, workspace):
    ctx = make_ctx(workspace)
    result = await shell.run({"command": "sleep 5", "background": True}, ctx)
    text = body(result)
    job = only_job(registry, ctx)
    assert f"cwd: {workspace}" in text and f"log: {job.log.path}" in text
    await shell.run({"action": "stop", "job_id": job.job_id}, ctx)
