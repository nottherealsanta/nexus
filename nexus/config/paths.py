"""Path resolution and containment (plan sections 5.3, 7).

Containment is enforced after ``realpath`` so ``../`` and symlink escapes fail
closed. This is the small piece of the permission engine that Phase 0 needs
because ``Config.read`` already promises it.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..errors import ConfigError

#: Environment override for the global state root (tests, power users).
NEXUS_HOME_ENV = "NEXUS_HOME"

#: Project-local extension directory (skills, agents, tools, hooks, mcp.json).
PROJECT_AGENTS_DIRNAME = ".agents"

#: Legacy project-local directory; read as a lower-precedence fallback only.
LEGACY_PROJECT_DIRNAME = ".nexus"


def nexus_home(home: Path | str | None = None) -> Path:
    """Global state root: ``$NEXUS_HOME`` or ``<home>/.nexus`` (STATE_PLAN §3).

    An explicit ``home`` wins over the environment so callers that already
    thread ``home=`` stay hermetic.
    """
    if home is not None:
        return Path(home).expanduser() / ".nexus"
    override = os.environ.get(NEXUS_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".nexus"


def state_db_path(home: Path | str | None = None) -> Path:
    """The one SQLite state database shared by every project."""
    return nexus_home(home) / "nexus.db"


def project_key(workspace: Path | str) -> str:
    """Stable, filesystem-safe digest of the resolved workspace path."""
    resolved = str(Path(workspace).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def project_state_dir(workspace: Path | str, home: Path | str | None = None) -> Path:
    """Per-project machine state (cache, logs, stage, trash, locks)."""
    return nexus_home(home) / "projects" / project_key(workspace)


def project_agents_dir(workspace: Path | str) -> Path:
    """``<workspace>/.agents``: where project extensions are written."""
    return Path(workspace) / PROJECT_AGENTS_DIRNAME


def legacy_project_dir(workspace: Path | str) -> Path:
    """``<workspace>/.nexus``: legacy, read-only fallback location."""
    return Path(workspace) / LEGACY_PROJECT_DIRNAME


def user_config_path(home: Path) -> Path:
    return Path(home) / ".nexus" / "config.toml"


def user_credentials_path(home: Path) -> Path:
    return Path(home) / ".nexus" / "credentials.json"


def workspace_config_path(workspace: Path) -> Path:
    return Path(workspace) / "nexus.toml"


def workspace_settings_config_path(workspace: Path) -> Path:
    """Canonical Settings-console project config (STATE_PLAN §5.4: ``.agents``)."""
    return project_agents_dir(workspace) / "nexus.toml"


def legacy_workspace_settings_config_path(workspace: Path) -> Path:
    """Read-only fallback: the pre-§5.4 Settings-console project config."""
    return legacy_project_dir(workspace) / "nexus.toml"


def resolve_within(workspace: Path, filename: str) -> Path:
    """Resolve ``filename`` under ``workspace``, following symlinks.

    Raises :class:`ConfigError` if the resolved target leaves the workspace, or
    if the name is not a usable path (for example an embedded NUL byte).
    """
    root = Path(workspace).resolve()
    try:
        candidate = (root / filename).resolve()
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Invalid context file path: {filename!r}") from exc
    if not candidate.is_relative_to(root):
        raise ConfigError(f"Context file must be inside workspace: {filename}")
    return candidate


__all__ = [
    "LEGACY_PROJECT_DIRNAME",
    "NEXUS_HOME_ENV",
    "PROJECT_AGENTS_DIRNAME",
    "legacy_project_dir",
    "legacy_workspace_settings_config_path",
    "nexus_home",
    "project_agents_dir",
    "project_key",
    "project_state_dir",
    "resolve_within",
    "state_db_path",
    "user_config_path",
    "user_credentials_path",
    "workspace_config_path",
    "workspace_settings_config_path",
]
