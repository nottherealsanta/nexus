"""Command line for Nexus Core.

Local commands (no daemon): `login`, `logout`, `status`, `models`, `serve`,
`daemon start|stop|status`.

Daemon clients (they start the daemon if it is not running and talk to it over
`/api/*`): `project`, `session`, `workspace`, `run`, `chat`. They render the daemon's event
stream. `session` can archive/unarchive a whole tree and filter lists by archive
state. Every tool call is shown with each argument labelled, every result with
its size, and any clipping is announced. `--wire` also prints the exact
request (credentials redacted) and every server-sent event from the provider;
`--wire-file` appends the same as JSONL. In chat, `/context` prints the request
body the next turn would send, `/model provider/model` and `/agent name` switch
the session's model and agent.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import select
import signal
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, TextIO

from . import daemon
from .client import ApiError, Client
from .llmhub import catalog
from .llmhub.auth import SIGN_INS, AuthError, CredentialError
from .llmhub.hub import connected
from .llmhub.providers import PROVIDERS

DISPLAY_RESULT_LINES = 40
WIRE_TYPES = {"wire_request", "wire_response", "wire_event"}

DIM, BOLD, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"


# -- rendering the daemon's event stream -------------------------------------


class Renderer:
    def __init__(self, *, wire: bool, wire_file: TextIO | None, out: TextIO = sys.stdout) -> None:
        self.wire = wire
        self.wire_file = wire_file
        self.out = out
        self.mode = ""  # "", "text" or "reasoning": what is streaming right now

    def _print(self, text: str = "", end: str = "\n") -> None:
        self.out.write(text + end)
        self.out.flush()

    def _break(self) -> None:
        if self.mode:
            self._print(RESET)
            self.mode = ""

    def event(self, event: dict[str, Any]) -> None:
        kind, data = event["type"], event.get("data") or {}
        if kind in WIRE_TYPES and self.wire_file is not None:
            self.wire_file.write(json.dumps(data) + "\n")
            self.wire_file.flush()
        if kind == "wire_request":
            if self.wire:
                self._break()
                self._print(f"{CYAN}→ {data['method']} {data['url']}{RESET}")
                for key, value in data["headers"].items():
                    self._print(f"{CYAN}  {key}: {value}{RESET}")
                self._print(f"{CYAN}{json.dumps(data['body'], indent=2)}{RESET}")
        elif kind == "wire_response":
            if self.wire:
                self._print(f"{CYAN}← HTTP {data['status']}{RESET}")
        elif kind == "wire_event":
            if self.wire:
                self._break()
                self._print(f"{CYAN}← {data['name']} {json.dumps(data['data'], separators=(',', ':'))}{RESET}")
        elif kind == "reasoning_delta":
            if self.wire:
                return
            if self.mode != "reasoning":
                self._break()
                self._print(f"{DIM}thinking: ", end="")
                self.mode = "reasoning"
            self._print(data["text"], end="")
        elif kind == "text_delta":
            if self.wire:
                return
            if self.mode != "text":
                self._break()
                self.mode = "text"
            self._print(data["text"], end="")
        elif kind == "usage":
            self._break()
            self._print(
                f"{DIM}tokens  in {data['input_tokens']} (cached {data['cached_input_tokens']})"
                f" · out {data['output_tokens']} (reasoning {data['reasoning_tokens']}){RESET}"
            )
        elif kind == "done":
            if data["stop"] not in ("end_turn", "tool_calls"):
                self._print(f"{YELLOW}stopped: {data['stop']} {data.get('detail') or ''}{RESET}")
        elif kind == "step_start":
            if data["step"] > 1:
                self._print(f"{DIM}— step {data['step']} —{RESET}")
        elif kind == "tool_start":
            self._break()
            call = data["call"]
            self._print(f"{BOLD}▸ {call['name']}{RESET} {DIM}{call['id']}{RESET}")
            if data["args"] is None:
                self._print(f"  {RED}invalid JSON arguments:{RESET} {call['arguments']}")
            for key, value in (data["args"] or {}).items():
                lines = (value if isinstance(value, str) else json.dumps(value)).split("\n")
                self._print(f"  {DIM}{key}:{RESET} {lines[0]}")
                for line in lines[1:]:
                    self._print(f"    {line}")
        elif kind == "tool_end":
            result = data["result"]
            lines = result["output"].split("\n")
            label = f"{RED}error{RESET}" if result["is_error"] else "result"
            self._print(f"  ↳ {label} {DIM}({len(lines)} lines, {len(result['output'])} chars){RESET}")
            for line in lines[:DISPLAY_RESULT_LINES]:
                self._print(f"    {DIM}{line}{RESET}")
            if len(lines) > DISPLAY_RESULT_LINES:
                self._print(f"    {YELLOW}[display clipped: {len(lines) - DISPLAY_RESULT_LINES} more lines sent to the model]{RESET}")
        elif kind == "turn_end":
            self._break()
            if data["stop"] == "max_steps":
                self._print(f"{YELLOW}stopped after {data['steps']} steps (limit){RESET}")
        elif kind == "session.error":
            self._break()
            self._print(f"{RED}{data['message']}{RESET}")
            if data.get("body"):
                self._print(f"{DIM}{data['body']}{RESET}")
        elif kind == "question.replied":
            self._break()
            if data.get("outcome") == "answered":
                self._print(f"{DIM}question answered{RESET}")
            else:
                self._print(f"{YELLOW}question cancelled: {data.get('reason') or 'no answer'}{RESET}")
        elif kind == "session.workspace_tools":
            self._break()
            self._print(f"{CYAN}⌂ workspace control tools activated{RESET}")
        elif kind == "session.directory_changed":
            self._break()
            branch = f" · branch {data['branch']}" if data.get("branch") else ""
            self._print(f"{CYAN}⌂ directory {data['from']} → {data['to']}{branch}{RESET}")
        elif kind in ("worktree.created", "worktree.deleted"):
            self._break()
            verb = "created" if kind == "worktree.created" else "deleted"
            branch = f" on branch {data['branch']}" if data.get("branch") else ""
            self._print(f"{CYAN}⌂ worktree {verb} {data['path']}{branch} (session directory unchanged){RESET}")
        elif kind == "bus.overflow":
            self._break()
            self._print(f"{RED}event stream overflowed after event {data['dropped_after']}; reconnect to continue{RESET}")


# -- daemon client flows -----------------------------------------------------


async def _open_session(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    if args.session:
        session = await client.call("GET", f"/api/session/{args.session}")
        if session["archived"]:
            root = session
            while root.get("parent_id"):
                root = await client.call("GET", f"/api/session/{root['parent_id']}")
            raise ValueError(
                f"session {args.session} is archived; unarchive its root tree first: "
                f"nexus session unarchive {root['id']}")
    else:
        project = await client.call("POST", "/api/project", {"path": str(Path(args.workspace).resolve())})
        existing = (await client.call("GET", "/api/session", project=project["id"], archived="exclude")
                    if getattr(args, "cont", False) else [])
        session = existing[0] if existing else await client.call(
            "POST", "/api/session", {"project": project["id"], "model": args.model, "effort": args.effort, "agent": args.agent})
    if args.agent and session["agent"] != args.agent:
        session = await client.call("POST", f"/api/session/{session['id']}/agent", {"agent": args.agent})
    if args.model and session["model"] != args.model:
        session = await client.call("POST", f"/api/session/{session['id']}/model", {"model": args.model})
    if args.effort is not None and session["effort"] != (args.effort or None):
        session = await client.call("PATCH", f"/api/session/{session['id']}", {"effort": args.effort})
    if args.yes and session["permission"] != "allow":
        session = await client.call("PATCH", f"/api/session/{session['id']}", {"permission": "allow"})
    if args.disable is not None:
        disabled = sorted({t.strip() for t in args.disable.split(",") if t.strip()})
        if disabled != session["disabled_tools"]:
            session = await client.call("PATCH", f"/api/session/{session['id']}", {"disabled_tools": disabled})
    return session


async def _send(client: Client, session_id: str, text: str, renderer: Renderer) -> None:
    """Prompt, then render the events of this session and its child sessions until
    the whole tree is idle. Ctrl-C interrupts the turn (and its children) and
    abandons a question prompt without submitting an answer."""
    loop = asyncio.get_running_loop()
    answering = threading.Event()
    input_readers: dict[str, threading.Event] = {}
    input_queue: asyncio.Queue[tuple[str, str, dict[str, Any], threading.Event]] = asyncio.Queue()

    async def answer_requests() -> None:
        # Keep consuming SSE while one stdin reader serves queued questions.
        while True:
            kind, sid, request, cancelled = await input_queue.get()
            if not cancelled.is_set():
                if kind == "question":
                    await _question(client, sid, request, renderer, cancelled)
                else:
                    await _ask(client, sid, request, renderer, cancelled)

    input_task = asyncio.create_task(answer_requests())

    def interrupt() -> None:
        answering.set()
        for cancelled in input_readers.values():
            cancelled.set()
        asyncio.ensure_future(client.call("POST", f"/api/session/{session_id}/interrupt"))

    loop.add_signal_handler(signal.SIGINT, interrupt)
    busy: set[str] = set()
    labels: dict[str, str] = {}
    current = session_id
    try:
        async with contextlib.aclosing(client.events(session_id)) as events:
            await anext(events)  # server.connected: subscribed before the turn starts
            await client.call("POST", f"/api/session/{session_id}/prompt", {"text": text})
            busy.add(session_id)
            async for event in events:
                sid = event.get("session_id") or session_id
                if event["type"] == "session.created" and event["data"]["parent_id"]:
                    labels[sid] = f"child {sid} · agent {event['data']['agent']} · {event['data']['model']}"
                if sid != current and event["type"] not in ("session.status", "session.created", "session.updated"):
                    renderer._break()
                    header = labels.get(sid, sid) if sid != session_id else "back to the parent session"
                    print(f"{CYAN}── {header} ──{RESET}")
                    current = sid
                if event["type"] in ("permission.asked", "question.asked"):
                    cancelled = threading.Event()
                    if answering.is_set():
                        cancelled.set()
                    input_readers[event["data"]["id"]] = cancelled
                    input_queue.put_nowait(("question" if event["type"] == "question.asked" else "permission",
                                            sid, event["data"], cancelled))
                    continue
                if event["type"] in ("question.replied", "permission.replied"):
                    cancelled = input_readers.pop(event["data"]["id"], None)
                    if cancelled is not None:
                        cancelled.set()
                if event["type"] == "session.notified":
                    renderer._break()
                    print(f"{CYAN}↩ child {event['data']['from']} finished; its answer goes to the parent{RESET}")
                    continue
                renderer.event(event)
                if event["type"] == "session.status":
                    (busy.add if event["data"]["status"] == "busy" else busy.discard)(sid)
                    if not busy:
                        renderer._break()
                        return
    finally:
        for cancelled in input_readers.values():
            cancelled.set()
        input_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await input_task
        loop.remove_signal_handler(signal.SIGINT)


async def _read_answer(prompt: str, cancelled: threading.Event) -> str | None:
    """Cancellable stdin read without a background thread that can steal later input."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if isinstance(sys.stdin, io.StringIO):
        line = sys.stdin.readline() if not cancelled.is_set() else ""
        return line.rstrip("\r\n") if line else None
    try:
        fd = sys.stdin.fileno()
        pending = bytearray()
        while not cancelled.is_set():
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                await asyncio.sleep(0.02)
                continue
            chunk = os.read(fd, 1)
            if not chunk or chunk == b"\n":
                if not chunk and not pending:
                    return None
                return pending.decode(sys.stdin.encoding or "utf-8", "replace").rstrip("\r")
            pending.extend(chunk)
        return None
    except (OSError, TypeError, ValueError):
        return None


def _option_tokens(line: str, question: dict[str, Any]) -> list[str] | None:
    """The option ids named by `line` (numbers or ids), or None when it is not a selection."""
    options = question["options"]
    by_number = {str(i + 1): option["id"] for i, option in enumerate(options)}
    by_id = {option["id"]: option["id"] for option in options}
    tokens = [token for token in re.split(r"[,\s]+", line) if token]
    chosen: list[str] = []
    for token in tokens:
        option_id = by_number.get(token) or by_id.get(token)
        if option_id is None:
            return None
        if option_id not in chosen:
            chosen.append(option_id)
    return chosen


async def _question(client: Client, session_id: str, request: dict[str, Any], renderer: Renderer,
                    cancelled: threading.Event) -> None:
    """Show one `question.asked` request and submit the answers through Core."""
    renderer._break()
    renderer._print(f"{BOLD}? The agent asks{RESET} {DIM}{request['id']} · call {request.get('call_id') or '-'}{RESET}")
    answers: list[dict[str, Any]] = []
    for question in request["questions"]:
        options = question["options"]
        how_many = "one or more" if question["multiple"] else "one"
        free = " or leave blank for free text" if question["allow_free_text"] else ""
        while True:
            renderer._print(f"{BOLD}  {question['question']}{RESET}")
            for i, option in enumerate(options, 1):
                renderer._print(f"    {i}. {option['label']} {DIM}(id {option['id']}){RESET}")
                if option.get("preview"):
                    for line in str(option["preview"]).split("\n"):
                        renderer._print(f"       {DIM}{line}{RESET}")
            selected: list[str] = []
            text: str | None = None
            if options:
                prompt = f"  select {how_many} (numbers or ids, comma-separated){free} > "
                line = await _read_answer(prompt, cancelled)
                if line is None:
                    if not cancelled.is_set():
                        await _abandon_question(client, session_id, renderer)
                    return
                line = line.strip()
                if line:
                    selected = _option_tokens(line, question) or []
                    if not selected:
                        if question["allow_free_text"]:
                            text = line
                        else:
                            renderer._print(f"{RED}  choose an option or its id{RESET}")
                            continue
                    elif not question["multiple"] and len(selected) > 1:
                        renderer._print(f"{RED}  this question is single-select{RESET}")
                        continue
                elif not question["allow_free_text"]:
                    renderer._print(f"{RED}  an option is required{RESET}")
                    continue
            if question["allow_free_text"]:
                note = await _read_answer("  text (optional) > " if options else "  answer > ", cancelled)
                if note is None:
                    if not cancelled.is_set():
                        await _abandon_question(client, session_id, renderer)
                    return
                if note.strip():
                    text = note.strip()
            if not selected and not text:
                renderer._print(f"{RED}  an answer is required{RESET}")
                continue
            answers.append({"question_id": question["id"], "selected": selected, "text": text})
            break
    if cancelled.is_set():
        return
    try:
        await client.call("POST", f"/api/session/{session_id}/question/{request['id']}", {"answers": answers})
    except ApiError as exc:
        renderer._print(f"{RED}answer rejected: {exc}{RESET}")
        if exc.status == 400:  # the CLI could not produce an acceptable answer; do not leave the ask pending
            await _abandon_question(client, session_id, renderer)


async def _abandon_question(client: Client, session_id: str, renderer: Renderer) -> None:
    renderer._print(f"{YELLOW}no answer submitted; interrupting the turn{RESET}")
    await client.call("POST", f"/api/session/{session_id}/interrupt")


async def _ask(client: Client, session_id: str, request: dict[str, Any], renderer: Renderer,
               cancelled: threading.Event) -> None:
    renderer._break()
    answer = await _read_answer(f"  {YELLOW}allow {request['tool']}? [y/N] {RESET}", cancelled)
    if cancelled.is_set():
        return
    if answer is None:
        await _abandon_question(client, session_id, renderer)
        return
    reply = "allow" if answer.strip().lower() in ("y", "yes") else "deny"
    try:
        await client.call("POST", f"/api/session/{session_id}/permission/{request['id']}", {"reply": reply})
    except ApiError as exc:
        renderer._print(f"{RED}approval rejected: {exc}{RESET}")


async def _run(args: argparse.Namespace, renderer: Renderer) -> None:
    client = Client.connect()
    try:
        session = await _open_session(client, args)
        await _send(client, session["id"], args.prompt, renderer)
        print(f"{DIM}session {session['id']}{RESET}")
    finally:
        await client.close()


async def _chat(args: argparse.Namespace, renderer: Renderer) -> None:
    client = Client.connect()
    try:
        session = await _open_session(client, args)
        print(f"{DIM}nexus · {_describe(session)} · {session['directory']} · session {session['id']}  (/context, /tools, /agent, /model, /exit){RESET}")
        for message in await client.call("GET", f"/api/session/{session['id']}/message"):
            text = "".join(p.get("text", "") for p in message["parts"] if p["type"] == "text")
            if text and message["role"] in ("user", "assistant"):
                print(f"{DIM}{'›' if message['role'] == 'user' else ' '} {text[:200]}{RESET}")
        while True:
            try:
                line = await _read_answer(f"{BOLD}› {RESET}", threading.Event())
                if line is None:
                    return
                text = line.strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not text:
                continue
            if text in ("/exit", "/quit"):
                return
            if text == "/context":
                print(json.dumps(await client.call("GET", f"/api/session/{session['id']}/context"), indent=2))
                continue
            if text.startswith("/model"):
                ref = text.removeprefix("/model").strip()
                if ref:
                    session = await client.call("POST", f"/api/session/{session['id']}/model", {"model": "" if ref == "default" else ref})
                print(f"{DIM}{_describe(session)}  (/model default follows the agent){RESET}")
                continue
            if text == "/tools":
                off = ", ".join(session["disabled_tools"]) or "none"
                print(f"{DIM}tools: {', '.join(session['tools']) or 'none'} · switched off: {off}{RESET}")
                continue
            if text.startswith("/agent"):
                name = text.removeprefix("/agent").strip()
                if name:
                    session = await client.call("POST", f"/api/session/{session['id']}/agent", {"agent": name})
                else:
                    await _agent_list(client, session["directory"])
                print(f"{DIM}{_describe(session)}{RESET}")
                continue
            try:
                await _send(client, session["id"], text, renderer)
            except ApiError as exc:
                print(f"{RED}{exc}{RESET}")
    finally:
        await client.close()


def _describe(session: dict[str, Any]) -> str:
    effort = f" effort {session['effort']} ({session['effort_source']})" if session["effort"] else ""
    off = f" · off: {','.join(session['disabled_tools'])}" if session["disabled_tools"] else ""
    return f"agent {session['agent']} · {session['model']} ({session['model_source']}){effort}{off}"


async def _agent_list(client: Client, directory: str) -> None:
    found = await client.call("GET", "/api/agent", directory=directory)
    for a in found["agents"]:
        prefs = " ".join(f"{k}={a[k]}" for k in ("model", "effort", "steps") if a[k])
        tools = ",".join(a["tools"]) if a["tools"] is not None else "all"
        print(f"{BOLD}{a['name']:<12}{RESET} {DIM}{a['source']:<8} tools {tools:<16} {prefs}{RESET}\n  {a['description']}")
    for problem in found["problems"]:
        print(f"{RED}{problem['path']}: {problem['error']}{RESET}")


async def _agent(args: argparse.Namespace) -> None:
    client = Client.connect()
    try:
        directory = str(Path(args.workspace).resolve())
        if args.name:
            a = await client.call("GET", f"/api/agent/{args.name}", directory=directory)
            print(f"{BOLD}{a['name']}{RESET} {DIM}{a['source']} · {a['path']}{RESET}\n{a['description']}\n")
            print(f"{DIM}model {a['model'] or '-'} · effort {a['effort'] or '-'} · steps {a['steps'] or 'default'} · "
                  f"tools {', '.join(a['tools']) if a['tools'] is not None else 'all'}{RESET}\n")
            print(a["prompt"])
        else:
            await _agent_list(client, directory)
    finally:
        await client.close()


async def _project(args: argparse.Namespace) -> None:
    client = Client.connect()
    try:
        if args.action == "open":
            project = await client.call("POST", "/api/project", {"path": str(Path(args.path or ".").resolve())})
            print(f"{project['id']}  {project['name']}  {project['path']}")
        else:
            for project in await client.call("GET", "/api/project"):
                print(f"{project['id']}  {project['name']:<24} {project['path']}")
    finally:
        await client.close()


async def _session(args: argparse.Namespace) -> None:
    client = Client.connect()
    try:
        if args.action == "list":
            project = None if args.all else (await client.call("POST", "/api/project", {"path": str(Path(args.workspace).resolve())}))["id"]
            archived = "only" if args.archived_only else ("include" if args.include_archived else "exclude")
            found = await client.call("GET", "/api/session", project=project, archived=archived)
            kids: dict[str | None, list[dict[str, Any]]] = {}
            for s in found:
                kids.setdefault(s["parent_id"], []).append(s)

            def show(s: dict[str, Any], depth: int) -> None:
                who = "" if s["launched_by"] == "user" else f" {DIM}(launched by agent, call {s['parent_call_id']}){RESET}"
                archived_mark = f" {YELLOW}[archived]{RESET}" if s["archived"] else ""
                print(f"{'  ' * depth}{'└ ' if depth else ''}{s['id']}  {s['status']:<4}  {s['agent']:<8} {s['model']:<28} {s['title']}{who}{archived_mark}")
                for child in sorted(kids.get(s["id"], []), key=lambda c: c["created"]):
                    show(child, depth + 1)
            for s in kids.get(None, []):
                show(s, 0)
        elif args.action == "show":
            for m in await client.call("GET", f"/api/session/{args.id}/message"):
                print(f"{BOLD}#{m['seq']} {m['role']}{RESET}")
                for part in m["parts"]:
                    body = part.get("text") or part.get("summary") or part.get("output") or f"{part.get('name')}({part.get('arguments')})"
                    print(f"  {DIM}{part['type']}:{RESET} {body}")
        elif args.action == "rm":
            await client.call("DELETE", f"/api/session/{args.id}")
            print(f"deleted {args.id}")
        else:  # archive | unarchive
            session = await client.call("POST", f"/api/session/{args.id}/{args.action}")
            state = "archived" if session["archived"] else "unarchived"
            print(f"{state} {session['id']} (whole tree)")
    finally:
        await client.close()


async def _workspace(args: argparse.Namespace) -> None:
    """Workspace controls through the API: activation, a directory move, worktrees."""
    client = Client.connect()
    try:
        sid = args.session
        if args.action == "expose":
            session = await client.call("POST", f"/api/session/{sid}/workspace-tools", {})
            if args.json:
                print(json.dumps(session, indent=2))
                return
            state = session["workspace"]
            print(f"{DIM}workspace tools {'active' if state['active'] else 'not active'} · "
                  f"entry {'available' if state['entry'] else 'not available (older session)'}{RESET}")
            print(f"{DIM}{', '.join(t for t in session['tools'] if t in ('change_session_directory', 'worktree', 'expose_workspace_tools')) or 'none'}{RESET}")
            return
        if args.action == "directory":
            if not args.argument:
                raise ValueError("workspace directory needs a path")
            target = str(Path(args.argument).expanduser().resolve())
            session = await client.call("POST", f"/api/session/{sid}/directory", {"directory": target})
            change = session.get("change") or {}
            if args.json:
                print(json.dumps(session, indent=2))
            elif change.get("status") == "pending":
                print(f"{YELLOW}pending: {session['directory']} → {change.get('to')} "
                      f"(applies at the next tool boundary or when the turn ends){RESET}")
            elif change.get("status") == "unchanged":
                print(f"{DIM}already in {session['directory']}{RESET}")
            else:
                branch = f" · branch {change['branch']}" if change.get("branch") else ""
                print(f"{session['directory']}{branch}")
            return
        if args.action == "list":
            data = await client.call("GET", f"/api/session/{sid}/worktrees")
            if args.json:
                print(json.dumps(data, indent=2))
                return
            print(f"{BOLD}{data['repository']}{RESET} {DIM}(main checkout){RESET}")
            for item in data["worktrees"]:
                flags = " main" if item["main"] else ""
                if item.get("dirty"):
                    state = f"{item['changes']} uncommitted"
                elif item.get("dirty") is False:
                    state = "clean"
                else:
                    state = "status unavailable"
                print(f"  {item['path']}{flags}  {DIM}{item.get('branch') or 'no branch'} · {state}{RESET}")
            return
        if not args.argument:
            raise ValueError(f"workspace {args.action} needs a worktree path")
        body: dict[str, Any] = {"action": args.action, "path": str(Path(args.argument).expanduser().resolve())}
        if args.branch:
            body["branch"] = args.branch
        if args.base:
            body["base"] = args.base
        data = await client.call("POST", f"/api/session/{sid}/worktrees", body)
        if args.json:
            print(json.dumps(data, indent=2))
        elif args.action == "create":
            verb = "reused" if data.get("reused") else "created"
            print(f"{verb} {data['path']} on {data.get('branch') or 'no branch'} "
                  f"{DIM}(session directory unchanged: {data['session_directory']}){RESET}")
        else:
            print(f"deleted {data['path']} {DIM}(branch {data.get('branch') or 'none'} kept){RESET}")
    finally:
        await client.close()


# -- local commands ----------------------------------------------------------


async def _login(args: argparse.Namespace) -> None:
    auth = SIGN_INS[args.provider]()
    method = "device" if args.device else (args.method or auth.methods[0])
    if method not in auth.methods:
        raise AuthError(f"{args.provider}: sign-in methods are {', '.join(auth.methods)}")
    await auth.login(method)
    print(f"Logged in to {args.provider} ({PROVIDERS[args.provider].label}).")


def _tokens(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    return f"{value / 1_000_000:.3g}M" if value >= 1_000_000 else f"{value / 1000:.0f}k"


def _price(value: Any) -> str:
    return f"${value:g}" if isinstance(value, (int, float)) else "?"


async def _models(args: argparse.Namespace) -> None:
    data, source = await catalog.load(refresh=args.refresh)
    print(f"{DIM}catalog: {source} · {catalog.CATALOG_URL}{RESET}")
    live = set(connected())
    for pid, info in PROVIDERS.items():
        if args.provider and pid != args.provider:
            continue
        if pid not in live and not args.all:
            print(f"\n{BOLD}{pid}{RESET} {DIM}{info.label} · not connected (nexus login {pid}){RESET}")
            continue
        models = catalog.models_for(data, pid)
        state = "connected" if pid in live else "not connected"
        print(f"\n{BOLD}{pid}{RESET} {DIM}{info.label} · {state} · {len(models)} models from models.dev/{info.catalog_id}{RESET}")
        width = max((len(m.id) for m in models), default=0)
        for m in models:
            efforts = " ".join(m.efforts) if m.efforts else ("yes" if m.reasoning else "no")
            print(
                f"  {m.id:<{width}}  {DIM}context{RESET} {_tokens(m.context):>5}  {DIM}output{RESET} {_tokens(m.max_output):>5}"
                f"  {DIM}in/out{RESET} {_price(m.input_cost)}/{_price(m.output_cost)}"
                f"  {DIM}reasoning{RESET} {efforts}  {DIM}{m.released or ''}{RESET}"
            )
        if pid == "codex":
            print(f"  {YELLOW}Codex has no models.dev entry; this is the openai list filtered to reasoning gpt-* models.{RESET}")


def _daemon(action: str) -> None:
    if action == "start":
        found = daemon.ensure()
        print(f"running at {found['url']} (pid {found['pid']})")
    elif action == "stop":
        print("stopped" if daemon.stop() else "not running")
    else:
        found = daemon.discover()
        print(f"running at {found['url']} (pid {found['pid']}) · logs {daemon.log_path()}" if found else "not running")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nexus")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="sign in to a provider")
    login.add_argument("provider", choices=list(PROVIDERS))
    login.add_argument("--method", help="browser | device | api_key (default: the provider's first)")
    login.add_argument("--device", action="store_true", help="shorthand for --method device")
    logout = sub.add_parser("logout", help="forget a provider's stored credentials")
    logout.add_argument("provider", choices=list(PROVIDERS))
    sub.add_parser("status", help="which providers are connected")
    models = sub.add_parser("models", help="models available on connected providers (models.dev)")
    models.add_argument("provider", nargs="?", choices=list(PROVIDERS))
    models.add_argument("--all", action="store_true", help="also list providers that are not connected")
    models.add_argument("--refresh", action="store_true", help="download the catalog now")

    serve = sub.add_parser("serve", help="run the daemon in the foreground")
    serve.add_argument("--port", type=int)
    dmn = sub.add_parser("daemon", help="start, stop or check the background daemon")
    dmn.add_argument("action", choices=["start", "stop", "status"])

    project = sub.add_parser("project", help="projects (a project is a directory)")
    project.add_argument("action", choices=["list", "open"])
    project.add_argument("path", nargs="?")
    agent = sub.add_parser("agent", help="agents available in the current project, or one agent's prompt")
    agent.add_argument("name", nargs="?")
    agent.add_argument("--workspace", default=".")
    session = sub.add_parser("session", aliases=["sessions"], help="sessions of the current project; `sessions inspector` opens the web inspector")
    session.add_argument("action", choices=["list", "show", "rm", "archive", "unarchive", "inspector"])
    session.add_argument("id", nargs="?")
    session.add_argument("--all", action="store_true", help="sessions of every project")
    archive_filter = session.add_mutually_exclusive_group()
    archive_filter.add_argument("--archived-only", action="store_true", help="list only archived trees")
    archive_filter.add_argument("--include-archived", action="store_true", help="include archived and unarchived trees")
    session.add_argument("--workspace", default=".")
    session.add_argument("--no-open", action="store_true", help="inspector: print the URL instead of opening a browser")
    workspace = sub.add_parser("workspace", help="workspace controls through the API: expose, directory, worktrees")
    workspace.add_argument("action", choices=["expose", "directory", "list", "create", "delete"])
    workspace.add_argument("argument", nargs="?", help="directory (directory) or worktree path (create/delete)")
    workspace.add_argument("--session", required=True, help="the session to change")
    workspace.add_argument("--branch", help="create: reuse or create this branch")
    workspace.add_argument("--base", help="create: commit or branch to start a new branch from")
    workspace.add_argument("--json", action="store_true", help="print the API JSON")

    for name in ("chat", "run"):
        cmd = sub.add_parser(name)
        if name == "run":
            cmd.add_argument("prompt")
        cmd.add_argument("--agent", help="agent for the session (default: build; see `nexus agent`)")
        cmd.add_argument("--model", help="provider/model; overrides the agent's preference (see `nexus models`)")
        cmd.add_argument("--effort", help="reasoning effort for the session, e.g. low | high (see `nexus models`); '' clears it")
        cmd.add_argument("--disable", metavar="TOOLS", help="comma-separated tools to switch off for the session, e.g. bash,write; '' turns all back on")
        cmd.add_argument("--session", help="continue this session id")
        cmd.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the project's latest session")
        cmd.add_argument("--workspace", default=".", help="the project directory")
        cmd.add_argument("--wire", action="store_true", help="print the raw request and every provider event")
        cmd.add_argument("--wire-file", help="append wire traffic to this JSONL file")
        cmd.add_argument("--yes", action="store_true", help="set the session to run bash/edit/write without asking")
    args = parser.parse_args(argv)

    try:
        if args.command == "login":
            asyncio.run(_login(args))
        elif args.command == "logout":
            SIGN_INS[args.provider]().logout()
            print(f"Logged out of {args.provider}.")
        elif args.command == "status":
            live = set(connected())
            for pid, info in PROVIDERS.items():
                state = "connected" if pid in live else f"not connected (nexus login {pid})"
                print(f"{pid:<9} {info.label:<17} {info.dialect:<9} {state}")
        elif args.command == "models":
            asyncio.run(_models(args))
        elif args.command == "serve":
            daemon.serve(args.port)
        elif args.command == "daemon":
            _daemon(args.action)
        elif args.command == "agent":
            asyncio.run(_agent(args))
        elif args.command == "project":
            asyncio.run(_project(args))
        elif args.command in ("session", "sessions") and args.action == "inspector":
            found = daemon.ensure()
            url = f"{found['url']}/inspector#token={found['token']}" + (f"&session={args.id}" if args.id else "")
            print(f"Session inspector: {url}")
            if not args.no_open:
                webbrowser.open(url)
        elif args.command in ("session", "sessions"):
            if args.action in ("show", "rm", "archive", "unarchive") and not args.id:
                parser.error(f"session {args.action} needs an id")
            asyncio.run(_session(args))
        elif args.command == "workspace":
            asyncio.run(_workspace(args))
        else:
            with contextlib.ExitStack() as stack:
                wire_file = stack.enter_context(open(args.wire_file, "a")) if args.wire_file else None
                renderer = Renderer(wire=args.wire, wire_file=wire_file)
                asyncio.run(_chat(args, renderer) if args.command == "chat" else _run(args, renderer))
    except (AuthError, CredentialError, ApiError, ValueError, OSError) as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
