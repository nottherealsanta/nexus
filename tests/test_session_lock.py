import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus.errors import SessionBusy
from nexus.model.message import Message, ToolUse
from nexus.session import migrate as migrate_module
from nexus.session.lock import SessionLock, TrashLock
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


# ---------------------------------------------------------------------------
# TrashLock hardening: a lock file is never a symlink or a blocking special
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable")
def test_trash_lock_refuses_a_symlinked_lock_file(tmp_path):
    """A symlinked lock path is an error, never a followed lock.

    Following the link would make ``flock`` guard an attacker-chosen file while
    the real trash directory went unguarded. ``O_NOFOLLOW`` fails closed with
    ``ELOOP``, and the error is a genuine I/O failure rather than contention.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    lock = TrashLock.for_dir(trash)
    target = tmp_path / "elsewhere.lock"
    target.write_text("", encoding="utf-8")
    lock.path.symlink_to(target)

    with pytest.raises(OSError) as excinfo:
        lock.acquire(blocking=False)
    assert excinfo.value.errno == errno.ELOOP
    # A non-blocking guard must propagate it, not report it as contention.
    with pytest.raises(OSError), TrashLock.for_dir(trash).guard(blocking=False):
        pass


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable")
def test_session_lock_refuses_a_symlinked_lock_file(tmp_path):
    """A session lock file is never followed through a symlink either.

    ``SessionLock`` shares the hardened open used by ``TrashLock``: following a
    planted link would make ``flock`` guard an attacker-chosen file while the
    real session went unguarded. ``O_NOFOLLOW`` fails closed with ``ELOOP``.
    """
    lock = SessionLock(tmp_path / "s.lock")
    target = tmp_path / "elsewhere.lock"
    target.write_text("", encoding="utf-8")
    lock.path.symlink_to(target)

    with pytest.raises(OSError) as excinfo:
        lock.acquire(shared=False, blocking=False)
    assert excinfo.value.errno == errno.ELOOP
    assert lock.held is False


def test_open_lock_file_closes_the_descriptor_when_wrapping_fails(
    tmp_path, monkeypatch
):
    """A failed ``fdopen`` must not leak the raw descriptor.

    The hardened open is the single place a lock descriptor is created, so a
    wrap failure there must close it rather than strand an fd for the process
    lifetime.
    """
    import nexus.session.lock as lock_module

    closed: list[int] = []
    real_close = lock_module.os.close

    def boom(fd, *args, **kwargs):
        raise RuntimeError("cannot wrap descriptor")

    def tracking_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(lock_module.os, "fdopen", boom)
    monkeypatch.setattr(lock_module.os, "close", tracking_close)
    with pytest.raises(RuntimeError):
        lock_module._open_lock_file(tmp_path / "x.lock")
    assert closed


def test_trash_lock_open_does_not_block_on_a_fifo(tmp_path):
    """Opening the lock file never blocks on a special file.

    ``O_NONBLOCK`` means a FIFO lock path cannot wedge acquisition. A platform
    that refuses to lock a FIFO (macOS raises ``ENOTSUP``) fails closed with its
    own error; a platform that permits it acquires and releases normally. Either
    way acquisition returns promptly.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    lock = TrashLock.for_dir(trash)
    os.mkfifo(lock.path)

    started = time.monotonic()
    try:
        acquired = lock.acquire(blocking=False)
    except OSError as exc:
        assert exc.errno != errno.EACCES
        acquired = False
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    if acquired:
        lock.release()
