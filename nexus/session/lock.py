"""Cross-process session locking (plan section 5.1).

Rehomes the legacy ``fcntl.flock`` mechanics into a small object with explicit
exclusive/shared modes:

* ``exclusive`` guards turn execution. It is non-blocking by default so a second
  turn attempt fails fast with :class:`~nexus.errors.SessionBusy`.
* ``shared`` is the seam for a future read-only attach (a second UI watching a
  running session). It is blocking by default, which is the useful behaviour for
  a reader.

The lock file name is ``<id>.lock`` in both the legacy and new runtimes, so a
legacy turn and a new-runtime turn contend on the same lock across processes.
Locks are released automatically by the OS on process death.
"""
from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import SessionBusy
from .ids import validate_session_id

#: ``flock`` errnos that mean genuine lock contention (non-blocking mode) rather
#: than an I/O failure. ``EACCES`` is what some platforms report instead of
#: ``EAGAIN``; ``EWOULDBLOCK`` aliases ``EAGAIN`` where defined.
_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN}
if hasattr(errno, "EWOULDBLOCK"):  # pragma: no branch - usually an alias
    _CONTENTION_ERRNOS.add(errno.EWOULDBLOCK)


def _open_lock_file(path: Path):
    """Open (creating if absent) a lock file without following a symlink.

    A lock file is not user data: it is created and held only by this code, so a
    symlink at the path is never legitimate and must not be followed -- a link
    could redirect the lock to an attacker-chosen object, and ``flock`` would
    then guard the wrong file. ``O_NOFOLLOW`` refuses it where the platform
    supports the flag. ``O_NONBLOCK`` keeps opening a special file (a FIFO) from
    blocking; a platform that refuses to lock such an object (macOS raises
    ``ENOTSUP``) then fails closed with its own error. ``O_CLOEXEC`` keeps the
    descriptor out of any child process. Missing flags degrade to plain open.
    """
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600)
    try:
        return os.fdopen(fd, "r+")
    except BaseException:
        # Never leak the descriptor if wrapping it as a file object fails.
        os.close(fd)
        raise


class SessionLock:
    """A single session's lock file, held one mode at a time."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None
        self._mode: str | None = None

    @classmethod
    def for_session(cls, directory: str | Path, session_id: str) -> SessionLock:
        session_id = validate_session_id(session_id)
        return cls(Path(directory) / f"{session_id}.lock")

    @property
    def held(self) -> bool:
        return self._handle is not None

    @property
    def mode(self) -> str | None:
        """``"exclusive"``, ``"shared"``, or ``None`` when not held."""
        return self._mode

    def acquire(self, *, shared: bool = False, blocking: bool = False) -> None:
        if self._handle is not None:
            raise RuntimeError(f"Session lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use the same hardened open as ``TrashLock``: a session lock file is
        # never a symlink and must not be followed, and ``O_CLOEXEC`` keeps the
        # descriptor out of child processes.
        handle = _open_lock_file(self.path)
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            handle.close()
            if exc.errno in _CONTENTION_ERRNOS:
                raise SessionBusy(
                    f"Session is locked by another turn: {self.path.name}"
                ) from exc
            # I/O failures (EIO, ENOLCK, unsupported locks, ...) are not
            # contention and must propagate rather than masquerade as busy.
            raise
        self._handle = handle
        self._mode = "shared" if shared else "exclusive"

    def release(self) -> None:
        handle, self._handle = self._handle, None
        self._mode = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @contextmanager
    def exclusive(self, *, blocking: bool = False) -> Iterator[SessionLock]:
        self.acquire(shared=False, blocking=blocking)
        try:
            yield self
        finally:
            self.release()

    @contextmanager
    def shared(self, *, blocking: bool = True) -> Iterator[SessionLock]:
        self.acquire(shared=True, blocking=blocking)
        try:
            yield self
        finally:
            self.release()


class TrashLock:
    """A cross-process advisory lock guarding one trash directory.

    Every mutation of a trash directory -- a producer's stage/publish/rollback,
    a recovery sweep, a restore, a purge -- must hold this lock so two processes
    can never touch the same staging entry at once. It is a plain ``flock`` on
    the sibling ``<dir>.lock``, so it is released by the OS if the holder dies
    and a crashed producer can never wedge recovery.

    Lock order is **session file lock then trash lock**: a producer already holds
    the session's exclusive :class:`SessionLock` when it reaches trash. The
    recovery sweep takes *only* this lock and never a session lock, so the two
    can never deadlock. Recovery uses a non-blocking acquire
    (:meth:`guard` with ``blocking=False``) and skips when a producer holds the
    lock, which keeps ``open``/``list`` from ever waiting on a live delete.

    The lock file lives *beside* the guarded directory (``<dir>.lock``), so it
    never appears in the trash directory's own entry listing.
    """

    #: Suffix appended to the guarded directory's name for the lock file.
    LOCK_SUFFIX = ".lock"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None

    @classmethod
    def for_dir(cls, directory: str | Path) -> TrashLock:
        directory = Path(directory)
        return cls(directory.parent / f"{directory.name}{cls.LOCK_SUFFIX}")

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, *, blocking: bool = True) -> bool:
        """Take the lock; return whether it was acquired.

        With ``blocking=False`` contention returns ``False`` instead of waiting;
        a genuine I/O failure still propagates (it is never reported as
        contention). Nested acquisition on the same object is a programming
        error and raises rather than deadlocking.
        """
        if self._handle is not None:
            raise RuntimeError(f"Trash lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = _open_lock_file(self.path)
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            handle.close()
            if exc.errno in _CONTENTION_ERRNOS:
                return False
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @contextmanager
    def guard(self, *, blocking: bool = True) -> Iterator[bool]:
        """Acquire for the duration of the block; yields whether it was held.

        A non-blocking guard that could not take the lock yields ``False`` and
        performs no cleanup, so a caller can skip its work entirely.
        """
        acquired = self.acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                self.release()


__all__ = ["SessionLock", "TrashLock"]
