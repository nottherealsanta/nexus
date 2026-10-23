from __future__ import annotations

import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor
import subprocess
from pathlib import Path
from types import SimpleNamespace

from nexus.config.schema import WorktreesSection
from nexus.worktree_sessions import SessionWorktrees


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, text=True, capture_output=True).stdout.strip()


def repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "tracked.txt").write_text("base\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "base")
    return path


class FakeRuntime:
    def __init__(self, path: Path, *, config: WorktreesSection | None = None):
        self.workspace = path
        self.config = config or WorktreesSection()

    def _load_config(self):
        return SimpleNamespace(v2=SimpleNamespace(worktrees=self.config))

    def session_root(self, _session):
        return SimpleNamespace(kind="main", path=self.workspace)


class FakeHandle:
    def __init__(self):
        self.id = "session-runtime"
        self.active_turn_id = "turn-a"
        self.events = []

    def read(self, **_kwargs):
        return SimpleNamespace(records=[])

    def append_event(self, event):
        self.events.append(event)
        return SimpleNamespace(event=event)

    def _observe_event(self, _event):
        pass

    def _publish(self, _event):
        pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def test_checkpoint_uses_private_index_and_hidden_ref_without_changing_checkout(tmp_path):
    path = repo(tmp_path / "project")
    controller = SessionWorktrees(FakeRuntime(path))
    handle = FakeHandle()
    controller._checkpoint_baseline(path, "session-a", 10)
    (path / "tracked.txt").write_text("changed\n")
    (path / "new file.txt").write_text("untracked\n")

    before_head = git(path, "rev-parse", "HEAD")
    before_index = digest(path / ".git" / "index")
    before_status = git(path, "status", "--porcelain=v1", "-z")
    result = controller._checkpoint_capture(path, "session-a", 10, handle)

    assert result["kind"] == "captured"
    event = result["event"]
    assert event["head"] == before_head
    assert event["files"] == []
    assert event["foreign"] == 2
    assert event["ref"] == "refs/nexus/checkpoints/session-a/0000000010"
    assert git(path, "rev-parse", "HEAD") == before_head
    assert digest(path / ".git" / "index") == before_index
    assert git(path, "status", "--porcelain=v1", "-z") == before_status
    assert git(path, "rev-parse", event["ref"]) == event["commit"]


def test_checkpoint_ignores_unchanged_turn_and_supports_unborn_head(tmp_path):
    path = tmp_path / "unborn"
    path.mkdir()
    git(path, "init", "-q")
    controller = SessionWorktrees(FakeRuntime(path))
    handle = FakeHandle()
    controller._checkpoint_baseline(path, "session-b", 1)
    unchanged = controller._checkpoint_capture(path, "session-b", 1, handle)
    assert unchanged == {"kind": "skipped", "event": {"seq": 1, "reason": "no_changes", "detail": ""}}

    controller._checkpoint_baseline(path, "session-b", 2)
    (path / "new.txt").write_text("new\n")
    captured = controller._checkpoint_capture(path, "session-b", 2, handle)
    assert captured["kind"] == "captured"
    assert captured["event"]["head"] == ""
    assert git(path, "cat-file", "-p", captured["event"]["commit"]).startswith("tree ")


def test_checkpoint_size_guard_does_not_write_checkpoint_ref(tmp_path):
    path = repo(tmp_path / "large")
    config = WorktreesSection(commit_max_files=1)
    controller = SessionWorktrees(FakeRuntime(path, config=config))
    controller._checkpoint_baseline(path, "session-c", 1)
    (path / "a.txt").write_text("a")
    (path / "b.txt").write_text("b")
    result = controller._checkpoint_capture(path, "session-c", 1, FakeHandle())
    assert result["kind"] == "skipped"
    assert result["event"]["reason"] == "too_large"
    ref = subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/nexus/checkpoints/session-c/0000000001"], cwd=path)
    assert ref.returncode == 1


def test_main_session_turn_hooks_capture_first_turn_diff(tmp_path):
    path = repo(tmp_path / "runtime")
    controller = SessionWorktrees(FakeRuntime(path))
    handle = FakeHandle()
    asyncio.run(controller.prepare(handle, SimpleNamespace(turn_id="turn-a"), "prompt"))
    (path / "tracked.txt").write_text("written during turn\n")
    handle.events.append(SimpleNamespace(type="worktree.file_written", turn="turn-a", data={"path": "tracked.txt"}))
    asyncio.run(controller.finish(handle, SimpleNamespace(turn="turn-a")))

    started = next(event for event in handle.events if event.type == "checkpoint.started")
    captured = next(event for event in handle.events if event.type == "checkpoint.captured")
    assert started.data["head"] == git(path, "rev-parse", "HEAD")
    assert captured.data["files"] == ["tracked.txt"]
    assert captured.data["foreign"] == 0
    assert captured.data["head_moved"] is False


def test_main_session_checkpoint_captures_unreceipted_external_write(tmp_path):
    path = repo(tmp_path / "external-write")
    controller = SessionWorktrees(FakeRuntime(path))
    handle = FakeHandle()
    asyncio.run(controller.prepare(handle, SimpleNamespace(turn_id="turn-a"), "prompt"))
    (path / "tracked.txt").write_text("external MCP write\n")
    before_head = git(path, "rev-parse", "HEAD")
    before_index = digest(path / ".git" / "index")

    asyncio.run(controller.finish(handle, SimpleNamespace(turn="turn-a")))

    captured = next(event for event in handle.events if event.type == "checkpoint.captured")
    assert captured.data["foreign"] == 1
    assert captured.data["files"] == []
    assert git(path, "show", captured.data["commit"] + ":tracked.txt") == "external MCP write"
    assert git(path, "rev-parse", "HEAD") == before_head
    assert digest(path / ".git" / "index") == before_index

    asyncio.run(controller.prepare(handle, SimpleNamespace(turn_id="turn-b"), "followup"))
    asyncio.run(controller.finish(handle, SimpleNamespace(turn="turn-b")))
    skipped = [event for event in handle.events if event.type == "checkpoint.skipped"]
    assert skipped[-1].data["reason"] == "no_changes"


def test_checkpoint_attributes_successful_file_tool_paths_and_head_moves(tmp_path):
    path = repo(tmp_path / "head-moved")
    controller = SessionWorktrees(FakeRuntime(path))
    handle = FakeHandle()
    asyncio.run(controller.prepare(handle, SimpleNamespace(turn_id="turn-a"), "prompt"))
    (path / "tracked.txt").write_text("committed during turn\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "user commit during turn")
    (path / "owned.txt").write_text("tool write\n")
    handle.events.append(SimpleNamespace(type="worktree.file_written", turn="turn-a", data={"path": "owned.txt"}))
    asyncio.run(controller.finish(handle, SimpleNamespace(turn="turn-a")))
    captured = next(event for event in handle.events if event.type == "checkpoint.captured")
    assert captured.data["head_moved"] is True
    assert captured.data["files"] == ["owned.txt"]
    assert captured.data["foreign"] == 1


def test_concurrent_sessions_have_independent_checkpoint_refs(tmp_path):
    path = repo(tmp_path / "concurrent")
    controller = SessionWorktrees(FakeRuntime(path))
    controller._checkpoint_baseline(path, "session-one", 1)
    controller._checkpoint_baseline(path, "session-two", 1)
    (path / "tracked.txt").write_text("shared dirty checkout\n")
    index_before = digest(path / ".git" / "index")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda session: controller._checkpoint_capture(path, session, 1, FakeHandle()),
                                ("session-one", "session-two")))
    assert [result["kind"] for result in results] == ["captured", "captured"]
    assert git(path, "rev-parse", "refs/nexus/checkpoints/session-one/0000000001")
    assert git(path, "rev-parse", "refs/nexus/checkpoints/session-two/0000000001")
    assert digest(path / ".git" / "index") == index_before
