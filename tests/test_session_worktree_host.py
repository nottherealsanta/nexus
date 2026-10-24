from __future__ import annotations

import threading
from types import SimpleNamespace

import msgspec
import pytest

from nexus.client.protocol import Client
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.message import Message, Text

from test_session_root_runtime import _project, _runtime


class _Runtime:
    def __init__(self, workspace):
        self.workspace = workspace
        self.calls = []
        self.call_threads = []
        self.sessions = SimpleNamespace(
            resolve=lambda ref, *, namespace: {"id": "canonical-session"}
        )

    def _record_thread(self, call):
        self.calls.append(call)
        self.call_threads.append(threading.get_ident())

    def list_session_worktrees(self, session):
        self._record_thread(("list", session))
        return {"current": "main", "worktrees": [{"kind": "main", "name": "main", "current": True}]}

    def move_session_worktree(self, session, to, reason):
        self._record_thread(("move", session, to, reason))
        return {"root": "main" if to == "main" else "worktree", "moved": True}

    async def promote_session(self, session, *, reason, continuation):
        self._record_thread(("promote", session, reason, continuation))
        return {"root": "worktree", "promoted": True}

    def detach_session_worktree(self, session):
        self._record_thread(("detach", session))
        return {"root": "main", "detached": True}

    def session_worktree_status(self, session):
        self._record_thread(("status", session))
        return {"available": True, "branches": ["main"], "enabled": False}

    def approve_worktree_setup(self, session, decision, *, digest):
        self._record_thread(("trust", session, decision, digest))
        return {"setup_trust": decision}


async def test_session_worktree_commands_dispatch_to_runtime(tmp_path):
    runtime = _Runtime(tmp_path)
    facade = HostFacade(runtime)

    listed = await facade.handle(p.SessionWorktreeList(session="friendly-session"))
    assert listed == p.SessionWorktreeResult(
        session="canonical-session",
        state={"current": "main", "worktrees": [{"kind": "main", "name": "main", "current": True}]},
    )
    moved = await facade.handle(
        p.SessionWorktreeMove(session="friendly-session", to="brave-otter", reason="continue the auth work")
    )
    assert moved.state == {"root": "worktree", "moved": True}
    promoted = await facade.handle(
        p.SessionPromote(session="friendly-session", reason="isolate", continuation="continue")
    )
    assert promoted.state == {"root": "worktree", "promoted": True}
    detached = await facade.handle(p.SessionDetach(session="friendly-session"))
    assert detached.state == {"root": "main", "detached": True}
    status = await facade.handle(p.SessionWorktreeStatus(session="friendly-session"))
    assert status.state == {"available": True, "branches": ["main"], "enabled": False}
    trust = await facade.handle(
        p.SessionWorktreeTrust(session="friendly-session", decision="allow_project", digest="a" * 64)
    )
    assert trust.state == {"setup_trust": "allow_project"}

    assert runtime.calls == [
        ("list", "canonical-session"),
        ("move", "canonical-session", "brave-otter", "continue the auth work"),
        ("promote", "canonical-session", "isolate", "continue"),
        ("detach", "canonical-session"),
        ("status", "canonical-session"),
        ("trust", "canonical-session", "allow_project", "a" * 64),
    ]
    loop_thread = threading.get_ident()
    assert all(
        thread_id == loop_thread
        for call, thread_id in zip(runtime.calls, runtime.call_threads, strict=True)
        if call[0] not in {"status", "list"}  # read-only projections run on a worker thread
    )


class _Transport:
    def __init__(self):
        self.commands = []

    async def request(self, command):
        self.commands.append(command)
        return p.SessionWorktreeResult(session=command.session, state={})


async def test_client_worktree_methods_emit_typed_protocol_commands():
    transport = _Transport()
    client = Client(transport)

    await client.list_session_worktrees("s")
    await client.move_session_worktree("s", "main", reason="done")
    await client.promote_session("s", reason="reason", continuation="next")
    await client.detach_session_worktree("s")
    await client.session_worktree_status("s")
    await client.approve_worktree_setup("s", "allow_once", digest="b" * 64)

    assert transport.commands == [
        p.SessionWorktreeList(session="s"),
        p.SessionWorktreeMove(session="s", to="main", reason="done"),
        p.SessionPromote(session="s", reason="reason", continuation="next"),
        p.SessionDetach(session="s"),
        p.SessionWorktreeStatus(session="s"),
        p.SessionWorktreeTrust(session="s", decision="allow_once", digest="b" * 64),
    ]
    for command in transport.commands:
        assert msgspec.json.decode(msgspec.json.encode(command), type=p.Command) == command


@pytest.mark.asyncio
async def test_host_status_reports_git_availability_and_bounded_branch_choices(tmp_path):
    project, _, _ = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "git-home")
    try:
        runtime.session("git-status").append_message(
            Message(role="user", content=[Text(text="Persist test session")])
        )
        result = await HostFacade(runtime).handle(p.SessionWorktreeStatus(session="git-status"))
        assert isinstance(result, p.SessionWorktreeResult)
        assert result.state["git"] is True
        assert result.state["disabled_reason"] == ""
        assert {"main", "feature-a", "feature-b"} <= set(result.state["branches"])
        assert len(result.state["branches"]) <= 200
    finally:
        await runtime.aclose()

    workspace = tmp_path / "not-a-repo"
    workspace.mkdir()
    runtime = _runtime(workspace, tmp_path / "plain-home")
    try:
        runtime.session("plain-status").append_message(
            Message(role="user", content=[Text(text="Persist test session")])
        )
        result = await HostFacade(runtime).handle(
            p.SessionWorktreeStatus(session="plain-status")
        )
        assert isinstance(result, p.SessionWorktreeResult)
        assert result.state["git"] is False
        assert result.state["disabled_reason"] == "Worktrees need a Git repository"
        assert result.state["branches"] == []
    finally:
        await runtime.aclose()
