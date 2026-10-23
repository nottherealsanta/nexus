"""Landing a session worktree with plain Git: the forced ask and merge detection."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.context.parts import render_session_worktree
from nexus.tools.permissions import Outcome, PermissionEngine
from nexus.tools.spec import ToolCall
from nexus.worktree_sessions import SessionWorktrees

from test_permissions import bash_spec
from test_session_integration import _controller, _git


def _verdict(mode: str, command: str):
    engine = PermissionEngine(mode=mode, allow=["Bash"])
    return engine.evaluate(ToolCall(id="1", name="Bash", input={"command": command}), bash_spec())


@pytest.mark.parametrize("mode", ["allow", "ask"])
@pytest.mark.parametrize("command", [
    "git -C /repo merge --ff-only nexus/session/x",
    "git -C '/my repo' merge --ff-only nexus/session/x",
    "cd /repo && git merge --ff-only nexus/session/x",
    "git --git-dir=/repo/.git --work-tree=/repo checkout main",
    "echo hi; git -c core.x=1 -C ../repo commit -m y",
])
def test_git_write_on_another_checkout_always_asks(mode, command):
    assert _verdict(mode, command).outcome is Outcome.ASK


def test_deny_rules_still_win_over_the_forced_ask():
    engine = PermissionEngine(mode="allow", deny=["Bash(git -C *)"])
    verdict = engine.evaluate(
        ToolCall(id="1", name="Bash", input={"command": "git -C /repo merge --ff-only x"}), bash_spec())
    assert verdict.outcome is Outcome.DENY


@pytest.mark.parametrize("command", [
    "git merge main", "git status", "git -C /repo status", "git -C /repo log -3", "cd sub && git status",
    "git commit -m x", "nexus sessions list",
])
def test_plain_and_read_only_git_is_not_forced(command):
    assert _verdict("allow", command).outcome is Outcome.ALLOW, command


def test_environment_block_teaches_the_git_landing_flow():
    text = render_session_worktree((
        ("Worktree directory", "/w/login"), ("Main checkout directory", "/repo"),
        ("Worktree branch", "nexus/session/login"), ("Base branch", "develop"), ("Base commit", "c" * 40)))
    for needle in ("git -C /repo merge --ff-only nexus/session/login", "git merge-base HEAD develop",
                   "git merge develop", "/merge", "git status"):
        assert needle in text, needle
    assert "nexus merge" not in text


def _landing_manager(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    manager = controller.runtime._session_worktrees
    sessions = SessionWorktrees(SimpleNamespace(_worktree_service=manager.service, _worktree_root=manager.registry))
    return project, record, sessions, SimpleNamespace(events=[]), controller.runtime.session_root("session")


def test_fresh_worktree_is_not_a_landing(tmp_path: Path):
    _project, _record, sessions, handle, root = _landing_manager(tmp_path)
    assert sessions._landing(handle, root) is None


def test_unlanded_commits_are_not_a_landing(tmp_path: Path):
    _project, record, sessions, handle, root = _landing_manager(tmp_path)
    (record.path / "a.txt").write_text("a\n")
    _git(record.path, "add", "a.txt")
    _git(record.path, "commit", "-m", "feat: a")
    assert sessions._landing(handle, root) is None


def test_ff_only_merge_into_main_is_detected_once(tmp_path: Path):
    project, record, sessions, handle, root = _landing_manager(tmp_path)
    base = _git(project, "rev-parse", "HEAD")
    for name in ("a.txt", "b.txt"):
        (record.path / name).write_text(name)
        _git(record.path, "add", name)
        _git(record.path, "commit", "-m", f"wip {name}")
    _git(record.path, "reset", "--soft", base)
    _git(record.path, "commit", "-m", "feat: add a and b", "-m", "body")
    _git(project, "merge", "--ff-only", record.branch)
    landed = _git(project, "rev-parse", "HEAD")

    payload = sessions._landing(handle, root)
    assert payload is not None
    assert payload["target_branch"] == "main" and payload["session_branch"] == record.branch
    assert payload["sha"] == landed and payload["before"] == base and payload["head"] == landed
    assert payload["subject"] == "feat: add a and b" and payload["commits"] == 1
    assert sorted(payload["files"]) == ["a.txt", "b.txt"] and payload["clipped"] is False

    handle.events.append(SimpleNamespace(type="worktree.merged", data=payload))
    assert sessions._landing(handle, root) is None
    # Later movement of the target does not repeat the event.
    (project / "later.txt").write_text("x")
    _git(project, "add", "later.txt")
    _git(project, "commit", "-m", "later")
    assert sessions._landing(handle, root) is None
