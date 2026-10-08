from pathlib import Path

import pytest

from nexus.ui.ratatui.prototype import _display_breadcrumb
from nexus.ui_support.context_header import agent_color


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/home/me", "~"),
        ("/home/me/repo › main", "~/repo:main"),
        ("/home/me-too/repo › main", "/home/me-too/repo:main"),
        ("/elsewhere/repo", "/elsewhere/repo"),
        ("relative/repo", "relative/repo"),
    ],
)
def test_breadcrumb_abbreviates_only_true_home(source, expected):
    assert _display_breadcrumb(source, Path("/home/me")) == expected


def test_root_home_is_not_abbreviated():
    assert _display_breadcrumb("/repo › main", Path("/")) == "/repo:main"


def test_primary_agent_identity_colors():
    assert agent_color("BUILD") == "#5C9CF5"
    assert agent_color("Orchestrator") == "#d18a38"
    assert agent_color("reviewer") == agent_color("REVIEWER")
