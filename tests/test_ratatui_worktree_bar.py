"""Top-bar session location: worktree, main checkout, pending draft, non-Git (worktree_bar.py)."""
from __future__ import annotations

import pytest

from nexus.ui.ratatui.worktree_bar import PLACEHOLDER, session_worktree_kind, session_worktree_label


@pytest.fixture(autouse=True)
def _glyphs(monkeypatch):
    monkeypatch.delenv("NEXUS_ASCII", raising=False)


def test_worktree_session_shows_badge_and_branch():
    state = {"kind": "worktree", "worktree_name": "fix-it", "branch": "nexus/fix-it"}
    assert session_worktree_label(state, "main", "") == ("nexus/fix-it", "⎇ fix-it")
    assert session_worktree_kind(state) == "worktree"


def test_main_checkout_is_stated_explicitly():
    state = {"kind": "main", "git": True, "editable": False}
    assert session_worktree_label(state, "main", "") == ("main", "main checkout")
    assert session_worktree_kind(state) == "main"


def test_main_checkout_text_is_plain_in_ascii_mode(monkeypatch):
    monkeypatch.setenv("NEXUS_ASCII", "1")
    assert session_worktree_label({"kind": "main"}, "main", "")[1] == "main checkout"


def test_pending_draft_keeps_placeholder_and_name():
    draft = {"kind": "main", "editable": True, "selected": True}
    assert session_worktree_label(draft, "main", "") == ("main", "⎇ " + PLACEHOLDER)
    assert session_worktree_kind(draft) == "pending"
    named = {**draft, "pending_name": "calm-otter"}
    assert session_worktree_label(named, "main", "")[1] == "⎇ calm-otter"


def test_non_git_and_unknown_state_show_nothing_new():
    assert session_worktree_label({"kind": "main", "git": False}, "", "") == ("", "")
    assert session_worktree_kind({"kind": "main", "git": False}) == ""
    assert session_worktree_label({}, "main", "") == ("main", "")
    assert session_worktree_kind({}) == ""
    assert session_worktree_kind({}, "linked") == "worktree"


def test_label_is_bounded():
    state = {"kind": "worktree", "worktree_name": "x" * 200}
    assert len(session_worktree_label(state, "b", "")[1]) == 80
