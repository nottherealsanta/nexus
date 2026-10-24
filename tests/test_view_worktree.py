from __future__ import annotations

from nexus.events import EVENT_TYPES, Event
from nexus.view import apply_many, initial_state


def _event(seq: int, type_: str, data: dict, *, turn: str | None = None) -> Event:
    return Event(type=type_, data=data, seq=seq, ts=float(seq), session="s", turn=turn, id=f"e{seq}")


def test_worktree_events_are_registered_and_replay_commit_links_to_turn() -> None:
    names = {
        "worktree.created", "worktree.setup_ran",
        "worktree.committed", "worktree.commit_skipped", "worktree.detached",
        "worktree.turn_started", "worktree.promotion_failed", "worktree.removed",
    }
    assert names <= EVENT_TYPES
    assert not {"session.worktree_set", "checkpoint.captured", "checkpoint.skipped", "checkpoint.started"} & EVENT_TYPES
    events = [
        _event(2, "turn.started", {}, turn="t1"),
        _event(3, "worktree.created", {
            "branch": "nexus/wt/blue-123456", "base_ref": "main",
            "base_sha": "a" * 40, "dirty_main": {"modified": 2, "untracked": 1},
        }),
        _event(4, "worktree.setup_ran", {"reason": "created"}),
        _event(5, "worktree.committed", {
            # This is the durable user-message record seq, not TurnView.index.
            "sha": "b" * 40, "seq": 12, "by": "harness", "agent_id": "root",
            "subject": "nexus: fix thing", "files": ["src/a.py"], "clipped": 0,
            "insertions": 3, "deletions": 1,
        }, turn="t1"),
    ]
    state = apply_many(initial_state("s"), events)
    assert state.worktree.enabled is True
    assert state.worktree.state == "active"
    assert state.worktree.branch == "nexus/wt/blue-123456"
    assert state.worktree.dirty_main == {"modified": 2, "untracked": 1}
    assert state.worktree.commits[0].subject == "nexus: fix thing"
    assert state.turns[0].commit_shas == ["b" * 40]
    assert apply_many(initial_state("s"), events).to_dict() == state.to_dict()


def test_turn_boundary_is_internal_and_promotion_failure_replays_as_warning() -> None:
    boundary = _event(1, "worktree.turn_started", {
        "head": "a" * 40, "seq": 12, "worktree_id": "internal-id",
    }, turn="t1")
    initial = initial_state("s")
    after_boundary = apply_many(initial, [boundary])
    assert after_boundary.worktree == initial.worktree
    assert after_boundary.turns == initial.turns
    assert after_boundary.errors == initial.errors

    events = [
        boundary,
        _event(2, "worktree.promotion_requested", {"request_id": "p1"}),
        _event(3, "worktree.promotion_failed", {
            "request_id": "p1", "detail": "Target branch moved; review again.",
        }),
    ]
    state = apply_many(initial, events)
    assert state.worktree.state == "promotion_failed"
    assert state.worktree.promotion_error == "Target branch moved; review again."
    assert "internal-id" not in repr(state.to_dict())
    assert apply_many(initial, events).to_dict() == state.to_dict()

    malformed = apply_many(initial, [
        _event(4, "worktree.promotion_failed", {"detail": "x\x00y"}),
    ])
    assert len(malformed.diagnostics) == 1


def test_worktree_commit_history_is_bounded_to_newest_200() -> None:
    events = [
        _event(i + 1, "worktree.committed", {
            "sha": f"{i:040x}", "seq": i, "by": "agent", "subject": f"c{i}",
        })
        for i in range(205)
    ]
    state = apply_many(initial_state(), events)
    assert len(state.worktree.commits) == 200
    assert state.worktree.commits[0].subject == "c5"
    assert state.worktree.commits[-1].subject == "c204"


def test_turnless_commit_seq_does_not_match_zero_based_turn_index() -> None:
    state = apply_many(initial_state("s"), [
        _event(1, "turn.started", {}, turn="t1"),
        _event(2, "worktree.committed", {
            "sha": "c" * 40, "seq": 0, "by": "harness", "agent_id": "root",
        }),
    ])
    assert state.turns[0].index == 0
    assert state.turns[0].commit_shas == []
    assert state.worktree.commits[0].seq == 0


def test_worktree_skip_detach_and_malformed_payloads() -> None:
    state = apply_many(initial_state(), [
        _event(1, "worktree.commit_skipped", {"reason": "too_large", "detail": "limit"}),
        _event(2, "worktree.detached", {}),
        _event(3, "worktree.committed", {"sha": "x" * 100, "seq": 0, "by": "forged"}),
    ])
    assert state.worktree.state == "detached"
    assert state.worktree.last_skip == {"reason": "too_large", "detail": "limit"}
    assert len(state.diagnostics) == 1


def test_main_default_worktree_view_is_empty() -> None:
    view = initial_state().to_dict()["worktree"]
    assert view["enabled"] is False
    assert view["state"] == "none"
    assert view["commits"] == []
    assert not {"history", "moves", "pending_created"} & set(view)  # absent until a session moves


def test_removed_checkpoint_events_are_only_diagnostics():
    view = apply_many(initial_state("s"), [Event(
        type="checkpoint.captured", data={"seq": 42}, seq=1, ts=1.0, session="s", turn="t1", id="e1")])
    assert "checkpoints" not in view.to_dict()
    assert len(view.diagnostics) == 1


def _root(seq, kind, path="", *, worktree_id="", branch="", reason="", base_ref="main", base_sha="a" * 12, turn=None):
    data = {"kind": kind, "path_display": path, "branch": branch, "base_ref": base_ref if kind == "worktree" else "",
            "base_sha": base_sha if kind == "worktree" else "", "worktree_id": worktree_id}
    if reason:
        data["reason"] = reason
    return _event(seq, "session.root_set", data, turn=turn)


def test_several_worktrees_keep_history_and_reasoned_rows_and_replay_identically():
    events = [
        _root(1, "main", "."),
        _event(2, "worktree.created", {"branch": "nexus/wt/brave-otter", "base_ref": "main", "base_sha": "a" * 12,
                                       "worktree_id": "wt-1", "reason": "refactor auth"}),
        _root(3, "worktree", "wt/brave-otter", worktree_id="wt-1", branch="nexus/wt/brave-otter", turn="t1"),
        _event(4, "worktree.merged", {"target_branch": "main", "sha": "b" * 12, "subject": "x", "worktree_id": "wt-1"}),
        _root(5, "main", ".", reason="done, merged"),
        _event(6, "worktree.created", {"branch": "nexus/wt/calm-heron", "base_ref": "main", "base_sha": "c" * 12,
                                       "worktree_id": "wt-2", "reason": "fix flaky test"}),
        _root(7, "worktree", "wt/calm-heron", worktree_id="wt-2", branch="nexus/wt/calm-heron", turn="t2"),
        _root(8, "main", ".", reason="left unmerged"),
        _root(9, "worktree", "wt/brave-otter", worktree_id="wt-1", branch="nexus/wt/brave-otter", reason="one more fix"),
        _event(10, "worktree.removed", {"worktree_id": "wt-2"}),
    ]
    view = apply_many(initial_state("s"), events)
    wt = view.worktree
    assert [(h.name, h.state, h.merged, h.created) for h in wt.history] == [
        ("brave-otter", "active", True, True), ("calm-heron", "removed", False, True)]
    assert [(m.action, m.kind, m.name, m.reason) for m in wt.moves] == [
        ("created", "worktree", "brave-otter", "refactor auth"), ("moved", "main", "", "done, merged"),
        ("created", "worktree", "calm-heron", "fix flaky test"), ("moved", "main", "", "left unmerged"),
        ("moved", "worktree", "brave-otter", "one more fix")]
    assert view.root.worktree_id == "wt-1" and view.root.reason == "one more fix"
    assert wt.enabled and wt.branch == "nexus/wt/brave-otter" and wt.state == "active"
    assert (wt.history[0].first_turn, wt.history[1].first_turn) == ("t1", "t2")
    assert apply_many(initial_state("s"), events).to_dict() == view.to_dict()


def test_move_back_to_main_closes_the_worktree_and_a_later_commit_unmerges_it():
    view = apply_many(initial_state("s"), [
        _root(1, "worktree", "wt/a", worktree_id="wt-1", branch="nexus/wt/a"),
        _event(2, "worktree.merged", {"target_branch": "main", "sha": "b" * 12, "subject": "x"}),
        _event(3, "worktree.committed", {"sha": "d" * 40, "seq": 1, "by": "agent"}),
        _root(4, "main", "."),
    ])
    assert [(h.state, h.merged) for h in view.worktree.history] == [("unmerged", False)]
    assert view.worktree.enabled is False and view.worktree.state == "none" and view.worktree.branch == ""
    assert view.root.kind == "main"


def test_initial_main_root_is_not_a_move():
    view = apply_many(initial_state("s"), [_root(1, "main", ".")])
    assert view.worktree.moves == [] and view.worktree.history == []


def test_history_and_moves_are_bounded():
    events = [_root(i + 1, "worktree", f"wt/n{i}", worktree_id=f"wt-{i}", branch=f"b{i}") for i in range(120)]
    wt = apply_many(initial_state("s"), events).worktree
    assert len(wt.history) == 50 and len(wt.moves) == 100
    assert wt.history[-1].worktree_id == "wt-119"
