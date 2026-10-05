"""A session left behind keeps its reduced view; returning re-reads only newer events."""
import pytest

from nexus.events import Event
from nexus.ui_support.session_controller import SessionController


class FakeClient:
    def __init__(self):
        self.streams = []
        self.log = {"a": 2, "b": 1}

    async def handshake(self): pass
    async def open_session(self, session): return None
    async def current_agent(self, session): return None

    async def stream(self, session, cursor, follow=False):
        self.streams.append((session, cursor))
        for seq in range(cursor + 1, self.log[session] + 1):
            yield Event(type="session.title", data={"title": f"t{seq}"}, session=session, seq=seq)


@pytest.mark.asyncio
async def test_returning_to_a_session_reads_only_events_after_its_cursor():
    client = FakeClient()
    controller = SessionController(client, "a")
    await controller.bootstrap()
    assert controller.cursor == 2 and client.streams == [("a", 0)]
    await controller.switch_session("b")
    await controller.bootstrap()
    assert client.streams[-1] == ("b", 0)
    client.log["a"] = 3  # work landed while "a" was in the background
    parked_view = None
    await controller.switch_session("a")
    assert controller.view.session_id == "a" and controller.cursor == 2
    parked_view = controller.view
    await controller.bootstrap()
    assert client.streams[-1] == ("a", 2) and controller.cursor == 3
    assert parked_view is not controller.view


@pytest.mark.asyncio
async def test_forgotten_or_unknown_sessions_rebuild_from_the_start():
    client = FakeClient()
    controller = SessionController(client, "a")
    await controller.bootstrap()
    await controller.switch_session("b")
    controller.forget("a")
    await controller.switch_session("a")
    await controller.bootstrap()
    assert client.streams[-1] == ("a", 0) and controller.cursor == 2
