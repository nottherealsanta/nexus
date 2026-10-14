"""External Claude Code worker: one interactive session per delegated assignment.

Contract (plans/in-progress/CLAUDE_CODE_ZMX_PLAN.md sections 3-5):

* Claude Code runs its OWN agent loop and its OWN tools inside a Nexus-owned
  PTY, with cwd set to the assigned workspace/worktree. Nexus does not translate
  Claude tool calls into Nexus tool calls: its dispatcher, permission engine and
  PathGuard do not mediate them. That boundary is stated in every result.
* Authority is an explicit policy (:class:`WorkerPolicy`) resolved from the
  ``[providers.<name>]`` section before launch. ``bypassPermissions``,
  ``--dangerously-skip-permissions`` are never passed; ``auto`` is passed only
  when the operator explicitly opts in (and never for a read-only role). Nexus never
  writes keystrokes into the terminal other than cursor-position replies; trust,
  authentication, onboarding and permission prompts end the assignment with a
  ``needs_attention`` result that tells the user what to run themselves.
* A role that Nexus treats as read-only only ever receives Claude's own
  read-only tool set (``--tools Read,Grep,Glob``). A configuration that would
  widen it is refused, never silently applied.
* Readback is structured: SessionStart/Stop/StopFailure command hooks (an
  isolated ``python -I`` script, no Nexus imports) write atomic result files, and
  the Claude JSONL transcript is read incrementally by byte offset for assistant
  text and tool activity. A quiet PTY or an exited process without a Stop result
  is never success. Terminal text is only a bounded, ANSI-stripped, redacted
  diagnostic.
* Everything is bounded: prompt (64 KiB), transcript reads, tool inputs/results,
  the returned answer, and the wall clock. One fresh session per assignment; no
  reuse, detach or reattach.

Requires POSIX and Claude Code >= 2.1.296. Events are reported through an
injected ``emit(type, data)`` coroutine, so this module (managers layer) never
imports the runtime, host or UI.
"""
from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import re
import shlex
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config.paths import nexus_home
from ..errors import ConfigError, ProviderError
from ..model.providers.opencode import build_child_env
from ..tools.names import canonical_tool_name
from ..util import redact_secrets
from .model import READ_ONLY_TOOLS

__all__ = [
    "ClaudeCodeWorker",
    "PROVENANCE",
    "READ_ONLY_CLAUDE_TOOLS",
    "TranscriptReader",
    "WorkerConfig",
    "WorkerPolicy",
    "WorkerResult",
    "changed_paths",
    "nexus_child_is_read_only",
    "permission_prompt",
    "private_working_directory",
    "resolve_policy",
    "snapshot_workspace",
    "startup_block",
    "terminal_text",
]

MIN_VERSION = (2, 1, 296)
#: argv has a much smaller platform limit than a pipe.
MAX_PROMPT_BYTES = 64 * 1024
MAX_DIAGNOSTIC_BYTES = 16 * 1024
#: Terminal diagnostics shown to people/parents (after strip + redaction).
MAX_DIAGNOSTIC_CHARS = 1500
MAX_RESULT_CHARS = 64_000
MAX_TOOL_CONTENT_CHARS = 8_000
MAX_INPUT_STRING_CHARS = 4_000
MAX_TRANSCRIPT_CHUNK = 1024 * 1024
MAX_TRANSCRIPT_LINE = 8 * 1024 * 1024
MAX_TRANSCRIPT_TOTAL = 32 * 1024 * 1024
MAX_HOOK_FILE = 2 * 1024 * 1024
#: How long to keep reading the transcript after the Stop hook (it may flush later).
RECONCILE_SECONDS = 2.0
#: Bound on each ``--version``/``--help`` preflight probe.
PROBE_TIMEOUT_SECONDS = 15.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_TIMEOUT_SECONDS = 600.0

#: A tool result Claude returns when its ``auto`` classifier refuses an action.
_AUTO_DENIAL_RE = re.compile(r"denied by the Claude Code auto mode classifier\. Reason: \[([^\]]{1,80})\]")
MAX_BLOCKED = 20

PROVENANCE = "Claude Code (own tools)"
PROVIDER = "claude-code"
READ_ONLY_CLAUDE_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")
#: An exact ID: ``claude-<family>-<version...>``. Aliases (``sonnet``, ``*-latest``) fail.
_MODEL_RE = re.compile(r"claude-[a-z]+-[0-9][a-z0-9.-]*")
#: Tools the worker can never serve: Nexus writes no answers into the terminal,
#: so a question menu would only stall the assignment until its deadline.
ALWAYS_DISALLOWED: tuple[str, ...] = ("AskUserQuestion",)
_PERMISSION_MODES = {
    "default": "manual", "manual": "manual", "acceptEdits": "acceptEdits",
    "plan": "plan", "dontAsk": "dontAsk", "auto": "auto",
}

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


def private_working_directory() -> Path:
    """Machine-state directory for hook scripts/settings; never repaired.

    The shared ``.nexus`` parent may be readable (older installations create it
    that way) but must not be writable by other users. The dedicated directory
    must be private, including when it already exists.
    """
    state = nexus_home().absolute()
    root = state / "claude-code"
    for directory, private in ((state, False), (root, True)):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)):
            raise ProviderError("claude-code requires an owned, non-symlink, private "
                                "~/.nexus/claude-code directory and a non-writable state parent; "
                                "refusing to change existing permissions")
    return root


# ---------------------------------------------------------------------------
# Terminal diagnostics (never a model response)
# ---------------------------------------------------------------------------


def terminal_text(output: bytes) -> str:
    """ANSI/OSC/control-stripped text of a terminal tail (not redacted)."""
    text = output.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?", "", text)
    text = re.sub(r"\x1b\[(\d*)C", lambda m: " " * min(int(m.group(1) or 1), 200), text)  # cursor-forward
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[78=>]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


_SQUASH_RE = re.compile(r"[\s│┃║|╎╏]+")


def _squash(output: bytes, prompt: str = "", echoes: Sequence[str] = ()) -> str:
    """Whitespace/border-free lowercase text, minus any echo of ``prompt``/``echoes``.

    The TUI echoes the assignment, wrapped and boxed, and renders Claude's own
    replies; neither may be mistaken for startup or permission text. Echo removal
    is best effort: Markdown rendering can change a reply's characters.
    """
    text = _SQUASH_RE.sub("", terminal_text(output)).lower()
    for source in (prompt, *echoes):
        echo = _SQUASH_RE.sub("", source).lower()
        if echo:
            text = text.replace(echo, "")
    return text


def startup_block(output: bytes, workspace: str | Path | None = None, prompt: str = "") -> str | None:
    """Classify a bounded terminal tail as a startup block, without exposing it.

    Ink inserts styling between words, so whitespace is removed before matching.
    The returned string names what the user must do manually.
    """
    text = _squash(output, prompt)
    if "yes,itrustthisfolder" in text and "accessingworkspace:" in text:
        where = shlex.quote(str(workspace)) if workspace else "<workspace>"
        return (f"workspace trust requires manual setup: run `cd {where} && claude` once, "
                "approve trust for that folder only, then exit and re-run the assignment")
    if "pleaserunclaudeauthlogin" in text or "notloggedin" in text:
        return "authentication required: run `claude auth login` manually"
    if "selectthetextstyle" in text or "choosethetextstyle" in text:
        return "interactive onboarding required: run `claude` once in a terminal and finish setup"
    if "invalidmodel" in text or "modelnotfound" in text:
        return "requested model rejected; no alias fallback was attempted"
    return None


_PROMPT_PHRASES = (
    "doyouwanttoproceed", "doyouwanttomakethisedit", "doyouwanttocreate",
    "doyouwanttooverwrite", "doyouwanttoallow", "wouldyouliketoproceed",
)


def permission_prompt(output: bytes, prompt: str = "", replies: Sequence[str] = ()) -> bool:
    """Whether the terminal tail shows a Claude approval menu (diagnostic only).

    Requires the question AND the numbered ``1. Yes`` choice, so assistant prose
    that merely quotes a question is not mistaken for a prompt; ``replies`` (the
    assistant text read from the transcript) are removed first, so a reply that
    contains both is not either. Matching is based on the current UI wording and
    is NOT verified against every Claude release.
    """
    text = _squash(output, prompt, replies)
    return "1.yes" in text and any(phrase in text for phrase in _PROMPT_PHRASES)


# ---------------------------------------------------------------------------
# Lifecycle hook (no Nexus imports, receives no credentials)
# ---------------------------------------------------------------------------

# Atomic rename means the poller never observes a half-written result. The file
# name is the event name; session_id must match the one Nexus assigned.
HOOK = r'''
import json, os, sys
limit = 8 * 1024 * 1024
try:
    raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit: raise ValueError()
    value = json.loads(raw)
    if value.get("session_id") != sys.argv[2]: raise ValueError()
    event = value.get("hook_event_name")
    if event not in ("SessionStart", "Stop", "StopFailure"): raise ValueError()
    out = {"event": event}
    for key in ("last_assistant_message", "error", "error_details", "reason", "stop_reason"):
        item = value.get(key)
        if isinstance(item, str): out[key] = item[:262144]
    path = value.get("transcript_path")
    if isinstance(path, str) and len(path) <= 4096: out["transcript_path"] = path
    data = json.dumps(out).encode()
    target = os.path.join(sys.argv[1], event + ".json")
    part = target + "." + str(os.getpid()) + ".part"
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle: handle.write(data)
    os.replace(part, target)
except Exception:
    sys.exit(1)
'''


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerConfig:
    """Operator configuration of the ``claude-code`` provider section."""

    executable: str = "claude"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    permission_mode: str = "dontAsk"
    tools: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    setting_sources: str = ""
    mcp_config: str | None = None
    restricted: bool = True
    #: Claude's config directory (transcripts live under ``projects/``); ``None``
    #: means ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``.
    config_dir: Path | None = None

    def __post_init__(self) -> None:
        timeout = self.timeout_seconds
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS):
            raise ConfigError("claude-code timeout_seconds must be between 0 and 3600")
        if self.permission_mode not in _PERMISSION_MODES:
            raise ConfigError(
                "claude-code worker_permission_mode must be default/manual, acceptEdits, plan, auto or dontAsk")
        if self.tools is not None and not self.tools:
            raise ConfigError("claude-code worker_tools must not be empty (omit it to use the default set)")

    @classmethod
    def from_section(cls, section: Any, *, config_dir: Path | None = None) -> WorkerConfig:
        def listed(name: str) -> tuple[str, ...] | None:
            value = getattr(section, name, None)
            return None if value is None else tuple(str(item) for item in value)

        timeout = getattr(section, "timeout_seconds", None)
        restricted = getattr(section, "worker_restricted", None)
        sources = getattr(section, "worker_setting_sources", None)
        return cls(
            executable=getattr(section, "executable", None) or "claude",
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
            permission_mode=getattr(section, "worker_permission_mode", None) or "dontAsk",
            tools=listed("worker_tools"),
            allowed_tools=listed("worker_allowed_tools") or (),
            disallowed_tools=listed("worker_disallowed_tools") or (),
            setting_sources="" if sources is None else sources,
            mcp_config=getattr(section, "worker_mcp_config", None),
            restricted=True if restricted is None else bool(restricted),
            config_dir=config_dir,
        )


@dataclass(frozen=True)
class WorkerPolicy:
    """The resolved, explicit external-execution authority of one assignment."""

    permission_mode: str
    tools: tuple[str, ...] | None
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    setting_sources: str
    mcp_config: str | None
    restricted: bool
    read_only: bool
    executable: str = "claude"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    config_dir: Path | None = None

    def authority(self, workspace: str | Path) -> str:
        """The model/user-facing statement of what ran and who mediated it."""
        tools = ",".join(self.tools) if self.tools is not None else "default set"
        detail = [f"mode {self.permission_mode}", f"tools {tools}"]
        if self.allowed_tools:
            detail.append("allowed " + ",".join(self.allowed_tools))
        if self.disallowed_tools:
            detail.append("disallowed " + ",".join(self.disallowed_tools))
        detail.append("restricted" if self.restricted else "not restricted")
        detail.append("settings " + (self.setting_sources or "none"))
        detail.append("MCP " + ("explicit config" if self.mcp_config else "none"))
        if self.read_only:
            detail.append("read-only role")
        return (f"Claude Code ran its own tools ({', '.join(detail)}) in {workspace}; "
                "Nexus permissions/PathGuard did not mediate them")


def nexus_child_is_read_only(tools: Sequence[str], *, flagged: bool = False) -> bool:
    """Whether a Nexus child holds no write, edit or shell capability.

    ``flagged`` is the role's structural read-only marker. Otherwise every
    granted tool must be one of the read-only roles' tools (or an MCP selector,
    which the worker never forwards); any other tool may write, so it is not.
    """
    if flagged:
        return True
    for name in tools:
        canonical = canonical_tool_name(name)
        if canonical in READ_ONLY_TOOLS or canonical in ("McpSearch", "McpCall") or canonical.startswith("mcp__"):
            continue
        return False
    return True


def _rule_tool(rule: str) -> str:
    return rule.split("(", 1)[0].strip()


def resolve_policy(config: WorkerConfig, *, read_only: bool) -> WorkerPolicy:
    """Resolve the authority for one assignment, refusing any widening.

    ``read_only`` is true when the Nexus child was granted no write, edit or
    shell tool. Claude then only gets its read-only tool set, in a mode that
    cannot prompt for or grant more; a configuration that would exceed it is
    refused with :class:`ConfigError`. :data:`ALWAYS_DISALLOWED` is always added
    to the disallowed tools, so it shows in the disclosed authority.
    """
    mode = _PERMISSION_MODES[config.permission_mode]
    tools = config.tools
    disallowed = config.disallowed_tools + tuple(
        name for name in ALWAYS_DISALLOWED if name not in config.disallowed_tools)
    if read_only:
        extra = [name for name in (tools or ()) if name not in READ_ONLY_CLAUDE_TOOLS]
        extra += [rule for rule in config.allowed_tools if _rule_tool(rule) not in READ_ONLY_CLAUDE_TOOLS]
        # --tools limits only built-ins: MCP servers, plugins and settings hooks
        # can still act, so neither may be loaded for a read-only role.
        if config.mcp_config:
            extra.append("worker_mcp_config servers")
        if config.setting_sources:
            extra.append(f"worker_setting_sources {config.setting_sources!r} (plugins/hooks)")
        if extra:
            raise ConfigError(
                "claude-code worker refused: this role is read-only in Nexus (no write, edit or "
                f"shell tool) but worker_tools/worker_allowed_tools would grant {', '.join(extra)}; "
                f"only {', '.join(READ_ONLY_CLAUDE_TOOLS)} can be enforced as read-only")
        tools = tools or READ_ONLY_CLAUDE_TOOLS
        if mode not in ("plan", "dontAsk"):
            mode = "dontAsk"  # narrows, never widens: nothing can prompt for more
    return WorkerPolicy(
        permission_mode=mode, tools=tuple(tools) if tools is not None else None,
        allowed_tools=config.allowed_tools, disallowed_tools=disallowed,
        setting_sources=config.setting_sources, mcp_config=config.mcp_config,
        restricted=config.restricted, read_only=read_only, executable=config.executable,
        timeout_seconds=float(config.timeout_seconds), config_dir=config.config_dir,
    )


# ---------------------------------------------------------------------------
# Transcript readback
# ---------------------------------------------------------------------------


def clip(text: str, limit: int) -> tuple[str, str | None]:
    """Clip ``text`` to ``limit`` characters with an announced note."""
    if len(text) <= limit:
        return text, None
    return text[:limit], f"clipped: showing the first {limit} of {len(text)} characters"


def _bound_input(value: Any, flags: list[str], depth: int = 0) -> Any:
    if isinstance(value, str):
        # Redact before clipping so a cut can never leave half a credential behind.
        text, note = clip(redact_secrets(value), MAX_INPUT_STRING_CHARS)
        if note:
            flags.append(note)
        return text
    if depth >= 6:
        flags.append("clipped: input nesting beyond 6 levels omitted")
        return "…"
    if isinstance(value, Mapping):
        items = list(value.items())
        if len(items) > 50:
            flags.append(f"clipped: {len(items) - 50} input keys omitted")
        return {redact_secrets(str(k))[:200]: _bound_input(v, flags, depth + 1) for k, v in items[:50]}
    if isinstance(value, (list, tuple)):
        if len(value) > 50:
            flags.append(f"clipped: {len(value) - 50} input items omitted")
        return [_bound_input(v, flags, depth + 1) for v in value[:50]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(str(value))[:200]


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, Mapping):
                parts.append(f"[{block.get('type', 'content')} omitted]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def parse_entry(entry: Mapping[str, Any], session: str) -> list[dict[str, Any]]:
    """Relevant items of one transcript entry; foreign/side-chain entries yield none."""
    if entry.get("sessionId") != session or entry.get("isSidechain"):
        return []
    kind = entry.get("type")
    message = entry.get("message")
    if kind not in ("assistant", "user") or not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    uuid = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
    items: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        if not isinstance(block, Mapping):
            continue
        btype = block.get("type")
        key = f"{uuid}:{index}" if uuid else None
        if kind == "assistant" and btype == "text" and isinstance(block.get("text"), str):
            if block["text"].strip():
                items.append({"kind": "text", "text": block["text"], "model": message.get("model"), "key": key})
        elif kind == "assistant" and btype == "tool_use" and isinstance(block.get("id"), str):
            name = block.get("name")
            items.append({"kind": "tool_use", "id": block["id"], "name": name if isinstance(name, str) else "",
                          "input": block.get("input") if isinstance(block.get("input"), Mapping) else {},
                          "key": key})
        elif kind == "user" and btype == "tool_result" and isinstance(block.get("tool_use_id"), str):
            items.append({"kind": "tool_result", "id": block["tool_use_id"],
                          "content": _result_text(block.get("content")),
                          "is_error": bool(block.get("is_error")), "key": key})
    return items


class TranscriptReader:
    """Incremental, bounded reader of one Claude JSONL transcript.

    Consumes only complete lines (a partial trailing line waits for its newline
    unless ``final``), reads at most :data:`MAX_TRANSCRIPT_CHUNK` per call and
    :data:`MAX_TRANSCRIPT_TOTAL` overall, drops lines over
    :data:`MAX_TRANSCRIPT_LINE`, and ignores entries of any other session id.
    """

    def __init__(self, session: str) -> None:
        self.session = session
        self.path: Path | None = None
        self.offset = 0
        self.total = 0
        self.truncated = False
        self.skipped_lines = 0
        self._pending = b""
        self._skipping = False
        self._seen: set[str] = set()

    def set_path(self, path: Path) -> None:
        """Follow ``path``; a changed path restarts the read (items are de-duplicated)."""
        if self.path == path:
            return
        self.path, self.offset, self._pending, self._skipping = path, 0, b"", False

    def _line(self, raw: bytes) -> list[dict[str, Any]]:
        try:
            entry = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(entry, Mapping):
            return []
        out = []
        for item in parse_entry(entry, self.session):
            key = item.pop("key", None)
            if key is None:
                key = f"{item['kind']}:{item.get('id', '')}:{hash(item.get('text', ''))}"
            if key in self._seen:
                continue
            if len(self._seen) < 200_000:
                self._seen.add(key)
            out.append(item)
        return out

    def poll(self, *, final: bool = False) -> list[dict[str, Any]]:
        path = self.path
        if path is None:
            return []
        items: list[dict[str, Any]] = []
        try:
            with path.open("rb") as handle:
                handle.seek(self.offset)
                while self.total < MAX_TRANSCRIPT_TOTAL:
                    chunk = handle.read(min(MAX_TRANSCRIPT_CHUNK, MAX_TRANSCRIPT_TOTAL - self.total))
                    if not chunk:
                        break
                    self.offset += len(chunk)
                    self.total += len(chunk)
                    self._pending += chunk
                    while (cut := self._pending.find(b"\n")) >= 0:
                        line, self._pending = self._pending[:cut], self._pending[cut + 1:]
                        if self._skipping:
                            self._skipping = False
                        elif line.strip():
                            items.extend(self._line(line))
                    if self._skipping:
                        self._pending = b""  # the rest of an over-long line
                    elif len(self._pending) > MAX_TRANSCRIPT_LINE:
                        self._pending, self._skipping = b"", True
                        self.skipped_lines += 1
        except OSError:
            return items
        if self.total >= MAX_TRANSCRIPT_TOTAL:
            self.truncated = True
        if final and self._pending.strip() and not self._skipping:
            items.extend(self._line(self._pending))
            self._pending = b""
        return items


def derive_transcript_path(config_dir: Path | None, workspace: Path, session: str) -> Path:
    """Where Claude Code is expected to write ``session``'s transcript.

    BEST EFFORT: this mirrors Claude's project-directory naming (every
    non-alphanumeric character of the real cwd becomes ``-``) as observed on
    2.1.x. It is not a documented contract, so the path given by the
    SessionStart/Stop hook always replaces it.
    """
    base = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace.resolve())) / f"{session}.jsonl"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerResult:
    """Terminal outcome of one external assignment."""

    #: completed | failed | interrupted | cancelled | needs_attention
    status: str
    text: str
    is_error: bool
    claude_session: str
    policy: WorkerPolicy
    error: str | None = None
    stop_event: str | None = None
    transcript_path: str | None = None
    tool_calls: int = 0
    clip_notes: tuple[str, ...] = ()
    #: ANSI-stripped, redacted, bounded terminal tail (diagnostic only).
    diagnostics: str = ""
    elapsed_seconds: float = 0.0
    attention: str | None = None


def _is_cancelled(cancel: object | None) -> bool:
    return bool(getattr(cancel, "cancelled", False)) if cancel is not None else False


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
    reader: TranscriptReader
    texts: list[str] = field(default_factory=list)
    tools: dict[str, str] = field(default_factory=dict)
    tool_calls: int = 0
    clip_notes: list[str] = field(default_factory=list)
    seen_assistant: bool = False
    #: Actions Claude's own ``auto`` classifier denied: ``"Tool: reason"``.
    blocked: list[str] = field(default_factory=list)
    tail: bytes = b""
    #: The assignment text, so its terminal echo is not read as Claude's own output.
    prompt: str = ""


class ClaudeCodeWorker:
    """Launch and supervise interactive Claude Code sessions (see module docs)."""

    def __init__(self, config: WorkerConfig, *, environ: Mapping[str, str] | None = None) -> None:
        self._config = config
        self._environ = environ
        self._active: set[asyncio.subprocess.Process] = set()
        self._closed = False

    @property
    def config(self) -> WorkerConfig:
        return self._config

    # -- process helpers -------------------------------------------------

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        # A reaped leader's pid (and so its group id) may already be reused.
        if process.returncode is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            await process.wait()

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.gather(*(self._stop(p) for p in tuple(self._active)))

    def _env(self) -> dict[str, str]:
        env = build_child_env(self._environ, inherit_env=("CLAUDE_CONFIG_DIR",))
        env.update(TERM="xterm-256color", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_AUTOUPDATER="1")
        return env

    async def _probe(self, executable: str, flag: str, env: dict, cwd: str) -> str:
        process = await asyncio.create_subprocess_exec(
            executable, flag, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        self._active.add(process)
        async def collect() -> str:
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > MAX_PROMPT_BYTES:
                    raise ProviderError("claude-code preflight output exceeds limit")
            if await process.wait() != 0:
                raise ProviderError("claude-code preflight failed")
            return output.decode("utf-8", errors="replace")

        try:
            return await asyncio.wait_for(collect(), PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise ProviderError(f"claude-code preflight timed out after {PROBE_TIMEOUT_SECONDS:g}s") from None
        finally:
            await self._stop(process)
            self._active.discard(process)

    async def _preflight(self, policy: WorkerPolicy, env: dict, cwd: str) -> None:
        version = await self._probe(policy.executable, "--version", env, cwd)
        match = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+) \(Claude Code\)\s*", version)
        if not match or tuple(map(int, match.groups())) < MIN_VERSION:
            raise ProviderError("claude-code requires Claude Code >= 2.1.296")
        help_text = await self._probe(policy.executable, "--help", env, cwd)
        required = ["--tools", "--strict-mcp-config", "--mcp-config", "--setting-sources", "--settings",
                    "--session-id", "--permission-mode", "--no-chrome", policy.permission_mode]
        if policy.restricted:
            required.append("--restricted")
        if policy.allowed_tools:
            required.append("--allowedTools")
        if policy.disallowed_tools:
            required.append("--disallowedTools")
        if any(item not in help_text for item in required):
            raise ProviderError("claude-code lacks required flags; refusing to start")

    # -- argv ------------------------------------------------------------

    @staticmethod
    def build_argv(policy: WorkerPolicy, *, model: str, session: str, settings: Path, prompt: str) -> list[str]:
        """The exact Claude argv. Never contains bypass flags or ``--tools ""``."""
        argv = [policy.executable, "--model", model, "--session-id", session,
                "--permission-mode", policy.permission_mode,
                "--setting-sources", policy.setting_sources, "--settings", str(settings),
                "--strict-mcp-config", "--mcp-config", policy.mcp_config or '{"mcpServers":{}}',
                "--no-chrome"]
        if not policy.setting_sources:
            argv.append("--disable-slash-commands")
        if policy.restricted:
            argv.append("--restricted")
        if policy.tools is not None:
            argv += ["--tools", ",".join(policy.tools)]
        if policy.allowed_tools:
            argv += ["--allowedTools", ",".join(policy.allowed_tools)]
        if policy.disallowed_tools:
            argv += ["--disallowedTools", ",".join(policy.disallowed_tools)]
        return [*argv, "--", prompt]

    # -- hook files ------------------------------------------------------

    @staticmethod
    def _read_hook(directory: Path, event: str) -> dict[str, Any] | None:
        path = directory / f"{event}.json"
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_HOOK_FILE + 1)
        except OSError:
            return None
        if len(raw) > MAX_HOOK_FILE:
            return {"event": event, "error": "hook result exceeds limit"}
        try:
            value = json.loads(raw)
        except ValueError:
            return {"event": event, "error": "malformed hook result"}
        return value if isinstance(value, dict) and value.get("event") == event else None

    @staticmethod
    def _hook_transcript(value: Mapping[str, Any], session: str) -> Path | None:
        raw = value.get("transcript_path")
        if not isinstance(raw, str):
            return None
        path = Path(raw)
        return path if path.is_absolute() and path.name == f"{session}.jsonl" else None

    # -- transcript -> events --------------------------------------------

    @staticmethod
    async def _send(run: _Run, event: str, data: dict[str, Any]) -> None:
        if run.emit is not None:
            await run.emit(event, data)

    async def _drain(self, run: _Run, *, final: bool = False) -> None:
        for item in run.reader.poll(final=final):
            kind = item["kind"]
            if kind == "text":
                run.seen_assistant = True
                text, note = clip(item["text"], MAX_RESULT_CHARS)
                if note:
                    run.clip_notes.append(f"assistant message {note}")
                run.texts.append(text)
                await self._send(run, "text", {
                    "text": text + (f"\n[{note}]" if note else ""),
                    "provider": PROVIDER, "model": item.get("model") or run.model,
                    "source": "claude-code-transcript"})
                await self._send(run, "model.stopped", {"stop_reason": "end_turn"})
            elif kind == "tool_use":
                run.seen_assistant = True
                run.tool_calls += 1
                run.tools[item["id"]] = item["name"]
                flags: list[str] = []
                bounded = _bound_input(item["input"], flags)
                if flags:
                    run.clip_notes.append(f"{item['name']} input {flags[0]}")
                await self._send(run, "external.tool", {
                    "phase": "requested", "call_id": item["id"], "tool": item["name"], "input": bounded,
                    **({"clip_note": "; ".join(dict.fromkeys(flags))} if flags else {}),
                    "provenance": PROVENANCE, "provider": PROVIDER, "session": run.session})
            elif kind == "tool_result":
                text, note = clip(redact_secrets(item["content"]), MAX_TOOL_CONTENT_CHARS)
                name = run.tools.get(item["id"], "")
                denial = _AUTO_DENIAL_RE.search(item["content"]) if item["is_error"] else None
                if denial and len(run.blocked) < MAX_BLOCKED:
                    run.blocked.append(f"{name or 'tool'}: {denial.group(1)}")
                if note:
                    run.clip_notes.append(f"{name or 'tool'} result {note}")
                await self._send(run, "external.tool", {
                    "phase": "completed", "call_id": item["id"], "tool": name,
                    "content": [{"type": "text", "text": text}], "is_error": item["is_error"],
                    **({"clip_note": note} if note else {}),
                    "provenance": PROVENANCE, "provider": PROVIDER, "session": run.session})

    async def _reconcile(self, run: _Run, hook_text: str | None) -> None:
        """Keep reading briefly after a hook: the transcript may flush later."""
        deadline = time.monotonic() + RECONCILE_SECONDS
        wanted = (hook_text or "").strip()
        while True:
            await self._drain(run)
            if not wanted or any(wanted == text.strip() for text in run.texts):
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        await self._drain(run, final=True)

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
        configured timeout. Claude-side iteration limits are deliberately not
        mapped. Raises :class:`ConfigError`/:class:`ProviderError` for refusals
        and preflight failures; an asyncio cancellation kills the process group
        and re-raises.
        """
        if self._closed:
            raise ProviderError("claude-code worker is closed")
        if os.name != "posix":
            raise ProviderError("claude-code requires a POSIX PTY")
        if not _MODEL_RE.fullmatch(model or ""):
            raise ConfigError("claude-code requires an exact Claude model ID, not an alias")
        if len(prompt.encode("utf-8", errors="replace")) > MAX_PROMPT_BYTES:
            raise ConfigError("claude-code argv prompt exceeds 64 KiB; refusing to truncate the assignment")
        if "\x00" in prompt:
            raise ConfigError("claude-code prompt contains a NUL byte")
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise ConfigError(f"claude-code workspace is not a directory: {workspace}")
        timeout = policy.timeout_seconds
        if max_seconds and max_seconds > 0:
            timeout = min(timeout, float(max_seconds))
        env = self._env()
        state_dir = private_working_directory()
        session = str(uuid4())
        process = None
        master = slave = None
        started = time.monotonic()
        run = _Run(session=session, model=model, workspace=workspace, policy=policy, emit=emit,
                   reader=TranscriptReader(session), prompt=prompt)
        result: WorkerResult | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="worker-", dir=state_dir) as directory:
                await self._preflight(policy, env, str(state_dir))
                root = Path(directory)
                hook = root / "hook.py"
                hook.write_text(HOOK)
                command = shlex.join([sys.executable, "-I", str(hook), str(root), session])
                settings = {
                    "enabledPlugins": {}, "enableAllProjectMcpServers": False, "autoMemoryEnabled": False,
                    "hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
                              for event in ("SessionStart", "Stop", "StopFailure")},
                }
                settings_path = root / "settings.json"
                settings_path.write_text(json.dumps(settings))
                argv = self.build_argv(policy, model=model, session=session, settings=settings_path,
                                       prompt=prompt)
                derived = derive_transcript_path(policy.config_dir, workspace, session)
                import fcntl
                import pty
                import termios
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                os.set_blocking(master, False)
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(workspace), env=env, stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True)
                self._active.add(process)
                os.close(slave)
                slave = None
                await self._send(run, "external.started", {
                    "provider": PROVIDER, "model": model, "session": session, "workspace": str(workspace),
                    "permission_mode": policy.permission_mode,
                    "tools": list(policy.tools) if policy.tools is not None else None,
                    "allowed_tools": list(policy.allowed_tools),
                    "disallowed_tools": list(policy.disallowed_tools),
                    "restricted": policy.restricted, "setting_sources": policy.setting_sources,
                    "mcp": "explicit" if policy.mcp_config else "none",
                    "read_only": policy.read_only, "timeout_seconds": timeout,
                    "provenance": PROVENANCE, "authority": policy.authority(workspace)})
                try:
                    result = await self._supervise(run, process, master, root, derived, started, timeout, cancel)
                except Exception as exc:  # unexpected: report it, and let `finally` clean up
                    reason = f"supervision failed unexpectedly ({type(exc).__name__}): {redact_secrets(str(exc))[:300]}"
                    result = self._result(run, started, "failed", f"Claude Code {reason}.", error=reason)
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
            for fd in (master, slave):
                if fd is not None:
                    with suppress(OSError):
                        os.close(fd)
        assert result is not None
        with suppress(Exception):
            await self._finish_event(run, result.status, result, started)
        return result

    async def _finish_event(self, run: _Run, status: str, result: WorkerResult | None, started: float) -> None:
        await self._send(run, "external.finished", {
            "status": status, "provider": PROVIDER, "model": run.model, "session": run.session,
            "stop_event": result.stop_event if result else None,
            "transcript_path": str(run.reader.path) if run.reader.path else None,
            "tool_calls": run.tool_calls, "usage": "unknown",
            "error": redact_secrets(result.error)[:500] if result and result.error else None,
            "attention": result.attention if result else None,
            "clip_notes": list(dict.fromkeys(run.clip_notes))[:20],
            "elapsed_seconds": round(time.monotonic() - started, 3), "provenance": PROVENANCE})

    def _result(self, run: _Run, started: float, status: str, text: str, *, error: str | None = None,
                stop: str | None = None, attention: str | None = None) -> WorkerResult:
        diag, _ = clip(redact_secrets(terminal_text(run.tail)).strip(), MAX_DIAGNOSTIC_CHARS)
        text, note = clip(text, MAX_RESULT_CHARS)
        if note:
            run.clip_notes.append(f"answer {note}")
        if run.reader.truncated:
            run.clip_notes.append("transcript read limit reached; later activity was not recorded")
        notes = tuple(dict.fromkeys(run.clip_notes))
        if notes:  # clipping is always announced
            text = (text + "\n" if text else "") + "\n".join(f"[{n}]" for n in notes)
        path = run.reader.path
        return WorkerResult(
            status=status, text=text, is_error=status != "completed", claude_session=run.session,
            policy=run.policy, error=error, stop_event=stop,
            transcript_path=str(path) if path else None, tool_calls=run.tool_calls,
            clip_notes=notes, diagnostics=diag, elapsed_seconds=time.monotonic() - started,
            attention=attention or self._blocked_attention(run))

    @staticmethod
    def _blocked_attention(run: _Run) -> str | None:
        if not run.blocked:
            return None
        return ("Claude's auto-mode classifier denied: " + "; ".join(dict.fromkeys(run.blocked))
                + ". Nexus approved nothing")

    async def _supervise(
        self, run: _Run, process: asyncio.subprocess.Process, master: int, root: Path,
        derived: Path, started: float, timeout: float, cancel: object | None,
    ) -> WorkerResult:
        session, policy = run.session, run.policy
        pending = b""
        deadline = started + timeout
        confirm_exit = False

        def partial() -> str:
            return f"\n[partial answer]\n{run.texts[-1]}" if run.texts else ""

        def attention(reason: str) -> WorkerResult:
            # No `claude --resume` hint: a startup block never created a session,
            # and resuming outside Nexus would drop the worker's policy flags.
            text = (f"Claude Code needs your attention and was stopped; Nexus approved nothing. {reason}."
                    + partial())
            return self._result(run, started, "needs_attention", text, error=reason, attention=reason)

        while True:
            if _is_cancelled(cancel):
                await self._drain(run, final=True)
                return self._result(run, started, "cancelled",
                                    "Claude Code assignment was cancelled." + partial(), error="cancelled")
            try:
                chunk = os.read(master, 65536)
            except BlockingIOError:
                chunk = b""
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                chunk = b""
            run.tail = (run.tail + chunk)[-MAX_DIAGNOSTIC_BYTES:]
            pending += chunk
            # Ink queries the cursor position. Answering that is the ONLY input
            # Nexus ever writes: never approvals, trust, auth or confirmations.
            for query, reply in ((b"\x1b[6n", b"\x1b[1;1R"), (b"\x1b[?6n", b"\x1b[?1;1R")):
                while query in pending:
                    os.write(master, reply)
                    pending = pending.replace(query, b"", 1)
            pending = pending[-8:]

            start = self._read_hook(root, "SessionStart")
            if start is not None:
                hooked = self._hook_transcript(start, session)
                if hooked is not None:
                    run.reader.set_path(hooked)
            if run.reader.path is None and derived.exists():
                run.reader.set_path(derived)
            await self._drain(run)

            stop = self._read_hook(root, "Stop")
            failure = None if stop is not None else self._read_hook(root, "StopFailure")
            value = stop if stop is not None else failure
            if value is not None:
                hooked = self._hook_transcript(value, session)
                if hooked is not None:
                    run.reader.set_path(hooked)
                message = value.get("last_assistant_message")
                message = message if isinstance(message, str) and message.strip() else None
                await self._reconcile(run, message)
                if message and not any(message.strip() == t.strip() for t in run.texts):
                    # The transcript never showed the final answer; record it from the hook.
                    text, _ = clip(message, MAX_RESULT_CHARS)
                    run.texts.append(text)
                    await self._send(run, "text", {"text": text, "provider": PROVIDER, "model": run.model,
                                                    "source": "claude-code-hook"})
                    await self._send(run, "model.stopped", {"stop_reason": "end_turn"})
                if stop is not None:
                    answer = message or (run.texts[-1] if run.texts else "")
                    return self._result(run, started, "completed",
                                        answer or "(Claude Code finished without a text answer)", stop="Stop")
                detail = value.get("error") or value.get("error_details") or value.get("reason") or ""
                detail = redact_secrets(str(detail))[:500]
                head = "Claude Code reported a turn failure (StopFailure)" + (f": {detail}" if detail else "")
                body = head + "." + (f"\n[partial answer]\n{message}" if message else partial())
                return self._result(run, started, "failed", body, error=head, stop="StopFailure")

            # Checked after the hooks, so a Stop that lands at the deadline still counts.
            if time.monotonic() >= deadline:
                await self._drain(run, final=True)
                return self._result(
                    run, started, "interrupted",
                    f"Claude Code did not finish within {timeout:g}s and was stopped." + partial(),
                    error=f"deadline of {timeout:g}s exceeded without a Stop result")

            # A started session has passed trust/login/onboarding, so only an
            # unstarted one can be blocked there.
            if not run.seen_assistant and start is None:
                block = startup_block(run.tail, run.workspace, run.prompt)
                if block:
                    return attention(block)
            if policy.permission_mode != "dontAsk" and permission_prompt(run.tail[-4096:], run.prompt, run.texts):
                return attention(
                    f"a Claude permission prompt is waiting in the worker terminal (mode {policy.permission_mode}); "
                    "the worker was stopped unanswered; grant the tool via worker_allowed_tools "
                    "(or use mode acceptEdits) and re-run the assignment")

            if process.returncode is not None:
                if not confirm_exit:
                    confirm_exit = True  # re-check hooks once: Stop may land just before exit
                    await asyncio.sleep(0.1)
                    continue
                await self._drain(run, final=True)
                code = process.returncode
                diag, _ = clip(redact_secrets(terminal_text(run.tail)).strip(), MAX_DIAGNOSTIC_CHARS)
                body = f"Claude Code exited (code {code}) without a Stop result; the work is not known to be complete."
                if diag:
                    body += f"\n[terminal tail, sanitized]\n{diag}"
                return self._result(run, started, "failed", body + partial(),
                                    error=f"exited with code {code} without a Stop result")
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Independent file-change verification (never taken from the model's claims)
# ---------------------------------------------------------------------------

MAX_SNAPSHOT_FILES = 20_000
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache"})


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def snapshot_workspace(workspace: Path, since: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A bounded fingerprint of the workspace's changed/visible files (blocking).

    In a git checkout this is ``git status --porcelain`` plus each listed file's
    mtime and size, so a file that was already dirty and is edited again still
    differs, and the ``HEAD`` commit. When ``since`` (the earlier snapshot) has a
    different ``HEAD``, the files the new commits touched are recorded under
    ``committed``, so work that was edited and committed is still reported.
    Gitignored files are not checked. Outside git it is an mtime/size walk
    capped at :data:`MAX_SNAPSHOT_FILES`. Paths are relative to ``workspace``.
    """
    workspace = Path(workspace).resolve()
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        top = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True, timeout=15, env=env, check=True).stdout.decode().strip()
        raw = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True, timeout=30, env=env, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return {"kind": "mtime", "files": _walk(workspace)}
    root = Path(top).resolve()
    head = _git_head(workspace, env)
    committed: list[str] = []
    old_head = since.get("head") if since is not None and since.get("kind") == "git" else None
    if head and old_head and head != old_head:
        committed = _committed_paths(workspace, root, str(old_head), head, env)
    files: dict[str, Any] = {}
    parts = raw.decode("utf-8", errors="replace").split("\0")
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        code, name = entry[:2], entry[3:]
        if code[0] in "RC":
            index += 1  # the rename/copy source follows as its own token
        full = root / name
        try:
            relative = str(full.relative_to(workspace))
        except ValueError:
            continue  # outside the assigned workspace
        files[relative] = (code, _stat_key(full))
        if len(files) >= MAX_SNAPSHOT_FILES:
            break
    return {"kind": "git", "head": head, "files": files, "committed": committed}


def _git_head(workspace: Path, env: Mapping[str, str]) -> str | None:
    """The ``HEAD`` commit, or ``None`` (unborn branch, git failure)."""
    try:
        out = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--verify", "-q", "HEAD"],
                             capture_output=True, timeout=15, env=env, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.decode().strip() or None


def _committed_paths(workspace: Path, root: Path, old: str, new: str, env: Mapping[str, str]) -> list[str]:
    """Workspace-relative paths that differ between commits ``old`` and ``new`` (bounded)."""
    try:
        raw = subprocess.run(
            ["git", "-C", str(workspace), "diff", "--name-only", "-z", "--no-renames", old, new],
            capture_output=True, timeout=30, env=env, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    paths: list[str] = []
    for name in raw.decode("utf-8", errors="replace").split("\0"):
        if not name:
            continue
        try:
            paths.append(str((root / name).relative_to(workspace)))
        except ValueError:
            continue  # outside the assigned workspace
        if len(paths) >= MAX_SNAPSHOT_FILES:
            break
    return paths


def _walk(workspace: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for current, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            path = Path(current) / name
            files[str(path.relative_to(workspace))] = _stat_key(path)
            if len(files) >= MAX_SNAPSHOT_FILES:
                return files
    return files


def changed_paths(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Sorted paths whose fingerprint differs between two snapshots, plus paths
    touched by commits made between them (``after["committed"]``)."""
    old, new = before.get("files", {}), after.get("files", {})
    committed = set(after.get("committed") or ())
    if before.get("kind") != after.get("kind"):
        return sorted(set(old) | set(new) | committed)
    return sorted({path for path in set(old) | set(new) if old.get(path) != new.get(path)} | committed)
