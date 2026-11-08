"""Cards (docs/manager-design.md 10.4 and the frozen 10.7 shapes), the stage, navigation and the holder's view.

Part one checks `stage.py` alone. Part two drives the scripted Manager for docs/manager-design.md section 11's
"stage and view" evals: a row number means the row the user saw, a move is announced only after the client
confirms it, a proactive wake never moves the screen, "go back" returns to where the user was, and every
card stays inside its bounds with the clipping stated.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from manager_seed import seed
from manager_support import Harness, until

from nexus.manager import stage
from nexus.manager.view import HolderView, NavigationError, View

ROW = {"session_id": "ses_1", "handle": "auth-refactor", "title": "Auth refactor", "project": "nexus", "state": "working"}


def test_a_board_has_exactly_the_contract_shape_and_numbers_rows_across_columns():
    body = stage.board({"needs_you": [ROW], "running": [ROW, ROW], "finished": [ROW]})
    assert set(body) == {"type", "columns", "more"} and body["type"] == "board"
    assert set(body["columns"]) == {"needs_you", "running", "finished"}
    numbers = [r["n"] for column in body["columns"].values() for r in column]
    assert numbers == [1, 2, 3, 4]
    assert set(body["columns"]["needs_you"][0]) == {"n", "session_id", "handle", "title", "project", "state"}
    assert body["more"] == {"needs_you": 0, "running": 0, "finished": 0}
    assert stage.board({}, project="nexus")["project"] == "nexus"


def test_a_board_column_is_bounded_and_says_how_many_more():
    body = stage.board({"running": [dict(ROW, session_id=f"s{i}") for i in range(7)]})
    assert len(body["columns"]["running"]) == stage.BOARD_ROWS and body["more"]["running"] == 3


def test_a_sessions_card_is_bounded_and_carries_why():
    items = [dict(ROW, session_id=f"s{i}", why="title match") for i in range(12)]
    body = stage.sessions(items)
    assert set(body) == {"type", "rows", "more"} and len(body["rows"]) == stage.SESSION_ROWS and body["more"] == 4
    assert body["rows"][0]["why"] == "title match" and [r["n"] for r in body["rows"]] == list(range(1, 9))


def test_an_approval_card_shows_the_exact_command_from_the_pending_request_itself():
    request = {"id": "per_1", "tool": "bash", "args": {"command": "alembic upgrade head\nrm -rf /tmp/x"}}
    body = stage.approval(session_id="ses_1", handle="auth-refactor", approval_id="per_1", request=request)
    assert set(body) == {"type", "session_id", "handle", "approval_id", "summary", "detail", "clipped"}
    assert body["detail"] == "alembic upgrade head\nrm -rf /tmp/x" and body["summary"] == "run: alembic upgrade head"
    assert body["clipped"] is False
    long = stage.approval(session_id="s", handle="h", approval_id="p",
                          request={"tool": "bash", "args": {"command": "x" * 9000}})
    assert long["clipped"] is True and len(long["detail"]) <= stage.APPROVAL_DETAIL_CHARS


def test_an_approval_for_another_tool_renders_its_arguments():
    body = stage.approval(session_id="s", handle="h", approval_id="p",
                          request={"tool": "write", "args": {"path": "a.txt", "content": "hi"}})
    assert body["summary"] == "write: a.txt" and '"content": "hi"' in body["detail"]


def test_a_question_card_takes_the_first_question_and_says_how_many_follow():
    request = {"questions": [{"id": "a", "question": "Which db?", "options": [{"id": "pg", "label": "Postgres"}]},
                             {"id": "b", "question": "Which region?"}]}
    body = stage.question(session_id="s", handle="h", request=request)
    assert set(body) == {"type", "session_id", "handle", "question", "options"}
    assert body["question"].startswith("Which db?") and "and 1 more question" in body["question"]
    assert body["options"] == ["Postgres"]
    with pytest.raises(stage.CardError):
        stage.question(session_id="s", handle="h", request={"questions": []})


def test_a_diagram_is_refused_when_too_long_rather_than_cut_in_half():
    assert stage.diagram("```mermaid\nflowchart TD\n  A --> B\n```") == {
        "type": "diagram", "source": "flowchart TD\n  A --> B", "clipped": False}
    with pytest.raises(stage.CardError, match="limit"):
        stage.diagram("flowchart TD\n" + "  A --> B\n" * 1000)
    with pytest.raises(stage.CardError):
        stage.diagram("  ")


def test_an_excerpt_and_a_note_are_clipped_with_a_flag():
    body = stage.excerpt(handle="h", session_id="s", text="word " * 1000)
    assert set(body) == {"type", "handle", "session_id", "markdown", "clipped"} and body["clipped"] is True
    assert len(body["markdown"]) <= stage.EXCERPT_CHARS and body["markdown"].endswith("…")
    assert stage.note("short") == {"type": "note", "markdown": "short", "clipped": False}
    assert stage.note("x" * 5000)["clipped"] is True
    with pytest.raises(stage.CardError):
        stage.note("")


def test_one_card_at_a_time_and_a_stale_dismiss_changes_nothing():
    seen: list = []
    surface = stage.Stage(seen.append)
    first = surface.show(stage.finish(stage.note("a"), focused=True, title="First"))
    second = surface.show(stage.finish(stage.note("b"), focused=False))
    assert seen == [first.payload, second.payload] and surface.card is second
    assert first.payload["title"] == "First" and "title" not in second.payload
    assert surface.dismiss(first.id) is False and surface.card is second
    assert surface.dismiss(second.id) is True and surface.card is None and seen[-1] is None
    assert surface.take_dismissed() == "note card" and surface.take_dismissed() is None
    assert surface.clear() is False


def test_the_row_a_user_saw_is_found_by_its_number():
    surface = stage.Stage(lambda card: None)
    body = stage.sessions([dict(ROW, session_id="a", handle="one"), dict(ROW, session_id="b", handle="two")])
    surface.show(stage.finish(body, focused=True))
    assert surface.row(2)["session_id"] == "b" and surface.row(3) is None
    surface.clear()
    assert surface.row(1) is None


# -- navigation handshake ---------------------------------------------------------------------

class Views:
    def __init__(self) -> None:
        self.current: dict | None = None

    def view_for(self, stream_id):
        return self.current


async def test_navigate_waits_for_the_clients_answer_and_remembers_where_the_user_was():
    views = Views()
    holder = HolderView(views, timeout=1)
    sent: list[dict] = []
    views.current = {"project": "p1", "session": "s1", "panel": None}
    task = asyncio.ensure_future(holder.navigate("st", sent.append, target=View("p2", "s2", "files")))
    await asyncio.sleep(0.01)
    assert not task.done() and sent[0]["session"] == "s2" and sent[0]["panel"] == "files" and "back" not in sent[0]
    assert holder.result("st", sent[0]["request_id"], True, None) is True
    before = await task
    assert before == View("p1", "s1", None) and holder.back_stack == [View("p1", "s1", None)]


async def test_a_refusal_carries_the_clients_reason_and_a_timeout_is_a_failure():
    views = Views()
    holder = HolderView(views, timeout=0.05)
    sent: list[dict] = []
    task = asyncio.ensure_future(holder.navigate("st", sent.append, target=View("p", "s", "file")))
    await asyncio.sleep(0.01)
    holder.result("st", sent[0]["request_id"], False, "the file panel is not available on iOS")
    with pytest.raises(NavigationError, match="not available on iOS"):
        await task
    with pytest.raises(NavigationError, match="did not confirm"):
        await holder.navigate("st", sent.append, target=View("p", "s", None))
    assert holder.back_stack == []  # nothing moved, so there is nothing to go back to


async def test_only_the_holders_answer_to_a_known_request_counts():
    holder = HolderView(Views(), timeout=1)
    sent: list[dict] = []
    task = asyncio.ensure_future(holder.navigate("st", sent.append, target=View("p", None, None)))
    await asyncio.sleep(0.01)
    rid = sent[0]["request_id"]
    assert holder.result("someone-else", rid, True, None) is False
    assert holder.result("st", "nav_unknown", True, None) is False
    assert holder.result("st", rid, True, None) is True
    await task
    assert holder.result("st", rid, True, None) is False  # late or repeated


async def test_back_pops_the_views_the_manager_moved_the_user_through():
    views = Views()
    holder = HolderView(views, timeout=1)
    sent: list[dict] = []

    async def go(target, **kwargs):
        task = asyncio.ensure_future(holder.navigate("st", sent.append, target=target, **kwargs))
        await asyncio.sleep(0.01)
        holder.result("st", sent[-1]["request_id"], True, None)
        await task

    views.current = {"project": "A", "session": "C", "panel": None}
    await go(View("B", "D", None))
    views.current = {"project": "B", "session": "D", "panel": None}
    await go(View("B", "E", None))
    views.current = {"project": "B", "session": "E", "panel": None}
    await go(None, back=True)
    assert sent[-1]["back"] is True and sent[-1]["session"] == "D"
    await go(None, back=True)
    assert sent[-1]["session"] == "C" and sent[-1]["project"] == "A"
    with pytest.raises(NavigationError, match="no earlier view"):
        await holder.navigate("st", sent.append, target=None, back=True)


async def test_reset_cancels_a_pending_move_with_a_clear_reason():
    holder = HolderView(Views(), timeout=5)
    task = asyncio.ensure_future(holder.navigate("st", lambda event: None, target=View("p", None, None)))
    await asyncio.sleep(0.01)
    holder.reset()
    with pytest.raises(NavigationError, match="moved to another client"):
        await task


# -- stage and view evals, end to end ------------------------------------------------------------

@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.store.set_preference("model.last", "mock/hello")
    yield harness
    await harness.close()


def two_sessions(h):
    project = h.project("nexus")
    now = time.time()
    first = seed(h.store, project, title="Sidebar polish", user="x", answer="y", now=now, age_days=0.2)
    second = seed(h.store, project, title="Dictation cue", user="x", answer="y", now=now, age_days=0.4)
    return project, first, second


async def test_the_second_one_is_the_row_the_user_saw_even_after_the_order_changed(h):
    project, _first, second = two_sessions(h)
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    card = h.card()
    rows = [r for column in card["columns"].values() for r in column]
    assert [r["n"] for r in rows] == [1, 2] and rows[1]["session_id"] == second.id
    newer = seed(h.store, project, title="Brand new", user="x", answer="y", now=time.time())  # reorders the world
    assert newer.id not in {r["session_id"] for r in rows}
    task = asyncio.ensure_future(h.say("open the second one"))
    await h.answer_navigation()
    await task
    nav = h.of("manager.navigate")[-1]["data"]
    assert nav["session"] == second.id and nav["project"] == project.id and "back" not in nav


async def test_a_move_is_announced_only_after_the_client_confirms_it(h):
    _project, _first, _second = two_sessions(h)
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    posted = asyncio.ensure_future(h.say("open the first one"))
    for _ in range(200):
        if h.of("manager.navigate"):
            break
        await asyncio.sleep(0.02)
    assert h.of("manager.navigate") and h.spoken() == ["I put it on the screen."]  # nothing about opening yet
    assert h.manager.stage.card is not None  # the card closes only once the move is confirmed
    await h.answer_navigation(ok=True)
    await posted
    assert h.spoken()[-1] == "Opened it." and h.manager.stage.card is None
    await asyncio.sleep(0.05)
    assert h.card() is None  # manager.card with card: null closed the modal


async def test_a_refused_move_is_reported_as_a_failure_and_the_card_stays(h):
    two_sessions(h)
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    posted = asyncio.ensure_future(h.say("open the first one"))
    await h.answer_navigation(ok=False, reason="that session was deleted")
    await posted
    assert h.spoken()[1] == "I could not do that." and "was deleted" in " ".join(h.spoken())
    assert h.manager.stage.card is not None


async def test_a_move_the_client_never_answers_times_out_honestly(h):
    two_sessions(h)
    h.manager.holder_view.timeout = 0.1
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    await h.say("open the first one")
    assert "did not confirm" in h.spoken()[-1] and "Opened it" not in " ".join(h.spoken())


async def test_a_proactive_wake_never_moves_the_screen(h):
    two_sessions(h)
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    h.manager.wake("open the first one")
    await h.settle()
    assert h.of("manager.navigate") == []
    assert h.manager.stage.card is not None


async def test_go_back_returns_to_the_view_before_the_manager_moved_it(h):
    project, first, second = two_sessions(h)
    h.open_stream()
    await h.start()
    await h.report_view(project.id, first.id)
    await h.say("what's going on?")
    posted = asyncio.ensure_future(h.say("open the second one"))
    await h.answer_navigation()
    await posted
    await h.report_view(project.id, second.id)  # the client reports where it landed
    back = asyncio.ensure_future(h.say("go back"))
    for _ in range(200):
        if len(h.of("manager.navigate")) == 2:
            break
        await asyncio.sleep(0.02)
    nav = h.of("manager.navigate")[-1]["data"]
    assert nav["back"] is True and nav["session"] == first.id and nav["project"] == project.id
    await h.answer_navigation()
    await back
    assert h.spoken()[-1] == "Opened it."


async def test_the_users_view_reaches_the_manager_once_and_again_only_when_it_changes(h):
    project, first, second = two_sessions(h)
    h.open_stream()
    await h.start()
    await h.report_view(project.id, first.id, "files")
    await h.say("hello")
    await h.say("hello again")
    await h.report_view(project.id, second.id)
    await h.say("and again")
    texts = [m.message.parts[0].text for m in h.store.messages(h.manager.current_episode().id) if m.message.role == "user"]
    assert "looking at" in texts[0] and "files panel" in texts[0] and "sidebar-polish" in texts[0]
    assert "<user_view>" not in texts[1]
    assert "dictation-cue" in texts[2]


async def test_what_am_i_looking_at_reads_the_holders_view_by_name(h):
    project, first, _ = two_sessions(h)
    h.open_stream()
    await h.start()
    await h.report_view(project.id, first.id)
    await h.say("what am i looking at")
    assert "sidebar-polish" in h.spoken()[-1] and "nexus" in h.spoken()[-1]


async def test_a_card_stays_inside_its_bounds_and_the_clipping_is_stated_to_the_manager(h):
    project = h.project("nexus")
    now = time.time()
    for i in range(7):
        session = seed(h.store, project, title=f"Job {i}", user="x", answer="y", now=now, age_days=0.1)
        h.rt.turns[session.id] = asyncio.ensure_future(asyncio.sleep(30))  # running
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    card = h.card()
    assert len(card["columns"]["running"]) == 4 and card["more"]["running"] == 3
    show = next(p for m in h.store.messages(h.manager.current_episode().id) for p in m.message.parts
            if hasattr(p, "output") and "Showing a board card" in p.output)
    assert "3 more in running" in show.output
    for task in h.rt.turns.values():
        task.cancel()
    h.rt.turns.clear()


async def test_the_demo_manager_shows_every_card_type(h):
    _project, first, _second = two_sessions(h)
    h.open_stream()
    await h.start()
    await h.say("diagram")
    assert h.card()["type"] == "diagram" and h.card()["source"].startswith("flowchart TD")
    await h.say("note")
    assert h.card()["type"] == "note" and h.of("manager.card")[-1]["data"]["card"]["focused"] is True
    await h.say("excerpt sidebar-polish")
    assert h.card()["type"] == "excerpt" and h.card()["session_id"] == first.id and h.card()["markdown"] == "y"
    await h.say("list sidebar")
    assert h.card()["type"] == "sessions" and h.card()["rows"][0]["handle"] == "sidebar-polish"
    await h.say("clear")
    await asyncio.sleep(0.05)
    assert h.card() is None
    await until(lambda: h.manager.stage.card is None)


async def test_an_approval_card_shows_what_the_session_really_asked_for(h):
    _project, first, _ = two_sessions(h)
    import asyncio as aio

    from nexus.store import new_id

    aid = new_id("per")
    request = {"id": aid, "session_id": first.id, "tool": "bash", "call_id": "c", "args": {"command": "alembic upgrade head"},
               "grant": "bash: alembic upgrade head"}
    h.rt.approvals[aid] = (first.id, aio.get_running_loop().create_future(), request)
    h.open_stream()
    await h.start()
    await h.say("approval")
    card = h.card()
    assert card["type"] == "approval" and card["approval_id"] == aid and card["detail"] == "alembic upgrade head"
    assert card["handle"] == "sidebar-polish" and card["session_id"] == first.id
    # It was shown, so a later "yes" can bind to it; the demo Manager then decides exactly this one.
    await h.say("yes")
    assert h.rt.approvals[aid][1].result() is True
    assert "Allowed once" in h.spoken()[-1]


async def test_scripted_day_reads_a_result_and_navigates_after_confirmation(h):
    """Exercise a continuous offline Manager call using the day fixture."""
    import json
    from pathlib import Path

    day = json.loads((Path(__file__).resolve().parents[2] / "interface/scenarios/manager-day.json").read_text())
    project = h.project("nexus")
    session = seed(h.store, project, title=day["session_title"], user="Please review", answer=day["session_answer"],
                   now=time.time(), age_days=0.2)
    h.open_stream()
    await h.start()
    await h.say(day["overview"], source="speech")
    assert any(row["session_id"] == session.id for column in h.card()["columns"].values() for row in column)
    await h.say(day["read"])
    assert h.card()["markdown"] == day["excerpt_card"]["markdown"]
    await h.say(day["overview"])
    task = asyncio.ensure_future(h.say(day["navigate"]))
    await h.answer_navigation()
    await task
    assert h.of("manager.navigate")[-1]["data"]["session"] == session.id
    await h.say(day["dismiss"])
    assert h.card() is None

    from nexus.store import new_id

    aid = new_id("per")
    request = {"id": aid, "session_id": session.id, "tool": "bash", "call_id": "c",
               "args": {"command": day["command"]}, "grant": f"bash: {day['command']}"}
    future = asyncio.get_running_loop().create_future()
    h.rt.approvals[aid] = (session.id, future, request)
    await h.say(day["approval"])
    assert h.card()["detail"] == day["approval_card"]["detail"]
    await h.say(day["approve"])
    assert future.result() is True
    del h.rt.approvals[aid]
    await h.say(day["start"], source="speech")
    assert any(row["tool"] == "start_session" and row["ok"] for row in h.store.audit())
    created = next(s for s in h.store.sessions() if s.id != session.id)
    first = h.store.messages(created.id)[0]
    assert first.origin == "manager" and first.message.parts[0].text == "check the release"
    await h.say(day["steer"])
    assert any(row["tool"] == "send_to_session" and row["ok"] for row in h.store.audit())
    await until(lambda: any(m.origin == "manager" for m in h.store.messages(session.id)))
    assert any(m.message.parts[0].text == "check the tests" for m in h.store.messages(session.id) if m.origin == "manager")
