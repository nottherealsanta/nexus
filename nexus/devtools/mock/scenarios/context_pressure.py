"""Large tool outputs and climbing scripted usage to exercise budgets and /cost."""
from ..checks import no_unexpected_errors, tool_called
from ..dsl import Scenario, call, calls, verdict


def _steps():
    steps = [calls(call("bash", command="python3 -c \"print('x' * 200000)\""), text="Producing a huge output.")]
    for i in range(1, 9):
        steps.append(calls(call("read", path="notes/long.txt"), text=f"Re-reading a big file ({i}/8).",
                           usage=(20_000 * i, 500)))
    steps.append(verdict("context-pressure", intro="Context is now heavy.", checks=[tool_called("read", 8), no_unexpected_errors(0)]))
    return steps


SCENARIO = Scenario(
    name="context-pressure",
    summary="200 KB tool output, repeated large reads, usage climbing toward the window",
    tags=("context",),
    est_seconds=10,
    prompt="Fill the context window.",
    actors={"main": _steps()},
)
