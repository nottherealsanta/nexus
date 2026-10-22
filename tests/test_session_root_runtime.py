from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection
from nexus.errors import SessionBusy
from nexus.model.message import Message, Text
from nexus.runtime import Runtime
from nexus.session.root import SessionRoot
from nexus.tools.permissions import PathSecurityError


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "nested" / "repo"
    project.mkdir(parents=True)
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Nexus Tests")
    _git(project, "config", "user.email", "nexus-tests@example.invalid")
    (project / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(project, "add", "tracked.txt")
    _git(project, "commit", "-m", "initial")
    linked_a = tmp_path / "linked-a"
    linked_b = tmp_path / "linked-b"
    _git(project, "worktree", "add", "-b", "feature-a", str(linked_a), "HEAD")
    _git(project, "worktree", "add", "-b", "feature-b", str(linked_b), "HEAD")
    return project, linked_a, linked_b


def _runtime(project: Path, home: Path) -> Runtime:
    config = Config(
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="ask"),
        ),
    )
    return Runtime(project, home=home, config=config)


@pytest.mark.asyncio
async def test_runtime_roots_are_independent_linked_worktrees(tmp_path: Path) -> None:
    project, linked_a, linked_b = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "home")
    try:
        runtime.session("a")
        runtime.session("b")
        root_a = runtime.set_session_root("a", linked_a, branch="feature-a")
        root_b = runtime.set_session_root("b", linked_b, branch="feature-b")

        assert runtime.validate_session_root("a") == root_a
        assert runtime.validate_session_root("b") == root_b
        assert root_a.path != root_b.path
        assert root_a.path == linked_a.resolve()
        assert root_b.path == linked_b.resolve()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_root_event_replays_after_runtime_reopen(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    home = tmp_path / "home"
    runtime = _runtime(project, home)
    try:
        runtime.session("durable").append_message(
            Message(role="user", content=[Text(text="Create the session row")])
        )
        root = runtime.set_session_root("durable", linked_a, branch="feature-a")
        event = next(e for e in runtime.session("durable").events if e.type == "session.root_set")
        assert event.data["path_display"] == "../linked-a"
    finally:
        await runtime.aclose()

    reopened = _runtime(project, home)
    try:
        assert reopened.session_root("durable") == root
        assert reopened.validate_session_root("durable").path == linked_a.resolve()
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_foreign_repository_and_missing_linked_root_are_refused(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-b", "main")
    runtime = _runtime(project, tmp_path / "home")
    try:
        runtime.session("foreign")
        with pytest.raises(ValueError, match="linked worktree of this project"):
            runtime.set_session_root("foreign", foreign)

        runtime.session("missing")
        runtime.set_session_root("missing", linked_a, branch="feature-a")
        shutil.rmtree(linked_a)
        with pytest.raises(ValueError, match="no longer exists"):
            runtime.validate_session_root("missing")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_worktree_gitdir_backlink_rejects_a_spoofed_checkout(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    spoof = tmp_path / "spoof"
    spoof.mkdir()
    (spoof / ".git").write_text((linked_a / ".git").read_text(encoding="utf-8"), encoding="utf-8")
    runtime = _runtime(project, tmp_path / "home")
    try:
        handle = runtime.session("spoof")
        handle.append_message(
            Message(role="user", content=[Text(text="Persist spoof test session")])
        )
        with pytest.raises(ValueError):
            runtime.set_session_root("spoof", spoof)

        forged = SessionRoot(
            "external", spoof.resolve(), SessionRoot.main(project).project_key
        )
        handle.emit_session_event("session.root_set", forged.to_data(project))
        with pytest.raises(ValueError):
            runtime.validate_session_root("spoof")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_fork_replays_the_inherited_root_event(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "home")
    try:
        runtime.session("parent").append_message(
            Message(role="user", content=[Text(text="Fork with root metadata")])
        )
        parent_root = runtime.set_session_root("parent", linked_a, branch="feature-a")

        runtime.sessions.fork("parent", new_id="child")

        assert runtime.session_root("child") == parent_root
        assert any(event.type == "session.root_set" for event in runtime.session("child").events)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_root_cache_refreshes_after_another_runtime_changes_the_root(tmp_path: Path) -> None:
    project, linked_a, linked_b = _project(tmp_path)
    home = tmp_path / "home"
    first = _runtime(project, home)
    second = _runtime(project, home)
    try:
        first.session("shared").append_message(
            Message(role="user", content=[Text(text="Persist shared session")])
        )
        root_a = first.set_session_root("shared", linked_a, branch="feature-a")
        assert first.session_root("shared") == root_a

        root_b = second.set_session_root("shared", linked_b, branch="feature-b")

        assert first.session_root("shared") == root_b
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_worktree_guard_allows_session_writes_and_denies_project_writes(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "home")
    try:
        runtime.session("guarded")
        runtime.set_session_root("guarded", linked_a, branch="feature-a")
        guard = runtime.session_path_guard("guarded")

        assert guard.resolve("session-write.txt", for_write=True).absolute == linked_a / "session-write.txt"
        with pytest.raises(PathSecurityError):
            guard.resolve(str(project / "project-write.txt"), for_write=True)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_root_change_is_refused_with_active_turn_or_queued_input(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "home")
    try:
        active = runtime.session("active")
        active._active = object()
        with pytest.raises(SessionBusy, match="turn boundary"):
            runtime.set_session_root("active", linked_a, branch="feature-a")
        active._active = None

        queued = runtime.session("queued")
        queued.enqueue("pending input")
        with pytest.raises(SessionBusy, match="queued inputs"):
            runtime.set_session_root("queued", linked_a, branch="feature-a")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_root_change_observes_input_queued_by_another_runtime(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    home = tmp_path / "home"
    first = _runtime(project, home)
    second = _runtime(project, home)
    try:
        first.session("shared").append_message(
            Message(role="user", content=[Text(text="Persist shared session")])
        )
        second.session("shared").enqueue("queued by second runtime")

        with pytest.raises(SessionBusy, match="queued inputs"):
            first.set_session_root("shared", linked_a, branch="feature-a")

        assert first.session_root("shared") == SessionRoot.main(project)
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_root_change_survives_shell_cwd_persistence_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, linked_a, _ = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "home")
    try:
        handle = runtime.session("cwd-failure")

        def fail_write(*_args, **_kwargs):
            raise OSError("state directory unavailable")

        monkeypatch.setattr(
            "nexus.tools.builtin._shell_logs.write_session_cwd", fail_write
        )

        selected = runtime.set_session_root(
            "cwd-failure", linked_a, branch="feature-a"
        )

        assert selected.path == linked_a.resolve()
        assert runtime.session_root("cwd-failure") == selected
        notices = [
            event.data for event in handle.events
            if event.type == "context.notice"
            and event.data.get("kind") == "session_root_cwd"
        ]
        assert notices
        assert "shell cwd could not be reset" in notices[-1]["message"]
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_default_root_matches_session_root_factory(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    runtime = _runtime(project, tmp_path / "home")
    try:
        runtime.session("main")
        assert runtime.session_root("main") == SessionRoot.main(project)
    finally:
        await runtime.aclose()
