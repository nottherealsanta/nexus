from types import SimpleNamespace
import asyncio

import pytest

from nexus.ui.ratatui.bridge import SessionDashboard
from nexus.ui.ratatui.workflows import find_tool


def test_plain_converts_structs_and_dataclasses_to_json_safe_dicts():
    import dataclasses
    import json

    import msgspec

    from nexus.ui.ratatui.bridge import _plain

    class Row(msgspec.Struct):
        id: str
        title: str
        tags: list[str]
        extra: dict

    @dataclasses.dataclass
    class Inner:
        name: str

    row = Row(id="a", title="hi\x1b[31m", tags=["x\x07"], extra={"inner": Inner("n\x1b")})
    plain = _plain({"rows": [row], "pair": (1, "ok")})
    assert plain["rows"][0]["id"] == "a"
    assert "\x1b" not in plain["rows"][0]["title"]
    assert "\x07" not in plain["rows"][0]["tags"][0]
    assert "\x1b" not in plain["rows"][0]["extra"]["inner"]["name"]
    assert plain["pair"] == [1, "ok"]
    assert json.loads(json.dumps(plain)) == plain


def test_full_nested_tool_lookup():
    root = SimpleNamespace(call_id="root")
    nested = SimpleNamespace(call_id="nested", result={"full": "payload"})
    view = SimpleNamespace(tools=[root], agents={"worker": SimpleNamespace(body=SimpleNamespace(tools=[nested], agents={}))})
    assert find_tool(view, "nested") is nested
    assert find_tool(view, "missing") is None


@pytest.mark.asyncio
async def test_preview_latest_selection_guard_and_close():
    pending = {}
    class Client:
        async def dashboard_preview(self, session_id, max_rows):
            assert max_rows == 12
            future = asyncio.get_running_loop().create_future()
            pending[session_id] = future
            return await future
    shell = SimpleNamespace(client=Client(), sessions_truncated=False)
    dashboard = SessionDashboard(shell)
    dashboard.visible = True
    first = asyncio.create_task(dashboard.select("first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(dashboard.select("second"))
    await asyncio.sleep(0)
    pending["second"].set_result({"session": "second", "title": "safe\x1b[31m"})
    await second
    pending["first"].set_result({"session": "first"})
    await first
    assert dashboard.preview["session"] == "second"
    assert "\x1b" not in dashboard.preview["title"]
    dashboard.close()
    assert dashboard.snapshot() is None


def _visible(rows):
    shell = SimpleNamespace(client=SimpleNamespace(), sessions_truncated=False, flash=lambda *a, **k: None,
                            toast=lambda *a, **k: None)
    dashboard = SessionDashboard(shell)
    dashboard.visible = True
    dashboard.rows = rows
    return dashboard


def test_snapshot_overlays_live_tails_and_keeps_a_stored_prompt():
    from nexus.ui.ratatui.bridge import SessionDashboard

    view = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", text="hello there"),
            SimpleNamespace(role="assistant", text=""),
        ],
        tools=[SimpleNamespace(name="grep", target="notes.md", status="running")],
    )
    shell = SimpleNamespace(
        client=SimpleNamespace(), sessions_truncated=False,
        controller=SimpleNamespace(session="a", view=view),
        breadcrumb="/work/nexus › main", workspace="/work/nexus",
    )
    dashboard = SessionDashboard(shell)
    dashboard.visible = True
    dashboard.selected = "a"
    dashboard.rows = [{
        "id": "a", "workspace": "/work/nexus", "repo": "", "worktree": "",
        "title": "A", "last_user": "old prompt", "last_agent": "stored reply",
    }]
    dashboard.preview = {"session": "a", "last_user": "stale", "last_agent": "stale reply", "activity": "old"}
    snap = dashboard.snapshot()
    assert snap["path"] == "/work/nexus" and snap["branch"] == "main" and snap["workspace"] == "/work/nexus"
    assert snap["rows"][0]["branch"] == "main"
    assert snap["rows"][0]["last_user"] == "hello there"
    assert snap["rows"][0]["last_agent"] == "stored reply"
    assert snap["rows"][0]["activity"] == "grep notes.md"
    assert snap["preview"]["last_user"] == "hello there"
    assert snap["preview"]["activity"] == "grep notes.md"
    assert snap["preview"]["last_agent"] == "stale reply"


def test_dashboard_accepts_header_actions_without_a_row():
    dashboard = _visible([])
    assert dashboard.accepts({"kind": "worktrees"})
    assert dashboard.accepts({"kind": "archived"})
    assert dashboard.accepts({"kind": "session_new"})


def test_dashboard_accepts_only_listed_rows_and_valid_titles():
    dashboard = _visible([{"id": "a", "workspace": "/w", "repo": "/r", "status": "idle", "state": "idle"}])
    assert dashboard.accepts({"kind": "session_preview", "id": "a"})
    assert not dashboard.accepts({"kind": "session_preview", "id": "other"})
    assert dashboard.accepts({"kind": "session_open", "id": "a", "workspace": "/w"})
    assert not dashboard.accepts({"kind": "session_open", "id": "a", "workspace": "/elsewhere"})
    assert dashboard.accepts({"kind": "session_rename", "id": "a", "title": "New"})
    assert not dashboard.accepts({"kind": "session_rename", "id": "a", "title": "  "})
    assert dashboard.accepts({"kind": "sessions_close"})
    dashboard.close()
    assert not dashboard.accepts({"kind": "sessions_close"})


@pytest.mark.asyncio
async def test_archive_refuses_working_and_waiting_sessions():
    from unittest.mock import AsyncMock
    dashboard = _visible([{"id": "w", "status": "working", "state": "running"},
                          {"id": "q", "status": "input", "state": "awaiting_input"},
                          {"id": "i", "status": "idle", "state": "idle"}])
    archive = AsyncMock()
    dashboard.shell.client = SimpleNamespace(archive_session=archive)
    flashes = []
    dashboard.shell.flash = lambda *args, **kwargs: flashes.append(args[0])
    await dashboard.archive("w")
    await dashboard.archive("q")
    archive.assert_not_awaited()
    assert len(flashes) == 2
    dashboard.refresh = AsyncMock()
    await dashboard.archive("i")
    archive.assert_awaited_once_with("i")


@pytest.mark.asyncio
async def test_rename_normalises_and_skips_empty_titles():
    from unittest.mock import AsyncMock
    dashboard = _visible([{"id": "a"}])
    rename = AsyncMock()
    dashboard.shell.client = SimpleNamespace(rename_session=rename)
    dashboard.refresh = AsyncMock()
    await dashboard.rename("a", "  two\n words  ")
    rename.assert_awaited_once_with("a", "two words")
    await dashboard.rename("a", "   ")
    assert rename.await_count == 1


def test_snapshot_is_hidden_while_a_panel_or_prompt_owns_input():
    from nexus.ui.ratatui.prototype import _dashboard_state
    dashboard = _visible([{"id": "a"}])
    shell = SimpleNamespace(panel_title="", workflows=SimpleNamespace(dashboard=dashboard, form=None))
    assert _dashboard_state(shell)["kind"] == "sessions"
    assert _dashboard_state(shell, prompt={"id": "p"}) is None
    shell.panel_title = "Models"
    assert _dashboard_state(shell) is None
    assert _dashboard_state(None) is None


@pytest.mark.asyncio
async def test_stale_list_refresh_cannot_publish_after_close():
    future = asyncio.get_running_loop().create_future()
    class Client:
        async def project_sessions(self):
            return await future
    dashboard = SessionDashboard(SimpleNamespace(client=Client(), controller=SimpleNamespace(session="s"), seen_seq={}))
    dashboard.visible = True
    task = asyncio.create_task(dashboard.refresh())
    await asyncio.sleep(0)
    dashboard.close()
    future.set_result(SimpleNamespace(sessions=[], truncated=False))
    await task
    assert dashboard.rows == []


def _shell_with_dashboard(dashboard):
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui.workflows import Workflows
    workflows = SimpleNamespace(form=None, dashboard=dashboard)
    workflows.close_dashboard = lambda: Workflows.close_dashboard(workflows)
    shell = SimpleNamespace(workflows=workflows, panel_revision=0, items=[])
    shell.show = lambda *a, **k: ShellActions.show(shell, *a, **k)
    return shell


@pytest.mark.asyncio
async def test_replacement_overlay_closes_dashboard_stops_polling_and_ignores_inflight():
    list_gate, preview_gate = asyncio.Event(), asyncio.Event()
    calls = {"preview": 0}

    class Client:
        async def project_sessions(self):
            await list_gate.wait()
            return SimpleNamespace(sessions=[], truncated=False)

        async def dashboard_preview(self, session_id, max_rows):
            calls["preview"] += 1
            await preview_gate.wait()
            return {"session": session_id}

    dashboard = SessionDashboard(SimpleNamespace(client=Client(), controller=SimpleNamespace(session="s"), seen_seq={}))
    shell = _shell_with_dashboard(dashboard)
    dashboard.shell.workflows = shell.workflows
    dashboard.visible = True
    dashboard.task = asyncio.create_task(dashboard._poll())
    poll = dashboard.task
    selecting = asyncio.create_task(dashboard.select("a"))
    refreshing = asyncio.create_task(dashboard.refresh())
    await asyncio.sleep(0)

    shell.show("Select model", "x")  # a picker/panel/detail replaces the dashboard

    assert not dashboard.visible and dashboard.snapshot() is None and dashboard.task is None
    list_gate.set()
    preview_gate.set()
    await selecting
    await refreshing
    await asyncio.sleep(0)
    assert poll.cancelled() or poll.done()
    assert dashboard.preview is None and dashboard.rows == []
    before = calls["preview"]
    await asyncio.sleep(0)
    assert calls["preview"] == before
    shell.show("Again", "y")  # closing is idempotent


@pytest.mark.asyncio
async def test_dashboard_operations_keep_it_open():
    dashboard = _visible([{"id": "a", "workspace": "/w", "status": "idle", "state": "idle"}])
    shell = _shell_with_dashboard(dashboard)
    for operation in ({"kind": "session_preview", "id": "a"}, {"kind": "session_rename", "id": "a", "title": "T"},
                      {"kind": "session_archive", "id": "a"}, {"kind": "sessions_refresh"}):
        assert dashboard.accepts(operation)
    assert dashboard.visible and shell.workflows.dashboard.snapshot() is not None


@pytest.mark.asyncio
async def test_submit_targets_the_selected_dashboard_session():
    from unittest.mock import AsyncMock
    from nexus.ui.ratatui.actions import ShellActions

    enqueued = []

    class Client:
        async def enqueue(self, session, text, mode="steer", attachments=None, attachment_labels=None):
            enqueued.append((session, text, mode, attachments))
            return ("t", "ok")

    shell = ShellActions(SimpleNamespace(client=Client(), session="current"))
    shell.workspace = "/w"
    dashboard = SessionDashboard(shell)
    dashboard.visible = True
    dashboard.rows = [
        {"id": "other", "workspace": "/w", "title": "Other"},
        {"id": "current", "workspace": "/w", "title": "Current"},
        {"id": "remote", "workspace": "/other", "title": "Remote"},
    ]
    dashboard.refresh = AsyncMock()
    dashboard.refresh_preview = AsyncMock()
    shell.workflows.dashboard = dashboard

    assert await shell.submit("hello", session="other")
    assert enqueued == [("other", "hello", "steer", None)]
    dashboard.refresh.assert_awaited()
    dashboard.refresh_preview.assert_awaited()

    shell.attachments.append(SimpleNamespace(attachment_id="a", kind="image"))
    assert await shell.submit("with file", session="other")
    assert shell.composer_restore == "with file"
    assert enqueued == [("other", "hello", "steer", None)]
    shell.attachments.clear()
    shell.composer_restore = ""

    switched = {}

    async def switch_project(workspace, session):
        switched["args"] = (workspace, session)
        shell.controller.session = session
        shell.workspace = workspace
        dashboard.close()

    shell.switch_project = switch_project
    assert await shell.submit("go", session="remote")
    assert switched["args"] == ("/other", "remote")
    assert enqueued[-1][0] == "remote"


@pytest.mark.asyncio
async def test_dismissed_preview_cannot_publish():
    future = asyncio.get_running_loop().create_future()
    class Client:
        async def dashboard_preview(self, *args, **kwargs):
            return await future
    dashboard = SessionDashboard(SimpleNamespace(client=Client()))
    dashboard.visible = True
    task = asyncio.create_task(dashboard.select("session"))
    await asyncio.sleep(0)
    dashboard.close()
    future.set_result({"session": "session"})
    await task
    assert dashboard.preview is None


def test_preview_tails_keep_agent_newlines_and_project_worked():
    from nexus.host_support.session_preview import message_tails, worked_label

    view = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", text="do the\nthing"),
            SimpleNamespace(role="assistant", text="# Title\n\n- a\x1b[31m\n" + "\n".join(f"line {i}" for i in range(60))),
        ],
        tools=[],
        turns=[
            SimpleNamespace(kind="agent", phase="completed", elapsed_ms=13_400),
            SimpleNamespace(kind="shell", phase="completed", elapsed_ms=900),
        ],
    )
    tails = message_tails(view)
    assert tails["last_user"] == "do the thing"
    lines = tails["last_agent"].split("\n")
    assert len(lines) <= 40 and lines[-1] == "line 59"
    assert not any(ord(c) < 32 for c in tails["last_agent"] if c != "\n")
    assert worked_label(view) == "13s"
    view.turns[0].elapsed_ms = 125_000
    assert worked_label(view) == "2m 05s"
    view.turns[0].phase = "active"
    assert worked_label(view) == ""
