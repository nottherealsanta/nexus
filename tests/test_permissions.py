"""Table-driven, adversarial tests for the permission engine (plan section 5.3)."""
from __future__ import annotations

import asyncio
import json

import pytest

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
    collect_grants,
    grant_for_decision,
    parse_rule,
)
from nexus.tools.spec import ToolCall, ToolSpec

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
    assert [grant.rule for grant in grants] == ['Bash("git status")', "Read(**)"]
    assert [grant.effect for grant in grants] == ["allow", "allow"]


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
    assert request.default_rule == 'Bash("git status")'
    # A keyed request never suggests whole-tool scope.
    assert request.suggestions == ('Bash("git status")',)


def test_engine_request_for_without_key_uses_whole_tool():
    engine = _engine(mode="ask")
    spec = bash_spec(permission_key=False)
    evaluation = engine.evaluate(call("Bash", command="git status"), spec)
    request = engine.request_for(evaluation)
    assert request.key is None
    assert request.default_rule == "Bash"


def test_path_security_error_is_a_nexus_error():
    from nexus.errors import NexusError

    assert issubclass(PathSecurityError, NexusError)
    assert issubclass(PermissionRuleError, NexusError)
