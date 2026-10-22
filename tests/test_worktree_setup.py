"""Real Runtime coverage for isolated worktree setup and trust boundaries."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_worktree_sessions_runtime import (
    drain as _drain_turn,
    runtime as _make_runtime,
)
from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection, WorktreesSection
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.tools.permissions import Decision


pytest_plugins = ("tests.test_worktree_sessions_runtime",)


def _setup_runtime(project: Path, tmp_path: Path, *, provider=None, **policy):
    return _make_runtime(
        project,
        tmp_path,
        provider or ScriptedProvider(text_response("done"), text_response("done")),
        sessions=True,
        **policy,
    )


async def _run(runtime, session: str, prompt: str = "prepare"):
    handle = runtime.session(session)
    events = await _drain_turn(handle, prompt)
    return handle, events


async def test_project_setup_is_skipped_until_explicit_approval(project, tmp_path):
    runtime = _setup_runtime(project, tmp_path, setup=[sys.executable, "-c", "print('should-not-run')"])
    try:
        handle, events = await _run(runtime, "untrusted-setup")
        assert events[-1].type == "turn.completed", events[-1]
        setup = next(event for event in handle.events if event.type == "worktree.setup_ran")
        assert setup.data["skipped"] == "approval_required"
        assert not any("should-not-run" in event.data.get("text", "") for event in handle.events)
        assert any("pending approval" in event.data.get("message", "") for event in handle.events)
    finally:
        await runtime.aclose()


async def test_allow_once_authorizes_one_setup_only(project, tmp_path):
    runtime = _setup_runtime(project, tmp_path, setup=[sys.executable, "-c", "print('approved-once')"])
    try:
        first = runtime.session("once-first")
        runtime.approve_worktree_setup(first.id, "allow_once")
        first_events = await _drain_turn(first, "first")
        assert first_events[-1].type == "turn.completed", first_events[-1]
        first_setup = next(event for event in first.events if event.type == "worktree.setup_ran")
        assert first_setup.data["exit"] == 0
        assert "approved-once" in "".join(
            event.data.get("text", "") for event in first.events if event.type == "worktree.setup_output"
        )

        second, second_events = await _run(runtime, "once-second")
        assert second_events[-1].type == "turn.completed", second_events[-1]
        second_setup = next(event for event in second.events if event.type == "worktree.setup_ran")
        assert second_setup.data["skipped"] == "approval_required"
    finally:
        await runtime.aclose()


async def test_setup_allow_once_for_one_session_cannot_be_consumed_by_another(project, tmp_path):
    runtime = _setup_runtime(project, tmp_path, setup=[sys.executable, "-c", "print('session-setup')"])
    try:
        session_a = runtime.session("setup-a")
        session_b = runtime.session("setup-b")
        digest = runtime.session_worktree_status(session_a.id)["setup_digest"]
        runtime.approve_worktree_setup(session_a.id, "allow_once", digest=digest)

        events_b = await _drain_turn(session_b, "session B first")
        assert events_b[-1].type == "turn.completed", events_b[-1]
        setup_b = next(event for event in session_b.events if event.type == "worktree.setup_ran")
        assert setup_b.data["skipped"] == "approval_required"

        events_a = await _drain_turn(session_a, "session A second")
        assert events_a[-1].type == "turn.completed", events_a[-1]
        setup_a = next(event for event in session_a.events if event.type == "worktree.setup_ran")
        assert setup_a.data["exit"] == 0
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    ("decision", "runs_setup", "persists_trust"),
    [
        (Decision.ALLOW_ONCE, True, False),
        (Decision.ALLOW_ALWAYS, True, True),
        (Decision.DENY_ONCE, False, False),
    ],
)
async def test_attended_setup_uses_permission_request_for_trust(
    project, tmp_path, decision, runs_setup, persists_trust
):
    runtime = _setup_runtime(project, tmp_path, setup=[sys.executable, "-c", "print('permission-approved')"])
    try:
        handle = runtime.session(f"permission-{decision.value}").mark_attended()
        setup_digest = runtime.session_worktree_status(handle.id)["setup_digest"]
        events = []
        approved_prompt = False
        async for event in handle.send("run worktree setup", attended=True):
            events.append(event)
            if event.type == "permission.requested":
                assert event.data["tool"] == "worktree_setup"
                assert event.data["key"] == setup_digest
                assert handle.resolve_permission(event.data["id"], decision)
                approved_prompt = True

        assert approved_prompt
        assert events[-1].type == "turn.completed", events[-1]
        setup = next(event for event in handle.events if event.type == "worktree.setup_ran")
        if runs_setup:
            assert setup.data["exit"] == 0
            assert "permission-approved" in "".join(
                event.data.get("text", "")
                for event in handle.events
                if event.type == "worktree.setup_output"
            )
        else:
            assert setup.data["skipped"] == "approval_required"
            assert not any(event.type == "worktree.setup_trust" for event in handle.events)

        trust_file = runtime._session_worktrees._trust_path()
        if persists_trust:
            import json

            stored = json.loads(trust_file.read_text())
            assert setup_digest in stored["approved"]
        elif runs_setup:
            assert not trust_file.exists()
    finally:
        await runtime.aclose()


async def test_project_approval_persists_for_matching_digest_only(project, tmp_path):
    runtime = _setup_runtime(project, tmp_path, setup=[sys.executable, "-c", "print('project-approved')"])
    try:
        first = runtime.session("project-approved")
        runtime.approve_worktree_setup(first.id, "allow_project")
        _, events = await _run(runtime, first.id)
        assert events[-1].type == "turn.completed", events[-1]
        assert next(event for event in first.events if event.type == "worktree.setup_ran").data["exit"] == 0

        # A changed command has a different trust digest, so the old decision
        # cannot authorize it.
        runtime._config = Config(
            model="scripted/m",
            version=2,
            v2=ConfigV2(
                model=ModelSection(default="scripted/m"),
                permissions=PermissionsSection(mode="allow"),
                worktrees=WorktreesSection(
                    sessions=True,
                    setup=[sys.executable, "-c", "print('changed-command')"],
                ),
            ),
        )
        second, second_events = await _run(runtime, "changed-command")
        assert second_events[-1].type == "turn.completed", second_events[-1]
        second_setup = next(event for event in second.events if event.type == "worktree.setup_ran")
        assert second_setup.data["skipped"] == "approval_required"
    finally:
        await runtime.aclose()


async def test_setup_trust_rejects_a_stale_review_digest(project, tmp_path):
    runtime = _setup_runtime(project, tmp_path, setup=[sys.executable, "-c", "print('reviewed')"])
    try:
        handle = runtime.session("stale-review")
        preview = runtime.session_worktree_status(handle.id)
        reviewed_digest = preview["setup_digest"]
        assert preview["setup"] == [sys.executable, "-c", "print('reviewed')"]
        assert preview["setup_shell"] is False
        assert preview["submodules"] == "none"
        runtime._config = Config(
            model="scripted/m",
            version=2,
            v2=ConfigV2(
                model=ModelSection(default="scripted/m"),
                permissions=PermissionsSection(mode="allow"),
                worktrees=WorktreesSection(
                    sessions=True,
                    setup=[sys.executable, "-c", "print('new unreviewed command')"],
                ),
            ),
        )
        with pytest.raises(ValueError, match="setup changed"):
            runtime.approve_worktree_setup(handle.id, "allow_once", digest=reviewed_digest)
        assert not any(event.type == "worktree.setup_trust" for event in handle.events)
    finally:
        await runtime.aclose()


async def test_setup_receives_worktree_cwd_environment_and_streams_output(project, tmp_path):
    script = (
        "import os; print('cwd_matches=' + str(os.getcwd() == os.environ['NEXUS_WORKSPACE'])); "
        "print('worktree=' + os.environ['NEXUS_WORKTREE']); "
        "print('workspace_present=' + str(bool(os.environ['NEXUS_WORKSPACE']))); "
        "print('project_name=' + os.path.basename(os.environ['NEXUS_PROJECT_ROOT'])); "
        "print('workspace_inside_project=' + str(os.path.commonpath([os.environ['NEXUS_PROJECT_ROOT'], "
        "os.environ['NEXUS_WORKSPACE']]) != os.environ['NEXUS_PROJECT_ROOT']))"
    )
    runtime = _setup_runtime(project, tmp_path, setup=[sys.executable, "-c", script])
    try:
        handle = runtime.session("setup-env")
        runtime.approve_worktree_setup(handle.id, "allow_once")
        events = await _drain_turn(handle, "prepare")
        assert events[-1].type == "turn.completed", events[-1]
        root = runtime.session_root(handle.id)
        assert root.kind == "worktree"
        output = "".join(
            event.data.get("text", "") for event in handle.events if event.type == "worktree.setup_output"
        )
        assert "cwd_matches=True" in output
        assert "worktree=1" in output
        assert "workspace_present=True" in output
        assert f"project_name={project.name}" in output
        assert "workspace_inside_project=True" in output
        setup = next(event for event in handle.events if event.type == "worktree.setup_ran")
        assert setup.data["exit"] == 0
    finally:
        await runtime.aclose()


async def test_copy_only_copies_ignored_untracked_files_and_never_overwrites_tracked(project, tmp_path):
    # The shared fixture's git helper is intentionally imported under an alias
    # in this test module to avoid shadowing its local helper names.
    from tests.test_worktree_sessions_runtime import git as _git

    (project / ".gitignore").write_text("ignored.txt\n")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-m", "ignore local setup file")
    (project / "ignored.txt").write_text("local-only\n")
    (project / "tracked.txt").write_text("dirty source must not copy\n")
    runtime = _setup_runtime(project, tmp_path, copy=["*.txt"])
    try:
        handle, events = await _run(runtime, "copy-setup")
        assert events[-1].type == "turn.completed", events[-1]
        root = runtime.session_root(handle.id)
        assert (root.path / "ignored.txt").read_text() == "local-only\n"
        assert (root.path / "tracked.txt").read_text() == "base\n"
        setup = next(event for event in handle.events if event.type == "worktree.setup_ran")
        assert setup.data["copied"] == ["ignored.txt"]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("case", ["symlink", "size"])
async def test_copy_setup_rejects_symlinks_and_enforces_total_size(project, tmp_path, case):
    if case == "symlink":
        outside = tmp_path / "outside.txt"
        outside.write_text("outside data")
        (project / "linked.txt").symlink_to(outside)
        policy = {"copy": ["linked.txt"]}
        error = "symlink"
    else:
        (project / ".gitignore").write_text("large.bin\n")
        (project / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
        policy = {"copy": ["large.bin"], "copy_max_mb": 1}
        error = "copy_max_mb"
    runtime = _setup_runtime(project, tmp_path, **policy)
    try:
        handle, events = await _run(runtime, f"copy-{case}")
        assert events[-1].type == "turn.failed", events[-1]
        assert error in events[-1].data["error"]
        assert runtime.session_root(handle.id).kind == "main"
        assert not any(event.type == "worktree.created" for event in handle.events)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(("on_fail", "terminal"), [("fail", "turn.failed"), ("warn", "turn.completed")])
async def test_setup_failure_policy_is_fail_or_warn(project, tmp_path, on_fail, terminal):
    runtime = _setup_runtime(
        project,
        tmp_path,
        setup=[sys.executable, "-c", "raise SystemExit(9)"],
        setup_on_fail=on_fail,
    )
    try:
        handle = runtime.session(f"setup-{on_fail}")
        runtime.approve_worktree_setup(handle.id, "allow_once")
        events = await _drain_turn(handle, "setup failure policy")
        assert events[-1].type == terminal, events[-1]
        assert any(event.type == "worktree.setup_ran" and event.data["exit"] == 9 for event in handle.events)
        if on_fail == "warn":
            assert any(event.type == "context.notice" and "setup failed" in event.data["message"] for event in handle.events)
        else:
            assert "setup failed" in events[-1].data["error"]
    finally:
        await runtime.aclose()


def test_setup_argv_has_an_aggregate_character_limit():
    with pytest.raises(ValueError, match="bounded command or argv"):
        WorktreesSection(setup=["a" * 4096, "b" * 4096, "c"])
