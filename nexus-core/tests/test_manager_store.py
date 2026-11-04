"""Hidden Manager sessions never leak (docs/manager-design.md section 5), and what the Manager stores survives a restart."""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from manager_seed import project as make_project
from manager_seed import seed
from manager_support import Harness

from nexus.eventlog import Stream
from nexus.store import Store


@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.store.set_preference("model.last", "mock/hello")
    yield harness
    await harness.close()


async def run_a_manager_turn(h: Harness):
    h.project("nexus")
    seed(h.store, h.rt.store.projects()[0], title="Visible work", user="please", answer="done", now=time.time(), age_days=0.1)
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    return h.manager.current_episode()


async def test_a_manager_episode_is_in_no_default_list_search_or_snapshot(h):
    episode = await run_a_manager_turn(h)
    assert episode.kind == "manager" and h.store.messages(episode.id)
    store = h.store
    assert [s.title for s in store.sessions()] == ["Visible work"]
    assert [s.title for s in store.sessions(archived="exclude")] == ["Visible work"]
    assert store.sessions(kind="manager") == [episode] or [s.id for s in store.sessions(kind="manager")] == [episode.id]
    assert {s.id for s in store.sessions(kind=None)} >= {episode.id}
    assert [p.name for p in store.projects()] == ["nexus"]
    assert "Manager" in {p.name for p in store.projects(hidden=True)}
    assert store.stale_root_sessions(before=time.time() + 10 * 86400) == [s.id for s in store.sessions()]
    snapshot = store.state_snapshot()
    assert [s["id"] for s in snapshot["sessions"]] == [s.id for s in store.sessions()]
    assert "Manager" not in {p["name"] for p in snapshot["projects"]}
    # Search: the Manager's own words are found only when asked for.
    assert store.search("going") == [] and store.search("Manager") == []
    assert store.search("going", kind="manager")


async def test_the_http_api_never_lists_a_manager_episode(h):
    episode = await run_a_manager_turn(h)
    get = lambda path: h.client.get(path)
    sessions = (await get("/api/session?archived=include")).json()
    assert [s["title"] for s in sessions] == ["Visible work"]
    assert {p["name"] for p in (await get("/api/project")).json()} == {"nexus"}
    snapshot = (await get("/api/snapshot")).json()
    assert episode.id not in str(snapshot) and "Manager" not in {p["name"] for p in snapshot["projects"]}
    assert (await get("/api/search?q=going")).json()["hits"] == []
    assert episode.id not in (await get("/api/session?project=" + h.rt.store.projects()[0].id)).text


async def test_no_stream_carries_a_manager_episodes_events_but_the_log_keeps_them(h):
    episode = await run_a_manager_turn(h)
    rail = Stream.open(h.store, h.rt.bus, after=0)  # a client rail replaying from the start
    seen = []

    async def collect(stream, sink):
        async for event in stream.events():
            sink.append(event)

    task = asyncio.ensure_future(collect(rail, seen))
    await asyncio.sleep(0.3)
    assert seen and all(e["session_id"] != episode.id for e in seen)
    assert all(episode.id not in str(e.get("data")) for e in seen if e["type"].startswith("session."))
    assert any(e["type"] == "manager.state" for e in seen)  # global manager events still flow
    durable = h.store.events(episode.id)
    assert any(e["type"] == "session.status" for e in durable) and any(e["type"] == "message.added" for e in durable)
    # The inspector can ask for the episode by id.
    inspector = Stream.open(h.store, h.rt.bus, after=0, session_id=episode.id)
    got = []
    task2 = asyncio.ensure_future(collect(inspector, got))
    await asyncio.sleep(0.3)
    assert any(e["session_id"] == episode.id for e in got)
    for stream, job in ((rail, task), (inspector, task2)):
        stream.close()
        job.cancel()
    await asyncio.gather(task, task2, return_exceptions=True)


async def test_the_idle_archive_sweep_never_touches_a_manager_episode(h):
    episode = await run_a_manager_turn(h)
    archived = h.rt.archive_stale_sessions(older_than=1, now=time.time() + 30 * 86400)
    assert episode.id not in archived
    assert not h.store.session(episode.id).archived


async def test_a_manager_session_json_is_minimal_and_never_a_user_session(h):
    episode = await run_a_manager_turn(h)
    data = h.rt.session_json(episode)
    assert data["kind"] == "manager" and data["model"] == "mock/manager_demo" and data["children"] == []
    assert "snapshot" not in data


def test_a_user_session_is_a_user_session(tmp_path):
    store = Store(tmp_path / "x.db")
    proj = make_project(store, tmp_path, "p")
    session = seed(store, proj, title="t", user="u", now=time.time())
    assert session.kind == "user" and session.topic is None and store.is_hidden(session.id) is False
    with pytest.raises(ValueError):
        store.create_session(proj, title="x", agent="build", kind="weird")
    with pytest.raises(ValueError):
        store.sessions(kind="weird")


def test_an_existing_database_gains_the_columns_in_place(tmp_path):
    path = tmp_path / "old.db"
    store = Store(path)
    proj = make_project(store, tmp_path, "p")
    seed(store, proj, title="Old session", user="hello", answer="hi", now=time.time())
    store.close()
    db = sqlite3.connect(path)
    for table, column in (("session", "kind"), ("session", "topic"), ("session", "topic_seq"),
                          ("message", "origin"), ("project", "hidden")):
        db.execute(f"alter table {table} drop column {column}")
    db.commit()
    db.close()
    reopened = Store(path)
    sessions = reopened.sessions()
    assert [s.title for s in sessions] == ["Old session"] and sessions[0].kind == "user"
    assert reopened.messages(sessions[0].id)[0].origin is None
    assert [p.name for p in reopened.projects()] == ["p"]
    assert reopened.handles() == {} and reopened.audit() == []  # the Manager's tables exist too
    reopened.close()


def test_origin_is_stored_on_messages_and_absent_otherwise(tmp_path):
    from nexus.llmhub.types import Message, Text
    from nexus.runtime import message_json

    store = Store(tmp_path / "o.db")
    session = seed(store, make_project(store, tmp_path, "p"), title="t", user="u", now=time.time())
    labelled = store.append_message(session.id, Message("user", (Text("sent by the manager"),)), origin="manager")
    plain = store.append_message(session.id, Message("user", (Text("typed by the user"),)))
    assert message_json(labelled)["origin"] == "manager" and "origin" not in message_json(plain)
    assert [m.origin for m in store.messages(session.id)][-2:] == ["manager", None]
    page = store.messages_page(session.id)["messages"]
    assert page[-2]["origin"] == "manager" and "origin" not in page[-1]
    selected = store.state_snapshot(session_id=session.id)["selected"]["messages"]
    assert selected[-2]["origin"] == "manager" and "origin" not in selected[-1]


async def test_a_queued_message_keeps_its_origin_when_it_is_delivered(h):
    project = h.project("nexus")
    session = seed(h.store, project, title="Busy", user="x", answer="y", now=time.time())
    h.rt.turns[session.id] = asyncio.ensure_future(asyncio.sleep(30))  # a running turn
    result = h.rt.prompt(session.id, "from the manager", mode="queue", via="manager")
    item = h.rt.inputs.get(session.id, result["input_id"])
    assert item["origin"] == "manager"
    plain = h.rt.prompt(session.id, "from the user", mode="queue")
    assert "origin" not in h.rt.inputs.get(session.id, plain["input_id"])
    stored = h.rt.inputs.deliver(session.id, result["input_id"], "trn_x")
    assert stored.origin == "manager" and h.store.messages(session.id)[-1].origin == "manager"
    h.rt.turns.pop(session.id).cancel()


def test_handles_persist_across_a_restart_and_never_change(tmp_path):
    from nexus.manager.retrieval import HandleRegistry

    store = Store(tmp_path / "h.db")
    proj = make_project(store, tmp_path, "p")
    first = seed(store, proj, title="Sidebar polish", user="u", now=time.time())
    second = seed(store, proj, title="Sidebar polish", user="u", now=time.time())
    registry = HandleRegistry(store.handles(), on_assign=store.save_handle)
    one, two = registry.assign(first.id, first.title), registry.assign(second.id, second.title)
    assert (one, two) == ("sidebar-polish", "sidebar-polish-2")
    store.update_session(first.id, title="Totally different")
    store.close()
    again = Store(tmp_path / "h.db")
    restored = HandleRegistry(again.handles(), on_assign=again.save_handle)
    assert restored.resolve("Sidebar Polish") == first.id and restored.resolve("sidebar-polish-2") == second.id
    assert restored.assign(first.id, "Totally different") == "sidebar-polish"  # stable once given
    again.close()


def test_the_audit_keeps_every_act_newest_first(tmp_path):
    store = Store(tmp_path / "a.db")
    store.add_audit(tool="start_session", target="docs", heard="start it", ok=True, result="done", episode_id="e", turn_id="t")
    store.add_audit(tool="send_to_session", target="docs", heard="tell it", ok=False, result="x" * 5000)
    rows = store.audit()
    assert [r["tool"] for r in rows] == ["send_to_session", "start_session"]
    assert rows[0]["ok"] is False and len(rows[0]["result"]) == 2000 and rows[1]["heard"] == "start it"
    assert [r["tool"] for r in store.audit(tool="start_session")] == ["start_session"]


def test_a_topic_is_stored_indexed_and_cleared_with_its_session(tmp_path):
    store = Store(tmp_path / "t.db")
    session = seed(store, make_project(store, tmp_path, "p"), title="Fix rail hover", user="u", now=time.time())
    assert store.search("tooltips") == []
    assert store.set_topic(session.id, "  Making the sidebar tooltips stop clipping  ", 1)
    found = store.search("tooltips")
    assert found and found[0]["session_id"] == session.id and found[0]["kind"] == "topic"
    assert store.session(session.id).topic == "Making the sidebar tooltips stop clipping"
    store.set_topic(session.id, "Something else", 1)
    assert store.search("tooltips") == [] and store.search("something")
    assert store.set_topic("ses_missing", "x", 0) is False
    store.delete_session(session.id)
    assert store.search("something") == []
