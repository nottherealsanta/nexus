"""Manager host surface (plan 02 M9, host half): config, singleton session, lists, refusals, Live, events."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.config.layers import build_v2, env_overlay_v2
from nexus.config.schema import ConfigV2, ManagerSection, ModelSection
from nexus.errors import ConfigError, SessionError
from nexus.events import EVENT_GROUPS, EVENT_TYPES, MANAGER_EVENTS, Event
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.host_support import live as live_support
from nexus.host_support import live_consent as lc
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.observability.session import read_session_page
from nexus.runtime import Runtime
from nexus.session.records import EventRecord
from nexus.view import fold
from manager_helpers import MANAGER_ID, managers, open_manager, user


def config(enabled: bool) -> Config:
    return Config(
        model="scripted/m", version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m"), manager=ManagerSection(enabled=enabled)),
    )


def build(tmp_path, enabled: bool = True):
    runtime = Runtime(
        tmp_path, home=tmp_path / "home", config=config(enabled),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    return runtime, HostFacade(runtime)


# -- config ----------------------------------------------------------------------------


def test_manager_config_is_off_by_default_with_the_planned_caps():
    section = build_v2({}).manager
    assert section == ManagerSection()
    assert (section.enabled, section.max_launches_per_turn, section.max_live_managed) == (False, 3, 8)


def test_manager_config_validates_and_reads_the_environment():
    assert build_v2({"manager": {"enabled": True, "max_live_managed": 4}}).manager.max_live_managed == 4
    for bad in ({"max_launches_per_turn": 0}, {"max_live_managed": 51}, {"unknown": 1}, {"enabled": "yes"}):
        with pytest.raises(ConfigError):
            build_v2({"manager": bad})
    overlay = env_overlay_v2({"NEXUS_MANAGER__ENABLED": "true", "NEXUS_MANAGER__MAX_LIVE_MANAGED": "4"})
    assert overlay == {"manager": {"enabled": True, "max_live_managed": 4}}


# -- the singleton session ------------------------------------------------------------------


async def test_manager_open_is_refused_while_disabled_but_state_still_answers(tmp_path, monkeypatch):
    runtime, facade = build(tmp_path, enabled=False)
    try:
        async def nobody(_runtime, _provider):
            return False

        monkeypatch.setattr("nexus.host.facade.provider_connected", nobody)
        refused = await facade.handle(p.ManagerOpen())
        assert isinstance(refused, p.ErrorResult) and "turned off" in refused.message
        state = await facade.handle(p.ManagerState())
        assert isinstance(state, p.ManagerStateResult)
        assert not state.enabled and state.session == "" and state.managed == []
        assert state.model == "codex/gpt-6-luna"
        assert not state.signed_in and "Settings" in state.sign_in
        assert not runtime.sessions.store.row_exists(MANAGER_ID)
    finally:
        await runtime.aclose()


async def test_manager_open_is_idempotent_and_creates_one_manager_kind_session(tmp_path):
    runtime, facade = build(tmp_path)
    try:
        first = await facade.handle(p.ManagerOpen())
        second = await facade.handle(p.ManagerOpen())
        assert isinstance(first, p.ManagerOpenResult) and first.created
        assert isinstance(second, p.ManagerOpenResult) and not second.created
        assert first.session.id == second.session.id == MANAGER_ID
        assert first.session.kind == "manager" and second.session.kind == "manager"
        assert runtime.session(MANAGER_ID).kind == "manager"
        assert runtime.manager_port_factory is not None  # the facade injects the port builder
        port = runtime.manager_port_factory(runtime.session(MANAGER_ID))
        assert type(port).__name__ == "HostManagerPort"
    finally:
        await runtime.aclose()


async def test_the_manager_id_is_reserved_and_a_disabled_manager_is_unreachable(tmp_path):
    runtime, facade = build(tmp_path)
    try:
        for command in (
            p.SessionOpen(session=MANAGER_ID),
            p.SessionStart(session=MANAGER_ID, content="hi"),
            p.SessionFork(session="s", new_id=MANAGER_ID),
        ):
            result = await facade.handle(command)
            assert isinstance(result, p.ErrorResult) and "reserved" in result.message, command
        await facade.handle(p.ManagerOpen())
        runtime.session(MANAGER_ID).append_message(user("hello"))
    finally:
        await runtime.aclose()
    runtime, facade = build(tmp_path, enabled=False)  # same workspace and home, manager now off
    try:
        for command in (
            p.SessionState(session=MANAGER_ID),
            p.SessionEnqueue(session=MANAGER_ID, content="more"),
        ):
            result = await facade.handle(command)
            assert isinstance(result, p.ErrorResult) and "turned off" in result.message, command
    finally:
        await runtime.aclose()


async def test_enqueue_to_a_forgotten_manager_draft_recreates_the_manager(tmp_path):
    runtime, facade = build(tmp_path)
    try:
        assert isinstance(await facade.handle(p.ManagerOpen()), p.ManagerOpenResult)
    finally:
        await runtime.aclose()  # the draft never got a message, so it is gone on restart
    runtime, facade = build(tmp_path)
    try:
        assert not runtime.sessions.exists(MANAGER_ID)
        result = await facade.handle(p.SessionEnqueue(session=MANAGER_ID, content="hi"))
        assert isinstance(result, p.SessionEnqueueResult), result
        assert runtime.sessions.kind_of(MANAGER_ID) == "manager"
    finally:
        await runtime.aclose()
    runtime, facade = build(tmp_path, enabled=False)
    try:
        result = await facade.handle(p.SessionEnqueue(session="nexus-manager", content="x"))
        assert isinstance(result, p.ErrorResult)
    finally:
        await runtime.aclose()


async def test_manager_state_reports_sign_in_consent_and_managed_roots(tmp_path, monkeypatch):
    runtime, facade = build(tmp_path)
    try:
        connected = {"codex": False}
        asked: list[str] = []

        async def lookup(_runtime, provider):
            asked.append(provider)
            return connected[provider]  # a Copilot lookup would raise KeyError: it is not required

        monkeypatch.setattr("nexus.host.facade.provider_connected", lookup)
        await facade.handle(p.ManagerOpen())
        manager = runtime.session(MANAGER_ID)
        manager.append_message(user("go"))
        manager.emit_session_event("session.linked", {"kind": "managed", "child": "mgr-1", "turn": "t1"})
        manager.emit_session_event("session.linked", {"kind": "managed", "child": "mgr-2", "turn": "t1"})
        state = await facade.handle(p.ManagerState())
        assert state.enabled and state.session == MANAGER_ID and state.managed == ["mgr-1", "mgr-2"]
        assert state.model == "codex/gpt-6-luna" and state.provider == "codex"
        assert not state.signed_in and "Codex" in state.sign_in and "Copilot" not in state.sign_in
        assert not state.codex_signed_in and not state.live_ready
        assert (state.max_launches_per_turn, state.max_live_managed) == (3, 8)
        connected["codex"] = True
        lc.write_consent(True, tmp_path / "home", "manager")
        ready = await facade.handle(p.ManagerState())
        assert ready.signed_in and ready.sign_in == "" and ready.live_manager_consent and ready.live_ready
        assert set(asked) == {"codex"}
    finally:
        await runtime.aclose()


async def test_the_manager_is_never_listed_and_refuses_archive_delete_fork_and_friends(tmp_path):
    runtime, facade = build(tmp_path)
    try:
        await facade.handle(p.ManagerOpen())
        runtime.session(MANAGER_ID).append_message(user("go"))
        plain = runtime.session("plain")
        plain.append_message(user("normal work"))
        assert [s.id for s in facade.list_sessions()] == ["plain"]
        listed = await facade.handle(p.SessionList())
        assert [s.id for s in listed.sessions] == ["plain"]
        projects = await facade.handle(p.ProjectSessionsList())
        assert [row.session.id for row in projects.sessions] == ["plain"]
        assert runtime.sessions.manager_summary().id == MANAGER_ID

        refusals = [
            (p.SessionArchive(session=MANAGER_ID), "archived"),
            (p.SessionDelete(session=MANAGER_ID), "deleted"),
            (p.SessionFork(session=MANAGER_ID), "forked"),
            (p.SessionShell(session=MANAGER_ID, command="ls"), "shell"),
            (p.ModelSelect(session=MANAGER_ID, ref="scripted/m"), "model"),
            (p.ReasoningEffortSelect(session=MANAGER_ID, effort="high"), "effort"),
            (p.AgentSelect(session=MANAGER_ID, name="build"), "role"),
            (p.AgentReset(session=MANAGER_ID), "role"),
            (p.SessionHandoff(session=MANAGER_ID), "handoff"),
            (p.ContextExtensionSelect(session=MANAGER_ID, category="skills", name="x", enabled=True), "skills"),
            (p.ContextMcpLoadingSelect(session=MANAGER_ID, server="x", mode="all"), "MCP"),
        ]
        for command, word in refusals:
            result = await facade.handle(command)
            assert isinstance(result, p.ErrorResult), command
            assert "manager" in result.message.lower() and word in result.message, (command, result.message)
        assert runtime.sessions.store.row_exists(MANAGER_ID)
        assert runtime.sessions.manager_summary() is not None
    finally:
        await runtime.aclose()


# -- storage -------------------------------------------------------------------------------


def test_storage_excludes_the_manager_everywhere_a_regular_list_reads(tmp_path):
    root, agents = managers(tmp_path)
    assert root.manager_summary() is None
    manager = open_manager(root)
    plain = root.open("plain")
    plain.append_message(user("normal work"))
    assert root.manager_summary().kind == "manager" and root.kind_of("plain") == "root"
    assert [s.id for s in root.list()] == ["plain"]
    assert [s.id for s in root.list(include_archived=True)] == ["plain"]
    assert [row["id"] for row in root.store.db.project_sessions()] == ["plain"]
    assert root.store.db.archived_project_sessions() == []
    assert root.store.stale_candidates(threshold=time.time() + 10, limit=50) == ["plain"]
    for action in (root.archive, root.delete, root.fork):
        with pytest.raises(SessionError, match="manager"):
            action(MANAGER_ID)
    assert manager.kind == "manager" and plain.kind == "root"
    root.close_all(); agents.close_all()


def test_a_draft_manager_is_found_before_its_first_message(tmp_path):
    root, agents = managers(tmp_path)
    root.open(MANAGER_ID, recover=False, ownership={"kind": "manager"})
    summary = root.manager_summary()
    assert summary is not None and summary.id == MANAGER_ID and summary.kind == "manager"
    assert root.list() == []
    root.close_all(); agents.close_all()


def test_the_manager_session_runs_no_hooks_and_gets_no_automatic_title(tmp_path):
    from nexus.host_support.auto_title import AutoTitler

    root, agents = managers(tmp_path)
    manager = open_manager(root)
    plain = root.open("plain")
    manager.bind(hooks=lambda: "runner")
    plain.bind(hooks=lambda: "runner")
    assert manager._hook_runner() is None and plain._hook_runner() == "runner"

    (tmp_path / "t").mkdir()
    empty_manager = managers(tmp_path / "t")[0]
    empty_manager.open(MANAGER_ID, recover=False, ownership={"kind": "manager"})
    runtime = SimpleNamespace(
        sessions=empty_manager, router=object(),
        _load_config=lambda: SimpleNamespace(v2=SimpleNamespace(
            sessions=SimpleNamespace(auto_title=True, title_model="openai/gpt-5-mini"))),
    )
    titler = AutoTitler(runtime)
    assert titler.candidate(MANAGER_ID, "start a session") is None
    assert titler.candidate("fresh-root", "start a session") == "start a session"
    root.close_all(); agents.close_all(); empty_manager.close_all()


# -- Live ------------------------------------------------------------------------------------


def test_live_consent_has_a_separate_cross_session_scope(tmp_path):
    assert lc.read_consent(tmp_path) == "" and lc.read_consent(tmp_path, "manager") == ""
    base = lc.write_consent(True, tmp_path)
    assert lc.read_consent(tmp_path) == base and lc.read_consent(tmp_path, "manager") == ""
    manager = lc.write_consent(True, tmp_path, "manager")
    assert lc.read_consent(tmp_path, "manager") == manager and lc.read_consent(tmp_path) == base
    lc.write_consent(True, tmp_path)  # re-accepting the base notice keeps the manager record
    assert lc.read_consent(tmp_path, "manager") == manager
    assert lc.write_consent(False, tmp_path, "manager") == ""
    assert lc.read_consent(tmp_path, "manager") == "" and lc.read_consent(tmp_path) != ""
    lc.write_consent(True, tmp_path, "manager")
    lc.write_consent(False, tmp_path)  # the base revocation takes the manager scope with it
    assert lc.read_consent(tmp_path) == "" and lc.read_consent(tmp_path, "manager") == ""
    with pytest.raises(ValueError):
        lc.write_consent(True, tmp_path, "other")


def test_a_call_on_the_manager_needs_it_enabled_and_the_cross_session_consent(tmp_path):
    def facade(kind, enabled=True):
        return SimpleNamespace(
            runtime=SimpleNamespace(sessions=SimpleNamespace(kind_of=lambda _session: kind)),
            manager_config=lambda: ManagerSection(enabled=enabled),
        )

    refusal = live_support._manager_refusal
    assert refusal(facade("root"), "s", tmp_path).kind == "live_manager_required"
    disabled = refusal(facade("manager", enabled=False), "m", tmp_path)
    assert disabled.kind == "manager_disabled"
    lc.write_consent(True, tmp_path)
    missing = refusal(facade("manager"), "m", tmp_path)
    assert missing.kind == "live_consent_required" and "other sessions" in missing.message
    lc.write_consent(True, tmp_path, "manager")
    assert refusal(facade("manager"), "m", tmp_path) is None


async def test_live_consent_command_round_trips_the_scope(tmp_path):
    runtime = SimpleNamespace(_home=tmp_path)
    facade = SimpleNamespace(runtime=runtime)
    wrote = await live_support.dispatch_live(p.LiveConsent(accept=True, scope="manager"), facade)
    assert isinstance(wrote, p.LiveConsentResult) and wrote.accepted and wrote.scope == "manager"
    base = await live_support.dispatch_live(p.LiveConsent(), facade)
    assert base.accepted and base.scope == ""
    gone = await live_support.dispatch_live(p.LiveConsent(accept=False, scope="manager"), facade)
    assert not gone.accepted
    assert (await live_support.dispatch_live(p.LiveConsent(), facade)).accepted


def test_the_live_host_names_the_manager_voice_persona(tmp_path):
    root, agents = managers(tmp_path)
    open_manager(root)
    facade = SimpleNamespace(runtime=SimpleNamespace(sessions=root))
    assert live_support.FacadeLiveHost(facade).agent_name(MANAGER_ID) == "Manager"
    root.close_all(); agents.close_all()


# -- events, view and logs -------------------------------------------------------------------


def test_manager_events_are_a_catalogue_group_and_session_linked_is_a_session_event():
    assert EVENT_GROUPS["manager"] == MANAGER_EVENTS
    assert set(MANAGER_EVENTS) <= EVENT_TYPES and "session.linked" in EVENT_GROUPS["session"]


def test_the_reducer_treats_manager_bookkeeping_as_metadata_and_labels_manager_origin():
    events = [
        Event(type="session.linked", data={"kind": "managed", "child": "c"}, seq=1),
        Event(type="manager.wait", data={"ids": ["c"], "outcome": "timeout"}, seq=2),
        Event(type="manager.launch_refused", data={"reason": "cap"}, seq=3),
        Event(type="manager.result_read", data={"session": "c", "bytes": 3}, seq=4),
        Event(type="manager.cancelled", data={"session": "c"}, seq=5),
        Event(type="input.queued", data={
            "queued_id": "q1", "content": [{"type": "text", "text": "fix it"}], "queue_depth": 1,
            "origin": {"kind": "manager", "manager": MANAGER_ID, "origin_message_id": "m1", "junk": "x"},
        }, seq=6),
    ]
    view = fold(events)
    assert view.diagnostics == [] and view.errors == []
    assert view.input_queue[0].origin == {
        "kind": "manager", "manager": MANAGER_ID, "origin_message_id": "m1",
    }


def test_session_logs_whitelist_manager_events_without_their_data():
    records = [
        EventRecord(seq=index, event=Event(type=name, data={"secret": "sk-leak", "ids": ["c"]}, seq=index), ts=float(index))
        for index, name in enumerate(("session.linked", *MANAGER_EVENTS), start=1)
    ]
    page = read_session_page(records, cursor=None, limit=20)
    kinds = [entry["kind"] for entry in page["entries"]]
    assert kinds == ["session.linked", *MANAGER_EVENTS]
    assert "sk-leak" not in str(page)
    assert {entry["level"] for entry in page["entries"]} <= {"info", "warning"}
