"""Signaling and sideband transport for a GPT Live call.

``LiveTransport`` is the seam the manager depends on; ``CodexLiveTransport`` is
the only implementation. It is the **only** module that sees the ChatGPT OAuth
bearer (via ``CodexOAuthManager.headers()``): credentials never reach events,
logs, host results or the audio helper, and every error text passes
``redact_secrets`` and is bounded before it leaves this module.

Protocol constants are pinned to two reference integrations (both MIT; see
``NOTICE``) as recorded in ``plans/in-progress/GPT_LIVE_PROTOCOL_NOTES.md``
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
#: Plan §6: a dropped sideband gets a reconnect attempt within 5 s, at most twice per call.
RECONNECT_TIMEOUT_S = 5.0
MAX_RECONNECTS = 2
SEND_TIMEOUT_S = 5.0
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


def safe_detail(text: object, limit: int = MAX_DETAIL_CHARS) -> str:
    """Collapse whitespace, redact credentials and bound ``text`` for display."""
    return redact_secrets(" ".join(str(text).split()))[:limit]


def error_for_status(status: int, detail: str = "") -> LiveError:
    """Map a signaling HTTP status to an actionable, redacted error (notes §8.1)."""
    if status == 401:
        return LiveError("live_auth", "ChatGPT rejected the sign-in; run `nexus auth codex login` and try again")
    if status == 403:
        return LiveError(
            "live_forbidden",
            "GPT Live is not available on this ChatGPT plan, or the voice allowance is used up",
        )
    if status == 429:
        return LiveError("live_rate_limited", "The GPT Live usage limit was reached; try again later")
    text = f"call creation failed ({status})"
    return LiveError("live_signaling", f"{text}: {safe_detail(detail)}" if detail.strip() else text)


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
    """Whether the optional ``websockets`` client (the ``live`` extra) can be imported."""
    import importlib.util

    return importlib.util.find_spec("websockets") is not None


async def _default_connector(url: str, headers: Mapping[str, str]) -> WebSocketLike:
    """Connect with ``websockets`` (optional ``live`` extra), imported lazily."""
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise LiveError(
            "live_unsupported", "GPT Live needs the WebSocket client: install it with `pip install 'nexus-harness[live]'`"
        ) from exc
    try:
        return await connect(
            url, additional_headers=dict(headers), open_timeout=CONNECT_TIMEOUT_S,
            max_size=1_048_576, ping_interval=20, ping_timeout=20, user_agent_header=None,
        )
    except InvalidStatus as exc:
        raise error_for_status(exc.response.status_code) from None


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
        raw_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._auth = auth
        self._client = client
        self._owns_client = client is None
        self._connector = connector or _default_connector
        self._calls_url = calls_url
        self._sideband_url = sideband_url
        self._sleep = sleep
        self._raw_log = raw_log
        self._ids = (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        self._headers_cache: dict[str, str] | None = None
        self._call_id = ""
        self._ws: WebSocketLike | None = None
        self._send_lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self._on_event: EventHandler | None = None
        self._on_closed: ClosedHandler | None = None
        self._first_open = asyncio.Event()

    # -- headers -----------------------------------------------------------

    async def _headers(self) -> dict[str, str]:
        try:
            base = dict(await self._auth.headers())
        except Exception as exc:  # noqa: BLE001 - auth text may echo secrets
            raise LiveError("live_auth", safe_detail(exc) or "ChatGPT sign-in required; run `nexus auth codex login`") from None
        merged = {key.lower(): value for key, value in base.items()}
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
                raise LiveError("live_signaling", f"Could not reach GPT Live: {safe_detail(type(exc).__name__)}") from None
            if response.status_code >= 500 and attempt < SIGNALING_5XX_RETRIES:
                await self._sleep(CONNECT_BASE_DELAY_S)
                continue
            break
        assert response is not None
        if not 200 <= response.status_code < 300:
            raise error_for_status(response.status_code, response.text[:2048])
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

    async def _connect(self, *, attempts: int, timeout: float) -> WebSocketLike:
        url = self._sideband_url.format(call_id=quote(self._call_id, safe=""))
        headers = {k: v for k, v in (self._headers_cache or {}).items() if k not in ("content-type", "accept")}
        last: Exception | None = None
        for attempt in range(attempts):
            if self._closing:
                raise LiveError("live_closed", "The call is closing")
            try:
                return await asyncio.wait_for(self._connector(url, headers), timeout)
            except LiveError as exc:
                if exc.code in _AUTH_CODES or exc.code == "live_unsupported":
                    raise  # notes "Discrepancies": do not retry auth failures
                last = exc
            except Exception as exc:  # noqa: BLE001
                last = exc
            if attempt + 1 < attempts:
                await self._sleep(CONNECT_BASE_DELAY_S * (2 ** attempt))
        raise LiveError("live_sideband", f"Could not open the sideband connection: {safe_detail(last or '')}")

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

    async def _run(self) -> None:
        reconnects = 0
        try:
            self._ws = await self._connect(attempts=CONNECT_ATTEMPTS, timeout=CONNECT_TIMEOUT_S)
            self._first_open.set()
            await self._emit(SidebandOpen())
            while True:
                ws = self._ws
                try:
                    frame = await ws.recv()  # type: ignore[union-attr]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any socket failure
                    if self._closing:
                        return
                    if getattr(exc, "code", None) == 1000 or type(exc).__name__ == "ConnectionClosedOK":
                        await self._notify_closed("remote_closed", None)
                        return
                    if reconnects >= MAX_RECONNECTS:
                        await self._notify_closed("sideband_lost", safe_detail(exc) or "connection lost")
                        return
                    reconnects += 1
                    try:
                        self._ws = await self._connect(attempts=1, timeout=RECONNECT_TIMEOUT_S)
                    except LiveError as err:
                        await self._notify_closed("sideband_lost", err.message)
                        return
                    continue
                if isinstance(frame, bytes):
                    await self._notify_closed("protocol_error", "Unexpected binary sideband frame")
                    return
                if self._raw_log is not None:
                    self._raw_log("in", frame)
                await self._emit(parse_event(frame))
        except asyncio.CancelledError:
            raise
        except LiveError as exc:
            await self._notify_closed("startup_failed" if not self._first_open.is_set() else "sideband_lost", exc.message)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback or token
            await self._notify_closed("transport_error", safe_detail(exc))

    async def send(self, message: Mapping[str, Any]) -> None:
        ws = self._ws
        if ws is None or self._closed:
            raise LiveError("live_not_connected", "The GPT Live sideband is not connected")
        text = json.dumps(message, ensure_ascii=False)
        if self._raw_log is not None:
            self._raw_log("out", text)
        try:
            async with self._send_lock:
                await asyncio.wait_for(ws.send(text), SEND_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            raise LiveError("live_send_failed", f"Could not send to GPT Live: {safe_detail(exc)}") from None

    async def close(self, reason: str = "done") -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send(json.dumps(close_message())), CLOSE_TIMEOUT_S)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(1000, reason[:100]), CLOSE_TIMEOUT_S)
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if self._owns_client and self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None
        self._headers_cache = None  # drop the bearer copy


__all__ = [
    "ALPHA_HEADER", "CALLS_URL", "SIDEBAND_URL", "CallAnswer", "CodexLiveTransport", "LiveTransport",
    "error_for_status", "parse_call_id", "safe_detail", "websocket_available",
]
