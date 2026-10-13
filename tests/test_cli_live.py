"""``nexus live``: the headless GPT Live call (docs/live.md, GPT_LIVE_PLAN section 5).

The daemon is replaced by a fake client; the audio helper is the real ``LiveCall``
driven through ``NEXUS_LIVE_BINARY`` pointing at a launcher for
``tests/fixtures/live/fake_nexus_live.py``, so the offer/answer/stop handshake runs.
"""
from __future__ import annotations

import asyncio
import io
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus import cli
from nexus.client.protocol import FacadeError
from nexus.events import Event
from nexus.host import protocol as p
from nexus.ui.cli import live as cli_live

FAKE = Path(__file__).parent / "fixtures" / "live" / "fake_nexus_live.py"


def live_event(kind: str, call_id: str = "c1", **data) -> Event:
    return Event(type=kind, data={"call_id": call_id, **data})


def status(**over) -> p.LiveStatusResult:
    base = dict(available=True, reason="", state="idle", phase="", active_session="", call_id="", voice="",
                muted=False, elapsed_s=0.0, max_minutes=30, levels_in=0.0, levels_out=0.0, dropped_frames=0,
                last_error="", consent=True)
    base.update(over)
    return p.LiveStatusResult(**base)


class FakeClient:
    """Enough of ``Client`` for a call: status, consent, open, start/stop, helper events, a stream."""

    def __init__(self, *, consent: bool = True, available: bool = True, reason: str = "", sessions=()):
        self._status = status(consent=consent, available=available, reason=reason)
        self.consent_calls: list[bool | None] = []
        self.starts: list[tuple[str, str, str, bool]] = []
        self.stops: list[tuple[str, str]] = []
        self.opened: list[str] = []
        self.helper_events: list[tuple[str, str]] = []
        self.sessions = list(sessions)
        self.start_error: Exception | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def live_status(self):
        return self._status

    async def live_consent(self, accept=None):
        self.consent_calls.append(accept)
        self._status = status(consent=bool(accept), available=self._status.available, reason=self._status.reason)
        return p.LiveConsentResult(accepted=bool(accept), accepted_at="2026-10-13T10:00:00+0000" if accept else "")

    async def open_session(self, session, *, create=True, recover=True):
        self.opened.append(session)
        return SimpleNamespace(id=session, last_seq=7)

    async def list_sessions(self):
        return list(self.sessions)

    async def live_start(self, session, offer_sdp, *, voice="", new_conversation=False):
        self.starts.append((session, offer_sdp, voice, new_conversation))
        if self.start_error is not None:
            raise self.start_error
        return p.LiveStartResult(call_id="c1", answer_sdp="v=0 answer", voice=voice or "cove")

    async def live_stop(self, session, reason="user"):
        self.stops.append((session, reason))
        return status(consent=True, state="idle")

    async def live_helper_event(self, session, kind, detail=""):
        self.helper_events.append((kind, detail))
        return p.LiveHelperEventResult()

    async def stream(self, session, from_seq=0, *, follow=True):
        assert from_seq == 7  # the session's end before the call: nothing earlier is re-read
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def aclose(self):
        self.closed = True


async def ask_yes():
    return "y"


async def ask_no():
    return "n"


async def never_ends():
    await asyncio.Event().wait()


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A ``nexus-live`` launcher that runs the fake helper; the command log is in ``tmp_path``."""
    launcher = tmp_path / "nexus-live"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "helper.log"
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(launcher))
    monkeypatch.setenv("FAKE_LIVE_LOG", str(log))
    monkeypatch.delenv("FAKE_LIVE_MODE", raising=False)
    return log


def commands(log: Path) -> list[str]:
    return [json.loads(line)["type"] for line in log.read_text().splitlines()] if log.exists() else []


def run(client, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    options = dict(session="s", stdout=out, stderr=err, interactive=True, ask=ask_yes, end_of_input=never_ends)
    options.update(kwargs)
    return cli_live.run_live(client, **options), out, err


# -- output labels ------------------------------------------------------------

def test_format_event_labels_each_kind_and_skips_other_calls():
    assert cli_live.format_event(live_event("live.transcript", role="user", text="book a table"), "c1") == \
        "you: book a table"
    assert cli_live.format_event(live_event("live.transcript", role="assistant", text="Done."), "c1") == \
        "voice: Done."
    assert cli_live.format_event(live_event("live.transcript", role="user", text="x", truncated=True), "c1") == \
        "you: x (truncated)"
    assert cli_live.format_event(live_event("live.delegated", delegation_id="d", text="summarise", mode="steer"),
                                 "c1") == "→ agent (steer): summarise"
    assert cli_live.format_event(live_event("live.appended", channel="commentary", text="reading files"), "c1") == \
        "← voice [commentary]: reading files"
    assert cli_live.format_event(live_event("live.error", message="slow network"), "c1") == "error: slow network"
    assert cli_live.format_event(live_event("live.ended", reason="deadline", duration_s=61.4), "c1") == \
        "Call ended (deadline, 61 s)"
    assert cli_live.format_event(live_event("live.ended", reason="daemon_shutdown", error="boom"), "c1") == \
        "Call ended (daemon_shutdown) · boom"
    # partial captions, levels and other calls' events are not printed
    assert cli_live.format_event(live_event("live.transcript.delta", role="user", text="pa"), "c1") is None
    assert cli_live.format_event(live_event("live.transcript", call_id="old", role="user", text="old"), "c1") is None


def test_format_event_escapes_controls_and_keeps_multiline_text_indented():
    line = cli_live.format_event(live_event("live.appended", channel="final", text="a\x1b[2J\nb"), "c1")
    assert "\x1b" not in line and line.startswith("← voice [final]: a\\x1b") and "\n    b" in line


# -- the call ----------------------------------------------------------------

async def test_consent_accepted_runs_a_call_and_prints_labelled_captions(helper):
    client = FakeClient(consent=False)
    for event in [
        live_event("live.transcript", call_id="old", role="user", text="from an earlier call"),
        live_event("live.transcript", role="user", text="check the build"),
        live_event("live.delegated", delegation_id="d1", text="check the build", mode="queue"),
        live_event("live.appended", delegation_id="d1", channel="commentary", text="running tests"),
        live_event("live.appended", delegation_id="d1", channel="final", text="All 12 tests pass."),
        live_event("live.transcript", role="assistant", text="The build passes."),
        live_event("live.ended", reason="user", duration_s=3.2),
    ]:
        client.queue.put_nowait(event)
    coro, out, err = run(client, ask=ask_yes)
    assert await coro == 0
    text = out.getvalue()
    assert client.consent_calls == [True]  # recorded only after the yes
    assert [(session, voice, new) for session, _offer, voice, new in client.starts] == [("s", "", False)]
    assert "OpenAI" in text and "microphone" in text  # the notice is shown before the prompt
    assert "Agree and start the call? [y/N]" in err.getvalue()
    lines = [line for line in text.splitlines() if line.startswith(("you:", "voice:", "→", "←", "Call ended"))]
    assert lines == [
        "you: check the build",
        "→ agent (queue): check the build",
        "← voice [commentary]: running tests",
        "← voice [final]: All 12 tests pass.",
        "voice: The build passes.",
        "Call ended (user, 3 s)",
    ]
    assert "from an earlier call" not in text
    assert client.stops == [("s", "user")]  # the daemon call was already ended; LiveStop is idempotent
    assert commands(helper)[-1] == "stop"  # the audio helper was released


async def test_declining_the_prompt_starts_nothing_and_records_nothing(helper):
    client = FakeClient(consent=False)
    coro, out, err = run(client, ask=ask_no)
    assert await coro == 1
    assert client.consent_calls == [] and client.starts == [] and client.stops == []
    assert "Not started. Nothing was recorded." in err.getvalue()
    assert not helper.exists()  # the helper was never spawned


async def test_empty_or_closed_answer_counts_as_no(helper):
    async def closed():
        return None

    client = FakeClient(consent=False)
    coro, _out, err = run(client, ask=closed)
    assert await coro == 1 and client.consent_calls == [] and "Not started" in err.getvalue()


async def test_non_terminal_stdin_without_consent_is_refused_and_never_auto_accepted(helper):
    client = FakeClient(consent=False)
    asked = []

    async def ask():
        asked.append(True)
        return "y"

    coro, out, err = run(client, interactive=False, ask=ask)
    assert await coro == 1
    assert asked == [] and client.consent_calls == [] and client.starts == []
    assert "nexus live" in err.getvalue() and "terminal" in err.getvalue()
    assert out.getvalue() == ""


async def test_recorded_consent_lets_a_non_terminal_run_start(helper):
    client = FakeClient(consent=True)
    client.queue.put_nowait(live_event("live.ended", reason="user"))
    coro, out, _err = run(client, interactive=False)
    assert await coro == 0 and client.consent_calls == [] and len(client.starts) == 1


async def test_ctrl_c_hangs_up_cleanly_and_releases_the_helper(helper):
    client = FakeClient(consent=True)
    coro, out, err = run(client, end_of_input=never_ends)
    task = asyncio.ensure_future(coro)
    for _ in range(500):  # until the call is connected
        if "Live call connected" in out.getvalue():
            break
        await asyncio.sleep(0.01)
    assert "Live call connected in session s" in out.getvalue()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.stops == [("s", "user")]
    assert "Hanging up." in out.getvalue()
    assert commands(helper)[-1] == "stop"  # the helper got its stop command


async def test_end_of_input_hangs_up(helper):
    client = FakeClient(consent=True)

    async def eof():
        return None

    coro, out, _err = run(client, end_of_input=eof)
    assert await coro == 0
    assert "Input ended; hanging up." in out.getvalue()
    assert client.stops == [("s", "user")]


async def test_a_helper_that_stops_ends_the_call_with_its_fix(helper, monkeypatch):
    monkeypatch.setenv("FAKE_LIVE_MODE", "crash_after_ready")
    client = FakeClient(consent=True)
    coro, out, err = run(client, end_of_input=never_ends)
    assert await coro == 1
    assert "the audio helper stopped" in err.getvalue()
    assert client.stops == [("s", "helper_failed")]


async def test_missing_helper_binary_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(tmp_path / "absent" / "nexus-live"))
    client = FakeClient(consent=False)
    coro, out, err = run(client)
    assert await coro == 1
    text = err.getvalue()
    assert "nexus-live audio helper is not installed" in text and "cargo build" in text
    assert client.consent_calls == [] and client.starts == []  # no prompt, no call


async def test_unavailable_status_shows_the_host_reason_and_fix(helper):
    client = FakeClient(available=False, reason="no_credential")
    coro, _out, err = run(client)
    assert await coro == 1
    assert "ChatGPT sign-in" in err.getvalue() and "nexus auth codex login" in err.getvalue()
    assert client.starts == []


async def test_busy_status_names_the_owning_session(helper):
    client = FakeClient(available=False, reason="busy")
    client._status = status(available=False, reason="busy", active_session="other")
    coro, _out, err = run(client)
    assert await coro == 1 and "session other" in err.getvalue()


async def test_daemon_start_failure_is_shown_and_the_helper_is_released(helper, monkeypatch):
    monkeypatch.setenv("FAKE_LIVE_MODE", "close_on_offer")
    client = FakeClient(consent=True)
    coro, _out, err = run(client)
    assert await coro == 1
    assert "could not start" in err.getvalue() and "Microphone" in err.getvalue()
    assert client.starts == [] and client.stops == []  # the daemon never accepted a call


async def test_consent_refusal_from_the_daemon_is_a_plain_error(helper):
    client = FakeClient(consent=True)
    client.start_error = FacadeError("live_consent_required", "GPT Live needs your consent first")
    coro, _out, err = run(client)
    assert await coro == 1
    text = err.getvalue()
    assert "GPT Live could not start" in text and "nexus live" in text


# -- the command ---------------------------------------------------------------

def test_parser_accepts_the_flags_and_both_workspace_positions(tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["live", "--session", "s1", "--voice", "cove", "--new", "--workspace", str(tmp_path)])
    assert (args.command, args.session, args.voice, args.new, args.workspace) == ("live", "s1", "cove", True, tmp_path)
    before = parser.parse_args(["--workspace", str(tmp_path), "live"])
    assert before.workspace == tmp_path and before.session is None and before.new is False
    bare = parser.parse_args(["live"])
    assert bare.session is None and bare.voice == "" and bare.new is False


async def test_command_uses_the_most_recent_session_or_creates_one(helper, monkeypatch, tmp_path):
    client = FakeClient(consent=True, sessions=[
        SimpleNamespace(id="old", last_activity=10.0), SimpleNamespace(id="newest", last_activity=99.0)])
    client.queue.put_nowait(live_event("live.ended", reason="user"))

    async def open_client(workspace):
        assert workspace == tmp_path
        return client

    monkeypatch.setattr("nexus.ui.cli.open_client", open_client)
    args = cli.build_parser().parse_args(["live", "--workspace", str(tmp_path)])
    assert await cli._live_command(tmp_path, args, io.StringIO(), io.StringIO()) == 0
    assert client.opened == ["newest"] and client.closed

    empty = FakeClient(consent=True)
    empty.queue.put_nowait(live_event("live.ended", reason="user", call_id="c1"))

    async def open_empty(workspace):
        return empty

    monkeypatch.setattr("nexus.ui.cli.open_client", open_empty)
    assert await cli._live_command(tmp_path, args, io.StringIO(), io.StringIO()) == 0
    assert empty.opened[0].startswith("session-") and len(empty.opened[0]) == len("session-") + 8


def test_live_is_listed_in_the_cli_help():
    help_text = cli.build_parser().format_help()
    assert "live" in help_text and "GPT Live" in help_text
