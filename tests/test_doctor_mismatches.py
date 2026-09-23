"""`doctor` aggregation of durable `registry.mismatch` events (PLAN §15.5/§15.12).

Closes §16.5 item 3. Two layers are covered:

* :mod:`nexus.host.doctor` directly -- bounded discovery/reads, dedupe, open
  sessions, corrupt tails, inaccessible/missing directories, and the
  "no secrets, no raw error text" rule against hostile logs.
* the facade and CLI surfaces -- the report shape, the human rendering, and the
  JSON output a caller actually sees.
"""
from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import pytest

from nexus import cli
from nexus.events import Event
from nexus.host.doctor import MISMATCH_EVENT, ScanLimits, mismatch_summary
from nexus.host.facade import HostFacade
from nexus.session.store import SessionStore


def _write_mismatch(
    store: SessionStore,
    session_id: str,
    *,
    provider: str = "anthropic",
    model: str = "claude-opus-5",
    feature: str = "tools",
    source: str = "provider-rejection",
    detail: str = "Bearer sk-deadbeefdeadbeef should never surface",
    ts: float = 1.5,
) -> None:
    store.append_event(
        session_id,
        Event(
            type=MISMATCH_EVENT,
            data={
                "provider": provider,
                "model": model,
                "feature": feature,
                "source": source,
                "detail": detail,
            },
            session=session_id,
            ts=ts,
        ),
    )


def _store(tmp_path: Path) -> SessionStore:
    directory = tmp_path / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return SessionStore(directory)


# ---------------------------------------------------------------------------
# Aggregation core
# ---------------------------------------------------------------------------


def test_mismatch_summary_counts_and_groupings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_mismatch(store, "alpha", provider="anthropic", model="m1")
    _write_mismatch(store, "alpha", provider="openai", model="m2", source="provider-rejection")
    _write_mismatch(store, "beta", provider="anthropic", model="m1", feature="context")

    summary = mismatch_summary(store.directory)
    assert summary["count"] == 3
    assert summary["sessions_scanned"] == 2
    assert summary["sessions_with_mismatches"] == 2
    assert summary["by_provider"] == {"anthropic": 2, "openai": 1}
    assert summary["by_model"] == {"m1": 2, "m2": 1}
    assert summary["by_reason"] == {"provider-rejection": 3}
    assert summary["by_session"] == {"alpha": 2, "beta": 1}
    assert summary["truncated"] is False
    assert len(summary["samples"]) == 3
    assert summary["samples"][0]["session"] == "alpha"
    assert summary["samples"][0]["ts"] == 1.5
    assert "seq" in summary["samples"][0]


def test_mismatch_summary_empty_or_absent_directory(tmp_path: Path) -> None:
    absent = tmp_path / "nope"
    summary = mismatch_summary(absent)
    assert summary["count"] == 0 and summary["sessions_scanned"] == 0
    assert summary["by_provider"] == {} and summary["samples"] == []
    assert summary["truncated"] is False


def test_mismatch_summary_accepts_opaque_and_missing_path() -> None:
    assert mismatch_summary(None)["count"] == 0
    assert mismatch_summary(object())["count"] == 0


def test_mismatch_summary_ignores_non_mismatch_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_mismatch(store, "keep")
    for event_type in ("registry.refreshed", "turn.completed", "context.degraded"):
        store.append_event("keep", Event(type=event_type, data={}, session="keep"))
    store.append_message("keep", _message("hello"))

    summary = mismatch_summary(store.directory)
    assert summary["count"] == 1
    assert summary["by_provider"] == {"anthropic": 1}


def test_mismatch_summary_dedupes_duplicate_events_across_reopen(tmp_path: Path) -> None:
    """A re-read log must not double count; the same event id is folded once."""
    store = _store(tmp_path)
    _write_mismatch(store, "dup")
    first = mismatch_summary(store.directory)
    second = mismatch_summary(store.directory)
    assert first["count"] == second["count"] == 1
    assert first["by_provider"] == second["by_provider"] == {"anthropic": 1}

    # A genuinely duplicated line (same id) is also folded once.
    path = store.log_path("dup")
    line = path.read_bytes()
    path.write_bytes(line + line)
    assert mismatch_summary(store.directory)["count"] == 1


def test_mismatch_summary_tolerates_open_session_without_handle(tmp_path: Path) -> None:
    """The scan reads bytes only; it never opens/creates/rehydrates a handle."""
    store = _store(tmp_path)
    _write_mismatch(store, "live")
    before = store.log_path("live").read_bytes()

    summary = mismatch_summary(store.directory)
    assert summary["count"] == 1
    assert store.log_path("live").read_bytes() == before


def test_mismatch_summary_tolerates_corrupt_tail_and_bad_lines(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_mismatch(store, "corrupt", provider="p1")
    path = store.log_path("corrupt")
    # An interior malformed line plus an unterminated crash tail.
    path.write_bytes(
        path.read_bytes()
        + b"not-json-at-all\n"
        + b'{"type":"event","event":{"type":"registry.mismatch"'  # truncated tail
    )

    summary = mismatch_summary(store.directory)
    assert summary["count"] == 1
    assert summary["by_provider"] == {"p1": 1}


def test_mismatch_summary_skips_interior_corrupt_log_without_crashing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_mismatch(store, "good")
    bad = store.log_path("bad")
    bad.write_bytes(b"garbage\n")

    summary = mismatch_summary(store.directory)
    assert summary["count"] == 1
    assert summary["sessions_scanned"] == 2


def test_mismatch_summary_skips_symlinked_log(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_mismatch(store, "real")
    target = tmp_path / "outside.jsonl"
    other = SessionStore(tmp_path / "other")
    _write_mismatch(other, "x")
    target.write_bytes(other.log_path("x").read_bytes())
    try:
        (store.directory / "link.jsonl").symlink_to(target)
    except OSError:
        return  # platform without symlinks
    summary = mismatch_summary(store.directory)
    assert summary["count"] == 1
    assert summary["sessions_skipped"] == 1


def test_mismatch_summary_does_not_block_on_named_fifo(tmp_path: Path) -> None:
    """A FIFO named like a log is refused without opening for a blocking read."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform has no mkfifo")
    store = _store(tmp_path)
    fifo = store.directory / "fifo.jsonl"
    os.mkfifo(fifo)

    result: dict[str, object] = {}
    thread = threading.Thread(target=lambda: result.update(mismatch_summary(store.directory)))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "mismatch_summary blocked on a named FIFO"
    assert result["count"] == 0
    assert result["sessions_scanned"] == 0
    assert result["sessions_skipped"] == 1


def test_mismatch_summary_bounds_a_concurrently_growing_log(tmp_path: Path) -> None:
    """A log appended to during the scan is still read with a hard byte cap."""
    store = _store(tmp_path)
    _write_mismatch(store, "growing")
    path = store.log_path("growing")
    stop = threading.Event()

    def grow() -> None:
        line = (
            b'{"type":"event","event":{"type":"registry.mismatch",'
            b'"data":{"provider":"p"}}}\n'
        )
        while not stop.is_set():
            with open(path, "ab") as handle:
                handle.write(line)

    thread = threading.Thread(target=grow, daemon=True)
    thread.start()
    try:
        for _ in range(25):
            summary = mismatch_summary(
                store.directory, limits=ScanLimits(max_bytes_per_log=2048)
            )
            assert summary["sessions_scanned"] == 1
            # 2048 bytes of ~60-byte lines is at most a few dozen records; the
            # read can never run away with the growing file.
            assert 0 < summary["count"] < 200
    finally:
        stop.set()
        thread.join(timeout=5)


def test_mismatch_summary_never_surfaces_secret_or_raw_detail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_mismatch(
        store,
        "hostile",
        provider="pk_liveABCDEFGH12345678",  # secret-shaped provider
        model="sk-proj-abcdefghijklmnop",
        detail="Authorization: Bearer supersecrettoken12345",
    )
    blob = json.dumps(mismatch_summary(store.directory))
    assert "supersecrettoken" not in blob
    assert "sk-proj-abcdefghijklmnop" not in blob
    assert "pk_liveABCDEFGH" not in blob
    assert "<redacted>" in blob or "***" in blob


def test_mismatch_summary_redacts_secret_in_session_and_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_event(
        "hostile",
        Event(
            type=MISMATCH_EVENT,
            data={
                "provider": "p",
                "model": "m",
                "source": "Bearer sk-secrettoken1234567",
                "detail": "never surfaced",
            },
            session="hostile",
        ),
    )
    summary = mismatch_summary(store.directory)
    rendered = json.dumps(summary)
    assert "sk-secrettoken" not in rendered
    assert "never surfaced" not in rendered


def test_mismatch_summary_handles_absent_or_hostile_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Missing data entirely.
    store.append_event("s", Event(type=MISMATCH_EVENT, data={}, session="s"))
    # Non-string, nested, and boolean values.
    store.append_event(
        "s",
        Event(
            type=MISMATCH_EVENT,
            data={"provider": {"x": 1}, "model": True, "source": ["a", "b"]},
            session="s",
        ),
    )
    summary = mismatch_summary(store.directory)
    assert summary["count"] == 2
    assert summary["by_provider"]  # grouped without raising
    assert summary["by_reason"]  # "unknown" fallback present


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_mismatch_summary_bounds_sessions_and_marks_truncated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        _write_mismatch(store, f"s{index}")
    summary = mismatch_summary(store.directory, limits=ScanLimits(max_sessions=2))
    assert summary["sessions_scanned"] == 2
    assert summary["truncated"] is True
    assert summary["count"] == 2


def test_mismatch_summary_enumeration_is_deterministic(tmp_path: Path) -> None:
    """The lowest ids are always the ones kept, independent of creation order."""
    store = _store(tmp_path)
    for index in (5, 2, 4, 0, 3, 1):  # deliberately not sorted
        _write_mismatch(store, f"s{index}", provider=f"p{index}")
    summary = mismatch_summary(store.directory, limits=ScanLimits(max_sessions=3))
    assert summary["truncated"] is True
    assert set(summary["by_session"]) == {"s0", "s1", "s2"}
    assert summary["count"] == 3


@pytest.mark.parametrize(
    "field",
    [
        "max_sessions",
        "max_bytes_per_log",
        "max_lines_per_log",
        "max_groups",
        "max_samples",
    ],
)
def test_scan_limits_reject_zero_and_negative(field: str) -> None:
    with pytest.raises(ValueError):
        ScanLimits(**{field: 0})
    with pytest.raises(ValueError):
        ScanLimits(**{field: -1})


def test_scan_limits_reject_non_integer_and_bool() -> None:
    with pytest.raises(ValueError):
        ScanLimits(max_sessions=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ScanLimits(max_sessions=True)


def test_mismatch_summary_bounds_tail_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(40):
        _write_mismatch(store, "big", provider=f"p{index}")
    summary = mismatch_summary(
        store.directory, limits=ScanLimits(max_bytes_per_log=800)
    )
    assert summary["truncated"] is True
    assert 0 < summary["count"] < 40


def test_mismatch_summary_bounds_lines_per_log(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(10):
        _write_mismatch(store, "lines")
    summary = mismatch_summary(
        store.directory, limits=ScanLimits(max_lines_per_log=3)
    )
    assert summary["count"] == 3
    assert summary["truncated"] is True


def test_mismatch_summary_bounds_groups_and_samples(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(30):
        _write_mismatch(store, "g", provider=f"provider-{index}")
    summary = mismatch_summary(
        store.directory, limits=ScanLimits(max_groups=4, max_samples=3)
    )
    assert summary["truncated"] is True
    assert len(summary["by_provider"]) == 5  # four kept + "(other)"
    assert summary["by_provider"]["(other)"] == 26
    assert len(summary["samples"]) == 3
    # Samples retain the most recent events.
    assert summary["samples"][-1]["provider"] == "provider-29"


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


def test_facade_doctor_reports_registry_mismatches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_mismatch(store, "alpha")
    _write_mismatch(store, "alpha", provider="openai")
    runtime = _FakeRuntime(store.directory)
    facade = HostFacade(runtime)

    report = facade.doctor()
    mismatches = report["registry_mismatches"]
    assert mismatches["count"] == 2
    assert mismatches["by_provider"] == {"anthropic": 1, "openai": 1}
    assert mismatches["sessions_scanned"] == 1


def test_facade_doctor_mismatches_zero_without_sessions_dir() -> None:
    runtime = _FakeRuntime(None)
    facade = HostFacade(runtime)
    report = facade.doctor()
    assert report["registry_mismatches"]["count"] == 0


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def test_print_doctor_human_reports_mismatches() -> None:
    out = io.StringIO()
    cli._print_doctor(
        {
            "workspace": "/tmp/ws",
            "providers": [],
            "registry": {"source": "cache", "models": 1, "stale": False},
            "registry_mismatches": {
                "count": 2,
                "sessions_scanned": 3,
                "sessions_with_mismatches": 2,
                "truncated": True,
                "by_provider": {"anthropic": 2},
                "by_model": {"m1": 2},
                "by_reason": {"provider-rejection": 2},
                "samples": [
                    {
                        "session": "alpha",
                        "provider": "anthropic",
                        "model": "m1",
                        "reason": "provider-rejection",
                        "feature": "tools",
                        "ts": 1.5,
                    }
                ],
            },
        },
        out,
    )
    text = out.getvalue()
    assert "registry mismatches: 2 across 2/3 sessions (scan truncated)" in text
    assert "by provider: anthropic=2" in text
    assert "session=alpha" in text and "feature=tools" in text


def test_print_doctor_human_reports_none_when_clean() -> None:
    out = io.StringIO()
    cli._print_doctor(
        {"workspace": "/tmp/ws", "registry_mismatches": {"count": 0}}, out
    )
    assert "registry mismatches: none" in out.getvalue()


def test_print_doctor_human_tolerates_missing_mismatches() -> None:
    out = io.StringIO()
    cli._print_doctor({"workspace": "/tmp/ws"}, out)
    assert "registry mismatches" not in out.getvalue()


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _message(text: str):
    from nexus.model.message import Message, Text

    return Message(role="user", content=[Text(text=text)])


class _FakeSessions:
    def __init__(self, directory: Path | None) -> None:
        self.directory = directory

    def list(self):
        return []


class _FakeRegistry:
    def status(self):
        return _status()


def _status():
    from nexus.model.registry import RegistryStatus

    return RegistryStatus(source="cache", model_count=1, stale=False)


class _FakeRuntime:
    def __init__(self, directory: Path | None) -> None:
        self.workspace = "/tmp/ws"
        self.sessions = _FakeSessions(directory)
        self.registry = _FakeRegistry()
        self.providers: dict[str, object] = {}
        self.extensions = None
        self.mcp = None
