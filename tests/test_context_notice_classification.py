"""Plan 13: notice replay and client dismissal never mutate context."""
from nexus.events import Event
from nexus.view import apply_many, initial_state
from nexus.view.notices import model_visible
from nexus.ui.ratatui.preferences import Preferences


def test_replay_classifies_legacy_and_explicit_notices():
    events = [Event(type="context.notice", data=data, seq=i, ts=float(i), session="s", id=f"e{i}")
              for i, data in enumerate([
                  {"kind": "subagent_roster", "message": "model context"},
                  {"kind": "client_status", "message": "notification"},
                  {"kind": "subagent_roster", "model_visible": False},
                  {"kind": "future_context", "model_visible": True},
              ], 1)]
    state = apply_many(initial_state("s"), events)
    assert [d.data["model_visible"] for d in state.diagnostics] == [True, False, False, True]
    assert apply_many(initial_state("s"), events).to_dict() == state.to_dict()
    assert "model_visible" not in events[0].data


def test_dismissal_persists_separately_from_durable_context(tmp_path):
    path = tmp_path / "tui.json"
    prefs = Preferences(path)
    identity = '["workspace","session",42]'
    prefs.set("dismissed_notices", [identity, identity])
    assert Preferences(path).values["dismissed_notices"] == [identity]
    assert model_visible({"kind": "subagent_reports"})
    assert not model_visible({"kind": "client_status"})


def test_projection_dismisses_only_notification(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui.prototype import project
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    events = [Event(type="context.notice", data={"kind": kind, "message": "full message"},
                    seq=i, ts=float(i), session="s", id=f"e{i}")
              for i, kind in enumerate(["tool_call_nudge", "client_status"], 1)]
    controller = SimpleNamespace(session="s", view=apply_many(initial_state("s"), events),
                                 agent_name="build", model="test", running=False, client=SimpleNamespace())
    shell = ShellActions(controller)
    before = project(controller, 1, shell=shell)["blocks"]
    context = next(b for b in before if b["kind"] == "context_notice")
    notice = next(b for b in before if b["kind"] == "notice")
    assert context["operation"] is None
    shell.preferences.set("dismissed_notices", [notice["operation"]["id"]])
    after = project(controller, 2, shell=shell)["blocks"]
    assert any(b["kind"] == "context_notice" for b in after)
    assert not any(b["kind"] == "notice" for b in after)
    assert controller.view == apply_many(initial_state("s"), events)
