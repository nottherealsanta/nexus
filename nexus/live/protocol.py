"""Wire shapes for a GPT Live call (undocumented; see ``NOTICE``).

Everything here is re-implemented from observed behaviour recorded in
``plans/in-progress/GPT_LIVE_PROTOCOL_NOTES.md`` (sections cited as ``notes §N``).
No code was copied from the reference integrations. Nothing in this module has
been exercised against the real service: **not verified**.

Contract:

- ``LiveSession`` / ``CallRequest`` build the signaling JSON body (notes §1.2-1.3).
- ``parse_event`` turns one sideband text frame into a frozen event struct and
  **never raises**: malformed or unrecognised frames become ``Unknown``.
- ``append_message`` / ``append_messages`` build the outbound context appends
  (notes §4.1-4.4); ``chunk_utf8`` splits on code points to at most 500 UTF-8
  bytes per message.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import msgspec

#: Pinned model id; not user config until the protocol is documented (plan §4.6).
MODEL = "gpt-live-1-codex"
#: The nine voice names both references accept (notes §7).
VOICES = ("arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale")
DEFAULT_VOICE = "sol"
#: Maximum UTF-8 bytes of text per append message (notes §4.4).
MAX_APPEND_BYTES = 500
#: Bounds for what the daemon accepts from the wire.
MAX_EVENT_BYTES = 1_048_576
MAX_DELEGATION_CHARS = 16_000
CHANNELS = ("speakable", "commentary")
_FATAL_CODES = frozenset({"authentication_error", "invalid_token", "token_expired", "invalid_api_key"})


class LiveError(RuntimeError):
    """A safe, typed Live failure: ``code`` is stable, ``message`` is redacted and bounded."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# -- signaling body -------------------------------------------------------

class LiveSession(msgspec.Struct, frozen=True):
    """The ``session`` object of the call request (notes §1.3)."""

    instructions: str
    voice: str = DEFAULT_VOICE
    model: str = MODEL

    def to_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "instructions": self.instructions,
            "audio": {"output": {"voice": self.voice}},
            "delegation": {"type": "client"},
        }


class CallRequest(msgspec.Struct, frozen=True):
    """The JSON body POSTed to the calls endpoint: ``{"sdp", "session"}`` (notes §1.2)."""

    sdp: str
    session: LiveSession

    def to_payload(self) -> dict[str, Any]:
        return {"sdp": self.sdp, "session": self.session.to_payload()}

    def __repr__(self) -> str:  # the SDP carries ICE credentials: never print it
        return f"CallRequest(sdp=<{len(self.sdp)} chars>, voice={self.session.voice!r})"


def resolve_voice(name: str) -> str:
    """Map config text to a voice name; ``""``/``default`` give the default; unknown raises."""
    value = (name or "").strip().lower()
    if value in ("", "default"):
        return DEFAULT_VOICE
    if value not in VOICES:
        raise ValueError(f"unknown voice {name!r}; choose one of {', '.join(VOICES)}")
    return value


# -- inbound events -------------------------------------------------------

class SidebandOpen(msgspec.Struct, frozen=True):
    """Synthetic: the transport opened the sideband socket (not a wire event)."""


class SidebandReconnected(msgspec.Struct, frozen=True):
    """Synthetic: a replacement sideband socket is established after a drop (not a wire event).

    Emitted once per successful recovery, never at startup (that is ``SidebandOpen``).
    ``gap_s`` is the monotonic seconds between detecting the drop and the new socket opening.
    Events sent on the old socket may have been lost; delivery of in-flight sends is unknown.
    """

    gap_s: float = 0.0


class SessionStarted(msgspec.Struct, frozen=True):
    session_id: str = ""


class SessionUpdated(msgspec.Struct, frozen=True):
    session_id: str = ""


class SessionClosed(msgspec.Struct, frozen=True):
    reason: str = ""


class TranscriptAdded(msgspec.Struct, frozen=True):
    """``input_transcript.added`` / ``output_transcript.added``; cumulative or delta (notes §3.2)."""

    role: str  # "user" | "assistant"
    text: str


class TranscriptDelta(msgspec.Struct, frozen=True):
    """``session.input_transcript.delta`` / ``session.output_transcript.delta``."""

    role: str
    delta: str


class TurnDone(msgspec.Struct, frozen=True):
    """Final text of one spoken turn: the source of truth for transcripts."""

    role: str
    transcript: str


class DelegationCreated(msgspec.Struct, frozen=True):
    """The voice model handed work to the client (notes §3.3)."""

    id: str
    text: str


class ErrorEvent(msgspec.Struct, frozen=True):
    message: str
    code: str = ""
    status: int | None = None
    fatal: bool = False


class Unknown(msgspec.Struct, frozen=True):
    """Anything unrecognised or malformed; ignored by the manager, never an error."""

    raw_type: str = ""


LiveEvent = (
    SidebandOpen | SidebandReconnected | SessionStarted | SessionUpdated | SessionClosed | TranscriptAdded
    | TranscriptDelta | TurnDone | DelegationCreated | ErrorEvent | Unknown
)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _error_message(obj: Mapping[str, Any]) -> str:
    """Message lookup order of notes §3.4: top-level, ``error.message``, ``error`` string, stringified."""
    if isinstance(obj.get("message"), str):
        return obj["message"]
    error = obj.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(error, str):
        return error
    if error is not None:
        return str(error)[:300]
    return "GPT Live error"


def _delegation_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        part["text"] for part in content
        if isinstance(part, Mapping) and part.get("type") == "input_text" and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts)  # notes "Discrepancies": newline-join keeps parts readable


def parse_event(raw: str | bytes) -> LiveEvent:
    """Decode one sideband frame. Total: every input yields an event, none raises."""
    try:
        if len(raw) > MAX_EVENT_BYTES:
            return Unknown("oversize")
        obj = msgspec.json.decode(raw)
    except (msgspec.DecodeError, ValueError, TypeError):
        return Unknown("malformed")
    if not isinstance(obj, dict):
        return Unknown("")
    kind = obj.get("type")
    if not isinstance(kind, str):
        return Unknown("")
    try:
        return _parse_known(kind, obj)
    except Exception:  # noqa: BLE001 - parsing is total by contract
        return Unknown(kind[:80])


def _parse_known(kind: str, obj: dict[str, Any]) -> LiveEvent:
    if kind in ("session.started", "session.updated"):
        session = obj.get("session")
        sid = _str(session.get("id")) if isinstance(session, Mapping) else ""
        return SessionStarted(sid) if kind == "session.started" else SessionUpdated(sid)
    if kind == "session.closed":
        return SessionClosed(_str(obj.get("reason"))[:200])
    if kind in ("input_transcript.added", "output_transcript.added"):
        item = obj.get("item")
        text = _str(item.get("text")) if isinstance(item, Mapping) else ""
        return TranscriptAdded("user" if kind.startswith("input") else "assistant", text)
    if kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
        return TranscriptDelta("user" if "input" in kind else "assistant", _str(obj.get("delta")))
    if kind == "turn.done":
        turn = obj.get("turn")
        if not isinstance(turn, Mapping) or turn.get("role") not in ("user", "assistant"):
            return Unknown(kind)
        return TurnDone(turn["role"], _str(turn.get("transcript")))
    if kind == "delegation.created":
        item = obj.get("item")
        if (
            not isinstance(item, Mapping) or item.get("type") != "delegation"
            or item.get("target") != "client" or not isinstance(item.get("id"), str) or not item["id"]
            or not isinstance(item.get("content"), list)
        ):
            return Unknown(kind)
        return DelegationCreated(item["id"][:200], _delegation_text(item["content"]))
    if kind == "error":
        err = obj.get("error") if isinstance(obj.get("error"), Mapping) else {}
        code = _str(obj.get("code") or err.get("code")).lower()
        status = obj.get("status") if isinstance(obj.get("status"), int) else err.get("status")
        status = status if isinstance(status, int) and not isinstance(status, bool) else None
        return ErrorEvent(
            _error_message(obj), code[:80], status, fatal=(status == 401 or code in _FATAL_CODES)
        )
    return Unknown(kind[:80])


# -- outbound -------------------------------------------------------------

def chunk_utf8(text: str, limit: int = MAX_APPEND_BYTES) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` UTF-8 bytes, never inside a code point.

    Empty input gives one empty chunk (notes §4.4); callers skip empty text.
    """
    if limit < 4:
        raise ValueError("limit must be at least 4 bytes (one code point)")
    chunks: list[str] = []
    start = 0
    size = 0
    for index, char in enumerate(text):
        point = ord(char)
        width = 1 if point < 0x80 else 2 if point < 0x800 else 3 if point < 0x10000 else 4
        if size + width > limit:
            chunks.append(text[start:index])
            start, size = index, 0
        size += width
    chunks.append(text[start:])
    return chunks


def append_message(text: str, *, channel: str | None = None, delegation_id: str | None = None) -> dict[str, Any]:
    """One context-append message; ``delegation.context.append`` when ``delegation_id`` is set."""
    if channel is not None and channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    message: dict[str, Any] = {"type": "session.context.append"}
    if delegation_id is not None:
        message = {"type": "delegation.context.append", "delegation_item_id": delegation_id}
    if channel is not None:
        message["channel"] = channel
    message["content"] = [{"type": "input_text", "text": text}]
    return message


def append_messages(
    text: str, *, channel: str | None = None, delegation_id: str | None = None
) -> list[dict[str, Any]]:
    """``text`` as an ordered list of appends, each at most 500 UTF-8 bytes."""
    return [
        append_message(chunk, channel=channel, delegation_id=delegation_id) for chunk in chunk_utf8(text)
    ]


def close_message() -> dict[str, str]:
    return {"type": "session.close"}


_CALL_ID = re.compile(r"^(?:rtc_[\w-]+|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$")


def valid_call_id(value: str) -> bool:
    return bool(_CALL_ID.fullmatch(value or ""))


__all__ = [
    "CHANNELS", "DEFAULT_VOICE", "MAX_APPEND_BYTES", "MAX_DELEGATION_CHARS", "MODEL",
    "VOICES", "CallRequest", "LiveError", "DelegationCreated", "ErrorEvent", "LiveEvent", "LiveSession",
    "SessionClosed", "SessionStarted", "SessionUpdated", "SidebandOpen", "SidebandReconnected", "TranscriptAdded",
    "TranscriptDelta", "TurnDone", "Unknown", "append_message", "append_messages", "chunk_utf8",
    "close_message", "parse_event", "resolve_voice", "valid_call_id",
]
