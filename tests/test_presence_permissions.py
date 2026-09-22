"""Phase 3.5 H4: derived attendance and per-session unattended approvals.

Presence is a subscriber count and attendance is derived from it (plan section
14.6). These tests cover the approval consequences: a drop to zero viewers
applies the session's unattended decision to any pending request *and* to a
request that arrives only after the last viewer has already left (the mid-turn
disconnect race); the last of several viewers is the trigger; and the first
resolver of a permission request wins whether it comes from a raw
``resolve_permission`` call or the attached ``send`` compatibility path.

The decision is settable per session (``Session.bind``/``SessionManager``), so
these tests pin the mechanism the integration packet feeds from
``permissions.on_unattended``. All providers are offline scripts.
"""
from __future__ import annotations

import asyncio

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)
from nexus.runtime import Runtime
from nexus.tools.permissions import Decision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(*, mode="ask", unattended="deny") -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode=mode, on_unattended=unattended),
            tools=ToolsSection(),
        ),
    )


def make_runtime(tmp_path, provider, **config_kwargs) -> Runtime:
    return Runtime(
        tmp_path,
        config=make_config(**config_kwargs),
        providers={"scripted": provider},
    )


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


async def wait_until_idle(session, timeout=3.0):
    """Bounded, cancellable idle wait so a hang is a failure, not a timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.active or session.queue_depth:
        if loop.time() > deadline:
            raise AssertionError("session did not settle within the timeout")
        await asyncio.sleep(0.001)


async def attach_viewer(session):
    stream = session.subscribe(0)
    await stream.__anext__()
    return stream


def permission_resolutions(session):
    return [e for e in session.events if e.type == "permission.resolved"]


def parked_write_script(release, path="b.txt", content="B"):
    """Persist some state, park, then request one ``Write`` approval."""
    return [
        MessageStart(model="m", provider="scripted"),
        TextDelta(text="working"),
        Wait(event=release),
        ToolCallStart(id="c1", name="Write"),
        ToolCallEnd(id="c1", input={"path": path, "content": content}),
        MessageStop(stop_reason="tool_use"),
    ]


async def start_pending_write(session, text="go"):
    turn_id = await session.start_turn(text)
    await wait_for(lambda: session.pending_permissions)
    return turn_id


# ---------------------------------------------------------------------------
# Drop to zero: default policy, ordering race, and configured decision
# ---------------------------------------------------------------------------


async def test_last_viewer_leaving_resolves_pending_approval(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("presence-deny")
    stream = await attach_viewer(session)

    await start_pending_write(session)
    assert session.pending_permissions

    await stream.aclose()
    assert session.viewers == 0
    await wait_until_idle(session)

    resolved = permission_resolutions(session)
    assert resolved and resolved[-1].data["decision"] == "deny_once"
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


async def test_viewer_drop_before_the_request_does_not_hang(tmp_path):
    # The last viewer disconnects *before* the approval is requested. Fallback
    # only runs on the disconnect edge, so the request would otherwise park the
    # turn forever with nobody to answer it.
    release = asyncio.Event()
    provider = ScriptedProvider(
        parked_write_script(release), text_response("done")
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("presence-race")
    stream = await attach_viewer(session)

    turn_id = await session.start_turn("go")
    await wait_for(
        lambda: any(
            e.type == "text.delta" and e.turn == turn_id
            for e in session.events
        )
    )
    await stream.aclose()
    assert session.viewers == 0
    assert session.pending_permissions == ()  # not requested yet

    release.set()
    await wait_until_idle(session)  # bounded: a regression would fail, not hang
    assert session.active is False
    assert session.events[-1].type == "turn.completed"

    resolved = permission_resolutions(session)
    assert resolved and resolved[-1].data["decision"] == "deny_once"
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


async def test_configured_allow_decision_is_applied_on_drop(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("presence-allow")
    session.bind(unattended_decision=Decision.ALLOW_ONCE)
    stream = await attach_viewer(session)

    await start_pending_write(session)
    await stream.aclose()
    await wait_until_idle(session)

    resolved = permission_resolutions(session)
    assert resolved and resolved[-1].data["decision"] == "allow_once"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"
    await runtime.aclose()


async def test_configured_deny_decision_is_applied_on_drop(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("presence-configured-deny")
    session.bind(unattended_decision="deny_once")
    stream = await attach_viewer(session)

    await start_pending_write(session)
    await stream.aclose()
    await wait_until_idle(session)

    resolved = permission_resolutions(session)
    assert resolved and resolved[-1].data["decision"] == "deny_once"
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


async def test_one_of_two_viewers_leaving_keeps_the_approval_pending(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("presence-two")
    first = await attach_viewer(session)
    second = await attach_viewer(session)
    assert session.viewers == 2

    await start_pending_write(session)
    request_id = session.pending_permissions[0]

    await first.aclose()
    assert session.viewers == 1
    assert session.attended is True
    assert session.pending_permissions == (request_id,)

    await second.aclose()
    assert session.viewers == 0
    await wait_until_idle(session)
    assert not session.pending_permissions
    await runtime.aclose()


# ---------------------------------------------------------------------------
# First resolver wins
# ---------------------------------------------------------------------------


async def test_first_resolver_wins_repeatedly(tmp_path):
    provider = ScriptedProvider(
        *[
            script
            for i in range(8)
            for script in (
                tool_response(
                    ("c1", "Write", {"path": f"f{i}.txt", "content": "X"})
                ),
                text_response("done"),
            )
        ]
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("first-wins")
    stream = await attach_viewer(session)

    for _ in range(8):
        await start_pending_write(session)
        request_id = session.pending_permissions[0]

        async def resolve(decision, rid=request_id):
            await asyncio.sleep(0)  # let the sibling race this one
            return session.resolve_permission(rid, decision)

        results = await asyncio.gather(
            resolve(Decision.ALLOW_ONCE), resolve(Decision.DENY_ONCE)
        )
        assert sorted(results) == [False, True], "first resolver did not win"
        await wait_until_idle(session)

    assert len(permission_resolutions(session)) == 8
    await stream.aclose()
    await runtime.aclose()


async def test_first_resolver_wins_through_send_compat(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("send-first-wins")
    session.mark_attended(True)

    outcomes = []
    async for event in session.send("go"):
        if event.type == "permission.requested":
            request_id = event.data["id"]
            outcomes.append(
                (
                    session.resolve_permission(request_id, Decision.ALLOW_ONCE),
                    session.resolve_permission(request_id, Decision.DENY_ONCE),
                )
            )

    assert outcomes == [(True, False)]
    assert len(permission_resolutions(session)) == 1
    assert session.events[-1].type == "turn.completed"
    await runtime.aclose()


async def test_stale_or_unknown_request_ids_return_false(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("stale")
    session.mark_attended(True)

    request_id = None
    async for event in session.send("go"):
        if event.type == "permission.requested" and request_id is None:
            request_id = event.data["id"]
            assert session.resolve_permission(request_id, Decision.ALLOW_ONCE)

    assert request_id is not None
    # The same id cannot resolve twice, and an unknown id never resolves.
    assert session.resolve_permission(request_id, Decision.DENY_ONCE) is False
    assert session.resolve_permission("does-not-exist", Decision.ALLOW_ONCE) is False
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Presence bookkeeping
# ---------------------------------------------------------------------------


async def test_presence_count_tracks_each_join_and_leave(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("x")))
    session = runtime.session("presence-count")
    assert session.viewers == 0
    assert session.attended is False

    first = session.subscribe(0, follow=False)
    await first.__anext__()
    second = session.subscribe(0, follow=False)
    await second.__anext__()
    assert session.viewers == 2
    assert session.attended is True

    await first.aclose()
    assert session.viewers == 1
    assert session.attended is True

    await second.aclose()
    assert session.viewers == 0
    assert session.attended is False

    kinds = [e.type for e in session.events]
    assert kinds.count("presence.joined") == 2
    assert kinds.count("presence.left") == 2
    await runtime.aclose()
