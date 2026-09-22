import os

import msgspec
import pytest

from nexus.errors import SessionError
from nexus.events import Event
from nexus.model.message import Document, Image, Message, Text
from nexus.session.store import (
    SESSION_LOG_VERSION,
    EventRecord,
    MessageRecord,
    SessionStore,
)


def _user(text="hello"):
    return Message(role="user", content=[Text(text=text)])


def _assistant(text="world"):
    return Message(role="assistant", content=[Text(text=text)])


def test_append_and_reopen_reconstructs_history(tmp_path):
    store = SessionStore(tmp_path)
    first = store.append_event("main", Event(type="session.opened", data={"n": 1}))
    second = store.append_message("main", _user("hi"))
    third = store.append_message("main", _assistant("there"))

    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert first.event.session == "main"
    assert first.event.seq == 1

    reopened = SessionStore(tmp_path).read("main")
    assert reopened.truncated_tail is False
    assert [type(r).__name__ for r in reopened.records] == [
        "EventRecord",
        "MessageRecord",
        "MessageRecord",
    ]
    assert reopened.messages() == [_user("hi"), _assistant("there")]
    assert [e.type for e in reopened.events()] == ["session.opened"]
    assert reopened.next_seq == 3


def test_bytes_blocks_round_trip_losslessly(tmp_path):
    store = SessionStore(tmp_path)
    message = Message(
        role="user",
        content=[
            Text(text="see"),
            Image(media_type="image/png", data=b"\x00\x01\xff\xfe"),
            Document(media_type="application/pdf", data=b"%PDF-1.7\x00binary", title="d"),
        ],
    )
    store.append_message("bytes", message)
    decoded = SessionStore(tmp_path).read("bytes").messages()[0]
    assert decoded == message
    assert decoded.content[1].data == b"\x00\x01\xff\xfe"
    assert decoded.content[2].data == b"%PDF-1.7\x00binary"


def test_every_append_is_a_complete_flushed_line(tmp_path):
    calls = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        real_fsync(fd)

    store = SessionStore(tmp_path, fsync=spy)
    store.append_message("main", _user("one"))
    store.append_message("main", _assistant("two"))

    raw = (tmp_path / "main.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.splitlines()
    assert len(lines) == 2
    for line in lines:
        msgspec.json.decode(line)  # each line is independently valid JSON
    assert len(calls) == 2


def test_append_never_rewrites_existing_bytes(tmp_path):
    store = SessionStore(tmp_path)
    store.append_message("main", _user("one"))
    before = (tmp_path / "main.jsonl").read_bytes()
    store.append_message("main", _assistant("two"))
    after = (tmp_path / "main.jsonl").read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)


def test_seq_is_monotonic_across_store_instances(tmp_path):
    SessionStore(tmp_path).append_message("main", _user("one"))
    second = SessionStore(tmp_path).append_message("main", _assistant("two"))
    assert second.seq == 2
    assert SessionStore(tmp_path).next_seq("main") == 3


def test_truncated_final_tail_is_tolerated(tmp_path):
    store = SessionStore(tmp_path)
    store.append_message("main", _user("complete"))
    path = tmp_path / "main.jsonl"
    with open(path, "ab") as handle:
        handle.write(b'{"type":"message","seq":2,"message":{"role":"user"')

    result = SessionStore(tmp_path).read("main")
    assert result.truncated_tail is True
    assert result.messages() == [_user("complete")]


def test_malformed_interior_record_fails(tmp_path):
    store = SessionStore(tmp_path)
    store.append_message("main", _user("complete"))
    path = tmp_path / "main.jsonl"
    raw = path.read_bytes()
    path.write_bytes(raw + b'{"not":"a record"}\n' + raw)

    with pytest.raises(SessionError):
        SessionStore(tmp_path).read("main")


def test_malformed_interior_line_before_valid_tail_fails(tmp_path):
    path = tmp_path / "main.jsonl"
    valid = msgspec.json.encode(MessageRecord(seq=1, message=_user("ok"))) + b"\n"
    path.write_bytes(b"{bad json}\n" + valid)
    with pytest.raises(SessionError):
        SessionStore(tmp_path).read("main")


def test_empty_interior_line_fails(tmp_path):
    store = SessionStore(tmp_path)
    store.append_message("main", _user("ok"))
    path = tmp_path / "main.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(SessionError):
        SessionStore(tmp_path).read("main")


def test_version_mismatch_is_rejected(tmp_path):
    path = tmp_path / "main.jsonl"
    record = MessageRecord(seq=1, message=_user("ok"))
    payload = msgspec.json.decode(msgspec.json.encode(record))
    payload["v"] = SESSION_LOG_VERSION + 1
    path.write_bytes(msgspec.json.encode(payload) + b"\n")
    with pytest.raises(SessionError):
        SessionStore(tmp_path).read("main")


def test_empty_and_missing_logs_read_as_empty(tmp_path):
    store = SessionStore(tmp_path)
    assert store.read("missing").records == ()
    (tmp_path / "empty.jsonl").write_bytes(b"")
    assert store.read("empty").records == ()


def test_record_envelope_distinguishes_event_and_message(tmp_path):
    store = SessionStore(tmp_path)
    store.append_event("main", Event(type="text", data={"text": "x"}))
    store.append_message("main", _user("x"))
    raw_lines = (tmp_path / "main.jsonl").read_bytes().splitlines()
    assert msgspec.json.decode(raw_lines[0])["type"] == "event"
    assert msgspec.json.decode(raw_lines[1])["type"] == "message"
    assert EventRecord(seq=1, event=Event(type="text")).v == SESSION_LOG_VERSION


def test_invalid_session_id_is_rejected(tmp_path):
    store = SessionStore(tmp_path)
    with pytest.raises(ValueError):
        store.read("../escape")
    with pytest.raises(ValueError):
        store.append_message("bad/id", _user())


# ---------------------------------------------------------------------------
# Crash-tail repair and durability
# ---------------------------------------------------------------------------


def test_append_repairs_truncated_tail_then_reopen_reads_all(tmp_path):
    store = SessionStore(tmp_path)
    store.append_message("main", _user("complete"))
    path = tmp_path / "main.jsonl"
    with open(path, "ab") as handle:
        handle.write(b'{"type":"message","seq":2,"message":{"role":"user"')

    assert SessionStore(tmp_path).read("main").truncated_tail is True

    record = store.append_message("main", _assistant("new"))
    assert record.seq == 2

    reopened = SessionStore(tmp_path).read("main")
    assert reopened.truncated_tail is False
    assert [m.content[0].text for m in reopened.messages()] == ["complete", "new"]
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert len(raw.splitlines()) == 2


def test_append_repairs_file_that_is_only_a_crash_tail(tmp_path):
    path = tmp_path / "main.jsonl"
    path.write_bytes(b'{"type":"mess')

    store = SessionStore(tmp_path)
    record = store.append_message("main", _user("first"))

    assert record.seq == 1
    reopened = SessionStore(tmp_path).read("main")
    assert [m.content[0].text for m in reopened.messages()] == ["first"]
    assert len(path.read_bytes().splitlines()) == 1


def test_append_after_valid_record_without_newline_yields_two_lines(tmp_path):
    # A complete record whose terminator was lost must be terminated before the
    # next append, or the two records would concatenate onto one line.
    store = SessionStore(tmp_path)
    store.append_message("main", _user("first"))
    path = tmp_path / "main.jsonl"
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    path.write_bytes(raw[:-1])  # drop only the newline

    second = store.append_message("main", _assistant("second"))

    assert second.seq == 2
    text = path.read_bytes()
    assert text.endswith(b"\n")
    lines = text.splitlines()
    assert len(lines) == 2
    reopened = SessionStore(tmp_path).read("main")
    assert reopened.truncated_tail is False
    assert [m.content[0].text for m in reopened.messages()] == ["first", "second"]


def test_newline_terminated_malformed_final_record_is_fail_closed(tmp_path):
    path = tmp_path / "main.jsonl"
    path.write_bytes(b'{"not":"a record"}\n')
    store = SessionStore(tmp_path)

    with pytest.raises(SessionError):
        store.read("main")
    with pytest.raises(SessionError):
        store.append_message("main", _user("x"))
    assert path.read_bytes() == b'{"not":"a record"}\n'


def test_create_fsyncs_file_and_parent_directory(tmp_path):
    calls = []
    store = SessionStore(tmp_path, fsync=calls.append)
    store.create("main")
    # One fsync for the new file, one for the containing directory.
    assert len(calls) >= 2


# ---------------------------------------------------------------------------
# Atomic publish from records (the fork primitive)
# ---------------------------------------------------------------------------


def test_create_from_records_preserves_records_and_never_overwrites(tmp_path):
    source = SessionStore(tmp_path)
    first = source.append_message("src", _user("a"))
    second = source.append_event("src", Event(type="custom", data={"n": 1}))

    dest = SessionStore(tmp_path)
    path = dest.create_from_records("child", [first, second])
    assert path.name == "child.jsonl"
    assert SessionStore(tmp_path).read("child").records == (first, second)

    with pytest.raises(SessionError):
        SessionStore(tmp_path).create_from_records("child", [first])
    assert SessionStore(tmp_path).read("child").records == (first, second)


def test_create_from_records_next_append_is_monotonic(tmp_path):
    source = SessionStore(tmp_path)
    records = [
        source.append_message("src", _user("a")),
        source.append_message("src", _assistant("b")),
    ]
    dest = SessionStore(tmp_path)
    dest.create_from_records("child", records)
    assert dest.next_seq("child") == 3
    appended = dest.append_message("child", _user("c"))
    assert appended.seq == 3


def test_create_from_records_publishes_atomically_without_replace(tmp_path, monkeypatch):
    # The publish must be an exclusive atomic link, not a check-then-replace.
    source = SessionStore(tmp_path)
    records = [source.append_message("src", _user("a"))]

    def boom(*args, **kwargs):
        raise AssertionError("create_from_records must not use os.replace")

    monkeypatch.setattr(os, "replace", boom)
    dest = SessionStore(tmp_path)
    dest.create_from_records("linked", records)
    assert SessionStore(tmp_path).read("linked").records == tuple(records)


def test_summary_records_keep_sequence_monotonic(tmp_path):
    store = SessionStore(tmp_path)
    store.append_message("s", _user("a"))
    first = store.append_summary("s", text="one", summary_id="1", source_to_seq=1)
    store.append_message("s", _assistant("b"))
    second = store.append_summary("s", text="two", summary_id="2", source_to_seq=3)
    assert second.seq > first.seq
    seqs = [record.seq for record in store.read("s").records]
    assert seqs == sorted(seqs)
    assert store.read("s").summaries() == [first, second]


