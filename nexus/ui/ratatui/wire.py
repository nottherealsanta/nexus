"""Section deltas for the terminal bridge (TUI_LOCAL_INTERACTION_PLAN §2).

Schema 3 omissions retain prior values. Dependent transcript suffixes are ordered;
reset snapshots establish a new session/page. One-shot composer effects always send.
"""
from __future__ import annotations


class TerminalWire:
    def __init__(self):
        self.previous = {}
        self.blocks = []
        self.identity = None
        self.block_count = 0

    def encode(self, snapshot):
        identity = (snapshot.get("generation"), snapshot.get("agent_page", ""))
        reset = identity != self.identity
        wire = {"schema": 3, "revision": snapshot["revision"], "generation": snapshot["generation"]}
        if reset:
            wire["reset"] = True
        excluded = {"schema", "revision", "generation", "blocks", "blocks_from", "changed_from", "event_sent_at", "event_kind"}
        for key, value in snapshot.items():
            if key in excluded:
                continue
            prior = self.previous.get(key)
            if reset or value != prior or key in {"restore", "insert"} and value:
                if key == "history" and not reset and prior is not None and value[:len(prior)] == prior:
                    wire["history_append"] = value[len(prior):]
                else:
                    wire[key] = value
        blocks = snapshot["blocks"]
        start = 0
        if not reset:
            if "changed_from" in snapshot:
                start = min(snapshot["changed_from"], self.block_count, len(blocks))
            else:
                while start < min(len(blocks), len(self.blocks)) and (blocks[start] is self.blocks[start] or
                        (blocks[start].get("id"), blocks[start].get("rev")) == (self.blocks[start].get("id"), self.blocks[start].get("rev"))):
                    start += 1
        if reset or start < len(blocks) or len(blocks) != self.block_count:
            wire.update(blocks_from=start, blocks=blocks[start:])
        if len(wire) == 3:
            return None
        if snapshot.get("event_sent_at"):
            wire["event_sent_at"] = snapshot["event_sent_at"]
            wire["event_kind"] = snapshot.get("event_kind", "")
        self.previous = {key: value for key, value in snapshot.items() if key not in excluded}
        self.blocks = blocks
        self.block_count = len(blocks)
        self.identity = identity
        return wire
