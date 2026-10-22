from __future__ import annotations

import asyncio
import contextlib
import subprocess
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.paths import project_key, project_state_dir
from nexus.config.schema import AgentSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection
from nexus.host import protocol as p
from nexus.host.daemon import Daemon
from nexus.host.transports.uds import UDSClient
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.tools.builtin._shell_logs import read_session_cwd
from nexus.util import new_id


def _config() -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _project(parent: Path, name: str) -> tuple[Path, Path, Path]:
    root = parent / name
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Nexus Test")
    _git(root, "config", "user.email", "nexus@example.test")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial")
    linked_a, linked_b = parent / f"{name}-a", parent / f"{name}-b"
    _git(root, "worktree", "add", "-qb", "feature-a", str(linked_a))
    _git(root, "worktree", "add", "-qb", "feature-b", str(linked_b))
    return root, linked_a, linked_b


@contextlib.asynccontextmanager
async def _running(tmp_path: Path, project: Path):
    def make_runtime(workspace, *, home=None, environ=None, **_kwargs):
        async def step(_request):
            return text_response("ok")

        return Runtime(
            workspace,
            home=home,
            environ=environ,
            config=_config(),
            providers={"scripted": ScriptedProvider(*([[step]] * 32))},
        )

    socket_path = Path("/tmp") / f"nexus-sr-{new_id()[:10]}.sock"
    daemon = Daemon(
        project,
        socket_path=socket_path,
        idle_timeout=120,
        runtime_factory=make_runtime,
    )
    task = asyncio.create_task(daemon.serve_forever())
    try:
        for _ in range(500):
            if daemon.started or task.done():
                break
            await asyncio.sleep(0.01)
        if task.done():
            task.result()
        assert daemon.started
        yield daemon, socket_path
    finally:
        daemon.request_stop("session root test")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 15)


@pytest.mark.asyncio
async def test_daemon_routes_sessions_and_files_by_linked_worktree_launch_root(tmp_path, monkeypatch):
    home = tmp_path / "nexus-home"
    monkeypatch.setenv("NEXUS_HOME", str(home))
    project, linked_a, linked_b = _project(tmp_path, "project")
    _, foreign_a, _ = _project(tmp_path, "foreign")
    (linked_a / "only-a.py").write_text("a marker\n", encoding="utf-8")
    (linked_b / "only-b.py").write_text("b marker\n", encoding="utf-8")
    nested = linked_a / "nested"
    nested.mkdir()

    async with _running(tmp_path, project) as (daemon, socket_path):
        clients: list[UDSClient] = []

        async def connect(path: Path) -> UDSClient:
            client = await UDSClient.connect(socket_path, focus=str(path))
            clients.append(client)
            return client

        try:
            client_a = await connect(linked_a)
            opened_a = await client_a.call(p.SessionOpen(session="session-a"))
            assert isinstance(opened_a, p.SessionOpenResult)

            client_b = await connect(linked_b)
            opened_b = await client_b.call(p.SessionOpen(session="session-b"))
            assert isinstance(opened_b, p.SessionOpenResult)

            key = project_key(project)
            host = daemon.projects.hosts[key]
            assert len(daemon.projects.hosts) == 1
            assert host.runtime.session_root("session-a").path == linked_a.resolve()
            assert host.runtime.session_root("session-b").path == linked_b.resolve()
            assert host.runtime is daemon.projects.hosts[key].runtime

            assert await client_b.call(p.FileSearch(query="only-a", session="session-a")) == p.FileSearchResult(
                paths=["only-a.py"]
            )
            assert await client_a.call(p.FileSearch(query="only-b", session="session-b")) == p.FileSearchResult(
                paths=["only-b.py"]
            )
            status_a = await client_b.call(p.GitStatus(session="session-a"))
            status_b = await client_a.call(p.GitStatus(session="session-b"))
            assert status_a.branch == "feature-a" and status_b.branch == "feature-b"
            assert {row["path"] for row in status_a.rows} == {"only-a.py"}
            assert {row["path"] for row in status_b.rows} == {"only-b.py"}

            main_client = await connect(project)
            main_open = await main_client.call(p.SessionOpen(session="main-session"))
            assert isinstance(main_open, p.SessionOpenResult)
            assert host.runtime.session_root("main-session").path == project.resolve()
            main_shell = await main_client.call(
                p.SessionShell(session="main-session", command="true")
            )
            assert isinstance(main_shell, p.SessionShellResult)
            main_handle = host.runtime.sessions.open("main-session", create=False)
            for _ in range(200):
                if any(event.type == "shell.completed" for event in main_handle.events):
                    break
                await asyncio.sleep(0.01)
            linked_reopen = await client_a.call(
                p.SessionOpen(session="main-session", create=False)
            )
            assert isinstance(linked_reopen, p.SessionOpenResult)
            assert host.runtime.session_root("main-session").path == project.resolve()

            focus_client = await connect(project)
            focused = await focus_client.call(p.ProjectFocus(project=str(linked_b)))
            assert isinstance(focused, p.ProjectFocusResult)
            focused_open = await focus_client.call(p.SessionOpen(session="focus-session"))
            assert isinstance(focused_open, p.SessionOpenResult)
            assert host.runtime.session_root("focus-session").path == linked_b.resolve()

            canonical_focus = await focus_client.call(p.ProjectFocus(project=key))
            assert isinstance(canonical_focus, p.ProjectFocusResult)
            canonical_open = await focus_client.call(
                p.SessionOpen(session="canonical_focus_session")
            )
            assert isinstance(canonical_open, p.SessionOpenResult)
            assert host.runtime.session_root("canonical_focus_session").path == project.resolve()

            foreign_open = await main_client.call(
                p.SessionOpen(session="foreign-session", root=str(foreign_a))
            )
            assert isinstance(foreign_open, p.ErrorResult)
            assert not host.runtime.sessions.exists("foreign-session")

            subdir_client = await connect(nested)
            subdir_open = await subdir_client.call(p.SessionOpen(session="subdir-session"))
            assert isinstance(subdir_open, p.SessionOpenResult)
            assert host.runtime.session_root("subdir-session").path == linked_a.resolve()
            assert read_session_cwd(project, "subdir-session") == str(nested.resolve())
            cwd_path = project_state_dir(project) / "shell" / "subdir-session" / "cwd"
            assert cwd_path.exists() and project_key(project) in str(cwd_path)

            shell_result = await subdir_client.call(
                p.SessionShell(session="subdir-session", command="pwd")
            )
            assert isinstance(shell_result, p.SessionShellResult)
            session = host.runtime.sessions.open("subdir-session", create=False)
            for _ in range(200):
                completed = next(
                    (event for event in reversed(session.events)
                     if event.type == "shell.completed"
                     and event.data.get("shell_id") == shell_result.shell_id),
                    None,
                )
                if completed is not None:
                    break
                await asyncio.sleep(0.01)
            assert completed is not None
            started = next(
                event for event in session.events
                if event.type == "shell.started" and event.data.get("shell_id") == shell_result.shell_id
            )
            assert started.data["cwd"] == str(linked_a.resolve())
            assert str(linked_a.resolve()) in completed.data["output"].splitlines()
        finally:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
