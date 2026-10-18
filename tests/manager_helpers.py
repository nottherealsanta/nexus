"""Shared fixtures for the manager host tests (plan 02): a manager session, a stub host, finished roots."""
from __future__ import annotations

from nexus.events import Event
from nexus.host_support.manager_port import ManagerLimits
from nexus.model.message import Message, MessageMeta, Text
from nexus.tools.manager_port import LaunchRefused
from test_session_ownership import managers

MANAGER_ID = "nexus-manager"


def user(text: str, turn: str = "t1") -> Message:
    return Message(role="user", content=[Text(text=text)], meta=MessageMeta(turn_id=turn))


def open_manager(root, text: str = "start a session to fix the flaky test", turn: str = "t1"):
    """The manager session with one real user prompt (record ``m1``) in turn ``turn``."""
    handle = root.open(MANAGER_ID, recover=False, ownership={"kind": "manager"})
    handle.append_message(user(text, turn))
    return handle


class Proxy:
    """A manager handle with a pinned turn, viewer and queue; events go to the real log."""

    def __init__(self, real, turn: str = "t1", attended: bool = True) -> None:
        self._real = real
        self.id = real.id
        self.active_turn_id = turn
        self.attended = attended
        self.queued_ids: tuple[str, ...] = ()

    def emit_session_event(self, event_type, data=None):
        return self._real.emit_session_event(event_type, data)


class StubHost:
    """Starts roots as plain rows; records what the port asked for."""

    def __init__(self, sessions, *, per_turn: int = 3, live: int = 8, keep_live: bool = False) -> None:
        self.sessions = sessions
        self.limit = ManagerLimits(per_turn, live)
        self.keep_live = keep_live
        self.fail = False
        self.started: list[tuple] = []
        self.cancelled: list[tuple[str, str]] = []

    def limits(self) -> ManagerLimits:
        return self.limit

    def new_session_id(self) -> str:
        return f"mgr-{len(self.started) + 1}"

    def check_agent(self, name: str) -> str:
        if name != "build":
            raise LaunchRefused("bad_agent", f"{name!r} is not a root role")
        return name

    async def start_root(self, session_id, instruction, *, agent, origin, title_text):
        if self.fail:
            raise RuntimeError("boom api_key=sk-secret1234567")
        self.started.append((session_id, instruction, agent, origin, title_text))
        child = self.sessions.open(session_id)
        child.append_message(Message(role="user", content=[Text(text=instruction)]))
        if self.keep_live:
            child.enqueue("still pending")  # a queued input keeps the root out of `idle`

    async def cancel_root(self, session_id, reason):
        self.cancelled.append((session_id, reason))
        return True


def complete(root, session_id, answer="done", outcome="completed"):
    """Finish a root's turn in its log (what a real turn leaves behind)."""
    handle = root.open(session_id)
    handle.append_event(Event(type="turn.started", turn="x1", data={}))
    handle.append_message(Message(role="assistant", content=[Text(text=answer)]))
    handle.append_event(Event(type=f"turn.{outcome}", turn="x1", data={}))
    return handle


def events_of(handle, *types):
    return [event for event in handle.read(force=True).events() if event.type in types]


__all__ = [
    "MANAGER_ID",
    "Proxy",
    "StubHost",
    "complete",
    "events_of",
    "managers",
    "open_manager",
    "user",
]
