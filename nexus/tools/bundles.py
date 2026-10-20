"""Built-in bundle and profile definitions (plan section 5.3).

Bundles group tools so a profile can say *what the harness is for* without any
code in ``core/`` or the tool manager knowing what "coding" means. This packet
ships the Phase 2 bundles (``fs``, ``shell``, ``task``) plus the Phase 4
self-extension bundles (``meta``: reload/inspect/author; ``ext``: skill
invocation), and the four built-in profiles. Unknown profiles **fail closed**:
resolution raises rather than silently granting no tools.

There is deliberately no custom/table-defined profile support yet; later packets
may add a config table that composes the same primitives.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..errors import ConfigError

__all__ = [
    "BASE_TOOL_AVAILABILITY",
    "BUNDLES",
    "BUNDLE_NAMES",
    "DEFAULT_PROFILE",
    "MANAGER_BUNDLE",
    "MANAGER_PROFILE",
    "MANAGER_TOOL_NAMES",
    "RESERVED_BUNDLES",
    "RESERVED_PROFILES",
    "PROFILES",
    "PROFILE_NAMES",
    "Bundle",
    "Profile",
    "UnknownBundleError",
    "UnknownProfileError",
    "all_bundles",
    "bundle_tools",
    "get_bundle",
    "get_profile",
    "profile_names",
    "profile_tools",
]


#: The manager session's eight tools, in model-facing order (plan 02 section 3.2).
MANAGER_TOOL_NAMES: tuple[str, ...] = (
    "sessions_find",
    "session_launch",
    "session_status",
    "session_wait",
    "session_result",
    "session_cancel",
    "session_send",
    "session_show",
)
MANAGER_BUNDLE = "manager"
MANAGER_PROFILE = "manager"
#: Bundles and profiles that only the packaged manager session may use.
RESERVED_BUNDLES = frozenset({MANAGER_BUNDLE})
RESERVED_PROFILES = frozenset({MANAGER_PROFILE})


class UnknownBundleError(ConfigError):
    """A bundle name is not one of the built-in bundles."""


class UnknownProfileError(ConfigError):
    """A profile name is not one of the built-in profiles (fail closed)."""


@dataclass(frozen=True)
class Bundle:
    """A named, ordered group of built-in tool names."""

    name: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    """A composition of bundles with optional include/exclude tool filters.

    ``read_only`` profiles additionally drop every *mutating* tool at selection
    time. That matters for dynamically named tools (MCP's ``mcp__<server>__*``)
    which a static ``exclude`` list cannot name: a read-only profile never
    enables a tool that declares ``mutates = True``.
    """

    name: str
    bundles: tuple[str, ...]
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    read_only: bool = False

    def tool_names(self) -> frozenset[str]:
        names: set[str] = set()
        for bundle in self.bundles:
            names.update(get_bundle(bundle).tools)
        names.update(self.include)
        names.difference_update(self.exclude)
        return frozenset(names)


BUNDLES: Mapping[str, Bundle] = MappingProxyType(
    {
        "code": Bundle(name="code", tools=("code",)),
        "fs": Bundle(
            name="fs",
            tools=("read", "glob", "grep", "edit", "write"),
        ),
        "patch": Bundle(
            name="patch",
            tools=("apply_patch",),
        ),
        "shell": Bundle(
            name="shell",
            tools=("bash",),
        ),
        "legacy_shell": Bundle(
            name="legacy_shell",
            tools=("BashOutput", "KillShell"),
        ),
        "legacy_fs": Bundle(
            name="legacy_fs",
            tools=("ls", "multiedit"),
        ),
        "task": Bundle(
            name="task",
            tools=("subagent", "todowrite", "question"),
        ),
        "web": Bundle(
            name="web",
            tools=("webfetch", "websearch"),
        ),
        # Phase 4 self-extension controls. ``meta`` owns the reload/inspect and
        # extension-authoring tools; ``ext`` owns the skill-invocation tool
        # (skills are extension-tier data, loaded progressively).
        "meta": Bundle(
            name="meta",
            tools=("ReloadExtensions", "ListExtensions", "WriteTool"),
        ),
        "ext": Bundle(
            name="ext",
            tools=("skill",),
        ),
        # Phase 5: everything bridged from MCP. The bundle owns no static names:
        # bridged tools declare ``bundle="mcp"`` and join this bundle's ordered
        # list through the profile-selection machinery (``ToolManager``).
        "mcp": Bundle(
            name="mcp",
            tools=(),
        ),
        # Plan 02: the manager session's whole catalog. Reserved: no profile a
        # user can select, no role and no extension may include this bundle;
        # the runtime builds the manager catalog structurally from it.
        "manager": Bundle(
            name="manager",
            tools=MANAGER_TOOL_NAMES,
        ),
    }
)

BUNDLE_NAMES = frozenset(BUNDLES)

# Baseline availability explanations for tools that require runtime services.
# Runtime host listings replace these with config-specific reasons.
BASE_TOOL_AVAILABILITY: Mapping[str, str] = MappingProxyType({
    "websearch": "Local search is disabled and no allowed HTTPS SearXNG instance is configured",
    "webfetch": "Web fetching is disabled or the outbound HTTP service is unavailable",
})

#: Research is read/search only: the fs bundle minus every mutating tool.
PROFILES: Mapping[str, Profile] = MappingProxyType(
    {
        "coding": Profile(
            name="coding",
            bundles=("fs", "patch", "shell", "task", "web", "ext", "mcp", "code"),
        ),
        # Extension-management tools are opt-in: the default coding vocabulary
        # remains lowercase, with this profile for callers that want meta.
        "coding_meta": Profile(
            name="coding_meta",
            bundles=("fs", "patch", "shell", "task", "web", "ext", "mcp", "meta"),
        ),
        "research": Profile(
            name="research",
            bundles=("fs", "task", "web", "ext", "mcp"),
            include=("subagent", "todowrite", "question", "skill"),
            exclude=("write", "edit", "bash"),
            read_only=True,
        ),
        "chat": Profile(name="chat", bundles=()),
        "ops": Profile(name="ops", bundles=("shell", "mcp")),
        # Dedicated manager profile (reserved, see RESERVED_PROFILES): exactly
        # the six manager tools; no MCP, skills, shell, fs or task bundle.
        "manager": Profile(name=MANAGER_PROFILE, bundles=(MANAGER_BUNDLE,)),
    }
)

PROFILE_NAMES = frozenset(PROFILES)

DEFAULT_PROFILE = "coding"


def all_bundles() -> Mapping[str, Bundle]:
    return BUNDLES


def get_bundle(name: str) -> Bundle:
    if not isinstance(name, str) or name not in BUNDLES:
        raise UnknownBundleError(
            f"Unknown bundle {name!r}; expected one of "
            f"{', '.join(sorted(BUNDLES))}"
        )
    return BUNDLES[name]


def bundle_tools(name: str) -> tuple[str, ...]:
    return get_bundle(name).tools


def get_profile(name: str) -> Profile:
    if not isinstance(name, str) or name not in PROFILES:
        raise UnknownProfileError(
            f"Unknown profile {name!r}; expected one of "
            f"{', '.join(sorted(PROFILES))}"
        )
    return PROFILES[name]


def profile_names() -> tuple[str, ...]:
    """The user-selectable profiles (reserved profiles are not listed)."""
    return tuple(name for name in PROFILES if name not in RESERVED_PROFILES)


def profile_tools(name: str) -> frozenset[str]:
    """Resolve a profile to the exact set of tool names it enables."""
    return get_profile(name).tool_names()
