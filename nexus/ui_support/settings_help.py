"""One-line help for each Settings area, shared by the Textual and native consoles."""
from __future__ import annotations

SETTINGS_HELP: dict[str, str] = {
    "agents": "Build is the default root agent; advisor, task and quick are subagents. "
              "Blank model fields inherit the session model. Fallbacks are tried "
              "in order when the model fails before replying.",
    "tools": "Python tools loaded from <scope>/tools.",
    "mcp": "MCP servers from mcp.json (mcpServers).",
    "skills": "Skills from <scope>/skills/<name>/SKILL.md.",
    "hooks": "Lifecycle hooks from hooks.toml.",
    "config": "Nexus configuration: models, fallbacks, permissions, context and more.",
    "soul": "Instructions added to every conversation (SOUL.md).",
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
