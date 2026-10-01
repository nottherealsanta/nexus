"""Offline release gates: CI failures and unexpected generated changes stop merges."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def texts(version):
    return {
        "pyproject.toml": f'[project]\nname = "nexus-harness"\nversion = "{version}"\n',
        "uv.lock": f'[[package]]\nname = "nexus-harness"\nversion = "{version}"\n',
        ".release-please-manifest.json": json.dumps({".": version}),
        "CHANGELOG.md": f'# Changelog\n\n## [{version}]\n\nFixes.\n',
    }


def test_patch_version():
    assert release.patch_after("v0.2.15") == "0.2.16"
    with pytest.raises(release.ReleaseError):
        release.patch_after("v0.3.0rc1")


@pytest.mark.parametrize("state", ["FAILURE", "CANCELLED", "TIMED_OUT", "ERROR", "ACTION_REQUIRED"])
def test_failed_checks_stop_release(state):
    with pytest.raises(release.ReleaseError, match="Checks failed"):
        release.validate_checks([{"name": "test", "state": state}])


def test_requires_named_gates_even_if_github_reports_no_required_checks():
    assert not release.validate_checks([])
    assert not release.validate_checks([{"name": "build", "state": "SUCCESS"}])
    gates = [{"name": name, "state": "SUCCESS"} for name in release.REQUIRED]
    assert release.validate_checks(gates)
    assert not release.validate_checks(gates + [{"name": "test", "state": "IN_PROGRESS"}])


def test_pending_check_exit_code_is_readable(monkeypatch):
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 8, '[{"name":"test","state":"IN_PROGRESS"}]', ''))
    assert release.gh("pr", "checks", "1", "--json", "name,state")[0]["state"] == "IN_PROGRESS"


def test_generated_release_has_only_expected_changes():
    baseline = texts("0.2.15")
    updated = texts("0.2.16")
    updated["CHANGELOG.md"] += baseline["CHANGELOG.md"].partition("\n")[2]
    release.validate_release(release.FILES, updated, "0.2.16", baseline)
    with pytest.raises(release.ReleaseError, match="exactly"):
        release.validate_release(release.FILES | {"nexus/runtime.py"}, updated, "0.2.16", baseline)
    with pytest.raises(release.ReleaseError, match="expected patch"):
        release.validate_release(release.FILES, updated, "0.3.0", baseline)
    updated["pyproject.toml"] += 'dependencies = ["surprise"]\n'
    with pytest.raises(release.ReleaseError, match="non-version"):
        release.validate_release(release.FILES, updated, "0.2.16", baseline)


def test_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(release.time, "monotonic", lambda: 10)
    with pytest.raises(release.ReleaseError, match="Timed out"):
        release.wait_for("CI", lambda: None, deadline=9, interval=20)


def test_changed_head_cannot_be_merged(monkeypatch):
    monkeypatch.setattr(release, "pr", lambda number: {
        "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefOid": "new",
    })
    with pytest.raises(release.ReleaseError, match="changed after review"):
        release.merge(1, "reviewed", deadline=100, interval=20)


def test_read_only_mode_never_merges(monkeypatch):
    calls = []
    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("repo", "view"):
            return {"nameWithOwner": "owner/nexus"}
        if args[:2] == ("release", "view"):
            return {"tagName": "v0.2.15"}
        if args[:2] == ("pr", "list"):
            return []
        raise AssertionError(args)
    monkeypatch.setattr(release, "gh", fake_gh)
    assert release.main([]) == 0
    assert not any(args[:2] == ("pr", "merge") for args in calls)
