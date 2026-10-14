"""Offline native projection and manual sign-in for Antigravity external work (no real ``agy``)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.host_support import provider_auth
from nexus.model.providers.antigravity_cli import LOGIN_INSTRUCTION
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import AgentView, BlockView, ConversationView, MessageView, ToolCallView, TurnView


def _project(tmp_path, monkeypatch, tools, *, agents=None, verbose=True):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    view.turns = [TurnView(id="t", index=0, phase="completed", messages=[
        MessageView(id="u", role="user", event_seq=1, blocks=[BlockView(text="go")])], tools=tools)]
    view.agents = agents or {}
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    shell.verbose = verbose
    return project(controller, 1, shell=shell)["blocks"]


def _external_agent(**external):
    body = ConversationView(session_id="a1", external=external)
    return AgentView(id="a1", type="explore", description="Scan", status="completed", body=body)


@pytest.mark.parametrize("provider, provenance, label", [
    ("antigravity", "Antigravity (agy) own tools", "Antigravity (agy)"),
    ("claude-code", "Claude Code (own tools)", "Claude Code"),
])
def test_task_card_and_tool_group_name_the_external_worker(tmp_path, monkeypatch, provider, provenance, label):
    task = ToolCallView(call_id="task-1", name="Task", event_seq=2, status="completed",
                        input={"subagent_type": "explore", "description": "Scan"}, child_agent_ids=["a1"])
    ext = ToolCallView(call_id="x1", name="run_command", event_seq=3, status="completed", executed=False,
                       input={"command": "ls"}, provenance=provenance)
    external = {"provider": provider, "finished": True, "status": "completed"}
    blocks = _project(tmp_path, monkeypatch, [task, ext], agents={"a1": _external_agent(**external)})
    card = next(b for b in blocks if b["id"] == "task-1")
    assert card["title"].endswith(f" · {label} (external, own tools)")
    group = next(b for b in blocks if b["kind"] == "tool_group")
    assert group["text"] == f"{label} ran 1 tool"
    assert all(provenance in m["heading"] for m in group["members"] if m["kind"] == "tool")


def test_antigravity_attention_note_names_agy_on_the_task_card(tmp_path, monkeypatch):
    task = ToolCallView(call_id="task-1", name="Task", event_seq=2, status="completed",
                        input={"subagent_type": "explore", "description": "Scan"}, child_agent_ids=["a1"])
    external = {"provider": "antigravity", "finished": True, "status": "needs_attention"}
    blocks = _project(tmp_path, monkeypatch, [task], agents={"a1": _external_agent(**external)})
    card = next(b for b in blocks if b["id"] == "task-1")
    assert "Antigravity (agy) is waiting for you" in card["detail"]
    assert "Claude" not in card["title"] + card["detail"]


async def test_antigravity_sign_in_is_a_manual_instruction_with_no_url_or_task(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    class Cli:
        async def status_view(self):
            return {"cli_present": True, "auth_verified": False, "message": "agy is installed; sign in there"}

    runtime = SimpleNamespace(workspace=home / "workspace", _home=home, _environ={},
                              _antigravity_cli_factory=lambda **_: Cli())
    rows = {row["id"]: row for row in (await provider_auth.providers_status(runtime))["providers"]}
    row = rows["antigravity"]
    assert row["methods"] == ["cli"] and row["can_logout"] is False and row["connected"] is False
    assert row["cli_present"] is True and row["auth_verified"] is False
    started = await provider_auth.provider_login(runtime, "antigravity")
    assert started["status"] == "manual" and started["message"] == LOGIN_INSTRUCTION
    assert not started.get("url")
    with pytest.raises(Exception, match="agy CLI"):
        await provider_auth.provider_logout(runtime, "antigravity")


async def test_fresh_provider_status_installed_manual_cli_and_custom_alias(tmp_path):
    from nexus.host_support import provider_auth
    from nexus.host import protocol as p
    made = []
    class Cli:
        async def status(self):
            return False
        async def status_view(self):
            return {"cli_present": True, "auth_verified": False, "connected": False,
                    "message": "Run agy in your terminal to sign in, then Re-check."}
    def factory(**kwargs):
        made.append(kwargs)
        return Cli()
    for sections, executable in (({}, None), ({"worker": SimpleNamespace(kind="agy", executable="/opt/custom-agy")}, "/opt/custom-agy")):
        config = SimpleNamespace(v2=SimpleNamespace(providers=sections))
        runtime = SimpleNamespace(_load_config=lambda: config, _antigravity_cli_factory=factory,
                                  _home=tmp_path, workspace=tmp_path, _environ={})
        status = await provider_auth.dispatch_providers(p.ProvidersStatus(), runtime)
        row = next(row for row in status.providers if row["id"] == "antigravity")
        assert row["cli_present"] and not row["connected"] and not row["auth_verified"]
        assert row["scope"] == "subagents" and row["capabilities"] == ["subagent"]
        assert row["methods"] == ["cli"] and not row["can_logout"]
        assert "Run agy" in row["detail"]
        assert made[-1]["executable"] == executable


async def test_provider_refresh_models_dispatches_the_provider_command_only():
    from nexus.ui.ratatui.settings_pages import providers
    calls = []
    class Client:
        async def provider_models_refresh(self, provider):
            calls.append(("provider", provider))
            return SimpleNamespace(message="3 models from agy", error="")
        async def refresh_models(self):
            calls.append("catalogue")  # must NOT be used
    flashes = []
    workflows = SimpleNamespace(client=Client(), page_state={}, shell=SimpleNamespace(flash=lambda m, level: flashes.append((m, level))))
    await providers.handle(workflows, {"key": "refresh_models", "provider": "antigravity"})
    assert calls == [("provider", "antigravity")] and flashes[0][1] == "info" and "3 models" in flashes[0][0]


async def test_antigravity_provider_page_shows_refresh_action_and_last_result():
    from nexus.ui.ratatui.settings_pages import providers
    probe = {"count": 4, "refreshed_at": 1_700_000_000.0, "error": ""}
    class Client:
        async def providers_status(self):
            return SimpleNamespace(providers=[{"id": "antigravity", "label": "Antigravity", "methods": ["cli"],
                                                "cli_present": False, "connected": False, "can_logout": False, "models_probe": probe}])
        async def setup_status(self):
            return SimpleNamespace(required=False)
    page = await providers.build(SimpleNamespace(client=Client(), page_state={}, login=None))
    section = next(b for b in page["blocks"] if b.get("id") == "prov-antigravity")
    button = next(item for block in section["blocks"] if block.get("t") == "buttons" for item in block["items"]
                  if item["label"] == "Refresh models")
    assert button["operation"] == {"kind": "sp_providers", "area": "providers", "key": "refresh_models", "provider": "antigravity"}
    assert "4 models · refreshed 20" in str(section["blocks"])
    assert "never" in providers._probe_summary({"count": 0, "refreshed_at": 0, "error": "boom"}) and "boom" in providers._probe_summary({"count": 0, "error": "boom"})


def test_antigravity_subagent_tier_items_keep_provider_neutral_note():
    from nexus.ui.ratatui.settings_pages.models import _items
    assert _items(["agy:model", "agy:other"], "agy:model", [
        {"ref": "agy:model", "scope": "subagents"}, {"ref": "agy:other", "scope": "subagents"},
    ]) == [
        ("agy:model", "subagents", "subagents only; titles and other jobs skip it"),
        ("agy:other", "subagents", "subagents only; titles and other jobs skip it"),
    ]
