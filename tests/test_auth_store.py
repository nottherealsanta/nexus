"""Private local credential persistence and cross-store visibility."""
import asyncio
import os

import pytest

from nexus.auth.store import CredentialRecord, FileCredentialStore, FileSecretStore
from nexus.errors import ProviderError


async def test_oauth_and_provider_round_trip_and_delete(tmp_path):
    path = tmp_path / "credentials.json"
    oauth = FileCredentialStore(path=path)
    keys = FileSecretStore(path=path)
    record = CredentialRecord("refresh", "account")
    await oauth.write("default", record)
    await keys.write_secret("opencode-go:default", "api-secret")
    assert await FileCredentialStore(path=path).read("default") == record
    assert await keys.read_secret("opencode-go:default") == "api-secret"
    await oauth.delete("default")
    assert await oauth.read("default") is None
    assert await keys.read_secret("opencode-go:default") == "api-secret"
    await keys.delete_secret("opencode-go:default")
    assert await keys.read_secret("opencode-go:default") is None
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


async def test_concurrent_writes_preserve_accounts_and_logout_is_seen(tmp_path):
    path = tmp_path / "credentials.json"
    stores = [FileSecretStore(path=path) for _ in range(8)]
    await asyncio.gather(*(s.write_secret(f"provider:p{i}", f"secret-{i}") for i, s in enumerate(stores)))
    for i in range(8):
        assert await stores[0].read_secret(f"provider:p{i}") == f"secret-{i}"
    await stores[1].delete_secret("provider:p0")
    assert await stores[0].read_secret("provider:p0") is None


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "invalid", "oversized"])
async def test_unsafe_files_fail_without_exposing_contents(tmp_path, unsafe):
    path = tmp_path / "credentials.json"
    target = tmp_path / "target"
    target.write_text("secret-value")
    if unsafe == "symlink":
        path.symlink_to(target)
    else:
        path.write_text('secret-value' if unsafe != "oversized" else 'x' * (1024 * 1024 + 1))
        os.chmod(path, 0o644 if unsafe == "permissions" else 0o600)
    store = FileSecretStore(path=path)
    for operation in [store.read_secret, lambda account: store.write_secret(account, "replacement")]:
        with pytest.raises((ProviderError, OSError)) as error:
            await operation("provider:default")
        assert "secret-value" not in str(error.value)
    assert target.read_text() == "secret-value"


async def test_home_override_and_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "private"))
    store = FileSecretStore()
    assert await store.read_secret("provider:default") is None
    await store.write_secret("provider:default", "value")
    assert (tmp_path / "private" / "credentials.json").exists()
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
