"""Apply an acknowledged, frozen worktree review to a clean parent checkout.

Each filesystem operation is individually durable, not collectively atomic. A
private journal is published before the first mutation so interrupted
transactions can be rolled back with :func:`recover_transactions`.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .worktree_review import ReviewArtifact, load_review
from .worktrees import WorktreeError, WorktreeRecord

__all__ = [
    "IntegrationResult",
    "WorktreeIntegrationError",
    "integrate_review",
    "recover_transactions",
]


class WorktreeIntegrationError(WorktreeError):
    """The parent, review, or transaction journal is unsafe to integrate."""


@dataclass(frozen=True)
class IntegrationResult:
    """Outcome of applying or recovering one filesystem transaction."""

    status: str
    transaction_id: str | None = None
    changed_paths: tuple[str, ...] = ()
    error: str | None = None
    recovery_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Entry:
    path: str
    old_exists: bool
    old_mode: int | None
    old_sha256: str | None
    old_blob: str | None
    new_mode: int | None
    new_sha256: str | None
    new_blob: str | None


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        env=_git_environment(),
    )
    if result.returncode:
        detail = result.stderr.strip()
        raise WorktreeIntegrationError(f"git {' '.join(args)} failed" + (f": {detail}" if detail else ""))
    return result.stdout.strip()


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _git_path(root: Path, name: str) -> Path:
    path = Path(_git(root, "rev-parse", "--git-path", name))
    return path if path.is_absolute() else root / path


def _parent_preconditions(record: WorktreeRecord) -> tuple[Path, str, bytes]:
    parent = record.parent_workspace.expanduser().resolve(strict=True)
    if not parent.is_dir() or Path(_git(parent, "rev-parse", "--show-toplevel")).resolve() != parent:
        raise WorktreeIntegrationError("recorded parent must be the top-level Git checkout")
    head = _git(parent, "rev-parse", "--verify", "HEAD^{commit}")
    if head != record.base_commit:
        raise WorktreeIntegrationError("parent HEAD no longer matches the recorded base commit")
    status = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=parent,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        env=_git_environment(),
    )
    if status.returncode or status.stdout:
        raise WorktreeIntegrationError("parent Git checkout must be clean, including untracked files")
    for marker in (
        "MERGE_HEAD", "MERGE_MSG", "CHERRY_PICK_HEAD", "REVERT_HEAD",
        "BISECT_LOG", "BISECT_START", "rebase-merge", "rebase-apply",
        "sequencer", "index.lock",
    ):
        if _git_path(parent, marker).exists():
            raise WorktreeIntegrationError(f"parent has an in-progress Git operation or index lock ({marker})")
    sparse = subprocess.run(
        ["git", "--no-optional-locks", "config", "--bool", "core.sparseCheckout"],
        cwd=parent,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        env=_git_environment(),
    )
    if sparse.returncode == 0 and sparse.stdout.strip().lower() == b"true":
        raise WorktreeIntegrationError("sparse parent checkouts are unsupported")
    index = _git_path(parent, "index")
    try:
        index_bytes = index.read_bytes()
    except OSError as exc:
        raise WorktreeIntegrationError("could not snapshot the parent index") from exc
    return parent, head, hashlib.sha256(index_bytes).digest()


def _validate_review(record: WorktreeRecord, artifact: ReviewArtifact) -> tuple[dict[str, Any], dict[str, bytes]]:
    from .worktrees import get as get_worktree

    if record.lifecycle != "finalized":
        raise WorktreeIntegrationError("worktree integration requires a finalized child")
    if (
        not record.current_review_id
        or record.current_review_id != artifact.review_id
        or record.current_review_digest != artifact.digest
        or record.acknowledged_review_id != artifact.review_id
        or record.acknowledged_digest != artifact.digest
    ):
        raise WorktreeIntegrationError("review must be the current acknowledged review")
    if artifact.artifact_path.name != artifact.review_id or artifact.artifact_path.parent.name != "reviews":
        raise WorktreeIntegrationError("review artifact is not in the owned worktree review store")
    persisted = get_worktree(record.child_id, root=artifact.artifact_path.parent.parent)
    if (
        persisted.parent_workspace.resolve(strict=True) != record.parent_workspace.resolve(strict=True)
        or persisted.path.resolve(strict=True) != record.path.resolve(strict=True)
        or persisted.base_commit != record.base_commit
        or persisted.lifecycle != "finalized"
        or persisted.current_review_id != artifact.review_id
        or persisted.current_review_digest != artifact.digest
        or persisted.acknowledged_review_id != artifact.review_id
        or persisted.acknowledged_digest != artifact.digest
    ):
        raise WorktreeIntegrationError("review acknowledgment is not authenticated as current")
    loaded = load_review(record, artifact.artifact_path.parent, artifact.review_id)
    if not hmac.compare_digest(loaded.digest, artifact.digest):
        raise WorktreeIntegrationError("review artifact digest changed")
    manifest = loaded.manifest
    if manifest.get("base_commit") != record.base_commit or manifest.get("child_id") != record.child_id:
        raise WorktreeIntegrationError("review artifact does not match the worktree record")
    blobs: dict[str, bytes] = {}
    for digest in manifest.get("blobs", []):
        data = (loaded.artifact_path / "blobs" / digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise WorktreeIntegrationError("frozen review blob failed integrity verification")
        blobs[digest] = data
    return manifest, blobs


def _path_bytes(value: object) -> bytes:
    if not isinstance(value, str) or not value or "\x00" in value or value.startswith("/"):
        raise WorktreeIntegrationError("review contains an invalid parent-relative path")
    encoded = os.fsencode(value)
    parts = encoded.split(b"/")
    if any(
        part in (b"", b".", b"..") or os.fsdecode(part).casefold() == ".git"
        for part in parts
    ):
        raise WorktreeIntegrationError("review path contains an unsafe component")
    return encoded


def _inventory(root: Path) -> dict[bytes, tuple[int, int]]:
    found: dict[bytes, tuple[int, int]] = {}
    stack: list[tuple[bytes, bytes]] = [(b"", os.fsencode(root))]
    while stack:
        prefix, directory = stack.pop()
        with os.scandir(directory) as scan:
            entries = list(scan)
        for entry in entries:
            name = entry.name if isinstance(entry.name, bytes) else os.fsencode(entry.name)
            if not prefix and os.fsdecode(name).casefold() == ".git":
                continue
            relative = prefix + name
            info = os.stat(os.path.join(directory, name), follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                found[relative] = (-3, info.st_ino)
                stack.append((relative + b"/", os.path.join(directory, name)))
            elif stat.S_ISREG(info.st_mode):
                found[relative] = (stat.S_IMODE(info.st_mode), info.st_ino)
            elif stat.S_ISLNK(info.st_mode):
                found[relative] = (-1, info.st_ino)
            else:
                found[relative] = (-2, info.st_ino)
    return found


def _open_parent(root_fd: int, path: bytes) -> tuple[int, bytes]:
    parts = path.split(b"/")
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            _check_name_alias(current, part)
            next_fd = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=current)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _read_at(directory_fd: int, name: bytes) -> tuple[bytes, int] | None:
    _check_name_alias(directory_fd, name)
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise WorktreeIntegrationError("integration target is not a regular file")
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def _check_name_alias(directory_fd: int, name: bytes) -> None:
    wanted = os.fsdecode(name).casefold()
    for candidate in os.listdir(directory_fd):
        candidate_bytes = os.fsencode(candidate)
        if os.fsdecode(candidate_bytes).casefold() == wanted and candidate_bytes != name:
            raise WorktreeIntegrationError("integration target resolves through a case-alias")


def _current(directory_fd: int, name: bytes) -> tuple[bool, int | None, str | None]:
    item = _read_at(directory_fd, name)
    if item is None:
        return False, None, None
    data, mode = item
    return True, mode, hashlib.sha256(data).hexdigest()


def _same_state(directory_fd: int, name: bytes, exists: bool, mode: int | None, digest: str | None) -> bool:
    return _current(directory_fd, name) == (exists, mode, digest)


def _write_at(directory_fd: int, name: bytes, data: bytes, mode: int, *, no_clobber: bool) -> None:
    temporary = f".nexus-integrate-{secrets.token_hex(12)}".encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fchmod(fd, mode)
        os.fsync(fd)
        if no_clobber:
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory_fd)
        else:
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise


def _apply_entry(root_fd: int, entry: _Entry, new_data: bytes | None) -> None:
    parent_fd, name = _open_parent(root_fd, _path_bytes(entry.path))
    try:
        if not _same_state(
            parent_fd, name, entry.old_exists, entry.old_mode, entry.old_sha256
        ):
            raise WorktreeIntegrationError("parent target changed during integration")
        if entry.new_sha256 is None:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        else:
            assert new_data is not None and entry.new_mode is not None
            _write_at(parent_fd, name, new_data, entry.new_mode, no_clobber=not entry.old_exists)
    finally:
        os.close(parent_fd)


def _restore_entry(root_fd: int, entry: _Entry, old_data: bytes | None) -> bool:
    parent_fd, name = _open_parent(root_fd, _path_bytes(entry.path))
    try:
        old_state = (entry.old_exists, entry.old_mode, entry.old_sha256)
        new_state = (entry.new_sha256 is not None, entry.new_mode, entry.new_sha256)
        current = _current(parent_fd, name)
        if current == old_state:
            return True
        if current != new_state:
            return False
        if not entry.old_exists:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        else:
            if old_data is None or entry.old_mode is None:
                return False
            _write_at(parent_fd, name, old_data, entry.old_mode, no_clobber=False)
        return True
    except (OSError, WorktreeIntegrationError):
        return False
    finally:
        os.close(parent_fd)


def _private_root(value: str | Path, parent: Path, child: Path) -> Path:
    requested = Path(value).expanduser()
    absolute = requested.absolute()
    if any(component.is_symlink() for component in (absolute, *absolute.parents) if component.exists()):
        raise WorktreeIntegrationError("transaction root path cannot contain symlinks")
    root = requested.resolve(strict=False)
    for checkout in (parent, child):
        if root == checkout or root in checkout.parents or checkout in root.parents:
            raise WorktreeIntegrationError("transaction root must be outside both checkouts")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise WorktreeIntegrationError("transaction root must be a directory owned by this user")
    os.chmod(root, 0o700)
    return root


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / "journal.json")
        _fsync_dir(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _journal_entries(manifest: dict[str, Any], directory: Path) -> tuple[list[_Entry], dict[str, bytes], dict[str, bytes]]:
    blob_dir = directory / "blobs"
    blob_info = blob_dir.stat(follow_symlinks=False)
    if not stat.S_ISDIR(blob_info.st_mode) or blob_info.st_uid != os.getuid() or stat.S_IMODE(blob_info.st_mode) != 0o700:
        raise WorktreeIntegrationError("transaction blob directory is not private")
    entries: list[_Entry] = []
    old_blobs: dict[str, bytes] = {}
    new_blobs: dict[str, bytes] = {}
    for row in manifest["entries"]:
        entry = _Entry(**row)
        entries.append(entry)
        for blob_name, digest, target in ((entry.old_blob, entry.old_sha256, old_blobs), (entry.new_blob, entry.new_sha256, new_blobs)):
            if blob_name is None:
                continue
            if Path(blob_name).name != blob_name or not all(c in "0123456789abcdef" for c in blob_name):
                raise WorktreeIntegrationError("transaction journal contains an invalid blob name")
            blob_path = directory / "blobs" / blob_name
            info = blob_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise WorktreeIntegrationError("transaction journal blob is not private and regular")
            data = blob_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise WorktreeIntegrationError("transaction journal blob failed integrity verification")
            target[entry.path] = data
    return entries, old_blobs, new_blobs


def _rollback(directory: Path, parent: Path, manifest: dict[str, Any]) -> tuple[str, ...]:
    entries, old_blobs, _ = _journal_entries(manifest, directory)
    root_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    conflicts: list[str] = []
    try:
        for entry in reversed(entries):
            if not _restore_entry(root_fd, entry, old_blobs.get(entry.path)):
                conflicts.append(entry.path)
    finally:
        os.close(root_fd)
    return tuple(conflicts)


def _check_targets(parent: Path, paths: list[bytes]) -> dict[bytes, tuple[bytes, int] | None]:
    inventory = _inventory(parent)
    aliases: dict[str, bytes] = {}
    for existing in inventory:
        key = os.fsdecode(existing).casefold()
        prior = aliases.get(key)
        if prior is not None and prior != existing:
            raise WorktreeIntegrationError("parent contains case-aliasing paths")
        aliases[key] = existing
    for path in paths:
        decoded = os.fsdecode(path)
        key = decoded.casefold()
        prior = aliases.get(key)
        if prior is not None and prior != path:
            raise WorktreeIntegrationError("review path has a case-alias in the parent")
        aliases[key] = path
        for component_end in [i for i, byte in enumerate(path) if byte == 47]:
            ancestor = path[:component_end]
            if ancestor in inventory and inventory[ancestor][0] != -3:
                raise WorktreeIntegrationError("review path has a non-directory or symlink ancestor")
            actual = next((candidate for candidate in inventory if os.fsdecode(candidate).casefold() == os.fsdecode(ancestor).casefold()), None)
            if actual is not None:
                raise WorktreeIntegrationError("review path has a case-alias ancestor")
        existing = inventory.get(path)
        if existing is not None and existing[0] < 0:
            raise WorktreeIntegrationError("review target is a symlink or special file")
        if existing is not None and existing[0] & 0o7000:
            raise WorktreeIntegrationError("review target has special permission bits")
    targets: dict[bytes, tuple[bytes, int] | None] = {}
    root_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for path in paths:
            try:
                parent_fd, name = _open_parent(root_fd, path)
            except FileNotFoundError as exc:
                raise WorktreeIntegrationError("review targets must have existing parent directories") from exc
            try:
                targets[path] = _read_at(parent_fd, name)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    return targets


def _expected_parent_status(parent: Path, paths: set[bytes]) -> bool:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=parent,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        env=_git_environment(),
    )
    if result.returncode:
        return False
    actual: set[bytes] = set()
    for row in result.stdout.split(b"\0"):
        if row:
            if len(row) < 4:
                return False
            actual.add(row[3:])
    return actual == paths


def _saved_git_state_matches(parent: Path, journal: dict[str, Any]) -> bool:
    try:
        head = _git(parent, "rev-parse", "--verify", "HEAD^{commit}")
        index_digest = hashlib.sha256(_git_path(parent, "index").read_bytes()).hexdigest()
    except (OSError, WorktreeIntegrationError):
        return False
    return head == journal.get("head") and index_digest == journal.get("index_sha256")


def _check_cancel(cancel: object | None) -> None:
    if cancel is None:
        return
    method = getattr(cancel, "raise_if_cancelled", None)
    if callable(method):
        method()
    elif callable(cancel):
        cancel()


def _integrate_review(
    record: WorktreeRecord,
    artifact: ReviewArtifact,
    transaction_root: str | Path,
    *,
    cancel: object | None = None,
    validate_current: Any | None = None,
    authenticated_record: bool = False,
) -> IntegrationResult:
    """Apply exactly the frozen bytes in a current acknowledged review.

    The helper never invokes Git mutation commands. Parent modifications remain
    unstaged, and each file operation is recoverable rather than globally
    atomic across process or machine failure.
    """
    parent, head, index_digest = _parent_preconditions(record)
    if authenticated_record:
        if (
            record.lifecycle != "finalized"
            or record.current_review_id != artifact.review_id
            or record.current_review_digest != artifact.digest
            or record.acknowledged_review_id != artifact.review_id
            or record.acknowledged_digest != artifact.digest
        ):
            raise WorktreeIntegrationError("review must be the current acknowledged review")
        loaded = load_review(record, artifact.artifact_path.parent, artifact.review_id)
        if not hmac.compare_digest(loaded.digest, artifact.digest):
            raise WorktreeIntegrationError("review artifact digest changed")
        manifest = loaded.manifest
        frozen_blobs = {
            digest: (loaded.artifact_path / "blobs" / digest).read_bytes()
            for digest in manifest.get("blobs", [])
        }
        if any(hashlib.sha256(blob).hexdigest() != digest for digest, blob in frozen_blobs.items()):
            raise WorktreeIntegrationError("frozen review blob failed integrity verification")
        if manifest.get("base_commit") != record.base_commit or manifest.get("child_id") != record.child_id:
            raise WorktreeIntegrationError("review artifact does not match the worktree record")
    else:
        manifest, frozen_blobs = _validate_review(record, artifact)
    rows = manifest.get("entries")
    if not isinstance(rows, list):
        raise WorktreeIntegrationError("review entries are invalid")
    paths = [_path_bytes(row.get("path")) for row in rows]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise WorktreeIntegrationError("review paths must be unique and deterministically sorted")
    root = _private_root(transaction_root, parent, record.path.resolve(strict=True))
    transaction_id = secrets.token_hex(16)
    journal = root / transaction_id
    journal.mkdir(mode=0o700)
    (journal / "blobs").mkdir(mode=0o700)
    _fsync_dir(root)
    entries: list[_Entry] = []
    try:
        snapshots = _check_targets(parent, paths)
        for row, path in zip(rows, paths):
            change = row.get("change")
            old_mode_text, new_mode_text = row.get("old_mode"), row.get("new_mode")
            old_mode = int(old_mode_text, 8) if old_mode_text is not None else None
            new_mode = int(new_mode_text, 8) if new_mode_text is not None else None
            if old_mode not in (None, 0o100644, 0o100755) or new_mode not in (None, 0o100644, 0o100755):
                raise WorktreeIntegrationError("review contains an unsupported file mode")
            target = snapshots[path]
            if (change == "added") != (target is None):
                raise WorktreeIntegrationError("review change type does not match the clean parent")
            old_data = target[0] if target else None
            old_perm = target[1] if target else None
            old_sha = hashlib.sha256(old_data).hexdigest() if old_data is not None else None
            if old_sha != row.get("old_sha256"):
                raise WorktreeIntegrationError("review original content does not match the parent base")
            if target is not None and bool(old_perm & 0o111) != (old_mode == 0o100755):
                raise WorktreeIntegrationError("parent target mode does not match review base mode")
            new_sha = row.get("new_sha256")
            new_data = frozen_blobs.get(new_sha) if new_sha is not None else None
            if new_sha is not None and (new_data is None or hashlib.sha256(new_data).hexdigest() != new_sha):
                raise WorktreeIntegrationError("review does not contain the frozen new file bytes")
            old_blob = hashlib.sha256(old_data).hexdigest() if old_data is not None else None
            new_blob = hashlib.sha256(new_data).hexdigest() if new_data is not None else None
            for digest, content in ((old_blob, old_data), (new_blob, new_data)):
                if digest is not None and content is not None:
                    path_on_disk = journal / "blobs" / digest
                    if not path_on_disk.exists():
                        fd = os.open(path_on_disk, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
            entries.append(_Entry(
                path=os.fsdecode(path), old_exists=target is not None, old_mode=old_perm,
                old_sha256=old_sha, old_blob=old_blob,
                new_mode=(
                    ((old_perm & ~0o111) | (0o111 if new_mode == 0o100755 else 0))
                    if old_perm is not None
                    else (0o755 if new_mode == 0o100755 else 0o644)
                ) if new_mode is not None else None,
                new_sha256=new_sha, new_blob=new_blob,
            ))
        _fsync_dir(journal / "blobs")
        journal_manifest: dict[str, Any] = {
            "version": 1, "transaction_id": transaction_id,
            "parent": str(parent), "child_id": record.child_id,
            "base_commit": record.base_commit, "head": head,
            "index_sha256": index_digest.hex(), "review_id": artifact.review_id,
            "review_digest": artifact.digest, "state": "applying",
            "entries": [entry.__dict__ for entry in entries],
        }
        _write_manifest(journal, journal_manifest)
    except BaseException:
        # No parent mutation has occurred; preserve no partial journal debris.
        import shutil

        shutil.rmtree(journal, ignore_errors=True)
        raise

    root_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    failure: BaseException | None = None
    try:
        for entry in entries:
            _check_cancel(cancel)
            data = frozen_blobs.get(entry.new_sha256) if entry.new_sha256 else None
            _apply_entry(root_fd, entry, data)
        _check_cancel(cancel)
        for entry in entries:
            parent_fd, name = _open_parent(root_fd, _path_bytes(entry.path))
            try:
                if not _same_state(
                    parent_fd,
                    name,
                    entry.new_sha256 is not None,
                    entry.new_mode,
                    entry.new_sha256,
                ):
                    raise WorktreeIntegrationError(
                        f"integrated target changed during transaction ({entry.path})"
                    )
            finally:
                os.close(parent_fd)
        if (
            _git(parent, "rev-parse", "--verify", "HEAD^{commit}") != head
            or hashlib.sha256(_git_path(parent, "index").read_bytes()).hexdigest() != index_digest.hex()
            or not _expected_parent_status(parent, {path for path in paths})
        ):
            raise WorktreeIntegrationError("parent Git state changed during integration")
        # A child edit during the transaction invalidates the approval. Re-load
        # the immutable artifact to re-run the review's child snapshot check.
        load_review(record, artifact.artifact_path.parent, artifact.review_id)
        if validate_current is not None:
            validate_current()
    except Exception as exc:  # noqa: BLE001 - all ordinary failures trigger rollback
        failure = exc
    finally:
        os.close(root_fd)

    if failure is not None:
        if not _saved_git_state_matches(parent, journal_manifest):
            conflicts = tuple(entry.path for entry in entries)
            journal_manifest["state"] = "recovery_required"
            journal_manifest["conflicts"] = list(conflicts)
            with contextlib.suppress(OSError):
                _write_manifest(journal, journal_manifest)
            return IntegrationResult(
                "recovery_required", transaction_id, tuple(entry.path for entry in entries),
                str(failure), conflicts,
            )
        try:
            conflicts = _rollback(journal, parent, journal_manifest)
        except (OSError, WorktreeIntegrationError, KeyError, TypeError, ValueError) as exc:
            journal_manifest["state"] = "recovery_required"
            journal_manifest["conflicts"] = [entry.path for entry in entries]
            with contextlib.suppress(OSError):
                _write_manifest(journal, journal_manifest)
            return IntegrationResult(
                "recovery_required", transaction_id, tuple(entry.path for entry in entries),
                str(exc), tuple(entry.path for entry in entries),
            )
        if conflicts:
            journal_manifest["state"] = "recovery_required"
            journal_manifest["conflicts"] = list(conflicts)
            with contextlib.suppress(OSError):
                _write_manifest(journal, journal_manifest)
            return IntegrationResult("recovery_required", transaction_id, tuple(entry.path for entry in entries), str(failure), conflicts)
        journal_manifest["state"] = "rolled_back"
        with contextlib.suppress(OSError):
            _write_manifest(journal, journal_manifest)
        return IntegrationResult("rolled_back", transaction_id, tuple(entry.path for entry in entries), str(failure))
    journal_manifest["state"] = "integrated"
    _write_manifest(journal, journal_manifest)
    return IntegrationResult("integrated", transaction_id, tuple(entry.path for entry in entries))


def integrate_review(
    record: WorktreeRecord,
    artifact: ReviewArtifact,
    transaction_root: str | Path,
    *,
    cancel: object | None = None,
) -> IntegrationResult:
    """Integrate under both service lifecycle and transaction locks."""
    from .worktrees import _integrate

    return _integrate(
        record.child_id,
        artifact.review_id,
        artifact.digest,
        root=artifact.artifact_path.parent.parent,
        runtime_ownership=None,
        cancel=cancel,
        transaction_root=transaction_root,
        expected_record=record,
        persist_lifecycle=False,
    )


def _integrate_authenticated(
    record: WorktreeRecord,
    artifact: ReviewArtifact,
    transaction_root: str | Path,
    *,
    validate_current: Any,
    cancel: object | None = None,
) -> IntegrationResult:
    """Transaction core for callers already holding the authenticated service lock."""
    parent = record.parent_workspace.expanduser().resolve(strict=True)
    root = _private_root(transaction_root, parent, record.path.resolve(strict=True))
    lock_fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise WorktreeIntegrationError("transaction lock is not a private owned regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        recovery = _recover_locked(root, parent, record)
        blocked = tuple(item for item in recovery if item.status == "recovery_required")
        if blocked:
            paths = tuple(dict.fromkeys(path for item in blocked for path in item.recovery_required))
            return IntegrationResult(
                "recovery_required",
                error="stale transaction recovery is incomplete; resolve the listed recovery requirements before integrating",
                recovery_required=paths,
            )
        return _integrate_review(
            record,
            artifact,
            root,
            cancel=cancel,
            validate_current=validate_current,
            authenticated_record=True,
        )
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _integrated_journals(
    transaction_root: str | Path,
    record: WorktreeRecord,
) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """Return safe integrated journals matching one child and parent."""
    parent = record.parent_workspace.expanduser().resolve(strict=True)
    root = _private_root(transaction_root, parent, record.path.resolve(strict=True))
    lock_fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise WorktreeIntegrationError("transaction lock is not a private owned regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        matches = []
        for directory in sorted(root.iterdir()):
            if directory.name == ".lock" or not directory.is_dir() or directory.is_symlink():
                continue
            journal = _read_journal(directory)
            if (
                journal.get("state") == "integrated"
                and journal.get("parent") == str(parent)
                and journal.get("child_id") == record.child_id
                and journal.get("base_commit") == record.base_commit
            ):
                matches.append((directory, journal))
        return tuple(matches)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _read_journal(directory: Path) -> dict[str, Any]:
    info = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WorktreeIntegrationError("transaction journal directory is not private")
    path = directory / "journal.json"
    try:
        file_info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorktreeIntegrationError("transaction journal manifest is missing or unavailable") from exc
    if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid() or stat.S_IMODE(file_info.st_mode) != 0o600:
        raise WorktreeIntegrationError("transaction journal manifest is not private")
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise WorktreeIntegrationError("transaction journal is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("state") not in {"applying", "recovery_required", "integrated", "rolled_back"}:
        raise WorktreeIntegrationError("transaction journal has an unsupported format")
    return manifest


def _recovery_result(directory: Path, journal: dict[str, Any] | None, error: str) -> IntegrationResult:
    entries = journal.get("entries", []) if journal else []
    if not isinstance(entries, list):
        entries = []
    paths = tuple(
        item.get("path", "<invalid-path>") for item in entries
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    )
    return IntegrationResult(
        "recovery_required",
        journal.get("transaction_id") if journal else directory.name,
        paths,
        error=error,
        recovery_required=paths or (str(directory),),
    )


def _recover_locked(root: Path, parent: Path, record: WorktreeRecord) -> tuple[IntegrationResult, ...]:
    results: list[IntegrationResult] = []
    for directory in sorted(root.iterdir()):
        if directory.name == ".lock" or not directory.is_dir() or directory.is_symlink():
            continue
        journal: dict[str, Any] | None = None
        try:
            directory_info = directory.stat(follow_symlinks=False)
            if (
                directory_info.st_uid != os.getuid()
                or stat.S_IMODE(directory_info.st_mode) != 0o700
            ):
                raise WorktreeIntegrationError("transaction journal directory is not private")
            if not (directory / "journal.json").exists():
                results.append(_recovery_result(directory, None, "orphan transaction directory has no journal; contents left untouched"))
                continue
            journal = _read_journal(directory)
            if journal.get("parent") != str(parent) or journal.get("child_id") != record.child_id:
                continue
            if journal.get("base_commit") != record.base_commit:
                results.append(_recovery_result(directory, journal, "transaction journal does not match the worktree base"))
                continue
            if journal["state"] in {"integrated", "rolled_back"}:
                continue
            if not _saved_git_state_matches(parent, journal):
                results.append(_recovery_result(directory, journal, "parent HEAD or index changed since the transaction was journaled; no files were restored"))
                continue
            conflicts = _rollback(directory, parent, journal)
            if conflicts:
                journal["state"] = "recovery_required"
                journal["conflicts"] = list(conflicts)
                _write_manifest(directory, journal)
                results.append(IntegrationResult("recovery_required", journal.get("transaction_id"), tuple(item["path"] for item in journal["entries"]), recovery_required=conflicts))
            else:
                journal["state"] = "rolled_back"
                journal.pop("conflicts", None)
                _write_manifest(directory, journal)
                results.append(IntegrationResult("rolled_back", journal.get("transaction_id"), tuple(item["path"] for item in journal["entries"])))
        except (OSError, WorktreeIntegrationError, KeyError, TypeError, ValueError) as exc:
            results.append(_recovery_result(directory, journal, str(exc)))
    return tuple(results)


def recover_transactions(transaction_root: str | Path, record: WorktreeRecord) -> tuple[IntegrationResult, ...]:
    """Idempotently roll back incomplete transactions for ``record``."""
    parent = record.parent_workspace.expanduser().resolve(strict=True)
    root = _private_root(transaction_root, parent, record.path.resolve(strict=True))
    info = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WorktreeIntegrationError("transaction root must be private and owned by this user")
    lock_fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
            raise WorktreeIntegrationError("transaction lock is not a private owned regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _recover_locked(root, parent, record)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
