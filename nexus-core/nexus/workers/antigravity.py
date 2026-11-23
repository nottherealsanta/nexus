"""Antigravity transports: the legacy agy stream-json worker and the ACP v1 worker.

The stream-json worker remains for existing sessions and for the runtime until the next
milestone wires ACP in. ACP setup is fail-closed: local `agy` session probes were blocked by
authentication, so session/resume, mode ids and the `session/set_model` extension are not
treated as verified; only advertised options or a successful request are used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..llmhub.types import ToolCall
from .acp import AcpClient, AcpError, AcpResponseError, permission_outcome
from .base import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_CHARS,
    MAX_PROMPT_BYTES,
    MAX_RESULT_CHARS,
    WorkerError,
    WorkerEvent,
    WorkerPolicy,
    WorkerResult,
    bounded_lines,
    child_env,
    clip,
    diagnostic_text,
    probe,
    protected_cleanup,
    stop_process,
    wait_for_exit,
)

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_STDOUT_BYTES = 32 * 1024 * 1024
MAX_STEPS = 1000
EXIT_GRACE_SECONDS = 10.0  # live 11-12: agy 1.5.6 took 0.6-2.2 s to exit after stdin closed; 3 s failed once
MIN_VERSION = (1, 4, 12)
ACP_TRANSPORT = "acp-v1"
LEGACY_TRANSPORT = "stream-json-v1"
SETUP_UPDATE_TIMEOUT_SECONDS = 5.0
# Expected ACP ids from the draft/scout. The worker only uses an id the agent
# actually advertises in its modes response, so an unverified id cannot be assumed.
ACP_MODES = ("default", "accept-edits", "auto", "plan")
Emit = Callable[[WorkerEvent], Awaitable[None]]
Approve = Callable[[ToolCall, dict[str, Any]], Awaitable[bool]]
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_ENDS = {"DONE", "ERROR", "CANCELED", "CANCELLED"}
_ASK = re.compile(r"permission|approv|confirm|consent|login|auth|trust|prompt|question", re.IGNORECASE)
_ATTENTION = re.compile(
    r"not (?:logged|signed) in|(?:log|sign) ?in (?:is )?required|please (?:log|sign) ?in|"
    r"authentication (?:required|failed)|unauthenticated|credentials? (?:expired|missing|invalid)|"
    r"trust (?:this|the) (?:folder|workspace|directory)|untrusted (?:folder|workspace|directory)|"
    r"permission (?:required|prompt|request)|requires? (?:your )?(?:approval|permission)", re.IGNORECASE)


class LegacyAntigravitySession(WorkerError):
    """A stored stream-json Antigravity child is readable but cannot be continued by ACP."""


def acp_resumable(worker: Mapping[str, Any] | None) -> bool:
    """Whether a stored Antigravity worker row can be resumed by the ACP worker."""
    return bool(isinstance(worker, Mapping) and worker.get("transport") == ACP_TRANSPORT
                and isinstance(worker.get("session_id"), str) and worker["session_id"]
                and isinstance(worker.get("policy_fingerprint"), str) and worker["policy_fingerprint"])


@dataclass(frozen=True)
class AcpPolicy:
    """ACP session policy. `default` is the prompting mode; accept-edits is never chosen for new children."""

    permission_mode: Literal["default", "accept-edits", "auto", "plan"] = "default"
    effort: str | None = None

    def __post_init__(self) -> None:
        if self.permission_mode not in ACP_MODES:
            raise ValueError("ACP permission mode must be default, accept-edits, auto or plan; bypass is not supported")
        if self.effort is not None and not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", self.effort):
            raise ValueError("invalid ACP reasoning effort")

    @classmethod
    def from_worker_policy(cls, policy: WorkerPolicy, *, effort: str | None = None) -> AcpPolicy:
        """Map legacy ask/allow policies to ACP default; only an explicit plan stays plan."""
        return cls("plan" if policy.permission_mode == "plan" else "default", effort)

    def fingerprint(self, model: str, workspace: Path) -> str:
        body = {"transport": ACP_TRANSPORT, "mode": self.permission_mode, "model": model, "effort": self.effort,
                "directory": str(workspace), "mcp_servers": [], "system_prompt": None}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def authority(self, workspace: Path) -> str:
        return (f"ACP · Antigravity ran its own tools in {workspace} (ACP mode {self.permission_mode}); "
                "the CLI's settings, MCP servers and plugins are inherited. Nexus tools and step limits do not "
                "apply; the CLI's permission requests go to Nexus for approval. The CLI adds context Nexus cannot see; "
                "this is not its full model context.")


@dataclass
class StreamParser:
    model: str
    workspace: Path
    policy: WorkerPolicy
    initialized: bool = False
    finished: bool = False
    session_id: str | None = None
    text: str = ""
    error: str | None = None
    attention: str | None = None
    usage: dict[str, int] | None = None
    notes: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    tools: dict[int, dict] = field(default_factory=dict)
    closed_tools: set[int] = field(default_factory=set)

    def feed(self, raw: bytes) -> list[WorkerEvent]:
        events = [WorkerEvent("wire", raw=raw)]
        if self.finished or self.error or self.attention:
            return events
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            return events + [self._notice("malformed stream line was not interpreted")]
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            return events + [self._notice("unknown stream shape was not interpreted")]
        kind = event["event"]
        data = event.get(kind)
        if kind in ("init", "step_update", "result") and not isinstance(data, dict):
            self.error = f"malformed {kind} payload"
        elif kind == "init":
            if self.initialized:
                self.error = "duplicate init event"
            elif data.get("permission_mode") == "always-proceed":
                self.error = "agy inherited always-proceed; bypass is not supported by this transport"
            elif data.get("cwd") is not None and data["cwd"] != str(self.workspace):
                self.error = "agy reported a different working directory"
            elif data.get("model") is not None and data["model"] != self.model:
                self.error = "agy reported a different model"
            else:
                self.initialized = True
                session = event.get("conversation_id")
                self.session_id = session[:200] if isinstance(session, str) else None
                events.append(WorkerEvent("init", {**data, "conversation_id": self.session_id}))
        elif kind == "result" and not self.initialized and data.get("status") != "SUCCESS":
            self.finished = True
            self._result(data)
            self.error = diagnostic_text(str(data.get("error") or self.error).encode("utf-8", "replace"))[:500]
            events.append(WorkerEvent("result", data))
        elif kind in ("step_update", "result") and not self.initialized:
            self.error = "agy emitted work or success before init"
        elif kind == "step_update":
            events.extend(self._step(data))
        elif kind == "result":
            self.finished = True
            self._result(data)
            events.append(WorkerEvent("result", data))
        elif _ASK.search(kind):
            self.attention = f"agy requested {kind!r}. Run `agy` interactively in the workspace; Nexus approved nothing."
        else:
            events.append(self._notice(f"unknown agy event {kind[:100]!r} was not interpreted"))
        return events

    def _notice(self, note: str) -> WorkerEvent:
        if note not in self.notes:
            self.notes.append(note)
        return WorkerEvent("notice", {"text": note})

    def _step(self, data: dict) -> list[WorkerEvent]:
        index, kind, state = data.get("step_index"), data.get("step_type"), data.get("state")
        if type(index) is not int or not 0 <= index < MAX_STEPS:
            self.error = "invalid step index or transport step-record limit reached (not an agent step limit)"
            return []
        if kind == "agent_response":
            delta = data.get("text_delta")
            if isinstance(delta, str):
                self.text, note = clip(self.text + delta, MAX_RESULT_CHARS)
                events = [WorkerEvent("text", {"index": index, "text": delta})]
                if note:
                    events.append(self._notice(f"partial answer {note}"))
                return events
            return []  # a closing agent_response step without text (agy 1.5.0 sends usage here)
        elif kind in ("user_input", "system_message"):
            return []  # recorded in the wire event; nothing to interpret (seen live on agy 1.5.0)
        elif kind == "tool":
            if index in self.closed_tools:
                return []
            info = data.get("tool_info")
            info = info if isinstance(info, dict) else {}
            events = []
            if index not in self.tools:
                name = data.get("tool_name") or info.get("name")
                self.tools[index] = {"index": index, "name": name if isinstance(name, str) else "unknown",
                                     "input": info.get("parameters", {})}
                events.append(WorkerEvent("tool.start", self.tools[index]))
            if not isinstance(state, str):
                self.error = "malformed tool step state"
                return events
            if state in _ENDS:
                events.append(WorkerEvent("tool.end", {**self.tools.pop(index), "state": state,
                                                       "output": info.get("output"), "error": info.get("error")}))
                self.closed_tools.add(index)
            return events
        return [self._notice(f"unknown step type {str(kind)[:100]!r} was not interpreted")]

    def _result(self, data: dict) -> None:
        session = data.get("conversation_id")
        if isinstance(session, str):
            self.session_id = session[:200]
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage = {k[:80]: v for k, v in list(usage.items())[:32] if type(v) is int and v >= 0} or None
        denied = data.get("denied_actions")
        if isinstance(denied, list):
            self.denied = [diagnostic_text(json.dumps(item, ensure_ascii=False).encode("utf-8", "replace"))[:500]
                           for item in denied[:20]]
            if len(denied) > 20:
                self._notice("denied actions clipped to 20 entries; full result is in the wire event")
        answer = data.get("response")
        if isinstance(answer, str) and answer.strip():
            self.text, note = clip(answer, MAX_RESULT_CHARS)
            if note:
                self._notice(f"answer {note}")
        if data.get("status") != "SUCCESS":
            self.error = "agy terminal result did not report SUCCESS"
            if _ATTENTION.search(str(data.get("error", ""))):
                self.attention = "Run `agy` interactively in the workspace to resolve sign-in, trust or permissions."
        elif not isinstance(answer, str):
            self.error = "agy success did not contain a response string"
        elif self.denied or not answer.strip():
            self.attention = ("agy denied actions or returned an empty answer; headless mode cannot ask for approval. "
                              "Run `agy` interactively in the workspace and review its own permission settings. "
                              "Nexus approved nothing.")
        elif self.tools:
            self.error = "agy reported success with unfinished tool steps"


class AntigravityWorker:
    transport = LEGACY_TRANSPORT

    def __init__(self, executable: str, *, environ: Mapping[str, str] | None = None) -> None:
        if not executable or "\x00" in executable:
            raise ValueError("agy executable must be a non-empty path or name")
        self.executable = executable
        self.env = child_env(environ)

    async def preflight(self, policy: WorkerPolicy, *, conversation: bool = False, effort: bool = False) -> str:
        if os.name != "posix":
            raise WorkerError("External-worker transports currently require POSIX process groups")
        cwd = Path(tempfile.gettempdir())
        version_text = await probe(self.executable, "--version", env=self.env, cwd=cwd)
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_text)
        if not match or tuple(int(v) for v in match.groups()) < MIN_VERSION:
            raise WorkerError("agy 1.4.12 or newer is required; CLI version could not be verified")
        help_text = await probe(self.executable, "--help", env=self.env, cwd=cwd)
        required = ["--input-format", "--output-format", "stream-json", "--model", "--mode", "--print-timeout", policy.permission_mode]
        if conversation:
            required.append("--conversation")
        if effort:
            required.append("--effort")
        missing = [flag for flag in required if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text)]
        if missing:
            raise WorkerError("agy lacks required headless flags/values: " + ", ".join(missing))
        return match.group(0)

    def build_argv(self, model: str, policy: WorkerPolicy, *, conversation: str | None = None, effort: str | None = None) -> list[str]:
        argv = [self.executable, "--input-format", "stream-json", "--output-format", "stream-json",
                "--model", model, "--mode", policy.permission_mode, "--print-timeout", "0s"]
        if conversation:
            argv += ["--conversation", conversation]
        if effort:
            argv += ["--effort", effort]
        return argv

    async def run(self, *, prompt: str, model: str, workspace: Path, policy: WorkerPolicy | None = None,
                  emit: Emit | None = None, conversation: str | None = None, effort: str | None = None) -> WorkerResult:
        policy = policy or WorkerPolicy()
        if conversation and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", conversation):
            raise ValueError("invalid agy conversation id")
        if effort and not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", effort):
            raise ValueError("invalid agy effort")
        if not _MODEL.fullmatch(model):
            raise ValueError("agy requires an exact model ID, not an option or an alias")
        if "\x00" in prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("agy prompt contains NUL or exceeds 64 KiB; refusing to truncate")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("agy workspace must be a directory")
        version = await self.preflight(policy, conversation=bool(conversation), effort=bool(effort))
        argv = self.build_argv(model, policy, conversation=conversation, effort=effort)
        parser = StreamParser(model, workspace, policy)
        diagnostics = bytearray()
        stop_requested = asyncio.Event()
        emitted_notes: set[str] = set()
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=workspace, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)

        async def stderr() -> None:
            while chunk := await process.stderr.read(4096):
                diagnostics.extend(chunk)
                if len(diagnostics) > MAX_DIAGNOSTIC_BYTES:
                    del diagnostics[:-MAX_DIAGNOSTIC_BYTES]
                    parser._notice("stderr clipped to its last 16 KiB")
                if _ATTENTION.search(diagnostic_text(bytes(diagnostics))):
                    parser.attention = "Run `agy` interactively in the workspace to resolve sign-in, trust or permissions."
                    stop_requested.set()

        async def stdout() -> None:
            async for raw in bounded_lines(process.stdout, line_limit=MAX_LINE_BYTES, total_limit=MAX_STDOUT_BYTES):
                if emit is not None:
                    await emit(WorkerEvent("wire", raw=raw))
                for event in parser.feed(raw)[1:]:
                    if emit is not None:
                        await emit(event)
                        if event.kind == "notice":
                            emitted_notes.add(event.data["text"])
                if parser.finished or parser.attention or parser.error:
                    stop_requested.set()

        async def send() -> None:
            message = {"event": "user", "message": {"content": prompt}}
            try:
                process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise WorkerError("agy closed stdin before prompt delivery completed") from None

        err = asyncio.create_task(stderr())
        out = asyncio.create_task(stdout())
        writer = asyncio.create_task(send())
        stop_wait = asyncio.create_task(stop_requested.wait())
        leader = asyncio.create_task(wait_for_exit(process))

        async def cleanup() -> None:
            process.stdin.close()
            try:
                await stop_process(process)
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(err), .2)
            finally:
                for task in (out, err, writer, stop_wait, leader):
                    task.cancel()
                await asyncio.gather(out, err, writer, stop_wait, leader, return_exceptions=True)

        try:
            if emit:
                await emit(WorkerEvent("launch", {"cli_version": version, "argv": argv, "directory": str(workspace),
                                                  "pid": process.pid, "transport": LEGACY_TRANSPORT}))
            try:
                pending = {out, stop_wait, leader, writer}
                while True:
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    if writer in done:
                        await writer
                        pending.discard(writer)
                    if done - {writer}:
                        break
                if leader in done and not out.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(out), EXIT_GRACE_SECONDS)
                    except TimeoutError:
                        parser.error = "agy exited but its stdout remained open; descendants may still be running"
                if out in done:
                    await out
            except WorkerError as exc:
                parser.error = str(exc)
                parser._notice(str(exc))
            process.stdin.close()
            if parser.finished and not parser.error and not parser.attention:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(asyncio.shield(leader), asyncio.shield(out), asyncio.shield(writer)), EXIT_GRACE_SECONDS)
                except TimeoutError:
                    parser.error = "agy did not close its process/stream after the terminal result; descendants may still be running"
                except WorkerError as exc:
                    parser.error = str(exc)
                    parser._notice(str(exc))
                if process.returncode not in (0, None):
                    parser.error = f"agy exited with code {process.returncode} after its terminal result"
        finally:
            await protected_cleanup(cleanup())
        full_diag = diagnostic_text(bytes(diagnostics))
        diag, note = clip(full_diag, MAX_DIAGNOSTIC_CHARS)
        if note:
            parser._notice(f"diagnostics {note}")
        fallback = re.search(r"--mode \S+ is not available[^\n]*", full_diag, re.IGNORECASE)
        if fallback:
            parser._notice(f"agy mode fallback: {fallback.group(0)}")
        if _ATTENTION.search(diag) and (not parser.finished or parser.error):
            parser.attention = "Run `agy` interactively in the workspace to resolve sign-in, trust or permissions."
        if not parser.finished and not parser.error and not parser.attention:
            parser.error = "agy stream ended without a terminal result; completion is not known"
        status = "needs_attention" if parser.attention else "failed" if parser.error else "completed"
        if emit is not None:
            for index, tool in parser.tools.items():
                await emit(WorkerEvent("tool.end", {**tool, "index": index, "state": "CANCELED",
                                                    "error": "stream ended without a tool result"}))
            for note in parser.notes:
                if note not in emitted_notes:
                    await emit(WorkerEvent("notice", {"text": note}))
        return WorkerResult(status, parser.text, policy.authority(workspace, vendor="Antigravity"), session_id=parser.session_id,
                            usage=parser.usage, error=parser.attention or parser.error, diagnostics=diag,
                            notes=tuple(parser.notes), denied_actions=tuple(parser.denied))

# -- ACP ------------------------------------------------------------------------------------------------
#
# The ACP shapes below follow protocol v1. The local `agy` session probes were blocked by
# authentication, so session/resume, mode ids and the `session/set_model` extension are treated as
# unverified: the worker uses only what the agent advertises or what a successful request confirms,
# and refuses to continue otherwise. Live verification waits for `agy acp --login` in the user's home.

AUTH_INSTRUCTION = "Antigravity ACP is not signed in. Run `agy acp --login` interactively, then retry."
_ACP_AUTH = re.compile(
    r"authentication required|not (?:logged|signed) in|(?:log|sign) ?in (?:is )?required|unauthenticated|"
    r"credentials? (?:expired|missing|invalid)", re.IGNORECASE)
_ACP_TOOL_NAMES = {"read": "read_file", "edit": "edit", "delete": "delete", "move": "move", "search": "Grep",
                   "execute": "run_command", "think": "think", "fetch": "web_fetch", "switch_mode": "switch_mode"}
_ACP_TERMINAL_TOOLS = {"completed": "DONE", "failed": "ERROR"}
_ACP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")


class _AcpSetupError(Exception):
    """A fail-closed setup invariant, distinct from transport and remote errors."""


def _safe_acp(value: object, limit: int = 1000) -> str:
    return clip(diagnostic_text(str(value).encode("utf-8", "replace")), limit)[0]


def _auth_required(exc: AcpResponseError) -> bool:
    return exc.code == -32000 or "auth" in str(exc).lower()


def _acp_args(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else ({} if value is None else {"value": value})


def _acp_call_id(call_id: str) -> str:
    """A bounded Nexus tool-call id matching the ACP tool call id for approval/timeline joins."""
    safe = call_id if _ACP_ID.fullmatch(call_id) else hashlib.sha256(call_id.encode()).hexdigest()[:32]
    return f"agy:{safe}"


def _acp_tool_name(tool: Mapping[str, Any]) -> str:
    name = tool.get("name")
    if isinstance(name, str) and name:
        return name[:200]
    kind = tool.get("kind")
    if isinstance(kind, str) and kind in _ACP_TOOL_NAMES:
        return _ACP_TOOL_NAMES[kind]
    title = tool.get("title")
    return title[:200] if isinstance(title, str) and title else "tool"


def _permission_option(options: object, allow: bool) -> str | None:
    wanted = "allow_once" if allow else "reject_once"
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and option.get("kind") == wanted:
            value = option.get("optionId")
            if isinstance(value, str) and value:
                return value
    return None


def _content_text(content: object) -> tuple[str, str | None]:
    if not isinstance(content, dict):
        return "", None
    kind = content.get("type")
    if kind == "text" and isinstance(content.get("text"), str):
        return content["text"], None
    if kind == "resource_link" and isinstance(content.get("uri"), str):
        return content["uri"], None
    if kind == "resource" and isinstance(content.get("resource"), dict):
        resource = content["resource"]
        if isinstance(resource.get("text"), str):
            return resource["text"], None
        if isinstance(resource.get("uri"), str):
            return resource["uri"], None
    if kind in ("image", "audio"):
        return f"[{kind}]", None
    return "", (f"ACP content type {_safe_acp(kind, 80)!r} is wire-only" if kind is not None else None)


def _tool_content(content: object) -> tuple[str, list[str]]:
    if not isinstance(content, list):
        return "", []
    parts: list[str] = []
    notes: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "content":
            text, note = _content_text(item.get("content"))
        elif kind == "diff":
            path = item.get("path") if isinstance(item.get("path"), str) else "file"
            before = item.get("oldText") if isinstance(item.get("oldText"), str) else ""
            after = item.get("newText") if isinstance(item.get("newText"), str) else ""
            text, note = f"--- {path}\n{before}\n+++ {path}\n{after}", None
        elif kind == "terminal":
            text, note = f"[terminal {str(item.get('terminalId'))[:100]}]", None
        elif kind == "session":
            text, note = f"[session {str(item.get('sessionId'))[:100]}]", None
        else:
            text = ""
            note = f"ACP tool content type {_safe_acp(kind, 80)!r} is wire-only" if kind is not None else None
        if note:
            notes.append(note)
        if text:
            parts.append(text)
    return "\n".join(parts), notes


def _available_models(result: Mapping[str, Any]) -> set[str]:
    models = result.get("models")
    if not isinstance(models, dict) or not isinstance(models.get("availableModels"), list):
        return set()
    return {model["modelId"] for model in models["availableModels"]
            if isinstance(model, dict) and isinstance(model.get("modelId"), str) and model["modelId"]}


def _model_confirmation(requested: str, model_values: list[str], thought_values: list[str]) -> tuple[str, str | None]:
    """The base model and effort the config options should report for a requested per-effort model id."""
    for effort in thought_values:
        suffix = f"-{effort}"
        if requested.endswith(suffix) and requested[:-len(suffix)] in model_values:
            return requested[:-len(suffix)], effort
    return requested, None


def _acp_usage(value: object) -> dict[str, int] | None:
    """Normalize the prompt result's per-turn usage for the Nexus Usage event."""
    if not isinstance(value, dict):
        return None
    mapping = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
               "thoughtTokens": "reasoning_tokens", "cachedReadTokens": "cache_read_input_tokens"}
    usage = {dst: count for src, dst in mapping.items()
             if type(count := value.get(src)) is int and count >= 0}
    return usage or None


def _available_modes(modes: object) -> set[str] | None:
    if not isinstance(modes, dict) or not isinstance(modes.get("availableModes"), list):
        return None
    ids = {mode["id"] for mode in modes["availableModes"]
           if isinstance(mode, dict) and isinstance(mode.get("id"), str) and mode["id"]}
    return ids or None


def _thought_level_options(config_options: object) -> list[dict[str, Any]]:
    if not isinstance(config_options, list):
        return []
    return [option for option in config_options if isinstance(option, dict)
            and (option.get("id") == "thought_level" or option.get("category") == "thought_level")]


def _model_options(config_options: object) -> list[dict[str, Any]]:
    if not isinstance(config_options, list):
        return []
    return [option for option in config_options if isinstance(option, dict)
            and (option.get("id") == "model" or option.get("category") == "model")]


def _config_values(option: Mapping[str, Any]) -> list[str]:
    values: list[str] = []

    def add(items: object) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("value"), str):
                values.append(item["value"])
            add(item.get("options"))

    add(option.get("options"))
    return values


def _config_current(config_options: object, config_id: str) -> str | None:
    if not isinstance(config_options, list):
        return None
    for option in config_options:
        if isinstance(option, dict) and option.get("id") == config_id and isinstance(option.get("currentValue"), str):
            return option["currentValue"]
    return None


@dataclass
class _AcpTool:
    index: int
    call_id: str
    name: str
    input: dict[str, Any]
    title: str
    kind: str
    status: str
    content: str = ""
    raw_output: object = None
    ended: bool = False


@dataclass
class _AcpUpdates:
    text: str = ""
    notes: list[str] = field(default_factory=list)
    tools: dict[str, _AcpTool] = field(default_factory=dict)
    seen_notes: set[str] = field(default_factory=set)
    mode: str | None = None
    model: str | None = None
    effort: str | None = None
    config_options: list[dict[str, Any]] | None = None
    next_index: int = 0

    def note(self, text: str) -> WorkerEvent | None:
        if text in self.seen_notes:
            return None
        self.seen_notes.add(text)
        self.notes.append(text)
        return WorkerEvent("notice", {"text": text})

    def feed(self, update: object) -> list[WorkerEvent]:
        if not isinstance(update, dict):
            event = self.note("malformed ACP session/update was not interpreted")
            return [event] if event else []
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str):
            event = self.note("ACP session/update without a sessionUpdate kind was not interpreted")
            return [event] if event else []
        if kind == "agent_message_chunk":
            return self._text(update.get("content"))
        if kind == "agent_thought_chunk":
            event = self.note("ACP thought chunks are wire-only")
            return [event] if event else []
        if kind == "user_message_chunk":
            return []  # the prompt is already in Nexus's timeline
        if kind in ("tool_call", "tool_call_update"):
            return self._tool(update)
        if kind == "current_mode_update":
            mode = update.get("currentModeId")
            if isinstance(mode, str) and mode:
                self.mode = mode
                return [WorkerEvent("process", {"permission_mode": mode})]
            event = self.note("ACP current_mode_update without a mode id was not interpreted")
            return [event] if event else []
        if kind == "config_option_update":
            options = update.get("configOptions")
            if not isinstance(options, list):
                return []
            self.config_options = options
            models = _model_options(options)
            if len(models) == 1 and isinstance(models[0].get("currentValue"), str):
                self.model = models[0]["currentValue"]
            found = _thought_level_options(options)
            if len(found) == 1 and isinstance(found[0].get("currentValue"), str):
                self.effort = found[0]["currentValue"]
                return [WorkerEvent("process", {"effort": self.effort})]
            return []
        if kind == "usage_update":
            event = self.note("ACP context-window usage updates are wire-only")
            return [event] if event else []
        event = self.note(f"ACP session update {_safe_acp(kind, 100)!r} is wire-only")
        return [event] if event else []

    def _text(self, content: object) -> list[WorkerEvent]:
        if not isinstance(content, dict) or content.get("type") != "text" or not isinstance(content.get("text"), str):
            event = self.note("ACP non-text agent content is wire-only")
            return [event] if event else []
        delta = content["text"]
        if not delta:
            return []
        self.text, note = clip(self.text + delta, MAX_RESULT_CHARS)
        events = [WorkerEvent("text", {"text": delta})]
        if note:
            event = self.note(f"final result summary {note}; the full streamed answer remains in the assistant message")
            if event:
                events.append(event)
        return events

    def _tool(self, update: dict) -> list[WorkerEvent]:
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            event = self.note("ACP tool update without a toolCallId was not interpreted")
            return [event] if event else []
        tool = self.tools.get(call_id)
        events: list[WorkerEvent] = []
        new = tool is None
        if new:
            tool = _AcpTool(self.next_index, call_id, _acp_tool_name(update), _acp_args(update.get("rawInput")),
                            str(update.get("title") or "")[:500], str(update.get("kind") or ""),
                            str(update.get("status") or "pending"), raw_output=update.get("rawOutput"))
            self.next_index += 1
            self.tools[call_id] = tool
        else:
            if isinstance(update.get("title"), str):
                tool.title = update["title"][:500]
            if isinstance(update.get("name"), str) and update["name"]:
                tool.name = update["name"][:200]
            if isinstance(update.get("kind"), str):
                tool.kind = update["kind"]
            if "rawInput" in update:
                tool.input = _acp_args(update.get("rawInput"))
            if "rawOutput" in update:
                tool.raw_output = update.get("rawOutput")
            if isinstance(update.get("status"), str):
                tool.status = update["status"]
        if new:
            events.append(WorkerEvent("tool.start", {"index": tool.index, "id": _acp_call_id(tool.call_id),
                                                     "name": tool.name, "input": tool.input,
                                                     "tool_call_id": tool.call_id, "title": tool.title,
                                                     "kind": tool.kind, "state": tool.status.upper()}))
        if "content" in update:
            tool.content, content_notes = _tool_content(update.get("content"))
            for text in content_notes:
                event = self.note(text)
                if event:
                    events.append(event)
        state = _ACP_TERMINAL_TOOLS.get(tool.status)
        if state and not tool.ended:
            output = self._tool_output(tool, events)
            data = {"index": tool.index, "id": _acp_call_id(tool.call_id), "state": state, "output": output,
                    "tool_call_id": tool.call_id, "title": tool.title, "kind": tool.kind}
            if state == "ERROR":
                data["error"] = "the ACP agent reported the tool call failed"
            tool.ended = True
            events.append(WorkerEvent("tool.end", data))
        return events

    def _tool_output(self, tool: _AcpTool, events: list[WorkerEvent]) -> str:
        value = tool.raw_output if tool.raw_output is not None else tool.content
        if isinstance(value, str):
            text = value
        elif value is None:
            text = ""
        else:
            text = json.dumps(value, ensure_ascii=True, default=str)
        text, note = clip(text, MAX_RESULT_CHARS)
        if note:
            event = self.note(f"tool output {note}")
            if event:
                events.append(event)
        return text

    def close_tools(self) -> list[WorkerEvent]:
        events = []
        for tool in self.tools.values():
            if tool.ended:
                continue
            events.append(WorkerEvent("tool.end", {"index": tool.index, "id": _acp_call_id(tool.call_id),
                                                   "state": "CANCELED", "output": tool.content,
                                                   "error": "ACP stream ended without a tool result",
                                                   "tool_call_id": tool.call_id, "title": tool.title,
                                                   "kind": tool.kind}))
            tool.ended = True
        return events


class AntigravityAcpWorker:
    """Antigravity's ACP v1 transport, kept separate from the legacy stream-json worker.

    ACP setup is fail-closed: an advertised mode, model confirmation, resume capability and effort
    option are required where applicable. The agent's permission requests run through `approve`, which
    is designed to call Nexus's existing approval path; the runtime wiring follows in the next milestone.
    """

    transport = ACP_TRANSPORT

    def __init__(self, executable: str, *, environ: Mapping[str, str] | None = None) -> None:
        if not executable or "\x00" in executable:
            raise ValueError("agy executable must be a non-empty path or name")
        self.executable = executable
        self.env = child_env(environ)

    async def preflight(self) -> str:
        if os.name != "posix":
            raise WorkerError("ACP stdio transport requires POSIX process groups")
        cwd = Path(tempfile.gettempdir())
        version_text = await probe(self.executable, "--version", env=self.env, cwd=cwd)
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_text)
        if not match:
            raise WorkerError("agy version could not be verified for ACP")
        return match.group(0)

    def build_argv(self) -> list[str]:
        return [self.executable, "acp"]

    async def run(self, *, prompt: str, model: str, workspace: Path, policy: AcpPolicy | None = None,
                  emit: Emit | None = None, resume: str | None = None, previous_transport: str | None = None,
                  previous_fingerprint: str | None = None, approve: Approve | None = None) -> WorkerResult:
        policy = policy or AcpPolicy()
        if not isinstance(policy, AcpPolicy):
            raise TypeError("policy must be an AcpPolicy")
        if "\x00" in prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("agy prompt contains NUL or exceeds 64 KiB; refusing to truncate")
        if not _MODEL.fullmatch(model):
            raise ValueError("agy requires an exact model ID, not an option or an alias")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("agy workspace must be a directory")
        if resume is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", resume):
            raise ValueError("invalid ACP session id")
        fingerprint = policy.fingerprint(model, workspace)
        if resume is not None:
            if previous_transport != ACP_TRANSPORT:
                raise LegacyAntigravitySession("this Antigravity session was created by the legacy stream-json "
                                               "transport and cannot be continued; start a new child session")
            if previous_fingerprint != fingerprint:
                raise WorkerError("this Antigravity ACP session was created with a different model, mode, effort "
                                  "or directory; start a new child session")
        version = await self.preflight()

        updates = _AcpUpdates()
        session_id = resume
        failure: str | None = None
        attention: str | None = None
        denied: list[str] = []
        approval_unavailable = False
        prompt_result: dict[str, Any] | None = None
        usage: dict[str, int] | None = None
        config_seen = asyncio.Event()

        async def send(kind: str, data: dict[str, Any] | None = None, raw: bytes | None = None) -> None:
            if emit is not None:
                await emit(WorkerEvent(kind, data or {}, raw=raw))

        async def note(text: str) -> None:
            event = updates.note(text)
            if event is not None and emit is not None:
                await emit(event)

        async def on_message(raw: bytes) -> None:
            await send("wire", raw=raw)

        async def on_notification(method: str, params: object) -> None:
            if method != "session/update":
                await note(f"ACP notification {_safe_acp(method, 100)!r} is wire-only")
                return
            if not isinstance(params, dict) or not isinstance(params.get("update"), dict):
                await note("malformed ACP session/update was not interpreted")
                return
            incoming = params.get("sessionId")
            if session_id is not None and isinstance(incoming, str) and incoming != session_id:
                await note("an ACP update for a different session was wire-only")
                return
            for event in updates.feed(params["update"]):
                if emit is not None:
                    await emit(event)
            if params["update"].get("sessionUpdate") == "config_option_update":
                config_seen.set()

        async def on_permission(params: object) -> dict[str, Any] | None:
            nonlocal approval_unavailable
            if not isinstance(params, dict):
                await note("malformed ACP permission request was cancelled")
                return None
            if session_id is None or params.get("sessionId") != session_id:
                await note("an ACP permission request for a different session was cancelled")
                return None
            tool = params.get("toolCall")
            if not isinstance(tool, dict):
                await note("ACP permission request without a tool call was cancelled")
                return None
            call_id = tool.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                await note("ACP permission request without a tool call id was cancelled")
                return None
            call = ToolCall(_acp_call_id(call_id), _acp_tool_name(tool),
                            json.dumps(_acp_args(tool.get("rawInput")), ensure_ascii=True), "agy",
                            {"permission": params, "acp_tool_call_id": call_id})
            title = str(tool.get("title") or call.name)[:200]
            options = params.get("options")
            if _permission_option(options, True) is None and _permission_option(options, False) is None:
                approval_unavailable = True
                denied.append(_safe_acp(title, 200))
                await note("Antigravity offered only persistent permission options; Nexus refused them and "
                           "cancelled the request")
                return None
            if approve is None:
                approval_unavailable = True
                denied.append(_safe_acp(title, 200))
                await note("Antigravity requested permission but no Nexus approval callback is configured; "
                           "the request was cancelled")
                return None
            try:
                allowed = await approve(call, _acp_args(tool.get("rawInput")))
            except asyncio.CancelledError:
                approval_unavailable = True
                denied.append(_safe_acp(title, 200))
                await note("Antigravity permission approval was interrupted; the request was cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - a broken approval must deny, never allow
                denied.append(_safe_acp(title, 200))
                await note(f"Antigravity permission approval failed ({type(exc).__name__}); "
                           "the request was rejected")
                option_id = _permission_option(params.get("options"), False)
                if option_id is not None:
                    return permission_outcome(option_id)
                approval_unavailable = True
                return None
            if not isinstance(allowed, bool):
                denied.append(_safe_acp(title, 200))
                await note("Antigravity permission approval returned no decision; the request was rejected")
                option_id = _permission_option(params.get("options"), False)
                if option_id is not None:
                    return permission_outcome(option_id)
                approval_unavailable = True
                return None
            option_id = _permission_option(params.get("options"), allowed)
            if option_id is None:
                approval_unavailable = True
                denied.append(_safe_acp(title, 200))
                await note("Antigravity offered no per-call option for this decision; Nexus cancelled the request")
                return None
            if not allowed:
                denied.append(_safe_acp(title, 200))
            return permission_outcome(option_id)

        client = AcpClient(self.executable, ["acp"], cwd=workspace, environ=self.env,
                           on_message=on_message, on_notification=on_notification, on_permission=on_permission)
        try:
            await client.start()
            await send("launch", {"pid": client.pid, "argv": self.build_argv(), "directory": str(workspace),
                                  "transport": ACP_TRANSPORT, "cli_version": version,
                                  "policy_fingerprint": fingerprint,
                                  "permission_mode": policy.permission_mode, "effort": policy.effort})
            info = await client.initialize(client_capabilities={})
            if not isinstance(info, dict):
                raise _AcpSetupError("the ACP initialize result was not an object")
            capabilities = info.get("agentCapabilities")
            capabilities = capabilities if isinstance(capabilities, dict) else {}
            session_caps = capabilities.get("sessionCapabilities")
            if not isinstance(session_caps, dict):
                session_caps = capabilities.get("session_capabilities")
            session_caps = session_caps if isinstance(session_caps, dict) else {}

            if resume is not None:
                if "resume" not in session_caps:
                    raise _AcpSetupError("the ACP agent does not advertise session/resume; this child cannot be continued")
                result = await client.request("session/resume", {"sessionId": resume, "cwd": str(workspace),
                                                                 "mcpServers": []})
                session_id = resume
            else:
                result = await client.request("session/new", {"cwd": str(workspace), "mcpServers": []})
                if not isinstance(result, dict):
                    raise _AcpSetupError("the ACP session/new result was not an object")
                sid = result.get("sessionId")
                if not isinstance(sid, str) or not sid:
                    raise _AcpSetupError("the ACP agent did not return a session id")
                session_id = sid
            if not isinstance(result, dict):
                raise _AcpSetupError("the ACP session result was not an object")
            await send("init", {"conversation_id": session_id, "transport": ACP_TRANSPORT})
            await send("process", {"pid": client.pid, "session_id": session_id, "transport": ACP_TRANSPORT})
            await note("Antigravity ACP ran its own tools; the CLI adds context Nexus cannot see")

            # The live ACP server advertises exact per-effort ids under `models.availableModels`, but its
            # `configOptions.model` values are base model ids. An exact advertised model id uses the agy
            # `session/set_model` extension and is verified through the subsequent configOptionUpdate; an
            # exact model config value is the spec fallback. `models.currentModelId` is stale on this server
            # and is deliberately never treated as confirmation.
            initial_config = result.get("configOptions")
            model_options = _model_options(initial_config)
            if len(model_options) > 1:
                raise _AcpSetupError("the ACP agent advertised more than one model option; model selection is ambiguous")
            model_option = model_options[0] if model_options else None
            model_values = _config_values(model_option) if model_option else []
            models_field = result.get("models")
            models_advertised = isinstance(models_field, dict) and isinstance(models_field.get("availableModels"), list)
            available_models = _available_models(result)
            thought_options = _thought_level_options(initial_config)
            thought_values = _config_values(thought_options[0]) if len(thought_options) == 1 else []
            strict_model_update = False
            if model in available_models:
                await client.flush_notifications()
                updates.model = None
                updates.effort = None
                updates.config_options = None
                config_seen.clear()
                model_result = await client.request("session/set_model", {"sessionId": session_id, "modelId": model})
                if model_result is not None and not isinstance(model_result, dict):
                    raise _AcpSetupError("the ACP model result was not an object")
                strict_model_update = True
            elif model in model_values:
                config_id = model_option.get("id") if model_option else None
                if not isinstance(config_id, str) or not config_id:
                    raise _AcpSetupError("the ACP model option had no id")
                model_result = await client.request("session/set_config_option",
                                                    {"sessionId": session_id, "configId": config_id, "value": model})
                if not isinstance(model_result, dict) or not isinstance(model_result.get("configOptions"), list):
                    raise _AcpSetupError("the ACP model config result did not include configOptions")
                if _config_current(model_result["configOptions"], config_id) != model:
                    raise _AcpSetupError("the ACP agent did not confirm the requested model")
            elif model_option is None and not models_advertised:
                # Older agents may expose only the agy session/set_model extension. It is accepted here
                # only because the agent advertised no model surface at all, and its own result must
                # confirm the exact model; the live 1.5.14 server uses the strict advertised-model path.
                model_result = await client.request("session/set_model", {"sessionId": session_id, "modelId": model})
                if not isinstance(model_result, dict):
                    raise _AcpSetupError("the ACP model result was not an object")
                confirmed = model_result.get("modelId", model_result.get("model"))
                if confirmed != model:
                    raise _AcpSetupError("the ACP agent did not confirm the requested model")
                await client.flush_notifications()
            else:
                advertised = sorted(set(model_values) | available_models)
                raise _AcpSetupError(f"the ACP agent did not advertise model {_safe_acp(model, 120)!r}; "
                                     f"available: {', '.join(advertised) or 'none'}")

            modes = model_result.get("modes") if isinstance(model_result, dict) else None
            modes = modes if isinstance(modes, dict) else result.get("modes")
            available = _available_modes(modes)
            if not available:
                raise _AcpSetupError("the ACP agent did not advertise session modes; the permission mode "
                                     "cannot be guaranteed")
            if policy.permission_mode not in available:
                raise _AcpSetupError(f"the ACP agent did not advertise mode {_safe_acp(policy.permission_mode, 100)!r}; "
                                     f"available modes: {', '.join(sorted(available))}")
            updates.mode = None  # only the set_mode outcome and later updates count as confirmation
            mode_result = await client.request("session/set_mode", {"sessionId": session_id,
                                                                     "modeId": policy.permission_mode})
            if mode_result is not None and not isinstance(mode_result, dict):
                raise _AcpSetupError("the ACP mode result was not an object")
            if isinstance(mode_result, dict) and isinstance(mode_result.get("modes"), dict):
                confirmed_mode = mode_result["modes"].get("currentModeId")
                if isinstance(confirmed_mode, str) and confirmed_mode != policy.permission_mode:
                    raise _AcpSetupError("the ACP agent did not confirm the requested mode")
            await client.flush_notifications()
            if updates.mode is not None and updates.mode != policy.permission_mode:
                raise _AcpSetupError(f"the ACP agent changed to mode {_safe_acp(updates.mode, 100)!r} after the requested mode")
            if strict_model_update:
                # The live probe sends session/set_model and then session/set_mode before its
                # config_option_update; wait for that update after sending the mode as well.
                try:
                    await asyncio.wait_for(config_seen.wait(), SETUP_UPDATE_TIMEOUT_SECONDS)
                except TimeoutError:
                    raise _AcpSetupError("the ACP agent did not confirm the requested model via configOptions") from None
                await client.flush_notifications()
                if updates.model is None:
                    raise _AcpSetupError("the ACP agent did not confirm the requested model via configOptions")
                expected_model, expected_effort = _model_confirmation(model, model_values, thought_values)
                if updates.model != expected_model:
                    raise _AcpSetupError(f"the ACP agent reported model {_safe_acp(updates.model, 100)!r}, "
                                         f"expected {_safe_acp(expected_model, 100)!r}")
                if expected_effort is not None and updates.effort != expected_effort:
                    raise _AcpSetupError(f"the ACP agent reported thinking level "
                                         f"{_safe_acp(updates.effort, 100)!r}, expected {expected_effort!r}")

            if isinstance(model_result, dict) and isinstance(model_result.get("configOptions"), list):
                config_options = model_result["configOptions"]
            elif updates.config_options is not None:
                config_options = updates.config_options
            else:
                config_options = result.get("configOptions")
            if policy.effort is not None:
                options = _thought_level_options(config_options)
                if len(options) != 1:
                    raise _AcpSetupError("the ACP agent did not advertise exactly one thought_level option; "
                                         "the reasoning effort cannot be guaranteed")
                option = options[0]
                config_id = option.get("id")
                if not isinstance(config_id, str) or not config_id:
                    raise _AcpSetupError("the ACP thought_level option had no id")
                values = _config_values(option)
                if policy.effort not in values:
                    raise _AcpSetupError(f"the ACP thought_level option does not allow {_safe_acp(policy.effort, 100)!r}; "
                                         f"available: {', '.join(values) or 'none'}")
                await client.flush_notifications()
                updates.effort = None  # only the set_config_option outcome and later updates count
                effort_result = await client.request("session/set_config_option",
                                                     {"sessionId": session_id, "configId": config_id,
                                                      "value": policy.effort})
                if effort_result is not None and not isinstance(effort_result, dict):
                    raise _AcpSetupError("the ACP effort result was not an object")
                if isinstance(effort_result, dict) and isinstance(effort_result.get("configOptions"), list):
                    current = _config_current(effort_result["configOptions"], config_id)
                    if current is not None and current != policy.effort:
                        raise _AcpSetupError("the ACP agent did not confirm the requested thinking level")
                await client.flush_notifications()
                if updates.effort is not None and updates.effort != policy.effort:
                    raise _AcpSetupError(f"the ACP agent reported thinking level {_safe_acp(updates.effort, 100)!r} after "
                                         f"the requested {_safe_acp(policy.effort, 100)!r}")

            await send("process", {"permission_mode": updates.mode or policy.permission_mode,
                                   "effort": updates.effort or policy.effort})
            prompt_result = await client.request("session/prompt", {
                "sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]})
            await client.flush_notifications()
            if not isinstance(prompt_result, dict):
                raise _AcpSetupError("the ACP prompt result was not an object")
            usage = _acp_usage(prompt_result.get("usage"))
            if client.pending_inbound:
                approval_unavailable = True
                await note("Antigravity still had an unanswered permission or client request when the prompt "
                           "ended; it was cancelled")
        except asyncio.CancelledError:
            if session_id is not None:
                with suppress(AcpError, asyncio.CancelledError):
                    await client.notify("session/cancel", {"sessionId": session_id})
            raise
        except AcpResponseError as exc:
            if _auth_required(exc):
                attention = AUTH_INSTRUCTION
            else:
                failure = f"Antigravity ACP request failed: {_safe_acp(exc)}"
        except _AcpSetupError as exc:
            failure = str(exc)
        except AcpError as exc:
            if _ACP_AUTH.search(client.diagnostics):
                attention = AUTH_INSTRUCTION
            else:
                failure = f"Antigravity ACP transport failed: {_safe_acp(exc)}"
        finally:
            for event in updates.close_tools():
                if emit is not None:
                    await emit(event)
            with suppress(asyncio.CancelledError):
                await client.close()

        if failure is not None and _ACP_AUTH.search(client.diagnostics):
            failure, attention = None, AUTH_INSTRUCTION
        stop_reason = prompt_result.get("stopReason") if isinstance(prompt_result, dict) else None
        text = updates.text
        if attention is not None:
            status, error = "needs_attention", attention
        elif failure is not None:
            status, error = "failed", failure
        elif approval_unavailable:
            status = "needs_attention"
            error = ("Antigravity requested permission, but Nexus could not obtain an approval; the request was "
                     "cancelled. Nexus approved nothing.")
        elif denied:
            status = "needs_attention"
            error = ("Antigravity's permission request was declined; the CLI may have continued without the tool. "
                     "Nexus approved nothing.")
        elif stop_reason == "end_turn":
            if text.strip():
                status, error = "completed", None
            else:
                status = "needs_attention"
                error = "Antigravity ended the turn without an answer; the CLI may have refused or still need attention."
        elif stop_reason == "refusal":
            status = "needs_attention"
            error = "Antigravity refused this turn; rephrase the assignment or review the CLI's own settings."
        elif stop_reason == "cancelled":
            status, error = "failed", "the Antigravity ACP agent cancelled the prompt"
        elif stop_reason in ("max_tokens", "max_turn_requests"):
            status, error = "failed", f"the Antigravity ACP agent stopped with {stop_reason}"
        elif stop_reason is None:
            status = "failed"
            error = failure or "the Antigravity ACP agent returned no terminal prompt result"
        else:
            status, error = "failed", f"unknown ACP stop reason {_safe_acp(stop_reason, 100)!r}"
        if len(denied) > 20:
            await note("denied actions were clipped to 20 entries")
        return WorkerResult(status, text, policy.authority(workspace), session_id=session_id, usage=usage,
                            error=error, diagnostics=client.diagnostics, notes=tuple(updates.notes),
                            denied_actions=tuple(list(dict.fromkeys(denied))[:20]), policy_fingerprint=fingerprint)
