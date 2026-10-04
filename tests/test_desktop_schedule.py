import asyncio

import pytest

from nexus.ui.desktop.wire_schedule import UpdateCoalescer


@pytest.mark.asyncio
async def test_coalesces_scheduled_updates_and_emits_latest_state():
    state = [0]
    emitted = []

    async def emit():
        emitted.append(state[0])

    coalescer = UpdateCoalescer(emit, delay=0.01)
    coalescer.schedule()
    state[0] = 1
    coalescer.schedule()
    state[0] = 2
    coalescer.schedule()

    await asyncio.sleep(0.02)
    await coalescer.flush()

    assert emitted == [2]
    assert not coalescer.pending


@pytest.mark.asyncio
async def test_flush_emits_pending_update_without_waiting_for_timer():
    emitted = asyncio.Event()
    calls = []

    async def emit():
        calls.append("emitted")
        emitted.set()

    coalescer = UpdateCoalescer(emit, delay=10)
    coalescer.schedule()
    await coalescer.flush()

    assert emitted.is_set()
    assert calls == ["emitted"]
    assert not coalescer.pending


@pytest.mark.asyncio
async def test_updates_scheduled_during_emission_are_not_lost():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def emit():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            entered.set()
            await release.wait()

    coalescer = UpdateCoalescer(emit, delay=0)
    coalescer.schedule()
    await entered.wait()
    coalescer.schedule()
    release.set()
    await coalescer.flush()

    assert calls == [1, 2]
    assert not coalescer.pending
