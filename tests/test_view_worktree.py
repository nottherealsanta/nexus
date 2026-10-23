from __future__ import annotations

from nexus.events import EVENT_TYPES, Event
from nexus.view import apply_many, initial_state


def _event(seq: int, type_: str, data: dict, *, turn: str | None = None) -> Event:
    return Event(type=type_, data=data, seq=seq, ts=float(seq), session="s", turn=turn, id=f"e{seq}")


def test_worktree_events_are_registered_and_replay_commit_links_to_turn() -> None:
    names = {
        "session.worktree_set", "worktree.created", "worktree.setup_ran",
        "worktree.committed", "worktree.commit_skipped", "worktree.detached",
        "worktree.turn_started", "worktree.promotion_failed",
    }
    assert names <= EVENT_TYPES
    events = [
        _event(1, "session.worktree_set", {"enabled": True, "base_ref": "main"}),
        _event(2, "turn.started", {}, turn="t1"),
        _event(3, "worktree.created", {
            "branch": "nexus/session/blue-123456", "base_ref": "main",
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
    assert state.worktree.branch == "nexus/session/blue-123456"
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


def test_checkpoint_replay_links_the_exact_turn():
    events = [_event(1, "turn.started", {}, turn="t1"), _event(2, "checkpoint.captured", {
        "seq": 42, "ref": "refs/nexus/checkpoints/s/0000000042", "commit": "a" * 40,
        "head": "b" * 40, "base_ref": "c" * 40, "files": ["saved.txt"],
    }, turn="t1")]
    view = apply_many(initial_state("s"), events)
    assert view.checkpoints[0].turn_id == "t1"
    assert view.checkpoints[0].seq == 42
    assert apply_many(initial_state("s"), events).to_dict() == view.to_dict()
