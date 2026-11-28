"""Direct-mode credentials and locks, independent of other provider refreshes."""
from __future__ import annotations

import asyncio
import fcntl
import os
from contextlib import asynccontextmanager, contextmanager

from ...paths import home
from .store import CredentialStore

_LOCKS: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}


class DirectStore(CredentialStore):
    def __init__(self, path=None):
        super().__init__(path or home() / "claude-direct" / "auth.json")

    @contextmanager
    def locked(self):
        self._ensure_dir()
        fd = os.open(self.path.parent / "claude-direct.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @asynccontextmanager
    async def alocked(self):
        key = (asyncio.get_running_loop(), str(self.path.resolve()))
        async with _LOCKS.setdefault(key, asyncio.Lock()):
            self._ensure_dir()
            fd = os.open(self.path.parent / "claude-direct.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
