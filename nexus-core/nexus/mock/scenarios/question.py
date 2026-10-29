"""Interactive ask tool demonstration adapted from old Nexus question."""

from ..dsl import Scenario, call, calls

SCENARIO = Scenario(
    "question",
    "Asks a question and waits for an answer",
    {
        "main": [
            calls(
                call(
                    "ask",
                    questions=[
                        {
                            "id": "theme",
                            "question": "Which colour should the theme use?",
                            "options": [{"id": "blue", "label": "Blue"}, {"id": "red", "label": "Red"}, {"id": "green", "label": "Green"}],
                        }
                    ],
                )
            )
        ]
    },
    prompt="Ask me what you need.",
)
