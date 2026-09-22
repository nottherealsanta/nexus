"""Print the full provider conformance matrix.

Usage (offline, no credentials)::

    python -m tests.provider_conformance

Exits non-zero if any case fails or errors.
"""
from __future__ import annotations

import asyncio

from .harness import run_all


def main() -> int:
    report = asyncio.run(run_all())
    print(report.format())
    return 1 if (report.failed or report.errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
