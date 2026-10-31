"""Opt-in synthetic retention throughput benchmark.

Run explicitly with `NEXUS10_RETENTION_BENCH=1 uv run pytest -q -s
tests/test_retention_bench.py`. It uses a scratch database under pytest's
tmp_path, never a real NEXUS10_HOME, and is skipped in the normal suite.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from nexus.bus import Bus
from nexus.retention import Retention, Settings
from nexus.store import Store

pytestmark = pytest.mark.skipif(not os.environ.get("NEXUS10_RETENTION_BENCH"),
                                reason="set NEXUS10_RETENTION_BENCH=1 to run the retention benchmark")


def test_sustained_event_pruning_throughput(tmp_path):
    events = int(os.environ.get("NEXUS10_RETENTION_BENCH_EVENTS", "100000"))
    store = Store(tmp_path / "nexus.db")
    bus = Bus()
    bus.attach(store)

    class Runtime:
        def __init__(self):
            self.turns: dict = {}
            self.questions: dict = {}

    runtime = Runtime()
    runtime.store, runtime.bus = store, bus
    now = 100_000.0
    with store.db:
        store.db.executemany(
            "insert into event (session_id, seq, time, type, data, payload_bytes, stored_bytes)"
            " values (NULL, ?, ?, 'probe', ?, 12, 12)",
            [(i, now - 1000, json.dumps({"i": i})) for i in range(events)])
        store.db.execute("update log set head_high = (select max(id) from event)")
    retention = Retention(runtime, Settings(
        event_age_seconds=1, event_bytes=10**12, turn_age_seconds=10**9,
        mutation_lifetime=10**9, batch_size=2000, event_batches=8, interval_seconds=10**9))

    started = time.monotonic()
    removed, passes = 0, 0
    while passes < 1000:
        summary = retention.run_once()
        removed += summary["events"]["removed"]
        passes += 1
        if not summary["worked"]:
            break
    elapsed = time.monotonic() - started
    assert removed >= events
    print(f"\nretention benchmark: {removed:,} events, {passes} passes, "
          f"{elapsed:.2f}s, {removed / max(elapsed, 1e-9):,.0f} events/s")
    store.close()
