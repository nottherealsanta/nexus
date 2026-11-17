"""`nexus bench run`: Core in-process against a fixture database, under load (plan 50, Phase 0).

Starts the real app (`build_app` + lifespan) on a copy of a fixture database in a scratch
`$NEXUS10_HOME` (and scratch `$HOME`), with the offline mock provider, then drives it through an
in-process ASGI client while a mock turn streams:

* a streaming turn loop (`mock/stress` sessions, back to back, plus one SSE-style bus subscriber);
* session opens the way the TUI does them (`load_history` in nexus-tui/src/sync.rs): messages
  page, snapshot, session and turn page in parallel, then older turn pages one by one;
* session and snapshot listings, the big session's `/context`;
* `/api/bash` and `/api/kernel` polls (trivial in-memory routes: the canary for a blocked loop);
* `@` file search on a generated large directory tree.

Latencies are measured by the client, so time spent queued behind a blocked loop counts, which is
what a real client sees. An idle phase (polls only) comes first as the reference. The loop's own
lateness is sampled every 10 ms; `nexus.perf.Perf` (`app.state.perf`) supplies stalls of 100 ms or
more with the requests in flight. Choices are seeded and the intervals fixed, so runs of one fixture
are comparable (the machine, not the script, is the remaining variation).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import enable_mock_provider
from . import fixture as fixture_mod

TOKEN = "bench-token"
QUERIES = ["", "alpha", "bravo_", "daemon.rs", "handler", "config.json", "nexus/", "zzzz", "worker_cache", "py"]
TURN_PAGE = 200  # TUI: nexus-tui/src/sync.rs TURN_PAGE / MESSAGE_PAGE
TURN_PAGES = 10


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


class Recorder:
    def __init__(self) -> None:
        self.ms: dict[str, list[float]] = {}
        self.errors: dict[str, int] = {}
        self.bytes: dict[str, int] = {}

    def add(self, label: str, ms: float, *, error: bool = False, size: int = 0) -> None:
        self.ms.setdefault(label, []).append(ms)
        self.bytes[label] = self.bytes.get(label, 0) + size
        if error:
            self.errors[label] = self.errors.get(label, 0) + 1

    def report(self) -> list[dict[str, Any]]:
        out = []
        for label, values in sorted(self.ms.items()):
            out.append({"route": label, "count": len(values), "errors": self.errors.get(label, 0),
                        "p50_ms": pct(values, 0.5), "p95_ms": pct(values, 0.95), "max_ms": round(max(values), 1),
                        "mean_bytes": self.bytes.get(label, 0) // len(values)})
        return out


class Lag:
    """Event-loop lateness sampled every 10 ms (finer than Perf's 100 ms watcher)."""

    def __init__(self) -> None:
        self.samples: list[float] = []
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(0.01)
            self.samples.append(max(0.0, (loop.time() - before - 0.01) * 1000))

    def start(self) -> None:
        self.samples = []
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> dict[str, Any]:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        s = self.samples
        return {"samples": len(s), "p50_ms": pct(s, 0.5), "p95_ms": pct(s, 0.95), "p99_ms": pct(s, 0.99),
                "max_ms": round(max(s), 1) if s else 0.0,
                "over_50ms": sum(v >= 50 for v in s), "over_100ms": sum(v >= 100 for v in s),
                "over_250ms": sum(v >= 250 for v in s)}


async def run(*, scale: str = "small", fixture: Path | None = None, seconds: float | None = None, seed: int = 1,
              maintenance: bool = True, progress: Any = None) -> dict[str, Any]:
    """Run the benchmark and return the report. Everything happens under a scratch directory."""
    import httpx

    say = progress or (lambda msg: None)
    enable_mock_provider()
    scratch = Path(tempfile.mkdtemp(prefix="nexus-bench-")).resolve()
    saved_env = {k: os.environ.get(k) for k in ("NEXUS10_HOME", "HOME")}
    home = scratch / "home"
    home.mkdir()
    os.environ["NEXUS10_HOME"] = str(home)
    os.environ["HOME"] = str(scratch / "user")  # never the developer's skills or credentials
    (scratch / "user").mkdir()
    try:
        if fixture is None:
            say(f"generating {scale} fixture")
            manifest = fixture_mod.generate(scratch / "fixture.db", scale, seed=seed, progress=say)
            shutil.move(manifest["db"], home / "nexus.db")
        else:
            manifest = json.loads(fixture.with_name(fixture.name + ".json").read_text())
            say(f"copying fixture {fixture} ({fixture.stat().st_size // 2**20} MiB)")
            shutil.copyfile(fixture, home / "nexus.db")
            scale = manifest["scale"]
        sc = fixture_mod.SCALES[manifest["scale"]]
        tree_root = Path(manifest["projects"][0]["path"])
        fixture_mod.make_tree(tree_root, sc.tree_files)  # a no-op unless the directory is gone
        seconds = seconds if seconds is not None else (4.0 if scale == "small" else 20.0)
        return await _drive(manifest, home, seconds=seconds, seed=seed, maintenance=maintenance, say=say, httpx=httpx)
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(scratch, ignore_errors=True)


async def _drive(manifest: dict[str, Any], home: Path, *, seconds: float, seed: int, maintenance: bool,
                 say: Any, httpx: Any) -> dict[str, Any]:
    from ..provider_usage import UsageCache
    from ..runtime import Runtime
    from ..server import build_app, make_runtime
    from ..store import Store

    async def never(self: Any) -> None:  # the usage refresh would call real providers
        await asyncio.Event().wait()

    patches: list[tuple[Any, str, Any]] = [(UsageCache, "run", UsageCache.run)]
    UsageCache.run = never  # type: ignore[method-assign]
    if not maintenance:
        async def noop(self: Any) -> None:
            return None
        patches.append((Runtime, "start_maintenance", Runtime.start_maintenance))
        Runtime.start_maintenance = noop  # type: ignore[method-assign]
    rng = random.Random(seed)
    started = time.perf_counter()
    store = Store(home / "nexus.db")
    runtime = make_runtime(store)
    app = build_app(runtime, token=TOKEN, url="http://bench")
    startup_ms = round((time.perf_counter() - started) * 1000)
    perf = app.state.perf
    recorder = Recorder()
    lag = Lag()
    result: dict[str, Any] = {"scale": manifest["scale"], "seconds": seconds, "seed": seed, "startup_ms": startup_ms,
                              "fixture": {"bytes": manifest["bytes"], "counts": manifest["counts"],
                                          "tree_files": manifest["tree_files"]}}
    big = manifest["big_session"]
    project0 = manifest["projects"][0]
    sessions = list(manifest["sessions"])
    try:
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://bench",
                headers={"authorization": f"Bearer {TOKEN}"}, timeout=120) as client:

            async def call(label: str, path: str, **params: Any) -> Any:
                t = time.perf_counter()
                try:
                    r = await client.get(path, params=params or None)
                except Exception:  # noqa: BLE001
                    recorder.add(label, (time.perf_counter() - t) * 1000, error=True)
                    return None
                recorder.add(label, (time.perf_counter() - t) * 1000, error=r.status_code >= 400, size=len(r.content))
                return r.json() if r.status_code < 400 else None

            async def every(period: float, stop: asyncio.Event, fn: Any) -> None:
                while not stop.is_set():
                    t = time.perf_counter()
                    await fn()
                    left = period - (time.perf_counter() - t)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), max(0.0, left))

            async def poll() -> None:
                await asyncio.gather(call("GET /api/bash", "/api/bash"), call("GET /api/kernel", "/api/kernel"))

            queries = iter(_cycle(QUERIES))

            async def search() -> None:
                await call("GET files/search (@)", f"/api/project/{project0['id']}/files/search", q=next(queries),
                           limit=30)

            async def open_session() -> None:
                sid = big if rng.random() < 0.5 else rng.choice(sessions)
                t = time.perf_counter()
                _, _, _, page = await asyncio.gather(
                    call("GET message?limit=200", f"/api/session/{sid}/message", limit=TURN_PAGE),
                    call("GET session/:id/snapshot", f"/api/session/{sid}/snapshot"),
                    call("GET session/:id", f"/api/session/{sid}"),
                    call("GET turn?limit=200", f"/api/session/{sid}/turn", limit=TURN_PAGE))
                pages = 1
                while page and page.get("more_before") and page["turns"] and pages < TURN_PAGES:
                    page = await call("GET turn?before=&limit=200", f"/api/session/{sid}/turn",
                                      before=page["turns"][0]["id"], limit=TURN_PAGE)
                    pages += 1
                recorder.add("OPEN big session (TUI sequence)" if sid == big else "OPEN session (TUI sequence)",
                             (time.perf_counter() - t) * 1000)

            async def lists() -> None:
                await asyncio.gather(call("GET /api/session (list)", "/api/session"),
                                     call("GET /api/snapshot", "/api/snapshot"),
                                     call("GET /api/project", "/api/project"))

            async def context() -> None:
                await call("GET session/:id/context (big)", f"/api/session/{big}/context")

            async def turns(stop: asyncio.Event) -> None:
                done = 0
                while not stop.is_set():
                    t = time.perf_counter()
                    r = await client.post("/api/session", json={"directory": project0["path"], "model": "mock/stress",
                                                                 "permission": "allow"})
                    if r.status_code >= 400:
                        recorder.add("mock turn (create+stream)", 0, error=True)
                        return
                    sid = r.json()["id"]
                    await client.post(f"/api/session/{sid}/prompt", json={"text": "Stream a long response."})
                    while sid in runtime.turns and not stop.is_set():
                        await asyncio.sleep(0.005)
                    recorder.add("mock turn (create+stream)", (time.perf_counter() - t) * 1000)
                    done += 1

            async def sse(stop: asyncio.Event, counter: dict[str, int]) -> None:
                sub = runtime.bus.open()
                try:
                    while not stop.is_set():
                        try:
                            await asyncio.wait_for(sub.get(), 0.5)
                            counter["events"] += 1
                        except TimeoutError:
                            continue
                finally:
                    sub.close()

            # Phase 1: idle reference (polls only).
            say("phase idle")
            idle_stop = asyncio.Event()
            lag.start()
            idle = asyncio.create_task(every(0.25, idle_stop, poll))
            await asyncio.sleep(min(2.0, seconds / 2))
            idle_stop.set()
            await idle
            result["idle"] = {"routes": recorder.report(), "loop_lag": await lag.stop()}
            recorder.ms.clear(), recorder.errors.clear(), recorder.bytes.clear()

            # Phase 2: load.
            say("phase load")
            stall_mark = len(perf.stalls)
            stop = asyncio.Event()
            events = {"events": 0}
            lag.start()
            tasks = [
                asyncio.create_task(every(0.25, stop, poll)),
                asyncio.create_task(every(0.2, stop, search)),
                asyncio.create_task(every(0.4, stop, open_session)),
                asyncio.create_task(every(1.0, stop, lists)),
                asyncio.create_task(every(2.0, stop, context)),
                asyncio.create_task(turns(stop)),
                asyncio.create_task(sse(stop, events)),
            ]
            await asyncio.sleep(seconds)
            stop.set()
            await asyncio.gather(*tasks)
            report = perf.json()
            result["load"] = {
                "routes": recorder.report(), "loop_lag": await lag.stop(), "sse_events_seen": events["events"],
                "stalls": [s for s in list(perf.stalls)[stall_mark:]],
                "core_routes": report["routes"][:15],
            }
    finally:
        for owner, name, original in patches:
            setattr(owner, name, original)
        with contextlib.suppress(Exception):
            store.db.close()
    return result


def _cycle(items: list[str]) -> Any:
    while True:
        yield from items


def render(result: dict[str, Any]) -> str:
    f = result["fixture"]
    head = (f"nexus bench: scale {result['scale']}, {result['seconds']} s load, db {f['bytes'] / 2**20:.0f} MiB, "
            f"{f['counts']['session']} sessions, {f['counts']['turn']} turns, {f['counts']['event']} events, "
            f"{f['tree_files']} files; startup {result['startup_ms']} ms")
    lines = [head, ""]
    for phase in ("idle", "load"):
        data = result[phase]
        lines.append(f"[{phase}] client-side latency (includes time queued behind a blocked loop)")
        lines.append(f"{'route':<38} {'n':>5} {'err':>4} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'bytes':>9}")
        for r in data["routes"]:
            lines.append(f"{r['route']:<38} {r['count']:>5} {r['errors']:>4} {r['p50_ms']:>8} {r['p95_ms']:>8} "
                         f"{r['max_ms']:>8} {r['mean_bytes']:>9}")
        g = data["loop_lag"]
        lines.append(f"loop lateness (10 ms probe, {g['samples']} samples): p50 {g['p50_ms']} p95 {g['p95_ms']} "
                     f"p99 {g['p99_ms']} max {g['max_ms']} ms; >=50ms {g['over_50ms']}, >=100ms {g['over_100ms']}, "
                     f">=250ms {g['over_250ms']}")
        lines.append("")
    load = result["load"]
    lines.append(f"Perf stalls >=100 ms during load: {len(load['stalls'])}; bus events seen by one subscriber: "
                 f"{load['sse_events_seen']}")
    for s in load["stalls"][:15]:
        names = ", ".join(f"{i['route']} ({i['open_ms']} ms)" for i in s["inflight"]) or "background work"
        lines.append(f"  {s['late_ms']:>8} ms late: {names}")
    lines.append("")
    lines.append("Core-side handler time (Perf, last 512 per route; excludes time queued before the handler ran)")
    lines.append(f"{'route':<34} {'n':>5} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'bytes':>9}")
    for r in load["core_routes"]:
        lines.append(f"{r['route'][:34]:<34} {r['count']:>5} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['max_ms']:>8} "
                     f"{r['mean_bytes']:>9}")
    return "\n".join(lines)


def main(args: Any) -> int:
    """CLI entry: `nexus bench fixture|run` (see `nexus/cli.py`)."""
    log = (lambda m: print(m, file=sys.stderr)) if not args.quiet else None
    if args.action == "fixture":
        if args.out is None:
            print("bench fixture needs --out PATH", file=sys.stderr)
            return 2
        enable_mock_provider()
        manifest = fixture_mod.generate(args.out, args.scale, seed=args.seed, progress=log)
        print(json.dumps({k: v for k, v in manifest.items() if k != "sessions"}, indent=2))
        return 0
    result = asyncio.run(run(scale=args.scale, fixture=args.fixture, seconds=args.seconds, seed=args.seed,
                             maintenance=not args.no_maintenance, progress=log))
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0
