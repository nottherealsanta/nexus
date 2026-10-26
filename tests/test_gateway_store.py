"""Gateway state: enrollment codes, device tokens, scopes, host snapshots, locking."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from nexus.gateway.store import (
    DEFAULT_CODE_TTL,
    MAX_DEVICES,
    MAX_PENDING,
    SCOPES,
    GatewayError,
    GatewayStore,
    PairingError,
    hash_secret,
    host_id_for,
    normalize_code,
    parse_scopes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class Clock:
    """Starts at the real time so subprocess stores (real clock) agree on expiry."""

    def __init__(self, now: float | None = None) -> None:
        self.now = time.time() if now is None else now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> GatewayStore:
    return GatewayStore(tmp_path / "home" / "gateway", clock=clock)


def workspace(tmp_path: Path, name: str = "ws") -> Path:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


def state_text(store: GatewayStore) -> str:
    return (store.root / "state.json").read_text("utf-8")


# -- workspaces ------------------------------------------------------------------


def test_allow_registers_an_existing_directory_under_the_daemon_socket_digest(store, tmp_path):
    entry = store.allow(workspace(tmp_path))
    assert entry.host_id == host_id_for(workspace(tmp_path).resolve())
    assert len(entry.host_id) == 16
    assert entry.workspace == str(workspace(tmp_path).resolve())
    assert store.hosts() == [entry]
    assert store.host(entry.host_id) == entry
    assert store.host("0" * 16) is None


def test_allow_is_idempotent_and_refuses_missing_paths_and_files(store, tmp_path):
    first = store.allow(workspace(tmp_path))
    assert store.allow(workspace(tmp_path)) == first
    assert len(store.hosts()) == 1
    with pytest.raises(GatewayError):
        store.allow(tmp_path / "missing")
    plain = tmp_path / "file.txt"
    plain.write_text("x")
    with pytest.raises(GatewayError):
        store.allow(plain)
    assert len(store.hosts()) == 1


def test_disallow_by_id_or_path_and_unknown_target(store, tmp_path):
    ws = workspace(tmp_path)
    entry = store.allow(ws)
    assert store.disallow(entry.host_id) == entry
    assert store.hosts() == []
    store.allow(ws)
    assert store.disallow(str(ws)).host_id == entry.host_id
    assert store.disallow(entry.host_id) is None
    assert store.disallow(str(tmp_path / "never-allowed")) is None


def test_host_cap(store, tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.gateway.store.MAX_HOSTS", 2)
    store.allow(workspace(tmp_path, "a"))
    store.allow(workspace(tmp_path, "b"))
    with pytest.raises(GatewayError):
        store.allow(workspace(tmp_path, "c"))
    store.allow(workspace(tmp_path, "a"))  # re-allowing an existing host is not growth


# -- state file --------------------------------------------------------------------


def test_state_lives_in_a_private_directory_and_file(store, tmp_path):
    store.allow(workspace(tmp_path))
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.root / "state.json").st_mode) == 0o600
    assert json.loads(state_text(store))["version"] == 1


def test_a_loose_directory_is_tightened(tmp_path, clock):
    root = tmp_path / "gw"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    GatewayStore(root, clock=clock).allow(workspace(tmp_path))
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


def test_a_symlinked_state_directory_is_refused(tmp_path, clock):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(GatewayError):
        GatewayStore(link, clock=clock).allow(workspace(tmp_path))


def test_group_readable_or_malformed_state_fails_closed(store, tmp_path):
    store.allow(workspace(tmp_path))
    code, _ = store.create_pairing()
    _, token = store.redeem(code)
    path = store.root / "state.json"

    os.chmod(path, 0o644)
    assert store.authenticate(token) is None
    with pytest.raises(GatewayError):
        store.hosts()
    os.chmod(path, 0o600)
    assert store.authenticate(token) is not None

    path.write_text("{not json")
    fresh = GatewayStore(store.root)
    assert fresh.authenticate(token) is None
    with pytest.raises(GatewayError):
        fresh.hosts()
    path.write_text(json.dumps({"version": 1, "gateway_id": "x", "hosts": {"zz": {}}, "devices": {}, "pending": {}}))
    with pytest.raises(GatewayError):
        GatewayStore(store.root).hosts()


def test_gateway_id_is_created_once_and_stable(store, tmp_path):
    first = store.gateway_id
    assert first.startswith("gw_")
    assert GatewayStore(store.root).gateway_id == first
    store.allow(workspace(tmp_path))
    assert store.gateway_id == first


# -- enrollment codes ---------------------------------------------------------------


def test_pairing_needs_an_allowed_workspace(store):
    with pytest.raises(GatewayError, match="allow"):
        store.create_pairing()


def test_code_is_valid_for_120_seconds_by_default(store, tmp_path, clock):
    store.allow(workspace(tmp_path))
    started = clock.now
    code, expires_at = store.create_pairing()
    assert DEFAULT_CODE_TTL == 120.0
    assert expires_at == started + 120.0
    assert store.pending_count() == 1
    clock.now = started + 119.9
    assert store.pending_count() == 1
    clock.now = started + 120.0
    assert store.pending_count() == 0
    with pytest.raises(PairingError):
        store.redeem(code)


def test_a_code_is_single_use(store, tmp_path):
    store.allow(workspace(tmp_path))
    code, _ = store.create_pairing(name="phone")
    device, token = store.redeem(code, "Santa's iPhone")
    assert device.name == "Santa's iPhone"
    assert store.authenticate(token) == device
    with pytest.raises(PairingError):
        store.redeem(code)
    assert len(store.devices()) == 1


def test_codes_are_six_digits_and_unique_while_pending(store, tmp_path):
    store.allow(workspace(tmp_path))
    codes = [store.create_pairing()[0] for _ in range(MAX_PENDING)]
    assert all(len(code) == 6 and code.isdigit() for code in codes)
    assert len(set(codes)) == MAX_PENDING


def test_code_format_and_normalization(store, tmp_path):
    store.allow(workspace(tmp_path))
    code, _ = store.create_pairing()
    groups = code.split("-")
    assert all(len(group) == 5 for group in groups[:-1])
    assert code == code.upper()
    assert normalize_code(" " + code.lower().replace("-", " ") + "\n") == code.replace("-", "")
    device, _ = store.redeem(code.lower().replace("-", " "))
    assert device.device_id.startswith("dev_")


def test_wrong_codes_fail_without_consuming_the_real_one(store, tmp_path):
    store.allow(workspace(tmp_path))
    code, _ = store.create_pairing()
    for bad in ("", "AAAAA-AAAAA", "x" * 500, code[:-1] + ("A" if code[-1] != "A" else "B")):
        with pytest.raises(PairingError):
            store.redeem(bad)
    with pytest.raises(PairingError):
        store.redeem(None)  # type: ignore[arg-type]
    store.redeem(code)


def test_only_hashes_reach_disk(store, tmp_path):
    store.allow(workspace(tmp_path))
    code, _ = store.create_pairing()
    assert normalize_code(code) not in state_text(store)
    assert code not in state_text(store)
    assert hash_secret(normalize_code(code)) in state_text(store)

    _, token = store.redeem(code)
    text = state_text(store)
    assert token not in text
    assert normalize_code(code) not in text
    assert hash_secret(token) in text
    assert len(token) >= 40
    for name in os.listdir(store.root):  # no temp file keeps a copy
        assert token not in (store.root / name).read_text("utf-8", errors="ignore")


def test_redeem_purges_expired_pending_codes(store, tmp_path, clock):
    store.allow(workspace(tmp_path))
    stale, _ = store.create_pairing()
    clock.now += 500
    live, _ = store.create_pairing()
    assert json.loads(state_text(store))["pending"].keys() == {hash_secret(normalize_code(live))}
    with pytest.raises(PairingError):
        store.redeem(stale)


def test_code_ttl_and_device_ttl_bounds(store, tmp_path):
    store.allow(workspace(tmp_path))
    for bad_ttl in (0, 91, -1):
        with pytest.raises(GatewayError):
            store.create_pairing(ttl_days=bad_ttl)
    for bad_code_ttl in (0.5, 601, -5):
        with pytest.raises(GatewayError):
            store.create_pairing(code_ttl=bad_code_ttl)
    store.create_pairing(ttl_days=1, code_ttl=1)
    store.create_pairing(ttl_days=90, code_ttl=600)


def test_pending_and_device_caps(store, tmp_path):
    store.allow(workspace(tmp_path))
    codes = [store.create_pairing()[0] for _ in range(MAX_PENDING)]
    with pytest.raises(GatewayError, match="pending"):
        store.create_pairing()
    for code in codes:
        store.redeem(code)
    while len(store.devices()) < MAX_DEVICES:
        store.redeem(store.create_pairing()[0])
    assert len(store.devices()) == MAX_DEVICES
    with pytest.raises(GatewayError, match="revoke"):
        store.create_pairing()
    store.revoke(store.devices()[0].device_id)
    store.create_pairing()


# -- permissions --------------------------------------------------------------------


def test_scopes_default_to_all_and_are_canonical(store, tmp_path):
    store.allow(workspace(tmp_path))
    device, _ = store.redeem(store.create_pairing()[0])
    assert device.scopes == SCOPES == ("read", "prompt", "approve", "voice", "live")
    narrow, _ = store.redeem(store.create_pairing(scopes="voice, read ,read")[0])
    assert narrow.scopes == ("read", "voice")
    listed, _ = store.redeem(store.create_pairing(scopes=["prompt", "read"])[0])
    assert listed.scopes == ("read", "prompt")


@pytest.mark.parametrize("bad", ["", " , ", "admin", "read,admin", "READ", "shell"])
def test_unknown_or_empty_scopes_are_rejected(store, tmp_path, bad):
    store.allow(workspace(tmp_path))
    with pytest.raises(GatewayError):
        store.create_pairing(scopes=bad)
    with pytest.raises(GatewayError):
        parse_scopes(bad)
    assert store.pending_count() == 0


def test_check_device_enforces_scope_and_host(store, tmp_path):
    entry = store.allow(workspace(tmp_path))
    other = store.allow(workspace(tmp_path, "other"))
    device, _ = store.redeem(store.create_pairing(scopes="read", host_ids=[entry.host_id])[0])
    assert store.check_device(device.device_id, host_id=entry.host_id, scope="read")
    assert store.check_device(device.device_id, host_id=entry.host_id, scope="prompt") is None
    assert store.check_device(device.device_id, host_id=other.host_id, scope="read") is None
    assert store.check_device("dev_unknown", scope="read") is None
    assert store.check_device(device.device_id)


# -- host snapshot -------------------------------------------------------------------


def test_pairing_snapshots_the_hosts_allowed_at_that_moment(store, tmp_path):
    first = store.allow(workspace(tmp_path, "one"))
    code, _ = store.create_pairing()
    later = store.allow(workspace(tmp_path, "two"))
    device, _ = store.redeem(code)
    assert device.host_ids == (first.host_id,)
    assert store.check_device(device.device_id, host_id=later.host_id) is None

    both, _ = store.redeem(store.create_pairing()[0])
    assert set(both.host_ids) == {first.host_id, later.host_id}


def test_pairing_can_name_specific_hosts_and_refuses_unallowed_ones(store, tmp_path):
    one = store.allow(workspace(tmp_path, "one"))
    store.allow(workspace(tmp_path, "two"))
    device, _ = store.redeem(store.create_pairing(host_ids=[one.host_id, one.host_id])[0])
    assert device.host_ids == (one.host_id,)
    with pytest.raises(GatewayError, match="not allowed"):
        store.create_pairing(host_ids=["f" * 16])


def test_disallow_strips_the_host_from_every_device_and_pending_code(store, tmp_path):
    one = store.allow(workspace(tmp_path, "one"))
    two = store.allow(workspace(tmp_path, "two"))
    device, _ = store.redeem(store.create_pairing()[0])
    pending_code, _ = store.create_pairing()
    store.disallow(one.host_id)
    refreshed = store.devices()[0]
    assert refreshed.host_ids == (two.host_id,)
    assert store.check_device(device.device_id, host_id=one.host_id) is None
    redeemed, _ = store.redeem(pending_code)
    assert redeemed.host_ids == (two.host_id,)


# -- expiry and revocation -----------------------------------------------------------


def test_device_expires_with_its_ttl(store, tmp_path, clock):
    store.allow(workspace(tmp_path))
    device, token = store.redeem(store.create_pairing(ttl_days=1)[0])
    assert device.expires_at == clock.now + 86400
    clock.now += 86399
    assert store.authenticate(token)
    assert store.check_device(device.device_id)
    clock.now += 2
    assert store.authenticate(token) is None
    assert store.check_device(device.device_id) is None
    store.create_pairing()  # any mutation purges the expired device
    assert store.devices() == []


def test_revoke_is_immediate_and_reports_unknown_devices(store, tmp_path):
    store.allow(workspace(tmp_path))
    device, token = store.redeem(store.create_pairing()[0])
    other, other_token = store.redeem(store.create_pairing()[0])
    assert store.revoke(device.device_id) is True
    assert store.authenticate(token) is None
    assert store.check_device(device.device_id) is None
    assert store.authenticate(other_token) == other
    assert store.revoke(device.device_id) is False


def test_another_process_view_sees_a_revoke_on_the_next_check(store, tmp_path):
    store.allow(workspace(tmp_path))
    device, token = store.redeem(store.create_pairing()[0])
    reader = GatewayStore(store.root)  # a running gateway with a warm cache
    assert reader.authenticate(token)
    store.revoke(device.device_id)
    assert reader.authenticate(token) is None
    assert reader.check_device(device.device_id) is None


def test_authenticate_rejects_malformed_tokens(store, tmp_path):
    store.allow(workspace(tmp_path))
    _, token = store.redeem(store.create_pairing()[0])
    assert store.authenticate("") is None
    assert store.authenticate("x" * 257) is None
    assert store.authenticate(token + "x") is None
    assert store.authenticate(token.upper()) is None


def test_device_names_are_cleaned_and_bounded(store, tmp_path):
    store.allow(workspace(tmp_path))
    device, _ = store.redeem(store.create_pairing(name="x" * 200)[0], "")
    assert device.name == "x" * 64
    device, _ = store.redeem(store.create_pairing(name="kept")[0], "a\x00b\x1b[31m\n")
    assert all(ch.isprintable() for ch in device.name)
    assert "\x00" not in device.name and "\n" not in device.name
    device, _ = store.redeem(store.create_pairing(name="fallback")[0], "\x00\x01")
    assert device.name == "fallback"


# -- cross-process locking ------------------------------------------------------------

_WORKER = textwrap.dedent(
    """
    import sys
    from nexus.gateway.store import GatewayStore
    store = GatewayStore(sys.argv[1])
    for code in sys.argv[2:]:
        store.redeem(code)
    """
)


def test_concurrent_processes_never_lose_each_others_updates(store, tmp_path):
    store.allow(workspace(tmp_path))
    codes = [store.create_pairing()[0] for _ in range(6)]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(store.root), *codes[i::2]],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for i in range(2)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()
    fresh = GatewayStore(store.root)
    assert len(fresh.devices()) == 6
    assert fresh.pending_count() == 0
    assert len({d.device_id for d in fresh.devices()}) == 6


def test_a_code_redeemed_in_two_processes_is_won_exactly_once(store, tmp_path):
    store.allow(workspace(tmp_path))
    code, _ = store.create_pairing()
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(store.root), code],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    codes = []
    for proc in procs:
        proc.communicate(timeout=60)
        codes.append(proc.returncode)
    assert sorted(c == 0 for c in codes) == [False, True]
    assert len(GatewayStore(store.root).devices()) == 1


def test_one_gateway_serves_a_nexus_home_at_a_time(store):
    assert store.serving() is False
    fd = store.acquire_serve_lock()
    try:
        assert store.serving() is True
        with pytest.raises(GatewayError, match="already serving"):
            GatewayStore(store.root).acquire_serve_lock()
    finally:
        os.close(fd)
    assert store.serving() is False
    os.close(store.acquire_serve_lock())
