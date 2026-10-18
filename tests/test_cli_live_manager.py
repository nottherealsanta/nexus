"""``nexus live`` on the manager session asks the separate cross-session notice (docs/manager.md)."""
from __future__ import annotations

import stat
import sys

import pytest

from nexus.host import protocol as p
from nexus.ui_support.manager import MANAGER_SESSION_ID

from tests.test_cli_live import FAKE, FakeClient, ask_no, ask_yes, live_event, run, status


class ManagerClient(FakeClient):
    """A fake daemon that tracks the base notice and the cross-session notice separately."""

    def __init__(self, *, base: bool = True, cross: bool = False):
        super().__init__(consent=base)
        self.cross = cross
        self.scoped: list[tuple[bool | None, str]] = []

    async def live_consent(self, accept=None, *, scope=""):
        self.scoped.append((accept, scope))
        if accept:
            self._status = status(consent=True)
            self.cross = self.cross or scope == "manager"
        if scope == "manager":
            return p.LiveConsentResult(accepted=self.cross and self._status.consent, scope="manager")
        return p.LiveConsentResult(accepted=self._status.consent)


@pytest.fixture
def helper(tmp_path, monkeypatch):
    launcher = tmp_path / "nexus-live"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(launcher))
    monkeypatch.setenv("FAKE_LIVE_LOG", str(tmp_path / "helper.log"))
    monkeypatch.delenv("FAKE_LIVE_MODE", raising=False)


async def test_both_notices_recorded_starts_without_asking(helper):
    client = ManagerClient(base=True, cross=True)
    client.queue.put_nowait(live_event("live.ended", reason="user"))
    asked = []

    async def ask():
        asked.append(True)
        return "y"

    coro, out, _err = run(client, session=MANAGER_SESSION_ID, ask=ask)
    assert await coro == 0
    assert asked == [] and client.scoped == [(None, "manager")]
    assert [start[0] for start in client.starts] == [MANAGER_SESSION_ID]


async def test_missing_cross_session_notice_is_shown_and_recorded_with_its_scope(helper):
    client = ManagerClient(base=True, cross=False)
    client.queue.put_nowait(live_event("live.ended", reason="user"))
    coro, out, err = run(client, session=MANAGER_SESSION_ID, ask=ask_yes)
    assert await coro == 0
    text = out.getvalue()
    assert "OpenAI" in text and "OTHER sessions" in text and "final reports" in text
    assert "Agree and start the call? [y/N]" in err.getvalue()
    assert client.scoped == [(None, "manager"), (True, "manager")]
    assert len(client.starts) == 1


async def test_declining_the_manager_notice_records_and_starts_nothing(helper):
    client = ManagerClient(base=True, cross=False)
    coro, _out, err = run(client, session=MANAGER_SESSION_ID, ask=ask_no)
    assert await coro == 1
    assert client.scoped == [(None, "manager")] and client.starts == []
    assert "Not started. Nothing was recorded." in err.getvalue()


async def test_the_base_notice_is_part_of_the_manager_dialog_when_missing(helper):
    client = ManagerClient(base=False, cross=False)
    client.queue.put_nowait(live_event("live.ended", reason="user"))
    coro, out, _err = run(client, session=MANAGER_SESSION_ID, ask=ask_yes)
    assert await coro == 0
    assert "microphone" in out.getvalue() and "OTHER sessions" in out.getvalue()
    assert client.scoped == [(True, "manager")], "one agreement records the cross-session notice and the base"


async def test_non_terminal_stdin_never_auto_accepts_the_manager_notice(helper):
    client = ManagerClient(base=True, cross=False)
    coro, out, err = run(client, session=MANAGER_SESSION_ID, interactive=False)
    assert await coro == 1
    assert client.scoped == [(None, "manager")] and client.starts == []
    assert "terminal" in err.getvalue() and out.getvalue() == ""


async def test_ordinary_sessions_never_ask_about_the_manager_notice(helper):
    client = ManagerClient(base=True, cross=False)
    client.queue.put_nowait(live_event("live.ended", reason="user"))
    coro, _out, _err = run(client, session="s")
    assert await coro == 0
    assert client.scoped == [] and len(client.starts) == 1
