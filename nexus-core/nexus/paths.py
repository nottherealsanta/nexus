"""Where Nexus 0.10 keeps machine state: `$NEXUS10_HOME` or `~/.nexus10`.

Kept apart from old Nexus (`~/.nexus`) so the two never share credentials,
databases or daemons while both are installed.
"""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("NEXUS10_HOME")
    return Path(override).expanduser() if override else Path.home() / ".nexus10"
