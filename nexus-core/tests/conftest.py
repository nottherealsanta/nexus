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

if _STALL_MS > 0:
    _original_run = _events.Handle._run

    def _timed_run(self):
        wall, cpu = _time.perf_counter(), _time.thread_time()
        try:
            return _original_run(self)
        finally:
            took = (_time.perf_counter() - wall) * 1000
            if took >= _STALL_MS:
                what = repr(self._callback)[:240]
                with _stalls_lock:
                    _stalls.append((took, (_time.thread_time() - cpu) * 1000, what))

    _events.Handle._run = _timed_run


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "blocks_loop(reason): the test (or code it exercises) blocks the event loop for the stall limit; a reason is required")


def pytest_collection_modifyitems(items):
    # Manager tests seed and read the SQLite store synchronously on the loop (~100-200 ms steps in nearly every
    # test, including teardown); that is workstream B's store threading. Drop this when B lands.
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
