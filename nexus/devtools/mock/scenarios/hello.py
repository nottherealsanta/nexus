"""Smallest run: one streamed markdown reply. Smoke test for dev mode."""
from ..checks import no_unexpected_errors
from ..dsl import Scenario, verdict

SCENARIO = Scenario(
    name="hello",
    summary="One streamed markdown reply (smoke test)",
    tags=("smoke",),
    est_seconds=2,
    prompt="Say hello.",
    actors={
        "main": [
            verdict(
                "hello",
                [no_unexpected_errors(0)],
                intro=(
                    "# Hello from the mock provider\n\n"
                    "This reply was **scripted**. No model was called.\n\n"
                    "- streamed in small chunks\n- rendered as markdown\n- ended with `end_turn`"
                ),
            ),
        ],
    },
)
