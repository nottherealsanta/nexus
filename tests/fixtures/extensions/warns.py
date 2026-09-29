"""A module with obvious import-time side effects, used to test the warn-list.

It imports a side-effecting module and calls a dangerous function at import
time. It is still importable (the warning is not a refusal), but the diagnosis
must surface both warnings.
"""

import subprocess

SPEC = {
    "name": "WarnyFixture",
    "description": "has import-time side effects",
    "input_schema": {"type": "object"},
    "bundle": "fs",
}


async def run(args, ctx):
    return None


_MARKER = subprocess.getstatusoutput("true")
