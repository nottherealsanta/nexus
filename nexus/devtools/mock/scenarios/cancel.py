"""Interactive: park mid-stream so ``/cancel`` (or Ctrl-C) can be exercised."""
from ..checks import no_unexpected_errors
from ..dsl import Ctx, Scenario, call, calls, dyn, hang, verdict


def _park_or_finish(ctx: Ctx):
    # After a cancel the next turn is a fresh user message; park until then.
    if ctx.step > 1 or ctx.last_user_text.strip().lower().startswith(("please continue", "continue")):
        return verdict("cancel", [no_unexpected_errors(0)], intro="Resumed cleanly after the cancel.")
    return hang("Working on a very long task... run /cancel to stop me. ")


SCENARIO = Scenario(
    name="cancel",
    summary="Streams, then parks until you /cancel; type 'continue' to resume cleanly",
    tags=("interactive", "cancel"),
    est_seconds=60,
    interactive=True,
    prompt="Start something long that I will cancel.",
    actors={"main": [calls(call("read", path=".")), dyn(_park_or_finish), dyn(_park_or_finish), dyn(_park_or_finish)]},
)
