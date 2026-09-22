"""Normalized MCP error taxonomy (plan section 5.5).

The official ``mcp`` package, ``httpx``, subprocess spawn failures, JSON
decoding, and a peer's JSON-RPC error objects all raise different exceptions.
None of them should escape the MCP boundary, because the rest of Nexus must not
depend on the wire library's error shapes (the plan confines an upstream
breaking change to ``mcp/client.py``). Every failure crossing that boundary is
translated into one of these types, all rooted at :class:`MCPError` and
therefore at :class:`~nexus.errors.NexusError`.

A note on inheritance: ``MCPConfigError`` also derives from ``ValueError`` and
``MCPTimeout`` from the builtin ``TimeoutError``/``OSError`` family so callers
that already catch those still work; the MCP type is what tests and the manager
should match on.
"""

from __future__ import annotations

from ..errors import NexusError, OperationCancelled

__all__ = [
    "MCPCallError",
    "MCPCancelled",
    "MCPClosed",
    "MCPConfigError",
    "MCPError",
    "MCPProtocolError",
    "MCPRemoteError",
    "MCPTimeout",
    "MCPTransportError",
    "MCPUnavailable",
]


class MCPError(NexusError):
    """Base class for every normalized MCP failure."""


class MCPUnavailable(MCPError, ImportError):
    """A requested backend or the optional ``mcp`` package is unavailable."""


class MCPConfigError(MCPError, ValueError):
    """A server definition is missing, malformed, or contradictory."""


class MCPTransportError(MCPError):
    """The transport failed: spawn, framing, socket, HTTP, or unexpected exit."""


class MCPProtocolError(MCPError):
    """The peer sent malformed JSON-RPC or violated the MCP handshake."""


class MCPRemoteError(MCPProtocolError):
    """The peer returned a JSON-RPC *error* object.

    ``code``/``message``/``data`` are the normalized JSON-RPC error fields, not
    an upstream object; ``data`` is carried through as-is but is never rendered
    into ``repr`` by this class.
    """

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class MCPCallError(MCPRemoteError):
    """A ``tools/call`` (or resource/prompt read) returned a JSON-RPC error."""


class MCPTimeout(MCPError, TimeoutError):
    """An MCP operation exceeded its deadline."""


class MCPClosed(MCPError):
    """A client or transport was used after it was closed."""


class MCPCancelled(MCPError, OperationCancelled):
    """A cooperative cancellation interrupted an MCP operation."""
