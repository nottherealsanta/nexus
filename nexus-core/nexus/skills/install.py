"""`/learn`: copy skills from a Git repository or a local folder into a skills folder.

No npx, no package manager: a source is resolved, fetched with a shallow
`git clone` (or read in place when it is local), every folder holding a
`SKILL.md` is found, and the chosen ones are copied. Nothing in a skill is run.

Accepted sources, with or without a leading `npx skills add` (and its `-y`,
`-g`, `-a <agent>` options, which are ignored; `--skill <name>` and `-s` select):

    https://github.com/owner/repo            [https://github.com/owner/repo/tree/<ref>/<path>]
    owner/repo                               owner/repo@skill-name
    owner/repo/path/to/skills                git@host:owner/repo.git, https://host/x.git
    ./folder   ../folder   /abs/folder   ~/folder   file:///abs/folder

A bare `a/b` is a GitHub repository, never a local folder; local folders need a
`.`, `/` or `~` prefix. Copies skip `.git`, symbolic links and anything past the
size bounds, and an existing skill is left alone unless `overwrite` is set.
Adapted from old Nexus `nexus/host_support/skills_install.py`, which ran
`npx skills`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import frontmatter, global_dir, load

CLONE_TIMEOUT = 120.0
MAX_FILES = 2000
MAX_BYTES = 20 * 1024 * 1024
MAX_DEPTH = 5
_SKIP = {".git", "node_modules", "__pycache__"}
_OWNER_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+\Z")
_GITHUB_PATH = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9._-]+?)(?:\.git)?(/.*)?\Z")


class LearnError(ValueError):
    """The source cannot be used; the message says what to change."""


@dataclass(frozen=True, slots=True)
class Source:
    url: str | None = None  # a clone URL, or None for a local folder
    path: Path | None = None
    ref: str | None = None
    subpath: str = ""
    skills: tuple[str, ...] = ()
    scope: str | None = None  # "global" when -g was given
    label: str = ""


@dataclass
class Found:
    name: str
    description: str
    directory: Path
    warnings: list[str] = field(default_factory=list)


def parse(text: str) -> Source:
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise LearnError(f"cannot read that: {exc}") from exc
    if tokens[:2] == ["npx", "skills"] or (tokens[:1] == ["npx"] and len(tokens) > 1 and tokens[1].startswith("skills@")):
        tokens = tokens[2:]
    elif tokens[:1] == ["skills"]:
        tokens = tokens[1:]
    if tokens[:1] in (["add"], ["install"], ["a"], ["i"]):
        tokens = tokens[1:]
    names: list[str] = []
    scope = None
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("--skill", "-s"):
            i += 1
            while i < len(tokens) and not tokens[i].startswith("-"):
                names.append(tokens[i])
                i += 1
            continue
        if token.startswith("--skill="):
            names.append(token.split("=", 1)[1])
        elif token in ("-g", "--global"):
            scope = "global"
        elif token in ("-y", "--yes", "--all", "--full-depth", "--copy"):
            pass
        elif token in ("-a", "--agent"):
            i += 1
            while i < len(tokens) and not tokens[i].startswith("-"):
                i += 1
            continue
        elif token.startswith("-"):
            raise LearnError(f"option {token} is not supported; use `--skill <name>` to pick one skill")
        else:
            rest.append(token)
        i += 1
    if len(rest) != 1:
        raise LearnError("give one source: owner/repo, a GitHub URL, or a folder (./path)")
    return _source(rest[0], tuple(names), scope)


def _source(spec: str, names: tuple[str, ...], scope: str | None) -> Source:
    if spec.startswith(("./", "../", "/", "~", "file://")) or spec in (".", ".."):
        path = Path(spec.removeprefix("file://")).expanduser()
        return Source(path=path, skills=names, scope=scope, label=str(path))
    if spec.startswith("-"):
        raise LearnError("a source cannot start with '-'")
    if spec.startswith(("git@", "ssh://", "git://")) or (spec.startswith("https://") and not _github(spec)):
        return Source(url=spec, skills=names, scope=scope, label=spec)
    if spec.startswith(("https://github.com/", "http://github.com/", "https://www.github.com/")):
        match = _GITHUB_PATH.fullmatch(spec.split("github.com/", 1)[1].split("#")[0].split("?")[0].rstrip("/"))
        if not match:
            raise LearnError("that does not look like a GitHub repository URL")
        owner, repo, tail = match.groups()
        ref, sub = None, ""
        if tail:
            parts = [p for p in tail.split("/") if p]
            if parts[:1] in (["tree"], ["blob"]) and len(parts) >= 2:
                ref, sub = parts[1], "/".join(parts[2:])
            else:
                sub = "/".join(parts)
        return Source(url=f"https://github.com/{owner}/{repo}.git", ref=ref, subpath=sub, skills=names, scope=scope,
                      label=f"{owner}/{repo}")
    if spec.startswith("http://"):
        raise LearnError("use https:// for remote sources")
    at = ""
    if "@" in spec:
        spec, _, at = spec.partition("@")
        if not at:
            raise LearnError("name the skill after '@', as in owner/repo@skill")
        names = (*names, at)
    match = _GITHUB_PATH.fullmatch(spec)
    if not match:
        raise LearnError(f"cannot tell what {spec!r} is; use owner/repo, a URL, or ./folder")
    owner, repo, tail = match.groups()
    return Source(url=f"https://github.com/{owner}/{repo}.git", subpath=(tail or "").strip("/"), skills=names,
                  scope=scope, label=f"{owner}/{repo}")


def _github(url: str) -> bool:
    return url.startswith(("https://github.com/", "https://www.github.com/"))


async def _clone(source: Source, into: Path) -> None:
    assert source.url
    cmd = ["git", "clone", "--depth", "1", "--quiet", "--no-tags"]
    if source.ref:
        cmd += ["--branch", source.ref]
    cmd += ["--", source.url, str(into)]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SSH_AUTH_SOCK", "LANG", "TMPDIR")}
    env |= {"GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:ssh:git", "GIT_ASKPASS": "true"}
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True)
    except FileNotFoundError as exc:
        raise LearnError("git is not installed, so a remote source cannot be fetched") from exc
    try:
        _, err = await asyncio.wait_for(proc.communicate(), CLONE_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise LearnError(f"fetching {source.label} took longer than {CLONE_TIMEOUT:g} s") from None
    if proc.returncode:
        reason = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["git failed"]
        raise LearnError(f"could not fetch {source.label}: {reason[0][:200]}")


def find(root: Path) -> tuple[list[Found], list[dict[str, str]]]:
    """Every folder under `root` (itself included) that holds a SKILL.md, and the ones that are unusable."""
    found: list[Found] = []
    problems: list[dict[str, str]] = []

    def walk(directory: Path, depth: int) -> None:
        marker = directory / "SKILL.md"
        if marker.is_file() and not marker.is_symlink():
            try:
                skill, _ = load(marker, "global")
                found.append(Found(skill.name, skill.description, directory, list(skill.warnings)))
            except (OSError, ValueError) as exc:
                problems.append({"path": str(marker.relative_to(root)) if marker != root else "SKILL.md",
                                 "error": str(exc).splitlines()[0]})
            return
        if depth >= MAX_DEPTH:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir() and not p.is_symlink() and p.name not in _SKIP)
        except OSError:
            return
        for child in children:
            walk(child, depth + 1)

    walk(root, 0)
    return found, problems


def _copy(source: Path, target: Path) -> int:
    """Copy a skill folder without `.git`, links or special files; bounded in files and bytes."""
    files = total = 0
    for current, dirs, names in os.walk(source):
        dirs[:] = [d for d in dirs if d not in _SKIP and not (Path(current) / d).is_symlink()]
        relative = Path(current).relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        for name in names:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            files += 1
            total += path.stat().st_size
            if files > MAX_FILES or total > MAX_BYTES:
                raise LearnError(f"{source.name} is larger than {MAX_FILES} files or {MAX_BYTES // 1024 // 1024} MiB")
            shutil.copy2(path, target / relative / name)
    return files


def destination(scope: str, project_path: str | None) -> Path:
    if scope == "global":
        return global_dir()
    if scope == "project" and project_path:
        return Path(project_path) / ".agents" / "skills"
    raise LearnError("scope must be 'project' (with a project) or 'global'")


async def learn(source: Source, dest: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Fetch, find and copy. Returns `{installed, skipped, problems, available}`; raises LearnError when nothing could be installed."""
    with tempfile.TemporaryDirectory(prefix="nexus-learn-") as tmp:
        if source.path is not None:
            root = source.path.resolve()
            if not root.is_dir():
                raise LearnError(f"{source.path} is not a folder")
        else:
            root = Path(tmp, "repo")
            await _clone(source, root)
            root = root.resolve()
        base = root
        if source.subpath:
            base = (root / source.subpath).resolve()
            if not base.is_dir() or not (base == root or root in base.parents):
                raise LearnError(f"{source.subpath!r} is not a folder in {source.label}")
        found, problems = await asyncio.to_thread(find, base)
        if not found:
            raise LearnError(f"no SKILL.md found in {source.label}" + (f" ({problems[0]['error']})" if problems else ""))
        available = [f.name for f in found]
        chosen = found
        if source.skills:
            wanted = set(source.skills)
            chosen = [f for f in found if f.name in wanted or f.directory.name in wanted]
            missing = wanted - {n for f in chosen for n in (f.name, f.directory.name)}
            if missing or not chosen:
                raise LearnError(f"no skill named {', '.join(sorted(missing or wanted))} in {source.label}; "
                                 f"it has: {', '.join(available)}")
        installed: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        dest.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        for item in chosen:
            name = item.name
            target = dest / name
            if name in seen:
                skipped.append({"name": name, "reason": "another skill in the source has the same name"})
                continue
            seen.add(name)
            if target.exists() and not overwrite:
                skipped.append({"name": name, "reason": f"already installed at {target}; run it again with overwrite"})
                continue
            staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=dest))
            try:
                count = await asyncio.to_thread(_copy, item.directory, staging)
                if target.exists():
                    shutil.rmtree(target)
                staging.rename(target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            installed.append({"name": name, "description": frontmatter.sanitize(item.description, 200),
                              "path": str(target), "files": count, "warnings": item.warnings})
        if not installed:
            raise LearnError("; ".join(f"{s['name']}: {s['reason']}" for s in skipped))
        return {"installed": installed, "skipped": skipped, "problems": problems, "available": available,
                "source": source.label}
