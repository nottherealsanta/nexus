"""Signaling and sideband transport for a GPT Live call.

``LiveTransport`` is the seam the manager depends on; ``CodexLiveTransport`` is
the only implementation. It is the **only** module that sees the ChatGPT OAuth
bearer (via ``CodexOAuthManager.headers()``): credentials never reach events,
logs, host results or the audio helper, and every error text passes
``redact_secrets`` and is bounded before it leaves this module.

Protocol constants are pinned to two reference integrations (both MIT; see
``NOTICE``) as recorded in ``plans/done/GPT_LIVE_PROTOCOL_NOTES.md``
(notes §N below):

- OC: ``malhashemi/opencode-gpt-live`` @ ``c23101cfdeb3feaf03f09aa678135ca01d72ed4e``
- OMP: ``can1357/oh-my-pi`` @ ``818dd42b73ab1caad17fa06d4edcc3a97086dc57``

Neither reference was run and this module has not been exercised against the
real service: **not verified**. Experimental; fails closed.

Client identity (decision, see ``docs/decisions.md``): the ``originator`` and
``User-Agent`` headers come from ``CodexOAuthManager`` (``nexus`` /
``Nexus/0.1``), the same identity Nexus already sends to the Codex Responses
endpoint. The notes show no evidence the server requires a specific value
(OC sends its own name, OMP sends the Codex desktop app's); whether it
rejects ``nexus`` is not verified. Attestation (``x-oai-attestation``) is
omitted (notes §6): neither reference requires it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx
import msgspec

from ..util import redact_secrets
from .protocol import (
    CallRequest,
    LiveError,
    LiveEvent,
    LiveSession,
    SidebandOpen,
    SidebandReconnected,
    close_message,
    parse_event,
    valid_call_id,
)

#: notes §1.1 (OC ``src/server/live.ts:11``; OMP ``transport.ts:18``).
CALLS_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
#: notes §2.1 (OC ``live.ts:12``; OMP ``transport.ts:73-77``).
SIDEBAND_URL = "wss://api.openai.com/v1/live/{call_id}"
#: notes §1.1: ``OpenAI-Alpha: quicksilver=v2`` (OC ``live.ts:56``; OMP ``transport.ts:87``).
ALPHA_HEADER = "quicksilver=v2"
#: notes §2.3 retry policy (OC ``live.ts:259-262``): 5 attempts, 200 * 2^n ms, 15 s each.
CONNECT_ATTEMPTS = 5
CONNECT_BASE_DELAY_S = 0.2
CONNECT_TIMEOUT_S = 15.0
#: Plan §6: one retry for a 5xx before media; signaling timeout.
SIGNALING_TIMEOUT_S = 30.0
SIGNALING_5XX_RETRIES = 1
#: Credential refresh bound, separate from a WebSocket open (DNS/TCP/TLS/HTTP upgrade).
RECONNECT_TIMEOUT_S = 5.0
#: Early-join retry (Codex-like, docs/live.md): the sideband of a freshly created call can answer
#: 404 briefly before the call is active. The INITIAL open retries a 404 at these delays, then fails.
#: A 404 during recovery is never retried (the call is gone).
JOIN_404_DELAYS_S = (0.1, 0.3, 0.8)
#: Recovery after an unclean loss (Codex-like): attempt ``n`` waits ``min(0.2 * 2**(n-1), 5)`` s.
#: ``n`` counts consecutive failed opens and short-lived connections; a connection that lasted
#: ``RECONNECT_STABLE_AFTER_S`` resets it. There is no attempt-count limit; only terminal refusals,
#: a clean close, stop, the call deadline and ``RECOVERY_GIVE_UP_S`` end recovery.
RECONNECT_BASE_DELAY_S = 0.2
RECONNECT_MAX_DELAY_S = 5.0
RECONNECT_STABLE_AFTER_S = 30.0
#: Total bound on continuous failure (from the first drop of an unstable stretch). Codex retries
#: forever; Nexus keeps every loop bounded, and the manager's ``max_minutes`` deadline also applies.
#: 5 minutes outlasts a network change or a sleep/wake while not leaving a dead call "live" for long.
RECOVERY_GIVE_UP_S = 300.0
#: One write attempt. A write that raises or times out is delivery-ambiguous and never replayed.
SEND_TIMEOUT_S = 5.0
#: How long a send waits (lock, initial connect, reconnect) before failing as "not sent". A message
#: that was never written may wait for the sideband to come back (Codex keeps it pending without a
#: limit); 10 s covers the first backoff steps (0.2+0.4+0.8+1.6+3.2 s) plus one open, yet a caller
#: (the bridge appends under a per-call lock) is never blocked for long. Ambiguous writes never replay.
SEND_WAIT_TIMEOUT_S = 10.0
CLOSE_TIMEOUT_S = 1.0
MAX_DETAIL_CHARS = 300  # notes §8.2 (OC cuts error bodies to 300 characters)
MAX_ANSWER_BYTES = 262_144

_AUTH_CODES = ("live_auth", "live_forbidden", "live_rate_limited")


class WebSocketLike(Protocol):
    async def send(self, text: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


#: ``(url, headers) -> websocket``; raises ``LiveError`` for a refused handshake.
Connector = Callable[[str, Mapping[str, str]], Awaitable[WebSocketLike]]
EventHandler = Callable[[LiveEvent], Awaitable[None] | None]
ClosedHandler = Callable[[str, str | None], Awaitable[None] | None]


class CallAnswer(msgspec.Struct, frozen=True):
    call_id: str
    answer_sdp: str


class LiveTransport(Protocol):
    """What the manager needs from a transport (a fake implements this in tests)."""

    async def start(
        self, offer_sdp: str, session: LiveSession, *, on_event: EventHandler, on_closed: ClosedHandler
    ) -> CallAnswer:
        """Signal, return the SDP answer, then deliver sideband events in the background."""

    async def send(self, message: Mapping[str, Any]) -> None:
        """Send one JSON message on the sideband, in order."""

    async def close(self, reason: str = "done") -> None:
        """Send ``session.close`` (bounded), close the socket. Idempotent."""


_BEARER = re.compile(r"(?i)\bbearer\b(?:\s+(?:\*{3}|[A-Za-z0-9._~+/=-]+))?")


def safe_detail(text: object, limit: int = MAX_DETAIL_CHARS) -> str:
    """Collapse whitespace, redact credentials and bound ``text`` for display."""
    return redact_secrets(" ".join(str(text).split()))[:limit]


class LiveHTTPError(LiveError):
    """An HTTP refusal with retry policy retained across the connector boundary."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(code, message)
        self.status = status
        # A missing call/route is not evidence of eventual consistency. Retry only
        # request timeouts and server failures; auth/rate limits remain terminal.
        self.retryable = status == 408 or 500 <= status < 600


def _terminal_open_error(error: LiveError) -> bool:
    return (isinstance(error, LiveHTTPError) and not error.retryable
            or error.code in (*_AUTH_CODES, "live_unsupported", "live_closed"))


def error_for_status(status: int, detail: str = "", *, sideband: bool = False) -> LiveHTTPError:
    """Map a signaling HTTP status to an actionable, redacted error (notes §8.1)."""
    if status == 401:
        return LiveHTTPError(status, "live_auth", "ChatGPT rejected the sign-in; run `nexus auth codex login` and try again")
    if status == 403:
        return LiveHTTPError(
            status, "live_forbidden",
            "GPT Live is not available on this ChatGPT plan, or the voice allowance is used up",
        )
    if status == 429:
        return LiveHTTPError(status, "live_rate_limited", "The GPT Live usage limit was reached; try again later")
    text = f"sideband WebSocket upgrade rejected ({status})" if sideband else f"call creation failed ({status})"
    if status in (404, 410):
        text += ("; call unavailable/expired or signaling endpoint/protocol mismatch; not retrying"
                 if sideband else "; signaling endpoint unavailable or protocol mismatch; not retrying")
    return LiveHTTPError(status, "live_sideband" if sideband else "live_signaling",
                         f"{text}: {safe_detail(detail)}" if detail.strip() else text)


def parse_call_id(location: str | None, url: str, fallback: str | None = None) -> str:
    """Call id from the ``Location`` header (notes §1.4), else ``openai-session-id``."""
    if location:
        path = urlsplit(urljoin(url, location.strip())).path
        for segment in path.split("/"):
            if valid_call_id(segment):
                return segment
    if fallback and valid_call_id(fallback.strip()):
        return fallback.strip()
    raise LiveError("live_no_call_id", "The call was created but the response carried no valid call id")


def websocket_available() -> bool:
    """Whether the ``websockets`` client (a base dependency) can be imported."""
    import importlib.util

    return importlib.util.find_spec("websockets") is not None


# No client WebSocket pings (``ping_interval=None``), as Codex does: it only answers server
# pings, which ``websockets`` does automatically. A half-open socket is therefore noticed only by
# a failed or timed-out write, a read error, or the server closing; see docs/live.md for the
# trade-off. Not verified against the real service.


async def _default_connector(url: str, headers: Mapping[str, str]) -> WebSocketLike:
    """Connect with ``websockets`` (a base dependency), imported lazily."""
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise LiveError(
            "live_unsupported",
            "GPT Live's WebSocket client is missing from this install. Reinstall with "
            "`uv tool install --upgrade --force nexus-harness`, then restart the daemon.",
        ) from exc
    try:
        return await connect(
            url, additional_headers=dict(headers), open_timeout=CONNECT_TIMEOUT_S,
            max_size=1_048_576, ping_interval=None, user_agent_header=None,
        )
    except InvalidStatus as exc:
        # This module-level connector has no call-local credential scrubber. Do
        # not include arbitrary provider bodies: truncating before _connect can
        # scrub them could expose a partial credential. Status and actionable
        # protocol guidance are safe and sufficient to classify the refusal.
        raise error_for_status(exc.response.status_code, sideband=True) from None


class _SendDropped(Exception):
    """Internal: a failed write invalidated the socket the reader was waiting on."""


class CodexLiveTransport:
    """Signaling POST plus sideband WebSocket, authenticated by the Codex OAuth manager."""

    def __init__(
        self,
        auth: Any,
        *,
        client: httpx.AsyncClient | None = None,
        connector: Connector | None = None,
        calls_url: str = CALLS_URL,
        sideband_url: str = SIDEBAND_URL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] | None = None,
        raw_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._auth = auth
        self._client = client
        self._owns_client = client is None
        self._connector = connector or _default_connector
        self._calls_url = calls_url
        self._sideband_url = sideband_url
        self._sleep = sleep
        self._clock = clock
        self._raw_log = raw_log
        self._ids = (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        self._headers_cache: dict[str, str] | None = None
        #: Literal credential values seen in the auth headers, scrubbed from every
        #: error text on top of ``redact_secrets`` (which cannot know an account id).
        self._secret_values: set[str] = set()
        self._call_id = ""
        self._ws: WebSocketLike | None = None
        self._send_lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self._on_event: EventHandler | None = None
        self._on_closed: ClosedHandler | None = None
        self._first_open = asyncio.Event()
        #: Set while ``_ws`` is a usable socket; cleared the moment a drop is detected.
        self._ready = asyncio.Event()
        #: Set once the transport is finished for good (close, or the reader ended).
        self._terminal = asyncio.Event()
        #: Set to tell the reader its current socket is damaged (send-detected drop).
        self._drop = asyncio.Event()
        #: Why the current socket was invalidated by a send (None: cancelled write / receive-detected).
        self._drop_cause: Exception | None = None
        #: Sockets taken out of service (damaged by a write, replaced, or finished) whose close has
        #: not completed. Whoever retires a socket owns closing it; a cancelled close leaves it
        #: here so ``_run``'s cleanup or ``close`` finishes it. Never written to, never reused;
        #: bounded because every drain empties it.
        self._retired: list[WebSocketLike] = []

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    # -- headers -----------------------------------------------------------

    def _clean(self, text: object, limit: int = MAX_DETAIL_CHARS) -> str:
        """``safe_detail`` plus removal of this call's literal credential values."""
        raw = " ".join(str(text).split())
        for value in self._secret_values:
            raw = raw.replace(value, "***")
        raw = _BEARER.sub("***", raw)  # never leave the scheme word behind a scrubbed token
        return safe_detail(raw, limit)

    def _describe_drop(self, exc: BaseException) -> str:
        """Exception text plus any WebSocket close metadata (type, rcvd/sent code and reason)."""
        parts = [self._clean(exc) or type(exc).__name__]
        if type(exc).__name__ not in parts[0]:
            parts.insert(0, type(exc).__name__)
        for side in ("rcvd", "sent"):
            frame = getattr(exc, side, None)
            if frame is None:
                parts.append(f"{side}=none")
                continue
            code = getattr(frame, "code", None)
            reason = self._clean(getattr(frame, "reason", "") or "", 80)
            parts.append(f"{side}={code}" + (f" {reason!r}" if reason else ""))
        return self._clean(" ".join(parts), 240)

    async def _headers(self) -> dict[str, str]:
        try:
            base = dict(await self._auth.headers())
        except Exception as exc:  # noqa: BLE001 - auth text may echo secrets
            raise LiveError("live_auth", safe_detail(exc) or "ChatGPT sign-in required; run `nexus auth codex login`") from None
        merged = {key.lower(): value for key, value in base.items()}
        for key, value in merged.items():
            if isinstance(value, str) and any(word in key for word in ("authorization", "account", "token", "residency")):
                self._secret_values.add(value.removeprefix("Bearer ").strip())
        self._secret_values.discard("")
        session_id, thread_id, realtime_id = self._ids
        # notes §1.1: random UUIDs per call; the Nexus session id is not sent.
        merged.update({"openai-alpha": ALPHA_HEADER, "session-id": session_id,
                       "thread-id": thread_id, "x-session-id": realtime_id})
        return merged

    # -- signaling -----------------------------------------------------------

    async def start(
        self, offer_sdp: str, session: LiveSession, *, on_event: EventHandler, on_closed: ClosedHandler
    ) -> CallAnswer:
        self._on_event, self._on_closed = on_event, on_closed
        headers = await self._headers()
        self._headers_cache = headers
        body = CallRequest(offer_sdp, session).to_payload()
        client = self._client or httpx.AsyncClient(timeout=SIGNALING_TIMEOUT_S)
        self._client = client
        request_headers = {**headers, "content-type": "application/json", "accept": "*/*"}
        response: httpx.Response | None = None
        for attempt in range(SIGNALING_5XX_RETRIES + 1):
            try:
                response = await client.post(
                    self._calls_url, content=json.dumps(body), headers=request_headers,
                    timeout=SIGNALING_TIMEOUT_S,
                )
            except httpx.TimeoutException:
                raise LiveError("live_signaling", "Timed out creating the call") from None
            except httpx.HTTPError as exc:
                raise LiveError("live_signaling", f"Could not reach GPT Live: {type(exc).__name__}") from None
            if response.status_code >= 500 and attempt < SIGNALING_5XX_RETRIES:
                await self._sleep(CONNECT_BASE_DELAY_S)
                continue
            break
        assert response is not None
        if not 200 <= response.status_code < 300:
            # Scrub the full buffered body before any display truncation, so a
            # credential straddling the limit cannot leave a visible fragment.
            raise error_for_status(response.status_code, self._clean(response.text))
        answer = response.text
        if not answer.strip():
            raise LiveError("live_signaling", "The call was created but the SDP answer was empty")
        if len(answer.encode()) > MAX_ANSWER_BYTES:
            raise LiveError("live_signaling", "The SDP answer was unexpectedly large")
        call_id = parse_call_id(
            response.headers.get("location"), self._calls_url, response.headers.get("openai-session-id")
        )
        self._call_id = call_id
        self._reader = asyncio.create_task(self._run(), name="nexus-live-sideband")
        return CallAnswer(call_id=call_id, answer_sdp=answer)

    # -- sideband ----------------------------------------------------------

    async def _connect(self, *, attempts: int, join: bool = False) -> WebSocketLike:
        """Open the sideband. ``join`` (initial open only) also retries an early 404 briefly."""
        # One open budget for initial connection and recovery. In particular, the
        # credential-refresh deadline must never be passed to this operation.
        timeout = CONNECT_TIMEOUT_S
        url = self._sideband_url.format(call_id=quote(self._call_id, safe=""))
        headers = {k: v for k, v in (self._headers_cache or {}).items() if k not in ("content-type", "accept")}
        last: Exception | None = None
        attempt = early_404 = 0
        while True:
            if self._closing:
                raise LiveError("live_closed", "The call is closing")
            try:
                return await asyncio.wait_for(self._connector(url, headers), timeout)
            except LiveError as exc:
                if join and isinstance(exc, LiveHTTPError) and exc.status == 404 and early_404 < len(JOIN_404_DELAYS_S):
                    last = exc
                    await self._sleep(JOIN_404_DELAYS_S[early_404])
                    early_404 += 1
                    continue
                if _terminal_open_error(exc):
                    if isinstance(exc, LiveHTTPError):
                        raise LiveHTTPError(exc.status, exc.code, self._clean(exc.message)) from None
                    raise LiveError(exc.code, self._clean(exc.message)) from None
                last = exc
            except Exception as exc:  # noqa: BLE001
                last = exc
            attempt += 1
            if attempt >= attempts:
                break
            await self._sleep(CONNECT_BASE_DELAY_S * (2 ** (attempt - 1)))
        # wait_for (and some WebSocket open failures) raises a TimeoutError with
        # an empty str(). Keep the type and bound explicit so the terminal error
        # doesn't lose the only useful diagnostic. Scrub before display as usual.
        if isinstance(last, TimeoutError):
            detail = f"TimeoutError: WebSocket open did not complete within {timeout:g}s"
            if str(last).strip():
                detail += f" ({last})"
        elif isinstance(last, LiveError):
            detail = f"{last.code}: {last.message}"
        elif last is not None:
            detail = f"{type(last).__name__}: {last}"
        else:
            detail = "No WebSocket open attempt completed"
        raise LiveError("live_sideband", f"Could not open the sideband connection: {self._clean(detail)}")

    async def _emit(self, event: LiveEvent) -> None:
        if self._on_event is not None:
            outcome = self._on_event(event)
            if asyncio.iscoroutine(outcome) or isinstance(outcome, asyncio.Future):
                await outcome

    async def _notify_closed(self, reason: str, error: str | None) -> None:
        handler, self._on_closed = self._on_closed, None
        if handler is not None and not self._closing:
            outcome = handler(reason, error)
            if asyncio.iscoroutine(outcome) or isinstance(outcome, asyncio.Future):
                await outcome

    def _install(self, ws: WebSocketLike) -> asyncio.Event:
        """Make ``ws`` the current socket and return its drop signal (reader only)."""
        self._ws = ws
        self._drop = drop = asyncio.Event()
        self._drop_cause = None
        self._ready.set()
        return drop

    def _invalidate(self, ws: WebSocketLike, cause: Exception | None = None) -> None:
        """Mark ``ws`` damaged: nothing may use it, and the reader (sole reconnect owner) is woken.

        A no-op if ``ws`` was already replaced or retired, so a receive-detected and a
        send-detected drop of the same socket start exactly one reconnect.
        """
        if self._ws is ws:
            self._ws = None
            self._retired.append(ws)  # ownership moves to the retired list until it is closed
            self._drop_cause = cause
            self._ready.clear()
            self._drop.set()

    async def _close_socket(self, ws: WebSocketLike, code: int = 1000, reason: str = "") -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code, reason), CLOSE_TIMEOUT_S)

    async def _close_retired(self, code: int = 1011, reason: str = "sideband replaced") -> None:
        """Close every retired socket. A socket stays owned until its close finishes, even on cancel."""
        while self._retired:
            ws = self._retired.pop(0)
            try:
                await self._close_socket(ws, code, reason)
            except BaseException:  # cancelled mid-close: keep ownership for the next drain
                self._retired.insert(0, ws)
                raise

    async def _read(self, ws: WebSocketLike, drop: asyncio.Event) -> tuple[str | bytes | None, Exception | None]:
        """Next frame from ``ws``, or the failure that ended it (a send-detected drop included)."""
        if drop.is_set():
            return None, self._drop_cause or _SendDropped("a write to the sideband failed")
        recv = asyncio.ensure_future(ws.recv())
        dropped = asyncio.ensure_future(drop.wait())
        try:
            await asyncio.wait({recv, dropped}, return_when=asyncio.FIRST_COMPLETED)
            if not recv.done():
                return None, self._drop_cause or _SendDropped("a write to the sideband failed")
            try:
                return recv.result(), None
            except Exception as exc:  # noqa: BLE001 - any socket failure
                return None, exc
        finally:
            recv.cancel()
            dropped.cancel()
            await asyncio.gather(recv, dropped, return_exceptions=True)

    async def _run(self) -> None:
        #: Consecutive failed opens and short-lived connections (Codex's ``n``); 0 after stability.
        attempts_used = 0
        failing_since = 0.0
        try:
            ws = await self._connect(attempts=CONNECT_ATTEMPTS, join=True)
            drop = self._install(ws)
            connected_at = self._now()
            self._first_open.set()
            await self._emit(SidebandOpen())
            while True:
                if self._closing:
                    return
                frame, exc = await self._read(ws, drop)
                if exc is not None:
                    lost_at = self._now()
                    self._invalidate(ws)  # clear readiness/socket before anything else can use it
                    await self._close_retired()
                    if self._closing:
                        return
                    if getattr(exc, "code", None) == 1000 or type(exc).__name__ == "ConnectionClosedOK":
                        await self._notify_closed("remote_closed", None)
                        return
                    if attempts_used == 0 or lost_at - connected_at >= RECONNECT_STABLE_AFTER_S:
                        attempts_used, failing_since = 0, lost_at
                    failure = self._clean(exc) or type(exc).__name__
                    recovered = False
                    while True:
                        if self._now() - failing_since >= RECOVERY_GIVE_UP_S:
                            failure += f"; gave up after {RECOVERY_GIVE_UP_S:g}s of continuous failure"
                            break
                        attempts_used += 1
                        await self._sleep(min(RECONNECT_BASE_DELAY_S * 2 ** min(attempts_used - 1, 10), RECONNECT_MAX_DELAY_S))
                        if self._closing:
                            return
                        try:
                            # Refresh on EVERY attempt; auth refusals never get retried.
                            self._headers_cache = await asyncio.wait_for(self._headers(), RECONNECT_TIMEOUT_S)
                            # An open includes DNS, TCP, TLS and HTTP upgrade. The old 5s
                            # outer wait cancelled the library's 15s open prematurely.
                            ws = await self._connect(attempts=1)
                        except asyncio.TimeoutError:
                            failure = "Timed out refreshing credentials"
                            break
                        except LiveError as err:
                            failure = err.message
                            if isinstance(err, LiveHTTPError) and err.status in (404, 410):
                                # The same call id opened this call earlier, so the service has
                                # stopped accepting it. The protocol has no rejoin or URL refresh,
                                # and a replacement call would lose the conversation: end instead.
                                failure += "; id was accepted earlier; no replacement call created"
                            if _terminal_open_error(err):
                                break
                        else:
                            recovered = True
                            break
                    if not recovered:
                        gap = self._now() - lost_at
                        detail = f"{failure}; recovery attempts used {attempts_used}, gap {gap:.1f}s; drop: {self._describe_drop(exc)}"
                        await self._notify_closed("sideband_lost", self._clean(detail))
                        return
                    if self._closing:
                        self._retired.append(ws)
                        await self._close_retired(1000, "")
                        return
                    drop = self._install(ws)
                    connected_at = self._now()
                    await self._emit(SidebandReconnected(gap_s=max(0.0, connected_at - lost_at)))
                    continue
                if isinstance(frame, bytes):
                    await self._notify_closed("protocol_error", "Unexpected binary sideband frame")
                    return
                if self._raw_log is not None:
                    self._raw_log("in", self._clean(frame, 8192))
                await self._emit(parse_event(frame))
        except asyncio.CancelledError:
            raise
        except LiveError as exc:
            await self._notify_closed("startup_failed" if not self._first_open.is_set() else "sideband_lost", exc.message)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback or token
            await self._notify_closed("transport_error", self._clean(exc))
        finally:
            # The reader is the only reconnect owner: once it ends, nothing can recover.
            self._terminal.set()
            self._ready.clear()
            leftover, self._ws = self._ws, None
            if leftover is not None:
                self._retired.append(leftover)
            await self._close_retired(1000, "")

    # -- send ----------------------------------------------------------------

    def _not_sent(self, why: str) -> LiveError:
        return LiveError("live_not_connected", f"The GPT Live sideband is not connected ({why}); the message was not sent")

    async def _race(self, *awaitables: Awaitable[Any], timeout: float) -> None:
        """Wait until one of ``awaitables`` finishes or ``timeout`` passes; cancel the rest."""
        tasks = [asyncio.ensure_future(item) for item in awaitables]
        try:
            await asyncio.wait(tasks, timeout=max(timeout, 0.0), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _acquire_send_lock(self, deadline: float) -> None:
        """Take the send lock, giving up at ``deadline`` or on terminal close (never hangs)."""
        loop = asyncio.get_running_loop()
        locking = asyncio.ensure_future(self._send_lock.acquire())
        terminal = asyncio.ensure_future(self._terminal.wait())
        try:
            await asyncio.wait({locking, terminal}, timeout=max(deadline - loop.time(), 0.0),
                               return_when=asyncio.FIRST_COMPLETED)
        except BaseException:  # our own cancellation: never leak a lock that was already granted
            terminal.cancel()
            if locking.done() and not locking.cancelled():
                self._send_lock.release()
            else:
                locking.cancel()
            raise
        terminal.cancel()
        got = locking.done() and not locking.cancelled()
        if not got:
            locking.cancel()  # asyncio.Lock passes the lock on if a grant raced this cancel
            raise self._not_sent("closed" if self._terminal.is_set() else "timed out waiting for the connection")
        if self._terminal.is_set():
            self._send_lock.release()
            raise self._not_sent("closed")

    async def _socket_for_send(self, deadline: float) -> WebSocketLike:
        """The current ready socket, read under the send lock; waits (bounded) for a reconnect."""
        loop = asyncio.get_running_loop()
        while True:
            if self._terminal.is_set() or self._closed:
                raise self._not_sent("closed")
            ws = self._ws
            if ws is not None and self._ready.is_set():
                return ws
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise self._not_sent("timed out waiting for the connection")
            await self._race(self._ready.wait(), self._terminal.wait(), timeout=remaining)

    async def send(self, message: Mapping[str, Any]) -> None:
        """Send one message in order.

        ``live_not_connected`` means *definitely not sent* (closed, or no socket within
        ``SEND_WAIT_TIMEOUT_S``). ``live_send_ambiguous`` means a write was attempted and
        raised or timed out, so delivery is unknown: the message is never replayed and the
        damaged socket is dropped so the reader recovers.
        """
        if self._closed or self._terminal.is_set() or self._reader is None:
            raise self._not_sent("closed" if self._closed or self._terminal.is_set() else "not started")
        try:
            # ASCII escapes keep any text (even a lone surrogate) UTF-8 encodable on the wire.
            text = json.dumps(message, ensure_ascii=True)
            deadline = asyncio.get_running_loop().time() + SEND_WAIT_TIMEOUT_S
            await self._acquire_send_lock(deadline)
            try:
                ws = await self._socket_for_send(deadline)  # read AFTER the lock: never a stale socket
                if self._raw_log is not None:
                    self._raw_log("out", self._clean(text, 8192))
                try:
                    await asyncio.wait_for(ws.send(text), SEND_TIMEOUT_S)
                except asyncio.CancelledError:
                    self._invalidate(ws)  # a half-written frame may be on the wire; now retired
                    raise
                except Exception as exc:  # noqa: BLE001 - raised or timed out: delivery unknown
                    self._invalidate(ws, exc)
                    await self._close_retired(1011, "send failed")
                    detail = self._clean(exc) or type(exc).__name__
                    raise LiveError(
                        "live_send_ambiguous",
                        f"A message to GPT Live may or may not have been delivered ({detail}). "
                        "It was not retried; the connection is being re-established, so check the "
                        "call's state before sending it again.",
                    ) from None
            finally:
                self._send_lock.release()
        except (LiveError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - legacy fatal fallback
            raise LiveError("live_send_failed", f"Could not send to GPT Live: {self._clean(exc)}") from None

    async def close(self, reason: str = "done") -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        self._terminal.set()  # wakes every waiting send
        self._ready.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            # session.close must not interleave with a write, but a stuck writer must not
            # block close: after CLOSE_TIMEOUT_S skip the message; closing the socket frees it.
            # Best effort. A damaged socket was already invalidated (so ``ws`` is never one),
            # and it is closed from ``_retired`` below without a ``session.close`` write.
            locked = False
            try:
                await asyncio.wait_for(self._send_lock.acquire(), CLOSE_TIMEOUT_S)
                locked = True
                await asyncio.wait_for(ws.send(json.dumps(close_message())), CLOSE_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                pass
            finally:
                if locked:
                    self._send_lock.release()
            self._retired.append(ws)
        reader, self._reader = self._reader, None
        try:
            await self._close_retired(1000, reason[:100])  # includes sockets a cancelled send damaged
        finally:
            if reader is not None and reader is not asyncio.current_task():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader  # its cleanup closes anything still retired
        if self._owns_client and self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None
        self._headers_cache = None  # drop the bearer copy


__all__ = [
    "ALPHA_HEADER", "CALLS_URL", "SIDEBAND_URL", "CallAnswer", "CodexLiveTransport", "LiveTransport",
    "error_for_status", "parse_call_id", "safe_detail", "websocket_available",
]
