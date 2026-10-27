"""The `ask` tool: question contract, waiting, cancellation, API and context.

Scripted providers and temporary homes/workspaces only; no live provider run.
"""
import asyncio
import json
import time

import httpx
import pytest

from nexus import agents as agent_files
from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.questions import PendingQuestion, QuestionError, normalize_request, result_text, validate_answers
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.requests = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "instructions": req.instructions,
                "tools": [t.name for t in req.tools]}

    async def stream(self, req):
        self.requests.append(req)
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted([])
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace


async def _idle(rt, session_id=None):
    for _ in range(400):
        if session_id is None:
            if not rt.turns:
                return
        elif session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


async def _pending(rt, session_id):
    for _ in range(400):
        pending = rt.pending_questions(session_id)
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"no pending question; events={rt.store.events(session_id)}")


def _ask(call_id, questions):
    return ToolCall(call_id, "ask", json.dumps({"questions": questions}))


def _result_after_header(output):
    return json.loads(output.split("\n", 1)[1])


async def _stored_events(client, session_id):
    return (await client.get(f"/api/session/{session_id}/event")).json()["events"]


MIXED = [
    {"id": "name", "question": "What is your name?"},
    {"id": "db", "question": "Which database?",
     "options": [{"id": "pg", "label": "PostgreSQL", "preview": "Postgres 16"},
                 {"id": "lite", "label": "SQLite"}]},
    {"id": "features", "question": "Which features?",
     "options": [{"id": "auth", "label": "Auth"}, {"id": "billing", "label": "Billing"}],
     "multiple": True},
]


async def test_free_text_single_select_multi_select_and_multiple_questions(env):
    rt, client, provider, workspace = env
    provider.replies = [[_ask("a1", MIXED)], [Text("thanks")]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    sid = session["id"]
    assert "ask" in session["tools"]
    context = (await client.get(f"/api/session/{sid}/context")).json()
    assert "ask" in context["tools"] and "`ask`" in context["instructions"]

    await client.post(f"/api/session/{sid}/prompt", json={"text": "help me"})
    pending = await _pending(rt, sid)
    assert pending["call_id"] == "a1" and pending["session_id"] == sid
    assert [q["id"] for q in pending["questions"]] == ["name", "db", "features"]
    assert (await client.get(f"/api/session/{sid}/question")).json() == [pending]
    assert (await client.get("/api/question")).json() == [pending]
    # Asking never creates an approval request.
    assert not rt.pending_approvals(sid)

    reply = (await client.post(f"/api/session/{sid}/question/{pending['id']}", json={"answers": [
        {"question_id": "name", "text": "Ada"},
        {"question_id": "db", "selected": ["pg"]},
        {"question_id": "features", "selected": ["auth", "billing"]},
    ]})).status_code
    assert reply == 204
    await _idle(rt, sid)

    messages = (await client.get(f"/api/session/{sid}/message")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    results = [p for m in messages for p in m["parts"] if p["type"] == "tool_result"]
    assert len(results) == 1 and results[0]["call_id"] == "a1" and not results[0]["is_error"]
    assert _result_after_header(results[0]["output"]) == {
        "name": {"selected": [], "labels": [], "text": "Ada"},
        "db": {"selected": ["pg"], "labels": ["PostgreSQL"], "text": None},
        "features": {"selected": ["auth", "billing"], "labels": ["Auth", "Billing"], "text": None},
    }
    # The real result is in the next provider request exactly once.
    second = provider.requests[1]
    tool_results = [p for m in second.messages for p in m.parts if hasattr(p, "call_id")]
    assert len(tool_results) == 1 and tool_results[0].call_id == "a1"

    events = await _stored_events(client, sid)
    asked = [e for e in events if e["type"] == "question.asked"]
    replied = [e for e in events if e["type"] == "question.replied"]
    assert len(asked) == 1 and len(replied) == 1
    assert "permission.asked" not in [e["type"] for e in events]
    assert asked[0]["data"]["id"] == pending["id"] and asked[0]["data"]["call_id"] == "a1"
    assert replied[0]["data"]["outcome"] == "answered"
    assert replied[0]["data"]["answers"][1] == {"question_id": "db", "selected": ["pg"], "text": None}
    assert [e["type"] for e in events].index("question.asked") < [e["type"] for e in events].index("question.replied")
    await rt.shutdown()


async def test_question_result_can_mix_selections_and_free_text(env):
    rt, client, provider, workspace = env
    questions = [{"id": "db", "question": "Which database?", "allow_free_text": True,
                  "options": [{"id": "pg", "label": "PostgreSQL"}, {"id": "lite", "label": "SQLite"}]}]
    provider.replies = [[_ask("a1", questions)], [Text("ok")]]
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    response = await client.post(f"/api/session/{sid}/question/{pending['id']}", json={"answers": [
        {"question_id": "db", "selected": ["lite"], "text": "Use a temporary file first."}]})
    assert response.status_code == 204
    await _idle(rt, sid)
    output = next(p["output"] for m in (await client.get(f"/api/session/{sid}/message")).json()
                  for p in m["parts"] if p["type"] == "tool_result")
    assert _result_after_header(output)["db"] == {
        "selected": ["lite"], "labels": ["SQLite"], "text": "Use a temporary file first."}
    await rt.shutdown()


async def test_malformed_request_is_a_tool_error_and_creates_nothing(env):
    rt, client, provider, workspace = env
    bad = [{"id": "a", "question": "One?"}, {"id": "a", "question": "Duplicate?"}]
    provider.replies = [[_ask("a1", bad)], [Text("fixed")]]
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    await _idle(rt, sid)
    result = next(p["output"] for m in (await client.get(f"/api/session/{sid}/message")).json()
                  for p in m["parts"] if p["type"] == "tool_result")
    assert "duplicate question id" in result
    assert not rt.pending_questions(sid)
    assert [e for e in await _stored_events(client, sid) if e["type"] == "question.asked"] == []
    await rt.shutdown()


@pytest.mark.parametrize("questions, error", [
    ([], "1-5"),
    ([{"id": "q", "question": "x", "extra": 1}], "unknown keys"),
    ([{"id": "q", "question": "  "}], "non-empty"),
    ([{"id": "q", "question": "x", "options": [], "allow_free_text": False}], "must allow_free_text"),
    ([{"id": "q", "question": "x", "multiple": True}], "multiple requires options"),
    ([{"id": "q", "question": "x", "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]}],
     "duplicate option ids"),
    ([{"id": "q", "question": "x", "options": [{"id": "a", "label": "A", "preview": 5}]}],
     "preview must be a string"),
    ([{"id": "q", "question": "x" * 2001}], "limited to 2000"),
    ([{"id": "q", "question": "x", "allow_free_text": "yes"}], "must be a boolean"),
])
def test_question_request_validation(questions, error):
    with pytest.raises(QuestionError, match=error):
        normalize_request({"questions": questions})


def test_cli_option_tokens_accept_numbers_and_ids():
    from nexus.cli import _option_tokens

    question = {"options": [{"id": "auth"}, {"id": "billing"}, {"id": "teams"}]}
    assert _option_tokens("1, billing", question) == ["auth", "billing"]
    assert _option_tokens("auth", question) == ["auth"]
    assert _option_tokens("1,1,teams", question) == ["auth", "teams"]
    assert _option_tokens("free text", question) is None


def test_question_reply_validation():
    questions = normalize_request({"questions": MIXED})
    with pytest.raises(QuestionError, match="missing answers"):
        validate_answers(questions, [])
    with pytest.raises(QuestionError, match="unknown question id"):
        validate_answers(questions, [{"question_id": "nope", "text": "x"}])
    with pytest.raises(QuestionError, match="unknown option ids"):
        validate_answers(questions, [{"question_id": "name", "text": "Ada"},
                                     {"question_id": "db", "selected": ["nope"]},
                                     {"question_id": "features", "selected": ["auth"]}])
    with pytest.raises(QuestionError, match="single-select"):
        validate_answers(questions, [{"question_id": "name", "text": "Ada"},
                                     {"question_id": "db", "selected": ["pg", "lite"]},
                                     {"question_id": "features", "selected": ["auth"]}])
    with pytest.raises(QuestionError, match="duplicate option ids"):
        validate_answers(questions, [{"question_id": "name", "text": "Ada"},
                                     {"question_id": "db", "selected": ["pg", "pg"]},
                                     {"question_id": "features", "selected": ["auth"]}])
    with pytest.raises(QuestionError, match="does not allow a free-text"):
        validate_answers(questions, [{"question_id": "name", "text": "Ada"},
                                     {"question_id": "db", "selected": ["pg"], "text": "x"},
                                     {"question_id": "features", "selected": ["auth"]}])
    with pytest.raises(QuestionError, match="duplicate answer"):
        validate_answers(questions, [{"question_id": "name", "text": "Ada"},
                                     {"question_id": "name", "text": "Grace"},
                                     {"question_id": "db", "selected": ["pg"]},
                                     {"question_id": "features", "selected": ["auth"]}])
    with pytest.raises(QuestionError, match="unknown keys"):
        validate_answers(questions, [{"question_id": "name", "text": "Ada", "other": 1},
                                     {"question_id": "db", "selected": ["pg"]},
                                     {"question_id": "features", "selected": ["auth"]}])
    answers = validate_answers(questions, [
        {"question_id": "features", "selected": ["auth"]},
        {"question_id": "db", "selected": ["pg"]},
        {"question_id": "name", "text": "Ada"},
    ])
    assert [a["question_id"] for a in answers] == ["name", "db", "features"]
    assert json.loads(result_text(questions, answers).split("\n", 1)[1])["db"]["labels"] == ["PostgreSQL"]


async def test_invalid_answers_are_rejected_and_the_question_stays_pending(env):
    rt, client, provider, workspace = env
    provider.replies = [[_ask("a1", MIXED)], [Text("ok")]]
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    url = f"/api/session/{sid}/question/{pending['id']}"
    invalid = [
        {},
        {"answers": "nope"},
        {"answers": []},
        {"answers": [{"question_id": "name", "text": "Ada"}]},
        {"answers": [{"question_id": "nope", "text": "x"}]},
        {"answers": [{"question_id": "name", "text": "Ada"},
                     {"question_id": "db", "selected": ["nope"]},
                     {"question_id": "features", "selected": ["auth"]}]},
        {"answers": [{"question_id": "name", "text": "Ada"},
                     {"question_id": "db", "selected": ["pg", "lite"]},
                     {"question_id": "features", "selected": ["auth"]}]},
        {"answers": [{"question_id": "name", "text": "Ada"},
                     {"question_id": "db", "selected": ["pg"]},
                     {"question_id": "features", "selected": []}]},
    ]
    for body in invalid:
        response = await client.post(url, json=body)
        assert response.status_code == 400, (body, response.text)
        assert rt.pending_questions(sid), body
    assert (await client.post(url, json={"answers": [
        {"question_id": "name", "text": "Ada"},
        {"question_id": "db", "selected": ["pg"]},
        {"question_id": "features", "selected": ["billing"]},
    ]})).status_code == 204
    await _idle(rt, sid)
    assert not rt.pending_questions(sid)
    replies = [e for e in await _stored_events(client, sid) if e["type"] == "question.replied"]
    assert len(replies) == 1
    await rt.shutdown()


async def test_question_endpoints_require_authentication(env):
    rt, client, _, workspace = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    for url in ("/api/question", f"/api/session/{sid}/question"):
        response = await client.get(url, headers={"authorization": "Bearer wrong"})
        assert response.status_code == 401
    response = await client.post(f"/api/session/{sid}/question/qst_missing",
                                 headers={"authorization": "Bearer wrong"}, json={"answers": []})
    assert response.status_code == 401
    assert (await client.get(f"/api/session/{sid}/question")).json() == []
    assert (await client.get("/api/question")).json() == []
    assert (await client.post(f"/api/session/{sid}/question/qst_missing", json={"answers": []})).status_code == 404
    await rt.shutdown()


async def test_duplicate_and_conflicting_replies_resolve_once(env):
    rt, client, provider, workspace = env
    provider.replies = [[_ask("a1", MIXED)], [Text("ok")]]
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    url = f"/api/session/{sid}/question/{pending['id']}"
    answers = [{"question_id": "name", "text": "Ada"},
               {"question_id": "db", "selected": ["pg"]},
               {"question_id": "features", "selected": ["auth"]}]
    assert (await client.post(url, json={"answers": answers})).status_code == 204
    assert (await client.post(url, json={"answers": answers})).status_code == 409
    assert (await client.post(url, json={"answers": answers[:1]})).status_code == 409
    await _idle(rt, sid)
    events = await _stored_events(client, sid)
    assert sum(1 for e in events if e["type"] == "question.replied") == 1
    results = [p for m in (await client.get(f"/api/session/{sid}/message")).json()
               for p in m["parts"] if p["type"] == "tool_result"]
    assert len(results) == 1
    await rt.shutdown()


async def test_interrupt_cancels_the_question_and_reports_an_accurate_result(env):
    rt, client, provider, workspace = env
    provider.replies = [[_ask("a1", MIXED)]]
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    assert (await client.post(f"/api/session/{sid}/interrupt")).status_code == 204
    await _idle(rt, sid)

    assert not rt.pending_questions(sid)
    events = await _stored_events(client, sid)
    replied = [e for e in events if e["type"] == "question.replied"]
    assert len(replied) == 1 and replied[0]["data"]["outcome"] == "cancelled"
    assert "interrupted" in replied[0]["data"]["reason"]
    messages = (await client.get(f"/api/session/{sid}/message")).json()
    results = [p for m in messages for p in m["parts"] if p["type"] == "tool_result"]
    assert len(results) == 1
    result = results[0]
    assert result["is_error"] and "while this tool was running" in result["output"]
    assert "before this tool ran" not in result["output"]
    assert messages[-1]["role"] == "tool"  # provider-valid history: every call has one result
    late = await client.post(f"/api/session/{sid}/question/{pending['id']}", json={"answers": [
        {"question_id": "name", "text": "Ada"},
        {"question_id": "db", "selected": ["pg"]},
        {"question_id": "features", "selected": ["auth"]},
    ]})
    assert late.status_code == 409
    assert sum(1 for e in await _stored_events(client, sid) if e["type"] == "question.replied") == 1
    await rt.shutdown()


async def test_deleting_the_session_or_project_closes_pending_questions(env):
    rt, client, provider, workspace = env
    seen: list[dict] = []

    async def watch() -> None:
        async for event in rt.bus.subscribe():
            seen.append(event)

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    provider.replies = [[_ask("a1", MIXED)]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    assert (await client.delete(f"/api/session/{sid}")).status_code == 204
    await _idle(rt, sid)
    assert not rt.pending_questions(sid)
    assert any(e["type"] == "question.replied" and e["data"]["id"] == pending["id"]
               and e["data"]["outcome"] == "cancelled" for e in seen)
    late = await client.post(f"/api/session/{sid}/question/{pending['id']}", json={"answers": []})
    assert late.status_code == 404

    other = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    provider.replies = [[_ask("a2", MIXED)]]
    await client.post(f"/api/session/{other['id']}/prompt", json={"text": "go"})
    pending = await _pending(rt, other["id"])
    assert (await client.delete(f"/api/project/{other['project_id']}")).status_code == 204
    await _idle(rt, other["id"])
    assert not rt.pending_questions(other["id"])
    assert any(e["type"] == "question.replied" and e["data"]["id"] == pending["id"]
               and e["data"]["outcome"] == "cancelled" for e in seen)
    assert (await client.post(f"/api/session/{other['id']}/question/{pending['id']}", json={"answers": []})).status_code == 404
    watcher.cancel()
    await rt.shutdown()


async def test_shutdown_closes_pending_questions(env):
    rt, client, provider, workspace = env
    provider.replies = [[_ask("a1", MIXED)]]
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    await rt.shutdown()
    assert not rt.pending_questions(sid)
    assert pending["id"] in rt.resolved_questions
    replies = [e for e in rt.store.events(sid) if e["type"] == "question.replied"]
    assert replies and replies[-1]["data"]["outcome"] == "cancelled"


async def test_close_questions_without_a_turn_is_explicit(env):
    rt, client, _, workspace = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    request = PendingQuestion(
        id="qst_orphan", session_id=sid, call_id="call_1",
        questions=normalize_request({"questions": [{"id": "q", "question": "Orphan?"}]}),
        created=time.time(), future=asyncio.get_running_loop().create_future(),
    )
    rt.questions[request.id] = request
    rt.close_questions(None, "the daemon is shutting down")
    assert request.future.cancelled() and not rt.pending_questions(sid)
    events = await _stored_events(client, sid)
    reply = next(e["data"] for e in events if e["type"] == "question.replied")
    assert reply["outcome"] == "cancelled" and reply["reason"] == "the daemon is shutting down"
    await rt.shutdown()


async def test_child_question_belongs_to_the_child_and_its_tree(tmp_path, monkeypatch):
    # Separate providers keep the parent's and child's replies independent.
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    providers = {"parent": Scripted([]), "child": Scripted([])}
    monkeypatch.setattr(runtime_module, "provider_for",
                        lambda ref: (providers[ref.partition("/")[2]], ref.partition("/")[2]))
    rt = make_runtime("scripted/parent", Store(tmp_path / "child.db"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "childproj"
    (workspace / ".agents" / "agents").mkdir(parents=True)
    (workspace / ".agents" / "agents" / "helper.md").write_text(
        "---\ndescription: Helps\nmodel: scripted/child\nmode: subagent\n---\nYou help.\n")
    providers["parent"].replies = [
        [ToolCall("c1", "session", json.dumps({"agent": "helper", "prompt": "ask me"}))],
        [Text("parent done")],
    ]
    providers["child"].replies = [[_ask("kid-ask", MIXED)], [Text("child done")]]
    parent = (await client.post("/api/session", json={"directory": str(workspace), "agent": "orchestrate"})).json()
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "delegate"})
    for _ in range(300):
        children = (await client.get(f"/api/session/{parent['id']}/children")).json()
        if children:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the child session was not created")
    [child] = children
    pending = await _pending(rt, child["id"])
    assert pending["session_id"] == child["id"] and pending["call_id"] == "kid-ask"
    assert not rt.pending_questions(parent["id"])
    events = await _stored_events(client, child["id"])
    asked = next(e for e in events if e["type"] == "question.asked")
    assert asked["session_id"] == child["id"] and rt.in_tree(asked["session_id"], parent["id"])
    assert not rt.in_tree(parent["id"], child["id"])
    assert (await client.post(f"/api/session/{child['id']}/question/{pending['id']}", json={"answers": [
        {"question_id": "name", "text": "Kid"},
        {"question_id": "db", "selected": ["lite"]},
        {"question_id": "features", "selected": ["auth", "billing"]},
    ]})).status_code == 204
    await _idle(rt)
    child_result = next(p["output"] for m in (await client.get(f"/api/session/{child['id']}/message")).json()
                        for p in m["parts"] if p["type"] == "tool_result" and p["call_id"] == "kid-ask")
    assert _result_after_header(child_result)["name"]["text"] == "Kid"
    parent_results = [p for m in (await client.get(f"/api/session/{parent['id']}/message")).json()
                      for p in m["parts"] if p["type"] == "tool_result"]
    assert "child done" in parent_results[0]["output"]
    await rt.shutdown()
    await client.aclose()


async def test_disabled_ask_is_absent_from_prompt_schema_and_calls(env):
    rt, client, provider, workspace = env
    (workspace / ".agents" / "agents").mkdir(parents=True)
    (workspace / ".agents" / "agents" / "reader.md").write_text(
        "---\ndescription: Reads\ntools: [read]\n---\nRead only.\n")
    session = (await client.post("/api/session", json={
        "directory": str(workspace), "disabled_tools": ["ask"]})).json()
    sid = session["id"]
    assert "ask" not in session["tools"]
    context = (await client.get(f"/api/session/{sid}/context")).json()
    assert "ask" not in context["tools"] and "`ask`" not in context["instructions"]
    provider.replies = [[_ask("a1", MIXED)], [Text("no tool")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    await _idle(rt, sid)
    result = next(p["output"] for m in (await client.get(f"/api/session/{sid}/message")).json()
                  for p in m["parts"] if p["type"] == "tool_result")
    assert "unknown tool 'ask'" in result
    assert not rt.pending_questions(sid)

    # The same restriction holds inside a code cell: no binding and no pending request.
    provider.replies = [[ToolCall("c1", "code", json.dumps({
        "code": 'await ask(questions=[{"id": "q", "question": "hidden?"}])'}))], [Text("done")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "try in code"})
    await _idle(rt, sid)
    code_result = next(p["output"] for m in (await client.get(f"/api/session/{sid}/message")).json()
                      for p in m["parts"] if p["type"] == "tool_result" and "kernel" in p["output"])
    assert ("Error" in code_result or "not defined" in code_result) and "ask" in code_result
    assert not rt.pending_questions(sid)

    reader = (await client.post("/api/session", json={"directory": str(workspace), "agent": "reader"})).json()
    assert "ask" not in reader["tools"]
    assert "ask" not in (await client.get(f"/api/session/{reader['id']}/context")).json()["tools"]
    await rt.shutdown()


async def test_existing_snapshots_do_not_gain_ask(env):
    rt, client, provider, workspace = env
    sid = (await client.post("/api/session", json={"directory": str(workspace)})).json()["id"]
    snapshot = rt.store.snapshot(sid)
    assert "ask" in snapshot["tools"] and "ask" in snapshot["tool_definitions"]

    # Simulate a snapshot stored before `ask` existed: it also lacked it in granted_tools,
    # its definitions and its frozen prompt.
    for data in (snapshot["granted_tools"], snapshot["tools"]):
        data.remove("ask")
    snapshot["tool_definitions"].pop("ask")
    for key in ("agent_prompt", "system_prompt"):
        snapshot[key] = "\n".join(
            line for line in snapshot[key].splitlines() if not line.startswith("- `ask`:")
        )
    rt.store.set_snapshot(sid, snapshot)

    context = (await client.get(f"/api/session/{sid}/context")).json()
    assert "ask" not in context["tools"] and "`ask`" not in context["instructions"]
    assert "ask" not in (await client.get(f"/api/session/{sid}/snapshot")).json()["tools"]
    patched = (await client.patch(f"/api/session/{sid}", json={"disabled_tools": []})).json()
    assert "ask" not in patched["tools"]
    assert "ask" not in rt.store.snapshot(sid)["tool_definitions"]
    # The session still runs with its stable snapshot; another new session gets ask.
    provider.replies = [[Text("ok")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    await _idle(rt, sid)
    newest = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    assert "ask" in newest["tools"]
    await rt.shutdown()


async def test_ask_inside_code_uses_the_nested_call_and_answers(env):
    rt, client, provider, workspace = env
    code = ('answer = await ask(questions=[{"id": "q", "question": "Pick one", '
            '"options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}])\n'
            'print(answer)\n')
    provider.replies = [[ToolCall("c1", "code", json.dumps({"code": code}))], [Text("done")]]
    sid = (await client.post("/api/session", json={
        "directory": str(workspace), "permission": "allow"})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    assert pending["session_id"] == sid and pending["call_id"] == "c1:1:1"
    assert (await client.post(f"/api/session/{sid}/question/{pending['id']}",
                              json={"answers": [{"question_id": "q", "selected": ["b"]}]})).status_code == 204
    await _idle(rt, sid)
    events = await _stored_events(client, sid)
    call = next(e["data"] for e in events if e["type"] == "code.call")
    assert call["tool"] == "ask" and call["error"] is None and "The user answered" in call["output"]
    result = next(p["output"] for m in (await client.get(f"/api/session/{sid}/message")).json()
                  for p in m["parts"] if p["type"] == "tool_result" and "kernel" in p["output"])
    assert "The user answered" in result and '"b"' in result
    await rt.shutdown()


async def test_code_cell_timeout_cancels_its_question(env):
    rt, client, provider, workspace = env
    code = 'await ask(questions=[{"id": "q", "question": "Never answered?"}])\n'
    provider.replies = [[ToolCall("c1", "code", json.dumps({"code": code, "timeout_s": 0.3}))],
                        [Text("done")]]
    sid = (await client.post("/api/session", json={
        "directory": str(workspace), "permission": "allow"})).json()["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    pending = await _pending(rt, sid)
    assert pending["call_id"] == "c1:1:1"
    await _idle(rt, sid)
    assert not rt.pending_questions(sid)
    events = await _stored_events(client, sid)
    replies = [e for e in events if e["type"] == "question.replied"]
    assert len(replies) == 1 and replies[0]["data"]["outcome"] == "cancelled"
    assert "cancelled" in replies[0]["data"]["reason"] or "interrupted" in replies[0]["data"]["reason"]
    late = await client.post(f"/api/session/{sid}/question/{pending['id']}", json={"answers": [
        {"question_id": "q", "text": "too late"}]})
    assert late.status_code == 409
    assert any(e["type"] == "kernel.deleted" for e in events)
    await rt.shutdown()


async def test_ask_is_auto_approved_and_not_a_control_tool(env):
    rt, _, _, workspace = env
    assert await rt._approve("ses_missing", ToolCall("c1", "ask", "{}"), {}) is True
    project = rt.open_project(str(workspace))
    session = rt.create_session(project, title=None, model=None, permission="allow")
    snapshot = rt.snapshot_for(session)
    enabled = rt.enabled_tool_names(snapshot["granted_tools"], snapshot["control"], set(), True)
    assert "ask" in enabled and "session" not in enabled
    # An explicit agent tool list still decides; `ask` is not appended to it.
    assert "ask" in rt.granted_tool_names(agent_files.load(str(workspace)).get("build"))
    await rt.shutdown()


async def test_large_valid_answers_preserve_complete_structured_result(env):
    rt, _, _, _ = env
    questions = normalize_request({"questions": [
        {"id": f"q{i}", "question": "Details?"} for i in range(5)
    ]})
    task = asyncio.create_task(rt.ask_user("session", "call", questions))
    await asyncio.sleep(0)
    pending = rt.pending_questions()[0]
    text = "\u0001" * 2000  # escaped JSON exceeds the generic 30k text clip
    rt.reply_question("session", pending["id"], [
        {"question_id": q["id"], "text": text} for q in questions
    ])
    result = _result_after_header(await task)
    assert len(result) == 5
    assert all(answer["text"] == text for answer in result.values())
    await rt.shutdown()


async def test_cancelled_future_cannot_be_answered_before_waiter_cleanup(env):
    from nexus.runtime import Conflict

    rt, _, _, _ = env
    task = asyncio.create_task(rt.ask_user("session", "call", normalize_request({
        "questions": [{"id": "q", "question": "Continue?"}]})))
    await asyncio.sleep(0)
    request = next(iter(rt.questions.values()))
    request.future.cancel()
    # A reply can arrive before ask_user gets CPU to clean up its cancelled wait.
    with pytest.raises(Conflict, match="cancelled"):
        rt.reply_question("session", request.id, [{"question_id": "q", "text": "yes"}])
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rt.questions and rt.resolved_questions[request.id] == "cancelled"
    await rt.shutdown()


async def test_cli_consumes_resolution_while_waiting_for_input(monkeypatch):
    import threading

    from nexus import cli

    entered = asyncio.Event()
    cancelled_read = threading.Event()

    async def read_answer(prompt, cancelled):
        entered.set()
        while not cancelled.is_set():
            await asyncio.sleep(0)
        cancelled_read.set()

    monkeypatch.setattr(cli, "_read_answer", read_answer)
    request = {"id": "qst_test", "call_id": "call", "questions": normalize_request({
        "questions": [{"id": "q", "question": "Continue?"}]})}

    class Client:
        def __init__(self):
            self.calls = []

        async def call(self, method, path, body=None):
            self.calls.append(path)

        async def events(self, session_id):
            yield {"type": "server.connected"}
            yield {"type": "session.status", "session_id": "ses", "data": {"status": "busy"}}
            yield {"type": "question.asked", "session_id": "ses", "data": request}
            await asyncio.wait_for(entered.wait(), 1)
            yield {"type": "question.replied", "session_id": "ses", "data": {
                "id": "qst_test", "outcome": "answered", "answers": []}}
            for _ in range(100):
                if cancelled_read.is_set():
                    break
                await asyncio.sleep(0)
            assert cancelled_read.is_set()
            yield {"type": "session.status", "session_id": "ses", "data": {"status": "idle"}}

    class Renderer:
        def _break(self):
            pass

        def _print(self, text):
            pass

        def event(self, event):
            pass

    client = Client()
    await asyncio.wait_for(cli._send(client, "ses", "go", Renderer()), 2)
    assert client.calls == ["/api/session/ses/prompt"]


async def test_cli_pipe_reader_cancels_without_stealing_later_lines(monkeypatch):
    import io
    import os
    import threading

    from nexus import cli

    read_fd, write_fd = os.pipe()
    source = io.TextIOWrapper(os.fdopen(read_fd, "rb"), encoding="utf-8")
    monkeypatch.setattr(cli.sys, "stdin", source)
    try:
        cancelled = threading.Event()
        task = asyncio.create_task(cli._read_answer("", cancelled))
        await asyncio.sleep(0)
        cancelled.set()
        assert await asyncio.wait_for(task, 1) is None
        os.write(write_fd, "€ first\nsecond\n".encode())
        assert await cli._read_answer("", threading.Event()) == "€ first"
        assert await cli._read_answer("", threading.Event()) == "second"
    finally:
        os.close(write_fd)
        source.close()
