"""One-line help for each Settings area, shared by the Textual and native consoles."""
from __future__ import annotations

SETTINGS_HELP: dict[str, str] = {
    "agents": "Build is the default root agent; advisor, task and quick are subagents. "
              "Blank model fields inherit the session model. Fallbacks are tried "
              "in order when the model fails before replying.",
    "tools": "Add or edit Python tools the agent can call. Choose a tool to edit its source; New file creates one.",
    "mcp": "Connect external tools through MCP servers. Edit mcp.json to configure commands, URLs and server options.",
    "skills": "Teach reusable workflows with skills. Choose a SKILL.md to edit its instructions, or create a new skill.",
    "hooks": "Run commands at lifecycle events. Edit hooks.toml to choose when each hook runs and what it executes.",
    "config": "Configure default models, fallbacks, permissions and context limits. Choose the config file to edit; the host validates changes.",
    "soul": "Set instructions included in every conversation. Edit SOUL.md here; start a new session to use updated instructions.",
}

#: Settings areas in Textual's order: ``(key, label)``; a ``None`` key is a heading.
SETTINGS_SECTIONS: tuple[tuple[str | None, str], ...] = (
    (None, "GENERAL"),
    ("appearance", "Appearance"), ("layout", "Layout"), ("keys", "Keyboard"), ("workspace", "Workspace"),
    (None, ""),
    (None, "CONFIGURE"),
    ("providers", "Providers"), ("voice", "Voice"), ("agents", "Agents"), ("tools", "Tools"),
    ("mcp", "MCP servers"), ("skills", "Skills"), ("hooks", "Hooks"), ("config", "Config"), ("soul", "Soul"),
)
