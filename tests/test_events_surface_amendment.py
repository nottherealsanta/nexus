"""Phase 3.5 surface amendment: the input/presence/daemon event catalogue.

Covers the §14.10 groups added to the §3.5 catalogue: uniqueness and grouping of
the exported constants, the exact planned membership, tolerance of unknown types
(the plan's rule for UIs), and round-tripping through the same msgspec path the
session log uses. The Event envelope itself is unchanged, so these tests also pin
its backward-compatible defaults, the transitional legacy names, and the
compatibility aliases still emitted by live code.
"""
import re

import msgspec

from nexus.events import (
    COMPAT_EVENTS,
    DAEMON_EVENTS,
    EVENT_GROUPS,
    EVENT_TYPES,
    INPUT_EVENTS,
    LEGACY_EVENTS,
    PRESENCE_EVENTS,
    Event,
)

# The exact planned groups from PLAN.md §14.10.
PLAN_GROUPS = {
    "input": ("input.started", "input.queued", "input.moved", "input.consumed", "input.dropped"),
    "presence": ("presence.joined", "presence.left"),
    "daemon": (
        "daemon.started",
        "daemon.stopping",
        "daemon.session_scheduled",
        "daemon.session_queued",
    ),
}

# Names still emitted by live code but not in the §14.10 set: kept as documented
# compatibility aliases so subscribers built on the earlier catalogue do not
# break. ``presence.changed`` accompanies join/leave; the daemon names predate
# the planned ``daemon.stopping``/``session_*`` events.
COMPAT = (
    "presence.changed",
    "daemon.stopped",
    "daemon.client_connected",
    "daemon.client_disconnected",
)

LEGACY = ("started", "message", "provider", "completed", "error")

_NAME_SHAPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


def test_plan_groups_exact_members():
    assert INPUT_EVENTS == PLAN_GROUPS["input"]
    assert PRESENCE_EVENTS == PLAN_GROUPS["presence"]
    assert DAEMON_EVENTS == PLAN_GROUPS["daemon"]


def test_plan_groups_registered_in_catalogue():
    for group, members in PLAN_GROUPS.items():
        assert EVENT_GROUPS[group] == members
        for name in members:
            assert name in EVENT_TYPES


def test_catalogue_group_names_and_order():
    assert list(EVENT_GROUPS) == [
        "session",
        "turn",
        "context",
        "model",
        "tool",
        "permission",
        "ext",
        "mcp",
        "skill",
        "agent",
        "hook",
        "input",
        "shell",
        "presence",
        "daemon",
        "misc",
    ]


def test_groups_are_internally_unique():
    for group, members in EVENT_GROUPS.items():
        assert len(members) == len(set(members)), f"duplicate in {group}"


def test_groups_are_mutually_disjoint():
    seen: dict[str, str] = {}
    for group, members in EVENT_GROUPS.items():
        for name in members:
            assert name not in seen, f"{name!r} in both {seen.get(name)} and {group}"
            seen[name] = group


def test_compat_aliases_are_known_but_outside_groups():
    grouped = {name for members in EVENT_GROUPS.values() for name in members}
    assert COMPAT_EVENTS == COMPAT
    for name in COMPAT:
        assert name in EVENT_TYPES
        assert name not in grouped
    # ``presence.changed`` must not silently rejoin the planned presence group.
    assert "presence.changed" not in PRESENCE_EVENTS


def test_event_types_is_exactly_groups_plus_legacy_plus_compat():
    grouped = {name for members in EVENT_GROUPS.values() for name in members}
    assert EVENT_TYPES == grouped | set(LEGACY_EVENTS) | set(COMPAT_EVENTS) | {"agent.selected"}
    assert "reasoning_effort.selected" in EVENT_GROUPS["model"]


def test_event_type_names_are_well_formed():
    for name in EVENT_TYPES:
        assert _NAME_SHAPE.match(name), name


def test_unknown_event_type_is_tolerated():
    # The plan's rule: a UI must tolerate a type it does not know. Nothing in
    # the catalogue rejects it; membership is a plain lookup.
    event = Event("future.something_new", {"x": 1})
    assert event.type == "future.something_new"
    assert event.type not in EVENT_TYPES
    assert event.to_dict()["data"] == {"x": 1}


def test_event_roundtrips_through_msgspec_json():
    event = Event(
        "input.queued",
        {"text": "hi", "queue_depth": 2},
        seq=7,
        ts=123.5,
        session="s1",
        turn="t1",
        id="fixed-id",
    )
    encoded = msgspec.json.encode(event)
    decoded = msgspec.json.decode(encoded, type=Event)
    assert decoded == event


def test_event_roundtrips_through_to_dict():
    event = Event("presence.changed", {"count": 2}, seq=3)
    rebuilt = Event(**event.to_dict())
    assert rebuilt == event


def test_event_envelope_defaults_are_unchanged():
    event = Event("message", {"text": "legacy"})
    assert event.data == {"text": "legacy"}
    assert event.seq == 0
    assert event.session is None
    assert event.turn is None
    assert isinstance(event.ts, float)
    assert event.id
    assert set(event.to_dict()) == {"type", "data", "seq", "ts", "session", "turn", "id"}


def test_transitional_legacy_events_preserved():
    for name in LEGACY:
        assert name in LEGACY_EVENTS
        assert name in EVENT_TYPES
        assert Event(name).type == name


def test_compat_alias_events_remain_constructible():
    for name in COMPAT:
        assert Event(name).type == name
