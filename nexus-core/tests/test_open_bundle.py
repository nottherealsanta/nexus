"""`GET /api/session/{id}/open`, older history with turns, the summary context and response-size budgets (plan 50 D)."""
import json

import pytest
from test_server import _wait_idle, setup  # noqa: F401 - the shared server fixture

from nexus.llmhub.types import Text

SEEDING = pytest.mark.blocks_loop("seeds 1,500-3,000 turns synchronously through the store before the request under test")
TURNS = 3000  # a long session: 6,000 messages, 3,000 turns, each with a request, an attempt and three tools
OPEN_BUDGET = 1_000_000  # bytes: /open with 200 messages (R4)
OLDER_BUDGET = 512_000  # bytes: 100 older messages with their turns
SUMMARY_CONTEXT_BUDGET = 1_000  # bytes: /context?summary=1


def _fill(store, session_id: str, turns: int = TURNS, *, running_last: bool = False) -> None:
    """Add `turns` synthetic turns after whatever the session holds, straight into the tables."""
    db = store.db
    base = db.execute("select coalesce(max(seq), 0) from message where session_id = ?", (session_id,)).fetchone()[0]
    big = "x" * 400  # a tool output the summaries must not carry
    tag = session_id[-6:]
    with db:
        for n in range(turns):
            seq = base + 2 * n
            running = running_last and n == turns - 1
            turn_id, request_id = f"trn_{tag}_{n:05d}", f"req_{tag}_{n:05d}"
            started = 1_800_000_000.0 + n
            db.execute("insert into message (id, session_id, seq, role, parts, created) values (?, ?, ?, 'user', ?, ?)",
                       (f"msg_u{tag}{n:05d}", session_id, seq + 1, json.dumps([{"type": "text", "text": f"q{n} {big}"}]), started))
            if not running:
                db.execute("insert into message (id, session_id, seq, role, parts, created) values (?, ?, ?, 'assistant', ?, ?)",
                           (f"msg_a{tag}{n:05d}", session_id, seq + 2, json.dumps([{"type": "text", "text": f"a{n} {big}"}]), started + 1))
            db.execute("insert into turn (id, session_id, status, started, finished, steps, start_seq)"
                       " values (?, ?, ?, ?, ?, 1, ?)",
                       (turn_id, session_id, "running" if running else "completed", started,
                        None if running else started + 1, seq))
            db.execute("insert into turn_request (id, turn_id, session_id, step, status, started, finished, message_id,"
                       " stop, model, input_tokens, output_tokens) values (?, ?, ?, 1, ?, ?, ?, ?, 'end_turn', 'scripted/m', 10, 5)",
                       (request_id, turn_id, session_id, "started" if running else "completed", started, started + 1,
                        None if running else f"msg_a{tag}{n:05d}"))
            db.execute("insert into turn_attempt (id, turn_id, request_id, session_id, attempt, status, started, finished)"
                       " values (?, ?, ?, ?, 1, 'completed', ?, ?)", (f"att_{tag}{n:05d}", turn_id, request_id, session_id, started, started + 1))
            for t in range(3):
                db.execute("insert into turn_tool (turn_id, session_id, call_id, name, state, started, finished, output)"
                           " values (?, ?, ?, 'read', 'inserted', ?, ?, ?)", (turn_id, session_id, f"c{n}_{t}", started, started + 1, big))


async def _session(client, workspace):
    return (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]


@SEEDING
async def test_open_is_one_bounded_bundle_whose_turns_cover_its_messages(setup):  # noqa: F811
    rt, client, _, workspace = setup
    sid = await _session(client, workspace)
    _fill(rt.store, sid)
    response = await client.get(f"/api/session/{sid}/open")
    assert response.status_code == 200
    assert len(response.content) < OPEN_BUDGET
    bundle = response.json()
    assert set(bundle) == {"session", "starting", "messages", "messages_more", "oldest_seq", "turns",
                           "turns_more_before", "floor", "usage", "approvals", "questions"}
    assert bundle["session"]["id"] == sid and "skills" in bundle["session"] and bundle["starting"]
    messages = bundle["messages"]
    assert len(messages) == 200 and bundle["messages_more"] and bundle["oldest_seq"] == messages[0]["seq"]
    assert [m["seq"] for m in messages] == sorted(m["seq"] for m in messages)
    turns = bundle["turns"]
    assert 100 <= len(turns) <= 102 and bundle["turns_more_before"]  # not all 3,000
    starts = [t["start_seq"] for t in turns]
    assert starts == sorted(starts)
    # every message belongs to a returned turn: the one with the largest start_seq below it
    for message in messages:
        assert any(s < message["seq"] for s in starts)
    assert starts[0] < messages[0]["seq"] <= starts[0] + 2
    assert bundle["floor"] is None or bundle["floor"]["seq"] <= messages[0]["seq"]
    assert all("output" not in tool for t in turns for tool in t["tools"])
    # same entries as the turn endpoint
    page = (await client.get(f"/api/session/{sid}/turn", params={"limit": 200})).json()
    by_id = {t["id"]: t for t in page["turns"]}
    assert all(by_id[t["id"]] == t for t in turns if t["id"] in by_id)
    assert bundle["usage"] == page["usage"]


async def test_older_pages_with_turns_walk_back_to_the_start_without_gaps(setup):  # noqa: F811
    rt, client, _, workspace = setup
    sid = await _session(client, workspace)
    _fill(rt.store, sid, 300)
    bundle = (await client.get(f"/api/session/{sid}/open", params={"limit": 100})).json()
    seqs = [m["seq"] for m in bundle["messages"]]
    turns = {t["id"]: t for t in bundle["turns"]}
    before, more = bundle["oldest_seq"], bundle["messages_more"]
    page = bundle
    while more:
        response = await client.get(f"/api/session/{sid}/message", params={"before": before, "limit": 100, "turns": 1})
        assert len(response.content) < OLDER_BUDGET
        page = response.json()
        assert {"messages", "more", "oldest_seq", "turns", "turns_more_before", "floor"} <= set(page)
        assert all(m["seq"] < before for m in page["messages"])
        seqs = [m["seq"] for m in page["messages"]] + seqs
        for t in page["turns"]:
            turns.setdefault(t["id"], t)
        before, more = page["oldest_seq"], page["more"]
    assert seqs == list(range(1, 601))  # contiguous
    assert len(turns) == 300  # every turn exactly once, though some came twice
    assert not page["turns_more_before"]
    assert (await client.get(f"/api/session/{sid}/message", params={"limit": 5, "turns": 1})).status_code == 400


async def test_open_matches_the_legacy_reads_and_keeps_a_running_turn(setup):  # noqa: F811
    rt, client, provider, workspace = setup
    provider.replies = [[Text("one")], [Text("two")]]
    sid = await _session(client, workspace)
    for text in ("a", "b"):
        await client.post(f"/api/session/{sid}/prompt", json={"text": text})
        await _wait_idle(rt, sid)
    _fill(rt.store, sid, 2, running_last=True)
    bundle = (await client.get(f"/api/session/{sid}/open")).json()
    legacy = (await client.get(f"/api/session/{sid}/turn")).json()
    assert bundle["turns"] == legacy["turns"] and bundle["floor"] == legacy["floor"]
    assert bundle["turns"][-1]["status"] == "running"
    page = (await client.get(f"/api/session/{sid}/message", params={"limit": 200})).json()
    assert bundle["messages"] == page["messages"]
    assert bundle["session"] == (await client.get(f"/api/session/{sid}")).json()
    assert bundle["starting"] == (await client.get(f"/api/session/{sid}/snapshot")).json()
    assert not bundle["messages_more"] and not bundle["turns_more_before"]


async def test_open_with_no_messages_and_pruned_turns_keeps_the_floor(setup):  # noqa: F811
    rt, client, _, workspace = setup
    sid = await _session(client, workspace)
    empty = (await client.get(f"/api/session/{sid}/open")).json()
    assert empty["messages"] == [] and empty["turns"] == [] and empty["oldest_seq"] is None and empty["floor"] is None
    _fill(rt.store, sid, 20)
    with rt.store.db:  # the oldest 10 turns were pruned; their messages remain
        rt.store.db.execute("delete from turn where session_id = ? and id < ?", (sid, f"trn_{sid[-6:]}_00010"))
    bundle = (await client.get(f"/api/session/{sid}/open", params={"limit": 500})).json()
    assert len(bundle["messages"]) == 40 and len(bundle["turns"]) == 10
    assert bundle["floor"] == {"seq": 21}  # messages below have no summary, as GET /turn says
    assert bundle["floor"] == (await client.get(f"/api/session/{sid}/turn")).json()["floor"]


async def test_open_errors(setup):  # noqa: F811
    _, client, _, workspace = setup
    assert (await client.get("/api/session/ses_nope/open")).status_code == 404
    sid = await _session(client, workspace)
    assert (await client.get(f"/api/session/{sid}/open", params={"limit": "x"})).status_code == 400


@SEEDING
async def test_open_reads_a_fixed_number_of_statements_however_long_the_session(setup):  # noqa: F811
    rt, client, _, workspace = setup
    counts = []
    for turns in (400, 1500):
        sid = await _session(client, workspace)
        _fill(rt.store, sid, turns)
        seen: list[str] = []
        rt.store.db.set_trace_callback(lambda sql, seen=seen: seen.append(sql.strip().split()[0].lower()))
        try:
            rt.store.open_bundle(sid)
        finally:
            rt.store.db.set_trace_callback(None)
        counts.append(len([s for s in seen if s == "select"]))
    assert counts[0] == counts[1] <= 14  # R3: the number of queries does not follow the number of turns


async def test_context_summary_is_a_few_bytes(setup):  # noqa: F811
    rt, client, _, workspace = setup
    sid = await _session(client, workspace)
    _fill(rt.store, sid, 50)
    full = await client.get(f"/api/session/{sid}/context")
    short = await client.get(f"/api/session/{sid}/context", params={"summary": "1"})
    assert len(short.content) < SUMMARY_CONTEXT_BUDGET < len(full.content)
    assert set(short.json()) == {"used", "window", "exact"}
    assert {k: full.json()[k] for k in short.json()} == short.json()
