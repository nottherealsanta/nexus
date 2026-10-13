"""nexus/live is a manager: it must not import the session, core, runtime, host or UI layers."""
from __future__ import annotations

import pytest

from tests.test_layering import REPO_ROOT, _imports

LIVE_ROOT = REPO_ROOT / "nexus" / "live"
FORBIDDEN = (
    "nexus.core", "nexus.session", "nexus.runtime", "nexus.host", "nexus.host_support",
    "nexus.ui", "nexus.ui_support", "nexus.client", "nexus.tools", "nexus.cli",
)


@pytest.mark.parametrize("path", sorted(LIVE_ROOT.glob("*.py")), ids=lambda p: p.name)
def test_live_does_not_import_higher_layers(path):
    violations = sorted(m for m in _imports(path) if m.startswith(FORBIDDEN))
    assert not violations, f"{path.name} imports {violations}"


def test_core_loop_knows_nothing_about_live():
    text = (REPO_ROOT / "nexus" / "core" / "loop.py").read_text(encoding="utf-8")
    assert "nexus.live" not in text and "from ..live" not in text
