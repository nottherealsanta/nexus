"""Phase 3 durable summary reuse and idempotency (review fix 2/3).

Assembly materializes the **exact persisted** summary for the prefix it covers,
reuses it without re-running the summarizer, and extends it with a single new
artifact (expanded source range) when more prefix must be compacted. Full
original messages remain authoritative.
"""
from __future__ import annotations

import asyncio

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection, ModelSection
from nexus.context import ContextManager
from nexus.context.compact import SummaryArtifact
from nexus.model.message import Message, Text
from nexus.session.manager import SessionManager


class RecordingSummarizer:
    def __init__(self):
        self.calls = 0
        self.inputs = []

    def summarize(self, messages):
        self.calls += 1
        self.inputs.append(list(messages))
        parts = []
        for message in messages:
            for block in message.content:
                parts.append(getattr(block, "text", ""))
        return SummaryArtifact(
            text="SUM(" + "|".join(parts) + ")",
            source_count=len(messages),
            tokens=5,
        )


class RequestCounter:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def count_request(self, request):
        self.calls += 1
        return self.value


def _config(max_tokens=400, strategy="summarize"):
    return Config(
        model="scripted/x",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/x"),
            context=ContextSection(
                max_tokens=max_tokens,
                safety_margin_tokens=0,
                compaction=strategy,
            ),
        ),
    )


def _seed(session, count, size=40):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        session.append_message(
            Message(role=role, content=[Text(text=f"m{index} " * size)])
        )


def _assemble(context, session):
    return context.for_turn().assemble(session)


def test_reuse_does_not_duplicate_artifact_or_call_summarizer(tmp_path):
    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = SessionManager(tmp_path, assemble=context).open("s")
    _seed(session, 8)
    session.append_message(Message(role="user", content=[Text(text="current")]))

    first = _assemble(context, session)
    second = _assemble(context, session)

    assert summarizer.calls == 1
    assert len(session.summaries) == 1
    assert first.metadata["context"]["compacted"] is not None
    # Reused: no new artifact, and no context.compacted accounting.
    assert second.metadata["context"]["compacted"] is None
    assert second.metadata["context"]["summary_reused"] is not None
    assert first.messages == second.messages
    assert "SUM(" in second.messages[0].content[0].text


def test_extended_prefix_appends_exactly_one_expanded_artifact(tmp_path):
    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = SessionManager(tmp_path, assemble=context).open("s")
    _seed(session, 8)
    session.append_message(Message(role="user", content=[Text(text="current")]))
    _assemble(context, session)
    assert len(session.summaries) == 1

    # Grow history so a larger prefix must be covered.
    _seed_after = 8
    for index in range(_seed_after, 16):
        role = "user" if index % 2 == 0 else "assistant"
        session.append_message(
            Message(role=role, content=[Text(text=f"m{index} " * 40)])
        )
    session.append_message(Message(role="user", content=[Text(text="current2")]))
    second = _assemble(context, session)

    assert summarizer.calls == 2
    assert len(session.summaries) == 2
    records = session.summaries
    # The new artifact covers strictly more messages and embeds the prior summary.
    assert records[1].source_messages > records[0].source_messages
    assert records[1].source_from_seq == records[0].source_from_seq
    assert records[0].text in records[1].text
    assert second.metadata["context"]["compacted"]["summary_id"] == records[1].summary_id


def test_reopen_reuses_existing_artifact_without_recalling(tmp_path):
    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = SessionManager(tmp_path, assemble=context).open("s")
    _seed(session, 8)
    session.append_message(Message(role="user", content=[Text(text="current")]))
    first = _assemble(context, session)
    assert len(session.summaries) == 1

    summarizer.calls = 0
    reopened_context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    reopened = SessionManager(tmp_path, assemble=reopened_context).open("s")
    second = _assemble(reopened_context, reopened)

    assert summarizer.calls == 0
    assert len(reopened.summaries) == 1
    assert second.metadata["context"]["summary_reused"] is not None
    assert first.messages == second.messages


def test_fork_reuses_copied_artifact(tmp_path):
    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    manager = SessionManager(tmp_path, assemble=context)
    session = manager.open("s")
    _seed(session, 8)
    session.append_message(Message(role="user", content=[Text(text="current")]))
    _assemble(context, session)

    child = manager.fork("s")
    summarizer.calls = 0
    request = _assemble(context, child)
    assert summarizer.calls == 0
    assert len(child.summaries) == 1
    assert request.metadata["context"]["summary_reused"] is not None
    assert "SUM(" in request.messages[0].content[0].text


def test_multi_iteration_same_boundary_reuses_not_duplicates(tmp_path):
    # Simulate two loop iterations that do not change the drop boundary.
    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = SessionManager(tmp_path, assemble=context).open("s")
    _seed(session, 8)
    session.append_message(Message(role="user", content=[Text(text="current")]))
    snapshot = context.for_turn()

    snapshot.assemble(session)
    snapshot.assemble(session)
    snapshot.assemble(session)

    assert summarizer.calls == 1
    assert len(session.summaries) == 1


def test_snapshot_reconstructs_same_summary_prompt(tmp_path):
    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    manager = SessionManager(tmp_path, assemble=context)
    session = manager.open("s")
    _seed(session, 8)
    session.append_message(Message(role="user", content=[Text(text="current")]))
    request = _assemble(context, session)
    summary_text = request.messages[0].content[0].text

    session.write_snapshot()
    loaded = manager.store.load_snapshot("s", session.read(force=True))
    assert loaded is not None and loaded.summary is not None
    assert loaded.summary.text  # the persisted artifact text is in the snapshot
    assert session.current.summary.text
    # A fresh assembly from the snapshot-backed session matches exactly.
    again = _assemble(
        ContextManager(tmp_path, config=_config(), summarizer=summarizer, counter=len),
        session,
    )
    assert again.messages[0].content[0].text == summary_text


def test_exact_refine_with_summarize_keeps_summary_and_describes_request(tmp_path):
    class ShortSummarizer:
        def summarize(self, messages):
            return SummaryArtifact(text="S", source_count=len(messages), tokens=1)

    summarizer = ShortSummarizer()
    counter = RequestCounter(100_000)
    context = ContextManager(
        tmp_path,
        config=_config(),
        summarizer=summarizer,
        counter=len,
        request_counter=counter,
    )
    session = SessionManager(tmp_path, assemble=context).open("s")
    _seed(session, 12)
    session.append_message(Message(role="user", content=[Text(text="current")]))

    request = asyncio.run(context.for_turn().assemble(session))
    context_meta = request.metadata["context"]

    assert context_meta.get("refined") is True
    assert counter.calls == 1
    # The sent request's first message is the durable summary it reports.
    reported = context_meta.get("compacted") or context_meta.get("summary_reused")
    assert reported is not None
    record = next(
        record for record in session.summaries if record.summary_id == reported["summary_id"]
    )
    assert record.text in request.messages[0].content[0].text
    # Accounting describes the actual sent request: the plan fits, plus the
    # pinned summary message it adds back.
    assert context_meta["used_tokens"] <= context_meta["input_budget"] + (
        record.tokens_after or 0
    )
    assert context_meta["history_dropped"] > 0
    assert context_meta["strategy"] == "summarize"


def test_strategy_change_does_not_reuse_or_relabel(tmp_path):
    summarizer = RecordingSummarizer()
    # First, produce a ``summarize``-strategy artifact.
    summarize_context = ContextManager(
        tmp_path,
        config=_config(strategy="summarize"),
        summarizer=summarizer,
        counter=len,
    )
    session = SessionManager(tmp_path, assemble=summarize_context).open("s")
    _seed(session, 8)
    session.append_message(Message(role="user", content=[Text(text="current")]))
    _assemble(summarize_context, session)
    assert [r.strategy for r in session.summaries] == ["summarize"]

    # Switching to hybrid must not reuse or relabel the summarize artifact.
    hybrid_context = ContextManager(
        tmp_path,
        config=_config(strategy="hybrid"),
        summarizer=summarizer,
        counter=len,
    )
    request = _assemble(hybrid_context, session)
    context_meta = request.metadata["context"]
    assert context_meta["summary_reused"] is None
    assert context_meta["compacted"]["strategy"] == "hybrid"
    assert [r.strategy for r in session.summaries] == ["summarize", "hybrid"]
