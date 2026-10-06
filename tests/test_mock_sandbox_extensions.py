"""Dev sandbox extension seeds: skills, generated mcp.json, seed-on-every-start (CONTEXT_SECTIONS_PLAN §5.2)."""
from __future__ import annotations

import json
import sys

from nexus.devtools.mock.sandbox import ensure_sandbox, generated_seed_files, restore_sandbox, sandbox_path


def test_seeds_skills_and_generated_mcp_in_both_scopes(tmp_path):
    home = tmp_path / "dev"
    root = ensure_sandbox(home)
    for name in ("mock-skill", "code-review", "release-notes", "sql-style"):
        assert (root / ".agents" / "skills" / name / "SKILL.md").is_file()
    assert (root / ".agents" / "skills" / "code-review" / "references" / "examples" / "bad.diff").is_file()
    for name in ("writing-style", "git-hygiene"):
        assert (home / "skills" / name / "SKILL.md").is_file()
    project = json.loads((root / ".agents" / "mcp.json").read_text())["servers"]
    assert set(project) == {"mock-tracker", "mock-broken"} and project["mock-tracker"]["tool_loading"] == "all"
    assert project["mock-tracker"]["command"] == sys.executable
    assert json.loads((home / "mcp.json").read_text())["servers"]["mock-docs"]["tool_loading"] == "search"
    assert ".agents/mcp.json" in (root / ".gitignore").read_text()


def test_new_seeds_reach_an_existing_sandbox_without_a_reset(tmp_path):
    home = tmp_path / "dev"
    root = ensure_sandbox(home)
    (root / ".agents" / "skills" / "code-review" / "SKILL.md").unlink()
    (home / "skills" / "git-hygiene" / "SKILL.md").unlink()
    (root / "src" / "app.py").write_text("edited\n")
    ensure_sandbox(home)
    assert (root / ".agents" / "skills" / "code-review" / "SKILL.md").is_file()
    assert (home / "skills" / "git-hygiene" / "SKILL.md").is_file()
    assert (root / "src" / "app.py").read_text() == "edited\n", "existing files are never overwritten"


def test_generated_mcp_follows_the_interpreter_and_stays_inside_the_dev_home(tmp_path, monkeypatch):
    home = tmp_path / "dev"
    ensure_sandbox(home)
    monkeypatch.setattr(sys, "executable", "/opt/venv/bin/python")
    ensure_sandbox(home)
    assert json.loads((home / "mcp.json").read_text())["servers"]["mock-docs"]["command"] == "/opt/venv/bin/python"
    base = home.resolve()
    assert all(base in path.resolve().parents for path in generated_seed_files(home))


def test_restore_brings_generated_files_back(tmp_path):
    home = tmp_path / "dev"
    root = ensure_sandbox(home)
    restore_sandbox(root, home)
    assert (root / ".agents" / "mcp.json").is_file() and sandbox_path(home) == root
