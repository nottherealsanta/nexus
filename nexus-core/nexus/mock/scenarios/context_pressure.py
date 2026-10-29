"""Large streamed context sample adapted from old Nexus context_pressure."""

from ..dsl import Scenario, verdict

SCENARIO = Scenario(
    "context_pressure",
    "Streams a substantial context sample",
    {"main": [verdict("context-pressure", [], intro=("Context sample: " + ("visible context item. " * 1200)))]},
    prompt="Show a large context sample.",
    slow=True,
)
