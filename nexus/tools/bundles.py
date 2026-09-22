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
    "BUNDLES",
    "BUNDLE_NAMES",
    "DEFAULT_PROFILE",
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
        "fs": Bundle(
            name="fs",
            tools=("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"),
        ),
        "shell": Bundle(
            name="shell",
            tools=("Bash", "BashOutput", "KillShell"),
        ),
        "task": Bundle(
            name="task",
            tools=("Task", "TodoWrite"),
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
            tools=("Skill",),
        ),
        # Phase 5: everything bridged from MCP. The bundle owns no static names:
        # bridged tools declare ``bundle="mcp"`` and join this bundle's ordered
        # list through the profile-selection machinery (``ToolManager``).
        "mcp": Bundle(
            name="mcp",
            tools=(),
        ),
    }
)

BUNDLE_NAMES = frozenset(BUNDLES)

#: Research is read/search only: the fs bundle minus every mutating tool.
PROFILES: Mapping[str, Profile] = MappingProxyType(
    {
        "coding": Profile(
            name="coding",
            bundles=("fs", "shell", "task", "meta", "ext", "mcp"),
        ),
        "research": Profile(
            name="research",
            bundles=("fs", "mcp"),
            exclude=("Write", "Edit", "MultiEdit"),
            read_only=True,
        ),
        "chat": Profile(name="chat", bundles=()),
        "ops": Profile(name="ops", bundles=("shell", "mcp")),
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
    return tuple(PROFILES)


def profile_tools(name: str) -> frozenset[str]:
    """Resolve a profile to the exact set of tool names it enables."""
    return get_profile(name).tool_names()
