"""File-loaded providers: ``.nexus/providers/*.py`` (plan section 8).

A new *wire protocol* is a new module in ``nexus/model/providers/`` that passes
the conformance suite. A vendor that already speaks an existing protocol needs
no core change at all: drop a trusted file in ``.nexus/providers/`` (workspace)
or ``~/.nexus/providers/`` (user) and it is loaded beside the built-in
adapters. This is the provider half of the self-extension story, deliberately
the same shape as hot tools.

Loading is **quarantined**: a file that is too large, a symlink, a syntax error,
a forbidden top-level side effect, an import crash, or a missing contract is
recorded as a diagnostic and skipped. One bad file can never stop the runtime
from starting; a workspace file shadows a user file of the same name.

The contract is intentionally tiny. A provider file defines one of:

* ``PROVIDER`` -- one object satisfying :class:`~nexus.model.provider.Provider`;
* ``PROVIDERS`` -- a mapping of provider name to such an object;
* ``build(context)`` -- a callable returning either of the above. It receives a
  :class:`ProviderFileContext` carrying the runtime's already-built transport
  kwargs (so a file can reuse the shared HTTP client) and the loaded config.

Only files whose provider name is not already configured are registered, so a
``nexus.toml`` section always wins over a same-named file.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...errors import ProviderError

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "FileProviderLoader",
    "ProviderFileContext",
    "ProviderFileDiagnostic",
    "ProviderFileResult",
]

#: Same bound the hot-tool loader uses (``ext.max_file_bytes`` default).
DEFAULT_MAX_FILE_BYTES = 262144

#: Top-level calls that are never a legitimate provider declaration.
_FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "input"})

#: ``owner.attr`` calls refused at import time. A provider file declares an
#: object; it does not shell out or touch the network while being loaded.
_FORBIDDEN_ATTRS = frozenset(
    {
        "os.system",
        "os.popen",
        "os.exec",
        "os.execv",
        "os.execve",
        "subprocess.run",
        "subprocess.call",
        "subprocess.Popen",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)

#: Directories, in precedence order, relative to the workspace and the home.
_WORKSPACE_DIR = (".nexus", "providers")
_USER_DIR = (".nexus", "providers")


@dataclass(frozen=True)
class ProviderFileDiagnostic:
    """Why one provider file was quarantined (or loaded)."""

    path: str
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "detail": self.detail}


@dataclass
class ProviderFileResult:
    """The providers a scan discovered, plus a diagnostic per skipped file."""

    providers: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[ProviderFileDiagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diagnostics


@dataclass(frozen=True)
class ProviderFileContext:
    """What a ``build(context)`` factory receives."""

    config: Any = None
    workspace: Path | None = None
    home: Path | None = None
    transport: Mapping[str, Any] = field(default_factory=dict)


def _is_provider(value: object) -> bool:
    """Structural Provider check: a name plus a stream method."""
    try:
        name = value.name  # type: ignore[attr-defined]
        stream = value.stream  # type: ignore[attr-defined]
    except AttributeError:
        return False
    return isinstance(name, str) and bool(name) and callable(stream)


def _top_level_side_effects(tree: ast.Module) -> str | None:
    """Return a description of a forbidden top-level call, or ``None``.

    Only calls that actually execute at *import time* are inspected: an
    expression statement, an assignment value, or a ``with``/``for`` header.
    A call nested inside a function or class body is deferred until (and if) the
    provider is actually used, so it is deliberately not flagged -- the same
    semantics as the extension quarantiner.
    """
    candidates: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.Expr):
            candidates.extend(ast.walk(node.value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = node.value
            if value is not None:
                candidates.extend(ast.walk(value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                candidates.extend(ast.walk(item.context_expr))
                if item.optional_vars is not None:
                    candidates.extend(ast.walk(item.optional_vars))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            candidates.extend(ast.walk(node.iter))
    for call in (node for node in candidates if isinstance(node, ast.Call)):
        func = call.func
        if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
            return func.id
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            qualified = f"{func.value.id}.{func.attr}"
            if qualified in _FORBIDDEN_ATTRS:
                return qualified
    return None


class FileProviderLoader:
    """Scan provider directories, quarantine bad files, load good ones."""

    def __init__(self, *, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> None:
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int):
            raise ProviderError("provider loader: max_file_bytes must be an integer")
        if max_file_bytes < 1:
            raise ProviderError("provider loader: max_file_bytes must be positive")
        self._max_file_bytes = max_file_bytes

    def directories(
        self, workspace: Path, home: Path | None = None
    ) -> list[Path]:
        """Provider directories, workspace first (it shadows the user's).

        ``home`` defaults to the current user's home, so ``~/.nexus/providers/``
        is discovered without the caller having to pass it explicitly.
        """
        found: list[Path] = []
        candidates = [Path(workspace).joinpath(*_WORKSPACE_DIR)]
        user_home = Path.home() if home is None else Path(home)
        candidates.append(user_home.joinpath(*_USER_DIR))
        for directory in candidates:
            if directory.is_dir() and directory not in found:
                found.append(directory)
        return found

    def load(
        self,
        workspace: Path,
        home: Path | None = None,
        *,
        context: ProviderFileContext | None = None,
    ) -> ProviderFileResult:
        """Load every provider file; a workspace name shadows a user name."""
        result = ProviderFileResult()
        seen_names: set[str] = set()
        for directory in self.directories(workspace, home):
            for path in sorted(directory.glob("*.py")):
                providers, diagnostic = self._load_file(path, context)
                if diagnostic is not None:
                    result.diagnostics.append(diagnostic)
                    continue
                for name, provider in providers.items():
                    if name in seen_names:
                        # A higher-precedence directory already provided it.
                        continue
                    seen_names.add(name)
                    result.providers[name] = provider
        return result

    # -- one file ----------------------------------------------------------

    def _load_file(
        self, path: Path, context: ProviderFileContext | None
    ) -> tuple[dict[str, Any], ProviderFileDiagnostic | None]:
        try:
            source, sha = self._read(path)
        except OSError as exc:
            return {}, self._diagnostic(path, "unreadable", str(exc))
        except ValueError as exc:
            return {}, self._diagnostic(path, "oversize", str(exc))
        try:
            tree = ast.parse(source, filename=path.name)
        except SyntaxError as exc:
            return {}, self._diagnostic(path, "syntax", str(exc))
        effect = _top_level_side_effects(tree)
        if effect is not None:
            return {}, self._diagnostic(
                path, "side_effect", f"forbidden top-level call {effect!r}"
            )
        module = self._import(path, source, sha)
        if isinstance(module, ProviderFileDiagnostic):
            return {}, module
        try:
            providers = self._extract(module, context)
        except BaseException as exc:  # noqa: BLE001 - a bad factory is quarantined
            return {}, self._diagnostic(path, "build_failed", _safe(exc))
        finally:
            # Do not retain a hot provider module in ``sys.modules``: a provider
            # object holds its own class references, and an edited file would
            # otherwise leave its previous content-hash module behind on every
            # reload. Popping here keeps repeated runtime builds leak-free.
            sys.modules.pop(_module_name(sha), None)
        if not providers:
            return {}, self._diagnostic(
                path,
                "no_contract",
                "expected PROVIDER, PROVIDERS, or build(context)",
            )
        return providers, None

    def _read(self, path: Path) -> tuple[str, str]:
        if path.is_symlink():
            raise ValueError("refuses to follow a symlink")
        size = path.stat().st_size
        if size > self._max_file_bytes:
            raise ValueError(
                f"{size} bytes exceeds the {self._max_file_bytes}-byte cap"
            )
        source = path.read_text(encoding="utf-8")
        return source, hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _import(
        self, path: Path, source: str, sha: str
    ) -> Any | ProviderFileDiagnostic:
        module_name = _module_name(sha)
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return self._diagnostic(path, "import_failed", "no import spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException as exc:  # noqa: BLE001 - quarantine, not crash
                sys.modules.pop(module_name, None)
                return self._diagnostic(path, "import_failed", _safe(exc))
        except BaseException as exc:  # noqa: BLE001 - a loader failure is quarantined
            return self._diagnostic(path, "import_failed", _safe(exc))
        return module

    @staticmethod
    def _extract(
        module: Any, context: ProviderFileContext | None
    ) -> dict[str, Any]:
        candidates: Any = None
        namespace = vars(module) if module is not None else {}
        builder = namespace.get("build")
        if callable(builder):
            candidates = builder(context or ProviderFileContext())
        elif "PROVIDERS" in namespace:
            candidates = namespace["PROVIDERS"]
        elif "PROVIDER" in namespace:
            candidates = namespace["PROVIDER"]

        providers: dict[str, Any] = {}
        if candidates is None:
            return providers
        if isinstance(candidates, Mapping):
            for name, provider in candidates.items():
                if isinstance(name, str) and name and _is_provider(provider):
                    providers[name] = provider
            return providers
        if _is_provider(candidates):
            providers[str(candidates.name)] = candidates
        return providers

    @staticmethod
    def _diagnostic(path: Path, code: str, detail: str) -> ProviderFileDiagnostic:
        return ProviderFileDiagnostic(
            path=str(path), code=code, detail=_safe_text(detail)
        )


def _module_name(sha: str) -> str:
    """The content-addressed module name a provider file is imported under."""
    return f"nexus_hot_provider_{sha[:16]}"


def _safe(exc: BaseException) -> str:
    return _safe_text(f"{type(exc).__name__}: {exc}")


def _safe_text(value: object, *, limit: int = 300) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit]
