"""Depth and fan-out limits: the runner must clamp or refuse, and tell the model."""
from ..checks import tool_called
from ..dsl import Check, Scenario, calls, say, task, verdict


def _chain(level: int):
    """Each level tries to spawn the next; the runner's depth cap must cut it off."""
    name = f"depth-{level}"
    if level >= 6:
        return {name: [say("Reached the bottom (should not happen if depth is capped).")]}
    return {name: [calls(task(f"depth-{level + 1}", "Go one level deeper.")), say(f"depth-{level} done.")]} | _chain(level + 1)


SCENARIO = Scenario(
    name="agent-limits",
    summary="Nested spawning past the max depth, and a 20-way fan-out past the cap",
    tags=("agents", "limits"),
    est_seconds=30,
    prompt="Push the subagent limits.",
    actors={
        "main": [
            calls(task("depth-1", "Go one level deeper."), text="Nesting deeply."),
            calls(*(task("leaf", f"Leaf {i}.") for i in range(20)), text="Fanning out twenty."),
            verdict("agent-limits", intro="Limits exercised.", checks=[
                tool_called("subagent", at_least=21),
                Check("some spawns were refused or clamped",
                      lambda ctx: any(r.name == "subagent" and r.is_error for r in ctx.all_results)
                      or "clamp" in " ".join(r.text.lower() for r in ctx.all_results)),
            ]),
        ],
        "leaf": [say("leaf ok")],
    } | _chain(1),
)
