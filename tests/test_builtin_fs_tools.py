"""Tests for the Phase 2 built-in filesystem tools (plan sections 3.4, 5.3).

The suite is intentionally adversarial: every escape vector is exercised against
a temporary workspace, and no test reads or writes a path outside ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection, ToolsSection
from nexus.errors import OperationCancelled
from nexus.tools.builtin import (
    EDIT_SPEC,
    FS_RUNNERS,
    FS_SPECS,
    FS_TOOLS,
    GLOB_SPEC,
    GREP_SPEC,
    LS_SPEC,
    MULTIEDIT_SPEC,
    READ_SPEC,
    WRITE_SPEC,
    edit,
    glob,
    grep,
    ls,
    multiedit,
    read,
    write,
)
from nexus.tools.permissions import Outcome, PermissionEngine
from nexus.tools.spec import ToolCall, ToolContext, ToolSpecError


class FakeCancel:
    """A minimal :class:`CancelTokenView` for deterministic cancellation tests."""

    def __init__(self, *, cancelled: bool = False, reason: str | None = None) -> None:
        self._cancelled = cancelled
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise OperationCancelled(self._reason or "cancelled")

    async def wait(self) -> None:  # pragma: no cover - unused by the tools
        return None


def make_config(
    *,
    write_roots: tuple[str, ...] = ("./",),
    read_denyroots: tuple[str, ...] = (),
    max_result_tokens: int | None = None,
) -> Config:
    tools = (
        ToolsSection(max_result_tokens=max_result_tokens)
        if max_result_tokens is not None
        else ToolsSection()
    )
    return Config(
        v2=ConfigV2(
            permissions=PermissionsSection(
                mode="allow",
                write_roots=list(write_roots),
                read_denyroots=list(read_denyroots),
            ),
            tools=tools,
        )
    )


def make_ctx(
    workspace: Path,
    *,
    config: Config | None = None,
    cancel_token: FakeCancel | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="s1",
        turn_id="t1",
        config=config or make_config(),
        cancel_token=cancel_token,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# Specs and registration
# ---------------------------------------------------------------------------


def test_specs_cover_the_fs_bundle_in_order():
    names = [spec.name for spec in FS_SPECS]
    assert names == ["Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"]
    assert all(spec.bundle == "fs" for spec in FS_SPECS)
    assert {spec.name for spec in FS_SPECS} == set(FS_RUNNERS)
    assert [tool.name for tool in FS_TOOLS] == names
    assert all(tool.origin == "builtin" for tool in FS_TOOLS)


def test_spec_mutates_and_concurrency_flags():
    assert READ_SPEC.mutates is False and GLOB_SPEC.mutates is False
    assert GREP_SPEC.mutates is False and LS_SPEC.mutates is False
    for spec in (WRITE_SPEC, EDIT_SPEC, MULTIEDIT_SPEC):
        assert spec.mutates is True
        assert spec.concurrency == "exclusive"


@pytest.mark.parametrize(
    "spec", FS_SPECS, ids=lambda spec: spec.name
)
def test_permission_key_accepts_relative_paths(spec):
    assert spec.resolve_permission_key({"path": "./sub/../sub/f.txt"}) == "sub/f.txt"


def test_permission_key_canonicalizes_absolute_symlink(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    key = READ_SPEC.resolve_permission_key({"path": str(alias / "f.txt")})
    assert key == str((real / "f.txt").resolve())


def test_permission_key_fails_closed():
    with pytest.raises(ToolSpecError):
        READ_SPEC.resolve_permission_key({})
    with pytest.raises(ToolSpecError):
        READ_SPEC.resolve_permission_key({"path": "bad\x00path"})


def test_directory_tools_default_key_to_workspace_root():
    assert GLOB_SPEC.resolve_permission_key({"pattern": "*.py"}) == "."
    assert GREP_SPEC.resolve_permission_key({"pattern": "x"}) == "."
    assert LS_SPEC.resolve_permission_key({}) == "."


def test_engine_allows_glob_without_path(workspace: Path):
    engine = PermissionEngine(mode="allow", workspace=workspace)
    call = ToolCall(id="c1", name="Glob", input={"pattern": "*.py"})
    evaluation = engine.evaluate(call, GLOB_SPEC)
    assert evaluation.outcome is Outcome.ALLOW


def test_engine_denies_read_without_path(workspace: Path):
    engine = PermissionEngine(mode="allow", workspace=workspace)
    evaluation = engine.evaluate(ToolCall(id="c1", name="Read", input={}), READ_SPEC)
    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "permission_key_error"


async def test_non_object_arguments_are_errors(workspace: Path):
    ctx = make_ctx(workspace)
    for runner in (read.run, write.run, edit.run, multiedit.run, glob.run, grep.run, ls.run):
        result = await runner([], ctx)  # type: ignore[arg-type]
        assert result.is_error is True


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def test_read_utf8_round_trip(workspace: Path):
    content = "héllo ☃\nsecond line"
    (workspace / "u.txt").write_bytes(content.encode("utf-8"))
    result = await read.run({"path": "u.txt"}, make_ctx(workspace))
    assert result.is_error is False
    assert result.content[0].text == content
    assert result.display.startswith("Read u.txt")
    assert result.metrics["lines"] == 2


async def test_read_crlf_and_missing_trailing_newline(workspace: Path):
    (workspace / "crlf.txt").write_bytes(b"a\r\nb\r\nc")
    result = await read.run({"path": "crlf.txt"}, make_ctx(workspace))
    assert result.content[0].text == "a\nb\nc"


async def test_read_offset_and_limit(workspace: Path):
    (workspace / "n.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    ctx = make_ctx(workspace)
    result = await read.run({"path": "n.txt", "offset": 2, "limit": 4}, ctx)
    assert result.content[0].text == "2\n3\n4\n5"
    assert result.metrics["truncated"] is False


async def test_read_missing_file_and_directory(workspace: Path):
    ctx = make_ctx(workspace)
    assert (await read.run({"path": "nope.txt"}, ctx)).is_error is True
    (workspace / "adir").mkdir()
    assert (await read.run({"path": "adir"}, ctx)).is_error is True


async def test_read_binary_reports_not_decodes(workspace: Path):
    (workspace / "b.bin").write_bytes(b"\x00\x01\x02payload")
    result = await read.run({"path": "b.bin"}, make_ctx(workspace))
    assert result.is_error is True
    assert result.metrics["binary"] is True
    assert "\x00" not in (result.display or "")


async def test_read_invalid_utf8_is_replaced_not_fatal(workspace: Path):
    (workspace / "latin.txt").write_bytes(b"caf\xe9")
    result = await read.run({"path": "latin.txt"}, make_ctx(workspace))
    assert result.is_error is False
    assert result.metrics["replacements"] == 1


async def test_read_line_truncation_marker_and_context_note(workspace: Path):
    (workspace / "many.txt").write_text(
        "\n".join(f"line{i}" for i in range(50)), encoding="utf-8"
    )
    result = await read.run({"path": "many.txt", "limit": 5}, make_ctx(workspace))
    assert result.metrics["truncated"] is True
    assert "showing 5 of 50 lines" in result.content[0].text
    assert result.context_note and "re-run with offset=" in result.context_note


async def test_read_byte_cap_truncates(workspace: Path):
    (workspace / "big.txt").write_text("x" * 500, encoding="utf-8")
    ctx = make_ctx(workspace, config=make_config(max_result_tokens=10))
    result = await read.run({"path": "big.txt"}, ctx)
    assert result.metrics["truncated"] is True
    assert "output cut at" in result.content[0].text


@pytest.mark.parametrize("raw", ["~/secret", "bad\x00path", "", "   "])
async def test_read_rejects_bad_paths(workspace: Path, raw: str):
    result = await read.run({"path": raw}, make_ctx(workspace))
    assert result.is_error is True


async def test_read_rejects_offset_and_limit_below_one(workspace: Path):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    ctx = make_ctx(workspace)
    assert (await read.run({"path": "f.txt", "offset": 0}, ctx)).is_error is True
    assert (await read.run({"path": "f.txt", "limit": 0}, ctx)).is_error is True


async def test_read_honours_read_denyroots(tmp_path: Path, workspace: Path):
    secret = workspace / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("key", encoding="utf-8")
    ctx = make_ctx(workspace, config=make_config(read_denyroots=(str(secret),)))
    result = await read.run({"path": "secret/id_rsa"}, ctx)
    assert result.is_error is True
    assert "read-deny" in result.content[0].text


async def test_read_denyroot_via_symlink_fails_closed(tmp_path: Path, workspace: Path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("key", encoding="utf-8")
    (workspace / "leak").symlink_to(secret, target_is_directory=True)
    ctx = make_ctx(workspace, config=make_config(read_denyroots=(str(secret),)))
    result = await read.run({"path": "leak/id_rsa"}, ctx)
    assert result.is_error is True


async def test_read_outside_workspace_is_allowed_within_tmp(
    tmp_path: Path, workspace: Path
):
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("sibling", encoding="utf-8")
    result = await read.run({"path": "../sibling.txt"}, make_ctx(workspace))
    assert result.is_error is False
    assert result.content[0].text == "sibling"


async def test_read_cancellation(workspace: Path):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    ctx = make_ctx(workspace, cancel_token=FakeCancel(cancelled=True))
    with pytest.raises(OperationCancelled):
        await read.run({"path": "f.txt"}, ctx)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def test_write_creates_and_reports(workspace: Path):
    result = await write.run({"path": "new.txt", "content": "hi"}, make_ctx(workspace))
    assert result.is_error is False
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "hi"
    assert result.metrics["created"] is True


async def test_write_replaces_existing_atomically(workspace: Path):
    target = workspace / "existing.txt"
    target.write_text("old", encoding="utf-8")
    result = await write.run({"path": "existing.txt", "content": "new"}, make_ctx(workspace))
    assert result.is_error is False
    assert result.metrics["created"] is False
    assert target.read_text(encoding="utf-8") == "new"
    leftovers = [p for p in workspace.iterdir() if p.name.startswith(".nexus-write-")]
    assert leftovers == []


async def test_write_requires_create_parents(workspace: Path):
    result = await write.run({"path": "missing/deep.txt", "content": "x"}, make_ctx(workspace))
    assert result.is_error is True
    assert not (workspace / "missing").exists()


async def test_write_creates_parents_when_asked(workspace: Path):
    result = await write.run(
        {"path": "missing/deep.txt", "content": "x", "create_parents": True},
        make_ctx(workspace),
    )
    assert result.is_error is False
    assert (workspace / "missing" / "deep.txt").read_text(encoding="utf-8") == "x"


async def test_write_outside_write_roots_is_denied(workspace: Path):
    result = await write.run({"path": "../escape.txt", "content": "x"}, make_ctx(workspace))
    assert result.is_error is True
    assert not (workspace.parent / "escape.txt").exists()


async def test_write_explicit_write_root_outside_workspace(workspace: Path, tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    ctx = make_ctx(workspace, config=make_config(write_roots=("./", str(shared))))
    result = await write.run({"path": str(shared / "out.txt"), "content": "ok"}, ctx)
    assert result.is_error is False
    assert (shared / "out.txt").read_text(encoding="utf-8") == "ok"


async def test_write_symlink_escape_denied(workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    result = await write.run({"path": "escape/new.txt", "content": "x"}, make_ctx(workspace))
    assert result.is_error is True
    assert list(outside.iterdir()) == []


async def test_write_to_read_denyroot_denied(workspace: Path):
    secret = workspace / "secret"
    secret.mkdir()
    ctx = make_ctx(workspace, config=make_config(read_denyroots=(str(secret),)))
    result = await write.run({"path": "secret/x.txt", "content": "x"}, ctx)
    assert result.is_error is True


async def test_write_cancellation_raises(workspace: Path):
    ctx = make_ctx(workspace, cancel_token=FakeCancel(cancelled=True))
    with pytest.raises(OperationCancelled):
        await write.run({"path": "x.txt", "content": "x"}, ctx)
    assert not (workspace / "x.txt").exists()


async def test_write_rejects_nul_and_tilde(workspace: Path):
    ctx = make_ctx(workspace)
    assert (await write.run({"path": "a\x00b", "content": "x"}, ctx)).is_error is True
    assert (await write.run({"path": "~/x", "content": "x"}, ctx)).is_error is True


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


async def test_edit_single_replacement(workspace: Path):
    (workspace / "f.txt").write_text("hello world", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "world", "new_string": "there"},
        make_ctx(workspace),
    )
    assert result.is_error is False
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "hello there"
    assert result.diff == {
        "path": "f.txt",
        "hunk": "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-hello world\n+hello there",
        "added_lines": 1,
        "removed_lines": 1,
        "truncated": False,
    }


async def test_edit_no_match_leaves_file_untouched(workspace: Path):
    target = workspace / "f.txt"
    target.write_text("hello", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "absent", "new_string": "x"},
        make_ctx(workspace),
    )
    assert result.is_error is True
    assert result.diff is None
    assert target.read_text(encoding="utf-8") == "hello"


async def test_edit_multiple_match_rejected_by_default(workspace: Path):
    target = workspace / "f.txt"
    target.write_text("x x x", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "x", "new_string": "y"}, make_ctx(workspace)
    )
    assert result.is_error is True
    assert target.read_text(encoding="utf-8") == "x x x"


async def test_edit_replace_all(workspace: Path):
    (workspace / "f.txt").write_text("x x x", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "x", "new_string": "y", "replace_all": True},
        make_ctx(workspace),
    )
    assert result.metrics["replacements"] == 3
    assert result.diff["added_lines"] == result.diff["removed_lines"] == 1
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "y y y"


async def test_edit_occurrence(workspace: Path):
    (workspace / "f.txt").write_text("x x x", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "x", "new_string": "y", "occurrence": 2},
        make_ctx(workspace),
    )
    assert result.is_error is False
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "x y x"


async def test_edit_occurrence_out_of_range(workspace: Path):
    target = workspace / "f.txt"
    target.write_text("x x", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "x", "new_string": "y", "occurrence": 5},
        make_ctx(workspace),
    )
    assert result.is_error is True
    assert target.read_text(encoding="utf-8") == "x x"


async def test_edit_rejects_replace_all_and_occurrence_together(workspace: Path):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    result = await edit.run(
        {
            "path": "f.txt",
            "old_string": "x",
            "new_string": "y",
            "replace_all": True,
            "occurrence": 1,
        },
        make_ctx(workspace),
    )
    assert result.is_error is True


async def test_edit_identical_strings_rejected(workspace: Path):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "x", "new_string": "x"}, make_ctx(workspace)
    )
    assert result.is_error is True


async def test_edit_preserves_crlf(workspace: Path):
    (workspace / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    await edit.run(
        {"path": "crlf.txt", "old_string": "b", "new_string": "B"},
        make_ctx(workspace),
    )
    assert (workspace / "crlf.txt").read_bytes() == b"a\r\nB\r\n"


async def test_edit_binary_rejected(workspace: Path):
    (workspace / "b.bin").write_bytes(b"\x00abc")
    result = await edit.run(
        {"path": "b.bin", "old_string": "a", "new_string": "b"}, make_ctx(workspace)
    )
    assert result.is_error is True


async def test_edit_size_cap(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(edit, "_MAX_EDIT_BYTES", 4)
    (workspace / "f.txt").write_text("0123456789", encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": "0", "new_string": "z"}, make_ctx(workspace)
    )
    assert result.is_error is True


async def test_edit_diff_preview_is_bounded_and_redacted(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(edit, "_MAX_DIFF_LINES", 3)
    monkeypatch.setattr(edit, "_MAX_DIFF_CHARS", 100)
    before = "\n".join(f"old-{index}" for index in range(20))
    after = "\n".join(
        "api_key=sk-live-secret-value" if index == 10 else f"new-{index}"
        for index in range(20)
    )
    (workspace / "f.txt").write_text(before, encoding="utf-8")
    result = await edit.run(
        {"path": "f.txt", "old_string": before, "new_string": after},
        make_ctx(workspace),
    )
    assert result.is_error is False
    assert result.diff["added_lines"] == 20
    assert result.diff["removed_lines"] == 20
    assert result.diff["truncated"] is True
    assert len(result.diff["hunk"].splitlines()) <= 4
    assert len(result.diff["hunk"]) <= 100
    assert "sk-live-secret-value" not in result.diff["hunk"]


# ---------------------------------------------------------------------------
# MultiEdit
# ---------------------------------------------------------------------------


async def test_multiedit_applies_progressively(workspace: Path):
    (workspace / "f.txt").write_text("alpha beta", encoding="utf-8")
    result = await multiedit.run(
        {
            "path": "f.txt",
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "ALPHA", "new_string": "omega"},
            ],
        },
        make_ctx(workspace),
    )
    assert result.is_error is False
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "omega beta"


async def test_multiedit_invalid_edit_rolls_back_everything(workspace: Path):
    target = workspace / "f.txt"
    target.write_text("alpha beta gamma", encoding="utf-8")
    result = await multiedit.run(
        {
            "path": "f.txt",
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "absent", "new_string": "x"},
            ],
        },
        make_ctx(workspace),
    )
    assert result.is_error is True
    assert target.read_text(encoding="utf-8") == "alpha beta gamma"
    leftovers = [p for p in workspace.iterdir() if p.name.startswith(".nexus-write-")]
    assert leftovers == []


async def test_multiedit_multiple_match_aborts(workspace: Path):
    target = workspace / "f.txt"
    target.write_text("x x", encoding="utf-8")
    result = await multiedit.run(
        {"path": "f.txt", "edits": [{"old_string": "x", "new_string": "y"}]},
        make_ctx(workspace),
    )
    assert result.is_error is True
    assert target.read_text(encoding="utf-8") == "x x"


async def test_multiedit_rejects_empty_edits(workspace: Path):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    assert (
        await multiedit.run({"path": "f.txt", "edits": []}, make_ctx(workspace))
    ).is_error is True


async def test_multiedit_cancellation(workspace: Path):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    ctx = make_ctx(workspace, cancel_token=FakeCancel(cancelled=True))
    with pytest.raises(OperationCancelled):
        await multiedit.run(
            {"path": "f.txt", "edits": [{"old_string": "x", "new_string": "y"}]}, ctx
        )
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


async def test_glob_is_sorted_deterministically(workspace: Path):
    for name in ("c.py", "a.py", "b.py"):
        (workspace / name).write_text("", encoding="utf-8")
    result = await glob.run({"pattern": "*.py"}, make_ctx(workspace))
    assert result.content[0].text.splitlines() == ["a.py", "b.py", "c.py"]


async def test_glob_recursive_double_star(workspace: Path):
    (workspace / "a.py").write_text("", encoding="utf-8")
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "b.py").write_text("", encoding="utf-8")
    (workspace / "pkg" / "sub").mkdir()
    (workspace / "pkg" / "sub" / "c.py").write_text("", encoding="utf-8")
    result = await glob.run({"pattern": "**/*.py"}, make_ctx(workspace))
    assert result.content[0].text.splitlines() == [
        "a.py",
        "pkg/b.py",
        "pkg/sub/c.py",
    ]


async def test_glob_hidden_policy(workspace: Path):
    (workspace / ".secret").write_text("", encoding="utf-8")
    (workspace / "visible").write_text("", encoding="utf-8")
    ctx = make_ctx(workspace)
    default = await glob.run({"pattern": "*"}, ctx)
    assert default.content[0].text.splitlines() == ["visible"]
    hidden = await glob.run({"pattern": "*", "include_hidden": True}, ctx)
    assert set(hidden.content[0].text.splitlines()) == {".secret", "visible"}


async def test_glob_dir_only_pattern(workspace: Path):
    (workspace / "src").mkdir()
    (workspace / "src" / "f.txt").write_text("", encoding="utf-8")
    (workspace / "top.txt").write_text("", encoding="utf-8")
    result = await glob.run({"pattern": "src/"}, make_ctx(workspace))
    assert result.content[0].text.splitlines() == ["src/"]


async def test_glob_prunes_outside_symlink(workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    result = await glob.run({"pattern": "**/*"}, make_ctx(workspace))
    assert "escape/secret.txt" not in result.content[0].text


async def test_glob_prunes_read_denyroot(workspace: Path):
    secret = workspace / "secret"
    secret.mkdir()
    (secret / "inner.txt").write_text("", encoding="utf-8")
    ctx = make_ctx(workspace, config=make_config(read_denyroots=(str(secret),)))
    result = await glob.run({"pattern": "**/*"}, ctx)
    assert result.content[0].text == ""


async def test_glob_rejects_outside_root(workspace: Path):
    result = await glob.run({"path": "..", "pattern": "*"}, make_ctx(workspace))
    assert result.is_error is True


async def test_glob_match_cap(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(glob, "_DEFAULT_MAX_MATCHES", 2)
    for name in ("a", "b", "c", "d", "e"):
        (workspace / name).write_text("", encoding="utf-8")
    result = await glob.run({"pattern": "*"}, make_ctx(workspace))
    assert result.metrics["matches"] == 2
    assert result.metrics["total_matches"] == 5
    assert result.metrics["truncated"] is True
    assert "showing 2 of 5 matches" in result.content[0].text


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------


async def test_grep_regex_and_sorted_output(workspace: Path):
    (workspace / "b.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (workspace / "a.txt").write_text("beta\ngamma\n", encoding="utf-8")
    result = await grep.run({"pattern": "beta"}, make_ctx(workspace))
    assert result.content[0].text.splitlines() == [
        "a.txt:1:beta",
        "b.txt:2:beta",
    ]


async def test_grep_literal_mode(workspace: Path):
    (workspace / "f.txt").write_text("a.c\nabc\n", encoding="utf-8")
    ctx = make_ctx(workspace)
    regex = await grep.run({"pattern": "a.c"}, ctx)
    assert len(regex.content[0].text.splitlines()) == 2
    literal = await grep.run({"pattern": "a.c", "regex": False}, ctx)
    assert literal.content[0].text.splitlines() == ["f.txt:1:a.c"]


async def test_grep_case_insensitive(workspace: Path):
    (workspace / "f.txt").write_text("HELLO\n", encoding="utf-8")
    ctx = make_ctx(workspace)
    assert (await grep.run({"pattern": "hello"}, ctx)).content[0].text == ""
    assert (
        await grep.run({"pattern": "hello", "case_insensitive": True}, ctx)
    ).content[0].text == "f.txt:1:HELLO"


async def test_grep_skips_binary(workspace: Path):
    (workspace / "b.bin").write_bytes(b"\x00hello")
    (workspace / "t.txt").write_text("hello\n", encoding="utf-8")
    result = await grep.run({"pattern": "hello"}, make_ctx(workspace))
    assert result.content[0].text == "t.txt:1:hello"
    assert result.metrics["binary_skipped"] == 1


async def test_grep_glob_filter(workspace: Path):
    (workspace / "a.py").write_text("needle\n", encoding="utf-8")
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    result = await grep.run({"pattern": "needle", "glob": "*.py"}, make_ctx(workspace))
    assert result.content[0].text == "a.py:1:needle"


async def test_grep_prunes_read_denyroot(workspace: Path):
    secret = workspace / "secret"
    secret.mkdir()
    (secret / "s.txt").write_text("needle\n", encoding="utf-8")
    (workspace / "ok.txt").write_text("needle\n", encoding="utf-8")
    ctx = make_ctx(workspace, config=make_config(read_denyroots=(str(secret),)))
    result = await grep.run({"pattern": "needle"}, ctx)
    assert result.content[0].text == "ok.txt:1:needle"


async def test_grep_invalid_regex(workspace: Path):
    result = await grep.run({"pattern": "("}, make_ctx(workspace))
    assert result.is_error is True


async def test_grep_match_cap(workspace: Path):
    (workspace / "f.txt").write_text("hit\nhit\nhit\nhit\n", encoding="utf-8")
    result = await grep.run({"pattern": "hit", "max_matches": 2}, make_ctx(workspace))
    assert result.metrics["matches"] == 2
    assert result.metrics["total_matches"] == 4
    assert result.metrics["truncated"] is True
    assert "showing 2 of 4 matches" in result.content[0].text


async def test_grep_output_byte_cap(workspace: Path):
    (workspace / "f.txt").write_text("x" * 500 + "\n", encoding="utf-8")
    ctx = make_ctx(workspace, config=make_config(max_result_tokens=10))
    result = await grep.run({"pattern": "x"}, ctx)
    assert result.metrics["truncated"] is True


# ---------------------------------------------------------------------------
# LS
# ---------------------------------------------------------------------------


async def test_ls_sorted_with_dirs_and_hidden(workspace: Path):
    (workspace / "b.txt").write_text("", encoding="utf-8")
    (workspace / "a.txt").write_text("", encoding="utf-8")
    (workspace / "dir").mkdir()
    (workspace / ".hidden").write_text("", encoding="utf-8")
    ctx = make_ctx(workspace)
    result = await ls.run({}, ctx)
    assert result.content[0].text.splitlines() == ["a.txt", "b.txt", "dir/"]
    hidden = await ls.run({"include_hidden": True}, ctx)
    assert ".hidden" in hidden.content[0].text


async def test_ls_symlink_inside_shows_relative_target(workspace: Path):
    (workspace / "real").mkdir()
    (workspace / "link").symlink_to(workspace / "real", target_is_directory=True)
    result = await ls.run({}, make_ctx(workspace))
    assert "link@ -> real" in result.content[0].text


async def test_ls_outside_symlink_hides_target(workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    result = await ls.run({}, make_ctx(workspace))
    assert "escape@" in result.content[0].text
    assert "->" not in result.content[0].text


async def test_ls_prunes_read_denyroot(workspace: Path):
    secret = workspace / "secret"
    secret.mkdir()
    (workspace / "ok.txt").write_text("", encoding="utf-8")
    ctx = make_ctx(workspace, config=make_config(read_denyroots=(str(secret),)))
    result = await ls.run({}, ctx)
    assert result.content[0].text.splitlines() == ["ok.txt"]


async def test_ls_entry_cap(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ls, "_HARD_MAX_ENTRIES", 2)
    for name in ("a", "b", "c", "d", "e"):
        (workspace / name).write_text("", encoding="utf-8")
    result = await ls.run({}, make_ctx(workspace))
    assert result.metrics["entries"] == 2
    assert result.metrics["total_entries"] == 5
    assert result.metrics["truncated"] is True


async def test_ls_not_a_directory(workspace: Path):
    (workspace / "f.txt").write_text("", encoding="utf-8")
    result = await ls.run({"path": "f.txt"}, make_ctx(workspace))
    assert result.is_error is True


# ---------------------------------------------------------------------------
# Config robustness
# ---------------------------------------------------------------------------


async def test_plain_config_uses_builtin_defaults(workspace: Path):
    ctx = ToolContext(
        workspace=workspace, session_id="s", turn_id="t", config=Config()
    )
    result = await write.run({"path": "x.txt", "content": "ok"}, ctx)
    assert result.is_error is False
    assert (workspace / "x.txt").read_text(encoding="utf-8") == "ok"
