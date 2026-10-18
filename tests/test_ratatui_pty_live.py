"""GPT Live in a real controlling PTY: consent, pane header, mute, Live card, hang up (M4).

Real Python bridge + real native binary + a real host whose Live manager uses the fake transport,
and the helper is `tests/fixtures/live/fake_nexus_live.py`. A dev-mode `live-call` mock scenario was
considered and not added: mock scenarios script *model* responses, while a call's events come from the
LiveManager, so a fake transport and fake helper cover the same ground without a second mechanism.
No real microphone or OpenAI call is involved (not verified).
Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import os
import stat
import sys
import textwrap
import time

import pytest

from tests.test_ratatui_e2e_settings import BINARY, ROOT, App

FAKE = ROOT / "tests" / "fixtures" / "live" / "fake_nexus_live.py"

CHILD = textwrap.dedent('''
    import asyncio, sys
    from pathlib import Path
    from nexus.client.protocol import Client
    from nexus.config import Config
    from nexus.config.schema import AgentSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection
    from nexus.host import HostFacade
    from nexus.live import protocol as lp
    from nexus.live.manager import LiveManager
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime
    from nexus.ui.ratatui.run import run
    from tests.live_fakes import FakeLiveTransport, live_config

    class FacadeTransport:
        def __init__(self, facade): self.facade = facade
        async def request(self, command): return await self.facade.handle(command)
        def events(self, session, from_seq=0, *, follow=True, client_id=None):
            return self.facade.subscribe(session, from_seq, follow=follow, client_id=client_id)
        async def aclose(self): pass

    async def speak(runtime, transport):
        while not runtime.live.active_session:
            await asyncio.sleep(.1)
        await asyncio.sleep(1.0)
        transport.push(lp.TurnDone("user", "read the readme please"))
        transport.push(lp.TurnDone("assistant", "Sure, asking the agent."))

    async def main():
        workspace = Path(sys.argv[1])
        config = Config(model="scripted/native", version=2, v2=ConfigV2(
            model=ModelSection(default="scripted/native"), agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"), tools=ToolsSection()))
        runtime = Runtime(workspace, config=config, providers={"scripted": ScriptedProvider(text_response("ok"))})
        transport = FakeLiveTransport()
        runtime.live = LiveManager(live_config(), transport_factory=lambda **kw: transport, environ={}, flush_interval=0.0)
        asyncio.get_event_loop().create_task(speak(runtime, transport))
        client = Client(FacadeTransport(HostFacade(runtime)))
        await run(client, session="e2e", workspace=workspace)

    asyncio.run(main())
''')


@pytest.fixture
def app(tmp_path, monkeypatch):
    if not BINARY.exists():
        pytest.skip("build the native prototype first")
    wrapper = tmp_path / "nexus-live"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE}\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(wrapper))
    import tests.test_ratatui_e2e_settings as base
    monkeypatch.setattr(base, "CHILD", CHILD)
    a = App(tmp_path)
    try:
        yield a
    finally:
        a.close()


def test_consent_call_bar_mute_card_and_hang_up(app, tmp_path):
    from pathlib import Path

    consent = Path(os.environ["NEXUS_HOME"]) / "live-consent.json"  # conftest gives every test a private home
    time.sleep(3.5)  # first run may open Settings on Providers (asynchronously); close it if it did
    for _ in range(3):
        app.key(b"\x1b", .6)
        if "Choose the default model" not in app.redraw(.4):
            break
    app.key(b"/live", .4)
    app.key(b"\r", 1.0)
    app.wait_for("Use GPT Live voice?")
    screen = app.redraw()
    assert "ChatGPT account" in screen and "Agree and start the call" in screen, screen
    assert not consent.exists()  # asking records nothing
    app.key(b"\x1b[B", .3)  # Down from the notice to "Agree and start the call"
    app.key(b"\r", 1.5)
    app.wait_for("LISTENING")  # the phase strip: caps phase, its animation, the clock (step 11)
    screen = app.redraw()
    assert consent.exists()  # consent survives in a file, not memory
    app.wait_for("⤢ expand")  # the open call's pane above the composer
    screen = app.redraw()
    lines = screen.splitlines()
    bar = next(line for line in lines if "⤢ expand" in line)  # the pane header carries the call line
    assert "LISTENING" in bar and "00:0" in bar and "⤢ expand" in bar, bar
    assert sum("LISTENING" in line for line in lines) == 1  # not also in the composer
    assert "gpt-live-1-codex · sol" not in screen  # no timeline card while the call is open
    app.wait_for("read the readme please")
    screen = app.redraw()
    assert "Voice" in screen and "Sure, asking the agent." in screen
    app.key(b"/live mute", .4)
    app.key(b"\r", 1.0)
    app.wait_for("MUTED")
    app.key(b"/live stop", .4)
    app.key(b"\r", 1.5)
    deadline = time.monotonic() + 10
    while "MUTED" in app.redraw(.4):
        assert time.monotonic() < deadline, "the call bar never left"
    screen = app.redraw()
    assert "⤢ expand" not in screen  # the pane closes with the call
    assert "gpt-live-1-codex · sol" in screen  # the ended call joins the timeline as a card
    assert "Ended" in screen and "user" in screen  # the card records how it ended
