"""Provider usage: thin bars, the even-pace marker and reserve.

Reserve is the share of a weekly or monthly window already elapsed minus the
share used (half the month gone with 20% used leaves 30% in reserve). The host
reports ``period_seconds`` and ``resets_at``; ``ui_support/usage.py`` words it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nexus.host_support.provider_usage import (
    claude_reset_at,
    month_before,
    parse_claude,
    parse_codex,
    parse_copilot,
    parse_opencode_go,
)
from nexus.ui_support.usage import RESERVE_NOTE, bar, elapsed_percent, reserve, reserve_phrase, summary, usage_lines

NOW = 1_000_000.0
DAY = 86_400


def _month(used: float, days_left: float) -> dict:
    return {"label": "Monthly", "used_percent": used, "resets_at": NOW + days_left * DAY, "period_seconds": 30 * DAY}


def test_half_the_month_gone_with_20_percent_used_leaves_30_in_reserve():
    window = _month(20, 15)
    assert elapsed_percent(window, NOW) == pytest.approx(50)
    assert reserve(window, NOW) == pytest.approx(30)
    assert reserve_phrase(window, NOW) == "30% in reserve"
    assert summary(window, NOW).startswith("20% used · 30% in reserve · resets in 15d")


@pytest.mark.parametrize("used,days_left,phrase", [(80, 15, "30% over pace"), (50.2, 15, "on pace"), (0, 30, "on pace")])
def test_over_pace_and_on_pace(used, days_left, phrase):
    assert reserve_phrase(_month(used, days_left), NOW) == phrase


@pytest.mark.parametrize("window", [
    {"label": "5-hour", "used_percent": 10, "resets_at": NOW + 3600, "period_seconds": 18_000},  # too short
    {"label": "Weekly", "used_percent": 10, "resets_at": NOW + DAY},  # no period
    {"label": "Weekly", "used_percent": 10, "period_seconds": 7 * DAY},  # no reset
    {"label": "Weekly", "used_percent": 10, "resets_at": NOW - 1, "period_seconds": 7 * DAY},  # reset due
    {"label": "Weekly", "used_percent": None, "resets_at": NOW + DAY, "period_seconds": 7 * DAY},  # unknown usage
])
def test_reserve_only_for_known_weekly_and_monthly_windows(window):
    assert reserve_phrase(window, NOW) == ""
    assert "reserve" not in summary(window, NOW) and "pace" not in summary(window, NOW)


def test_thin_bar_marks_the_even_pace_point():
    assert bar(_month(25, 15), width=8, now=NOW) == "━━──┃───"
    assert bar({"label": "5-hour", "used_percent": 50}, width=8) == "━━━━────"
    assert bar({"label": "?", "used_percent": None}, width=4) == "────"
    assert "█" not in bar(_month(90, 1), now=NOW)


def test_usage_lines_tone_meters_and_explain_reserve_once():
    result = {"providers": [{"label": "Codex", "windows": [
        {"label": "5-hour", "used_percent": 95, "resets_at": NOW + 3600, "period_seconds": 18_000},
        {"label": "Weekly ━ odd", "used_percent": 20, "resets_at": NOW + 3.5 * DAY, "period_seconds": 7 * DAY},
    ]}]}
    lines, tones = usage_lines(result, now=NOW)
    meters = [(line, tone) for line, tone in zip(lines, tones) if tone.startswith("meter-")]
    assert [tone for _, tone in meters] == ["meter-critical", "meter-ok"]
    assert "30% in reserve" in meters[1][0]
    assert "Weekly - odd" in meters[1][0]  # a label never contains bar glyphs
    assert lines.count(RESERVE_NOTE) == 1
    no_reserve = usage_lines({"providers": [{"label": "Codex", "windows": [{"label": "5-hour", "used_percent": 5}]}]})[0]
    assert RESERVE_NOTE not in no_reserve


# -- host: window length and Claude reset times --------------------------------------


def _epoch(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def test_month_before_is_the_calendar_month_ending_at_the_reset():
    assert month_before(_epoch(2026, 11, 1)) == 31 * DAY  # October
    assert month_before(_epoch(2026, 3, 1)) == 28 * DAY  # February
    assert month_before(_epoch(2026, 3, 31)) == 31 * DAY  # Feb 28 → Mar 31 is clamped from Feb 31
    assert month_before(None) is None


def test_hosts_report_period_seconds_for_each_window():
    codex = parse_codex({"rate_limit": {"primary_window": {"used_percent": 4, "limit_window_seconds": 18000, "reset_at": 1},
                                        "secondary_window": {"used_percent": 9, "limit_window_seconds": 604800, "reset_at": 2}}})
    assert [w["period_seconds"] for w in codex["windows"]] == [18_000, 604_800]
    copilot = parse_copilot({"quota_reset_date_utc": "2026-11-01T00:00:00Z",
                             "quota_snapshots": {"premium_interactions": {"percent_remaining": 75}}})
    assert copilot["windows"][0]["period_seconds"] == 31 * DAY
    go = parse_opencode_go({"usage": {"rolling": {"usagePercent": 1, "resetInSec": 60},
                                      "weekly": {"usagePercent": 1, "resetInSec": 60},
                                      "monthly": {"usagePercent": 1, "resetAt": "2026-11-01T00:00:00Z"}}}, now=NOW)
    assert [w["period_seconds"] for w in go["windows"]] == [18_000, 604_800, 31 * DAY]


def test_claude_reset_text_becomes_a_timestamp_in_its_zone():
    now = _epoch(2026, 10, 14, 10, 0)  # 15:30 in Asia/Calcutta
    assert claude_reset_at("Oct 15 at 8:30pm (Asia/Calcutta)", now) == _epoch(2026, 10, 15, 15, 0)
    assert claude_reset_at("12pm (Asia/Calcutta)", now) == _epoch(2026, 10, 15, 6, 30)  # a bare time is the next one
    assert claude_reset_at("Jan 2 at 9am (America/New_York)", now) == _epoch(2027, 1, 2, 14, 0)  # year rolls over
    for text in ("", "soon", "Oct 15 at 8:30pm (Not/AZone)", "Oct 15 at 8:75pm (UTC)"):
        assert claude_reset_at(text, now) is None
    windows = parse_claude("Current session: 38% used · resets 12pm (Asia/Calcutta)\n"
                           "Current week (all models): 56% used · resets Oct 15 at 8:30pm (Asia/Calcutta)\n", now=now)["windows"]
    assert [(w["label"], w["period_seconds"]) for w in windows] == [("5-hour session", 18_000), ("Weekly (all models)", 604_800)]
    assert windows[1]["resets_at"] == _epoch(2026, 10, 15, 15, 0)
    assert windows[1]["reset_text"] == "Oct 15 at 8:30pm (Asia/Calcutta)"  # the CLI's own words are kept
    assert reserve_phrase(windows[1], now) == "27% in reserve"  # 29h of 168h left: 82.7% elapsed, 56% used
