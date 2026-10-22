"""Real native project switching over one shared daemon and one UDS client."""
import textwrap
from pathlib import Path

import pytest

import test_ratatui_e2e_settings as terminal


CHILD = textwrap.dedent('''
    import asyncio, os, sys, tempfile
    from pathlib import Path
    from nexus.client.protocol import Client
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection, UpdatesSection
    from nexus.host import protocol as p
    from nexus.host.daemon import Daemon
    from nexus.host.transports.uds import UDSClient
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime
    from nexus.ui.cli.uds import UdsTransport
    from nexus.ui.ratatui.run import run

    async def main():
        root = Path(sys.argv[1])
        os.environ["NEXUS_DEV"] = "1"  # Scripted providers intentionally bypass credential setup.
        a, b = root / 'project-a', root / 'project-b'
        a.mkdir()
        b.mkdir()
        (root / '.nexus').mkdir(exist_ok=True)
        (root / '.nexus/config.toml').write_text('config_version = 2\\n[models]\\ndefault = "scripted/native"\\n')
        config = Config(model='scripted/native', version=2, v2=ConfigV2(
            model=ModelSection(default='scripted/native'),
            permissions=PermissionsSection(mode='allow', on_unattended='allow'),
            updates=UpdatesSection(check=False)))
        def factory(workspace, **kwargs):
            provider = ScriptedProvider(*[text_response('Answer from ' + workspace.name) for _ in range(32)])
            return Runtime(workspace, config=config, providers={'scripted': provider}, **kwargs)
        with tempfile.TemporaryDirectory(prefix='nexus-pty-', dir='/tmp') as short:
            daemon = Daemon(a, home=root, socket_path=Path(short) / 's.sock', runtime_factory=factory)
            await daemon.start()
            try:
                peer = await UDSClient.connect(daemon.socket, focus=str(a), environ={})
                await peer.call(p.SessionOpen(session='chat-a'))
                await peer.call(p.SessionStart(session='chat-a', content='Initial A message'))
                await daemon.projects.hosts[peer.info.focus.key].facade.wait_idle(timeout=5)
                await peer.call(p.ProjectFocus(project=str(b)))
                await peer.call(p.SessionOpen(session='chat-b'))
                await peer.call(p.SessionStart(session='chat-b', content='Initial B message'))
                for host in daemon.projects.hosts.values():
                    await host.facade.wait_idle(timeout=5)
                await peer.call(p.ProjectSessionOpen(workspace=str(a), session='chat-a'))
                await run(Client(UdsTransport(peer)), session='chat-a', workspace=a)
            finally:
                await daemon.aclose()
    asyncio.run(main())
''')


def test_native_picker_switches_projects_without_reconnecting(tmp_path, monkeypatch):
    binary = terminal.ROOT / 'rust/tui/target/debug/nexus-ratatui'
    if not binary.exists():
        pytest.skip('native binary is unavailable')
    monkeypatch.setattr(terminal, 'BINARY', Path(binary))
    monkeypatch.setattr(terminal, 'CHILD', CHILD)
    app = terminal.App(tmp_path)
    try:
        app.wait_for('Answer from project-a')
        app.key(b'\x0f', .6)
        app.key(b'\x0f', .6)  # Focus the already-open Sessions sidebar.
        app.wait_for('Initial B message')
        lines = app.redraw(.5).splitlines()
        row, line = next((i + 1, line) for i, line in enumerate(lines) if 'Initial B message' in line)
        col = line.index('Initial B message') + 3
        app.key(f'\x1b[<0;{col};{row}M\x1b[<0;{col};{row}m'.encode() * 2, .8)
        app.key(b'\x1b', .5)  # Close the picker after opening the target.
        app.wait_for('Answer from project-b')
        lines = app.redraw(.5).splitlines()
        row = next(i + 1 for i, line in enumerate(lines) if 'Type a message' in line)
        app.key(f'\x1b[<0;12;{row}M\x1b[<0;12;{row}m'.encode(), .2)
        app.key(b'Second B message\r', .8)
        app.wait_for('Second B message')
        app.key(b'\x0f', .6)
        app.key(b'\x0f', .6)  # Focus the already-open Sessions sidebar.
        app.wait_for('Initial A message')
        app.key(b'\x11', .5)
    finally:
        app.close()
