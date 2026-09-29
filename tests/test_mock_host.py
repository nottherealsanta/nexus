"""Dev-mode gating, the Mock* host commands, the provider's fail-closed routing,
the sandbox and the DSL (MOCK_PLAN §4-§7, §9)."""
from __future__ import annotations

import importlib
import re

import pytest

from nexus.devtools.mock import MockProvider, MockRouteViolation, catalog
from nexus.devtools.mock.directive import Directive, format_directive, parse_directive
from nexus.devtools.mock.dsl import Dyn, Turn
from nexus.devtools.mock.sandbox import (
    ensure_sandbox,
    reset_sandbox,
    restore_sandbox,
    sandbox_path,
    tree_hash,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.message import Message, Text
from nexus.model.request import ModelRequest
from nexus.runtime import Runtime


def _runtime(tmp_path, monkeypatch, *, dev=True):
    home = tmp_path / "home"
    monkeypatch.setenv("NEXUS_HOME", str(home))
    if dev:
        monkeypatch.setenv("NEXUS_DEV", "1")
    else:
        monkeypatch.delenv("NEXUS_DEV", raising=False)
    sandbox = ensure_sandbox(home)
    env = {"NEXUS_DEV": "1"} if dev else {}
    return Runtime(sandbox, home=home.parent, environ=env), home, sandbox


# -- gating ------------------------------------------------------------------


async def test_mock_commands_are_refused_outside_dev_mode(tmp_path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path, monkeypatch, dev=False)
    facade = HostFacade(runtime)
    try:
        for command in (p.MockList(), p.MockStart(scenario="hello"), p.MockClean()):
            result = await facade.handle(command)
            assert isinstance(result, p.ErrorResult) and "dev mode" in result.message
        assert facade.health()["dev"] is False
        assert "mock" not in runtime.providers
    finally:
        await runtime.aclose()


def test_mock_slash_command_exists_only_in_dev_mode(monkeypatch):
    from nexus.ui.cli import commands

    try:
        monkeypatch.delenv("NEXUS_DEV", raising=False)
        importlib.reload(commands)
        assert "/mock" not in commands.BY_NAME
        assert "/mock" not in commands.help_text()
        monkeypatch.setenv("NEXUS_DEV", "1")
        importlib.reload(commands)
        assert commands.parse("/mock parallel-subagents --speed 0").args == ("parallel-subagents", "--speed", "0")
        assert "/mock" in commands.help_text()
    finally:
        monkeypatch.delenv("NEXUS_DEV", raising=False)
        importlib.reload(commands)


# -- host commands -----------------------------------------------------------


async def test_mock_list_start_and_clean(tmp_path, monkeypatch):
    runtime, _home, sandbox = _runtime(tmp_path, monkeypatch)
    facade = HostFacade(runtime)
    try:
        assert facade.health()["dev"] is True
        listed = await facade.handle(p.MockList())
        names = {row.name for row in listed.scenarios}
        assert {"hello", "tool-marathon", "parallel-subagents"} <= names

        bad = await facade.handle(p.MockStart(scenario="nope"))
        assert isinstance(bad, p.ErrorResult) and "unknown mock scenario" in bad.message
        invalid = await facade.handle(p.MockStart(scenario="hello", session="../x"))
        assert isinstance(invalid, p.ErrorResult)

        started = await facade.handle(p.MockStart(scenario="diff-review", speed=0))
        assert isinstance(started, p.MockStartResult) and started.session == "mock-diff-review-1"
        again = await facade.handle(p.MockStart(scenario="hello", speed=0))
        assert again.session == "mock-hello-1"
        await facade.wait_idle(timeout=30)
        assert "Hello, nexus!" not in (sandbox / "src/app.py").read_text()  # greet body edited
        assert (sandbox / "src/newmod.py").exists()

        cleaned = await facade.handle(p.MockClean())
        assert cleaned.restored and not (sandbox / "src/newmod.py").exists()
        assert 'return f"hello, {name}"' in (sandbox / "src/app.py").read_text()
    finally:
        await runtime.aclose()


# -- provider ----------------------------------------------------------------


def _req(text):
    return ModelRequest(messages=[Message(role="user", content=[Text(text)])], model="mock/hello")


async def test_provider_fails_closed_without_a_directive():
    provider = MockProvider()
    with pytest.raises(MockRouteViolation):
        async for _ in provider.stream(_req("just a normal prompt")):
            pass
    with pytest.raises(MockRouteViolation):
        async for _ in provider.stream(_req(format_directive(Directive("no-such-scenario")))):
            pass


async def test_provider_is_stateless_and_keyed_by_the_conversation():
    provider = MockProvider()
    req = _req("go\n" + format_directive(Directive("hello", "main", 0.0)))
    first = [e async for e in provider.stream(req)]
    again = [e async for e in provider.stream(req)]
    assert first == again  # no cursor: same conversation, same reply


def test_directive_round_trip():
    directive = Directive("parallel-subagents", "worker-3", 0.5, 7)
    assert parse_directive(f"text before\n{format_directive(directive)}\nafter") == directive
    assert parse_directive("no directive here") is None
    assert parse_directive("⟦mock actor=x⟧") is None  # scenario is required


# -- sandbox -----------------------------------------------------------------


def test_sandbox_seed_restore_and_reset_guards(tmp_path):
    home = tmp_path / "dev"
    root = ensure_sandbox(home)
    assert (root / ".git").is_dir() and (root / "src/app.py").is_file()
    seeded = tree_hash(root)
    (root / "src/app.py").write_text("changed")
    (root / "junk.txt").write_text("x")
    assert restore_sandbox(root, home) and tree_hash(root) == seeded
    with pytest.raises(ValueError):
        restore_sandbox(tmp_path, home)  # not the sandbox
    sibling = home / "sandbox" / "keep.txt"
    sibling.write_text("keep")
    (root / "junk.txt").write_text("x")
    assert reset_sandbox(home) == root and not (root / "junk.txt").exists()
    assert sibling.read_text() == "keep"  # reset only ever touches <home>/sandbox/workspace
    assert sandbox_path(home) == home / "sandbox" / "workspace"


# -- catalogue hygiene -------------------------------------------------------

_ALLOWED_COMMANDS = {"echo", "python3", "git", "sleep", "cat", "grep", "wc", "ls"}


def _calls(step):
    if isinstance(step, Turn):
        yield from step.calls


def test_scenario_bash_commands_are_allowlisted_and_stay_in_the_sandbox():
    checked = 0
    for scenario in catalog().values():
        for steps in scenario.actors.values():
            for step in steps:
                for item in _calls(step):
                    if item.name != "bash":
                        continue
                    command = item.input.get("command", "")
                    for part in re.split(r"&&|\|\||;|\|", command):
                        assert part.split()[0] in _ALLOWED_COMMANDS, (scenario.name, command)
                    assert "/" not in command.replace("</", ""), (scenario.name, command)
                    assert ".." not in command and "curl" not in command and "http" not in command
                    checked += 1
    assert checked >= 4


def test_scenarios_never_reach_the_network_or_outside_paths():
    for scenario in catalog().values():
        for steps in scenario.actors.values():
            for step in steps:
                for item in _calls(step):
                    assert item.name not in {"webfetch", "websearch"}, scenario.name
                    path = str(item.input.get("path", ""))
                    if scenario.name != "errors":  # `errors` escapes on purpose to prove it is refused
                        assert not path.startswith(("/", "~")) and ".." not in path, (scenario.name, path)


def test_every_scenario_has_a_final_dyn_verdict_and_valid_metadata():
    for name, scenario in catalog().items():
        assert scenario.name == name and scenario.summary and scenario.prompt
        assert "main" in scenario.actors
        assert isinstance(scenario.actors["main"][-1], Dyn) or name == "provider-failure", name
