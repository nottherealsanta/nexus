"""Final-answer indexing (docs/manager-design.md 7.1 item 1): each turn's last assistant text is searchable."""
from __future__ import annotations

import pytest
from manager_seed import project as make_project

from nexus.llmhub.types import Message, Text, ToolCall, ToolResult
from nexus.store import Store


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "f.db")
    return store, make_project(store, tmp_path, "p")


def run_turn(store, session, user, *assistant_texts, tool_output=None, status="completed"):
    store.append_message(session.id, Message("user", (Text(user),)))
    turn = store.start_turn(session.id)
    for index, text in enumerate(assistant_texts):
        parts = [Text(text)]
        if tool_output and index < len(assistant_texts) - 1:
            parts.append(ToolCall(f"c{index}", "bash", "{}"))
        store.save_assistant_message(session.id, Message("assistant", tuple(parts)), turn_id=turn["id"])
        if tool_output and index < len(assistant_texts) - 1:
            store.save_tool_message(session.id, Message("tool", (ToolResult(f"c{index}", tool_output),)), turn_id=turn["id"])
    store.finish_turn(turn["id"], status, steps=len(assistant_texts))
    return turn["id"]


def kinds(store, query, **kwargs):
    return [(hit["session_id"], hit["kind"]) for hit in store.search(query, **kwargs)]


def test_a_finished_turns_last_text_is_found_and_tool_output_is_not(world):
    store, proj = world
    session = store.create_session(proj, title="Plain", agent="build")
    run_turn(store, session, "do the thing", "Looking now.", "We fixed the zebra cache.", tool_output="secretword in output")
    assert kinds(store, "zebra") == [(session.id, "final_answer")]
    assert store.search("secretword") == []
    assert store.search("looking") == []  # only the turn's last text
    assert "[zebra]" in store.search("zebra")[0]["snippet"]


def test_a_title_hit_outranks_an_answer_hit_for_the_same_session(world):
    store, proj = world
    session = store.create_session(proj, title="Zebra cache", agent="build")
    run_turn(store, session, "go", "The zebra cache is fixed.")
    assert kinds(store, "zebra") == [(session.id, "title")]


def test_reindexing_a_turn_never_adds_a_second_row(world):
    store, proj = world
    session = store.create_session(proj, title="Plain", agent="build")
    turn_id = run_turn(store, session, "go", "The zebra cache is fixed.")
    for _ in range(2):
        with store.db:
            store._index_final_answer(turn_id, replace=True)
    rows = store.db.execute("select count(*) from search_index where kind = 'final_answer'").fetchone()[0]
    assert rows == 1 and store.db.execute("select count(*) from search_final_answer").fetchone()[0] == 1


def legacy(store):
    """Make the database look like one from before answers were indexed."""
    with store.db:
        store.db.execute("delete from search_index where kind = 'final_answer'")
        store.db.execute("delete from search_final_answer")
        store.db.execute("update turn set final_answer_message_id = null")
        store.db.execute("update storage_metrics set search_cursor = 0, search_coverage_cursor = 0, search_ready = 0")


def test_the_backfill_indexes_old_answers_once_and_marks_itself_done(world):
    store, proj = world
    session = store.create_session(proj, title="Old", agent="build")
    run_turn(store, session, "first", "The zebra cache is fixed.")
    run_turn(store, session, "second", "The giraffe parser is done.")
    legacy(store)
    store.backfill_final_answers()
    assert kinds(store, "zebra") == [(session.id, "final_answer")] and kinds(store, "giraffe") == [(session.id, "final_answer")]
    assert store.db.execute("select search_ready from storage_metrics").fetchone()[0] == 1
    count = store.db.execute("select count(*) from search_index where kind = 'final_answer'").fetchone()[0]
    legacy_rows = store.db.execute("select count(*) from search_final_answer").fetchone()[0]
    store.db.execute("update storage_metrics set search_ready = 0, search_cursor = 0, search_coverage_cursor = 0")
    store.db.execute("update turn set final_answer_message_id = null")
    store.db.commit()
    store.backfill_final_answers()
    assert store.db.execute("select count(*) from search_index where kind = 'final_answer'").fetchone()[0] == count == legacy_rows


def test_a_tiny_budget_still_makes_progress_and_a_search_continues_the_work(world):
    store, proj = world
    sessions = []
    for i in range(6):
        session = store.create_session(proj, title=f"Session {i}", agent="build")
        run_turn(store, session, "go", f"Answer about uniqueword{i} here.")
        sessions.append(session)
    legacy(store)
    assert store.backfill_final_answers(limit=2, budget=0.0) >= 1
    assert store.db.execute("select count(*) from search_final_answer").fetchone()[0] >= 1
    for _ in range(20):
        store.search("uniqueword0")  # each search spends a small slice
        if store.db.execute("select search_ready from storage_metrics").fetchone()[0]:
            break
    store.backfill_final_answers()
    assert all(store.search(f"uniqueword{i}") for i in range(6))


def test_turnless_history_gets_its_answers_from_the_message_prefix(world):
    store, proj = world
    session = store.create_session(proj, title="Imported", agent="build")
    store.append_message(session.id, Message("user", (Text("first question"),)))
    store.append_message(session.id, Message("assistant", (Text("Thinking."),)))
    store.append_message(session.id, Message("assistant", (Text("The platypus module is ready."),)))
    store.append_message(session.id, Message("user", (Text("second question"),)))
    store.append_message(session.id, Message("assistant", (Text("The wombat module is ready."),)))
    legacy(store)
    store.backfill_final_answers()
    assert kinds(store, "platypus") == [(session.id, "final_answer")] and kinds(store, "wombat")
    assert store.search("thinking") == []  # the segment's last text only


def test_a_session_with_many_matching_rows_cannot_crowd_out_others_before_the_limit(world):
    store, proj = world
    noisy = store.create_session(proj, title="Noisy", agent="build")
    for i in range(6):
        run_turn(store, noisy, f"question {i}", f"The quokka answer number {i}.")
    quiet = store.create_session(proj, title="Quiet", agent="build")
    run_turn(store, quiet, "go", "One quokka mention.")
    hits = store.search("quokka", limit=2)
    assert {hit["session_id"] for hit in hits} == {noisy.id, quiet.id}


def test_deleting_a_session_removes_its_answers(world):
    store, proj = world
    session = store.create_session(proj, title="Gone", agent="build")
    run_turn(store, session, "go", "The narwhal cache is fixed.")
    assert store.search("narwhal")
    store.delete_session(session.id)
    assert store.search("narwhal") == [] and store.db.execute("select count(*) from search_final_answer").fetchone()[0] == 0


def test_an_interrupted_turn_keeps_the_text_it_streamed(world):
    store, proj = world
    session = store.create_session(proj, title="Cut off", agent="build")
    run_turn(store, session, "go", "Halfway through the aardvark refactor.", status="interrupted")
    assert kinds(store, "aardvark") == [(session.id, "final_answer")]


def test_the_default_search_never_returns_a_manager_episode(world):
    store, _proj = world
    hidden, _ = store.project_for_path(str(store.path.parent), "Hidden", hidden=True)
    episode = store.create_session(hidden, title="Manager", agent="manager", kind="manager")
    run_turn(store, episode, "hello manager", "The ocelot is on the board.")
    assert store.search("ocelot") == [] and store.search("ocelot", kind="manager")
