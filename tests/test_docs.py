"""Docs stay navigable: relative links resolve and every module is mapped.

``docs/`` is the source of truth for agents (AGENTS.md). Two cheap, offline
guards keep it that way: no broken relative link in ``AGENTS.md`` or ``docs/``,
and every Python module under ``nexus/`` has a row in ``docs/module-map.md``.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"
MODULE_MAP = DOCS / "module-map.md"

_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)


def _doc_files() -> list[Path]:
    return [REPO_ROOT / "AGENTS.md", *sorted(DOCS.glob("*.md"))]


def _slug(heading: str) -> str:
    """GitHub-style anchor for a heading."""
    text = re.sub(r"`", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slug(match.group(1)) for match in _HEADING.finditer(path.read_text("utf-8"))}


def _links(path: Path) -> list[str]:
    text = re.sub(r"```.*?```", "", path.read_text("utf-8"), flags=re.DOTALL)
    return [match.group(1) for match in _LINK.finditer(text)]


@pytest.mark.parametrize("path", _doc_files(), ids=lambda p: p.name)
def test_relative_links_resolve(path: Path):
    broken: list[str] = []
    for target in _links(path):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue  # external URL
        if target.startswith("#"):
            if target[1:] not in _anchors(path):
                broken.append(target)
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / unquote(file_part)).resolve()
        if not resolved.exists():
            broken.append(target)
        elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            broken.append(target)
    assert not broken, f"{path.relative_to(REPO_ROOT)} has broken links: {broken}"


def _mapped_modules() -> set[str]:
    package = ""
    mapped: set[str] = set()
    for line in MODULE_MAP.read_text("utf-8").splitlines():
        heading = re.match(r"^### `([^`]+)/`\s*$", line)
        if heading:
            package = heading.group(1)
            continue
        row = re.match(r"^\| `([^`|]+\.py)` \|", line)
        if row and package:
            mapped.add(f"{package}/{row.group(1)}")
    return mapped


def _modules() -> set[str]:
    modules = set()
    for path in (REPO_ROOT / "nexus").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name == "__init__.py" and path.stat().st_size < 50:
            continue  # empty package markers carry no contract
        modules.add(path.relative_to(REPO_ROOT).as_posix())
    return modules


def test_every_module_is_in_the_module_map():
    missing = sorted(_modules() - _mapped_modules())
    assert not missing, f"add rows to docs/module-map.md for: {missing}"


def test_module_map_has_no_stale_rows():
    stale = sorted(
        module for module in _mapped_modules() if not (REPO_ROOT / module).exists()
    )
    assert not stale, f"remove rows from docs/module-map.md for: {stale}"


def test_agents_md_points_at_every_topic_doc():
    text = (REPO_ROOT / "AGENTS.md").read_text("utf-8")
    index = (DOCS / "README.md").read_text("utf-8")
    for doc in sorted(DOCS.glob("*.md")):
        if doc.name == "README.md":
            continue
        assert f"({doc.name})" in index or f"({doc.name}#" in index, (
            f"docs/README.md does not link {doc.name}"
        )
        assert f"docs/{doc.name}" in text, f"AGENTS.md does not link docs/{doc.name}"
