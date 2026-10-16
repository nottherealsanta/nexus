"""Client-side process management for the ``nexus-live`` audio helper (GPT_LIVE_PLAN 4.2, M4).

Stdlib only, so the Ratatui bridge, the desktop client and a headless CLI can all
reuse it. The helper speaks JSONL on stdio (``rust/live/README.md``); this module
owns the process, reads its events with a hard line bound, and runs the
offer -> ``LiveStart`` -> answer handshake against any host client exposing
``live_start`` / ``live_stop`` / ``live_helper_event`` (``nexus.client.protocol``).

The helper never sees credentials: it exchanges only SDP and audio. Closing our
side (``stop``, stdin EOF) always releases the microphone: the helper exits on
EOF, and :meth:`LiveHelper.stop` escalates to ``terminate`` and ``kill``.

Bounds: stdin/stdout lines are at most :data:`MAX_LINE` bytes (longer lines are
dropped and counted), the SDP offer is checked against the host's 64 KiB limit,
every wait has a timeout, and forwarded details are at most 2 KiB.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

#: Longest accepted helper line (an SDP offer is at most 64 KiB; the rest is headroom).
MAX_LINE = 128 * 1024
#: Largest offer the host accepts (``LiveStart``); refuse locally with a clear message.
MAX_OFFER = 64 * 1024
MAX_DETAIL = 2048
OFFER_TIMEOUT = 30.0
READY_TIMEOUT = 30.0
STOP_TIMEOUT = 3.0
#: Dropped-frame counts are batched into at most one forward per interval.
DROPPED_INTERVAL = 1.0

INSTALL_HINT = (
    "Upgrade or reinstall Nexus with `uv tool install --upgrade --force nexus-harness`, "
    "then restart the daemon."
)
MIC_HINT = "Allow the microphone for your terminal (macOS: System Settings > Privacy & Security > Microphone)."


class LiveHelperError(RuntimeError):
    """A safe, displayable helper failure. ``code`` is stable (``unsupported``, ``timeout`` ...)."""

    def __init__(self, code: str, message: str, *, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


def binary_path() -> Path:
    """The ``nexus-live`` executable, found like ``ui/ratatui/run.py:binary_path``.

    ``NEXUS_LIVE_BINARY`` overrides; otherwise the interpreter's bin directory, then
    ``rust/live/target/{release,debug}``, then ``PATH``. In a source checkout the
    most recently built one wins. The interpreter's bin directory is not resolved, so a
    venv ``python`` symlink (``uv tool``) still finds its neighbouring binary.
    Raises :class:`LiveHelperError` (``unsupported``).

    Source developers can build the helper with
    ``cargo build --release --manifest-path rust/live/Cargo.toml`` or point
    ``NEXUS_LIVE_BINARY`` at it; installed users just upgrade the package.
    """
    override = os.environ.get("NEXUS_LIVE_BINARY")
    if override:
        candidates = [Path(override)]
    else:
        root = Path(__file__).resolve().parents[2]
        candidates = [Path(sys.executable).parent / "nexus-live",
                      *(root / f"rust/live/target/{profile}/nexus-live" for profile in ("release", "debug"))]
        installed = shutil.which("nexus-live")
        if installed:
            candidates.append(Path(installed))
        candidates.sort(key=lambda path: path.stat().st_mtime if path.is_file() else 0, reverse=True)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise LiveHelperError("unsupported", "The nexus-live audio helper is not installed.", hint=INSTALL_HINT)


def available() -> bool:
    try:
        binary_path()
    except LiveHelperError:
        return False
    return True


def classify_failure(reason: str, detail: str = "") -> str:
    """A one-line fix for a helper ``closed`` reason or error text (microphone, build, device)."""
    text = f"{reason} {detail}".lower()
    if any(word in text for word in ("permission", "denied", "not allowed", "microphone", "audio_error", "no input")):
        return MIC_HINT
    if "unsupported" in text:
        return INSTALL_HINT
    return ""


CONSENT_LINES = (  # short lines: list dialogs clip instead of wrapping
    "GPT Live is a spoken conversation with OpenAI's",
    "voice model, working through this session.",
    "",
    "Sent to OpenAI through your ChatGPT account:",
    "  - audio from your microphone during the call",
    "  - summaries of the agent's work and results,",
    "    which can include file contents and output",
    "  - at call start, up to 8 KiB of this session's recent",
    "    prompts and replies (not thinking or tool output)",
    "",
    "The session's model, tools and permissions do the",
    "work. The voice can ask the agent to act but can",
    "never approve anything: permissions stay on screen.",
    "",
    "Experimental, undocumented service. Asked once per",
    "machine; withdraw any time with /live revoke.",
)

LOGIN_HINT = "Sign in again with `nexus auth codex login`."
_FIXES: dict[str, str] = {
    "live_auth": LOGIN_HINT,
    "live_forbidden": "Voice may not be available on this ChatGPT plan, or the allowance is used up. " + LOGIN_HINT,
    "live_rate_limited": "Wait a moment and try again.",
    "no_credential": LOGIN_HINT,
    "disabled": "Enable it with `[live] enabled = true` in nexus.toml and unset NEXUS_LIVE=off.",
    "live_unavailable": INSTALL_HINT,
    "unsupported": INSTALL_HINT,
    "live_timeout": "Check the network connection and try again.",
    "live_consent_required": "Accept the GPT Live notice first: `/live` in the chat, or `nexus live`.",
}
_FIXES["live_disabled"] = _FIXES["disabled"]


def fix_for(kind: str) -> str:
    """The one-line fix for a host error ``kind`` or Live status ``reason`` (empty if none)."""
    return _FIXES.get(kind, "")


def unavailable_message(reason: str, active_session: str = "") -> str:
    """Why a Live status is not available (``LiveStatusResult.reason``), shown by every client."""
    reasons = {
        "busy": f"A Live call is already running in session {active_session or 'another session'}.",
        "no_credential": "GPT Live needs a ChatGPT sign-in.",
        "disabled": "GPT Live is turned off.",
        "unsupported": "GPT Live is not available in this install.",
    }
    return reasons.get(reason, f"GPT Live is unavailable ({reason or 'unknown reason'}).")


def describe_failure(exc: BaseException) -> tuple[str, str]:
    """``(message, fix)`` for a failed start or a host error; ``fix`` may be empty.

    Host errors carry a redacted ``message`` and a stable ``kind`` (``FacadeError``);
    helper errors carry a ``hint``. Anything else is shown by type only.
    """
    if isinstance(exc, LiveHelperError):
        return str(exc), exc.hint
    kind = getattr(exc, "kind", "")
    message = getattr(exc, "message", "") or str(exc) or type(exc).__name__
    return str(message)[:400], fix_for(str(kind))


class LiveHelper:
    """One ``nexus-live`` process.

    ``on_event(event)`` receives every parsed helper event (also ``levels``);
    ``forward(kind, detail)`` is awaited for the events the daemon wants
    (``closed``, ``dc_error``, ``dropped``, ``phase``). A failing callback never
    breaks the reader.
    """

    def __init__(
        self,
        *,
        binary: Path | str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        forward: Callable[[str, str], Awaitable[Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._binary = Path(binary) if binary else None
        self._on_event = on_event
        self._forward = forward
        self._clock = clock
        self._spawn = spawn or asyncio.create_subprocess_exec
        self.process: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self._pending_dropped = 0
        self._dropped_at = 0.0
        self._forwards: set[asyncio.Task[None]] = set()
        self._stopping = False
        self.closed_reason: str | None = None
        self.ready = False
        self.muted = False
        self.paused = False
        self.phase = "listening"
        self.level_in = 0.0
        self.level_out = 0.0
        self.dropped_frames = 0
        self.overlong_lines = 0
        self.error = ""

    # -- lifecycle -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None and self.closed_reason is None

    async def start(self) -> None:
        if self.process is not None:
            return
        path = self._binary or binary_path()
        try:
            self.process = await self._spawn(
                str(path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_LINE,
            )
        except OSError as exc:
            self.process = None
            raise LiveHelperError("unsupported", f"The nexus-live audio helper could not start: {exc.strerror or exc}",
                                  hint=INSTALL_HINT) from exc
        self._reader = asyncio.ensure_future(self._read())

    async def offer(self, timeout: float = OFFER_TIMEOUT) -> str:
        """Open the microphone and speaker and return the SDP offer."""
        await self.start()
        waiter = self._waiter("offer", "closed")
        await self._send({"type": "offer"})
        event = await self._wait(waiter, timeout, "The audio helper did not produce an offer in time.")
        if event["type"] == "closed":
            raise self._closed_error(event)
        sdp = event.get("sdp")
        if not isinstance(sdp, str) or not sdp:
            raise LiveHelperError("bad_offer", "The audio helper returned an empty offer.")
        if len(sdp.encode()) > MAX_OFFER:
            raise LiveHelperError("bad_offer", "The audio helper's offer is larger than the 64 KiB limit.")
        return sdp

    async def answer(self, sdp: str, timeout: float = READY_TIMEOUT) -> None:
        """Apply the daemon's SDP answer and wait for ``ready`` (peer and data channel up)."""
        if self.ready:
            return
        waiter = self._waiter("ready", "closed")
        await self._send({"type": "answer", "sdp": sdp})
        event = await self._wait(waiter, timeout, "The audio connection did not become ready in time.")
        if event["type"] == "closed":
            raise self._closed_error(event)

    async def mute(self, on: bool) -> None:
        self.muted = bool(on)
        await self._send({"type": "mute", "on": bool(on)})

    async def pause(self, on: bool) -> None:
        """Silence the microphone and drop the voice's audio; the peer stays up (docs/live.md)."""
        self.paused = bool(on)
        await self._send({"type": "pause", "on": bool(on)})

    async def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Idempotent shutdown: ``stop`` command, stdin EOF, then terminate and kill."""
        self._stopping = True
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                await self._send({"type": "stop"})
            with contextlib.suppress(Exception):
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 1.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(process.wait(), 1.0)
        if self._reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._reader), 1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._reader.cancel()
            self._reader = None
        for task in list(self._forwards):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 1.0)
        if self.closed_reason is None:
            self.closed_reason = "stop"
        self._release_waiters()

    # -- io ------------------------------------------------------------------

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.returncode is not None or process.stdin.is_closing():
            raise LiveHelperError("closed", "The audio helper is not running.")
        line = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(line) > MAX_LINE:
            raise LiveHelperError("bad_command", "Command too large for the audio helper.")
        try:
            process.stdin.write(line)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise LiveHelperError("closed", "The audio helper exited.") from exc

    async def _readline(self) -> bytes | None:
        """One bounded line, ``b""`` for an over-long line (discarded), ``None`` at EOF."""
        stdout = self.process.stdout
        try:
            return await stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError:
            return None  # EOF (any partial tail is discarded)
        except asyncio.LimitOverrunError as exc:
            await stdout.readexactly(exc.consumed)  # drop what is buffered, then to the newline
            while True:
                try:
                    await stdout.readuntil(b"\n")
                    return b""
                except asyncio.IncompleteReadError:
                    return None
                except asyncio.LimitOverrunError as more:
                    await stdout.readexactly(more.consumed)

    async def _read(self) -> None:
        try:
            while True:
                raw = await self._readline()
                if raw is None:
                    break
                if raw == b"":
                    self.overlong_lines += 1
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("type"), str):
                    self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken pipe ends the call, never raises here
            self.error = f"{type(exc).__name__}"
        finally:
            if self.closed_reason is None:
                self.closed_reason = "helper_exited" if not self._stopping else "stop"
                self._notify({"type": "closed", "reason": self.closed_reason})
            self._release_waiters()

    def _handle(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "levels":
            self.level_in = _unit(event.get("in"))
            self.level_out = _unit(event.get("out"))
        elif kind == "phase":
            phase = event.get("phase")
            if phase in ("listening", "speaking"):
                self.phase = phase
                self._queue_forward("phase", phase)
        elif kind == "ready":
            self.ready = True
        elif kind == "dc_error":
            self._queue_forward("dc_error", str(event.get("message", ""))[:MAX_DETAIL])
        elif kind == "dropped":
            self._dropped(event.get("frames"))
        elif kind == "error":
            self.error = str(event.get("message", ""))[:300]
        elif kind == "closed":
            reason = str(event.get("reason", "closed"))[:100]
            self.closed_reason = reason
            if not self._stopping:
                self._queue_forward("closed", reason)
        self._notify(event)

    def _dropped(self, frames: Any) -> None:
        try:
            count = max(0, min(int(frames), 1_000_000))
        except (TypeError, ValueError):
            return
        self.dropped_frames += count
        self._pending_dropped += count
        now = self._clock()
        if self._pending_dropped and now - self._dropped_at >= DROPPED_INTERVAL:
            batch, self._pending_dropped, self._dropped_at = self._pending_dropped, 0, now
            self._queue_forward("dropped", str(batch))

    def _notify(self, event: dict[str, Any]) -> None:
        for future in self._waiters.pop(event["type"], []):
            if not future.done():
                future.set_result(event)
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(event)

    def _queue_forward(self, kind: str, detail: str) -> None:
        if self._forward is None:
            return

        async def run() -> None:
            with contextlib.suppress(Exception):  # the daemon may already have ended the call
                await self._forward(kind, detail)

        task = asyncio.ensure_future(run())
        self._forwards.add(task)
        task.add_done_callback(self._forwards.discard)

    # -- waiting -------------------------------------------------------------

    def _waiter(self, *kinds: str) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        if self.closed_reason is not None and "closed" in kinds:
            future.set_result({"type": "closed", "reason": self.closed_reason})
            return future
        for kind in set(kinds):
            self._waiters.setdefault(kind, []).append(future)
        return future

    async def _wait(self, future: asyncio.Future[dict[str, Any]], timeout: float, message: str) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise LiveHelperError("timeout", message, hint=MIC_HINT) from exc

    def _release_waiters(self) -> None:
        for futures in self._waiters.values():
            for future in futures:
                if not future.done():
                    future.set_result({"type": "closed", "reason": self.closed_reason or "closed"})
        self._waiters.clear()

    def _closed_error(self, event: dict[str, Any]) -> LiveHelperError:
        reason = str(event.get("reason", "closed"))
        hint = classify_failure(reason, self.error)
        text = f"The audio helper closed ({reason})"
        if self.error:
            text += f": {self.error}"
        return LiveHelperError(reason if reason.isidentifier() else "closed", text + ".", hint=hint)


def _unit(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


class LiveCall:
    """The client half of a call: helper process plus the daemon handshake.

    ``start`` runs offer -> ``client.live_start`` -> answer and returns the
    ``LiveStartResult``; on any failure the helper is stopped and, if the daemon had
    accepted the call, ``live_stop`` is sent so no half-open call remains.
    """

    def __init__(self, client: Any, session: str, *, helper: LiveHelper | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.client = client
        self.session = session
        self.helper = helper or LiveHelper(on_event=on_event, forward=self._forward)
        if helper is not None and helper._forward is None:
            helper._forward = self._forward
        self.result: Any = None
        self.started_at = 0.0

    async def _forward(self, kind: str, detail: str) -> Any:
        return await self.client.live_helper_event(self.session, kind, detail)

    async def start(self, *, voice: str = "", new_conversation: bool = False) -> Any:
        accepted = False
        try:
            offer = await self.helper.offer()
            self.result = await self.client.live_start(
                self.session, offer, voice=voice, new_conversation=new_conversation)
            accepted = True
            await self.helper.answer(self.result.answer_sdp)
            self.started_at = time.time()
            return self.result
        except BaseException:
            await self.helper.stop()
            if accepted:
                with contextlib.suppress(Exception):
                    await self.client.live_stop(self.session, "helper_failed")
            raise

    async def mute(self, muted: bool | None = None) -> Any:
        """Toggle (``None``) or set mute; the helper mutes, the daemon records it."""
        state = await self.client.live_mute(self.session, muted)
        await self.helper.mute(state.muted)
        return state

    async def pause(self, paused: bool | None = None) -> Any:
        """Toggle (``None``) or set pause; the daemon records it first, then the helper goes quiet."""
        state = await self.client.live_pause(self.session, paused)
        await self.helper.pause(state.paused)
        return state

    async def stop(self, reason: str = "user") -> Any:
        """Tell the daemon first (so ``live.ended`` carries ``reason``), then release the helper."""
        state = None
        try:
            state = await self.client.live_stop(self.session, reason)
        finally:
            await self.helper.stop()
        return state


__all__ = [
    "CONSENT_LINES", "INSTALL_HINT", "MIC_HINT", "LiveCall", "LiveHelper", "LiveHelperError",
    "LOGIN_HINT", "fix_for", "available", "binary_path", "classify_failure", "describe_failure",
    "unavailable_message",
]
