"""Install, upgrade, and daemon-hygiene helpers (plans/install.md).

Everything here is client-side and local: it inspects how this copy of Nexus was
installed, finds ``uv``, enumerates every workspace daemon under
``~/.nexus/daemon/`` and stops them, and reports stale-daemon and duplicate-binary
problems. It never reads credentials and never signals a process it cannot
verify (stopping goes through :func:`nexus.host.daemon.stop`).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

InstallSource = Literal["pypi", "git", "path", "unknown"]

PACKAGE = "nexus-harness"
REPO_URL = "https://github.com/nottherealsanta/nexus"
#: ``uv-receipt.toml`` is tiny; anything larger is not one of ours.
_RECEIPT_MAX_BYTES = 64 * 1024
#: ``nexus update`` waits at most this long for one daemon to exit.
_STOP_POLLS = 100
_STOP_POLL_SECONDS = 0.1
#: An upgrade never runs longer than this (uv resolves and downloads).
UPDATE_TIMEOUT_SECONDS = 600


def package_version() -> str:
    try:
        from importlib.metadata import version

        return version(PACKAGE)
    except Exception:  # noqa: BLE001 - informational only
        return "0.0.0"


def _direct_url() -> dict[str, Any] | None:
    """The install's ``direct_url.json`` (PEP 610), or ``None`` when absent."""
    try:
        from importlib.metadata import distribution

        raw = distribution(PACKAGE).read_text("direct_url.json")
        data = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 - detection is best effort
        return None
    return data if isinstance(data, dict) else None


def install_method() -> str:
    """How this copy is managed: ``uv-tool``, ``editable`` or ``pip``."""
    data = _direct_url()
    if data and (data.get("dir_info") or {}).get("editable"):
        return "editable"
    return "uv-tool" if _is_uv_tool() else "pip"


def install_source() -> InstallSource:
    """Where this copy came from: an index (``pypi``), ``git``, a local ``path``."""
    data = _direct_url()
    if data is None:
        return "pypi"
    if "vcs_info" in data:
        return "git"
    dir_info = data.get("dir_info")
    if isinstance(dir_info, dict) and not dir_info.get("editable"):
        return "path"
    return "unknown"


def installed_extras() -> list[str]:
    """Extras the uv tool was installed with, from its ``uv-receipt.toml``."""
    receipt = Path(sys.prefix) / "uv-receipt.toml"
    try:
        if receipt.stat().st_size > _RECEIPT_MAX_BYTES:
            return []
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
        requirements = data["tool"]["requirements"]
        for req in requirements:
            if req.get("name") == PACKAGE:
                return sorted({str(e) for e in req.get("extras") or []})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return []


def _is_uv_tool() -> bool:
    parts = Path(sys.prefix).resolve().parts
    return "tools" in parts and "uv" in parts


def find_uv(environ: dict[str, str] | None = None) -> str | None:
    """The ``uv`` binary on ``PATH``, or in uv's usual install directories."""
    env = os.environ if environ is None else environ
    found = shutil.which("uv", path=env.get("PATH"))
    if found:
        return found
    home = Path(env.get("HOME") or Path.home())
    for directory in (
        env.get("UV_INSTALL_DIR"),
        env.get("XDG_BIN_HOME"),
        home / ".local" / "bin",
        home / ".cargo" / "bin",
    ):
        if not directory:
            continue
        candidate = Path(directory) / "uv"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def nexus_executables(path: str | None = None) -> list[str]:
    """Distinct ``nexus`` executables on ``PATH`` in resolution order."""
    seen: set[str] = set()
    found: list[str] = []
    for entry in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "nexus"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            real = str(candidate.resolve())
            if real not in seen:
                seen.add(real)
                found.append(str(candidate))
    return found


def _daemon_sockets(home: str | Path | None = None) -> list[Path]:
    from ..host.daemon import daemon_dir

    directory = daemon_dir(home)
    try:
        return sorted(directory.glob("*.sock"))
    except OSError:
        return []


async def running_daemons(home: str | Path | None = None) -> list[dict[str, Any]]:
    """Every live workspace daemon: socket, pid, workspace, and server version."""
    from ..host.daemon import DaemonUnavailable, TransportError, VersionMismatch
    from ..host.transports.uds import UDSClient

    found: list[dict[str, Any]] = []
    for socket in _daemon_sockets(home):
        try:
            client = await UDSClient.connect(socket, timeout=2.0)
        except (DaemonUnavailable, VersionMismatch, TransportError, OSError):
            continue
        try:
            info = client.info
            found.append(
                {
                    "socket": str(socket),
                    "pid": getattr(info, "pid", 0),
                    "workspace": getattr(info, "workspace", ""),
                    "server": getattr(info, "server", ""),
                }
            )
        finally:
            await client.close()
    return found


async def stop_all_daemons(home: str | Path | None = None) -> int:
    """Gracefully stop every live daemon; returns how many were stopped."""
    from ..host import daemon as daemon_mod

    stopped = 0
    for entry in await running_daemons(home):
        socket = Path(entry["socket"])
        try:
            was_running = await daemon_mod.stop(".", socket_path=socket)
        except Exception:  # noqa: BLE001 - an exiting daemon may drop the socket
            was_running = False
        if not was_running:
            continue
        stopped += 1
        for _ in range(_STOP_POLLS):
            try:
                report = await daemon_mod.status(".", socket_path=socket, timeout=0.5)
            except Exception:  # noqa: BLE001 - a closing socket may drop mid-call
                break
            if not report.get("running"):
                break
            await asyncio.sleep(_STOP_POLL_SECONDS)
    return stopped


async def install_report(home: str | Path | None = None) -> dict[str, Any]:
    """The doctor's install-hygiene section, with warnings spelled out."""
    version = package_version()
    warnings: list[str] = []
    executables = nexus_executables()
    if len(executables) > 1:
        warnings.append(
            "more than one `nexus` is on PATH ("
            + ", ".join(executables)
            + "); the first one wins. Remove the others."
        )
    stale = [
        d for d in await running_daemons(home) if d["server"] and d["server"] != version
    ]
    for daemon in stale:
        warnings.append(
            f"daemon for {daemon['workspace']} runs {daemon['server']} but this "
            f"client is {version}; run `nexus daemon restart`."
        )
    return {
        "version": version,
        "method": install_method(),
        "python": sys.executable,
        "executables": executables,
        "warnings": warnings,
    }


def _spec(extras: list[str], suffix: str = "") -> str:
    name = f"{PACKAGE}[{','.join(extras)}]" if extras else PACKAGE
    return f"{name}{suffix}"


def update_command(
    uv: str,
    source: str,
    *,
    extras: list[str],
    python: str,
    channel: str = "stable",
    version: str | None = None,
    ref: str | None = None,
) -> list[str] | None:
    """The uv invocation that updates this install, or ``None`` to refuse.

    ``python`` is ``"X.Y"``: never the path of the tool venv's own interpreter,
    because ``--force`` deletes that venv.
    """
    install = [uv, "tool", "install", "--force", "--python", python]
    if channel == "git":
        spec = _spec(extras, f" @ git+{REPO_URL}@{ref or 'main'}")
        return [*install, "--reinstall", spec]
    if version:
        return [*install, _spec(extras, f"=={version}")]
    if source == "pypi":
        return [uv, "tool", "upgrade", PACKAGE]
    if source == "git":  # migration: git installs move to PyPI releases
        return [*install[:4], "--refresh-package", PACKAGE, *install[4:], _spec(extras)]
    return None


VOICE_EXTRA = "voice"


def voice_runtime_installed() -> bool:
    """Whether the local dictation runtime (``kestrel``) is importable here."""
    import importlib
    import importlib.util

    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec("kestrel") is not None
    except (ImportError, ValueError):
        return False


def is_musl() -> bool:
    """musl libc (Alpine): the voice runtime publishes no wheels for it."""
    if not sys.platform.startswith("linux"):
        return False
    import platform

    if platform.libc_ver()[0] == "glibc":
        return False
    try:
        return any(Path("/lib").glob("ld-musl-*"))
    except OSError:
        return False


def voice_requirements() -> list[str]:
    """The ``voice`` extra's requirements, read from this install's metadata."""
    try:
        from importlib.metadata import requires

        entries = requires(PACKAGE) or []
    except Exception:  # noqa: BLE001 - metadata is best effort
        return []
    found: list[str] = []
    for entry in entries:
        requirement, _, marker = entry.partition(";")
        compact = marker.replace(" ", "").replace("'", '"')
        if f'extra=="{VOICE_EXTRA}"' in compact:
            found.append(requirement.strip())
    return found


def voice_install_command(
    uv: str | None,
    *,
    method: str,
    source: str,
    extras: list[str],
    python: str,
    version: str,
    direct_url: dict[str, Any] | None,
    executable: str,
) -> list[str] | None:
    """The command that adds the voice runtime to this install, or ``None``.

    A uv tool is reinstalled at the *same* version and source with ``voice``
    added to its extras, so ``nexus update`` keeps the runtime. Editable and
    pip installs get the extra's requirements installed into this interpreter.
    """
    if method == "uv-tool":
        if uv is None:
            return None
        spec_extras = sorted({*extras, VOICE_EXTRA})
        install = [uv, "tool", "install", "--force", "--python", python]
        url = str((direct_url or {}).get("url") or "")
        if source == "pypi":
            return [*install, _spec(spec_extras, f"=={version}")]
        if source == "git":
            commit = str(((direct_url or {}).get("vcs_info") or {}).get("commit_id") or "")
            if not url or not commit:
                return None
            return [*install, _spec(spec_extras, f" @ git+{url}@{commit}")]
        if source == "path" and url:
            return [*install, _spec(spec_extras, f" @ {url}")]
        return None
    requirements = voice_requirements()
    if not requirements:
        return None
    if uv is not None:
        return [uv, "pip", "install", "--python", executable, *requirements]
    return [executable, "-m", "pip", "install", *requirements]


def run_update(command: list[str]) -> int:
    """Run the upgrade, streaming uv's output. Returns uv's exit code."""
    try:
        return subprocess.run(command, check=False, timeout=UPDATE_TIMEOUT_SECONDS).returncode
    except subprocess.TimeoutExpired:
        return 124


def installed_version_after_update(binary: str | None = None) -> str:
    """Ask the freshly installed binary for its version (this process is stale)."""
    exe = binary or shutil.which("nexus") or "nexus"
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.rsplit(" ", 1)[-1] if out else ""
