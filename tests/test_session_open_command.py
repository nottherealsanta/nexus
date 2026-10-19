"""Chat `/open <name|id>` and `nexus sessions show` (plan 03 N2)."""
from __future__ import annotations

import argparse
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus import cli
from nexus.ui.cli import commands
from test_ratatui_actions import _shell


def test_open_is_a_shared_command_with_usage():
    spec = commands.BY_NAME["/open"]
    assert spec.usage == "<name|id>"
    parsed = commands.parse("/open quiet-cobalt-otter")
    assert parsed is not None and parsed.name == "/open" and parsed.args == ("quiet-cobalt-otter",)


@pytest.mark.asyncio
async def test_open_switches_by_resolved_logical_id(tmp_path, monkeypatch):
    resolve = AsyncMock(return_value={"logical_id": "alpha-1", "trashed": False})
    shell = _shell(tmp_path, monkeypatch, session_resolve=resolve)
    shell.switch = AsyncMock()
    await shell.command("/open", ("quiet-cobalt-otter",))
    resolve.assert_awaited_once_with("quiet-cobalt-otter")
    shell.switch.assert_awaited_once_with("alpha-1")
    with pytest.raises(ValueError, match="/open"):
        await shell.command("/open", ())


@pytest.mark.asyncio
async def test_open_reports_missing_and_trashed_without_switching(tmp_path, monkeypatch):
    resolve = AsyncMock(side_effect=RuntimeError("missing"))
    shell = _shell(tmp_path, monkeypatch, session_resolve=resolve)
    shell.switch = AsyncMock()
    await shell.command("/open", ("nope",))
    assert shell.notice == "No session named or numbered nope"
    resolve.side_effect = None
    resolve.return_value = {"logical_id": "x", "trashed": True}
    await shell.command("/open", ("old",))
    assert "trash" in shell.notice
    shell.switch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessions_show_prints_labelled_rows():
    client = SimpleNamespace(
        session_resolve=AsyncMock(return_value={
            "logical_id": "alpha-1", "name": "quiet-cobalt-otter", "kind": "root",
            "state": "idle", "project": "p1", "retired": False,
        }),
        list_sessions=AsyncMock(return_value=[SimpleNamespace(id="alpha-1", title="Fix the bug")]),
    )
    out = io.StringIO()
    code = await cli._dispatch_sessions(
        client, argparse.Namespace(session_action="show", session_id="quiet-cobalt-otter"), out
    )
    assert code == 0
    rows = dict(line.split(None, 1) for line in out.getvalue().splitlines())
    assert rows == {"name": "quiet-cobalt-otter", "id": "alpha-1", "title": "Fix the bug",
                    "kind": "root", "state": "idle", "project": "p1"}
