"""ZMX selection and discovery. No CLI is probed until its user-only switch is on."""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigFile
from ..llmhub.providers import ProviderInfo
from .base import WorkerError, child_env, probe

IDS = ("claude-code", "agy")
LABELS = {"claude-code": "ZMX · Claude Code", "agy": "ZMX · Antigravity"}
DISCLOSURE = ("The CLI runs its own tools, permissions, settings, MCP servers and plugins. Nexus tool restrictions, "
              "permission rules and step limits do not apply. The CLI adds context Nexus cannot see; this is not its full model context.")
INFO = {pid: ProviderInfo(pid, LABELS[pid], "external-cli", f"{pid}://", "anthropic" if pid == "claude-code" else "", None, False)
        for pid in IDS}
_models: dict[str, tuple[float, list[str]]] = {}


def external(ref: str) -> bool:
    return ref.partition("/")[0] in IDS


def enabled(pid: str) -> bool:
    values = ConfigFile().read()["values"]["zmx"]
    return os.name == "posix" and (values["claude_backend"] == "zmx" if pid == "claude-code" else bool(values["antigravity"]))


def require(pid: str) -> None:
    if os.name != "posix":
        raise ValueError("ZMX requires POSIX PTYs/process groups; it is unavailable on this platform")
    if not enabled(pid):
        key = "zmx.claude_backend = 'zmx'" if pid == "claude-code" else "zmx.antigravity = true"
        raise ValueError(f"{LABELS[pid]} is disabled; enable {key} in user Settings first")


def child_ref(ref: str) -> str:
    if ref.startswith("claude/") and enabled("claude-code"):
        return "claude-code/" + ref.partition("/")[2]
    return ref


def cli_path(pid: str) -> str | None:
    if pid == "claude-code":
        from ..llmhub.auth.claude import cli_path as claude_path
        return claude_path()
    override = os.environ.get("NEXUS10_AGY_CLI")
    if override:
        return override
    return shutil.which("agy") or next((str(p) for p in (Path.home() / ".local/bin/agy", Path("/usr/local/bin/agy"),
                                                        Path("/opt/homebrew/bin/agy")) if p.is_file() and os.access(p, os.X_OK)), None)


def connected_ids() -> list[str]:
    return [pid for pid in IDS if enabled(pid) and cli_path(pid)]


def visible() -> dict[str, ProviderInfo]:
    return {pid: INFO[pid] for pid in IDS if enabled(pid)}


def facts(pid: str) -> dict:
    return {"provider": pid, "label": LABELS[pid], "context_complete": False, "disclosure": DISCLOSURE,
            "status": "not_started", "usage": None, "not_applied": ["Nexus permission rules", "disabled tools", "step limits"],
            "settings": "CLI inherited", "mcp": "CLI inherited"}


@dataclass
class ExternalProvider:
    name: str

    def body(self, req) -> dict:
        return {"backend": "external-cli", "model": req.model, "context_complete": False, "disclosure": DISCLOSURE,
                "instructions_appended": req.instructions, "messages_observed": len(req.messages), "tools": "CLI owned"}

    async def stream(self, req):
        raise ValueError("ZMX can only run as a guarded child session, never as a provider stream or one-shot")
        yield  # pragma: no cover


async def agy_models() -> list[str]:
    require("agy")
    cli = cli_path("agy")
    if not cli:
        return []
    cached = _models.get(cli)
    if cached and time.monotonic() - cached[0] < 60:
        return list(cached[1])
    try:
        output = await probe(cli, "models", env=child_env(), cwd=Path.home())
    except (OSError, WorkerError):
        return []
    ids = [line.strip() for line in output.splitlines() if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", line.strip())
           and line.strip() not in {"Models", "models"}]
    ids = list(dict.fromkeys(ids))[:200]
    _models[cli] = time.monotonic(), ids
    return ids
