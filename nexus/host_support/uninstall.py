"""``nexus uninstall``: remove Nexus and everything it stored on this machine (plans/install.md).

Client-side and local, like ``nexus update``. :func:`uninstall_plan` lists what
exists (the Nexus home with every session, credential, setting, extension, log
and the dictation and speech models; the private daemon socket directory; the
terminal preferences; the speech models in the Hugging Face cache) so the CLI can
show it and ask before anything is removed. Project files inside workspaces
(``.agents/``, ``nexus.toml``, ``AGENTS.md``) belong to those projects and are
never touched. A Nexus home that does not look like one (the user's home, ``/``,
or a custom ``NEXUS_HOME`` without ``nexus.db``) is refused rather than deleted.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config.paths import nexus_home
from .install import PACKAGE
from .socket_dir import fallback_daemon_dir
from .speech import _LEGACY_REPO_DIR, _REPO_DIR, _hub_cache

#: Sizes are informational; stop counting a tree after this many entries.
_SIZE_MAX_ENTRIES = 200_000


@dataclass(frozen=True)
class Target:
    path: Path
    label: str
    size: int


def _size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = seen = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            seen += 1
            if seen > _SIZE_MAX_ENTRIES:
                return total
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _exists(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def unsafe_home_reason(home: Path) -> str | None:
    """Why ``home`` must not be deleted as a whole, or ``None`` when it is a Nexus home."""
    resolved = home.expanduser().resolve()
    user_home = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == user_home or resolved in user_home.parents:
        return f"{home} is not a Nexus home directory"
    if home.name != ".nexus" and not (home / "nexus.db").exists():
        return f"{home} (NEXUS_HOME) does not look like a Nexus home: it has no nexus.db"
    return None


def _tui_preferences() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "nexus" / "tui.json"


def uninstall_plan(home: str | Path | None = None) -> list[Target]:
    """Everything Nexus stored on this machine that exists now, with sizes."""
    root = nexus_home(home)
    hub = _hub_cache()
    candidates = [
        (root, "Nexus home: all sessions (nexus.db), credentials, settings, user extensions, "
               "logs, caches, the dictation model and the speech phonemizer"),
        (fallback_daemon_dir(home), "daemon sockets (private temp directory)"),
        (_tui_preferences(), "terminal preferences"),
        (hub / _REPO_DIR, "speech model (Paradee, Hugging Face cache)"),
        (hub / _LEGACY_REPO_DIR, "earlier speech model (Kokoro, Hugging Face cache)"),
    ]
    return [Target(path, label, _size(path)) for path, label in candidates if _exists(path)]


def remove(target: Target) -> str | None:
    """Delete one target without following symlinks; returns an error message or ``None``."""
    path = target.path
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)
        if path.name == "tui.json":  # leave ~/.config/nexus only when something else lives there
            try:
                path.parent.rmdir()
            except OSError:
                pass
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{path}: {exc.strerror or exc}"
    return None


def program_uninstall_command(method: str, uv: str | None) -> list[str] | None:
    """The command that removes the ``nexus`` program itself, or ``None`` when we must not."""
    if method == "uv-tool" and uv is not None:
        return [uv, "tool", "uninstall", PACKAGE]
    return None


def human_size(size: int) -> str:
    if size < 1000:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1000
        if value < 1000 or unit == "GB":
            break
    return f"{value:.1f} {unit}"
