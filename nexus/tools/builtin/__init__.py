"""Phase 2 and Phase 4 built-in tools.

Registration surface for the ``ToolManager``: each tool module exposes a
validated :class:`~nexus.tools.spec.ToolSpec` and an async ``run(args, ctx)``,
and this package bundles them into ready-to-register ``RegisteredTool`` pairs.
Importing is cheap and side-effect free.

The catalog order is the bundle order declared in
:mod:`nexus.tools.bundles` (``fs``, ``shell``, ``task``, ``meta``, ``ext``),
which keeps ``schemas()`` deterministic. ``Task`` (subagents, Phase 6) is built
per turn by the runtime (its permission key and authority are bound to the live
``SubagentRunner``), so ``BUILTIN_TOOLS`` ships the static ``TodoWrite`` only and
the runtime appends ``Task`` to the iteration catalog.
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
    meta,
    multiedit,
    read,
    skill,
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
from .meta import LIST_EXTENSIONS_SPEC, RELOAD_EXTENSIONS_SPEC, WRITE_TOOL_SPEC
from .multiedit import SPEC as MULTIEDIT_SPEC
from .read import SPEC as READ_SPEC
from .skill import SKILL_SPEC
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

#: Bundle order, matching ``nexus.tools.bundles.bundle_tools("meta")``.
META_SPECS = (RELOAD_EXTENSIONS_SPEC, LIST_EXTENSIONS_SPEC, WRITE_TOOL_SPEC)

META_RUNNERS = MappingProxyType(
    {
        RELOAD_EXTENSIONS_SPEC.name: meta.run_reload_extensions,
        LIST_EXTENSIONS_SPEC.name: meta.run_list_extensions,
        WRITE_TOOL_SPEC.name: meta.run_write_tool,
    }
)

META_TOOLS = tuple(
    RegisteredTool(spec=spec, run=META_RUNNERS[spec.name], origin="builtin")
    for spec in META_SPECS
)

#: Bundle order, matching ``nexus.tools.bundles.bundle_tools("ext")``.
EXT_SPECS = (SKILL_SPEC,)

EXT_RUNNERS = MappingProxyType({SKILL_SPEC.name: skill.run})

EXT_TOOLS = tuple(
    RegisteredTool(spec=spec, run=EXT_RUNNERS[spec.name], origin="builtin")
    for spec in EXT_SPECS
)

#: The full built-in catalog in bundle order (fs, shell, task, meta, ext).
BUILTIN_SPECS = FS_SPECS + SHELL_SPECS + TASK_SPECS + META_SPECS + EXT_SPECS
BUILTIN_TOOLS = FS_TOOLS + SHELL_TOOLS + TASK_TOOLS + META_TOOLS + EXT_TOOLS

__all__ = [
    "BASH_OUTPUT_SPEC",
    "BASH_SPEC",
    "BUILTIN_SPECS",
    "BUILTIN_TOOLS",
    "EDIT_SPEC",
    "EXT_RUNNERS",
    "EXT_SPECS",
    "EXT_TOOLS",
    "FS_RUNNERS",
    "FS_SPECS",
    "FS_TOOLS",
    "GLOB_SPEC",
    "GREP_SPEC",
    "KILL_SHELL_SPEC",
    "LIST_EXTENSIONS_SPEC",
    "LS_SPEC",
    "META_RUNNERS",
    "META_SPECS",
    "META_TOOLS",
    "MULTIEDIT_SPEC",
    "READ_SPEC",
    "RELOAD_EXTENSIONS_SPEC",
    "SHELL_RUNNERS",
    "SHELL_SPECS",
    "SHELL_TOOLS",
    "SKILL_SPEC",
    "TASK_RUNNERS",
    "TASK_SPECS",
    "TASK_TOOLS",
    "TODO_SPEC",
    "WRITE_SPEC",
    "WRITE_TOOL_SPEC",
    "_jobs",
    "bash",
    "bash_output",
    "edit",
    "glob",
    "grep",
    "kill_shell",
    "ls",
    "meta",
    "multiedit",
    "read",
    "skill",
    "todo",
    "write",
]
