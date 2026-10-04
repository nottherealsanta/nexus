"""Bounded, redacted health projections for doctor.

PLAN §15.5/§15.12 call for ``nexus doctor`` to surface accumulated catalogue
defects: the loop records a ``registry.mismatch`` whenever a provider rejects a
capability the registry claimed, and doctor reports them so a bad upstream entry
can be fixed. STATE_PLAN §5.3 also calls for identifying legacy project
extensions. This module is the read half of those checks.

Design constraints (all defensive, because persisted records are untrusted input):

* **Bounded and deterministic.** A workspace may hold thousands of sessions
  and arbitrarily large histories, so every read is capped: the
  ``max_sessions`` lowest session ids (selected with SQL ``ORDER BY id``), a
  bounded tail of each, and at most ``max_lines_per_log`` records. No session
  handle is opened (so a health check never migrates, recovers, or writes).
* **Never raises.** An unavailable database or corrupt record is skipped rather
  than allowed to fail the report; malformed individual lines are skipped in
  place.
* **Redacted.** Only descriptive fields cross the boundary -- a count, grouped
  provider/model/reason tallies, and a bounded sample of ``session``/``seq``/
  ``ts`` plus those descriptive fields. The event's raw ``detail`` (which may
  echo a provider error) is deliberately dropped, and every surfaced string is
  passed through ``sanitize_text`` and ``redact_secrets`` for good measure.
"""
from __future__ import annotations

import contextlib
import math
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from ..ext.quarantine import sanitize_text
from ..session.ids import is_valid_session_id
from ..util import redact_secrets
from .git_head import git_head

#: The durable event this module aggregates.
MISMATCH_EVENT = "registry.mismatch"

#: Sentinel for an absent descriptive field, so a group key is always present.
_MISSING = "(none)"

#: Sentinel that absorbs group overflow once ``max_groups`` distinct keys exist.
_OTHER = "(other)"

#: Longest descriptive field kept after sanitizing.
_FIELD_LIMIT = 120

#: Every cap must be a positive integer; a zero/negative cap is a caller bug,
#: not a request for an unbounded scan, so construction rejects it.
_LIMIT_FIELDS = (
    "max_sessions",
    "max_bytes_per_log",
    "max_lines_per_log",
    "max_groups",
    "max_samples",
)


@dataclass(frozen=True)
class ScanLimits:
    """Hard caps on the doctor mismatch scan.

    ``max_sessions`` bounds how many session logs are examined, ``max_bytes_per_log``
    bounds the tail read of each, ``max_lines_per_log`` bounds parsed records,
    ``max_groups`` bounds distinct keys in each tally, and ``max_samples`` bounds
    the retained sample rows. Any cap that is hit sets ``truncated`` on the
    report so a caller can tell an incomplete scan from a clean one. Every cap
    must be a positive integer; a non-positive or non-integer value raises
    :class:`ValueError` so a zero cap can never silently mean "no bound".
    """

    max_sessions: int = 64
    max_bytes_per_log: int = 512 * 1024
    max_lines_per_log: int = 4000
    max_groups: int = 50
    max_samples: int = 25

    def __post_init__(self) -> None:
        for field in _LIMIT_FIELDS:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"ScanLimits.{field} must be a positive int, got {value!r}"
                )


def mismatch_summary(
    sessions: object, *, limits: ScanLimits | None = None
) -> dict[str, Any]:
    """Aggregate ``registry.mismatch`` events from a SQLite session store.

    Returns a counter-only, redacted summary. The result is always a well-formed
    dict: a missing, unsupported, or inaccessible source yields zeroed counters
    rather than an exception. A SQLite store is detected by its ``tail_bytes``
    and ``db`` attributes; this module deliberately does not import a concrete
    manager/store type.
    """
    limits = limits or ScanLimits()
    result = _empty_summary()
    if not _is_sqlite_store(sessions):
        return result
    _scan_sqlite_store(sessions, limits, result)
    return result


def database_diagnostics(sessions: object) -> dict[str, Any] | None:
    """Return bounded, redacted SQLite health metadata for a duck-typed store."""
    if not _is_sqlite_store(sessions):
        return None
    db = getattr(sessions, "db", None)
    path_value = getattr(db, "path", None)
    path = _as_path(path_value)
    if path is None:
        return {"path": "", "schema_user_version": None, "quick_check": "unavailable",
                "size_bytes": 0, "wal_size_bytes": 0}

    try:
        schema = getattr(db, "schema_version", None)
        schema_version = schema() if callable(schema) else None
        if type(schema_version) is not int or schema_version < 0:
            schema_version = None
    except Exception:  # noqa: BLE001 - diagnostics must never break doctor
        schema_version = None
    try:
        check = getattr(db, "quick_check", None)
        raw_check = check() if callable(check) else "unavailable"
        quick_check = "ok" if raw_check == "ok" else "issues"
    except Exception:  # noqa: BLE001 - diagnostics must never break doctor
        quick_check = "unavailable"

    size_bytes = _file_size(path)
    wal_size_bytes = _file_size(path.with_name(path.name + "-wal"))
    return {
        "path": _clean(str(path))[:240],
        "schema_user_version": schema_version,
        "quick_check": quick_check,
        "size_bytes": size_bytes,
        "wal_size_bytes": wal_size_bytes,
    }


_LEGACY_EXTENSION_ENTRIES = (
    "skills",
    "agents",
    "tools",
    "hooks",
    "providers",
    "mcp.json",
    "hooks.toml",
    "nexus.toml",
)


def legacy_extensions_pending(workspace: object) -> list[str]:
    """List documented legacy extension entries without reading their contents.

    The fixed allowlist bounds the result and avoids enumerating or traversing
    workspace-controlled directories. ``lstat`` recognizes an entry itself
    without following a symlink (including a symlinked ``.nexus`` root).
    """
    root = _as_path(workspace)
    if root is None:
        return []
    legacy = root / ".nexus"
    try:
        if not stat_mod.S_ISDIR(legacy.lstat().st_mode):
            return []
    except OSError:
        return []

    pending: list[str] = []
    for name in _LEGACY_EXTENSION_ENTRIES:
        try:
            (legacy / name).lstat()
        except OSError:
            continue
        pending.append(name)
    return pending


def doctor_report(
    runtime: object,
    *,
    list_sessions: Any,
    list_extensions: Any,
    explain_reload: bool = False,
) -> dict[str, Any]:
    """Assemble a bounded health projection without coupling host support to host."""
    report: dict[str, Any] = {
        "workspace": str(getattr(runtime, "workspace", "") or ""),
        # Lets a browser shorten the breadcrumb to ``~/…`` as the terminal does.
        "home": str(Path.home()),
        "git": git_head(getattr(runtime, "workspace", None)),
        "providers": _provider_report(runtime),
        "registry": _status_dict(
            getattr(getattr(runtime, "registry", None), "status", lambda: None)()
        ),
        "registry_mismatches": mismatch_summary(_session_source(runtime)),
        "sessions": len(list_sessions()),
        "legacy_extensions_pending": legacy_extensions_pending(
            getattr(runtime, "workspace", None)
        ),
    }
    report["agents_without_tiers"] = _agents_without_tiers(runtime)
    sessions = getattr(runtime, "sessions", None)
    database = database_diagnostics(getattr(sessions, "store", None))
    if database is not None:
        report["database"] = database
    mcp = _mcp_report(runtime)
    if mcp is not None:
        report["mcp"] = mcp
    extensions = getattr(runtime, "extensions", None)
    if extensions is not None:
        report["extensions"] = {
            "generation": getattr(extensions, "generation", 0),
            "loaded": len(list_extensions()),
            "diagnostics": [
                _asdict(item)
                for item in getattr(extensions, "diagnostics", lambda: ())()
            ],
        }
    if explain_reload:
        report["reload"] = _reload_boundary()
    return report


def _agents_without_tiers(runtime: object, *, limit: int = 32) -> list[str]:
    """Subagent roles that declare no ``tiers`` (a hint, never an error).

    Such a role keeps the earlier rule: its ``model`` or the parent's model.
    """
    agents = getattr(runtime, "agents", None)
    roles = getattr(agents, "agents", None)
    if roles is None:
        return []
    try:
        names = [
            str(role.name)
            for role in roles
            if role.eligible_in("subagent") and not role.tiers
        ]
    except Exception:  # noqa: BLE001 - a doctor hint must never break the report
        return []
    return sorted(names)[:limit]


def _session_source(runtime: object) -> object:
    sessions = getattr(runtime, "sessions", None)
    return getattr(sessions, "store", None)


def _provider_report(runtime: object) -> list[dict[str, Any]]:
    providers = getattr(runtime, "providers", {})
    return [
        {"name": name, "kind": type(providers[name]).__name__}
        for name in sorted(providers)
    ]


def _mcp_report(runtime: object) -> dict[str, Any] | None:
    """Point-in-time MCP server health, redacted and counter-only."""
    mcp = getattr(runtime, "mcp", None)
    statuses = getattr(mcp, "statuses", None)
    if mcp is None or not callable(statuses):
        return None
    try:
        servers = [_asdict(status) for status in statuses()]
        for row in servers:
            lookup = getattr(mcp, "server_snapshot", None)
            snapshot = lookup(row["name"]) if callable(lookup) else None
            row["tool_loading"] = getattr(snapshot, "tool_loading", "search")
    except Exception:  # noqa: BLE001 - health must never raise
        servers = []
    diagnostics = getattr(mcp, "diagnostics", None)
    rows: list[dict[str, Any]] = []
    if callable(diagnostics):
        with contextlib.suppress(Exception):
            rows = [_asdict(row) for row in (diagnostics() or ())]
    return {"servers": servers, "diagnostics": rows}


def _asdict(value: Any) -> dict[str, Any]:
    if isinstance(value, msgspec.Struct):
        return msgspec.structs.asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return dict(value)


def _status_dict(status: Any) -> dict[str, Any] | None:
    return _asdict(status) if status is not None else None


def _reload_boundary() -> dict[str, Any]:
    """The hot-vs-restart boundary, stated plainly (PLAN section 6.6)."""
    return {
        "hot": [
            ".agents/tools/*.py",
            ".agents/providers/*.py",
            ".agents/hooks/*.py",
            ".agents/skills/**/SKILL.md",
            ".agents/agents/*.md",
            ".agents/mcp.json",
            ".nexus/tools/*.py",
            ".nexus/providers/*.py",
            ".nexus/hooks/*.py",
            ".nexus/skills/**/SKILL.md",
            ".nexus/agents/*.md",
            ".nexus/mcp.json",
            "nexus.toml",
            "SOUL.md",
            "MEMORY.md",
        ],
        "restart_only": [
            "nexus/core/**",
            "nexus/model/message.py",
            "nexus/runtime.py",
            "the Manifest shape itself",
            "new pip installs",
        ],
        "note": (
            "Hot extensions swap at the next loop iteration in the same turn. "
            "Core source, the manifest shape, and new imports need a daemon "
            "restart; use `nexus daemon stop` and the next command auto-starts."
        ),
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "count": 0,
        "sessions_scanned": 0,
        "sessions_skipped": 0,
        "sessions_with_mismatches": 0,
        "truncated": False,
        "by_provider": {},
        "by_model": {},
        "by_reason": {},
        "by_session": {},
        "samples": [],
    }


def _is_sqlite_store(value: object) -> bool:
    try:
        return (
            callable(getattr(value, "tail_bytes", None))
            and getattr(value, "db", None) is not None
        )
    except Exception:  # noqa: BLE001 - unsupported adapters yield an empty report
        return False


def _scan_sqlite_store(
    store: object, limits: ScanLimits, result: dict[str, Any]
) -> None:
    """Scan at most ``max_sessions`` namespaced SQLite record tails."""
    try:
        db = getattr(store, "db", None)
        connection = getattr(db, "_connection", None)
        project_id = getattr(store, "project_id", None)
        namespace = getattr(store, "namespace", None)
        tail_bytes = getattr(store, "tail_bytes", None)
    except Exception:  # noqa: BLE001 - unsupported adapters yield zero counters
        return
    if (
        not callable(connection)
        or not isinstance(project_id, str)
        or not isinstance(namespace, str)
    ):
        return
    try:
        rows = connection().execute(
            "SELECT id FROM sessions WHERE project_id=? AND namespace=? "
            "AND trash_id IS NULL "
            "ORDER BY id LIMIT ?",
            (project_id, namespace, limits.max_sessions + 1),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a damaged/unavailable DB is best-effort
        return
    result["truncated"] = len(rows) > limits.max_sessions
    for row in rows[: limits.max_sessions]:
        try:
            session_id = row[0]
            if not isinstance(session_id, str) or not is_valid_session_id(session_id):
                result["sessions_skipped"] += 1
                continue
            data = tail_bytes(session_id, max_bytes=limits.max_bytes_per_log)
            if not isinstance(data, bytes):
                result["sessions_skipped"] += 1
                continue
        except Exception:  # noqa: BLE001 - malformed rows/read failures are skipped
            result["sessions_skipped"] += 1
            continue
        if len(data) >= limits.max_bytes_per_log:
            result["truncated"] = True
            # A bounded SQL tail can begin part-way through a JSON record.
            newline = data.find(b"\n")
            data = data[newline + 1 :] if newline >= 0 else b""
        result["sessions_scanned"] += 1
        if _scan_session(session_id, data, limits, result):
            result["sessions_with_mismatches"] += 1


def _as_path(value: object) -> Path | None:
    if isinstance(value, (str, Path)):
        return Path(value)
    return None


def _file_size(path: Path) -> int:
    try:
        return max(0, path.stat().st_size)
    except OSError:
        return 0


def _scan_session(
    session_id: str,
    data: bytes,
    limits: ScanLimits,
    result: dict[str, Any],
) -> int:
    """Fold one log tail into ``result``; return distinct mismatches found."""
    lines = data.splitlines()
    if len(lines) > limits.max_lines_per_log:
        result["truncated"] = True
        lines = lines[-limits.max_lines_per_log :]

    seen: set[str] = set()
    hits = 0
    for raw in lines:
        if not raw.strip():
            continue
        event = _decode_mismatch(raw)
        if event is None:
            continue
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id:
            if event_id in seen:
                continue
            seen.add(event_id)
        _record(session_id, event, limits, result)
        hits += 1
    return hits


def _decode_mismatch(raw: bytes) -> dict[str, Any] | None:
    """Decode one canonical ``{"type": "event", "event": {...}}`` mismatch line.

    Any other shape (a message/summary record, a non-dict, a raw event that is
    not a registry mismatch, or an undecodable line -- a crash tail counts) is
    ``None``. Nothing here can raise on hostile bytes.
    """
    try:
        record = msgspec.json.decode(raw)
    except Exception:  # noqa: BLE001 - a corrupt line is skipped, never fatal
        return None
    if not isinstance(record, dict) or record.get("type") != "event":
        return None
    event = record.get("event")
    if not isinstance(event, dict) or event.get("type") != MISMATCH_EVENT:
        return None
    return event


def _record(
    session_id: str,
    event: dict[str, Any],
    limits: ScanLimits,
    result: dict[str, Any],
) -> None:
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    provider = _clean(data.get("provider"))
    model = _clean(data.get("model"))
    reason = _clean(data.get("source") or data.get("reason") or "unknown")
    feature = _clean(data.get("feature"))

    result["count"] += 1
    _bump(result["by_provider"], provider or _MISSING, limits.max_groups, result)
    _bump(result["by_model"], model or _MISSING, limits.max_groups, result)
    _bump(result["by_reason"], reason, limits.max_groups, result)
    _bump(result["by_session"], session_id, limits.max_groups, result)

    sample: dict[str, Any] = {
        "session": session_id,
        "provider": provider,
        "model": model,
        "reason": reason,
        "feature": feature,
    }
    ts = _number(event.get("ts"))
    if ts is not None:
        sample["ts"] = ts
    seq = _integer(event.get("seq"))
    if seq is not None:
        sample["seq"] = seq
    result["samples"].append(sample)
    if len(result["samples"]) > limits.max_samples:
        result["samples"].pop(0)


# -- shaping ----------------------------------------------------------------


def _bump(
    tally: dict[str, int],
    key: str,
    max_groups: int,
    result: dict[str, Any],
) -> None:
    if key in tally:
        tally[key] += 1
    elif len(tally) < max_groups:
        tally[key] = 1
    else:
        result["truncated"] = True
        tally[_OTHER] = tally.get(_OTHER, 0) + 1


def _clean(value: object) -> str:
    """A bounded, control-free, credential-free single line (or ``""``)."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return redact_secrets(sanitize_text(text, limit=_FIELD_LIMIT))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


__all__ = ["MISMATCH_EVENT", "ScanLimits", "mismatch_summary"]
