"""Dev-mode sandbox workspace (MOCK_PLAN §7).

Dev mode never runs against the user's real workspace. The CLI swaps the
workspace for a seeded, git-initialised project under the dev home, so the real
tools (read/write/edit/bash/…) execute for real but inside a folder the
permission ``PathGuard`` already confines them to.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

__all__ = [
    "DEFAULT_DEV_HOME",
    "SEED_FILES",
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


def ensure_sandbox(home: Path | str | None = None) -> Path:
    """Create (once) and return the sandbox workspace."""
    root = sandbox_path(home)
    config = root / "nexus.toml"
    if not config.is_file():
        root.mkdir(parents=True, exist_ok=True)
        _seed(root)
    elif config.read_text(encoding="utf-8") != SEED_FILES["nexus.toml"]:
        config.write_text(SEED_FILES["nexus.toml"], encoding="utf-8")  # dev config is ours: keep it current
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
    for relative, content in SEED_FILES.items():
        seeded = target / relative
        if not seeded.is_file():
            seeded.parent.mkdir(parents=True, exist_ok=True)
            seeded.write_text(content, encoding="utf-8")
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
