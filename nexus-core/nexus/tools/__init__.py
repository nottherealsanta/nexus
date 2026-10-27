"""The task tools: read, bash, edit (replacement or patch), write, ask, code,
plus bash_output and bash_kill for background bash jobs.

These act on the workspace, except `ask`, which asks the user a question and
returns the answer as data (never a permission grant). Changing Nexus itself
(session, workspace, worktree, model) is a separate control plane and is never
one of these tools (PLAN.md, "Task tools vs. control").
"""
from __future__ import annotations

from .ask import TOOL as ASK
from .base import Tool, ToolContext, ToolError
from .bash import KILL_TOOL as BASH_KILL
from .bash import OUTPUT_TOOL as BASH_OUTPUT
from .bash import TOOL as BASH
from .code import TOOL as CODE
from .edit import TOOL as EDIT
from .read import TOOL as READ
from .write import TOOL as WRITE

DEFAULT_TOOLS: tuple[Tool, ...] = (READ, BASH, EDIT, WRITE, ASK, CODE)
BASH_JOB_TOOLS: tuple[Tool, ...] = (BASH_OUTPUT, BASH_KILL)  # granted with bash for background jobs

__all__ = ["BASH_JOB_TOOLS", "DEFAULT_TOOLS", "Tool", "ToolContext", "ToolError"]
