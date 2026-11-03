"""The Manager service: one per daemon, above every project (MANAGER_PLAN.md sections 5 to 7 and 10.7).

The Manager *is* a hidden session (``Session.kind == "manager"``), one row per
episode linked by ``continued_from``, so the loop, retries, provider auth, wire
log, usage ledger and recovery are the ones every session has. ``Runtime._agent``
hands such a session to :meth:`ManagerService.build_agent`, which supplies the
code-defined prompt and tools. Nothing here is reachable from a user file.

One call at a time. A client starts a call with the id of its event stream (the
bearer capability from ``server.connected``) and becomes the *holder*; starting
from another client moves the call. Only the holder's stream is accepted by the
routes that take one, and only the holder's view and navigation count.

Events (all carry ``call_id``, never a ``stream_id``): ``manager.state``,
``manager.caption``, ``manager.speech``, ``manager.card``, ``manager.navigate``.
They are ordinary durable global events, so a reconnecting client resumes them
from its cursor and ``GET /api/manager`` restores the call state.

Seams for the next steps (no voice code here): ``manager.speech`` carries one
sentence at a time for a speech-synthesis client; ``wake()`` starts a turn with
no authority for the attention queue.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import ConfigFile
from ..llmhub.types import Message, Request, Text
from ..loop import Agent
from ..paths import home
from ..store import Project, Session
from . import context as ctx
from .attention import Attention
from .consent import Consent
from .context import WorldRow
from .live.media import FRAME_SAMPLES, RATE
from .live.protocol import LiveError
from .live.voice import LiveVoice
from .redact import redact
from .retrieval import HandleRegistry, Retriever
from .stage import Stage
from .state import SessionFacts, session_state
from .topics import Topics, model_writer
from .turn import RateLimiter, SentenceSplitter, TurnState
from .view import HolderView, View

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

MAX_UTTERANCE_CHARS = 4000
LIVE_CONSENT_REF = "codex/gpt-live"
AUDIO_FORMAT = {"path": "/api/manager/live/audio", "encoding": "pcm_s16le", "rate": RATE, "channels": 1,
                "frame_samples": FRAME_SAMPLES}
MAX_STEPS = 10  # tool rounds in one Manager turn; a voice turn that needs more should ask
EPISODE_PREF = "manager.episode"


class Forbidden(Exception):
    """The caller is not the holder of the call. The message names no ids."""


@dataclass
class Call:
    call_id: str
    stream_id: str  # the holder's bearer capability: never leaves this object
    client_id: str
    started: float
    phase: str = "idle"  # idle | thinking | replying
    error: dict[str, str] | None = None
    listening: bool = False
    speaking: bool = False
    turns: int = 0
    active: bool = True


@dataclass
class Utterance:
    turn_id: str
    text: str
    source: str
    delegation_id: str | None = None


Narrator = Callable[[str], Awaitable[str]]


class ManagerService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.clock: Callable[[], float] = time.time
        self.handles = HandleRegistry(self.store.handles(), on_assign=self.store.save_handle)
        self.retriever = Retriever(self.store, handles=self.handles, clock=lambda: self.clock())
        self.topics = Topics(self.store, writer=None)
        self.narrator: Narrator | None = None  # None: one call on the Manager's model; tests inject a function
        self.holder_view = HolderView()
        self.limiter = RateLimiter(lambda: self.clock())
        self.consent = Consent(self.store, lambda: self.clock())
        self.attention = Attention(self)
        self.last_activity = 0.0  # when the user last spoke, typed, or the client last reported audio
        self.proactive_notes: list[str] = []  # what was said or shown unprompted, told to the Manager next turn
        self.stage = Stage(self._publish_card)
        self.call: Call | None = None
        self.turn: TurnState | None = None
        self.views: Any = None
        self.presented: dict[str, tuple[str, int]] = {}  # pending approval/question id -> (fingerprint, utterance index)
        self.baseline: dict[str, tuple[str, str]] | None = None
        self.baseline_episode: str | None = None
        self.told_view: tuple[str | None, str | None, str | None] | None = None
        self._queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._tools: Any = None
        self._lock = asyncio.Lock()
        self._topic_hook_ready = False
        self.live: LiveVoice | None = None
        self.live_factory: Callable[[], Any] | None = None  # tests inject a fake transport
        self.peer_factory: Callable[[], Any] | None = None  # ... and a fake WebRTC peer

    # -- wiring ---------------------------------------------------------------

    def attach_views(self, views: Any) -> None:
        """Connect the server's per-stream view store (``view_for``, ``has_stream``, ``on_detach``)."""
        self.views = views
        self.holder_view.views = views
        views.on_detach(self.on_stream_closed)

    @property
    def tools(self) -> Any:
        if self._tools is None:
            from .tools import ManagerTools

            self._tools = ManagerTools(self)
        return self._tools

    def settings(self) -> dict[str, Any]:
        return ConfigFile().read()["values"]["manager"]

    # -- the call -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        call = self.call
        return {"enabled": bool(settings["enabled"]), "mode": settings["mode"], "model": settings["model"],
                "consent": self.consent.status(settings["model"]),
                "live_consent": self.consent.status(LIVE_CONSENT_REF) if settings["mode"] == "live" else None,
                "call": None if call is None or not call.active else {
                    "call_id": call.call_id, "holder": call.client_id, "phase": call.phase,
                    "error": call.error, "card": self.stage.payload,
                    "live": self.live.info() if self.live is not None else None}}

    def _state_data(self, call: Call) -> dict[str, Any]:
        return {"call_id": call.call_id, "active": call.active, "phase": call.phase if call.active else "idle",
                "holder": call.client_id, "mode": self.settings()["mode"], "error": call.error}

    def _publish_state(self, call: Call) -> None:
        self.bus.publish("manager.state", self._state_data(call))

    def _publish(self, kind: str, data: dict[str, Any]) -> None:
        call = self.call
        if call is None or not call.active:
            return
        if kind == "manager.speech" and self.live is not None:
            # GPT-Live speaks for itself: the sentence goes to the voice model, and the stage only shows it.
            if not data.get("proactive"):
                self.live.speak(str(data.get("text") or ""), self._live_delegation(data))
            else:
                self.live.speak(str(data.get("text") or ""), None)
            kind, data = "manager.caption", {"turn_id": data.get("turn_id"), "role": "manager",
                                             "text": data.get("text"), "final": True}
        self.bus.publish(kind, {"call_id": call.call_id, **data})

    def _live_delegation(self, data: dict[str, Any]) -> str | None:
        turn = self.turn
        return turn.delegation_id if turn is not None and turn.turn_id == data.get("turn_id") else None

    def _publish_card(self, card: dict[str, Any] | None) -> None:
        self._publish("manager.card", {"card": card})

    def _holder(self, stream_id: Any) -> Call:
        call = self.call
        if (call is None or not call.active or not isinstance(stream_id, str)
                or not secrets.compare_digest(stream_id.encode(), call.stream_id.encode())):
            raise Forbidden("this client does not hold the Manager call")
        return call

    def require_holder(self, stream_id: str) -> None:
        """Raise :class:`Forbidden` unless ``stream_id`` is the holder's (for routes outside ``/api/manager``)."""
        self._holder(stream_id)

    async def start(self, stream_id: str, client_id: str) -> dict[str, Any]:
        """Start the call on this client, or move it here from another. Same holder again: the same call."""
        from ..runtime import Conflict

        settings = self.settings()
        if not settings["enabled"]:
            raise Conflict("the Manager is off; turn it on in Settings, Manager ([manager] enabled)",
                           code="manager_disabled")
        if self.views is None or not self.views.has_stream(stream_id):
            raise Conflict("the event stream for this call is not connected; reconnect and start again",
                           code="stream_closed")
        self._require_consent(Conflict)
        async with self._lock:
            call = self.call
            if call is not None and call.active and call.stream_id == stream_id:
                call.client_id = client_id
                return self._start_result(call)
            if call is not None and call.active:
                await self._end(call, publish=True)  # the previous holder sees the new call_id and "Moved to ..."
            call = Call(call_id=f"call_{secrets.token_hex(8)}", stream_id=stream_id, client_id=client_id,
                        started=self.clock())
            self.call = call
            self.holder_view.reset()
            self.presented.clear()
            self.told_view = None
            self.stage.card = None
            self._queue = asyncio.Queue()
            self._worker = asyncio.ensure_future(self._work(call))
            self._publish_state(call)
            self.proactive_notes.clear()
            self.attention.start()  # opens with what is pending, if anything
            return self._start_result(call)

    def _require_consent(self, conflict: type[Exception]) -> None:
        settings = self.settings()
        refs = [settings["model"]]
        if settings["mode"] == "live":
            refs.append(LIVE_CONSENT_REF)  # GPT-Live also sends the user's voice and the Manager's words to OpenAI
        for ref in refs:
            status = self.consent.status(ref)
            if status["required"]:
                raise conflict("the Manager sends session text to a provider; acknowledge the privacy notice first "
                               "(POST /api/manager/consent)", code="consent_required", payload={"consent": status})

    def _start_result(self, call: Call) -> dict[str, Any]:
        return {"call_id": call.call_id, "holder": call.client_id, "mode": self.settings()["mode"],
                "model": self.settings()["model"]}

    async def stop(self, stream_id: str) -> None:
        """End the call. Never stops a session."""
        async with self._lock:
            call = self._holder(stream_id)
            await self._end(call, publish=True)

    async def _end(self, call: Call, *, publish: bool) -> None:
        call.active = False
        call.phase = "idle"
        await self._close_live()
        self.attention.stop()
        self.stage.card = None
        self.holder_view.reset()
        worker, self._worker = self._worker, None
        episode = self._episode_id()
        if self.turn is not None and episode is not None:
            self.turn.cancelled = True
            await self.runtime.interrupt(episode)
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if publish:
            self._publish_state(call)

    def on_stream_closed(self, stream_id: str) -> None:
        """The holder's event stream closed: the call ends (a session is never touched)."""
        call = self.call
        if call is not None and call.active and call.stream_id == stream_id:
            asyncio.ensure_future(self._closed(call))

    async def _closed(self, call: Call) -> None:
        async with self._lock:
            if self.call is call and call.active:
                await self._end(call, publish=True)

    async def live_start(self, stream_id: str) -> dict[str, Any]:
        """GPT-Live: Core opens the call (its own WebRTC peer to OpenAI). The client then sends and gets PCM over the audio socket."""
        from ..runtime import Conflict

        async with self._lock:
            call = self._holder(stream_id)
            if self.settings()["mode"] != "live":
                raise Conflict("the Manager is not in live mode ([manager] mode = \"live\")", code="live_mode_off")
            status = self.consent.status(LIVE_CONSENT_REF)
            if status["required"]:
                raise Conflict("GPT-Live sends your voice and the Manager's words to OpenAI; acknowledge the privacy "
                               "notice first (POST /api/manager/consent with provider \"codex\")", code="consent_required",
                               payload={"consent": status})
            if self.live is not None:
                raise Conflict("a GPT-Live call is already open for this Manager call", code="live_busy")
            voice = LiveVoice(self, call, self.live_factory, self.peer_factory)
            try:
                await voice.open()
            except LiveError as exc:
                raise Conflict(exc.message, code=exc.code) from None
            self.live = voice
            self._set_error(call, None)
            return {"call_id": call.call_id, "audio": AUDIO_FORMAT}

    def live_voice(self, stream_id: str) -> LiveVoice:
        """The open GPT-Live voice for the holder's stream, for the audio socket."""
        self._holder(stream_id)
        if self.live is None:
            raise Forbidden("no GPT-Live call is open")
        return self.live

    def live_utterance(self, call: Call, text: str, delegation_id: str) -> None:
        """The voice model delegated: the user's own transcript is an utterance with authority."""
        if self.call is not call or not call.active:
            return
        self.last_activity = self.clock()
        turn_id = f"mt_{secrets.token_hex(6)}"
        self._publish("manager.caption", {"turn_id": turn_id, "role": "user", "text": text, "final": True})
        self._queue.put_nowait(Utterance(turn_id, text, "speech", delegation_id))

    def live_ended(self, voice: LiveVoice) -> None:
        """The voice call ended by itself (lost, closed by the service): the Manager call goes on, with the error shown."""
        if self.live is voice:
            self.live = None

    async def _close_live(self) -> None:
        voice, self.live = self.live, None
        if voice is not None:
            await voice.close("done")

    def activity(self, stream_id: str, listening: bool, speaking: bool) -> None:
        call = self._holder(stream_id)
        call.listening, call.speaking = bool(listening), bool(speaking)
        if listening or speaking:
            self.last_activity = self.clock()
        else:
            self.attention.on_quiet()  # the user went quiet: a held line may go

    def dismiss_card(self, stream_id: str, card_id: str) -> bool:
        self._holder(stream_id)
        return self.stage.dismiss(card_id)

    def navigate_result(self, stream_id: str, request_id: str, ok: bool, reason: str | None) -> bool:
        self._holder(stream_id)
        return self.holder_view.result(stream_id, request_id, ok, reason)

    def say(self, stream_id: str, text: Any, source: Any) -> dict[str, Any]:
        """Queue one user utterance. Returns at once; the turn's words arrive as events."""
        from ..runtime import Conflict

        call = self._holder(stream_id)
        if source not in ("typed", "speech"):
            raise ValueError("source must be 'typed' or 'speech'")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if len(text) > MAX_UTTERANCE_CHARS:
            raise ValueError(f"text is {len(text)} characters; the limit is {MAX_UTTERANCE_CHARS}")
        if not self.settings()["enabled"]:
            raise Conflict("the Manager is off", code="manager_disabled")
        self._require_consent(Conflict)
        self.last_activity = self.clock()
        return self._enqueue(call, text.strip(), source)

    def _enqueue(self, call: Call, text: str, source: str) -> dict[str, Any]:
        turn_id = f"mt_{secrets.token_hex(6)}"
        if source == "typed":
            self._publish("manager.caption", {"turn_id": turn_id, "role": "user", "text": text, "final": True})
        self._queue.put_nowait(Utterance(turn_id, text, source))
        return {"turn_id": turn_id}

    def wake(self, text: str) -> dict[str, Any] | None:
        """Seam for the attention queue (M4): a turn with no user words, which may inform and ask but never act."""
        call = self.call
        if call is None or not call.active:
            return None
        turn_id = f"mt_{secrets.token_hex(6)}"
        self._queue.put_nowait(Utterance(turn_id, text, "wake"))
        return {"turn_id": turn_id}

    async def cancel(self, stream_id: str) -> dict[str, Any]:
        """Barge-in: stop speech now; cancel the turn only if nothing has had side effects."""
        call = self._holder(stream_id)
        turn = self.turn
        dropped = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            dropped += 1
        if turn is None:
            call.speaking = False
            self.attention.on_barge_in()
            return {"cancelled": False, "muted": False, "dropped": dropped}
        turn.muted = True
        call.speaking = False
        if self.live is not None:
            await self.live.stop_speaking()
        self.attention.on_barge_in()
        if turn.acted:
            return {"cancelled": False, "muted": True, "dropped": dropped,
                    "reason": "the turn already changed a session, so it keeps going"}
        turn.cancelled = True
        episode = self._episode_id()
        if episode is not None:
            await self.runtime.interrupt(episode)
        call.speaking = False
        return {"cancelled": True, "muted": True, "dropped": dropped}

    # -- the worker -----------------------------------------------------------

    async def _work(self, call: Call) -> None:
        while call.active:
            utterance = await self._queue.get()
            if not call.active:
                return
            await self._run_turn(call, utterance)

    def _set_phase(self, call: Call, phase: str) -> None:
        if call.active and call.phase != phase:
            call.phase = phase
            self._publish_state(call)

    def _set_error(self, call: Call, error: dict[str, str] | None) -> None:
        if call.error != error:
            call.error = error
            self._publish_state(call)

    async def _run_turn(self, call: Call, utterance: Utterance) -> None:
        call.turns += 1
        turn = TurnState(turn_id=utterance.turn_id, source=utterance.source, heard=utterance.text, index=call.turns,
                         delegation_id=utterance.delegation_id)
        self.turn = turn
        self._set_phase(call, "thinking")
        try:
            await self._execute(call, turn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("manager turn failed")
            self._fail(call, turn, {"code": "manager_error", "message": f"{type(exc).__name__}: {exc}",
                                    "fix": "Try again; if it keeps failing, run nexus doctor."})
        finally:
            self.turn = None
            if self.live is not None:
                self.live.turn_done(turn.delegation_id, bool(turn.spoken), call.error["message"] if call.error and not turn.spoken else None)
            self._set_phase(call, "idle")
            self.attention.on_quiet()

    def _fail(self, call: Call, turn: TurnState, error: dict[str, str]) -> None:
        self._set_error(call, error)
        text = f"{error['message']} {error['fix']}".strip()
        self._publish("manager.caption", {"turn_id": turn.turn_id, "role": "manager", "text": text, "final": True})

    async def _execute(self, call: Call, turn: TurnState) -> None:
        episode, first, recap = await self._admit()
        turn.episode_id = episode.id
        rows = self.world()
        snapshot = delta = None
        if first or self.baseline is None or self.baseline_episode != episode.id:
            snapshot = ctx.snapshot_text(rows, now=self.clock())
        else:
            delta = ctx.delta_text(self.baseline, rows)
        self.baseline, self.baseline_episode = ctx.remember(rows), episode.id
        text = ctx.turn_input(
            utterance=turn.heard, source=turn.source, snapshot=snapshot, delta=delta, recap=recap,
            view=self._view_for_turn(call), dismissed=self.stage.take_dismissed(),
            extra=[*self.proactive_notes,
                   *([("This turn was started by a notification, not by the user speaking. You may inform and ask; "
                      "you may not act.")] if not turn.authority else [])] or None)
        self.proactive_notes.clear()
        subscription = self.bus.open()
        pump = asyncio.ensure_future(self._pump(call, turn, subscription, episode.id))
        try:
            started = self.runtime.prompt(episode.id, text)
            turn.runtime_turn_id = started["turn_id"]
            task = self.runtime.turns.get(episode.id)
            if task is not None:
                await asyncio.wait({task})
        finally:
            subscription.close()
            await asyncio.gather(pump, return_exceptions=True)
        record = self.store.turn(turn.runtime_turn_id) if turn.runtime_turn_id else None
        if record is None:
            return
        if record["status"] == "failed":
            self._fail(call, turn, self._error_for(record.get("error") or {}))
        elif record["status"] == "completed":
            self._set_error(call, None)

    @staticmethod
    def _error_for(error: dict[str, Any]) -> dict[str, str]:
        kind = str(error.get("kind") or "")
        message = str(error.get("message") or "the Manager's model failed")
        if kind == "auth":
            return {"code": "model_unavailable", "message": message,
                    "fix": "Sign in again with nexus login, or choose another model in Settings, Manager."}
        if kind == "provider":
            return {"code": "model_failed", "message": message,
                    "fix": "Try again in a moment, or choose another model in Settings, Manager."}
        return {"code": "model_unavailable", "message": message,
                "fix": "Choose a model in Settings, Manager."}

    async def _pump(self, call: Call, turn: TurnState, subscription: Any, episode_id: str) -> None:
        """Turn the episode's streamed text into ``manager.speech``, one sentence at a time."""
        splitter = SentenceSplitter()
        fed = False

        def emit(sentence: str) -> None:
            if turn.muted or not call.active:
                return
            self._set_phase(call, "replying")
            sentence = redact(sentence)
            turn.spoken.append(sentence)
            self._publish("manager.speech", {"turn_id": turn.turn_id, "seq": turn.seq, "text": sentence,
                                             "urgent": False})
            turn.seq += 1
            self.attention.on_sentence_break()

        while (event := await subscription.get()) is not None:
            if event.get("session_id") != episode_id:
                continue
            data = event.get("data") or {}
            if event["type"] == "text_delta":
                fed = True
                for sentence in splitter.feed(str(data.get("text") or "")):
                    emit(sentence)
            elif event["type"] == "message.added" and data.get("role") == "assistant":
                if not fed:  # a provider that sent the text whole, without deltas
                    whole = "".join(p.get("text", "") for p in data.get("parts", []) if p.get("type") == "text")
                    for sentence in splitter.feed(whole):
                        emit(sentence)
                for sentence in splitter.flush():
                    emit(sentence)
                fed = False

    def record_proactive(self, note: str, session_ids: list[str], line: str) -> None:
        """Remember an unprompted line (and card) for the Manager's next turn, and in the audit."""
        self.proactive_notes.append(note)
        del self.proactive_notes[:-5]
        targets = ", ".join(self.retriever.handle_for(s) for sid in session_ids[:3] if (s := self.store.session(sid)))
        self.store.add_audit(tool="proactive", target=targets or None, heard=None, ok=True, result=redact(line),
                             episode_id=self._episode_id())

    def _view_for_turn(self, call: Call) -> str | None:
        """One sentence about the holder's screen, only when it changed since it was last told."""
        view = self.holder_view.current(call.stream_id)
        key = (view.project, view.session, view.panel) if view else None
        if key == self.told_view and self.told_view is not None:
            return None
        if view is None and self.told_view is None:
            self.told_view = None
            return ctx.view_line(None, names={})
        self.told_view = key
        return ctx.view_line(view.as_dict() if view else None, names=self._view_names(view))

    def _view_names(self, view: View | None) -> dict[str, str]:
        names: dict[str, str] = {}
        if view is None:
            return names
        project = self.store.project(view.project) if view.project else None
        if project is not None:
            names["project"] = project.name
        session = self.store.session(view.session) if view.session else None
        if session is not None and session.kind == "user":
            names["session"] = session.title
            names["handle"] = self.retriever.handle_for(session)
        return names

    # -- episodes -------------------------------------------------------------

    def _episode_id(self) -> str | None:
        return self.store.preference(EPISODE_PREF)

    def current_episode(self) -> Session | None:
        episode_id = self._episode_id()
        session = self.store.session(episode_id) if episode_id else None
        return session if session is not None and session.kind == "manager" else None

    def episodes(self) -> list[Session]:
        """Every Manager episode, newest first. The only place hidden sessions are listed."""
        return self.store.sessions(kind="manager")

    def _project(self) -> Project:
        path = home() / "manager"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        project, _ = self.store.project_for_path(str(path), "Manager", hidden=True)
        return project

    def _snapshot(self, ref: str) -> dict[str, Any]:
        from .tools import definitions

        defs = definitions()
        return {"version": 1, "origin": "manager", "created": time.time(), "kind": "manager",
                "agent": {"name": "manager", "model": ref, "effort": None, "steps": MAX_STEPS, "source": "builtin",
                          "path": None, "fingerprint": "builtin"},
                "tools": [d["name"] for d in defs], "tool_definitions": {d["name"]: d for d in defs},
                "system_prompt": self._prompt(), "directory": str(home() / "manager")}

    @staticmethod
    def _prompt() -> str:
        from .prompt import PROMPT

        return PROMPT

    def _new_episode(self, ref: str, *, continued_from: str | None = None) -> Session:
        project = self._project()
        session = self.store.create_session(
            project, title="Manager", agent="manager", model=ref, permission="allow", kind="manager",
            snapshot=self._snapshot(ref), directory=project.path, continued_from=continued_from)
        self.store.set_preference(EPISODE_PREF, session.id)
        self.baseline = None
        return session

    def _episode_tokens(self, episode: Session) -> int:
        measured = self.runtime.measured_input(episode.id, episode.model)
        if measured is not None:
            return measured
        total = 0
        for stored in self.store.messages(episode.id):
            for part in stored.message.parts:
                total += ctx.estimate_tokens(getattr(part, "text", "") or getattr(part, "output", "")
                                             or getattr(part, "arguments", "") or "")
        return total

    def rollover_reason(self, episode: Session, settings: dict[str, Any] | None = None) -> str | None:
        settings = settings or self.settings()
        if self.clock() - episode.updated >= settings["idle_episode_minutes"] * 60:
            return "idle"
        if self._episode_tokens(episode) >= settings["max_episode_tokens"]:
            return "size"
        return None

    async def _admit(self) -> tuple[Session, bool, str | None]:
        """The episode this turn runs in, whether it is a fresh one, and the recap it starts from."""
        settings = self.settings()
        ref = settings["model"]
        episode = self.current_episode()
        if episode is None:
            return self._new_episode(ref), True, None
        if self.rollover_reason(episode, settings):
            recap = await self.recap(episode)
            return self._new_episode(ref, continued_from=episode.id), True, recap.text()
        if episode.model != ref:
            self.store.update_session(episode.id, model=ref)
            episode = self.store.session(episode.id) or episode
        return episode, False, None

    async def recap(self, episode: Session) -> ctx.Recap:
        """Written before the episode closes: deterministic parts always, a same-model narrative if it works."""
        open_items = [f"{kind} {item_id} was shown and has no answer yet"
                      for item_id, (kind, _) in self._open_presented().items()]
        recap = ctx.build_recap(self.store, episode.id, audit=self.store.audit(limit=200), open_items=open_items)
        material = ctx.narrative_material(self.store, episode.id)
        if material:
            try:
                recap.narrative = (await (self.narrator or self._default_narrator())(material)).strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the deterministic parts are enough
                log.warning("manager recap narrative failed: %s", exc)
        return recap

    def _default_narrator(self) -> Narrator:
        from .. import oneshot
        from .. import runtime as runtime_module

        async def write(material: str) -> str:
            provider, model = runtime_module.provider_for(self.settings()["model"])
            request = Request(model=model, instructions=ctx.NARRATIVE_INSTRUCTION,
                              messages=(Message("user", (Text(material),)),), tools=(), reasoning_effort=None,
                              conversation_id="manager-recap")
            return str((await oneshot.ask(provider, request))["text"])

        return write

    def _open_presented(self) -> dict[str, tuple[str, str]]:
        open_items: dict[str, tuple[str, str]] = {}
        for item_id in self.presented:
            if item_id in self.runtime.approvals:
                open_items[item_id] = ("approval", item_id)
            elif item_id in self.runtime.questions:
                open_items[item_id] = ("question", item_id)
        return open_items

    # -- the world: sessions, facts, states ---------------------------------

    def facts(self, sessions: list[Session]) -> dict[str, SessionFacts]:
        """Live facts for each session; roots roll their whole tree up, as the clients' rail counts do.

        Core has no unread source (it is a client-side notion), so ``unread`` is reported as ``False``:
        a settled session is ``idle``, and ``finished_unread`` is never produced here.
        """
        runtime = self.runtime
        approvals = Counter(sid for sid, _, _ in runtime.approvals.values())
        questions = Counter(q.session_id for q in runtime.questions.values())
        escalated = {sid for sid, blocks in runtime.escalations.items() if blocks}
        busy = set(runtime.turns)
        failed = {row["session_id"] for row in self.store.db.execute(
            "select session_id from turn t where status = 'failed'"
            " and started = (select max(started) from turn where session_id = t.session_id)")}
        children: dict[str, list[str]] = {}
        for session in sessions:
            if session.parent_id:
                children.setdefault(session.parent_id, []).append(session.id)

        def tree(session_id: str) -> list[str]:
            out, stack = [], [session_id]
            while stack:
                current = stack.pop()
                out.append(current)
                stack.extend(children.get(current, ()))
            return out

        facts: dict[str, SessionFacts] = {}
        for session in sessions:
            members = tree(session.id) if session.parent_id is None else [session.id]
            facts[session.id] = SessionFacts(
                pending_approvals=sum(approvals[m] for m in members),
                pending_questions=sum(questions[m] for m in members),
                escalations=sum(1 for m in members if m in escalated),
                failed=session.id in failed, busy=any(m in busy for m in members), unread=False)
        return facts

    def states(self, sessions: list[Session]) -> dict[str, str]:
        facts = self.facts(sessions)
        return {s.id: session_state(s, facts[s.id]).state for s in sessions}

    def world(self, project: Project | None = None, *, roots_only: bool = True) -> list[WorldRow]:
        sessions = self.store.sessions(project.id if project else None)
        all_sessions = sessions if project is None else self.store.sessions()
        states = self.states(all_sessions)
        names = {p.id: p.name for p in self.store.projects()}
        rows = []
        for session in sessions:
            if roots_only and session.parent_id is not None:
                continue
            rows.append(WorldRow(
                session_id=session.id, handle=self.retriever.handle_for(session), title=session.title,
                project=names.get(session.project_id, Path(session.directory).name),
                state=states.get(session.id, "unknown"), updated=session.updated, archived=bool(session.archived)))
        return rows

    # -- what Runtime asks of the Manager ------------------------------------------

    def build_agent(self, session: Session, *, refresh: bool = True) -> Agent:
        from .. import runtime as runtime_module

        ref = session.model
        if not ref:
            raise ValueError("no model selected; set [manager] model")
        provider, model = runtime_module.provider_for(ref)  # an unavailable model fails the turn visibly: no fallback
        runtime = self.runtime
        return Agent(
            provider, model=model, workspace=Path(session.directory), tools=tuple(self.tools.build()),
            instructions=self._prompt(), reasoning_effort=session.effort, max_steps=MAX_STEPS,
            messages=[m.message for m in self.store.messages(session.id)], conversation_id=session.id,
            retry_policy=runtime.retry_policy, model_ref=ref, fallback_models=[],
            render=lambda message: runtime.render_media(message, model))

    def session_json(self, session: Session) -> dict[str, Any]:
        """A minimal, honest description of a Manager episode (it is never listed to clients)."""
        return {"id": session.id, "project_id": session.project_id, "directory": session.directory,
                "title": session.title, "agent": session.agent, "kind": session.kind, "parent_id": None,
                "model": session.model, "model_source": "session", "effort": session.effort,
                "effort_source": "session" if session.effort else "provider", "tools": self._tool_names(),
                "disabled_tools": [], "status": "busy" if session.id in self.runtime.turns else "idle",
                "archived": False, "revision": session.revision, "continued_from": session.continued_from,
                "created": session.created, "updated": session.updated, "children": []}

    def _tool_names(self) -> list[str]:
        from .tools import definitions

        return [d["name"] for d in definitions()]

    # -- topic lines -----------------------------------------------------------------

    def enable_topics(self) -> None:
        """Write topic lines with the Manager's model. Called once the Manager is on; a test may set ``topics.writer``."""
        if self.topics.writer is None:
            async def write(session: Session, body: str) -> str:
                return await model_writer(self.settings()["model"])(session, body)

            self.topics.writer = write

    def after_turn(self, session_id: str) -> None:
        """A session's turn finished: refresh its topic line if the Manager is on and the topic moved."""
        if not self.settings()["enabled"]:
            return
        self.enable_topics()
        self.topics.schedule([session_id])

    async def shutdown(self) -> None:
        if self.call is not None and self.call.active:
            await self._end(self.call, publish=False)
        await self.topics.drain()
