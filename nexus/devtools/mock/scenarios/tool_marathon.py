"""A long chain of tool calls using most built-in tools, all inside the sandbox."""
import re

from ..checks import no_unexpected_errors, tool_called
from ..dsl import Ctx, Scenario, call, calls, dyn, say, verdict

_JOB = re.compile(r"job[_ ]?id[\"'=: ]+\s*([A-Za-z0-9_-]+)", re.IGNORECASE)


def _wait_job(ctx: Ctx):
    match = next((m for r in ctx.last_results if (m := _JOB.search(r.text))), None)
    if match is None:
        return say("The background job did not report an id; skipping its status check.")
    return calls(call("bash", action="wait", job_id=match.group(1)), text="Checking the background job.")


def _steps():
    steps = [
        calls(call("read", path="."), call("glob", pattern="**/*.py"), text="Surveying the project."),
        calls(call("read", path="README.md")),
        calls(call("read", path="src/app.py")),
        calls(call("grep", pattern="TODO", path="src")),
        calls(call("todowrite", todos=[
            {"id": "1", "content": "Read the sources", "status": "completed"},
            {"id": "2", "content": "Fix the TODOs", "status": "in_progress"},
            {"id": "3", "content": "Add a test", "status": "pending"},
        ])),
        calls(call("edit", path="src/app.py", old_string="    # TODO: support greeting several names\n", new_string="")),
        calls(call("edit", path="src/config.py", old_string="DEBUG = False", new_string="DEBUG = True")),
        calls(call("edit", path="src/config.py", old_string="RETRIES = 3", new_string="RETRIES = 5")),
        calls(call("apply_patch", patch="*** Begin Patch\n*** Add File: notes/patched.txt\n+added by apply_patch\n*** End Patch")),
        calls(call("write", path="notes/mock-output.md", content="# Mock output\n\nWritten by the marathon.\n")),
        calls(call("bash", command="echo marathon && python3 -c \"print(6*7)\"")),
        calls(call("bash", command="sleep 1 && echo background-done", run_in_background=True)),
        dyn(_wait_job),
        calls(call("bash", command="git status --short")),
        calls(call("read", path="notes/long.txt", offset=100, limit=40)),
    ]
    # Pad to a genuinely long list of sequential tool calls.
    for i in range(1, 31):
        steps.append(calls(call("read", path="src/util.py") if i % 3 == 0 else
                           call("grep", pattern="def ", path="src") if i % 3 == 1 else
                           call("glob", pattern="src/*.py"), text=f"Step {i} of the sweep."))
    steps.append(verdict("tool-marathon", intro="Marathon complete: every step above ran for real inside the sandbox.", checks=[
        tool_called("read", at_least=5),
        tool_called("grep", at_least=5),
        tool_called("edit", 3),
        tool_called("apply_patch", 1),
        tool_called("write", 1),
        tool_called("bash", at_least=3),
        tool_called("todowrite", 1),
        no_unexpected_errors(0),
    ]))
    return steps


SCENARIO = Scenario(
    name="tool-marathon",
    summary="~46 sequential tool calls across read/write/edit/apply_patch/bash/grep/glob/todo",
    tags=("tools", "long"),
    est_seconds=20,
    prompt="Run the tool marathon.",
    actors={"main": _steps()},
)
