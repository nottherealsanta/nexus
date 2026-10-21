"""UsageSnapshotService: ingest, coalesced and rate-limited refresh, fail-open fetch."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nexus.host_support.usage_snapshot import UsageSnapshotService
from nexus.model.usage_gate import UsagePolicy

RESULT = {"providers": [{"id": "codex", "error": "", "windows": [
    {"label": "5-hour", "used_percent": 40.0, "resets_at": 2000, "kind": "five_hour"}]}], "fetched_at": 1000.0}


def _runtime(**overrides):
    snapshots = []
    values = dict(set_usage_snapshot=snapshots.append, snapshots=snapshots, _usage_policy=UsagePolicy.from_section(None))
    values.update(overrides)
    return SimpleNamespace(**values)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_ingest_pushes_a_snapshot():
    runtime = _runtime()
    UsageSnapshotService(runtime).ingest(RESULT)
    [snapshot] = runtime.snapshots
    assert snapshot.fetched_at == 1000.0 and snapshot.providers["codex"].windows[0].kind == "five_hour"


def test_ingest_without_a_setter_is_a_no_op():
    UsageSnapshotService(SimpleNamespace()).ingest(RESULT)


def test_kick_without_a_running_loop_is_a_no_op():
    calls = []

    async def fetch(runtime):
        calls.append(1)
        return RESULT

    service = UsageSnapshotService(_runtime(), fetch=fetch)
    service.kick()
    assert calls == [] and service._task is None


async def test_kick_coalesces_and_rate_limits():
    calls = []
    gate = asyncio.Event()

    async def fetch(runtime):
        calls.append(1)
        await gate.wait()
        return RESULT

    clock = _Clock()
    runtime = _runtime()
    service = UsageSnapshotService(runtime, fetch=fetch, clock=clock)
    service.kick()
    service.kick()  # in flight: coalesced
    await asyncio.sleep(0)
    assert calls == [1]
    gate.set()
    await service._task
    assert len(runtime.snapshots) == 1
    clock.now += 10
    service.kick()  # younger than the 180 s refresh interval
    assert service._task.done() and calls == [1]
    clock.now += 200
    service.kick()
    await service._task
    assert calls == [1, 1] and len(runtime.snapshots) == 2


async def test_kick_is_a_no_op_when_disabled():
    calls = []

    async def fetch(runtime):
        calls.append(1)
        return RESULT

    service = UsageSnapshotService(_runtime(_usage_policy=UsagePolicy.from_section({"enabled": False})), fetch=fetch)
    service.kick()
    await asyncio.sleep(0)
    assert calls == [] and service._task is None


async def test_kick_defaults_to_enabled_without_a_policy():
    calls = []

    async def fetch(runtime):
        calls.append(1)
        return RESULT

    service = UsageSnapshotService(SimpleNamespace(), fetch=fetch)
    service.kick()
    await service._task
    assert calls == [1]


async def test_fetch_error_is_swallowed_and_the_old_snapshot_stays():
    runtime = _runtime()
    fail = False

    async def fetch(_runtime):
        if fail:
            raise RuntimeError("boom")
        return RESULT

    service = UsageSnapshotService(runtime, fetch=fetch)
    await service.refresh()
    fail = True
    await service.refresh()
    assert len(runtime.snapshots) == 1


async def test_aclose_cancels_the_in_flight_refresh():
    started = asyncio.Event()

    async def fetch(runtime):
        started.set()
        await asyncio.sleep(60)

    service = UsageSnapshotService(_runtime(), fetch=fetch)
    service.kick()
    await started.wait()
    task = service._task
    await service.aclose()
    assert task.cancelled()
