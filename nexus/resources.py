"""Process-wide resource admission, fair by project then session (plan 01 §6).

Only standard-library dependencies. A task's inherited scope follows a turn and
its children; standalone runtimes without a daemon scope remain unrestricted.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class FairPool:
    """Bounded active leases with round-robin admission and cancellation cleanup."""

    def __init__(self, capacity: int):
        if isinstance(capacity, bool) or not 1 <= capacity <= 1024:
            raise ValueError("Resource capacity must be 1..1024")
        self.capacity = capacity
        self.active = 0
        self._projects = deque()
        self._sessions = {}
        self._waiting = {}
        self._last_project = None
        self._last_session = {}

    async def acquire(self, project: str, session: str):
        if sum(len(q) for q in self._waiting.values()) >= 8192:
            raise ValueError("Resource admission queue is full")
        key = (project, session)
        future = asyncio.get_running_loop().create_future()
        if key not in self._waiting:
            self._waiting[key] = deque()
            self._sessions.setdefault(project, deque()).append(session)
        self._waiting[key].append(future)
        if project not in self._projects:
            self._projects.append(project)
        self._pump()
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
                self._pump()
            raise
        released = False
        def release():
            nonlocal released
            if not released:
                released = True
                self.release()
        return release

    def release(self):
        self.active -= 1
        self._pump()

    def _pump(self):
        while self.active < self.capacity and self._projects:
            if len(self._projects) > 1 and self._projects[0] == self._last_project:
                self._projects.rotate(-1)
            project = self._projects.popleft()
            sessions = self._sessions[project]
            if len(sessions) > 1 and sessions[0] == self._last_session.get(project):
                sessions.rotate(-1)
            session = sessions.popleft()
            key = (project, session)
            queue = self._waiting[key]
            while queue and queue[0].cancelled():
                queue.popleft()
            waiter = queue.popleft() if queue else None
            if queue:
                sessions.append(session)
            else:
                del self._waiting[key]
            if sessions:
                self._projects.append(project)
            else:
                del self._sessions[project]
                self._last_session.pop(project, None)
            if waiter is not None:
                self.active += 1
                self._last_project = project
                if project in self._sessions:
                    self._last_session[project] = session
                waiter.set_result(None)


class ResourceLimits:
    def __init__(self, *, max_processes=16, max_requests_per_provider=6):
        self.processes = FairPool(max_processes)
        self.max_requests_per_provider = max_requests_per_provider
        # Validate even before the first provider is used.
        FairPool(max_requests_per_provider)
        self.requests = {}

    def provider_pool(self, provider):
        if provider not in self.requests:
            if len(self.requests) >= 128:
                raise ValueError("Too many provider resource pools")
            self.requests[provider] = FairPool(self.max_requests_per_provider)
        return self.requests[provider]


@dataclass(frozen=True)
class ResourceScope:
    limits: ResourceLimits
    project: str
    session: str
    environ: object = None


current_scope: ContextVar[ResourceScope | None] = ContextVar("nexus_resource_scope", default=None)


async def acquire_process(session: str = ""):
    scope = current_scope.get()
    if scope is None:
        return lambda: None
    return await scope.limits.processes.acquire(scope.project, session or scope.session)


@asynccontextmanager
async def process_slot(session: str = ""):
    release = await acquire_process(session)
    try:
        yield
    finally:
        release()


@asynccontextmanager
async def request_slot(provider: str):
    scope = current_scope.get()
    if scope is None:
        yield
        return
    release = await scope.limits.provider_pool(provider).acquire(scope.project, scope.session)
    try:
        yield
    finally:
        release()


def resource_environment():
    """Task-local project environment; standalone callers use their own process."""
    scope = current_scope.get()
    if scope is not None and scope.environ is not None:
        return scope.environ
    import os
    return os.environ


def process_waiting():
    scope = current_scope.get()
    return scope is not None and scope.limits.processes.active >= scope.limits.processes.capacity
