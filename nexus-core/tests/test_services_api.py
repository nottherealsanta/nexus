"""The services API: `GET /api/services` and the nonblocking search start, with the legacy routes intact."""
import httpx
import pytest
from conftest import remembering
from test_mcp import Scripted

from nexus import runtime as runtime_module
from nexus import searchservice
from nexus.server import build_app, make_runtime
from nexus.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    searchservice._clear_start()
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/m"))
    app = build_app(rt, token="t", url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": "Bearer t"})
    return rt, client


def _absent(monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    monkeypatch.setattr(searchservice, "runtime_status",
                        lambda: {"kind": "container", "state": "ready", "detail": "Apple container"})
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)


async def test_services_reports_the_runtime_and_the_search_row(env, monkeypatch):
    rt, client = env
    _absent(monkeypatch)
    response = await client.get("/api/services")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] == {"kind": "container", "state": "ready", "detail": "Apple container"}
    assert body["services"] == [{"id": "search", "label": "Web search", "state": "absent",
                                 "url": body["services"][0]["url"], "detail": None}]
    await rt.shutdown()


async def test_search_start_returns_202_starting_at_once(env, monkeypatch):
    rt, client = env
    calls = []

    def start_async():
        calls.append(True)
        return {"state": "starting", "url": "http://127.0.0.1:18765/search", "runtime": "container", "detail": "starting"}

    monkeypatch.setattr(searchservice, "start_async", start_async)
    response = await client.post("/api/services/search/start")
    assert response.status_code == 202
    assert response.json() == {"id": "search", "label": "Web search", "state": "starting",
                               "url": "http://127.0.0.1:18765/search", "runtime": "container", "detail": "starting"}
    assert calls == [True]
    await rt.shutdown()


async def test_unknown_services_are_not_managed(env):
    rt, client = env
    assert (await client.post("/api/services/firecrawl/start")).status_code == 404
    await rt.shutdown()


async def test_a_start_that_cannot_run_is_422(env, monkeypatch):
    rt, client = env

    def start_async():
        raise RuntimeError("neither Apple container nor Docker is installed")

    monkeypatch.setattr(searchservice, "start_async", start_async)
    response = await client.post("/api/services/search/start")
    assert response.status_code == 422
    assert response.json() == {"error": "neither Apple container nor Docker is installed", "code": "search_start_failed"}
    await rt.shutdown()


async def test_legacy_status_and_blocking_start_keep_their_shape(env, monkeypatch):
    rt, client = env
    loud = {"state": "stopped", "url": "http://127.0.0.1:18765/search", "runtime": "docker", "detail": None}
    monkeypatch.setattr(searchservice, "status", lambda: loud)
    monkeypatch.setattr(searchservice, "start", lambda: {**loud, "state": "running"})
    assert (await client.get("/api/search/service")).json() == loud
    response = await client.post("/api/search/service/start")
    assert response.status_code == 200
    assert response.json() == {**loud, "state": "running"}
    await rt.shutdown()
