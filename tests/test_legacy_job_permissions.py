"""Permission compatibility for legacy shell-job tools and unified bash."""
from __future__ import annotations

import pytest

from nexus.tools.builtin.bash import SPEC as BASH_SPEC
from nexus.tools.names import canonical_tool_name
from nexus.tools.permissions import (
    Outcome,
    PermissionEngine,
    PermissionRuleError,
    parse_rule,
)
from nexus.tools.spec import ToolCall, ToolSpec


def _bash_call(action: str, job_id: str = "job_1") -> ToolCall:
    return ToolCall(
        id=f"{action}-{job_id}",
        name="bash",
        input={"action": action, "job_id": job_id},
    )


def _evaluate(
    *,
    action: str,
    job_id: str = "job_1",
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
):
    return PermissionEngine(mode="deny", allow=allow, deny=deny).evaluate(
        _bash_call(action, job_id), BASH_SPEC
    )


@pytest.mark.parametrize(
    ("legacy_rule", "actions"),
    [
        ("BashOutput", ("status", "wait")),
        ("KillShell", ("stop",)),
    ],
)
def test_bare_legacy_job_rule_translates_only_to_its_unified_actions(
    legacy_rule: str, actions: tuple[str, ...]
):
    for action in actions:
        allowed = _evaluate(action=action, allow=(legacy_rule,))
        denied = _evaluate(action=action, deny=(legacy_rule,))
        assert allowed.outcome is Outcome.ALLOW
        assert allowed.rule.raw == legacy_rule
        assert denied.outcome is Outcome.DENY
        assert denied.rule.raw == legacy_rule

    for action in {"run", "status", "wait", "stop"} - set(actions):
        assert _evaluate(action=action, allow=(legacy_rule,)).outcome is Outcome.DENY


@pytest.mark.parametrize(
    ("legacy_rule", "action", "matching_job", "other_job"),
    [
        ("BashOutput(job_1)", "status", "job_1", "job_2"),
        ("BashOutput(job_*)", "wait", "job_1", "other"),
        ("KillShell(job_1)", "stop", "job_1", "job_2"),
    ],
)
def test_keyed_legacy_job_rule_is_scoped_to_action_and_job(
    legacy_rule: str, action: str, matching_job: str, other_job: str
):
    assert _evaluate(
        action=action, job_id=matching_job, allow=(legacy_rule,)
    ).outcome is Outcome.ALLOW
    assert _evaluate(
        action=action, job_id=other_job, allow=(legacy_rule,)
    ).outcome is Outcome.DENY
    assert _evaluate(
        action=action, job_id=matching_job, deny=(legacy_rule,)
    ).outcome is Outcome.DENY
    assert _evaluate(
        action="run", job_id=matching_job, allow=(legacy_rule,)
    ).outcome is Outcome.DENY


@pytest.mark.parametrize(
    "rule", ["BashOutput(status:job_1)", "KillShell(stop:job_1)"]
)
def test_action_qualified_legacy_job_rules_are_rejected_with_migration_hint(
    rule: str,
):
    with pytest.raises(PermissionRuleError, match="unqualified job ID") as error:
        parse_rule(rule)
    assert "remove the action prefix" in str(error.value)


def test_legacy_rule_order_is_first_matching_rule_wins():
    evaluation = _evaluate(
        action="status",
        allow=("BashOutput(job_*)", "BashOutput"),
    )
    assert evaluation.outcome is Outcome.ALLOW
    assert evaluation.rule.raw == "BashOutput(job_*)"

    reversed_evaluation = _evaluate(
        action="status",
        allow=("BashOutput", "BashOutput(job_*)"),
    )
    assert reversed_evaluation.outcome is Outcome.ALLOW
    assert reversed_evaluation.rule.raw == "BashOutput"


@pytest.mark.parametrize("rule", ["BashOutput", "KillShell"])
def test_legacy_job_allow_does_not_grant_bash_run_command(rule: str):
    call = ToolCall(
        id="run",
        name="bash",
        input={"action": "run", "command": "status:job_1"},
    )
    evaluation = PermissionEngine(mode="deny", allow=(rule,)).evaluate(
        call, BASH_SPEC
    )
    assert evaluation.outcome is Outcome.DENY


def test_legacy_spelling_stays_opt_in_and_mixed_case_or_wildcard_heads_stay_exact():
    # Permission compatibility must not turn historical API spellings into
    # dispatch aliases; the manager still controls explicit opt-in.
    assert canonical_tool_name("BashOutput") == "BashOutput"
    assert canonical_tool_name("KillShell") == "KillShell"

    for rule in ("bashoutput", "BashO*utput", "BashOutput*"):
        assert _evaluate(action="status", allow=(rule,)).outcome is Outcome.DENY
        assert _evaluate(action="stop", allow=(rule,)).outcome is Outcome.DENY

    legacy_spec = ToolSpec(
        name="BashOutput",
        description="Legacy output tool",
        input_schema={"type": "object"},
        bundle="legacy_shell",
        permission_key=lambda data: str(data.get("job_id", "")),
    )
    legacy_call = ToolCall(
        id="legacy", name="BashOutput", input={"job_id": "job_1"}
    )
    legacy_evaluation = PermissionEngine(mode="deny", allow=("BashOutput",)).evaluate(
        legacy_call, legacy_spec
    )
    assert legacy_evaluation.outcome is Outcome.ALLOW
