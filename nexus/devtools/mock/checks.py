"""Reusable verdict checks (MOCK_PLAN §6.3)."""
from __future__ import annotations

from .dsl import Check, Ctx

__all__ = [
    "answered",
    "child_errors",
    "child_results",
    "no_unexpected_errors",
    "tool_called",
    "tool_errors",
    "tool_result_contains",
]


def tool_called(name: str, count: int | None = None, *, at_least: int | None = None) -> Check:
    def _fn(ctx: Ctx) -> bool:
        n = sum(1 for v in ctx.tool_names().values() if v == name)
        if count is not None:
            return n == count
        return n >= (at_least if at_least is not None else 1)

    want = f"== {count}" if count is not None else f">= {at_least or 1}"
    return Check(f"{name} called {want} times", _fn)


def tool_errors(name: str, count: int) -> Check:
    return Check(
        f"{name} failed exactly {count} time(s)",
        lambda ctx: sum(1 for r in ctx.all_results if r.name == name and r.is_error) == count,
    )


def tool_result_contains(name: str, needle: str) -> Check:
    return Check(
        f"{name} result contains {needle!r}",
        lambda ctx: any(r.name == name and needle in r.text for r in ctx.all_results),
    )


def no_unexpected_errors(expected: int = 0) -> Check:
    return Check(
        f"exactly {expected} tool error(s)",
        lambda ctx: sum(1 for r in ctx.all_results if r.is_error) == expected,
    )


def child_results(count: int) -> Check:
    return Check(
        f"{count} subagent reports returned",
        lambda ctx: sum(1 for r in ctx.all_results if r.name == "subagent") == count,
    )


def child_errors(count: int) -> Check:
    return Check(
        f"{count} subagent report(s) flagged as error",
        lambda ctx: sum(1 for r in ctx.all_results if r.name == "subagent" and r.is_error) == count,
    )


def answered(needle: str) -> Check:
    return Check(f"user answer contained {needle!r}", lambda ctx: needle in ctx.last_user_text)
