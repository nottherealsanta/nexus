"""Shared host LogsRead command, bounded session projection, and safe wire shape."""
from __future__ import annotations

import secrets
import tempfile
from pathlib import Path
from types import SimpleNamespace

import msgspec
import pytest

from nexus.events import Event
from nexus.host import protocol as p
from nexus.host.diagnostics import DaemonDiagnostics
from nexus.host.facade import HostFacade
from nexus.host.session_diagnostics import (
    SESSION_LOG_READ_BYTES,
    SESSION_LOG_SCAN_WINDOW,
    read_session_page,
    session_records,
)
from nexus.session.ids import validate_session_id
from nexus.session.store import (
    SESSION_LOG_VERSION,
    EventRecord,
    ReadResult,
    SummaryRecord,
)


class _Handle:
    def __init__(self, session_id: str, records=()):
        self.id = session_id
        self._read = SimpleNamespace(records=tuple(records))


class _Sessions:
    def __init__(self):
        self.handles: dict[str, _Handle] = {}
        self.calls: list[tuple[str, bool, bool]] = []
        self.store = SimpleNamespace(exists=lambda session_id: session_id in self.handles)

    def open(self, session_id, *, create=True, recover=True):
        session_id = validate_session_id(session_id)
        self.calls.append((session_id, create, recover))
        if session_id not in self.handles:
            if not create:
                raise ValueError("session does not exist")
            self.handles[session_id] = _Handle(session_id)
        return self.handles[session_id]

    def _live_handle(self, session_id):
        return self.handles.get(session_id)


class _Runtime:
    def __init__(self):
        self.sessions = _Sessions()

    def session(self, session_id, *, create=True, recover=True):
        return self.sessions.open(session_id, create=create, recover=recover)


def _event(seq: int, kind: str, data=None, *, ts: float | None = None):
    return EventRecord(
        seq=seq,
        ts=float(seq),
        event=Event(
            type=kind,
            seq=seq,
            ts=float(seq) if ts is None else ts,
            session="s",
            data=data or {},
        ),
    )


def _facade(records=(), diagnostics=None):
    runtime = _Runtime()
    runtime.sessions.handles["s"] = _Handle("s", records)
    facade = HostFacade(runtime)
    facade.daemon_diagnostics = diagnostics
    return facade, runtime


def test_logs_read_protocol_command_and_result_round_trip():
    command = p.LogsRead(
        session="s", daemon_cursor="a" * 32 + ":4", session_cursor=7, limit=20
    )
    assert p.decode_command(p.encode_command(command)) == command
    result = p.LogsReadResult(
        daemon=p.DaemonLogPage(
            entries=[p.LogEntry("daemon", 4, 1.25, "info", "daemon.started", "Daemon started.")],
            next_cursor="a" * 32 + ":4",
        ),
        session=p.SessionLogPage(
            entries=[p.LogEntry("session", 8, 8.0, "warning", "turn.cancelled", "A turn was cancelled.")],
            next_cursor=8,
        ),
    )
    assert p.decode_result(p.encode_result(result)) == result
    assert p.LogsRead in p.COMMANDS
    assert p.LogsReadResult in p.RESULTS


@pytest.mark.asyncio
async def test_logs_read_pages_both_sources_and_missing_session():
    diagnostics = DaemonDiagnostics()
    diagnostics.capture("daemon.started", {"pid": 123})
    records = [_event(1, "turn.started"), _event(2, "text.delta", {"text": "private"}), _event(3, "turn.completed")]
    facade, runtime = _facade(records, diagnostics)

    first = await facade.handle(p.LogsRead(session="s", limit=1))
    assert isinstance(first, p.LogsReadResult)
    assert [entry.kind for entry in first.daemon.entries] == ["daemon.started"]
    assert [entry.kind for entry in first.session.entries] == ["turn.completed"]
    assert first.session.next_cursor == 3
    assert runtime.sessions.calls == []

    daemon_error = await facade.handle(
        p.LogsRead(daemon_cursor=first.daemon.next_cursor)
    )
    assert daemon_error.daemon.entries == []

    missing = await facade.handle(p.LogsRead())
    assert missing.session.entries == []
    assert missing.session.next_cursor == 0
    assert missing.daemon.entries == first.daemon.entries

    follow = await facade.handle(
        p.LogsRead(session="s", daemon_cursor=first.daemon.next_cursor, session_cursor=1)
    )
    assert follow.daemon.entries == []
    assert [entry.kind for entry in follow.session.entries] == ["turn.completed"]


@pytest.mark.asyncio
async def test_logs_read_projects_error_level_entries_from_both_sources():
    diagnostics = DaemonDiagnostics()
    diagnostics.capture("daemon.session_failed")
    facade, _ = _facade([_event(1, "turn.failed")], diagnostics)
    result = await facade.handle(p.LogsRead(session="s"))
    assert result.daemon.entries[0].level == "error"
    assert result.session.entries[0].level == "error"


@pytest.mark.asyncio
async def test_logs_read_allowlist_never_copies_sensitive_event_data():
    secret_values = {
        "text": "SESSION_MESSAGE_SECRET",
        "thinking": "THINKING_SECRET",
        "tool": "CustomTool_SECRET",
        "params": {"path": "/private/path_SECRET", "content": "TOOL_INPUT_SECRET"},
        "result": "TOOL_OUTPUT_SECRET",
        "preview": "APPROVAL_PREVIEW_SECRET",
        "error": "PROVIDER_ERROR_SECRET",
        "path": "/etc/passwd_SECRET",
        "control": "CONTROL\x1b[31mSECRET",
    }
    records = [
        _event(index, kind, secret_values)
        for index, kind in enumerate(
            (
                "turn.started",
                "tool.started",
                "tool.progress",
                "tool.result",
                "permission.requested",
                "permission.resolved",
                "model.retrying",
                "turn.failed",
                "turn.cancelled",
                "error",
            ),
            1,
        )
    ]
    facade, _ = _facade(records)
    result = await facade.handle(p.LogsRead(session="s", limit=100))
    wire = p.encode_result(result).decode()
    for secret in secret_values.values():
        assert str(secret) not in wire
    for secret in (
        "SESSION_MESSAGE_SECRET",
        "THINKING_SECRET",
        "CustomTool_SECRET",
        "TOOL_INPUT_SECRET",
        "TOOL_OUTPUT_SECRET",
        "APPROVAL_PREVIEW_SECRET",
        "PROVIDER_ERROR_SECRET",
        "/etc/passwd_SECRET",
        "CONTROL\x1b[31mSECRET",
    ):
        assert secret not in wire
    assert [entry.kind for entry in result.session.entries] == [
        "turn.started",
        "tool.started",
        "permission.requested",
        "permission.resolved",
        "model.retrying",
        "turn.failed",
        "turn.cancelled",
        "error",
    ]
    assert all(len(entry.summary) < 100 for entry in result.session.entries)


@pytest.mark.asyncio
async def test_logs_read_large_history_is_bounded_and_stale_cursor_truncates():
    records = [_event(seq, "turn.started") for seq in range(1, SESSION_LOG_SCAN_WINDOW + 1001)]
    facade, _ = _facade(records)

    tail = await facade.handle(p.LogsRead(session="s", limit=7))
    assert len(tail.session.entries) == 7
    assert [entry.seq for entry in tail.session.entries] == list(range(len(records) - 6, len(records) + 1))
    assert tail.session.truncated is True
    assert tail.session.has_more is False

    stale = await facade.handle(p.LogsRead(session="s", session_cursor=0, limit=5))
    assert stale.session.truncated is True
    assert [entry.seq for entry in stale.session.entries] == list(range(1001, 1006))
    assert stale.session.has_more is True
    assert stale.session.next_cursor == 1005

    following = await facade.handle(
        p.LogsRead(session="s", session_cursor=stale.session.next_cursor, limit=5)
    )
    assert [entry.seq for entry in following.session.entries] == list(range(1006, 1011))


def test_cold_session_scan_reads_only_a_bounded_tail(tmp_path):
    path = tmp_path / "safe-session.jsonl"
    with path.open("wb") as stream:
        for seq in range(1, 30_001):
            stream.write(
                msgspec.json.encode(_event(seq, "turn.started")) + b"\n"
            )

    class Store:
        def log_path(self, session_id):
            assert session_id == "safe-session"
            return path

    handle = SimpleNamespace(id="safe-session", _read=None, _store=Store())
    records, clipped, latest_seq = session_records(handle)
    assert path.stat().st_size > SESSION_LOG_READ_BYTES
    assert len(records) <= SESSION_LOG_SCAN_WINDOW
    assert clipped is True
    page = read_session_page(records, cursor=0, limit=3, latest_seq=latest_seq)
    assert page["truncated"] is True
    assert len(page["entries"]) == 3
    assert page["entries"][0]["seq"] > 1


@pytest.mark.asyncio
async def test_logs_read_rejects_bad_session_cursors_limits_and_types():
    facade, runtime = _facade([_event(1, "turn.started")])
    bad_commands = [
        p.LogsRead(session="../s"),
        p.LogsRead(session="missing"),
        p.LogsRead(session="s", session_cursor=-1),
        p.LogsRead(session="s", session_cursor=True),
        p.LogsRead(session="s", session_cursor="1"),
        p.LogsRead(session="s", session_cursor=2),
        p.LogsRead(session="s", limit=0),
        p.LogsRead(session="s", limit=101),
        p.LogsRead(session="s", limit=True),
        p.LogsRead(session="s", daemon_cursor=2),
        p.LogsRead(daemon_cursor="bad-generation:1"),
        p.LogsRead(daemon_cursor="a" * 32 + ":-1"),
    ]
    for command in bad_commands:
        result = await facade.handle(command)
        assert isinstance(result, p.ErrorResult), command
    assert runtime.sessions.calls == []


def test_session_page_advances_and_marks_truncation_when_all_records_filtered():
    page = read_session_page([], cursor=4, limit=10, latest_seq=12)

    assert page == {
        "entries": [],
        "next_cursor": 12,
        "truncated": True,
        "has_more": False,
    }
    tail = read_session_page([], cursor=None, limit=10, latest_seq=12)
    assert tail["next_cursor"] == 12
    assert tail["truncated"] is True


def test_cold_session_scan_skips_unsupported_envelopes_and_flags_them(tmp_path):
    path = tmp_path / "versioned-session.jsonl"
    supported = msgspec.json.encode(_event(1, "turn.started"))
    unsupported = msgspec.json.encode(
        {**msgspec.json.decode(supported), "seq": 2, "v": SESSION_LOG_VERSION + 1}
    )
    with path.open("wb") as stream:
        stream.write(unsupported + b"\n")

    class Store:
        def log_path(self, session_id):
            return path

    records, clipped, latest_seq = session_records(
        SimpleNamespace(id="versioned-session", _read=None, _store=Store())
    )

    assert records == []
    assert clipped is True
    assert latest_seq == 2
    page = read_session_page(records, cursor=0, limit=10, latest_seq=latest_seq)
    assert page["next_cursor"] == 2
    assert page["truncated"] is True


def test_cached_truncated_tail_is_reported(tmp_path):
    record = _event(5, "turn.started")
    handle = SimpleNamespace(
        id="s",
        _read=ReadResult(records=(record,), truncated_tail=True),
    )

    records, clipped, latest_seq = session_records(handle)
    page = read_session_page(records, cursor=None, limit=10, latest_seq=latest_seq)

    assert clipped is True
    assert page["entries"][0]["seq"] == 5


def test_cold_session_scan_discards_crash_tail_and_uses_valid_cursor(tmp_path):
    path = tmp_path / "crash-tail-session.jsonl"
    with path.open("wb") as stream:
        stream.write(msgspec.json.encode(_event(5, "turn.started")) + b"\n{broken")

    class Store:
        def log_path(self, session_id):
            return path

    records, clipped, latest_seq = session_records(
        SimpleNamespace(id="crash-tail-session", _read=None, _store=Store())
    )
    page = read_session_page(records, cursor=0, limit=10, latest_seq=latest_seq)

    assert clipped is True
    assert [entry["seq"] for entry in page["entries"]] == [5]
    assert page["next_cursor"] == 5


@pytest.mark.asyncio
async def test_daemon_generation_cursor_restart_is_reported_as_truncated():
    old = DaemonDiagnostics()
    old.capture("daemon.started", {"pid": 1})
    old_page = old.read(None, 10)
    replacement = DaemonDiagnostics()
    replacement.capture("daemon.started", {"pid": 2})
    replacement.capture("daemon.stopping")
    facade, _ = _facade(diagnostics=replacement)
    facade.daemon_diagnostics = replacement

    restarted = await facade.handle(
        p.LogsRead(daemon_cursor=old_page["next_cursor"])
    )
    assert restarted.daemon.truncated is True
    assert [entry.kind for entry in restarted.daemon.entries] == ["daemon.started", "daemon.stopping"]
    assert restarted.session.entries == []


@pytest.mark.asyncio
async def test_real_daemon_injects_its_generation_diagnostics_into_facade(tmp_path):
    from nexus.config import Config
    from nexus.config.schema import (
        AgentSection,
        ConfigV2,
        ModelSection,
        PermissionsSection,
        ToolsSection,
    )
    from nexus.host.daemon import Daemon
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime

    def make_runtime(workspace, **kwargs):
        config = Config(
            model="scripted/m",
            version=2,
            v2=ConfigV2(
                model=ModelSection(default="scripted/m"),
                agent=AgentSection(profile="coding"),
                permissions=PermissionsSection(mode="ask", on_unattended="deny"),
                tools=ToolsSection(),
            ),
        )
        return Runtime(workspace, config=config, providers={"scripted": ScriptedProvider(text_response("ok"))})

    daemon = Daemon(
        tmp_path,
        socket_path=Path(tempfile.gettempdir()) / f"n-{secrets.token_hex(4)}.sock",
        runtime_factory=make_runtime,
    )
    await daemon.start()
    try:
        result = await daemon.facade.handle(p.LogsRead())
        assert isinstance(result, p.LogsReadResult)
        assert result.daemon.entries
        assert result.daemon.entries[0].kind == "daemon.started"
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_logs_read_does_not_change_session_records_or_daemon_ring():
    diagnostics = DaemonDiagnostics()
    diagnostics.capture("daemon.started", {"pid": 5})
    before_daemon = diagnostics.read(None, 10)
    records = [_event(1, "turn.started"), SummaryRecord(seq=2, ts=2.0, text="hidden")]
    facade, _ = _facade(records, diagnostics)
    before_records = facade.runtime.sessions.handles["s"]._read.records

    await facade.handle(p.LogsRead(session="s"))

    assert facade.runtime.sessions.handles["s"]._read.records == before_records
    assert diagnostics.read(None, 10) == before_daemon
