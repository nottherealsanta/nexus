"""``SkillsInstall``: stage with a fake ``npx``, validate, import, never touch the network."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from nexus.client.protocol import Client
from nexus.errors import ConfigError
from nexus.host import protocol as p
from nexus.host.facade import HostFacade
from nexus.host_support import skills_install as si

GOOD = "---\nname: {name}\ndescription: Does {name}.\n---\nBody {name}.\n"


class _Extensions:
    def __init__(self):
        self.reloads = []

    async def reload(self, trigger="api", sink=None):
        self.reloads.append(trigger)
        return type("Report", (), {"failed": ()})()


def _runtime(workspace):
    return type("RuntimeStub", (), {"workspace": workspace, "extensions": _Extensions()})()


def _payload(root: Path, skills: dict[str, str]) -> Path:
    base = root / "payload" / ".claude" / "skills"
    base.mkdir(parents=True)
    for name, body in skills.items():
        (base / name).mkdir()
        (base / name / "SKILL.md").write_text(body)
    return root / "payload"


def _fake_npx(root: Path, payload: Path | None, *, code: int = 0, extra: str = "") -> Path:
    """A real ``npx`` stand-in on PATH: records argv/env/cwd, copies the payload, no network."""
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    copy = f'cp -R "{payload}/." "$PWD/"' if payload else ":"
    script = bin_dir / "npx"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{root}/argv"\n'
        f'env > "{root}/env"\n'
        f'pwd > "{root}/cwd"\n'
        f'echo $$ > "{root}/pid"\n'
        f"{copy}\n{extra}\nexit {code}\n"
    )
    script.chmod(0o755)
    return bin_dir


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NEXUS_HOME", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setattr(si, "_installing", False)
    fakes = tmp_path / "fake"
    fakes.mkdir()

    def use(payload, **kwargs):
        bin_dir = _fake_npx(fakes, payload, **kwargs)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")

    return type("Env", (), {"home": home, "workspace": workspace, "fakes": fakes, "use": staticmethod(use), "tmp": tmp_path})()


def _install(env, *args, **kwargs):
    runtime = _runtime(env.workspace)
    result = asyncio.run(si.install(runtime, *args, **kwargs))
    return result, runtime


@pytest.mark.parametrize("source", ["", "-g", "--yes", "a b/c", "owner/repo\n", "owner", "http://x.y/z", "https://u:p@host/r",
                                    "relative/dir/x", "/does/not/exist", "x" * 300, "owner/repo;rm"])
def test_bad_sources_are_rejected(source):
    with pytest.raises(ConfigError):
        si.validate_source(source)


def test_good_sources(tmp_path):
    for source in ("vercel-labs/agent-skills", "https://github.com/vercel-labs/agent-skills.git", str(tmp_path)):
        assert si.validate_source(source) == source


@pytest.mark.parametrize("names", [["--all"], ["a b"], ["../x"], [""], ["x"] * 65])
def test_bad_names_are_rejected(names):
    with pytest.raises(ConfigError):
        si.validate_names(names)


def test_global_install_refuses_when_discovery_reads_another_home(env, monkeypatch):
    payload = _payload(env.tmp, {"alpha": GOOD.format(name="alpha")})
    env.use(payload)
    monkeypatch.setenv("NEXUS_HOME", str(env.tmp / "elsewhere"))
    with pytest.raises(ConfigError, match="NEXUS_HOME"):
        _install(env, "global", "owner/repo")
    assert not (env.home / ".nexus").exists() and not (env.tmp / "elsewhere").exists()
    assert not (env.fakes / "argv").exists(), "npx must not even run"
    # Project installs are unaffected, and a matching NEXUS_HOME is accepted.
    result, _ = _install(env, "project", "owner/repo")
    assert result["status"] == "installed"
    monkeypatch.setenv("NEXUS_HOME", str(env.home / ".nexus"))
    result, _ = _install(env, "global", "owner/repo")
    assert result["status"] == "installed" and (env.home / ".nexus/skills/alpha/SKILL.md").is_file()


def test_project_install_copies_files_with_a_clean_environment(env):
    payload = _payload(env.tmp, {"alpha": GOOD.format(name="alpha"), "beta": GOOD.format(name="beta")})
    script = payload / ".claude/skills/alpha/run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    (payload / ".claude/skills/alpha/tools").mkdir()
    (payload / ".claude/skills/alpha/tools/evil.py").write_text("print(1)\n")
    (payload / ".claude/skills/alpha/docs").mkdir()
    (payload / ".claude/skills/alpha/docs/a.md").write_text("doc\n")
    env.use(payload)

    result, runtime = _install(env, "project", "vercel-labs/agent-skills")

    assert result["status"] == "installed" and result["installed"] == ["alpha", "beta"]
    assert result["skipped"] == ["alpha: tools/ not imported (executable code)"]
    root = env.workspace / ".agents" / "skills"
    assert (root / "alpha" / "SKILL.md").read_text() == GOOD.format(name="alpha")
    assert (root / "alpha" / "docs" / "a.md").read_text() == "doc\n"
    assert not (root / "alpha" / "tools").exists()
    assert (root / "alpha" / "run.sh").stat().st_mode & 0o111 == 0, "permission bits are not imported"
    assert not any(path.is_symlink() for path in root.rglob("*"))
    assert runtime.extensions.reloads == ["settings"]
    argv = (env.fakes / "argv").read_text().split()
    assert argv == ["--yes", si.PACKAGE, "add", "vercel-labs/agent-skills", "--copy", "-y", "-a", "claude-code"]
    child_env = (env.fakes / "env").read_text()
    assert "ANTHROPIC_API_KEY" not in child_env and "CI=1" in child_env
    cwd = (env.fakes / "cwd").read_text().strip()
    assert not Path(cwd).exists(), "the staging directory is removed"
    assert not str(Path(cwd).resolve()).startswith(str(env.workspace))
    staging = env.workspace / ".agents" / "trash" / "skills-staging"
    assert not list(staging.iterdir())


def test_global_install_and_named_skills(env):
    env.use(_payload(env.tmp, {"alpha": GOOD.format(name="alpha"), "beta": GOOD.format(name="beta")}))
    result, _ = _install(env, "global", "owner/repo", ["beta", "missing"])
    assert result["status"] == "partial" and result["installed"] == ["beta"]
    assert result["failed"] == ["missing: not offered by the source"]
    assert (env.home / ".nexus" / "skills" / "beta" / "SKILL.md").is_file()
    assert not (env.home / ".nexus" / "skills" / "alpha").exists()
    assert "-s\nbeta\n-s\nmissing\n" in (env.fakes / "argv").read_text()
    assert not (env.workspace / ".agents").exists()


def test_conflict_is_refused_then_overwrite_moves_the_old_folder_to_trash(env):
    env.use(_payload(env.tmp, {"alpha": GOOD.format(name="alpha") + "NEW\n"}))
    old = env.workspace / ".agents" / "skills" / "alpha"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text(GOOD.format(name="alpha"))
    (old / "extra.txt").write_text("keep me")

    result, runtime = _install(env, "project", "owner/repo")
    assert result["status"] == "conflict" and result["conflicts"] == ["alpha"] and result["installed"] == []
    assert (old / "extra.txt").exists() and "NEW" not in (old / "SKILL.md").read_text()
    assert runtime.extensions.reloads == []

    result, runtime = _install(env, "project", "owner/repo", overwrite=True)
    assert result["status"] == "installed" and runtime.extensions.reloads == ["settings"]
    assert "NEW" in (old / "SKILL.md").read_text() and not (old / "extra.txt").exists()
    entries = list((env.workspace / ".agents" / "trash" / "settings").iterdir())
    assert len(entries) == 1
    assert (entries[0] / "item" / "extra.txt").read_text() == "keep me"
    meta = json.loads((entries[0] / "meta.json").read_text())
    assert meta == {"rel_path": "skills/alpha", "scope": "project", "category": "skills", "id": "alpha", "kind": "directory"}


def test_a_failed_rename_puts_the_old_folder_back(env, monkeypatch):
    env.use(_payload(env.tmp, {"alpha": GOOD.format(name="alpha") + "NEW\n"}))
    old = env.workspace / ".agents" / "skills" / "alpha"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text(GOOD.format(name="alpha"))
    real = os.replace

    def flaky(src, dst):
        if str(src).endswith("/alpha") and "skills-staging" in str(src):
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(si.os, "replace", flaky)
    result, _ = _install(env, "project", "owner/repo", overwrite=True)
    assert result["status"] == "failed" and "alpha" in result["failed"][0]
    assert (old / "SKILL.md").read_text() == GOOD.format(name="alpha")
    assert not list((env.workspace / ".agents" / "trash" / "settings").iterdir())


def test_symlinks_special_files_and_bad_skills_are_refused_individually(env):
    payload = _payload(env.tmp, {
        "good": GOOD.format(name="good"), "linked": GOOD.format(name="linked"), "bad": "no frontmatter",
        "renamed": GOOD.format(name="other"), "unsupported": "---\nname: unsupported\ndescription: x\ncompatibility: unsupported\n---\n",
        "fifo": GOOD.format(name="fifo"), "tools-only": GOOD.format(name="tools-only"),
    })
    skills = payload / ".claude/skills"
    (skills / "linked" / "leak").symlink_to("/etc/passwd")
    os.mkfifo(skills / "fifo" / "pipe")
    (skills / "escape").symlink_to("/tmp")
    env.use(payload)

    result, _ = _install(env, "project", "owner/repo")

    assert result["status"] == "partial"
    assert result["installed"] == ["good", "tools-only"]
    failed = {row.split(":")[0]: row for row in result["failed"]}
    assert set(failed) == {"linked", "bad", "renamed", "unsupported", "fifo", "escape"}
    assert "symlink" in failed["linked"] and "special file" in failed["fifo"] and "does not match" in failed["renamed"]
    assert "compatibility" in failed["unsupported"], "unknown frontmatter keys (and allowed-tools semantics) are never silently ignored"
    root = env.workspace / ".agents" / "skills"
    assert sorted(path.name for path in root.iterdir()) == ["good", "tools-only"]


def test_size_and_file_count_limits(env, monkeypatch):
    payload = _payload(env.tmp, {"big": GOOD.format(name="big")})
    (payload / ".claude/skills/big/blob.bin").write_bytes(b"x" * 2048)
    env.use(payload)
    monkeypatch.setattr(si, "MAX_FILE_BYTES", 1024)
    result, _ = _install(env, "project", "owner/repo")
    assert result["status"] == "failed" and "larger than" in result["failed"][0]
    assert not (env.workspace / ".agents" / "skills" / "big").exists()


def test_missing_npx_changes_nothing(env, monkeypatch):
    empty = env.tmp / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result, runtime = _install(env, "project", "owner/repo")
    assert result["status"] == "unavailable" and "npx" in result["message"]
    assert not (env.workspace / ".agents").exists() and runtime.extensions.reloads == []


def test_nonzero_exit_is_failed_redacted_and_bounded(env):
    extra = ('echo "api_key=sk-live-supersecret123456" >&2\n'
             'head -c 300000 /dev/zero | tr "\\0" "x"')
    env.use(_payload(env.tmp, {"alpha": GOOD.format(name="alpha")}), code=3, extra=extra)
    result, _ = _install(env, "project", "owner/repo")
    assert result["status"] == "failed" and "code 3" in result["message"]
    assert "supersecret" not in result["message"] and len(result["message"]) <= si.MAX_MESSAGE_CHARS + 100
    assert not (env.workspace / ".agents").exists(), "a failed run imports nothing even if files were staged"


def test_timeout_kills_the_process_group_and_changes_nothing(env, monkeypatch):
    monkeypatch.setattr(si, "TIMEOUT_SECONDS", 0.5)
    env.use(None, extra="sleep 30 &\necho $! > \"%s/child\"\nwait" % env.fakes)
    result, _ = _install(env, "project", "owner/repo")
    assert result["status"] == "timeout"
    child = int((env.fakes / "child").read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert not (env.workspace / ".agents").exists()
    assert si._installing is False


def test_only_one_install_at_a_time(env, monkeypatch):
    env.use(_payload(env.tmp, {"alpha": GOOD.format(name="alpha")}))
    monkeypatch.setattr(si, "_installing", True)
    result, _ = _install(env, "project", "owner/repo")
    assert result["status"] == "busy"
    assert not (env.workspace / ".agents").exists()


def test_nothing_staged_reports_nothing(env):
    env.use(None)
    result, runtime = _install(env, "project", "owner/repo")
    assert result["status"] == "nothing" and runtime.extensions.reloads == []


def test_symlinked_skills_root_is_refused(env):
    env.use(_payload(env.tmp, {"alpha": GOOD.format(name="alpha")}))
    outside = env.tmp / "outside"
    outside.mkdir()
    agents = env.workspace / ".agents"
    agents.mkdir()
    (agents / "skills").symlink_to(outside)
    result, _ = _install(env, "project", "owner/repo")
    assert result["status"] == "failed" and not list(outside.iterdir())


def test_host_command_round_trip_and_default_deny_for_devices(env):
    env.use(_payload(env.tmp, {"alpha": GOOD.format(name="alpha")}))

    class Transport:
        def __init__(self, facade):
            self.facade = facade

        async def request(self, command):
            return await self.facade.handle(command)

        async def aclose(self):
            pass

    client = Client(Transport(HostFacade(_runtime(env.workspace))))
    result = asyncio.run(client.skills_install("project", "owner/repo", ["alpha"]))
    assert isinstance(result, p.SkillsInstallResult) and result.installed == ["alpha"]
    command = p.SkillsInstall(scope="global", source="owner/repo", skills=["a"], overwrite=True)
    assert p.decode_command(p.encode_command(command)) == command

    from nexus.gateway import policy
    assert policy.required_scope(command) is None
    with pytest.raises(policy.CommandDenied):
        policy.authorize(command, {"read", "prompt", "approve", "voice", "live"}, "dev_1")


def test_bad_source_at_the_host_boundary_is_an_error(env):
    env.use(None)
    with pytest.raises(ConfigError):
        asyncio.run(si.install(_runtime(env.workspace), "project", "--yes"))
