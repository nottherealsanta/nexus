"""Signaling POST and sideband WebSocket for a GPT-Live call, authenticated by the Codex sign-in. **Not verified.**

Condensed from old Nexus ``nexus/live/transport.py``. This is the only module that sees the ChatGPT bearer; every
error text is scrubbed and bounded. The endpoints and headers were read from reference integrations (Codex,
opencode-gpt-live, oh-my-pi) and may change without notice.

Recovery follows Codex: no client pings, backoff ``0.2 * 2**n`` capped at 5 s, a refusal (auth, 404/410) or a clean
close ends it, and recovery gives up after five minutes of continuous failure. A message whose write failed is never
replayed (``live_send_ambiguous``); one that was never written says ``live_not_connected``.
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

from ...manager.redact import redact
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

CALLS_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
SIDEBAND_URL = "wss://api.openai.com/v1/live/{call_id}"
ALPHA_HEADER = "quicksilver=v2"
CONNECT_ATTEMPTS = 5
CONNECT_TIMEOUT_S = 15.0
SIGNALING_TIMEOUT_S = 30.0
JOIN_404_DELAYS_S = (0.1, 0.3, 0.8)  # a fresh call's sideband can answer 404 briefly
RECONNECT_BASE_DELAY_S = 0.2
RECONNECT_MAX_DELAY_S = 5.0
RECOVERY_GIVE_UP_S = 300.0
SEND_TIMEOUT_S = 5.0
SEND_WAIT_TIMEOUT_S = 10.0
CLOSE_TIMEOUT_S = 1.0
MAX_DETAIL_CHARS = 300
MAX_ANSWER_BYTES = 262_144
_AUTH_CODES = ("live_auth", "live_forbidden", "live_rate_limited")
_BEARER = re.compile(r"(?i)\bbearer\b(?:\s+(?:\*{3}|[A-Za-z0-9._~+/=-]+))?")


class WebSocketLike(Protocol):
    async def send(self, text: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


Connector = Callable[[str, Mapping[str, str]], Awaitable[WebSocketLike]]
EventHandler = Callable[[LiveEvent], Awaitable[None] | None]
ClosedHandler = Callable[[str, str | None], Awaitable[None] | None]


class CallAnswer:
    def __init__(self, call_id: str, answer_sdp: str) -> None:
        self.call_id, self.answer_sdp = call_id, answer_sdp


class LiveTransport(Protocol):
    async def start(self, offer_sdp: str, session: LiveSession, *, on_event: EventHandler,
                    on_closed: ClosedHandler) -> CallAnswer: ...
    async def send(self, message: Mapping[str, Any]) -> None: ...
    async def close(self, reason: str = "done") -> None: ...


class LiveHTTPError(LiveError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code, message)
        self.status = status
        self.retryable = status == 408 or 500 <= status < 600


def safe_detail(text: object, limit: int = MAX_DETAIL_CHARS) -> str:
    return redact(" ".join(str(text).split()))[:limit]


def error_for_status(status: int, detail: str = "", *, sideband: bool = False) -> LiveHTTPError:
    if status == 401:
        return LiveHTTPError(status, "live_auth", "ChatGPT rejected the sign-in; run `nexus login codex` and try again")
    if status == 403:
        return LiveHTTPError(status, "live_forbidden",
                             "GPT-Live is not available on this ChatGPT plan, or the voice allowance is used up")
    if status == 429:
        return LiveHTTPError(status, "live_rate_limited", "The GPT-Live usage limit was reached; try again later")
    text = f"sideband upgrade rejected ({status})" if sideband else f"call creation failed ({status})"
    if status in (404, 410):
        text += "; the call or endpoint is unavailable, or the protocol changed; not retrying"
    return LiveHTTPError(status, "live_sideband" if sideband else "live_signaling",
                         f"{text}: {safe_detail(detail)}" if detail.strip() else text)


def parse_call_id(location: str | None, url: str, fallback: str | None = None) -> str:
    if location:
        for segment in urlsplit(urljoin(url, location.strip())).path.split("/"):
            if valid_call_id(segment):
                return segment
    if fallback and valid_call_id(fallback.strip()):
        return fallback.strip()
    raise LiveError("live_no_call_id", "The call was created but the response carried no valid call id")


async def _default_connector(url: str, headers: Mapping[str, str]) -> WebSocketLike:
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    try:
        return await connect(url, additional_headers=dict(headers), open_timeout=CONNECT_TIMEOUT_S,
                             max_size=1_048_576, ping_interval=None, user_agent_header=None,
                             ssl=httpx.create_ssl_context())
    except InvalidStatus as exc:  # the body could echo a credential: keep the status only
        raise error_for_status(exc.response.status_code, sideband=True) from None


def _terminal(error: LiveError) -> bool:
    return (isinstance(error, LiveHTTPError) and not error.retryable) or error.code in (*_AUTH_CODES, "live_closed")


class CodexLiveTransport:
    def __init__(self, auth: Any, *, client: httpx.AsyncClient | None = None, connector: Connector | None = None,
                 calls_url: str = CALLS_URL, sideband_url: str = SIDEBAND_URL,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._auth = auth
        self._client = client
        self._owns_client = client is None
        self._connector = connector or _default_connector
        self._calls_url, self._sideband_url, self._sleep = calls_url, sideband_url, sleep
        self._ids = (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        self._headers_cache: dict[str, str] = {}
        self._secrets: set[str] = set()
        self._call_id = ""
        self._ws: WebSocketLike | None = None
        self._ready = asyncio.Event()
        self._terminal_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._closing = False
        self._on_event: EventHandler | None = None
        self._on_closed: ClosedHandler | None = None

    def _clean(self, text: object, limit: int = MAX_DETAIL_CHARS) -> str:
        raw = " ".join(str(text).split())
        for value in self._secrets:
            raw = raw.replace(value, "***")
        return safe_detail(_BEARER.sub("***", raw), limit)

    async def _headers(self) -> dict[str, str]:
        try:
            base = dict(await self._auth.headers())
        except Exception as exc:  # noqa: BLE001 - auth text may echo secrets
            raise LiveError("live_auth", safe_detail(exc) or "ChatGPT sign-in required; run `nexus login codex`") from None
        merged = {k.lower(): v for k, v in base.items()}
        for key, value in merged.items():
            if isinstance(value, str) and any(w in key for w in ("authorization", "account", "token")):
                self._secrets.add(value.removeprefix("Bearer ").strip())
        self._secrets.discard("")
        session_id, thread_id, realtime_id = self._ids
        merged.update({"openai-alpha": ALPHA_HEADER, "session-id": session_id, "thread-id": thread_id,
                       "x-session-id": realtime_id})
        return merged

    async def start(self, offer_sdp: str, session: LiveSession, *, on_event: EventHandler,
                    on_closed: ClosedHandler) -> CallAnswer:
        self._on_event, self._on_closed = on_event, on_closed
        self._headers_cache = await self._headers()
        client = self._client or httpx.AsyncClient(timeout=SIGNALING_TIMEOUT_S)
        self._client = client
        headers = {**self._headers_cache, "content-type": "application/json", "accept": "*/*"}
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                response = await client.post(self._calls_url, content=json.dumps(CallRequest(offer_sdp, session).to_payload()),
                                             headers=headers, timeout=SIGNALING_TIMEOUT_S)
            except httpx.TimeoutException:
                raise LiveError("live_signaling", "Timed out creating the call") from None
            except httpx.HTTPError as exc:
                raise LiveError("live_signaling", f"Could not reach GPT-Live: {type(exc).__name__}") from None
            if response.status_code >= 500 and attempt == 0:
                await self._sleep(0.2)
                continue
            break
        assert response is not None
        if not 200 <= response.status_code < 300:
            raise error_for_status(response.status_code, self._clean(response.text))
        answer = response.text
        if not answer.strip():
            raise LiveError("live_signaling", "The call was created but the SDP answer was empty")
        if len(answer.encode()) > MAX_ANSWER_BYTES:
            raise LiveError("live_signaling", "The SDP answer was unexpectedly large")
        self._call_id = parse_call_id(response.headers.get("location"), self._calls_url,
                                      response.headers.get("openai-session-id"))
        opened: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._run(opened), name="nexus-gpt-live-sideband")
        # The call exists once we have an answer; the sideband must open before the client's media matters.
        try:
            await asyncio.shield(opened)
        except LiveError:
            await self.close("startup_failed")
            raise
        return CallAnswer(self._call_id, answer)

    async def _connect(self, *, join: bool) -> WebSocketLike:
        url = self._sideband_url.format(call_id=quote(self._call_id, safe=""))
        headers = {k: v for k, v in self._headers_cache.items() if k not in ("content-type", "accept")}
        early = attempt = 0
        last: Exception | None = None
        while True:
            if self._closing:
                raise LiveError("live_closed", "The call is closing")
            try:
                return await asyncio.wait_for(self._connector(url, headers), CONNECT_TIMEOUT_S)
            except LiveError as exc:
                if join and isinstance(exc, LiveHTTPError) and exc.status == 404 and early < len(JOIN_404_DELAYS_S):
                    await self._sleep(JOIN_404_DELAYS_S[early])
                    early += 1
                    continue
                if _terminal(exc):
                    raise LiveError(exc.code, self._clean(exc.message)) from None
                last = exc
            except Exception as exc:  # noqa: BLE001
                last = exc
            attempt += 1
            if attempt >= (CONNECT_ATTEMPTS if join else 1):
                break
            await self._sleep(0.2 * 2 ** (attempt - 1))
        detail = f"{type(last).__name__}: {last}" if last is not None else "no attempt completed"
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

    async def _run(self, opened: asyncio.Future[None]) -> None:
        loop = asyncio.get_running_loop()
        try:
            try:
                ws = await self._connect(join=True)
            except LiveError as exc:
                if not opened.done():
                    opened.set_exception(exc)
                return
            self._ws = ws
            self._ready.set()
            opened.set_result(None)
            await self._emit(SidebandOpen())
            while not self._closing:
                try:
                    frame = await ws.recv()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any socket failure
                    lost = loop.time()
                    self._ready.clear()
                    self._ws = None
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(ws.close(1011, "sideband lost"), CLOSE_TIMEOUT_S)
                    if self._closing:
                        return
                    if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1000:
                        await self._notify_closed("remote_closed", None)
                        return
                    ws, failure = await self._recover(lost, exc)
                    if ws is None:
                        await self._notify_closed("sideband_lost", failure)
                        return
                    self._ws = ws
                    self._ready.set()
                    await self._emit(SidebandReconnected(gap_s=max(0.0, loop.time() - lost)))
                    continue
                if isinstance(frame, bytes):
                    await self._notify_closed("protocol_error", "Unexpected binary sideband frame")
                    return
                await self._emit(parse_event(frame))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a traceback or token
            await self._notify_closed("transport_error", self._clean(exc))
        finally:
            self._terminal_event.set()
            self._ready.clear()
            ws, self._ws = self._ws, None
            if ws is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ws.close(1000, ""), CLOSE_TIMEOUT_S)

    async def _recover(self, lost: float, exc: BaseException) -> tuple[WebSocketLike | None, str]:
        loop = asyncio.get_running_loop()
        failure = self._clean(exc) or type(exc).__name__
        attempts = 0
        while loop.time() - lost < RECOVERY_GIVE_UP_S:
            attempts += 1
            await self._sleep(min(RECONNECT_BASE_DELAY_S * 2 ** min(attempts - 1, 10), RECONNECT_MAX_DELAY_S))
            if self._closing:
                return None, "closed"
            try:
                self._headers_cache = await asyncio.wait_for(self._headers(), 5.0)
                return await self._connect(join=False), ""
            except TimeoutError:
                failure = "Timed out refreshing credentials"
                break
            except LiveError as err:
                failure = err.message
                if _terminal(err):
                    break
        return None, f"{failure}; recovery attempts {attempts}"

    def _not_sent(self, why: str) -> LiveError:
        return LiveError("live_not_connected", f"The GPT-Live sideband is not connected ({why}); the message was not sent")

    async def send(self, message: Mapping[str, Any]) -> None:
        if self._closing or self._terminal_event.is_set() or self._reader is None:
            raise self._not_sent("closed")
        text = json.dumps(message, ensure_ascii=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SEND_WAIT_TIMEOUT_S
        try:
            async with asyncio.timeout_at(deadline):
                async with self._send_lock:
                    while not self._ready.is_set():
                        if self._terminal_event.is_set():
                            raise self._not_sent("closed")
                        await asyncio.sleep(0.05)
                    ws = self._ws
                    assert ws is not None
                    try:
                        await asyncio.wait_for(ws.send(text), SEND_TIMEOUT_S)
                    except Exception as exc:  # noqa: BLE001 - delivery unknown; never replayed
                        self._ready.clear()
                        self._ws = None
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(ws.close(1011, "send failed"), CLOSE_TIMEOUT_S)
                        raise LiveError(
                            "live_send_ambiguous",
                            f"A message to GPT-Live may or may not have been delivered ({self._clean(exc) or type(exc).__name__}); "
                            "it was not retried.") from None
        except TimeoutError:
            raise self._not_sent("timed out waiting for the connection") from None

    async def close(self, reason: str = "done") -> None:
        if self._closing:
            return
        self._closing = True
        self._terminal_event.set()
        self._ready.clear()
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
        self._headers_cache = {}
