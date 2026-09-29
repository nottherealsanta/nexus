"""The host-protocol client and one-shot renderer used by terminal surfaces.

This package is the *pure client* half of the Phase 8 surfaces: it speaks the
canonical
:mod:`nexus.host.transports` wire over an injected transport and imports only
``nexus.host``, ``nexus.view``, ``nexus.events``, and the standard library. It
never imports a runtime, a session manager, a model, or a tool, and it has no
in-process fallback — the daemon always owns the ``Runtime``.

Entry points:

* :func:`~nexus.ui.cli.uds.open_client` — connect to a daemon socket, auto-start
  one with :func:`nexus.host.ensure_daemon`, and complete the version handshake;
* :func:`~nexus.ui.cli.run.run_once` — one turn, human or JSONL;
* :func:`~nexus.ui.cli.run.run_once` — one-shot runs, human or JSONL.

Interactive chat is the Textual shell in :mod:`nexus.ui.tui`.
"""
from __future__ import annotations

from ...client.protocol import (
    Client,
    ClientError,
    FacadeError,
    ProtocolVersionError,
    Transport,
    TransportClosed,
)
from .approve import Approver
from .render import TERMINAL_EVENTS, TerminalRenderer, exit_code
from .run import run_once
from .uds import DaemonUnavailable, UdsTransport, connect, open_client

__all__ = [
    "TERMINAL_EVENTS", "Approver", "Client",
    "ClientError", "DaemonUnavailable", "FacadeError", "ProtocolVersionError",
    "TerminalRenderer", "Transport", "TransportClosed", "UdsTransport",
    "connect", "exit_code", "open_client", "run_once",
]
