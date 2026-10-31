"""Project `.mcp.json`: automatic enabled servers, isolation, collisions and strict parsing.

Every test writes its own file under `tmp_path`. No repository or user `.mcp.json` is
read, and the autouse home fixture points every log and path at the test's temporary
home. Commands are either nonexistent (so they fail before running anything) or the
transport is monkeypatched.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import mcp
import pytest

from nexus.mcp import config as mcp_config
from nexus.mcp import manager as manager_module
from nexus.mcp.manager import McpUnavailable
from nexus.mcp.project import ProjectMcp


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))


def write_file(path: Path, servers: dict[str, object]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def project_for(path: Path, project_id: str = "p-1", *,
                reserved: set[str] | None = None) -> ProjectMcp:
    names = reserved if reserved is not None else set()
    return ProjectMcp(project_id, str(path), lambda *_: None, reserved=lambda: set(names))


async def test_enabled_project_servers_are_loaded_and_start_without_approval(tmp_path):
    path = tmp_path / "project"
    write_file(path, {
        "shell": {"command": "/nonexistent/project-server", "args": ["--shared"], "connect_timeout_s": 1},
        "off": {"command": "/nonexistent/off-server", "enabled": False},
    })
    project = project_for(path)

    definition = project.refresh()
    assert definition.exists and definition.error is None
    assert set(definition.servers) == {"shell", "off"}
    assert project.names() == ["shell"]
    assert project.names(enabled_only=False) == ["off", "shell"]
    assert set(project.manager.connections) == {"shell", "off"}
    assert project.row("shell")["state"] == "idle"
    assert project.row("off")["state"] == "disabled"
    assert "pending" not in project.row("shell")

    # A new session asks for the enabled names; the disabled entry is filtered out.
    result = await project.connect_all(["shell", "off"])
    assert set(result) == {"shell"} and result["shell"] is not None
    assert project.manager.connections["off"].state == "idle"
    with pytest.raises(McpUnavailable, match="switched off"):
        project.connection("off")
    await project.close()


async def test_a_disabled_project_entry_is_never_started(tmp_path):
    path = tmp_path / "project"
    write_file(path, {"off": {"command": "/nonexistent/off-server", "enabled": False}})
    project = project_for(path)

    assert project.names() == [] and project.names(enabled_only=False) == ["off"]
    assert await project.connect_all(["off"]) == {}
    assert project.manager.connections["off"].state == "idle"
    with pytest.raises(McpUnavailable, match="switched off"):
        project.connection("off")
    await project.close()


async def test_changing_or_removing_an_entry_reconciles_the_connection(tmp_path):
    path = tmp_path / "project"
    write_file(path, {"shell": {"command": "/nonexistent/one"},
                      "kept": {"command": "/nonexistent/kept"}})
    project = project_for(path)
    project.refresh()
    old = project.manager.connections["shell"]
    kept = project.manager.connections["kept"]

    write_file(path, {"shell": {"command": "/nonexistent/two"}, "kept": {"command": "/nonexistent/kept"}})
    definition = project.refresh()
    current = project.manager.connections["shell"]
    assert definition.servers["shell"].command == "/nonexistent/two"
    assert current is not old and old._retired
    assert project.manager.connections["kept"] is kept

    write_file(path, {"kept": {"command": "/nonexistent/kept"}})
    project.refresh()
    assert set(project.manager.connections) == {"kept"}
    assert "shell" not in project.names() and "shell" not in project.known_names()
    await project.close()


def test_two_projects_with_the_same_server_name_are_isolated(tmp_path):
    entry = {"command": "/nonexistent/shared", "args": ["--same"], "cwd": str(tmp_path / "shared")}
    path_a, path_b = tmp_path / "a", tmp_path / "b"
    write_file(path_a, {"shell": entry})
    write_file(path_b, {"shell": entry})
    project_a = project_for(path_a, "project-a")
    project_b = project_for(path_b, "project-b")

    project_a.refresh()
    project_b.refresh()
    assert project_a.names() == project_b.names() == ["shell"]
    assert project_a.manager.connections["shell"] is not project_b.manager.connections["shell"]

    # A file change in one project does not touch the other's definition or connection.
    write_file(path_a, {"shell": {**entry, "args": ["--changed"]}})
    project_a.refresh()
    assert project_a.manager.connections["shell"].config.args == ("--changed",)
    assert project_b.manager.connections["shell"].config.args == ("--same",)
    assert project_a.project_id != project_b.project_id


def test_a_project_entry_cannot_shadow_an_enabled_global_server(tmp_path):
    path = tmp_path / "project"
    write_file(path, {"shell": {"command": "/nonexistent/project-server"}})
    project = project_for(path, reserved={"shell"})

    definition = project.refresh()
    assert definition.servers == {}
    assert "already used by a global MCP server" in definition.invalid["shell"]
    assert project.names() == []
    assert project.manager.connections == {}
    row = project.row("shell")
    assert row is not None and row["state"] == "invalid"


def test_reserved_names_follow_global_enable_and_disable_without_a_file_change(tmp_path):
    """A project entry may use a disabled global name, but enabling it takes the name back."""
    path = tmp_path / "project"
    write_file(path, {"shell": {"command": "/nonexistent/project-server"}})
    reserved: set[str] = set()
    project = project_for(path, reserved=reserved)

    definition = project.refresh()
    assert set(definition.servers) == {"shell"} and definition.invalid == {}
    assert project.names() == ["shell"]
    assert "shell" in project.manager.connections

    # The global entry is enabled: it now reserves the name, so the project entry is invalid
    # and its connection is closed. The definition cache must notice this even though neither
    # the project file nor an approval changed.
    reserved.add("shell")
    definition = project.refresh()
    assert definition.servers == {}
    assert "already used by a global MCP server" in definition.invalid["shell"]
    assert project.manager.connections == {}
    assert project.row("shell")["state"] == "invalid"

    # Disabling the global entry frees the name again; the project entry is valid and a new
    # session can start it.
    reserved.clear()
    definition = project.refresh()
    assert set(definition.servers) == {"shell"} and definition.invalid == {}
    assert set(project.manager.connections) == {"shell"}
    assert project.row("shell")["state"] == "idle"


def test_a_name_defined_in_two_wrappers_is_rejected_without_starting_anything(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    (path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"dup": {"command": "/nonexistent/server"}},
        "servers": {"dup": {"command": "/nonexistent/server"}},
    }), encoding="utf-8")
    project = project_for(path)

    definition = project.refresh()
    assert definition.error is not None and "defines server 'dup' more than once" in definition.error
    assert definition.servers == {} and definition.invalid == {}
    assert project.names() == [] and project.manager.connections == {}
    assert project.status()[0]["state"] == "invalid"


async def test_a_whole_file_error_closes_its_connections(tmp_path):
    path = tmp_path / "project"
    write_file(path, {"shell": {"command": "/nonexistent/server"}})
    project = project_for(path)
    project.refresh()
    assert set(project.manager.connections) == {"shell"}

    (path / ".mcp.json").write_text("{not json", encoding="utf-8")
    definition = project.refresh()
    assert "not valid JSON" in definition.error
    assert definition.servers == {} and project.manager.connections == {}
    assert project.status()[0]["server"] == "(file)" and project.status()[0]["state"] == "invalid"


def test_a_guard_judges_the_configuration_after_the_manager_reconciled(tmp_path, monkeypatch):
    """A file change while a start is being authorized leaves the captured entry stale."""
    path = tmp_path / "project"
    write_file(path, {"shell": {"command": "/nonexistent/one", "args": ["--current"]}})
    project = project_for(path)
    project.refresh()
    captured = project.manager.connections["shell"].config

    configure = project.manager.configure

    def configure_after_the_file_changes():
        # Simulate the file changing after the project scope read the entry but before the
        # manager finished reconciling. The captured connection is now stale.
        write_file(path, {"shell": {"command": "/nonexistent/two", "args": ["--changed"]}})
        configure()

    monkeypatch.setattr(project.manager, "configure", configure_after_the_file_changes)

    with pytest.raises(McpUnavailable, match="changed"):
        project._require(captured)
    assert project.manager.connections["shell"].config.command == "/nonexistent/two"


async def test_a_connection_retired_by_its_guard_never_reaches_the_transport(monkeypatch):
    created = []
    monkeypatch.setattr(manager_module, "_transport", lambda config, _errlog: created.append(config.name))
    config = mcp_config.parse("shell", {"command": "fake-server"})
    connection = manager_module.Connection(config, lambda *_: None)
    connection.guard = lambda _config: connection.retire()

    with pytest.raises(McpUnavailable, match="changed"):
        await connection.connect()

    assert created == []
    await connection.close()


async def test_retiring_a_slow_start_cancels_it_before_the_process_starts(monkeypatch):
    entered = asyncio.Event()
    started = []

    class SlowTransport:
        async def __aenter__(self):
            entered.set()
            await asyncio.sleep(0.2)
            started.append("started")
            return object()

        async def __aexit__(self, *_exc):
            return None

    class FakeClient:
        def __init__(self, transport, **_kwargs):
            self.transport = transport

        async def __aenter__(self):
            await self.transport.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self.transport.__aexit__(*exc)

    monkeypatch.setattr(manager_module, "_transport", lambda *_args: SlowTransport())
    monkeypatch.setattr(mcp, "Client", FakeClient)
    config = mcp_config.parse("shell", {"command": "fake-server"})
    connection = manager_module.Connection(config, lambda *_: None)
    starting = asyncio.create_task(connection.connect())

    await asyncio.wait_for(entered.wait(), 1)
    connection.retire()
    with pytest.raises(McpUnavailable):
        await starting
    await connection.close()

    assert started == []
