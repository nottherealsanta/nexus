"""A module that raises during import, before any declaration is reachable."""

raise RuntimeError("boom during import")

SPEC = {
    "name": "ExplodesFixture",
    "description": "never reached",
    "input_schema": {"type": "object"},
    "bundle": "fs",
}
