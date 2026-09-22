"""Tests for :mod:`nexus.skills.resources`.

Covers the fail-closed resource resolver (absolute/traversal/NUL/symlink-escape/
device/directory rejection, byte and UTF-8 bounds) and the deterministic
inventory and tool-candidate builders.
"""
from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path

import pytest

from nexus.skills import (
    DEFAULT_MAX_INVENTORY_BYTES,
    BundledToolCandidate,
    SkillOversizeError,
    SkillResourceError,
    SkillSecurityError,
    build_inventory,
    build_tool_candidates,
    normalize_resource_path,
    read_resource,
    resolve_resource,
    resolve_resource_path,
    resolve_resource_snapshot,
    sha256_hex,
)


def make_skill_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "references").mkdir(parents=True)
    return skill


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "/etc/passwd",
        "\\windows\\path",
        "~/secret",
        "C:/windows",
        "scripts/../../etc/passwd",
        "scripts/../references/x",
        "scripts/a\x00b",
        "etc/passwd",
        "tools/x.py",
        "scripts",
        "references",
    ],
)
def test_normalize_rejects_unsafe_paths(relative):
    with pytest.raises((SkillSecurityError, SkillResourceError)):
        normalize_resource_path(relative)


@pytest.mark.parametrize(
    "relative, expected",
    [
        ("scripts/run.sh", "scripts/run.sh"),
        ("references/guide.md", "references/guide.md"),
        ("scripts/nested/deep.py", "scripts/nested/deep.py"),
        ("./scripts/run.sh", "scripts/run.sh"),
    ],
)
def test_normalize_accepts_safe_paths(relative, expected):
    assert normalize_resource_path(relative).as_posix() == expected


def test_normalize_rejects_non_strings():
    for bad in (None, 5, Path("scripts/x")):
        with pytest.raises(SkillResourceError):
            normalize_resource_path(bad)


# ---------------------------------------------------------------------------
# Resolution and reading
# ---------------------------------------------------------------------------


def test_reads_scripts_and_references(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (skill / "references" / "guide.md").write_text("# guide\n", encoding="utf-8")

    script = resolve_resource(skill, "scripts/run.sh")
    assert script.kind == "scripts"
    assert script.text == "echo hi\n"
    assert script.size == len("echo hi\n")
    assert script.sha256 == sha256_hex(b"echo hi\n")
    assert read_resource(skill, "references/guide.md") == "# guide\n"
    assert resolve_resource_path(skill, "scripts/run.sh").name == "run.sh"


def test_missing_resource_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    with pytest.raises(SkillResourceError):
        resolve_resource(skill, "scripts/nope.sh")


def test_directory_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "sub").mkdir()
    with pytest.raises(SkillResourceError):
        resolve_resource(skill, "scripts/sub")


def test_symlink_escape_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (skill / "scripts" / "escape.sh").symlink_to(outside)
    with pytest.raises(SkillSecurityError):
        resolve_resource(skill, "scripts/escape.sh")


def test_absolute_symlink_escape_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "passwd").symlink_to("/etc/passwd")
    with pytest.raises(SkillSecurityError):
        resolve_resource(skill, "scripts/passwd")


def test_symlink_within_root_is_allowed(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "real.sh").write_text("ok\n", encoding="utf-8")
    (skill / "scripts" / "alias.sh").symlink_to("real.sh")
    assert resolve_resource(skill, "scripts/alias.sh").text == "ok\n"


def test_device_or_fifo_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    fifo = skill / "scripts" / "pipe"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        pytest.skip("mkfifo is not available on this platform")
    with pytest.raises(SkillResourceError):
        resolve_resource(skill, "scripts/pipe")


def test_oversize_resource_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "big.sh").write_bytes(b"x" * 100)
    with pytest.raises(SkillOversizeError):
        resolve_resource(skill, "scripts/big.sh", max_bytes=10)


def test_invalid_utf8_resource_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "bad.sh").write_bytes(b"\xff\xfe")
    with pytest.raises(SkillResourceError):
        resolve_resource(skill, "scripts/bad.sh")


def test_nul_byte_resource_is_rejected(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "nul.sh").write_bytes(b"a\x00b")
    with pytest.raises(SkillResourceError):
        resolve_resource(skill, "scripts/nul.sh")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def test_inventory_is_sorted_deterministic_and_hashed(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "b.sh").write_text("b", encoding="utf-8")
    (skill / "scripts" / "a.sh").write_text("a", encoding="utf-8")
    (skill / "references" / "doc.md").write_text("doc", encoding="utf-8")

    first = build_inventory(skill)
    second = build_inventory(skill)
    assert first == second
    assert [r.path for r in first] == [
        "scripts/a.sh",
        "scripts/b.sh",
        "references/doc.md",
    ]
    assert [r.kind for r in first] == ["script", "script", "reference"]
    assert all(r.sha256 for r in first)


def test_inventory_flags_escaping_symlinks_without_hashing(tmp_path):
    skill = make_skill_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (skill / "scripts" / "escape.sh").symlink_to(outside)
    inventory = build_inventory(skill)
    assert len(inventory) == 1
    entry = inventory[0]
    assert entry.escaped is True
    assert entry.sha256 is None


def test_inventory_marks_oversized_files(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "big.sh").write_bytes(b"x" * 100)
    inventory = build_inventory(skill, max_bytes=10)
    assert inventory[0].oversized is True
    assert inventory[0].sha256 is None


def test_inventory_skips_non_regular_entries(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "sub").mkdir()
    fifo = skill / "scripts" / "pipe"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        fifo = None
    (skill / "scripts" / "ok.sh").write_text("ok", encoding="utf-8")
    inventory = build_inventory(skill)
    assert [r.path for r in inventory] == ["scripts/ok.sh"]


def test_inventory_filters_an_escaped_glob_entry(tmp_path, monkeypatch):
    from nexus.skills import resources

    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "ok.sh").write_text("ok", encoding="utf-8")
    escaped = tmp_path / "escaped.txt"
    escaped.write_text("outside", encoding="utf-8")

    real_rglob = Path.rglob

    def fake_rglob(self, pattern, *args, **kwargs):
        yield from real_rglob(self, pattern, *args, **kwargs)
        yield escaped  # a path that is not lexically under the skill root

    monkeypatch.setattr(Path, "rglob", fake_rglob)
    inventory = resources.build_inventory(skill)
    # The escaped entry is filtered before ``relative_to`` can raise, so the
    # walk still returns only the in-tree resources.
    assert [r.path for r in inventory] == ["scripts/ok.sh"]


def test_inventory_default_bound_is_positive():
    assert DEFAULT_MAX_INVENTORY_BYTES > 0


# ---------------------------------------------------------------------------
# Tool candidates
# ---------------------------------------------------------------------------


def test_tool_candidates_are_metadata_only(tmp_path):
    skill = make_skill_dir(tmp_path)
    tools = skill / "tools"
    tools.mkdir()
    (tools / "one.py").write_text("SPEC = 1\n", encoding="utf-8")
    (tools / "two.py").write_text("SPEC = 2\n", encoding="utf-8")
    (tools / "_helper.py").write_text("x = 1\n", encoding="utf-8")
    (tools / "__init__.py").write_text("", encoding="utf-8")
    (tools / "notes.txt").write_text("nope", encoding="utf-8")

    candidates = build_tool_candidates(skill)
    assert [c.module for c in candidates] == ["one", "two"]
    assert all(isinstance(c, BundledToolCandidate) for c in candidates)
    assert [c.path for c in candidates] == ["tools/one.py", "tools/two.py"]
    assert all(c.sha256 for c in candidates)


def test_tool_candidate_inventory_is_deterministic(tmp_path):
    skill = make_skill_dir(tmp_path)
    tools = skill / "tools"
    tools.mkdir()
    (tools / "z.py").write_text("z", encoding="utf-8")
    (tools / "a.py").write_text("a", encoding="utf-8")
    assert build_tool_candidates(skill) == build_tool_candidates(skill)


def test_sha256_hex_matches_hashlib():
    import hashlib

    assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_stat_import_is_used_for_regular_file_check(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "ok.sh").write_text("ok", encoding="utf-8")
    resolved = resolve_resource_path(skill, "scripts/ok.sh")
    assert stat_module.S_ISREG(resolved.stat().st_mode)


# ---------------------------------------------------------------------------
# Content snapshots
# ---------------------------------------------------------------------------


def test_inventory_with_content_snapshots_bytes_and_text(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (skill / "references" / "doc.md").write_text("# doc\n", encoding="utf-8")

    inventory = build_inventory(skill, with_content=True)
    assert [r.path for r in inventory] == ["scripts/run.sh", "references/doc.md"]
    assert all(r.snapshotted for r in inventory)
    run = inventory[0]
    assert run.content == b"echo hi\n"
    assert run.text == "echo hi\n"
    assert run.sha256 == sha256_hex(b"echo hi\n")
    assert run.snapshot_error is None


def test_inventory_with_content_refuses_escaped_and_oversized(tmp_path):
    skill = make_skill_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (skill / "scripts" / "escape.sh").symlink_to(outside)
    (skill / "scripts" / "big.sh").write_bytes(b"x" * 100)

    inventory = build_inventory(skill, max_bytes=10, with_content=True)
    by_path = {r.path: r for r in inventory}
    escaped = by_path["scripts/escape.sh"]
    assert escaped.escaped is True
    assert escaped.content is None and escaped.text is None
    oversized = by_path["scripts/big.sh"]
    assert oversized.oversized is True
    assert oversized.content is None and oversized.text is None


def test_inventory_with_content_marks_invalid_utf8(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "bad.sh").write_bytes(b"\xff\xfe")
    inventory = build_inventory(skill, with_content=True)
    entry = inventory[0]
    assert entry.content == b"\xff\xfe"
    assert entry.text is None
    assert entry.snapshot_error is not None


def test_inventory_with_content_follows_within_root_symlink(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "real.sh").write_text("ok\n", encoding="utf-8")
    (skill / "scripts" / "alias.sh").symlink_to("real.sh")
    inventory = build_inventory(skill, with_content=True)
    alias = next(r for r in inventory if r.path == "scripts/alias.sh")
    assert alias.symlink is True
    assert alias.text == "ok\n"


def test_resolve_resource_snapshot_is_generation_stable(tmp_path):
    skill = make_skill_dir(tmp_path)
    target = skill / "scripts" / "run.sh"
    target.write_text("one\n", encoding="utf-8")
    inventory = build_inventory(skill, with_content=True)

    # A later edit must not be visible through the snapshot.
    target.write_text("two\n", encoding="utf-8")
    resolved = resolve_resource_snapshot(inventory, "scripts/run.sh")
    assert resolved.text == "one\n"
    assert resolved.sha256 == sha256_hex(b"one\n")
    assert resolved.kind == "scripts"
    assert resolved.size == len(b"one\n")


def test_resolve_resource_snapshot_refuses_missing_and_unavailable(tmp_path):
    skill = make_skill_dir(tmp_path)
    (skill / "scripts" / "big.sh").write_bytes(b"x" * 100)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (skill / "scripts" / "escape.sh").symlink_to(outside)
    inventory = build_inventory(skill, max_bytes=10, with_content=True)

    with pytest.raises(SkillResourceError):
        resolve_resource_snapshot(inventory, "scripts/missing.sh")
    with pytest.raises(SkillSecurityError):
        resolve_resource_snapshot(inventory, "scripts/escape.sh")
    with pytest.raises(SkillOversizeError):
        resolve_resource_snapshot(inventory, "scripts/big.sh")
    with pytest.raises((SkillSecurityError, SkillResourceError)):
        resolve_resource_snapshot(inventory, "../escape")
