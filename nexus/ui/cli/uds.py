"""Unix-socket transport for the CLI, over the canonical host wire.

The local wire is :mod:`nexus.host.transports`: one socket, length-framed JSON
envelopes, a ``hello``/``welcome`` handshake that fails loudly on a protocol
revision mismatch, correlated command results, and id-tagged event streams. This
module owns **no framing and no codec of its own** — it adapts the canonical
:class:`~nexus.host.transports.uds.UDSClient` to the surface's small two-method
:class:`~nexus.ui.cli.client.Transport` protocol, so there is exactly one
implementation of the wire.

Auto-start lives here and nowhere else. :func:`connect` calls
:func:`nexus.host.ensure_daemon`, which starts ``python -m nexus.host.daemon``
when no daemon is listening. The daemon subprocess builds the ``Runtime``; a
surface process never imports it, so ``nexus run`` can reach a workspace without
pulling the runtime, the providers, or ``httpx`` into the client interpreter.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ...events import Event
from ...host import TransportError, UDSClient, VersionMismatch, ensure_daemon
from ...host import protocol as p
from .client import Client, ClientError, ProtocolVersionError, TransportClosed

#: Default bound on connect + auto-start readiness.
DEFAULT_CONNECT_TIMEOUT = 10.0


class DaemonUnavailable(ClientError):
    """No daemon answered at the socket and auto-start did not produce one."""


def _unavailable(exc: BaseException) -> DaemonUnavailable:
    return DaemonUnavailable(str(exc) or type(exc).__name__)


class UdsTransport:
    """The UI transport protocol over one canonical :class:`UDSClient`.

    The canonical client already multiplexes commands and subscriptions over one
    socket with a single background reader, so both halves of the surface
    protocol are thin delegations. ``client_id`` is set once at connect time and
    carried by the connection; the per-call argument is accepted for protocol
    parity with a future HTTP transport and otherwise ignored.
    """

    def __init__(self, client: UDSClient) -> None:
        self.client = client
        self._closed = False

    @property
    def client_id(self) -> str:
        return self.client.client_id

    @property
    def closed(self) -> bool:
        return self._closed

    async def request(self, command: p.Command) -> p.Result:
        try:
            return await self.client.call(command)
        except TransportError as exc:
            raise TransportClosed(str(exc)) from exc

    def events(
        self,
        session: str,
        from_seq: int = 0,
        *,
        follow: bool = True,
        client_id: str | None = None,
    ) -> AsyncIterator[Event]:
        return self._stream(session, from_seq, follow)

    async def _stream(
        self, session: str, from_seq: int, follow: bool
    ) -> AsyncIterator[Event]:
        try:
            subscription = await self.client.subscribe(session, from_seq, follow=follow)
        except TransportError as exc:
            raise TransportClosed(str(exc)) from exc
        try:
            async for event in subscription:
                yield event
        except TransportError as exc:
            # A mid-stream transport failure (a dead daemon, a closed socket)
            # must surface as the UI's own error type, not a host-internal one.
            raise TransportClosed(str(exc)) from exc
        finally:
            with contextlib.suppress(Exception):
                await subscription.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.client.close()


async def connect(
    workspace: Any,
    *,
    home: Any = None,
    socket_path: Any = None,
    client_id: str | None = None,
    version: int = p.PROTOCOL_VERSION,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    idle_timeout: float | None = None,
    max_concurrent_turns: int | None = None,
    environ: Any = None,
    spawn: Any = None,
) -> UdsTransport:
    """Connect to ``workspace``'s daemon, auto-starting one if none is listening.

    The canonical handshake already ran inside :func:`ensure_daemon`; a version
    mismatch is surfaced as :class:`~nexus.ui.cli.client.ProtocolVersionError`
    and is never retried.
    """
    try:
        client = await ensure_daemon(
            workspace,
            home=home,
            socket_path=socket_path,
            client_id=client_id,
            version=version,
            timeout=timeout,
            idle_timeout=idle_timeout,
            max_concurrent_turns=max_concurrent_turns,
            environ=environ,
            spawn=spawn,
        )
    except VersionMismatch as exc:
        # ``VersionMismatch`` is phrased daemon-first (``expected`` is what the
        # daemon speaks, ``got`` what the client sent); the surface error is
        # phrased client-first (``expected`` is the client's revision).
        raise ProtocolVersionError(exc.got, exc.expected) from exc
    except TransportError as exc:
        # Framing, handshake, or readiness failures all mean "no usable daemon".
        raise _unavailable(exc) from exc
    return UdsTransport(client)


async def open_client(
    workspace: Any,
    *,
    home: Any = None,
    socket_path: Any = None,
    client_id: str | None = None,
    version: int = p.PROTOCOL_VERSION,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    idle_timeout: float | None = None,
    max_concurrent_turns: int | None = None,
    environ: Any = None,
    spawn: Any = None,
) -> Client:
    """Connect, auto-starting a daemon, and return a ready :class:`Client`."""
    transport = await connect(
        workspace,
        home=home,
        socket_path=socket_path,
        client_id=client_id,
        version=version,
        timeout=timeout,
        idle_timeout=idle_timeout,
        max_concurrent_turns=max_concurrent_turns,
        environ=environ,
        spawn=spawn,
    )
    client = Client(
        transport,
        expected_version=version,
        client_id=getattr(transport, "client_id", None),
    )
    try:
        await client.handshake()
    except BaseException:
        with contextlib.suppress(Exception):
            await transport.aclose()
        raise
    return client


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DaemonUnavailable",
    "UdsTransport",
    "connect",
    "open_client",
]
