"""Deterministic host events for native terminal screenshots."""
from __future__ import annotations
import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from nexus.events import Event
from nexus.host import protocol as p


FIXTURE_PATH = "fixture.css"

FIXTURE_BEFORE = "Screen {\n    color: #d6d3d1;\n}\n"

def _event(kind: str, seq: int, data: dict | None = None, *, turn: str = "turn-1") -> Event:
    return Event(type=kind, data=data or {}, seq=seq, session="visual", turn=turn)

def _seed_events(state: str) -> list[Event]:
    if state in ("empty", "functional", "slash_menu"):
        return []
    if state == "first_message":
        return [
            _event("input.queued", 1, {"queued_id": "hi", "content": [{"text": "hi"}]}),
            _event("turn.started", 2, {"agent": {"name": "build"}}),
            _event("input.consumed", 3, {"queued_id": "hi", "turn": "turn-1"}),
            _event("model.started", 4, {"provider": "openai", "model": "gpt-5.6-luna"}),
            _event("turn.failed", 5, {"error": "HTTP 400: invalid request"}),
        ]
    if state == "design":
        return _design_events()
    if state == "reference":
        first = "reference-1"
        second = "reference-2"

        def event(kind: str, seq: int, data: dict | None = None, *, turn: str = first, ts: float = 1_759_000_000.0) -> Event:
            return Event(type=kind, data=data or {}, seq=seq, session="visual", turn=turn, ts=ts)

        return [
            event("input.queued", 1, {"queued_id": "hi", "content": [{"text": "hi"}]}, ts=1_759_000_000.0),
            event("turn.started", 2, {}, ts=1_759_000_000.1),
            event("input.consumed", 3, {"queued_id": "hi", "turn": first}, ts=1_759_000_000.2),
            event("model.started", 4, {"provider": "openai", "model": "GPT-6 Luna", "streaming": True}, ts=1_759_000_000.3),
            event("text", 5, {"text": "Hi! What can I help you with?"}, ts=1_759_000_003.5),
            event("model.stopped", 6, {"stop_reason": "end_turn"}, ts=1_759_000_003.5),
            event("turn.completed", 7, {"stop_reason": "end_turn"}, ts=1_759_000_003.5),
            event("input.queued", 8, {"queued_id": "daemon", "content": [{"text": "how to start the daemon"}]}, turn=second, ts=1_759_000_100.0),
            event("turn.started", 9, {}, turn=second, ts=1_759_000_100.1),
            event("input.consumed", 10, {"queued_id": "daemon", "turn": second}, turn=second, ts=1_759_000_100.2),
            event("model.started", 11, {"provider": "openai", "model": "GPT-6 Luna", "streaming": True}, turn=second, ts=1_759_000_100.3),
            event("thinking", 12, {"text": "I’ll check the project setup first."}, turn=second, ts=1_759_000_100.7),
            event("model.stopped", 13, {"stop_reason": "tool_use"}, turn=second, ts=1_759_000_101.0),
            event("tool.requested", 14, {"call_id": "glob", "tool": "Glob", "input": {"pattern": "**/*", "path": "."}}, turn=second, ts=1_759_000_101.1),
            event("tool.completed", 15, {"call_id": "glob", "tool": "Glob", "duration_ms": 88, "result": {"display": "100 matches"}}, turn=second, ts=1_759_000_101.2),
            event("tool.requested", 16, {"call_id": "grep", "tool": "Grep", "input": {"pattern": "daemon|serve", "path": "."}}, turn=second, ts=1_759_000_101.3),
            event("tool.completed", 17, {"call_id": "grep", "tool": "Grep", "duration_ms": 41, "result": {"display": "56 matches"}}, turn=second, ts=1_759_000_101.4),
            event("tool.requested", 18, {"call_id": "read", "tool": "Read", "input": {"path": "README.md", "offset": 90, "limit": 38}}, turn=second, ts=1_759_000_101.5),
            event("tool.completed", 19, {"call_id": "read", "tool": "Read", "duration_ms": 24, "result": {"display": "38 lines"}}, turn=second, ts=1_759_000_101.6),
            event("model.started", 20, {"provider": "openai", "model": "GPT-6 Luna", "iteration": 1, "streaming": True}, turn=second, ts=1_759_000_101.7),
            event("text", 21, {"text": "You usually don’t need to start it manually: running a Nexus command such as `nexus doctor` or `nexus run \"...\"` starts the workspace daemon automatically if it isn’t already running.\n\nTo check it or manage it directly:\n\n```text\nnexus daemon status\nnexus daemon logs\nnexus daemon stop\n```\n\nThe daemon shuts itself down after 300 seconds idle by default."}, turn=second, ts=1_759_000_111.8),
            event("model.stopped", 22, {"stop_reason": "end_turn"}, turn=second, ts=1_759_000_111.8),
            event("turn.completed", 23, {"stop_reason": "end_turn"}, turn=second, ts=1_759_000_111.8),
        ]
    return [
        _event("turn.started", 1),
        _event("input.queued", 2, {"queued_id": "input-1", "content": [{"text": "Give the login flow a quick visual pass."}]}),
        _event("input.consumed", 3, {"queued_id": "input-1", "turn": "turn-1"}),
        _event("model.started", 4, {"provider": "demo", "model": "nexus-small", "streaming": True}),
        _event("thinking", 5, {"text": "I will inspect the current shell and make a focused update."}),
        _event("text", 6, {"text": "I tightened the shell around the conversation and kept the interaction model intact."}),
        _event("tool.requested", 7, {"call_id": "read-1", "tool": "Read", "input": {"path": FIXTURE_PATH}}),
        _event("tool.completed", 8, {"call_id": "read-1", "tool": "Read", "duration_ms": 24, "result": {"content": [{"type": "text", "text": FIXTURE_BEFORE}], "display": "3 lines"}}),
        _event("tool.requested", 9, {"call_id": "edit-1", "tool": "Edit", "input": {"path": FIXTURE_PATH}}),
        _event("tool.completed", 10, {"call_id": "edit-1", "tool": "Edit", "duration_ms": 38, "result": {"display": "updated", "diff": {"path": FIXTURE_PATH, "added_lines": 3, "removed_lines": 2, "hunk": f"--- a/{FIXTURE_PATH}\n+++ b/{FIXTURE_PATH}\n@@ -1,2 +1,3 @@\n-Screen {{\n+Screen {{\n+    background: #0c0b0b;\n     color: #d6d3d1;"}}}),
        _event("text", 11, {"text": "The diff preview remains bounded, and the composer stays ready for the next turn."}),
        _event("turn.completed", 12, {"stop_reason": "end_turn"}),
    ]

def _design_events() -> list[Event]:
    """Every timeline row the redesign touches: numbered prompt with image and
    document chips, a thought headline, tool rows, an inline diff, the agent
    label over the reply, the right-aligned footer, and a live shell tail."""
    one, two = "design-1", "design-2"

    def event(kind: str, seq: int, data: dict | None = None, *, turn: str = one, ts: float = 1_759_000_000.0) -> Event:
        return Event(type=kind, data=data or {}, seq=seq, session="visual", turn=turn, ts=ts)

    prompt = [
        {"text": "Fix the token refresh race in image 1 and follow document 1."},
        {"text": "\n\nAttachment: image 1 · idp-rate-limits.png · image/png · 412000 bytes\n"},
        {"text": "\n\nAttachment: document 1 · refresh-spec.md\n\n" + "Spec line.\n" * 400},
    ]
    hunk = (
        "--- a/src/auth/refresh.py\n+++ b/src/auth/refresh.py\n@@ -10,3 +10,5 @@\n"
        " def refresh(token):\n-    if token.expired:\n+    with _lock:\n+        if token.expired:\n"
        "-        return exchange(token)\n+            return exchange(token)\n     return token"
    )
    return [
        event("input.queued", 1, {"queued_id": "q1", "content": prompt}),
        event("turn.started", 2, {"agent": {"name": "build"}}),
        event("input.consumed", 3, {"queued_id": "q1", "turn": one}),
        event("model.started", 4, {"provider": "anthropic", "model": "claude-opus-5-5", "streaming": True}),
        event("thinking", 5, {"text": "The symptom is a thundering herd on expiry. Every request that sees the expired token starts its own exchange."}),
        event("tool.requested", 6, {"call_id": "r1", "tool": "Read", "input": {"path": "src/auth/refresh.py"}}),
        event("tool.completed", 7, {"call_id": "r1", "tool": "Read", "duration_ms": 12, "result": {"display": "212 lines"}}),
        event("tool.requested", 8, {"call_id": "g1", "tool": "Grep", "input": {"pattern": "refresh(", "path": "src/"}}),
        event("tool.completed", 9, {"call_id": "g1", "tool": "Grep", "duration_ms": 30, "result": {"display": "37 matches"}}),
        event("tool.requested", 10, {"call_id": "e1", "tool": "Edit", "input": {"path": "src/auth/refresh.py"}}),
        event("tool.completed", 11, {"call_id": "e1", "tool": "Edit", "duration_ms": 20, "result": {"display": "updated", "diff": {"path": "src/auth/refresh.py", "added_lines": 3, "removed_lines": 2, "hunk": hunk}}}),
        event("text", 12, {"text": "## Why it refreshed five times\n\n`refresh()` checked `expires_at` with no lock, so every request that saw the expired token started its own exchange.\n\n- added a module lock\n- re-check expiry inside it"}),
        event("model.usage", 13, {"input": 12400, "output": 3100, "cache_read": 52000, "reasoning": 800}),
        event("turn.completed", 14, {"stop_reason": "end_turn"}, ts=1_759_000_041.7),
        event("input.queued", 15, {"queued_id": "q2", "content": [{"text": "Run the auth tests"}]}, turn=two, ts=1_759_000_100.0),
        event("turn.started", 16, {"agent": {"name": "build"}}, turn=two, ts=1_759_000_100.1),
        event("input.consumed", 17, {"queued_id": "q2", "turn": two}, turn=two, ts=1_759_000_100.2),
        event("model.started", 18, {"provider": "anthropic", "model": "claude-opus-5-5", "streaming": True}, turn=two, ts=1_759_000_100.3),
        event("thinking.end", 19, {"signature": "hidden"}, turn=two, ts=1_759_000_100.5),
        event("tool.requested", 20, {"call_id": "b1", "tool": "Bash", "input": {"command": "pytest -q tests/auth"}}, turn=two, ts=1_759_000_101.0),
        event("tool.started", 21, {"call_id": "b1", "tool": "Bash"}, turn=two, ts=1_759_000_101.1),
        event("tool.progress", 22, {"call_id": "b1", "text": "".join(f"tests/auth/test_{name}.py ....\n" for name in ("login", "logout", "refresh", "jobs", "tokens", "scopes"))}, turn=two, ts=1_759_000_102.0),
    ]

def _functional_turn(content: str) -> list[Event]:
    """A real read-file, edit-file turn driven by the submitted prompt.

    Every event carries the same turn id, including the ``input.consumed``
    payload, so the user prompt and the assistant/tool output land in one turn
    in the correct order rather than opening a second turn for the prompt.
    """
    turn = "turn-live"

    def event(kind: str, seq: int, data: dict | None = None) -> Event:
        return _event(kind, seq, data, turn=turn)

    return [
        event("input.queued", 1, {"queued_id": "q-live", "content": [{"text": content}]}),
        event("turn.started", 2),
        event("input.consumed", 3, {"queued_id": "q-live", "turn": turn}),
        event("model.started", 4, {"provider": "demo", "model": "nexus-small", "streaming": True}),
        event("text", 5, {"text": "Reading the file, then applying the requested change."}),
        event("tool.requested", 6, {"call_id": "read-live", "tool": "Read", "input": {"path": FIXTURE_PATH}}),
        event("tool.completed", 7, {"call_id": "read-live", "tool": "Read", "duration_ms": 12, "result": {"content": [{"type": "text", "text": FIXTURE_BEFORE}], "display": "3 lines"}}),
        event("tool.requested", 8, {"call_id": "edit-live", "tool": "Edit", "input": {"path": FIXTURE_PATH}}),
        event("tool.completed", 9, {"call_id": "edit-live", "tool": "Edit", "duration_ms": 20, "result": {"display": "updated", "diff": {"path": FIXTURE_PATH, "added_lines": 1, "removed_lines": 0, "hunk": f"--- a/{FIXTURE_PATH}\n+++ b/{FIXTURE_PATH}\n@@ -1,2 +1,3 @@\n Screen {{\n+    background: #0c0b0b;\n     color: #d6d3d1;"}}}),
        event("text", 10, {"text": "Edit applied."}),
        event("turn.completed", 11, {"stop_reason": "end_turn"}),
    ]

def _record_submission(content: str) -> None:
    """Append a submitted prompt to ``NEXUS_VISUAL_SUBMIT_LOG`` when set.

    This gives the browser check an app-level signal for what the shell actually
    submitted (the xterm helper textarea is always empty), including embedded
    newlines. One JSON string per line keeps multi-line drafts unambiguous.
    """
    path = os.environ.get("NEXUS_VISUAL_SUBMIT_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(content) + "\n")

class DemoTransport:
    """A deterministic host transport shared by every visual fixture."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.events_log = _seed_events(state)
        self._live: list[Event] = []
        self._started = False

    async def request(self, command: p.Command) -> p.Result:
        if isinstance(command, p.Health):
            return p.HealthResult(sessions=1)
        if isinstance(command, p.Doctor):
            return p.DoctorResult(report={"status": "ok", "checks": []})
        if isinstance(command, p.SessionOpen):
            return p.SessionOpenResult(session=SimpleNamespace(id=command.session))
        if isinstance(command, p.SessionState):
            return p.SessionStateResult(session=command.session, seq=len(self.events_log), view={})
        if isinstance(command, p.AgentCurrent):
            return p.AgentCurrentResult(
                session=command.session,
                name="build",
                source="visual",
                provider="OpenAI",
                model="GPT-6 Luna",
                reasoning_effort="not exposed",
                thinking_budget=2048,
            )
        if isinstance(command, p.AgentsList):
            return p.AgentsListResult(agents=[
                {"name": "general", "description": "General implementation", "contexts": ["root"]},
                {"name": "build", "description": "Focused product changes", "contexts": ["root"]},
                {"name": "explore", "description": "Read-only investigation", "contexts": ["root"]},
            ])
        if isinstance(command, p.ContextInspect):
            return p.ContextInspectResult(
                session=command.session,
                agent={"name": "build"},
                system_text="You are a pragmatic coding assistant.\nRead the workspace before editing.",
                tools=[
                    {"name": "bash", "group": "bash", "description": "Run a command", "input_schema": {"type": "object"}},
                    {"name": "BashOutput", "group": "bash", "description": "Read command output", "input_schema": {"type": "object"}},
                    {"name": "Read", "group": "Read", "description": "Read a file", "input_schema": {"type": "object"}},
                    {"name": "Edit", "group": "Edit", "description": "Edit a file", "input_schema": {"type": "object"}},
                ],
                skills_index=[{"name": "prompt-toolkit", "description": "Terminal skill", "included": True}],
                mcp_servers=[{"name": "cvc", "status": "connected", "tool_count": 4, "tools": ["search"]}],
            )
        if isinstance(command, p.SessionStart):
            # A functional turn: later subscribers replay the scripted events.
            _record_submission(command.content)
            self._live = _functional_turn(command.content)
            self._started = True
            return p.SessionStartResult(session=command.session, turn_id="turn-live")
        if isinstance(command, p.SessionCancel):
            return p.SessionCancelResult(session=command.session, cancelled=True)
        if isinstance(command, p.PermissionResolve):
            return p.PermissionResolveResult(session=command.session, request_id=command.request_id, resolved=True)
        raise AssertionError(f"visual fixture received unsupported command: {command!r}")

    async def _stream(
        self, _session: str, from_seq: int = 0, *, follow: bool = True, **_kwargs
    ) -> AsyncIterator[Event]:
        for event in self.events_log:
            if event.seq > from_seq:
                yield event
        if not follow:
            return
        # A live subscription is opened before ``SessionStart`` is issued, so
        # wait briefly for the scripted functional turn to be armed, then
        # deliver it. Bound the wait so a state that never starts still ends.
        deadline = time.monotonic() + 5
        while not self._started and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        for event in self._live:
            if event.seq > from_seq:
                yield event

    def events(self, session: str, from_seq: int = 0, **kwargs) -> AsyncIterator[Event]:
        return self._stream(session, from_seq, **kwargs)

    async def aclose(self) -> None:
        return None

def subagent_fixture():
    """Identical recorded child conversation for the native terminal."""
    from nexus.view import fold
    from nexus.view.model import AgentView
    body = fold(_seed_events("transcript"))
    agent = AgentView(id="visual-child", type="advisor", task="Inspect the UI", description="Inspect the UI",
                      status="completed", model="nexus-small", body=body)
    context = {"system_text": "You are an advisor. Inspect the UI and report what matters.",
               "agent": {"name": "advisor", "color": "#86b97a"}, "model": "nexus-small",
               "messages": [{"blocks": [{"text": "Inspect the UI"}]}], "tools": []}
    return agent, context