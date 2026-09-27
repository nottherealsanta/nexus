from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.agents import worktrees
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.runtime import Runtime
from nexus.ui.cli.client import Client


def git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Host worktree test")
    git(path, "config", "user.email", "host-worktree@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "initial")
    return path


def runtime_for(parent: Path) -> Runtime:
    return Runtime(parent, providers={})


@pytest.mark.asyncio
async def test_owned_worktree_read_inspect_and_review_over_facade(tmp_path: Path):
    parent = repository(tmp_path / "owned-parent")
    runtime = runtime_for(parent)
    facade = HostFacade(runtime)
    service_root = runtime._worktree_root
    record = runtime._worktree_service.create(parent, "parent/sub/1", root=service_root)
    (record.path / "tracked.txt").write_text("reviewed change\n", encoding="utf-8")
    runtime._worktree_service.mark_finished(
        record.child_id, SimpleNamespace(status="completed"), root=service_root
    )
    before_head = git(parent, "rev-parse", "HEAD")
    before_status = git(parent, "status", "--porcelain", "--untracked-files=all")
    before_index = (parent / ".git" / "index").read_bytes()

    listed = await facade.handle(p.WorktreeList())
    assert isinstance(listed, p.WorktreeListResult)
    assert listed.status == "ok" and not listed.has_more
    assert listed.worktrees[0]["child_id"] == record.child_id
    assert "parent_workspace" not in str(listed)
    assert "review_id" in listed.worktrees[0]

    inspected = await facade.handle(p.WorktreeInspect(child_id=record.child_id))
    assert isinstance(inspected, p.WorktreeInspectResult)
    assert inspected.status == "finalized"
    assert inspected.record["dirty"] is True
    assert "path" not in inspected.record
    assert "branch" not in inspected.record

    reviewed = await facade.handle(p.WorktreeReview(child_id=record.child_id, limit=2))
    assert isinstance(reviewed, p.WorktreeReviewResult)
    assert reviewed.status == "finalized"
    assert reviewed.review_id and reviewed.digest
    assert reviewed.entries[0]["path"] == "tracked.txt"
    assert "reviewed change" in str(reviewed.diff)
    assert "artifact_path" not in str(reviewed)
    assert "blobs" not in str(reviewed)

    reread = await facade.handle(
        p.WorktreeReview(
            child_id=record.child_id,
            review_id=reviewed.review_id,
            cursor=0,
            limit=2,
        )
    )
    assert isinstance(reread, p.WorktreeReviewResult)
    assert reread.digest == reviewed.digest
    assert reread.diff == reviewed.diff

    assert git(parent, "rev-parse", "HEAD") == before_head
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == before_status
    assert (parent / ".git" / "index").read_bytes() == before_index
    await runtime.aclose()


@pytest.mark.asyncio
async def test_unknown_foreign_and_tampered_child_worktrees_are_refused(tmp_path: Path):
    parent = repository(tmp_path / "owned-parent")
    runtime = runtime_for(parent)
    facade = HostFacade(runtime)
    unknown = await facade.handle(p.WorktreeInspect(child_id="missing"))
    assert isinstance(unknown, p.ErrorResult)
    assert isinstance(unknown, p.ErrorResult)

    foreign_parent = repository(tmp_path / "foreign-parent")
    foreign_root = tmp_path / "foreign-service"
    foreign_record = worktrees.create(foreign_parent, "foreign", root=foreign_root)
    foreign = await facade.handle(p.WorktreeInspect(child_id=foreign_record.child_id))
    assert isinstance(foreign, p.ErrorResult)
    assert not any(
        row["child_id"] == foreign_record.child_id
        for row in (await facade.handle(p.WorktreeList())).worktrees
    )

    owned = runtime._worktree_service.create(
        parent, "tampered", root=runtime._worktree_root
    )
    record_path = runtime._worktree_root / "records" / worktrees._record_name(owned.child_id)
    envelope = json.loads(record_path.read_text(encoding="utf-8"))
    envelope["record"]["path"] = str(foreign_record.path)
    key = (runtime._worktree_root / ".ownership-key").read_bytes()
    payload = envelope["record"]
    envelope["mac"] = hmac.new(
        key,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    record_path.write_text(json.dumps(envelope), encoding="utf-8")
    record_path.chmod(0o600)

    tampered = await facade.handle(p.WorktreeInspect(child_id=owned.child_id))
    assert isinstance(tampered, p.ErrorResult)
    assert "outside" in tampered.message.lower()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_worktree_review_validates_bounds_before_runtime_dispatch(tmp_path: Path):
    class RuntimeStub:
        def __init__(self):
            self.calls = []

        def review_worktree(self, child_id, **kwargs):
            self.calls.append((child_id, kwargs))
            return SimpleNamespace(
                manifest={"entries": []},
                diff_pages=(),
                cursor=kwargs["cursor"],
                next_cursor=None,
                review_id="a" * 32,
                digest="b" * 64,
            )

        def inspect_worktree(self, child_id):
            return SimpleNamespace(child_id=child_id, lifecycle="finalized")

    runtime = RuntimeStub()
    facade = HostFacade(runtime)
    for command in (
        p.WorktreeReview(child_id="child", cursor=-1),
        p.WorktreeReview(child_id="child", limit=9),
        p.WorktreeReview(child_id="child", review_id="../bad"),
    ):
        response = await facade.handle(command)
        assert isinstance(response, p.ErrorResult)
    assert runtime.calls == []

    result = await facade.handle(p.WorktreeReview(child_id="child", limit=8))
    assert isinstance(result, p.WorktreeReviewResult)
    assert result.status == "finalized"
    assert runtime.calls[0][1]["limit"] == 8

    commands = []

    class Transport:
        async def request(self, command):
            commands.append(command)
            return await facade.handle(command)

        async def aclose(self):
            return None

        def events(self, *_args, **_kwargs):
            raise AssertionError("worktree query does not stream session events")

    client_result = await Client(Transport()).review_worktree("child", limit=2)
    assert client_result.review_id == "a" * 32
    assert commands == [p.WorktreeReview(child_id="child", limit=2)]


@pytest.mark.asyncio
async def test_host_worktree_integrate_requires_fresh_single_use_confirmation(tmp_path: Path):
    parent = repository(tmp_path / "parent")
    runtime = runtime_for(parent)
    facade = HostFacade(runtime)
    record = runtime._worktree_service.create(parent, "parent/sub/1", root=runtime._worktree_root)
    (record.path / "tracked.txt").write_text("child change\n", encoding="utf-8")
    runtime._worktree_service.mark_finished(
        record.child_id, SimpleNamespace(status="completed"), root=runtime._worktree_root
    )
    page = runtime.review_worktree(record.child_id)
    ack = await facade.handle(
        p.WorktreeAcknowledge(record.child_id, page.review_id, page.digest)
    )
    assert isinstance(ack, p.WorktreeAcknowledgeResult)
    before = (git(parent, "rev-parse", "HEAD"), git(parent, "status", "--porcelain"), (parent / ".git" / "index").read_bytes())

    preview = await facade.handle(
        p.WorktreeIntegrate(record.child_id, page.review_id, page.digest)
    )
    assert isinstance(preview, p.WorktreeMutationResult)
    assert preview.status == "requires_confirmation"
    assert preview.confirmation_token
    assert preview.impact["parent_clean"] is True
    assert before == (git(parent, "rev-parse", "HEAD"), git(parent, "status", "--porcelain"), (parent / ".git" / "index").read_bytes())

    committed = await facade.handle(
        p.WorktreeIntegrate(
            record.child_id, page.review_id, page.digest, preview.confirmation_token
        )
    )
    assert isinstance(committed, p.WorktreeMutationResult)
    assert committed.status == "committed"
    assert (parent / "tracked.txt").read_text(encoding="utf-8") == "child change\n"
    replay = await facade.handle(
        p.WorktreeIntegrate(
            record.child_id, page.review_id, page.digest, preview.confirmation_token
        )
    )
    assert isinstance(replay, p.ErrorResult)
    await runtime.aclose()


@pytest.mark.asyncio
async def test_confirmation_denies_forged_mismatched_expired_and_stale_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import nexus.host.facade as facade_module

    parent = repository(tmp_path / "parent")
    runtime = runtime_for(parent)
    facade = HostFacade(runtime)
    record = runtime._worktree_service.create(parent, "parent/sub/1", root=runtime._worktree_root)
    (record.path / "tracked.txt").write_text("child change\n", encoding="utf-8")
    runtime._worktree_service.mark_finished(
        record.child_id, SimpleNamespace(status="completed"), root=runtime._worktree_root
    )
    page = runtime.review_worktree(record.child_id)
    await facade.handle(p.WorktreeAcknowledge(record.child_id, page.review_id, page.digest))
    preview = await facade.handle(p.WorktreeIntegrate(record.child_id, page.review_id, page.digest))
    assert isinstance(preview, p.WorktreeMutationResult)

    forged = preview.confirmation_token[:-1] + ("0" if preview.confirmation_token[-1] != "0" else "1")
    denied = await facade.handle(
        p.WorktreeIntegrate(record.child_id, page.review_id, page.digest, forged)
    )
    assert isinstance(denied, p.ErrorResult)

    mismatched = await facade.handle(
        p.WorktreeDiscard(record.child_id, confirmation_token=preview.confirmation_token)
    )
    assert isinstance(mismatched, p.ErrorResult)

    real_time = facade_module.time.time
    monkeypatch.setattr(facade_module.time, "time", lambda: real_time() + 121)
    expired = await facade.handle(
        p.WorktreeIntegrate(record.child_id, page.review_id, page.digest, preview.confirmation_token)
    )
    assert isinstance(expired, p.ErrorResult)
    monkeypatch.setattr(facade_module.time, "time", real_time)

    fresh = await facade.handle(p.WorktreeIntegrate(record.child_id, page.review_id, page.digest))
    assert isinstance(fresh, p.WorktreeMutationResult)
    (record.path / "tracked.txt").write_text("changed after preview\n", encoding="utf-8")
    stale = await facade.handle(
        p.WorktreeIntegrate(record.child_id, page.review_id, page.digest, fresh.confirmation_token)
    )
    assert isinstance(stale, p.ErrorResult)
    assert (parent / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_change", ["dirty", "advanced"])
async def test_host_integrate_denies_dirty_or_advanced_parent(tmp_path: Path, parent_change: str):
    parent = repository(tmp_path / "parent")
    runtime = runtime_for(parent)
    facade = HostFacade(runtime)
    record = runtime._worktree_service.create(parent, "parent/sub/1", root=runtime._worktree_root)
    (record.path / "tracked.txt").write_text("child change\n", encoding="utf-8")
    runtime._worktree_service.mark_finished(
        record.child_id, SimpleNamespace(status="completed"), root=runtime._worktree_root
    )
    page = runtime.review_worktree(record.child_id)
    await facade.handle(p.WorktreeAcknowledge(record.child_id, page.review_id, page.digest))
    if parent_change == "dirty":
        (parent / "untracked.txt").write_text("preserve\n", encoding="utf-8")
    else:
        (parent / "advance.txt").write_text("advance\n", encoding="utf-8")
        git(parent, "add", "advance.txt")
        git(parent, "commit", "-qm", "advance")
    before = (parent / "tracked.txt").read_bytes()
    denied = await facade.handle(p.WorktreeIntegrate(record.child_id, page.review_id, page.digest))
    assert isinstance(denied, p.ErrorResult)
    assert (parent / "tracked.txt").read_bytes() == before
    await runtime.aclose()


@pytest.mark.asyncio
async def test_host_force_discard_is_confirmed_and_preserves_parent(tmp_path: Path):
    parent = repository(tmp_path / "parent")
    runtime = runtime_for(parent)
    facade = HostFacade(runtime)
    record = runtime._worktree_service.create(parent, "parent/sub/1", root=runtime._worktree_root)
    runtime._worktree_service.mark_finished(
        record.child_id, SimpleNamespace(status="completed"), root=runtime._worktree_root
    )
    (record.path / "tracked.txt").write_text("deliberately dirty\n", encoding="utf-8")
    preview = await facade.handle(p.WorktreeDiscard(record.child_id, force=True))
    assert isinstance(preview, p.WorktreeMutationResult)
    assert preview.status == "requires_confirmation"
    assert preview.impact["child_dirty"] is True
    result = await facade.handle(
        p.WorktreeDiscard(record.child_id, force=True, confirmation_token=preview.confirmation_token)
    )
    assert isinstance(result, p.WorktreeMutationResult)
    assert result.status == "committed"
    assert not record.path.exists()
    assert (parent / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_host_clean_discard_uses_acknowledged_review_and_confirmation(tmp_path: Path):
    parent = repository(tmp_path / "parent")
    runtime = runtime_for(parent)
    facade = HostFacade(runtime)
    record = runtime._worktree_service.create(parent, "parent/sub/1", root=runtime._worktree_root)
    runtime._worktree_service.mark_finished(
        record.child_id, SimpleNamespace(status="completed"), root=runtime._worktree_root
    )
    page = runtime.review_worktree(record.child_id)
    acknowledged = await facade.handle(
        p.WorktreeAcknowledge(record.child_id, page.review_id, page.digest)
    )
    assert isinstance(acknowledged, p.WorktreeAcknowledgeResult)
    preview = await facade.handle(p.WorktreeDiscard(record.child_id))
    assert isinstance(preview, p.WorktreeMutationResult)
    assert preview.status == "requires_confirmation"
    result = await facade.handle(
        p.WorktreeDiscard(
            record.child_id, confirmation_token=preview.confirmation_token
        )
    )
    assert isinstance(result, p.WorktreeMutationResult)
    assert result.status == "committed"
    assert not record.path.exists()
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == ""
    await runtime.aclose()
