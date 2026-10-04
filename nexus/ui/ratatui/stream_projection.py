"""Changed-tail presentation for ordinary stream deltas (responsiveness plan §4).

Other events use the full projection, preserving prompt/chrome and child-page
contracts. Stable root history is neither traversed nor formatted per token.
"""
from __future__ import annotations

STREAM_EVENTS = frozenset({"text.delta", "thinking.delta", "text", "thinking"})


class StreamProjection:
    def __init__(self):
        self.snapshot = None
        self.start = None
        self.turn = None
        self.identity = None

    def remember(self, snapshot, controller, shell):
        self.snapshot = snapshot
        self.start = None
        self.identity = (shell.generation, shell.agent_page if hasattr(shell, "agent_page") else "")
        if not controller.view.turns or snapshot.get("agent_page"):
            return
        self.turn = controller.view.turns[-1].id
        blocks = snapshot["blocks"]
        start = len(blocks)
        while start and blocks[start-1].get("turn_id") == self.turn:
            start -= 1
        if start < len(blocks):
            self.start = start

    def project(self, controller, shell, revision, turn_id, project_turn):
        if self.snapshot is None or self.start is None or shell.generation != self.snapshot["generation"] or self.snapshot.get("agent_page"):
            return None
        if not controller.view.turns or controller.view.turns[-1].id != self.turn or turn_id != self.turn:
            return None
        _, tail = project_turn(controller.view.turns[-1], shell, controller.view.agents, literal=False)
        if self.start and tail and tail[0].get("gap", 0) < 1:
            from .prototype import _revision
            tail[0] = {**tail[0], "gap": 1}
            tail[0]["rev"] = _revision(tail[0])
        blocks = self.snapshot["blocks"]
        offset = 0
        while offset < min(len(tail), len(blocks)-self.start) and tail[offset]["rev"] == blocks[self.start+offset]["rev"]:
            offset += 1
        blocks[self.start+offset:] = tail[offset:]
        snapshot = {**self.snapshot, "blocks": blocks, "revision": revision,
                    "status": controller.view.phase, "changed_from": self.start+offset}
        from ...ui_support.context import thinking_status
        from ...ui_support.text import escape_controls, redact
        activity = redact(escape_controls(thinking_status(controller.view)))
        panel = snapshot["details_panel"]
        rows = [row for row in panel.get("session", []) if row[0] != "Activity"]
        if activity:
            rows.append(["Activity", activity])
        snapshot["details_panel"] = {**panel, "session": rows}
        self.snapshot = snapshot
        return snapshot
