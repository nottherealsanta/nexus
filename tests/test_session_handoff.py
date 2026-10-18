"""Handoff transaction, replay, model input, and safety contracts."""
from __future__ import annotations
import msgspec
import pytest
from nexus.context.manager import ContextManager
from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection
from nexus.errors import SessionBusy
from nexus.model.message import Message, Text, ToolUse, ToolResult
from nexus.session import SessionManager
from nexus.session.records import SummaryRecord

def populate(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("demo")
    for i in range(8):
        session.append_message(Message(role="user" if i % 2 == 0 else "assistant",
            content=[Text(text=f"original-{i} " + "x" * 150)]))
    return manager, session

def env(overflow="compact", brief="deterministic"):
    return Config(v2=ConfigV2(context=ContextSection(max_tokens=500,
        safety_margin_tokens=0, compact_at_fraction=1, on_overflow=overflow,
        handoff_brief=brief, compaction="drop_oldest")))

@pytest.mark.parametrize("failure", ["record", "index", "row", "commit"])
def test_transaction_failure_is_all_or_nothing(tmp_path, failure):
    import sqlite3
    manager, session = populate(tmp_path)
    before = session.read(force=True)
    conn = manager.store.db._connection()
    def authorize(action, first, second, database, source):
        if failure == "record" and action == sqlite3.SQLITE_INSERT and first == "records":
            return sqlite3.SQLITE_DENY
        if failure == "index" and action == sqlite3.SQLITE_INSERT and first == "submissions":
            return sqlite3.SQLITE_DENY
        if failure == "row" and action == sqlite3.SQLITE_UPDATE and second == "handoff_count":
            return sqlite3.SQLITE_DENY
        if failure == "commit" and action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    try:
        with pytest.raises(Exception):
            session.handoff(summary="brief", request_id="retry", continuation="go")
    finally:
        conn.set_authorizer(None)
    assert session.read(force=True) == before
    assert manager.store.session_row("demo")["handoff_count"] == 0
    assert manager.store.submission("demo", "retry", "unused") is None
    session.handoff(summary="brief", request_id="retry", continuation="go")
    reopened = SessionManager(tmp_path).open("demo", recover=False)
    assert reopened.latest_summary().text == "brief" and len(reopened.queued_ids) == 1

@pytest.mark.parametrize("summary", ["", " \x00 ", 9])
def test_invalid_summary_does_not_write(tmp_path, summary):
    _, session = populate(tmp_path)
    before = session.read(force=True)
    with pytest.raises(ValueError):
        session.handoff(summary=summary)
    assert session.read(force=True) == before


def test_observer_failure_after_head_commit_is_recoverable(tmp_path, monkeypatch):
    manager, session = populate(tmp_path)

    def fail_publish(event):
        raise RuntimeError("injected post-commit observer failure")

    monkeypatch.setattr(session._bus, "publish", fail_publish)
    first = session.handoff(summary="pinned brief", request_id="same")
    assert first["fresh"]
    reopened = SessionManager(tmp_path).open("demo", recover=False)
    retry = reopened.handoff(summary="pinned brief", request_id="same")
    assert not retry["fresh"]
    assert manager.store.session_row("demo")["handoff_count"] == 1
    assert reopened.latest_summary().text == "pinned brief"

def test_cutover_input_transcript_retry_and_decode(tmp_path):
    manager, session = populate(tmp_path)
    before = list(session.messages)
    boundary = session.message_seqs()[6]
    first = session.handoff(summary="brief", kept_from_seq=boundary, request_id="one")
    retry = session.handoff(summary="brief", kept_from_seq=boundary, request_id="one")
    assert first["fresh"] and not retry["fresh"]
    assert list(session.messages) == before
    assert session.active_messages(at_seq=8)[0] == before
    request = ContextManager(tmp_path, config=env(), counter=len, identity="").assemble(session)
    assert "brief" in repr(request.messages) and "original-0" not in repr(request.messages)
    assert manager.store.session_row("demo")["handoff_count"] == 1
    head = manager.store.context_head("demo")
    assert msgspec.json.decode(msgspec.json.encode(head), type=SummaryRecord) == head
    assert msgspec.json.decode(b'{"type":"summary","seq":1,"text":"old"}', type=SummaryRecord).brief_version == 0

def test_pair_safe_boundary_and_redaction(tmp_path):
    _, session = populate(tmp_path)
    session.append_message(Message(role="assistant", content=[ToolUse(id="t", name="read", input={})]))
    call = session.message_seqs()[-1]
    session.append_message(Message(role="user", content=[ToolResult(tool_use_id="t", content=[Text(text="ok")])]))
    with pytest.raises(ValueError, match="tool calls"):
        session.handoff(kept_from_seq=session.message_seqs()[-1])
    session.handoff(summary="password=hunter2\x1b " + "雪" * 20000, kept_from_seq=call)
    head = session.latest_summary()
    assert "hunter2" not in head.text and "\x1b" not in head.text
    assert len(head.text.encode()) < 24576
    assert isinstance(session.active_messages()[0][1].content[0], ToolUse)



@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "empty", "error"])
async def test_summarized_handoff_fallback_and_reserve(tmp_path, outcome):
    _, session = populate(tmp_path)
    class Recall:
        calls = 0
        async def summarize(self, messages):
            self.calls += 1
            if outcome == "error":
                raise OSError("provider unavailable")
            return "remember the goal" if outcome == "ok" else " "
    recall = Recall()
    context = ContextManager(tmp_path, config=env("handoff", "summarized"),
        counter=len, summarizer=recall, identity="", keep_recent=2)
    with session.begin_turn():
        request = await context.assemble(session)
    assert request.messages and recall.calls == 1
    marker = next(e for e in session.events if e.type == "context.handoff")
    assert marker.data["summary_fallback"] == (outcome != "ok")
    assert len(marker.data["preview"].encode()) <= 2048


def test_after_commit_lost_reply_and_fork_replay(tmp_path, monkeypatch):
    manager, session = populate(tmp_path)
    original = session._publish
    def lost_reply(event):
        if event.type == "context.handoff":
            raise OSError("connection lost after commit")
        return original(event)
    monkeypatch.setattr(session, "_publish", lost_reply)
    with pytest.raises(OSError, match="after commit"):
        session.handoff(summary="brief", continuation="go", request_id="lost")
    reopened = SessionManager(tmp_path).open("demo", recover=False)
    retry = reopened.handoff(summary="brief", continuation="go", request_id="lost")
    assert not retry["fresh"] and len(reopened.queued_ids) == 1
    fork = manager.fork("demo", new_id="branch")
    assert fork.id != reopened.id
    assert fork.active_messages()[0] == reopened.active_messages()[0]
    assert manager.store.context_head("branch").text == "brief"
    assert "original-0" in manager.export("demo", format="markdown")


def test_pending_queue_permissions_and_attachments_not_moved(tmp_path):
    manager, session = populate(tmp_path)
    from nexus.model.message import Image
    image = Image(url="https://example.test/a.png", media_type="image/png")
    session.append_message(Message(role="user", content=[image, Text(text="inspect this")]))
    pending = session.enqueue("already queued")
    session._emit("permission.requested", {"id": "approval", "tool": "write"})
    before = set(session._pending_permissions)
    tail = session.message_seqs()[-1]
    session.handoff(summary="brief", kept_from_seq=tail)
    assert session.queued_ids == (pending,)
    assert session._pending_permissions == before
    assert session.active_messages()[0][-1].content[0] == image
    assert manager.store.session_row("demo")["kind"] == "root"


def test_independent_connection_cannot_handoff_live_turn(tmp_path):
    _, session = populate(tmp_path)
    other = SessionManager(tmp_path).open("demo", recover=False)
    with session.begin_turn():
        with pytest.raises(SessionBusy):
            other.handoff(summary="must not write")
    assert other.latest_summary() is None


@pytest.mark.asyncio
async def test_precompact_block_and_cancel_do_not_cutover(tmp_path):
    import asyncio
    from types import SimpleNamespace
    _, session = populate(tmp_path)
    async def block(request):
        assert request.strategy == "handoff"
        return SimpleNamespace(blocked=True, reason="not now", outcome=None)
    context = ContextManager(tmp_path, config=env("handoff"), counter=len,
        identity="", pre_compact=block)
    with session.begin_turn():
        assert (await context.assemble(session)).messages
    assert session.latest_summary() is None
    class Cancelled:
        async def summarize(self, messages):
            raise asyncio.CancelledError()
    context = ContextManager(tmp_path, config=env("handoff", "summarized"),
        counter=len, identity="", summarizer=Cancelled())
    with session.begin_turn():
        with pytest.raises(asyncio.CancelledError):
            await context.assemble(session)
    assert session.latest_summary() is None


def test_rollover_once_and_new_messages_remain_active(tmp_path):
    manager, session = populate(tmp_path)
    context = ContextManager(tmp_path, config=env("handoff"), counter=len, identity="")
    with session.begin_turn():
        request = context.assemble(session)
    assert manager.store.session_row("demo")["handoff_count"] == 1
    head = session.latest_summary()
    session.append_message(Message(role="user", content=[Text(text="steer after cutover")]))
    active = session.active_messages()[0]
    assert active[-1].content[0].text == "steer after cutover"
    assert request.messages[0].content[0].text == head.text
    session.handoff(kept_from_seq=head.kept_from_seq)
    assert manager.store.session_row("demo")["handoff_count"] == 1


@pytest.mark.asyncio
async def test_optional_recall_uses_selected_model_and_no_tools():
    from nexus.context.handoff_model import HandoffSummarizer
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    provider = ScriptedProvider(text_response("remember the goal"))
    summarizer = HandoffSummarizer(provider, "selected-model", 123)
    assert await summarizer.summarize([Message(role="user", content=[Text(text="goal")])]) == "remember the goal"
    request = provider.requests[0]
    assert request.model == "selected-model" and not request.tools
    assert request.params.max_output_tokens == 123


def test_reducer_marker_and_logs_never_expose_private_brief(tmp_path):
    from nexus.view import apply, initial_state
    from nexus.observability.session import read_session_page
    _, session = populate(tmp_path)
    session.handoff(summary="private goal")
    marker = next(e for e in session.events if e.type == "context.handoff")
    view = initial_state(session.id)
    for event in session.events:
        view = apply(view, event)
    assert any(c.kind == "handoff" for t in view.turns for c in t.context)
    page = read_session_page(session.read().records, cursor=0, limit=100)
    assert "private goal" not in repr(page)
    assert marker.data["preview"] == "private goal"


def test_small_turn_after_handoff_does_not_rehandoff(tmp_path):
    # Realistic headroom: the 500-token ``populate`` budget is smaller than one
    # kept group plus a brief, so any follow-up there legitimately re-trips the
    # threshold. Here the retained tail is a small fraction of the budget.
    manager = SessionManager(tmp_path)
    session = manager.open("demo")
    for i in range(8):
        session.append_message(Message(role="user" if i % 2 == 0 else "assistant",
            content=[Text(text=f"original-{i} " + "word " * 80)]))
    config = Config(v2=ConfigV2(context=ContextSection(max_tokens=2000,
        safety_margin_tokens=0, compact_at_fraction=1, on_overflow="handoff",
        handoff_brief="deterministic", compaction="drop_oldest")))
    context = ContextManager(tmp_path, config=config, counter=len, identity="")
    with session.begin_turn():
        context.assemble(session)
    assert manager.store.session_row("demo")["handoff_count"] == 1
    head_seq = manager.store.session_row("demo")["context_head_seq"]
    session.append_message(Message(role="user", content=[Text(text="ok")]))
    with session.begin_turn():
        context.assemble(session)
    row = manager.store.session_row("demo")
    assert row["handoff_count"] == 1 and row["context_head_seq"] == head_seq


def test_fork_before_head_sees_pre_handoff_context_and_after_head_inherits_it(tmp_path):
    manager, session = populate(tmp_path)
    seqs = session.message_seqs()
    session.handoff(summary="brief", kept_from_seq=seqs[6])
    head = session.latest_summary()
    before = manager.fork("demo", at_seq=seqs[5], new_id="before")
    assert "original-0" in repr(before.active_messages()[0])
    assert manager.store.context_head("before") is None
    after = manager.fork("demo", at_seq=head.seq, new_id="after")
    assert after.active_messages()[0][0].content[0].text == "brief"
    assert manager.store.context_head("after").text == "brief"


def test_unfit_required_context_raises_overflow_without_handoff_loop(tmp_path):
    from nexus.context.budget import ContextOverflow
    manager, session = populate(tmp_path)
    session.append_message(Message(role="user", content=[Text(text="y" * 4000)]))
    context = ContextManager(tmp_path, config=env("handoff"), counter=len, identity="")
    for _ in range(2):
        with session.begin_turn():
            with pytest.raises(ContextOverflow):
                context.assemble(session)
    assert manager.store.session_row("demo")["handoff_count"] <= 1


def test_oversized_brief_keeps_every_section_instead_of_intent_only():
    from nexus.context.handoff import build_brief, fit_brief
    # Word-separated prose: an unbroken 900-char run is redacted as an opaque
    # token (by safe_brief), which would shrink the brief below the limit.
    messages = [Message(role="user", content=[Text(text="goal " + "alpha beta " * 80)]),
                Message(role="assistant", content=[Text(text="decision " + "gamma delta " * 80),
                    ToolUse(id="t", name="Write", input={"file_path": "/a/b.py"})])]
    brief = build_brief(messages, boundary=5)
    assert len(brief) > 400 and "***" not in brief
    fitted = fit_brief(brief, 400)
    assert fitted is not None and len(fitted) <= 400 and fitted.endswith("[truncated]")
    for heading in ("Handoff brief v1", "Earlier history", "First user intent",
                    "Recent tool calls", "Files changed", "Recent decisions"):
        assert heading in fitted
    assert fit_brief(brief, 20) is None
    assert fit_brief("short", 100) == "short"


def test_handoff_marker_tokens_use_token_counts_not_characters(tmp_path):
    _, session = populate(tmp_path)
    session.handoff(summary="brief", kept_from_seq=session.message_seqs()[6])
    marker = next(e for e in session.events if e.type == "context.handoff")
    # Eight ~160-char messages vs a short brief plus two kept messages: token
    # counts, not len(str(...)) // 4 of the Message reprs.
    assert 0 < marker.data["tokens_after"] < marker.data["tokens_before"]
    assert marker.data["tokens_before"] < 8 * 160


def test_later_compaction_does_not_resurrect_retired_messages(tmp_path):
    _, session = populate(tmp_path)
    boundary = session.message_seqs()[6]
    session.handoff(summary="brief", kept_from_seq=boundary)
    session.append_summary(text="later compact summary", strategy="summarize",
        source_from_seq=boundary, source_to_seq=boundary, source_messages=1)
    active, _ = session.active_messages()
    assert active[0].content[0].text == "brief"
    assert "original-0" not in repr(active)
    from nexus.host_support.session_handoff import read_handoff
    manager = SessionManager(tmp_path)
    assert read_handoff(manager, "demo")["text"] == "brief"
