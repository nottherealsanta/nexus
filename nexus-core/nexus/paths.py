"""Where Nexus keeps machine state: `$NEXUS10_HOME` or `~/.nexus`.

`~/.nexus` held old Nexus before 0.10.0. A home is Nexus 0.10's once it carries
the `MARKER` file, which `nexus migrate` writes. An unmarked `~/.nexus` (old
Nexus), or a `~/.nexus10` left by a pre-release build, is never used or
modified until the user confirms `nexus migrate`; see `migrate.py`. A machine
with neither gets a fresh marked `~/.nexus`.

An explicit `$NEXUS10_HOME` elsewhere (tests, scratch homes) is used as given.
"""
from __future__ import annotations

import os
from pathlib import Path

MARKER = ".nexus10-home"


class HomeNotMigrated(Exception):
    """`~/.nexus` still belongs to old Nexus, or a pre-release `~/.nexus10` has not been moved in."""


def default_home() -> Path:
    return Path.home() / ".nexus"


def legacy_home() -> Path:
    """Where pre-release builds of 0.10 kept state."""
    return Path.home() / ".nexus10"


def claimed(path: Path) -> bool:
    return (path / MARKER).is_file()


def home() -> Path:
    override = os.environ.get("NEXUS10_HOME")
    path = Path(override).expanduser() if override else default_home()
    default = default_home()
    if path == default or default in path.parents:
        _require_claimed(default)
    return path


def _require_claimed(default: Path) -> None:
    if claimed(default):
        return
    if not default.exists() and not legacy_home().exists():
        default.mkdir(mode=0o700, parents=True)
        (default / MARKER).write_text("{}\n")
        return
    held = "old Nexus's state" if default.exists() else f"a pre-release 0.10 home at {legacy_home()}"
    raise HomeNotMigrated(f"{default} is not set up for Nexus 0.10 ({held}); run `nexus migrate`")
