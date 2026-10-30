"""Native UI drives a real first-turn worktree write and harness commit."""
from __future__ import annotations

import subprocess
import textwrap
import time
from pathlib import Path

import pytest

import test_ratatui_e2e_settings as terminal


CHILD = textwrap.dedent('''
    import asyncio, os, sys, tempfile
    from pathlib import Path
    from nexus.client.protocol import Client
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection, UpdatesSection, WorktreesSection
    from nexus.host import protocol as p
    from nexus.host.daemon import Daemon
    from nexus.host.transports.uds import UDSClient
    from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
    from nexus.runtime import Runtime
    from nexus.ui.cli.uds import UdsTransport
    from nexus.ui.ratatui.run import run

    async def main():
        root = Path(sys.argv[1])
        os.environ['NEXUS_DEV'] = '1'
        project = root / 'main'
        (root / '.nexus').mkdir(exist_ok=True)
        (root / '.nexus/config.toml').write_text('config_version = 2\\n[models]\\ndefault = "scripted/native"\\n')
        config = Config(model='scripted/native', version=2, v2=ConfigV2(
            model=ModelSection(default='scripted/native'),
            permissions=PermissionsSection(mode='allow', on_unattended='allow'),
            updates=UpdatesSection(check=False),
            worktrees=WorktreesSection()))
        provider = ScriptedProvider(
            text_response('Session root ready'),
            tool_response(('create-call', 'checkout', {'action': 'isolate', 'reason': 'write the result file in isolation'})),
            tool_response(('write-call', 'write', {'path': 'session-result.txt', 'content': 'created in session root\\n'})),
            text_response('Worktree write complete'))
        def factory(workspace, **kwargs):
            return Runtime(workspace, config=config, providers={'scripted': provider}, **kwargs)
        with tempfile.TemporaryDirectory(prefix='nexus-worktree-pty-', dir='/tmp') as short:
            daemon = Daemon(project, home=root, socket_path=Path(short) / 's.sock', runtime_factory=factory)
            await daemon.start()
            try:
                peer = await UDSClient.connect(daemon.socket, focus=str(project), environ=dict(os.environ))
                opened = await peer.call(p.SessionOpen(session='worktree-session'))
                assert isinstance(opened, p.SessionOpenResult), opened
                host = daemon.projects.hosts[peer.info.focus.key]
                await peer.call(p.SessionStart(session='worktree-session', content='Prepare the session UI'))
                await host.facade.wait_idle(timeout=15)
                await run(Client(UdsTransport(peer)), session='worktree-session', workspace=project)
            finally:
                await daemon.aclose()
    asyncio.run(main())
''')

CHILD = "try:\n" + textwrap.indent(CHILD, "    ") + "\nexcept BaseException:\n    import pathlib, sys, traceback\n    pathlib.Path(sys.argv[1], 'startup-error').write_text(traceback.format_exc())\n    raise\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_native_agent_creates_a_worktree_mid_turn_then_writes_and_commits_in_it(tmp_path: Path, monkeypatch):
    binary = terminal.ROOT / "rust/tui/target/debug/nexus-ratatui"
    if not binary.exists():
        pytest.skip("native binary is unavailable")
    project = tmp_path / "main"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.test")
    (project / "tracked.txt").write_text("seed\n", encoding="utf-8")
    _git(project, "add", "tracked.txt")
    _git(project, "commit", "-m", "seed")
    main_head = _git(project, "rev-parse", "HEAD")

    monkeypatch.setattr(terminal, "BINARY", Path(binary))
    monkeypatch.setattr(terminal, "CHILD", CHILD)
    app = terminal.App(tmp_path)
    try:
        startup_deadline = time.monotonic() + 15
        while not app.screen and app.process.poll() is None and time.monotonic() < startup_deadline:
            time.sleep(.05)
        try:
            app.wait_for("Type a message")
        except Exception as exc:
            error_file = tmp_path / "startup-error"
            diagnostic = error_file.read_text() if error_file.exists() else app.text()
            raise AssertionError(f"Child status: {app.process.poll()}\n{diagnostic}") from exc
        lines = app.text().splitlines()
        row = next(i + 1 for i, line in enumerate(lines) if "Type a message" in line)
        app.key(f"\x1b[<0;12;{row}M\x1b[<0;12;{row}m".encode(), .2)
        app.key(b"Create session-result.txt with the requested contents\r", .5)
        app.wait_for("Worktree write complete", timeout=25)
        assert "Create a worktree, or work in the main checkout?" not in app.text()  # no question, ever
        shown = app.text()
        assert "Created worktree" in shown and "write the result file in isolation" in shown
        assert "⎇" in shown  # the top bar follows the agent's move with no tab switch

        result = project / "session-result.txt"
        assert not result.exists(), "the main checkout must remain untouched"
        linked = [
            Path(line.removeprefix("worktree "))
            for line in _git(project, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ") and Path(line.removeprefix("worktree ")).resolve() != project.resolve()
        ]
        assert len(linked) == 1
        worktree = linked[0]
        assert (worktree / "session-result.txt").read_text(encoding="utf-8") == "created in session root\n"
        assert _git(project, "rev-parse", "HEAD") == main_head
        commit_deadline = time.monotonic() + 10
        while time.monotonic() < commit_deadline and _git(worktree, "rev-parse", "HEAD") == main_head:
            time.sleep(.1)
        assert _git(worktree, "rev-parse", "HEAD") != main_head
        assert _git(worktree, "branch", "--show-current").startswith("nexus/wt/")
        assert "Nexus-Session:" in _git(worktree, "log", "-1", "--format=%B")
        assert "Nexus-Agent: root" in _git(worktree, "log", "-1", "--format=%B")
        app.wait_for("Saved turn ·")
        saved_row = next(i + 1 for i, line in enumerate(app.text().splitlines()) if "Saved turn ·" in line)
        app.key(f"\x1b[<0;20;{saved_row}M\x1b[<0;20;{saved_row}m".encode(), .4)
        app.wait_for("Sequence:")
        app.key(b"\x11", .5)
    finally:
        app.close()


def test_native_first_message_is_sent_without_a_worktree_question(tmp_path: Path, monkeypatch):
    project = tmp_path / "main"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.test")
    (project / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(project, "add", "seed.txt")
    _git(project, "commit", "-m", "seed")
    binary = terminal.ROOT / "rust/tui/target/debug/nexus-ratatui"
    if not binary.exists():
        pytest.skip("native binary is unavailable")
    monkeypatch.setattr(terminal, "BINARY", Path(binary))
    # The daemon child no longer pre-runs the first prompt, so the user's message is the first turn.
    first_turn_child = CHILD.replace(
        "await peer.call(p.SessionStart(session='worktree-session', content='Prepare the session UI'))\n                await host.facade.wait_idle(timeout=15)",
        "await asyncio.sleep(.3)",
    )
    monkeypatch.setattr(terminal, "CHILD", first_turn_child)
    app = terminal.App(tmp_path)
    try:
        app.wait_for("Type a message")
        lines = app.text().splitlines()
        row = next(i + 1 for i, line in enumerate(lines) if "Type a message" in line)
        app.key(f"\x1b[<0;12;{row}M\x1b[<0;12;{row}m".encode(), .2)
        app.key(b"/worktree\r", .4)  # the mode picker is gone
        assert "Always worktree" not in app.redraw(.3)
        app.key(b"\x1b", .3)
        app.key(b"Please answer with ok\r", .4)
        app.wait_for("Session root ready", timeout=25)  # sent at once: no Yes/No question
        shown = app.redraw(.3)
        assert "Create a worktree, or work in the main checkout?" not in shown
        assert "main checkout" in shown  # the bar states where the session is
        assert _git(project, "worktree", "list", "--porcelain").count("worktree ") == 1
    finally:
        app.close()
