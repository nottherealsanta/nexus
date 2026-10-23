"""Manager launch with ``worktree=true`` against the real facade and a real Git repo."""
from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ManagerSection, ModelSection, PermissionsSection
from nexus.host import HostFacade
from nexus.host_support.manager_port import FacadeManagerHost, HostManagerPort
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.tools.manager_port import LaunchRefused
from manager_helpers import MANAGER_ID, Proxy, user
from test_worktree_sessions_runtime import project, git  # noqa: F401


def _setup(project, tmp_path):  # noqa: F811
    config = Config(
        model="scripted/m", version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m"), manager=ManagerSection(enabled=True),
                    permissions=PermissionsSection(mode="allow")),
    )
    runtime = Runtime(project, home=tmp_path / "home", config=config,
                      providers={"scripted": ScriptedProvider(text_response("done"))})
    facade = HostFacade(runtime)
    facade.open_manager()
    manager = runtime.session(MANAGER_ID)
    manager.append_message(user("work in a worktree"))
    port = HostManagerPort(runtime.sessions, handle=Proxy(manager), host=FacadeManagerHost(facade),
                           poll_interval=0.01)
    return runtime, facade, port


async def test_worktree_launch_creates_the_named_branch_on_the_first_turn(project, tmp_path):  # noqa: F811
    runtime, facade, port = _setup(project, tmp_path)
    try:
        launched = await port.launch("do it", "m1", title="isolated-fix", worktree=True)
        assert launched.worktree_name == "isolated-fix" and launched.title == "isolated-fix"
        waited = await port.wait((launched.id,), "any", 30)
        assert waited.completed == (launched.id,)
        root = runtime.session_root(launched.id)
        assert root.kind == "worktree" and root.path.name == "isolated-fix"
        assert root.branch == "nexus/session/isolated-fix"
        assert git(project, "branch", "--list", "nexus/session/isolated-fix")
        assert runtime.sessions.summary(launched.id).title == "isolated-fix"
    finally:
        await facade.wait_idle(timeout=10)
        await runtime.aclose()


async def test_worktree_launch_with_a_taken_name_starts_nothing(project, tmp_path):  # noqa: F811
    runtime, facade, port = _setup(project, tmp_path)
    try:
        git(project, "branch", "nexus/session/taken")
        with pytest.raises(LaunchRefused) as raised:
            await port.launch("do it", "m1", title="taken", worktree=True)
        assert "already exists" in str(raised.value) and "mgr-" in str(raised.value)
        sid = str(raised.value).split("session ")[1].split(" ")[0]
        assert not [e for e in runtime.session(sid).events if e.type == "input.queued"]
    finally:
        await facade.wait_idle(timeout=10)
        await runtime.aclose()
