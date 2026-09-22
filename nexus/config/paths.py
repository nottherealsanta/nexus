"""Path resolution and containment (plan sections 5.3, 7).

Containment is enforced after ``realpath`` so ``../`` and symlink escapes fail
closed. This is the small piece of the permission engine that Phase 0 needs
because ``Config.read`` already promises it.
"""
from __future__ import annotations

from pathlib import Path

from ..errors import ConfigError


def user_config_path(home: Path) -> Path:
    return Path(home) / ".nexus" / "config.toml"


def user_credentials_path(home: Path) -> Path:
    return Path(home) / ".nexus" / "credentials.json"


def workspace_config_path(workspace: Path) -> Path:
    return Path(workspace) / "nexus.toml"


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
    "resolve_within",
    "user_config_path",
    "user_credentials_path",
    "workspace_config_path",
]
