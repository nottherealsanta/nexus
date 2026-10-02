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
        "worktree",
    }


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


@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
async def test_worktree_argument_is_coerced_as_a_boolean(tmp_path, value, expected):
    service = FakeService(outcome())
    await task.run({"prompt": "x", "worktree": value}, make_ctx(tmp_path, subagents=service))
    assert service.requests[-1]["worktree"] is expected


async def test_worktree_defaults_off_and_rejects_non_boolean(tmp_path):
    service = FakeService(outcome())
    await task.run({"prompt": "x"}, make_ctx(tmp_path, subagents=service))
    assert service.requests[-1] == {"prompt": "x", "subagent_type": "general"}

    result = await task.run(
        {"prompt": "x", "worktree": "true"}, make_ctx(tmp_path, subagents=service)
    )
    assert result.is_error
    assert len(service.requests) == 1


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
        {"prompt": "x", "subagent_type": ""},
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
