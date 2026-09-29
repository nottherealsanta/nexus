"""Performance: every built-in tool, many subagents of every kind, nesting and failures.

Excluded from the default CI run (``slow``). Main sweeps all tools for real inside
the sandbox, then 24 workers (task/quick/advisor roles) each run their own mix of
tools on private files, some spawn grandchildren, some fail.
"""
from ..checks import child_errors, child_results, tool_called
from ..dsl import Check, Ctx, Scenario, call, calls, dyn, fail, say, task, verdict

_WORKERS = 24
_ROUNDS = 40
_READ_ONLY = ("task", "quick", "advisor")


def _sweep(i: int):
    """One round of parallel read-only calls, plus one mutating tool in rotation."""
    batch = [call("read", path="src/app.py"), call("read", path="src/util.py"), call("read", path="notes/long.txt", offset=10 * i + 1, limit=20),
             call("glob", pattern="**/*.py"), call("glob", pattern="notes/*.txt"), call("grep", pattern="def ", path="src"),
             call("grep", pattern="line 0", path="notes", glob="*.txt"), call("read", path="src")]
    return calls(*batch, text=f"Sweep {i + 1}/{_ROUNDS}: eight reads in parallel.")


def _mutations():
    """Every mutating built-in once, serially (they share the workspace)."""
    return [
        calls(call("write", path="stress/main.txt", content="stress main\n"), text="Writing."),
        calls(call("edit", path="stress/main.txt", old_string="stress main", new_string="stress main edited")),
        calls(call("apply_patch", patch="*** Begin Patch\n*** Add File: stress/patched.txt\n+one\n+two\n*** End Patch")),
        calls(call("apply_patch", patch="*** Begin Patch\n*** Update File: stress/patched.txt\n@@\n one\n-two\n+three\n*** End Patch")),
        calls(call("bash", command="echo stress && python3 -c \"print(sum(range(1000)))\" && git status --short")),
        calls(call("bash", command="sleep 1 && echo bg-done", run_in_background=True)),
        calls(call("todowrite", todos=[
            {"id": "a", "content": "sweep the sandbox", "status": "completed"},
            {"id": "b", "content": "spawn the workers", "status": "in_progress"},
            {"id": "c", "content": "read the verdict", "status": "pending"},
        ])),
        calls(call("skill", name="mock-skill")),
        calls(call("bash", command="python3 -c \"print('x' * 60000)\""), text="One large output."),
    ]


def _worker(index: int):
    """A private script using many tools; every path is unique to this worker."""
    w = f"stress/w{index}"
    steps = [
        calls(call("write", path=f"{w}.txt", content=f"worker {index}\n"), call("read", path="src/config.py"),
              call("glob", pattern="src/*.py")),
        calls(call("edit", path=f"{w}.txt", old_string=f"worker {index}", new_string=f"worker {index} done"),
              call("grep", pattern="TODO", path="src")),
        calls(call("bash", command=f"echo worker-{index}"), call("read", path=f"{w}.txt")),
        calls(call("apply_patch", patch=f"*** Begin Patch\n*** Add File: {w}-patch.txt\n+patched\n*** End Patch")),
        calls(call("todowrite", todos=[{"id": "1", "content": f"worker {index} task", "status": "completed"}])),
    ]
    if index % 6 == 0:  # some workers nest another worker
        steps.insert(2, calls(task(f"g{index}", f"Grandchild of worker {index}.", subagent_type="quick")))
    steps.append(say(f"worker {index}: complete."))
    return steps


def _readonly_worker(index: int):
    return [calls(call("read", path="README.md"), call("grep", pattern="def ", path="src"), call("glob", pattern="**/*.py")),
            calls(call("read", path="src/app.py"), call("read", path="notes")), say(f"advice {index}: looks fine.")]


def _actors():
    actors = {}
    spawn = []
    for i in range(_WORKERS):
        role = _READ_ONLY[i % 3]
        if role == "advisor":
            actors[f"w{i}"] = _readonly_worker(i)
        elif i == 7:
            actors[f"w{i}"] = [fail("scripted 529 overloaded")]
        else:
            actors[f"w{i}"] = _worker(i)
        spawn.append(task(f"w{i}", f"Stress job {i}.", subagent_type=role))
        if i % 6 == 0:
            actors[f"g{i}"] = [calls(call("glob", pattern="**/*"), call("read", path="README.md")), say(f"grandchild {i} done.")]
    return actors, spawn


def _steps(spawn):
    steps = [_sweep(i) for i in range(_ROUNDS)]
    steps += _mutations()
    steps.append(dyn(lambda ctx: _wait_bg(ctx)))
    steps.append(calls(*spawn, text=f"Launching {len(spawn)} workers of three kinds."))
    steps.append(verdict("stress", intro="Stress complete: every tool and every kind of subagent ran.", checks=[
        tool_called("read", at_least=300), tool_called("glob", at_least=80), tool_called("grep", at_least=80),
        tool_called("write", 1), tool_called("edit", 1), tool_called("apply_patch", 2),
        tool_called("bash", at_least=4), tool_called("todowrite", 1), tool_called("skill", 1),
        tool_called("subagent", _WORKERS),
        child_results(_WORKERS),
        child_errors(1),
        Check("no unexpected tool errors in main", lambda ctx: sum(1 for r in ctx.all_results if r.is_error and r.name != "subagent") == 0),
    ]))
    return steps


def _wait_bg(ctx: Ctx):
    import re

    text = " ".join(r.text for r in ctx.all_results if r.name == "bash")
    match = re.findall(r"job[_ ]?id[\"'=: ]+\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    if not match:
        return say("No background job id was reported; continuing.")
    return calls(call("bash", action="wait", job_id=match[-1], wait_s=3), text="Collecting the background job.")


_ACTORS, _SPAWN = _actors()

SCENARIO = Scenario(
    name="stress",
    summary="All built-in tools ×40 sweeps, 24 subagents (task/quick/advisor), nesting and a failure",
    tags=("perf", "agents", "tools"),
    est_seconds=90,
    slow=True,
    prompt="Stress everything.",
    actors={"main": _steps(_SPAWN)} | _ACTORS,
)
