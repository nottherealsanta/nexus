"""Prompt history over HTTP: what the user sent, per project (PLAN C)."""

from __future__ import annotations

from test_inputs_integration import _session, _wait_until
from test_inputs_integration import setup_inputs as _setup_inputs

from nexus.llmhub.types import Message, Text
from nexus.store import Store

setup_inputs = _setup_inputs


async def _send(client, sid, text, **extra):
    r = await client.post(f"/api/session/{sid}/prompt", json={"text": text, **extra})
    assert r.status_code == 202, r.text


async def _idle(runtime, sid):
    await _wait_until(lambda: sid not in runtime.turns, "turn did not finish")


async def _history(client, project, **params):
    r = await client.get(f"/api/project/{project}/prompts", params=params)
    assert r.status_code == 200, r.text
    return r.json()["prompts"]


async def test_history_is_per_project_newest_first_and_deduplicated(setup_inputs, tmp_path):
    runtime, client, _provider, workspace = setup_inputs
    other = tmp_path / "other"
    other.mkdir()
    first = await _session(client, workspace)
    second = await _session(client, workspace)
    foreign = await _session(client, other)
    for sid, text in [(first, "one\nmulti line"), (second, "two"), (first, "two"), (foreign, "elsewhere")]:
        await _send(client, sid, text)
        await _idle(runtime, sid)
    project = runtime.session(first).project_id
    prompts = await _history(client, project)
    assert [p["text"] for p in prompts] == ["two", "one\nmulti line"]
    assert prompts[0]["session_id"] == first
    assert [p["text"] for p in await _history(client, project, q="MULTI")] == ["one\nmulti line"]
    assert [p["text"] for p in await _history(client, project, before=prompts[0]["id"])] == ["one\nmulti line"]
    assert len(await _history(client, project, limit=1)) == 1


async def test_notices_and_steered_prompts(setup_inputs):
    runtime, client, _provider, workspace = setup_inputs
    sid = await _session(client, workspace)
    runtime._forward_notice(sid, "[Nexus notice: something]")
    await _send(client, sid, "real prompt")
    await _idle(runtime, sid)
    await _send(client, sid, "queued while idle", mode="queue")
    await _idle(runtime, sid)
    texts = [p["text"] for p in await _history(client, runtime.session(sid).project_id)]
    assert texts == ["queued while idle", "real prompt"]


def test_record_prompt_labels_images_out():
    from nexus.llmhub.types import Image
    message = Message("user", (Text("look"), Text("[Image A]"), Image("data:image/png;base64,AA")))
    assert Store._prompt_text(message) == ("look", 1)



async def test_search_finds_titles_and_user_messages(setup_inputs):
    runtime, client, _provider, workspace = setup_inputs
    first = await _session(client, workspace)
    second = await _session(client, workspace)
    await _send(client, first, "refactor the report generator, and also remember the quarterly invoice rounding")
    await _idle(runtime, first)
    await _send(client, second, "hello there")
    await _idle(runtime, second)
    runtime.store.update_session(second, title="Gardening Notes")

    async def hits(**params):
        r = await client.get("/api/search", params=params)
        assert r.status_code == 200, r.text
        return r.json()["hits"]

    found = await hits(q="quart invoice")
    assert [h["session_id"] for h in found] == [first] and found[0]["kind"] == "user"
    assert "[" in found[0]["snippet"] and found[0]["session"]["id"] == first
    assert [h["session_id"] for h in await hits(q="garden")] == [second]
    assert (await hits(q="garden"))[0]["kind"] == "title"
    assert await hits(q="nothing like this") == [] and await hits(q="") == []
    assert await hits(q='"; drop table') == []  # query text is never FTS syntax
    await client.post(f"/api/session/{second}/archive")
    assert await hits(q="garden", archived="exclude") == []
    assert len(await hits(q="garden", archived="only")) == 1
    await client.delete(f"/api/session/{second}")
    assert await hits(q="garden") == []


async def test_messages_page_walks_back_to_the_first_message(setup_inputs):
    runtime, client, _provider, workspace = setup_inputs
    sid = await _session(client, workspace)
    for n in range(3):
        await _send(client, sid, f"turn {n}")
        await _idle(runtime, sid)
    total = len(runtime.store.messages(sid))
    assert total >= 6
    seen: list[int] = []
    before = None
    while True:
        params = {"limit": 2, **({"before": before} if before else {})}
        page = (await client.get(f"/api/session/{sid}/message", params=params)).json()
        assert len(page["messages"]) <= 2
        seen = [m["seq"] for m in page["messages"]] + seen
        if not page["more"]:
            break
        before = page["oldest_seq"]
    assert seen == [m.seq for m in runtime.store.messages(sid)]
    assert len((await client.get(f"/api/session/{sid}/message")).json()) == total  # no params: the whole list
