"""User tools: Python files in `~/.nexus10/tools/` and `<project>/.agents/tools/`.

```python
# ~/.nexus10/tools/hello.py
NAME = "hello"                       # optional; the file name by default
DESCRIPTION = "Greet someone."       # required
PARAMETERS = {"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]}
GUIDANCE = "Use to say hello."       # optional: one line for the system prompt
TIMEOUT = 30                         # optional seconds, at most 600 (default 60)

def run(args):                       # or `async def`; returns text
    return f"Hello, {args['who']}!"
```

The daemon never executes these files. Metadata is read with `ast.literal_eval`,
so a file with side effects at import time cannot run inside the daemon. A call
runs in a child Python process (isolated mode, scrubbed environment, the
session's directory as cwd, a timeout, killed as a process group). That is
process quarantine, not a sandbox: the tool can do what the user can do.

The session snapshot stores each tool's source as it was when the session
started (Principle 8, P6), so a running session keeps running the code it
started with. When the file on disk has changed since, the result says so.

Adapted from old Nexus `nexus/ext/quarantine.py` and `nexus/ext/manifest.py`
(much reduced: no manager, trash or templates).
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import home
from .tools.base import MAX_OUTPUT_CHARS, ToolError, clip

MAX_SOURCE_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")

_RUNNER = r"""
import asyncio, inspect, json, sys, traceback
payload = json.load(sys.stdin)
out, sys.stdout = sys.stdout, sys.stderr
try:
    ns = {"__name__": "nexus_user_tool", "__file__": payload["path"]}
    exec(compile(payload["source"], payload["path"], "exec"), ns)
    result = ns["run"](payload["args"])
    if inspect.isawaitable(result):
        async def wait():
            return await result
        result = asyncio.run(wait())
    if not isinstance(result, str):
        result = json.dumps(result, default=str, ensure_ascii=False)
    reply = {"ok": True, "result": result}
except BaseException as exc:
    traceback.print_exc()
    reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
out.write(json.dumps(reply))
out.flush()
"""


@dataclass(frozen=True, slots=True)
class UserTool:
    name: str
    description: str
    parameters: dict[str, Any]
    source: str
    path: str
    scope: str  # "global" | "project"
    sha256: str
    timeout: float = DEFAULT_TIMEOUT

    def record(self, *, source: bool = True) -> dict[str, Any]:
        """The snapshot form: the schema a request needs, and (with `source`) the frozen code."""
        info: dict[str, Any] = {"path": self.path, "scope": self.scope, "sha256": self.sha256, "timeout": self.timeout}
        if source:
            info["source"] = self.source
        return {"name": self.name, "description": self.description, "parameters": self.parameters,
                "extension": info}


@dataclass
class ExtensionSet:
    tools: dict[str, UserTool] = field(default_factory=dict)
    problems: list[dict[str, str]] = field(default_factory=list)  # {"path", "error"}


def directories(project_path: str | None) -> list[tuple[str, Path]]:
    dirs = [("global", home() / "tools")]
    if project_path:
        dirs.append(("project", Path(project_path) / ".agents" / "tools"))
    return dirs


def parse(path: Path, scope: str) -> UserTool:
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("file is too large")
    source = raw.decode("utf-8")
    try:
        tree = ast.parse(source, str(path))
    except SyntaxError as exc:
        raise ValueError(f"syntax error on line {exc.lineno}: {exc.msg}") from exc
    meta: dict[str, Any] = {}
    has_run = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "run":
            has_run = True
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            key, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            key, value = node.target.id, node.value
        else:
            continue
        if key in ("NAME", "DESCRIPTION", "PARAMETERS", "GUIDANCE", "TIMEOUT"):
            try:
                meta[key] = ast.literal_eval(value)
            except (ValueError, SyntaxError) as exc:
                raise ValueError(f"{key} must be a literal (no calls or names)") from exc
    if not has_run:
        raise ValueError("define a top-level `run(args)` function")
    name = meta.get("NAME", path.stem)
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("NAME must be lowercase letters, digits and '_', starting with a letter")
    description = meta.get("DESCRIPTION")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("DESCRIPTION is required")
    parameters = meta.get("PARAMETERS", {"type": "object", "properties": {}, "additionalProperties": False})
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ValueError('PARAMETERS must be a JSON schema object with "type": "object"')
    guidance = meta.get("GUIDANCE", "")
    if not isinstance(guidance, str):
        raise ValueError("GUIDANCE must be a string")  # noqa: TRY004 - a content error, reported like the others
    timeout = meta.get("TIMEOUT", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"TIMEOUT must be a number of seconds up to {int(MAX_TIMEOUT)}")
    description = description.strip() + (f"\n{guidance.strip()}" if guidance.strip() else "")
    return UserTool(name=name, description=description, parameters=parameters,
                    source=source, path=str(path), scope=scope, sha256=hashlib.sha256(raw).hexdigest(),
                    timeout=float(timeout))


def discover(project_path: str | None, reserved: frozenset[str] | set[str] = frozenset()) -> ExtensionSet:
    """Every valid user tool; the project's replaces the global one of the same name. A bad file is a problem."""
    result = ExtensionSet()
    for scope, directory in directories(project_path):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            try:
                tool = parse(path, scope)
                if tool.name in reserved or tool.name.startswith("mcp__"):
                    raise ValueError(f"{tool.name!r} is the name of a built-in tool")
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                result.problems.append({"path": str(path), "error": str(exc).splitlines()[0]})
                continue
            result.tools[tool.name] = tool
    return result


def fingerprint(project_paths: list[str]) -> str:
    stamps: list[tuple[str, int, int]] = []
    dirs = [d for s, d in directories(None) if s == "global"]
    dirs += [Path(p) / ".agents" / "tools" for p in sorted(set(project_paths))]
    for directory in dirs:
        try:
            for path in sorted(directory.glob("*.py")):
                stat = path.stat()
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            pass
    return hashlib.sha256(repr(stamps).encode()).hexdigest()[:16]


def changed_note(record: dict[str, Any]) -> str:
    """Words for the model when the file differs from the code this session loaded; empty if it matches."""
    info = record["extension"]
    try:
        current = hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest()
    except OSError:
        return f"[Nexus: {info['path']} no longer exists; this ran the version loaded when the session started.]"
    if current == info["sha256"]:
        return ""
    return (f"[Nexus: {info['path']} changed on disk after this session started; "
            "this ran the version loaded at the start. A new session uses the current file.]")


def _environment(workspace: Path, name: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in _SAFE_ENV if key in os.environ}
    env["NEXUS_WORKSPACE"] = str(workspace)
    env["NEXUS_TOOL"] = name
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def call(record: dict[str, Any], args: dict[str, Any], workspace: Path) -> str:
    """Run a stored tool record in a child process and return its text (or raise ToolError)."""
    info = record["extension"]
    payload = json.dumps({"source": info["source"], "path": info["path"], "args": args}).encode()
    timeout = float(info.get("timeout") or DEFAULT_TIMEOUT)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", _RUNNER, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=workspace, env=_environment(workspace, record["name"]),
            start_new_session=True)
    except OSError as exc:
        raise ToolError(f"{record['name']} could not start: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout)
    except TimeoutError:
        _kill(proc)
        await proc.wait()
        raise ToolError(f"{record['name']} timed out after {timeout:g}s and was killed") from None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    log = clip(stderr.decode("utf-8", "replace").strip(), 4_000)
    try:
        reply = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        suffix = f"\n{log}" if log else ""
        raise ToolError(f"{record['name']} exited with code {proc.returncode} and no result{suffix}") from None
    if not reply.get("ok"):
        suffix = f"\n{log}" if log else ""
        raise ToolError(f"{record['name']} failed: {reply.get('error', 'unknown error')}{suffix}")
    text = clip(str(reply["result"]), MAX_OUTPUT_CHARS)
    note = changed_note(record)
    return f"{text}\n{note}" if note else text


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
