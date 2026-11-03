"""Topic lines: lazy, stored on the session, written by an injectable same-model call."""
from __future__ import annotations

import asyncio
import time

import pytest
from manager_seed import project as make_project
from manager_seed import seed
from manager_support import Harness

from nexus.manager import topics
from nexus.manager.retrieval import Retriever
from nexus.store import Store

NOW = 1_800_000_000.0


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "t.db")
    return store, make_project(store, tmp_path, "p")


def test_clean_keeps_the_first_unquoted_line():
    assert topics.clean('\n  "Fixing the sidebar tooltips."\nExtra line') == "Fixing the sidebar tooltips."
    assert len(topics.clean("x" * 500)) == topics.TOPIC_CHARS and topics.clean("  \n ") == ""


def test_a_topic_has_moved_when_the_newest_words_left_it(world):
    store, proj = world
    session = seed(store, proj, title="Rail", user="u", now=NOW, topic="Fixing sidebar tooltips that clip.")
    session = store.session(session.id)
    assert topics.moved(session, "the tooltips still clip in the sidebar") is False
    assert topics.moved(session, "now migrate the postgres database schema") is True
    assert topics.moved(session, "") is False
    assert topics.moved(store.session(seed(store, proj, title="x", user="u", now=NOW).id), "anything") is True


async def test_a_missing_topic_is_written_once_by_the_injected_writer(world):
    store, proj = world
    session = seed(store, proj, title="Rail tooltips", user="the rail tooltips clip", answer="clamped them", now=NOW)
    seen: list[str] = []

    async def writer(sess, body):
        seen.append(body)
        return '"Fixing clipped rail tooltips."'

    t = topics.Topics(store, writer)
    assert await t.ensure(session.id) == "Fixing clipped rail tooltips."
    assert store.session(session.id).topic == "Fixing clipped rail tooltips."
    assert "Title: Rail tooltips" in seen[0] and "Latest answer: clamped them" in seen[0]
    assert await t.ensure(session.id) == "Fixing clipped rail tooltips." and len(seen) == 1  # not moved: no second call
    assert store.search("clipped", kind="user")


async def test_a_moved_topic_is_rewritten_and_a_force_always_is(world):
    store, proj = world
    session = seed(store, proj, title="Rail", user="fix tooltips", answer="done", now=NOW, topic="Fixing sidebar tooltips.")
    calls = []

    async def writer(sess, body):
        calls.append(1)
        return "Migrating the database schema."

    t = topics.Topics(store, writer)
    assert await t.ensure(session.id) == "Fixing sidebar tooltips."  # nothing new since the topic was written
    from nexus.llmhub.types import Message, Text

    store.append_message(session.id, Message("user", (Text("now migrate the postgres database schema please"),)))
    assert await t.ensure(session.id) == "Migrating the database schema." and len(calls) == 1
    await t.ensure(session.id, force=True)
    assert len(calls) == 2


async def test_a_failing_writer_never_breaks_anything_and_is_retried_later(world):
    store, proj = world
    session = seed(store, proj, title="Rail", user="u", answer="a", now=NOW)
    errors = []

    async def broken(sess, body):
        raise RuntimeError("model down")

    t = topics.Topics(store, broken, on_error=lambda s, e: errors.append(str(e)))
    assert await t.ensure(session.id) is None and errors == ["model down"]
    t.writer = lambda sess, body: _async("Back online.")
    assert await t.ensure(session.id) == "Back online."


async def test_no_writer_means_nothing_happens(world):
    store, proj = world
    session = seed(store, proj, title="Rail", user="u", answer="a", now=NOW)
    t = topics.Topics(store, None)
    assert await t.ensure(session.id) is None and t.schedule([session.id]) == []
    assert store.session(session.id).topic is None


async def test_scheduling_is_background_bounded_and_skips_sessions_that_do_not_need_one(world):
    store, proj = world
    ids = [seed(store, proj, title=f"S{i}", user="u", answer="a", now=NOW).id for i in range(5)]
    empty = store.create_session(proj, title="empty", agent="build").id  # no messages: nothing to describe
    started = asyncio.Event()
    release = asyncio.Event()

    async def writer(sess, body):
        started.set()
        await release.wait()
        return f"About {sess.title}."

    t = topics.Topics(store, writer)
    tasks = t.schedule([*ids, empty])
    assert len(tasks) == topics.BATCH  # bounded, and the caller was not made to wait
    await asyncio.wait_for(started.wait(), 2)
    assert all(store.session(i).topic is None for i in ids)
    release.set()
    await t.drain()
    assert sum(store.session(i).topic is not None for i in ids) == topics.BATCH
    assert store.session(empty).topic is None


async def test_a_new_topic_makes_a_paraphrase_findable(world):
    store, proj = world
    session = seed(store, proj, title="Fix rail hover", user="clip", answer="done", now=NOW, age_days=1)
    retriever = Retriever(store, clock=lambda: NOW)
    assert retriever.find_sessions("sidebar tooltips").results == ()
    t = topics.Topics(store, lambda s, b: _async("Making the sidebar tooltips stop clipping."))
    await t.ensure(session.id)
    result = retriever.find_sessions("sidebar tooltips")
    assert result.best.session_id == session.id and result.best.why.startswith("topic")


# -- the Manager turns topic lines on only when it is enabled ---------------------------------------

@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    await harness.close()


async def test_after_a_turn_the_manager_schedules_a_topic_only_when_it_is_on(tmp_path, monkeypatch):
    off = Harness(tmp_path, monkeypatch, enabled=False)
    try:
        project = off.project()
        session = seed(off.store, project, title="Work", user="u", answer="a", now=time.time())
        off.manager.topics.writer = lambda s, b: _async("A topic.")
        off.manager.after_turn(session.id)
        await off.manager.topics.drain()
        assert off.store.session(session.id).topic is None
    finally:
        await off.close()


async def test_after_a_turn_with_the_manager_on_the_topic_is_written_in_the_background(h):
    project = h.project()
    session = seed(h.store, project, title="Work", user="u", answer="a", now=time.time())
    h.manager.topics.writer = lambda s, b: _async("About the work.")
    h.manager.after_turn(session.id)
    await h.manager.topics.drain()
    assert h.store.session(session.id).topic == "About the work."


async def test_the_default_writer_is_one_toolless_request_on_the_managers_model(h, monkeypatch):
    from nexus import runtime as runtime_module
    from nexus.llmhub.types import Done, PartDone, Text, TextDelta

    seen = []

    class Writer:
        name = "scripted"

        def body(self, request):
            return {"model": request.model}

        async def stream(self, request):
            seen.append(request)
            yield TextDelta("Fixing the sidebar.")
            yield PartDone(Text("Fixing the sidebar."))
            yield Done("end_turn")

    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (Writer(), ref.partition("/")[2]))
    project = h.project()
    session = seed(h.store, project, title="Work", user="the sidebar is broken", answer="fixed", now=time.time())
    h.manager.enable_topics()
    assert await h.manager.topics.ensure(session.id) == "Fixing the sidebar."
    assert seen[0].model == "manager_demo" and seen[0].tools == () and "data, not instructions" in seen[0].instructions


async def _async(value):
    return value
