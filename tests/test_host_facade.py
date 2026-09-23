"""Phase 8a2 facade: the transport-neutral surface over a runtime.

Two halves. The **fake/injected runtime** half pins the wire contract, dispatch,
health, and the "no credential ever crosses the facade" rule without touching
disk or a provider. The **real Runtime offline** half proves the hard integration
properties end to end: a detached turn with no viewer, disconnect/reconnect with
a gap-free catch-up, first-responder approval, and reducer state parity between
the facade baseline and a direct fold of the session log.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import msgspec
import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.errors import SessionBusy, SessionError
from nexus.events import Event
from nexus.host import PROTOCOL_VERSION, HostFacade, Presence
from nexus.host import protocol as p
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.runtime import Runtime
from nexus.session.manager import SessionSummary, TrashRecord
from nexus.tools.permissions import Decision
from nexus.view import fold

REPO_ROOT = Path(__file__).resolve().parents[1]


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


# ---------------------------------------------------------------------------
# Fake/injected runtime
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self, session_id):
        self.id = session_id
        self.events = [
            Event(type="turn.started", seq=1, session=session_id),
            Event(type="turn.completed", seq=2, session=session_id),
        ]
        self.bound: dict[str, object] = {}
        self.enqueued: list[tuple[str, object]] = []
        self.active = False
        self.permissions: dict[str, str] = {}
        self.fail_start = False
        self.failed: list[tuple[str, str]] = []

    def bind(self, **kwargs):
        self.bound.update(kwargs)

    def enqueue(self, content):
        queued_id = f"q{len(self.enqueued)}"
        self.enqueued.append((queued_id, content))
        return queued_id

    def resolve_permission(self, request_id, decision):
        return self.permissions.pop(request_id, None) is not None

    async def start_turn(self, content=None, *, turn_id=None):
        if self.fail_start:
            raise RuntimeError("provider unavailable")
        self.active = True
        return turn_id

    def fail_turn(self, turn_id, error, *, reason=""):
        self.failed.append((turn_id, error))
        self.events.append(
            Event(
                type="turn.failed",
                data={"error": error, "reason": reason},
                seq=len(self.events) + 1,
                session=self.id,
                turn=turn_id,
            )
        )

    async def wait_turn(self, turn_id=None):
        self.active = False

    def cancel(self, reason=None, drop_queue=True):
        self.active = False

    async def subscribe(self, from_seq=0, follow=True):
        for event in self.events:
            if event.seq > from_seq:
                yield event


class _FakeSessions:
    def __init__(self):
        self.handles: dict[str, _FakeHandle] = {}
        self.deleted: list[str] = []
        self.closed_all = False

    def handle(self, session_id):
        return self.handles.setdefault(session_id, _FakeHandle(session_id))

    def list(self):
        return [SessionSummary(id=sid) for sid in sorted(self.handles)]

    def summary(self, session_id):
        return SessionSummary(id=session_id, title="t")

    def open(self, session_id, **kwargs):
        return self.handle(session_id)

    def fork(self, session_id, at_seq=None, *, new_id=None):
        child = new_id or f"{session_id}-fork"
        return self.handle(child)

    def delete(self, session_id, *, force=False, reason=""):
        self.deleted.append(session_id)
        return TrashRecord(
            trash_id=f"{session_id}-t",
            session_id=session_id,
            trashed_at=1.0,
            delete_after=2.0,
        )

    def restore(self, trash_id):
        return trash_id.rsplit("-t", 1)[0]

    def export(self, session_id, *, format="json"):
        return f"export:{session_id}:{format}"

    async def aclose_all(self):
        self.closed_all = True
        return []


class _FakeExtensions:
    generation = 7

    def __init__(self):
        self.reloaded = 0

    async def reload(self, trigger="api"):
        self.reloaded += 1
        return SimpleNamespace(
            generation=8,
            previous_generation=7,
            changed=True,
            diff=SimpleNamespace(
                modules=SimpleNamespace(added=("mod_a",), removed=("mod_b",))
            ),
            failed=(),
        )

    def list_extensions(self):
        return ({"name": "mod_a", "origin": "workspace"},)

    def validate(self, target=None):
        return SimpleNamespace(
            valid=True,
            checked=1,
            results=({"name": "mod_a", "ok": True, "detail": ""},),
        )

    async def trash(self, target, *, reason="", force=False):
        return SimpleNamespace(
            record=SimpleNamespace(
                trash_id="mod_a-abc",
                source_path=target,
                relative_path=target,
                origin="workspace",
                modules=("nexus_ext.mod_a__g7",),
                tools=("ModA",),
                sha256="a" * 64,
                generation=7,
                trashed_at=1.0,
                delete_after=2.0,
                reason=reason,
            ),
            report=SimpleNamespace(
                changed=True, generation=9, previous_generation=8
            ),
        )

    def diagnostics(self):
        return ()


class _FakeTiers:
    order = ("low", "medium", "high")
    default = "medium"
    builtin: ClassVar[dict[str, str]] = {"anthropic/claude-opus-5": "high"}
    overrides: ClassVar[dict[str, str]] = {}


class _FakeRegistry:
    def list(self, *, provider=None, tier=None, selectable_only=False, search=None):
        return [{"id": "m", "provider": provider or "p", "tier": tier}]

    def get(self, ref):
        if ref == "p/m":
            return SimpleNamespace(id="m", provider="p")
        return None

    def status(self):
        return SimpleNamespace(source="cache", models=1, stale=False)


class _FakeRuntime:
    def __init__(self):
        self.workspace = "/tmp/ws"
        self.sessions = _FakeSessions()
        self.extensions = _FakeExtensions()
        self.registry = _FakeRegistry()
        self.tiers = _FakeTiers()
        self.providers = {"scripted": SimpleNamespace()}
        self.agents = SimpleNamespace(
            generation=3,
            index=(
                SimpleNamespace(
                    name="explore",
                    description="read-only",
                    source="builtin",
                    model=None,
                    read_only=True,
                ),
            ),
        )
        self.closed = False
        self.refreshed = 0

    def session(self, session_id, *, create=True, recover=True):
        return self.sessions.open(session_id)

    async def list_tools(self):
        return [
            {
                "name": "Read",
                "description": "read a file",
                "bundle": "fs",
                "mutates": False,
                "input_schema": {"type": "object"},
            }
        ]

    async def refresh_models(self):
        self.refreshed += 1
        return {"source": "cache", "models": 1}

    async def aclose(self):
        self.closed = True


class _SecretRuntime(_FakeRuntime):
    def session(self, session_id, *, create=True, recover=True):
        raise SessionError("connect failed for api_key=sk-live-supersecret123456")


def test_protocol_round_trips_every_command_and_result():
    commands = [
        p.SessionList(),
        p.SessionOpen(session="s"),
        p.SessionStart(session="s", content="hi"),
        p.SessionEnqueue(session="s", content="hi"),
        p.SessionCancel(session="s"),
        p.SessionSubscribe(session="s", from_seq=3),
        p.SessionState(session="s", from_seq=1),
        p.SessionFork(session="s", at_seq=2, new_id="c"),
        p.SessionDelete(session="s", force=True),
        p.SessionRestore(trash_id="t"),
        p.SessionExport(session="s", format="markdown"),
        p.PermissionResolve(session="s", request_id="r", decision="allow_once"),
        p.ExtensionsReload(),
        p.ExtensionsList(),
        p.ExtensionsValidate(target="a.py"),
        p.ExtensionsTrash(target="a.py"),
        p.ModelsRefresh(),
        p.ModelsList(provider="p", tier="high"),
        p.ModelShow(ref="p/m"),
        p.ModelTiers(),
        p.AgentsList(),
        p.ToolsList(),
        p.Doctor(explain_reload=True),
        p.Health(),
        p.Shutdown(reason="bye"),
    ]
    assert {type(command) for command in commands} == set(p.COMMANDS)
    for command in commands:
        assert p.decode_command(p.encode_command(command)) == command

    summary = SessionSummary(id="s", title="hello", last_seq=4, viewers=2)
    results = [
        p.SessionListResult(sessions=[summary]),
        p.SessionOpenResult(session=summary),
        p.SessionStartResult(session="s", turn_id="t"),
        p.SessionEnqueueResult(session="s", queued_id="q", depth=1),
        p.SessionCancelResult(session="s", cancelled=True, dropped=2),
        p.SessionSubscribeResult(session="s", from_seq=0),
        p.SessionStateResult(session="s", seq=4, view={"session_id": "s"}),
        p.SessionForkResult(session=summary),
        p.SessionDeleteResult(session="s", trash_id="t", delete_after=2.0),
        p.SessionRestoreResult(session="s"),
        p.SessionExportResult(session="s", format="json", content="{}"),
        p.PermissionResolveResult(session="s", request_id="r", resolved=True),
        p.ExtensionsReloadResult(generation=8),
        p.ExtensionsListResult(generation=8, extensions=[{"name": "m"}]),
        p.ExtensionsValidateResult(generation=8, valid=True, checked=1, results=[{"ok": True}]),
        p.ExtensionsTrashResult(target="a.py", trash_id="t", names=["m"]),
        p.ModelsRefreshResult(status={"source": "cache"}),
        p.ModelsListResult(count=1, models=[{"id": "m"}]),
        p.ModelShowResult(ref="p/m", found=True, model={"id": "m"}),
        p.ModelTiersResult(order=["low", "medium", "high"], default="medium"),
        p.AgentsListResult(generation=3, agents=[{"name": "explore"}]),
        p.ToolsListResult(count=1, tools=[{"name": "Read"}]),
        p.DoctorResult(ok=True, report={"workspace": "/tmp/ws"}),
        p.HealthResult(ok=True, version=PROTOCOL_VERSION),
        p.ShutdownResult(stopping=True),
        p.ErrorResult(kind="SessionError", message="no"),
    ]
    assert {type(result) for result in results} == set(p.RESULTS)
    for result in results:
        assert p.decode_result(p.encode_result(result)) == result

    with pytest.raises(msgspec.ValidationError):
        p.decode_command(b'{"type": "DoesNotExist"}')


async def test_facade_handle_dispatches_every_verb():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    runtime.sessions.handle("s")

    opened = await facade.handle(p.SessionOpen(session="s"))
    assert isinstance(opened, p.SessionOpenResult)

    start = await facade.handle(p.SessionStart(session="s", content="hi"))
    assert isinstance(start, p.SessionStartResult) and start.turn_id
    await facade.wait_idle(timeout=2.0)

    enqueued = await facade.handle(p.SessionEnqueue(session="s", content="later"))
    assert isinstance(enqueued, p.SessionEnqueueResult)
    assert enqueued.queued_id == "q0"
    await facade.wait_idle(timeout=2.0)

    state = await facade.handle(p.SessionState(session="s"))
    assert isinstance(state, p.SessionStateResult)
    assert state.view["last_seq"] == 2

    listed = await facade.handle(p.SessionList())
    assert isinstance(listed, p.SessionListResult)

    reloaded = await facade.handle(p.ExtensionsReload())
    assert isinstance(reloaded, p.ExtensionsReloadResult)
    assert reloaded.loaded == ["mod_a"] and reloaded.unloaded == ["mod_b"]

    ext = await facade.handle(p.ExtensionsList())
    assert isinstance(ext, p.ExtensionsListResult) and ext.generation == 7

    validated = await facade.handle(p.ExtensionsValidate(target="a.py"))
    assert isinstance(validated, p.ExtensionsValidateResult)
    assert validated.valid and validated.checked == 1

    trashed = await facade.handle(p.ExtensionsTrash(target="a.py"))
    assert isinstance(trashed, p.ExtensionsTrashResult)
    assert trashed.trash_id == "mod_a-abc" and trashed.changed is True
    assert trashed.previous_generation == 8 and trashed.generation == 9

    models = await facade.handle(p.ModelsList(provider="openai"))
    assert isinstance(models, p.ModelsListResult) and models.count == 1

    shown = await facade.handle(p.ModelShow(ref="p/m"))
    assert isinstance(shown, p.ModelShowResult) and shown.found

    missing = await facade.handle(p.ModelShow(ref="nope/x"))
    assert isinstance(missing, p.ModelShowResult) and not missing.found

    tiers = await facade.handle(p.ModelTiers())
    assert isinstance(tiers, p.ModelTiersResult)
    assert tiers.default == "medium" and tiers.order == ["low", "medium", "high"]

    doctor = await facade.handle(p.Doctor(explain_reload=True))
    assert isinstance(doctor, p.DoctorResult) and doctor.ok
    assert doctor.report["reload"]["hot"]

    refreshed = await facade.handle(p.ModelsRefresh())
    assert isinstance(refreshed, p.ModelsRefreshResult)
    assert refreshed.status == {"source": "cache", "models": 1}

    agents = await facade.handle(p.AgentsList())
    assert isinstance(agents, p.AgentsListResult) and agents.agents[0]["name"] == "explore"

    tools = await facade.handle(p.ToolsList())
    assert isinstance(tools, p.ToolsListResult) and tools.count == 1
    assert tools.tools[0]["name"] == "Read"

    deleted = await facade.handle(p.SessionDelete(session="s"))
    assert isinstance(deleted, p.SessionDeleteResult)
    assert runtime.sessions.deleted == ["s"]

    health = await facade.handle(p.Health())
    assert isinstance(health, p.HealthResult) and health.ok
    assert health.version == PROTOCOL_VERSION

    stopped = await facade.handle(p.Shutdown())
    assert isinstance(stopped, p.ShutdownResult) and stopped.stopping


async def test_facade_health_exposes_counters_only():
    facade = HostFacade(_FakeRuntime())
    health = facade.health()
    assert set(health) == {
        "ok",
        "version",
        "sessions",
        "running",
        "queued",
        "max_concurrent",
        "viewers",
        "uptime",
    }
    assert not hasattr(facade, "config")
    assert not hasattr(facade, "environ")


async def test_facade_redacts_credentials_from_error_results():
    facade = HostFacade(_SecretRuntime())
    result = await facade.handle(p.SessionOpen(session="s"))
    assert isinstance(result, p.ErrorResult)
    assert "sk-live-supersecret123456" not in result.message
    assert "***" in result.message


async def test_facade_enqueue_disables_session_auto_start():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    await facade.enqueue("s", "hello")
    handle = runtime.sessions.handle("s")
    assert handle.bound.get("auto_start_queued") is False
    assert handle.enqueued == [("q0", "hello")]


async def test_facade_resolve_permission_first_responder_wins():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    handle = runtime.sessions.handle("s")
    handle.permissions["r1"] = "pending"

    assert facade.resolve_permission("s", "r1", "allow_once", client_id="a") is True
    assert facade.resolve_permission("s", "r1", "deny_once", client_id="b") is False
    # An unknown id never resolves and never holds a lease.
    assert facade.resolve_permission("s", "missing", "allow_once", client_id="a") is False
    assert facade.presence.holder("s", "r1") is None


def test_presence_detach_releases_the_lease_the_view_held():
    """A disconnecting view frees its first-responder lease (PLAN §14.6).

    A lease is keyed by the ``client_id`` passed to ``claim``, never by the
    attachment token, so releasing on detach by the token left the lease held by
    a ghost and a live view lost a race against a dead one.
    """
    presence = Presence()
    view_a = presence.attach("s", "view-a")
    presence.attach("s", "view-b")

    assert presence.claim("s", "r1", "view-a") is True
    assert presence.holder("s", "r1") == "view-a"
    assert presence.claim("s", "r1", "view-b") is False

    presence.detach(view_a)
    assert presence.holder("s", "r1") is None
    assert presence.claim("s", "r1", "view-b") is True
    assert presence.holder("s", "r1") == "view-b"


def test_presence_detach_keeps_a_lease_while_a_view_shares_the_client_id():
    """A client id is not an identity: a still-attached view keeps the lease."""
    presence = Presence()
    first = presence.attach("s", "shared")
    second = presence.attach("s", "shared")
    assert presence.claim("s", "r1", "shared") is True

    presence.detach(first)
    assert presence.holder("s", "r1") == "shared"

    presence.detach(second)
    assert presence.holder("s", "r1") is None


async def test_facade_shutdown_closes_sessions_and_runtime():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime, owns_runtime=True)
    assert await facade.shutdown("bye") is True
    assert runtime.sessions.closed_all is True
    assert runtime.closed is True
    assert await facade.shutdown() is False  # idempotent


async def test_facade_start_failure_persists_turn_failed_for_followers():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    handle = runtime.sessions.handle("s")
    handle.fail_start = True

    turn_id = await facade.start_turn("s", "hi")
    await facade.wait_idle(timeout=2.0)

    assert handle.failed and handle.failed[0][0] == turn_id
    events = [event async for event in facade.subscribe("s", 0, follow=False)]
    assert any(event.type == "turn.failed" for event in events)


async def test_facade_validate_extensions_without_manager_uses_results_shape():
    runtime = _FakeRuntime()
    runtime.extensions = None
    facade = HostFacade(runtime)

    result = await facade.handle(p.ExtensionsValidate())
    assert isinstance(result, p.ExtensionsValidateResult)
    assert result.valid is True and result.checked == 0
    assert result.results == []
    assert facade.validate_extensions()["results"] == []


async def test_facade_doctor_reports_mcp_server_health():
    runtime = _FakeRuntime()
    runtime.mcp = SimpleNamespace(
        statuses=lambda: (
            SimpleNamespace(
                name="github",
                health=SimpleNamespace(value="connected"),
                connected=True,
                tool_count=3,
                last_error="",
            ),
        ),
        diagnostics=lambda: ({"name": "github", "kind": "server", "error": "boom"},),
    )
    facade = HostFacade(runtime)

    report = facade.doctor()
    assert report["mcp"]["servers"][0]["name"] == "github"
    assert report["mcp"]["servers"][0]["tool_count"] == 3
    assert report["mcp"]["diagnostics"] == [
        {"name": "github", "kind": "server", "error": "boom"}
    ]


# ---------------------------------------------------------------------------
# Real Runtime offline
# ---------------------------------------------------------------------------


def _config() -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )


def _runtime(tmp_path, provider) -> Runtime:
    return Runtime(tmp_path, config=_config(), providers={"scripted": provider})


async def test_facade_state_matches_a_direct_fold_of_the_log(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("world")))
    facade = HostFacade(runtime)
    facade.open_session("s")

    await facade.start_turn("s", "hello")
    await facade.wait_idle(timeout=5.0)

    handle = runtime.session("s")
    view, seq = facade.state("s")
    direct = fold(handle.events)
    assert view.to_dict() == direct.to_dict()
    assert seq == handle.events[-1].seq
    assert any("world" in message.text for message in view.messages)
    assert view.turns and view.turns[-1].terminal
    await runtime.aclose()


async def test_facade_disconnect_then_reconnect_catches_up(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("answer")))
    facade = HostFacade(runtime)
    facade.open_session("s")

    first = facade.subscribe("s", 0, follow=True, client_id="view-1")
    await first.__anext__()
    assert facade.presence.viewers("s") == 1

    await facade.start_turn("s", "question")
    await facade.wait_idle(timeout=5.0)
    await first.aclose()
    assert facade.presence.viewers("s") == 0

    # A reconnecting view replays the whole turn from the log, gap-free.
    before = list(runtime.session("s").events)
    replayed = [event async for event in facade.subscribe("s", 0, follow=False)]
    assert replayed[: len(before)] == before
    assert any(event.type == "turn.completed" for event in replayed)
    view, seq = facade.state("s")
    assert seq == runtime.session("s").events[-1].seq
    assert any("answer" in message.text for message in view.messages)
    await runtime.aclose()


async def test_facade_first_approval_from_two_views(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    facade.open_session("s")

    view_a = facade.subscribe("s", 0, follow=True, client_id="a")
    await view_a.__anext__()
    view_b = facade.subscribe("s", 0, follow=True, client_id="b")
    await view_b.__anext__()
    assert facade.presence.viewers("s") == 2

    await facade.start_turn("s", "go")
    handle = runtime.session("s")
    await wait_for(lambda: handle.pending_permissions)
    request_id = handle.pending_permissions[0]

    assert facade.resolve_permission(
        "s", request_id, Decision.ALLOW_ONCE, client_id="a"
    ) is True
    assert facade.resolve_permission(
        "s", request_id, Decision.DENY_ONCE, client_id="b"
    ) is False
    await facade.wait_idle(timeout=5.0)
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"

    await view_a.aclose()
    await view_b.aclose()
    await runtime.aclose()


async def test_facade_enqueue_runs_at_the_next_turn_boundary(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("ok"), text_response("two")))
    facade = HostFacade(runtime)
    facade.open_session("s")

    await facade.start_turn("s", "one")
    queued_id, turn_id = await facade.enqueue("s", "two")
    assert queued_id and turn_id
    await facade.wait_idle(timeout=5.0)

    handle = runtime.session("s")
    kinds = [event.type for event in handle.events]
    assert kinds.count("input.queued") == 1
    assert "input.consumed" in kinds
    assert kinds[-1] == "turn.completed"
    assert handle.queue_depth == 0
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Delete must refuse scheduled/durable work so a session cannot resurrect
# ---------------------------------------------------------------------------


async def test_facade_delete_refuses_active_and_parked_work_then_no_resurrection(
    tmp_path,
):
    """With a cap of 1, A is active and B parked; both deletes must be refused.

    Then B is cancelled while idle parked (which must drop its durable queue),
    deleted, and A is allowed to finish. If the parked submission had been
    stranded it would start against a deleted session -- so no second script may
    ever be consumed.
    """
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [
            MessageStart(model="m", provider="scripted"),
            TextDelta(text="working"),
            Wait(gate),
            MessageStop(stop_reason="stop"),
        ],
        text_response("second"),
    )
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime, max_concurrent_turns=1)
    facade.open_session("a")
    facade.open_session("b")

    await facade.start_turn("a", "one")
    await wait_for(lambda: runtime.session("a").active)
    queued_id, turn_id = await facade.enqueue("b", "two")
    assert queued_id and turn_id
    await wait_for(lambda: facade.supervisor.queued_for("b") == 1)
    assert facade.supervisor.running_for("a") == 1

    with pytest.raises(SessionBusy):
        facade.delete("a")
    with pytest.raises(SessionBusy):
        facade.delete("b", force=True)
    # Neither attempt touched the scheduled work.
    assert facade.supervisor.running_for("a") == 1
    assert facade.supervisor.queued_for("b") == 1

    # Cancelling the idle parked B drops the supervisor queue *and* the session's
    # durable FIFO (emitting input.dropped).
    cancelled, dropped = await facade.cancel("b")
    assert cancelled is False and dropped == 1
    assert runtime.session("b").queue_depth == 0
    kinds = [event.type for event in runtime.session("b").events]
    assert kinds.count("input.queued") == 1
    assert kinds.count("input.dropped") == 1

    assert facade.delete("b").session_id == "b"
    assert not runtime.sessions.exists("b")

    gate.set()
    await facade.wait_idle(timeout=5.0)

    # No resurrection: B is still gone and its parked turn never started.
    assert not runtime.sessions.exists("b")
    assert facade.supervisor.queued_for("b") == 0
    assert provider.calls == 1  # only A's script ran
    assert facade.delete("a").session_id == "a"
    await runtime.aclose()


async def test_facade_delete_refuses_a_session_only_queued_input(tmp_path):
    """The facade refuses a delete when only the session holds queued input."""
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("ok")))
    facade = HostFacade(runtime)
    handle = runtime.session("s")
    handle.enqueue("later")
    assert handle.queue_depth == 1

    with pytest.raises(SessionBusy):
        facade.delete("s", force=True)
    assert runtime.sessions.exists("s")

    handle.cancel(drop_queue=True)
    assert facade.delete("s").session_id == "s"
    await runtime.aclose()


async def test_facade_force_delete_then_view_disconnect_no_resurrection(tmp_path):
    """Detaching a view after a force-delete must not recreate the log."""
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("bye")))
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.start_turn("s", "hi")
    await facade.wait_idle(timeout=5.0)
    handle = runtime.session("s")
    log_path = handle.path

    view = facade.subscribe("s", 0, follow=True, client_id="view")
    await view.__anext__()
    assert facade.presence.viewers("s") == 1

    record = facade.delete("s", force=True)
    assert not runtime.sessions.exists("s")
    assert not log_path.exists()

    # Closing the view runs the same presence cleanup that used to append to the
    # moved log and resurrect the session.
    await view.aclose()
    assert facade.presence.viewers("s") == 0
    assert not log_path.exists()
    assert not runtime.sessions.exists("s")
    assert [item.session_id for item in facade.list_trashed()] == ["s"]
    assert runtime.sessions.restore(record.trash_id) == "s"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Layering: host imports downward only, never a UI
# ---------------------------------------------------------------------------

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_host_layer_never_imports_a_ui():
    host_root = REPO_ROOT / "nexus" / "host"
    files = sorted(host_root.rglob("*.py"))
    assert files
    forbidden = ("nexus.ui", "nexus.cli")
    for path in files:
        violations = sorted(
            module for module in _imports(path) if module.startswith(forbidden)
        )
        assert not violations, f"{path.name} imports {violations}"
