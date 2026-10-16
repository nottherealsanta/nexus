"""Once-per-machine consent for GPT Live (GPT_LIVE_PLAN section 7).

Live sends microphone audio, summaries of the agent's work (which may include
file contents) and, at call start, a bounded excerpt of the current session's
conversation (user prompts and final assistant text) to OpenAI through the
user's ChatGPT account. A client shows that
notice before the first call and records the answer here, so it survives restarts
and applies to every client of the daemon. The record holds a timestamp and the
notice version only; revoking deletes it. Stored beside the other machine state
(``<nexus home>/live-consent.json``), written atomically with owner-only mode.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..config.paths import nexus_home

#: Bump when the notice text changes materially; an older record then counts as not given.
NOTICE_VERSION = 2  # 2: the call now also receives a bounded excerpt of the session conversation
_MAX_BYTES = 4096


def consent_path(home: Path | str | None = None) -> Path:
    return nexus_home(home) / "live-consent.json"


def read_consent(home: Path | str | None = None) -> str:
    """The ISO timestamp consent was given, or ``""`` when it was not (or is stale or unreadable)."""
    path = consent_path(home)
    try:
        if path.stat().st_size > _MAX_BYTES:
            return ""
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("version") != NOTICE_VERSION:
        return ""
    stamp = data.get("accepted_at")
    return stamp[:40] if isinstance(stamp, str) else ""


def write_consent(accepted: bool, home: Path | str | None = None) -> str:
    """Record (``True``) or revoke (``False``) consent; returns the timestamp or ``""``."""
    path = consent_path(home)
    if not accepted:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return ""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"version": NOTICE_VERSION, "accepted_at": stamp}, handle)
    os.replace(temporary, path)
    return stamp
