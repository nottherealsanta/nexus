"""Seed one long session for the TUI load benchmark (tools/bench.sh).

Writes straight to the store of an isolated Nexus home, as Core would while
running the turns, so it must run with that home's daemon stopped:

  NEXUS10_HOME=<scratch home> uv run --project ../../nexus-core python bench_seed.py [turns]

Each turn is a user prompt, a first step that reasons, writes text and calls
one tool, the tool's result, and a second step that answers. Every streamed
chunk is stored as a delta event plus its `wire_event`, with `wire_request`
bodies that carry the conversation so far, `usage`, `tool_start`/`tool_end`,
`turn_end` and the `message.added` outbox. The state tables get the same turns
(`turn`, `turn_request` with model and usage, `turn_attempt`, `turn_tool`),
so the session reads the same through the event log and through
`GET /api/session/{id}/turn`. Every tenth turn's first request is retried once
and every seventh turn's tool fails. The default 40 turns give about 30 000
events. Deterministic apart from ids: a fake clock starts two hours ago.
"""
import json
import os
import sys
import time
from pathlib import Path

from nexus.codec import message_to_json
from nexus.llmhub.types import Message, Reasoning, Text, ToolCall, ToolResult, Usage
from nexus.store import Store

TURNS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
MODEL = "codex/gpt-6.1-terra"
WORDS = ("the loader caches each manifest on its mtime so edits still show up and a test counts"
         " parses while the reader keeps one handle per file and the writer flushes on close").split()

clock = [time.time() - 2 * 3600]
real_time = time.time


def tick(seconds: float = 0.02) -> float:
    clock[0] += seconds
    return clock[0]


time.time = lambda: clock[0]  # the store stamps rows with time.time()

st = Store()
workspace = Path(os.environ["NEXUS10_HOME"]).parent / "ws"  # a directory beside the home
workspace.mkdir(exist_ok=True)
project, _ = st.project_for_path(str(workspace), "bench")
session = st.create_session(project, title="Bench: long session", agent="build", model=MODEL)
sid = session.id
seq = [0]


def event(kind: str, data: dict) -> int:
    seq[0] += 1
    return st.append_event({"session_id": sid, "seq": seq[0], "time": tick(), "type": kind, "data": data})


def chunks(n: int, salt: int) -> list[str]:
    return [WORDS[(i * 7 + salt) % len(WORDS)] + " " for i in range(n)]


def add_message(stored) -> None:
    eid = event("message.added", {"id": stored.id, "session_id": sid, "seq": stored.seq, "created": stored.created,
                                  **message_to_json(stored.message)})
    st.link_message_event(stored.id, eid)


history: list[dict] = []


def stream(turn_id: str, step: int, *, reasoning: int, text: int, call: ToolCall | None, salt: int,
           retry: bool) -> tuple[str, list]:
    """One provider request: its events, its request row and its parts."""
    event("step_start", {"type": "step_start", "step": step})
    request = st.start_request(turn_id, sid, step, model=MODEL, effort="medium")
    attempt = f"{request['id']}:1"
    st.start_attempt(turn_id, sid, request["id"], attempt, 1)
    st.mark_attempt_started(attempt)
    body = {"model": MODEL.split("/")[1], "instructions": "You are a careful coding agent.",
            "input": history[-40:], "reasoning": {"effort": "medium"}, "stream": True}
    if retry:
        event("wire_request", {"type": "wire_request", "method": "POST", "url": "https://example.invalid/v1/responses",
                               "headers": {}, "body": body})
        event("wire_response", {"type": "wire_response", "status": 503, "headers": {}})
        event("retry_scheduled", {"type": "retry_scheduled", "attempt": 1, "next_attempt": 2, "attempt_id": f"{request['id']}:2",
                                  "delay": 1.5, "reason": "HTTP 503: upstream overloaded", "status": 503,
                                  "retry_after": None, "max_attempts": 4})
        st.finish_attempt(attempt, "failed", error="HTTP 503: upstream overloaded")
        attempt = f"{request['id']}:2"
        st.start_attempt(turn_id, sid, request["id"], attempt, 2, delay=1.5, error="HTTP 503: upstream overloaded")
        tick(1.5)
        event("retry_started", {"type": "retry_started", "attempt": 2, "attempt_id": attempt})
        st.mark_attempt_started(attempt)
    event("wire_request", {"type": "wire_request", "method": "POST", "url": "https://example.invalid/v1/responses",
                           "headers": {}, "body": body})
    event("wire_response", {"type": "wire_response", "status": 200, "headers": {}})
    parts: list = []
    started = clock[0]
    if reasoning:
        summary = "".join(chunks(reasoning, salt))
        for c in chunks(reasoning, salt):
            event("wire_event", {"type": "wire_event", "name": "response.reasoning_summary_text.delta", "data": {"delta": c}})
            event("reasoning_delta", {"type": "reasoning_delta", "text": c})
        parts.append(Reasoning(summary.strip()))
        event("part_done", {"type": "part_done", "part": {"type": "reasoning", "summary": summary.strip()}})
    thought = clock[0] - started if reasoning else None
    words = chunks(text, salt + 3)
    for c in words:
        event("wire_event", {"type": "wire_event", "name": "response.output_text.delta", "data": {"delta": c}})
        event("text_delta", {"type": "text_delta", "text": c})
    parts.append(Text("".join(words).strip()))
    event("part_done", {"type": "part_done", "part": {"type": "text", "text": parts[-1].text}})
    if call is not None:
        event("tool_call_started", {"type": "tool_call_started", "id": call.id, "name": call.name})
        event("tool_args_delta", {"type": "tool_args_delta", "id": call.id, "delta": call.arguments})
        parts.append(call)
        event("part_done", {"type": "part_done", "part": {"type": "tool_call", "id": call.id, "name": call.name,
                                                          "arguments": call.arguments}})
    usage = Usage(input_tokens=2000 + 900 * len(history), cached_input_tokens=1500 + 850 * len(history),
                  output_tokens=text * 2 + reasoning * 2, reasoning_tokens=reasoning * 2)
    event("usage", {"type": "usage", "input_tokens": usage.input_tokens, "cached_input_tokens": usage.cached_input_tokens,
                    "output_tokens": usage.output_tokens, "reasoning_tokens": usage.reasoning_tokens})
    st.record_usage(request["id"], attempt, usage)
    stop = "tool_calls" if call is not None else "end_turn"
    event("done", {"type": "done", "stop": stop, "response_id": f"resp_{turn_id}_{step}", "detail": None})
    st.finish_attempt(attempt, "completed")
    st.finish_request(request["id"], stop=stop, response_id=f"resp_{turn_id}_{step}", parts=[],
                      reasoning_seconds=thought)
    message = Message("assistant", tuple(parts))
    add_message(st.save_assistant_message(sid, message, turn_id=turn_id, request_id=request["id"]))
    history.append(message_to_json(message))
    return request["id"], parts


for n in range(TURNS):
    prompt = Message("user", (Text(f"Turn {n + 1}: " + "".join(chunks(30, n)).strip()),))
    turn = st.start_turn(sid, started=tick())
    add_message(st.append_message(sid, prompt))
    history.append(message_to_json(prompt))
    event("session.status", {"status": "busy", "turn_id": turn["id"], "origin": "prompt"})
    call = ToolCall(f"call_{n}", "bash", json.dumps({"command": f"rg -n parse_manifest src/{n}"}))
    stream(turn["id"], 1, reasoning=80, text=110, call=call, salt=n, retry=n % 10 == 9)
    event("tool_start", {"type": "tool_start", "call": {"type": "tool_call", "id": call.id, "name": call.name,
                                                        "arguments": call.arguments}, "args": json.loads(call.arguments)})
    st.mark_tool_started(turn["id"], sid, call)
    failed = n % 7 == 6
    output = "exit code 2\nrg: src: No such file or directory" if failed else "\n".join(
        f"src/{n}/loader.py:{i}: parse_manifest(path)" for i in range(1, 12))
    result = ToolResult(call.id, output, failed)
    tick(1.2)
    event("tool_end", {"type": "tool_end", "call": {"type": "tool_call", "id": call.id, "name": call.name,
                                                    "arguments": call.arguments},
                       "result": {"type": "tool_result", "call_id": call.id, "output": output, "is_error": failed}})
    st.mark_tool_finished(turn["id"], sid, call, result)
    tool = Message("tool", (result,))
    add_message(st.save_tool_message(sid, tool, turn_id=turn["id"]))
    history.append(message_to_json(tool))
    stream(turn["id"], 2, reasoning=0, text=180, call=None, salt=n + 1, retry=False)
    event("turn_end", {"type": "turn_end", "stop": "end_turn", "steps": 2})
    st.mark_turn_ended(turn["id"], "end_turn", 2)
    st.finish_turn(turn["id"], "completed", steps=2)
    st.set_turn_recovery(turn["id"], "none")
    event("session.status", {"status": "idle", "turn_id": turn["id"], "origin": "prompt"})

st.db.execute("update session set updated = ? where id = ?", (clock[0], sid))
st.db.commit()
time.time = real_time
counts = st.event_counts(sid)
size = st.db.execute("select coalesce(sum(length(data)), 0) from event where session_id = ?", (sid,)).fetchone()[0]
print(json.dumps({"session": sid, "events": sum(counts.values()), "event_data_bytes": size,
                  "messages": len(st.messages(sid)), "by_type": counts}))
