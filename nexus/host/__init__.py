"""The host layer (L4½): the transport-neutral surface over one runtime.

A surface imports only ``nexus.host``, ``nexus.view``, and ``nexus.events``
(PLAN §14.1). This package holds:

* :class:`~nexus.host.facade.HostFacade` — the only surface API, over a runtime;
* :mod:`~nexus.host.protocol` — the serializable command/result wire contract;
* :class:`~nexus.host.supervisor.Supervisor` — the global turn scheduler;
* :class:`~nexus.host.presence.Presence` — view attachment and first-responder
  permission leases.

The daemon lifecycle (:mod:`~nexus.host.daemon`) and the concrete transports
(``uds``/``http_sse``) build on this package; nothing here imports a UI.

A surface reaches the canonical local wire through this package's public names
(``UDSClient``, ``ensure_daemon``, the transport errors) rather than a deep
submodule import, so ``nexus.host.transports`` can evolve behind the facade.
"""
from __future__ import annotations

from .daemon import ensure_daemon
from .facade import DEFAULT_MAX_CONCURRENT_TURNS, HostFacade
from .presence import Attachment, Presence
from .protocol import PROTOCOL_VERSION
from .supervisor import Supervisor
from .transports import DaemonUnavailable, TransportError, VersionMismatch
from .transports.http_sse import HTTPSSEError, HTTPSSEServer
from .transports.uds import EventSubscription, UDSClient

__all__ = [
    "DEFAULT_MAX_CONCURRENT_TURNS",
    "PROTOCOL_VERSION",
    "Attachment",
    "DaemonUnavailable",
    "EventSubscription",
    "HTTPSSEError",
    "HTTPSSEServer",
    "HostFacade",
    "Presence",
    "Supervisor",
    "TransportError",
    "UDSClient",
    "VersionMismatch",
    "ensure_daemon",
]
