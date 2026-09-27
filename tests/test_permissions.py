"""Table-driven, adversarial tests for the permission engine (plan section 5.3)."""
from __future__ import annotations

import asyncio
import json

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection
from nexus.tools.builtin import read as read_tool
from nexus.tools.permissions import (
    ApprovalBroker,
    Decision,
    Grant,
    Outcome,
    PathGuard,
    PathSecurityError,
    PermissionEngine,
    PermissionRequest,
    PermissionRuleError,
    PermissionTargetRequest,
    collect_grants,
    exact_rule,
    grant_for_decision,
    parse_rule,
)
from nexus.tools.spec import PathTarget, ToolCall, ToolContext, ToolSpec

BASH_SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}}}


def bash_spec(*, permission_key=True) -> ToolSpec:
    return ToolSpec(
        name="Bash",
        description="Run a command",
        input_schema=dict(BASH_SCHEMA),
        bundle="shell",
        mutates=True,
        permission_key=(lambda data: str(data["command"])) if permission_key else None,
    )


def read_spec(*, permission_key=True, mutates=False) -> ToolSpec:
    return ToolSpec(
        name="Read",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        bundle="fs",
        mutates=mutates,
        permission_key=(lambda data: str(data["path"])) if permission_key else None,
    )


def multi_path_spec(*, scalar_key="scalar-key") -> ToolSpec:
    return ToolSpec(
        name="Multi",
        description="Mutate multiple paths",
        input_schema={"type": "object"},
        bundle="fs",
        mutates=True,
        permission_key=lambda _data: scalar_key,
        multi_path_targets=lambda data: tuple(
            PathTarget(role, path) for role, path in data["targets"]
        ),
    )


def call(name: str, call_id: str = "c1", **payload) -> ToolCall:
    return ToolCall(id=call_id, name=name, input=dict(payload))


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("Read", "tool"),
        ("mcp__server__tool", "tool"),
        ("Read(**)", "tool_pattern"),
        ("Bash(git diff*)", "tool_pattern"),
        ("Bundle:fs", "bundle"),
        ("mcp__server__*", "wildcard"),
        ("mcp__server__get_*", "wildcard"),
        ("Bash(test?)", "tool_pattern"),
    ],
)
def test_parse_rule_valid(raw, kind):
    rule = parse_rule(raw)
    assert rule.kind == kind
    assert rule.raw == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        " Read",
        "Read ",
        "Read\x00",
        "Bundle:",
        "Bundle:fs(**)",
        "Bundle:fs:extra",
        "Bundle:1bad",
        "Bundle:fs*",
        "a:b",
        "Read(",
        "Read)",
        "Read(a(b))",
        "Read(a)b)",
        "Read()",
        "(Read)",
        "1Bad",
        "Read(*",
        "Read(**)(",
        "Read(**)extra",
        "only:a:colon",
    ],
)
def test_parse_rule_invalid(raw):
    with pytest.raises(PermissionRuleError):
        parse_rule(raw)


def test_parse_rule_rejects_non_string():
    with pytest.raises(PermissionRuleError):
        parse_rule(None)
    with pytest.raises(PermissionRuleError):
        parse_rule(5)


@pytest.mark.parametrize(
    "raw,tool,key,bundle,expected",
    [
        ("Read", "Read", "/x", "fs", True),
        ("Read", "Write", "/x", "fs", False),
        ("Read(**)", "Read", "/x", "fs", True),
        ("Read(**)", "Read", None, "fs", False),
        ("Read(**/.env)", "Read", "/ws/.env", "fs", True),
        ("Read(**/.env)", "Read", "/ws/a.txt", "fs", False),
        ("Bundle:fs", "Read", "/x", "fs", True),
        ("Bundle:fs", "Bash", "ls", "shell", False),
        ("mcp__server__*", "mcp__server__echo", None, "mcp", True),
        ("mcp__server__*", "other", None, "mcp", False),
        ("Bash(git diff*)", "Bash", "git diff --stat", "shell", True),
        ("Bash(git diff*)", "Bash", "git status", "shell", False),
    ],
)
def test_rule_matching(raw, tool, key, bundle, expected):
    assert parse_rule(raw).matches(tool, key, bundle) is expected


def test_legacy_names_map_exactly_without_casefold_or_wildcard_rewriting():
    assert parse_rule("Task(explore:high)").matches("subagent", "explore:high", "task")
    assert parse_rule("Read(**/.env)").matches("read", "/ws/.env", "fs")
    assert parse_rule("LS").matches("ls", None, "fs")
    assert parse_rule("MultiEdit").matches("multiedit", None, "fs")
    assert parse_rule("BashOutput").matches("BashOutput", None, "legacy_shell")
    # A wildcard over historical spelling is not rewritten into a broader
    # wildcard over the canonical tool name.
    assert not parse_rule("Tas*").matches("subagent", None, "task")
    assert parse_rule("task(explore:high)").tool == "task"
    assert parse_rule("Read(**/.env)").pattern == "**/.env"
    assert not parse_rule("MULTIEDIT").matches("multiedit", None, "fs")
    assert not parse_rule("LS*").matches("ls", None, "fs")


@pytest.mark.parametrize(
    "rule_bundle,actual_bundle,tool",
    [
        ("shell", "shell", "bash"),
        ("shell", "legacy_shell", "KillShell"),
        ("fs", "fs", "read"),
        ("fs", "legacy_fs", "multiedit"),
    ],
)
def test_historical_bundle_rules_match_bounded_split_union(
    rule_bundle, actual_bundle, tool
):
    assert parse_rule(f"Bundle:{rule_bundle}").matches(tool, None, actual_bundle)


def test_explicit_legacy_bundle_rule_remains_distinct():
    legacy_shell = parse_rule("Bundle:legacy_shell")
    assert legacy_shell.matches("KillShell", None, "legacy_shell")
    assert not legacy_shell.matches("bash", None, "shell")
    assert parse_rule("Bundle:shell").matches("bash", None, "shell")


@pytest.mark.parametrize(
    "rule,tool,bundle",
    [
        ("Bundle:shell", "KillShell", "legacy_shell"),
        ("Bundle:fs", "multiedit", "legacy_fs"),
    ],
)
def test_historical_bundle_deny_and_allow_apply(rule, tool, bundle):
    spec = ToolSpec(
        name=tool,
        description=tool,
        input_schema={"type": "object"},
        bundle=bundle,
    )
    denied = _engine(mode="allow", deny=[rule]).evaluate(call(tool), spec)
    assert denied.outcome is Outcome.DENY
    assert denied.code == "deny"
    assert denied.rule.raw == rule

    allowed = _engine(mode="deny", allow=[rule]).evaluate(call(tool), spec)
    assert allowed.outcome is Outcome.ALLOW
    assert allowed.code == "allow"
    assert allowed.rule.raw == rule

    asked = _engine(mode="deny", ask=[rule]).evaluate(call(tool), spec)
    assert asked.outcome is Outcome.ASK
    assert asked.code == "ask"
    assert asked.rule.raw == rule


def test_historical_bundle_match_preserves_first_match_order():
    spec = ToolSpec(
        name="KillShell",
        description="KillShell",
        input_schema={"type": "object"},
        bundle="legacy_shell",
    )
    legacy_first = _engine(
        mode="deny", allow=["Bundle:legacy_shell", "Bundle:shell"]
    ).evaluate(call("KillShell"), spec)
    historical_first = _engine(
        mode="deny", allow=["Bundle:shell", "Bundle:legacy_shell"]
    ).evaluate(call("KillShell"), spec)
    assert legacy_first.rule.raw == "Bundle:legacy_shell"
    assert historical_first.rule.raw == "Bundle:shell"


@pytest.mark.parametrize(
    "legacy,canonical",
    [("LS", "ls"), ("MultiEdit", "multiedit")],
)
def test_legacy_exact_deny_applies_to_canonical_opt_in_tool(legacy, canonical):
    spec = ToolSpec(
        name=canonical,
        description=canonical,
        input_schema={"type": "object"},
        bundle="fs",
    )
    evaluation = _engine(mode="allow", deny=[legacy]).evaluate(
        call(canonical), spec
    )
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "deny"
    assert evaluation.rule.raw == legacy


# ---------------------------------------------------------------------------
# Precedence: deny -> session grants -> allow -> ask -> mode
# ---------------------------------------------------------------------------


def _engine(**kwargs) -> PermissionEngine:
    defaults = {"mode": "ask"}
    defaults.update(kwargs)
    return PermissionEngine(**defaults)


def test_deny_is_absolute_over_allow_and_grants():
    engine = _engine(allow=["Bash(**)"], deny=["Bash(rm -rf*)"])
    evaluation = engine.evaluate(
        call("Bash", command="rm -rf /"),
        bash_spec(),
        grants=[Grant(effect="allow", rule="Bash")],
    )
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "deny"
    assert evaluation.rule.raw == "Bash(rm -rf*)"


def test_deny_beats_session_allow_grant():
    engine = _engine(allow=["Bash(**)"])
    evaluation = engine.evaluate(
        call("Bash", command="rm -rf /"),
        bash_spec(),
        grants=[Grant(effect="allow", rule="Bash")],
    )
    assert evaluation.outcome is Outcome.ALLOW  # no deny rule present

    engine = _engine(deny=["Bash"])
    evaluation = engine.evaluate(
        call("Bash", command="anything"),
        bash_spec(),
        grants=[Grant(effect="allow", rule="Bash")],
    )
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "deny"


def test_session_allow_grant_beats_allow_and_ask():
    engine = _engine(mode="deny", ask=["Bash"], allow=[])
    evaluation = engine.evaluate(
        call("Bash", command="ls"),
        bash_spec(),
        grants=[Grant(effect="allow", rule="Bash")],
    )
    assert evaluation.outcome is Outcome.ALLOW
    assert evaluation.code == "session_grant"


def test_session_deny_grant_beats_allow_rule():
    engine = _engine(allow=["Bash(**)"])
    evaluation = engine.evaluate(
        call("Bash", command="ls"),
        bash_spec(),
        grants=[Grant(effect="deny", rule="Bash(ls)")],
    )
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "session_grant_deny"


def test_allow_rule_beats_ask_and_mode():
    engine = _engine(mode="deny", ask=["Bash"], allow=["Bash(**)"])
    assert engine.evaluate(call("Bash", command="ls"), bash_spec()).outcome is Outcome.ALLOW


def test_ask_rule_beats_mode_allow():
    engine = _engine(mode="allow", ask=["Bash"])
    evaluation = engine.evaluate(call("Bash", command="ls"), bash_spec())
    assert evaluation.outcome is Outcome.ASK
    assert evaluation.code == "ask"


@pytest.mark.parametrize(
    "mode,expected",
    [("deny", Outcome.DENY), ("allow", Outcome.ALLOW), ("ask", Outcome.ASK)],
)
def test_mode_is_the_final_default(mode, expected):
    engine = _engine(mode=mode)
    assert engine.evaluate(call("Bash", command="ls"), bash_spec()).outcome is expected


def test_first_matching_rule_wins_within_a_category():
    engine = _engine(mode="deny", allow=["Bash(git status)", "Bash(**)"])
    evaluation = engine.evaluate(call("Bash", command="git status"), bash_spec())
    assert evaluation.rule.raw == "Bash(git status)"


def test_unknown_tool_is_denied_closed():
    engine = _engine(mode="allow")
    evaluation = engine.evaluate(call("Nope"), None)
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "unknown_tool"


def test_permission_key_error_fails_closed():
    spec = ToolSpec(
        name="Read",
        description="Read",
        input_schema={"type": "object"},
        bundle="fs",
        permission_key=lambda data: 1 / 0,
    )
    evaluation = _engine(mode="allow").evaluate(call("Read", path="/x"), spec)
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "permission_key_error"


def test_tool_pattern_does_not_match_when_key_is_missing():
    engine = _engine(mode="deny", allow=["Read(**)"])
    evaluation = engine.evaluate(call("Read", path="/x"), read_spec(permission_key=False))
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "mode_deny"


# ---------------------------------------------------------------------------
# Decisions, once/always modeling, and grants
# ---------------------------------------------------------------------------


def test_decision_properties():
    assert Decision.ALLOW_ONCE.allows and not Decision.ALLOW_ONCE.persists
    assert Decision.ALLOW_ALWAYS.allows and Decision.ALLOW_ALWAYS.persists
    assert Decision.DENY_ONCE.denies and not Decision.DENY_ONCE.persists
    assert Decision.DENY_ALWAYS.denies and Decision.DENY_ALWAYS.persists
    assert Decision.from_value("allow_once") is Decision.ALLOW_ONCE
    with pytest.raises(PermissionRuleError):
        Decision.from_value("maybe")


def test_grant_for_decision_only_persists_always():
    assert grant_for_decision(Decision.ALLOW_ONCE, rule="Bash") is None
    assert grant_for_decision(Decision.DENY_ONCE, rule="Bash") is None
    allow = grant_for_decision(Decision.ALLOW_ALWAYS, rule="Read(**)")
    deny = grant_for_decision(Decision.DENY_ALWAYS, rule="Bundle:fs")
    assert allow is not None and allow.effect == "allow"
    assert deny is not None and deny.effect == "deny"
    assert allow.matches("Read", "/x", "fs")
    assert deny.matches("Write", "/x", "fs")


def test_grant_round_trips_through_dict():
    grant = Grant(effect="allow", rule="Bash(git status)", scope="session")
    restored = Grant.from_dict(grant.to_dict())
    assert restored == grant
    assert hash(restored) == hash(grant)
    payload = json.dumps(restored.to_dict())
    assert "git status" in payload


def test_grants_reconstructable_from_resolved_records():
    records = [
        # A ``tool``-only record has no explicit rule and no key: it must NOT be
        # widened into a bare whole-tool grant on replay.
        {"decision": "allow_always", "tool": "Bash", "scope": "session"},
        {"decision": "deny_always", "tool": "Write", "scope": "session"},
        {"decision": "allow_once", "tool": "Read", "scope": "session"},
        # A key with no rule reconstructs as the bounded exact rule.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": "git status",
            "scope": "session",
        },
        {
            "decision": "allow_always",
            "tool": "Read",
            "grant": {"effect": "allow", "rule": "Read(**)", "scope": "session"},
        },
    ]
    grants = collect_grants(records)
    assert [grant.rule for grant in grants] == ['bash("git status")', "Read(**)"]
    assert [grant.effect for grant in grants] == ["allow", "allow"]


@pytest.mark.parametrize(
    "legacy,canonical",
    [("LS", "ls"), ("MultiEdit", "multiedit")],
)
def test_replayed_keyed_grants_migrate_legacy_exact_rules(legacy, canonical):
    key = "one exact action"
    grants = collect_grants(
        [
            {
                "decision": "allow_always",
                "tool": legacy,
                "key": key,
                "rule": f'{legacy}("{key}")',
                "scope": "session",
            },
            {
                "decision": "deny_always",
                "tool": legacy,
                "key": key,
                "scope": "session",
            },
        ]
    )
    assert len(grants) == 2
    assert all(grant.matches(canonical, key, "fs") for grant in grants)
    assert not any(
        grant.matches(canonical, "another action", "fs") for grant in grants
    )


# ---------------------------------------------------------------------------
# Unattended policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy,expected,code",
    [
        ("deny", Outcome.DENY, "unattended_deny"),
        ("allow", Outcome.ALLOW, "unattended_allow"),
        ("fail_turn", Outcome.FAIL_TURN, "unattended_fail_turn"),
    ],
)
def test_unattended_policy_applies_only_to_ask(policy, expected, code):
    engine = _engine(mode="ask", on_unattended=policy)
    attended = engine.evaluate(call("Bash", command="ls"), bash_spec(), attended=True)
    assert attended.outcome is Outcome.ASK
    unattended = engine.evaluate(call("Bash", command="ls"), bash_spec(), attended=False)
    assert unattended.outcome is expected
    assert unattended.code == code


def test_unattended_does_not_touch_allowed_or_denied():
    engine = _engine(mode="ask", allow=["Read(**)"], on_unattended="allow")
    assert (
        engine.evaluate(call("Read", path="/x"), read_spec(), attended=False).outcome
        is Outcome.ALLOW
    )


def test_engine_rejects_bad_configuration():
    with pytest.raises(PermissionRuleError):
        _engine(mode="sometimes")
    with pytest.raises(PermissionRuleError):
        _engine(on_unattended="maybe")


# ---------------------------------------------------------------------------
# Batch planning is pure and total
# ---------------------------------------------------------------------------


def test_batch_plan_evaluates_every_call_and_executes_nothing():
    engine = _engine(mode="ask", allow=["Read(**)"], deny=["Bash"])
    specs = {"Read": read_spec(), "Bash": bash_spec(), "Glob": None}
    calls = [
        call("Bash", "c1", command="rm -rf /"),
        call("Nope", "c2"),
        call("Read", "c3", path="/x"),
    ]
    plan = engine.plan(calls, {k: v for k, v in specs.items() if v is not None})
    assert len(plan.evaluations) == 3
    assert plan.for_call("c1").outcome is Outcome.DENY
    assert plan.for_call("c2").outcome is Outcome.DENY  # missing spec -> unknown
    assert plan.for_call("c3").outcome is Outcome.ALLOW
    assert plan.executable is False
    # Planning returns data only; nothing resembling execution is reachable.
    assert not any(hasattr(item, "result") for item in plan.evaluations)


def test_batch_plan_executable_when_all_allowed():
    plan = _engine(mode="ask", allow=["Read(**)", "Bash(**)"]).plan(
        [call("Read", "c1", path="/x"), call("Bash", "c2", command="ls")],
        {"Read": read_spec(), "Bash": bash_spec()},
    )
    assert plan.executable is True
    assert len(plan.allows()) == 2


def test_batch_plan_prepares_allowed_calls_for_the_dispatcher():
    plan = _engine(mode="ask", allow=["Read(**)"], ask=["Bash"]).plan(
        [call("Read", "c1", path="/x"), call("Bash", "c2", command="ls")],
        {"Read": read_spec(), "Bash": bash_spec()},
    )
    prepared = plan.prepared()
    assert len(prepared) == 1
    assert prepared[0].call.id == "c1"
    assert prepared[0].spec.name == "Read"
    assert prepared[0].decision is Decision.ALLOW_ONCE
    assert json.dumps(prepared[0].to_dict())


def test_multi_target_plan_aggregates_allow_and_ask_and_audits_each_path(
    tmp_path,
):
    allow_path = str(tmp_path / "allowed.txt")
    ask_path = str(tmp_path / "review.txt")
    input_data = {
        "targets": (("source", allow_path), ("destination", ask_path)),
    }
    before = {"targets": input_data["targets"]}
    plan = _engine(
        mode="deny",
        allow=[exact_rule("Multi", allow_path)],
        ask=[exact_rule("Multi", ask_path)],
        workspace=tmp_path,
    ).plan(
        [ToolCall(id="multi", name="Multi", input=input_data)],
        {"Multi": multi_path_spec()},
    )

    evaluation = plan.evaluations[0]
    assert evaluation.outcome is Outcome.ASK
    assert [target.outcome for target in evaluation.target_evaluations] == [
        Outcome.ALLOW,
        Outcome.ASK,
    ]
    assert [target.key for target in evaluation.target_evaluations] == [
        allow_path,
        ask_path,
    ]
    assert [target.role for target in evaluation.target_evaluations] == [
        "source",
        "destination",
    ]
    assert evaluation.to_dict()["targets"][1]["code"] == "ask"
    assert input_data == before


def test_multi_target_plan_denies_if_any_path_denied_and_deny_beats_grant(
    tmp_path,
):
    allowed_path = str(tmp_path / "allowed.txt")
    denied_path = str(tmp_path / "denied.txt")
    engine = _engine(
        mode="allow",
        deny=[exact_rule("Multi", denied_path)],
        allow=[exact_rule("Multi", allowed_path), "Multi(**)"],
        workspace=tmp_path,
    )
    plan = engine.plan(
        [
            call(
                "Multi",
                targets=(("source", allowed_path), ("destination", denied_path)),
            )
        ],
        {"Multi": multi_path_spec()},
        grants=[Grant(effect="allow", rule="Multi")],
    )

    evaluation = plan.evaluations[0]
    assert evaluation.outcome is Outcome.DENY
    assert [item.outcome for item in evaluation.target_evaluations] == [
        Outcome.ALLOW,
        Outcome.DENY,
    ]
    assert evaluation.target_evaluations[1].code == "deny"
    assert evaluation.target_evaluations[1].rule.raw == exact_rule(
        "Multi", denied_path
    )


def test_multi_target_plan_uses_first_matching_rule_per_path(tmp_path):
    target = str(tmp_path / "one.txt")
    later_target = str(tmp_path / "two.txt")
    first = exact_rule("Multi", target)
    second = "Multi(**)"
    evaluation = _engine(
        mode="deny",
        allow=[first, second],
        workspace=tmp_path,
    ).plan(
        [
            call(
                "Multi",
                targets=(("source", target), ("destination", later_target)),
            )
        ],
        {"Multi": multi_path_spec()},
    ).evaluations[0]

    assert evaluation.outcome is Outcome.ALLOW
    assert evaluation.target_evaluations[0].rule.raw == first
    assert evaluation.target_evaluations[1].rule.raw == second


@pytest.mark.parametrize(
    ("mode", "attended", "on_unattended", "expected", "code"),
    [
        ("deny", True, "allow", Outcome.DENY, "mode_deny"),
        ("deny", False, "allow", Outcome.DENY, "mode_deny"),
        ("allow", False, "deny", Outcome.ALLOW, "mode_allow"),
        ("ask", True, "deny", Outcome.ASK, "ask"),
        ("ask", False, "allow", Outcome.ALLOW, "unattended_allow"),
    ],
)
def test_multi_target_mode_fallback_matches_scalar_evaluation(
    tmp_path, mode, attended, on_unattended, expected, code
):
    path = str(tmp_path / "target.txt")
    evaluation = _engine(
        mode=mode,
        on_unattended=on_unattended,
        workspace=tmp_path,
    ).plan(
        [call("Multi", targets=(("target", path),))],
        {"Multi": multi_path_spec()},
        attended=attended,
    ).evaluations[0]

    target = evaluation.target_evaluations[0]
    assert target.outcome is expected
    assert target.code == code
    assert evaluation.outcome is expected


@pytest.mark.parametrize(
    ("mode", "attended", "on_unattended", "expected", "code"),
    [
        ("deny", True, "allow", Outcome.ASK, "ask"),
        ("allow", False, "deny", Outcome.DENY, "unattended_deny"),
    ],
)
def test_multi_target_explicit_ask_rule_uses_unattended_policy(
    tmp_path, mode, attended, on_unattended, expected, code
):
    path = str(tmp_path / "target.txt")
    evaluation = _engine(
        mode=mode,
        ask=["Multi(**)"],
        on_unattended=on_unattended,
        workspace=tmp_path,
    ).plan(
        [call("Multi", targets=(("target", path),))],
        {"Multi": multi_path_spec()},
        attended=attended,
    ).evaluations[0]

    target = evaluation.target_evaluations[0]
    assert target.outcome is expected
    assert target.code == code
    assert target.rule.raw == "Multi(**)"
    assert evaluation.outcome is expected


def test_multi_target_plan_hard_boundaries_precede_scalar_allow_and_grant(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    secret = workspace / "secret"
    secret.mkdir()
    outside_path = str(outside)
    secret_path = str(secret / "key")
    evaluation = _engine(
        mode="allow",
        allow=["Multi"],
        workspace=workspace,
        write_roots=["./"],
        read_denyroots=[str(secret)],
    ).plan(
        [
            call(
                "Multi",
                targets=(("source", outside_path), ("destination", secret_path)),
            )
        ],
        {"Multi": multi_path_spec()},
        grants=[Grant(effect="allow", rule="Multi")],
    ).evaluations[0]

    assert evaluation.outcome is Outcome.DENY
    assert [item.code for item in evaluation.target_evaluations] == [
        "write_root",
        "read_deny",
    ]


def test_multi_target_request_aggregates_every_unresolved_path(tmp_path):
    source = str(tmp_path / "source.txt")
    first = str(tmp_path / "first.txt")
    second = str(tmp_path / "second.txt")
    evaluation = _engine(mode="ask", workspace=tmp_path).plan(
        [
            call(
                "Multi",
                targets=(
                    ("source", source),
                    ("destination", first),
                    ("backup", second),
                ),
            )
        ],
        {"Multi": multi_path_spec()},
    ).evaluations[0]
    request = _engine(mode="ask", workspace=tmp_path).request_for(evaluation)

    assert request.key is None
    assert [item.path for item in request.targets] == [source, first, second]
    assert [item.role for item in request.targets] == [
        "source",
        "destination",
        "backup",
    ]
    assert all(item.suggested_rule for item in request.targets)
    assert all(path in request.preview for path in (source, first, second))
    assert request.persistence_available is True
    assert "Multi(" not in request.default_rule
    assert "Multi" not in request.suggestions


async def test_multi_target_deny_once_creates_no_grants(tmp_path):
    paths = (str(tmp_path / "first.txt"), str(tmp_path / "second.txt"))
    evaluation = _engine(mode="ask", workspace=tmp_path).plan(
        [call("Multi", targets=tuple(("target", path) for path in paths))],
        {"Multi": multi_path_spec()},
    ).evaluations[0]
    request = _engine(mode="ask", workspace=tmp_path).request_for(evaluation)
    broker = ApprovalBroker()
    future = broker.request(request)

    assert broker.resolve(request.id, Decision.DENY_ONCE)
    assert await future is Decision.DENY_ONCE
    assert broker.grants() == ()


@pytest.mark.parametrize(
    ("decision", "effect"),
    [
        (Decision.ALLOW_ALWAYS, "allow"),
        (Decision.DENY_ALWAYS, "deny"),
    ],
)
async def test_multi_target_persistence_is_exact_and_replayable(
    tmp_path, decision, effect
):
    paths = (str(tmp_path / "first.txt"), str(tmp_path / "second.txt"))
    evaluation = _engine(mode="ask", workspace=tmp_path).plan(
        [call("Multi", targets=tuple(("target", path) for path in paths))],
        {"Multi": multi_path_spec()},
    ).evaluations[0]
    request = _engine(mode="ask", workspace=tmp_path).request_for(evaluation)
    broker = ApprovalBroker()
    future = broker.request(request)

    assert broker.resolve(request.id, decision)
    assert await future is decision
    assert len(broker.grants()) == len(paths)
    assert all(grant.effect == effect for grant in broker.grants())
    assert [grant.rule for grant in broker.grants()] == [
        exact_rule("Multi", path) for path in paths
    ]
    replayed = collect_grants(broker.records)
    assert replayed == broker.grants()
    assert all(
        grant.matches("Multi", path, "fs")
        for grant, path in zip(replayed, paths)
    )
    assert not any(
        grant.matches("Multi", str(tmp_path / "other.txt"), "fs")
        for grant in replayed
    )


async def test_multi_target_unrepresentable_path_degrades_persistence_atomically():
    paths = ("/tmp/one.txt", "x" * 9000)
    request = PermissionRequest(
        id="multi-long",
        call_id="multi",
        tool="Multi",
        targets=tuple(
            PermissionTargetRequest(
                role="target",
                path=path,
                reason="approval required",
                suggested_rule=(
                    exact_rule("Multi", path) if len(path) <= 8192 else ""
                ),
            )
            for path in paths
        ),
        persistence_available=False,
    )
    broker = ApprovalBroker()
    future = broker.request(request)

    assert broker.resolve(request.id, Decision.ALLOW_ALWAYS)
    assert future.result() is Decision.ALLOW_ONCE
    assert broker.grants() == ()
    assert "target_grants" not in broker.records[0]


# ---------------------------------------------------------------------------
# Hard path boundaries run before rule allow
# ---------------------------------------------------------------------------


def test_read_deny_root_wins_over_allow_rule(tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    guard_workspace = tmp_path / "ws"
    guard_workspace.mkdir()
    engine = _engine(
        mode="allow",
        allow=["Read(**)"],
        workspace=guard_workspace,
        read_denyroots=[str(secret)],
    )
    target = secret / "id_rsa"
    target.write_text("secret")
    evaluation = engine.evaluate(call("Read", path=str(target)), read_spec())
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "read_deny"


@pytest.mark.parametrize(
    "target_name", ["parent/notes.txt", "child/report.pdf"]
)
async def test_worktree_read_denyroot_blocks_text_and_document_reads(
    tmp_path, target_name
):
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    parent_secret = parent / "notes.txt"
    child_secret = child / "report.pdf"
    parent_secret.write_text("parent secret")
    child_secret.write_bytes(b"document")
    context = ToolContext(
        workspace=child,
        session_id="s1",
        turn_id="t1",
        config=Config(
            v2=ConfigV2(
                permissions=PermissionsSection(
                    mode="allow",
                    read_denyroots=[str(parent_secret), "report.pdf"],
                )
            )
        ),
    )

    result = await read_tool.run({"path": str(tmp_path / target_name)}, context)

    assert result.is_error is True
    assert "read-deny root" in result.content[0].text


def test_write_root_wins_over_allow_rule(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    engine = _engine(
        mode="allow",
        allow=["Write(**)"],
        workspace=workspace,
        write_roots=["./"],
    )
    spec = ToolSpec(
        name="Write",
        description="Write a file",
        input_schema={"type": "object"},
        bundle="fs",
        mutates=True,
        permission_key=lambda data: str(data["path"]),
    )
    evaluation = engine.evaluate(call("Write", path=str(outside / "x")), spec)
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "write_root"


def test_worktree_guard_preserves_absolute_permission_denies_without_rewriting_rules(
    tmp_path,
):
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    denied_path = str(child / "blocked.txt")
    parent_scoped_grant = Grant(
        effect="allow", rule=exact_rule("Write", str(parent / "allowed.txt"))
    )
    spec = ToolSpec(
        name="Write",
        description="Write a file",
        input_schema={"type": "object"},
        bundle="fs",
        mutates=True,
        permission_key=lambda data: str(data["path"]),
    )
    engine = PermissionEngine(
        mode="allow",
        allow=["Write(**)"],
        deny=[exact_rule("Write", denied_path)],
        path_guard=PathGuard(parent).for_worktree(child),
    )

    denied = engine.evaluate(call("Write", path=denied_path), spec)
    assert denied.outcome is Outcome.DENY
    assert denied.code == "deny"
    assert denied.rule.raw == exact_rule("Write", denied_path)

    # Absolute grants for the old checkout remain exact; they are not rebased
    # into grants for a matching child-relative path.
    mode_denied_engine = PermissionEngine(
        mode="deny", path_guard=PathGuard(parent).for_worktree(child)
    )
    not_rebased = mode_denied_engine.evaluate(
        call("Write", path=str(child / "allowed.txt")),
        spec,
        grants=[parent_scoped_grant],
    )
    assert not_rebased.outcome is Outcome.DENY
    assert not_rebased.code == "mode_deny"


def test_shell_write_roots_do_not_apply(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    engine = _engine(
        mode="allow",
        allow=["Bash(**)"],
        workspace=workspace,
        write_roots=["./"],
    )
    evaluation = engine.evaluate(call("Bash", command="rm -rf /"), bash_spec())
    assert evaluation.outcome is Outcome.ALLOW


def test_path_guard_error_code_is_surfaced(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    spec = ToolSpec(
        name="Write",
        description="Write",
        input_schema={"type": "object"},
        bundle="fs",
        mutates=True,
        permission_key=lambda data: str(data["path"]),
    )
    engine = PermissionEngine(
        mode="allow",
        allow=["Write(**)"],
        path_guard=PathGuard(workspace),
    )
    evaluation = engine.evaluate(call("Write", path=str(outside / "x")), spec)
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "write_root"


def test_path_mode_tool_boundary_is_checked(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = ToolSpec(
        name="WriteTool",
        description="Write an extension tool",
        input_schema={"type": "object"},
        bundle="meta",
        mutates=True,
        path_mode=True,
        permission_key=lambda data: f".nexus/tools/{data['filename']}",
    )
    engine = PermissionEngine(
        mode="allow",
        allow=["WriteTool(**)"],
        workspace=workspace,
        write_roots=["./src"],
    )
    evaluation = engine.evaluate(
        ToolCall(
            id="c",
            name="WriteTool",
            input={"filename": "x.py", "content": "c"},
        ),
        spec,
    )
    # The relative path-mode key is resolved through the guard, so the fs
    # write-root boundary denies even though a whole-tool allow rule matches.
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "write_root"


def test_malformed_permission_key_is_denied_before_rules():
    spec = ToolSpec(
        name="Read",
        description="Read",
        input_schema={"type": "object"},
        bundle="fs",
        permission_key=lambda data: "bad\x00path",
    )
    engine = PermissionEngine(mode="allow", allow=["Read(**)"])
    evaluation = engine.evaluate(call("Read", path="x"), spec)
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "permission_key_error"


# ---------------------------------------------------------------------------
# Approval primitives
# ---------------------------------------------------------------------------


def _request(**overrides) -> PermissionRequest:
    payload = {
        "id": "req-1",
        "call_id": "call-1",
        "tool": "Bash",
        "key": "git status",
        "bundle": "shell",
        "preview": "git status",
        "suggestions": ('Bash("git status")', "Bash", "Bundle:shell"),
        "default_rule": 'Bash("git status")',
    }
    payload.update(overrides)
    return PermissionRequest(**payload)


async def test_approval_broker_resolves_and_records_grant():
    broker = ApprovalBroker(clock=lambda: 123.0)
    future = broker.request(_request())
    assert [request.id for request in broker.pending] == ["req-1"]
    assert broker.resolve("req-1", Decision.ALLOW_ALWAYS) is True
    assert await future is Decision.ALLOW_ALWAYS
    assert broker.pending == ()
    record = broker.records[0]
    assert record["decision"] == "allow_always"
    assert record["grant"] == {
        "effect": "allow",
        "rule": 'Bash("git status")',
        "scope": "session",
    }
    assert json.dumps(record)  # serialization-safe
    grants = broker.grants()
    assert grants[0].matches("Bash", "git status", "shell")
    assert not grants[0].matches("Bash", "rm -rf /", "shell")
    assert broker.resolve("req-1", Decision.DENY_ONCE) is False


async def test_approval_broker_degrades_bare_keyed_rule_to_once():
    broker = ApprovalBroker()
    # A hand-built keyed request whose rule is the bare tool must not persist it.
    future = broker.request(_request(default_rule="Bash"))
    assert broker.resolve("req-1", Decision.ALLOW_ALWAYS) is True
    assert await future is Decision.ALLOW_ONCE  # degraded
    assert broker.records[0]["decision"] == "allow_once"
    assert "grant" not in broker.records[0]
    assert broker.grants() == ()


async def test_approval_broker_honours_explicit_exact_rule():
    broker = ApprovalBroker()
    future = broker.request(_request())
    exact = 'Bash("git status")'
    assert broker.resolve("req-1", Decision.ALLOW_ALWAYS, rule=exact)
    assert await future is Decision.ALLOW_ALWAYS
    assert broker.records[0]["grant"]["rule"] == exact


async def test_approval_broker_rejects_explicit_glob_for_keyed_request():
    broker = ApprovalBroker()
    future = broker.request(_request())
    # A glob rule for a keyed request would broaden; it degrades to once.
    assert broker.resolve("req-1", Decision.ALLOW_ALWAYS, rule="Bash(git*)")
    assert await future is Decision.ALLOW_ONCE
    assert "grant" not in broker.records[0]
    assert broker.grants() == ()


async def test_approval_broker_once_does_not_grant():
    broker = ApprovalBroker()
    future = broker.request(_request())
    assert broker.resolve("req-1", Decision.ALLOW_ONCE)
    assert await future is Decision.ALLOW_ONCE
    assert "grant" not in broker.records[0]
    assert broker.grants() == ()


async def test_approval_broker_cancel():
    broker = ApprovalBroker()
    future = broker.request(_request())
    assert broker.cancel("req-1") is True
    with pytest.raises(asyncio.CancelledError):
        await future
    assert broker.pending == ()
    assert broker.cancel("req-1") is False


def test_engine_request_for_builds_ui_payload():
    engine = _engine(mode="ask")
    evaluation = engine.evaluate(call("Bash", path=None, command="git status"), bash_spec())
    request = engine.request_for(evaluation)
    assert request.tool == "Bash"
    assert request.key == "git status"
    assert request.bundle == "shell"
    # The persisted scope is the most specific exact-action rule, shown to the UI.
    assert request.default_rule == 'bash("git status")'
    # A keyed request never suggests whole-tool scope.
    assert request.suggestions == ('bash("git status")',)
    assert "targets" not in request.to_dict()


def test_engine_request_preview_is_bounded():
    engine = _engine(mode="ask")
    long_command = "x" * 500
    evaluation = engine.evaluate(call("Bash", path=None, command=long_command), bash_spec())
    request = engine.request_for(evaluation)
    assert request.key == long_command
    assert request.preview == long_command[:200]


def test_engine_request_for_without_key_uses_whole_tool():
    engine = _engine(mode="ask")
    spec = bash_spec(permission_key=False)
    evaluation = engine.evaluate(call("Bash", command="git status"), spec)
    request = engine.request_for(evaluation)
    assert request.key is None
    assert request.default_rule == "bash"


def test_path_security_error_is_a_nexus_error():
    from nexus.errors import NexusError

    assert issubclass(PathSecurityError, NexusError)
    assert issubclass(PermissionRuleError, NexusError)
