import asyncio
import json
import sqlite3

import pytest

from nexus.runtime import Conflict
from nexus.terminals import TerminalService


async def test_slow_controller_is_revoked_without_losing_disconnect(tmp_path):
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="one",
                          session_id=None, rows=24, cols=80)
    slow, fast = term.attach("slow"), term.attach("fast")
    slow_attachment = term.attachments[slow]
    fast_attachment = term.attachments[fast]
    slow_attachment.connected = fast_attachment.connected = True
    slow_attachment.queue = asyncio.Queue(maxsize=2)
    fast_attachment.queue = asyncio.Queue(maxsize=1000)
    try:
        term.control(slow, "acquire")
        term._emit("rename", name="one")
        term._emit("rename", name="two")
        assert slow not in term.attachments
        assert term.controller is None
        with pytest.raises(Conflict, match="expired or disconnected"):
            term.input(slow, 1, b"echo must-not-run\n")
        for _ in range(300):
            term._emit("rename", name="later")
        assert slow_attachment.queue.get_nowait() is None
        assert slow_attachment.queue.empty()
        frames = []
        while not fast_attachment.queue.empty():
            frames.append(fast_attachment.queue.get_nowait())
        assert [f["seq"] for f in frames] == sorted(f["seq"] for f in frames)
        assert any(f["type"] == "control" and f["controller"] is None for f in frames)
        term.control(fast, "acquire")
    finally:
        await service.shutdown()


async def test_detach_full_viewer_queue_always_ends_stream(tmp_path):
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="one",
                          session_id=None, rows=24, cols=80)
    aid = term.attach("viewer")
    attachment = term.attachments[aid]
    attachment.connected = True
    attachment.queue = asyncio.Queue(maxsize=1)
    attachment.queue.put_nowait({"type": "output"})
    try:
        term.detach(aid)
        assert attachment.queue.get_nowait() is None
        assert attachment.queue.empty()
    finally:
        await service.shutdown()


@pytest.mark.parametrize("status", ["running", "exited", "closed", "ended"])
def test_restart_never_retains_controller(status):
    db = sqlite3.connect(":memory:")
    TerminalService(db)
    metadata = {"id": "terminal", "project_id": "p", "status": status, "controller": "old-client"}
    db.execute("insert into terminal_resource values (?, ?)", ("terminal", json.dumps(metadata)))
    db.commit()
    restarted = TerminalService(db)
    assert restarted.metadata("terminal")["controller"] is None
    assert json.loads(db.execute("select metadata from terminal_resource").fetchone()[0])["controller"] is None
    if status == "running":
        assert restarted.metadata("terminal")["reason"] == "core_restart"
