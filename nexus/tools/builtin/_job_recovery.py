"""Restart recovery for shell job process groups (SHELL_PLAN P7).

A daemon that dies without cleanup (``kill -9``, power loss) leaves its jobs'
process groups running. This module lets the next start find and stop exactly
those groups -- and nothing else -- without ever trusting a PID or a group id
alone, either of which the OS recycles.

State (one pair of files per :class:`~nexus.tools.builtin._jobs.JobRegistry`,
under ``<home>/projects/<project-key>/shell-owners/``)::

    owner_<16 hex>.lock    held with ``flock`` for the registry's whole life
    owner_<16 hex>.json    {"v": 1, "owner", "pid", "uid", "jobs": {job_id: ...}}

Why a lock per owner and not "the daemon's PID": a PID can be reused and a live
unrelated process can have it. The kernel drops a ``flock`` when its holder dies,
however it dies, and nothing else can fake that: a record is *dead* only when its
lock can be taken. Descriptors are close-on-exec, so a job can never inherit the
lock and keep a dead owner looking alive.

What is recorded per job: its process group id plus the identity -- ``(pid,
start time, pgid, uid)`` -- of up to :data:`MAX_MEMBERS` group members. A group is
signalled only if, in a **fresh** process listing taken right before the signal,
at least one recorded member still has exactly its recorded identity. That holds
the group id: a group id cannot be recycled while a member lives, and a recycled
*pid* has a different start time. A persistent job whose shell exited is still
verifiable through any surviving member, which is why members are refreshed while
the job runs. ``killpg(pgid, 0)`` ("the group exists") is never enough.

Everything here is best effort and bounded: record reads are size-capped, the
sweep has a wall-clock budget, files are ``0600`` in a ``0700`` directory that is
never a symlink, and any malformed or foreign record is ignored (never signalled).
No function in this module signals a process except :func:`sweep`, and it only
signals groups owned by the current user.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_MEMBERS",
    "OWNERS_DIRNAME",
    "OwnerRecord",
    "Proc",
    "SweepResult",
    "group_procs",
    "identify",
    "job_may_be_live",
    "sweep",
]

#: Directory (beside ``shell/``) holding the owner records.
OWNERS_DIRNAME = "shell-owners"
#: Group members whose identity is kept per job.
MAX_MEMBERS = 8
#: Largest owner record read; a bigger file is malformed.
MAX_RECORD_BYTES = 256 * 1024
#: Owners / jobs per owner considered by one sweep or probe.
MAX_OWNERS = 64
MAX_JOBS_PER_OWNER = 64
#: Process listing output cap.
_MAX_PS_BYTES = 8 * 1024 * 1024
_PS_TIMEOUT_S = 3.0
#: Wall-clock budget of one sweep; kept under the daemon's autostart timeout.
_SWEEP_BUDGET_S = 6.0
_POLL_S = 0.1
#: Past ``budget_s`` a group already signalled may finish (its KILL wait), by this much.
_SWEEP_SLACK_S = 1.0
#: A group is not started with less budget than this left.
_MIN_GROUP_S = 0.5

_OWNER_RE = re.compile(r"owner_[0-9a-f]{16}\Z")
_JOB_ID_RE = re.compile(r"job_[0-9a-f]{12}\Z")
_START_RE = re.compile(r"[A-Za-z]{3} [A-Za-z]{3} \d{1,2} \d{2}:\d{2}:\d{2} \d{4}\Z")
_PID_MAX = 2**31 - 1
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class Proc:
    """A process identity. Equality is the whole check: every field must match."""

    pid: int
    pgid: int
    uid: int
    start: str


def _valid_proc(pid: object, pgid: object, uid: object, start: object) -> bool:
    return (
        type(pid) is int
        and type(pgid) is int
        and type(uid) is int
        and 1 < pid <= _PID_MAX
        and 1 < pgid <= _PID_MAX
        and uid >= 0
        and isinstance(start, str)
        and _START_RE.fullmatch(start) is not None
    )


# -- process listing -----------------------------------------------------------


def _ps_binary() -> str | None:
    for candidate in ("/bin/ps", "/usr/bin/ps"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _listing(
    pids: list[int] | None = None, *, timeout: float = _PS_TIMEOUT_S
) -> dict[int, Proc] | None:
    """``pid -> Proc`` for every process (or just ``pids``); ``None`` if unreadable.

    ``ps`` with ``lstart`` is the one start-time source that reads the same on
    macOS and Linux. The locale is pinned so the timestamp format is stable.
    """
    binary = _ps_binary()
    if binary is None:
        return None
    argv = [binary]
    if pids is None:
        argv += ["-A"]
    else:
        argv += ["-p", ",".join(str(int(pid)) for pid in pids)]
    argv += ["-o", "pid=,pgid=,uid=,lstart="]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env={"LC_ALL": "C", "LANG": "C", "PATH": "/bin:/usr/bin"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # ``ps -p`` exits 1 when none of the pids exist: that is a valid empty answer.
    if done.returncode not in (0, 1) or len(done.stdout) > _MAX_PS_BYTES:
        return None
    if done.returncode == 1 and (pids is None or done.stderr.strip()):
        return None
    found: dict[int, Proc] = {}
    for raw in done.stdout.decode("utf-8", "replace").splitlines():
        parts = raw.split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid, pgid, uid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        start = " ".join(parts[3].split())
        if _valid_proc(pid, pgid, uid, start):
            found[pid] = Proc(pid, pgid, uid, start)
    if pids is None and not found:
        return None
    return found


def identify(pid: int) -> Proc | None:
    """The identity of one live process, or ``None`` (gone, or not readable)."""
    if type(pid) is not int or pid <= 1:
        return None
    found = _listing([pid])
    return None if found is None else found.get(pid)


def group_procs(pgid: int) -> list[Proc] | None:
    """Live members of ``pgid`` owned by this user (oldest pid first), or ``None``.

    Only meaningful to a caller that already knows ``pgid`` is still the group it
    started (it holds a verified member); the listing alone proves nothing.
    """
    found = _listing()
    if found is None:
        return None
    uid = os.getuid()
    return sorted(
        (proc for proc in found.values() if proc.pgid == pgid and proc.uid == uid),
        key=lambda proc: proc.pid,
    )


# -- files ----------------------------------------------------------------------


def _secure_dir(path: Path) -> None:
    """Create ``path`` (``0700``), refusing a symlink or a directory we do not own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"{path} is not a directory owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


def _dir_is_safe(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not stat.S_IMODE(info.st_mode) & 0o022
    )


def _fd_is_safe(fd: int, *, limit: int = MAX_RECORD_BYTES) -> bool:
    """A regular file we own that others cannot write, not larger than ``limit``."""
    info = os.fstat(fd)
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and not stat.S_IMODE(info.st_mode) & 0o022
        and info.st_size <= limit
    )


def _open_safe(path: Path, flags: int) -> int | None:
    """Open an existing owner file without following links; ``None`` if unsafe."""
    try:
        fd = os.open(path, flags | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if _fd_is_safe(fd):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _read_record(path: Path) -> dict[str, Any] | None:
    fd = _open_safe(path, os.O_RDONLY)
    if fd is None:
        return None
    try:
        data = os.read(fd, MAX_RECORD_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_RECORD_BYTES:
        return None
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class _Entry:
    job_id: str
    session: str
    pgid: int
    members: tuple[Proc, ...]


def _parse_member(raw: object, pgid: int) -> Proc | None:
    if not isinstance(raw, dict):
        return None
    pid, member_pgid, uid, start = (
        raw.get("pid"), raw.get("pgid"), raw.get("uid"), raw.get("start"),
    )
    if not _valid_proc(pid, member_pgid, uid, start):
        return None
    if member_pgid != pgid or uid != os.getuid():
        return None
    return Proc(pid, member_pgid, uid, start)  # type: ignore[arg-type]


def _parse_record(raw: dict[str, Any], owner_id: str) -> list[_Entry] | None:
    """Validated entries of one record, or ``None`` when the record is malformed.

    A single bad job entry is dropped (and so never signalled) without discarding
    its valid siblings; a bad header discards the whole record.
    """
    if raw.get("v") != 1 or raw.get("owner") != owner_id:
        return None
    if raw.get("uid") != os.getuid():
        return None
    jobs = raw.get("jobs")
    if not isinstance(jobs, dict) or len(jobs) > MAX_JOBS_PER_OWNER:
        return None
    entries: list[_Entry] = []
    own_group = os.getpgrp()
    for job_id, item in jobs.items():
        if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
            continue
        if not isinstance(item, dict):
            continue
        pgid, session, members = item.get("pgid"), item.get("session"), item.get("members")
        if type(pgid) is not int or not 1 < pgid <= _PID_MAX or pgid == own_group:
            continue
        if not isinstance(session, str) or len(session) > 256:
            continue
        # An empty list is valid: the job is known to the owner but no member
        # identity was captured. It counts as "may be live" and is never signalled.
        if not isinstance(members, list) or len(members) > MAX_MEMBERS:
            continue
        parsed = [_parse_member(member, pgid) for member in members]
        if any(member is None for member in parsed):
            continue
        entries.append(_Entry(job_id, session, pgid, tuple(parsed)))  # type: ignore[arg-type]
    return entries


def _lock_nb(fd: int) -> bool:
    """Take the exclusive lock without waiting; ``False`` when someone holds it."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _same_file(fd: int, path: Path) -> bool:
    """Whether ``path`` (not followed) is still the very file ``fd`` has open."""
    try:
        held, named = os.fstat(fd), os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(named.st_mode)
        and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
    )


# -- the owner side -------------------------------------------------------------


class OwnerRecord:
    """One registry's lock and record. Thread-safe; every method is best effort.

    Nothing is created until :meth:`activate`, so a registry that never spawns a
    job leaves no files. A failure disables tracking (``active`` stays false)
    and is reported through :attr:`error`; it never affects a job.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._guard = threading.Lock()
        self._owner_id: str | None = None
        self._fd: int | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._failed = False
        #: Why tracking is off (``None`` while it works or was never needed).
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self._fd is not None

    @property
    def owner_id(self) -> str | None:
        return self._owner_id

    def _lock_path(self, owner_id: str) -> Path:
        return self._directory / f"{owner_id}.lock"

    def _record_path(self, owner_id: str) -> Path:
        return self._directory / f"{owner_id}.json"

    def activate(self) -> bool:
        """Create and lock this owner's files (once); ``False`` if that failed."""
        with self._guard:
            return self._activate_locked()

    def _activate_locked(self) -> bool:
        if self._fd is not None:
            return True
        if self._failed:
            return False
        try:
            _secure_dir(self._directory)
            for _attempt in range(4):
                owner_id = "owner_" + secrets.token_hex(8)
                path = self._lock_path(owner_id)
                try:
                    fd = os.open(
                        path,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                        0o600,
                    )
                except FileExistsError:
                    continue
                # A concurrent sweep may have taken the brand-new file first, or
                # locked, unlinked and released it between our open and our flock
                # (which then succeeds on an inode no path leads to, invisible to
                # every reader). Either way: start over with a fresh name.
                if not _lock_nb(fd) or not _same_file(fd, path):
                    os.close(fd)
                    continue
                self._fd, self._owner_id = fd, owner_id
                self._write_locked()
                return True
            raise OSError("could not create an owner lock")
        except OSError as exc:
            self._failed = True
            self.error = exc.strerror or str(exc)
            self._drop_files_locked()
            return False

    def _document(self) -> dict[str, Any]:
        return {
            "v": 1,
            "owner": self._owner_id,
            "pid": os.getpid(),
            "uid": os.getuid(),
            "created": time.time(),
            "jobs": self._jobs,
        }

    def _write_locked(self) -> None:
        """Replace the record atomically (temp file + rename, ``0600``)."""
        if self._owner_id is None:
            return
        final = self._record_path(self._owner_id)
        temp = final.with_suffix(".json.tmp")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
        try:
            payload = json.dumps(self._document(), separators=(",", ":")).encode()
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, final)

    def track(
        self,
        job_id: str,
        session: str,
        pgid: int,
        members: list[Proc],
        *,
        persistent: bool = False,
    ) -> bool:
        """Record a job (or replace its members). ``False`` = not recorded."""
        with self._guard:
            if not self._activate_locked():
                return False
            usable = [m for m in members if m.pgid == pgid][:MAX_MEMBERS]
            existing = self._jobs.get(job_id)
            if not usable and existing is not None and existing["pgid"] == pgid:
                # Never forget identities already captured for this group.
                usable = [
                    Proc(m["pid"], m["pgid"], m["uid"], m["start"])
                    for m in existing["members"]
                ]
            if len(self._jobs) >= MAX_JOBS_PER_OWNER and job_id not in self._jobs:
                return False
            self._jobs[job_id] = {
                "session": session[:256],
                "pgid": pgid,
                "persistent": bool(persistent),
                "members": [
                    {"pid": m.pid, "pgid": m.pgid, "uid": m.uid, "start": m.start}
                    for m in usable
                ],
            }
            try:
                self._write_locked()
            except OSError as exc:
                self.error = exc.strerror or str(exc)
                return False
            return True

    def members(self, job_id: str) -> list[Proc]:
        with self._guard:
            item = self._jobs.get(job_id)
            if item is None:
                return []
            return [
                Proc(m["pid"], m["pgid"], m["uid"], m["start"]) for m in item["members"]
            ]

    def untrack(self, job_id: str) -> None:
        with self._guard:
            if self._fd is None or self._jobs.pop(job_id, None) is None:
                return
            with contextlib.suppress(OSError):
                self._write_locked()

    def has(self, session: str, job_id: str) -> bool:
        with self._guard:
            item = self._jobs.get(job_id)
            return item is not None and item["session"] == session[:256]

    def release(self, *, keep_record: bool = False) -> None:
        """Graceful shutdown: delete both files, then drop the lock.

        ``keep_record`` closes only the lock and leaves the record: the owner then
        reads as dead, so the next sweep deals with whatever the record lists.
        """
        with self._guard:
            if not keep_record:
                self._jobs.clear()
            self._drop_files_locked(keep_record=keep_record)
            self._failed = True  # a released owner never re-creates its files

    def _drop_files_locked(self, *, keep_record: bool = False) -> None:
        owner_id, fd = self._owner_id, self._fd
        self._owner_id, self._fd = None, None
        if owner_id is not None and not keep_record:
            for path in (
                self._record_path(owner_id),
                self._record_path(owner_id).with_suffix(".json.tmp"),
                self._lock_path(owner_id),
            ):
                with contextlib.suppress(OSError):
                    os.unlink(path)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


# -- the reader side: is a job's owner alive? -----------------------------------


def job_may_be_live(directory: Path, session: str, job_id: str) -> bool:
    """Whether some **live** owner records ``(session, job_id)`` -- fail closed.

    ``False`` only when every owner file in ``directory`` is provably dead (its
    lock can be taken) or does not list the job. An unreadable directory, an
    unreadable record of a live owner, or more owners than the bound all answer
    ``True``: "cannot prove it is gone" must never be reported as gone.
    """
    directory = Path(directory)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not _dir_is_safe(directory):
        return True
    locks = [n[:-5] for n in names if n.endswith(".lock") and _OWNER_RE.fullmatch(n[:-5])]
    if len(locks) > MAX_OWNERS:
        return True
    for owner_id in locks:
        fd = _open_safe(directory / f"{owner_id}.lock", os.O_RDWR)
        if fd is None:
            if (directory / f"{owner_id}.lock").exists():
                return True  # present but not safely readable: cannot say
            continue
        try:
            if _lock_nb(fd):
                continue  # dead owner: whatever it recorded is not live
        finally:
            os.close(fd)
        raw = _read_record(directory / f"{owner_id}.json")
        entries = None if raw is None else _parse_record(raw, owner_id)
        if entries is None:
            return True
        if any(e.job_id == job_id and e.session == session for e in entries):
            return True
    return False


# -- the sweep -------------------------------------------------------------------


@dataclass
class SweepResult:
    """What one :func:`sweep` did; counts only, never commands or output."""

    #: Owners whose lock was held by a live process (left alone).
    live_owners: int = 0
    #: Dead owners whose record was examined.
    dead_owners: int = 0
    #: Owner files that were malformed or unsafe (ignored, never signalled).
    invalid: int = 0
    #: Jobs seen in dead owners' records.
    jobs: int = 0
    #: Groups with no verifiable member left (nothing signalled).
    gone: int = 0
    #: Groups that exited after SIGTERM / needed SIGKILL / survived both.
    terminated: int = 0
    killed: int = 0
    failed: int = 0
    #: Groups that could not be checked (process listing failed or budget spent).
    unverified: int = 0
    notes: list[str] = field(default_factory=list)


def _alive(group: list[Proc], listing: dict[int, Proc]) -> list[Proc]:
    return [proc for proc in group if listing.get(proc.pid) == proc]


def _stop_group(
    entry: _Entry, *, grace_s: float, kill_wait_s: float, deadline: float
) -> str:
    """``gone|terminated|killed|failed|unverified`` for one recorded group.

    TERM needs a recorded member that still matches in a fresh listing; KILL needs
    a member of the group *as seen at TERM time* that still matches in another
    fresh listing taken just before it. The signal goes to the group id only while
    such a member proves the id has not been recycled.
    """
    if not entry.members:
        return "unverified"  # no identity captured: nothing can prove the group
    hard = deadline + _SWEEP_SLACK_S

    def ps() -> dict[int, Proc] | None:
        return _listing(timeout=min(_PS_TIMEOUT_S, max(hard - time.monotonic(), 0.2)))

    if deadline - time.monotonic() < _MIN_GROUP_S:
        return "unverified"  # too little budget to see the group through
    listing = ps()
    if listing is None:
        return "unverified"
    if not _alive(list(entry.members), listing):
        return "gone"
    uid = os.getuid()
    group = [p for p in listing.values() if p.pgid == entry.pgid and p.uid == uid]
    if entry.pgid == os.getpgrp() or not group:
        return "gone"
    try:
        os.killpg(entry.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return "failed"
    # Leave room for the KILL wait inside the budget (at most half of what is left).
    now = time.monotonic()
    reserve = min(kill_wait_s, max(deadline - now, 0.0) / 2)
    end = min(now + grace_s, deadline - reserve)
    while True:
        time.sleep(_POLL_S)
        listing = ps()
        if listing is None:
            return "failed"  # cannot re-verify, so no SIGKILL
        if not _alive(group, listing):
            return "terminated"
        if time.monotonic() >= end:
            break
    try:
        os.killpg(entry.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return "failed"
    end = min(time.monotonic() + kill_wait_s, hard)
    while time.monotonic() < end:
        time.sleep(_POLL_S)
        listing = ps()
        if listing is None:
            return "failed"
        if not _alive(group, listing):
            return "killed"
    return "failed"


def sweep(
    directory: Path,
    *,
    budget_s: float = _SWEEP_BUDGET_S,
    grace_s: float = 2.0,
    kill_wait_s: float = 2.0,
) -> SweepResult:
    """Stop the verified process groups of every **dead** owner in ``directory``.

    Blocking (sleeps while groups exit): call it from a worker thread. Live
    owners are skipped; malformed or foreign files are ignored; the whole call
    stops at ``budget_s``. A record is deleted only once every job in it is
    settled (gone, terminated or killed); anything unverified or still alive is
    kept so the next start tries again.
    """
    result = SweepResult()
    directory = Path(directory)
    deadline = time.monotonic() + max(budget_s, 0.0)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return result
    if not _dir_is_safe(directory):
        result.notes.append("owner directory is not a private directory; ignored")
        result.invalid += 1
        return result
    owners = [n[:-5] for n in names if n.endswith(".lock") and _OWNER_RE.fullmatch(n[:-5])]
    for owner_id in owners[:MAX_OWNERS]:
        if time.monotonic() >= deadline:
            result.notes.append("sweep budget spent")
            break
        lock_path = directory / f"{owner_id}.lock"
        fd = _open_safe(lock_path, os.O_RDWR)
        if fd is None:
            result.invalid += 1
            continue
        try:
            if not _lock_nb(fd):
                result.live_owners += 1
                continue
            if not _same_file(fd, lock_path):
                continue  # replaced or unlinked while we opened it: not that owner
            result.dead_owners += 1
            _sweep_owner(directory, owner_id, fd, result, grace_s, kill_wait_s, deadline)
        finally:
            os.close(fd)
    return result


def _sweep_owner(
    directory: Path,
    owner_id: str,
    lock_fd: int,
    result: SweepResult,
    grace_s: float,
    kill_wait_s: float,
    deadline: float,
) -> None:
    """Handle one owner whose lock this process holds."""
    record_path = directory / f"{owner_id}.json"
    raw = _read_record(record_path) if record_path.exists() or record_path.is_symlink() else {}
    entries = None if raw is None else (_parse_record(raw, owner_id) if raw else [])
    if entries is None:
        # Unreadable, oversized, foreign or malformed: nothing in it can be acted
        # on, and its owner is dead, so the files are only litter.
        result.invalid += 1
        _remove_owner(directory, owner_id, lock_fd)
        return
    settled = True
    for entry in entries:
        result.jobs += 1
        if time.monotonic() >= deadline:
            result.unverified += 1
            settled = False
            continue
        outcome = _stop_group(
            entry, grace_s=grace_s, kill_wait_s=kill_wait_s, deadline=deadline
        )
        if outcome == "gone":
            result.gone += 1
        elif outcome == "terminated":
            result.terminated += 1
        elif outcome == "killed":
            result.killed += 1
        elif outcome == "failed":
            result.failed += 1
            settled = False
        else:
            result.unverified += 1
            settled = False
    if settled:
        _remove_owner(directory, owner_id, lock_fd)


def _remove_owner(directory: Path, owner_id: str, lock_fd: int) -> None:
    """Delete a dead owner's files, only while its lock name is still our locked file."""
    if not _same_file(lock_fd, directory / f"{owner_id}.lock"):
        return
    for suffix in (".json", ".json.tmp", ".lock"):
        with contextlib.suppress(OSError):
            os.unlink(directory / f"{owner_id}{suffix}")
