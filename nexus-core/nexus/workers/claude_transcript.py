"""Bounded, incremental readback of Claude Code's untrusted JSONL transcript.

Ported deliberately from nexus01's worker; raw entries precede interpretation.
No credential files or transcript-provided paths are read here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import WorkerError, WorkerEvent

MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


class TranscriptReader:
    def __init__(self, path: Path, session: str, *, offset: int = 0):
        self.path, self.session, self.offset = path, session, offset
        self.pending = bytearray()
        self.total = 0
        self.identity: tuple[int, int] | None = None
        self.seen: set[str] = set()

    def poll(self) -> list[WorkerEvent]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return []
        identity = stat.st_dev, stat.st_ino
        if (self.identity is not None and identity != self.identity) or stat.st_size < self.offset:
            raise WorkerError("Claude transcript was rewritten or truncated; refusing to replay it")
        self.identity = identity
        with self.path.open("rb") as source:
            source.seek(self.offset)
            chunk = source.read(min(1024 * 1024, MAX_TOTAL_BYTES - self.total + 1))
        self.offset += len(chunk)
        self.total += len(chunk)
        if self.total > MAX_TOTAL_BYTES:
            raise WorkerError("Claude transcript size limit reached; later activity was not recorded")
        self.pending.extend(chunk)
        events = []
        while (cut := self.pending.find(b"\n")) >= 0:
            if cut > MAX_LINE_BYTES:
                raise WorkerError("Claude transcript line limit reached")
            line = bytes(self.pending[:cut])
            del self.pending[:cut + 1]
            if line.strip():
                events.extend(self.parse(line))
        if len(self.pending) > MAX_LINE_BYTES:
            raise WorkerError("Claude transcript line limit reached")
        return events

    def parse(self, raw: bytes) -> list[WorkerEvent]:
        events = [WorkerEvent("wire", raw=raw)]
        try:
            entry = json.loads(raw)
        except (ValueError, RecursionError):
            return events + [WorkerEvent("notice", {"text": "malformed Claude transcript line was not interpreted"})]
        if not isinstance(entry, dict):
            return events + [WorkerEvent("notice", {"text": "unknown Claude transcript shape"})]
        if entry.get("isSidechain") or entry.get("sessionId") not in (None, self.session):
            return events
        key = entry.get("uuid")
        if isinstance(key, str):
            if key in self.seen:
                return events
            self.seen.add(key)
        kind = entry.get("type")
        message = entry.get("message")
        if kind in ("assistant", "user") and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                return events + [WorkerEvent("notice", {"text": "unknown Claude message content"})]
            if kind == "assistant":
                events.append(WorkerEvent("assistant", {"message_id": message.get("id"), "parts": content,
                                                        "usage": message.get("usage"), "raw": entry}))
            else:
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_result":
                        output = part.get("content", "")
                        if not isinstance(output, str):
                            output = json.dumps(output, ensure_ascii=True)
                        events.append(WorkerEvent("tool.result", {"id": part.get("tool_use_id"), "output": output,
                                                                  "is_error": bool(part.get("is_error")), "raw": entry}))
                    elif part.get("type") == "text":
                        events.append(WorkerEvent("context", {"text": part.get("text", ""), "raw": entry}))
        elif kind == "attachment" and isinstance(entry.get("attachment"), dict):
            attachment = entry["attachment"]
            if isinstance(attachment.get("rendered"), str):
                events.append(WorkerEvent("context", {"text": attachment["rendered"], "raw": entry}))
        else:
            events.append(WorkerEvent("notice", {"text": f"Claude transcript entry {str(kind)[:80]!r} is wire-only; CLI context is incomplete"}))
        return events


MAX_SUBAGENTS = 16
MAX_SUBAGENT_ENTRIES = 200
MAX_SUBAGENT_TEXT = 4000
_AGENT_FILE = re.compile(r"agent-([A-Za-z0-9_-]{1,64})\.jsonl")


def _clip(text: str, limit: int = MAX_SUBAGENT_TEXT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[clipped {len(text) - limit} characters]"


def sidechain_entries(entry: dict) -> list[dict]:
    """The readable part of one subagent transcript entry: prompt, text, tool call, tool result. Wire noise is dropped."""
    message = entry.get("message")
    if entry.get("type") not in ("assistant", "user") or not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and not part["text"].lstrip().startswith("<system-reminder>"):
            out.append({"kind": "prompt" if entry["type"] == "user" else "text", "text": _clip(part["text"])})
        elif kind == "tool_use":
            out.append({"kind": "tool", "id": str(part.get("id", ""))[:80], "name": str(part.get("name", ""))[:80],
                        "input": _clip(json.dumps(part.get("input", {}), ensure_ascii=False), 1000)})
        elif kind == "tool_result":
            output = part.get("content", "")
            if isinstance(output, list):
                output = "".join(b.get("text", "") for b in output if isinstance(b, dict))
            out.append({"kind": "result", "id": str(part.get("tool_use_id", ""))[:80], "output": _clip(str(output)),
                        "error": bool(part.get("is_error"))})
    return out


class SubagentReader:
    """Claude's own subagents: `<session>/subagents/agent-<id>.jsonl` plus `.meta.json`, read-only.

    The meta file's `toolUseId` names the parent call (usually `Agent`) whose card shows the nested entries.
    Files stay under the transcript's own directory; nothing here is taken from entry content.
    """

    def __init__(self, transcript: Path):
        self.dir = transcript.with_suffix("") / "subagents"
        self.offsets: dict[str, int] = {}
        self.pending: dict[str, bytearray] = {}
        self.entries: dict[str, list[dict]] = {}
        self.meta: dict[str, dict] = {}
        self.sent: dict[str, tuple] = {}

    def poll(self) -> list[WorkerEvent]:
        try:
            names = sorted(p.name for p in self.dir.iterdir() if _AGENT_FILE.fullmatch(p.name) and p.is_file() and not p.is_symlink())
        except OSError:
            return []
        events = []
        for name in names[:MAX_SUBAGENTS]:
            agent = _AGENT_FILE.fullmatch(name)[1]
            meta_path = self.dir / f"agent-{agent}.meta.json"
            try:
                if meta_path.is_file() and not meta_path.is_symlink():
                    value = json.loads(meta_path.read_bytes()[:65536])
                    if isinstance(value, dict):
                        self.meta[agent] = value
                with (self.dir / name).open("rb") as source:
                    source.seek(self.offsets.get(agent, 0))
                    chunk = source.read(1024 * 1024)
            except (OSError, ValueError):
                continue
            self.offsets[agent] = self.offsets.get(agent, 0) + len(chunk)
            pending = self.pending.setdefault(agent, bytearray())
            pending.extend(chunk)
            items = self.entries.setdefault(agent, [])
            while (cut := pending.find(b"\n")) >= 0:
                line = bytes(pending[:cut])
                del pending[:cut + 1]
                try:
                    entry = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(entry, dict) and len(items) < MAX_SUBAGENT_ENTRIES:
                    items.extend(sidechain_entries(entry))
            meta = self.meta.get(agent, {})
            state = (len(items), bool(meta.get("finished")))
            if self.sent.get(agent) != state and isinstance(meta.get("toolUseId"), str):
                self.sent[agent] = state
                events.append(WorkerEvent("subagent", {
                    "tool_use_id": meta["toolUseId"][:80], "agent_id": agent, "agent_type": str(meta.get("agentType", ""))[:80],
                    "description": str(meta.get("description", ""))[:300], "finished": bool(meta.get("finished")),
                    "entries": list(items[:MAX_SUBAGENT_ENTRIES]), "clipped": len(items) >= MAX_SUBAGENT_ENTRIES}))
        return events
