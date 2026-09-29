"""Phase 4 skills-index context part: frozen, sanitized, whole-line, leak-free.

Covers the P4-F contract:

* the ``skills_index`` part renders deterministic, sanitized
  ``name: description`` lines from an injected snapshot and stays a no-op when
  the snapshot is empty;
* the index is frozen per turn (``for_turn``/``configure``/``freeze_skills``) and
  per iteration (``for_iteration``), and a per-iteration change never mutates an
  already-frozen assembler;
* ``context.limits.skills_index`` bounds the part on whole-line boundaries and
  its priority-1 budget is respected;
* only names and descriptions are read: bodies, resources, paths, bundled-tool
  candidates, and provenance never reach the prompt or the accounting metadata;
* an equal snapshot renders byte-identically and keeps the same semantic token
  key, while a changed index updates that key.

Everything runs offline against fake sessions; no provider is touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import (
    ConfigV2,
    ContextLimits,
    ContextSection,
    ModelSection,
)
from nexus.context import ContextManager
from nexus.context.counting import request_semantic_key
from nexus.context.parts import (
    IDENTITY_PREAMBLE,
    MAX_SKILLS_INDEX_DESCRIPTION_CHARS,
    PART_ORDER,
    PART_PRIORITY,
    AssemblyContext,
    PartOutput,
    builtin_parts,
    capture_environment,
    freeze_skills_index,
    render_parts,
)
from nexus.model.message import Message, Text
from nexus.model.request import ToolSchema
from nexus.skills.model import SkillIndex, SkillIndexEntry, SkillSource


class FakeSession:
    """The only surface ``assemble`` reads: structured history."""

    def __init__(self, *messages):
        self.id = "s1"
        self._messages = list(messages)

    @property
    def messages(self):
        return list(self._messages)


def config(*, skills_index: int = 4000) -> Config:
    return Config(
        model="anthropic/claude-test",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="anthropic/claude-test"),
            context=ContextSection(
                safety_margin_tokens=0,
                limits=ContextLimits(skills_index=skills_index),
            ),
        ),
    )


def user_message(text: str = "hi") -> Message:
    return Message(role="user", content=[Text(text=text)])


def entry(name: str, description: str) -> SkillIndexEntry:
    return SkillIndexEntry(
        name=name, description=description, source=SkillSource.BUILTIN
    )


class LeakySkill:
    """A full ``Skill``-shaped object; only name/description may be read."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.body = b"SECRET-BODY"
        self.allowed_tools = ("Bash",)
        self.bundles = ("shell",)
        self.resources = (object(),)
        self.tool_candidates = (object(),)
        self.provenance = object()


# ---------------------------------------------------------------------------
# Empty is a no-op
# ---------------------------------------------------------------------------


def test_empty_snapshot_renders_nothing(tmp_path):
    manager = ContextManager(tmp_path, config=config())
    request = manager.assemble(FakeSession(user_message()))

    assert request.system is not None
    assert IDENTITY_PREAMBLE == ""
    assert request.system.startswith("<environment>")
    assert "You are Nexus" not in request.system
    assert manager.skills_index == ()
    assert all(p["name"] != "skills_index" for p in manager.last_budget["parts"])


def test_part_render_is_none_without_a_snapshot(tmp_path):
    ctx = AssemblyContext(
        workspace=Path(tmp_path),
        config=config(),
        identity=IDENTITY_PREAMBLE,
        soul_text="",
        memory_text="",
        environment=capture_environment(tmp_path),
        tool_schemas=(),
        messages=(),
        current_user_index=None,
        capabilities=None,
        model=None,
        provider=None,
        max_file_bytes=1000,
    )
    by_name = {part.name: part for part in builtin_parts()}
    assert by_name["skills_index"].render(ctx) is None


def test_part_order_and_priority_are_unchanged():
    assert PART_ORDER[4] == "skills_index"
    assert PART_PRIORITY["skills_index"] == 1
    parts = builtin_parts()
    assert tuple(p.name for p in parts) == PART_ORDER


# ---------------------------------------------------------------------------
# Sanitization and determinism
# ---------------------------------------------------------------------------


def test_index_renders_sanitized_name_description_lines():
    lines = freeze_skills_index(
        [
            ("alpha", "first\nsecond\tthird"),
            ("beta", "  padded   spaces  "),
            ("gamma", "control\x01chars\x7fhere"),
        ]
    )
    assert lines == (
        "alpha: first second third",
        "beta: padded spaces",
        "gamma: control chars here",
    )
    assert all("\n" not in line and "\t" not in line for line in lines)


def test_overlong_description_is_bounded():
    lines = freeze_skills_index([("alpha", "x" * 5000)])
    assert len(lines) == 1
    name, _, description = lines[0].partition(": ")
    assert name == "alpha"
    assert description.endswith("\u2026")
    assert len(description) <= MAX_SKILLS_INDEX_DESCRIPTION_CHARS + 1


def test_index_is_deterministically_ordered():
    unordered = [("zeta", "z"), ("Alpha", "a"), ("beta", "b"), ("alpha", "a2")]
    first = freeze_skills_index(unordered)
    second = freeze_skills_index(list(reversed(unordered)))
    assert first == second
    # Case-insensitive by name, ties broken by the rendered line.
    assert first == (
        "Alpha: a",
        "alpha: a2",
        "beta: b",
        "zeta: z",
    )


def test_unordered_iterable_snapshot_is_stable():
    entries = {("zeta", "z"), ("Alpha", "a"), ("beta", "b")}
    assert freeze_skills_index(entries) == freeze_skills_index(entries)
    assert freeze_skills_index(entries) == (
        "Alpha: a",
        "beta: b",
        "zeta: z",
    )


def test_skill_manager_snapshot_is_accepted(tmp_path):
    from nexus.skills.manager import SkillManager

    root = tmp_path / "skills"
    for name in ("beta", "alpha"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description\n---\nBODY",
            encoding="utf-8",
        )
    manager = SkillManager(roots=[(SkillSource.WORKSPACE, root)])
    frozen = freeze_skills_index(manager)
    assert frozen == ("alpha: alpha description", "beta: beta description")


def test_equal_snapshots_render_byte_identically(tmp_path):
    snapshot = [("alpha", "one"), ("beta", "two")]
    first = ContextManager(tmp_path, config=config(), skills_index=snapshot)
    second = ContextManager(tmp_path, config=config(), skills_index=list(snapshot))
    a = first.assemble(FakeSession(user_message()))
    b = second.assemble(FakeSession(user_message()))
    assert a.system == b.system
    assert request_semantic_key(a) == request_semantic_key(b)


# ---------------------------------------------------------------------------
# Whole-line budget and priority
# ---------------------------------------------------------------------------


def test_one_hundred_skills_are_bounded_on_whole_lines(tmp_path):
    full = [entry(f"skill-{index:03d}", "x" * 60) for index in range(100)]
    manager = ContextManager(tmp_path, config=config(skills_index=60))
    manager.freeze_skills(full)
    request = manager.assemble(FakeSession(user_message()))

    part = next(p for p in manager.last_budget["parts"] if p["name"] == "skills_index")
    assert part["priority"] == 1
    assert part["cap"] == 60
    assert part["granted"] <= 60
    assert part["truncated"] is True

    # The skills block is a contiguous, whole-line prefix of the full index.
    skills_segment = next(
        segment
        for segment in request.system.split("\n\n")
        if segment.startswith("skill-")
    )
    rendered = skills_segment.splitlines()
    assert 0 < len(rendered) < 100
    expected = [f"skill-{index:03d}: {'x' * 60}" for index in range(100)]
    assert rendered == expected[: len(rendered)]


def test_index_fits_when_under_the_cap(tmp_path):
    manager = ContextManager(tmp_path, config=config(skills_index=4000))
    manager.freeze_skills([("alpha", "one"), ("beta", "two")])
    request = manager.assemble(FakeSession(user_message()))
    assert "alpha: one" in request.system
    assert "beta: two" in request.system
    part = next(p for p in manager.last_budget["parts"] if p["name"] == "skills_index")
    assert part["truncated"] is False


# ---------------------------------------------------------------------------
# No disclosure
# ---------------------------------------------------------------------------


def test_index_never_leaks_body_resource_path_tool_or_provenance(tmp_path):
    leaky = LeakySkill("leaky", "a visible description")
    manager = ContextManager(tmp_path, config=config(), skills_index=[leaky])
    request = manager.assemble(FakeSession(user_message()))

    assert "leaky: a visible description" in request.system
    for secret in (
        "SECRET-BODY",
        "Bash",
        "shell",
        "object at",
        "provenance",
        "tool_candidates",
    ):
        assert secret not in request.system
    assert "SECRET-BODY" not in str(manager.last_accounting)


def test_skill_index_object_is_accepted_without_leaking_provenance(tmp_path):
    snapshot = SkillIndex(
        entries=(entry("alpha", "one"), entry("beta", "two")),
        diagnostics=(),
        generation=7,
    )
    manager = ContextManager(tmp_path, config=config(), skills_index=snapshot)
    request = manager.assemble(FakeSession(user_message()))
    assert "alpha: one" in request.system
    assert "beta: two" in request.system


# ---------------------------------------------------------------------------
# Cache semantics
# ---------------------------------------------------------------------------


def test_index_change_updates_the_semantic_token_key(tmp_path):
    manager = ContextManager(tmp_path, config=config())
    manager.freeze_skills([("alpha", "one")])
    first = manager.assemble(FakeSession(user_message()))
    manager.freeze_skills([("alpha", "two")])
    second = manager.assemble(FakeSession(user_message()))

    assert first.system != second.system
    assert request_semantic_key(first) != request_semantic_key(second)


# ---------------------------------------------------------------------------
# Per-turn and per-iteration seams
# ---------------------------------------------------------------------------


def test_configure_and_for_turn_freeze_the_index(tmp_path):
    manager = ContextManager(tmp_path, config=config())
    manager.configure(skills_index=[("alpha", "one")])
    snapshot = manager.for_turn()
    # A later configure on the parent does not change the frozen turn.
    manager.configure(skills_index=[("beta", "two")])
    assert "alpha: one" in snapshot.assemble(FakeSession(user_message())).system
    assert "beta: two" not in snapshot.assemble(FakeSession(user_message())).system


def test_for_turn_accepts_an_index_override(tmp_path):
    manager = ContextManager(tmp_path, config=config(), skills_index=[("alpha", "one")])
    snapshot = manager.for_turn(skills_index=[("gamma", "three")])
    system = snapshot.assemble(FakeSession(user_message())).system
    assert "gamma: three" in system
    assert "alpha: one" not in system


def test_per_iteration_index_leaves_the_frozen_turn_stable(tmp_path):
    turn = ContextManager(
        tmp_path, config=config(), skills_index=[("alpha", "one")]
    ).for_turn()

    first = turn.assemble(FakeSession(user_message()))
    iteration = turn.for_iteration(skills_index=[("gamma", "three")])
    changed = iteration.assemble(FakeSession(user_message()))
    again = turn.assemble(FakeSession(user_message()))

    assert "gamma: three" in changed.system
    assert "alpha: one" not in changed.system
    # The original frozen assembler is byte-for-byte unchanged.
    assert again.system == first.system
    assert request_semantic_key(again) == request_semantic_key(first)
    assert turn.skills_index == ("alpha: one",)
    assert iteration.skills_index == ("gamma: three",)


def test_for_iteration_without_an_override_clones_the_index(tmp_path):
    turn = ContextManager(
        tmp_path, config=config(), skills_index=[("alpha", "one")]
    ).for_turn()
    clone = turn.for_iteration()
    assert clone is not turn
    assert clone.skills_index == turn.skills_index
    assert clone.assemble(FakeSession(user_message())).system == (
        turn.assemble(FakeSession(user_message())).system
    )


def test_clearing_the_index_returns_to_noop(tmp_path):
    manager = ContextManager(tmp_path, config=config(), skills_index=[("alpha", "one")])
    manager.freeze_skills(())
    request = manager.assemble(FakeSession(user_message()))
    assert "alpha" not in request.system


def test_skills_alias_is_accepted_at_every_seam(tmp_path):
    manager = ContextManager(tmp_path, config=config(), skills=[("alpha", "one")])
    assert manager.skills_index == ("alpha: one",)

    manager.configure(skills=[("beta", "two")])
    assert manager.skills_index == ("beta: two",)

    turn = manager.for_turn(skills=[("gamma", "three")])
    assert turn.skills_index == ("gamma: three",)

    iteration = turn.for_iteration(skills=[("delta", "four")])
    assert iteration.skills_index == ("delta: four",)
    assert turn.skills_index == ("gamma: three",)


# ---------------------------------------------------------------------------
# Phase 2 / Phase 3 behavior stays intact
# ---------------------------------------------------------------------------


def test_tool_schemas_stay_structured_alongside_the_index(tmp_path):
    schema = ToolSchema(
        name="Read",
        description="SECRET-TOOL-DESCRIPTION",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    manager = ContextManager(tmp_path, config=config(), skills_index=[("alpha", "one")])
    manager.freeze_tools([schema])
    request = manager.assemble(FakeSession(user_message()))

    assert request.tools == [schema]
    assert "alpha: one" in request.system
    assert "SECRET-TOOL-DESCRIPTION" not in request.system


def test_async_counter_still_returns_an_awaitable_with_the_index(tmp_path):
    import asyncio

    calls = {"n": 0}

    async def counter(text: str) -> int:
        calls["n"] += 1
        return max(1, len(text) // 2)

    manager = ContextManager(
        tmp_path, config=config(), counter=counter, skills_index=[("alpha", "one")]
    )
    result = manager.assemble(FakeSession(user_message()))
    assert hasattr(result, "__await__")
    request = asyncio.run(result)
    assert "alpha: one" in request.system
    assert calls["n"] > 0


def test_render_parts_keeps_slots_aligned_with_the_index(tmp_path):
    manager = ContextManager(tmp_path, config=config(), skills_index=[("alpha", "one")])
    ctx = AssemblyContext(
        workspace=Path(tmp_path),
        config=config(),
        identity=IDENTITY_PREAMBLE,
        soul_text="",
        memory_text="",
        environment=capture_environment(tmp_path),
        tool_schemas=(),
        messages=(),
        current_user_index=None,
        capabilities=None,
        model=None,
        provider=None,
        max_file_bytes=1000,
        skills_index=manager.skills_index,
    )
    outputs = render_parts(builtin_parts(), ctx)
    assert len(outputs) == len(PART_ORDER)
    by_name = {name: output for name, output in zip(PART_ORDER, outputs)}
    index_output = by_name["skills_index"]
    assert isinstance(index_output, PartOutput)
    assert index_output.kind == "skills_index"
    assert index_output.lines == ("alpha: one",)
    assert by_name["mcp_index"] is None
    assert by_name["attachments"] is None


@pytest.mark.parametrize("source", [[], (), None, ""])
def test_empty_sources_freeze_to_nothing(source):
    assert freeze_skills_index(source) == ()
