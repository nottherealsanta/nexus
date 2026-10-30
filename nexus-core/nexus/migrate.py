"""`nexus migrate`: make `~/.nexus` Nexus 0.10's home (O3).

Old Nexus's `~/.nexus` is renamed to `~/.nexus.old-<time>` and never deleted.
A pre-release `~/.nexus10` is renamed to `~/.nexus`. Both renames stay on one
filesystem, so no data is copied. The marker is written into the pre-release
home before the rename: a crash then leaves either the untouched pair (run
again) or a finished migration, never a 0.10 home that looks like old Nexus.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .paths import MARKER, claimed, default_home, legacy_home


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    target: Path
    aside: Path | None  # old Nexus's home, moved to `aside_to`
    aside_to: Path | None
    adopt: Path | None  # a pre-release 0.10 home, moved to `target`

    @property
    def needed(self) -> bool:
        return self.aside is not None or self.adopt is not None


def plan(now: float | None = None) -> Plan:
    target, legacy = default_home(), legacy_home()
    if claimed(target):
        return Plan(target, None, None, None)
    aside = target if target.exists() else None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    aside_to = None
    if aside is not None:
        aside_to = target.with_name(f"{target.name}.old-{stamp}")
        n = 1
        while aside_to.exists():
            n += 1
            aside_to = target.with_name(f"{target.name}.old-{stamp}-{n}")
    return Plan(target, aside, aside_to, legacy if legacy.exists() else None)


def describe(p: Plan) -> str:
    lines = []
    if p.aside is not None:
        lines.append(f"Move old Nexus's {p.aside} aside to {p.aside_to} (nothing is deleted).")
    if p.adopt is not None:
        lines.append(f"Move {p.adopt} to {p.target}.")
    else:
        lines.append(f"Create a fresh {p.target}.")
    return "\n".join(lines)


def _running(path: Path) -> int | None:
    """The pid in `path/daemon.json` if that process is alive."""
    try:
        pid = int(json.loads((path / "daemon.json").read_text())["pid"])
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid


def migrate(confirm: Callable[[Plan], bool]) -> Plan:
    """Apply the plan once `confirm(plan)` returns true. Returns the plan (not `needed` when already migrated)."""
    p = plan()
    if not p.needed:
        return p
    if p.adopt is not None and p.adopt.is_symlink():
        raise MigrationError(f"{p.adopt} is a symlink; move its target by hand")
    for path in (p.aside, p.adopt):
        if path is not None and (pid := _running(path)) is not None:
            raise MigrationError(f"a daemon (pid {pid}) is running from {path}; stop it first")
    if not confirm(p):
        raise MigrationError("cancelled; nothing was changed")
    source = p.adopt or p.target
    if p.aside is not None and p.aside_to is not None:
        os.rename(p.aside, p.aside_to)
    try:
        if p.adopt is not None:
            _mark(p.adopt, p)
            os.rename(p.adopt, p.target)
        else:
            p.target.mkdir(mode=0o700, parents=True, exist_ok=True)
            _mark(p.target, p)
    except OSError as exc:
        if p.aside is not None and p.aside_to is not None and not p.aside.exists():
            os.rename(p.aside_to, p.aside)
        raise MigrationError(f"could not move {source}: {exc}") from exc
    return p


def _mark(directory: Path, p: Plan) -> None:
    record = {
        "migrated": time.time(),
        "from": str(p.adopt) if p.adopt else None,
        "old_nexus": str(p.aside_to) if p.aside_to else None,
    }
    (directory / MARKER).write_text(json.dumps(record) + "\n")
