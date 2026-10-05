"""Bounded body-free bridge timings (TUI_LOCAL_INTERACTION_PLAN §0)."""
from __future__ import annotations
from collections import defaultdict, deque
import os
from pathlib import Path
import time


class BridgeTrace:
    def __init__(self):
        self.enabled = os.environ.get("NEXUS_TUI_TRACE") == "1"
        self.samples = deque(maxlen=16000)
        self.published = 0.0

    def record(self, name, value):
        if self.enabled:
            self.samples.append((name, value))

    def elapsed(self, name, started):
        self.record(name, (time.perf_counter() - started) * 1000)

    def summary(self):
        groups = defaultdict(list)
        for name, value in self.samples:
            groups[name].append(value)
        lines = []
        for name, values in sorted(groups.items()):
            values.sort()
            unit = "bytes" if name == "snapshot_bytes" else "ms"
            p = lambda q: values[min(len(values)-1, int((len(values)-1)*q + .999))]
            lines.append(f"Python {name}: p50 {p(.5):.3f} · p95 {p(.95):.3f} · max {values[-1]:.3f} {unit} (n={len(values)})")
        return lines

    def publish(self, logs):
        now = time.monotonic()
        if self.enabled and now - self.published >= 2:
            logs.python_trace = self.summary()
            self.published = now

    def finish(self):
        if self.enabled:
            path = Path(os.environ.get("NEXUS_TUI_TRACE_FILE") or f"/tmp/nexus-tui-trace-{os.getpid()}.log")
            path = path.with_name(path.name + ".python")
            try:
                path.write_text("\n".join(self.summary()))
            except OSError:
                pass
