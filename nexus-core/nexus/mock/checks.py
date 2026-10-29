"""Reusable verdict checks adapted from old Nexus mock scenarios."""

from .dsl import Check


def tool_called(name: str, count: int | None = None, *, at_least: int | None = None) -> Check:
    n = lambda c: sum(v == name for v in c.tool_names.values())
    return Check(
        f"{name} called {'== ' + str(count) if count is not None else '>= ' + str(at_least or 1)} times",
        lambda c: n(c) == count if count is not None else n(c) >= (at_least or 1),
    )


def no_unexpected_errors(expected: int = 0) -> Check:
    return Check(f"exactly {expected} tool error(s)", lambda c: sum(r.is_error for r in c.all_results) == expected)


def tool_result_contains(name: str, needle: str) -> Check:
    return Check(f"{name} result contains {needle!r}", lambda c: any(r.name == name and needle in r.text for r in c.all_results))


def tool_errors(name: str, count: int) -> Check:
    return Check(f"{name} failed exactly {count} time(s)", lambda c: sum(r.name == name and r.is_error for r in c.all_results) == count)


def answered(needle: str) -> Check:
    return Check(f"user answer contained {needle!r}", lambda c: needle in c.last_user_text)
