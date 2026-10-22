"""Daemon lifecycle, socket, handshake, and shutdown (PLAN section 14.8).

The daemon always owns the :class:`~nexus.runtime.Runtime`; a surface is a pure
client with no in-process fallback. One daemon serves many project runtimes, addressed by
a deterministic socket path derived from the protocol and Python install identity. This module owns:

* **a deterministic socket path** — ``~/.nexus/daemon/shared-v5-<install>.sock``, mode ``0600``
  inside a ``0700`` directory;
* **duplicate prevention** — an exclusive ``flock`` on a sibling lock file plus a
  pid file, so a second daemon for the same home/install refuses to start;
* **stale-socket cleanup** — under the lock, a socket left by a dead daemon is
  removed and replaced rather than reported as an error;
* **a version handshake** — a client built against a different protocol revision
  is rejected loudly instead of speaking a half-understood protocol;
* **auto-start** — :func:`ensure_daemon` starts a daemon when the socket is absent
  and waits for readiness with a bounded timeout;
* **an idle shutdown policy** — exit after ``idle_timeout`` with no viewers, no
  running turn, and no queued turn; never with a turn in flight;
* **signals and graceful close** — ``SIGINT``/``SIGTERM`` stop the accept loop and
  close every session through the facade;
* **an opt-in HTTP/SSE surface** — when enabled, the daemon starts
  :class:`~nexus.host.transports.http_sse.HTTPSSEServer` over the *same*
  :class:`~nexus.host.facade.HostFacade` the Unix socket serves, so a terminal
  and an HTTP view are peers on one session. It is off by default, binds loopback
  only, requires a strong bearer token, and checks a strict ``Origin`` allowlist.
  The token is published in a mode-``0600`` discovery file colocated with the
  socket (inside the ``0700`` daemon directory), never logged and never included
  in status/health; the daemon removes the file when it stops.

Every log line and every error a peer can observe is passed through
:func:`~nexus.util.redact_secrets`, so a credential echoed by a provider or a
config repr cannot reach the log or the wire.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from ..config.paths import nexus_home, project_key
from ..errors import NexusError
from ..events import Event
from ..host_support.session_archive import sweep_stale_sessions
from ..host_support.socket_dir import (
    MAX_SOCKET_PATH_BYTES,
    ensure_private_dir,
)
from ..host_support.socket_dir import (
    fallback_daemon_dir as _fallback_daemon_dir,
)
from ..observability.daemon import DaemonDiagnostics
from ..util import new_id, redact_secrets
from . import protocol as p
from .facade import HostFacade
from .projects import ProjectRegistry, project_ref, validate_environment
from ..host_support.project_identity import resolve_project

from .transports import (
    Bye,
    CommandFrame,
    DaemonUnavailable,
    EventFrame,
    Hello,
    Ping,
    Pong,
    Reject,
    ResultFrame,
    SubscribeDone,
    TransportError,
    Unsubscribe,
    VersionMismatch,
    Welcome,
    read_frame,
    write_frame,
)
from .transports.http_sse import LOOPBACK_HOST, HTTPSSEServer
from .transports.uds import UDSClient

DEFAULT_MAX_CONCURRENT_TURNS = 32

#: Default idle window before a view-less, turn-less daemon exits.
DEFAULT_IDLE_TIMEOUT = 300.0
#: Default bound on auto-start readiness.
DEFAULT_AUTOSTART_TIMEOUT = 10.0
#: Default bound on the handshake (a peer that connects and never speaks).
DEFAULT_HANDSHAKE_TIMEOUT = 5.0
#: Bound on closing one accepted connection during shutdown: cancel its
#: subscriptions, close the writer, and wait for the transport. A wedged peer
#: (or a callback that never returns) must not stop the daemon from releasing
#: its exclusive lock.
TEARDOWN_TIMEOUT = 1.0
#: Longest the startup sweep of a killed daemon's shell process groups may take.
SHELL_RECOVERY_TIMEOUT_S = 8.0
#: Bound on concurrent subscriptions from one client, so a buggy client cannot
#: open unbounded streams.
MAX_SUBSCRIPTIONS_PER_CLIENT = 64
#: Commands that can take seconds on a cold daemon (Doctor scans the whole
#: SQLite file, the others may fetch the model catalogue or the network). They
#: run as background tasks so one slow request never holds up the connection's
#: next frame (a session open right behind the startup Doctor poll).
#: ``SettingsMcpAdd`` belongs here too: its hot reload starts the new server's subprocess.
SLOW_COMMANDS = tuple(
    getattr(p, name)
    for name in ("Doctor", "UpdateStatus", "ModelsRefresh", "ModelsList", "SetupStatus", "SkillsInstall", "SettingsMcpAdd")
    if hasattr(p, name)
)
MAX_SLOW_COMMANDS_PER_CLIENT = 8
#: The only host the opt-in HTTP/SSE surface may bind (PLAN section 14.12).
DEFAULT_HTTP_HOST = LOOPBACK_HOST

class DaemonError(NexusError, RuntimeError):
    """The daemon could not start, or a peer refused the connection."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def workspace_hash(workspace: str | Path) -> str:
    """A stable, filesystem-safe digest of the resolved workspace path.

    Delegates to :func:`nexus.config.paths.project_key` (STATE_PLAN §3) so the
    daemon socket name and the state database's project row always agree.
    """
    return project_key(workspace)


def daemon_dir(home: str | Path | None = None) -> Path:
    return nexus_home(home) / "daemon"


def _ensure_private_fallback_dir(path: Path) -> None:
    try:
        ensure_private_dir(path)
    except OSError as exc:
        raise DaemonError(f"cannot prepare daemon directory: {exc}") from exc


def default_socket_path(
    workspace: str | Path, *, home: str | Path | None = None
) -> Path:
    """The socket shared by this Nexus home, protocol and Python install."""
    install = hashlib.sha256(str(Path(sys.prefix).resolve()).encode()).hexdigest()[:8]
    filename = f"shared-v{p.PROTOCOL_VERSION}-{install}.sock"
    path = daemon_dir(home) / filename
    if len(str(path).encode("utf-8")) <= MAX_SOCKET_PATH_BYTES:
        return path

    fallback = _fallback_daemon_dir(home)
    _ensure_private_fallback_dir(fallback)
    path = fallback / filename
    if len(str(path).encode("utf-8")) > MAX_SOCKET_PATH_BYTES:
        raise DaemonError("daemon fallback socket path exceeds the Unix socket limit")
    return path


def _auxiliary(socket: Path, suffix: str) -> Path:
    """A pid/lock/log file colocated with the socket it belongs to."""
    return socket.with_name(socket.stem + suffix)


def default_pid_path(workspace: str | Path, *, home: str | Path | None = None) -> Path:
    return _auxiliary(default_socket_path(workspace, home=home), ".pid")


def default_lock_path(
    workspace: str | Path, *, home: str | Path | None = None
) -> Path:
    return _auxiliary(default_socket_path(workspace, home=home), ".lock")


def default_log_path(workspace: str | Path, *, home: str | Path | None = None) -> Path:
    return _auxiliary(default_socket_path(workspace, home=home), ".log")


def default_http_path(workspace: str | Path, *, home: str | Path | None = None) -> Path:
    """The discovery file a daemon publishes when its HTTP/SSE surface is on."""
    return _auxiliary(default_socket_path(workspace, home=home), ".http")


def read_http_endpoint(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read a running daemon's HTTP/SSE endpoint, or ``None`` when absent.

    The file holds the loopback host, the bound port, the bearer token, and the
    accepted origins. It lives beside the socket inside the daemon directory and
    must be owner-only; a file another user could read is treated as absent, so
    the token is never disclosed from an insecure location.
    """
    resolved = Path(workspace).expanduser().resolve()
    socket = (
        Path(socket_path)
        if socket_path is not None
        else default_socket_path(resolved, home=home)
    )
    path = _auxiliary(socket, ".http")
    try:
        if path.stat().st_mode & 0o077:
            return None
        data = msgspec.json.decode(path.read_bytes())
    except (OSError, msgspec.DecodeError, msgspec.ValidationError):
        return None
    if not isinstance(data, dict) or not data.get("token") or not data.get("port"):
        return None
    return data


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@dataclass
class _Subscription:
    project: str
    session: str
    task: asyncio.Task[None]


class _ClientConnection:
    """One accepted socket: handshake, command dispatch, and event streaming."""

    def __init__(self, daemon: Daemon, reader: Any, writer: Any) -> None:
        self.daemon = daemon
        self.reader = reader
        self.writer = writer
        self.client_id = new_id()
        self._subs: dict[str, _Subscription] = {}
        self.focus = None
        self.environment = {}
        self.client_name = ""
        self.initial_path = ""
        self.launch_path = None
        self._write_lock = asyncio.Lock()
        self._slow: set[asyncio.Task[None]] = set()
        self._closed = False
        self._torn_down = False

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        try:
            hello = await asyncio.wait_for(
                read_frame(self.reader), self.daemon.handshake_timeout
            )
            if not isinstance(hello, Hello):
                await self._send(Reject(reason="expected_hello"))
                return
            if hello.version != p.PROTOCOL_VERSION:
                await self._send(
                    Reject(
                        reason="version_mismatch",
                        expected=p.PROTOCOL_VERSION,
                        got=hello.version,
                    )
                )
                return
            try:
                if not hello.focus or not Path(hello.focus).is_absolute():
                    raise ValueError("Hello.focus must be an absolute project path")
                self.environment = validate_environment(hello.env)
                self.client_name = hello.client
                self.initial_path = hello.focus
                self.launch_path = Path(hello.focus).resolve()
                if hello.client != "control":
                    self.focus = await asyncio.wait_for(self.daemon.projects.acquire(
                        resolve_project(hello.focus), self.environment, hello.client), 10)
            except Exception as exc:
                await self._send(Reject(reason=redact_secrets(str(exc))))
                return
            if hello.client_id:
                self.client_id = hello.client_id
            await self._send(
                Welcome(
                    version=p.PROTOCOL_VERSION,
                    pid=os.getpid(),
                    workspace=str(self.focus.identity.root) if self.focus else self.initial_path,
                    focus=project_ref(self.focus.identity) if self.focus else None,
                    socket=str(self.daemon.socket),
                    server=_server_version(),
                    client_id=self.client_id,
                )
            )
            # ``aclose`` may have snapshotted and stopped accepting while this
            # handshake was in flight. Refusing the join here means a late
            # client is closed by the ``finally`` instead of left active.
            if not self.daemon._client_joined(self):
                return
            await self._loop()
        except (TimeoutError, TransportError, OSError) as exc:
            self.daemon._log("connection_error", error=redact_secrets(str(exc)))
        except asyncio.CancelledError:
            raise
        finally:
            await self._teardown()

    async def _loop(self) -> None:
        while not self._closed:
            frame = await read_frame(self.reader)
            if frame is None:
                break
            self.daemon.touch()
            if isinstance(frame, CommandFrame):
                await self._command(frame)
            elif isinstance(frame, Unsubscribe):
                self._stop_sub(frame.id)
            elif isinstance(frame, Ping):
                await self._send(Pong(nonce=frame.nonce))
            elif isinstance(frame, Bye):
                break
            else:
                await self._send(Reject(reason=f"unexpected_frame:{type(frame).__name__}"))
                break

    async def _route(self, command):
        projects = self.daemon.projects
        if isinstance(command, p.Health):
            totals = {"sessions": 0, "running": 0, "queued": 0, "viewers": 0,
                      "persistent_jobs_total": 0}
            jobs = []
            for host in list(projects.hosts.values()):
                health = host.facade.health()
                for key in totals:
                    totals[key] += health[key]
                jobs.extend(health["persistent_jobs"])
            return p.HealthResult(**totals, persistent_jobs=jobs[:32],
                                  max_concurrent=projects.supervisor.max_concurrent,
                                  projects=projects.loaded_rows())
        if isinstance(command, p.UpdateStatus):
            return await self.daemon.update_status(command.announce)
        if isinstance(command, p.ProjectList):
            return projects.rows(command.limit)
        if isinstance(command, p.Shutdown):
            if (projects.supervisor.running or projects.supervisor.queued) and not command.force:
                raise ValueError("Projects have running or queued turns; use --force")
            self.daemon.request_stop("client shutdown")
            return p.ShutdownResult()
        if isinstance(command, (p.ProjectFocus, p.ProjectOpen)):
            target = command.path if isinstance(command, p.ProjectOpen) else command.project
            identity = projects.identity(target)
            existed = projects.registered(identity.key)
            host = await projects.acquire(identity, self.environment, self.client_name)
            if isinstance(command, p.ProjectOpen):
                projects.release(host)
                return p.ProjectOpenResult(project_ref(identity), not existed)
            if self.focus is not None:
                projects.release(self.focus)
            self.focus = host
            self.launch_path = identity.launch_root if Path(target).is_absolute() else identity.root
            return p.ProjectFocusResult(project_ref(identity))
        if isinstance(command, (p.ProjectUnload, p.ProjectReload)):
            identity = projects.identity(command.project or self.initial_path) if self.focus is None else (projects.identity(command.project) if command.project else self.focus.identity)
            host = projects.hosts.get(identity.key)
            # This administrative connection's focus is not an active viewer.
            if host is not None and host is self.focus:
                projects.release(host)
            try:
                await projects.unload(identity.key, force=getattr(command, "force", False))
            except BaseException:
                if host is self.focus:
                    host.references += 1
                raise
            if isinstance(command, p.ProjectReload):
                replacement = await projects.acquire(identity, self.environment, self.client_name)
                if self.focus is None or host is self.focus:
                    self.focus = replacement
                else:
                    projects.release(replacement)
            return p.ProjectFocusResult(project_ref(identity))
        if isinstance(command, p.WebLaunch):
            return p.WebLaunchResult(url=await self.daemon.web_launch(self.focus))
        if isinstance(command, p.ProjectSessionOpen):
            target = resolve_project(command.workspace)
            identity = projects.session_project(command.session, self.focus, target.key)
            host = await projects.acquire(identity, self.environment, self.client_name)
            try:
                opened = await host.facade.handle(p.SessionOpen(session=command.session, create=False, recover=True))
                if isinstance(opened, p.ErrorResult):
                    projects.release(host)
                    return opened
            except BaseException:
                projects.release(host)
                raise
            if self.focus is not None:
                projects.release(self.focus)
            self.focus = host
            self.launch_path = identity.root
            return p.ProjectSessionOpenResult(project=project_ref(identity), session=command.session)
        if self.focus is None:
            raise ValueError("Control connections require an explicit project operation")
        session = getattr(command, "session", "") or getattr(command, "session_id", "")
        explicit = getattr(command, "project", "")
        identity = (projects.session_project(session, self.focus, explicit) if session else
                    projects.identity(explicit) if explicit else self.focus.identity)
        if not identity.root.is_dir() and identity.key not in projects.hosts:
            raise ValueError("Project root is missing")
        host = await projects.acquire(identity, self.environment, self.client_name)
        self.daemon._last_routed_project = identity.key
        try:
            host.facade.daemon_info.update({"pid": os.getpid(), "socket": str(self.daemon.socket)})
            fresh = (isinstance(command, p.SessionOpen) and command.create
                     and not host.runtime.sessions.exists(command.session))
            if fresh:
                from ..errors import SessionError
                try:
                    host.runtime.sessions.resolve(command.session, namespace="main")
                except SessionError:
                    pass
                else:
                    fresh = False
            launch = self.launch_path if host is self.focus else None
            if fresh and not command.root and launch is not None:
                launch_identity = resolve_project(launch)
                if launch_identity.key == identity.key and launch_identity.launch_worktree:
                    command = msgspec.structs.replace(command, root=launch_identity.launch_worktree)
            result = await host.facade.handle(command)
            if fresh and isinstance(result, p.SessionOpenResult) and launch is not None:
                host.runtime.initialize_session_cwd(result.session.id, launch)
            return result
        finally:
            projects.release(host)

    async def _command(self, frame: CommandFrame) -> None:
        if (isinstance(frame.command, SLOW_COMMANDS)
                and len(self._slow) < MAX_SLOW_COMMANDS_PER_CLIENT):
            task = asyncio.ensure_future(self._slow_command(frame))
            self._slow.add(task)
            task.add_done_callback(self._slow.discard)
            return
        await self._slow_command(frame)

    async def _slow_command(self, frame: CommandFrame) -> None:
        try:
            result = await self._route(frame.command)
        except Exception as exc:
            result = p.ErrorResult(kind=type(exc).__name__, message=redact_secrets(str(exc)))
        subscription_host = None
        if isinstance(frame.command, p.SessionSubscribe) and isinstance(result, p.SessionSubscribeResult):
            identity = self.daemon.projects.session_project(frame.command.session, self.focus, frame.command.project)
            subscription_host = self.daemon.projects.hosts[identity.key]
            subscription_host.references += 1
        try:
            await self._send(ResultFrame(id=frame.id, result=result))
            if subscription_host is not None:
                self._start_sub(frame.id, frame.command.session, frame.command.from_seq, frame.command.follow,
                                subscription_host)
        finally:
            if subscription_host is not None:
                self.daemon.projects.release(subscription_host)
        self.daemon.touch()

    # -- subscriptions -----------------------------------------------------

    def _start_sub(self, sub_id: str, session: str, from_seq: int, follow: bool, host) -> None:
        if len(self._subs) >= MAX_SUBSCRIPTIONS_PER_CLIENT:
            asyncio.ensure_future(
                self._send(SubscribeDone(id=sub_id, error="subscription limit reached"))
            )
            return
        self._stop_sub(sub_id)
        host.references += 1
        task = asyncio.ensure_future(self._stream(sub_id, session, from_seq, follow, host))
        self._subs[sub_id] = _Subscription(project=host.identity.key, session=session, task=task)
        def finished(done):
            self.daemon.projects.release(host)
            current = self._subs.get(sub_id)
            if current is not None and current.task is done:
                self._subs.pop(sub_id, None)
        task.add_done_callback(finished)

    async def _stream(
        self, sub_id: str, session: str, from_seq: int, follow: bool, focus
    ) -> None:
        error = ""
        host = focus
        try:
            if follow:
                # Reconnect-safe: a (re)attached follower learns whether this
                # session is parked on the global turn cap, not only from events.
                await self._send(EventFrame(id=sub_id, event=Event(
                    type="daemon.session_slot", session=session,
                    data={"session": session,
                          "slot_wait": host.facade.supervisor.slot_wait(session)}), project=host.identity.key))
            async for event in host.facade.subscribe(
                session, from_seq, follow=follow, client_id=self.client_id
            ):
                await self._send(EventFrame(id=sub_id, event=event, project=host.identity.key))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad stream ends itself
            error = redact_secrets(f"{type(exc).__name__}: {exc}")
            self.daemon._log("subscription_error", session=session, error=error)
        finally:
            self.daemon.touch()
        if not self._closed:
            await self._send(SubscribeDone(id=sub_id, error=error))

    def _stop_sub(self, sub_id: str) -> None:
        subscription = self._subs.pop(sub_id, None)
        if subscription is not None:
            subscription.task.cancel()

    async def deliver(self, event: Event) -> None:
        """Fan one daemon-level event out to every matching subscription."""
        for sub_id, subscription in list(self._subs.items()):
            project = event.data.get("project", "")
            if project and project != subscription.project:
                continue
            if event.session is None or event.session == subscription.session:
                await self._send(EventFrame(id=sub_id, event=event, project=subscription.project))

    # -- writes ------------------------------------------------------------

    async def _send(self, frame: Any) -> None:
        if self._closed:
            return
        async with self._write_lock:
            try:
                await write_frame(self.writer, frame)
            except (TransportError, OSError):
                self._closed = True

    async def _teardown(self) -> None:
        """Close this connection: cancel streams, drop registration, close writer.

        Bounded and idempotent. A wedged peer (or a subscription whose task
        ignores cancellation) must not stop the daemon from releasing its lock,
        so both the subscription wait and ``wait_closed`` are time-boxed. Safe
        to call from both ``run``'s ``finally`` and :meth:`Daemon.aclose`.
        """
        self._closed = True
        if self._torn_down:
            return
        self._torn_down = True
        tasks = [subscription.task for subscription in self._subs.values()]
        tasks.extend(self._slow)
        self._slow.clear()
        self._subs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            # Wait as a group rather than awaiting each task: a task that
            # suppresses ``CancelledError`` would otherwise pin teardown.
            _done, pending = await asyncio.wait(tasks, timeout=TEARDOWN_TIMEOUT)
            for task in pending:
                self.daemon._log(
                    "connection_task_timeout", client=self.client_id
                )
        if self.focus is not None:
            self.daemon.projects.release(self.focus)
            self.focus = None
        self.daemon._client_left(self)
        with contextlib.suppress(Exception):
            self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), TEARDOWN_TIMEOUT)
        except TimeoutError:
            self.daemon._log("connection_close_timeout", client=self.client_id)
        except Exception:  # noqa: BLE001, S110 - closing an already-failed peer is best-effort
            pass


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


RuntimeFactory = Callable[..., Any]
Spawn = Callable[..., "subprocess.Popen[bytes]"]


def _http_requested(
    http: bool | None, environ: Mapping[str, str] | None
) -> bool:
    """Resolve the opt-in flag: explicit wins, otherwise ``NEXUS_HTTP``."""
    if http is not None:
        return bool(http)
    env = environ if environ is not None else os.environ
    return str(env.get("NEXUS_HTTP", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class Daemon:
    """Own one socket and a bounded registry of project runtimes."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        home: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        socket_path: str | Path | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        max_concurrent_turns: int = DEFAULT_MAX_CONCURRENT_TURNS,
        runtime_factory: RuntimeFactory | None = None,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        reap_interval: float | None = None,
        http: bool | None = None,
        http_host: str = DEFAULT_HTTP_HOST,
        http_port: int = 0,
        http_origins: Iterable[str] | None = None,
        http_token: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._home = home
        self._environ = environ
        self._socket = (
            Path(socket_path)
            if socket_path is not None
            else default_socket_path(self.workspace, home=home)
        )
        # Auxiliary files are always colocated with the socket, so an explicit
        # short socket path (tests, an embedder) keeps them short too and a
        # workspace's daemon state lives in exactly one directory.
        self._pid_file = _auxiliary(self._socket, ".pid")
        self._lock_file = _auxiliary(self._socket, ".lock")
        self._log_file = _auxiliary(self._socket, ".log")
        self._http_file = _auxiliary(self._socket, ".http")
        self.idle_timeout = float(idle_timeout)
        self.max_concurrent_turns = int(max_concurrent_turns)
        self.handshake_timeout = float(handshake_timeout)
        self._runtime_factory = runtime_factory or _default_runtime_factory
        from ..resources import ResourceLimits
        from ..config.schema import DaemonSection, UpdatesSection
        import tomllib
        config_path = nexus_home(home) / "config.toml"
        settings = DaemonSection()
        document = {}
        if config_path.is_file():
            with config_path.open("rb") as handle:
                raw = handle.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise DaemonError("User config exceeds 256 KiB")
            document = tomllib.loads(raw.decode())
            settings = msgspec.convert(document.get("daemon", {}), type=DaemonSection)
        self._update_check = msgspec.convert(document.get("updates", {}), type=UpdatesSection).check
        self.projects = ProjectRegistry(self._runtime_factory, home=home,
            limits=ResourceLimits(max_processes=settings.max_processes,
                                  max_requests_per_provider=settings.max_requests_per_provider),
                                        max_concurrent=max_concurrent_turns, emit=self._emit)
        self._reap_interval = reap_interval or max(
            0.05, min(1.0, self.idle_timeout / 2) if self.idle_timeout > 0 else 1.0
        )
        # HTTP/SSE is opt-in and off unless explicitly requested. ``None`` defers
        # to ``NEXUS_HTTP`` so an auto-started daemon inherits an embedder's env.
        self.http_enabled = _http_requested(http, environ)
        self.http_host = http_host
        self._http_port = int(http_port)
        self._http_origins = (
            tuple(http_origins) if http_origins is not None else None
        )
        self._http_token = http_token
        self._web_start_lock = asyncio.Lock()
        self.diagnostics = DaemonDiagnostics()

        self._server: asyncio.AbstractServer | None = None
        self._http: HTTPSSEServer | None = None
        self._lock_fd: int | None = None
        self._bound = False
        self._clients: set[_ClientConnection] = set()
        self._stop_event = asyncio.Event()
        self._reaper: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._last_routed_project = ""
        self._last_activity = time.time()
        self._started_at = 0.0
        self._closed = False

    # -- introspection -----------------------------------------------------

    @property
    def socket(self) -> Path:
        return self._socket

    @property
    def pid_file(self) -> Path:
        return self._pid_file

    @property
    def log_file(self) -> Path:
        return self._log_file

    @property
    def http_file(self) -> Path:
        """The discovery file that publishes the HTTP/SSE endpoint when enabled."""
        return self._http_file

    @property
    def facade(self) -> HostFacade:
        if self.projects.hosts:
            return next(iter(self.projects.hosts.values())).facade
        raise DaemonError("no project is loaded")

    @property
    def started(self) -> bool:
        return self._server is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def http_port(self) -> int:
        """The bound HTTP/SSE port, or ``0`` when the surface is not running."""
        return self._http.port if self._http is not None else 0

    async def update_status(self, announce=False):
        from ..host_support.update_check import update_status, claim_announcement
        status = await asyncio.to_thread(update_status, home=self._home,
                                         config_enabled=self._update_check)
        if announce:
            status["announce"] = await asyncio.to_thread(claim_announcement, status.get("available"), home=self._home)
        return p.UpdateStatusResult(**status)

    async def web_launch(self, host=None) -> str:
        """Mint a ticket bound to one project's cookie and URL namespace."""
        if self._closed:
            raise DaemonError("daemon is stopping")
        if host is None:
            host = next(iter(self.projects.hosts.values()), None)
        if host is None:
            raise DaemonError("No project is loaded")
        async with self._web_start_lock:
            if self._http is None:
                await self._start_http()
            from .web import BrowserRoutes
            key = host.identity.key
            web = self._http.project_web.get(key)
            if web is None:
                web = BrowserRoutes(host.facade, workspace=str(host.identity.root),
                                    port=self._http.port, prefix=f"/w/{key}", project_open=self._browser_project_open)
                self._http.project_web[key] = web
            ticket = web.issue_ticket()
            return f"http://127.0.0.1:{self._http.port}/w/{key}/#ticket={ticket}"

    async def _browser_project_open(self, command):
        identity = resolve_project(command.workspace)
        host = self.projects.hosts.get(identity.key)
        if host is None:
            raise ValueError("Open this project with nexus web before navigating to it")
        opened = await host.facade.handle(p.SessionOpen(session=command.session, create=False, recover=True))
        if isinstance(opened, p.ErrorResult):
            return opened
        url = await self.web_launch(host)
        base, ticket = url.split("/#", 1)
        return p.ProjectSessionOpenResult(project_ref(identity), command.session,
                                         url=f"{base}/s/{command.session}#{ticket}")

    @property
    def http_endpoint(self) -> dict[str, Any] | None:
        """The running HTTP/SSE endpoint (host, port, token, origins), or ``None``.

        This is the in-process counterpart of the discovery file; it is never
        folded into health or status, so the token never crosses that channel.
        """
        if self._http is None:
            return None
        return {
            "host": self._http.host,
            "port": self._http.port,
            "token": self._http.token,
            "origins": sorted(self._http.origins),
        }

    def health(self) -> dict[str, Any]:
        base = {
            "pid": os.getpid(),
            "workspace": str(self.workspace),
            "socket": str(self._socket),
            "clients": len(self._clients),
            "idle_timeout": self.idle_timeout,
            "uptime": max(0.0, time.time() - self._started_at) if self._started_at else 0.0,
            "http": self._http is not None,
        }
        base["projects"] = self.projects.loaded_rows()
        base["running"] = self.projects.supervisor.running
        base["queued"] = self.projects.supervisor.queued
        return base

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Acquire the lock, bind the socket, and build the runtime + facade."""
        if self._server is not None:
            return
        self._prepare_dir()
        self._acquire_lock()
        try:
            self._reclaim_stale()
            # Project construction is lazy: bad config in one project must not
            # prevent the process listener from serving other projects.
            self._server = await asyncio.start_unix_server(
                self._on_connection, path=str(self._socket)
            )
            self._bound = True
            os.chmod(self._socket, 0o600)
            self._write_pid()
            if self.http_enabled:
                await self._start_http()
            self._started_at = time.time()
            self._last_activity = self._started_at
            self._stop_event = asyncio.Event()
            self._log("daemon.started", pid=os.getpid(), socket=str(self._socket))
            self._reaper = asyncio.ensure_future(self._reap())
            self._watchdog = asyncio.create_task(self._watch_loop_lag())
        except BaseException:
            # Close a partially-bound listener before releasing the lock so a
            # failed start (for example a refused HTTP bind) leaks no fd.
            if self._server is not None:
                self._server.close()
                with contextlib.suppress(Exception):
                    await self._server.wait_closed()
                self._server = None
            if self._http is not None:
                with contextlib.suppress(Exception):
                    await self._http.aclose()
                self._http = None
            await self.projects.aclose()
            await self._release()
            raise

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.aclose()

    def run(self) -> int:
        """Blocking entrypoint used by the module and by auto-start."""
        try:
            asyncio.run(self._serve_with_signals())
            return 0
        except DaemonError as exc:
            print(redact_secrets(str(exc)), file=sys.stderr)
            return 3
        except KeyboardInterrupt:
            return 130

    async def _serve_with_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.request_stop, f"signal:{sig.name}")
        await self.serve_forever()

    def request_stop(self, reason: str = "") -> None:
        if not self._stop_event.is_set():
            if reason:
                self._log("daemon.stopping", reason=reason)
            self._stop_event.set()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._log("daemon.stopping")
        try:
            await self._shutdown()
        finally:
            # ``_release`` closes the lock fd and removes the socket/pid. It
            # must run even if a connection teardown raised or this coroutine
            # was cancelled mid-shutdown; otherwise the exclusive ``flock``
            # leaks and the next start fails with "another daemon already
            # owns ...". ``_release`` never awaits, so it cannot be interrupted.
            await self._release()
            self._log("daemon.stopped")

    async def _shutdown(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
            self._reaper = None
        if self._server is not None:
            # Stop accepting first, but do not await ``wait_closed`` yet: since
            # Python 3.12 ``Server.wait_closed`` does not return until every
            # accepted connection has ended, and an idle peer (for example a
            # ``chat`` shell whose socket is still open) would otherwise pin
            # shutdown forever. Tear the live connections down next, *then* the
            # listener can reap them and report closed.
            self._server.close()
        # Close the HTTP surface before the facade it wraps; it owns only its own
        # sockets, and the facade stays the daemon's to shut down.
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None
        # Snapshot then clear before tearing down: a handshake landing during
        # teardown is refused by ``_client_joined`` and cannot slip into a set
        # that is about to be dropped.
        connections = list(self._clients)
        self._clients.clear()
        for connection in connections:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    connection._teardown(), 2 * TEARDOWN_TIMEOUT
                )
        if self._server is not None:
            # Connections are gone, so this is immediate; bounded anyway so a
            # wedged transport cannot stop the daemon from releasing its lock.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._server.wait_closed(), timeout=TEARDOWN_TIMEOUT
                )
            self._server = None
        await self.projects.aclose()
    # -- connections -------------------------------------------------------

    async def _on_connection(self, reader: Any, writer: Any) -> None:
        await _ClientConnection(self, reader, writer).run()

    def _client_joined(self, connection: _ClientConnection) -> bool:
        """Register a handshaked connection, unless the daemon is closing.

        A handshake can complete after :meth:`aclose` has snapshotted the
        clients and stopped accepting. Refuse the late join (return ``False``)
        so ``run`` closes the connection rather than leaving it active after
        the lock is gone.
        """
        if self._closed:
            return False
        self._clients.add(connection)
        self.touch()
        return True

    def _client_left(self, connection: _ClientConnection) -> None:
        self._clients.discard(connection)
        self.touch()

    # -- idle policy -------------------------------------------------------

    async def _watch_loop_lag(self):
        while True:
            started = time.monotonic()
            await asyncio.sleep(0.5)
            lag = time.monotonic() - started - 0.5
            if lag > 1:
                # Attribution is a hint: arbitrary extension callbacks can run
                # outside a command, so never claim this identifies the offender.
                self._log("daemon.loop_lag", lag_seconds=round(lag, 3),
                          last_routed_project=self._last_routed_project)

    async def _reap(self) -> None:
        next_archive = 0.0
        while not self._stop_event.is_set():
            await asyncio.sleep(self._reap_interval)
            await self.projects.evict_idle(self.idle_timeout)
            if time.monotonic() >= next_archive:
                try:
                    await self._archive_sweep()
                except Exception as exc:
                    self._log("daemon.archive_failed", error=redact_secrets(str(exc)))
                next_archive = time.monotonic() + 3600
            if self._idle():
                self.request_stop("idle")

    async def _archive_sweep(self) -> None:
        """Best-effort startup/hourly auto-archive; never blocks request handling."""
        for host in list(self.projects.hosts.values()):
            sessions = getattr(host.runtime, "sessions", None)
            idle_seconds = getattr(host.runtime, "auto_archive_idle_seconds", None)
            if sessions is not None and callable(idle_seconds):
                count = await sweep_stale_sessions(sessions, idle_seconds)
                if count:
                    self._log("sessions.auto_archived", project=host.identity.key, count=count)

    def _idle(self) -> bool:
        if self.idle_timeout <= 0 or self._clients or self.projects.hosts:
            self.touch()
            return False
        return (time.time() - self._last_activity) >= self.idle_timeout

    def touch(self) -> None:
        self._last_activity = time.time()

    # -- daemon events -----------------------------------------------------

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Supervisor sink: log and fan a ``daemon.*`` event to subscribers."""
        data = dict(data)
        session = data.get("session", "")
        if ":" in session:
            project, logical = session.split(":", 1)
            data.update(project=project, session=logical)
        safe = {key: _redact_value(value) for key, value in data.items()}
        self._log(event_type, **safe)
        event = Event(type=event_type, data=safe, session=safe.get("session"))
        for connection in list(self._clients):
            with contextlib.suppress(RuntimeError):
                asyncio.ensure_future(connection.deliver(event))

    # -- lock / files ------------------------------------------------------

    def _prepare_dir(self) -> None:
        fallback_dir = _fallback_daemon_dir(self._home)
        if self._socket.parent == fallback_dir:
            # Revalidate at startup immediately before any lock/socket access;
            # default_socket_path may have been computed well before this point.
            _ensure_private_fallback_dir(fallback_dir)
        for path in (
            self._socket,
            self._pid_file,
            self._lock_file,
            self._log_file,
            self._http_file,
        ):
            parent = path.parent
            existed = parent.exists()
            parent.mkdir(parents=True, exist_ok=True)
            # Only harden a directory this daemon created. Touching the mode of
            # a pre-existing directory would let an explicit socket path chmod
            # ``/tmp`` or the user's home, which is never acceptable.
            if not existed:
                with contextlib.suppress(OSError):
                    os.chmod(parent, 0o700)

    def _acquire_lock(self) -> None:
        import fcntl

        fd = os.open(self._lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise DaemonError(
                f"another daemon already owns {self.workspace}"
            ) from exc
        self._lock_fd = fd

    def _reclaim_stale(self) -> None:
        """Under the lock, replace a socket/pid left by a dead daemon."""
        pid = self._read_pid()
        if pid is not None and _pid_alive(pid):
            raise DaemonError(
                f"daemon pid {pid} is alive but does not hold the lock"
            )
        for path in (self._socket, self._pid_file, self._http_file):
            with contextlib.suppress(FileNotFoundError, OSError):
                path.unlink()

    def _write_pid(self) -> None:
        payload = msgspec.json.encode(
            {
                "pid": os.getpid(),
                "workspace": str(self.workspace),
                "socket": str(self._socket),
                "started": time.time(),
            }
        )
        self._pid_file.write_bytes(payload + b"\n")

    def _read_pid(self) -> int | None:
        try:
            data = msgspec.json.decode(self._pid_file.read_bytes())
        except (OSError, msgspec.DecodeError, msgspec.ValidationError):
            return None
        pid = data.get("pid") if isinstance(data, dict) else None
        return int(pid) if isinstance(pid, int) else None

    # -- HTTP/SSE surface --------------------------------------------------

    def _http_project(self, command):
        hosts = self.projects.hosts
        if not hosts:
            raise ValueError("Open the project over the local socket first")
        focus = next(iter(hosts.values()))
        explicit = getattr(command, "project", "")
        session = getattr(command, "session", "") or getattr(command, "session_id", "")
        if session:
            identity = self.projects.session_project(session, focus, explicit)
            key = identity.key
        elif explicit:
            key = explicit
        elif len(hosts) == 1:
            key = focus.identity.key
        else:
            raise ValueError("Specify the project key")
        if key not in hosts:
            raise ValueError("Open the project over the local socket first")
        return hosts[key]

    async def handle(self, command):
        try:
            if isinstance(command, p.UpdateStatus):
                return await self.update_status(command.announce)
            if isinstance(command, p.Health):
                rows = self.projects.loaded_rows()
                return p.HealthResult(projects=rows,
                    running=self.projects.supervisor.running,
                    queued=self.projects.supervisor.queued,
                    max_concurrent=self.projects.supervisor.max_concurrent,
                    sessions=sum(row.sessions for row in rows))
            if isinstance(command, p.Shutdown):
                if (self.projects.supervisor.running or self.projects.supervisor.queued) and not command.force:
                    raise ValueError("Projects have running or queued turns; use --force")
                self.request_stop("HTTP shutdown")
                return p.ShutdownResult()
            host = self._http_project(command)
            return await host.facade.handle(command)
        except Exception as exc:
            return p.ErrorResult(kind=type(exc).__name__, message=redact_secrets(str(exc)))

    async def subscribe(self, session, from_seq=0, **kwargs):
        host = self._http_project(p.SessionSubscribe(session=session))
        host.references += 1
        try:
            async for event in host.facade.subscribe(session, from_seq, **kwargs):
                yield event
        finally:
            self.projects.release(host)

    async def _start_http(self) -> None:
        """Start the opt-in HTTP/SSE surface over the shared facade.

        The surface is loopback-only by construction; a non-loopback host or a
        failed bind aborts the whole daemon start rather than silently leaving
        the requested surface absent. The token is generated fresh (or taken
        from the caller) and published only in the owner-only discovery file.
        """
        token = self._http_token or secrets.token_urlsafe(32)
        self._http_token = token
        try:
            server = HTTPSSEServer(
                self,
                host=self.http_host,
                port=self._http_port,
                token=token,
                allowed_origins=self._http_origins,
                on_shutdown=self.request_stop,
                web_workspace="",
            )
            await server.start()
        except (ValueError, TransportError, OSError) as exc:
            raise DaemonError(
                f"HTTP/SSE surface failed to start: {redact_secrets(str(exc))}"
            ) from exc
        self._http = server
        self.projects.on_unload = lambda key: server.project_web.pop(key, None)
        self._write_http_endpoint()
        # The token is deliberately absent from the log; only the address is.
        self._log(
            "daemon.http_started",
            host=server.host,
            port=server.port,
            origins=len(server.origins),
        )

    def _write_http_endpoint(self) -> None:
        if self._http is None:
            return
        payload = msgspec.json.encode(
            {
                "host": self._http.host,
                "port": self._http.port,
                "token": self._http_token,
                "origins": sorted(self._http.origins),
                "pid": os.getpid(),
                "workspace": str(self.workspace),
            }
        )
        # Create owner-only in one step: a chmod after a default-mode create
        # leaves a window in which the bearer token is world-readable.
        fd = os.open(
            self._http_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload + b"\n")
        with contextlib.suppress(OSError):
            os.chmod(self._http_file, 0o600)

    async def _release(self) -> None:
        # Only remove the socket/pid this daemon actually created; a duplicate
        # that failed to acquire the lock must never delete a live daemon's files.
        if self._bound:
            # The socket goes last: observers treat its absence as "fully
            # stopped", so the pid and token files must already be gone.
            for path in (self._http_file, self._pid_file, self._socket):
                with contextlib.suppress(FileNotFoundError, OSError):
                    path.unlink()
            self._bound = False
        if self._lock_fd is not None:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None

    # -- logging -----------------------------------------------------------

    def _log(self, message: str, **fields: Any) -> None:
        # The diagnostics store independently selects reviewed categories and
        # fields; keep the owner log's existing raw-field/redaction behavior.
        self.diagnostics.capture(message, fields)
        host = self.projects.hosts.get(fields.get("project", ""))
        if host is not None:
            host.facade.daemon_diagnostics.capture(message, fields)
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}"
        if fields:
            pairs = " ".join(f"{key}={value!r}" for key, value in fields.items())
            line = f"{line} {pairs}"
        line = redact_secrets(line)
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            with self._log_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Auto-start and control helpers
# ---------------------------------------------------------------------------


def _default_runtime_factory(
    workspace: Path, *, home: Any = None, environ: Any = None, **_: Any
) -> Any:
    from ..runtime import Runtime

    return Runtime(workspace, home=home, environ=environ)


def _server_version() -> str:
    try:
        from importlib.metadata import version

        return version("nexus-harness")
    except Exception:  # noqa: BLE001 - informational only
        return "0.0.0"


def _duration_text(seconds: float) -> str:
    """``3600`` -> ``1 h``, ``120`` -> ``2 min``, else seconds (for recorded reasons)."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{seconds:g} s"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    return value


#: ``daemon.err`` never grows past this (it is reset on the next start beyond it).
_EARLY_OUTPUT_MAX_BYTES = 64 * 1024
_EARLY_OUTPUT_TAIL_BYTES = 4096


def _default_spawn(
    workspace: Path,
    path: Path,
    *,
    home: Any = None,
    idle_timeout: float | None = None,
    max_concurrent_turns: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    argv = [
        sys.executable,
        "-m",
        "nexus.host.daemon",
        "--workspace",
        str(workspace),
        "--socket",
        str(path),
    ]
    if home is not None:
        argv += ["--home", str(home)]
    if idle_timeout is not None:
        argv += ["--idle-timeout", str(idle_timeout)]
    if max_concurrent_turns is not None:
        argv += ["--max-concurrent-turns", str(max_concurrent_turns)]
    env = {**os.environ, **dict(environ or {})}
    # A crash before the daemon's own log opens (bad install, import error)
    # would otherwise leave nothing behind, so its stdout/stderr go to a small
    # file beside the socket; a failed start quotes the tail of it.
    early = _early_output_path(path)
    try:
        if early.stat().st_size > _EARLY_OUTPUT_MAX_BYTES:
            early.unlink()
    except OSError:
        pass
    try:
        sink: Any = os.open(early, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        sink = subprocess.DEVNULL
    nexus_home(home).mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
            cwd=str(nexus_home(home)),
            env=env,
        )
    finally:
        if sink is not subprocess.DEVNULL:
            os.close(sink)


def _early_output_path(socket: Path) -> Path:
    return _auxiliary(socket, ".err")


def _early_output_tail(socket: Path) -> str:
    """The redacted last lines a failed daemon start wrote, or ``""``."""
    try:
        with open(_early_output_path(socket), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _EARLY_OUTPUT_TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return redact_secrets("\n".join(lines[-6:]))


async def ensure_daemon(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
    socket_path: str | Path | None = None,
    client_id: str | None = None,
    version: int = p.PROTOCOL_VERSION,
    client: str = "",
    timeout: float = DEFAULT_AUTOSTART_TIMEOUT,
    idle_timeout: float | None = None,
    max_concurrent_turns: int | None = None,
    environ: Mapping[str, str] | None = None,
    spawn: Spawn | None = None,
    http: bool | None = None,
) -> UDSClient:
    """Return a connected client, starting a daemon if none is listening.

    Readiness is bounded by ``timeout``; a daemon that exits or never becomes
    ready raises :class:`DaemonUnavailable`. A version mismatch is never retried
    — it is a build problem, not a race. ``http`` is the opt-in switch for the
    HTTP/SSE surface on an auto-started daemon; ``None`` leaves the daemon's own
    default (off) and its ``NEXUS_HTTP`` environment untouched.
    """
    resolved = Path(workspace).expanduser().resolve()
    path = (
        Path(socket_path)
        if socket_path is not None
        else default_socket_path(resolved, home=home)
    )
    if http is not None:
        environ = {**dict(environ or {}), "NEXUS_HTTP": "1" if http else "0"}
    try:
        return await UDSClient.connect(
            path,
            client_id=client_id,
            version=version,
            client=client,
            focus=str(resolved), environ=dict(os.environ if environ is None else environ),
            timeout=min(timeout, 2.0),
        )
    except VersionMismatch:
        raise
    except (TransportError, OSError):
        # A missing socket, a refused connect, or a daemon that dies mid-
        # handshake all mean "start one"; only a version mismatch is terminal.
        pass

    # Never unlink a socket we did not create: a concurrent cold start may have
    # just bound it (before it is accepting), and a live daemon that merely timed
    # out must not be disturbed. A genuinely stale socket is reclaimed by the
    # daemon itself, under the exclusive lock, in ``Daemon._reclaim_stale``.
    launcher = spawn or _default_spawn
    process = launcher(
        resolved,
        path,
        home=home,
        idle_timeout=idle_timeout,
        max_concurrent_turns=max_concurrent_turns,
        environ=environ,
    )
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            return await UDSClient.connect(
                path, client_id=client_id, version=version, client=client, timeout=1.0,
                focus=str(resolved), environ=dict(os.environ if environ is None else environ)
            )
        except VersionMismatch:
            raise
        except (TransportError, OSError) as exc:
            last_error = exc
        expired = time.monotonic() >= deadline
        if process.poll() is not None:
            # Our own child is gone: it either failed outright or lost the
            # duplicate-daemon race to a concurrent starter. Keep polling until
            # the deadline so a winner that is still coming up can be adopted;
            # only surface the failure once time runs out.
            if expired:
                tail = _early_output_tail(path)
                raise DaemonUnavailable(
                    f"daemon exited with code {process.returncode} "
                    f"before readiness: {last_error}"
                    + (f"\ndaemon output:\n{tail}" if tail else "")
                )
        elif expired:
            with contextlib.suppress(Exception):
                process.terminate()
            raise DaemonUnavailable(
                f"daemon did not become ready within {timeout}s: {last_error}"
            )
        await asyncio.sleep(0.05)


async def status(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
    socket_path: str | Path | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Report whether a daemon is running for ``workspace`` and its health."""
    resolved = Path(workspace).expanduser().resolve()
    path = (
        Path(socket_path)
        if socket_path is not None
        else default_socket_path(resolved, home=home)
    )
    try:
        client = await UDSClient.connect(path, timeout=timeout, focus=str(resolved), environ=dict(os.environ), client="control")
    except (DaemonUnavailable, VersionMismatch, TransportError, OSError) as exc:
        return {
            "running": False,
            "socket": str(path),
            "error": redact_secrets(str(exc)),
        }
    try:
        try:
            result = await client.call(p.Health(), timeout=timeout)
        except (DaemonUnavailable, TransportError, OSError, asyncio.TimeoutError) as exc:
            # A daemon that is shutting down still accepts a connection and then
            # drops it ("Connection lost"); that is "not running", not a failure.
            return {
                "running": False,
                "socket": str(path),
                "error": redact_secrets(str(exc) or type(exc).__name__),
            }
        data = msgspec.to_builtins(result) if isinstance(result, p.HealthResult) else {}
        info = client.info
        # Health carries a ``running`` *turn* count; the daemon's own liveness
        # flag is set last so it can never be clobbered by the health payload.
        return {
            **data,
            "running": True,
            "pid": info.pid if info is not None else 0,
            "socket": str(path),
            "workspace": info.workspace if info is not None else str(resolved),
        }
    finally:
        await client.close()


async def stop(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
    socket_path: str | Path | None = None,
    timeout: float = 5.0,
    force: bool = False,
) -> bool:
    """Stop the daemon gracefully; fall back to a pid-file signal if needed."""
    resolved = Path(workspace).expanduser().resolve()
    path = (
        Path(socket_path)
        if socket_path is not None
        else default_socket_path(resolved, home=home)
    )
    try:
        client = await UDSClient.connect(path, timeout=timeout, focus=str(resolved), environ=dict(os.environ), client="control")
    except (DaemonUnavailable, VersionMismatch, TransportError, OSError):
        return _signal_stale(path)
    try:
        result = await client.call(p.Shutdown(reason="stop", force=force), timeout=timeout)
        if isinstance(result, p.ErrorResult):
            raise DaemonError(result.message)
        return isinstance(result, p.ShutdownResult) and result.stopping
    except (DaemonUnavailable, TransportError, OSError):
        # Teardown may close the socket before its ShutdownResult reaches us.
        # A disconnected peer after the shutdown request is an expected stop.
        return True
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def wait_stopped(
    workspace: str | Path,
    pid: int | None = None,
    *,
    home: str | Path | None = None,
    polls: int = 100,
    interval: float = 0.1,
) -> bool:
    """Wait until the daemon that was running has exited and released its lock.

    ``status`` is not proof: it reads "not running" as soon as the socket is
    unlinked, while the process still holds the exclusive lock. With ``pid``
    (from a prior ``status``) the process itself is the evidence; without it the
    lock is probed (acquired and dropped at once). A different daemon that took
    the lock afterwards does not count as the old one, so a known ``pid`` wins.
    """
    import fcntl

    lock = default_lock_path(Path(workspace).expanduser().resolve(), home=home)

    def exited() -> bool:
        if pid:
            return not _pid_alive(pid)
        try:
            fd = os.open(lock, os.O_RDWR)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        finally:
            os.close(fd)  # closing drops the probe lock
        return True

    for _ in range(max(1, polls)):
        if exited():
            return True
        await asyncio.sleep(interval)
    return exited()


def _signal_stale(socket: Path) -> bool:
    """Best-effort stop of a daemon whose socket is not answering.

    A pid file alone is not proof of identity: pids are reused and signaling an
    unrelated process is unacceptable. Signal only when the pid is alive **and**
    its command line is verifiably a Nexus daemon (and, when recorded, the pid
    file names this socket). Absent that proof, return ``False`` — never guess.
    """
    file = _auxiliary(socket, ".pid")
    try:
        data = msgspec.json.decode(file.read_bytes())
        pid = int(data["pid"])
        recorded_socket = data.get("socket")
    except (OSError, KeyError, TypeError, ValueError, msgspec.DecodeError):
        return False
    if (
        isinstance(recorded_socket, str)
        and recorded_socket
        and Path(recorded_socket) != socket
    ):
        return False
    if not _pid_alive(pid) or not _looks_like_nexus_daemon(pid):
        return False
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
        return True
    return False


def _looks_like_nexus_daemon(pid: int) -> bool:
    """Whether ``pid``'s command line is recognizably a Nexus daemon.

    Verification is mandatory before any signal. ``ps`` is the portable source;
    when it is unavailable the process cannot be verified, so the caller must
    not signal it.
    """
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no ps
        return False
    command = proc.stdout.strip()
    return bool(command) and (
        "nexus.host.daemon" in command or "nexus/host/daemon.py" in command
    )


def logs(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
    log_path: str | Path | None = None,
    lines: int = 200,
    all_projects: bool = False,
) -> str:
    """Return the last ``lines`` of the daemon log (empty when absent)."""
    resolved = Path(workspace).expanduser().resolve()
    if log_path is not None:
        path = Path(log_path)
    else:
        path = _auxiliary(default_socket_path(resolved, home=home), ".log")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    rows = text.splitlines()
    if not all_projects:
        key = resolve_project(resolved).key
        rows = [row for row in rows if f"project={key!r}" in row]
    return "\n".join(rows[-max(0, lines) :])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_origins(name: str) -> list[str] | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return origins or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nexus workspace daemon")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--socket", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument(
        "--max-concurrent-turns", type=int, default=DEFAULT_MAX_CONCURRENT_TURNS
    )
    # The HTTP/SSE surface is opt-in (default off) and loopback-only. The bearer
    # token is never a CLI flag -- argv is visible in ``ps`` -- so it comes from
    # ``NEXUS_HTTP_TOKEN`` (or is generated and published in the discovery file).
    parser.add_argument(
        "--http",
        action="store_true",
        default=_env_flag("NEXUS_HTTP"),
        help="enable the loopback HTTP/SSE surface (also NEXUS_HTTP=1)",
    )
    parser.add_argument(
        "--http-host",
        default=os.environ.get("NEXUS_HTTP_HOST", DEFAULT_HTTP_HOST),
        help="loopback host to bind (default 127.0.0.1)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=int(os.environ.get("NEXUS_HTTP_PORT", "0")),
        help="port to bind; 0 chooses an ephemeral one",
    )
    parser.add_argument(
        "--http-origin",
        action="append",
        default=_env_origins("NEXUS_HTTP_ORIGINS"),
        help="allowed Origin; repeatable (defaults to the loopback aliases)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    daemon = Daemon(
        args.workspace,
        home=args.home,
        socket_path=args.socket,
        idle_timeout=args.idle_timeout,
        max_concurrent_turns=args.max_concurrent_turns,
        http=args.http,
        http_host=args.http_host,
        http_port=args.http_port,
        http_origins=args.http_origin,
        http_token=os.environ.get("NEXUS_HTTP_TOKEN") or None,
    )
    return daemon.run()


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())


__all__ = [
    "DEFAULT_AUTOSTART_TIMEOUT",
    "DEFAULT_HANDSHAKE_TIMEOUT",
    "DEFAULT_HTTP_HOST",
    "DEFAULT_IDLE_TIMEOUT",
    "MAX_SUBSCRIPTIONS_PER_CLIENT",
    "Daemon",
    "DaemonError",
    "daemon_dir",
    "default_http_path",
    "default_lock_path",
    "default_log_path",
    "default_pid_path",
    "default_socket_path",
    "ensure_daemon",
    "logs",
    "main",
    "read_http_endpoint",
    "status",
    "stop",
    "wait_stopped",
    "workspace_hash",
]
