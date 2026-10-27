"""Durable recovery for turns interrupted by a daemon crash or restart (PLAN.md Step 11).

Recovery runs once while the daemon is starting, before it accepts requests. It
never calls a provider and never runs a tool. It reconciles only what the durable
records prove:

| record | meaning |
| --- | --- |
| `turn` `running` | the prompt was accepted and the turn never recorded an end |
| `turn_request` `started` | a provider request was sent and its stream never finished |
| `turn_request` `completed` + `message_id` null | a complete response whose assistant message was not saved |
| `turn_tool` `saved` | the call is in a saved assistant message and never started |
| `turn_tool` `started` | the tool was about to run; its result is unknown unless an approval was pending |
| `turn_tool` `finished` | the result is durable but never entered conversation history |
| `turn_tool` `inserted` | the result is in a saved tool message |
| unanswered `permission.asked` / `question.asked` | a wait that the restart lost |

Each action is repeatable on its own. A message is inserted together with the
marker that says it was inserted, a tool message is inserted together with the
`inserted` states of its results, and every event is checked against the durable
log before it is published again. So a crash during recovery, followed by another
start, appends no duplicate result, notice, cancellation or completion event.

**Partial output.** Deltas and completed parts of an interrupted stream stay in
the event log: they are preserved for inspection, with their `raw` and `origin`.
They do not enter conversation history, because an interrupted response cannot
be represented as a completed message without inventing a completion, complete
arguments or reasoning. Only a response that durably reached `Done` is restored:
its completed `PartDone` items become an assistant message exactly as produced.
The recovery notice names what remained diagnostic-only.

**Runtime state.** Kernels and background bash jobs live in the daemon's memory
and do not survive it. Recovery reports their loss; it does not kill a process
using a persisted PID (the PID may have been reused). Nothing is supervised or
resumed automatically.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import questions as question_contract
from .codec import part_from_json
from .llmhub.types import Message, Text, ToolCall, ToolResult
from .observations import FileGuard
from .store import Store, StoredMessage
from .tools.base import clip

if TYPE_CHECKING:
    from .bus import Bus
    from .runtime import Runtime

NOTICE_CHARS = 16_000
RECOVERY_REASON = "the daemon restarted while the turn was running"
WAIT_REASON = "the daemon restarted before this was answered"
CRASH_POINTS = frozenset({
    "turn.closed", "messages.reconciled", "assistant.restored", "tools.closed",
    "waits.cancelled", "notice", "idle", "recovered_event",
})


class SimulatedCrash(RuntimeError):
    """A deterministic stop used by tests to interrupt recovery itself."""


class Recovery:
    """One startup reconciliation over durable turn records."""

    def __init__(self, runtime: Runtime, *, crash_after: str | None = None) -> None:
        if crash_after is not None and crash_after not in CRASH_POINTS:
            raise ValueError(f"unknown crash point {crash_after!r}")
        self.runtime = runtime
        self.store: Store = runtime.store
        self.bus: Bus = runtime.bus
        self.crash_after = crash_after

    def _checkpoint(self, name: str) -> None:
        if self.crash_after == name:
            raise SimulatedCrash(name)

    def run(self) -> list[dict[str, Any]]:
        """Reconcile every unfinished turn. Returns one summary per recovered turn."""
        self._publish_unnannounced_messages()
        self._checkpoint("messages.reconciled")
        self._prepare_legacy()
        by_session: dict[str, list[dict[str, Any]]] = {}
        for turn in self.store.unfinished_turns():
            by_session.setdefault(turn["session_id"], []).append(turn)
        summaries = []
        for session_id, turns in by_session.items():
            for turn in turns:
                summaries.append(self._recover(session_id, turn))
        return summaries

    def _prepare_legacy(self) -> None:
        """Give an older dangling session a durable turn so recovery can label it once."""
        for session_id, message in self.store.legacy_dangling_sessions():
            self.store.start_turn(
                session_id, start_seq=max(0, message.seq - 1), started=message.created,
                data={"legacy": True, "reason": "the session predates durable execution records"},
            )

    # -- one turn --------------------------------------------------------------

    def _recover(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        turn_id = turn["id"]
        if self.store.session(session_id) is None:  # deleted with its session: the tombstone covers it
            return {"turn_id": turn_id, "session_id": session_id, "skipped": "session deleted"}
        requests = self.store.requests_for_turn(turn_id)
        if turn["status"] == "completed":  # a previous run finished the turn but not its recovery bookkeeping
            return self._finish_completed(turn, requests)
        if turn["status"] == "running":
            end = self._durable_turn_end(session_id, requests)
            self.store.set_turn_recovery(turn_id, "pending")
            if end is not None:
                # The loop yielded TurnEnd before the daemon stopped: the turn finished;
                # only its closing bookkeeping was lost. Do not call it interrupted.
                self.store.finish_turn(turn_id, "completed", steps=int(end["data"].get("steps") or 0))
                turn = self.store.turn(turn_id) or turn
                return self._finish_completed(turn, requests)
            self.store.finish_turn(turn_id, "interrupted", steps=max(
                (request["step"] for request in requests), default=turn["steps"]),
                error={"kind": "interrupted", "message": RECOVERY_REASON})
        self.store.set_turn_recovery(turn_id, "pending")
        turn = self.store.turn(turn_id) or turn
        self._checkpoint("turn.closed")

        for request in requests:
            if request["status"] == "started":
                # The stream ended with the daemon, not with the provider.
                self.store.interrupt_request(request["id"], reason=RECOVERY_REASON)
                request["status"] = "interrupted"
        last_request = requests[-1] if requests else None
        assistant = self._restore_completed_response(turn, last_request)
        if assistant is not None and last_request is not None:
            last_request["message_id"] = assistant.id
        self._checkpoint("assistant.restored")

        lifecycle = self.store.wait_lifecycle(session_id)
        legacy = bool(turn["data"].get("legacy"))
        tools = self._close_tool_calls(turn, lifecycle, legacy=legacy)
        self._restore_inserted_observations(turn)
        self._checkpoint("tools.closed")

        waits = self._cancel_waits(session_id, lifecycle)
        self._checkpoint("waits.cancelled")

        losses = self._losses(session_id, turn)
        partial = None
        if last_request is not None and last_request["status"] != "completed":
            partial = {"diagnostic_only": True, **self.store.stream_summary(
                session_id, last_request["event_after"])}
        notice = self._notice(turn, last_request, assistant, tools, waits, losses, partial)
        self._checkpoint("notice")

        self._publish_idle(turn)
        self._checkpoint("idle")

        summary = {
            "turn_id": turn_id, "session_id": session_id, "outcome": "interrupted",
            "reason": turn["data"].get("reason") or RECOVERY_REASON, "legacy": legacy,
            "origin": turn["data"].get("origin"),
            "continuation": turn["data"].get("continuation"),
            "request": _request_summary(last_request),
            "assistant_message": assistant.id if assistant else None,
            "tool_message": tools["message_id"],
            "tools": tools["calls"],
            "waits": waits,
            "losses": losses,
            "partial_output": partial,
            "notice": notice.id if notice else turn["notice_id"],
        }
        self._publish_recovered(turn, summary)
        self._checkpoint("recovered_event")
        self.store.set_turn_recovery(turn_id, "done")
        self.store.resolve_mutations_for_turn(turn_id, lifetime=self.runtime.mutation_lifetime)
        return summary

    # -- messages --------------------------------------------------------------

    def _publish_message(self, session_id: str, stored: StoredMessage) -> dict[str, Any]:
        from .runtime import message_json  # local import keeps the module graph acyclic at import time

        event = self.bus.publish("message.added", message_json(stored), session_id=session_id)
        if event.get("id") is not None:
            self.store.link_message_event(stored.id, event["id"])
        return event

    def _publish_unnannounced_messages(self) -> None:
        """Announce messages saved just before a crash whose event commit was lost.

        The message row is the context; the event is only its announcement. Pre-recovery
        history is marked `event_id = -1` at migration, so only new rows are selected.
        Checking the log first makes a second startup publish nothing twice.
        """
        for stored in self.store.messages_missing_events():
            if self.store.session(stored.session_id) is None:
                continue
            existing = self.store.event_for_message(stored.session_id, stored.id)
            if existing is not None:
                self.store.link_message_event(stored.id, existing)
            else:
                self._publish_message(stored.session_id, stored)

    # -- provider output -------------------------------------------------------

    def _finish_completed(self, turn: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, Any]:
        """Close the bookkeeping of a turn whose durable `turn_end` says it finished."""
        self._restore_inserted_observations(turn)
        self._publish_idle(turn, reason="the daemon restarted after the turn completed")
        summary = {
            "turn_id": turn["id"], "session_id": turn["session_id"], "outcome": "completed",
            "reason": "the turn had durably ended before the daemon restarted", "legacy": False,
            "origin": turn["data"].get("origin"),
            "continuation": turn["data"].get("continuation"),
            "request": _request_summary(requests[-1] if requests else None),
            "assistant_message": requests[-1]["message_id"] if requests else None,
            "tool_message": None,
            "tools": [], "waits": {"questions": [], "approvals": []},
            "losses": self._losses(turn["session_id"], turn), "partial_output": None, "notice": None,
        }
        self._publish_recovered(turn, summary)
        self.store.set_turn_recovery(turn["id"], "done")
        self.store.resolve_mutations_for_turn(turn["id"], lifetime=self.runtime.mutation_lifetime)
        return summary

    def _durable_turn_end(self, session_id: str, requests: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The turn's own `turn_end` event, if the loop finished before the crash."""
        if not requests:
            return None
        rows = self.store.events(session_id, after=requests[-1]["event_after"], limit=1, types=["turn_end"])
        return rows[0] if rows else None

    def _restore_completed_response(self, turn: dict[str, Any], request: dict[str, Any] | None) -> StoredMessage | None:
        """Restore a durably completed response whose message was never saved.

        Only `PartDone` items recorded after `Done` count. An interrupted stream is
        never turned into an assistant message.
        """
        if request is None or request["status"] != "completed" or request["message_id"]:
            return None
        if not request["parts"]:
            return None
        messages = self.store.messages(turn["session_id"])
        if messages and messages[-1].message.role == "assistant":
            return None
        try:
            parts = tuple(part_from_json(item) for item in request["parts"])
        except (KeyError, TypeError):  # an unreadable record is not invented back
            return None
        stored = self.store.save_assistant_message(
            turn["session_id"], Message("assistant", parts), turn_id=turn["id"], request_id=request["id"])
        self._publish_message(turn["session_id"], stored)
        return stored

    # -- tool calls ------------------------------------------------------------

    def _close_tool_calls(self, turn: dict[str, Any], lifecycle: dict[str, Any], *,
                          legacy: bool = False) -> dict[str, Any]:
        """Close the last assistant message's calls with the best durable evidence."""
        session_id = turn["session_id"]
        messages = self.store.messages(session_id)
        if not messages or messages[-1].message.role != "assistant":
            return {"message_id": None, "calls": []}
        calls = [part for part in messages[-1].message.parts if isinstance(part, ToolCall)]
        if not calls:
            return {"message_id": None, "calls": []}
        rows = {row["call_id"]: row for row in self.store.tools_for_turn(turn["id"])}
        lifecycle = self.store.wait_lifecycle(session_id, since=messages[-1].created)
        questions = open_questions(lifecycle)
        approvals = open_approvals(lifecycle)
        answers = question_answers(lifecycle)
        if any(rows.get(call.id, {}).get("state") == "inserted" for call in calls):
            # A tool message already holds at least one result; never append a second
            # partial batch. This state is unreachable with the atomic insert above.
            return {"message_id": None, "calls": []}
        results: list[ToolResult] = []
        observations: list[dict[str, Any]] = []
        summary: list[dict[str, Any]] = []
        for call in calls:
            row = rows.get(call.id) or {"call_id": call.id, "name": call.name, "state": "saved"
                                        if not legacy else "unknown"}
            outcome, result = classify_call(call, row, questions, approvals, answers, legacy=legacy)
            results.append(result)
            summary.append({"call_id": call.id, "name": call.name, "state": row["state"],
                            "outcome": outcome})
            if row["state"] == "finished" and not row["is_error"]:
                observations.extend(row["observations"])
        stored = self.store.save_tool_message(session_id, Message("tool", tuple(results)), turn_id=turn["id"])
        self._publish_message(session_id, stored)
        if observations:
            # Exactly the reads whose saved output was just restored; never a fresh read.
            FileGuard(self.store, session_id, self.runtime.file_lock).restore(observations)
        return {"message_id": stored.id, "calls": summary}

    def _restore_inserted_observations(self, turn: dict[str, Any]) -> None:
        """Grant coverage only for reads whose result is already in a saved tool message.

        A crash can land between saving that message and committing its observations.
        The message is context, so restoring its recorded observation grants nothing
        beyond the content the model has.
        """
        observations: list[dict[str, Any]] = []
        for row in self.store.tools_for_turn(turn["id"]):
            if row["state"] == "inserted" and not row["is_error"]:
                observations.extend(row["observations"])
        if observations:
            FileGuard(self.store, turn["session_id"], self.runtime.file_lock).restore(observations)

    # -- waits -----------------------------------------------------------------

    def _cancel_waits(self, session_id: str, lifecycle: dict[str, Any]) -> dict[str, Any]:
        """Close every pending question and approval with an explicit cancellation."""
        cancelled_questions: list[str] = []
        cancelled_approvals: list[str] = []
        for request in open_questions(lifecycle).values():
            qid = request["id"]
            if not self.store.has_event("question.replied", {"id": qid}, session_id=session_id):
                self.bus.publish("question.replied", {
                    "id": qid, "session_id": session_id, "call_id": request.get("call_id"),
                    "outcome": "cancelled", "answers": [], "reason": WAIT_REASON,
                }, session_id=session_id)
            # The old id can no longer resolve work, including when a previous recovery
            # already published the cancellation.
            self.runtime.resolved_questions[qid] = "cancelled"
            from .runtime import MAX_RESOLVED_QUESTIONS

            while len(self.runtime.resolved_questions) > MAX_RESOLVED_QUESTIONS:
                self.runtime.resolved_questions.pop(next(iter(self.runtime.resolved_questions)))
            cancelled_questions.append(qid)
        for request in open_approvals(lifecycle).values():
            if request["replied"]:
                continue  # already answered; not a lost wait
            pid = request["id"]
            if not self.store.has_event("permission.replied", {"id": pid}, session_id=session_id):
                self.bus.publish("permission.replied", {
                    "id": pid, "session_id": session_id, "call_id": request.get("call_id"),
                    "allow": False, "outcome": "cancelled", "reason": WAIT_REASON,
                }, session_id=session_id)
            cancelled_approvals.append(pid)
        return {"questions": cancelled_questions, "approvals": cancelled_approvals}

    # -- runtime losses --------------------------------------------------------

    def _losses(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        """Report in-memory runtime state the restart necessarily discarded."""
        code_used = self.store.has_event_type(session_id, ["code.cell", "code.call", "kernel.deleted"])
        if not code_used:
            code_used = any(row["name"] == "code" and row["state"] != "saved"
                            for row in self.store.tools_for_turn(turn["id"]))
        return {"kernels": code_used, "bash_jobs": self.store.unfinished_bash_jobs(session_id)}

    # -- notice and completion -------------------------------------------------

    def _notice(self, turn: dict[str, Any], request: dict[str, Any] | None,
                assistant: StoredMessage | None, tools: dict[str, Any],
                waits: dict[str, Any], losses: dict[str, Any],
                partial: dict[str, Any] | None) -> StoredMessage | None:
        text = self._notice_text(turn, request, assistant, tools, waits, losses, partial)
        stored = self.store.set_recovery_notice(turn["id"], Message("user", (Text(text),)))
        if stored is not None:
            self._publish_message(turn["session_id"], stored)
            return stored
        if turn["notice_id"]:
            existing = next((m for m in self.store.messages(turn["session_id"]) if m.id == turn["notice_id"]), None)
            return existing
        return None

    def _notice_text(self, turn: dict[str, Any], request: dict[str, Any] | None,
                     assistant: StoredMessage | None, tools: dict[str, Any],
                     waits: dict[str, Any], losses: dict[str, Any],
                     partial: dict[str, Any] | None) -> str:
        session = self.store.session(turn["session_id"])
        archived = bool(session.archived) if session else False
        legacy = bool(turn["data"].get("legacy"))
        lines = [
            ("[Nexus recovery: legacy interrupted turn]" if legacy
             else "[Nexus recovery: interrupted turn]"),
            ((f"Session {turn['session_id']} · legacy turn {turn['id']} was reconstructed from context saved "
              f"before execution records existed (last message {_time(turn['started'])}). Nothing was retried or "
              "resumed automatically; recovery made no provider request and ran no tool.") if legacy else
             (f"Session {turn['session_id']} · turn {turn['id']} was interrupted by a daemon restart "
              f"(started {_time(turn['started'])}). Nothing was retried or resumed automatically; recovery made "
              "no provider request and ran no tool.")),
        ]
        if legacy:
            lines.append(
                "This session was saved before this Nexus version recorded durable turn, request and tool "
                "execution state. Its last assistant message ends in tool calls, but Nexus cannot tell which of "
                "them started or completed. Their results are therefore labelled unknown, not 'did not run'. "
                "The conversation is closed with unknown results so it stays valid for a later prompt.")
        if turn["data"].get("origin") == "continuation":
            predecessor = (turn["data"].get("continuation") or {}).get("predecessor") or {}
            if predecessor.get("turn_id"):
                attempt = f"turn {predecessor['turn_id']}"
                if predecessor.get("request_id"):
                    attempt += f" request {predecessor['request_id']}"
                if predecessor.get("step") is not None:
                    attempt += f" (step {predecessor['step']}, {predecessor.get('status') or 'unknown'})"
                lines.append(f"This turn was started by manual continuation of {attempt}.")
            else:
                lines.append(
                    "This turn was started by manual continuation, but no durable preceding turn or request record "
                    "exists to link it to; the exact earlier attempt is unknown.")

        if request is None:
            if legacy:
                lines.append("No provider request record exists for this legacy turn.")
            else:
                user_saved = any(m.message.role == "user" for m in self.store.messages_after_seq(
                    turn["session_id"], turn["start_seq"]))
                lines.append("No provider request was recorded, so no model output exists for this turn."
                             + ("" if user_saved else " The prompt itself was not saved either."))
        elif request["status"] == "completed":
            if assistant is not None:
                lines.append(
                    f"The provider response in step {request['step']} had completed; its durable parts were "
                    f"restored as assistant message {assistant.id} exactly as recorded, without a provider call.")
            elif request["message_id"]:
                lines.append(
                    f"The provider response in step {request['step']} had completed and is already in the "
                    "conversation.")
            else:
                lines.append(
                    f"The provider response in step {request['step']} completed with no output parts, so no "
                    "assistant message was recorded.")
        elif request["status"] == "failed":
            error = request.get("error") or {}
            lines.append(
                f"The provider request in step {request['step']} had failed "
                f"({error.get('message') or 'an unrecorded error'}) before the daemon stopped; the turn did not "
                "finish. Any output received before the failure remains in the event log for inspection. Saved "
                "messages and events are unchanged.")
        else:
            summary = partial or self.store.stream_summary(turn["session_id"], request["event_after"])
            counts = summary["counts"]
            text_chars = counts.get("text_delta", {}).get("chars") or 0
            reasoning_chars = counts.get("reasoning_delta", {}).get("chars") or 0
            arg_chars = counts.get("tool_args_delta", {}).get("arg_chars") or 0
            started = summary["tool_calls"]
            names = ", ".join(f"`{call['name']}`" for call in started) or "none"
            parts = ", ".join(f"{name}×{count}" for name, count in sorted(summary["parts"].items())) or "none"
            lines.append(
                f"Provider request in step {request['step']} did not finish: it was interrupted while streaming. "
                f"Received before the stop: {text_chars} text characters, {reasoning_chars} reasoning characters, "
                f"{arg_chars} tool-argument characters, started tool call(s) {names}, and completed part(s) {parts}.")
            lines.append(
                "That output remains in the session's event log for inspection, with its provider raw data. It is "
                "not added to the conversation: an interrupted response cannot be replayed safely, because restoring "
                "it would present a partial response, partial arguments or partial reasoning as complete.")
        if tools["calls"]:
            lines.append("Tool calls in the last assistant message:")
            for call in tools["calls"]:
                lines.append(f"- `{call['call_id']}` `{call['name']}`: {OUTCOMES[call['outcome']]}")
            if tools["message_id"]:
                lines.append(f"Their results are in tool message {tools['message_id']}.")
        if waits["questions"] or waits["approvals"]:
            pending = ", ".join(
                [f"question `{qid}`" for qid in waits["questions"]]
                + [f"approval `{pid}`" for pid in waits["approvals"]])
            lines.append(f"Cancelled pending waits: {pending}. Their ids cannot resume this turn.")
        if losses["kernels"]:
            lines.append(
                "Kernels do not survive a daemon restart: Monty variable and cell state is gone, and no kernel "
                "state was invented. Any earlier `code.call` and `code.cell` events remain in the event log.")
        if losses["bash_jobs"]:
            lines.append(
                "Background job(s) " + ", ".join(f"`{job}`" for job in losses["bash_jobs"])
                + " have no finished record. Their retained output is lost and the command may still be running; "
                "it was not killed by PID because a reused pid could belong to another process.")
        if archived:
            lines.append("The session remains archived and idle.")
        lines.append(
            f"Send a new prompt or use manual continuation (POST /api/session/{turn['session_id']}/continue or "
            "`/continue` in chat) to resume from this context; earlier context is unchanged.")
        return clip("\n".join(line for line in lines if line), NOTICE_CHARS)

    def _publish_idle(self, turn: dict[str, Any], *, reason: str | None = None) -> None:
        fields = {"status": "idle", "turn_id": turn["id"]}
        if not self.store.has_event("session.status", fields, session_id=turn["session_id"]):
            self.bus.publish("session.status", {
                "status": "idle", "reason": reason or turn["data"].get("reason") or RECOVERY_REASON,
                "turn_id": turn["id"],
            }, session_id=turn["session_id"])

    def _publish_recovered(self, turn: dict[str, Any], summary: dict[str, Any]) -> None:
        if not self.store.has_event("turn.recovered", {"turn_id": turn["id"]}, session_id=turn["session_id"]):
            self.bus.publish("turn.recovered", summary, session_id=turn["session_id"])


OUTCOMES = {
    "restored": "its completed result was preserved exactly and inserted as a tool result (known outcome).",
    "inserted": "its result was already inserted; nothing changed.",
    "not_run": "it did not run; there is no durable record that it started.",
    "approval_pending": "it did not run; it was waiting for approval when the daemon restarted.",
    "approval_denied": "it did not run; the approval was denied before the restart.",
    "approval_cancelled": "it did not run; its approval had been cancelled before the restart.",
    "question_cancelled": "it was waiting for an answer; the question was cancelled and no answer was invented.",
    "question_answered": "its question had been answered; the durable answer was restored as its result (known outcome).",
    "unknown": "it had started; its result is unknown and it may have changed the workspace. It was not re-run.",
    "unknown_code": "it had started inside the lost kernel; its result is unknown and it may have changed the "
                    "workspace. Nested `code.call`/`code.cell` evidence remains in the event log; no successful "
                    "result or kernel state was invented.",
    "legacy_unknown": "its outcome is unknown: no execution record is linked to this saved call, so "
                      "Nexus cannot tell whether it ran. It was not re-run.",
}


def classify_call(call: ToolCall, row: dict[str, Any], questions: dict[str, dict[str, Any]],
                  approvals: dict[str, dict[str, Any]],
                  answers: dict[str, tuple[str, str | None]], *, legacy: bool = False) -> tuple[str, ToolResult]:
    """Classify one saved call from durable evidence; shared with continuation."""
    state = row["state"]
    if legacy:
        return "legacy_unknown", ToolResult(
            call.id,
            "no execution record is linked to this saved call; its outcome is unknown and it "
            "may have changed the workspace. It was not re-run and no result was invented.",
            is_error=True,
        )
    if state == "finished":
        return "restored", ToolResult(call.id, row["output"] or "", is_error=bool(row["is_error"]))
    if state == "inserted":
        return "inserted", ToolResult(call.id, row["output"] or "", is_error=bool(row["is_error"]))
    if state == "saved":
        return "not_run", ToolResult(
            call.id, "interrupted by a daemon restart before this tool ran", is_error=True)
    approval = approvals.get(call.id)
    if approval is not None:
        if approval.get("replied") and not approval.get("allow"):
            if approval.get("outcome") == "cancelled":
                return "approval_cancelled", ToolResult(
                    call.id, "interrupted by a daemon restart after this call's approval was cancelled; it did not "
                    "run", is_error=True)
            return "approval_denied", ToolResult(
                call.id, "interrupted by a daemon restart after this call's approval was denied; it did not run",
                is_error=True)
        if not approval.get("replied"):
            return "approval_pending", ToolResult(
                call.id, "interrupted by a daemon restart while this call was waiting for approval; it did not run",
                is_error=True)
    if call.name == "ask":
        answer = answers.get(call.id)
        if answer is not None and answer[0] == "answered" and answer[1] is not None:
            # The validated answer is durable even though its tool message was lost.
            return "question_answered", ToolResult(call.id, answer[1])
        if any(item.get("call_id") == call.id for item in questions.values()) or (
                answer is not None and answer[0] == "cancelled"):
            return "question_cancelled", ToolResult(
                call.id,
                "interrupted by a daemon restart while waiting for the user's answer; the question was cancelled "
                "and the answer is unknown",
                is_error=True,
            )
    if call.name == "code":
        return "unknown_code", ToolResult(
            call.id,
            "interrupted by a daemon restart while this code cell was running; its result is unknown and it may "
            "have changed the workspace. Nested code.call/code.cell events were preserved in the log, but kernel "
            "state did not survive the restart and was not invented.",
            is_error=True,
        )
    return "unknown", ToolResult(
        call.id,
        "interrupted by a daemon restart while this tool was running; its result is unknown and it may have "
        "changed the workspace",
        is_error=True,
    )


def question_answers(lifecycle: dict[str, Any]) -> dict[str, tuple[str, str | None]]:
    """Per `ask` call: the durable answer text, or its cancellation outcome."""
    asked = {item.get("id"): item for item in lifecycle["questions_asked"]}
    out: dict[str, tuple[str, str | None]] = {}
    for reply in lifecycle["questions_replied"]:
        question = asked.get(reply.get("id"))
        call_id = question.get("call_id") if question else reply.get("call_id")
        if not call_id:
            continue
        if reply.get("outcome") == "answered" and question is not None:
            try:
                text = question_contract.result_text(question["questions"], reply.get("answers") or [])
            except (KeyError, TypeError, ValueError):
                continue  # malformed old record: report uncertainty instead of inventing an answer
            out[call_id] = ("answered", text)
        else:
            out[call_id] = ("cancelled", None)
    return out


def open_questions(lifecycle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    replied = {item.get("id") for item in lifecycle["questions_replied"]}
    return {item["id"]: item for item in lifecycle["questions_asked"] if item.get("id") not in replied}


def open_approvals(lifecycle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Approvals by call id: unanswered ones and replied ones, so a denial is known."""
    replies = {item.get("id"): item for item in lifecycle["approvals_replied"]}
    out: dict[str, dict[str, Any]] = {}
    for asked in lifecycle["approvals_asked"]:
        reply = replies.get(asked.get("id"))
        entry = dict(asked)
        entry["replied"] = reply is not None
        entry["allow"] = bool(reply.get("allow")) if reply else None
        entry["outcome"] = reply.get("outcome") if reply else None
        call_id = asked.get("call_id")
        if call_id and (call_id not in out or not entry["replied"]):
            out[call_id] = entry
    return out


def _request_summary(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if request is None:
        return None
    return {"id": request["id"], "step": request["step"], "status": request["status"],
            "stop": request["stop"], "response_id": request["response_id"],
            "message_id": request["message_id"]}


def _time(value: float | None) -> str:
    import datetime

    if value is None:
        return "unknown"
    return datetime.datetime.fromtimestamp(value, datetime.UTC).isoformat(timespec="seconds")
