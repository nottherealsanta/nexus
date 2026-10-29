"""Adapted from old Nexus nexus/devtools/mock/scenarios/hello.py."""

from ..checks import no_unexpected_errors
from ..dsl import Scenario, verdict

SCENARIO = Scenario(
    "hello",
    "Streamed markdown smoke test",
    {
        "main": [
            verdict(
                "hello",
                [no_unexpected_errors()],
                intro="# Hello from the mock provider\n\nThis reply was **scripted**. No model was called.\n\n- streamed in small chunks\n- rendered as markdown",
            )
        ]
    },
    prompt="Say hello.",
)
