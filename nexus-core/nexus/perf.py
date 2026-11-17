"""Request timing and event-loop health for Core (docs/api.md#performance).

Every API handler is timed by the server's route wrapper: wall time, the CPU the
loop thread spent while the handler was open, the response size, and the client's
trace id (`x-nexus-trace`). A watcher task measures how late the event loop wakes;
a late wake means something ran on the loop without yielding (a synchronous
SQLite query, JSON encoding, a big Python loop), and the stall records which
requests were in flight. Only timings, sizes and route names are kept, never
bodies, query strings or headers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from typing import Any

log = logging.getLogger("nexus.perf")

#: A request at least this slow is kept in `slow` and logged.
SLOW_MS = float(os.environ.get("NEXUS_PERF_SLOW_MS") or 250)
#: The event-loop watcher's tick, and the lateness that counts as a stall.
LAG_TICK = 0.1
STALL_MS = 100.0
#: Recent durations kept per route for percentiles.
WINDOW = 512


@dataclass
class _Route:
    count: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    bytes: int = 0
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))

    def json(self, name: str) -> dict[str, Any]:
        ordered = sorted(self.recent)

        def pct(q: float) -> float:
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1) if ordered else 0.0

        return {"route": name, "count": self.count, "errors": self.errors,
                "mean_ms": round(self.total_ms / self.count, 1) if self.count else 0.0,
                "p50_ms": pct(0.5), "p95_ms": pct(0.95), "max_ms": round(self.max_ms, 1),
                "mean_bytes": self.bytes // self.count if self.count else 0}


class Perf:
    def __init__(self, *, slow_ms: float = SLOW_MS) -> None:
        self.slow_ms = slow_ms
        self.started = time.time()
        self.routes: dict[str, _Route] = {}
        self.slow: deque[dict[str, Any]] = deque(maxlen=200)
        self.stalls: deque[dict[str, Any]] = deque(maxlen=200)
        self.inflight: dict[int, tuple[str, float, str | None]] = {}
        self._ids = count(1)
        self.lag_max_ms = 0.0
        self.lag_ticks = 0
        self.lag_buckets = {"100ms": 0, "250ms": 0, "1s": 0}
        self._cpu = (time.monotonic(), time.process_time())
        self.cpu_percent = 0.0

    # Requests ------------------------------------------------------------

    def begin(self, name: str, trace: str | None) -> tuple[int, float, float]:
        rid = next(self._ids)
        start = time.perf_counter()
        self.inflight[rid] = (name, start, trace)
        return rid, start, time.thread_time()

    def end(self, token: tuple[int, float, float], *, method: str, status: int, size: int | None) -> dict[str, Any]:
        rid, start, cpu0 = token
        wall = (time.perf_counter() - start) * 1000
        cpu = (time.thread_time() - cpu0) * 1000
        name, _, trace = self.inflight.pop(rid, ("?", start, None))
        key = f"{method} {name}"
        stats = self.routes.setdefault(key, _Route())
        stats.count += 1
        stats.errors += status >= 500
        stats.total_ms += wall
        stats.max_ms = max(stats.max_ms, wall)
        stats.bytes += size or 0
        stats.recent.append(wall)
        entry = {"at": round(time.time(), 3), "route": key, "status": status, "ms": round(wall, 1),
                 "cpu_ms": round(cpu, 1), "bytes": size, "trace": trace}
        if wall >= self.slow_ms:
            self.slow.append(entry)
            log.warning("slow request %s %.0f ms (loop cpu %.0f ms, %s bytes, trace %s)",
                        key, wall, cpu, size, trace)
        return entry

    # Event loop ----------------------------------------------------------

    def tick(self, late_ms: float) -> None:
        self.lag_ticks += 1
        self.lag_max_ms = max(self.lag_max_ms, late_ms)
        for label, limit in (("100ms", 100), ("250ms", 250), ("1s", 1000)):
            if late_ms >= limit:
                self.lag_buckets[label] += 1
        now_m, now_cpu = time.monotonic(), time.process_time()
        then_m, then_cpu = self._cpu
        if now_m - then_m >= 1.0:
            self.cpu_percent = round(100 * (now_cpu - then_cpu) / (now_m - then_m), 1)
            self._cpu = (now_m, now_cpu)
        if late_ms < STALL_MS:
            return
        now = time.perf_counter()
        stall = {"at": round(time.time(), 3), "late_ms": round(late_ms, 1),
                 "inflight": [{"route": n, "open_ms": round((now - s) * 1000, 1), "trace": t}
                              for n, s, t in self.inflight.values()]}
        self.stalls.append(stall)
        log.warning("event loop stalled %.0f ms; in flight: %s", late_ms,
                    ", ".join(r["route"] for r in stall["inflight"]) or "no request (background work)")

    async def watch(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(LAG_TICK)
            self.tick(max(0.0, (loop.time() - before - LAG_TICK) * 1000))

    # Report --------------------------------------------------------------

    def json(self) -> dict[str, Any]:
        routes = sorted((s.json(k) for k, s in self.routes.items()), key=lambda r: -r["mean_ms"] * r["count"])
        return {
            "since": round(self.started, 3), "pid": os.getpid(), "slow_ms": self.slow_ms,
            "cpu_percent": self.cpu_percent,
            "loop": {"ticks": self.lag_ticks, "max_late_ms": round(self.lag_max_ms, 1),
                     "late": dict(self.lag_buckets), "tasks": len(asyncio.all_tasks())
                     if _running() else None},
            "routes": routes, "slow": list(self.slow), "stalls": list(self.stalls),
            "inflight": [{"route": n, "open_ms": round((time.perf_counter() - s) * 1000, 1), "trace": t}
                         for n, s, t in self.inflight.values()],
        }


def _running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


# `nexus perf` ------------------------------------------------------------------

def read_client_log(path: Any, *, limit: int = 2000) -> list[dict[str, Any]]:
    """The newest lines of a client's perf log (`tui-perf.jsonl`), oldest first."""
    import json
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def render(core: dict[str, Any] | None, client: list[dict[str, Any]], *, top: int = 12) -> str:
    """A plain-text report: Core's routes, stalls and slow requests, then the TUI's view."""
    out: list[str] = []
    if core is None:
        out.append("Core: daemon not running")
    else:
        loop = core["loop"]
        out.append(f"Core pid {core['pid']} · cpu {core['cpu_percent']}% · loop late max {loop['max_late_ms']} ms "
                   f"(≥100ms {loop['late']['100ms']}, ≥250ms {loop['late']['250ms']}, ≥1s {loop['late']['1s']}) "
                   f"· tasks {loop['tasks']}")
        out.append("")
        out.append(f"{'route':<34} {'count':>6} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'bytes':>9}")
        for r in core["routes"][:top]:
            out.append(f"{r['route'][:34]:<34} {r['count']:>6} {r['p50_ms']:>8} {r['p95_ms']:>8} "
                       f"{r['max_ms']:>8} {r['mean_bytes']:>9}")
        if core["stalls"]:
            out.append("")
            out.append("Event-loop stalls (newest last):")
            for s in core["stalls"][-top:]:
                names = ", ".join(f"{i['route']} ({i['open_ms']} ms)" for i in s["inflight"]) or "background work"
                out.append(f"  {_clock(s['at'])}  {s['late_ms']:>7} ms late · {names}")
        if core["slow"]:
            out.append("")
            out.append(f"Slow Core requests ≥{core['slow_ms']:.0f} ms (newest last):")
            for s in core["slow"][-top:]:
                out.append(f"  {_clock(s['at'])}  {s['route']:<30} {s['ms']:>8} ms  cpu {s['cpu_ms']:>7} ms  "
                           f"{s['bytes'] or '-':>9} B  {s['trace'] or ''}")
    loads = [e for e in client if e.get("kind") == "history"]
    slow = [e for e in client if e.get("kind") == "request"]
    out.append("")
    if not client:
        out.append("TUI: no timings yet (written to logs/tui-perf.jsonl)")
        return "\n".join(out)
    if loads:
        out.append("TUI session opens (newest last): total = first parallel requests + older turn pages one by one")
        for e in loads[-top:]:
            ph = e.get("phases") or {}
            out.append(f"  {_clock(e['at'])}  {e['ms']:>8} ms  first {ph.get('first_ms', '-')} ms · "
                       f"{ph.get('older_pages', 0)} older pages {ph.get('older_ms', '-')} ms · "
                       f"{e.get('messages')} messages, {e.get('turns')} turns  {e.get('session')}"
                       + (f"  ERROR {e['error']}" if e.get("error") else ""))
    if slow:
        out.append("")
        out.append("TUI slow requests (newest last): outside Core = queued in Core's loop, network, body read")
        for e in slow[-top:]:
            out.append(f"  {_clock(e['at'])}  {e['method']:<6} {e['route'][:40]:<40} {e['ms']:>8} ms  "
                       f"core {e.get('core_ms') if e.get('core_ms') is not None else '-':>8}  "
                       f"outside {e.get('outside_core_ms') if e.get('outside_core_ms') is not None else '-':>8}  "
                       f"{e.get('bytes') or '-':>9} B")
    return "\n".join(out)


def _clock(at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(at))
