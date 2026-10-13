"""The once-per-machine GPT Live consent store and its host command (docs/live.md, docs/security.md)."""
from __future__ import annotations

import json
import stat

from nexus.host import protocol as p
from nexus.host.facade import HostFacade
from nexus.host_support import live_consent as lc
from tests.test_host_facade import _live_runtime


def test_consent_round_trips_survives_a_new_reader_and_can_be_revoked(tmp_path):
    assert lc.read_consent(tmp_path) == ""
    stamp = lc.write_consent(True, tmp_path)
    assert stamp and lc.read_consent(tmp_path) == stamp  # a fresh read, not process memory
    path = lc.consent_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.parent == tmp_path / ".nexus"
    assert lc.write_consent(False, tmp_path) == "" and lc.read_consent(tmp_path) == ""
    assert lc.write_consent(False, tmp_path) == ""  # idempotent


def test_stale_corrupt_or_oversized_consent_counts_as_not_given(tmp_path):
    path = lc.consent_path(tmp_path)
    path.parent.mkdir(parents=True)
    for body in ('{"version": 0, "accepted_at": "x"}', "not json", "[]", " " * 5000):
        path.write_text(body)
        assert lc.read_consent(tmp_path) == ""
    path.write_text(json.dumps({"version": lc.NOTICE_VERSION, "accepted_at": "2026-01-01"}))
    assert lc.read_consent(tmp_path) == "2026-01-01"


async def test_consent_command_and_status_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "elsewhere"))
    runtime = _live_runtime(tmp_path)
    runtime._home = tmp_path
    facade = HostFacade(runtime)
    try:
        assert (await facade.handle(p.LiveConsent())) == p.LiveConsentResult(accepted=False)
        assert (await facade.handle(p.LiveStatus())).consent is False
        granted = await facade.handle(p.LiveConsent(accept=True))
        assert granted.accepted and granted.accepted_at
        assert (await facade.handle(p.LiveStatus())).consent is True
        assert (await facade.handle(p.LiveConsent())).accepted
        assert (await facade.handle(p.LiveConsent(accept=False))).accepted is False
        assert (await facade.handle(p.LiveStatus())).consent is False
    finally:
        await runtime.aclose()
