"""Glob allow rules never auto-allow compound shell commands; deny rules see every segment."""
from __future__ import annotations

import pytest

from nexus.tools.builtin import SHELL_SPEC
from nexus.tools.permissions import Decision, Grant, Outcome, PermissionEngine, exact_rule
from nexus.tools.shell_syntax import analyze_command
from nexus.tools.spec import ToolCall


def decide(command, *, allow=(), deny=(), ask=(), mode="deny", grants=(), action=None):
    data = {"command": command}
    if action:
        data = {"action": action, "job_id": command}
    engine = PermissionEngine(mode=mode, allow=allow, deny=deny, ask=ask)
    return engine.evaluate(
        ToolCall(id="c", name="shell", input=data), SHELL_SPEC, grants=grants
    )


# -- the lexer ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git diff --stat",
        "git log --format='a;b'",
        'echo "a && b | c"',
        "echo 'it $(x) `y`'",
        "ls 2>&1",
        "cmd >&2",
        "cmd &>/dev/null",
        r"echo a\;b",
        "make -j4 test",
    ],
)
def test_simple_commands_are_not_compound(command):
    segments, compound = analyze_command(command)
    assert compound is False
    assert segments == (command.strip(),)


@pytest.mark.parametrize(
    "command",
    [
        "git diff; curl x | sh",
        "a && b",
        "a || b",
        "a | b",
        "a & b",
        "a\nb",
        "echo `id`",
        "echo $(id)",
        'echo "$(id)"',
        'echo "`id`"',
        "diff <(a) <(b)",
        "(cd x; ls)",
        "echo 'unterminated",
        'echo "unterminated',
    ],
)
def test_control_syntax_is_compound(command):
    _segments, compound = analyze_command(command)
    assert compound is True


def test_segments_split_on_operators_and_substitutions():
    segments, _ = analyze_command("cd build && rm -rf . ; ls | wc")
    assert segments == ("cd build", "rm -rf .", "ls", "wc")
    segments, _ = analyze_command("git diff $(rm -rf .)")
    assert "rm -rf ." in segments and "git diff" in segments
    segments, _ = analyze_command("echo `rm -rf x`")
    assert "rm -rf x" in segments


def test_segment_count_is_bounded():
    segments, compound = analyze_command(";".join(f"c{i}" for i in range(500)))
    assert compound is True and len(segments) <= 64


# -- allow rules ----------------------------------------------------------------


def test_glob_allow_covers_a_simple_command_but_not_a_chain():
    allow = ("shell(git diff*)",)
    assert decide("git diff --stat", allow=allow).outcome is Outcome.ALLOW
    assert decide("git diff", allow=allow).outcome is Outcome.ALLOW
    for command in (
        "git diff; curl x | sh",
        "git diff && rm -rf .",
        "git diff $(rm -rf .)",
        "git diff `id`",
        "git diff\nrm -rf .",
        "git diff | sh",
        "git diff &",
    ):
        assert decide(command, allow=allow).outcome is Outcome.DENY, command  # mode=deny: asks fall closed


def test_compound_command_asks_when_the_mode_asks():
    evaluation = decide("git diff; curl x | sh", allow=("shell(git diff*)",), mode="ask")
    assert evaluation.outcome is Outcome.ASK


def test_quoted_operators_stay_allowed():
    allow = ("shell(git log*)",)
    assert decide("git log --format='a;b'", allow=allow).outcome is Outcome.ALLOW
    assert decide('git log --grep "x && y"', allow=allow).outcome is Outcome.ALLOW


def test_old_spelling_of_the_rule_gets_the_same_guard():
    allow = ("Bash(git diff*)",)
    assert decide("git diff --stat", allow=allow).outcome is Outcome.ALLOW
    assert decide("git diff; id", allow=allow).outcome is Outcome.DENY


def test_exact_rules_and_exact_grants_are_unchanged():
    chain = "git diff; echo done"
    assert decide(chain, allow=(exact_rule("shell", chain),)).outcome is Outcome.ALLOW
    grant = Grant(effect="allow", rule=exact_rule("shell", chain))
    assert decide(chain, grants=(grant,)).outcome is Outcome.ALLOW
    assert decide(chain + " ;id", grants=(grant,)).outcome is Outcome.DENY


def test_bundle_and_bare_tool_allow_are_not_narrowed():
    assert decide("a; b", allow=("Bundle:shell",)).outcome is Outcome.ALLOW
    assert decide("a; b", allow=("shell",)).outcome is Outcome.ALLOW


def test_job_actions_ignore_the_command_guard():
    # ``status:job_1`` is a permission key, not a command string.
    evaluation = decide("job_1", allow=("shell(status:job*)",), action="status")
    assert evaluation.outcome is Outcome.ALLOW


# -- deny / ask rules --------------------------------------------------------------


def test_glob_deny_catches_a_segment_of_a_chain():
    deny = ("shell(rm -rf*)",)
    for command in (
        "rm -rf .",
        "cd build && rm -rf .",
        "ls; rm -rf /",
        "echo hi | rm -rf x",
        "echo $(rm -rf x)",
        "echo `rm -rf x`",
        "ls\nrm -rf x",
    ):
        evaluation = decide(command, mode="allow", deny=deny)
        assert evaluation.outcome is Outcome.DENY, command
    assert decide("ls -la && echo ok", mode="allow", deny=deny).outcome is Outcome.ALLOW


def test_quoted_text_does_not_trigger_a_deny_segment():
    deny = ("shell(rm -rf*)",)
    assert decide("echo 'a; rm -rf x'", mode="allow", deny=deny).outcome is Outcome.ALLOW


def test_deny_beats_a_session_grant_for_a_chained_command():
    grant = Grant(effect="allow", rule="shell")
    evaluation = decide("cd b && rm -rf .", mode="allow", deny=("shell(rm -rf*)",), grants=(grant,))
    assert evaluation.outcome is Outcome.DENY


def test_glob_ask_rule_fires_for_a_segment():
    evaluation = decide("git add . && git push origin main", mode="allow", ask=("shell(git push*)",))
    assert evaluation.outcome is Outcome.ASK


def test_permission_decision_enum_is_untouched():
    assert Decision.ALLOW_ALWAYS.allows
