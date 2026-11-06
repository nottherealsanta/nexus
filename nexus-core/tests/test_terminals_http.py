import asyncio
import base64
import json
import socket

import httpx
import pytest
import uvicorn

from nexus.server import build_app, make_runtime
from nexus.store import Store


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


async def test_tokenless_non_loopback_refuses_terminal_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    app = build_app(make_runtime(Store(tmp_path / "db.sqlite")), token="", url="http://0.0.0.0:4747")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/terminal")).status_code == 403
        assert (await client.post("/api/terminal", json={})).status_code == 403
        assert (await client.get("/api/terminal/anything/stream")).status_code == 403


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
