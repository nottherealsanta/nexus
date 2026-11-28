"""HTTP adapter for the recorder service (plan 54, B3).

Every handler validates on the event loop (cheap, no I/O), then runs the synchronous
:class:`~nexus.recorder.service.RecorderService` on the recorder host's single worker through
``host.call``. Nothing here takes the service lock, reads a recording or fsyncs on the loop.

Transport rules:

* Authentication is the daemon's existing middleware: the bearer token when one is configured, and the
  documented trusted-network policy when the daemon is bound without one. This module adds no second
  login. Lease and recovery tokens travel only in headers, never in paths or query strings.
* JSON bodies are capped at 4 KiB and must be ``application/json`` (a cross-origin simple request
  cannot start or stop a capture). Chunk bodies must be ``application/octet-stream``, are capped at
  ``MAX_CHUNK_BYTES`` by Content-Length *and* by a streaming read, and at most
  ``RECORDER_MAX_INFLIGHT_BODIES`` are buffered at once.
* A WAV download is only produced for a recording whose capture has stopped; it is streamed from the
  store's verified prefix on the anyio threadpool, so a long download never blocks chunk commits.
* Create requires a client-generated recovery capability in ``X-Recorder-Recovery``; Core stores only
  its hash and never returns it. A ``mode: "resume"`` create is lookup-only and never creates.
* Raw lease tokens, recovery capabilities and stream ids never appear in a response body except the
  lease that create/replay deliberately hands to its owner, and never in a log line or error detail here.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import suppress
from typing import Any

from anyio import CancelScope
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .jsonenc import JSONResponse
from .recorder.host import RecorderHost, RecorderUnavailable
from .recorder.scheduler import SchedulerError
from .recorder.service import MAX_SEGMENT_ID, MIN_RECOVERY_LENGTH, RecorderServiceError
from .recorder.store import (
    MAX_CHUNK_BYTES,
    MAX_SEQUENCE,
    RecorderConflict,
    RecorderCorrupt,
    RecorderError,
    RecorderInvalid,
    RecorderNotFound,
    RecorderSecurityError,
)
from .recorder.transcript_log import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, TranscriptLog

log = logging.getLogger(__name__)

MAX_RECORDER_JSON = 4096
RECORDER_MAX_INFLIGHT_BODIES = 4
MAX_LEASE_HEADER = 512
MAX_RECOVERY_HEADER = 512
MAX_STREAM_HEADER = 128
MAX_CLIENT_HEADER = 128
MAX_LIST_LIMIT = 50
TRANSCRIPT_EXPORT_PAGE = MAX_PAGE_LIMIT
MAX_RETRY_SCOPE = 100
MAX_TRANSCRIPT_EXPORTS = 4  # concurrent exports pinning one recording's transcript against deletion
EXPORT_PAGE_RETRY_SECONDS = 10.0  # total bounded wait for one later transcript page
EXPORT_PAGE_RETRY_MIN_SLEEP = 0.05
EXPORT_PAGE_RETRY_MAX_SLEEP = 1.0
_RETRYABLE_EXPORT_CODES = frozenset({"recorder_backpressure", "recorder_background_full"})

_RECORDING_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_SEQUENCE = re.compile(r"\A[0-9]{1,9}\Z")
# The daemon's public idempotency-key rule (a subset of the service's alphabet); one rule for clients.
_IDEMPOTENCY_KEY = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
_DETAIL_KEYS = frozenset({"durable_offset", "final_sample_count", "sequence", "chunks",
                          "durable_bytes", "start", "next_sequence"})
_DONE = object()


class HttpError(Exception):
    """A recorder route rejection with the HTTP status, stable code and optional headers."""

    def __init__(self, status: int, code: str, message: str,
                 *, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.headers = headers


class _Inflight:
    """A loop-local counter bounding buffered chunk bodies (no cross-thread state)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0

    def acquire(self) -> bool:
        if self._used >= self._limit:
            return False
        self._used += 1
        return True

    def release(self) -> None:
        if self._used:
            self._used -= 1


class _ExportPins:
    """Loop-local read pins: a transcript export must finish before its recording can be deleted.

    The durable log is read page by page, so deleting the recording between pages would otherwise turn a
    `200` export into a truncated body or invalid JSON. A pin is bounded per recording and is released by
    the response on completion, error or client disconnect. It never touches the service lock.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active: dict[str, int] = {}

    def acquire(self, recording_id: str) -> bool:
        count = self._active.get(recording_id, 0)
        if count >= self._limit:
            return False
        self._active[recording_id] = count + 1
        return True

    def release(self, recording_id: str) -> None:
        count = self._active.get(recording_id, 0)
        if count <= 1:
            self._active.pop(recording_id, None)
        else:
            self._active[recording_id] = count - 1

    def active(self, recording_id: str) -> bool:
        return recording_id in self._active


class _PinnedExportResponse(StreamingResponse):
    """Release an export pin even when the response never starts or the client disconnects."""

    def __init__(self, *args: Any, release: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._release_export = release

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release_export()


def _cache_headers() -> dict[str, str]:
    return {"cache-control": "no-store"}


def _json(value: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    merged = _cache_headers()
    if headers:
        merged.update(headers)
    return JSONResponse(value, status_code=status, headers=merged)


def _details(exc: RecorderError) -> dict[str, int]:
    """Only integer progress fields; never a hash, token, stream id or free text from the store."""
    return {key: value for key, value in (getattr(exc, "details", None) or {}).items()
            if key in _DETAIL_KEYS and type(value) is int}


def _error_response(exc: Exception) -> Response:
    if isinstance(exc, HttpError):
        return _json({"error": str(exc), "code": exc.code}, status=exc.status, headers=exc.headers)
    if isinstance(exc, RecorderUnavailable):
        headers = {"retry-after": str(exc.retry_after)} if getattr(exc, "retry_after", None) else None
        return _json({"error": str(exc), "code": getattr(exc, "code", "recorder_unavailable")},
                     status=getattr(exc, "status", 503), headers=headers)
    if isinstance(exc, RecorderServiceError):
        body: dict[str, Any] = {"error": str(exc), "code": exc.code}
        body.update(_details(exc))
        return _json(body, status=exc.status)
    if isinstance(exc, RecorderNotFound):
        return _json({"error": str(exc), "code": exc.code}, status=404)
    if isinstance(exc, RecorderCorrupt):
        return _json({"error": str(exc), "code": getattr(exc, "code", None) or "recording_corrupt",
                      **_details(exc)}, status=409)
    if isinstance(exc, RecorderSecurityError):
        # The recording id would be the only useful context, and this layer does not log it either.
        log.warning("recorder storage security check failed")
        return _json({"error": "the recording storage is not private", "code": "recording_insecure"},
                     status=500)
    if isinstance(exc, RecorderConflict):
        body = {"error": str(exc), "code": exc.code}
        body.update(_details(exc))
        return _json(body, status=409)
    if isinstance(exc, RecorderInvalid):
        return _json({"error": str(exc), "code": exc.code}, status=400)
    log.warning("recorder request failed (%s)", type(exc).__name__)
    code = getattr(exc, "code", "recorder_error")
    return _json({"error": "the recorder request failed", "code": code}, status=500)


def _checked(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
    """Map recorder/service/transport exceptions to the documented JSON errors."""

    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001 - every recorder failure has a stable HTTP shape
            return _error_response(exc)

    return wrapper


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").partition(";")[0].strip().lower()


def _content_length(request: Request) -> int:
    raw = request.headers.get("content-length")
    if raw is None:
        raise HttpError(411, "length_required", "Content-Length is required")
    try:
        value = int(raw)
    except ValueError as exc:
        raise HttpError(411, "length_required", "Content-Length must be an integer") from exc
    if value < 0:
        raise HttpError(411, "length_required", "Content-Length must not be negative")
    return value


async def _bounded_json(request: Request) -> dict[str, Any]:
    if _media_type(request) != "application/json":
        raise HttpError(415, "unsupported_media_type", "recorder JSON requests must be application/json")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HttpError(400, "bad_request", "Content-Length must be an integer") from exc
        if length < 0:
            raise HttpError(400, "bad_request", "Content-Length must not be negative")
        if length > MAX_RECORDER_JSON:
            raise HttpError(413, "request_too_large", "recorder request body is too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_RECORDER_JSON:
            raise HttpError(413, "request_too_large", "recorder request body is too large")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise HttpError(400, "invalid_json", "request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise HttpError(400, "invalid_json", "request body must be a JSON object")
    return value


async def _bounded_chunk(request: Request, inflight: _Inflight) -> bytes:
    """Read one bounded chunk body. The caller owns the budget until its commit finishes.

    On success the acquired slot is *not* released here: the route releases it after `host.call`
    returns, so at most ``RECORDER_MAX_INFLIGHT_BODIES`` chunk commits (not just reads) are in flight.
    """
    if _media_type(request) != "application/octet-stream":
        raise HttpError(415, "unsupported_media_type", "chunk bodies must be application/octet-stream")
    length = _content_length(request)
    if length > MAX_CHUNK_BYTES:
        raise HttpError(413, "chunk_too_large",
                        f"chunk body exceeds {MAX_CHUNK_BYTES} bytes")
    if not inflight.acquire():
        raise HttpError(503, "recorder_backpressure",
                        "too many chunks are being received; retry shortly",
                        headers={"retry-after": "1"})
    try:
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_CHUNK_BYTES:
                raise HttpError(413, "chunk_too_large",
                                f"chunk body exceeds {MAX_CHUNK_BYTES} bytes")
        return bytes(data)
    except BaseException:
        inflight.release()
        raise


def _recording_id(request: Request) -> str:
    value = request.path_params.get("id", "")
    if not isinstance(value, str) or not _RECORDING_ID.fullmatch(value):
        raise HttpError(400, "bad_request", "recording id must be 32 lowercase hex characters")
    return value


def _sequence(request: Request) -> int:
    value = request.path_params.get("seq", "")
    if not isinstance(value, str) or not _SEQUENCE.fullmatch(value):
        raise HttpError(400, "bad_request", "chunk sequence must be a decimal integer")
    sequence = int(value)
    if sequence > MAX_SEQUENCE:
        raise HttpError(400, "bad_request", "chunk sequence is out of range")
    return sequence


def _header(request: Request, name: str, *, required: bool = True) -> str | None:
    value = request.headers.get(name)
    if value is None or value == "":
        if required:
            raise HttpError(400, "bad_request", f"{name} is required")
        return None
    return value


def _lease(request: Request) -> str:
    value = _header(request, "x-recorder-lease", required=False)
    if value is None or len(value) > MAX_LEASE_HEADER:
        raise HttpError(403, "invalid_lease", "the capture lease is missing or invalid")
    return value


def _recovery(request: Request) -> str:
    value = _header(request, "x-recorder-recovery", required=False)
    if value is None or len(value) > MAX_RECOVERY_HEADER:
        raise HttpError(403, "invalid_lease", "the recovery token is missing or invalid")
    return value


def _create_recovery(request: Request) -> str:
    """The client-owned recovery capability required by create; never echoed or logged."""
    value = request.headers.get("x-recorder-recovery")
    if (value is None or len(value) > MAX_RECOVERY_HEADER
            or len(value) < MIN_RECOVERY_LENGTH
            or any(not 0x21 <= ord(character) <= 0x7E for character in value)):
        raise HttpError(400, "recovery_required",
                        f"X-Recorder-Recovery must be {MIN_RECOVERY_LENGTH}-{MAX_RECOVERY_HEADER} "
                        "printable ASCII characters")
    return value


def _stream_header(request: Request) -> str:
    value = _header(request, "x-recorder-stream") or ""
    if len(value) > MAX_STREAM_HEADER:
        raise HttpError(400, "bad_request", "X-Recorder-Stream is too long")
    return value


def _client_header(request: Request) -> str:
    value = _header(request, "x-recorder-client") or ""
    if len(value) > MAX_CLIENT_HEADER:
        raise HttpError(400, "bad_request", "X-Recorder-Client is too long")
    return value


def _text_field(data: dict[str, Any], name: str, *, required: bool = True,
                limit: int = 128) -> str | None:
    value = data.get(name)
    if value is None or value == "":
        if required:
            raise HttpError(400, "bad_request", f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise HttpError(400, "bad_request", f"{name} must be a string of at most {limit} characters")
    return value


def _int_field(data: dict[str, Any], name: str) -> int | None:
    value = data.get(name)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise HttpError(400, "bad_request", f"{name} must be a non-negative integer")
    return value


def _sample_start(request: Request) -> int:
    raw = _header(request, "x-sample-start")
    if raw is None or not raw.isdigit():
        raise HttpError(400, "bad_request", "X-Sample-Start must be a non-negative integer")
    return int(raw)


def _sha(request: Request) -> str:
    value = _header(request, "x-content-sha256") or ""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise HttpError(400, "bad_request", "X-Content-SHA256 must be 64 hex characters")
    return value


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if key is None or key == "":
        raise HttpError(400, "idempotency_key_required", "Idempotency-Key is required to start a recording")
    if not _IDEMPOTENCY_KEY.fullmatch(key):
        raise HttpError(400, "bad_request",
                        "Idempotency-Key must be 1-128 characters from A-Z, a-z, 0-9, '.', '_', ':' or '-'")
    return key


def _query_int(request: Request, name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    negative = minimum < 0 and raw.startswith("-") and raw[1:].isdigit()
    if not (raw.isdigit() or negative):
        raise HttpError(400, "bad_request", f"{name} must be an integer between {minimum} and {maximum}")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise HttpError(400, "bad_request", f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _scheduler(host: RecorderHost) -> Any:
    return getattr(host.service, "scheduler", None)


def _transcript_snapshot(host: RecorderHost, recording_id: str) -> dict[str, Any] | None:
    scheduler = _scheduler(host)
    snapshot = getattr(scheduler, "snapshot", None)
    if not callable(snapshot):
        return None
    try:
        value = snapshot(recording_id)
    except Exception:  # noqa: BLE001 - a diagnostic snapshot must not break a transcript read
        log.warning("recorder transcript snapshot failed")
        return None
    return value if isinstance(value, dict) else None


def _live_provisional(host: RecorderHost, recording_id: str, after: int = -1) -> dict[str, Any]:
    """At most one live-only provisional tail after ``after`` from the bounded in-memory transcript."""
    scheduler = _scheduler(host)
    view = getattr(scheduler, "transcript", None)
    transcript = view(recording_id) if callable(view) else None
    if transcript is None:
        return {"provisional": None}
    try:
        documents = transcript.documents()
    except Exception:  # noqa: BLE001 - the durable page is still useful if the live view races a close
        return {"provisional": None}
    provisional = [dict(document) for document in documents
                   if document.get("status") == "provisional"
                   and int(document.get("segment_id", -1)) > after]
    return {"provisional": provisional[-1] if provisional else None}


def _read_transcript_page(store: Any, recording_id: str, after: int, limit: int) -> dict[str, Any]:
    """Worker-side bounded durable page read; the provider observes deletion and never recreates a parent."""
    log = TranscriptLog(lambda: store.transcript_dir(recording_id))
    page = log.read_page(after, limit)
    return {"segments": [dict(document) for document in page.segments],
            "next_after": page.next_after}


async def _read_transcript_page_retrying(host: RecorderHost, store: Any, recording_id: str,
                                         after: int, limit: int) -> dict[str, Any]:
    """Read one export page, waiting out bounded host-admission backpressure.

    A later page is read after HTTP 200, so a transient `recorder_backpressure` must not truncate the
    export. The wait is bounded and uses asyncio sleep (never a busy spin); cancellation from a client
    disconnect still unwinds the export generator and releases its pin.
    """
    deadline = time.monotonic() + EXPORT_PAGE_RETRY_SECONDS
    delay = EXPORT_PAGE_RETRY_MIN_SLEEP
    while True:
        try:
            return await host.call(_read_transcript_page, store, recording_id, after, limit)
        except RecorderUnavailable as exc:
            if getattr(exc, "code", "recorder_unavailable") not in _RETRYABLE_EXPORT_CODES:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            retry_after = getattr(exc, "retry_after", None)
            wait = max(delay, retry_after if isinstance(retry_after, int) else 0)
            await asyncio.sleep(min(wait, remaining, EXPORT_PAGE_RETRY_MAX_SLEEP))
            delay = min(delay * 2, EXPORT_PAGE_RETRY_MAX_SLEEP)


def _transcript_log_status(store: Any, recording_id: str, active: bool) -> dict[str, Any]:
    """Fallback status from the durable log when the scheduler has no loaded job (for example during restart)."""
    log = TranscriptLog(lambda: store.transcript_dir(recording_id))
    _transcript, cursors = log.replay()
    ended = cursors.segmentation.ended_at
    pending = int(cursors.counts.pending)
    errors = int(cursors.counts.error)
    if ended is None:
        state = "preparing" if active else "catching_up"
    elif pending:
        state = "catching_up"
    elif errors:
        state = "failed"
    else:
        state = "complete"
    return {"state": state, "transcript_revision": int(cursors.transcript_revision),
            "complete": state == "complete"}


async def _transcript_status(host: RecorderHost, recording_id: str, state: dict[str, Any]) -> dict[str, Any]:
    snapshot = _transcript_snapshot(host, recording_id)
    if snapshot is not None:
        value = snapshot.get("state")
        state_name = value if isinstance(value, str) and value else str(
            state.get("transcription_state") or "unavailable")
        revision = snapshot.get("transcript_revision")
        return {
            "state": state_name,
            "transcript_revision": int(revision) if type(revision) is int else 0,
            "complete": state_name == "complete",
        }
    return await host.call(_transcript_log_status, host.service.store, recording_id,
                           bool(state.get("active")))


def _scope(value: Any) -> str | list[int]:
    if value is None:
        return "failed"
    if isinstance(value, str):
        if value not in {"failed", "all"}:
            raise HttpError(400, "bad_request", "scope must be 'failed', 'all' or a list of segment ids")
        return value
    if isinstance(value, list):
        if len(value) > MAX_RETRY_SCOPE:
            raise HttpError(400, "bad_request", f"scope may name at most {MAX_RETRY_SCOPE} segments")
        if any(type(item) is not int or not 0 <= item <= MAX_SEGMENT_ID for item in value):
            raise HttpError(400, "bad_request", "scope segment ids must be non-negative integers")
        return value
    raise HttpError(400, "bad_request", "scope must be 'failed', 'all' or a list of segment ids")


async def _optional_json(request: Request) -> dict[str, Any]:
    if not request.headers.get("content-type") and request.headers.get("content-length") in (None, "0"):
        return {}
    return await _bounded_json(request)


def _next_block(iterator: Iterator[bytes]) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return _DONE


async def _wav_body(iterator: Iterator[bytes], first: bytes,
                    release: Callable[[], None]) -> AsyncIterator[bytes]:
    """Yield the primed header, then stream verified chunks off the anyio threadpool."""
    try:
        yield first
        while True:
            pending = asyncio.create_task(run_in_threadpool(_next_block, iterator))
            try:
                chunk = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # Do not close a generator that is still reading a chunk.
                with CancelScope(shield=True), suppress(Exception):
                    await asyncio.shield(pending)
                raise
            if chunk is _DONE:
                break
            yield chunk
    finally:
        iterator.close()
        release()


def routes(host: RecorderHost, wrap: Callable[[Callable[..., Any]], Callable[..., Any]]) -> list[Route]:
    """The recorder HTTP surface, wrapped by the daemon's auth/idempotency/error middleware."""
    inflight = _Inflight(RECORDER_MAX_INFLIGHT_BODIES)
    exports = _ExportPins(MAX_TRANSCRIPT_EXPORTS)
    checked = _checked

    async def recordings(request: Request) -> Response:
        if request.method == "GET":
            raw = request.query_params.get("limit")
            if raw is None:
                limit = MAX_LIST_LIMIT
            elif not raw.isdigit() or not 1 <= int(raw) <= MAX_LIST_LIMIT:
                raise HttpError(400, "bad_request", f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
            else:
                limit = int(raw)
            items = await host.call(host.service.list, limit=limit)
            return _json({"recordings": items})

        data = await _bounded_json(request)
        mode = data.get("mode")
        if mode not in (None, "create", "resume"):
            raise HttpError(400, "bad_request", "mode must be 'create' or 'resume'")
        client_id = _text_field(data, "client_id")
        title = data.get("title")
        if title is not None and not isinstance(title, str):
            raise HttpError(400, "bad_request", "title must be a string or null")
        key = _idempotency_key(request)
        recovery = _create_recovery(request)
        if mode == "resume":
            # Never creates: an unknown key is withdrawn for this Core process so its in-flight create
            # cannot later produce a second recording.
            resumed = await host.call(host.service.resume, client_id, key, recovery, title=title)
            return _json(resumed, status=200)
        stream_id = _text_field(data, "stream_id")
        created = await host.call(host.service.create, stream_id, client_id, key, title=title,
                                  recovery=recovery)
        status = 200 if created.get("idempotent") else 201
        return _json(created, status=status)

    async def recording(request: Request) -> Response:
        recording_id = _recording_id(request)
        if request.method == "DELETE":
            if exports.active(recording_id):
                raise HttpError(409, "recording_export_active",
                                "a transcript export is still reading this recording; retry deletion")
            deleted = await host.call(host.service.delete, recording_id)
            if not deleted:
                raise HttpError(404, "recording_not_found", "no such recording")
            return Response(status_code=204, headers=_cache_headers())
        state = await host.call(host.service.get, recording_id)
        return _json(state)

    async def chunk(request: Request) -> Response:
        recording_id = _recording_id(request)
        sequence = _sequence(request)
        lease = _lease(request)
        stream_id = _stream_header(request)
        start = _sample_start(request)
        sha256 = _sha(request)
        data = await _bounded_chunk(request, inflight)
        try:
            result = await host.call(host.service.put_chunk, recording_id, sequence, start, data, sha256,
                                     lease, stream_id=stream_id)
        finally:
            inflight.release()
        return _json(result)

    async def heartbeat(request: Request) -> Response:
        recording_id = _recording_id(request)
        lease = _lease(request)
        data = await _bounded_json(request)
        stream_id = _text_field(data, "stream_id")
        state = await host.call(host.service.heartbeat, recording_id, lease, stream_id=stream_id)
        return _json(state)

    async def rebind(request: Request) -> Response:
        recording_id = _recording_id(request)
        lease = _lease(request)
        data = await _bounded_json(request)
        stream_id = _text_field(data, "stream_id") or ""
        client_id = _text_field(data, "client_id") or ""
        state = await host.call(host.service.rebind, recording_id, lease, stream_id, client_id)
        return _json(state)

    async def stop(request: Request) -> Response:
        recording_id = _recording_id(request)
        lease = _lease(request)
        data = await _bounded_json(request)
        stream_id = _text_field(data, "stream_id", required=False)
        final = _int_field(data, "final_sample_count")
        state = await host.call(host.service.stop, recording_id, lease, final, stream_id=stream_id)
        return _json(state)

    async def recover(request: Request) -> Response:
        recording_id = _recording_id(request)
        token = _recovery(request)
        data = await _bounded_json(request)
        client_id = _text_field(data, "client_id") or ""
        final = _int_field(data, "final_sample_count")
        stream_id = _text_field(data, "stream_id", required=False)
        state = await host.call(host.service.recover_stop, recording_id, token, client_id, final,
                                stream_id=stream_id)
        return _json(state)

    async def recover_chunk(request: Request) -> Response:
        recording_id = _recording_id(request)
        sequence = _sequence(request)
        token = _recovery(request)
        client_id = _client_header(request)
        stream_id = _header(request, "x-recorder-stream", required=False)
        if stream_id is not None and len(stream_id) > MAX_STREAM_HEADER:
            raise HttpError(400, "bad_request", "X-Recorder-Stream is too long")
        start = _sample_start(request)
        sha256 = _sha(request)
        data = await _bounded_chunk(request, inflight)
        try:
            result = await host.call(host.service.recover_upload, recording_id, sequence, start, data,
                                     sha256, token, client_id, stream_id=stream_id)
        finally:
            inflight.release()
        return _json(result)

    async def audio(request: Request) -> Response:
        recording_id = _recording_id(request)
        if not exports.acquire(recording_id):
            raise HttpError(503, "recording_export_busy",
                            "too many recorder exports are reading this recording; retry shortly",
                            headers={"retry-after": "1"})
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            exports.release(recording_id)

        iterator: Iterator[bytes] | None = None
        try:
            state = await host.call(host.service.get, recording_id)
            if state.get("active"):
                # A snapshot of a live capture would be labelled silence after the fact; B3 only exports stopped audio.
                raise HttpError(409, "recording_active", "stop the recording before downloading its audio")
            if not state.get("sealed"):
                # An interrupted/unsealed capture is inactive but not stopped; recovery must seal its final offset first.
                raise HttpError(409, "recording_not_stopped",
                                "the recording is not sealed; recover its final offset before downloading audio")
            if state.get("problems"):
                raise HttpError(409, "recording_corrupt",
                                "the recording has chunks that do not match their checksums")
            iterator = host.service.store.stream_wav(recording_id)
            header = await run_in_threadpool(_next_block, iterator)
            if header is _DONE or len(header) < 44:
                raise HttpError(500, "recording_error", "the recording has no WAV header")
            data_size = int.from_bytes(header[40:44], "little")
            if data_size % 2:
                raise HttpError(409, "recording_corrupt", "the recording's WAV header is invalid")
            expected = 2 * int(state.get("durable_offset", 0))
            if data_size < expected:
                # The size-only status read accepted the durable range, but the hash-verified export prefix is
                # shorter: a committed chunk no longer matches its recorded digest. Do not send a short 200.
                raise HttpError(409, "recording_corrupt",
                                "the recording's committed audio does not match its checksums")
        except BaseException:
            if iterator is not None:
                iterator.close()
            release()
            raise
        samples = data_size // 2
        headers = {
            **_cache_headers(),
            "content-encoding": "identity",  # never gzip 115 MB of PCM on the server
            "content-disposition": f'attachment; filename="recording-{recording_id[:8]}.wav"',
            "x-content-type-options": "nosniff",
            "x-recording-complete": "1" if state.get("complete") else "0",
            "x-recording-holes": str(len(state.get("holes") or [])),
            "x-recording-samples": str(samples),
            "content-length": str(44 + data_size),
        }
        return _PinnedExportResponse(
            _wav_body(iterator, header, release), media_type="audio/wav", headers=headers, release=release)

    async def transcript(request: Request) -> Response:
        recording_id = _recording_id(request)
        after = _query_int(request, "after", default=-1, minimum=-1, maximum=MAX_SEGMENT_ID)
        limit = _query_int(request, "limit", default=DEFAULT_PAGE_LIMIT, minimum=1,
                           maximum=MAX_PAGE_LIMIT)
        state = await host.call(host.service.get, recording_id)
        page = await host.call(_read_transcript_page, host.service.store, recording_id, after, limit)
        status = await _transcript_status(host, recording_id, state)
        return _json({
            "segments": page["segments"],
            "next_after": page["next_after"],
            "live": _live_provisional(host, recording_id, after),
            "transcript_revision": status["transcript_revision"],
            "state": status["state"],
            "complete": bool(status["complete"]) and not bool(state.get("active")),
        })

    async def _pages(recording_id: str, first: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        page = first
        previous: int | None = None
        while True:
            yield page
            after = page.get("next_after")
            if type(after) is not int or after <= (previous if previous is not None else -1):
                return
            previous = after
            page = await _read_transcript_page_retrying(
                host, host.service.store, recording_id, after, TRANSCRIPT_EXPORT_PAGE)
            if not page["segments"]:
                return

    async def _txt_body(recording_id: str, first: dict[str, Any],
                        release: Callable[[], None]) -> AsyncIterator[str]:
        try:
            first_text = True
            async for page in _pages(recording_id, first):
                for document in page["segments"]:
                    if document.get("status") == "skipped":
                        continue
                    text = str(document.get("text") or "")
                    if not text:
                        continue
                    if not first_text:
                        yield " "
                    first_text = False
                    yield text
        finally:
            release()

    async def _json_body(recording_id: str, first: dict[str, Any], complete: bool,
                         release: Callable[[], None]) -> AsyncIterator[str]:
        try:
            yield '{"segments":['
            emitted = False
            async for page in _pages(recording_id, first):
                for document in page["segments"]:
                    if emitted:
                        yield ","
                    emitted = True
                    yield json.dumps(document, ensure_ascii=False, sort_keys=True)
            yield '],"complete":'
            yield "true" if complete else "false"
            yield "}"
        finally:
            release()

    async def transcript_export(request: Request) -> Response:
        recording_id = _recording_id(request)
        if not exports.acquire(recording_id):
            raise HttpError(503, "transcript_export_busy",
                            "too many transcript exports are reading this recording; retry shortly",
                            headers={"retry-after": "1"})
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            exports.release(recording_id)

        try:
            # Exports are allowed during capture (unlike WAV); the complete marker tells the client it may grow.
            # The pin keeps DELETE from removing the log between page reads; the response releases it on
            # completion, error or disconnect.
            state = await host.call(host.service.get, recording_id)
            status = await _transcript_status(host, recording_id, state)
            complete = bool(status["complete"]) and not bool(state.get("active"))
            first = await _read_transcript_page_retrying(
                host, host.service.store, recording_id, -1, TRANSCRIPT_EXPORT_PAGE)
        except BaseException:
            release()
            raise
        as_json = request.url.path.endswith(".json")
        body = (_json_body(recording_id, first, complete, release) if as_json
                else _txt_body(recording_id, first, release))
        suffix = "json" if as_json else "txt"
        headers = {
            **_cache_headers(),
            "x-content-type-options": "nosniff",
            "x-recorder-transcript-complete": "true" if complete else "false",
            "content-disposition": f'attachment; filename="recording-{recording_id[:8]}.{suffix}"',
        }
        return _PinnedExportResponse(
            body,
            media_type="application/json" if as_json else "text/plain; charset=utf-8",
            headers=headers,
            release=release,
        )

    async def transcript_retry(request: Request) -> Response:
        recording_id = _recording_id(request)
        await host.call(host.service.get, recording_id)
        data = await _optional_json(request)
        scope = _scope(data.get("scope"))
        scheduler = _scheduler(host)
        retry = getattr(scheduler, "retry", None)
        if not callable(retry):
            raise HttpError(503, "transcription_unavailable", "the transcription scheduler is unavailable")
        try:
            result = await retry(recording_id, scope)
        except SchedulerError as exc:
            raise HttpError(409, exc.code, str(exc)) from exc
        return _json(result, status=202)

    return [
        Route("/api/recordings", wrap(checked(recordings)), methods=["GET", "POST"]),
        Route("/api/recordings/{id}", wrap(checked(recording)), methods=["GET", "DELETE"]),
        Route("/api/recordings/{id}/chunks/{seq}", wrap(checked(chunk)), methods=["PUT"]),
        Route("/api/recordings/{id}/heartbeat", wrap(checked(heartbeat)), methods=["POST"]),
        Route("/api/recordings/{id}/rebind", wrap(checked(rebind)), methods=["POST"]),
        Route("/api/recordings/{id}/stop", wrap(checked(stop)), methods=["POST"]),
        Route("/api/recordings/{id}/recover", wrap(checked(recover)), methods=["POST"]),
        Route("/api/recordings/{id}/recover/chunks/{seq}", wrap(checked(recover_chunk)), methods=["PUT"]),
        Route("/api/recordings/{id}/audio.wav", wrap(checked(audio)), methods=["GET"]),
        Route("/api/recordings/{id}/transcript", wrap(checked(transcript)), methods=["GET"]),
        Route("/api/recordings/{id}/transcript.txt", wrap(checked(transcript_export)), methods=["GET"]),
        Route("/api/recordings/{id}/transcript.json", wrap(checked(transcript_export)), methods=["GET"]),
        Route("/api/recordings/{id}/transcript/retry", wrap(checked(transcript_retry)), methods=["POST"]),
    ]
