"""Phase 3 session durability: snapshots, current-state reconstruction, fork/replay.

These tests exercise the exact Phase 3 scope:

* versioned ``<id>.snap.json`` derived snapshots with schema/prefix validation;
* atomic, crash-safe snapshot writes that never touch the authoritative log;
* snapshot-aware current-state reconstruction (snapshot + tail == full log);
* dangling ``ToolUse`` recovery across a snapshot boundary;
* snapshot cadence on completed turns via the ``snapshot_every`` seam;
* fork/replay fidelity and shared-lock-safe concurrent reads.

The log is always the authority. Every snapshot assertion is paired with a
full-log assertion so "omission from the snapshot" can never silently mean
"omission from history".
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import msgspec
import pytest

from nexus.core.loop import ResolvedModel
from nexus.errors import ProviderError
from nexus.events import Event
from nexus.model.message import (
    Document,
    Image,
    Message,
    MessageMeta,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.request import ModelRequest
from nexus.session import snapshot as snapshot_mod
from nexus.session.lock import SessionLock
from nexus.session.manager import SessionManager
from nexus.session.snapshot import (
    SNAPSHOT_VERSION,
    Snapshot,
    SnapshotSummary,
    current_state,
    load,
    write,
)


def _msg(text, role="user", usage=None):
    meta = MessageMeta(usage=usage) if usage is not None else MessageMeta()
    return Message(role=role, content=[Text(text=text)], meta=meta)


def _tool_use(call_id, name="Read"):
    return Message(role="assistant", content=[ToolUse(id=call_id, name=name, input={"p": "x"})])


def _open(tmp_path, name="s"):
    return SessionManager(tmp_path).open(name)


# ---------------------------------------------------------------------------
# Snapshot round-trip and schema
# ---------------------------------------------------------------------------


def test_snapshot_roundtrip_is_a_valid_log_prefix(tmp_path):
    session = _open(tmp_path)
    session.append_message(_msg("hi"))
    session.append_event(Event(type="turn.started", data={"i": 1}))
    last = session.append_message(_msg("yo", role="assistant", usage={"input": 3, "output": 4}))

    snap = session.write_snapshot()

    assert snap.v == SNAPSHOT_VERSION
    assert snap.id == "s"
    assert snap.seq == last.seq
    assert session.snapshot_path.name == "s.snap.json"
    assert session.snapshot_path.read_bytes().endswith(b"\n")
    loaded = load(tmp_path, "s", session.read(force=True))
    assert loaded is not None
    assert loaded.messages == [m for m in session.messages]
    assert loaded.usage == {"input": 3, "output": 4}


def test_snapshot_preserves_bytes_and_tagged_blocks(tmp_path):
    session = _open(tmp_path, "bytes")
    message = Message(
        role="user",
        content=[
            Text(text="see"),
            Thinking(text="hmm", signature="sig-xyz"),
            ToolUse(id="c1", name="Read", input={"path": "a"}),
            ToolResult(
                tool_use_id="c1",
                content=[Text(text="ok"), Image(media_type="image/png", data=b"\x00\x01\xff")],
                is_error=False,
            ),
            Image(media_type="image/jpeg", data=b"\xff\xd8\xff", url=None),
            Document(media_type="application/pdf", data=b"%PDF-1.7\x00", title="d"),
        ],
    )
    session.append_message(message)
    session.write_snapshot()

    state = session.current
    assert state.messages[0] == message
    assert state.messages[0].content[4].data == b"\xff\xd8\xff"
    assert state.messages[0].content[3].content[1].data == b"\x00\x01\xff"
    assert state.messages[0].content[1].signature == "sig-xyz"


def test_snapshot_summary_roundtrips_as_metadata(tmp_path):
    session = _open(tmp_path, "sum")
    session.append_message(_msg("hi"))
    summary = SnapshotSummary(text="condensed", through_seq=1, source="context.compacted")
    session.write_snapshot(summary=summary)
    loaded = load(tmp_path, "sum", session.read(force=True))
    assert loaded is not None
    assert loaded.summary == summary
    # The summary is metadata; messages still come from the authoritative log.
    assert [m.content[0].text for m in session.messages] == ["hi"]


def test_snapshot_never_rewrites_or_truncates_the_log(tmp_path):
    session = _open(tmp_path, "keep")
    session.append_message(_msg("hi"))
    before = session.path.read_bytes()
    session.write_snapshot()
    after = session.path.read_bytes()
    assert after == before
    assert session.path.read_bytes().startswith(before)


# ---------------------------------------------------------------------------
# Validation / fallback
# ---------------------------------------------------------------------------


def _corrupt_snapshot_to_unknown_field(path: Path) -> None:
    payload = msgspec.json.decode(path.read_bytes())
    payload["surprise"] = 1
    path.write_bytes(msgspec.json.encode(payload) + b"\n")


def test_corrupt_snapshot_is_ignored(tmp_path):
    session = _open(tmp_path, "corrupt")
    session.append_message(_msg("hi"))
    session.write_snapshot()
    session.snapshot_path.write_bytes(b"{not json at all")
    state = session.current
    assert state.snapshot_seq is None
    assert [m.content[0].text for m in state.messages] == ["hi"]


def test_unknown_snapshot_field_is_ignored(tmp_path):
    session = _open(tmp_path, "unknown")
    session.append_message(_msg("hi"))
    session.write_snapshot()
    _corrupt_snapshot_to_unknown_field(session.snapshot_path)
    assert session.current.snapshot_seq is None
    assert len(session.messages) == 1


def test_future_version_snapshot_is_ignored(tmp_path):
    session = _open(tmp_path, "future")
    session.append_message(_msg("hi"))
    read = session.read(force=True)
    write(
        tmp_path,
        "future",
        Snapshot(v=SNAPSHOT_VERSION + 1, id="future", seq=1, messages=read.messages()),
    )
    assert load(tmp_path, "future", read) is None
    assert session.current.snapshot_seq is None


def test_wrong_id_snapshot_is_ignored(tmp_path):
    session = _open(tmp_path, "idcheck")
    session.append_message(_msg("hi"))
    read = session.read(force=True)
    write(tmp_path, "idcheck", Snapshot(id="someone-else", seq=1, messages=read.messages()))
    assert session.current.snapshot_seq is None


def test_future_boundary_snapshot_is_ignored(tmp_path):
    session = _open(tmp_path, "ahead")
    session.append_message(_msg("hi"))
    read = session.read(force=True)
    write(tmp_path, "ahead", Snapshot(id="ahead", seq=99, messages=read.messages()))
    assert session.current.snapshot_seq is None
    assert len(session.messages) == 1


def test_stale_prefix_mismatch_snapshot_is_ignored(tmp_path):
    session = _open(tmp_path, "stale")
    session.append_message(_msg("hi"))
    wrong = Message(role="user", content=[Text(text="different")])
    write(tmp_path, "stale", Snapshot(id="stale", seq=1, messages=[wrong]))
    assert session.current.snapshot_seq is None
    assert [m.content[0].text for m in session.messages] == ["hi"]


def test_usage_mismatch_snapshot_is_ignored(tmp_path):
    session = _open(tmp_path, "usage")
    session.append_message(_msg("hi", usage={"input": 1}))
    read = session.read(force=True)
    write(
        tmp_path,
        "usage",
        Snapshot(id="usage", seq=1, messages=read.messages(), usage={"input": 999}),
    )
    assert session.current.snapshot_seq is None
    assert session.current.usage == {"input": 1}


# ---------------------------------------------------------------------------
# Snapshot + tail reconstruction equals full log
# ---------------------------------------------------------------------------


def test_snapshot_plus_tail_equals_full_log_messages_events_usage(tmp_path):
    session = _open(tmp_path, "tail")
    session.append_message(_msg("q"))
    session.append_message(_msg("a", role="assistant", usage={"input": 10, "output": 2}))
    # Snapshot covers only the first two records, so events and later messages
    # have to come from the log tail.
    session.write_snapshot(through_seq=2)
    session.append_event(Event(type="turn.completed", data={"usage": {"input_tokens": 10}}))
    session.append_message(_msg("again"))
    session.append_message(_msg("more", role="assistant", usage={"input": 5, "output": 7}))

    read = session.read(force=True)
    loaded = load(tmp_path, "tail", read)
    assert loaded is not None and loaded.seq == 2
    state = current_state(read, loaded)

    assert state.snapshot_seq == 2
    assert state.messages == tuple(read.messages())
    assert state.events == tuple(read.events())
    assert state.records == read.records
    assert state.usage == snapshot_mod.accumulate_usage(read.records)
    assert state.usage == {"input": 15, "output": 9}


def test_public_records_and_events_never_omit_history(tmp_path):
    session = _open(tmp_path, "authoritative")
    session.append_message(_msg("one"))
    session.append_event(Event(type="turn.started"))
    session.write_snapshot()
    session.append_message(_msg("two"))

    # Even with a valid snapshot, public surfaces expose the full log.
    assert len(session.records) == 3
    assert [e.type for e in session.events] == ["turn.started"]
    assert [m.content[0].text for m in session.messages] == ["one", "two"]
    assert session.current.snapshot_seq == 2


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


def _snapshot_for(session, seq):
    read = session.read(force=True)
    return snapshot_mod.build_from_records(session.id, read.records, seq)


def test_interrupted_write_leaves_no_snapshot(tmp_path):

    def boom(fd):
        raise OSError("disk full")

    session = _open(tmp_path, "crash")
    session.append_message(_msg("hi"))
    snap = _snapshot_for(session, 1)
    with pytest.raises(OSError):
        write(tmp_path, "crash", snap, fsync=boom)
    assert not session.snapshot_path.exists()
    assert list(tmp_path.glob(".crash-*")) == []


def test_interrupted_write_keeps_prior_valid_snapshot(tmp_path):
    session = _open(tmp_path, "prior")
    session.append_message(_msg("hi"))
    session.append_message(_msg("yo", role="assistant", usage={"input": 1, "output": 1}))
    session.write_snapshot(through_seq=1)
    before = session.snapshot_path.read_bytes()
    assert load(tmp_path, "prior", session.read(force=True)) is not None

    def boom(fd):
        raise OSError("disk full")

    with pytest.raises(OSError):
        write(tmp_path, "prior", _snapshot_for(session, 2), fsync=boom)

    assert session.snapshot_path.read_bytes() == before
    assert list(tmp_path.glob(".prior-*")) == []
    loaded = load(tmp_path, "prior", session.read(force=True))
    assert loaded is not None and loaded.seq == 1


# ---------------------------------------------------------------------------
# Dangling tool-use recovery across snapshot + tail
# ---------------------------------------------------------------------------


def test_dangling_recovery_across_snapshot_and_tail_never_executes(tmp_path):
    session = _open(tmp_path, "dangle")
    session.append_message(_msg("go"))
    session.append_message(_tool_use("c1"))
    session.write_snapshot()  # c1 is now inside the snapshot prefix
    session.append_message(_tool_use("c2", name="Write"))

    recovered = session.recover_dangling_tool_uses()
    assert len(recovered) == 1
    blocks = recovered[0].message.content
    assert [block.tool_use_id for block in blocks] == ["c1", "c2"]
    assert all(block.is_error for block in blocks)
    assert "c1" in blocks[0].content[0].text
    assert "Write" in blocks[1].content[0].text

    before = session.path.read_bytes()
    assert session.recover_dangling_tool_uses() == []
    assert session.path.read_bytes() == before
    # A reopen sees the snapshot plus the recovery record and stays idempotent.
    reopened = SessionManager(tmp_path).open("dangle")
    assert reopened.recover_dangling_tool_uses() == []
    assert reopened.current.snapshot_seq is not None


def test_snapshot_aware_recovery_finds_prefix_and_tail_on_reopen(tmp_path):
    session = _open(tmp_path, "dangle2")
    session.append_message(_tool_use("prefix-call"))
    session.write_snapshot()
    session.append_message(_tool_use("tail-call"))
    # No explicit recovery yet; reopening recovers both through snapshot+tail.
    reopened = SessionManager(tmp_path).open("dangle2")
    assert len(reopened.recovered) == 1
    ids = [b.tool_use_id for b in reopened.recovered[0].message.content]
    assert ids == ["prefix-call", "tail-call"]


# ---------------------------------------------------------------------------
# Cadence seam
# ---------------------------------------------------------------------------


class _Assembler:
    def assemble(self, session):
        return ModelRequest(messages=list(session.messages), provider="scripted", model="m")


class _Resolver:
    def __init__(self, provider):
        self.provider = provider

    def resolve(self, request):
        return ResolvedModel(self.provider, "m", self.provider.capabilities("m"))


def _sendable(tmp_path, provider, *, name, snapshot_every=None):
    manager = SessionManager(
        tmp_path,
        assemble=_Assembler(),
        provider_for=_Resolver(provider),
        snapshot_every=snapshot_every,
    )
    return manager.open(name)


async def _drain(iterator):
    return [event async for event in iterator]


async def test_no_snapshot_without_cadence(tmp_path):
    provider = ScriptedProvider(text_response("a"))
    session = _sendable(tmp_path, provider, name="noca", snapshot_every=None)
    await _drain(session.send("one"))
    assert not session.snapshot_path.exists()


async def test_completed_turn_writes_snapshot_at_cadence(tmp_path):
    provider = ScriptedProvider(text_response("a"), text_response("b"))
    session = _sendable(tmp_path, provider, name="cad", snapshot_every=2)

    await _drain(session.send("one"))
    assert not session.snapshot_path.exists()

    await _drain(session.send("two"))
    assert session.snapshot_path.exists()
    loaded = load(tmp_path, "cad", session.read(force=True))
    assert loaded is not None
    assert loaded.seq == session.read(force=True).next_seq
    assert [m.role for m in loaded.messages] == [m.role for m in session.messages]


async def test_snapshot_cadence_callable_seam(tmp_path):
    seen = {"calls": 0}

    def every():
        seen["calls"] += 1
        return 1

    provider = ScriptedProvider(text_response("a"))
    session = _sendable(tmp_path, provider, name="call", snapshot_every=every)
    await _drain(session.send("one"))
    assert session.snapshot_path.exists()
    assert seen["calls"] >= 1


async def test_failed_turn_does_not_snapshot(tmp_path):
    provider = ScriptedProvider([ProviderError("boom")])
    session = _sendable(tmp_path, provider, name="fail", snapshot_every=1)
    events = await _drain(session.send("x"))
    assert events[-1].type == "turn.failed"
    assert not session.snapshot_path.exists()


def test_invalid_cadence_is_rejected(tmp_path):
    session = _open(tmp_path, "badcad")
    session.bind(snapshot_every=0)
    with pytest.raises(ValueError):
        session.maybe_snapshot()


# ---------------------------------------------------------------------------
# Concurrency: readers never disturb an active writer
# ---------------------------------------------------------------------------


def test_fork_reads_consistent_prefix_while_writer_holds_lock(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("one"))
    lock = SessionLock.for_session(tmp_path, "src")
    lock.acquire(shared=False, blocking=False)
    try:
        child = manager.fork("src")
    finally:
        lock.release()
    assert [m.content[0].text for m in child.messages] == ["one"]


async def test_replay_while_writer_holds_lock(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_event(Event(type="custom.unknown", data={"x": 1}))
    lock = SessionLock.for_session(tmp_path, "src")
    lock.acquire(shared=False, blocking=False)
    try:
        events = [event async for event in manager.replay("src")]
    finally:
        lock.release()
    assert [event.type for event in events] == ["custom.unknown"]


def test_fork_ignores_partial_crash_tail(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("complete"))
    with open(session.path, "ab") as handle:
        handle.write(b'{"type":"message","seq":2,"message":')
    child = manager.fork("src")
    assert [m.content[0].text for m in child.messages] == ["complete"]


def test_concurrent_replays_do_not_conflict(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_event(Event(type="turn.started"))

    async def run():
        return [event async for event in manager.replay("src")]

    async def main():
        results = await asyncio.gather(run(), run(), run())
        assert all([e.type for e in result] == ["turn.started"] for result in results)

    asyncio.run(main())
