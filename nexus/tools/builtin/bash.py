"""Compatibility shim: the ``bash`` tool is now ``shell`` (``shell.py``).

``bash`` and ``Bash`` remain accepted model-facing names; the tool manager maps
them to ``shell`` and translates the renamed arguments. This module keeps the old
import path for one release: ``SPEC`` is the ``shell`` spec and ``run`` accepts
the legacy argument names (``workdir``, ``run_in_background``) before delegating.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from ..names import is_legacy_capped_background, translate_legacy_shell_input
from ..spec import ToolContext, ToolExecutionResult
from .shell import SPEC

__all__ = ["SPEC", "run"]


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    """Run a legacy ``bash`` call: rename old arguments, then run ``shell``."""
    from . import shell

    if isinstance(args, dict):
        if is_legacy_capped_background("bash", args) and dataclasses.is_dataclass(ctx):
            ctx = dataclasses.replace(ctx, legacy_capped_background=True)
        args = translate_legacy_shell_input("bash", args)
    return await shell.run(args, ctx)
