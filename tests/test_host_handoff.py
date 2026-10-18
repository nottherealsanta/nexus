"""Transport-neutral handoff and native command contracts."""
from __future__ import annotations

import pytest

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from tests.test_host_facade import _runtime
from tests.test_ratatui_actions import _shell
from unittest.mock import AsyncMock, ANY


@pytest.mark.asyncio
async def test_host_cutover_read_retry_and_continuation(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("done")))
    facade = HostFacade(runtime)
    try:
        runtime.session("s")
        command = p.SessionHandoff(session="s", summary="brief", continuation="next", request_id="one")
        first = await facade.handle(command)
        await facade.wait_idle(timeout=5)
        retry = await facade.handle(command)
        assert first.data["fresh"] and not retry.data["fresh"]
        assert first.data["turn_id"] == retry.data["turn_id"]
        before = tuple(runtime.session("s").events)
        read = await facade.handle(p.SessionHandoffRead(session="s"))
        assert read.data["text"] == "brief"
        historical = await facade.handle(p.SessionHandoffRead(
            session="s", handoff_id=first.data["handoff_id"]))
        assert historical.data["text"] == "brief"
        assert tuple(runtime.session("s").events) == before
        assert len([e for e in before if e.type == "context.handoff"]) == 1
        assert len([e for e in before if e.type == "input.queued"]) == 1
    finally:
        await facade.shutdown()


@pytest.mark.asyncio
async def test_native_handoff_and_read_stay_in_same_tab(tmp_path, monkeypatch):
    handoff = AsyncMock()
    read = AsyncMock(return_value={"text": "safe brief"})
    shell = _shell(tmp_path, monkeypatch, handoff=handoff, read_handoff=read)
    await shell.command("/handoff", [])
    assert shell.controller.session == "s"
    shell.controller.bootstrap.assert_awaited_once()
    handoff.assert_awaited_once_with("s", request_id=ANY)
    await shell.command("/handoff", ["read"])
    assert shell.notice == "safe brief" and shell.controller.session == "s"
    with pytest.raises(ValueError, match="Usage"):
        await shell.command("/handoff", ["unexpected"])
