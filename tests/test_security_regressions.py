"""Focused regressions for the Phase 2 security-review blockers.

Each test pins one mandatory fix so it cannot regress silently:

* duplicate tool-call ids are refused before any permission decision or dispatch;
* the Bash environment overlay preserves the inherited environment;
* fs execution re-canonicalizes and compares the permission-planned key;
* atomic writes refuse a swapped/symlinked parent;
* ``*_ALWAYS`` grants are exact-action rules that cannot broaden;
* Grep's regex scan is bounded and runs in a killable worker;
* a symlink loop is a path-security failure;
* ``~`` in a permission rule pattern is rejected rather than silently inert;
* the job registry evicts old completed jobs while preserving active ones;
* Runtime only closes tool managers it explicitly owns.
"""
from __future__ import annotations

import asyncio
import io
import re
import time
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.errors import MalformedToolCall, ToolError
from nexus.model.message import (
    DUPLICATE_TOOL_CALL_KEY,
    Message,
    Text,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.model.stream import ToolCallAccumulator
from nexus.runtime import Runtime
from nexus.tools import permissions
from nexus.tools.builtin import OPT_IN_TOOLS, _jobs, grep, ls, write
from nexus.tools.manager import ToolManager
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
    escape_glob,
    exact_rule,
    grant_for_decision,
    parse_rule,
)
from nexus.tools.spec import (
    PathTarget,
    RegisteredTool,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)

SCRIPTED = "scripted/m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    mode: str = "allow",
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    profile: str = "coding",
    unattended: str = "deny",
    grep_timeout_s: float = 5.0,
) -> Config:
    return Config(
        model=SCRIPTED,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=SCRIPTED),
            agent=AgentSection(profile=profile),
            permissions=PermissionsSection(
                mode=mode,
                on_unattended=unattended,
                allow=allow or [],
                deny=deny or [],
            ),
            tools=ToolsSection(grep_timeout_s=grep_timeout_s),
        ),
    )


def bash_spec(*, permission_key: bool = True) -> ToolSpec:
    return ToolSpec(
        name="Bash",
        description="Run a command",
        input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
        bundle="shell",
        mutates=True,
        permission_key=(lambda data: str(data["command"])) if permission_key else None,
    )


def make_ctx(workspace: Path, *, config: Config | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="s",
        turn_id="t",
        config=config or make_config(),
    )


def ctx_factory(workspace: Path):
    def make(call, spec):
        return make_ctx(workspace)

    return make


async def _drain(iterator):
    return [event async for event in iterator]


def _anthropic_wire_ids(messages):
    """Translate history and return ``(tool_use_ids, tool_result_ids)``.

    Asserts every wire tool_use id is provider-safe; callers assert uniqueness
    and one-to-one pairing.
    """
    from nexus.model.providers.anthropic import _messages_to_wire

    use_ids: list[str] = []
    result_ids: list[str] = []
    for message in _messages_to_wire(messages):
        for block in message["content"]:
            if block["type"] == "tool_use":
                assert re.fullmatch(r"[A-Za-z0-9_-]+", block["id"])
                use_ids.append(block["id"])
            elif block["type"] == "tool_result":
                result_ids.append(block["tool_use_id"])
    return use_ids, result_ids


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws.resolve()


# ---------------------------------------------------------------------------
# 1. Duplicate tool-call ids
# ---------------------------------------------------------------------------


def test_accumulator_duplicate_start_is_typed_error():
    acc = ToolCallAccumulator()
    acc.start("c1", "Bash")
    with pytest.raises(MalformedToolCall):
        acc.start("c1", "Read")

    # An id already finished in the same message is also a duplicate.
    finished = ToolCallAccumulator()
    finished.start("c1", "Bash")
    finished.finish("c1", {})
    with pytest.raises(MalformedToolCall):
        finished.start("c1", "Bash")


def test_duplicate_id_group_rejected_before_permission(workspace: Path):
    manager = ToolManager(
        make_config(mode="allow", deny=["bash(echo*)"]), workspace=workspace
    )
    batch = manager.prepare(
        [
            ToolCall(id="dup", name="Bash", input={"command": "echo hi > pwned"}),
            ToolCall(id="dup", name="Read", input={"path": "ok.txt"}),
        ]
    )
    assert batch.executable == ()
    assert batch.calls() == ()
    assert all(
        entry.error is not None and entry.code == "duplicate_tool_call_id"
        for entry in batch.entries
    )
    # The absolute deny still holds for the Bash call evaluated on its own.
    engine = PermissionEngine(
        mode="allow", deny=["bash(echo*)"], path_guard=manager.path_guard
    )
    evaluation = engine.evaluate(
        ToolCall(id="x", name="Bash", input={"command": "echo hi > pwned"}),
        manager.get("bash").spec,
    )
    assert evaluation.outcome is Outcome.DENY


async def test_duplicate_ids_never_execute_and_results_are_ordered(tmp_path: Path):
    provider = ScriptedProvider(
        tool_response(
            ("dup", "Bash", {"command": "echo hi > pwned.txt"}),
            ("dup", "Read", {"path": "missing.txt"}),
        ),
        text_response("done"),
    )
    runtime = Runtime(
        tmp_path,
        config=make_config(mode="ask", deny=["Bash(echo*)"]),
        providers={"scripted": provider},
    )
    session = runtime.session("dup")
    events = [event async for event in session.send("go")]

    assert not (tmp_path / "pwned.txt").exists()
    assert "tool.started" not in [event.type for event in events]

    # Persisted assistant ids are unique; the first call keeps its original id.
    assistant = next(m for m in session.messages if m.role == "assistant")
    uses = [b for b in assistant.content if isinstance(b, ToolUse)]
    use_ids = [b.id for b in uses]
    assert use_ids == ["dup", "dup_dup1"]
    assert len(set(use_ids)) == len(use_ids)
    # The later duplicate is marked with the id it duplicated, so it is
    # self-describing on replay.
    assert uses[1].input[DUPLICATE_TOOL_CALL_KEY] == "dup"

    result_messages = [
        message
        for message in session.messages
        if message.role == "user"
        and message.content
        and hasattr(message.content[0], "tool_use_id")
    ]
    assert len(result_messages) == 1
    blocks = result_messages[0].content
    # Exactly one result per persisted ToolUse, same ids and order.
    assert [block.tool_use_id for block in blocks] == use_ids
    assert all(block.is_error for block in blocks)
    # The first (denied) Bash remains denied; the later duplicate is refused as
    # a duplicate and can never execute.
    assert "Denied by rule" in blocks[0].content[0].text
    assert "duplicate" in blocks[1].content[0].text

    # The persisted history translates to a valid Anthropic wire payload:
    # unique ids, one result per call, same order.
    wire_use_ids, wire_result_ids = _anthropic_wire_ids(session.messages)
    assert wire_use_ids == use_ids
    assert wire_result_ids == use_ids
    await runtime.aclose()


async def test_duplicate_ids_phase1_fallback_persists_unique_pairs(tmp_path: Path):
    provider = ScriptedProvider(
        tool_response(
            ("dup", "Bash", {"command": "echo hi > pwned.txt"}),
            ("dup", "Read", {"path": "missing.txt"}),
        ),
        text_response("done"),
    )
    # ``tool_factory`` returning ``None`` removes the dispatcher/gate: the Phase 1
    # fallback path must normalize ids too.
    runtime = Runtime(
        tmp_path,
        config=make_config(mode="allow"),
        providers={"scripted": provider},
        tool_factory=lambda **_: None,
    )
    session = runtime.session("dup-phase1")
    await _drain(session.send("go"))

    assert not (tmp_path / "pwned.txt").exists()
    assistant = next(m for m in session.messages if m.role == "assistant")
    uses = [b for b in assistant.content if isinstance(b, ToolUse)]
    assert [b.id for b in uses] == ["dup", "dup_dup1"]
    result_messages = [
        m
        for m in session.messages
        if m.role == "user" and m.content and hasattr(m.content[0], "tool_use_id")
    ]
    assert len(result_messages) == 1
    blocks = result_messages[0].content
    assert [b.tool_use_id for b in blocks] == [b.id for b in uses]
    assert all(b.is_error for b in blocks)
    assert "malformed" in blocks[1].content[0].text or "duplicate" in blocks[1].content[0].text
    await runtime.aclose()


def test_anthropic_wire_has_unique_tool_ids_and_valid_pairing():
    messages = [
        Message(role="user", content=[Text(text="go")]),
        Message(
            role="assistant",
            content=[
                ToolUse(id="dup", name="Bash", input={"command": "echo hi"}),
                ToolUse(
                    id="dup_dup1",
                    name="Read",
                    input={DUPLICATE_TOOL_CALL_KEY: "dup", "path": "x"},
                ),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResult(
                    tool_use_id="dup", content=[Text(text="denied")], is_error=True
                ),
                ToolResult(
                    tool_use_id="dup_dup1",
                    content=[Text(text="duplicate")],
                    is_error=True,
                ),
            ],
        ),
    ]
    use_ids, result_ids = _anthropic_wire_ids(messages)
    assert len(set(use_ids)) == len(use_ids)
    # Same ids, same order, one result per call.
    assert result_ids == use_ids
    assert use_ids == ["dup", "dup_dup1"]


def test_apply_plan_and_decisions_do_not_collapse_duplicate_ids():
    manager = ToolManager(make_config(mode="allow"), workspace=Path.cwd())
    batch = manager.prepare(
        [
            ToolCall(id="x", name="Read", input={"path": "a"}),
            ToolCall(id="x", name="Read", input={"path": "b"}),
        ]
    )
    # Duplicates are rejected at prepare, so nothing is executable to decide.
    assert batch.executable == ()
    gated = batch.with_decisions([("x", Decision.ALLOW_ONCE), ("x", Decision.DENY_ONCE)])
    assert gated.executable == ()


# ---------------------------------------------------------------------------
# 2. Bash environment overlay
# ---------------------------------------------------------------------------


async def test_bash_env_overlay_preserves_inherited_environment(workspace: Path):
    from nexus.tools.builtin import bash

    registry = _jobs.JobRegistry()
    _jobs.set_default_registry(registry)
    try:
        result = await bash.run(
            {
                "command": (
                    'printf "%s|" "$NEXUS_SEC_TEST"; '
                    'printf "%s" "$(command -v ls)"; ls -d / >/dev/null && printf ok'
                ),
                "env": {"NEXUS_SEC_TEST": "override"},
            },
            make_ctx(workspace),
        )
    finally:
        await registry.aclose()
        _jobs.set_default_registry(None)

    text = result.content[0].text
    assert "override|" in text
    assert "ok" in text  # external `ls` resolved through the inherited PATH
    assert "ls" in text


# ---------------------------------------------------------------------------
# 3. Execution-time path authorization
# ---------------------------------------------------------------------------


async def test_execution_recheck_refuses_swapped_path(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "a" / "sub").mkdir(parents=True)
    (ws / "a" / "sub" / "f.txt").write_text("INSIDE", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("OUTSIDE", encoding="utf-8")

    manager = ToolManager(make_config(mode="allow"), workspace=ws)
    batch = manager.prepare(
        [ToolCall(id="c", name="Read", input={"path": "a/sub/f.txt"})]
    )
    entry = batch.for_call("c")
    assert entry.key == str((ws / "a" / "sub" / "f.txt").resolve())

    # Swap the canonical parent for a symlink to a directory outside the ws.
    (ws / "a" / "sub").rename(ws / "a" / "sub-away")
    (ws / "a" / "sub").symlink_to(outside, target_is_directory=True)

    events: list = []
    results = await manager.dispatch(
        batch.with_decisions({"c": Decision.ALLOW_ONCE}),
        ctx_factory(ws),
        emit=events.append,
    )
    assert results[0].is_error is True
    assert "path changed" in results[0].content[0].text
    assert [event.data.get("code") for event in events] == ["path_changed"]
    assert not any(event.type == "tool.started" for event in events)


async def test_write_recheck_refuses_swapped_path(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "a" / "sub").mkdir(parents=True)
    manager = ToolManager(make_config(mode="allow"), workspace=ws)
    batch = manager.prepare(
        [ToolCall(id="c", name="Write", input={"path": "a/sub/out.txt", "content": "x"})]
    )
    (ws / "a" / "sub").rename(ws / "a" / "sub-away")
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "a" / "sub").symlink_to(outside, target_is_directory=True)

    results = await manager.dispatch(
        batch.with_decisions({"c": Decision.ALLOW_ONCE}), ctx_factory(ws)
    )
    assert results[0].is_error is True
    assert list(outside.iterdir()) == []


def test_legacy_multiedit_canonical_deny_blocks_symlink_alias(workspace: Path):
    secret = workspace / "secret.txt"
    secret.write_text("before", encoding="utf-8")
    (workspace / "alias.txt").symlink_to(secret)
    manager = ToolManager(
        make_config(
            mode="allow",
            allow=["multiedit"],
            deny=[f"multiedit({secret.resolve()})"],
        ),
        workspace=workspace,
        tools=OPT_IN_TOOLS,
        tool_names=["multiedit"],
    )
    entry = manager.prepare(
        [
            ToolCall(
                id="edit",
                name="multiedit",
                input={
                    "path": "alias.txt",
                    "edits": [{"old_string": "before", "new_string": "after"}],
                },
            )
        ]
    ).for_call("edit")

    assert entry is not None and entry.error is None
    assert entry.key == str(secret.resolve())
    engine = PermissionEngine(
        mode="allow",
        allow=["multiedit"],
        deny=[f"multiedit({secret.resolve()})"],
        path_guard=manager.path_guard,
    )
    evaluation = engine.evaluate(entry.call, entry.spec)
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "deny"
    assert secret.read_text(encoding="utf-8") == "before"


def test_legacy_ls_read_denyroot_blocks_symlink_alias(workspace: Path):
    secret = workspace.parent / "secret"
    secret.mkdir()
    (secret / "private.txt").write_text("secret", encoding="utf-8")
    (workspace / "leak").symlink_to(secret, target_is_directory=True)
    guard = PathGuard(workspace, read_denyroots=[str(secret)])

    evaluation = PermissionEngine(
        mode="allow", allow=["ls"], path_guard=guard
    ).evaluate(
        ToolCall(id="list", name="ls", input={"path": "leak"}), ls.SPEC
    )
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "read_deny"


async def test_legacy_multiedit_execution_recheck_refuses_swapped_path(
    tmp_path: Path,
):
    workspace = tmp_path / "ws"
    parent = workspace / "a" / "sub"
    parent.mkdir(parents=True)
    (parent / "file.txt").write_text("before", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "file.txt"
    outside_target.write_text("outside", encoding="utf-8")
    manager = ToolManager(
        make_config(mode="allow", allow=["multiedit"]),
        workspace=workspace,
        tools=OPT_IN_TOOLS,
        tool_names=["multiedit"],
    )
    batch = manager.prepare(
        [
            ToolCall(
                id="edit",
                name="multiedit",
                input={
                    "path": "a/sub/file.txt",
                    "edits": [{"old_string": "before", "new_string": "after"}],
                },
            )
        ]
    )

    parent.rename(parent.with_name("sub-away"))
    parent.symlink_to(outside, target_is_directory=True)
    events: list = []
    results = await manager.dispatch(
        batch.with_decisions({"edit": Decision.ALLOW_ONCE}),
        ctx_factory(workspace),
        emit=events.append,
    )

    assert results[0].is_error is True
    assert [event.data.get("code") for event in events] == ["write_root"]
    assert not any(event.type == "tool.started" for event in events)
    assert outside_target.read_text(encoding="utf-8") == "outside"


# ---------------------------------------------------------------------------
# 4. Atomic write parent handling
# ---------------------------------------------------------------------------


async def test_atomic_write_refuses_symlinked_parent(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError):
        await write.atomic_write_bytes(
            ctx, link / "f.txt", b"x", create_parents=False
        )
    assert not (real / "f.txt").exists()


# ---------------------------------------------------------------------------
# 5. Approval prompt terminal safety (canonical CLI client)
# ---------------------------------------------------------------------------


def test_describe_shows_exact_scope_or_once_only_fallback():
    from nexus.ui.cli.approve import describe

    exact = describe(
        {
            "tool": "Bash",
            "key": "~/deploy.sh",
            "default_rule": 'Bash("~/deploy.sh")',
            "persistence_available": True,
        }
    )
    assert 'always persists: Bash("~/deploy.sh")' in exact

    once = describe(
        {
            "tool": "Probe",
            "key": "x" * 9000,
            "default_rule": 'Probe("exact key")',
            "persistence_available": False,
        }
    )
    assert "unavailable" in once
    assert "once only" in once
    assert "Probe(" not in once  # never a whole-tool suggestion for a keyed ask


def test_approver_sanitizes_control_injection():
    from nexus.ui.cli.approve import Approver

    payload = {
        "tool": "Bash\x1b]0;pwned\x07",
        "bundle": "shell\x9b31m",
        "key": "git status\nrm -rf /\t\x00\x1b[31m\u202eevil\u202c",
        "preview": "x\x07y",
        "suggestions": ["Bash(\x1b[2Jclear)", "ok\u200f"],
        "default_rule": 'Bash("\x1b[31m\u2066")',
    }
    stderr = io.StringIO()

    async def reader(prompt: str) -> str:
        return "n"

    approver = Approver(reader, stderr=stderr)
    decision = asyncio.run(approver.ask(payload))

    assert decision == "deny_once"
    output = stderr.getvalue()
    for raw in ("\x1b", "\x07", "\x9b", "\x00", "\u202e", "\u202c", "\u200f", "\u2066"):
        assert raw not in output
    assert "\\x1b" in output  # rendered visibly instead
    assert "\\u202e" in output
    assert "\\u2066" in output


def test_multi_target_approval_lists_every_path_without_truncation_and_fails_closed():
    from nexus.ui.cli.approve import MAX_PERMISSION_TARGETS, Approver, describe

    targets = [
        {"role": "source", "path": f"src/{index:02}.py", "reason": "Read required."}
        for index in range(MAX_PERMISSION_TARGETS)
    ]
    description = describe({"tool": "Move", "targets": targets})
    assert f"targets ({MAX_PERMISSION_TARGETS})" in description
    for target in targets:
        assert target["path"] in description

    protected = describe({
        "tool": "Write",
        "targets": [{
            "role": "destination\x1b[2J",
            "path": "private/api_key=supersecret-sk-abcdefghijk123\nfile",
            "reason": "credential ghp_12345678901234567890",
        }],
    })
    assert "\\x1b[2J" in protected
    # Nothing is redacted: the user sees what the agent sees (controls are still escaped).
    assert "supersecret" in protected
    assert "ghp_12345678901234567890" in protected
    assert "private/api_key=supersecret" in protected

    overflow = {"tool": "Move", "targets": [*targets, {
        "role": "source", "path": "src/overflow.py", "reason": "Read required."
    }]}
    assert "targets: unavailable" in describe(overflow)
    assert "always persists: unavailable" in describe(overflow)

    for answer in ("y", "a"):
        async def reader(prompt: str, answer=answer) -> str:
            return answer

        stderr = io.StringIO()
        decision = asyncio.run(Approver(reader, stderr=stderr).ask(overflow))
        assert decision == "deny_once"
        assert "src/overflow.py" not in stderr.getvalue()
        assert "refusing approval" in stderr.getvalue()


@pytest.mark.parametrize(
    "target",
    [
        {"role": "source", "path": "src/file.py"},
        {"role": "source", "path": "p" * 4097, "reason": "Read required."},
    ],
)
@pytest.mark.parametrize("answer", ["y", "a"])
def test_invalid_multi_target_approval_is_refused(target, answer):
    from nexus.ui.cli.approve import Approver

    async def reader(prompt: str) -> str:
        return answer

    stderr = io.StringIO()
    decision = asyncio.run(
        Approver(reader, stderr=stderr).ask({"tool": "Move", "targets": [target]})
    )
    assert decision == "deny_once"
    assert "refusing approval" in stderr.getvalue()


def test_scalar_approval_description_remains_unchanged():
    from nexus.ui.cli.approve import describe

    assert describe({"tool": "Bash", "key": "git status"}) == (
        "Permission requested: Bash\n  key: git status"
    )


def test_run_once_resolving_a_stale_request_does_not_hang(tmp_path: Path):
    # The daemon is the race arbiter: a losing/stale resolution returns False and
    # the caller reports it rather than deadlocking on a prompt nobody owns.
    from nexus.host import protocol as p
    from nexus.ui.cli.client import Client
    from nexus.ui.cli.stream import answer_permission

    class StaleTransport:
        async def request(self, command):
            assert isinstance(command, p.PermissionResolve)
            return p.PermissionResolveResult(
                session=command.session, request_id=command.request_id, resolved=False
            )

        async def aclose(self):
            return None

        def events(self, *args, **kwargs):  # pragma: no cover - unused here
            raise NotImplementedError

    async def reader(prompt: str) -> str:
        return "y"

    from nexus.ui.cli.approve import Approver

    err = io.StringIO()
    client = Client(StaleTransport())
    resolved = asyncio.run(
        answer_permission(
            client,
            "s",
            {"id": "r1", "tool": "Write"},
            Approver(reader, stderr=err),
            err,
        )
    )
    assert resolved is False
    assert "answered this request first" in err.getvalue()


# ---------------------------------------------------------------------------
# 6. Always-grant specificity
# ---------------------------------------------------------------------------


def test_exact_rule_round_trips_and_does_not_broaden():
    rule = parse_rule(exact_rule("Bash", "echo *"))
    assert rule.kind == "tool_exact"
    assert rule.matches("Bash", "echo *", "shell")
    assert not rule.matches("Bash", "echo rm -rf /", "shell")


def test_escaped_glob_matches_literal_metacharacters():
    rule = parse_rule(f"Bash({escape_glob('echo *')})")
    assert rule.kind == "tool_pattern"
    assert rule.matches("Bash", "echo *", "shell")
    assert not rule.matches("Bash", "echo other", "shell")
    assert not rule.matches("Bash", "echo * rm -rf /", "shell")


def test_always_grant_is_exact_and_deny_stays_absolute():
    engine = PermissionEngine(mode="ask", deny=["Bash(rm -rf*)"])
    grant = grant_for_decision(
        Decision.ALLOW_ALWAYS, rule=exact_rule("Bash", "echo *")
    )
    same = engine.evaluate(
        ToolCall(id="c1", name="Bash", input={"command": "echo *"}),
        bash_spec(),
        grants=[grant],
    )
    assert same.outcome is Outcome.ALLOW
    other = engine.evaluate(
        ToolCall(id="c2", name="Bash", input={"command": "echo rm -rf /"}),
        bash_spec(),
        grants=[grant],
    )
    assert other.outcome is not Outcome.ALLOW
    denied = engine.evaluate(
        ToolCall(id="c3", name="Bash", input={"command": "rm -rf /"}),
        bash_spec(),
        grants=[Grant(effect="allow", rule="Bash")],
    )
    assert denied.outcome is Outcome.DENY


def test_git_status_grant_does_not_grant_unrelated_commands():
    engine = PermissionEngine(mode="deny")
    grant = grant_for_decision(
        Decision.ALLOW_ALWAYS, rule=exact_rule("Bash", "git status")
    )
    assert (
        engine.evaluate(
            ToolCall(id="c1", name="Bash", input={"command": "git status"}),
            bash_spec(),
            grants=[grant],
        ).outcome
        is Outcome.ALLOW
    )
    assert (
        engine.evaluate(
            ToolCall(id="c2", name="Bash", input={"command": "rm -rf /"}),
            bash_spec(),
            grants=[grant],
        ).outcome
        is Outcome.DENY
    )


def test_request_for_default_rule_is_exact_scope():
    engine = PermissionEngine(mode="ask")
    evaluation = engine.evaluate(
        ToolCall(id="c1", name="Bash", input={"command": "git status"}), bash_spec()
    )
    request = engine.request_for(evaluation)
    assert request.default_rule == 'bash("git status")'
    assert request.persistence_available is True
    assert request.default_rule in request.suggestions


def _probe_spec() -> ToolSpec:
    return ToolSpec(
        name="Probe",
        description="probe",
        input_schema={"type": "object", "properties": {"v": {"type": "string"}}},
        bundle="task",
        permission_key=lambda data: str(data["v"]),
    )


def test_exact_rule_encodes_home_path_literally():
    rule = parse_rule(exact_rule("Bash", "~/deploy.sh"))
    assert rule.kind == "tool_exact"
    assert rule.matches("Bash", "~/deploy.sh", "shell")
    assert not rule.matches("Bash", "rm -rf /", "shell")
    # Glob/pattern rules still reject ``~`` where it is ambiguous or inert.
    with pytest.raises(PermissionRuleError):
        parse_rule("Bash(~/deploy.sh)")


def test_tilde_exact_grant_does_not_allow_rm_and_deny_dominates():
    engine = PermissionEngine(mode="ask", deny=["Bash(rm -rf*)"])
    grant = grant_for_decision(
        Decision.ALLOW_ALWAYS, rule=exact_rule("Bash", "~/deploy.sh")
    )
    assert (
        engine.evaluate(
            ToolCall(id="c1", name="Bash", input={"command": "~/deploy.sh"}),
            bash_spec(),
            grants=[grant],
        ).outcome
        is Outcome.ALLOW
    )
    assert (
        engine.evaluate(
            ToolCall(id="c2", name="Bash", input={"command": "rm -rf /"}),
            bash_spec(),
            grants=[grant],
        ).outcome
        is Outcome.DENY
    )


def test_keyed_request_uses_exact_home_path_default():
    engine = PermissionEngine(mode="ask")
    evaluation = engine.evaluate(
        ToolCall(id="c1", name="Bash", input={"command": "~/deploy.sh"}), bash_spec()
    )
    assert evaluation.outcome is Outcome.ASK
    request = engine.request_for(evaluation)
    assert request.default_rule == 'bash("~/deploy.sh")'
    assert request.persistence_available is True


async def test_broker_persists_exact_home_rule_on_allow_always():
    engine = PermissionEngine(mode="ask")
    evaluation = engine.evaluate(
        ToolCall(id="c1", name="Bash", input={"command": "~/deploy.sh"}), bash_spec()
    )
    request = engine.request_for(evaluation)
    broker = ApprovalBroker()
    future = broker.request(request)
    assert broker.resolve(request.id, Decision.ALLOW_ALWAYS) is True
    assert await future is Decision.ALLOW_ALWAYS

    grants = broker.grants()
    assert grants and grants[-1].rule == 'bash("~/deploy.sh")'
    assert grants[-1].matches("Bash", "~/deploy.sh", "shell")
    assert not grants[-1].matches("Bash", "rm -rf /", "shell")


async def test_overlong_keyed_request_degrades_to_once_with_no_grant():
    engine = PermissionEngine(mode="ask")
    evaluation = engine.evaluate(
        ToolCall(id="c1", name="Probe", input={"v": "x" * 9000}), _probe_spec()
    )
    assert evaluation.outcome is Outcome.ASK
    request = engine.request_for(evaluation)
    # No persistable rule and NO whole-tool/bundle suggestion.
    assert request.default_rule == ""
    assert request.persistence_available is False
    assert request.suggestions == ()
    assert "default_rule" in request.to_dict()
    assert request.to_dict()["persistence_available"] is False

    broker = ApprovalBroker()
    future = broker.request(request)
    assert broker.resolve(request.id, Decision.ALLOW_ALWAYS) is True
    assert await future is Decision.ALLOW_ONCE  # deterministically degraded
    assert broker.records[0]["decision"] == "allow_once"
    assert "grant" not in broker.records[0]
    assert broker.grants() == ()


async def test_broker_deny_always_exact_and_overlong_fallback():
    engine = PermissionEngine(mode="ask")
    exact_eval = engine.evaluate(
        ToolCall(id="c1", name="Bash", input={"command": "~/deploy.sh"}), bash_spec()
    )
    exact_request = engine.request_for(exact_eval)
    broker = ApprovalBroker()
    future = broker.request(exact_request)
    assert broker.resolve(exact_request.id, Decision.DENY_ALWAYS) is True
    assert await future is Decision.DENY_ALWAYS
    assert broker.records[0]["grant"]["rule"] == 'bash("~/deploy.sh")'
    assert broker.records[0]["grant"]["effect"] == "deny"

    long_eval = engine.evaluate(
        ToolCall(id="c2", name="Probe", input={"v": "y" * 9000}), _probe_spec()
    )
    long_request = engine.request_for(long_eval)
    assert long_request.default_rule == ""
    broker2 = ApprovalBroker()
    future2 = broker2.request(long_request)
    assert broker2.resolve(long_request.id, Decision.DENY_ALWAYS) is True
    assert await future2 is Decision.DENY_ONCE
    assert "grant" not in broker2.records[0]
    assert broker2.grants() == ()


async def test_broker_degrades_empty_rule_always_to_once():
    broker = ApprovalBroker()
    request = PermissionRequest(
        id="req-1",
        call_id="c1",
        tool="Read",
        key="x" * 9000,
        default_rule="",
        persistence_available=False,
    )
    future = broker.request(request)
    assert broker.resolve("req-1", Decision.ALLOW_ALWAYS) is True
    assert future.result() is Decision.ALLOW_ONCE
    assert "grant" not in broker.records[0]
    assert broker.grants() == ()


def test_collect_grants_never_widens_keyed_record_to_bare_tool():
    records = [
        # Keyed, no rule -> bounded exact rule (never bare "Bash").
        {"decision": "allow_always", "tool": "Bash", "key": "git status"},
        # Keyed with a bare whole-tool rule -> dropped, not replayed broad.
        {"decision": "allow_always", "tool": "Bash", "key": "git status", "rule": "Bash"},
        # Truncated key -> dropped.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": "x",
            "key_truncated": True,
        },
        # Keyless with only a tool -> dropped (no broad fallback).
        {"decision": "allow_always", "tool": "Write"},
    ]
    grants = collect_grants(records)
    assert [grant.rule for grant in grants] == ['bash("git status")']


def test_collect_grants_rejects_broader_or_mismatched_keyed_rules():
    key = "git status"
    exact = exact_rule("Bash", key)
    records = [
        # A persisted grant object with a bare tool rule.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": key,
            "grant": {"effect": "allow", "rule": "Bash", "scope": "session"},
        },
        # ... with a glob rule.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": key,
            "grant": {"effect": "allow", "rule": "Bash(git*)", "scope": "session"},
        },
        # ... with a bundle rule.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": key,
            "grant": {"effect": "allow", "rule": "Bundle:shell", "scope": "session"},
        },
        # ... with a wildcard rule.
        {
            "decision": "allow_always",
            "tool": "mcp__srv__echo",
            "key": "x",
            "grant": {"effect": "allow", "rule": "mcp__srv__*", "scope": "session"},
        },
        # ... with an exact rule for a different tool.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": key,
            "grant": {"effect": "allow", "rule": 'Read("git status")', "scope": "session"},
        },
        # ... with an exact rule for a different key.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": key,
            "grant": {"effect": "allow", "rule": 'Bash("other")', "scope": "session"},
        },
        # Explicit (non-grant) broader rules for a keyed record.
        {"decision": "allow_always", "tool": "Bash", "key": key, "rule": "Bash(git*)"},
        {
            "decision": "deny_always",
            "tool": "Bash",
            "key": key,
            "rule": "Bundle:shell",
        },
        # The only acceptable keyed grant: an exact rule for the same tool/key.
        {
            "decision": "allow_always",
            "tool": "Bash",
            "key": key,
            "grant": {"effect": "allow", "rule": exact, "scope": "session"},
        },
    ]
    grants = collect_grants(records)
    assert [grant.rule for grant in grants] == [exact]
    assert grants[0].matches("Bash", key, "shell")
    assert not grants[0].matches("Bash", "rm -rf /", "shell")


async def test_multi_target_replay_rejects_partial_persistent_grant_set(
    tmp_path: Path,
):
    paths = (str(tmp_path / "one.txt"), str(tmp_path / "two.txt"))
    spec = ToolSpec(
        name="Multi",
        description="multi",
        input_schema={"type": "object"},
        bundle="fs",
        mutates=True,
        permission_key=lambda _data: "unused-scalar-key",
        multi_path_targets=lambda data: tuple(
            PathTarget(role, path) for role, path in data["targets"]
        ),
    )
    evaluation = PermissionEngine(mode="ask", workspace=tmp_path).plan(
        [
            ToolCall(
                id="multi",
                name="Multi",
                input={"targets": tuple(("target", path) for path in paths)},
            )
        ],
        {"Multi": spec},
    ).evaluations[0]
    request = PermissionEngine(mode="ask", workspace=tmp_path).request_for(
        evaluation
    )
    assert len(request.targets) == 2, (evaluation.outcome, evaluation.reason)
    broker = ApprovalBroker()
    future = broker.request(request)
    assert broker.resolve(request.id, Decision.ALLOW_ALWAYS)
    assert await future is Decision.ALLOW_ALWAYS

    record = dict(broker.records[0])
    assert "target_grants" in record, record
    partial = list(record["target_grants"])
    partial[1] = {
        **partial[1],
        "grant": {
            **partial[1]["grant"],
            "rule": 'Multi("/a/different/path")',
        },
    }
    record["target_grants"] = partial

    assert collect_grants([record]) == ()


async def test_multi_target_deny_prevents_approval_and_dispatch(tmp_path: Path):
    denied_path = str((tmp_path / "denied.txt").resolve())
    ran: list[bool] = []

    async def run(_args, _ctx):
        ran.append(True)
        return ToolExecutionResult.text("ran")

    spec = ToolSpec(
        name="Multi",
        description="multi",
        input_schema={"type": "object"},
        bundle="fs",
        mutates=True,
        permission_key=lambda _data: "unused-scalar-key",
        multi_path_targets=lambda data: tuple(
            PathTarget(role, path) for role, path in data["targets"]
        ),
    )
    manager = ToolManager(
        make_config(mode="allow"),
        workspace=tmp_path,
        tools=[RegisteredTool(spec=spec, run=run, origin="builtin")],
        tool_names=["Multi"],
    )
    prepared = manager.prepare(
        [
            ToolCall(
                id="multi",
                name="Multi",
                input={
                    "targets": [
                        ["source", "allowed.txt"],
                        ["destination", "denied.txt"],
                    ]
                },
            )
        ]
    )
    plan = PermissionEngine(
        mode="allow",
        deny=[exact_rule("Multi", denied_path)],
        path_guard=manager.path_guard,
    ).plan(prepared.calls(), prepared.spec_map())

    evaluation = plan.evaluations[0]
    assert evaluation.outcome is Outcome.DENY
    assert [target.outcome for target in evaluation.target_evaluations] == [
        Outcome.ALLOW,
        Outcome.DENY,
    ]
    assert plan.asks() == ()
    gated = prepared.apply_plan(plan)
    assert gated.for_call("multi").error is not None

    events: list = []
    results = await manager.dispatch(gated, emit=events.append)
    assert results[0].is_error is True
    assert ran == []
    assert not any(event.type == "tool.started" for event in events)
    await manager.aclose()


def test_collect_grants_keeps_keyless_broad_rules():
    records = [
        {
            "decision": "allow_always",
            "tool": "Bash",
            "grant": {"effect": "allow", "rule": "Bundle:shell", "scope": "session"},
        },
        {"decision": "allow_always", "tool": "Bash", "rule": "Bash(git*)"},
    ]
    grants = collect_grants(records)
    assert [grant.rule for grant in grants] == ["Bundle:shell", "Bash(git*)"]


@pytest.mark.parametrize(
    "bad_rule",
    [
        "Bash",  # bare tool
        "Bash(git*)",  # glob/pattern
        "Bundle:shell",  # bundle
        "mcp__srv__*",  # wildcard
        'Read("git status")',  # exact, wrong tool
        'Bash("other")',  # exact, wrong key
    ],
)
@pytest.mark.parametrize(
    "decision,expected",
    [
        (Decision.ALLOW_ALWAYS, Decision.ALLOW_ONCE),
        (Decision.DENY_ALWAYS, Decision.DENY_ONCE),
    ],
)
async def test_broker_degrades_keyed_nonexact_caller_rule(
    bad_rule, decision, expected
):
    broker = ApprovalBroker()
    exact = exact_rule("Bash", "git status")
    request = PermissionRequest(
        id="r", call_id="c", tool="Bash", key="git status", default_rule=exact
    )
    future = broker.request(request)
    assert broker.resolve("r", decision, rule=bad_rule) is True
    assert await future is expected
    assert broker.records[0]["decision"] == expected.value
    assert "grant" not in broker.records[0]
    assert broker.grants() == ()


async def test_broker_allows_matching_exact_caller_rule_for_keyed_request():
    broker = ApprovalBroker()
    exact = exact_rule("Bash", "git status")
    request = PermissionRequest(
        id="r", call_id="c", tool="Bash", key="git status", default_rule=exact
    )
    future = broker.request(request)
    assert broker.resolve("r", Decision.ALLOW_ALWAYS, rule=exact) is True
    assert await future is Decision.ALLOW_ALWAYS
    assert broker.records[0]["grant"]["rule"] == exact
    grants = broker.grants()
    assert grants[0].matches("Bash", "git status", "shell")
    assert not grants[0].matches("Bash", "rm -rf /", "shell")


async def test_broker_allows_broader_rule_for_keyless_request():
    broker = ApprovalBroker()
    request = PermissionRequest(
        id="r", call_id="c", tool="Bash", key=None, default_rule="Bash"
    )
    future = broker.request(request)
    assert broker.resolve("r", Decision.ALLOW_ALWAYS, rule="Bundle:shell") is True
    assert await future is Decision.ALLOW_ALWAYS
    assert broker.records[0]["grant"]["rule"] == "Bundle:shell"


# ---------------------------------------------------------------------------
# 7. Grep DoS bounds and killable worker
# ---------------------------------------------------------------------------


async def test_grep_bounds_stored_line_length(workspace: Path):
    (workspace / "big.txt").write_text("needle" + "x" * 200_000 + "\n", encoding="utf-8")
    result = await grep.run({"pattern": "needle"}, make_ctx(workspace))
    assert result.is_error is False
    assert result.metrics["line_truncated"] is True
    first_line = result.content[0].text.split("\n")[0]
    assert len(first_line) <= 2100


async def test_grep_bounds_many_matches(workspace: Path):
    (workspace / "m.txt").write_text("hit\n" * 1000, encoding="utf-8")
    result = await grep.run({"pattern": "hit"}, make_ctx(workspace))
    assert result.metrics["matches"] <= 200
    assert result.metrics["total_matches"] == 1000
    assert result.metrics["truncated"] is True


async def test_grep_catastrophic_pattern_times_out_without_leaking_workers(
    workspace: Path,
):
    (workspace / "f.txt").write_text("a" * 64 + "b\n", encoding="utf-8")
    ctx = make_ctx(workspace, config=make_config(grep_timeout_s=0.3))
    started = time.monotonic()
    result = await grep.run({"pattern": "(a+)+$"}, ctx)
    elapsed = time.monotonic() - started

    assert result.is_error is True
    # Classified as a resource/timeout outcome, not a "bad pattern" error.
    assert "deadline" in result.content[0].text
    assert result.metrics["timeout"] is True
    assert result.metrics["timeout_s"] == 0.3
    assert elapsed < 5
    assert grep.live_workers() == ()


def test_grep_timeout_config_is_validated():
    from nexus.config.schema import ToolsSection

    assert ToolsSection().grep_timeout_s == 5.0
    with pytest.raises(ValueError):
        ToolsSection(grep_timeout_s=0)
    with pytest.raises(ValueError):
        ToolsSection(grep_timeout_s=float("inf"))
    with pytest.raises(ValueError):
        ToolsSection(grep_timeout_s=-1.0)


async def test_grep_rejects_oversize_pattern(workspace: Path):
    result = await grep.run({"pattern": "a" * 5000}, make_ctx(workspace))
    assert result.is_error is True


def test_glob_rejects_oversize_pattern_and_segments(workspace: Path):
    from nexus.tools.builtin import glob as glob_tool

    assert glob_tool is not None
    long_result = asyncio.run(
        glob_tool.run({"pattern": "a" * 2000}, make_ctx(workspace))
    )
    assert long_result.is_error is True
    segments = asyncio.run(
        glob_tool.run({"pattern": "/".join(["a"] * 100)}, make_ctx(workspace))
    )
    assert segments.is_error is True


async def test_glob_star_star_bomb_is_rejected_promptly(workspace: Path):
    from nexus.tools.builtin import glob as glob_tool

    bomb = "/".join(["**"] * 40) + "/x.py"
    started = time.monotonic()
    result = await glob_tool.run({"pattern": bomb}, make_ctx(workspace))
    assert result.is_error is True
    assert "**" in result.content[0].text
    assert time.monotonic() - started < 2


async def test_glob_recursive_match_on_deep_tree_is_bounded(workspace: Path):
    from nexus.tools.builtin import glob as glob_tool

    deep = workspace
    for index in range(30):
        deep = deep / f"d{index}"
        deep.mkdir()
    (deep / "leaf.py").write_text("x", encoding="utf-8")

    # At the ``**`` cap: must return the correct bounded result promptly.
    pattern = "/".join(["**"] * 16) + "/*.py"
    started = time.monotonic()
    result = await glob_tool.run({"pattern": pattern}, make_ctx(workspace))
    assert result.is_error is False
    assert "leaf.py" in result.content[0].text
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------------------
# 8. Path canonicalization: symlink loops
# ---------------------------------------------------------------------------


class _BoomPath:
    def resolve(self, strict: bool = False):
        raise RuntimeError("Symlink loop")

    def __str__(self) -> str:
        return "boom"


def test_canonical_symlink_loop_is_path_security_error():
    with pytest.raises(PathSecurityError) as excinfo:
        permissions._canonical(_BoomPath())  # type: ignore[arg-type]
    assert excinfo.value.code == "unresolvable"


def test_native_symlink_loop_never_authorizes_outside(tmp_path: Path):
    # A real loop must fail closed or stay inside the workspace; it must never
    # authorize a path outside it.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").symlink_to(ws / "b")
    (ws / "b").symlink_to(ws / "a")
    guard = PathGuard(ws)
    try:
        resolved = guard.resolve("a/secret", for_write=True)
    except PathSecurityError:
        return
    assert resolved.inside_workspace is True


# ---------------------------------------------------------------------------
# 9. Tilde permission-rule patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["Read(~/.ssh/**)", "Read(~)", "Read(foo/~/bar)", "Write(~/x)", "Read(~\\.ssh)"],
)
def test_tilde_rule_pattern_is_rejected(raw: str):
    with pytest.raises(PermissionRuleError):
        parse_rule(raw)


# ---------------------------------------------------------------------------
# 10. Job registry retention
# ---------------------------------------------------------------------------


async def test_registry_evicts_oldest_completed_jobs(workspace: Path):
    registry = _jobs.JobRegistry(max_completed_jobs=2)
    ids: list[str] = []
    for _ in range(3):
        job = await registry.spawn("true", cwd=workspace)
        ids.append(job.job_id)
        await job.wait()

    assert registry.job(ids[0]) is None  # oldest evicted
    assert registry.job(ids[1]) is not None
    assert registry.job(ids[2]) is not None

    # Active jobs are never evicted by retention.
    active = await registry.spawn("sleep 30", cwd=workspace)
    job = await registry.spawn("true", cwd=workspace)
    await job.wait()
    assert registry.job(active.job_id) is not None
    await registry.aclose()


# ---------------------------------------------------------------------------
# 11. Runtime tool-manager ownership
# ---------------------------------------------------------------------------


async def test_injected_tool_manager_closed_only_when_owned(tmp_path: Path):
    manager = ToolManager(make_config(), workspace=tmp_path, profile="research")
    runtime = Runtime(
        tmp_path,
        config=make_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
        tools=manager,
    )
    await runtime.aclose()
    assert manager.closed is False

    owned = ToolManager(make_config(), workspace=tmp_path, profile="research")
    runtime2 = Runtime(
        tmp_path,
        config=make_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
        tools=owned,
        owns_tools=True,
    )
    await runtime2.aclose()
    assert owned.closed is True


async def test_runtime_built_tool_manager_is_closed(tmp_path: Path):
    runtime = Runtime(
        tmp_path,
        config=make_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    session = runtime.session("t")
    turn = runtime._make_tool_turn(
        config=make_config(), session=session, turn_id="turn-1", attended=False
    )
    manager = turn.manager
    assert manager.closed is False
    await runtime.aclose()
    assert manager.closed is True
