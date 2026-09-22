"""Phase 3 Runtime wiring: per-turn capabilities/counting and snapshot cadence."""
from __future__ import annotations

import asyncio

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection, ModelSection, SessionSection
from nexus.model.capabilities import Capabilities
from nexus.model.message import Message, Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


class FakeAnthropic:
    name = "anthropic"

    def __init__(self):
        self.count_calls = 0
        self.closed = False

    def capabilities(self, model):
        return Capabilities(
            tools=True,
            streaming=True,
            prompt_caching=True,
            max_context_tokens=100_000,
            max_output_tokens=1_000,
        )

    async def count_tokens(self, request):
        self.count_calls += 1
        return 11

    async def aclose(self):
        self.closed = True


class Session:
    id = "s"

    def __init__(self, messages):
        self._messages = messages

    @property
    def messages(self):
        return list(self._messages)


def _config(model="anthropic/x", *, snapshot_every=20, max_tokens=100_000):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            context=ContextSection(max_tokens=max_tokens, safety_margin_tokens=0),
            session=SessionSection(snapshot_every=snapshot_every),
        ),
    )


def test_runtime_injects_capabilities_and_request_counter(tmp_path):
    provider = FakeAnthropic()
    runtime = Runtime(tmp_path, config=_config(), providers={"anthropic": provider})
    snapshot = runtime._assembler.for_turn()
    request = asyncio.run(
        snapshot.assemble(
            Session([Message(role="user", content=[Text(text="hi")])])
        )
    )
    # Capabilities came from the resolved provider, not the conservative default.
    assert request.metadata["cache"]["enabled"] is True
    # The request-aware counter asked the provider exactly once.
    assert provider.count_calls == 1


async def _drain(iterator):
    return [event async for event in iterator]


def test_runtime_supplies_snapshot_cadence_from_config(tmp_path):
    provider = ScriptedProvider(text_response("a"), text_response("b"))
    runtime = Runtime(
        tmp_path,
        config=_config(model="scripted/m", snapshot_every=2),
        providers={"scripted": provider},
    )
    session = runtime.session("s")
    asyncio.run(_drain(session.send("one")))
    assert not session.snapshot_path.exists()
    events = asyncio.run(_drain(session.send("two")))
    assert session.snapshot_path.exists()
    assembled = next(e for e in events if e.type == "context.assembled")
    assert "context" in assembled.data
    assert assembled.data["context"]["input_budget"] > 0


def test_runtime_assembled_event_has_no_content(tmp_path):
    provider = ScriptedProvider(text_response("a"))
    runtime = Runtime(
        tmp_path, config=_config(model="scripted/m"), providers={"scripted": provider}
    )
    session = runtime.session("s")
    events = asyncio.run(_drain(session.send("SECRET-USER-TEXT")))
    assembled = next(e for e in events if e.type == "context.assembled")
    assert "SECRET-USER-TEXT" not in str(assembled.data)
    assert "context" in assembled.data
    assert "cache" in assembled.data
