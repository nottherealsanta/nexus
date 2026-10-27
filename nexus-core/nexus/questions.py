"""The `ask` tool's question/reply contract (PLAN.md step 10).

The model asks one or more questions; the user answers them through a client.
This module is the single definition of the wire shape, so tool arguments and
HTTP replies are validated the same way:

```jsonc
// ask arguments
{"questions": [
  {"id": "db", "question": "Which database?",            // required, stable within the request
   "options": [                                          // optional; 1-10
     {"id": "pg", "label": "PostgreSQL", "preview": "…"}, // preview is optional
     {"id": "lite", "label": "SQLite"}
   ],
   "multiple": false,                                    // optional; multi-select when true
   "allow_free_text": true}                              // optional; default: true only when no options
]}

// reply submitted to Core
{"answers": [
  {"question_id": "db", "selected": ["pg"], "text": "Version 16."}
]}
```

Every question must be answered. `selected` is empty for a free-text question,
and `text` is null/absent when the question does not allow (or did not get) free
text. IDs are stable ASCII: 1-64 characters from `[A-Za-z0-9_.:-]`, starting
with a letter or digit. Unknown IDs, duplicate answers, invalid selection counts
and oversized values are rejected before a pending request is created (tool
arguments) or before a wait is resolved (replies).

The tool result is one JSON object keyed by question id, so the mapping back to
the model is unambiguous:

```json
{"db": {"selected": ["pg"], "labels": ["PostgreSQL"], "text": "Version 16."}}
```
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

MAX_QUESTIONS = 5
MAX_OPTIONS = 10
MAX_QUESTION_CHARS = 2_000
MAX_OPTION_LABEL_CHARS = 100
MAX_OPTION_PREVIEW_CHARS = 1_000
MAX_ANSWER_CHARS = 2_000
MAX_ID_CHARS = 64
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")

_QUESTION_KEYS = {"id", "question", "options", "multiple", "allow_free_text"}
_OPTION_KEYS = {"id", "label", "preview"}
_ANSWER_KEYS = {"question_id", "selected", "text"}


class QuestionError(ValueError):
    """A malformed question request or answer. Reported as a 400 or a tool error."""


@dataclass
class PendingQuestion:
    """One published `question.asked` request waiting for a reply."""

    id: str
    session_id: str
    call_id: str | None
    questions: list[dict[str, Any]]
    created: float
    future: asyncio.Future[list[dict[str, Any]]]
    closed: bool = False

    def json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "questions": self.questions,
            "created": self.created,
        }


def _string(value: Any, field_name: str, *, limit: int | None = None, blank: bool = True) -> str:
    if not isinstance(value, str):
        raise QuestionError(f"{field_name} must be a string")
    if "\x00" in value:
        raise QuestionError(f"{field_name} must not contain NUL characters")
    if not blank and not value.strip():
        raise QuestionError(f"{field_name} must be a non-empty string")
    if limit is not None and len(value) > limit:
        raise QuestionError(f"{field_name} is limited to {limit} characters")
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise QuestionError(
            f"{field_name} must be 1-{MAX_ID_CHARS} characters of letters, digits, '.', '_', ':' or '-', "
            "starting with a letter or digit"
        )
    return value


def _option(raw: Any, question_id: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise QuestionError(f"questions[{question_id}].options[{index}] must be an object")
    unknown = set(raw) - _OPTION_KEYS
    if unknown:
        raise QuestionError(
            f"questions[{question_id}].options[{index}] has unknown keys: {', '.join(sorted(unknown))}"
        )
    if "id" not in raw:
        raise QuestionError(f"questions[{question_id}].options[{index}].id is required")
    if "label" not in raw:
        raise QuestionError(f"questions[{question_id}].options[{index}].label is required")
    preview = raw.get("preview")
    if preview is not None:
        preview = _string(preview, f"questions[{question_id}].options[{index}].preview",
                          limit=MAX_OPTION_PREVIEW_CHARS)
    return {
        "id": _identifier(raw["id"], f"questions[{question_id}].options[{index}].id"),
        "label": _string(raw["label"], f"questions[{question_id}].options[{index}].label",
                         limit=MAX_OPTION_LABEL_CHARS, blank=False),
        "preview": preview or None,
    }


def normalize_request(args: Any) -> list[dict[str, Any]]:
    """Validate `ask` arguments and return the normalized questions.

    Nothing is published unless this function returns.
    """
    if not isinstance(args, dict):
        raise QuestionError("ask arguments must be an object")
    unknown = set(args) - {"questions"}
    if unknown:
        raise QuestionError(f"ask arguments have unknown keys: {', '.join(sorted(unknown))}")
    raw_questions = args.get("questions")
    if not isinstance(raw_questions, list):
        raise QuestionError("questions must be a list")
    if not 1 <= len(raw_questions) <= MAX_QUESTIONS:
        raise QuestionError(f"questions must contain 1-{MAX_QUESTIONS} items")
    questions: list[dict[str, Any]] = []
    seen_question_ids: set[str] = set()
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise QuestionError(f"questions[{index}] must be an object")
        missing = _QUESTION_KEYS - {"options", "multiple", "allow_free_text"} - set(raw)
        if missing:
            raise QuestionError(f"questions[{index}] is missing: {', '.join(sorted(missing))}")
        unknown = set(raw) - _QUESTION_KEYS
        if unknown:
            raise QuestionError(f"questions[{index}] has unknown keys: {', '.join(sorted(unknown))}")
        question_id = _identifier(raw["id"], f"questions[{index}].id")
        if question_id in seen_question_ids:
            raise QuestionError(f"duplicate question id {question_id!r}")
        seen_question_ids.add(question_id)
        text = _string(raw["question"], f"questions[{question_id}].question",
                       limit=MAX_QUESTION_CHARS, blank=False)
        raw_options = raw.get("options")
        if raw_options is None:
            raw_options = []
        if not isinstance(raw_options, list):
            raise QuestionError(f"questions[{question_id}].options must be a list")
        if len(raw_options) > MAX_OPTIONS:
            raise QuestionError(f"questions[{question_id}].options is limited to {MAX_OPTIONS} items")
        options = [_option(option, question_id, i) for i, option in enumerate(raw_options)]
        option_ids = [option["id"] for option in options]
        if len(option_ids) != len(set(option_ids)):
            raise QuestionError(f"questions[{question_id}] has duplicate option ids")
        multiple = raw.get("multiple", False)
        if not isinstance(multiple, bool):
            raise QuestionError(f"questions[{question_id}].multiple must be a boolean")
        if multiple and not options:
            raise QuestionError(f"questions[{question_id}].multiple requires options")
        allow_free_text = raw.get("allow_free_text", not options)
        if not isinstance(allow_free_text, bool):
            raise QuestionError(f"questions[{question_id}].allow_free_text must be a boolean")
        if not options and not allow_free_text:
            raise QuestionError(
                f"questions[{question_id}] has no options, so it must allow_free_text"
            )
        questions.append({
            "id": question_id,
            "question": text,
            "options": options,
            "multiple": multiple,
            "allow_free_text": allow_free_text,
        })
    return questions


def _answers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise QuestionError("answers must be a list")
    return raw


def validate_answers(questions: list[dict[str, Any]], raw_answers: Any) -> list[dict[str, Any]]:
    """Validate a complete reply; return it in question order.

    Every question needs exactly one answer. Unknown question or option IDs,
    duplicates, invalid selection counts and text where none is allowed fail
    before the waiting request is resolved.
    """
    by_id = {question["id"]: question for question in questions}
    answers: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(_answers(raw_answers)):
        if not isinstance(raw, dict):
            raise QuestionError(f"answers[{index}] must be an object")
        unknown = set(raw) - _ANSWER_KEYS
        if unknown:
            raise QuestionError(f"answers[{index}] has unknown keys: {', '.join(sorted(unknown))}")
        if "question_id" not in raw:
            raise QuestionError(f"answers[{index}].question_id is required")
        question_id = raw["question_id"]
        if not isinstance(question_id, str):
            raise QuestionError(f"answers[{index}].question_id must be a string")
        if question_id not in by_id:
            raise QuestionError(f"unknown question id {question_id!r}")
        if question_id in answers:
            raise QuestionError(f"duplicate answer for question {question_id!r}")
        question = by_id[question_id]
        selected = raw.get("selected", [])
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise QuestionError(f"answers[{question_id}].selected must be a list of option ids")
        if len(selected) != len(set(selected)):
            raise QuestionError(f"answers[{question_id}].selected contains duplicate option ids")
        known = {option["id"] for option in question["options"]}
        unknown_options = [item for item in selected if item not in known]
        if unknown_options:
            raise QuestionError(
                f"answers[{question_id}].selected has unknown option ids: {', '.join(unknown_options)}"
            )
        text = raw.get("text")
        if text is not None:
            text = _string(text, f"answers[{question_id}].text", limit=MAX_ANSWER_CHARS)
            if not text.strip():
                text = None
        if not question["allow_free_text"] and text is not None:
            raise QuestionError(f"question {question_id!r} does not allow a free-text answer")
        if not question["options"]:
            if selected:
                raise QuestionError(f"question {question_id!r} has no options to select")
            if text is None:
                raise QuestionError(f"question {question_id!r} requires a free-text answer")
        elif question["multiple"]:
            if not selected and text is None:
                raise QuestionError(f"question {question_id!r} requires at least one selected option")
        else:
            if len(selected) > 1:
                raise QuestionError(f"question {question_id!r} is single-select; choose at most one option")
            if not selected and text is None:
                raise QuestionError(f"question {question_id!r} requires one selected option")
        answers[question_id] = {"question_id": question_id, "selected": list(selected), "text": text}
    missing = [question["id"] for question in questions if question["id"] not in answers]
    if missing:
        raise QuestionError("missing answers for: " + ", ".join(missing))
    return [answers[question["id"]] for question in questions]


def result_text(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> str:
    """The model-visible result: one JSON object keyed by question id."""
    labels = {
        question["id"]: {option["id"]: option["label"] for option in question["options"]}
        for question in questions
    }
    body = {
        answer["question_id"]: {
            "selected": answer["selected"],
            "labels": [labels[answer["question_id"]][item] for item in answer["selected"]],
            "text": answer["text"],
        }
        for answer in answers
    }
    return "The user answered the questions:\n" + json.dumps(body, indent=2)


def asked_event(question: PendingQuestion) -> dict[str, Any]:
    return question.json()


def replied_event(question: PendingQuestion, *, outcome: str, answers: list[dict[str, Any]] | None = None,
                  reason: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": question.id,
        "session_id": question.session_id,
        "call_id": question.call_id,
        "outcome": outcome,
    }
    if outcome == "answered":
        data["answers"] = answers or []
    else:
        data["answers"] = []
        data["reason"] = reason or "cancelled"
    return data


def now() -> float:
    return time.time()
