"""The keychain read cache in ``auth/store.py``: one read per process until a
Nexus write or delete (in any process) replaces the credential's stamp."""
from __future__ import annotations

import types

from nexus.auth.store import CredentialRecord, KeyringCredentialStore, KeyringSecretStore


class _Backend:
    priority = 5


_Backend.__module__ = "keyring.backends.macOS"


class _PasswordDeleteError(Exception):
    pass


def _fake_keyring():
    items: dict[tuple[str, str], str] = {}
    reads: list[tuple[str, str]] = []

    def get_password(service, account):
        reads.append((service, account))
        return items.get((service, account))

    def set_password(service, account, value):
        items[(service, account)] = value

    def delete_password(service, account):
        if items.pop((service, account), None) is None:
            raise _PasswordDeleteError

    module = types.SimpleNamespace(
        get_keyring=_Backend, get_password=get_password, set_password=set_password,
        delete_password=delete_password, errors=types.SimpleNamespace(PasswordDeleteError=_PasswordDeleteError),
    )
    return module, items, reads


async def test_repeated_reads_hit_the_keychain_once(tmp_path):
    keyring, items, reads = _fake_keyring()
    items[("nexus.provider-credentials", "opencode-go:default")] = "key-1"
    store = KeyringSecretStore(keyring, lock_dir=tmp_path)
    for _ in range(5):
        assert await store.read_secret("opencode-go:default") == "key-1"
    # A fresh store in the same process (a status check) reuses the cache.
    assert await KeyringSecretStore(keyring, lock_dir=tmp_path).read_secret("opencode-go:default") == "key-1"
    assert len(reads) == 1


async def test_missing_credentials_are_cached_too(tmp_path):
    keyring, _items, reads = _fake_keyring()
    store = KeyringSecretStore(keyring, lock_dir=tmp_path)
    assert await store.read_secret("github-copilot:default") is None
    assert await store.read_secret("github-copilot:default") is None
    assert len(reads) == 1


async def test_own_write_and_delete_update_the_cache_without_reading(tmp_path):
    keyring, _items, reads = _fake_keyring()
    store = KeyringCredentialStore(keyring, lock_dir=tmp_path)
    record = CredentialRecord("refresh", "acct")
    await store.write("default", record)
    assert await store.read("default") == record
    await store.delete("default")
    assert await store.read("default") is None
    assert reads == []


async def test_another_process_write_is_seen_through_the_stamp(tmp_path):
    keyring, items, reads = _fake_keyring()
    daemon = KeyringSecretStore(keyring, lock_dir=tmp_path)
    assert await daemon.read_secret("opencode-go:default") is None
    # Simulate another process: change the keychain and the stamp, not this cache.
    items[("nexus.provider-credentials", "opencode-go:default")] = "key-2"
    daemon._bump_stamp(daemon._stamp_path("nexus.provider-credentials", "opencode-go:default"))
    assert await daemon.read_secret("opencode-go:default") == "key-2"
    assert await daemon.read_secret("opencode-go:default") == "key-2"
    assert len(reads) == 2


async def test_stamp_holds_no_secret_and_is_private(tmp_path):
    keyring, _items, _reads = _fake_keyring()
    store = KeyringSecretStore(keyring, lock_dir=tmp_path)
    await store.write_secret("opencode-go:default", "secret-value-123")
    stamps = list(tmp_path.glob("credential-*.stamp"))
    assert len(stamps) == 1
    assert b"secret-value" not in stamps[0].read_bytes()
    assert "opencode" not in stamps[0].name
    assert stamps[0].stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob("*.tmp"))
