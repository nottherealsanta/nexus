"""Five ``subagent`` calls at once against the concurrency cap, mixed outcomes."""
from ..checks import child_errors, child_results, tool_called
from ..dsl import Scenario, call, calls, fail, say, task, verdict

_AREAS = ("api", "storage", "ui", "auth", "billing")

SCENARIO = Scenario(
    name="parallel-subagents",
    summary="5 subagents at once (cap 4): queueing, one failure, one grandchild",
    tags=("agents", "parallel"),
    est_seconds=25,
    prompt="Audit the project with parallel workers.",
    actors={
        "main": [
            calls(
                *(task(f"worker-{i}", f"Audit the {area} area and report findings.")
                  for i, area in enumerate(_AREAS, start=1)),
                text="Splitting the audit across five workers.",
                think="Five independent areas; run them in parallel.",
            ),
            verdict("parallel-subagents", intro="All workers reported back. Summary: 4 succeeded, 1 failed, 1 nested worker ran.", checks=[
                tool_called("subagent", 5),
                child_results(5),
                child_errors(1),
            ]),
        ],
        "worker-1": [calls(call("read", path="src/app.py"), call("grep", pattern="TODO")), say("api: 2 TODOs found.")],
        "worker-2": [calls(call("read", path="src")), calls(call("read", path="src/util.py")), say("storage: clean.")],
        "worker-3": [fail("scripted 529 overloaded")],
        "worker-4": [
            calls(task("grand-1", "Deep dive into the auth token flow.")),
            say("auth: grandchild reported, nothing to fix."),
        ],
        "worker-5": [calls(call("glob", pattern="**/*.py")), say("billing: 3 python files.")],
        "grand-1": [calls(call("read", path="src")), say("Leaf report: token flow is fine.")],
    },
)
