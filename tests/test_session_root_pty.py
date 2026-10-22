"""Native composer runs in a linked-worktree session over the shared daemon."""
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

import test_ratatui_e2e_settings as terminal


CHILD = textwrap.dedent('''
    import asyncio, os, subprocess, sys, tempfile
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
        os.environ['NEXUS_DEV'] = '1'
        project, linked = root / 'main', root / 'linked'
        (root / '.nexus').mkdir(exist_ok=True)
        (root / '.nexus/config.toml').write_text('config_version = 2\\n[models]\\ndefault = "scripted/native"\\n')
        config = Config(model='scripted/native', version=2, v2=ConfigV2(
            model=ModelSection(default='scripted/native'),
            permissions=PermissionsSection(mode='allow', on_unattended='allow'),
            updates=UpdatesSection(check=False)))
        def factory(workspace, **kwargs):
            return Runtime(workspace, config=config,
                providers={'scripted': ScriptedProvider(text_response('Session root ready'))}, **kwargs)
        with tempfile.TemporaryDirectory(prefix='nexus-root-pty-', dir='/tmp') as short:
            daemon = Daemon(project, home=root, socket_path=Path(short) / 's.sock', runtime_factory=factory)
            await daemon.start()
            try:
                peer = await UDSClient.connect(daemon.socket, focus=str(linked), environ=dict(os.environ))
                opened = await peer.call(p.SessionOpen(session='root-session'))
                assert isinstance(opened, p.SessionOpenResult), opened
                host = daemon.projects.hosts[peer.info.focus.key]
                assert host.runtime.session_root('root-session').path == linked.resolve()
                await peer.call(p.SessionStart(session='root-session', content='Verify the session root'))
                await host.facade.wait_idle(timeout=5)
                await run(Client(UdsTransport(peer)), session='root-session', workspace=project)
            finally:
                await daemon.aclose()
    asyncio.run(main())
''')


CHILD = "try:\n" + textwrap.indent(CHILD, "    ") + "\nexcept BaseException:\n    import pathlib, sys, traceback\n    pathlib.Path(sys.argv[1], 'startup-error').write_text(traceback.format_exc())\n    raise\n"


def test_native_composer_starts_in_external_session_root(tmp_path, monkeypatch):
    binary = terminal.ROOT / 'rust/tui/target/debug/nexus-ratatui'
    if not binary.exists():
        pytest.skip('native binary is unavailable')
    project = tmp_path / 'main'
    project.mkdir()
    def git(*args):
        subprocess.run(['git', '-C', str(project), *args], check=True, capture_output=True)
    git('init', '-b', 'main')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'seed')
    git('worktree', 'add', '-b', 'session-test', str(tmp_path / 'linked'))
    monkeypatch.setattr(terminal, 'BINARY', Path(binary))
    monkeypatch.setattr(terminal, 'CHILD', CHILD)
    app = terminal.App(tmp_path)
    try:
        startup_deadline = time.monotonic() + 15
        while not app.screen and app.process.poll() is None and time.monotonic() < startup_deadline:
            time.sleep(.05)
        try:
            app.wait_for('Session root ready')
        except Exception as exc:
            error_file = tmp_path / 'startup-error'
            diagnostic = error_file.read_text() if error_file.exists() else bytes(app.screen).decode('utf-8', 'replace')
            raise AssertionError(f'Child status: {app.process.poll()}\n{diagnostic}') from exc
        lines = app.redraw(.5).splitlines()
        row = next(i + 1 for i, line in enumerate(lines) if 'Type a message' in line)
        app.key(f'\x1b[<0;12;{row}M\x1b[<0;12;{row}m'.encode(), .2)
        app.key(b'!pwd > cwd-result\r', .5)
        result = tmp_path / 'linked' / 'cwd-result'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not result.exists():
            time.sleep(.05)
        assert result.exists(), app.text()
        assert result.read_text().strip() == str((tmp_path / 'linked').resolve())
        assert not (tmp_path / 'main' / 'cwd-result').exists()
        app.key(b'\x11', .5)
    finally:
        app.close()
