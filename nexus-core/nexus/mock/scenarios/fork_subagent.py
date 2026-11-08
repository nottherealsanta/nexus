"""A fork sub agent: the parent builds some context, forks twice from it, and gets both reports back."""

from ..checks import no_unexpected_errors, tool_called
from ..dsl import Scenario, call, calls, say, verdict

SCENARIO = Scenario(
    "fork_subagent",
    "Builds context, then launches two forks that share it",
    {
        "main": [
            calls(call("ls", path="."), text="Looking at the workspace before splitting the work."),
            calls(
                call("fork", prompt="List the files you saw and say which one to read first.",
                     title="Pick a file", wait=True),
                call("fork", prompt="Say in one sentence what this workspace is for.",
                     title="Workspace purpose", wait=True),
                text="Forking twice from here; both forks already know what I have seen.",
            ),
            verdict("fork-subagent", [tool_called("fork", 2), no_unexpected_errors()],
                    intro="Both forks reported back."),
        ],
        "fork": [
            calls(call("glob", pattern="*"), text="Checking the workspace once more from the fork."),
            say("Fork report: I reused the parent's listing and checked it again; nothing else was needed."),
        ],
    },
    prompt="Exercise fork sub agents.",
)
