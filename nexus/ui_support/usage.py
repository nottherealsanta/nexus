"""Provider usage formatting shared by surfaces (``ProvidersUsageResult``).

Pure functions: the native usage panel (``ui/ratatui/actions.py``, also the
desktop client's ``/usage``) renders these strings, so every surface words a limit
identically: ``38% used · 12% in reserve · resets in 6d 5h (Wed Oct 21 14:15)``.

**Reserve** (weekly and monthly windows only, when the host reports
``resets_at`` and ``period_seconds``): the share of the window already elapsed
minus the share used. Half the month gone with 20% used leaves 30% in reserve:
usage that can still be spent while staying on an even pace. A negative value
reads ``N% over pace``. The bar marks the even-pace point with ``┃``.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

BAR_WIDTH = 24
#: Used-percent thresholds for the bar tone: ``ok`` below 70, ``warn`` below 90.
WARN_AT, CRITICAL_AT = 70.0, 90.0
#: Thin bar glyphs: used, remaining, and the even-pace marker.
FILLED, TRACK, PACE = "━", "─", "┃"
#: Reserve is shown for windows at least a week long (weekly and monthly).
RESERVE_MIN_PERIOD = 7 * 86_400
RESERVE_NOTE = "Reserve: share of the week or month elapsed minus share used, i.e. usage you can still spend and stay on pace."


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def used_percent(window: Any) -> float | None:
    value = _field(window, "used_percent")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(0.0, min(float(value), 100.0))


def tone(window: Any) -> str:
    """``ok``, ``warn``, ``critical`` or ``unknown`` for colouring a bar."""
    used = used_percent(window)
    if used is None:
        return "unknown"
    return "critical" if used >= CRITICAL_AT else "warn" if used >= WARN_AT else "ok"


def percent(value: float) -> str:
    """Whole percents, except near the ends: ``0.2%`` used must not read as ``100%`` left."""
    return f"{value:.0f}%" if value == int(value) or 10 <= value <= 90 else f"{value:.1f}%"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def elapsed_percent(window: Any, now: float | None = None) -> float | None:
    """How much of a weekly or monthly window has passed, or ``None``."""
    resets_at, period = _number(_field(window, "resets_at")), _number(_field(window, "period_seconds"))
    if resets_at is None or period is None or period < RESERVE_MIN_PERIOD:
        return None
    now = time.time() if now is None else now
    if resets_at <= now:
        return None
    return max(0.0, min((1 - (resets_at - now) / period) * 100, 100.0))


def reserve(window: Any, now: float | None = None) -> float | None:
    """Elapsed share minus used share: positive is spare usage, negative is ahead of pace."""
    used, elapsed = used_percent(window), elapsed_percent(window, now)
    return None if used is None or elapsed is None else elapsed - used


def reserve_phrase(window: Any, now: float | None = None) -> str:
    """``30% in reserve``, ``on pace`` or ``12% over pace``; ``""`` when not estimable."""
    value = reserve(window, now)
    if value is None:
        return ""
    rounded = round(value)
    if rounded == 0:
        return "on pace"
    return f"{rounded}% in reserve" if rounded > 0 else f"{-rounded}% over pace"


def bar(window: Any, width: int = BAR_WIDTH, now: float | None = None) -> str:
    """A thin fixed-width bar: ``━`` used, ``─`` remaining, ``┃`` at the even-pace point."""
    used = used_percent(window)
    filled = 0 if used is None else min(width, max(1 if used > 0 else 0, round(used / 100 * width)))
    cells = [FILLED] * filled + [TRACK] * (width - filled)
    elapsed = elapsed_percent(window, now)
    if elapsed is not None:
        cells[min(width - 1, int(elapsed / 100 * width))] = PACE
    return "".join(cells)


def duration(seconds: float) -> str:
    """``42m``, ``3h 5m``, ``6d 5h``; ``now`` under a minute."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "now"
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def reset_phrase(window: Any, now: float | None = None) -> str:
    """When the window resets, relative and absolute; ``""`` when unknown."""
    resets_at = _field(window, "resets_at")
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) and resets_at > 0:
        now = time.time() if now is None else now
        when = datetime.fromtimestamp(resets_at)
        same_day = when.date() == datetime.fromtimestamp(now).date()
        stamp = when.strftime("%H:%M") if same_day else f"{when:%a %b} {when.day} {when:%H:%M}"
        if resets_at <= now:
            return f"reset due ({stamp})"
        return f"resets in {duration(resets_at - now)} ({stamp})"
    text = _field(window, "reset_text") or ""
    return f"resets {text}" if text else ""


def summary(window: Any, now: float | None = None) -> str:
    """``38% used · 12% in reserve · resets in 6d 5h (Wed Oct 21 14:15) · 14,979 of 15,000 left``.

    The remaining share is the bar's track, so it is not repeated as text.
    """
    used = used_percent(window)
    parts = ["usage unknown"] if used is None else [f"{percent(used)} used"]
    if phrase := reserve_phrase(window, now):
        parts.append(phrase)
    if phrase := reset_phrase(window, now):
        parts.append(phrase)
    if detail := _field(window, "detail"):
        parts.append(str(detail))
    return " · ".join(parts)


def heading(row: Any) -> str:
    """``ChatGPT (Codex) · Plus``."""
    label = str(_field(row, "label", "") or _field(row, "id", "") or "?")
    plan = _field(row, "plan") or ""
    return f"{label} · {plan}" if plan else label


def fetched_text(fetched_at: Any) -> str:
    if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool) or fetched_at <= 0:
        return ""
    return "Fetched " + datetime.fromtimestamp(fetched_at).strftime("%H:%M:%S")


def usage_lines(result: Any, now: float | None = None) -> tuple[list[str], list[str]]:
    """The usage screen as text lines with a tone each.

    Limit windows carry ``meter-ok``/``meter-warn``/``meter-critical``/``meter-unknown``:
    the native renderer colours only the bar glyphs by that tone and keeps the
    label and summary neutral. Other tones: ``title``, ``dim``, ``bad``, ``""``.
    """
    from .text import sanitize

    lines: list[str] = []
    tones: list[str] = []

    def put(line: str, tone_name: str = "") -> None:
        lines.append(line)
        tones.append(tone_name)

    rows = list(_field(result, "providers", ()) or ())[:8]
    shows_reserve = False
    if not rows:
        put("No connected provider reports usage.", "title")
        put("Connect ChatGPT, Claude, GitHub Copilot, OpenCode Go or xAI in Settings → Providers.", "dim")
    width = max((len(sanitize(str(_field(w, "label", "") or "Limit"), 40))
                 for row in rows for w in list(_field(row, "windows", ()) or ())[:16]), default=0)
    for index, row in enumerate(rows):
        if index:
            put("")
        put(sanitize(heading(row), 80), "title")
        error = _field(row, "error")
        if error:
            put("  Unavailable: " + sanitize(str(error), 240), "bad")
        windows = list(_field(row, "windows", ()) or ())[:16]
        if not windows and not error:
            put("  No limit windows reported.", "dim")
        for window in windows:
            label = sanitize(str(_field(window, "label", "") or "Limit"), 40).translate(_NO_BAR_GLYPHS)
            put(f"  {label:<{width}}  {bar(window, now=now)}  {sanitize(summary(window, now), 200)}", "meter-" + tone(window))
            shows_reserve = shows_reserve or bool(reserve_phrase(window, now))
        for note in list(_field(row, "notes", ()) or ())[:8]:
            put(f"  · {sanitize(str(note), 160)}", "dim")
        if source := _field(row, "source"):
            put(f"  Source: {sanitize(str(source), 80)}", "dim")
    if shows_reserve:
        put("")
        put(RESERVE_NOTE, "dim")
    missing = [sanitize(str(name), 40) for name in list(_field(result, "not_connected", ()) or ())[:8]]
    if missing and rows:
        put("")
        put("Not connected: " + ", ".join(missing), "dim")
    if fetched := fetched_text(_field(result, "fetched_at")):
        put("")
        put(fetched, "dim")
    return lines, tones


#: A label never contains bar glyphs, so a renderer can find the bar by its glyphs.
_NO_BAR_GLYPHS = str.maketrans({FILLED: "-", TRACK: "-", PACE: "|"})

__all__ = [
    "usage_lines",
    "RESERVE_NOTE",
    "elapsed_percent",
    "reserve",
    "reserve_phrase",
    "BAR_WIDTH",
    "bar",
    "duration",
    "fetched_text",
    "heading",
    "percent",
    "reset_phrase",
    "summary",
    "tone",
    "used_percent",
]
