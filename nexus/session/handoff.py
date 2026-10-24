"""Atomic same-session context cutover on the existing append transaction."""
from __future__ import annotations

import contextlib
import hashlib
import time

from ..context.handoff import BRIEF_VERSION, build_brief, fit_brief, safe_brief
from ..events import Event
from ..model.message import ToolResult, ToolUse
from ..util import new_id
from ..errors import SessionBusy
from .records import EventRecord, SummaryRecord
from .submissions import submission_digest, validate_request_id


def handoff(handle, *, summary=None, kept_from_seq=None, reason="manual",
            request_id="", continuation=None, _within_turn=False,
            _summary_fallback=False, _brief_limit=None, _scheduled=False,
            _tokens_before=None, _kept_tokens=None, _kernel_manifest=""):
    from ..context.parts import canonical_message_text
    from ..model.tokenizer import DEFAULT_TOKENIZER
    from .session import _coerce_user_input, _encode_queued_content

    handle._ensure_writable()
    if request_id:
        validate_request_id(request_id)
    digest = submission_digest("handoff", {"summary": summary,
        "kept_from_seq": kept_from_seq, "continuation": continuation}, "queue", None)
    def previous():
        prior = handle._store.submission(handle.id, request_id, digest) if request_id else None
        if prior is not None:
            return {"queued_id": prior[3], "turn_id": prior[2], "fresh": False}
        return None
    prior = previous()
    if prior is not None:
        return prior
    if _scheduled:
        raise SessionBusy("Cannot handoff a scheduled or active turn")
    if _within_turn:
        if handle._active is None:
            raise SessionBusy("Automatic handoff requires the turn lease")
        guard = contextlib.nullcontext()
    else:
        if handle._active is not None:
            raise SessionBusy("Cannot handoff an active session")
        guard = handle._lock.exclusive(blocking=False)
    with guard:
        prior = previous()
        if prior is not None:
            return prior
        read = handle.read(force=True)
        messages, seqs = handle.active_messages()
        boundary = read.next_seq + 1 if kept_from_seq is None else kept_from_seq
        if type(boundary) is not int or boundary < 1 or boundary > read.next_seq + 1:
            raise ValueError("Invalid handoff boundary")
        if seqs and boundary <= min(seqs):
            raise ValueError("Handoff must retire context")
        prior_head = handle._store.context_head(handle.id)
        if prior_head is not None and prior_head.strategy == "handoff" and boundary <= prior_head.kept_from_seq:
            return {"handoff_id": prior_head.handoff_id, "queued_id": "", "turn_id": "", "fresh": False}
        candidate = next((m for m, seq in zip(messages, seqs, strict=True) if seq == boundary), None)
        if boundary != read.next_seq + 1 and (candidate is None or (
            candidate.role == "user" and any(isinstance(b, ToolResult) for b in candidate.content))):
            raise ValueError("Handoff requires a pair-safe message boundary (tool calls)")
        pending = set()
        for message, seq in zip(messages, seqs, strict=True):
            if seq >= boundary:
                break
            for block in message.content:
                if isinstance(block, ToolUse):
                    pending.add(block.id)
                elif isinstance(block, ToolResult):
                    pending.discard(block.tool_use_id)
        if pending:
            raise ValueError("Handoff boundary splits unresolved tool calls")
        retired = [m for m, seq in zip(messages, seqs, strict=True) if seq < boundary]
        deterministic = build_brief(retired, handle=handle, boundary=boundary)
        brief = safe_brief(summary) if summary is not None else deterministic
        if reason == "overflow" and summary is not None:
            brief = safe_brief(deterministic[:4000] + "\nModel recall (untrusted):\n" + brief[:1500])
        if _kernel_manifest:
            # Code-mode kernels of this session (plan code-mode-kernel §3.9): names, types and sizes only,
            # bounded by the manifest itself, so the brief keeps them for the model after the cutover.
            brief = safe_brief(brief + "\n" + _kernel_manifest)
        if _brief_limit is not None and len(brief) > _brief_limit:
            # Keep every section, shortened evenly. Only a budget too small for
            # that falls back to the first user intent. Full state stays
            # readable in this session's transcript.
            fitted = fit_brief(brief, _brief_limit)
            if fitted is None:
                intent = next((b.text for m in retired for b in m.content
                               if m.role == "user" and hasattr(b, "text")), brief)
                fitted = safe_brief(intent)[:max(1, _brief_limit - 14)] + "\n[truncated]"
            brief = fitted
        # The assembler passes its projection counts (environment counter). A
        # direct call has none, so use the default tokenizer over the same
        # canonical text the assembler counts; never a character heuristic.
        tokens_before = _tokens_before if _tokens_before is not None else sum(
            DEFAULT_TOKENIZER.count_tokens(canonical_message_text(m)) for m in messages)
        kept_tokens = _kept_tokens if _kept_tokens is not None else sum(
            DEFAULT_TOKENIZER.count_tokens(canonical_message_text(m))
            for m, seq in zip(messages, seqs, strict=True) if seq >= boundary)
        tokens_after = DEFAULT_TOKENIZER.count_tokens(brief) + kept_tokens
        hid = hashlib.sha256(f"{handle.id}:{read.next_seq}:{boundary}:{BRIEF_VERSION}".encode()).hexdigest()[:16]
        queued_id, turn_id = "", ""
        stamp = time.time()
        factories = [lambda seq: SummaryRecord(seq=seq, text=brief,
            strategy="handoff", summary_id=hid, handoff_id=hid,
            brief_version=BRIEF_VERSION, kept_from_seq=boundary,
            source_from_seq=min(seqs, default=0), source_to_seq=boundary - 1,
            ts=stamp)]
        def marker(seq):
            return EventRecord(seq=seq, ts=stamp, event=Event(
                type="context.handoff", seq=seq, ts=stamp, session=handle.id,
                data={"handoff_id": hid, "head_seq": seq - 1,
                      "summary_seq": seq - 1, "through_seq": read.next_seq,
                      "brief_chars": len(brief), "method": "summarized" if summary is not None and reason == "overflow" else "deterministic",
                      "preview": brief.encode("utf-8")[:2048].decode("utf-8", errors="ignore"),
                      "tokens_before": int(tokens_before),
                      "tokens_after": int(tokens_after),
                      "kept_from_seq": boundary, "brief_version": BRIEF_VERSION,
                      "reason": reason, "summary_fallback": _summary_fallback}))
        factories.append(marker)
        indexes = []
        if continuation is not None:
            blocks = _coerce_user_input(continuation)
            queued_id, turn_id = new_id(), new_id()
            def queued(seq):
                return EventRecord(seq=seq, ts=stamp, event=Event(
                    type="input.queued", seq=seq, ts=stamp, session=handle.id,
                    data={"queued_id": queued_id, "turn_id": turn_id,
                          "mode": "queue", "content": _encode_queued_content(blocks),
                          "queue_depth": handle.queue_depth + 1}))
            factories.append(queued)
        if request_id:
            indexes.append(dict(request_id=request_id, content_digest=digest,
                queued_id=queued_id, turn_id=turn_id, created_at=stamp))
        records = handle._store.append_batch(handle.id, factories, index_rows=indexes)
        if not records:
            return previous()
        handle._read = None
        handle._rehydrate_queue()
        for record in records:
            if isinstance(record, EventRecord):
                handle._observe_event(record.event)
                handle._publish(record.event)
        return {"handoff_id": hid, "queued_id": queued_id,
                "turn_id": turn_id, "fresh": True}
