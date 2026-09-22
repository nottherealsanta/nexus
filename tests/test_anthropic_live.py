"""Credential-gated live Anthropic smoke test.

Marked ``live`` and excluded from the offline suite. Run explicitly with::

    ANTHROPIC_API_KEY=... pytest -m live tests/test_anthropic_live.py

The test is skipped when no credential is present, so it never breaks an
offline run and never makes a network call without a key.
"""
import os

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, ProviderSection
from nexus.runtime import Runtime

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY is not set",
    ),
]


def _live_config():
    model = "anthropic/claude-haiku-4-5-20251001"
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            providers={
                "anthropic": ProviderSection(api_key="${env:ANTHROPIC_API_KEY}")
            },
        ),
    )


async def test_live_anthropic_text_turn(tmp_path):
    async with Runtime(tmp_path, config=_live_config()) as runtime:
        session = runtime.session("live")
        events = [
            event
            async for event in session.send(
                "Reply with exactly one word: pong"
            )
        ]

    types = [event.type for event in events]
    assert types[0] == "turn.started"
    assert types[-1] == "turn.completed"
    assert any(event.type == "text" for event in events)
    assert session.active is False
