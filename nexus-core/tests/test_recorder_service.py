"""Recorder service and leases (plan 54, B2): ownership, expiry, recovery and idempotent transport."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import threading

import pytest

from nexus.recorder import (
    RecorderConflict,
    RecorderError,
    RecorderInvalid,
    RecorderNotFound,
    RecorderSecurityError,
    RecorderStore,
)
from nexus.recorder import service as service_module
from nexus.recorder.service import (
    CHUNK_TOO_LARGE,
    CLIENT_MISMATCH,
    CREATE_GUARD_FULL,
    CREATE_NOT_FOUND,
    CREATE_WITHDRAWN,
    DELETE_NOT_DURABLE,
    DETACH_GRACE_SECONDS,
    IDEMPOTENCY_CONFLICT,
    INVALID_LEASE,
    LEASE_EXPIRED,
    LEASE_TTL_SECONDS,
    MAX_WITHDRAWN_KEYS,
    NOT_ACTIVE,
    NOT_OWNER,
    RECORDER_BUSY,
    RECORDING_ACTIVE,
    RECOVERY_NOT_SEALED,
    RECOVERY_REQUIRED,
    STARTUP_FAILED,
    STOP_NOT_DURABLE,
    STREAM_CLOSED,
    STREAM_DETACHED,
    STREAM_UNBOUND,
    RecorderService,
    RecorderServiceError,
)

BYTES_PER_SAMPLE = 2


def body(index: int, samples: int = 160) -> bytes:
    return bytes([index % 251]) * (samples * BYTES_PER_SAMPLE)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Clock:
    def __init__(self, now: float = 1_700_000_000.0, mono: float = 10_000.0) -> None:
        self.now = now
        self.mono = mono

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.mono += seconds

    def jump_wall(self, seconds: float) -> None:
        self.now += seconds


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fail_remaining = 0
        self.fail_types: set[str] | None = None

    def fail(self, times: int = 1, types: set[str] | None = None) -> None:
        self.fail_remaining = times
        self.fail_types = types

    def publish(self, type_, data=None, *, session_id=None, turn_id=None, durable=True):
        if self.fail_remaining > 0 and (self.fail_types is None or type_ in self.fail_types):
            self.fail_remaining -= 1
            raise RuntimeError("injected bus failure")
        event = {"type": type_, "data": copy.deepcopy(data), "session_id": session_id, "durable": durable}
        self.events.append(event)
        return event

    def of(self, kind: str) -> list[dict]:
        return [event for event in self.events if event["type"] == kind]


class FakeStreams:
    def __init__(self, connected: set[str] | None = None,
                 clients: dict[str, str] | None = None) -> None:
        self.connected = set(connected if connected is not None else {"s1", "s2", "s3"})
        self.clients = dict(clients if clients is not None else {"s1": "tui", "s2": "nsh", "s3": "nsh"})
        self.listeners = []

    def has_stream(self, stream_id: str) -> bool:
        return stream_id in self.connected

    def view_for(self, stream_id: str) -> dict[str, str] | None:
        if stream_id not in self.connected:
            return None
        client_id = self.clients.get(stream_id)
        return None if client_id is None else {"stream_id": stream_id, "client_id": client_id}

    def bind(self, stream_id: str, client_id: str) -> None:
        self.connected.add(stream_id)
        self.clients[stream_id] = client_id

    def on_detach(self, listener) -> None:
        self.listeners.append(listener)

    def detach(self, stream_id: str) -> None:
        self.connected.discard(stream_id)
        for listener in list(self.listeners):
            listener(stream_id)


class FakeScheduler:
    def __init__(self, *, raise_hooks: bool = False) -> None:
        self.cancelled: list[str] = []
        self.calls: list[tuple[str, tuple, int]] = []
        self.raise_hooks = raise_hooks
        self.capture_failures = 0

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, args, threading.get_ident()))
        if self.raise_hooks:
            raise RuntimeError(f"injected {name} failure")

    def audio_committed(self, recording_id: str, generation: int, durable_offset: int) -> None:
        self._record("audio_committed", recording_id, generation, durable_offset)

    def capture_ended(self, recording_id: str, generation: int, end_offset: int, state: str) -> None:
        self._record("capture_ended", recording_id, generation, end_offset, state)
        if self.capture_failures > 0:
            self.capture_failures -= 1
            raise RuntimeError("injected capture_ended failure")

    def recovered(self, ids: list[str]) -> None:
        self._record("recovered", ids)

    def cancel(self, recording_id: str) -> None:
        self.cancelled.append(recording_id)
        self._record("cancel", recording_id)


class Setup:
    def __init__(self, tmp_path, *, streams=None, bus=None, scheduler=None, store=None):
        self.clock = Clock()
        self.bus = bus if bus is not None else FakeBus()
        self.streams = streams if streams is not None else FakeStreams()
        self.scheduler = scheduler if scheduler is not None else FakeScheduler()
        self.store = store if store is not None else RecorderStore(tmp_path / "recordings")
        self.recovery = secrets.token_urlsafe(32)
        self.service = RecorderService(self.bus, self.streams, self.store, self.scheduler,
                                       clock=self.clock, monotonic=self.clock.monotonic)

    def create(self, *, stream="s1", client="tui", key="key-1", title=None, recovery=None):
        recovery = self.recovery if recovery is None else recovery
        return self.service.create(stream, client, key, title=title, recovery=recovery)


def has_bytes(value) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, dict):
        return any(has_bytes(key) or has_bytes(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_bytes(item) for item in value)
    return False


def assert_secret_free(event: dict, *secrets_to_check: str) -> None:
    text = json.dumps(event)
    assert not has_bytes(event)
    for secret in secrets_to_check:
        assert secret not in text


def test_create_is_single_active_and_idempotent_without_secrets(tmp_path):
    s = Setup(tmp_path)
    first = s.create(title="memo")
    recording_id = first["id"]
    assert first["active"] is True and first["capture_state"] == "recording"
    assert first["lease_token"]
    assert first["recovery_token"] is None and first["recovery"] == "client"
    assert first["owner"] == {"client_id": "tui"}
    assert first["free_seconds"] > 0
    assert "stream_id" not in first and "stream_id" not in first["owner"]

    snapshot = s.service.get(recording_id)
    assert snapshot["active"] is True and snapshot["owner"] == {"client_id": "tui"}
    assert "lease_token" not in snapshot and "recovery_token" not in snapshot
    assert "s1" not in json.dumps(snapshot)

    replay = s.create(title="memo")
    assert replay["id"] == recording_id and replay["idempotent"] is True
    assert replay["lease_token"] == first["lease_token"]
    assert replay["recovery_token"] is None
    assert replay["tokens_reissued"] is True

    with pytest.raises(RecorderServiceError) as busy:
        s.create(stream="s2", client="nsh", key="key-2")
    assert busy.value.code == RECORDER_BUSY and busy.value.status == 409

    with pytest.raises(RecorderServiceError) as conflict:
        s.create(stream="s2", client="nsh", key="key-1", title="memo")
    assert conflict.value.code == IDEMPOTENCY_CONFLICT

    states = s.bus.of("recorder.state")
    assert states and states[0]["session_id"] is None
    for event in states:
        assert event["durable"] is True
        assert_secret_free(event, first["lease_token"], s.recovery, "s1")


def test_create_replay_after_stop_hides_raw_tokens(tmp_path):
    s = Setup(tmp_path)
    first = s.create()
    s.service.stop(first["id"], first["lease_token"])

    replay = s.create()
    assert replay["id"] == first["id"] and replay["idempotent"] is True
    assert replay["replay_state"] == "stopped" and replay["tokens_reissued"] is False
    assert replay["lease_token"] is None and replay["lease"] is None
    assert replay["recovery_token"] is None


def test_startup_failure_is_reported_and_blocks_every_operation(tmp_path, monkeypatch):
    s = Setup(tmp_path)

    def fail() -> list[dict]:
        raise OSError("injected reconcile failure")

    monkeypatch.setattr(s.store, "reconcile_all", fail)
    with pytest.raises(RecorderServiceError) as startup:
        s.service.startup()
    assert startup.value.code == STARTUP_FAILED and startup.value.status == 503
    assert isinstance(startup.value.__cause__, OSError)

    for operation in (
        lambda: s.service.create("s1", "tui", "key", recovery=s.recovery),
        lambda: s.service.get("a" * 32),
        lambda: s.service.list(),
        lambda: s.service.sweep(),
    ):
        with pytest.raises(RecorderServiceError) as blocked:
            operation()
        assert blocked.value.code == STARTUP_FAILED
    assert not (tmp_path / "recordings").exists()


def test_startup_retries_after_a_transient_reconcile_failure(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    real_reconcile = s.store.reconcile_all
    calls = 0

    def flaky() -> list[dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected transient reconcile failure")
        return real_reconcile()

    monkeypatch.setattr(s.store, "reconcile_all", flaky)
    with pytest.raises(RecorderServiceError) as startup:
        s.service.startup()
    assert startup.value.code == STARTUP_FAILED
    created = s.create()
    assert created["active"] is True and calls == 2


def test_startup_keeps_per_recording_reconcile_errors_non_fatal(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    monkeypatch.setattr(s.store, "reconcile_all",
                        lambda: [{"id": "a" * 32, "error": "recording_corrupt"}])
    assert s.service.startup() == []
    created = s.create()
    assert created["active"] is True


def test_create_idempotency_survives_restart_without_reissuing_tokens(tmp_path):
    s = Setup(tmp_path)
    first = s.create(key="stable")
    recording_id, lease = first["id"], first["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)

    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    replay = service.create("s1", "tui", "stable", recovery=s.recovery)
    assert replay["id"] == recording_id and replay["idempotent"] is True
    assert replay["replay_state"] == "interrupted" and replay["recoverable"] is True
    assert replay["tokens_reissued"] is False
    assert replay["lease_token"] is None and replay["lease"] is None
    assert replay["recovery_token"] is None

    with pytest.raises(RecorderServiceError) as conflict:
        service.create("s2", "nsh", "stable", recovery=s.recovery)
    assert conflict.value.code == IDEMPOTENCY_CONFLICT

    # The client's original recovery token still works; create never minted or exposed a new one.
    sealed = service.recover_stop(recording_id, s.recovery, "tui", 160)
    assert sealed["sealed"] is True and sealed["complete"] is True


def test_create_idempotency_survives_restart_for_a_stopped_recording(tmp_path):
    s = Setup(tmp_path)
    first = s.create(key="stopped-key")
    s.service.stop(first["id"], first["lease_token"])

    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    replay = service.create("s1", "tui", "stopped-key", recovery=s.recovery)
    assert replay["id"] == first["id"] and replay["replay_state"] == "stopped"
    assert replay["idempotent"] is True and replay["tokens_reissued"] is False
    assert replay["lease_token"] is None and replay["recovery_token"] is None
    assert len(service.list()) == 1


def test_create_crash_after_store_create_recovers_idempotency_without_a_sidecar(
        tmp_path, monkeypatch):
    s = Setup(tmp_path)

    def crash(_record):
        raise SystemExit("simulated crash before the lease sidecar")

    def fail_rollback(_recording_id):
        raise OSError("simulated crash prevented rollback")

    monkeypatch.setattr(s.service, "_write_sidecar", crash)
    monkeypatch.setattr(s.store, "delete", fail_rollback)
    with pytest.raises(SystemExit):
        s.create(key="crash-key")

    recordings = [path for path in s.store.root.iterdir()
                  if path.is_dir() and len(path.name) == 32]
    assert len(recordings) == 1
    recording_id = recordings[0].name
    assert not (recordings[0] / "lease.json").exists()
    hashes = s.store.creation_hashes(recording_id)
    assert hashes["idempotency_sha256"] is not None
    assert hashes["recovery_sha256"] is not None

    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    replay = service.create("s1", "tui", "crash-key", recovery=s.recovery)
    assert replay["id"] == recording_id and replay["idempotent"] is True
    assert replay["replay_state"] == "interrupted" and replay["recoverable"] is True
    assert replay["lease_token"] is None and replay["recovery_token"] is None
    assert replay["recovery"] == "client"
    assert len(service.list()) == 1

    # The client capability that was persisted before create still seals the abandoned recording.
    resumed = service.resume("tui", "crash-key", s.recovery)
    assert resumed["id"] == recording_id and resumed["active"] is False
    assert resumed["lease_token"] is None and resumed["recovery_token"] is None
    sealed = service.recover_stop(recording_id, s.recovery, "tui", 0)
    assert sealed["sealed"] is True and sealed["complete"] is True


def test_create_requires_a_client_recovery_capability_and_never_exposes_it(tmp_path, caplog):
    s = Setup(tmp_path)
    with pytest.raises(RecorderServiceError) as missing:
        s.service.create("s1", "tui", "key")
    assert missing.value.code == RECOVERY_REQUIRED and missing.value.status == 400
    with pytest.raises(RecorderServiceError) as short:
        s.service.create("s1", "tui", "key", recovery="short")
    assert short.value.code == RECOVERY_REQUIRED

    created = s.create(key="kept")
    assert created["recovery_token"] is None and created["recovery"] == "client"
    recording_id = created["id"]
    meta = (s.store.root / recording_id / "meta.json").read_text()
    sidecar = (s.store.root / recording_id / "lease.json").read_text()
    digest = hashlib.sha256(s.recovery.encode()).hexdigest()
    assert digest in meta and digest in sidecar
    assert s.recovery not in meta and s.recovery not in sidecar
    for event in s.bus.events:
        assert_secret_free(event, s.recovery)
    assert s.recovery not in caplog.text


def test_create_replay_and_resume_require_the_same_recovery_capability(tmp_path):
    s = Setup(tmp_path)
    first = s.create(key="same")
    recording_id, lease = first["id"], first["lease_token"]
    wrong = secrets.token_urlsafe(32)

    with pytest.raises(RecorderServiceError) as wrong_create:
        s.create(key="same", recovery=wrong)
    assert wrong_create.value.code == IDEMPOTENCY_CONFLICT and wrong_create.value.status == 409
    with pytest.raises(RecorderServiceError) as wrong_resume:
        s.service.resume("tui", "same", wrong)
    assert wrong_resume.value.code == IDEMPOTENCY_CONFLICT and wrong_resume.value.status == 409

    replay = s.create(key="same")
    assert replay["lease_token"] == lease and replay["tokens_reissued"] is True
    resumed = s.service.resume("tui", "same", s.recovery)
    assert resumed["lease_token"] == lease and resumed["recovery_token"] is None

    s.service.stop(recording_id, lease)
    stopped = s.service.resume("tui", "same", s.recovery)
    assert stopped["replay_state"] == "stopped" and stopped["tokens_reissued"] is False
    assert stopped["lease_token"] is None and stopped["recovery_token"] is None


def test_resume_never_creates_and_withdraws_an_unknown_key(tmp_path):
    s = Setup(tmp_path)
    with pytest.raises(RecorderServiceError) as missing:
        s.service.resume("tui", "ghost", s.recovery)
    assert missing.value.code == CREATE_NOT_FOUND and missing.value.status == 404
    assert s.service.list() == []

    # A create still in flight for the looked-up key must not produce a second recording.
    with pytest.raises(RecorderServiceError) as withdrawn:
        s.create(key="ghost")
    assert withdrawn.value.code == CREATE_WITHDRAWN and withdrawn.value.status == 409
    assert s.service.list() == []

    fresh = s.create(key="fresh")
    assert fresh["active"] is True and len(s.service.list()) == 1


def test_resume_and_create_race_never_duplicates(tmp_path):
    s = Setup(tmp_path)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def lookup() -> None:
        barrier.wait(timeout=5)
        try:
            results["resume"] = s.service.resume("tui", "race", s.recovery)
        except RecorderServiceError as exc:
            results["resume_error"] = exc.code

    def start() -> None:
        barrier.wait(timeout=5)
        try:
            results["create"] = s.create(key="race")
        except RecorderServiceError as exc:
            results["create_error"] = exc.code

    threads = [threading.Thread(target=lookup), threading.Thread(target=start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    if "create_error" in results:
        assert results["create_error"] == CREATE_WITHDRAWN
        assert results["resume_error"] == CREATE_NOT_FOUND
        assert s.service.list() == []
    else:
        created = results["create"]
        resumed = results["resume"]
        assert isinstance(created, dict) and isinstance(resumed, dict)
        assert resumed["id"] == created["id"]
        assert len(s.service.list()) == 1


def test_resume_finds_an_active_recording_and_can_stop_it_at_zero(tmp_path):
    s = Setup(tmp_path)
    first = s.create(key="race")
    resumed = s.service.resume("tui", "race", s.recovery)
    assert resumed["active"] is True and resumed["lease_token"] == first["lease_token"]
    assert resumed["recovery_token"] is None and resumed["tokens_reissued"] is True

    sealed = s.service.stop(first["id"], resumed["lease_token"], 0)
    assert sealed["sealed"] is True and sealed["complete"] is True
    assert sealed["durable_offset"] == 0 and sealed["final_sample_count"] == 0
    assert s.create(stream="s2", client="nsh", key="next")["active"] is True


def test_resume_after_restart_returns_interrupted_without_tokens_then_recovery_seals(tmp_path):
    s = Setup(tmp_path)
    first = s.create(key="restart")
    recording_id = first["id"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), first["lease_token"])

    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    resumed = service.resume("tui", "restart", s.recovery)
    assert resumed["id"] == recording_id and resumed["replay_state"] == "interrupted"
    assert resumed["lease_token"] is None and resumed["recovery_token"] is None
    assert resumed["recoverable"] is True

    sealed = service.recover_stop(recording_id, s.recovery, "tui", 160)
    assert sealed["sealed"] is True and sealed["complete"] is True
    assert sealed["durable_offset"] == 160


def test_withdrawn_lookup_set_is_bounded_and_fails_closed(tmp_path):
    s = Setup(tmp_path)
    for index in range(MAX_WITHDRAWN_KEYS):
        with pytest.raises(RecorderServiceError) as missing:
            s.service.resume("tui", f"gone-{index}", s.recovery)
        assert missing.value.code == CREATE_NOT_FOUND
    assert s.service.list() == []

    # The set is full: another lookup cannot be remembered, so it fails instead of returning 404
    # (which would tell the client it is safe to let a create run).
    with pytest.raises(RecorderServiceError) as full:
        s.service.resume("tui", "overflow", s.recovery)
    assert full.value.code == CREATE_GUARD_FULL and full.value.status == 503

    # Every new create is closed permanently for this process; an already withdrawn key is still
    # refused explicitly.
    with pytest.raises(RecorderServiceError) as blocked:
        s.create(key="brand-new")
    assert blocked.value.code == CREATE_GUARD_FULL and blocked.value.status == 503
    with pytest.raises(RecorderServiceError) as withdrawn:
        s.create(key="gone-0")
    assert withdrawn.value.code == CREATE_WITHDRAWN
    assert s.service.list() == []

    # Time does not clear a withdrawal or the closed guard: a delayed create has no provable bound.
    s.clock.advance(30 * 24 * 60 * 60)
    with pytest.raises(RecorderServiceError) as still_blocked:
        s.create(key="brand-new")
    assert still_blocked.value.code == CREATE_GUARD_FULL
    with pytest.raises(RecorderServiceError) as still_withdrawn:
        s.create(key="gone-0")
    assert still_withdrawn.value.code == CREATE_WITHDRAWN

    # A restart terminates every in-flight connection, so a fresh process starts with no guard.
    clock = Clock(s.clock.now, s.clock.mono)
    restarted = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                                clock=clock, monotonic=clock.monotonic)
    restarted.startup()
    allowed = restarted.create("s1", "tui", "brand-new", recovery=s.recovery)
    assert allowed["active"] is True and len(restarted.list()) == 1


def test_a_withdrawn_key_cannot_create_after_any_delay_in_the_process(tmp_path):
    s = Setup(tmp_path)
    with pytest.raises(RecorderServiceError) as missing:
        s.service.resume("tui", "delayed", s.recovery)
    assert missing.value.code == CREATE_NOT_FOUND

    s.clock.advance(365 * 24 * 60 * 60)
    with pytest.raises(RecorderServiceError) as withdrawn:
        s.create(key="delayed")
    assert withdrawn.value.code == CREATE_WITHDRAWN
    with pytest.raises(RecorderServiceError) as still_missing:
        s.service.resume("tui", "delayed", s.recovery)
    assert still_missing.value.code == CREATE_NOT_FOUND
    assert s.service.list() == []


def test_a_persisted_key_without_a_client_capability_can_never_duplicate(tmp_path):
    store = RecorderStore(tmp_path / "recordings")
    record = store.create(idempotency_sha256=hashlib.sha256(b"unproven").hexdigest())
    clock = Clock()
    service = RecorderService(FakeBus(), FakeStreams(), store, FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    with pytest.raises(RecorderServiceError) as refused:
        service.create("s1", "tui", "unproven", recovery=secrets.token_urlsafe(32))
    assert refused.value.code == CREATE_WITHDRAWN and refused.value.status == 409
    assert [item["id"] for item in service.list()] == [record["id"]]


def test_create_uses_the_registry_client_identity(tmp_path):
    streams = FakeStreams()
    streams.clients["s1"] = "alice"
    s = Setup(tmp_path, streams=streams)
    with pytest.raises(RecorderServiceError) as mismatch:
        s.service.create("s1", "mallory", "key", recovery=s.recovery)
    assert mismatch.value.code == CLIENT_MISMATCH and mismatch.value.status == 409

    created = s.service.create("s1", "alice", "key", recovery=s.recovery)
    recording_id, lease = created["id"], created["lease_token"]
    assert created["owner"] == {"client_id": "alice"}

    # A registry reassignment makes the old lease unusable even though the caller still has its token.
    streams.clients["s1"] = "bob"
    chunk = body(0)
    with pytest.raises(RecorderServiceError) as heartbeat:
        s.service.heartbeat(recording_id, lease)
    assert heartbeat.value.code == NOT_OWNER
    with pytest.raises(RecorderServiceError) as upload:
        s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    assert upload.value.code == NOT_OWNER
    streams.bind("s2", "bob")
    with pytest.raises(RecorderServiceError) as rebind:
        s.service.rebind(recording_id, lease, "s2", "bob")
    assert rebind.value.code == NOT_OWNER


def test_create_refuses_a_connected_stream_without_a_registry_identity(tmp_path):
    streams = FakeStreams()
    streams.clients.pop("s1")
    s = Setup(tmp_path, streams=streams)
    with pytest.raises(RecorderServiceError) as unbound:
        s.service.create("s1", "tui", "key", recovery=s.recovery)
    assert unbound.value.code == STREAM_UNBOUND and unbound.value.status == 409


def test_recovery_binds_a_supplied_stream_to_the_registry_identity(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    s.clock.advance(LEASE_TTL_SECONDS + 0.1)
    assert s.service.get(recording_id)["capture_state"] == "interrupted"

    s.streams.clients["s1"] = "bob"
    with pytest.raises(RecorderServiceError) as wrong_stream:
        s.service.recover_stop(recording_id, s.recovery, "tui", 160, stream_id="s1")
    assert wrong_stream.value.code == NOT_OWNER

    s.streams.bind("s2", "tui")
    sealed = s.service.recover_stop(recording_id, s.recovery, "tui", 160,
                                    stream_id="s2")
    assert sealed["sealed"] is True and sealed["complete"] is True


def test_lease_uses_the_monotonic_clock_not_the_wall_clock(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id = created["id"]
    s.clock.jump_wall(10_000)
    state = s.service.get(recording_id)
    assert state["active"] is True and state["capture_state"] == "recording"
    assert state["lease_expires_in"] > 0

    s.clock.advance(LEASE_TTL_SECONDS + 0.1)
    expired = s.service.get(recording_id)
    assert expired["capture_state"] == "interrupted" and expired["active"] is False
    assert s.bus.of("recorder.state")[-1]["data"]["reason"] == "lease_expired"


def test_stop_intent_survives_a_crash_before_seal(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)

    def crash(_recording_id, final_sample_count=None):
        raise SystemExit("simulated crash between stop intent and seal")

    monkeypatch.setattr(s.store, "seal", crash)
    with pytest.raises(SystemExit):
        s.service.stop(recording_id, lease, 160)

    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    assert service.get(recording_id)["capture_state"] == "interrupted"
    retried = service.stop(recording_id, lease, 160)
    assert retried["sealed"] is True and retried["capture_state"] == "stopped"
    assert retried["complete"] is True


def test_stop_survives_a_crash_after_seal_before_sidecar_revision(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)

    def crash(*_args, **_kwargs):
        raise SystemExit("simulated crash after seal")

    monkeypatch.setattr(s.service, "_publish_state_locked", crash)
    with pytest.raises(SystemExit):
        s.service.stop(recording_id, lease, 160)

    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    state = service.get(recording_id)
    assert state["sealed"] is True and state["complete"] is True
    repeated = service.stop(recording_id, lease, 160)
    assert repeated["sealed"] is True and repeated["complete"] is True


def test_stop_refuses_to_seal_if_the_intent_cannot_be_persisted(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]

    def fail(_record):
        raise OSError("injected sidecar failure")

    monkeypatch.setattr(s.service, "_write_sidecar", fail)
    with pytest.raises(RecorderServiceError) as not_durable:
        s.service.stop(recording_id, lease, 0)
    assert not_durable.value.code == STOP_NOT_DURABLE and not_durable.value.status == 503
    state = s.service.get(recording_id)
    assert state["active"] is True and state["capture_state"] == "recording"
    assert state["sealed"] is False

    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    assert s.service.get(recording_id)["durable_offset"] == 160


def test_create_survives_a_bus_failure_and_retries_its_state_event(tmp_path):
    s = Setup(tmp_path)
    s.bus.fail(1, {"recorder.state"})
    created = s.create()
    assert created["active"] is True and created["lease_token"]
    assert s.service._records[created["id"]].pending_event is not None
    replay = s.create()
    assert replay["id"] == created["id"] and replay["lease_token"] == created["lease_token"]

    s.service.sweep()
    states = s.bus.of("recorder.state")
    assert states and states[-1]["data"]["reason"] == "created"
    assert states[-1]["data"]["id"] == created["id"]


def test_bus_failures_keep_mutations_successful_and_retry_pending_events(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    first = body(0)
    s.service.put_chunk(recording_id, 0, 0, first, digest(first), lease)
    s.clock.advance(1.0)

    s.bus.fail(2, {"recorder.state"})
    second = body(1)
    result = s.service.put_chunk(recording_id, 1, 160, second, digest(second), lease)
    assert result["recording"]["durable_offset"] == 320
    assert s.service._records[recording_id].pending_event is not None
    assert s.service._records[recording_id].pending_event["durable_offset"] == 320

    s.service.sweep()
    states = s.bus.of("recorder.state")
    assert states[-1]["data"]["durable_offset"] == 320
    assert s.service._records[recording_id].pending_event is None

    s.bus.fail(1, {"recorder.state"})
    stopped = s.service.stop(recording_id, lease)
    assert stopped["sealed"] is True and stopped["complete"] is True
    s.service.sweep()
    assert s.bus.of("recorder.state")[-1]["data"]["reason"] == "stopped"


def test_delete_event_failure_is_retried_without_failing_the_delete(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    s.service.stop(recording_id, lease)
    s.bus.fail(1, {"recorder.deleted"})
    assert s.service.delete(recording_id) is True
    assert s.bus.of("recorder.deleted") == []
    s.service.sweep()
    assert [event["data"]["id"] for event in s.bus.of("recorder.deleted")] == [recording_id]


def test_create_rolls_back_if_its_recovery_capability_cannot_be_persisted(tmp_path, monkeypatch):
    s = Setup(tmp_path)

    def fail(_record):
        raise OSError("injected sidecar failure")

    monkeypatch.setattr(s.service, "_write_sidecar", fail)
    with pytest.raises(OSError):
        s.create()
    assert s.service.list() == []
    assert not any((tmp_path / "recordings").glob("*"))


def test_failed_create_rollback_does_not_duplicate_on_an_immediate_retry(tmp_path, monkeypatch):
    s = Setup(tmp_path)

    def fail_sidecar(_record):
        raise OSError("injected sidecar failure")

    def fail_rollback(_recording_id):
        raise OSError("injected rollback failure")

    monkeypatch.setattr(s.service, "_write_sidecar", fail_sidecar)
    monkeypatch.setattr(s.store, "delete", fail_rollback)
    with pytest.raises(OSError):
        s.create(key="half-created")

    # The recording may still be durable, so the key stays replayable in this process and the same
    # capability reaches the same recording rather than a second one.
    replay = s.create(key="half-created")
    assert replay["idempotent"] is True and replay["recovery_token"] is None
    assert len(s.service.list()) == 1
    sealed = s.service.recover_stop(replay["id"], s.recovery, "tui", 0)
    assert sealed["sealed"] is True and sealed["complete"] is True


def test_create_requires_a_connected_stream_and_bounded_identity(tmp_path):
    s = Setup(tmp_path, streams=FakeStreams(set()))
    with pytest.raises(RecorderServiceError) as closed:
        s.create()
    assert closed.value.code == STREAM_CLOSED

    with pytest.raises(RecorderServiceError) as bad:
        s.service.create("has spaces", "tui", "key", recovery=s.recovery)
    assert bad.value.code == "bad_request"
    with pytest.raises(RecorderServiceError) as bad_key:
        s.service.create("s1", "tui", "x" * 300, recovery=s.recovery)
    assert bad_key.value.code == "bad_request"


def test_chunk_ownership_conflicts_and_bounded_validation(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    first = body(0)

    committed = s.service.put_chunk(recording_id, 0, 0, first, digest(first), lease,
                                    stream_id="s1", client_id="tui")
    assert committed["chunk"]["duplicate"] is False
    assert committed["recording"]["durable_offset"] == 160

    duplicate = s.service.put_chunk(recording_id, 0, 0, first, digest(first), lease)
    assert duplicate["chunk"]["duplicate"] is True
    assert duplicate["recording"]["durable_offset"] == 160

    with pytest.raises(RecorderConflict) as conflicting:
        s.service.put_chunk(recording_id, 0, 0, body(9), digest(body(9)), lease)
    assert conflicting.value.code == "chunk_conflict"

    with pytest.raises(RecorderConflict) as gap:
        s.service.put_chunk(recording_id, 2, 320, body(2), digest(body(2)), lease)
    assert gap.value.code == "noncontiguous" and gap.value.details["durable_offset"] == 160

    with pytest.raises(RecorderInvalid) as bad_hash:
        s.service.put_chunk(recording_id, 1, 160, body(1), digest(b"other"), lease)
    assert bad_hash.value.code == "bad_chunk_hash"

    with pytest.raises(RecorderServiceError) as too_large:
        s.service.put_chunk(recording_id, 1, 160, b"\0" * 64_002, "0" * 64, lease)
    assert too_large.value.code == CHUNK_TOO_LARGE and too_large.value.status == 413

    with pytest.raises(RecorderServiceError) as odd:
        s.service.put_chunk(recording_id, 1, 160, b"\0" * 3, "0" * 64, lease)
    assert odd.value.code == "bad_request"

    with pytest.raises(RecorderServiceError) as wrong_token:
        s.service.put_chunk(recording_id, 1, 160, body(1), digest(body(1)), "x" * 32)
    assert wrong_token.value.code == INVALID_LEASE and wrong_token.value.status == 403

    with pytest.raises(RecorderServiceError) as wrong_client:
        s.service.put_chunk(recording_id, 1, 160, body(1), digest(body(1)), lease, client_id="nsh")
    assert wrong_client.value.code == NOT_OWNER

    with pytest.raises(RecorderServiceError) as wrong_stream:
        s.service.put_chunk(recording_id, 1, 160, body(1), digest(body(1)), lease, stream_id="s2")
    assert wrong_stream.value.code == NOT_OWNER

    for event in s.bus.of("recorder.state"):
        assert_secret_free(event, lease, s.recovery, "s1")
    snapshot = s.service.get(recording_id)
    assert "lease_token" not in snapshot and "recovery_token" not in snapshot


def test_active_stop_and_heartbeat_require_the_owner_and_lease(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]

    with pytest.raises(RecorderServiceError) as stop_token:
        s.service.stop(recording_id, "q" * 32)
    assert stop_token.value.code == INVALID_LEASE and stop_token.value.status == 403
    with pytest.raises(RecorderServiceError) as stop_client:
        s.service.stop(recording_id, lease, client_id="nsh")
    assert stop_client.value.code == NOT_OWNER
    with pytest.raises(RecorderServiceError) as stop_stream:
        s.service.stop(recording_id, lease, stream_id="s2")
    assert stop_stream.value.code == NOT_OWNER
    with pytest.raises(RecorderServiceError) as heartbeat_token:
        s.service.heartbeat(recording_id, "q" * 32)
    assert heartbeat_token.value.code == INVALID_LEASE
    with pytest.raises(RecorderServiceError) as heartbeat_stream:
        s.service.heartbeat(recording_id, lease, stream_id="s2")
    assert heartbeat_stream.value.code == NOT_OWNER

    still_active = s.service.get(recording_id)
    assert still_active["active"] is True and still_active["capture_state"] == "recording"


def test_repeated_stop_is_idempotent_and_conflicting_counts_are_rejected(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)

    stopped = s.service.stop(recording_id, lease, 160, stream_id="s1", client_id="tui")
    assert stopped["capture_state"] == "stopped" and stopped["sealed"] is True
    assert stopped["complete"] is True and stopped["active"] is False
    assert stopped["final_sample_count"] == 160 and stopped["missing_samples"] == 0

    again = s.service.stop(recording_id, lease, 160)
    assert again["sealed"] is True and again["complete"] and again["durable_offset"] == 160
    no_count = s.service.stop(recording_id, lease)
    assert no_count["final_sample_count"] == 160

    with pytest.raises(RecorderServiceError) as different:
        s.service.stop(recording_id, lease, 999)
    assert different.value.code == "final_count_conflict"
    with pytest.raises(RecorderServiceError) as wrong_lease:
        s.service.stop(recording_id, "y" * 32, 160)
    assert wrong_lease.value.code == INVALID_LEASE

    with pytest.raises(RecorderServiceError) as heartbeat:
        s.service.heartbeat(recording_id, lease)
    assert heartbeat.value.code == NOT_ACTIVE
    assert s.bus.of("recorder.state")[-1]["data"]["reason"] == "stopped"


def test_stop_and_upload_race_is_serialized_and_consistent(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    first = body(0)
    s.service.put_chunk(recording_id, 0, 0, first, digest(first), lease)
    second = body(1)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def upload() -> None:
        barrier.wait()
        try:
            s.service.put_chunk(recording_id, 1, 160, second, digest(second), lease)
        except RecorderError as exc:  # expected when the stop wins
            errors.append(exc)

    def stop() -> None:
        barrier.wait()
        s.service.stop(recording_id, lease)

    uploader = threading.Thread(target=upload)
    stopper = threading.Thread(target=stop)
    uploader.start()
    stopper.start()
    uploader.join(5)
    stopper.join(5)
    assert not uploader.is_alive() and not stopper.is_alive()

    state = s.service.get(recording_id)
    assert state["sealed"] is True and state["capture_state"] == "stopped"
    assert state["final_sample_count"] == state["durable_offset"]
    if errors:
        assert len(errors) == 1
        assert isinstance(errors[0], RecorderConflict) and errors[0].code == "recording_sealed"
        assert state["durable_offset"] == 160
    else:
        assert state["durable_offset"] == 320


def test_lease_expiry_interrupts_capture_and_blocks_the_old_lease(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]

    s.clock.advance(LEASE_TTL_SECONDS + 0.1)
    state = s.service.get(recording_id)
    assert state["capture_state"] == "interrupted" and state["active"] is False
    assert state["lease_expires_in"] is None
    event = s.bus.of("recorder.state")[-1]
    assert event["data"]["capture_state"] == "interrupted"
    assert event["data"]["reason"] == "lease_expired"
    assert_secret_free(event, lease, s.recovery, "s1")

    chunk = body(0)
    with pytest.raises(RecorderServiceError) as upload:
        s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    assert upload.value.code == LEASE_EXPIRED
    with pytest.raises(RecorderServiceError) as heartbeat:
        s.service.heartbeat(recording_id, lease)
    assert heartbeat.value.code == LEASE_EXPIRED
    with pytest.raises(RecorderServiceError) as stop:
        s.service.stop(recording_id, lease)
    assert stop.value.code == LEASE_EXPIRED

    replacement = s.create(stream="s2", client="nsh", key="key-2")
    assert replacement["active"] is True and replacement["id"] != recording_id


def test_detach_grace_rebind_and_expiry(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]

    assert s.streams.detach("s1") is None  # the registered listener runs
    detached = s.service.get(recording_id)
    assert detached["active"] is True and detached["stream_detached"] is True

    chunk = body(0)
    with pytest.raises(RecorderServiceError) as upload:
        s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    assert upload.value.code == STREAM_DETACHED

    s.clock.advance(10)
    s.streams.bind("s2", "tui")
    rebound = s.service.rebind(recording_id, lease, "s2", "tui")
    assert rebound["active"] is True and rebound["stream_detached"] is False
    s.service.heartbeat(recording_id, lease, stream_id="s2", client_id="tui")
    accepted = s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease, stream_id="s2")
    assert accepted["recording"]["durable_offset"] == 160

    s.streams.detach("s2")
    s.clock.advance(19)
    assert s.service.get(recording_id)["active"] is True
    s.clock.advance(DETACH_GRACE_SECONDS)
    expired = s.service.sweep()
    assert expired and expired[-1]["capture_state"] == "interrupted"
    assert s.bus.of("recorder.state")[-1]["data"]["reason"] == "stream_detached"

    with pytest.raises(RecorderServiceError) as late_rebind:
        s.service.rebind(recording_id, lease, "s3", "tui")
    assert late_rebind.value.code == LEASE_EXPIRED


def test_recovery_requires_sealing_and_is_bounded_by_the_sealed_offset(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    first = body(0)
    s.service.put_chunk(recording_id, 0, 0, first, digest(first), lease)
    s.clock.advance(LEASE_TTL_SECONDS + 0.1)
    assert s.service.get(recording_id)["capture_state"] == "interrupted"

    second = body(1)
    with pytest.raises(RecorderServiceError) as unsealed:
        s.service.recover_upload(recording_id, 1, 160, second, digest(second),
                                 s.recovery, "tui")
    assert unsealed.value.code == RECOVERY_NOT_SEALED

    sealed = s.service.recover_stop(recording_id, s.recovery, "tui", 320)
    assert sealed["sealed"] is True and sealed["capture_state"] == "stopped"
    assert sealed["complete"] is False and sealed["missing_samples"] == 160

    with pytest.raises(RecorderServiceError) as wrong_token:
        s.service.recover_upload(recording_id, 1, 160, second, digest(second), "z" * 32, "tui")
    assert wrong_token.value.code == INVALID_LEASE
    with pytest.raises(RecorderServiceError) as wrong_client:
        s.service.recover_upload(recording_id, 1, 160, second, digest(second),
                                 s.recovery, "nsh")
    assert wrong_client.value.code == NOT_OWNER

    recovered = s.service.recover_upload(recording_id, 1, 160, second, digest(second),
                                         s.recovery, "tui")
    assert recovered["recording"]["durable_offset"] == 320
    assert recovered["recording"]["complete"] is True

    beyond = body(2)
    with pytest.raises(RecorderConflict) as outside:
        s.service.recover_upload(recording_id, 2, 320, beyond, digest(beyond),
                                 s.recovery, "tui")
    assert outside.value.code == "beyond_sealed"

    # Recovery never renews capture: the old lease stays dead and a fresh recording is possible.
    with pytest.raises(RecorderServiceError) as heartbeat:
        s.service.heartbeat(recording_id, lease)
    assert heartbeat.value.code == NOT_ACTIVE
    assert s.create(stream="s2", client="nsh", key="key-2")["active"] is True


def test_stop_with_unreceived_audio_can_be_completed_by_recovery(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    first = body(0)
    s.service.put_chunk(recording_id, 0, 0, first, digest(first), lease)

    stopped = s.service.stop(recording_id, lease, 320)
    assert stopped["sealed"] is True and stopped["complete"] is False
    second = body(1)
    result = s.service.recover_upload(recording_id, 1, 160, second, digest(second),
                                      s.recovery, "tui")
    assert result["recording"]["complete"] is True
    assert result["chunk"]["duplicate"] is False


def test_delete_refuses_active_capture_then_cancels_jobs_and_publishes(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    with pytest.raises(RecorderServiceError) as active:
        s.service.delete(recording_id)
    assert active.value.code == RECORDING_ACTIVE

    s.service.stop(recording_id, lease)
    assert s.service.delete(recording_id) is True
    assert s.scheduler.cancelled == [recording_id]
    with pytest.raises(RecorderNotFound):
        s.service.get(recording_id)
    deleted = s.bus.of("recorder.deleted")
    assert len(deleted) == 1 and deleted[0]["session_id"] is None
    assert_secret_free(deleted[0], lease, s.recovery, "s1")
    assert s.service.delete(recording_id) is False


def test_delete_can_remove_an_unreadable_recording(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    s.service.stop(recording_id, lease)
    (s.store.root / recording_id / "meta.json").write_text("not json\n")
    assert s.service.delete(recording_id) is True
    assert not (s.store.root / recording_id).exists()


def test_pending_state_event_survives_restart_from_the_durable_outbox(tmp_path):
    s = Setup(tmp_path)
    created = s.service.create("s1", "tui", None, recovery=s.recovery)
    recording_id, lease = created["id"], created["lease_token"]
    s.bus.fail(1, {"recorder.state"})
    stopped = s.service.stop(recording_id, lease)
    assert stopped["sealed"] is True
    assert [event["data"]["reason"] for event in s.bus.of("recorder.state")] == ["created"]
    sidecar = s.service._read_sidecar(recording_id)
    assert sidecar is not None and isinstance(sidecar.get("pending_event"), dict)

    bus = FakeBus()
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(bus, FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    events = bus.of("recorder.state")
    assert [event["data"]["id"] for event in events] == [recording_id]
    assert events[-1]["data"]["reason"] == "stopped"
    assert service._read_sidecar(recording_id)["pending_event"] is None


def test_state_outbox_double_failure_is_snapshot_recovered_not_event_durable(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    created = s.service.create("s1", "tui", None, recovery=s.recovery)
    recording_id, lease = created["id"], created["lease_token"]
    s.clock.advance(1.0)

    def fail_sidecar(_record):
        raise OSError("injected sidecar failure")

    monkeypatch.setattr(s.service, "_write_sidecar", fail_sidecar)
    s.bus.fail(1, {"recorder.state"})
    chunk = body(0)
    result = s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    assert result["recording"]["durable_offset"] == 160
    assert [event["data"]["reason"] for event in s.bus.of("recorder.state")] == ["created"]
    sidecar = s.service._read_sidecar(recording_id)
    assert sidecar is not None and sidecar.get("pending_event") is None

    # Do not call service.get: it would sweep the in-memory event. The store/snapshot is authoritative.
    assert s.store.get(recording_id).durable_offset == 160
    bus = FakeBus()
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(bus, FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    recovered = bus.of("recorder.state")
    assert any(event["data"]["durable_offset"] == 160 for event in recovered)
    assert all(event["data"].get("reason") != "chunk" for event in recovered)


def test_deletion_tombstone_survives_restart_and_publishes_once(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    s.service.stop(recording_id, lease)
    s.bus.fail(1, {"recorder.deleted"})
    assert s.service.delete(recording_id) is True
    assert s.bus.of("recorder.deleted") == []
    outbox = s.store.root / ".outbox"
    assert [path.name for path in outbox.iterdir()] == [f"{recording_id}.json"]

    bus = FakeBus()
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(bus, FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    assert [event["data"]["id"] for event in bus.of("recorder.deleted")] == [recording_id]
    assert list(outbox.iterdir()) == []

    service.sweep()
    assert [event["data"]["id"] for event in bus.of("recorder.deleted")] == [recording_id]


def test_deletion_outbox_requires_a_private_owned_directory(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    s.service.stop(recording_id, lease)
    outbox = s.store.root / ".outbox"
    outbox.mkdir(mode=0o700)
    os.chmod(outbox, 0o755)

    with pytest.raises(RecorderSecurityError) as unsafe:
        s.service.delete(recording_id)
    assert unsafe.value.code == "directory_mode"
    assert s.store.get(recording_id).sealed is True


def test_deletion_outbox_is_bounded_and_refuses_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_DELETED_TOMBSTONES", 1)
    s = Setup(tmp_path)
    first = s.create()
    s.service.stop(first["id"], first["lease_token"])
    second = s.create(key="key-2")
    s.service.stop(second["id"], second["lease_token"])

    s.bus.fail(2, {"recorder.deleted"})
    assert s.service.delete(first["id"]) is True
    with pytest.raises(RecorderServiceError) as full:
        s.service.delete(second["id"])
    assert full.value.code == DELETE_NOT_DURABLE and full.value.status == 503
    assert len(list((s.store.root / ".outbox").iterdir())) == 1
    assert s.store.get(second["id"]).sealed is True


def test_delete_refuses_to_delete_if_its_tombstone_cannot_be_persisted(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    s.service.stop(recording_id, lease)

    def fail(_recording_id, _generation):
        raise OSError("injected tombstone failure")

    monkeypatch.setattr(s.service, "_write_tombstone", fail)
    with pytest.raises(RecorderServiceError) as not_durable:
        s.service.delete(recording_id)
    assert not_durable.value.code == DELETE_NOT_DURABLE and not_durable.value.status == 503
    assert s.service.get(recording_id)["sealed"] is True
    assert not (s.store.root / ".outbox").exists()


def test_state_events_are_coalesced_and_bounded(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    for sequence in range(6):
        s.clock.advance(0.1)
        chunk = body(sequence, samples=1600)
        s.service.put_chunk(recording_id, sequence, sequence * 1600, chunk, digest(chunk), lease)

    states = s.bus.of("recorder.state")
    assert 2 <= len(states) <= 3  # created + at most one coalesced progress event
    assert states[-1]["data"]["durable_offset"] > 0
    last = states[-1]["data"]
    assert set(last) >= {"id", "generation", "revision", "capture_state", "durable_offset",
                         "transcription_state", "holes", "complete"}
    revisions = [event["data"]["revision"] for event in states]
    assert revisions == sorted(revisions) and len(set(revisions)) == len(revisions)
    for event in states:
        assert_secret_free(event, lease, s.recovery, "s1")

    stopped = s.service.stop(recording_id, lease)
    assert stopped["sealed"] is True
    assert s.bus.of("recorder.state")[-1]["data"]["reason"] == "stopped"


def test_segment_events_are_bounded_revisioned_and_always_live_only(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id = created["id"]
    for revision, status in enumerate(("provisional", "final", "error"), start=1):
        published = s.service.publish_segment(
            recording_id, segment_id=1, revision=revision, start=0, end=160,
            status=status, text=f"hello-{status}")
        assert published is not None
    segments = s.bus.of("recorder.segment")
    assert [event["durable"] for event in segments] == [False, False, False]
    assert all(event["session_id"] is None for event in segments)
    assert [event["data"]["status"] for event in segments] == ["provisional", "final", "error"]
    assert s.service.publish_segment(
        recording_id, segment_id=1, revision=4, start=0, end=160,
        status="final", text="stale", generation=99) is None
    with pytest.raises(RecorderServiceError) as bad_status:
        s.service.publish_segment(
            recording_id, segment_id=1, revision=4, start=0, end=160,
            status="made_up", text="no")
    assert bad_status.value.code == "bad_request"
    with pytest.raises(RecorderServiceError) as bad_id:
        s.service.publish_segment(
            recording_id, segment_id="1", revision=5, start=0, end=160,
            status="final", text="no")
    assert bad_id.value.code == "bad_request"


def test_segment_text_never_enters_the_durable_event_log(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    texts = {
        "provisional": "live provisional words",
        "final": "stable final words",
        "error": "failed transcription words",
    }
    for revision, (status, text) in enumerate(texts.items(), start=1):
        s.service.publish_segment(
            recording_id, segment_id=1, revision=revision, start=0, end=160,
            status=status, text=text)
    live = s.bus.of("recorder.segment")
    assert [event["durable"] for event in live] == [False, False, False]
    assert [event["data"]["text"] for event in live] == list(texts.values())

    s.service.stop(recording_id, lease)
    s.service.delete(recording_id)
    durable = [event for event in s.bus.events if event["durable"]]
    assert durable  # state/deleted events still exercise the durable path
    serialized = json.dumps(durable)
    for text in texts.values():
        assert text not in serialized


def test_restart_marks_an_active_capture_interrupted_and_keeps_recovery(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    first = body(0)
    s.service.put_chunk(recording_id, 0, 0, first, digest(first), lease)

    # A new process has its own store/service objects over the same scratch state.
    restarted_store = RecorderStore(s.store.root)
    restarted_bus = FakeBus()
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(restarted_bus, FakeStreams(), restarted_store, FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    recovered = service.startup()
    assert [item["id"] for item in recovered] == [recording_id]
    state = service.get(recording_id)
    assert state["capture_state"] == "interrupted" and state["active"] is False
    assert state["durable_offset"] == 160
    assert restarted_bus.of("recorder.state")[-1]["data"]["reason"] == "recovered"

    second = body(1)
    with pytest.raises(RecorderServiceError) as stale:
        service.put_chunk(recording_id, 1, 160, second, digest(second), lease)
    assert stale.value.code == LEASE_EXPIRED

    sealed = service.recover_stop(recording_id, s.recovery, "tui", 320)
    assert sealed["complete"] is False
    recovered_chunk = service.recover_upload(recording_id, 1, 160, second, digest(second),
                                             s.recovery, "tui")
    assert recovered_chunk["recording"]["complete"] is True


def test_restart_keeps_stopped_state_and_repeated_stop_idempotent(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    s.service.stop(recording_id, lease, 160)

    restarted_store = RecorderStore(s.store.root)
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), restarted_store, FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    assert service.startup() == []
    state = service.get(recording_id)
    assert state["capture_state"] == "stopped" and state["sealed"] is True and state["complete"] is True

    repeated = service.stop(recording_id, lease, 160)
    assert repeated["sealed"] is True and repeated["complete"] is True
    with pytest.raises(RecorderServiceError) as different:
        service.stop(recording_id, lease, 999)
    assert different.value.code == "final_count_conflict"

    new = service.create("s2", "nsh", "after-restart", recovery=s.recovery)
    assert new["active"] is True and new["id"] != recording_id


def test_missing_recordings_invalid_ids_and_bounded_lists(tmp_path):
    s = Setup(tmp_path)
    missing = "a" * 32
    with pytest.raises(RecorderNotFound):
        s.service.get(missing)
    with pytest.raises(RecorderNotFound):
        s.service.heartbeat(missing, "x" * 32)
    with pytest.raises(RecorderNotFound):
        s.service.stop(missing, "x" * 32)
    assert s.service.delete(missing) is False
    with pytest.raises(RecorderInvalid):
        s.service.get("../escape")

    with pytest.raises(RecorderServiceError) as bad_limit:
        s.service.list(limit=0)
    assert bad_limit.value.code == "bad_request"

    for index in range(3):
        created = s.service.create("s1", "tui", f"key-{index}", recovery=s.recovery)
        s.service.stop(created["id"], created["lease_token"])
    listed = s.service.list(limit=10_000)
    assert len(listed) == 3 and len(listed) <= 50
    assert all(item["owner"] == {"client_id": "tui"} for item in listed)
    assert all("lease_token" not in item and "recovery_token" not in item for item in listed)
    assert "s1" not in json.dumps(listed)


def test_scheduler_hooks_fire_only_after_successful_commits_and_stop(tmp_path):
    hooks = FakeScheduler()
    s = Setup(tmp_path, scheduler=hooks)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    assert ("audio_committed", (recording_id, 1, 160)) in [
        (name, args) for name, args, _thread in hooks.calls]

    before = len(hooks.calls)
    with pytest.raises(RecorderServiceError):
        s.service.put_chunk(recording_id, 1, 160, chunk, digest(chunk), "x" * 32)
    assert len(hooks.calls) == before

    s.service.stop(recording_id, lease)
    assert ("capture_ended", (recording_id, 1, 160, "stopped")) in [
        (name, args) for name, args, _thread in hooks.calls]


def test_scheduler_hooks_cover_expiry_recovery_and_startup(tmp_path):
    hooks = FakeScheduler()
    s = Setup(tmp_path, scheduler=hooks)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    s.clock.advance(LEASE_TTL_SECONDS + 0.1)
    s.service.sweep()
    assert ("capture_ended", (recording_id, 1, 160, "interrupted")) in [
        (name, args) for name, args, _thread in hooks.calls]

    calls_before = len([call for call in hooks.calls if call[0] == "audio_committed"])
    s.service.recover_stop(recording_id, s.recovery, "tui", 320)
    second = body(1)
    s.service.recover_upload(recording_id, 1, 160, second, digest(second),
                             s.recovery, "tui")
    assert len([call for call in hooks.calls if call[0] == "audio_committed"]) == calls_before + 1
    assert [args[3] for name, args, _thread in hooks.calls if name == "capture_ended"][-1] == "stopped"

    restart_hooks = FakeScheduler()
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), restart_hooks,
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    recovered = [args for name, args, _thread in restart_hooks.calls if name == "recovered"]
    assert recovered == [([],)]  # stopped and complete; nothing needs transcription recovery


def test_scheduler_recovered_reports_interrupted_recordings(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)

    hooks = FakeScheduler()
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), hooks,
                              clock=clock, monotonic=clock.monotonic)
    service.startup()
    assert [args for name, args, _thread in hooks.calls if name == "recovered"] == [([recording_id],)]


def test_stop_retry_resignals_capture_ended_after_hook_failure(tmp_path):
    hooks = FakeScheduler()
    s = Setup(tmp_path, scheduler=hooks)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)

    hooks.capture_failures = 1
    first = s.service.stop(recording_id, lease)
    assert first["sealed"] is True
    hooks.capture_failures = 0
    retry = s.service.stop(recording_id, lease)
    assert retry["sealed"] is True

    captures = [args for name, args, _thread in hooks.calls if name == "capture_ended"]
    assert captures == [
        (recording_id, 1, 160, "stopped"),
        (recording_id, 1, 160, "stopped"),
    ]


def test_recover_stop_retry_resignals_capture_ended_after_hook_failure(tmp_path):
    hooks = FakeScheduler()
    s = Setup(tmp_path, scheduler=hooks)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)
    s.clock.advance(LEASE_TTL_SECONDS + 0.1)
    s.service.sweep()

    hooks.capture_failures = 1
    first = s.service.recover_stop(recording_id, s.recovery, "tui", 320)
    assert first["sealed"] is True
    hooks.capture_failures = 0
    retry = s.service.recover_stop(recording_id, s.recovery, "tui", 320)
    assert retry["sealed"] is True

    captures = [args for name, args, _thread in hooks.calls if name == "capture_ended"]
    assert captures[-2:] == [
        (recording_id, 1, 320, "stopped"),
        (recording_id, 1, 320, "stopped"),
    ]


def test_raising_scheduler_never_fails_capture_or_stop(tmp_path):
    hooks = FakeScheduler(raise_hooks=True)
    s = Setup(tmp_path, scheduler=hooks)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    chunk = body(0)
    assert s.service.put_chunk(recording_id, 0, 0, chunk, digest(chunk), lease)["chunk"]["durable_offset"] == 160
    assert s.service.stop(recording_id, lease)["sealed"] is True
    assert s.service.delete(recording_id) is True


def test_transcription_progress_is_bounded_public_and_text_free(tmp_path):
    s = Setup(tmp_path)
    created = s.create()
    recording_id = created["id"]
    progress = {
        "processed_through": 160,
        "lag_samples": 0,
        "preview_through": 160,
        "pending_finals": 1,
        "failed": 0,
        "transcript_revision": 7,
        "reason": "voice_busy",
    }
    updated = s.service.set_transcription_progress(recording_id, "catching_up", progress)
    assert updated["transcription"] == {"state": "catching_up", "progress": progress}
    assert updated["transcription_state"] == "catching_up"
    assert updated["transcript_revision"] == 7
    event = s.bus.of("recorder.state")[-1]["data"]
    assert event["transcription"] == {"state": "catching_up", "progress": progress}
    assert event["transcript_revision"] == 7
    assert "text" not in json.dumps(event)

    preserved = s.service.set_transcription_state(recording_id, "live")
    assert preserved["transcription"]["progress"] == progress
    assert preserved["transcription_state"] == "live"

    for bad in (
        {"unknown": 1},
        {"lag_samples": -1},
        {"lag_samples": 10**20},
        {"reason": "contains spaces"},
    ):
        with pytest.raises(RecorderServiceError) as invalid:
            s.service.set_transcription_progress(recording_id, "live", bad)
        assert invalid.value.code == "bad_request"
    with pytest.raises(RecorderServiceError):
        s.service.set_transcription_progress(recording_id, "made_up", {})


def test_enumerate_recordings_discovers_older_stopped_recordings_after_restart(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    created = s.create()
    recording_id, lease = created["id"], created["lease_token"]
    s.service.stop(recording_id, lease)

    def forbidden_list(*_args, **_kwargs):
        raise AssertionError("enumeration must not use the capped public list")

    monkeypatch.setattr(s.store, "list", forbidden_list)
    clock = Clock(s.clock.now, s.clock.mono)
    service = RecorderService(FakeBus(), FakeStreams(), RecorderStore(s.store.root), FakeScheduler(),
                              clock=clock, monotonic=clock.monotonic)
    page = service.enumerate_recordings(None, 10)
    assert page["next_after"] is None
    assert page["recordings"] == [{
        "id": recording_id,
        "created_at": page["recordings"][0]["created_at"],
        "generation": 1,
        "capture_state": "stopped",
        "sealed": True,
        "final_sample_count": 0,
        "durable_offset": 0,
        "durable_bytes": 0,
        "chunks": 0,
        "holes": 0,
        "complete": True,
        "revision": page["recordings"][0]["revision"],
    }]
    assert "lease_token" not in json.dumps(page) and "stream_id" not in json.dumps(page)


def test_enumerate_recordings_is_cursor_paged_and_reports_corrupt_entries(tmp_path):
    s = Setup(tmp_path)
    ids: list[str] = []
    for index in range(3):
        created = s.create(key=f"key-{index}")
        ids.append(created["id"])
        s.service.stop(created["id"], created["lease_token"])
    (s.store.root / ids[1] / "meta.json").write_text("broken\n")

    seen: list[str] = []
    errors: list[dict] = []
    after = None
    while True:
        page = s.service.enumerate_recordings(after, 1)
        assert len(page["recordings"]) <= 1
        for item in page["recordings"]:
            if "error" in item:
                errors.append(item)
            else:
                seen.append(item["id"])
                assert item["capture_state"] == "stopped"
        after = page["next_after"]
        if after is None:
            break
    assert seen == sorted([ids[0], ids[2]])
    assert errors == [{"id": ids[1], "error": "bad_json"}]

    with pytest.raises(RecorderServiceError):
        s.service.enumerate_recordings("not-an-id", 10)
    with pytest.raises(RecorderServiceError):
        s.service.enumerate_recordings(None, 0)
