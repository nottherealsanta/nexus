"""The prompt_toolkit CLI client over the host facade (PLAN sections 14.3, 14.11).

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
* :func:`~nexus.ui.cli.app.run_chat` — the interactive line-mode surface.

``prompt_toolkit`` is an optional extra (``nexus[cli]``) loaded lazily by
:mod:`nexus.ui.cli.keys`; importing this package must not import it.
"""
from __future__ import annotations

from .app import ChatSession, run_chat
from .approve import Approver
from .client import (
    Client,
    ClientError,
    FacadeError,
    ProtocolVersionError,
    Transport,
    TransportClosed,
)
from .keys import CliDependencyError, StdinReader, available, make_reader
from .render import TERMINAL_EVENTS, TerminalRenderer, exit_code
from .run import run_once
from .uds import DaemonUnavailable, UdsTransport, connect, open_client

__all__ = [
    "TERMINAL_EVENTS", "Approver", "ChatSession", "CliDependencyError", "Client",
    "ClientError", "DaemonUnavailable", "FacadeError", "ProtocolVersionError",
    "StdinReader", "TerminalRenderer", "Transport", "TransportClosed", "UdsTransport",
    "available", "connect", "exit_code", "make_reader", "open_client", "run_chat",
    "run_once",
]
