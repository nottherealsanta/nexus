"""Cancellation demonstration adapted from old Nexus cancel scenario."""

from ..dsl import Scenario, hang

SCENARIO = Scenario(
    "cancel",
    "Streams then waits for cancellation",
    {"main": [hang("Streaming until you cancel this turn.")]},
    prompt="Start the cancellable mock turn.",
)
