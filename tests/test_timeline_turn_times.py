"""Durable turn clocks and shared duration bands."""
from datetime import datetime

import pytest

from nexus.ui_support.timeline import _clock, _turn_duration, turn_footer_text
from nexus.ui.ratatui.verb_groups import format_time
from nexus.view.model import TurnView


@pytest.mark.parametrize(('ms', 'expected'), [(59900, '59.9s'), (700000, '11m 40s'), (3899000, '1h 4m')])
def test_duration_bands(ms, expected):
    assert _turn_duration(TurnView(elapsed_ms=ms)) == expected


def test_completion_clock_and_legacy_fallback():
    start = 1700000000.0
    end = start + 120
    clock = datetime.fromtimestamp(end).strftime('%I:%M %p').lstrip('0')
    assert turn_footer_text(TurnView(started_ts=start, user_ts=start, completed_ts=end)).startswith(clock)
    assert turn_footer_text(TurnView(started_ts=start, elapsed_ms=120000)).startswith(clock)
    assert turn_footer_text(TurnView(user_ts=start)) == ''
    assert _clock(float('nan')) == ''
    assert format_time(3899000) == '1h 4m'
