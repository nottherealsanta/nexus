"""One GPT Live call per daemon: lifecycle, events and cleanup (plan sections 4.1, 6).

Contract:

- At most **one call** exists per daemon (one mic, one speaker). A second
  ``start`` fails with ``live_busy`` naming the session that owns it.
- State machine ``idle -> connecting -> active <-> muted -> closing -> idle``.
- **Exactly one** durable ``live.ended`` per ``live.started``, on every exit path:
  startup failure, explicit stop, transport error, remote close, call deadline,
  client detach (no attached view for ``detach_timeout`` seconds), session
  deleted, helper exit, daemon shutdown. All of them converge on one guarded
  ``_finish``. Ending a call never cancels the agent's running turn.
- The manager imports no session, runtime or host code; the host layer supplies a
  ``LiveHost`` for each call (``host_support/live.py``).
- Credentials live only in the transport. Events, results and logs carry
  redacted, bounded text.
- ``live.transcript.delta`` and ``live.phase`` are persisted coalesced to at most
  4 Hz (the session sink has no non-durable fan-out); levels are not persisted.

Not exercised against the real service: **not verified**.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import msgspec

from ..util import new_id, redact_secrets
from .bridge import LiveBridge
from .protocol import (
    MAX_DELEGATION_CHARS,
    MODEL,
    DelegationCreated,
    ErrorEvent,
    LiveError,
    LiveEvent,
    LiveSession,
    SessionClosed,
    SessionStarted,
    SessionUpdated,
    SidebandOpen,
    SidebandReconnected,
    TranscriptAdded,
    TranscriptDelta,
    TurnDone,
    append_messages,
    resolve_voice,
)
from .transport import LiveTransport, safe_detail

log = logging.getLogger("nexus.live")

MAX_SDP_BYTES = 65_536
MAX_INSTRUCTIONS_BYTES = 32_768
MAX_CARRY_BYTES = 8_192
MAX_TRANSCRIPT_CHARS = 16_000
MAX_CAPTION_CHARS = 4_000
MAX_APPEND_CHARS = 32_000
MAX_PENDING_DELEGATIONS = 8
MAX_HELPER_DETAIL = 2_048
RAW_LOG_MAX_BYTES = 1_048_576
HELPER_KINDS = ("ready", "levels", "phase", "dc_error", "dropped", "closed")
PHASES = ("connecting", "listening", "speaking", "working", "muted")
_FATAL_ERROR_HINT = "authentication"
# Transport send failures that leave the call running: not sent / delivery unknown (never replayed).
RECOVERABLE_SEND_ERRORS = ("live_not_connected", "live_send_ambiguous")
MAX_REJECTION_NOTICES = 4
RECONNECT_NOTICE = (
    "The voice connection was briefly interrupted. Something the user said, an interruption of "
    "your speech, or an update from the agent may have been missed. Do not repeat any earlier "
    "handoff; the user can read the agent's result in the conversation. If it matters, say "
    "honestly that you may have missed something and ask the user to repeat only if necessary."
)
REJECT_NOTICE = (
    "Too many requests are already waiting, so this request was not sent to the agent. Tell the "
    "user briefly and ask them to try again in a moment."
)


class LiveHost(Protocol):
    """The per-call seam to the session layer (implemented in ``host_support/live.py``)."""

    def emit(self, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Persist and publish one turn-less durable session event."""

    def has_session(self, session_id: str) -> bool: ...

    def viewers(self, session_id: str) -> int: ...

    def agent_name(self, session_id: str) -> str: ...

    def transcript_turns(self, session_id: str) -> list[tuple[str, str]]:
        """Final ``(role, text)`` turns of earlier calls since the last ``new_conversation``."""

    def turn_running(self, session_id: str) -> bool: ...

    async def enqueue(self, session_id: str, text: str, mode: str, origin: dict[str, str]) -> str:
        """Queue or steer a user message; return its ``queued_id``."""

    def observe(self, session_id: str) -> EventTap:
        """A non-viewer tap on the session's persisted events."""


class EventTap(Protocol):
    def __aiter__(self) -> AsyncIterator[Any]: ...
    def close(self) -> None: ...


class StartResult(msgspec.Struct, frozen=True):
    call_id: str
    answer_sdp: str


@dataclass(frozen=True)
class LiveInfo:
    """Redacted, bounded snapshot for ``LiveStatus`` / ``LiveMute`` / ``LiveStop``."""

    available: bool = True
    reason: str = ""  # disabled|unsupported|no_credential|busy
    state: str = "idle"  # idle|connecting|active|muted|closing
    phase: str = ""
    active_session: str = ""
    call_id: str = ""
    voice: str = ""
    muted: bool = False
    elapsed_s: float = 0.0
    max_minutes: int = 30
    levels_in: float = 0.0
    levels_out: float = 0.0
    dropped_frames: int = 0
    last_error: str = ""


@dataclass
class _Call:
    call_id: str
    session_id: str
    host: LiveHost
    voice: str
    started: float
    transport: LiveTransport | None = None
    state: str = "connecting"
    phase: str = "connecting"
    muted: bool = False
    ending: asyncio.Task[None] | None = None
    start_task: asyncio.Task[Any] | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    bridge: LiveBridge | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # transcripts
    partial: dict[str, str] = field(default_factory=dict)
    dirty: set[str] = field(default_factory=set)
    turn_count: int = 0
    # coalescing
    emitted_phase: str = ""
    last_flush: float = 0.0
    flush_task: asyncio.Task[None] | None = None
    # helper facts (in memory only)
    levels: tuple[float, float] = (0.0, 0.0)
    dropped_frames: int = 0
    last_error: str = ""
    last_error_ts: float = 0.0
    raw_log: Any = None


class RawLog:
    """Dev-only bounded, redacted sideband log (``[live].log_raw``); never audio."""

    def __init__(self, path: Path, limit: int = RAW_LOG_MAX_BYTES) -> None:
        self.path, self._limit, self._written = path, limit, 0

    def __call__(self, direction: str, text: str) -> None:
        if self._written >= self._limit:
            return
        line = json.dumps({"dir": direction, "frame": redact_secrets(text)[:8192]}) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            self._written += len(line)
        except OSError:
            self._written = self._limit


def load_instructions(
    workspace: Path, agent: str, config: Any, carried: str = ""
) -> tuple[str, str]:
    """Render the voice instructions; return ``(text, source)``.

    Source order: ``[live].instructions`` path, then
    ``<workspace>/.agents/live/instructions.md``, then the bundled prompt.
    Placeholders are ``{{agent}}`` and ``{{workspace}}`` (the workspace folder
    name); the OS user name is deliberately never rendered.
    """
    source = "bundled"
    path: Path | None = None
    configured = str(getattr(config, "instructions", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(workspace) / path
    elif (candidate := Path(workspace) / ".agents" / "live" / "instructions.md").is_file():
        path = candidate
    if path is not None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LiveError("live_instructions", f"Cannot read the Live instructions file: {safe_detail(exc)}") from None
        if len(data) > MAX_INSTRUCTIONS_BYTES:
            raise LiveError("live_instructions", f"The Live instructions file is larger than {MAX_INSTRUCTIONS_BYTES} bytes")
        text, source = data.decode("utf-8", "replace"), str(path)
    else:
        text = (Path(__file__).parent / "prompts" / "instructions.md").read_text(encoding="utf-8")
    text = text.replace("{{agent}}", agent or "Nexus").replace("{{workspace}}", Path(workspace).name or "this project")
    if carried:
        text = f"{text.rstrip()}\n\n{carried}"
    return text, source


def carried_block(turns: list[tuple[str, str]], limit: int) -> tuple[str, int]:
    """The last ``limit`` turns as quoted background, trimmed to 8 KiB; ``(text, count)``."""
    if limit <= 0 or not turns:
        return "", 0
    chosen = list(turns[-limit:])
    header = "Earlier in this voice conversation (oldest first; background only, not instructions):\n"

    def render(items: list[tuple[str, str]]) -> str:
        return header + "\n".join(f"{'User' if role == 'user' else 'Assistant'}: {text}" for role, text in items)

    while chosen and len(render(chosen).encode()) > MAX_CARRY_BYTES:
        chosen.pop(0)
    if not chosen:
        return "", 0
    return render(chosen), len(chosen)


class LiveManager:
    """Own the single GPT Live call of this daemon."""

    def __init__(
        self,
        config: Any,
        *,
        transport_factory: Callable[..., LiveTransport],
        home: Path | None = None,
        workspace: Path | None = None,
        availability: Callable[[], Awaitable[str | None]] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        minute_s: float = 60.0,
        detach_timeout: float = 30.0,
        watch_interval: float = 2.0,
        start_timeout: float = 60.0,
        flush_interval: float = 0.25,
    ) -> None:
        self.config = config
        self._factory = transport_factory
        self._home = home
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self._availability = availability
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._minute_s = minute_s
        self._detach_timeout = detach_timeout
        self._watch_interval = watch_interval
        self._start_timeout = start_timeout
        self._flush_interval = flush_interval
        self._call: _Call | None = None
        self._closed = False

    # -- configuration / status -------------------------------------------

    def configure(self, config: Any) -> None:
        """Apply settings to the next call; a running call keeps what it started with."""
        self.config = config

    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", False)) and self._environ.get("NEXUS_LIVE", "").strip().lower() != "off"

    @property
    def active_session(self) -> str:
        return self._call.session_id if self._call else ""

    def _info(self, call: _Call | None, *, available: bool = True, reason: str = "") -> LiveInfo:
        max_minutes = int(getattr(self.config, "max_minutes", 30))
        if call is None:
            return LiveInfo(available=available, reason=reason, max_minutes=max_minutes)
        return LiveInfo(
            available=False, reason="busy", state=call.state, phase=call.phase,
            active_session=call.session_id, call_id=call.call_id, voice=call.voice, muted=call.muted,
            elapsed_s=round(max(0.0, self._clock() - call.started), 1), max_minutes=max_minutes,
            levels_in=call.levels[0], levels_out=call.levels[1], dropped_frames=call.dropped_frames,
            last_error=call.last_error,
        )

    async def status(self) -> LiveInfo:
        if self._call is not None:
            return self._info(self._call)
        if not self.enabled():
            return self._info(None, available=False, reason="disabled")
        if self._availability is not None:
            try:
                reason = await self._availability()
            except Exception:  # noqa: BLE001 - a probe failure must not break status
                reason = "no_credential"
            if reason:
                return self._info(None, available=False, reason=reason)
        return self._info(None)

    def snapshot(self) -> LiveInfo:
        return self._info(self._call)

    # -- start ---------------------------------------------------------------

    async def start(
        self, session_id: str, offer_sdp: str, *, host: LiveHost, voice: str = "", new_conversation: bool = False
    ) -> StartResult:
        if self._closed:
            raise LiveError("live_unavailable", "The daemon is shutting down")
        if not self.enabled():
            raise LiveError("live_disabled", "GPT Live is disabled ([live].enabled or NEXUS_LIVE=off)")
        if self._call is not None:
            raise LiveError("live_busy", f"A call is already active in session {self._call.session_id}")
        if not isinstance(offer_sdp, str) or not offer_sdp.strip():
            raise LiveError("live_bad_offer", "The SDP offer is empty")
        if len(offer_sdp.encode("utf-8", "replace")) > MAX_SDP_BYTES:
            raise LiveError("live_bad_offer", f"The SDP offer is larger than {MAX_SDP_BYTES} bytes")
        if not host.has_session(session_id):
            raise LiveError("live_no_session", "That session does not exist")
        try:
            chosen = resolve_voice(voice or str(getattr(self.config, "voice", "default")))
        except ValueError as exc:
            raise LiveError("live_bad_voice", str(exc)) from None
        carry = 0 if new_conversation else int(getattr(self.config, "carry_turns", 0))
        block, carried_n = carried_block(host.transcript_turns(session_id) if carry else [], carry)
        instructions, source = load_instructions(self.workspace, host.agent_name(session_id), self.config, block)

        call = _Call(call_id=new_id(), session_id=session_id, host=host, voice=chosen, started=self._clock())
        self._call = call  # reserved before the first await: a concurrent start sees busy
        self._emit(call, "live.started", {
            "model": MODEL, "voice": chosen, "instructions_source": source,
            "instructions": instructions, "carried_turns": carried_n, "new_conversation": bool(new_conversation),
        })
        self._emit(call, "live.phase", {"phase": "connecting"})
        call.emitted_phase = "connecting"
        raw = None
        if getattr(self.config, "log_raw", False) and self._home is not None:
            raw = RawLog(Path(self._home) / "logs" / "live" / f"{call.call_id}.jsonl")
        try:
            call.transport = self._factory(raw_log=raw)
            call.bridge = LiveBridge(
                host=host, session_id=session_id,
                append=lambda text, label, delegation_id: self._append(call, text, label, delegation_id),
                emit=lambda kind, data: self._emit(call, kind, data),
                set_phase=lambda phase: self._set_phase(call, phase),
                progress=bool(getattr(self.config, "progress", True)),
                clock=self._clock,
            )
            call.bridge.start()  # tap the session before media flows, so no event is missed
            call.start_task = asyncio.ensure_future(call.transport.start(
                offer_sdp, LiveSession(instructions=instructions, voice=chosen),
                on_event=lambda event: self._on_event(call, event),
                on_closed=lambda reason, error: self._on_closed(call, reason, error),
            ))
            answer = await asyncio.wait_for(asyncio.shield(call.start_task), self._start_timeout)
        except asyncio.CancelledError:
            self._begin_end(call, "cancelled", None)
            raise
        except TimeoutError:
            error = LiveError("live_timeout", "Timed out starting the call")
            await self._end_and_wait(call, "startup_failed", error.message)
            raise error from None
        except LiveError as exc:
            await self._end_and_wait(call, "startup_failed", exc.message)
            raise
        except Exception as exc:  # noqa: BLE001 - transport text may echo secrets
            message = safe_detail(exc) or "The call could not be started"
            await self._end_and_wait(call, "startup_failed", message)
            raise LiveError("live_signaling", message) from None
        if call.ending is not None:
            await asyncio.shield(call.ending)
            raise LiveError("live_closed", "The call ended while it was starting")
        call.tasks.add(asyncio.create_task(self._deadline(call), name="nexus-live-deadline"))
        call.tasks.add(asyncio.create_task(self._watch(call), name="nexus-live-watch"))
        return StartResult(call_id=call.call_id, answer_sdp=answer.answer_sdp)

    # -- commands ------------------------------------------------------------

    def _owned(self, session_id: str) -> _Call:
        call = self._call
        if call is None or call.ending is not None:
            raise LiveError("live_not_active", "No call is active")
        if call.session_id != session_id:
            raise LiveError("live_busy", f"The active call belongs to session {call.session_id}")
        return call

    def mute(self, session_id: str, muted: bool | None = None) -> LiveInfo:
        call = self._owned(session_id)
        call.muted = (not call.muted) if muted is None else bool(muted)
        if call.state in ("active", "muted"):
            call.state = "muted" if call.muted else "active"
        self._set_phase(call, "muted" if call.muted else "listening")
        return self._info(call)

    async def stop(self, session_id: str, reason: str = "user") -> LiveInfo:
        """Idempotent: an idle manager returns the idle snapshot."""
        call = self._call
        if call is None:
            return self._info(None)
        if call.session_id != session_id:
            raise LiveError("live_busy", f"The active call belongs to session {call.session_id}")
        await self._end_and_wait(call, (reason or "user")[:40], None)
        return self._info(None)

    def helper_event(self, session_id: str, kind: str, detail: str = "") -> LiveInfo:
        """A forwarded event from the client's audio helper (``closed``, ``dc_error`` ...)."""
        if kind not in HELPER_KINDS:
            raise LiveError("live_bad_event", f"Unknown helper event {kind!r}")
        call = self._owned(session_id)
        text = safe_detail(detail, MAX_HELPER_DETAIL)
        if kind == "closed":
            self._begin_end(call, "helper_closed", text or None)
        elif kind == "dc_error":
            self._note_error(call, f"audio helper: {text or 'data channel error'}")
        elif kind == "dropped":
            with contextlib.suppress(ValueError):
                call.dropped_frames += max(0, min(int(text), 1_000_000))
        elif kind == "levels":
            try:
                parsed = json.loads(detail or "{}")
                call.levels = (_unit(parsed.get("in")), _unit(parsed.get("out")))
            except (ValueError, AttributeError, TypeError):
                pass
        elif kind == "phase" and text in ("listening", "speaking") and not call.muted and call.state == "active":
            self._set_phase(call, text)
        return self._info(call)

    async def shutdown(self) -> None:
        """Daemon shutdown: end any call (``live.ended`` reason ``daemon_shutdown``)."""
        self._closed = True
        call = self._call
        if call is not None:
            await self._end_and_wait(call, "daemon_shutdown", None)

    # -- ending --------------------------------------------------------------

    def _begin_end(self, call: _Call, reason: str, error: str | None) -> asyncio.Task[None]:
        if call.ending is None:
            call.state = "closing"
            call.ending = asyncio.ensure_future(self._finish(call, reason, error))
        return call.ending

    async def _end_and_wait(self, call: _Call, reason: str, error: str | None) -> None:
        await asyncio.shield(self._begin_end(call, reason, error))

    async def _finish(self, call: _Call, reason: str, error: str | None) -> None:
        try:
            me = asyncio.current_task()
            pending = [t for t in (*call.tasks, call.flush_task, call.start_task) if t is not None and t is not me]
            for task in pending:
                task.cancel()
            # Mark the transport terminal in the same loop turn as the cancels: a send cancelled
            # mid-write invalidates its socket, and that must not start a reconnect that races
            # this intentional stop.
            closer = (
                asyncio.ensure_future(asyncio.wait_for(call.transport.close(reason), 3.0))
                if call.transport is not None else None
            )
            if call.bridge is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(call.bridge.close(), 2.0)
            for task in pending:
                with contextlib.suppress(BaseException):
                    await task
            if closer is not None:
                with contextlib.suppress(BaseException):
                    await closer
        finally:
            duration = round(max(0.0, self._clock() - call.started), 1)
            self._emit(call, "live.ended", {
                "reason": reason, "duration_s": duration,
                "error": (redact_secrets(error)[:500] if error else None),
            })
            if self._call is call:
                self._call = None
            call.state = "idle"

    async def _deadline(self, call: _Call) -> None:
        await asyncio.sleep(max(1, int(getattr(self.config, "max_minutes", 30))) * self._minute_s)
        self._begin_end(call, "deadline", None)

    async def _watch(self, call: _Call) -> None:
        """End the call when the session is gone or no view has held it for the detach timeout."""
        detached_since: float | None = None
        while call.ending is None:
            await asyncio.sleep(self._watch_interval)
            try:
                if not call.host.has_session(call.session_id):
                    self._begin_end(call, "session_deleted", None)
                    return
                if call.host.viewers(call.session_id) > 0:
                    detached_since = None
                else:
                    now = self._clock()
                    detached_since = now if detached_since is None else detached_since
                    if now - detached_since >= self._detach_timeout:
                        self._begin_end(call, "client_detached", None)
                        return
            except Exception:  # noqa: BLE001 - a probe failure never ends a call by itself
                log.debug("live watchdog probe failed", exc_info=True)

    # -- events out ------------------------------------------------------------

    def _emit(self, call: _Call, event_type: str, data: dict[str, Any]) -> None:
        payload = {"call_id": call.call_id, **data}
        try:
            call.host.emit(call.session_id, event_type, payload)
        except Exception as exc:  # noqa: BLE001 - e.g. the session was deleted mid-call
            log.warning("live event %s not recorded: %s", event_type, safe_detail(exc))

    def _note_error(self, call: _Call, message: str) -> None:
        now = self._clock()
        call.last_error = redact_secrets(message)[:300]
        if now - call.last_error_ts >= 1.0:  # at most one durable error row per second
            call.last_error_ts = now
            self._emit(call, "live.error", {"message": call.last_error})

    def _set_phase(self, call: _Call, phase: str) -> None:
        if call.ending is not None or phase not in PHASES or phase == call.phase:
            return
        call.phase = phase
        self._kick(call)

    def _kick(self, call: _Call) -> None:
        """Flush pending phase/caption now, or schedule one flush so the rate stays <= 4 Hz."""
        if call.flush_task is not None and not call.flush_task.done():
            return
        wait = call.last_flush + self._flush_interval - self._clock()
        if wait <= 0:
            self._flush(call)
        else:
            call.flush_task = asyncio.create_task(self._flush_later(call, wait), name="nexus-live-flush")

    async def _flush_later(self, call: _Call, wait: float) -> None:
        await asyncio.sleep(wait)
        self._flush(call)

    def _flush(self, call: _Call) -> None:
        if call.ending is not None:
            return
        call.last_flush = self._clock()
        if call.phase != call.emitted_phase:
            call.emitted_phase = call.phase
            self._emit(call, "live.phase", {"phase": call.phase})
        for role in sorted(call.dirty):
            self._emit(call, "live.transcript.delta", {"role": role, "text": call.partial.get(role, "")[-MAX_CAPTION_CHARS:]})
        call.dirty.clear()

    def _record_error(self, call: _Call, message: str) -> None:
        """A durable ``live.error`` that is never rate-limited away (rare, user-visible warnings)."""
        call.last_error = redact_secrets(message)[:300]
        call.last_error_ts = self._clock()
        self._emit(call, "live.error", {"message": call.last_error})

    async def _append(self, call: _Call, text: str, label: str, delegation_id: str | None) -> None:
        """Send text to the voice model (chunked) and record exactly the confirmed prefix.

        Chunks are serialized under ``call.send_lock``. A failed chunk is never replayed: the
        recorded ``live.appended`` covers only chunks the transport confirmed. A recoverable
        failure (``live_not_connected``: not sent; ``live_send_ambiguous``: delivery unknown)
        is recorded as ``live.error`` and the call continues; anything else ends the call.
        """
        text = text[:MAX_APPEND_CHARS]
        if not text.strip() or call.ending is not None or call.transport is None:
            return
        channel = {"commentary": "commentary", "notice": "speakable"}.get(label)
        messages = append_messages(text, channel=channel, delegation_id=delegation_id)
        parts = [m["content"][0]["text"] for m in messages]
        confirmed = 0
        failure: LiveError | None = None
        try:
            async with call.send_lock:
                if call.ending is not None or call.transport is None:  # ended while we waited
                    return
                for message in messages:
                    if call.ending is not None:
                        break
                    try:
                        await call.transport.send(message)
                    except LiveError as exc:
                        failure = exc
                        break
                    confirmed += 1
        finally:
            if confirmed:  # also on cancellation: the prefix really was sent
                self._emit(call, "live.appended", {
                    "delegation_id": delegation_id, "channel": label,
                    "text": "".join(parts[:confirmed]), "chunks": confirmed,
                })
        if failure is None:
            return
        if failure.code in RECOVERABLE_SEND_ERRORS:
            if failure.code == "live_send_ambiguous":
                state = f"part {confirmed + 1} may or may not have arrived and was not retried"
            else:
                state = f"part {confirmed + 1} was not sent"
            self._record_error(call, (
                f"A {label} update to the voice model was interrupted after {confirmed} of {len(messages)} "
                f"parts: {state}; the remaining parts were not sent. Nothing is replayed automatically."
            ))
            return
        self._begin_end(call, "transport_error", failure.message)

    def _spawn_notice(
        self, call: _Call, name: str, limit: int, text: str, delegation_id: str | None = None
    ) -> bool:
        """Send a speakable notice from a tracked task; at most ``limit`` such tasks in flight."""
        if call.ending is not None or sum(1 for t in call.tasks if t.get_name() == name) >= limit:
            return False
        task = asyncio.create_task(self._append(call, text, "notice", delegation_id), name=name)
        call.tasks.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            call.tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.debug("live notice failed: %s", safe_detail(finished.exception()))

        task.add_done_callback(_done)
        return True

    # -- events in -------------------------------------------------------------

    def _on_closed(self, call: _Call, reason: str, error: str | None) -> None:
        self._begin_end(call, reason, error)

    def _on_event(self, call: _Call, event: LiveEvent) -> None:
        if call.ending is not None:
            return
        if isinstance(event, SidebandOpen):
            if call.state == "connecting":
                call.state = "muted" if call.muted else "active"
                self._set_phase(call, "muted" if call.muted else "listening")
        elif isinstance(event, (SessionStarted, SessionUpdated)):
            if call.phase == "connecting":
                self._set_phase(call, "muted" if call.muted else "listening")
        elif isinstance(event, SidebandReconnected):
            self._on_reconnected(call, event)
        elif isinstance(event, SessionClosed):
            self._begin_end(call, "remote_closed", event.reason or None)
        elif isinstance(event, TranscriptAdded):
            self._merge_partial(call, event.role, event.text, cumulative=True)
        elif isinstance(event, TranscriptDelta):
            self._merge_partial(call, event.role, event.delta, cumulative=False)
        elif isinstance(event, TurnDone):
            self._final_turn(call, event)
        elif isinstance(event, DelegationCreated):
            self._delegate(call, event)
        elif isinstance(event, ErrorEvent):
            self._note_error(call, event.message)
            if event.fatal or event.status in (401, 403):
                self._begin_end(call, "error", event.message)

    def _on_reconnected(self, call: _Call, event: Any) -> None:
        """The transport re-opened the sideband: warn the user, tell the model, repeat nothing."""
        gap = max(0.0, float(getattr(event, "gap_s", 0.0) or 0.0))
        self._record_error(call, (
            f"The voice connection was interrupted for about {gap:.1f}s and restored. "
            "Something you said, or an interruption of the voice, may have been missed."
        ))
        # Not awaited here: the transport reader calls us, and a send would wait on that reader.
        self._spawn_notice(call, "nexus-live-reconnect", 1, RECONNECT_NOTICE)

    def _merge_partial(self, call: _Call, role: str, text: str, *, cumulative: bool) -> None:
        if role not in ("user", "assistant") or not text:
            return
        previous = call.partial.get(role, "")
        if cumulative and text.startswith(previous):
            merged = text
        elif cumulative and previous.endswith(text):
            merged = previous
        else:
            merged = previous + text
        call.partial[role] = merged[-MAX_TRANSCRIPT_CHARS:]
        call.dirty.add(role)
        if role == "assistant" and not call.muted:
            self._set_phase(call, "speaking")
        elif role == "user" and call.phase == "speaking":
            self._set_phase(call, "listening")
        self._kick(call)

    def _final_turn(self, call: _Call, event: TurnDone) -> None:
        text = event.transcript or call.partial.get(event.role, "")
        call.partial.pop(event.role, None)
        call.dirty.discard(event.role)
        if text.strip():
            call.turn_count += 1
            clipped = text[:MAX_TRANSCRIPT_CHARS]
            self._emit(call, "live.transcript", {
                "role": event.role, "text": clipped, "turn": call.turn_count,
                "truncated": len(clipped) < len(text),
            })
        if event.role == "assistant" and call.phase == "speaking":
            self._set_phase(call, "muted" if call.muted else "listening")

    def _delegate(self, call: _Call, event: DelegationCreated) -> None:
        bridge = call.bridge
        if bridge is None:
            return
        if sum(1 for t in call.tasks if t.get_name() == "nexus-live-delegation") >= MAX_PENDING_DELEGATIONS:
            self._note_error(call, "too many pending delegations; ignored one request")
            # bounded: beyond MAX_REJECTION_NOTICES in flight the durable error above is all there is
            self._spawn_notice(call, "nexus-live-reject", MAX_REJECTION_NOTICES, REJECT_NOTICE, event.id)
            return
        task = asyncio.create_task(
            bridge.on_delegation(event.id, event.text[:MAX_DELEGATION_CHARS]), name="nexus-live-delegation"
        )
        call.tasks.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            call.tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                self._note_error(call, f"delegation failed: {safe_detail(finished.exception())}")

        task.add_done_callback(_done)


def _unit(value: Any) -> float:
    return round(min(1.0, max(0.0, float(value))), 3) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


__all__ = [
    "HELPER_KINDS", "MAX_SDP_BYTES", "EventTap", "LiveHost", "LiveInfo", "LiveManager", "RawLog",
    "StartResult", "carried_block", "load_instructions",
]
