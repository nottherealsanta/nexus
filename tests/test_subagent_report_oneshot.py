"""One-shot runs follow a session until its background subagents have reported.

``run_once`` is driven with a fake client (the daemon's ``SessionActivity`` answer is
scripted), so the wait, the bound and the "still running" note are observable
without a daemon. The activity contract itself is covered with the host facade in
``tests/test_subagent_reports.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from nexus.events import Event
from nexus.ui.cli import run as run_module
from nexus.ui.cli.run import run_once


def activity(*, agents=0, queued=0, pending=0, turn=False, wait=30):
    return SimpleNamespace(
        turn_active=turn, queued=queued, running_agents=agents, reports_pending=pending,
        wait_seconds=wait,
    )


class FakeClient:
    """Streams scripted turns; ``activities`` answers ``session_activity`` in order."""

    def __init__(self, turns, activities, *, delay=0.05, quiet_when_delivered=False):
        self.turns = list(turns)  # each: list of Event, the second turn is the "wake"
        self.activities = list(activities)
        self.calls = 0
        self.cancelled = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._delay = delay
        self._quiet_when_delivered = quiet_when_delivered
        self._delivered = len(self.turns) == 1

    async def handshake(self):
        return None

    async def open_session(self, session):
        return SimpleNamespace(last_seq=0)

    async def start_turn(self, session, content, **kw):
        for event in self.turns[0]:
            await self._queue.put(event)
        asyncio.ensure_future(self._later())
        return "t1"

    async def _later(self):
        for events in self.turns[1:]:
            await asyncio.sleep(self._delay)
            for event in events:
                await self._queue.put(event)
        self._delivered = True

    async def stream(self, session, from_seq=0, *, follow=True):
        while True:
            yield await self._queue.get()

    async def session_activity(self, session):
        self.calls += 1
        if self._quiet_when_delivered and self.calls > 1:
            # Busy until the woken turn has been streamed and consumed by the run.
            done = self._delivered and self._queue.empty()
            return activity() if done else activity(agents=1)
        index = min(self.calls - 1, len(self.activities) - 1)
        return self.activities[index]

    async def cancel(self, session, **kw):
        self.cancelled = True


def turn(n, text):
    return [
        Event(type="turn.started", session="s", turn=f"t{n}", data={}),
        Event(type="text", session="s", turn=f"t{n}", data={"text": text}),
        Event(type="turn.completed", session="s", turn=f"t{n}", data={"stop_reason": "end_turn"}),
    ]


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(run_module, "_ACTIVITY_POLL_SECONDS", 0.02)


async def go(client):
    out, err = io.StringIO(), io.StringIO()
    code = await run_once(client, session="s", content="hi", stdout=out, stderr=err, json_output=True)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()], err.getvalue()


async def test_waits_for_the_woken_turn_after_a_background_child_reports():
    client = FakeClient(
        [turn(1, "spawned"), turn(2, "the report arrived")],
        [activity(agents=1)],
        quiet_when_delivered=True,
    )
    code, events, err = await asyncio.wait_for(go(client), 10)
    assert code == 0
    assert [e["type"] for e in events].count("turn.completed") == 2
    assert any(e["data"].get("text") == "the report arrived" for e in events)
    assert err == "" and client.calls >= 2


async def test_a_run_without_children_does_not_poll_after_the_terminal_event():
    client = FakeClient([turn(1, "done")], [activity()])
    code, events, err = await asyncio.wait_for(go(client), 10)
    assert code == 0 and client.calls == 1 and err == ""


async def test_wait_zero_exits_at_the_first_terminal_event_with_a_note():
    client = FakeClient([turn(1, "spawned")], [activity(agents=2, wait=0)])
    code, events, err = await asyncio.wait_for(go(client), 10)
    assert code == 0 and client.calls == 1
    assert "2 subagent(s) still running" in err and "oneshot_wait_seconds = 0" in err


async def test_gives_up_at_the_bound_and_says_so():
    client = FakeClient([turn(1, "spawned")], [activity(agents=1, wait=1)])
    code, events, err = await asyncio.wait_for(go(client), 10)
    assert code == 0
    assert "1 subagent(s) still running" in err and "gave up after 1s" in err


async def test_an_older_daemon_without_activity_exits_at_the_first_terminal_event():
    client = FakeClient([turn(1, "done")], [])

    async def broken(session):
        raise RuntimeError("unknown command")

    client.session_activity = broken
    code, _events, err = await asyncio.wait_for(go(client), 10)
    assert code == 0 and err == ""


async def test_exit_code_is_the_last_turns():
    failed = [
        Event(type="turn.started", session="s", turn="t2", data={}),
        Event(type="turn.failed", session="s", turn="t2", data={"error": "boom"}),
    ]
    client = FakeClient([turn(1, "spawned"), failed], [activity(agents=1)], quiet_when_delivered=True)
    code, _events, _err = await asyncio.wait_for(go(client), 10)
    assert code == 1
