"""Manager presentation shared by the native clients (ui_support/manager.py, docs/manager.md)."""
from __future__ import annotations

import pytest

from nexus.host import protocol as p
from nexus.ui_support import manager as ui
from nexus.ui_support.prompts import approval_choices


def state(**over) -> p.ManagerStateResult:
    base = dict(
        enabled=True, session="nexus-manager", state="idle", model="codex/gpt-6-luna",
        provider="codex", signed_in=True, codex_signed_in=True, live_consent=True,
        live_manager_consent=True, live_ready=True, managed=["a", "b"],
    )
    base.update(over)
    return p.ManagerStateResult(**base)


def test_constants_mirror_the_host_and_runtime_contracts():
    from nexus.agents.manager_prompt import MANAGER_MODEL_REF, MANAGER_SIGN_IN_HINT
    from nexus.host.facade import MANAGER_SESSION_ID

    assert ui.MANAGER_SESSION_ID == MANAGER_SESSION_ID
    assert ui.MANAGER_MODEL_REF == MANAGER_MODEL_REF
    assert ui.SIGN_IN_HINT == MANAGER_SIGN_IN_HINT
    assert not hasattr(ui, "CONFIRMED_TOOLS") and not hasattr(ui, "confirmation")


def test_ready_entry_shows_model_status_live_readiness_and_privacy():
    row = ui.entry(state(), current="s")
    assert row["known"] and row["enabled"] and row["can_open"] and not row["active"]
    assert (row["tone"], row["status"]) == ("ok", "idle")
    assert row["summary"] == "Ready · 2 sessions launched"
    assert row["model"] == "codex/gpt-6-luna"
    assert row["live"] == "Voice ready" and row["live_ready"]
    assert "other sessions' titles, status and reports" in row["privacy"]
    assert any("fixed, no fallback" in note for note in row["notes"])


def test_entry_marks_the_open_manager_and_maps_session_state():
    assert ui.entry(state(), current="nexus-manager")["active"]
    assert ui.entry(state(state="running"))["status"] == "working"
    assert ui.entry(state(state="awaiting_permission"))["status"] == "input"
    assert ui.entry(state(state="running"))["summary"].startswith("Working")
    assert ui.entry(state(state="awaiting_input"))["summary"].startswith("Needs you")
    assert not ui.entry(state(session=""), current="")["active"], "an uncreated manager is never 'open'"
    assert "1 session launched" in ui.entry(state(managed=["a"]))["summary"]
    assert ui.entry(state(managed=[]))["summary"] == "Ready"


def test_disabled_entry_says_off_and_how_to_turn_it_on_without_a_voice_route():
    row = ui.entry(state(enabled=False, session="", signed_in=False))
    assert (row["enabled"], row["can_open"], row["tone"]) == (False, False, "off")
    assert row["summary"] == "Off · [manager] enabled = false"
    assert "enabled = true" in row["notes"][0]
    assert not row["live"] and not row["live_ready"]


def test_signed_out_entry_names_the_exact_fix():
    row = ui.entry(state(signed_in=False, sign_in="sign in to ChatGPT (Codex) under Settings > Providers"))
    assert row["tone"] == "warn" and row["enabled"]
    assert row["summary"] == "Not signed in · sign in to ChatGPT (Codex) under Settings > Providers"
    assert any("turns fail until" in note for note in row["notes"])


@pytest.mark.parametrize("over,expect,ready", [
    (dict(codex_signed_in=False), "ChatGPT (Codex) sign-in", False),
    (dict(live_consent=False, live_manager_consent=False), "data notice", False),
    (dict(live_manager_consent=False), "cross-session notice", False),
    ({}, "Voice ready", True),
])
def test_live_readiness_line(over, expect, ready):
    text, ok = ui.live_line(state(**over))
    assert expect in text and ok is ready


def test_unread_or_failed_state_is_visible_not_silent():
    assert ui.entry(None)["summary"] == "Checking the manager…"
    failed = ui.entry(None, error="Manager unavailable: unknown command ManagerState")
    assert failed["tone"] == "warn" and "unknown command" in failed["summary"] and not failed["known"]


def test_entry_text_is_control_safe():
    row = ui.entry(state(sign_in="fix\x1b[31m it", signed_in=False, session="nexus-manager\x07"))
    assert "\x1b" not in row["summary"] and "\x07" not in row["session"]


def test_status_lines_label_every_fact():
    rows = ui.status_lines(state(managed=["x", "y"]))
    assert rows["Enabled"] == "yes" and rows["Model"] == "codex/gpt-6-luna"
    assert rows["Codex (manager model)"] == "signed in" and "GitHub Copilot" not in rows and rows["Codex (GPT Live)"] == "signed in"
    assert rows["Launched sessions"] == "x, y" and "3 per turn" in rows["Launch cap"]
    assert ui.status_lines(None) == {"Manager": "unknown"}


def test_manager_refuses_role_model_fork_and_shell_with_reasons():
    assert set(ui.REFUSED_COMMANDS) == {"/model", "/effort", "/agent", "/fork", "/handoff"}
    assert all(ui.REFUSED_COMMANDS.values()) and "shell" in ui.SHELL_REFUSED


def test_ordinary_approval_keeps_its_four_decisions():
    ordinary = approval_choices({"tool": "Bash", "persistence_available": True})
    assert {c.value for c in ordinary} == {"allow_once", "allow_always", "deny_once", "deny_always"}
    # Manager session actions no longer have a special confirmation presentation.
    assert not hasattr(ui, "is_confirmed") and not hasattr(ui, "approval_choices")


def test_manager_cli_parser_and_host_only_status(monkeypatch, tmp_path):
    import asyncio
    import io
    from nexus import cli
    from nexus.ui import cli as cli_surface

    class Client:
        closed = False

        async def manager_state(self):
            return state()

        async def aclose(self):
            self.closed = True

    client = Client()

    async def open_client(workspace):
        assert workspace == tmp_path
        return client

    monkeypatch.setattr(cli_surface, "open_client", open_client)
    parser = cli.build_parser()
    assert parser.parse_args(["manager"]).manager_action == "open"
    args = parser.parse_args(["manager", "status"])
    output = io.StringIO()
    asyncio.run(cli._manager_command(tmp_path, args, output))
    assert "codex/gpt-6-luna" in output.getvalue()
    assert client.closed


def test_manager_entry_exposes_launch_and_finder_guidance():
    notes = " ".join(ui.entry(state())["notes"])
    assert "Start a session" in notes and "starts right away" in notes and "confirm" not in notes
    assert "/sessions" in notes and "m returns" in notes
