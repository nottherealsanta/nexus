"""The ``bash`` tool is now ``shell``; every historical spelling keeps its meaning."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection, ToolsSection
from nexus.model.message import Message, Text, ToolUse
from nexus.tools.builtin import SHELL_SPEC, _jobs, bash
from nexus.tools.builtin import shell as shell_module
from nexus.tools.manager import ToolManager
from nexus.tools.names import (
    canonical_permission_rule,
    canonical_tool_name,
    canonical_tool_names,
    translate_legacy_shell_input,
)
from nexus.tools.permissions import (
    Grant,
    Outcome,
    PermissionEngine,
    exact_rule,
    parse_rule,
)
from nexus.tools.spec import ToolCall, ToolContext


def cfg(**tools_kw) -> Config:
    return Config(
        v2=ConfigV2(
            permissions=PermissionsSection(mode="allow"),
            tools=ToolsSection(**tools_kw),
        )
    )


def text_of(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, Text))


def evaluate(command: str, *, name: str = "shell", allow=(), deny=(), ask=(), grants=(), mode="deny"):
    engine = PermissionEngine(mode=mode, allow=allow, deny=deny, ask=ask)
    return engine.evaluate(
        ToolCall(id="c", name=name, input={"command": command}),
        SHELL_SPEC,
        grants=grants,
    )


# -- names -------------------------------------------------------------------


@pytest.mark.parametrize("legacy", ["bash", "Bash"])
def test_legacy_names_canonicalize_to_shell(legacy):
    assert canonical_tool_name(legacy) == "shell"
    assert canonical_tool_name("shell") == "shell"


def test_alias_is_exact_not_case_folding():
    for other in ("BASH", "bAsH", "Shell", "bash ", "bash*"):
        assert canonical_tool_name(other) == other


def test_declarations_naming_bash_get_shell_and_collisions_are_reported():
    names, notices = canonical_tool_names(["bash", "read"])
    assert names == ("shell", "read") and notices == ()
    names, notices = canonical_tool_names(["Bash", "bash", "shell"])
    assert names == ("shell",) and notices == ("shell",)


def test_legacy_arguments_are_translated_only_for_legacy_names():
    legacy = {"command": "ls", "workdir": "a", "run_in_background": True}
    assert translate_legacy_shell_input("bash", legacy) == {
        "command": "ls", "cwd": "a", "background": True,
    }
    assert translate_legacy_shell_input("Bash", legacy)["cwd"] == "a"
    # A call that already says ``shell`` is validated as written (and rejected).
    assert translate_legacy_shell_input("shell", legacy) == legacy
    # Both spellings at once are left alone so validation rejects the call.
    both = {"command": "ls", "workdir": "a", "cwd": "b"}
    assert translate_legacy_shell_input("bash", both) == both
    # Per-stream offsets keep their names for status/wait.
    offsets = {"action": "status", "job_id": "j", "stdout_offset": 3}
    assert translate_legacy_shell_input("bash", offsets) == offsets


# -- dispatch ------------------------------------------------------------------


async def test_bash_call_with_old_arguments_dispatches_to_shell(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    registry = _jobs.JobRegistry()
    manager = ToolManager(cfg(), workspace=tmp_path, job_registry=registry)
    try:
        batch = manager.prepare(
            [
                ToolCall(
                    id="c1",
                    name="Bash",
                    input={"command": "pwd", "workdir": "sub", "run_in_background": False},
                )
            ]
        )
        entry = batch.for_call("c1")
        assert entry.error is None, entry.error
        assert entry.call.name == "shell"
        assert entry.call.input == {"command": "pwd", "cwd": "sub", "background": False}
        gated = batch.with_decisions({"c1": _allow_once()})
        results = await manager.dispatch(
            gated,
            lambda call, spec: ToolContext(
                workspace=tmp_path, session_id="s", turn_id="t", config=cfg()
            ),
        )
        assert results[0].is_error is False
        assert str(tmp_path.resolve() / "sub") in text_of(results[0])
    finally:
        await registry.aclose()


def _allow_once():
    from nexus.tools.permissions import Decision

    return Decision.ALLOW_ONCE


def test_explicit_tool_selection_by_old_name_gets_shell(tmp_path: Path):
    manager = ToolManager(cfg(), workspace=tmp_path, tool_names=("bash", "read"))
    assert manager.names == ("shell", "read")


async def test_bash_shim_module_accepts_old_arguments(tmp_path: Path):
    registry = _jobs.JobRegistry()
    _jobs.set_default_registry(registry)
    try:
        (tmp_path / "w").mkdir()
        ctx = ToolContext(
            workspace=tmp_path.resolve(), session_id="s", turn_id="t", config=Config()
        )
        result = await bash.run({"command": "pwd", "workdir": "w"}, ctx)
        assert result.is_error is False and "/w" in text_of(result)
        rejected = await shell_module.run({"command": "pwd", "workdir": "w"}, ctx)
        assert rejected.is_error is True and "renamed" in text_of(rejected)
    finally:
        await registry.aclose()
        _jobs.set_default_registry(None)


def test_shell_timeout_backstop_uses_new_key_then_deprecated_alias(tmp_path: Path):
    from nexus.tools.manager import BASH_TIMEOUT_GRACE_S

    by_alias = ToolManager(cfg(bash_max_s=700), workspace=tmp_path)
    assert by_alias._timeout_for(SHELL_SPEC) == 700 + BASH_TIMEOUT_GRACE_S
    by_new = ToolManager(cfg(bash_max_s=700, shell_max_s=1500), workspace=tmp_path)
    assert by_new._timeout_for(SHELL_SPEC) == 1500 + BASH_TIMEOUT_GRACE_S


def test_config_yield_window_prefers_shell_key_over_aliases():
    tools = ToolsSection(bash_timeout_s=7, bash_yield_s=9)
    assert tools.shell_yield_window_s == 9
    assert ToolsSection(bash_yield_s=9, shell_yield_s=4).shell_yield_window_s == 4
    assert ToolsSection().shell_yield_window_s == 120
    assert ToolsSection(bash_max_s=800).shell_max_runtime_s == 800
    assert ToolsSection(bash_max_s=800, shell_max_s=900).shell_max_runtime_s == 900
    with pytest.raises(ValueError):
        ToolsSection(shell_max_s=0)


# -- permission rules, grants, hooks --------------------------------------------


@pytest.mark.parametrize("head", ["Bash", "bash", "shell"])
def test_old_and_new_rule_spellings_allow_and_deny_the_shell_tool(head):
    allow = (f"{head}(git status*)",)
    for call_name in ("shell", "bash", "Bash"):
        assert evaluate("git status", name=call_name, allow=allow).outcome is Outcome.ALLOW
        assert evaluate("rm -rf x", name=call_name, allow=allow).outcome is Outcome.DENY
        denied = evaluate(
            "rm -rf x", name=call_name, mode="allow", deny=(f"{head}(rm -rf*)",)
        )
        assert denied.outcome is Outcome.DENY


@pytest.mark.parametrize("pattern", ["b*", "Ba*", "ba*", "Bash*", "bas?", "shell", "s*"])
def test_wildcard_deny_written_for_the_old_name_still_denies_shell(pattern):
    # Wildcard heads are not rewritten, so they must keep matching the renamed tool.
    evaluation = evaluate("ls", mode="allow", deny=(pattern,))
    assert evaluation.outcome is Outcome.DENY, pattern


def test_wildcard_that_matched_no_old_name_does_not_match_shell():
    for pattern in ("z*", "Bashx*", "read*"):
        assert evaluate("ls", mode="allow", deny=(pattern,)).outcome is Outcome.ALLOW


def test_stored_bash_grant_still_matches_and_never_widens():
    grant = Grant(effect="allow", rule=exact_rule("bash", "git status"))
    assert evaluate("git status", grants=(grant,)).outcome is Outcome.ALLOW
    assert evaluate("git status; rm -rf x", grants=(grant,)).outcome is Outcome.DENY
    assert Grant(effect="allow", rule='bash("git status")').rule == 'bash("git status")'
    assert parse_rule('Bash("git status")').tool == "shell"


def test_hook_matcher_for_bash_matches_shell_calls():
    assert parse_rule("Bash").matches("shell", None, "shell")
    assert parse_rule("bash(git *)").matches("shell", "git push", "shell", effect="ask")
    assert canonical_permission_rule("Bash(git *)") == "shell(git *)"


def test_legacy_job_rules_never_authorise_a_run():
    for rule in ("BashOutput", "KillShell"):
        assert evaluate("status:job_1", allow=(rule,)).outcome is Outcome.DENY
    call = ToolCall(
        id="s", name="bash", input={"action": "status", "job_id": "job_1"}
    )
    engine = PermissionEngine(mode="deny", allow=("BashOutput",))
    assert engine.evaluate(call, SHELL_SPEC).outcome is Outcome.ALLOW


# -- history ---------------------------------------------------------------------


def test_old_session_history_names_shell_in_the_next_request():
    from nexus.context.manager import _present_legacy_shell_history

    old = Message(
        role="assistant",
        content=[
            Text(text="running"),
            ToolUse(id="t1", name="bash", input={"command": "ls", "workdir": "a"}),
            ToolUse(id="t2", name="read", input={"path": "x"}),
        ],
    )
    plain = Message(role="user", content=[Text(text="hi")])
    out = _present_legacy_shell_history([plain, old])
    assert out[0] is plain
    blocks = out[1].content
    assert [b.name for b in blocks if isinstance(b, ToolUse)] == ["shell", "read"]
    assert blocks[1].input == {"command": "ls", "cwd": "a"}
    # The stored message is not modified.
    assert old.content[1].name == "bash"
