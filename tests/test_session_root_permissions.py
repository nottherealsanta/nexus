from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection
from nexus.events import Event
from nexus.runtime import Runtime
from nexus.session.root import SessionRoot
from nexus.tools.manager import ToolManager
from nexus.tools.permissions import (
    Decision,
    Grant,
    Outcome,
    PathGuard,
    PathSecurityError,
    PermissionEngine,
    exact_rule,
)
from nexus.tools.spec import ToolCall, ToolSpec


def _config(permissions: PermissionsSection) -> Config:
    return Config(
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            permissions=permissions,
        ),
    )


def _guard(project: Path, root: Path) -> PathGuard:
    if root.resolve() == project.resolve():
        return PathGuard(project, write_roots=("./",))
    return PathGuard(project, write_roots=("./",)).for_worktree(root)


def _prepared_write(config: Config, project: Path, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    guard = _guard(project, root)
    manager = ToolManager(config, workspace=root, path_guard=guard)
    call = ToolCall(
        id="write-1",
        name="Write",
        input={"path": "src/file.txt", "content": "data"},
    )
    return guard, manager, manager.prepare([call]).entries[0]


@pytest.mark.parametrize("root_name", ["project", "linked-a", "linked-b"])
def test_relative_write_rule_applies_at_each_session_root(
    tmp_path: Path, root_name: str
) -> None:
    project = tmp_path / "project"
    root = project if root_name == "project" else tmp_path / root_name
    project.mkdir()
    permissions = PermissionsSection(mode="ask", allow=["Write(src/**)"])
    config = _config(permissions)
    guard, _manager, prepared = _prepared_write(config, project, root)

    evaluation = PermissionEngine(
        mode="ask", allow=permissions.allow, path_guard=guard
    ).evaluate(prepared.call, prepared.spec)

    assert prepared.key == str((root / "src" / "file.txt").resolve())
    assert evaluation.outcome is Outcome.ALLOW
    assert evaluation.code == "allow"
    assert evaluation.rule is not None and evaluation.rule.raw == "Write(src/**)"


def test_relative_deny_precedes_bundle_allow_and_exact_session_grant(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    permissions = PermissionsSection(
        mode="ask", allow=["Bundle:fs"], deny=["Write(src/**)"]
    )
    config = _config(permissions)
    guard, _manager, prepared = _prepared_write(config, project, worktree)
    absolute = str((worktree / "src" / "file.txt").resolve())
    grant = Grant(effect="allow", rule=exact_rule("Write", absolute))

    evaluation = PermissionEngine(
        mode="ask",
        allow=permissions.allow,
        deny=permissions.deny,
        path_guard=guard,
    ).evaluate(prepared.call, prepared.spec, grants=(grant,))

    assert evaluation.outcome is Outcome.DENY
    assert evaluation.code == "deny"
    assert evaluation.rule is not None and evaluation.rule.raw == "Write(src/**)"


def test_shell_command_keys_are_not_reinterpreted_as_root_relative_paths(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    worktree.mkdir()
    guard = _guard(project, worktree)
    spec = ToolSpec(
        name="RunCmd",
        description="Run a command",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        bundle="shell",
        permission_key=lambda data: data["command"],
    )
    call = ToolCall(
        id="shell-1",
        name="RunCmd",
        input={"command": str(worktree / "src" / "file.txt")},
    )

    evaluation = PermissionEngine(
        mode="ask", allow=["RunCmd(src/**)"], path_guard=guard
    ).evaluate(call, spec)

    assert evaluation.outcome is Outcome.ASK
    assert evaluation.code == "ask"


def test_path_guard_still_blocks_project_write_from_worktree(tmp_path: Path) -> None:
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    permissions = PermissionsSection(mode="allow", allow=["Write(**)"])
    config = _config(permissions)
    guard, _manager, _prepared = _prepared_write(config, project, worktree)

    with pytest.raises(PathSecurityError, match="outside the child workspace"):
        guard.resolve(str(project / "src" / "other.txt"), for_write=True)


def test_exact_path_grant_rebinds_within_session_but_not_to_another_session(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    project.mkdir()
    worktree_a.mkdir()
    worktree_b.mkdir()
    permissions = PermissionsSection(mode="ask")
    config = _config(permissions)
    guard_b, manager, prepared_b = _prepared_write(config, project, worktree_b)
    key_a = str(worktree_a / "src" / "file.txt")
    root_event = Event(
        "session.root_set", SessionRoot("external", worktree_a, "p").to_data(project)
    )
    grant_event = Event(
        "permission.resolved",
        {
            "tool": "Write",
            "key": key_a,
            "decision": Decision.ALLOW_ALWAYS.value,
            "scope": "session",
            "grant": {
                "effect": "allow",
                "rule": exact_rule("Write", key_a),
                "scope": "session",
            },
        },
    )
    runtime = Runtime(project, home=tmp_path / "home", config=config)
    try:
        same_session_grants = runtime._session_root_grants(
            SimpleNamespace(events=[root_event, grant_event]),
            SessionRoot("external", worktree_b, "p"),
            manager,
        )
        other_session_grants = runtime._session_root_grants(
            SimpleNamespace(events=[]), SessionRoot("external", worktree_b, "p"), manager
        )

        engine = PermissionEngine(mode="ask", path_guard=guard_b)
        same_session = engine.evaluate(
            prepared_b.call, prepared_b.spec, grants=same_session_grants
        )
        other_session = engine.evaluate(
            prepared_b.call, prepared_b.spec, grants=other_session_grants
        )

        assert same_session.outcome is Outcome.ALLOW
        assert same_session.code == "session_grant"
        assert other_session.outcome is Outcome.ASK
        assert other_session.code == "ask"
    finally:
        import asyncio

        asyncio.run(runtime.aclose())


def test_relative_deny_beats_rebound_same_session_grant(tmp_path: Path) -> None:
    project = tmp_path / "project"
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    project.mkdir()
    worktree_a.mkdir()
    worktree_b.mkdir()
    permissions = PermissionsSection(mode="ask", deny=["Write(src/**)"])
    config = _config(permissions)
    guard_b, manager, prepared_b = _prepared_write(config, project, worktree_b)
    key_a = str(worktree_a / "src" / "file.txt")
    runtime = Runtime(project, home=tmp_path / "home", config=config)
    try:
        grants = runtime._session_root_grants(
            SimpleNamespace(events=[
                Event("session.root_set", SessionRoot("external", worktree_a, "p").to_data(project)),
                Event("permission.resolved", {
                    "tool": "Write",
                    "key": key_a,
                    "decision": Decision.ALLOW_ALWAYS.value,
                    "scope": "session",
                    "grant": {
                        "effect": "allow",
                        "rule": exact_rule("Write", key_a),
                        "scope": "session",
                    },
                }),
            ]),
            SessionRoot("external", worktree_b, "p"),
            manager,
        )
        evaluation = PermissionEngine(
            mode="ask", deny=permissions.deny, path_guard=guard_b
        ).evaluate(prepared_b.call, prepared_b.spec, grants=grants)

        assert evaluation.outcome is Outcome.DENY
        assert evaluation.code == "deny"
        assert evaluation.rule is not None and evaluation.rule.raw == "Write(src/**)"
    finally:
        import asyncio

        asyncio.run(runtime.aclose())
