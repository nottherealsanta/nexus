"""Root package import hygiene (plan section 2.2: import cost matters).

``import nexus`` and ``import nexus.errors`` must not pull the heavy Phase 1
machinery: httpx, the Anthropic adapter, the runtime, the session layer, the
router, or core. Phase 1 exports stay available lazily.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

HEAVY_MODULES = [
    "httpx",
    "fcntl",
    "nexus.runtime",
    "nexus.session",
    "nexus.session.lock",
    "nexus.model",
    "nexus.model.providers",
    "nexus.model.providers.anthropic",
    "nexus.model.router",
    "nexus.core",
    "nexus.core.loop",
]


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_import_nexus_does_not_load_heavy_phase1_modules():
    script = f"""
        import sys
        before = set(sys.modules)
        import nexus
        import nexus.errors
        after = set(sys.modules)
        heavy = {HEAVY_MODULES!r}
        newly_loaded = sorted(m for m in heavy if m not in before and m in after)
        assert not newly_loaded, newly_loaded
        # fcntl is preloaded by CPython's pathlib in this interpreter, so assert
        # the nexus module that imports it (on use) is not loaded at import time.
        assert "nexus.session.lock" not in sys.modules
        print("ok")
    """
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_lazy_root_exports_resolve_and_legacy_exports_stay_eager():
    script = """
        import nexus
        # Legacy exports are present immediately.
        assert nexus.Agent is not None
        assert nexus.Config is not None
        assert nexus.Provider is not None
        assert nexus.CodexProvider is not None
        assert nexus.ProviderError is not None
        assert nexus.SessionBusy is not None
        # Phase 1 exports resolve lazily (PEP 562) and match their real homes.
        from nexus.context import ContextManager as C
        from nexus.model.router import ModelRouter as M
        from nexus.runtime import Runtime as R
        from nexus.session import Session as S
        from nexus.session import SessionManager as SM
        assert nexus.ContextManager is C
        assert nexus.ModelRouter is M
        assert nexus.Runtime is R
        assert nexus.Session is S
        assert nexus.SessionManager is SM
        # Unknown attributes still raise AttributeError.
        try:
            nexus.DoesNotExist
        except AttributeError:
            pass
        else:
            raise AssertionError("expected AttributeError")
        print("ok")
    """
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
