"""Once-per-machine consent for GPT Live (GPT_LIVE_PLAN section 7).

Live sends microphone audio, summaries of the agent's work (which may include
file contents) and, at call start, a bounded excerpt of the current session's
conversation (user prompts and final assistant text) to OpenAI through the
user's ChatGPT account. A client shows that
notice before the first call and records the answer here, so it survives restarts
and applies to every client of the daemon. The record holds a timestamp and the
notice version only; revoking deletes it. Stored beside the other machine state
(``<nexus home>/live-consent.json``), written atomically with owner-only mode.

A call on the **manager** session (plan 02) is a wider disclosure: the manager reads
other sessions' titles, status and final reports, and the call carries them to
OpenAI. That is a second, separate notice (``scope="manager"``, versioned on its own
so the base consent already given stays valid for ordinary calls). It is recorded
in the same file, needs the base consent, and revoking it keeps the base.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..config.paths import nexus_home

#: Bump when the notice text changes materially; an older record then counts as not given.
NOTICE_VERSION = 2  # 2: the call now also receives a bounded excerpt of the session conversation
#: The cross-session (manager) notice, versioned independently of the base notice.
MANAGER_NOTICE_VERSION = 1
_MAX_BYTES = 4096
SCOPES = ("", "manager")


def consent_path(home: Path | str | None = None) -> Path:
    return nexus_home(home) / "live-consent.json"


def _read(home: Path | str | None) -> dict:
    path = consent_path(home)
    try:
        if path.stat().st_size > _MAX_BYTES:
            return {}
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != NOTICE_VERSION:
        return {}
    return data


def read_consent(home: Path | str | None = None, scope: str = "") -> str:
    """The ISO timestamp consent was given, or ``""`` when it was not (or is stale or unreadable).

    ``scope="manager"`` answers for the cross-session notice, which also needs the base consent.
    """
    data = _read(home)
    stamp = data.get("accepted_at")
    if not isinstance(stamp, str) or not stamp:
        return ""
    if scope == "manager":
        if data.get("manager_version") != MANAGER_NOTICE_VERSION:
            return ""
        manager_stamp = data.get("manager_accepted_at")
        return manager_stamp[:40] if isinstance(manager_stamp, str) else ""
    return stamp[:40]


def write_consent(accepted: bool, home: Path | str | None = None, scope: str = "") -> str:
    """Record (``True``) or revoke (``False``) consent; returns the timestamp or ``""``.

    Recording the manager scope records the base consent too (the manager notice
    includes it). Revoking the base consent deletes the file, manager scope with it;
    revoking only the manager scope keeps the base record.
    """
    if scope not in SCOPES:
        raise ValueError("unknown consent scope")
    path = consent_path(home)
    data = _read(home)
    if not accepted:
        if scope == "manager" and data:
            data.pop("manager_version", None)
            data.pop("manager_accepted_at", None)
            _write(path, data)
            return ""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return ""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    base = data.get("accepted_at")
    record = {"version": NOTICE_VERSION, "accepted_at": base if scope == "manager" and base else stamp}
    if scope == "manager":
        record["manager_version"] = MANAGER_NOTICE_VERSION
        record["manager_accepted_at"] = stamp
        _write(path, record)
        return stamp
    # Re-accepting the base notice keeps an existing manager record.
    for key in ("manager_version", "manager_accepted_at"):
        if key in data:
            record[key] = data[key]
    _write(path, record)
    return stamp


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)
    os.replace(temporary, path)
