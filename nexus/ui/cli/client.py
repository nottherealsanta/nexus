"""The pure host client (PLAN section 14.8).

A surface is a *client* of the daemon and nothing else. This module makes that
literal: :class:`Client` holds a :class:`Transport`, speaks only the
:mod:`nexus.host.protocol` command/result structs, and never imports a runtime,
a session manager, a model, or a tool. The daemon always owns the ``Runtime``;
there is deliberately no in-process fallback, so a surface cannot accidentally
grow a second execution path.

The transport is an injected seam, which is what makes the client testable
without a daemon: the concrete Unix-socket transport lives in
:mod:`nexus.ui.cli.uds`, and a fake transport in the tests implements the same
two methods. That transport adapts the canonical
:class:`~nexus.host.transports.uds.UDSClient`, so the envelope framing and the
version handshake are implemented exactly once, in ``nexus.host``. The explicit
:meth:`Client.handshake` below is the *facade* health baseline a surface pins
before it draws anything; the wire-level version gate already ran at connect.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from ...events import Event
from ...host import protocol as p


class ClientError(Exception):
    """Base class for every failure the UI client raises."""


class ProtocolVersionError(ClientError):
    """The daemon speaks a different protocol revision than the client."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"protocol version mismatch: client speaks v{expected}, "
            f"daemon reports v{actual}; upgrade the client and daemon together"
        )


class FacadeError(ClientError):
    """A command was rejected by the facade, with its redacted message."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}" if message else kind)


class TransportClosed(ClientError):
    """The transport went away mid-command or mid-stream."""


@runtime_checkable
class Transport(Protocol):
    """The two operations every transport must provide.

    ``request`` is the request/response half (one command, one result) and
    ``events`` is the streaming half (a session's event tail, from ``from_seq``).
    A Unix socket, the HTTP/SSE surface, and a test double all satisfy this.
    """

    async def request(self, command: p.Command) -> p.Result:
        """Send one command and await its result."""
        ...

    def events(
        self,
        session: str,
        from_seq: int = 0,
        *,
        follow: bool = True,
        client_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """Return an async iterator over a session's events from ``from_seq``."""
        ...

    async def aclose(self) -> None:
        """Release the transport's resources."""
        ...


class Client:
    """A typed, transport-neutral client over the host protocol."""

    def __init__(
        self,
        transport: Transport,
        *,
        expected_version: int = p.PROTOCOL_VERSION,
        client_id: str | None = None,
    ) -> None:
        self.transport = transport
        self.expected_version = expected_version
        self.client_id = client_id or uuid.uuid4().hex
        self._health: p.HealthResult | None = None

    @property
    def health(self) -> p.HealthResult | None:
        return self._health

    # -- handshake ---------------------------------------------------------

    async def handshake(self) -> p.HealthResult:
        """Exchange versions and pin the daemon's health snapshot."""
        result = await self._request(p.Health())
        if not isinstance(result, p.HealthResult):
            raise ClientError(f"unexpected handshake result {type(result).__name__}")
        if result.version != self.expected_version:
            raise ProtocolVersionError(self.expected_version, result.version)
        self._health = result
        return result

    # -- session lifecycle -------------------------------------------------

    async def list_sessions(self) -> list[Any]:
        result = await self._request(p.SessionList())
        return list(result.sessions)  # type: ignore[union-attr]

    async def open_session(
        self, session: str, *, create: bool = True, recover: bool = True
    ) -> Any:
        result = await self._request(
            p.SessionOpen(session=session, create=create, recover=recover)
        )
        return result.session  # type: ignore[union-attr]

    async def start_turn(self, session: str, content: str) -> str:
        result = await self._request(p.SessionStart(session=session, content=content))
        return result.turn_id  # type: ignore[union-attr]

    async def enqueue(self, session: str, content: str) -> tuple[str, str]:
        result = await self._request(p.SessionEnqueue(session=session, content=content))
        return result.queued_id, result.turn_id  # type: ignore[union-attr]

    async def cancel(
        self, session: str, *, reason: str = "", drop_queue: bool = True
    ) -> tuple[bool, int]:
        result = await self._request(
            p.SessionCancel(session=session, reason=reason, drop_queue=drop_queue)
        )
        return result.cancelled, result.dropped  # type: ignore[union-attr]

    async def fork(
        self, session: str, at_seq: int | None = None, *, new_id: str | None = None
    ) -> Any:
        result = await self._request(
            p.SessionFork(session=session, at_seq=at_seq, new_id=new_id)
        )
        return result.session  # type: ignore[union-attr]

    async def delete(
        self, session: str, *, force: bool = False, reason: str = ""
    ) -> tuple[str, float]:
        result = await self._request(
            p.SessionDelete(session=session, force=force, reason=reason)
        )
        return result.trash_id, result.delete_after  # type: ignore[union-attr]

    async def restore(self, trash_id: str) -> str:
        result = await self._request(p.SessionRestore(trash_id=trash_id))
        return result.session  # type: ignore[union-attr]

    async def export(self, session: str, *, format: str = "markdown") -> str:
        result = await self._request(p.SessionExport(session=session, format=format))
        return result.content  # type: ignore[union-attr]

    async def state(self, session: str, from_seq: int = 0) -> tuple[dict[str, Any], int]:
        result = await self._request(p.SessionState(session=session, from_seq=from_seq))
        return dict(result.view), result.seq  # type: ignore[union-attr]

    async def resolve_permission(
        self, session: str, request_id: str, decision: str
    ) -> bool:
        result = await self._request(
            p.PermissionResolve(
                session=session,
                request_id=request_id,
                decision=decision,
                client_id=self.client_id,
            )
        )
        return bool(result.resolved)  # type: ignore[union-attr]

    # -- extensions / models / agents / health -----------------------------

    async def reload_extensions(self, trigger: str = "api") -> Any:
        return await self._request(p.ExtensionsReload(trigger=trigger))

    async def list_extensions(self) -> tuple[int, list[dict[str, Any]]]:
        result = await self._request(p.ExtensionsList())
        return result.generation, list(result.extensions)  # type: ignore[union-attr]

    async def validate_extensions(self, target: str | None = None) -> Any:
        return await self._request(p.ExtensionsValidate(target=target))

    async def trash_extensions(
        self, target: str, *, reason: str = "", force: bool = False
    ) -> Any:
        return await self._request(
            p.ExtensionsTrash(target=target, reason=reason, force=force)
        )

    async def list_models(
        self,
        *,
        provider: str | None = None,
        tier: str | None = None,
        selectable_only: bool = False,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._request(
            p.ModelsList(
                provider=provider,
                tier=tier,
                selectable_only=selectable_only,
                search=search,
            )
        )
        return list(result.models)  # type: ignore[union-attr]

    async def show_model(self, ref: str) -> Any:
        return await self._request(p.ModelShow(ref=ref))

    async def model_tiers(self) -> Any:
        return await self._request(p.ModelTiers())

    async def select_model(self, session: str, ref: str) -> Any:
        """Validate and persist a per-session model selection.

        Returns the accepted :class:`~nexus.host.protocol.ModelSelectResult`; a
        bad reference is raised as a :class:`FacadeError`.
        """
        return await self._request(p.ModelSelect(session=session, ref=ref))

    async def refresh_models(self) -> Any:
        return await self._request(p.ModelsRefresh())

    async def list_agents(self) -> list[dict[str, Any]]:
        result = await self._request(p.AgentsList())
        return list(result.agents)  # type: ignore[union-attr]

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request(p.ToolsList())
        return list(result.tools)  # type: ignore[union-attr]

    async def doctor(self, *, explain_reload: bool = False) -> Any:
        return await self._request(p.Doctor(explain_reload=explain_reload))

    async def shutdown(self, reason: str = "") -> bool:
        result = await self._request(p.Shutdown(reason=reason))
        return bool(result.stopping)  # type: ignore[union-attr]

    # -- streaming ---------------------------------------------------------

    def stream(
        self, session: str, from_seq: int = 0, *, follow: bool = True
    ) -> AsyncIterator[Event]:
        """Follow ``session`` from ``from_seq`` (exclusive), reconnect-safe."""
        return self.transport.events(
            session, from_seq, follow=follow, client_id=self.client_id
        )

    # -- internals ---------------------------------------------------------

    async def _request(self, command: p.Command) -> p.Result:
        result = await self.transport.request(command)
        if isinstance(result, p.ErrorResult):
            raise FacadeError(result.kind, result.message)
        return result

    async def aclose(self) -> None:
        await self.transport.aclose()


__all__ = [
    "Client",
    "ClientError",
    "FacadeError",
    "ProtocolVersionError",
    "Transport",
    "TransportClosed",
]
