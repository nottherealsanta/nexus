"""A module whose SPEC declares a schema that is not an object schema."""

SPEC = {
    "name": "BadSchemaFixture",
    "description": "input_schema is not an object",
    "input_schema": {"type": "array"},
    "bundle": "fs",
}


async def run(args, ctx):  # noqa: ANN001, ANN201
    return None
