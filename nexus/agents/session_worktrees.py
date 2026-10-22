"""Authenticated worktrees owned by root sessions (worktree-sessions plan 03).

Session records use a distinct v3 envelope and lifecycle. The existing v1/v2
subagent records remain decoded by :mod:`nexus.agents.worktrees` unchanged.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import worktrees as _base

_VERSION = 3
_PREFIX = "session-"
_BRANCH_PREFIX = "nexus/session/"
_LIFECYCLES = frozenset({"pending", "active", "detached", "landed", "removed", "cleanup_pending"})
_GIT_TIMEOUT_S = 60
_MAX_OUTPUT = 1_048_576
_MAX_STATUS_ENTRY = 16_384
_MAX_FILES = 2000
_NEXUS_TRAILER = re.compile(r"(?im)^Nexus-[A-Za-z0-9-]+\s*:")
_TRAILER_LINE = re.compile(r"(?im)^Nexus-[A-Za-z0-9-]+\s*:.*(?:\n|$)")


@dataclass(frozen=True)
class SessionWorktreeRecord:
    """Private authenticated ownership record for one session checkout."""

    child_id: str
    session_id: str
    parent_workspace: Path
    base_ref: str
    base_commit: str
    branch: str
    path: Path
    owner_uid: int
    created_at: float
    updated_at: float
    parent_record: str = ""
    lifecycle: str = "pending"

    @property
    def id(self) -> str:
        return self.child_id

    @property
    def kind(self) -> str:
        return "session"


def session_record_id(session_id: str) -> str:
    _base._validate_child_id(session_id)
    if len(session_id) > 190:
        raise _base.WorktreeError("session id is too long for a worktree record")
    return _PREFIX + session_id


def _git(cwd: Path, *args: str, check: bool = True, observational: bool = True,
         input_text: str | None = None, extra_env: Mapping[str, str] | None = None) -> str:
    argv = _base._git_argv(*args, observational=observational)
    env = {**_base._git_environment(), "GIT_LITERAL_PATHSPECS": "1", **(extra_env or {})}
    stdin_file = None
    process = None
    try:
        if input_text is not None:
            # Avoid a pipe writer that can block if Git exits before consuming input.
            stdin_file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
            stdin_file.write(input_text)
            stdin_file.seek(0)
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, env=env,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        total = 0
        deadline = time.monotonic() + _GIT_TIMEOUT_S
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                for key, _ in selector.select(min(remaining, 0.25)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > _MAX_OUTPUT:
                        raise OverflowError
                    chunks[key.data].append(data)
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except (TimeoutError, OverflowError):
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if total > _MAX_OUTPUT:
                raise _base.WorktreeError("Git output exceeded the session worktree limit")
            raise _base.WorktreeError(f"git {' '.join(args)} timed out")
        finally:
            selector.close()
        stdout = b"".join(chunks["stdout"]).decode("utf-8", "replace")
        stderr = b"".join(chunks["stderr"]).decode("utf-8", "replace")
    except _base.WorktreeError:
        raise
    except OSError as exc:
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise _base.WorktreeError(f"could not run git: {exc}") from exc
    finally:
        if stdin_file is not None:
            stdin_file.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    if check and returncode:
        detail = (stderr.strip() or stdout.strip())[:500]
        raise _base.WorktreeError(f"git {' '.join(args)} failed ({returncode})" + (f": {detail}" if detail else ""))
    return stdout.rstrip("\n")


def status_counts(cwd: str | Path, *, max_entries: int = 10_000) -> dict[str, int]:
    """Count dirty/untracked status entries without buffering a large checkout."""
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or not 1 <= max_entries <= 10_000:
        raise _base.WorktreeError("max_entries must be between 1 and 10000")
    argv = _base._git_argv("status", "--porcelain=v2", "-z", "--untracked-files=all")
    env = {**_base._git_environment(), "GIT_LITERAL_PATHSPECS": "1"}
    process = None
    selector = None
    try:
        process = subprocess.Popen(
            argv, cwd=Path(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True,
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        counts = {"modified": 0, "added": 0, "deleted": 0,
                  "untracked": 0, "conflicted": 0, "clipped": 0}
        count_keys = ("modified", "added", "deleted", "untracked", "conflicted")
        field = bytearray()
        field_oversized = False
        skip_rename_source = False
        deadline = time.monotonic() + _GIT_TIMEOUT_S
        stop_early = False

        def consume(value: bytes, oversized: bool) -> None:
            nonlocal skip_rename_source, stop_early
            if not value:
                return
            if skip_rename_source:
                skip_rename_source = False
                return
            if sum(counts[key] for key in count_keys) >= max_entries:
                counts["clipped"] = 1
                stop_early = True
                return
            if value.startswith(b"? "):
                counts["untracked"] += 1
            elif value.startswith(b"u "):
                counts["conflicted"] += 1
            elif value.startswith((b"1 ", b"2 ")):
                status = value.split(b" ", 2)[1]
                if b"A" in status:
                    counts["added"] += 1
                elif b"D" in status:
                    counts["deleted"] += 1
                else:
                    counts["modified"] += 1
                if value.startswith(b"2 "):
                    skip_rename_source = True
            if oversized:
                counts["clipped"] = 1

        while selector.get_map() and not stop_early:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(min(remaining, 0.25)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    consume(bytes(field), field_oversized)
                    field.clear()
                    break
                start = 0
                while start < len(chunk) and not stop_early:
                    end = chunk.find(b"\0", start)
                    if end < 0:
                        part = chunk[start:]
                        end = len(chunk)
                        complete = False
                    else:
                        part = chunk[start:end]
                        complete = True
                    room = max(0, _MAX_STATUS_ENTRY - len(field))
                    if len(part) > room:
                        field.extend(part[:room])
                        field_oversized = True
                    else:
                        field.extend(part)
                    if complete:
                        consume(bytes(field), field_oversized)
                        field.clear()
                        field_oversized = False
                        start = end + 1
                    else:
                        start = len(chunk)
        if stop_early:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        else:
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if returncode:
                raise _base.WorktreeError("git status failed in the session worktree")
        return counts
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise _base.WorktreeError("git status timed out") from exc
    except OSError as exc:
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise _base.WorktreeError(f"could not run git status: {exc}") from exc
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _payload(record: SessionWorktreeRecord) -> dict[str, object]:
    return {
        "version": _VERSION, "kind": "session", "child_id": record.child_id,
        "session_id": record.session_id, "parent_workspace": str(record.parent_workspace),
        "base_ref": record.base_ref, "base_commit": record.base_commit,
        "branch": record.branch, "path": str(record.path), "owner_uid": record.owner_uid,
        "parent_record": record.parent_record, "created_at": record.created_at,
        "updated_at": record.updated_at, "lifecycle": record.lifecycle,
    }


def _encoded(record: SessionWorktreeRecord, key: bytes) -> bytes:
    payload = _payload(record)
    mac = hmac.new(key, _base._canonical_json(payload), hashlib.sha256).hexdigest()
    return json.dumps({"record": payload, "mac": mac}, sort_keys=True, indent=2).encode() + b"\n"


def _write(path: Path, record: SessionWorktreeRecord, key: bytes, *, replace: bool = False) -> None:
    data = _encoded(record, key)
    if replace:
        # Match the base service's fsync + atomic replace publication.
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
            _base._fsync_directory(path.parent)
        except BaseException:
            with contextlib.suppress(OSError): os.unlink(temporary)
            raise
    else:
        # Shared publisher refuses to replace an existing record.
        fd, temporary = __import__("tempfile").mkstemp(prefix=f".{path.name}.", dir=path.parent)
        published = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.link(temporary, path, follow_symlinks=False); published = True
            os.unlink(temporary); _base._fsync_directory(path.parent)
        except BaseException:
            with contextlib.suppress(OSError): os.unlink(temporary)
            if published:
                with contextlib.suppress(OSError): path.unlink()
            raise


def _read(path: Path, record_id: str, key: bytes) -> SessionWorktreeRecord:
    try:
        envelope = json.loads(_base._read_metadata_bytes(path))
        payload, supplied = envelope["record"], envelope["mac"]
        if not isinstance(payload, dict) or not isinstance(supplied, str):
            raise ValueError("invalid record envelope")
        expected = hmac.new(key, _base._canonical_json(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("authentication failed")
        version = payload.get("version")
        if type(version) is not int or version != _VERSION or payload.get("kind") != "session":
            raise ValueError("record kind or version mismatch")
        record = SessionWorktreeRecord(
            child_id=payload["child_id"], session_id=payload["session_id"],
            parent_workspace=Path(payload["parent_workspace"]), base_ref=payload["base_ref"],
            base_commit=payload["base_commit"], branch=payload["branch"], path=Path(payload["path"]),
            owner_uid=payload["owner_uid"], created_at=payload["created_at"],
            updated_at=payload["updated_at"], parent_record=payload["parent_record"],
            lifecycle=payload["lifecycle"],
        )
    except _base.WorktreeError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _base.WorktreeError(f"invalid session worktree record {record_id!r}: {exc}") from exc
    if (
        not isinstance(record.child_id, str) or record.child_id != record_id
        or not isinstance(record.session_id, str) or len(record.session_id) > 190
        or record.session_id != record_id.removeprefix(_PREFIX)
        or not record.child_id.startswith(_PREFIX)
        or type(record.owner_uid) is not int or record.owner_uid != os.getuid()
        or not isinstance(record.lifecycle, str) or record.lifecycle not in _LIFECYCLES
        or not record.parent_workspace.is_absolute() or not record.path.is_absolute()
        or not isinstance(record.base_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", record.base_commit)
        or not isinstance(record.branch, str) or not record.branch.startswith(_BRANCH_PREFIX) or len(record.branch) > 100
        or not isinstance(record.base_ref, str) or not record.base_ref or len(record.base_ref) > 200
        or "\x00" in record.base_ref or not isinstance(record.parent_record, str)
        or len(record.parent_record) > 256
        or type(record.created_at) is not float or not math.isfinite(record.created_at)
        or type(record.updated_at) is not float or not math.isfinite(record.updated_at)
        or record.updated_at < record.created_at
    ):
        raise _base.WorktreeError("session worktree metadata is invalid")
    return record


def _transition(record: SessionWorktreeRecord, lifecycle: str) -> SessionWorktreeRecord:
    """Return a lifecycle update with a strictly monotonic persisted timestamp."""
    return __import__("dataclasses").replace(
        record, lifecycle=lifecycle, updated_at=max(time.time(), record.updated_at + 0.000001)
    )


def _record_path(records: Path, record_id: str) -> Path:
    return records / _base._record_name(record_id)


def _read_record(root: Path, record_id: str) -> tuple[Path, bytes, SessionWorktreeRecord]:
    service_root, records = _base._service_layout(root)
    key = _base._load_key(service_root, create=False)
    path = _record_path(records, record_id)
    if not path.exists():
        raise _base.WorktreeError(f"no owned session worktree record for {record_id!r}")
    return service_root, key, _read(path, record_id, key)


def _verify(record: SessionWorktreeRecord, root: Path, *, allow_pending: bool = True) -> SessionWorktreeRecord:
    parent = _base._checkout_root(record.parent_workspace)
    service_root = root.resolve(strict=True)
    if service_root == parent or service_root in parent.parents or parent in service_root.parents:
        raise _base.WorktreeError("worktree service root must remain separate from the checkout")
    expected_dir = service_root / "worktrees"
    if record.path.parent != expected_dir or record.path.is_symlink():
        raise _base.WorktreeError("session worktree path is outside its owned directory")
    _git(parent, "cat-file", "-e", f"{record.base_commit}^{{commit}}")
    probe = subprocess.run(_base._git_argv("check-ref-format", f"refs/heads/{record.branch}"), cwd=parent,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_base._git_environment(),
        timeout=_GIT_TIMEOUT_S)
    if probe.returncode:
        raise _base.WorktreeError("session worktree branch ref is invalid")
    if record.lifecycle == "pending" and allow_pending and not record.path.exists():
        # A crash after publishing pending but before git worktree add is recoverable.
        return record
    if record.lifecycle in {"removed", "landed"} and not record.path.exists():
        return record
    if record.path.is_symlink():
        raise _base.WorktreeError("session worktree path is a symlink")
    if not record.path.exists():
        if record.lifecycle in {"active", "detached"}:
            return record
        raise _base.WorktreeError("session worktree path is missing")
    if not record.path.is_dir():
        raise _base.WorktreeError("session worktree path is not a directory")
    expected_ref = f"refs/heads/{record.branch}"
    head = _git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
    branch_head = _git(parent, "rev-parse", "--verify", f"{expected_ref}^{{commit}}", check=False)
    branch = _git(record.path, "symbolic-ref", "--quiet", "HEAD", check=False)
    parent_common = Path(_git(parent, "rev-parse", "--git-common-dir"))
    if not parent_common.is_absolute(): parent_common = (parent / parent_common).resolve(strict=True)
    else: parent_common = parent_common.resolve(strict=True)
    child_common = Path(_git(record.path, "rev-parse", "--git-common-dir"))
    if not child_common.is_absolute(): child_common = (record.path / child_common).resolve(strict=True)
    else: child_common = child_common.resolve(strict=True)
    registered_ref = _base._registered_worktrees(parent).get(record.path)
    registrations = _base._registered_worktrees(parent)
    if child_common != parent_common or record.path not in registrations or registered_ref != branch:
        raise _base.WorktreeError("Git does not register the session path on its current branch")
    if branch == expected_ref:
        if not branch_head or head != branch_head:
            raise _base.WorktreeError("session checkout HEAD does not match its owned branch")
        ancestor = subprocess.run(_base._git_argv("merge-base", "--is-ancestor", record.base_commit, head), cwd=parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_base._git_environment(),
            timeout=_GIT_TIMEOUT_S)
        if ancestor.returncode:
            raise _base.WorktreeError("session branch no longer descends from its recorded base")
    return record


def _base_commit(parent: Path, base: str) -> tuple[str, str]:
    if not isinstance(base, str) or not base or len(base) > 200 or "\x00" in base or base.startswith("-"):
        raise _base.WorktreeError("base must be a local branch name or 'current'")
    if base == "current":
        ref = _git(parent, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or "HEAD"
    elif re.fullmatch(r"[0-9a-fA-F]{40,64}", base):
        commit = _git(parent, "rev-parse", "--verify", f"{base}^{{commit}}", check=False)
        if not commit:
            raise _base.WorktreeError("base commit does not exist in this repository")
        return base, commit
    else:
        ref = base.removeprefix("refs/heads/")
        if ref.startswith("refs/") or not ref or ".." in ref or ref.startswith("/"):
            raise _base.WorktreeError("base must name a local branch")
        probe = subprocess.run(_base._git_argv("check-ref-format", f"refs/heads/{ref}"), cwd=parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_base._git_environment(), timeout=_GIT_TIMEOUT_S)
        if probe.returncode:
            raise _base.WorktreeError("base must name a valid local branch")
    revision = _git(parent, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if not revision:
        raise _base.WorktreeError(f"base ref {ref!r} does not resolve to a commit")
    return ref, revision


def _checkout_for_session(parent_workspace: str | Path) -> Path:
    """Resolve a supported full checkout before creating an owned worktree."""
    candidate = Path(parent_workspace).expanduser().resolve(strict=True)
    if not candidate.is_dir():
        raise _base.WorktreeError("parent_workspace must be a Git checkout directory")
    core_worktree = _git(candidate, "config", "--local", "--get", "core.worktree", check=False)
    sparse = _git(candidate, "config", "--bool", "--get", "core.sparseCheckout", check=False)
    partial_clone = _git(candidate, "config", "--local", "--get", "extensions.partialClone", check=False)
    promisor = _git(candidate, "config", "--local", "--get-regexp", r"^remote\..*\.promisor$", check=False)
    if core_worktree or sparse == "true" or partial_clone or promisor:
        raise _base.WorktreeError("unsupported repository layout for session worktrees")
    try:
        return _base._checkout_root(candidate)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _base.WorktreeError("unsupported repository layout for session worktrees") from exc


def _slug(name: str, token: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._") or "session"
    clean = clean[:35]
    return f"{clean}-{token}"


def create_session(parent_workspace: str | Path, session_id: str, name: str, *, root: str | Path,
                   base: str = "current", max_worktrees: int = 16) -> SessionWorktreeRecord:
    record_id = session_record_id(session_id)
    if not isinstance(name, str) or not name or len(name) > 200:
        raise _base.WorktreeError("session name must be non-empty and bounded")
    if isinstance(max_worktrees, bool) or not isinstance(max_worktrees, int) or max_worktrees < 1:
        raise _base.WorktreeError("max_worktrees must be a positive integer")
    parent = _checkout_for_session(parent_workspace)
    base_ref, base_sha = _base_commit(parent, base)
    service_root, records, worktrees = _base._layout(root, parent, create=True)
    with _base._locked(service_root):
        key = _base._load_key(service_root, create=True)
        record_path = _record_path(records, record_id)
        replaced_record = None
        if record_path.exists() or record_path.is_symlink():
            if record_path.is_symlink():
                raise _base.WorktreeError("session worktree record cannot be a symlink")
            replaced_record = _read(record_path, record_id, key)
            if replaced_record.lifecycle != "removed":
                raise _base.WorktreeError(f"session worktree already exists for {session_id!r}")
        count = 0
        for entry in records.glob("*.json"):
            try:
                envelope = json.loads(_base._read_metadata_bytes(entry))
                payload = envelope.get("record", {})
                if payload.get("kind") == "session":
                    existing = _read(entry, payload.get("child_id", ""), key)
                    if existing.lifecycle not in {"removed", "landed"}:
                        count += 1
            except Exception as exc:
                raise _base.WorktreeError(f"cannot count authenticated session worktrees: {exc}") from exc
        if count >= max_worktrees:
            raise _base.WorktreeError(f"maximum session worktrees reached ({max_worktrees})")
        for _ in range(32):
            token = __import__("secrets").token_hex(3)
            slug = _slug(name, token)
            branch, path = f"{_BRANCH_PREFIX}{slug}", worktrees / slug
            branch_probe = subprocess.run(_base._git_argv("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                cwd=parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_base._git_environment(), timeout=_GIT_TIMEOUT_S)
            if not path.exists() and not path.is_symlink() and branch_probe.returncode == 1:
                break
        else:
            raise _base.WorktreeError("could not allocate a unique session worktree")
        now = time.time()
        record = SessionWorktreeRecord(
            record_id, session_id, parent, base_ref, base_sha, branch, path, os.getuid(),
            now, now,
        )
        # Authenticated intent is durable before git mutates the common repository.
        _write(record_path, record, key, replace=replaced_record is not None)
        try:
            _git(parent, "worktree", "add", "-b", branch, str(path), base_sha, observational=False)
            return _verify(record, service_root)
        except BaseException:
            if _cleanup_pending(record, service_root):
                with contextlib.suppress(OSError): record_path.unlink()
            else:
                pending_cleanup = _transition(record, "cleanup_pending")
                _write(record_path, pending_cleanup, key, replace=True)
            raise


def _cleanup_pending(record: SessionWorktreeRecord, service_root: Path) -> bool:
    """Remove only a pending checkout still exactly at its recorded base."""
    parent = record.parent_workspace
    ref = f"refs/heads/{record.branch}"
    try:
        registrations = _base._registered_worktrees(parent)
        head = _git(parent, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if record.path.exists():
            if record.path.is_symlink() or registrations.get(record.path.resolve()) != ref:
                return False
            common = _git(record.path, "rev-parse", "--git-common-dir")
            common_path = Path(common)
            if not common_path.is_absolute(): common_path = (record.path / common_path).resolve(strict=True)
            else: common_path = common_path.resolve(strict=True)
            parent_common = Path(_git(parent, "rev-parse", "--git-common-dir"))
            if not parent_common.is_absolute(): parent_common = (parent / parent_common).resolve(strict=True)
            else: parent_common = parent_common.resolve(strict=True)
            if common_path != parent_common or head != record.base_commit:
                return False
            _git(parent, "worktree", "remove", "--force", str(record.path), observational=False)
        if not _base._registered_worktrees(parent).get(record.path) and head == record.base_commit:
            _git(parent, "branch", "-D", record.branch, check=False, observational=False)
        remaining = _git(parent, "rev-parse", "--verify", f"refs/heads/{record.branch}^{{commit}}", check=False)
        return not record.path.exists() and record.path not in _base._registered_worktrees(parent) and not remaining
    except (OSError, _base.WorktreeError, subprocess.TimeoutExpired):
        return False


def activate_session(record_id: str, *, root: str | Path) -> SessionWorktreeRecord:
    service_root, key, record = _read_record(Path(root), record_id)
    with _base._locked(service_root):
        record = _read(_record_path(service_root / "records", record_id), record_id, key)
        if record.lifecycle == "active": return _verify(record, service_root)
        if record.lifecycle != "pending": raise _base.WorktreeError("session worktree is not pending activation")
        record = _verify(record, service_root, allow_pending=False)
        active = _transition(record, "active")
        _write(_record_path(service_root / "records", record_id), active, key, replace=True)
        return _verify(active, service_root)


def abort_session(record_id: str, *, root: str | Path) -> SessionWorktreeRecord:
    service_root, key, record = _read_record(Path(root), record_id)
    with _base._locked(service_root):
        record = _read(_record_path(service_root / "records", record_id), record_id, key)
        if record.lifecycle != "pending": raise _base.WorktreeError("only a pending session worktree can be aborted")
        if not _cleanup_pending(record, service_root):
            raise _base.WorktreeError("pending session worktree cleanup is ambiguous; record retained")
        removed = _transition(record, "removed")
        _write(_record_path(service_root / "records", record_id), removed, key, replace=True)
        return removed


def detach_session(record_id: str, *, root: str | Path) -> SessionWorktreeRecord:
    """Retain the owned checkout and branch while detaching the session."""
    service_root, _key, _record = _read_record(Path(root), record_id)
    with _base._locked(service_root):
        service_root, key, record = _read_record(service_root, record_id)
        if record.lifecycle not in {"active", "detached"}:
            raise _base.WorktreeError("only an active session worktree can be detached")
        _verify(record, service_root)
        detached = _transition(record, "detached")
        _write(_record_path(service_root / "records", record_id), detached, key, replace=True)
        return detached


def recover_sessions(*, root: str | Path, created_ids: set[str],
                     pending_ids: set[str] | frozenset[str] = frozenset()) -> tuple[SessionWorktreeRecord, ...]:
    service_root, records = _base._service_layout(root)
    results = []
    for path in sorted(records.glob("*.json")):
        try:
            envelope = json.loads(_base._read_metadata_bytes(path))
            payload = envelope["record"]
            if payload.get("kind") != "session": continue
            record_id = payload.get("child_id")
            if not isinstance(record_id, str): raise ValueError("missing record id")
        except Exception as exc:
            raise _base.WorktreeError(f"invalid session worktree record file {path.name}: {exc}") from exc
        _service, key, record = _read_record(service_root, record_id)
        if record.lifecycle in {"pending", "cleanup_pending"}:
            with _base._locked(service_root):
                if record.lifecycle == "pending" and record.session_id in pending_ids and record.session_id not in created_ids:
                    if record.path.exists(): _verify(record, service_root)
                    results.append(record)
                elif record.lifecycle == "pending" and record.session_id in created_ids:
                    if record.path.exists():
                        _verify(record, service_root)
                    else:
                        branch_head = _git(record.parent_workspace, "rev-parse", "--verify",
                                           f"refs/heads/{record.branch}^{{commit}}", check=False)
                        if not branch_head:
                            detached = _transition(record, "detached")
                            _write(path, detached, key, replace=True); results.append(detached)
                            continue
                    active = _transition(record, "active")
                    _write(path, active, key, replace=True); results.append(active)
                else:
                    if _cleanup_pending(record, service_root):
                        removed = _transition(record, "removed")
                        _write(path, removed, key, replace=True); results.append(removed)
                    else:
                        cleanup_pending = _transition(record, "cleanup_pending")
                        _write(path, cleanup_pending, key, replace=True); results.append(cleanup_pending)
        elif record.lifecycle == "active" and not record.path.exists():
            results.append(record)
        else:
            results.append(_verify(record, service_root))
    return tuple(results)


def recreate_session(record_id: str, *, root: str | Path) -> SessionWorktreeRecord:
    service_root, key, record = _read_record(Path(root), record_id)
    with _base._locked(service_root):
        record = _read(_record_path(service_root / "records", record_id), record_id, key)
        if record.lifecycle != "active": raise _base.WorktreeError("only an active session worktree can be recreated")
        if record.path.exists(): return _verify(record, service_root)
        ref = f"refs/heads/{record.branch}"
        branch_head = _git(record.parent_workspace, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if not branch_head: raise _base.WorktreeError("session branch is missing; detach or recreate it explicitly")
        _git(record.parent_workspace, "worktree", "prune", observational=False)
        _git(record.parent_workspace, "worktree", "add", str(record.path), record.branch, observational=False)
        return _verify(record, service_root)


def get_session(record_id: str, *, root: str | Path) -> SessionWorktreeRecord:
    service_root, _records = _base._service_layout(root)
    with _base._locked(service_root):
        _service, key, record = _read_record(service_root, record_id)
        return _verify(record, service_root)


def inspect_session(record_id: str, *, root: str | Path) -> SessionWorktreeRecord:
    return get_session(record_id, root=root)


def list_sessions(*, root: str | Path) -> tuple[SessionWorktreeRecord, ...]:
    service_root, records = _base._service_layout(root)
    results = []
    for path in sorted(records.glob("*.json")):
        try:
            payload = json.loads(_base._read_metadata_bytes(path))["record"]
            if payload.get("kind") == "session": results.append(get_session(payload["child_id"], root=service_root))
        except _base.WorktreeError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise _base.WorktreeError(f"invalid session worktree record file {path.name}: {exc}") from exc
    return tuple(results)


def session_status(record_id: str, *, root: str | Path) -> dict[str, object]:
    record = get_session(record_id, root=root)
    if not record.path.exists():
        return {"lifecycle": record.lifecycle, "branch": record.branch, "base_ref": record.base_ref,
                "base_commit": record.base_commit, "missing": True, "head": "",
                "uncommitted": {"modified": 0, "added": 0, "deleted": 0,
                                "untracked": 0, "conflicted": 0, "clipped": 0}}
    head = _git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
    branch = _git(record.path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    uncommitted = status_counts(record.path)
    rows = session_commits(record_id, root=root, since=record.base_commit)
    return {"lifecycle": record.lifecycle, "branch": branch, "expected_branch": record.branch,
            "base_ref": record.base_ref, "base_commit": record.base_commit, "head": head,
            "uncommitted": uncommitted, "commits": rows, "missing": False}


def _safe_paths(paths: list[str] | None) -> list[str] | None:
    if paths is None: return None
    if not isinstance(paths, list) or not paths or len(paths) > 500 or any(not isinstance(p, str) or not p or "\x00" in p for p in paths):
        raise _base.WorktreeError("paths must be a list of at most 500 relative paths")
    normalized = []
    for value in paths:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise _base.WorktreeError("commit paths must stay inside the session worktree")
        normalized.append(candidate.as_posix())
    return normalized


def _commit_stats(path: Path, *, max_file_bytes: int, extra_env: Mapping[str, str]) -> tuple[int, int, int, list[str]]:
    raw = _git(path, "diff", "--cached", "--numstat", "-z", extra_env=extra_env)
    insertions, deletions, files = 0, 0, []
    for entry in raw.split("\0"):
        if not entry: continue
        fields = entry.split("\t", 2)
        if len(fields) != 3: continue
        added = None if fields[0] == "-" else int(fields[0])
        deleted = None if fields[1] == "-" else int(fields[1])
        name = fields[2]
        files.append(name)
        if added is not None: insertions += added
        if deleted is not None: deletions += deleted
        target = path / name
        if target.exists() and target.is_file() and target.stat().st_size > max_file_bytes:
            raise _base.WorktreeError("staged change exceeds the configured per-file limit")
    return len(files), insertions, deletions, files


def commit_session(record_id: str, *, root: str | Path, message: str, session_name: str,
                   turn_seq: int, agent_id: str = "root", paths: list[str] | None = None,
                   max_files: int = _MAX_FILES, max_file_mb: int = 50, by: str = "harness",
                   allow_pending: bool = False) -> dict[str, object]:
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise _base.WorktreeError("commit message must be non-empty and at most 4000 characters")
    if isinstance(turn_seq, bool) or not isinstance(turn_seq, int) or turn_seq < 0:
        raise _base.WorktreeError("turn_seq must be a non-negative integer")
    if by not in {"harness", "agent", "subagent"}:
        raise _base.WorktreeError("invalid commit author kind")
    pathspec = _safe_paths(paths)
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= 100_000:
        raise _base.WorktreeError("max_files must be between 1 and 100000")
    if isinstance(max_file_mb, bool) or not isinstance(max_file_mb, int) or not 1 <= max_file_mb <= 2048:
        raise _base.WorktreeError("max_file_mb must be between 1 and 2048")
    service_root, _records = _base._service_layout(root)
    with _base._locked(service_root):
        service_root, key, record = _read_record(service_root, record_id)
        record = _verify(record, service_root, allow_pending=allow_pending)
        allowed_lifecycles = {"active", "pending"} if allow_pending else {"active"}
        if record.lifecycle not in allowed_lifecycles: raise _base.WorktreeError("session worktree is not active")
        ref = f"refs/heads/{record.branch}"
        branch = _git(record.path, "symbolic-ref", "--quiet", "HEAD", check=False)
        old_head = _git(record.parent_workspace, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        head = _git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
        if branch != ref or old_head != head:
            return {"kind": "skipped", "reason": "git_error", "detail": "session branch changed; refusing to commit"}
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
            marker_path = Path(_git(record.path, "rev-parse", "--git-path", marker))
            if not marker_path.is_absolute(): marker_path = record.path / marker_path
            if marker_path.exists():
                return {"kind": "skipped", "reason": "conflict_in_progress", "detail": "Git operation is in progress in the session worktree"}
        # Build an isolated index so a requested path commit cannot scoop up
        # unrelated pre-staged paths from the session's normal index.
        index_path = Path(_git(record.path, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute(): index_path = record.path / index_path
        index_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, isolated_index_name = tempfile.mkstemp(prefix=".nexus-session-index-", dir=index_path.parent)
        os.close(fd)
        isolated_index = Path(isolated_index_name)
        index_env = {"GIT_INDEX_FILE": str(isolated_index)}
        try:
            _git(record.path, "read-tree", "HEAD", extra_env=index_env, observational=False)
            args = ["add", "-A"]
            if pathspec is not None: args += ["--", *pathspec]
            _git(record.path, *args, observational=False, extra_env=index_env)
            # --quiet output is empty either way; inspect the exit status separately.
            probe = subprocess.run(_base._git_argv("diff", "--cached", "--quiet"), cwd=record.path,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**_base._git_environment(), **index_env}, timeout=_GIT_TIMEOUT_S)
            if probe.returncode == 0:
                return {"kind": "unchanged"}
            if probe.returncode != 1: raise _base.WorktreeError("could not inspect staged changes")
            count, insertions, deletions, files = _commit_stats(
                record.path, max_file_bytes=max_file_mb * 1024 * 1024, extra_env=index_env
            )
            if count > max_files:
                raise _base.WorktreeError(f"staged change exceeds {max_files} files")
            stripped_trailers = bool(_NEXUS_TRAILER.search(message))
            message = _TRAILER_LINE.sub("", message).strip()
            subject = message.splitlines()[0][:72]
            body = "\n".join(message.splitlines()[1:]).strip()
            safe_session = re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", session_name)[:200]
            safe_agent = re.sub(r"[\r\n\x00-\x1f\x7f]+", "-", agent_id)[:128]
            trailers = [f"Nexus-Session: {safe_session} ({record.session_id})", f"Nexus-Turn-Seq: {turn_seq}", f"Nexus-Agent: {safe_agent}"]
            config = []
            identity_fallback = False
            if not _git(record.path, "config", "--get", "user.name", check=False):
                config += ["-c", "user.name=Nexus"]; identity_fallback = True
            if not _git(record.path, "config", "--get", "user.email", check=False):
                config += ["-c", "user.email=nexus@localhost"]; identity_fallback = True
            config += ["-c", "commit.gpgsign=false"]
            file_lines = [f"- {name}" for name in files[:50]]
            if len(files) > 50: file_lines.append(f"- and {len(files) - 50} more")
            if file_lines: body = (body + "\n\n" if body else "") + "Files:\n" + "\n".join(file_lines)
            commit_message = subject + ("\n\n" + body if body else "") + "\n\n" + "\n".join(trailers) + "\n"
            tree = _git(record.path, "write-tree", extra_env=index_env, observational=False)
            new_head = _git(record.path, *config, "commit-tree", tree, "-p", head, "-F", "-",
                            input_text=commit_message, observational=False)
            # Compare-and-swap prevents racing a shell `git commit` or branch
            # move and can only update the authenticated Nexus session ref.
            _git(record.parent_workspace, "update-ref", ref, new_head, head, observational=False)
            try:
                if _git(record.path, "symbolic-ref", "--quiet", "HEAD", check=False) != ref:
                    raise _base.WorktreeError("session branch changed after commit")
                if pathspec is None:
                    _git(record.path, "read-tree", new_head, observational=False)
                else:
                    _git(record.path, "reset", "--quiet", new_head, "--", *pathspec, observational=False)
                index_refresh_failed = False
            except _base.WorktreeError:
                # The ref update is already durable; report the commit truthfully
                # and leave an explicit hint that the normal index needs refresh.
                index_refresh_failed = True
            return {"kind": "committed", "sha": new_head, "seq": turn_seq, "by": by,
                    "agent_id": agent_id, "subject": subject, "files": files[:200],
                    "clipped": max(0, len(files)-200), "insertions": insertions, "deletions": deletions,
                    "identity_fallback": identity_fallback, "stripped_nexus_trailers": stripped_trailers,
                    "index_refresh_failed": index_refresh_failed}
        except _base.WorktreeError as exc:
            detail = str(exc)[:500]
            reason = "too_large" if "exceeds" in detail or "limit" in detail else "git_error"
            return {"kind": "skipped", "reason": reason, "detail": detail}
        finally:
            with contextlib.suppress(OSError): isolated_index.unlink()


def session_commits(record_id: str, *, root: str | Path, since: str | None = None) -> tuple[dict[str, object], ...]:
    record = get_session(record_id, root=root)
    revision = since or record.base_commit
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        raise _base.WorktreeError("since must be a full commit object id")
    pretty = (
        "%H%x00%<(200,trunc)%s%x00"
        "%<(128,trunc)%(trailers:key=Nexus-Agent,valueonly)%x00"
        "%<(20,trunc)%(trailers:key=Nexus-Turn-Seq,valueonly)%x00"
        "%<(240,trunc)%(trailers:key=Nexus-Session,valueonly)%x00"
    )
    output = _git(record.path, "log", "-50", f"--format={pretty}", f"{revision}..HEAD")
    fields = output.split("\x00")
    results = []
    for idx in range(0, len(fields)-4, 5):
        sha, subject, agent_value, seq_value, session_value = (field.strip() for field in fields[idx:idx+5])
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            continue
        numstat = _git(record.path, "show", "--format=", "--numstat", "-z", sha)
        files, insertions, deletions = [], 0, 0
        for row in numstat.split("\0"):
            if not row: continue
            columns = row.split("\t", 2)
            if len(columns) != 3: continue
            files.append(columns[2])
            if columns[0] != "-": insertions += int(columns[0])
            if columns[1] != "-": deletions += int(columns[1])
        seq = re.fullmatch(r"\d+", seq_value)
        session_trailer = re.search(r"\(([^()]+)\)\s*$", session_value)
        nexus_owned = bool(session_trailer and session_trailer.group(1) == record.session_id and seq)
        trailer_lines = []
        if agent_value:
            trailer_lines.append(f"Nexus-Agent: {agent_value}")
        if seq:
            trailer_lines.append(f"Nexus-Turn-Seq: {seq.group(0)}")
        if session_value:
            trailer_lines.append(f"Nexus-Session: {session_value}")
        row: dict[str, object] = {
            "sha": sha, "subject": subject[:200], "body": "\n".join(trailer_lines)[:2000],
            "session": record.session_id, "files": files[:200],
            "clipped": max(0, len(files) - 200), "insertions": insertions,
            "deletions": deletions, "by": "agent", "via": "nexus" if nexus_owned else "shell",
        }
        if agent_value: row["agent_id"] = agent_value[:128]
        if seq: row["seq"] = int(seq.group(0))
        results.append(row)
    return tuple(results)
