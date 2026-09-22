"""Reusable provider conformance harness (plan section 9).

Every provider adapter must pass one parametrized suite, offline, against
recorded or generated wire frames. This package is the suite; the pytest
entry point is ``tests/test_provider_conformance.py`` and a human-readable
matrix can be produced with ``python -m tests.provider_conformance``.

Public API::

    from provider_conformance import ADAPTER_SPECS, CASES, run_all

    report = await run_all()
    print(report.format())
    assert report.failed == 0

The harness is deliberately adapter-neutral:

* a *case* describes a logical scenario as normalized ``StreamEvent`` plans and
  transport faults, independent of any wire format;
* an *adapter spec* declares capability tags (``stream``, ``http``,
  ``wire_request``, ...) and constructs a fresh adapter under test;
* the harness skips a case when the adapter lacks a required tag, so the suite
  grows by adding cases and adapters rather than by editing tests.

Production adapters and contracts are imported but never modified: the harness
tests the seam.
"""
from .adapters import (
    ADAPTER_SPECS,
    AnthropicAdapter,
    GeminiAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    ScriptedAdapter,
)
from .cases import CASES, ConformanceCase, Expectation
from .contract import ALL_DIALECTS
from .harness import (
    CaseOutcome,
    CaseResult,
    ConformanceReport,
    assert_case,
    run_all,
    run_case,
    skip_reason,
)

__all__ = [
    "ADAPTER_SPECS",
    "ALL_DIALECTS",
    "CASES",
    "AnthropicAdapter",
    "CaseOutcome",
    "CaseResult",
    "ConformanceCase",
    "ConformanceReport",
    "Expectation",
    "GeminiAdapter",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
    "ScriptedAdapter",
    "assert_case",
    "run_all",
    "run_case",
    "skip_reason",
]
