"""AgentManager: deterministic subagent discovery, seeding, and tool selection.

Plan sections 5.6, 15.6-15.8. Discovery is a pure scan of ordered roots::

    nexus/agents/data/  <  ~/.nexus/agents/  <  <workspace>/.nexus/agents/

Each root contains one ``<name>.md`` file per definition. Roots are sorted by
``(precedence, path)`` and scanned low-to-high, so a workspace definition shadows
a user definition which shadows a built-in one. Within one root, files are sorted
by ``(casefold name, name)`` and the **first** winner is deterministic.

What the manager guarantees:

* **Generation-stable snapshots.** Each refresh reads a ``*.md`` exactly once and
  snapshots the system-prompt body from those same opened bytes, so a spawn under
  a pinned generation cannot observe a later edit. The context still sees
  progressive disclosure: :meth:`render_index` emits sanitized ``name:
  description`` lines only, and the body is absent from the idle index.
* **Object reuse.** An unchanged definition is reused as the same immutable
  object (compared by whole-definition fingerprint *and* provenance), so a
  manifest diff is a true no-op.
* **Retained diagnostics.** Every skipped file, every shadowed definition, every
  case-fold collision, and every unknown or forbidden declaration is recorded.
  Nothing is silently dropped.
* **Case-fold safety.** Definitions are keyed by ``name.casefold()``, so lookups
  are case-insensitive and no two winners can share a key.
* **Deletion fallback.** :meth:`refresh` rebuilds from disk, so deleting a
  workspace definition immediately resurfaces the lower-tier one it shadowed.
* **Declarations are not grants.** ``bundles``/``tools`` are validated against
  injected known names but never expand a profile, register a tool, or change the
  manager's own surface. :meth:`select_tools` intersects a declaration with the
  parent's authority and can only narrow it. ``explore`` and ``planner`` are
  structurally denied shell and mutating-filesystem tools regardless of what
  their file declares.
* **Seeding is once per workspace.** :func:`seed_workspace_roles` writes the
  built-in ``general``/``explore``/``planner`` definitions into the workspace on
  first run and drops a marker; the marker stops the seeder from resurrecting a
  file the user deleted.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    FORBIDDEN_ROLE_BUNDLES,
    FORBIDDEN_ROLE_TOOLS,
    MAX_AGENT_FILE_BYTES,
    MAX_FRONTMATTER_BYTES,
    READ_ONLY_ROLES,
    AgentDef,
    AgentDiagnostic,
    AgentDiagnosticCode,
    AgentError,
    AgentIndex,
    AgentIndexEntry,
    AgentNotFoundError,
    AgentOversizeError,
    AgentParseError,
    AgentProvenance,
    AgentSeedError,
    AgentSource,
    ParsedFrontmatter,
    read_agent_bytes,
    read_agent_file,
    split_frontmatter,
    validate_agent_name,
)

__all__ = [
    "AGENT_FILE_SUFFIX",
    "DATA_DIR_NAME",
    "SEEDED_ROLES",
    "SEED_MARKER_NAME",
    "SEED_VERSION",
    "AgentManager",
    "AgentToolSelection",
    "RootSpec",
    "SeedReport",
    "seed_workspace_roles",
]

#: The file suffix that makes a file a definition.
AGENT_FILE_SUFFIX = ".md"
#: The packaged built-in definitions live next to this module under ``data/``.
DATA_DIR_NAME = "data"
#: The marker written into the workspace once the built-in roles are seeded.
SEED_MARKER_NAME = ".seeded"
#: Bumped only when the seeding contract itself changes.
SEED_VERSION = 1
#: The three roles seeded into every fresh workspace (plan section 15.7).
SEEDED_ROLES = ("explore", "general", "planner")

#: A ``(tier, path)`` discovery root.
RootSpec = tuple[AgentSource, Path]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedReport:
    """The outcome of one seeding attempt (deterministic and JSON-friendly).

    ``already_seeded`` is true when the workspace marker already existed, which
    is the normal steady state and the mechanism that keeps a deleted file from
    being recreated. ``source_missing`` is true when the built-in source is not
    installed; no marker is written in that case so a later run can still seed.
    """

    already_seeded: bool = False
    source: Path | None = None
    marker: Path | None = None
    source_missing: bool = False
    written: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def seeded(self) -> bool:
        return bool(self.written)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _default_seed_source() -> Path:
    return Path(__file__).with_name(DATA_DIR_NAME)


def _seed_candidates(source: Path) -> list[Path]:
    if not source.is_dir():
        return []
    candidates = [
        entry
        for entry in source.iterdir()
        if entry.is_file()
        and not entry.name.startswith(".")
        and entry.suffix.lower() == AGENT_FILE_SUFFIX
    ]
    candidates.sort(key=lambda p: (p.name.casefold(), p.name))
    return candidates


def seed_workspace_roles(
    workspace: str | Path,
    *,
    source: str | Path | None = None,
    marker_name: str = SEED_MARKER_NAME,
    max_file_bytes: int = MAX_AGENT_FILE_BYTES,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
    overwrite: bool = False,
) -> SeedReport:
    """Seed the built-in roles into ``<workspace>/.nexus/agents`` exactly once.

    The marker is checked first: if it exists (and ``overwrite`` is false) this is
    a no-op, so deleting a seeded file does **not** bring it back. Existing files
    are never clobbered even before the marker exists, so a user definition that
    predates the first run is preserved. Each seed file is validated against the
    restricted grammar *and* the declared name must match its filename before it
    is written; a symlinked target is skipped rather than followed.
    """
    source_dir = Path(source) if source is not None else _default_seed_source()
    agents_dir = Path(workspace) / ".nexus" / "agents"
    marker = agents_dir / marker_name

    if marker.exists() and not overwrite:
        return SeedReport(
            already_seeded=True, source=source_dir, marker=marker
        )
    if not source_dir.is_dir():
        return SeedReport(
            already_seeded=False,
            source=source_dir,
            source_missing=True,
        )

    written: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for entry in _seed_candidates(source_dir):
        name = entry.stem
        try:
            validate_agent_name(name)
            data = read_agent_bytes(entry, max_file_bytes=max_file_bytes)
            parsed, _body = split_frontmatter(
                data, max_frontmatter_bytes=max_frontmatter_bytes
            )
            if parsed.name != name:
                raise AgentParseError(
                    f"seed file {entry.name!r} declares {parsed.name!r}"
                )
        except AgentError:
            failed.append(name)
            continue

        target = agents_dir / f"{name}{AGENT_FILE_SUFFIX}"
        if (target.exists() or target.is_symlink()) and not overwrite:
            skipped.append(name)
            continue
        try:
            _atomic_write(target, data)
        except OSError:
            failed.append(name)
            continue
        written.append(name)

    payload = {
        "version": SEED_VERSION,
        "seeded": written,
        "source": source_dir.name,
    }
    try:
        _atomic_write(
            marker,
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
        )
    except OSError as exc:
        raise AgentSeedError(
            f"could not write seed marker {marker}: {exc}"
        ) from exc
    return SeedReport(
        already_seeded=False,
        source=source_dir,
        marker=marker,
        written=tuple(written),
        skipped=tuple(skipped),
        failed=tuple(failed),
    )


# ---------------------------------------------------------------------------
# Tool selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentToolSelection:
    """The result of narrowing a definition's request to the parent's authority.

    ``selected`` is always a subset of ``ceiling``, which is itself a subset of
    ``available``. ``requested`` is what the definition (or inheritance) asked
    for; ``dropped`` is what was asked for but not granted; ``stripped`` is the
    subset of ``dropped`` that the read-only guarantee removed. Nothing here
    registers a tool or mutates a registry -- it is a view, never a grant.
    """

    agent: str
    available: frozenset[str]
    profile: frozenset[str]
    ceiling: frozenset[str]
    requested: frozenset[str]
    selected: frozenset[str]
    dropped: frozenset[str]
    stripped: frozenset[str]
    read_only: bool

    @property
    def narrowed(self) -> bool:
        return self.selected != self.available

    def permits(self, name: object) -> bool:
        return name in self.selected

    def __contains__(self, name: object) -> bool:
        return name in self.selected

    def __len__(self) -> int:
        return len(self.selected)

    def __iter__(self):
        return iter(sorted(self.selected))


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


def _coerce_source(value: object) -> AgentSource:
    if isinstance(value, AgentSource):
        return value
    try:
        return AgentSource(str(value))
    except ValueError as exc:
        raise AgentError(f"unknown agent source tier {value!r}") from exc


def _normalize_roots(roots: Iterable[object] | None) -> tuple[RootSpec, ...]:
    out: list[RootSpec] = []
    seen: set[tuple[str, str]] = set()
    for item in roots or ():
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise AgentError("roots must be (source, path) pairs")
        tier = _coerce_source(item[0])
        path = Path(item[1])
        key = (tier.value, str(path))
        if key in seen:
            continue
        seen.add(key)
        out.append((tier, path))
    out.sort(key=lambda spec: (spec[0].precedence, str(spec[1])))
    return tuple(out)


def _normalize_bundles(
    known_bundles: Mapping[str, Iterable[str]] | Iterable[str] | None,
) -> dict[str, tuple[str, ...]] | None:
    if known_bundles is None:
        return None
    if isinstance(known_bundles, Mapping):
        return {
            str(name): tuple(str(tool) for tool in tools)
            for name, tools in known_bundles.items()
        }
    return {str(name): () for name in known_bundles}


class AgentManager:
    """Discovers subagent definitions from ordered roots and serves metadata."""

    def __init__(
        self,
        *,
        roots: Iterable[object] | None = None,
        known_tools: Iterable[str] | None = None,
        known_bundles: Mapping[str, Iterable[str]] | Iterable[str] | None = None,
        max_file_bytes: int = MAX_AGENT_FILE_BYTES,
        max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
        max_body_bytes: int = MAX_AGENT_FILE_BYTES,
        max_description_chars: int | None = None,
    ) -> None:
        self._roots = _normalize_roots(roots)
        self._known_tools = (
            frozenset(str(tool) for tool in known_tools)
            if known_tools is not None
            else None
        )
        self._known_bundles = _normalize_bundles(known_bundles)
        self._max_file_bytes = max_file_bytes
        self._max_frontmatter_bytes = max_frontmatter_bytes
        self._max_body_bytes = max_body_bytes
        self._max_description_chars = max_description_chars
        self._generation = 0
        self._agents: tuple[AgentDef, ...] = ()
        self._by_key: dict[str, AgentDef] = {}
        self._diagnostics: tuple[AgentDiagnostic, ...] = ()
        #: Diagnostics from seeding (kept separate from discovery, which
        #: ``refresh`` replaces). Surfaced through :attr:`diagnostics`.
        self._seed_diagnostics: tuple[AgentDiagnostic, ...] = ()
        self.refresh()

    # -- construction ------------------------------------------------------

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        *,
        home: str | Path | None = None,
        builtin: str | Path | Iterable[str | Path] | None = None,
        seed: bool = True,
        seed_source: str | Path | None = None,
        marker_name: str = SEED_MARKER_NAME,
        **kwargs: Any,
    ) -> AgentManager:
        """Convenience roots: ``<builtin>`` < ``~/.nexus/agents`` < ``.nexus/agents``.

        When ``seed`` is true the built-in roles are written into the workspace
        (once) before discovery, so ``general``/``explore``/``planner`` appear as
        editable workspace definitions.
        """
        roots: list[RootSpec] = []
        if builtin is None:
            builtin = _default_seed_source()
        if isinstance(builtin, (str, Path)):
            builtin_paths: Iterable[str | Path] = (builtin,)
        else:
            builtin_paths = builtin
        for path in builtin_paths:
            roots.append((AgentSource.BUILTIN, Path(path)))
        if home is not None:
            roots.append((AgentSource.USER, Path(home) / ".nexus" / "agents"))
        roots.append(
            (AgentSource.WORKSPACE, Path(workspace) / ".nexus" / "agents")
        )
        report: SeedReport | None = None
        if seed:
            report = seed_workspace_roles(
                workspace,
                source=(
                    seed_source
                    if seed_source is not None
                    else _default_seed_source()
                ),
                marker_name=marker_name,
                max_file_bytes=kwargs.get("max_file_bytes", MAX_AGENT_FILE_BYTES),
                max_frontmatter_bytes=kwargs.get(
                    "max_frontmatter_bytes", MAX_FRONTMATTER_BYTES
                ),
            )
        manager = cls(roots=roots, **kwargs)
        if report is not None:
            manager._seed_diagnostics = manager._seed_diagnostic_rows(report)
        return manager

    @staticmethod
    def _seed_diagnostic_rows(report: SeedReport) -> tuple[AgentDiagnostic, ...]:
        """Surface seeding failures/missing sources instead of swallowing them."""
        rows: list[AgentDiagnostic] = []
        if report.source_missing:
            rows.append(
                AgentDiagnostic(
                    code=AgentDiagnosticCode.SEED_ERROR,
                    message=(
                        "built-in agent seed source is missing; workspace roles "
                        f"were not seeded ({report.source})"
                    ),
                    path=str(report.source) if report.source is not None else "",
                )
            )
        for name in report.failed:
            rows.append(
                AgentDiagnostic(
                    code=AgentDiagnosticCode.SEED_ERROR,
                    message=f"failed to seed built-in agent role {name!r}",
                    name=name,
                )
            )
        return tuple(rows)

    # -- introspection -----------------------------------------------------

    @property
    def roots(self) -> tuple[RootSpec, ...]:
        return self._roots

    @property
    def known_tools(self) -> frozenset[str] | None:
        return self._known_tools

    @property
    def known_bundles(self) -> Mapping[str, tuple[str, ...]] | None:
        return self._known_bundles

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def agents(self) -> tuple[AgentDef, ...]:
        return self._agents

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(agent.name for agent in self._agents)

    @property
    def diagnostics(self) -> tuple[AgentDiagnostic, ...]:
        return (*self._diagnostics, *self._seed_diagnostics)

    @property
    def shadowed(self) -> tuple[AgentDiagnostic, ...]:
        codes = {
            AgentDiagnosticCode.SHADOWED,
            AgentDiagnosticCode.CASE_COLLISION,
        }
        return tuple(d for d in self._diagnostics if d.code in codes)

    @property
    def failures(self) -> tuple[AgentDiagnostic, ...]:
        codes = {
            AgentDiagnosticCode.PARSE_ERROR,
            AgentDiagnosticCode.READ_ERROR,
            AgentDiagnosticCode.OVERSIZE,
        }
        return tuple(d for d in self._diagnostics if d.code in codes)

    @property
    def security(self) -> tuple[AgentDiagnostic, ...]:
        codes = {
            AgentDiagnosticCode.FORBIDDEN_TOOL,
            AgentDiagnosticCode.FORBIDDEN_BUNDLE,
        }
        return tuple(d for d in self._diagnostics if d.code in codes)

    def get(self, name: object) -> AgentDef | None:
        """Case-insensitive lookup; returns the deterministic winner or ``None``."""
        if not isinstance(name, str):
            return None
        return self._by_key.get(name.casefold())

    def require(self, name: object) -> AgentDef:
        agent = self.get(name)
        if agent is None:
            raise AgentNotFoundError(name)
        return agent

    def __contains__(self, name: object) -> bool:
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self):
        return iter(self._agents)

    @property
    def read_only_names(self) -> tuple[str, ...]:
        return tuple(agent.name for agent in self._agents if agent.read_only)

    # -- index (progressive disclosure) ------------------------------------

    @property
    def index(self) -> tuple[AgentIndexEntry, ...]:
        if self._max_description_chars is None:
            descriptions = [agent.sanitized_description() for agent in self._agents]
        else:
            descriptions = [
                agent.sanitized_description(max_chars=self._max_description_chars)
                for agent in self._agents
            ]
        return tuple(
            AgentIndexEntry(
                name=agent.name,
                description=description,
                source=agent.source,
                model=agent.model,
                read_only=agent.read_only,
            )
            for agent, description in zip(self._agents, descriptions)
        )

    @property
    def snapshot(self) -> AgentIndex:
        return AgentIndex(
            entries=self.index,
            diagnostics=self._diagnostics,
            generation=self._generation,
        )

    def render_index(self, *, max_chars: int | None = None) -> str:
        return self.snapshot.render(max_chars=max_chars)

    # -- stable fingerprints ----------------------------------------------

    @property
    def fingerprints(self) -> Mapping[str, str]:
        """A stable ``name -> whole-definition fingerprint`` map for diffing."""
        return {agent.name: agent.fingerprint() for agent in self._agents}

    def fingerprint(self) -> str:
        """A stable digest of the whole discovered set (names + digests)."""
        payload = json.dumps(
            sorted(self.fingerprints.items()),
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint_key(agent: object) -> str:
        """A ``ManifestDiff`` key callable: prefer the definition's fingerprint."""
        method = getattr(agent, "fingerprint", None)
        if callable(method):
            return method()
        return repr(agent)

    # -- body (spawn time) -------------------------------------------------

    def _coerce_agent(self, agent: AgentDef | str) -> AgentDef:
        if isinstance(agent, AgentDef):
            return agent
        if isinstance(agent, str):
            return self.require(agent)
        raise AgentError("expected an AgentDef or agent name")

    def load_body(
        self, agent: AgentDef | str, *, max_bytes: int | None = None
    ) -> str:
        resolved = self._coerce_agent(agent)
        return resolved.load_body(
            max_bytes=self._max_body_bytes if max_bytes is None else max_bytes
        )

    # -- tool selection (a view, never a grant) ----------------------------

    def select_tools(
        self,
        agent: AgentDef | str,
        *,
        available: Iterable[str],
        profile: Iterable[str] | None = None,
        bundle_map: Mapping[str, Iterable[str]] | None = None,
        mutating: Iterable[str] = (),
    ) -> AgentToolSelection:
        """Narrow a definition's request to the parent's authority.

        The effective set is ``(available ∩ profile) ∩ requested`` minus the
        declared exclusions. A definition with no positive ``bundles``/``tools``
        **inherits** the ceiling rather than requesting nothing. ``explore`` and
        ``planner`` additionally strip every shell and mutating-filesystem tool --
        including any passed through ``mutating`` -- no matter what the file
        declares. A declaration can only ever remove from the ceiling; it can
        never add to it.
        """
        resolved = self._coerce_agent(agent)
        available_set = frozenset(str(tool) for tool in available)
        profile_set = (
            available_set
            if profile is None
            else frozenset(str(tool) for tool in profile)
        )
        ceiling = available_set & profile_set

        declared = not resolved.bundles and not resolved.tools
        bundles = bundle_map if bundle_map is not None else self._known_bundles
        requested: set[str] = set(ceiling) if declared else set()
        if not declared and bundles is not None:
            for bundle in resolved.bundles:
                requested.update(str(tool) for tool in bundles.get(bundle, ()))
        requested.update(resolved.tools)

        requested_set = frozenset(requested)
        selected = (ceiling & requested_set) - frozenset(resolved.excluded_tools)
        stripped = frozenset()
        if resolved.read_only:
            forbidden = FORBIDDEN_ROLE_TOOLS | frozenset(
                str(tool) for tool in mutating
            )
            stripped = selected & forbidden
            selected = selected - forbidden

        return AgentToolSelection(
            agent=resolved.name,
            available=available_set,
            profile=profile_set,
            ceiling=ceiling,
            requested=requested_set,
            selected=frozenset(selected),
            dropped=requested_set - selected,
            stripped=stripped,
            read_only=resolved.read_only,
        )

    # -- discovery ---------------------------------------------------------

    def refresh(self) -> AgentIndex:
        """Rescan every root and atomically replace the current index."""
        diagnostics: list[AgentDiagnostic] = []
        candidates: list[tuple[AgentSource, Path]] = []
        for tier, root in self._roots:
            if not root.is_dir():
                continue
            try:
                entries = sorted(
                    root.iterdir(), key=lambda p: (p.name.casefold(), p.name)
                )
            except OSError as exc:
                diagnostics.append(
                    AgentDiagnostic(
                        code=AgentDiagnosticCode.READ_ERROR,
                        message=f"cannot list agent root {root}: {exc}",
                        tier=tier,
                        path=str(root),
                    )
                )
                continue
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.suffix.lower() != AGENT_FILE_SUFFIX:
                    continue
                if entry.is_file():
                    candidates.append((tier, entry))

        winners: dict[str, AgentDef] = {}
        for tier, path in candidates:
            try:
                agent = self._load_agent(tier, path, diagnostics)
            except AgentOversizeError as exc:
                diagnostics.append(
                    self._failure(AgentDiagnosticCode.OVERSIZE, exc, tier, path)
                )
                continue
            except AgentParseError as exc:
                diagnostics.append(
                    self._failure(AgentDiagnosticCode.PARSE_ERROR, exc, tier, path)
                )
                continue
            except OSError as exc:
                diagnostics.append(
                    self._failure(AgentDiagnosticCode.READ_ERROR, exc, tier, path)
                )
                continue

            key = agent.name.casefold()
            existing = winners.get(key)
            if existing is None:
                winners[key] = agent
                continue
            if tier.precedence > existing.source.precedence:
                diagnostics.append(
                    AgentDiagnostic(
                        code=AgentDiagnosticCode.SHADOWED,
                        message=(
                            f"agent {existing.name!r} from "
                            f"{existing.source.value} is shadowed by "
                            f"{agent.name!r} from {tier.value}"
                        ),
                        tier=existing.source,
                        path=str(existing.path),
                        name=existing.name,
                        shadowed_by=agent.name,
                    )
                )
                winners[key] = agent
            elif tier.precedence < existing.source.precedence:
                diagnostics.append(
                    AgentDiagnostic(
                        code=AgentDiagnosticCode.SHADOWED,
                        message=(
                            f"agent {agent.name!r} from {tier.value} is shadowed "
                            f"by {existing.name!r} from {existing.source.value}"
                        ),
                        tier=tier,
                        path=str(path),
                        name=agent.name,
                        shadowed_by=existing.name,
                    )
                )
            else:
                diagnostics.append(
                    AgentDiagnostic(
                        code=AgentDiagnosticCode.CASE_COLLISION,
                        message=(
                            f"agent {agent.name!r} collides (case-insensitively) "
                            f"with {existing.name!r} in the {tier.value} tier"
                        ),
                        tier=tier,
                        path=str(path),
                        name=agent.name,
                        shadowed_by=existing.name,
                    )
                )

        # Reuse an unchanged immutable object so a manifest diff sees no change at
        # all (identity-first). The fingerprint deliberately ignores filesystem
        # paths, so provenance must *also* match: an identical definition promoted
        # from the user tier to the workspace tier is a different definition.
        previous = self._by_key
        resolved: dict[str, AgentDef] = {}
        for key, agent in winners.items():
            old = previous.get(key)
            if (
                old is not None
                and old.fingerprint() == agent.fingerprint()
                and old.provenance == agent.provenance
            ):
                resolved[key] = old
            else:
                resolved[key] = agent

        self._agents = tuple(
            sorted(resolved.values(), key=lambda a: (a.name.casefold(), a.name))
        )
        self._by_key = {agent.name.casefold(): agent for agent in self._agents}
        self._diagnostics = tuple(diagnostics)
        self._generation += 1
        return self.snapshot

    @staticmethod
    def _failure(
        code: AgentDiagnosticCode,
        exc: Exception,
        tier: AgentSource,
        path: Path,
    ) -> AgentDiagnostic:
        return AgentDiagnostic(
            code=code,
            message=str(exc),
            tier=tier,
            path=str(path),
        )

    def _load_agent(
        self,
        tier: AgentSource,
        path: Path,
        diagnostics: list[AgentDiagnostic],
    ) -> AgentDef:
        # One open, one read: declaration, body, and hashes all derive from the
        # same bytes, so a concurrent edit cannot split the snapshot.
        agent_file = read_agent_file(
            path,
            max_frontmatter_bytes=self._max_frontmatter_bytes,
            max_file_bytes=self._max_file_bytes,
        )
        parsed = agent_file.parsed
        self._check_declarations(parsed, tier, path, diagnostics)
        provenance = AgentProvenance(
            tier=tier,
            root=self._root_for(path),
            path=path,
            relpath=path.name,
            declaration_sha256=agent_file.declaration_sha256,
            file_size=agent_file.file_size,
        )
        return AgentDef(
            name=parsed.name,
            description=parsed.description,
            provenance=provenance,
            bundles=parsed.bundles,
            tools=parsed.tools,
            excluded_tools=parsed.excluded_tools,
            model=parsed.model,
            max_iterations=parsed.max_iterations,
            context_tokens=parsed.context_tokens,
            body=agent_file.body,
            body_sha256=agent_file.body_sha256,
            body_size=agent_file.body_size,
            file_sha256=agent_file.file_sha256,
            snapshotted=True,
        )

    def _root_for(self, path: Path) -> Path:
        for _tier, root in self._roots:
            try:
                if path.is_relative_to(root):
                    return root
            except ValueError:
                continue
        return path.parent

    def _check_declarations(
        self,
        parsed: ParsedFrontmatter,
        tier: AgentSource,
        path: Path,
        diagnostics: list[AgentDiagnostic],
    ) -> None:
        if self._known_tools is not None:
            for tool in parsed.tools:
                if tool not in self._known_tools:
                    diagnostics.append(
                        AgentDiagnostic(
                            code=AgentDiagnosticCode.UNKNOWN_TOOL,
                            message=(
                                f"agent {parsed.name!r} declares unknown tool "
                                f"{tool!r}; declarations never grant access"
                            ),
                            tier=tier,
                            path=str(path),
                            name=parsed.name,
                        )
                    )
        if self._known_bundles is not None:
            for bundle in parsed.bundles:
                if bundle not in self._known_bundles:
                    diagnostics.append(
                        AgentDiagnostic(
                            code=AgentDiagnosticCode.UNKNOWN_BUNDLE,
                            message=(
                                f"agent {parsed.name!r} declares unknown bundle "
                                f"{bundle!r}; declarations never expand a profile"
                            ),
                            tier=tier,
                            path=str(path),
                            name=parsed.name,
                        )
                    )
        if parsed.name.casefold() in READ_ONLY_ROLES:
            for tool in parsed.tools:
                if tool in FORBIDDEN_ROLE_TOOLS:
                    diagnostics.append(
                        AgentDiagnostic(
                            code=AgentDiagnosticCode.FORBIDDEN_TOOL,
                            message=(
                                f"read-only agent {parsed.name!r} declares "
                                f"forbidden tool {tool!r}; it will never hold it"
                            ),
                            tier=tier,
                            path=str(path),
                            name=parsed.name,
                        )
                    )
            for bundle in parsed.bundles:
                if bundle in FORBIDDEN_ROLE_BUNDLES:
                    diagnostics.append(
                        AgentDiagnostic(
                            code=AgentDiagnosticCode.FORBIDDEN_BUNDLE,
                            message=(
                                f"read-only agent {parsed.name!r} declares "
                                f"forbidden bundle {bundle!r}; it will never "
                                "expand to it"
                            ),
                            tier=tier,
                            path=str(path),
                            name=parsed.name,
                        )
                    )
