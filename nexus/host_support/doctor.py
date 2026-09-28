"""Bounded, redacted aggregation of durable ``registry.mismatch`` events.

PLAN §15.5/§15.12 call for ``nexus doctor`` to surface accumulated catalogue
defects: the loop records a ``registry.mismatch`` whenever a provider rejects a
capability the registry claimed, and doctor reports them so a bad upstream entry
can be fixed. This module is the read half of that loop.

Design constraints (all defensive, because a session log is untrusted input):

* **Bounded and deterministic.** A workspace may hold thousands of sessions and
  arbitrarily large logs, so every read is capped: the ``max_sessions`` lowest
  session ids (chosen by sorting directory entry names, so selection does not
  depend on filesystem order), a bounded tail of each, and at most
  ``max_lines_per_log`` records. No unbounded file read, no unbounded whole-log
  tail, and no session handle is opened (so a health check never migrates,
  recovers, or writes).
* **Never raises.** An unreadable, symlinked, or interior-corrupt log is skipped
  rather than allowed to fail the report; malformed individual lines are skipped
  in place.
* **Redacted.** Only descriptive fields cross the boundary -- a count, grouped
  provider/model/reason tallies, and a bounded sample of ``session``/``seq``/
  ``ts`` plus those descriptive fields. The event's raw ``detail`` (which may
  echo a provider error) is deliberately dropped, and every surfaced string is
  passed through ``sanitize_text`` and ``redact_secrets`` for good measure.
"""
from __future__ import annotations

import math
import os
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from ..ext.quarantine import sanitize_text
from ..session.ids import is_valid_session_id
from ..util import redact_secrets

#: The durable event this module aggregates.
MISMATCH_EVENT = "registry.mismatch"

#: Sentinel for an absent descriptive field, so a group key is always present.
_MISSING = "(none)"

#: Sentinel that absorbs group overflow once ``max_groups`` distinct keys exist.
_OTHER = "(other)"

#: Longest descriptive field kept after sanitizing.
_FIELD_LIMIT = 120

#: Open flags that make a log read safe against a hostile path: never follow a
#: symlink and never block on a FIFO/device. ``O_NONBLOCK``/``O_NOFOLLOW`` are
#: missing on some platforms, so each contributes 0 there and the ``fstat``
#: regular-file check below remains the backstop.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)

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
    sessions_dir: object, *, limits: ScanLimits | None = None
) -> dict[str, Any]:
    """Aggregate ``registry.mismatch`` events from a sessions directory.

    Returns a counter-only, redacted summary. The result is always a well-formed
    dict: a missing/absent/inaccessible directory yields zeroed counters rather
    than an exception. ``sessions_dir`` is accepted as an opaque value so a
    facade can pass whatever its runtime exposes without this module importing a
    manager type.
    """
    limits = limits or ScanLimits()
    result: dict[str, Any] = {
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
    directory = _as_directory(sessions_dir)
    if directory is None or not _is_dir(directory):
        return result

    logs, truncated = _candidate_logs(directory, limits.max_sessions)
    result["truncated"] = truncated
    for session_id, path in logs:
        if _is_symlink(path):
            result["sessions_skipped"] += 1
            continue
        tail = _read_tail(path, limits.max_bytes_per_log)
        if tail is None:
            result["sessions_skipped"] += 1
            continue
        data, cut = tail
        if cut:
            result["truncated"] = True
        result["sessions_scanned"] += 1
        if _scan_session(session_id, data, limits, result):
            result["sessions_with_mismatches"] += 1
    return result


# -- discovery --------------------------------------------------------------


def _as_directory(value: object) -> Path | None:
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        return Path(value)
    return None


def _is_dir(directory: Path) -> bool:
    try:
        return directory.is_dir()
    except OSError:
        return False


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _candidate_logs(
    directory: Path, max_sessions: int
) -> tuple[list[tuple[str, Path]], bool]:
    """The first ``max_sessions`` ``(session_id, path)`` pairs, sorted by id.

    Enumeration reads directory entry *names* only (never file contents), then
    sorts the valid ids and slices, so which logs are examined is deterministic
    regardless of filesystem iteration order. The expensive bound -- reading at
    most ``max_sessions`` bounded tails -- is applied after selection, and
    ``truncated`` is true exactly when more valid logs existed than the cap.
    """
    if max_sessions < 1:
        return [], True
    found: list[tuple[str, Path]] = []
    try:
        for path in directory.iterdir():
            name = path.name
            if not name.endswith(".jsonl"):
                continue
            session_id = name[: -len(".jsonl")]
            if not is_valid_session_id(session_id):
                continue
            found.append((session_id, path))
    except OSError:
        return [], False
    found.sort(key=lambda item: item[0])
    truncated = len(found) > max_sessions
    if truncated:
        found = found[:max_sessions]
    return found, truncated


# -- reading ----------------------------------------------------------------


def _read_tail(path: Path, max_bytes: int) -> tuple[bytes, bool] | None:
    """A bounded tail of ``path``, or ``None`` when it is not a readable file.

    The path is opened by descriptor with ``O_NONBLOCK``/``O_NOFOLLOW`` (where
    available) so a symlink is refused rather than followed and a named pipe or
    device cannot block the open, and ``fstat`` confirms a regular file before a
    single byte is read. The size is taken from that descriptor, the tail is
    sought, and at most ``max_bytes`` bytes are read -- so a file that grows
    while being read can never push the read past the cap. The descriptor is
    always closed, and any ``OSError`` (a race that replaced the path, a
    permission denial) yields ``None`` rather than raising.

    Returns ``(data, cut)``. ``data`` starts on a line boundary (a leading
    partial line from the seek is dropped) and ``cut`` is ``True`` when the file
    was larger than the cap.
    """
    if max_bytes < 1:
        return None
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat_mod.S_ISREG(info.st_mode):
            return None
        size = info.st_size
        cut = size > max_bytes
        if cut:
            os.lseek(fd, size - max_bytes, os.SEEK_SET)
        data = _read_capped(fd, max_bytes)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - a descriptor close rarely fails
            pass
    if not cut:
        return data, False
    newline = data.find(b"\n")
    if newline == -1:
        return b"", True
    return data[newline + 1 :], True


def _read_capped(fd: int, limit: int) -> bytes:
    """Read at most ``limit`` bytes from ``fd``, looping over short reads."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


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
