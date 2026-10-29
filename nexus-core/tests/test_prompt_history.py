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

