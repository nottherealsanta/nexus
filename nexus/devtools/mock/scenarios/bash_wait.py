"""A slow, chatty command is yielded to the background and waited on once (BASH_WAIT_PLAN)."""
import re

from ..checks import no_unexpected_errors, tool_called, tool_result_contains
from ..dsl import Ctx, Scenario, call, calls, dyn, say, verdict

_JOB = re.compile(r"job_id[\"'=: ]+\s*(job_[A-Za-z0-9_-]+)", re.IGNORECASE)
# One python3 program (newlines, not shell separators) so it stays inside the
# mock command allowlist.
_COMMAND = (
    'python3 -c "import time\n'
    "for i in range(1, 9):\n"
    "    print('build step', i, flush=True)\n"
    "    time.sleep(1)\n"
    "print('BUILD-OK')\n"
    '"'
)


def _wait_once(ctx: Ctx):
    match = next((m for r in ctx.last_results if (m := _JOB.search(r.text))), None)
    if match is None:
        return say("The command finished inside the yield window; nothing to wait for.")
    return calls(
        call("bash", action="wait", job_id=match.group(1)),
        text="Still running: waiting once for it to exit instead of polling.",
    )


SCENARIO = Scenario(
    name="bash-wait",
    summary="A ~8s command outlives the 3s yield window; one wait returns at exit (2 tool calls, no polling)",
    tags=("tools", "bash"),
    est_seconds=12,
    prompt="Run the slow build and tell me when it is done.",
    actors={"main": [
        calls(call("bash", command=_COMMAND), text="Running the build in the foreground."),
        dyn(_wait_once),
        verdict("bash-wait", intro="The build finished after a single wait.", checks=[
            tool_called("bash", 2),
            tool_result_contains("bash", "moved to background"),
            tool_result_contains("bash", "BUILD-OK"),
            no_unexpected_errors(0),
        ]),
    ]},
)
