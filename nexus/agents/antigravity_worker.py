"""External Antigravity (``agy``) worker: one headless run per delegated assignment.

Contract (plans/ANTIGRAVITY_PLAN.md sections 3-5), same duck contract as
:mod:`nexus.agents.claude_code_worker` (``run(...)`` returns its
:class:`WorkerResult`; ``resolve_policy``/``WorkerConfig.from_section``):

* ``agy --input-format stream-json --output-format stream-json`` runs its OWN
  agent loop and tools with cwd set to the assigned workspace (``-p`` is NOT
  passed: streaming mode only listens on stdin). Nexus does not translate or mediate them:
  its dispatcher, permission engine and PathGuard do not apply, and NO sandbox
  is requested, so nothing here claims one. That is stated in every result.
* Authority comes from agy's own user settings, MCP servers and plugins, so the
  inheritance defaults to ``worker_inherits_agy_settings = true``. Any
  configuration Nexus cannot enforce on agy (read-only role, tool lists, MCP
  config, setting sources, ``restricted``, non-agy permission modes) is refused,
  never silently ignored. Bypass/"always proceed" flags are never passed and
  Nexus never answers a prompt: login, trust and permission needs end the
  assignment as ``needs_attention``.
* Readback is the NDJSON event stream on stdout. Success requires an explicit
  terminal ``result`` event (see :data:`SCHEMA`); EOF, a non-zero exit or a
  malformed terminal event is never success. stderr is a bounded, ANSI-stripped,
  redacted diagnostic only.
* The prompt travels on stdin as one documented stream-json user event
  (``{"event":"user","message":{"content":...}}``; bounded, never argv), then EOF.
  The child leads its own process group, which is terminated on completion,
  cancel, deadline and close. Token usage is reported only from agy's terminal
  ``result.usage``; cost is never estimated.

SCHEMA STATUS: the event shapes follow antigravity.google/docs/cli/headless
(``init``, ``step_update``, ``result``; every event is keyed by ``event`` with
its payload under the same-named key). No real agy turn has been run. Success
needs a prior ``init`` (whose ``permission_mode`` must not be ``always-proceed``)
and a ``result`` whose ``status`` is exactly ``SUCCESS``. The documented list of
other statuses was not available, so any other status is a failure that
preserves the partial answer. The shapes live in :data:`SCHEMA`.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import ConfigError, ProviderError
from ..model.providers.opencode import build_child_env
from ..util import redact_secrets
from .claude_code_worker import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_CHARS,
    MAX_PROMPT_BYTES,
    MAX_RESULT_CHARS,
    MAX_TIMEOUT_SECONDS,
    MAX_TOOL_CONTENT_CHARS,
    PROBE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    Emit,
    WorkerResult,
    _bound_input,
    _is_cancelled,
    _result_text,
    changed_paths,
    clip,
    nexus_child_is_read_only,
    snapshot_workspace,
    terminal_text,
)

__all__ = [
    "AntigravityWorker",
    "PROVENANCE",
    "PROVIDER",
    "SCHEMA",
    "WorkerConfig",
    "WorkerPolicy",
    "WorkerResult",
    "changed_paths",
    "nexus_child_is_read_only",
    "resolve_policy",
    "snapshot_workspace",
]

PROVIDER = "antigravity"
PROVENANCE = "Antigravity (own tools)"
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_STDOUT_BYTES = 32 * 1024 * 1024
MAX_BLOCKS = 200
#: How long a finished or terminating child may take to exit before SIGKILL.
EXIT_GRACE_SECONDS = 3.0
TERM_GRACE_SECONDS = 2.0
#: Slack so Nexus' own deadline, not agy's ``--print-timeout``, decides first.
PRINT_TIMEOUT_SLACK = 5

#: Everything the parser matches on lives here (see module docs).
SCHEMA: dict[str, Any] = {
    "init": "init",              # init.{cwd,tools,permission_mode,model}; conversation_id at top level
    "step": "step_update",       # step_update.{step_index,state,step_type,text_delta,tool_name,tool_info,subagent_info,usage}
    "terminal": "result",        # result.{conversation_id,status,response,usage,...}
    "success_status": "SUCCESS",
    "unsafe_permission_mode": "always-proceed",
    "input_event": "user",       # stdin: {"event":"user","message":{"content":prompt}}
}
MAX_TOOLS_LISTED = 200
MAX_SUBAGENTS = 20
_MODES = {"auto": "auto", "accept-edits": "accept-edits", "acceptEdits": "accept-edits", "plan": "plan"}
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
#: Event types that mean agy is asking for something Nexus will never answer.
_ASK_EVENT_RE = re.compile(r"permission|approv|confirm|consent|login|auth|trust|prompt|question", re.I)
_LOGIN_RE = re.compile(
    r"not (?:logged|signed) in|(?:log|sign) ?in (?:is )?required|please (?:log|sign) ?in|unauthenticated|"
    r"authentication (?:required|failed)|credentials? (?:expired|missing|invalid)|run `?agy`? .{0,30}sign", re.I)
_TRUST_RE = re.compile(r"trust (?:this|the) (?:folder|workspace|directory)|(?:folder|workspace|directory) is not trusted|"
                       r"untrusted (?:folder|workspace|directory)", re.I)
_PERMISSION_RE = re.compile(
    r"permission (?:required|prompt|request)|requires? (?:your )?(?:approval|permission)|"
    r"awaiting (?:your )?(?:approval|permission|confirmation)|needs? (?:your )?approval", re.I)


def _attention_for(text: str) -> str | None:
    """What the user must do manually, if ``text`` shows a login/trust/permission need."""
    if _LOGIN_RE.search(text):
        return "authentication required: run `agy` interactively once and sign in, then re-run the assignment"
    if _TRUST_RE.search(text):
        return "workspace trust required: run `agy` interactively in the workspace, approve trust, then re-run"
    if _PERMISSION_RE.search(text):
        return ("agy is waiting for a permission approval that Nexus will not give; adjust agy's own "
                "permission settings (docs: antigravity.google/docs/cli/permissions) and re-run")
    return None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerConfig:
    """Operator configuration of the ``antigravity`` provider section."""

    executable: str = "agy"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    permission_mode: str = "auto"
    tools: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    setting_sources: str = ""
    mcp_config: str | None = None
    restricted: bool = False
    #: Enabled by default: agy applies its own settings, MCP servers and plugins.
    worker_inherits_agy_settings: bool | None = True
    config_dir: Path | None = None

    def __post_init__(self) -> None:
        timeout = self.timeout_seconds
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS):
            raise ConfigError("antigravity timeout_seconds must be between 0 and 3600")
        if not isinstance(self.executable, str) or not self.executable.strip() or "\x00" in self.executable:
            raise ConfigError("antigravity executable must be a non-empty path or name")
        if self.worker_inherits_agy_settings not in (None, True, False):
            raise ConfigError("antigravity worker_inherits_agy_settings must be a boolean")

    @classmethod
    def from_section(cls, section: Any, *, config_dir: Path | None = None) -> WorkerConfig:
        def listed(name: str) -> tuple[str, ...] | None:
            value = getattr(section, name, None)
            return None if value is None else tuple(str(item) for item in value)

        timeout = getattr(section, "timeout_seconds", None)
        restricted = getattr(section, "worker_restricted", None)
        sources = getattr(section, "worker_setting_sources", None)
        return cls(
            executable=getattr(section, "executable", None) or "agy",
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
            permission_mode=getattr(section, "worker_permission_mode", None) or "auto",
            tools=listed("worker_tools"),
            allowed_tools=listed("worker_allowed_tools") or (),
            disallowed_tools=listed("worker_disallowed_tools") or (),
            setting_sources="" if sources is None else sources,
            mcp_config=getattr(section, "worker_mcp_config", None),
            restricted=False if restricted is None else bool(restricted),
            # Older section-like callers use None for an omitted option. Preserve
            # an explicit False, but give omission the same default as the schema.
            worker_inherits_agy_settings=(
                True if getattr(section, "worker_inherits_agy_settings", None) is None
                else section.worker_inherits_agy_settings
            ),
            config_dir=config_dir,
        )


@dataclass(frozen=True)
class WorkerPolicy:
    """The resolved, explicit external-execution authority of one assignment."""

    permission_mode: str
    executable: str = "agy"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    read_only: bool = False
    #: agy's tools cannot be listed or restricted by Nexus.
    tools: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    setting_sources: str = ""
    mcp_config: str | None = None
    restricted: bool = False
    config_dir: Path | None = None

    def authority(self, workspace: str | Path) -> str:
        """The model/user-facing statement of what ran and who mediated it."""
        return (f"Antigravity ran its own tools (--mode {self.permission_mode}; agy's own user settings, MCP "
                f"servers and plugins inherited; no sandbox requested) in {workspace}; Nexus "
                "permissions/PathGuard did not mediate them")


def resolve_policy(config: WorkerConfig, *, read_only: bool) -> WorkerPolicy:
    """Resolve the authority for one assignment, refusing anything not enforceable on agy."""
    if config.worker_inherits_agy_settings is not True:
        raise ConfigError(
            "antigravity worker refused: worker_inherits_agy_settings is explicitly disabled; "
            "agy applies its own user/global settings, MCP servers and plugins "
            "and Nexus PathGuard does not apply inside it; set worker_inherits_agy_settings = true to accept that")
    if read_only:
        raise ConfigError(
            "antigravity worker refused: this role is read-only in Nexus, but agy's read-only enforcement "
            "(--mode plan) is not verified to prevent writes and Nexus cannot restrict agy's tools")
    mode = _MODES.get(config.permission_mode)
    if mode is None:
        raise ConfigError("antigravity worker_permission_mode must be auto, accept-edits or plan "
                          f"(got {config.permission_mode!r}); bypass and other modes are never passed")
    unsupported = []
    if config.tools is not None:
        unsupported.append("worker_tools")
    if config.allowed_tools:
        unsupported.append("worker_allowed_tools")
    if config.disallowed_tools:
        unsupported.append("worker_disallowed_tools")
    if config.setting_sources:
        unsupported.append("worker_setting_sources")
    if config.mcp_config:
        unsupported.append("worker_mcp_config")
    if config.restricted:
        unsupported.append("worker_restricted")
    if unsupported:
        raise ConfigError(f"antigravity worker refused: {', '.join(unsupported)} cannot be enforced on agy; "
                          "remove them rather than have them silently ignored")
    return WorkerPolicy(permission_mode=mode, executable=config.executable,
                        timeout_seconds=float(config.timeout_seconds), read_only=False,
                        config_dir=config.config_dir)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    """Mutable per-assignment state."""

    session: str
    model: str
    workspace: Path
    policy: WorkerPolicy
    emit: Emit | None
    texts: list[str] = field(default_factory=list)
    tools: dict[str, str] = field(default_factory=dict)
    tool_calls: int = 0
    clip_notes: list[str] = field(default_factory=list)
    #: ``(status, text, error)`` once an explicit terminal event arrived.
    final: tuple[str, str, str | None] | None = None
    attention: str | None = None
    refusal: str | None = None
    unknown_events: int = 0
    malformed_lines: int = 0
    oversized_lines: int = 0
    stdout_bytes: int = 0
    stdout_truncated: bool = False
    stderr: bytes = b""
    agy_session: str | None = None
    init_seen: bool = False
    init_tools: list[str] = field(default_factory=list)
    agy_permission_mode: str | None = None
    #: Terminal ``result.usage`` (authoritative); ``None`` if agy reported none.
    usage: dict[str, int] | None = None
    #: step_index -> text accumulated from ``text_delta`` of a not yet DONE agent_response.
    pending: dict[int, str] = field(default_factory=dict)
    #: step_index of tool steps already announced as requested.
    announced: set[int] = field(default_factory=set)

    def partial_text(self) -> str:
        """The latest answer text, including an unfinished streamed step."""
        if self.pending:
            return self.pending[max(self.pending)]
        return self.texts[-1] if self.texts else ""


class AntigravityWorker:
    """Launch and supervise headless ``agy`` runs (see module docs)."""

    def __init__(self, config: WorkerConfig, *, environ: Mapping[str, str] | None = None) -> None:
        self._config = config
        self._environ = environ
        self._active: set[asyncio.subprocess.Process] = set()
        self._closed = False

    @property
    def config(self) -> WorkerConfig:
        return self._config

    # -- process helpers -------------------------------------------------

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        """SIGTERM the child's group, SIGKILL after a grace period (never a reaped pid)."""
        if process.returncode is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), TERM_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
        if process.returncode is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            await process.wait()

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.gather(*(self._stop(p) for p in tuple(self._active)))

    def _env(self) -> dict[str, str]:
        return build_child_env(self._environ)

    async def _probe(self, executable: str, flag: str, env: dict, cwd: str) -> str:
        process = await asyncio.create_subprocess_exec(
            executable, flag, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        self._active.add(process)

        async def collect() -> str:
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > MAX_PROMPT_BYTES:
                    raise ProviderError("antigravity preflight output exceeds limit")
            if await process.wait() != 0:
                raise ProviderError(f"antigravity preflight `{flag}` failed")
            return output.decode("utf-8", errors="replace")

        try:
            return await asyncio.wait_for(collect(), PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise ProviderError(f"antigravity preflight timed out after {PROBE_TIMEOUT_SECONDS:g}s") from None
        finally:
            await self._stop(process)
            self._active.discard(process)

    async def _preflight(self, policy: WorkerPolicy, env: dict, cwd: str) -> None:
        version = await self._probe(policy.executable, "--version", env, cwd)
        if not _VERSION_RE.search(version):
            raise ProviderError("antigravity --version did not report a version; refusing to start")
        help_text = await self._probe(policy.executable, "--help", env, cwd)
        required = ["--input-format", "--output-format", "stream-json", "--model", "--mode", "--print-timeout",
                    policy.permission_mode]
        # Match flag tokens, not prefixes (e.g. --print-timeout is not --print).
        missing = [item for item in required if not re.search(
            rf"(?<![\w-]){re.escape(item)}(?![\w-])", help_text)]
        if missing:
            raise ProviderError(
                "antigravity lacks required headless flags/values: "
                + ", ".join(missing) + "; refusing to start (check `agy --help`)"
            )

    # -- argv ------------------------------------------------------------

    @staticmethod
    def build_argv(policy: WorkerPolicy, *, model: str, print_timeout: int) -> list[str]:
        """The documented streaming argv; stream-json selects headless stdin input.

        Do not pass ``--print``/``-p``: those accept a command-line prompt, which
        streaming mode ignores. The prompt is sent only as a user event on stdin.
        ``--print-timeout`` is a Go-style duration (``60s``), never a bare integer.
        """
        return [policy.executable, "--input-format", "stream-json", "--output-format", "stream-json",
                "--model", model, "--mode", policy.permission_mode, "--print-timeout", f"{print_timeout}s"]

    @staticmethod
    def build_input(prompt: str) -> bytes:
        """The one documented stream-json user event carrying the prompt, newline-terminated."""
        message = {"event": SCHEMA["input_event"], "message": {"content": prompt}}
        return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8", errors="replace")

    # -- stream -> events ------------------------------------------------

    @staticmethod
    async def _send(run: _Run, event: str, data: dict[str, Any]) -> None:
        if run.emit is not None:
            await run.emit(event, data)

    async def _handle(self, run: _Run, event: Mapping[str, Any]) -> None:
        kind = event.get("event")
        payload = event.get(kind) if isinstance(kind, str) else None
        if kind == SCHEMA["init"] and isinstance(payload, Mapping):
            self._init(run, event, payload)
        elif kind == SCHEMA["step"] and isinstance(payload, Mapping):
            if not run.init_seen:
                run.refusal = "agy emitted work before its init event; refusing to continue"
            else:
                await self._step(run, payload)
        elif kind == SCHEMA["terminal"]:
            self._terminal(run, payload if isinstance(payload, Mapping) else {})
        elif isinstance(kind, str) and _ASK_EVENT_RE.search(kind):
            run.attention = (_attention_for(str(event)[:2000])
                             or f"agy emitted a {kind!r} event (it is asking for something Nexus will not answer); "
                                "resolve it in agy's own settings and re-run")
        else:
            run.unknown_events += 1  # retained only as a count; never interpreted

    @staticmethod
    def _init(run: _Run, event: Mapping[str, Any], init: Mapping[str, Any]) -> None:
        """Validate agy's init: its authority must be the one Nexus asked for."""
        conversation = event.get("conversation_id")
        run.agy_session = conversation[:200] if isinstance(conversation, str) else None
        run.init_seen = True
        mode = init.get("permission_mode")
        run.agy_permission_mode = mode[:64] if isinstance(mode, str) else None
        tools = init.get("tools")
        if isinstance(tools, list):
            names = [t.get("name") if isinstance(t, Mapping) else t for t in tools[:MAX_TOOLS_LISTED]]
            run.init_tools = [n[:100] for n in names if isinstance(n, str)]
        cwd, model = init.get("cwd"), init.get("model")
        if mode == SCHEMA["unsafe_permission_mode"]:
            run.refusal = ("agy runs with permission_mode 'always-proceed' (possibly from its global settings); "
                           "Nexus never allows that")
        elif cwd is not None and (not isinstance(cwd, str) or Path(cwd).resolve() != run.workspace):
            run.refusal = f"agy reported cwd {str(cwd)[:200]!r}, not the assigned workspace; refusing to continue"
        elif model is not None and model != run.model:
            run.refusal = (f"agy reported model {str(model)[:100]!r}, not the requested {run.model!r}; "
                           "refusing to continue")

    async def _step(self, run: _Run, step: Mapping[str, Any]) -> None:
        index, state, stype = step.get("step_index"), step.get("state"), step.get("step_type")
        if isinstance(index, bool) or not isinstance(index, int):
            run.unknown_events += 1
            return
        if stype == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str):
                buffered = run.pending.get(index, "") + delta
                run.pending[index] = buffered[:MAX_RESULT_CHARS * 2]
            if state == "DONE" and index in run.pending:
                full = run.pending.pop(index)
                if full.strip():
                    text, note = clip(full, MAX_RESULT_CHARS)
                    if note:
                        run.clip_notes.append(f"assistant message {note}")
                    run.texts.append(text)
                    await self._send(run, "text", {
                        "text": text + (f"\n[{note}]" if note else ""), "provider": PROVIDER,
                        "model": run.model, "source": "antigravity-stream"})
                    await self._send(run, "model.stopped", {"stop_reason": "end_turn"})
        elif stype == "tool":
            await self._tool_step(run, index, state, step)
        else:
            run.unknown_events += 1
        subs = step.get("subagent_info")
        if isinstance(subs, list) and subs and stype != "tool":
            await self._subagent_step(run, index, subs)

    async def _subagent_step(self, run: _Run, index: int, subs: list[object]) -> None:
        lines = "subagents:\n" + "\n".join(self._subagent_line(s) for s in subs[:MAX_SUBAGENTS])
        text, note = clip(redact_secrets(lines), MAX_TOOL_CONTENT_CHARS)
        if note:
            run.clip_notes.append(f"subagent metadata {note}")
        await self._send(run, "external.tool", {
            "phase": "completed", "call_id": f"step-{index}-subagents", "tool": "subagents",
            "content": [{"type": "text", "text": text}], "is_error": False,
            "provenance": PROVENANCE, "provider": PROVIDER, "session": run.session,
            **({"clip_note": note} if note else {}),
        })

    async def _tool_step(self, run: _Run, index: int, state: object, step: Mapping[str, Any]) -> None:
        info = step.get("tool_info") if isinstance(step.get("tool_info"), Mapping) else {}
        name = step.get("tool_name") or info.get("name")
        name = name[:100] if isinstance(name, str) else ""
        call_id = f"step-{index}"
        base = {"call_id": call_id, "tool": name, "provenance": PROVENANCE, "provider": PROVIDER,
                "session": run.session}
        if index not in run.announced and run.tool_calls < MAX_BLOCKS:
            run.announced.add(index)
            run.tool_calls += 1
            flags: list[str] = []
            params = info.get("parameters")
            bounded = _bound_input(params if isinstance(params, Mapping) else {}, flags)
            if flags:
                run.clip_notes.append(f"{name} input {flags[0]}")
            await self._send(run, "external.tool", {
                "phase": "requested", "input": bounded,
                **({"clip_note": "; ".join(dict.fromkeys(flags))} if flags else {}), **base})
        if state != "DONE" or index not in run.announced:
            return
        error = info.get("error")
        parts = [_result_text(info.get("output"))] if info.get("output") is not None else []
        if isinstance(error, Mapping) and error:
            parts.append(f"error {error.get('type') or ''}: {error.get('message') or ''}".strip())
        elif error:
            parts.append(f"error: {error}")
        subs = info.get("subagent_info")
        if isinstance(subs, list) and subs:
            parts.append("subagents:\n" + "\n".join(self._subagent_line(s) for s in subs[:MAX_SUBAGENTS]))
        text, note = clip(redact_secrets("\n".join(parts)), MAX_TOOL_CONTENT_CHARS)
        if note:
            run.clip_notes.append(f"{name or 'tool'} result {note}")
        run.announced.discard(index)
        await self._send(run, "external.tool", {
            "phase": "completed", "content": [{"type": "text", "text": text}], "is_error": bool(error),
            **({"clip_note": note} if note else {}), **base})

    @staticmethod
    def _subagent_line(sub: object) -> str:
        if not isinstance(sub, Mapping):
            return f"- {str(sub)[:200]}"
        keys = ("type_name", "role", "conversation_id", "log_uri", "workspace_uris")
        return "- " + ", ".join(f"{k}={json.dumps(sub[k], ensure_ascii=False)[:300]}" for k in keys if k in sub)

    @staticmethod
    def _terminal(run: _Run, result: Mapping[str, Any]) -> None:
        usage = result.get("usage")
        if isinstance(usage, Mapping):
            run.usage = {str(k)[:40]: v for k, v in list(usage.items())[:16]
                         if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
        answer, status = result.get("response"), result.get("status")
        if run.pending:  # a streamed step that never reported DONE is still the agent's text
            run.texts.append(run.pending[max(run.pending)])
            run.pending.clear()
        if status == SCHEMA["success_status"] and run.init_seen and isinstance(answer, str):
            text = answer if answer.strip() else (run.texts[-1] if run.texts else "")
            if text.strip():
                run.final = ("completed", text, None)
                return
            # SUCCESS with no answer at all: agy ends this way after soft-denying a tool request in headless
            # mode (shell commands default to Ask), so an empty final is never reported as completed.
            reason = ("agy reported SUCCESS but returned no answer; a tool request may have been soft-denied "
                      "because headless mode cannot ask for approval (add scoped `permissions.allow` rules in "
                      "agy's own settings, docs: antigravity.google/docs/cli/headless) and re-run")
            run.attention = reason
            run.final = ("failed", "", reason)
            return
        if status == SCHEMA["success_status"]:
            detail = ("success reported before init" if not run.init_seen else "success without a response string")
        else:
            detail = f"status {str(status)[:60]!r}: " + str(result.get("error") or result.get("response") or "")
        detail = redact_secrets(detail)[:500]
        run.attention = _attention_for(detail)
        run.final = ("failed", "", f"agy reported a failed or unrecognised terminal result: {detail}")

    async def _line(self, run: _Run, raw: bytes) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            run.malformed_lines += 1
            return
        if not isinstance(event, Mapping):
            run.malformed_lines += 1
            return
        await self._handle(run, event)

    async def _read_stdout(self, run: _Run, stream: asyncio.StreamReader) -> None:
        buf, skipping = bytearray(), False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                if buf and not skipping:
                    await self._line(run, bytes(buf))
                return
            run.stdout_bytes += len(chunk)
            if run.stdout_bytes > MAX_STDOUT_BYTES:
                run.stdout_truncated = True
                return
            buf.extend(chunk)
            while (cut := buf.find(b"\n")) >= 0:
                line = bytes(buf[:cut])
                del buf[:cut + 1]
                if skipping:
                    skipping = False
                elif line.strip():
                    await self._line(run, line)
                if run.final or run.attention or run.refusal:
                    return
            if len(buf) > MAX_LINE_BYTES:
                buf.clear()
                skipping = True
                run.oversized_lines += 1

    @staticmethod
    async def _read_stderr(run: _Run, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            run.stderr = (run.stderr + chunk)[-MAX_DIAGNOSTIC_BYTES:]

    # -- main entry ------------------------------------------------------

    async def run(
        self,
        *,
        prompt: str,
        model: str,
        workspace: Path,
        policy: WorkerPolicy,
        max_seconds: float = 0.0,
        cancel: object | None = None,
        emit: Emit | None = None,
    ) -> WorkerResult:
        """Run one assignment to a terminal :class:`WorkerResult`.

        ``max_seconds`` (a Nexus wall-clock cap, 0 = none) only ever shortens the
        configured timeout. Raises :class:`ConfigError`/:class:`ProviderError` for
        refusals and preflight failures; an asyncio cancellation kills the
        process group and re-raises.
        """
        if self._closed:
            raise ProviderError("antigravity worker is closed")
        if os.name != "posix":
            raise ProviderError("antigravity worker requires POSIX process groups")
        if self._config.worker_inherits_agy_settings is not True or policy.read_only or policy.tools is not None \
                or policy.mcp_config or policy.allowed_tools or policy.disallowed_tools or policy.restricted \
                or policy.setting_sources or policy.permission_mode not in ("auto", "accept-edits", "plan"):
            raise ConfigError("antigravity policy is not one resolve_policy() would produce; refusing to start")
        if not _MODEL_RE.fullmatch(model or ""):
            raise ConfigError("antigravity requires an exact model ID from `agy models`")
        if len(prompt.encode("utf-8", errors="replace")) > MAX_PROMPT_BYTES:
            raise ConfigError("antigravity prompt exceeds 64 KiB; refusing to truncate the assignment")
        if "\x00" in prompt:
            raise ConfigError("antigravity prompt contains a NUL byte")
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise ConfigError(f"antigravity workspace is not a directory: {workspace}")
        timeout = policy.timeout_seconds
        if max_seconds and max_seconds > 0:
            timeout = min(timeout, float(max_seconds))
        env = self._env()
        run = _Run(session=str(uuid4()), model=model, workspace=workspace, policy=policy, emit=emit)
        started = time.monotonic()
        process = None
        result: WorkerResult | None = None
        try:
            await self._preflight(policy, env, tempfile.gettempdir())
            argv = self.build_argv(policy, model=model, print_timeout=math.ceil(timeout) + PRINT_TIMEOUT_SLACK)
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(workspace), env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            self._active.add(process)
            await self._send(run, "external.started", {
                "provider": PROVIDER, "model": model, "session": run.session, "workspace": str(workspace),
                "permission_mode": policy.permission_mode, "tools": None, "allowed_tools": [],
                "disallowed_tools": [], "restricted": False, "setting_sources": "", "mcp": "agy-inherited",
                "read_only": False, "timeout_seconds": timeout, "provenance": PROVENANCE,
                "authority": policy.authority(workspace)})
            try:
                result = await self._supervise(run, process, prompt, started, timeout, cancel)
            except Exception as exc:  # unexpected: report it, `finally` cleans up
                reason = f"supervision failed unexpectedly ({type(exc).__name__}): {redact_secrets(str(exc))[:300]}"
                result = self._result(run, started, "failed", f"Antigravity {reason}.", error=reason)
        except asyncio.CancelledError:
            if process is not None:
                await self._stop(process)
            with suppress(Exception):
                await self._finish_event(run, "cancelled", None, started)
            raise
        finally:
            if process is not None:
                await self._stop(process)
                self._active.discard(process)
        assert result is not None
        with suppress(Exception):
            await self._finish_event(run, result.status, result, started)
        return result

    async def _finish_event(self, run: _Run, status: str, result: WorkerResult | None, started: float) -> None:
        await self._send(run, "external.finished", {
            "status": status, "provider": PROVIDER, "model": run.model, "session": run.session,
            "agy_session": run.agy_session, "stop_event": result.stop_event if result else None,
            "transcript_path": None, "tool_calls": run.tool_calls,
            "usage": run.usage if run.usage is not None else "unknown", "cost": "unknown",
            "agy_permission_mode": run.agy_permission_mode, "agy_tools": list(run.init_tools),
            "error": redact_secrets(result.error)[:500] if result and result.error else None,
            "attention": result.attention if result else None,
            "clip_notes": list(dict.fromkeys(run.clip_notes))[:20],
            "unknown_events": run.unknown_events, "malformed_lines": run.malformed_lines,
            "elapsed_seconds": round(time.monotonic() - started, 3), "provenance": PROVENANCE})

    def _result(self, run: _Run, started: float, status: str, text: str, *, error: str | None = None,
                stop: str | None = None, attention: str | None = None) -> WorkerResult:
        diag, _ = clip(redact_secrets(terminal_text(run.stderr)).strip(), MAX_DIAGNOSTIC_CHARS)
        text, note = clip(text, MAX_RESULT_CHARS)
        if note:
            run.clip_notes.append(f"answer {note}")
        if run.stdout_truncated:
            run.clip_notes.append("stdout limit reached; later activity was not recorded")
        if run.oversized_lines:
            run.clip_notes.append(f"{run.oversized_lines} over-long stream line(s) dropped")
        if run.malformed_lines:
            run.clip_notes.append(f"{run.malformed_lines} malformed stream line(s) ignored")
        notes = tuple(dict.fromkeys(run.clip_notes))
        if notes:
            text = (text + "\n" if text else "") + "\n".join(f"[{n}]" for n in notes)
        return WorkerResult(
            status=status, text=text, is_error=status != "completed", claude_session=run.session,
            policy=run.policy, error=error, stop_event=stop, transcript_path=None, tool_calls=run.tool_calls,
            clip_notes=notes, diagnostics=diag, elapsed_seconds=time.monotonic() - started, attention=attention)

    async def _supervise(self, run: _Run, process: asyncio.subprocess.Process, prompt: str,
                         started: float, timeout: float, cancel: object | None) -> WorkerResult:
        def partial() -> str:
            text = run.partial_text()
            return f"\n[partial answer]\n{text}" if text else ""

        def attention(reason: str) -> WorkerResult:
            return self._result(
                run, started, "needs_attention",
                f"Antigravity needs your attention and was stopped; Nexus approved nothing. {reason}." + partial(),
                error=reason, attention=reason)

        # One bounded stream-json user event, then EOF: Nexus writes nothing else, so no prompt is answered.
        try:
            process.stdin.write(self.build_input(prompt))
            await asyncio.wait_for(process.stdin.drain(), 10)
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
            pass
        out = asyncio.create_task(self._read_stdout(run, process.stdout))
        err = asyncio.create_task(self._read_stderr(run, process.stderr))
        deadline = started + timeout
        timed_out = cancelled = False
        process_code: int | None = None
        try:
            while True:
                if _is_cancelled(cancel):
                    cancelled = True
                    break
                done, _ = await asyncio.wait({out}, timeout=0.05)
                if done:
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
            failure = out.exception() if out.done() and not out.cancelled() else None
            if cancelled or timed_out or run.refusal or run.attention or run.final is None:
                await self._stop(process)  # no terminal event: nothing is left running
            else:
                try:
                    await asyncio.wait_for(process.wait(), EXIT_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    await self._stop(process)
                    process_code = None
                else:
                    process_code = process.returncode
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(err), 1.0)
        finally:
            for task in (out, err):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if failure is not None:
            raise failure
        if cancelled:
            return self._result(run, started, "cancelled", "Antigravity assignment was cancelled." + partial(),
                                error="cancelled")
        if run.refusal:
            return self._result(run, started, "failed", f"Antigravity run refused: {run.refusal}." + partial(),
                                error=run.refusal)
        if run.final is not None and run.final[0] == "completed":
            if process_code not in (0, None):
                err_text = f"agy exited with code {process_code} after reporting success; not trusted"
                return self._result(run, started, "failed", err_text + "." + partial(), error=err_text)
            return self._result(run, started, "completed", run.final[1], stop="result")
        stderr_text = redact_secrets(terminal_text(run.stderr))
        reason = run.attention or (None if run.final else _attention_for(stderr_text))
        if reason:
            return attention(reason)
        if run.final is not None:
            return self._result(run, started, "failed", f"{run.final[2]}." + partial(), error=run.final[2],
                                stop="result")
        if timed_out:
            return self._result(run, started, "interrupted",
                                f"Antigravity did not finish within {timeout:g}s and was stopped." + partial(),
                                error=f"deadline of {timeout:g}s exceeded without a terminal result")
        code = process.returncode
        error = (f"agy stream ended (exit code {code}) without a terminal result"
                 if not run.stdout_truncated else "agy stdout exceeded the size limit without a terminal result")
        return self._result(run, started, "failed", f"{error}; the work is not known to be complete." + partial(),
                            error=error)
