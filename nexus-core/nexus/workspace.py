"""Workspace control: on-demand tools, session-directory moves and worktrees.

These are control-plane tools, not task tools: they change Nexus state (where a
session works, which Git worktrees exist), never files. They reach the model
through `expose_workspace_tools`, which starts a new session with one
lightweight entry point and adds the other two definitions to the conversation
when the model asks for them; a client can activate them explicitly for an older
session through the API. Execution is bound by the daemon
(`nexus/runtime.py`); the Git work itself is in `nexus/worktrees.py`.

No permission setting guards them. Activation only makes tools available; it
never asks a question, moves the session or changes its frozen system prompt.
"""

from __future__ import annotations

import json
from typing import Any

EXPOSE = "expose_workspace_tools"
CHANGE_DIRECTORY = "change_session_directory"
WORKTREE = "worktree"
WORKSPACE_TOOLS = (CHANGE_DIRECTORY, WORKTREE)
CONTROL_NAMES = (EXPOSE, CHANGE_DIRECTORY, WORKTREE)  # never available inside `code`

_DEFINITIONS: dict[str, dict[str, Any]] = {
    EXPOSE: {
        "name": EXPOSE,
        "description": (
            "Make the workspace control tools available: moving this session's working directory, and listing, "
            "creating or deleting Git worktrees. Call it when the task needs work in a different directory or a "
            "worktree. It adds their definitions to this conversation and to the next request; it does "
            "not move the session, change files or ask the user."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    CHANGE_DIRECTORY: {
        "name": CHANGE_DIRECTORY,
        "description": (
            "Move this session's working directory to an existing directory. Subsequent tool and model calls "
            "resolve from the new directory, and that directory's AGENTS.md arrives as forward context. It "
            "does not create a worktree or move files, and it never rewrites the frozen system prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "An existing directory: absolute, or relative to the current session directory.",
                },
            },
            "required": ["directory"],
            "additionalProperties": False,
        },
    },
    WORKTREE: {
        "name": WORKTREE,
        "description": (
            "List, create or delete Git worktrees for this session's repository. create returns the worktree path "
            "and leaves the session directory unchanged; call change_session_directory to work there. delete "
            "refuses the main checkout, dirty worktrees and worktrees used by any session, and never deletes a "
            "branch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "delete"]},
                "path": {
                    "type": "string",
                    "description": "create/delete: worktree path; absolute, or relative to the main checkout.",
                },
                "branch": {
                    "type": "string",
                    "description": "create: reuse this branch, or create it if it does not exist.",
                },
                "base": {
                    "type": "string",
                    "description": "create: commit or branch to start a new branch from (default HEAD).",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def definition(name: str) -> dict[str, Any]:
    """A snapshot-shaped tool definition (name, description, parameters)."""
    return dict(_DEFINITIONS[name])


def _render_definition(name: str) -> str:
    item = _DEFINITIONS[name]
    parameters = json.dumps(item["parameters"], indent=2, sort_keys=True)
    return (
        f"### `{item['name']}`\n"
        f"{item['description']}\n"
        f"Parameters:\n{parameters}"
    )


def activation_text(enabled: list[str] | None = None) -> str:
    """The model-visible result of `expose_workspace_tools`."""
    available = list(WORKSPACE_TOOLS) if enabled is None else [n for n in WORKSPACE_TOOLS if n in enabled]
    disabled = [n for n in WORKSPACE_TOOLS if n not in available]
    text = (
        "[Nexus notice: workspace control tools activated]\n"
        "Enabled control tools below are callable in the next request. Activation changes no directory "
        "and asks for no approval.\n\n"
        + "\n\n".join(_render_definition(name) for name in available)
    )
    if disabled:
        text += "\nSwitched off and unavailable: " + ", ".join(disabled) + "."
    return text


def already_active_text(enabled: list[str] | None = None) -> str:
    return (
        "[Nexus notice: workspace control tools already active]\n"
        "Nothing changed. Currently enabled: "
        + (", ".join(WORKSPACE_TOOLS if enabled is None else [n for n in WORKSPACE_TOOLS if n in enabled]) or "none")
        + "."
    )


def api_activation_text() -> str:
    """Forward guidance when a client activates the tools without a tool call."""
    return activation_text()


def directory_notice(*, previous: str, current: str, git: dict[str, Any] | None,
                      instruction_section: str, source: str) -> str:
    """The one announcement used by the tool result and by a client-driven move."""
    branch = "-"
    if git is None:
        branch = "not a Git repository"
    elif git.get("branch"):
        branch = str(git["branch"])
    else:
        branch = "detached or no branch"
    lines = [
        "[Nexus notice: session directory changed]",
        f"Previous directory: {previous}",
        f"Current directory: {current}",
        f"Branch: {branch}",
        (
            "Subsequent model and tool calls resolve paths from the current directory. Earlier messages, the frozen "
            "system prompt and the instructions file loaded at session start are unchanged. Project identity is "
            "unchanged as well."
        ),
        instruction_section,
    ]
    if source == "tool":
        lines.insert(
            5,
            "This result is the forward context for the change. The call that made it kept the previous directory, "
            "and later calls resolve from the current one.",
        )
    return "\n\n".join(lines)


def worktree_text(action: str, data: dict[str, Any]) -> str:
    """The model-visible result of a worktree control call."""
    if action == "list":
        lines = [f"Git worktrees of {data['repository']} (main checkout {data['main']}):"]
        for item in data["worktrees"]:
            flags = ["main"] if item["main"] else []
            if item.get("detached"):
                flags.append("detached")
            if item.get("bare"):
                flags.append("bare")
            if item.get("prunable"):
                flags.append("prunable")
            if item.get("locked"):
                flags.append("locked")
            if not item.get("exists", True):
                flags.append("missing")
            suffix = f" [{' '.join(flags)}]" if flags else ""
            branch = item.get("branch") or "no branch"
            if item.get("dirty"):
                state = f"{item.get('changes', 0)} uncommitted change(s)"
            elif item.get("dirty") is False:
                state = "clean"
            else:
                state = "status unavailable"
            lines.append(f"- {item['path']}{suffix} · branch {branch} · {state}")
        if not data["worktrees"]:
            lines.append("- (none)")
        return "\n".join(lines)
    if action == "create":
        verb = "reused existing" if data.get("reused") else "created"
        return (
            f"{verb} worktree {data['path']} on branch {data.get('branch') or '(no branch)'} "
            f"(repository {data['repository']}).\n"
            f"The session directory is unchanged: {data['session_directory']}."
        )
    if action == "delete":
        return (
            f"deleted worktree {data['path']} (branch {data.get('branch') or '(no branch)'}; "
            f"repository {data['repository']}).\n"
            "The branch was kept. The session directory is unchanged: " + data["session_directory"] + "."
        )
    return str(data)
