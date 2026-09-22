"""Skills: restricted ``SKILL.md`` discovery with progressive disclosure (plan 5.4).

A skill is a directory with a ``SKILL.md`` file. This package exposes:

* :mod:`nexus.skills.frontmatter` -- the restricted, dependency-free declaration
  parser (six keys; no YAML library, no arbitrary YAML). ``model`` is opaque;
* :mod:`nexus.skills.model` -- immutable :class:`~nexus.skills.model.Skill`
  metadata, provenance, resource inventory, bundled-tool candidates, and a
  whole-skill fingerprint;
* :mod:`nexus.skills.resources` -- a fail-closed resolver for ``scripts/`` and
  ``references/`` files, with bounded snapshot support;
* :class:`~nexus.skills.manager.SkillManager` -- deterministic discovery across
  builtin/user/workspace roots, with workspace > user > builtin precedence. Each
  refresh snapshots the body and resources so invocation under a pinned
  generation cannot observe a later edit;
* :class:`~nexus.skills.activation.SkillActivation` -- the immutable,
  session/turn-local overlay that narrows authority without ever expanding it;
* :func:`~nexus.skills.invocation.render_invocation` -- a delimited, bounded
  invocation document carrying provenance and hashes.

The context layer sees only :meth:`SkillManager.render_index` (sanitized
``name: description`` lines). Bodies live in memory after refresh but are absent
from the idle index and load via :meth:`SkillManager.load_body`. Declarations
are never grants.
"""

from __future__ import annotations

from .activation import SkillActivation
from .errors import (
    SkillActivationError,
    SkillError,
    SkillNotFoundError,
    SkillOversizeError,
    SkillParseError,
    SkillResourceError,
    SkillSecurityError,
    SkillStaleError,
)
from .frontmatter import (
    DELIMITER,
    MAX_DESCRIPTION_CHARS,
    MAX_FRONTMATTER_BYTES,
    MAX_SKILL_FILE_BYTES,
    MODEL_INHERIT,
    MODEL_TIERS,
    ParsedFrontmatter,
    SkillFile,
    find_frontmatter_bounds,
    is_model_tier,
    parse_frontmatter,
    read_declaration,
    read_skill_file,
    sanitize_description,
    split_frontmatter,
    validate_skill_name,
)
from .invocation import (
    DEFAULT_MAX_OUTPUT_BYTES,
    SKILL_BEGIN_DELIMITER,
    SKILL_END_DELIMITER,
    SkillInvocation,
    render_invocation,
)
from .manager import SKILL_FILE_NAME, RootSpec, SkillManager
from .model import (
    DEFAULT_MAX_BODY_BYTES,
    SOURCE_PRECEDENCE,
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
    RESOURCE_KINDS,
    TOOLS_DIR,
    BundledToolCandidate,
    ResolvedResource,
    SkillResource,
    build_inventory,
    build_tool_candidates,
    normalize_resource_path,
    read_resource,
    resolve_resource,
    resolve_resource_path,
    resolve_resource_snapshot,
    sha256_hex,
)

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_INVENTORY_BYTES",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_RESOURCE_BYTES",
    "DELIMITER",
    "MAX_DESCRIPTION_CHARS",
    "MAX_FRONTMATTER_BYTES",
    "MAX_SKILL_FILE_BYTES",
    "MODEL_INHERIT",
    "MODEL_TIERS",
    "RESOURCE_KINDS",
    "SKILL_BEGIN_DELIMITER",
    "SKILL_END_DELIMITER",
    "SKILL_FILE_NAME",
    "SOURCE_PRECEDENCE",
    "TOOLS_DIR",
    "BundledToolCandidate",
    "ParsedFrontmatter",
    "ResolvedResource",
    "RootSpec",
    "Skill",
    "SkillActivation",
    "SkillActivationError",
    "SkillDiagnostic",
    "SkillDiagnosticCode",
    "SkillError",
    "SkillFile",
    "SkillIndex",
    "SkillIndexEntry",
    "SkillInvocation",
    "SkillManager",
    "SkillNotFoundError",
    "SkillOversizeError",
    "SkillParseError",
    "SkillProvenance",
    "SkillResource",
    "SkillResourceError",
    "SkillSecurityError",
    "SkillSource",
    "SkillStaleError",
    "build_inventory",
    "build_tool_candidates",
    "find_frontmatter_bounds",
    "is_model_tier",
    "normalize_resource_path",
    "parse_frontmatter",
    "read_declaration",
    "read_resource",
    "read_skill_file",
    "render_invocation",
    "resolve_resource",
    "resolve_resource_path",
    "resolve_resource_snapshot",
    "sanitize_description",
    "sha256_hex",
    "split_frontmatter",
    "validate_skill_name",
]
