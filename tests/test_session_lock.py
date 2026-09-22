import errno
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.errors import SessionBusy
from nexus.model.message import Message, ToolUse
from nexus.session import migrate as migrate_module
from nexus.session.lock import SessionLock
from nexus.session.manager import SessionManager

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_exclusive_lock_contends_and_releases(tmp_path):
    path = tmp_path / "main.lock"
    first = SessionLock(path)
    second = SessionLock(path)

    first.acquire(shared=False, blocking=False)
    assert first.held
    assert first.mode == "exclusive"
    with pytest.raises(SessionBusy):
        second.acquire(shared=False, blocking=False)

    first.release()
    assert first.held is False
    second.acquire(shared=False, blocking=False)
    second.release()


def test_shared_blocks_while_exclusive_held_then_succeeds(tmp_path):
    path = tmp_path / "main.lock"
    exclusive = SessionLock(path)
    reader = SessionLock(path)

    exclusive.acquire(shared=False, blocking=False)
    with pytest.raises(SessionBusy):
        reader.acquire(shared=True, blocking=False)

    exclusive.release()
    reader.acquire(shared=True, blocking=False)
    assert reader.mode == "shared"
    reader.release()


def test_two_shared_readers_coexist(tmp_path):
    path = tmp_path / "main.lock"
    first = SessionLock(path)
    second = SessionLock(path)
    first.acquire(shared=True, blocking=False)
    second.acquire(shared=True, blocking=False)
    assert first.mode == second.mode == "shared"
    second.release()
    first.release()


def test_context_manager_releases_on_exception(tmp_path):
    path = tmp_path / "main.lock"
    lock = SessionLock(path)
    with pytest.raises(RuntimeError), lock.exclusive(blocking=False):
        assert lock.held
        raise RuntimeError("boom")
    assert lock.held is False
    with lock.exclusive(blocking=False):
        pass


def test_reacquire_without_release_is_an_error(tmp_path):
    lock = SessionLock(tmp_path / "main.lock")
    lock.acquire(shared=False, blocking=False)
    with pytest.raises(RuntimeError):
        lock.acquire(shared=False, blocking=False)
    lock.release()


def test_for_session_uses_legacy_lock_filename(tmp_path):
    lock = SessionLock.for_session(tmp_path, "my-session")
    assert lock.path.name == "my-session.lock"


def test_lock_is_cross_process(tmp_path):
    path = tmp_path / "main.lock"
    parent = SessionLock(path)
    parent.acquire(shared=False, blocking=False)

    def probe() -> subprocess.CompletedProcess:
        script = (
            "import sys\n"
            "from nexus.session.lock import SessionLock\n"
            "from nexus.errors import SessionBusy\n"
            "lock = SessionLock(sys.argv[1])\n"
            "try:\n"
            "    lock.acquire(shared=False, blocking=False)\n"
            "    print('acquired')\n"
            "    lock.release()\n"
            "except SessionBusy:\n"
            "    print('busy')\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script, str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    blocked = probe()
    assert blocked.returncode == 0, blocked.stderr
    assert blocked.stdout.strip() == "busy"

    parent.release()
    acquired = probe()
    assert acquired.returncode == 0, acquired.stderr
    assert acquired.stdout.strip() == "acquired"


# ---------------------------------------------------------------------------
# Error classification: only genuine contention is SessionBusy
# ---------------------------------------------------------------------------


def _flock_raiser(exc: BaseException):
    def raiser(fd, flags):
        raise exc

    return raiser


def test_blocking_io_error_is_session_busy(tmp_path, monkeypatch):
    import nexus.session.lock as lock_module

    lock = SessionLock(tmp_path / "main.lock")
    monkeypatch.setattr(
        lock_module.fcntl,
        "flock",
        _flock_raiser(BlockingIOError(errno.EAGAIN, "would block")),
    )
    with pytest.raises(SessionBusy):
        lock.acquire(shared=False, blocking=False)


def test_non_contention_os_error_propagates(tmp_path, monkeypatch):
    import nexus.session.lock as lock_module

    lock = SessionLock(tmp_path / "main.lock")
    monkeypatch.setattr(
        lock_module.fcntl, "flock", _flock_raiser(OSError(errno.EIO, "I/O error"))
    )
    with pytest.raises(OSError) as excinfo:
        lock.acquire(shared=False, blocking=False)
    assert excinfo.value.errno == errno.EIO


def test_recovery_propagates_non_contention_error(tmp_path, monkeypatch):
    import nexus.session.lock as lock_module

    session = SessionManager(tmp_path).open("main")
    session.append_message(
        Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})])
    )
    monkeypatch.setattr(
        lock_module.fcntl, "flock", _flock_raiser(OSError(errno.EIO, "I/O error"))
    )
    with pytest.raises(OSError) as excinfo:
        session.recover_dangling_tool_uses()
    assert excinfo.value.errno == errno.EIO


def test_migration_propagates_non_contention_error(tmp_path, monkeypatch):
    import nexus.session.lock as lock_module

    legacy = migrate_module.legacy_path(tmp_path, "main")
    legacy.write_text(
        json.dumps({"version": 1, "exchanges": [{"user": "a", "assistant": "b"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lock_module.fcntl, "flock", _flock_raiser(OSError(errno.EIO, "I/O error"))
    )
    with pytest.raises(OSError) as excinfo:
        SessionManager(tmp_path).migrate("main")
    assert excinfo.value.errno == errno.EIO
