"""Install skills from an ``npx skills`` source into a Settings scope (plan 04).

Stage, then import: ``npx`` runs in a fresh temporary directory and copies the
skills into ``.claude/skills`` there; Nexus validates each staged skill itself
and copies the survivors into ``<scope>/skills/<name>/`` through the Settings
path policy. The destination, conflict handling and trash stay ours.

Safety stance (this runs third-party code at the user's explicit request):

* argv only, never a shell; the source is validated and cannot look like an option;
* the child gets the allow-listed environment of ``build_child_env`` (no Nexus or
  provider credentials), its own process group, no stdin, a timeout and bounded
  output; the whole group is killed on timeout or cancellation;
* only the known staged location is read, never searched recursively;
* a skill with a symlink or special file, too many or too large files, or a
  ``SKILL.md`` the strict frontmatter parser rejects is refused on its own;
* a top-level ``tools/`` folder (executable code) and file permission bits are
  never imported;
* a skill is copied to a staging folder under the scope's ``trash/`` (outside the
  ``skills/`` discovery path) and renamed into place; an overwritten folder moves
  whole to settings trash first and is put back if the rename fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..errors import ConfigError, NexusError
from ..model.providers.opencode import build_child_env
from ..skills.errors import SkillError
from ..skills.frontmatter import read_skill_file, validate_skill_name
from ..util import redact_secrets
from .settings_scope import settings_root, settings_target

#: Pinned so a hijacked ``latest`` is not run implicitly. Metadata checked on the
#: web 2026-10-20 (npm ``skills``, vercel-labs/skills); a live run is NOT verified.
PACKAGE = "skills@1.7.2"
#: ``npx skills add --copy -a claude-code`` writes ``<cwd>/.claude/skills/<name>/``.
AGENT = "claude-code"
STAGED_PARTS = (".claude", "skills")
TIMEOUT_SECONDS = 120.0
MAX_OUTPUT_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 2000
MAX_SOURCE_CHARS = 200
MAX_SKILLS = 64
MAX_FILES = 256
MAX_DIRS = 64
MAX_DEPTH = 8
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024

_OWNER_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\s]")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
#: Proxies and registry settings the child may need; everything else is dropped.
_INHERIT = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")

_installing = False


class _Reject(Exception):
    """One skill cannot be imported; the message is the user-facing reason."""


def validate_source(source: object) -> str:
    """``owner/repo``, an ``https://`` URL or an absolute existing directory."""
    if not isinstance(source, str) or not source or len(source) > MAX_SOURCE_CHARS:
        raise ConfigError("skill source must be 1-200 characters")
    if source.startswith("-") or _CONTROL.search(source):
        raise ConfigError("skill source must not contain whitespace or control characters or start with '-'")
    if _OWNER_REPO.fullmatch(source):
        return source
    if source.startswith("https://"):
        rest = source[len("https://"):]
        host = rest.split("/", 1)[0]
        if not host or "@" in host or "\\" in source:
            raise ConfigError("skill source URL must not contain credentials")
        return source
    if os.path.isabs(source):
        if not os.path.isdir(source):
            raise ConfigError("skill source directory does not exist")
        return source
    raise ConfigError("skill source must be owner/repo, an https:// URL or an absolute directory")


def validate_names(names: object) -> list[str]:
    if not isinstance(names, (list, tuple)) or len(names) > MAX_SKILLS:
        raise ConfigError("too many skill names")
    seen: list[str] = []
    for name in names:
        try:
            validate_skill_name(name)
        except SkillError as exc:
            raise ConfigError(f"invalid skill name {str(name)[:40]!r}: {exc}") from None
        if name not in seen:
            seen.append(name)
    return seen


def _clean(text: bytes) -> str:
    value = text.decode("utf-8", errors="replace")
    value = _UNSAFE.sub("", _ANSI.sub("", value))
    return redact_secrets(value).strip()[-MAX_MESSAGE_CHARS:]


def _argv(npx: str, source: str, names: list[str]) -> list[str]:
    argv = [npx, "--yes", PACKAGE, "add", source, "--copy", "-y", "-a", AGENT]
    for name in names:
        argv += ["-s", name]
    return argv


async def _drain(stream: asyncio.StreamReader | None) -> bytes:
    """Read to EOF but keep only the first ``MAX_OUTPUT_BYTES`` (the pipe must not fill)."""
    kept = bytearray()
    while stream is not None:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if len(kept) < MAX_OUTPUT_BYTES:
            kept += chunk[: MAX_OUTPUT_BYTES - len(kept)]
    return bytes(kept)


def _kill(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(ProcessLookupError):
        process.kill()


async def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[str, int | None, str]:
    """Run ``argv``; returns ``(state, code, output)`` with state ``ok | timeout``."""
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    readers = asyncio.gather(_drain(process.stdout), _drain(process.stderr))
    try:
        out, err = await asyncio.wait_for(asyncio.shield(readers), TIMEOUT_SECONDS)
        code = await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        _kill(process)
        readers.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await process.wait()
        return "timeout", None, ""
    except BaseException:  # cancellation: never leave the group running
        _kill(process)
        readers.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await process.wait()
        raise
    return "ok", code, _clean(out + b"\n" + err)


def _scan(skill: Path) -> list[tuple[Path, str, int]]:
    """Regular files of one staged skill as ``(path, relative, size)``; raises ``_Reject``."""
    files: list[tuple[Path, str, int]] = []
    total = dirs = 0
    pending = [(skill, "", 0)]
    while pending:
        folder, prefix, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise _Reject("directories are nested too deeply")
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                rel = f"{prefix}{entry.name}"
                if entry.is_symlink():
                    raise _Reject(f"contains a symlink ({rel})")
                if entry.is_dir(follow_symlinks=False):
                    if not prefix and entry.name == "tools":
                        continue  # executable code is never imported (reported by the caller)
                    dirs += 1
                    if dirs > MAX_DIRS:
                        raise _Reject("too many directories")
                    pending.append((Path(entry.path), rel + "/", depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    if size > MAX_FILE_BYTES:
                        raise _Reject(f"{rel} is larger than {MAX_FILE_BYTES // 1024} KiB")
                    total += size
                    if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                        raise _Reject("too many or too large files")
                    files.append((Path(entry.path), rel, size))
                else:
                    raise _Reject(f"contains a special file ({rel})")
    return files


def _copy_tree(files: list[tuple[Path, str, int]], destination: Path) -> None:
    destination.mkdir(mode=0o755)
    for source, rel, _ in files:
        target = destination / rel
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:  # copies bytes only, never mode bits
            shutil.copyfileobj(src, out)


def _plain_dir(path: Path, label: str) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ConfigError(f"{label} is not a plain directory")
    return path


def _inside(root: Path, *parts: str) -> Path:
    """``root/parts`` created if needed; no component may be a symlink or escape ``root``."""
    current = root
    for part in parts:
        current = _plain_dir(current / part, f"settings path {part!r}")
    current.mkdir(parents=True, exist_ok=True)
    if not current.resolve().is_relative_to(root.resolve()):
        raise ConfigError("settings path escapes its scope")
    return current


def _install_one(root: Path, scope: str, name: str, staged: Path, files: list, overwrite: bool) -> str:
    """Copy ``staged`` to ``<root>/skills/<name>``; returns ``installed`` or ``conflict``."""
    skills = _inside(root, "skills")
    destination = skills / name
    exists = destination.is_symlink() or destination.exists()
    if exists and not overwrite:
        return "conflict"
    scratch = _inside(root, "trash", "skills-staging") / uuid.uuid4().hex
    try:
        scratch.mkdir(mode=0o700)  # the unique folder itself does not exist yet
        _copy_tree(files, scratch / name)
        trash_entry: Path | None = None
        if exists:
            trash_entry = _inside(root, "trash", "settings") / uuid.uuid4().hex
            trash_entry.mkdir(mode=0o700)
            (trash_entry / "meta.json").write_text(json.dumps({
                "rel_path": f"skills/{name}", "scope": scope, "category": "skills", "id": name, "kind": "directory",
            }), encoding="utf-8")
            os.replace(destination, trash_entry / "item")
        try:
            os.replace(scratch / name, destination)
        except OSError:
            if trash_entry is not None:
                with suppress(OSError):
                    os.replace(trash_entry / "item", destination)
                    shutil.rmtree(trash_entry, ignore_errors=True)
            raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return "installed"


def _import_staged(runtime: object, scope: str, stage: Path, wanted: list[str], overwrite: bool) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"installed": [], "conflicts": [], "skipped": [], "failed": []}
    root, _ = settings_root(runtime, scope)
    _check_global_root(runtime, scope, root)
    if root.is_symlink():
        raise ConfigError("settings root is a symlink")
    base = stage.joinpath(*STAGED_PARTS)
    if base.is_symlink() or not base.is_dir():
        return result
    with os.scandir(base) as entries:
        found = sorted(entry.name for entry in entries)[: MAX_SKILLS + 1]
    if len(found) > MAX_SKILLS:
        raise ConfigError(f"source offers more than {MAX_SKILLS} skills; name the ones you want")
    for name in wanted:
        if name not in found:
            result["failed"].append(f"{name}: not offered by the source")
    for name in found:
        if wanted and name not in wanted:
            continue
        directory = base / name
        try:
            if directory.is_symlink() or not directory.is_dir():
                raise _Reject("is not a plain directory")
            settings_target(runtime, scope, "skills", name)  # name grammar, symlink and containment policy
            skill_md = directory / "SKILL.md"
            if skill_md.is_symlink() or not skill_md.is_file():
                raise _Reject("has no SKILL.md")
            parsed = read_skill_file(skill_md).parsed
            if parsed.name != name:
                raise _Reject(f"SKILL.md name {parsed.name!r} does not match its folder")
            files = _scan(directory)
            if (directory / "tools").is_dir():
                result["skipped"].append(f"{name}: tools/ not imported (executable code)")
            outcome = _install_one(root, scope, name, directory, files, overwrite)
            result["installed" if outcome == "installed" else "conflicts"].append(name)
        except (_Reject, SkillError, ConfigError, OSError) as exc:
            reason = str(exc) if isinstance(exc, (_Reject, SkillError, ConfigError)) else f"{type(exc).__name__}: {exc}"
            result["failed"].append(f"{name}: {_clean(reason.encode())[:300]}")
    return result


def _status(done: dict[str, list[str]]) -> str:
    if done["installed"]:
        return "partial" if done["failed"] or done["conflicts"] else "installed"
    if done["conflicts"]:
        return "conflict"
    return "failed" if done["failed"] else "nothing"


def _check_global_root(runtime: object, scope: str, root: Path) -> None:
    """A global install must land where skill discovery reads (``$NEXUS_HOME`` vs ``~/.nexus``)."""
    if scope != "global":
        return
    from ..config.paths import nexus_home
    try:
        same = nexus_home(getattr(runtime, "_home", None)).resolve() == root.resolve()
    except (OSError, TypeError, ValueError):
        same = False
    if not same:
        raise ConfigError("the host reads global skills from a different folder than Settings writes (NEXUS_HOME is set); "
                          "unset NEXUS_HOME or copy the skill into $NEXUS_HOME/skills by hand")


async def install(runtime: object, scope: str, source: str, names: list[str] | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Run the whole install; returns the ``SkillsInstallResult`` fields."""
    global _installing
    source = validate_source(source)
    wanted = validate_names(names or [])
    root, _ = settings_root(runtime, scope)  # fail early on an unavailable project scope
    _check_global_root(runtime, scope, root)
    env = build_child_env(inherit_env=_INHERIT, env={
        "CI": "1", "NO_COLOR": "1", "DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
        "npm_config_update_notifier": "false", "npm_config_fund": "false", "npm_config_audit": "false",
    })
    npx = shutil.which("npx", path=env.get("PATH"))
    if npx is None:
        return {"status": "unavailable", "message": "npx was not found on PATH (Node.js 22.20+ is required); nothing was changed."}
    if _installing:
        return {"status": "busy", "message": "Another skill install is running; wait for it to finish."}
    _installing = True
    stage = Path(tempfile.mkdtemp(prefix="nexus-skills-"))
    try:
        state, code, output = await _run(_argv(npx, source, wanted), stage, env)
        if state == "timeout":
            return {"status": "timeout", "message": f"npx skills did not finish within {int(TIMEOUT_SECONDS)} s and was stopped; nothing was changed."}
        if code != 0:
            return {"status": "failed", "message": f"npx skills exited with code {code}; nothing was changed.\n{output}".strip()}
        done = await asyncio.to_thread(_import_staged, runtime, scope, stage, wanted, overwrite)
    finally:
        _installing = False
        shutil.rmtree(stage, ignore_errors=True)
    reload_failed: list[str] = []
    if done["installed"]:
        reload = getattr(getattr(runtime, "extensions", None), "reload", None)
        if callable(reload):
            try:
                report = await reload(trigger="settings")
                reload_failed = [str(row) for row in (getattr(report, "failed", ()) or ())]
            except NexusError as exc:
                reload_failed = [str(exc)[:300]]
    status = _status(done)
    message = "" if status == "installed" else (output if status in {"nothing", "failed"} else "")
    return {"status": status, **done, "reload_failed": reload_failed, "message": message}


async def dispatch_skills_install(command: Any, runtime: object) -> Any:
    from ..host import protocol as p
    result = await install(runtime, command.scope, command.source, list(command.skills), command.overwrite)
    return p.SkillsInstallResult(**result)


__all__ = ["PACKAGE", "dispatch_skills_install", "install", "validate_names", "validate_source"]
