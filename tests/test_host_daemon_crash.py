"""Real daemon SIGKILL, restart and reconnect (plan 01 section 7, daemon half).

``tests/test_session_crash_recovery.py`` kills a bare storage process. These tests
kill a real ``nexus.host.daemon.Daemon`` *subprocess* (offline ``Runtime`` with a
``ScriptedProvider``, real SQLite store, real turn locks, real Unix socket), start
a second daemon over the same isolated home through :func:`ensure_daemon`, and
reconnect with a new :class:`UDSClient`. Three cases:

* **T1** kill after a handoff committed, before its continuation was scheduled;
* **T2** kill while a turn is running with a second submission admitted behind it;
* **T3** kill while a native ``subagent`` child's model stream is interrupted
  (parked in its third provider call) after its ``bash`` call already completed.

What they pin: the restarted process is a different pid; no provider call happens
unprompted (bounded window); after the first ``SessionTree`` following restart,
``SessionHandoffRead`` / ``SessionTree`` (T2, T3) and ``SessionInspect`` (T3)
leave every node's ``last_seq`` unchanged and make no provider call. Startup
work before that baseline is not asserted (T2 shows startup only reports).
Opening paths (``SessionOpen``, ``SessionSubscribe``) repair abandoned
turns/children exactly once and idempotently but never resume, replay or run
anything; request-id retries return the original identity and add no record;
durable queued input is not auto-run and a fresh request consumes it exactly
once. T3 pins only that an already *completed* bash side effect is not replayed
(effect file keeps one line) and that ``SessionInspect`` shows that bash call
completed with no running tool. An in-flight (running) tool at kill time is
**not** covered. They deliberately do **not** pin resumption of the interrupted
turn: a retried ``SessionStart`` returns its historical turn id, and that turn
stays failed. Known policy gap, unchanged here: that historical returned id was
never started again (its queued work is consumed under a fresh turn id).

Scope limits: the "no unsolicited provider call" checks observe a bounded quiet
window, not forever. Injection is test-owned: a driver script (this file's
``DRIVER``) patches ``Session._publish`` for T1 and uses provider barrier steps
for T2/T3; there are no production hooks. Single machine, no network, no
credentials, no concurrent second daemon (the F3 lock case stays in the storage
suite).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from nexus.host import protocol as p
from nexus.host.daemon import ensure_daemon
from nexus.host.transports.uds import UDSClient

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 30.0
QUIET = 0.6  # bounded window used for "nothing happens unprompted" checks

DRIVER = r'''
import argparse
import asyncio
import json
import os
import shlex
import threading
from pathlib import Path

from nexus.config import Config
from nexus.config.schema import (
    AgentSection, AgentsSection, ConfigV2, ModelSection, PermissionsSection,
    SessionsSection, ToolsSection,
)
from nexus.host.daemon import Daemon
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.runtime import Runtime

CALLS = os.environ["NEXUS_T_CALLS"]
SENTINEL = os.environ["NEXUS_T_SENTINEL"]
PLAN = json.loads(os.environ["NEXUS_T_PLAN"])
EFFECT = os.environ.get("NEXUS_T_EFFECT", "")

if os.environ.get("NEXUS_T_HANDOFF_SENTINEL"):
    # Park inside the handoff's after-commit publish: the SQLite batch is
    # committed, the continuation has not been handed to the supervisor yet.
    from nexus.session.session import Session

    _original_publish = Session._publish

    def _publish(self, event):
        if event.type == "context.handoff":
            Path(os.environ["NEXUS_T_HANDOFF_SENTINEL"]).write_text("reached")
            threading.Event().wait()
        return _original_publish(self, event)

    Session._publish = _publish


def _texts(request):
    out = []
    for message in request.messages:
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                out.append(text)
    return out


def _step(kind):
    async def step(request):
        with open(CALLS, "a") as handle:
            handle.write(json.dumps(_texts(request)) + "\n")
        if kind == "barrier":
            Path(SENTINEL).write_text("reached")
            await asyncio.Event().wait()  # the event loop stays responsive
        if kind == "subagent":
            return tool_response(("spawn", "subagent", {"prompt": "child job"}))
        if kind == "bash":
            return tool_response(("shell", "bash", {"command": "echo ran >> " + shlex.quote(EFFECT)}))
        return text_response(kind.split(":", 1)[1] if ":" in kind else "ok")

    return step


def make_runtime(workspace, **kwargs):
    scripts = [[_step(kind)] for kind in PLAN] + [[_step("text:ok")] for _ in range(64)]
    config = Config(model="scripted/m", version=2, v2=ConfigV2(
        model=ModelSection(default="scripted/m"),
        agent=AgentSection(profile="coding"),
        permissions=PermissionsSection(mode="allow", on_unattended="allow", write_roots=["./"]),
        tools=ToolsSection(),
        agents=AgentsSection(max_tier="medium", max_depth=3, max_concurrent=4, seed_roles=False, background=False),
        sessions=SessionsSection(auto_title=False),
    ))
    return Runtime(
        workspace, home=kwargs.get("home"), environ=kwargs.get("environ"),
        config=config, providers={"scripted": ScriptedProvider(*scripts)},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--socket", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--idle-timeout", type=float, default=300.0)
    parser.add_argument("--max-concurrent-turns", type=int, default=4)
    args = parser.parse_args()
    daemon = Daemon(
        Path(args.workspace), home=args.home, socket_path=args.socket,
        idle_timeout=args.idle_timeout, max_concurrent_turns=args.max_concurrent_turns,
        runtime_factory=make_runtime,
    )
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
'''


class Harness:
    """Owns one isolated home/socket and every daemon process started over it."""

    def __init__(self, root: Path):
        self.root = root
        (root / "ws").mkdir(exist_ok=True)
        self.workspace = (root / "ws").resolve()  # /tmp is a symlink on macOS
        self.socket = root / "d.sock"  # short path: AF_UNIX limit on macOS
        self.driver = root / "driver.py"
        self.driver.write_text(DRIVER, encoding="utf-8")
        self.effect = self.workspace / "effect.txt"
        self.procs: list[subprocess.Popen] = []
        self.clients: list[UDSClient] = []
        self._logs: list = []
        self._env: dict[str, str] = {}
        self.phase = 0
        self.pid = 0

    # -- per-phase files ----------------------------------------------------

    @property
    def calls_path(self) -> Path:
        return self.root / f"calls-{self.phase}.jsonl"

    @property
    def sentinel(self) -> Path:
        return self.root / f"sentinel-{self.phase}"

    @property
    def handoff_sentinel(self) -> Path:
        return self.root / f"handoff-sentinel-{self.phase}"

    def calls(self) -> list[list[str]]:
        """Provider calls of the current daemon generation, as user-visible texts."""
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines() if line]

    def effect_lines(self) -> int:
        return len(self.effect.read_text().splitlines()) if self.effect.exists() else 0

    # -- process control ----------------------------------------------------

    def _spawn(self, workspace, path, *, home=None, idle_timeout=None,
               max_concurrent_turns=None, environ=None):
        argv = [sys.executable, str(self.driver), "--workspace", str(workspace),
                "--socket", str(path)]
        log = open(self.root / f"daemon-{self.phase}.log", "w")
        self._logs.append(log)
        proc = subprocess.Popen(
            argv, cwd=str(workspace), start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=self._env,
        )
        self.procs.append(proc)
        return proc

    async def start(self, plan: list[str], *, handoff_barrier: bool = False) -> UDSClient:
        """Start a daemon generation through ``ensure_daemon`` and connect to it."""
        self.phase += 1
        self._env = {  # no inherited credentials; storage is the temporary root only
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root / "user-home"),
            "NEXUS_HOME": str(self.root / "nexus-home"),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NEXUS_IMAGE_PROTOCOL": "halfblocks",
            "NEXUS_T_CALLS": str(self.calls_path),
            "NEXUS_T_SENTINEL": str(self.sentinel),
            "NEXUS_T_PLAN": json.dumps(plan),
            "NEXUS_T_EFFECT": str(self.effect),
        }
        if handoff_barrier:
            self._env["NEXUS_T_HANDOFF_SENTINEL"] = str(self.handoff_sentinel)
        client = await ensure_daemon(
            self.workspace, socket_path=self.socket, spawn=self._spawn, timeout=TIMEOUT,
        )
        self.clients.append(client)
        proc = self.procs[-1]
        assert proc.poll() is None
        assert client.info is not None and client.info.pid == proc.pid
        self.pid = proc.pid
        return client

    async def kill(self) -> None:
        """SIGKILL the current daemon, reap it, and drop the clients it served."""
        proc = self.procs[-1]
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=TIMEOUT)  # reaped: no zombie for stale-pid reclaim to trip on
        assert proc.returncode == -signal.SIGKILL
        await self.close_clients()

    async def close_clients(self) -> None:
        clients, self.clients = self.clients, []
        for client in clients:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(), 5.0)

    def cleanup(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=TIMEOUT)
        for log in self._logs:
            log.close()

    def daemon_logs(self) -> str:
        return "\n".join(
            f"--- {path.name}\n{path.read_text(errors='replace')[-2000:]}"
            for path in sorted(self.root.glob("daemon-*.log"))
        )


@pytest.fixture
def harness():
    root = Path(tempfile.mkdtemp(prefix="nx-crash-", dir="/tmp"))
    instance = Harness(root)
    try:
        yield instance
    finally:
        instance.cleanup()
        shutil.rmtree(root, ignore_errors=True)


# -- helpers -----------------------------------------------------------------


async def until(predicate, what: str, harness: Harness, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {what}\n{harness.daemon_logs()}")
        await asyncio.sleep(0.02)


async def call(client: UDSClient, command: p.Command):
    result = await client.call(command, timeout=TIMEOUT)
    assert not isinstance(result, p.ErrorResult), f"{command}: {result.kind}: {result.message}"
    return result


async def replay(client: UDSClient, session: str) -> list:
    async def collect():
        return [event async for event in await client.subscribe(session, 0, follow=False)]

    return await asyncio.wait_for(collect(), TIMEOUT)


async def wait_turn(client: UDSClient, session: str, turn_id: str, kind: str = "turn.completed") -> list:
    async def poll():
        while True:
            events = await replay(client, session)
            if any(e.type == kind and e.turn == turn_id and not is_child(e) for e in events):
                return events
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(poll(), TIMEOUT)


def is_child(event) -> bool:
    """True for a relayed native-child event.

    A root ``turn.started`` also carries ``data["agent"]``, but that is the root
    agent definition (``name``/``fingerprint``/``tools``...). Child metadata names
    its own identity (``id``/``session``), which is what marks a relayed event.
    """
    agent = event.data.get("agent")
    return isinstance(agent, dict) and any(key in agent for key in ("id", "session", "parent_session"))


def of(events, kind: str, *, root: bool = True) -> list:
    return [e for e in events if e.type == kind and not (root and is_child(e))]


def flatten(tree: dict) -> list[dict]:
    out: list[dict] = []

    def walk(node):
        out.append(node)
        for child in node["children"]:
            walk(child)

    for node in [*tree["nodes"], *tree["unattached"]]:
        walk(node)
    return out


def assert_none_running(tree: dict) -> None:
    assert [n["id"] for n in flatten(tree) if n["status"] == "running"] == []
    assert all(n["running_children"] == 0 for n in flatten(tree))


async def quiet(harness: Harness) -> None:
    await asyncio.sleep(QUIET)


def last_seqs(tree: dict) -> dict[str, int]:
    """Stored record high-water mark of every node (root and children)."""
    return {n["id"]: n["last_seq"] for n in flatten(tree)}


async def assert_read_only(client: UDSClient, harness: Harness, session: str,
                           baseline: dict[str, int], *extra: p.Command) -> None:
    """Run read-only commands; no node's ``last_seq`` moves and no provider call happens.

    ``SessionHandoffRead`` is issued raw: whether a session without a handoff
    answers with a result or an error is not what is pinned, only that it writes
    nothing. The baseline is taken *after* the daemon's own startup work.
    """
    calls = len(harness.calls())
    await client.call(p.SessionHandoffRead(session=session), timeout=TIMEOUT)
    for command in extra:
        await call(client, command)
    tree = (await call(client, p.SessionTree(session=session))).tree
    assert last_seqs(tree) == baseline
    assert len(harness.calls()) == calls


# -- T1: kill after a committed handoff, before its continuation ran ---------


async def test_t1_kill_after_committed_handoff_keeps_one_head_and_unrun_continuation(harness):
    try:
        client = await harness.start(["text:seed reply"], handoff_barrier=True)
        await call(client, p.SessionOpen(session="demo"))
        seed = await call(client, p.SessionStart(
            session="demo", content="seed question", client_request_id="seed-1"))
        await wait_turn(client, "demo", seed.turn_id)

        request = p.SessionHandoff(
            session="demo", summary="crash brief", continuation="RESUME-CONT", request_id="req-1")

        async def attempt():
            while True:  # the turn lease may outlive turn.completed by a moment
                result = await client.call(request)
                busy = isinstance(result, p.ErrorResult) and "busy" in (result.kind + result.message).lower()
                if not busy:
                    return result
                await asyncio.sleep(0.1)

        task = asyncio.ensure_future(attempt())
        await until(lambda: harness.handoff_sentinel.exists() or task.done(),
                    "handoff after-commit barrier", harness)
        assert harness.handoff_sentinel.exists(), f"handoff did not park: {task.result()}"
        first_pid = harness.pid
        await harness.kill()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        assert len(harness.calls()) == 1  # only the seed turn ever reached the provider

        client = await harness.start(["text:after restart"])
        assert harness.pid not in (first_pid, os.getpid())

        # Read-only surfaces: the committed handoff is visible, nothing is opened.
        tree = await call(client, p.SessionTree(session="demo"))
        demo = next(n for n in flatten(tree.tree) if n["id"] == "demo")
        assert demo["status"] != "running" and demo["handoff_count"] == 1
        assert_none_running(tree.tree)
        brief = (await call(client, p.SessionHandoffRead(session="demo"))).data
        assert brief["head_seq"] > 0 and "crash brief" in brief["text"]

        events = await replay(client, "demo")
        marker = of(events, "context.handoff")
        assert len(marker) == 1
        continuation = [e for e in of(events, "input.queued") if e.seq > marker[0].seq]
        assert len(continuation) == 1  # admitted in the same commit as the head
        queued_id, turn_id = continuation[0].data["queued_id"], continuation[0].data["turn_id"]
        assert queued_id not in [e.data["queued_id"] for e in of(events, "input.consumed")]
        assert of(events, "input.dropped") == []
        assert len(of(events, "turn.started")) == 1  # the seed turn only

        # The lost reply is retried: same identity, no second head, no second input.
        retry = (await call(client, request)).data
        assert retry["fresh"] is False
        assert (retry["queued_id"], retry["turn_id"]) == (queued_id, turn_id)
        again = await replay(client, "demo")
        assert [e.seq for e in of(again, "context.handoff")] == [marker[0].seq]
        assert [e.data["queued_id"] for e in of(again, "input.queued")] == [
            e.data["queued_id"] for e in of(events, "input.queued")]
        await quiet(harness)
        assert harness.calls() == []  # the continuation is not auto-run on restart

        # A fresh request starts a turn that consumes the stranded input once.
        fresh = await call(client, p.SessionStart(
            session="demo", content="FRESH-REQ", client_request_id="fresh-1"))
        done = await wait_turn(client, "demo", fresh.turn_id)
        consumed = [e.data["queued_id"] for e in of(done, "input.consumed")]
        assert consumed.count(queued_id) == 1
        assert len(of(done, "context.handoff")) == 1
        calls = harness.calls()
        assert len(calls) == 1
        text = "\n".join(calls[0])
        assert text.count("RESUME-CONT") == 1 and text.count("FRESH-REQ") == 1
        assert "crash brief" in text and "seed question" not in text  # starts at the head

        replayed = (await call(client, p.SessionStart(
            session="demo", content="FRESH-REQ", client_request_id="fresh-1")))
        assert replayed.turn_id == fresh.turn_id
        assert (await call(client, request)).data["fresh"] is False
        await quiet(harness)
        assert len(harness.calls()) == 1
        final = await replay(client, "demo")
        assert [e.data["queued_id"] for e in of(final, "input.consumed")].count(queued_id) == 1
        assert len(of(final, "context.handoff")) == 1
    finally:
        await harness.close_clients()


# -- T2: kill with a running turn and a queued admission behind it -----------


async def test_t2_kill_during_running_turn_keeps_queued_admission_unrun(harness):
    try:
        client = await harness.start(["barrier"])
        await call(client, p.SessionOpen(session="q"))
        first = await call(client, p.SessionStart(
            session="q", content="FIRST-TURN", client_request_id="start-1"))
        await until(harness.sentinel.exists, "provider barrier", harness)
        enqueue = p.SessionEnqueue(
            session="q", mode="queue", content="STRANDED-Q", client_request_id="enq-1")
        admitted = await call(client, enqueue)  # loop is responsive while the model call is parked
        assert admitted.queued_id and admitted.turn_id
        assert len(harness.calls()) == 1
        first_pid = harness.pid
        await harness.kill()

        client = await harness.start(["text:after restart"])
        assert harness.pid not in (first_pid, os.getpid())

        # SessionTree is the read-only surface (docs/sessions.md): the dead turn is
        # displayed as interrupted, nothing runs and nothing is scheduled. Startup
        # admission recovery only reports; it does not write.
        tree = await call(client, p.SessionTree(session="q"))
        node = next(n for n in flatten(tree.tree) if n["id"] == "q")
        assert node["status"] == "interrupted"
        assert_none_running(tree.tree)
        await assert_read_only(client, harness, "q", last_seqs(tree.tree))
        # SessionSubscribe is *not* read-only: it opens the session with crash
        # recovery, which closes the abandoned turn with one ``turn.failed``
        # (reason ``interrupted``). It never resumes the turn or consumes the queue.
        events = await replay(client, "q")
        assert len(of(events, "turn.started")) == 1
        assert [e.data["queued_id"] for e in of(events, "input.queued")].count(admitted.queued_id) == 1
        assert admitted.queued_id not in [e.data["queued_id"] for e in of(events, "input.consumed")]
        assert [e for e in events if e.type in {"turn.completed", "turn.cancelled"}
                and not is_child(e)] == []
        failed = of(events, "turn.failed")
        assert len(failed) == 1 and failed[0].data["reason"] == "interrupted"
        assert failed[0].turn == first.turn_id
        await quiet(harness)
        assert harness.calls() == []

        # Retries return the original identity and add no record, run nothing.
        retry = await call(client, enqueue)
        assert (retry.queued_id, retry.turn_id) == (admitted.queued_id, admitted.turn_id)
        restart = await call(client, p.SessionStart(
            session="q", content="FIRST-TURN", client_request_id="start-1"))
        # Pinned current behaviour: the historical id is returned, but that
        # interrupted turn is not resumed or replayed.
        assert restart.turn_id == first.turn_id
        await quiet(harness)
        assert harness.calls() == []
        after = await replay(client, "q")
        assert len(of(after, "turn.started")) == 1
        assert len(of(after, "turn.failed")) == 1  # closed once by the first open; idempotent
        assert of(after, "turn.completed") == []
        assert [e.data["queued_id"] for e in of(after, "input.queued")].count(admitted.queued_id) == 1
        assert of(after, "input.dropped") == []
        assert admitted.queued_id not in [e.data["queued_id"] for e in of(after, "input.consumed")]

        # A fresh request consumes the stranded queue exactly once.
        fresh = await call(client, p.SessionStart(
            session="q", content="FRESH-NEW", client_request_id="start-2"))
        done = await wait_turn(client, "q", fresh.turn_id)
        assert [e.data["queued_id"] for e in of(done, "input.consumed")].count(admitted.queued_id) == 1
        calls = harness.calls()
        assert len(calls) == 1
        text = "\n".join(calls[0])
        assert text.count("STRANDED-Q") == 1 and text.count("FRESH-NEW") == 1

        again = await call(client, enqueue)
        assert (again.queued_id, again.turn_id) == (admitted.queued_id, admitted.turn_id)
        await quiet(harness)
        assert len(harness.calls()) == 1
        final = await replay(client, "q")
        assert [e.data["queued_id"] for e in of(final, "input.consumed")].count(admitted.queued_id) == 1
        assert [e.data["queued_id"] for e in of(final, "input.queued")].count(admitted.queued_id) == 1
    finally:
        await harness.close_clients()


# -- T3: kill with a native child parked after its bash side effect ----------


async def test_t3_kill_with_interrupted_child_stream_does_not_replay_completed_bash(harness):
    try:
        client = await harness.start(["subagent", "bash", "barrier"])
        await call(client, p.SessionOpen(session="root"))
        start = await call(client, p.SessionStart(
            session="root", content="delegate", client_request_id="t3-start"))
        await until(harness.sentinel.exists, "child barrier", harness)
        assert harness.effect_lines() == 1 and len(harness.calls()) == 3
        before = await replay(client, "root")
        spawned = of(before, "agent.spawned", root=False)
        assert len(spawned) == 1
        meta = spawned[0].data["agent"]
        first_pid = harness.pid
        await harness.kill()

        client = await harness.start(["text:after restart"])
        assert harness.pid not in (first_pid, os.getpid())

        # Read-only tree/inspect: the dead child is never "running" and its log is
        # inspectable without a provider call. The tree keys nodes by the stored
        # (physical) id; the spawn metadata carries the logical ``root/sub/N`` id,
        # which the node exposes as ``logical_session_id``.
        tree = await call(client, p.SessionTree(session="root"))
        nodes = flatten(tree.tree)
        child = [n for n in nodes if n["logical_session_id"] == meta["session"]]
        assert len(child) == 1, [(n["id"], n["logical_session_id"]) for n in nodes]
        child = child[0]
        assert child["agent_id"] == meta["id"] and child["id"] != meta["session"]
        assert child["owner_id"] == "root" and child["namespace"] == "agents"
        assert_none_running(tree.tree)
        # SessionInspect takes the stored id (docs/host.md); the logical ``root/sub/N``
        # id is not a valid session id and is rejected. The tree's
        # ``logical_session_id`` match above already links the two.
        inspect = p.SessionInspect(session=child["id"], namespace="agents")
        baseline = last_seqs(tree.tree)
        inspected = await call(client, inspect)
        assert isinstance(inspected, p.SessionInspectResult) and not inspected.truncated
        # Schema (docs/host.md, nexus/view/model.py): ``view`` is the pure-reduced
        # ConversationView; completed tool calls carry ``status``/``executed``.
        before_tools = [t for turn in inspected.view["turns"] for t in turn["tools"]]
        bash = [t for t in before_tools if t["name"] == "bash"]
        assert len(bash) == 1 and bash[0]["status"] == "completed"
        assert bash[0]["executed"] is True and not bash[0]["is_error"]
        assert bash[0]["input"]["command"].startswith("echo ran >>")
        assert bash[0]["finished_ts"] is not None
        assert [t["name"] for t in before_tools if t["status"] in ("requested", "running")] == []
        # The child turn's phase is deliberately not asserted: whether the child's
        # own log is closed by the parent's open is not a documented contract.
        await assert_read_only(client, harness, "root", baseline, inspect)
        await quiet(harness)
        assert harness.calls() == [] and harness.effect_lines() == 1

        # Opening the parent (SessionOpen, or SessionSubscribe which opens with
        # recovery) closes the orphaned child once, idempotently.
        await call(client, p.SessionOpen(session="root"))
        opened = await replay(client, "root")
        completed = of(opened, "agent.completed", root=False)
        assert len(completed) == 1
        assert completed[0].data["agent"]["id"] == meta["id"]
        assert completed[0].data["status"] == "interrupted"
        assert len(of(opened, "turn.failed")) == 1
        await call(client, p.SessionOpen(session="root"))
        reopened = await replay(client, "root")
        assert len(of(reopened, "agent.completed", root=False)) == 1
        assert len(of(reopened, "turn.failed")) == 1
        assert_none_running((await call(client, p.SessionTree(session="root"))).tree)
        # The child's transcript after the parent's open: the completed bash call is
        # still the single, completed call (no second call, no running tool).
        closed = await call(client, inspect)
        closed_tools = [t for turn in closed.view["turns"] for t in turn["tools"]]
        assert [(t["name"], t["status"]) for t in closed_tools] == [("bash", "completed")]
        await quiet(harness)
        assert harness.calls() == [] and harness.effect_lines() == 1  # bash not replayed

        # A fresh request runs a clean turn; the side effect still happened once.
        fresh = await call(client, p.SessionStart(
            session="root", content="after restart", client_request_id="t3-fresh"))
        await wait_turn(client, "root", fresh.turn_id)
        assert len(harness.calls()) == 1
        assert harness.effect_lines() == 1
        final = await replay(client, "root")
        assert len(of(final, "agent.completed", root=False)) == 1
        assert len(of(final, "agent.spawned", root=False)) == 1
        retry = await call(client, p.SessionStart(
            session="root", content="delegate", client_request_id="t3-start"))
        assert retry.turn_id == start.turn_id  # historical id, not resumed
        await quiet(harness)
        assert len(harness.calls()) == 1 and harness.effect_lines() == 1
    finally:
        await harness.close_clients()
