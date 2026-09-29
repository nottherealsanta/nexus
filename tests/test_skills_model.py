"""Contract tests for :mod:`nexus.skills.model` and the frontmatter parser.

Covers the restricted grammar (including an adversarial table), version and
description normalization, immutable metadata, progressive-disclosure body
loading, and the layer boundary (skills never import a higher layer).
"""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nexus.skills import (
    MODEL_TIERS,
    SKILL_BEGIN_DELIMITER,
    SKILL_END_DELIMITER,
    BundledToolCandidate,
    Skill,
    SkillDiagnosticCode,
    SkillIndex,
    SkillIndexEntry,
    SkillOversizeError,
    SkillParseError,
    SkillProvenance,
    SkillResource,
    SkillSource,
    is_model_tier,
    parse_frontmatter,
    render_invocation,
    sanitize_description,
    sha256_hex,
    split_frontmatter,
    validate_skill_name,
)
from nexus.skills.frontmatter import MAX_DESCRIPTION_CHARS, MAX_FRONTMATTER_BYTES
from nexus.skills.model import SOURCE_PRECEDENCE

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "nexus" / "skills"

#: Packages the skills layer must never reach for.
FORBIDDEN_PREFIXES = (
    "nexus.core",
    "nexus.session",
    "nexus.context",
    "nexus.runtime",
    "nexus.tools",
    "nexus.ext",
    "nexus.agent",
    "nexus.cli",
)


def doc(frontmatter: str, body: str = "") -> str:
    return f"---\n{frontmatter}\n---\n{body}"


VALID_MINIMAL = "name: demo\ndescription: A demo skill"
VALID_FULL = (
    "name: risk3-docker-testing\n"
    "description: Run DS-pack tests locally. Use when..."
)


# ---------------------------------------------------------------------------
# Valid grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frontmatter, expected",
    [
        (
            VALID_MINIMAL,
            {
                "name": "demo",
                "description": "A demo skill",
                "allowed_tools": (),
                "bundles": (),
                "model": None,
                "version": "1",
            },
        ),
        (
            (
                "name: demo\ndescription: d\nallowed-tools: [Bash, Read, Glob]\n"
                "bundles: [fs, shell]\nmodel: inherit\nversion: 1"
            ),
            {
                "name": "demo",
                "description": "d",
                    "allowed_tools": ("bash", "read", "glob"),
                "bundles": ("fs", "shell"),
                "model": "inherit",
                "version": "1",
            },
        ),
        (
            "name: demo\ndescription: d\nallowed-tools: []\nbundles: []",
            {
                "name": "demo",
                "description": "d",
                "allowed_tools": (),
                "bundles": (),
                "model": None,
                "version": "1",
            },
        ),
        (
            "name: demo\ndescription: has: a colon, and punctuation!\nversion: v2",
            {
                "name": "demo",
                "description": "has: a colon, and punctuation!",
                "allowed_tools": (),
                "bundles": (),
                "model": None,
                "version": "v2",
            },
        ),
        (
            "name: Demo.Skill_2\ndescription: d\nmodel: anthropic/claude-opus-5",
            {
                "name": "Demo.Skill_2",
                "description": "d",
                "allowed_tools": (),
                "bundles": (),
                "model": "anthropic/claude-opus-5",
                "version": "1",
            },
        ),
        (
            "name: demo\ndescription: d\nversion: 01",
            {
                "name": "demo",
                "description": "d",
                "allowed_tools": (),
                "bundles": (),
                "model": None,
                "version": "1",
            },
        ),
    ],
)
def test_valid_declarations_parse(frontmatter, expected):
    parsed = parse_frontmatter(doc(frontmatter, "# body"))
    for key, value in expected.items():
        assert getattr(parsed, key) == value


def test_crlf_and_bom_are_accepted():
    text = "\ufeff---\r\nname: demo\r\ndescription: d\r\n---\r\nbody\r\n"
    parsed = parse_frontmatter(text)
    assert parsed.name == "demo"
    assert parsed.description == "d"


def test_blank_lines_in_frontmatter_are_ignored():
    parsed = parse_frontmatter("---\nname: demo\n\ndescription: d\n\n---\n")
    assert parsed.name == "demo"


def test_duplicate_list_items_are_deduplicated_in_order():
    parsed = parse_frontmatter(
        "---\nname: demo\ndescription: d\nallowed-tools: [Bash, Read, Bash]\n---\n"
    )
    assert parsed.allowed_tools == ("bash", "read")


def test_split_frontmatter_returns_body():
    parsed, body = split_frontmatter(doc(VALID_MINIMAL, "hello body"))
    assert parsed.name == "demo"
    assert body == b"hello body"


# ---------------------------------------------------------------------------
# Adversarial table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty file
        "no frontmatter at all",
        "name: demo\ndescription: d",  # no delimiters
        "---\nname: demo\ndescription: d",  # no closing delimiter
        "----\nname: demo\ndescription: d\n---",  # malformed opening
        "\n---\nname: demo\ndescription: d\n---",  # opening not first line
        "---\nname: demo\ndescription: d\n----\n",  # malformed closing
        "---\n---\n",  # empty frontmatter, missing required
        "---\nname: demo\n---\n",  # missing description
        "---\ndescription: d\n---\n",  # missing name
        "---\nname:\ndescription: d\n---\n",  # empty name value
        "---\nname: demo\ndescription:\n---\n",  # empty description
        "---\nname: demo\ndescription: d\nunknown: x\n---\n",  # unknown field
        "---\nname: demo\nname: other\ndescription: d\n---\n",  # duplicate field
        "---\nName: demo\ndescription: d\n---\n",  # key case
        "---\nname: demo\ndescription: d\nallowed-tools: Bash\n---\n",  # not a list
        "---\nname: demo\ndescription: d\nbundles: 3\n---\n",  # wrong type
        "---\nname: demo\ndescription: d\nversion: [1]\n---\n",  # wrong type
        "---\nname: demo\ndescription: d\nversion: true\n---\n",  # typed scalar
        "---\nname: demo\ndescription: d\nversion: 1.5\n---\n",  # float
        "---\nname: demo\ndescription: d\nallowed-tools: [Bash,]\n---\n",  # empty item
        "---\nname: demo\ndescription: d\nallowed-tools: [Bash\n---\n",  # unclosed list
        "---\nname: demo\ndescription: d\nallowed-tools: Bash, Read\n---\n",  # not flow
        "---\nname: demo\ndescription: d\nallowed-tools: [1Bash]\n---\n",  # bad token
        "---\nname: demo\ndescription: d\nbundles: [FS]\n---\n",  # bad bundle token
        "---\nname: demo\ndescription: d\n  model: inherit\n---\n",  # indentation
        "---\nname: demo\ndescription: d\n- item\n---\n",  # block sequence
        "---\nname: demo\ndescription: d\n# comment\n---\n",  # comment
        "---\nname: demo\ndescription: {a: b}\n---\n",  # flow mapping
        "---\nname: &anchor demo\ndescription: d\n---\n",  # anchor
        "---\nname: *alias\ndescription: d\n---\n",  # alias
        "---\nname: !!str demo\ndescription: d\n---\n",  # tag
        "---\nname: demo\ndescription: |\n---\n",  # block scalar
        "---\nname: demo\ndescription: >\n---\n",  # folded scalar
        "---\nname: ../escape\ndescription: d\n---\n",  # traversal in name
        "---\nname: has space\ndescription: d\n---\n",  # space in name
        "---\nname: " + "x" * 65 + "\ndescription: d\n---\n",  # name too long
        "---\nname: demo\ndescription: " + "d" * (MAX_DESCRIPTION_CHARS + 1) + "\n---\n",
    ],
)
def test_invalid_declarations_are_rejected(text):
    with pytest.raises(SkillParseError):
        parse_frontmatter(text)


def test_nul_byte_in_frontmatter_is_rejected():
    with pytest.raises(SkillParseError):
        parse_frontmatter("---\nname: de\x00mo\ndescription: d\n---\n")


def test_oversize_frontmatter_is_rejected():
    big = "---\nname: demo\ndescription: d\n" + ("# pad\n" * MAX_FRONTMATTER_BYTES) + "---\n"
    with pytest.raises(SkillOversizeError):
        parse_frontmatter(big)


def test_validate_skill_name_accepts_and_rejects():
    assert validate_skill_name("a") == "a"
    assert validate_skill_name("risk3-docker.test_1") == "risk3-docker.test_1"
    for bad in ("", "-lead", "_lead", "a b", "a/b", "a" * 65, None, 5):
        with pytest.raises(SkillParseError):
            validate_skill_name(bad)


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_sanitize_description_collapses_whitespace_and_controls():
    assert sanitize_description("a\n\tb  c") == "a b c"
    assert sanitize_description("  padded  ") == "padded"
    assert sanitize_description("a\x00b") == "a b"


def test_sanitize_description_truncates_with_ellipsis():
    out = sanitize_description("x" * 100, max_chars=10)
    assert out.endswith("\u2026")
    assert len(out) <= 11


# ---------------------------------------------------------------------------
# Immutable metadata
# ---------------------------------------------------------------------------


def make_skill(**overrides) -> Skill:
    provenance = SkillProvenance(
        tier=SkillSource.WORKSPACE,
        root=Path("/root"),
        directory=Path("/root/demo"),
        skill_md=Path("/root/demo/SKILL.md"),
        relpath="demo/SKILL.md",
        declaration_sha256="abc",
        file_size=42,
    )
    payload = {
        "name": "demo",
        "description": "A demo",
        "provenance": provenance,
    }
    payload.update(overrides)
    return Skill(**payload)


def test_skill_is_frozen_and_exposes_provenance():
    skill = make_skill(allowed_tools=("Bash",), bundles=("shell",))
    assert skill.source is SkillSource.WORKSPACE
    assert skill.directory == Path("/root/demo")
    assert skill.declaration_sha256 == "abc"
    assert skill.index_line() == "demo: A demo"
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        skill.name = "other"  # type: ignore[misc]


def test_provenance_and_metadata_are_frozen():
    provenance = make_skill().provenance
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        provenance.tier = SkillSource.BUILTIN  # type: ignore[misc]
    resource = SkillResource(kind="script", path="scripts/a.sh", size=1)
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        resource.path = "b"  # type: ignore[misc]
    candidate = BundledToolCandidate(path="tools/x.py", module="x", size=1)
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        candidate.module = "y"  # type: ignore[misc]


def test_skill_source_precedence_and_values():
    assert SOURCE_PRECEDENCE[SkillSource.BUILTIN] < SOURCE_PRECEDENCE[SkillSource.USER]
    assert (
        SOURCE_PRECEDENCE[SkillSource.USER]
        < SOURCE_PRECEDENCE[SkillSource.WORKSPACE_LEGACY]
    )
    assert (
        SOURCE_PRECEDENCE[SkillSource.WORKSPACE_LEGACY]
        < SOURCE_PRECEDENCE[SkillSource.WORKSPACE]
    )
    assert [tier.value for tier in SkillSource] == [
        "builtin",
        "user",
        "workspace_legacy",
        "workspace",
    ]
    assert SkillDiagnosticCode.PARSE_ERROR.value == "parse_error"


def test_skill_structurally_satisfies_manifest_skill_protocol():
    skill = make_skill()
    assert isinstance(skill.name, str)
    assert isinstance(skill.description, str)


# ---------------------------------------------------------------------------
# Progressive disclosure: body loads only on demand
# ---------------------------------------------------------------------------


def test_body_is_not_loaded_until_requested(tmp_path):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_bytes(
        b"---\nname: demo\ndescription: A demo\n---\nSENTINEL-BODY\n"
    )
    from nexus.skills import read_declaration

    parsed = read_declaration(skill_md)
    assert parsed.name == "demo"
    assert b"SENTINEL-BODY" not in parsed.raw
    assert "SENTINEL-BODY" not in make_skill().index_line()


def test_invalid_utf8_body_does_not_break_discovery(tmp_path):
    from nexus.skills import read_declaration

    skill_md = tmp_path / "SKILL.md"
    skill_md.write_bytes(b"---\nname: demo\ndescription: d\n---\n\xff\xfe")
    parsed = read_declaration(skill_md)
    assert parsed.name == "demo"

    skill = make_skill(provenance=SkillProvenance(
        tier=SkillSource.WORKSPACE,
        root=tmp_path,
        directory=tmp_path,
        skill_md=skill_md,
        relpath="demo/SKILL.md",
        declaration_sha256="x",
        file_size=skill_md.stat().st_size,
    ))
    with pytest.raises(SkillParseError):
        skill.load_body()


def test_load_body_returns_body_and_rejects_nul(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_bytes(b"---\nname: demo\ndescription: d\n---\nhello body\n")
    provenance = SkillProvenance(
        tier=SkillSource.WORKSPACE,
        root=tmp_path,
        directory=tmp_path,
        skill_md=skill_md,
        relpath="demo/SKILL.md",
        declaration_sha256="x",
        file_size=skill_md.stat().st_size,
    )
    skill = make_skill(provenance=provenance)
    assert skill.load_body() == "hello body\n"

    skill_md.write_bytes(b"---\nname: demo\ndescription: d\n---\na\x00b")
    with pytest.raises(SkillParseError):
        skill.load_body()


def test_load_body_enforces_size_budget(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_bytes(b"---\nname: demo\ndescription: d\n---\n" + b"x" * 100)
    provenance = SkillProvenance(
        tier=SkillSource.WORKSPACE,
        root=tmp_path,
        directory=tmp_path,
        skill_md=skill_md,
        relpath="demo/SKILL.md",
        declaration_sha256="x",
        file_size=skill_md.stat().st_size,
    )
    skill = make_skill(provenance=provenance)
    with pytest.raises(SkillOversizeError):
        skill.load_body(max_bytes=10)


# ---------------------------------------------------------------------------
# Layer boundary
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            modules.add(node.module or "")
    return modules


@pytest.mark.parametrize(
    "path", sorted(SKILLS_ROOT.rglob("*.py")), ids=lambda p: p.name
)
def test_skills_layer_does_not_import_higher_layers(path):
    violations = sorted(
        module for module in _imports(path) if module.startswith(FORBIDDEN_PREFIXES)
    )
    assert not violations, f"{path.relative_to(REPO_ROOT)} imports {violations}"


# ---------------------------------------------------------------------------
# Opaque model field: tiers, provider ids, bare ids, inherit
# ---------------------------------------------------------------------------


def test_model_tiers_constant_is_low_medium_high():
    assert MODEL_TIERS == ("low", "medium", "high")


@pytest.mark.parametrize(
    "model",
    [
        "low",
        "medium",
        "high",
        "inherit",
        "anthropic/claude-opus-5",
        "openai/gpt-5.6",
        "claude-haiku-4-5",
    ],
)
def test_model_accepts_tiers_provider_and_bare_ids_verbatim(model):
    parsed = parse_frontmatter(doc(f"name: demo\ndescription: d\nmodel: {model}"))
    # Opaque: returned exactly as written, never resolved or rewritten.
    assert parsed.model == model
    assert is_model_tier(model) == (model in MODEL_TIERS)


def test_model_is_not_resolved_and_stays_opaque():
    skill = make_skill(model="anthropic/claude-opus-5")
    assert skill.model == "anthropic/claude-opus-5"
    assert skill.model_is_tier is False
    assert make_skill(model="high").model_is_tier is True
    assert make_skill(model=None).model_is_tier is False


# ---------------------------------------------------------------------------
# Whole-skill fingerprint
# ---------------------------------------------------------------------------


def test_whole_skill_fingerprint_is_stable_and_tracks_every_field():
    base = make_skill(body=b"one", body_sha256="a" * 64)
    same = make_skill(body=b"one", body_sha256="a" * 64)
    assert base.fingerprint() == same.fingerprint()
    assert base.fingerprint() != make_skill(body=b"two", body_sha256="b" * 64).fingerprint()
    assert base.fingerprint() != make_skill(model="high").fingerprint()
    assert base.fingerprint() != make_skill(description="different").fingerprint()
    assert base.fingerprint() != make_skill(
        resources=(SkillResource(kind="script", path="scripts/a.sh", size=1, sha256="c" * 64),)
    ).fingerprint()


def test_fingerprint_covers_bundled_tool_metadata():
    candidate = BundledToolCandidate(path="tools/x.py", module="x", size=3, sha256="d" * 64)
    with_tool = make_skill(tool_candidates=(candidate,))
    assert with_tool.fingerprint() != make_skill().fingerprint()


# ---------------------------------------------------------------------------
# Progressive disclosure with a body snapshot
# ---------------------------------------------------------------------------


def test_body_snapshot_is_in_memory_but_absent_from_the_idle_index():
    skill = make_skill(body=b"SECRET-BODY", body_sha256=sha256_hex(b"SECRET-BODY"))
    assert skill.has_body_snapshot is True
    assert skill.body == b"SECRET-BODY"
    assert skill.load_body() == "SECRET-BODY"

    index = SkillIndex(
        entries=(
            SkillIndexEntry(
                name=skill.name,
                description=skill.sanitized_description(),
                source=skill.source,
            ),
        )
    )
    rendered = index.render()
    assert "SECRET-BODY" not in rendered
    assert "demo: A demo" in rendered


# ---------------------------------------------------------------------------
# Bounded, delimited invocation output
# ---------------------------------------------------------------------------


def test_render_invocation_is_delimited_and_carries_provenance():
    body = "do the thing\n"
    skill = make_skill(
        body=body.encode("utf-8"),
        body_sha256=sha256_hex(body.encode("utf-8")),
        snapshotted=True,
        file_sha256="f" * 64,
    )
    invocation = render_invocation(skill, generation=7)
    assert invocation.text.startswith(SKILL_BEGIN_DELIMITER)
    assert invocation.text.endswith(SKILL_END_DELIMITER)
    assert body.strip() in invocation.text
    assert 'name="demo"' in invocation.text
    assert 'source="workspace"' in invocation.text
    assert 'generation="7"' in invocation.text
    assert invocation.fingerprint == skill.fingerprint()
    assert invocation.body_sha256 == sha256_hex(body.encode("utf-8"))
    assert invocation.truncated is False


def test_render_invocation_is_bounded_even_with_a_huge_body():
    skill = make_skill(body=b"x" * 4096, snapshotted=True)
    invocation = render_invocation(
        skill, max_body_bytes=64, max_output_bytes=256
    )
    assert invocation.truncated is True
    assert len(invocation.text.encode("utf-8")) <= 256
    assert invocation.text.startswith(SKILL_BEGIN_DELIMITER)
    assert invocation.text.endswith(SKILL_END_DELIMITER)
