"""Dev-mode sandbox workspace (MOCK_PLAN §7).

Dev mode never runs against the user's real workspace. The CLI swaps the
workspace for a seeded, git-initialised project under the dev home, so the real
tools (read/write/edit/bash/…) execute for real but inside a folder the
permission ``PathGuard`` already confines them to.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "DEFAULT_DEV_HOME",
    "GLOBAL_SEED_FILES",
    "SEED_FILES",
    "generated_seed_files",
    "dev_home",
    "ensure_sandbox",
    "reset_sandbox",
    "restore_sandbox",
    "sandbox_path",
    "tree_hash",
]

DEFAULT_DEV_HOME = Path("~/.nexus/dev")
_MAX_HASH_FILES = 5_000

SEED_FILES: dict[str, str] = {
    "nexus.toml": (
        "config_version = 2\n\n"
        "[models]\n"
        'default = "mock/hello"\n'
        "offline = true\n\n"
        "[permissions]\n"
        'mode = "allow"\n'
        'ask = ["bash(git push*)"]\n\n'
        "[tools]\n"
        "bash_yield_s = 3\n"  # short window so the bash-wait scenario yields quickly
    ),
    "README.md": (
        "# Mock sandbox\n\n"
        "A throwaway project used by `/mock` scenarios. Everything in here may be\n"
        "rewritten at any time; `/mock clean` restores it.\n"
    ),
    "src/app.py": (
        '"""Tiny demo app used by mock scenarios."""\n\n\n'
        "def greet(name: str) -> str:\n"
        "    # TODO: support greeting several names\n"
        '    return f"hello, {name}"\n\n\n'
        "def total(values: list[int]) -> int:\n"
        "    # TODO: handle an empty list\n"
        "    return sum(values)\n\n\n"
        'if __name__ == "__main__":\n'
        '    print(greet("nexus"))\n'
    ),
    "src/util.py": (
        '"""Helpers."""\n\n\n'
        "def clamp(value: int, low: int, high: int) -> int:\n"
        "    return max(low, min(high, value))\n\n\n"
        "def slug(text: str) -> str:\n"
        '    return "-".join(text.lower().split())\n'
    ),
    "src/config.py": 'DEBUG = False\nTIMEOUT = 30\nRETRIES = 3\n',
    "notes/todo.txt": "- write tests\n- add docs\n- TODO: rename util.slug\n",
    "notes/long.txt": "\n".join(f"line {i:03d}: the quick brown fox jumps over the lazy dog" for i in range(1, 401)) + "\n",
    "tests/test_app.py": (
        "from src.app import greet, total\n\n\n"
        "def test_greet():\n"
        '    assert greet("a") == "hello, a"\n\n\n'
        "def test_total():\n"
        "    assert total([1, 2]) == 3\n"
    ),
    ".agents/skills/mock-skill/SKILL.md": (
        "---\n"
        "name: mock-skill\n"
        "description: A sandbox skill used by the mock extensions scenario.\n"
        "allowed-tools: [read, glob]\n"
        "version: 1\n"
        "---\n\n"
        "# Mock skill\n\nWhen invoked, list the files under `src/` and stop.\n"
    ),
    ".gitignore": ".agents/mcp.json\n",  # generated per machine (sys.executable moves between venvs)
    ".agents/skills/code-review/SKILL.md": (
        "---\nname: code-review\ndescription: Review a diff for correctness and style.\n"
        "allowed-tools: [read, grep, glob]\nversion: 2\n---\n\n"
        "# Code review\n\nReview the change in this order:\n\n"
        "## Checklist\n\n| Area | Question |\n| --- | --- |\n"
        "| Correctness | Does it do what the title says? |\n| Tests | Is the new behaviour covered? |\n"
        "| Style | Does it match the surrounding code? |\n\n"
        "See `references/checklist.md` and `references/examples/bad.diff`.\n\n"
        "```python\ndef review(diff):\n    return [hunk for hunk in diff if hunk.risky]\n```\n"
    ),
    ".agents/skills/code-review/references/checklist.md": "- [ ] logic\n- [ ] tests\n- [ ] naming\n",
    ".agents/skills/code-review/references/examples/bad.diff": "--- a/x.py\n+++ b/x.py\n@@\n-return 1\n+return None  # oops\n",
    ".agents/skills/release-notes/SKILL.md": (
        "---\nname: release-notes\ndescription: Draft release notes from merged pull requests.\n"
        "allowed-tools: [read, bash]\nmodel: mock/hello\nversion: 3\n---\n\n"
        "# Release notes\n\nGroup merged changes under Added, Changed and Fixed.\n"
    ),
    ".agents/skills/sql-style/SKILL.md": (
        "---\nname: sql-style\ndescription: " + "Format and review SQL queries so that keywords are upper case, joins are explicit, "
        "every column is qualified, CTEs are preferred over nested subqueries, and long statements stay readable. " * 3
        + "\n---\n\n# SQL style\n\nUpper-case keywords; one clause per line.\n"
    ),
}

# Global (dev home) skills: the second scope, so both appear in the Skills section.
GLOBAL_SEED_FILES: dict[str, str] = {
    "skills/writing-style/SKILL.md": (
        "---\nname: writing-style\ndescription: Keep prose short, concrete and active.\nversion: 1\n---\n\n"
        "# Writing style\n\nPrefer short sentences. Cut filler words.\n"
    ),
    "skills/git-hygiene/SKILL.md": (
        "---\nname: git-hygiene\ndescription: Small commits with Conventional Commit subjects.\nversion: 1\n---\n\n"
        "# Git hygiene\n\nOne logical change per commit.\n"
    ),
}


def generated_seed_files(home: Path | str | None = None) -> dict[Path, str]:
    """Machine-dependent seed files, keyed by absolute path (rewritten when they differ).

    The dummy MCP servers run with this interpreter, so the commands are absolute.
    """
    base = (Path(home).expanduser() if home is not None else dev_home())

    def server(profile: str, **extra: object) -> dict:
        return {"command": sys.executable, "args": ["-m", "nexus.devtools.mock.mcp_server", "--profile", profile], **extra}

    project = {"servers": {"mock-tracker": server("tracker", tool_loading="all"), "mock-broken": server("broken")}}
    global_ = {"servers": {"mock-docs": server("docs", tool_loading="search")}}
    return {
        sandbox_path(base) / ".agents" / "mcp.json": json.dumps(project, indent=2) + "\n",
        base / "mcp.json": json.dumps(global_, indent=2) + "\n",
    }


def dev_home(environ: dict[str, str] | None = None) -> Path:
    """The isolated dev home: ``$NEXUS_HOME`` when set, else ``~/.nexus/dev``."""
    env = os.environ if environ is None else environ
    override = env.get("NEXUS_HOME")
    return (Path(override) if override else DEFAULT_DEV_HOME).expanduser()


def sandbox_path(home: Path | str | None = None) -> Path:
    return (Path(home).expanduser() if home is not None else dev_home()) / "sandbox" / "workspace"


def _git(root: Path, *args: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Mock", "GIT_AUTHOR_EMAIL": "mock@example.invalid",
        "GIT_COMMITTER_NAME": "Mock", "GIT_COMMITTER_EMAIL": "mock@example.invalid",
    }
    subprocess.run(["git", *args], cwd=root, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def _seed(root: Path) -> None:
    for relative, content in SEED_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    try:
        _git(root, "init", "-q", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "seed")
    except (OSError, subprocess.SubprocessError):  # git missing: still usable
        pass


def _write_missing(files: dict[Path, str]) -> None:
    for target, content in files.items():
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


def _write_generated(home: Path | str | None) -> None:
    """Never touches anything outside the dev home or the sandbox."""
    for target, content in generated_seed_files(home).items():
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


def _seed_extras(root: Path, home: Path | str | None) -> None:
    base = Path(home).expanduser() if home is not None else dev_home()
    _write_missing({root / relative: content for relative, content in SEED_FILES.items()})
    _write_missing({base / relative: content for relative, content in GLOBAL_SEED_FILES.items()})
    _write_generated(home)


def ensure_sandbox(home: Path | str | None = None) -> Path:
    """Create (once) and return the sandbox workspace.

    Seed files that are missing are written on every call (never overwritten), so new
    seeds reach an existing sandbox; generated files follow ``sys.executable``.
    """
    root = sandbox_path(home)
    config = root / "nexus.toml"
    if not config.is_file():
        root.mkdir(parents=True, exist_ok=True)
        _seed(root)
    elif config.read_text(encoding="utf-8") != SEED_FILES["nexus.toml"]:
        config.write_text(SEED_FILES["nexus.toml"], encoding="utf-8")  # dev config is ours: keep it current
    _seed_extras(root, home)
    return root.resolve()


def reset_sandbox(home: Path | str | None = None) -> Path:
    """Delete and re-seed the sandbox. Refuses any path outside the dev home."""
    root = sandbox_path(home)
    base = (Path(home).expanduser() if home is not None else dev_home()).resolve()
    resolved = root.resolve()
    if base not in resolved.parents:
        raise ValueError("refusing to reset a sandbox outside the dev home")
    if resolved.exists():
        shutil.rmtree(resolved)
    return ensure_sandbox(home)


def restore_sandbox(root: Path | str, home: Path | str | None = None) -> bool:
    """Restore a running sandbox to its seeded state *in place*.

    The daemon's working directory is the sandbox, so the directory itself is
    never deleted: tracked files are reset from the seed commit, everything else
    is cleaned, and any missing seed file is rewritten. Refuses any path that is
    not the dev-home sandbox.
    """
    target = Path(root).resolve()
    if target != sandbox_path(home).resolve() or not (target / "nexus.toml").is_file():
        raise ValueError("refusing to restore a directory that is not the dev sandbox")
    try:
        _git(target, "reset", "-q", "--hard")
        _git(target, "clean", "-q", "-fdx")
    except (OSError, subprocess.SubprocessError):
        for entry in target.iterdir():
            if entry.name == ".git":
                continue
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    _seed_extras(target, home)
    return True


def tree_hash(root: Path, *, skip: tuple[str, ...] = (".git",)) -> str:
    """Bounded content hash of a directory tree (containment checks in tests)."""
    digest = hashlib.sha256()
    count = 0
    for path in sorted(Path(root).rglob("*")):
        if any(part in skip for part in path.relative_to(root).parts):
            continue
        if path.is_file() and not path.is_symlink():
            count += 1
            if count > _MAX_HASH_FILES:
                break
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()
