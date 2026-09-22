"""Conformance adapter for the OpenCode ACP subprocess surface (Phase 7c).

OpenCode is an *agent* surface, not a model wire protocol: ``opencode acp``
owns its own tool loop, so this adapter deliberately declares a narrow tag set
(``stream``/``close``/``cancel``) and every tools/thinking/wire-request case is
reported as a skip rather than faked. Cases that *do* apply are driven through a
real subprocess: the fake ACP server fixture is launched with a normalized
``--conformance-plan`` and replays it as ACP ``session/update`` chunks, so the
same case catalogue that drives the HTTP adapters also exercises the ACP
translation without any network.

The provider is built lazily on first ``provider`` access so a plan queued
beforehand is reflected in the child argv.
"""
from __future__ import annotations

import base64
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from nexus.model.provider import Provider
from nexus.model.providers.opencode import OpenCodeProvider
from nexus.model.request import ModelRequest
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

from .contract import (
    CANCEL,
    CLOSE,
    OPENCODE_DIALECT,
    STREAM,
    AdapterSpec,
    CapturedRequest,
    EventsStep,
    ParkStep,
    UnsupportedStep,
    WireStep,
)

__all__ = ["OPENCODE_ACP_SPEC", "OpenCodeACPAdapter"]

FAKE_AGENT = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "opencode"
    / "fake_acp_agent.py"
)

DEFAULT_MODEL = "agent-opaque-model"

#: An agent surface exposes no Nexus tools and carries no thinking signature, so
#: those tags are intentionally absent. It is not an HTTP transport either.
OPENCODE_ACP_TAGS = frozenset({STREAM, CLOSE, CANCEL})


def _plan_for(events: Sequence[StreamEvent]) -> str:
    """Encode a normalized event plan as the fake agent's conformance payload."""
    chunks: list[dict[str, str]] = []
    stop_reason = "end_turn"
    usage: dict[str, int] | None = None
    for event in events:
        if isinstance(event, TextDelta):
            chunks.append({"kind": "text", "text": event.text})
        elif isinstance(event, ThinkingDelta):
            chunks.append({"kind": "thinking", "text": event.text})
        elif isinstance(event, ToolCallStart):
            # The agent's own tool activity is not a Nexus tool call; surface it
            # as an opaque update the adapter will translate to ``Raw``.
            chunks.append({"kind": "tool", "name": event.name or ""})
        elif isinstance(event, (ToolCallDelta, ToolCallEnd)):
            continue
        elif isinstance(event, MessageStop):
            stop_reason = event.stop_reason
        elif isinstance(event, Usage):
            usage = {
                "inputTokens": event.input,
                "outputTokens": event.output,
            }
    payload: dict[str, Any] = {"chunks": chunks, "stop_reason": stop_reason}
    if usage is not None:
        payload["usage"] = usage
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


class _RecordingProvider(OpenCodeProvider):
    """OpenCodeProvider that records each request the harness issues."""

    def __init__(self, *, recorded: list[CapturedRequest], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._recorded = recorded

    def stream(self, req: ModelRequest):  # type: ignore[override]
        self._recorded.append(CapturedRequest(request=req))
        return super().stream(req)


class OpenCodeACPAdapter:
    """A live OpenCode ACP provider driven by the fake agent fixture."""

    name = "opencode_acp"
    model = DEFAULT_MODEL
    dialect = OPENCODE_DIALECT
    tags = OPENCODE_ACP_TAGS

    def __init__(self) -> None:
        self._scenario = "conformance"
        self._plan = _plan_for(
            (MessageStart(), MessageStop(stop_reason="end_turn"))
        )
        self._provider: OpenCodeProvider | None = None
        self._requests: list[CapturedRequest] = []

    # -- contract ----------------------------------------------------------

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            self._provider = _RecordingProvider(
                recorded=self._requests,
                command=[
                    sys.executable,
                    str(FAKE_AGENT),
                    "--scenario",
                    self._scenario,
                    "--conformance-plan",
                    self._plan,
                ],
                model=self.model,
                timeout_seconds=10.0,
                environ={},
            )
        return self._provider

    def queue(self, step: WireStep) -> None:
        if isinstance(step, EventsStep):
            self._scenario = "conformance"
            self._plan = _plan_for(step.events)
            return
        if isinstance(step, ParkStep):
            # The fake parks until the consumer closes/cancels; the provider
            # still emits MessageStart first, which the cancellation invariant
            # expects.
            self._scenario = "park"
            return
        raise UnsupportedStep(
            f"opencode_acp: transport step {type(step).__name__} is HTTP-only"
        )

    def queue_count_tokens(self, value: int | None) -> None:
        raise UnsupportedStep("opencode_acp: no count_tokens endpoint")

    def captured_requests(self) -> list[CapturedRequest]:
        return list(self._requests)

    @property
    def retry_delays(self) -> list[float]:
        return []

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()


OPENCODE_ACP_SPEC = AdapterSpec(
    name="opencode_acp",
    tags=OPENCODE_ACP_TAGS,
    make=OpenCodeACPAdapter,
    dialect=OPENCODE_DIALECT,
    description=(
        "OpenCode ACP subprocess surface over the fake agent fixture "
        "(agent-internal tools and signatures are documented skips)"
    ),
)
