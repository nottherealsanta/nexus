"""Scoped Settings file operations at the host boundary."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from nexus.client.protocol import Client, FacadeError
from nexus.host.facade import HostFacade


class _Transport:
    def __init__(self, facade):
        self.facade = facade

    async def request(self, command):
        return await self.facade.handle(command)

    async def aclose(self):
        pass

    async def events(self, *_args, **_kwargs):
        if False:
            yield None


BUILTINS = {"advisor", "build", "orchestrator", "quick", "task"}


def _client(workspace: Path) -> Client:
    runtime = type("RuntimeStub", (), {"workspace": workspace, "extensions": None})()
    return Client(_Transport(HostFacade(runtime)))


def test_settings_inventory_read_write_delete_and_sha_conflict(tmp_path):
    async def scenario():
        client = _client(tmp_path)
        root = tmp_path / ".agents"
        (root / "agents").mkdir(parents=True)
        agent = "---\nname: helper\ndescription: test\n---\nHello.\n"
        (root / "agents" / "helper.md").write_text(agent)
        result = await client.settings_inventory("project")
        assert result.root_display == "<project>/.agents"
        # Built-in agents are listed alongside custom ones.
        assert next(row for row in result.categories if row.key == "agents").count == 1 + len(BUILTINS)
        assert {row.id for row in result.items if row.builtin} == BUILTINS
        item = next(row for row in result.items if row.id == "helper")
        assert item.rel_path == "agents/helper.md"
        read = await client.settings_read("project", "agents", "helper")
        assert read.body == agent
        assert read.sha256 == hashlib.sha256(agent.encode()).hexdigest()
        conflict = await client.settings_write("project", "agents", "helper", agent + "Changed\n", "0" * 64)
        assert conflict.status == "conflict"
        assert (root / "agents" / "helper.md").read_text() == agent
        saved = await client.settings_write("project", "agents", "helper", agent + "Changed\n", read.sha256)
        assert saved.status == "written"
        assert (root / "agents" / "helper.md").read_text().endswith("Changed\n")
        deleted = await client.settings_delete("project", "agents", "helper")
        assert deleted.status == "trashed"
        assert not (root / "agents" / "helper.md").exists()
        assert (root / "trash" / "settings" / deleted.trash_id / "item").exists()

    asyncio.run(scenario())


def test_settings_refuses_invalid_frontmatter_symlinks_and_blocked_paths(tmp_path):
    async def scenario():
        client = _client(tmp_path)
        with pytest.raises(FacadeError):
            await client.settings_write("project", "agents", "bad", "not frontmatter")
        root = tmp_path / ".agents"
        root.mkdir()
        elsewhere = tmp_path / "outside"
        elsewhere.mkdir()
        (root / "agents").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(FacadeError):
            await client.settings_read("project", "agents", "agent")
        with pytest.raises(FacadeError):
            await client.settings_read("project", "agents", "../sessions")

    asyncio.run(scenario())


def test_settings_rejects_symlinked_scope_root_for_every_operation(tmp_path):
    async def scenario():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        (outside / "agents").mkdir(parents=True)
        (outside / "agents" / "helper.md").write_text(
            "---\nname: helper\ndescription: test\n---\n"
        )
        (workspace / ".agents").symlink_to(outside, target_is_directory=True)
        client = _client(workspace)
        with pytest.raises(FacadeError):
            await client.settings_read("project", "agents", "helper")
        with pytest.raises(FacadeError):
            await client.settings_write("project", "agents", "helper", "---\nname: helper\ndescription: test\n---\n")
        with pytest.raises(FacadeError):
            await client.settings_delete("project", "agents", "helper")

    asyncio.run(scenario())


def test_settings_config_round_trip_is_validated(tmp_path, monkeypatch):
    async def scenario():
        client = _client(tmp_path)
        body = "[tools]\nbash_timeout_s = 8\n"
        saved = await client.settings_write("project", "config", "nexus.toml", body)
        assert saved.status == "written"
        assert saved.config_reloaded
        with pytest.raises(FacadeError):
            await client.settings_write("project", "config", "nexus.toml", "[unknown]\nfoo = 2\n")

    asyncio.run(scenario())


def test_every_settings_category_round_trips_in_both_scopes(tmp_path, monkeypatch):
    async def scenario():
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        workspace = tmp_path / "project"
        workspace.mkdir()
        client = _client(workspace)
        contents = {
            "agents": ("helper", "---\nname: helper\ndescription: test\n---\nPrompt.\n"),
            "skills": ("helper", "---\nname: helper\ndescription: test\n---\nSteps.\n"),
            "tools": ("helper", "def register(api):\n    pass\n"),
            "hooks": ("hooks.toml", "enabled = true\n"),
            "mcp": ("mcp.json", '{"mcpServers": {}}\n'),
            "soul": ("SOUL.md", "Be helpful.\n"),
        }
        for scope in ("global", "project"):
            root = home / ".nexus" if scope == "global" else workspace / ".agents"
            for category, (item_id, body) in contents.items():
                if category == "agents":
                    path = root / "agents" / f"{item_id}.md"
                elif category == "skills":
                    path = root / "skills" / item_id / "SKILL.md"
                elif category == "tools":
                    path = root / "tools" / f"{item_id}.py"
                else:
                    path = root / item_id
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            config_id = "config.toml" if scope == "global" else "nexus.toml"
            config_path = root / config_id
            config_path.write_text("config_version = 2\n[agent]\nname = 'general'\n")
            result = await client.settings_inventory(scope)
            assert result.root_display == ("~/.nexus" if scope == "global" else "<project>/.agents")
            assert len([item for item in result.items if not item.builtin]) == 7
            for item in result.items:
                if item.builtin:
                    continue
                read = await client.settings_read(scope, item.category, item.id)
                assert read.body
                assert not str(home) in read.rel_path
                assert read.sha256

    asyncio.run(scenario())


def test_settings_save_preserves_redacted_secret_values(tmp_path):
    async def scenario():
        client = _client(tmp_path)
        root = tmp_path / ".agents"
        root.mkdir()
        source = 'enabled = true\napi_token = "secret-value"\n'
        path = root / "hooks.toml"
        path.write_text(source)
        original = await client.settings_read("project", "hooks", "hooks.toml")
        assert "secret-value" not in original.body
        assert "[redacted]" in original.body
        edited = original.body + "\nnew_value = 3\n"
        result = await client.settings_write(
            "project", "hooks", "hooks.toml", edited, original.sha256
        )
        assert result.status == "written"
        saved = path.read_text()
        assert 'api_token="secret-value"' in saved
        assert "new_value = 3" in saved

    asyncio.run(scenario())


def test_builtin_agent_reads_default_and_saving_creates_a_resettable_override(tmp_path, monkeypatch):
    async def scenario():
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        client = _client(tmp_path / "ws")
        read = await client.settings_read("global", "agents", "task")
        assert read.builtin is True and read.sha256 == ""
        assert "name: task" in read.body
        edited = read.body.replace("contexts: [subagent]", "contexts: [subagent]\nmodel: high\nfallback: [openai/gpt-5]")
        saved = await client.settings_write("global", "agents", "task", edited, read.sha256)
        assert saved.status == "written"
        override = home / ".nexus" / "agents" / "task.md"
        assert "fallback: [openai/gpt-5]" in override.read_text()
        inventory = await client.settings_inventory("global")
        row = next(row for row in inventory.items if row.id == "task")
        assert row.builtin is False and row.overrides_builtin is True
        again = await client.settings_read("global", "agents", "task")
        assert again.overrides_builtin is True and not again.builtin
        await client.settings_delete("global", "agents", "task")
        assert not override.exists()
        reset = await client.settings_read("global", "agents", "task")
        assert reset.builtin is True

    asyncio.run(scenario())


def test_set_toml_key_edits_one_key_and_keeps_the_rest():
    from nexus.host_support.settings_inventory import set_toml_key

    assert set_toml_key("", "agent", "name", "build") == '[agent]\nname = "build"\n'
    body = '# mine\n[models]\ndefault = "x/y"\n'
    assert set_toml_key(body, "agent", "name", "quick") == body + '\n[agent]\nname = "quick"\n'
    body = '[agent]\nprofile = "coding"  # keep\nname = "old"\n\n[context]\nname = "untouched"\n'
    assert set_toml_key(body, "agent", "name", "build") == body.replace('name = "old"', 'name = "build"')
    assert set_toml_key("[agent]\nprofile = \"coding\"\n", "agent", "name", "build") == (
        '[agent]\nname = "build"\nprofile = "coding"\n'
    )


def test_default_agent_setting_applies_to_new_sessions(tmp_path, monkeypatch):
    from nexus.host import protocol as p
    from nexus.runtime import Runtime

    home = tmp_path / "home"
    (home / ".nexus" / "agents").mkdir(parents=True)
    (home / ".nexus" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews\ncontexts: [root]\n---\nReview.\n"
    )
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "nexus.toml").write_text("config_version = 2\n")

    async def scenario():
        facade = HostFacade(Runtime(workspace, home=home))
        listing = await facade.handle(p.AgentsList())
        assert listing.default == "build"
        facade.open_session("before", create=True, recover=True)
        assert (await facade.handle(p.AgentCurrent(session="before"))).name == "build"

        result = await facade.handle(p.AgentDefaultSet(name="reviewer", scope="project"))
        assert isinstance(result, p.AgentDefaultSetResult), result
        assert (result.name, result.effective, result.rel_path) == ("reviewer", "reviewer", "nexus.toml")
        saved = (workspace / ".agents" / "nexus.toml").read_text()
        assert saved.startswith("config_version = 2\n") and 'name = "reviewer"' in saved
        assert (await facade.handle(p.AgentsList())).default == "reviewer"
        facade.open_session("after", create=True, recover=True)
        assert (await facade.handle(p.AgentCurrent(session="after"))).name == "reviewer"

        refused = await facade.handle(p.AgentDefaultSet(name="missing", scope="project"))
        assert isinstance(refused, p.ErrorResult)
        assert 'name = "reviewer"' in (workspace / ".agents" / "nexus.toml").read_text()

    asyncio.run(scenario())
