import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Tests never see the developer's real `~/.agents/skills` (or other files under `$HOME`)."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _no_background_requests(monkeypatch):
    """Titles and automatic handoffs would consume a scripted provider's replies; their own tests turn them on."""
    from nexus.runtime import Runtime

    monkeypatch.setattr(Runtime, "background_requests", False)


@pytest.fixture(autouse=True)
def _no_catalog_download(monkeypatch):
    """Catalog lookups can use a test's cache, but the offline suite never reaches models.dev."""
    from nexus.llmhub import catalog

    async def offline():
        raise OSError("catalog downloads are disabled in offline tests")

    monkeypatch.setattr(catalog, "_download", offline)


def remembering(store, model):
    """A store whose last selected model is `model`, so a new root session starts with it."""
    store.set_preference("model.last", model)
    return store


# --- Event-loop stall watchdog (plan 50, design rule R1) -------------------------------------------------------
#
# Anything that runs on an asyncio loop without yielding for STALL_MS or more fails the test that did it. Every
# unit of loop work is one `Handle._run` (a task step, a callback), so timing that call finds exactly what
# `nexus.perf` finds in the daemon (a late-waking loop), with no sampling gap and for loops in any thread.
#
#   NEXUS_TEST_STALL_MS=250    raise the limit (default `nexus.perf.STALL_MS`, 100); 0 turns the watchdog off
#   NEXUS_TEST_STALL_REPORT=1  never fail; print every stall at the end of the run (to survey a suite)
#   @pytest.mark.blocks_loop("why")   this test blocks the loop on purpose (or the code under test still does,
#                                     tracked in plans/50-rust-core.md); the reason is required
#
# Wall time is measured, so a machine under heavy load can inflate a step; the limit leaves headroom for that.
# Garbage-collection time inside a step is shown but not counted against the limit (see `_timed_run`). A step that
# runs past half the limit has its stack sampled, so a stall names the code that was blocking.
import asyncio.events as _events
import os as _os
import threading as _threading
import time as _time

from nexus.perf import STALL_MS as _DEFAULT_STALL_MS

_STALL_MS = float(_os.environ.get("NEXUS_TEST_STALL_MS") or _DEFAULT_STALL_MS)
_REPORT = bool(_os.environ.get("NEXUS_TEST_STALL_REPORT"))
_stalls: list[tuple[float, float, str]] = []
_all_stalls: list[tuple[str, float, float, str]] = []
_stalls_lock = _threading.Lock()

# A stalled step's sampled stack says what was running (a callback repr like `task_wakeup` alone says nothing);
# only the latest sample is kept per running step.
_running: dict[int, list] = {}  # thread id -> [start, sampled stack or None, gc ms, gc start]


def _gc_timer(phase: str, info: dict) -> None:
    slot = _running.get(_threading.get_ident())
    if slot is None:
        return
    if phase == "start":
        slot[3] = _time.perf_counter()
    elif slot[3] is not None:
        slot[2] += (_time.perf_counter() - slot[3]) * 1000
        slot[3] = None


def _sample_stacks() -> None:
    import sys
    import traceback

    while True:
        _time.sleep(_STALL_MS / 4000)
        now = _time.perf_counter()
        frames = None
        for tid, slot in list(_running.items()):
            if now - slot[0] < _STALL_MS / 2000:
                continue
            frames = frames or sys._current_frames()
            frame = frames.get(tid)
            if frame is not None:
                slot[1] = traceback.extract_stack(frame)


def _format_stack(stack) -> str:
    keep = [f for f in stack if "/asyncio/" not in f.filename and "/_pytest/" not in f.filename
            and "/pluggy/" not in f.filename and "conftest.py" not in f.filename]
    return "".join(f"      {f.filename.rsplit('/site-packages/', 1)[-1]}:{f.lineno} {f.name}\n" for f in keep[-12:])


if _STALL_MS > 0:
    _original_run = _events.Handle._run

    def _timed_run(self):
        tid = _threading.get_ident()
        outer = _running.get(tid)
        slot = [_time.perf_counter(), None, 0.0, None]
        _running[tid] = slot
        wall, cpu = slot[0], _time.thread_time()
        try:
            return _original_run(self)
        finally:
            if outer is None:
                _running.pop(tid, None)
            else:
                _running[tid] = outer
            took = (_time.perf_counter() - wall) * 1000
            # A full collection scans the whole test process's heap (1,700+ tests' worth), so its pause depends on
            # where in the run a step lands, not on the code under test; only the step's own time counts.
            if took - slot[2] >= _STALL_MS:
                what = repr(self._callback)[:240]
                if slot[2] >= 1:
                    what += f" (garbage collection: {slot[2]:.0f} ms)"
                if slot[1] is not None:
                    what += "\n    sampled while blocked:\n" + _format_stack(slot[1]).rstrip()
                with _stalls_lock:
                    _stalls.append((took, (_time.thread_time() - cpu) * 1000, what))

    _events.Handle._run = _timed_run
    import gc as _gc

    _gc.callbacks.append(_gc_timer)
    _threading.Thread(target=_sample_stacks, name="loop-stall-sampler", daemon=True).start()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "blocks_loop(reason): the test (or code it exercises) blocks the event loop for the stall limit; a reason is required")


def pytest_collection_modifyitems(items):
    # Manager tests seed and read the SQLite store synchronously on the loop. With B's store threading merged,
    # 12 of 245 still stall >= 100 ms (seeding and teardown in test_manager_store/stage/redteam/live/context/
    # authority/attention, 11-17); drop this once those tests seed off the loop.
    for item in items:
        if item.path.name.startswith("test_manager_"):
            item.add_marker(pytest.mark.blocks_loop("Manager tests: synchronous SQLite seeding and store work on the loop; workstream B (store threading) owns the fix"))


@pytest.fixture(autouse=True)
def _loop_stall_watchdog(request):
    marker = request.node.get_closest_marker("blocks_loop")
    if marker is not None and not (marker.args or marker.kwargs.get("reason")):
        pytest.fail("blocks_loop needs a reason: @pytest.mark.blocks_loop('why')")
    with _stalls_lock:
        _stalls.clear()
    yield
    with _stalls_lock:
        found = list(_stalls)
        _stalls.clear()
    if not found or _STALL_MS <= 0:
        return
    _all_stalls.extend((request.node.nodeid, *s) for s in found)
    if marker is not None or _REPORT:
        return
    lines = "\n".join(f"  {took:.0f} ms wall, {cpu:.0f} ms cpu: {what}" for took, cpu, what in found)
    pytest.fail(
        f"the event loop was blocked >= {_STALL_MS:.0f} ms (plan 50 R1): move the work off the loop "
        f"(asyncio.to_thread), or mark a deliberate case with @pytest.mark.blocks_loop('reason')\n{lines}",
        pytrace=False)


def pytest_terminal_summary(terminalreporter):
    if _REPORT and _all_stalls:
        terminalreporter.section(f"event-loop stalls >= {_STALL_MS:.0f} ms")
        for node, took, cpu, what in sorted(_all_stalls, key=lambda s: -s[1]):
            terminalreporter.write_line(f"{took:7.0f} ms wall {cpu:7.0f} cpu  {node}  {what}")
