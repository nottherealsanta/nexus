"""First-party Claude thinking compatibility, separate from request serialization."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThinkingPolicy:
    mode: str = "none"
    efforts: tuple[str, ...] = ()
    display: bool = False
    off: str = "disabled"
    prefix_binding: bool = False


def model_version(model: str) -> tuple[str, tuple[int, int]] | None:
    match = re.match(r"^claude-(haiku|sonnet|opus|fable|mythos)-(\d+)(?:[.-](\d{1,2})(?=-|$))?(?:-|$)", model)
    if not match:
        match = re.match(r"^claude-(\d+)(?:[.-](\d{1,2}))?-(haiku|sonnet|opus)(?:-|$)", model)
        if not match:
            return None
        major, minor, family = match.groups()
    else:
        family, major, minor = match.groups()
        suffix = model[match.end():]
        if minor is None and suffix and suffix[0].isdigit() and not re.match(r"^\d{8}(?:-|$)", suffix):
            return None
    if minor is None and len(major) == 2:
        major, minor = major[0], major[1]
    return family, (int(major), int(minor or 0))


def thinking_policy(model: str) -> ThinkingPolicy:
    match = model_version(model)
    if not match:
        return ThinkingPolicy()
    family, version = match
    basic = ("low", "medium", "high")
    if family in {"fable", "mythos"} and (5, 0) <= version < (6, 0):
        return ThinkingPolicy("adaptive", (*basic, "xhigh", "max"), True, "low", version >= (5, 1))
    if family == "haiku":
        if (5, 5) <= version < (6, 0):
            return ThinkingPolicy("adaptive", (*basic, "xhigh", "max"), True, prefix_binding=True)
        if (4, 5) <= version < (5, 5):
            return ThinkingPolicy("budget", ("minimal", *basic, "xhigh", "max"))
        return ThinkingPolicy()
    if (5, 5) <= version < (6, 0):
        return ThinkingPolicy("adaptive", (*basic, "xhigh", "max"), True,
                              "between_tools" if family == "sonnet" else "low", True)
    if (4, 6) <= version < (5, 5):
        efforts = (*basic, "xhigh", "max") if version >= (4, 7) else (*basic, "max") if family == "opus" else basic
        return ThinkingPolicy("adaptive", efforts, family == "opus" and version >= (4, 7), "low")
    if family == "opus" and version == (4, 5):
        return ThinkingPolicy("budget-effort", (*basic, "max"))
    if ((3, 7) if family == "sonnet" else (4, 0)) <= version < (4, 6):
        return ThinkingPolicy("budget", ("minimal", *basic, "xhigh", "max"))
    return ThinkingPolicy()


def image_limit(model: str) -> int:
    match = model_version(model)
    return 600 if match and match[1] >= (4, 6) else 100
