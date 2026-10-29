"""Concurrent tool calls adapted from old Nexus parallel_tools."""

from ..checks import no_unexpected_errors, tool_called
from ..dsl import Scenario, call, calls, verdict

SCENARIO = Scenario(
    "parallel_tools",
    "Runs concurrent reads",
    {
        "main": [
            calls(call("write", path="parallel-seed.txt", content="read me\n")),
            calls(*(call("read", path="parallel-seed.txt") for _ in range(8)), text="Reading in parallel."),
            verdict("parallel-tools", [tool_called("read", 8), no_unexpected_errors()]),
        ]
    },
    prompt="Exercise parallel tool calls.",
)
