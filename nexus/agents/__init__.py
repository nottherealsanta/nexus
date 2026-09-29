"""Agents: restricted ``*.md`` subagent definitions (plan sections 5.6, 15.6-15.8).

A subagent definition is a single ``.md`` file with a restricted frontmatter
declaration and a system-prompt body. This package exposes:

* :mod:`nexus.agents.model` -- the restricted, dependency-free declaration parser
  (no YAML library) and the immutable
  :class:`~nexus.agents.model.AgentDef` with provenance, a whole-definition
  fingerprint, and a bounded body snapshot. ``model`` is opaque; it is validated
  for shape only and is never resolved here;
* :class:`~nexus.agents.manager.AgentManager` -- deterministic discovery across
  builtin/user/workspace roots, with workspace > user > builtin precedence, plus
  :meth:`~nexus.agents.manager.AgentManager.select_tools`, which narrows a
  definition's request to the parent's authority and can only ever restrict;
* the built-in roles in ``data/``: the root agents ``build`` and ``orchestrator`` and the subagents
  ``advisor`` (read-only), ``task`` and ``quick``. They are global; edits land in
  ``~/.nexus/agents``. :func:`~nexus.agents.manager.retire_seeded_roles` retires
  untouched copies older releases seeded into workspaces, and
  :func:`~nexus.agents.manager.seed_workspace_roles` remains as opt-in seeding;
* :class:`~nexus.agents.runner.SubagentRunner` -- the bounded nested run behind
  the ``Task`` tool. It computes ``parent_tools & role_tools & requested_tools``,
  clamps the tier to ``max_tier``, shares one concurrency-safe tree budget
  (depth, concurrent, fan-out, aggregate token/cost), allocates the
  ``<parent>/sub/<n>`` child session, relays the child's events with agent
  metadata, and runs the child through an injected ``RuntimeFactory`` /
  ``SessionFacade`` (never ``nexus.runtime``).

The context layer sees only :meth:`AgentManager.render_index` (sanitized
``name: description`` lines). Bodies live in memory after refresh but are absent
from the idle index and load via :meth:`AgentManager.load_body` only when a
subagent is spawned. Declarations are never grants.
"""

from __future__ import annotations

from .manager import (
    AGENT_FILE_SUFFIX,
    DATA_DIR_NAME,
    LEGACY_AGENT_ALIASES,
    SEED_MARKER_NAME,
    SEED_VERSION,
    SEEDED_ROLES,
    AgentManager,
    AgentToolSelection,
    RootSpec,
    SeedReport,
    retire_seeded_roles,
    seed_workspace_roles,
)
from .model import (
    AGENT_CONTEXTS,
    DEFAULT_AGENT_COLORS,
    DEFAULT_MAX_BODY_BYTES,
    DELIMITER,
    FORBIDDEN_ROLE_BUNDLES,
    FORBIDDEN_ROLE_TOOLS,
    MAX_AGENT_FILE_BYTES,
    MAX_BUNDLES,
    MAX_CONTEXT_TOKENS,
    MAX_DESCRIPTION_CHARS,
    MAX_FALLBACKS,
    MAX_FRONTMATTER_BYTES,
    MAX_ITERATIONS,
    MAX_LIST_ITEMS,
    MAX_MODEL_CHARS,
    MAX_NAME_CHARS,
    MAX_PROVIDER_CHARS,
    MAX_TOOLS,
    MODEL_INHERIT,
    MODEL_TIERS,
    MUTATING_FS_TOOLS,
    READ_ONLY_ROLES,
    REASONING_EFFORTS,
    SHELL_TOOLS,
    SOURCE_PRECEDENCE,
    AgentDef,
    AgentDiagnostic,
    AgentDiagnosticCode,
    AgentError,
    AgentFile,
    AgentIndex,
    AgentIndexEntry,
    AgentNotFoundError,
    AgentOversizeError,
    AgentParseError,
    AgentProvenance,
    AgentSeedError,
    AgentSource,
    AgentStaleError,
    ParsedFrontmatter,
    default_agent_color,
    find_frontmatter_bounds,
    is_model_tier,
    parse_frontmatter,
    read_agent_bytes,
    read_agent_file,
    read_declaration,
    sanitize_description,
    split_frontmatter,
    validate_agent_name,
)
from .runner import (
    AGENT_CLAMPED,
    AGENT_COMPLETED,
    AGENT_SPAWNED,
    CHILD_SESSION_SEGMENT,
    DEFAULT_CHILD_TYPE,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FANOUT,
    DEFAULT_MAX_TIER,
    MAX_REPORTED_FILES,
    ChildRuntime,
    ChildSpec,
    DefaultSessionFacade,
    RuntimeFactory,
    SessionFacade,
    SubagentBudget,
    SubagentError,
    SubagentOutcome,
    SubagentReservation,
    SubagentResult,
    SubagentRunner,
    SubagentUsage,
    TaskRequest,
    files_changed,
)

__all__ = [
    "AGENT_CLAMPED",
    "AGENT_COMPLETED",
    "AGENT_CONTEXTS",
    "AGENT_FILE_SUFFIX",
    "AGENT_SPAWNED",
    "CHILD_SESSION_SEGMENT",
    "DATA_DIR_NAME",
    "DEFAULT_AGENT_COLORS",
    "DEFAULT_CHILD_TYPE",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FANOUT",
    "DEFAULT_MAX_TIER",
    "DELIMITER",
    "FORBIDDEN_ROLE_BUNDLES",
    "FORBIDDEN_ROLE_TOOLS",
    "LEGACY_AGENT_ALIASES",
    "MAX_AGENT_FILE_BYTES",
    "MAX_BUNDLES",
    "MAX_CONTEXT_TOKENS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_FALLBACKS",
    "MAX_FRONTMATTER_BYTES",
    "MAX_ITERATIONS",
    "MAX_LIST_ITEMS",
    "MAX_MODEL_CHARS",
    "MAX_NAME_CHARS",
    "MAX_PROVIDER_CHARS",
    "MAX_REPORTED_FILES",
    "MAX_TOOLS",
    "MODEL_INHERIT",
    "MODEL_TIERS",
    "MUTATING_FS_TOOLS",
    "READ_ONLY_ROLES",
    "REASONING_EFFORTS",
    "SEEDED_ROLES",
    "SEED_MARKER_NAME",
    "SEED_VERSION",
    "SHELL_TOOLS",
    "SOURCE_PRECEDENCE",
    "AgentDef",
    "AgentDiagnostic",
    "AgentDiagnosticCode",
    "AgentError",
    "AgentFile",
    "AgentIndex",
    "AgentIndexEntry",
    "AgentManager",
    "AgentNotFoundError",
    "AgentOversizeError",
    "AgentParseError",
    "AgentProvenance",
    "AgentSeedError",
    "AgentSource",
    "AgentStaleError",
    "AgentToolSelection",
    "ChildRuntime",
    "ChildSpec",
    "DefaultSessionFacade",
    "ParsedFrontmatter",
    "RootSpec",
    "RuntimeFactory",
    "SeedReport",
    "SessionFacade",
    "SubagentBudget",
    "SubagentError",
    "SubagentOutcome",
    "SubagentReservation",
    "SubagentResult",
    "SubagentRunner",
    "SubagentUsage",
    "TaskRequest",
    "default_agent_color",
    "files_changed",
    "find_frontmatter_bounds",
    "is_model_tier",
    "parse_frontmatter",
    "read_agent_bytes",
    "read_agent_file",
    "read_declaration",
    "retire_seeded_roles",
    "sanitize_description",
    "seed_workspace_roles",
    "split_frontmatter",
    "validate_agent_name",
]
