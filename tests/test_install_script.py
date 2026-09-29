"""Offline tests for ``install.sh`` (plans/install.md).

The script runs against a stub ``uv`` that records its argv and a stub ``nexus``,
inside a throwaway ``HOME``. Nothing touches the network.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")

UV_STUB = """#!/bin/sh
echo "$@" >> "$UV_LOG"
case "$1" in
  --version) echo "uv 0.6.0" ;;
  tool)
    case "$2" in
      dir) echo "$STUB_BIN" ;;
    esac ;;
esac
exit 0
"""

NEXUS_STUB = """#!/bin/sh
echo "nexus $*" >> "$UV_LOG"
case "$1" in --version) echo "nexus 9.9.9" ;; esac
exit 0
"""


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _exe(stubs / "uv", UV_STUB)
    _exe(bin_dir / "nexus", NEXUS_STUB)
    log = tmp_path / "uv.log"
    log.write_text("")
    base = {
        "HOME": str(home),
        "PATH": f"{stubs}:{bin_dir}:/usr/bin:/bin",
        "UV_LOG": str(log),
        "STUB_BIN": str(bin_dir),
        "TMPDIR": str(tmp_path),
        "NEXUS_ALLOW_ROOT": "1",
    }
    return base, log, stubs


def run(env_vars, *args):
    return subprocess.run(
        [SH, str(SCRIPT), *args], env=env_vars, capture_output=True, text=True, timeout=60, check=False
    )


def test_syntax():
    assert subprocess.run([SH, "-n", str(SCRIPT)], check=False).returncode == 0


def test_installs_from_git_by_default(env):
    vars_, log, _ = env
    result = run(vars_, "--no-modify-path")
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert (
        "tool install --force --python 3.13 "
        "nexus-harness @ git+https://github.com/nottherealsanta/nexus@main"
    ) in calls
    assert "nexus --version" in calls
    assert "nexus daemon stop --all" in calls
    assert "nexus chat" in result.stdout


def test_pypi_source_with_version_and_extras(env):
    vars_, log, _ = env
    result = run(
        {**vars_, "NEXUS_SOURCE": "pypi", "NEXUS_VERSION": "0.2.0", "NEXUS_EXTRAS": "documents"},
        "--no-doctor",
    )
    assert result.returncode == 0, result.stderr
    assert "tool install --force --python 3.13 nexus-harness[documents]==0.2.0" in log.read_text()


def test_flags_beat_environment(env):
    vars_, log, _ = env
    result = run(
        {**vars_, "NEXUS_SOURCE": "git", "NEXUS_PYTHON": "3.13"},
        "--source", "pypi", "--python", "3.14", "--no-doctor",
    )
    assert result.returncode == 0, result.stderr
    assert "tool install --force --python 3.14 nexus-harness" in log.read_text()
    assert "git+" not in log.read_text()


def test_path_edit_only_when_bin_dir_missing_from_path(env):
    vars_, log, _ = env
    assert run(vars_, "--no-doctor").returncode == 0
    assert "tool update-shell" not in log.read_text()  # bin dir already on PATH

    log.write_text("")
    stubs = vars_["PATH"].split(":")[0]
    off_path = {**vars_, "PATH": f"{stubs}:/usr/bin:/bin"}
    result = run(off_path, "--no-doctor")
    assert result.returncode == 0, result.stderr
    assert "tool update-shell" in log.read_text()
    assert "Open a new terminal" in result.stdout

    log.write_text("")
    run(off_path, "--no-doctor", "--no-modify-path")
    assert "tool update-shell" not in log.read_text()


def test_installs_uv_when_missing(env, tmp_path):
    vars_, log, stubs = env
    home = Path(vars_["HOME"])
    (stubs / "uv").rename(tmp_path / "uv-real")
    fake_curl = stubs / "curl"
    # The "installer" curl emits drops a uv into ~/.local/bin, like the real one.
    _exe(
        fake_curl,
        "#!/bin/sh\n"
        "cat <<'EOS'\n"
        f"cp {tmp_path / 'uv-real'} {home / '.local' / 'bin' / 'uv'}\n"
        "EOS\n",
    )
    result = run({**vars_, "PATH": f"{stubs}:/usr/bin:/bin"}, "--no-doctor", "--no-modify-path")
    assert result.returncode == 0, result.stderr
    assert "installing it from astral.sh" in result.stdout
    assert "tool install --force" in log.read_text()


def test_truncated_download_runs_nothing(env, tmp_path):
    vars_, log, _ = env
    text = SCRIPT.read_text().splitlines()
    cut = tmp_path / "cut.sh"
    # Everything before the final ``main "$@"`` line: definitions only.
    cut.write_text("\n".join(text[:-1]) + "\n")
    result = subprocess.run([SH, str(cut)], env=vars_, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert log.read_text() == ""


def test_rejects_unknown_option(env):
    vars_, _, _ = env
    result = run(vars_, "--bogus")
    assert result.returncode == 1
    assert "unknown option" in result.stderr


def test_refuses_root_without_override(env):
    vars_, _, _ = env
    if os.getuid() != 0:
        pytest.skip("only meaningful as root")
    result = run({k: v for k, v in vars_.items() if k != "NEXUS_ALLOW_ROOT"})
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# nexus --version / update / daemon stop --all / install helpers
# ---------------------------------------------------------------------------


def test_version_flag(capsys):
    from nexus import cli
    from nexus.host_support.install import package_version

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"nexus {package_version()}"


def test_parser_accepts_update_and_stop_all():
    from nexus import cli

    parser = cli.build_parser()
    assert parser.parse_args(["update", "--no-restart"]).no_restart is True
    assert parser.parse_args(["daemon", "stop", "--all"]).all is True
    assert parser.parse_args(["daemon", "stop"]).all is False


def test_update_command_argv():
    from nexus.host_support.install import update_command

    assert update_command("/u/uv", "uv-tool") == ["/u/uv", "tool", "upgrade", "nexus-harness"]
    assert update_command("/u/uv", "git")[:4] == ["/u/uv", "tool", "install", "--force"]


def test_find_uv_checks_usual_install_dirs(tmp_path):
    from nexus.host_support.install import find_uv

    home = tmp_path / "h"
    (home / ".local" / "bin").mkdir(parents=True)
    assert find_uv({"PATH": "", "HOME": str(home)}) is None
    uv = home / ".local" / "bin" / "uv"
    _exe(uv, "#!/bin/sh\n")
    assert find_uv({"PATH": "", "HOME": str(home)}) == str(uv)


def test_nexus_executables_dedupes_by_realpath(tmp_path):
    from nexus.host_support.install import nexus_executables

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _exe(a / "nexus", "#!/bin/sh\n")
    (b / "nexus").symlink_to(a / "nexus")
    assert nexus_executables(f"{a}:{b}") == [str(a / "nexus")]
    other = tmp_path / "c"
    other.mkdir()
    _exe(other / "nexus", "#!/bin/sh\n")
    assert len(nexus_executables(f"{a}:{other}")) == 2


async def test_install_report_flags_stale_daemon_and_duplicates(monkeypatch):
    from nexus.host_support import install

    async def fake_daemons(home=None):
        return [{"socket": "s", "pid": 1, "workspace": "/w", "server": "0.0.1"}]

    monkeypatch.setattr(install, "running_daemons", fake_daemons)
    monkeypatch.setattr(install, "package_version", lambda: "0.2.0")
    monkeypatch.setattr(install, "nexus_executables", lambda path=None: ["/a/nexus", "/b/nexus"])
    report = await install.install_report()
    text = " ".join(report["warnings"])
    assert "more than one `nexus`" in text
    assert "runs 0.0.1" in text and "nexus daemon restart" in text


def test_update_refuses_editable_and_non_uv_installs(monkeypatch, capsys):
    from nexus import cli
    from nexus.host_support import install

    monkeypatch.setattr(install, "install_method", lambda: "editable")
    assert cli.main(["update"]) == 1
    assert "editable install" in capsys.readouterr().err

    monkeypatch.setattr(install, "install_method", lambda: "uv-tool")
    monkeypatch.setattr(install, "find_uv", lambda environ=None: None)
    assert cli.main(["update"]) == 1
    assert "not installed with uv" in capsys.readouterr().err


def test_update_upgrades_then_restarts_running_daemons(monkeypatch, capsys):
    import subprocess as sp

    from nexus import cli
    from nexus.host_support import install

    calls: list[list[str]] = []
    versions = iter(["0.1.0", "0.2.0"])

    async def daemons(home=None):
        return [{"socket": "s", "pid": 1, "workspace": "/w1", "server": "0.1.0"}]

    stopped = []

    async def stop_all(home=None):
        stopped.append(True)
        return 1

    monkeypatch.setattr(install, "install_method", lambda: "uv-tool")
    monkeypatch.setattr(install, "find_uv", lambda environ=None: "/u/uv")
    monkeypatch.setattr(install, "package_version", lambda: next(versions))
    monkeypatch.setattr(install, "running_daemons", daemons)
    monkeypatch.setattr(install, "stop_all_daemons", stop_all)
    monkeypatch.setattr(install, "run_update", lambda uv, method: 0)
    monkeypatch.setattr(install, "installed_version_after_update", lambda b=None: "0.2.0")
    monkeypatch.setattr(
        sp, "run",
        lambda argv, **kw: calls.append(argv) or sp.CompletedProcess(argv, 0, "", ""),
    )
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "0.1.0 -> 0.2.0" in out and "Restarted daemon for /w1" in out
    assert stopped
    assert calls[0][-4:] == ["--workspace", "/w1", "daemon", "restart"]


def test_update_failure_leaves_daemons_alone(monkeypatch, capsys):
    from nexus import cli
    from nexus.host_support import install

    async def daemons(home=None):
        return [{"socket": "s", "pid": 1, "workspace": "/w", "server": "0.1.0"}]

    async def boom(home=None):
        raise AssertionError("must not stop daemons when the upgrade failed")

    monkeypatch.setattr(install, "install_method", lambda: "uv-tool")
    monkeypatch.setattr(install, "find_uv", lambda environ=None: "/u/uv")
    monkeypatch.setattr(install, "running_daemons", daemons)
    monkeypatch.setattr(install, "stop_all_daemons", boom)
    monkeypatch.setattr(install, "run_update", lambda uv, method: 2)
    assert cli.main(["update"]) == 1
    assert "not changed" in capsys.readouterr().err
