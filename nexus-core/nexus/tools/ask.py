"""The `ask` task-tool spec; the daemon binds waiting to its question registry.

Questions are not approvals. The tool waits for a user's answer (no timeout of
its own) and returns a JSON object keyed by question id; interruption cancels
the pending question and the turn. The schema and bounds live in
`nexus/questions.py`.
"""
from typing import Any

from ..llmhub.types import ToolSpec
from ..questions import MAX_OPTION_LABEL_CHARS, MAX_OPTION_PREVIEW_CHARS, MAX_OPTIONS, MAX_QUESTION_CHARS, MAX_QUESTIONS
from .base import Tool, ToolContext, ToolError

SPEC = ToolSpec(
    name="ask",
    description=(
        "Ask the user one or more questions and wait for the answers. Each question has a stable id and question "
        "text, optional options (stable ids, labels and optional previews), and is single-select or multi-select. "
        "A question without options asks for free text; with options, free text is off unless allow_free_text is "
        "true. Every question must be answered. Returns a JSON object keyed by question id with selected option ids, "
        "their labels and any free text. Use it for decisions that are genuinely the user's; this is not an approval "
        "or permission request."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": f"The questions, 1-{MAX_QUESTIONS}, each with a stable id",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Stable question id (letters, digits, '.', '_', ':', '-'; at most 64)",
                        },
                        "question": {
                            "type": "string",
                            "maxLength": MAX_QUESTION_CHARS,
                            "description": "The complete question for the user",
                        },
                        "options": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": MAX_OPTIONS,
                            "description": "Choices; omit for a free-text question",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "string", "description": "Stable option id"},
                                    "label": {
                                        "type": "string",
                                        "maxLength": MAX_OPTION_LABEL_CHARS,
                                        "description": "Short label shown to the user",
                                    },
                                    "preview": {
                                        "type": "string",
                                        "maxLength": MAX_OPTION_PREVIEW_CHARS,
                                        "description": "Optional longer preview for an option",
                                    },
                                },
                                "required": ["id", "label"],
                            },
                        },
                        "multiple": {
                            "type": "boolean",
                            "description": "Allow more than one selected option (default false)",
                        },
                        "allow_free_text": {
                            "type": "boolean",
                            "description": (
                                "Allow a free-text answer in addition to selections (default true only when "
                                "there are no options)"
                            ),
                        },
                    },
                    "required": ["id", "question"],
                },
            },
        },
        "required": ["questions"],
    },
)


async def unbound(args: dict[str, Any], ctx: ToolContext) -> str:
    raise ToolError("ask requires the daemon's question registry")


TOOL = Tool(
    SPEC,
    unbound,
    "Ask the user one or more questions and wait for the answers; give each a stable id, with options when the "
    "choices are known. This is not an approval request. The answer is a JSON object keyed by question id.",
)
