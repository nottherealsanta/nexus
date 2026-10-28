"""The daemon's HTTP API, modelled on OpenCode's `/api/*` (opencode.ai/v2/docs/api).

Loopback only. Every request needs `Authorization: Bearer <token>` (the token is
in `<home>/daemon.json`, mode 0600). `GET /api/event` also accepts `?token=` so a
browser `EventSource` can connect. Errors are `{"error": "..."}` with 400, 401,
404 or 409.

| method | path | what |
| --- | --- | --- |
| GET | /api/info | daemon identity: version, pid, home, url, log generation and head |
| GET | /api/provider | the provider table, with `connected` |
| GET | /api/model | models of connected providers (models.dev); `?provider=` |
| GET | /api/model/default | the default `provider/model` |
| GET | /api/agent | agents for `?project=` or `?directory=` (built-in, global, project) and file problems |
| GET | /api/agent/{name} | one agent, with its full prompt |
| GET | /api/project | all projects |
| POST | /api/project | `{path, name?}` → the project at that path (created if new) |
| GET/PATCH/DELETE | /api/project/{id} | one project; PATCH `{name}` |
| GET | /api/project/{id}/worktrees | the project checkout's worktrees, before any session exists |
| GET | /api/session | `?project=`, `?directory=` or `?parent=`; `?archived=` exclude/only/include |
| POST | /api/session | `{project? or directory? or parent?, agent?, title?, model?, permission?, effort?}` |
| GET/PATCH/DELETE | /api/session/{id} | one session; PATCH `{agent?, title?, model?, permission?, effort?}` |
| POST | /api/session/{id}/archive | archive the whole root tree (child ids get 400) |
| POST | /api/session/{id}/unarchive | unarchive the whole root tree |
| POST | /api/session/{id}/model | `{model}`; `""` returns to the agent's or default model |
| POST | /api/session/{id}/agent | `{agent}` |
| POST | /api/session/{id}/workspace-tools | activate the two workspace control tools (idempotent) |
| POST | /api/session/{id}/directory | `{directory}`; 200 applied or 202 pending while a call runs |
| GET | /api/session/{id}/worktrees | list the repository's worktrees with branch and working-tree state |
| POST | /api/session/{id}/worktrees | `{action: "list" or "create" or "delete", path?, branch?, base?}` |
| POST | /api/session/{id}/prompt | `{text}` → 202; 409 while a turn runs |
| POST | /api/session/{id}/continue | start a turn from the stored context with no new user message → 202 |
| POST | /api/session/{id}/interrupt | cancel the running turn → 204 |
| GET | /api/session/{id}/message | saved messages, in order |
| GET | /api/session/{id}/event | stored events: `?after=<event id>&limit=&type=a,b` (the durable log) |
| GET | /api/session/{id}/children | direct children |
| GET | /api/session/{id}/tree | the whole tree containing it, from its root, nested |
| GET | /api/session/{id}/context | the request body the next turn would send |
| GET | /api/session/{id}/snapshot | the full frozen session snapshot |
| GET | /api/session/{id}/export | `?format=markdown or json&scope=session or tree`: a read-only export of one session or its descendants |
| GET | /api/session/{id}/permission | pending tool approvals |
| POST | /api/session/{id}/permission/{pid} | `{reply: "allow" | "deny"}` → 204 |
| GET | /api/question | every pending `ask` question request |
| GET | /api/session/{id}/question | pending questions for that session |
| POST | /api/session/{id}/question/{qid} | `{answers: [...]}` → 204; 400 invalid, 404 unknown, 409 already resolved |
| GET | /inspector | the session inspector page (static; it reads the API with the token from its URL fragment) |
| GET | /api/event | server-sent events; `?session=` filters to that session and its descendants; `?cursor=` or `Last-Event-ID` replays from a durable cursor |
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from anyio import CancelScope
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import export
from .bus import Bus
from .codec import to_json
from .eventlog import CursorError, Stream, format_cursor, parse_cursor
from .llmhub import catalog
from .llmhub.hub import connected
from .llmhub.providers import PROVIDERS
from .paths import home
from .runtime import Busy, Conflict, NotFound, Runtime, message_json, project_json
from .store import Store
from .tools import ToolError

VERSION = "0.10.0.dev0"
# The HTTP API contract clients check before sending anything else. A change
# that a client written for an older major cannot survive is a new major; new
# fields and endpoints are a minor bump.
API_VERSION = {"major": 1, "minor": 1}
INSPECTOR = Path(__file__).parent / "inspector" / "index.html"
INSPECTOR_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"
MAX_BODY_BYTES = 1024 * 1024
IDEMPOTENCY_MAX = 128
_IDEMPOTENCY_KEY = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
_MUTATING = frozenset({"POST", "PATCH", "DELETE"})
_KEYED = ("/prompt", "/continue")

Handler = Callable[[Request], Awaitable[Response]]


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
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
    """One SSE frame. Events without a durable id (stream control) carry no `id:` line."""
    lines = []
    if event.get("id") is not None:
        lines.append(f"id: {event.get('cursor') or event['id']}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event)}")
    return "\n".join(lines) + "\n\n"


def build_app(runtime: Runtime, *, token: str, url: str) -> Starlette:
    def route(handler: Handler) -> Handler:
        async def inner(request: Request) -> Response:
            try:
                return await handler(request)
            except NotFound as exc:
                return _error(404, str(exc))
            except Busy as exc:
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
                if len(raw) > MAX_BODY_BYTES:
                    raise ValueError("request body exceeds 1 MiB")
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
            "name": "nexus", "version": VERSION, "api_version": API_VERSION,
            "pid": os.getpid(), "home": str(home()), "url": url,
            "log": {"generation": generation, "head": head, "head_cursor": format_cursor(generation, head),
                    "floor": runtime.store.log_floor(),
                    "floor_cursor": format_cursor(generation, runtime.store.log_floor())},
            "retention": asdict(runtime.retention.settings),
            "retry": {"max_attempts": runtime.retry_policy.max_attempts,
                      "base_delay": runtime.retry_policy.base_delay,
                      "max_wait": runtime.retry_policy.max_wait,
                      "max_elapsed": runtime.retry_policy.max_elapsed},
            "idempotency": {"header": "Idempotency-Key", "max_length": IDEMPOTENCY_MAX,
                            "lifetime_seconds": runtime.mutation_lifetime},
        })

    async def providers(request: Request) -> Response:
        live = set(connected())
        return JSONResponse([
            {"id": i.id, "label": i.label, "dialect": i.dialect, "base_url": i.base_url, "catalog": i.catalog_id,
             "default_model": f"{i.id}/{i.default_model}", "connected": i.id in live, "verified": i.verified}
            for i in PROVIDERS.values()
        ])

    async def models(request: Request) -> Response:
        data, source = await catalog.load()
        wanted = request.query_params.get("provider")
        out = [to_json(m) | {"ref": m.ref} for pid in connected() if not wanted or pid == wanted for m in catalog.models_for(data, pid)]
        return JSONResponse({"source": source, "models": out})

    async def default_model(request: Request) -> Response:
        return JSONResponse({"model": runtime.default_model})

    def agent_json(agent: Any, *, prompt: bool) -> dict[str, Any]:
        data = {k: getattr(agent, k) for k in ("name", "description", "source", "path", "model", "effort", "tools", "steps")}
        return data | ({"prompt": agent.prompt} if prompt else {})

    def agent_project_path(request: Request) -> str | None:
        if request.query_params.get("project"):
            return runtime.project(request.query_params["project"]).path
        if request.query_params.get("directory"):
            return runtime.open_project(request.query_params["directory"]).path
        return None

    async def agent_list(request: Request) -> Response:
        found = runtime.agents(agent_project_path(request))
        return JSONResponse({"agents": [agent_json(a, prompt=False) for a in found.agents.values()], "problems": found.problems})

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
            return JSONResponse(project_json(runtime.rename_project(
                pid, body["name"], expected_revision=_expected_revision(request, body))))
        if request.method == "DELETE":
            await runtime.delete_project(pid)
            return Response(status_code=204)
        return JSONResponse(project_json(runtime.project(pid)))

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
        return JSONResponse(await files.list_directory(file_root(request), request.query_params.get("path", "")))

    async def project_file(request: Request) -> Response:
        from nexus import files
        return JSONResponse(files.read_file(file_root(request), request.query_params.get("path", "")))

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
            session = runtime.create_session(target, title=body.get("title"), model=body.get("model"),
                                             permission=body.get("permission"), effort=body.get("effort"), agent=body.get("agent"),
                                             parent=parent, disabled_tools=body.get("disabled_tools"))
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
            return JSONResponse(runtime.session_json(runtime.update_session(sid, **fields)))
        if request.method == "DELETE":
            await runtime.delete_session(sid)
            return Response(status_code=204)
        return JSONResponse(runtime.session_json(runtime.session(sid)))

    async def stored_events(request: Request) -> Response:
        sid = p(request, "id")
        runtime.session(sid)
        limit = min(int(request.query_params.get("limit") or 1000), 5000)
        types = [t for t in (request.query_params.get("type") or "").split(",") if t]
        rows = runtime.store.events(sid, after=int(request.query_params.get("after") or 0), limit=limit, types=types or None)
        return JSONResponse({"events": rows, "counts": runtime.store.event_counts(sid), "more": len(rows) == limit,
                             "floor": runtime.store.log_floor(), "head": runtime.store.log_head(),
                             "generation": runtime.store.log_generation(),
                             "history_truncated": runtime.store.log_floor() > 0})

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
        data["running"] = runtime.store.running_turns()
        data["approvals"] = [request_ for sid_, _, request_ in runtime.approvals.values()]
        data["questions"] = runtime.pending_questions()
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
        return JSONResponse(runtime.session_json(runtime.update_session(p(request, "id"), **fields)))

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
        return JSONResponse(runtime.session_json(runtime.archive_session(sid, archived=True)))

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
        result = runtime.prompt(p(request, "id"), str(body["text"]), mutation=mutation)
        if mutation is not None:
            result = {**result, "idempotency_key": mutation["key"]}
        return JSONResponse(result, status_code=202)

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
        return JSONResponse([message_json(m) for m in runtime.store.messages(p(request, "id"))])

    async def context(request: Request) -> Response:
        return JSONResponse(runtime.context(p(request, "id")))

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
        reply = (await _body(request)).get("reply")
        if reply not in ("allow", "deny"):
            raise ValueError("reply must be 'allow' or 'deny'")
        runtime.reply(p(request, "id"), p(request, "pid"), reply == "allow")
        return Response(status_code=204)

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
        return JSONResponse([k.json() for k in runtime.kernels.items.values()])

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
        connected = {
            "version": VERSION, "generation": stream.generation, "head": stream.head,
            "head_cursor": format_cursor(stream.generation, stream.head),
            "resumed_from": after, "session": only, "floor": store.log_floor(),
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
        Route("/api/question", route(all_questions)),
        Route("/api/bash", route(bash_jobs)),
        Route("/api/bash/{id}", route(bash_job), methods=["GET", "DELETE"]),
        Route("/api/kernel", route(kernels)),
        Route("/api/kernel/{id}", route(kernel), methods=["GET", "DELETE"]),
        Route("/api/provider", route(providers)),
        Route("/api/model", route(models)),
        Route("/api/model/default", route(default_model)),
        Route("/api/snapshot", route(snapshot)),
        Route("/api/agent", route(agent_list)),
        Route("/api/agent/{name}", route(agent_one)),
        Route("/api/project", route(projects), methods=["GET", "POST"]),
        Route("/api/project/{id}", route(project), methods=["GET", "PATCH", "DELETE"]),
        Route("/api/project/{id}/worktrees", route(project_worktrees)),
        Route("/api/project/{id}/files", route(project_files)),
        Route("/api/project/{id}/file", route(project_file)),
        Route("/api/session", route(sessions), methods=["GET", "POST"]),
        Route("/api/session/{id}", route(session), methods=["GET", "PATCH", "DELETE"]),
        Route("/api/session/{id}/model", route(session_model), methods=["POST"]),
        Route("/api/session/{id}/agent", route(session_agent), methods=["POST"]),
        Route("/api/session/{id}/workspace-tools", route(session_workspace_tools), methods=["POST"]),
        Route("/api/session/{id}/archive", route(session_archive), methods=["POST"]),
        Route("/api/session/{id}/unarchive", route(session_unarchive), methods=["POST"]),
        Route("/api/session/{id}/directory", route(session_directory), methods=["POST"]),
        Route("/api/session/{id}/worktrees", route(session_worktrees), methods=["GET", "POST"]),
        Route("/api/session/{id}/git", route(session_git)),
        Route("/api/session/{id}/git/diff", route(session_git_diff)),
        Route("/api/session/{id}/prompt", route(prompt), methods=["POST"]),
        Route("/api/session/{id}/continue", route(session_continue), methods=["POST"]),
        Route("/api/session/{id}/interrupt", route(interrupt), methods=["POST"]),
        Route("/api/session/{id}/message", route(messages)),
        Route("/api/session/{id}/event", route(stored_events)),
        Route("/api/session/{id}/children", route(children)),
        Route("/api/session/{id}/tree", route(tree)),
        Route("/api/session/{id}/context", route(context)),
        Route("/api/session/{id}/snapshot", route(session_snapshot_json)),
        Route("/api/session/{id}/export", route(session_export)),
        Route("/api/session/{id}/permission", route(permissions)),
        Route("/api/session/{id}/permission/{pid}", route(permission_reply), methods=["POST"]),
        Route("/api/session/{id}/question", route(questions)),
        Route("/api/session/{id}/question/{qid}", route(question_reply), methods=["POST"]),
        Route("/api/event", route(events)),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await runtime.start_maintenance()
        yield
        await runtime.shutdown()

    return Starlette(routes=routes, lifespan=lifespan)


def make_runtime(default_model: str, store: Store | None = None) -> Runtime:
    runtime = Runtime(store or Store(), Bus(), default_model)
    runtime.recover()  # finish before the app can accept a request
    return runtime
