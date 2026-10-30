"""Session export: Markdown, JSON, scopes, snapshots, boundaries and the CLI.

Everything runs offline with scripted providers, temporary homes/workspaces and
direct store fixtures. No provider is called by an export.
"""
import argparse
import asyncio
import io
import json
import time
from contextlib import asynccontextmanager, redirect_stdout

import httpx
import pytest
from conftest import remembering
from starlette.requests import ClientDisconnect

from nexus import runtime as runtime_module
from nexus.cli import _export
from nexus.export import FORMAT_VERSION, ExportView, json_export, markdown_export, stream
from nexus.llmhub.types import Done, Message, PartDone, Reasoning, Text, ToolCall, ToolResult
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self):
        self.replies = []
        self.requests = []
        self.gates = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages)}

    async def stream(self, req):
        self.requests.append(req)
        gate = self.gates.pop(0) if self.gates else None
        if gate is not None:
            await gate.wait()
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


class Env:
    def __init__(self, rt, client, provider, workspace, store, tmp_path):
        self.rt, self.client, self.provider, self.workspace = rt, client, provider, workspace
        self.store, self.tmp_path = store, tmp_path

    def __iter__(self):
        return iter((self.rt, self.client, self.provider, self.workspace))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    store = Store(tmp_path / "nexus.db")
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    return Env(rt, client, provider, workspace, store, tmp_path)


async def _create(client, workspace, **fields):
    response = await client.post("/api/session", json={"directory": str(workspace), **fields})
    assert response.status_code == 200, response.text
    return response.json()


async def _child(client, parent, **fields):
    response = await client.post("/api/session", json={"parent": parent, **fields})
    assert response.status_code == 200, response.text
    return response.json()


async def _idle(rt, session_id=None):
    for _ in range(500):
        if session_id is None:
            if not rt.turns:
                return
        elif session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


def _message(session_id, role, *parts):
    return Message(role, tuple(parts))


def _database_rows(store):
    """Every logical row that an export must not change."""
    return {
        table: [tuple(row) for row in store.db.execute(f"select * from {table} order by rowid")]
        for table in ("project", "session", "session_snapshot", "message", "event", "file_observation")
    }


# -- formats -----------------------------------------------------------------


async def test_markdown_and_json_formats(env):
    _, client, provider, workspace = env
    session = await _create(client, workspace)
    sid = session["id"]
    env.store.append_message(sid, _message(sid, "user", Text("Please inspect a.txt")))
    env.store.append_message(sid, _message(
        sid, "assistant",
        Reasoning("Need to read a.txt", origin="codex", raw={"encrypted_content": "ciphertext", "id": "rs1"}),
        ToolCall("call_1", "read", '{"path": "a.txt"}', origin="codex", raw={"type": "function_call", "call_id": "call_1"}),
    ))
    env.store.append_message(sid, _message(
        sid, "tool", ToolResult("call_1", "     1\thello\n[clipped: 20 more lines]", is_error=False)))
    env.store.append_message(sid, _message(
        sid, "user", Text("[Nexus notice: tool availability changed] bash is now switched off.")))

    markdown = await client.get(f"/api/session/{sid}/export", params={"format": "markdown"})
    assert markdown.status_code == 200
    assert markdown.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "attachment" in markdown.headers["content-disposition"]
    assert markdown.headers["content-disposition"].endswith('.md"')
    text = markdown.text
    assert text.startswith("# Nexus session export")
    assert f"## Session `{sid}`" in text
    assert "### Stored context, in order" in text
    assert "#### Message 1 · user" in text
    assert "Please inspect a.txt" in text
    assert "Readable reasoning summary:" in text and "Need to read a.txt" in text
    assert "Encrypted reasoning: present (10 characters of encrypted state) and not readable" in text
    assert "Tool call" in text and "Tool result" in text
    assert '"path": "a.txt"' in text
    assert "[clipped: 20 more lines]" in text  # the stored clip notice is kept
    assert "Kind: context notice stored as a user message" in text
    assert "origin `codex` · raw wire item: present" in text

    structured = await client.get(f"/api/session/{sid}/export", params={"format": "json"})
    assert structured.status_code == 200
    assert structured.headers["content-type"] == "application/json; charset=utf-8"
    assert structured.headers["x-nexus-export-format-version"] == str(FORMAT_VERSION)
    assert structured.headers["content-disposition"].endswith('.json"')
    document = structured.json()
    assert document["format"] == "nexus.session-export"
    assert document["format_version"] == FORMAT_VERSION
    assert document["scope"] == "session" and document["root"] == sid
    assert document["state"]["durable_partial"] is False
    assert document["boundary"]["event_id"] >= 1
    assert document["boundary"]["counts"][sid]["messages"] == 4
    (exported,) = document["sessions"]
    assert exported["session"]["id"] == sid and exported["session"]["archived"] is False
    assert exported["snapshot"]["origin"] == "created"
    assert exported["snapshot_notice"].startswith("Recorded when the session was created")
    assert [m["role"] for m in exported["messages"]] == ["user", "assistant", "tool", "user"]
    assert [m["seq"] for m in exported["messages"]] == [1, 2, 3, 4]
    assert all(m["id"].startswith("msg_") for m in exported["messages"])
    reasoning = exported["messages"][1]["parts"][0]
    assert reasoning["type"] == "reasoning" and reasoning["origin"] == "codex"
    assert reasoning["raw"]["encrypted_content"] == "ciphertext"
    call = exported["messages"][1]["parts"][1]
    assert call["type"] == "tool_call" and call["origin"] == "codex"
    assert call["raw"] == {"type": "function_call", "call_id": "call_1"}
    assert exported["messages"][2]["parts"][0]["output"].endswith("[clipped: 20 more lines]")
    assert [e["id"] for e in exported["events"]] == sorted(e["id"] for e in exported["events"])
    assert provider.requests == []  # the export never called a provider


# -- scopes and child separation ---------------------------------------------


async def test_session_and_tree_scopes_keep_child_contexts_separate(env):
    _, client, _, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"], agent="build")
    grandchild = await _child(client, child["id"], agent="build")
    sibling = await _child(client, root["id"], agent="build")
    for session, label in ((root, "ROOT_TEXT"), (child, "CHILD_TEXT"),
                           (grandchild, "LEAF_TEXT"), (sibling, "SIBLING_TEXT")):
        env.store.append_message(session["id"], _message(session["id"], "user", Text(label)))

    only_root = (await client.get(f"/api/session/{root['id']}/export",
                                  params={"format": "json", "scope": "session"})).json()
    assert [s["session"]["id"] for s in only_root["sessions"]] == [root["id"]]
    assert only_root["sessions"][0]["tree"]["parent_in_export"] is False
    assert only_root["sessions"][0]["tree"]["children"] == [child["id"], sibling["id"]]
    assert only_root["sessions"][0]["tree"]["children_in_export"] == []

    whole_tree = (await client.get(f"/api/session/{root['id']}/export",
                                   params={"format": "json", "scope": "tree"})).json()
    # Depth-first, parent before its own subtree, then the next sibling.
    assert [s["session"]["id"] for s in whole_tree["sessions"]] == [
        root["id"], child["id"], grandchild["id"], sibling["id"]]
    assert [s["tree"]["depth"] for s in whole_tree["sessions"]] == [0, 1, 2, 1]
    assert whole_tree["sessions"][1]["tree"]["parent_in_export"] is True
    assert whole_tree["sessions"][0]["tree"]["children_in_export"] == [child["id"], sibling["id"]]
    assert whole_tree["sessions"][1]["tree"]["children_in_export"] == [grandchild["id"]]
    for exported, own, other in (
        (whole_tree["sessions"][0], "ROOT_TEXT", ("CHILD_TEXT", "LEAF_TEXT")),
        (whole_tree["sessions"][1], "CHILD_TEXT", ("ROOT_TEXT", "LEAF_TEXT")),
        (whole_tree["sessions"][2], "LEAF_TEXT", ("ROOT_TEXT", "CHILD_TEXT")),
        (whole_tree["sessions"][3], "SIBLING_TEXT", ("ROOT_TEXT", "CHILD_TEXT", "LEAF_TEXT")),
    ):
        text = json.dumps(exported["messages"])
        assert own in text
        assert not any(value in text for value in other)

    markdown = (await client.get(f"/api/session/{root['id']}/export",
                                 params={"format": "markdown", "scope": "tree"})).text
    root_at = markdown.index(f"## Session `{root['id']}`")
    child_at = markdown.index(f"## Session `{child['id']}`")
    grandchild_at = markdown.index(f"## Session `{grandchild['id']}`")
    sibling_at = markdown.index(f"## Session `{sibling['id']}`")
    assert root_at < child_at < grandchild_at < sibling_at
    assert f"    - `{grandchild['id']}`" in markdown  # nested under its own parent
    assert f"  - `{sibling['id']}`" in markdown
    assert "ROOT_TEXT" in markdown[root_at:child_at]
    assert "CHILD_TEXT" not in markdown[root_at:child_at]
    assert "CHILD_TEXT" in markdown[child_at:grandchild_at]
    assert "ROOT_TEXT" not in markdown[child_at:grandchild_at]
    assert "LEAF_TEXT" in markdown[grandchild_at:sibling_at]
    assert "SIBLING_TEXT" in markdown[sibling_at:]

    subtree = (await client.get(f"/api/session/{child['id']}/export",
                                params={"format": "json", "scope": "tree"})).json()
    assert [s["session"]["id"] for s in subtree["sessions"]] == [child["id"], grandchild["id"]]
    assert subtree["sessions"][0]["tree"]["parent_in_export"] is False


# -- archive, empty sessions and missing snapshots ---------------------------


async def test_archived_sessions_export_without_being_unarchived(env):
    _, client, _, workspace = env
    root = await _create(client, workspace)
    await _child(client, root["id"], agent="build")
    env.store.append_message(root["id"], _message(root["id"], "user", Text("kept while archived")))
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200

    markdown = await client.get(f"/api/session/{root['id']}/export", params={"format": "markdown", "scope": "tree"})
    assert markdown.status_code == 200 and "kept while archived" in markdown.text
    structured = await client.get(f"/api/session/{root['id']}/export", params={"format": "json", "scope": "tree"})
    assert structured.status_code == 200
    document = structured.json()
    assert all(s["session"]["archived"] is True for s in document["sessions"])
    # Reading for export did not unarchive anything.
    assert env.store.session(root["id"]).archived == 1
    assert (await client.get(f"/api/session/{root['id']}")).json()["archived"] is True


async def test_empty_session_with_a_snapshot(env):
    _, client, _, workspace = env
    session = await _create(client, workspace)
    document = (await client.get(f"/api/session/{session['id']}/export",
                                 params={"format": "json"})).json()
    exported = document["sessions"][0]
    assert exported["messages"] == [] and exported["counts"] == {"messages": 0, "events": 1}
    assert exported["snapshot"] is not None
    markdown = (await client.get(f"/api/session/{session['id']}/export",
                                 params={"format": "markdown"})).text
    assert "No messages were stored for this session." in markdown


def test_missing_snapshot_is_labeled_and_not_reconstructed(tmp_path):
    store = Store(tmp_path / "nexus.db")
    project, _ = store.project_for_path(str(tmp_path))
    session = store.create_session(project, title="old session", agent="build")
    store.append_message(session.id, Message("user", (Text("old text"),)))
    before = _database_rows(store)

    view = ExportView(store.path, session.id, "session")
    try:
        document = json.loads("".join(json_export(view)))
    finally:
        view.close()
    exported = document["sessions"][0]
    assert exported["snapshot"] is None and exported["snapshot_origin"] is None
    assert "does not reconstruct" in exported["snapshot_notice"]
    assert exported["messages"][0]["parts"][0]["text"] == "old text"
    assert _database_rows(store) == before  # no snapshot written, no event appended
    assert store.snapshot(session.id) is None

    view = ExportView(store.path, session.id, "session")
    try:
        markdown = "".join(markdown_export(view))
    finally:
        view.close()
    assert "Snapshot: none stored" in markdown
    assert "does not reconstruct" in markdown


async def test_agent_switch_snapshot_is_not_presented_as_the_original_prompt(env):
    _, client, _, workspace = env
    session = await _create(client, workspace, agent="build")
    sid = session["id"]
    env.store.append_message(sid, _message(sid, "user", Text("before the switch")))
    switched = await client.post(f"/api/session/{sid}/agent", json={"agent": "orchestrate"})
    assert switched.status_code == 200
    assert switched.json()["agent"] == "orchestrate"

    document = (await client.get(f"/api/session/{sid}/export", params={"format": "json"})).json()
    exported = document["sessions"][0]
    assert exported["snapshot_origin"] == "agent-switch"
    assert exported["snapshot_created"] == exported["snapshot"]["created"]
    assert "not the session's original historical prompt" in exported["snapshot_notice"]
    assert "before the switch" in json.dumps(exported["messages"])
    markdown = (await client.get(f"/api/session/{sid}/export", params={"format": "markdown"})).text
    assert "origin `agent-switch`" in markdown
    assert "not the session's original historical prompt" in markdown
    assert "before the switch" in markdown


async def test_reconstructed_snapshot_notice_is_exported(env):
    _, client, _, workspace = env
    project, _ = env.store.project_for_path(str(workspace))
    session = env.store.create_session(project, title="old", agent="build")
    env.store.append_message(session.id, _message(session.id, "user", Text("old history")))
    # An explicit snapshot read reconstructs once; the export only reports the stored result.
    assert (await client.get(f"/api/session/{session.id}/snapshot")).status_code == 200

    document = (await client.get(f"/api/session/{session.id}/export", params={"format": "json"})).json()
    exported = document["sessions"][0]
    assert exported["snapshot_origin"] == "reconstructed"
    assert "not a record of the exact prompt" in exported["snapshot_notice"]
    assert exported["messages"][0]["parts"][0]["text"] == "old history"
    markdown = (await client.get(f"/api/session/{session.id}/export", params={"format": "markdown"})).text
    assert "origin `reconstructed`" in markdown
    assert "not a record of the exact prompt" in markdown


async def test_api_export_does_not_reconstruct_a_missing_snapshot(env):
    _, client, _, workspace = env
    project, _ = env.store.project_for_path(str(workspace))
    session = env.store.create_session(project, title="old session", agent="build")
    env.store.append_message(session.id, _message(session.id, "user", Text("old text")))
    before = _database_rows(env.store)

    structured = await client.get(f"/api/session/{session.id}/export", params={"format": "json"})
    markdown = await client.get(f"/api/session/{session.id}/export", params={"format": "markdown"})
    assert structured.status_code == markdown.status_code == 200
    exported = structured.json()["sessions"][0]
    assert exported["snapshot"] is None
    assert "does not reconstruct" in exported["snapshot_notice"]
    assert "Snapshot: none stored" in markdown.text
    assert env.store.snapshot(session.id) is None
    assert _database_rows(env.store) == before  # no reconstructed snapshot and no session.snapshot event


# -- running sessions --------------------------------------------------------


async def test_running_session_exports_durable_partial_state(env):
    rt, client, provider, workspace = env
    provider.replies = [[Text("finished later")]]
    gate = asyncio.Event()
    provider.gates = [gate]
    session = await _create(client, workspace)
    sid = session["id"]
    assert (await client.post(f"/api/session/{sid}/prompt", json={"text": "slow turn"})).status_code == 202
    for _ in range(200):
        kinds = [event["type"] for event in env.store.events(sid, limit=100)]
        if "step_start" in kinds and sid in rt.turns:
            break
        await asyncio.sleep(0.01)
    assert rt.turns and "step_start" in [e["type"] for e in env.store.events(sid, limit=100)]
    # The turn is now parked on the provider gate, so no writer can race this comparison.

    before = _database_rows(env.store)
    calls = len(provider.requests)
    document = (await client.get(f"/api/session/{sid}/export",
                                 params={"format": "json", "scope": "tree"})).json()
    assert document["state"]["running"] is True
    assert document["state"]["durable_partial"] is True
    assert document["state"]["running_sessions"] == [sid]
    exported = document["sessions"][0]
    assert exported["state"] == {"running": True, "durable_partial": True}
    assert [m["role"] for m in exported["messages"]] == ["user"]
    assert exported["messages"][0]["parts"][0]["text"] == "slow turn"
    assert any(e["type"] == "step_start" for e in exported["events"])
    assert _database_rows(env.store) == before  # read-only, no notice or event appended
    assert len(provider.requests) == calls  # no provider call
    assert sid in rt.turns  # not interrupted

    markdown = (await client.get(f"/api/session/{sid}/export",
                                 params={"format": "markdown"})).text
    assert "durable partial state" in markdown
    assert "**running**" in markdown

    gate.set()
    await _idle(rt, sid)
    finished = (await client.get(f"/api/session/{sid}/export", params={"format": "json"})).json()
    assert finished["state"]["running"] is False
    assert [m["role"] for m in finished["sessions"][0]["messages"]] == ["user", "assistant"]
    assert finished["sessions"][0]["messages"][1]["parts"][0]["text"] == "finished later"


# -- fidelity: fences, Unicode, transport redaction --------------------------


async def test_fences_and_unusual_unicode_round_trip(env):
    _, client, _, workspace = env
    session = await _create(client, workspace)
    sid = session["id"]
    tricky = "line with ``` four ```` and five ````` ticks\n~~~\n## not a heading\n日本語 🙂 مرحبا e\u0301 \u200b"
    env.store.append_message(sid, _message(sid, "user", Text(tricky)))
    markdown = (await client.get(f"/api/session/{sid}/export", params={"format": "markdown"})).text
    assert tricky in markdown
    # The fence is longer than the longest backtick run (5), so the transcript stays valid.
    assert "``````text\n" in markdown
    assert "``````\n" in markdown
    document = (await client.get(f"/api/session/{sid}/export", params={"format": "json"})).json()
    assert document["sessions"][0]["messages"][0]["parts"][0]["text"] == tricky


async def test_transport_headers_are_redacted_but_text_is_preserved(env):
    _, client, _, workspace = env
    session = await _create(client, workspace)
    sid = session["id"]
    env.store.append_event({
        "seq": 10_000, "time": time.time(), "type": "wire_request", "session_id": sid,
        "data": {
            "type": "wire_request", "method": "POST", "url": "https://api.example/v1",
            "headers": {"Authorization": "Bearer supersecret", "x-api-key": "key-123",
                        "x-safe": "visible", "content-type": "application/json"},
            "body": {"instructions": "Authorization: Bearer supersecret is user text"},
        },
    })
    env.store.append_event({
        "seq": 10_001, "time": time.time(), "type": "wire_response", "session_id": sid,
        "data": {"type": "wire_response", "status": 200,
                 "headers": {"set-cookie": "session=secret", "x-request-id": "abc"}},
    })
    env.store.append_message(sid, _message(sid, "user", Text("my token looks like Bearer supersecret")))

    document = (await client.get(f"/api/session/{sid}/export", params={"format": "json"})).json()
    request = next(e for e in document["sessions"][0]["events"] if e["type"] == "wire_request")
    assert request["data"]["headers"]["Authorization"] == "***"
    assert request["data"]["headers"]["x-api-key"] == "***"
    assert request["data"]["headers"]["x-safe"] == "visible"
    assert request["data"]["body"]["instructions"] == "Authorization: Bearer supersecret is user text"
    response = next(e for e in document["sessions"][0]["events"] if e["type"] == "wire_response")
    assert response["data"]["headers"]["set-cookie"] == "***"
    assert response["data"]["headers"]["x-request-id"] == "abc"
    assert "Bearer supersecret" in json.dumps(document["sessions"][0]["messages"])
    markdown = (await client.get(f"/api/session/{sid}/export", params={"format": "markdown"})).text
    assert "my token looks like Bearer supersecret" in markdown


# -- large logs and the boundary ---------------------------------------------


async def test_more_events_than_one_api_page_are_exported_in_order(env):
    _, client, _, workspace = env
    project, _ = env.store.project_for_path(str(workspace))
    session = env.store.create_session(project, title="big", agent="build")
    sid = session.id
    start = 1_000
    count = 5_010  # the event endpoint caps a page at 5,000
    now = time.time()
    with env.store.db:
        env.store.db.executemany(
            "insert into event (session_id, seq, time, type, data) values (?, ?, ?, ?, ?)",
            [(sid, start + i, now, "probe", json.dumps({"n": i})) for i in range(count)],
        )
    before_max = env.store.db.execute("select max(id) from event").fetchone()[0]

    document = (await client.get(f"/api/session/{sid}/export", params={"format": "json"})).json()
    events = document["sessions"][0]["events"]
    assert len(events) == count
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    assert [e["data"]["n"] for e in events] == list(range(count))
    assert document["sessions"][0]["counts"]["events"] == count
    assert document["boundary"]["event_id"] == before_max
    markdown = await client.get(f"/api/session/{sid}/export", params={"format": "markdown"})
    assert markdown.status_code == 200 and "Messages: 0" in markdown.text


def test_export_view_is_one_coherent_boundary(tmp_path):
    store = Store(tmp_path / "nexus.db")
    project, _ = store.project_for_path(str(tmp_path))
    session = store.create_session(project, title="boundary", agent="build")
    store.append_message(session.id, Message("user", (Text("one"),)))
    for n in range(3):
        store.append_event({"seq": 100 + n, "time": time.time(), "type": "probe",
                            "session_id": session.id, "data": {"n": n}})

    view = ExportView(store.path, session.id, "session")
    try:
        first = list(view.events(session.id))
        assert [e["data"]["n"] for e in first] == [0, 1, 2]
        # A writer commits after the read transaction started.
        store.append_event({"seq": 200, "time": time.time(), "type": "probe",
                            "session_id": session.id, "data": {"n": 3}})
        assert list(view.events(session.id)) == first  # still the same boundary
        assert view.counts(session.id)["events"] == 3
    finally:
        view.close()

    fresh = ExportView(store.path, session.id, "session")
    try:
        assert [e["data"]["n"] for e in fresh.events(session.id)] == [0, 1, 2, 3]
        assert fresh.counts(session.id)["events"] == 4
    finally:
        fresh.close()

    # Repeating an export without changes gives identical ordering and watermark.
    def snapshot(value):
        return [(row["session"]["id"], [m["id"] for m in row["messages"]], [e["id"] for e in row["events"]])
                for row in value["sessions"]], value["boundary"]["event_id"], value["boundary"]["seq"]

    documents = []
    for _ in range(2):
        view = ExportView(store.path, session.id, "session")
        documents.append(json.loads("".join(stream(view, "json"))))
        assert view.closed  # the streaming wrapper releases the read transaction
    assert snapshot(documents[0]) == snapshot(documents[1])


# -- API errors --------------------------------------------------------------


async def test_api_authentication_and_invalid_options(env):
    _, client, _, workspace = env
    session = await _create(client, workspace)
    sid = session["id"]

    unauthorized = await client.get(f"/api/session/{sid}/export", params={"format": "json"},
                                    headers={"authorization": "Bearer wrong"})
    assert unauthorized.status_code == 401
    assert (await client.get("/api/session/ses_missing/export", params={"format": "json"})).status_code == 404
    assert (await client.get(f"/api/session/{sid}/export")).status_code == 400
    assert (await client.get(f"/api/session/{sid}/export", params={"format": "pdf"})).status_code == 400
    assert (await client.get(f"/api/session/{sid}/export",
                             params={"format": "json", "scope": "project"})).status_code == 400
    # The default scope is the selected session.
    default = await client.get(f"/api/session/{sid}/export", params={"format": "json"})
    assert default.json()["scope"] == "session"


# -- CLI file handling -------------------------------------------------------


class Download:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.calls = 0

    @asynccontextmanager
    async def download(self, path, **params):
        self.calls += 1
        self.path, self.params = path, params

        class Response:
            def __init__(self, chunks, error):
                self.chunks, self.error = chunks, error

            async def aiter_bytes(self):
                for chunk in self.chunks:
                    yield chunk
                if self.error:
                    raise self.error

        yield Response(self.chunks, self.error)


def _part_files(directory):
    return [item for item in directory.iterdir() if item.name.endswith(".part")]


def _args(**fields):
    values = {"id": "ses_test", "format": "markdown", "scope": "session", "output": None, "force": False}
    values.update(fields)
    return argparse.Namespace(**values)


async def test_cli_writes_a_file_atomically_and_refuses_overwrites(tmp_path):
    target = tmp_path / "transcript.md"
    client = Download([b"# hello\n", "wörld 🌍\n".encode()])
    await _export(client, _args(output=str(target)))
    assert target.read_text() == "# hello\nwörld 🌍\n"
    assert client.calls == 1
    assert client.path == "/api/session/ses_test/export"
    assert client.params == {"format": "markdown", "scope": "session"}
    assert _part_files(tmp_path) == []

    exists = Download([b"replacement"])
    with pytest.raises(ValueError, match="already exists"):
        await _export(exists, _args(output=str(target)))
    assert exists.calls == 0  # refused before any request
    assert target.read_text() == "# hello\nwörld 🌍\n"

    await _export(Download([b"replacement"]), _args(output=str(target), force=True))
    assert target.read_text() == "replacement"


async def test_cli_stdout_and_failed_downloads_leave_no_partial_file(tmp_path):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        await _export(Download(["# hé".encode()[:2], "# hé".encode()[2:]]), _args())
    assert stdout.getvalue() == "# hé"

    target = tmp_path / "export.json"
    with pytest.raises(RuntimeError, match="stream broke"):
        await _export(Download([b"partial"], RuntimeError("stream broke")),
                      _args(format="json", output=str(target)))
    assert not target.exists()
    assert _part_files(tmp_path) == []

    # A forced overwrite that fails keeps the previous complete file.
    target.write_text("old complete export")
    with pytest.raises(RuntimeError, match="stream broke"):
        await _export(Download([b"partial"], RuntimeError("stream broke")),
                      _args(format="json", output=str(target), force=True))
    assert target.read_text() == "old complete export"
    assert _part_files(tmp_path) == []


async def test_cli_requires_a_format(env):
    with pytest.raises(ValueError, match="--format"):
        await _export(Download([b"x"]), _args(format=None))


async def test_export_preserves_headers_inside_tool_data_and_request_bodies(env):
    _, client, _, workspace = env
    sid = (await _create(client, workspace))["id"]
    user_data = {"headers": {"Authorization": "intentional tool data"}}
    env.store.append_event({"seq": 1, "time": time.time(), "type": "tool_start", "session_id": sid,
                            "data": {"args": user_data}})
    env.store.append_event({"seq": 2, "time": time.time(), "type": "wire_request", "session_id": sid,
                            "data": {"headers": {"Authorization": "credential"}, "body": user_data}})
    doc = (await client.get(f"/api/session/{sid}/export", params={"format": "json"})).json()
    events = doc["sessions"][0]["events"]
    tool = next(e for e in events if e["type"] == "tool_start")
    wire = next(e for e in events if e["type"] == "wire_request")
    assert tool["data"]["args"] == user_data
    assert wire["data"]["body"] == user_data
    assert wire["data"]["headers"]["Authorization"] == "***"


async def test_markdown_includes_stored_context_without_claiming_notice_was_sent(env):
    _, client, _, workspace = env
    sid = (await _create(client, workspace))["id"]
    snapshot = env.store.snapshot(sid)
    env.store.append_message(sid, Message("user", (Text("[Notification: not sent yet]"),)))
    text = (await client.get(f"/api/session/{sid}/export", params={"format": "markdown"})).text
    assert snapshot["system_prompt"] in text
    assert "Stored snapshot tool definitions" in text
    assert "may not yet have been sent" in text
    assert "the model received it" not in text


async def test_cli_nonforced_publish_never_overwrites_a_racing_destination(tmp_path, monkeypatch):
    import os

    from nexus import cli

    target = tmp_path / "raced.md"
    link = os.link

    def racing_link(source, destination):
        target.write_text("created by another writer")
        return link(source, destination)

    monkeypatch.setattr(cli.os, "link", racing_link)
    with pytest.raises(ValueError, match="appeared while exporting"):
        await _export(Download([b"export bytes"]), _args(output=str(target)))
    assert target.read_text() == "created by another writer"
    assert not _part_files(tmp_path)


async def test_export_view_closes_even_if_response_headers_cannot_be_sent(tmp_path):
    from nexus.server import ExportResponse, _stream_export

    store = Store(tmp_path / "nexus.db")
    project, _ = store.project_for_path(str(tmp_path))
    session = store.create_session(project, title="close", agent="build")
    view = ExportView(store.path, session.id, "session")
    response = ExportResponse(_stream_export(stream(view, "json")), view=view)

    async def send(message):
        raise OSError("client disconnected before headers")

    async def receive():
        return {"type": "http.disconnect"}

    try:
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert view.closed
    finally:
        view.close()
        store.close()


async def test_export_cancellation_waits_for_worker_before_closing_iterator():
    import threading

    from nexus.server import _stream_export

    entered, release = threading.Event(), threading.Event()

    class Iterator:
        active = False
        closed = False

        def __next__(self):
            self.active = True
            entered.set()
            release.wait(2)
            self.active = False
            return "chunk"

        def close(self):
            assert not self.active
            self.closed = True

    iterator = Iterator()
    body = _stream_export(iterator)
    task = asyncio.create_task(anext(body))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not iterator.closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert iterator.closed


async def test_export_cancellation_scope_does_not_close_active_worker():
    import threading

    import anyio

    from nexus.server import _stream_export

    entered, release = threading.Event(), threading.Event()

    class Iterator:
        active = False
        closed = False

        def __next__(self):
            self.active = True
            entered.set()
            release.wait(2)
            time.sleep(0.02)
            self.active = False
            return "chunk"

        def close(self):
            assert not self.active
            self.closed = True

    iterator = Iterator()

    async def consume():
        async for _ in _stream_export(iterator):
            pass

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(consume)
        assert await asyncio.to_thread(entered.wait, 2)
        tasks.cancel_scope.cancel()
        release.set()
    assert iterator.closed
