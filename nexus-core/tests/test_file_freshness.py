"""Read-before-edit freshness, coverage, isolation, restart and approval.

Scripted providers, temporary homes/workspaces and real files only.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus.bus import Bus
from nexus.llmhub.types import Done, PartDone, Text, ToolCall, ToolResult
from nexus.runtime import Runtime
from nexus.store import Store


class Scripted:
    name = "scripted"

    def __init__(self) -> None:
        self.replies: list[list] = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "tools": [t.name for t in req.tools]}

    async def stream(self, req):
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    db = tmp_path / "nexus.db"
    rt = Runtime(remembering(Store(db), "scripted/m"), Bus())
    workspace = tmp_path / "proj"
    workspace.mkdir()
    project = rt.open_project(str(workspace))
    return rt, provider, workspace, project, db


def read(call_id, path, **kwargs):
    return ToolCall(call_id, "read", json.dumps({"path": path, **kwargs}))


def edit(call_id, path, old, new):
    return ToolCall(call_id, "edit", json.dumps({"path": path, "old_string": old, "new_string": new}))


def patch(call_id, text):
    return ToolCall(call_id, "patch", json.dumps({"patch": text}))


def write(call_id, path, content):
    return ToolCall(call_id, "write", json.dumps({"path": path, "content": content}))


def code(call_id, text):
    return ToolCall(call_id, "code", json.dumps({"code": text}))


async def _idle(rt, session_id):
    for _ in range(500):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"turn did not finish: {session_id}")


async def _turn(rt, provider, session_id, replies):
    provider.replies = replies
    rt.prompt(session_id, "go")
    await _idle(rt, session_id)


def _session(rt, project, permission="ask"):
    return rt.create_session(project, title=None, model=None, permission=permission)


def _results(rt, session_id):
    return [
        part
        for stored in rt.store.messages(session_id)
        for part in stored.message.parts
        if isinstance(part, ToolResult)
    ]


def _last(rt, session_id):
    return _results(rt, session_id)[-1]


def _asked(rt, session_id):
    return [e["data"] for e in rt.store.events(session_id) if e["type"] == "permission.asked"]


async def test_unread_file_is_rejected_then_read_allows_the_edit(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "f.txt"
    target.write_text("alpha\nbeta\n")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [[edit("e1", "f.txt", "beta", "BETA")], [Text("cannot")]])
    result = _last(rt, session.id)
    assert result.is_error and "has not been read" in result.output
    assert target.read_text() == "alpha\nbeta\n"

    await _turn(rt, provider, session.id, [[read("r1", "f.txt")], [edit("e2", "f.txt", "beta", "BETA")], [Text("done")]])
    assert _last(rt, session.id).output == "edited f.txt (1 replacement)"
    assert target.read_text() == "alpha\nBETA\n"
    assert rt.store.file_observation(session.id, str(target)) is None  # the edit invalidated it


async def test_edit_after_a_successful_edit_needs_a_fresh_read(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "f.txt"
    target.write_text("one\ntwo\n")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [read("r1", "f.txt")],
        [edit("e1", "f.txt", "one", "ONE")],
        [edit("e2", "f.txt", "two", "TWO")],
        [Text("done")],
    ])
    second = _results(rt, session.id)[-1]
    assert second.is_error and "has not been read" in second.output
    assert target.read_text() == "ONE\ntwo\n"


async def test_external_change_and_another_session_do_not_authorize_an_edit(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "external.txt"
    target.write_text("alpha\nbeta\n")
    reader = _session(rt, project)

    await _turn(rt, provider, reader.id, [[read("r1", "external.txt")], [Text("read")]])
    target.write_text("alpha\nBETA\n")  # an external process, not a Nexus tool
    await _turn(rt, provider, reader.id, [[edit("e1", "external.txt", "BETA", "gamma")], [Text("no")]])
    stale = _last(rt, reader.id)
    assert stale.is_error and "changed since this session read it" in stale.output

    shared = workspace / "shared.txt"
    shared.write_text("one\ntwo\n")
    other = _session(rt, project)
    await _turn(rt, provider, reader.id, [[read("r2", "shared.txt")], [Text("read")]])
    await _turn(rt, provider, other.id, [[read("r3", "shared.txt")], [edit("e2", "shared.txt", "two", "TWO")], [Text("done")]])
    await _turn(rt, provider, reader.id, [[edit("e3", "shared.txt", "TWO", "three")], [Text("no")]])
    cross_session = _last(rt, reader.id)
    assert cross_session.is_error and "changed since this session read it" in cross_session.output
    assert shared.read_text() == "one\nTWO\n"


async def test_read_by_another_session_does_not_count(env):
    rt, provider, workspace, project, _ = env
    (workspace / "f.txt").write_text("alpha\nbeta\n")
    reader = _session(rt, project)
    other = _session(rt, project)

    await _turn(rt, provider, other.id, [[read("r1", "f.txt")], [Text("read")]])
    await _turn(rt, provider, reader.id, [[edit("e1", "f.txt", "beta", "BETA")], [Text("no")]])
    result = _last(rt, reader.id)
    assert result.is_error and "has not been read in this session" in result.output


async def test_identical_timestamps_with_new_contents_are_stale(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "f.txt"
    target.write_text("alpha\nbeta\n")
    stat = target.stat()
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [[read("r1", "f.txt")], [Text("read")]])
    target.write_text("alpha\nBETA\n")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert target.stat().st_mtime_ns == stat.st_mtime_ns

    await _turn(rt, provider, session.id, [[edit("e1", "f.txt", "BETA", "gamma")], [Text("no")]])
    result = _last(rt, session.id)
    assert result.is_error and "changed since this session read it" in result.output


async def test_partial_reads_only_authorize_the_lines_they_show(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "ten.txt"
    target.write_text("".join(f"line {number}\n" for number in range(1, 11)))
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [read("r1", "ten.txt", offset=1, limit=2)],
        [edit("e1", "ten.txt", "line 9", "LINE NINE")],
        [edit("e2", "ten.txt", "line 1\n", "LINE ONE\n")],
        [Text("done")],
    ])
    unseen, seen = _results(rt, session.id)[-2:]
    assert unseen.is_error and "line 9 was not read intact" in unseen.output
    assert "Next: read path='ten.txt' offset=7 limit=5" in unseen.output  # the call that fixes it
    assert seen.output == "edited ten.txt (1 replacement)"
    text = target.read_text()
    assert text.startswith("LINE ONE\n") and "line 9\n" in text


async def test_a_clipped_line_is_not_covered(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "long.txt"
    long_line = "x" * 3000
    target.write_text(f"{long_line}\nshort\n")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [read("r1", "long.txt")],
        [edit("e1", "long.txt", long_line, "y" * 3000)],
        [edit("e2", "long.txt", "short", "SHORT")],
        [Text("done")],
    ])
    clipped, other = _results(rt, session.id)[-2:]
    assert clipped.is_error and "not read intact" in clipped.output
    assert other.output == "edited long.txt (1 replacement)"
    assert target.read_text().endswith("\nSHORT\n")


async def test_a_whole_clipped_read_establishes_no_coverage(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "big.txt"
    target.write_text("".join(f"row {number:04d} " + "z" * 20 + "\n" for number in range(3000)))
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [read("r1", "big.txt")],
        [edit("e1", "big.txt", "row 0000 " + "z" * 20, "row 0000 " + "y" * 20)],
        [Text("done")],
    ])
    clipped = _results(rt, session.id)[-1]
    assert clipped.is_error and "not read intact" in clipped.output

    await _turn(rt, provider, session.id, [
        [read("r2", "big.txt", offset=1, limit=1)],
        [edit("e2", "big.txt", "row 0000 " + "z" * 20, "row 0000 " + "y" * 20)],
        [Text("done")],
    ])
    assert _last(rt, session.id).output == "edited big.txt (1 replacement)"
    assert target.read_text().startswith("row 0000 " + "y" * 20)


async def test_read_windows_add_up_only_within_one_revision(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "six.txt"
    target.write_text("".join(f"line {number}\n" for number in range(1, 7)))
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [read("r1", "six.txt", offset=1, limit=3)],
        [read("r2", "six.txt", offset=4, limit=3)],
        [write("w1", "six.txt", "all new\n")],
        [Text("done")],
    ])
    assert _last(rt, session.id).output.startswith("overwrote six.txt")
    assert target.read_text() == "all new\n"

    target.write_text("".join(f"line {number}\n" for number in range(1, 7)))
    await _turn(rt, provider, session.id, [[read("r3", "six.txt", offset=1, limit=3)], [Text("read")]])
    target.write_text("".join(f"line {number}\n" for number in range(1, 7)) + "extra\n")
    await _turn(rt, provider, session.id, [
        [read("r4", "six.txt", offset=4, limit=3)],
        [write("w2", "six.txt", "all new\n")],
        [Text("done")],
    ])
    incomplete = _last(rt, session.id)
    assert incomplete.is_error and "was not read completely" in incomplete.output
    assert target.read_text().endswith("extra\n")


async def test_a_patch_update_needs_only_its_affected_lines(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "patch.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n")
    session = _session(rt, project)
    body = (
        "*** Begin Patch\n"
        "*** Update File: patch.txt\n"
        "@@\n"
        " two\n-three\n+THREE\n"
        "*** End Patch"
    )

    await _turn(rt, provider, session.id, [
        [read("r1", "patch.txt", offset=2, limit=2)],
        [patch("p1", body)],
        [Text("done")],
    ])
    assert _last(rt, session.id).output == "updated patch.txt (1 hunks)"
    assert target.read_text() == "one\ntwo\nTHREE\nfour\nfive\n"


async def test_overwrite_delete_move_and_new_destination_need_the_right_coverage(env):
    rt, provider, workspace, project, _ = env
    overwrite = workspace / "overwrite.txt"
    overwrite.write_text("one\ntwo\nthree\n")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [write("w0", "overwrite.txt", "replaced\n")],
        [read("r1", "overwrite.txt", offset=1, limit=2)],
        [write("w1", "overwrite.txt", "replaced\n")],
        [Text("done")],
    ])
    unread, partial, complete = _results(rt, session.id)[-3:]
    assert unread.is_error and "has not been read" in unread.output
    assert not partial.is_error
    assert complete.is_error and "was not read completely" in complete.output
    assert overwrite.read_text() == "one\ntwo\nthree\n"

    await _turn(rt, provider, session.id, [
        [read("r1b", "overwrite.txt")],
        [write("w1b", "overwrite.txt", "replaced\n")],
        [Text("done")],
    ])
    assert _last(rt, session.id).output.startswith("overwrote overwrite.txt")
    assert overwrite.read_text() == "replaced\n"

    # Deleting and moving are whole-file operations.
    doomed = workspace / "doomed.txt"
    doomed.write_text("a\nb\n")
    await _turn(rt, provider, session.id, [[read("r2", "doomed.txt", offset=1, limit=1)], [Text("read")]])
    await _turn(rt, provider, session.id, [
        [patch("p1", "*** Begin Patch\n*** Delete File: doomed.txt\n*** End Patch")], [Text("no")]])
    assert _last(rt, session.id).is_error and "was not read completely" in _last(rt, session.id).output
    assert doomed.exists()

    await _turn(rt, provider, session.id, [[read("r3", "doomed.txt")], [Text("read")]])
    await _turn(rt, provider, session.id, [
        [patch("p2", "*** Begin Patch\n*** Delete File: doomed.txt\n*** End Patch")], [Text("done")]])
    assert _last(rt, session.id).output == "deleted doomed.txt"
    assert not doomed.exists()

    moving = workspace / "moving.txt"
    moving.write_text("x\ny\n")
    await _turn(rt, provider, session.id, [[read("r4", "moving.txt", offset=1, limit=1)], [Text("read")]])
    await _turn(rt, provider, session.id, [
        [patch("p3", "*** Begin Patch\n*** Update File: moving.txt\n*** Move to: moved.txt\n@@\n-x\n+X\n*** End Patch")],
        [Text("no")],
    ])
    assert _last(rt, session.id).is_error and "was not read completely" in _last(rt, session.id).output
    assert moving.exists() and not (workspace / "moved.txt").exists()

    await _turn(rt, provider, session.id, [[read("r5", "moving.txt")], [Text("read")]])
    await _turn(rt, provider, session.id, [
        [patch("p4", "*** Begin Patch\n*** Update File: moving.txt\n*** Move to: moved.txt\n@@\n-x\n+X\n*** End Patch")],
        [Text("done")],
    ])
    assert _last(rt, session.id).output == "updated moving.txt → moved.txt (1 hunks)"
    assert not moving.exists() and (workspace / "moved.txt").read_text() == "X\ny\n"

    # Adding a file needs no read, but never overwrites an unexpected destination.
    collision = workspace / "exists.txt"
    collision.write_text("keep\n")
    await _turn(rt, provider, session.id, [
        [patch("p5", "*** Begin Patch\n*** Add File: exists.txt\n+new\n*** End Patch")], [Text("no")]])
    assert _last(rt, session.id).is_error and "already exists" in _last(rt, session.id).output
    assert collision.read_text() == "keep\n"

    await _turn(rt, provider, session.id, [
        [write("w2", "created.txt", "fresh\n")], [Text("done")]])
    assert _last(rt, session.id).output.startswith("created created.txt")
    assert (workspace / "created.txt").read_text() == "fresh\n"


async def test_directory_listing_is_not_a_read_and_an_empty_file_can_be_observed(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "empty.txt"
    target.write_text("")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [read("r1", ".")],
        [write("w1", "empty.txt", "now full\n")],
        [Text("done")],
    ])
    listing, refused = _results(rt, session.id)[-2:]
    assert "is a directory" in listing.output
    assert refused.is_error and "has not been read in this session" in refused.output

    await _turn(rt, provider, session.id, [
        [read("r2", "empty.txt")],
        [write("w2", "empty.txt", "now full\n")],
        [Text("done")],
    ])
    assert _last(rt, session.id).output.startswith("overwrote empty.txt")
    assert target.read_text() == "now full\n"


async def test_a_stale_file_rejects_the_entire_patch_before_any_write(env):
    rt, provider, workspace, project, _ = env
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("a\nb\n")
    second.write_text("c\nd\n")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [[read("r1", "first.txt"), read("r2", "second.txt")], [Text("read")]])
    second.write_text("c\nd\nextra\n")  # external change after both reads; the hunk still matches
    content = first.read_text()

    await _turn(rt, provider, session.id, [
        [patch("p1", (
            "*** Begin Patch\n"
            "*** Update File: first.txt\n@@\n-a\n+A\n"
            "*** Update File: second.txt\n@@\n-d\n+D\n"
            "*** End Patch"))],
        [Text("no")],
    ])
    result = _last(rt, session.id)
    assert result.is_error and "changed since this session read it" in result.output
    assert first.read_text() == content and second.read_text() == "c\nd\nextra\n"


async def test_observations_are_isolated_and_survive_a_restart(env):
    rt, provider, workspace, project, db = env
    kept = workspace / "kept.txt"
    changed = workspace / "changed.txt"
    kept.write_text("keep\nme\n")
    changed.write_text("before\n")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [[read("r1", "kept.txt"), read("r2", "changed.txt")], [Text("read")]])
    rt.store.close()

    restarted = Runtime(remembering(Store(db), "scripted/m"), Bus())
    changed.write_text("after\n")
    provider.replies = [[edit("e1", "changed.txt", "after", "AFTER"), edit("e2", "kept.txt", "keep", "KEEP")],
                        [Text("done")]]
    restarted.prompt(session.id, "go")
    await _idle(restarted, session.id)
    results = _results(restarted, session.id)[-2:]
    assert results[0].is_error and "changed since this session read it" in results[0].output
    assert results[1].output == "edited kept.txt (1 replacement)"
    assert kept.read_text() == "KEEP\nme\n" and changed.read_text() == "after\n"


async def test_nested_reads_do_not_authorize_but_nested_edits_check_the_guard(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "nested.txt"
    target.write_text("old\n")
    session = _session(rt, project)

    await _turn(rt, provider, session.id, [
        [code("c1", 'try:\n    await edit(path="nested.txt", old_string="old", new_string="new")\n'
                    'except RuntimeError as e:\n    print(e)')],
        [Text("done")],
    ])
    output = _last(rt, session.id).output
    assert "has not been read" in output and target.read_text() == "old\n"

    await _turn(rt, provider, session.id, [
        [code("c2", 'text = await read(path="nested.txt")\nprint(text)\n'
                    'try:\n    await edit(path="nested.txt", old_string="old", new_string="new")\n'
                    'except RuntimeError as e:\n    print(e)')],
        [Text("done")],
    ])
    cell = _last(rt, session.id)
    assert "old" in cell.output and "has not been read" in cell.output
    assert target.read_text() == "old\n"

    # A direct edit cannot use coverage inferred from the cell's printed output.
    await _turn(rt, provider, session.id, [[edit("e1", "nested.txt", "old", "new")], [Text("no")]])
    refused = _last(rt, session.id)
    assert refused.is_error and "has not been read in this session" in refused.output

    await _turn(rt, provider, session.id, [
        [read("r1", "nested.txt")],
        [code("c3", 'await edit(path="nested.txt", old_string="old", new_string="new")')],
        [Text("done")],
    ])
    assert target.read_text() == "new\n"


async def test_file_edits_never_ask_but_bash_still_does(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "f.txt"
    target.write_text("old\n")
    session = _session(rt, project, permission="ask")

    await _turn(rt, provider, session.id, [[read("r1", "f.txt")], [Text("read")]])
    provider.replies = [[
        edit("e1", "f.txt", "old", "new"),
        write("w1", "fresh.txt", "content\n"),
        ToolCall("b1", "bash", '{"command": "true"}'),
    ], [Text("done")]]
    rt.prompt(session.id, "go")
    for _ in range(300):
        pending = rt.pending_approvals(session.id)
        if pending:
            break
        await asyncio.sleep(0.01)
    assert [request["tool"] for request in pending] == ["bash"]
    assert target.read_text() == "new\n" and (workspace / "fresh.txt").read_text() == "content\n"
    rt.reply(session.id, pending[0]["id"], True)
    await _idle(rt, session.id)

    assert [request["tool"] for request in _asked(rt, session.id)] == ["bash"]


async def test_repeated_clipped_reads_do_not_grant_coverage(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "long.txt"
    text = "x" * 3000
    target.write_text(text + "\n")
    session = _session(rt, project)
    await _turn(rt, provider, session.id, [
        [read("r1", "long.txt")], [read("r2", "long.txt")],
        [edit("e1", "long.txt", text, "changed")], [Text("done")],
    ])
    assert _last(rt, session.id).is_error
    assert target.read_text() == text + "\n"


async def test_read_does_not_authorize_an_edit_in_the_same_batch(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "f.txt"
    target.write_text("old\n")
    session = _session(rt, project)
    await _turn(rt, provider, session.id, [
        [read("r1", "f.txt"), edit("e1", "f.txt", "old", "new")], [Text("done")],
    ])
    assert _last(rt, session.id).is_error and target.read_text() == "old\n"
    # The result has now entered context, so a subsequent call may use it.
    await _turn(rt, provider, session.id, [[edit("e2", "f.txt", "old", "new")], [Text("done")]])
    assert not _last(rt, session.id).is_error and target.read_text() == "new\n"


async def test_partial_replacement_preserves_unseen_crlf_bytes(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "f.txt"
    target.write_bytes(b"old\r\nunseen\r\n")
    session = _session(rt, project)
    await _turn(rt, provider, session.id, [
        [read("r1", "f.txt", limit=1)], [edit("e1", "f.txt", "old", "new")], [Text("done")],
    ])
    assert not _last(rt, session.id).is_error
    assert target.read_bytes() == b"new\r\nunseen\r\n"


async def test_unicode_line_separators_cannot_bypass_coverage(env):
    rt, provider, workspace, project, _ = env
    target = workspace / "f.txt"
    target.write_text("seen\u2028unseen\n")
    session = _session(rt, project)
    await _turn(rt, provider, session.id, [
        [read("r1", "f.txt", limit=1)],
        [edit("e1", "f.txt", "unseen", "changed")],
        [patch("p1", "*** Begin Patch\n*** Update File: f.txt\n@@\n-seen\u2028unseen\n+changed\n*** End Patch")],
        [Text("done")],
    ])
    assert all(result.is_error for result in _results(rt, session.id)[-2:])
    assert target.read_text() == "seen\u2028unseen\n"
