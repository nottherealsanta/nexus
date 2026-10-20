"""Host projection and dispatch for GPT Live commands (docs/live.md).

``dispatch_live`` maps ``LiveStatus/Start/Mute/Pause/Stop/HelperEvent`` onto the
runtime's ``LiveManager``; ``FacadeLiveHost`` is the per-call seam that lets the
manager (which imports no host code) emit durable events, read earlier
transcripts, observe the session without becoming a viewer, and enqueue a
delegation through ``HostFacade.enqueue``. Results never carry credentials, and
errors are redacted and bounded before they cross the host boundary.
"""
from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

from ..host import protocol as p
from ..live.manager import LiveInfo, LiveManager, SessionContext
from ..live.protocol import LiveError
from ..util import redact_secrets
from ..view import fold
from .live_consent import read_consent, write_consent
from .session_preview import clean_multiline

CONSENT_REQUIRED_MESSAGE = (
    "GPT Live needs your consent first: microphone audio, summaries of the agent's work and the recent "
    "conversation of the current session go to OpenAI. "
    "Accept the notice with /live in a Nexus chat, or run `nexus live`."
)
MANAGER_CONSENT_REQUIRED_MESSAGE = (
    "A call with the manager also sends what the manager reads of your other sessions (titles, status and "
    "final reports) to OpenAI. Accept that notice first: record the consent with scope \"manager\"."
)
MANAGER_DISABLED_MESSAGE = "The manager is turned off. Set [manager] enabled = true to use it."
_COMMANDS = (p.LiveStatus, p.LiveStart, p.LiveMute, p.LivePause, p.LiveStop, p.LiveHelperEvent, p.LiveConsent)
_MAX_SESSION_ID = 200


def live_status_result(info: LiveInfo, *, consent: bool = False) -> p.LiveStatusResult:
    values = dataclasses.asdict(info)
    values["last_error"] = redact_secrets(str(values["last_error"]))[:300]
    return p.LiveStatusResult(**values, consent=consent)


_CONTEXT_STATUS = {"running": "running", "awaiting_input": "awaiting_input", "awaiting_permission": "awaiting_permission"}
_CONTEXT_MAX_ITEMS = 60
_CONTEXT_ITEM_CHARS = 2000


def _context_text(value: Any) -> str:
    text = clean_multiline(value, max_lines=60, max_chars=100_000)
    if len(text) > _CONTEXT_ITEM_CHARS:
        text = text[:_CONTEXT_ITEM_CHARS].rstrip() + " [...]"
    return text


class _Tap:
    """A session event tap that is not a viewer (``Session.observe``)."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._sub = handle.observe()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._sub.__aiter__()

    def close(self) -> None:
        self._handle.unobserve(self._sub)


class FacadeLiveHost:
    """``LiveHost`` over a ``HostFacade``."""

    def __init__(self, facade: Any) -> None:
        self._facade = facade

    def _handle(self, session_id: str) -> Any:
        return self._facade._session(session_id, create=False, recover=False)

    def emit(self, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        self._handle(session_id).emit_session_event(event_type, data)

    def has_session(self, session_id: str) -> bool:
        try:
            return bool(self._facade.runtime.sessions.exists(session_id))
        except Exception:  # noqa: BLE001 - a bad id is simply "no such session"
            return False

    def viewers(self, session_id: str) -> int:
        return int(self._facade.presence.viewers(session_id))

    def agent_name(self, session_id: str) -> str:
        try:
            if self._facade.runtime.sessions.kind_of(session_id) == "manager":
                return "Manager"
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(self._facade.current_agent(session_id)[0]) or "Nexus"
        except Exception:  # noqa: BLE001
            return "Nexus"

    def transcript_turns(self, session_id: str) -> list[tuple[str, str]]:
        """Final turns of earlier calls, restarting after a ``new_conversation`` call."""
        turns: list[tuple[str, str]] = []
        for event in self._handle(session_id).read(force=True).events():
            data = event.data if isinstance(event.data, dict) else {}
            if event.type == "live.started" and data.get("new_conversation"):
                turns.clear()
            elif event.type == "live.transcript" and isinstance(data.get("text"), str):
                role = "assistant" if data.get("role") == "assistant" else "user"
                turns.append((role, data["text"]))
        return turns

    def session_context(self, session_id: str) -> SessionContext:
        """User prompts and final assistant text of the session, from the shared event projection.

        Omits thinking, tool calls and tool output, and the user messages GPT Live itself
        delegated (the voice already knows what it asked). Text is redacted and cleaned.
        """
        try:
            view = fold(self._handle(session_id).read(force=True).events())
        except Exception:  # noqa: BLE001 - a deleted or unreadable session simply has no context
            return SessionContext()
        items: list[tuple[str, str]] = []
        for turn in view.turns:
            for message in turn.messages:
                if message.role == "user":
                    if (message.origin or {}).get("kind") == "live":
                        continue
                    text = _context_text(message.text)
                    if text:
                        items.append(("user", text))
            final = next((m for m in reversed(turn.messages) if m.role == "assistant" and m.done and m.text.strip()), None)
            if final is not None:
                items.append(("assistant", _context_text(final.text)))
        status = _CONTEXT_STATUS.get(view.phase, "")
        if not status and view.turns and view.turns[-1].phase == "failed":
            status = "error"
        return SessionContext(turns=tuple(items[-_CONTEXT_MAX_ITEMS:]), status=status)

    def turn_running(self, session_id: str) -> bool:
        return self._handle(session_id).active_turn_id is not None

    async def enqueue(
        self, session_id: str, text: str, mode: str, origin: dict[str, str], title: str | None = None
    ) -> str:
        # ``title`` is the spoken request alone, so a fresh session is not titled "Voice request ...".
        queued_id, _turn = await self._facade.enqueue(session_id, text, mode=mode, origin=origin, title_text=title)
        return queued_id

    def observe(self, session_id: str) -> _Tap:
        return _Tap(self._handle(session_id))


def _error(exc: Exception) -> p.ErrorResult:
    if isinstance(exc, LiveError):
        return p.ErrorResult(kind=exc.code, message=redact_secrets(exc.message)[:400])
    return p.ErrorResult(kind="live_unavailable", message="GPT Live request failed")


async def dispatch_live(command: Any, facade: Any) -> p.Result | None:
    """Handle Live commands; ``None`` when ``command`` is not one of them."""
    if not isinstance(command, _COMMANDS):
        return None
    home = getattr(facade.runtime, "_home", None)
    if isinstance(command, p.LiveConsent):  # a file, not the call: it needs no manager
        try:
            if command.accept is not None:
                stamp = write_consent(command.accept, home, command.scope)
            else:
                stamp = read_consent(home, command.scope)
        except OSError:
            return p.ErrorResult(kind="live_consent", message="Could not save the GPT Live consent")
        return p.LiveConsentResult(accepted=bool(stamp), accepted_at=stamp, scope=command.scope)
    manager: LiveManager | None = getattr(facade.runtime, "live", None)
    if manager is None:
        return p.ErrorResult(kind="live_unavailable", message="GPT Live is unavailable")
    session = getattr(command, "session", "")
    if isinstance(session, str) and len(session) > _MAX_SESSION_ID:
        return p.ErrorResult(kind="live_no_session", message="Invalid session id")
    try:
        if isinstance(command, p.LiveStatus):
            _refresh(facade, manager)
            return live_status_result(await manager.status(), consent=bool(read_consent(home)))
        if isinstance(command, p.LiveStart):
            _refresh(facade, manager)
            if not read_consent(home):  # the daemon, not only the clients, enforces the notice
                return p.ErrorResult(kind="live_consent_required", message=CONSENT_REQUIRED_MESSAGE)
            refusal = _manager_refusal(facade, command.session, home)
            if refusal is not None:
                return refusal
            result = await manager.start(
                command.session, command.offer_sdp, host=FacadeLiveHost(facade),
                voice=command.voice, new_conversation=command.new_conversation,
            )
            return p.LiveStartResult(
                call_id=result.call_id, answer_sdp=result.answer_sdp, voice=manager.snapshot().voice
            )
        if isinstance(command, p.LiveMute):
            return live_status_result(manager.mute(command.session, command.muted), consent=bool(read_consent(home)))
        if isinstance(command, p.LivePause):
            return live_status_result(manager.pause(command.session, command.paused), consent=bool(read_consent(home)))
        if isinstance(command, p.LiveStop):
            return live_status_result(
                await manager.stop(command.session, command.reason), consent=bool(read_consent(home)))
        info = manager.helper_event(command.session, command.kind, command.detail)
        return p.LiveHelperEventResult(ok=True, state=info.state)
    except Exception as exc:  # noqa: BLE001 - the wire never raises a traceback
        return _error(exc)


def _manager_refusal(facade: Any, session: str, home: Any) -> p.ErrorResult | None:
    """A GPT Live call on the manager session needs the manager enabled and the cross-session consent.

    The call then carries what the manager read of other sessions to OpenAI, which the
    base notice does not cover (docs/manager.md). Any other session is unaffected.
    """
    try:
        kind = facade.runtime.sessions.kind_of(session)
    except Exception:  # noqa: BLE001 - a bad id is rejected later, by the live manager
        return None
    if kind != "manager":
        return p.ErrorResult(kind="live_manager_required", message="GPT Live connects only to the manager session")
    if not facade.manager_config().enabled:
        return p.ErrorResult(kind="manager_disabled", message=MANAGER_DISABLED_MESSAGE)
    if not read_consent(home, "manager"):
        return p.ErrorResult(kind="live_consent_required", message=MANAGER_CONSENT_REQUIRED_MESSAGE)
    return None


def _refresh(facade: Any, manager: LiveManager) -> None:
    """Pick up persisted ``[live]`` changes before a status or start (idle only)."""
    refresh = getattr(facade.runtime, "refresh_live_config", None)
    if callable(refresh) and not manager.active_session:
        try:
            refresh()
        except Exception:  # noqa: BLE001 - keep the last good settings
            pass


__all__ = ["FacadeLiveHost", "dispatch_live", "live_status_result"]
