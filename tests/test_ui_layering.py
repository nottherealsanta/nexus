"""Layering: terminal UI surfaces import downward only (PLAN section 14.11).

"Encapsulation is enforced, not requested." The same AST walk that keeps
``nexus/model/**`` out of ``nexus.core`` here keeps every module under the UI
client out of ``nexus.runtime``, ``nexus.session``, ``nexus.model``,
``nexus.tools``, and ``nexus.core``. A surface may import ``nexus.host``,
``nexus.view``, ``nexus.events``, the standard library, and nothing else —
Textual/Rich imports are permitted only in the first-class TUI package.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = REPO_ROOT / "nexus" / "ui"

#: The pure-client surface this phase owns. ``ui/native.py`` is the retired
#: pre-Phase-8 adapter and is deliberately out of scope until the root CLI is
#: rewired.
CLIENT_FILES = sorted((UI_ROOT / "cli").rglob("*.py")) + [UI_ROOT / "jsonl.py", UI_ROOT / "tui" / "app.py"]
TUI_FILES = sorted((UI_ROOT / "tui").rglob("*.py")) + [UI_ROOT / "turn_stream.py"]

#: Host protocol clients and pure terminal projections sit beside (not inside)
#: the Textual/CLI package and are valid UI dependencies.
ALLOWED_NEXUS_PREFIXES = (
    "nexus.host", "nexus.view", "nexus.events", "nexus.ui",
    "nexus.client", "nexus.host_support", "nexus.ui_support",
)

#: Only the first-class TUI is permitted to import these presentation packages.
ALLOWED_THIRD_PARTY: tuple[str, ...] = ("textual_diff_view",)
TEXTUAL_SUPPORT_FILES = {
    "tui_widgets.py", "tui_panels.py", "tui_list.py", "tui_context_header.py",
    "tui_archived.py", "tui_diff.py", "tui_settings.py", "tui_setup.py",
    "tui_providers.py", "tui_voice.py", "tui_models.py", "tui_speech.py",
}

#: The interpreter's standard-library module names, for a precise allow-list.
STDLIB = set(sys.stdlib_module_names)

#: The layers a surface must never import, spelled out for a readable failure.
FORBIDDEN_PREFIXES = (
    "nexus.runtime",
    "nexus.core",
    "nexus.session",
    "nexus.model",
    "nexus.tools",
    "nexus.context",
    "nexus.agents",
    "nexus.mcp",
    "nexus.skills",
    "nexus.hooks",
    "nexus.ext",
    "nexus.config",
    "nexus.cli.",
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
    package = parts if path.name == "__init__.py" else parts[:-1]
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


def test_client_files_exist():
    assert CLIENT_FILES, "expected UI client modules to lint"
    names = {path.name for path in CLIENT_FILES}
    assert "client.py" in names
    assert "app.py" in names


@pytest.mark.parametrize("path", CLIENT_FILES + TUI_FILES, ids=lambda p: str(p.relative_to(UI_ROOT)))
def test_ui_client_imports_only_allowed_layers(path: Path):
    for module in _imports(path):
        top = module.split(".")[0]
        if top == "nexus":
            assert module.startswith(ALLOWED_NEXUS_PREFIXES), (
                f"{path.relative_to(REPO_ROOT)} imports {module}"
            )
        elif top in {"textual", "rich"}:
            assert path in TUI_FILES or (path.parent.name == "ui_support" and path.name in TEXTUAL_SUPPORT_FILES), (
                f"{path.relative_to(REPO_ROOT)} imports {module} outside ui/tui"
            )
        elif top not in ALLOWED_THIRD_PARTY:
            # Everything else must be the standard library.
            assert top in STDLIB, (
                f"{path.relative_to(REPO_ROOT)} imports non-stdlib {module}"
            )


@pytest.mark.parametrize("path", CLIENT_FILES + TUI_FILES, ids=lambda p: str(p.relative_to(UI_ROOT)))
def test_ui_client_never_imports_the_runtime_or_managers(path: Path):
    violations = sorted(
        module for module in _imports(path) if module.startswith(FORBIDDEN_PREFIXES)
    )
    assert not violations, f"{path.relative_to(REPO_ROOT)} imports {violations}"


def test_ui_client_does_not_import_msgspec():
    for path in CLIENT_FILES:
        assert "msgspec" not in _imports(path), f"{path.name} imports msgspec"


def test_client_imports_do_not_eagerly_load_tui():
    script = (
        "import sys\n"
        "import nexus.ui.cli\n"
        "import nexus.ui.jsonl\n"
        "assert 'textual' not in sys.modules\n"
        "assert not any(name.startswith('textual') for name in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_prompt_toolkit_is_absent_from_runtime_and_lockfile():
    root = REPO_ROOT
    assert "prompt_toolkit" not in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "prompt-toolkit" not in (root / "uv.lock").read_text(encoding="utf-8")
