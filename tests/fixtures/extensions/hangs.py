"""A module that hangs forever during import (killed by quarantine's timeout)."""

SPEC = {
    "name": "HangsFixture",
    "description": "never finishes importing",
    "input_schema": {"type": "object"},
    "bundle": "fs",
}

while True:
    pass
