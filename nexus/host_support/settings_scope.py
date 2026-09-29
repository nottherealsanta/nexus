"""Single path policy for Settings console reads and mutations (TAUI §4.2).

Project-scope reads and writes target ``<project>/.agents`` (STATE_PLAN §5.4):
the legacy ``<project>/.nexus`` is a read-only fallback the extension managers
still discover, but Settings console mutations always land in ``.agents``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..config.paths import project_agents_dir
from ..errors import ConfigError

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
BLOCKED = frozenset(
    {
        "credentials.json",
        "sessions",
        "cache",
        "daemon",
        "daemon.sock",
        "daemon.pid",
        "daemon.lock",
        # The shared session state database (STATE_PLAN §5.4) is machine
        # state, never reachable through the Settings console.
        "nexus.db",
    }
)
CATEGORIES = ("agents", "skills", "tools", "hooks", "mcp", "config", "soul")


@dataclass(frozen=True)
class SettingsTarget:
    root: Path
    display: str
    category: str
    item_id: str
    path: Path


def settings_root(runtime: object, scope: str) -> tuple[Path, str]:
    if scope == "global":
        home = Path.home().resolve()
        return home / ".nexus", "~/.nexus"
    if scope == "project":
        workspace = getattr(runtime, "workspace", None)
        if not isinstance(workspace, (str, Path)):
            raise ConfigError("project settings are unavailable")
        return project_agents_dir(Path(workspace).resolve()), "<project>/.agents"
    raise ConfigError("scope must be 'global' or 'project'")


def settings_target(runtime: object, scope: str, category: str, item_id: str) -> SettingsTarget:
    root, display = settings_root(runtime, scope)
    if root.is_symlink():
        raise ConfigError("settings root is a symlink")
    if category not in CATEGORIES:
        raise ConfigError("unknown settings category")
    if not isinstance(item_id, str) or not item_id or len(item_id) > 256 or "\\" in item_id:
        raise ConfigError("invalid settings item id")
    if category == "agents":
        if not _NAME.fullmatch(item_id): raise ConfigError("invalid agent id")
        relative = Path("agents") / f"{item_id}.md"
    elif category == "skills":
        if not _NAME.fullmatch(item_id): raise ConfigError("invalid skill id")
        relative = Path("skills") / item_id / "SKILL.md"
    elif category == "tools":
        if not _NAME.fullmatch(item_id): raise ConfigError("invalid tool id")
        relative = Path("tools") / f"{item_id}.py"
    elif category in {"hooks", "mcp", "soul"}:
        expected = {"hooks": "hooks.toml", "mcp": "mcp.json", "soul": "SOUL.md"}[category]
        if item_id not in {expected, category}: raise ConfigError("invalid settings item id")
        relative = Path(expected)
    else:
        expected = "config.toml" if scope == "global" else "nexus.toml"
        if item_id not in {expected, "config"}: raise ConfigError("invalid settings item id")
        relative = Path(expected)
    if relative.parts[0] in BLOCKED:
        raise ConfigError("settings path is blocked")
    candidate = root / relative
    # Reject symlinks in every existing component; resolving then containing is
    # also required to catch races/parent substitutions and symlink escapes.
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ConfigError("settings path crosses a symlink")
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(resolved_root):
        raise ConfigError("settings path escapes its scope")
    return SettingsTarget(root, display, category, item_id, candidate)


__all__ = ["BLOCKED", "CATEGORIES", "SettingsTarget", "settings_root", "settings_target"]
