"""`nexus doctor`: what is wrong with this install, in one list.

Each check returns a row `{name, status, detail}` with status `ok`, `warn` or
`fail`. A `fail` makes the command exit 1; a `warn` is something optional that
is missing. Checks never start the daemon, load a model or download anything.
"""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from . import daemon
from .config import ConfigFile
from .llmhub import catalog
from .llmhub.hub import connected
from .llmhub.providers import PROVIDERS

Row = dict[str, str]
CATALOG_STALE_S = 7 * 24 * 3600


def source_fingerprint() -> str:
    """Hash of every `nexus/**/*.py`: a daemon started from other code differs."""
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _row(name: str, status: str, detail: str) -> Row:
    return {"name": name, "status": status, "detail": detail}


def _get(found: dict[str, Any], path: str) -> Any:
    response = httpx.get(f"{found['url']}{path}", headers={"authorization": f"Bearer {found['token']}"}, timeout=5)
    response.raise_for_status()
    return response.json()


def check_daemon(found: dict[str, Any] | None) -> Row:
    if found is None:
        return _row("daemon", "warn", "not running (`nexus daemon start`)")
    try:
        info = _get(found, "/api/info")
    except (httpx.HTTPError, ValueError) as exc:
        return _row("daemon", "fail", f"{found['url']} answered badly: {exc}")
    running, disk = info.get("code"), source_fingerprint()
    where = f"{found['url']} pid {info['pid']} v{info['version']}"
    if running != disk:
        return _row("daemon", "fail", f"{where} runs other code than the checkout (restart: `nexus daemon stop`)")
    return _row("daemon", "ok", f"{where}, code {disk}")


def check_sign_ins() -> Row:
    live = [pid for pid in connected() if pid != "mock"]
    if not live:
        return _row("sign-ins", "fail", f"no provider connected (`nexus login {next(iter(PROVIDERS))}`)")
    return _row("sign-ins", "ok", ", ".join(live))


def check_catalog() -> Row:
    path = catalog.cache_path()
    if not path.exists():
        return _row("catalog", "warn", "no models.dev cache yet; the next `nexus models` downloads it")
    age = time.time() - path.stat().st_mtime
    text = f"models.dev cache {int(age // 3600)} h old"
    return _row("catalog", "warn" if age > CATALOG_STALE_S else "ok", text)


def check_config() -> Row:
    errors = ConfigFile().read()["errors"]
    if errors:
        return _row("config", "fail", "; ".join(errors[:3]) + (f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""))
    return _row("config", "ok", str(ConfigFile().path))


def check_search() -> Row:
    url = ConfigFile().read()["values"]["web"]["search_url"]
    try:
        response = httpx.get(url, params={"q": "nexus", "format": "json"}, timeout=3)
    except httpx.HTTPError:
        return _row("search", "warn", f"nothing answers at {url} (web_search is off; see nexus-search/README.md)")
    return _row("search", "ok" if response.status_code == 200 else "warn", f"{url} HTTP {response.status_code}")


def check_mcp(found: dict[str, Any] | None) -> Row:
    if found is None:
        return _row("mcp", "warn", "daemon not running, servers not checked")
    try:
        servers = _get(found, "/api/mcp")["servers"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return _row("mcp", "fail", str(exc))
    bad = [f"{s['server']} ({s['state']}: {s.get('error') or '?'})" for s in servers if s["state"] in ("failed", "invalid")]
    if bad:
        return _row("mcp", "fail", "; ".join(bad))
    return _row("mcp", "ok", f"{len(servers)} server(s)" if servers else "none configured")


def check_voice() -> list[Row]:
    from .voice import status
    rows = []
    for name, entry in status.status().items():
        state = entry["state"]
        level = "ok" if state == "ready" else "fail" if state == "error" else "warn"
        detail = f"{state} ({entry['repo']})" + (f": {entry['error']}" if entry["error"] else "")
        rows.append(_row(f"voice {name}", level, detail))
        rows.append(_row(f"hf cache {name}", "ok" if state in ("ready", "runtime_missing") else "warn",
                         f"{entry['cache_path']}" + ("" if state in ("ready", "runtime_missing") else f" ({state})")))
    return rows


def check_rg() -> Row:
    path = shutil.which("rg")
    if path:
        return _row("rg", "ok", path)
    return _row("rg", "warn", "not installed; grep falls back to a slower scan")


def run() -> list[Row]:
    found = daemon.discover()
    return [check_daemon(found), check_sign_ins(), check_catalog(), check_config(), check_search(),
            check_mcp(found), *check_voice(), check_rg()]


RELEASES_URL = "https://api.github.com/repos/nottherealsanta/nexus/releases/latest"


def _version_key(text: str) -> tuple[tuple[int, ...], int]:
    """`0.10.0.dev0` sorts before `0.10.0`; anything else non-numeric is ignored."""
    core, dev, _ = text.lstrip("v").lower().partition("dev")
    numbers = tuple(int(x) for x in core.replace("-", ".").split(".") if x.isdigit())
    return numbers, 0 if dev else 1


def update_check(current: str) -> dict[str, Any]:
    """Compare with the latest GitHub release. Reports only; never installs."""
    try:
        response = httpx.get(RELEASES_URL, headers={"accept": "application/vnd.github+json"}, timeout=10)
    except httpx.HTTPError as exc:
        return {"current": current, "latest": None, "state": "unknown", "detail": f"could not reach GitHub: {exc}"}
    if response.status_code == 404:
        return {"current": current, "latest": None, "state": "none", "detail": "no release has been published yet"}
    if response.status_code != 200:
        return {"current": current, "latest": None, "state": "unknown", "detail": f"GitHub answered HTTP {response.status_code}"}
    release = response.json()
    latest = str(release.get("tag_name", "")).lstrip("v")
    newer = _version_key(latest) > _version_key(current)
    return {"current": current, "latest": latest, "url": release.get("html_url"),
            "state": "available" if newer else "current",
            "detail": f"{latest} is available" if newer else f"{current} is the latest release"}
