from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock, Mock

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
async def test_snapshot_has_no_preview_and_poll_never_requests_one(monkeypatch):
    from nexus.ui.ratatui import bridge

    monkeypatch.setattr(bridge, "POLL_SECONDS", 0)
    calls = {"list": 0}

    class Client:
        async def project_sessions(self, *, limit=1000, query="", archived=0):
            calls["list"] += 1
            return SimpleNamespace(sessions=[], truncated=False)

        def __getattr__(self, name):
            assert "preview" not in name, name
            raise AttributeError(name)

    shell = SimpleNamespace(client=Client(), sessions_truncated=False, controller=SimpleNamespace(session="s"), seen_seq={})
    dashboard = SessionDashboard(shell)
    dashboard.visible = True
    assert not hasattr(dashboard, "preview") and not hasattr(dashboard, "refresh_preview")
    dashboard.task = asyncio.create_task(dashboard._poll())
    for _ in range(5):
        await asyncio.sleep(0)
    snap = dashboard.snapshot()
    assert "preview" not in snap
    assert set(snap) == {"kind", "rows", "selected", "query", "error", "truncated", "path", "branch", "worktree", "workspace"}
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
    snap = dashboard.snapshot()
    assert snap["path"] == "/work/nexus" and snap["branch"] == "main" and snap["workspace"] == "/work/nexus"
    assert snap["rows"][0]["branch"] == "main"
    assert snap["rows"][0]["last_user"] == "hello there"
    assert snap["rows"][0]["last_agent"] == "stored reply"
    assert snap["rows"][0]["activity"] == "grep notes.md"


def test_dashboard_accepts_header_actions_without_a_row():
    dashboard = _visible([])
    assert dashboard.accepts({"kind": "worktrees"})
    assert dashboard.accepts({"kind": "archived"})
    assert dashboard.accepts({"kind": "session_new"})


def test_dashboard_accepts_only_listed_rows_and_valid_titles():
    dashboard = _visible([{"id": "a", "workspace": "/w", "repo": "/r", "status": "idle", "state": "idle"}])
    assert dashboard.accepts({"kind": "session_archive", "id": "a"})
    assert not dashboard.accepts({"kind": "session_archive", "id": "other"})
    assert not dashboard.accepts({"kind": "session_preview", "id": "a"})
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
        async def project_sessions(self, *, limit=1000, query="", archived=0):
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
    list_gate = asyncio.Event()

    class Client:
        async def project_sessions(self, *, limit=1000, query="", archived=0):
            await list_gate.wait()
            return SimpleNamespace(sessions=[], truncated=False)

    dashboard = SessionDashboard(SimpleNamespace(client=Client(), controller=SimpleNamespace(session="s"), seen_seq={}))
    shell = _shell_with_dashboard(dashboard)
    dashboard.shell.workflows = shell.workflows
    dashboard.visible = True
    dashboard.task = asyncio.create_task(dashboard._poll())
    poll = dashboard.task
    refreshing = asyncio.create_task(dashboard.refresh())
    await asyncio.sleep(0)

    shell.show("Select model", "x")  # a picker/panel/detail replaces the dashboard

    assert not dashboard.visible and dashboard.snapshot() is None and dashboard.task is None
    list_gate.set()
    await refreshing
    await asyncio.sleep(0)
    assert poll.cancelled() or poll.done()
    assert dashboard.rows == []
    shell.show("Again", "y")  # closing is idempotent


@pytest.mark.asyncio
async def test_dashboard_operations_keep_it_open():
    dashboard = _visible([{"id": "a", "workspace": "/w", "status": "idle", "state": "idle"}])
    shell = _shell_with_dashboard(dashboard)
    for operation in ({"kind": "session_rename", "id": "a", "title": "T"},
                      {"kind": "session_archive", "id": "a"}, {"kind": "sessions_refresh"}):
        assert dashboard.accepts(operation)
    assert dashboard.visible and shell.workflows.dashboard.snapshot() is not None


@pytest.mark.asyncio
async def test_submit_targets_the_selected_dashboard_session():
    from unittest.mock import AsyncMock
    from nexus.ui.ratatui.actions import ShellActions

    enqueued = []

    class Client:
        async def enqueue(self, session, text, mode="steer", attachments=None, attachment_labels=None, client_request_id=""):
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
    shell.workflows.dashboard = dashboard

    assert await shell.submit("hello", session="other")
    assert enqueued == [("other", "hello", "steer", None)]
    dashboard.refresh.assert_awaited()

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


def test_rows_carry_local_time_buckets():
    import time
    from nexus.ui.ratatui.workflows import _time_bucket

    now = time.mktime((2026, 10, 14, 15, 0, 0, 0, 0, -1))
    day = 86400
    assert _time_bucket(now - 3600, now) == "Today"
    assert _time_bucket(now - day, now) == "Yesterday"
    assert _time_bucket(now - 3 * day, now) == "This week"
    assert _time_bucket(now - 9 * day, now) == "Earlier"
    assert _time_bucket(0.0, now) == "Earlier"

def _project_session(session_id, state="idle", *, archived=False, pending=None):
    from nexus.host.protocol import ProjectSession
    from nexus.session.manager import SessionSummary
    return ProjectSession(
        workspace="/w", project_id="p",
        session=SessionSummary(id=session_id, title=session_id, state=state, last_user_at=100.0, message_count=1),
        archived=archived, pending=pending or {})


def test_session_rows_carry_archived_status_and_pending_request():
    from nexus.host.protocol import ProjectSessionsListResult
    from nexus.ui.ratatui.workflows import session_rows

    pending = {"request_id": "req-1", "kind": "permission", "summary": "Allow Bash: git push\x1b[31m", "ts": 12.5}
    result = ProjectSessionsListResult(sessions=[
        _project_session("ask", "awaiting_permission", pending=pending),
        _project_session("old", archived=True),
        _project_session("plain"),
    ])
    rows = {row["id"]: row for row in session_rows(result, now=1000.0)}
    assert rows["ask"]["status"] == "input" and rows["ask"]["pending"]["request_id"] == "req-1"
    assert "\x1b" not in rows["ask"]["pending"]["summary"] and rows["ask"]["pending"]["ts"] == 12.5
    assert rows["old"]["status"] == "archived" and rows["old"]["archived"] is True
    assert rows["plain"]["pending"] == {} and rows["plain"]["archived"] is False


def test_dashboard_answers_only_the_pending_request_it_lists():
    pending = {"request_id": "req-1", "kind": "permission", "summary": "Allow Bash", "ts": 1.0}
    dashboard = _visible([
        {"id": "ask", "workspace": "/w", "status": "input", "state": "awaiting_permission", "pending": pending},
        {"id": "old", "workspace": "/w", "status": "archived", "state": "idle", "archived": True},
    ])
    ask = {"kind": "session_permission", "id": "ask", "request_id": "req-1", "decision": "allow_once"}
    assert dashboard.accepts(ask)
    assert dashboard.accepts({**ask, "decision": "deny_once"})
    assert not dashboard.accepts({**ask, "decision": "allow_always"}), "only once-decisions from the dashboard"
    assert not dashboard.accepts({**ask, "request_id": "req-2"})
    assert not dashboard.accepts({**ask, "id": "old"})
    assert dashboard.accepts({"kind": "archived_preview", "id": "old"})
    assert not dashboard.accepts({"kind": "archived_preview", "id": "ask"})


@pytest.mark.asyncio
async def test_permission_answer_goes_through_the_host_without_opening_the_session():
    dashboard = _visible([])
    resolve = AsyncMock(return_value=True)
    dashboard.shell.client = SimpleNamespace(resolve_permission=resolve)
    dashboard.refresh = AsyncMock()
    await dashboard.resolve_permission("ask", "req-1", "allow_once")
    resolve.assert_awaited_once_with("ask", "req-1", "allow_once")
    dashboard.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_requests_archived_rows_outside_search_and_keeps_them_out_of_tabs():
    from nexus.host.protocol import ProjectSessionsListResult
    shell = _search_shell()
    shell.client.project_sessions = AsyncMock(return_value=ProjectSessionsListResult(
        workspace="/w", sessions=[_project_session("live"), _project_session("old", archived=True)]))
    dash = SessionDashboard(shell)
    dash.visible = True
    await dash.refresh()
    shell.client.project_sessions.assert_awaited_with(limit=20, query="", archived=20)
    assert [row["id"] for row in dash.rows] == ["live", "old"]
    assert [row["id"] for row in shell.refresh_session_tabs.call_args.args[0]] == ["live"]
    assert dash.snapshot()["rows"][1]["status"] == "archived"
    dash.close()


def _search_shell():
    return SimpleNamespace(
        client=SimpleNamespace(project_sessions=AsyncMock(return_value=SimpleNamespace(sessions=[], truncated=False))),
        controller=SimpleNamespace(session="s", view=None), seen_seq={},
        refresh_session_tabs=Mock(), sessions_truncated=False,
    )


@pytest.mark.asyncio
async def test_remote_search_limits_clear_refresh_and_validation():
    shell = _search_shell()
    dash = SessionDashboard(shell)
    await dash.open()
    try:
        assert dash.accepts({"kind": "sessions_search", "query": "x" * 256})
        for query in (None, 42, "x" * 257):
            assert not dash.accepts({"kind": "sessions_search", "query": query})
        await dash.search("workspace needle")
        shell.client.project_sessions.assert_awaited_with(limit=1000, query="workspace needle", archived=20)
        assert dash.snapshot()["query"] == "workspace needle"
        await dash.refresh()
        shell.client.project_sessions.assert_awaited_with(limit=1000, query="workspace needle", archived=20)
        await dash.search("")
        shell.client.project_sessions.assert_awaited_with(limit=20, query="", archived=20)
        assert dash.snapshot()["query"] == ""
    finally:
        dash.close()


@pytest.mark.asyncio
async def test_remote_search_ignores_stale_query():
    shell = _search_shell()
    dash = SessionDashboard(shell)
    dash.visible = True
    old_gate = asyncio.Event()
    async def listing(*, limit, query, archived=0):
        if query == "old":
            await old_gate.wait()
        return SimpleNamespace(sessions=[], truncated=False)
    shell.client.project_sessions = AsyncMock(side_effect=listing)
    old = asyncio.create_task(dash.search("old"))
    await asyncio.sleep(0)
    await dash.search("new")
    old_gate.set()
    await old
    assert dash.snapshot()["query"] == "new"
    assert dash.rows == []
    dash.close()
