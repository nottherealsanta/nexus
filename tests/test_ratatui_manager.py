"""Native Manager entry: pinned row, open/Live routes, refusals (plan 02, docs/manager.md)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.cli import approve, commands
from nexus.ui.ratatui import live as live_module
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.bridge import SessionDashboard
from nexus.ui.ratatui.prototype import project
from nexus.ui_support import live_helper
from nexus.ui_support import manager as ui
from nexus.ui_support.completion import STATIC_ARGUMENTS
from nexus.view import initial_state
from nexus.view.model import PermissionView

from tests.test_ratatui_live import FakeCall, status


def state(**over) -> p.ManagerStateResult:
    base = dict(
        enabled=True, session="nexus-manager", state="idle", model="codex/gpt-6-luna",
        provider="codex", signed_in=True, codex_signed_in=True, live_consent=True,
        live_manager_consent=True, live_ready=True, managed=["a"],
    )
    base.update(over)
    return p.ManagerStateResult(**base)


def opened(session_id="nexus-manager"):
    return SimpleNamespace(session=SimpleNamespace(id=session_id), created=True)


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    FakeCall.instances, FakeCall.fail = [], None
    monkeypatch.setattr(live_module, "LiveCall", FakeCall)
    monkeypatch.setattr(live_module, "play_cue", lambda cue: None)
    monkeypatch.setattr(live_helper, "binary_path", lambda: tmp_path / "nexus-live")
    monkeypatch.setattr(live_helper, "available", lambda: True)
    client = SimpleNamespace(
        manager_state=AsyncMock(return_value=state()), manager_open=AsyncMock(return_value=opened()),
        live_status=AsyncMock(return_value=status()),
        live_consent=AsyncMock(return_value=p.LiveConsentResult(accepted=True)),
        live_stop=AsyncMock(), live_mute=AsyncMock(), live_helper_event=AsyncMock(),
        enqueue=AsyncMock(), shell=AsyncMock(), fork=AsyncMock(), select_reasoning_effort=AsyncMock(),
        list_agents=AsyncMock(), handoff=AsyncMock(), list_models=AsyncMock())
    controller = SimpleNamespace(session="s", client=client, view=initial_state("s"), agent_name="build",
                                 provider="codex", model="gpt-6-luna", reasoning_effort="high",
                                 supported_levels=["low", "high"])
    shell = ShellActions(controller)
    shell.workspace = "/w"

    async def switch(session):
        controller.session = session
        controller.view = initial_state(session)
        shell.tabs.append({"id": session, "title": "New Session", "workspace": "/w", "state": "idle"})

    shell.switch = AsyncMock(side_effect=switch)
    shell.live.watch_interval = 0.02
    return shell


# -- command -------------------------------------------------------------------------------


def test_manager_command_is_declared_and_completes():
    spec = commands.BY_NAME["/manager"]
    assert "open" in spec.usage and "live" in spec.usage and "status" in spec.usage
    assert commands.parse("/manager live").args == ("live",)
    assert STATIC_ARGUMENTS["/manager"] == ("open", "live", "status")
    assert "/manager" in commands.help_text()


async def test_bad_manager_argument_shows_usage(shell):
    with pytest.raises(ValueError, match="/manager"):
        await shell.command("/manager", ("bogus",))
    with pytest.raises(ValueError, match="/manager"):
        await shell.command("/manager", ("open", "extra"))


# -- pinned entry --------------------------------------------------------------------------


async def test_entry_is_read_from_manager_state_and_marks_the_open_session(shell):
    assert shell.manager.entry()["known"] is False  # nothing read yet: "Checking", never a guess
    await shell.manager.refresh()
    row = shell.manager.entry()
    assert row["summary"] == "Ready · 1 session launched" and not row["active"]
    shell.controller.session = "nexus-manager"
    assert shell.manager.entry()["active"] and shell.manager.is_active()
    assert not shell.manager.is_active("other") and not shell.manager.is_active("")


async def test_an_old_daemon_without_manager_state_is_shown_not_hidden(shell):
    shell.client.manager_state.side_effect = RuntimeError("unknown command ManagerState")
    await shell.manager.refresh()
    row = shell.manager.entry()
    assert not row["known"] and "unknown command ManagerState" in row["summary"] and row["tone"] == "warn"
    assert await shell.manager.open() is False
    shell.client.manager_open.assert_not_awaited()
    assert any(t["level"] == "error" for t in shell.toasts)


async def test_refresh_is_throttled_by_due(shell):
    assert shell.manager.due()
    await shell.manager.refresh()
    assert not shell.manager.due()


async def test_dashboard_pins_the_manager_apart_from_the_rows(shell):
    await shell.manager.refresh()
    dashboard = SessionDashboard(shell)
    dashboard.visible = True
    dashboard.rows = [{"id": "root", "title": "A root", "workspace": "/w", "repo": "", "status": "idle"}]
    snapshot = dashboard.snapshot()
    assert [row["id"] for row in snapshot["rows"]] == ["root"], "the manager is never a row"
    assert snapshot["manager"]["summary"].startswith("Ready") and snapshot["manager"]["model"].endswith("gpt-6-luna")
    for kind in ("manager_open", "manager_live"):
        assert dashboard.accepts({"kind": kind}), kind


async def test_dashboard_open_reads_the_manager_state_first(shell):
    shell.client.project_sessions = AsyncMock(return_value=SimpleNamespace(sessions=[], truncated=False))
    dashboard = SessionDashboard(shell)
    await dashboard.open()
    dashboard.close()
    shell.client.manager_state.assert_awaited()
    assert dashboard.snapshot() is None


# -- opening -------------------------------------------------------------------------------


async def test_open_creates_switches_and_remembers_the_session_it_replaced(shell):
    assert await shell.manager.open() is True
    shell.client.manager_open.assert_awaited_once_with()
    shell.switch.assert_awaited_once_with("nexus-manager")
    assert shell.controller.session == "nexus-manager"
    assert shell.manager.is_active()
    assert shell.manager.previous == ("/w", "s")


async def test_a_failed_switch_keeps_the_earlier_way_back(shell):
    shell.switch.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await shell.manager.open()
    assert shell.manager.previous is None


# -- the top-bar toggle (Ctrl+X A) ---------------------------------------------------------


@pytest.fixture
def tabbed(shell):
    shell.tabs = [{"id": "s", "title": "Work", "workspace": "/w", "state": "idle"},
                  {"id": "t", "title": "Other", "workspace": "/w", "state": "idle"}]
    return shell


async def test_toggle_opens_the_manager_then_restores_the_prior_session(tabbed):
    shell = tabbed
    await shell.manager.toggle()
    assert shell.controller.session == "nexus-manager" and shell.manager.is_active()
    await shell.manager.toggle()
    assert shell.controller.session == "s" and not shell.manager.is_active()
    assert shell.manager.previous is None
    shell.client.manager_open.assert_awaited_once_with()  # closing never opens, archives or deletes anything


async def test_toggle_restores_a_non_first_session(tabbed):
    shell = tabbed
    shell.controller.session = "t"
    await shell.manager.toggle()
    await shell.manager.toggle()
    assert shell.controller.session == "t"
    await shell.manager.toggle()
    await shell.manager.toggle()  # and again: the manager is a singleton, reopened each time
    assert shell.controller.session == "t" and shell.client.manager_open.await_count == 2


async def test_opening_again_from_the_manager_keeps_the_way_back(tabbed):
    shell = tabbed
    await shell.manager.open()
    await shell.manager.open()  # /manager while in the manager
    await shell.manager.toggle()
    assert shell.controller.session == "s"


async def test_toggle_falls_back_to_a_remaining_tab_when_the_prior_one_was_closed(tabbed):
    shell = tabbed
    await shell.manager.toggle()
    shell.tabs = [tab for tab in shell.tabs if tab["id"] != "s"]
    await shell.manager.toggle()
    assert shell.controller.session == "t"


async def test_toggle_with_nothing_to_return_to_starts_a_new_session(tabbed):
    shell = tabbed
    await shell.manager.toggle()
    shell.manager.previous = None  # e.g. the client started in the manager
    shell.tabs = [tab for tab in shell.tabs if tab["id"] == "nexus-manager"]
    shell.command = AsyncMock()
    await shell.manager.toggle()
    shell.command.assert_awaited_once_with("/new", ())


async def test_toggle_while_off_explains_and_changes_nothing(tabbed):
    shell = tabbed
    shell.client.manager_state.return_value = state(enabled=False, session="")
    await shell.manager.toggle()
    assert shell.panel_title == "Manager is off"
    assert shell.controller.session == "s" and shell.manager.previous is None
    shell.client.manager_open.assert_not_awaited()


def test_the_manager_is_never_drawn_as_a_session_tab(tabbed):
    from nexus.ui.ratatui.prototype import _tab_rows
    shell = tabbed
    shell.tabs.append({"id": "nexus-manager", "title": "Manager", "workspace": "/w", "state": "idle"})
    shell.controller.session = "nexus-manager"
    rows = _tab_rows(shell.controller, shell)
    assert [row["id"] for row in rows] == ["s", "t"] and not any(row["active"] for row in rows)
    shell.controller.session = "t"
    assert [row["id"] for row in _tab_rows(shell.controller, shell) if row["active"]] == ["t"]


def test_ctrl_x_a_and_the_top_bar_button_are_wired_natively():
    from pathlib import Path
    from nexus.ui_support.shortcuts import KEYBOARD_SHORTCUTS, LEADER_SHORTCUTS
    assert ("a", "toggle_manager") == next((k, a) for k, a, _ in LEADER_SHORTCUTS if a == "toggle_manager")
    assert any("Ctrl+X A" in line and "Manager" in line for line in KEYBOARD_SHORTCUTS)
    tui = Path("rust/tui/src/main.rs").read_text()
    assert "KeyCode::Char('A')" in tui and tui.count('"manager_toggle"') >= 2  # the chord and the click
    assert "TabHit::Manager" in Path("rust/tui/src/render/chrome.rs").read_text()
    desktop = Path("rust/desktop/src/panels.rs").read_text()
    assert '"manager-toggle"' in desktop and desktop.index('"manager-toggle"') < desktop.index('"sessions-toggle"')


async def test_slash_manager_and_pinned_operation_open_it(shell):
    await shell.command("/manager", ())
    assert shell.controller.session == "nexus-manager"
    shell.controller.session = "s"
    await shell.workflows.operate({"kind": "manager_open"})
    assert shell.controller.session == "nexus-manager" and shell.client.manager_open.await_count == 2


async def test_open_while_off_explains_and_never_calls_manager_open(shell):
    shell.client.manager_state.return_value = state(enabled=False, session="")
    assert await shell.manager.open() is False
    shell.client.manager_open.assert_not_awaited()
    shell.switch.assert_not_awaited()
    assert shell.panel_title == "Manager is off"
    text = "\n".join(shell.panel_lines)
    assert "[manager] enabled = false" in text and "enabled = true" in text and "restart the daemon" in text


async def test_open_when_signed_out_opens_and_warns_with_the_fix(shell):
    shell.client.manager_state.return_value = state(
        signed_in=False, sign_in="sign in to ChatGPT (Codex) under Settings > Providers")
    assert await shell.manager.open() is True
    assert shell.controller.session == "nexus-manager"
    toast = shell.toasts[-1]
    assert toast["level"] == "warning" and "Settings > Providers" in (toast["title"] + toast["body"])


async def test_status_command_labels_every_fact(shell):
    await shell.command("/manager", ("status",))
    assert shell.panel_title == "Manager status"
    text = "\n".join(shell.panel_lines)
    assert "Model: codex/gpt-6-luna" in text and "Cross-session notice: yes" in text


# -- no role / model / fork / shell on the manager ----------------------------------------


@pytest.mark.parametrize("name", ["/model", "/effort", "/agent", "/fork", "/handoff"])
async def test_manager_chat_refuses_session_configuration_commands(shell, name):
    shell.controller.session = "nexus-manager"
    with pytest.raises(ValueError) as caught:
        await shell.command(name, ())
    assert str(caught.value) == ui.REFUSED_COMMANDS[name]
    for call in ("fork", "select_reasoning_effort", "list_agents", "handoff", "list_models"):
        getattr(shell.client, call).assert_not_awaited()


async def test_ordinary_sessions_keep_those_commands(shell):
    shell.client.fork.return_value = SimpleNamespace(id="forked")
    await shell.command("/fork", ())
    shell.client.fork.assert_awaited_once()


async def test_manager_chat_has_no_shell_mode(shell):
    shell.controller.session = "nexus-manager"
    with pytest.raises(ValueError, match="no shell"):
        await shell.submit("!rm -rf /")
    shell.client.shell.assert_not_awaited()
    assert shell.composer_restore == "!rm -rf /", "the draft comes back"
    await shell.submit("start a session for the flaky test")
    shell.client.enqueue.assert_awaited_once()


# -- projection ----------------------------------------------------------------------------


async def test_snapshot_carries_the_entry_and_drops_role_model_effort_chrome_for_the_manager(shell):
    await shell.manager.refresh()
    ordinary = project(shell.controller, 1, shell=shell, literal=False)
    assert ordinary["manager"]["active"] is False
    assert ordinary["agent"] == "build" and ordinary["effort"] == "high"
    assert ordinary["title"] != "Manager"
    assert not any(label == "Kind" for label, _ in ordinary["details_panel"]["session"])

    await shell.manager.open()
    snap = project(shell.controller, 2, shell=shell, literal=False)
    assert snap["manager"]["active"] is True
    assert snap["agent"] == "manager" and snap["effort"] == "" and snap["title"] == "Manager"
    rows = dict(snap["details_panel"]["session"])
    assert rows["Kind"].startswith("Manager") and rows["Title"] == "Manager"
    assert "Agent" not in rows and "Effort" not in rows
    assert rows["Model"] == ui.MANAGER_MODEL_REF


def test_projection_without_a_shell_has_an_empty_manager_field():
    assert project(SimpleNamespace(view=initial_state("s")), 1)["manager"] == {}


def test_manager_rides_the_sessions_topic_on_the_desktop_wire():
    from nexus.ui.desktop.wire import DesktopWire

    wire = DesktopWire()
    base = {"schema": 2, "revision": 1, "generation": 1, "agent_page": "", "blocks": [], "blocks_from": 0,
            "sessions": [], "manager": {"summary": "Ready"}}
    wire.encode(base)
    encoded = wire.encode({**base, "revision": 2, "manager": {"summary": "Working"}})
    assert set(encoded["topics"]) == {"sessions"}, "the pinned entry never invalidates the (larger) header topic"
    assert encoded["topics"]["sessions"]["manager"] == {"summary": "Working"}


# -- Live on the manager -------------------------------------------------------------------


async def test_live_on_the_manager_needs_its_own_cross_session_consent(shell):
    await shell.manager.open()
    shell.client.live_consent.return_value = p.LiveConsentResult(accepted=False, scope="manager")
    await shell.command("/live", ())
    shell.client.live_consent.assert_awaited_once_with(None, scope="manager")
    assert FakeCall.instances == [] and shell.panel_title == "Use GPT Live with the Manager?"
    text = "\n".join(shell.panel_lines)
    assert "OpenAI" in text and "OTHER sessions" in text and "titles and status" in text
    assert "final reports" in text and "never approve" in text
    agree = shell.items[1]["operation"]
    assert agree["scope"] == "manager"
    shell.client.live_consent.reset_mock()
    shell.client.live_consent.return_value = p.LiveConsentResult(accepted=True, scope="manager")
    await shell.workflows.operate(agree)
    shell.client.live_consent.assert_awaited_once_with(True, scope="manager")
    assert shell.live.state == "active" and FakeCall.instances[0].session == "nexus-manager"
    await shell.live.shutdown()


async def test_live_on_the_manager_with_both_notices_goes_straight_to_the_call(shell):
    await shell.manager.open()
    await shell.command("/live", ())
    shell.client.live_consent.assert_awaited_once_with(None, scope="manager")
    assert shell.live.state == "active" and shell.panel_title != "Use GPT Live with the Manager?"
    await shell.live.shutdown()


async def test_the_base_notice_is_still_asked_first_for_the_manager(shell):
    await shell.manager.open()
    shell.client.live_status.return_value = status(consent=False)
    await shell.command("/live", ())
    assert shell.panel_title == "Use GPT Live with the Manager?" and FakeCall.instances == []
    shell.client.live_consent.assert_not_awaited()
    assert "audio from your microphone" in "\n".join(shell.panel_lines), "the base notice is part of it"


async def test_a_daemon_refusal_on_the_manager_reopens_the_manager_notice(shell):
    from nexus.client.protocol import FacadeError

    await shell.manager.open()
    FakeCall.fail = FacadeError("live_consent_required", "GPT Live needs your consent first")
    await shell.command("/live", ())
    assert shell.panel_title == "Use GPT Live with the Manager?" and shell.live.state == "idle"


async def test_manager_live_attaches_to_the_manager_and_leaves_the_foreground_alone(shell):
    await shell.workflows.operate({"kind": "manager_live"})
    assert shell.controller.session == "s", "Live never moves the session on screen"
    shell.switch.assert_not_awaited()
    assert shell.live.state == "active" and FakeCall.instances[0].session == "nexus-manager"
    assert shell.live.bar()["elsewhere"] == "Manager" and shell.live.bar()["manager"] is True
    await shell.live.shutdown()


async def test_manager_live_while_off_starts_nothing(shell):
    shell.client.manager_state.return_value = state(enabled=False, session="")
    await shell.command("/manager", ("live",))
    assert FakeCall.instances == [] and shell.live.state == "idle"
    shell.client.live_status.assert_not_awaited()


async def test_live_from_an_ordinary_session_is_on_the_manager_and_asks_for_its_notice(shell):
    await shell.command("/live", ())
    shell.client.live_consent.assert_awaited_once_with(None, scope="manager")
    assert FakeCall.instances[0].session == "nexus-manager" and shell.controller.session == "s"
    assert shell.live.state == "active"
    await shell.live.shutdown()


async def test_revoking_the_manager_scope_keeps_the_base_notice(shell):
    await shell.command("/live", ("revoke", "manager"))
    shell.client.live_consent.assert_awaited_once_with(False, scope="manager")
    assert "Manager" in shell.notice
    shell.client.live_consent.reset_mock()
    await shell.command("/live", ("revoke",))
    shell.client.live_consent.assert_awaited_once_with(False)
    with pytest.raises(ValueError, match="revoke \\[manager\\]"):
        await shell.command("/live", ("revoke", "other"))


# -- confirmation --------------------------------------------------------------------------


def permission_snapshot(**fields):
    view = initial_state("s")
    view.permissions.append(PermissionView(id="p1", ts=5.0, **fields))
    return project(SimpleNamespace(view=view), 1)["prompt"]


def test_an_ordinary_permission_still_offers_always_allow():
    prompt = permission_snapshot(tool="Bash", preview="ls", default_rule="Bash(ls:*)")
    assert {c["value"] for c in prompt["choices"]} == {"allow_once", "allow_always", "deny_once", "deny_always"}
    assert "what_rows" not in prompt


def test_manager_session_actions_have_no_special_prompt_presentation():
    # They never reach the user as permissions now; if one ever did, it is an ordinary prompt.
    prompt = permission_snapshot(tool="session_launch", key="fix tests", bundle="manager", preview="fix tests")
    assert prompt["title"] == "Allow session_launch fix tests?"
    assert "what_rows" not in prompt and not prompt.get("why")
    assert {c["value"] for c in prompt["choices"]} == {"allow_once", "allow_always", "deny_once", "deny_always"}
    data = {"tool": "session_launch", "key": "fix tests", "bundle": "manager"}
    assert not hasattr(approve, "PROMPT_ONCE")
    assert "Asked every time" not in approve.describe(data)
