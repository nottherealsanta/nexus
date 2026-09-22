"""Bundled skill resource resolution and inventory (plan section 5.4).

A skill may ship two kinds of readable resource under its own root::

    <skill>/scripts/...       executable helpers the skill body can tell the
                              model to run
    <skill>/references/...    supporting documents the body can point at

Resolution is deliberately narrow and fail-closed:

* only ``scripts/`` and ``references/`` are addressable;
* the path must be relative, POSIX-separated, NUL-free, and contain no ``..``;
* the target is ``realpath``-resolved and must stay inside the skill root, so an
  absolute symlink or a symlink that escapes the root is refused;
* directories, character/block devices, FIFOs, and sockets are refused;
* reads are byte-bounded and strictly UTF-8; a NUL byte is refused.

:func:`build_inventory` and :func:`build_tool_candidates` produce deterministic,
read-only metadata for the immutable :class:`~nexus.skills.model.Skill`. Bundled
``tools/*.py`` files are surfaced as **candidates** only: this module never
imports, executes, or registers them.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import SkillOversizeError, SkillResourceError, SkillSecurityError

__all__ = [
    "DEFAULT_MAX_INVENTORY_BYTES",
    "DEFAULT_MAX_RESOURCE_BYTES",
    "RESOURCE_KINDS",
    "TOOLS_DIR",
    "BundledToolCandidate",
    "ResolvedResource",
    "SkillResource",
    "build_inventory",
    "build_tool_candidates",
    "normalize_resource_path",
    "read_resource",
    "resolve_resource",
    "resolve_resource_path",
    "resolve_resource_snapshot",
    "sha256_hex",
]

#: The only top-level directories a resource may live under.
RESOURCE_KINDS = ("scripts", "references")
#: Bundled Python tools live here; they are candidates, never imported.
TOOLS_DIR = "tools"

DEFAULT_MAX_RESOURCE_BYTES = 1_048_576
DEFAULT_MAX_INVENTORY_BYTES = 1_048_576
_MAX_PATH_CHARS = 512
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")


def sha256_hex(data: bytes) -> str:
    """The lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Immutable metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedResource:
    """A resource that has been safely resolved and read as UTF-8 text."""

    kind: str
    path: str
    text: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SkillResource:
    """Inventory entry for one bundled file (metadata plus optional snapshot).

    ``escaped`` marks a symlink whose target leaves the skill root: it is listed
    so the inventory is honest, but the resolver refuses to read it.

    ``content``/``text`` are the *bounded snapshot* taken during refresh when the
    manager asks for one. A resource that was escaped, oversized, unreadable, or
    not valid UTF-8 has ``content``/``text`` of ``None`` and an explanatory
    ``snapshot_error``; invocation refuses it rather than silently reading newer
    bytes off disk.
    """

    kind: str
    path: str
    size: int
    sha256: str | None = None
    symlink: bool = False
    oversized: bool = False
    escaped: bool = False
    content: bytes | None = None
    text: str | None = None
    snapshot_error: str | None = None

    @property
    def snapshotted(self) -> bool:
        return self.content is not None or self.text is not None


@dataclass(frozen=True)
class BundledToolCandidate:
    """Metadata for a ``tools/*.py`` file. Never imported or registered."""

    path: str
    module: str
    size: int
    sha256: str | None = None
    oversized: bool = False
    symlink: bool = False
    escaped: bool = False


# ---------------------------------------------------------------------------
# Relative-path validation
# ---------------------------------------------------------------------------


def normalize_resource_path(relative: object) -> PurePosixPath:
    """Validate and normalize a skill-relative resource path.

    Rejects non-strings, empty strings, NUL bytes, absolute paths, Windows drive
    prefixes, backslashes, ``~``, any ``..`` segment, and paths outside
    ``scripts/``/``references/``.
    """
    if not isinstance(relative, str):
        raise SkillResourceError("resource path must be a string")
    if not relative or "\x00" in relative:
        raise SkillSecurityError(
            "resource path must be a non-empty string without a NUL byte"
        )
    if len(relative) > _MAX_PATH_CHARS:
        raise SkillResourceError("resource path is too long")
    if relative.startswith(("/", "\\", "~")):
        raise SkillSecurityError("resource path must be relative to the skill root")
    if _WINDOWS_DRIVE.match(relative) or "\\" in relative:
        raise SkillSecurityError("resource path must use POSIX separators")
    rel = PurePosixPath(relative)
    if rel.is_absolute():
        raise SkillSecurityError("resource path must be relative")
    parts = rel.parts
    if not parts:
        raise SkillResourceError("resource path is empty")
    if any(part == ".." for part in parts):
        raise SkillSecurityError("resource path must not contain '..'")
    if parts[0] not in RESOURCE_KINDS:
        raise SkillResourceError(
            "resource must live under " + " or ".join(f"{k}/" for k in RESOURCE_KINDS)
        )
    if len(parts) < 2:
        raise SkillResourceError("resource path must name a file, not a directory")
    return rel


def _checked_target(
    skill_dir: str | Path, relative: object
) -> tuple[PurePosixPath, Path, Path, int]:
    """Resolve ``relative`` to a real regular file inside ``skill_dir``.

    Returns ``(rel, root, resolved, size)``. Raises on escapes, non-regular
    files, and unresolvable targets.
    """
    rel = normalize_resource_path(relative)
    root = Path(skill_dir).resolve()
    candidate = root.joinpath(*rel.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillResourceError(
            f"resource {rel.as_posix()!r} cannot be resolved"
        ) from exc
    if not resolved.is_relative_to(root):
        raise SkillSecurityError(
            f"resource {rel.as_posix()!r} escapes the skill root"
        )
    try:
        info = resolved.stat()
    except OSError as exc:
        raise SkillResourceError(
            f"resource {rel.as_posix()!r} cannot be inspected"
        ) from exc
    if stat_module.S_ISDIR(info.st_mode):
        raise SkillResourceError(f"resource {rel.as_posix()!r} is a directory")
    if not stat_module.S_ISREG(info.st_mode):
        raise SkillResourceError(
            f"resource {rel.as_posix()!r} is not a regular file"
        )
    return rel, root, resolved, info.st_size


def resolve_resource_path(skill_dir: str | Path, relative: object) -> Path:
    """Resolve ``relative`` to a safe absolute path without reading it."""
    return _checked_target(skill_dir, relative)[2]


def resolve_resource(
    skill_dir: str | Path,
    relative: object,
    *,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> ResolvedResource:
    """Resolve and read a UTF-8 resource, bounded by ``max_bytes``."""
    rel, _root, resolved, size = _checked_target(skill_dir, relative)
    if size > max_bytes:
        raise SkillOversizeError(
            f"resource {rel.as_posix()!r} is {size} bytes (limit {max_bytes})"
        )
    try:
        with resolved.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise SkillResourceError(
            f"resource {rel.as_posix()!r} cannot be read"
        ) from exc
    if len(data) > max_bytes:
        raise SkillOversizeError(
            f"resource {rel.as_posix()!r} exceeds {max_bytes} bytes"
        )
    if b"\x00" in data:
        raise SkillResourceError(f"resource {rel.as_posix()!r} contains a NUL byte")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillResourceError(
            f"resource {rel.as_posix()!r} is not valid UTF-8"
        ) from exc
    return ResolvedResource(
        kind=rel.parts[0],
        path=rel.as_posix(),
        text=text,
        size=len(data),
        sha256=sha256_hex(data),
    )


def read_resource(
    skill_dir: str | Path,
    relative: object,
    *,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> str:
    """The UTF-8 text of one safely resolved resource."""
    return resolve_resource(skill_dir, relative, max_bytes=max_bytes).text


# ---------------------------------------------------------------------------
# Deterministic inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FileInfo:
    size: int
    sha256: str | None
    symlink: bool
    oversized: bool
    escaped: bool
    content: bytes | None = None
    text: str | None = None
    error: str | None = None


def _read_opened_regular(path: Path, max_bytes: int) -> tuple[bytes, int]:
    """Open ``path`` once, confirm it is regular via ``fstat``, read bounded.

    The bytes returned are the same ones that were hashed and snapshotted, and
    the size reported is their length, so size and hash can never disagree.
    """
    with path.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SkillOversizeError(f"{path} exceeds {max_bytes} bytes")
    return data, len(data)


def _iter_files(
    base: Path,
    skill_root: Path,
    max_bytes: int,
    *,
    with_content: bool = False,
):
    """Yield ``(rel_posix, _FileInfo)`` for regular files/symlinks, sorted.

    Symlinks are resolved and checked to stay inside ``skill_root``; an escaping
    symlink is reported with ``escaped=True`` and never read. When
    ``with_content`` is set, the bounded bytes and decoded text are retained on
    the info so the caller can snapshot them.
    """
    if not base.is_dir():
        return
    root_resolved = skill_root.resolve()
    entries: list[tuple[str, Path]] = []
    for path in base.rglob("*"):
        # A recursive walk can surface a path that is not lexically under the
        # skill root when a symlinked component is followed. Such an entry is
        # escaped; filter it out *before* ``relative_to`` (which would raise)
        # rather than letting one escaped link abort the whole inventory.
        try:
            rel = path.relative_to(skill_root).as_posix()
        except ValueError:
            continue
        entries.append((rel, path))
    entries.sort(key=lambda item: item[0])
    for rel, path in entries:
        try:
            lst = path.lstat()
        except OSError:
            continue
        if stat_module.S_ISDIR(lst.st_mode):
            continue
        symlink = stat_module.S_ISLNK(lst.st_mode)
        if not symlink and not stat_module.S_ISREG(lst.st_mode):
            continue  # devices, FIFOs, sockets are never resources
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(root_resolved):
            yield rel, _FileInfo(0, None, symlink, False, True)
            continue
        try:
            info = resolved.stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(info.st_mode):
            continue
        size = info.st_size
        if size > max_bytes:
            yield rel, _FileInfo(size, None, symlink, True, False)
            continue
        try:
            data, opened_size = _read_opened_regular(resolved, max_bytes)
        except SkillOversizeError:
            yield rel, _FileInfo(size, None, symlink, True, False)
            continue
        except OSError:
            continue
        digest = sha256_hex(data)
        content: bytes | None = None
        text: str | None = None
        error: str | None = None
        if with_content:
            content = data
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                error = "not valid UTF-8"
            else:
                if "\x00" in text:
                    text = None
                    error = "contains a NUL byte"
        yield rel, _FileInfo(
            opened_size, digest, symlink, False, False, content, text, error
        )


def build_inventory(
    skill_dir: str | Path,
    *,
    kinds: tuple[str, ...] = RESOURCE_KINDS,
    max_bytes: int = DEFAULT_MAX_INVENTORY_BYTES,
    with_content: bool = False,
) -> tuple[SkillResource, ...]:
    """Deterministic metadata for every bundled ``scripts``/``references`` file.

    With ``with_content`` set, each entry also carries the bounded bytes and
    decoded text read during this pass, so the caller can snapshot it for
    generation-stable invocation.
    """
    root = Path(skill_dir)
    label = {"scripts": "script", "references": "reference"}
    out: list[SkillResource] = []
    for kind in kinds:
        base = root / kind
        for rel, info in _iter_files(base, root, max_bytes, with_content=with_content):
            out.append(
                SkillResource(
                    kind=label.get(kind, kind),
                    path=rel,
                    size=info.size,
                    sha256=info.sha256,
                    symlink=info.symlink,
                    oversized=info.oversized,
                    escaped=info.escaped,
                    content=info.content,
                    text=info.text,
                    snapshot_error=info.error,
                )
            )
    return tuple(out)


def build_tool_candidates(
    skill_dir: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_INVENTORY_BYTES,
) -> tuple[BundledToolCandidate, ...]:
    """Deterministic metadata for ``tools/*.py`` candidates (never imported).

    ``__init__.py`` and ``_``-prefixed helpers are excluded: they are support
    modules, not tool candidates.
    """
    root = Path(skill_dir)
    base = root / TOOLS_DIR
    out: list[BundledToolCandidate] = []
    for rel, info in _iter_files(base, root, max_bytes):
        filename = PurePosixPath(rel).name
        if not filename.endswith(".py"):
            continue
        module = filename[:-3]
        if not module or module.startswith("_"):
            continue
        out.append(
            BundledToolCandidate(
                path=rel,
                module=module,
                size=info.size,
                sha256=info.sha256,
                oversized=info.oversized,
                symlink=info.symlink,
                escaped=info.escaped,
            )
        )
    return tuple(out)


def resolve_resource_snapshot(
    resources: Iterable[SkillResource],
    relative: object,
    *,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> ResolvedResource:
    """Resolve ``relative`` from a refresh-time inventory snapshot.

    This is the invocation path for a generation-pinned skill: it never touches
    disk, so a later edit cannot change what an already-discovered skill serves.
    Missing, escaped, oversized, or unreadable entries are refused.
    """
    rel = normalize_resource_path(relative)
    target = rel.as_posix()
    match = next((entry for entry in resources if entry.path == target), None)
    if match is None:
        raise SkillResourceError(
            f"resource {target!r} is not part of the skill snapshot"
        )
    if match.escaped:
        raise SkillSecurityError(f"resource {target!r} escapes the skill root")
    if match.oversized:
        raise SkillOversizeError(
            f"resource {target!r} was oversized at discovery and was not snapshotted"
        )
    if match.text is None:
        raise SkillResourceError(
            match.snapshot_error
            or f"resource {target!r} was not snapshotted"
        )
    if match.size > max_bytes:
        raise SkillOversizeError(
            f"resource {target!r} is {match.size} bytes (limit {max_bytes})"
        )
    return ResolvedResource(
        kind=rel.parts[0],
        path=match.path,
        text=match.text,
        size=match.size,
        sha256=match.sha256 or sha256_hex(match.text.encode("utf-8")),
    )
