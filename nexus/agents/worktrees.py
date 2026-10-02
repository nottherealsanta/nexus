"""Safe Git worktree lifecycle management for subagents.

Creation, review, acknowledgment, and integration use authenticated service
records and a service-root lock. Integration applies frozen file bytes without
Git commit, reset, stash, or checkout commands; creation cleanup removes only a
worktree allocated by that failed creation.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .worktree_integrate import IntegrationResult

__all__ = [
    "WorktreeError",
    "WorktreeRecord",
    "WorktreeReviewPage",
    "WorktreeService",
    "create",
    "get",
    "inspect",
    "list",
]

_FORMAT_VERSION = 2
_LEGACY_FORMAT_VERSION = 1
_LIFECYCLES = frozenset(
    {"active", "finalized", "integrated", "cleanup_pending", "discarded"}
)
_BRANCH_PREFIX = "nexus/subagent/"
_TOKEN_BYTES = 12
_COLLISION_ATTEMPTS = 32
_CHILD_ID_MAX = 256
_REVIEW_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_REVIEW_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_REVIEW_PAGE_LIMIT = 8
_GIT_PROXY_ENV = frozenset(
    {
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class WorktreeError(RuntimeError):
    """A worktree precondition, Git operation, or ownership check failed."""


@dataclass(frozen=True)
class WorktreeRecord:
    """Durable ownership information for a subagent checkout."""

    child_id: str
    parent_workspace: Path
    base_commit: str
    branch: str
    path: Path
    owner_uid: int
    created_at: str
    dirty_status: str = ""
    lifecycle: str = "active"
    final_status: str | None = None
    final_dirty_status: str | None = None
    finalized_at: str | None = None
    review_status: str | None = None
    reviewed_at: str | None = None
    reviewer: str | None = None
    current_review_id: str | None = None
    current_review_digest: str | None = None
    acknowledged_review_id: str | None = None
    acknowledged_digest: str | None = None
    acknowledged_at: str | None = None
    integrated_at: str | None = None
    integrated_review_id: str | None = None
    integrated_digest: str | None = None
    integration_transaction_id: str | None = None
    discard_expected_oid: str | None = None
    discard_state: str | None = None

    @property
    def dirty(self) -> bool:
        if self.lifecycle == "finalized" and self.final_dirty_status is not None:
            return bool(self.final_dirty_status)
        return bool(self.dirty_status)


@dataclass(frozen=True)
class WorktreeReviewPage:
    """One bounded page from the current immutable child review."""

    review_id: str
    digest: str
    manifest: dict[str, object]
    diff_pages: tuple[bytes, ...]
    cursor: int
    next_cursor: int | None
    total_pages: int
    artifact_path: Path


class WorktreeService:
    """Injected facade over the standalone worktree operations."""

    def __init__(
        self,
        *,
        runtime_ownership: Callable[[str], bool | None] | None = None,
    ) -> None:
        """Create the facade.

        ``runtime_ownership`` returns true for a live owner, false when absence
        of an owner has been confirmed, and ``None`` when ownership is unknown.
        Legacy v1 records remain conservatively active unless it returns false.
        """
        self._runtime_ownership = runtime_ownership

    def create(
        self,
        parent_workspace: str | Path,
        child_id: str,
        *,
        root: str | Path,
    ) -> WorktreeRecord:
        return create(parent_workspace, child_id, root=root)

    def inspect(self, child_id: str, *, root: str | Path) -> WorktreeRecord:
        return _inspect(child_id, root=root, runtime_ownership=self._runtime_ownership)

    def get(self, child_id: str, *, root: str | Path) -> WorktreeRecord:
        """Load and verify one record using this service's legacy ownership check."""
        return _get(child_id, root=root, runtime_ownership=self._runtime_ownership)

    def list(self, *, root: str | Path) -> tuple[WorktreeRecord, ...]:
        """List verified records using this service's legacy ownership check."""
        return _list(root=root, runtime_ownership=self._runtime_ownership)

    def mark_finished(
        self,
        child_id: str,
        outcome: object,
        *,
        root: str | Path,
    ) -> WorktreeRecord:
        """Persist final status and dirty state without removing the checkout."""
        return _mark_finished(
            child_id,
            outcome,
            root=root,
            runtime_ownership=self._runtime_ownership,
        )

    def review(
        self,
        child_id: str,
        review_id: str | None = None,
        cursor: int = 0,
        limit: int = 1,
        *,
        root: str | Path,
    ) -> WorktreeReviewPage:
        """Build or load the current review and return a bounded page."""
        return _review(
            child_id,
            review_id=review_id,
            cursor=cursor,
            limit=limit,
            root=root,
            runtime_ownership=self._runtime_ownership,
        )

    def acknowledge(
        self,
        child_id: str,
        review_id: str,
        digest: str,
        *,
        root: str | Path,
    ) -> WorktreeRecord:
        """Durably acknowledge the current review after revalidating the child."""
        return _acknowledge(
            child_id,
            review_id,
            digest,
            root=root,
            runtime_ownership=self._runtime_ownership,
        )

    def integrate(
        self,
        child_id: str,
        review_id: str,
        digest: str,
        *,
        root: str | Path,
        cancel: object | None = None,
    ) -> IntegrationResult:
        """Apply the acknowledged current review while excluding review/ack races."""
        return _integrate(
            child_id,
            review_id,
            digest,
            root=root,
            runtime_ownership=self._runtime_ownership,
            cancel=cancel,
        )

    def recover_pending(
        self, child_id: str, *, root: str | Path
    ) -> WorktreeRecord:
        """Finish authenticated lifecycle promotion after an integrated journal."""
        return _recover_pending(
            child_id,
            root=root,
            runtime_ownership=self._runtime_ownership,
        )

    def discard(
        self,
        child_id: str,
        *,
        root: str | Path,
        force: bool = False,
        acknowledged_review_id: str | None = None,
        cancel: object | None = None,
    ) -> WorktreeRecord:
        """Safely remove an owned, inactive child checkout and its exact branch.

        ``force`` bypasses only review freshness and checkout cleanliness. It
        never bypasses authenticated ownership, Git registration, branch/base
        validation, or the live-child check.
        """
        return _discard(
            child_id,
            root=root,
            force=force,
            acknowledged_review_id=acknowledged_review_id,
            runtime_ownership=self._runtime_ownership,
            cancel=cancel,
        )


def _git_environment() -> dict[str, str]:
    """Build a Git environment that cannot redirect repository discovery."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        and key.lower() not in _GIT_PROXY_ENV
        and "proxy_" not in key.lower()
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git_argv(*args: str, observational: bool = True) -> list[str]:
    """Return a fixed, shell-free argv; read-only calls skip optional locks."""
    prefix = ["--no-optional-locks"] if observational else []
    return ["git", *prefix, *args]


def _git(cwd: Path, *args: str, check: bool = True, observational: bool = True) -> str:
    try:
        result = subprocess.run(
            _git_argv(*args, observational=observational),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise WorktreeError(f"could not run git: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise WorktreeError(
            f"git {' '.join(args)} failed ({result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()


def _checkout_root(parent_workspace: str | Path) -> Path:
    parent = Path(parent_workspace).expanduser().resolve(strict=True)
    if not parent.is_dir():
        raise WorktreeError("parent_workspace must be a Git checkout directory")
    top_level = Path(_git(parent, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if parent != top_level:
        raise WorktreeError("parent_workspace must be the top-level Git checkout")
    return top_level


def _validate_child_id(child_id: str) -> str:
    if not isinstance(child_id, str) or not child_id or len(child_id) > _CHILD_ID_MAX:
        raise WorktreeError(f"child_id must be a non-empty string of at most {_CHILD_ID_MAX} characters")
    if "\x00" in child_id:
        raise WorktreeError("child_id cannot contain NUL")
    try:
        child_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorktreeError("child_id must be valid UTF-8 text") from exc
    return child_id


def _record_name(child_id: str) -> str:
    return hashlib.sha256(child_id.encode("utf-8")).hexdigest() + ".json"


def _secure_directory(path: Path, *, create: bool) -> None:
    if path.is_symlink():
        raise WorktreeError(f"worktree service directory cannot be a symlink: {path}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorktreeError(f"cannot access worktree service directory {path}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise WorktreeError(f"worktree service path is not a directory: {path}")
    if info.st_uid != os.getuid():
        raise WorktreeError(f"worktree service directory is not owned by this user: {path}")
    # The caller explicitly designated this as its daemon-owned storage root.
    # Tighten permissions before creating any contents beneath it.
    os.chmod(path, 0o700)


def _layout(root_value: str | Path, parent: Path, *, create: bool) -> tuple[Path, Path, Path]:
    requested = Path(root_value).expanduser()
    if requested.is_symlink():
        raise WorktreeError("worktree root cannot be a symlink")
    root = requested.resolve(strict=False)
    if root == parent or parent in root.parents or root in parent.parents:
        raise WorktreeError("worktree root must be outside and separate from the parent Git checkout")
    if create:
        _secure_directory(root, create=True)
        _secure_directory(root / "records", create=True)
        _secure_directory(root / "worktrees", create=True)
    else:
        _secure_directory(root, create=False)
        _secure_directory(root / "records", create=False)
        _secure_directory(root / "worktrees", create=False)
    return root, root / "records", root / "worktrees"


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    lock_path = root / ".lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WorktreeError(f"cannot open worktree service lock: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise WorktreeError("worktree service lock is not a private owned file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_key(root: Path, *, create: bool) -> bytes:
    key_path = root / ".ownership-key"
    if create:
        try:
            fd = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        else:
            try:
                key = secrets.token_bytes(32)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_directory(root)
            except BaseException:
                with contextlib.suppress(OSError):
                    key_path.unlink()
                raise
    try:
        key_fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(key_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise WorktreeError("worktree ownership key is not a private owned file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise WorktreeError("worktree ownership key permissions are not private")
        with os.fdopen(key_fd, "rb") as handle:
            key_fd = -1
            key = handle.read()
    except OSError as exc:
        raise WorktreeError(f"cannot read worktree ownership key: {exc}") from exc
    finally:
        if "key_fd" in locals() and key_fd >= 0:
            os.close(key_fd)
    if len(key) != 32:
        raise WorktreeError("worktree ownership key has an invalid length")
    return key


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _payload(record: WorktreeRecord) -> dict[str, object]:
    return {
        "version": _FORMAT_VERSION,
        "child_id": record.child_id,
        "parent_workspace": str(record.parent_workspace),
        "base_commit": record.base_commit,
        "branch": record.branch,
        "path": str(record.path),
        "owner_uid": record.owner_uid,
        "created_at": record.created_at,
        "lifecycle": record.lifecycle,
        "final_status": record.final_status,
        "final_dirty_status": record.final_dirty_status,
        "finalized_at": record.finalized_at,
        "review_status": record.review_status,
        "reviewed_at": record.reviewed_at,
        "reviewer": record.reviewer,
        "current_review_id": record.current_review_id,
        "current_review_digest": record.current_review_digest,
        "acknowledged_review_id": record.acknowledged_review_id,
        "acknowledged_digest": record.acknowledged_digest,
        "acknowledged_at": record.acknowledged_at,
        "integrated_at": record.integrated_at,
        "integrated_review_id": record.integrated_review_id,
        "integrated_digest": record.integrated_digest,
        "integration_transaction_id": record.integration_transaction_id,
        "discard_expected_oid": record.discard_expected_oid,
        "discard_state": record.discard_state,
    }


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_metadata_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WorktreeError(f"cannot safely open worktree metadata {path.name}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise WorktreeError("worktree metadata is not a regular file owned by this user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise WorktreeError("worktree metadata permissions are not private")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _encoded_record(record: WorktreeRecord, key: bytes) -> bytes:
    payload = _payload(record)
    envelope = {
        "record": payload,
        "mac": hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest(),
    }
    return json.dumps(envelope, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _write_record(path: Path, record: WorktreeRecord, key: bytes) -> None:
    data = _encoded_record(record, key)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    published = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # link(2) publishes atomically without replacing any pre-existing file.
        os.link(temporary, path, follow_symlinks=False)
        published = True
        os.unlink(temporary)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        if published:
            with contextlib.suppress(OSError):
                path.unlink()
        raise


def _replace_record(path: Path, record: WorktreeRecord, key: bytes) -> None:
    """Atomically replace an authenticated record and durably publish it."""
    data = _encoded_record(record, key)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_record(path: Path, child_id: str, key: bytes) -> WorktreeRecord:
    try:
        envelope = json.loads(_read_metadata_bytes(path))
        payload = envelope["record"]
        supplied_mac = envelope["mac"]
    except WorktreeError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorktreeError(f"cannot read worktree metadata for {child_id!r}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(supplied_mac, str):
        raise WorktreeError("worktree metadata has an invalid format")
    expected_mac = hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise WorktreeError("worktree metadata authentication failed")
    try:
        version = payload["version"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version not in {_LEGACY_FORMAT_VERSION, _FORMAT_VERSION}
            or payload["child_id"] != child_id
        ):
            raise WorktreeError("worktree metadata identity or version does not match")
        record = WorktreeRecord(
            child_id=payload["child_id"],
            parent_workspace=Path(payload["parent_workspace"]),
            base_commit=payload["base_commit"],
            branch=payload["branch"],
            path=Path(payload["path"]),
            owner_uid=payload["owner_uid"],
            created_at=payload["created_at"],
            lifecycle=payload.get("lifecycle", "active"),
            final_status=payload.get("final_status"),
            final_dirty_status=payload.get("final_dirty_status"),
            finalized_at=payload.get("finalized_at"),
            review_status=payload.get("review_status"),
            reviewed_at=payload.get("reviewed_at"),
            reviewer=payload.get("reviewer"),
            current_review_id=payload.get("current_review_id"),
            current_review_digest=payload.get("current_review_digest"),
            acknowledged_review_id=payload.get("acknowledged_review_id"),
            acknowledged_digest=payload.get("acknowledged_digest"),
            acknowledged_at=payload.get("acknowledged_at"),
            integrated_at=payload.get("integrated_at"),
            integrated_review_id=payload.get("integrated_review_id"),
            integrated_digest=payload.get("integrated_digest"),
            integration_transaction_id=payload.get("integration_transaction_id"),
            discard_expected_oid=payload.get("discard_expected_oid"),
            discard_state=payload.get("discard_state"),
        )
    except (KeyError, TypeError) as exc:
        raise WorktreeError("worktree metadata has an invalid record") from exc
    if record.owner_uid != os.getuid():
        raise WorktreeError("worktree metadata owner does not match this user")
    if version == _LEGACY_FORMAT_VERSION:
        record = replace(
            record,
            lifecycle="active",
            final_status=None,
            final_dirty_status=None,
            finalized_at=None,
            review_status=None,
            reviewed_at=None,
            reviewer=None,
            current_review_id=None,
            current_review_digest=None,
            acknowledged_review_id=None,
            acknowledged_digest=None,
            acknowledged_at=None,
            integrated_at=None,
            integrated_review_id=None,
            integrated_digest=None,
            integration_transaction_id=None,
        )
    elif (
        not isinstance(record.lifecycle, str)
        or record.lifecycle not in _LIFECYCLES
        or (record.final_status is not None and not isinstance(record.final_status, str))
        or (
            record.final_dirty_status is not None
            and not isinstance(record.final_dirty_status, str)
        )
        or (record.finalized_at is not None and not isinstance(record.finalized_at, str))
        or (record.review_status is not None and not isinstance(record.review_status, str))
        or (record.reviewed_at is not None and not isinstance(record.reviewed_at, str))
        or (record.reviewer is not None and not isinstance(record.reviewer, str))
        or (record.current_review_id is not None and not isinstance(record.current_review_id, str))
        or (record.current_review_digest is not None and not isinstance(record.current_review_digest, str))
        or (record.acknowledged_review_id is not None and not isinstance(record.acknowledged_review_id, str))
        or (record.acknowledged_digest is not None and not isinstance(record.acknowledged_digest, str))
        or (record.acknowledged_at is not None and not isinstance(record.acknowledged_at, str))
        or (record.integrated_at is not None and not isinstance(record.integrated_at, str))
        or (record.integrated_review_id is not None and not isinstance(record.integrated_review_id, str))
        or (record.integrated_digest is not None and not isinstance(record.integrated_digest, str))
        or (record.integration_transaction_id is not None and not isinstance(record.integration_transaction_id, str))
        or (record.discard_expected_oid is not None and not isinstance(record.discard_expected_oid, str))
        or (record.discard_state is not None and record.discard_state not in {"prepared", "worktree_removed", "branch_removed", "complete"})
    ):
        raise WorktreeError("worktree lifecycle metadata is invalid")
    if (record.discard_expected_oid is None) != (record.discard_state is None):
        raise WorktreeError("worktree discard journal metadata is invalid")
    if record.discard_expected_oid is not None and not re.fullmatch(r"[0-9a-fA-F]{40,64}", record.discard_expected_oid):
        raise WorktreeError("worktree discard branch object id is invalid")
    if record.lifecycle in {"cleanup_pending", "discarded"} and record.discard_state is None:
        raise WorktreeError("worktree cleanup lifecycle is missing its discard journal")
    if record.discard_state == "complete" and record.lifecycle != "discarded":
        raise WorktreeError("completed discard journal has an invalid lifecycle")
    if (
        (record.current_review_id is None) != (record.current_review_digest is None)
        or (record.acknowledged_review_id is None) != (record.acknowledged_digest is None)
        or (record.acknowledged_review_id is None) != (record.acknowledged_at is None)
        or (record.current_review_id is not None and not _REVIEW_ID_PATTERN.fullmatch(record.current_review_id))
        or (record.current_review_digest is not None and not _REVIEW_DIGEST_PATTERN.fullmatch(record.current_review_digest))
        or (record.acknowledged_review_id is not None and not _REVIEW_ID_PATTERN.fullmatch(record.acknowledged_review_id))
        or (record.acknowledged_digest is not None and not _REVIEW_DIGEST_PATTERN.fullmatch(record.acknowledged_digest))
        or (
            record.acknowledged_review_id is not None
            and (
                record.acknowledged_review_id != record.current_review_id
                or record.acknowledged_digest != record.current_review_digest
            )
        )
    ):
        raise WorktreeError("worktree review acknowledgment metadata is invalid")
    if record.finalized_at is not None:
        try:
            datetime.fromisoformat(record.finalized_at)
        except ValueError as exc:
            raise WorktreeError("worktree finalization time is invalid") from exc
    if record.acknowledged_at is not None:
        try:
            datetime.fromisoformat(record.acknowledged_at)
        except ValueError as exc:
            raise WorktreeError("worktree acknowledgment time is invalid") from exc
    integration_values = (
        record.integrated_at,
        record.integrated_review_id,
        record.integrated_digest,
        record.integration_transaction_id,
    )
    if record.lifecycle == "integrated" and not any(
        value is not None for value in integration_values
    ):
        raise WorktreeError("integrated worktree is missing integration metadata")
    if any(value is not None for value in integration_values):
        if (
            any(value is None for value in integration_values)
            or not _REVIEW_ID_PATTERN.fullmatch(record.integrated_review_id or "")
            or not _REVIEW_DIGEST_PATTERN.fullmatch(record.integrated_digest or "")
            or not _REVIEW_ID_PATTERN.fullmatch(record.integration_transaction_id or "")
            or record.lifecycle not in {"integrated", "cleanup_pending", "discarded"}
            or record.integrated_review_id != record.current_review_id
            or record.integrated_digest != record.current_review_digest
            or record.integrated_review_id != record.acknowledged_review_id
            or record.integrated_digest != record.acknowledged_digest
        ):
            raise WorktreeError("worktree integration metadata is invalid")
        try:
            datetime.fromisoformat(record.integrated_at or "")
        except ValueError as exc:
            raise WorktreeError("worktree integration time is invalid") from exc
    if not isinstance(record.base_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", record.base_commit):
        raise WorktreeError("worktree metadata base commit is invalid")
    if not isinstance(record.branch, str) or not record.branch.startswith(_BRANCH_PREFIX):
        raise WorktreeError("worktree metadata branch is invalid")
    if not record.parent_workspace.is_absolute() or not record.path.is_absolute():
        raise WorktreeError("worktree metadata paths must be absolute")
    try:
        datetime.fromisoformat(record.created_at)
    except (TypeError, ValueError) as exc:
        raise WorktreeError("worktree metadata creation time is invalid") from exc
    return record


def _registered_worktrees(parent: Path) -> dict[Path, str | None]:
    output = _git(parent, "worktree", "list", "--porcelain")
    registered: dict[Path, str | None] = {}
    current_path: Path | None = None
    current_branch: str | None = None
    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            current_path = Path(line[9:]).resolve(strict=False)
            current_branch = None
        elif line.startswith("branch "):
            current_branch = line[7:]
        elif not line and current_path is not None:
            registered[current_path] = current_branch
            current_path = None
            current_branch = None
    return registered


def _verify(record: WorktreeRecord, root: Path, key: bytes) -> WorktreeRecord:
    if record.lifecycle in {"cleanup_pending", "discarded"}:
        return _verify_cleanup_state(record, root)
    parent = record.parent_workspace.resolve(strict=True)
    top_level = _checkout_root(parent)
    if top_level != parent:
        raise WorktreeError("recorded parent is no longer the top-level Git checkout")
    canonical_root = root.resolve(strict=True)
    if (
        canonical_root == parent
        or parent in canonical_root.parents
        or canonical_root in parent.parents
    ):
        raise WorktreeError("worktree service root must remain outside and separate from the parent checkout")
    canonical_path = record.path.resolve(strict=False)
    expected_parent = canonical_root / "worktrees"
    if canonical_path.parent != expected_parent or record.path != canonical_path:
        raise WorktreeError("recorded worktree path is outside its owned worktree directory")
    if record.path.is_symlink() or not record.path.is_dir():
        raise WorktreeError("owned worktree path is missing or is a symlink")
    common_parent = Path(_git(parent, "rev-parse", "--git-common-dir"))
    if not common_parent.is_absolute():
        common_parent = (parent / common_parent).resolve(strict=True)
    else:
        common_parent = common_parent.resolve(strict=True)
    child_top = Path(_git(record.path, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if child_top != record.path:
        raise WorktreeError("owned worktree resolves to a different checkout path")
    common_child = Path(_git(record.path, "rev-parse", "--git-common-dir"))
    if not common_child.is_absolute():
        common_child = (record.path / common_child).resolve(strict=True)
    else:
        common_child = common_child.resolve(strict=True)
    if common_child != common_parent:
        raise WorktreeError("owned checkout belongs to a different Git repository")
    branch_ref = _git(record.path, "symbolic-ref", "--quiet", "HEAD", check=False)
    if branch_ref != f"refs/heads/{record.branch.removeprefix('refs/heads/')}":
        raise WorktreeError("owned checkout is attached to a different branch")
    head = _git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
    branch_name = record.branch.removeprefix("refs/heads/")
    branch_head = _git(parent, "rev-parse", "--verify", f"refs/heads/{branch_name}^{{commit}}")
    if head != branch_head:
        raise WorktreeError("owned checkout HEAD does not match its owned branch")
    _git(parent, "cat-file", "-e", f"{record.base_commit}^{{commit}}")
    ancestor = subprocess.run(
        _git_argv("merge-base", "--is-ancestor", record.base_commit, head),
        cwd=parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        env=_git_environment(),
    )
    if ancestor.returncode != 0:
        raise WorktreeError("owned branch no longer descends from its recorded base commit")
    registration = _registered_worktrees(parent).get(record.path)
    if registration != f"refs/heads/{record.branch.removeprefix('refs/heads/')}":
        raise WorktreeError("Git does not register this path on its owned branch")
    return record


def _verify_cleanup_state(record: WorktreeRecord, root: Path) -> WorktreeRecord:
    """Validate a journaled discard without trusting a client-supplied path."""
    if record.discard_expected_oid is None or record.discard_state is None:
        raise WorktreeError("worktree cleanup journal is incomplete")
    parent = record.parent_workspace.resolve(strict=True)
    if _checkout_root(parent) != parent:
        raise WorktreeError("recorded parent is no longer the top-level Git checkout")
    canonical_root = root.resolve(strict=True)
    if canonical_root == parent or parent in canonical_root.parents or canonical_root in parent.parents:
        raise WorktreeError("worktree service root must remain outside and separate from the parent checkout")
    expected_path = canonical_root / "worktrees" / record.path.name
    if record.path != expected_path or record.path.parent != canonical_root / "worktrees":
        raise WorktreeError("recorded worktree path is outside its owned worktree directory")
    if record.path.is_symlink():
        raise WorktreeError("owned worktree path is a symlink")
    expected_ref = f"refs/heads/{record.branch.removeprefix('refs/heads/')}"
    if not record.branch.startswith(_BRANCH_PREFIX):
        raise WorktreeError("worktree metadata branch is invalid")
    probe = subprocess.run(
        _git_argv("check-ref-format", expected_ref), cwd=parent,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, env=_git_environment(),
    )
    if probe.returncode:
        raise WorktreeError("worktree metadata branch ref is invalid")
    registrations = _registered_worktrees(parent)
    branch_oid = _git(parent, "rev-parse", "--verify", expected_ref, check=False)
    if record.path.exists():
        if record.lifecycle == "discarded":
            raise WorktreeError("discarded worktree path unexpectedly exists")
        if registrations.get(record.path) != expected_ref or branch_oid != record.discard_expected_oid:
            raise WorktreeError("pending worktree cleanup no longer matches its journal")
        parent_common = Path(_git(parent, "rev-parse", "--git-common-dir"))
        if not parent_common.is_absolute():
            parent_common = (parent / parent_common).resolve(strict=True)
        else:
            parent_common = parent_common.resolve(strict=True)
        child_common = Path(_git(record.path, "rev-parse", "--git-common-dir"))
        if not child_common.is_absolute():
            child_common = (record.path / child_common).resolve(strict=True)
        else:
            child_common = child_common.resolve(strict=True)
        child_head = _git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
        if (
            child_common != parent_common
            or _git(record.path, "symbolic-ref", "--quiet", "HEAD") != expected_ref
            or child_head != record.discard_expected_oid
        ):
            raise WorktreeError("pending worktree cleanup no longer matches its authenticated checkout")
        _git(parent, "cat-file", "-e", f"{record.base_commit}^{{commit}}")
        ancestor = subprocess.run(
            _git_argv("merge-base", "--is-ancestor", record.base_commit, child_head),
            cwd=parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, env=_git_environment(),
        )
        if ancestor.returncode:
            raise WorktreeError("owned branch no longer descends from its recorded base commit")
    else:
        if record.path in registrations:
            raise WorktreeError("worktree path is missing but remains registered with Git")
        if branch_oid:
            _git(parent, "cat-file", "-e", f"{record.base_commit}^{{commit}}")
            _git(parent, "cat-file", "-e", f"{record.discard_expected_oid}^{{commit}}")
            ancestor = subprocess.run(
                _git_argv("merge-base", "--is-ancestor", record.base_commit, record.discard_expected_oid),
                cwd=parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False, env=_git_environment(),
            )
            if ancestor.returncode:
                raise WorktreeError("pending branch no longer descends from its recorded base commit")
        if (
            branch_oid
            and branch_oid != record.discard_expected_oid
            and record.discard_state in {"branch_removed", "complete"}
        ):
            raise WorktreeError("worktree branch ref changed during cleanup")
        if record.lifecycle == "discarded" and branch_oid:
            raise WorktreeError("discarded worktree branch ref unexpectedly exists")
        if record.discard_state in {"branch_removed", "complete"} and branch_oid:
            raise WorktreeError("discard journal says the branch was removed but it still exists")
    return record


def _cleanup_created(parent: Path, path: Path, branch: str, base_commit: str) -> None:
    """Remove only a just-created matching worktree; never follow a foreign path."""
    try:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            return
        expected_ref = f"refs/heads/{branch.removeprefix('refs/heads/')}"
        registrations = _registered_worktrees(parent)
        if path.is_dir():
            resolved_path = path.resolve(strict=True)
            if registrations.get(resolved_path) != expected_ref:
                return
            common_parent = Path(_git(parent, "rev-parse", "--git-common-dir"))
            if not common_parent.is_absolute():
                common_parent = (parent / common_parent).resolve(strict=True)
            child_common = Path(_git(path, "rev-parse", "--git-common-dir"))
            if not child_common.is_absolute():
                child_common = (path / child_common).resolve(strict=True)
            if child_common.resolve(strict=True) != common_parent.resolve(strict=True):
                return
        elif any(ref == expected_ref for ref in registrations.values()):
            return
        branch_head = _git(
            parent,
            "rev-parse",
            "--verify",
            f"refs/heads/{branch.removeprefix('refs/heads/')}^{{commit}}",
            check=False,
        )
        if branch_head != base_commit:
            return
        if path.is_dir():
            _git(parent, "worktree", "remove", "--force", str(path), check=False)
        elif path in registrations:
            return
        if any(ref == expected_ref for ref in _registered_worktrees(parent).values()):
            return
        _git(parent, "branch", "-D", branch.removeprefix("refs/heads/"), check=False)
    except (OSError, WorktreeError):
        # Keep the original creation error. Ownership checks above deliberately
        # make cleanup conservative if Git's state is ambiguous.
        return


def create(
    parent_workspace: str | Path,
    child_id: str,
    *,
    root: str | Path,
) -> WorktreeRecord:
    """Create and persist an isolated branch/worktree from the parent's HEAD.

    The parent must be a clean top-level checkout. ``root`` is daemon-owned
    storage outside that checkout and holds worktrees, authenticated metadata,
    and the process-safe creation lock.
    """
    child_id = _validate_child_id(child_id)
    parent = _checkout_root(parent_workspace)
    # Check before creating the service root so a refused dirty checkout has no
    # side effects outside the checkout either. Recheck under the lock below to
    # close the gap with other calls through this service.
    status = _git(parent, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise WorktreeError("parent Git checkout must be clean, including untracked files")
    base_commit = _git(parent, "rev-parse", "--verify", "HEAD^{commit}")
    service_root, records_dir, worktrees_dir = _layout(root, parent, create=True)
    with _locked(service_root):
        key = _load_key(service_root, create=True)
        record_path = records_dir / _record_name(child_id)
        if record_path.exists() or record_path.is_symlink():
            raise WorktreeError(f"a worktree record already exists for child {child_id!r}")
        status = _git(parent, "status", "--porcelain", "--untracked-files=all")
        if status:
            raise WorktreeError("parent Git checkout must be clean, including untracked files")
        base_commit = _git(parent, "rev-parse", "--verify", "HEAD^{commit}")

        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", child_id).strip("-._")[:48] or "child"
        candidate: tuple[str, Path] | None = None
        for _ in range(_COLLISION_ATTEMPTS):
            token = secrets.token_hex(_TOKEN_BYTES)
            slug = f"{safe_id}-{token}"
            branch = f"{_BRANCH_PREFIX}{slug}"
            path = worktrees_dir / slug
            # Probe exit status directly: show-ref's quiet mode has no output.
            probe = subprocess.run(
                _git_argv("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                cwd=parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                env=_git_environment(),
            )
            if path.exists() or path.is_symlink() or probe.returncode == 0:
                continue
            if probe.returncode != 1:
                raise WorktreeError("could not safely check whether a worktree branch exists")
            candidate = branch, path
            break
        if candidate is None:
            raise WorktreeError("could not allocate a collision-free worktree path and branch")

        branch, path = candidate
        worktree_added = False
        record_published = False
        try:
            _git(
                parent,
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                "HEAD",
                observational=False,
            )
            worktree_added = True
            record = WorktreeRecord(
                child_id=child_id,
                parent_workspace=parent,
                base_commit=base_commit,
                branch=branch,
                path=path,
                owner_uid=os.getuid(),
                created_at=datetime.now(UTC).isoformat(),
            )
            _write_record(record_path, record, key)
            record_published = True
            return _verify(record, service_root, key)
        except BaseException:
            if worktree_added:
                _cleanup_created(parent, path, branch, base_commit)
            if record_published:
                with contextlib.suppress(OSError):
                    record_path.unlink()
            raise


def get(child_id: str, *, root: str | Path) -> WorktreeRecord:
    """Load and verify the owned worktree record for ``child_id``."""
    return _get(child_id, root=root)


def _service_layout(root: str | Path) -> tuple[Path, Path]:
    root_path = Path(root).expanduser()
    if root_path.is_symlink():
        raise WorktreeError("worktree root cannot be a symlink")
    service_root = root_path.resolve(strict=False)
    _secure_directory(service_root, create=False)
    _secure_directory(service_root / "records", create=False)
    _secure_directory(service_root / "worktrees", create=False)
    return service_root, service_root / "records"


def _legacy_finalized(
    record: WorktreeRecord,
    service_root: Path,
    key: bytes,
    *,
    runtime_ownership: Callable[[str], bool | None] | None,
) -> WorktreeRecord:
    """Finalize legacy metadata only on an explicit no-live-owner answer."""
    if runtime_ownership is None:
        return record
    try:
        live = runtime_ownership(record.child_id)
    except Exception as exc:
        raise WorktreeError("could not determine legacy worktree runtime ownership") from exc
    if live is not False:
        return record
    status = _git(record.path, "status", "--porcelain", "--untracked-files=all")
    finalized = replace(
        record,
        lifecycle="finalized",
        final_status="unknown",
        final_dirty_status=status,
        finalized_at=datetime.now(UTC).isoformat(),
    )
    _replace_record(service_root / "records" / _record_name(record.child_id), finalized, key)
    return finalized


def _get(
    child_id: str,
    *,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None = None,
) -> WorktreeRecord:
    child_id = _validate_child_id(child_id)
    service_root, records_dir = _service_layout(root)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        record_path = records_dir / _record_name(child_id)
        if not record_path.exists():
            raise WorktreeError(f"no owned worktree record for child {child_id!r}")
        record = _read_record(record_path, child_id, key)
        record = _verify(record, service_root, key)
        if _record_version(record_path) == _LEGACY_FORMAT_VERSION:
            record = _legacy_finalized(
                record, service_root, key, runtime_ownership=runtime_ownership
            )
        return record


def inspect(child_id: str, *, root: str | Path) -> WorktreeRecord:
    """Verify ownership and live Git state, returning the durable record."""
    record = get(child_id, root=root)
    status = _git(record.path, "status", "--porcelain", "--untracked-files=all")
    return replace(record, dirty_status=status)


def list(*, root: str | Path) -> tuple[WorktreeRecord, ...]:
    """List all owned worktrees, verifying each record and checkout."""
    return _list(root=root)


def _record_version(path: Path) -> int:
    try:
        envelope = json.loads(_read_metadata_bytes(path))
        version = envelope["record"]["version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise TypeError("record version must be an integer")
        return version
    except (OSError, ValueError, KeyError, TypeError, WorktreeError) as exc:
        raise WorktreeError(f"cannot determine worktree record version: {exc}") from exc


def _list(
    *,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None = None,
) -> tuple[WorktreeRecord, ...]:
    service_root, records_dir = _service_layout(root)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        results = []
        for path in sorted(records_dir.glob("*.json")):
            try:
                envelope = json.loads(_read_metadata_bytes(path))
                child_id = envelope["record"]["child_id"]
            except (OSError, ValueError, KeyError, TypeError, WorktreeError) as exc:
                raise WorktreeError(f"invalid worktree record file {path.name}: {exc}") from exc
            child_id = _validate_child_id(child_id)
            if path.name != _record_name(child_id):
                raise WorktreeError("worktree record filename does not match its child id")
            record = _read_record(path, child_id, key)
            record = _verify(record, service_root, key)
            if _record_version(path) == _LEGACY_FORMAT_VERSION:
                record = _legacy_finalized(
                    record, service_root, key, runtime_ownership=runtime_ownership
                )
            results.append(record)
        return tuple(results)


def _inspect(
    child_id: str,
    *,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None = None,
) -> WorktreeRecord:
    record = _get(child_id, root=root, runtime_ownership=runtime_ownership)
    if record.lifecycle == "discarded":  # the checkout is gone; listing must keep working
        return record
    status = _git(record.path, "status", "--porcelain", "--untracked-files=all")
    return replace(record, dirty_status=status)


def _outcome_status(outcome: object) -> str:
    if isinstance(outcome, Mapping):
        status = outcome.get("status")
    else:
        status = getattr(outcome, "status", None)
    if not isinstance(status, str) or not status.strip():
        raise WorktreeError("outcome must provide a non-empty status")
    return status.strip()


def _mark_finished(
    child_id: str,
    outcome: object,
    *,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None,
) -> WorktreeRecord:
    child_id = _validate_child_id(child_id)
    final_status = _outcome_status(outcome)
    service_root, records_dir = _service_layout(root)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        path = records_dir / _record_name(child_id)
        if not path.exists():
            raise WorktreeError(f"no owned worktree record for child {child_id!r}")
        record = _read_record(path, child_id, key)
        record = _verify(record, service_root, key)
        if _record_version(path) == _LEGACY_FORMAT_VERSION:
            if runtime_ownership is None:
                raise WorktreeError("legacy worktree runtime ownership is unknown")
            try:
                live = runtime_ownership(child_id)
            except Exception as exc:
                raise WorktreeError("could not determine legacy worktree runtime ownership") from exc
            if live is not False:
                raise WorktreeError("legacy worktree cannot be finalized while runtime ownership is live or unknown")
        if record.lifecycle == "finalized":
            return record
        if record.lifecycle != "active":
            raise WorktreeError(f"cannot finalize worktree in lifecycle {record.lifecycle!r}")
        dirty_status = _git(record.path, "status", "--porcelain", "--untracked-files=all")
        finalized = replace(
            record,
            lifecycle="finalized",
            final_status=final_status,
            final_dirty_status=dirty_status,
            finalized_at=datetime.now(UTC).isoformat(),
        )
        _replace_record(path, finalized, key)
        # Confirm that the newly published authenticated state still belongs to
        # this checkout/root before returning it to the caller.
        return _verify(_read_record(path, child_id, key), service_root, key)


def _review_artifact_root(service_root: Path) -> Path:
    """Return the fixed private review directory beneath the service root."""
    from .worktree_review import ReviewError

    path = service_root / "reviews"
    if path.is_symlink():
        raise ReviewError("review artifact root cannot be a symlink")
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReviewError("review artifact root is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ReviewError("review artifact root must be private and owned by this user")
    return path


def _review(
    child_id: str,
    *,
    review_id: str | None,
    cursor: int,
    limit: int,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None,
) -> WorktreeReviewPage:
    from .worktree_review import ReviewError, build_review, load_review

    child_id = _validate_child_id(child_id)
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ReviewError("review cursor must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _REVIEW_PAGE_LIMIT:
        raise ReviewError(f"review page limit must be between 1 and {_REVIEW_PAGE_LIMIT}")
    if review_id is not None and (
        not isinstance(review_id, str) or not _REVIEW_ID_PATTERN.fullmatch(review_id)
    ):
        raise ReviewError("invalid review id")

    service_root, records_dir = _service_layout(root)
    record_path = records_dir / _record_name(child_id)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        if not record_path.exists():
            raise WorktreeError(f"no owned worktree record for child {child_id!r}")
        record = _read_record(record_path, child_id, key)
        record = _verify(record, service_root, key)
        if _record_version(record_path) == _LEGACY_FORMAT_VERSION:
            record = _legacy_finalized(
                record, service_root, key, runtime_ownership=runtime_ownership
            )
        if record.lifecycle != "finalized":
            raise ReviewError("worktree review requires a finalized child")

        artifact_root = _review_artifact_root(service_root)
        artifact = (
            build_review(record, artifact_root)
            if review_id is None
            else load_review(record, artifact_root, review_id)
        )
        if cursor > len(artifact.diff_pages):
            raise ReviewError("review cursor is beyond the final page")
        next_cursor = min(cursor + limit, len(artifact.diff_pages))
        selected = artifact.diff_pages[cursor:next_cursor]
        is_new_review = (
            record.current_review_id != artifact.review_id
            or record.current_review_digest != artifact.digest
        )
        updated = replace(
            record,
            current_review_id=artifact.review_id,
            current_review_digest=artifact.digest,
            acknowledged_review_id=None if is_new_review else record.acknowledged_review_id,
            acknowledged_digest=None if is_new_review else record.acknowledged_digest,
            acknowledged_at=None if is_new_review else record.acknowledged_at,
        )
        _replace_record(record_path, updated, key)
        return WorktreeReviewPage(
            review_id=artifact.review_id,
            digest=artifact.digest,
            manifest=artifact.manifest,
            diff_pages=selected,
            cursor=cursor,
            next_cursor=next_cursor if next_cursor < len(artifact.diff_pages) else None,
            total_pages=len(artifact.diff_pages),
            artifact_path=artifact.artifact_path,
        )


def _acknowledge(
    child_id: str,
    review_id: str,
    digest: str,
    *,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None,
) -> WorktreeRecord:
    from .worktree_review import ReviewError, load_review

    child_id = _validate_child_id(child_id)
    if not isinstance(review_id, str) or not _REVIEW_ID_PATTERN.fullmatch(review_id):
        raise ReviewError("invalid review id")
    if not isinstance(digest, str) or not _REVIEW_DIGEST_PATTERN.fullmatch(digest):
        raise ReviewError("invalid review digest")

    service_root, records_dir = _service_layout(root)
    record_path = records_dir / _record_name(child_id)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        if not record_path.exists():
            raise WorktreeError(f"no owned worktree record for child {child_id!r}")
        record = _read_record(record_path, child_id, key)
        record = _verify(record, service_root, key)
        if _record_version(record_path) == _LEGACY_FORMAT_VERSION:
            record = _legacy_finalized(
                record, service_root, key, runtime_ownership=runtime_ownership
            )
        if record.lifecycle != "finalized":
            raise ReviewError("cannot acknowledge a review while the child is active")
        if record.current_review_id != review_id or record.current_review_digest is None:
            raise ReviewError("review is not the child's current review")

        artifact_root = _review_artifact_root(service_root)
        artifact = load_review(record, artifact_root, review_id)
        if not hmac.compare_digest(artifact.digest, digest):
            raise ReviewError("review digest does not match the current artifact")
        if not hmac.compare_digest(record.current_review_digest, digest):
            raise ReviewError("review digest does not match the authenticated current review")

        acknowledged = replace(
            record,
            acknowledged_review_id=review_id,
            acknowledged_digest=digest,
            acknowledged_at=datetime.now(UTC).isoformat(),
        )
        _replace_record(record_path, acknowledged, key)
        return _verify(_read_record(record_path, child_id, key), service_root, key)


def _integrate(
    child_id: str,
    review_id: str,
    digest: str,
    *,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None,
    cancel: object | None,
    transaction_root: str | Path | None = None,
    expected_record: WorktreeRecord | None = None,
    persist_lifecycle: bool = True,
):
    from .worktree_integrate import IntegrationResult, _integrate_authenticated
    from .worktree_review import ReviewError, load_review

    child_id = _validate_child_id(child_id)
    if not isinstance(review_id, str) or not _REVIEW_ID_PATTERN.fullmatch(review_id):
        raise ReviewError("invalid review id")
    if not isinstance(digest, str) or not _REVIEW_DIGEST_PATTERN.fullmatch(digest):
        raise ReviewError("invalid review digest")

    service_root, records_dir = _service_layout(root)
    record_path = records_dir / _record_name(child_id)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        if not record_path.exists():
            raise WorktreeError(f"no owned worktree record for child {child_id!r}")
        record = _verify(_read_record(record_path, child_id, key), service_root, key)
        if expected_record is not None and (
            expected_record.parent_workspace.resolve(strict=True) != record.parent_workspace.resolve(strict=True)
            or expected_record.path.resolve(strict=True) != record.path.resolve(strict=True)
            or expected_record.base_commit != record.base_commit
            or expected_record.lifecycle != record.lifecycle
            or expected_record.current_review_id != record.current_review_id
            or expected_record.current_review_digest != record.current_review_digest
            or expected_record.acknowledged_review_id != record.acknowledged_review_id
            or expected_record.acknowledged_digest != record.acknowledged_digest
        ):
            raise WorktreeError("supplied worktree record is not the authenticated current acknowledged review record")
        if _record_version(record_path) == _LEGACY_FORMAT_VERSION:
            record = _legacy_finalized(
                record, service_root, key, runtime_ownership=runtime_ownership
            )
        if record.lifecycle != "finalized":
            raise WorktreeError("cannot integrate while the child is active")
        if (
            record.current_review_id != review_id
            or record.current_review_digest != digest
            or record.acknowledged_review_id != review_id
            or record.acknowledged_digest != digest
        ):
            raise WorktreeError("integration requires the current explicitly acknowledged review and digest")
        if record.integrated_review_id == review_id and record.integrated_digest == digest:
            return IntegrationResult(
                "integrated",
                record.integration_transaction_id,
                (),
            )

        from .worktree_integrate import _integrated_journals

        completed = [
            (directory, journal)
            for directory, journal in _integrated_journals(
                service_root / "transactions", record
            )
            if journal.get("review_id") == review_id
            and journal.get("review_digest") == digest
        ]
        if completed:
            if len(completed) != 1:
                return IntegrationResult(
                    "recovery_required",
                    error="multiple completed integration journals require manual recovery",
                    recovery_required=tuple(directory.name for directory, _ in completed),
                )
            directory, journal = completed[0]
            try:
                integrated = replace(
                    record,
                    lifecycle="integrated",
                    integrated_at=datetime.now(UTC).isoformat(),
                    integrated_review_id=review_id,
                    integrated_digest=digest,
                    integration_transaction_id=journal.get("transaction_id", directory.name),
                )
                _replace_record(record_path, integrated, key)
                _verify(_read_record(record_path, child_id, key), service_root, key)
            except (OSError, WorktreeError) as exc:
                return IntegrationResult(
                    "recovery_required",
                    journal.get("transaction_id", directory.name),
                    tuple(
                        entry.get("path", "<invalid-path>")
                        for entry in journal.get("entries", [])
                        if isinstance(entry, dict)
                    ),
                    f"integration journal is complete but lifecycle metadata needs recovery: {exc}",
                )
            return IntegrationResult(
                "integrated",
                journal.get("transaction_id", directory.name),
                tuple(
                    entry["path"]
                    for entry in journal.get("entries", [])
                    if isinstance(entry, dict) and isinstance(entry.get("path"), str)
                ),
            )

        artifact_root = _review_artifact_root(service_root)
        artifact = load_review(record, artifact_root, review_id)
        if not hmac.compare_digest(artifact.digest, digest):
            raise ReviewError("review digest does not match the current artifact")

        def validate_current() -> None:
            current = _verify(_read_record(record_path, child_id, key), service_root, key)
            if (
                current.lifecycle != "finalized"
                or current.current_review_id != review_id
                or current.current_review_digest != digest
                or current.acknowledged_review_id != review_id
                or current.acknowledged_digest != digest
            ):
                raise WorktreeError("current child review acknowledgment changed during integration")

        result = _integrate_authenticated(
            record,
            artifact,
            transaction_root or service_root / "transactions",
            validate_current=validate_current,
            cancel=cancel,
        )
        if result.status != "integrated":
            return result
        if not persist_lifecycle:
            return result
        integrated = replace(
            record,
            lifecycle="integrated",
            integrated_at=datetime.now(UTC).isoformat(),
            integrated_review_id=review_id,
            integrated_digest=digest,
            integration_transaction_id=result.transaction_id,
        )
        try:
            _replace_record(record_path, integrated, key)
            _verify(_read_record(record_path, child_id, key), service_root, key)
        except Exception as exc:  # noqa: BLE001 - durable journal needs explicit recovery on any publication failure
            return IntegrationResult(
                "recovery_required",
                result.transaction_id,
                result.changed_paths,
                f"integration journal is complete but lifecycle metadata needs recovery: {exc}",
                result.changed_paths,
            )
        return IntegrationResult(
            "integrated",
            result.transaction_id,
            result.changed_paths,
        )


def _recover_pending(
    child_id: str,
    *,
    root: str | Path,
    runtime_ownership: Callable[[str], bool | None] | None,
) -> WorktreeRecord:
    from .worktree_integrate import _integrated_journals

    child_id = _validate_child_id(child_id)
    service_root, records_dir = _service_layout(root)
    record_path = records_dir / _record_name(child_id)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        if not record_path.exists():
            raise WorktreeError(f"no owned worktree record for child {child_id!r}")
        record = _verify(_read_record(record_path, child_id, key), service_root, key)
        if record.lifecycle == "integrated":
            return record
        if record.lifecycle != "finalized":
            raise WorktreeError("cannot recover integration while the child is active")
        transactions = service_root / "transactions"
        journals = _integrated_journals(transactions, record)
        matching = [
            (directory, journal)
            for directory, journal in journals
            if journal.get("review_id") == record.current_review_id
            and journal.get("review_digest") == record.current_review_digest
            and journal.get("review_id") == record.acknowledged_review_id
            and journal.get("review_digest") == record.acknowledged_digest
        ]
        if not matching:
            raise WorktreeError("no completed integration journal requires lifecycle recovery")
        if len(matching) != 1:
            raise WorktreeError("multiple completed integration journals require manual recovery")
        directory, journal = matching[0]
        if not record.current_review_id or not record.current_review_digest:
            raise WorktreeError("completed integration journal has no current acknowledged review")
        integrated = replace(
            record,
            lifecycle="integrated",
            integrated_at=datetime.now(UTC).isoformat(),
            integrated_review_id=record.current_review_id,
            integrated_digest=record.current_review_digest,
            integration_transaction_id=journal.get("transaction_id", directory.name),
        )
        _replace_record(record_path, integrated, key)
        return _verify(_read_record(record_path, child_id, key), service_root, key)


def _check_cancel(cancel: object | None) -> None:
    if cancel is None:
        return
    method = getattr(cancel, "raise_if_cancelled", None)
    if callable(method):
        method()
    elif callable(cancel):
        cancel()


def _discard(
    child_id: str,
    *,
    root: str | Path,
    force: bool,
    acknowledged_review_id: str | None,
    runtime_ownership: Callable[[str], bool | None] | None,
    cancel: object | None,
) -> WorktreeRecord:
    from .worktree_review import load_review

    child_id = _validate_child_id(child_id)
    if not isinstance(force, bool):
        raise WorktreeError("force must be an explicit boolean")
    service_root, records_dir = _service_layout(root)
    record_path = records_dir / _record_name(child_id)
    with _locked(service_root):
        key = _load_key(service_root, create=False)
        if not record_path.exists():
            raise WorktreeError(f"no owned worktree record for child {child_id!r}")
        record = _read_record(record_path, child_id, key)

        # A prior invocation has crossed the point of no return. Never repeat
        # caller policy checks; reconcile the exact journaled Git state instead.
        if record.lifecycle == "cleanup_pending":
            return _finish_discard(record, record_path, key, service_root)
        if record.lifecycle == "discarded":
            _verify(record, service_root, key)
            return record

        record = _verify(record, service_root, key)
        if runtime_ownership is not None:
            try:
                live = runtime_ownership(child_id)
            except Exception as exc:
                raise WorktreeError("could not determine child runtime ownership") from exc
            if live is not False:
                raise WorktreeError("cannot discard while child runtime ownership is live or unknown")
        if record.lifecycle == "active":
            raise WorktreeError("cannot discard an active child")

        expected_ref = f"refs/heads/{record.branch.removeprefix('refs/heads/')}"
        branch_oid = _git(record.parent_workspace, "rev-parse", "--verify", expected_ref)
        if branch_oid != _git(record.path, "rev-parse", "--verify", "HEAD^{commit}"):
            raise WorktreeError("owned branch ref changed from the child checkout")
        _git(record.parent_workspace, "cat-file", "-e", f"{record.base_commit}^{{commit}}")

        if not force:
            if acknowledged_review_id is not None and (
                not isinstance(acknowledged_review_id, str)
                or not _REVIEW_ID_PATTERN.fullmatch(acknowledged_review_id)
            ):
                raise WorktreeError("invalid acknowledged review id")
            if (
                record.lifecycle != "finalized"
                or not acknowledged_review_id
                or acknowledged_review_id != record.current_review_id
                or acknowledged_review_id != record.acknowledged_review_id
                or record.current_review_digest is None
                or record.acknowledged_digest != record.current_review_digest
            ):
                raise WorktreeError("discard requires the current acknowledged review")
            status = _git(
                record.path,
                "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=traditional",
            )
            if status:
                raise WorktreeError("discard refuses a changed or dirty worktree, including ignored files")
            load_review(record, _review_artifact_root(service_root), acknowledged_review_id)

        # This is the point of no return. Cancellation is honored until this
        # authenticated intent is durably published, and ignored afterwards.
        _check_cancel(cancel)
        pending = replace(
            record,
            lifecycle="cleanup_pending",
            discard_expected_oid=branch_oid,
            discard_state="prepared",
        )
        try:
            _replace_record(record_path, pending, key)
        except BaseException:
            # Atomic replacement can become visible before a signal or fsync
            # error is delivered. If so, the durable intent wins and cleanup
            # must be completed rather than reporting a pre-mutation abort.
            published = _read_record(record_path, child_id, key)
            if published.lifecycle != "cleanup_pending" or published.discard_expected_oid != branch_oid:
                raise
            pending = published
        # Cancellation is intentionally not sampled after durable intent.
        return _finish_discard(pending, record_path, key, service_root)


def _finish_discard(
    record: WorktreeRecord,
    record_path: Path,
    key: bytes,
    service_root: Path,
    *,
    retry_interruption: bool = True,
) -> WorktreeRecord:
    """Idempotently complete cleanup after the durable discard intent."""
    parent = record.parent_workspace.resolve(strict=True)
    expected_ref = f"refs/heads/{record.branch.removeprefix('refs/heads/')}"
    expected_oid = record.discard_expected_oid
    assert expected_oid is not None
    failure: BaseException | None = None

    try:
        current = _read_record(record_path, record.child_id, key)
        if current != record:
            record = current
        if record.lifecycle != "cleanup_pending":
            if record.lifecycle == "discarded":
                return _verify(record, service_root, key)
            raise WorktreeError("discard journal lifecycle changed unexpectedly")
        if record.path.is_symlink():
            raise WorktreeError("owned worktree path became a symlink during cleanup")

        if record.path.exists():
            # Reconfirm path, repository registration and ref immediately before
            # the sole destructive Git operation. The path comes only from the
            # authenticated record and must remain exactly under service root.
            _verify_cleanup_state(record, service_root)
            registrations = _registered_worktrees(parent)
            if registrations.get(record.path) != expected_ref:
                raise WorktreeError("Git worktree registration changed during cleanup")
            if _git(parent, "rev-parse", "--verify", expected_ref, check=False) != expected_oid:
                raise WorktreeError("worktree branch ref changed during cleanup")
            _git(parent, "worktree", "remove", "--force", "--", str(record.path), observational=False)

        registrations = _registered_worktrees(parent)
        if record.path.exists() or record.path in registrations:
            raise WorktreeError("Git worktree removal did not complete")
        if record.discard_state == "prepared":
            record = replace(record, discard_state="worktree_removed")
            _replace_record(record_path, record, key)

        current_oid = _git(parent, "rev-parse", "--verify", expected_ref, check=False)
        if current_oid:
            if current_oid != expected_oid:
                raise WorktreeError("worktree branch ref changed; refusing to delete the changed ref")
            _git(parent, "update-ref", "-d", expected_ref, expected_oid, observational=False)
        if _git(parent, "rev-parse", "--verify", expected_ref, check=False):
            raise WorktreeError("exact worktree branch ref was not deleted")
        if record.discard_state != "branch_removed":
            record = replace(record, discard_state="branch_removed")
            _replace_record(record_path, record, key)

        tombstone = replace(record, lifecycle="discarded", discard_state="complete")
        _replace_record(record_path, tombstone, key)
        _fsync_directory(record_path.parent)
        return _verify(_read_record(record_path, record.child_id, key), service_root, key)
    except BaseException as exc:  # noqa: BLE001 - reconcile even on interruption after point of no return
        failure = exc

    # Reconcile observable state after any post-intent error (including an
    # interruption injected after Git completed). Keep the authenticated
    # cleanup_pending record for a bounded retry, never delete by client ID.
    try:
        current = _read_record(record_path, record.child_id, key)
        if current.lifecycle == "discarded":
            return _verify(current, service_root, key)
        registrations = _registered_worktrees(parent)
        path_present = record.path.exists() or record.path.is_symlink()
        branch_oid = _git(parent, "rev-parse", "--verify", expected_ref, check=False)
        state = current.discard_state or "prepared"
        if not path_present and record.path not in registrations:
            state = "worktree_removed"
        if state in {"worktree_removed", "branch_removed"} and not branch_oid:
            state = "branch_removed"
        reconciled = replace(current, lifecycle="cleanup_pending", discard_state=state)
        _replace_record(record_path, reconciled, key)
    except BaseException as reconcile_error:  # noqa: BLE001 - durable cleanup may need recovery
        raise WorktreeError(
            f"discard crossed its point of no return; cleanup state is uncertain: {reconcile_error}"
        ) from failure
    if isinstance(failure, (KeyboardInterrupt, SystemExit)):
        if retry_interruption:
            return _finish_discard(
                reconciled,
                record_path,
                key,
                service_root,
                retry_interruption=False,
            )
        raise failure
    raise WorktreeError(
        f"discard cleanup is pending ({reconciled.discard_state}); retry discard to finish: {failure}"
    ) from failure
