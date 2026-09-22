"""A module that raises at import with a credential embedded in the error.

The quarantine diagnostic must never echo the credential. ``ToolSpecError`` and
similar allow-listed types sanitize their message; arbitrary exceptions are
reduced to the type name.
"""

import builtins

SPEC = {
    "name": "SecretLeakFixture",
    "description": "tries to leak a secret through an import-time error",
    "input_schema": {"type": "object"},
    "bundle": "fs",
}

raise builtins.RuntimeError(
    "failed to authenticate with api_key=sk-supersecretvalue1234567890"
)
