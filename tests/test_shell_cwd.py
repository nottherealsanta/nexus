"""The persistent per-session working directory (SHELL_PLAN P3)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.model.message import Text
from nexus.tools.builtin import SHELL_SPEC, _jobs, _shell_logs, shell
from nexus.tools.spec import ToolContext


def body(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, Text))


def make_ctx(workspace: Path, session_id: str = "cwd-session") -> ToolContext:
    return ToolContext(workspace=workspace, session_id=session_id, turn_id="t", config=Config())


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "ws"
    root.mkdir()
    return root


@pytest.fixture
async def registry():
    value = _jobs.JobRegistry()
    _jobs.set_default_registry(value)
    try:
        yield value
    finally:
        await value.aclose()
        _jobs.set_default_registry(None)


async def pwd(ctx, **extra) -> str:
    result = await shell.run({"command": "pwd", **extra}, ctx)
    assert result.is_error is False, body(result)
    return re.search(r"stdout:\n(\S+)", body(result)).group(1)


async def cd(ctx, path: str):
    return await shell.run({"action": "cd", "path": path}, ctx)


async def test_cd_persists_across_calls_and_a_reopen(registry, workspace):
    (workspace / "packages" / "api").mkdir(parents=True)
    ctx = make_ctx(workspace)
    assert await pwd(ctx) == str(workspace)
    moved = await cd(ctx, "packages/api")
    assert moved.is_error is False
    assert "cwd: packages/api" in body(moved) and "previous: ." in body(moved)
    assert await pwd(ctx) == str(workspace / "packages" / "api")
    assert await pwd(ctx) == str(workspace / "packages" / "api")

    # A new registry (a restarted daemon / reopened session) reads the same record.
    reopened = _jobs.JobRegistry()
    try:
        ctx2 = ToolContext(
            workspace=workspace, session_id="cwd-session", turn_id="t2",
            config=Config(), job_registry=reopened,
        )
        assert await pwd(ctx2) == str(workspace / "packages" / "api")
    finally:
        await reopened.aclose()


async def test_relative_cwd_resolves_against_the_session_cwd(registry, workspace):
    (workspace / "a" / "b").mkdir(parents=True)
    ctx = make_ctx(workspace)
    await cd(ctx, "a")
    assert await pwd(ctx, cwd="b") == str(workspace / "a" / "b")
    # Absolute paths inside the workspace ignore the session cwd.
    assert await pwd(ctx, cwd=str(workspace)) == str(workspace)
    # And a relative cd moves from the session cwd, not from the root.
    moved = await cd(ctx, "b")
    assert "cwd: a/b" in body(moved)
    assert await pwd(ctx) == str(workspace / "a" / "b")


async def test_cd_inside_a_command_does_not_persist(registry, workspace):
    (workspace / "sub").mkdir()
    ctx = make_ctx(workspace)
    inner = await shell.run({"command": "cd sub && pwd"}, ctx)
    assert str(workspace / "sub") in body(inner)
    assert await pwd(ctx) == str(workspace)


@pytest.mark.parametrize(
    "target", ["..", "../x", "~", "~/x", "/etc", "missing", "file.txt", "", "  "]
)
async def test_cd_rejects_escapes_home_missing_and_non_directories(registry, workspace, target):
    (workspace / "file.txt").write_text("x")
    ctx = make_ctx(workspace)
    result = await cd(ctx, target)
    assert result.is_error is True
    assert await pwd(ctx) == str(workspace)  # nothing changed
    assert _shell_logs.read_session_cwd(workspace, ctx.session_id) is None


async def test_cd_rejects_symlink_escape(registry, workspace, tmp_path):
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    ctx = make_ctx(workspace)
    assert (await cd(ctx, "link")).is_error is True
    escaped = await shell.run({"command": "touch should-not-exist", "cwd": "link"}, ctx)
    assert escaped.is_error is True and not (outside / "should-not-exist").exists()


async def test_run_cwd_rejects_home_expansion_and_escape(registry, workspace):
    ctx = make_ctx(workspace)
    for bad in ("~", "../", "/"):
        result = await shell.run({"command": "touch nope", "cwd": bad}, ctx)
        assert result.is_error is True, bad
    assert registry.jobs(session_id=ctx.session_id) == ()


async def test_a_deleted_session_cwd_gives_a_named_error_and_cd_dot_resets(registry, workspace):
    (workspace / "gone").mkdir()
    ctx = make_ctx(workspace)
    await cd(ctx, "gone")
    (workspace / "gone").rmdir()
    result = await shell.run({"command": "pwd"}, ctx)
    text = body(result)
    assert result.is_error is True
    assert str(workspace / "gone") in text and "action=cd" in text
    assert registry.jobs(session_id=ctx.session_id) == ()  # nothing ran, no fallback

    reset = await cd(ctx, ".")
    assert reset.is_error is False and "cwd: ." in body(reset)
    assert await pwd(ctx) == str(workspace)


async def test_session_cwd_that_left_the_workspace_is_refused(registry, workspace, tmp_path):
    ctx = make_ctx(workspace)
    outside = tmp_path.resolve() / "elsewhere"
    outside.mkdir()
    _shell_logs.write_session_cwd(workspace, ctx.session_id, str(outside))
    result = await shell.run({"command": "pwd"}, ctx)
    assert result.is_error is True and "elsewhere" in body(result)


async def test_each_session_has_its_own_cwd_and_a_child_starts_at_its_root(
    registry, workspace, tmp_path
):
    (workspace / "deep").mkdir()
    parent = make_ctx(workspace, "parent")
    await cd(parent, "deep")
    sibling = make_ctx(workspace, "parent/sub/1")
    assert await pwd(sibling) == str(workspace)
    # A child in its own workspace (a worktree) starts at that workspace's root.
    child_root = tmp_path.resolve() / "worktree"
    child_root.mkdir()
    child = make_ctx(child_root, "parent/sub/2")
    assert await pwd(child) == str(child_root)
    assert await pwd(parent) == str(workspace / "deep")


async def test_cd_requires_a_path_and_no_other_fields(registry, workspace):
    ctx = make_ctx(workspace)
    for args in (
        {"action": "cd"},
        {"action": "cd", "path": "a", "cwd": "b"},
        {"action": "cd", "path": "a", "command": "ls"},
        {"action": "run", "command": "ls", "path": "a"},
    ):
        assert (await shell.run(args, ctx)).is_error is True, args


def test_cd_permission_key_is_action_qualified():
    key = SHELL_SPEC.resolve_permission_key
    assert key({"action": "cd", "path": "src"}) == "cd:src"
    assert key({"action": "log", "job_id": "job_x"}) == "log:job_x"
    assert key({"command": "cd src"}) == "cd src"  # a command named cd is still a command


def test_session_directory_names_are_safe(tmp_path):
    odd = _shell_logs.session_dir(tmp_path, "a/b/../../c")
    assert odd.parent == _shell_logs.shell_root(tmp_path) and ".." not in odd.name
    _shell_logs.write_session_cwd(tmp_path, "a/b/../../c", str(tmp_path))
    assert _shell_logs.read_session_cwd(tmp_path, "a/b/../../c") == str(tmp_path)
    assert (odd / "cwd").stat().st_mode & 0o777 == 0o600
