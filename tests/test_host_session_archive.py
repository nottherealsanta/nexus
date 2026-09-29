"""Archive sidecar projections and their host/client wire contract."""

from __future__ import annotations

import asyncio

import pytest

from nexus.client.protocol import Client, ClientError
from nexus.events import Event
from nexus.host import protocol as p
from nexus.host.facade import HostFacade
from nexus.host_support.session_archive import (
    _safe_transcript_text,
    reap_with_archive_sweep,
    search_sessions,
)
from nexus.session.manager import SessionManager


class _Transport:
    def __init__(self, facade: HostFacade):
        self.facade = facade

    async def request(self, command):
        return await self.facade.handle(command)

    async def aclose(self):
        return None

    async def events(self, *_args, **_kwargs):
        if False:
            yield None


def _archived_client(tmp_path) -> tuple[Client, SessionManager]:
    manager = SessionManager(tmp_path / "sessions")
    session = manager.open("archive-me")
    session.append_event(Event(type="turn.started", turn="t"))
    session.append_event(
        Event(
            type="input.started",
            data={
                "input_id": "input-1",
                "content": [{"type": "text", "text": "password=hunter2 searchword"}],
            },
            turn="t",
        )
    )
    session.append_event(Event(type="text", data={"text": "reply sk-123456789"}, turn="t"))
    session.append_event(Event(type="turn.completed", turn="t"))
    from nexus.model.message import Message, Text

    session.append_message(Message(role="user", content=[Text(text="password=hunter2 searchword")]))
    manager.archive("archive-me", "auto")
    facade = HostFacade(type("RuntimeStub", (), {"sessions": manager})())
    return Client(_Transport(facade)), manager


def test_archive_host_commands_preview_search_and_count(tmp_path):
    async def scenario():
        client, _manager = _archived_client(tmp_path)
        listing = await client.session_list_result()
        assert listing.sessions == []
        assert listing.archived_count == 1

        archived = await client.list_archived_sessions(limit=1)
        assert not archived.has_more
        row = archived.sessions[0]
        assert row.id == "archive-me"
        assert row.reason == "auto"
        assert row.message_count == 1
        assert row.created_at > 0

        preview = await client.preview_session(row.id)
        assert "user: password=*** searchword" in preview.text
        assert "assistant: reply ***" in preview.text
        assert not preview.truncated
        assert await client.search_sessions("searchword") == [row.id]

        unarchived = await client.unarchive_session(row.id)
        assert unarchived.id == row.id
        assert (await client.session_list_result()).archived_count == 0
        assert [item.id for item in await client.list_sessions()] == [row.id]

    asyncio.run(scenario())


def test_archived_pages_validate_bounds_and_page(tmp_path):
    client, manager = _archived_client(tmp_path)
    manager.open("other")
    manager.archive("other")

    async def scenario():
        first = await client.list_archived_sessions(limit=1)
        assert len(first.sessions) == 1
        assert first.has_more
        second = await client.list_archived_sessions(limit=1, cursor=1)
        assert len(second.sessions) == 1
        assert not second.has_more
        with pytest.raises(ClientError):
            await client.list_archived_sessions(limit=0)

    asyncio.run(scenario())


def test_archive_commands_round_trip_codec():
    command = p.SessionListArchived(query="needle", limit=5, cursor=10)
    assert p.decode_command(p.encode_command(command)) == command
    result = p.SessionListArchivedResult(sessions=[], has_more=True)
    assert p.decode_result(p.encode_result(result)) == result


def test_archive_reaper_sweeps_at_startup_before_idle_shutdown():
    async def scenario():
        stopped = asyncio.Event()
        calls = []

        async def sweep():
            calls.append("sweep")

        def stop(reason):
            calls.append(reason)
            stopped.set()

        await reap_with_archive_sweep(
            stopped, 0, sweep, lambda: True, stop, calls.append
        )
        assert calls == ["sweep", "daemon.idle_shutdown", "idle"]

    asyncio.run(scenario())


def test_archive_search_finds_content_in_the_state_database(tmp_path):
    # Legacy on-disk ``.json``/``.jsonl`` sessions are handled once, up front,
    # by ``nexus.session.import_legacy`` (STATE_PLAN §5.1) -- by the time a
    # session is searchable here it is already a database row.
    from nexus.model.message import Message, Text

    manager = SessionManager(tmp_path / "sessions")
    session = manager.open("findme")
    session.append_message(Message(role="user", content=[Text(text="needlecontent")]))
    manager.archive("findme", "auto")
    assert search_sessions(manager, query="needlecontent") == ["findme"]
    assert "\x9b" not in _safe_transcript_text("safe\x9b[31m")
