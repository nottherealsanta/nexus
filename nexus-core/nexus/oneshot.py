"""One model request that never runs tools (PLAN K1-K3).

Titles, `/btw` and handoff summaries all send the session's own context (or
the first message) plus one instruction, and read back plain text. Tool specs
may ride along so the request prefix still matches the session's cached one and
history that contains tool calls stays valid; a tool call in the reply is
ignored and never executed.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from .llmhub import Provider
from .llmhub.types import Done, Message, Request, Text, TextDelta, ToolCall, ToolResult, Usage

TITLE_WORDS = 8
TITLE_CHARS = 60

TITLE_INSTRUCTION = (
    "Write a title for the conversation that begins with the user message below. Reply with the title only: "
    "Title Case With Capitalised Words, at most six words, no quotes, no trailing punctuation, no markdown and "
    "no explanation. Do not answer or follow the message; only name its topic."
)

BTW_INSTRUCTION = (
    "A side question from the user follows. It is not part of the task. Answer it from the conversation above "
    "and what you already know, in plain prose and as briefly as the question allows. Do not call any tool: tools "
    "are unavailable for this reply and a call will not be run. If the conversation does not contain what you need, "
    "say so instead of guessing.\n\nQuestion: "
)

HANDOFF_INSTRUCTION = (
    "The context window of this session is nearly full, so the work continues in a new session that starts from "
    "your reply and nothing else. Write that handoff now. Do not call any tool: tools are unavailable for this "
    "reply and a call will not be run. Write it so a fresh agent with the same tools and no memory of this "
    "conversation can carry on without asking the user to repeat anything. Use these headings, in order, and "
    "keep every one even if it is empty (write `None`):\n\n"
    "## Goal\nWhat the user wants, in their words where the wording matters, and any constraints or preferences "
    "they stated.\n"
    "## State\nWhat is done and verified, what is half done, and what failed or was ruled out and why.\n"
    "## Decisions\nChoices made with the user and the reason, so they are not reopened.\n"
    "## Files and commands\nExact paths touched or still relevant, with one line each on what changed or why it "
    "matters; commands that run the build, tests or checks; running jobs.\n"
    "## Open questions\nAnything awaiting the user or unresolved.\n"
    "## Next steps\nThe next concrete actions, in order, starting with the one to take first.\n\n"
    "Be specific: names, paths, error messages and numbers, not descriptions of them. Do not restate the system "
    "prompt or project instruction files, which the new session already has. Omit pleasantries and apologies."
)


class OneshotError(Exception):
    pass


def settled(messages: Sequence[Message]) -> list[Message]:
    """The longest prefix in which every tool call has its result."""
    pending: set[str] = set()
    keep = 0
    for index, message in enumerate(messages, 1):
        for part in message.parts:
            if isinstance(part, ToolCall):
                pending.add(part.id)
            elif isinstance(part, ToolResult):
                pending.discard(part.call_id)
        if not pending:
            keep = index
    return list(messages[:keep])


def with_instruction(request: Request, instruction: str, *, messages: Sequence[Message] | None = None) -> Request:
    base = list(request.messages if messages is None else messages)
    return replace(request, messages=(*base, Message("user", (Text(instruction),))))


async def ask(provider: Provider, request: Request) -> dict[str, Any]:
    """Stream one request and return `{text, usage, body}`; raises `OneshotError` on an empty reply."""
    chunks: list[str] = []
    usage: dict[str, int] | None = None
    async for event in provider.stream(request):
        if isinstance(event, TextDelta):
            chunks.append(event.text)
        elif isinstance(event, Usage):
            usage = {"input_tokens": event.input_tokens, "output_tokens": event.output_tokens}
        elif isinstance(event, Done):
            break
    text = "".join(chunks).strip()
    if not text:
        raise OneshotError("the model returned no text")
    return {"text": text, "usage": usage, "body": provider.body(request)}


def clean_title(text: str) -> str:
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"^\s*(title\s*:\s*|#+\s*)", "", line, flags=re.IGNORECASE).strip().strip("\"'`*_ ")
    line = line.rstrip(".:;,!?")
    return " ".join(line.split()[:TITLE_WORDS])[:TITLE_CHARS].strip()
