from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.agents.worktrees import WorktreeService
from nexus.session.root import SessionRoot
from nexus.session.lock import SessionLock
from nexus.session_integration import integrate_session, integration_preview


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _controller(tmp_path: Path, *, ignore_local: bool = False):
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.invalid")
    (project / "base.txt").write_text("base\n")
    if ignore_local:
        (project / ".gitignore").write_text("local.env\n")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    registry = tmp_path / "service"
    service = WorktreeService()
    record = service.create_session(project, "session", "task", root=registry, base="main")
    record = service.activate_session(record.child_id, root=registry)
    handle = SimpleNamespace(_lock=SessionLock(tmp_path / "locks" / "session.lock"), active=False, queued_ids=[])
    root = SessionRoot("worktree", record.path, "test", branch=record.branch,
                       base_ref=record.base_ref, base_sha=record.base_commit,
                       worktree_id=record.child_id)
    manager = SimpleNamespace(service=service, registry=registry)
    runtime = SimpleNamespace(
        sessions=SimpleNamespace(open=lambda *_a, **_k: handle),
        session_root=lambda _session: root,
        validate_session_root=lambda _session: root,
        session_has_active_jobs=lambda _session: False,
        _session_worktrees=manager,
    )
    controller = SimpleNamespace(runtime=runtime, _idle=lambda _handle: None)
    return project, record, controller


def test_integrate_fast_forwards_only_target_branch(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    (record.path / "change.txt").write_text("session change\n")
    _git(record.path, "add", "change.txt")
    _git(record.path, "commit", "-m", "session change")

    preview = integration_preview(controller, "session")
    assert preview["ready"] is True
    assert preview["target_branch"] == "main"
    result = integrate_session(controller, "session", preview)

    assert result["kind"] == "integrated"
    assert _git(project, "rev-parse", "HEAD") == preview["session_head"]
    assert (project / "change.txt").read_text() == "session change\n"
    assert _git(record.path, "branch", "--show-current") == record.branch


def test_preview_refuses_dirty_target_and_wrong_branch(tmp_path: Path):
    project, _record, controller = _controller(tmp_path)
    (project / "base.txt").write_text("dirty\n")
    # A dirty target no longer blocks integration unless it overlaps the landed files.
    assert integration_preview(controller, "session")["ready"] is True
    _git(project, "checkout", "-f", "-b", "other")
    assert "Check out target branch" in integration_preview(controller, "session")["reason"]


def test_preview_refuses_dirty_session(tmp_path: Path):
    _project, record, controller = _controller(tmp_path)
    (record.path / "scratch.txt").write_text("uncommitted\n")
    assert "session changes" in integration_preview(controller, "session")["reason"]


def test_stale_preview_refuses_and_does_not_merge_new_head(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    (record.path / "first.txt").write_text("first\n")
    _git(record.path, "add", "first.txt")
    _git(record.path, "commit", "-m", "first")
    preview = integration_preview(controller, "session")
    (record.path / "later.txt").write_text("later\n")
    _git(record.path, "add", "later.txt")
    _git(record.path, "commit", "-m", "later")

    with pytest.raises(ValueError, match="changed since integration preview"):
        integrate_session(controller, "session", preview)
    assert _git(project, "rev-parse", "HEAD") == preview["target_head"]


def test_ff_merge_does_not_overwrite_ignored_target_file(tmp_path: Path):
    project, record, controller = _controller(tmp_path, ignore_local=True)
    (project / "local.env").write_text("keep me\n")
    (record.path / "local.env").write_text("session value\n")
    _git(record.path, "add", "-f", "local.env")
    _git(record.path, "commit", "-m", "track local env")
    preview = integration_preview(controller, "session")
    assert preview["ready"] is True

    with pytest.raises(Exception):
        integrate_session(controller, "session", preview)
    assert (project / "local.env").read_text() == "keep me\n"


def test_preview_refuses_divergence_and_git_operations(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    (record.path / "session.txt").write_text("session\n")
    _git(record.path, "add", "session.txt")
    _git(record.path, "commit", "-m", "session change")
    (project / "target.txt").write_text("target\n")
    _git(project, "add", "target.txt")
    _git(project, "commit", "-m", "target moved independently")
    before = _git(project, "rev-parse", "HEAD")
    preview = integration_preview(controller, "session")
    assert not preview["ready"] and "diverged" in preview["reason"]
    assert _git(project, "rev-parse", "HEAD") == before
    (project / ".git" / "MERGE_HEAD").write_text(before + "\n")
    preview = integration_preview(controller, "session")
    assert not preview["ready"] and "Git operation" in preview["reason"]


def test_preview_refuses_foreign_session_branch(tmp_path: Path):
    project, record, controller = _controller(tmp_path)
    _git(record.path, "switch", "-c", "user-branch")
    before = _git(project, "rev-parse", "HEAD")
    preview = integration_preview(controller, "session")
    assert not preview["ready"] and "owned branch" in preview["reason"]
    assert _git(project, "rev-parse", "HEAD") == before
