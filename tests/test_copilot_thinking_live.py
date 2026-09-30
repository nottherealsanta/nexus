"""Explicitly gated live Copilot Luna thinking probe (plan section 9).

Run with NEXUS_COPILOT_THINKING_LIVE=1 pytest -m live tests/test_copilot_thinking_live.py.
Authentication stays in the existing secure keychain; no secrets are printed.
"""
import asyncio
import os

import pytest

from nexus.auth.copilot import CopilotAuthManager, CopilotHeaders, DEFAULT_BASE_URL
from nexus.model.message import Message, Text
from nexus.model.providers.openai import EndpointFallback, OpenAIProvider
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStop, TextDelta, ThinkingDelta, ThinkingEnd

pytestmark = pytest.mark.live


@pytest.mark.skipif(os.environ.get("NEXUS_COPILOT_THINKING_LIVE") != "1",
                    reason="set NEXUS_COPILOT_THINKING_LIVE=1 to authorize the live request")
async def test_copilot_luna_thinking_summaries():
    manager = CopilotAuthManager()
    if not await manager.status():
        pytest.skip("Connect GitHub Copilot in Settings first")
    selector = EndpointFallback()
    provider = OpenAIProvider(
        model="gpt-6-luna", api="chat", base_url=DEFAULT_BASE_URL,
        auth_headers=CopilotHeaders(manager), api_selector=selector,
    )
    try:
        async with asyncio.timeout(60):
            events = [event async for event in provider.stream(ModelRequest(
                messages=[Message("user", [Text(
                    "Find the smallest positive integer divisible by 7 whose digits sum to 23. "
                    "Check your answer carefully and explain briefly."
                )])],
            ))]
        assert selector.api_for("gpt-6-luna") == "responses"
        assert any(isinstance(event, ThinkingDelta) and event.text for event in events)
        assert any(isinstance(event, ThinkingEnd) for event in events)
        assert any(isinstance(event, TextDelta) and event.text for event in events)
        assert events[-1] == MessageStop(stop_reason="end_turn")
    finally:
        await provider.aclose()
