"""Strict layering: the model layer must not import the core layer (or above).

Plan section 2.1 defines a one-way dependency chain L0..L5. The router and
provider contracts live under ``nexus/model`` (L1), so nothing there may import
``nexus.core`` (L2) or any higher manager/UI package.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = REPO_ROOT / "nexus" / "model"

#: Packages the model layer must never reach for.
FORBIDDEN_PREFIXES = (
    "nexus.core",
    "nexus.session",
    "nexus.context",
    "nexus.runtime",
    "nexus.tools",
    "nexus.agent",
    "nexus.cli",
)


def _module_parts(path: Path) -> list[str]:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _resolve_from(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    parts = _module_parts(path)
    is_init = path.name == "__init__.py"
    package = parts if is_init else parts[:-1]
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(_resolve_from(path, node))
    return modules


def _model_files() -> list[Path]:
    return sorted(MODEL_ROOT.rglob("*.py"))


def test_model_layer_has_files_to_check():
    assert _model_files(), "expected model layer modules to lint"


@pytest.mark.parametrize("path", _model_files(), ids=lambda p: p.name)
def test_model_layer_does_not_import_core_or_higher(path: Path):
    violations = sorted(
        module
        for module in _imports(path)
        if module.startswith(FORBIDDEN_PREFIXES)
    )
    assert not violations, f"{path.relative_to(REPO_ROOT)} imports {violations}"


def test_resolved_model_is_defined_in_the_model_layer():
    from nexus.model.provider import ResolvedModel as ModelResolvedModel
    from nexus.model.router import ResolvedModel as RouterResolvedModel

    assert ModelResolvedModel is RouterResolvedModel
    assert ModelResolvedModel.__module__ == "nexus.model.provider"


def test_core_loop_reexports_resolved_model_for_compatibility():
    from nexus.core.loop import ResolvedModel as LoopResolvedModel
    from nexus.model.provider import ResolvedModel as ModelResolvedModel

    assert LoopResolvedModel is ModelResolvedModel
