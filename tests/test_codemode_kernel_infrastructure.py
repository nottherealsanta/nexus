"""Phase 1 internal pool/registry contract; no product registration."""
import asyncio
import pickle
import time

import pytest

from nexus.tools.codemode.kernel_host import HostLost, KernelHost, KernelLost
from nexus.tools.codemode.registry import KernelBusy, KernelRegistry


class FakeHost:
    max_kernels = 32
    def __init__(self):
        self.next = 0
        self.live = {}
        self.events = []
        self.fail = None
        self.gate = None
        self.entered = asyncio.Queue()

    async def open(self):
        self.next += 1
        self.live[self.next] = []
        return self.next

    async def feed(self, handle, program, *, functions, **kwargs):
        await self.entered.put(handle)
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise self.fail
        self.live[handle].append(program)
        return list(self.live[handle]), functions['fresh']()

    async def dump(self, handle):
        self.events.append(('dump', handle))
        return pickle.dumps(self.live[handle])

    async def load(self, handle, snapshot):
        self.live[handle] = pickle.loads(snapshot)

    async def close(self, handle):
        self.events.append(('close', handle))
        self.live.pop(handle, None)

    async def shutdown(self):
        self.live.clear()


async def feed(reg, s='s', a='root', p='x', n=1):
    return await reg.feed(s, a, p, functions={'fresh': lambda: n})


async def test_registry_keys_fresh_authority_and_eviction():
    host = FakeHost()
    reg = KernelRegistry(host, max_live=1)
    await reg.start('s', 'root')
    assert await feed(reg) == (['x'], 1)
    await reg.start('s', 'child')
    assert host.events[:2] == [('dump', 1), ('close', 1)]
    assert await feed(reg, a='child') == (['x'], 1)
    assert await feed(reg, n=2) == (['x', 'x'], 2)
    await reg.start('other', 'root')
    assert await feed(reg, s='other') == (['x'], 1)
    await reg.shutdown()
    assert not host.live
    with pytest.raises(KernelLost):
        await reg.start('s', 'root')


async def test_registry_concurrency_and_active_pinning():
    host = FakeHost()
    reg = KernelRegistry(host, max_live=2)
    await reg.start('s', 'a')
    await reg.start('s', 'b')
    host.gate = asyncio.Event()
    first = asyncio.create_task(feed(reg, a='a', p='first'))
    assert await host.entered.get() == 1
    queued = asyncio.create_task(feed(reg, a='a', p='second'))
    other = asyncio.create_task(feed(reg, a='b'))
    assert await host.entered.get() == 2
    with pytest.raises(KernelBusy):
        await reg.start('s', 'c')
    with pytest.raises(KernelBusy):
        await reg.close('s', 'a')
    host.gate.set()
    assert (await first)[0] == ['first']
    assert (await queued)[0] == ['first', 'second']
    await other
    await reg.shutdown()


async def test_registry_idle_caps_and_dump_failure_preserves_live():
    host = FakeHost()
    now = [0]
    reg = KernelRegistry(host, max_live=1, max_entries=2,
                         max_snapshot_bytes=1, idle_seconds=1, clock=lambda: now[0])
    await reg.start('s', 'a')
    now[0] = 2
    with pytest.raises(KernelBusy):
        await reg.evict_idle()
    assert 1 in host.live
    with pytest.raises(KernelBusy):
        await reg.start('s', 'b')
    with pytest.raises(KernelBusy):
        await reg.start('s', 'c')
    await reg.close('s', 'a')
    await reg.close('s', 'b')
    await reg.shutdown()


@pytest.mark.parametrize('failure', [KernelLost('worker lost'), HostLost('helper lost')])
async def test_registry_failure_never_replays(failure):
    host = FakeHost()
    reg = KernelRegistry(host)
    await reg.start('s', 'a')
    await reg.start('s', 'b')
    host.fail = failure
    with pytest.raises(type(failure)):
        await feed(reg, a='a')
    host.fail = None
    with pytest.raises(KernelLost):
        await feed(reg, a='a')
    if isinstance(failure, HostLost):
        with pytest.raises(KernelLost):
            await feed(reg, a='b')
    else:
        assert (await feed(reg, a='b'))[0] == ['x']
    await reg.close('s', 'a')
    await reg.start('s', 'a')
    assert (await feed(reg, a='a'))[0] == ['x']
    await reg.shutdown()


async def test_host_real_persistence_restore_and_fresh_callbacks():
    pytest.importorskip('pydantic_monty')
    host = KernelHost()
    try:
        handle = await host.open()
        assert (await host.feed(handle, 'x=4\nx', functions={}))['value'] == 4
        snap = await host.dump(handle)
        await host.close(handle)
        handle = await host.open()
        await host.load(handle, snap)
        assert (await host.feed(handle, 'await f(x)', functions={'f': lambda n: n+1}))['value'] == 5
        result = await host.feed(handle, 'f(x)', functions={})
        assert result['kind'] == 'failed'
        result = await host.feed(handle, 'await f()', functions={'f': lambda: 1/0})
        assert 'division' not in str(result)
    finally:
        await host.shutdown()


async def test_host_cancellation_isolated_and_generation_safe():
    pytest.importorskip('pydantic_monty')
    host = KernelHost()
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    try:
        one, two = await host.open(), await host.open()
        task = asyncio.create_task(host.feed(one, 'await wait()', functions={'wait': wait}))
        await asyncio.wait_for(entered.wait(), 10)
        assert (await host.feed(two, 'x=7\nx', functions={}))['value'] == 7
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await host.feed(two, 'x', functions={}))['value'] == 7
        await host._break(host._proc)
        with pytest.raises(KernelLost):
            await host.feed(two, 'x', functions={})
    finally:
        await host.shutdown()


async def test_host_deadline_discards_only_worker():
    pytest.importorskip('pydantic_monty')
    host = KernelHost()
    try:
        a, b = await host.open(), await host.open()
        async def slow():
            await asyncio.sleep(10)
        with pytest.raises(TimeoutError):
            await host.feed(a, 'await slow()', functions={'slow': slow},
                            deadline=time.monotonic()+0.1)
        assert (await host.feed(b, '2+2', functions={}))['value'] == 4
    finally:
        await host.shutdown()


async def test_failed_reopen_preserves_snapshot_and_failed_start_removes_entry():
    host = FakeHost()
    reg = KernelRegistry(host, max_live=1)
    await reg.start('s', 'a')
    await feed(reg, a='a', p='saved')
    await reg.start('s', 'b')
    snapshot = reg._entries['s', 'a'].snapshot
    original = host.open

    async def fail():
        raise KernelLost('checkout unavailable')

    host.open = fail
    with pytest.raises(KernelLost):
        await reg.start('s', 'a')
    assert reg._entries['s', 'a'].snapshot == snapshot
    assert not reg._entries['s', 'a'].lost
    with pytest.raises(KernelLost):
        await reg.start('s', 'new')
    assert ('s', 'new') not in reg._entries
    host.open = original
    assert (await feed(reg, a='a'))[0] == ['saved', 'x']
    await reg.shutdown()


async def test_cancelled_feed_unpins_while_global_guard_is_busy_and_closes_handle():
    host = FakeHost()
    reg = KernelRegistry(host)
    await reg.start('s', 'root')
    host.gate = asyncio.Event()
    task = asyncio.create_task(feed(reg))
    await host.entered.get()
    async with reg._guard:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert reg._entries['s', 'root'].pins == 0
        assert not host.live
    await reg.shutdown()


async def test_request_timeout_includes_blocked_write_and_breaks_unresponsive_helper():
    host = KernelHost()
    await host._start()
    try:
        async with host._write_lock:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(host._request('open', timeout=.01), 2)
        assert host._proc is None
        assert not host._pending
    finally:
        await host.shutdown()


async def test_late_reply_is_ignored_and_cancelled_open_does_not_leak():
    host = KernelHost(max_kernels=1)
    try:
        handle = await host.open()
        await host.close(handle)
        # A response for an abandoned ID must not destroy this generation.
        generation = host.generation
        await host._write({'verb': 'close', 'request': 'abandoned', 'kernel': handle.id})
        handle = await host.open()
        assert host.generation == generation
        await host.close(handle)
        task = asyncio.create_task(host.open())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        handle = await host.open()
        assert (await host.feed(handle, '3', functions={}))['value'] == 3
    finally:
        await host.shutdown()


async def test_generation_safe_write_rejects_old_process():
    host = KernelHost()
    try:
        handle = await host.open()
        old = host._proc
        await host._break(old)
        await host.open()
        with pytest.raises(HostLost):
            await host._write({'verb': 'close', 'request': 'old', 'kernel': handle.id},
                              proc=old)
    finally:
        await host.shutdown()


async def test_eviction_reserves_entry_against_new_pins():
    host = FakeHost()
    reg = KernelRegistry(host, max_live=1)
    await reg.start('s', 'a')
    entered = asyncio.Event()
    gate = asyncio.Event()
    original = host.dump

    async def dump(handle):
        entered.set()
        await gate.wait()
        return await original(handle)

    host.dump = dump
    task = asyncio.create_task(reg.start('s', 'b'))
    await entered.wait()
    with pytest.raises(KernelBusy):
        await feed(reg, a='a')
    gate.set()
    await task
    await reg.shutdown()


async def test_repeated_cancel_during_abort_does_not_break_other_worker():
    host = KernelHost()
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = host._abort

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    async def abort(*args):
        cleanup.set()
        await release.wait()
        await original(*args)

    host._abort = abort
    try:
        a, b = await host.open(), await host.open()
        generation = host.generation
        task = asyncio.create_task(host.feed(a, 'await wait()', functions={'wait': wait}))
        await entered.wait()
        task.cancel()
        await cleanup.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.generation == generation
        assert (await host.feed(b, '42', functions={}))['value'] == 42
    finally:
        await host.shutdown()


async def test_shutdown_waits_for_subprocess_creation(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        proc = await original(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    host = KernelHost()
    opening = asyncio.create_task(host.open())
    await entered.wait()
    closing = asyncio.create_task(host.shutdown())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(HostLost):
        await opening
    await closing
    assert host._proc is None
    assert all(proc.returncode is not None for proc in processes)


async def test_stale_generation_failure_does_not_invalidate_new_handles():
    from nexus.tools.codemode.kernel_host import KernelHandle
    host = FakeHost()
    reg = KernelRegistry(host)
    await reg.start('s', 'a')
    await reg.start('s', 'b')
    a, b = reg._entries['s', 'a'], reg._entries['s', 'b']
    a.handle = KernelHandle('old', 'a')
    b.handle = KernelHandle('new', 'b')
    await reg._invalidate(HostLost('delayed failure', 'old'), a)
    assert a.lost and a.handle is None
    assert not b.lost and b.handle.generation == 'new'
    await reg.shutdown()


@pytest.mark.parametrize('cancel', [False, True])
async def test_eviction_after_dump_preserves_snapshot_and_drops_handle(cancel):
    host = FakeHost()
    reg = KernelRegistry(host, max_live=1)
    await reg.start('s', 'a')
    await feed(reg, a='a', p='saved')
    entered, release = asyncio.Event(), asyncio.Event()
    original = host.close

    async def close(handle):
        entered.set()
        await release.wait()
        await original(handle)
        if not cancel:
            raise TimeoutError

    host.close = close
    task = asyncio.create_task(reg.start('s', 'b'))
    await entered.wait()
    if cancel:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    entry = reg._entries['s', 'a']
    assert entry.handle is None and entry.snapshot is not None and not entry.lost
    host.close = original
    assert (await feed(reg, a='a'))[0] == ['saved', 'x']
    await reg.shutdown()


async def test_candidate_failure_does_not_blame_incoming_and_live_fast_path():
    host = FakeHost()
    reg = KernelRegistry(host, max_live=2)
    await reg.start('s', 'a')
    await reg.start('s', 'b')
    entered, release = asyncio.Event(), asyncio.Event()

    async def dump(handle):
        entered.set()
        await release.wait()
        raise KernelLost('candidate failed')

    host.dump = dump
    task = asyncio.create_task(reg.start('s', 'c'))
    await entered.wait()
    assert (await asyncio.wait_for(feed(reg, a='b'), 1))[0] == ['x']
    release.set()
    await task
    assert reg._entries['s', 'a'].lost
    assert not reg._entries['s', 'c'].lost
    await reg.shutdown()


async def test_old_handle_close_does_not_respawn_helper():
    host = KernelHost()
    handle = await host.open()
    await host._break(host._proc)
    with pytest.raises(KernelLost):
        await host.close(handle)
    assert host._proc is None
    await host.shutdown()


@pytest.mark.parametrize('oversized', [False, True])
async def test_callback_response_bounds_do_not_break_helper(oversized):
    host = KernelHost()

    async def response():
        value = 'x' * (5 * 1024 * 1024) if oversized else 1
        if not oversized:
            for _ in range(40):
                value = [value]
        return value

    try:
        handle = await host.open()
        generation = host.generation
        result = await host.feed(handle, 'await response()', functions={'response': response})
        assert result['kind'] == 'failed'
        assert host.generation == generation
        assert (await host.feed(handle, '42', functions={}))['value'] == 42
    finally:
        await host.shutdown()


async def test_nested_output_is_bounded_without_helper_loss():
    host = KernelHost()
    try:
        handle = await host.open()
        result = await host.feed(handle, 'v = 1\nfor i in range(40):\n    v = [v]\nv', functions={})
        assert result['kind'] == 'failed'
        assert (await host.feed(handle, '42', functions={}))['value'] == 42
    finally:
        await host.shutdown()


@pytest.mark.parametrize('restore', [False, True])
async def test_real_kernel_tool_error_alias_and_runtime_lines(restore):
    from nexus.tools.codemode.runner import CodeToolError

    async def fail():
        raise CodeToolError('callback failed')

    host = KernelHost()
    try:
        handle = await host.open()
        if restore:
            snapshot = await host.dump(handle)
            await host.close(handle)
            handle = await host.open()
            await host.load(handle, snapshot)
        result = await host.feed(
            handle,
            'try:\n    await fail()\nexcept ToolError as exc:\n    caught = str(exc)\ncaught',
            functions={'fail': fail},
        )
        assert result['kind'] == 'done'
        assert result['value'] == 'callback failed'
        result = await host.feed(handle, 'x = 1\ny = 0\nx / y', functions={})
        diagnostic = result['diagnostic']
        assert diagnostic['kind'] == 'ZeroDivisionError'
        assert diagnostic['line'] == 3
        assert '  line 3, in <module>\n    x / y' in diagnostic['traceback']
    finally:
        await host.shutdown()


async def test_real_kernel_failed_open_setup_releases_checkout():
    host = KernelHost(max_kernels=1, limits={'max_memory': 1})
    try:
        with pytest.raises(KernelLost):
            await host.open()
        # A failed prelude must not leave the only slot occupied.
        host.limits['max_memory'] = 64 * 1024 * 1024
        handle = await host.open()
        assert (await host.feed(handle, '42', functions={}))['value'] == 42
    finally:
        await host.shutdown()


async def test_cancel_dump_keeps_original_worker_under_repeated_cancel():
    class SlowDump(FakeHost):
        def __init__(self):
            super().__init__()
            self.dumping = asyncio.Event()
            self.release = asyncio.Event()

        async def dump(self, handle):
            self.dumping.set()
            await self.release.wait()
            return await super().dump(handle)

    host = SlowDump()
    registry = KernelRegistry(host, max_live=1)
    await registry.start('a', 'main')
    task = asyncio.create_task(registry.start('b', 'main'))
    await host.dumping.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    host.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    entry = registry._entries[('a', 'main')]
    assert entry.handle == 1 and not entry.lost and not entry.closing
    assert not any(e[0] == 'close' for e in host.events)
    assert await registry.feed('a', 'main', 'still here', functions={'fresh': lambda: 1}) == (['still here'], 1)
    await registry.shutdown()


@pytest.mark.parametrize('failure', [TimeoutError, KernelLost, HostLost, asyncio.CancelledError])
async def test_failed_restore_keeps_checkpoint_for_retry(failure):
    class BrokenLoad(FakeHost):
        async def load(self, handle, snapshot):
            raise failure('restore interrupted')

    host = BrokenLoad()
    registry = KernelRegistry(host, max_live=1)
    await registry.start('a', 'main')
    await registry.start('b', 'main')
    checkpoint = registry._entries[('a', 'main')].snapshot
    with pytest.raises(failure):
        await registry.start('a', 'main')
    entry = registry._entries[('a', 'main')]
    assert entry.snapshot == checkpoint and not entry.lost and entry.handle is None
    assert ('close', 3) in host.events
    host.load = FakeHost.load.__get__(host)
    await registry.start('a', 'main')
    assert registry._entries[('a', 'main')].handle is not None
    await registry.shutdown()


async def test_invalid_restore_is_explicitly_lost():
    from nexus.tools.codemode.kernel_host import SnapshotInvalid

    class BadSnapshot(FakeHost):
        async def load(self, handle, snapshot):
            raise SnapshotInvalid('invalid snapshot')

    host = BadSnapshot()
    registry = KernelRegistry(host, max_live=1)
    await registry.start('a', 'main')
    await registry.start('b', 'main')
    with pytest.raises(SnapshotInvalid):
        await registry.start('a', 'main')
    entry = registry._entries[('a', 'main')]
    assert entry.lost and entry.snapshot is None and entry.handle is None
    assert ('close', 3) in host.events
    await registry.shutdown()


async def test_restore_cleanup_finishes_under_repeated_cancel():
    class SlowRestore(FakeHost):
        def __init__(self):
            super().__init__()
            self.loading = asyncio.Event()
            self.closing = asyncio.Event()
            self.release = asyncio.Event()

        async def load(self, handle, snapshot):
            self.loading.set()
            await asyncio.Event().wait()

        async def close(self, handle):
            if handle == 3:
                self.closing.set()
                await self.release.wait()
            await super().close(handle)

    host = SlowRestore()
    registry = KernelRegistry(host, max_live=1)
    await registry.start('a', 'main')
    await registry.start('b', 'main')
    task = asyncio.create_task(registry.start('a', 'main'))
    await host.loading.wait()
    task.cancel()
    await host.closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    host.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ('close', 3) in host.events
    assert registry._entries[('a', 'main')].snapshot == pickle.dumps([])
    await registry.shutdown()


async def test_close_keeps_entry_until_outcome_and_survives_repeated_cancel():
    class SlowClose(FakeHost):
        def __init__(self):
            super().__init__()
            self.closing = asyncio.Event()
            self.release = asyncio.Event()

        async def close(self, handle):
            self.closing.set()
            await self.release.wait()
            await super().close(handle)

    host = SlowClose()
    registry = KernelRegistry(host)
    await registry.start('a', 'main')
    task = asyncio.create_task(registry.close('a', 'main'))
    await host.closing.wait()
    assert ('a', 'main') in registry._entries
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    host.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ('a', 'main') not in registry._entries and host.events == [('close', 1)]
    await registry.shutdown()


async def test_unknown_close_failure_keeps_entry_retryable():
    class BrokenClose(FakeHost):
        async def close(self, handle):
            raise RuntimeError('no close outcome')

    host = BrokenClose()
    registry = KernelRegistry(host)
    await registry.start('a', 'main')
    with pytest.raises(RuntimeError):
        await registry.close('a', 'main')
    entry = registry._entries[('a', 'main')]
    assert entry.handle == 1 and not entry.closing
    host.close = FakeHost.close.__get__(host)
    await registry.close('a', 'main')
    await registry.shutdown()


def test_registry_capacity_cannot_exceed_host():
    with pytest.raises(ValueError):
        KernelRegistry(KernelHost(max_kernels=1), max_live=2)


async def test_generation_eviction_skips_handles_invalidated_by_host_loss():
    from nexus.tools.codemode.kernel_host import KernelHandle

    class GenerationHost(FakeHost):
        async def open(self):
            self.next += 1
            return KernelHandle('new' if self.next > 2 else 'old', str(self.next))

        async def dump(self, handle):
            assert isinstance(handle, KernelHandle)
            if handle.generation == 'old':
                raise HostLost('helper lost', generation='old')
            return b'ok'

    host = GenerationHost()
    registry = KernelRegistry(host, max_live=2)
    await registry.start('a', 'main')
    await registry.start('b', 'main')
    await registry.start('c', 'main')
    assert registry._entries[('a', 'main')].lost
    assert registry._entries[('b', 'main')].lost
    assert registry._entries[('c', 'main')].handle.generation == 'new'
    assert len([e for e in host.events if e[0] == 'close']) == 2
    await registry.shutdown()


async def test_eviction_rechecks_pins_after_prior_candidate_dump():
    class BlockingDumpHost(FakeHost):
        def __init__(self):
            super().__init__()
            self.dumping = asyncio.Event()
            self.release_dump = asyncio.Event()

        async def dump(self, handle):
            self.events.append(('dump', handle))
            if handle == 1:
                self.dumping.set()
                await self.release_dump.wait()
                raise KernelBusy('candidate cannot be snapshotted')
            return pickle.dumps(self.live[handle])

    host = BlockingDumpHost()
    reg = KernelRegistry(host, max_live=2)
    await reg.start('s', 'a')
    await reg.start('s', 'b')
    admission = asyncio.create_task(reg.start('s', 'c'))
    running = None
    try:
        await asyncio.wait_for(host.dumping.wait(), 1)
        host.gate = asyncio.Event()
        running = asyncio.create_task(feed(reg, a='b', p='still active'))
        assert await asyncio.wait_for(host.entered.get(), 1) == 2
        assert reg._entries['s', 'b'].pins == 1
        host.release_dump.set()
        with pytest.raises(KernelBusy, match='no live kernel can be snapshotted'):
            await asyncio.wait_for(admission, 1)
        assert host.events == [('dump', 1)]
        assert set(host.live) == {1, 2}
        assert not running.done()
        host.gate.set()
        assert await asyncio.wait_for(running, 1) == (['still active'], 1)
    finally:
        host.release_dump.set()
        if host.gate:
            host.gate.set()
        await asyncio.gather(admission, *([running] if running else []),
                             return_exceptions=True)
        await reg.shutdown()
