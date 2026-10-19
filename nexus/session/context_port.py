"""Bounded, redacted, read-only projections of another session's context (plan 03 §3.9).

``ContextReadPort`` is the host side of the CodeMode context namespace. Every
operation first runs :meth:`ContextAuthorizer.check` (so an edge that ended or a
trashed ancestor stops the read), then reads the target's log through the
store's seq-addressed ``record_at`` API in bounded windows. A handoff is a
segment boundary inside one append-only log, so a seq range spans all segments
of the target's lineage; ``meta`` reports the segment count.

Bounds: a read window is at most ``MAX_WINDOW`` (200) seqs and returns at most
``MAX_ITEMS`` (100) rows; ``search`` scans at most ``MAX_SEARCH_RECORDS`` records
and ``MAX_SEARCH_BYTES`` of text and returns at most ``MAX_HITS`` (50) hits;
every text is redacted, control-stripped and clipped. Results are marked
``untrusted``: they are data written by another agent, never instructions.
No UI or host projection is consulted.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..model.message import Text, ToolResult, ToolUse
from ..model.tokenizer import DEFAULT_TOKENIZER
from ..util import redact_secrets
from .context_authz import ContextAuthorizer
from .db import SqliteSessionStore
from .records import EventRecord, MessageRecord, SummaryRecord

MAX_WINDOW = 200
MAX_ITEMS = 100
MAX_HITS = 50
MAX_SEARCH_RECORDS = 5_000
MAX_SEARCH_BYTES = 512 * 1024
MAX_RESULT_LOOKAHEAD = 50
TEXT_LIMIT = 8_000
REPORT_LIMIT = 16_000
SEARCH_WINDOW = 300
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_text(value: object, limit: int) -> str:
    """Redacted, control-stripped, clipped text (newlines are kept); clipping is announced."""
    if not isinstance(value, str):
        return ""
    text = redact_secrets(_CONTROL.sub(" ", value))
    if len(text) > limit:
        return text[:limit] + f"... [clipped, {len(text) - limit} more chars]"
    return text


def redact_value(value: object) -> object:
    """Bounded, redacted JSON-safe copy of a tool input."""
    if isinstance(value, str):
        return safe_text(value, 1024)
    if isinstance(value, dict):
        return {safe_text(str(k), 128): redact_value(v) for k, v in list(value.items())[:32]}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value[:32]]
    return value if value is None or isinstance(value, (bool, int, float)) else None


def _text_of(blocks) -> str:
    return "\n".join(b.text for b in blocks if isinstance(b, Text))


class ContextReadPort:
    def __init__(self, manager, *, authorizer: ContextAuthorizer | None = None,
                 report_reader: Callable[[str], str] | None = None):
        self.manager = manager
        self.auth = authorizer or ContextAuthorizer(manager)
        #: Optional override (tests/hosts); by default the port reads the report itself.
        self.report_reader = report_reader

    # -- helpers -----------------------------------------------------------

    def _store(self, row: dict[str, Any]) -> SqliteSessionStore:
        store = self.manager.store
        return SqliteSessionStore(store.db, store.project_id, row["namespace"], register_project=False)

    @staticmethod
    def _window(start: object, stop: object, last_seq: int) -> tuple[int, int]:
        if (type(start) is not int or type(stop) is not int or start < 0 or stop < start):
            raise ValueError("invalid context range")
        return start, min(stop, start + MAX_WINDOW, last_seq + 1)

    def _envelope(self, row: dict[str, Any], data: object, **extra: Any) -> dict[str, Any]:
        return {"untrusted": True, "target": row["logical_id"], "name": row.get("name"),
                "source": "session-context", **extra, "data": data}

    def _tokens_estimate(self, store: SqliteSessionStore, row: dict[str, Any]) -> int:
        """Tail sample scaled by message count; an estimate, labelled as such."""
        tail = store.read_tail(row["id"], max_records=MAX_WINDOW).records
        messages = [r for r in tail if isinstance(r, MessageRecord)]
        if not messages:
            return 0
        sampled = sum(DEFAULT_TOKENIZER.count_tokens(_text_of(m.message.content)) +
                      sum(DEFAULT_TOKENIZER.count_tokens(_text_of(b.content))
                          for b in m.message.content if isinstance(b, ToolResult))
                      for m in messages)
        total = max(int(row.get("message_count") or 0), len(messages))
        return int(sampled * total / len(messages))

    # -- operations --------------------------------------------------------

    def meta(self, caller: str, target: str) -> dict[str, Any]:
        row = self.auth.check(caller, target)
        store = self._store(row)
        data = {"name": row.get("name"), "kind": row.get("kind") or None,
                "segments": int(row.get("handoff_count") or 0) + 1,
                "records": int(row.get("last_seq") or 0),
                "tokens_estimate": self._tokens_estimate(store, row),
                "status": row.get("lifecycle") or row.get("state") or "idle"}
        return self._envelope(row, data)

    def messages(self, caller: str, target: str, start: int = 0, stop: int = 100) -> dict[str, Any]:
        row = self.auth.check(caller, target)
        store = self._store(row)
        start, end = self._window(start, stop, int(row.get("last_seq") or 0))
        items = []
        for seq in range(start, end):
            record = store.record_at(row["id"], seq)
            if isinstance(record, MessageRecord):
                text = safe_text(_text_of(record.message.content), TEXT_LIMIT)
                if text:
                    items.append({"seq": seq, "role": record.message.role, "text": text})
            if len(items) >= MAX_ITEMS:
                end = seq + 1
                break
        return self._envelope(row, items, cursor={"start": start, "stop": end,
                              "more": end <= int(row.get("last_seq") or 0)})

    def tool_calls(self, caller: str, target: str, start: int = 0, stop: int = 100) -> dict[str, Any]:
        """One row per ToolUse, joined with its ToolResult (looked ahead a bounded distance)."""
        row = self.auth.check(caller, target)
        store = self._store(row)
        last = int(row.get("last_seq") or 0)
        start, end = self._window(start, stop, last)
        pending: dict[str, dict[str, Any]] = {}
        items: list[dict[str, Any]] = []

        def visit(seq: int, only_results: bool) -> None:
            record = store.record_at(row["id"], seq)
            if not isinstance(record, MessageRecord):
                return
            for block in record.message.content:
                if isinstance(block, ToolUse) and not only_results and len(items) < MAX_ITEMS:
                    item = {"seq": seq, "tool": safe_text(block.name, 128),
                            "input_summary": redact_value(block.input),
                            "is_error": None, "result_excerpt": ""}
                    pending[block.id] = item
                    items.append(item)
                elif isinstance(block, ToolResult):
                    item = pending.pop(block.tool_use_id, None)
                    if item is not None:
                        item["is_error"] = bool(block.is_error)
                        item["result_excerpt"] = safe_text(_text_of(block.content), TEXT_LIMIT)

        for seq in range(start, end):
            visit(seq, False)
            if len(items) >= MAX_ITEMS:
                end = seq + 1
                break
        for seq in range(end, min(end + MAX_RESULT_LOOKAHEAD, last + 1)):
            if not pending:
                break
            visit(seq, True)
        return self._envelope(row, items, cursor={"start": start, "stop": end, "more": end <= last})

    def search(self, caller: str, target: str, needle: str, limit: int = 50) -> dict[str, Any]:
        row = self.auth.check(caller, target)
        if not isinstance(needle, str) or not needle or len(needle) > 256:
            raise ValueError("needle must be a non-empty string of at most 256 characters")
        limit = max(0, min(int(limit), MAX_HITS))
        store = self._store(row)
        last = int(row.get("last_seq") or 0)
        hits: list[dict[str, Any]] = []
        scanned_bytes = 0
        seq = 0
        stop_at = min(last, MAX_SEARCH_RECORDS)
        for seq in range(1, stop_at + 1):
            if len(hits) >= limit or scanned_bytes >= MAX_SEARCH_BYTES:
                break
            record = store.record_at(row["id"], seq)
            if not isinstance(record, MessageRecord):
                continue
            text = _text_of(record.message.content)
            scanned_bytes += len(text)
            at = text.find(needle)
            if at >= 0:
                window = text[max(0, at - SEARCH_WINDOW // 3): at + SEARCH_WINDOW * 2 // 3]
                hits.append({"seq": seq, "role": record.message.role, "text": safe_text(window, SEARCH_WINDOW)})
        complete = seq >= last and len(hits) < limit and scanned_bytes < MAX_SEARCH_BYTES
        return self._envelope(row, hits, cursor={"scanned_through": seq, "complete": bool(complete or last == 0)})

    def report(self, caller: str, target: str) -> dict[str, Any]:
        """The child's final report: its last assistant text, else its handoff digest."""
        row = self.auth.check(caller, target)
        if self.report_reader is not None:
            text = self.report_reader(row["logical_id"])
        else:
            text = self._read_report(row)
        return self._envelope(row, safe_text(text, REPORT_LIMIT))

    def _read_report(self, row: dict[str, Any]) -> str:
        store = self._store(row)
        for record in reversed(store.read_tail(row["id"], max_records=MAX_WINDOW).records):
            if isinstance(record, MessageRecord) and record.message.role == "assistant":
                text = _text_of(record.message.content).strip()
                if text:
                    return text
            elif isinstance(record, EventRecord) and record.event.type == "turn.failed":
                break
        head = store.context_head(row["id"])
        return head.text if isinstance(head, SummaryRecord) else ""
