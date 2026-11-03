"""Retrieval behaviour (MANAGER_PLAN.md 7.1): windows, ties, fuzzy matching, handles, errors."""
from __future__ import annotations

import pytest
from manager_seed import project as make_project
from manager_seed import seed

from nexus.manager.retrieval import (
    HandleRegistry,
    ProjectNotFound,
    RankingContext,
    RetrievalError,
    Retriever,
    _normalize_handle,
    _query_tokens,
)
from nexus.store import Store

NOW = 1_800_000_000.0


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "r.db")
    projects = {name: make_project(store, tmp_path, name) for name in ("nexus", "docs")}
    return store, projects, Retriever(store, clock=lambda: NOW)


def add(world, project, title, user="x", answer=None, age=0.1, **kwargs):
    store, projects, _ = world
    return seed(store, projects[project], title=title, user=user, answer=answer, now=NOW, age_days=age, **kwargs)


def test_a_command_verb_is_dropped_only_at_the_start():
    assert _query_tokens("find the sidebar session") == ("sidebar",)
    assert _query_tokens("please search for the search session") == ("search",)
    assert _query_tokens("the search session") == ("search",)
    assert _query_tokens("the") == ("the",)  # all stop words: keep what was said


def test_an_exact_older_match_beats_a_loose_recent_one(world):
    add(world, "nexus", "Fix rail tooltip overflow", age=0.5)
    wanted = add(world, "nexus", "Worktree creation tool", age=12)
    result = world[2].find_sessions("worktree creation tool")
    assert result.best.session_id == wanted.id and result.choice.session_id == wanted.id


def test_two_equally_good_matches_are_a_question_whichever_is_newer(world):
    newer = add(world, "nexus", "Sidebar resize handle", age=0.2)
    older = add(world, "docs", "Docs sidebar navigation", age=20)
    result = world[2].find_sessions("the sidebar one")
    assert result.ambiguous and result.choice is None and result.best.session_id == newer.id
    assert {c.session_id for c in result.ambiguity.candidates} == {newer.id, older.id}
    assert result.ambiguity.question.startswith("Which one did you mean")
    assert result.as_dict()["ambiguous"] is True and result.as_dict()["choice"] is None


def test_context_can_break_a_tie_but_only_with_real_evidence(world):
    on_screen = add(world, "nexus", "Sidebar resize handle", age=3)
    add(world, "docs", "Docs sidebar navigation", age=3)
    retriever = world[2]
    assert retriever.find_sessions("sidebar").ambiguous
    result = retriever.find_sessions("sidebar", context=RankingContext(on_screen_session=on_screen.id))
    assert not result.ambiguous and result.choice.session_id == on_screen.id and "on screen" in result.best.boosts


def test_a_named_project_boosts_its_sessions_and_a_mismatch_is_not_confident(world):
    add(world, "nexus", "Sidebar resize handle", age=3)
    docs = add(world, "docs", "Docs sidebar navigation", age=3)
    result = world[2].find_sessions("sidebar in docs")
    assert result.choice.session_id == docs.id and "project named" in result.best.boosts
    missing = world[2].find_sessions("resize handle in docs")
    assert missing.best is not None and missing.weak and missing.choice is None
    assert any("another project" in note for note in missing.notes)


def test_a_loose_prefix_is_not_a_confident_match(world):
    add(world, "nexus", "Fix rail tooltip overflow")
    result = world[2].find_sessions("the over cut")
    assert result.choice is None


def test_fuzzy_matching_finds_a_mangled_title_and_a_weak_match_is_not_chosen(world):
    wanted = add(world, "nexus", "Terraform state migration", age=6)
    retriever = world[2]
    result = retriever.find_sessions("tara form state migration")
    assert result.best.session_id == wanted.id
    assert retriever.find_sessions("quantum chromodynamics").results == ()


def test_a_session_is_found_by_its_final_answer_and_says_so(world):
    wanted = add(world, "nexus", "Plain title", answer="We moved the cursor lock into the sync worker.")
    result = world[2].find_sessions("cursor lock")
    assert result.best.session_id == wanted.id and result.best.why.startswith("an answer said")


def test_archived_sessions_are_included_by_default_and_can_be_left_out(world):
    old = add(world, "nexus", "Archived work", archived=True, age=30)
    retriever = world[2]
    assert retriever.find_sessions("archived work").best.session_id == old.id
    assert retriever.find_sessions("archived work").best.archived is True
    assert retriever.find_sessions("archived work", include_archived=False).results == ()
    assert retriever.find_sessions("x", state="archived").best.session_id == old.id


def test_since_is_a_hard_window_and_browsing_widens_with_a_note(world):
    add(world, "nexus", "Old one", age=10)
    add(world, "nexus", "Older one", age=40)
    retriever = world[2]
    hard = retriever.find_sessions("", since=NOW - 2 * 86400)
    assert hard.results == () and any("No sessions since" in n for n in hard.notes)
    browse = retriever.find_sessions("")
    assert browse.widened and [r.title for r in browse.results] == ["Old one"]
    assert any("widened to 30 days" in n for n in browse.notes)
    assert retriever.find_sessions("", widen=False).results == ()


def test_results_are_clipped_to_the_limit_and_say_so(world):
    for i in range(5):
        add(world, "nexus", f"Report number {i}", age=0.1 * (i + 1))
    result = world[2].find_sessions("", limit=2)
    assert len(result.results) == 2 and result.clipped == 3 and result.total_matched == 5
    assert any("Showing 2 of 5" in n for n in result.notes)
    capped = world[2].find_sessions("", limit=500)
    assert any("Limit capped at 50" in n for n in capped.notes)


def test_bad_requests_are_explained(world):
    retriever = world[2]
    with pytest.raises(RetrievalError, match="unknown session state"):
        retriever.find_sessions("x", state="napping")
    with pytest.raises(RetrievalError, match="needs states="):
        retriever.find_sessions("x", state="working")
    with pytest.raises(ProjectNotFound, match="known projects: docs, nexus"):
        retriever.find_sessions("x", project="elsewhere")
    with pytest.raises(ValueError):
        retriever.find_sessions("x", limit=0)
    with pytest.raises(ValueError):
        Retriever(world[0], recency_floor=1.0)


def test_a_state_filter_uses_the_unified_state_map(world):
    working = add(world, "nexus", "Busy one")
    add(world, "nexus", "Idle one")
    result = world[2].find_sessions("", state="working", states={working.id: "working"})
    assert [r.session_id for r in result.results] == [working.id]
    assert result.results[0].state == "working"


def test_the_project_filter_resolves_id_name_and_path(world):
    _store, projects, retriever = world
    add(world, "docs", "Docs thing")
    add(world, "nexus", "Nexus thing")
    for ref in (projects["docs"].id, "docs", "DOCS", projects["docs"].path):
        assert [r.title for r in retriever.find_sessions("", project=ref).results] == ["Docs thing"]


def test_handles_are_derived_unique_stable_and_resolvable(world):
    a = add(world, "nexus", "The Auth Refactor!!")
    b = add(world, "docs", "The Auth Refactor!!")
    retriever = world[2]
    one, two = retriever.handle_for(a), retriever.handle_for(b)
    assert one == "auth-refactor" and two == "auth-refactor-2"
    assert retriever.resolve("Auth Refactor").id == a.id and retriever.resolve_id("auth_refactor_2") == b.id
    assert retriever.resolve("nothing") is None
    assert _normalize_handle('  "Auth--Refactor" ') == "auth-refactor"


def test_an_empty_registry_passed_in_is_the_one_used(world):
    seen: list = []
    registry = HandleRegistry(on_assign=lambda sid, handle: seen.append((sid, handle)))
    retriever = Retriever(world[0], handles=registry)  # an empty registry is falsy: it must not be swapped out
    session = add(world, "nexus", "Persisted work")
    retriever.handle_for(session)
    assert retriever.handles is registry and seen == [(session.id, "persisted-work")]


def test_duplicate_persisted_handles_are_rejected():
    with pytest.raises(ValueError, match="duplicate handle"):
        HandleRegistry({"a": "same", "b": "same"})


def test_branch_matching_is_inert_without_a_real_branch(world):
    add(world, "nexus", "Plain work")
    result = world[2].find_sessions("feature login")
    assert result.results == ()
