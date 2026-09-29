"""Canonical CLI (Phase 8c): pure daemon client, commands, and clean checkout.

Two halves. The **in-process** half exercises the parser, ``init``, and the pure
render/format helpers with no daemon at all. The **subprocess** half starts a
real daemon around an offline :class:`~nexus.model.providers.scripted.
ScriptedProvider` at the deterministic workspace socket path, then drives the
canonical ``nexus`` entrypoint (``python -m nexus``) exactly as a user would:
``run`` (human and JSONL), ``doctor --explain-reload``, ``models``,
``sessions``, ``ext``, ``agents``, and ``daemon status``.

No Codex CLI is installed or consulted: the daemon builds a Nexus-owned Runtime
and every command goes over the host protocol.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from nexus import cli

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The daemon the CLI auto-connects to. It is started by the test at the exact
#: socket path ``ensure_daemon`` would compute, so the CLI finds a live daemon
#: rather than spawning one itself. The provider is scripted, so no network or
#: credential is used and no Codex binary is involved.
DAEMON_DRIVER = r'''
import argparse
from pathlib import Path

from nexus.config import Config
from nexus.config.schema import (
    AgentSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection,
)
from nexus.host.daemon import Daemon
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


def make_runtime(workspace, **kwargs):
    provider = ScriptedProvider(*([text_response("pong")] * 64))
    config = Config(model="scripted/m", version=2, v2=ConfigV2(
        model=ModelSection(default="scripted/m"),
        agent=AgentSection(profile="coding"),
        permissions=PermissionsSection(mode="ask", on_unattended="deny"),
        tools=ToolsSection(),
    ))
    return Runtime(
        workspace, home=kwargs.get("home"), config=config,
        providers={"scripted": provider},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--socket", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--idle-timeout", type=float, default=300.0)
    parser.add_argument("--max-concurrent-turns", type=int, default=4)
    args = parser.parse_args()
    daemon = Daemon(
        Path(args.workspace), home=args.home, socket_path=args.socket,
        idle_timeout=args.idle_timeout,
        max_concurrent_turns=args.max_concurrent_turns,
        runtime_factory=make_runtime,
    )
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
'''


# ---------------------------------------------------------------------------
# In-process: parser, init, and pure helpers
# ---------------------------------------------------------------------------


def test_parser_exposes_the_canonical_command_set():
    parser = cli.build_parser()
    choices = set(parser._subparsers._group_actions[0].choices)  # type: ignore[attr-defined]
    assert {
        "init",
        "doctor",
        "run",
        "chat",
        "web",
        "replay",
        "daemon",
        "sessions",
        "ext",
        "models",
        "agents",
        "tools",
        "worktrees",
        "auth",
        "update",
        "mock",
    } == choices

    for action, expected in (
        ("daemon", {"status", "stop", "restart", "logs"}),
        ("sessions", {"list", "fork", "replay", "export", "delete", "restore"}),
        ("ext", {"list", "reload", "validate", "trash"}),
        ("models", {"list", "show", "refresh", "tiers"}),
        ("agents", {"list"}),
        ("tools", {"list"}),
        ("worktrees", {"list", "inspect", "review", "acknowledge", "integrate", "discard"}),
    ):
        # Nested subparsers are registered on the child parser; assert via a full parse.
        argv = {
            "daemon": ["daemon", "status"],
            "sessions": ["sessions", "list"],
            "ext": ["ext", "list"],
            "models": ["models", "list"],
            "agents": ["agents", "list"],
            "tools": ["tools", "list"],
        "worktrees": ["worktrees", "list"],
        }[action]
        assert parser.parse_args(argv).command == action
        assert expected  # documented in the parser; full coverage below


@pytest.mark.parametrize(
    "argv",
    [
        ["daemon", "status"],
        ["daemon", "stop"],
        ["daemon", "restart"],
        ["daemon", "logs"],
        ["sessions", "list"],
        ["sessions", "fork", "s"],
        ["sessions", "replay", "s"],
        ["sessions", "export", "s"],
        ["sessions", "delete", "s"],
        ["sessions", "restore", "t"],
        ["ext", "list"],
        ["ext", "reload"],
        ["ext", "validate"],
        ["ext", "trash", ".agents/tools/foo.py"],
        ["ext", "trash", "foo.py", "--reason", "cleanup", "--force"],
        ["models", "list"],
        ["models", "show", "p/m"],
        ["models", "refresh"],
        ["models", "tiers"],
        ["models", "select", "high", "--session", "s"],
        ["agents", "list"],
        ["doctor"],
        ["doctor", "--explain-reload", "--json"],
        ["replay", "s"],
        ["run", "hi", "--json"],
        ["chat"],
        ["worktrees", "list"],
        ["worktrees", "inspect", "child"],
        ["worktrees", "review", "child", "--cursor", "8", "--json"],
        ["worktrees", "review", "child", "--all"],
        ["worktrees", "acknowledge", "child", "a" * 32, "b" * 64],
        ["worktrees", "integrate", "child", "a" * 32, "b" * 64],
        ["worktrees", "discard", "child", "--force", "--review", "a" * 32],
    ],
)
def test_parser_accepts_every_documented_invocation(argv):
    args = cli.build_parser().parse_args(["--workspace", "/tmp/ws", *argv])
    assert args.workspace == Path("/tmp/ws")
    assert args.command is not None


def test_chat_starts_a_new_session_unless_one_is_named(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cli, "_chat_entry", lambda workspace, *, session: seen.append(session) or 0)
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", Tty())
    monkeypatch.setattr(cli.sys, "stdout", Tty())
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv("TERM", "xterm")
    assert cli.main(["chat"]) == 0
    assert cli.main(["chat"]) == 0
    assert cli.main(["chat", "--session", "keep"]) == 0
    assert seen[0].startswith("session-") and seen[1].startswith("session-")
    assert seen[0] != seen[1]
    assert seen[2] == "keep"


def test_chat_rejects_mode_flags():
    parser = cli.build_parser()
    for flag in ("--tui", "--line"):
        with pytest.raises(SystemExit):
            parser.parse_args(["chat", flag])


def test_chat_non_tty_fails_without_opening_client(monkeypatch):
    class Stream(io.StringIO):
        def isatty(self):
            return False

    out, err = Stream(), Stream()
    monkeypatch.setattr(cli.sys, "stdin", Stream())
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", err)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(cli, "_chat_entry", lambda *_args, **_kwargs: pytest.fail("must not launch Textual"))
    assert cli.main(["chat"]) == 2
    assert "interactive terminal" in err.getvalue()
    assert "nexus run" in err.getvalue()


def test_init_creates_defaults_without_overwriting(tmp_path):
    (tmp_path / "SOUL.md").write_text("custom", encoding="utf-8")
    cli.initialize(tmp_path)
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "custom"
    assert (tmp_path / "nexus.toml").exists()
    assert "config_version = 2" in (tmp_path / "nexus.toml").read_text(encoding="utf-8")


def test_main_init_and_help(tmp_path):
    assert cli.main(["--workspace", str(tmp_path), "init"]) == 0
    assert (tmp_path / "nexus.toml").exists()
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0


class _WorktreeClient:
    def __init__(self, *, mutation_status="committed", pages=None):
        self.calls = []
        self.mutation_status = mutation_status
        self.pages = list(pages or [])

    async def integrate_worktree(self, child_id, review_id, digest, *, confirmation_token=""):
        self.calls.append(("integrate", child_id, review_id, digest, confirmation_token))
        from nexus.host.protocol import WorktreeMutationResult

        if not confirmation_token:
            return WorktreeMutationResult(
                child_id, "requires_confirmation", operation="integrate",
                confirmation_token="fresh-token", impact={"parent_clean": True},
            )
        if confirmation_token != "fresh-token":
            raise RuntimeError("stale token rejected")
        return WorktreeMutationResult(child_id, self.mutation_status, operation="integrate")

    async def discard_worktree(self, child_id, *, force=False, review_id=None, confirmation_token=""):
        self.calls.append(("discard", child_id, force, review_id, confirmation_token))
        from nexus.host.protocol import WorktreeMutationResult

        if not confirmation_token:
            return WorktreeMutationResult(
                child_id, "requires_confirmation", operation="discard",
                confirmation_token="fresh-token",
                impact={"child_dirty": True, "force": force, "summary": "Remove child"},
            )
        return WorktreeMutationResult(child_id, "committed", operation="discard")

    async def review_worktree(self, child_id, *, review_id=None, cursor=0, limit=8):
        self.calls.append(("review", child_id, review_id, cursor, limit))
        return self.pages.pop(0)


def _review_page(cursor, has_more):
    from nexus.host.protocol import WorktreeReviewResult

    return WorktreeReviewResult(
        child_id="child", status="finalized", entries=[{"path": "changed.txt", "change": "modified"}],
        diff=[{"path": "changed.txt", "patch": "+updated\n"}], cursor=cursor,
        has_more=has_more, review_id="a" * 32, digest="b" * 64,
    )


@pytest.mark.asyncio
async def test_worktree_integrate_requires_preview_and_typed_confirmation(monkeypatch):
    client = _WorktreeClient(pages=[_review_page(0, False)])
    args = cli.build_parser().parse_args(["worktrees", "integrate", "child", "a" * 32, "b" * 64])
    out, err = io.StringIO(), io.StringIO()

    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", TTY("confirm integrate child " + "b" * 64 + "\n"))
    monkeypatch.setattr(cli.sys, "stdout", TTY())
    assert await cli._dispatch_worktrees(client, args, out, err) == 0
    assert client.calls[0][-1] == ""
    assert client.calls[-1][-1] == "fresh-token"
    assert "operation: integrate" in out.getvalue()
    assert "changed.txt" in out.getvalue()
    assert "digest: " + "b" * 64 in out.getvalue()
    assert "Type exactly: confirm integrate child " + "b" * 64 in err.getvalue()


@pytest.mark.asyncio
async def test_worktree_force_discard_warns_and_non_tty_refuses(monkeypatch):
    args = cli.build_parser().parse_args(["worktrees", "discard", "child", "--force"])
    client = _WorktreeClient()
    out, err = io.StringIO(), io.StringIO()

    class NonTTY(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", NonTTY())
    monkeypatch.setattr(cli.sys, "stdout", NonTTY())
    assert await cli._dispatch_worktrees(client, args, out, err) == 2
    assert client.calls == [("discard", "child", True, None, "")]
    assert "force: yes" in out.getvalue()
    assert "WARNING: force discard" in out.getvalue()
    assert "interactive typed confirmation" in err.getvalue()


@pytest.mark.asyncio
async def test_worktree_review_is_bounded_and_prints_next_cursor():
    client = _WorktreeClient(pages=[_review_page(0, True)])
    args = cli.build_parser().parse_args(["worktrees", "review", "child", "--limit", "2"])
    out = io.StringIO()
    assert await cli._dispatch_worktrees(client, args, out, io.StringIO()) == 0
    assert client.calls == [("review", "child", None, 0, 2)]
    assert "next cursor: 2" in out.getvalue()


@pytest.mark.asyncio
async def test_worktree_review_all_paginates_bounded_requests():
    client = _WorktreeClient(pages=[_review_page(0, True), _review_page(8, False)])
    args = cli.build_parser().parse_args(["worktrees", "review", "child", "--all"])
    out = io.StringIO()
    assert await cli._dispatch_worktrees(client, args, out, io.StringIO()) == 0
    assert [call[3:] for call in client.calls] == [(0, 8), (8, 8)]
    assert out.getvalue().count("+updated") == 2


@pytest.mark.asyncio
async def test_worktree_mutation_partial_status_and_stale_confirmation_fail(monkeypatch):
    client = _WorktreeClient(mutation_status="recovery_required", pages=[_review_page(0, False)])
    args = cli.build_parser().parse_args(["worktrees", "integrate", "child", "a" * 32, "b" * 64, "--confirm-token"])
    out, err = io.StringIO(), io.StringIO()
    assert await cli._dispatch_worktrees(client, args, out, err) == 1
    assert "status: recovery_required" in out.getvalue()

    class StaleClient(_WorktreeClient):
        async def integrate_worktree(self, child_id, review_id, digest, *, confirmation_token=""):
            self.calls.append(("integrate", child_id, review_id, digest, confirmation_token))
            if not confirmation_token:
                from nexus.host.protocol import WorktreeMutationResult

                return WorktreeMutationResult(child_id, "requires_confirmation", operation="integrate", confirmation_token="fresh-token")
            raise RuntimeError("stale token rejected")

    stale = StaleClient(pages=[_review_page(0, False)])
    with pytest.raises(RuntimeError, match="stale token"):
        await cli._dispatch_worktrees(stale, args, io.StringIO(), io.StringIO())


@pytest.mark.parametrize(
    ("argv", "cwd"),
    [
        ([str(REPO_ROOT / "nexus"), "--help"], None),
        (["-m", "nexus", "--help"], REPO_ROOT),
    ],
)
def test_package_entrypoint_help_supports_directory_and_module_execution(tmp_path, argv, cwd):
    result = subprocess.run(
        [sys.executable, "-E", *argv],
        cwd=str(tmp_path if cwd is None else cwd),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: nexus" in result.stdout
    assert "attempted relative import" not in result.stderr


def test_render_view_is_transcript_like():
    view = {
        "messages": [
            {"role": "assistant", "blocks": [{"kind": "text", "text": "hello world"}]},
            {"role": "assistant", "blocks": [{"kind": "thinking", "text": "hmm"}]},
        ],
        "turns": [
            {"phase": "completed", "tools": [{"name": "Read", "status": "done"}]}
        ],
    }
    rendered = cli._render_view(view)
    assert "hello world" in rendered
    assert "[thinking] hmm" in rendered
    assert "[tool Read done]" in rendered
    assert "[turn completed]" in rendered


def test_format_summary_includes_state_and_cursor():
    summary = argparse.Namespace(
        id="s", state="running", last_seq=7, viewers=2, title="work"
    )
    line = cli._format_summary(summary)
    assert "s" in line and "running" in line and "seq=7" in line and "viewers=2" in line


def test_print_doctor_names_the_reload_boundary():
    out = io.StringIO()
    cli._print_doctor(
        {
            "workspace": "/tmp/ws",
            "providers": [{"name": "scripted", "kind": "ScriptedProvider"}],
            "registry": {"source": "cache", "models": 3, "stale": False},
            "extensions": {"generation": 2, "loaded": 1, "diagnostics": []},
            "reload": {
                "hot": [".agents/tools/*.py"],
                "restart_only": ["nexus/core/**"],
                "note": "hot vs restart",
            },
        },
        out,
    )
    text = out.getvalue()
    assert "scripted" in text
    assert "hot:" in text and ".agents/tools/*.py" in text
    assert "restart only:" in text and "nexus/core/**" in text


def test_print_doctor_database_and_legacy_warnings_are_bounded():
    out = io.StringIO()
    cli._print_doctor(
        {
            "workspace": "/tmp/ws",
            "database": {
                "path": "/home/user/.nexus/nexus.db\nforged output",
                "schema_user_version": 3,
                "quick_check": "ok",
                "size_bytes": 2048,
                "wal_size_bytes": 64,
            },
            "legacy_extensions_pending": [
                "skills", "agents", "tools", "hooks", "providers", "mcp.json",
                "hooks.toml", "nexus.toml", "unexpected", "x" * 10000,
            ],
        },
        out,
    )

    text = out.getvalue()
    assert "database: quick_check=ok schema=3 size=2048B wal=64B" in text
    assert "forged output" in text
    assert text.count("WARNING:") == 1
    assert "move them to .agents/" in text
    assert "unexpected" not in text and "x" * 1000 not in text


# ---------------------------------------------------------------------------
# Subprocess: the canonical entry point over a real (scripted) daemon
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env():
    short = Path(tempfile.mkdtemp(prefix="nexus-cli-", dir="/tmp"))
    workspace = short / "ws"
    workspace.mkdir()
    home = short / "home"
    home.mkdir()
    driver = short / "daemon_driver.py"
    driver.write_text(DAEMON_DRIVER, encoding="utf-8")

    from nexus.host.daemon import default_socket_path

    socket = default_socket_path(workspace, home=home)
    socket.parent.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            "--workspace",
            str(workspace),
            "--socket",
            str(socket),
            "--home",
            str(home),
            "--idle-timeout",
            "60",
        ],
        cwd=str(workspace),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "HOME": str(home), "NEXUS_HOME": str(home / ".nexus"), "PYTHONPATH": str(REPO_ROOT)},
    )
    _wait_for_socket(socket)
    try:
        yield workspace, home, socket
    finally:
        if process.poll() is None:
            process.terminate()
            with contextlib.suppress(Exception):
                process.wait(timeout=5)
        shutil.rmtree(short, ignore_errors=True)


def _wait_for_socket(path: Path, timeout: float = 15.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        import socket as socket_mod

        probe = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise AssertionError(f"daemon socket never became live: {path}")


def _cli(cli_env, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
    workspace, home, _socket = cli_env
    result = subprocess.run(
        [sys.executable, "-m", "nexus", "--workspace", str(workspace), *argv],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "HOME": str(home), "NEXUS_HOME": str(home / ".nexus"), "PYTHONPATH": str(REPO_ROOT)},
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"nexus {' '.join(argv)} failed ({result.returncode}):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def test_run_human_and_jsonl(cli_env):
    human = _cli(cli_env, "run", "hi")
    assert "pong" in human.stdout

    jsonl = _cli(cli_env, "run", "hi", "--session", "json", "--json")
    lines = [json.loads(line) for line in jsonl.stdout.splitlines()]
    assert lines[-1]["type"] == "turn.completed"
    assert any(item["type"] == "text.delta" for item in lines)
    for item in lines:
        assert {"type", "data", "seq", "ts", "session", "turn", "id"} <= set(item)


def test_doctor_explain_reload_json(cli_env):
    result = _cli(cli_env, "doctor", "--explain-reload", "--json")
    report = json.loads(result.stdout)
    assert report["reload"]["hot"]
    assert report["reload"]["restart_only"]
    assert isinstance(report["providers"], list)


def test_doctor_reports_aggregated_registry_mismatches(cli_env):
    """`nexus doctor` surfaces durable registry.mismatch events (PLAN §15.5)."""
    from nexus.config.paths import project_key, state_db_path
    from nexus.events import Event
    from nexus.session.db import SqliteSessionStore, StateDatabase

    workspace, home, _socket = cli_env
    store = SqliteSessionStore(
        StateDatabase(state_db_path(home)),
        project_key(workspace),
        root=str(workspace),
    )
    store.create("probe")
    store.append_event(
        "probe",
        Event(
            type="registry.mismatch",
            data={
                "provider": "anthropic",
                "model": "claude-opus-5",
                "feature": "tools",
                "source": "provider-rejection",
                "detail": "Authorization: Bearer sk-secret1234567",
            },
            seq=1,
            session="probe",
            ts=1.0,
        ),
    )
    human = _cli(cli_env, "doctor")
    assert "registry mismatches: 1" in human.stdout
    assert "feature=tools" in human.stdout
    assert "sk-secret1234567" not in human.stdout

    result = _cli(cli_env, "doctor", "--json")
    report = json.loads(result.stdout)
    summary = report["registry_mismatches"]
    assert summary["count"] == 1
    assert summary["by_provider"] == {"anthropic": 1}
    assert "sk-secret1234567" not in result.stdout


def test_models_sessions_agents_and_ext(cli_env):
    models = _cli(cli_env, "models", "list")
    assert models.returncode == 0
    tiers = _cli(cli_env, "models", "tiers")
    assert "default:" in tiers.stdout
    assert "low" in tiers.stdout

    sessions = _cli(cli_env, "sessions", "list")
    assert sessions.returncode == 0

    agents = _cli(cli_env, "agents", "list")
    assert agents.returncode == 0

    tools = _cli(cli_env, "tools", "list")
    assert tools.returncode == 0
    assert "Read" in tools.stdout

    ext = _cli(cli_env, "ext", "list")
    assert "generation" in ext.stdout


def test_models_select_sets_a_session_model(cli_env):
    result = _cli(cli_env, "models", "select", "scripted/alt", "--session", "s")
    assert "scripted/alt" in result.stdout
    assert "session s" in result.stdout

    # An invalid reference is a clean non-zero error, not a traceback or a change.
    bad = _cli(cli_env, "models", "select", "nope/x", "--session", "s", check=False)
    assert bad.returncode == 1
    assert "nope" in bad.stderr


def test_ext_trash_over_the_daemon(cli_env):
    workspace, _home, _socket = cli_env
    tools = workspace / ".agents" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    target = tools / "scratch.py"
    target.write_text(
        "from nexus.tools.spec import ToolExecutionResult\n"
        "SPEC = {'name': 'Scratch', 'description': 'd', "
        "'input_schema': {'type': 'object'}, 'bundle': 'ext'}\n"
        "async def run(args, ctx):\n"
        "    return ToolExecutionResult.text('ok')\n",
        encoding="utf-8",
    )

    result = _cli(cli_env, "ext", "trash", ".agents/tools/scratch.py")

    assert "trashed" in result.stdout
    assert "delete_after" in result.stdout
    assert not target.exists()
    from nexus.config.paths import project_state_dir

    entries = list(
        (project_state_dir(workspace, _home) / "trash" / "extensions").iterdir()
    )
    assert entries and (entries[0] / "meta.json").exists()


def test_daemon_status_reports_the_running_daemon(cli_env):
    result = _cli(cli_env, "daemon", "status", "--json")
    report = json.loads(result.stdout)
    assert report["running"] is True
    assert report["pid"] > 0


def test_daemon_restart_replaces_the_running_daemon(cli_env):
    before = json.loads(_cli(cli_env, "daemon", "status", "--json").stdout)["pid"]
    result = _cli(cli_env, "daemon", "restart")
    assert result.stdout.startswith("restarted pid=")
    after = json.loads(_cli(cli_env, "daemon", "status", "--json").stdout)
    assert after["running"] is True
    assert after["pid"] != before
    assert f"pid={after['pid']}" in result.stdout


def test_replay_reconstructs_the_transcript(cli_env):
    _cli(cli_env, "run", "hi", "--session", "replayme")
    replayed = _cli(cli_env, "sessions", "replay", "replayme")
    assert "pong" in replayed.stdout


def test_no_codex_binary_or_acp_is_needed_for_regular_commands():
    # Auth is local-only. The daemon-backed path must not invoke a Codex CLI/ACP.
    source = (REPO_ROOT / "nexus" / "cli.py").read_text(encoding="utf-8")
    assert "opencode acp" not in source.lower()
    daemon_source = (REPO_ROOT / "nexus" / "host" / "daemon.py").read_text(encoding="utf-8")
    assert "codex" not in daemon_source.lower()
    # The legacy modules are gone from the package tree.
    assert not (REPO_ROOT / "nexus" / "agent.py").exists()
    assert not (REPO_ROOT / "nexus" / "provider.py").exists()
    assert not (REPO_ROOT / "nexus" / "store.py").exists()
    assert not (REPO_ROOT / "nexus" / "model" / "providers" / "legacy_codex_cli.py").exists()
    assert not (REPO_ROOT / "nexus" / "ui" / "native.py").exists()
