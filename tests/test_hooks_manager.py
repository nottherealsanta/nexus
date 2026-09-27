"""Phase 6 P6B: the lifecycle hook manager.

Covers the plan section 5.7 contract:

* the nine lifecycle events and the ``HookDecision`` shape
  (``allow``/``warn``/``block``/``modify``), with ``modify`` requiring the
  caller to revalidate the schema and re-run the permission gate;
* restricted ``hooks.toml`` discovery (unknown events/keys refused, command
  hooks are argv with no shell by default);
* in-process ``.nexus/hooks/*.py`` loaded through the quarantine / version-
  stamped import seam, with workspace-over-user precedence, object reuse for
  unchanged bytes, and a trusted-code warning;
* command execution that is bounded (env/stdin/output), deadline- and
  cancel-aware, and tears down the whole process group (descendants included);
* isolated, sanitized failures: a broken file, timeout, cancel, secret-laden
  output, or control characters never escape a run;
* matcher evaluation structurally reusing the permission rule grammar, and a
  deterministic hook order.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from nexus.core.cancel import CancelToken
from nexus.errors import ManagerClosed, OperationCancelled
from nexus.ext.manifest import ModuleHandle
from nexus.hooks import (
    HOOK_EVENTS,
    MODIFIABLE_EVENTS,
    TRUSTED_CODE_WARNING,
    HookAction,
    HookDecision,
    HookError,
    HookInvocation,
    HookManager,
    HookModule,
    HookSpec,
)
from nexus.hooks.manager import HookModuleLoader

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hooks"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def install_fixture(tmp_path: Path, stem: str, *, name: str | None = None) -> Path:
    target_dir = tmp_path / ".nexus" / "hooks"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name or stem}.py"
    shutil.copyfile(FIXTURES / f"{stem}.py", target)
    return target


def write_hook_module(tmp_path: Path, stem: str, body: str) -> Path:
    target_dir = tmp_path / ".nexus" / "hooks"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{stem}.py"
    target.write_text(body, encoding="utf-8")
    return target


def write_script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def render_toml(entries: list[str]) -> str:
    return "\n\n".join(entries) + "\n"


def toml_command_hook(
    event: str,
    *,
    command: object,
    name: str | None = None,
    matcher: str | None = None,
    type_: str | None = None,
    on_nonzero: str | None = None,
    timeout_s: object | None = None,
    env: dict[str, str] | None = None,
    shell: bool | None = None,
    cwd: str | None = None,
) -> str:
    lines = [f"[[hooks.{event}]]"]
    if name is not None:
        lines.append(f"name = {json.dumps(name)}")
    if type_ is not None:
        lines.append(f"type = {json.dumps(type_)}")
    if matcher is not None:
        lines.append(f"matcher = {json.dumps(matcher)}")
    lines.append(f"command = {json.dumps(command)}")
    if shell is not None:
        lines.append(f"shell = {'true' if shell else 'false'}")
    if on_nonzero is not None:
        lines.append(f"on_nonzero = {json.dumps(on_nonzero)}")
    if timeout_s is not None:
        lines.append(f"timeout_s = {timeout_s!r}")
    if cwd is not None:
        lines.append(f"cwd = {json.dumps(cwd)}")
    if env:
        pairs = ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in env.items())
        lines.append(f"env = {{ {pairs} }}")
    return "\n".join(lines)


def write_toml(tmp_path: Path, entries: list[str]) -> Path:
    path = tmp_path / ".nexus" / "hooks.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_toml(entries), encoding="utf-8")
    return path


def command_script(tmp_path: Path, name: str, body: str) -> list[str]:
    return [sys.executable, write_script(tmp_path, name, body)]


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event) -> None:
        self.events.append((event.type, dict(event.data)))

    def types(self) -> list[str]:
        return [name for name, _ in self.events]


def make_manager(tmp_path: Path, **kwargs) -> HookManager:
    return HookManager(tmp_path, **kwargs)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_plan_lifecycle_events_exact():
    assert HOOK_EVENTS == (
        "SessionStart",
        "UserPromptSubmit",
        "ContextAssembled",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "TurnEnd",
        "SessionEnd",
        "ExtensionLoaded",
    )
    assert MODIFIABLE_EVENTS == {"PreToolUse", "UserPromptSubmit", "PreCompact"}


def test_decision_constructors_and_roundtrip():
    assert HookDecision.allow().action is HookAction.ALLOW
    assert HookDecision.warn("careful").action is HookAction.WARN
    assert HookDecision.block("no").action is HookAction.BLOCK
    modify = HookDecision.modify({"x": 1})
    assert modify.modifies and dict(modify.new_input) == {"x": 1}
    assert modify.to_dict()["new_input"] == {"x": 1}


def test_decision_modify_requires_input():
    with pytest.raises(HookError):
        HookDecision(action=HookAction.MODIFY)
    with pytest.raises(HookError):
        HookDecision(action="modify")


def test_decision_aliases_and_action_coercion():
    assert HookDecision(action="deny").action is HookAction.BLOCK
    assert HookDecision(action="approve").action is HookAction.ALLOW
    with pytest.raises(HookError):
        HookDecision(action="explode")


def test_spec_matcher_reuses_permission_grammar():
    spec = HookSpec(
        event="PreToolUse", name="m", kind="command", command=("true",), matcher="Write(**)"
    )
    assert spec.matches("Write", "/ws/a.py", "fs")
    assert not spec.matches("Read", "/ws/a.py", "fs")
    bundle = HookSpec(
        event="PreToolUse", name="b", kind="command", command=("true",), matcher="Bundle:fs"
    )
    assert bundle.matches("Read", None, "fs")
    assert not bundle.matches("Read", None, "shell")
    wildcard = HookSpec(
        event="PreToolUse", name="w", kind="command", command=("true",), matcher="mcp__srv__*"
    )
    assert wildcard.matches("mcp__srv__list", None, None)
    assert not wildcard.matches("Read", None, None)


@pytest.mark.parametrize(
    ("matcher", "action", "matches"),
    [
        ("BashOutput", "status", True),
        ("BashOutput", "wait", True),
        ("BashOutput", "stop", False),
        ("BashOutput", "run", False),
        ("KillShell", "stop", True),
        ("KillShell", "status", False),
        ("KillShell", "wait", False),
        ("KillShell", "run", False),
    ],
)
async def test_legacy_bash_job_matchers_use_tool_action(
    tmp_path: Path, matcher: str, action: str, matches: bool
):
    async def allow(invocation, context):
        return HookDecision.allow()

    spec = HookSpec(
        event="PreToolUse",
        name="legacy-job",
        kind="python",
        matcher=matcher,
        fn=allow,
    )
    manager = make_manager(tmp_path)
    outcome = await manager.run(
        "PreToolUse",
        HookInvocation(
            event="PreToolUse",
            tool="bash",
            key=f"{action}:job_1",
            bundle="shell",
            tool_input={"action": action, "job_id": "job_1"},
        ),
        specs=(spec,),
    )
    assert outcome.fired == (("legacy-job",) if matches else ())


def test_command_hook_is_argv_without_shell_by_default():
    spec = HookSpec(event="PreToolUse", name="c", command=("echo", "hi"))
    assert spec.argv == ("echo", "hi")
    assert not spec.shell
    shell = HookSpec(
        event="PreToolUse", name="s", shell=True, shell_command="echo hi"
    )
    assert shell.argv == ("/bin/sh", "-c", "echo hi")


def test_spec_fingerprint_ignores_generation_and_fn():
    left = HookSpec(
        event="PreToolUse", name="x", command=("true",), source="a", index=0, generation=1
    )
    right = HookSpec(
        event="PreToolUse", name="x", command=("true",), source="a", index=0, generation=9
    )
    assert left.fingerprint() == right.fingerprint()
    different = HookSpec(
        event="PreToolUse", name="x", command=("false",), source="a", index=0
    )
    assert left.fingerprint() != different.fingerprint()


def test_invocation_env_is_bounded_context():
    invocation = HookInvocation(
        event="PreToolUse",
        tool="Write",
        key="/ws/a.py",
        bundle="fs",
        session_id="s1",
        turn_id="t1",
    )
    env = invocation.env()
    assert env["NEXUS_HOOK_EVENT"] == "PreToolUse"
    assert env["NEXUS_TOOL_NAME"] == "Write"
    assert env["NEXUS_TOOL_PATH"] == "/ws/a.py"
    assert env["NEXUS_SESSION_ID"] == "s1"


# ---------------------------------------------------------------------------
# Command hooks: discovery and decisions
# ---------------------------------------------------------------------------


async def test_command_hook_allow_emits_fired(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="noop",
                command=command_script(tmp_path, "noop.py", "raise SystemExit(0)\n"),
            )
        ],
    )
    sink = Sink()
    manager = make_manager(tmp_path, sink=sink)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Write", key="/ws/a.py", tool_input={"a": 1})
    assert outcome.allowed
    assert outcome.fired == ("noop",)
    assert "hook.fired" in sink.types()
    assert "hook.blocked" not in sink.types()


async def test_command_hook_nonzero_blocks_with_sanitized_reason(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="gate",
                on_nonzero="block",
                command=command_script(
                    tmp_path,
                    "gate.py",
                    "import sys\nsys.stderr.write('policy says no\\n')\nraise SystemExit(3)\n",
                ),
            )
        ],
    )
    sink = Sink()
    manager = make_manager(tmp_path, sink=sink)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Bash", key="rm -rf /", tool_input={})
    assert outcome.blocked
    assert "policy says no" in outcome.reason
    assert "hook.blocked" in sink.types()


async def test_command_hook_on_nonzero_warn_does_not_block(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="advisory",
                on_nonzero="warn",
                command=command_script(
                    tmp_path, "advisory.py", "raise SystemExit(2)\n"
                ),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Read", tool_input={})
    assert not outcome.blocked
    assert outcome.warnings


async def test_command_hook_modify_json(tmp_path: Path):
    script = (
        "import json\n"
        "print(json.dumps({'decision': 'modify', 'input': {'x': 2}, 'reason': 'rewrote'}))\n"
    )
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse", name="rewrite", command=command_script(tmp_path, "rw.py", script)
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Write", key="/ws/a.py", tool_input={"x": 1})
    assert outcome.decision is HookAction.MODIFY
    assert dict(outcome.modified_input) == {"x": 2}
    assert outcome.requires_revalidation


async def test_command_hook_reads_stdin_context(tmp_path: Path):
    script = (
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(json.dumps({'decision': 'modify', 'input': {**data['tool_input'], 'seen': data['tool']}}))\n"
    )
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse", name="stdin", command=command_script(tmp_path, "stdin.py", script)
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Write", key="/ws/a.py", tool_input={"a": 1})
    assert dict(outcome.modified_input) == {"a": 1, "seen": "Write"}


async def test_command_hook_matcher_filters(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="write_only",
                matcher="Write(**)",
                on_nonzero="block",
                command=command_script(
                    tmp_path, "block_all.py", "raise SystemExit(4)\n"
                ),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    assert (await manager.pre_tool_use(tool="Read", key="/ws/a.py", tool_input={})).allowed
    blocked = await manager.pre_tool_use(tool="Write", key="/ws/a.py", tool_input={})
    assert blocked.blocked


async def test_no_shell_by_default_does_not_execute_metacharacters(tmp_path: Path):
    pwned = tmp_path / "pwned"
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="noshell",
                command=f"echo hi; touch {pwned}",
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Bash", tool_input={})
    assert outcome.allowed
    assert not pwned.exists()


async def test_shell_opt_in_executes_a_shell_command(tmp_path: Path):
    marker = tmp_path / "shelled"
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="shelled",
                shell=True,
                command=f"echo ok > {marker}",
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    await manager.pre_tool_use(tool="Bash", tool_input={})
    assert marker.read_text().strip() == "ok"


# ---------------------------------------------------------------------------
# Bounds, timeout, cancel, descendants
# ---------------------------------------------------------------------------


async def test_command_hook_timeout_blocks_and_is_fast(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="slow",
                on_nonzero="block",
                timeout_s=0.2,
                command=command_script(
                    tmp_path, "slow.py", "import time\ntime.sleep(30)\n"
                ),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    started = time.monotonic()
    outcome = await manager.pre_tool_use(tool="Read", tool_input={})
    elapsed = time.monotonic() - started
    assert outcome.blocked
    assert "timed out" in outcome.reason
    assert elapsed < 5.0


async def test_command_hook_cancel_raises_and_kills(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="slow",
                timeout_s=30,
                command=command_script(
                    tmp_path, "slow.py", "import time\ntime.sleep(30)\n"
                ),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    token = CancelToken()
    task = asyncio.ensure_future(
        manager.pre_tool_use(tool="Read", tool_input={}, cancel=token)
    )
    await asyncio.sleep(0.2)
    token.cancel("user cancelled")
    with pytest.raises(OperationCancelled):
        await asyncio.wait_for(task, timeout=5.0)


async def test_command_hook_kills_descendant_process_group(tmp_path: Path):
    marker = tmp_path / "descendant-marker"
    body = (
        "import subprocess, sys, time\n"
        "subprocess.Popen(['/bin/sh', '-c', 'sleep 1; echo alive > \"$NEXUS_TEST_MARKER\"'])\n"
        "time.sleep(30)\n"
    )
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="forker",
                on_nonzero="warn",
                timeout_s=0.3,
                env={"NEXUS_TEST_MARKER": str(marker)},
                command=command_script(tmp_path, "forker.py", body),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    await manager.pre_tool_use(tool="Bash", tool_input={})
    # The detached child would have written the marker after one second; if the
    # process group was torn down, it never does.
    await asyncio.sleep(1.6)
    assert not marker.exists()


async def test_command_hook_env_is_bounded_and_declared_env_applied(tmp_path: Path):
    marker = tmp_path / "env-marker"
    body = (
        "import os, sys\n"
        "if 'NEXUS_HOST_SECRET' in os.environ:\n"
        "    raise SystemExit(7)\n"
        "if os.environ.get('NEXUS_TOOL_PATH') != '/ws/a.py':\n"
        "    raise SystemExit(8)\n"
        "if os.environ.get('MY_HOOK_FLAG') != 'yes':\n"
        "    raise SystemExit(9)\n"
        "open(os.environ['NEXUS_TEST_MARKER'], 'w').write('ok')\n"
    )
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="envcheck",
                on_nonzero="block",
                env={"MY_HOOK_FLAG": "yes", "NEXUS_TEST_MARKER": str(marker)},
                command=command_script(tmp_path, "envcheck.py", body),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    os.environ["NEXUS_HOST_SECRET"] = "topsecret"
    try:
        outcome = await manager.pre_tool_use(
            tool="Write", key="/ws/a.py", tool_input={}
        )
    finally:
        del os.environ["NEXUS_HOST_SECRET"]
    assert outcome.allowed, outcome.reason
    assert marker.read_text() == "ok"


async def test_command_hook_output_is_bounded(tmp_path: Path):
    body = "import sys\nsys.stdout.write('x' * 200000)\n"
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="loud",
                command=command_script(tmp_path, "loud.py", body),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Read", tool_input={})
    assert outcome.allowed


async def test_command_hook_secrets_and_controls_are_sanitized(tmp_path: Path):
    body = (
        "import sys\n"
        "sys.stderr.write('bad\\x00ctrl sk-abcdefghijklmnop API_KEY=supersecretvalue\\n')\n"
        "raise SystemExit(3)\n"
    )
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="leaky",
                on_nonzero="block",
                command=command_script(tmp_path, "leaky.py", body),
            )
        ],
    )
    sink = Sink()
    manager = make_manager(tmp_path, sink=sink)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Read", tool_input={})
    assert outcome.blocked
    assert "\x00" not in outcome.reason
    assert "sk-abcdefghijklmnop" not in outcome.reason
    assert "supersecretvalue" not in outcome.reason
    blocked_events = [data for name, data in sink.events if name == "hook.blocked"]
    assert blocked_events
    assert "supersecretvalue" not in json.dumps(blocked_events[0])


async def test_command_hook_expands_nexus_variables_in_argv(tmp_path: Path):
    script = (
        "import json, sys\n"
        "print(json.dumps({'decision': 'modify', 'input': {'arg': sys.argv[1]}}))\n"
    )
    program = write_script(tmp_path, "expand.py", script)
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="expand",
                on_nonzero="block",
                env={"MARKER": "from-env"},
                command=[sys.executable, program, "$NEXUS_TOOL_PATH", "${MARKER}"],
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Write", key="/ws/a.py", tool_input={})
    assert outcome.allowed, outcome.reason
    assert dict(outcome.modified_input) == {"arg": "/ws/a.py"}


async def test_command_hook_leaves_unknown_variable_reference_literal(tmp_path: Path):
    script = (
        "import json, sys\n"
        "print(json.dumps({'decision': 'modify', 'input': {'arg': sys.argv[1]}}))\n"
    )
    program = write_script(tmp_path, "expand2.py", script)
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="expand2",
                command=[sys.executable, program, "$NOT_A_NEXUS_VAR"],
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Read", tool_input={})
    assert dict(outcome.modified_input) == {"arg": "$NOT_A_NEXUS_VAR"}


async def test_run_accepts_a_plain_mapping(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="ok",
                command=command_script(tmp_path, "ok2.py", "raise SystemExit(0)\n"),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.run(
        "PreToolUse", {"tool": "Write", "key": "/ws/a.py", "tool_input": {"a": 1}}
    )
    assert outcome.allowed
    assert outcome.fired == ("ok",)


# ---------------------------------------------------------------------------
# In-process Python hooks
# ---------------------------------------------------------------------------


async def test_python_hook_modify_and_trusted_warning(tmp_path: Path):
    install_fixture(tmp_path, "tool_hooks", name="tool_hooks")
    manager = make_manager(tmp_path)
    manager.refresh()
    assert TRUSTED_CODE_WARNING in manager.warnings
    assert any(row["name"] == "trusted_code" for row in manager.diagnostics())
    outcome = await manager.pre_tool_use(
        tool="Write", key="/ws/a.py", tool_input={"a": 1}
    )
    assert outcome.decision is HookAction.MODIFY
    assert dict(outcome.modified_input) == {"a": 1, "reviewed": True}
    assert outcome.requires_revalidation
    post = await manager.post_tool_use(tool="Write", key="/ws/a.py", tool_input={})
    assert post.warnings


async def test_python_hook_register_form_blocks(tmp_path: Path):
    install_fixture(tmp_path, "register_hooks")
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Bash", key="ls", tool_input={})
    assert outcome.blocked
    assert "registered hook" in outcome.reason


async def test_python_hook_non_tool_event_dispatch(tmp_path: Path):
    install_fixture(tmp_path, "tool_hooks", name="tool_hooks")
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.dispatch("SessionStart", data={"x": 1})
    assert outcome.allowed
    assert "async_allow" in outcome.fired


async def test_python_hook_bad_return_is_isolated(tmp_path: Path):
    install_fixture(tmp_path, "bad_return")
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Read", tool_input={})
    assert not outcome.blocked
    assert outcome.failures


async def test_python_hook_broken_syntax_is_isolated(tmp_path: Path):
    write_hook_module(tmp_path, "broken_syntax", "def nope(:\n    pass\n")
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    assert any(row["kind"] == "hook" for row in manager.diagnostics())


async def test_python_hook_import_error_is_isolated(tmp_path: Path):
    install_fixture(tmp_path, "import_error")
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    rows = manager.diagnostics()
    assert rows and "import-time boom" not in json.dumps(rows)


async def test_python_hook_no_contract_is_isolated(tmp_path: Path):
    install_fixture(tmp_path, "no_contract")
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    assert manager.diagnostics()


# ---------------------------------------------------------------------------
# Hot refresh, precedence, object reuse
# ---------------------------------------------------------------------------


def test_refresh_reuses_objects_and_generation(tmp_path: Path):
    install_fixture(tmp_path, "tool_hooks", name="tool_hooks")
    manager = make_manager(tmp_path)
    first = manager.refresh()
    second = manager.refresh()
    assert first is second
    assert manager.hook_set is first
    assert manager.specs[0] is first.specs[0]
    assert manager.fingerprint == first.fingerprint


def test_changed_python_hook_reloads(tmp_path: Path):
    path = write_hook_module(
        tmp_path,
        "evolving",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx): return HookDecision.allow()\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'evolving', 'run': _run}]\n",
    )
    manager = make_manager(tmp_path)
    first = manager.refresh()
    assert first.generation == 1
    old_name = manager._live_modules[next(iter(manager._live_modules))].handle.name
    path.write_text(
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx): return HookDecision.warn('changed')\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'evolving', 'run': _run}]\n",
        encoding="utf-8",
    )
    second = manager.refresh()
    assert second.generation == 2
    assert second.fingerprint != first.fingerprint
    assert old_name not in sys.modules


def test_workspace_hook_shadows_user_hook(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    user_dir = home / ".nexus" / "hooks"
    user_dir.mkdir(parents=True)
    (user_dir / "shared.py").write_text(
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx): return HookDecision.block('user')\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'user_shared', 'run': _run}]\n",
        encoding="utf-8",
    )
    install_fixture(tmp_path, "tool_hooks", name="shared")
    manager = make_manager(tmp_path, home=home)
    manager.refresh()
    names = {spec.name for spec in manager.specs}
    assert "mark_reviewed" in names
    assert "user_shared" not in names


def test_as_manifest_map_is_event_keyed(tmp_path: Path):
    install_fixture(tmp_path, "tool_hooks", name="tool_hooks")
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreCompact",
                name="compactor",
                command=command_script(tmp_path, "ok.py", "raise SystemExit(0)\n"),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    mapping = manager.as_manifest_map()
    assert set(mapping) == {"PreToolUse", "PostToolUse", "SessionStart", "PreCompact"}
    assert all(isinstance(spec.name, str) for specs in mapping.values() for spec in specs)
    # Command hooks sort before Python hooks for deterministic execution.
    order = [spec.kind for specs in mapping.values() for spec in specs]
    assert order[0] == "command"


def test_injected_loader_seam_is_used(tmp_path: Path):
    write_hook_module(tmp_path, "dummy", "VALUE = 1\n")

    class FakeLoader:
        def __init__(self) -> None:
            self.loads = 0
            self.released: list[str] = []

        def load(self, staged, generation):
            self.loads += 1
            handle = ModuleHandle(
                name=f"nexus_ext.fake__g{generation}",
                path=str(staged.path),
                generation=generation,
                sha256=staged.sha256,
                origin="hook",
                module=object(),
            )
            spec = HookSpec(
                event="PreToolUse",
                name="from_fake",
                kind="python",
                fn=lambda invocation, ctx: HookDecision.allow(),
                source=str(staged.candidate.path),
                source_sha256=staged.sha256,
                generation=generation,
            )
            return HookModule(handle=handle, hooks=(spec,), staged=staged)

        def release_module(self, name: str) -> bool:
            self.released.append(name)
            return True

    fake = FakeLoader()
    manager = make_manager(tmp_path, loader=fake)
    manager.refresh()
    assert fake.loads == 1
    assert [spec.name for spec in manager.specs] == ["from_fake"]


def test_default_loader_and_quarantine_types(tmp_path: Path):
    loader = HookModuleLoader()
    assert loader.origin == "hook"
    assert loader.owned_modules == ()
    assert not loader.owns("nexus_ext.nope__g0")


# ---------------------------------------------------------------------------
# Restricted hooks.toml discovery
# ---------------------------------------------------------------------------


def _single_failure(manager: HookManager) -> dict:
    rows = manager.diagnostics()
    assert rows
    return rows[0]


def test_unknown_top_level_key_is_refused(tmp_path: Path):
    path = tmp_path / ".nexus" / "hooks.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[other]\nx = 1\n", encoding="utf-8")
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    assert "unknown top-level keys" in _single_failure(manager)["error"]


def test_unknown_event_is_refused(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "NotAnEvent",
                name="bad",
                command=command_script(tmp_path, "ok.py", "raise SystemExit(0)\n"),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    assert "unknown hook event" in _single_failure(manager)["error"]


def test_unknown_entry_key_is_refused(tmp_path: Path):
    entry = (
        "[[hooks.PreToolUse]]\n"
        "name = 'bad'\n"
        "command = ['/bin/true']\n"
        "mystery = 1\n"
    )
    write_toml(tmp_path, [entry])
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    assert "unknown keys" in _single_failure(manager)["error"]


def test_type_python_in_toml_is_refused(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="nope",
                type_="python",
                command=command_script(tmp_path, "ok.py", "raise SystemExit(0)\n"),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    assert "type must be 'command'" in _single_failure(manager)["error"]


def test_invalid_matcher_is_refused(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="badmatcher",
                matcher="Write(~",
                command=command_script(tmp_path, "ok.py", "raise SystemExit(0)\n"),
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    assert manager.specs == ()
    assert "matcher" in _single_failure(manager)["error"]


def test_broken_toml_does_not_raise_and_keeps_python_hooks(tmp_path: Path):
    install_fixture(tmp_path, "tool_hooks", name="tool_hooks")
    path = tmp_path / ".nexus" / "hooks.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not = = toml", encoding="utf-8")
    manager = make_manager(tmp_path)
    manager.refresh()
    # The broken command file is isolated; the valid Python hook still loads.
    assert any(spec.kind == "python" for spec in manager.specs)
    assert any(row["kind"] == "hook" for row in manager.diagnostics())


# ---------------------------------------------------------------------------
# Modification chain and order
# ---------------------------------------------------------------------------


async def test_modification_chain_is_ordered(tmp_path: Path):
    first = (
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx): return HookDecision.modify({'steps': ['one']})\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'first', 'run': _run}]\n"
    )
    second = (
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    steps = list(inv.tool_input.get('steps', []))\n"
        "    steps.append('two')\n"
        "    return HookDecision.modify({'steps': steps})\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'second', 'run': _run}]\n"
    )
    write_hook_module(tmp_path, "a_first", first)
    write_hook_module(tmp_path, "b_second", second)
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Write", key="/ws/a.py", tool_input={})
    assert dict(outcome.modified_input) == {"steps": ["one", "two"]}
    assert outcome.requires_revalidation
    assert outcome.fired == ("first", "second")


async def test_first_block_wins(tmp_path: Path):
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "PreToolUse",
                name="blocker_one",
                on_nonzero="block",
                command=command_script(tmp_path, "b1.py", "raise SystemExit(1)\n"),
            ),
            toml_command_hook(
                "PreToolUse",
                name="blocker_two",
                on_nonzero="block",
                command=command_script(tmp_path, "b2.py", "raise SystemExit(1)\n"),
            ),
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.pre_tool_use(tool="Read", tool_input={})
    assert outcome.blocked
    assert outcome.fired == ("blocker_one",)


async def test_modify_on_non_modifiable_event_is_downgraded(tmp_path: Path):
    body = (
        "import json\n"
        "print(json.dumps({'decision': 'modify', 'input': {'x': 1}}))\n"
    )
    write_toml(
        tmp_path,
        [
            toml_command_hook(
                "TurnEnd", name="late_modify", command=command_script(tmp_path, "lm.py", body)
            )
        ],
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    outcome = await manager.dispatch("TurnEnd", data={})
    assert not outcome.modified
    assert outcome.warnings


# ---------------------------------------------------------------------------
# Resource lifecycle
# ---------------------------------------------------------------------------


def test_repeated_refresh_does_not_leak_modules_or_stages(tmp_path: Path):
    path = write_hook_module(
        tmp_path,
        "churn",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx): return HookDecision.allow()\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'churn', 'run': _run}]\n",
    )
    manager = make_manager(tmp_path)
    manager.refresh()
    for cycle in range(20):
        path.write_text(
            "from nexus.hooks.model import HookDecision\n"
            "def _run(inv, ctx): return HookDecision.allow()\n"
            f"# cycle {cycle}\n"
            "HOOKS = [{'event': 'PreToolUse', 'name': 'churn', 'run': _run}]\n",
            encoding="utf-8",
        )
        manager.refresh()
    owned = manager._loader.owned_modules
    assert len(owned) == 1
    live = [name for name in sys.modules if name.startswith("nexus_ext.churn_")]
    assert live == list(owned)
    stage = tmp_path / ".nexus" / "stage"
    staged = list(stage.glob("*.py")) if stage.is_dir() else []
    assert len(staged) <= 1


async def test_aclose_releases_modules_and_is_terminal(tmp_path: Path):
    install_fixture(tmp_path, "tool_hooks", name="tool_hooks")
    manager = make_manager(tmp_path)
    manager.refresh()
    module_name = manager._live_modules[next(iter(manager._live_modules))].handle.name
    assert module_name in sys.modules
    await manager.aclose()
    await manager.aclose()
    assert manager.closed
    assert module_name not in sys.modules
    assert manager.specs == ()
    with pytest.raises(ManagerClosed):
        manager.refresh()
