"""The lifecycle hook manager (plan section 5.7).

:class:`HookManager` discovers two kinds of hook and runs them for a lifecycle
event:

* **command hooks** declared in ``.nexus/hooks.toml`` (workspace, falling back
  to ``~/.nexus/hooks.toml``). The file is *restricted*: only ``[[hooks.Event]]``
  tables with a known, small key set are accepted, commands are argv with no
  shell unless ``shell = true`` is opted into, and the environment, stdin, and
  captured output are all bounded;
* **in-process Python hooks** from ``.nexus/hooks/*.py`` (workspace shadows
  user, by file stem). A module declares ``HOOKS`` or a synchronous
  ``register()`` returning hook declarations. The file is read, hashed, and
  staged through the **existing** :class:`~nexus.ext.quarantine.Quarantine` and
  imported under the same version-stamped module name the tool loader uses
  (:func:`nexus.tools.loader.module_name_for`), so unchanged bytes are reused
  object-for-object and a changed file is re-imported at a fresh generation.
  These files are **trusted code**; loading one emits
  :data:`~nexus.hooks.model.TRUSTED_CODE_WARNING`.

A hook returns a :class:`~nexus.hooks.model.HookDecision`. ``block`` stops the
action, ``warn`` records an advisory, and ``modify`` rewrites the event input.
The modification chain is applied deterministically in declaration order and
the caller **must revalidate the schema and re-run the permission gate** on the
result -- a hook is policy, not a grant. Every failure (a broken file, a refused
command, a timeout) is isolated and sanitized: it never raises out of a normal
run and never carries a source body or a credential.

Events: ``hook.fired`` for every hook that ran, ``hook.blocked`` when a hook
blocked. Both use the existing event envelope; a broken sink never breaks a run.

Boundary
--------
Manager layer (L3). Imports the quarantine/loader seam, the permission grammar
(for matchers), and the hook contracts -- never ``core``, ``runtime``, or the
session layer. Cancellation is accepted structurally (anything with a coroutine
``wait()`` and a ``reason``), so this module does not import ``core.cancel``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import shlex
import signal
import sys
import threading
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ExtensionError, ManagerClosed, OperationCancelled
from ..events import Event
from ..ext.manifest import ModuleHandle
from ..ext.quarantine import Quarantine, StagedSource, sanitize_text
from ..tools.loader import build_default_quarantine, module_name_for
from ..tools.permissions import PermissionRuleError, parse_rule
from .model import (
    MODIFIABLE_EVENTS,
    TRUSTED_CODE_WARNING,
    HookAction,
    HookContext,
    HookDecision,
    HookError,
    HookEvent,
    HookFailure,
    HookInvocation,
    HookOnNonzero,
    HookOutcome,
    HookSpec,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_OUTPUT_BYTES",
    "MAX_STDIN_BYTES",
    "MAX_TIMEOUT_S",
    "HookLoadError",
    "HookManager",
    "HookModule",
    "HookModuleLoader",
    "HookSet",
]

#: Where a command hook's output is captured to before truncation.
MAX_OUTPUT_BYTES = 65_536
#: The hook-input JSON handed to a command hook on stdin.
MAX_STDIN_BYTES = 262_144
#: The command-hook timeout when ``hooks.toml`` omits one.
DEFAULT_TIMEOUT_S = 10.0
#: A ceiling on any declared hook timeout.
MAX_TIMEOUT_S = 600.0
#: Refuse a ``hooks.toml`` larger than this before reading it.
_MAX_TOML_BYTES = 262_144
#: Ceilings on a declared command and environment, so a config cannot smuggle
#: an unbounded payload into the process environment or argv.
_MAX_ARGS = 256
_MAX_ARG_BYTES = 8192
_MAX_COMMAND_BYTES = 32_768
_MAX_ENV_KEYS = 64
_MAX_ENV_BYTES = 16_384
#: How long a SIGTERM'd hook process group gets before SIGKILL, and how long the
#: SIGKILL is awaited. Chosen to be short: a hook has already overrun.
_GRACE_S = 0.5
_KILL_WAIT_S = 2.0
#: Drain a reader for at most this long after the process is gone.
_DRAIN_S = 2.0

#: ``hooks.toml`` keys, per entry. Anything else is refused.
_ALLOWED_ENTRY_KEYS = frozenset(
    {
        "name",
        "matcher",
        "type",
        "command",
        "shell",
        "on_nonzero",
        "timeout_s",
        "env",
        "cwd",
        "disabled",
    }
)
#: The safe portion of the host environment a command hook inherits.
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL")


class HookLoadError(ExtensionError):
    """A validated hook module could not be imported or registered."""


def _order_key(spec: HookSpec) -> tuple[int, str, int]:
    """Deterministic execution order: command hooks, then Python hooks.

    Within a kind, hooks run in ``(source, declaration index)`` order, which is
    stable across refreshes because both come from a sorted file listing or an
    ordered TOML array.
    """
    return (0 if spec.is_command else 1, spec.source, spec.index)


def _set_fingerprint(specs: Sequence[HookSpec]) -> str:
    material = "\n".join(spec.fingerprint() for spec in specs)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _failure(kind: str, name: str, exc: BaseException, *, path: Path | None = None) -> HookFailure:
    return HookFailure(
        kind=kind,
        name=name,
        error=sanitize_text(f"{type(exc).__name__}: {exc}"),
        error_type=type(exc).__name__,
        path=sanitize_text(str(path), limit=200) if path is not None else None,
    )


def _message_failure(kind: str, name: str, message: str, *, error_type: str = "HookError", path: Path | None = None) -> HookFailure:
    return HookFailure(
        kind=kind,
        name=name,
        error=sanitize_text(message),
        error_type=error_type,
        path=sanitize_text(str(path), limit=200) if path is not None else None,
    )


def _validate_matcher(matcher: str, *, name: str) -> None:
    """Refuse a matcher that is not a permission rule (structurally reused)."""
    if matcher in ("", "*"):
        return
    try:
        parse_rule(matcher)
    except PermissionRuleError as exc:
        raise HookError(f"hook {name!r} has an invalid matcher: {exc}") from exc


# ---------------------------------------------------------------------------
# Module loading (the quarantine/loader seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookModule:
    """One imported ``.py`` hook module and the hooks it declared."""

    handle: ModuleHandle
    hooks: tuple[HookSpec, ...]
    staged: StagedSource | None = None

    @property
    def name(self) -> str:
        return self.handle.name

    @property
    def source_id(self) -> str:
        return self.handle.path

    @property
    def sha256(self) -> str:
        return self.handle.sha256


def _declaration_mapping(item: object, index: int) -> dict[str, Any]:
    """Normalize one declared hook (mapping or object) into a plain mapping."""
    if isinstance(item, HookSpec):
        return {
            "event": item.event,
            "name": item.name,
            "matcher": item.matcher,
            "timeout_s": item.timeout_s,
            "disabled": item.disabled,
            "run": item.fn,
        }
    if isinstance(item, Mapping):
        return dict(item)
    mapping: dict[str, Any] = {}
    for key in ("event", "name", "matcher", "timeout_s", "disabled"):
        if hasattr(item, key):
            mapping[key] = getattr(item, key)
    for key in ("run", "fn", "handler"):
        candidate = getattr(item, key, None)
        if candidate is not None:
            mapping["run"] = candidate
            break
    return mapping


def extract_hook_declarations(
    module: Any,
    *,
    source: str,
    source_sha256: str,
    generation: int,
) -> tuple[HookSpec, ...]:
    """Extract the hook declarations from an imported module.

    A module must define exactly one of ``HOOKS`` (an iterable) or a synchronous
    ``register()`` returning an iterable. Every declaration names a lifecycle
    event and carries a callable ``run``; the matcher is validated against the
    permission grammar. Anything malformed raises :class:`HookLoadError`.
    """
    has_hooks = hasattr(module, "HOOKS")
    register = getattr(module, "register", None)
    has_register = callable(register)
    if has_hooks and has_register:
        raise HookLoadError("module defines both HOOKS and register(); ambiguous")
    if has_hooks:
        raw = module.HOOKS
    elif has_register:
        try:
            raw = register()
        except Exception as exc:
            raise HookLoadError(f"register() raised {type(exc).__name__}") from exc
        if inspect.isawaitable(raw):
            raise HookLoadError("register() must be synchronous")
    else:
        raise HookLoadError("module defines neither HOOKS nor register()")

    if raw is None:
        raise HookLoadError("hook declaration is empty")
    try:
        items = list(raw)
    except TypeError as exc:
        raise HookLoadError("hook declarations must be a finite iterable") from exc
    if not items:
        raise HookLoadError("hook declaration is empty")

    specs: list[HookSpec] = []
    for index, item in enumerate(items):
        mapping = _declaration_mapping(item, index)
        event = mapping.get("event")
        try:
            event_name = HookEvent.coerce(event).value
        except HookError as exc:
            raise HookLoadError(f"hook [{index}] {exc}") from exc
        run = mapping.get("run")
        if not callable(run):
            raise HookLoadError(f"hook [{index}] has no callable run")
        matcher = mapping.get("matcher")
        if matcher is not None and not isinstance(matcher, str):
            raise HookLoadError(f"hook [{index}] matcher must be a string")
        if matcher is not None:
            try:
                _validate_matcher(matcher, name=f"{source}#{index}")
            except HookError as exc:
                raise HookLoadError(str(exc)) from exc
        name = mapping.get("name") or f"{Path(source).stem}[{index}]"
        if not isinstance(name, str) or not name:
            raise HookLoadError(f"hook [{index}] name must be a non-empty string")
        timeout = mapping.get("timeout_s", DEFAULT_TIMEOUT_S)
        try:
            timeout_value = _coerce_timeout(timeout)
        except HookError as exc:
            raise HookLoadError(f"hook [{index}] {exc}") from exc
        specs.append(
            HookSpec(
                event=event_name,
                name=name,
                kind="python",
                matcher=matcher,
                on_nonzero=HookOnNonzero.WARN,
                timeout_s=timeout_value,
                disabled=bool(mapping.get("disabled", False)),
                source=source,
                source_sha256=source_sha256,
                index=index,
                order=index,
                generation=generation,
                fn=run,
            )
        )
    return tuple(specs)


class HookModuleLoader:
    """Import validated hook modules under generation-stamped names.

    This is the same seam the tool loader uses: a private, content-addressed
    staged copy is re-hashed immediately before import; the import runs under
    ``nexus_ext.<label>_<hash>__g<generation>``; and a partially-installed
    module is removed from ``sys.modules`` on any failure. It is injectable so
    a test can substitute a double without executing workspace code.
    """

    def __init__(self, *, origin: str = "hook") -> None:
        self._origin = origin
        self._owned: set[str] = set()
        self._lock = threading.RLock()

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def owned_modules(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._owned))

    def owns(self, name: str) -> bool:
        with self._lock:
            return name in self._owned

    def load(self, staged: StagedSource, generation: int) -> HookModule:
        """Import frozen staged bytes into ``generation`` and extract hooks."""
        expected = staged.sha256
        if not expected:
            raise HookLoadError("staged source has no content hash")
        if not self._verify(staged, expected):
            raise HookLoadError("staged source changed before import; refusing")
        module_name = module_name_for(staged.source_identity, generation)
        with self._lock:
            if module_name in sys.modules:
                raise HookLoadError(f"module {module_name!r} is already loaded")
            spec = importlib.util.spec_from_file_location(module_name, staged.path)
            if spec is None or spec.loader is None:
                raise HookLoadError("cannot build an import spec for the staged file")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            self._owned.add(module_name)
            try:
                spec.loader.exec_module(module)
                hooks = extract_hook_declarations(
                    module,
                    source=str(staged.candidate.path),
                    source_sha256=expected,
                    generation=generation,
                )
            except HookLoadError:
                self._discard(module_name)
                raise
            except SyntaxError as exc:
                self._discard(module_name)
                raise HookLoadError(
                    sanitize_text(
                        f"SyntaxError: {exc.msg} (line {exc.lineno or '?'})"
                    )
                ) from exc
            except BaseException as exc:  # arbitrary import-time code
                # Never echo an arbitrary exception's message: it could carry a
                # source body or a credential. The type name is the safe label.
                self._discard(module_name)
                raise HookLoadError(type(exc).__name__) from exc
            handle = ModuleHandle(
                name=module_name,
                path=str(staged.path),
                generation=generation,
                sha256=expected,
                origin=self._origin,
                module=module,
            )
            return HookModule(handle=handle, hooks=hooks, staged=staged)

    def release_module(self, name: str) -> bool:
        if not self.owns(name):
            return False
        with self._lock:
            existed = sys.modules.pop(name, None) is not None
            self._owned.discard(name)
            return existed

    def _discard(self, name: str) -> None:
        with self._lock:
            sys.modules.pop(name, None)
            self._owned.discard(name)

    @staticmethod
    def _verify(staged: StagedSource, expected: str) -> bool:
        try:
            if staged.path.is_symlink() or not staged.path.is_file():
                return False
            return hashlib.sha256(staged.path.read_bytes()).hexdigest() == expected
        except OSError:
            return False


# ---------------------------------------------------------------------------
# The immutable hook set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookSet:
    """One immutable snapshot of the loaded hook world."""

    generation: int = 0
    specs: tuple[HookSpec, ...] = ()
    failures: tuple[HookFailure, ...] = ()
    warnings: tuple[str, ...] = ()
    fingerprint: str = ""

    def for_event(self, event: str) -> tuple[HookSpec, ...]:
        return tuple(spec for spec in self.specs if spec.event == event)

    def as_map(self) -> dict[str, tuple[HookSpec, ...]]:
        """The ``Manifest.hooks``-shaped mapping (event -> specs)."""
        result: dict[str, tuple[HookSpec, ...]] = {}
        for spec in self.specs:
            result.setdefault(spec.event, ())
            result[spec.event] = (*result[spec.event], spec)
        return result

    @property
    def events(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for spec in self.specs:
            seen.setdefault(spec.event, None)
        return tuple(seen)


# ---------------------------------------------------------------------------
# Process execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Capture:
    data: bytes = b""
    total: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class _ProcessOutcome:
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    spawn_error: str = ""


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            if sig == signal.SIGKILL:
                process.kill()


async def _terminate_group(process: asyncio.subprocess.Process) -> None:
    """TERM then KILL the child's whole process group, never a single pid.

    ``start_new_session=True`` makes the child its own group leader, so
    ``killpg(pid, ...)`` reaches every descendant even if the leader exits
    first. Both signals and the reap tolerate an already-gone process.
    """
    if process.returncode is None:
        _signal_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), _GRACE_S)
        except TimeoutError:
            _signal_group(process, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), _KILL_WAIT_S)
    if process.returncode is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), _KILL_WAIT_S)


async def _read_bounded(
    stream: asyncio.StreamReader | None, limit: int
) -> _Capture:
    if stream is None:
        return _Capture()
    buffer = bytearray()
    total = 0
    truncated = False
    while True:
        try:
            chunk = await stream.read(65_536)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        total += len(chunk)
        if len(buffer) < limit:
            room = limit - len(buffer)
            buffer.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        else:
            truncated = True
    return _Capture(data=bytes(buffer), total=total, truncated=truncated)


async def _feed_stdin(
    stream: asyncio.StreamWriter | None, data: bytes
) -> None:
    if stream is None:
        return
    try:
        stream.write(data)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(Exception):
            stream.close()


async def _collect_reader(task: asyncio.Task[_Capture]) -> _Capture:
    try:
        return await asyncio.wait_for(asyncio.shield(task), _DRAIN_S)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return _Capture()


async def _run_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    stdin: bytes,
    timeout_s: float,
    limit: int = MAX_OUTPUT_BYTES,
    cancel: Any | None = None,
) -> _ProcessOutcome:
    """Run argv in its own process group with bounded I/O, deadline, and cancel."""
    if cancel is not None and getattr(cancel, "cancelled", False):
        return _ProcessOutcome(cancelled=True)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return _ProcessOutcome(spawn_error=sanitize_text(f"{type(exc).__name__}: {exc}"))

    out_task = asyncio.ensure_future(_read_bounded(process.stdout, limit))
    err_task = asyncio.ensure_future(_read_bounded(process.stderr, limit))
    in_task = asyncio.ensure_future(_feed_stdin(process.stdin, stdin))
    wait_task = asyncio.ensure_future(process.wait())
    cancel_task: asyncio.Task[Any] | None = None
    waiter = getattr(cancel, "wait", None) if cancel is not None else None
    if callable(waiter):
        cancel_task = asyncio.ensure_future(waiter())

    waiters = [wait_task]
    if cancel_task is not None:
        waiters.append(cancel_task)
    timed_out = False
    cancelled = False
    try:
        done, _ = await asyncio.wait(
            waiters, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if wait_task in done:
            pass
        elif cancel_task is not None and cancel_task in done:
            cancelled = True
            await _terminate_group(process)
        else:
            timed_out = True
            await _terminate_group(process)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_group(process))
        raise
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if not wait_task.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(wait_task), _KILL_WAIT_S)

    out = await _collect_reader(out_task)
    err = await _collect_reader(err_task)
    with contextlib.suppress(Exception):
        await in_task
    return _ProcessOutcome(
        returncode=process.returncode,
        stdout=out.data.decode("utf-8", "replace"),
        stderr=err.data.decode("utf-8", "replace"),
        timed_out=timed_out,
        cancelled=cancelled,
        truncated=out.truncated or err.truncated,
    )


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class HookManager:
    """Discover, load, and run lifecycle hooks.

    Construction is inert: call :meth:`refresh` to read the hook files. A
    refresh that hits a broken file records a sanitized diagnostic and drops
    only that candidate; it never raises and never leaves a half-loaded module
    behind.
    """

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        home: str | os.PathLike[str] | None = None,
        config: Config | None = None,
        hooks_path: str | os.PathLike[str] | None = None,
        hooks_dir: str | os.PathLike[str] | None = None,
        user_hooks_path: str | os.PathLike[str] | None = None,
        user_hooks_dir: str | os.PathLike[str] | None = None,
        quarantine: Quarantine | None = None,
        loader: Any | None = None,
        sink: Any | None = None,
    ) -> None:
        self._workspace = _absolute(workspace)
        self._home = _absolute(home) if home is not None else Path.home()
        self._hooks_path = (
            _absolute(hooks_path)
            if hooks_path is not None
            else self._workspace / ".nexus" / "hooks.toml"
        )
        self._hooks_dir = (
            _absolute(hooks_dir)
            if hooks_dir is not None
            else self._workspace / ".nexus" / "hooks"
        )
        self._user_hooks_path = (
            _absolute(user_hooks_path)
            if user_hooks_path is not None
            else self._home / ".nexus" / "hooks.toml"
        )
        self._user_hooks_dir = (
            _absolute(user_hooks_dir)
            if user_hooks_dir is not None
            else self._home / ".nexus" / "hooks"
        )
        self._quarantine = quarantine or build_default_quarantine(
            config,
            root=self._workspace,
            stage_root=self._workspace / ".nexus" / "stage",
        )
        self._loader = loader or HookModuleLoader()
        self._sink = sink

        self._set = HookSet()
        self._live_modules: dict[str, HookModule] = {}
        self._failures: tuple[HookFailure, ...] = ()
        self._warnings: tuple[str, ...] = ()
        self._closed = False

    # -- introspection -----------------------------------------------------

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def hooks_path(self) -> Path:
        return self._hooks_path

    @property
    def hooks_dir(self) -> Path:
        return self._hooks_dir

    @property
    def generation(self) -> int:
        return self._set.generation

    @property
    def fingerprint(self) -> str:
        return self._set.fingerprint

    @property
    def specs(self) -> tuple[HookSpec, ...]:
        return self._set.specs

    @property
    def hook_set(self) -> HookSet:
        return self._set

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    @property
    def closed(self) -> bool:
        return self._closed

    def specs_for(self, event: str | HookEvent) -> tuple[HookSpec, ...]:
        name = HookEvent.coerce(event).value
        return self._set.for_event(name)

    @property
    def hooks(self) -> dict[str, tuple[HookSpec, ...]]:
        """The live event -> specs mapping (also ``as_manifest_map``)."""
        return self._set.as_map()

    def as_manifest_map(self) -> dict[str, tuple[HookSpec, ...]]:
        """The ordered ``Manifest.hooks`` view of the current set."""
        return self._set.as_map()

    def diagnostics(self) -> tuple[dict[str, Any], ...]:
        rows = [failure.to_dict() for failure in self._failures]
        rows.extend(
            {"kind": "warning", "name": "trusted_code", "error": text}
            for text in self._warnings
        )
        return tuple(rows)

    # -- discovery / refresh ----------------------------------------------

    def refresh(self) -> HookSet:
        """Re-read hook files and rebuild the immutable set.

        Unchanged Python modules are reused object-for-object (their hooks keep
        the same identity and fingerprint), so a no-op refresh neither churns the
        generation nor re-imports working code. A semantic change advances the
        generation and releases the modules no longer present.
        """
        if self._closed:
            raise ManagerClosed("hook manager is closed")
        next_generation = self._set.generation + 1
        command_specs, failures = self._load_command_hooks()
        modules, newly, python_failures = self._load_python_hooks(next_generation)
        failures = [*failures, *python_failures]

        python_specs = [hook for module in modules.values() for hook in module.hooks]
        warnings: list[str] = []
        if python_specs:
            warnings.append(TRUSTED_CODE_WARNING)
        specs = tuple(sorted([*command_specs, *python_specs], key=_order_key))
        fingerprint = _set_fingerprint(specs)

        if fingerprint == self._set.fingerprint and self._set.specs:
            # No semantic change: release anything loaded this pass and keep the
            # exact same set object (and therefore the same HookSpec objects).
            for module in newly:
                self._release_module(module)
            self._failures = tuple(failures)
            self._warnings = tuple(warnings)
            return self._set

        old_modules = self._live_modules
        self._live_modules = modules
        for identity, module in old_modules.items():
            if modules.get(identity) is not module:
                self._release_module(module)
        self._set = HookSet(
            generation=next_generation if specs else 0,
            specs=specs,
            failures=tuple(failures),
            warnings=tuple(warnings),
            fingerprint=fingerprint,
        )
        self._failures = tuple(failures)
        self._warnings = tuple(warnings)
        return self._set

    # -- command hooks -----------------------------------------------------

    def _load_command_hooks(
        self,
    ) -> tuple[list[HookSpec], list[HookFailure]]:
        path = self._hooks_path if self._hooks_path.is_file() else None
        if path is None and self._user_hooks_path.is_file():
            path = self._user_hooks_path
        if path is None:
            return [], []
        try:
            size = path.stat().st_size
        except OSError as exc:
            return [], [_failure("hook", "hooks.toml", exc, path=path)]
        if size > _MAX_TOML_BYTES:
            return [], [
                _message_failure(
                    "hook",
                    "hooks.toml",
                    f"hooks.toml exceeds {_MAX_TOML_BYTES} bytes",
                    path=path,
                )
            ]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [], [_failure("hook", "hooks.toml", exc, path=path)]
        try:
            document = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            return [], [_failure("hook", "hooks.toml", exc, path=path)]
        return self._parse_hooks_document(document, path)

    def _parse_hooks_document(
        self, document: object, path: Path
    ) -> tuple[list[HookSpec], list[HookFailure]]:
        specs: list[HookSpec] = []
        failures: list[HookFailure] = []
        if not isinstance(document, Mapping):
            return [], [
                _message_failure("hook", "hooks.toml", "hooks.toml must be a table", path=path)
            ]
        unknown = sorted(set(document) - {"hooks"})
        if unknown:
            return [], [
                _message_failure(
                    "hook",
                    "hooks.toml",
                    "unknown top-level keys: " + ", ".join(unknown),
                    path=path,
                )
            ]
        table = document.get("hooks", {})
        if not isinstance(table, Mapping):
            return [], [
                _message_failure("hook", "hooks.toml", "[hooks] must be a table", path=path)
            ]
        for event, entries in table.items():
            if not HookEvent.is_valid(event):
                failures.append(
                    _message_failure("hook", str(event), "unknown hook event", path=path)
                )
                continue
            if not isinstance(entries, list):
                failures.append(
                    _message_failure("hook", str(event), "hooks must be an array of tables", path=path)
                )
                continue
            for index, entry in enumerate(entries):
                spec = self._parse_hook_entry(event, index, entry, path, failures)
                if spec is not None:
                    specs.append(spec)
        return specs, failures

    def _parse_hook_entry(
        self,
        event: str,
        index: int,
        entry: object,
        path: Path,
        failures: list[HookFailure],
    ) -> HookSpec | None:
        label = f"{event}[{index}]"
        if not isinstance(entry, Mapping):
            failures.append(_message_failure("hook", label, "hook entry must be a table", path=path))
            return None
        unknown = sorted(set(entry) - _ALLOWED_ENTRY_KEYS)
        if unknown:
            failures.append(
                _message_failure("hook", label, "unknown keys: " + ", ".join(unknown), path=path)
            )
            return None
        kind = entry.get("type", "command")
        if kind != "command":
            failures.append(
                _message_failure(
                    "hook",
                    label,
                    "type must be 'command'; in-process hooks live in .nexus/hooks/*.py",
                    path=path,
                )
            )
            return None
        name = entry.get("name", label)
        if not isinstance(name, str) or not name:
            failures.append(_message_failure("hook", label, "name must be a non-empty string", path=path))
            return None
        matcher = entry.get("matcher")
        if matcher is not None:
            if not isinstance(matcher, str):
                failures.append(_message_failure("hook", label, "matcher must be a string", path=path))
                return None
            try:
                _validate_matcher(matcher, name=label)
            except HookError as exc:
                failures.append(_message_failure("hook", label, str(exc), path=path))
                return None
        shell = entry.get("shell", False)
        if not isinstance(shell, bool):
            failures.append(_message_failure("hook", label, "shell must be a bool", path=path))
            return None
        command = entry.get("command")
        argv: tuple[str, ...] | None = None
        shell_command: str | None = None
        try:
            if shell:
                if not isinstance(command, str) or not command.strip():
                    raise HookError("a shell hook needs a non-empty command string")
                shell_command = command
            else:
                argv = _parse_argv(command)
        except HookError as exc:
            failures.append(_message_failure("hook", label, str(exc), path=path))
            return None
        try:
            on_nonzero = HookOnNonzero.coerce(entry.get("on_nonzero", "warn"))
            timeout_s = _coerce_timeout(entry.get("timeout_s", DEFAULT_TIMEOUT_S))
        except HookError as exc:
            failures.append(_message_failure("hook", label, str(exc), path=path))
            return None
        disabled = entry.get("disabled", False)
        if not isinstance(disabled, bool):
            failures.append(_message_failure("hook", label, "disabled must be a bool", path=path))
            return None
        try:
            env = _parse_env(entry.get("env"))
        except HookError as exc:
            failures.append(_message_failure("hook", label, str(exc), path=path))
            return None
        cwd = entry.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd):
            failures.append(_message_failure("hook", label, "cwd must be a non-empty string", path=path))
            return None
        if isinstance(cwd, str):
            candidate = Path(cwd)
            if not candidate.is_absolute():
                cwd = str(self._workspace / candidate)
        return HookSpec(
            event=event,
            name=name,
            kind="command",
            matcher=matcher,
            command=argv,
            shell_command=shell_command,
            shell=shell,
            on_nonzero=on_nonzero,
            timeout_s=timeout_s,
            disabled=disabled,
            env=env,
            cwd=cwd,
            source=str(path),
            index=index,
            order=index,
        )

    # -- python hooks ------------------------------------------------------

    def _python_candidates(self) -> list[tuple[Path, int]]:
        """Discover ``.py`` hooks with workspace-over-user precedence by stem."""
        found: dict[str, tuple[Path, int]] = {}
        tiers = ((self._hooks_dir, 2), (self._user_hooks_dir, 1))
        for directory, tier in tiers:
            if not directory.is_dir():
                continue
            try:
                entries = sorted(directory.glob("*.py"), key=lambda p: (p.name.casefold(), p.name))
            except OSError:  # pragma: no cover - listing race
                continue
            for path in entries:
                if path.name.startswith("_"):
                    continue
                if not path.is_file():
                    continue
                absolute = _absolute(path)
                key = absolute.stem.casefold()
                existing = found.get(key)
                if existing is None or tier > existing[1]:
                    found[key] = (absolute, tier)
        return sorted(found.values(), key=lambda item: (-item[1], str(item[0])))

    def _load_python_hooks(
        self, generation: int
    ) -> tuple[dict[str, HookModule], list[HookModule], list[HookFailure]]:
        modules: dict[str, HookModule] = {}
        newly: list[HookModule] = []
        failures: list[HookFailure] = []
        for path, _tier in self._python_candidates():
            try:
                probe = self._quarantine.open(path, origin="hook")
            except Exception as exc:  # noqa: BLE001 - any refusal is a failure
                failures.append(_failure("hook", path.stem, exc, path=path))
                continue
            identity = probe.source_identity
            previous = self._live_modules.get(identity)
            if previous is not None and previous.handle.sha256 == probe.sha256:
                modules[identity] = previous
                continue
            try:
                private = self._quarantine.stage(probe)
            except Exception as exc:  # noqa: BLE001
                failures.append(_failure("hook", path.stem, exc, path=path))
                continue
            try:
                module = self._loader.load(private, generation)
            except Exception as exc:  # noqa: BLE001 - loader failures are isolated
                with contextlib.suppress(Exception):
                    self._quarantine.discard_staged(private)
                failures.append(_failure("hook", path.stem, exc, path=path))
                continue
            modules[identity] = module
            newly.append(module)
        return modules, newly, failures

    def _release_module(self, module: HookModule) -> None:
        with contextlib.suppress(Exception):
            self._loader.release_module(module.handle.name)
        if module.staged is not None:
            with contextlib.suppress(Exception):
                self._quarantine.discard_staged(module.staged)

    # -- running -----------------------------------------------------------

    async def run(
        self,
        event: str | HookEvent,
        invocation: HookInvocation | Mapping[str, Any],
        *,
        cancel: Any | None = None,
        specs: Sequence[HookSpec] | None = None,
    ) -> HookOutcome:
        """Run every matching hook for one event, in declaration order.

        ``specs`` overrides the live set for this call, so a caller holding a
        pinned manifest generation can run exactly the hooks that generation
        declared even if a concurrent refresh has already swapped the live set.
        """
        hook_event = HookEvent.coerce(event)
        if isinstance(invocation, Mapping):
            payload = dict(invocation)
            payload.pop("event", None)
            invocation = HookInvocation(event=hook_event.value, **payload)
        if not isinstance(invocation, HookInvocation):
            raise HookError("invocation must be a HookInvocation or mapping")
        if invocation.event != hook_event.value:
            invocation = replace(invocation, event=hook_event.value)

        specs = (
            tuple(specs)
            if specs is not None
            else self._set.for_event(hook_event.value)
        )
        original = dict(invocation.tool_input)
        working = dict(original)
        decisions: list[HookDecision] = []
        warnings: list[str] = []
        fired: list[str] = []
        failures: list[str] = []
        block_reason = ""

        for spec in specs:
            if spec.disabled:
                continue
            if not spec.matches(invocation.tool, invocation.key, invocation.bundle):
                continue
            current = invocation.with_tool_input(working)
            try:
                decision = await self._run_hook(spec, current, cancel=cancel)
            except OperationCancelled:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a broken hook never breaks a run
                failures.append(
                    sanitize_text(f"{spec.name}: {type(exc).__name__}: {exc}")
                )
                decision = HookDecision.warn(
                    f"hook {spec.name!r} failed: {type(exc).__name__}",
                    hook=spec.name,
                    event=spec.event,
                )
            decision = decision.with_source(hook=spec.name, event=spec.event)
            decisions.append(decision)
            fired.append(spec.name)
            await self._emit(
                "hook.fired",
                {
                    "event": spec.event,
                    "hook": spec.name,
                    "kind": spec.kind,
                    "action": decision.action.value,
                },
            )
            if decision.action is HookAction.BLOCK:
                block_reason = decision.reason or f"blocked by hook {spec.name!r}"
                await self._emit(
                    "hook.blocked",
                    {
                        "event": spec.event,
                        "hook": spec.name,
                        "reason": sanitize_text(block_reason),
                    },
                )
                break
            if decision.action is HookAction.MODIFY:
                if hook_event.value in MODIFIABLE_EVENTS and isinstance(
                    decision.new_input, Mapping
                ):
                    working = dict(decision.new_input)
                else:
                    warnings.append(
                        sanitize_text(
                            f"hook {spec.name!r} returned modify for non-modifiable "
                            f"event {spec.event}; ignored"
                        )
                    )
            elif decision.action is HookAction.WARN and decision.reason:
                warnings.append(sanitize_text(decision.reason))

        modified = working != original
        if block_reason:
            action = HookAction.BLOCK
        elif modified:
            action = HookAction.MODIFY
        elif warnings:
            action = HookAction.WARN
        else:
            action = HookAction.ALLOW
        return HookOutcome(
            event=hook_event.value,
            decision=action,
            reason=block_reason,
            original_input=original,
            modified_input=working if modified else None,
            decisions=tuple(decisions),
            warnings=tuple(warnings),
            fired=tuple(fired),
            failures=tuple(failures),
        )

    async def _run_hook(
        self,
        spec: HookSpec,
        invocation: HookInvocation,
        *,
        cancel: Any | None,
    ) -> HookDecision:
        if spec.is_python:
            return await self._run_python(spec, invocation, cancel=cancel)
        return await self._run_command(spec, invocation, cancel=cancel)

    async def _run_command(
        self,
        spec: HookSpec,
        invocation: HookInvocation,
        *,
        cancel: Any | None,
    ) -> HookDecision:
        payload = invocation.to_json().encode("utf-8")
        if len(payload) > MAX_STDIN_BYTES:
            return _nonzero_decision(
                spec,
                f"hook input exceeds {MAX_STDIN_BYTES} bytes",
                timed_out=False,
            )
        env = _build_command_env(spec, invocation)
        argv = _expand_argv(spec.argv, env) if not spec.shell else spec.argv
        outcome = await _run_process(
            argv,
            env=env,
            cwd=spec.cwd,
            stdin=payload,
            timeout_s=spec.timeout_s,
            limit=MAX_OUTPUT_BYTES,
            cancel=cancel,
        )
        if outcome.cancelled:
            reason = getattr(cancel, "reason", None)
            raise OperationCancelled(reason or "hook cancelled")
        return _decision_from_process(spec, outcome)

    async def _run_python(
        self,
        spec: HookSpec,
        invocation: HookInvocation,
        *,
        cancel: Any | None,
    ) -> HookDecision:
        if cancel is not None and getattr(cancel, "cancelled", False):
            raise OperationCancelled(getattr(cancel, "reason", None) or "hook cancelled")
        ctx = HookContext(
            workspace=self._workspace,
            event=spec.event,
            session_id=invocation.session_id,
            turn_id=invocation.turn_id,
        )
        call = spec.fn
        try:
            if inspect.iscoroutinefunction(call):
                result = await asyncio.wait_for(call(invocation, ctx), spec.timeout_s)
            else:
                loop = asyncio.get_running_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, call, invocation, ctx),
                    spec.timeout_s,
                )
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, spec.timeout_s)
        except TimeoutError as exc:
            raise HookError(
                f"python hook {spec.name!r} timed out after {spec.timeout_s:g}s"
            ) from exc
        return _decision_from_python(result, spec)

    # -- convenience entry points -----------------------------------------

    async def pre_tool_use(
        self,
        *,
        tool: str,
        key: str | None = None,
        bundle: str | None = None,
        tool_input: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        cancel: Any | None = None,
    ) -> HookOutcome:
        invocation = HookInvocation(
            event=HookEvent.PRE_TOOL_USE.value,
            tool=tool,
            key=key,
            bundle=bundle,
            tool_input=dict(tool_input or {}),
            session_id=session_id,
            turn_id=turn_id,
        )
        return await self.run(HookEvent.PRE_TOOL_USE, invocation, cancel=cancel)

    async def post_tool_use(
        self,
        *,
        tool: str,
        key: str | None = None,
        bundle: str | None = None,
        tool_input: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        cancel: Any | None = None,
    ) -> HookOutcome:
        invocation = HookInvocation(
            event=HookEvent.POST_TOOL_USE.value,
            tool=tool,
            key=key,
            bundle=bundle,
            tool_input=dict(tool_input or {}),
            session_id=session_id,
            turn_id=turn_id,
        )
        return await self.run(HookEvent.POST_TOOL_USE, invocation, cancel=cancel)

    async def dispatch(
        self,
        event: str | HookEvent,
        *,
        data: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        cancel: Any | None = None,
    ) -> HookOutcome:
        """Run a non-tool lifecycle event (SessionStart, TurnEnd, ...)."""
        hook_event = HookEvent.coerce(event)
        invocation = HookInvocation(
            event=hook_event.value,
            session_id=session_id,
            turn_id=turn_id,
            data=dict(data or {}),
        )
        return await self.run(hook_event, invocation, cancel=cancel)

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Release every loaded module. Idempotent and terminal."""
        if self._closed:
            return
        self._closed = True
        for module in self._live_modules.values():
            self._release_module(module)
        self._live_modules = {}
        self._set = HookSet()
        self._failures = ()
        self._warnings = ()

    # -- events ------------------------------------------------------------

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        sink = self._sink
        if sink is None:
            return
        event = Event(type=event_type, data=dict(data))
        try:
            if hasattr(sink, "publish"):
                outcome = sink.publish(event)
            elif hasattr(sink, "emit"):
                outcome = sink.emit(event)
            elif callable(sink):
                outcome = sink(event)
            else:
                return
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - a broken sink never breaks a run
            return


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _coerce_timeout(value: object) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_S
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise HookError("timeout_s must be a positive finite number")
    if value > MAX_TIMEOUT_S:
        raise HookError(f"timeout_s must not exceed {MAX_TIMEOUT_S:g}")
    return float(value)


def _parse_argv(command: object) -> tuple[str, ...]:
    if isinstance(command, str):
        if not command.strip():
            raise HookError("command must be a non-empty string")
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise HookError(f"command could not be parsed: {exc}") from exc
    elif isinstance(command, (list, tuple)):
        parts = list(command)
    else:
        raise HookError("command must be a string or an array of strings")
    if not parts:
        raise HookError("command must not be empty")
    if len(parts) > _MAX_ARGS:
        raise HookError(f"command has more than {_MAX_ARGS} arguments")
    argv: list[str] = []
    for part in parts:
        if not isinstance(part, str) or not part:
            raise HookError("command arguments must be non-empty strings")
        if "\x00" in part:
            raise HookError("command arguments must not contain a NUL byte")
        if len(part.encode("utf-8", "surrogatepass")) > _MAX_ARG_BYTES:
            raise HookError(f"a command argument exceeds {_MAX_ARG_BYTES} bytes")
        argv.append(part)
    if sum(len(part.encode("utf-8", "surrogatepass")) for part in argv) > _MAX_COMMAND_BYTES:
        raise HookError(f"command exceeds {_MAX_COMMAND_BYTES} bytes")
    return tuple(argv)


def _parse_env(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise HookError("env must be a table of strings")
    if len(value) > _MAX_ENV_KEYS:
        raise HookError(f"env has more than {_MAX_ENV_KEYS} entries")
    env: dict[str, str] = {}
    total = 0
    for key, item in value.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise HookError("env keys must be non-empty names without '='")
        if not isinstance(item, str) or "\x00" in item:
            raise HookError("env values must be strings without a NUL byte")
        total += len(key.encode("utf-8", "surrogatepass")) + len(
            item.encode("utf-8", "surrogatepass")
        )
        env[key] = item
    if total > _MAX_ENV_BYTES:
        raise HookError(f"env exceeds {_MAX_ENV_BYTES} bytes")
    return env


def _build_command_env(spec: HookSpec, invocation: HookInvocation) -> dict[str, str]:
    """The bounded environment handed to a command hook.

    Starts from a tiny allow-list of the host environment (never the whole of
    ``os.environ``, so a host credential cannot be read by a hook), adds the
    explicit ``NEXUS_*`` context, then the hook's declared overlay.
    """
    env: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(invocation.env())
    env.update(spec.env)
    return env


#: ``$VAR`` / ``${VAR}`` references, expanded harness-side from the bounded hook
#: environment only. This is not a shell: an unknown name is left untouched and
#: no command substitution, pipe, or glob is ever interpreted.
_VAR_REFERENCE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _expand_argv(argv: Sequence[str], env: Mapping[str, str]) -> tuple[str, ...]:
    """Expand ``$NEXUS_*`` (and declared env) references in an argv, safely.

    The plan's example command is ``ruff check --stdin-filename $NEXUS_TOOL_PATH
    -``. A command hook runs without a shell, so the harness performs the
    substitution itself -- from the bounded environment only, never from
    ``os.environ``, and never by interpreting shell syntax.
    """

    def _replace(match: Any) -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))

    return tuple(_VAR_REFERENCE.sub(_replace, part) for part in argv)


# ---------------------------------------------------------------------------
# Decision parsing
# ---------------------------------------------------------------------------


def _nonzero_decision(
    spec: HookSpec, reason: str, *, timed_out: bool
) -> HookDecision:
    text = sanitize_text(reason)
    if spec.on_nonzero is HookOnNonzero.BLOCK:
        return HookDecision.block(text, hook=spec.name, event=spec.event)
    if spec.on_nonzero is HookOnNonzero.IGNORE:
        return HookDecision.allow(hook=spec.name, event=spec.event)
    return HookDecision.warn(text, hook=spec.name, event=spec.event)


def _decision_from_process(spec: HookSpec, outcome: _ProcessOutcome) -> HookDecision:
    if outcome.spawn_error:
        return _nonzero_decision(spec, outcome.spawn_error, timed_out=False)
    if not outcome.truncated and outcome.stdout.strip():
        payload = _parse_json_object(outcome.stdout)
        if payload is not None:
            decision = _decision_from_payload(payload, spec)
            if decision is not None:
                return decision
    if outcome.timed_out:
        return _nonzero_decision(
            spec, f"hook timed out after {spec.timeout_s:g}s", timed_out=True
        )
    if outcome.returncode == 0:
        return HookDecision.allow(hook=spec.name, event=spec.event)
    detail = ""
    if outcome.stderr.strip():
        detail = outcome.stderr.strip().splitlines()[-1]
    reason = detail or f"hook exited with code {outcome.returncode}"
    return _nonzero_decision(spec, reason, timed_out=False)


def _decision_from_python(value: object, spec: HookSpec) -> HookDecision:
    if value is None:
        return HookDecision.allow(hook=spec.name, event=spec.event)
    if isinstance(value, HookDecision):
        return value
    if isinstance(value, bool):
        if value:
            return HookDecision.allow(hook=spec.name, event=spec.event)
        return HookDecision.block(
            f"hook {spec.name!r} returned False", hook=spec.name, event=spec.event
        )
    if isinstance(value, Mapping):
        decision = _decision_from_payload(value, spec)
        if decision is not None:
            return decision
        return HookDecision.allow(hook=spec.name, event=spec.event)
    if isinstance(value, (HookAction, str)):
        action = HookAction.coerce(value)
        if action is HookAction.MODIFY:
            raise HookError("a python hook must return new_input for a modify decision")
        if action is HookAction.BLOCK:
            return HookDecision.block(
                f"blocked by hook {spec.name!r}", hook=spec.name, event=spec.event
            )
        if action is HookAction.WARN:
            return HookDecision.warn(
                f"warned by hook {spec.name!r}", hook=spec.name, event=spec.event
            )
        return HookDecision.allow(hook=spec.name, event=spec.event)
    raise HookError(
        f"python hook {spec.name!r} returned an unsupported {type(value).__name__}"
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    start = stripped.find("{")
    if start < 0:
        return None
    candidate = stripped[start:]
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _decision_from_payload(
    payload: Mapping[str, Any], spec: HookSpec
) -> HookDecision | None:
    raw_action = payload.get("decision", payload.get("action"))
    if raw_action is None:
        return None
    try:
        action = HookAction.coerce(raw_action)
    except HookError:
        return None
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)
    reason = sanitize_text(reason)
    if action is HookAction.MODIFY:
        new_input = payload.get("input", payload.get("new_input", payload.get("tool_input")))
        if not isinstance(new_input, Mapping):
            raise HookError("a modify decision needs an 'input' object")
        return HookDecision.modify(
            dict(new_input), reason=reason, hook=spec.name, event=spec.event
        )
    if action is HookAction.BLOCK:
        return HookDecision.block(
            reason or f"blocked by hook {spec.name!r}",
            hook=spec.name,
            event=spec.event,
        )
    if action is HookAction.WARN:
        return HookDecision.warn(
            reason or f"warned by hook {spec.name!r}",
            hook=spec.name,
            event=spec.event,
        )
    return HookDecision.allow(hook=spec.name, event=spec.event)
