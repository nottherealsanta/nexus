"""A large synthetic `nexus.db` built through the real `Store` (plan 50, Phase 0).

Everything is generated text from a seeded random source: nothing is read from
any real database or home. The fixture is written through `Store` methods
(sessions, messages, turns, requests, tool rows, events), so it carries the real
schema, triggers and indexes. Only the spread of session timestamps and the
final checkpoint use plain SQL.

`generate(out, scale)` writes `out` (a nexus.db) plus `<out>.json`, a manifest of
the ids a benchmark needs (projects, the big session, a sample of sessions), and
creates project directories next to it, the first holding a file tree for `@`
file search. Run it with a scratch `$NEXUS10_HOME`: building the session snapshot
template reads the configuration of whatever home is active.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..llmhub.types import Message, Text, ToolCall, ToolResult, Usage
from ..store import Store

WORDS = ["alpha", "bravo", "cache", "config", "daemon", "event", "fixture", "graph", "handler", "index", "journal", "kernel", "latency", "message", "nexus", "output", "parser", "queue", "route", "session", "socket", "stream", "thread", "token", "update", "vector", "worker", "yield", "buffer", "commit", "deploy", "filter", "gather", "header", "import", "select", "stable", "table", "unique"]


@dataclass(frozen=True)
class Scale:
    name: str
    projects: int
    sessions: int  # root sessions across the projects (besides the big one)
    big_turns: int  # turns in the one big session
    turns_max: int  # turns per ordinary session: 1..turns_max
    tool_kb: tuple[int, int]  # tool output size range, KiB, ordinary turns
    big_tool_kb: tuple[int, int]  # tool output size range, KiB, in the big session
    huge_every: int  # every n-th big-session turn has one huge (512 KiB) tool output; 0 = none
    wire_every: int  # a wire request/response event pair every n-th big-session turn (big bodies)
    wire_kb: int
    children_every: int  # every n-th ordinary session gets 1-3 child sessions
    tree_files: int  # files in the first project's directory tree for `@` search


SCALES = {
    "small": Scale("small", projects=3, sessions=40, big_turns=120, turns_max=3, tool_kb=(1, 4), big_tool_kb=(2, 8),
                   huge_every=40, wire_every=4, wire_kb=8, children_every=8, tree_files=1500),
    "large": Scale("large", projects=8, sessions=4000, big_turns=5000, turns_max=6, tool_kb=(2, 24),
                   big_tool_kb=(4, 48), huge_every=250, wire_every=3, wire_kb=40, children_every=10,
                   tree_files=60000),
}


class _Text:
    """Cheap synthetic text: slices of a pre-built random pool."""

    def __init__(self, rng: random.Random, size: int = 1_000_000) -> None:
        self.rng = rng
        self.pool = " ".join(rng.choice(WORDS) + str(rng.randrange(1000)) for _ in range(size // 9))

    def take(self, n: int) -> str:
        start = self.rng.randrange(0, max(1, len(self.pool) - n))
        return self.pool[start:start + n]

    def sentence(self) -> str:
        return self.take(self.rng.randrange(40, 160)).strip().capitalize() + "."


class Writer:
    def __init__(self, store: Store, rng: random.Random, scale: Scale, snapshot: dict[str, Any]) -> None:
        self.store, self.rng, self.scale, self.snapshot = store, rng, scale, snapshot
        self.text = _Text(rng)
        self.seq = 0

    def event(self, session_id: str | None, type_: str, data: dict[str, Any], turn_id: str | None = None) -> None:
        self.seq += 1
        event: dict[str, Any] = {"session_id": session_id, "seq": self.seq, "time": time.time(), "type": type_,
                                 "data": data}
        if turn_id:
            event["_turn_id"] = turn_id
        self.store.append_event(event)

    def turn(self, session_id: str, *, tool_kb: tuple[int, int], huge: bool = False, wire: bool = False) -> None:
        s, rng, text = self.store, self.rng, self.text
        s.append_message(session_id, Message("user", (Text(text.sentence()),)))
        turn = s.start_turn(session_id)
        tid = turn["id"]
        steps = 1 + rng.randrange(3)
        for step in range(1, steps + 1):
            request = s.start_request(tid, session_id, step, model="mock/hello")
            last = step == steps
            if last:
                message = Message("assistant", (Text(text.take(rng.randrange(300, 2500))),))
            else:
                call = ToolCall(f"call_{tid[-8:]}_{step}", rng.choice(("bash", "read", "grep")),
                                json.dumps({"command": text.take(60)}))
                message = Message("assistant", (Text(text.sentence()), call))
            stored = s.save_assistant_message(session_id, message, turn_id=tid, request_id=request["id"])
            s.finish_request(request["id"], stop="end_turn" if last else "tool_calls", response_id=None, parts=[])
            s.record_usage(request["id"], None, Usage(rng.randrange(1000, 50000), rng.randrange(0, 20000),
                                                      rng.randrange(50, 2000), rng.randrange(0, 500)))
            self.event(session_id, "message.added", {"id": stored.id, "seq": stored.seq, "role": "assistant"}, tid)
            if wire and step == 1:
                body = {"model": "mock/hello", "input": text.take(self.scale.wire_kb * 1024)}
                self.event(session_id, "wire_request", body, tid)
                self.event(session_id, "wire_response", {"status": 200, "text": text.take(self.scale.wire_kb * 256)}, tid)
            if not last:
                size = (512 if huge and step == 1 else rng.randrange(*tool_kb)) * 1024
                result = ToolResult(call.id, text.take(size))
                s.mark_tool_started(tid, session_id, call)
                s.mark_tool_finished(tid, session_id, call, result)
                s.save_tool_message(session_id, Message("tool", (result,)), turn_id=tid)
        s.mark_turn_ended(tid, "end_turn", steps)
        s.finish_turn(tid, "completed", steps=steps)
        self.event(session_id, "turn.completed", {"turn_id": tid, "steps": steps}, tid)


def make_tree(root: Path, files: int, *, seed: int = 7) -> int:
    """A directory tree of `files` small files (depth <= 6, repeated words in many names), then `git init`."""
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        return sum(1 for p in root.rglob("*") if p.is_file())
    dirs = [root]
    for i in range(max(10, files // 25)):
        parent = rng.choice(dirs)
        if len(parent.relative_to(root).parts) < 6:
            d = parent / f"{rng.choice(WORDS)}_{i}"
            d.mkdir()
            dirs.append(d)
    exts = (".py", ".rs", ".ts", ".tsx", ".md", ".json", ".toml", ".txt")
    for i in range(files):
        d = rng.choice(dirs)
        (d / f"{rng.choice(WORDS)}_{rng.choice(WORDS)}_{i}{rng.choice(exts)}").write_text(f"// synthetic {i}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=False, capture_output=True)
    return files


def _snapshot_template(tmp: Path) -> dict[str, Any]:
    """A real session snapshot (agent, prompt, tool descriptions) built once by a throwaway Runtime."""
    from ..bus import Bus
    from ..runtime import Runtime
    from . import enable_mock_provider
    enable_mock_provider()
    tmp.mkdir(parents=True, exist_ok=True)
    store = Store(tmp / "template.db")
    try:
        rt = Runtime(store, Bus())
        project = rt.open_project(str(tmp))
        session = rt.create_session(project, title="template", model="mock/hello", permission="allow")
        snapshot = store.snapshot(session.id)
        assert snapshot is not None
        return snapshot
    finally:
        store.db.close()


def generate(out: Path, scale: str | Scale = "small", *, seed: int = 1, progress: Any = None) -> dict[str, Any]:
    """Write the fixture database at `out` and its manifest at `<out>.json`; returns the manifest."""
    sc = SCALES[scale] if isinstance(scale, str) else scale
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError(f"{out} exists; refusing to overwrite")
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    say = progress or (lambda msg: None)
    started = time.time()
    work = out.parent / f".{out.name}.work"
    snapshot = _snapshot_template(work)
    for leftover in work.iterdir():
        leftover.unlink()
    work.rmdir()

    store = Store(out)
    store.db.execute("pragma synchronous = off")  # a generated file: speed over crash safety
    writer = Writer(store, rng, sc, snapshot)
    tree_root = out.parent / f"{out.name}.projects"
    projects = []
    for i in range(sc.projects):
        path = tree_root / f"project-{i}"
        path.mkdir(parents=True, exist_ok=True)
        project, _ = store.project_for_path(str(path), f"project-{i}")
        projects.append(project)
    tree_files = make_tree(tree_root / "project-0", sc.tree_files)
    say(f"tree: {tree_files} files")

    def session(project: Any, title: str, parent: Any = None) -> Any:
        return store.create_session(project, title=title, agent="build", model="mock/hello", snapshot=snapshot,
                                    parent=parent, launched_by="agent" if parent else "user")

    big = session(projects[0], "Big session: " + writer.text.sentence()[:40])
    for n in range(sc.big_turns):
        writer.turn(big.id, tool_kb=sc.big_tool_kb,
                    huge=bool(sc.huge_every) and n % sc.huge_every == sc.huge_every - 1,
                    wire=n % sc.wire_every == 0)
        if n and n % 500 == 0:
            say(f"big session: {n}/{sc.big_turns} turns ({time.time() - started:.0f}s)")
    sessions, children = [], 0
    for n in range(sc.sessions):
        root = session(projects[n % sc.projects], f"{writer.text.sentence()[:50]} #{n}")
        sessions.append(root.id)
        for _ in range(1 + rng.randrange(sc.turns_max)):
            writer.turn(root.id, tool_kb=sc.tool_kb, wire=rng.random() < 0.3)
        if sc.children_every and n % sc.children_every == 0:
            for c in range(1 + rng.randrange(3)):
                child = session(projects[n % sc.projects], f"child {c} of #{n}", parent=root)
                writer.turn(child.id, tool_kb=sc.tool_kb)
                children += 1
        if n and n % 500 == 0:
            say(f"sessions: {n}/{sc.sessions} ({time.time() - started:.0f}s)")
    # Spread the root sessions over ~120 days so listings are not all "just now" (plain SQL, timestamps only).
    now = time.time()
    with store.db:
        rows = store.db.execute("select id from session where parent_id is null order by rowid").fetchall()
        for i, row in enumerate(rows):
            age = (len(rows) - i) / len(rows) * 120 * 86400
            store.db.execute("update session set created = ?, updated = ? where id = ?",
                             (now - age - 3600, now - age, row["id"]))
    store.ensure_payload_indexes(limit=99)
    for _ in range(100_000):
        if not store.backfill_final_answers(limit=2000):
            break
    store.db.execute("pragma wal_checkpoint(truncate)")
    counts = {t: store.db.execute(f"select count(*) from {t}").fetchone()[0]
              for t in ("project", "session", "message", "turn", "turn_request", "turn_tool", "event")}
    store.db.close()
    manifest = {
        "scale": sc.name, "seed": seed, "db": str(out), "bytes": out.stat().st_size,
        "generated_seconds": round(time.time() - started, 1), "counts": counts, "children": children,
        "projects": [{"id": p.id, "path": p.path} for p in projects], "tree_files": tree_files,
        "big_session": big.id, "big_turns": sc.big_turns, "sessions": sessions, "scale_spec": asdict(sc),
    }
    out.with_name(out.name + ".json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="nexus bench fixture")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--scale", choices=sorted(SCALES), default="small")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    manifest = generate(args.out, args.scale, seed=args.seed, progress=lambda m: print(m, file=sys.stderr))
    print(json.dumps({k: v for k, v in manifest.items() if k != "sessions"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
