import importlib.metadata
import json
import subprocess

import pytest

from nexus import cli, update


class FakeDistribution:
    def __init__(self, text):
        self.text = text

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.text


def install_metadata(monkeypatch, payload):
    monkeypatch.setattr(update.importlib.metadata, "distribution",
                        lambda name: FakeDistribution(json.dumps(payload)))


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path):
    """A seed checkout with an origin, and a clean clone tracking origin/main."""
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "install.sh").write_text("#!/bin/sh\n")
    (seed / "nexus-core" / "nexus").mkdir(parents=True)
    (seed / "nexus-core" / "nexus" / "cli.py").write_text("VERSION = 1\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "first")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    return seed, work


def commit(seed, name, text):
    (seed / name).write_text(text)
    git(seed, "add", name)
    git(seed, "commit", "-q", "-m", f"add {name}")
    git(seed, "push", "-q", "origin", "main")


# --- installation detection -------------------------------------------------------------------------------------

def test_a_checkout_install_is_found_from_direct_url(tmp_path, monkeypatch):
    (tmp_path / "checkout" / "nexus-core" / "nexus").mkdir(parents=True)
    (tmp_path / "checkout" / ".git").mkdir()
    (tmp_path / "checkout" / "install.sh").write_text("#!/bin/sh\n")
    install_metadata(monkeypatch, {"url": (tmp_path / "checkout" / "nexus-core").as_uri(), "dir_info": {"editable": True}})
    found = update.installed()
    assert found.kind == "checkout" and found.root == (tmp_path / "checkout").resolve()


def test_a_release_wheel_is_a_release_install(tmp_path, monkeypatch):
    install_metadata(monkeypatch, {"url": "file:///tmp/nexus10-1.0.0-py3-none-any.whl", "archive_info": {}})
    assert update.installed().kind == "release"


def test_a_local_directory_that_is_not_a_checkout_is_unknown(tmp_path, monkeypatch):
    (tmp_path / "package" / "nexus-core" / "nexus").mkdir(parents=True)
    (tmp_path / "package" / "install.sh").write_text("#!/bin/sh\n")
    install_metadata(monkeypatch, {"url": (tmp_path / "package" / "nexus-core").as_uri(), "dir_info": {}})
    assert update.installed().kind == "unknown"


@pytest.mark.parametrize("payload", [
    {"url": "https://example.com/nexus.git", "vcs_info": {"vcs": "git"}},
    {"url": "file:///tmp/gone/nexus-core", "dir_info": {}},
    {},
    {"url": "file:///tmp/gone", "unknown_info": {}},
])
def test_an_install_it_cannot_verify_is_unknown(tmp_path, monkeypatch, payload):
    install_metadata(monkeypatch, payload)
    found = update.installed()
    assert found.kind == "unknown" and found.detail


def test_missing_metadata_is_unknown(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(update.importlib.metadata, "distribution", missing)
    assert update.installed().kind == "unknown"


# --- checkout updates -------------------------------------------------------------------------------------------

def test_a_clean_checkout_fast_forwards_and_reinstalls(tmp_path, monkeypatch):
    seed, work = repository(tmp_path)
    commit(seed, "new.txt", "new")
    calls = []
    monkeypatch.setattr(update, "_exec_installer", lambda script, *args: calls.append((script, args)))
    assert update.update_checkout(work, yes=True) == 0
    assert (work / "new.txt").read_text() == "new"
    assert calls == [(work / "install.sh", ("--source",))]


def test_an_up_to_date_checkout_reinstalls_to_recover_from_a_previous_pull(tmp_path, monkeypatch, capsys):
    _, work = repository(tmp_path)
    calls = []
    monkeypatch.setattr(update, "_exec_installer", lambda *args: calls.append(args))
    assert update.update_checkout(work, yes=True) == 0
    assert "up to date" in capsys.readouterr().out
    assert calls == [(work / "install.sh", "--source")]


def test_untracked_files_refuse_before_fetching(tmp_path, monkeypatch):
    seed, work = repository(tmp_path)
    commit(seed, "new.txt", "new")
    (work / "scratch.txt").write_text("mine")
    fetched = []
    monkeypatch.setattr(update, "_exec_installer", lambda *args: pytest.fail("installed anyway"))
    real_git = update._git

    def tracked(root, *args):
        if args and args[0] == "fetch":
            fetched.append(args)
        return real_git(root, *args)
    monkeypatch.setattr(update, "_git", tracked)
    with pytest.raises(update.UpdateError, match="untracked"):
        update.update_checkout(work, yes=True)
    assert fetched == []


def test_local_commits_are_not_discarded(tmp_path, monkeypatch):
    seed, work = repository(tmp_path)
    (work / "local.txt").write_text("local")
    git(work, "add", "local.txt")
    git(work, "commit", "-q", "-m", "local")
    commit(seed, "new.txt", "new")
    monkeypatch.setattr(update, "_exec_installer", lambda *args: pytest.fail("installed anyway"))
    with pytest.raises(update.UpdateError, match="not on origin/main"):
        update.update_checkout(work, yes=True)
    assert (work / "local.txt").exists()
    assert not (work / "new.txt").exists()


def test_a_detached_checkout_is_refused(tmp_path):
    _, work = repository(tmp_path)
    git(work, "checkout", "-q", "--detach")
    with pytest.raises(update.UpdateError, match="detached"):
        update.update_checkout(work, yes=True)


def test_a_branch_without_an_upstream_is_refused(tmp_path):
    _, work = repository(tmp_path)
    git(work, "checkout", "-q", "-b", "topic")
    with pytest.raises(update.UpdateError, match="no upstream"):
        update.update_checkout(work, yes=True)


def test_without_a_terminal_yes_is_required_before_fetching(tmp_path, monkeypatch):
    seed, work = repository(tmp_path)
    commit(seed, "new.txt", "new")
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(update, "_exec_installer", lambda *args: pytest.fail("installed anyway"))
    with pytest.raises(update.UpdateError, match="--yes"):
        update.update_checkout(work, yes=False)
    assert not (work / "new.txt").exists()


# --- release updates --------------------------------------------------------------------------------------------

def test_the_installer_is_checked_against_the_pinned_release(tmp_path, monkeypatch):
    installer = b"#!/bin/sh\necho install\n"
    digest = update.hashlib.sha256(installer).hexdigest()
    seen = []

    def download(url, limit):
        seen.append(url)
        return (f"{digest}  install.sh\n".encode() if url.endswith("SHA256SUMS") else installer)
    monkeypatch.setattr(update, "_download", download)
    assert update.release_installer("v1.2.3") == installer
    assert seen == [f"{update.RELEASE_DOWNLOAD}/v1.2.3/SHA256SUMS",
                    f"{update.RELEASE_DOWNLOAD}/v1.2.3/install.sh"]


def test_a_checksum_mismatch_installs_nothing(monkeypatch):
    monkeypatch.setattr(update, "_download", lambda url, limit: (
        b"0" * 64 + b"  install.sh\n" if url.endswith("SHA256SUMS") else b"tampered"))
    with pytest.raises(update.UpdateError, match="does not match"):
        update.release_installer("v1.2.3")


def test_an_unexpected_tag_is_refused_before_download(monkeypatch):
    monkeypatch.setattr(update, "_download", lambda *args: pytest.fail("downloaded anyway"))
    with pytest.raises(update.UpdateError, match="unexpected release tag"):
        update.release_installer("../../evil")


def test_a_release_update_hands_the_verified_installer_to_the_installer(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(update, "release_installer", lambda tag: b"installer")
    monkeypatch.setattr(update, "_exec_installer_bytes", lambda content, *args: seen.append((content, args)))
    report = {"current": "1.0.0", "latest": "2.0.0", "state": "available", "detail": "2.0.0 is available"}
    assert update.update_release(report, {"tag_name": "v2.0.0"}, yes=True) == 0
    assert seen == [(b"installer", ("--release", "v2.0.0"))]
    out = capsys.readouterr().out
    assert "restart a running Core daemon" in out and "daemon start" in out


def test_a_current_release_does_not_prompt_or_install(monkeypatch, capsys):
    monkeypatch.setattr(update, "_exec_installer_bytes", lambda *args: pytest.fail("installed anyway"))
    report = {"current": "2.0.0", "latest": "2.0.0", "state": "current", "detail": "2.0.0 is the latest release"}
    assert update.update_release(report, {"tag_name": "v2.0.0"}, yes=False) == 0
    assert "latest release" in capsys.readouterr().out


def test_release_confirmation_needs_a_terminal_without_yes(monkeypatch):
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(update, "_exec_installer_bytes", lambda *args: pytest.fail("installed anyway"))
    report = {"current": "1.0.0", "latest": "2.0.0", "state": "available", "detail": "2.0.0 is available"}
    with pytest.raises(update.UpdateError, match="--yes"):
        update.update_release(report, {"tag_name": "v2.0.0"}, yes=False)


# --- command modes ----------------------------------------------------------------------------------------------

def test_unknown_installs_fail_closed_with_instructions(monkeypatch, capsys):
    monkeypatch.setattr(update, "installed", lambda: update.Install("unknown", detail="not installed by install.sh"))
    monkeypatch.setattr(update, "update_release", lambda *args: pytest.fail("installed anyway"))
    monkeypatch.setattr(update, "update_checkout", lambda *args: pytest.fail("installed anyway"))
    with pytest.raises(update.UpdateError, match="Reinstall with install.sh") as refused:
        update.apply("1.0.0", yes=True)
    assert "not installed by install.sh" in str(refused.value)


def test_apply_routes_a_checkout_to_its_upstream_update(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(update, "installed", lambda: update.Install("checkout", root=tmp_path))
    monkeypatch.setattr(update, "update_checkout", lambda root, yes: seen.append((root, yes)) or 0)
    assert update.apply("1.0.0", yes=True) == 0
    assert seen == [(tmp_path, True)]


def test_apply_routes_a_release_to_the_release_check(monkeypatch):
    seen = []
    shape = {"current": "1.0.0", "latest": "2.0.0", "state": "available", "detail": "2.0.0 is available"}
    monkeypatch.setattr(update, "installed", lambda: update.Install("release"))
    monkeypatch.setattr(update.doctor, "check_release", lambda current: (shape, {"tag_name": "v2.0.0"}))
    monkeypatch.setattr(update, "update_release", lambda report, release, yes: seen.append((report, release, yes)) or 0)
    assert update.apply("1.0.0", yes=True) == 0
    assert seen == [(shape, {"tag_name": "v2.0.0"}, True)]


def test_json_keeps_the_existing_shape_and_never_classifies_the_install(monkeypatch, capsys):
    shape = {"current": "1.0.0", "latest": "2.0.0", "url": "https://example.com/r", "state": "available",
             "detail": "2.0.0 is available"}
    monkeypatch.setattr(update.doctor, "update_check", lambda current: shape)
    monkeypatch.setattr(update, "installed", lambda: pytest.fail("checked the install in report mode"))
    assert update.run("1.0.0", as_json=True, yes=True) == 0
    assert json.loads(capsys.readouterr().out) == shape


def test_check_mode_reports_and_hints_at_the_updater(monkeypatch, capsys):
    shape = {"current": "1.0.0", "latest": "2.0.0", "url": "https://example.com/r", "state": "available",
             "detail": "2.0.0 is available"}
    monkeypatch.setattr(update.doctor, "update_check", lambda current: shape)
    monkeypatch.setattr(update, "installed", lambda: pytest.fail("checked the install in check mode"))
    assert update.run("1.0.0", check_only=True) == 0
    out = capsys.readouterr().out
    assert "2.0.0 is available" in out and "nexus update" in out


def test_the_cli_passes_check_json_and_yes_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(update, "run", lambda current, **kwargs: seen.update(kwargs) or 0)
    assert cli.main(["update", "--check", "--json", "--yes"]) == 0
    assert seen == {"check_only": True, "as_json": True, "yes": True}
