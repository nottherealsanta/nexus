"""SkillManager: deterministic discovery and progressive disclosure (plan 5.4).

Discovery is a pure scan of ordered roots::

    builtin/  <  ~/.nexus/skills/  <  <workspace>/.nexus/skills/ (legacy)  <  <workspace>/.agents/skills/

Each root contains one directory per skill with a ``SKILL.md``. Roots are sorted
by ``(precedence, path)`` and scanned low-to-high, so a ``.agents`` skill
shadows a legacy ``.nexus`` skill which shadows a user skill which shadows a
builtin skill (STATE_PLAN §5.4: writes always go to ``.agents``, ``.nexus`` is
read-only). Within one root, directories are sorted by ``(casefold name, name)``
and the **first** wins, so two roots of the same tier resolve deterministically.

What the manager guarantees:

* **Generation-stable snapshots.** Each refresh reads a ``SKILL.md`` exactly once
  and snapshots the body and bundled resources from those same opened bytes, so
  invocation under a pinned generation cannot observe a later edit. The context
  still sees progressive disclosure: :meth:`render_index` emits sanitized
  ``name: description`` lines only, and the body is absent from the idle index.
* **Object reuse.** An unchanged skill is reused as the same immutable object
  (compared by whole-skill fingerprint), so a manifest diff is a true no-op.
* **Retained diagnostics.** Every skipped file (parse/read/oversize failure),
  every shadowed skill, every case-fold collision, and every unknown declared
  tool/bundle is recorded. Nothing is silently dropped.
* **Case-fold safety.** Skills are keyed by ``name.casefold()``, so lookups are
  case-insensitive and no two winners can share a key.
* **Deletion fallback.** :meth:`refresh` rebuilds from disk, so deleting a
  workspace skill immediately resurfaces the lower-tier skill it shadowed.
* **Declarations are not grants.** ``allowed-tools``/``bundles`` are validated
  against injected known names but never expand a profile, register a tool, or
  change the manager's own surface. Bundled ``tools/*.py`` files are candidates.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..config.paths import legacy_project_dir, nexus_home, project_agents_dir
from .errors import (
    SkillError,
    SkillNotFoundError,
    SkillOversizeError,
    SkillParseError,
)
from .frontmatter import (
    MAX_FRONTMATTER_BYTES,
    MAX_SKILL_FILE_BYTES,
    read_skill_file,
)
from .model import (
    Skill,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillIndex,
    SkillIndexEntry,
    SkillProvenance,
    SkillSource,
)
from .resources import (
    DEFAULT_MAX_INVENTORY_BYTES,
    DEFAULT_MAX_RESOURCE_BYTES,
    BundledToolCandidate,
    ResolvedResource,
    build_inventory,
    build_tool_candidates,
)

__all__ = [
    "SKILL_FILE_NAME",
    "RootSpec",
    "SkillManager",
]

#: The file that makes a directory a skill.
SKILL_FILE_NAME = "SKILL.md"

#: A ``(tier, path)`` discovery root.
RootSpec = tuple[SkillSource, Path]


def _coerce_source(value: object) -> SkillSource:
    if isinstance(value, SkillSource):
        return value
    try:
        return SkillSource(str(value))
    except ValueError as exc:
        raise SkillError(f"unknown skill source tier {value!r}") from exc


def _normalize_roots(roots: Iterable[object] | None) -> tuple[RootSpec, ...]:
    out: list[RootSpec] = []
    seen: set[tuple[str, str]] = set()
    for item in roots or ():
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise SkillError("roots must be (source, path) pairs")
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


class SkillManager:
    """Discovers skills from ordered roots and serves sanitized metadata."""

    def __init__(
        self,
        *,
        roots: Iterable[object] | None = None,
        known_tools: Iterable[str] | None = None,
        known_bundles: Mapping[str, Iterable[str]] | Iterable[str] | None = None,
        max_file_bytes: int = MAX_SKILL_FILE_BYTES,
        max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
        max_inventory_bytes: int = DEFAULT_MAX_INVENTORY_BYTES,
        max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
        max_body_bytes: int = MAX_SKILL_FILE_BYTES,
        max_description_chars: int | None = None,
        build_resource_inventory: bool = True,
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
        self._max_inventory_bytes = max_inventory_bytes
        self._max_resource_bytes = max_resource_bytes
        self._max_body_bytes = max_body_bytes
        self._max_description_chars = max_description_chars
        self._build_resource_inventory = build_resource_inventory
        self._generation = 0
        self._skills: tuple[Skill, ...] = ()
        self._by_key: dict[str, Skill] = {}
        self._diagnostics: tuple[SkillDiagnostic, ...] = ()
        self.refresh()

    # -- construction ------------------------------------------------------

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        *,
        home: str | Path | None = None,
        builtin: str | Path | Iterable[str | Path] | None = None,
        **kwargs: Any,
    ) -> SkillManager:
        """Convenience roots.

        ``<builtin>`` < ``~/.nexus/skills`` < ``<workspace>/.nexus/skills``
        (legacy, read-only fallback) < ``<workspace>/.agents/skills``
        (STATE_PLAN §5.4; writes always go to ``.agents``).
        """
        roots: list[RootSpec] = []
        if builtin is None:
            builtin = Path(__file__).with_name("builtin")
        if isinstance(builtin, (str, Path)):
            builtin_paths: Iterable[str | Path] = (builtin,)
        else:
            builtin_paths = builtin
        for path in builtin_paths:
            roots.append((SkillSource.BUILTIN, Path(path)))
        roots.append((SkillSource.USER, nexus_home(home) / "skills"))
        roots.append(
            (SkillSource.WORKSPACE_LEGACY, legacy_project_dir(workspace) / "skills")
        )
        roots.append(
            (SkillSource.WORKSPACE, project_agents_dir(workspace) / "skills")
        )
        return cls(roots=roots, **kwargs)

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
    def skills(self) -> tuple[Skill, ...]:
        return self._skills

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self._skills)

    @property
    def diagnostics(self) -> tuple[SkillDiagnostic, ...]:
        return self._diagnostics

    @property
    def shadowed(self) -> tuple[SkillDiagnostic, ...]:
        codes = {
            SkillDiagnosticCode.SHADOWED,
            SkillDiagnosticCode.CASE_COLLISION,
        }
        return tuple(d for d in self._diagnostics if d.code in codes)

    @property
    def failures(self) -> tuple[SkillDiagnostic, ...]:
        codes = {
            SkillDiagnosticCode.PARSE_ERROR,
            SkillDiagnosticCode.READ_ERROR,
            SkillDiagnosticCode.OVERSIZE,
        }
        return tuple(d for d in self._diagnostics if d.code in codes)

    def get(self, name: object) -> Skill | None:
        """Case-insensitive lookup; returns the deterministic winner or ``None``."""
        if not isinstance(name, str):
            return None
        return self._by_key.get(name.casefold())

    def require(self, name: object) -> Skill:
        skill = self.get(name)
        if skill is None:
            raise SkillNotFoundError(name)
        return skill

    def __contains__(self, name: object) -> bool:
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self):
        return iter(self._skills)

    # -- index (progressive disclosure) ------------------------------------

    @property
    def index(self) -> tuple[SkillIndexEntry, ...]:
        if self._max_description_chars is None:
            descriptions = [skill.sanitized_description() for skill in self._skills]
        else:
            descriptions = [
                skill.sanitized_description(max_chars=self._max_description_chars)
                for skill in self._skills
            ]
        return tuple(
            SkillIndexEntry(
                name=skill.name,
                description=description,
                source=skill.source,
            )
            for skill, description in zip(self._skills, descriptions)
        )

    @property
    def snapshot(self) -> SkillIndex:
        return SkillIndex(
            entries=self.index,
            diagnostics=self._diagnostics,
            generation=self._generation,
        )

    def render_index(self, *, max_chars: int | None = None) -> str:
        return self.snapshot.render(max_chars=max_chars)

    # -- stable fingerprints ----------------------------------------------

    @property
    def fingerprints(self) -> Mapping[str, str]:
        """A stable ``name -> whole-skill fingerprint`` map for manifest diffing."""
        return {skill.name: skill.fingerprint() for skill in self._skills}

    def fingerprint(self) -> str:
        """A stable digest of the whole discovered set (names + skill digests)."""
        payload = json.dumps(
            sorted(self.fingerprints.items()),
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint_key(skill: object) -> str:
        """A ``ManifestDiff`` key callable: prefer the skill's own fingerprint."""
        method = getattr(skill, "fingerprint", None)
        if callable(method):
            return method()
        return repr(skill)

    # -- body and resources (invocation time) ------------------------------

    def _coerce_skill(self, skill: Skill | str) -> Skill:
        if isinstance(skill, Skill):
            return skill
        if isinstance(skill, str):
            return self.require(skill)
        raise SkillError("expected a Skill or skill name")

    def load_body(
        self, skill: Skill | str, *, max_bytes: int | None = None
    ) -> str:
        resolved = self._coerce_skill(skill)
        return resolved.load_body(
            max_bytes=self._max_body_bytes if max_bytes is None else max_bytes
        )

    def resolve_resource(
        self,
        skill: Skill | str,
        relative: object,
        *,
        max_bytes: int | None = None,
    ) -> ResolvedResource:
        resolved = self._coerce_skill(skill)
        return resolved.resolve_resource(
            relative,
            max_bytes=self._max_resource_bytes if max_bytes is None else max_bytes,
        )

    def read_resource(
        self,
        skill: Skill | str,
        relative: object,
        *,
        max_bytes: int | None = None,
    ) -> str:
        return self.resolve_resource(skill, relative, max_bytes=max_bytes).text

    def tool_candidates(self, skill: Skill | str) -> tuple[BundledToolCandidate, ...]:
        """The bundled ``tools/*.py`` candidates. Never imported or registered."""
        return self._coerce_skill(skill).tool_candidates

    def scoped_tool_descriptors(
        self,
        skill: Skill | str,
        catalog: Mapping[str, Any],
    ) -> tuple[Any, ...]:
        """Declared descriptors from ``catalog`` for one skill.

        This is a *view*: it resolves ``allowed-tools`` and expands ``bundles``
        through the injected bundle map, but it registers nothing and grants
        nothing. Unknown names are skipped (and were already diagnosed during
        discovery).
        """
        resolved = self._coerce_skill(skill)
        ordered: list[str] = []
        seen: set[str] = set()
        for name in resolved.allowed_tools:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        for bundle in resolved.bundles:
            for name in self._bundle_tools(bundle):
                if name not in seen:
                    seen.add(name)
                    ordered.append(name)
        return tuple(catalog[name] for name in ordered if name in catalog)

    def _bundle_tools(self, bundle: str) -> tuple[str, ...]:
        if self._known_bundles is None:
            return ()
        return self._known_bundles.get(bundle, ())

    # -- discovery ---------------------------------------------------------

    def refresh(self) -> SkillIndex:
        """Rescan every root and atomically replace the current index."""
        diagnostics: list[SkillDiagnostic] = []
        candidates: list[tuple[SkillSource, Path, Path]] = []
        for tier, root in self._roots:
            if not root.is_dir():
                continue
            try:
                entries = sorted(
                    root.iterdir(), key=lambda p: (p.name.casefold(), p.name)
                )
            except OSError as exc:
                diagnostics.append(
                    SkillDiagnostic(
                        code=SkillDiagnosticCode.READ_ERROR,
                        message=f"cannot list skill root {root}: {exc}",
                        tier=tier,
                        path=str(root),
                    )
                )
                continue
            for entry in entries:
                if not entry.is_dir():
                    continue
                skill_md = entry / SKILL_FILE_NAME
                if skill_md.is_file():
                    candidates.append((tier, entry, skill_md))

        winners: dict[str, Skill] = {}
        for tier, directory, skill_md in candidates:
            try:
                skill = self._load_skill(tier, directory, skill_md, diagnostics)
            except SkillOversizeError as exc:
                diagnostics.append(
                    self._failure(SkillDiagnosticCode.OVERSIZE, exc, tier, skill_md)
                )
                continue
            except SkillParseError as exc:
                diagnostics.append(
                    self._failure(SkillDiagnosticCode.PARSE_ERROR, exc, tier, skill_md)
                )
                continue
            except OSError as exc:
                diagnostics.append(
                    self._failure(SkillDiagnosticCode.READ_ERROR, exc, tier, skill_md)
                )
                continue

            key = skill.name.casefold()
            existing = winners.get(key)
            if existing is None:
                winners[key] = skill
                continue
            if tier.precedence > existing.source.precedence:
                diagnostics.append(
                    SkillDiagnostic(
                        code=SkillDiagnosticCode.SHADOWED,
                        message=(
                            f"skill {existing.name!r} from {existing.source.value} "
                            f"is shadowed by {skill.name!r} from {tier.value}"
                        ),
                        tier=existing.source,
                        path=str(existing.provenance.skill_md),
                        name=existing.name,
                        shadowed_by=skill.name,
                    )
                )
                winners[key] = skill
            elif tier.precedence < existing.source.precedence:
                diagnostics.append(
                    SkillDiagnostic(
                        code=SkillDiagnosticCode.SHADOWED,
                        message=(
                            f"skill {skill.name!r} from {tier.value} is shadowed "
                            f"by {existing.name!r} from {existing.source.value}"
                        ),
                        tier=tier,
                        path=str(skill_md),
                        name=skill.name,
                        shadowed_by=existing.name,
                    )
                )
            else:
                diagnostics.append(
                    SkillDiagnostic(
                        code=SkillDiagnosticCode.CASE_COLLISION,
                        message=(
                            f"skill {skill.name!r} collides (case-insensitively) "
                            f"with {existing.name!r} in the {tier.value} tier"
                        ),
                        tier=tier,
                        path=str(skill_md),
                        name=skill.name,
                        shadowed_by=existing.name,
                    )
                )

        # Reuse an unchanged immutable Skill object so a manifest diff sees no
        # change at all (identity-first), not merely an equal-but-rebuilt entry.
        # The fingerprint deliberately ignores filesystem paths, so provenance
        # (tier, root, directory, ``SKILL.md`` path) must *also* match: an
        # identical skill body promoted from the user tier to the workspace tier
        # is a different skill and must not keep the lower tier's object.
        previous = self._by_key
        resolved: dict[str, Skill] = {}
        for key, skill in winners.items():
            old = previous.get(key)
            if (
                old is not None
                and old.fingerprint() == skill.fingerprint()
                and old.provenance == skill.provenance
            ):
                resolved[key] = old
            else:
                resolved[key] = skill

        self._skills = tuple(
            sorted(resolved.values(), key=lambda s: (s.name.casefold(), s.name))
        )
        self._by_key = {skill.name.casefold(): skill for skill in self._skills}
        self._diagnostics = tuple(diagnostics)
        self._generation += 1
        return self.snapshot

    @staticmethod
    def _failure(
        code: SkillDiagnosticCode,
        exc: Exception,
        tier: SkillSource,
        skill_md: Path,
    ) -> SkillDiagnostic:
        return SkillDiagnostic(
            code=code,
            message=str(exc),
            tier=tier,
            path=str(skill_md),
        )

    def _load_skill(
        self,
        tier: SkillSource,
        directory: Path,
        skill_md: Path,
        diagnostics: list[SkillDiagnostic],
    ) -> Skill:
        # One open, one read: declaration, body, and hashes all derive from the
        # same bytes, so a concurrent edit cannot split the snapshot.
        skill_file = read_skill_file(
            skill_md,
            max_frontmatter_bytes=self._max_frontmatter_bytes,
            max_file_bytes=self._max_file_bytes,
        )
        parsed = skill_file.parsed
        self._check_declarations(parsed.name, parsed.allowed_tools, parsed.bundles,
                                 tier, skill_md, diagnostics)

        resources = (
            build_inventory(
                directory,
                max_bytes=self._max_inventory_bytes,
                with_content=True,
            )
            if self._build_resource_inventory
            else ()
        )
        tool_candidates = build_tool_candidates(
            directory, max_bytes=self._max_inventory_bytes
        )
        provenance = SkillProvenance(
            tier=tier,
            root=self._root_for(directory),
            directory=directory,
            skill_md=skill_md,
            relpath=f"{directory.name}/{SKILL_FILE_NAME}",
            declaration_sha256=skill_file.declaration_sha256,
            file_size=skill_file.file_size,
        )
        return Skill(
            name=parsed.name,
            description=parsed.description,
            provenance=provenance,
            allowed_tools=parsed.allowed_tools,
            bundles=parsed.bundles,
            model=parsed.model,
            version=parsed.version,
            resources=resources,
            tool_candidates=tool_candidates,
            body=skill_file.body,
            body_sha256=skill_file.body_sha256,
            body_size=skill_file.body_size,
            file_sha256=skill_file.file_sha256,
            snapshotted=True,
        )

    def _root_for(self, directory: Path) -> Path:
        for _tier, root in self._roots:
            try:
                if directory.is_relative_to(root):
                    return root
            except ValueError:
                continue
        return directory.parent

    def _check_declarations(
        self,
        name: str,
        allowed_tools: tuple[str, ...],
        bundles: tuple[str, ...],
        tier: SkillSource,
        skill_md: Path,
        diagnostics: list[SkillDiagnostic],
    ) -> None:
        if self._known_tools is not None:
            for tool in allowed_tools:
                if tool not in self._known_tools:
                    diagnostics.append(
                        SkillDiagnostic(
                            code=SkillDiagnosticCode.UNKNOWN_ALLOWED_TOOL,
                            message=(
                                f"skill {name!r} declares unknown allowed-tool "
                                f"{tool!r}; declarations never grant access"
                            ),
                            tier=tier,
                            path=str(skill_md),
                            name=name,
                        )
                    )
        if self._known_bundles is not None:
            for bundle in bundles:
                if bundle not in self._known_bundles:
                    diagnostics.append(
                        SkillDiagnostic(
                            code=SkillDiagnosticCode.UNKNOWN_BUNDLE,
                            message=(
                                f"skill {name!r} declares unknown bundle "
                                f"{bundle!r}; declarations never expand a profile"
                            ),
                            tier=tier,
                            path=str(skill_md),
                            name=name,
                        )
                    )
