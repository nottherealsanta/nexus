"""Interactive: the question tool, branching on the answers."""
from ..checks import tool_called
from ..dsl import Ctx, Scenario, call, calls, dyn, verdict


def _reaction(ctx: Ctx) -> str:
    answer = " ".join(r.text for r in ctx.last_results).lower()
    if "blue" in answer:
        return "Blue it is: a calm choice."
    if "red" in answer:
        return "Red: bold."
    return f"Noted: {answer.strip()[:120] or '(empty answer)'}"


def _ask_more(ctx: Ctx):
    return calls(call("question", question="Anything else to add?"), text=_reaction(ctx))


def _finish(ctx: Ctx):
    return verdict("question", [tool_called("question", 2)], intro=_reaction(ctx))


SCENARIO = Scenario(
    name="question",
    summary="Asks a multiple-choice question and a free-text one, branches on the answers",
    tags=("interactive", "question"),
    est_seconds=30,
    interactive=True,
    prompt="Ask me what you need.",
    actors={
        "main": [
            calls(call("question", question="Which colour should the theme use?", options=["Blue", "Red", "Green"])),
            dyn(_ask_more),
            dyn(_finish),
        ]
    },
)
