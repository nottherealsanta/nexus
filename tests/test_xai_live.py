"""Gated live xAI (Grok) checks over the SuperGrok subscription.

Marked ``live`` and excluded from the offline suite. Sign in first (Settings →
Providers → xAI (SuperGrok)), then run::

    NEXUS_XAI_LIVE=1 .venv/bin/python -m pytest -q -m live tests/test_xai_live.py

``NEXUS_XAI_LIVE_MODEL`` overrides the model (default ``grok-4.7``). The test
reads the existing sign-in from the **real** private credential file (its path
is captured at import, before ``conftest.py`` gives each test a private
``NEXUS_HOME``) through ``XAIOAuthManager``, which may refresh and rotate it,
and prints nothing secret. Sessions still go to the private test home. The
provider check streams one bounded request and asserts real inference and
termination; the runtime check runs one full turn.
"""
import os

import pytest

from nexus.auth.store import FileSecretStore
from nexus.auth.xai import XAIHeaders, XAIOAuthManager
from nexus.config import Config
from nexus.config.paths import nexus_home
from nexus.config.schema import ConfigV2, ModelSection, ProviderSection
from nexus.model.message import Message, Text
from nexus.model.providers.xai import XAIProvider
from nexus.model.request import ModelRequest, SamplingParams
from nexus.model.stream import MessageStart, MessageStop, TextDelta, Usage
from nexus.runtime import Runtime

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("NEXUS_XAI_LIVE") != "1", reason="set NEXUS_XAI_LIVE=1 after signing in"),
]

MODEL = os.environ.get("NEXUS_XAI_LIVE_MODEL", "grok-4.7")
#: The user's real credential file, resolved before per-test isolation applies.
_CREDENTIALS = nexus_home() / "credentials.json"


def _manager(**kwargs) -> XAIOAuthManager:
    return XAIOAuthManager(store=FileSecretStore(path=_CREDENTIALS), **kwargs)


async def _signed_in() -> XAIOAuthManager:
    manager = _manager()
    if not await manager.status():
        pytest.skip("no SuperGrok sign-in; connect xAI in Settings → Providers")
    return manager


async def test_live_xai_stream_reaches_a_normal_stop():
    provider = XAIProvider(model=MODEL, auth_headers=XAIHeaders(await _signed_in()), timeout=60.0)
    request = ModelRequest(
        messages=[Message(role="user", content=[Text(text="Reply with exactly one word: pong")])],
        params=SamplingParams(max_output_tokens=256, reasoning_effort="low"),
        metadata={"session_id": "nexus-live-xai"},
    )
    try:
        events = [event async for event in provider.stream(request)]
    finally:
        await provider.aclose()
    assert isinstance(events[0], MessageStart)
    assert "pong" in "".join(event.text for event in events if isinstance(event, TextDelta)).lower()
    assert any(isinstance(event, Usage) and event.input > 0 for event in events)
    assert isinstance(events[-1], MessageStop) and events[-1].stop_reason == "end_turn"


async def test_live_xai_text_turn(tmp_path):
    await _signed_in()
    reference = f"xai/{MODEL}"
    config = Config(
        model=reference, version=2,
        v2=ConfigV2(model=ModelSection(default=reference),
                    providers={"xai": ProviderSection(auth="xai_oauth", api="responses")}),
    )
    async with Runtime(tmp_path, config=config, xai_auth_factory=_manager) as runtime:
        session = runtime.session("live")
        events = [event async for event in session.send("Reply with exactly one word: pong")]
    types = [event.type for event in events]
    assert types[0] == "turn.started" and types[-1] == "turn.completed"
    assert any(event.type == "text" for event in events)
