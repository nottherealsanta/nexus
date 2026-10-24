"""Top-bar session location: worktree, main checkout, non-Git, and following ConversationView.root."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.events import Event
from nexus.ui.ratatui.worktree_bar import (
    root_move_text,
    session_worktree_kind,
    session_worktree_label,
    session_worktree_state,
    sync_bar_with_root,
)
from nexus.view import apply, initial_state


@pytest.fixture(autouse=True)
def _glyphs(monkeypatch):
    monkeypatch.delenv("NEXUS_ASCII", raising=False)


def test_worktree_session_shows_badge_and_branch():
    state = {"kind": "worktree", "worktree_name": "fix-it", "branch": "nexus/wt/fix-it"}
    assert session_worktree_label(state, "main", "") == ("nexus/wt/fix-it", "⎇ fix-it")
    assert session_worktree_kind(state) == "worktree"


def test_main_checkout_is_stated_explicitly():
    state = {"kind": "main", "git": True}
    assert session_worktree_label(state, "main", "") == ("main", "main checkout")
    assert session_worktree_kind(state) == "main"


def test_main_checkout_text_is_plain_in_ascii_mode(monkeypatch):
    monkeypatch.setenv("NEXUS_ASCII", "1")
    assert session_worktree_label({"kind": "main"}, "main", "")[1] == "main checkout"


def test_non_git_and_unknown_state_show_nothing_new():
    assert session_worktree_label({"kind": "main", "git": False}, "", "") == ("", "")
    assert session_worktree_kind({"kind": "main", "git": False}) == ""
    assert session_worktree_label({}, "main", "") == ("main", "")
    assert session_worktree_kind({}) == ""
    assert session_worktree_kind({}, "linked") == "worktree"


def test_label_is_bounded():
    state = {"kind": "worktree", "worktree_name": "x" * 200}
    assert len(session_worktree_label(state, "b", "")[1]) == 80


def _root(seq, kind, path, **extra):
    data = {"kind": kind, "path_display": path, "branch": extra.pop("branch", ""),
            "base_ref": "main" if kind == "worktree" else "", "worktree_id": extra.pop("worktree_id", ""), **extra}
    return Event(type="session.root_set", data=data, seq=seq, ts=float(seq), session="s", turn=None, id=f"e{seq}")


def _shell(view):
    return SimpleNamespace(workspace="/repo", controller=SimpleNamespace(session="s", view=view),
                           _git_branch="main", _git_worktree="", breadcrumb="/repo", branch="",
                           worktree="", worktree_kind="")


def test_state_is_read_from_the_view_root_not_the_host():
    view = apply(initial_state("s"), _root(1, "worktree", "wt/brave-otter", branch="nexus/wt/brave-otter", worktree_id="wt-1"))
    state = session_worktree_state(_shell(view))
    assert (state["kind"], state["worktree_name"], state["branch"], state["base_branch"], state["git"]) == (
        "worktree", "brave-otter", "nexus/wt/brave-otter", "main", True)
    assert session_worktree_state(SimpleNamespace(controller=None)) == {}


def test_bar_follows_an_agent_move_without_a_tab_switch():
    view = apply(initial_state("s"), _root(1, "main", "."))
    shell = _shell(view)
    assert sync_bar_with_root(shell, view) is True  # first sight of this root
    assert (shell.worktree, shell.worktree_kind, shell.branch) == ("main checkout", "main", "main")
    assert sync_bar_with_root(shell, view) is False  # unchanged root: nothing to redraw
    view = apply(view, _root(2, "worktree", "wt/brave-otter", branch="nexus/wt/brave-otter", worktree_id="wt-1"))
    assert sync_bar_with_root(shell, view) is True
    assert (shell.worktree, shell.worktree_kind, shell.branch) == ("⎇ brave-otter", "worktree", "nexus/wt/brave-otter")
    assert shell.breadcrumb == "/repo › nexus/wt/brave-otter"
    view = apply(view, _root(3, "main", ".", reason="done"))
    assert sync_bar_with_root(shell, view) is True
    assert (shell.worktree, shell.worktree_kind, shell.branch) == ("main checkout", "main", "main")


def test_move_rows_state_the_action_base_and_reason():
    view = apply(initial_state("s"), Event(
        type="worktree.created", data={"branch": "nexus/wt/brave-otter", "base_ref": "main", "base_sha": "1a2b3c4d5e",
                                       "reason": "refactor auth"}, seq=1, ts=1.0, session="s", turn=None, id="c"))
    view = apply(view, _root(2, "worktree", "wt/brave-otter", branch="nexus/wt/brave-otter", worktree_id="wt-1",
                             base_sha="1a2b3c4d5e"))
    view = apply(view, _root(3, "main", ".", reason="left unmerged"))
    created, back = view.worktree.moves
    assert root_move_text(created) == '⎇ Created worktree brave-otter · from main @ 1a2b3c4 · reason: "refactor auth"'
    assert root_move_text(back) == '⎇ Moved to the main checkout · reason: "left unmerged"'
