"""Parallel child sessions adapted from old Nexus parallel_subagents."""

from ..checks import no_unexpected_errors, tool_called
from ..dsl import Scenario, call, calls, say, verdict

SCENARIO = Scenario(
    "parallel_subagents",
    "Launches two child sessions in parallel",
    {
        "main": [
            calls(
                call("session", agent="scout", prompt="Read the project README and report its purpose.", wait=True),
                call("session", agent="quick", prompt="Read the project README and report its purpose.", wait=True),
                text="Asking two child agents to inspect the README.",
            ),
            verdict(
                "parallel-subagents",
                [tool_called("session", 2), no_unexpected_errors()],
                intro="Both child sessions returned their reports.",
            ),
        ],
        "child": [say("The README describes this project; inspection completed.")],
    },
    prompt="Exercise parallel child sessions.",
)
