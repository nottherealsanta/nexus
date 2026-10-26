"""Shared fuzzy matcher behavior."""

from __future__ import annotations

from nexus.ui_support.fuzzy import fuzzy_filter, fuzzy_match, highlight_spans


def test_subsequence_positions() -> None:
    score, pos = fuzzy_match("gm", "git merge")  # type: ignore[misc]
    assert pos == (0, 4)
    assert isinstance(score, int)


def test_case_insensitive() -> None:
    assert fuzzy_match("GIT", "git merge")[1] == (0, 1, 2)  # type: ignore[index]
    assert fuzzy_match("git", "GIT MERGE")[1] == (0, 1, 2)  # type: ignore[index]


def test_no_match() -> None:
    assert fuzzy_match("xyz", "git merge") is None
    assert fuzzy_match("mg", "git") is None
    assert fuzzy_match("tig", "git") is None


def test_empty_query() -> None:
    assert fuzzy_match("", "anything") == (0, ())
    assert fuzzy_match("   ", "anything") == (0, ())


def test_boundary_bonus() -> None:
    ranked = [i for i, _ in fuzzy_filter("gm", ["fragment", "git merge"])]
    assert ranked == ["git merge", "fragment"]
    camel = fuzzy_match("gm", "getModel")[0]  # type: ignore[index]
    assert camel > fuzzy_match("gm", "agamma")[0]  # type: ignore[index]


def test_exact_substring_beats_scattered() -> None:
    ranked = [i for i, _ in fuzzy_filter("model", ["mxoxdxexl", "xxmodelxx"])]
    assert ranked[0] == "xxmodelxx"


def test_stable_ties() -> None:
    items = ["abc 1", "abc 2", "abc 3"]
    assert [i for i, _ in fuzzy_filter("abc", items)] == items


def test_filter_key_and_drops() -> None:
    items = [("a", "git merge"), ("b", "nothing"), ("c", "fragment")]
    out = fuzzy_filter("gm", items, key=lambda t: t[1])
    assert [i[0] for i, _ in out] == ["a", "c"]


def test_highlight_spans() -> None:
    assert highlight_spans([0, 1, 2, 5, 7, 8]) == [(0, 3), (5, 6), (7, 9)]
    assert highlight_spans([]) == []


def test_bounds() -> None:
    assert fuzzy_match("a", "b" * 600 + "a") is None
    assert fuzzy_match("a" * 200, "a" * 200) is not None


