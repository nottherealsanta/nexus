"""Remote MCP servers: adding one, OAuth sign-in, token refresh, and editing global and project entries.

The remote server is a real streamable-HTTP MCP server with the SDK's own OAuth authorization server behind
it (`fixtures/mcp_remote.py`); nothing about the sign-in is mocked except the browser, which is a GET on the
link Core hands to the client.
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import remembering

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import mcp_remote

from nexus.llmhub.auth.store import CredentialStore
from nexus.mcp import config as mcp_config
from nexus.mcp import oauth_store
from nexus.server import build_app, make_runtime
from nexus.store import Store


@pytest.fixture
async def remote():
    server = await asyncio.to_thread(mcp_remote.serve)
    yield server
    await asyncio.to_thread(server.stop)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/m"))
    app = build_app(rt, token="t", url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": "Bearer t"}, timeout=30)
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, workspace, home


async def sign_in(client, remote, path="/api/mcp/{name}/login", name="r"):
    """Start a sign-in, then be the browser: follow the link so the listener receives the redirect."""
    started = await client.post(path.format(name=name))
    assert started.status_code == 202, started.text
    state = started.json()
    for _ in range(100):
        if state.get("verification_uri"):
            break
        await asyncio.sleep(0.05)
        state = (await client.get(path.format(name=name))).json()
    assert state.get("verification_uri"), state
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as browser:
        landed = await browser.get(state["verification_uri"])
    assert "Signed in" in landed.text, landed.text
    for _ in range(200):
        state = (await client.get(path.format(name=name))).json()
        if state["status"] != "pending":
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(state)


# -- config -----------------------------------------------------------------------

def test_oauth_is_on_for_a_remote_entry_without_its_own_authorization():
    assert mcp_config.parse("a", {"url": "https://h/mcp"}).oauth == {}
    assert mcp_config.parse("a", {"url": "https://h/mcp", "headers": {"authorization": "Bearer x"}}).oauth is None
    assert mcp_config.parse("a", {"url": "https://h/mcp", "oauth": False}).oauth is None
    assert mcp_config.parse("a", {"url": "https://h/mcp", "headers": {"Authorization": "x"}, "oauth": True}).oauth == {}
    table = mcp_config.parse("a", {"url": "https://h/mcp", "oauth": {"client_id": "c", "scope": "read", "redirect_port": 18800}})
    assert table.oauth == {"client_id": "c", "scope": "read", "redirect_port": 18800}
    assert mcp_config.parse("a", {"command": "x"}).oauth is None
    assert mcp_config.parse("a", {"url": "https://h/mcp"}).describe()["oauth"] is True


@pytest.mark.parametrize("oauth,message", [
    ("yes", "oauth must be"), ({"nope": "x"}, "unknown oauth keys"), ({"client_secret": "s"}, "needs oauth.client_id"),
    ({"redirect_port": 80, "client_id": "c"}, "redirect_port"), ({"client_id": ""}, "non-empty"),
])
def test_a_bad_oauth_table_names_the_problem(oauth, message):
    with pytest.raises(mcp_config.McpConfigError, match=message):
        mcp_config.parse("a", {"url": "https://h/mcp", "oauth": oauth})


# -- adding a remote server --------------------------------------------------------

async def test_a_remote_server_with_a_bearer_header_is_added_and_connects(env):
    rt, client, _workspace, home = env
    remote = await asyncio.to_thread(mcp_remote.serve, oauth=False)
    try:
        added = await client.post("/api/mcp", json={"name": "r", "entry": {
            "url": remote.url, "headers": {"Authorization": "Bearer s3cret"}}})
        assert added.status_code == 201, added.text
        row = added.json()
        assert row["server"] == "r" and row["transport"] == "http" and row["oauth"] is False and "auth" not in row
        assert "s3cret" not in added.text
        restarted = (await client.post("/api/mcp/r/restart")).json()
        assert restarted["state"] == "connected" and restarted["tools"] == 1
        # The secret is in config.toml for the daemon's use, and masked everywhere the API serves it.
        assert "Bearer s3cret" in (home / "config.toml").read_text()
        served = (await client.get("/api/config")).json()["values"]["mcp"]["servers"]["r"]
        assert served["headers"]["Authorization"] == "********"
    finally:
        await asyncio.to_thread(remote.stop)
        await rt.mcp.close()


async def test_a_wrong_header_shows_failed_not_signed_in(env):
    rt, client, _workspace, _home = env
    remote = await asyncio.to_thread(mcp_remote.serve, oauth=False)
    try:
        await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url, "headers": {"Authorization": "Bearer nope"}}})
        row = (await client.post("/api/mcp/r/restart")).json()
        assert row["state"] == "failed" and row["error"]
    finally:
        await asyncio.to_thread(remote.stop)
        await rt.mcp.close()


async def test_adding_validates_and_refuses_duplicates(env):
    _rt, client, _workspace, _home = env
    assert (await client.post("/api/mcp", json={"name": "bad name", "entry": {"url": "https://h/mcp"}})).status_code == 400
    assert (await client.post("/api/mcp", json={"name": "a", "entry": {"url": "ftp://h/mcp"}})).status_code == 400
    assert (await client.post("/api/mcp", json={"name": "a", "entry": {"command": "x", "bogus": 1}})).status_code == 400
    assert (await client.post("/api/mcp", json={"name": "a", "entry": "https://h"})).status_code == 400
    assert (await client.post("/api/mcp", json={"entry": {"command": "x"}})).status_code == 400  # no name, no address
    assert (await client.post("/api/mcp", json={"name": 5, "entry": {"url": "https://h/mcp"}})).status_code == 400
    ok = await client.post("/api/mcp", json={"name": "a", "entry": {"url": "https://h/mcp"}})
    assert ok.status_code == 201
    assert (await client.post("/api/mcp", json={"name": "a", "entry": {"url": "https://h/other"}})).status_code == 409


async def test_replace_keeps_a_masked_secret_and_remove_deletes_the_entry(env):
    _rt, client, _workspace, home = env
    await client.post("/api/mcp", json={"name": "a", "entry": {"url": "https://h/mcp", "headers": {"X-Key": "topsecret"}}})
    changed = await client.put("/api/mcp/a", json={"entry": {"url": "https://h/v2", "headers": {"X-Key": "********"}}})
    assert changed.status_code == 200 and changed.json()["url"] == "https://h/v2"
    text = (home / "config.toml").read_text()
    assert "topsecret" in text and "https://h/v2" in text
    assert (await client.put("/api/mcp/missing", json={"entry": {"url": "https://h/mcp"}})).status_code == 404
    assert (await client.delete("/api/mcp/a")).status_code == 200
    assert "[mcp.servers.a]" not in (home / "config.toml").read_text()
    assert (await client.delete("/api/mcp/a")).status_code == 404
    assert (await client.get("/api/mcp")).json()["servers"] == []


async def test_editing_the_config_keeps_its_comments_and_other_settings(env):
    _rt, client, _workspace, home = env
    (home / "config.toml").write_text('# my settings\n[ui]\ndetail = "high"  # keep\n')
    await client.post("/api/mcp", json={"name": "a", "entry": {"url": "https://h/mcp"}})
    text = (home / "config.toml").read_text()
    assert "# my settings" in text and 'detail = "high"  # keep' in text and "[mcp.servers.a]" in text


# -- OAuth -------------------------------------------------------------------------

async def test_oauth_sign_in_connects_and_stores_tokens_outside_the_config(env, remote):
    rt, client, _workspace, home = env
    try:
        row = (await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url}})).json()
        # Adding connects at once, so the row already says the server wants a sign-in.
        assert row["oauth"] is True and row["state"] == "failed" and row["auth"] == "required" and "sign in" in row["error"]

        done = await sign_in(client, remote)
        assert done["status"] == "signed_in", done
        row = next(r for r in (await client.get("/api/mcp")).json()["servers"] if r["server"] == "r")
        assert row["state"] == "connected" and row["auth"] == "signed_in" and row["tools"] == 1

        # Tokens are in auth.json (0600), never in config.toml or any API response.
        stored = [k for k in CredentialStore().names() if k.startswith("mcp-oauth:global:r:")]
        assert len(stored) == 1
        token = CredentialStore().read(stored[0])["tokens"]["access_token"]
        assert token not in (home / "config.toml").read_text()
        for path in ("/api/mcp", "/api/mcp/r", "/api/config"):
            assert token not in (await client.get(path)).text
        assert (home / "auth.json").stat().st_mode & 0o077 == 0
        assert remote.provider.registrations == 1
    finally:
        await rt.mcp.close()


async def test_an_expired_token_is_refreshed_without_asking_the_user(env):
    rt, client, _workspace, _home = env
    remote = await asyncio.to_thread(mcp_remote.serve, ttl=1)
    try:
        await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url}})
        assert (await sign_in(client, remote))["status"] == "signed_in"
        await asyncio.sleep(2.2)  # the access token has expired; a refresh token is stored
        row = (await client.post("/api/mcp/r/restart")).json()
        assert row["state"] == "connected" and row["auth"] == "signed_in", row
        assert remote.provider.refreshes >= 1
    finally:
        await asyncio.to_thread(remote.stop)
        await rt.mcp.close()


async def test_a_revoked_token_asks_for_a_new_sign_in_and_signing_out_forgets_it(env, remote):
    rt, client, _workspace, _home = env
    try:
        await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url}})
        assert (await sign_in(client, remote))["status"] == "signed_in"
        remote.provider.access.clear()
        remote.provider.refresh.clear()
        row = (await client.post("/api/mcp/r/restart")).json()
        assert row["state"] == "failed" and row["auth"] == "required"
        assert (await sign_in(client, remote))["status"] == "signed_in"
        assert (await client.post("/api/mcp/r/restart")).json()["state"] == "connected"
        assert (await client.delete("/api/mcp/r/login")).status_code == 204
        assert not [k for k in CredentialStore().names() if k.startswith("mcp-oauth:")]
        row = (await client.get("/api/mcp/r")).json()
        assert row["state"] != "connected" and row["auth"] == "required"  # the restart after sign-out got a 401
    finally:
        await rt.mcp.close()


async def test_changing_the_address_or_removing_the_server_drops_its_tokens(env, remote):
    rt, client, _workspace, _home = env
    try:
        await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url}})
        assert (await sign_in(client, remote))["status"] == "signed_in"
        assert any(k.startswith("mcp-oauth:") for k in CredentialStore().names())
        await client.put("/api/mcp/r", json={"entry": {"url": remote.url + "/"}})
        assert not [k for k in CredentialStore().names() if k.startswith("mcp-oauth:")]
        assert (await client.get("/api/mcp/r")).json()["auth"] == "none"
        assert (await sign_in(client, remote))["status"] == "signed_in"
        await client.delete("/api/mcp/r")
        assert not [k for k in CredentialStore().names() if k.startswith("mcp-oauth:")]
    finally:
        await rt.mcp.close()


async def test_signing_in_to_a_server_that_does_not_use_oauth_is_refused(env):
    _rt, client, _workspace, _home = env
    await client.post("/api/mcp", json={"name": "h", "entry": {"url": "https://h/mcp", "headers": {"Authorization": "x"}}})
    await client.post("/api/mcp", json={"name": "s", "entry": {"command": "x"}})
    assert (await client.post("/api/mcp/h/login")).status_code == 400
    assert (await client.post("/api/mcp/s/login")).status_code == 400
    assert (await client.post("/api/mcp/none/login")).status_code == 404


async def test_a_failed_sign_in_says_so_and_can_be_retried(env):
    rt, client, _workspace, _home = env
    remote = await asyncio.to_thread(mcp_remote.serve, registration=False)
    try:
        await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url}})
        started = await client.post("/api/mcp/r/login")
        assert started.status_code == 202
        for _ in range(200):
            state = (await client.get("/api/mcp/r/login")).json()
            if state["status"] != "pending":
                break
            await asyncio.sleep(0.05)
        assert state["status"] == "failed" and state["message"] and "verification_uri" not in state or not state.get("verification_uri")
        assert (await client.post("/api/mcp/r/login")).status_code == 202  # not stuck "already running"
        await client.delete("/api/mcp/r/login")
    finally:
        await asyncio.to_thread(remote.stop)
        await rt.mcp.close()


async def test_a_pre_registered_client_signs_in_without_dynamic_registration(env):
    rt, client, _workspace, _home = env
    remote = await asyncio.to_thread(mcp_remote.serve, registration=False)
    try:
        from mcp.shared.auth import OAuthClientInformationFull
        remote.provider.clients["known"] = OAuthClientInformationFull(
            client_id="known", redirect_uris=["http://127.0.0.1:18791/callback"], token_endpoint_auth_method="none", scope="read",
            grant_types=["authorization_code", "refresh_token"], response_types=["code"])
        await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url, "oauth": {"client_id": "known"}}})
        assert (await sign_in(client, remote))["status"] == "signed_in"
        assert remote.provider.registrations == 0
        assert (await client.post("/api/mcp/r/restart")).json()["state"] == "connected"
    finally:
        await asyncio.to_thread(remote.stop)
        await rt.mcp.close()


# -- project servers ---------------------------------------------------------------

async def test_a_project_server_is_written_to_mcp_json_and_signs_in_per_project(env, remote):
    rt, client, workspace, _home = env
    try:
        project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
        base = f"/api/project/{project}/mcp"
        (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"keep": {"command": "x"}}, "extra": 1}))
        added = await client.post(base, json={"name": "r", "entry": {"url": remote.url}})
        assert added.status_code == 201, added.text
        document = json.loads((workspace / ".mcp.json").read_text())
        assert set(document["mcpServers"]) == {"keep", "r"} and document["extra"] == 1
        assert (await client.post(base, json={"name": "r", "entry": {"url": remote.url}})).status_code == 409

        assert (await sign_in(client, remote, f"{base}/{{name}}/login"))["status"] == "signed_in"
        row = (await client.get(f"{base}/r")).json()
        assert row["state"] == "connected" and row["auth"] == "signed_in" and row["source"] == "project"
        assert not [k for k in CredentialStore().names() if k.startswith("mcp-oauth:global:")]

        assert (await client.put(f"{base}/r", json={"entry": {"url": remote.url, "enabled": False}})).status_code == 200
        assert json.loads((workspace / ".mcp.json").read_text())["mcpServers"]["r"]["enabled"] is False
        assert (await client.delete(f"{base}/r")).status_code == 200
        assert "r" not in json.loads((workspace / ".mcp.json").read_text())["mcpServers"]
        assert not [k for k in CredentialStore().names() if k.startswith("mcp-oauth:")]
        assert (await client.delete(f"{base}/r")).status_code == 404
    finally:
        for scope in rt.project_mcps.values():
            await scope.close()
        await rt.mcp.close()


async def test_a_project_name_may_not_shadow_an_enabled_global_server(env):
    _rt, client, workspace, _home = env
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
    await client.post("/api/mcp", json={"name": "g", "entry": {"url": "https://h/mcp"}})
    refused = await client.post(f"/api/project/{project}/mcp", json={"name": "g", "entry": {"url": "https://h/other"}})
    assert refused.status_code == 400 and "global" in refused.json()["error"]
    assert not (workspace / ".mcp.json").exists()


async def test_a_project_file_that_cannot_be_read_strictly_is_never_rewritten(env):
    _rt, client, workspace, _home = env
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
    original = '{"mcpServers": {"a": {"url": "https://h"},}}'  # a trailing comma: refused by the strict reader
    (workspace / ".mcp.json").write_text(original)
    refused = await client.post(f"/api/project/{project}/mcp", json={"name": "b", "entry": {"url": "https://h/mcp"}})
    assert refused.status_code == 400 and "not valid JSON" in refused.json()["error"]
    assert (workspace / ".mcp.json").read_text() == original


async def test_a_project_entry_goes_to_the_file_that_already_defines_it(env):
    _rt, client, workspace, _home = env
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
    (workspace / ".agents").mkdir()
    (workspace / ".agents" / "mcp.json").write_text(json.dumps({"servers": {"a": {"url": "https://h/mcp"}}}))
    assert (await client.put(f"/api/project/{project}/mcp/a", json={"entry": {"url": "https://h/v2"}})).status_code == 200
    assert json.loads((workspace / ".agents" / "mcp.json").read_text())["servers"]["a"]["url"] == "https://h/v2"
    assert not (workspace / ".mcp.json").exists()


def test_oauth_entries_are_keyed_by_scope_name_and_address():
    one = mcp_config.parse("r", {"url": "https://h/mcp"})
    two = mcp_config.parse("r", {"url": "https://h/other"})
    assert oauth_store.key("global", one) != oauth_store.key("global", two)
    assert oauth_store.key("global", one) != oauth_store.key("projects/abc", one)
    assert oauth_store.key("global", one).startswith(oauth_store.prefix("global", "r"))


async def test_patch_sets_one_header_or_a_switch_without_resending_the_entry(env):
    _rt, client, workspace, home = env
    await client.post("/api/mcp", json={"name": "a", "entry": {"url": "https://h/mcp", "headers": {"X-Keep": "one"}}})
    patched = await client.patch("/api/mcp/a", json={"entry": {"headers": {"Authorization": "Bearer abc"}, "enabled": False}})
    assert patched.status_code == 200 and patched.json()["state"] == "disabled"
    assert patched.json()["header_names"] == ["Authorization", "X-Keep"] and patched.json()["oauth"] is False
    text = (home / "config.toml").read_text()
    assert "Bearer abc" in text and '"X-Keep"' in text or "X-Keep" in text
    cleared = await client.patch("/api/mcp/a", json={"entry": {"headers": {"Authorization": None}, "enabled": None}})
    assert cleared.json()["header_names"] == ["X-Keep"] and cleared.json()["oauth"] is True
    assert (await client.patch("/api/mcp/missing", json={"entry": {"enabled": False}})).status_code == 404

    project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
    base = f"/api/project/{project}/mcp"
    made = await client.post(base, json={"name": "p", "entry": {"url": "https://h/mcp", "headers": {"X-Token": "${env:TOK_NOT_SET}"}}})
    assert made.status_code == 201 and made.json()["state"] == "invalid" and "TOK_NOT_SET" in made.json()["error"]
    changed = await client.patch(f"{base}/p", json={"entry": {"headers": {"Authorization": "Bearer ${env:TOK_NOT_SET}"}}})
    assert changed.status_code == 200, changed.text
    document = json.loads((workspace / ".mcp.json").read_text())
    assert document["mcpServers"]["p"]["headers"] == {"X-Token": "${env:TOK_NOT_SET}", "Authorization": "Bearer ${env:TOK_NOT_SET}"}
    for scope in _rt.project_mcps.values():
        await scope.close()


async def test_a_project_file_never_receives_a_literal_credential(env):
    _rt, client, workspace, _home = env
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
    base = f"/api/project/{project}/mcp"
    refused = await client.post(base, json={"name": "p", "entry": {"url": "https://h/mcp", "headers": {"Authorization": "Bearer abc123"}}})
    assert refused.status_code == 400 and "${env:NAME}" in refused.json()["error"] and "abc123" not in refused.text
    assert not (workspace / ".mcp.json").exists()
    fine = await client.post(base, json={"name": "p", "entry": {"url": "https://h/mcp", "headers": {
        "Authorization": "Bearer ${env:TOK_NOT_SET}", "X-Api-Version": "2"}}})
    assert fine.status_code == 201
    again = await client.patch(f"{base}/p", json={"entry": {"headers": {"X-Api-Key": "literal"}}})
    assert again.status_code == 400
    for scope in _rt.project_mcps.values():
        await scope.close()


async def test_a_server_that_rejects_the_token_it_issued_fails_the_sign_in_instead_of_looping(env):
    _rt, client, _workspace, _home = env
    remote = await asyncio.to_thread(mcp_remote.serve, accept_tokens=False)
    try:
        await client.post("/api/mcp", json={"name": "r", "entry": {"url": remote.url}})
        done = await sign_in(client, remote)
        assert done["status"] == "failed" and "did not accept the token" in done["message"], done
        assert (await client.post("/api/mcp/r/login")).status_code == 202  # not stuck "already running"
        await client.delete("/api/mcp/r/login")
    finally:
        await asyncio.to_thread(remote.stop)
        await _rt.mcp.close()
