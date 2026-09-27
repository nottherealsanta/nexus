"""Immutable, read-only review snapshots for finalized agent worktrees.

The helper intentionally uses Git plumbing and filesystem reads only. It does
not create a temporary worktree, update refs/indexes, or change a worktree
lifecycle. A review compares the recorded base tree with the child's final
filesystem, so commits and index-only changes are included alongside ordinary
working-tree edits.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .worktrees import WorktreeError, WorktreeRecord

__all__ = [
    "ReviewArtifact",
    "ReviewError",
    "ReviewLimits",
    "build_review",
    "load_review",
]

_PAGE_BYTES = 128 * 1024
_FILE_BYTES = 16 * 1024 * 1024
_AGGREGATE_BYTES = 128 * 1024 * 1024
_FILE_COUNT = 500


class ReviewError(WorktreeError):
    """A review could not be captured, validated, or loaded safely."""


@dataclass(frozen=True)
class ReviewLimits:
    """Resource bounds for the changed content in one review."""

    max_files: int = _FILE_COUNT
    max_file_bytes: int = _FILE_BYTES
    max_total_bytes: int = _AGGREGATE_BYTES
    page_bytes: int = _PAGE_BYTES

    def __post_init__(self) -> None:
        if self.max_files < 1 or self.max_file_bytes < 1 or self.max_total_bytes < 1:
            raise ValueError("review file and byte limits must be positive")
        if self.max_files > _FILE_COUNT:
            raise ValueError(f"max_files cannot exceed {_FILE_COUNT}")
        if self.max_file_bytes > _FILE_BYTES:
            raise ValueError(f"max_file_bytes cannot exceed {_FILE_BYTES}")
        if self.max_total_bytes > _AGGREGATE_BYTES:
            raise ValueError(f"max_total_bytes cannot exceed {_AGGREGATE_BYTES}")
        if not 1 <= self.page_bytes <= _PAGE_BYTES:
            raise ValueError(f"page_bytes must be between 1 and {_PAGE_BYTES}")


@dataclass(frozen=True)
class ReviewArtifact:
    """A frozen review manifest and deterministic bounded diff pages."""

    review_id: str
    digest: str
    manifest: dict[str, Any]
    diff_pages: tuple[bytes, ...]
    artifact_path: Path


@dataclass(frozen=True)
class _FileSnapshot:
    mode: int
    size: int
    git_oid: str
    content: bytes | None
    stat_key: tuple[int, int, int, int, int]


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    environment = _git_environment()
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=cwd,
            input=input_bytes,
            capture_output=True,
            env=environment,
            check=False,
        )
    except OSError as exc:
        raise ReviewError(f"could not run Git plumbing: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise ReviewError(f"Git plumbing command failed ({result.returncode})" + (f": {message}" if message else ""))
    return result.stdout


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _stable_git_state(record: WorktreeRecord) -> tuple[str, bytes]:
    if record.lifecycle != "finalized":
        raise ReviewError("worktree review requires a finalized child")
    path = record.path
    if path.is_symlink() or not path.is_dir():
        raise ReviewError("child worktree is missing or is a symlink")
    child = path.resolve(strict=True)
    parent = record.parent_workspace.resolve(strict=True)
    if child != path or Path(_git(child, "rev-parse", "--show-toplevel").decode().strip()).resolve() != child:
        raise ReviewError("recorded child path is not the worktree root")
    common_child = Path(_git(child, "rev-parse", "--git-common-dir").decode().strip())
    if not common_child.is_absolute():
        common_child = (child / common_child).resolve(strict=True)
    else:
        common_child = common_child.resolve(strict=True)
    common_parent = Path(_git(parent, "rev-parse", "--git-common-dir").decode().strip())
    if not common_parent.is_absolute():
        common_parent = (parent / common_parent).resolve(strict=True)
    else:
        common_parent = common_parent.resolve(strict=True)
    if common_child != common_parent:
        raise ReviewError("child worktree belongs to a different repository")

    head = _git(child, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    branch = _git(child, "symbolic-ref", "--quiet", "HEAD").decode().strip()
    if branch != f"refs/heads/{record.branch.removeprefix('refs/heads/')}":
        raise ReviewError("child worktree is not attached to its recorded branch")
    if len(record.base_commit) not in (40, 64) or any(
        character not in "0123456789abcdef" for character in record.base_commit
    ):
        raise ReviewError("recorded base commit is invalid")
    if _git(
        parent,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{record.base_commit}^{{commit}}",
    ).decode().strip() != record.base_commit:
        raise ReviewError("recorded base commit is unavailable")

    # config --bool exits nonzero when unset.
    probe = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "config", "--bool", "core.sparseCheckout"],
        cwd=child,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_git_environment(),
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip() == b"true":
        raise ReviewError("sparse checkouts are unsupported for worktree review")
    staged = _git(child, "ls-files", "--stage", "-z")
    if any(row.startswith(b"160000 ") for row in staged.split(b"\0") if row):
        raise ReviewError("submodules are unsupported for worktree review")
    tags = _git(child, "ls-files", "-t", "-z")
    if any(row.startswith(b"S ") for row in tags.split(b"\0") if row):
        raise ReviewError("sparse checkout entries are unsupported for worktree review")
    status = _git(child, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching")
    return head, status


def _tree_inventory(record: WorktreeRecord) -> dict[bytes, tuple[int, str, int]]:
    raw = _git(record.parent_workspace, "ls-tree", "-r", "-z", "--full-tree", record.base_commit)
    inventory: dict[bytes, tuple[int, str, int]] = {}
    for row in raw.split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode_raw, kind, oid_raw = metadata.split(b" ", 2)
        mode = int(mode_raw, 8)
        oid = oid_raw.decode("ascii")
        if mode == 0o160000 or mode == 0o120000 or kind != b"blob":
            raise ReviewError("base tree contains an unsupported symlink or submodule")
        if mode not in (0o100644, 0o100755):
            raise ReviewError("base tree contains an unsupported file mode")
        size = int(_git(record.parent_workspace, "cat-file", "-s", oid).decode().strip())
        inventory[name] = (mode, oid, size)
    return inventory


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _ignore_paths(root: Path, names: list[bytes]) -> set[bytes]:
    if not names:
        return set()
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "check-ignore", "-z", "--stdin", "--no-index"],
        cwd=root,
        input=b"\0".join(names) + b"\0",
        capture_output=True,
        env=_git_environment(),
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ReviewError("could not inspect ignored child paths")
    response = result.stdout
    return {part for part in response.split(b"\0") if part}


def _scan(
    record: WorktreeRecord,
    limits: ReviewLimits,
    base: dict[bytes, tuple[int, str, int]],
) -> tuple[dict[bytes, _FileSnapshot], list[bytes]]:
    root = record.path
    files: dict[bytes, _FileSnapshot] = {}
    ignored: set[bytes] = set()
    stack: list[tuple[bytes, bytes]] = [(b"", os.fsencode(root))]
    object_format = _git(root, "rev-parse", "--show-object-format").decode().strip()
    hash_name = "sha256" if object_format == "sha256" else "sha1"
    changed_count = 0
    aggregate_size = sum(size for path, (_, _, size) in base.items() if not (root / os.fsdecode(path)).exists())
    if aggregate_size > limits.max_total_bytes:
        raise ReviewError("review aggregate content limit exceeded")

    while stack:
        relative_dir, absolute_dir = stack.pop()
        try:
            with os.scandir(absolute_dir) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ReviewError("cannot safely enumerate child filesystem") from exc
        names = [entry.name if isinstance(entry.name, bytes) else os.fsencode(entry.name) for entry in entries]
        if relative_dir:
            markers = set(names)
            if b".git" in markers:
                raise ReviewError("nested repositories are unsupported")
        ignored_here = _ignore_paths(
            root,
            [relative_dir + name if relative_dir else name for name in names if name != b".git"],
        )
        for entry, name in zip(entries, names):
            relative = relative_dir + name
            if not relative_dir and name == b".git":
                continue
            if relative_dir and name == b".git":
                raise ReviewError("nested repositories are unsupported")
            relative_posix = relative.replace(os.sep.encode(), b"/")
            full = os.path.join(absolute_dir, name)
            try:
                info = os.stat(full, follow_symlinks=False)
            except OSError as exc:
                raise ReviewError("child filesystem changed during review") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ReviewError("child filesystem contains an unsupported symlink")
            if stat.S_ISDIR(info.st_mode):
                stack.append((relative_posix + b"/", full))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ReviewError("child filesystem contains an unsupported special file")
            is_ignored = relative_posix in ignored_here and relative_posix not in base
            if is_ignored:
                ignored.add(relative_posix)
            mode = 0o100755 if info.st_mode & 0o111 else 0o100644
            git_hash = hashlib.new(hash_name)
            git_hash.update(f"blob {info.st_size}\0".encode("ascii"))
            try:
                with open(full, "rb", buffering=0) as source:
                    before = os.fstat(source.fileno())
                    if _stat_key(before) != _stat_key(info):
                        raise ReviewError("child file changed during review")
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        git_hash.update(block)
                    after = os.fstat(source.fileno())
            except OSError as exc:
                raise ReviewError("cannot safely read child file") from exc
            if _stat_key(before) != _stat_key(after) or _stat_key(after) != _stat_key(os.stat(full, follow_symlinks=False)):
                raise ReviewError("child file changed during review")
            oid = git_hash.hexdigest()
            expected = base.get(relative_posix)
            changed = expected is None or expected[0] != mode or expected[1] != oid
            if changed and not is_ignored:
                changed_count += 1
                aggregate_size += info.st_size + (expected[2] if expected is not None else 0)
                if changed_count > limits.max_files:
                    raise ReviewError("review changed-file limit exceeded")
                if aggregate_size > limits.max_total_bytes:
                    raise ReviewError("review aggregate content limit exceeded")
            content: bytes | None = None
            if changed and not is_ignored and info.st_size <= limits.max_file_bytes:
                try:
                    with open(full, "rb", buffering=0) as source:
                        copy_before = os.fstat(source.fileno())
                        if _stat_key(copy_before) != _stat_key(after):
                            raise ReviewError("child file changed during review")
                        content = source.read(limits.max_file_bytes + 1)
                        copy_after = os.fstat(source.fileno())
                except OSError as exc:
                    raise ReviewError("cannot safely read child file") from exc
                if _stat_key(copy_after) != _stat_key(after) or len(content) != info.st_size:
                    raise ReviewError("child file changed during review")
                if _git_blob_oid(content, object_format) != oid:
                    raise ReviewError("child file content changed during review")
            files[relative_posix] = _FileSnapshot(
                mode=mode,
                size=info.st_size,
                git_oid=oid,
                content=content,
                stat_key=_stat_key(after),
            )
    return files, sorted(ignored)


def _source_path(path: bytes) -> str:
    return path.decode("utf-8", "surrogateescape")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git_blob_oid(content: bytes, object_format: str) -> str:
    digest = hashlib.new("sha256" if object_format == "sha256" else "sha1")
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _capture(record: WorktreeRecord, limits: ReviewLimits) -> tuple[dict[str, Any], dict[str, bytes], tuple[bytes, ...]]:
    head_before, status_before = _stable_git_state(record)
    base = _tree_inventory(record)
    final, ignored = _scan(record, limits, base)
    object_format = _git(record.path, "rev-parse", "--show-object-format").decode().strip()
    changed_paths = sorted(
        path for path in set(base) | set(final)
        if path not in ignored
        and (
            path not in base
            or path not in final
            or base[path][0] != final[path].mode
            or base[path][1] != final[path].git_oid
        )
    )
    if len(changed_paths) > limits.max_files:
        raise ReviewError("review changed-file limit exceeded")

    blobs: dict[str, bytes] = {}
    manifest_entries: list[dict[str, Any]] = []
    diff_rows: list[bytes] = []
    aggregate = 0
    for path in changed_paths:
        base_entry = base.get(path)
        final_entry = final.get(path)
        old_content: bytes | None = None
        new_content: bytes | None = final_entry.content if final_entry else None
        if base_entry:
            old_mode, old_oid, old_size = base_entry
            if old_size > limits.max_file_bytes:
                raise ReviewError("review changed file exceeds per-file content limit")
            old_content = _git(record.parent_workspace, "cat-file", "blob", old_oid)
            if len(old_content) != old_size or _git_blob_oid(old_content, object_format) != old_oid:
                raise ReviewError("base blob failed integrity verification")
        if final_entry and new_content is None:
            raise ReviewError("review changed file exceeds per-file content limit")
        for content in (old_content, new_content):
            if content is not None:
                if len(content) > limits.max_file_bytes:
                    raise ReviewError("review changed file exceeds per-file content limit")
                aggregate += len(content)
        if aggregate > limits.max_total_bytes:
            raise ReviewError("review aggregate content limit exceeded")

        old_sha = hashlib.sha256(old_content).hexdigest() if old_content is not None else None
        new_sha = hashlib.sha256(new_content).hexdigest() if new_content is not None else None
        if old_content is not None:
            blobs[old_sha] = old_content
        if new_content is not None:
            blobs[new_sha] = new_content
        old_mode = base_entry[0] if base_entry else None
        new_mode = final_entry.mode if final_entry else None
        binary = any(content is not None and (b"\0" in content or _not_utf8(content)) for content in (old_content, new_content))
        patch = ""
        if not binary:
            old_lines = (old_content or b"").decode("utf-8").splitlines(keepends=True)
            new_lines = (new_content or b"").decode("utf-8").splitlines(keepends=True)
            patch = "".join(difflib.unified_diff(old_lines, new_lines, fromfile="a", tofile="b"))
        row = {
            "path": _source_path(path),
            "change": "added" if base_entry is None else "deleted" if final_entry is None else "modified",
            "old_mode": format(old_mode, "06o") if old_mode is not None else None,
            "new_mode": format(new_mode, "06o") if new_mode is not None else None,
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "binary": binary,
            "patch": patch,
        }
        manifest_entries.append({key: value for key, value in row.items() if key != "patch"})
        diff_rows.append(_canonical(row) + b"\n")

    # Recheck all sampled files and Git's observable checkout state after the
    # content pass, before any artifact is published.
    final_after, ignored_after = _scan(record, limits, base)
    if ignored_after != ignored or {
        path: (item.mode, item.size, item.git_oid) for path, item in final.items()
    } != {
        path: (item.mode, item.size, item.git_oid) for path, item in final_after.items()
    }:
        raise ReviewError("child filesystem content changed during review")
    head_after, status_after = _stable_git_state(record)
    if head_before != head_after or status_before != status_after:
        raise ReviewError("child Git state changed during review")

    diff_stream = b"".join(diff_rows)
    pages = tuple(diff_stream[offset : offset + limits.page_bytes] for offset in range(0, len(diff_stream), limits.page_bytes))
    manifest = {
        "format": 1,
        "child_id": record.child_id,
        "checkout_identity": hashlib.sha256(os.fsencode(record.path.resolve())).hexdigest(),
        "base_commit": record.base_commit,
        "head": head_before,
        "git_status_sha256": hashlib.sha256(status_before).hexdigest(),
        "ignored_paths": [_source_path(path) for path in ignored],
        "ignored_files": [
            {
                "path": _source_path(path),
                "mode": format(final[path].mode, "06o"),
                "size": final[path].size,
                "git_oid": final[path].git_oid,
            }
            for path in ignored
            if path in final
        ],
        "limits": {
            "max_files": limits.max_files,
            "max_file_bytes": limits.max_file_bytes,
            "max_total_bytes": limits.max_total_bytes,
            "page_bytes": limits.page_bytes,
        },
        "entries": manifest_entries,
        "blobs": sorted(blobs),
        "diff_pages": [hashlib.sha256(page).hexdigest() for page in pages],
    }
    return manifest, blobs, pages


def _not_utf8(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _private_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise ReviewError("review artifact directories cannot be symlinks")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.stat()
    except OSError as exc:
        raise ReviewError("review artifact directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ReviewError("review artifact directory must be private and owned by this user")


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.exists() and component.is_symlink():
            raise ReviewError("review artifact paths cannot contain symlinks")


def _outside_checkouts(root: Path, record: WorktreeRecord) -> Path:
    resolved = root.resolve(strict=False)
    for checkout in (record.parent_workspace.resolve(strict=True), record.path.resolve(strict=True)):
        if resolved == checkout or resolved in checkout.parents or checkout in resolved.parents:
            raise ReviewError("review artifact root must be separate from the parent and child checkouts")
    return resolved


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def build_review(
    record: WorktreeRecord,
    artifact_root: str | Path,
    *,
    limits: ReviewLimits | None = None,
) -> ReviewArtifact:
    """Freeze a finalized child's base-to-final review without changing Git."""
    limits = ReviewLimits() if limits is None else limits
    root = Path(artifact_root).expanduser()
    _reject_symlink_components(root)
    root = _outside_checkouts(root, record)
    _private_directory(root, create=True)
    manifest, blobs, pages = _capture(record, limits)
    review_id = secrets.token_hex(16)
    review_dir = root / review_id
    review_dir.mkdir(mode=0o700)
    (review_dir / "blobs").mkdir(mode=0o700)
    (review_dir / "pages").mkdir(mode=0o700)
    try:
        for digest, content in sorted(blobs.items()):
            _write_private(review_dir / "blobs" / digest, content)
        for index, page in enumerate(pages):
            _write_private(review_dir / "pages" / f"{index:06d}.jsonl", page)
        manifest_bytes = _canonical(manifest)
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        envelope = _canonical({"digest": digest, "manifest": manifest})
        _write_private(review_dir / "manifest.json", envelope)
    except BaseException:
        # Only remove the random directory allocated by this invocation.
        import shutil

        shutil.rmtree(review_dir, ignore_errors=True)
        raise
    return ReviewArtifact(review_id, digest, manifest, pages, review_dir)


def _read_private(path: Path) -> bytes:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ReviewError("review artifact file is not private and regular")
        return path.read_bytes()
    except OSError as exc:
        raise ReviewError("review artifact is missing or unreadable") from exc


def load_review(
    record: WorktreeRecord,
    artifact_root: str | Path,
    review_id: str,
) -> ReviewArtifact:
    """Validate a frozen artifact and reject it if the child has since changed."""
    if not review_id or any(character not in "0123456789abcdef" for character in review_id):
        raise ReviewError("invalid review id")
    root = Path(artifact_root).expanduser()
    _reject_symlink_components(root)
    root = _outside_checkouts(root, record)
    _private_directory(root)
    review_dir = root / review_id
    _private_directory(review_dir)
    _private_directory(review_dir / "blobs")
    _private_directory(review_dir / "pages")
    try:
        envelope = json.loads(_read_private(review_dir / "manifest.json"))
        manifest = envelope["manifest"]
        digest = envelope["digest"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ReviewError("review manifest has an invalid format") from exc
    manifest_bytes = _canonical(manifest)
    if not isinstance(digest, str) or not secrets.compare_digest(hashlib.sha256(manifest_bytes).hexdigest(), digest):
        raise ReviewError("review manifest digest does not match")
    if manifest.get("child_id") != record.child_id or manifest.get("base_commit") != record.base_commit:
        raise ReviewError("review artifact does not belong to this child record")
    limits = ReviewLimits(**manifest["limits"])
    actual_blobs: dict[str, bytes] = {}
    for blob_digest in manifest["blobs"]:
        content = _read_private(review_dir / "blobs" / blob_digest)
        if hashlib.sha256(content).hexdigest() != blob_digest:
            raise ReviewError("frozen review content failed integrity verification")
        actual_blobs[blob_digest] = content
    pages = tuple(
        _read_private(review_dir / "pages" / f"{index:06d}.jsonl")
        for index in range(len(manifest["diff_pages"]))
    )
    if any(len(page) > limits.page_bytes for page in pages):
        raise ReviewError("review diff page exceeds its declared limit")
    if [hashlib.sha256(page).hexdigest() for page in pages] != manifest["diff_pages"]:
        raise ReviewError("review diff page failed integrity verification")

    expected_files = {
        "manifest.json",
        *(f"blobs/{blob_digest}" for blob_digest in manifest["blobs"]),
        *(f"pages/{index:06d}.jsonl" for index in range(len(pages))),
    }
    found_files: set[str] = set()
    expected_directories = {".", "blobs", "pages"}
    found_directories: set[str] = set()
    for directory, subdirs, filenames in os.walk(review_dir, followlinks=False):
        base_dir = Path(directory)
        found_directories.add("." if base_dir == review_dir else base_dir.relative_to(review_dir).as_posix())
        for name in subdirs:
            candidate = base_dir / name
            if candidate.is_symlink():
                raise ReviewError("review artifact contents do not match the manifest")
        for name in filenames:
            candidate = base_dir / name
            if candidate.is_symlink():
                raise ReviewError("review artifact contents do not match the manifest")
            found_files.add(candidate.relative_to(review_dir).as_posix())
    if found_files != expected_files or found_directories != expected_directories:
        raise ReviewError("review artifact contents do not match the manifest")
    for entry in manifest["entries"]:
        for key in ("old_sha256", "new_sha256"):
            blob_digest = entry[key]
            if blob_digest is not None and blob_digest not in actual_blobs:
                raise ReviewError("review manifest references missing frozen content")

    current, _, _ = _capture(record, limits)
    if current != manifest:
        raise ReviewError("review is stale because the child worktree changed")
    return ReviewArtifact(review_id, digest, manifest, pages, review_dir)
