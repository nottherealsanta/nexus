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
    assert EDIT.spec.parameters["required"] == ["path", "old_string", "new_string"]
    assert "patch" not in EDIT.spec.parameters["properties"]
    assert PATCH.spec.parameters["required"] == ["patch"]
    (ctx.workspace / "f.py").write_text("x = 1\n")
    with pytest.raises(ToolError, match="use the `patch` tool"):
        await edit({"patch": "*** Begin Patch\n*** End Patch"}, ctx)


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
