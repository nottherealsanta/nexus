"""Concrete host transports (PLAN section 14.9).

One wire protocol, many transports. ``uds`` is the local CLI <-> daemon link;
``http_sse`` maps the same commands to POST and the events to SSE. Both speak
the same :mod:`nexus.host.protocol` commands and :class:`~nexus.events.Event`
envelopes, so this package owns only what is *shared by every transport*: the
logical frame envelope and the error taxonomy a peer can raise.

The frames here are transport-neutral: they say "this is a command", "this is a
result", "this is a streamed event", and so on. A concrete transport decides how
to put them on the wire. ``uds`` puts a 4-byte big-endian length prefix in front
of compact JSON and caps the length in both directions, so a corrupt or hostile
peer cannot make a reader allocate unbounded memory; the SSE transport maps the
same envelope onto named event types instead.

Nothing in this module reaches upward: it imports the protocol and the event
envelope only, never a runtime, a manager, or a UI.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any

import msgspec

from ...errors import NexusError
from ...events import Event
from .. import protocol as p

#: Length-prefix width in bytes (a big-endian unsigned 32-bit length).
FRAME_HEADER_BYTES = 4
#: Upper bound on one frame body. Bounds both what a reader will allocate and
#: what a writer will emit, so a runaway export or event cannot wedge a peer.
MAX_FRAME_BYTES = 16 * 1024 * 1024


class TransportError(NexusError):
    """A transport-level failure: framing, handshake, or a dead peer."""


class ProtocolError(TransportError):
    """A frame was malformed, truncated, or carried an unknown kind."""


class FrameTooLarge(ProtocolError):
    """A frame body exceeded :data:`MAX_FRAME_BYTES`."""

    def __init__(self, size: int, limit: int = MAX_FRAME_BYTES):
        super().__init__(f"frame of {size} bytes exceeds the {limit}-byte limit")
        self.size = size
        self.limit = limit


class VersionMismatch(TransportError):
    """A client and daemon disagree on the wire protocol revision.

    The handshake fails loudly instead of letting two builds speak a
    half-understood protocol (PLAN section 14.8).
    """

    def __init__(self, expected: int, got: int):
        super().__init__(
            f"protocol version mismatch: daemon speaks {expected}, client sent {got}"
        )
        self.expected = expected
        self.got = got


class DaemonUnavailable(TransportError):
    """A daemon could not be reached, and auto-start did not produce one."""


# ---------------------------------------------------------------------------
# Frame envelope (tagged by ``type``)
# ---------------------------------------------------------------------------


class Hello(msgspec.Struct, tag="hello", frozen=True):
    """Client -> daemon: the first frame, carrying the protocol revision."""

    version: int
    client_id: str = ""
    client: str = ""


class Welcome(msgspec.Struct, tag="welcome", frozen=True):
    """Daemon -> client: the handshake succeeded; the peer may now speak."""

    version: int
    pid: int = 0
    workspace: str = ""
    socket: str = ""
    server: str = ""
    client_id: str = ""


class Reject(msgspec.Struct, tag="reject", frozen=True):
    """Daemon -> client: the handshake failed; the daemon closes the link."""

    reason: str
    expected: int = 0
    got: int = 0


class CommandFrame(msgspec.Struct, tag="command", frozen=True):
    """Client -> daemon: one :data:`~nexus.host.protocol.Command`, correlated by ``id``."""

    id: str
    command: p.Command


class ResultFrame(msgspec.Struct, tag="result", frozen=True):
    """Daemon -> client: the result for the command with the same ``id``."""

    id: str
    result: p.Result


class EventFrame(msgspec.Struct, tag="event", frozen=True):
    """Daemon -> client: one streamed event for subscription ``id``."""

    id: str
    event: Event


class SubscribeDone(msgspec.Struct, tag="subscribe_done", frozen=True):
    """Daemon -> client: subscription ``id`` reached its end (non-follow, or closed)."""

    id: str
    error: str = ""


class Unsubscribe(msgspec.Struct, tag="unsubscribe", frozen=True):
    """Client -> daemon: stop streaming subscription ``id``."""

    id: str


class Ping(msgspec.Struct, tag="ping", frozen=True):
    nonce: str = ""


class Pong(msgspec.Struct, tag="pong", frozen=True):
    nonce: str = ""


class Bye(msgspec.Struct, tag="bye", frozen=True):
    """Either direction: a graceful close, with a redacted reason."""

    reason: str = ""


#: The complete frame union; ``msgspec`` decodes it by the ``type`` tag.
Frame = (
    Hello
    | Welcome
    | Reject
    | CommandFrame
    | ResultFrame
    | EventFrame
    | SubscribeDone
    | Unsubscribe
    | Ping
    | Pong
    | Bye
)

FRAMES: tuple[type, ...] = (
    Hello,
    Welcome,
    Reject,
    CommandFrame,
    ResultFrame,
    EventFrame,
    SubscribeDone,
    Unsubscribe,
    Ping,
    Pong,
    Bye,
)


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


def encode_frame(frame: Frame) -> bytes:
    """Encode one frame as a length prefix plus compact JSON."""
    body = msgspec.json.encode(frame)
    if len(body) > MAX_FRAME_BYTES:
        raise FrameTooLarge(len(body))
    return struct.pack(">I", len(body)) + body


def decode_frame(body: bytes | str) -> Frame:
    """Decode a frame body; an unknown or malformed body raises."""
    try:
        return msgspec.json.decode(body, type=Frame)
    except (msgspec.ValidationError, msgspec.DecodeError, ValueError) as exc:
        raise ProtocolError(f"malformed frame: {exc}") from exc


async def write_frame(writer: Any, frame: Frame) -> None:
    """Write one frame and flush it."""
    writer.write(encode_frame(frame))
    await writer.drain()


async def read_frame(
    reader: Any, *, max_bytes: int = MAX_FRAME_BYTES
) -> Frame | None:
    """Read one frame; return ``None`` on a clean end-of-stream.

    A truncated header or body is a :class:`ProtocolError`; an oversized length
    is a :class:`FrameTooLarge`. Neither can be turned into a giant allocation.
    """
    try:
        header = await reader.readexactly(FRAME_HEADER_BYTES)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ProtocolError("truncated frame header") from exc
    (size,) = struct.unpack(">I", header)
    if size > max_bytes:
        raise FrameTooLarge(size, max_bytes)
    try:
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("truncated frame body") from exc
    return decode_frame(body)


__all__ = [
    "FRAMES",
    "FRAME_HEADER_BYTES",
    "MAX_FRAME_BYTES",
    "Bye",
    "CommandFrame",
    "DaemonUnavailable",
    "EventFrame",
    "Frame",
    "FrameTooLarge",
    "Hello",
    "Ping",
    "Pong",
    "ProtocolError",
    "Reject",
    "ResultFrame",
    "SubscribeDone",
    "TransportError",
    "Unsubscribe",
    "VersionMismatch",
    "Welcome",
    "decode_frame",
    "encode_frame",
    "read_frame",
    "write_frame",
]
