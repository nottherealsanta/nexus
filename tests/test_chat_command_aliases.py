"""Slash-command aliases resolve to one canonical command (ui/cli/commands.py)."""

from __future__ import annotations

import pytest

from nexus.ui.cli import commands


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("/clear", "/new"),
        ("/quit", "/exit"),
        ("/reasoning", "/effort"),
        ("/session", "/sessions"),
        ("/resume", "/archived"),
        ("/sesssion", "/sessions"),
        ("/new", "/new"),
    ],
)
def test_alias_parses_to_canonical_name(typed: str, canonical: str):
    parsed = commands.parse(f"{typed} arg")
    assert parsed is not None
    assert parsed.name == canonical
    assert parsed.args == ("arg",)
    assert parsed.spec is commands.BY_NAME[canonical]


def test_aliases_are_unique_and_never_shadow_a_command():
    names = [name for spec in commands.SPECS for name in (spec.name, *spec.aliases)]
    assert len(names) == len(set(names))
    assert "/sesssion" not in commands.help_text()


def test_help_lists_aliases_next_to_their_command():
    text = commands.help_text()
    assert "/new, /clear " in text
    assert "/exit, /quit" in text
    assert "/effort, /reasoning " in text
