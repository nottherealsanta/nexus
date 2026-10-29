"""Long real-tool sequence adapted from old Nexus tool_marathon."""

from ..checks import no_unexpected_errors, tool_called
from ..dsl import Scenario, call, calls, verdict

_steps = [
    calls(call("write", path="mock-output.md", content="hello mock\n"), text="Creating a file."),
    calls(call("read", path="mock-output.md")),
    calls(call("edit", path="mock-output.md", old_string="hello", new_string="hello from")),
    calls(call("read", path="mock-output.md")),
    calls(call("bash", command="printf 'mock-shell-ok\\n'")),
]
for i in range(1, 31):
    _steps.append(calls(call("read", path="mock-output.md"), text=f"Marathon step {i}."))
_steps.append(
    verdict(
        "tool-marathon",
        intro="Marathon complete: every step above ran for real in the selected workspace.",
        checks=[
            tool_called("read", at_least=30),
            tool_called("write", 1),
            tool_called("edit", 1),
            tool_called("bash", 1),
            no_unexpected_errors(),
        ],
    )
)
SCENARIO = Scenario("tool_marathon", "Thirty six sequential real tool calls", {"main": _steps}, prompt="Run the tool marathon.")
