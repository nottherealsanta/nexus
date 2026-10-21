"""``agent.routed`` reduces into ``AgentView.route`` and never into the child body."""

from __future__ import annotations

import json
from pathlib import Path

from nexus.events import EVENT_TYPES, Event
from nexus.view import fold

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "view"

META = {"id": "s1/sub/1", "parent": None, "depth": 1, "type": "general", "task": "t", "index": 0}
SKIPPED = {
    "reference": "github-copilot/gpt-6-luna",
    "quota": "github-copilot",
    "kind": "monthly",
    "label": "Premium requests (monthly)",
    "remaining_percent": 1.5,
    "threshold_percent": 2.0,
    "resets_at": 1793491200,
}


def _ev(seq, type_, data=None, *, session="s1"):
    return Event(type=type_, data=dict(data or {}), seq=seq, ts=float(seq), session=session, turn=None, id=f"x{seq:03d}")


def _routed(seq=2, **over):
    payload = {
        "tier": "low",
        "requested": "low",
        "first": "github-copilot/gpt-6-luna",
        "chosen": "codex/gpt-6-luna",
        "all_blocked": False,
        "observed_at": 1792000000.0,
        "skipped": [SKIPPED],
    }
    payload.update(over)
    return _ev(seq, "agent.routed", {"agent": META, **META, **payload})


def _events(routed):
    return [_ev(1, "agent.spawned", {"agent": META, **META}), routed]


def test_agent_routed_is_a_known_event():
    assert "agent.routed" in EVENT_TYPES


def test_routed_sets_route_and_selected_model():
    agent = fold(_events(_routed())).agents[META["id"]]
    assert agent.route == {
        "requested": "low",
        "first": "github-copilot/gpt-6-luna",
        "chosen": "codex/gpt-6-luna",
        "all_blocked": False,
        "skipped": [SKIPPED],
    }
    assert agent.selected_model == "codex/gpt-6-luna"


def test_routed_is_not_folded_into_the_child_body():
    spawn = _ev(1, "agent.spawned", {"agent": META, **META})
    routed = _routed()
    agent_with_route = fold([spawn, routed]).agents[META["id"]]
    agent_spawn_only = fold([spawn]).agents[META["id"]]
    assert agent_with_route.body.to_dict() == agent_spawn_only.body.to_dict()
    assert not agent_with_route.body.diagnostics


def test_all_blocked_keeps_selected_model_and_records_route():
    first = "github-copilot/gpt-6-luna"
    events = _events(_routed(chosen=first, all_blocked=True))
    agent = fold(events).agents[META["id"]]
    assert agent.selected_model is None
    assert agent.route["all_blocked"] is True
    assert agent.route["chosen"] == first
    assert agent.route["skipped"] == [SKIPPED]


def test_replay_is_deterministic():
    events = _events(_routed())
    assert fold(events).to_dict() == fold(events).to_dict()
    assert fold(events + events).to_dict() == fold(events).to_dict()


def test_view_without_routed_event_has_no_route_key():
    raw = json.loads((FIXTURES / "subagent.events.json").read_text(encoding="utf-8"))
    dumped = json.dumps(fold([Event(**item) for item in raw]).to_dict())
    assert '"route"' not in dumped
    spawn = _ev(1, "agent.spawned", {"agent": META, **META})
    view = fold([spawn]).to_dict()
    agent = next(a for a in view["agents"] if a["id"] == META["id"])
    assert "route" not in agent
