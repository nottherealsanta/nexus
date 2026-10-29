"""Background shell job and follow-up adapted from old Nexus bash_wait."""

import re

from ..dsl import Scenario, call, calls, dyn, say, verdict

_JOB = re.compile(r"job[_ ]?id[\"'=: ]+\s*(job_[A-Za-z0-9_-]+)", re.IGNORECASE)


def _wait_once(ctx):
    match = next((found for result in ctx.last_results if (found := _JOB.search(result.text))), None)
    if match is None:
        return say("The command finished inside the yield window; no wait was needed.")
    return calls(call("bash_output", job_id=match.group(1)), text="Collecting the background command.")


SCENARIO = Scenario(
    "bash_wait",
    "Runs a background command and collects it",
    {
        "main": [
            calls(call("bash", command="sleep 0.2; printf BUILD-OK", background=True), text="Starting a short background build."),
            dyn(_wait_once),
            verdict("bash-wait", [], intro="The background job scenario is complete."),
        ]
    },
    prompt="Run the slow build and tell me when it is done.",
)
