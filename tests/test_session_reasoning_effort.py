"""Durable root-session reasoning-effort selection state."""
from __future__ import annotations

import json

import pytest

from nexus.events import Event
from nexus.model.reasoning_effort import ReasoningEffortSelection
from nexus.model.selection import ModelSelection
from nexus.session.agent_selection import AgentSelection
from nexus.session.manager import SessionManager


def test_selection_record_is_frozen_validated_and_json_safe():
    selection = ReasoningEffortSelection(effort="xhigh")
    payload = selection.to_dict()
    assert payload == {"effort": "xhigh", "version": 1}
    assert json.loads(json.dumps(payload)) == payload
    assert ReasoningEffortSelection.from_dict(payload) == selection
    with pytest.raises((AttributeError, TypeError)):
        selection.effort = "low"
    with pytest.raises(ValueError):
        ReasoningEffortSelection(effort="unlisted")
    with pytest.raises(ValueError, match="effort must be one of"):
        ReasoningEffortSelection(effort=[])
    payload["effort"] = "low"
    assert selection.effort == "xhigh"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"effort": "high"},
        {"effort": "high", "version": True},
        {"effort": 1, "version": 1},
        {"effort": "custom", "version": 1},
        {"effort": "high", "version": 2},
    ],
)
def test_malformed_and_future_payloads_are_ignored(payload):
    assert ReasoningEffortSelection.from_dict(payload) is None


async def test_set_replace_clear_and_replay_without_turn_or_other_selection_mutation(tmp_path):
    sessions = SessionManager(tmp_path)
    session = sessions.open("s")
    session.select_reasoning_effort(ReasoningEffortSelection(effort="low"))
    session.select_reasoning_effort(ReasoningEffortSelection(effort="high"))
    selected = [event for event in session.events if event.type == "reasoning_effort.selected"]
    assert [event.data for event in selected] == [
        {"effort": "low", "version": 1},
        {"effort": "high", "version": 1},
    ]
    assert all(event.turn is None for event in selected)
    assert session.reasoning_effort_selection == ReasoningEffortSelection(effort="high")

    session.select_model(
        ModelSelection(reference="vendor/model", provider="vendor", model="model")
    )
    session.select_agent(AgentSelection(name="plan"))
    assert session.model_selection.model == "model"
    assert session.agent_selection.name == "plan"
    assert session.active is False

    session.clear_reasoning_effort()
    assert session.reasoning_effort_selection == ReasoningEffortSelection(effort=None)
    clear_event = [
        event for event in session.events if event.type == "reasoning_effort.selected"
    ][-1]
    assert clear_event.data == {"effort": None, "version": 1}

    replayed = [event async for event in sessions.replay("s")]
    assert [event.type for event in replayed].count("reasoning_effort.selected") == 3
    await sessions.aclose_session("s")
    reopened = sessions.open("s", create=False)
    assert reopened.reasoning_effort_selection == ReasoningEffortSelection(effort=None)
    assert reopened.model_selection.model == "model"
    assert reopened.agent_selection.name == "plan"


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"effort": "not-valid", "version": 1},
        {"effort": 42, "version": 1},
        {"effort": "low", "version": 99},
        {"effort": "low", "version": False},
    ],
)
def test_reopen_skips_malformed_events_and_preserves_latest_valid_selection(
    tmp_path, invalid_payload
):
    sessions = SessionManager(tmp_path)
    session = sessions.open("s")
    session.select_reasoning_effort(ReasoningEffortSelection(effort="medium"))
    session.append_event(
        Event(type="reasoning_effort.selected", data=invalid_payload, session="s")
    )
    sessions.evict("s")

    reopened = sessions.open("s", create=False)
    assert reopened.reasoning_effort_selection == ReasoningEffortSelection(
        effort="medium"
    )


def test_old_log_without_selection_reopens_with_default_state(tmp_path):
    sessions = SessionManager(tmp_path)
    assert sessions.open("old").reasoning_effort_selection is None
    sessions.evict("old")
    assert sessions.open("old", create=False).reasoning_effort_selection is None
