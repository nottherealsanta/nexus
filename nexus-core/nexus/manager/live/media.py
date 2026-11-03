"""Core's end of the GPT-Live call audio: a WebRTC peer to OpenAI, and plain PCM to the client.

Clients no longer run WebRTC. A client captures the microphone and plays speech; everything else is here, next to the
Manager: Core makes the SDP offer, the Codex-signed signaling call answers it, and the audio flows

    client mic --PCM16 24 kHz mono--> Core --Opus/WebRTC--> OpenAI --Opus--> Core --PCM16--> client speaker

over one WebSocket (``/api/manager/live/audio``). That is the same for the TUI, NSH on every target and iOS. Echo
cancellation stays the client's (browsers have it built in; the TUI wants headphones until M6).

Not verified against the real service. The peer is tested against a second in-process peer.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from fractions import Fraction
from typing import Any, Protocol

log = logging.getLogger(__name__)

RATE = 24000  # PCM sample rate both ways (the Opus encoder and decoder resample to and from 48 kHz)
FRAME_SAMPLES = RATE // 50  # 20 ms
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_QUEUED_FRAMES = 50  # one second: older client audio is dropped rather than delayed
GATHER_TIMEOUT_S = 4.0
DATA_CHANNEL = "oai-events"


class Peer(Protocol):
    """What ``LiveVoice`` needs from the WebRTC side (a fake implements this in tests)."""

    on_audio: Callable[[bytes], None] | None
    on_lost: Callable[[str], None] | None
    on_state: Callable[[str], None] | None

    async def offer(self) -> str: ...
    async def accept(self, answer_sdp: str) -> None: ...
    def push(self, pcm: bytes) -> None: ...
    def set_muted(self, muted: bool) -> None: ...
    async def close(self) -> None: ...


def silence() -> bytes:
    return bytes(FRAME_BYTES)


class AiortcPeer:
    """The real peer, on ``aiortc``. Imported lazily so Core runs without it."""

    def __init__(self) -> None:
        from aiortc import RTCPeerConnection

        self.on_audio: Callable[[bytes], None] | None = None
        self.on_lost: Callable[[str], None] | None = None
        self.on_state: Callable[[str], None] | None = None
        self.muted = False
        self._pc = RTCPeerConnection()
        self._buffer = bytearray()
        self._pts = 0
        self._started: float | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._opened = False
        self._track = self._make_track()
        self._pc.addTrack(self._track)
        self._pc.createDataChannel(DATA_CHANNEL)
        self._pc.on("track", self._on_track)
        self._pc.on("connectionstatechange", self._on_state)

    def _make_track(self) -> Any:
        import av
        from aiortc import MediaStreamTrack

        peer = self

        class ClientAudio(MediaStreamTrack):
            kind = "audio"

            async def recv(self) -> Any:
                if peer._started is None:
                    peer._started = time.monotonic()
                delay = peer._started + peer._pts / RATE - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                data = bytes(peer._buffer[:FRAME_BYTES])
                del peer._buffer[:FRAME_BYTES]
                if len(data) < FRAME_BYTES or peer.muted:
                    data = silence()
                frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
                frame.planes[0].update(data)
                frame.sample_rate = RATE
                frame.pts = peer._pts
                frame.time_base = Fraction(1, RATE)
                peer._pts += FRAME_SAMPLES
                return frame

        return ClientAudio()

    # -- signaling ------------------------------------------------------------------

    async def offer(self) -> str:
        await self._pc.setLocalDescription(await self._pc.createOffer())
        # aiortc gathers inside setLocalDescription; the description already carries its candidates.
        sdp = self._pc.localDescription.sdp
        if not sdp:
            raise RuntimeError("no local description")
        return sdp

    async def accept(self, answer_sdp: str) -> None:
        from aiortc import RTCSessionDescription

        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
        self._opened = True

    # -- audio ----------------------------------------------------------------------

    def push(self, pcm: bytes) -> None:
        """Client microphone audio (PCM16 mono 24 kHz). The newest second is kept."""
        self._buffer += pcm
        limit = MAX_QUEUED_FRAMES * FRAME_BYTES
        if len(self._buffer) > limit:
            del self._buffer[: len(self._buffer) - limit]

    def set_muted(self, muted: bool) -> None:
        self.muted = bool(muted)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_track(self, track: Any) -> None:
        if track.kind == "audio":
            self._spawn(self._pump(track))

    async def _pump(self, track: Any) -> None:
        import av

        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while not self._closed:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    data = bytes(out.planes[0])[: out.samples * 2]
                    if self.on_audio is not None and data:
                        self.on_audio(data)
        except Exception as exc:  # noqa: BLE001 - the track ended
            log.debug("remote audio ended: %s", type(exc).__name__)

    def _on_state(self) -> None:
        state = self._pc.connectionState
        if self.on_state is not None:
            self.on_state(state)
        if state in ("failed", "closed") and self._opened and not self._closed and self.on_lost is not None:
            self.on_lost("The audio connection to GPT-Live dropped.")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._pc.close(), 2.0)
