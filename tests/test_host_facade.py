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
from nexus.session.db import SCHEMA_VERSION
from nexus.host import PROTOCOL_VERSION, HostFacade, Presence
from nexus.host import protocol as p
from nexus.model.message import Message, Text, ToolResult, ToolUse
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
from nexus.view import apply, fold

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

    def enqueue(self, content, *, mode="queue"):
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
            get=lambda name: SimpleNamespace(contexts=("subagent",)),
        )
        self.closed = False
        self.refreshed = 0

    def session(self, session_id, *, create=True, recover=True):
        return self.sessions.open(session_id)

    def select_session_model(self, session_id, ref):
        from nexus.model.selection import ModelSelection

        if not isinstance(ref, str) or not ref:
            raise ValueError("bad ref")
        return ModelSelection(
            reference=ref,
            provider="scripted",
            model=ref,
            tier="low",
            tier_source="tier",
        )

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
        p.SettingsInventory(scope="project"),
        p.SettingsRead(scope="project", category="agents", id="helper"),
        p.SettingsWrite(scope="project", category="agents", id="helper", body=""),
        p.SettingsDelete(scope="project", category="agents", id="helper"),
        p.SettingsReset(scope="project", category="agents"),
        p.SetupStatus(),
        p.SetupSave(provider="openai", model="gpt-5.6"),
        p.ProvidersStatus(),
        p.ProviderLogin(provider="github-copilot", method="device", domain="company.ghe.com"),
        p.ProviderLoginPoll(login_id="l"),
        p.ProviderLoginCancel(login_id="l"),
        p.ProviderKeySet(provider="opencode-go", key="sk-test-key"),
        p.ProviderLogout(provider="codex"),
        p.ProviderLoginCode(login_id="l", code="abc#state"),
        p.ProvidersUsage(),
        p.SessionArchive(session="s"),
        p.SessionUnarchive(session="s"),
        p.SessionListArchived(),
        p.SessionPreview(session="s"),
        p.SessionSearch(query="term"),
        p.SessionOpen(session="s"),
        p.AttachmentPrepare(name="note.txt", data=b"note"),
        p.AttachmentPreview(attachment_id="att-1"),
        p.SessionStart(session="s", content="hi"),
        p.SessionEnqueue(session="s", content="hi"),
        p.SessionCancel(session="s"),
        p.SessionSubscribe(session="s", from_seq=3),
        p.SessionState(session="s", from_seq=1),
        p.LogsRead(session="s"),
        p.AgentTranscript(session="s", agent_id="s/sub/1"),
        p.SessionFork(session="s", at_seq=2, new_id="c"),
        p.SessionDelete(session="s", force=True),
        p.SessionRestore(trash_id="t"),
        p.SessionExport(session="s", format="markdown"),
        p.PermissionResolve(session="s", request_id="r", decision="allow_once"),
        p.QuestionAnswer(session="s", answer="1", call_id="c"),
        p.ExtensionsReload(),
        p.ExtensionsList(),
        p.ExtensionsValidate(target="a.py"),
        p.ExtensionsTrash(target="a.py"),
        p.ModelsRefresh(),
        p.ModelsList(provider="p", tier="high"),
        p.ModelShow(ref="p/m"),
        p.ModelTiers(),
        p.DefaultModelSettings(),
        p.DefaultModelSet(refs=["openai/gpt-5-mini"]),
        p.SpeechStatus(),
        p.SpeechPrepare(),
        p.SpeakStop(),
        p.ModelTierSet(tier="low", refs=["openai/gpt-5-mini"]),
        p.ModelTierReset(tier="low"),
        p.AgentMaxTierSet(tier="high"),
        p.SessionTitleSettings(),
        p.SessionTitleSettingsSet(enabled=False, model="low"),
        p.ModelSelect(session="s", ref="high"),
        p.ReasoningEffortSelect(session="s", effort="high"),
        p.FileSearch(query="src"),
        p.GitDiff(staged=True, ref="HEAD"),
        p.WorktreeList(),
        p.WorktreeInspect(child_id="s/sub/1"),
        p.WorktreeReview(child_id="s/sub/1", review_id="a" * 32, cursor=2, limit=8),
        p.WorktreeAcknowledge(child_id="s/sub/1", review_id="a" * 32, digest="b" * 64),
        p.WorktreeIntegrate(child_id="s/sub/1", review_id="a" * 32, digest="b" * 64),
        p.WorktreeDiscard(child_id="s/sub/1", force=True, review_id="a" * 32),
        p.Speak(session_id="s", download=True),
        p.VoiceStatus(),
        p.VoicePrepare(force=True),
        p.VoiceTranscribe(audio=b"wav", request_id="req_1"),
        p.VoiceCancel(request_id="req_1"),
        p.VoiceRemove(),
        p.AgentsList(),
        p.AgentCurrent(session="s"),
        p.AgentSelect(session="s", name="general"),
        p.AgentReset(session="s"),
        p.AgentDefaultSet(name="build"),
        p.ToolsList(),
        p.ContextInspect(session="s"),
        p.ContextExtensionSelect(session="s", category="skills", name="example", enabled=False),
        p.ContextMcpLoadingSelect(session="s", server="example", mode="search"),
        p.SettingsMcpLoadingSet(scope="project", server="example", mode="all", expected_sha256="abc"),
        p.Doctor(explain_reload=True),
        p.UpdateStatus(),
        p.Health(),
        p.MockList(),
        p.MockStart(scenario="hello", speed=0.0, seed=1, session="mock-hello-1"),
        p.MockClean(),
        p.WebLaunch(),
        p.Shutdown(reason="bye"),
    ]
    assert {type(command) for command in commands} == set(p.COMMANDS)
    for command in commands:
        assert p.decode_command(p.encode_command(command)) == command

    summary = SessionSummary(id="s", title="hello", last_seq=4, viewers=2)
    results = [
        p.AttachmentPrepareResult("id", "note.txt", "markdown", "note"),
        p.SettingsInventoryResult(scope="project", root_display="<project>/.nexus"),
        p.SettingsReadResult(body="", rel_path="agents/a.md", builtin=False, sha256=""),
        p.SettingsWriteResult(status="written"),
        p.SettingsDeleteResult(status="trashed", trash_id="t"),
        p.SettingsResetResult(status="reset", trash_ids=["t"]),
        p.SetupStatusResult(required=True),
        p.SetupSaveResult(global_model="openai/gpt-5.6"),
        p.ProvidersStatusResult(providers=[{"id": "codex", "connected": False}]),
        p.ProviderLoginResult(login_id="l", provider="codex", url="https://auth.openai.com/x", user_code="AB-12"),
        p.ProviderAuthResult(provider="opencode-go", connected=True),
        p.ProvidersUsageResult(providers=[{"id": "codex", "windows": [{"label": "5-hour", "used_percent": 41.0}]}],
                               not_connected=["OpenCode Go"], fetched_at=1.0),
        p.SessionListResult(sessions=[summary]),
        p.SessionArchiveResult(session=summary),
        p.SessionUnarchiveResult(session=summary),
        p.SessionListArchivedResult(sessions=[p.ArchivedSummary(id="s")]),
        p.SessionPreviewResult(text="user: hi"),
        p.SessionSearchResult(ids=["s"]),
        p.SessionOpenResult(session=summary),
        p.SessionStartResult(session="s", turn_id="t"),
        p.SessionEnqueueResult(session="s", queued_id="q", depth=1),
        p.SessionCancelResult(session="s", cancelled=True, dropped=2),
        p.SessionSubscribeResult(session="s", from_seq=0),
        p.SessionStateResult(session="s", seq=4, view={"session_id": "s"}),
        p.LogsReadResult(
            daemon=p.DaemonLogPage(), session=p.SessionLogPage()
        ),
        p.AgentTranscriptResult(session="s", agent_id="s/sub/1", view={"id": "s/sub/1"}),
        p.SessionForkResult(session=summary),
        p.SessionDeleteResult(session="s", trash_id="t", delete_after=2.0),
        p.SessionRestoreResult(session="s"),
        p.SessionExportResult(session="s", format="json", content="{}"),
        p.PermissionResolveResult(session="s", request_id="r", resolved=True),
        p.QuestionAnswerResult(session="s", call_id="c", resolved=True),
        p.ExtensionsReloadResult(generation=8),
        p.ExtensionsListResult(generation=8, extensions=[{"name": "m"}]),
        p.ExtensionsValidateResult(generation=8, valid=True, checked=1, results=[{"ok": True}]),
        p.ExtensionsTrashResult(target="a.py", trash_id="t", names=["m"]),
        p.ModelsRefreshResult(status={"source": "cache"}),
        p.ModelsListResult(count=1, models=[{"id": "m"}]),
        p.ModelShowResult(ref="p/m", found=True, model={"id": "m"}),
        p.ModelTiersResult(order=["low", "medium", "high"], default="medium"),
        p.DefaultModelSettingsResult(refs=["openai/gpt-5-mini"]),
        p.SessionTitleSettingsResult(enabled=True, model="low", resolved="openai/gpt-5-mini"),
        p.SpeechStatusResult(state="absent", bytes_total=345_000_000),
        p.AttachmentPreviewResult(attachment_id="att-1", media_type="image/png", data=b"png"),
        p.ModelSelectResult(session="s", provider="p", model="m", tier="high"),
        p.ReasoningEffortSelectResult(
            session="s", stored_override="high", effective_effort="high",
            source="session", supported_levels=["low", "high"],
        ),
        p.FileSearchResult(paths=["src/main.py"]),
        p.GitDiffResult(patch="+line", truncated=False),
        p.WorktreeListResult(worktrees=[{"child_id": "s/sub/1"}]),
        p.WorktreeInspectResult(child_id="s/sub/1", status="finalized"),
        p.WorktreeReviewResult(
            child_id="s/sub/1", status="ok", entries=[{"path": "a.txt"}],
            diff=[{"path": "a.txt", "patch": "+line"}], has_more=True,
            review_id="a" * 32, digest="b" * 64,
        ),
        p.WorktreeAcknowledgeResult(
            child_id="s/sub/1", review_id="a" * 32, digest="b" * 64
        ),
        p.WorktreeMutationResult(
            child_id="s/sub/1", status="requires_confirmation",
            operation="integrate", confirmation_token="token", impact={"parent_clean": True},
        ),
        p.AgentsListResult(generation=3, agents=[{"name": "explore"}]),
        p.AgentCurrentResult(session="s", name="general", source="config"),
        p.AgentCurrentResult(
            session="s", supported_levels=["low", "high"],
            stored_override="high", reasoning_effort_source="session",
        ),
        p.AgentSelectResult(session="s", name="build"),
        p.AgentDefaultSetResult(name="build", effective="build", scope="global"),
        p.ToolsListResult(count=1, tools=[{"name": "Read"}]),
        p.ContextInspectResult(
            session="s",
            system_text="standing prompt",
            included_parts=[{"name": "identity", "text": "standing prompt"}],
        ),
        p.DoctorResult(ok=True, report={"workspace": "/tmp/ws"}),
        p.SpeakResult(message="Finished speaking", backend="kokoro-cpu"),
        p.VoiceStatusResult(state="ready", enabled=True),
        p.VoiceTranscribeResult(request_id="req_1", text="hello", duration_s=1.0, elapsed_s=0.1),
        p.VoiceCancelResult(cancelled=True),
        p.UpdateStatusResult(enabled=True, current="0.1.1", latest="0.1.2", available="0.1.2"),
        p.HealthResult(ok=True, version=PROTOCOL_VERSION),
        p.MockListResult(scenarios=[p.MockScenarioInfo(name="hello", summary="s")]),
        p.MockStartResult(session="mock-hello-1", scenario="hello", turn_id="t"),
        p.MockCleanResult(restored=True),
        p.WebLaunchResult(url="http://127.0.0.1:8080/#ticket"),
        p.ShutdownResult(stopping=True),
        p.ErrorResult(kind="SessionError", message="no"),
    ]
    assert {type(result) for result in results} == set(p.RESULTS)
    for result in results:
        assert p.decode_result(p.encode_result(result)) == result

    with pytest.raises(msgspec.ValidationError):
        p.decode_command(b'{"type": "DoesNotExist"}')

    legacy_current = p.decode_result(
        b'{"type":"AgentCurrentResult","session":"s","name":"general",'
        b'"source":"default","reasoning_effort":"high"}'
    )
    assert legacy_current == p.AgentCurrentResult(
        session="s", name="general", reasoning_effort="high"
    )


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

    default = await facade.handle(p.DefaultModelSettings())
    assert isinstance(default, p.DefaultModelSettingsResult)

    selected = await facade.handle(p.ModelSelect(session="s", ref="low"))
    assert isinstance(selected, p.ModelSelectResult)
    assert selected.accepted and selected.provider == "scripted"
    assert selected.model == "low" and selected.tier == "low"

    facade.daemon_info = {"pid": 123, "socket": "/tmp/nexus-test.sock"}
    doctor = await facade.handle(p.Doctor(explain_reload=True))
    assert isinstance(doctor, p.DoctorResult) and doctor.ok
    assert doctor.report["reload"]["hot"]
    assert doctor.report["daemon"] == facade.daemon_info

    # A runtime that cannot answer never breaks the advisory notice (and never
    # reaches the network from a test).
    update = await facade.handle(p.UpdateStatus())
    assert isinstance(update, p.UpdateStatusResult) and update.available is None

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


async def test_context_inspect_is_a_read_only_current_request_projection(tmp_path):
    runtime = _runtime(
        tmp_path,
        ScriptedProvider(text_response("unused")),
    )
    facade = HostFacade(runtime)
    try:
        session = runtime.session("context")
        await facade.start_turn("context", "history")
        await facade.wait_idle(timeout=5.0)
        before = list(session.events)
        result = await facade.handle(p.ContextInspect(session="context"))

        assert isinstance(result, p.ContextInspectResult)
        assert result.mode == "next_turn_preview"
        assert not result.actually_sent and not result.draft_provided
        assert result.manifest_generation is not None
        assert result.system_text
        assert "You are Nexus" not in result.system_text
        assert "--- Selected agent instructions ---" not in result.system_text
        assert result.tools_supported
        assert result.tools and all(
            isinstance(tool["input_schema"], dict) for tool in result.tools
        )
        assert result.omitted[0] == "draft input (not provided)"
        assert list(session.events) == before
        assert "history" not in (result.system_text or "")
        assert result.history_included
        assert result.messages
        assert result.messages[0]["role"] == "user"
        assert result.messages[0]["blocks"][0]["text"] == "history"
        assert result.request_context["final_messages"] == len(result.messages)
        assert list(session.events) == before
    finally:
        await runtime.aclose()


async def test_new_workspace_context_includes_packaged_build_prompt(tmp_path):
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    config = Config.load(workspace, home=tmp_path / "home", environ={})
    runtime = Runtime(workspace, config=config,
                      providers={"scripted": ScriptedProvider(text_response("unused"))})
    facade = HostFacade(runtime)
    try:
        facade.open_session("new-session")
        result = await facade.handle(p.ContextInspect(session="new-session"))
        assert isinstance(result, p.ContextInspectResult)
        assert result.agent["name"] == "build"
        assert result.agent["instructions_included"] is True
        assert "You are an expert coding assistant operating inside Nexus" in (result.system_text or "")
    finally:
        await runtime.aclose()


def test_context_block_projection_preserves_tool_call_result_linkage():
    call = Runtime._context_block(ToolUse(id="call-7", name="Read", input={"path": "a.py"}))
    result = Runtime._context_block(
        ToolResult(tool_use_id="call-7", content=[Text(text="contents")])
    )

    assert call["id"] == "call-7"
    assert result["tool_use_id"] == call["id"]
    assert result["content"] == [{"type": "text", "text": "contents"}]


async def test_context_inspect_reads_while_a_turn_is_active(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("unused")))
    facade = HostFacade(runtime)
    session = runtime.session("busy-context")
    lease = session.begin_turn()
    try:
        result = await facade.handle(p.ContextInspect(session="busy-context"))
        assert isinstance(result, p.ContextInspectResult)
    finally:
        lease.release()
        await runtime.aclose()


async def test_context_inspect_surfaces_only_activated_skills_and_connected_mcp(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("unused")))
    facade = HostFacade(runtime)
    try:
        await runtime.ensure_started()
        facade.open_session("indexes")
        old = runtime._extensions.ref.get()
        manifest = msgspec.structs.replace(
            old,
            generation=old.generation + 1,
            mcp={
                "ready": {
                    "connected": True,
                    "resources": [{"uri": "file:///safe/root"}],
                },
                "offline": {"connected": False, "resources": [{"uri": "file:///secret"}]},
            },
        )
        runtime._extensions.ref.swap(manifest)
        async def ready():
            return None

        runtime.ensure_started = ready
        result = await facade.handle(p.ContextInspect(session="indexes"))

        assert isinstance(result, p.ContextInspectResult)
        assert result.mcp_index and "ready" in result.mcp_index
        assert "offline" not in result.mcp_index and "file:///secret" not in result.mcp_index
        # The built-in catalog need not contain a skill body/tool candidate.
        assert "Skill tool" not in str(result.tools)
        assert all(not row["included"] for row in result.skills_index)
    finally:
        await runtime.aclose()


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
        "dev",
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


async def test_web_projection_keeps_ids_and_streams_small_text_appends():
    from nexus.host.facade import _json_patch

    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    handle = runtime.sessions.handle("s")
    # Seed a complete assistant message, then stream another long response in
    # two chunks. The patch for the second chunk should carry only its suffix.
    handle.events = [
        Event(type="turn.started", seq=1, session="s", turn="t"),
        Event(type="text.delta", data={"text": "hello"}, seq=2, session="s", turn="t", id="m"),
        Event(type="text.delta", data={"text": " world"}, seq=3, session="s", turn="t", id="m"),
    ]
    snapshot = facade.web_snapshot("s")
    message = snapshot["view"]["turns"][0]["messages"][0]
    assert snapshot["seq"] == 3
    assert message["id"] == "m"
    assert message["event_seq"] == 2

    # This isolates the wire-size property relied on for high-frequency model
    # deltas: the operation payload is the new fragment, never the whole text.
    before = {"turns": [{"messages": [{"id": "m", "blocks": [{"text": "x" * 100_000}]}]}]}
    after = {"turns": [{"messages": [{"id": "m", "blocks": [{"text": "x" * 100_000 + " tail"}]}]}]}
    ops = _json_patch(before, after)
    assert ops == [{"op": "append", "path": "/turns/0/messages/0/blocks/0/text", "value": " tail"}]
    assert len(msgspec.json.encode(ops)) < 100

    handle.events = [Event(type="turn.started", seq=1, session="s", turn="big")]
    handle.events.extend(
        Event(
            type="text.delta",
            data={"text": "z" * 10},
            seq=seq,
            session="s",
            turn="big",
        )
        for seq in range(2, 1002)
    )
    frames = [frame async for frame in facade.subscribe_web("s", from_seq=1)]
    assert len(frames) == 1000
    assert len(msgspec.json.encode(frames[0])) < 1000
    assert max(len(msgspec.json.encode(frame)) for frame in frames[1:]) < 512
    long_snapshot = facade.web_snapshot("s")
    assert len(long_snapshot["view"]["turns"][0]["messages"][0]["blocks"][0]["text"]) == 10_000


async def test_permission_targets_reach_web_snapshot_and_patch_without_rules():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    handle = runtime.sessions.handle("s")
    target = {
        "role": "destination",
        "path": "notes/today.md",
        "reason": "This file is outside the writable root.",
        "suggested_rule": "Write(notes/today.md)",
        "private_metadata": "must not be projected",
    }
    handle.events = [
        Event(
            type="permission.requested",
            data={"id": "p1", "tool": "Write", "targets": [target]},
            seq=1,
            session="s",
            turn="t",
        )
    ]

    snapshot = facade.web_snapshot("s")
    expected = [{key: target[key] for key in ("role", "path", "reason")}]
    assert snapshot["view"]["permissions"][0]["targets"] == expected
    assert "suggested_rule" not in str(snapshot)
    assert "private_metadata" not in str(snapshot)

    frames = [frame async for frame in facade.subscribe_web("s")]
    patch = str(frames[0]["ops"])
    assert "notes/today.md" in patch
    assert "This file is outside the writable root." in patch
    assert "suggested_rule" not in patch
    assert "private_metadata" not in patch


async def test_web_projection_accepts_nonconsecutive_event_sequences():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    handle = runtime.sessions.handle("s")
    # Session message/snapshot records consume sequence values without
    # producing event frames. Replaying from seq 1 must therefore accept seq 3.
    handle.events = [
        Event(type="turn.started", seq=1, session="s", turn="t"),
        Event(type="text.delta", data={"text": "hello"}, seq=3, session="s", turn="t", id="m"),
        Event(type="text.delta", data={"text": " world"}, seq=5, session="s", turn="t", id="m"),
    ]

    async def replay_with_duplicates(session_id, from_seq=0, *, follow=True, client_id=None):
        del session_id, follow, client_id
        for event in [handle.events[1], handle.events[1], handle.events[0], handle.events[2]]:
            yield event

    facade.subscribe = replay_with_duplicates

    frames = [frame async for frame in facade.subscribe_web("s", from_seq=1)]

    assert [frame["seq"] for frame in frames] == [3, 5]
    assert frames[0]["schema_version"] == 1
    assert frames[0]["session"] == "s"
    assert frames[0]["ops"]
    assert {"op": "append", "path": "/turns/0/messages/0/blocks/0/text", "value": " world"} in frames[1]["ops"]
    assert frames[-1]["seq"] == 5
    assert facade.web_snapshot("s")["seq"] == 5


async def test_web_projection_reuses_unchanged_history_branches():
    from nexus.host.facade import _web_view

    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    handle = runtime.sessions.handle("s")
    handle.events = []
    for turn in range(20):
        handle.events.extend(
            [
                Event(type="turn.started", seq=turn * 4 + 1, session="s", turn=f"t{turn}"),
                Event(type="text.delta", data={"text": f"answer-{turn}"}, seq=turn * 4 + 2, session="s", turn=f"t{turn}", id=f"m{turn}"),
                Event(type="turn.completed", seq=turn * 4 + 3, session="s", turn=f"t{turn}"),
            ]
        )
    before, _ = facade.state("s")
    before_wire = _web_view(before)
    after = apply(
        before,
        Event(type="turn.started", seq=81, session="s", turn="live"),
    )
    after = apply(
        after,
        Event(type="text.delta", data={"text": "tail"}, seq=82, session="s", turn="live", id="live-message"),
    )
    after_wire = _web_view(after, before, before_wire)

    assert after_wire["turns"][:-1] == before_wire["turns"]
    assert all(
        after_turn is before_turn
        for after_turn, before_turn in zip(after_wire["turns"][:-1], before_wire["turns"])
    )
    assert after_wire["messages"][:-1] == before_wire["messages"]
    assert all(
        after_message is before_message
        for after_message, before_message in zip(after_wire["messages"][:-1], before_wire["messages"])
    )


def test_web_projection_pairs_agent_wire_reuse_by_stable_id():
    from nexus.host.facade import _web_view
    from nexus.view.model import AgentView, ConversationView

    first = AgentView(id="first", description="unchanged")
    second = AgentView(id="second", description="must not reuse first")
    old = ConversationView(
        agents={"first": first, "second": second},
        agent_order=["first", "missing", "second"],
    )
    old_wire = _web_view(old)
    old_second_wire = old_wire["agents"][1]
    # Model an older/incomplete projection where an ordered agent lacks a wire
    # entry. Positional pairing would incorrectly reuse this branch for first.
    old_wire["agents"] = [old_second_wire]

    changed_first = AgentView(id="first", description="changed")
    new = ConversationView(
        agents={"first": changed_first, "second": second},
        agent_order=["first", "missing", "second"],
    )
    new_wire = _web_view(new, old, old_wire)

    assert [agent["id"] for agent in new_wire["agents"]] == ["first", "second"]
    assert new_wire["agents"][0] is not old_second_wire
    assert new_wire["agents"][0]["description"] == "changed"
    assert new_wire["agents"][1] is old_second_wire
    assert new_wire["agents"][1]["description"] == "must not reuse first"


def test_web_projection_only_preserves_unbounded_assistant_text():
    from nexus.host.facade import _web_view
    from nexus.view.model import (
        MAX_TEXT,
        BlockView,
        MessageView,
        PermissionView,
        ToolCallView,
    )

    long_text = "x" * (MAX_TEXT + 1)
    assistant = MessageView(role="assistant", blocks=[BlockView(kind="thinking", text=long_text)])
    user = MessageView(role="user", blocks=[BlockView(kind="text", text=long_text)])
    permission = PermissionView(preview=long_text)
    tool = ToolCallView(error=long_text)

    assert _web_view(assistant)["blocks"][0]["text"] == long_text
    assert _web_view(user)["blocks"][0]["text"] == long_text[:MAX_TEXT] + "…"
    assert _web_view(permission)["preview"] == long_text[:MAX_TEXT] + "…"
    assert _web_view(tool)["error"] == long_text[:MAX_TEXT] + "…"


async def test_web_workspace_feed_emits_session_index_changes():
    runtime = _FakeRuntime()
    facade = HostFacade(runtime)
    feed = facade.subscribe_workspace(interval=0.1)
    initial = await asyncio.wait_for(anext(feed), 1)
    assert initial["revision"] == 1
    assert initial["sessions"] == []
    runtime.sessions.handle("created-in-terminal")
    changed = await asyncio.wait_for(anext(feed), 1)
    assert changed["revision"] == 2
    assert changed["sessions"][0]["id"] == "created-in-terminal"
    await feed.aclose()


async def test_facade_doctor_aggregates_durable_registry_mismatches(tmp_path):
    """A recorded `registry.mismatch` is surfaced, redacted, by `doctor` (§15.5)."""
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("ok")))
    handle = runtime.session("probe")
    handle.append_message(Message(role="user", content=[Text(text="probe")]))
    handle.append_event(
        Event(
            type="registry.mismatch",
            data={
                "provider": "anthropic",
                "model": "claude-opus-5",
                "feature": "tools",
                "source": "provider-rejection",
                "detail": "Authorization: Bearer sk-secret1234567",
            },
            session="probe",
        )
    )
    facade = HostFacade(runtime)

    report = facade.doctor()
    summary = report["registry_mismatches"]
    assert summary["count"] == 1
    assert summary["by_provider"] == {"anthropic": 1}
    assert summary["samples"][0]["session"] == "probe"
    assert "sk-secret1234567" not in str(summary)
    assert summary["sessions_scanned"] == 1
    assert report["database"]["schema_user_version"] == SCHEMA_VERSION
    assert report["database"]["quick_check"] == "ok"
    assert report["database"]["size_bytes"] > 0
    await runtime.aclose()


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


async def test_facade_state_cache_applies_only_the_new_tail(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("one"), text_response("two")))
    facade = HostFacade(runtime)
    facade.open_session("s")

    await facade.start_turn("s", "first")
    await facade.wait_idle(timeout=5.0)
    first, _ = facade.state("s")
    assert facade.state("s")[0] is first  # nothing new: the cached fold is reused

    await facade.start_turn("s", "second")
    await facade.wait_idle(timeout=5.0)
    view, seq = facade.state("s")
    events = runtime.session("s").events
    assert view.to_dict() == fold(events).to_dict()
    assert seq == events[-1].seq and len(view.turns) == 2

    # A log that no longer matches the cache is folded from scratch.
    cached, count, last = facade._state_cache["s"]
    facade._state_cache["s"] = (cached, count, Event(type="x", seq=last.seq + 1000))
    assert facade.state("s")[0].to_dict() == fold(events).to_dict()
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
    queued_id, turn_id = await facade.enqueue("s", "two", mode="queue")
    assert queued_id and turn_id
    await facade.wait_idle(timeout=5.0)

    handle = runtime.session("s")
    kinds = [event.type for event in handle.events]
    queued = [event for event in handle.events if event.type == "input.queued"]
    consumed = [event for event in handle.events if event.type == "input.consumed"]
    assert len(queued) == len(consumed) == 1
    assert queued[0].data["queued_id"] == consumed[0].data["queued_id"]
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
    """Detaching a view after a force-delete must not recreate the session."""
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("bye")))
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.start_turn("s", "hi")
    await facade.wait_idle(timeout=5.0)
    store = runtime.sessions.store
    view = facade.subscribe("s", 0, follow=True, client_id="view")
    await view.__anext__()
    assert facade.presence.viewers("s") == 1
    records_before_delete = store.read("s").records

    record = facade.delete("s", force=True)
    assert not runtime.sessions.exists("s")
    assert store.row_exists("s")
    assert store.read("s").records == records_before_delete

    # Closing the view runs presence cleanup; it must not recreate the session.
    await view.aclose()
    assert facade.presence.viewers("s") == 0
    assert not runtime.sessions.exists("s")
    assert store.row_exists("s")
    assert store.read("s").records == records_before_delete
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


@pytest.mark.parametrize("mode", ["queue", "steer", "interrupt", "default"])
async def test_messages_during_active_turn(tmp_path, mode):
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [MessageStart(model="m", provider="scripted"), TextDelta(text="working"),
         Wait(gate), MessageStop(stop_reason="stop")],
        text_response("followup"), text_response("queued response"),
    )
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        await facade.start_turn("s", "original")
        await wait_for(lambda: provider.calls == 1)
        await facade.enqueue("s", "later", mode="queue")
        command = (p.SessionEnqueue(session="s", content="direction") if mode == "default"
                   else p.SessionEnqueue(session="s", content="direction", mode=mode))
        if mode == "default":
            mode = "steer"
        result = await facade.handle(command)
        assert isinstance(result, p.SessionEnqueueResult)
        if mode != "interrupt":
            assert provider.calls == 1
            assert len(facade.state("s")[0].input_queue) == 2
            gate.set()
        await facade.wait_idle(timeout=5)
        handle = runtime.session("s")
        assert handle.queue_depth == 0
        kinds = [e.type for e in handle.events]
        assert kinds.count("turn.started") == 2
        assert kinds.count("turn.cancelled") == (1 if mode == "interrupt" else 0)
        texts = ["".join(b.text for b in m.content if isinstance(b, Text))
                 for m in handle.messages if m.role == "user"]
        assert texts == (["original", "later\n\ndirection"] if mode == "queue"
                         else ["original", "direction\n\nlater"] if mode == "interrupt"
                         else ["original", "direction", "later"])
        assert facade.state("s")[0].to_dict() == fold(handle.events).to_dict()
        assert all(t.phase != "failed" for t in facade.state("s")[0].turns)
    finally:
        await runtime.aclose()


async def test_cancel_returns_pending_messages_to_composer_in_order(tmp_path):
    gate = asyncio.Event()
    runtime = _runtime(tmp_path, ScriptedProvider([
        MessageStart(model="m", provider="scripted"), TextDelta(text="working"),
        Wait(gate), MessageStop(stop_reason="stop"),
    ]))
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        await facade.start_turn("s", "already sent")
        await wait_for(lambda: runtime.session("s").active)
        await facade.enqueue("s", "message 1")
        await facade.enqueue("s", "message 2")
        result = await facade.handle(p.SessionCancel(session="s", return_queue=True))
        assert result.returned_messages == ["message 1", "message 2"]
        assert result.cancelled and result.dropped == 2
        assert runtime.session("s").queue_depth == 0
        await facade.wait_idle(timeout=5)
        assert facade.state("s")[0].input_queue == []
        again = await facade.handle(p.SessionCancel(session="s", return_queue=True))
        assert again.returned_messages == []
        queued = [event for event in runtime.session("s").events if event.type == "input.queued"]
        assert [event.data["content"][0]["text"] for event in queued] == ["message 1", "message 2"]
    finally:
        await facade.supervisor.aclose()
        await runtime.aclose()


async def test_project_sessions_list_and_open_validate_recorded_workspace(tmp_path, monkeypatch):
    from nexus.session.db import SqliteSessionStore
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("answer")))
    facade = HostFacade(runtime)
    facade.open_session("same")
    runtime.session("same").append_message(Message(role="user", content=[Text(text="hi")]))
    other = tmp_path / "other"
    other.mkdir()
    store = SqliteSessionStore(runtime.sessions.store.db, "other", root=str(other))
    store.create("same")
    store.append_message("same", Message(role="user", content=[Text(text="hi")]))
    result = await facade.handle(p.ProjectSessionsList())
    assert isinstance(result, p.ProjectSessionsListResult)
    assert result.workspace == str(tmp_path)
    assert {(row.workspace, row.session.id) for row in result.sessions} == {
        (str(tmp_path), "same"), (str(other), "same"),
    }
    calls = []
    class Peer:
        async def call(self, command):
            calls.append(command)
            if isinstance(command, p.WebLaunch):
                return p.WebLaunchResult(url="http://127.0.0.1:3210/#ticket=secret")
            return SimpleNamespace()
        async def close(self):
            calls.append("closed")
    async def connect(workspace, **kwargs):
        assert workspace == str(other)
        return Peer()
    monkeypatch.setattr("nexus.host.daemon.ensure_daemon", connect)
    opened = await facade.handle(p.ProjectSessionOpen(workspace=str(other), session="same", browser=True))
    assert isinstance(opened, p.ProjectSessionOpenResult)
    assert opened.url == "http://127.0.0.1:3210/s/same#ticket=secret"
    assert calls[-1] == "closed"
    calls.clear()
    rejected = await facade.handle(p.ProjectSessionOpen(workspace="/unknown", session="same"))
    assert isinstance(rejected, p.ErrorResult)
    assert not calls
    await runtime.aclose()
