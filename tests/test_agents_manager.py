"""Tests for the Phase 6 P6A agent manager, parser, and model.

Covers the restricted frontmatter grammar, discovery precedence and shadow
diagnostics, deletion fallback, once-per-workspace seeding (and its marker),
object reuse and stable fingerprints, progressive disclosure, resource bounds,
and the security invariants: declarations are never grants, and ``explore`` /
``planner`` are structurally denied shell and mutating-filesystem tools.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nexus.agents import (
    DEFAULT_AGENT_COLORS,
    FORBIDDEN_ROLE_TOOLS,
    MAX_FALLBACKS,
    MUTATING_FS_TOOLS,
    READ_ONLY_ROLES,
    SEED_MARKER_NAME,
    SEED_VERSION,
    SEEDED_ROLES,
    AgentDef,
    AgentDiagnosticCode,
    AgentError,
    AgentManager,
    AgentNotFoundError,
    AgentOversizeError,
    AgentParseError,
    AgentSeedError,
    AgentSource,
    AgentStaleError,
    default_agent_color,
    is_model_tier,
    parse_frontmatter,
    read_agent_file,
    retire_seeded_roles,
    seed_workspace_roles,
    validate_agent_name,
)
from nexus.agents.manager import _default_seed_source

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_agent(
    root: Path,
    name: str,
    *,
    description: str = "An agent",
    body: str = "PROMPT",
    bundles: list[str] | None = None,
    tools: list[str] | None = None,
    model: str | None = None,
    max_iterations: int | None = None,
    context_tokens: int | None = None,
    filename: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", f"description: {description}"]
    if bundles is not None:
        lines.append("bundles: [" + ", ".join(bundles) + "]")
    if tools is not None:
        lines.append("tools: [" + ", ".join(tools) + "]")
    if model is not None:
        lines.append(f"model: {model}")
    if max_iterations is not None:
        lines.append(f"max_iterations: {max_iterations}")
    if context_tokens is not None:
        lines.append(f"context_tokens: {context_tokens}")
    path = root / f"{filename if filename is not None else name}.md"
    path.write_text(
        "---\n" + "\n".join(lines) + "\n---\n" + body, encoding="utf-8"
    )
    return path


def doc(*lines: str, body: str = "") -> str:
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def manager(tmp_path: Path, **kwargs) -> AgentManager:
    return AgentManager(
        roots=[
            (AgentSource.BUILTIN, tmp_path / "builtin"),
            (AgentSource.USER, tmp_path / "user"),
            (AgentSource.WORKSPACE, tmp_path / "workspace"),
        ],
        **kwargs,
    )


def seed_bundle_map() -> dict[str, list[str]]:
    return {"fs": ["Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"]}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_all_fields():
    parsed = parse_frontmatter(
        doc(
            "name: explorer",
            "description: read-only search",
            "bundles: [fs, task]",
            "tools: [Read, -Write, -Edit]",
            "model: low",
            "max_iterations: 30",
            "context_tokens: 100000",
            body="BODY",
        )
    )
    assert parsed.name == "explorer"
    assert parsed.description == "read-only search"
    assert parsed.bundles == ("fs", "task")
    assert parsed.tools == ("read",)
    assert parsed.excluded_tools == ("write", "edit")
    assert parsed.model == "low"
    assert parsed.max_iterations == 30
    assert parsed.context_tokens == 100_000
    assert parsed.contexts == ("subagent",)
    assert b"name: explorer" in parsed.raw
    assert b"BODY" not in parsed.raw


def test_optional_model_provider_effort_and_color_fields():
    parsed = parse_frontmatter(
        doc(
            "name: reviewer",
            "description: d",
            "model: anthropic/claude-opus-5",
            "provider: anthropic",
            "reasoning_effort: xhigh",
            "color: #aBcD09",
        )
    )
    assert parsed.provider == "anthropic"
    assert parsed.reasoning_effort == "xhigh"
    assert parsed.color == "#ABCD09"

    for effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
        assert parse_frontmatter(
            doc("name: agent", "description: d", f"reasoning_effort: {effort}")
        ).reasoning_effort == effort


def test_agent_reasoning_effort_none_is_accepted():
    assert parse_frontmatter(
        doc("name: agent", "description: d", "reasoning_effort: none")
    ).reasoning_effort == "none"


def test_invalid_agent_provider_effort_color_and_qualified_conflict_rejected():
    for field in (
        "color: red",
        "color: #12345",
        "color: #12345678",
        "color: #12GG56",
        "provider: bad/provider",
        "reasoning_effort: ultra",
        "reasoning_effort: MAX",
    ):
        with pytest.raises(AgentParseError):
            parse_frontmatter(doc("name: agent", "description: d", field))
    with pytest.raises(AgentParseError, match="conflicts"):
        parse_frontmatter(
            doc(
                "name: agent",
                "description: d",
                "model: anthropic/claude-opus-5",
                "provider: openai",
            )
        )


def test_omitted_color_is_stable_for_normalized_name():
    parsed = parse_frontmatter(doc("name: My-Agent", "description: d"))
    assert parsed.color == default_agent_color("my-agent")
    assert parsed.color in DEFAULT_AGENT_COLORS
    assert default_agent_color("My-Agent") == default_agent_color("my-agent")


def test_contexts_parse_and_legacy_defaults_to_subagent():
    assert parse_frontmatter(doc(
        "name: both", "description: d", "contexts: [root, subagent]"
    )).contexts == ("root", "subagent")
    assert parse_frontmatter(doc(
        "name: root", "description: d", "contexts: [root]"
    )).contexts == ("root",)
    for value in ("[]", "[unknown]", "[root, root]"):
        with pytest.raises(AgentParseError):
            parse_frontmatter(doc("name: bad", "description: d", f"contexts: {value}"))


def test_parse_requires_name_and_description():
    with pytest.raises(AgentParseError, match="missing required"):
        parse_frontmatter(doc("name: only"))
    with pytest.raises(AgentParseError, match="missing required"):
        parse_frontmatter(doc("description: only"))


def test_unknown_and_duplicate_keys_are_rejected():
    with pytest.raises(AgentParseError, match="unknown frontmatter field"):
        parse_frontmatter(doc("name: a", "description: d", "version: 1"))
    with pytest.raises(AgentParseError, match="duplicate frontmatter field"):
        parse_frontmatter(doc("name: a", "name: b", "description: d"))


def test_invalid_name_rejected():
    for bad in ("-leading", "has space", "", "x" * 65, "bad/slash"):
        source = doc(f"name: {bad}", "description: d")
        with pytest.raises(AgentParseError):
            parse_frontmatter(source)
    with pytest.raises(AgentParseError):
        validate_agent_name("bad name")
    assert validate_agent_name("explore-2.0_test") == "explore-2.0_test"


def test_tools_exclusion_parsing_and_dedup():
    parsed = parse_frontmatter(
        doc("name: a", "description: d", "tools: [Bash, Bash, -Edit, -Edit]")
    )
    assert parsed.tools == ("bash",)
    assert parsed.excluded_tools == ("edit",)


def test_legacy_tool_declarations_migrate_exactly_and_report_collisions():
    parsed = parse_frontmatter(
        doc("name: a", "description: d", "tools: [Read, read, READ]")
    )
    assert parsed.tools == ("read", "READ")
    assert parsed.migration_notices == ("read",)


def test_collision_migration_diagnostics_are_bounded_without_widening_tools(tmp_path):
    legacy_and_canonical = [
        name
        for legacy, canonical in (
            ("Read", "read"),
            ("Bash", "bash"),
            ("Edit", "edit"),
            ("Write", "write"),
            ("Glob", "glob"),
            ("Grep", "grep"),
        )
        for name in (legacy, canonical)
    ]
    write_agent(tmp_path / "workspace", "demo", tools=legacy_and_canonical)
    mgr = manager(tmp_path)

    notices = [
        diagnostic
        for diagnostic in mgr.diagnostics
        if diagnostic.code is AgentDiagnosticCode.LEGACY_TOOL_NAME
    ]
    assert len(notices) == 5
    assert all("deduplicated" in diagnostic.message for diagnostic in notices)

    selection = mgr.select_tools("demo", available={"read"})
    assert selection.selected == frozenset({"read"})
    assert selection.selected <= selection.ceiling <= selection.available


def test_bundle_derived_legacy_tool_names_are_canonicalized_within_ceiling(tmp_path):
    write_agent(tmp_path / "workspace", "demo", bundles=["fs"])
    mgr = manager(tmp_path)
    selection = mgr.select_tools(
        "demo",
        available={"ls", "multiedit"},
        bundle_map={"fs": ["LS", "MultiEdit"]},
    )
    assert selection.requested == frozenset({"ls", "multiedit"})
    assert selection.selected == frozenset({"ls", "multiedit"})
    assert selection.selected <= selection.ceiling <= selection.available


def test_exclusion_wins_over_inclusion():
    parsed = parse_frontmatter(
        doc("name: a", "description: d", "tools: [Write, -Write]")
    )
    assert parsed.tools == ()
    assert parsed.excluded_tools == ("write",)


def test_model_is_opaque_and_shape_only():
    opaque = (
        "low",
        "medium",
        "high",
        "inherit",
        "anthropic/claude-opus-5",
        "gpt-5",
    )
    for value in opaque:
        assert parse_frontmatter(
            doc("name: a", "description: d", f"model: {value}")
        ).model == value
    with pytest.raises(AgentParseError, match="whitespace"):
        parse_frontmatter(doc("name: a", "description: d", "model: two words"))
    with pytest.raises(AgentParseError, match="at most"):
        parse_frontmatter(
            doc("name: a", "description: d", "model: " + "x" * 129)
        )
    assert is_model_tier("low")
    assert not is_model_tier("inherit")
    assert not is_model_tier("anthropic/claude-opus-5")


def test_integer_fields_are_validated():
    parsed = parse_frontmatter(
        doc("name: a", "description: d", "max_iterations: 7", "context_tokens: 500")
    )
    assert parsed.max_iterations == 7
    assert parsed.context_tokens == 500
    for bad in ("0", "-1", "1.5", "true", "many"):
        with pytest.raises(AgentParseError):
            parse_frontmatter(
                doc("name: a", "description: d", f"max_iterations: {bad}")
            )


def test_structural_yaml_is_rejected():
    cases = [
        doc("# a comment", "name: a", "description: d"),
        doc("name: a", "description: &anchor d"),
        doc("name: a", "description: |"),
        doc("name: a", "description: {nested: map}"),
        doc("name: a", "description: >"),
        doc("name: a", "description: d", "  bundles: [fs]"),
        doc("- name: a", "description: d"),
        doc("name: a", "description: d", "- item"),
    ]
    for source in cases:
        with pytest.raises(AgentParseError):
            parse_frontmatter(source)


def test_nul_byte_and_missing_delimiter_are_rejected():
    with pytest.raises(AgentParseError, match="NUL"):
        parse_frontmatter(
            doc("name: a", "description: d").replace("d\n---", "d\x00\n---")
        )
    with pytest.raises(AgentParseError, match="closing"):
        parse_frontmatter("---\nname: a\ndescription: d\n")
    with pytest.raises(AgentParseError, match="begin with"):
        parse_frontmatter("name: a\ndescription: d\n")


def test_bom_is_tolerated():
    source = b"\xef\xbb\xbf" + doc("name: a", "description: d").encode("utf-8")
    assert parse_frontmatter(source).name == "a"


def test_body_is_not_decoded_or_validated_by_the_parser():
    # A body may contain anything, including delimiter-looking lines.
    parsed = parse_frontmatter(
        doc("name: a", "description: d", body="---\nnot: frontmatter\n")
    )
    assert parsed.name == "a"


def test_oversize_frontmatter_is_rejected():
    with pytest.raises(AgentOversizeError):
        parse_frontmatter(doc("name: a", "description: d"), max_frontmatter_bytes=4)


def test_read_agent_file_snapshots_hashes(tmp_path):
    path = write_agent(tmp_path, "a", description="d", body="PROMPT")
    agent_file = read_agent_file(path)
    assert agent_file.parsed.name == "a"
    assert agent_file.body == b"PROMPT"
    assert agent_file.body_size == len(b"PROMPT")
    assert agent_file.file_sha256 and agent_file.declaration_sha256
    assert agent_file.body_sha256 != agent_file.declaration_sha256


# ---------------------------------------------------------------------------
# Discovery, precedence, deletion
# ---------------------------------------------------------------------------


def test_discovers_agents_across_roots(tmp_path):
    write_agent(tmp_path / "builtin", "alpha")
    write_agent(tmp_path / "user", "beta")
    write_agent(tmp_path / "workspace", "gamma")
    mgr = manager(tmp_path)
    assert mgr.names == ("alpha", "beta", "gamma")
    assert len(mgr) == 3
    assert "alpha" in mgr


def test_missing_roots_are_skipped(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.names == ()
    assert mgr.diagnostics == ()


def test_lookup_is_case_insensitive(tmp_path):
    write_agent(tmp_path / "workspace", "MyAgent")
    mgr = manager(tmp_path)
    assert mgr.get("myagent") is mgr.get("MYAGENT")
    assert mgr.require("MyAgent").name == "MyAgent"
    with pytest.raises(AgentNotFoundError):
        mgr.require("absent")


def test_roots_are_normalized_and_sorted_by_precedence(tmp_path):
    mgr = AgentManager(
        roots=[
            (AgentSource.WORKSPACE, tmp_path / "w"),
            (AgentSource.BUILTIN, tmp_path / "b"),
            (AgentSource.USER, tmp_path / "u"),
            (AgentSource.WORKSPACE, tmp_path / "w"),
        ]
    )
    assert [tier for tier, _path in mgr.roots] == [
        AgentSource.BUILTIN,
        AgentSource.USER,
        AgentSource.WORKSPACE,
    ]
    with pytest.raises(AgentError):
        AgentManager(roots=[("bogus", tmp_path)])


def test_workspace_shadows_user_shadows_builtin(tmp_path):
    write_agent(tmp_path / "builtin", "demo", description="builtin")
    write_agent(tmp_path / "user", "demo", description="user")
    write_agent(tmp_path / "workspace", "demo", description="workspace")
    mgr = manager(tmp_path)
    assert len(mgr) == 1
    assert mgr.require("demo").description == "workspace"
    assert mgr.require("demo").source is AgentSource.WORKSPACE
    assert len(mgr.shadowed) == 2
    assert {d.tier for d in mgr.shadowed} == {
        AgentSource.BUILTIN,
        AgentSource.USER,
    }
    assert all(d.code is AgentDiagnosticCode.SHADOWED for d in mgr.shadowed)


def test_deletion_falls_back_to_lower_tier(tmp_path):
    write_agent(tmp_path / "user", "demo", description="user")
    path = write_agent(tmp_path / "workspace", "demo", description="workspace")
    mgr = manager(tmp_path)
    assert mgr.require("demo").source is AgentSource.WORKSPACE

    path.unlink()
    mgr.refresh()
    assert mgr.require("demo").source is AgentSource.USER
    assert mgr.require("demo").description == "user"


def test_case_collision_within_one_tier_is_deterministic(tmp_path):
    write_agent(tmp_path / "ws-a", "Foo", description="upper")
    write_agent(tmp_path / "ws-b", "foo", description="lower")
    roots = [
        (AgentSource.WORKSPACE, tmp_path / "ws-a"),
        (AgentSource.WORKSPACE, tmp_path / "ws-b"),
    ]
    first = AgentManager(roots=roots)
    second = AgentManager(roots=roots)
    assert len(first) == 1
    assert first.names == second.names
    assert first.get("foo") is first.get("FOO")
    assert any(
        d.code is AgentDiagnosticCode.CASE_COLLISION for d in first.diagnostics
    )


def test_failed_definition_is_retained_as_a_diagnostic(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    (root / "broken.md").write_text("---\nname: broken\n---\n", encoding="utf-8")
    write_agent(root, "good")
    mgr = manager(tmp_path)
    assert mgr.names == ("good",)
    assert len(mgr.failures) == 1
    assert mgr.failures[0].code is AgentDiagnosticCode.PARSE_ERROR


def test_marker_and_non_markdown_files_are_ignored(tmp_path):
    root = tmp_path / "workspace"
    write_agent(root, "good")
    (root / ".seeded").write_text("{}", encoding="utf-8")
    (root / "notes.txt").write_text("not an agent", encoding="utf-8")
    mgr = manager(tmp_path)
    assert mgr.names == ("good",)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_for_workspace_serves_builtins_without_touching_the_workspace(tmp_path):
    workspace = tmp_path / "ws"
    mgr = AgentManager.for_workspace(workspace)
    assert set(mgr.names) == {"advisor", "build", "orchestrator", "quick", "task"}
    assert all(agent.source is AgentSource.BUILTIN for agent in mgr.agents)
    assert not (workspace / ".nexus").exists()


def test_user_override_shadows_the_builtin(tmp_path):
    home = tmp_path / "home"
    write_agent(home / ".nexus" / "agents", "task", body="MINE")
    mgr = AgentManager.for_workspace(tmp_path / "ws", home=home)
    assert mgr.require("task").source is AgentSource.USER
    assert mgr.load_body("task") == "MINE"


def test_for_workspace_seeds_the_canonical_roles_once_when_opted_in(tmp_path):
    workspace = tmp_path / "ws"
    mgr = AgentManager.for_workspace(workspace, seed=True)
    assert set(mgr.names) == set(SEEDED_ROLES)
    agents_dir = workspace / ".agents" / "agents"
    assert (agents_dir / SEED_MARKER_NAME).is_file()
    assert not (workspace / ".nexus").exists()
    for role in SEEDED_ROLES:
        assert (agents_dir / f"{role}.md").is_file()
        assert mgr.require(role).source is AgentSource.WORKSPACE

    report = seed_workspace_roles(workspace)
    assert report.already_seeded is True
    assert report.written == ()


def test_seeding_is_idempotent_and_does_not_touch_files(tmp_path):
    workspace = tmp_path / "ws"
    seed_workspace_roles(workspace)
    agents_dir = workspace / ".agents" / "agents"
    before = {
        path.name: path.read_bytes() for path in agents_dir.iterdir()
    }
    # A second construction must not rewrite anything.
    AgentManager.for_workspace(workspace, seed=True)
    after = {path.name: path.read_bytes() for path in agents_dir.iterdir()}
    assert before == after


def test_deleted_seed_is_not_recreated_but_falls_back_to_builtin(tmp_path):
    workspace = tmp_path / "ws"
    AgentManager.for_workspace(workspace, seed=True)
    target = workspace / ".agents" / "agents" / "advisor.md"
    target.unlink()

    mgr = AgentManager.for_workspace(workspace, seed=True)
    assert not target.exists()  # the marker prevents recreation
    assert mgr.require("advisor").source is AgentSource.BUILTIN

    # And again, to be sure the marker is durable.
    AgentManager.for_workspace(workspace, seed=True)
    assert not target.exists()


def test_untouched_current_seeds_are_retired_once_and_edited_ones_kept(tmp_path):
    import json
    import os

    from nexus.config.paths import project_state_dir

    workspace = tmp_path / "ws"
    agents_dir = workspace / ".agents" / "agents"
    old_build = write_agent(agents_dir, "build", description="old", body="OLD")
    edited = write_agent(agents_dir, "general", description="mine", body="EDITED")
    marker = agents_dir / SEED_MARKER_NAME
    marker.write_text(json.dumps({"version": 1, "seeded": ["build", "general"]}))
    stamp = marker.stat().st_mtime
    os.utime(old_build, (stamp, stamp))
    os.utime(edited, (stamp + 3600, stamp + 3600))

    mgr = AgentManager.for_workspace(workspace)
    assert not old_build.exists()
    trash_root = project_state_dir(workspace) / "trash" / "agents"
    assert list(trash_root.glob("*/build.md"))
    assert mgr.require("build").source is AgentSource.BUILTIN
    # The retired copy never lands back in the project.
    assert not (workspace / ".agents" / "trash").exists()
    assert (
        edited.exists()
        and mgr.require("general").source is AgentSource.WORKSPACE
    )
    state = json.loads(marker.read_text())
    assert state["version"] == SEED_VERSION and state["retired"] == ["build"]
    # A second run is a no-op.
    assert retire_seeded_roles(workspace) == ()


def test_manager_construction_leaves_legacy_seeded_roles_untouched(tmp_path):
    import json
    import os

    workspace = tmp_path / "ws"
    agents_dir = workspace / ".nexus" / "agents"
    old_build = write_agent(agents_dir, "build", description="old", body="OLD")
    marker = agents_dir / SEED_MARKER_NAME
    marker.write_text(
        json.dumps({"version": 1, "seeded": ["build"]}), encoding="utf-8"
    )
    stamp = marker.stat().st_mtime
    os.utime(old_build, (stamp, stamp))
    original_marker = marker.read_bytes()
    original_role = old_build.read_bytes()

    manager = AgentManager.for_workspace(workspace)

    assert old_build.read_bytes() == original_role
    assert marker.read_bytes() == original_marker
    assert manager.require("build").source is AgentSource.WORKSPACE_LEGACY


def test_default_user_agents_are_discovered_from_nexus_home(tmp_path, monkeypatch):
    nexus_home = tmp_path / "nexus-home"
    user_agents = nexus_home / "agents"
    write_agent(user_agents, "user-role", body="USER ROLE")
    monkeypatch.setenv("NEXUS_HOME", str(nexus_home))

    manager = AgentManager.for_workspace(tmp_path / "ws")

    agent = manager.require("user-role")
    assert agent.source is AgentSource.USER
    assert agent.path == user_agents / "user-role.md"


def test_marker_alone_prevents_seeding(tmp_path):
    workspace = tmp_path / "ws"
    agents_dir = workspace / ".nexus" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / SEED_MARKER_NAME).write_text("{}", encoding="utf-8")
    report = seed_workspace_roles(workspace)
    assert report.already_seeded is True
    assert not (agents_dir / "general.md").exists()


def test_seeding_does_not_overwrite_an_existing_definition(tmp_path):
    workspace = tmp_path / "ws"
    custom = write_agent(
        workspace / ".agents" / "agents", "task", description="mine"
    )
    report = seed_workspace_roles(workspace)
    assert "task" in report.skipped
    assert custom.read_text(encoding="utf-8").find("mine") != -1
    assert report.already_seeded is False


def test_seeding_refuses_to_follow_a_symlinked_target(tmp_path):
    workspace = tmp_path / "ws"
    agents_dir = workspace / ".agents" / "agents"
    agents_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("DO NOT TOUCH", encoding="utf-8")
    (agents_dir / "advisor.md").symlink_to(outside)

    report = seed_workspace_roles(workspace)
    assert "advisor" in report.skipped
    assert outside.read_text(encoding="utf-8") == "DO NOT TOUCH"
    assert (agents_dir / "advisor.md").is_symlink()


def test_seeding_reports_an_invalid_source_file(tmp_path):
    workspace = tmp_path / "ws"
    source = tmp_path / "src"
    source.mkdir()
    (source / "bad.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
    write_agent(source, "good")
    report = seed_workspace_roles(workspace, source=source)
    assert report.failed == ("bad",)
    assert report.written == ("good",)
    assert not (workspace / ".agents" / "agents" / "bad.md").exists()


def test_seeding_missing_source_is_reported_without_a_marker(tmp_path):
    workspace = tmp_path / "ws"
    report = seed_workspace_roles(workspace, source=tmp_path / "absent")
    assert report.source_missing is True
    assert report.already_seeded is False
    assert not (workspace / ".agents" / "agents" / SEED_MARKER_NAME).exists()


def test_default_seed_source_ships_all_roles():
    source = _default_seed_source()
    assert source.is_dir()
    for role in SEEDED_ROLES:
        assert (source / f"{role}.md").is_file()


def test_for_workspace_roots_are_builtin_user_workspace(tmp_path):
    workspace = tmp_path / "ws"
    mgr = AgentManager.for_workspace(workspace, home=tmp_path / "home")
    assert [tier for tier, _path in mgr.roots] == [
        AgentSource.BUILTIN,
        AgentSource.USER,
        AgentSource.WORKSPACE_LEGACY,
        AgentSource.WORKSPACE,
    ]


def test_builtin_roles_match_the_default_contract(tmp_path):
    mgr = AgentManager.for_workspace(tmp_path / "ws")
    assert mgr.require("build").contexts == ("root",)
    assert mgr.require("orchestrator").contexts == ("root",)
    assert all(mgr.require(name).contexts == ("subagent",) for name in ("advisor", "task", "quick"))
    assert mgr.require("advisor").read_only
    assert not mgr.require("task").read_only and not mgr.require("quick").read_only
    # Model, provider, effort and fallbacks are unset by default.
    for agent in mgr.agents:
        assert agent.model is None and agent.provider is None
        assert agent.reasoning_effort is None and agent.fallback == ()
    # Editing roles tell the root agent which files they changed.
    for name in ("task", "quick"):
        assert "Files changed" in mgr.load_body(name)
    assert len({mgr.require(name).color for name in SEEDED_ROLES}) == len(SEEDED_ROLES)


def test_legacy_names_resolve_to_the_new_builtins(tmp_path):
    mgr = AgentManager.for_workspace(tmp_path / "builtins")
    assert mgr.resolve("general", context="root").name == "build"
    assert mgr.resolve("general", context="subagent").name == "task"
    for legacy in ("explore", "plan", "planner"):
        assert mgr.resolve(legacy, context="subagent").name == "advisor"
    workspace_root = tmp_path / "custom" / ".nexus" / "agents"
    write_agent(workspace_root, "planner", body="CUSTOM")
    custom = AgentManager.for_workspace(tmp_path / "custom")
    assert (
        custom.resolve("planner", context="subagent").source
        is AgentSource.WORKSPACE_LEGACY
    )
    assert any(d.code is AgentDiagnosticCode.DEPRECATED_PLANNER for d in custom.diagnostics)


def test_for_workspace_agents_dir_wins_over_legacy_nexus(tmp_path):
    """STATE_PLAN §5.4: ``.agents/agents`` outranks the legacy ``.nexus/agents``."""
    workspace = tmp_path / "custom"
    write_agent(workspace / ".nexus" / "agents", "planner", body="LEGACY")
    write_agent(workspace / ".agents" / "agents", "planner", body="CURRENT")
    mgr = AgentManager.for_workspace(workspace)
    winner = mgr.resolve("planner", context="subagent")
    assert winner.source is AgentSource.WORKSPACE
    assert mgr.load_body("planner") == "CURRENT"


def test_fallback_list_is_parsed_bounded_and_fingerprinted(tmp_path):
    root = tmp_path / "workspace"
    path = write_agent(root, "demo")
    path.write_text(
        "---\nname: demo\ndescription: d\nfallback: [openai/gpt-5, low, anthropic/claude-sonnet-5]\n---\nx",
        encoding="utf-8",
    )
    mgr = manager(tmp_path)
    agent = mgr.require("demo")
    assert agent.fallback == ("openai/gpt-5", "low", "anthropic/claude-sonnet-5")
    assert mgr.index[0].fallback == agent.fallback
    many = ", ".join(f"p/m{i}" for i in range(MAX_FALLBACKS + 1))
    with pytest.raises(AgentParseError):
        parse_frontmatter(f"---\nname: demo\ndescription: d\nfallback: [{many}]\n---\n")
    with pytest.raises(AgentParseError):
        parse_frontmatter("---\nname: demo\ndescription: d\nfallback: [has space]\n---\n")


# ---------------------------------------------------------------------------
# Fingerprints and object reuse
# ---------------------------------------------------------------------------


def test_refresh_reuses_unchanged_objects_and_rebuilds_changed_ones(tmp_path):
    root = tmp_path / "workspace"
    path = write_agent(root, "demo", body="one")
    mgr = manager(tmp_path)
    first = mgr.require("demo")
    fingerprint = first.fingerprint()

    mgr.refresh()
    assert mgr.require("demo") is first
    assert mgr.require("demo").fingerprint() == fingerprint

    path.write_text(
        "---\nname: demo\ndescription: An agent\n---\ntwo", encoding="utf-8"
    )
    mgr.refresh()
    assert mgr.require("demo") is not first
    assert mgr.require("demo").fingerprint() != fingerprint


def test_identical_definition_in_a_new_tier_is_not_reused(tmp_path):
    write_agent(tmp_path / "user", "demo", description="same")
    write_agent(tmp_path / "workspace", "demo", description="same")
    mgr = manager(tmp_path)
    workspace_agent = mgr.require("demo")
    assert workspace_agent.source is AgentSource.WORKSPACE

    workspace_agent.path.unlink()
    mgr.refresh()
    user_agent = mgr.require("demo")
    assert user_agent.source is AgentSource.USER
    assert user_agent is not workspace_agent
    assert user_agent.path == tmp_path / "user" / "demo.md"


def test_declaration_hash_ignores_body_but_tracks_frontmatter(tmp_path):
    root = tmp_path / "workspace"
    path = write_agent(root, "demo", description="d", body="one")
    first = manager(tmp_path).require("demo").declaration_sha256

    path.write_text(
        "---\nname: demo\ndescription: d\n---\ntwo", encoding="utf-8"
    )
    assert manager(tmp_path).require("demo").declaration_sha256 == first

    path.write_text(
        "---\nname: demo\ndescription: changed\n---\ntwo", encoding="utf-8"
    )
    assert manager(tmp_path).require("demo").declaration_sha256 != first


def test_manager_fingerprints_are_stable_and_map_identity(tmp_path):
    root = tmp_path / "workspace"
    write_agent(root, "alpha", body="A")
    write_agent(root, "beta", body="B")
    first = manager(tmp_path)
    second = manager(tmp_path)
    assert first.fingerprints == second.fingerprints
    assert set(first.fingerprints) == {"alpha", "beta"}
    assert first.fingerprint() == second.fingerprint()
    assert all(
        value == first.require(name).fingerprint()
        for name, value in first.fingerprints.items()
    )
    assert (
        AgentManager.fingerprint_key(first.require("alpha"))
        == first.require("alpha").fingerprint()
    )


def test_new_fields_flow_through_manager_index_and_fingerprint(tmp_path):
    root = tmp_path / "workspace"
    path = write_agent(root, "worker")
    path.write_text(
        doc(
            "name: worker",
            "description: agent",
            "model: anthropic/claude-opus-5",
            "provider: anthropic",
            "reasoning_effort: high",
            "color: #123ABC",
        ),
        encoding="utf-8",
    )
    mgr = manager(tmp_path)
    agent = mgr.require("worker")
    assert (agent.provider, agent.reasoning_effort, agent.color) == (
        "anthropic",
        "high",
        "#123ABC",
    )
    entry = mgr.index[0]
    assert (entry.provider, entry.reasoning_effort, entry.color) == (
        "anthropic",
        "high",
        "#123ABC",
    )
    fingerprint = agent.fingerprint()

    path.write_text(
        doc(
            "name: worker",
            "description: agent",
            "model: anthropic/claude-opus-5",
            "provider: anthropic",
            "reasoning_effort: xhigh",
            "color: #123ABC",
        ),
        encoding="utf-8",
    )
    mgr.refresh()
    assert mgr.require("worker").fingerprint() != fingerprint


def test_definitions_are_immutable(tmp_path):
    write_agent(tmp_path / "workspace", "demo")
    agent = manager(tmp_path).require("demo")
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        agent.name = "other"  # type: ignore[misc]


def test_agent_def_satisfies_the_manifest_protocol(tmp_path):
    write_agent(tmp_path / "workspace", "demo")
    agent = manager(tmp_path).require("demo")
    assert isinstance(agent, AgentDef)
    assert agent.name == "demo"  # the manifest AgentDef protocol needs ``name``
    assert agent.fingerprint()


# ---------------------------------------------------------------------------
# Progressive disclosure, bodies, resource bounds
# ---------------------------------------------------------------------------


def test_index_is_sanitized_and_bodies_stay_lazy(tmp_path):
    root = tmp_path / "workspace"
    write_agent(root, "demo", description="demo agent", body="SECRET-PROMPT")
    mgr = manager(tmp_path)
    rendered = mgr.render_index()
    assert "SECRET-PROMPT" not in rendered
    assert rendered == "demo: demo agent"
    assert mgr.load_body("demo") == "SECRET-PROMPT"


def test_render_index_respects_a_character_budget(tmp_path):
    root = tmp_path / "workspace"
    for index in range(5):
        write_agent(root, f"a{index}", description="x" * 20)
    mgr = manager(tmp_path)
    full = mgr.render_index()
    limited = mgr.render_index(max_chars=30)
    assert 0 < len(limited) <= 30
    assert full.startswith(limited)


def test_edits_after_refresh_do_not_change_the_pinned_generation(tmp_path):
    root = tmp_path / "workspace"
    path = write_agent(root, "demo", body="one")
    mgr = manager(tmp_path)
    pinned = mgr.require("demo")
    assert pinned.load_body() == "one"

    path.write_text(
        "---\nname: demo\ndescription: An agent\n---\ntwo", encoding="utf-8"
    )
    assert pinned.load_body() == "one"
    assert mgr.load_body("demo") == "one"
    with pytest.raises(AgentStaleError):
        pinned.verify_snapshot()

    mgr.refresh()
    assert mgr.load_body("demo") == "two"


def test_oversize_file_is_refused(tmp_path):
    root = tmp_path / "workspace"
    write_agent(root, "big", body="x" * 200)
    mgr = manager(tmp_path, max_file_bytes=50)
    assert mgr.names == ()
    assert mgr.failures[0].code is AgentDiagnosticCode.OVERSIZE
    with pytest.raises(AgentOversizeError):
        read_agent_file(root / "big.md", max_file_bytes=50)


def test_oversized_body_is_refused_at_load(tmp_path):
    root = tmp_path / "workspace"
    write_agent(root, "demo", body="x" * 100)
    mgr = manager(tmp_path)
    with pytest.raises(AgentError):
        mgr.load_body("demo", max_bytes=10)


def test_body_with_nul_byte_is_refused(tmp_path):
    root = tmp_path / "workspace"
    path = write_agent(root, "demo", body="a\x00b")
    mgr = manager(tmp_path)
    assert path.is_file()
    with pytest.raises(AgentParseError, match="NUL"):
        mgr.load_body("demo")


# ---------------------------------------------------------------------------
# Security: declarations are not grants; read-only roles are structural
# ---------------------------------------------------------------------------


def test_declarations_never_grant_beyond_the_ceiling(tmp_path):
    write_agent(
        tmp_path / "workspace", "demo", bundles=["shell"], tools=["Bash", "Read"]
    )
    mgr = manager(
        tmp_path,
        known_tools={"Bash", "Read"},
        known_bundles=seed_bundle_map() | {"shell": ["Bash"]},
    )
    # The parent only offers Read, so Bash can never be granted.
    selection = mgr.select_tools(
        "demo",
        available={"Read"},
        bundle_map=seed_bundle_map() | {"shell": ["Bash"]},
    )
    assert selection.selected == frozenset({"read"})
    assert "bash" in selection.dropped
    assert selection.selected <= selection.ceiling <= selection.available


def test_empty_tools_list_inherits_only_the_parent_profile_ceiling(tmp_path):
    write_agent(tmp_path / "workspace", "demo", tools=[])
    mgr = manager(tmp_path)
    selection = mgr.select_tools(
        "demo",
        available={"Read", "Write", "Bash"},
        profile={"Read", "Write"},
    )
    assert selection.requested == frozenset({"read", "write"})
    assert selection.selected == frozenset({"read", "write"})
    assert selection.selected <= selection.ceiling <= selection.available


def test_unknown_declarations_are_diagnosed_but_retained(tmp_path):
    write_agent(
        tmp_path / "workspace",
        "demo",
        tools=["Read", "Nope"],
        bundles=["fs", "ghost"],
    )
    mgr = manager(
        tmp_path,
        known_tools={"Read", "Bash"},
        known_bundles={"fs": ["Read", "Write"], "shell": ["Bash"]},
    )
    assert mgr.names == ("demo",)
    codes = {d.code for d in mgr.diagnostics}
    assert AgentDiagnosticCode.UNKNOWN_TOOL in codes
    assert AgentDiagnosticCode.UNKNOWN_BUNDLE in codes
    agent = mgr.require("demo")
    assert agent.tools == ("read", "Nope")
    assert agent.bundles == ("fs", "ghost")


def test_read_only_roles_strip_shell_and_mutating_tools(tmp_path):
    # A tampered definition that asks for everything still gets nothing forbidden.
    write_agent(
        tmp_path / "workspace",
        "explore",
        bundles=["shell"],
        tools=["Bash", "Write", "Read"],
    )
    mgr = manager(
        tmp_path,
        known_tools={"Bash", "Write", "Read", "Grep"},
        known_bundles={"shell": ["Bash"], "fs": ["Read", "Write", "Grep"]},
    )
    assert mgr.require("explore").read_only
    codes = {d.code for d in mgr.security}
    assert AgentDiagnosticCode.FORBIDDEN_TOOL in codes
    assert AgentDiagnosticCode.FORBIDDEN_BUNDLE in codes

    selection = mgr.select_tools(
        "explore",
        available={"Bash", "Write", "Read", "Grep", "Edit", "MultiEdit"},
        bundle_map={"shell": ["Bash"], "fs": ["Read", "Write", "Grep"]},
    )
    assert selection.selected == frozenset({"read"})
    assert selection.stripped  # the structural strip did real work
    assert not (selection.selected & FORBIDDEN_ROLE_TOOLS)
    assert not (selection.selected & MUTATING_FS_TOOLS)


def test_read_only_multiedit_legacy_declaration_is_diagnosed(tmp_path):
    write_agent(tmp_path / "workspace", "explore", tools=["MultiEdit"])
    mgr = manager(tmp_path)
    assert any(
        diagnostic.code is AgentDiagnosticCode.FORBIDDEN_TOOL
        and "multiedit" in diagnostic.message
        for diagnostic in mgr.security
    )
    selection = mgr.select_tools(
        "explore", available={"multiedit"}, mutating=set()
    )
    assert selection.selected == frozenset()


def test_read_only_role_inherits_the_ceiling_without_mutating(tmp_path):
    # No declaration at all: an ordinary agent inherits everything, but a
    # read-only role inherits everything *except* the write path.
    mgr = manager(tmp_path, known_bundles=seed_bundle_map())
    ordinary = AgentDef(
        name="worker",
        description="d",
        provenance=_provenance(tmp_path, "worker"),
    )
    explore = AgentDef(
        name="explore",
        description="d",
        provenance=_provenance(tmp_path, "explore"),
    )
    available = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "Bash"}
    worker = mgr.select_tools(ordinary, available=available)
    scout = mgr.select_tools(
        explore, available=available, mutating={"SomeDynamicWrite"}
    )
    assert worker.selected == frozenset(
        {"read", "write", "edit", "multiedit", "glob", "grep", "ls", "bash"}
    )
    assert not (scout.selected & FORBIDDEN_ROLE_TOOLS)
    assert "SomeDynamicWrite" not in scout.selected
    assert scout.selected == frozenset({"read", "glob", "grep"})


def test_exclusions_can_only_remove_from_the_ceiling(tmp_path):
    write_agent(tmp_path / "workspace", "demo", tools=["-Write"])
    mgr = manager(tmp_path)
    selection = mgr.select_tools(
        "demo", available={"Read", "Write", "Glob"}, mutating=set()
    )
    assert selection.selected == frozenset({"read", "glob"})
    assert "write" in selection.stripped or "write" in selection.dropped


def test_declarations_are_not_grants_for_read_only_names(tmp_path):
    # The read-only contract follows the *name*, so a workspace shadow cannot
    # escape it: a file at the workspace path still cannot hold Bash.
    mgr = AgentManager(roots=[(AgentSource.WORKSPACE, tmp_path / "workspace")])
    write_agent(
        tmp_path / "workspace",
        "planner",
        tools=["Bash", "Read"],
        bundles=["shell"],
    )
    mgr.refresh()
    selection = mgr.select_tools(
        "planner",
        available={"Bash", "Read"},
        bundle_map={"shell": ["Bash"]},
    )
    assert selection.selected == frozenset({"read"})
    assert selection.read_only


def _provenance(tmp_path: Path, name: str):
    from nexus.agents import AgentProvenance

    root = tmp_path / "workspace"
    return AgentProvenance(
        tier=AgentSource.WORKSPACE,
        root=root,
        path=root / f"{name}.md",
        relpath=f"{name}.md",
        declaration_sha256="0" * 64,
        file_size=0,
    )


def test_read_only_roles_constant_covers_shell_and_mutating_fs():
    assert {"bash", "BashOutput", "KillShell"} <= FORBIDDEN_ROLE_TOOLS
    assert {"write", "edit", "multiedit"} <= FORBIDDEN_ROLE_TOOLS
    assert READ_ONLY_ROLES == frozenset({"advisor", "explore", "plan", "planner"})


def test_seed_error_is_an_agent_error(tmp_path, monkeypatch):
    import nexus.agents.manager as manager_module

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(manager_module, "_atomic_write", boom)
    with pytest.raises(AgentSeedError):
        seed_workspace_roles(tmp_path / "ws")


def test_builtin_build_prompt_names_no_optional_tools_or_roles() -> None:
    """Tools and subagent roles are discovered at runtime, not hardcoded.

    The tool list and the ``subagent`` roster carry names and usage; the build
    prompt may assume only read/search/edit, so it never names a tool or role.
    """
    body = (_default_seed_source() / "build.md").read_text()
    for name in (
        "bash", "todowrite", "question", "apply_patch", "webfetch", "websearch",
        "skill", "subagent", "subagent_type", "advisor", "task", "quick",
    ):
        assert f"`{name}`" not in body and f"**{name}**" not in body, name

@pytest.mark.parametrize("role", ["build", "orchestrator"])
def test_packaged_root_prompts_open_with_identity(role):
    """Root prompts open with the Nexus identity line and stay short."""
    body = (_default_seed_source() / f"{role}.md").read_text().split("---", 2)[2].strip()
    identity = "You are an expert coding assistant operating inside Nexus, a coding agent harness. You have tools."
    assert body.startswith(identity)
    if role == "orchestrator":
        assert "You never write code yourself." in body
    assert len(body) < 2400
