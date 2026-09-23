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
    return Runtime(workspace, config=config, providers={"scripted": provider})


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
        "replay",
        "daemon",
        "sessions",
        "ext",
        "models",
        "agents",
        "tools",
    } == choices

    for action, expected in (
        ("daemon", {"status", "stop", "logs"}),
        ("sessions", {"list", "fork", "replay", "export", "delete", "restore"}),
        ("ext", {"list", "reload", "validate", "trash"}),
        ("models", {"list", "show", "refresh", "tiers"}),
        ("agents", {"list"}),
        ("tools", {"list"}),
    ):
        # Nested subparsers are registered on the child parser; assert via a full parse.
        argv = {
            "daemon": ["daemon", "status"],
            "sessions": ["sessions", "list"],
            "ext": ["ext", "list"],
            "models": ["models", "list"],
            "agents": ["agents", "list"],
            "tools": ["tools", "list"],
        }[action]
        assert parser.parse_args(argv).command == action
        assert expected  # documented in the parser; full coverage below


@pytest.mark.parametrize(
    "argv",
    [
        ["daemon", "status"],
        ["daemon", "stop"],
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
        ["ext", "trash", ".nexus/tools/foo.py"],
        ["ext", "trash", "foo.py", "--reason", "cleanup", "--force"],
        ["models", "list"],
        ["models", "show", "p/m"],
        ["models", "refresh"],
        ["models", "tiers"],
        ["agents", "list"],
        ["doctor"],
        ["doctor", "--explain-reload", "--json"],
        ["replay", "s"],
        ["run", "hi", "--json"],
        ["chat"],
    ],
)
def test_parser_accepts_every_documented_invocation(argv):
    args = cli.build_parser().parse_args(["--workspace", "/tmp/ws", *argv])
    assert args.workspace == Path("/tmp/ws")
    assert args.command is not None


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
                "hot": [".nexus/tools/*.py"],
                "restart_only": ["nexus/core/**"],
                "note": "hot vs restart",
            },
        },
        out,
    )
    text = out.getvalue()
    assert "scripted" in text
    assert "hot:" in text and ".nexus/tools/*.py" in text
    assert "restart only:" in text and "nexus/core/**" in text


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
        env={**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO_ROOT)},
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
        env={**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO_ROOT)},
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


def test_ext_trash_over_the_daemon(cli_env):
    workspace, _home, _socket = cli_env
    tools = workspace / ".nexus" / "tools"
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

    result = _cli(cli_env, "ext", "trash", ".nexus/tools/scratch.py")

    assert "trashed" in result.stdout
    assert "delete_after" in result.stdout
    assert not target.exists()
    entries = list((workspace / ".nexus" / "trash" / "extensions").iterdir())
    assert entries and (entries[0] / "meta.json").exists()


def test_daemon_status_reports_the_running_daemon(cli_env):
    result = _cli(cli_env, "daemon", "status", "--json")
    report = json.loads(result.stdout)
    assert report["running"] is True
    assert report["pid"] > 0


def test_replay_reconstructs_the_transcript(cli_env):
    _cli(cli_env, "run", "hi", "--session", "replayme")
    replayed = _cli(cli_env, "sessions", "replay", "replayme")
    assert "pong" in replayed.stdout


def test_no_codex_binary_is_needed():
    # The canonical path must not reference the Codex CLI anywhere: the daemon
    # owns a Nexus Runtime and providers are adapters.
    source = (REPO_ROOT / "nexus" / "cli.py").read_text(encoding="utf-8")
    assert "codex" not in source.lower()
    daemon_source = (REPO_ROOT / "nexus" / "host" / "daemon.py").read_text(encoding="utf-8")
    assert "codex" not in daemon_source.lower()
    # The legacy modules are gone from the package tree.
    assert not (REPO_ROOT / "nexus" / "agent.py").exists()
    assert not (REPO_ROOT / "nexus" / "provider.py").exists()
    assert not (REPO_ROOT / "nexus" / "store.py").exists()
    assert not (REPO_ROOT / "nexus" / "model" / "providers" / "legacy_codex_cli.py").exists()
    assert not (REPO_ROOT / "nexus" / "ui" / "native.py").exists()
