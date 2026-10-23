"""Phase 6 P6C: the ``Task`` builtin (subagents as a tool).

Covers the plan sections 5.6/15.6 tool-facing contract:

* one tool/schema for named and ad-hoc spawning, ``bundle="task"``, parallel and
  non-mutating so children fan out;
* the ``"<subagent_type>:<tier>"`` permission key, both static and bound to a
  live service (so ``deny = ["Task(*:high)"]`` catches a role's declared tier);
* execution through the injected :class:`SubagentServiceView` (or the legacy
  ``spawn_agent`` callable), never a ``Runtime`` or an ``nexus.agents`` import;
* the final report returned as a :class:`ToolExecutionResult` with clamp/drop
  notes and metrics the model can see;
* an actionable error when no service is wired, cancellation propagation, and
  isolated conversion of unexpected failures.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus.agents import SubagentOutcome, SubagentUsage
from nexus.config import Config
from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled
from nexus.tools.builtin import task
from nexus.tools.spec import RegisteredTool, ToolContext, ToolExecutionResult

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeService:
    """A minimal ``SubagentServiceView`` double."""

    def __init__(
        self,
        outcome: object,
        *,
        key: str = "general:medium",
        error: BaseException | None = None,
    ) -> None:
        self._outcome = outcome
        self._key = key
        self._error = error
        self.default_type = "general"
        self.requests: list[dict] = []
        self.cancel: object | None = None
        self.emit: object | None = None

    def permission_key(self, request: object) -> str:
        return self._key

    def resolve_tier(self, request: object) -> str:
        return self._key.partition(":")[2] or "medium"

    async def spawn(self, request, /, *, cancel=None, emit=None, call_id=""):
        self.requests.append(dict(request))
        self.cancel = cancel
        self.emit = emit
        self.call_id = call_id
        if self._error is not None:
            raise self._error
        return self._outcome


def make_ctx(tmp_path: Path, **kwargs) -> ToolContext:
    defaults: dict = {
        "workspace": tmp_path,
        "session_id": "sess",
        "turn_id": "turn",
        "config": Config(),
    }
    defaults.update(kwargs)
    return ToolContext(**defaults)


def outcome(**overrides) -> SubagentOutcome:
    payload = {
        "agent": "general",
        "session_id": "root/sub/1",
        "status": "completed",
        "text": "the report body",
        "usage": SubagentUsage(input_tokens=10, output_tokens=5),
    }
    payload.update(overrides)
    return SubagentOutcome(**payload)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


def test_task_spec_shape_and_bundle():
    spec = task.TASK_SPEC
    assert spec.name == "subagent"
    assert spec.bundle == "task"
    assert spec.mutates is False
    assert spec.concurrency == "parallel"
    schema = spec.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["prompt"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "prompt",
        "subagent_type",
        "tools",
        "model",
        "description",
    }
    assert "resume" not in schema["properties"]
    # Subagent worktrees are disabled for now, so the model is not offered them.
    assert "background" not in schema["properties"] and "wait" not in schema["properties"]


def test_resume_key_never_falls_back_to_the_default_role():
    # Static (no service): fails closed to a key no role rule names.
    for data in (
        {"prompt": "x", "resume": "root/sub/1"},
        {"prompt": "x", "resume": "root/sub/1", "subagent_type": "advisor", "model": "low"},
    ):
        assert task.TASK_SPEC.resolve_permission_key(data) == "resume:unresolved"
    from nexus.tools.permissions import parse_rule

    assert not parse_rule("Task(task:*)").matches("subagent", "resume:unresolved", "task")


def test_bound_resume_key_is_the_services_and_gets_no_default_type():
    seen: list[dict] = []

    class Service(FakeService):
        def permission_key(self, request):
            seen.append(dict(request))
            return "advisor:high"

    service = Service(outcome())
    spec = task.make_task_spec(service)
    assert spec.resolve_permission_key({"prompt": "x", "resume": "root/sub/1"}) == "advisor:high"
    assert "subagent_type" not in seen[-1]
    spec.resolve_permission_key({"prompt": "x"})
    assert seen[-1]["subagent_type"] == "general"


async def test_resume_request_is_forwarded_without_a_default_type(tmp_path):
    service = FakeService(outcome())
    await task.run({"prompt": "and the tests?", "resume": "root/sub/1"}, make_ctx(tmp_path, subagents=service))
    assert service.requests[-1] == {"prompt": "and the tests?", "resume": "root/sub/1"}
    # A placeholder resume is ignored: the call starts a new subagent.
    for filler in ("", "none", "new", "invalid"):
        await task.run({"prompt": "x", "resume": filler}, make_ctx(tmp_path, subagents=service))
        assert "resume" not in service.requests[-1]


@pytest.mark.parametrize("filler", [",", "no", "none", "null"])
async def test_filler_resume_beside_fresh_arguments_starts_a_new_subagent(tmp_path, filler):
    """Regression: a model filled ``resume`` with ``","`` on a fresh call (session-5283e69c)."""
    service = FakeService(outcome())
    args = {"prompt": "x", "subagent_type": "scout", "resume": filler, "tools": ["read"], "model": "low"}
    result = await task.run(args, make_ctx(tmp_path, subagents=service))
    assert not result.is_error
    assert "resume" not in service.requests[-1]
    assert service.requests[-1]["subagent_type"] == "scout"
    assert task.TASK_SPEC.resolve_permission_key(args) == "scout:low"
    assert task.make_task_spec(service).resolve_permission_key(args) == "general:medium"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"prompt": "x"}, "task:inherit"),
        ({"prompt": "x", "model": " "}, "task:inherit"),
        ({"prompt": "x", "model": " low "}, "task:low"),
        ({"prompt": "x", "subagent_type": "explore"}, "explore:inherit"),
        ({"prompt": "x", "model": "low"}, "task:low"),
        ({"prompt": "x", "model": "high"}, "task:high"),
        ({"prompt": "x", "model": "anthropic/claude-opus-5"}, "task:auto"),
        (
            {"prompt": "x", "subagent_type": "explore", "model": "medium"},
            "explore:medium",
        ),
    ],
)
def test_static_permission_key(data, expected):
    assert task.TASK_SPEC.resolve_permission_key(data) == expected


def test_worktree_permission_key_has_a_distinct_approval_scope():
    assert task.TASK_SPEC.resolve_permission_key(
        {"prompt": "x", "subagent_type": "explore", "model": "low"}
    ) == "explore:low"
    assert task.TASK_SPEC.resolve_permission_key(
        {
            "prompt": "x",
            "subagent_type": "explore",
            "model": "low",
            "worktree": True,
        }
    ) == "explore:low:worktree"
    from nexus.tools.permissions import parse_rule

    broad_legacy_rule = parse_rule("Task(*)")
    assert broad_legacy_rule.matches("subagent", "explore:low:worktree", "task")


def test_bound_spec_uses_the_service_permission_key():
    service = FakeService(outcome(), key="explore:low")
    service.default_type = "explore"
    spec = task.make_task_spec(service)
    assert spec.resolve_permission_key({"prompt": "x"}) == "explore:low"
    assert spec.resolve_permission_key({"prompt": "x", "worktree": True}) == (
        "explore:low:worktree"
    )


def test_managed_worktree_service_uses_the_normal_task_permission_key():
    service = FakeService(outcome(), key="explore:low")
    service.managed_worktrees = True
    spec = task.make_task_spec(service)
    assert spec.resolve_permission_key({"prompt": "x", "worktree": True}) == (
        "explore:low"
    )


def test_build_task_tool_returns_a_registered_task_tool():
    service = FakeService(outcome(), key="planner:medium")
    tool = task.build_task_tool(service)
    assert isinstance(tool, RegisteredTool)
    assert tool.name == "subagent"
    assert tool.bundle == "task"
    assert tool.spec.resolve_permission_key({"prompt": "x"}) == "planner:medium"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def test_run_without_a_service_is_actionable(tmp_path):
    ctx = make_ctx(tmp_path)
    result = await task.run({"prompt": "x"}, ctx)
    assert isinstance(result, ToolExecutionResult)
    assert result.is_error is True
    assert "SubagentRunner" in result.content[0].text


async def test_run_passes_the_request_and_context_seams(tmp_path):
    service = FakeService(outcome())
    token = CancelToken()
    emitted: list = []

    def sink(event_type, data):
        emitted.append((event_type, data))

    ctx = make_ctx(tmp_path, subagents=service, cancel_token=token, emit=sink, call_id="task-call-7")
    await task.run(
        {
            "prompt": "search the repo",
            "subagent_type": "explore",
            "tools": ["read", "grep"],
            "model": "low",
            "description": "fan out",
        },
        ctx,
    )
    request = service.requests[-1]
    assert request == {
        "prompt": "search the repo",
        "subagent_type": "explore",
        "tools": ["read", "grep"],
        "model": "low",
        "description": "fan out",
    }
    assert service.cancel is token
    assert service.emit is sink
    assert service.call_id == "task-call-7"


async def test_worktree_false_is_passed_and_true_is_refused_while_disabled(tmp_path):
    service = FakeService(outcome())
    await task.run({"prompt": "x", "worktree": False}, make_ctx(tmp_path, subagents=service))
    assert service.requests[-1]["worktree"] is False

    result = await task.run({"prompt": "x", "worktree": True}, make_ctx(tmp_path, subagents=service))
    assert result.is_error
    assert "Subagent worktrees are disabled" in result.content[0].text
    assert len(service.requests) == 1


async def test_worktree_defaults_off_and_rejects_non_boolean(tmp_path):
    service = FakeService(outcome())
    await task.run({"prompt": "x"}, make_ctx(tmp_path, subagents=service))
    assert service.requests[-1] == {"prompt": "x", "subagent_type": "general"}

    result = await task.run(
        {"prompt": "x", "worktree": "true"}, make_ctx(tmp_path, subagents=service)
    )
    assert result.is_error
    assert len(service.requests) == 1


class LegacyBackgroundService(FakeService):
    """A service that still exposes the retired detached-start seam."""

    background = True

    def __init__(self, outcome_: object) -> None:
        super().__init__(outcome_)
        self.started: list[dict] = []

    async def start(self, request, /, *, emit=None, call_id=""):
        self.started.append(dict(request))
        raise AssertionError("Task must never start a detached child")


@pytest.mark.parametrize("args", [{}, {"background": True}, {"wait": False}, {"wait": True}])
async def test_task_always_blocks_for_the_report(tmp_path, args):
    """Old ``background``/``wait`` arguments are ignored: the call is foreground-only."""
    service = LegacyBackgroundService(outcome())
    result = await task.run({"prompt": "x", **args}, make_ctx(tmp_path, subagents=service))
    assert not service.started and len(service.requests) == 1
    assert "background" not in service.requests[0] and "wait" not in service.requests[0]
    assert "the report body" in result.content[0].text
    assert "background" not in result.metrics


def test_task_description_tells_the_model_to_batch_independent_calls():
    text = task.TASK_SPEC.description
    assert "blocks" in text and "together" in text
    assert "background" not in text and "subagent_send" not in text


async def test_bound_service_default_type_applies_to_spawn(tmp_path):
    service = FakeService(outcome(), key="explore:low")
    service.default_type = "explore"
    ctx = make_ctx(tmp_path, subagents=service)

    result = await task.run({"prompt": "search the repo"}, ctx)

    assert result.is_error is False
    assert service.requests[-1]["subagent_type"] == "explore"


async def test_run_converts_the_outcome_with_notes_and_metrics(tmp_path):
    service = FakeService(
        outcome(
            dropped_tools=("Write", "Bash"),
            clamped=True,
            tier="medium",
            requested_tier="high",
        )
    )
    ctx = make_ctx(tmp_path, subagents=service)
    result = await task.run({"prompt": "x"}, ctx)

    assert result.is_error is False
    text = result.content[0].text
    assert "the report body" in text
    assert "dropped" in text and "Write" in text
    assert "clamped" in text and "high" in text
    assert result.display == "Task general: completed"
    assert "root/sub/1" in (result.context_note or "")
    assert result.metrics["dropped_tools"] == ["Write", "Bash"]
    assert result.metrics["clamped"] is True
    assert result.metrics["tier"] == "medium"
    assert result.metrics["usage"]["total_tokens"] == 15


async def test_run_metrics_carry_actual_models(tmp_path):
    service = FakeService(outcome(tier="low", models=("anthropic/a", "openai/b")))
    result = await task.run({"prompt": "x"}, make_ctx(tmp_path, subagents=service))
    assert result.metrics["actual_model"] == "openai/b"
    assert result.metrics["actual_models"] == ["anthropic/a", "openai/b"]

    service = FakeService(outcome(tier="low"))
    result = await task.run({"prompt": "x"}, make_ctx(tmp_path, subagents=service))
    assert result.metrics["actual_model"] is None
    assert result.metrics["actual_models"] == []


async def test_run_reports_a_child_failure_as_an_error(tmp_path):
    service = FakeService(
        outcome(status="failed", is_error=True, text="the child failed")
    )
    ctx = make_ctx(tmp_path, subagents=service)
    result = await task.run({"prompt": "x"}, ctx)
    assert result.is_error is True
    assert "the child failed" in result.content[0].text
    assert result.metrics["status"] == "failed"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"prompt": ""},
        {"prompt": "x", "tools": "Read"},
        {"prompt": "x", "tools": ["Read", ""]},
        {"prompt": "x", "model": 123},
    ],
)
async def test_run_rejects_bad_arguments(tmp_path, args):
    service = FakeService(outcome())
    ctx = make_ctx(tmp_path, subagents=service)
    result = await task.run(args, ctx)
    assert result.is_error is True
    assert service.requests == []


async def test_run_propagates_cancellation(tmp_path):
    service = FakeService(outcome(), error=OperationCancelled("stop"))
    ctx = make_ctx(tmp_path, subagents=service)
    with pytest.raises(OperationCancelled):
        await task.run({"prompt": "x"}, ctx)


async def test_run_isolates_unexpected_failures(tmp_path):
    service = FakeService(outcome(), error=RuntimeError("kaboom"))
    ctx = make_ctx(tmp_path, subagents=service)
    result = await task.run({"prompt": "x"}, ctx)
    assert result.is_error is True
    assert "spawn failed" in result.content[0].text
    assert "RuntimeError" in result.content[0].text


async def test_run_supports_a_legacy_spawn_agent_callable(tmp_path):
    seen: list[dict] = []

    class LegacyOutcome:
        agent = "general"
        session_id = "root/sub/9"
        status = "completed"
        text = "legacy report"
        is_error = False
        dropped_tools = ()
        clamped = False
        tier = "medium"
        requested_tier = "medium"
        usage = SubagentUsage(input_tokens=1)

    async def spawn_agent(request):
        seen.append(dict(request))
        return LegacyOutcome()

    ctx = make_ctx(tmp_path, spawn_agent=spawn_agent)
    result = await task.run({"prompt": "x"}, ctx)
    assert seen == [{"prompt": "x"}]
    assert "legacy report" in result.content[0].text
    assert result.metrics["session"] == "root/sub/9"


async def test_run_handles_a_bare_outcome_object(tmp_path):
    class Bare:
        text = "bare text"

    service = FakeService(Bare())
    ctx = make_ctx(tmp_path, subagents=service)
    result = await task.run({"prompt": "x"}, ctx)
    assert result.is_error is False
    assert "bare text" in result.content[0].text


# ---------------------------------------------------------------------------
# Layering: the builtin must not import upward
# ---------------------------------------------------------------------------

_FORBIDDEN = ("nexus.runtime", "nexus.ui", "nexus.cli", "nexus.agent", "nexus.agents")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = list(
                    path.relative_to(REPO_ROOT).with_suffix("").parts
                )
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if path.name != "__init__.py":
                    parts = parts[:-1]
                base = parts[: len(parts) - (node.level - 1)]
                if node.module:
                    base = base + node.module.split(".")
                modules.add(".".join(base))
            else:
                modules.add(node.module or "")
    return modules


def test_task_builtin_never_imports_upward():
    path = REPO_ROOT / "nexus" / "tools" / "builtin" / "task.py"
    violations = sorted(
        module for module in _imports(path) if module.startswith(_FORBIDDEN)
    )
    assert not violations, violations


# ---------------------------------------------------------------------------
# Role roster (roles load like skills)
# ---------------------------------------------------------------------------


def test_static_spec_lists_no_hardcoded_roles() -> None:
    assert "Available agents" not in task.TASK_SPEC.description
    assert "advisor" not in task.TASK_SPEC.description


def test_bound_spec_lists_the_service_role_index() -> None:
    service = FakeService(object())
    service.role_index = lambda: "advisor: read-only\nreviewer: checks diffs"
    description = task.make_task_spec(service).description
    assert description.startswith(task.TASK_SPEC.description)
    assert "default 'general'" in description
    assert description.endswith("advisor: read-only\nreviewer: checks diffs")


def test_broken_or_empty_role_index_keeps_the_base_description() -> None:
    service = FakeService(object())
    service.role_index = lambda: ""
    assert task.make_task_spec(service).description == task.TASK_SPEC.description

    def boom() -> str:
        raise RuntimeError("discovery failed")

    service.role_index = boom
    assert task.make_task_spec(service).description == task.TASK_SPEC.description


# -- placeholder optionals are absent ------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tools", []),
        ("model", ""),
        ("model", "   "),
        ("subagent_type", ""),
        ("description", ""),
        ("description", ". "),
        ("description", " ,-- "),
    ],
)
async def test_empty_or_placeholder_optionals_are_treated_as_absent(tmp_path, field, value):
    service = FakeService(outcome())
    result = await task.run({"prompt": "x", field: value}, make_ctx(tmp_path, subagents=service))
    assert not result.is_error
    if field == "subagent_type":  # absent means the service default, not an empty role
        assert service.requests[-1]["subagent_type"] == service.default_type
    else:
        assert field not in service.requests[-1]


async def test_empty_tools_never_means_no_tools(tmp_path):
    service = FakeService(outcome())
    await task.run({"prompt": "x", "tools": []}, make_ctx(tmp_path, subagents=service))
    assert "tools" not in service.requests[-1]
    # A real narrowing and a real description still pass through.
    await task.run({"prompt": "x", "tools": ["read"], "description": "Scan"}, make_ctx(tmp_path, subagents=service))
    assert service.requests[-1]["tools"] == ["read"] and service.requests[-1]["description"] == "Scan"


async def test_the_observed_filled_in_call_starts_a_new_subagent(tmp_path):
    """Regression: codex filled every optional field, including a guessed resume."""
    service = FakeService(outcome())
    args = {"subagent_type": "task", "resume": "none", "tools": [], "model": "", "description": ". ", "prompt": "do it"}
    result = await task.run(args, make_ctx(tmp_path, subagents=service))
    assert not result.is_error
    assert service.requests[-1] == {"prompt": "do it", "subagent_type": "task"}
    assert task.TASK_SPEC.resolve_permission_key(args) == "task:inherit"


def test_the_new_subagent_schema_rejects_a_live_resume():
    from nexus.tools.manager import ToolInputError, validate_tool_input

    with pytest.raises(ToolInputError):
        validate_tool_input(task.TASK_SPEC, {"prompt": "x", "resume": "root/sub/1"})
    validate_tool_input(
        task.TASK_SPEC,
        {"prompt": "x", "tools": [], "model": "", "description": ". ", "subagent_type": ""},
    )


# -- subagent_continue ---------------------------------------------------------------------


def test_continue_spec_shape_and_bundle():
    spec = task.CONTINUE_SPEC
    assert spec.name == "subagent_continue" == task.CONTINUE_TOOL_NAME
    assert spec.bundle == task.TASK_SPEC.bundle == "task"
    schema = spec.input_schema
    assert set(schema["properties"]) == {"agent_id", "prompt"}
    assert sorted(schema["required"]) == ["agent_id", "prompt"]
    assert schema["additionalProperties"] is False
    assert "subagent_continue" in task.TASK_SPEC.description


async def test_continue_forwards_agent_id_as_resume_and_nothing_else(tmp_path):
    service = FakeService(outcome())
    result = await task.run_continue(
        {"agent_id": " root/sub/1 ", "prompt": "and the tests?"}, make_ctx(tmp_path, subagents=service)
    )
    assert not result.is_error
    assert service.requests[-1] == {"resume": "root/sub/1", "prompt": "and the tests?"}


@pytest.mark.parametrize("args", [{}, {"agent_id": "root/sub/1"}, {"prompt": "x"}, {"agent_id": " ", "prompt": "x"}])
async def test_continue_rejects_bad_arguments(tmp_path, args):
    service = FakeService(outcome())
    result = await task.run_continue(args, make_ctx(tmp_path, subagents=service))
    assert result.is_error and service.requests == []


def test_continue_permission_key_is_the_services_or_fails_closed():
    assert task.CONTINUE_SPEC.resolve_permission_key({"agent_id": "root/sub/1", "prompt": "x"}) == "resume:unresolved"
    seen: list[dict] = []

    class Service(FakeService):
        def permission_key(self, request):
            seen.append(dict(request))
            return "advisor:high"

    spec = task.make_continue_spec(Service(outcome()))
    assert spec.resolve_permission_key({"agent_id": "root/sub/1", "prompt": "x"}) == "advisor:high"
    assert seen[-1] == {"resume": "root/sub/1", "prompt": "x"}


def test_build_task_tools_registers_both_tools_with_the_same_bundle():
    new, cont = task.build_task_tools(None)
    assert (new.name, cont.name) == ("subagent", "subagent_continue")
    assert new.spec.bundle == cont.spec.bundle
