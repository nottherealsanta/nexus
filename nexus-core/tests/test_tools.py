import asyncio

import pytest

from nexus.tools import ToolContext, ToolError
from nexus.tools.bash import run as bash
from nexus.tools.edit import run as edit
from nexus.tools.patch import run as patch_tool
from nexus.tools.read import run as read
from nexus.tools.write import run as write


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(tmp_path)


async def test_write_read_round_trip(ctx):
    assert "created" in await write({"path": "a/b.txt", "content": "one\ntwo\n"}, ctx)
    out = await read({"path": "a/b.txt"}, ctx)
    assert "     1\tone" in out and "     2\ttwo" in out
    assert "[lines 2–2 of 2]" in await read({"path": "a/b.txt", "offset": 2}, ctx)


async def test_missing_paths_name_likely_ones(tmp_path):
    """Real guesses from 2026-11 sessions, in a miniature of their repo."""
    from nexus.tools.grep import run as grep
    from nexus.tools.ls import run as ls

    ctx = ToolContext(tmp_path / "ws")
    (tmp_path / "neighbour").mkdir()
    for rel in ("nexus-nsh/App.tsx", "core/nexus/loop.py", "core/nexus/llmhub/types.py",
                "core/tests/test_terminals_http.py", ".agents/skills/x/SKILL.md"):
        (tmp_path / "ws" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "ws" / rel).write_text("x\n")
    cases = [
        (read, {"path": "nexus-nsh/src/App.tsx"}, "nexus-nsh/src/App.tsx does not exist; did you mean nexus-nsh/App.tsx?"),
        (read, {"path": "core/nexus/loop/types.py"}, "did you mean core/nexus/llmhub/types.py?"),
        (grep, {"pattern": "x", "path": "core/tests/test_terminal_api.py"}, "did you mean core/tests/test_terminals_http.py?"),
        (ls, {"path": "core/nexus/loop"}, "core/nexus/loop does not exist; did you mean core/nexus/loop.py?"),
        (edit, {"path": "nexus-nsh/src/App.tsx", "old_string": "a", "new_string": "b"}, "did you mean nexus-nsh/App.tsx?"),
        (patch_tool, {"patch": "*** Begin Patch\n*** Update File: src/App.tsx\n@@\n-a\n+b\n*** End Patch"},
         "Update File src/App.tsx: does not exist; did you mean nexus-nsh/App.tsx?"),
        (read, {"path": ".agents/skills/y/SKILL.md"}, "did you mean .agents/skills/x/SKILL.md?"),  # hidden, when asked for
        (read, {"path": "../neighbor"}, None),  # outside: only the parent directory is consulted
    ]
    for tool, args, expected in cases:
        with pytest.raises(ToolError) as caught:
            await tool(args, ctx)
        if expected is None:
            assert str(caught.value).endswith(f"did you mean {(tmp_path / 'neighbour').resolve()}?"), caught.value
        else:
            assert str(caught.value).endswith(expected) or expected in str(caught.value), caught.value
    with pytest.raises(ToolError, match=r"^nothing-like-it\.md does not exist$"):
        await read({"path": "nothing-like-it.md"}, ctx)


async def test_paths_outside_workspace_are_refused(ctx):
    with pytest.raises(ToolError):
        await read({"path": "../outside"}, ctx)


async def test_bash_reports_exit_code(ctx):
    assert (await bash({"command": "echo hi; exit 3"}, ctx)).startswith("[exit code 3]\nhi")


async def test_cancelling_bash_kills_its_process_group(ctx):
    task = asyncio.create_task(bash({"command": "sleep 2; touch late"}, ctx))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.2)
    assert not (ctx.workspace / "late").exists()


async def test_edit_skips_a_no_op_entry_in_a_list(ctx, tmp_path):
    """Four calls in a week failed whole because one entry of several left its text unchanged."""
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n")
    out = await edit({"path": "f.txt", "edits": [{"old_string": "alpha", "new_string": "ALPHA"},
                                                  {"old_string": "beta", "new_string": "beta"}]}, ctx)
    assert out == "edited f.txt (1 replacement; skipped 1 entry whose old_string and new_string are identical)"
    assert (tmp_path / "f.txt").read_text() == "ALPHA\nbeta\ngamma\n"
    with pytest.raises(ToolError, match="identical"):  # a lone no-op is still an error
        await edit({"path": "f.txt", "old_string": "beta", "new_string": "beta"}, ctx)
    with pytest.raises(ToolError, match="nothing would change"):
        await edit({"path": "f.txt", "edits": [{"old_string": "beta", "new_string": "beta"},
                                               {"old_string": "gamma", "new_string": "gamma"}]}, ctx)
    with pytest.raises(ToolError, match="not found"):  # a skipped entry must still match the file
        await edit({"path": "f.txt", "edits": [{"old_string": "ALPHA", "new_string": "A"},
                                               {"old_string": "delta", "new_string": "delta"}]}, ctx)
    assert (tmp_path / "f.txt").read_text() == "ALPHA\nbeta\ngamma\n"


async def test_edit_replacement_must_be_unique(ctx):
    (ctx.workspace / "f.py").write_text("x = 1\nx = 1\n")
    with pytest.raises(ToolError, match="occurs 2 times"):
        await edit({"path": "f.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    await edit({"path": "f.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, ctx)
    assert (ctx.workspace / "f.py").read_text() == "x = 2\nx = 2\n"


async def test_patch_add_update_move_delete(ctx):
    (ctx.workspace / "keep.py").write_text("def f():\n    return 1\n\ndef g():\n    return 1\n")
    (ctx.workspace / "old.txt").write_text("a\nb\n")
    (ctx.workspace / "gone.txt").write_text("x\n")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.txt\n+hello\n"
        "*** Update File: keep.py\n@@ def g():\n-    return 1\n+    return 2\n"
        "*** Update File: old.txt\n*** Move to: moved.txt\n@@\n a\n-b\n+c\n"
        "*** Delete File: gone.txt\n"
        "*** End Patch"
    )
    out = await patch_tool({"patch": patch}, ctx)
    assert out.splitlines() == ["added new.txt (1 lines)", "updated keep.py (1 hunks)", "updated old.txt → moved.txt (1 hunks)", "deleted gone.txt"]
    assert (ctx.workspace / "keep.py").read_text() == "def f():\n    return 1\n\ndef g():\n    return 2\n"
    assert (ctx.workspace / "moved.txt").read_text() == "a\nc\n"
    assert not (ctx.workspace / "old.txt").exists() and not (ctx.workspace / "gone.txt").exists()


async def test_failed_patch_writes_nothing(ctx):
    patch = "*** Begin Patch\n*** Add File: a.txt\n+x\n*** Update File: missing.txt\n@@\n-y\n+z\n*** End Patch\n"
    with pytest.raises(ToolError):
        await patch_tool({"patch": patch}, ctx)
    assert not (ctx.workspace / "a.txt").exists()


async def test_edit_and_patch_are_separate_tools(ctx):
    from nexus.tools import DEFAULT_TOOLS
    from nexus.tools.edit import TOOL as EDIT
    from nexus.tools.patch import TOOL as PATCH

    assert [t.spec.name for t in DEFAULT_TOOLS if t.spec.name in ("edit", "patch", "multiedit")] == ["edit", "patch"]
    assert EDIT.spec.parameters["required"] == ["path"]
    assert EDIT.spec.parameters["anyOf"] == [{"required": ["old_string", "new_string"]}, {"required": ["edits"]}]
    assert "patch" not in EDIT.spec.parameters["properties"]
    assert PATCH.spec.parameters["required"] == ["patch"]
    (ctx.workspace / "f.py").write_text("x = 1\n")
    with pytest.raises(ToolError, match="use the `patch` tool"):
        await edit({"patch": "*** Begin Patch\n*** End Patch"}, ctx)


@pytest.mark.parametrize("original,old,new,expected", [
    ('say “hello”—now\n', 'say "hello"-now', 'done', 'done\n'),
    ('one  \r\ntwo\r\n', 'one\ntwo', 'three\nfour', 'three\r\nfour\r\n'),
    ('one  \ntwo\n', 'one\ntwo', 'done', 'done\n'),
    ('“x” and "x"\n', '"x"', 'exact', '“x” and exact\n'),
])
async def test_edit_matching_tiers_preserve_unmatched_bytes(ctx, original, old, new, expected):
    path = ctx.workspace / "f.txt"
    path.write_text(original, newline="")
    await edit({"path": "f.txt", "old_string": old, "new_string": new}, ctx)
    assert path.read_text(newline="") == expected


async def test_edit_fuzzy_ambiguity_is_refused(ctx):
    path = ctx.workspace / "f.txt"
    path.write_text('“x” and ”x“\n')
    with pytest.raises(ToolError, match="occurs 2 times"):
        await edit({"path": "f.txt", "old_string": '"x"', "new_string": 'y'}, ctx)
    assert path.read_text() == '“x” and ”x“\n'


async def test_multi_edit_is_ordered_and_atomic(ctx):
    path = ctx.workspace / "f.txt"
    path.write_text("one two\n")
    with pytest.raises(ToolError, match="not found"):
        await edit({"path": "f.txt", "edits": [
            {"old_string": "one", "new_string": "three"},
            {"old_string": "missing", "new_string": "four"},
        ]}, ctx)
    assert path.read_text() == "one two\n"
    await edit({"path": "f.txt", "edits": [
        {"old_string": "one", "new_string": "three"},
        {"old_string": "three two", "new_string": "done"},
    ]}, ctx)
    assert path.read_text() == "done\n"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


async def test_read_returns_an_image_file_as_media(ctx):
    (ctx.workspace / "shot.png").write_bytes(PNG)
    saved = []

    def save(data, mime, name):
        saved.append((data, mime, name))
        return "art_1"

    out = await read({"path": "shot.png"}, ToolContext(ctx.workspace, "c1", save_media=save))
    assert saved == [(PNG, "image/png", "shot.png")]
    assert [(m.artifact, m.mime, m.name) for m in out.media] == [("art_1", "image/png", "shot.png")]
    assert "art_1" in out


async def test_read_image_without_storage_is_an_error(ctx):
    (ctx.workspace / "shot.png").write_bytes(PNG)
    with pytest.raises(ToolError, match="image"):
        await read({"path": "shot.png"}, ctx)
