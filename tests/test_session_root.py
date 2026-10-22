from pathlib import Path

import pytest

from nexus.events import EVENT_TYPES, Event
from nexus.session.root import SessionRoot
from nexus.view.model import ConversationView
from nexus.view.reduce import apply


def test_root_round_trip_uses_relative_path(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    checkout = tmp_path / "repo-feature"
    root = SessionRoot("worktree", checkout, "project-key", "nexus/session/feature", "main", "abc123", "wt-1")

    data = root.to_data(project)

    assert data == {
        "kind": "worktree",
        "path_display": "repo-feature",
        "branch": "nexus/session/feature",
        "base_ref": "main",
        "base_sha": "abc123",
        "worktree_id": "wt-1",
    }
    assert str(project) not in repr(data)
    assert SessionRoot.from_data(project, data, "project-key") == root


def test_root_round_trip_allows_linked_worktree_outside_project_parent(tmp_path: Path) -> None:
    project = tmp_path / "nested" / "repo"
    checkout = tmp_path / "linked" / "feature"
    root = SessionRoot("external", checkout, "project-key", "feature")

    data = root.to_data(project)

    assert data["path_display"] == "../linked/feature"
    assert SessionRoot.from_data(project, data, "project-key").path == checkout


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"kind": "unknown", "path_display": "repo-feature"},
        {"kind": "external", "path_display": "/private/checkout"},
        {"kind": "external", "path_display": "./checkout"},
        {"kind": "worktree", "path_display": "repo-feature"},
        {"kind": "external", "path_display": "x" * 1025},
    ],
)
def test_root_rejects_malformed_data(tmp_path: Path, data: object) -> None:
    with pytest.raises(ValueError):
        SessionRoot.from_data(tmp_path / "repo", data)


def test_root_event_replays_and_main_is_default() -> None:
    assert "session.root_set" in EVENT_TYPES
    state = ConversationView()
    assert state.root.kind == "main"
    assert state.root.to_dict() == {
        "kind": "main",
        "path_display": "",
        "branch": "",
        "base_ref": "",
        "base_sha": "",
        "worktree_id": "",
    }

    state = apply(state, Event("session.root_set", {
        "kind": "external",
        "path_display": "repo-feature",
        "branch": "feature",
        "base_ref": "main",
        "base_sha": "deadbeef",
        "worktree_id": "",
    }, seq=1))

    assert state.root.kind == "external"
    assert state.root.path_display == "repo-feature"
    assert state.root.branch == "feature"
    assert state.root.base_ref == "main"
    assert state.root.base_sha == "deadbeef"


def test_main_root_resolves_to_canonical_project(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    root = SessionRoot.main(project / ".." / "repo")
    assert root.path == project.resolve()
    assert root.kind == "main"
