"""Manual continuation from a session's existing context (PLAN.md Step 11).

Continuation is an explicit client action. It starts a normal turn without a new
user message: the stored system prompt and messages are sent unchanged, and the
new provider request, response and any tool calls are recorded like any other
turn. Reconnecting, re-reading a session and daemon startup never continue a
session by themselves; startup recovery reports what a crash left unfinished.

Before a continuation request can be sent, the stored conversation must be valid
for a provider: every saved tool call needs a result. `reconcile` closes calls a
crash left dangling, using only durable evidence:

| evidence | outcome |
| --- | --- |
| `turn_tool` `finished` | its completed result is preserved |
| `turn_tool` `started` | it may have run; the result is unknown and it is not re-run |
| `turn_tool` `saved` | it never ran |
| pending/denied/cancelled approval or question | did not run, or its durable answer is restored |
| no execution record at all | labelled unknown; Nexus cannot tell whether it ran |

The repair is one transaction: one tool message with the results and one labelled
``[Nexus recovery: continuation reconciled interrupted tool calls]`` user notice.
Repeating a continuation therefore finds no unresolved call and appends nothing
again. Partial provider output remains diagnostic history in the event log and is
never reconstructed into arguments or reasoning.

The new turn records its provenance in `turn.data`:
`{"origin": "continuation", "continuation": {"predecessor": {...},
"reconciliation": {...}}}`. The predecessor names the preceding turn and its last
request when they exist; a legacy conversation without execution records says so
instead of inventing a link. This is execution/API metadata, not an extra
model-visible user message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .llmhub.types import Message, Text, ToolCall, ToolResult
from .recovery import OUTCOMES, classify_call, open_approvals, open_questions, question_answers
from .store import Store, StoredMessage
from .tools.base import clip

NOTICE_CHARS = 16_000
NOTICE_HEADER = "[Nexus recovery: continuation reconciled interrupted tool calls]"


class ContextInvalid(ValueError):
    """The saved context cannot become provider-valid without rewriting history."""


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The durable repair appended before a continuation request."""

    calls: list[dict[str, Any]]
    tool_message: StoredMessage
    notice: StoredMessage

    def json(self) -> dict[str, Any]:
        return {"calls": self.calls, "tool_message": self.tool_message.id, "notice": self.notice.id}


def predecessor(store: Store, session_id: str) -> dict[str, Any]:
    """The turn and request this continuation follows, or an honest missing link."""
    turns = store.turns_for_session(session_id)
    if not turns:
        return {
            "turn_id": None, "request_id": None, "status": None, "request_status": None,
            "step": None, "stop": None, "link": "none", "legacy": False,
            "note": ("No durable turn or request record exists for the preceding conversation, so this "
                     "continuation cannot be linked to a specific attempt."),
        }
    turn = turns[-1]
    requests = store.requests_for_turn(turn["id"])
    request = requests[-1] if requests else None
    legacy = bool(turn["data"].get("legacy"))
    note = None
    if legacy:
        note = ("The preceding conversation was reconstructed from context saved before execution records "
                "existed; the exact attempt before this continuation is unknown.")
    return {
        "turn_id": turn["id"],
        "request_id": request["id"] if request else None,
        "status": turn["status"],
        "request_status": request["status"] if request else None,
        "step": request["step"] if request else None,
        "stop": request["stop"] if request else None,
        "link": "request" if request else "turn",
        "legacy": legacy,
        "note": note,
    }


def unresolved_calls(messages: list[StoredMessage]) -> list[tuple[int, ToolCall]]:
    """Calls in saved messages with no later result, matched in conversation order.

    Matching in order matters: providers are not guaranteed to use a call id only
    once across a conversation, so a result from an earlier turn must not satisfy a
    later call that reuses the id.
    """
    pending: list[tuple[int, ToolCall]] = []
    last_assistant: int | None = None
    for index, stored in enumerate(messages):
        role = stored.message.role
        if role == "tool":
            done = {part.call_id for part in stored.message.parts if isinstance(part, ToolResult)}
            # Only the assistant message these results follow can be closed here; a
            # result that arrives after a later assistant or user message is stale.
            pending = [(position, call) for position, call in pending
                       if not (position == last_assistant and call.id in done)]
            continue
        if role != "assistant":
            last_assistant = None
            continue
        last_assistant = index
        for part in stored.message.parts:
            if isinstance(part, ToolCall):
                pending.append((index, part))
    return pending


def reconcile(store: Store, session_id: str) -> Reconciliation | None:
    """Make one session's saved context provider-valid, durably and once.

    It never calls a provider or runs a tool. It refuses (instead of guessing) a
    dangling call that later user or assistant messages already follow, because
    closing that call at the end would not be the order the model saw.
    """
    messages = store.messages(session_id)
    missing = unresolved_calls(messages)
    if not missing:
        return None
    for index, call in missing:
        if any(later.message.role != "tool" for later in messages[index + 1:]):
            raise ContextInvalid(
                f"session {session_id} has unresolved tool call {call.id} followed by later conversation; "
                "continuation cannot repair that without rewriting history. Inspect the event log or start "
                "a new session."
            )

    calls = [call for _, call in missing]
    assistant = messages[missing[0][0]]
    evidence = store.latest_tool_rows(session_id, [call.id for call in calls], message_id=assistant.id)
    lifecycle = store.wait_lifecycle(session_id, since=assistant.created)
    questions = open_questions(lifecycle)
    approvals = open_approvals(lifecycle)
    answers = question_answers(lifecycle)
    turns = store.turns_for_session(session_id)
    legacy = not turns
    turn_id = next((evidence[call.id]["turn_id"] for call in calls if call.id in evidence),
                   None)

    results: list[ToolResult] = []
    summary: list[dict[str, Any]] = []
    for call in calls:
        row = evidence.get(call.id) or {"call_id": call.id, "name": call.name, "state": "saved"}
        outcome, result = classify_call(call, row, questions, approvals, answers, legacy=call.id not in evidence)
        results.append(result)
        summary.append({"call_id": call.id, "name": call.name, "state": row["state"], "outcome": outcome})
    notice = Message("user", (Text(_notice_text(session_id, summary, legacy=legacy)),))
    tool_message, explained = store.save_tool_message_with_notice(
        session_id, Message("tool", tuple(results)), notice, turn_id=turn_id)
    return Reconciliation(summary, tool_message, explained)


def _notice_text(session_id: str, calls: list[dict[str, Any]], *, legacy: bool) -> str:
    lines = [NOTICE_HEADER]
    if legacy:
        lines.append(
            f"Session {session_id} · manual continuation found {len(calls)} saved tool call(s) without a result. "
            "This conversation predates durable execution records, so Nexus cannot tell whether they ran. They "
            "are labelled unknown, not 'did not run'. They were not re-run and no provider request was made to "
            "obtain their results.")
    else:
        lines.append(
            f"Session {session_id} · manual continuation found {len(calls)} saved tool call(s) without a result "
            "and closed them so the stored context is valid for a provider request. No provider request was made "
            "to obtain their results and no tool was run or re-run.")
    for call in calls:
        lines.append(f"- `{call['call_id']}` `{call['name']}`: {OUTCOMES[call['outcome']]}")
    lines.append(
        "These results are ordinary tool results in this conversation. Earlier messages are unchanged. This "
        "notice is added once; later continuations send the reconciled context unchanged.")
    return clip("\n".join(lines), NOTICE_CHARS)
