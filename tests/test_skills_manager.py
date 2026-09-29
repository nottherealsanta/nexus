"""Tests for :class:`nexus.skills.manager.SkillManager`.

Covers discovery roots and precedence, shadow/failure diagnostics, case-fold
safety, deletion fallback, the 100-skill progressive-disclosure guarantee,
deterministic index/hashes, bundled-tool candidates (never imported), and the
fact that declarations are not grants.
"""
from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nexus.skills import (
    SKILL_BEGIN_DELIMITER,
    SKILL_END_DELIMITER,
    SkillActivation,
    SkillActivationError,
    SkillDiagnosticCode,
    SkillError,
    SkillManager,
    SkillNotFoundError,
    SkillSource,
    SkillStaleError,
    render_invocation,
)


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "A skill",
    body: str = "BODY",
    allowed_tools: list[str] | None = None,
    bundles: list[str] | None = None,
    model: str | None = None,
    version: str | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", f"description: {description}"]
    if allowed_tools is not None:
        lines.append("allowed-tools: [" + ", ".join(allowed_tools) + "]")
    if bundles is not None:
        lines.append("bundles: [" + ", ".join(bundles) + "]")
    if model is not None:
        lines.append(f"model: {model}")
    if version is not None:
        lines.append(f"version: {version}")
    (directory / "SKILL.md").write_text(
        "---\n" + "\n".join(lines) + "\n---\n" + body, encoding="utf-8"
    )
    return directory


def manager(tmp_path: Path, **kwargs) -> SkillManager:
    return SkillManager(
        roots=[
            (SkillSource.BUILTIN, tmp_path / "builtin"),
            (SkillSource.USER, tmp_path / "user"),
            (SkillSource.WORKSPACE, tmp_path / "workspace"),
        ],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Discovery basics
# ---------------------------------------------------------------------------


def test_discovers_skills_across_roots(tmp_path):
    write_skill(tmp_path / "builtin", "alpha")
    write_skill(tmp_path / "user", "beta")
    write_skill(tmp_path / "workspace", "gamma")
    mgr = manager(tmp_path)
    assert mgr.names == ("alpha", "beta", "gamma")
    assert len(mgr) == 3
    assert "alpha" in mgr


def test_missing_roots_are_skipped(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.names == ()
    assert mgr.diagnostics == ()


def test_lookup_is_case_insensitive(tmp_path):
    write_skill(tmp_path / "workspace", "MySkill")
    mgr = manager(tmp_path)
    assert mgr.get("myskill") is mgr.get("MYSKILL")
    assert mgr.require("MySkill").name == "MySkill"
    with pytest.raises(SkillNotFoundError):
        mgr.require("absent")


def test_roots_are_normalized_and_sorted_by_precedence(tmp_path):
    mgr = SkillManager(
        roots=[
            (SkillSource.WORKSPACE, tmp_path / "w"),
            (SkillSource.BUILTIN, tmp_path / "b"),
            (SkillSource.USER, tmp_path / "u"),
            (SkillSource.WORKSPACE, tmp_path / "w"),  # duplicate dropped
        ]
    )
    assert [tier for tier, _path in mgr.roots] == [
        SkillSource.BUILTIN,
        SkillSource.USER,
        SkillSource.WORKSPACE,
    ]
    with pytest.raises(SkillError):
        SkillManager(roots=[("bogus", tmp_path)])


# ---------------------------------------------------------------------------
# Precedence, shadowing, deletion fallback
# ---------------------------------------------------------------------------


def test_workspace_shadows_user_shadows_builtin(tmp_path):
    write_skill(tmp_path / "builtin", "demo", description="builtin")
    write_skill(tmp_path / "user", "demo", description="user")
    write_skill(tmp_path / "workspace", "demo", description="workspace")
    mgr = manager(tmp_path)
    assert len(mgr) == 1
    assert mgr.require("demo").description == "workspace"
    assert mgr.require("demo").source is SkillSource.WORKSPACE
    assert len(mgr.shadowed) == 2
    assert {d.tier for d in mgr.shadowed} == {
        SkillSource.BUILTIN,
        SkillSource.USER,
    }
    assert all(d.code is SkillDiagnosticCode.SHADOWED for d in mgr.shadowed)


def test_deletion_falls_back_to_lower_tier(tmp_path):
    user_dir = write_skill(tmp_path / "user", "demo", description="user")
    write_skill(tmp_path / "workspace", "demo", description="workspace")
    mgr = manager(tmp_path)
    assert mgr.require("demo").source is SkillSource.WORKSPACE

    (tmp_path / "workspace" / "demo" / "SKILL.md").unlink()
    (tmp_path / "workspace" / "demo").rmdir()
    mgr.refresh()
    assert mgr.require("demo").source is SkillSource.USER
    assert mgr.require("demo").description == "user"
    assert user_dir.is_dir()


def test_case_collision_within_one_tier_is_deterministic(tmp_path):
    # Two same-tier roots: the case-insensitive filesystem cannot host both
    # variants in one directory, but two roots exercise the collision directly.
    write_skill(tmp_path / "ws-a", "Foo", description="upper")
    write_skill(tmp_path / "ws-b", "foo", description="lower")
    roots = [
        (SkillSource.WORKSPACE, tmp_path / "ws-a"),
        (SkillSource.WORKSPACE, tmp_path / "ws-b"),
    ]
    first = SkillManager(roots=roots)
    second = SkillManager(roots=roots)
    assert len(first) == 1
    assert first.names == second.names
    assert first.get("foo") is first.get("FOO")
    assert any(
        d.code is SkillDiagnosticCode.CASE_COLLISION for d in first.diagnostics
    )


# ---------------------------------------------------------------------------
# Progressive disclosure: 100 skills
# ---------------------------------------------------------------------------


def test_one_hundred_skills_index_is_sanitized_and_bodies_stay_lazy(tmp_path):
    root = tmp_path / "workspace"
    for index in range(100):
        write_skill(
            root,
            f"skill-{index:03d}",
            description=f"description number {index}",
            body=f"SECRET-BODY-{index}",
        )
    mgr = manager(tmp_path)
    assert len(mgr) == 100

    rendered = mgr.render_index()
    assert len(rendered.splitlines()) == 100
    assert "SECRET-BODY" not in rendered
    assert rendered.splitlines()[0] == "skill-000: description number 0"

    # Bodies are still reachable, on demand only.
    assert mgr.load_body("skill-042") == "SECRET-BODY-42"


def test_render_index_respects_a_character_budget(tmp_path):
    root = tmp_path / "workspace"
    for index in range(5):
        write_skill(root, f"s{index}", description="x" * 20)
    mgr = manager(tmp_path)
    full = mgr.render_index()
    limited = mgr.render_index(max_chars=30)
    assert 0 < len(limited) <= 30
    assert full.startswith(limited)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_index_and_hashes_are_deterministic(tmp_path):
    root = tmp_path / "workspace"
    write_skill(root, "alpha", description="one", body="A")
    write_skill(root, "beta", description="two", body="B")
    first = manager(tmp_path)
    second = manager(tmp_path)
    assert first.render_index() == second.render_index()
    assert [s.declaration_sha256 for s in first] == [
        s.declaration_sha256 for s in second
    ]
    assert first.names == second.names
    assert first.diagnostics == second.diagnostics


def test_declaration_hash_ignores_body_but_tracks_frontmatter(tmp_path):
    root = tmp_path / "workspace"
    directory = write_skill(root, "demo", description="d", body="one")
    first = manager(tmp_path).require("demo").declaration_sha256
    (directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\ntwo", encoding="utf-8"
    )
    assert manager(tmp_path).require("demo").declaration_sha256 == first

    (directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: changed\n---\ntwo", encoding="utf-8"
    )
    assert manager(tmp_path).require("demo").declaration_sha256 != first


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_failed_skill_is_retained_as_a_diagnostic(tmp_path):
    root = tmp_path / "workspace"
    bad = root / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("---\nname: broken\n---\n", encoding="utf-8")
    write_skill(root, "good")
    mgr = manager(tmp_path)
    assert mgr.names == ("good",)
    assert len(mgr.failures) == 1
    assert mgr.failures[0].code is SkillDiagnosticCode.PARSE_ERROR


def test_oversize_skill_file_is_reported(tmp_path):
    root = tmp_path / "workspace"
    directory = root / "big"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: big\ndescription: d\n---\n" + "x" * 100, encoding="utf-8"
    )
    mgr = manager(tmp_path, max_file_bytes=50)
    assert mgr.names == ()
    assert mgr.failures[0].code is SkillDiagnosticCode.OVERSIZE


# ---------------------------------------------------------------------------
# Declarations are not grants
# ---------------------------------------------------------------------------


def test_unknown_declarations_are_diagnosed_but_do_not_remove_the_skill(tmp_path):
    write_skill(
        tmp_path / "workspace",
        "demo",
        allowed_tools=["Read", "Nope"],
        bundles=["fs", "ghost"],
    )
    mgr = manager(
        tmp_path,
        known_tools={"Read", "Bash"},
        known_bundles={"fs": ["Read", "Write"], "shell": ["Bash"]},
    )
    assert mgr.names == ("demo",)
    codes = {d.code for d in mgr.diagnostics}
    assert SkillDiagnosticCode.UNKNOWN_ALLOWED_TOOL in codes
    assert SkillDiagnosticCode.UNKNOWN_BUNDLE in codes
    # The declaration is retained verbatim; nothing was expanded.
    skill = mgr.require("demo")
    assert skill.allowed_tools == ("read", "Nope")
    assert skill.bundles == ("fs", "ghost")


def test_scoped_tool_descriptors_are_a_view_not_a_grant(tmp_path):
    write_skill(
        tmp_path / "workspace",
        "demo",
        allowed_tools=["Read", "Nope"],
        bundles=["fs"],
    )
    catalog = {"Read": object(), "Write": object(), "Bash": object()}
    mgr = manager(
        tmp_path,
        known_tools=set(catalog),
        known_bundles={"fs": ["Read", "Write"]},
    )
    descriptors = mgr.scoped_tool_descriptors("demo", catalog)
    assert descriptors == (catalog["Read"], catalog["Write"])
    # The manager holds no tool registry; the catalog is untouched.
    assert mgr.scoped_tool_descriptors("demo", {}) == ()


# ---------------------------------------------------------------------------
# Bundled tools are candidates only
# ---------------------------------------------------------------------------


def test_bundled_tools_are_candidates_and_never_imported(tmp_path):
    root = tmp_path / "workspace"
    directory = write_skill(root, "demo")
    tools = directory / "tools"
    tools.mkdir()
    (tools / "boom.py").write_text(
        "raise RuntimeError('this module must never be imported')\n",
        encoding="utf-8",
    )
    (tools / "_helper.py").write_text("x = 1\n", encoding="utf-8")
    (tools / "__init__.py").write_text("", encoding="utf-8")

    sys.modules.pop("boom", None)
    mgr = manager(tmp_path)
    assert mgr.names == ("demo",)
    assert "boom" not in sys.modules

    candidates = mgr.tool_candidates("demo")
    assert [c.module for c in candidates] == ["boom"]
    candidate = candidates[0]
    assert candidate.path == "tools/boom.py"
    assert candidate.size > 0
    assert candidate.sha256


def test_for_workspace_builds_builtin_user_workspace_roots(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    write_skill(home / ".nexus" / "skills", "user-skill")
    write_skill(workspace / ".agents" / "skills", "ws-skill")
    builtin = tmp_path / "builtin"
    write_skill(builtin, "builtin-skill")
    mgr = SkillManager.for_workspace(workspace, home=home, builtin=builtin)
    assert mgr.names == ("builtin-skill", "user-skill", "ws-skill")
    assert [tier for tier, _path in mgr.roots] == [
        SkillSource.BUILTIN,
        SkillSource.USER,
        SkillSource.WORKSPACE_LEGACY,
        SkillSource.WORKSPACE,
    ]


def test_for_workspace_legacy_nexus_skills_are_a_read_only_fallback(tmp_path):
    """STATE_PLAN §5.4: ``.nexus/skills`` is still read, but ``.agents`` wins."""
    workspace = tmp_path / "ws"
    write_skill(workspace / ".nexus" / "skills", "legacy-only", description="legacy")
    write_skill(workspace / ".nexus" / "skills", "shared", description="legacy")
    write_skill(workspace / ".agents" / "skills", "shared", description="agents")
    mgr = SkillManager.for_workspace(workspace, builtin=tmp_path / "no-builtin")
    assert mgr.require("legacy-only").source is SkillSource.WORKSPACE_LEGACY
    assert mgr.require("shared").source is SkillSource.WORKSPACE
    assert mgr.require("shared").description == "agents"


# ---------------------------------------------------------------------------
# Generation-stable content snapshots
# ---------------------------------------------------------------------------


def _rewrite(directory: Path, name: str, body: str, description: str = "A skill") -> None:
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


def test_edits_after_refresh_do_not_change_the_pinned_generation(tmp_path):
    root = tmp_path / "workspace"
    directory = write_skill(root, "demo", body="one")
    scripts = directory / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("echo one\n", encoding="utf-8")

    mgr = manager(tmp_path)
    pinned = mgr.require("demo")
    assert pinned.load_body() == "one"
    assert mgr.read_resource("demo", "scripts/run.sh") == "echo one\n"

    # Edit both artifacts after discovery.
    _rewrite(directory, "demo", "two")
    (scripts / "run.sh").write_text("echo two\n", encoding="utf-8")

    # The pinned generation and the live manager both keep the old snapshot.
    assert pinned.load_body() == "one"
    assert mgr.load_body("demo") == "one"
    assert mgr.read_resource("demo", "scripts/run.sh") == "echo one\n"
    with pytest.raises(SkillStaleError):
        pinned.verify_snapshot()  # on-disk bytes no longer match the snapshot

    # A refresh sees the new bytes.
    mgr.refresh()
    assert mgr.load_body("demo") == "two"
    assert mgr.read_resource("demo", "scripts/run.sh") == "echo two\n"


def test_verify_snapshot_refuses_after_an_edit(tmp_path):
    root = tmp_path / "workspace"
    directory = write_skill(root, "demo", body="one")
    mgr = manager(tmp_path)
    pinned = mgr.require("demo")
    pinned.verify_snapshot()  # unchanged: no error

    _rewrite(directory, "demo", "two")
    with pytest.raises(SkillStaleError):
        pinned.verify_snapshot()


def test_oversized_resource_is_refused_by_the_snapshot(tmp_path):
    root = tmp_path / "workspace"
    directory = write_skill(root, "demo")
    scripts = directory / "scripts"
    scripts.mkdir()
    (scripts / "big.sh").write_bytes(b"x" * 100)
    mgr = manager(tmp_path, max_inventory_bytes=10)
    with pytest.raises(SkillError):
        mgr.read_resource("demo", "scripts/big.sh")


# ---------------------------------------------------------------------------
# Object reuse and stable fingerprints
# ---------------------------------------------------------------------------


def test_refresh_reuses_unchanged_objects_and_rebuilds_changed_ones(tmp_path):
    root = tmp_path / "workspace"
    directory = write_skill(root, "demo", body="one")
    mgr = manager(tmp_path)
    first = mgr.require("demo")
    first_fingerprint = first.fingerprint()

    mgr.refresh()
    assert mgr.require("demo") is first  # identity reuse, no diff churn
    assert mgr.require("demo").fingerprint() == first_fingerprint

    _rewrite(directory, "demo", "two")
    mgr.refresh()
    assert mgr.require("demo") is not first
    assert mgr.require("demo").fingerprint() != first_fingerprint


def test_identical_skill_in_a_new_tier_is_not_reused(tmp_path):
    # Byte-identical content in two tiers still differs in provenance. The
    # unchanged-object reuse must compare tier/directory too, or a skill
    # revealed from a lower tier would keep the shadowing tier's provenance.
    write_skill(tmp_path / "user", "demo", description="same")
    write_skill(tmp_path / "workspace", "demo", description="same")
    mgr = manager(tmp_path)
    workspace_skill = mgr.require("demo")
    assert workspace_skill.source is SkillSource.WORKSPACE

    (tmp_path / "workspace" / "demo" / "SKILL.md").unlink()
    (tmp_path / "workspace" / "demo").rmdir()
    mgr.refresh()
    user_skill = mgr.require("demo")

    assert user_skill.source is SkillSource.USER
    assert user_skill is not workspace_skill
    assert user_skill.directory == tmp_path / "user" / "demo"


def test_manager_fingerprints_map_and_index_fingerprint_are_stable(tmp_path):
    root = tmp_path / "workspace"
    write_skill(root, "alpha", body="A")
    write_skill(root, "beta", body="B")
    first = manager(tmp_path)
    second = manager(tmp_path)
    assert first.fingerprints == second.fingerprints
    assert set(first.fingerprints) == {"alpha", "beta"}
    assert first.fingerprint() == second.fingerprint()
    assert all(
        value == first.require(name).fingerprint()
        for name, value in first.fingerprints.items()
    )
    # The manifest diff key callable delegates to the skill's own fingerprint.
    assert (
        SkillManager.fingerprint_key(first.require("alpha"))
        == first.require("alpha").fingerprint()
    )


def test_whole_skill_fingerprint_changes_on_resource_edit(tmp_path):
    root = tmp_path / "workspace"
    directory = write_skill(root, "demo")
    scripts = directory / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("one\n", encoding="utf-8")
    mgr = manager(tmp_path)
    before = mgr.require("demo").fingerprint()

    (scripts / "run.sh").write_text("two\n", encoding="utf-8")
    mgr.refresh()
    assert mgr.require("demo").fingerprint() != before


# ---------------------------------------------------------------------------
# SkillActivation: an intersection that never expands authority
# ---------------------------------------------------------------------------


def test_activation_intersects_available_profile_and_declarations():
    activation = SkillActivation.intersect(
        skill="demo",
        available={"Read", "Write", "Bash", "Glob", "Secret"},
        profile={"Read", "Write", "Bash"},
        allowed_tools=["Read", "Secret"],
        bundles=["fs"],
        bundle_tools={"fs": ["Read", "Write", "Glob"]},
        generation=3,
        session="s1",
        turn=2,
    )
    assert activation.authority == frozenset({"Read", "Write", "Bash"})
    assert activation.active == frozenset({"Read", "Write"})
    # Declared but outside authority: not in the manifest (Secret) or not in the
    # profile (Glob). Neither can ever be granted.
    assert activation.unavailable == frozenset({"Secret", "Glob"})
    assert activation.narrowed is True
    assert activation.permits("Read")
    assert "Write" in activation
    assert not activation.permits("Secret")
    assert not activation.permits("Bash")  # authority, but not declared
    assert activation.generation == 3
    assert activation.session == "s1"
    assert activation.turn == 2


def test_activation_declaring_nothing_does_not_narrow():
    activation = SkillActivation.intersect(
        skill="demo",
        available={"Read", "Bash"},
        profile={"Read", "Bash"},
    )
    assert activation.active == frozenset({"Read", "Bash"})
    assert activation.narrowed is False


def test_activation_unknown_bundle_fails_closed():
    activation = SkillActivation.intersect(
        skill="demo",
        available={"Read"},
        profile={"Read"},
        bundles=["ghost"],
    )
    assert activation.active == frozenset()
    assert activation.unknown_bundles == ("ghost",)
    assert activation.narrowed is True


def test_activation_can_never_expand_authority():
    with pytest.raises(SkillActivationError):
        SkillActivation(
            skill="demo",
            active=frozenset({"Bash"}),
            available=frozenset({"Read"}),
            profile=frozenset({"Bash"}),
        )
    # Even with a permissive profile, a tool the manifest does not expose is denied.
    activation = SkillActivation.intersect(
        skill="demo",
        available={"Read"},
        profile={"Read", "Bash"},
        allowed_tools=["Bash"],
    )
    assert activation.active == frozenset()
    assert activation.unavailable == frozenset({"Bash"})


def test_activation_is_immutable_and_isolated_across_instances():
    available = {"Read", "Write"}
    profile = {"Read"}
    allowed = ["Read"]
    activation = SkillActivation.intersect(
        skill="demo",
        available=available,
        profile=profile,
        allowed_tools=allowed,
    )
    # Mutating the caller's collections cannot change a built activation.
    available.add("Bash")
    profile.add("Bash")
    allowed.append("Bash")
    assert activation.active == frozenset({"Read"})
    assert activation.available == frozenset({"Read", "Write"})
    assert activation.profile == frozenset({"Read"})

    other = SkillActivation.intersect(
        skill="other", available={"Bash"}, profile={"Bash"}, allowed_tools=["Bash"]
    )
    assert activation.active == frozenset({"Read"})
    assert other.active == frozenset({"Bash"})
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        activation.active = frozenset()  # type: ignore[misc]


def test_activation_for_skill_uses_the_declaration(tmp_path):
    write_skill(
        tmp_path / "workspace",
        "demo",
        allowed_tools=["Read", "Nope"],
        bundles=["fs"],
    )
    mgr = manager(
        tmp_path,
        known_tools={"Read", "Write", "Bash"},
        known_bundles={"fs": ["Read", "Write"]},
    )
    activation = SkillActivation.for_skill(
        mgr.require("demo"),
        available={"Read", "Write", "Bash"},
        profile={"Read", "Write"},
        bundle_tools={"fs": ["Read", "Write"]},
    )
    assert activation.active == frozenset({"Read", "Write"})
    assert activation.unavailable == frozenset({"Nope", "read"})


# ---------------------------------------------------------------------------
# Bounded, delimited invocation output
# ---------------------------------------------------------------------------


def test_manager_invocation_output_is_delimited_bounded_with_provenance(tmp_path):
    root = tmp_path / "workspace"
    write_skill(root, "demo", body="SECRET-BODY", description="demo skill")
    mgr = manager(tmp_path)
    invocation = render_invocation(mgr.require("demo"), generation=mgr.generation)

    assert invocation.text.startswith(SKILL_BEGIN_DELIMITER)
    assert invocation.text.endswith(SKILL_END_DELIMITER)
    assert "SECRET-BODY" in invocation.text
    assert 'name="demo"' in invocation.text
    assert 'source="workspace"' in invocation.text
    assert invocation.body_sha256 == mgr.require("demo").body_sha256
    assert invocation.fingerprint == mgr.require("demo").fingerprint()
