"""Bounded, read-only Git diff projection for the host (TAUI port §5.3)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..util import redact_secrets

MAX_PATCH_BYTES = 256 * 1024
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^:-]{0,127}\Z")


async def git_diff(workspace: Path, *, staged: bool = False, ref: str = "") -> tuple[str, bool]:
    """Run Git without a shell, limiting time, bytes, and paths to the workspace."""
    if ref and (not _REF.fullmatch(ref) or ".." in ref or "@{" in ref):
        raise ValueError("invalid Git ref")
    args = ["git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--relative"]
    if staged:
        args.append("--cached")
    if ref:
        args.append(ref)
    args.extend(("--", "."))
    process = await asyncio.create_subprocess_exec(
        *args, cwd=workspace, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    data = bytearray()
    truncated = False

    async def collect() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            remaining = MAX_PATCH_BYTES + 1 - len(data)
            data.extend(chunk[:remaining])
            if len(data) > MAX_PATCH_BYTES:
                truncated = True
                process.kill()
                break

    try:
        await asyncio.wait_for(collect(), timeout=5)
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError as exc:
        raise ValueError("Git diff timed out") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode and not truncated:
        raise ValueError("Git diff is unavailable for this workspace")
    return redact_secrets(data[:MAX_PATCH_BYTES].decode("utf-8", "replace")), truncated
