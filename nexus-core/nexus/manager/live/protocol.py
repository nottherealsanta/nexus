"""Wire shapes for a GPT-Live call over the ChatGPT (Codex) sign-in. Undocumented: **not verified** against the service.

Ported from old Nexus ``nexus/live/protocol.py`` with dataclasses instead of msgspec. ``parse_event`` is total: a
malformed or unknown frame becomes ``Unknown``, never an exception. Inbound names are accepted with or without the
``session.`` prefix, because the public guide and the reference integrations disagree on it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MODEL = "gpt-live-1-codex"
VOICES = ("arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale")
DEFAULT_VOICE = "sol"
MAX_APPEND_BYTES = 500  # one append message, in UTF-8 bytes
MAX_EVENT_BYTES = 1_048_576
_FATAL_CODES = frozenset({"authentication_error", "invalid_token", "token_expired", "invalid_api_key"})


class LiveError(RuntimeError):
    """A typed Live failure: ``code`` is stable, ``message`` is redacted and bounded."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LiveSession:
    instructions: str
    voice: str = DEFAULT_VOICE
    model: str = MODEL

    def to_payload(self) -> dict[str, Any]:
        return {"model": self.model, "instructions": self.instructions,
                "audio": {"output": {"voice": self.voice}}, "delegation": {"type": "client"}}


@dataclass(frozen=True)
class CallRequest:
    sdp: str
    session: LiveSession

    def to_payload(self) -> dict[str, Any]:
        return {"sdp": self.sdp, "session": self.session.to_payload()}

    def __repr__(self) -> str:  # the SDP carries ICE credentials
        return f"CallRequest(sdp=<{len(self.sdp)} chars>, voice={self.session.voice!r})"


def resolve_voice(name: str) -> str:
    value = (name or "").strip().lower()
    if value in ("", "default"):
        return DEFAULT_VOICE
    if value not in VOICES:
        raise ValueError(f"unknown voice {name!r}; choose one of {', '.join(VOICES)}")
    return value


# -- inbound ---------------------------------------------------------------------

@dataclass(frozen=True)
class SidebandOpen:
    """Synthetic: the sideband socket opened."""


@dataclass(frozen=True)
class SidebandReconnected:
    """Synthetic: a replacement socket opened after a drop. Events in the gap may have been lost."""

    gap_s: float = 0.0


@dataclass(frozen=True)
class SessionStarted:
    session_id: str = ""


@dataclass(frozen=True)
class SessionClosed:
    reason: str = ""


@dataclass(frozen=True)
class TranscriptDelta:
    role: str  # "user" | "assistant"
    delta: str


@dataclass(frozen=True)
class TranscriptAdded:
    """A transcript that may be cumulative or a delta; the voice merges it."""

    role: str
    text: str


@dataclass(frozen=True)
class TurnDone:
    role: str
    transcript: str


@dataclass(frozen=True)
class DelegationCreated:
    """The voice model handed work to the client. ``text`` may be empty: the guide says the event has no request."""

    id: str
    text: str


@dataclass(frozen=True)
class ErrorEvent:
    message: str
    code: str = ""
    status: int | None = None
    fatal: bool = False


@dataclass(frozen=True)
class Unknown:
    raw_type: str = ""


LiveEvent = (SidebandOpen | SidebandReconnected | SessionStarted | SessionClosed | TranscriptDelta | TranscriptAdded
             | TurnDone | DelegationCreated | ErrorEvent | Unknown)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_event(raw: str | bytes) -> LiveEvent:
    try:
        if len(raw) > MAX_EVENT_BYTES:
            return Unknown("oversize")
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return Unknown("malformed")
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return Unknown("")
    kind: str = obj["type"]
    try:
        return _known(kind.removeprefix("session."), kind, obj)
    except Exception:  # noqa: BLE001 - parsing is total by contract
        return Unknown(kind[:80])


def _known(kind: str, original: str, obj: dict[str, Any]) -> LiveEvent:
    if kind in ("started", "updated") and original.startswith("session."):
        session = obj.get("session")
        return SessionStarted(_str(session.get("id")) if isinstance(session, Mapping) else "")
    if kind == "closed":
        return SessionClosed(_str(obj.get("reason"))[:200])
    if kind in ("input_transcript.added", "output_transcript.added"):
        item = obj.get("item")
        return TranscriptAdded("user" if kind.startswith("input") else "assistant",
                               _str(item.get("text")) if isinstance(item, Mapping) else "")
    if kind in ("input_transcript.delta", "output_transcript.delta"):
        return TranscriptDelta("user" if kind.startswith("input") else "assistant", _str(obj.get("delta")))
    if kind == "turn.done":
        turn = obj.get("turn")
        if not isinstance(turn, Mapping) or turn.get("role") not in ("user", "assistant"):
            return Unknown(kind)
        return TurnDone(turn["role"], _str(turn.get("transcript")))
    if kind == "delegation.created":
        # Codex route: {"item": {id, target, content}}; public API: {"delegation": {id, target}} with no request text.
        item = obj.get("item") if isinstance(obj.get("item"), Mapping) else obj.get("delegation")
        item = item if isinstance(item, Mapping) else obj
        ident = item.get("id") or obj.get("delegation_id") or obj.get("delegation_item_id")
        if not isinstance(ident, str) or not ident:
            return Unknown(kind)
        if item.get("target") not in (None, "client"):
            return Unknown(kind)
        return DelegationCreated(ident[:200], _delegation_text(item.get("content")))
    if kind == "error":
        err = obj.get("error") if isinstance(obj.get("error"), Mapping) else {}
        code = _str(obj.get("code") or err.get("code")).lower()
        status = obj.get("status") if isinstance(obj.get("status"), int) else err.get("status")
        status = status if isinstance(status, int) and not isinstance(status, bool) else None
        message = _str(obj.get("message")) or _str(err.get("message")) or (
            obj["error"] if isinstance(obj.get("error"), str) else "GPT Live error")
        return ErrorEvent(message[:300], code[:80], status, fatal=status == 401 or code in _FATAL_CODES)
    return Unknown(original[:80])


def _delegation_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(p["text"] for p in content
                     if isinstance(p, Mapping) and p.get("type") == "input_text" and isinstance(p.get("text"), str))


# -- outbound --------------------------------------------------------------------

def chunk_utf8(text: str, limit: int = MAX_APPEND_BYTES) -> list[str]:
    """Split into chunks of at most ``limit`` UTF-8 bytes, never inside a code point."""
    if limit < 4:
        raise ValueError("limit must be at least 4 bytes")
    chunks: list[str] = []
    start = size = 0
    for index, char in enumerate(text):
        point = ord(char)
        width = 1 if point < 0x80 else 2 if point < 0x800 else 3 if point < 0x10000 else 4
        if size + width > limit:
            chunks.append(text[start:index])
            start, size = index, 0
        size += width
    chunks.append(text[start:])
    return chunks


STYLES = ("codex", "api")


def outbound(kind: str, text: str, *, delegation_id: str | None, style: str = "codex") -> list[dict[str, Any]]:
    """Messages that put ``text`` in front of the voice model, each within 500 UTF-8 bytes.

    ``kind`` is what the Manager wants: ``say`` (the voice says it, paraphrased), ``quiet`` (it may use it later,
    never says it now) or ``instruct`` (direction for the call). The two routes name these differently, and the
    channel words even mean opposite things, so the mapping lives here and nowhere else:

    - ``codex`` (ChatGPT sign-in, read from the Codex source): ``session.context.append`` or, with a delegation,
      ``delegation.context.append``; ``say`` is channel ``speakable``, ``quiet`` is channel ``commentary``;
      there is no instruction event, so ``instruct`` is not sent.
    - ``api`` (the documented public API): ``session.commentary.append`` says, ``session.thinking.append`` is quiet,
      ``session.instructions.append`` instructs, each with a string ``content`` and ``delegation_id`` (or null).
    """
    if kind not in ("say", "quiet", "instruct") or style not in STYLES:
        raise ValueError("unknown kind or style")
    out: list[dict[str, Any]] = []
    for chunk in chunk_utf8(text):
        if not chunk:
            continue
        if style == "api":
            wire = {"say": "session.commentary.append", "quiet": "session.thinking.append",
                    "instruct": "session.instructions.append"}[kind]
            out.append({"type": wire, "delegation_id": delegation_id, "content": chunk})
            continue
        if kind == "instruct":
            return []
        message: dict[str, Any] = ({"type": "delegation.context.append", "delegation_item_id": delegation_id}
                                   if delegation_id else {"type": "session.context.append"})
        message["channel"] = "speakable" if kind == "say" else "commentary"
        message["content"] = [{"type": "input_text", "text": chunk}]
        out.append(message)
    return out


def close_message() -> dict[str, str]:
    return {"type": "session.close"}


_CALL_ID = re.compile(r"^(?:rtc_[\w-]+|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$")


def valid_call_id(value: str) -> bool:
    return bool(_CALL_ID.fullmatch(value or ""))
