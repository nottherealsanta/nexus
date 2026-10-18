"""Atomic batch and admission tests against real independent SQLite connections."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from nexus.errors import RequestIdConflict, SessionError
from nexus.events import Event
from nexus.model.message import Message, Text
from nexus.session.db import StateDatabase, SqliteSessionStore
from nexus.session.records import EventRecord, MessageRecord
from nexus.session.session import Session


def store_at(path):
    return SqliteSessionStore(StateDatabase(path), project_id="p", namespace="main")


def event_builder(kind="turn.completed"):
    return lambda seq: EventRecord(seq=seq, ts=10, event=Event(type=kind, seq=seq, ts=10))


def index(request="request", digest="digest"):
    return dict(request_id=request, content_digest=digest, turn_id="turn", queued_id="queue", created_at=10)


def test_batch_bookkeeping_and_draft_rollback(tmp_path):
    store = store_at(tmp_path / "nexus.db")
    store.create_draft("s")
    store.append_event("s", Event(type="session.created"))
    before = list(store.read("s").records)
    def fail(seq):
        raise RuntimeError("injected builder failure")
    with pytest.raises(RuntimeError, match="injected"):
        store.append_batch("s", [event_builder(), fail], row_updates={"handoff_count": 1})
    assert store.read("s").records == before
    other = store_at(tmp_path / "nexus.db")
    assert not other.exists("s")
    builders = [lambda seq: MessageRecord(seq=seq, ts=9, message=Message(role="user", content=[Text("hello")])), event_builder()]
    records = store.append_batch("s", builders, row_updates={"context_head_seq": 2, "handoff_count": 1}, index_rows=[index()])
    assert [r.seq for r in records] == [2, 3]
    row = other.db._connection().execute("SELECT last_seq, completion_seq, title, last_user, message_count, context_head_seq, handoff_count FROM sessions").fetchone()
    assert tuple(row) == (3, 3, "hello", "hello", 1, 2, 1)
    assert tuple(other.submission("s", "request", "digest"))[1:] == (3, "turn", "queue")
    assert "s" not in store._drafts


def test_index_failure_rolls_back_records_rows_and_draft(tmp_path):
    store = store_at(tmp_path / "nexus.db")
    store.create_draft("s")
    conn = store.db._connection()
    conn.execute("CREATE TRIGGER reject_submission BEFORE INSERT ON submissions BEGIN SELECT RAISE(ABORT, 'injected index failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.append_batch("s", [event_builder()], row_updates={"handoff_count": 1}, index_rows=[index()])
    assert not store_at(tmp_path / "nexus.db").exists("s")
    assert "s" in store._drafts
    conn.execute("DROP TRIGGER reject_submission")
    store.append_batch("s", [event_builder()], index_rows=[index()])
    before = store.read("s").records
    conn.execute("CREATE TRIGGER reject_record BEFORE INSERT ON records WHEN NEW.seq=3 BEGIN SELECT RAISE(ABORT, 'injected record failure'); END")
    with pytest.raises(SessionError):
        store.append_batch("s", [event_builder(), event_builder()], row_updates={"handoff_count": 8})
    assert store.read("s").records == before
    assert conn.execute("SELECT handoff_count FROM sessions").fetchone()[0] == 0


def test_admission_observes_and_publishes_only_after_commit(tmp_path, monkeypatch):
    store = store_at(tmp_path / "nexus.db")
    store.create_draft("s")
    handle = Session("s", store=store)
    seen = []
    other = store_at(tmp_path / "nexus.db")
    def observe(event):
        assert other.submission("s", "r", "d") is not None
        seen.append(event)
    monkeypatch.setattr(handle, "_publish", observe)
    conn = store.db._connection()
    conn.execute("CREATE TRIGGER reject_submission BEFORE INSERT ON submissions BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        handle.admit_submission("hello", request_id="r", digest="d")
    assert seen == [] and handle.queue_depth == 0
    conn.execute("DROP TRIGGER reject_submission")
    result = handle.admit_submission("hello", request_id="r", digest="d")
    assert result[2] and len(seen) == 1 and handle.queue_depth == 1
    assert handle.admit_submission("hello", request_id="r", digest="d") == (*result[:2], False)
    assert len(seen) == 1
    with pytest.raises(RequestIdConflict):
        handle.admit_submission("different", request_id="r", digest="different")


@pytest.mark.parametrize("conflict", [False, True])
def test_two_independent_connections_race_admission(tmp_path, conflict):
    path = tmp_path / "nexus.db"
    seed = store_at(path)
    seed.create("s")
    barrier = threading.Barrier(2)
    def submit(i):
        store = store_at(path)
        handle = Session("s", store=store)
        barrier.wait(timeout=5)
        try:
            return handle.admit_submission("hello", request_id="r", digest=str(i) if conflict else "same")
        except RequestIdConflict:
            return "conflict"
        finally:
            store.db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [0, 1]))
    assert sum(isinstance(r, tuple) and r[2] for r in results) == 1
    if conflict:
        assert "conflict" in results
    else:
        assert results[0][:2] == results[1][:2]
    assert len(seed.read("s").records) == 1
    assert seed.db._connection().execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1


def test_concurrent_batches_have_consecutive_nonoverlapping_sequences(tmp_path):
    path = tmp_path / "nexus.db"
    store_at(path).create("s")
    barrier = threading.Barrier(2)
    def append(_):
        store = store_at(path)
        barrier.wait(timeout=5)
        return [r.seq for r in store.append_batch("s", [event_builder(), event_builder()])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, [0, 1]))
    assert sorted(results) == [[1, 2], [3, 4]]


def _process_admission(path, barrier, output):
    store = store_at(path)
    handle = Session("s", store=store)
    barrier.wait(timeout=10)
    output.put(handle.admit_submission("hello", request_id="r", digest="d"))
    store.db.close()


def test_two_processes_admit_once(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "nexus.db"
    store = store_at(path)
    store.create("s")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_process_admission, args=(path, barrier, output)) for _ in range(2)]
    for process in processes:
        process.start()
    results = [output.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert results[0][:2] == results[1][:2]
    assert sorted(r[2] for r in results) == [False, True]
    assert len(store.read("s").records) == 1


def test_commit_failure_keeps_draft_and_rolls_back(tmp_path):
    store = store_at(tmp_path / "nexus.db")
    store.create_draft("s")
    conn = store.db._connection()
    # A missing parent fails at COMMIT, not insertion.
    conn.execute("CREATE TABLE commit_parent (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE deferred_failure (id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED)")
    conn.execute("CREATE TRIGGER defer_failure AFTER INSERT ON submissions BEGIN INSERT INTO deferred_failure VALUES (99); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_batch("s", [event_builder()], index_rows=[index()])
    assert not conn.in_transaction
    assert "s" in store._drafts and not store_at(tmp_path / "nexus.db").exists("s")
    conn.execute("DROP TRIGGER defer_failure")
    assert len(store.append_batch("s", [event_builder()], index_rows=[index()])) == 1


@pytest.mark.asyncio
async def test_client_generates_fresh_ids_and_accepts_explicit_retry_id():
    from nexus.client.protocol import Client
    from nexus.host import protocol as p
    class Transport:
        def __init__(self):
            self.commands = []
        async def request(self, command):
            self.commands.append(command)
            if isinstance(command, p.SessionStart):
                return p.SessionStartResult(session=command.session, turn_id="t")
            return p.SessionEnqueueResult(session=command.session, queued_id="q", turn_id="t")
    transport = Transport()
    client = Client(transport)
    await client.start_turn("s", "hello")
    await client.enqueue("s", "hello")
    assert all(command.client_request_id for command in transport.commands)
    assert transport.commands[0].client_request_id != transport.commands[1].client_request_id
    await client.enqueue("s", "hello", client_request_id="retry")
    await client.enqueue("s", "hello", client_request_id="retry")
    assert transport.commands[-1].client_request_id == transport.commands[-2].client_request_id == "retry"


def test_digest_supports_multimodal_bytes_and_canonical_mapping_order():
    from nexus.model.message import Image
    from nexus.session.submissions import submission_digest
    image = Image("image/png", data=b"image bytes")
    assert submission_digest("enqueue", [image], "queue", {"b": 2, "a": 1}) == submission_digest("enqueue", [image], "queue", {"a": 1, "b": 2})


def test_append_event_does_not_observe_failed_write(tmp_path, monkeypatch):
    store = store_at(tmp_path / "nexus.db")
    store.create("s")
    session = Session("s", store=store)
    seen = []
    monkeypatch.setattr(session, "_observe_event", seen.append)
    store.db._connection().execute("CREATE TRIGGER reject_event BEFORE INSERT ON records BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(SessionError):
        session.append_event(Event(type="permission.requested", data={"id": "approval"}))
    assert seen == []


def test_observer_failure_does_not_fail_committed_admission(tmp_path, monkeypatch):
    store = store_at(tmp_path / "nexus.db")
    store.create_draft("s")
    session = Session("s", store=store)

    def fail_publish(event):
        raise RuntimeError("injected observer failure")

    monkeypatch.setattr(session._bus, "publish", fail_publish)
    admitted = session.admit_submission("hello", request_id="req", digest="digest")
    retry = session.admit_submission("hello", request_id="req", digest="digest")
    assert retry == (*admitted[:2], False)
    assert len(store.read("s").events()) == 1


@pytest.mark.asyncio
async def test_restart_keeps_stranded_admission_queued_without_execution(tmp_path):
    from nexus.host.supervisor import Supervisor
    from nexus.session import SessionManager

    manager = SessionManager(tmp_path)
    session = manager.open("s")
    admitted = session.admit_submission("do not execute twice", request_id="req", digest="digest")
    assert manager.store.pending_submission_sessions() == ["s"]
    restarted = SessionManager(tmp_path)
    supervisor = Supervisor()
    assert await supervisor.recover_admissions(restarted) == [{"session": "s", "error": ""}]
    # Nothing is dropped or written: the input stays durable for the next turn.
    assert [event.type for event in restarted.store.read("s").events()] == ["input.queued"]
    assert restarted.store.pending_submission_sessions() == ["s"]
    assert restarted.open("s").queued_ids == (admitted[0],)
    retry = restarted.open("s").admit_submission("do not execute twice", request_id="req", digest="digest")
    assert retry == (*admitted[:2], False)
    await supervisor.recover_admissions(restarted)
    assert len(restarted.store.read("s").events()) == 1


def test_consumed_or_dropped_admission_is_not_pending(tmp_path):
    from nexus.session import SessionManager

    manager = SessionManager(tmp_path)
    consumed = manager.open("c").admit_submission("one", request_id="r1", digest="d1")
    dropped = manager.open("d").admit_submission("two", request_id="r2", digest="d2")
    manager.open("p").admit_submission("three", request_id="r3", digest="d3")
    assert manager.store.pending_submission_sessions() == ["c", "d", "p"]
    manager.store.append_event("c", Event(type="input.consumed", data={"queued_id": consumed[0]}))
    manager.store.append_event("d", Event(type="input.dropped", data={"queued_id": dropped[0]}))
    assert manager.store.pending_submission_sessions() == ["p"]


@pytest.mark.asyncio
async def test_recovery_skips_archived_and_trashed_without_unarchiving(tmp_path):
    from nexus.host.supervisor import Supervisor
    from nexus.session import SessionManager

    manager = SessionManager(tmp_path)
    for name in ("live", "arch"):
        manager.open(name).admit_submission(name, request_id="req", digest=name)
    manager.store.archive("arch", "test")
    assert manager.store.pending_submission_sessions() == ["live"]
    restarted = SessionManager(tmp_path)
    await Supervisor().recover_admissions(restarted)
    assert [row["session_id"] for row in restarted.store.archived_rows()] == ["arch"]
    assert [event.type for event in restarted.store.read("arch").events()] == ["input.queued"]


def test_legacy_queue_without_submission_row_survives(tmp_path):
    from nexus.session import SessionManager

    manager = SessionManager(tmp_path)
    queued_id = manager.open("legacy").enqueue("legacy input")
    assert manager.store.pending_submission_sessions() == []
    assert SessionManager(tmp_path).open("legacy").queued_ids == (queued_id,)


@pytest.mark.asyncio
async def test_unreadable_session_does_not_block_other_recovery(tmp_path):
    from nexus.host.supervisor import Supervisor

    class Store:
        def pending_submission_sessions(self):
            return ["bad", "good"]

        def read(self, session):
            if session == "bad":
                raise SessionError("corrupt")

    manager = type("M", (), {"store": Store()})()
    assert await Supervisor().recover_admissions(manager) == [
        {"session": "bad", "error": "SessionError"},
        {"session": "good", "error": ""},
    ]
