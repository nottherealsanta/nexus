"""Isolated agy stream-json transport, based on nexus01's antigravity_worker.

No SDK, token access, provider registration, Nexus tools, deadline or approval
keystrokes. The stream schema is provisional until approved live verification.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .base import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_CHARS,
    MAX_PROMPT_BYTES,
    MAX_RESULT_CHARS,
    WorkerError,
    WorkerEvent,
    WorkerPolicy,
    WorkerResult,
    bounded_lines,
    child_env,
    clip,
    diagnostic_text,
    probe,
    protected_cleanup,
    stop_process,
    wait_for_exit,
)

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_STDOUT_BYTES = 32 * 1024 * 1024
MAX_STEPS = 1000
EXIT_GRACE_SECONDS = 10.0  # live 11-12: agy 1.5.6 took 0.6-2.2 s to exit after stdin closed; 3 s failed once
MIN_VERSION = (1, 4, 12)
Emit = Callable[[WorkerEvent], Awaitable[None]]
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_ENDS = {"DONE", "ERROR", "CANCELED", "CANCELLED"}
_ASK = re.compile(r"permission|approv|confirm|consent|login|auth|trust|prompt|question", re.IGNORECASE)
_ATTENTION = re.compile(
    r"not (?:logged|signed) in|(?:log|sign) ?in (?:is )?required|please (?:log|sign) ?in|"
    r"authentication (?:required|failed)|unauthenticated|credentials? (?:expired|missing|invalid)|"
    r"trust (?:this|the) (?:folder|workspace|directory)|untrusted (?:folder|workspace|directory)|"
    r"permission (?:required|prompt|request)|requires? (?:your )?(?:approval|permission)", re.IGNORECASE)


@dataclass
class StreamParser:
    model: str
    workspace: Path
    policy: WorkerPolicy
    initialized: bool = False
    finished: bool = False
    session_id: str | None = None
    text: str = ""
    error: str | None = None
    attention: str | None = None
    usage: dict[str, int] | None = None
    notes: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    tools: dict[int, dict] = field(default_factory=dict)
    closed_tools: set[int] = field(default_factory=set)

    def feed(self, raw: bytes) -> list[WorkerEvent]:
        events = [WorkerEvent("wire", raw=raw)]
        if self.finished or self.error or self.attention:
            return events
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            return events + [self._notice("malformed stream line was not interpreted")]
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            return events + [self._notice("unknown stream shape was not interpreted")]
        kind = event["event"]
        data = event.get(kind)
        if kind in ("init", "step_update", "result") and not isinstance(data, dict):
            self.error = f"malformed {kind} payload"
        elif kind == "init":
            if self.initialized:
                self.error = "duplicate init event"
            elif data.get("permission_mode") == "always-proceed":
                self.error = "agy inherited always-proceed; bypass is not supported by this transport"
            elif data.get("cwd") is not None and data["cwd"] != str(self.workspace):
                self.error = "agy reported a different working directory"
            elif data.get("model") is not None and data["model"] != self.model:
                self.error = "agy reported a different model"
            else:
                self.initialized = True
                session = event.get("conversation_id")
                self.session_id = session[:200] if isinstance(session, str) else None
                events.append(WorkerEvent("init", {**data, "conversation_id": self.session_id}))
        elif kind in ("step_update", "result") and not self.initialized:
            self.error = "agy emitted work or success before init"
        elif kind == "step_update":
            events.extend(self._step(data))
        elif kind == "result":
            self.finished = True
            self._result(data)
            events.append(WorkerEvent("result", data))
        elif _ASK.search(kind):
            self.attention = f"agy requested {kind!r}. Run `agy` interactively in the workspace; Nexus approved nothing."
        else:
            events.append(self._notice(f"unknown agy event {kind[:100]!r} was not interpreted"))
        return events

    def _notice(self, note: str) -> WorkerEvent:
        if note not in self.notes:
            self.notes.append(note)
        return WorkerEvent("notice", {"text": note})

    def _step(self, data: dict) -> list[WorkerEvent]:
        index, kind, state = data.get("step_index"), data.get("step_type"), data.get("state")
        if type(index) is not int or not 0 <= index < MAX_STEPS:
            self.error = "invalid step index or transport step-record limit reached (not an agent step limit)"
            return []
        if kind == "agent_response":
            delta = data.get("text_delta")
            if isinstance(delta, str):
                self.text, note = clip(self.text + delta, MAX_RESULT_CHARS)
                events = [WorkerEvent("text", {"index": index, "text": delta})]
                if note:
                    events.append(self._notice(f"partial answer {note}"))
                return events
            return []  # a closing agent_response step without text (agy 1.5.0 sends usage here)
        elif kind in ("user_input", "system_message"):
            return []  # recorded in the wire event; nothing to interpret (seen live on agy 1.5.0)
        elif kind == "tool":
            if index in self.closed_tools:
                return []
            info = data.get("tool_info")
            info = info if isinstance(info, dict) else {}
            events = []
            if index not in self.tools:
                name = data.get("tool_name") or info.get("name")
                self.tools[index] = {"index": index, "name": name if isinstance(name, str) else "unknown",
                                     "input": info.get("parameters", {})}
                events.append(WorkerEvent("tool.start", self.tools[index]))
            if not isinstance(state, str):
                self.error = "malformed tool step state"
                return events
            if state in _ENDS:
                events.append(WorkerEvent("tool.end", {**self.tools.pop(index), "state": state,
                                                       "output": info.get("output"), "error": info.get("error")}))
                self.closed_tools.add(index)
            return events
        return [self._notice(f"unknown step type {str(kind)[:100]!r} was not interpreted")]

    def _result(self, data: dict) -> None:
        session = data.get("conversation_id")
        if isinstance(session, str):
            self.session_id = session[:200]
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage = {k[:80]: v for k, v in list(usage.items())[:32] if type(v) is int and v >= 0} or None
        denied = data.get("denied_actions")
        if isinstance(denied, list):
            self.denied = [diagnostic_text(json.dumps(item, ensure_ascii=False).encode("utf-8", "replace"))[:500]
                           for item in denied[:20]]
            if len(denied) > 20:
                self._notice("denied actions clipped to 20 entries; full result is in the wire event")
        answer = data.get("response")
        if isinstance(answer, str) and answer.strip():
            self.text, note = clip(answer, MAX_RESULT_CHARS)
            if note:
                self._notice(f"answer {note}")
        if data.get("status") != "SUCCESS":
            self.error = "agy terminal result did not report SUCCESS"
            if _ATTENTION.search(str(data.get("error", ""))):
                self.attention = "Run `agy` interactively in the workspace to resolve sign-in, trust or permissions."
        elif not isinstance(answer, str):
            self.error = "agy success did not contain a response string"
        elif self.denied or not answer.strip():
            self.attention = ("agy denied actions or returned an empty answer; headless mode cannot ask for approval. "
                              "Run `agy` interactively in the workspace and review its own permission settings. "
                              "Nexus approved nothing.")
        elif self.tools:
            self.error = "agy reported success with unfinished tool steps"


class AntigravityWorker:
    def __init__(self, executable: str, *, environ: Mapping[str, str] | None = None) -> None:
        if not executable or "\x00" in executable:
            raise ValueError("agy executable must be a non-empty path or name")
        self.executable = executable
        self.env = child_env(environ)

    async def preflight(self, policy: WorkerPolicy, *, conversation: bool = False, effort: bool = False) -> str:
        if os.name != "posix":
            raise WorkerError("ZMX transports currently require POSIX process groups")
        cwd = Path(tempfile.gettempdir())
        version_text = await probe(self.executable, "--version", env=self.env, cwd=cwd)
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_text)
        if not match or tuple(int(v) for v in match.groups()) < MIN_VERSION:
            raise WorkerError("agy 1.4.12 or newer is required; CLI version could not be verified")
        help_text = await probe(self.executable, "--help", env=self.env, cwd=cwd)
        required = ["--input-format", "--output-format", "stream-json", "--model", "--mode", "--print-timeout", policy.permission_mode]
        if conversation:
            required.append("--conversation")
        if effort:
            required.append("--effort")
        missing = [flag for flag in required if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text)]
        if missing:
            raise WorkerError("agy lacks required headless flags/values: " + ", ".join(missing))
        return match.group(0)

    def build_argv(self, model: str, policy: WorkerPolicy, *, conversation: str | None = None, effort: str | None = None) -> list[str]:
        argv = [self.executable, "--input-format", "stream-json", "--output-format", "stream-json",
                "--model", model, "--mode", policy.permission_mode, "--print-timeout", "0s"]
        if conversation:
            argv += ["--conversation", conversation]
        if effort:
            argv += ["--effort", effort]
        return argv

    async def run(self, *, prompt: str, model: str, workspace: Path, policy: WorkerPolicy | None = None,
                  emit: Emit | None = None, conversation: str | None = None, effort: str | None = None) -> WorkerResult:
        policy = policy or WorkerPolicy()
        if conversation and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", conversation):
            raise ValueError("invalid agy conversation id")
        if effort and not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", effort):
            raise ValueError("invalid agy effort")
        if not _MODEL.fullmatch(model):
            raise ValueError("agy requires an exact model ID, not an option or an alias")
        if "\x00" in prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("agy prompt contains NUL or exceeds 64 KiB; refusing to truncate")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("agy workspace must be a directory")
        version = await self.preflight(policy, conversation=bool(conversation), effort=bool(effort))
        argv = self.build_argv(model, policy, conversation=conversation, effort=effort)
        parser = StreamParser(model, workspace, policy)
        diagnostics = bytearray()
        stop_requested = asyncio.Event()
        emitted_notes: set[str] = set()
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=workspace, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)

        async def stderr() -> None:
            while chunk := await process.stderr.read(4096):
                diagnostics.extend(chunk)
                if len(diagnostics) > MAX_DIAGNOSTIC_BYTES:
                    del diagnostics[:-MAX_DIAGNOSTIC_BYTES]
                    parser._notice("stderr clipped to its last 16 KiB")
                if _ATTENTION.search(diagnostic_text(bytes(diagnostics))):
                    parser.attention = "Run `agy` interactively in the workspace to resolve sign-in, trust or permissions."
                    stop_requested.set()

        async def stdout() -> None:
            async for raw in bounded_lines(process.stdout, line_limit=MAX_LINE_BYTES, total_limit=MAX_STDOUT_BYTES):
                if emit is not None:
                    await emit(WorkerEvent("wire", raw=raw))
                for event in parser.feed(raw)[1:]:
                    if emit is not None:
                        await emit(event)
                        if event.kind == "notice":
                            emitted_notes.add(event.data["text"])
                if parser.finished or parser.attention or parser.error:
                    stop_requested.set()

        async def send() -> None:
            message = {"event": "user", "message": {"content": prompt}}
            try:
                process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise WorkerError("agy closed stdin before prompt delivery completed") from None

        err = asyncio.create_task(stderr())
        out = asyncio.create_task(stdout())
        writer = asyncio.create_task(send())
        stop_wait = asyncio.create_task(stop_requested.wait())
        leader = asyncio.create_task(wait_for_exit(process))

        async def cleanup() -> None:
            process.stdin.close()
            try:
                await stop_process(process)
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(err), .2)
            finally:
                for task in (out, err, writer, stop_wait, leader):
                    task.cancel()
                await asyncio.gather(out, err, writer, stop_wait, leader, return_exceptions=True)

        try:
            if emit:
                await emit(WorkerEvent("launch", {"cli_version": version, "argv": argv, "directory": str(workspace), "pid": process.pid}))
            try:
                pending = {out, stop_wait, leader, writer}
                while True:
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    if writer in done:
                        await writer
                        pending.discard(writer)
                    if done - {writer}:
                        break
                if leader in done and not out.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(out), EXIT_GRACE_SECONDS)
                    except TimeoutError:
                        parser.error = "agy exited but its stdout remained open; descendants may still be running"
                if out in done:
                    await out
            except WorkerError as exc:
                parser.error = str(exc)
                parser._notice(str(exc))
            process.stdin.close()
            if parser.finished and not parser.error and not parser.attention:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(asyncio.shield(leader), asyncio.shield(out), asyncio.shield(writer)), EXIT_GRACE_SECONDS)
                except TimeoutError:
                    parser.error = "agy did not close its process/stream after the terminal result; descendants may still be running"
                except WorkerError as exc:
                    parser.error = str(exc)
                    parser._notice(str(exc))
                if process.returncode not in (0, None):
                    parser.error = f"agy exited with code {process.returncode} after its terminal result"
        finally:
            await protected_cleanup(cleanup())
        full_diag = diagnostic_text(bytes(diagnostics))
        diag, note = clip(full_diag, MAX_DIAGNOSTIC_CHARS)
        if note:
            parser._notice(f"diagnostics {note}")
        fallback = re.search(r"--mode \S+ is not available[^\n]*", full_diag, re.IGNORECASE)
        if fallback:
            parser._notice(f"agy mode fallback: {fallback.group(0)}")
        if _ATTENTION.search(diag) and (not parser.finished or parser.error):
            parser.attention = "Run `agy` interactively in the workspace to resolve sign-in, trust or permissions."
        if not parser.finished and not parser.error and not parser.attention:
            parser.error = "agy stream ended without a terminal result; completion is not known"
        status = "needs_attention" if parser.attention else "failed" if parser.error else "completed"
        if emit is not None:
            for index, tool in parser.tools.items():
                await emit(WorkerEvent("tool.end", {**tool, "index": index, "state": "CANCELED",
                                                    "error": "stream ended without a tool result"}))
            for note in parser.notes:
                if note not in emitted_notes:
                    await emit(WorkerEvent("notice", {"text": note}))
        return WorkerResult(status, parser.text, policy.authority(workspace, vendor="Antigravity"), session_id=parser.session_id,
                            usage=parser.usage, error=parser.attention or parser.error, diagnostics=diag,
                            notes=tuple(parser.notes), denied_actions=tuple(parser.denied))
