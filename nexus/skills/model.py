"""Immutable skill metadata (plan section 5.4).

Discovery produces a :class:`Skill`: the validated declaration plus provenance,
a deterministic resource inventory, bundled-tool candidate metadata, and a
bounded content snapshot. It is frozen, so a reload can never mutate a skill a
running turn already holds.

Progressive disclosure is structural, not a convention:

* the manager snapshots the body and bundled-resource bytes at refresh, so
  invocation under a pinned generation is generation-stable, but
  :attr:`SkillIndexEntry` -- the sanitized ``name: description`` pair the context
  layer renders -- carries no body and never touches the snapshot;
* bundled ``tools/*.py`` files appear as :class:`BundledToolCandidate` metadata
  and are never imported or registered by this layer.

:meth:`Skill.fingerprint` is a whole-skill digest (declaration, body, resources,
bundled tools) that is stable across an equal rebuild, so the manager can reuse
an unchanged immutable object and a manifest diff sees no churn.

The :class:`Skill` also satisfies the structural ``nexus.ext.manifest.Skill``
protocol (``name`` + ``description``), so a manifest can hold these objects
without ``nexus.skills`` importing ``nexus.ext``.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .errors import SkillOversizeError, SkillParseError, SkillStaleError
from .frontmatter import (
    MAX_DESCRIPTION_CHARS,
    MAX_SKILL_FILE_BYTES,
    MODEL_TIERS,
    ParsedFrontmatter,
    find_frontmatter_bounds,
    sanitize_description,
)
from .resources import (
    BundledToolCandidate,
    ResolvedResource,
    SkillResource,
    resolve_resource_snapshot,
)

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "SOURCE_PRECEDENCE",
    "BundledToolCandidate",
    "Skill",
    "SkillDiagnostic",
    "SkillDiagnosticCode",
    "SkillIndex",
    "SkillIndexEntry",
    "SkillProvenance",
    "SkillResource",
    "SkillSource",
]

#: Bodies are read whole at invocation; the cap matches the whole-file cap.
DEFAULT_MAX_BODY_BYTES = MAX_SKILL_FILE_BYTES


class SkillSource(StrEnum):
    """Where a skill was discovered, lowest to highest precedence.

    ``WORKSPACE_LEGACY`` is the read-only ``<workspace>/.nexus/skills``
    fallback (STATE_PLAN §5.4): it outranks ``USER`` so a project's own
    skills still beat the user's, but ``WORKSPACE`` (``.agents/skills``) wins
    any collision since writes always go there now.
    """

    BUILTIN = "builtin"
    USER = "user"
    WORKSPACE_LEGACY = "workspace_legacy"
    WORKSPACE = "workspace"

    @property
    def precedence(self) -> int:
        return SOURCE_PRECEDENCE[self]


#: Higher precedence wins a same-name collision: workspace > legacy > user > builtin.
SOURCE_PRECEDENCE: Mapping[SkillSource, int] = {
    SkillSource.BUILTIN: 0,
    SkillSource.USER: 1,
    SkillSource.WORKSPACE_LEGACY: 2,
    SkillSource.WORKSPACE: 3,
}


class SkillDiagnosticCode(StrEnum):
    """Machine-readable reasons a skill was skipped, shadowed, or flagged."""

    PARSE_ERROR = "parse_error"
    READ_ERROR = "read_error"
    OVERSIZE = "oversize"
    MISSING_SKILL_MD = "missing_skill_md"
    SHADOWED = "shadowed"
    CASE_COLLISION = "case_collision"
    UNKNOWN_ALLOWED_TOOL = "unknown_allowed_tool"
    UNKNOWN_BUNDLE = "unknown_bundle"


@dataclass(frozen=True)
class SkillDiagnostic:
    """A retained discovery diagnostic. Never silently dropped."""

    code: SkillDiagnosticCode
    message: str
    tier: SkillSource | None = None
    path: str = ""
    name: str | None = None
    shadowed_by: str | None = None


@dataclass(frozen=True)
class SkillProvenance:
    """Where a skill came from and a hash of its declaration.

    ``declaration_sha256`` covers only the frontmatter bytes, so it is stable
    across body edits and can be computed without reading the body.
    """

    tier: SkillSource
    root: Path
    directory: Path
    skill_md: Path
    relpath: str
    declaration_sha256: str
    file_size: int


@dataclass(frozen=True)
class Skill:
    """A discovered, immutable skill declaration plus a bounded content snapshot.

    The manager snapshots the body and bundled-resource bytes during
    :meth:`SkillManager.refresh`, so a generation-pinned skill keeps serving the
    bytes it was discovered with even after the files on disk change. When
    ``snapshotted`` is false (a hand-built :class:`Skill`, e.g. in a unit test)
    the body and resources are read from disk on demand instead.
    """

    name: str
    description: str
    provenance: SkillProvenance
    allowed_tools: tuple[str, ...] = ()
    bundles: tuple[str, ...] = ()
    model: str | None = None
    version: str = "1"
    resources: tuple[SkillResource, ...] = ()
    tool_candidates: tuple[BundledToolCandidate, ...] = ()
    # -- refresh-time snapshot (progressive disclosure keeps it out of the index)
    body: bytes | None = None
    body_sha256: str = ""
    body_size: int = 0
    file_sha256: str = ""
    snapshotted: bool = False
    parsed: ParsedFrontmatter | None = None

    # -- provenance conveniences ------------------------------------------

    @property
    def source(self) -> SkillSource:
        return self.provenance.tier

    @property
    def directory(self) -> Path:
        return self.provenance.directory

    @property
    def declaration_sha256(self) -> str:
        return self.provenance.declaration_sha256

    @property
    def has_body_snapshot(self) -> bool:
        return self.body is not None

    @property
    def model_is_tier(self) -> bool:
        """Whether ``model`` is a reserved tier name (never resolved here)."""
        return self.model in MODEL_TIERS

    # -- stable fingerprint ------------------------------------------------

    def fingerprint(self) -> str:
        """A whole-skill content digest: declaration, body, resources, tools.

        Stable across a rebuild that reproduced equal content, so an unchanged
        skill reuses its object and never churns a manifest diff. The digest
        deliberately ignores filesystem paths, timestamps, and the discovery
        generation; it changes exactly when the skill's *meaning* changes.
        """
        payload = {
            "name": self.name,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "bundles": list(self.bundles),
            "model": self.model,
            "version": self.version,
            "declaration": self.provenance.declaration_sha256,
            "body": self.body_sha256,
            "file": self.file_sha256,
            "resources": [
                {
                    "kind": resource.kind,
                    "path": resource.path,
                    "size": resource.size,
                    "sha256": resource.sha256,
                    "symlink": resource.symlink,
                    "oversized": resource.oversized,
                    "escaped": resource.escaped,
                }
                for resource in self.resources
            ],
            "tools": [
                {
                    "path": candidate.path,
                    "module": candidate.module,
                    "size": candidate.size,
                    "sha256": candidate.sha256,
                    "symlink": candidate.symlink,
                    "oversized": candidate.oversized,
                    "escaped": candidate.escaped,
                }
                for candidate in self.tool_candidates
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    # -- progressive disclosure -------------------------------------------

    def load_body(self, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> str:
        """Return the markdown body. Call this only on invocation.

        For a snapshotted skill the bytes captured at refresh are returned, so a
        later edit is invisible under the pinned generation. A hand-built skill
        falls back to reading from disk.
        """
        if self.body is not None:
            data = self.body
        else:
            path = self.provenance.skill_md
            try:
                with path.open("rb") as handle:
                    data = handle.read(max_bytes + 1)
            except OSError as exc:
                raise SkillParseError(
                    f"cannot read skill body from {path}: {exc}"
                ) from exc
            if len(data) > max_bytes:
                raise SkillOversizeError(
                    f"skill {self.name!r} body exceeds {max_bytes} bytes"
                )
            _, _, body_start = find_frontmatter_bounds(data)
            data = data[body_start:]
        if len(data) > max_bytes:
            raise SkillOversizeError(
                f"skill {self.name!r} body exceeds {max_bytes} bytes"
            )
        if b"\x00" in data:
            raise SkillParseError(f"skill {self.name!r} body contains a NUL byte")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillParseError(
                f"skill {self.name!r} body is not valid UTF-8"
            ) from exc

    def verify_snapshot(self) -> None:
        """Refuse when the on-disk ``SKILL.md`` no longer matches the snapshot.

        This is the explicit staleness check: a caller that wants to detect a
        file edited *after* discovery can call it and receive
        :class:`SkillStaleError` instead of silently reading newer bytes.
        """
        if not self.snapshotted or not self.file_sha256:
            return
        path = self.provenance.skill_md
        try:
            with path.open("rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise SkillStaleError(
                f"skill {self.name!r} snapshot cannot be verified: {exc}"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != self.file_sha256:
            raise SkillStaleError(
                f"skill {self.name!r} changed on disk after discovery"
            )

    def resolve_resource(
        self,
        relative: object,
        *,
        max_bytes: int,
    ) -> ResolvedResource:
        """Resolve a bundled resource from the snapshot (or disk when unsnapshotted)."""
        if self.snapshotted:
            return resolve_resource_snapshot(
                self.resources, relative, max_bytes=max_bytes
            )
        from .resources import resolve_resource as _resolve

        return _resolve(self.directory, relative, max_bytes=max_bytes)

    def sanitized_description(
        self, *, max_chars: int = MAX_DESCRIPTION_CHARS
    ) -> str:
        """The single-line, safe description used by the index."""
        return sanitize_description(self.description, max_chars=max_chars)

    def index_line(self, *, max_chars: int = MAX_DESCRIPTION_CHARS) -> str:
        """The single sanitized ``name: description`` line."""
        return f"{self.name}: {self.sanitized_description(max_chars=max_chars)}"

    def resources_of_kind(self, kind: str) -> tuple[SkillResource, ...]:
        return tuple(resource for resource in self.resources if resource.kind == kind)

    def tool_candidate_modules(self) -> tuple[str, ...]:
        return tuple(candidate.module for candidate in self.tool_candidates)


@dataclass(frozen=True)
class SkillIndexEntry:
    """One sanitized index row. Carries no body, path, or tool information."""

    name: str
    description: str
    source: SkillSource

    def line(self) -> str:
        return f"{self.name}: {self.description}"


@dataclass(frozen=True)
class SkillIndex:
    """An immutable snapshot of discovery: entries plus retained diagnostics."""

    entries: tuple[SkillIndexEntry, ...]
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    generation: int = 0

    def render(self, *, max_chars: int | None = None) -> str:
        """Render whole ``name: description`` lines, optionally under a budget.

        Whole lines are emitted until the next would exceed ``max_chars``, so a
        truncated index never ends mid-skill.
        """
        lines = [entry.line() for entry in self.entries]
        if max_chars is None:
            return "\n".join(lines)
        out: list[str] = []
        total = 0
        for line in lines:
            extra = len(line) + (1 if out else 0)
            if total + extra > max_chars:
                break
            out.append(line)
            total += extra
        return "\n".join(out)
