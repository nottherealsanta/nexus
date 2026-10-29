"""Command hooks (PLAN.md H).

Adapted from old Nexus nexus/hooks/manager.py and model.py, cut down to command
hooks configured as `[[hooks.<Event>]]` in config.toml.

A hook is argv run with no shell (unless `shell = true`), with the invocation as
JSON on stdin and a fixed safe environment. Exit 0 allows. Its stdout may be one
JSON object:

    {"decision": "allow|warn|block|modify", "message": "...", "tool_input": {...}, "context": "..."}

A non-zero exit, a timeout or a crash is handled by `on_nonzero` (`warn` by
default; `block` or `ignore`). Hooks are policy, not permission: a `modify`
result is validated and approved again by the caller.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .permissions import Rule, matches, parse_rule

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "TurnEnd", "SessionEnd")
DECISIONS = ("allow", "warn", "block", "modify")
ON_NONZERO = ("warn", "block", "ignore")
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 600.0
MAX_STDIN = 256 * 1024
MAX_STDOUT = 64 * 1024
MAX_HOOKS = 64
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "USER", "SHELL")


@dataclass(frozen=True, slots=True)
class Hook:
    event: str
    name: str
    command: tuple[str, ...]
    shell: bool = False
    matcher: Rule | None = None
    on_nonzero: str = "warn"
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True, slots=True)
class HookOutcome:
    hook: str
    event: str
    decision: str  # allow | warn | block | modify | ignored
    message: str = ""
    tool_input: dict[str, Any] | None = None
    context: str = ""
    error: str = ""
    seconds: float = 0.0


def parse_hooks(value: Any) -> tuple[list[Hook], list[str]]:
    """Hooks in file order and a problem per rejected entry; good entries still apply."""
    hooks: list[Hook] = []
    problems: list[str] = []
    if not isinstance(value, dict):
        return hooks, ["hooks: expected a table"]
    for event, entries in value.items():
        if event not in EVENTS:
            problems.append(f"hooks.{event}: unknown event (one of {', '.join(EVENTS)})")
            continue
        if not isinstance(entries, list):
            problems.append(f"hooks.{event}: expected an array of tables")
            continue
        for index, entry in enumerate(entries):
            where = f"hooks.{event}[{index}]"
            try:
                if len(hooks) >= MAX_HOOKS:
                    raise ValueError(f"more than {MAX_HOOKS} hooks")
                hooks.append(_parse_one(event, entry))
            except (ValueError, TypeError) as exc:
                problems.append(f"{where}: {exc}")
    return hooks, problems


def _parse_one(event: str, entry: Any) -> Hook:
    if not isinstance(entry, dict):
        raise TypeError("expected a table")
    if entry.get("type", "command") != "command":
        raise ValueError("only type = \"command\" is supported")
    shell = entry.get("shell", False)
    if not isinstance(shell, bool):
        raise TypeError("shell must be true or false")
    command = entry.get("command")
    if shell:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("with shell = true, command must be a string")
        argv: tuple[str, ...] = (command,)
    else:
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command) or not command[0]:
            raise ValueError("command must be a non-empty list of strings (argv, no shell)")
        argv = tuple(command)
    name = entry.get("name") or Path(argv[0]).name
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("name must be short text")
    on_nonzero = entry.get("on_nonzero", "warn")
    if on_nonzero not in ON_NONZERO:
        raise ValueError(f"on_nonzero must be one of {', '.join(ON_NONZERO)}")
    timeout = entry.get("timeout_s", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"timeout_s must be above 0 and at most {int(MAX_TIMEOUT)}")
    matcher = None
    text = entry.get("matcher")
    if text is not None:
        if event not in ("PreToolUse", "PostToolUse"):
            raise ValueError("matcher applies to PreToolUse and PostToolUse only")
        tool, pattern = parse_rule(text)
        matcher = Rule(tool, pattern, "deny")  # any-key matching, as for deny rules
    return Hook(event, name.strip(), argv, shell, matcher, on_nonzero, float(timeout))


def _environment(workspace: Path, event: str, payload: dict[str, Any]) -> dict[str, str]:
    env = {key: os.environ[key] for key in _SAFE_ENV if key in os.environ}
    env.update({"NEXUS_HOOK_EVENT": event, "NEXUS_WORKSPACE": str(workspace), "NEXUS_PROJECT_ROOT": str(workspace)})
    for key, name in (("session_id", "NEXUS_SESSION_ID"), ("turn_id", "NEXUS_TURN_ID"), ("tool_name", "NEXUS_TOOL_NAME")):
        if isinstance(payload.get(key), str):
            env[name] = payload[key]
    return env


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    data = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(data), False
        data += chunk
        if len(data) > limit:
            return bytes(data[:limit]), True


class HookRunner:
    def __init__(self, hooks: list[Hook], workspace: Path, *, session_id: str | None = None) -> None:
        self.hooks = hooks
        self.workspace = Path(workspace)
        self.session_id = session_id

    def has(self, event: str) -> bool:
        return any(h.event == event for h in self.hooks)

    def selected(self, event: str, tool: str | None = None, args: dict[str, Any] | None = None) -> list[Hook]:
        from .permissions import call_keys
        keys = call_keys(tool, args or {}, self.workspace) if tool else None
        return [h for h in self.hooks if h.event == event
                and (h.matcher is None or (tool is not None and matches(h.matcher, tool, keys)))]

    async def fire(self, event: str, *, tool: str | None = None, args: dict[str, Any] | None = None,
                   turn_id: str | None = None, **extra: Any) -> list[HookOutcome]:
        """Run the matching hooks in order. A `modify` feeds its input to the hooks after it; a `block` stops the rest."""
        outcomes: list[HookOutcome] = []
        current = args
        for hook in self.selected(event, tool, args):
            payload: dict[str, Any] = {"event": event, "session_id": self.session_id, "turn_id": turn_id,
                                       "cwd": str(self.workspace), **extra}
            if tool is not None:
                payload.update(tool_name=tool, tool_input=current)
            outcome = await self._run(hook, payload)
            outcomes.append(outcome)
            if outcome.decision == "modify" and outcome.tool_input is not None:
                current = outcome.tool_input
            elif outcome.decision == "block":
                break
        return outcomes

    async def _run(self, hook: Hook, payload: dict[str, Any]) -> HookOutcome:
        started = time.monotonic()

        def failed(error: str) -> HookOutcome:
            decision = {"block": "block", "warn": "warn", "ignore": "ignored"}[hook.on_nonzero]
            return HookOutcome(hook.name, hook.event, decision, message=error if decision != "ignored" else "",
                               error=error, seconds=time.monotonic() - started)

        body = json.dumps(payload, default=str).encode()
        if len(body) > MAX_STDIN:
            return failed(f"hook input is {len(body)} bytes, over the {MAX_STDIN} limit")
        try:
            if hook.shell:
                process = await asyncio.create_subprocess_shell(
                    hook.command[0], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, cwd=self.workspace, start_new_session=True,
                    env=_environment(self.workspace, hook.event, payload))
            else:
                process = await asyncio.create_subprocess_exec(
                    *hook.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, cwd=self.workspace, start_new_session=True,
                    env=_environment(self.workspace, hook.event, payload))
        except (OSError, ValueError) as exc:
            return failed(f"could not start: {exc}")
        assert process.stdin is not None and process.stdout is not None

        async def talk() -> tuple[bytes, bool, int]:
            try:
                process.stdin.write(body)  # type: ignore[union-attr]
                await process.stdin.drain()  # type: ignore[union-attr]
                process.stdin.close()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError):
                pass  # the hook did not read its input; its exit status still decides
            out, clipped = await _read_limited(process.stdout, MAX_STDOUT)  # type: ignore[arg-type]
            if clipped:
                _kill(process)
            return out, clipped, await process.wait()

        try:
            out, clipped, code = await asyncio.wait_for(talk(), hook.timeout)
        except TimeoutError:
            _kill(process)
            await process.wait()
            return failed(f"timed out after {hook.timeout:g}s")
        except BaseException:
            _kill(process)
            raise
        if clipped:
            return failed(f"output over {MAX_STDOUT} bytes")
        if code != 0:
            return failed(f"exited with status {code}")
        return self._decode(hook, out, time.monotonic() - started)

    @staticmethod
    def _decode(hook: Hook, out: bytes, seconds: float) -> HookOutcome:
        text = out.decode("utf-8", "replace").strip()
        if not text:
            return HookOutcome(hook.name, hook.event, "allow", seconds=seconds)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return HookOutcome(hook.name, hook.event, "warn" if hook.on_nonzero != "ignore" else "ignored",
                               message="" if hook.on_nonzero == "ignore" else "output was not JSON",
                               error="output was not JSON", seconds=seconds)
        if not isinstance(data, dict):
            return HookOutcome(hook.name, hook.event, "warn", message="output was not a JSON object",
                               error="output was not a JSON object", seconds=seconds)
        decision = data.get("decision", "allow")
        if decision not in DECISIONS:
            return HookOutcome(hook.name, hook.event, "warn", message=f"unknown decision {decision!r}",
                               error=f"unknown decision {decision!r}", seconds=seconds)
        message = data.get("message", "")
        context = data.get("context", "")
        new_input = data.get("tool_input")
        if not isinstance(message, str) or not isinstance(context, str):
            return HookOutcome(hook.name, hook.event, "warn", message="message and context must be text",
                               error="bad fields", seconds=seconds)
        if decision == "modify" and (hook.event != "PreToolUse" or not isinstance(new_input, dict)):
            return HookOutcome(hook.name, hook.event, "warn",
                               message="modify needs PreToolUse and a tool_input object", error="bad modify",
                               seconds=seconds)
        return HookOutcome(hook.name, hook.event, decision, message=message[:MAX_STDOUT],
                           tool_input=new_input if decision == "modify" else None,
                           context=context[:MAX_STDOUT], seconds=seconds)


def config_hooks() -> tuple[list[Hook], list[str]]:
    """`[[hooks.<Event>]]` from the config file; entries that do not parse are skipped and reported."""
    from .config import ConfigFile, _plain
    _, doc, _ = ConfigFile()._read()
    table = doc.get("hooks", {})
    plain = _plain(table) if isinstance(table, dict) else None
    return parse_hooks(plain if plain is not None else {})
