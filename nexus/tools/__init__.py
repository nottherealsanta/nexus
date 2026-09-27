"""Tool contracts, bundles, permissions, and the manager (plan sections 3.4, 5.3).

Phase 2 is delivered in packets. This package now exposes:

* :mod:`nexus.tools.spec` — :class:`ToolSpec`, :class:`ToolCall`,
  :class:`ToolContext`, :class:`ToolExecutionResult`, and the explicit
  ``job_registry``/``todo_store`` service seams;
* :mod:`nexus.tools.bundles` — built-in bundles and profiles;
* :mod:`nexus.tools.permissions` — the rule engine, path security, and approval
  primitives;
* :mod:`nexus.tools.manager` — :class:`ToolManager`: the immutable selected
  registry/catalog, strict JSON Schema validation, preparation/gating seam,
  concurrency planning, per-tool timeouts, cancellation, result capping, and
  event emission.

``nexus.tools.PreparedCall`` remains the permission-layer type; the manager's
batch entry is exported as :data:`ManagerPreparedCall` to avoid the collision.
"""
from __future__ import annotations

from .bundles import (
    BASE_TOOL_AVAILABILITY,
    BUNDLE_NAMES,
    BUNDLES,
    DEFAULT_PROFILE,
    PROFILE_NAMES,
    PROFILES,
    Bundle,
    Profile,
    UnknownBundleError,
    UnknownProfileError,
    all_bundles,
    bundle_tools,
    get_bundle,
    get_profile,
    profile_names,
    profile_tools,
)
from .manager import (
    CHARS_PER_TOKEN,
    DuplicateToolError,
    PreparedBatch,
    ToolInputError,
    ToolManager,
    ToolManagerError,
    ToolSelectionError,
    validate_tool_input,
)
from .manager import PreparedCall as ManagerPreparedCall
from .permissions import (
    ApprovalBroker,
    BatchPlan,
    Decision,
    Evaluation,
    Grant,
    Outcome,
    PathGuard,
    PathSecurityError,
    PermissionEngine,
    PermissionRequest,
    PermissionRuleError,
    PreparedCall,
    ResolvedPath,
    Rule,
    collect_grants,
    grant_for_decision,
    parse_rule,
)
from .spec import (
    CancelTokenView,
    JobRegistryView,
    PermissionKeyFn,
    ProgressEmitter,
    RegisteredTool,
    TodoStoreView,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolFn,
    ToolSpec,
    ToolSpecError,
    validate_input_schema,
    validate_tool_name,
)

__all__ = [
    "BASE_TOOL_AVAILABILITY",
    "BUNDLES",
    "BUNDLE_NAMES",
    "CHARS_PER_TOKEN",
    "DEFAULT_PROFILE",
    "PROFILES",
    "PROFILE_NAMES",
    "ApprovalBroker",
    "BatchPlan",
    "Bundle",
    "CancelTokenView",
    "Decision",
    "DuplicateToolError",
    "Evaluation",
    "Grant",
    "JobRegistryView",
    "ManagerPreparedCall",
    "Outcome",
    "PathGuard",
    "PathSecurityError",
    "PermissionEngine",
    "PermissionKeyFn",
    "PermissionRequest",
    "PermissionRuleError",
    "PreparedBatch",
    "PreparedCall",
    "Profile",
    "ProgressEmitter",
    "RegisteredTool",
    "ResolvedPath",
    "Rule",
    "TodoStoreView",
    "ToolCall",
    "ToolContext",
    "ToolExecutionResult",
    "ToolFn",
    "ToolInputError",
    "ToolManager",
    "ToolManagerError",
    "ToolSelectionError",
    "ToolSpec",
    "ToolSpecError",
    "UnknownBundleError",
    "UnknownProfileError",
    "all_bundles",
    "bundle_tools",
    "collect_grants",
    "get_bundle",
    "get_profile",
    "grant_for_decision",
    "parse_rule",
    "profile_names",
    "profile_tools",
    "validate_input_schema",
    "validate_tool_input",
    "validate_tool_name",
]
