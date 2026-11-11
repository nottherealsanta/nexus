r"""The daemon's HTTP API, modelled on OpenCode's `/api/*` (opencode.ai/v2/docs/api).

All IPv4 interfaces by default, without a token: trusted networks only. An explicit
loopback bind (`NEXUS_HOST=127.0.0.1` or `--host 127.0.0.1`) requires
`Authorization: Bearer <token>` (in `<home>/daemon.json`, mode 0600).
`GET /api/event` also accepts `?token=` for browser `EventSource`.
Errors are `{"error": "..."}`
with 400, 401, 404, 409, 410, 413, 422 or 502. The table is not the whole API:
files, git, pending inputs, draft context, provider login and the rest of voice
are in docs/api.md.

| method | path | what |
| --- | --- | --- |
| GET | /api/info | daemon identity: version, pid, home, url, log generation and head |
| GET | /api/provider | the provider table, with `connected` |
| GET | /api/model | models of connected providers (models.dev); `?provider=` |
| GET | /api/model/last | `{model, source}`: the model a new session for this project and agent starts with |
| GET | /api/agent | agents for `?project=` or `?directory=` (built-in, global, project) and file problems |
| GET | /api/agent/{name} | one agent, with its full prompt |
| GET | /api/skill | skills for `?project=` or `?directory=` (project over global) and file problems |
| POST | /api/skill/preview | `{source, scope, project?}`: fetch a source and list its skills (`{fetch_id, found: [{name, description, installed}], problems}`); the fetch is kept 10 minutes |
| POST | /api/skill/install | `{source, scope: project or global, project?\|directory?, overwrite?}`: copy skills from a Git repo or folder (`/learn`); returns `{installed, skipped, problems, available}` |
| GET | /api/skill/{name} | one skill: frontmatter facts, body and bundled files |
| GET | /api/mcp | MCP servers: state, tool count, error and (no secret) settings |
| GET | /api/mcp/{name} | one server: the same, plus its tool names once connected; never connects |
| POST | /api/mcp/{name}/restart | close and reconnect one server |
| GET | /api/mcp/{name}/resources, /resource?uri=, /prompts, /prompt?name=&arguments= | read-only MCP resources and prompts (connects the server; fenced untrusted text; also under `/api/project/{id}/mcp/{name}/`) |
| GET | /api/extension | user tools for `?project=` or `?directory=` (global and project) and file problems |
| GET | /api/voice | offline voice model status |
| GET | /api/search/service | state of the local SearXNG behind `web_search` |
| POST | /api/search/service/start | start it and wait until it answers |
| POST | /api/voice/transcribe | mono 16 kHz PCM16 WAV → `{text}`; `?partial=1` never queues |
| POST | /api/voice/stt/download | consented, pinned STT model download |
| POST | /api/voice/speak | `{session_id}` → streamed WAV; final answer only |
| POST | /api/voice/speak/stop | stop the active speech stream |
| GET | /api/project | all projects |
| POST | /api/project | `{path, name?}` → the project at that path (created if new) |
| GET/PATCH/DELETE | /api/project/{id} | one project; PATCH `{name}` |
| GET | /api/project/{id}/worktrees | the project checkout's worktrees, before any session exists |
| GET | /api/project/{id}/worktree/review | `?worktree=`, `?base=`: what integrating the worktree's branch into its base would do (commits, files, conflicts, blockers) |
| POST | /api/project/{id}/worktree/integrate | keyed; `{worktree, expected_head, mode: ff or merge, base?, message?, remove?, confirmed_live?}`: merge the reviewed branch into its base checkout |
| GET | /api/project/{id}/mcp | this project's MCP files (.mcp.json, .agents/mcp.json): files, duplicates, revision, error, warnings and server rows |
| GET | /api/project/{id}/mcp/{name} | one project server, plus `tool_names` and `log`; never connects |
| POST | /api/project/{id}/mcp/{name}/restart | close and reconnect a project server |
| GET | /api/session | `?project=`, `?directory=` or `?parent=`; `?archived=` exclude/only/include |
| POST | /api/session | `{project? or directory? or parent?, agent?, title?, model?, permission?, effort?}` |
| GET/PATCH/DELETE | /api/session/{id} | one session; PATCH `{agent?, title?, model?, permission?, effort?}` |
| POST | /api/session/{id}/archive | archive the whole root tree (child ids get 400) |
| POST | /api/session/{id}/unarchive | unarchive the whole root tree |
| POST | /api/session/{id}/model | `{model}`; `""` returns to the agent's own model (400 if it names none) |
| POST | /api/session/{id}/agent | `{agent}` |
| POST | /api/session/{id}/workspace-tools | activate the two workspace control tools (idempotent) |
| POST | /api/session/{id}/directory | `{directory}`; 200 applied or 202 pending while a call runs |
| GET | /api/session/{id}/worktrees | list the repository's worktrees with branch and working-tree state |
| POST | /api/session/{id}/worktrees | `{action: "list" or "create" or "delete", path?, branch?, base?}` |
| POST | /api/session/{id}/prompt | `{text?, images?, mode?}` → 202; `send` is 409 while a turn runs, `steer`/`queue` return an input id |
| POST | /api/session/{id}/continue | start a turn from the stored context with no new user message → 202 |
| POST | /api/session/{id}/interrupt | cancel the running turn → 204 |
| GET | /api/session/{id}/message | saved messages, in order |
| GET | /api/session/{id}/event | stored events: `?after=<event id>&limit=&type=a,b` (the durable log) |
| GET | /api/session/{id}/turn | turn summaries (usage, model, timings, retries) and the spend ledger: `?after=` or `?before=<turn id>&limit=` |
| GET | /api/session/{id}/children | direct children |
| GET | /api/session/{id}/tree | the whole tree containing it, from its root, nested |
| POST | /api/session/{id}/btw | answer a side question from the context so far; nothing is stored |
| POST | /api/session/{id}/handoff | summarise, then start a continued root session → 201 |
| GET | /api/session/{id}/context | the request body the next turn would send |
| GET | /api/session/{id}/snapshot | the full frozen session snapshot |
| GET | /api/session/{id}/export | `?format=markdown or json&scope=session or tree`: a read-only export of one session or its descendants |
| GET | /api/session/{id}/permission | pending tool approvals |
| POST | /api/session/{id}/permission/{pid} | `{reply: "allow" | "deny"}` → 204 |
| GET | /api/question | every pending `ask` question request |
| GET | /api/session/{id}/question | pending questions for that session |
| POST | /api/session/{id}/question/{qid} | `{answers: [...]}` → 204; 400 invalid, 404 unknown, 409 already resolved |
| GET | /inspector | the session inspector page (static; it reads the API with the token from its URL fragment) |
| POST | /api/client/view | ephemeral view per open event stream: `{stream_id, client_id, project, session, panel}`; there is no read route |
| GET | /api/manager | the Manager: `{enabled, mode, model, call}`; restores a call after a reconnect |
| GET | /api/manager/episode | the Manager's own past conversations, newest first, so the inspector can open one by id |
| GET | /api/manager/audit | what the Manager did on the user's words: `?limit=&tool=` |
| POST | /api/manager/start | `{stream_id, client_id}` → start or move the Manager call (409 `manager_disabled`) |
| POST | /api/manager/consent | `{accept, version}` → the provider privacy notice acknowledged or revoked (`GET /api/manager` carries it) |
| POST | /api/voice/speak | `{session_id}` as before, or `{stream_id, text}`: one Manager sentence as a streamed WAV (holder only, redacted, 1200 characters at most) |
| POST | /api/manager/stop | `{stream_id}` → end the call; never stops a session |
| POST | /api/manager/say | `{stream_id, text, source}` → 202 `{turn_id}`; the answer arrives as `manager.*` events |
| POST | /api/manager/cancel | `{stream_id}` → barge-in: mute speech, cancel the turn if nothing had side effects |
| POST | /api/manager/activity | `{stream_id, listening, speaking}` → 204 |
| POST | /api/manager/card/dismiss | `{stream_id, card_id}` → 204 |
| POST | /api/manager/navigate/result | `{stream_id, request_id, ok, reason?}` → 204 |
| GET | /api/event | server-sent events; `?session=` filters to that session and its descendants; `?cursor=` or `Last-Event-ID` replays from a durable cursor |
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

from anyio import CancelScope
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import agents as agents_module
from . import export
from . import extensions as extensions_module
from . import mcp as mcp_module
from . import skills as skills_module
from .bus import Bus
from .codec import to_json
from .config import ConfigConflict, ConfigFile
from .doctor import source_fingerprint
from .eventlog import CursorError, Stream, format_cursor, parse_cursor
from .inputs import InputBusy, InputNotFound
from .llmhub import catalog
from .llmhub.auth import SIGN_INS, ApiKeyAuth, AuthError, CredentialError
from .llmhub.hub import connected
from .llmhub.providers import PROVIDERS
from .llmhub.types import ProviderError
from .manager.consent import NOTICE_VERSION
from .manager.redact import redact
from .manager.service import LIVE_CONSENT_REF, Forbidden
from .mcp import manager as mcp_manager
from .oneshot import OneshotError
from .paths import home
from .runtime import Busy, Conflict, NotFound, Runtime, message_json, project_json
from .skills import install as skills_install
from .store import TURN_PAGE, TURN_PAGE_MAX, Store
from .terminal_api import routes as terminal_routes
from .terminals import TerminalService
from .tools import ToolError
from .voice.audio import MAX_WAV_BYTES, VoiceAudioError, parse_wav
from .voice.manager import VoiceManager
from .voice.model import VoiceError
from .voice.speech import SpeechError, SpeechService, final_answer
from .workers.registry import visible as zmx_visible

VERSION = version("nexus10")
MAX_SPEAK_CHARS = 1200  # one Manager sentence is at most 400; this leaves room for a coalesced line
# The HTTP API contract clients check before sending anything else. A change
# that a client written for an older major cannot survive is a new major; new
# fields and endpoints are a minor bump.
API_VERSION = {"major": 1, "minor": 5}
INSPECTOR = Path(__file__).parent / "inspector" / "index.html"
INSPECTOR_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"
MAX_BODY_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 12 * 1024 * 1024
# A project file is limited to 1 MiB of UTF-8, but its JSON form can be larger:
# every control character becomes six bytes and a 4-byte character twelve. The
# route still enforces the content limit; only the envelope gets more room.
MAX_FILE_BODY_BYTES = 8 * 1024 * 1024


def _version(name: str) -> tuple[int, ...]:
    """The numbers in a model's display name, so "Sonnet 5.5" sorts above "Sonnet 5" and "Sonnet 4.6"."""
    return tuple(int(n) for n in re.findall(r"\d+", name.split("(")[0])[:3])


def _body_limit(request: Request) -> int:
    if request.url.path == "/api/voice/transcribe":
        return MAX_WAV_BYTES
    if request.method == "PUT" and request.url.path.endswith("/file"):
        return MAX_FILE_BODY_BYTES
    return MAX_PROMPT_BYTES if request.url.path.endswith("/prompt") else MAX_BODY_BYTES

IDEMPOTENCY_MAX = 128
_IDEMPOTENCY_KEY = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
_MUTATING = frozenset({"POST", "PATCH", "DELETE"})
_KEYED = ("/prompt", "/continue")

Handler = Callable[[Request], Awaitable[Response]]


def _missing(exc: ValueError) -> NotFound | None:
    """A path that does not exist is a 404, not a bad request."""
    return NotFound(str(exc)) if isinstance(exc.__cause__, FileNotFoundError) else None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > _body_limit(request):
        raise ValueError("request body too large")
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("request body must be a JSON object")
    return data


def _archive_filter(request: Request) -> str:
    """`?archived=exclude` (default), `only` or `include` for session lists."""
    mode = request.query_params.get("archived") or "exclude"
    if mode not in ("exclude", "only", "include"):
        raise ValueError("archived must be 'exclude', 'only' or 'include'")
    return mode


def _idempotency_key(request: Request) -> str | None:
    key = request.headers.get("idempotency-key")
    if key is None:
        return None
    if not _IDEMPOTENCY_KEY.match(key):
        raise ValueError(
            "Idempotency-Key must be 1-128 characters from A-Z, a-z, 0-9, '.', '_', ':' or '-'")
    return key


def _fingerprint(request: Request, raw: bytes) -> str:
    """Stable hash of method, target and semantic body; credentials are not included."""
    try:
        body: Any = json.loads(raw) if raw else None
    except ValueError:
        body = raw.decode("utf-8", "replace")
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    query = sorted(request.query_params.multi_items())
    target = json.dumps([request.method, request.url.path, query, _expected_revision(request)], separators=(",", ":"))
    return "sha256:" + hashlib.sha256((target + "\n" + canonical).encode("utf-8")).hexdigest()


def _expected_revision(request: Request, body: dict[str, Any] | None = None) -> int | None:
    raw: Any = request.headers.get("if-match")
    if raw is None and body is not None:
        raw = body.get("expected_revision")
    if raw is None:
        return None
    text = str(raw).strip()
    if text.startswith("W/"):
        text = text[2:].strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError("If-Match/expected_revision must be a positive integer revision") from exc
    if value < 0:
        raise ValueError("If-Match/expected_revision must be a positive integer revision")
    return value


def _json_payload(response: Response) -> Any:
    if not response.body:
        return None
    try:
        return json.loads(response.body)
    except (ValueError, TypeError):
        return None


_EXHAUSTED = object()


def _next_chunk(iterator: Iterator[str]) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return _EXHAUSTED


async def _stream_export(iterator: Iterator[str]) -> AsyncIterator[str]:
    """Run the synchronous export generator off the event loop and always close it."""
    try:
        while True:
            pending = asyncio.create_task(run_in_threadpool(_next_chunk, iterator))
            try:
                chunk = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # Do not close a generator/SQLite view while its worker is still
                # executing the current chunk.
                with CancelScope(shield=True), suppress(Exception):
                    await asyncio.shield(pending)
                raise
            if chunk is _EXHAUSTED:
                break
            yield chunk
    finally:
        iterator.close()


class ExportResponse(StreamingResponse):
    """Release the read transaction even when sending response headers fails."""

    def __init__(self, *args: Any, view: export.ExportView, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.export_view = view

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.export_view.close()


class EventStreamResponse(StreamingResponse):
    """Close the live subscription even if the response body never starts."""

    def __init__(self, *args: Any, stream: Stream, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stream = stream

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.stream.close()


def sse(event: dict[str, Any]) -> str:
    """One SSE frame; live-only deltas and stream control carry no `id:` line."""
    lines = []
    if event.get("id") is not None:
        lines.append(f"id: {event.get('cursor') or event['id']}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event)}")
    return "\n".join(lines) + "\n\n"


_VIEW_TEXT = re.compile(r"\A[\x21-\x7e]{1,128}\Z")


def _view_text(value: Any, name: str, *, required: bool = False) -> str | None:
    """A short opaque view id, or `None` for an empty optional field."""
    if value is None or value == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or null")
    if not _VIEW_TEXT.match(value):
        raise ValueError(f"{name} must be 1-128 printable ASCII characters without spaces")
    return value


class ClientViews:
    """Ephemeral views reported by clients, one per open event stream.

    The key is the stream id issued in `server.connected`, never `client_id`:
    two windows can share a client id, and a reconnect is a new subscriber.
    Closing the stream drops only its view. Nothing here is durable, published
    as an event, or exposed through a read route. An in-process Manager may
    inspect `views()` but must decide which client holds the call itself.
    """

    def __init__(self) -> None:
        self._streams: set[str] = set()
        self._views: dict[str, dict[str, Any]] = {}
        self._detach_listeners: list[Callable[[str], None]] = []
        self._sequence = 0

    def attach(self, stream: Stream) -> None:
        self._streams.add(stream.stream_id)
        stream.add_close_listener(self._detach)

    def _detach(self, stream: Stream) -> None:
        self._streams.discard(stream.stream_id)
        self._views.pop(stream.stream_id, None)
        for listener in list(self._detach_listeners):
            try:
                listener(stream.stream_id)
            except Exception:
                logging.getLogger(__name__).exception("client view close listener failed")

    def on_detach(self, listener: Callable[[str], None]) -> None:
        """Call ``listener(stream_id)`` when any stream closes (the Manager ends a call this way)."""
        self._detach_listeners.append(listener)

    def has_stream(self, stream_id: str) -> bool:
        return stream_id in self._streams

    def view_for(self, stream_id: str) -> dict[str, Any] | None:
        """One stream's view, for the in-process Manager. The HTTP API has no read route for it."""
        view = self._views.get(stream_id)
        return self._public(view) if view is not None else None

    def report(self, stream_id: str, client_id: str, *, project: str | None,
               session: str | None, panel: str | None) -> dict[str, Any]:
        if stream_id not in self._streams:
            raise Conflict(
                "the event stream for this view is not connected; "
                "reconnect and report its new stream_id",
                code="stream_closed")
        previous = self._views.get(stream_id)
        if previous is not None and previous["client_id"] != client_id:
            raise Conflict(
                "this event stream is already bound to another client; "
                "open a new event stream and report from it",
                code="client_mismatch")
        self._sequence += 1
        view = {"stream_id": stream_id, "client_id": client_id, "project": project,
                "session": session, "panel": panel, "updated": time.time(),
                "_sequence": self._sequence}
        self._views[stream_id] = view
        return self._public(view)

    @staticmethod
    def _public(view: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in view.items() if not key.startswith("_")}

    def views(self) -> list[dict[str, Any]]:
        return [self._public(view) for view in sorted(
            self._views.values(), key=lambda view: view["_sequence"])]


def build_app(runtime: Runtime, *, token: str, url: str, voice_manager: VoiceManager | None = None) -> Starlette:
    config = ConfigFile()
    from .provider_usage import UsageCache

    usage_cache = UsageCache(runtime.store.db, lambda: config.read()["values"]["usage"]["refresh_minutes"])
    code_fingerprint = source_fingerprint()  # what `nexus doctor` compares with the checkout
    config_lock = asyncio.Lock()
    config_revision = config.read()["revision"]
    client_views = ClientViews()
    runtime.manager.attach_views(client_views)
    terminal_service = TerminalService(runtime.store.db)
    from .file_watch import FileWatches

    file_watches = FileWatches(runtime.bus.publish)

    async def config_api(request: Request) -> Response:
        nonlocal config_revision
        async with config_lock:
            if request.method == "GET":
                return JSONResponse(config.read())
            body = await _body(request)
            revision = body.get("expected_revision")
            set_values = body.get("set", {})
            unset = body.get("unset", [])
            if not isinstance(revision, str) or not isinstance(set_values, dict) or not isinstance(unset, list):
                raise TypeError("expected_revision must be a string, set an object, and unset an array")
            if not all(isinstance(key, str) for key in set_values) or not all(isinstance(key, str) for key in unset):
                raise ValueError("setting paths must be strings")
            try:
                result = config.patch(revision, set_values, unset)
            except ConfigConflict as exc:
                return JSONResponse({"error": str(exc), "code": "revision_conflict"}, status_code=409)
            config_revision = result["revision"]
            runtime.bus.publish("config.changed", {"revision": config_revision})
            return JSONResponse(result)

    def project_paths() -> list[str]:
        return [x.path for x in runtime.store.projects()]

    def publish_extension_problems() -> None:
        seen: set[str] = set()
        for path in [None, *project_paths()]:
            for problem in runtime.extensions(path).problems:
                if problem["path"] not in seen:
                    seen.add(problem["path"])
                    runtime.bus.publish("extension.failed", problem)

    def mcp_signature() -> str:
        servers = config.mcp_servers()
        modes = config.mcp_modes()
        return hashlib.sha256(json.dumps([servers, modes], sort_keys=True, default=str).encode()).hexdigest()[:16]

    async def watch_config() -> None:
        nonlocal config_revision
        agent_stamp = agents_module.fingerprint(project_paths())
        tool_stamp = extensions_module.fingerprint(project_paths())
        skill_stamp = skills_module.fingerprint([None, *project_paths()])
        mcp_stamp = mcp_signature()
        while True:
            await asyncio.sleep(2)
            paths = project_paths()
            stamp = agents_module.fingerprint(paths)
            if stamp != agent_stamp:
                agent_stamp = stamp
                runtime.bus.publish("agents.changed", {})
            stamp = extensions_module.fingerprint(paths)
            if stamp != tool_stamp:
                tool_stamp = stamp
                runtime.bus.publish("extensions.changed", {})
                publish_extension_problems()
            stamp = skills_module.fingerprint([None, *project_paths()])
            if stamp != skill_stamp:
                skill_stamp = stamp
                runtime.bus.publish("skills.changed", {})
            signature = mcp_signature()
            if signature != mcp_stamp:
                mcp_stamp = signature
                runtime.bus.publish("mcp.changed", {})  # running sessions keep their servers; new ones use these
            async with config_lock:
                revision = config.read()["revision"]
                if revision != config_revision:
                    runtime.bus.publish("config.changed", {"revision": revision})
                    config_revision = revision

    login_tasks: dict[str, asyncio.Task[None]] = {}
    login_states: dict[str, dict[str, Any]] = {}

    async def provider_login(request: Request) -> Response:
        provider = request.path_params["id"]
        if provider not in SIGN_INS:
            raise NotFound(f"unknown provider {provider}")
        auth = SIGN_INS[provider]()
        if request.method == "GET":
            return JSONResponse(login_states.get(provider, {"status": "idle"}))
        if request.method == "DELETE":
            task = login_tasks.pop(provider, None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            auth.logout()
            login_states.pop(provider, None)
            runtime.bus.publish("provider.login", {"provider": provider, "status": "signed_out"})
            return Response(status_code=204)
        body = await _body(request)
        if provider in login_tasks and not login_tasks[provider].done():
            raise Busy(f"{provider} sign-in already running")
        method = body.get("method", "device" if "device" in auth.methods else auth.methods[0])
        if method not in auth.methods:
            raise ValueError(f"{provider} sign-in methods are {', '.join(auth.methods)}")
        if method == "api_key":
            key = body.get("api_key")
            if not isinstance(key, str):
                raise TypeError("api_key must be a string")
            try:
                auth.set_key(key)
            except AuthError as exc:
                raise ValueError(str(exc)) from exc
            state = {"provider": provider, "status": "connected"}
            login_states[provider] = state
            runtime.bus.publish("provider.login", state)
            return JSONResponse(state)
        state = {"provider": provider, "status": "pending", "message": None}
        login_states[provider] = state

        def notify(message: str) -> None:
            state["message"] = message
            # Device flows say "Open <url> and enter code <code>"; browser flows
            # say "Open <url> to sign in to ChatGPT.". Clients show both parts.
            found = re.search(r"Open (https?://\S+) and enter code (\S+)", message)
            if found:
                state["verification_uri"], state["user_code"] = found.group(1), found.group(2)
            else:
                found = re.search(r"(https?://\S+)", message)
                if found:
                    state["verification_uri"] = found.group(1).rstrip(".,;")
            runtime.bus.publish("provider.login", dict(state))

        def handoff(url: str) -> bool:
            # The daemon never launches a browser: the client opens the URL.
            notify(f"Open {url} to sign in to ChatGPT.")
            return True

        async def run() -> None:
            try:
                if method == "browser":
                    await auth.login(method, notify=notify, browser_open=handoff)
                else:
                    await auth.login(method, notify=notify)
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                state.update(status="failed", message=str(exc), verification_uri=None, user_code=None)
            except OSError:
                state.update(status="failed", verification_uri=None, user_code=None,
                             message=("Port 1455 is in use; try the device code." if method == "browser"
                                      else "Sign-in failed; retry or use the local login command."))
            except Exception:  # noqa: BLE001 - never leak provider failures or credentials
                state.update(status="failed", message="Sign-in failed; retry or use the local login command.",
                             verification_uri=None, user_code=None)
            else:
                state.update(status="connected", message=None, verification_uri=None, user_code=None)
            runtime.bus.publish("provider.login", dict(state))

        login_tasks[provider] = asyncio.create_task(run())
        return JSONResponse(dict(state), status_code=202)

    def route(handler: Handler) -> Handler:
        async def inner(request: Request) -> Response:
            try:
                return await handler(request)
            except Forbidden as exc:
                return JSONResponse({"error": str(exc), "code": "not_holder"}, status_code=403)
            except (NotFound, InputNotFound) as exc:
                return _error(404, str(exc))
            except (Busy, InputBusy) as exc:
                return _error(409, str(exc))
            except Conflict as exc:
                payload: dict[str, Any] = {"error": str(exc)}
                if exc.code:
                    payload["code"] = exc.code
                if exc.payload:
                    payload.update(exc.payload)
                return JSONResponse(payload, status_code=409)
            except CursorError as exc:
                payload = exc.payload(runtime.store)
                return JSONResponse(payload, status_code=exc.status,
                                    headers={"x-nexus-log-generation": payload["generation"]})
            except ToolError as exc:
                return _error(400, str(exc))
            except (ProviderError, AuthError, CredentialError, OneshotError) as exc:
                return _error(502, str(exc))
            except (ValueError, KeyError, TypeError) as exc:
                return _error(400, str(exc))

        def replay(entry: dict[str, Any]) -> Response:
            headers = {"idempotent-replayed": "true", "idempotency-key": entry["key"],
                       "cache-control": "no-store"}
            body = entry.get("body")
            status = entry.get("status") or 200
            if body is None:
                return Response(status_code=status, headers=headers)
            return JSONResponse(body, status_code=status, headers=headers)

        async def wrapped(request: Request) -> Response:
            if token:
                given = request.headers.get("authorization", "").removeprefix("Bearer ")
                if request.url.path == "/api/event":
                    given = given or request.query_params.get("token", "")
                if not hmac.compare_digest(given, token):
                    return _error(401, "missing or wrong token (see daemon.json)")
            try:
                key = _idempotency_key(request)
                if key is None or request.method not in _MUTATING:
                    return await inner(request)
                raw = await request.body()
                if len(raw) > _body_limit(request):
                    raise ValueError("request body too large")
                fingerprint = _fingerprint(request, raw)
                async with runtime.mutation_lock(key):
                    entry = runtime.replay_mutation(key, fingerprint)
                    if entry is not None:
                        return replay(entry)
                    if request.url.path.endswith(_KEYED):
                        request.state.mutation = {
                            "key": key, "fingerprint": fingerprint,
                            "operation": "prompt" if request.url.path.endswith("/prompt") else "continue",
                            "target": request.url.path, "state": "accepted", "status": 202,
                            "body": {"status": "busy", "idempotency_key": key},
                        }
                    keyed_turn = request.url.path.endswith(_KEYED)
                    if not keyed_turn:
                        # Intent must precede *every* mutation, including ordinary
                        # SQLite changes whose helpers commit independently. A
                        # crash in the response-journal gap is unknown, never a
                        # license to repeat a create/delete or release a wait twice.
                        runtime.begin_external_mutation(
                            key, fingerprint, f"{request.method} {request.url.path}", request.url.path)
                    response = await inner(request)
                    if not keyed_turn:
                        payload = _json_payload(response)
                        resource = payload.get("id", payload.get("path")) if isinstance(payload, dict) else None
                        runtime.finish_external_mutation(
                            key, status=response.status_code, body=payload, resource=resource)
                    return response
            except Conflict as exc:
                payload = {"error": str(exc)}
                if exc.code:
                    payload["code"] = exc.code
                if exc.payload:
                    payload.update(exc.payload)
                return JSONResponse(payload, status_code=409)
            except (ValueError, TypeError) as exc:
                return _error(400, str(exc))
        return wrapped

    p = lambda request, name: request.path_params[name]

    async def info(request: Request) -> Response:
        generation = runtime.store.log_generation()
        head = runtime.store.log_head()
        return JSONResponse({
            "name": "nexus", "version": VERSION, "api_version": API_VERSION, "code": code_fingerprint,
            "pid": os.getpid(), "home": str(home()), "url": url,
            "log": {"generation": generation, "head": head, "head_cursor": format_cursor(generation, head),
                    "floor": runtime.store.log_floor(),
                    "floor_cursor": format_cursor(generation, runtime.store.log_floor())},
            "retention": asdict(runtime.retention.settings),
            "retention_status": runtime.retention.metrics(),
            "retry": {"max_attempts": runtime.retry_policy.max_attempts,
                      "base_delay": runtime.retry_policy.base_delay,
                      "max_wait": runtime.retry_policy.max_wait,
                      "max_elapsed": runtime.retry_policy.max_elapsed},
            "idempotency": {"header": "Idempotency-Key", "max_length": IDEMPOTENCY_MAX,
                            "lifetime_seconds": runtime.mutation_lifetime},
        })

    def sign_in(provider: str) -> dict[str, Any]:
        """How a client connects this provider: its methods and, for a pasted key,
        where to get one, its variable and whether the key in use is stored or
        from the environment. `browser` is a local loopback flow: the daemon
        starts the callback server and hands the URL to the client to open."""
        kind = SIGN_INS.get(provider)
        if kind is None:
            return {"methods": []}
        out: dict[str, Any] = {"methods": list(kind.methods)}
        if issubclass(kind, ApiKeyAuth):
            out |= {"key_hint": kind.hint, "key_env": kind.env, "key_source": kind().key_source()}
        return out

    async def providers(request: Request) -> Response:
        live = set(connected())
        return JSONResponse([
            {"id": i.id, "label": i.label, "dialect": i.dialect, "base_url": i.base_url, "catalog": i.catalog_id,
             "connected": i.id in live, "verified": i.verified} | sign_in(i.id)
            for i in PROVIDERS.values()
        ] + [{"id": i.id, "label": i.label, "dialect": i.dialect, "base_url": i.base_url, "catalog": i.catalog_id,
              "connected": i.id in live, "verified": False, "methods": [], "zmx": True}
             for i in zmx_visible().values()])

    async def provider_limits(request: Request) -> Response:
        if request.method == "POST":
            return JSONResponse(await usage_cache.refresh())
        return JSONResponse(usage_cache.snapshot())

    async def session_usage_totals(request: Request) -> Response:
        runtime.session(p(request, "id"))
        return JSONResponse(runtime.store.session_usage(p(request, "id")))

    async def models(request: Request) -> Response:
        data, source = await catalog.load()
        wanted = request.query_params.get("provider")
        # ZMX models carry `zmx: true`; clients list them in subagent pools only, never in a root picker.
        out = [to_json(m) | {"ref": m.ref} | ({"zmx": True} if pid in ("claude-code", "agy") else {})
               for pid in connected() if not wanted or pid == wanted for m in await catalog.list_models(data, pid)]
        out.sort(key=lambda m: (_version(m["name"]), m.get("released") or ""), reverse=True)
        return JSONResponse({"source": source, "models": out})

    async def last_model(request: Request) -> Response:
        """The model a new session for this project and agent starts with: `{model, source}` (`""`/`none` when never chosen)."""
        query = request.query_params
        ref, source = runtime.remembered_model(query.get("project"), query.get("agent"))
        return JSONResponse({"model": ref, "source": source})

    def agent_json(agent: Any, *, prompt: bool, values: dict[str, Any] | None = None) -> dict[str, Any]:
        data = {k: getattr(agent, k) for k in ("name", "description", "source", "path", "mode", "color", "model", "effort", "tools", "steps")}
        data["permission"] = dict(agent.permission)
        if agent.mode in ("subagent", "all"):
            data["subagent"] = runtime.subagent_models(agent, values)  # pool, fallbacks and what an empty pool uses
        return data | ({"prompt": agent.prompt} if prompt else {})

    def agent_project_path(request: Request) -> str | None:
        if request.query_params.get("project"):
            return runtime.project(request.query_params["project"]).path
        if request.query_params.get("directory"):
            return runtime.open_project(request.query_params["directory"]).path
        return None

    async def agent_list(request: Request) -> Response:
        found = runtime.agents(agent_project_path(request))
        values = config.read()["values"]
        return JSONResponse({"agents": [agent_json(a, prompt=False, values=values) for a in found.agents.values()],
                             "problems": found.problems})

    async def extension_list(request: Request) -> Response:
        found = runtime.extensions(agent_project_path(request))
        tools = [{"name": t.name, "description": t.description, "scope": t.scope, "path": t.path,
                  "sha256": t.sha256, "timeout": t.timeout} for t in found.tools.values()]
        return JSONResponse({"tools": tools, "problems": found.problems})

    def skill_json(skill: skills_module.Skill) -> dict[str, Any]:
        return {"name": skill.name, "description": skill.description, "scope": skill.scope, "path": skill.path,
                "sha256": skill.sha256, "allowed_tools": list(skill.allowed_tools), "warnings": list(skill.warnings)}

    async def skill_list(request: Request) -> Response:
        found = runtime.skills(agent_project_path(request))
        return JSONResponse({"skills": [skill_json(s) for s in found.skills.values()], "problems": found.problems})

    async def skill_target(body: dict[str, Any], source: skills_install.Source | None) -> tuple[str, Path]:
        scope = body.get("scope") or (source.scope if source else None)
        project_path = None
        if body.get("project"):
            project_path = runtime.project(body["project"]).path
        elif body.get("directory"):
            project_path = runtime.open_project(body["directory"]).path
        return str(scope), skills_install.destination(str(scope), project_path)

    async def skill_preview(request: Request) -> Response:
        body = await _body(request)
        try:
            source = skills_install.parse(str(body.get("source") or ""))
            scope, dest = await skill_target(body, source)
            held = await skills_install.stage(source)
        except skills_install.LearnError as exc:
            raise ValueError(str(exc)) from exc
        return JSONResponse({"fetch_id": held.id, "source": source.label, "scope": scope, "problems": held.problems,
                             "requested": list(source.skills),
                             "found": skills_install.preview(held, dest)})

    async def skill_install(request: Request) -> Response:
        body = await _body(request)
        overwrite = bool(body.get("overwrite"))
        try:
            if body.get("fetch_id"):
                held = skills_install.staged(str(body["fetch_id"]))
                scope, dest = await skill_target(body, held.source)
                names = body.get("skills")
                if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
                    raise ValueError("skills must be a list of names")
                if names is not None and not names:
                    raise ValueError("choose at least one skill")
                result = await skills_install.install_staged(held, dest, names, overwrite=overwrite)
                skills_install.release(held.id)  # a failed install keeps the fetch, so the user can choose again
            else:
                source = skills_install.parse(str(body.get("source") or ""))
                scope, dest = await skill_target(body, source)
                result = await skills_install.learn(source, dest, overwrite=overwrite)
        except skills_install.LearnError as exc:
            raise ValueError(str(exc)) from exc
        runtime.bus.publish("skills.changed", {})
        return JSONResponse(result | {"scope": scope})

    async def skill_one(request: Request) -> Response:
        found = runtime.skills(agent_project_path(request))
        skill = found.skills.get(p(request, "name"))
        if skill is None:
            raise NotFound(f"no skill {p(request, 'name')}")
        content = Path(skill.path).read_text(encoding="utf-8")
        parsed = skills_module.frontmatter.parse(content.encode("utf-8"))
        return JSONResponse(skill_json(skill) | {
            "body": parsed.body, "content": content, "extra": parsed.extra,
            "children": [entry.name + ("/" if not entry.is_symlink() and entry.is_dir() else "")
                         for entry in sorted(Path(skill.directory).iterdir(), key=lambda entry: entry.name)
                         if entry.name != "SKILL.md"],
            "files": skills_module.resources.listing(skill.directory)})

    async def voice_status(request: Request) -> Response:
        from .voice import status as voice_status_module
        result = voice_status_module.status()
        stt = result["stt"]
        if voice.downloading:
            stt.update(state="downloading", error=None)
        elif voice.error is not None:
            stt.update(state="error", error=voice.error)
        elif voice.loaded and stt["state"] != "unsupported":
            stt.update(state="loaded", error=None)
        tts = result["tts"]
        job = tts_job["task"]
        if job is not None and not job.done():
            tts.update(state="downloading", error=None)
        elif tts_job["error"] is not None and tts["state"] == "absent":
            tts.update(state="error", error=tts_job["error"])
        return JSONResponse(result)

    def voice_error(exc: VoiceError) -> JSONResponse:
        status = 409 if exc.code in {"voice_busy", "voice_model_not_cached"} else 422
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=status)

    async def voice_transcribe(request: Request) -> Response:
        raw = await request.body()
        if len(raw) > MAX_WAV_BYTES:
            return JSONResponse({"error": "audio exceeds the 120 second limit", "code": "voice_too_long"}, status_code=413)
        partial_value = request.query_params.get("partial")
        if partial_value not in (None, "0", "1"):
            return _error(400, "partial must be 0 or 1")
        try:
            parse_wav(raw)
            result = await voice.transcribe(raw, partial=partial_value == "1")
        except VoiceAudioError as exc:
            status = 413 if exc.code == "voice_too_long" else 400
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=status)
        except VoiceError as exc:
            return voice_error(exc)
        return JSONResponse({"text": result.text})

    async def voice_stt_prepare(request: Request) -> Response:
        try:
            await voice.prepare()
        except VoiceError as exc:
            return voice_error(exc)
        return JSONResponse({"state": "loaded"})

    async def voice_stt_download(request: Request) -> Response:
        try:
            state = await voice.start_download(lambda data: runtime.bus.publish("voice.progress", data))
        except VoiceError as exc:
            return voice_error(exc)
        return JSONResponse(state, status_code=202)

    speech = SpeechService()
    voice = voice_manager or VoiceManager(lambda: config.read()["values"]["voice"]["stt_model"])

    async def voice_speak(request: Request) -> Response:
        body = await _body(request)
        manager_text = "session_id" not in body and "text" in body
        if manager_text:
            # The Manager's sentences: holder only, redacted, bounded. Waits briefly for the previous sentence
            # to finish instead of answering 409, so a client may ask for the next one while one still plays.
            runtime.manager.require_holder(_view_text(body.get("stream_id"), "stream_id") or "")
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("text must be a non-empty string")
            text = redact(text).strip()
            if len(text) > MAX_SPEAK_CHARS:
                return JSONResponse({"error": f"text is {len(text)} characters; the limit is {MAX_SPEAK_CHARS}",
                                     "code": "speech_text_too_long"}, status_code=422)
            for _ in range(200):
                if not speech.busy:
                    break
                runtime.manager.require_holder(body["stream_id"])
                await asyncio.sleep(0.05)
        else:
            sid = str(body.get("session_id", ""))
            runtime.session(sid)
        try:
            if not manager_text:
                text = final_answer(runtime.store, sid)
            repo = config.read()["values"]["speech"]["tts_model"]
            stream = await speech.speak(text, repo)
        except SpeechError as exc:
            status = 409 if exc.code in ("speech_busy", "speech_turn_active") else 404 if exc.code == "speech_no_answer" else 422
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=status)
        return StreamingResponse(stream, media_type="audio/wav", headers={"cache-control": "no-store"})

    async def voice_speak_stop(request: Request) -> Response:
        return JSONResponse({"stopped": speech.stop()})

    tts_job: dict[str, Any] = {"task": None, "error": None}

    async def voice_tts_download(request: Request) -> Response:
        from .voice import download as voice_download
        from .voice import status as voice_status_module
        task = tts_job["task"]
        if task is not None and not task.done():
            return JSONResponse({"state": "downloading"}, status_code=202)
        repo = config.read()["values"]["speech"]["tts_model"]
        spec = voice_status_module._chosen(voice_status_module.TTS, repo)["repos"][0]

        async def run() -> None:
            runtime.bus.publish("voice.progress", {"target": "tts", "state": "downloading", "repo": repo})
            try:
                await asyncio.to_thread(voice_download.download_tts, spec[0], spec[1], spec[2])
                tts_job["error"] = None
                runtime.bus.publish("voice.progress", {"target": "tts", "state": "ready", "repo": repo})
            except Exception as exc:  # noqa: BLE001
                tts_job["error"] = f"{type(exc).__name__}: {exc}"[:300]
                runtime.bus.publish("voice.progress", {"target": "tts", "state": "error", "repo": repo, "error": tts_job["error"]})

        tts_job["task"] = asyncio.create_task(run())
        return JSONResponse({"state": "downloading"}, status_code=202)

    async def voice_remove(request: Request) -> Response:
        from .voice import download as voice_download
        from .voice import status as voice_status_module
        target = p(request, "target")
        if target not in ("stt", "tts"):
            raise NotFound(f"no voice model {target}")
        task = tts_job["task"]
        if voice.downloading or (target == "tts" and task is not None and not task.done()):
            raise Busy("a download is running")
        if target == "tts":
            if speech.busy:
                raise Busy("speech is playing")
            speech.unload()
            repo = config.read()["values"]["speech"]["tts_model"]
        else:
            await voice.unload()
            repo = config.read()["values"]["voice"]["stt_model"]
        freed = voice_download.remove(repo)
        return JSONResponse({"repo": repo, "freed": freed, **voice_status_module.status()[target]})

    async def search_service_status(request: Request) -> Response:
        from . import searchservice
        return JSONResponse(await asyncio.to_thread(searchservice.status))

    async def search_service_start(request: Request) -> Response:
        from . import searchservice
        try:
            return JSONResponse(await asyncio.to_thread(searchservice.start))
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc), "code": "search_start_failed"}, status_code=422)

    async def mcp_list(request: Request) -> Response:
        return JSONResponse({"servers": runtime.mcp.status()})

    async def mcp_one(request: Request) -> Response:
        name = p(request, "name")
        row = next((r for r in runtime.mcp.status() if r["server"] == name), None)
        if row is None:
            raise NotFound(f"no MCP server {name}")
        conn = runtime.mcp.connections.get(name)
        return JSONResponse(row | {"tool_names": [t.name for t in conn.tools] if conn else [],
                                   "log": str(mcp_manager.log_path(name))})

    async def mcp_restart(request: Request) -> Response:
        try:
            return JSONResponse(await runtime.mcp.restart(p(request, "name")))
        except mcp_module.McpUnavailable as exc:
            raise NotFound(str(exc)) from exc

    async def mcp_extra(manager: Any, request: Request, name: str, what: str) -> Response:
        """Read-only MCP resources and prompts of one server. These connect it; nothing reaches a session."""
        try:
            if what == "resources":
                return JSONResponse({"server": name, "resources": await manager.resources(name)})
            if what == "prompts":
                return JSONResponse({"server": name, "prompts": await manager.prompts(name)})
            if what == "resource":
                uri = request.query_params.get("uri", "")
                if not uri or len(uri) > 2000:
                    raise ValueError("uri is required (at most 2000 characters)")
                return JSONResponse({"server": name, "uri": uri, "text": await manager.read_resource(name, uri)})
            prompt = request.query_params.get("name", "")
            raw = request.query_params.get("arguments", "")
            arguments = json.loads(raw) if raw else {}
            if not prompt or len(prompt) > 200:
                raise ValueError("name is required (at most 200 characters)")
            if not isinstance(arguments, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                          for k, v in arguments.items()) or len(arguments) > 50:
                raise ValueError("arguments must be a JSON object of strings (at most 50)")
            return JSONResponse({"server": name, "prompt": prompt,
                                 "text": await manager.get_prompt(name, prompt, arguments)})
        except mcp_module.McpUnavailable as exc:
            raise NotFound(str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise ValueError("arguments must be JSON") from exc

    def mcp_extra_route(what: str) -> Any:
        async def handler(request: Request) -> Response:
            return await mcp_extra(runtime.mcp, request, p(request, "name"), what)
        return handler

    def project_mcp_extra_route(what: str) -> Any:
        async def handler(request: Request) -> Response:
            scope = project_mcp_scope(runtime.project(p(request, "id")))
            return await mcp_extra(scope.manager, request, p(request, "name"), what)
        return handler

    def project_mcp_scope(project: Any) -> Any:
        """The project's MCP scope, with its project-scoped event sink attached."""
        scope = runtime.project_mcp(project)
        return scope

    async def project_mcp_list(request: Request) -> Response:
        project = runtime.project(p(request, "id"))
        project_mcp_scope(project)
        return JSONResponse(runtime.project_mcp_summary(project))

    async def project_mcp_one(request: Request) -> Response:
        project, name = runtime.project(p(request, "id")), p(request, "name")
        scope = project_mcp_scope(project)
        row = scope.row(name)
        if row is None:
            raise NotFound(f"no MCP server {name!r} in this project")
        conn = scope.manager.connections.get(name)
        return JSONResponse(row | {"tool_names": [t.name for t in conn.tools] if conn else [],
                                   "log": str(scope.log_path(name))})

    async def project_mcp_restart(request: Request) -> Response:
        project, name = runtime.project(p(request, "id")), p(request, "name")
        project_mcp_scope(project)
        try:
            return JSONResponse(await runtime.restart_project_mcp(project, name))
        except mcp_module.McpUnavailable as exc:
            raise NotFound(str(exc)) from exc

    async def agent_one(request: Request) -> Response:
        return JSONResponse(agent_json(runtime.agents(agent_project_path(request)).get(p(request, "name")), prompt=True))

    async def projects(request: Request) -> Response:
        if request.method == "POST":
            body = await _body(request)
            return JSONResponse(project_json(runtime.open_project(body["path"], body.get("name"))))
        return JSONResponse([project_json(x) for x in runtime.store.projects()])

    async def project(request: Request) -> Response:
        pid = p(request, "id")
        if request.method == "PATCH":
            body = await _body(request)
            name = body.get("name")
            status = body.get("status")
            if name is None and status is None:
                raise ValueError("PATCH needs a name or a status")
            return JSONResponse(project_json(runtime.update_project(
                pid, name=name, status=status, expected_revision=_expected_revision(request, body))))
        if request.method == "DELETE":
            await runtime.delete_project(pid)
            return Response(status_code=204)
        return JSONResponse(project_json(runtime.project(pid)))

    async def project_remove(request: Request) -> Response:
        sessions = runtime.remove_project(p(request, "id"))
        return JSONResponse({"id": p(request, "id"), "removed": True, "sessions_kept": len(sessions)})

    async def project_move(request: Request) -> Response:
        body = await _body(request)
        has_before, has_after = "before" in body, "after" in body
        if has_before == has_after:
            raise ValueError("give exactly one of before or after (null means an end)")
        before = body.get("before")
        after = body.get("after")
        for value in (before, after):
            if value is not None and not isinstance(value, str):
                raise ValueError("before and after must be project ids or null")
        projects = runtime.move_project(p(request, "id"), before=before, after=after,
                                        use_before=has_before)
        return JSONResponse({"projects": [project_json(x) for x in projects]})

    async def project_git(request: Request) -> Response:
        from . import git_ops

        project = runtime.project(p(request, "id"))
        target = request.query_params.get("worktree") or str(await git_ops.default_worktree(project.path))
        return JSONResponse(await git_ops.status(project.path, target))

    async def project_git_diff(request: Request) -> Response:
        from . import git_ops

        project = runtime.project(p(request, "id"))
        target = request.query_params.get("worktree") or str(await git_ops.default_worktree(project.path))
        return JSONResponse(await git_ops.diff(project.path, target, request.query_params.get("path", "")))

    async def project_git_live(request: Request) -> Response:
        from . import git_ops

        project = runtime.project(p(request, "id"))
        target = request.query_params.get("worktree") or str(await git_ops.default_worktree(project.path))
        async with runtime.worktree_lock:
            root = await git_ops.registered(project.path, target)
            return JSONResponse(runtime.git_live_sessions(root))

    async def project_git_action(request: Request) -> Response:
        from . import git_ops

        if _idempotency_key(request) is None:
            raise ValueError("Idempotency-Key is required for Git actions")
        body = await _body(request)
        project = runtime.project(p(request, "id"))
        target = body.get("worktree")
        if target is None:
            target = str(await git_ops.default_worktree(project.path))
        if not isinstance(target, str):
            raise TypeError("worktree must be a path")
        action = body.get("action")
        if not isinstance(action, str):
            raise TypeError("action must be a string")
        async with runtime.worktree_lock:
            root = await git_ops.registered(project.path, target)
            if action in ("stage", "unstage"):
                result = await git_ops.index(project.path, root, action, body.get("paths"))
            else:
                confirmed = body.get("confirmed_live")
                if not isinstance(confirmed, list) or not all(isinstance(sid, str) for sid in confirmed):
                    raise ValueError("confirmed_live must be an array of session IDs")
                live = runtime.git_live_sessions(root)
                if {item["id"] for item in live["working"]} - set(confirmed):
                    raise Conflict("worktree has newly working sessions", code="worktree_live", payload=live)
                if action == "discard":
                    result = await git_ops.discard(project.path, root, body.get("paths"))
                else:
                    if action == "branch_delete_force" and body.get("force_confirmed") is not True:
                        raise ValueError("force_confirmed must be true for force deletion")
                    result = await git_ops.action(project.path, root, action, value=body.get("value", ""))
            runtime.bus.publish("git.changed", {"project_id": project.id, "worktree": str(root), "workspace": str(root), "source": "git", "action": action})
            if action in ("switch", "pull", "discard"):
                runtime.bus.publish("files.changed", {"project_id": project.id, "worktree": str(root), "workspace": str(root), "source": "git", "all": True})
            return JSONResponse(result)

    async def project_worktree_review(request: Request) -> Response:
        project = runtime.project(p(request, "id"))
        worktree = request.query_params.get("worktree")
        if not worktree:
            raise ValueError("worktree is required")
        return JSONResponse(await runtime.review_worktree(project.id, worktree, request.query_params.get("base") or None))

    async def project_worktree_integrate(request: Request) -> Response:
        if _idempotency_key(request) is None:
            raise ValueError("Idempotency-Key is required to integrate a worktree")
        body = await _body(request)
        project = runtime.project(p(request, "id"))
        for key in ("worktree", "expected_head", "mode"):
            if not isinstance(body.get(key), str):
                raise TypeError(f"{key} must be a string")
        for key in ("base", "message"):
            if body.get(key) is not None and not isinstance(body[key], str):
                raise TypeError(f"{key} must be a string")
        confirmed = body.get("confirmed_live", [])
        if not isinstance(confirmed, list) or not all(isinstance(sid, str) for sid in confirmed):
            raise ValueError("confirmed_live must be an array of session IDs")
        if body.get("remove", False) not in (True, False):
            raise TypeError("remove must be true or false")
        return JSONResponse(await runtime.integrate_worktree(
            project.id, body["worktree"], expected_head=body["expected_head"], mode=body["mode"],
            base=body.get("base"), message=body.get("message"), remove=bool(body.get("remove", False)),
            confirmed_live=confirmed))

    async def project_worktrees(request: Request) -> Response:
        return JSONResponse(await runtime.list_project_worktrees(p(request, "id")))

    def file_root(request: Request) -> Path:
        project = runtime.project(p(request, "id"))
        session_id = request.query_params.get("session")
        if session_id:
            session = runtime.session(session_id)
            if session.project_id != project.id:
                raise ValueError("session does not belong to project")
            return Path(session.directory)
        return Path(project.path)

    async def project_files(request: Request) -> Response:
        from nexus import files
        if request.method == "POST":
            body = await _body(request)
            action, path, destination = body.get("action"), body.get("path"), body.get("to")
            if not isinstance(action, str) or not isinstance(path, str):
                raise ValueError("action and path are required")
            async with runtime.file_lock:
                done = await asyncio.to_thread(files.mutate_path, file_root(request), action, path, destination)
            runtime.bus.publish("files.changed", {"project_id": p(request, "id"), "workspace": str(file_root(request)), **done, "source": "user"})
            return JSONResponse(done)
        if request.query_params.get("count") == "true":
            count = await asyncio.to_thread(files.count_entries, file_root(request), request.query_params.get("path", ""))
            return JSONResponse({"count": count})
        try:
            return JSONResponse(await files.list_directory(file_root(request), request.query_params.get("path", "")))
        except ValueError as exc:
            raise (_missing(exc) or exc) from exc

    async def project_watch(request: Request) -> Response:
        body = await _body(request)
        action = body.get("action", "renew")
        if action not in ("renew", "release"):
            raise ValueError("watch action must be renew or release")
        return JSONResponse(file_watches.renew(p(request, "id"), file_root(request), body.get("client_id"),
                                               release=action == "release"))

    async def project_file(request: Request) -> Response:
        from nexus import files
        if request.method == "PUT":
            return await project_file_write(request)
        try:
            return JSONResponse(files.read_file(file_root(request), request.query_params.get("path", "")))
        except ValueError as exc:
            raise (_missing(exc) or exc) from exc

    async def project_file_write(request: Request) -> Response:
        """K10: the built-in editor's save. `expected_revision` is the `revision` a read returned, or "new"."""
        from nexus import files
        body = await _body(request)
        path, content, expected = request.query_params.get("path", ""), body.get("content"), body.get("expected_revision")
        if not isinstance(content, str) or not isinstance(expected, str) or not expected:
            raise ValueError("content and expected_revision (a read's revision, or \"new\") are required")
        project = runtime.project(p(request, "id"))
        root = file_root(request)
        try:
            async with runtime.file_lock:
                done = await asyncio.to_thread(files.write_file, root, path, content, expected)
        except files.StaleFile as exc:
            raise Conflict(str(exc), code="stale_file", payload={"path": path, "current_revision": exc.revision}) from exc
        except ValueError as exc:
            raise (_missing(exc) or exc) from exc
        runtime.bus.publish("files.changed", {"project_id": project.id, "path": path, "revision": done["revision"],
                                              "source": "editor", "workspace": str(root)})
        return JSONResponse(done)

    async def project_file_search(request: Request) -> Response:
        from nexus import files
        query = request.query_params.get("q", "")
        try:
            limit = int(request.query_params.get("limit", "30"))
        except ValueError:
            raise ValueError("limit must be a non-negative integer") from None
        directories = request.query_params.get("directories", "") in ("1", "true")
        paths = await files.search_files(
            file_root(request), query, limit, include_directories=directories)
        return JSONResponse({"query": query, "paths": paths})

    async def project_prompts(request: Request) -> Response:
        project = runtime.project(p(request, "id"))
        try:
            limit = max(1, min(200, int(request.query_params.get("limit", "50"))))
            before = int(request.query_params["before"]) if request.query_params.get("before") else None
        except ValueError:
            raise ValueError("limit and before must be integers") from None
        prompts = runtime.store.prompt_history(project.id, q=request.query_params.get("q", ""), limit=limit, before=before)
        return JSONResponse({"prompts": prompts})

    async def search(request: Request) -> Response:
        try:
            limit = max(1, min(100, int(request.query_params.get("limit", "30"))))
        except ValueError:
            raise ValueError("limit must be an integer") from None
        project_id = request.query_params.get("project")
        if project_id:
            runtime.project(project_id)
        hits = runtime.store.search(request.query_params.get("q", ""), project_id=project_id,
                                    archived=_archive_filter(request), limit=limit)
        out = []
        for hit in hits:
            session = runtime.store.session(hit["session_id"])
            if session is not None:
                out.append({**hit, "session": runtime.session_json(session)})
        return JSONResponse({"query": request.query_params.get("q", ""), "hits": out})

    async def artifact_list(request: Request) -> Response:
        runtime.session(p(request, "id"))
        return JSONResponse([{k: a[k] for k in ("id", "mime", "name", "bytes", "source", "created")}
                             for a in runtime.store.artifacts(p(request, "id"))])

    async def artifact_one(request: Request) -> Response:
        from nexus import artifacts
        record = runtime.store.artifact(p(request, "id"))
        if record is None:
            raise NotFound(f"no artifact {p(request, 'id')}")
        try:
            raw = artifacts.read_blob(record["sha"])
        except OSError:
            raise NotFound("the artifact's bytes are gone") from None
        # Served as an attachment with a fixed type and no sniffing: a server's SVG or HTML never runs on this origin.
        return Response(raw, media_type=record["mime"], headers={
            "content-disposition": "attachment", "x-content-type-options": "nosniff",
            "content-security-policy": "sandbox", "cache-control": "private, max-age=31536000, immutable"})

    async def session_git(request: Request) -> Response:
        from nexus import files
        session = runtime.session(p(request, "id"))
        return JSONResponse(await files.git_status(Path(session.directory)))

    async def session_git_diff(request: Request) -> Response:
        from nexus import files
        session = runtime.session(p(request, "id"))
        return JSONResponse(await files.git_diff(Path(session.directory), request.query_params.get("path", "")))

    async def sessions(request: Request) -> Response:
        if request.method == "POST":
            body = await _body(request)
            parent = runtime.session(body["parent"]) if body.get("parent") else None
            if parent:
                target = runtime.project(parent.project_id)
            else:
                target = runtime.project(body["project"]) if body.get("project") else runtime.open_project(body["directory"])
            session = await runtime.create_session_ready(
                target, title=body.get("title"), model=body.get("model"), permission=body.get("permission"),
                effort=body.get("effort"), agent=body.get("agent"), parent=parent, disabled_tools=body.get("disabled_tools"))
            if parent is None and body.get("model"):
                runtime.remember_model(session.project_id, session.agent, body["model"])
            if parent is None and isinstance(body.get("effort"), str):
                runtime.store.set_preference("effort.last", body["effort"])
            return JSONResponse(runtime.session_json(session))
        archived = _archive_filter(request)
        if request.query_params.get("parent"):
            runtime.session(request.query_params["parent"])
            return JSONResponse([runtime.session_json(s) for s in runtime.store.children(
                request.query_params["parent"], archived=archived)])
        project_id = request.query_params.get("project")
        if request.query_params.get("directory"):
            project_id = runtime.open_project(request.query_params["directory"]).id
        return JSONResponse([runtime.session_json(s) for s in runtime.store.sessions(project_id, archived=archived)])

    async def session(request: Request) -> Response:
        sid = p(request, "id")
        if request.method == "PATCH":
            body = await _body(request)
            fields = {k: body.get(k) for k in ("agent", "title", "model", "permission", "effort", "disabled_tools")}
            expected = _expected_revision(request, body)
            if expected is not None:
                fields["expected_revision"] = expected
            session = runtime.update_session(sid, **fields)
            if body.get("model"):
                runtime.remember_model(session.project_id, session.agent, body["model"])
            if not session.parent_id and isinstance(body.get("effort"), str):
                runtime.store.set_preference("effort.last", body["effort"])
            return JSONResponse(runtime.session_json(session))
        if request.method == "DELETE":
            body = await _body(request)
            await runtime.delete_session(sid, drop_inputs=body.get("drop_inputs") is True)
            return Response(status_code=204)
        return JSONResponse(runtime.session_json(runtime.session(sid)))

    async def diagnostics(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)
        if request.method == "POST":
            body = await request.json()
            if not isinstance(body.get("keep"), bool):
                raise ValueError("keep must be a boolean")
            runtime.store.keep_diagnostics(sid, body["keep"])
        row = runtime.store.db.execute("select keep from session_diagnostics where session_id = ?", (sid,)).fetchone()
        return JSONResponse({"keep": bool(row and row[0]), "compaction": runtime.store.compaction(sid),
                             "waits": runtime.store.wait_lifecycle(sid)})

    async def stored_events(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)
        limit = min(int(request.query_params.get("limit") or 1000), 5000)
        types = [t for t in (request.query_params.get("type") or "").split(",") if t]
        rows = runtime.store.events(sid, after=int(request.query_params.get("after") or 0), limit=limit, types=types or None)
        return JSONResponse({"events": rows, "counts": runtime.store.event_counts(sid), "more": len(rows) == limit,
                             "floor": runtime.store.log_floor(), "head": runtime.store.log_head(),
                             "generation": runtime.store.log_generation(),
                             "history_truncated": runtime.store.log_floor() > 0,
                             "compaction": runtime.store.compaction(sid)})

    def _inspector_target(request: Request) -> tuple[str, str | None]:
        sid = p(request, "id")
        runtime.session(sid)
        session = request.query_params.get("session") or None
        if session is not None:
            runtime.session(session)
        return sid, session

    async def native_inspector(request: Request) -> Response:
        from . import inspector_data as insp

        sid, session = _inspector_target(request)
        query = request.query_params
        if query.get("page") not in (None, "", "0"):
            raise ValueError("page was removed in inspector v2; use before=<next_before>")
        try:
            limit = int(query.get("limit") or insp.PAGE)
        except ValueError as exc:
            raise ValueError("limit must be an integer") from exc
        data = insp.inspect(
            runtime.store, sid, view=query.get("view", "Timing"), before=query.get("before") or None, limit=limit,
            session=session, q=query.get("q"), kind=query.get("kind"), status=query.get("status"),
            types=[t for t in (query.get("type") or "").split(",") if t])
        if not data["before"] and data["session"] == sid and data["view"] in ("Requests", "Raw"):
            if data["view"] == "Requests":
                parts = [("Snapshot", json.dumps(runtime.snapshot_json(sid), indent=2)),
                         ("Next request", json.dumps(await runtime.context_usage(sid), indent=2))]
                label = "Retained requests"
            else:
                parts = [("Session record", json.dumps(runtime.session_json(runtime.session(sid)), indent=2)),
                         ("Retained event counts", json.dumps(runtime.store.event_counts(sid), indent=2))]
                label = "Retained events"
            head = ""
            for name, body in parts:
                body, lost = insp.clip(body)
                head += f"{name}\n{body}\n\n"
                if lost:
                    data["omitted"].append({"kind": "section", "bytes": lost, "reason": f"{name} clipped to {insp.EXTRA_MAX} bytes"})
            data["text"] = head + label + "\n" + data["text"]
        return JSONResponse(data, headers={"cache-control": "no-store"})

    async def native_inspector_head(request: Request) -> Response:
        from . import inspector_data as insp

        sid, session = _inspector_target(request)
        return JSONResponse(insp.head(runtime.store, sid, session), headers={"cache-control": "no-store"})

    async def native_inspector_record(request: Request) -> Response:
        from . import inspector_data as insp

        sid = p(request, "id")
        runtime.session(sid)
        kind, ident = request.query_params.get("kind"), request.query_params.get("id")
        if not kind or not ident:
            raise ValueError("kind and id are required")
        found = insp.record(runtime.store, sid, kind, ident)
        if found is None:
            raise NotFound(f"no {kind} record {ident} in this session tree")
        return JSONResponse(found, headers={"cache-control": "no-store"})

    async def turns(request: Request) -> Response:
        """Turn summaries, oldest first: the newest page, or one page after/before a turn."""
        sid = p(request, "id")
        runtime.session(sid)
        query = request.query_params
        return JSONResponse(runtime.turn_summaries(
            sid, after=query.get("after") or None, before=query.get("before") or None,
            limit=min(int(query.get("limit") or TURN_PAGE), TURN_PAGE_MAX)))

    async def snapshot(request: Request) -> Response:
        """A consistent view to rebuild client state, plus an event watermark.

        Apply events strictly after `watermark.head`. A selected session gets its
        last `message_limit` messages (default 200, at most 1,000); older history
        is available through the message endpoint.
        """
        sid = request.query_params.get("session") or None
        if sid is not None:
            runtime.session(sid)
        limit = max(1, min(int(request.query_params.get("message_limit") or 200), 1000))
        data = runtime.store.state_snapshot(session_id=sid, message_limit=limit)
        # An explicit debug selection must survive reconnect snapshots. Ordinary
        # discovery still excludes every Manager session and its hidden project.
        if sid is not None and runtime.session(sid).kind == "manager":
            data["sessions"].append(runtime.manager.session_json(runtime.session(sid)))
        data["running"] = runtime.store.running_turns()
        data["approvals"] = [request_ for sid_, _, request_ in runtime.approvals.values()]
        data["questions"] = runtime.pending_questions()
        data["inputs"] = runtime.inputs.list()
        data["held_sessions"] = [r["session_id"] for r in runtime.store.db.execute("select session_id from input_hold")]
        return JSONResponse(data, headers={"x-nexus-log-generation": data["watermark"]["generation"],
                                           "cache-control": "no-store"})

    async def children(request: Request) -> Response:
        runtime.session(p(request, "id"))
        return JSONResponse([runtime.session_json(s) for s in runtime.store.children(p(request, "id"))])

    async def tree(request: Request) -> Response:
        return JSONResponse(runtime.tree(p(request, "id")))

    async def session_model(request: Request) -> Response:
        body = await _body(request)
        fields: dict[str, Any] = {"model": body["model"]}
        expected = _expected_revision(request, body)
        if expected is not None:
            fields["expected_revision"] = expected
        session = runtime.update_session(p(request, "id"), **fields)
        if body["model"]:
            runtime.remember_model(session.project_id, session.agent, body["model"])
        return JSONResponse(runtime.session_json(session))

    async def session_switch_cost(request: Request) -> Response:
        query = request.query_params
        return JSONResponse(runtime.switch_cost(p(request, "id"), model=query.get("model"), agent=query.get("agent")))

    async def session_agent(request: Request) -> Response:
        body = await _body(request)
        fields: dict[str, Any] = {"agent": body["agent"]}
        expected = _expected_revision(request, body)
        if expected is not None:
            fields["expected_revision"] = expected
        return JSONResponse(runtime.session_json(runtime.update_session(p(request, "id"), **fields)))

    async def session_workspace_tools(request: Request) -> Response:
        sid = p(request, "id")
        runtime.activate_workspace_tools(sid, source="api")
        return JSONResponse(runtime.session_json(runtime.session(sid)))

    async def session_archive(request: Request) -> Response:
        sid = p(request, "id")
        body = await _body(request)
        return JSONResponse(runtime.session_json(runtime.archive_session(sid, archived=True, drop_inputs=body.get("drop_inputs") is True)))

    async def session_unarchive(request: Request) -> Response:
        sid = p(request, "id")
        return JSONResponse(runtime.session_json(runtime.archive_session(sid, archived=False)))

    async def session_directory(request: Request) -> Response:
        sid = p(request, "id")
        body = await _body(request)
        result = await runtime.change_directory(sid, body.get("directory"), source="api")
        payload = runtime.session_json(runtime.session(sid)) | {"change": result}
        return JSONResponse(payload, status_code=202 if result["status"] == "pending" else 200)

    async def session_worktrees(request: Request) -> Response:
        sid = p(request, "id")
        if request.method == "GET":
            return JSONResponse(await runtime.list_worktrees(sid))
        body = await _body(request)
        action = body.get("action")
        if action == "list":
            return JSONResponse(await runtime.list_worktrees(sid))
        if action == "create":
            return JSONResponse(await runtime.create_worktree(
                sid, body.get("path"), body.get("branch"), body.get("base")))
        if action == "delete":
            return JSONResponse(await runtime.delete_worktree(sid, body.get("path")))
        raise ValueError("action must be 'list', 'create' or 'delete'")

    async def prompt(request: Request) -> Response:
        body = await _body(request)
        mutation = getattr(request.state, "mutation", None)
        if runtime.session(p(request, "id")).kind == "manager":
            raise Conflict("The Manager debug session is read-only; type to the Manager in its dropdown.", code="manager_session_read_only")
        result = runtime.prompt(p(request, "id"), str(body.get("text", "")), images=body.get("images"), pastes=body.get("pastes"), mutation=mutation, mode=body.get("mode", "send"), typed=True)
        if mutation is not None:
            result = {**result, "idempotency_key": mutation["key"]}
        return JSONResponse(result, status_code=202)

    async def shell(request: Request) -> Response:
        body = await _body(request)
        command = body.get("command", "")
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        return JSONResponse(await runtime.shell(p(request, "id"), command))

    async def session_inputs(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)
        return JSONResponse({"inputs": runtime.inputs.list(sid), "held": runtime.inputs.held(sid)})

    async def session_input(request: Request) -> Response:
        sid, iid = p(request, "id"), p(request, "iid")
        runtime.session(sid)
        if request.method == "DELETE":
            held = runtime.inputs.held(sid)
            item = runtime.inputs.remove(sid, iid)
            runtime.bus.publish("input.removed", item, session_id=sid)
            if held and not runtime.inputs.held(sid):
                runtime.bus.publish("input.held", {"session_id": sid, "held": False}, session_id=sid)
            return JSONResponse(item)
        body = await _body(request)
        item = runtime.inputs.get(sid, iid)
        parts = None
        if "text" in body:
            text = body["text"]
            if not isinstance(text, str):
                raise TypeError("text must be a string")
            parts = [part for part in item["parts"] if part.get("type") != "text" or str(part.get("text", "")).startswith("[Image ")]
            if text:
                parts.insert(0, {"type": "text", "text": text})
            if not parts:
                raise ValueError("input cannot be empty")
        item = runtime.inputs.update(sid, iid, mode=body.get("mode"), parts=parts)
        runtime.bus.publish("input.updated", item, session_id=sid)
        return JSONResponse(item)

    async def move_input(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)
        body = await _body(request)
        if not isinstance(body.get("before"), str):
            raise TypeError("before must be an input id")
        item = runtime.inputs.move(sid, p(request, "iid"), body["before"])
        runtime.bus.publish("input.updated", item, session_id=sid)
        return JSONResponse({"input": item, "inputs": runtime.inputs.list(sid)})

    async def next_input(request: Request) -> Response:
        return JSONResponse(runtime.release_input(p(request, "id")), status_code=202)

    async def session_continue(request: Request) -> Response:
        mutation = getattr(request.state, "mutation", None)
        result = runtime.continue_session(p(request, "id"), mutation=mutation)
        if mutation is not None:
            result = {**result, "idempotency_key": mutation["key"]}
        return JSONResponse(result, status_code=202)

    async def interrupt(request: Request) -> Response:
        runtime.session(p(request, "id"))
        await runtime.interrupt(p(request, "id"))
        return Response(status_code=204)

    async def messages(request: Request) -> Response:
        runtime.session(p(request, "id"))
        if "limit" in request.query_params or "before" in request.query_params:  # a page: K6
            try:
                limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
                before = int(request.query_params["before"]) if request.query_params.get("before") else None
            except ValueError:
                raise ValueError("limit and before must be integers") from None
            return JSONResponse(runtime.store.messages_page(p(request, "id"), before=before, limit=limit))
        return JSONResponse([message_json(m) for m in runtime.store.messages(p(request, "id"))])

    async def context(request: Request) -> Response:
        return JSONResponse(await runtime.context_usage(p(request, "id")))

    async def draft_context(request: Request) -> Response:
        body = await _body(request)
        target = runtime.project(body["project"]) if body.get("project") else runtime.open_project(body["directory"])
        return JSONResponse(await runtime.draft_context(
            target, model=body.get("model"), effort=body.get("effort"), agent=body.get("agent"),
            disabled_tools=body.get("disabled_tools")))

    async def btw(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse(runtime.btw_state(p(request, "id")))
        body = await _body(request)
        question = body.get("question", "")
        if not isinstance(question, str):
            raise TypeError("question must be a string")
        return JSONResponse(await runtime.btw(p(request, "id"), question, request_id=body.get("request_id")))

    async def handoff(request: Request) -> Response:
        return JSONResponse(await runtime.handoff(p(request, "id")), status_code=201)

    async def session_snapshot_json(request: Request) -> Response:
        return JSONResponse(runtime.snapshot_json(p(request, "id")))

    async def session_export(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)  # 404 before opening the read-only view
        format_, scope = export.check_options(
            request.query_params.get("format"), request.query_params.get("scope"))
        try:
            view = export.ExportView(runtime.store.path, sid, scope, running=set(runtime.turns))
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        chunks = export.stream(view, format_)
        name = export.filename(sid, scope, format_, view.boundary.generated)
        return ExportResponse(
            _stream_export(chunks), view=view,
            media_type=export.MEDIA_TYPES[format_],
            headers={
                "content-disposition": f'attachment; filename="{name}"',
                "cache-control": "no-store",
                "x-nexus-export-format-version": str(export.FORMAT_VERSION),
                "x-nexus-export-boundary": f"event:{view.boundary.event_id};seq:{view.boundary.seq}",
                "x-nexus-log-generation": view.boundary.generation,
                "x-nexus-log-floor": str(view.boundary.floor),
            },
        )

    async def permissions(request: Request) -> Response:
        runtime.session(p(request, "id"))
        return JSONResponse(runtime.pending_approvals(p(request, "id")))

    async def permission_reply(request: Request) -> Response:
        body = await _body(request)
        reply = body.get("reply")
        if reply not in ("allow", "deny"):
            raise ValueError("reply must be 'allow' or 'deny'")
        scope, note = body.get("scope", "once"), body.get("note", "")
        if not isinstance(scope, str) or not isinstance(note, str):
            raise TypeError("scope and note must be strings")
        runtime.reply(p(request, "id"), p(request, "pid"), reply == "allow", scope=scope, note=note)
        return Response(status_code=204)

    async def grants(request: Request) -> Response:
        runtime.session(p(request, "id"))
        if request.method == "DELETE":
            runtime.clear_grants(p(request, "id"))
            return Response(status_code=204)
        return JSONResponse({"grants": runtime.session_grants(p(request, "id"))})

    async def questions(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)
        return JSONResponse(runtime.pending_questions(sid))

    async def all_questions(request: Request) -> Response:
        return JSONResponse(runtime.pending_questions())

    async def question_reply(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)
        body = await _body(request)
        runtime.reply_question(sid, p(request, "qid"), body.get("answers"))
        return Response(status_code=204)

    async def kernels(request: Request) -> Response:
        summary = request.query_params.get("summary", "").lower() in ("1", "true", "yes")
        return JSONResponse([k.json(summary=summary) for k in runtime.kernels.items.values()])

    async def bash_jobs(request: Request) -> Response:
        return JSONResponse([job.json() for job in runtime.jobs.list()])

    async def bash_job(request: Request) -> Response:
        jid = p(request, "id")
        if jid not in runtime.jobs.items:
            raise NotFound(f"no bash job {jid}")
        if request.method == "DELETE":
            await runtime.jobs.kill(jid, root=None)
            return Response(status_code=204)
        return JSONResponse(runtime.jobs.get(jid).json(output=True))

    async def kernel(request: Request) -> Response:
        kid = p(request, "id")
        if kid not in runtime.kernels.items:
            raise NotFound(f"no kernel {kid}")
        if request.method == "DELETE":
            try:
                await runtime.kernels.delete(kid)
            except ToolError as exc:
                raise Busy(str(exc)) from exc
            runtime.bus.publish("kernel.deleted", {"id": kid})
            return Response(status_code=204)
        return JSONResponse(runtime.kernels.get(kid).json())

    async def client_view(request: Request) -> Response:
        body = await _body(request)
        stream_id = _view_text(body.get("stream_id"), "stream_id", required=True)
        client_id = _view_text(body.get("client_id"), "client_id", required=True)
        project_id = _view_text(body.get("project"), "project")
        session_id = _view_text(body.get("session"), "session")
        panel = _view_text(body.get("panel"), "panel")
        if project_id is not None:
            project = runtime.project(project_id)
        else:
            project = None
        if session_id is not None:
            session = runtime.session(session_id)
            if project is not None and session.project_id != project.id:
                raise ValueError("session does not belong to project")
            project_id = session.project_id
        view = client_views.report(
            stream_id, client_id, project=project_id, session=session_id, panel=panel)
        # Do not echo the capability that authorized this report.
        view.pop("stream_id", None)
        return JSONResponse(view, headers={"cache-control": "no-store"})

    async def manager_status(request: Request) -> Response:
        return JSONResponse(runtime.manager.status(), headers={"cache-control": "no-store"})

    async def manager_episodes(request: Request) -> Response:
        return JSONResponse([{"id": s.id, "created": s.created, "updated": s.updated, "continued_from": s.continued_from,
                              "model": s.model} for s in runtime.manager.episodes()])

    async def manager_context(request: Request) -> Response:
        return JSONResponse(runtime.manager.context_view(), headers={"cache-control": "no-store"})

    async def manager_audit(request: Request) -> Response:
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
        except ValueError:
            raise ValueError("limit must be an integer") from None
        return JSONResponse(runtime.store.audit(limit=limit, tool=request.query_params.get("tool") or None))

    async def manager_session(request: Request) -> Response:
        if request.method == "POST":
            return JSONResponse(await runtime.manager.debug_session(), headers={"cache-control": "no-store"})
        session = runtime.manager.current_episode()
        return JSONResponse(runtime.manager.session_json(session) if session else None, headers={"cache-control": "no-store"})

    async def manager_live_stop(request: Request) -> Response:
        body = await _body(request)
        await runtime.manager.live_stop(_view_text(body.get("stream_id"), "stream_id", required=True) or "")
        return Response(status_code=204)

    async def manager_start(request: Request) -> Response:
        body = await _body(request)
        stream_id = _view_text(body.get("stream_id"), "stream_id", required=True)
        client_id = _view_text(body.get("client_id"), "client_id", required=True)
        audio = body.get("audio", True)
        if not isinstance(audio, bool):
            raise TypeError("audio must be a boolean")
        return JSONResponse(await runtime.manager.start(stream_id or "", client_id or "", audio=audio),
                            headers={"cache-control": "no-store"})

    async def manager_stop(request: Request) -> Response:
        body = await _body(request)
        await runtime.manager.stop(_view_text(body.get("stream_id"), "stream_id", required=True) or "")
        return Response(status_code=204)

    async def manager_say(request: Request) -> Response:
        body = await _body(request)
        result = runtime.manager.say(_view_text(body.get("stream_id"), "stream_id", required=True) or "",
                                     body.get("text"), body.get("source"))
        return JSONResponse(result, status_code=202)

    async def manager_cancel(request: Request) -> Response:
        body = await _body(request)
        result = await runtime.manager.cancel(_view_text(body.get("stream_id"), "stream_id", required=True) or "")
        result["speech_stopped"] = speech.stop()  # barge-in: the sentence being synthesised stops too
        return JSONResponse(result)

    async def manager_consent(request: Request) -> Response:
        body = await _body(request)
        accept = body.get("accept")
        if not isinstance(accept, bool):
            raise TypeError("accept must be a boolean")
        shown = body.get("version")
        if accept and shown != NOTICE_VERSION:
            raise ValueError(f"version must be the notice version being acknowledged ({NOTICE_VERSION})")
        provider = body.get("provider")
        if provider is not None and provider != "codex":
            raise ValueError("provider must be omitted (the Manager's model) or \"codex\" (GPT-Live)")
        ref = LIVE_CONSENT_REF if provider == "codex" else runtime.manager.settings()["model"]
        return JSONResponse(runtime.manager.consent.set(ref, accept))

    async def manager_live_start(request: Request) -> Response:
        body = await _body(request)
        return JSONResponse(await runtime.manager.live_start(
            _view_text(body.get("stream_id"), "stream_id", required=True) or ""),
            headers={"cache-control": "no-store"})

    async def manager_live_audio(websocket: WebSocket) -> None:
        """Raw call audio: binary frames of PCM16 mono 24 kHz both ways. Holder only; the token and the stream id
        are query parameters because a browser cannot set headers on a WebSocket."""
        if token and not hmac.compare_digest(websocket.query_params.get("token", ""), token):
            await websocket.close(code=4401)
            return
        try:
            voice = runtime.manager.live_voice(websocket.query_params.get("stream_id", ""))
        except Forbidden:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        await voice.attach_client(websocket)

    async def manager_activity(request: Request) -> Response:
        body = await _body(request)
        listening, speaking = body.get("listening"), body.get("speaking")
        if not isinstance(listening, bool) or not isinstance(speaking, bool):
            raise TypeError("listening and speaking must be booleans")
        runtime.manager.activity(_view_text(body.get("stream_id"), "stream_id", required=True) or "",
                                 listening, speaking)
        return Response(status_code=204)

    async def manager_card_dismiss(request: Request) -> Response:
        body = await _body(request)
        card_id = _view_text(body.get("card_id"), "card_id", required=True) or ""
        runtime.manager.dismiss_card(_view_text(body.get("stream_id"), "stream_id", required=True) or "", card_id)
        return Response(status_code=204)  # a stale card id (an older card) changes nothing

    async def manager_navigate_result(request: Request) -> Response:
        body = await _body(request)
        ok, reason = body.get("ok"), body.get("reason")
        if not isinstance(ok, bool) or not (reason is None or isinstance(reason, str)):
            raise TypeError("ok must be a boolean and reason a string")
        request_id = _view_text(body.get("request_id"), "request_id", required=True) or ""
        runtime.manager.navigate_result(
            _view_text(body.get("stream_id"), "stream_id", required=True) or "", request_id, ok,
            reason[:300] if reason else None)
        return Response(status_code=204)  # an unknown or late request id is ignored

    async def events(request: Request) -> Response:
        only = request.query_params.get("session") or None
        if only is not None:
            runtime.session(only)  # an unknown filter cannot be followed; the client must resync
        store = runtime.store
        generation = store.log_generation()
        # An explicit query cursor wins over an automatic `Last-Event-ID`, so a client can
        # always decide where to resume from a script or a stored position.
        supplied = request.query_params.get("cursor") if "cursor" in request.query_params else request.headers.get("last-event-id")
        after = parse_cursor(supplied, generation=generation, head=store.log_head(), floor=store.log_floor()) \
            if supplied is not None else None
        stream = Stream.open(store, runtime.bus, after=after, session_id=only)
        client_views.attach(stream)  # dropped by the stream's close listener on disconnect
        connected = {
            "version": VERSION, "generation": stream.generation, "head": stream.head,
            "head_cursor": format_cursor(stream.generation, stream.head),
            "resumed_from": after, "session": only, "floor": store.log_floor(),
            "stream_id": stream.stream_id,
        }

        async def body() -> AsyncIterator[str]:
            yield f"event: server.connected\ndata: {json.dumps(connected)}\n\n"
            async for event in stream.events():
                yield sse(event)

        return EventStreamResponse(
            body(), stream=stream, media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-nexus-log-generation": stream.generation,
                     "x-accel-buffering": "no"},
        )

    async def inspector(request: Request) -> Response:
        # No data in the page itself, so no token needed to load it; every API call it makes needs the token.
        return HTMLResponse(INSPECTOR.read_text(), headers={"content-security-policy": INSPECTOR_CSP, "cache-control": "no-store",
                                                            "referrer-policy": "no-referrer"})

    routes = [
        Route("/inspector", inspector),
        Route("/api/info", route(info)),
        Route("/api/config", route(config_api), methods=["GET", "PATCH"]),
        Route("/api/question", route(all_questions)),
        Route("/api/bash", route(bash_jobs)),
        Route("/api/bash/{id}", route(bash_job), methods=["GET", "DELETE"]),
        Route("/api/kernel", route(kernels)),
        Route("/api/kernel/{id}", route(kernel), methods=["GET", "DELETE"]),
        Route("/api/provider", route(providers)),
        Route("/api/provider/{id}/login", route(provider_login), methods=["GET", "POST", "DELETE"]),
        Route("/api/usage", route(provider_limits), methods=["GET", "POST"]),
        Route("/api/session/{id}/usage", route(session_usage_totals)),
        Route("/api/model", route(models)),
        Route("/api/model/last", route(last_model)),
        Route("/api/snapshot", route(snapshot)),
        Route("/api/agent", route(agent_list)),
        Route("/api/agent/{name}", route(agent_one)),
        Route("/api/extension", route(extension_list)),
        Route("/api/voice", route(voice_status)),
        Route("/api/voice/transcribe", route(voice_transcribe), methods=["POST"]),
        Route("/api/voice/stt/prepare", route(voice_stt_prepare), methods=["POST"]),
        Route("/api/voice/stt/download", route(voice_stt_download), methods=["POST"]),
        Route("/api/voice/tts/download", route(voice_tts_download), methods=["POST"]),
        Route("/api/voice/{target}", route(voice_remove), methods=["DELETE"]),
        Route("/api/voice/speak", route(voice_speak), methods=["POST"]),
        Route("/api/voice/speak/stop", route(voice_speak_stop), methods=["POST"]),
        Route("/api/search/service", route(search_service_status)),
        Route("/api/search/service/start", route(search_service_start), methods=["POST"]),
        Route("/api/mcp", route(mcp_list)),
        *(Route(f"/api/mcp/{{name}}/{what}", route(mcp_extra_route(what)))
          for what in ("resources", "resource", "prompts", "prompt")),
        *(Route(f"/api/project/{{id}}/mcp/{{name}}/{what}", route(project_mcp_extra_route(what)))
          for what in ("resources", "resource", "prompts", "prompt")),
        Route("/api/mcp/{name}", route(mcp_one)),
        Route("/api/mcp/{name}/restart", route(mcp_restart), methods=["POST"]),
        Route("/api/skill", route(skill_list)),
        Route("/api/skill/preview", route(skill_preview), methods=["POST"]),
        Route("/api/skill/install", route(skill_install), methods=["POST"]),
        Route("/api/skill/{name}", route(skill_one)),
        Route("/api/project", route(projects), methods=["GET", "POST"]),
        Route("/api/project/{id}", route(project), methods=["GET", "PATCH", "DELETE"]),
        Route("/api/project/{id}/move", route(project_move), methods=["POST"]),
        Route("/api/project/{id}/remove", route(project_remove), methods=["POST"]),
        Route("/api/project/{id}/worktrees", route(project_worktrees)),
        Route("/api/project/{id}/worktree/review", route(project_worktree_review)),
        Route("/api/project/{id}/worktree/integrate", route(project_worktree_integrate), methods=["POST"]),
        Route("/api/project/{id}/git", route(project_git)),
        Route("/api/project/{id}/git/diff", route(project_git_diff)),
        Route("/api/project/{id}/git/live", route(project_git_live)),
        Route("/api/project/{id}/git/action", route(project_git_action), methods=["POST"]),
        Route("/api/project/{id}/mcp", route(project_mcp_list)),
        Route("/api/project/{id}/mcp/{name}/restart", route(project_mcp_restart), methods=["POST"]),
        Route("/api/project/{id}/mcp/{name}", route(project_mcp_one)),
        Route("/api/project/{id}/files", route(project_files), methods=["GET", "POST"]),
        Route("/api/project/{id}/watch", route(project_watch), methods=["POST"]),
        Route("/api/project/{id}/file", route(project_file), methods=["GET", "PUT"]),
        Route("/api/project/{id}/files/search", route(project_file_search)),
        Route("/api/project/{id}/prompts", route(project_prompts)),
        Route("/api/search", route(search)),
        Route("/api/artifact/{id}", route(artifact_one)),
        Route("/api/session", route(sessions), methods=["GET", "POST"]),
        Route("/api/draft/context", route(draft_context), methods=["POST"]),
        Route("/api/session/{id}", route(session), methods=["GET", "PATCH", "DELETE"]),
        Route("/api/session/{id}/model", route(session_model), methods=["POST"]),
        Route("/api/session/{id}/switch-cost", route(session_switch_cost)),
        Route("/api/session/{id}/agent", route(session_agent), methods=["POST"]),
        Route("/api/session/{id}/workspace-tools", route(session_workspace_tools), methods=["POST"]),
        Route("/api/session/{id}/archive", route(session_archive), methods=["POST"]),
        Route("/api/session/{id}/unarchive", route(session_unarchive), methods=["POST"]),
        Route("/api/session/{id}/directory", route(session_directory), methods=["POST"]),
        Route("/api/session/{id}/worktrees", route(session_worktrees), methods=["GET", "POST"]),
        Route("/api/session/{id}/git", route(session_git)),
        Route("/api/session/{id}/git/diff", route(session_git_diff)),
        Route("/api/session/{id}/prompt", route(prompt), methods=["POST"]),
        Route("/api/session/{id}/shell", route(shell), methods=["POST"]),
        Route("/api/session/{id}/input", route(session_inputs)),
        Route("/api/session/{id}/input/next", route(next_input), methods=["POST"]),
        Route("/api/session/{id}/input/{iid}", route(session_input), methods=["PATCH", "DELETE"]),
        Route("/api/session/{id}/input/{iid}/move", route(move_input), methods=["POST"]),
        Route("/api/session/{id}/continue", route(session_continue), methods=["POST"]),
        Route("/api/session/{id}/interrupt", route(interrupt), methods=["POST"]),
        Route("/api/session/{id}/message", route(messages)),
        Route("/api/session/{id}/event", route(stored_events)),
        Route("/api/session/{id}/diagnostics", route(diagnostics), methods=["GET", "POST"]),
        Route("/api/session/{id}/turn", route(turns)),
        Route("/api/session/{id}/inspect", route(native_inspector)),
        Route("/api/session/{id}/inspect/head", route(native_inspector_head)),
        Route("/api/session/{id}/inspect/record", route(native_inspector_record)),
        Route("/api/session/{id}/children", route(children)),
        Route("/api/session/{id}/tree", route(tree)),
        Route("/api/session/{id}/context", route(context)),
        Route("/api/session/{id}/btw", route(btw), methods=["GET", "POST"]),
        Route("/api/session/{id}/handoff", route(handoff), methods=["POST"]),
        Route("/api/session/{id}/snapshot", route(session_snapshot_json)),
        Route("/api/session/{id}/export", route(session_export)),
        Route("/api/session/{id}/permission", route(permissions)),
        Route("/api/session/{id}/artifact", route(artifact_list)),
        Route("/api/session/{id}/grant", route(grants), methods=["GET", "DELETE"]),
        Route("/api/session/{id}/permission/{pid}", route(permission_reply), methods=["POST"]),
        Route("/api/session/{id}/question", route(questions)),
        Route("/api/session/{id}/question/{qid}", route(question_reply), methods=["POST"]),
        Route("/api/client/view", route(client_view), methods=["POST"]),
        Route("/api/manager", route(manager_status)),
        Route("/api/manager/episode", route(manager_episodes)),
        Route("/api/manager/context", route(manager_context)),
        Route("/api/manager/audit", route(manager_audit)),
        Route("/api/manager/consent", route(manager_consent), methods=["POST"]),
        Route("/api/manager/session", route(manager_session), methods=["GET", "POST"]),
        Route("/api/manager/live/stop", route(manager_live_stop), methods=["POST"]),
        Route("/api/manager/start", route(manager_start), methods=["POST"]),
        Route("/api/manager/stop", route(manager_stop), methods=["POST"]),
        Route("/api/manager/live/start", route(manager_live_start), methods=["POST"]),
        WebSocketRoute("/api/manager/live/audio", manager_live_audio),
        Route("/api/manager/say", route(manager_say), methods=["POST"]),
        Route("/api/manager/cancel", route(manager_cancel), methods=["POST"]),
        Route("/api/manager/activity", route(manager_activity), methods=["POST"]),
        Route("/api/manager/card/dismiss", route(manager_card_dismiss), methods=["POST"]),
        Route("/api/manager/navigate/result", route(manager_navigate_result), methods=["POST"]),
        Route("/api/event", route(events)),
    ]
    routes.extend(terminal_routes(terminal_service, runtime, route, token=token, url=url))

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await runtime.start_maintenance()
        watcher = asyncio.create_task(watch_config())
        usage_watcher = asyncio.create_task(usage_cache.run())
        try:
            yield
        finally:
            for task in login_tasks.values():
                task.cancel()
            if login_tasks:
                await asyncio.gather(*login_tasks.values(), return_exceptions=True)
            watcher.cancel()
            usage_watcher.cancel()
            with suppress(asyncio.CancelledError):
                await usage_watcher
            with suppress(asyncio.CancelledError):
                await watcher
            await voice.close()
            await file_watches.close()
            await runtime.shutdown()
            await terminal_service.shutdown()

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(GZipMiddleware)])
    # Raw per-stream registry for the future Manager; it does not choose a call holder.
    app.state.client_views = client_views
    app.state.file_watches = file_watches
    return app


def make_runtime(store: Store | None = None) -> Runtime:
    runtime = Runtime(store or Store(), Bus())
    runtime.recover()  # finish before the app can accept a request
    return runtime
