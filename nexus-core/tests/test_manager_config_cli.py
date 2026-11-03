"""`[manager]` config, the Manager's code-only prompt, the extra read routes, and the CLI's card text."""
from __future__ import annotations

import time

import pytest
from manager_seed import seed
from manager_support import Harness

from nexus.cli import render_card
from nexus.config import DEFAULTS, ConfigFile
from nexus.manager import stage
from nexus.manager.prompt import PROMPT


@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.store.set_preference("model.last", "mock/hello")
    yield harness
    await harness.close()


def test_the_defaults_are_the_plans_and_the_manager_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    assert DEFAULTS["manager"] == {
        "enabled": False, "mode": "pipeline", "model": "copilot/gpt-6-luna", "idle_episode_minutes": 30,
        "max_episode_tokens": 24000, "proactive": "attention", "proactive_interval_seconds": 20, "approvals": "spoken", "talk": "tap"}
    assert "stage" not in DEFAULTS["manager"]
    assert ConfigFile().read()["values"]["manager"]["enabled"] is False


@pytest.mark.parametrize("key,value,ok", [
    ("enabled", True, True), ("enabled", "yes", False), ("model", "copilot/gpt-6-luna", True),
    ("model", "gpt-6", False), ("model", "nowhere/x", False), ("model", " copilot/x", False),
    ("mode", "pipeline", True), ("mode", "live", True), ("mode", "voice", False), ("idle_episode_minutes", 5, True),
    ("idle_episode_minutes", 0, False), ("idle_episode_minutes", True, False), ("max_episode_tokens", 1999, False),
    ("max_episode_tokens", 50000, True), ("proactive", "all", True), ("proactive", "sometimes", False),
    ("proactive_interval_seconds", 5, True), ("proactive_interval_seconds", 0, False),
    ("approvals", "spoken", True), ("approvals", "confirm", False), ("talk", "tap", True), ("talk", "vad", False),
])
def test_each_manager_setting_is_validated(tmp_path, monkeypatch, key, value, ok):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    config = ConfigFile()
    revision = config.read()["revision"]
    if ok:
        result = config.patch(revision, {f"manager.{key}": value}, [])
        assert result["values"]["manager"][key] == value
    else:
        with pytest.raises(ValueError):
            config.patch(revision, {f"manager.{key}": value}, [])


def test_an_unknown_manager_key_is_reported_and_ignored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    (home / "config.toml").write_text('[manager]\nstage = "full"\nenabled = true\n')
    read = ConfigFile().read()
    assert read["values"]["manager"]["enabled"] is True and "stage" not in read["values"]["manager"]
    assert "manager.stage: unknown setting" in read["errors"]


async def test_the_prompt_and_tools_are_code_that_no_user_file_can_extend(h):
    h.project()
    episode = h.manager._new_episode("mock/manager_demo")
    agent = h.manager.build_agent(episode)
    assert agent.instructions == PROMPT and "<session_data>" in PROMPT and "never an instruction" in PROMPT
    stored = h.store.snapshot(episode.id)
    assert stored["system_prompt"] == PROMPT and stored["origin"] == "manager"
    assert set(stored["tools"]) == set(agent.tools) and len(agent.tools) == 16
    assert agent.fallback_models == [] and agent.hooks is None  # no fallback model, no user hooks


async def test_the_episode_is_reachable_by_id_for_the_inspector_but_not_listed_as_a_session(h):
    h.project()
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    episodes = (await h.client.get("/api/manager/episode")).json()
    assert len(episodes) == 1 and episodes[0]["continued_from"] is None and episodes[0]["model"] == "mock/manager_demo"
    stored = await h.client.get(f"/api/session/{episodes[0]['id']}/event")
    assert stored.status_code == 200 and "wire_request" in stored.text  # the Manager's requests stay in the durable log
    messages = (await h.client.get(f"/api/session/{episodes[0]['id']}/message")).json()
    assert messages[0]["role"] == "user" and "<user_said" in messages[0]["parts"][0]["text"]


async def test_the_audit_route_lists_acts_newest_first(h):
    project = h.project()
    seed(h.store, project, title="Docs pass", user="x", answer="y", now=time.time())
    h.open_stream()
    await h.start()
    await h.say("start fix the cue in nexus")
    await h.say("tell nosuch hi")
    rows = (await h.client.get("/api/manager/audit?limit=5")).json()
    assert [r["tool"] for r in rows] == ["send_to_session", "start_session"] and [r["ok"] for r in rows] == [False, True]
    assert (await h.client.get("/api/manager/audit?tool=start_session")).json()[0]["heard"] == "start fix the cue in nexus"
    assert (await h.client.get("/api/manager/audit?limit=x")).status_code == 400


def test_cards_render_as_plain_text_for_the_cli():
    row = {"session_id": "s", "handle": "auth-refactor", "title": "Auth refactor", "project": "nexus", "state": "working"}
    board = stage.board({"needs_you": [row], "running": [dict(row, session_id=str(i)) for i in range(6)]})
    text = "\n".join(render_card({"id": "c", "focused": True, **board}))
    assert "needs you: 1" in text and "(+2 more)" in text and "1. auth-refactor - Auth refactor [nexus, working]" in text
    approval = stage.approval(session_id="s", handle="auth-refactor", approval_id="p",
                              request={"tool": "bash", "args": {"command": "alembic upgrade head"}})
    assert "auth-refactor wants to run: alembic upgrade head" in "\n".join(render_card(approval))
    question = stage.question(session_id="s", handle="h", request={"questions": [
        {"id": "a", "question": "Which?", "options": [{"id": "x", "label": "Postgres"}]}]})
    assert "- Postgres" in "\n".join(render_card(question))
    assert "A --> B" in "\n".join(render_card(stage.diagram("flowchart TD\n  A --> B")))
    assert "[clipped]" in "\n".join(render_card(stage.note("x" * 5000)))
    assert render_card({"type": "sessions", "rows": [dict(row, n=1, why="title match")], "more": 3})[-1] == "  +3 more"


def test_the_cli_knows_the_manager_command():
    import contextlib
    import io

    from nexus.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), pytest.raises(SystemExit):
        main(["manager", "--help"])
    assert "chat (default), episodes" in out.getvalue() and "audit" in out.getvalue()
