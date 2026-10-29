"""Provider failure and recovery adapted from old Nexus provider_failure."""

from ..dsl import Scenario, fail, verdict

SCENARIO = Scenario(
    "provider_failure",
    "First request fails; retry succeeds",
    {
        "main": [
            fail(
                "mock: scripted temporary provider failure",
                recover_at_user_turn=2,
                then=verdict("provider-failure", [], intro="Recovered after a retry."),
            )
        ]
    },
    prompt="Try something that will fail once.",
)
