"""Persistent gateway state: allowed workspaces, paired devices, pairing codes.

One JSON file in a ``0700`` directory under the Nexus home, replaced atomically
and mutated only under an exclusive ``flock`` so the CLI and a running gateway
never lose each other's updates. Secrets are never stored: a pairing code and a
device token are kept only as SHA-256 digests, and the token is shown once.
Readers cache by ``(inode, mtime, size)`` so per-request and per-second stream
rechecks cost one ``stat`` while a revoke or disallow made by another process is
seen on the next check.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config.paths import nexus_home, project_key

SCOPES: tuple[str, ...] = ("read", "prompt", "approve", "voice", "live")
MAX_DEVICES = 16
MAX_PENDING = 8
MAX_HOSTS = 32
MAX_TTL_DAYS = 90
DEFAULT_TTL_DAYS = 90
DEFAULT_CODE_TTL = 120.0
MAX_CODE_TTL = 600.0
MAX_NAME = 64
HOST_ID = re.compile(r"[0-9a-f]{16}\Z")
_STATE_VERSION = 1


class GatewayError(Exception):
    """A gateway state operation was refused; the message is safe to show."""


class PairingError(GatewayError):
    """A pairing code was unknown, expired, replayed, or the device cap was hit."""


@dataclass(frozen=True, slots=True)
class HostEntry:
    host_id: str
    workspace: str
    label: str


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    name: str
    scopes: tuple[str, ...]
    host_ids: tuple[str, ...]
    created_at: float
    expires_at: float


def host_id_for(path: Path | str) -> str:
    """The opaque host id: the same digest the daemon socket name uses."""
    return project_key(path)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def clean_name(value: object, fallback: str = "device") -> str:
    text = "".join(ch for ch in str(value) if ch.isprintable()).strip()[:MAX_NAME]
    return text or fallback


def parse_scopes(value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else list(value)
    scopes = [item.strip() for item in items if item.strip()]
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown or not scopes:
        raise GatewayError(f"scopes must be a non-empty subset of {', '.join(SCOPES)}")
    return tuple(scope for scope in SCOPES if scope in scopes)


class GatewayStore:
    """The file-backed registry; safe across threads and processes."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root) if root is not None else nexus_home() / "gateway"
        self._clock = clock
        self._state_path = self.root / "state.json"
        self._lock_path = self.root / "state.lock"
        self._serve_lock_path = self.root / "serve.lock"
        self._thread_lock = threading.RLock()
        self._cache_key: tuple[int, int, int] | None = None
        self._cache: dict[str, Any] | None = None

    # -- filesystem --------------------------------------------------------

    def _ensure_dir(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            info = os.lstat(self.root)
        except OSError as exc:
            raise GatewayError("cannot prepare the gateway directory") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise GatewayError("the gateway directory must be a real directory")
        if info.st_uid != os.geteuid():
            raise GatewayError("the gateway directory is owned by another user")
        if info.st_mode & 0o077:
            os.chmod(self.root, 0o700)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self._ensure_dir()
            fd = os.open(
                self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _load(self, *, strict_mode: bool = True) -> dict[str, Any]:
        """Read the state file (cached by stat); a missing file is empty state."""
        with self._thread_lock:
            try:
                info = os.lstat(self._state_path)
            except FileNotFoundError:
                self._cache_key = None
                self._cache = None
                return _empty_state()
            except OSError as exc:
                raise GatewayError("cannot read gateway state") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise GatewayError("gateway state file is not trusted")
            if strict_mode and info.st_mode & 0o077:
                raise GatewayError("gateway state file permissions are too open")
            key = (info.st_ino, info.st_mtime_ns, info.st_size)
            if self._cache is not None and key == self._cache_key:
                return self._cache
            try:
                fd = os.open(self._state_path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as handle:
                    data = json.loads(handle.read(4 * 1024 * 1024))
            except (OSError, ValueError) as exc:
                raise GatewayError("gateway state is unreadable") from exc
            state = _validate_state(data)
            self._cache_key, self._cache = key, state
            return state

    def _write(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        tmp = self.root / f".state.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        self._cache_key = None
        self._cache = None

    def _mutate(self, change: Callable[[dict[str, Any], float], Any]) -> Any:
        with self._locked():
            state = json.loads(json.dumps(self._load()))  # private deep copy
            if not state["gateway_id"]:
                state["gateway_id"] = "gw_" + secrets.token_hex(8)
            now = self._clock()
            _purge(state, now)
            result = change(state, now)
            self._write(state)
            return result

    # -- identity ----------------------------------------------------------

    @property
    def gateway_id(self) -> str:
        state = self._load()
        if state["gateway_id"]:
            return state["gateway_id"]
        return self._mutate(lambda state, now: state["gateway_id"])

    # -- hosts -------------------------------------------------------------

    def hosts(self) -> list[HostEntry]:
        return [_host(h, v) for h, v in sorted(self._load()["hosts"].items())]

    def host(self, host_id: str) -> HostEntry | None:
        entry = self._load()["hosts"].get(host_id)
        return _host(host_id, entry) if entry else None

    def allow(self, path: Path | str) -> HostEntry:
        """Allow one existing local directory; never called with network input."""
        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise GatewayError("the workspace path does not exist") from exc
        if not resolved.is_dir():
            raise GatewayError("the workspace path is not a directory")
        host_id = host_id_for(resolved)

        def change(state: dict[str, Any], now: float) -> HostEntry:
            if host_id not in state["hosts"] and len(state["hosts"]) >= MAX_HOSTS:
                raise GatewayError(f"at most {MAX_HOSTS} workspaces may be allowed")
            state["hosts"][host_id] = {
                "workspace": str(resolved),
                "label": clean_name(resolved.name or str(resolved), "workspace"),
            }
            return _host(host_id, state["hosts"][host_id])

        return self._mutate(change)

    def disallow(self, target: str) -> HostEntry | None:
        """Remove a workspace by id or path and strip it from every device."""
        host_id = target
        if not HOST_ID.fullmatch(target):
            try:
                host_id = host_id_for(Path(target).expanduser().resolve())
            except OSError:
                return None

        def change(state: dict[str, Any], now: float) -> HostEntry | None:
            entry = state["hosts"].pop(host_id, None)
            if entry is None:
                return None
            for device in state["devices"].values():
                device["host_ids"] = [h for h in device["host_ids"] if h != host_id]
            return _host(host_id, entry)

        return self._mutate(change)

    # -- pairing -----------------------------------------------------------

    def create_pairing(
        self,
        *,
        name: str = "device",
        scopes: str | list[str] | tuple[str, ...] = SCOPES,
        ttl_days: int = DEFAULT_TTL_DAYS,
        host_ids: list[str] | tuple[str, ...] | None = None,
        code_ttl: float = DEFAULT_CODE_TTL,
    ) -> tuple[str, float]:
        """Mint a single-use pairing code bound to the hosts allowed right now."""
        granted = parse_scopes(scopes)
        if not 1 <= int(ttl_days) <= MAX_TTL_DAYS:
            raise GatewayError(f"ttl must be 1 to {MAX_TTL_DAYS} days")
        if not 1 <= float(code_ttl) <= MAX_CODE_TTL:
            raise GatewayError(f"the code lifetime must be 1 to {int(MAX_CODE_TTL)} seconds")
        code = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")

        def change(state: dict[str, Any], now: float) -> float:
            allowed = sorted(state["hosts"])
            chosen = sorted(set(host_ids)) if host_ids else allowed
            if not chosen:
                raise GatewayError("no workspace is allowed; run `nexus gateway allow PATH` first")
            missing = [h for h in chosen if h not in state["hosts"]]
            if missing:
                raise GatewayError("host is not allowed: " + ", ".join(missing))
            if len(state["pending"]) >= MAX_PENDING:
                raise GatewayError("too many pending pairing codes; wait or let them expire")
            if len(state["devices"]) + len(state["pending"]) >= MAX_DEVICES:
                raise GatewayError(f"at most {MAX_DEVICES} devices may be paired; revoke one first")
            expires = now + float(code_ttl)
            state["pending"][hash_secret(code)] = {
                "name": clean_name(name),
                "scopes": list(granted),
                "host_ids": chosen,
                "ttl_days": int(ttl_days),
                "expires_at": expires,
            }
            return expires

        expires_at = self._mutate(change)
        return "-".join(code[i : i + 5] for i in range(0, len(code), 5)), expires_at

    def redeem(self, code: str, device_name: str = "") -> tuple[Device, str]:
        """Atomically consume ``code`` and issue a device token (shown once)."""
        key = hash_secret(normalize_code(code)) if isinstance(code, str) else ""

        def change(state: dict[str, Any], now: float) -> tuple[Device, str]:
            pending = state["pending"].get(key)
            if pending is None:
                raise PairingError("invalid or expired pairing code")
            if len(state["devices"]) >= MAX_DEVICES:
                raise PairingError("the device limit has been reached")
            del state["pending"][key]
            token = secrets.token_urlsafe(32)
            device_id = "dev_" + secrets.token_hex(8)
            hosts = [h for h in pending["host_ids"] if h in state["hosts"]]
            record = {
                "name": clean_name(device_name, pending["name"]),
                "token_sha256": hash_secret(token),
                "scopes": pending["scopes"],
                "host_ids": hosts,
                "created_at": now,
                "expires_at": now + pending["ttl_days"] * 86400.0,
            }
            state["devices"][device_id] = record
            return _device(device_id, record), token

        return self._mutate(change)

    # -- devices -----------------------------------------------------------

    def authenticate(self, token: str) -> Device | None:
        """The device owning ``token``, or ``None`` (unknown, revoked, expired)."""
        if not token or len(token) > 256:
            return None
        try:
            state = self._load()
        except GatewayError:
            return None
        digest = hash_secret(token)
        found: str | None = None
        for device_id, record in state["devices"].items():
            if hmac.compare_digest(record["token_sha256"], digest):
                found = device_id
        if found is None:
            return None
        device = _device(found, state["devices"][found])
        return device if device.expires_at > self._clock() else None

    def check_device(
        self, device_id: str, *, host_id: str | None = None, scope: str | None = None
    ) -> Device | None:
        """Recheck a device against current file state (revoke/disallow/expiry)."""
        try:
            state = self._load()
        except GatewayError:
            return None
        record = state["devices"].get(device_id)
        if record is None:
            return None
        device = _device(device_id, record)
        if device.expires_at <= self._clock():
            return None
        if host_id is not None and (
            host_id not in device.host_ids or host_id not in state["hosts"]
        ):
            return None
        if scope is not None and scope not in device.scopes:
            return None
        return device

    def devices(self) -> list[Device]:
        state = self._load()
        return sorted(
            (_device(i, r) for i, r in state["devices"].items()),
            key=lambda device: device.created_at,
        )

    def pending_count(self) -> int:
        now = self._clock()
        return sum(1 for r in self._load()["pending"].values() if r["expires_at"] > now)

    def revoke(self, device_id: str) -> bool:
        def change(state: dict[str, Any], now: float) -> bool:
            return state["devices"].pop(device_id, None) is not None

        return self._mutate(change)

    # -- single gateway per machine ----------------------------------------

    def acquire_serve_lock(self) -> int:
        """Hold the serve lock for the process lifetime; raises if one is running."""
        self._ensure_dir()
        fd = os.open(self._serve_lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise GatewayError("another gateway is already serving this Nexus home") from exc
        return fd

    def serving(self) -> bool:
        try:
            fd = self.acquire_serve_lock()
        except GatewayError:
            return True
        os.close(fd)
        return False


# ---------------------------------------------------------------------------
# State shape
# ---------------------------------------------------------------------------


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "gateway_id": "", "hosts": {}, "devices": {}, "pending": {}}


def _host(host_id: str, entry: dict[str, Any]) -> HostEntry:
    return HostEntry(host_id, entry["workspace"], entry["label"])


def _device(device_id: str, record: dict[str, Any]) -> Device:
    return Device(
        device_id=device_id,
        name=record["name"],
        scopes=tuple(record["scopes"]),
        host_ids=tuple(record["host_ids"]),
        created_at=float(record["created_at"]),
        expires_at=float(record["expires_at"]),
    )


def _purge(state: dict[str, Any], now: float) -> None:
    state["pending"] = {k: v for k, v in state["pending"].items() if v["expires_at"] > now}
    state["devices"] = {k: v for k, v in state["devices"].items() if v["expires_at"] > now}


def _validate_state(data: object) -> dict[str, Any]:
    """Fail closed on a malformed file rather than guessing what it meant."""
    try:
        assert isinstance(data, dict) and data.get("version") == _STATE_VERSION
        gateway_id = data["gateway_id"]
        assert isinstance(gateway_id, str)
        hosts, devices, pending = data["hosts"], data["devices"], data["pending"]
        assert isinstance(hosts, dict) and isinstance(devices, dict) and isinstance(pending, dict)
        for host_id, entry in hosts.items():
            assert HOST_ID.fullmatch(host_id)
            assert isinstance(entry["workspace"], str) and isinstance(entry["label"], str)
        for device_id, record in devices.items():
            assert isinstance(device_id, str) and isinstance(record["name"], str)
            assert isinstance(record["token_sha256"], str) and len(record["token_sha256"]) == 64
            assert isinstance(record["scopes"], list) and set(record["scopes"]) <= set(SCOPES)
            assert isinstance(record["host_ids"], list)
            assert all(isinstance(h, str) for h in record["host_ids"])
            float(record["created_at"]), float(record["expires_at"])
        for key, record in pending.items():
            assert isinstance(key, str) and isinstance(record["name"], str)
            assert isinstance(record["scopes"], list) and isinstance(record["host_ids"], list)
            assert isinstance(record["ttl_days"], int)
            float(record["expires_at"])
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise GatewayError("gateway state is malformed") from exc
    return data


__all__ = [
    "DEFAULT_CODE_TTL",
    "DEFAULT_TTL_DAYS",
    "HOST_ID",
    "MAX_DEVICES",
    "MAX_HOSTS",
    "MAX_PENDING",
    "MAX_TTL_DAYS",
    "SCOPES",
    "Device",
    "GatewayError",
    "GatewayStore",
    "HostEntry",
    "PairingError",
    "clean_name",
    "hash_secret",
    "host_id_for",
    "normalize_code",
    "parse_scopes",
]
