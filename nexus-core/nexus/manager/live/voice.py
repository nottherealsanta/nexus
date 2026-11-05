"""The GPT-Live voice: sound in and out is OpenAI's, every word of substance is the Manager's (docs/manager-design.md section 5).

One ``LiveVoice`` per Manager call in ``mode = "live"``. The client sends an SDP offer; the daemon signs and posts it
with the Codex sign-in, opens the sideband and returns the answer, so the client never sees a token.

Rules kept here:

- **Authority is the user's transcript, not the voice model.** On ``delegation.created`` (which carries no request in
  the documented protocol) the Manager hears the *user's* ``input_transcript`` text since the last hand-off, as a
  ``speech`` utterance. Any text on the delegation event, and everything the voice model said, is ignored.
- Each Manager sentence goes back as ``session.commentary.append`` on that delegation (the voice paraphrases it);
  proactive lines (attention) go as commentary with no delegation. Nothing is replayed after a failed send.
- ``manager.speech`` is *not* published in this mode: the client must not also synthesize it. The same sentence is
  published as a ``manager.caption`` so the stage still shows it.
- Cancelling is the Manager's (``ManagerService.cancel``); interrupting the voice never undoes work already done.

Not verified against the real service.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..redact import redact
from .media import AiortcPeer, Peer
from .prompt import INSTRUCTIONS, RECONNECT_NOTICE
from .protocol import (
    DelegationCreated,
    ErrorEvent,
    LiveError,
    LiveEvent,
    LiveSession,
    SessionClosed,
    SessionStarted,
    SidebandOpen,
    SidebandReconnected,
    TranscriptAdded,
    TranscriptDelta,
    TurnDone,
    Unknown,
    outbound,
    resolve_voice,
)
from .transport import CodexLiveTransport, LiveTransport, safe_detail

if TYPE_CHECKING:
    from ..service import Call, ManagerService

log = logging.getLogger(__name__)

MAX_HEARD_CHARS = 4000  # the Manager's utterance limit; the start is dropped and the cut is said
MAX_TURNS_KEPT = 16
MAX_PENDING = 8
MAX_APPEND_CHARS = 8000
Factory = Callable[[], LiveTransport]
PeerFactory = Callable[[], Peer]


def _default_factory(svc: ManagerService) -> LiveTransport:
    from ...llmhub.auth import CodexAuth

    return CodexLiveTransport(CodexAuth())


class LiveVoice:
    def __init__(self, svc: ManagerService, call: Call, factory: Factory | None = None,
                 peer_factory: PeerFactory | None = None) -> None:
        self.svc, self.call = svc, call
        self._transport: LiveTransport | None = None
        self._factory = factory or (lambda: _default_factory(svc))
        self._peer_factory: PeerFactory = peer_factory or AiortcPeer
        self.peer: Peer | None = None
        self._heard: list[str] = []  # the user's finished turns since the last hand-off
        self._partial = ""
        self._pending = 0
        self._ending = False
        self.style = "codex"  # the ChatGPT-sign-in route; "api" is the documented public API (later)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._send_lock = asyncio.Lock()
        self._client: Any = None
        self.stats: Counter[str] = Counter()  # what the call has done so far, for GET /api/manager
        self.peer_state = "new"

    # -- lifecycle ------------------------------------------------------------

    async def open(self) -> None:
        """Create the call: Core's own WebRTC offer, signed and posted with the Codex sign-in, then the sideband."""
        voice = resolve_voice(str(self.svc.settings().get("live_voice", "")))
        transport = self._factory()
        self._transport = transport
        try:
            peer = self._peer_factory()
        except ImportError:
            raise LiveError("live_unsupported", "GPT-Live audio needs the aiortc package; reinstall Nexus.") from None
        self.peer = peer
        peer.on_lost = lambda why: self._on_closed("peer_lost", why)
        peer.on_state = self._peer_state
        try:
            offer = await peer.offer()
            answer = await asyncio.wait_for(transport.start(
                offer, LiveSession(instructions=INSTRUCTIONS, voice=voice),
                on_event=self._on_event, on_closed=self._on_closed), 60.0)
            await peer.accept(answer.answer_sdp)
        except TimeoutError:
            await self.close("startup_failed")
            raise LiveError("live_timeout", "Timed out starting the call") from None
        except LiveError:
            await self.close("startup_failed")
            raise
        except Exception as exc:  # noqa: BLE001 - transport text may echo secrets
            await self.close("startup_failed")
            raise LiveError("live_signaling", safe_detail(exc) or "The call could not be started") from None

    # -- client audio -----------------------------------------------------------------

    async def attach_client(self, websocket: Any) -> None:
        """Serve the client's audio socket until it closes: its microphone in, the voice's speech out.

        One client at a time; a second attach replaces the first. A closed socket never ends the call: the client
        may reconnect, and the Manager call goes on.
        """
        previous, self._client = self._client, websocket
        if previous is not None:
            with contextlib.suppress(Exception):
                await previous.close(code=4409)
        peer = self.peer
        if peer is None:
            await websocket.close(code=4404)
            return
        loop = asyncio.get_running_loop()
        out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=100)

        def deliver(data: bytes) -> None:
            self.stats["audio.to_client"] += 1
            if out.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    out.get_nowait()  # the client is behind: drop the oldest speech rather than lag
            out.put_nowait(data)

        peer.on_audio = deliver

        async def send() -> None:
            while (data := await out.get()) is not None:
                await websocket.send_bytes(data)

        sender = loop.create_task(send())
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data:
                    self.stats["audio.from_client"] += 1
                    peer.push(data)
                elif message.get("text") == "mute":
                    peer.set_muted(True)
                elif message.get("text") == "unmute":
                    peer.set_muted(False)
        finally:
            peer.on_audio = None
            with contextlib.suppress(asyncio.QueueFull):
                out.put_nowait(None)  # the sender is cancelled next, so a full queue needs no sentinel
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            if self._client is websocket:
                self._client = None

    async def close(self, reason: str = "done") -> None:
        if self._ending:
            return
        self._ending = True
        for task in list(self._tasks):
            task.cancel()
        transport, self._transport = self._transport, None
        peer, self.peer = self.peer, None
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close(code=1000)
        if peer is not None:
            await peer.close()
        if transport is not None:
            await transport.close(reason)

    # -- events in ------------------------------------------------------------

    def _on_closed(self, reason: str, error: str | None) -> None:
        if self._ending:
            return
        message = {"remote_closed": "The voice service ended the call.",
                   "sideband_lost": "The connection to GPT-Live was lost."}.get(reason, "The voice call ended.")
        self._fail(error or message, "live_closed", "Start the call again.")
        self._ending = True
        asyncio.ensure_future(self._drop())

    async def _drop(self) -> None:
        transport, self._transport = self._transport, None
        peer, self.peer = self.peer, None
        if peer is not None:
            await peer.close()
        if transport is not None:
            await transport.close("ended")
        self.svc.live_ended(self)

    def _peer_state(self, state: str) -> None:
        self.peer_state = state
        log.info("GPT-Live peer %s", state)

    def _fail(self, message: str, code: str, fix: str) -> None:
        self.svc._set_error(self.call, {"code": code, "message": redact(safe_detail(message)), "fix": fix})

    def info(self) -> dict[str, Any]:
        """Progress of the call, so a silent one can be diagnosed without logs: what arrived from each side."""
        return {"style": self.style, "peer": self.peer_state, "client_attached": self._client is not None,
                "pending": self._pending, "counts": dict(self.stats)}

    def _on_event(self, event: LiveEvent) -> None:
        self.stats[f"event.{type(event).__name__}"] += 1
        if isinstance(event, Unknown):
            self.stats[f"unknown.{event.raw_type or 'untyped'}"] += 1
        if self._ending:
            return
        if isinstance(event, (SidebandOpen, SessionStarted)):
            self.svc._set_error(self.call, None) if self.call.error else None
        elif isinstance(event, SidebandReconnected):
            self._spawn(self._append(RECONNECT_NOTICE, "say", None))
            self._fail(f"The voice connection dropped for about {event.gap_s:.1f}s and came back; something may have "
                       "been missed.", "live_reconnected", "Say it again if it matters.")
        elif isinstance(event, SessionClosed):
            self._on_closed("remote_closed", event.reason or None)
        elif isinstance(event, TranscriptDelta) and event.role == "user":
            self._partial = (self._partial + event.delta)[-MAX_HEARD_CHARS:]
            self.call.listening = True
        elif isinstance(event, TranscriptAdded) and event.role == "user":
            text = event.text
            self._partial = text if text.startswith(self._partial) else self._partial + text
            self._partial = self._partial[-MAX_HEARD_CHARS:]
        elif isinstance(event, TurnDone):
            if event.role == "user":
                text = (event.transcript or self._partial).strip()
                self._partial = ""
                self.call.listening = False
                if text:
                    self._heard.append(text)
                    del self._heard[:-MAX_TURNS_KEPT]
            elif event.role == "assistant":
                self.call.speaking = False
        elif isinstance(event, DelegationCreated):
            self._delegated(event)
        elif isinstance(event, ErrorEvent):
            self._fail(event.message, "live_error", "Start the call again." if event.fatal else "")
            if event.fatal or event.status in (401, 403):
                self._on_closed("error", event.message)
        elif isinstance(event, Unknown) and event.raw_type == "oversize":
            self._fail("A message from the voice service was too large to read; a request may have been lost.",
                       "live_oversize", "Say it again.")

    def _delegated(self, event: DelegationCreated) -> None:
        """The voice model asked the Manager for help. What the *user* said since the last hand-off is the request."""
        parts = [*self._heard, self._partial.strip()]
        self._heard.clear()
        self._partial = ""
        heard = " ".join(p for p in parts if p).strip()
        if self._pending >= MAX_PENDING:
            self._spawn(self._append("Too many requests are waiting, so this one was not sent. Tell the user briefly "
                                     "and ask them to try again in a moment.", "say", event.id))
            return
        if not heard:
            self._spawn(self._append("Nothing was heard from the user for this request, so the Manager was not asked. "
                                     "Ask the user to repeat it.", "say", event.id))
            return
        if len(heard) > MAX_HEARD_CHARS:
            heard = "[earlier speech omitted] " + heard[-MAX_HEARD_CHARS:]
        self._pending += 1
        # A voice utterance is the user's own: authority comes from this transcript, never from the delegation text.
        self.svc.live_utterance(self.call, heard, event.id)

    # -- the Manager's words out -------------------------------------------------

    def speak(self, text: str, delegation_id: str | None) -> None:
        """One Manager sentence for the voice to say (paraphrased). Never replayed if the send fails."""
        if self._ending or not text.strip():
            return
        self.call.speaking = True
        self._spawn(self._append(text, "say", delegation_id))

    def note(self, text: str, delegation_id: str | None = None) -> None:
        """Context the voice may use later without saying it now."""
        if not self._ending and text.strip():
            self._spawn(self._append(text, "quiet", delegation_id))

    def turn_done(self, delegation_id: str | None, spoke: bool, error: str | None) -> None:
        if delegation_id is not None:
            self._pending = max(0, self._pending - 1)
        if self._ending or delegation_id is None:
            return
        if error:
            self._spawn(self._append(f"The Manager could not finish this request: {error}. Tell the user plainly.",
                                     "say", delegation_id))
        elif not spoke:
            self._spawn(self._append("The Manager has nothing to add to this request.", "quiet", delegation_id))

    async def stop_speaking(self) -> None:
        if not self._ending:
            self._spawn(self._append("Stop speaking about that now and wait for the user.", "instruct", None))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _append(self, text: str, kind: str, delegation_id: str | None) -> None:
        transport = self._transport
        if transport is None or self._ending:
            return
        messages = outbound(kind, redact(text)[:MAX_APPEND_CHARS], delegation_id=delegation_id, style=self.style)
        sent = 0
        self.stats[f"append.{kind}"] += 1
        try:
            async with self._send_lock:
                for message in messages:
                    if self._ending:
                        return
                    await transport.send(message)
                    sent += 1
        except LiveError as exc:
            if exc.code in ("live_not_connected", "live_send_ambiguous"):
                self._fail(f"A message to the voice was interrupted after {sent} of {len(messages)} parts and was "
                           "not retried.", exc.code, "Ask again if the answer was cut off.")
            else:
                self._fail(exc.message, exc.code, "Start the call again.")
                self._on_closed("transport_error", exc.message)
