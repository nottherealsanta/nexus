"""A worktree belongs to the project: several sessions can be in one (plan 3.4)."""
from __future__ import annotations

import pytest

from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_worktree_sessions_runtime import drain, git, project, runtime  # noqa: F401


async def test_two_sessions_share_a_worktree_and_commit_only_their_own_paths(project, tmp_path):  # noqa: F811
    provider = ScriptedProvider(
        text_response("b says hi"),
        tool_response(("w", "write", {"path": "a.txt", "content": "a\n"})), text_response("a done"))
    rt = runtime(project, tmp_path, provider)
    try:
        a = rt.session("session-a")
        await rt.promote_session(a.id, reason="start the shared work", name="shared")
        b = rt.session("session-b")
        await drain(b, "hi")
        await rt.move_session_worktree(b.id, "shared", "join a")
        root_a, root_b = rt.session_root(a.id), rt.session_root(b.id)
        assert root_a.worktree_id == root_b.worktree_id and root_a.path == root_b.path
        assert rt.session_root(a.id) == root_a  # nothing was transferred and a was not moved

        listing = rt.list_session_worktrees(b.id)
        row = next(item for item in listing["worktrees"] if item["name"] == "shared")
        assert row["current"] and {s["id"] for s in row["sessions"]} >= {b.id}
        assert listing["current"] == "shared" and listing["main"]["current"] is False

        (root_a.path / "b-wip.txt").write_text("b is still editing\n")  # another session's half-finished work
        events = await drain(a, "write a.txt")
        assert events[-1].type == "turn.completed", events[-1]
        assert "a.txt" in git(root_a.path, "ls-tree", "-r", "--name-only", "HEAD").split()
        assert "b-wip.txt" not in git(root_a.path, "ls-tree", "-r", "--name-only", "HEAD").split()
        assert git(root_a.path, "status", "--porcelain") == "?? b-wip.txt"
        facts = dict(rt._session_worktree_facts(a.id))
        assert facts["Other sessions in this worktree"] != "none"

        # Leaving is refused while changes nobody committed remain, then works.
        with pytest.raises(ValueError, match="uncommitted"):
            await rt.move_session_worktree(b.id, "main", "done")
        assert rt.session_root(b.id).kind == "worktree"
        (root_a.path / "b-wip.txt").unlink()
        await rt.move_session_worktree(b.id, "main", "done")
        assert rt.session_root(b.id).kind == "main"
        assert rt.session_root(a.id).kind == "worktree"  # a's root is untouched
        assert root_a.path.is_dir()  # unmerged work and a still in it: the checkout stays
        moved = [e for e in b.events if e.type == "worktree.moved"]
        assert moved[-1].data["from_name"] == "shared" and moved[-1].data["to_name"] == "main"
    finally:
        await rt.aclose()


async def test_moving_into_a_removed_checkout_recreates_it(project, tmp_path):  # noqa: F811
    import subprocess
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        a = rt.session("recreate-a")
        await rt.promote_session(a.id, reason="start", name="revive")
        path = rt.session_root(a.id).path
        await rt.detach_session_worktree(a.id)
        if path.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=project, check=True)
        assert not path.exists()
        listing = rt.list_session_worktrees(a.id)
        assert next(r for r in listing["worktrees"] if r["name"] == "revive")["present"] is False
        b = rt.session("recreate-b")
        await rt.move_session_worktree(b.id, "revive", "continue it")
        assert path.is_dir() and rt.session_root(b.id).path == path
    finally:
        await rt.aclose()


async def test_move_to_an_unknown_name_lists_what_exists(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        a = rt.session("unknown-a")
        await rt.promote_session(a.id, reason="start", name="known")
        b = rt.session("unknown-b")
        with pytest.raises(ValueError, match="known"):
            await rt.move_session_worktree(b.id, "missing", "x")
        with pytest.raises(ValueError, match="already in the main checkout"):
            await rt.move_session_worktree(b.id, "main", "x")
    finally:
        await rt.aclose()


async def test_worktree_list_is_bounded_and_marks_external_worktrees(project, tmp_path):  # noqa: F811
    import subprocess
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")), max_worktrees=2)
    try:
        subprocess.run(["git", "worktree", "add", "-b", "outside", str(tmp_path / "outside-wt")], cwd=project, check=True,
                       capture_output=True)
        a = rt.session("bounded-a")
        await rt.promote_session(a.id, reason="one", name="one")
        await rt.detach_session_worktree(a.id)
        await rt.promote_session(a.id, reason="two", name="two")
        listing = rt.list_session_worktrees(a.id)
        rows = [item for item in listing["worktrees"] if item["kind"] == "worktree"]
        assert listing["worktrees"][0]["kind"] == "main" and len(rows) == 2
        assert listing["clipped"] == 0 and listing["max_worktrees"] == 2
        from pathlib import Path
        assert [Path(item["path"]).resolve() for item in listing["external"]] == [(tmp_path / "outside-wt").resolve()]
        await rt.detach_session_worktree(a.id)
        # Clean, empty worktrees are removed on departure and free their quota.
        await rt.promote_session(a.id, reason="three", name="three")
    finally:
        await rt.aclose()


async def test_merged_clean_worktree_is_cleaned_up_only_when_the_last_session_leaves(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        a = rt.session("clean-a")
        await rt.promote_session(a.id, reason="start", name="landed")
        root = rt.session_root(a.id)
        (root.path / "landed.txt").write_text("landed\n")
        git(root.path, "add", "landed.txt")
        git(root.path, "commit", "-qm", "landed work")
        git(project, "merge", "--ff-only", root.branch)
        b = rt.session("clean-b")
        await rt.move_session_worktree(b.id, "landed", "join")
        await rt.move_session_worktree(a.id, "main", "leave first")
        assert root.path.is_dir()  # b is still in it
        await rt.move_session_worktree(b.id, "main", "leave last")
        assert not root.path.exists()  # merged, clean, last session gone
        assert git(project, "branch", "--list", root.branch)  # the branch is kept
        assert any(e.type == "worktree.removed" for e in b.events)
    finally:
        await rt.aclose()
