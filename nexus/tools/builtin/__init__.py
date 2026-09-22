"""Phase 2 built-in tools (bundles ``fs``, ``shell``, ``task``).

Registration surface for the ``ToolManager``: each tool module exposes a
validated :class:`~nexus.tools.spec.ToolSpec` and an async ``run(args, ctx)``,
and this package bundles them into ready-to-register ``RegisteredTool`` pairs.
Importing is cheap and side-effect free.

The catalog order is the bundle order declared in
:mod:`nexus.tools.bundles` (``fs`` then ``shell`` then ``task``), which keeps
``schemas()`` deterministic. ``Task`` (subagents) is not implemented until
Phase 6, so the ``task`` bundle currently contributes ``TodoWrite`` only.
"""
from __future__ import annotations

from types import MappingProxyType

from ..spec import RegisteredTool
from . import (
    _jobs,
    bash,
    bash_output,
    edit,
    glob,
    grep,
    kill_shell,
    ls,
    multiedit,
    read,
    todo,
    write,
)
from .bash import SPEC as BASH_SPEC
from .bash_output import SPEC as BASH_OUTPUT_SPEC
from .edit import SPEC as EDIT_SPEC
from .glob import SPEC as GLOB_SPEC
from .grep import SPEC as GREP_SPEC
from .kill_shell import SPEC as KILL_SHELL_SPEC
from .ls import SPEC as LS_SPEC
from .multiedit import SPEC as MULTIEDIT_SPEC
from .read import SPEC as READ_SPEC
from .todo import TODO_SPEC
from .write import SPEC as WRITE_SPEC

#: Bundle order, matching ``nexus.tools.bundles.bundle_tools("fs")``.
FS_SPECS = (
    READ_SPEC,
    WRITE_SPEC,
    EDIT_SPEC,
    MULTIEDIT_SPEC,
    GLOB_SPEC,
    GREP_SPEC,
    LS_SPEC,
)

FS_RUNNERS = MappingProxyType(
    {
        READ_SPEC.name: read.run,
        WRITE_SPEC.name: write.run,
        EDIT_SPEC.name: edit.run,
        MULTIEDIT_SPEC.name: multiedit.run,
        GLOB_SPEC.name: glob.run,
        GREP_SPEC.name: grep.run,
        LS_SPEC.name: ls.run,
    }
)

#: Ready-to-register pairs for the filesystem tools.
FS_TOOLS = tuple(
    RegisteredTool(spec=spec, run=FS_RUNNERS[spec.name], origin="builtin")
    for spec in FS_SPECS
)

#: Bundle order, matching ``nexus.tools.bundles.bundle_tools("shell")``.
SHELL_SPECS = (BASH_SPEC, BASH_OUTPUT_SPEC, KILL_SHELL_SPEC)

SHELL_RUNNERS = MappingProxyType(
    {
        BASH_SPEC.name: bash.run,
        BASH_OUTPUT_SPEC.name: bash_output.run,
        KILL_SHELL_SPEC.name: kill_shell.run,
    }
)

SHELL_TOOLS = tuple(
    RegisteredTool(spec=spec, run=SHELL_RUNNERS[spec.name], origin="builtin")
    for spec in SHELL_SPECS
)

#: Bundle order, matching ``nexus.tools.bundles.bundle_tools("task")`` minus
#: the Phase 6 ``Task`` tool.
TASK_SPECS = (TODO_SPEC,)

TASK_RUNNERS = MappingProxyType({TODO_SPEC.name: todo.run})

TASK_TOOLS = tuple(
    RegisteredTool(spec=spec, run=TASK_RUNNERS[spec.name], origin="builtin")
    for spec in TASK_SPECS
)

#: The full built-in catalog in bundle order (fs, shell, task).
BUILTIN_SPECS = FS_SPECS + SHELL_SPECS + TASK_SPECS
BUILTIN_TOOLS = FS_TOOLS + SHELL_TOOLS + TASK_TOOLS

__all__ = [
    "BASH_OUTPUT_SPEC",
    "BASH_SPEC",
    "BUILTIN_SPECS",
    "BUILTIN_TOOLS",
    "EDIT_SPEC",
    "FS_RUNNERS",
    "FS_SPECS",
    "FS_TOOLS",
    "GLOB_SPEC",
    "GREP_SPEC",
    "KILL_SHELL_SPEC",
    "LS_SPEC",
    "MULTIEDIT_SPEC",
    "READ_SPEC",
    "SHELL_RUNNERS",
    "SHELL_SPECS",
    "SHELL_TOOLS",
    "TASK_RUNNERS",
    "TASK_SPECS",
    "TASK_TOOLS",
    "TODO_SPEC",
    "WRITE_SPEC",
    "_jobs",
    "bash",
    "bash_output",
    "edit",
    "glob",
    "grep",
    "kill_shell",
    "ls",
    "multiedit",
    "read",
    "todo",
    "write",
]
