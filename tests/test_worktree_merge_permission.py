"""The agent's own landing (`git -C <main> merge --ff-only <branch>`) never asks; nothing else does that."""
from __future__ import annotations

import pytest

from nexus.tools.permissions import Outcome, PermissionEngine, WorktreeMergePolicy, is_worktree_landing
from nexus.tools.spec import ToolCall

MAIN = "/work/repo"
BRANCH = "nexus/wt/brave-otter"
POLICY = WorktreeMergePolicy(
    frozenset({MAIN}),
    lambda name: name == BRANCH,
    lambda name: name in {"main", "develop"},
)


@pytest.mark.parametrize("command", [
    f"git -C {MAIN} merge --ff-only {BRANCH}",
    f"git -C '{MAIN}' merge --ff-only '{BRANCH}'",
    f'git -C "{MAIN}/" merge --ff-only {BRANCH}',
    f"git -C {MAIN} checkout main",
    f"git -C {MAIN} switch develop",
])
def test_exact_landing_forms_are_recognised(command):
    assert is_worktree_landing(command, POLICY)


@pytest.mark.parametrize("command", [
    f"git -C {MAIN} merge --no-ff {BRANCH}",
    f"git -C {MAIN} merge --ff-only {BRANCH} --no-verify",
    f"git -C {MAIN} merge --ff-only nexus/wt/other",
    f"git -C {MAIN} merge --ff-only main",
    f"git -C {MAIN} merge --ff-only -- {BRANCH}",
    f"git -C /elsewhere merge --ff-only {BRANCH}",
    f"git -C {MAIN}/sub merge --ff-only {BRANCH}",
    f"git -C {MAIN} merge --ff-only {BRANCH} && rm -rf /",
    f"git -C {MAIN} merge --ff-only {BRANCH}; echo x",
    f"git -C {MAIN} merge --ff-only {BRANCH} | cat",
    f"git -C {MAIN} merge --ff-only {BRANCH} > out.txt",
    f"git -C {MAIN} merge --ff-only $(echo {BRANCH})",
    f"git -C {MAIN} merge --ff-only `echo {BRANCH}`",
    f"git -C {MAIN} merge --ff-only {BRANCH}\nrm x",
    f"cd {MAIN} && git merge --ff-only {BRANCH}",
    f"GIT_DIR=/x git -C {MAIN} merge --ff-only {BRANCH}",
    f"git --git-dir=/x -C {MAIN} merge --ff-only {BRANCH}",
    f"git -c core.hooksPath=/x -C {MAIN} merge --ff-only {BRANCH}",
    f"git -C {MAIN} checkout -b evil",
    f"git -C {MAIN} checkout unknown-branch",
    f"git -C {MAIN} checkout main -- file",
    f"git -C {MAIN} reset --hard {BRANCH}",
    "git status",
    "",
])
def test_anything_else_is_not_a_landing(command):
    assert not is_worktree_landing(command, POLICY)


def test_no_policy_means_no_exemption():
    assert not is_worktree_landing(f"git -C {MAIN} merge --ff-only {BRANCH}", None)


def _shell(command: str):
    return ToolCall(id="c", name="shell", input={"action": "run", "command": command})


@pytest.mark.parametrize("mode", ["ask", "allow", "deny"])
def test_landing_runs_without_asking_in_every_mode_but_variants_ask(mode):
    from nexus.tools.builtin import shell

    spec = shell.SPEC
    engine = PermissionEngine(mode=mode if mode != "deny" else "deny")
    engine.allow_worktree_merge(POLICY)
    ok = engine.evaluate(_shell(f"git -C {MAIN} merge --ff-only {BRANCH}"), spec)
    assert ok.outcome is Outcome.ALLOW and ok.code == "worktree_merge"
    for command in (f"git -C {MAIN} merge --no-ff {BRANCH}", f"git -C {MAIN} merge --ff-only {BRANCH} && true",
                    f"git -C {MAIN} merge --ff-only nexus/wt/unknown"):
        assert engine.evaluate(_shell(command), spec).code != "worktree_merge"
    if mode == "allow":
        # an allow mode still forces the ask for git writes elsewhere
        assert engine.evaluate(_shell(f"git -C {MAIN} merge --no-ff {BRANCH}"), spec).outcome is Outcome.ASK


def test_explicit_deny_rule_still_wins_over_the_landing():
    from nexus.tools.builtin import shell

    engine = PermissionEngine(mode="allow", deny=("shell(git*)",))
    engine.allow_worktree_merge(POLICY)
    assert engine.evaluate(_shell(f"git -C {MAIN} merge --ff-only {BRANCH}"), shell.SPEC).outcome is Outcome.DENY
