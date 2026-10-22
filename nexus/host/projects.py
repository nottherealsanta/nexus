"""Lazy, bounded project hosts and connection routing (worktree-sessions/01).

One runtime owns each project's settings, grants and sessions. References pin a
host across focus changes and subscriptions; only idle hosts may be evicted.
Environment snapshots are inward-only and never included in diagnostics.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..host_support.project_identity import ProjectIdentity, resolve_project
from ..observability.daemon import DaemonDiagnostics
from ..util import redact_secrets
from ..resources import ResourceLimits, ResourceScope, current_scope
from . import protocol as p
from .facade import HostFacade
from .supervisor import Supervisor

MAX_PROJECTS = 32
MAX_ENV_VARS = 1024
MAX_ENV_BYTES = 256 * 1024


def validate_environment(env: dict[str, str] | None) -> dict[str, str]:
    snapshot = dict(env or {})
    if len(snapshot) > MAX_ENV_VARS:
        raise ValueError("Environment exceeds 1024 variables")
    if any(not k or "=" in k or "\0" in k or "\0" in v for k, v in snapshot.items()):
        raise ValueError("Invalid environment snapshot")
    if sum(len(k.encode()) + len(v.encode()) + 2 for k, v in snapshot.items()) > MAX_ENV_BYTES:
        raise ValueError("Environment exceeds 256 KiB")
    return snapshot


def project_ref(identity: ProjectIdentity) -> p.ProjectRef:
    return p.ProjectRef(identity.key, str(identity.root), identity.root.name, identity.kind)


class ProjectSupervisor:
    """A project-scoped view of the process supervisor, including teardown."""

    def __init__(self, shared: Supervisor, key: str, limits: ResourceLimits, environ) -> None:
        self.shared = shared
        self.prefix = key + ":"
        self.limits = limits
        self.environ = environ

    @property
    def max_concurrent(self):
        return self.shared.max_concurrent

    def active_sessions(self):
        return tuple(s[len(self.prefix):] for s in self.shared.active_sessions() if s.startswith(self.prefix))

    @property
    def running(self):
        return sum(self.shared.running_for(self.prefix + s) for s in self.active_sessions())

    @property
    def queued(self):
        return sum(len(q) for s, q in self.shared._queues.items() if s.startswith(self.prefix))

    def running_for(self, session):
        return self.shared.running_for(self.prefix + session)

    def queued_for(self, session):
        return self.shared.queued_for(self.prefix + session)

    def slot_wait(self, session):
        return self.shared.slot_wait(self.prefix + session)

    async def submit(self, session, handle, content=None, **kwargs):
        token = current_scope.set(ResourceScope(self.limits, self.prefix[:-1], session, self.environ))
        try:
            return await self.shared.submit(self.prefix + session, handle, content, **kwargs)
        finally:
            current_scope.reset(token)

    async def cancel(self, session, **kwargs):
        return await self.shared.cancel(self.prefix + session, **kwargs)

    def reorder(self, session, order):
        return self.shared.reorder(self.prefix + session, order)

    def forget(self, session):
        return self.shared.forget(self.prefix + session)

    def lent_slot(self, session):
        return self.shared.lent_slot(self.prefix + session)

    async def recover_admissions(self, manager):
        return await self.shared.recover_admissions(manager)

    async def wait_idle(self, *, timeout=None):
        async def wait():
            while self.running or self.queued:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout)

    async def aclose(self):
        for session in list(self.shared._sessions):
            if session.startswith(self.prefix):
                await self.shared.cancel(session, reason="shutdown")
        await self.wait_idle(timeout=5)
        for session in list(self.shared._sessions):
            if session.startswith(self.prefix):
                self.shared.forget(session)


@dataclass
class ProjectHost:
    identity: ProjectIdentity
    facade: HostFacade
    env_from: str
    loaded_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.monotonic)
    references: int = 0
    orphan_since: float | None = None

    @property
    def runtime(self):
        return self.facade.runtime

    def busy(self) -> bool:
        return bool(self.references or self.facade.presence.total_viewers()
                    or self.facade.supervisor.running or self.facade.supervisor.queued
                    or getattr(self.runtime, "has_active_shell_jobs", lambda: False)()
                    or getattr(getattr(self.runtime, "voice", None), "active", False)
                    or getattr(getattr(self.runtime, "live", None), "active", False))


class ProjectRegistry:
    def __init__(self, factory, *, home=None, max_concurrent=32, emit=None, limits=None):
        self.limits = limits or ResourceLimits()
        self.factory = factory
        self.home = home
        self.hosts: dict[str, ProjectHost] = {}
        self.emit = emit or (lambda kind, data: None)
        self.supervisor = Supervisor(max_concurrent=max_concurrent, emit=emit)
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self.on_unload = lambda key: None
        self._capacity_lock = asyncio.Lock()
        self._loading = set()
        self._retired = set()
        self._closing = {}

    def identity(self, project: str) -> ProjectIdentity:
        if project in self.hosts:
            return self.hosts[project].identity
        # Keys may refer to registered, unloaded projects.
        from ..config.paths import state_db_path
        from ..session.db import StateDatabase
        db = StateDatabase(state_db_path(self.home))
        try:
            with db.read() as conn:
                row = conn.execute("SELECT root FROM projects WHERE id=?", (project,)).fetchone()
            return resolve_project(row[0] if row else project)
        finally:
            db.close()

    async def acquire(self, identity: ProjectIdentity, env, client: str) -> ProjectHost:
        snapshot = validate_environment(env)
        if self._closed:
            raise ValueError("Daemon is stopping")
        key = identity.key
        if key not in self._locks:
            if len(self._locks) >= 64:
                raise ValueError("Too many concurrent project loads")
            self._locks[key] = asyncio.Lock()
        lock = self._locks[key]
        try:
            async with lock:
                if self._closed:
                    raise ValueError("Daemon is stopping")
                closing = self._closing.get(key)
                if closing is not None:
                    await asyncio.shield(closing)
                host = self.hosts.get(key)
                if host is None:
                    async with self._capacity_lock:
                        if len(self.hosts) + len(self._loading) + len(self._retired) + len(self._closing) >= MAX_PROJECTS:
                            idle = [h for h in self.hosts.values() if not h.busy()]
                            if not idle:
                                raise ValueError("All 32 project slots are busy")
                            await self.unload(min(idle, key=lambda h: h.last_activity).identity.key)
                        self._loading.add(key)
                    try:
                        host = await self._build_host(identity, snapshot, client)
                        if self._closed:
                            await host.facade.shutdown("daemon stopping")
                            raise ValueError("Daemon is stopping")
                        self.hosts[key] = host
                        self.emit("daemon.project_loaded", {"project": key, "root": str(identity.root),
                                                             "environment_from": host.env_from})
                    finally:
                        self._loading.discard(key)
                host.references += 1
                host.last_activity = time.monotonic()
                return host
        finally:
            # Keep locks with waiters until the last waiter has completed.
            if not lock.locked() and not getattr(lock, "_waiters", None):
                self._locks.pop(key, None)

    async def _build_host(self, identity, snapshot, client):
        # Runtime construction reads config/files and imports extensions. Keep it
        # off the event loop so another project's connections remain responsive.
        build = asyncio.create_task(asyncio.to_thread(
            self.factory, identity.root, home=self.home, environ=snapshot))
        try:
            runtime = await asyncio.shield(build)
        except BaseException:
            async def discard():
                try:
                    runtime = await build
                    await runtime.aclose()
                except Exception:
                    pass
            cleanup = asyncio.create_task(discard())
            self._retired.add(cleanup)
            cleanup.add_done_callback(self._retired.discard)
            raise
        facade = HostFacade(runtime, owns_runtime=True,
                            supervisor=ProjectSupervisor(self.supervisor, identity.key, self.limits, snapshot))
        facade.resource_limits = self.limits
        facade.project_key = identity.key
        host = ProjectHost(identity, facade, redact_secrets(client[:128]))
        try:
            await self.recover_shell_orphans(runtime, identity.key)
            await facade.recover_admissions()
            facade.daemon_diagnostics = DaemonDiagnostics()
            facade.daemon_info = {"environment_from": host.env_from,
                                  "environment_captured_at": host.loaded_at}
            facade.start_usage_polling()
            voice = getattr(runtime, "voice", None)
            if voice is not None and voice.config.enabled:
                voice.schedule_prepare(allow_download=False)
        except BaseException:
            await facade.shutdown("project load failed")
            raise
        return host

    def release(self, host: ProjectHost):
        host.references = max(0, host.references - 1)
        host.last_activity = time.monotonic()

    async def unload(self, key: str, *, force=False):
        closing = self._closing.get(key)
        if closing is not None:
            await asyncio.shield(closing)
            return
        host = self.hosts.get(key)
        if host is None:
            return
        if host.busy() and not force:
            raise ValueError("Project is in use; detach its clients or use --force")
        # Retire atomically before yielding. Acquirers await teardown before
        # constructing a replacement; eviction never takes another key's lock.
        self.hosts.pop(key)
        self.on_unload(key)
        async def close():
            try:
                await host.facade.shutdown("project unloaded")
            finally:
                self._closing.pop(key, None)
        closing = asyncio.create_task(close())
        self._closing[key] = closing
        await asyncio.shield(closing)

    async def evict_idle(self, timeout: float):
        for host in list(self.hosts.values()):
            await self.check_orphans(host)
            if timeout <= 0:
                continue
            if host.busy():
                host.last_activity = time.monotonic()
            elif time.monotonic() - host.last_activity >= timeout:
                await self.unload(host.identity.key)

    async def recover_shell_orphans(self, runtime, key):
        recover = getattr(runtime, "recover_shell_orphans", None)
        if callable(recover):
            try:
                result = await asyncio.wait_for(recover(), 8)
                if result:
                    self.emit("daemon.shell_orphans_swept", {"project": key, **result})
            except Exception as exc:
                self.emit("daemon.shell_recovery_failed", {"project": key,
                                                           "error": type(exc).__name__})

    async def check_orphans(self, host):
        runtime = host.runtime
        probe = getattr(runtime, "persistent_shell_jobs", None)
        seconds = getattr(runtime, "shell_orphan_seconds", None)
        if not callable(probe) or not callable(seconds):
            return
        limit = seconds()
        if (not limit or not probe(1)[0] or host.facade.presence.total_viewers()
                or host.facade.supervisor.running or host.facade.supervisor.queued
                or getattr(getattr(runtime, "live", None), "active", False)):
            host.orphan_since = None
            return
        now = time.monotonic()
        if host.orphan_since is None:
            host.orphan_since = now
        if now - host.orphan_since >= limit:
            reason = f"no viewer for {limit:g} s"
            count = await runtime.stop_persistent_shell_jobs(reason)
            host.orphan_since = None
            self.emit("daemon.orphan_jobs_stopped", {"project": host.identity.key, "count": count,
                                                     "reason": reason})

    async def aclose(self):
        self._closed = True
        for key in list(self.hosts):
            try:
                await self.unload(key, force=True)
            except Exception:
                pass
        if self._closing:
            await asyncio.gather(*list(self._closing.values()), return_exceptions=True)
        await self.supervisor.aclose()
        if self._retired:
            await asyncio.wait(self._retired, timeout=10)

    def registered(self, key):
        from ..config.paths import state_db_path
        from ..session.db import StateDatabase
        db = StateDatabase(state_db_path(self.home))
        try:
            with db.read() as conn:
                return conn.execute("SELECT 1 FROM projects WHERE id=?", (key,)).fetchone() is not None
        finally:
            db.close()

    def loaded_rows(self):
        rows = []
        for host in self.hosts.values():
            health = host.facade.health()
            rows.append(p.ProjectRow(project_ref(host.identity), True, host.identity.root.is_dir(),
                sessions=health["sessions"], running=health["running"], queued=health["queued"],
                last_activity=time.time() - max(0, time.monotonic() - host.last_activity),
                loaded_at=host.loaded_at, environment_from=host.env_from))
        return rows

    def rows(self, limit=100):
        if not 1 <= limit <= 500:
            raise ValueError("Project limit must be 1..500")
        from ..config.paths import state_db_path
        from ..session.db import StateDatabase
        db = StateDatabase(state_db_path(self.home))
        try:
            with db.read() as conn:
                rows = conn.execute("SELECT p.id,p.root,p.last_opened,COUNT(s.id) FROM projects p "
                                    "LEFT JOIN sessions s ON s.project_id=p.id "
                                    "GROUP BY p.id ORDER BY p.last_opened DESC LIMIT ?", (limit + 1,)).fetchall()
            result = []
            for key, root, opened, count in rows[:limit]:
                host = self.hosts.get(key)
                exists = Path(root).is_dir()
                kind = host.identity.kind if host else ("git" if (Path(root) / ".git").exists() else "plain")
                result.append(p.ProjectRow(p.ProjectRef(key, root, Path(root).name, kind),
                                           host is not None, exists, sessions=count,
                                           running=host.facade.supervisor.running if host else 0,
                                           queued=host.facade.supervisor.queued if host else 0,
                                           last_activity=opened))
            return p.ProjectListResult(result, len(rows) > limit)
        finally:
            db.close()

    def session_project(self, session: str, focus: ProjectHost, explicit: str = "") -> ProjectIdentity:
        from ..config.paths import state_db_path
        from ..session.db import StateDatabase
        db = StateDatabase(state_db_path(self.home))
        try:
            with db.read() as conn:
                rows = conn.execute("SELECT DISTINCT p.id,p.root FROM sessions s JOIN projects p "
                                    "ON p.id=s.project_id WHERE s.id=? LIMIT 33", (session,)).fetchall()
            rows = list(rows)
            known = {r[0] for r in rows}
            for key, host in self.hosts.items():
                if key not in known and host.runtime.sessions.exists(session):
                    rows.append((key, str(host.identity.root)))
            if explicit:
                rows = [r for r in rows if r[0] == explicit]
            if not rows:
                return self.identity(explicit) if explicit else focus.identity
            if len(rows) > 1:
                selected = [r for r in rows if r[0] == focus.identity.key]
                if not selected:
                    raise ValueError("Ambiguous session id; specify a project")
                rows = selected
            if rows[0][0] in self.hosts:
                return self.hosts[rows[0][0]].identity
            return resolve_project(rows[0][1])
        finally:
            db.close()
