"""Deliberately gated live ChatGPT OAuth smoke test for the Codex provider.

Run explicitly with::

    NEXUS_CODEX_OAUTH_LIVE=1 pytest -m live tests/test_codex_oauth_live.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from nexus.auth.codex import CodexOAuthManager
from nexus.config import Config
from nexus.errors import ProviderError
from nexus.model.message import Message, Text
from nexus.model.providers.openai import OpenAIProvider
from nexus.model.request import ModelRequest
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from nexus.runtime import Runtime

MARKER = "NEXUS_CODEX_OAUTH_OK"
WORKSPACE = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.environ.get("NEXUS_CODEX_OAUTH_LIVE") != "1",
    reason="set NEXUS_CODEX_OAUTH_LIVE=1 to authorize the live Codex OAuth request",
)
async def test_live_codex_oauth_responses_request():
    """Use the checked-in OAuth config and make exactly one text-only request."""
    manager = CodexOAuthManager(profile="default")
    try:
        credential_present = await manager.status()
    except ProviderError:
        pytest.skip(
            "Codex OAuth keychain is unavailable; configure a secure native keychain and log in"
        )
    if not credential_present:
        pytest.skip("Codex OAuth credential is absent; run `nexus auth codex login`")

    config = Config.load(WORKSPACE)
    assert config.model == "codex/gpt-5.6-luna"
    assert config.v2 is not None
    section = config.v2.providers["codex"]
    assert section.auth == "chatgpt_oauth"
    assert section.profile == "default"
    assert section.api == "responses"

    async with Runtime(WORKSPACE, config=config) as runtime:
        provider = runtime.providers["codex"]
        assert isinstance(provider, OpenAIProvider)
        async with asyncio.timeout(60):
            events = [
                event
                async for event in provider.stream(
                    ModelRequest(
                        messages=[
                            Message(
                                "user",
                                [
                                    Text(
                                        f"Reply with exactly {MARKER} and nothing else."
                                    )
                                ],
                            )
                        ],
                        model="gpt-5.6-luna",
                    )
                )
            ]

    starts = [event for event in events if isinstance(event, MessageStart)]
    stops = [event for event in events if isinstance(event, MessageStop)]
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    tool_events = [
        event
        for event in events
        if isinstance(event, (ToolCallStart, ToolCallDelta, ToolCallEnd))
    ]

    assert starts == [MessageStart(provider="codex", model="gpt-5.6-luna")]
    assert text.strip() == MARKER
    assert stops == [MessageStop(stop_reason="end_turn")]
    assert tool_events == []
