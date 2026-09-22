"""A hook that raises at import time; the manager must isolate it."""

from __future__ import annotations


def _never():
    return None


raise RuntimeError("import-time boom")
