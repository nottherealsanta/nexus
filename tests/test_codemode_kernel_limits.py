"""Persistent helper resource boundaries; uses no registered product tools."""
import pytest

from nexus.tools.codemode.kernel_host import KernelHost, KernelLost, MAX_SNAPSHOT_BYTES


async def test_limits_and_caps():
    pytest.importorskip('pydantic_monty')
    host = KernelHost(max_kernels=1)
    try:
        handle = await host.open()
        with pytest.raises(KernelLost):
            await host.open()
        with pytest.raises(ValueError):
            await host.feed(handle, 'x' * 16385, functions={})
        with pytest.raises(ValueError):
            await host.load(handle, b'x' * (MAX_SNAPSHOT_BYTES + 1))
        result = await host.feed(handle, 'print("x" * 2000000)', functions={})
        assert result['kind'] == 'failed'
        calls = []
        result = await host.feed(handle, 'for i in range(105):\n    await f()',
                                 functions={'f': lambda: calls.append(1)})
        assert result['kind'] == 'failed'
        assert 0 < len(calls) <= 100
        result = await host.feed(handle, 'while True:\n    pass', functions={})
        assert result['kind'] == 'failed'
    finally:
        await host.shutdown()


def test_limits_cannot_be_relaxed():
    with pytest.raises(ValueError):
        KernelHost(limits={'unknown': 10})
    with pytest.raises(ValueError):
        KernelHost(max_kernels=33)


async def test_unicode_output_and_callback_frame_overflow_keep_helper_alive():
    pytest.importorskip('pydantic_monty')
    host = KernelHost()
    try:
        handle = await host.open()
        result = await host.feed(handle, 'print("\\u0800" * 400000)', functions={})
        assert result['kind'] == 'failed'
        assert len(result['output']) < 400000
        generation = host.generation
        result = await host.feed(handle, 'await f()',
                                 functions={'f': lambda: '\u0800' * 400000})
        assert result['kind'] == 'failed'
        assert (await host.feed(handle, '42', functions={}))['value'] == 42
        assert host.generation == generation
    finally:
        await host.shutdown()


async def test_dependency_worker_isolation_after_error_and_cancel():
    import asyncio

    pytest.importorskip('pydantic_monty')
    host = KernelHost(max_kernels=1)
    try:
        handle = await host.open()
        result = await host.feed(handle, 'secret = 891\n1 / 0', functions={})
        assert result['kind'] == 'failed'
        await host.close(handle)
        handle = await host.open()
        result = await host.feed(handle, 'secret', functions={})
        assert result['kind'] == 'failed'
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(host.feed(
            handle, 'secret = 892\nawait block()', functions={'block': block}))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        handle = await host.open()
        result = await host.feed(handle, 'secret', functions={})
        assert result['kind'] == 'failed'
        assert (await host.feed(handle, '7', functions={}))['value'] == 7
    finally:
        await host.shutdown()


async def test_nonexistent_kernel_feed_is_kernel_lost():
    from nexus.tools.codemode.kernel_host import KernelHandle

    host = KernelHost()
    live = await host.open()
    try:
        with pytest.raises(KernelLost, match='kernel not found'):
            await host.feed(KernelHandle(live.generation, 'nonexistent'), '1', functions={})
        assert (await host.feed(live, '1', functions={}))['value'] == 1
    finally:
        await host.shutdown()


async def test_cpu_timeout_isolates_sibling_worker():
    host = KernelHost(max_kernels=2)
    slow = await host.open()
    sibling = await host.open()
    try:
        await host.feed(sibling, 'answer = 42', functions={})
        import time
        # Use an actual CPU loop, not an async fake or external-call pause.
        with pytest.raises(TimeoutError):
            await host.feed(slow, 'while True:\n    pass', functions={}, deadline=time.monotonic() + 0.1)
        assert (await host.feed(sibling, 'answer', functions={}))['value'] == 42
    finally:
        await host.shutdown()


async def test_helper_oversized_snapshot_is_capacity_and_worker_remains_live():
    from nexus.tools.codemode.kernel_host import KernelBusy

    host = KernelHost()
    handle = await host.open()
    try:
        await host.feed(handle, 'payload = "x" * 1100000', functions={})
        with pytest.raises(KernelBusy):
            await host.dump(handle)
        assert (await host.feed(handle, 'len(payload)', functions={}))['value'] == 1100000
    finally:
        await host.shutdown()


async def test_host_snapshot_check_is_capacity_not_loss(monkeypatch):
    import base64
    from nexus.tools.codemode import kernel_host

    host = KernelHost()
    handle = await host.open()
    request = host._request
    try:
        async def oversized(verb, **payload):
            if verb == 'dump':
                return base64.b64encode(b'xx').decode()
            return await request(verb, **payload)

        monkeypatch.setattr(host, '_request', oversized)
        monkeypatch.setattr(kernel_host, 'MAX_SNAPSHOT_BYTES', 1)
        with pytest.raises(kernel_host.KernelBusy):
            await host.dump(handle)
        assert (await host.feed(handle, '42', functions={}))['value'] == 42
    finally:
        await host.shutdown()


async def test_real_invalid_snapshot_classification():
    from nexus.tools.codemode.kernel_host import SnapshotInvalid

    host = KernelHost()
    handle = await host.open()
    try:
        with pytest.raises(SnapshotInvalid):
            await host.load(handle, b'not a snapshot')
    finally:
        await host.shutdown()
