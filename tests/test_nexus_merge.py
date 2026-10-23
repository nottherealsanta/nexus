"""`nexus merge`: squash landing, dirty-target relaxation, forced ask and CLI wiring."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import cli
from nexus.context.parts import render_session_worktree
from nexus.session_integration import integration_preview, integrate_session, squash_merge_session
from nexus.tools.permissions import Outcome, PermissionEngine
from nexus.tools.spec import ToolCall

from test_permissions import bash_spec
from test_session_integration import _controller, _git


def _commit(path: Path, name: str, text: str, message: str = "change") -> None:
    (path / name).write_text(text)
    _git(path, "add", name)
    _git(path, "commit", "-m", message)


def test_squash_lands_one_commit_and_syncs_session_branch(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    base = _git(project, "rev-parse", "HEAD")
    _commit(record.path, "a.txt", "a\n", "first")
    _commit(record.path, "b.txt", "b\n", "second")
    head = _git(record.path, "rev-parse", "HEAD")
    # The turn owns the session lock; the in-turn entry point must not need it.
    controller.runtime.sessions.open("session").active = True
    result = squash_merge_session(controller, "session", "feat: add a and b\n\nbody\nNexus-Agent: forged",
                                  session_name="s", turn_seq=3)
    landed = _git(project, "rev-parse", "HEAD")
    assert result["kind"] == "merged" and result["commit"] == landed == result["after"]
    assert _git(project, "rev-parse", "HEAD^") == base == result["before"]
    assert _git(project, "rev-parse", "HEAD^{tree}") == _git(record.path, "rev-parse", f"{head}^{{tree}}")
    assert _git(project, "rev-list", "--count", f"{base}..HEAD") == "1"
    message = _git(project, "log", "-1", "--format=%B")
    assert message.startswith("feat: add a and b") and "forged" not in message
    assert "Nexus-Session: s (session)" in message and "Nexus-Squashed-Commits: 2" in message
    assert _git(record.path, "rev-parse", "HEAD") == landed and result["session_branch_moved"]
    assert _git(record.path, "status", "--porcelain") == ""
    assert {row["path"] for row in result["files"]} == {"a.txt", "b.txt"}
    with pytest.raises(ValueError, match="Nothing to merge"):
        squash_merge_session(controller, "session", "again")


def test_dirty_target_allowed_when_not_overlapping_and_kept(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    _commit(record.path, "a.txt", "a\n")
    (project / "base.txt").write_text("user edit\n")
    (project / "scratch.txt").write_text("untracked\n")
    squash_merge_session(controller, "session", "feat: a")
    assert (project / "a.txt").read_text() == "a\n"
    assert (project / "base.txt").read_text() == "user edit\n"
    assert (project / "scratch.txt").read_text() == "untracked\n"


def test_overlapping_dirty_target_refused_with_paths(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    _commit(record.path, "base.txt", "session\n")
    (project / "base.txt").write_text("user edit\n")
    with pytest.raises(ValueError, match="overlap.*base.txt"):
        squash_merge_session(controller, "session", "feat")
    assert "base.txt" in integration_preview(controller, "session")["reason"]
    (project / "base.txt").write_text("base\n")
    (project / "new").mkdir()
    (project / "new" / "x").write_text("x\n")
    _commit(record.path, "new", "file where the user has a directory\n")
    with pytest.raises(ValueError, match="overlap"):
        squash_merge_session(controller, "session", "feat")


def test_existing_fast_forward_accepts_non_overlapping_dirty_target(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    _commit(record.path, "a.txt", "a\n")
    (project / "base.txt").write_text("user edit\n")
    preview = integration_preview(controller, "session")
    assert preview["ready"] is True
    integrate_session(controller, "session", preview)
    assert (project / "base.txt").read_text() == "user edit\n"


def test_diverged_base_and_dirty_session_refused(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    _commit(record.path, "a.txt", "a\n")
    _commit(project, "other.txt", "o\n")
    with pytest.raises(ValueError, match="diverged"):
        squash_merge_session(controller, "session", "feat")
    _git(record.path, "merge", "--no-edit", "main")
    (record.path / "scratch.txt").write_text("uncommitted\n")
    with pytest.raises(ValueError, match="session changes"):
        squash_merge_session(controller, "session", "feat")
    (record.path / "scratch.txt").unlink()
    assert squash_merge_session(controller, "session", "feat")["kind"] == "merged"
    assert (project / "a.txt").exists() and (project / "other.txt").exists()


def test_session_for_checkout_resolves_from_worktree_subdirectory(tmp_path: Path):
    from nexus.session_integration import session_for_checkout
    _project, record, controller = _controller(tmp_path)
    (record.path / "sub").mkdir()
    assert session_for_checkout(controller, record.path / "sub") == "session"
    with pytest.raises(ValueError, match="not a Nexus session worktree"):
        session_for_checkout(controller, tmp_path)


@pytest.mark.parametrize("mode", ["allow", "ask", "deny"])
@pytest.mark.parametrize("command", [
    'nexus merge -m "done"', "git status && nexus merge -m x", "cd /tmp; nexus --workspace . merge -m x",
    "(nexus  merge --message x)",
])
def test_shell_nexus_merge_always_asks(mode, command):
    engine = PermissionEngine(mode=mode, allow=["Bash"])
    verdict = engine.evaluate(ToolCall(id="1", name="Bash", input={"command": command}), bash_spec())
    assert verdict.outcome is (Outcome.DENY if mode == "deny" else Outcome.ASK) or mode == "deny"
    if mode != "deny":
        assert verdict.outcome is Outcome.ASK


def test_other_shell_commands_are_not_forced_to_ask():
    engine = PermissionEngine(mode="allow")
    for command in ("git merge main", "echo nexus merged", "nexus sessions list"):
        verdict = engine.evaluate(ToolCall(id="1", name="Bash", input={"command": command}), bash_spec())
        assert verdict.outcome is Outcome.ALLOW, command


def test_cli_parser_and_labelled_output():
    args = cli.build_parser().parse_args(["merge", "-m", "feat: x", "--session", "abc"])
    assert args.command == "merge" and args.message == "feat: x" and args.merge_session == "abc"
    lines = cli.merge_lines({
        "target_branch": "main", "session_branch": "nexus/session/x", "before": "a" * 40, "after": "b" * 40,
        "commit": "b" * 40, "subject": "feat: x", "total_commits": 2, "total_files": 60,
        "files": [{"path": f"f{i}", "insertions": 1, "deletions": 0} for i in range(60)],
    })
    assert lines[0] == "merged into: main" and any(l.startswith("target: aaaaaaaaaaaa -> bbbbbbbbbbbb") for l in lines)
    assert any("10 more files not shown" in l for l in lines)


def test_environment_block_carries_merge_steps_with_real_values():
    text = render_session_worktree((
        ("Worktree directory", "/w/login"), ("Main checkout directory", "/repo"),
        ("Worktree branch", "nexus/session/login"), ("Base branch", "develop"), ("Base commit", "c" * 40)))
    for needle in ("branch nexus/session/login in /w/login", "based on develop at cccccccccccc",
                   "main checkout is /repo", "`nexus merge -m", "squashes the branch into one commit on develop",
                   "git merge develop", "/merge", "git status"):
        assert needle in text, needle
    assert "Do NOT merge" not in text
