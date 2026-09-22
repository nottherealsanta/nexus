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
        handle = self.path.open("a+")
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


__all__ = ["SessionLock"]
