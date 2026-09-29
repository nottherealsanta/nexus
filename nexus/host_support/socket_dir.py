"""Private fallback directory for daemon sockets whose default path is too long.

Pathname Unix sockets are limited (macOS ``sun_path`` is 104 bytes), so a deep
``NEXUS_HOME`` falls back to a compact, owner-only directory under ``/tmp``.
Raises ``OSError``; ``nexus.host.daemon`` wraps it in ``DaemonError``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ..config.paths import nexus_home

#: Keep sockets below the 104-byte limit, with room for the terminating NUL.
MAX_SOCKET_PATH_BYTES = 100
SHORT_TEMP_ROOT = Path("/tmp")


def fallback_daemon_dir(home: str | Path | None) -> Path:
    """Return a compact, home-specific directory in the per-user temp root."""
    resolved_home = nexus_home(home).expanduser().resolve()
    home_key = hashlib.sha256(os.fsencode(resolved_home)).hexdigest()[:16]
    return SHORT_TEMP_ROOT / f"nexus-{os.geteuid()}-{home_key}"


def ensure_private_dir(path: Path) -> None:
    """Create or validate an owner-only directory without following links."""
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OSError("daemon fallback directory is not a private directory") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("daemon fallback directory has an unexpected owner or type")
        permissions = stat.S_IMODE(info.st_mode)
        if created:
            # The fd is the verified directory itself, so this cannot chmod a
            # symlink target or a foreign parent directory.
            if permissions != 0o700:
                os.fchmod(fd, 0o700)
        elif permissions != 0o700:
            raise OSError("daemon fallback directory must have mode 0700")
    finally:
        os.close(fd)
