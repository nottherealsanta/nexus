"""The "update available" notice (docs/release.md).

The daemon asks PyPI at most once a day which release of ``nexus-harness`` is
newest and every surface shows the answer; nothing is ever upgraded silently. The
answer, including a failed lookup, is cached in ``~/.nexus/cache/update-check.json``
so an offline machine does not retry on every start. Each newer release is
announced (a client toast) once, recorded in ``update-announced.json``. Only the version string
leaves this module: no credential, path, or workspace data is sent, and the
response is size-capped and parsed defensively.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from ..config.paths import nexus_home
from .install import PACKAGE, install_method, package_version

INDEX_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
#: A day between lookups; a failed lookup is remembered just as long.
CACHE_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 2.0
#: The package JSON lists every file of every release; anything past this is not it.
MAX_RESPONSE_BYTES = 256 * 1024
_CACHE_MAX_BYTES = 4 * 1024
_RELEASE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}$")
_MAX_RELEASE_LENGTH = 64


def cache_path(home: Path | str | None = None) -> Path:
    return nexus_home(home) / "cache" / "update-check.json"


def check_disabled_reason(
    environ: Mapping[str, str] | None = None, *, config_enabled: bool = True
) -> str | None:
    """Why the check is off, or ``None`` when it may run."""
    env = os.environ if environ is None else environ
    if env.get("NEXUS_NO_UPDATE_CHECK"):
        return "NEXUS_NO_UPDATE_CHECK is set"
    if env.get("CI"):
        return "running in CI"
    if not config_enabled:
        return "updates.check is false in config"
    if install_method() == "editable":
        return "editable install"
    return None


def _release_key(text: str) -> tuple[int, ...] | None:
    """Numeric key for a final release (``0.1.1``); ``None`` for anything else."""
    if len(text) > _MAX_RELEASE_LENGTH or not _RELEASE.fullmatch(text):
        return None
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def is_newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is a later final release than ``current``.

    A dev or pre-release ``current`` (``0.2.0.dev1``, ``0.2.0rc1``) has no numeric
    key, so it never claims an update.
    """
    new, old = _release_key(candidate), _release_key(current)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def newest_release(payload: Any) -> str | None:
    """The highest final, non-yanked release in a PyPI package JSON document."""
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, dict):
        return None
    best: tuple[tuple[int, ...], str] | None = None
    for name, files in releases.items():
        key = _release_key(name) if isinstance(name, str) else None
        if key is None or not isinstance(files, list) or not files:
            continue
        if not all(
            isinstance(file, dict) and isinstance(file.get("yanked"), bool)
            for file in files
        ):
            continue
        if all(file["yanked"] for file in files):
            continue
        if best is None or key > best[0]:
            best = (key, name)
    return best[1] if best else None


def latest_release(client: httpx.Client | None = None) -> str | None:
    """Ask PyPI for the newest release; ``None`` on any failure."""
    owns = client is None
    http = client or httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        with http.stream("GET", INDEX_URL) as response:
            if response.status_code != 200:
                return None
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    return None
        return newest_release(json.loads(bytes(body)))
    except (httpx.HTTPError, ValueError, OSError, RecursionError):
        return None
    finally:
        if owns:
            http.close()


def _read_cache(path: Path, now: float) -> tuple[bool, str | None]:
    """``(fresh, latest)`` from the cache; anything unreadable counts as stale."""
    try:
        with path.open("rb") as cache_file:
            raw = cache_file.read(_CACHE_MAX_BYTES + 1)
        if len(raw) > _CACHE_MAX_BYTES:
            return False, None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return False, None
        checked = data["checked_at"]
        latest = data.get("latest")
        if isinstance(checked, bool) or not isinstance(checked, (int, float)):
            return False, None
        try:
            checked = float(checked)
        except (OverflowError, ValueError):
            return False, None
        if not math.isfinite(checked) or checked < 0:
            return False, None
        if latest is not None and not (isinstance(latest, str) and _release_key(latest)):
            return False, None
        return 0 <= now - checked < CACHE_TTL_SECONDS, latest
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
        OverflowError,
        RecursionError,
    ):
        return False, None


def _write_cache(path: Path, latest: str | None, now: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as cache_file:
                json.dump({"checked_at": now, "latest": latest}, cache_file)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        pass  # a cache that cannot be written only costs another lookup


def update_status(
    *,
    home: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    config_enabled: bool = True,
    now: float | None = None,
    fetch=latest_release,
    current: str | None = None,
    cached_only: bool = False,
) -> dict[str, Any]:
    """The notice state: ``{enabled, current, latest, available, command}``.

    Blocking (one bounded HTTP request when the cache is stale); async callers run
    it in a worker thread. ``cached_only`` never touches the network and trusts
    the last answer however old (for ``nexus --version``, which has no config).
    ``available`` is the newer version or ``None``.
    """
    current = current or package_version()
    status: dict[str, Any] = {
        "enabled": False, "current": current, "latest": None,
        "available": None, "command": "nexus update",
    }
    if check_disabled_reason(environ, config_enabled=config_enabled) is not None:
        return status
    status["enabled"] = True
    moment = time.time() if now is None else now
    path = cache_path(home)
    fresh, latest = _read_cache(path, moment)
    if not fresh and not cached_only:
        fetched = fetch()
        if isinstance(fetched, str) and _release_key(fetched):
            latest = fetched
        # A failed or malformed lookup keeps the last good answer.
        _write_cache(path, latest, moment)
    status["latest"] = latest
    if latest and is_newer(latest, current):
        status["available"] = latest
    return status


def announced_path(home: Path | str | None = None) -> Path:
    return nexus_home(home) / "cache" / "update-announced.json"


def claim_announcement(available: str | None, *, home: Path | str | None = None) -> bool:
    """True exactly once per newer release: the first caller toasts it, later ones stay quiet.

    The announced version is recorded in ``~/.nexus/cache/update-announced.json`` so
    restarting a client does not repeat the toast. An unwritable record also answers
    False, so a broken cache never makes the toast appear on every start.
    """
    if not available or not _release_key(available):
        return False
    path = announced_path(home)
    try:
        with path.open("rb") as record:
            raw = record.read(_CACHE_MAX_BYTES + 1)
        data = json.loads(raw) if len(raw) <= _CACHE_MAX_BYTES else {}
        if isinstance(data, dict) and data.get("announced") == available:
            return False
    except (OSError, ValueError):
        pass  # missing or unreadable: not announced yet
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as record:
                json.dump({"announced": available}, record)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        return False
    return True


def notice(status: Mapping[str, Any]) -> str:
    """One line for a status area or the CLI; empty when nothing is available."""
    available = status.get("available")
    return f"{available} available: {status.get('command', 'nexus update')}" if available else ""
