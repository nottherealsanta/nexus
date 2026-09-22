"""Phase 2 Packet F: native CLI adapter tests (no network).

These exercise the opt-in ``nexus native-run`` / ``nexus native-chat`` adapter
against an injected ``Runtime`` + ``ScriptedProvider``. The legacy ``run`` /
``chat`` path is tested unchanged in ``test_cli.py``.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nexus.cli import build_parser
from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection
from nexus.errors import ProviderError
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime
from nexus.ui import native

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def scripted_config(*, model: str = "scripted/sm", permissions=None) -> Config:
    v2 = ConfigV2(model=ModelSection(default=model))
    if permissions is not None:
        v2 = ConfigV2(model=ModelSection(default=model), permissions=permissions)
    return Config(model=model, version=2, v2=v2)


def make_runtime(tmp_path: Path, provider, *, config: Config | None = None) -> Runtime:
    return Runtime(
        tmp_path,
        config=config or scripted_config(),
        providers={"scripted": provider},
    )


def native_run_args(tmp_path: Path, message: str = "go", *, json_output: bool = False):
    argv = ["--workspace", str(tmp_path), "native-run", message]
    if json_output:
        argv.append("--json")
    return build_parser().parse_args(argv)


def native_chat_args(tmp_path: Path):
    return build_parser().parse_args(["--workspace", str(tmp_path), "native-chat"])


def scripted_reader(*answers: str):
    queue = iter(answers)

    def read_line(prompt, stream):
        return next(queue)

    return read_line


# ---------------------------------------------------------------------------
# Parser / help
# ---------------------------------------------------------------------------


def test_parser_lists_native_commands():
    parser = build_parser()
    help_text = parser.format_help()
    assert "native-run" in help_text
    assert "native-chat" in help_text

    one_shot = parser.parse_args(["native-run", "hello"])
    assert one_shot.command == "native-run"
    assert one_shot.message == "hello"
    assert one_shot.json is False

    chat = parser.parse_args(["native-chat", "--session", "work"])
    assert chat.command == "native-chat"
    assert chat.session == "work"


def test_cli_help_subprocess_lists_native_commands():
    result = subprocess.run(
        [sys.executable, "-m", "nexus", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "native-run" in result.stdout
    assert "native-chat" in result.stdout


def test_legacy_cli_import_does_not_eagerly_load_runtime():
    script = """
        import sys
        before = set(sys.modules)
        import nexus.cli
        after = set(sys.modules)
        loaded = after - before
        assert "nexus.runtime" not in loaded, "runtime imported eagerly"
        assert "httpx" not in loaded, "httpx imported eagerly"
        assert "nexus.ui.native" not in loaded, "native adapter imported eagerly"
        print("ok")
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# One-shot native run: text and JSON
# ---------------------------------------------------------------------------


def test_native_run_text_renders_final_text_once(tmp_path, capsys):
    provider = ScriptedProvider(text_response("hello"))
    args = native_run_args(tmp_path, "hi")

    code = native.run_native(
        args, runtime_factory=lambda workspace: make_runtime(tmp_path, provider)
    )

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == "hello\n"
    assert provider.calls == 1


def test_native_run_json_emits_full_envelopes_in_persisted_order(tmp_path, capsys):
    provider = ScriptedProvider(text_response("hi"))
    runtime = make_runtime(tmp_path, provider)
    args = native_run_args(tmp_path, "go", json_output=True)

    code = native.run_native(args, runtime_factory=lambda workspace: runtime)

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    envelopes = [json.loads(line) for line in lines]
    types = [envelope["type"] for envelope in envelopes]

    assert types[0] == "turn.started"
    assert types[-1] == "turn.completed"

    session = runtime.session("default")
    assert [event.type for event in session.events] == types
    assert [envelope["seq"] for envelope in envelopes] == [
        event.seq for event in session.events
    ]
    assert all(envelope["id"] for envelope in envelopes)
    assert all(isinstance(envelope["ts"], float) for envelope in envelopes)


class _FakeSession:
    """Minimal session double for renderer-level tests."""

    def __init__(self, events):
        self._events = list(events)
        self.resolved: list[tuple[str, object]] = []

    async def _send(self, message):
        for event in self._events:
            yield event

    def send(self, message):
        return self._send(message)

    def resolve_permission(self, request_id, decision):
        self.resolved.append((request_id, decision))
        return True


def test_native_json_tolerates_unknown_events(capsys):
    from nexus.events import Event

    session = _FakeSession(
        [
            Event("turn.started"),
            Event("future.unknown", {"payload": 1}),
            Event("turn.completed", {"stop_reason": "end_turn"}),
        ]
    )

    terminal = asyncio.run(
        native._consume(
            session,
            "go",
            json_output=True,
            approver=None,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )

    assert terminal is not None and terminal.type == "turn.completed"
    types = [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()]
    assert types == ["turn.started", "future.unknown", "turn.completed"]


# ---------------------------------------------------------------------------
# Interactive approvals
# ---------------------------------------------------------------------------


def _ask_config() -> Config:
    return scripted_config(permissions=PermissionsSection(mode="ask"))


def test_native_run_interactive_allow_resolves_and_unblocks(tmp_path, capsys):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=_ask_config())
    args = native_run_args(tmp_path, "write")

    code = native.run_native(
        args,
        runtime_factory=lambda workspace: runtime,
        read_line=scripted_reader("y"),
    )

    assert code == 0
    assert (tmp_path / "out.txt").read_text() == "x"
    err = capsys.readouterr().err
    assert "Permission requested" in err
    assert "key: " in err
    assert "-> allow_once" in err


def test_native_run_interactive_deny_keeps_turn_alive(tmp_path, capsys):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=_ask_config())
    args = native_run_args(tmp_path, "write")

    code = native.run_native(
        args,
        runtime_factory=lambda workspace: runtime,
        read_line=scripted_reader("n"),
    )

    # A denial is a model-visible tool result, not a process failure.
    assert code == 0
    assert not (tmp_path / "out.txt").exists()
    assert "-> deny_once" in capsys.readouterr().err


def test_native_run_allow_always_persists_across_turns(tmp_path, capsys):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "one"})),
        text_response("done-1"),
        tool_response(("c2", "Write", {"path": "out.txt", "content": "two"})),
        text_response("done-2"),
    )
    # ``run_native`` closes the runtime it is given, and a closed runtime is
    # terminal, so each turn gets a fresh runtime. The persisted session log is
    # what carries the ``*_ALWAYS`` grant across turns.
    def factory(workspace):
        return make_runtime(tmp_path, provider, config=_ask_config())

    prompts: list[int] = []

    def read_line(prompt, stream):
        prompts.append(1)
        return "a"

    first = native.run_native(
        native_run_args(tmp_path, "write"),
        runtime_factory=factory,
        read_line=read_line,
    )
    second = native.run_native(
        native_run_args(tmp_path, "again"),
        runtime_factory=factory,
        read_line=read_line,
    )

    assert first == 0 and second == 0
    assert (tmp_path / "out.txt").read_text() == "two"
    # The session-scoped grant replays, so the second turn never prompted.
    assert len(prompts) == 1


def test_native_run_resolves_whole_batch_while_stream_is_live(tmp_path, capsys):
    provider = ScriptedProvider(
        tool_response(
            ("c1", "Write", {"path": "a.txt", "content": "A"}),
            ("c2", "Write", {"path": "b.txt", "content": "B"}),
        ),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=_ask_config())

    code = native.run_native(
        native_run_args(tmp_path, "write both"),
        runtime_factory=lambda workspace: runtime,
        read_line=scripted_reader("y", "n"),
    )

    assert code == 0
    assert (tmp_path / "a.txt").read_text() == "A"
    assert not (tmp_path / "b.txt").exists()
    err = capsys.readouterr().err
    assert "Permission requested (#1)" in err
    assert "Permission requested (#2)" in err
    assert "-> allow_once" in err and "-> deny_once" in err


def test_native_run_eof_denies_and_never_allows(tmp_path, capsys):
    provider = ScriptedProvider(
        tool_response(("c1", "Bash", {"command": "echo hi > out.txt"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=_ask_config())
    args = native_run_args(tmp_path, "run")

    def eof(prompt, stream):
        raise EOFError

    code = native.run_native(
        args, runtime_factory=lambda workspace: runtime, read_line=eof
    )

    assert code == 0
    assert not (tmp_path / "out.txt").exists()
    err = capsys.readouterr().err
    assert "input closed; denying this request once" in err
    assert "-> allow" not in err


def test_native_run_invalid_input_falls_back_to_deny_once(tmp_path, capsys):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=_ask_config())

    code = native.run_native(
        native_run_args(tmp_path, "write"),
        runtime_factory=lambda workspace: runtime,
        read_line=scripted_reader("wat", "huh", "nope"),
    )

    assert code == 0
    assert not (tmp_path / "out.txt").exists()
    err = capsys.readouterr().err
    assert "too many invalid entries; denying this request once" in err


def test_native_run_json_never_prompts_and_applies_unattended(tmp_path, capsys):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("ok"),
    )
    config = scripted_config(
        permissions=PermissionsSection(mode="ask", on_unattended="deny")
    )
    runtime = make_runtime(tmp_path, provider, config=config)

    def forbid_prompt(prompt, stream):
        raise AssertionError("JSON mode must never prompt")

    code = native.run_native(
        native_run_args(tmp_path, "write", json_output=True),
        runtime_factory=lambda workspace: runtime,
        read_line=forbid_prompt,
    )

    assert code == 0
    assert not (tmp_path / "out.txt").exists()
    out = capsys.readouterr().out
    assert "Permission requested" not in out
    envelopes = [json.loads(line) for line in out.splitlines()]
    resolved = [e for e in envelopes if e["type"] == "permission.resolved"]
    assert resolved and resolved[0]["data"]["code"] == "unattended_deny"


# ---------------------------------------------------------------------------
# Failures and exit codes
# ---------------------------------------------------------------------------


def test_native_run_failed_turn_exits_nonzero(tmp_path, capsys):
    provider = ScriptedProvider([ProviderError("boom")])
    runtime = make_runtime(tmp_path, provider)

    code = native.run_native(
        native_run_args(tmp_path, "go"),
        runtime_factory=lambda workspace: runtime,
    )

    assert code == 1
    assert "boom" in capsys.readouterr().err


def test_native_run_failed_turn_json_keeps_stdout_clean(tmp_path, capsys):
    provider = ScriptedProvider([ProviderError("boom")])
    runtime = make_runtime(tmp_path, provider)

    code = native.run_native(
        native_run_args(tmp_path, "go", json_output=True),
        runtime_factory=lambda workspace: runtime,
    )

    assert code == 1
    captured = capsys.readouterr()
    envelopes = [json.loads(line) for line in captured.out.splitlines()]
    assert envelopes[-1]["type"] == "turn.failed"
    assert "boom" in captured.err


# ---------------------------------------------------------------------------
# Interactive chat
# ---------------------------------------------------------------------------


def test_native_chat_multiple_turns_then_exit(tmp_path, capsys):
    provider = ScriptedProvider(text_response("a"), text_response("b"))
    runtime = make_runtime(tmp_path, provider)

    code = native.chat_native(
        native_chat_args(tmp_path),
        runtime_factory=lambda workspace: runtime,
        read_line=scripted_reader("one", "two", "/exit"),
    )

    assert code == 0
    assert provider.calls == 2
    session = runtime.session("default")
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    out = capsys.readouterr().out
    assert "a" in out and "b" in out


def test_native_chat_eof_exits_cleanly(tmp_path, capsys):
    provider = ScriptedProvider(text_response("unused"))
    runtime = make_runtime(tmp_path, provider)

    def eof(prompt, stream):
        raise EOFError

    code = native.chat_native(
        native_chat_args(tmp_path),
        runtime_factory=lambda workspace: runtime,
        read_line=eof,
    )

    assert code == 0
    assert provider.calls == 0


def test_native_chat_reports_turn_failure_and_continues(tmp_path, capsys):
    provider = ScriptedProvider(
        [ProviderError("boom")],
        text_response("recovered"),
    )
    runtime = make_runtime(tmp_path, provider)

    code = native.chat_native(
        native_chat_args(tmp_path),
        runtime_factory=lambda workspace: runtime,
        read_line=scripted_reader("first", "second", "/exit"),
    )

    assert code == 0
    assert provider.calls == 2
    assert "boom" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Runtime lifecycle: closes on success, failure, and interrupt
# ---------------------------------------------------------------------------


class _TrackingRuntime:
    """Wrap a real Runtime, recording aclose without changing behaviour."""

    def __init__(self, inner: Runtime):
        self._inner = inner
        self.closed = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def aclose(self):
        self.closed += 1
        await self._inner.aclose()


def test_runtime_closes_on_success(tmp_path):
    provider = ScriptedProvider(text_response("hi"))
    runtime = _TrackingRuntime(make_runtime(tmp_path, provider))

    code = native.run_native(
        native_run_args(tmp_path, "go"),
        runtime_factory=lambda workspace: runtime,
    )

    assert code == 0
    assert runtime.closed == 1


def test_runtime_closes_on_failure(tmp_path):
    provider = ScriptedProvider([ProviderError("boom")])
    runtime = _TrackingRuntime(make_runtime(tmp_path, provider))

    code = native.run_native(
        native_run_args(tmp_path, "go"),
        runtime_factory=lambda workspace: runtime,
    )

    assert code == 1
    assert runtime.closed == 1


def test_runtime_closes_on_interrupt(tmp_path, monkeypatch):
    provider = ScriptedProvider(text_response("hi"))
    runtime = _TrackingRuntime(make_runtime(tmp_path, provider))

    async def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(native, "_run_once", interrupted)

    with pytest.raises(KeyboardInterrupt):
        native.run_native(
            native_run_args(tmp_path, "go"),
            runtime_factory=lambda workspace: runtime,
        )

    assert runtime.closed == 1


def test_native_chat_interrupt_closes_runtime(tmp_path):
    provider = ScriptedProvider(text_response("unused"))
    runtime = _TrackingRuntime(make_runtime(tmp_path, provider))

    def interrupt(prompt, stream):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        native.chat_native(
            native_chat_args(tmp_path),
            runtime_factory=lambda workspace: runtime,
            read_line=interrupt,
        )

    assert runtime.closed == 1
    assert provider.calls == 0

