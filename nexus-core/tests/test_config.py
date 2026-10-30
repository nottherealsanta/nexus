from pathlib import Path

import pytest

from nexus.config import ConfigConflict, ConfigFile


def test_defaults_and_invalid_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = ConfigFile(path)
    assert config.read()["values"]["defaults"]["permission"] == "allow"
    path.write_text('[defaults]\nmodel = "codex/example"\npermission = 3\n')
    result = config.read()
    assert result["values"]["defaults"]["model"] == "codex/example"
    assert result["values"]["defaults"]["permission"] == "allow"
    assert result["errors"] == ["defaults.permission: invalid value"]


def test_agent_model_pools_round_trip_and_validation(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.toml")
    models = {"scout": ["codex/one", "copilot/two"]}
    result = config.patch(config.read()["revision"], {"agents.models": models}, [])
    assert result["values"]["agents"]["models"] == models
    for bad in ({"scout": []}, {"scout": ["codex/one", "codex/one"]},
                {"scout": ["unknown/model"]}, {"scout": ["codex/"]}, {"Bad Name": ["codex/x"]}):
        with pytest.raises(ValueError):
            config.patch(config.read()["revision"], {"agents.models": bad}, [])
    assert config.read()["values"]["agents"]["models"] == models


def test_patch_preserves_comments_and_rejects_stale_revision(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# keep me\n[defaults]\nmodel = "old" # model comment\n')
    config = ConfigFile(path)
    revision = config.read()["revision"]
    config.patch(revision, {"defaults.model": "new"}, [])
    assert "# keep me" in path.read_text()
    assert "# model comment" in path.read_text()
    with pytest.raises(ConfigConflict):
        config.patch(revision, {"defaults.model": "lost"}, [])
    assert config.read()["values"]["defaults"]["model"] == "new"


def test_patch_validates_all_before_writing(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.toml")
    with pytest.raises(ValueError):
        config.patch(config.read()["revision"], {"defaults.model": "new", "defaults.permission": "bad"}, [])
    assert not config.path.exists()


async def test_api_conflicts_and_notifies(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    import httpx

    from nexus.server import build_app, make_runtime
    from nexus.store import Store

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    runtime = make_runtime("example/model", Store(tmp_path / "store.db"))
    app = build_app(runtime, token="token", url="http://test")
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"authorization": "Bearer token"}) as client:
        state = (await client.get("/api/config")).json()
        assert state["schema"]["defaults.permission"]["enum"] == ["allow", "ask"]
        sub = runtime.bus.open()
        body = {"expected_revision": state["revision"], "set": {"defaults.model": "a"}}
        first, second = await asyncio.gather(client.patch("/api/config", json=body),
                                             client.patch("/api/config", json=body))
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        event = await asyncio.wait_for(sub.get(), 1)
        assert event["type"] == "config.changed"
        assert event["data"]["revision"] == (await client.get("/api/config")).json()["revision"]
        config_path = tmp_path / "home" / "config.toml"
        config_path.write_text('[defaults]\nmodel = "external"\n')
        event = await asyncio.wait_for(sub.get(), 3)
        assert event["type"] == "config.changed"
        assert event["data"]["revision"] == (await client.get("/api/config")).json()["revision"]
        sub.close()


async def test_session_defaults_and_explicit_overrides(tmp_path: Path, monkeypatch) -> None:
    import httpx

    from nexus.server import build_app, make_runtime
    from nexus.store import Store

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    (home / "config.toml").write_text('[defaults]\npermission = "ask"\nagent = "orchestrate"\neffort = "high"\n[tools]\ndisabled = ["bash"]\n')
    runtime = make_runtime("codex/model", Store(tmp_path / "store.db"))
    app = build_app(runtime, token="token", url="http://test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"authorization": "Bearer token"}) as client:
        created = await client.post("/api/session", json={"directory": str(tmp_path)})
        assert created.status_code == 200, created.text
        session = created.json()
        assert (session["permission"], session["agent"], session["effort"], session["disabled_tools"]) == (
            "ask", "orchestrate", "high", ["bash"])
        created = await client.post("/api/session", json={"directory": str(tmp_path), "permission": "allow",
                                                           "disabled_tools": [], "agent": "build"})
        assert created.status_code == 200, created.text
        assert created.json()["permission"] == "allow"
        assert created.json()["disabled_tools"] == []
        assert created.json()["agent"] == "build"


async def test_provider_login_and_signout(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    import httpx

    from nexus.llmhub.auth import SIGN_INS
    from nexus.server import build_app, make_runtime
    from nexus.store import Store

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    done = asyncio.Event()

    class FakeAuth:
        methods = ("device",)

        async def login(self, method, notify):
            notify("Open example and enter code 123")
            await done.wait()

        def logout(self):
            pass

    monkeypatch.setitem(SIGN_INS, "codex", FakeAuth)
    runtime = make_runtime("codex/model", Store(tmp_path / "store.db"))
    app = build_app(runtime, token="token", url="http://test")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"authorization": "Bearer token"},
    ) as client:
        assert (await client.post("/api/provider/codex/login", json={})).status_code == 202
        await asyncio.sleep(0)
        state = (await client.get("/api/provider/codex/login")).json()
        assert state["status"] == "pending" and "123" in state["message"]
        assert (await client.post("/api/provider/codex/login", json={})).status_code == 409
        done.set()
        await asyncio.sleep(0)
        assert (await client.get("/api/provider/codex/login")).json()["status"] == "connected"
        assert (await client.delete("/api/provider/codex/login")).status_code == 204
        assert (await client.get("/api/provider/codex/login")).json()["status"] == "idle"


def test_import_mcp_preserves_dialects_and_existing_file(tmp_path: Path) -> None:
    import json

    import tomlkit

    from nexus.config import import_mcp

    source = tmp_path / "mcp.json"
    source.write_text(json.dumps({"mcpServers": {"local": {"command": "foo", "env": {"TOKEN": "secret"}}},
                                  "context_servers": {"remote": {"url": "https://example.test"}}}))
    path = tmp_path / "config.toml"
    path.write_text('# keep\n[defaults]\nmodel = "codex/m"\n')
    config = ConfigFile(path)
    assert import_mcp(source, config) == ["local", "remote"]
    parsed = tomlkit.parse(path.read_text())
    assert parsed["mcp"]["servers"]["local"]["env"]["TOKEN"] == "secret"
    assert "secret" not in str(config.read())
    assert "# keep" in path.read_text()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        import_mcp(source, config)
    assert path.read_bytes() == before


def test_mcp_secrets_mask_and_roundtrip(tmp_path):
    config = ConfigFile(tmp_path / "config.toml")
    result = config.patch(config.read()["revision"], {"mcp.servers": {
        "demo": {"command": "demo", "env": {"KEY": "private"},
                 "headers": {"Authorization": "Bearer private"}},
    }}, [])
    assert "private" not in str(result)
    assert result["values"]["mcp"]["servers"]["demo"]["env"]["KEY"] == "********"
    config.patch(result["revision"], {"mcp.servers": result["values"]["mcp"]["servers"]}, [])
    assert "Bearer private" in config.path.read_text()


def test_shared_ui_preferences_validate_independently(tmp_path):
    config = ConfigFile(tmp_path / "config.toml")
    result = config.patch(config.read()["revision"], {
        "ui.detail": "high", "ui.reduced_motion": True,
    }, [])
    assert result["values"]["ui"] == {"detail": "high", "reduced_motion": True}
    config.path.write_text('[ui]\ndetail = "unknown"\nreduced_motion = true\n')
    result = config.read()
    assert result["values"]["ui"] == {"detail": "medium", "reduced_motion": True}
    assert "ui.detail: invalid value" in result["errors"]
