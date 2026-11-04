"""`POST /api/client/view`: ephemeral per-stream views (docs/manager-design.md 10.5, docs/api.md#client-views-ephemeral)."""
from __future__ import annotations

import asyncio
import time

import pytest
from manager_seed import seed
from manager_support import Harness


@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    await harness.close()


async def post(h, stream, **body):
    return await h.client.post("/api/client/view", json={"stream_id": stream.stream_id, "client_id": "tui", **body})


async def test_a_view_is_stored_for_its_stream_and_never_echoes_the_capability(h):
    project = h.project()
    session = seed(h.store, project, title="Work", user="u", now=time.time())
    stream = h.open_stream()
    response = await post(h, stream, project=project.id, session=session.id, panel="files")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["project"] == project.id and body["session"] == session.id and body["panel"] == "files"
    assert body["client_id"] == "tui" and "stream_id" not in body and stream.stream_id not in response.text
    assert h.manager.views.view_for(stream.stream_id)["panel"] == "files"


async def test_a_session_fills_in_its_project_and_empty_values_clear_the_view(h):
    project = h.project()
    session = seed(h.store, project, title="Work", user="u", now=time.time())
    stream = h.open_stream()
    assert (await post(h, stream, session=session.id)).json()["project"] == project.id
    cleared = (await post(h, stream, project="", session=None, panel="")).json()
    assert cleared["project"] is None and cleared["session"] is None and cleared["panel"] is None


async def test_invalid_ids_and_unknown_or_mismatched_references_are_refused(h):
    project = h.project("one")
    other = h.project("two")
    session = seed(h.store, project, title="Work", user="u", now=time.time())
    stream = h.open_stream()
    assert (await post(h, stream, session="has space")).status_code == 400
    assert (await post(h, stream, panel="x" * 200)).status_code == 400
    assert (await post(h, stream, project=5)).status_code == 400
    assert (await h.client.post("/api/client/view", json={"stream_id": stream.stream_id})).status_code == 400
    assert (await post(h, stream, project="prj_nope")).status_code == 404
    assert (await post(h, stream, session="ses_nope")).status_code == 404
    mismatch = await post(h, stream, project=other.id, session=session.id)
    assert mismatch.status_code == 400 and "does not belong" in mismatch.text


async def test_a_closed_or_unknown_stream_is_409_and_a_client_cannot_change_on_a_stream(h):
    project = h.project()
    stream = h.open_stream()
    assert (await post(h, stream, project=project.id)).status_code == 200
    rebind = await h.client.post("/api/client/view", json={"stream_id": stream.stream_id, "client_id": "zz-client",
                                                          "project": project.id})
    assert rebind.status_code == 409 and rebind.json()["code"] == "client_mismatch"
    assert "zz-client" not in rebind.text and stream.stream_id not in rebind.text
    unknown = await h.client.post("/api/client/view", json={"stream_id": "stream_nope", "client_id": "tui"})
    assert unknown.status_code == 409 and unknown.json()["code"] == "stream_closed" and "stream_nope" not in unknown.text
    stream.close()
    await asyncio.sleep(0.02)
    closed = await post(h, stream, project=project.id)
    assert closed.status_code == 409 and closed.json()["code"] == "stream_closed"
    assert h.manager.views.view_for(stream.stream_id) is None


async def test_two_streams_with_one_client_id_keep_separate_views(h):
    project = h.project()
    session = seed(h.store, project, title="Work", user="u", now=time.time())
    first, second = h.open_stream(), h.open_stream([])
    await post(h, first, project=project.id)
    await post(h, second, session=session.id)
    assert h.manager.views.view_for(first.stream_id)["session"] is None
    assert h.manager.views.view_for(second.stream_id)["session"] == session.id
    first.close()
    await asyncio.sleep(0.02)
    assert h.manager.views.view_for(second.stream_id) is not None


async def test_there_is_no_route_that_reads_views(h):
    h.open_stream()
    for method, path in (("get", "/api/client/view"), ("get", "/api/client/views"), ("get", "/api/client")):
        response = await getattr(h.client, method)(path)
        assert response.status_code in (404, 405)
