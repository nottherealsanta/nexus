"""The daemon's HTTP API, modelled on OpenCode's `/api/*` (opencode.ai/v2/docs/api).

Loopback only. Every request needs `Authorization: Bearer <token>` (the token is
in `<home>/daemon.json`, mode 0600). `GET /api/event` also accepts `?token=` so a
browser `EventSource` can connect. Errors are `{"error": "..."}` with 400, 401,
404 or 409.

| method | path | what |
| --- | --- | --- |
| GET | /api/info | daemon identity: version, pid, home, url |
| GET | /api/provider | the provider table, with `connected` |
| GET | /api/model | models of connected providers (models.dev); `?provider=` |
| GET | /api/model/default | the default `provider/model` |
| GET | /api/agent | agents for `?project=` or `?directory=` (built-in, global, project) and file problems |
| GET | /api/agent/{name} | one agent, with its full prompt |
| GET | /api/project | all projects |
| POST | /api/project | `{path, name?}` → the project at that path (created if new) |
| GET/PATCH/DELETE | /api/project/{id} | one project; PATCH `{name}` |
| GET | /api/session | `?project=`, `?directory=` or `?parent=` |
| POST | /api/session | `{project? or directory? or parent?, agent?, title?, model?, permission?, effort?}` |
| GET/PATCH/DELETE | /api/session/{id} | one session; PATCH `{agent?, title?, model?, permission?, effort?}` |
| POST | /api/session/{id}/model | `{model}`; `""` returns to the agent's or default model |
| POST | /api/session/{id}/agent | `{agent}` |
| POST | /api/session/{id}/prompt | `{text}` → 202; 409 while a turn runs |
| POST | /api/session/{id}/interrupt | cancel the running turn → 204 |
| GET | /api/session/{id}/message | saved messages, in order |
| GET | /api/session/{id}/event | stored events: `?after=<event id>&limit=&type=a,b` (the durable log) |
| GET | /api/session/{id}/children | direct children |
| GET | /api/session/{id}/tree | the whole tree containing it, from its root, nested |
| GET | /api/session/{id}/context | the request body the next turn would send |
| GET | /api/session/{id}/permission | pending tool approvals |
| POST | /api/session/{id}/permission/{pid} | `{reply: "allow" | "deny"}` → 204 |
| GET | /inspector | the session inspector page (static; it reads the API with the token from its URL fragment) |
| GET | /api/event | server-sent events; `?session=` filters to that session and its descendants |
"""
from __future__ import annotations

import hmac
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .bus import Bus
from .codec import to_json
from .llmhub import catalog
from .llmhub.hub import connected
from .llmhub.providers import PROVIDERS
from .paths import home
from .runtime import Busy, NotFound, Runtime, message_json, project_json
from .store import Store

VERSION = "0.10.0.dev0"
INSPECTOR = Path(__file__).parent / "inspector" / "index.html"
INSPECTOR_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"
MAX_BODY_BYTES = 1024 * 1024

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


def build_app(runtime: Runtime, *, token: str, url: str) -> Starlette:
    def route(handler: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            given = request.headers.get("authorization", "").removeprefix("Bearer ")
            if request.url.path == "/api/event":
                given = given or request.query_params.get("token", "")
            if not hmac.compare_digest(given, token):
                return _error(401, "missing or wrong token (see daemon.json)")
            try:
                return await handler(request)
            except NotFound as exc:
                return _error(404, str(exc))
            except Busy as exc:
                return _error(409, str(exc))
            except (ValueError, KeyError, TypeError) as exc:
                return _error(400, str(exc))
        return wrapped

    p = lambda request, name: request.path_params[name]

    async def info(request: Request) -> Response:
        return JSONResponse({"name": "nexus", "version": VERSION, "pid": os.getpid(), "home": str(home()), "url": url})

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
            return JSONResponse(project_json(runtime.rename_project(pid, (await _body(request))["name"])))
        if request.method == "DELETE":
            await runtime.delete_project(pid)
            return Response(status_code=204)
        return JSONResponse(project_json(runtime.project(pid)))

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
                                             parent=parent)
            return JSONResponse(runtime.session_json(session))
        if request.query_params.get("parent"):
            runtime.session(request.query_params["parent"])
            return JSONResponse([runtime.session_json(s) for s in runtime.store.children(request.query_params["parent"])])
        project_id = request.query_params.get("project")
        if request.query_params.get("directory"):
            project_id = runtime.open_project(request.query_params["directory"]).id
        return JSONResponse([runtime.session_json(s) for s in runtime.store.sessions(project_id)])

    async def session(request: Request) -> Response:
        sid = p(request, "id")
        if request.method == "PATCH":
            body = await _body(request)
            return JSONResponse(runtime.session_json(runtime.update_session(sid, **{k: body.get(k) for k in ("agent", "title", "model", "permission", "effort")})))
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
        return JSONResponse({"events": rows, "counts": runtime.store.event_counts(sid), "more": len(rows) == limit})

    async def children(request: Request) -> Response:
        runtime.session(p(request, "id"))
        return JSONResponse([runtime.session_json(s) for s in runtime.store.children(p(request, "id"))])

    async def tree(request: Request) -> Response:
        return JSONResponse(runtime.tree(p(request, "id")))

    async def session_model(request: Request) -> Response:
        return JSONResponse(runtime.session_json(runtime.update_session(p(request, "id"), model=(await _body(request))["model"])))

    async def session_agent(request: Request) -> Response:
        return JSONResponse(runtime.session_json(runtime.update_session(p(request, "id"), agent=(await _body(request))["agent"])))

    async def prompt(request: Request) -> Response:
        body = await _body(request)
        runtime.prompt(p(request, "id"), str(body["text"]))
        return JSONResponse({"status": "busy"}, status_code=202)

    async def interrupt(request: Request) -> Response:
        runtime.session(p(request, "id"))
        await runtime.interrupt(p(request, "id"))
        return Response(status_code=204)

    async def messages(request: Request) -> Response:
        runtime.session(p(request, "id"))
        return JSONResponse([message_json(m) for m in runtime.store.messages(p(request, "id"))])

    async def context(request: Request) -> Response:
        return JSONResponse(runtime.context(p(request, "id")))

    async def permissions(request: Request) -> Response:
        runtime.session(p(request, "id"))
        return JSONResponse(runtime.pending_approvals(p(request, "id")))

    async def permission_reply(request: Request) -> Response:
        reply = (await _body(request)).get("reply")
        if reply not in ("allow", "deny"):
            raise ValueError("reply must be 'allow' or 'deny'")
        runtime.reply(p(request, "id"), p(request, "pid"), reply == "allow")
        return Response(status_code=204)

    async def events(request: Request) -> Response:
        only = request.query_params.get("session")

        async def stream() -> AsyncIterator[str]:
            yield f"event: server.connected\ndata: {json.dumps({'version': VERSION})}\n\n"
            async for event in runtime.bus.subscribe():
                if only and event["session_id"] is not None and not runtime.in_tree(event["session_id"], only):
                    continue
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"cache-control": "no-cache"})

    async def inspector(request: Request) -> Response:
        # No data in the page itself, so no token needed to load it; every API call it makes needs the token.
        return HTMLResponse(INSPECTOR.read_text(), headers={"content-security-policy": INSPECTOR_CSP, "cache-control": "no-store",
                                                            "referrer-policy": "no-referrer"})

    routes = [
        Route("/inspector", inspector),
        Route("/api/info", route(info)),
        Route("/api/provider", route(providers)),
        Route("/api/model", route(models)),
        Route("/api/model/default", route(default_model)),
        Route("/api/agent", route(agent_list)),
        Route("/api/agent/{name}", route(agent_one)),
        Route("/api/project", route(projects), methods=["GET", "POST"]),
        Route("/api/project/{id}", route(project), methods=["GET", "PATCH", "DELETE"]),
        Route("/api/session", route(sessions), methods=["GET", "POST"]),
        Route("/api/session/{id}", route(session), methods=["GET", "PATCH", "DELETE"]),
        Route("/api/session/{id}/model", route(session_model), methods=["POST"]),
        Route("/api/session/{id}/agent", route(session_agent), methods=["POST"]),
        Route("/api/session/{id}/prompt", route(prompt), methods=["POST"]),
        Route("/api/session/{id}/interrupt", route(interrupt), methods=["POST"]),
        Route("/api/session/{id}/message", route(messages)),
        Route("/api/session/{id}/event", route(stored_events)),
        Route("/api/session/{id}/children", route(children)),
        Route("/api/session/{id}/tree", route(tree)),
        Route("/api/session/{id}/context", route(context)),
        Route("/api/session/{id}/permission", route(permissions)),
        Route("/api/session/{id}/permission/{pid}", route(permission_reply), methods=["POST"]),
        Route("/api/event", route(events)),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await runtime.shutdown()

    return Starlette(routes=routes, lifespan=lifespan)


def make_runtime(default_model: str, store: Store | None = None) -> Runtime:
    return Runtime(store or Store(), Bus(), default_model)
