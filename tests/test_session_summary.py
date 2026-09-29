"""Phase 3 durable summary/compaction artifacts (exit criterion 2).

A summary is an append-only, versioned record — never a transcript role. The
original history stays authoritative; snapshots cache and validate the latest
artifact; fork/replay/current-state stay reproducible; and the context manager
appends the artifact **before** it is used in an assembled copy.
"""
from __future__ import annotations

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection, ModelSection
from nexus.context import ContextManager
from nexus.context.compact import SummaryArtifact
from nexus.core.loop import ResolvedModel
from nexus.events import Event
from nexus.model.message import Message, Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.request import ModelRequest
from nexus.session.manager import SessionManager
from nexus.session.records import SummaryRecord
from nexus.session.snapshot import Snapshot, SnapshotSummary


class RecordingSummarizer:
    def __init__(self):
        self.calls = 0
        self.seen = []

    def summarize(self, messages):
        self.calls += 1
        self.seen.append(list(messages))
        return SummaryArtifact(text="SUMMARY-OF-OLD", source_count=len(messages), tokens=3)


def _config(*, max_tokens=400, compaction="summarize", model="scripted/x"):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            context=ContextSection(
                max_tokens=max_tokens,
                safety_margin_tokens=0,
                compaction=compaction,
            ),
        ),
    )


def _seed(session, count=8, size=50):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        session.append_message(
            Message(role=role, content=[Text(text=f"m{index} " * size)])
        )


# ---------------------------------------------------------------------------
# Record shape and append/query
# ---------------------------------------------------------------------------


def test_summary_record_is_not_a_message_or_event(tmp_path):
    session = SessionManager(tmp_path).open("s")
    session.append_message(Message(role="user", content=[Text(text="hi")]))
    record = session.append_summary(
        text="condensed",
        summary_id="sum-1",
        strategy="summarize",
        source_from_seq=1,
        source_to_seq=1,
        source_messages=1,
        tokens_before=10,
        tokens_after=2,
        provider="anthropic",
        model="m",
    )
    assert isinstance(record, SummaryRecord)
    assert record.seq > 0
    # Not surfaced as transcript history or events.
    assert [m.content[0].text for m in session.messages] == ["hi"]
    assert session.events == []
    assert [s.summary_id for s in session.summaries] == ["sum-1"]
    assert session.latest_summary() == record
    assert session.message_seqs() == (1,)


def test_summaries_are_append_only_and_ordered(tmp_path):
    session = SessionManager(tmp_path).open("s")
    session.append_summary(text="a", summary_id="1", source_to_seq=1)
    session.append_summary(text="b", summary_id="2", source_to_seq=2)
    session.append_message(Message(role="user", content=[Text(text="x")]))
    seqs = [record.seq for record in session.summaries]
    assert seqs == sorted(seqs)
    assert session.latest_summary().summary_id == "2"
    # Reopen reads the same artifacts from the log.
    reopened = SessionManager(tmp_path).open("s")
    assert [s.summary_id for s in reopened.summaries] == ["1", "2"]


# ---------------------------------------------------------------------------
# Snapshot includes and validates the summary
# ---------------------------------------------------------------------------


def test_snapshot_carries_and_validates_latest_summary(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("s")
    session.append_message(Message(role="user", content=[Text(text="old")]))
    record = session.append_summary(
        text="condensed",
        summary_id="sum-1",
        strategy="summarize",
        source_from_seq=1,
        source_to_seq=1,
        source_messages=1,
        tokens_before=5,
        tokens_after=2,
    )
    snap = session.write_snapshot()
    assert snap.summary is not None
    assert snap.summary.summary_id == "sum-1"
    assert snap.summary.text == "condensed"
    assert snap.summary.through_seq == record.source_to_seq

    loaded = manager.store.load_snapshot("s", session.read(force=True))
    assert loaded is not None and loaded.summary == snap.summary
    assert session.current.summary == snap.summary


def test_snapshot_with_summary_disagreeing_with_log_is_ignored(tmp_path):
    session = SessionManager(tmp_path).open("s")
    session.append_message(Message(role="user", content=[Text(text="old")]))
    session.append_summary(text="real", summary_id="sum-1", source_to_seq=1)
    read = session.read(force=True)
    wrong = SnapshotSummary(text="forged", through_seq=1, summary_id="sum-1")
    manager = SessionManager(tmp_path)
    manager.store.write_snapshot(
        "s",
        Snapshot(id="s", seq=read.next_seq, messages=read.messages(), summary=wrong),
    )
    assert manager.store.load_snapshot("s", read) is None
    assert session.current.snapshot_seq is None
    # The authoritative summary still wins in current state.
    assert session.current.summary.summary_id == "sum-1"


# ---------------------------------------------------------------------------
# Fork and replay
# ---------------------------------------------------------------------------


def test_fork_copies_summary_and_replay_yields_only_events(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(Message(role="user", content=[Text(text="old")]))
    session.append_summary(text="condensed", summary_id="sum-1", source_to_seq=1)
    session.append_event(Event(type="context.compacted", data={"strategy": "summarize"}))
    session.append_message(Message(role="assistant", content=[Text(text="new")]))

    child = manager.fork("src")
    assert [s.summary_id for s in child.summaries] == ["sum-1"]
    assert child.current.summary.summary_id == "sum-1"

    import asyncio

    async def collect():
        return [event async for event in manager.replay("src")]

    events = asyncio.run(collect())
    # Replay yields public events (including context.compacted), never the
    # summary record itself.
    assert [e.type for e in events] == ["context.compacted"]


# ---------------------------------------------------------------------------
# Context manager persists before use
# ---------------------------------------------------------------------------


def test_context_manager_persists_summary_before_use(tmp_path):
    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    manager = SessionManager(tmp_path, assemble=context)
    session = manager.open("s")
    _seed(session, count=8)
    session.append_message(Message(role="user", content=[Text(text="current")]))

    snapshot = context.for_turn()
    request = snapshot.assemble(session)

    assert summarizer.calls == 1
    summaries = session.summaries
    assert len(summaries) == 1
    record = summaries[0]
    assert record.text == "SUMMARY-OF-OLD"
    assert record.strategy == "summarize"
    assert record.source_messages == 8
    assert record.source_from_seq >= 1
    assert record.source_to_seq >= record.source_from_seq
    assert record.tokens_after is not None
    # The assembled copy contains the summary and the pinned current user.
    assert "SUMMARY-OF-OLD" in request.messages[0].content[0].text
    assert request.messages[-1].content[0].text == "current"
    # Safe, content-free compaction metadata is on the request.
    compacted = request.metadata["context"]["compacted"]
    assert compacted["summary_id"] == record.summary_id
    assert "SUMMARY-OF-OLD" not in str(compacted)
    assert snapshot.last_compacted == compacted


def test_hybrid_without_persistence_degrades_to_drop(tmp_path):
    # No session with append_summary (a plain fake) and no summarizer: hybrid
    # must still produce a request rather than raising.
    class FakeSession:
        id = "s"

        def __init__(self, messages):
            self._messages = messages

        @property
        def messages(self):
            return list(self._messages)

    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path,
        config=_config(compaction="hybrid"),
        summarizer=summarizer,
        counter=len,
    )
    messages = [Message(role="user", content=[Text(text=f"m{i} " * 40)]) for i in range(8)]
    messages.append(Message(role="user", content=[Text(text="current")]))
    request = context.assemble(FakeSession(messages))
    assert request.messages[-1].content[0].text == "current"
    # No durable sink means no model call and no unpersisted summary.
    assert summarizer.calls == 0
    assert request.metadata["context"]["compacted"] is None


def test_summarize_without_summarizer_degrades_and_does_not_break(tmp_path):
    context = ContextManager(
        tmp_path, config=_config(compaction="summarize"), counter=len
    )
    manager = SessionManager(tmp_path, assemble=context)
    session = manager.open("s")
    _seed(session, count=8)
    session.append_message(Message(role="user", content=[Text(text="current")]))
    request = context.for_turn().assemble(session)
    assert request.messages[-1].content[0].text == "current"
    assert session.summaries == []


# ---------------------------------------------------------------------------
# Loop integration: context.compacted event
# ---------------------------------------------------------------------------


class _Resolver:
    def __init__(self, provider):
        self.provider = provider

    def resolve(self, request):
        return ResolvedModel(self.provider, "m", self.provider.capabilities("m"))


def test_completed_turn_emits_context_compacted_with_safe_accounting(tmp_path):
    import asyncio

    summarizer = RecordingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(max_tokens=400), summarizer=summarizer, counter=len
    )
    provider = ScriptedProvider(text_response("done"))
    manager = SessionManager(
        tmp_path, assemble=context, provider_for=_Resolver(provider)
    )
    session = manager.open("s")
    _seed(session, count=8)

    async def run():
        return [event async for event in session.send("current")]

    events = asyncio.run(run())
    types = [event.type for event in events]
    assert "context.compacted" in types
    assert types.count("context.compacted") == 1
    assert types.index("context.compacted") < types.index("model.started")
    compacted = next(e for e in events if e.type == "context.compacted")
    assert compacted.data["summary_id"]
    assert "SUMMARY-OF-OLD" not in str(compacted.data)
    # The event is durable and the summary artifact is durable.
    assert any(e.type == "context.compacted" for e in session.events)
    assert len(session.summaries) == 1
    assert session.summaries[0].summary_id == compacted.data["summary_id"]


class _MetadataAssembler:
    """A stub assembler that returns fixed accounting metadata."""

    def __init__(self, metadata):
        self._metadata = metadata

    def assemble(self, session):
        return ModelRequest(
            messages=list(session.messages),
            provider="scripted",
            model="m",
            metadata=self._metadata,
        )


def _send_with_metadata(tmp_path, metadata, name):
    provider = ScriptedProvider(text_response("ok"))
    manager = SessionManager(
        tmp_path,
        assemble=_MetadataAssembler(metadata),
        provider_for=_Resolver(provider),
    )
    session = manager.open(name)
    import asyncio

    return asyncio.run(_drain_events(session.send("hi")))


async def _drain_events(iterator):
    return [event async for event in iterator]


def test_reuse_metadata_does_not_emit_context_compacted(tmp_path):
    metadata = {
        "context": {"compacted": None, "summary_reused": {"summary_id": "s1"}},
        "cache": {"enabled": False, "boundaries": []},
    }
    events = _send_with_metadata(tmp_path, metadata, "reuse")
    types = [event.type for event in events]
    assert "context.compacted" not in types
    assembled = next(e for e in events if e.type == "context.assembled")
    assert assembled.data["context"]["summary_reused"] == {"summary_id": "s1"}


def test_actual_compaction_emits_context_compacted_once(tmp_path):
    metadata = {
        "context": {
            "compacted": {"summary_id": "s1", "strategy": "summarize"},
            "summary_reused": None,
        },
        "cache": {"enabled": False, "boundaries": []},
    }
    events = _send_with_metadata(tmp_path, metadata, "actual")
    types = [event.type for event in events]
    assert types.count("context.compacted") == 1
    compacted = next(e for e in events if e.type == "context.compacted")
    assert compacted.data["summary_id"] == "s1"
