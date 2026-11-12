"""Worktree review and integration against disposable repositories (no network, no provider)."""
import asyncio
import subprocess

import httpx
import pytest
from conftest import remembering

from nexus import worktree_merge
from nexus.server import build_app, make_runtime
from nexus.store import Store
from nexus.worktrees import WorktreeError

TOKEN = "t0ken"


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout


def commit(path, name, text, message):
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Tester")
    git(root, "config", "user.email", "test@example.invalid")
    commit(root, "tracked", "initial\n", "initial")
    return root


@pytest.fixture
def feature(repo, tmp_path):
    path = tmp_path / "feature-wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(path))
    git(path, "config", "user.name", "Tester")
    git(path, "config", "user.email", "test@example.invalid")
    commit(path, "new.txt", "one\n", "add new")
    return path


async def test_review_lists_commits_files_and_fast_forward(repo, feature):
    data = await worktree_merge.review(repo, feature)
    assert (data["branch"], data["base"], data["ahead"], data["behind"]) == ("feature", "main", 1, 0)
    assert data["fast_forward"] and data["conflicts"] in ([], None) and data["blockers"] == []
    assert [c["subject"] for c in data["commits"]] == ["add new"]
    assert data["files"] == [{"path": "new.txt", "status": "A", "added": 1, "deleted": 0}]
    assert data["head"] == git(feature, "rev-parse", "HEAD").strip()


async def test_review_reports_blockers(repo, feature):
    (feature / "new.txt").write_text("dirty\n")
    (repo / "scratch").write_text("x\n")
    data = await worktree_merge.review(repo, feature)
    assert any("worktree has 1 uncommitted" in b for b in data["blockers"])
    assert any("base checkout has 1 uncommitted" in b for b in data["blockers"])
    with pytest.raises(WorktreeError, match="main checkout"):
        await worktree_merge.review(repo, repo)
    with pytest.raises(WorktreeError, match="not a registered"):
        await worktree_merge.review(repo, repo.parent)


async def test_fast_forward_integrates_and_needs_the_reviewed_head(repo, feature):
    review = await worktree_merge.review(repo, feature)
    with pytest.raises(WorktreeError, match="expected_head"):
        await worktree_merge.integrate(repo, feature, expected_head="abc", mode="ff")
    with pytest.raises(WorktreeError, match="changed after the review"):
        await worktree_merge.integrate(repo, feature, expected_head="0" * 40, mode="ff")
    with pytest.raises(WorktreeError, match="mode"):
        await worktree_merge.integrate(repo, feature, expected_head=review["head"], mode="squash")
    result = await worktree_merge.integrate(repo, feature, expected_head=review["head"], mode="ff")
    assert result["merged"] and result["head"] == review["head"]
    assert git(repo, "rev-parse", "HEAD").strip() == review["head"] and (repo / "new.txt").exists()
    assert (await worktree_merge.review(repo, feature))["blockers"] == ["the branch has no commits the base lacks"]


async def test_fast_forward_refuses_a_moved_base_but_merge_commit_works(repo, feature):
    commit(repo, "other.txt", "o\n", "base moves")
    review = await worktree_merge.review(repo, feature)
    assert review["behind"] == 1 and not review["fast_forward"] and review["conflicts"] in ([], None)
    with pytest.raises(WorktreeError, match="cannot fast-forward"):
        await worktree_merge.integrate(repo, feature, expected_head=review["head"], mode="ff")
    before = git(repo, "rev-parse", "HEAD").strip()
    assert before == review["base_head"]
    result = await worktree_merge.integrate(repo, feature, expected_head=review["head"], mode="merge", message="join")
    assert git(repo, "log", "-1", "--format=%s").strip() == "join"
    assert len(git(repo, "rev-list", "--parents", "-n1", "HEAD").split()) == 3  # a real merge commit
    assert result["previous_base_head"] == before and (repo / "other.txt").exists() and (repo / "new.txt").exists()


async def test_a_conflicting_merge_changes_nothing(repo, feature):
    commit(feature, "tracked", "feature side\n", "feature edits tracked")
    commit(repo, "tracked", "main side\n", "main edits tracked")
    review = await worktree_merge.review(repo, feature)
    if review["conflicts"] is None:
        pytest.skip("this Git cannot predict merges (merge-tree --write-tree)")
    assert review["conflicts"] == ["tracked"] and review["blockers"]
    before = git(repo, "rev-parse", "HEAD").strip()
    with pytest.raises(WorktreeError, match="conflict"):
        await worktree_merge.integrate(repo, feature, expected_head=review["head"], mode="merge")
    assert git(repo, "rev-parse", "HEAD").strip() == before and git(repo, "status", "--porcelain").strip() == ""


async def test_in_progress_operation_on_the_base_blocks(repo, feature):
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    data = await worktree_merge.review(repo, feature)
    assert any("Git operation in progress" in b for b in data["blockers"])


@pytest.fixture
def api(tmp_path, monkeypatch, repo):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    return rt, client


async def test_api_review_then_integrate_and_remove(api, repo, feature):
    rt, client = api
    project = (await client.post("/api/project", json={"path": str(repo)})).json()
    base = f"/api/project/{project['id']}/worktree"
    review = await client.get(base + "/review", params={"worktree": str(feature)})
    assert review.status_code == 200, review.text
    data = review.json()
    assert data["live"]["worktree"]["working"] == [] and (await client.get(base + "/review")).status_code == 400
    body = {"worktree": str(feature), "expected_head": data["head"], "mode": "ff", "remove": True}
    assert (await client.post(base + "/integrate", json=body)).status_code == 400  # a key is required
    done = await client.post(base + "/integrate", json=body, headers={"Idempotency-Key": "int-1"})
    assert done.status_code == 200, done.text
    assert done.json()["removed"] == {"worktree": True, "branch": True, "error": None}
    assert not feature.exists() and "feature" not in git(repo, "branch", "--list")
    replay = await client.post(base + "/integrate", json=body, headers={"Idempotency-Key": "int-1"})
    assert replay.headers["idempotent-replayed"] == "true"
    events = rt.store.replay(0, rt.store.log_head(), 1000)
    assert any(e["type"] == "worktree.integrated" and e["session_id"] is None for e in events)


async def test_api_refuses_removal_while_a_session_uses_the_worktree_and_merges_nothing(api, repo, feature):
    _rt, client = api
    project = (await client.post("/api/project", json={"path": str(repo)})).json()
    await client.post("/api/session", json={"directory": str(feature)})
    base = f"/api/project/{project['id']}/worktree"
    head = (await client.get(base + "/review", params={"worktree": str(feature)})).json()["head"]
    before = git(repo, "rev-parse", "HEAD").strip()
    refused = await client.post(base + "/integrate", headers={"Idempotency-Key": "k2"},
                                json={"worktree": str(feature), "expected_head": head, "mode": "ff", "remove": True})
    assert refused.status_code == 400 and "directory of session" in refused.text
    assert git(repo, "rev-parse", "HEAD").strip() == before
    kept = await client.post(base + "/integrate", headers={"Idempotency-Key": "k3"},
                             json={"worktree": str(feature), "expected_head": head, "mode": "ff"})
    assert kept.status_code == 200 and feature.exists() and kept.json()["removed"]["worktree"] is False


async def test_api_worktree_live_requires_confirmation(api, repo, feature):
    rt, client = api
    project = (await client.post("/api/project", json={"path": str(repo)})).json()
    session = (await client.post("/api/session", json={"directory": str(feature)})).json()
    request = {"id": "per_w", "session_id": session["id"], "tool": "bash", "call_id": "c", "args": {}}
    rt.approvals["per_w"] = (session["id"], asyncio.get_running_loop().create_future(), request)
    try:
        base = f"/api/project/{project['id']}/worktree"
        head = (await client.get(base + "/review", params={"worktree": str(feature)})).json()["head"]
        body = {"worktree": str(feature), "expected_head": head, "mode": "ff"}
        refused = await client.post(base + "/integrate", json=body, headers={"Idempotency-Key": "w1"})
        assert refused.status_code == 409 and refused.json()["code"] == "worktree_live"
        assert [s["id"] for s in refused.json()["live"]["worktree"]["working"]] == [session["id"]]
        body["confirmed_live"] = [session["id"]]
        ok = await client.post(base + "/integrate", json=body, headers={"Idempotency-Key": "w2"})
        assert ok.status_code == 200, ok.text
    finally:
        del rt.approvals["per_w"]
