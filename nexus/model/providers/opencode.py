"""OpenCode adapter over its documented Agent Client Protocol surface.

Assumption #2 in the plan (section 0) is resolved the honest way: OpenCode is
driven as a **subprocess agent**, not by scraping its credential store. The
documented surface is ``opencode acp`` -- an ACP (Agent Client Protocol) server
that speaks newline-delimited JSON-RPC 2.0 over stdio (see
https://opencode.ai/docs/acp/). Nexus starts that process with
``create_subprocess_exec`` (never a shell), completes the ``initialize`` /
``session/new`` handshake, and translates one :class:`~nexus.model.request.
ModelRequest` into one ``session/prompt``.

What this adapter deliberately does **not** do:

* It never opens ``~/.local/share/opencode/auth.json`` or any other credential
  file. OpenCode resolves its own credentials inside the subprocess.
* It never offers Nexus-native tools. ACP tool calls are the agent's own
  execution loop; surfacing them as Nexus ``tool_call`` events would make the
  Nexus loop try to execute tools it did not declare. Agent-internal activity is
  surfaced as :class:`~nexus.model.stream.Raw`, never as ``ToolCall*`` events,
  and :meth:`SubprocessAgentProvider.capabilities` reports ``tools=False``.
* It never inherits the parent environment wholesale. Only a fixed OS allowlist
  plus explicitly configured names (:data:`SAFE_ENV_NAMES` / ``inherit_env`` /
  ``env``) reach the child, so an unrelated ``ANTHROPIC_API_KEY`` in the parent
  shell is not copied into an agent that could echo it back.

Structured history and content blocks are flattened to one deterministic text
prompt (the same marker vocabulary the legacy Codex adapter uses). That
limitation is stated in the rendered prompt and in
:data:`SubprocessAgentProvider.LIMITATIONS`.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import suppress
from typing import Any

from ...errors import ConfigError, ProviderError
from ..capabilities import Capabilities
from ..http import redact_secrets
from ..message import (
    ContentBlock,
    Document,
    Image,
    Text,
    ToolResult,
    ToolUse,
)
from ..request import ModelRequest
from ..stream import (
    MessageStart,
    MessageStop,
    Raw,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
)

__all__ = [
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "DEFAULT_ARGS",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROTOCOL_VERSION",
    "SAFE_ENV_NAMES",
    "OpenCodeAuthRequired",
    "OpenCodeError",
    "OpenCodeProvider",
    "SubprocessAgentProvider",
    "build_child_env",
    "render_prompt",
]

#: ACP protocol version this client speaks (schema ``ProtocolVersion``).
PROTOCOL_VERSION = 1

CLIENT_NAME = "nexus"
CLIENT_VERSION = "0.1.0"

#: The documented command. ``opencode acp`` starts the ACP server on stdio.
DEFAULT_EXECUTABLE = "opencode"
DEFAULT_ARGS: tuple[str, ...] = ("acp",)

DEFAULT_TIMEOUT_SECONDS = 600.0

#: Bound on the best-effort ``session/close`` after a turn. It is deliberately
#: independent of (and much shorter than) the turn timeout and all its errors
#: are suppressed: a slow or failing close must never turn a successful,
#: already-streamed turn into a failure.
_CLOSE_TIMEOUT_SECONDS = 5.0

#: Grace period for the agent to acknowledge a ``session/cancel`` before the
#: process group is killed. Bounded so cancellation stays responsive.
_CANCEL_GRACE_SECONDS = 2.0

#: Maximum bytes of a redacted stderr tail carried on a process error.
_STDERR_LIMIT = 8_000

#: Maximum size of a raw (non-normalized) update surfaced as ``Raw``.
_RAW_LIMIT = 4_000

#: Environment variables copied into the agent subprocess by default. This is a
#: credential-free OS baseline: anything that could carry a provider token must
#: be named explicitly via ``inherit_env`` or ``env``.
SAFE_ENV_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SHELL",
    "USER",
    "LOGNAME",
    "TERM",
    "COLORTERM",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "SYSTEMROOT",
    "WINDIR",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
)

#: ACP ``StopReason`` -> normalized :data:`~nexus.model.stream.StopReason`.
#: A cancelled prompt is surfaced as ``error`` so the loop does not record a
#: cancelled turn as a clean end. An unknown or absent reason is likewise an
#: error: the protocol defines a closed vocabulary, so anything else is a
#: surprise that must not be silently reported as a successful ``end_turn``.
_STOP_REASONS: dict[str, str] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "max_turn_requests": "max_tokens",
    "refusal": "refusal",
    "cancelled": "error",
}

_PERMISSION_POLICIES = ("deny", "allow")

_PERMISSION_PREFERRED: dict[str, tuple[str, ...]] = {
    "deny": ("reject_always", "reject_once"),
    "allow": ("allow_always", "allow_once"),
}


class OpenCodeError(ProviderError):
    """An OpenCode ACP transport or protocol failure."""


class OpenCodeAuthRequired(OpenCodeError):
    """The agent requires authentication before a session can be created."""


class _MethodNotFound(Exception):
    """Internal: an inbound ACP request this client does not implement."""


def build_child_env(
    environ: Mapping[str, str] | None = None,
    *,
    inherit_env: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess environment from an explicit allowlist.

    ``environ`` is the source mapping (the parent environment by default). The
    fixed OS allowlist is always copied; ``inherit_env`` names additional parent
    variables; ``env`` supplies explicit literal values last. Nothing else is
    inherited, so a parent ``*_API_KEY`` is absent unless named.
    """
    source = os.environ if environ is None else environ
    child: dict[str, str] = {
        name: source[name] for name in SAFE_ENV_NAMES if name in source
    }
    for name in inherit_env:
        value = source.get(name)
        if value is not None:
            child[name] = value
    if env:
        child.update({key: str(value) for key, value in env.items()})
    return child


def _render_content(blocks: Sequence[ContentBlock]) -> str:
    """Deterministic text projection of a content-block list.

    ACP carries text natively; everything else degrades to a stable bracketed
    marker so nothing is silently dropped. Thinking is dropped: its signature is
    provider-private and not replayable through ACP.
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
        elif isinstance(block, ToolUse):
            arguments = json.dumps(block.input, sort_keys=True, ensure_ascii=False)
            parts.append(f"[tool_use {block.name} {arguments}]")
        elif isinstance(block, ToolResult):
            label = "tool_result_error" if block.is_error else "tool_result"
            parts.append(f"[{label} {_render_content(block.content)}]")
        elif isinstance(block, Image):
            reference = (
                f"data:{block.media_type}"
                if block.data is not None
                else (block.url or block.media_type)
            )
            parts.append(f"[image {reference}]")
        elif isinstance(block, Document):
            title = f" {block.title}" if block.title else ""
            parts.append(f"[document {block.media_type}{title}]")
        # Thinking: dropped by policy; see docstring.
    return "".join(parts)


def render_prompt(req: ModelRequest) -> str:
    """Render a structured request into one ACP text prompt.

    The final user message is the active request; every earlier message becomes
    history. ``metadata["prompt"]`` overrides rendering entirely. Declared Nexus
    tools are named but explicitly marked uncallable, because ACP exposes only
    the agent's own tools.
    """
    override = req.metadata.get("prompt") if req.metadata else None
    if isinstance(override, str):
        return override
    messages = list(req.messages)
    user = ""
    prior = messages
    if messages and messages[-1].role == "user":
        user = _render_content(messages[-1].content)
        prior = messages[:-1]

    lines: list[str] = []
    if req.system:
        lines.extend(("System instructions:", req.system, ""))
    if req.tools:
        names = ", ".join(tool.name for tool in req.tools)
        lines.extend(
            (
                (
                    "Nexus tool schemas are unavailable through the OpenCode ACP "
                    "surface; the agent runs its own tools."
                ),
                f"Declared but not callable here: {names}.",
                "",
            )
        )
    if prior:
        lines.append("Conversation history:")
        lines.extend(
            f"{message.role}: {_render_content(message.content)}" for message in prior
        )
        lines.append("")
    lines.append("Current request:")
    lines.append(user)
    rendered = "\n".join(lines).strip()
    return rendered if rendered else "(empty request)"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_from(payload: object) -> Usage | None:
    """Map an OpenCode prompt-result usage block onto normalized ``Usage``.

    OpenCode reports camelCase counters beyond the ACP schema
    (``inputTokens``/``outputTokens``/``thoughtTokens``/cached read+write); an
    unknown or absent block yields ``None`` rather than a fabricated zero.
    """
    if not isinstance(payload, Mapping):
        return None
    values = {
        "input": _as_int(payload.get("inputTokens")),
        "output": _as_int(payload.get("outputTokens")),
        "reasoning": _as_int(payload.get("thoughtTokens")),
        "cache_read": _as_int(payload.get("cachedReadTokens")),
        "cache_write": _as_int(payload.get("cachedWriteTokens")),
    }
    if all(value is None for value in values.values()):
        return None
    return Usage(
        input=values["input"] or 0,
        output=values["output"] or 0,
        reasoning=values["reasoning"] or 0,
        cache_read=values["cache_read"] or 0,
        cache_write=values["cache_write"] or 0,
    )


def _normalize_stop_reason(reason: object) -> str:
    if isinstance(reason, str):
        return _STOP_REASONS.get(reason, "error")
    return "error"


def _error_detail(error: object) -> str:
    if isinstance(error, Mapping):
        for key in ("message", "data"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return redact_secrets(value.strip())[:300]
    if isinstance(error, str):
        return redact_secrets(error)[:300]
    return ""


def _rpc_error(method: str, error: object) -> ProviderError:
    detail = _error_detail(error)
    if "auth" in detail.lower():
        return OpenCodeAuthRequired(
            "opencode: authentication required; run `opencode auth login` in a "
            "terminal so the ACP subprocess can use its own credential store "
            "(Nexus never reads or copies it)"
        )
    return OpenCodeError(f"opencode: {method} failed: {detail or 'unknown error'}")


def _bounded_raw(update: Mapping[str, Any]) -> dict[str, Any]:
    """A redacted, size-bounded view of an agent-internal update."""
    kind = update.get("sessionUpdate")
    text = redact_secrets(json.dumps(update, ensure_ascii=False, default=str))
    if len(text) > _RAW_LIMIT:
        text = f"{text[:_RAW_LIMIT]}...[truncated]"
    return {"sessionUpdate": str(kind) if kind is not None else "", "payload": text}


def _missing_executable(command: Sequence[str], exc: BaseException) -> OpenCodeError:
    binary = command[0] if command else DEFAULT_EXECUTABLE
    return OpenCodeError(
        f"opencode: could not start ACP agent {binary!r} ({type(exc).__name__}). "
        f"Install OpenCode and ensure {binary!r} is on PATH, or set "
        "command=/executable explicitly. Verify with `opencode --version`."
    )


class _JsonRpcProcess:
    """A JSON-RPC 2.0 client over one ACP subprocess's stdio.

    The process is its own session/group (``start_new_session=True``) so a
    timeout, cancel, or close can terminate the agent and any tool children it
    spawned with one ``killpg``. stdout is newline-delimited JSON; every line is
    dispatched to a pending response, an inbound request handler, or the update
    queue consumed during a prompt.
    """

    def __init__(
        self,
        *,
        command: Sequence[str],
        env: Mapping[str, str],
        cwd: str | None,
        request_handler: Any,
    ) -> None:
        self._command = list(command)
        self._env = dict(env)
        self._cwd = cwd
        self._request_handler = request_handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: deque[str] = deque(maxlen=64)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._streaming_id: int | None = None
        self._stream_result: dict[str, Any] | None = None
        self._protocol_error: ProviderError | None = None
        #: The ACP agent capabilities returned by this process's ``initialize``.
        #: Per-process, so two concurrent streams never observe each other's
        #: handshake result.
        self.agent_capabilities: dict[str, Any] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stream_result(self) -> dict[str, Any]:
        return self._stream_result if self._stream_result is not None else {}

    async def start(self) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
        except OSError as exc:
            # FileNotFoundError/PermissionError/NotADirectoryError and any other
            # spawn failure normalize to one actionable, redacted error.
            raise _missing_executable(self._command, exc) from exc
        self._process = process
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise self._connection_error()
        ident = self._allocate_id()
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[ident] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": ident, "method": method, "params": dict(params)}
            )
        except BaseException:
            # A failed send must not leave an orphan future (and a later
            # response for its id) pinned in ``_pending`` forever.
            self._pending.pop(ident, None)
            raise
        message = await future
        if "error" in message:
            raise _rpc_error(method, message.get("error"))
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        await self._send(
            {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        )

    def notify_nowait(self, method: str, params: Mapping[str, Any]) -> None:
        """Write a notification without awaiting, for use while cancelling.

        Cancellation can be re-delivered at every await point, so an ``await``
        on the write path may abort before the bytes reach the pipe. This writes
        synchronously (no drain, no lock) so the agent reliably receives the
        cancellation; the stream's ``finally`` still kills it afterwards.
        """
        process = self._process
        if process is None or process.stdin is None:
            return
        data = (
            json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": dict(params)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        with suppress(OSError):
            process.stdin.write(data)

    async def flush(self) -> None:
        """Flush buffered stdin writes without taking the write lock."""
        process = self._process
        if process is None or process.stdin is None:
            return
        await process.stdin.drain()

    async def begin_stream(self, method: str, params: Mapping[str, Any]) -> None:
        if self._streaming_id is not None:
            raise OpenCodeError("opencode: a prompt is already streaming")
        self._clear_updates()
        ident = self._allocate_id()
        self._streaming_id = ident
        self._stream_result = None
        await self._send(
            {"jsonrpc": "2.0", "id": ident, "method": method, "params": dict(params)}
        )

    async def updates(self) -> AsyncIterator[dict[str, Any]]:
        """Yield notifications until the streaming request's response arrives."""
        while True:
            message = await self._updates.get()
            if message.get("_closed"):
                raise self._connection_error()
            if message.get("id") == self._streaming_id:
                self._streaming_id = None
                if "error" in message:
                    raise _rpc_error("session/prompt", message.get("error"))
                result = message.get("result")
                self._stream_result = result if isinstance(result, dict) else {}
                return
            yield message

    async def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise OpenCodeError("opencode: ACP process is not running")
        data = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        async with self._write_lock:
            process.stdin.write(data)
            try:
                await process.stdin.drain()
            except OSError as exc:
                raise OpenCodeError(
                    f"opencode: ACP stdin closed ({type(exc).__name__})"
                ) from exc

    async def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    self._protocol_error = OpenCodeError(
                        "opencode: malformed ACP message: "
                        f"{redact_secrets(text)[:200]}"
                    )
                    break
                if isinstance(message, dict):
                    await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError) as exc:
            self._protocol_error = OpenCodeError(
                f"opencode: ACP read failed: {type(exc).__name__}"
            )
        finally:
            await self._wait_for_exit()
            self._fail_pending()

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while chunk := await process.stderr.read(4096):
            self._stderr.append(chunk.decode("utf-8", "replace"))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:
                await self._handle_request(message)
            else:
                self._updates.put_nowait(message)
            return
        ident = message.get("id")
        future = self._pending.pop(ident, None) if isinstance(ident, int) else None
        if future is not None and not future.done():
            future.set_result(message)
        if isinstance(ident, int) and ident == self._streaming_id:
            self._updates.put_nowait(message)

    async def _handle_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        ident = message.get("id")
        handler = self._request_handler
        if handler is None:
            await self._send_error(ident, -32601, f"Method not found: {method}")
            return
        try:
            result = handler(method, message.get("params") or {})
        except _MethodNotFound:
            await self._send_error(ident, -32601, f"Method not found: {method}")
            return
        except ProviderError as exc:
            await self._send_error(ident, -32603, redact_secrets(str(exc))[:300])
            return
        await self._send({"jsonrpc": "2.0", "id": ident, "result": result})

    async def _send_error(self, ident: object, code: int, message: str) -> None:
        await self._send(
            {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
        )

    def _clear_updates(self) -> None:
        while True:
            try:
                self._updates.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _stderr_text(self) -> str:
        return redact_secrets("".join(self._stderr).strip())[:_STDERR_LIMIT]

    def _connection_error(self) -> ProviderError:
        if self._protocol_error is not None:
            return self._protocol_error
        process = self._process
        message = "opencode: ACP process exited"
        if process is not None and process.returncode is not None:
            message += f" with code {process.returncode}"
        tail = self._stderr_text()
        if tail:
            message += f": {tail}"
        return OpenCodeError(message)

    async def _wait_for_exit(self) -> None:
        process = self._process
        if process is None:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)
        if self._stderr_task is not None:
            with suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._stderr_task, timeout=1)

    def _fail_pending(self) -> None:
        error = self._connection_error()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        if self._streaming_id is not None:
            self._streaming_id = None
            self._updates.put_nowait({"_closed": True})

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Fail any request whose response can no longer arrive, even if the
        # reader task never ran its own cleanup (for example, a start/shutdown
        # race where a request was registered just before the process died).
        self._fail_pending()


class SubprocessAgentProvider:
    """A :class:`~nexus.model.provider.Provider` over an ACP agent subprocess.

    One ``stream`` owns one process: it is started on the first ``anext`` and
    torn down (process group SIGTERM, then SIGKILL) in the stream's ``finally``,
    so a timeout, an ``aclose``, or an early consumer close all terminate the
    agent and its tool children.

    ``permission_policy`` is **not a sandbox**. It only chooses how Nexus answers
    the *agent's own* ACP ``session/request_permission`` prompts (``deny`` by
    default, ``allow`` to auto-approve). The agent still runs with the same OS
    privileges as Nexus and can act on its own; containment is the subprocess
    environment allowlist and the agent's own configuration, not this policy.
    """

    name = "subprocess-agent"
    DEFAULT_EXECUTABLE = DEFAULT_EXECUTABLE
    DEFAULT_ARGS: tuple[str, ...] = DEFAULT_ARGS
    DEFAULT_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS

    #: Human-readable, honest description of what the ACP surface cannot carry.
    LIMITATIONS: tuple[str, ...] = (
        (
            "Nexus tools are not offered: the agent runs its own tool loop, and "
            "agent-internal tool activity is surfaced as Raw, never as ToolCall "
            "events."
        ),
        (
            "Structured history and non-text content are flattened into one text "
            "prompt with stable bracketed markers."
        ),
        "Usage is reported only when the agent includes it on the prompt result.",
        (
            "Thinking is streamed when the agent emits a thought chunk but cannot "
            "be replayed (no signature)."
        ),
        (
            "Credentials are resolved inside the subprocess; Nexus never reads the "
            "agent's credential store and only passes explicitly allowed "
            "environment variables."
        ),
        (
            "The permission policy answers the agent's ACP permission prompts; it "
            "is not an OS sandbox and does not confine what the agent can do."
        ),
    )

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        executable: str | None = None,
        args: Sequence[str] | None = None,
        extra_args: Sequence[str] = (),
        workspace: str | os.PathLike[str] | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        environ: Mapping[str, str] | None = None,
        inherit_env: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        permission_policy: str = "deny",
        capabilities: Capabilities | None = None,
    ) -> None:
        if permission_policy not in _PERMISSION_POLICIES:
            raise ConfigError(
                f"opencode: unknown permission_policy {permission_policy!r} "
                f"(expected one of {', '.join(_PERMISSION_POLICIES)})"
            )
        self._workspace = str(workspace) if workspace is not None else None
        self._model = model
        self._timeout_seconds = (
            DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
        self._permission_policy = permission_policy
        self._capabilities = capabilities
        self._command = self._resolve_command(
            command, executable, args, extra_args
        )
        self._child_env = build_child_env(
            environ, inherit_env=inherit_env, env=env
        )
        #: Every in-flight stream's process, so ``aclose`` can terminate them.
        #: Guarded by ``_active_lock`` so a stream's start cannot slip past a
        #: concurrent close (the close/start race).
        self._active: set[_JsonRpcProcess] = set()
        self._active_lock = asyncio.Lock()
        self._closed = False

    def _resolve_command(
        self,
        command: Sequence[str] | None,
        executable: str | None,
        args: Sequence[str] | None,
        extra_args: Sequence[str],
    ) -> list[str]:
        if command is not None:
            resolved = [str(part) for part in command]
        else:
            resolved = [
                str(executable or self.DEFAULT_EXECUTABLE),
                *[str(part) for part in (args if args is not None else self.DEFAULT_ARGS)],
            ]
            if self._workspace:
                resolved.extend(("--cwd", self._workspace))
        resolved.extend(str(part) for part in extra_args)
        return resolved

    def server_argv(self) -> list[str]:
        """The exact argv used to start the agent, without a shell.

        Not for logging: an argv can carry a credential. Use :meth:`__repr__`
        for a redacted rendering.
        """
        return list(self._command)

    def __repr__(self) -> str:
        # An argv can carry a credential (``--token sk-...``), so redact every
        # element; the workspace path and permission policy are safe.
        command = [redact_secrets(part) for part in self._command]
        return (
            f"{type(self).__name__}(command={command!r}, "
            f"workspace={self._workspace!r}, "
            f"permission_policy={self._permission_policy!r})"
        )

    def capabilities(self, model: str) -> Capabilities:
        if self._capabilities is not None:
            return self._capabilities
        return Capabilities(
            tools=False,
            parallel_tool_calls=False,
            streaming=True,
            thinking=True,
            prompt_caching=False,
            vision=False,
            documents=False,
            json_schema_strict=False,
            max_context_tokens=0,
            max_output_tokens=0,
            degradation={
                "thinking": "drop",
                "vision": "to_text",
                "documents": "to_text",
            },
        )

    def describe_limitations(self) -> tuple[str, ...]:
        """The declared limitations of this surface, for logs and UI."""
        return self.LIMITATIONS

    async def count_tokens(self, req: ModelRequest) -> int | None:
        if self._closed:
            raise OpenCodeError("opencode: provider is closed")
        return None

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise OpenCodeError("opencode: provider is closed")
        process = _JsonRpcProcess(
            command=self._command,
            env=self._child_env,
            cwd=self._workspace,
            request_handler=self._handle_agent_request,
        )
        await process.start()
        if not await self._register(process):
            # ``aclose`` won the close/start race: tear the new process down and
            # refuse the stream rather than leaking an unowned child.
            await process.aclose()
            raise OpenCodeError("opencode: provider is closed")
        session_id: str | None = None
        try:
            yield MessageStart(model=req.model or self._model, provider=self.name)
            async with asyncio.timeout(self._timeout_seconds):
                await self._initialize(process)
                session_id = await self._open_session(process)
                async for event in self._run_turn(process, session_id, req):
                    yield event
        except TimeoutError as exc:
            raise OpenCodeError(
                f"opencode: ACP turn exceeded {self._timeout_seconds:g}s"
            ) from exc
        except asyncio.CancelledError:
            # Cancellation is cooperative: ask the agent to stop, give it a
            # bounded grace to acknowledge, then let the ``finally`` kill the
            # process group if it did not.
            if session_id is not None:
                await self._cancel_session(process, session_id)
            raise
        else:
            # The turn already streamed successfully; session/close is a
            # best-effort courtesy *outside* the turn timeout, so a slow or
            # failing close can never convert success into failure.
            if session_id is not None:
                await self._close_session(process, session_id)
        finally:
            await self._unregister(process)
            await process.aclose()

    async def _register(self, process: _JsonRpcProcess) -> bool:
        async with self._active_lock:
            if self._closed:
                return False
            self._active.add(process)
            return True

    async def _unregister(self, process: _JsonRpcProcess) -> None:
        async with self._active_lock:
            self._active.discard(process)

    def _initialize_params(self) -> dict[str, Any]:
        # No fs/terminal client capabilities are advertised: Nexus is not a
        # filesystem or command proxy for the agent.
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }

    async def _initialize(self, process: _JsonRpcProcess) -> None:
        result = await process.request("initialize", self._initialize_params())
        version = result.get("protocolVersion")
        # The ACP protocol version is a closed negotiated contract: a mismatch
        # means the wire shape below cannot be trusted, so refuse rather than
        # decode a possibly-incompatible stream.
        if version != PROTOCOL_VERSION:
            raise OpenCodeError(
                f"opencode: unsupported ACP protocolVersion {version!r} "
                f"(Nexus speaks {PROTOCOL_VERSION})"
            )
        agent_caps = result.get("agentCapabilities")
        process.agent_capabilities = agent_caps if isinstance(agent_caps, dict) else {}

    async def _open_session(self, process: _JsonRpcProcess) -> str:
        cwd = self._workspace or os.getcwd()
        result = await process.request(
            "session/new", {"cwd": os.path.abspath(cwd), "mcpServers": []}
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeError("opencode: session/new returned no sessionId")
        return session_id

    @staticmethod
    async def _close_session(process: _JsonRpcProcess, session_id: str) -> None:
        """Best-effort ``session/close``, bounded and fully suppressed.

        Called only after a successful turn (outside its timeout). Every failure
        path -- a provider error, a timeout, an OS error, or cancellation during
        the close -- is swallowed so a good turn is never reported as failed.
        """
        caps = process.agent_capabilities.get("sessionCapabilities")
        if not isinstance(caps, Mapping) or "close" not in caps:
            return
        with suppress(
            ProviderError, TimeoutError, OSError, asyncio.CancelledError
        ):
            async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                await process.request("session/close", {"sessionId": session_id})

    @staticmethod
    async def _cancel_session(process: _JsonRpcProcess, session_id: str) -> None:
        """Ask the agent to cancel, then wait a bounded grace for its ack.

        The ``session/cancel`` notification is written synchronously (a cancelled
        task cannot reliably await the write), then updates are drained until the
        prompt result arrives or the grace period expires. All errors are
        suppressed because the caller is already unwinding a cancellation; the
        process group is killed in the stream's ``finally`` regardless.
        """
        process.notify_nowait("session/cancel", {"sessionId": session_id})
        # Flush the queued notification; a cancelled task may interrupt the
        # await, but the bytes are already in the transport buffer.
        with suppress(OSError, asyncio.CancelledError):
            await process.flush()
        try:
            async with asyncio.timeout(_CANCEL_GRACE_SECONDS):
                async for _message in process.updates():
                    pass
        except (ProviderError, TimeoutError, OSError, asyncio.CancelledError):
            pass

    async def _run_turn(
        self, process: _JsonRpcProcess, session_id: str, req: ModelRequest
    ) -> AsyncIterator[StreamEvent]:
        await process.begin_stream(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": render_prompt(req)}],
            },
        )
        async for update in process.updates():
            for event in self._translate_update(update):
                yield event
        for event in self._translate_result(process.stream_result):
            yield event

    def _translate_update(self, message: Mapping[str, Any]) -> Iterator[StreamEvent]:
        params = message.get("params")
        if not isinstance(params, Mapping):
            return
        update = params.get("update")
        if not isinstance(update, Mapping):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                yield TextDelta(text=text)
            return
        if kind == "agent_thought_chunk":
            text = _content_text(update.get("content"))
            if text:
                yield ThinkingDelta(text=text)
            return
        # Everything else is agent-internal activity (tool calls, plans, usage
        # updates, mode changes, the echoed user message, and future variants).
        # It is surfaced opaquely so it is not lost, but never as a Nexus
        # ToolCall* event: the loop must not execute a tool it did not declare.
        yield Raw(data=_bounded_raw(update))

    def _translate_result(self, result: Mapping[str, Any]) -> Iterator[StreamEvent]:
        usage = _usage_from(result.get("usage"))
        if usage is not None:
            yield usage
        yield MessageStop(
            stop_reason=_normalize_stop_reason(result.get("stopReason"))  # type: ignore[arg-type]
        )

    def _handle_agent_request(
        self, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        if method == "session/request_permission":
            return {"outcome": self._permission_outcome(params)}
        raise _MethodNotFound(method)

    def _permission_outcome(self, params: Mapping[str, Any]) -> dict[str, Any]:
        options = params.get("options")
        if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
            for kind in _PERMISSION_PREFERRED[self._permission_policy]:
                for option in options:
                    if not isinstance(option, Mapping):
                        continue
                    if option.get("kind") == kind:
                        option_id = option.get("optionId")
                        if isinstance(option_id, str) and option_id:
                            return {"outcome": "selected", "optionId": option_id}
        return {"outcome": "cancelled"}

    async def aclose(self) -> None:
        # Each stream owns its own process; closing the adapter also terminates
        # any stream still in flight, then blocks new streams. Idempotent.
        # The lock makes this atomic with respect to a concurrent stream start,
        # so a process cannot be added to ``_active`` after the snapshot.
        async with self._active_lock:
            if self._closed:
                return
            self._closed = True
            active = list(self._active)
        for process in active:
            await process.aclose()


#: The named adapter for the documented ``opencode acp`` surface.
class OpenCodeProvider(SubprocessAgentProvider):
    """OpenCode over ACP. See :class:`SubprocessAgentProvider` for the contract."""

    name = "opencode"
    DEFAULT_EXECUTABLE = DEFAULT_EXECUTABLE
    DEFAULT_ARGS = DEFAULT_ARGS


def _content_text(content: object) -> str:
    if isinstance(content, Mapping) and content.get("type") == "text":
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""
