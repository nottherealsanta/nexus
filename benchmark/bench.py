"""Small, standalone agent benchmark harness.

Run from the repository with ``python benchmark/bench.py --help``. Runtime
workspaces and logs live below the ignored ``artifacts/benchmark`` directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any, Self

REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / "artifacts"
ROOT = ARTIFACTS / "benchmark"
MARKER = ".benchmark-owned"
MARKER_CONTENT = b"nexus-benchmark-owned-v1\n"
SCENARIOS = ("file-edit", "shell-command")
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 600
OUTPUT_FILE = "result.txt"
SHELL_COMMAND = "echo shell-ok > result.txt; echo BENCHMARK_BASH_RAN"
SHELL_OUTPUT_MARKER = "BENCHMARK_BASH_RAN"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class BenchError(Exception):
    """A safe, user-facing benchmark error."""


def _say(message: str, *, error: bool = False) -> None:
    print(message, file=sys.stderr if error else sys.stdout)


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _require_plain_dir(path: Path, *, create: bool = False) -> None:
    if _is_symlink(path):
        raise BenchError(f"Refusing symlinked path: {path}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if not create:
            raise BenchError(f"Not set up: {path} does not exist")
        path.mkdir()
        mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        raise BenchError(f"Expected a real directory: {path}")


def _require_marker(directory: Path, *, create: bool = False) -> None:
    marker = directory / MARKER
    if _is_symlink(marker):
        raise BenchError(f"Refusing symlinked ownership marker: {marker}")
    try:
        mode = marker.lstat().st_mode
    except FileNotFoundError:
        if not create:
            raise BenchError(f"Refusing to use unowned path: {directory}")
        try:
            fd = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(MARKER_CONTENT)
        except FileExistsError:
            pass
        mode = marker.lstat().st_mode
    if not stat.S_ISREG(mode) or _read_regular(marker, max_bytes=128) != MARKER_CONTENT:
        raise BenchError(f"Ownership marker is invalid: {marker}")


def _prepare_root() -> None:
    _require_plain_dir(ARTIFACTS, create=True)
    existed = ROOT.exists() or _is_symlink(ROOT)
    _require_plain_dir(ROOT, create=not existed)
    _require_marker(ROOT, create=not existed)


class RootLock:
    """An exclusive lock shared by setup/run/check/reset."""

    def __init__(self) -> None:
        self.path = ROOT / ".lock"
        self.handle: Any = None

    def __enter__(self) -> Self:
        if _is_symlink(self.path):
            raise BenchError(f"Refusing symlinked lock: {self.path}")
        try:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            self.handle = os.fdopen(fd, "w", encoding="utf-8")
        except FileExistsError as exc:
            detail = "another benchmark command may be running"
            try:
                detail += (
                    " (lock contents: "
                    f"{_read_regular(self.path, max_bytes=256).decode('utf-8').strip()})"
                )
            except OSError:
                pass
            raise BenchError(
                f"Benchmark is locked: {detail}; remove {self.path} only if stale"
            ) from exc
        self.handle.write(f"pid={os.getpid()} started={time.time():.0f}\n")
        self.handle.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            self.handle.close()
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _scenario(value: str) -> str:
    if value not in SCENARIOS:
        raise BenchError(f"Unknown scenario {value!r}; choose: {', '.join(SCENARIOS)}")
    return value


def _scenario_dir(name: str) -> Path:
    return ROOT / _scenario(name)


def _workspace(name: str) -> Path:
    return _scenario_dir(name) / "workspace"


def _safe_owned_scenario(
    name: str, *, require_workspace: bool = True
) -> tuple[Path, Path]:
    scenario_dir = _scenario_dir(name)
    _require_plain_dir(scenario_dir)
    _require_marker(scenario_dir)
    workspace = _workspace(name)
    _require_plain_dir(workspace)
    _require_marker(workspace)
    if require_workspace:
        for path in (
            workspace / "nexus.toml",
            workspace / ".nexus",
            workspace / ".nexus" / "agents",
            workspace / ".nexus" / "agents" / "general.md",
            *(workspace / filename for filename in _fixed_files(name)),
        ):
            if _is_symlink(path):
                raise BenchError(f"Refusing symlinked scenario path: {path}")
        if not (workspace / "nexus.toml").is_file():
            raise BenchError(f"Scenario {name!r} is incomplete; run setup {name}")
        _require_plain_dir(workspace / ".nexus")
        _require_plain_dir(workspace / ".nexus" / "agents")
    return scenario_dir, workspace


def _fixed_files(name: str) -> dict[str, bytes]:
    source = Path(__file__).resolve().parent / "scenarios" / name / "workspace"
    if _is_symlink(source):
        raise BenchError(f"Refusing symlinked seed directory: {source}")
    names = ("input.txt",) if name == "file-edit" else ("instructions.txt",)
    files: dict[str, bytes] = {}
    for filename in names:
        path = source / filename
        if _is_symlink(path) or not path.is_file():
            raise BenchError(f"Missing or unsafe scenario seed file: {path}")
        files[filename] = _read_regular(path)
    return files


def _expected(name: str) -> bytes:
    path = (
        Path(__file__).resolve().parent / "scenarios" / name / "expected" / OUTPUT_FILE
    )
    if _is_symlink(path) or not path.is_file():
        raise BenchError(f"Missing or unsafe expected output file: {path}")
    return _read_regular(path)


def _allow_rules(name: str, workspace: Path, *, allow_shell: bool = False) -> list[str]:
    source = "input.txt" if name == "file-edit" else "instructions.txt"
    read_target = str((workspace / source).resolve(strict=False))
    rules = [
        f"read({json.dumps(read_target, ensure_ascii=False)})",
    ]
    if name == "file-edit":
        target = str((workspace / OUTPUT_FILE).resolve(strict=False))
        rules.append(f"write({json.dumps(target, ensure_ascii=False)})")
    elif allow_shell:
        rules.append(f"bash({json.dumps(SHELL_COMMAND, ensure_ascii=False)})")
    return rules


def _write_workspace_config(
    name: str, workspace: Path, *, allow_shell: bool = False
) -> None:
    from nexus.config import Config

    rules = _allow_rules(name, workspace, allow_shell=allow_shell)
    configured = Config.load(REPO)
    if not configured.source:
        raise BenchError(
            "no repository Nexus config found; configure a default model before setup"
        )
    source = Path(configured.source)
    if _is_symlink(source) or not source.is_file():
        raise BenchError(f"configured Nexus config is missing or symlinked: {source}")
    config = _read_regular(source).decode("utf-8")
    try:
        doc = tomllib.loads(config)
    except tomllib.TOMLDecodeError as exc:
        raise BenchError(f"cannot copy configured Nexus TOML: {exc}") from exc
    _reject_literal_credentials(doc)
    _reject_literal_provider_env(doc)
    if "permissions" in doc and not isinstance(doc["permissions"], dict):
        raise BenchError("configured [permissions] must be a TOML table")
    if any(
        key in doc
        for key in ("executable", "sandbox", "timeout_seconds", "context_chars")
    ) or (isinstance(doc.get("model"), str)):
        raise BenchError(
            "benchmark workspaces require a sectioned Nexus config (config_version = 2); "
            "migrate the configured provider/model settings before setup"
        )

    # The workspace's scalar mode overrides inherited `mode=allow`; its local
    # allowlist is intentionally exact. Layered user/project grants are checked
    # again against the effective config by _preflight_policy before a run.
    permissions = doc.get("permissions", {})
    deny = permissions.get("deny", [])
    read_denyroots = permissions.get("read_denyroots", [])
    if not all(isinstance(values, list) for values in (deny, read_denyroots)):
        raise BenchError("configured permissions.deny/read_denyroots must be lists")
    section = (
        "[permissions]\n"
        'mode = "deny"\n'
        'on_unattended = "deny"\n'
        f"allow = {json.dumps(rules, ensure_ascii=False)}\n"
        "ask = []\n"
        f"deny = {json.dumps(deny, ensure_ascii=False)}\n"
        'write_roots = ["./"]\n'
        f"read_denyroots = {json.dumps(read_denyroots, ensure_ascii=False)}\n"
    )
    lines = config.splitlines(keepends=True)
    section_start = next(
        (index for index, line in enumerate(lines) if line.strip() == "[permissions]"),
        None,
    )
    if section_start is None:
        config = config.rstrip() + "\n\n" + section
    else:
        section_end = next(
            (
                index
                for index in range(section_start + 1, len(lines))
                if lines[index].lstrip().startswith("[")
            ),
            len(lines),
        )
        lines[section_start:section_end] = [section]
        config = "".join(lines)
    try:
        tomllib.loads(config)
    except tomllib.TOMLDecodeError as exc:
        raise BenchError(f"generated benchmark config is invalid: {exc}") from exc
    path = workspace / "nexus.toml"
    _write_regular(path, config.encode("utf-8"))


_SECRET_CONFIG_KEYS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "credential",
)


def _reject_literal_credentials(value: object, key: str = "") -> None:
    """Never place literal credential values in a generated workspace config."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            _reject_literal_credentials(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            _reject_literal_credentials(child, key)
    elif (
        isinstance(value, str)
        and any(part in key.lower() for part in _SECRET_CONFIG_KEYS)
        and value
        and not (value.startswith("${env:") and value.endswith("}"))
    ):
        raise BenchError(
            f"configured {key} contains a literal credential and cannot be copied into "
            "benchmark artifacts; replace it with a ${env:VARIABLE} reference "
            "or use Nexus's credential store"
        )


def _reject_literal_provider_env(doc: dict[str, Any]) -> None:
    providers = doc.get("providers", {})
    if not isinstance(providers, dict):
        return
    for provider_name, provider in providers.items():
        if not isinstance(provider, dict) or not isinstance(provider.get("env"), dict):
            continue
        for key, value in provider["env"].items():
            is_credential = any(
                part in str(key).lower() for part in _SECRET_CONFIG_KEYS
            )
            if (
                is_credential
                and isinstance(value, str)
                and value
                and not (value.startswith("${env:") and value.endswith("}"))
            ):
                raise BenchError(
                    f"configured providers.{provider_name}.env.{key} is a literal value "
                    "and cannot be copied into benchmark artifacts; use a ${env:VARIABLE} "
                    "reference or move credentials to Nexus's credential store"
                )


def _write_root_agent(workspace: Path) -> None:
    """Keep the built-in general agent on the configured workspace model."""
    source = REPO / "nexus" / "agents" / "data" / "general.md"
    if _is_symlink(source) or not source.is_file():
        raise BenchError(
            f"built-in general agent seed is missing or symlinked: {source}"
        )
    text = _read_regular(source).decode("utf-8")
    text, count = (
        text.replace("model: medium", "model: inherit", 1),
        text.count("model: medium"),
    )
    if count != 1:
        raise BenchError(
            "built-in general agent seed has an unexpected model declaration"
        )
    agent_dir = workspace / ".nexus" / "agents"
    _require_plain_dir(workspace / ".nexus", create=True)
    _require_plain_dir(agent_dir, create=True)
    _write_regular(agent_dir / "general.md", text.encode("utf-8"))


def _write_regular(path: Path, content: bytes) -> None:
    if _is_symlink(path):
        raise BenchError(f"Refusing to write through symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise BenchError(f"cannot safely open file for writing {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BenchError(f"Refusing to replace non-regular file: {path}")
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
    finally:
        if fd >= 0:
            os.close(fd)


def _read_regular(path: Path, *, max_bytes: int | None = None) -> bytes:
    if _is_symlink(path):
        raise BenchError(f"Refusing to read symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BenchError(f"cannot safely open file {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BenchError(f"Expected a regular file: {path}")
        if max_bytes is not None and info.st_size > max_bytes:
            raise BenchError(f"File exceeds the {max_bytes}-byte limit: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read(max_bytes + 1 if max_bytes is not None else -1)
    finally:
        if fd >= 0:
            os.close(fd)


def _remove_owned_scenario(name: str) -> bool:
    artifacts_fd = _open_dir_anchored(ARTIFACTS)
    try:
        try:
            root_fd = os.open("benchmark", _directory_flags(), dir_fd=artifacts_fd)
        except OSError as exc:
            raise BenchError(f"Refusing unsafe benchmark root {ROOT}: {exc}") from exc
        root_info = os.fstat(root_fd)
        path_info = os.stat("benchmark", dir_fd=artifacts_fd, follow_symlinks=False)
        if (root_info.st_dev, root_info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise BenchError(f"Benchmark root changed while opening: {ROOT}")
        if not _marker_at(root_fd):
            raise BenchError(f"Refusing to reset unowned benchmark root: {ROOT}")
        if name not in SCENARIOS:
            raise BenchError(f"Refusing non-fixed scenario path: {name}")
        try:
            scenario_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(scenario_stat.st_mode):
            raise BenchError(
                f"Refusing non-directory or symlinked scenario: {ROOT / name}"
            )
        scenario_fd = os.open(name, _directory_flags(), dir_fd=root_fd)
        try:
            opened = os.fstat(scenario_fd)
            if (opened.st_dev, opened.st_ino) != (
                scenario_stat.st_dev,
                scenario_stat.st_ino,
            ):
                raise BenchError(f"Scenario path changed while opening: {ROOT / name}")
            if not _marker_at(scenario_fd):
                raise BenchError(f"Refusing to remove unowned path: {ROOT / name}")
            _assert_anchored_entry(root_fd, name, scenario_stat)
            _validate_contents_fd(scenario_fd, ROOT / name)
            current_root = os.stat(
                "benchmark", dir_fd=artifacts_fd, follow_symlinks=False
            )
            if (current_root.st_dev, current_root.st_ino) != (
                root_info.st_dev,
                root_info.st_ino,
            ):
                raise BenchError(f"Benchmark root changed during reset: {ROOT}")
            _remove_contents_fd(scenario_fd, ROOT / name)
        finally:
            os.close(scenario_fd)
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            scenario_stat.st_dev,
            scenario_stat.st_ino,
        ):
            raise BenchError(f"Scenario path changed during reset: {ROOT / name}")
        _assert_anchored_entry(root_fd, name, scenario_stat)
        os.rmdir(name, dir_fd=root_fd)
        return True
    except OSError as exc:
        raise BenchError(f"Could not safely reset {ROOT / name}: {exc}") from exc
    finally:
        os.close(artifacts_fd)
        if "root_fd" in locals():
            os.close(root_fd)


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_dir_anchored(path: Path) -> int:
    """Open each absolute path component with no-follow dir_fd traversal."""
    if not path.is_absolute():
        raise BenchError(f"Expected an absolute directory path: {path}")
    fd = os.open(path.anchor, _directory_flags())
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise BenchError(f"Refusing unsafe directory {path}: {exc}") from exc


def _marker_at(directory_fd: int) -> bool:
    try:
        info = os.stat(MARKER, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return False
        fd = os.open(
            MARKER,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(fd)
            return (
                stat.S_ISREG(opened.st_mode)
                and (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino)
                and os.read(fd, 129) == MARKER_CONTENT
            )
        finally:
            os.close(fd)
    except FileNotFoundError:
        return False


def _remove_contents_fd(directory_fd: int, display: Path) -> None:
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        path = display / name
        if stat.S_ISLNK(info.st_mode):
            raise BenchError(f"Refusing to remove symlink: {path}")
        _assert_anchored_entry(directory_fd, name, info)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise BenchError(f"Directory changed while resetting: {path}")
                _remove_contents_fd(child_fd, path)
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise BenchError(f"Directory changed during reset: {path}")
            _assert_anchored_entry(directory_fd, name, info)
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(info.st_mode):
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise BenchError(f"File changed during reset: {path}")
            _assert_anchored_entry(directory_fd, name, info)
            os.unlink(name, dir_fd=directory_fd)
        else:
            raise BenchError(f"Refusing to remove special file: {path}")


def _validate_contents_fd(directory_fd: int, display: Path) -> None:
    """Check the full tree before deletion, anchored to already-open directories."""
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        path = display / name
        if stat.S_ISDIR(info.st_mode):
            _assert_anchored_entry(directory_fd, name, info)
            child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise BenchError(f"Directory changed while checking reset: {path}")
                _validate_contents_fd(child_fd, path)
            finally:
                os.close(child_fd)
        elif not stat.S_ISREG(info.st_mode):
            raise BenchError(f"Refusing symlink or special file in scenario: {path}")


def _assert_anchored_entry(
    directory_fd: int, name: str, expected: os.stat_result
) -> None:
    """Where supported, verify entry identity immediately before mutation."""
    if not hasattr(os, "O_PATH"):
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise BenchError(f"Path changed while resetting: {name}")
        return
    try:
        fd = os.open(
            name, os.O_PATH | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
        )
    except OSError as exc:
        raise BenchError(f"Path changed while resetting: {name}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise BenchError(f"Path changed while resetting: {name}")
    finally:
        os.close(fd)


def command_list(_: argparse.Namespace) -> int:
    print("Available scenarios:")
    print("  file-edit      Change input.txt from color=blue to color=green")
    print("  shell-command  Run the fixed, narrow command that writes result.txt")
    print(f"Artifacts: {ROOT}")
    return 0


def command_setup(args: argparse.Namespace) -> int:
    names = SCENARIOS if args.scenario is None else (_scenario(args.scenario),)
    _prepare_root()
    with RootLock():
        for name in names:
            _remove_owned_scenario(name)
            directory = _scenario_dir(name)
            directory.mkdir()
            _require_marker(directory, create=True)
            workspace = _workspace(name)
            workspace.mkdir()
            _require_marker(workspace, create=True)
            for filename, content in _fixed_files(name).items():
                _write_regular(workspace / filename, content)
            _write_workspace_config(name, workspace)
            _write_root_agent(workspace)
            runs = directory / "run"
            runs.mkdir()
            _require_marker(runs, create=True)
            print(f"Set up {name}: {workspace}")
        print(
            f"Runtime config keeps the configured home/provider; generated workspaces are under {ROOT}."
        )
    return 0


def _preflight_policy(name: str, workspace: Path, *, allow_shell: bool) -> str | None:
    """Check the effective policy without changing user/global configuration."""
    from nexus.config import Config
    from nexus.tools.builtin.bash import SPEC as BASH_SPEC
    from nexus.tools.builtin.read import SPEC as READ_SPEC
    from nexus.tools.builtin.write import SPEC as WRITE_SPEC
    from nexus.tools.permissions import Outcome, PermissionEngine
    from nexus.tools.spec import ToolCall

    if name == "shell-command" and not allow_shell:
        return "shell-command requires --allow-shell; no shell rule was added"
    if name == "shell-command":
        try:
            _write_workspace_config(name, workspace, allow_shell=True)
        except BenchError as exc:
            return str(exc)

    try:
        config = Config.load(workspace)
        permissions = getattr(config.v2, "permissions", None)
        if permissions is None:
            return "effective config has no v2 permissions section; cannot verify unattended policy"
        expected_allow = _allow_rules(name, workspace, allow_shell=allow_shell)
        if permissions.mode != "deny":
            return (
                "effective permissions.mode must be 'deny' for benchmark runs; "
                "permissive inherited defaults are not accepted"
            )
        if permissions.on_unattended != "deny":
            return (
                "effective permissions.on_unattended must be 'deny' for benchmark runs"
            )
        if set(permissions.allow) != set(expected_allow):
            return (
                "effective permissions.allow must contain only the benchmark's exact "
                "minimum rules; inherited broad or additional grants are not accepted"
            )
        if permissions.ask:
            return (
                "effective permissions.ask must be empty for unattended benchmark runs"
            )
        engine = PermissionEngine.from_config(permissions, workspace=workspace)
        if engine.path_guard is None or engine.path_guard.write_roots != (
            workspace.resolve(),
        ):
            return "effective permissions.write_roots must be limited to the benchmark workspace"
        source = "input.txt" if name == "file-edit" else "instructions.txt"
        calls = [
            (
                ToolCall(
                    "benchmark-read",
                    "read",
                    {"path": str((workspace / source).resolve(strict=False))},
                ),
                READ_SPEC,
            )
        ]
        if name == "file-edit":
            calls.append(
                (
                    ToolCall(
                        "benchmark-write",
                        "write",
                        {
                            "path": str(
                                (workspace / OUTPUT_FILE).resolve(strict=False)
                            ),
                            "content": "",
                        },
                    ),
                    WRITE_SPEC,
                )
            )
        else:
            calls.append(
                (
                    ToolCall("benchmark-bash", "bash", {"command": SHELL_COMMAND}),
                    BASH_SPEC,
                )
            )
        results = [engine.evaluate(call, spec, attended=False) for call, spec in calls]
        forbidden = [
            (
                ToolCall(
                    "benchmark-forbidden-bash",
                    "bash",
                    {"command": "echo not-the-benchmark"},
                ),
                BASH_SPEC,
            ),
            (
                ToolCall(
                    "benchmark-forbidden-write",
                    "write",
                    {
                        "path": str(
                            (workspace / "forbidden.txt").resolve(strict=False)
                        ),
                        "content": "",
                    },
                ),
                WRITE_SPEC,
            ),
            (
                ToolCall(
                    "benchmark-forbidden-read",
                    "read",
                    {"path": str((workspace / "forbidden.txt").resolve(strict=False))},
                ),
                READ_SPEC,
            ),
        ]
        negative_results = [
            engine.evaluate(call, spec, attended=False) for call, spec in forbidden
        ]
    except Exception as exc:  # noqa: BLE001 - report an actionable config/policy issue
        return f"could not verify effective permissions: {exc}"
    for index, result in enumerate(results):
        if result.outcome is not Outcome.ALLOW:
            return (
                f"effective unattended policy does not allow tool {result.call.name} "
                f"for {name} (outcome={result.outcome.value}, reason={result.reason}); "
                "no global permission was changed"
            )
        expected_rule = expected_allow[index]
        if (
            result.code != "allow"
            or result.rule is None
            or result.rule.raw != expected_rule
        ):
            return f"required tool {result.call.name} was not allowed by its exact benchmark rule"
    for result in negative_results:
        if result.outcome is Outcome.ALLOW:
            return f"effective unattended policy unexpectedly allows forbidden tool {result.call.name}"
    return None


def _parse_events(text: str) -> tuple[list[dict[str, Any]], str | None]:
    events = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return [], f"invalid JSONL event at line {number}"
        if isinstance(value, dict):
            events.append(value)
    return events, None


def _read_events(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if _is_symlink(path):
        return [], f"refusing symlinked event log: {path}"
    try:
        text = _read_regular(path).decode("utf-8")
    except (OSError, BenchError) as exc:
        return [], f"cannot read event log: {exc}"
    return _parse_events(text)


def _redact_runtime_secrets(text: str) -> str:
    """Best-effort redact JSONL string values without changing event structure."""
    from nexus.util import redact_secrets

    values: set[str] = set()
    for key, value in os.environ.items():
        if (
            any(
                part in key.lower()
                for part in (
                    "secret",
                    "token",
                    "password",
                    "api_key",
                    "apikey",
                    "authorization",
                    "credential",
                )
            )
            and len(value) >= 8
        ):
            values.add(value)

    def collect_credentials(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect_credentials(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                collect_credentials(child, key)
        elif (
            isinstance(value, str)
            and value
            and any(
                part in key.lower()
                for part in (
                    "secret",
                    "token",
                    "password",
                    "api_key",
                    "apikey",
                    "authorization",
                    "credential",
                )
            )
        ):
            if value.startswith("${env:") and value.endswith("}"):
                value = os.environ.get(value[6:-1], "")
            if len(value) >= 8:
                values.add(value)

    credential_file = Path.home() / ".nexus" / "credentials.json"
    if not _is_symlink(credential_file) and credential_file.is_file():
        try:
            credentials = json.loads(_read_regular(credential_file).decode("utf-8"))
        except (OSError, json.JSONDecodeError, BenchError):
            credentials = None
        collect_credentials(credentials)

    config_paths = (
        REPO / "nexus.toml",
        Path.home() / ".nexus" / "config.toml",
        *(ROOT / name / "workspace" / "nexus.toml" for name in SCENARIOS),
    )
    for config_path in config_paths:
        if _is_symlink(config_path) or not config_path.is_file():
            continue
        try:
            collect_credentials(
                tomllib.loads(_read_regular(config_path).decode("utf-8"))
            )
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, BenchError):
            continue

    def clean(value: object, key: str = "") -> object:
        if isinstance(value, dict):
            return {key: clean(child, str(key)) for key, child in value.items()}
        if isinstance(value, list):
            return [clean(child, key) for child in value]
        if isinstance(value, str):
            if any(part in key.lower() for part in _SECRET_CONFIG_KEYS):
                return "***"
            result = value
            for secret in sorted(values, key=len, reverse=True):
                result = result.replace(secret, "***")
            if re.search(
                r"(?i)(api[-_]?key|authorization|access[-_]?token|refresh[-_]?token|"
                r"client[-_]?secret|password|secret|token)\s*[:=]|"
                r"(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{12,}|"
                r"xox[baprs]-[A-Za-z0-9-]{6,}|https?://[^/\s:@]+:[^/\s@]+@)",
                result,
            ):
                result = redact_secrets(result)
            return result
        return value

    lines = []
    for line in text.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        payload = line[:-1] if ending else line
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            safe = redact_secrets(payload)
            for secret in sorted(values, key=len, reverse=True):
                safe = safe.replace(secret, "***")
            lines.append(safe + ending)
        else:
            lines.append(
                json.dumps(clean(parsed), ensure_ascii=False, separators=(",", ":"))
                + ending
            )
    return "".join(lines)


def _shell_evidence(events: list[dict[str, Any]]) -> tuple[bool, str]:
    requests: list[tuple[str, str]] = []
    inputs: list[tuple[str, dict[str, Any]]] = []
    started: list[tuple[str, str]] = []
    completed: list[tuple[str, str, object, object]] = []
    failed: list[tuple[str, str]] = []
    result_ids: list[str] = []
    outputs: dict[str, list[str]] = {}
    for event in events:
        kind, data = event.get("type"), event.get("data")
        if not isinstance(data, dict):
            continue
        call_id = data.get("call_id")
        tool_input = data.get("input")
        if (
            kind == "tool.requested"
            and isinstance(call_id, str)
            and isinstance(data.get("tool"), str)
        ):
            requests.append((call_id, data["tool"]))
        elif (
            kind == "tool.input"
            and isinstance(tool_input, dict)
            and isinstance(call_id, str)
        ):
            inputs.append((call_id, tool_input))
        elif (
            kind == "tool.started"
            and isinstance(data.get("tool"), str)
            and isinstance(call_id, str)
        ):
            started.append((call_id, data["tool"]))
        elif (
            kind == "tool.completed"
            and isinstance(data.get("tool"), str)
            and isinstance(call_id, str)
        ):
            completed.append(
                (
                    call_id,
                    data["tool"],
                    data.get("executed"),
                    data.get("is_error"),
                )
            )
        elif (
            kind == "tool.failed"
            and isinstance(data.get("tool"), str)
            and isinstance(call_id, str)
        ):
            failed.append((call_id, data["tool"]))
        elif kind == "tool.result":
            result_id = data.get("tool_use_id")
            blocks = data.get("content", [])
            if isinstance(result_id, str):
                result_ids.append(result_id)
                for block in blocks if isinstance(blocks, list) else []:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        outputs.setdefault(result_id, []).append(block["text"])
    if len(requests) != 1 or requests[0][1] != "bash":
        return False, "expected exactly one requested tool call, and it must be Bash"
    call_id = requests[0][0]
    if len(inputs) != 1 or inputs[0][0] != call_id:
        return False, "expected exactly one tool input matching the requested Bash call"
    tool_input = inputs[0][1]
    # Nexus's durable tool.input event deliberately removes bash env/settings;
    # the exact command string remains present and is the permission key.
    if (
        tool_input.get("command") != SHELL_COMMAND
        or tool_input.get("action", "run") != "run"
        or set(tool_input) - {"command", "action"}
    ):
        return False, "Bash input was not the exact fixed benchmark invocation"
    if started != [(call_id, "bash")]:
        return False, "expected exactly one started Bash invocation and no other tools"
    if completed != [(call_id, "bash", True, False)]:
        return False, "expected exactly one successfully completed Bash invocation"
    if failed:
        return False, "unexpected failed tool event in shell scenario"
    if result_ids != [call_id]:
        return False, "expected exactly one tool result for the Bash invocation"
    output = "\n".join(outputs.get(call_id, ()))
    lines = output.splitlines()
    exit_codes = [line for line in lines if line.startswith("exit_code:")]
    if (
        SHELL_OUTPUT_MARKER in output
        and exit_codes == ["exit_code: 0"]
        and "status: completed" in lines
    ):
        return (
            True,
            "exactly one fixed Bash invocation completed successfully with the marker",
        )
    return False, "fixed Bash result did not contain the success marker and exit_code 0"


def _grade(
    name: str,
    workspace: Path,
    run_dir: Path | None = None,
    *,
    raw_events: list[dict[str, Any]] | None = None,
    raw_event_error: str | None = None,
) -> dict[str, Any]:
    output = workspace / OUTPUT_FILE
    if _is_symlink(output):
        return {"passed": False, "reason": f"output is a symlink: {OUTPUT_FILE}"}
    if not output.exists():
        return {"passed": False, "reason": f"required output is missing: {OUTPUT_FILE}"}
    try:
        actual = _read_regular(output, max_bytes=4096)
    except (OSError, BenchError) as exc:
        return {"passed": False, "reason": str(exc)}
    if actual != _expected(name):
        return {
            "passed": False,
            "reason": (
                f"{OUTPUT_FILE} does not exactly match the required contents "
                f"(actual output: {len(actual)} bytes); rewrite it with the required contents"
            ),
        }
    if name == "shell-command":
        if run_dir is None:
            run_dir = _scenario_dir(name) / "run"
        try:
            _require_plain_dir(run_dir)
            _require_marker(run_dir)
        except BenchError as exc:
            return {"passed": False, "reason": str(exc)}
        if raw_event_error:
            return {"passed": False, "reason": raw_event_error}
        if raw_events is None:
            raw_events, error = _read_events(run_dir / "events.jsonl")
            if error:
                return {"passed": False, "reason": error}
        passed, evidence = _shell_evidence(raw_events)
        if not passed:
            return {"passed": False, "reason": evidence}
        return {"passed": True, "reason": "exact output plus " + evidence}
    return {"passed": True, "reason": "exact file contents verified"}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_regular(
        path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _stop_workspace_daemon(workspace: Path) -> None:
    """Drop stale runtime/provider state for this owned benchmark workspace."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "nexus",
                "--workspace",
                str(workspace.resolve()),
                "daemon",
                "stop",
            ],
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchError(f"could not stop stale workspace daemon: {exc}") from exc
    if result.returncode:
        raise BenchError("could not stop stale workspace daemon before run")


def command_run(args: argparse.Namespace) -> int:
    name = _scenario(args.scenario)
    _prepare_root()
    with RootLock():
        scenario_dir, workspace = _safe_owned_scenario(name)
        if workspace.parent.resolve(strict=True) != scenario_dir.resolve(strict=True):
            raise BenchError("scenario workspace escaped its owned scenario")
        if name == "shell-command" and not args.allow_shell:
            raise BenchError("shell-command requires explicit --allow-shell")
        policy_problem = _preflight_policy(
            name, workspace, allow_shell=args.allow_shell
        )
        if policy_problem:
            if name == "shell-command" and args.allow_shell:
                print(
                    "WARNING: shell permission matching is not an OS sandbox; approved shell actions run in the user environment.",
                    file=sys.stderr,
                )
            raise BenchError(policy_problem)
        if name == "shell-command":
            print(
                "WARNING: shell permission matching is not an OS sandbox; approved shell actions run in the user environment.",
                file=sys.stderr,
            )

        _stop_workspace_daemon(workspace)
        run_dir = scenario_dir / "run"
        _require_plain_dir(run_dir)
        _require_marker(run_dir)
        if run_dir.parent.resolve(strict=True) != scenario_dir.resolve(strict=True):
            raise BenchError("run artifact directory escaped its owned scenario")
        for filename in ("stdout.log", "stderr.log", "events.jsonl", "result.json"):
            old = run_dir / filename
            if _is_symlink(old):
                raise BenchError(f"Refusing symlinked run artifact: {old}")
            if old.exists():
                if not stat.S_ISREG(old.lstat().st_mode):
                    raise BenchError(f"Refusing non-regular run artifact: {old}")
                old.unlink()
        output = workspace / OUTPUT_FILE
        if _is_symlink(output):
            raise BenchError(f"Refusing symlinked output path: {output}")
        if output.exists():
            if not stat.S_ISREG(output.lstat().st_mode):
                raise BenchError(f"Refusing non-regular output path: {output}")
            output.unlink()
        session = "bench-" + uuid.uuid4().hex
        prompt = (
            "Read input.txt and change its color value from blue to green. "
            "Write the complete result to result.txt."
            if name == "file-edit"
            else "Use Bash exactly once and do not use other tools. The command must be "
            "copied exactly from the code block below, with no added punctuation or text. "
            "After Bash returns, finish without further tool calls.\n\n"
            f"```sh\n{SHELL_COMMAND}\n```"
        )
        command = [
            sys.executable,
            "-m",
            "nexus",
            "--workspace",
            str(workspace.resolve()),
            "run",
            "--json",
            "--session",
            session,
            prompt,
        ]
        timeout = min(max(args.timeout, 1), MAX_TIMEOUT)
        if args.timeout > MAX_TIMEOUT:
            print(f"Timeout capped at {MAX_TIMEOUT}s.", file=sys.stderr)
        try:
            process = subprocess.Popen(
                command,
                cwd=REPO,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise BenchError(f"could not start Nexus process: {exc}") from exc
        try:
            stdout_raw, stderr_raw = process.communicate(timeout=timeout)
            process_exit = process.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                stdout_raw, stderr_raw = process.communicate(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout_raw, stderr_raw = process.communicate()
            process_exit = None
        stdout = stdout_raw.decode("utf-8", "replace")
        stderr = stderr_raw.decode("utf-8", "replace")
        raw_events, event_error = _parse_events(stdout)

        stdout = _redact_runtime_secrets(stdout)
        stderr = _redact_runtime_secrets(stderr)
        _stop_workspace_daemon(workspace)
        _write_regular(run_dir / "stdout.log", stdout.encode("utf-8"))
        _write_regular(run_dir / "stderr.log", stderr.encode("utf-8"))
        _write_regular(run_dir / "events.jsonl", stdout.encode("utf-8"))
        grading = _grade(
            name,
            workspace,
            run_dir,
            raw_events=raw_events,
            raw_event_error=event_error,
        )
        result = {
            "scenario": name,
            "session": session,
            "process": {
                "exit_code": process_exit,
                "timed_out": timed_out,
                "timeout_seconds": timeout,
            },
            "grading": grading,
            "logs": {
                "events": str(run_dir / "events.jsonl"),
                "stdout": str(run_dir / "stdout.log"),
                "stderr": str(run_dir / "stderr.log"),
            },
        }
        _write_json(run_dir / "result.json", result)
        print(f"Process: {'timeout' if timed_out else f'exit {process_exit}'}")
        print(f"Grade: {'PASS' if grading['passed'] else 'FAIL'} — {grading['reason']}")
        print(f"Result: {run_dir / 'result.json'}")
        return 0 if process_exit == 0 and not timed_out and grading["passed"] else 1


def command_check(args: argparse.Namespace) -> int:
    names = SCENARIOS if args.scenario is None else (_scenario(args.scenario),)
    _prepare_root()
    failures = 0
    with RootLock():
        for name in names:
            try:
                _, workspace = _safe_owned_scenario(name)
                result = _grade(name, workspace)
            except BenchError as exc:
                result = {"passed": False, "reason": str(exc)}
            print(
                f"{name}: {'PASS' if result['passed'] else 'FAIL'} — {result['reason']}"
            )
            failures += not result["passed"]
    return 0 if failures == 0 else 1


def command_reset(args: argparse.Namespace) -> int:
    names = SCENARIOS if args.scenario is None else (_scenario(args.scenario),)
    _prepare_root()
    with RootLock():
        removed = [name for name in names if _remove_owned_scenario(name)]
        if removed:
            print("Reset: " + ", ".join(removed))
        else:
            print("Nothing to reset")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the standalone Nexus agent benchmark. Workspaces/logs are isolated "
            "under artifacts/benchmark and the home provider/model configuration is retained."
        )
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List the two fixed scenarios").set_defaults(
        handler=command_list
    )

    setup = commands.add_parser(
        "setup", help="Create or recreate owned scenario workspaces"
    )
    setup.add_argument("scenario", nargs="?", choices=SCENARIOS)
    setup.set_defaults(handler=command_setup)

    run = commands.add_parser(
        "run", help="Start a real agent run and grade its workspace"
    )
    run.add_argument("scenario", choices=SCENARIOS)
    run.add_argument(
        "--allow-shell",
        action="store_true",
        help="Opt in to the shell-command scenario's exact, fixed Bash command",
    )
    run.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Process timeout in seconds (default {DEFAULT_TIMEOUT}, maximum {MAX_TIMEOUT})",
    )
    run.epilog = (
        "Each run uses a fresh session but reuses its workspace, clearing only the previous "
        "output and run artifacts. The process exit and exact-output grade are reported "
        "separately and saved with best-effort redacted JSONL/logs. "
        "Grading never relies on the model's final text. Shell matching is a permission "
        "check, not an OS sandbox; shell-command requires explicit --allow-shell and is "
        "graded using both exact output and Bash tool/completion/output evidence."
    )
    run.set_defaults(handler=command_run)

    check = commands.add_parser(
        "check", help="Grade exact output contents (and shell invocation evidence)"
    )
    check.add_argument("scenario", nargs="?", choices=SCENARIOS)
    check.set_defaults(handler=command_check)

    reset = commands.add_parser(
        "reset", help="Remove only benchmark-owned fixed scenario directories"
    )
    reset.add_argument("scenario", nargs="?", choices=SCENARIOS)
    reset.set_defaults(handler=command_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except BenchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Interrupted; workspace changes may already have occurred.", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
