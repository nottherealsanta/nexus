"""The retired background-subagent ``[agents]`` keys still parse and change nothing.

An older config that sets ``background`` or any report/slot bound keeps loading
(including values the old range checks rejected), a project file no longer
clamps them, and the runtime builds the same foreground subagent budget either
way.
"""

from __future__ import annotations

import dataclasses

import msgspec
from test_phase6_integration import drain, make_config, make_runtime, tool_result_for

from nexus.config import Config
from nexus.config.schema import AgentsSection
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response

LEGACY_KEYS = {
    "background": True,
    "max_live": 0,
    "max_pending_reports": 0,
    "max_report_batch": 0,
    "max_report_chars": 5,
    "report_coalesce_ms": 999_999,
    "max_auto_wakes": 5000,
    "max_followup_chars": 0,
    "oneshot_wait_seconds": 900,
}


def _home(tmp_path):
    home = tmp_path / "home"
    (home / ".nexus").mkdir(parents=True)
    return home


def test_every_legacy_key_parses_even_outside_the_old_bounds():
    section = msgspec.convert(LEGACY_KEYS, AgentsSection)
    assert section.background is True and section.max_live == 0
    assert section.oneshot_wait_seconds == 900
    # The live bounds are still validated.
    for bad in ({"max_concurrent": 0}, {"max_depth": -1}, {"max_fanout": 0}):
        try:
            msgspec.convert(bad, AgentsSection)
        except (msgspec.ValidationError, ValueError):
            continue
        raise AssertionError(f"{bad} should be rejected")


def test_legacy_keys_load_from_user_and_project_files_without_clamping(tmp_path):
    home = _home(tmp_path)
    lines = "\n".join(f"{key} = {str(value).lower()}" for key, value in LEGACY_KEYS.items())
    (home / ".nexus" / "config.toml").write_text(
        f"config_version = 2\n\n[agents]\n{lines}\n", encoding="utf-8"
    )
    (tmp_path / "nexus.toml").write_text(
        "config_version = 2\n\n[agents]\nmax_live = 64\noneshot_wait_seconds = 86400\n",
        encoding="utf-8",
    )
    agents = Config.load(tmp_path, home=home, environ={}).v2.agents
    assert agents.background is True
    # Ignored keys are plain overlays now; the old lower-only rule is gone.
    assert agents.max_live == 64 and agents.oneshot_wait_seconds == 86400
    assert agents.max_report_chars == 5


async def test_background_true_still_runs_the_child_in_the_foreground(tmp_path):
    base = make_config(max_concurrent=2)
    legacy = msgspec.structs.replace(
        base.v2.agents, background=True, max_live=1, max_followup_chars=1
    )
    config = dataclasses.replace(base, v2=msgspec.structs.replace(base.v2, agents=legacy))
    provider = ScriptedProvider(
        tool_response(
            ("a", "subagent", {"prompt": "one"}),
            ("b", "subagent", {"prompt": "two"}),
        ),
        text_response("report-one"),
        text_response("report-two"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config)
    session = runtime.session("root")
    events = await drain(session)

    # ``max_live = 1`` no longer refuses the second parallel child.
    bodies = {tool_result_for(session, call).content[0].text.partition("\n")[2].strip().split("\n")[0] for call in "ab"}
    assert bodies == {"report-one", "report-two"}
    assert [e.data["status"] for e in events if e.type == "agent.completed"] == ["completed"] * 2
    budget = runtime.agent_pool.budget("root")
    assert budget.max_concurrent == 2 and not hasattr(budget, "max_live")
    await runtime.aclose()
