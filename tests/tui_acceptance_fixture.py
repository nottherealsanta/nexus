"""Host-command instrumentation for the real-browser Textual acceptance check."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import ClassVar

from visual_tui_demo import DemoTransport, VisualDemoApp

from nexus.host import protocol as p
from nexus.session.manager import SessionSummary
from nexus.ui.cli.client import Client


class AcceptanceTransport(DemoTransport):
    """Deterministic supported host commands plus an observable command ledger."""

    MODELS: ClassVar[list[dict[str, object]]] = [
        {"provider": "fixture", "id": "alpha", "tier": "small", "context": 4096},
        {"provider": "fixture", "id": "beta", "tier": "medium", "context": 8192},
    ]
    REASONING_LEVELS: ClassVar[list[str]] = ["low", "medium", "high"]

    def __init__(self) -> None:
        super().__init__("functional")
        self.active_session = "visual"
        self.models: dict[str, str] = {"visual": "alpha"}
        self.agent = "build"
        self.reasoning: dict[str, str] = {"visual": "low"}
        self.log_path = os.environ.get("NEXUS_TUI_ACCEPTANCE_LOG")
        self.turn_gate = os.environ.get("NEXUS_TUI_ACCEPTANCE_TURN_GATE")

    def _record(self, command: p.Command, **state: object) -> None:
        if not self.log_path:
            return
        row = {"command": type(command).__name__, **state}
        with Path(self.log_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    async def request(self, command: p.Command) -> p.Result:
        if isinstance(command, p.SessionOpen):
            self.active_session = command.session
            self.models.setdefault(command.session, "alpha")
            self.reasoning.setdefault(command.session, "low")
            self._record(command, active_session=self.active_session)
            return p.SessionOpenResult(SessionSummary(id=command.session))
        if isinstance(command, p.SessionState):
            self._record(command, active_session=self.active_session)
            return p.SessionStateResult(session=command.session, seq=0, view={})
        if isinstance(command, p.AgentCurrent):
            model = self.models.get(command.session, "alpha")
            self._record(
                command,
                active_session=self.active_session,
                model=model,
                reasoning_effort=self.reasoning.get(command.session, "low"),
            )
            return p.AgentCurrentResult(
                session=command.session,
                name=self.agent,
                source="fixture",
                provider="fixture",
                model=model,
                reasoning_effort=self.reasoning.get(command.session, "low"),
                supported_levels=self.REASONING_LEVELS,
                thinking_budget=2048,
            )
        if isinstance(command, p.ModelsList):
            self._record(command, active_session=self.active_session)
            return p.ModelsListResult(count=len(self.MODELS), models=self.MODELS)
        if isinstance(command, p.AgentsList):
            self._record(command, active_session=self.active_session)
            return p.AgentsListResult(agents=[
                {"name": "general", "description": "General implementation", "contexts": ["root"]},
                {"name": "build", "description": "Focused product changes", "contexts": ["root"]},
                {"name": "explore", "description": "Read-only investigation", "contexts": ["root"]},
            ])
        if isinstance(command, p.AgentSelect):
            self._record(
                command,
                active_session=self.active_session,
                selected_agent=command.name,
            )
            self.agent = command.name
            return p.AgentSelectResult(
                session=command.session,
                name=self.agent,
                source="fixture",
            )
        if isinstance(command, p.ModelSelect):
            model = command.ref.rsplit("/", 1)[-1]
            if model == "beta":
                self._record(
                    command,
                    active_session=self.active_session,
                    selected_model=model,
                    accepted=False,
                    prior_model=self.models.get(command.session, "alpha"),
                )
                return p.ErrorResult(kind="SelectionRejected", message="fixture rejected beta")
            if model not in {row["id"] for row in self.MODELS}:
                return p.ErrorResult(kind="InvalidModel", message=f"unknown model {command.ref}")
            self.models[command.session] = model
            self._record(
                command,
                active_session=self.active_session,
                selected_model=model,
            )
            return p.ModelSelectResult(
                session=command.session,
                reference=command.ref,
                provider="fixture",
                model=model,
                tier=next(row["tier"] for row in self.MODELS if row["id"] == model),
            )
        if isinstance(command, p.ReasoningEffortSelect):
            self.reasoning[command.session] = command.effort or "low"
            self._record(
                command,
                active_session=self.active_session,
                selected_effort=self.reasoning[command.session],
            )
            return p.ReasoningEffortSelectResult(
                session=command.session,
                stored_override=command.effort,
                effective_effort=self.reasoning[command.session],
                supported_levels=self.REASONING_LEVELS,
            )
        if isinstance(command, p.SessionList):
            self._record(command, active_session=self.active_session)
            return p.SessionListResult(
                sessions=[
                    SessionSummary(id="visual", title="Visual fixture"),
                    SessionSummary(id="archive-session", title="Archive fixture"),
                ]
            )
        if isinstance(command, p.LogsRead):
            self._record(
                command,
                active_session=self.active_session,
                polled_at=time.monotonic(),
            )
            return p.LogsReadResult(
                daemon=p.DaemonLogPage(
                    entries=[
                        p.LogEntry(
                            source="daemon",
                            seq=1,
                            ts=1_700_000_000,
                            level="info",
                            kind="fixture.ready",
                            summary="Deterministic acceptance fixture",
                        )
                    ],
                    next_cursor="fixture-1",
                ),
                session=p.SessionLogPage(),
            )
        if isinstance(command, p.SessionStart):
            self._record(command, active_session=self.active_session, content=command.content)
            return await super().request(command)
        if isinstance(command, p.SessionEnqueue):
            self._record(command, active_session=self.active_session, content=command.content)
            return p.SessionEnqueueResult(
                session=command.session,
                queued_id="fixture-enqueue",
            )
        return await super().request(command)

    async def _stream(self, session: str, from_seq: int = 0, *, follow: bool = True, **kwargs):
        async for event in super()._stream(session, from_seq, follow=follow, **kwargs):
            if event.type == "turn.completed" and self.turn_gate:
                gate = Path(self.turn_gate)
                while not gate.exists():
                    await asyncio.sleep(0.05)
            yield event

    def events(self, session: str, from_seq: int = 0, **kwargs):
        return self._stream(session, from_seq, **kwargs)


class AcceptanceApp(VisualDemoApp):
    """The existing real shell with deterministic acceptance host behavior."""

    def __init__(self) -> None:
        super().__init__("functional")
        self.acceptance_transport = AcceptanceTransport()
        self.controller.client = Client(self.acceptance_transport)


def main() -> None:
    AcceptanceApp().run()


if __name__ == "__main__":
    main()
