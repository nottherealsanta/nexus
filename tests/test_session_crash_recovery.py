"""Offline SIGKILL crash durability for handoffs and child lifecycle (plan 01 section 7, F1-F6).

Scope, stated plainly: each case runs a real child *process* (``python -c``) over
the real SQLite session store, ownership rows, ``fcntl`` turn locks, ``Session``
and (for F4/F6) ``ContextManager`` handoff code, then ``SIGKILL``s it at a
deterministic barrier and reopens the same on-disk state in this process.

What this is NOT: no Nexus daemon/host process, no ``Runtime``, no provider or
model stream, no network and no credentials. F2's "model stream" is the
persisted state a stream leaves behind (lease held, ``turn.started`` and
``model.started`` written, no terminal event). Daemon-level kill/restart
coverage remains separate.

Barriers are sentinel lines on the child's stdout followed by a blocking read of
its stdin; the parent waits for the sentinel (bounded), never sleeps, and kills
the child while it is parked at that exact point. Injection uses no production
hooks: a SQLite trace callback on the store connection (``mid_batch``), a wrapper
around ``db.transaction`` that parks before its COMMIT (``before_commit``), a wrapper around ``Session._publish`` (``after_commit``) and a
replaced module-level ``build_brief`` (brief build).
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection
from nexus.context.manager import ContextManager
from nexus.model.message import Message, Text
from nexus.session import SessionManager
from nexus.session.records import EventRecord, MessageRecord, SummaryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
BARRIER_TIMEOUT = 60
EXIT_TIMEOUT = 15

# Keep in step with CHILD below: realistic headroom, so one handoff is enough.
CONFIG = Config(v2=ConfigV2(context=ContextSection(
    max_tokens=2000, safety_margin_tokens=0, compact_at_fraction=1,
    on_overflow="handoff", handoff_brief="deterministic", compaction="drop_oldest")))

CHILD = r'''
import os, sqlite3, sys
scenario, home = sys.argv[1], sys.argv[2]
from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection
from nexus.context.manager import ContextManager
from nexus.events import Event
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.session import SessionManager

CONFIG = Config(v2=ConfigV2(context=ContextSection(
    max_tokens=2000, safety_margin_tokens=0, compact_at_fraction=1,
    on_overflow="handoff", handoff_brief="deterministic", compaction="drop_oldest")))

def barrier():
    sys.stdout.write("BARRIER " + scenario + "\n")
    sys.stdout.flush()
    sys.stdin.readline()  # parked until SIGKILL; EOF means the parent died
    os._exit(3)

def msg(role, text):
    return Message(role=role, content=[Text(text=text)])

def seed():
    manager = SessionManager(home)
    session = manager.open("demo")
    for i in range(8):
        session.append_message(msg("user" if i % 2 == 0 else "assistant",
                                   f"original-{i} " + "word " * 80))
    session.enqueue("already queued")
    return manager, session

def assemble(session):
    context = ContextManager(home, config=CONFIG, counter=len, identity="")
    with session.begin_turn():
        context.assemble(session)

def own():
    return {"owner_namespace": "main", "owner_id": "root", "owner_turn_id": "t",
            "owner_call_id": "call", "agent_id": "child", "logical_session_id": "root/sub/1"}

META = {"id": "child", "parent": "root", "depth": 1, "type": "native",
        "task": "task-1", "session": "child"}

def spawn_parent():
    # Production order: the parent holds its turn lease before turn.started and
    # agent.spawned are appended; the lease dies with the killed process.
    root = SessionManager(home)
    session = root.open("root")
    session.append_message(msg("user", "delegate"))
    session.begin_turn(turn_id="t")
    session.append_event(Event(type="turn.started", session="root", turn="t", data={}))
    session.append_event(Event(type="agent.spawned", session="root", turn="t",
                               data={"agent": META, **META}))
    return session

if scenario == "brief_build":
    manager, session = seed()
    import nexus.session.handoff as handoff_module
    handoff_module.build_brief = lambda *a, **k: barrier()
    assemble(session)
elif scenario in ("before_commit", "mid_batch"):
    manager, session = seed()
    db = manager.store.db
    def submissions(conn):
        return conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    if scenario == "mid_batch":
        # Trace fires on every execution (the authorizer only runs at prepare, so cached
        # statements bypass it): park just before the batch's submissions INSERT, after
        # its record rows were written inside the open transaction.
        def trace(statement):
            if statement.lstrip().upper().startswith("INSERT INTO SUBMISSIONS"):
                barrier()
        db._connection().set_trace_callback(trace)
    else:
        # Park after the batch body ran, with every row written, right before the
        # transaction's COMMIT (contextmanager resumes here, then executes COMMIT).
        import contextlib
        original = db.transaction
        @contextlib.contextmanager
        def transaction():
            with original() as conn:
                before = submissions(conn)
                yield conn
                if submissions(conn) > before:
                    barrier()
        db.transaction = transaction
    session.handoff(summary="crash brief", request_id="req-1", continuation="resume")
elif scenario in ("after_commit_direct", "after_commit_turn"):
    manager, session = seed()
    original = session._publish
    def publish(event):
        if event.type == "context.handoff":
            barrier()
        return original(event)
    session._publish = publish
    if scenario == "after_commit_direct":
        session.handoff(summary="crash brief", request_id="req-1", continuation="resume")
    else:
        assemble(session)
elif scenario == "spawn_unpromoted":
    session = spawn_parent()
    # Reserved, never promoted: an owned handle writes its row with its first record.
    SessionManager(home, namespace="agents").open("child", recover=False, ownership=own())
    barrier()
elif scenario in ("child_stream", "child_locked"):
    session = spawn_parent()
    agents = SessionManager(home, namespace="agents")
    child = agents.open("child", recover=False, ownership=own())
    child.append_message(msg("user", "child task"))
    child.begin_turn(turn_id="ct")  # lease first, then turn.started, as in production
    child.append_event(Event(type="turn.started", session="child", turn="ct", data={}))
    if scenario == "child_stream":
        child.append_message(Message(role="assistant", content=[ToolUse(id="tool", name="read", input={})]))
        child.append_event(Event(type="tool.started", session="child", turn="ct", data={"id": "tool", "name": "read"}))
        child.append_event(Event(type="tool.completed", session="child", turn="ct", data={"id": "tool", "name": "read"}))
        child.append_message(Message(role="user", content=[ToolResult(tool_use_id="tool", content=[Text(text="ok")])]))
        child.append_event(Event(type="model.started", session="child", turn="ct", data={"model": "scripted/m"}))
    barrier()
else:
    raise SystemExit("unknown scenario " + scenario)
# Hook scenarios park inside their hook; reaching here means the hook never fired.
sys.exit("scenario finished without reaching its crash point")
'''


class Crash:
    """Launches barrier-parked children and guarantees none outlive the test."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self._tmp = tmp_path
        self._procs: list[tuple[subprocess.Popen, object]] = []

    def start(self, scenario: str) -> subprocess.Popen:
        """Run ``scenario`` and return once the child is parked at its barrier."""
        stderr_path = self._tmp / f"{scenario}.stderr"
        stderr = open(stderr_path, "w+")
        env = {  # no inherited credentials; storage is the temporary home only
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self._tmp / "user-home"),
            "NEXUS_HOME": str(self._tmp / "nexus-home"),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD, scenario, str(self.home)],
            cwd=REPO_ROOT, env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
        )
        self._procs.append((proc, stderr))
        arrived: queue.Queue[bool] = queue.Queue()

        def pump() -> None:
            for line in proc.stdout:  # type: ignore[union-attr]
                if line.strip() == f"BARRIER {scenario}":
                    arrived.put(True)
                    return
            arrived.put(False)

        threading.Thread(target=pump, daemon=True).start()
        try:
            reached = arrived.get(timeout=BARRIER_TIMEOUT)
        except queue.Empty:
            proc.kill()
            pytest.fail(f"{scenario}: barrier not reached\n{stderr_path.read_text()}")
        if not reached:
            proc.wait(EXIT_TIMEOUT)
            pytest.fail(f"{scenario}: child exited {proc.returncode} before its barrier\n"
                        f"{stderr_path.read_text()}")
        return proc

    def kill(self, proc: subprocess.Popen) -> None:
        proc.send_signal(signal.SIGKILL)
        proc.wait(EXIT_TIMEOUT)
        assert proc.returncode == -signal.SIGKILL  # killed at the barrier, did not exit on its own

    def crash(self, scenario: str) -> None:
        self.kill(self.start(scenario))

    def close(self) -> None:
        for proc, stderr in self._procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(EXIT_TIMEOUT)
            for stream in (proc.stdin, proc.stdout, stderr):
                if stream is not None:
                    stream.close()


@pytest.fixture
def crash(tmp_path):
    harness = Crash(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


# -- shared assertions -------------------------------------------------------


def _snapshot(home: Path, session: str = "demo"):
    store = SessionManager(home).store
    records = list(store.read(session).records)
    row = store.session_row(session)
    submissions = store.db._connection().execute(
        "SELECT COUNT(*) FROM submissions WHERE project_id=? AND namespace=? AND session_id=?",
        (store.project_id, store.namespace, session)).fetchone()[0]
    return store, records, row, submissions


def assert_handoff_state(home: Path, *, heads: int, queued: int, submissions: int, messages: int = 8):
    """Durable rows, log and replay agree: a handoff is fully committed or absent."""
    store, records, row, indexed = _snapshot(home)
    assert [r.seq for r in records] == list(range(1, len(records) + 1))
    assert row["last_seq"] == len(records)
    head_records = [r for r in records if isinstance(r, SummaryRecord) and r.strategy == "handoff"]
    markers = [r for r in records if isinstance(r, EventRecord) and r.event.type == "context.handoff"]
    queued_events = [r.event.data["queued_id"] for r in records
                     if isinstance(r, EventRecord) and r.event.type == "input.queued"]
    assert len(head_records) == len(markers) == row["handoff_count"] == heads
    assert len(queued_events) == queued and indexed == submissions
    assert sum(isinstance(r, MessageRecord) for r in records) == messages  # nothing retired or lost
    # Seed is `messages` message records plus the pre-queued input.queued; a handoff adds
    # exactly a head and its marker (plus its continuation's input.queued, counted in
    # `queued`). Any other record is a stray partial write.
    assert len(records) == messages + queued + 2 * heads
    if heads:
        head = head_records[-1]
        assert row["context_head_seq"] == head.seq == store.context_head("demo").seq
        assert markers[-1].seq == head.seq + 1  # marker is committed with its head
        assert markers[-1].event.data["summary_seq"] == head.seq
    else:
        assert not row["context_head_seq"] and store.context_head("demo") is None
    # Replay through a fresh handle sees exactly the durable state.
    replay = SessionManager(home).open("demo", recover=False)
    assert (replay.latest_summary().seq if heads else replay.latest_summary()) == (
        head_records[-1].seq if heads else None)
    assert replay.queued_ids == tuple(queued_events)  # queued input is still queued
    return queued_events


def next_turn(home: Path, extra: str | None = None):
    manager = SessionManager(home)
    session = manager.open("demo", recover=False)
    if extra:
        session.append_message(Message(role="user", content=[Text(text=extra)]))
    with session.begin_turn():
        request = ContextManager(home, config=CONFIG, counter=len, identity="").assemble(session)
    return session, request


def flatten(tree) -> list[dict]:
    out: list[dict] = []

    def walk(node):
        out.append(node)
        for child in node["children"]:
            walk(child)

    for node in [*tree["nodes"], *tree["unattached"]]:
        walk(node)
    return out


# -- F4/F5/F6: handoff atomicity ----------------------------------------------


def test_f4_kill_during_brief_build_leaves_no_head_then_one_head(crash):
    crash.crash("brief_build")
    pending = assert_handoff_state(crash.home, heads=0, queued=1, submissions=0)
    session, request = next_turn(crash.home)
    assert_handoff_state(crash.home, heads=1, queued=1, submissions=0)
    head = session.latest_summary()
    assert request.messages[0].content[0].text == head.text
    again, _ = next_turn(crash.home)  # a further turn must not write a second head
    assert_handoff_state(crash.home, heads=1, queued=1, submissions=0)
    assert again.latest_summary().seq == head.seq
    assert again.queued_ids == tuple(pending)


@pytest.mark.parametrize("point", ["before_commit", "mid_batch"])
def test_f5_kill_inside_append_batch_makes_neither_record_nor_event_visible(crash, point):
    crash.crash(point)
    pending = assert_handoff_state(crash.home, heads=0, queued=1, submissions=0)
    session = SessionManager(crash.home).open("demo", recover=False)
    result = session.handoff(summary="crash brief", request_id="req-1", continuation="resume")
    assert result["fresh"]  # the dead attempt left no submission behind to dedupe against
    queued = assert_handoff_state(crash.home, heads=1, queued=2, submissions=1)
    assert queued[0] == pending[0]
    retry = SessionManager(crash.home).open("demo", recover=False).handoff(
        summary="crash brief", request_id="req-1", continuation="resume")
    assert not retry["fresh"]
    assert_handoff_state(crash.home, heads=1, queued=2, submissions=1)


def test_f6_kill_after_commit_before_request_keeps_one_head_and_starts_at_brief(crash):
    crash.crash("after_commit_turn")
    assert_handoff_state(crash.home, heads=1, queued=1, submissions=0)
    head_seq = SessionManager(crash.home).store.session_row("demo")["context_head_seq"]
    session, request = next_turn(crash.home, extra="ok")
    assert_handoff_state(crash.home, heads=1, queued=1, submissions=0, messages=9)
    assert SessionManager(crash.home).store.session_row("demo")["context_head_seq"] == head_seq
    assert request.messages[0].content[0].text == session.latest_summary().text


def test_f6_kill_after_direct_handoff_commit_dedupes_retry_and_keeps_queue(crash):
    crash.crash("after_commit_direct")
    queued = assert_handoff_state(crash.home, heads=1, queued=2, submissions=1)
    session = SessionManager(crash.home).open("demo", recover=False)
    retry = session.handoff(summary="crash brief", request_id="req-1", continuation="resume")
    assert not retry["fresh"]
    assert assert_handoff_state(crash.home, heads=1, queued=2, submissions=1) == queued


# -- F1/F2/F3: child lifecycle ------------------------------------------------


def _children_never_running(manager) -> None:
    assert not [n["id"] for n in flatten(manager.tree("root")) if n["status"] == "running"]


def test_f1_kill_after_spawn_before_promotion_closes_parent_and_shows_no_child(crash):
    crash.crash("spawn_unpromoted")
    manager = SessionManager(crash.home)
    agents = SessionManager(crash.home, namespace="agents")
    assert not agents.exists("child") and manager.store.owned_rows("main", "root") == []
    reopened = manager.open("root")
    completed = [e for e in reopened.events if e.type == "agent.completed"]
    assert len(completed) == 1
    assert completed[0].data["agent"]["id"] == "child" and completed[0].data["status"] == "interrupted"
    assert len([e for e in reopened.events if e.type == "turn.failed"]) == 1
    assert not agents.exists("child") and agents.store.owned_rows("main", "root") == []
    tree = manager.tree("root")
    assert [n["children"] for n in tree["nodes"]] == [[]]
    _children_never_running(manager)
    assert SessionManager(crash.home).open("root").events == reopened.events  # idempotent


def test_f2_kill_during_child_stream_recovers_owned_child_log_adding_only_turn_failed(crash):
    crash.crash("child_stream")
    manager = SessionManager(crash.home)
    agents = SessionManager(crash.home, namespace="agents")
    before = list(agents.store.read("child").records)
    _children_never_running(manager)  # lock died with the process; no stale "running"
    assert not any(isinstance(r, EventRecord) and r.event.type in {"turn.failed", "turn.cancelled"}
                   for r in before)
    reopened = manager.open("root")
    completed = [e for e in reopened.events if e.type == "agent.completed"]
    assert len(completed) == 1 and completed[0].data["agent"]["id"] == "child"
    assert completed[0].data["status"] == "interrupted"
    assert len([e for e in reopened.events if e.type == "turn.failed"]) == 1
    after = list(agents.store.read("child").records)
    assert after[:len(before)] == before  # recovery only appends
    added = after[len(before):]
    assert [r.event.type for r in added if isinstance(r, EventRecord)] == ["turn.failed"]
    assert len(added) == 1 and added[0].event.data["reason"] == "interrupted"
    # Recovery added no tool events: still exactly the one start/finish from before the crash
    # (no executor runs here; this only pins the log).
    types = [r.event.type for r in after if isinstance(r, EventRecord)]
    assert types.count("tool.started") == 1 and types.count("tool.completed") == 1
    _children_never_running(manager)
    again = SessionManager(crash.home).open("root")
    assert len(list(agents.store.read("child").records)) == len(after) and again.events == reopened.events


def test_f3_second_holder_keeps_child_running_until_it_dies(crash):
    holder = crash.start("child_locked")
    manager = SessionManager(crash.home)
    agents = SessionManager(crash.home, namespace="agents")
    before = list(agents.store.read("child").records)
    manager.open("root")  # "first daemon" recovery: the child lock is held elsewhere
    assert list(agents.store.read("child").records) == before
    nodes = {n["id"]: n for n in flatten(manager.tree("root"))}
    assert nodes["child"]["status"] == "running"
    assert nodes["root"]["running_children"] == 1
    crash.kill(holder)
    SessionManager(crash.home).open("root")
    after = list(agents.store.read("child").records)
    assert [r.event.type for r in after[len(before):] if isinstance(r, EventRecord)] == ["turn.failed"]
    _children_never_running(manager)
    assert {n["id"]: n for n in flatten(manager.tree("root"))}["root"]["running_children"] == 0
