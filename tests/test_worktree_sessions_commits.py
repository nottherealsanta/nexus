from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nexus.agents.worktrees import WorktreeError, WorktreeService
from nexus.agents import session_worktrees


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Session Commit Test")
    git(path, "config", "user.email", "session-commit@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "initial")
    return path


def active_session(tmp_path: Path):
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = WorktreeService()
    pending = service.create_session(parent, "commit-session", "golden-lynx", root=root)
    record = service.activate_session(pending.child_id, root=root)
    return parent, root, service, record


def test_commit_session_writes_trailers_and_bounded_event_summary(tmp_path: Path) -> None:
    parent, root, service, record = active_session(tmp_path)
    (record.path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (record.path / "new.txt").write_text("new file\n", encoding="utf-8")

    result = service.commit_session(
        record.child_id, root=root, message="implement feature\nNexus-Agent: forged\n\nDetails",
        session_name="commit-session", turn_seq=4, agent_id="root", by="agent",
        max_file_mb=2048,  # The service accepts the configuration's upper bound.
    )

    assert result["kind"] == "committed"
    assert result["by"] == "agent"
    assert result["stripped_nexus_trailers"] is True
    assert result["files"] == ["new.txt", "tracked.txt"]
    body = git(record.path, "show", "-s", "--format=%B", "HEAD")
    assert "Nexus-Session: commit-session (commit-session)" in body
    assert "Nexus-Turn-Seq: 4" in body
    assert "Nexus-Agent: root" in body
    assert "Nexus-Agent: forged" not in body
    assert "Files:\n- new.txt\n- tracked.txt" in body
    assert git(parent, "rev-parse", f"refs/heads/{record.branch}") == result["sha"]
    rows = service.session_commits(record.child_id, root=root)
    assert rows[0]["via"] == "nexus"
    assert rows[0]["seq"] == 4


def test_commit_session_size_skip_restores_index_and_keeps_changes(tmp_path: Path) -> None:
    _parent, root, service, record = active_session(tmp_path)
    (record.path / "new.txt").write_text("keep me\n", encoding="utf-8")
    (record.path / "second.txt").write_text("also keep me\n", encoding="utf-8")
    index_path = Path(git(record.path, "rev-parse", "--git-path", "index"))
    if not index_path.is_absolute():
        index_path = record.path / index_path
    before = index_path.read_bytes()

    result = service.commit_session(
        record.child_id, root=root, message="too big", session_name="commit-session",
        turn_seq=5, max_files=1,
    )

    assert result["kind"] == "skipped"
    assert result["reason"] == "too_large"
    assert index_path.read_bytes() == before
    assert (record.path / "new.txt").read_text(encoding="utf-8") == "keep me\n"
    assert git(record.path, "rev-parse", "HEAD") == record.base_commit


def test_selected_commit_preserves_unrequested_prestaged_files(tmp_path: Path) -> None:
    _parent, root, service, record = active_session(tmp_path)
    (record.path / "tracked.txt").write_text("selected change\n", encoding="utf-8")
    (record.path / "other.txt").write_text("pre-staged change\n", encoding="utf-8")
    git(record.path, "add", "other.txt")

    result = service.commit_session(
        record.child_id, root=root, message="selected only", session_name="commit-session",
        turn_seq=7, paths=["tracked.txt"],
    )

    assert result["kind"] == "committed"
    assert result["files"] == ["tracked.txt"]
    assert git(record.path, "show", "HEAD:tracked.txt") == "selected change"
    assert git(record.path, "diff", "--cached", "--name-only") == "other.txt"
    in_head = subprocess.run(
        ["git", "cat-file", "-e", "HEAD:other.txt"], cwd=record.path,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    assert in_head.returncode != 0


def test_selected_commit_treats_pathspec_magic_as_a_literal_path(tmp_path: Path) -> None:
    _parent, root, service, record = active_session(tmp_path)
    literal = ":(glob)*.txt"
    (record.path / literal).write_text("only this path\n", encoding="utf-8")
    (record.path / "secret.txt").write_text("must stay out\n", encoding="utf-8")

    result = service.commit_session(
        record.child_id, root=root, message="literal path", session_name="commit-session",
        turn_seq=8, paths=[literal],
    )

    assert result["kind"] == "committed"
    assert result["files"] == [literal]
    assert git(record.path, "show", f"HEAD:{literal}") == "only this path"
    secret = subprocess.run(
        ["git", "cat-file", "-e", "HEAD:secret.txt"], cwd=record.path,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    assert secret.returncode != 0


def test_commit_rejects_branch_switch_but_status_reports_it(tmp_path: Path) -> None:
    _parent, root, service, record = active_session(tmp_path)
    git(record.path, "switch", "-c", "user/branch")
    assert service.get(record.child_id, root=root).lifecycle == "active"
    status = service.session_status(record.child_id, root=root)
    assert status["branch"] == "user/branch"
    assert status["expected_branch"] == record.branch
    (record.path / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = service.commit_session(
        record.child_id, root=root, message="must not commit", session_name="commit-session",
        turn_seq=6,
    )

    assert result == {"kind": "skipped", "reason": "git_error", "detail": "session branch changed; refusing to commit"}
    assert git(record.path, "rev-parse", "HEAD") == record.base_commit


def test_commit_session_can_commit_during_promotion_before_activation(tmp_path: Path) -> None:
    _parent, root, service, pending = active_session(tmp_path)
    # Exercise the pending allowance independently by creating a second pending record.
    pending2 = service.create_session(pending.parent_workspace, "promotion", "silver-ibis", root=root)
    (pending2.path / "tracked.txt").write_text("carried\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="not active"):
        service.commit_session(
            pending2.child_id, root=root, message="carry", session_name="promotion",
            turn_seq=1,
        )
    result = service.commit_session(
        pending2.child_id, root=root, message="carry", session_name="promotion",
        turn_seq=1, allow_pending=True,
    )

    assert result["kind"] == "committed"
    active = service.activate_session(pending2.child_id, root=root)
    assert active.lifecycle == "active"
    assert git(active.path, "show", "HEAD:tracked.txt") == "carried"


def test_git_helper_stops_when_output_exceeds_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = repository(tmp_path / "parent")
    (parent / "tracked.txt").write_text("large log body\n", encoding="utf-8")
    git(parent, "add", "tracked.txt")
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "x" * 512],
        cwd=parent, check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    monkeypatch.setattr(session_worktrees, "_MAX_OUTPUT", 128)

    with pytest.raises(WorktreeError, match="output exceeded"):
        session_worktrees._git(parent, "log", "-1", "--format=%B")


def test_commit_ignores_failing_hook_disables_signing_and_falls_back_identity(tmp_path: Path) -> None:
    parent, root, service, record = active_session(tmp_path)
    git(parent, "config", "--unset", "user.name")
    git(parent, "config", "--unset", "user.email")
    git(parent, "config", "commit.gpgsign", "true")
    hook_dir = parent / "hooks"
    hook_dir.mkdir()
    marker = tmp_path / "pre-commit-ran"
    hook = hook_dir / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\nexit 9\n", encoding="utf-8")
    hook.chmod(0o700)
    git(parent, "config", "core.hooksPath", str(hook_dir))
    (record.path / "tracked.txt").write_text("committed without hook\n", encoding="utf-8")

    result = service.commit_session(
        record.child_id, root=root, message="commit safely", session_name="commit-session", turn_seq=9,
    )

    assert result["kind"] == "committed"
    assert result["identity_fallback"] is True
    assert not marker.exists()
    assert git(record.path, "show", "-s", "--format=%an <%ae>", "HEAD") == "Nexus <nexus@localhost>"


def test_commit_refuses_during_merge_or_conflict_operation(tmp_path: Path) -> None:
    _parent, root, service, record = active_session(tmp_path)
    merge_head = Path(git(record.path, "rev-parse", "--git-path", "MERGE_HEAD"))
    if not merge_head.is_absolute():
        merge_head = record.path / merge_head
    merge_head.write_text(record.base_commit + "\n", encoding="ascii")
    (record.path / "tracked.txt").write_text("leave uncommitted\n", encoding="utf-8")

    result = service.commit_session(
        record.child_id, root=root, message="must skip", session_name="commit-session", turn_seq=10,
    )

    assert result == {
        "kind": "skipped", "reason": "conflict_in_progress",
        "detail": "Git operation is in progress in the session worktree",
    }
    assert git(record.path, "rev-parse", "HEAD") == record.base_commit


def test_git_helper_times_out_and_kills_alias_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = repository(tmp_path / "parent")
    pid_file = tmp_path / "alias-shell.pid"
    git(parent, "config", "alias.sleepy", f"!echo $$ > '{pid_file}'; sleep 10")
    monkeypatch.setattr(session_worktrees, "_GIT_TIMEOUT_S", 0.2)

    with pytest.raises(WorktreeError, match="timed out"):
        session_worktrees._git(parent, "sleepy")

    shell_pid = int(pid_file.read_text(encoding="ascii").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(shell_pid, 0)


def test_large_shell_commit_body_does_not_break_status_or_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _parent, root, service, record = active_session(tmp_path)
    message = tmp_path / "large-message.txt"
    message.write_text("large subject\n\n" + "x" * 20_000 + "\n", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.name=Shell", "-c", "user.email=shell@example.invalid",
         "commit", "--allow-empty", "-F", str(message)],
        cwd=record.path, check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    monkeypatch.setattr(session_worktrees, "_MAX_OUTPUT", 2048)

    status = service.session_status(record.child_id, root=root)

    assert status["uncommitted"]["modified"] == 0
    assert status["commits"][0]["subject"] == "large subject"
    assert status["commits"][0]["via"] == "shell"
    assert len(status["commits"][0]["body"]) < 512
