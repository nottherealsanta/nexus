"""Session snapshots: the configuration a session freezes when it is created.

A session does not reload its agent file, its tool descriptions or its
instruction file on later turns. At creation (root or child) the runtime loads
the current agent definitions, resolves the selected agent and its tools,
renders the system prompt once, reads exactly `<session.directory>/AGENTS.md`,
and stores all of it as JSON next to the session (`session_snapshot` in
`nexus/store.py`). Later turns and daemon restarts replay that snapshot, so an
edited, added or deleted agent file and an edited `AGENTS.md` only affect new
sessions.

The snapshot is deliberately serializable: tool descriptions and guidance are
records, not Python callables. The runtime rebinds each stored name to its
execution function through the existing tool registry (`nexus/tools/`).

A session from before snapshots existed is initialized lazily from the current
files. That reconstruction cannot be the exact historical prompt, so it carries
`origin: "reconstructed"` and a visible note that is part of the system prompt.
Once written it is stable like any other snapshot.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = 2
INSTRUCTIONS_FILE = "AGENTS.md"
INSTRUCTIONS_MAX_BYTES = 256 * 1024

RECONSTRUCTED_NOTE = (
    "This session predates stored session snapshots. Its configuration was "
    "reconstructed from the agent files and AGENTS.md as they existed when the "
    "session was first used after the upgrade. It is not a record of the exact "
    "prompt this session used before that point. It is now stored and will not "
    "change on its own; start a new session to pick up file changes."
)


def file_fingerprint(path: str | Path) -> str | None:
    """`sha256:<hex>` for a file's bytes, or None when it cannot be read."""
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def read_instruction_file(directory: str | Path) -> dict[str, Any]:
    """Read exactly `<directory>/AGENTS.md`, recording every outcome explicitly.

    No ancestor or nested search is done. A missing, unreadable or oversized file
    still produces a record, so the prompt can say what happened instead of
    silently omitting the instructions.
    """
    path = Path(directory) / INSTRUCTIONS_FILE
    record: dict[str, Any] = {
        "path": str(path),
        "status": "missing",
        "content": None,
        "bytes": None,
        "sha256": None,
        "error": None,
        "limit": INSTRUCTIONS_MAX_BYTES,
    }
    try:
        # Bound allocation even when the file grows while it is being read.
        with path.open("rb") as source:
            size = source.seek(0, 2)
            source.seek(0)
            raw = source.read(INSTRUCTIONS_MAX_BYTES + 1)
    except FileNotFoundError:
        return record
    except OSError as exc:
        record["status"] = "unreadable"
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record["bytes"] = max(size, len(raw))
    if len(raw) > INSTRUCTIONS_MAX_BYTES:
        record["status"] = "too_large"
        record["error"] = f"file is at least {record['bytes']} bytes; the limit is {INSTRUCTIONS_MAX_BYTES} bytes"
        return record
    record["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        record["content"] = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        record["status"] = "unreadable"
        record["error"] = f"not valid UTF-8: {exc}"
        return record
    record["status"] = "loaded"
    return record


def instruction_section(record: dict[str, Any]) -> str:
    """The labelled, model-visible section for the session's instruction file."""
    head = f"# Instructions file\nPath: {record['path']}\n"
    status = record["status"]
    if status == "loaded":
        return f"{head}Status: loaded ({record['bytes']} bytes)\n\n{record['content']}"
    if status == "missing":
        return (
            f"{head}Status: not found\n"
            "No instructions file was loaded: this path does not exist. Only this exact file is read; "
            "ancestor and nested directories are not searched."
        )
    if status == "too_large":
        return (
            f"{head}Status: too large ({record['bytes']} bytes; limit {record['limit']} bytes)\n"
            f"No instructions were loaded. {record['error']}."
        )
    return f"{head}Status: unreadable\nNo instructions were loaded. {record['error']}"


def compose_prompt(agent_prompt: str, instruction_file: dict[str, Any], *, notice: str | None = None) -> str:
    """The frozen provider `instructions`: agent prompt, instruction file, and any note."""
    prompt = f"{agent_prompt}\n\n{instruction_section(instruction_file)}"
    if notice:
        prompt += f"\n\n# Snapshot note\n{notice}"
    return prompt


def agent_record(agent: Any) -> dict[str, Any]:
    """Serializable agent configuration. No callables, and the body is kept verbatim."""
    return {
        "name": agent.name,
        "description": agent.description,
        "source": agent.source,
        "path": agent.path,
        "fingerprint": file_fingerprint(agent.path),
        "mode": agent.mode,
        "model": agent.model,
        "effort": agent.effort,
        "tools": list(agent.tools) if agent.tools is not None else None,
        "control": list(agent.control),
        "steps": agent.steps,
        "prompt": agent.prompt,
    }


def agent_summary(agent: Any) -> dict[str, Any]:
    """What a parent's `session` tool needs to offer a child agent."""
    return {
        "name": agent.name,
        "description": agent.description,
        "mode": agent.mode,
        "source": agent.source,
        "path": agent.path,
        "fingerprint": file_fingerprint(agent.path),
    }


def tool_record(tool: Any) -> dict[str, Any]:
    """A tool's request schema and system-prompt guidance, as plain JSON."""
    return {
        "name": tool.spec.name,
        "description": tool.spec.description,
        "parameters": tool.spec.parameters,
        "guidance": tool.guidance,
    }


def summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The inspectable, bounded snapshot view returned with a session."""
    agent = snapshot["agent"]
    file = snapshot["instruction_file"]
    workspace = snapshot.get("workspace") or {}
    current_file = snapshot.get("current_instruction_file") or file
    data: dict[str, Any] = {
        "version": snapshot["version"],
        "origin": snapshot["origin"],
        "created": snapshot["created"],
        "directory": snapshot["directory"],
        "agent": {
            "name": agent["name"],
            "source": agent["source"],
            "path": agent["path"],
            "fingerprint": agent["fingerprint"],
        },
        "instructions": {
            "path": file["path"],
            "status": file["status"],
            "bytes": file["bytes"],
            "sha256": file["sha256"],
        },
        "current_instructions": {
            "path": current_file["path"],
            "status": current_file["status"],
            "bytes": current_file["bytes"],
            "sha256": current_file["sha256"],
        },
        "workspace": {
            "entry": bool(workspace.get("entry")),
            "active": bool(workspace.get("activated")),
        },
        "directory_changes": len(snapshot.get("directory_changes") or []),
        "messaging": {"active": bool((snapshot.get("messaging") or {}).get("activated"))},
        "granted_tools": list(snapshot["granted_tools"]),
        "child_agents": snapshot["child_agents"],
    }
    if snapshot.get("notice"):
        data["notice"] = snapshot["notice"]
    return data
