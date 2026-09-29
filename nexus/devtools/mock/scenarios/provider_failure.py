"""Interactive: the provider fails once; the next message recovers."""
from ..dsl import Scenario, fail, verdict

SCENARIO = Scenario(
    name="provider-failure",
    summary="First turn fails with a scripted 503; send any message to recover",
    tags=("errors", "interactive"),
    est_seconds=5,
    interactive=True,
    prompt="Try something that will fail once.",
    actors={
        "main": [
            fail(
                "scripted 503 upstream unavailable",
                recover_at_user_turn=2,
                then=verdict("provider-failure", [], intro="Recovered after a transient provider error."),
            ),
        ]
    },
)
