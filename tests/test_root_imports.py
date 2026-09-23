"""Root package import hygiene (plan section 2.2: import cost matters).

``import nexus`` and ``import nexus.errors`` must not pull the heavy runtime
machinery: httpx, the provider adapters, the runtime, the session layer, the
router, the host/facade, the model layer, or core. Every public contract stays
available lazily through PEP 562 and resolves to its real home on first access.
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
    "nexus.host",
    "nexus.host.facade",
    "nexus.host.daemon",
    "nexus.view",
    "nexus.config",
    "nexus.context",
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


def test_import_nexus_does_not_load_heavy_modules():
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


def test_lazy_root_exports_resolve_to_their_real_homes():
    script = """
        import nexus
        # Eager contracts are present immediately and are dependency-free.
        assert nexus.Event is not None
        assert nexus.SessionBusy is not None
        # Heavy contracts resolve lazily (PEP 562) and match their real homes.
        from nexus.config import Config as C
        from nexus.context import ContextManager as CM
        from nexus.host import HostFacade as H
        from nexus.model.router import ModelRouter as M
        from nexus.runtime import Runtime as R
        from nexus.session import Session as S
        from nexus.session import SessionManager as SM
        from nexus.view import ConversationView as V
        from nexus.view import apply as A
        assert nexus.Config is C
        assert nexus.ContextManager is CM
        assert nexus.HostFacade is H
        assert nexus.ModelRouter is M
        assert nexus.Runtime is R
        assert nexus.Session is S
        assert nexus.SessionManager is SM
        assert nexus.ConversationView is V
        assert nexus.apply is A
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


def test_legacy_codex_exports_are_gone():
    script = """
        import nexus
        for name in ("Agent", "CodexProvider", "Provider", "ProviderError"):
            assert name not in nexus.__all__, name
            try:
                getattr(nexus, name)
            except AttributeError:
                pass
            else:
                raise AssertionError(f"{name} still exported")
        print("ok")
    """
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
