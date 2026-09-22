"""A module that hard-exits the interpreter during import.

``os._exit`` bypasses ``except`` handlers and ``atexit`` hooks, so it is the
worst case for an in-process import: it would take the whole harness down. The
isolated subprocess contains it.
"""

import os

SPEC = {
    "name": "HardExitFixture",
    "description": "kills the interpreter at import time",
    "input_schema": {"type": "object"},
    "bundle": "fs",
}

os._exit(7)
