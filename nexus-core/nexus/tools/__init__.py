"""The four task tools: read, bash, edit (replacement or patch), write.

These act on the workspace. Changing Nexus itself (session, workspace,
worktree, model) is a separate control plane and is never one of these tools
(PLAN.md, "Task tools vs. control").
"""
from __future__ import annotations

from .base import Tool, ToolContext, ToolError
from .bash import TOOL as BASH
from .edit import TOOL as EDIT
from .read import TOOL as READ
from .write import TOOL as WRITE

DEFAULT_TOOLS: tuple[Tool, ...] = (READ, BASH, EDIT, WRITE)

__all__ = ["DEFAULT_TOOLS", "Tool", "ToolContext", "ToolError"]
