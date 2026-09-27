"""Privacy and resource bounds for the daemon's in-memory diagnostics."""
from __future__ import annotations

import json

import pytest

import nexus.host.diagnostics as diagnostics_module
from nexus.host.daemon import Daemon
from nexus.host.diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_ENTRIES,
    MAX_DIAGNOSTIC_PAGE,
    MAX_DIAGNOSTIC_SUMMARY_BYTES,
    DaemonDiagnostics,
)


def _capture(store: DaemonDiagnostics, count: int) -> None:
    for _ in range(count):
        store.capture("daemon.session_scheduled")


def test_daemon_captures_reviewed_ordinary_and_error_events(monkeypatch):
    times = iter((100.25, 101.5))
    monkeypatch.setattr(diagnostics_module.time, "time", lambda: next(times))
    daemon = DaemonDiagnostics()

    daemon.capture("daemon.started", {"pid": 123})
    daemon.capture(
        "daemon.session_failed",
        {"session": "private-session", "error": "private exception"},
    )

    page = daemon.read(None, 10)
    assert page["entries"] == [
        {
            "source": "daemon",
            "seq": 1,
            "ts": 100.25,
            "level": "info",
            "kind": "daemon.started",
            "summary": "Daemon started (pid 123).",
        },
        {
            "source": "daemon",
            "seq": 2,
            "ts": 101.5,
            "level": "error",
            "kind": "daemon.session_failed",
            "summary": "A session turn failed.",
        },
    ]
    assert not page["truncated"]
    assert not page["has_more"]


def test_daemon_log_and_emit_do_not_leak_secrets_or_raw_fields(tmp_path):
    daemon = Daemon(tmp_path)
    secret = "sk-super-secret-token"
    daemon._log("daemon.started", pid=456, socket="/private/socket", token=secret)
    daemon._emit(
        "daemon.session_failed",
        {
            "session": "session-secret",
            "error": secret,
            "prompt": "private prompt",
            "tool_params": {"password": secret},
        },
    )
    daemon._log("unreviewed.category", value=secret)

    packet = json.dumps(daemon.diagnostics.read(None, 100), ensure_ascii=False)
    assert "sk-super-secret-token" not in packet
    assert "private/socket" not in packet
    assert "session-secret" not in packet
    assert "private prompt" not in packet
    assert "unreviewed.category" not in packet
    assert "token" not in packet


def test_summaries_are_fixed_text_and_ignore_unicode_control_injection():
    store = DaemonDiagnostics()
    hostile = "\x1b[31m<script>秘密\u202e\x00\nhttps://user:pass@example.test/?token=secret"
    store.capture("daemon.session_failed", {"error": hostile, "prompt": hostile})
    store.capture(hostile, {"value": hostile})

    page = store.read(None, 10)
    assert len(page["entries"]) == 1
    assert page["entries"][0]["summary"] == "A session turn failed."
    assert hostile not in json.dumps(page, ensure_ascii=False)
    assert "<script>" not in json.dumps(page, ensure_ascii=False)
    assert "\x1b" not in json.dumps(page, ensure_ascii=False)


def test_count_byte_page_and_response_limits():
    store = DaemonDiagnostics()
    _capture(store, MAX_DIAGNOSTIC_ENTRIES + 50)

    page = store.read(None, MAX_DIAGNOSTIC_PAGE)
    assert len(page["entries"]) == MAX_DIAGNOSTIC_PAGE
    assert page["truncated"] is False
    assert page["has_more"] is False
    assert all(len(entry["summary"].encode("utf-8")) <= MAX_DIAGNOSTIC_SUMMARY_BYTES for entry in page["entries"])
    assert len(json.dumps(page).encode("utf-8")) < MAX_DIAGNOSTIC_BYTES

    tail = store.read(None, MAX_DIAGNOSTIC_PAGE)
    assert len(tail["entries"]) == MAX_DIAGNOSTIC_PAGE
    assert tail["entries"][0]["seq"] == MAX_DIAGNOSTIC_ENTRIES + 50 - MAX_DIAGNOSTIC_PAGE + 1
    assert tail["entries"][-1]["seq"] == MAX_DIAGNOSTIC_ENTRIES + 50
    assert store._bytes <= MAX_DIAGNOSTIC_BYTES
    assert len(store._entries) == MAX_DIAGNOSTIC_ENTRIES
    assert store._entries[0][0]["seq"] == 51


def test_capture_enforces_summary_byte_limit(monkeypatch):
    monkeypatch.setattr(diagnostics_module, "MAX_DIAGNOSTIC_SUMMARY_BYTES", 8)
    store = DaemonDiagnostics()

    store.capture("daemon.session_failed")

    assert store.read(None, 10)["entries"] == []


def test_count_eviction_with_small_temporary_cap(monkeypatch):
    monkeypatch.setattr(diagnostics_module, "MAX_DIAGNOSTIC_ENTRIES", 3)
    store = DaemonDiagnostics()
    _capture(store, 5)

    assert len(store._entries) == 3
    assert store._bytes <= diagnostics_module.MAX_DIAGNOSTIC_BYTES
    assert [entry[0]["seq"] for entry in store._entries] == [3, 4, 5]


def test_byte_eviction_with_small_temporary_cap(monkeypatch):
    entry_size = 96 + len("daemon.session_scheduled") + len("A session turn was scheduled.")
    monkeypatch.setattr(diagnostics_module, "MAX_DIAGNOSTIC_BYTES", 2 * entry_size)
    store = DaemonDiagnostics()
    _capture(store, 5)

    assert len(store._entries) == 2
    assert store._bytes <= diagnostics_module.MAX_DIAGNOSTIC_BYTES
    assert [entry[0]["seq"] for entry in store._entries] == [4, 5]


def test_cursor_pages_advance_and_overwrite_is_explicit():
    store = DaemonDiagnostics()
    _capture(store, 5)
    first = store.read(f"{store._generation}:0", 2)
    second = store.read(first["next_cursor"], 2)
    assert [entry["seq"] for entry in first["entries"]] == [1, 2]
    assert first["has_more"] is True
    assert [entry["seq"] for entry in second["entries"]] == [3, 4]
    assert second["has_more"] is True

    _capture(store, MAX_DIAGNOSTIC_ENTRIES + 1)
    stale = store.read(first["next_cursor"], 3)
    assert stale["truncated"] is True
    assert [entry["seq"] for entry in stale["entries"]] == [7, 8, 9]
    assert stale["next_cursor"].endswith(":9")


def test_restart_generation_mismatch_returns_tail_and_marks_truncation():
    previous = DaemonDiagnostics()
    _capture(previous, 3)
    old_cursor = previous.read(None, 10)["next_cursor"]

    restarted = DaemonDiagnostics()
    _capture(restarted, 2)
    page = restarted.read(old_cursor, 10)
    assert page["truncated"] is True
    assert [entry["seq"] for entry in page["entries"]] == [1, 2]
    assert page["next_cursor"].split(":", 1)[0] != old_cursor.split(":", 1)[0]


@pytest.mark.parametrize("cursor", ["bad", "0:1", "g" * 32 + ":1", "a" * 32 + ":-1"])
def test_invalid_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        DaemonDiagnostics().read(cursor, 10)


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "10"])
def test_invalid_limit_is_rejected(limit):
    with pytest.raises(ValueError):
        DaemonDiagnostics().read(None, limit)


def test_diagnostics_read_never_opens_the_owner_log_file(tmp_path, monkeypatch):
    daemon = Daemon(tmp_path)
    _capture(daemon.diagnostics, 1)

    def fail_open(*_args, **_kwargs):
        raise AssertionError("diagnostics attempted filesystem access")

    monkeypatch.setattr(type(daemon.log_file), "open", fail_open)
    assert daemon.diagnostics.read(None, 10)["entries"][0]["kind"] == "daemon.session_scheduled"
