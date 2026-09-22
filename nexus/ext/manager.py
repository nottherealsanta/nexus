"""The serialized, atomic hot-reload orchestrator (plan sections 6.1-6.4).

:class:`ExtensionManager` owns everything a live generation is built from and
turns a single rebuild into exactly one immutable :class:`Manifest`. It is the
only writer of the :class:`ManifestRef` the loop reads, so every kind of trigger
-- an API call, the ``ReloadExtensions`` tool, or the directory watcher -- funnels
through the same serialized, coalescing transaction:

1. **load effective config.** A config that fails to load is itself a failed
   rebuild: the previous manifest is kept.
2. **discover.** Workspace and user tool roots are scanned with workspace
   precedence; system files are selected by config; skills are re-discovered
   through a persistent :class:`~nexus.skills.manager.SkillManager` so an
   unchanged skill is reused as the *same object*.
3. **reuse or re-quarantine.** An extension whose frozen bytes are unchanged is
   reused (same :class:`ModuleRecord`, same tools, same module name) without
   running quarantine again. A changed file is re-read and re-quarantined; the
   loader imports those exact bytes under a fresh, generation-stamped name.
4. **all-or-nothing.** If any candidate fails -- quarantine, import, a name
   collision, or an unreadable system file -- the rebuild is aborted, every
   module loaded *by this attempt* is released, and the previous manifest is
   left untouched. There is never a partial swap.
5. **CAS swap exactly once.** A new immutable manifest is diffed against the
   current generation. A semantic change is installed with a single
   compare-and-swap; a no-op performs no swap and therefore never churns the
   generation.
6. **retire after leases.** Modules for a superseded generation are released only
   from :meth:`ManifestRef`'s ``on_retire`` callback, i.e. after the last
   :class:`ManifestLease` pin is gone. A cleanup failure is retained on the ref,
   never rolled back.

Concurrency is explicit. ``reload()`` is serialized by one ``asyncio.Lock`` and
coalesces concurrent requests: callers that arrive while a rebuild is running
share the next rebuild instead of each forcing one, and a stale swap can never
overwrite a newer generation. Blocking work (file reads, the isolated import)
runs in a worker thread so the event loop is not held.

Events (``ext.loaded`` / ``ext.unloaded`` / ``ext.failed`` /
``ext.manifest_changed``) are emitted through a deliberately defensive seam that
accepts a :class:`~nexus.core.bus.Bus`, a sync callable, or an async callable; a
sink that raises or closes can never break a reload. Every report and event is
JSON-safe, credential-free, control-character-free, and never carries a source
body.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config, resolve_within
from ..config.schema import ExtSection
from ..core.watch import DirectoryWatcher
from ..errors import ConfigError, ManagerClosed, StaleGenerationError
from ..events import Event
from ..skills.manager import SkillManager
from ..tools.loader import ModuleRecord, ToolLoader, build_default_quarantine
from ..tools.spec import RegisteredTool
from .manifest import (
    EqualGenerationError,
    Manifest,
    ManifestDiff,
    ManifestRef,
    ReloadFailure,
    ReloadReport,
    SkillToolSet,
    SystemFile,
    SystemFiles,
)
from .quarantine import Quarantine, QuarantineOutcome, StagedSource, sanitize_text
from .template import ensure_tool_template

__all__ = ["ExtensionManager", "LoadedExtension"]

#: Discovery precedence: workspace shadows user shadows anything else.
_TIER_WORKSPACE = 2
_TIER_USER = 1
_TIER_OTHER = 0

#: The MCP server definition file, relative to the workspace. Its set of servers
#: is reconciled hot on every rebuild; editing it takes effect live.
_MCP_CONFIG_RELATIVE = ".nexus/mcp.json"
#: A definition file larger than this is refused rather than read (it carries
#: command/env/url strings, never a payload).
_MCP_MAX_BYTES = 262_144


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments and trailing commas from JSONC.

    ``.nexus/mcp.json`` is shown as JSONC in the plan, so a human may add a
    comment. Only comments and trailing commas are relaxed; the decoded value is
    then parsed as strict JSON (duplicate keys and ``NaN``/``Infinity`` are
    refused). A ``//`` or ``/*`` inside a string literal is left alone.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    cleaned = "".join(out)
    return _remove_trailing_commas(cleaned)


def _remove_trailing_commas(text: str) -> str:
    """Drop a comma that is followed only by whitespace and a closing bracket."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            look = index + 1
            while look < length and text[look] in " \t\r\n":
                look += 1
            if look < length and text[look] in "}]":
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _strict_json_object(text: str) -> Any:
    """Parse ``text`` as JSONC for comments/trailing commas, then strict JSON.

    Python's default ``json`` reader accepts ``NaN``/``Infinity`` and silently
    keeps the last of duplicate object keys. Neither is valid JSON or a safe
    configuration semantic, so a definition document is refused instead of
    guessed at. Comments and trailing commas are the only relaxations.
    """

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError(f"duplicate key {key!r}")
            seen[key] = value
        return seen

    def _constant(name: str) -> Any:
        raise ValueError(f"non-standard JSON constant {name!r}")

    return json.loads(
        _strip_jsonc(text), object_pairs_hook=_pairs, parse_constant=_constant
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _ext_section(config: Config) -> ExtSection:
    """The ``[ext]`` section, or its built-in defaults for a v1 config."""
    v2 = getattr(config, "v2", None)
    ext = getattr(v2, "ext", None)
    return ext if isinstance(ext, ExtSection) else ExtSection()


def _config_equal(left: Config, right: Config) -> bool:
    """Whether two configs resolve to the same meaning.

    ``Config`` equality deliberately excludes the v2 section (which can carry
    credentials), so the v2 structs are compared explicitly. Comparing only to
    decide *object reuse* is safe: no value is copied into a report or event.
    """
    if left is right:
        return True
    try:
        if left != right:
            return False
        return getattr(left, "v2", None) == getattr(right, "v2", None)
    except Exception:  # noqa: BLE001 - a bad comparison must not fail a reload
        return False


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _expand_dir(value: object, workspace: Path, home: Path) -> Path:
    """Expand one ``[ext].dirs`` entry against ``workspace``/``home``."""
    text = str(value)
    if text == "~":
        text = str(home)
    elif text.startswith("~/"):
        text = str(home / text[2:])
    path = Path(text)
    if not path.is_absolute():
        path = workspace / path
    return _absolute(path)


def _tier_for(path: Path, workspace: Path, home: Path) -> int:
    workspace = _absolute(workspace)
    home = _absolute(home)
    try:
        if path.is_relative_to(workspace):
            return _TIER_WORKSPACE
        if path.is_relative_to(home):
            return _TIER_USER
    except ValueError:  # pragma: no cover - is_relative_to rarely raises
        pass
    return _TIER_OTHER


def _config_failure(exc: BaseException) -> ReloadFailure:
    return ReloadFailure(
        kind="config",
        name="config",
        error=sanitize_text(str(exc)),
        error_type=type(exc).__name__,
    )


def _refusal_failure(path: Path, exc: BaseException) -> ReloadFailure:
    code = getattr(exc, "code", None)
    error_type = getattr(code, "value", None) or type(exc).__name__
    return ReloadFailure(
        kind="ext",
        name=path.stem,
        error=sanitize_text(str(exc)),
        error_type=str(error_type),
        path=sanitize_text(str(path), limit=200),
    )


def _outcome_failure(path: Path, outcome: QuarantineOutcome) -> ReloadFailure:
    # ``outcome.code`` is the stable refusal reason (``import_timeout``,
    # ``bad_spec``, ...); ``error_type`` on the raw outcome is often just the
    # internal carrier (``IsolatedImport``), so the code is the better label.
    return ReloadFailure(
        kind=outcome.kind or "ext",
        name=path.stem,
        error=sanitize_text(outcome.detail or outcome.error_type or str(outcome.code)),
        error_type=str(outcome.code),
        path=sanitize_text(str(path), limit=200),
    )


def _safe_skill_fingerprint(skill: object) -> str:
    """The skill's own fingerprint, or ``""`` when it has no stable one."""
    method = getattr(skill, "fingerprint", None)
    if not callable(method):
        return ""
    try:
        value = method()
    except Exception:  # noqa: BLE001 - a bad fingerprint must not fail a reload
        return ""
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Internal data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedExtension:
    """One live external module: its provenance, tools, and private bytes."""

    source_id: str
    record: ModuleRecord
    tools: tuple[RegisteredTool, ...]
    staged: StagedSource | None = None


@dataclass
class _BuildResult:
    """The pure outcome of one (thread-run) build attempt."""

    config: Config | None = None
    candidate: Manifest | None = None
    diff: ManifestDiff = field(default_factory=ManifestDiff)
    changed: bool = False
    failures: tuple[ReloadFailure, ...] = ()
    #: Non-fatal shadow diagnostics (a higher-precedence tool hiding a
    #: lower-precedence one). Recorded, never fatal.
    shadows: tuple[ReloadFailure, ...] = ()
    loaded: dict[str, LoadedExtension] = field(default_factory=dict)
    newly_loaded: tuple[LoadedExtension, ...] = ()
    #: Skill-scoped bundled tools: loaded modules keyed by source identity, the
    #: immutable association per skill, and the modules first loaded by this
    #: attempt (so a failure releases exactly those).
    skill_loaded: dict[str, LoadedExtension] = field(default_factory=dict)
    skill_newly_loaded: tuple[LoadedExtension, ...] = ()
    skill_tools: dict[str, SkillToolSet] = field(default_factory=dict)

    @property
    def newly_loaded_all(self) -> tuple[LoadedExtension, ...]:
        return (*self.newly_loaded, *self.skill_newly_loaded)


class ExtensionManager:
    """Owns the extension world and performs the one serialized rebuild."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        home: str | os.PathLike[str] | None = None,
        config: Config | None = None,
        config_loader: Callable[[], Config] | None = None,
        builtin_tools: Sequence[RegisteredTool] | None = None,
        loader: ToolLoader | None = None,
        quarantine: Quarantine | None = None,
        skills: SkillManager | None = None,
        mcp: Any | None = None,
        sink: Any | None = None,
    ) -> None:
        self._workspace = _absolute(Path(workspace))
        self._home = _absolute(Path(home)) if home is not None else Path.home()
        self._config_loader = config_loader or self._default_config_loader
        self._loader = loader or ToolLoader()
        self._quarantine = quarantine
        self._skills = skills or SkillManager.for_workspace(
            self._workspace, home=self._home
        )
        #: The MCP manager (duck-typed) whose immutable snapshot this manager
        #: folds into each pinned generation. ``None`` keeps MCP off entirely;
        #: ``nexus.ext`` never imports ``nexus.mcp`` for it.
        self._mcp = mcp
        #: Definition-parse / hot-apply failures from the last MCP sync. They are
        #: diagnostics only: a broken or dead MCP server must never fail a
        #: rebuild or a turn, so they are never added to ``_BuildResult.failures``.
        self._mcp_failures: tuple[ReloadFailure, ...] = ()
        #: The single, long-lived ``ReadMcpResource`` tool. Built once on the
        #: loop (its resolver reads live server state) so every generation reuses
        #: the same object and the manifest diff never churns on it.
        self._read_resource_tool: Any | None = None
        self._sink = sink

        tools = tuple(builtin_tools or ())
        self._builtin_tools = tools
        self._builtin_by_name: dict[str, RegisteredTool] = {t.name: t for t in tools}
        self._builtin_folded = frozenset(t.name.casefold() for t in tools)

        initial = config if config is not None else self._load_config_or_none()
        if initial is None:
            initial = Config()
        self._last_config: Config = initial

        self._ref = ManifestRef(
            Manifest(
                generation=0,
                config=initial,
                tools=dict(self._builtin_by_name),
            ),
            on_retire=self._retire_manifest,
        )

        self._loaded: dict[str, LoadedExtension] = {}
        self._skill_loaded: dict[str, LoadedExtension] = {}
        self._staged_paths: dict[str, Path] = {}
        self._failures: tuple[ReloadFailure, ...] = ()
        #: Non-fatal shadow diagnostics from the last successful build.
        self._shadows: tuple[ReloadFailure, ...] = ()
        #: Private staged copies created by the build currently running. A build
        #: that is cancelled or raises unexpectedly unlinks exactly these, so no
        #: validated-but-unexecuted artifact is left behind.
        self._build_staged: list[StagedSource] = []
        #: The worker future of a build still running after its reload was
        #: cancelled. A later reload drains it before starting a second build, so
        #: two builds never touch the loader concurrently.
        self._inflight_build: asyncio.Future | None = None
        self._state_lock = threading.RLock()

        self._lock = asyncio.Lock()
        self._request_seq = 0
        self._served_seq = 0
        self._last_report: ReloadReport | None = None

        self._watch_task: asyncio.Task[None] | None = None
        self._watch_stop: asyncio.Event | None = None
        self._watch_sink: Any | None = None
        self._watchers: tuple[DirectoryWatcher, ...] = ()
        self._watch_sig: tuple[Any, ...] | None = None
        #: Terminal flag. Set once by :meth:`aclose`; no reload may start and no
        #: compare-and-swap may run after it is set.
        self._closed = False

    # -- construction helpers ---------------------------------------------

    def _default_config_loader(self) -> Config:
        return Config.load(self._workspace, home=self._home)

    def _load_config_or_none(self) -> Config | None:
        try:
            loaded = self._config_loader()
        except Exception:  # noqa: BLE001 - best-effort initial config
            return None
        return loaded if isinstance(loaded, Config) else None

    # -- introspection -----------------------------------------------------

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def home(self) -> Path:
        return self._home

    @property
    def ref(self) -> ManifestRef:
        return self._ref

    @property
    def manifest(self) -> Manifest:
        return self._ref.get()

    @property
    def generation(self) -> int:
        return self._ref.generation

    @property
    def loader(self) -> ToolLoader:
        return self._loader

    @property
    def skills(self) -> SkillManager:
        return self._skills

    @property
    def last_report(self) -> ReloadReport | None:
        return self._last_report

    @property
    def closed(self) -> bool:
        """Whether :meth:`aclose` has run; a closed manager is terminal."""
        return self._closed

    @property
    def cleanup_failures(self) -> tuple[Any, ...]:
        """Cleanup failures retained by the ref (never rolled back)."""
        return self._ref.cleanup_failures

    def list_extensions(self) -> tuple[dict[str, Any], ...]:
        """A sanitized, JSON-safe view of the live external modules."""
        manifest = self.manifest
        rows: list[dict[str, Any]] = []
        for name in sorted(manifest.modules):
            handle = manifest.modules[name]
            rows.append(
                {
                    "name": name,
                    "path": sanitize_text(handle.path, limit=200),
                    "sha256": handle.sha256,
                    "generation": handle.generation,
                    "origin": handle.origin,
                }
            )
        return tuple(rows)

    def diagnostics(self) -> tuple[dict[str, Any], ...]:
        """Last reload's failures plus skill diagnostics and cleanup failures."""
        rows: list[dict[str, Any]] = []
        for failure in self._failures:
            rows.append(dict(failure.to_dict()))
        for shadow in self._shadows:
            rows.append(dict(shadow.to_dict()))
        for failure in self._ref.cleanup_failures:
            row = failure.to_dict()
            row["kind"] = "cleanup"
            rows.append(row)
        for diagnostic in self._skills.diagnostics:
            rows.append(
                {
                    "kind": "skill",
                    "name": getattr(diagnostic, "name", None),
                    "code": str(getattr(diagnostic, "code", "")),
                    "message": sanitize_text(str(getattr(diagnostic, "message", ""))),
                    "path": sanitize_text(
                        str(getattr(diagnostic, "path", "") or ""), limit=200
                    ),
                }
            )
        for failure in self._mcp_failures:
            row = failure.to_dict()
            row["kind"] = "mcp"
            rows.append(row)
        return tuple(rows)

    # -- MCP ---------------------------------------------------------------

    @property
    def mcp(self) -> Any | None:
        """The MCP manager this extension world folds into its manifest."""
        return self._mcp

    def mcp_config_path(self) -> Path:
        """The watched MCP definition file (``<workspace>/.nexus/mcp.json``)."""
        return self._workspace / _MCP_CONFIG_RELATIVE

    def _load_mcp_definitions(
        self,
    ) -> tuple[dict[str, Any] | None, tuple[ReloadFailure, ...]]:
        """Read and validate the ``servers`` map from ``mcp.json``.

        Returns ``(None, failures)`` when the file exists but cannot be used, so
        the caller keeps the previous definition set rather than silently
        removing every server. A missing file is an empty set (no MCP servers);
        an explicit ``{"servers": {}}`` is the same. The strict per-server
        parsing (allowed keys, ``${env:VAR}`` interpolation, transport rules)
        lives in :func:`nexus.mcp.client.parse_server_config` and runs inside the
        manager's ``apply``; this only bounds the file and decodes it as JSONC
        (comments/trailing commas) into a strict JSON object, rejecting duplicate
        keys and non-standard constants.
        """
        path = self.mcp_config_path()
        try:
            if not path.is_file():
                return {}, ()
            size = path.stat().st_size
        except OSError as exc:
            return None, (
                ReloadFailure(
                    kind="mcp",
                    name="mcp.json",
                    error=sanitize_text(str(exc)),
                    error_type=type(exc).__name__,
                    path=sanitize_text(str(path), limit=200),
                ),
            )
        if size > _MCP_MAX_BYTES:
            return None, (
                ReloadFailure(
                    kind="mcp",
                    name="mcp.json",
                    error=f"definition file exceeds {_MCP_MAX_BYTES} bytes",
                    error_type="MCPConfigError",
                    path=sanitize_text(str(path), limit=200),
                ),
            )
        try:
            document = _strict_json_object(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return None, (
                ReloadFailure(
                    kind="mcp",
                    name="mcp.json",
                    error=sanitize_text(f"{type(exc).__name__}: {exc}"),
                    error_type=type(exc).__name__,
                    path=sanitize_text(str(path), limit=200),
                ),
            )
        if not isinstance(document, Mapping):
            return None, (
                ReloadFailure(
                    kind="mcp",
                    name="mcp.json",
                    error="mcp.json must be a JSON object",
                    error_type="MCPConfigError",
                    path=sanitize_text(str(path), limit=200),
                ),
            )
        unknown = sorted(set(document) - {"servers"})
        if unknown:
            return None, (
                ReloadFailure(
                    kind="mcp",
                    name="mcp.json",
                    error="unknown top-level keys: " + ", ".join(unknown),
                    error_type="MCPConfigError",
                    path=sanitize_text(str(path), limit=200),
                ),
            )
        servers = document.get("servers", {})
        if not isinstance(servers, Mapping):
            return None, (
                ReloadFailure(
                    kind="mcp",
                    name="mcp.json",
                    error="mcp.json 'servers' must be an object",
                    error_type="MCPConfigError",
                    path=sanitize_text(str(path), limit=200),
                ),
            )
        return dict(servers), ()

    def _failure_from_apply(self, failure: Any) -> ReloadFailure:
        to_dict = getattr(failure, "to_dict", None)
        if callable(to_dict):
            try:
                data = dict(to_dict())
            except Exception:  # noqa: BLE001 - diagnostics are best-effort
                data = {}
        else:
            data = {}
        return ReloadFailure(
            kind="mcp",
            name=sanitize_text(str(data.get("name", "mcp")), limit=200) or "mcp",
            error=sanitize_text(str(data.get("error", ""))),
            error_type=sanitize_text(str(data.get("error_type", "MCPConfigError"))),
            path=sanitize_text(str(self.mcp_config_path()), limit=200),
        )

    async def sync_mcp(self) -> None:
        """Reconcile the live MCP definition set from ``mcp.json``.

        Hot add/remove/reconfigure: the manager's ``apply`` closes removed or
        reconfigured servers, parks added ones, and advances its snapshot only
        when the aggregate tool set changed. Definition and apply failures are
        recorded as diagnostics, never raised: MCP is additive and a bad entry
        must not fail a rebuild or a turn.
        """
        if self._mcp is None:
            return
        definitions, failures = self._load_mcp_definitions()
        collected = list(failures)
        if definitions is not None:
            try:
                report = await self._mcp.apply(definitions)
            except Exception as exc:  # noqa: BLE001 - MCP must never fail a reload
                collected.append(
                    ReloadFailure(
                        kind="mcp",
                        name="mcp.json",
                        error=sanitize_text(f"{type(exc).__name__}: {exc}"),
                        error_type=type(exc).__name__,
                        path=sanitize_text(str(self.mcp_config_path()), limit=200),
                    )
                )
            else:
                for failure in getattr(report, "failures", ()) or ():
                    collected.append(self._failure_from_apply(failure))
        if self._read_resource_tool is None:
            builder = getattr(self._mcp, "read_resource_tool", None)
            if callable(builder):
                try:
                    self._read_resource_tool = builder()
                except Exception:  # noqa: BLE001 - resources are best-effort
                    self._read_resource_tool = None
        self._mcp_failures = tuple(collected)

    async def _connect_mcp(self) -> None:
        """Lazily connect every enabled server, isolating each failure.

        This is the "first use" that makes an MCP server's tools part of the
        manifest: a server that is not connected contributes nothing, and a dead
        or hung one records its health and is skipped. A failure here is never
        fatal to the rebuild.
        """
        if self._mcp is None:
            return
        try:
            definitions = self._mcp.definitions
        except Exception:  # noqa: BLE001 - a foreign manager must not break us
            return
        if not isinstance(definitions, Mapping):
            return
        for name in sorted(definitions):
            if self._closed:
                return
            try:
                await self._mcp.ensure_connected(name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001, S112 - one server never fails the rebuild
                continue

    def _mcp_manifest_parts(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], Any | None, list[ReloadFailure]]:
        """Read the MCP snapshot into ``(tools, mcp_map, read_tool, shadows)``.

        Called from the build thread. ``snapshot()`` is one atomic read of an
        immutable value, so no lock is needed and no half-built state is visible.
        Tool objects are reused across rebuilds (the manager keeps them until its
        own generation changes), so the manifest diff does not churn. The
        ``ReadMcpResource`` tool is a single long-lived object built once on the
        loop and included only while a connected server actually exposes
        resources or resource templates.
        """
        mcp_tools: dict[str, Any] = {}
        mcp_map: dict[str, Any] = {}
        shadows: list[ReloadFailure] = []
        if self._mcp is None:
            return mcp_tools, mcp_map, None, shadows
        try:
            snapshot = self._mcp.snapshot()
        except Exception as exc:  # noqa: BLE001 - a broken manager is not fatal
            shadows.append(
                ReloadFailure(
                    kind="mcp",
                    name="snapshot",
                    error=sanitize_text(f"{type(exc).__name__}: {exc}"),
                    error_type=type(exc).__name__,
                )
            )
            return mcp_tools, mcp_map, None, shadows
        for server in getattr(snapshot, "servers", ()) or ():
            name = getattr(server, "name", None)
            if isinstance(name, str) and name:
                mcp_map[name] = server
        has_resources = False
        for tool in getattr(snapshot, "tools", ()) or ():
            tool_name = getattr(tool, "name", None)
            if isinstance(tool_name, str) and tool_name:
                mcp_tools[tool_name] = tool
        for server in getattr(snapshot, "servers", ()) or ():
            if getattr(server, "resources", ()) or getattr(
                server, "resource_templates", ()
            ):
                has_resources = True
                break
        read_tool = self._read_resource_tool if has_resources else None
        return mcp_tools, mcp_map, read_tool, shadows

    # -- the transaction ---------------------------------------------------

    async def reload(
        self,
        trigger: str = "api",
        sink: Any | None = None,
    ) -> ReloadReport:
        """Run one serialized, coalescing rebuild and return its report.

        Concurrent callers are coalesced: a caller that arrives while a rebuild
        is running is served by the next rebuild to start, never by a second
        rebuild of its own. Events are emitted once, by the rebuild that ran.

        A closed manager rejects the call with :class:`ManagerClosed` both before
        and after acquiring the reload lock, so a reload that was queued behind a
        close can never start a build or install a generation.
        """
        if self._closed:
            raise ManagerClosed("extension manager is closed")
        self._request_seq += 1
        mine = self._request_seq
        async with self._lock:
            if self._closed:
                raise ManagerClosed("extension manager is closed")
            if self._served_seq >= mine and self._last_report is not None:
                return self._last_report
            self._served_seq = self._request_seq
            report = await self._rebuild(trigger, sink)
            self._last_report = report
            return report

    async def _run_build(
        self, previous: Manifest, generation: int
    ) -> _BuildResult:
        """Run one build in a worker thread, cancellation- and leak-safe.

        The build is a blocking import that cannot be interrupted, so a reload
        cancellation must **drain** it rather than abandon it. On cancellation
        the worker is awaited to completion (even across repeated cancellation),
        every module and staged copy it produced is released, the cancelled
        result is never returned to the caller (so it can never swap in), and
        the :class:`asyncio.CancelledError` is re-raised. Any other unexpected
        ``_build`` exception is sanitized into a failure report and its
        artifacts are released. The worker future is retained until it is
        drained, so a subsequent reload can never start a second build while an
        orphaned one is still touching the loader.
        """
        # Draining an orphan build is itself cancellable: if we are cancelled
        # while waiting for it, ``_drain_inflight_build`` re-raises *after* the
        # worker is drained, so no second build ever starts concurrently.
        await self._drain_inflight_build()
        self._build_staged = []
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._guarded_build, previous, generation)
        self._inflight_build = future
        try:
            result = await asyncio.shield(future)
            # Success: the staged copies it produced are now owned by the
            # manifest (via ``_staged_paths``), so drop the transient build list.
            self._build_staged = []
            return result
        except asyncio.CancelledError:
            result = await self._drain_future(future)
            if result is not None:
                self._release_new(result.newly_loaded_all)
            self._release_staged_artifacts()
            raise
        finally:
            if self._inflight_build is future:
                self._inflight_build = None

    async def _drain_inflight_build(self) -> None:
        future = self._inflight_build
        if future is None:
            return
        # A cancellation observed while draining is re-raised only after the
        # worker is fully drained, so the loader is never left mid-import.
        cancelled = await self._drain_future(future, observe_cancel=True)
        if self._inflight_build is future:
            self._inflight_build = None
        if cancelled:
            raise asyncio.CancelledError()

    async def _drain_future(
        self, future: asyncio.Future, *, observe_cancel: bool = False
    ) -> _BuildResult | None | bool:
        """Await a worker to completion, ignoring repeated cancellation.

        ``asyncio.shield`` keeps the inner future alive across a cancellation of
        the awaiting task; the loop re-arms it until the worker is done, so the
        thread is always drained before the cancellation is allowed to
        propagate. With ``observe_cancel`` the return is ``True`` when this task
        was cancelled at least once while draining (the caller then decides
        whether to re-raise).
        """
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
                continue
            except Exception:  # noqa: BLE001 - guarded build should not raise
                break
        if observe_cancel:
            return cancelled
        if future.cancelled():
            return None
        if future.exception() is not None:
            return None
        return future.result()

    def _guarded_build(self, previous: Manifest, generation: int) -> _BuildResult:
        """Run :meth:`_build`, converting an unexpected raise into a failure.

        A build that raises outside its own handled paths must still leave the
        loader and stage clean: the modules installed during the attempt are
        released and every private staged copy is unlinked before a sanitized
        failure is returned. The generation is never advanced.
        """
        before = set(self._loader.owned_modules)
        try:
            return self._build(previous, generation)
        except BaseException as exc:  # noqa: BLE001 - guarantee cleanup
            for name in set(self._loader.owned_modules) - before:
                with contextlib.suppress(Exception):
                    self._loader.release_module(name)
            self._release_staged_artifacts()
            return _BuildResult(
                config=None,
                failures=(
                    ReloadFailure(
                        kind="reload",
                        name="build",
                        error=sanitize_text(f"{type(exc).__name__}: {exc}"),
                        error_type=type(exc).__name__,
                    ),
                ),
            )

    def _release_staged_artifacts(self) -> None:
        staged, self._build_staged = self._build_staged, []
        for item in staged:
            if item.private:
                with contextlib.suppress(OSError):
                    item.path.unlink()

    async def _rebuild(self, trigger: str, sink: Any | None) -> ReloadReport:
        start = time.monotonic()
        previous = self._ref.get()
        # Reconcile and lazily connect MCP before the build reads its snapshot,
        # so a newly added or newly connected server's tools are part of this
        # generation. Both are failure-isolated: a broken or dead server only
        # removes its own tools and is recorded as a diagnostic.
        if not self._closed and self._mcp is not None:
            await self.sync_mcp()
            await self._connect_mcp()
        result = await self._run_build(previous, previous.generation + 1)
        if result.config is not None:
            self._last_config = result.config

        active_sink = sink if sink is not None else self._sink
        for failure in result.failures:
            await self._emit(active_sink, "ext.failed", failure.to_dict())
        # Shadow diagnostics are only re-emitted when the shadow set actually
        # changed, so a no-op rebuild does not repeat ``ext.tool_shadowed``.
        shadows = tuple(result.shadows)
        if shadows != self._shadows:
            for shadow in shadows:
                await self._emit(active_sink, "ext.tool_shadowed", shadow.to_dict())
        self._shadows = shadows

        if result.config is None or result.failures:
            self._release_new(result.newly_loaded_all)
            self._failures = result.failures
            self._ensure_watchers()
            return self._report(previous, previous, None, False, result.failures, start)

        candidate = result.candidate
        if candidate is None or not result.diff.changed:
            # Defensive: a no-diff build should have loaded nothing, but if it
            # did, release it rather than leaving an unowned module/staged copy.
            self._release_new(result.newly_loaded_all)
            self._failures = ()
            self._ensure_watchers()
            return self._report(previous, previous, result.diff, False, (), start)

        if self._closed:
            # The manager was closed while this build ran: never install a
            # generation. Release exactly what this attempt loaded and report a
            # non-swap. This is the "no closed manager CAS" guarantee.
            self._release_new(result.newly_loaded_all)
            self._failures = ()
            return self._report(previous, previous, result.diff, False, (), start)

        try:
            self._ref.compare_and_swap(previous, candidate)
        except StaleGenerationError as exc:
            self._release_new(result.newly_loaded_all)
            failure = ReloadFailure(
                kind="reload",
                name="manifest",
                error=sanitize_text(str(exc)),
                error_type="StaleGenerationError",
            )
            await self._emit(active_sink, "ext.failed", failure.to_dict())
            self._failures = (failure,)
            return self._report(previous, previous, None, False, (failure,), start)

        with self._state_lock:
            self._loaded = result.loaded
            self._skill_loaded = result.skill_loaded
            for loaded in result.newly_loaded_all:
                if loaded.staged is not None:
                    self._staged_paths[loaded.record.handle.name] = loaded.staged.path
        self._failures = ()
        self._ensure_watchers()

        report = self._report(previous, candidate, result.diff, True, (), start)
        await self._emit_changes(active_sink, trigger, previous, candidate, report)
        return report

    def _report(
        self,
        previous: Manifest,
        current: Manifest,
        diff: ManifestDiff | None,
        changed: bool,
        failures: tuple[ReloadFailure, ...],
        start: float,
    ) -> ReloadReport:
        if diff is None:
            diff = ManifestDiff.between(previous, previous)
        return ReloadReport(
            previous_generation=previous.generation,
            generation=current.generation,
            changed=changed,
            diff=diff,
            failed=failures,
            duration_ms=(time.monotonic() - start) * 1000.0,
        )

    # -- the build (runs off-loop) ----------------------------------------

    def _build(self, previous: Manifest, generation: int) -> _BuildResult:
        try:
            config = self._config_loader()
        except Exception as exc:  # noqa: BLE001 - a bad config is a failed reload
            return _BuildResult(config=None, failures=(_config_failure(exc),))
        if not isinstance(config, Config):
            return _BuildResult(
                config=None,
                failures=(
                    ReloadFailure(
                        kind="config",
                        name="config",
                        error="config loader did not return a Config",
                        error_type="ConfigError",
                    ),
                ),
            )
        if _config_equal(config, previous.config):
            config = previous.config

        ext = _ext_section(config)
        system_files, failures = self._load_system_files(config)
        all_failures: list[ReloadFailure] = list(failures)

        if ext.enabled:
            # Seed the workspace tool template (never overwriting) so the model
            # can Read it to learn the extension contract. Best-effort: a
            # read-only workspace must not fail a rebuild.
            with contextlib.suppress(OSError):
                ensure_tool_template(self._workspace / ".nexus" / "tools")
            skills_map, skill_failures = self._discover_skills()
            all_failures.extend(skill_failures)
            quarantine = self._quarantine or build_default_quarantine(
                config,
                root=self._workspace,
                stage_root=self._workspace / ".nexus" / "stage",
            )
            external, loaded, newly, tool_failures, shadows = self._discover_tools(
                ext,
                quarantine,
                isolate=bool(ext.quarantine),
                previous_loaded=self._loaded,
                generation=generation,
            )
            all_failures.extend(tool_failures)
            skill_tools, skill_loaded, skill_newly, skill_tool_failures = (
                self._discover_skill_tools(
                    skills_map,
                    external,
                    quarantine,
                    isolate=bool(ext.quarantine),
                    generation=generation,
                )
            )
            all_failures.extend(skill_tool_failures)
        else:
            skills_map = {}
            external = {}
            loaded = {}
            newly = ()
            shadows = ()
            skill_tools = {}
            skill_loaded = {}
            skill_newly = ()

        tools_map = dict(self._builtin_by_name)
        tools_map.update(external)
        mcp_tools, mcp_map, read_tool, mcp_shadows = self._mcp_manifest_parts()
        shadow_rows = list(shadows)
        for name, tool in mcp_tools.items():
            if name in tools_map and tools_map[name] is not tool:
                # A builtin or external tool already claims this exact name.
                # Keep the local tool (workspace precedence) and record it, so
                # the collision is visible rather than silent.
                shadow_rows.append(
                    ReloadFailure(
                        kind="shadowed",
                        name=name,
                        error=sanitize_text(
                            f"MCP tool {name!r} is shadowed by a local tool"
                        ),
                        error_type="shadow",
                    )
                )
                continue
            tools_map[name] = tool
        if read_tool is not None and read_tool.name not in tools_map:
            tools_map[read_tool.name] = read_tool
        shadow_rows.extend(mcp_shadows)
        modules_map = {
            item.record.handle.name: item.record.handle for item in loaded.values()
        }
        for item in skill_loaded.values():
            modules_map[item.record.handle.name] = item.record.handle
        candidate = Manifest(
            generation=generation,
            config=config,
            tools=tools_map,
            skills=skills_map,
            skill_tools=skill_tools,
            system_files=system_files,
            modules=modules_map,
            mcp=mcp_map,
        )
        diff = ManifestDiff.between(previous, candidate)
        return _BuildResult(
            config=config,
            candidate=candidate,
            diff=diff,
            changed=diff.changed,
            failures=tuple(all_failures),
            shadows=tuple(shadow_rows),
            loaded=loaded,
            newly_loaded=tuple(newly),
            skill_loaded=skill_loaded,
            skill_newly_loaded=tuple(skill_newly),
            skill_tools=skill_tools,
        )

    # -- discovery ---------------------------------------------------------

    def _load_system_files(
        self, config: Config
    ) -> tuple[SystemFiles, list[ReloadFailure]]:
        files: dict[str, SystemFile] = {}
        failures: list[ReloadFailure] = []
        for logical, filename in (
            ("soul", config.instructions_file),
            ("memory", config.memory_file),
        ):
            if not isinstance(filename, str) or not filename:
                continue
            try:
                path = resolve_within(self._workspace, filename)
            except ConfigError as exc:
                failures.append(
                    ReloadFailure(
                        kind="system_file",
                        name=logical,
                        error=sanitize_text(str(exc)),
                        error_type="ConfigError",
                    )
                )
                continue
            if not path.is_file():
                continue
            try:
                content = config.read(self._workspace, filename)
            except Exception as exc:  # noqa: BLE001 - unreadable is a failed reload
                failures.append(
                    ReloadFailure(
                        kind="system_file",
                        name=logical,
                        error=sanitize_text(str(exc)),
                        error_type=type(exc).__name__,
                    )
                )
                continue
            files[logical] = SystemFile(
                name=logical, content=content, path=str(path)
            )
        return SystemFiles(files=files), failures

    def _discover_skills(
        self,
    ) -> tuple[dict[str, Any], list[ReloadFailure]]:
        try:
            self._skills.refresh()
        except Exception as exc:  # noqa: BLE001 - discovery failure is a failure
            return {}, [
                ReloadFailure(
                    kind="skill",
                    name="skills",
                    error=sanitize_text(str(exc)),
                    error_type=type(exc).__name__,
                )
            ]
        return {skill.name: skill for skill in self._skills.skills}, []

    def _discover_tools(
        self,
        ext: ExtSection,
        quarantine: Quarantine,
        *,
        isolate: bool,
        previous_loaded: dict[str, LoadedExtension],
        generation: int,
    ) -> tuple[
        dict[str, RegisteredTool],
        dict[str, LoadedExtension],
        tuple[LoadedExtension, ...],
        list[ReloadFailure],
        list[ReloadFailure],
    ]:
        tools: dict[str, RegisteredTool] = {}
        loaded: dict[str, LoadedExtension] = {}
        newly: list[LoadedExtension] = []
        failures: list[ReloadFailure] = []
        shadows: list[ReloadFailure] = []
        claim: dict[str, str] = {}
        claim_tier: dict[str, int] = {}

        for path, tier in self._candidate_files(ext):
            try:
                probe = quarantine.open(path)
            except Exception as exc:  # noqa: BLE001 - any refusal is a failure
                failures.append(_refusal_failure(path, exc))
                continue

            identity = probe.source_identity
            previous = previous_loaded.get(identity)
            if previous is not None and previous.record.handle.sha256 == probe.sha256:
                extension = previous
            else:
                outcome = self._load_candidate(
                    quarantine, probe, generation, path, isolate=isolate
                )
                if isinstance(outcome, ReloadFailure):
                    failures.append(outcome)
                    continue
                extension = outcome
                newly.append(extension)

            verdict = self._classify(
                extension.record.tool_names, tier, claim, claim_tier
            )
            if verdict is not None:
                reason, message = verdict
                if reason == "failure":
                    failures.append(
                        ReloadFailure(
                            kind="ext",
                            name=path.stem,
                            error=sanitize_text(message),
                            error_type="collision",
                            path=sanitize_text(str(path), limit=200),
                        )
                    )
                else:
                    # A higher-precedence tool hid a lower-precedence one. That
                    # is not a failure, but it must never be silent: record a
                    # diagnostic and let the orchestrator emit an event.
                    shadows.append(
                        ReloadFailure(
                            kind="shadowed",
                            name=path.stem,
                            error=sanitize_text(message),
                            error_type="shadow",
                            path=sanitize_text(str(path), limit=200),
                        )
                    )
                if any(item is extension for item in newly):
                    self._release_temp(extension)
                    newly = [item for item in newly if item is not extension]
                continue

            loaded[identity] = extension
            for name in extension.record.tool_names:
                claim[name.casefold()] = identity
                claim_tier[name.casefold()] = tier
            for tool in extension.tools:
                tools[tool.name] = tool

        return tools, loaded, tuple(newly), failures, shadows

    def _discover_skill_tools(
        self,
        skills_map: Mapping[str, Any],
        external: Mapping[str, RegisteredTool],
        quarantine: Quarantine,
        *,
        isolate: bool,
        generation: int,
    ) -> tuple[
        dict[str, SkillToolSet],
        dict[str, LoadedExtension],
        list[LoadedExtension],
        list[ReloadFailure],
    ]:
        """Quarantine and load each skill's bundled ``tools/*.py`` (plan 5.4).

        The modules are loaded under the same generation-stamped loader but are
        **not** added to ``Manifest.tools``: the association lives in
        :class:`~nexus.ext.manifest.SkillToolSet`, keyed by skill name, so the
        runtime exposes them only while the skill is active. Any failure
        (quarantine, import, hang, or a name collision with a builtin or another
        live tool) aborts the whole rebuild per the documented all-or-nothing
        policy, and the modules loaded by this attempt are released.
        """
        tools_by_skill: dict[str, SkillToolSet] = {}
        loaded: dict[str, LoadedExtension] = {}
        newly: list[LoadedExtension] = []
        failures: list[ReloadFailure] = []

        claimed = {name.casefold() for name in self._builtin_by_name}
        claimed.update(name.casefold() for name in external)

        for skill_name in sorted(skills_map):
            skill = skills_map[skill_name]
            candidates = tuple(getattr(skill, "tool_candidates", ()) or ())
            if not candidates:
                continue
            directory = getattr(skill, "directory", None)
            if directory is None:
                continue

            skill_extensions: list[LoadedExtension] = []
            local_claimed: set[str] = set()
            failure: ReloadFailure | None = None
            for candidate in candidates:
                relative = getattr(candidate, "path", None)
                if not isinstance(relative, str) or not relative:
                    continue
                path = Path(directory) / relative
                try:
                    probe = quarantine.open(path, origin="skill")
                except Exception as exc:  # noqa: BLE001 - any refusal is a failure
                    failure = self._skill_refusal_failure(skill_name, path, exc)
                    break

                identity = probe.source_identity
                previous = self._skill_loaded.get(identity)
                if (
                    previous is not None
                    and previous.record.handle.sha256 == probe.sha256
                ):
                    extension = previous
                else:
                    outcome = self._load_candidate(
                        quarantine,
                        probe,
                        generation,
                        path,
                        isolate=isolate,
                        origin="skill",
                    )
                    if isinstance(outcome, ReloadFailure):
                        failure = self._as_skill_failure(skill_name, outcome)
                        break
                    extension = outcome
                    newly.append(extension)

                collision = self._skill_tool_collision(
                    skill_name, path, extension.record.tool_names, claimed, local_claimed
                )
                if collision is not None:
                    failure = collision
                    break

                for name in extension.record.tool_names:
                    local_claimed.add(name.casefold())
                skill_extensions.append(extension)

            if failure is not None:
                failures.append(failure)
                continue

            claimed.update(local_claimed)
            for extension in skill_extensions:
                loaded[extension.source_id] = extension
            tools_by_skill[skill_name] = SkillToolSet(
                skill=skill_name,
                skill_fingerprint=_safe_skill_fingerprint(skill),
                generation=generation,
                tools=tuple(
                    tool for extension in skill_extensions for tool in extension.tools
                ),
                modules=tuple(
                    extension.record.handle.name for extension in skill_extensions
                ),
            )

        return tools_by_skill, loaded, newly, failures

    @staticmethod
    def _skill_refusal_failure(
        skill_name: str, path: Path, exc: BaseException
    ) -> ReloadFailure:
        base = _refusal_failure(path, exc)
        return ReloadFailure(
            kind="skill",
            name=skill_name,
            error=base.error,
            error_type=base.error_type,
            path=base.path,
        )

    @staticmethod
    def _as_skill_failure(skill_name: str, failure: ReloadFailure) -> ReloadFailure:
        return ReloadFailure(
            kind="skill",
            name=skill_name,
            error=failure.error,
            error_type=failure.error_type,
            path=failure.path,
        )

    def _skill_tool_collision(
        self,
        skill_name: str,
        path: Path,
        names: Sequence[str],
        claimed: set[str],
        local_claimed: set[str],
    ) -> ReloadFailure | None:
        seen: set[str] = set()
        for name in names:
            folded = name.casefold()
            if folded in seen:
                return ReloadFailure(
                    kind="skill",
                    name=skill_name,
                    error=sanitize_text(
                        f"duplicate tool name {name!r} in one bundled module"
                    ),
                    error_type="collision",
                    path=sanitize_text(str(path), limit=200),
                )
            seen.add(folded)
            if folded in self._builtin_folded:
                return ReloadFailure(
                    kind="skill",
                    name=skill_name,
                    error=sanitize_text(f"tool name {name!r} collides with a builtin"),
                    error_type="collision",
                    path=sanitize_text(str(path), limit=200),
                )
            if folded in claimed or folded in local_claimed:
                return ReloadFailure(
                    kind="skill",
                    name=skill_name,
                    error=sanitize_text(
                        f"tool name {name!r} collides with another live tool"
                    ),
                    error_type="collision",
                    path=sanitize_text(str(path), limit=200),
                )
        return None

    def _candidate_files(self, ext: ExtSection) -> list[tuple[Path, int]]:
        found: dict[str, tuple[Path, int]] = {}
        for raw in ext.dirs:
            root = _expand_dir(raw, self._workspace, self._home)
            tier = _tier_for(root, self._workspace, self._home)
            if not root.is_dir():
                continue
            try:
                entries = sorted(
                    root.glob("*.py"), key=lambda p: (p.name.casefold(), p.name)
                )
            except OSError:  # pragma: no cover - a listing race is skipped
                continue
            for path in entries:
                if path.name.startswith("_"):
                    continue
                if not path.is_file():
                    continue
                absolute = _absolute(path)
                key = str(absolute)
                if key not in found or tier > found[key][1]:
                    found[key] = (absolute, tier)
        return sorted(found.values(), key=lambda item: (-item[1], str(item[0])))

    def _load_candidate(
        self,
        quarantine: Quarantine,
        probe: StagedSource,
        generation: int,
        path: Path,
        *,
        isolate: bool,
        origin: str = "ext",
    ) -> LoadedExtension | ReloadFailure:
        try:
            inspection = quarantine.inspect(probe)
        except Exception as exc:  # noqa: BLE001
            return _refusal_failure(path, exc)
        if not inspection.ok:
            return _outcome_failure(path, inspection)

        if isolate:
            try:
                isolated = quarantine.run_isolated(probe)
            except Exception as exc:  # noqa: BLE001
                return _refusal_failure(path, exc)
            if not isolated.ok:
                return _outcome_failure(path, isolated)

        try:
            private = quarantine.stage(probe)
        except Exception as exc:  # noqa: BLE001
            return _refusal_failure(path, exc)
        # Track the staged copy as soon as it exists but before it is imported:
        # a cancellation or an unexpected raise during the import must still be
        # able to unlink it, since the loader records nothing about artifacts.
        self._build_staged.append(private)

        outcome = self._loader.load(private, generation, origin=origin)
        if not outcome.ok or outcome.record is None:
            return _outcome_failure(path, outcome.outcome)
        return LoadedExtension(
            source_id=probe.source_identity,
            record=outcome.record,
            tools=outcome.tools,
            staged=private,
        )

    def _classify(
        self,
        names: Sequence[str],
        tier: int,
        claim: dict[str, str],
        claim_tier: dict[str, int],
    ) -> tuple[str, str] | None:
        for name in names:
            folded = name.casefold()
            if folded in self._builtin_folded:
                return ("failure", f"tool name {name!r} collides with a builtin")
            owner = claim.get(folded)
            if owner is None:
                continue
            if claim_tier.get(folded, -1) == tier:
                return (
                    "failure",
                    f"tool name {name!r} collides with {owner!r} in the same tier",
                )
            return (
                "shadow",
                f"tool name {name!r} is shadowed by higher-precedence {owner!r}",
            )
        return None

    # -- cleanup -----------------------------------------------------------

    def _release_temp(self, extension: LoadedExtension) -> None:
        self._loader.release_module(extension.record.handle.name)
        if extension.staged is not None and extension.staged.private:
            with contextlib.suppress(OSError):
                extension.staged.path.unlink()

    def _release_new(self, newly_loaded: Iterable[LoadedExtension] = ()) -> None:
        for extension in newly_loaded:
            self._release_temp(extension)

    def _retire_manifest(self, manifest: Manifest) -> None:
        """Release modules a retired generation owned and later ones do not.

        Runs from :meth:`ManifestRef` only after every pin on ``manifest`` is
        gone, so a module still backing an in-flight call is never dropped. A
        raise here is caught by the ref and retained as a cleanup failure; the
        swap that retired the generation is already committed.
        """
        live = set(self._ref.get().modules)
        for name in manifest.modules:
            if name in live:
                continue
            self._loader.release_module(name)
            with self._state_lock:
                staged_path = self._staged_paths.pop(name, None)
            if staged_path is not None:
                with contextlib.suppress(OSError):
                    staged_path.unlink()

    # -- events ------------------------------------------------------------

    async def _emit(self, sink: Any | None, event_type: str, data: dict[str, Any]) -> None:
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
        except Exception:  # noqa: BLE001 - a broken sink never breaks a reload
            return

    async def _emit_changes(
        self,
        sink: Any | None,
        trigger: str,
        previous: Manifest,
        candidate: Manifest,
        report: ReloadReport,
    ) -> None:
        for name in (*report.diff.modules.added, *report.diff.modules.changed):
            handle = candidate.modules.get(name)
            if handle is None:
                continue
            await self._emit(
                sink,
                "ext.loaded",
                {
                    "name": handle.name,
                    "source": sanitize_text(handle.path, limit=200),
                    "sha256": handle.sha256,
                    "generation": handle.generation,
                    "origin": handle.origin,
                },
            )
        for name in report.diff.modules.removed:
            handle = previous.modules.get(name)
            await self._emit(
                sink,
                "ext.unloaded",
                {
                    "name": name,
                    "source": (
                        sanitize_text(handle.path, limit=200)
                        if handle is not None
                        else None
                    ),
                },
            )
        await self._emit(
            sink,
            "ext.manifest_changed",
            {
                "previous_generation": report.previous_generation,
                "generation": report.generation,
                "trigger": trigger,
                "summary": report.summary,
                "diff": report.diff.to_dict(),
            },
        )

    # -- watcher -----------------------------------------------------------

    def start(self, *, sink: Any | None = None) -> bool:
        """Lazily create the polling watcher task. Idempotent; False if disabled.

        A closed manager is terminal: ``start`` always returns ``False`` rather
        than resurrect a watcher over released resources.
        """
        if self._closed:
            return False
        if self._watch_task is not None and not self._watch_task.done():
            return False
        if self._interval_seconds() <= 0.0:
            return False
        self._ensure_watchers()
        self._watch_stop = asyncio.Event()
        self._watch_sink = sink if sink is not None else self._sink
        self._watch_task = asyncio.ensure_future(self._watch_loop(self._watch_sink))
        return True

    def stop(self) -> None:
        """Signal the watcher task to stop. Safe to call repeatedly."""
        if self._watch_stop is not None:
            self._watch_stop.set()

    async def aclose(self) -> None:
        """Stop the watcher and retire the live world. Idempotent and terminal.

        Close is coordinated under the reload lock: ``_closed`` is set first, so
        an in-flight rebuild observes it before its compare-and-swap and is
        refused (releasing exactly what it loaded), and every reload queued
        behind the close rejects with :class:`ManagerClosed`. The in-flight build
        is then drained so nothing is released out from under a running import.

        The current generation is retired through the ref, not released
        directly: a single empty **terminal generation** is swapped in, and the
        normal ``on_retire`` callback releases a module and its staged copy only
        once the last lease on that generation is gone. A generation still pinned
        by an in-flight call therefore survives ``aclose`` and is cleaned when
        its lease is released; ``aclose`` returns without waiting for pins.
        """
        if self._closed:
            return
        self._closed = True
        self.stop()
        async with self._lock:
            await self._drain_inflight_build()
            self._build_staged = []
            task = self._watch_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._watch_task = None
            self._watchers = ()
            self._watch_sig = None
            self._retire_current()
            with self._state_lock:
                self._loaded = {}
                self._skill_loaded = {}

    def _retire_current(self) -> None:
        """Retire the live generation by swapping in an empty terminal one.

        The retired generation's modules and staged copies are released by
        ``on_retire`` *after* its last lease drains -- never directly here -- so
        a call pinned to it keeps working. Any generation already retired while
        pinned is untouched and cleaned by the same callback when its lease is
        released.
        """
        current = self._ref.get()
        with self._state_lock:
            has_staged = bool(self._staged_paths)
        if not current.modules and not has_staged:
            return
        terminal = Manifest(
            generation=current.generation + 1,
            config=self._last_config,
        )
        try:
            self._ref.swap(terminal)
        except (StaleGenerationError, EqualGenerationError):  # pragma: no cover
            # The lock makes this unreachable from our own reloads; a failed
            # swap leaves the ref untouched and the ref's own on_retire still
            # owns any cleanup.
            return

    async def _watch_loop(self, sink: Any | None) -> None:
        try:
            while True:
                interval = self._interval_seconds()
                if interval <= 0.0:
                    break
                stop = self._watch_stop
                if stop is None:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    break
                except TimeoutError:
                    pass
                if stop.is_set():
                    break
                # A watcher poll or reload that raises unexpectedly must not kill
                # the loop: report a sanitized failure and keep watching. A
                # cancellation still propagates.
                try:
                    if self._poll_watchers():
                        await self.reload(trigger="watcher", sink=sink)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the watcher must survive
                    await self._emit(
                        sink,
                        "ext.failed",
                        {
                            "kind": "watcher",
                            "name": "watcher",
                            "error": sanitize_text(f"{type(exc).__name__}: {exc}"),
                            "error_type": type(exc).__name__,
                        },
                    )
        finally:
            self._watch_task = None

    def _interval_seconds(self) -> float:
        config = self._last_config
        ext = _ext_section(config)
        if not ext.enabled:
            return 0.0
        try:
            millis = int(ext.watch_interval_ms)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, millis / 1000.0)

    def _expanded_dirs(self, ext: ExtSection) -> tuple[Path, ...]:
        return tuple(_expand_dir(raw, self._workspace, self._home) for raw in ext.dirs)

    def _watcher_signature(self, config: Config) -> tuple[Any, ...]:
        ext = _ext_section(config)
        return (
            tuple(str(path) for path in self._expanded_dirs(ext)),
            tuple(str(root) for _tier, root in self._skills.roots),
            ext.watch_interval_ms,
            bool(ext.enabled),
        )

    def _ensure_watchers(self) -> None:
        config = self._last_config
        if config is None:
            return
        signature = self._watcher_signature(config)
        if signature == self._watch_sig and self._watchers:
            return
        ext = _ext_section(config)
        watchers: list[DirectoryWatcher] = []
        hot = list(self._expanded_dirs(ext))
        if hot:
            watchers.append(DirectoryWatcher(hot, pattern="*.py", recursive=False))
        watchers.append(
            DirectoryWatcher(
                (self._workspace, self._home / ".nexus"), pattern="*", recursive=False
            )
        )
        if self._mcp is not None:
            mcp_path = self.mcp_config_path()
            watchers.append(
                DirectoryWatcher(
                    (mcp_path.parent,), pattern=mcp_path.name, recursive=False
                )
            )
        skill_roots = [root for _tier, root in self._skills.roots]
        if skill_roots:
            watchers.append(
                DirectoryWatcher(skill_roots, pattern="*", recursive=True)
            )
        for watcher in watchers:
            watcher.prime()
        self._watchers = tuple(watchers)
        self._watch_sig = signature

    def _poll_watchers(self) -> list[Any]:
        changes: list[Any] = []
        for watcher in self._watchers:
            changes.extend(watcher.poll())
        return changes
