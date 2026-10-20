"""Native Live is always on the manager: persistent panel, approvals anywhere, focus reports (plan 02 section 10)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import msgspec
import pytest

from nexus.client.protocol import FacadeError
from nexus.ui.ratatui.focus import HEARTBEAT_SECONDS, Focus
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import PermissionView

from tests.test_ratatui_live import (  # noqa: F401 - the fixture and helpers are shared
    FakeCall, call_events, fold, settle, shell as live_shell,
)

MANAGER = "nexus-manager"


@pytest.fixture
def shell(live_shell):
    """The live shell, but the manager is a different session from the one on screen."""
    client = live_shell.client
    client.manager_state.return_value = msgspec.structs.replace(client.manager_state.return_value, session=MANAGER)
    client.manager_open.return_value = SimpleNamespace(session=SimpleNamespace(id=MANAGER), created=False)
    live_shell.controller.session = "work"
    live_shell.controller.view = initial_state("work")
    return live_shell


def ask(view, **fields):
    view.permissions.append(PermissionView(id="p1", ts=5.0, tool="bash", key="ls -la", **fields))


async def test_live_attaches_to_the_manager_and_the_foreground_stays(shell):
    await shell.command("/live", ())
    assert FakeCall.instances[0].session == MANAGER
    assert shell.controller.session == "work" and shell.live.session == MANAGER
    bar = shell.live.bar()
    assert bar["manager"] is True and bar["elsewhere"] == "Manager" and bar["attention"] == ""
    assert shell.live.follow.session == MANAGER, "the panel subscribes to the manager itself"
    await shell.live.shutdown()
    assert shell.live.follow.session == "" and shell.live.state == "idle"


async def test_the_pane_keeps_the_calls_rows_while_another_session_is_in_front(shell):
    await shell.command("/live", ())
    shell.live.follow.view = fold(call_events(ended=False))  # what the manager subscription folded
    snapshot = project(shell.controller, 1, shell=shell, literal=False)
    assert "Sent to agent (queue)" in snapshot["live"]["detail"]
    assert snapshot["live"]["operation"] == {"kind": "live_toggle", "id": "c1"}
    assert not [b for b in snapshot["blocks"] if b["kind"] == "live"], "the foreground timeline is not touched"
    await shell.live.shutdown()


async def test_a_manager_approval_reaches_the_user_in_another_session(shell):
    await shell.command("/live", ())
    ask(shell.live.follow.view)
    assert shell.live.bar()["attention"] == "approval waiting"
    prompt = project(shell.controller, 1, shell=shell, literal=False)["prompt"]
    assert prompt["kind"] == "permission" and prompt["id"] == "p1"
    assert prompt["session"] == MANAGER, "the answer is routed to the manager session"
    assert prompt["lines"][0].startswith("Manager · ") and prompt["title"].startswith("Manager · ")
    assert [c["value"] for c in prompt["choices"] if not c["disabled"]]
    await shell.live.shutdown()


async def test_the_foregrounds_own_prompt_wins_and_nothing_shows_without_a_call(shell):
    ask(shell.live.follow.view)  # no call: the followed view is not offered
    assert project(shell.controller, 1, shell=shell, literal=False)["prompt"] is None
    await shell.command("/live", ())
    ask(shell.live.follow.view)
    ask(shell.controller.view)
    own = project(shell.controller, 2, shell=shell, literal=False)["prompt"]
    assert own is not None and "session" not in own
    await shell.live.shutdown()


async def test_with_the_manager_in_front_its_own_view_owns_the_prompt(shell):
    await shell.command("/live", ())
    shell.controller.session, shell.controller.view = MANAGER, initial_state(MANAGER)
    ask(shell.controller.view)
    prompt = project(shell.controller, 1, shell=shell, literal=False)["prompt"]
    assert prompt["id"] == "p1" and "session" not in prompt
    assert shell.live.bar()["attention"] == "" and shell.live.bar()["elsewhere"] == ""
    await shell.live.shutdown()


async def test_the_composer_still_sends_to_the_displayed_session(shell):
    shell.client.enqueue = AsyncMock()
    await shell.command("/live", ())
    await shell.submit("hello there")
    shell.client.enqueue.assert_awaited_once()
    assert shell.client.enqueue.await_args.args[:2] == ("work", "hello there")
    await shell.live.shutdown()


# -- focus reporting -----------------------------------------------------------------------


def focus_shell(**client):
    return SimpleNamespace(client=SimpleNamespace(**client))


async def test_a_foreground_change_is_reported_with_the_client_id():
    reports = AsyncMock(return_value=SimpleNamespace(ok=True))
    focus = Focus(focus_shell(session_focus=reports), client_id="native-1")
    focus.report("a")
    await settle(lambda: focus.sent == "a")
    reports.assert_awaited_once_with("a", "native-1")
    focus.report("b")
    await settle(lambda: focus.sent == "b")
    assert [call.args for call in reports.await_args_list] == [("a", "native-1"), ("b", "native-1")]
    assert focus.supported is True and focus.error == ""


async def test_rapid_changes_send_the_latest_and_never_block_the_caller():
    gate = asyncio.Event()
    seen = []

    async def slow(session, client_id):
        seen.append(session)
        await gate.wait()

    focus = Focus(focus_shell(session_focus=slow))
    focus.report("a")
    focus.report("b")
    focus.report("c")  # all return immediately
    gate.set()
    await settle(lambda: focus.sent == "c")
    assert seen[-1] == "c" and len(seen) <= 3, "changes coalesce; the latest session is what the host ends up with"


async def test_heartbeat_resends_a_stale_claim_and_only_then():
    reports = AsyncMock()
    focus = Focus(focus_shell(session_focus=reports), client_id="c")
    focus.report("a")
    await settle(lambda: focus.sent == "a")
    focus.tick()
    await asyncio.sleep(0.02)
    assert reports.await_count == 1, "a fresh claim is not resent"
    focus.sent_at = time.monotonic() - HEARTBEAT_SECONDS - 1
    focus.tick()
    await settle(lambda: reports.await_count == 2)


async def test_an_unknown_command_switches_reporting_off_and_other_errors_retry():
    unknown = AsyncMock(side_effect=FacadeError("unknown_command", "no such command"))
    focus = Focus(focus_shell(session_focus=unknown))
    focus.report("a")
    await settle(lambda: focus.supported is False)
    focus.report("b")
    focus.tick()
    await asyncio.sleep(0.02)
    assert unknown.await_count == 1

    flaky = AsyncMock(side_effect=[FacadeError("busy", "try later"), None])
    focus = Focus(focus_shell(session_focus=flaky))
    focus.report("a")
    await settle(lambda: focus.error != "")
    assert focus.supported is None and focus.sent == ""
    focus.tick()
    await settle(lambda: focus.sent == "a")
    assert focus.error == "" and focus.supported is True


async def test_a_client_without_the_command_is_quietly_left_alone():
    focus = Focus(focus_shell())
    focus.report("a")
    await settle(lambda: focus.supported is False)
    assert focus.sent == ""


async def test_closing_releases_the_claim_with_an_empty_session():
    reports = AsyncMock()
    focus = Focus(focus_shell(session_focus=reports), client_id="c")
    focus.report("a")
    await settle(lambda: focus.sent == "a")
    await focus.clear()
    assert reports.await_args.args == ("", "c") and focus.sent == "" and focus.session == ""
