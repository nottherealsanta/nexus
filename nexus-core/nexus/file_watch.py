"""Leased, bounded workspace watching for the desktop Files/Git surfaces.

No file contents or session ids enter these global invalidation events. A failed
native watcher falls back to a visible three-second invalidation poll.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from watchfiles import awatch

from nexus import files, worktrees

LEASE_SECONDS = 60.0
MAX_ROOTS = 32
MAX_CLIENTS = 256
MAX_PATHS = 256
POLL_SECONDS = 3.0


async def ignored_paths(root: Path, names: list[str]) -> set[str]:
    """NUL-safe ignore classification, with output bounded by the offered path list."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "check-ignore", "--stdin", "-z", cwd=root, env=worktrees.ENV,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        payload = ("\0".join(names) + "\0").encode("utf-8", "surrogateescape")
        output, _ = await asyncio.wait_for(process.communicate(payload), timeout=5)
        return set(output.decode("utf-8", "surrogateescape").split("\0")) if process.returncode == 0 else set()
    except (OSError, TimeoutError):
        return set()
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class FileWatches:
    def __init__(self, publish: Callable[..., Any], *, clock: Callable[[], float] = time.monotonic) -> None:
        self.publish = publish
        self.clock = clock
        self.leases: dict[tuple[str, str, str], float] = {}
        self.tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.modes: dict[tuple[str, str], str] = {}

    def renew(self, project: str, root: Path, client: str, *, release: bool = False) -> dict:
        if not isinstance(client, str) or not client or len(client) > 128:
            raise ValueError("client_id must contain 1 to 128 characters")
        root = root.resolve()
        key = (project, str(root))
        lease = (*key, client)
        if release:
            self.leases.pop(lease, None)
            if not self._alive(key):
                if task := self.tasks.pop(key, None):
                    task.cancel()
                self.modes.pop(key, None)
            return {"status": "stopped", "lease_seconds": LEASE_SECONDS}
        if not root.is_dir():
            raise ValueError("watch root must be a directory")
        self._expire()
        if lease not in self.leases and len(self.leases) >= MAX_CLIENTS:
            raise ValueError("too many file watcher clients")
        if key not in self.tasks and len(self.tasks) >= MAX_ROOTS:
            raise ValueError("too many watched workspaces")
        self.leases[lease] = self.clock() + LEASE_SECONDS
        if key not in self.tasks:
            self.modes[key] = "watching"
            self.tasks[key] = asyncio.create_task(self._run(key))
        return {"status": self.modes[key], "lease_seconds": LEASE_SECONDS, "poll_seconds": POLL_SECONDS}

    def _expire(self) -> None:
        now = self.clock()
        for lease, until in list(self.leases.items()):
            if until <= now:
                del self.leases[lease]

    def _alive(self, key: tuple[str, str]) -> bool:
        self._expire()
        return any(lease[:2] == key for lease in self.leases)

    def _event(self, key: tuple[str, str], kind: str, **data: Any) -> None:
        self.publish(kind, {"project_id": key[0], "workspace": key[1], "source": "disk", **data})

    async def _changes(self, key: tuple[str, str], changed: set[tuple[Any, str]]) -> None:
        root = Path(key[1])
        names = set()
        git_changed = False
        for _, name in changed:
            try:
                relative = Path(name).relative_to(root)
            except ValueError:
                git_changed = True
                continue
            if ".git" in relative.parts:
                git_changed = True
            else:
                names.add(relative.as_posix())
        selected = sorted(names)[:MAX_PATHS]
        if selected:
            # check-ignore leaves tracked files visible even when a later ignore rule matches.
            excluded = await ignored_paths(root, selected)
            visible = [name for name in selected if name not in excluded]
            if visible or len(names) > MAX_PATHS:
                self._event(key, "files.changed", paths=visible, omitted=max(0, len(names) - MAX_PATHS))
                git_changed = True
        if git_changed:
            self._event(key, "git.changed")

    async def _run(self, key: tuple[str, str]) -> None:
        current = asyncio.current_task()
        try:
            root = Path(key[1])
            ok, git_dir = await files._git(root, "rev-parse", "--absolute-git-dir")
            roots = [str(root)]
            if ok and git_dir.strip() and Path(git_dir.strip()).is_dir() and not Path(git_dir.strip()).is_relative_to(root):
                roots.append(git_dir.strip())
            ok, common = await files._git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if ok and common.strip() and Path(common.strip()).is_dir():
                directory = Path(common.strip())
                if not any(directory.is_relative_to(Path(watched)) for watched in roots):
                    roots = [watched for watched in roots if not Path(watched).is_relative_to(directory)]
                    roots.append(str(directory))
            try:
                async for changes in awatch(*roots, watch_filter=None, debounce=250, step=50,
                                           rust_timeout=1000, yield_on_timeout=True):
                    if not self._alive(key):
                        break
                    if changes:
                        await self._changes(key, changes)
            except (OSError, RuntimeError):
                self.modes[key] = "polling"
                self._event(key, "files.watch", status="polling", poll_seconds=POLL_SECONDS)
                while self._alive(key):
                    self._event(key, "files.changed", paths=[], polling=True)
                    self._event(key, "git.changed", polling=True)
                    await asyncio.sleep(POLL_SECONDS)
        finally:
            if self.tasks.get(key) is current:
                self.tasks.pop(key, None)
                self.modes.pop(key, None)
                for lease in list(self.leases):
                    if lease[:2] == key:
                        del self.leases[lease]

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        self.leases.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.modes.clear()
