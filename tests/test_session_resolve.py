"""SessionResolve (plan 03 §3.1): name or id to identity, project scoped, lifecycle aware."""
from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ManagerSection, ModelSection
from nexus.errors import SessionError
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.message import Message, Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.session.db import SqliteSessionStore
from manager_helpers import MANAGER_ID, open_manager


def build(tmp_path):
    config = Config(
        model="scripted/m", version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m"), manager=ManagerSection(enabled=True)),
    )
    runtime = Runtime(
        tmp_path, home=tmp_path / "home", config=config,
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    return runtime, HostFacade(runtime)


def persisted(runtime, facade, session_id):
    facade.open_session(session_id)
    runtime.session(session_id).append_message(Message(role="user", content=[Text(text="hi")]))


async def resolve(facade, ref, **kwargs):
    result = await facade.handle(p.SessionResolve(ref=ref, **kwargs))
    return result


async def test_name_and_id_resolve_to_the_same_identity(tmp_path):
    runtime, facade = build(tmp_path)
    persisted(runtime, facade, "alpha")
    name = runtime.sessions.store.session_row("alpha")["name"]
    by_id = (await resolve(facade, "alpha")).resolution
    by_name = (await resolve(facade, name)).resolution
    assert by_id["logical_id"] == by_name["logical_id"] == "alpha"
    assert by_name["name"] == name and by_name["kind"] == "root"
    assert by_name["state"] == "idle" and by_name["trashed"] is False
    assert by_name["project"] and by_name["retired"] is False
    listed = {s.id: s.name for s in runtime.sessions.list()}
    assert listed["alpha"] == name
    await runtime.aclose()


async def test_unknown_and_other_project_refs_are_refused(tmp_path):
    runtime, facade = build(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = SqliteSessionStore(runtime.sessions.store.db, "other", root=str(other_root))
    other.create("foreign")
    foreign_name = other.session_row("foreign")["name"]
    for ref in ("missing", "foreign", foreign_name):
        result = await resolve(facade, ref)
        assert isinstance(result, p.ErrorResult), ref
    await runtime.aclose()


async def test_trashed_is_flagged_and_purged_is_retired(tmp_path):
    runtime, facade = build(tmp_path)
    persisted(runtime, facade, "gone")
    name = runtime.sessions.store.session_row("gone")["name"]
    await facade.handle(p.SessionDelete(session="gone"))
    trashed = (await resolve(facade, name)).resolution
    assert trashed["trashed"] is True and trashed["state"] == "trashed"
    runtime.sessions.store.db._connection()  # keep the connection warm for the raw purge
    row = runtime.sessions.store.trashed_rows()[0]
    runtime.sessions.store.purge_expired(now=row["trash_expires_at"] + 1)
    assert isinstance(await resolve(facade, name), p.ErrorResult)
    retired_result = await resolve(facade, name, include_retired=True)
    retired = retired_result.resolution
    assert retired["retired"] is True and retired["state"] == "retired" and retired["name"] == name
    await runtime.aclose()


async def test_the_manager_is_not_resolvable_by_name_and_names_cannot_bypass_refusals(tmp_path):
    runtime, facade = build(tmp_path)
    open_manager(runtime.sessions)
    name = runtime.sessions.store.session_row(MANAGER_ID)["name"]
    with pytest.raises(SessionError):
        runtime.sessions.resolve(name)
    assert runtime.sessions.resolve(MANAGER_ID)["kind"] == "manager"
    # A name for the manager is left untouched by dispatch, so the manager refusals still apply.
    for command in (p.SessionDelete(session=name), p.SessionArchive(session=name)):
        assert isinstance(await facade.handle(command), p.ErrorResult)
    assert runtime.sessions.store.session_row(MANAGER_ID)["trash_id"] is None
    assert isinstance(await facade.handle(p.SessionFork(session="s", new_id=MANAGER_ID)), p.ErrorResult)
    await runtime.aclose()
