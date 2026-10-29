"""Bundled skill files, resolved fail-closed.

A skill may carry `scripts/` and `references/`. The `skill` tool can return one
of them as text. Everything else is refused: absolute paths, `..`, backslashes,
`~`, NUL bytes, other folders, symlinks that leave the skill directory,
directories, special files, oversized files and non-UTF-8 content.

Adapted from old Nexus `nexus/skills/resources.py` (the snapshot inventory and
bundled-tool candidates are dropped; files are read when asked for).
"""
from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

KINDS = ("scripts", "references")
MAX_RESOURCE_BYTES = 128 * 1024
MAX_LISTED = 200
_MAX_PATH = 512


class ResourceError(ValueError):
    """The path is not a readable bundled file."""


@dataclass(frozen=True, slots=True)
class Resource:
    path: str
    text: str
    size: int
    sha256: str


def normalize(relative: object) -> PurePosixPath:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ResourceError("resource must be a non-empty path string")
    if len(relative) > _MAX_PATH:
        raise ResourceError("resource path is too long")
    if relative.startswith(("/", "\\", "~")) or "\\" in relative or (len(relative) > 1 and relative[1] == ":"):
        raise ResourceError("resource path must be relative to the skill, with '/' separators")
    rel = PurePosixPath(relative)
    if any(part == ".." for part in rel.parts):
        raise ResourceError("resource path must not contain '..'")
    if not rel.parts or rel.parts[0] not in KINDS:
        raise ResourceError("a resource lives under " + " or ".join(f"{k}/" for k in KINDS))
    if len(rel.parts) < 2:
        raise ResourceError("name a file, not a folder")
    return rel


def read(skill_dir: str | Path, relative: object, *, max_bytes: int = MAX_RESOURCE_BYTES) -> Resource:
    rel = normalize(relative)
    root = Path(skill_dir).resolve()
    try:
        target = root.joinpath(*rel.parts).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ResourceError(f"{rel.as_posix()} does not exist") from exc
    if not target.is_relative_to(root):
        raise ResourceError(f"{rel.as_posix()} resolves outside the skill folder")
    try:
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ResourceError(f"{rel.as_posix()} is not a regular file")
        if info.st_size > max_bytes:
            raise ResourceError(f"{rel.as_posix()} is {info.st_size} bytes; the limit is {max_bytes}")
        with target.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ResourceError(f"{rel.as_posix()} cannot be read: {exc.strerror or exc}") from exc
    if len(data) > max_bytes:
        raise ResourceError(f"{rel.as_posix()} is larger than {max_bytes} bytes")
    if b"\x00" in data:
        raise ResourceError(f"{rel.as_posix()} is binary (contains a NUL byte)")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResourceError(f"{rel.as_posix()} is not valid UTF-8") from exc
    return Resource(rel.as_posix(), text, len(data), hashlib.sha256(data).hexdigest())


def listing(skill_dir: str | Path) -> list[str]:
    """Relative paths of regular files under `scripts/` and `references/` (links that escape are left out)."""
    root = Path(skill_dir).resolve()
    found: list[str] = []
    for kind in KINDS:
        base = root / kind
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            try:
                if path.is_file() and path.resolve().is_relative_to(root):
                    found.append(path.relative_to(root).as_posix())
            except OSError:
                continue
            if len(found) >= MAX_LISTED:
                return found
    return found
