"""Minimal ACP v1 stdio JSON-RPC transport for the Antigravity external worker.

This module is transport only. It owns one child process, frames NDJSON, correlates
requests with responses, forwards notifications and answers client-side requests such
as permission prompts. It does not know about Nexus sessions, models, tools or
credentials, and nothing in the runtime imports it yet.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from .base import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_CHARS,
    WorkerError,
    bounded_lines,
    child_env,
    clip,
    diagnostic_text,
    protected_cleanup,
    stop_process,
    wait_for_exit,
)

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_QUEUED_NOTIFICATIONS = 1024
MAX_INBOUND_REQUESTS = 64
EXIT_GRACE_SECONDS = 10.0
EOF_GRACE_SECONDS = 2.0
STDERR_GRACE_SECONDS = .2
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_CANCELLED = -32800

JsonParams = dict[str, Any] | list[Any] | None
MessageCallback = Callable[[bytes], Awaitable[None]]
NotificationCallback = Callable[[str, JsonParams], Awaitable[None]]
RequestHandler = Callable[[JsonParams], Awaitable[Any]]
PermissionHandler = Callable[[Mapping[str, Any]], Awaitable[str | dict[str, Any] | None]]


class AcpError(Exception):
    """Base for ACP transport, protocol and remote failures."""


class AcpProcessError(AcpError):
    """The agent process could not be started, closed its pipes or exited."""


class AcpProtocolError(AcpError):
    """The agent violated the expected JSON-RPC/ACP framing."""


class AcpTimeoutError(AcpError, TimeoutError):
    """A request produced no response before its caller-supplied timeout."""


class AcpResponseError(AcpError):
    """A JSON-RPC error response, with the remote fields kept for callers."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        self.method = method
        self.code = code
        self.message = _safe_text(message, 500)
        self.data = data
        super().__init__(f"ACP {_safe_text(method, 100)} failed ({code}): {self.message}")


def _safe_text(value: object, limit: int = 500) -> str:
    text = diagnostic_text(str(value).encode("utf-8", "replace"))
    return clip(text, limit)[0]


def _valid_method(method: object) -> bool:
    return isinstance(method, str) and bool(method) and "\x00" not in method


def _valid_params(params: object) -> bool:
    return params is None or isinstance(params, (dict, list))


def _request_id(value: object) -> int | str:
    if type(value) is int or (isinstance(value, str) and value):
        return value
    raise AcpProtocolError("ACP request id must be an integer or a non-empty string")


def permission_outcome(option_id: str | None) -> dict[str, Any]:
    """Build a session/request_permission result; None asks for the cancelled outcome."""
    if option_id is None:
        return {"outcome": {"outcome": "cancelled"}}
    if not isinstance(option_id, str) or not option_id:
        raise ValueError("permission option id must be a non-empty string or None")
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


@dataclass(frozen=True, slots=True)
class _Pending:
    method: str
    future: asyncio.Future[Any]


class AcpClient:
    """One ACP agent process speaking JSON-RPC 2.0 over stdin/stdout.

    Start it with `start()` or `async with`. Every inbound line reaches `on_message`
    exactly as received, before parsing, so a session can record wire events; that
    callback is awaited inline and must not wait on a client request. Inbound
    notifications run in an ordered, separate task, and inbound requests (permission,
    file and terminal calls) run in their own tasks. Both may therefore issue client
    requests back to the agent without deadlocking the reader. A permission callback may
    stay pending while notifications and the prompt response continue to arrive.
    POSIX process groups are required.
    """

    def __init__(
        self,
        executable: str,
        args: Sequence[str] = (),
        *,
        cwd: str | Path,
        environ: Mapping[str, str] | None = None,
        on_message: MessageCallback | None = None,
        on_notification: NotificationCallback | None = None,
        on_permission: PermissionHandler | None = None,
        request_handlers: Mapping[str, RequestHandler] | None = None,
        message_limit: int = MAX_MESSAGE_BYTES,
        stream_limit: int = MAX_STREAM_BYTES,
        max_queued_notifications: int = MAX_QUEUED_NOTIFICATIONS,
        max_inbound_requests: int = MAX_INBOUND_REQUESTS,
    ) -> None:
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise ValueError("ACP executable must be a non-empty path or name")
        if isinstance(args, str):
            raise TypeError("ACP args must be a sequence of strings, not a string")
        self._args = tuple(args)
        if any(not isinstance(arg, str) or "\x00" in arg for arg in self._args):
            raise ValueError("ACP arguments must be strings without NUL")
        try:
            self._cwd = Path(cwd).resolve(strict=True)
        except OSError:
            raise ValueError("ACP cwd must be an existing directory") from None
        if not self._cwd.is_dir():
            raise ValueError("ACP cwd must be a directory")
        for name, value in (("message_limit", message_limit), ("stream_limit", stream_limit),
                            ("max_queued_notifications", max_queued_notifications),
                            ("max_inbound_requests", max_inbound_requests)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"ACP {name} must be a positive integer")
        if on_message is not None and not callable(on_message):
            raise ValueError("ACP on_message must be callable")
        if on_notification is not None and not callable(on_notification):
            raise ValueError("ACP on_notification must be callable")
        if on_permission is not None and not callable(on_permission):
            raise ValueError("ACP on_permission must be callable")

        self._executable = executable
        self._environ = None if environ is None else dict(environ)
        self._on_message = on_message
        self._on_notification = on_notification
        self._on_permission = on_permission
        self._message_limit = message_limit
        self._stream_limit = stream_limit
        self._max_queued_notifications = max_queued_notifications
        self._max_inbound_requests = max_inbound_requests

        self._handlers = dict(request_handlers or {})
        for method, handler in self._handlers.items():
            if not _valid_method(method) or not callable(handler):
                raise ValueError("ACP request_handlers must map method names to callables")
        self._handlers.setdefault("session/request_permission", self._permission_request)

        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._leader_task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[int | str, _Pending] = {}
        self._write_lock = asyncio.Lock()
        self._notifications: asyncio.Queue[tuple[str, JsonParams]] = asyncio.Queue()
        self._notification_drained = asyncio.Event()
        self._notification_drained.set()
        self._ids = iter(range(1, 2 ** 31))
        self._stderr = bytearray()
        self._notes: list[str] = []
        self._failure: AcpError | None = None
        self._started = False
        self._closed = False

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    @property
    def diagnostics(self) -> str:
        """Sanitized, clipped stderr tail; credentials in labelled fields are redacted."""
        return clip(diagnostic_text(bytes(self._stderr)), MAX_DIAGNOSTIC_CHARS)[0]

    @property
    def pending_inbound(self) -> int:
        """Inbound requests (permission, fs, terminal) that have not been answered yet."""
        return sum(1 for task in self._inbound_tasks if not task.done())

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(self._notes)

    @property
    def failure(self) -> AcpError | None:
        return self._failure

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Spawn the agent in its own process group and begin reading its streams."""
        if self._started:
            raise RuntimeError("ACP client has already been started")
        if self._closed:
            raise AcpProcessError("ACP client is closed")
        if os.name != "posix":
            raise AcpProcessError("ACP stdio transport requires POSIX process groups")
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable, *self._args, cwd=self._cwd, env=child_env(self._environ),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except (OSError, ValueError) as exc:
            detail = getattr(exc, "strerror", None) or type(exc).__name__
            raise AcpProcessError(f"could not start ACP agent {self._executable!r}: {detail}") from None
        self._process = process
        self._started = True
        try:
            self._read_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            self._notification_task = asyncio.create_task(self._notification_loop())
            self._leader_task = asyncio.create_task(self._wait_leader())
        except BaseException:
            self._started = False
            await protected_cleanup(stop_process(process))
            raise

    async def initialize(
        self,
        *,
        client_capabilities: Mapping[str, Any] | None = None,
        client_info: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send ACP initialize with protocol version 1 and require that version back."""
        params: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": dict(client_capabilities or {}),
        }
        if client_info is not None:
            params["clientInfo"] = dict(client_info)
        result = await self.request("initialize", params, timeout=timeout)
        if (not isinstance(result, dict) or type(result.get("protocolVersion")) is not int
                or result["protocolVersion"] != PROTOCOL_VERSION):
            error = AcpProtocolError(f"ACP agent did not negotiate protocol version {PROTOCOL_VERSION}")
            self._fail(error)
            raise error
        return result

    async def request(self, method: str, params: JsonParams = None, *, timeout: float | None = None) -> Any:
        """Send a request and return its result, or raise the correlated failure."""
        self._check_method(method)
        self._check_params(params)
        if timeout is not None and timeout < 0:
            raise ValueError("ACP request timeout cannot be negative")
        self._check_running()
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _Pending(method, future)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write(message)
        except BaseException:
            self._pending.pop(request_id, None)
            if future.done():
                with suppress(Exception):
                    future.exception()
            else:
                future.cancel()
            raise
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            if self._pending.pop(request_id, None) is not None:
                await self._best_effort_cancel(request_id)
            raise AcpTimeoutError(f"ACP request {_safe_text(method, 100)!r} timed out after {timeout:g}s") from None
        except asyncio.CancelledError:
            if self._pending.pop(request_id, None) is not None:
                await self._best_effort_cancel(request_id)
            raise

    async def notify(self, method: str, params: JsonParams = None) -> None:
        """Send a JSON-RPC notification."""
        self._check_method(method)
        self._check_params(params)
        await self._write_notification(method, params)

    async def flush_notifications(self) -> None:
        """Wait until every notification delivered so far has reached its callback."""
        await self._notification_drained.wait()

    async def close(self) -> None:
        """Stop the owned process group and tasks. Safe to call more than once."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await protected_cleanup(self._close_task)

    def _check_method(self, method: object) -> None:
        if not _valid_method(method):
            raise ValueError("ACP method must be a non-empty string without NUL")

    def _check_params(self, params: object) -> None:
        if not _valid_params(params):
            raise ValueError("ACP params must be an object, an array or None")

    def _check_running(self) -> None:
        if not self._started or self._closed or self._process is None:
            raise self._failure or AcpProcessError("ACP client is not running")
        if self._failure is not None:
            raise self._failure
        if self._process.returncode is not None:
            error = AcpProcessError(f"ACP agent exited with code {self._process.returncode}")
            self._fail(error)
            raise error

    def _note(self, text: str) -> None:
        if text not in self._notes:
            self._notes.append(text)

    async def _write_notification(self, method: str, params: JsonParams) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def _best_effort_cancel(self, request_id: int) -> None:
        with suppress(AcpError, OSError, RuntimeError):
            await self._write_notification("$/cancel_request", {"requestId": request_id})

    async def _write(self, message: dict[str, Any]) -> None:
        self._check_running()
        try:
            payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError(f"ACP message is not valid JSON ({type(exc).__name__})") from None
        if len(payload) > self._message_limit:
            raise ValueError(f"ACP message exceeds {self._message_limit} bytes; refusing to truncate")
        process = self._process
        assert process is not None and process.stdin is not None
        async with self._write_lock:
            self._check_running()
            if process.returncode is not None:
                error = AcpProcessError(f"ACP agent exited with code {process.returncode}")
                self._fail(error)
                raise error
            try:
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()
            except (OSError, RuntimeError, ValueError) as exc:
                error = AcpProcessError(f"ACP agent closed stdin ({type(exc).__name__})")
                self._fail(error)
                raise error from None

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            async for raw in bounded_lines(process.stdout, line_limit=self._message_limit, total_limit=self._stream_limit):
                if self._on_message is not None:
                    try:
                        await self._on_message(raw)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - a user callback boundary; never let it corrupt the stream
                        raise AcpError(f"ACP raw message callback failed ({type(exc).__name__})") from None
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError):
                    raise AcpProtocolError("ACP agent sent malformed JSON; the stream was not interpreted") from None
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except WorkerError as exc:
            self._fail(AcpProtocolError(str(exc)))
        except AcpError as exc:
            self._fail(exc)
        except Exception as exc:  # noqa: BLE001 - every unexpected reader failure must close pending callers
            self._fail(AcpProtocolError(f"ACP reader failed ({type(exc).__name__})"))
        finally:
            if not self._closed and self._failure is None:
                code = process.returncode
                detail = f" (exit code {code})" if code is not None else ""
                self._fail(AcpProcessError(f"ACP agent closed its output{detail}"))

    async def _dispatch(self, message: object) -> None:
        if not isinstance(message, dict):
            raise AcpProtocolError("ACP message was not a JSON object")
        if message.get("jsonrpc") != "2.0":
            raise AcpProtocolError("ACP message did not declare JSON-RPC 2.0")
        if "method" in message:
            method = message.get("method")
            if not _valid_method(method):
                raise AcpProtocolError("ACP message had an invalid method")
            if "id" not in message:
                params = message.get("params")
                if method == "$/cancel_request":
                    self._handle_cancel_request(params)
                    return
                if not _valid_params(params):
                    raise AcpProtocolError("ACP notification had invalid params")
                self._enqueue_notification(method, params)
                return
            if "result" in message or "error" in message:
                raise AcpProtocolError("ACP request also contained a response field")
            params = message.get("params")
            if not _valid_params(params):
                raise AcpProtocolError("ACP request had invalid params")
            await self._dispatch_request(method, _request_id(message["id"]), params)
            return
        if "id" not in message:
            raise AcpProtocolError("ACP message was neither a request, response nor notification")
        request_id = _request_id(message["id"])
        has_result, has_error = "result" in message, "error" in message
        if has_result == has_error:
            raise AcpProtocolError("ACP response must contain exactly one of result or error")
        pending = self._pending.pop(request_id, None)
        if pending is None:
            self._note(f"ignored a response for unknown request id {_safe_text(request_id, 100)!r}")
            return
        if pending.future.done():
            return
        if has_error:
            pending.future.set_exception(self._response_error(pending.method, message["error"]))
        else:
            pending.future.set_result(message["result"])

    def _response_error(self, method: str, error: object) -> AcpResponseError:
        if not isinstance(error, dict):
            raise AcpProtocolError("ACP error response was not an object")
        code, message = error.get("code"), error.get("message")
        if type(code) is not int or not isinstance(message, str):
            raise AcpProtocolError("ACP error response lacked an integer code or string message")
        return AcpResponseError(method, code, message, error.get("data"))

    def _enqueue_notification(self, method: str, params: JsonParams) -> None:
        if self._notifications.qsize() >= self._max_queued_notifications:
            self._fail(AcpProtocolError("ACP notification backlog limit reached; later updates were not retained"))
            return
        self._notification_drained.clear()
        self._notifications.put_nowait((method, params))

    async def _dispatch_request(self, method: str, request_id: int | str, params: JsonParams) -> None:
        handler = self._handlers.get(method)
        if handler is None:
            self._note(f"unsupported client request {_safe_text(method, 100)!r} was answered with method not found")
            self._spawn_inbound(self._answer_error(request_id, METHOD_NOT_FOUND, f"method {method} is not supported"))
            return
        if len(self._inbound_tasks) >= self._max_inbound_requests:
            self._spawn_inbound(self._answer_error(request_id, INTERNAL_ERROR, "client inbound request limit reached"))
            return
        self._spawn_inbound(self._answer_request(method, request_id, params, handler))

    def _spawn_inbound(self, action: Awaitable[None]) -> None:
        task = asyncio.create_task(action)
        self._inbound_tasks.add(task)
        task.add_done_callback(self._inbound_tasks.discard)

    async def _permission_request(self, params: JsonParams) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("session/request_permission params must be an object")  # noqa: TRY004 - mapped to JSON-RPC invalid params
        if self._on_permission is None:
            self._note("session/request_permission was answered cancelled because no permission callback is configured")
            return permission_outcome(None)
        choice = await self._on_permission(params)
        if choice is None or isinstance(choice, str):
            return permission_outcome(choice)
        if isinstance(choice, dict) and isinstance(choice.get("outcome"), dict):
            return choice
        raise ValueError("permission callback must return an option id, an outcome object or None")

    async def _answer_request(self, method: str, request_id: int | str, params: JsonParams,
                              handler: RequestHandler) -> None:
        try:
            result = await handler(params)
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except AcpError:
            return
        except ValueError as exc:
            self._note(f"client handler for {_safe_text(method, 100)!r} rejected its input")
            await self._answer_error(request_id, INVALID_PARAMS, f"invalid params for {method}: {exc}")
        except Exception as exc:  # noqa: BLE001 - answer a broken client handler instead of abandoning the agent's request
            self._note(f"client handler for {_safe_text(method, 100)!r} failed ({type(exc).__name__})")
            await self._answer_error(request_id, INTERNAL_ERROR, f"client handler for {method} failed ({type(exc).__name__})")

    async def _answer_error(self, request_id: int | str, code: int, message: str) -> None:
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": code, "message": _safe_text(message, 300)}})
        except (AcpError, ValueError):
            return

    def _handle_cancel_request(self, params: object) -> None:
        if not isinstance(params, dict):
            return
        value = params.get("requestId")
        if not (type(value) is int or (isinstance(value, str) and value)):
            return
        pending = self._pending.pop(value, None)
        if pending is not None and not pending.future.done():
            pending.future.set_exception(AcpResponseError(pending.method, REQUEST_CANCELLED, "Request cancelled"))

    async def _notification_loop(self) -> None:
        try:
            while True:
                method, params = await self._notifications.get()
                try:
                    if self._on_notification is not None:
                        await self._on_notification(method, params)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a callback failure fails the client visibly, never silently
                    self._fail(AcpError(f"ACP notification callback failed ({type(exc).__name__})"))
                    return
                finally:
                    self._notifications.task_done()
                    if self._notifications.empty():
                        self._notification_drained.set()
        finally:
            self._notification_drained.set()

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while chunk := await process.stderr.read(4096):
                self._stderr.extend(chunk)
                if len(self._stderr) > MAX_DIAGNOSTIC_BYTES:
                    del self._stderr[:-MAX_DIAGNOSTIC_BYTES]
                    self._note(f"stderr clipped to its last {MAX_DIAGNOSTIC_BYTES // 1024} KiB")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stderr is diagnostic; a read failure must not fail the transport
            return

    async def _wait_leader(self) -> None:
        process = self._process
        assert process is not None
        code = await wait_for_exit(process)
        if self._closed:
            return
        reader = self._read_task
        if reader is not None and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), EXIT_GRACE_SECONDS)
            except TimeoutError:
                self._fail(AcpProcessError(f"ACP agent exited with code {code} but its output remained open"))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the reader already owns the failure
                return

    def _fail_pending(self, error: AcpError) -> None:
        pending, self._pending = self._pending, {}
        for entry in pending.values():
            if not entry.future.done():
                entry.future.set_exception(error)

    def _fail(self, error: AcpError) -> None:
        if self._failure is None:
            self._failure = error
        self._fail_pending(error)
        if self._stop_task is None and self._process is not None and self._process.returncode is None \
                and not self._closed:
            self._stop_task = asyncio.create_task(self._stop())

    async def _stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            with suppress(Exception):
                process.stdin.close()
        with suppress(Exception):
            await stop_process(process)

    async def _close(self) -> None:
        self._closed = True
        error = AcpProcessError("ACP client was closed")
        self._fail_pending(error)
        if self._failure is None:
            self._failure = error
        process = self._process
        if self._stop_task is not None:
            self._stop_task.cancel()
            await asyncio.gather(self._stop_task, return_exceptions=True)
            self._stop_task = None
        if process is not None:
            if process.stdin is not None:
                with suppress(Exception):
                    process.stdin.close()
            # ACP agents are expected to exit cleanly when stdin reaches EOF; give
            # that a bounded chance before escalating to process signals.
            with suppress(TimeoutError):
                await asyncio.wait_for(wait_for_exit(process), EOF_GRACE_SECONDS)
            with suppress(Exception):
                await stop_process(process)
            if self._stderr_task is not None:
                with suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(self._stderr_task), STDERR_GRACE_SECONDS)
        current = asyncio.current_task()
        tasks = [task for task in (self._read_task, self._stderr_task, self._leader_task,
                                   self._notification_task, *self._inbound_tasks)
                 if task is not None and task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._notification_drained.set()
