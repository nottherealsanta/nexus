"""Deterministic context: pinned notes plus a suffix of complete exchanges."""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class Exchange:
    user: str
    assistant: str


@dataclass(frozen=True)
class Context:
    prompt: str
    omitted_exchanges: int


def build_context(instructions: str, memory: str, history: list[Exchange], user: str, limit: int) -> Context:
    if not isinstance(user, str) or not user.strip():
        raise ValueError("Message must be a nonempty string")

    def render(exchanges: list[Exchange], omitted: int) -> str:
        # JSON preserves roles and boundaries even when content contains delimiters.
        return json.dumps({
            "instructions": instructions,
            "memory": memory,
            "omitted_exchanges": omitted,
            "history": [{"user": e.user, "assistant": e.assistant} for e in exchanges],
            "user": user,
        }, ensure_ascii=False)

    selected: list[Exchange] = []
    prompt = render(selected, len(history))
    if len(prompt) > limit:
        raise ValueError("Instructions, memory and new message exceed context_chars; shorten them or raise the limit")
    for exchange in reversed(history):
        candidate = [exchange, *selected]
        candidate_prompt = render(candidate, len(history) - len(candidate))
        if len(candidate_prompt) > limit:
            break
        selected, prompt = candidate, candidate_prompt
    return Context(prompt, len(history) - len(selected))
