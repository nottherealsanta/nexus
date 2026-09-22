"""A module whose ``run`` is synchronous: the contract requires async run."""

SPEC = {
    "name": "SyncRunFixture",
    "description": "declares a sync run",
    "input_schema": {"type": "object"},
    "bundle": "fs",
}


def run(args, ctx):  # noqa: ANN001, ANN201 - deliberately the wrong shape
    return None
