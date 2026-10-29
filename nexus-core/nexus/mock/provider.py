"""Scripted offline llmhub provider adapted from old Nexus mock provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from ..llmhub.types import *
from .dsl import Ctx, Dyn, Fail, Hang, Scenario, Turn


class MockProvider:
    name = "mock"

    def __init__(self, scenario: str):
        from .scenarios import CATALOG

        self.scenario = scenario
        self.catalog: dict[str, Scenario] = CATALOG

    def body(self, req: Request) -> dict:
        return {
            "model": f"mock/{self.scenario}",
            "instructions": req.instructions,
            "messages": [{"role": m.role, "parts": [str(p) for p in m.parts]} for m in req.messages],
            "tools": [t.name for t in req.tools],
        }

    def stream(self, req: Request) -> AsyncIterator[Event]:
        return self._stream(req)

    async def _stream(self, req: Request) -> AsyncIterator[Event]:
        scenario = self.catalog.get(self.scenario)
        if scenario is None:
            raise ProviderError(f"mock: unknown scenario {self.scenario!r}")
        actor = "child" if "child" in scenario.actors and not any(tool.name == "session" for tool in req.tools) else "main"
        index = sum(1 for m in req.messages if m.role == "assistant")
        steps = scenario.actors.get(actor, ())
        step: object = steps[index] if index < len(steps) else Turn(text=f"[mock] {scenario.name} script finished.")
        ctx = Ctx(scenario.name, actor, index, req.messages)
        for _ in range(8):
            if isinstance(step, Dyn):
                step = step.fn(ctx)
                continue
            break
        if isinstance(step, Fail):
            if step.recover_at_user_turn is None or ctx.user_turns < step.recover_at_user_turn:
                raise ProviderError(step.message, status=503, body=step.message, phase="response")
            step = step.then or Turn()
        yield WireRequest("MOCK", f"mock://{scenario.name}", {}, self.body(req))
        yield WireResponse(200, {"content-type": "text/event-stream"})
        if isinstance(step, Hang):
            async for e in self._text(step.text):
                yield e
            await asyncio.Event().wait()
        assert isinstance(step, Turn)
        if step.think:
            for i in range(0, len(step.think), 16):
                delta = step.think[i : i + 16]
                yield WireEvent("reasoning.delta", {"text": delta})
                yield ReasoningDelta(delta)
                await asyncio.sleep(0)
            yield PartDone(Reasoning(step.think, "mock"))
        if step.text:
            full = ""
            for i in range(0, len(step.text), 12):
                delta = step.text[i : i + 12]
                full += delta
                yield WireEvent("text.delta", {"text": delta})
                yield TextDelta(delta)
                await asyncio.sleep(0)
            yield PartDone(Text(full, "mock"))
        for pos, c in enumerate(step.calls):
            cid = f"mock_{actor}_{index}_{pos}"
            args = json.dumps(c.input, ensure_ascii=False)
            yield WireEvent("tool.started", {"id": cid, "name": c.name})
            yield ToolCallStarted(cid, c.name)
            yield WireEvent("tool.args", {"id": cid, "arguments": args})
            yield ToolArgsDelta(cid, args)
            call = ToolCall(cid, c.name, args, "mock", {"type": "mock_call", "id": cid, "name": c.name, "arguments": args})
            yield WireEvent("tool.done", {"id": cid, "name": c.name, "arguments": args})
            yield PartDone(call)
        used = step.usage or (
            max(1, sum(len(str(p)) for m in req.messages for p in m.parts) // 4),
            max(1, (len(step.text) + len(step.think)) // 4 + len(step.calls) * 10),
        )
        yield WireEvent("usage", {"input_tokens": used[0], "output_tokens": used[1]})
        yield Usage(*used)
        yield WireEvent("done", {"stop": "tool_calls" if step.calls else "end_turn"})
        yield Done("tool_calls" if step.calls else "end_turn", f"mock-{scenario.name}-{index}")

    async def _text(self, text: str):
        for i in range(0, len(text), 12):
            d = text[i : i + 12]
            yield WireEvent("text.delta", {"text": d})
            yield TextDelta(d)
            await asyncio.sleep(0.01)
