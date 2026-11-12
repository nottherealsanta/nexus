import asyncio
import base64
import json
import socket

import httpx
import pytest
import uvicorn

from nexus.server import build_app, make_runtime
from nexus.store import Store


def _terminal_app(tmp_path, monkeypatch, *, token="", url="http://127.0.0.1:4747"):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = make_runtime(Store(tmp_path / "db.sqlite"))
    project = runtime.open_project(str(workspace))
    return build_app(runtime, token=token, url=url), runtime, project, workspace


def _terminal_client(app, *, peer=("127.0.0.1", 40000), base_url="http://127.0.0.1:4747", headers=None):
    # The ASGI peer is the actual TCP peer; the base URL only supplies Host.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer), base_url=base_url,
                             headers=headers, timeout=5)


@pytest.fixture
async def terminal_server(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = make_runtime(Store(tmp_path / "db.sqlite"))
    project = runtime.open_project(str(workspace))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    app = build_app(runtime, token="secret", url=url)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    try:
        async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer secret"}, timeout=5) as client:
            yield client, project, workspace
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.fixture
async def tokenless_terminal_server(tmp_path, monkeypatch):
    """The default wildcard listener, reached over real loopback."""
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = make_runtime(Store(tmp_path / "db.sqlite"))
    project = runtime.open_project(str(workspace))
    sock = socket.socket()
    sock.bind(("0.0.0.0", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    app = build_app(runtime, token="", url=url)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    try:
        async with httpx.AsyncClient(base_url=url, timeout=5) as client:
            yield client, project, workspace
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def test_http_two_viewers_control_reconnect_and_close(terminal_server):
    client, project, workspace = terminal_server
    created = await client.post("/api/terminal", json={"project_id": project.id, "workspace": str(workspace),
                                                        "client_id": "one", "rows": 24, "cols": 80})
    assert created.status_code == 201, created.text
    terminal_id = created.json()["id"]
    path = f"/api/terminal/{terminal_id}"
    first = (await client.post(path + "/attach", json={"client_id": "one"})).json()["attachment_id"]
    second = (await client.post(path + "/attach", json={"client_id": "two"})).json()["attachment_id"]
    async with client.stream("GET", path + "/stream", params={"attachment_id": first}) as a:
        assert a.status_code == 200
        async with client.stream("GET", path + "/stream", params={"attachment_id": second}) as b:
            assert b.status_code == 200
            assert (await client.post(path + "/control", json={"attachment_id": first, "action": "acquire"})).status_code == 200
            listing = await client.get("/api/terminal", params={"project_id": project.id})
            assert listing.json()[0]["controller"] == "one"
            assert (await client.post(path + "/control", json={"attachment_id": second, "action": "acquire"})).status_code == 409
            data = base64.b64encode(b"printf HTTPMARKER\\n\n").decode()
            result = await client.post(path + "/input", json={"attachment_id": first, "input_seq": 1, "data": data})
            assert result.status_code == 200, result.text
            observed = b""
            async with asyncio.timeout(5):
                async for line in b.aiter_lines():
                    if line.startswith("data: "):
                        frame = json.loads(line[6:])
                        if frame["type"] == "output":
                            observed += base64.b64decode(frame["data"])
                    if b"HTTPMARKER" in observed:
                        break
    async with asyncio.timeout(5):
        while (await client.get(path)).json()["controller"] is not None:
            await asyncio.sleep(.01)
    third = (await client.post(path + "/attach", json={"client_id": "three"})).json()["attachment_id"]
    async with client.stream("GET", path + "/stream", params={"attachment_id": third, "after": 0}) as replay:
        assert replay.status_code == 200
        async with asyncio.timeout(5):
            async for line in replay.aiter_lines():
                if '"type": "output"' in line:
                    break
    closed = await client.delete(path)
    assert closed.status_code == 200 and closed.json()["status"] == "closed"


async def test_terminal_requires_auth_and_correct_origin(terminal_server):
    client, project, workspace = terminal_server
    body = {"project_id": project.id, "workspace": str(workspace), "client_id": "one"}
    assert (await client.post("/api/terminal", json=body, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await client.post("/api/terminal", json=body, headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_terminal_rename_validation_and_listing(terminal_server):
    client, project, workspace = terminal_server
    created = await client.post("/api/terminal", json={"project_id": project.id, "workspace": str(workspace),
                                                       "client_id": "one"})
    assert created.json()["name"] == "Terminal"
    path = f"/api/terminal/{created.json()['id']}"
    for name in ("", "  ", "a" * 81, "bad\nname", 123, None):
        assert (await client.patch(path, json={"name": name})).status_code == 400
    assert (await client.patch(path, json={"name": "Work", "extra": True})).status_code == 400
    renamed = await client.patch(path, json={"name": "  Work  "})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Work"
    assert (await client.get("/api/terminal", params={"project_id": project.id})).json()[0]["name"] == "Work"
    assert (await client.delete(path)).status_code == 200
    assert (await client.patch(path, json={"name": "Done"})).json()["name"] == "Done"


async def test_tokenless_loopback_terminal_round_trip(tokenless_terminal_server):
    client, project, workspace = tokenless_terminal_server
    body = {"project_id": project.id, "workspace": str(workspace), "client_id": "local"}
    keyed = {"Idempotency-Key": "local-terminal", "Origin": f"http://127.0.0.1:{client.base_url.port}"}
    created = await client.post("/api/terminal", json=body, headers=keyed)
    assert created.status_code == 201, created.text
    terminal_id = created.json()["id"]
    replay = await client.post("/api/terminal", json=body, headers=keyed)
    assert replay.status_code == 201 and replay.headers["idempotent-replayed"] == "true"

    path = f"/api/terminal/{terminal_id}"
    listing = await client.get("/api/terminal", params={"project_id": project.id})
    assert [item["id"] for item in listing.json()] == [terminal_id]
    aid = (await client.post(path + "/attach", json={"client_id": "local"})).json()["attachment_id"]
    async with client.stream("GET", path + "/stream", params={"attachment_id": aid}) as stream:
        assert stream.status_code == 200
        control = await client.post(path + "/control", json={"attachment_id": aid, "action": "acquire"})
        assert control.status_code == 200 and control.json()["controller"] == "local"
        resized = await client.post(path + "/resize", json={"attachment_id": aid, "rows": 30, "cols": 100})
        assert resized.status_code == 200 and resized.json()["rows"] == 30
        data = base64.b64encode(b"printf LOCALTERM\\n\n").decode()
        result = await client.post(path + "/input", json={"attachment_id": aid, "input_seq": 1, "data": data})
        assert result.status_code == 200, result.text
        observed = b""
        async with asyncio.timeout(5):
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    frame = json.loads(line[6:])
                    if frame["type"] == "output":
                        observed += base64.b64decode(frame["data"])
                if b"LOCALTERM" in observed:
                    break
    assert b"LOCALTERM" in observed
    closed = await client.delete(path)
    assert closed.status_code == 200 and closed.json()["status"] == "closed"


@pytest.mark.parametrize(("peer", "base_url"), [
    (("::1", 40000), "http://[::1]:4747"),
    (("::ffff:127.0.0.1", 40000), "http://127.0.0.1:4747"),
])
async def test_tokenless_ipv6_and_mapped_loopback_peers_are_allowed(tmp_path, monkeypatch, peer, base_url):
    app, _, project, workspace = _terminal_app(tmp_path, monkeypatch)
    body = {"project_id": project.id, "workspace": str(workspace), "client_id": "local"}
    async with app.router.lifespan_context(app), _terminal_client(app, peer=peer, base_url=base_url) as client:
        created = await client.post("/api/terminal", json=body)
        assert created.status_code == 201, created.text
        assert (await client.delete("/api/terminal/" + created.json()["id"])).status_code == 200


@pytest.mark.parametrize(("peer", "extra"), [
    (("192.0.2.10", 40000), {}),
    (("::ffff:192.0.2.10", 40000), {}),
    (None, {}),
    (("192.0.2.10", 40000), {"Authorization": "Bearer guessed"}),
    (("127.0.0.1", 40000), {"X-Forwarded-For": "127.0.0.1"}),
    (("127.0.0.1", 40000), {"Forwarded": "for=127.0.0.1"}),
    (("127.0.0.1", 40000), {"X-Real-IP": "127.0.0.1"}),
])
async def test_tokenless_terminal_refuses_remote_missing_and_forwarded_peers(tmp_path, monkeypatch, peer, extra):
    app, _, project, workspace = _terminal_app(tmp_path, monkeypatch)
    body = {"project_id": project.id, "workspace": str(workspace), "client_id": "remote"}
    async with _terminal_client(app, peer=peer, headers=extra) as client:
        assert (await client.get("/api/terminal")).status_code == 403
        assert (await client.post("/api/terminal", json=body)).status_code == 403
        assert (await client.get("/api/terminal/anything")).status_code == 403
        assert (await client.delete("/api/terminal/anything")).status_code == 403
        assert (await client.get("/api/terminal/anything/stream")).status_code == 403
    async with _terminal_client(app) as local:
        assert (await local.get("/api/terminal", params={"project_id": project.id})).json() == []


async def test_tokenless_terminal_requires_loopback_host_and_origin(tmp_path, monkeypatch):
    app, _, project, workspace = _terminal_app(tmp_path, monkeypatch)
    body = {"project_id": project.id, "workspace": str(workspace), "client_id": "local"}
    async with app.router.lifespan_context(app):
        async with _terminal_client(app, base_url="http://evil.example:4747") as forged_host:
            assert (await forged_host.post("/api/terminal", json=body)).status_code == 403
        async with _terminal_client(app, base_url="http://127.0.0.1:9999") as wrong_port:
            assert (await wrong_port.post("/api/terminal", json=body)).status_code == 403
        async with _terminal_client(app) as client:
            for origin in ("http://evil.example:4747", "http://localhost:4747", "null"):
                response = await client.post("/api/terminal", json=body, headers={"Origin": origin})
                assert response.status_code == 403
        async with _terminal_client(app, base_url="http://localhost:4747") as localhost:
            created = await localhost.post("/api/terminal", json=body, headers={"Origin": "http://localhost:4747"})
            assert created.status_code == 201, created.text
            assert (await localhost.delete("/api/terminal/" + created.json()["id"])).status_code == 200


async def test_remote_cannot_replay_a_tokenless_local_terminal_mutation(tmp_path, monkeypatch):
    app, _, project, workspace = _terminal_app(tmp_path, monkeypatch)
    body = {"project_id": project.id, "workspace": str(workspace), "client_id": "local"}
    keyed = {"Idempotency-Key": "local-terminal-replay"}
    async with app.router.lifespan_context(app):
        async with _terminal_client(app) as local:
            created = await local.post("/api/terminal", json=body, headers=keyed)
            assert created.status_code == 201, created.text
            replay = await local.post("/api/terminal", json=body, headers=keyed)
            assert replay.status_code == 201 and replay.headers["idempotent-replayed"] == "true"
            terminal_id = created.json()["id"]
        async with _terminal_client(app, peer=("192.0.2.10", 40000)) as remote:
            for extra in ({}, {"Origin": "http://127.0.0.1:4747"}, {"X-Forwarded-For": "127.0.0.1"}):
                denied = await remote.post("/api/terminal", json=body, headers={**keyed, **extra})
                assert denied.status_code == 403, (extra, denied.text)
                assert "idempotent-replayed" not in denied.headers
        async with _terminal_client(app) as local:
            listing = await local.get("/api/terminal", params={"project_id": project.id})
            assert [item["id"] for item in listing.json()] == [terminal_id]
            assert (await local.delete("/api/terminal/" + terminal_id)).status_code == 200


async def test_token_mode_requires_bearer_and_keeps_the_origin_check(tmp_path, monkeypatch):
    app, _, project, workspace = _terminal_app(tmp_path, monkeypatch, token="secret")
    body = {"project_id": project.id, "workspace": str(workspace), "client_id": "remote"}
    remote = ("192.0.2.10", 40000)
    async with app.router.lifespan_context(app):
        async with _terminal_client(app, peer=remote) as anonymous:
            assert (await anonymous.post("/api/terminal", json=body)).status_code == 401
        async with _terminal_client(app, peer=remote, headers={"Authorization": "Bearer wrong"}) as wrong:
            assert (await wrong.post("/api/terminal", json=body)).status_code == 401
        async with _terminal_client(app, peer=remote, headers={"Authorization": "Bearer secret"}) as client:
            evil = await client.post("/api/terminal", json=body, headers={"Origin": "https://evil.example"})
            assert evil.status_code == 403
            created = await client.post("/api/terminal", json=body)
            assert created.status_code == 201, created.text
            assert (await client.delete("/api/terminal/" + created.json()["id"])).status_code == 200


async def test_http_announces_discard_on_reconnect(terminal_server, monkeypatch):
    from nexus import terminals

    monkeypatch.setattr(terminals, "RING_BYTES", 4096)
    client, project, workspace = terminal_server
    created = await client.post("/api/terminal", json={"project_id": project.id, "workspace": str(workspace),
                                                        "client_id": "one"})
    path = f"/api/terminal/{created.json()['id']}"
    aid = (await client.post(path + "/attach", json={"client_id": "one"})).json()["attachment_id"]
    async with client.stream("GET", path + "/stream", params={"attachment_id": aid}) as stream:
        assert stream.status_code == 200
        await client.post(path + "/control", json={"attachment_id": aid, "action": "acquire"})
        command = b"head -c 30000 /dev/zero | tr '\\000' X; echo FINISHED\n"
        result = await client.post(path + "/input", json={"attachment_id": aid, "input_seq": 1,
                                                          "data": base64.b64encode(command).decode()})
        assert result.status_code == 200
        async with asyncio.timeout(5):
            async for line in stream.aiter_lines():
                if line.startswith("data: ") and json.loads(line[6:])["type"] == "discard":
                    break
    new_id = (await client.post(path + "/attach", json={"client_id": "two"})).json()["attachment_id"]
    async with client.stream("GET", path + "/stream", params={"attachment_id": new_id, "after": 0}) as stream:
        async with asyncio.timeout(5):
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    gap = json.loads(line[6:])
                    assert gap["type"] == "gap" and gap["discarded"] > 0
                    assert base64.b64decode(gap["reset"]) == b"\x1bc"
                    break
    await client.delete(path)


async def test_http_created_terminal_is_closed_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    runtime = make_runtime(Store(tmp_path / "db.sqlite"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = runtime.open_project(str(workspace))
    app = build_app(runtime, token="secret", url="http://test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer secret"}) as client:
        async with app.router.lifespan_context(app):
            response = await client.post("/api/terminal", json={"project_id": project.id,
                                                                 "workspace": str(workspace), "client_id": "one"})
            assert response.status_code == 201
            path = f"/api/terminal/{response.json()['id']}"
        assert (await client.get(path)).json()["status"] == "closed"


async def test_background_job_creates_no_terminal_resource(tmp_path, monkeypatch):
    from nexus.tools import ToolContext

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    runtime = make_runtime(Store(tmp_path / "db.sqlite"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = runtime.open_project(str(workspace))
    session = runtime.create_session(project, title=None, model=None, permission=None)
    finished = []
    runtime._job_finished = finished.append
    app = build_app(runtime, token="secret", url="http://test")
    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                  headers={"Authorization": "Bearer secret"}) as client,
                app.router.lifespan_context(app)):
        await runtime._start_job(session, session, {"command": "sleep 30"}, ToolContext(workspace))
        job = runtime.jobs.list()[0]
        listing = await client.get("/api/terminal", params={"project_id": project.id})
        assert listing.json() == []
        assert "terminal_id" not in job.json()
        await runtime.jobs.kill(job.id)
        assert job.status == "killed" and finished == [job]


async def test_closing_an_exited_terminal_hides_it_and_is_idempotent(terminal_server):
    client, project, workspace = terminal_server
    created = await client.post("/api/terminal", json={"project_id": project.id, "workspace": str(workspace),
                                                       "client_id": "one"})
    path = f"/api/terminal/{created.json()['id']}"
    aid = (await client.post(path + "/attach", json={"client_id": "one"})).json()["attachment_id"]
    async with client.stream("GET", path + "/stream", params={"attachment_id": aid}) as stream:
        assert stream.status_code == 200
        assert (await client.post(path + "/control", json={"attachment_id": aid, "action": "acquire"})).status_code == 200
        result = await client.post(path + "/input", json={"attachment_id": aid, "input_seq": 1,
                                                          "data": base64.b64encode(b"exit\n").decode()})
        assert result.status_code == 200
        async with asyncio.timeout(5):
            while (await client.get(path)).json()["status"] != "exited":
                await asyncio.sleep(.02)
    for _ in range(2):
        closed = await client.delete(path)
        assert closed.status_code == 200 and closed.json()["status"] == "closed"
    listing = await client.get("/api/terminal", params={"project_id": project.id})
    assert [t["status"] for t in listing.json()] == ["closed"]


async def test_user_stopped_job_does_not_wake_idle_session(tmp_path, monkeypatch):
    from nexus.tools import ToolContext

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    runtime = make_runtime(Store(tmp_path / "db.sqlite"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = runtime.open_project(str(workspace))
    session = runtime.create_session(project, title=None, model=None, permission=None)
    prompts = []
    monkeypatch.setattr(runtime, "prompt", lambda *a, **k: prompts.append(a))
    app = build_app(runtime, token="secret", url="http://test")
    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                  headers={"Authorization": "Bearer secret"}) as client,
                app.router.lifespan_context(app)):
        await runtime._start_job(session, session, {"command": "sleep 30"}, ToolContext(workspace))
        job = runtime.jobs.list()[0]
        assert (await client.delete(f"/api/bash/{job.id}")).status_code == 204
    assert prompts == []
    assert any("Background Shell Done" in str(m.message) for m in runtime.store.messages(session.id))
