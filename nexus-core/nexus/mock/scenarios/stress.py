"""Long streaming response adapted from old Nexus stress scenario."""

from ..dsl import Scenario, verdict

SCENARIO = Scenario(
    "stress",
    "Streams a long generated response",
    {"main": [verdict("stress", [], intro="\n".join(f"Line {n}: streamed offline content." for n in range(500)))]},
    prompt="Stream a long response.",
    slow=True,
)
