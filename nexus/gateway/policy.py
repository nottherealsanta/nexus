"""Which host commands a paired device may send, and in which scope.

Default deny: only the command types listed here are ever forwarded, each is
checked against the device's scopes, and every free-form field is bounded again
here so a remote client cannot rely on the host to do it. Settings, providers,
extensions, shell, delete, export, logs, worktrees, mock, shutdown, ``WebLaunch``,
cross-project listing and attachments are deliberately absent.
"""
from __future__ import annotations

import re

import msgspec

from ..host import protocol as p

MAX_COMMAND_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 256 * 1024
MAX_BLOCKS = 16
MAX_OFFER_SDP_BYTES = 64 * 1024
#: Mirrors ``nexus.live.manager.HELPER_KINDS`` / ``MAX_HELPER_DETAIL``.
HELPER_KINDS = frozenset({"ready", "levels", "phase", "dc_error", "dropped", "closed"})
MAX_HELPER_DETAIL = 2048

SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_IDENT = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_DECISION = re.compile(r"[A-Za-z0-9_:-]{1,64}\Z")
_EFFORT = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_QUEUE_MODES = frozenset({"queue", "steer", "interrupt"})

_READ = (
    p.SessionList, p.SessionState, p.ContextInspect, p.ModelsList, p.AgentsList,
    p.AgentCurrent, p.AgentTranscript, p.LiveStatus, p.VoiceStatus, p.Health,
    p.ToolsList, p.GitDiff,
)
_PROMPT = (
    p.SessionOpen, p.SessionStart, p.SessionEnqueue, p.SessionCancel,
    p.SessionQueueMove, p.SessionQueueRemove, p.ModelSelect,
    p.ReasoningEffortSelect, p.AgentSelect, p.AgentCancel, p.SessionArchive,
    p.SessionUnarchive,
)
_APPROVE = (p.PermissionResolve, p.QuestionAnswer)
_VOICE = (p.VoiceCancel,)
_LIVE = (p.LiveStart, p.LiveMute, p.LivePause, p.LiveStop, p.LiveHelperEvent)

#: command type -> required scope (``LiveConsent`` is resolved per call).
COMMAND_SCOPES: dict[type, str] = {
    **dict.fromkeys(_READ, "read"),
    **dict.fromkeys(_PROMPT, "prompt"),
    **dict.fromkeys(_APPROVE, "approve"),
    **dict.fromkeys(_VOICE, "voice"),
    **dict.fromkeys(_LIVE, "live"),
}


class CommandDenied(Exception):
    """The command is not allowed for this device (unknown type or missing scope)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CommandInvalid(Exception):
    """The command is allowed but a field is out of bounds."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def required_scope(command: object) -> str | None:
    """The scope a command needs, or ``None`` when it is never allowed."""
    if isinstance(command, p.LiveConsent):
        return "read" if command.accept is None else "live"
    return COMMAND_SCOPES.get(type(command))


def is_read_only(command: object) -> bool:
    """Safe to retry after a connection failure (never mutates host state)."""
    return required_scope(command) == "read"


def authorize(command: p.Command, scopes: tuple[str, ...], device_id: str) -> p.Command:
    """Return the (possibly rewritten) command to forward, or raise."""
    scope = required_scope(command)
    if scope is None:
        raise CommandDenied("command_denied", "this command is not available through the gateway")
    if scope not in scopes:
        raise CommandDenied("scope_required", f"this command needs the {scope} scope")
    return _validate(command, device_id)


def _bad(message: str):
    raise CommandInvalid(message)


def _session(value: object) -> None:
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        _bad("invalid session id")


def _ident(value: object, label: str) -> None:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        _bad(f"invalid {label}")


def _text(value: object, limit: int, label: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value):
        _bad(f"invalid {label}")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
        _bad(f"invalid {label}")


def _line(value: object, limit: int, label: str, *, allow_empty: bool = True) -> None:
    _text(value, limit, label, allow_empty=allow_empty)
    if isinstance(value, str) and any(ch in "\n\r" for ch in value):
        _bad(f"invalid {label}")


def _prompt_blocks(content: str, blocks: list, attachments: list, labels: list) -> None:
    if attachments or labels:
        _bad("attachments are not available through the gateway")
    _text(content, MAX_TEXT_CHARS, "content")
    if len(blocks) > MAX_BLOCKS:
        _bad("too many content blocks")
    total = len(content)
    for block in blocks:
        if not isinstance(block, dict) or set(block) != {"type", "text"}:
            _bad("only plain text content blocks are available through the gateway")
        if block["type"] != "text" or not isinstance(block["text"], str):
            _bad("only plain text content blocks are available through the gateway")
        total += len(block["text"])
        _text(block["text"], MAX_TEXT_CHARS, "content block")
    if total > MAX_TEXT_CHARS:
        _bad("content is too long")


def _validate(command: p.Command, device_id: str) -> p.Command:  # noqa: C901 - one flat table
    match command:
        case p.SessionOpen() | p.SessionUnarchive() | p.ContextInspect() | p.AgentCurrent():
            _session(command.session)
        case p.SessionState():
            _session(command.session)
            if not 0 <= command.from_seq <= 2**53:
                _bad("invalid from_seq")
        case p.SessionStart():
            _session(command.session)
            if command.client_request_id and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", command.client_request_id) is None:
                _bad("invalid client_request_id")
            _prompt_blocks(command.content, command.blocks, command.attachments, command.attachment_labels)
        case p.SessionEnqueue():
            _session(command.session)
            if command.mode not in _QUEUE_MODES:
                _bad("invalid mode")
            if command.client_request_id and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", command.client_request_id) is None:
                _bad("invalid client_request_id")
            _prompt_blocks(command.content, command.blocks, command.attachments, command.attachment_labels)
        case p.SessionCancel():
            _session(command.session)
            _line(command.reason, 200, "reason")
        case p.SessionQueueMove():
            _session(command.session)
            _ident(command.queued_id, "queued id")
            if command.offset not in (-1, 1):
                _bad("invalid offset")
        case p.SessionQueueRemove():
            _session(command.session)
            _ident(command.queued_id, "queued id")
        case p.SessionArchive():
            _session(command.session)
            _line(command.reason, 200, "reason")
        case p.ModelSelect():
            _session(command.session)
            _line(command.ref, 200, "model", allow_empty=False)
        case p.ReasoningEffortSelect():
            _session(command.session)
            if command.effort is not None and not _EFFORT.fullmatch(command.effort):
                _bad("invalid effort")
        case p.AgentSelect():
            _session(command.session)
            _line(command.name, 128, "agent name", allow_empty=False)
        case p.AgentCancel():
            _session(command.session)
            _ident(command.agent_id, "agent id")
            _line(command.reason, 200, "reason")
        case p.AgentTranscript():
            _session(command.session)
            _ident(command.agent_id, "agent id")
        case p.ModelsList():
            for label, value in (("provider", command.provider), ("tier", command.tier), ("search", command.search)):
                if value is not None:
                    _line(value, 128, label)
        case p.GitDiff():
            _line(command.ref, 200, "ref")
        case p.PermissionResolve():
            _session(command.session)
            _ident(command.request_id, "request id")
            if not _DECISION.fullmatch(command.decision):
                _bad("invalid decision")
            # The lease holder is the paired device, never a client-chosen id.
            return msgspec.structs.replace(command, client_id=f"gateway:{device_id}")
        case p.QuestionAnswer():
            _session(command.session)
            _text(command.answer, MAX_TEXT_CHARS, "answer")
            if command.call_id:
                _ident(command.call_id, "call id")
            if command.question_id:
                _ident(command.question_id, "question id")
        case p.VoiceCancel():
            if not _REQUEST_ID.fullmatch(command.request_id):
                _bad("invalid request id")
        case p.LiveStart():
            _session(command.session)
            if not command.offer_sdp or len(command.offer_sdp.encode("utf-8")) > MAX_OFFER_SDP_BYTES:
                _bad("invalid offer")
            _line(command.voice, 64, "voice")
        case p.LiveMute() | p.LivePause():
            _session(command.session)
        case p.LiveStop():
            _session(command.session)
            _line(command.reason, 40, "reason")
        case p.LiveHelperEvent():
            _session(command.session)
            if command.kind not in HELPER_KINDS:
                _bad("invalid helper event")
            if len(command.detail) > MAX_HELPER_DETAIL:
                _bad("helper detail is too long")
        case _:
            pass  # field-less commands (lists, status, health) and LiveConsent
    return command


__all__ = [
    "COMMAND_SCOPES",
    "HELPER_KINDS",
    "MAX_COMMAND_BYTES",
    "SESSION_ID",
    "CommandDenied",
    "CommandInvalid",
    "authorize",
    "is_read_only",
    "required_scope",
]
