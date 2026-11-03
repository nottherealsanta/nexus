"""A real Core (store, bus, runtime, HTTP app) with the scripted Manager model, for the Manager's tests and for
client development: `Harness` shows how to drive a call with `mock/manager_demo` and read its events."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from nexus.eventlog import Stream
from nexus.llmhub.providers import PROVIDERS, ChatQuirks, ProviderInfo
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


def enable_manager(home: Path, model: str = "mock/manager_demo", **extra: Any) -> None:
    lines = ["[manager]", "enabled = true", f'model = "{model}"']
    lines += [f"{k} = {v!r}".replace("'", '"') if isinstance(v, str) else f"{k} = {str(v).lower()}"
              for k, v in extra.items()]
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    (home / "config.toml").write_text("\n".join(lines) + "\n")


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: Any, *, model: str = "mock/manager_demo", enabled: bool = True,
                 consent: bool = True, **settings: Any) -> None:
        # The offline mock provider exists only with NEXUS10_DEV=1 at import; tests switch it on directly.
        monkeypatch.setitem(PROVIDERS, "mock", ProviderInfo(
            "mock", "Offline mock", "scripted", "mock://", "mock", ChatQuirks(), verified=True))
        self.home = tmp_path / "home"
        monkeypatch.setenv("NEXUS10_HOME", str(self.home))
        if enabled:
            enable_manager(self.home, model, **settings)
        self.store = Store(tmp_path / "nexus.db")
        self.rt = make_runtime(self.store)
        self.app = build_app(self.rt, token=TOKEN, url="http://test")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test",
                                        headers={"authorization": f"Bearer {TOKEN}"})
        self.manager = self.rt.manager
        if consent:  # the privacy notice is acknowledged; test_manager_consent.py covers it not being
            self.manager.consent.set(model, True)
        self.tmp = tmp_path
        self.streams: list[Stream] = []
        self.events: list[dict[str, Any]] = []
        self._tasks: list[asyncio.Task[Any]] = []
        self.stream: Stream | None = None

    def project(self, name: str = "nexus") -> Any:
        path = self.tmp / name
        path.mkdir(exist_ok=True)
        return self.rt.open_project(str(path), name)

    def open_stream(self, sink: list[dict[str, Any]] | None = None) -> Stream:
        """An event stream as a client holds it (the same object `/api/event` serves)."""
        stream = Stream.open(self.rt.store, self.rt.bus)
        self.manager.views.attach(stream)
        sink = self.events if sink is None else sink

        async def pump() -> None:
            async for event in stream.events():
                sink.append(event)

        self._tasks.append(asyncio.ensure_future(pump()))
        self.streams.append(stream)
        self.stream = self.stream or stream
        return stream

    async def start(self, client_id: str = "tui", stream: Stream | None = None) -> dict[str, Any]:
        stream = stream or self.stream or self.open_stream()
        response = await self.client.post("/api/manager/start", json={"stream_id": stream.stream_id, "client_id": client_id})
        assert response.status_code == 200, response.text
        await asyncio.sleep(0.05)  # let the event pump see the call's first events
        return response.json()

    async def settle(self, timeout: float = 10.0) -> None:
        """Wait until the Manager has nothing queued or running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            if self.manager.turn is None and self.manager._queue.empty():
                await asyncio.sleep(0.05)
                if self.manager.turn is None and self.manager._queue.empty():
                    return
        raise AssertionError("the Manager did not settle")

    async def say(self, text: str, source: str = "typed", stream: Stream | None = None, *, wait: bool = True) -> dict[str, Any]:
        stream = stream or self.stream
        response = await self.client.post("/api/manager/say", json={"stream_id": stream.stream_id, "text": text, "source": source})
        assert response.status_code == 202, response.text
        if wait:
            await self.settle()
            await asyncio.sleep(0.05)
        return response.json()

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == kind]

    def spoken(self) -> list[str]:
        return [e["data"]["text"] for e in self.of("manager.speech")]

    def card(self) -> dict[str, Any] | None:
        cards = self.of("manager.card")
        return cards[-1]["data"]["card"] if cards else None

    async def answer_navigation(self, ok: bool = True, reason: str | None = None) -> None:
        """Play the client: answer the next `manager.navigate` event."""
        for _ in range(200):
            nav = [e for e in self.of("manager.navigate") if e["data"]["request_id"] not in getattr(self, "_answered", set())]
            if nav:
                self._answered = getattr(self, "_answered", set()) | {nav[-1]["data"]["request_id"]}
                response = await self.client.post("/api/manager/navigate/result", json={
                    "stream_id": self.stream.stream_id, "request_id": nav[-1]["data"]["request_id"], "ok": ok,
                    **({"reason": reason} if reason else {})})
                assert response.status_code == 204, response.text
                return
            await asyncio.sleep(0.02)
        raise AssertionError("no manager.navigate event arrived")

    async def report_view(self, project: str | None, session: str | None, panel: str | None = None) -> None:
        response = await self.client.post("/api/client/view", json={
            "stream_id": self.stream.stream_id, "client_id": "tui", "project": project, "session": session, "panel": panel})
        assert response.status_code == 200, response.text

    async def close(self) -> None:
        await self.rt.shutdown()
        for stream in self.streams:
            stream.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.aclose()


async def until(check, timeout: float = 8.0, what: str = "the condition") -> None:
    """Poll `check()` until it is truthy; the Manager's own turns take a moment to start (catalog lookup)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} never became true")


class Pending:
    """Fake pending approvals and questions in a runtime, as a session's turn would leave them."""

    def __init__(self, h: Harness) -> None:
        self.h = h

    def approval(self, session, command: str = "alembic upgrade head", *, publish: bool = True):
        from nexus.store import new_id

        aid = new_id("per")
        future = asyncio.get_running_loop().create_future()
        request = {"id": aid, "session_id": session.id, "tool": "bash", "call_id": "c1",
                   "args": {"command": command}, "grant": "bash: " + command}
        self.h.rt.approvals[aid] = (session.id, future, request)
        if publish:
            self.h.rt.bus.publish("permission.asked", request, session_id=session.id)
        return aid, future, request

    def question(self, session, text: str = "Which database should the test use?", *, publish: bool = True):
        from nexus.questions import PendingQuestion, normalize_request
        from nexus.store import new_id

        qid = new_id("qst")
        questions = normalize_request({"questions": [{"id": "db", "question": text, "options": [
            {"id": "pg", "label": "Postgres"}, {"id": "lite", "label": "SQLite"}]}]})
        self.h.rt.questions[qid] = PendingQuestion(qid, session.id, "c", questions, time.time(),
                                                   asyncio.get_running_loop().create_future())
        if publish:
            self.h.rt.bus.publish("question.asked", {"id": qid, "session_id": session.id, "questions": questions},
                                  session_id=session.id)
        return qid
