"""Validate a hot-loaded extension file before it is staged and imported.

Plan section 6.4. Quarantine is the gate between "a ``.py`` file appeared on
disk" and "the harness will execute it". It is deliberately paranoid about the
*bytes it validated*: a file that is edited between validation and execution is
refused, not raced, so the code that runs is exactly the code that passed.

The stages, in order, each a distinct refusal code:

1. :meth:`Quarantine.open` -- resolve the path and read it under a **bounded**
   reader (a regular file, no symlink components, under the size cap). The read
   is the single source of truth: size, UTF-8 validity, and ``sha256`` are all
   derived from those exact bytes, and nothing is read again.
2. :meth:`Quarantine.inspect` -- static analysis of that frozen source:
   ``ast.parse`` for syntax, an import-time/dangerous-call scan for warnings,
   and extraction of the declared ``SPEC`` / ``register()`` symbol shape.
3. :meth:`Quarantine.run_isolated` -- import the module in a **separate,
   minimal-environment subprocess** with a hard wall-clock timeout. Import-time
   hangs and ``os._exit`` crashes kill the child, never the harness. The child
   serializes back only JSON-safe metadata: the extracted spec and the names of
   its tools.
4. :meth:`Quarantine.verify` / :meth:`Quarantine.finalize` -- after the
   orchestrator has staged a private, content-addressed copy of the source,
   re-read the staged bytes and require them to hash to the same digest before
   the in-process import is allowed.

Boundary (stated plainly, per plan section 11)
---------------------------------------------
Quarantine catches **syntax errors, import crashes, import-time hangs, and
obviously dangerous side effects**. It does not sandbox arbitrary Python. A
staged extension is trusted code, exactly as ``nexus.toml`` and ``SOUL.md`` are;
the permission engine gates *calls*, not *loading*. ``SPEC``-bearing modules are
never asked to execute ``run`` here -- only to import and declare.

This module is manager-layer (L3). It imports ``nexus.tools.spec`` for the
contract types (L1 contracts) but never the manager, runtime, session, or core
loop.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..errors import ExtensionError
from ..tools.spec import NAME_PATTERN, ToolSpec, ToolSpecError

__all__ = [
    "CHILD_BOOTSTRAP",
    "DEFAULT_TIMEOUT_S",
    "EXIT_DECLARED",
    "EXIT_NO_SPEC",
    "EXIT_OK",
    "MAX_OUTPUT_BYTES",
    "SOURCE_READ_CHUNK",
    "ExtCandidate",
    "Quarantine",
    "QuarantineCode",
    "QuarantineError",
    "QuarantineOutcome",
    "QuarantineResult",
    "StagedSource",
    "SubprocessOutcome",
    "sanitize_text",
]

#: Default wall-clock budget for the isolated import.
DEFAULT_TIMEOUT_S = 10.0
#: Hard cap on how many bytes of the child's stdout/stderr are retained.
MAX_OUTPUT_BYTES = 64 * 1024
#: Read the source in bounded chunks; the cap makes the read O(1) in file size.
SOURCE_READ_CHUNK = 64 * 1024
#: Bytes an extension may not exceed, when the caller does not override it.
DEFAULT_MAX_FILE_BYTES = 262_144

#: Child exit codes beyond 0/1 that the protocol distinguishes.
EXIT_OK = 0
EXIT_NO_SPEC = 3
EXIT_DECLARED = 4

#: The default environment handed to the isolated importer. A deliberate
#: allow-list: nothing about the invoking process (credentials, config
#: redirections, ``PYTHONPATH``, ``PYTHONSTARTUP``, virtualenv shims) crosses
#: the boundary. Only the base ``PATH`` and a locale are kept so that a module
#: doing trivial environment lookups at import still behaves.
_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL")


class QuarantineCode(StrEnum):
    """Why a candidate extension was refused or warned about."""

    OK = "ok"
    MISSING = "missing"
    NOT_A_FILE = "not_a_file"
    SYMLINK = "symlink"
    OVERSIZE = "oversize"
    NOT_UTF8 = "not_utf8"
    NUL_BYTE = "nul_byte"
    SYNTAX_ERROR = "syntax_error"
    NO_CONTRACT = "no_contract"
    AMBIGUOUS_CONTRACT = "ambiguous_contract"
    BAD_SPEC = "bad_spec"
    SYNC_RUN = "sync_run"
    BAD_RUN = "bad_run"
    BAD_REGISTER = "bad_register"
    REGISTER_EMPTY = "register_empty"
    REGISTER_NOT_CALLABLE = "register_not_callable"
    DUPLICATE_TOOL = "duplicate_tool"
    BAD_TOOL_NAME = "bad_tool_name"
    BAD_SCHEMA = "bad_schema"
    CASE_COLLISION = "case_collision"
    BUILTIN_COLLISION = "builtin_collision"
    IMPORT_ERROR = "import_error"
    IMPORT_EXIT = "import_exit"
    IMPORT_TIMEOUT = "import_timeout"
    OUTPUT_OVERSIZE = "output_oversize"
    BAD_RESULT = "bad_result"
    SPEC_MISMATCH = "spec_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    STAGE_MISSING = "stage_missing"


class QuarantineError(ExtensionError):
    """A candidate could not be quarantined (I/O or protocol failure)."""


class _Refusal(QuarantineError):
    """Internal carrier so a stage can raise a code-carrying refusal."""

    def __init__(self, code: QuarantineCode, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

#: Credential shapes that must never survive into a diagnostic. Mirrors the
#: provider-error redaction rules (``nexus.model.http``) so one policy applies
#: to every place text can leak.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 ***"),
    (re.compile(r"\bsk-[A-Za-z0-9._-]{4,}"), "***"),
    (re.compile(r"\b(?:pk|rk)_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), "***"),
    (re.compile(r"\bAIza[A-Za-z0-9._-]{8,}"), "***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{6,}"), "***"),
    (re.compile(r"\bAKIA[A-Z0-9]{12,}"), "***"),
    (
        re.compile(
            r"(?i)(\b(?:api[-_]?key|x-api-key|authorization|access[-_]?token|"
            r"refresh[-_]?token|client[-_]?secret|secret|password|token)\b"
            r"\s*[:=]\s*[\"']?)[^\s\"',}]{4,}"
        ),
        r"\1***",
    ),
    (re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b"), "***"),
)

#: Exception types whose *message* is inherently a path/identifier and safe to
#: echo; everything else is shown as a type name only. This is the rule that
#: makes "no source body in diagnostics" structural rather than a convention:
#: a ``SyntaxError``'s line text could be an edited credential, so it is never
#: included.
_MESSAGE_SAFE_TYPES = frozenset(
    {
        "ModuleNotFoundError",
        "ImportError",
        "AttributeError",
        "QuarantineError",
        # ToolSpecError messages describe the declaration, not the file body.
        "ToolSpecError",
    }
)


def sanitize_text(value: object, *, limit: int = 300) -> str:
    """Return a bounded, control-free, credential-free single line.

    Control characters (including newlines and ``DEL``) become spaces, secret
    shapes are redacted, whitespace is collapsed, and the result is truncated
    to ``limit`` characters. Used for every piece of text that leaves this
    module's diagnostics.
    """
    raw = str(value)
    cleaned = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in raw)
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\u2026"
    return cleaned


def _safe_error(exc: BaseException, *, limit: int = 300) -> str:
    """A diagnostic string for an exception without its source body.

    Only the exception type and, for a small allow-list of types whose messages
    describe the *declaration* rather than the file, the sanitized message.
    """
    name = type(exc).__name__
    message = getattr(exc, "msg", None) if isinstance(exc, SyntaxError) else None
    if isinstance(exc, SyntaxError):
        # SyntaxError.msg is the compiler's own one-liner ("invalid syntax",
        # "unexpected indent"); the offending *line* is deliberately dropped.
        detail = sanitize_text(message or "invalid syntax", limit=200)
        return f"{name}: {detail} (line {exc.lineno or '?'})"
    if name in _MESSAGE_SAFE_TYPES:
        detail = sanitize_text(str(exc), limit=limit)
        if detail:
            return f"{name}: {detail}"
    return name


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtCandidate:
    """A discovered ``.py`` extension file and where it came from."""

    path: Path
    origin: str = "ext"
    source_id: str | None = None

    def identity(self) -> str:
        """A stable identity for the module name (never the raw path)."""
        return self.source_id or self.path.stem


@dataclass(frozen=True)
class StagedSource:
    """The result of a successful :meth:`Quarantine.open`.

    ``data`` are the exact bytes that were hashed and parsed; staging copies
    these bytes so the executed source is byte-identical to the validated one.
    ``private`` marks a content-addressed copy written by :meth:`Quarantine.stage`
    (as opposed to a workspace file read by :meth:`Quarantine.open`); only a
    private copy may be deleted by cleanup.
    """

    candidate: ExtCandidate
    path: Path
    data: bytes
    text: str
    sha256: str
    size: int
    mode: int
    warnings: tuple[str, ...] = ()
    private: bool = False

    @property
    def origin(self) -> str:
        return self.candidate.origin

    @property
    def source_identity(self) -> str:
        """The stable absolute identity of the *original* source file.

        This is deliberately the original workspace path, not the staged copy,
        so a module name derived from it is stable across staging and
        generations and unique across two files that share a stem. ``resolve``
        is non-strict, so a source deleted after staging still yields its path.
        """
        try:
            return str(self.candidate.path.resolve())
        except OSError:  # pragma: no cover - resolve rarely fails
            return str(self.candidate.path.absolute())


@dataclass(frozen=True)
class ExtractedSpec:
    """The JSON-safe subset of a declared :class:`ToolSpec`."""

    name: str
    description: str
    input_schema: dict[str, Any]
    bundle: str
    mutates: bool
    concurrency: str
    timeout_s: float | None
    #: The declared path mode, carried across the isolated-import JSON boundary
    #: so a hot-loaded path-bearing tool keeps the manager's ``PathGuard``
    #: boundary. Declared last for positional compatibility.
    path_mode: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "bundle": self.bundle,
            "mutates": self.mutates,
            "concurrency": self.concurrency,
            "timeout_s": self.timeout_s,
            "path_mode": self.path_mode,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> ExtractedSpec:
        """Build from JSON-safe metadata; only the four required fields matter.

        ``mutates``/``concurrency``/``timeout_s``/``path_mode`` are optional and
        default to the same values :class:`ToolSpec` uses, so a minimal
        declaration round trips through both the child and the parent
        identically.
        """
        for required in ("name", "description", "input_schema", "bundle"):
            if required not in payload:
                raise KeyError(required)
        return cls(
            name=str(payload["name"]),
            description=str(payload["description"]),
            input_schema=dict(payload["input_schema"]),
            bundle=str(payload["bundle"]),
            mutates=bool(payload.get("mutates", False)),
            concurrency=str(payload.get("concurrency", "parallel")),
            timeout_s=payload.get("timeout_s"),
            path_mode=bool(payload.get("path_mode", False)),
        )


@dataclass(frozen=True)
class SubprocessOutcome:
    """The raw outcome of the isolated import (untrusted until re-validated)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_oversize: bool = False
    duration_ms: float = 0.0


@dataclass(frozen=True)
class QuarantineOutcome:
    """A sanitized verdict for one candidate, safe to show a model or log."""

    code: QuarantineCode
    ok: bool = False
    origin: str = "ext"
    path: str = ""
    name: str = ""
    sha256: str = ""
    size: int = 0
    kind: str = "tool"
    detail: str = ""
    error_type: str = ""
    warnings: tuple[str, ...] = ()
    declaration: str = ""
    tools: tuple[str, ...] = ()
    bundled_tools: tuple[str, ...] = ()
    attempt: int = 0

    def with_attempt(self, attempt: int) -> QuarantineOutcome:
        return QuarantineOutcome(
            code=self.code,
            ok=self.ok,
            origin=self.origin,
            path=self.path,
            name=self.name,
            sha256=self.sha256,
            size=self.size,
            kind=self.kind,
            detail=self.detail,
            error_type=self.error_type,
            warnings=self.warnings,
            declaration=self.declaration,
            tools=self.tools,
            bundled_tools=self.bundled_tools,
            attempt=attempt,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "ok": self.ok,
            "origin": self.origin,
            "path": self.path,
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "kind": self.kind,
            "detail": self.detail,
            "error_type": self.error_type,
            "warnings": list(self.warnings),
            "declaration": self.declaration,
            "tools": list(self.tools),
            "bundled_tools": list(self.bundled_tools),
            "attempt": self.attempt,
        }


@dataclass(frozen=True)
class QuarantineResult:
    """A successful quarantine run: the outcome plus everything needed to load.

    ``specs`` are validated :class:`ToolSpec` objects reconstructed in-process
    from the isolated child's JSON, so the in-process import never has to trust
    a value the child produced.
    """

    outcome: QuarantineOutcome
    staged: StagedSource
    specs: tuple[ToolSpec, ...] = ()
    declaration: str = ""
    bundled_tools: tuple[str, ...] = ()

    @property
    def code(self) -> QuarantineCode:
        return self.outcome.code

    @property
    def ok(self) -> bool:
        return self.outcome.ok


@dataclass
class _StaticReport:
    """Internal static-analysis result for one frozen source."""

    declaration: str = ""
    warnings: list[str] = field(default_factory=list)
    syntax_error: BaseException | None = None
    registered_names: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The child bootstrap
# ---------------------------------------------------------------------------

#: The program the isolated subprocess runs. It is a self-contained string (no
#: import of Nexus in the parent's interpreter) so the child's environment and
#: import surface are exactly what is described here.
#:
#: It refuses to run any ``run`` coroutine (``SPEC``-only modules are never
#: asked to execute), imports the module from the file path, extracts the
#: declaration, and prints one JSON object on stdout prefixed with a sentinel.
CHILD_BOOTSTRAP = r'''
import importlib.util
import inspect
import json
import sys

_SENTINEL = "@@NEXUS_EXT@@"


def _fail(kind, message):
    sys.stdout.write(_SENTINEL + json.dumps({"status": kind, "error": message}) + "\n")
    sys.stdout.flush()
    raise SystemExit(3 if kind == "no_contract" else 4)


def _schema_ok(schema):
    return isinstance(schema, dict)


def _extract(spec):
    if spec is None:
        return None
    required = ("name", "description", "input_schema", "bundle")
    optional = ("mutates", "concurrency", "timeout_s", "path_mode")
    values = {}
    for field in required:
        if isinstance(spec, dict):
            if field not in spec:
                return None
            values[field] = spec[field]
        else:
            if not hasattr(spec, field):
                return None
            values[field] = getattr(spec, field)
    for field in optional:
        if isinstance(spec, dict):
            values[field] = spec.get(field)
        else:
            values[field] = getattr(spec, field, None)
    if values["input_schema"] is None:
        return None
    try:
        values["input_schema"] = json.loads(json.dumps(values["input_schema"]))
    except (TypeError, ValueError):
        return None
    if values.get("mutates") is None:
        values["mutates"] = False
    if values.get("concurrency") is None:
        values["concurrency"] = "parallel"
    return {
        "name": values["name"],
        "description": values["description"],
        "input_schema": values["input_schema"],
        "bundle": values["bundle"],
        "mutates": values["mutates"],
        "concurrency": values["concurrency"],
        "timeout_s": values.get("timeout_s"),
        "path_mode": bool(values.get("path_mode") or False),
    }


def main(path):
    name = "nexus_ext_isolated"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        _fail("no_contract", "cannot build an import spec for the file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    has_spec = hasattr(module, "SPEC")
    has_register = callable(getattr(module, "register", None))
    if has_spec and has_register:
        _fail("ambiguous", "module defines both SPEC and register()")
    if not has_spec and not has_register:
        _fail("no_contract", "module defines neither SPEC nor register()")

    if has_spec:
        raw = module.SPEC
        extracted = _extract(raw)
        if extracted is None:
            _fail("bad_spec", "SPEC is not a tool declaration object")
        run = getattr(module, "run", None)
        if not callable(run):
            _fail("bad_run", "module has SPEC but no callable run(args, ctx)")
        if not inspect.iscoroutinefunction(run):
            _fail("sync_run", "run(args, ctx) must be an async function")
        payload = {"status": "ok", "declaration": "spec", "spec": extracted,
                   "tools": [extracted["name"]]}
    else:
        register = module.register
        try:
            produced = register()
        except Exception as exc:
            _fail("bad_register", type(exc).__name__)
        if inspect.iscoroutine(produced):
            _fail("bad_register", "register() must be synchronous, not a coroutine")
        if produced is None:
            produced = []
        try:
            items = list(produced)
        except TypeError:
            _fail("bad_register", "register() must return an iterable of tools")
        names = []
        specs = []
        for item in items:
            if isinstance(item, dict):
                item_spec = item.get("spec")
                item_run = item.get("run")
            else:
                item_spec = getattr(item, "spec", None)
                item_run = getattr(item, "run", None)
            extracted = _extract(item_spec)
            if extracted is None:
                _fail("bad_spec", "registered tool has no valid spec")
            if item_run is not None and not inspect.iscoroutinefunction(item_run):
                _fail("sync_run", "registered tool run must be async")
            names.append(extracted["name"])
            specs.append(extracted)
        payload = {"status": "ok", "declaration": "register", "tools": names,
                   "specs": specs}
    sys.stdout.write(_SENTINEL + json.dumps(payload) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except SystemExit:
        raise
    except BaseException as exc:
        sys.stderr.write(type(exc).__name__ + "\n")
        sys.stderr.flush()
        raise SystemExit(4)
'''


# ---------------------------------------------------------------------------
# Isolation helper
# ---------------------------------------------------------------------------


def _isolated_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the minimal environment for the child.

    Starts from an explicit allow-list, never from ``os.environ`` as a whole, so
    an injected ``PYTHONPATH``/``PYTHONSTARTUP`` or a credential variable cannot
    be read by import-time code. The virtualenv's ``bin`` is placed on ``PATH``
    (derived from ``sys.executable``, not the environment) so a legitimate
    third-party dependency remains importable.
    """
    source = os.environ if base is None else base
    env = {key: source[key] for key in _SAFE_ENV_KEYS if key in source}
    interpreter_dir = os.path.dirname(os.path.abspath(sys.executable))
    existing = env.get("PATH")
    env["PATH"] = interpreter_dir if not existing else f"{interpreter_dir}{os.pathsep}{existing}"
    # Keep bytecode out of the source tree; a quarantined file must not seed a
    # cached .pyc that a later import could pick up.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _group_alive(pgid: int) -> bool:
    """Whether any process still belongs to the process group ``pgid``."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - own group; defensive
        return True
    except OSError:
        return False


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """TERM then KILL the child's whole process group, never a single pid.

    Deliberately does **not** early-return when the leader has already exited:
    the leader can exit while leaving a hung descendant (a forked grandchild)
    alive in the same session/process group. ``start_new_session=True`` makes
    the leader its own group leader, so ``killpg(leader.pid, ...)`` still
    reaches every descendant after the leader is reaped. Every signal and wait
    tolerates an already-gone process.
    """
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + 0.5
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Reap the leader if it has not been reaped; a live descendant keeps the
    # group alive and is already SIGKILLed above.
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        pass


def _run_child(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    timeout_s: float,
    output_limit: int,
) -> SubprocessOutcome:
    """Run the child, draining stdout/stderr concurrently and bounding output.

    Reading with a selector means a child that writes megabytes to stderr cannot
    deadlock on a full pipe while the parent waits for it to exit -- the classic
    ``subprocess`` hang. Each stream is truncated at ``output_limit``; once
    both are capped the pipes are drained but discarded so the child still sees
    an EOF and can finish.
    """
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers: dict[Any, bytearray] = {
        process.stdout: bytearray(),
        process.stderr: bytearray(),
    }
    oversize = False
    deadline = started + timeout_s
    timed_out = False
    try:
        for stream in (process.stdout, process.stderr):
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=min(remaining, 0.1))
            if not events and process.poll() is not None:
                # The leader has exited and no output is pending. A forked
                # descendant may still hold the pipe write end open, which would
                # otherwise stall this drain until the deadline; stop here and
                # let the group teardown below reap it.
                break
            for key, _ in events:
                # Read from the raw fd: a buffered ``read`` would block waiting
                # to fill its buffer while a forked descendant still holds the
                # write end open, even though the selector reported data ready.
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[key.fileobj]
                if len(buffer) < output_limit:
                    room = output_limit - len(buffer)
                    buffer.extend(chunk[:room])
                    if len(chunk) > room:
                        oversize = True
                else:
                    oversize = True
    finally:
        if not timed_out:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        # A clean exit (code 0, no timeout) means the import never forked a
        # descendant, and ``wait`` has now reaped the leader -- so its pid and
        # process group may already have been reused. Guard that case: do not
        # signal a pgid that may belong to an unrelated process group. Any
        # non-zero exit or timeout still tears the whole group down, which is
        # what reaps a hung forked descendant after the leader is gone.
        if timed_out or process.returncode != 0:
            _kill_process_group(process)
        else:
            try:
                process.wait(timeout=0.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - already reaped
                pass
        selector.close()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    return SubprocessOutcome(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=buffers[process.stdout].decode("utf-8", "replace"),
        stderr=buffers[process.stderr].decode("utf-8", "replace"),
        timed_out=timed_out,
        output_oversize=oversize,
        duration_ms=duration_ms,
    )


def _dir_identity(path: Path) -> tuple[int, int, int] | None:
    """The device/inode/type identity of a directory, or ``None`` if missing.

    Uses ``lstat`` so a directory swapped for a symlink between two calls is a
    different identity, which is exactly the parent-swap race the bounded read
    must fail closed against.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _ancestor_components(path: Path) -> tuple[Path, ...]:
    """Every ancestor of ``path`` from the filesystem anchor down to ``path``.

    The anchor (``/``) is never a symlink and is omitted; the final element is
    ``path`` itself. Used to inspect each component of a file's parent chain
    consistently, whether the caller supplied a resolved or a lexical path.
    """
    parts = path.parts
    if not parts:
        return ()
    current = Path(parts[0])
    components: list[Path] = []
    for part in parts[1:]:
        current = current / part
        components.append(current)
    return tuple(components)


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------

#: Import names whose import has a side effect obvious enough to warn about.
_SIDE_EFFECT_IMPORTS = frozenset({"subprocess", "shutil", "socket", "requests"})
#: Call names that look like import-time side effects. A warning, not a refusal:
#: a legitimate extension may use these inside its ``run`` body, and the
#: warning is surfaced rather than silently ignored.
_DANGEROUS_CALLS = frozenset(
    {
        "system",
        "popen",
        "exec",
        "eval",
        "compile",
        "exec_module",
        "remove",
        "unlink",
        "rmtree",
        "chmod",
        "chown",
        "rename",
        "replace",
        "open",
    }
)


def _iter_imports(tree: ast.Module) -> list[ast.stmt]:
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _call_owner(node: ast.Call) -> str:
    """The top-level object a method call is made on, if statically knowable."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return ""


def _is_side_effect_call(node: ast.Call) -> str | None:
    """Return a warning name for a dangerous import-time call, else ``None``."""
    name = _call_name(node)
    if name in _DANGEROUS_CALLS:
        return f"{name}()"
    owner = _call_owner(node)
    if owner in _SIDE_EFFECT_IMPORTS:
        return f"{owner}.{name}()"
    return None


def _top_level_calls(node: ast.stmt) -> list[ast.Call]:
    """Calls that execute when a top-level statement runs at import time.

    Covers expression statements, assignment values (``X = f()``), annotated
    assignment values, return/raise arguments, and the ``with``/``for`` headers.
    Calls nested inside a function or class body are deliberately excluded --
    the harness imports the module but never calls those.
    """
    found: list[ast.Call] = []
    if isinstance(node, ast.Expr):
        found.extend(ast.walk(node.value))
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        value = node.value
        if value is not None:
            found.extend(ast.walk(value))
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            found.extend(ast.walk(item.context_expr))
            if item.optional_vars is not None:
                found.extend(ast.walk(item.optional_vars))
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        found.extend(ast.walk(node.iter))
    return [child for child in found if isinstance(child, ast.Call)]


def _static_analysis(tree: ast.Module) -> list[str]:
    """Warn-list scan for import-time side effects.

    Only top-level statements are considered, because the harness imports the
    module but never calls ``run``; a dangerous call inside a function body is
    out of scope for a warning.
    """
    warnings: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _SIDE_EFFECT_IMPORTS:
                    warnings.append(f"import-time import of {root!r}")
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _SIDE_EFFECT_IMPORTS:
                warnings.append(f"import-time import from {root!r}")
            continue
        if isinstance(node, ast.If):
            # ``if __name__ == "__main__":`` is the conventional guard and is
            # expected to do real work at import; do not warn on its body.
            test = node.test
            is_main_guard = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            )
            if is_main_guard:
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    described = _is_side_effect_call(child)
                    if described is not None:
                        warnings.append(f"conditional import-time call to {described}")
                        break
            continue
        for call in _top_level_calls(node):
            described = _is_side_effect_call(call)
            if described is not None:
                warnings.append(f"import-time call to {described}")
    return sorted(dict.fromkeys(warnings))


def _module_has_contract(tree: ast.Module) -> tuple[bool, bool]:
    """Whether the module defines ``SPEC`` and/or ``register`` at top level."""
    has_spec = False
    has_register = False
    for node in tree.body:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            targets = [
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if "SPEC" in targets:
            has_spec = True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "register"
        ):
            has_register = True
    # A module-level ``from x import SPEC`` also satisfies the contract.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and any(
            alias.asname == "SPEC" or alias.name == "SPEC" for alias in node.names
        ):
            has_spec = True
    return has_spec, has_register


# ---------------------------------------------------------------------------
# The quarantiner
# ---------------------------------------------------------------------------


class Quarantine:
    """Validate extension files before they are imported into a generation.

    One instance is cheap and stateless beyond its configuration; the
    orchestrator may reuse it across a reload. All methods are synchronous: the
    only blocking work is a bounded file read and a timeout-bounded subprocess,
    both of which the caller runs off the event loop (a reload is not an
    iteration-critical operation).
    """

    def __init__(
        self,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        output_limit: int = MAX_OUTPUT_BYTES,
        python: str | None = None,
        builtin_names: Sequence[str] | None = None,
        reserved_names: Sequence[str] | None = None,
        stage_root: str | os.PathLike[str] | None = None,
        root: str | os.PathLike[str] | None = None,
    ) -> None:
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int):
            raise QuarantineError("max_file_bytes must be a positive integer")
        if max_file_bytes < 1:
            raise QuarantineError("max_file_bytes must be a positive integer")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or timeout_s <= 0
        ):
            raise QuarantineError("timeout_s must be a positive finite number")
        self._max_file_bytes = max_file_bytes
        self._timeout_s = float(timeout_s)
        self._output_limit = output_limit
        self._python = python or sys.executable
        self._builtin_names = frozenset(builtin_names or ())
        self._reserved_names = frozenset(reserved_names or ())
        self._stage_root = Path(stage_root) if stage_root is not None else None
        #: The configured root as a *lexical* absolute spelling (``abspath``,
        #: never ``resolve``). Only symlink components that are part of this
        #: spelling -- a lexical ancestor of the root, or the root itself -- are
        #: treated as trusted host layout (``/tmp`` on macOS, ``/var`` on Linux).
        #: Any symlink strictly below the root is an in-tree link and refused,
        #: regardless of where it points. The final component is always checked
        #: regardless.
        self._root = (
            Path(os.path.abspath(os.fspath(root))) if root is not None else None
        )

    # -- configuration -----------------------------------------------------

    @property
    def max_file_bytes(self) -> int:
        return self._max_file_bytes

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def python(self) -> str:
        return self._python

    def with_builtin_names(self, names: Sequence[str]) -> Quarantine:
        return Quarantine(
            max_file_bytes=self._max_file_bytes,
            timeout_s=self._timeout_s,
            output_limit=self._output_limit,
            python=self._python,
            builtin_names=names,
            reserved_names=self._reserved_names,
            stage_root=self._stage_root,
            root=self._root,
        )

    # -- stage 1: bounded, exact read --------------------------------------

    def open(self, path: str | os.PathLike[str], *, origin: str = "ext") -> StagedSource:
        """Read and freeze a candidate's exact bytes, or raise :class:`_Refusal`."""
        # Resolve to an absolute path so the isolated child (which runs with a
        # different cwd) imports the same file we hashed, and so path-component
        # checks have a single canonical form.
        candidate_path = Path(os.path.abspath(os.fspath(path)))
        try:
            lst = candidate_path.lstat()
        except FileNotFoundError as exc:
            raise _Refusal(QuarantineCode.MISSING, "file does not exist") from exc
        except OSError as exc:
            raise _Refusal(QuarantineCode.NOT_A_FILE, _safe_error(exc)) from exc
        if stat.S_ISLNK(lst.st_mode):
            raise _Refusal(QuarantineCode.SYMLINK, "refuses to follow a symlink")
        if not stat.S_ISREG(lst.st_mode):
            raise _Refusal(QuarantineCode.NOT_A_FILE, "not a regular file")
        if lst.st_size > self._max_file_bytes:
            raise _Refusal(
                QuarantineCode.OVERSIZE,
                f"{lst.st_size} bytes exceeds the {self._max_file_bytes}-byte cap",
            )
        # Refuse any path below the configured root whose components include a
        # symlink, so a swapped parent cannot redirect the read after the lstat
        # above. The final component was already checked in ``open``.
        self._reject_symlinked_components(candidate_path)
        parent_before = _dir_identity(candidate_path.parent)
        data = self._read_bounded(candidate_path, lst.st_size)
        parent_after = _dir_identity(candidate_path.parent)
        if parent_before is None or parent_before != parent_after:
            raise _Refusal(
                QuarantineCode.SYMLINK,
                "parent directory changed during the bounded read; refusing",
            )
        try:
            after = candidate_path.lstat()
        except OSError as exc:
            raise _Refusal(QuarantineCode.NOT_A_FILE, _safe_error(exc)) from exc
        if stat.S_ISLNK(after.st_mode) or (after.st_dev, after.st_ino) != (
            lst.st_dev,
            lst.st_ino,
        ):
            raise _Refusal(
                QuarantineCode.SYMLINK,
                "file changed during the bounded read; refusing to trust it",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _Refusal(
                QuarantineCode.NOT_UTF8, f"source is not valid UTF-8: {_safe_error(exc)}"
            ) from exc
        if "\x00" in text:
            raise _Refusal(QuarantineCode.NUL_BYTE, "source contains a NUL byte")
        digest = hashlib.sha256(data).hexdigest()
        return StagedSource(
            candidate=ExtCandidate(
                path=candidate_path, origin=origin, source_id=candidate_path.stem
            ),
            path=candidate_path,
            data=data,
            text=text,
            sha256=digest,
            size=len(data),
            mode=stat.S_IMODE(lst.st_mode),
        )

    def _reject_symlinked_components(self, path: Path) -> None:
        """Refuse symlinked path components that could redirect the read.

        Every component from the filesystem anchor down to (and including) the
        file's parent is inspected with ``lstat``. The decision is **lexical**:
        a symlinked component is allowed only when it is part of the configured
        root's own spelling -- a lexical ancestor of the root, or the root
        itself (for example ``/tmp`` when the root is spelled ``/tmp/ws`` on
        macOS). Such a component is trusted host layout, not a swapped parent.

        Any symlink strictly below the root is an in-tree link and is refused,
        no matter what it points at: a link to an ancestor (``ws/up -> ..``) or
        to the filesystem root (``ws/out -> /``) could redirect the bounded read
        outside the validated tree, so target inspection is deliberately not
        used to exempt it. With no root configured every symlinked component is
        refused, so a rootless quarantine fails closed rather than checking
        nothing.
        """
        absolute = path if path.is_absolute() else Path.cwd() / path
        for component in _ancestor_components(absolute.parent):
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _Refusal(
                    QuarantineCode.SYMLINK,
                    f"cannot inspect path component {component.name!r}: "
                    f"{_safe_error(exc)}",
                ) from exc
            if not stat.S_ISLNK(mode):
                continue
            if not self._symlink_is_root_prefix(component):
                raise _Refusal(
                    QuarantineCode.SYMLINK,
                    f"path component {component.name!r} is a symlink",
                )

    def _symlink_is_root_prefix(self, component: Path) -> bool:
        """Whether a symlinked ``component`` is a lexical part of the root.

        True only when ``component`` *is* the configured root or a lexical
        ancestor of it (``root.is_relative_to(component)``). The comparison is
        pure path arithmetic -- the symlink's target is never resolved or
        inspected -- so an in-tree link to an ancestor or outside the tree is
        never mistaken for host layout.
        """
        root = self._root
        if root is None:
            return False
        try:
            return component == root or root.is_relative_to(component)
        except ValueError:  # pragma: no cover - is_relative_to rarely raises
            return False

    def _read_bounded(self, path: Path, expected_size: int) -> bytes:
        """Read exactly ``expected_size`` bytes; refuse if the file changed."""
        chunks: list[bytes] = []
        total = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(min(SOURCE_READ_CHUNK, self._max_file_bytes + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > self._max_file_bytes:
                    raise _Refusal(
                        QuarantineCode.OVERSIZE,
                        f"grew past the {self._max_file_bytes}-byte cap while reading",
                    )
                chunks.append(chunk)
            # A file that grew or shrank between lstat and read is a race: the
            # validated bytes would not be the bytes on disk.
            if total != expected_size:
                raise _Refusal(
                    QuarantineCode.HASH_MISMATCH,
                    "file changed size during the bounded read",
                )
        return b"".join(chunks)

    # -- stage 2: static analysis ------------------------------------------

    def inspect(self, staged: StagedSource) -> QuarantineOutcome:
        """Parse and statically analyze frozen bytes; never raises a refusal."""
        report = self._analyze(staged)
        if report.syntax_error is not None:
            return self._failure(
                staged, QuarantineCode.SYNTAX_ERROR, report.syntax_error, report
            )
        if not report.declaration:
            return self._failure(
                staged,
                QuarantineCode.NO_CONTRACT,
                _Refusal(
                    QuarantineCode.NO_CONTRACT,
                    "no SPEC assignment and no register() function",
                ),
                report,
            )
        return self._outcome(staged, QuarantineCode.OK, ok=True, report=report)

    def _analyze(self, staged: StagedSource) -> _StaticReport:
        report = _StaticReport()
        try:
            tree = ast.parse(staged.text, filename=staged.path.name)
        except SyntaxError as exc:
            report.syntax_error = exc
            return report
        report.warnings = _static_analysis(tree)
        has_spec, has_register = _module_has_contract(tree)
        if has_spec and has_register:
            report.declaration = "ambiguous"
        elif has_spec:
            report.declaration = "spec"
        elif has_register:
            report.declaration = "register"
        return report

    # -- stage 3: isolated import ------------------------------------------

    def run_isolated(self, staged: StagedSource) -> QuarantineOutcome:
        """Import the frozen file in a minimal-env subprocess, or refuse.

        The return is either ``OK`` with ``declaration``/``tools`` populated
        from the child's JSON, or a refusal with a sanitized ``detail``. A
        timeout is always a :attr:`QuarantineCode.IMPORT_TIMEOUT`, never a
        traceback.
        """
        env = _isolated_environment()
        outcome = _run_child(
            [
                self._python,
                "-I",
                "-B",
                "-c",
                CHILD_BOOTSTRAP,
                str(staged.path),
            ],
            env=env,
            cwd=str(staged.path.parent),
            timeout_s=self._timeout_s,
            output_limit=self._output_limit,
        )
        if outcome.timed_out:
            return self._subprocess_failure(
                staged, QuarantineCode.IMPORT_TIMEOUT, outcome, "import timed out"
            )
        # Oversize output is refused before the payload is trusted: a child that
        # floods a stream has already exceeded its contract, regardless of what
        # it printed on the other stream.
        if outcome.output_oversize:
            return self._subprocess_failure(
                staged,
                QuarantineCode.OUTPUT_OVERSIZE,
                outcome,
                "child produced more output than allowed",
            )
        payload = self._parse_child_payload(outcome)
        if payload is None:
            if outcome.returncode != 0:
                detail = sanitize_text(outcome.stderr.strip().splitlines()[-1]) if outcome.stderr.strip() else ""
                return self._subprocess_failure(
                    staged,
                    QuarantineCode.IMPORT_EXIT,
                    outcome,
                    detail or f"child exited with code {outcome.returncode}",
                )
            return self._subprocess_failure(
                staged,
                QuarantineCode.BAD_RESULT,
                outcome,
                "child returned no JSON result",
            )
        status = str(payload.get("status", ""))
        if status != "ok":
            mapping = {
                "no_contract": QuarantineCode.NO_CONTRACT,
                "ambiguous": QuarantineCode.AMBIGUOUS_CONTRACT,
                "bad_spec": QuarantineCode.BAD_SPEC,
                "bad_run": QuarantineCode.BAD_RUN,
                "sync_run": QuarantineCode.SYNC_RUN,
                "bad_register": QuarantineCode.BAD_REGISTER,
            }
            code = mapping.get(status, QuarantineCode.BAD_RESULT)
            return self._subprocess_failure(
                staged, code, outcome, sanitize_text(payload.get("error", code))
            )
        declaration = str(payload.get("declaration", ""))
        names = tuple(
            str(name) for name in payload.get("tools", []) if isinstance(name, str)
        )
        try:
            specs_payload = payload.get("specs")
            if specs_payload is not None:
                for item in specs_payload:
                    self._validate_extracted(ExtractedSpec.from_json(item))
            elif payload.get("spec") is not None:
                self._validate_extracted(ExtractedSpec.from_json(payload["spec"]))
        except (KeyError, TypeError, ValueError, ToolSpecError) as exc:
            return self._subprocess_failure(
                staged, QuarantineCode.BAD_SPEC, outcome, _safe_error(exc)
            )
        return self._outcome(
            staged,
            QuarantineCode.OK,
            ok=True,
            report=self._analyze(staged),
            declaration=declaration,
            tools=names,
        )

    @staticmethod
    def _parse_child_payload(outcome: SubprocessOutcome) -> dict[str, Any] | None:
        sentinel = "@@NEXUS_EXT@@"
        for line in outcome.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith(sentinel):
                try:
                    parsed = json.loads(stripped[len(sentinel) :])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
        return None

    def _validate_extracted(self, extracted: ExtractedSpec) -> None:
        """Reconstruct a real :class:`ToolSpec` from child JSON and validate it.

        Validation is deliberately done with the *contract's own* constructors
        rather than a hand-rolled check, so any future tightening of
        ``ToolSpec`` applies to quarantined extensions automatically.
        """
        ToolSpec(
            name=extracted.name,
            description=extracted.description,
            input_schema=extracted.input_schema,
            bundle=extracted.bundle,
            mutates=extracted.mutates,
            concurrency=extracted.concurrency,
            timeout_s=extracted.timeout_s,
            path_mode=extracted.path_mode,
        )

    # -- stage 4: staging + hash verification ------------------------------

    def stage(self, staged: StagedSource, *, root: str | os.PathLike[str] | None = None) -> StagedSource:
        """Write an exact, content-addressed private copy and verify it.

        The copy is addressed by content hash and written to a private
        directory, so a subsequent edit to the workspace file cannot change
        what will be imported. The staged bytes are re-read and re-hashed, and
        a mismatch is a refusal -- this is the TOCTOU close between "validated"
        and "executed".
        """
        base = Path(root) if root is not None else self._staged_root()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = base / f"{staged.candidate.identity()}-{staged.sha256[:16]}.py"
        if target.exists():
            current = target.read_bytes()
            if current != staged.data:
                # Content-addressed name collision is a hash collision or a
                # stale partial write; neither is safe to reuse.
                raise _Refusal(
                    QuarantineCode.HASH_MISMATCH,
                    "staged copy differs from the validated source",
                )
        else:
            tmp = target.with_suffix(".py.tmp")
            tmp.write_bytes(staged.data)
            os.replace(tmp, target)
        verified = self._read_staged(target)
        if verified.sha256 != staged.sha256:
            raise _Refusal(
                QuarantineCode.HASH_MISMATCH,
                "staged copy does not match the validated hash",
            )
        return StagedSource(
            candidate=staged.candidate,
            path=target,
            data=verified.data,
            text=verified.text,
            sha256=verified.sha256,
            size=verified.size,
            mode=staged.mode,
            warnings=staged.warnings,
            private=True,
        )

    def discard_staged(self, staged: StagedSource) -> bool:
        """Delete a private staged copy; never touches a workspace source.

        Returns ``True`` when a private copy was removed, ``False`` when the
        source was not private or the file was already gone. This is the
        file-side counterpart to :meth:`ToolLoader.release`'s ``sys.modules``
        cleanup, and the reason ``StagedSource.private`` exists: cleanup must be
        unable to delete the user's original extension file.
        """
        if not staged.private:
            return False
        try:
            staged.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _staged_root(self) -> Path:
        if self._stage_root is not None:
            return self._stage_root
        return Path(os.path.abspath(os.path.join(os.getcwd(), ".nexus", "stage")))

    @staticmethod
    def _read_staged(path: Path) -> StagedSource:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - staged copy only
            raise _Refusal(
                QuarantineCode.NOT_UTF8, _safe_error(exc)
            ) from exc
        return StagedSource(
            candidate=ExtCandidate(path=path, source_id=path.stem),
            path=path,
            data=data,
            text=text,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            mode=stat.S_IMODE(path.stat().st_mode),
        )

    def verify(self, staged: StagedSource, expected_sha256: str) -> bool:
        """Whether the staged bytes still hash to the validated digest."""
        try:
            current = hashlib.sha256(staged.path.read_bytes()).hexdigest()
        except OSError:
            return False
        return current == expected_sha256

    def finalize(self, staged: StagedSource, expected_sha256: str) -> None:
        """Refuse unless the staged bytes hash to ``expected_sha256``."""
        if not self.verify(staged, expected_sha256):
            raise _Refusal(
                QuarantineCode.HASH_MISMATCH,
                "staged source changed before execution; refusing to import",
            )

    # -- name and schema validation against the live world -----------------

    def check_names(
        self,
        specs: Sequence[ToolSpec],
        *,
        builtin_names: Sequence[str] | None = None,
        reserved_names: Sequence[str] | None = None,
    ) -> None:
        """Refuse duplicate names, case-fold collisions, and builtin collisions.

        ``reserved_names`` are names already owned by a *lower-precedence*
        source in the same reload (a builtin catalog, or another file); a
        collision is refused because the manifest is keyed by exact name.
        Case-fold collisions are refused across the batch so the harness can
        never hold ``Read`` and ``read`` as distinct tools while a case-blind
        lookup would confuse them.
        """
        builtin = frozenset(builtin_names) if builtin_names is not None else self._builtin_names
        reserved = frozenset(reserved_names) if reserved_names is not None else self._reserved_names
        seen: dict[str, str] = {}
        folded: dict[str, str] = {}
        for spec in specs:
            if NAME_PATTERN.fullmatch(spec.name) is None:
                raise _Refusal(
                    QuarantineCode.BAD_TOOL_NAME, f"invalid tool name {spec.name!r}"
                )
            if spec.name in builtin:
                raise _Refusal(
                    QuarantineCode.BUILTIN_COLLISION,
                    f"tool name {spec.name!r} collides with a builtin",
                )
            if spec.name in reserved:
                raise _Refusal(
                    QuarantineCode.DUPLICATE_TOOL,
                    f"tool name {spec.name!r} is already registered",
                )
            if spec.name in seen:
                raise _Refusal(
                    QuarantineCode.DUPLICATE_TOOL,
                    f"duplicate tool name {spec.name!r}",
                )
            key = spec.name.casefold()
            if key in folded:
                raise _Refusal(
                    QuarantineCode.CASE_COLLISION,
                    f"tool name {spec.name!r} case-folds onto {folded[key]!r}",
                )
            seen[spec.name] = spec.name
            folded[key] = spec.name

    # -- one-shot convenience ----------------------------------------------

    def inspect_only(
        self,
        path: str | os.PathLike[str],
        *,
        origin: str = "ext",
    ) -> QuarantineOutcome:
        """Static-only validation (no subprocess); used when quarantine is off."""
        try:
            staged = self.open(path, origin=origin)
            inspection = self.inspect(staged)
            return inspection
        except _Refusal as refusal:
            return self._refusal_outcome(path, origin, refusal)
        except QuarantineError as exc:
            return QuarantineOutcome(
                code=QuarantineCode.NOT_A_FILE,
                origin=origin,
                path=sanitize_text(path, limit=200),
                detail=sanitize_text(str(exc)),
            )

    def inspect_bytes(
        self, data: bytes, *, origin: str = "ext", source_id: str = "buffer"
    ) -> QuarantineOutcome:
        """Validate a candidate already held in memory (tests/fixtures).

        The bytes are hashed and parsed directly; an oversize buffer is refused
        exactly like an oversize file.
        """
        if len(data) > self._max_file_bytes:
            return QuarantineOutcome(
                code=QuarantineCode.OVERSIZE,
                origin=origin,
                name=source_id,
                size=len(data),
                detail=(
                    f"{len(data)} bytes exceeds the {self._max_file_bytes}-byte cap"
                ),
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return QuarantineOutcome(
                code=QuarantineCode.NOT_UTF8,
                origin=origin,
                name=source_id,
                detail=_safe_error(exc),
            )
        if "\x00" in text:
            return QuarantineOutcome(
                code=QuarantineCode.NUL_BYTE, origin=origin, name=source_id
            )
        staged = StagedSource(
            candidate=ExtCandidate(
                path=Path(source_id), origin=origin, source_id=source_id
            ),
            path=Path(source_id),
            data=data,
            text=text,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            mode=0o644,
        )
        return self.inspect(staged)

    # -- outcome builders ---------------------------------------------------

    def _outcome(
        self,
        staged: StagedSource,
        code: QuarantineCode,
        *,
        ok: bool = False,
        report: _StaticReport | None = None,
        declaration: str = "",
        tools: Sequence[str] = (),
    ) -> QuarantineOutcome:
        return QuarantineOutcome(
            code=code,
            ok=ok,
            origin=staged.origin,
            path=sanitize_text(str(staged.path), limit=200),
            name=staged.candidate.identity(),
            sha256=staged.sha256,
            size=staged.size,
            detail="",
            warnings=tuple(report.warnings) if report else (),
            declaration=declaration or (report.declaration if report else ""),
            tools=tuple(tools),
        )

    def _failure(
        self,
        staged: StagedSource,
        code: QuarantineCode,
        exc: BaseException,
        report: _StaticReport | None = None,
    ) -> QuarantineOutcome:
        return QuarantineOutcome(
            code=code,
            ok=False,
            origin=staged.origin,
            path=sanitize_text(str(staged.path), limit=200),
            name=staged.candidate.identity(),
            sha256=staged.sha256,
            size=staged.size,
            detail=_safe_error(exc),
            error_type=type(exc).__name__,
            warnings=tuple(report.warnings) if report else (),
            declaration=report.declaration if report else "",
        )

    def _subprocess_failure(
        self,
        staged: StagedSource,
        code: QuarantineCode,
        outcome: SubprocessOutcome,
        detail: str,
    ) -> QuarantineOutcome:
        return QuarantineOutcome(
            code=code,
            ok=False,
            origin=staged.origin,
            path=sanitize_text(str(staged.path), limit=200),
            name=staged.candidate.identity(),
            sha256=staged.sha256,
            size=staged.size,
            detail=sanitize_text(detail),
            error_type="IsolatedImport",
            warnings=tuple(self._analyze(staged).warnings),
        )

    @staticmethod
    def _refusal_outcome(
        path: str | os.PathLike[str], origin: str, refusal: _Refusal
    ) -> QuarantineOutcome:
        text_path = sanitize_text(str(path), limit=200)
        return QuarantineOutcome(
            code=refusal.code,
            ok=False,
            origin=origin,
            path=text_path,
            name=Path(path).stem if isinstance(path, (str, os.PathLike)) else "",
            detail=sanitize_text(str(refusal)),
        )


# Re-export for the orchestrator's type checking without importing privates.
Defense = Callable[[], None]
