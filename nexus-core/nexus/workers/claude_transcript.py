"""Bounded, incremental readback of Claude Code's untrusted JSONL transcript.

Ported deliberately from nexus01's worker; raw entries precede interpretation.
No credential files or transcript-provided paths are read here.
"""
from __future__ import annotations

import json
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
