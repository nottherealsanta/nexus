"""Per-role allowed tiers: parsing, resolution, events, and the roster.

Plan: plans/done/SESSION_TITLE_PLAN.md, Part 3. A role's ``tiers:`` lists the tiers
it may run on, default first. The call may name a tier or a concrete model;
anything outside the list moves to the nearest allowed tier (never an error),
and the global ``max_tier`` is applied last.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.agents import AgentManager, TaskRequest
from nexus.agents.model import AgentParseError, parse_frontmatter
from tests.test_subagent_runner import Factory, Recorder, make_runner


def _write_role(tmp_path: Path, name: str, extra: str = "") -> None:
    agents = tmp_path / "ws" / ".agents" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {name} role\n{extra}---\nWork.\n",
        encoding="utf-8",
    )


# -- parsing ---------------------------------------------------------------


def _parse(extra: str):
    return parse_frontmatter(f"---\nname: a\ndescription: d\n{extra}---\nbody\n")


def test_tiers_parse_in_order_and_default_empty():
    assert _parse("tiers: [medium, high]\n").tiers == ("medium", "high")
    assert _parse("").tiers == ()
    # Like the other list fields, repeats collapse and the first stays default.
    assert _parse("tiers: [medium, low, medium]\n").tiers == ("medium", "low")


@pytest.mark.parametrize(
    "extra",
    [
        "tiers: []\n",
        "tiers: [Low]\n",
        "tiers: [a, b, c, d, e, f, g, h, i]\n",
        "tiers: [low]\nmodel: inherit\n",
    ],
)
def test_bad_tiers_are_definition_errors(extra):
    with pytest.raises(AgentParseError):
        _parse(extra)


def test_builtin_roles_declare_their_tiers(tmp_path):
    manager = AgentManager.for_workspace(tmp_path / "ws")
    found = {agent.name: agent.tiers for agent in manager.agents}
    assert found["quick"] == ("low",)
    assert found["task"] == ("low", "medium")
    assert found["advisor"] == ("medium", "high")
    assert found["build"] == ()


# -- resolution ------------------------------------------------------------


async def _spawn(tmp_path, role, model=None, **kwargs):
    factory = Factory()
    recorder = Recorder()
    runner = make_runner(
        tmp_path, factory, event_sink=recorder, parent_model="codex/gpt-6-luna", **kwargs
    )
    outcome = await runner.spawn(
        TaskRequest(prompt="x", subagent_type=role, model=model)
    )
    return outcome, factory.specs[-1], recorder


async def test_default_is_the_roles_first_tier(tmp_path):
    outcome, spec, _ = await _spawn(tmp_path, "advisor")
    assert (outcome.tier, spec.model) == ("medium", "medium")
    assert not outcome.clamped


async def test_allowed_tier_request_is_used(tmp_path):
    outcome, spec, recorder = await _spawn(tmp_path, "advisor", "high")
    assert (outcome.tier, spec.model) == ("high", "high")
    assert not outcome.clamped
    assert recorder.of("agent.spawned")[-1]["allowed_tiers"] == ["medium", "high"]


async def test_tier_outside_the_list_moves_to_nearest_and_says_so(tmp_path):
    outcome, spec, recorder = await _spawn(tmp_path, "quick", "high")
    assert (outcome.tier, spec.model) == ("low", "low")
    assert outcome.clamped and outcome.requested_tier == "high"
    assert "quick runs on low only" in outcome.render()
    assert "'high'" in outcome.render()
    clamped = recorder.of("agent.clamped")[-1]
    assert clamped["reason"] == "role" and clamped["allowed_tiers"] == ["low"]


async def test_lower_than_allowed_moves_up(tmp_path):
    outcome, _spec, _ = await _spawn(tmp_path, "advisor", "low")
    assert outcome.tier == "medium" and outcome.clamped


async def test_concrete_model_is_kept_when_its_tier_is_allowed(tmp_path):
    _outcome, spec, _ = await _spawn(tmp_path, "task", "codex/gpt-5-mini")
    assert spec.model == "codex/gpt-5-mini"


async def test_concrete_model_outside_the_list_runs_the_tier(tmp_path):
    # anthropic/claude-opus-5 is a built-in high-tier model.
    outcome, spec, _ = await _spawn(tmp_path, "quick", "anthropic/claude-opus-5")
    assert (outcome.tier, spec.model) == ("low", "low")
    assert outcome.clamped


async def test_max_tier_is_applied_after_the_role_list(tmp_path):
    outcome, _spec, recorder = await _spawn(tmp_path, "advisor", "high", max_tier="medium")
    assert outcome.tier == "medium" and outcome.clamped
    assert recorder.of("agent.clamped")[-1]["reason"] == "max_tier"
    assert "max_tier" in outcome.render()


async def test_unrunnable_tier_falls_through_then_to_the_parent_model(tmp_path):
    runnable = {"low": False, "medium": True}
    factory = Factory()
    runner = make_runner(
        tmp_path, factory, parent_model="codex/gpt-6-luna",
        tier_probe=lambda tier: runnable.get(tier, False),
    )
    await runner.spawn(TaskRequest(prompt="x", subagent_type="task"))
    assert factory.specs[-1].tier == "medium"
    runnable["medium"] = False
    await runner.spawn(TaskRequest(prompt="x", subagent_type="task"))
    assert factory.specs[-1].model == "codex/gpt-6-luna"


async def test_user_added_role_resolves_like_a_builtin(tmp_path):
    _write_role(tmp_path, "reviewer", "tiers: [medium, high]\n")
    outcome, spec, _ = await _spawn(tmp_path, "reviewer", "high")
    assert (outcome.tier, spec.model) == ("high", "high")
    outcome, _spec, _ = await _spawn(tmp_path, "reviewer", "low")
    assert outcome.tier == "medium" and outcome.clamped


async def test_pinned_concrete_model_is_kept(tmp_path):
    _write_role(tmp_path, "pinned", "tiers: [low, medium]\nmodel: codex/gpt-5-mini\n")
    _outcome, spec, _ = await _spawn(tmp_path, "pinned")
    assert spec.model == "codex/gpt-5-mini"


def test_permission_key_uses_the_final_tier(tmp_path):
    runner = make_runner(tmp_path, Factory())
    assert runner.permission_key({"prompt": "x", "subagent_type": "quick", "model": "high"}) == "quick:low"
    assert runner.permission_key({"prompt": "x", "subagent_type": "advisor"}) == "advisor:medium"


# -- roster ----------------------------------------------------------------


def test_roster_shows_tiers_and_the_default(tmp_path):
    _write_role(tmp_path, "custom", "tiers: [medium]\n")
    _write_role(tmp_path, "plain")
    lines = make_runner(tmp_path, Factory()).role_index().splitlines()
    assert any(line.startswith("quick [tiers: low]: ") for line in lines)
    assert any(line.startswith("task [tiers: low (default), medium]: ") for line in lines)
    assert any(line.startswith("advisor [tiers: medium (default), high]: ") for line in lines)
    assert any(line.startswith("custom [tiers: medium]: ") for line in lines)
    assert "plain: plain role" in lines


# -- tool text -------------------------------------------------------------


def test_tool_description_teaches_tier_choice_with_the_roster(tmp_path):
    from nexus.tools.builtin.task import make_task_spec

    runner = make_runner(tmp_path, Factory())
    spec = make_task_spec(runner)
    assert "Choosing a tier" in spec.description
    assert "Prefer the cheapest tier" in spec.description
    assert "task [tiers: low (default), medium]" in spec.description
    model = spec.input_schema["properties"]["model"]["description"]
    assert "nearest allowed tier" in model and "Omit it" in model


def test_roster_budget_keeps_every_builtin_role(tmp_path):
    names = [re_name.split(" ")[0] for re_name in make_runner(tmp_path, Factory()).role_index().splitlines()]
    assert {"quick", "task", "advisor"} <= set(names)


# -- doctor hint -----------------------------------------------------------


def test_doctor_lists_subagent_roles_without_tiers(tmp_path):
    from types import SimpleNamespace

    from nexus.host_support.doctor import _agents_without_tiers

    _write_role(tmp_path, "plain")
    _write_role(tmp_path, "tiered", "tiers: [low]\n")
    manager = AgentManager.for_workspace(tmp_path / "ws")
    assert _agents_without_tiers(SimpleNamespace(agents=manager)) == ["plain"]
    assert _agents_without_tiers(SimpleNamespace()) == []
