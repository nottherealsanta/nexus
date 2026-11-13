"""Adapter from CLI-owned loops to Nexus's existing durable loop-event surface."""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..llmhub.types import (
    Done,
    Message,
    PartDone,
    ProviderError,
    Reasoning,
    ReasoningDelta,
    Request,
    Text,
    TextDelta,
    ToolArgsDelta,
    ToolCall,
    ToolCallStarted,
    ToolResult,
    Usage,
    WireEvent,
)
from ..loop import MessageAdded, NoUserMessage, StepStart, ToolEnd, ToolStart, TurnEnd
from ..paths import home
from . import records
from .antigravity import AntigravityWorker
from .base import WorkerError, WorkerPolicy, WorkerResult, protected_cleanup
from .claude_code import ClaudeCodeWorker, ClaudePolicy
from .registry import DISCLOSURE, ExternalProvider, cli_path, facts, require


@dataclass(frozen=True)
class WorkerState:
    worker: dict[str, Any]


def usage_event(value: dict | None) -> Usage | None:
    if not value:
        return None
    def count(*names):
        return next((value[n] for n in names if type(value.get(n)) is int and value[n] >= 0), 0)
    return Usage(count("input_tokens", "input", "prompt_tokens"), count("cache_read_input_tokens", "cached_input_tokens"),
                 count("output_tokens", "output", "completion_tokens"), count("reasoning_tokens"))


class ExternalAgent:
    def __init__(self, *, ref: str, workspace: Path, messages: list[Message], instructions: str,
                 permission: str, effort: str | None, worker: dict | None = None):
        self.pid, self.model = ref.split("/", 1)
        self.model_ref, self.workspace, self.messages = ref, workspace, messages
        self.instructions, self.reasoning_effort, self.permission = instructions, effort, permission
        self.provider = ExternalProvider(self.pid)
        self.worker = {**facts(self.pid), **(worker or {})}
        self.turn_id = ""
        self.render = None

    def _record(self, data: dict) -> None:
        """Durable pid + start time + CLI session for recovery and orphan offers; a failure never fails the turn."""
        try:
            records.write(self.turn_id, {"pid": data["pid"], "started": records.process_start(data["pid"]),
                                         "provider": self.pid, "directory": str(self.workspace),
                                         "cli_session": data.get("session_id") or self.worker.get("session_id"),
                                         "transcript_path": data.get("transcript_path")})
        except (OSError, WorkerError):
            self.worker.setdefault("notes", []).append("worker record could not be written; orphan cleanup cannot be offered")

    def request(self) -> Request:
        return Request(self.model, self.instructions, tuple(self.messages), reasoning_effort=self.reasoning_effort)

    async def turn(self, user: str | Message | NoUserMessage, *,
                   notices: tuple[dict[str, Any], ...] = ()) -> AsyncIterator:
        require(self.pid)
        if isinstance(user, NoUserMessage):
            raise ProviderError("ZMX cannot continue without a user message; send a follow-up instead")
        message = Message("user", (Text(user),)) if isinstance(user, str) else user
        if any(not isinstance(p, Text) for p in message.parts):
            raise ValueError("ZMX currently accepts text-only assignments; images are unavailable")
        prompt = "\n".join(p.text for p in message.parts)
        executable = cli_path(self.pid)
        if not executable:
            raise ValueError(f"{self.worker['label']} CLI is not installed on the Core machine")
        self.messages.append(message)
        yield MessageAdded(message, notices=notices)
        self.worker.update(status="running", error=None, model=self.model_ref, directory=str(self.workspace),
                           permission_mode=("dontAsk" if self.permission == "ask" else "auto") if self.pid == "claude-code"
                           else "accept-edits" if self.permission == "ask" else "auto", effort=self.reasoning_effort)
        yield WorkerState(dict(self.worker))
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        resume = self.worker.get("session_id")

        async def emit(event):
            await queue.put(event)

        async def run():
            try:
                if self.pid == "claude-code":
                    worker = ClaudeCodeWorker(executable, state_dir=home() / "workers")
                    result = await worker.run(prompt=prompt, model=self.model, workspace=self.workspace, emit=emit,
                                              policy=ClaudePolicy(self.worker["permission_mode"], self.reasoning_effort),
                                              instructions=self.instructions if not resume else "", resume=resume,
                                              previous_fingerprint=self.worker.get("policy_fingerprint"),
                                              previous_transcript=self.worker.get("transcript_path"))
                else:
                    worker = AntigravityWorker(executable)
                    assignment = prompt if resume or not self.instructions else "[Nexus assignment instructions]\n" + self.instructions + "\n\n[Assignment]\n" + prompt
                    result = await worker.run(prompt=assignment, model=self.model, workspace=self.workspace,
                                              policy=WorkerPolicy(self.worker["permission_mode"]), emit=emit,
                                              conversation=resume,
                                              effort=None if re.search(r"-(low|medium|high)$", self.model) else self.reasoning_effort)
                await queue.put(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(exc)
                raise

        task = asyncio.create_task(run())
        step, active, response = 0, False, None
        parts: list = []
        streamed = ""
        last_text = ""
        reported_usage = None
        calls: dict[str, ToolCall] = {}
        results: dict[str, ToolResult] = {}
        response_calls: list[str] = []
        seen_parts: set[str] = set()
        sidechains: dict[str, dict] = {}

        async def begin():
            nonlocal step, active
            if not active:
                step += 1
                active = True
                yield StepStart(step)

        async def save(msg):
            self.messages.append(msg)
            yield MessageAdded(msg)

        async def flush():
            nonlocal active, parts, streamed, last_text, reported_usage, response_calls, seen_parts
            if not active:
                return
            if streamed:
                part = Text(streamed, origin=self.pid)
                parts.append(part)
                last_text = streamed
                yield PartDone(part)
            if usage := usage_event(reported_usage):
                yield usage
            response_calls = [p.id for p in parts if isinstance(p, ToolCall)]
            yield Done("tool_calls" if response_calls else "end_turn")
            if parts:
                async for event in save(Message("assistant", tuple(parts))):
                    yield event
            for part in parts:
                if isinstance(part, ToolCall):
                    calls[part.id] = part
                    yield ToolStart(part, json.loads(part.arguments))
            parts, streamed, reported_usage, seen_parts = [], "", None, set()
            active = False

        async def finish_result(cid, output, is_error):
            if cid not in calls or cid in results:
                return
            side = sidechains.get(cid)
            result = ToolResult(cid, output, is_error=is_error, raw={"sidechain": side} if side else None)
            results[cid] = result
            yield ToolEnd(calls[cid], result)
            if response_calls and all(c in results for c in response_calls):
                async for event in save(Message("tool", tuple(results[c] for c in response_calls))):
                    yield event

        try:
            while True:
                event = await queue.get()
                if isinstance(event, Exception):
                    event = WorkerResult("failed", "", DISCLOSURE, error=str(event))
                if isinstance(event, WorkerResult):
                    async for item in flush():
                        yield item
                    for cid in response_calls:
                        if cid not in results:
                            async for item in finish_result(cid, "CLI ended without a tool result; its outcome is unknown", True):
                                yield item
                    if event.text and event.text != last_text:
                        async for item in begin():
                            yield item
                        streamed = event.text
                        reported_usage = event.usage if self.pid == "agy" else None
                        async for item in flush():
                            yield item
                    records.clear(self.turn_id)
                    self.worker.update(status=event.status, session_id=event.session_id or resume,
                                       transcript_path=event.transcript_path, policy_fingerprint=event.policy_fingerprint,
                                       usage=event.usage, error=event.error, diagnostics=event.diagnostics,
                                       notes=list(dict.fromkeys([*self.worker.get("notes", []), *event.notes])),
                                       denied_actions=list(event.denied_actions))
                    yield WorkerState(dict(self.worker))
                    if event.status != "completed":
                        raise ProviderError(event.error or "ZMX worker failed")
                    yield TurnEnd("end_turn", step)
                    return
                data = event.data
                if event.kind == "wire":
                    raw = event.raw.decode("utf-8", "replace") if event.raw else data
                    yield WireEvent(self.pid, {"raw": raw, "context_complete": False})
                elif event.kind == "init" and data.get("conversation_id"):
                    # An interrupted turn returns no result, so the CLI session id is stored as soon as it is known.
                    self.worker.update(session_id=data["conversation_id"])
                    yield WorkerState(dict(self.worker))
                elif event.kind in ("launch", "process"):
                    self.worker.update(data)
                    if isinstance(data.get("pid"), int):
                        self._record(data)
                    yield WorkerState(dict(self.worker))
                elif event.kind == "notice":
                    self.worker.setdefault("notes", []).append(data["text"])
                elif event.kind == "subagent":
                    sidechains[str(data["tool_use_id"])] = {k: v for k, v in data.items() if k != "tool_use_id"}
                elif event.kind == "context":
                    async for item in flush():
                        yield item
                    async for item in save(Message("user", (Text(str(data["text"]), origin=self.pid, raw=data.get("raw")),))):
                        yield item
                elif event.kind == "assistant":
                    key = data.get("message_id")
                    if active and response != key:
                        async for item in flush():
                            yield item
                    if any(c not in results for c in response_calls):
                        raise ProviderError("Claude began another response before all observed tool results; unsupported transcript ordering")
                    response = key
                    async for item in begin():
                        yield item
                    reported_usage = data.get("usage") or reported_usage
                    for block in data["parts"]:
                        if not isinstance(block, dict):
                            continue
                        dedup = json.dumps(block, sort_keys=True)
                        if dedup in seen_parts:
                            continue
                        seen_parts.add(dedup)
                        kind = block.get("type")
                        if kind == "text":
                            part = Text(str(block.get("text", "")), self.pid, data.get("raw"))
                            last_text = part.text
                            yield TextDelta(part.text)
                        elif kind == "thinking":
                            part = Reasoning(str(block.get("thinking", "")), self.pid, data.get("raw"))
                            yield ReasoningDelta(part.summary)
                        elif kind == "tool_use":
                            part = ToolCall(str(block.get("id")), str(block.get("name")), json.dumps(block.get("input", {})), self.pid,
                                            {"entry": data.get("raw"), "display": display_tool(str(block.get("name")))})
                            yield ToolCallStarted(part.id, part.name)
                            yield ToolArgsDelta(part.id, part.arguments)
                        else:
                            self.worker.setdefault("notes", []).append(f"Claude block {kind!r} is wire-only")
                            continue
                        parts.append(part)
                        yield PartDone(part)
                elif event.kind == "text":
                    if response_calls and any(c not in results for c in response_calls):
                        raise ProviderError("agy emitted another response before its tool results")
                    async for item in begin():
                        yield item
                    streamed += data["text"]
                    yield TextDelta(data["text"])
                elif event.kind == "tool.start":
                    async for item in begin():
                        yield item
                    cid = f"{self.turn_id}:agy:{data['index']}"
                    call = ToolCall(cid, data["name"], json.dumps(data.get("input", {})), self.pid,
                                    {"entry": data, "display": display_tool(data["name"])})
                    parts.append(call)
                    yield ToolCallStarted(call.id, call.name)
                    yield ToolArgsDelta(call.id, call.arguments)
                    yield PartDone(call)
                elif event.kind in ("tool.end", "tool.result"):
                    async for item in flush():
                        yield item
                    cid = f"{self.turn_id}:agy:{data['index']}" if event.kind == "tool.end" else data.get("id")
                    output = data.get("output", "")
                    if not isinstance(output, str):
                        output = json.dumps(output, ensure_ascii=True)
                    if data.get("error"):
                        output += "\n" + str(data["error"])
                    is_error = data.get("is_error", data.get("state") != "DONE")
                    async for item in finish_result(cid, output, is_error):
                        yield item
                elif event.kind == "result" and self.pid == "agy":
                    reported_usage = data.get("usage")
        finally:
            async def cleanup():
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await protected_cleanup(cleanup())
            records.clear(self.turn_id)  # the process is stopped here; only a daemon crash leaves a record behind


def display_tool(name: str) -> str:
    return {"Read": "read", "Edit": "edit", "Write": "write", "Bash": "bash", "Grep": "grep", "Glob": "glob",
            "read_file": "read", "run_command": "bash"}.get(name, "generic")
