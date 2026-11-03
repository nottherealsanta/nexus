"""The once-per-provider privacy notice (MANAGER_PLAN.md section 9, "Privacy")."""
from __future__ import annotations

import pytest
from manager_support import Harness, enable_manager

from nexus.manager import consent as consent_module


@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, model="copilot/gpt-6-luna", consent=False)
    yield harness
    await harness.close()


async def test_a_real_provider_needs_the_notice_and_the_mock_does_not(h, tmp_path, monkeypatch):
    status = (await h.client.get("/api/manager")).json()["consent"]
    assert status["provider"] == "copilot" and status["required"] is True and status["accepted_at"] is None
    assert "GitHub Copilot" in status["notice"] and status["version"] == consent_module.NOTICE_VERSION
    enable_manager(h.home, "mock/manager_demo")
    mock = (await h.client.get("/api/manager")).json()["consent"]
    assert mock["required"] is False and mock["notice"] == ""


async def test_starting_and_saying_are_refused_until_it_is_acknowledged(h):
    stream = h.open_stream()
    refused = await h.client.post("/api/manager/start", json={"stream_id": stream.stream_id, "client_id": "tui"})
    assert refused.status_code == 409 and refused.json()["code"] == "consent_required"
    assert refused.json()["consent"]["required"] is True and "GitHub Copilot" in refused.json()["consent"]["notice"]
    assert h.manager.call is None
    wrong = await h.client.post("/api/manager/consent", json={"accept": True, "version": 0})
    assert wrong.status_code == 400
    assert (await h.client.post("/api/manager/consent", json={"accept": "yes", "version": 1})).status_code == 400
    ok = await h.client.post("/api/manager/consent", json={"accept": True, "version": consent_module.NOTICE_VERSION})
    assert ok.status_code == 200 and ok.json()["required"] is False and ok.json()["accepted_at"]
    await h.start()
    revoked = await h.client.post("/api/manager/consent", json={"accept": False})
    assert revoked.json()["required"] is True
    said = await h.client.post("/api/manager/say", json={"stream_id": stream.stream_id, "text": "hi", "source": "typed"})
    assert said.status_code == 409 and said.json()["code"] == "consent_required"


async def test_switching_provider_or_bumping_the_notice_asks_again(h, monkeypatch):
    await h.client.post("/api/manager/consent", json={"accept": True, "version": consent_module.NOTICE_VERSION})
    assert (await h.client.get("/api/manager")).json()["consent"]["required"] is False
    enable_manager(h.home, "codex/gpt-6-luna")
    other = (await h.client.get("/api/manager")).json()["consent"]
    assert other["provider"] == "codex" and other["required"] is True and "OpenAI" in other["notice"]
    enable_manager(h.home, "copilot/gpt-6-luna")
    assert (await h.client.get("/api/manager")).json()["consent"]["required"] is False
    monkeypatch.setattr(consent_module, "NOTICE_VERSION", consent_module.NOTICE_VERSION + 1)
    assert (await h.client.get("/api/manager")).json()["consent"]["required"] is True  # re-shown when it changes


async def test_the_acknowledgement_is_stored_and_survives_a_restart(h):
    await h.client.post("/api/manager/consent", json={"accept": True, "version": consent_module.NOTICE_VERSION})
    from nexus.server import make_runtime

    again = make_runtime(h.store)
    assert again.manager.consent.status("copilot/x")["required"] is False
    await again.shutdown()
