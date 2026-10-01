"""Provider usage formatting shared by surfaces (``ProvidersUsageResult``).

Pure functions, no Textual: the TUI usage modal (``ui/tui/usage.py``) renders
these strings and the web client's ``js/usage.js`` is a line-for-line port, so
both surfaces word a limit identically: ``41% used · 59% left · resets in 6d
5h (Wed 14:15)``.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

BAR_WIDTH = 24
#: Used-percent thresholds for the bar tone: ``ok`` below 70, ``warn`` below 90.
WARN_AT, CRITICAL_AT = 70.0, 90.0


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


def bar(window: Any, width: int = BAR_WIDTH) -> str:
    """A fixed-width text bar: filled cells are the used share."""
    used = used_percent(window)
    if used is None:
        return "░" * width
    filled = min(width, max(1 if used > 0 else 0, round(used / 100 * width)))
    return "█" * filled + "░" * (width - filled)


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
    """``41% used · 59% left · resets in 6d 5h (Wed Oct 7 14:15) · 14,979 of 15,000 left``."""
    used = used_percent(window)
    parts = ["usage unknown"] if used is None else [f"{percent(used)} used", f"{percent(100 - used)} left"]
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


__all__ = [
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
