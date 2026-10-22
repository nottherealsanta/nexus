"""A persistent job is started, waited on for readiness with ``until=match``, then stopped (SHELL_PLAN P6)."""
import re

from ..checks import no_unexpected_errors, tool_called, tool_result_contains
from ..dsl import Ctx, Scenario, call, calls, dyn, say, verdict

_JOB = re.compile(r"job_id[\"'=: ]+\s*(job_[A-Za-z0-9_-]+)", re.IGNORECASE)
# One python3 program (newlines, not shell separators) so it stays inside the
# mock command allowlist. The banner is coloured, as a dev server's would be.
_COMMAND = (
    'python3 -c "import time\n'
    "print('starting', flush=True)\n"
    "time.sleep(1)\n"
    "print('\\033[32mREADY on port 8080\\033[0m', flush=True)\n"
    "time.sleep(600)\n"
    '"'
)


def _job(ctx: Ctx) -> str | None:
    match = next((m for r in ctx.last_results if (m := _JOB.search(r.text))), None)
    return match.group(1) if match else None


def _wait_ready(ctx: Ctx):
    job = _job(ctx)
    if job is None:
        return say("The server did not start; nothing to wait for.")
    return calls(
        call("shell", action="wait", job_id=job, until="match", pattern=r"READY on port \d+", wait_s=20),
        text="Waiting once for the readiness banner instead of polling.",
    )


def _stop(ctx: Ctx):
    job = _job(ctx)
    if job is None:
        return say("No job to stop.")
    return calls(call("shell", action="stop", job_id=job), text="Ready; stopping the server again.")


SCENARIO = Scenario(
    name="shell-persistent",
    summary="A persistent job (background, no timeout) is waited on until its banner matches, then stopped (3 tool calls)",
    tags=("tools", "shell"),
    est_seconds=8,
    prompt="Start the dev server, tell me when it is ready, then stop it.",
    actors={"main": [
        calls(call("shell", command=_COMMAND, background=True), text="Starting the server as a persistent job."),
        dyn(_wait_ready),
        dyn(_stop),
        verdict("shell-persistent", intro="The server was ready after one wait and was stopped.", checks=[
            tool_called("shell", 3),
            tool_result_contains("shell", "kind: persistent"),
            tool_result_contains("shell", "match: READY on port 8080"),
            no_unexpected_errors(0),
        ]),
    ]},
)
