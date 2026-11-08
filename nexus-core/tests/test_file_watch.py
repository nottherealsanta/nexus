"""Leased watchers invalidate workspace resources without entering conversation history."""
import asyncio
import subprocess

import pytest

from nexus import file_watch
from nexus.file_watch import FileWatches


async def test_leases_expire_renew_and_stop_without_background_work(tmp_path, monkeypatch):
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
        yield set()
    monkeypatch.setattr(file_watch, "awatch", blocked)
    now = [0.0]
    service = FileWatches(lambda *a, **kw: None, clock=lambda: now[0])
    assert service.renew("p", tmp_path, "a")["status"] == "watching"
    key = ("p", str(tmp_path.resolve()))
    now[0] = 50
    service.renew("p", tmp_path, "a")
    now[0] = 70
    assert service._alive(key)
    service.renew("p", tmp_path, "b")
    service.renew("p", tmp_path, "a", release=True)
    assert service._alive(key)
    now[0] = 131
    assert not service._alive(key)
    await service.close()
    assert not service.tasks and not service.leases


async def test_ignored_paths_and_git_metadata_are_distinct_global_invalidations(tmp_path):
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n")
    emitted = []
    service = FileWatches(lambda kind, data: emitted.append((kind, data)))
    key = ("project", str(tmp_path))
    await service._changes(key, {(1, str(tmp_path / "ignored" / "noise"))})
    assert not emitted
    await service._changes(key, {(1, str(tmp_path / "visible")), (1, str(tmp_path / ".git" / "HEAD"))})
    assert [kind for kind, _ in emitted] == ["files.changed", "git.changed"]
    assert emitted[0][1]["paths"] == ["visible"]
    assert all("session_id" not in data and data["source"] == "disk" for _, data in emitted)
    assert all(data["workspace"] == str(tmp_path) for _, data in emitted)


async def test_failed_native_watch_has_visible_polling_fallback_and_releases(tmp_path, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError("watch resources exhausted")
        yield set()
    monkeypatch.setattr(file_watch, "awatch", unavailable)
    emitted = []
    service = FileWatches(lambda kind, data: emitted.append((kind, data)))
    service.renew("p", tmp_path, "client")
    try:
        async with asyncio.timeout(3):
            while not emitted:
                await asyncio.sleep(0.01)
        assert emitted[0][0] == "files.watch" and emitted[0][1]["status"] == "polling"
        assert service.renew("p", tmp_path, "client")["status"] == "polling"
        service.renew("p", tmp_path, "client", release=True)
        await asyncio.sleep(0)
        assert not service.tasks and not service.leases
    finally:
        await service.close()


async def test_real_watcher_sees_an_isolated_file_change(tmp_path):
    emitted = []
    service = FileWatches(lambda kind, data: emitted.append((kind, data)))
    service.renew("p", tmp_path, "client")
    try:
        async with asyncio.timeout(5):
            while not any(kind == "files.changed" for kind, _ in emitted):
                (tmp_path / "change.txt").write_text(str(asyncio.get_running_loop().time()))
                await asyncio.sleep(0.1)
        assert any("change.txt" in data.get("paths", []) for kind, data in emitted if kind == "files.changed")
    finally:
        await service.close()


async def test_watcher_path_payload_and_client_limits_are_bounded(tmp_path, monkeypatch):
    async def ignored(*args):
        return True, ""
    monkeypatch.setattr(file_watch.files, "_git", ignored)
    emitted = []
    service = FileWatches(lambda kind, data: emitted.append((kind, data)))
    await service._changes(("p", str(tmp_path)), {(1, str(tmp_path / f"file-{n}")) for n in range(300)})
    assert len(emitted[0][1]["paths"]) == file_watch.MAX_PATHS
    assert emitted[0][1]["omitted"] == 44
    with pytest.raises(ValueError, match="client_id"):
        service.renew("p", tmp_path, "")
