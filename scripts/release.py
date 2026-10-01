#!/usr/bin/env python3
"""Drive a prepared patch release through GitHub gates (docs/release.md).

Read-only by default. Uses git/gh and the standard library; release-please
remains the sole owner of version files, tags and publishing.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {"pyproject.toml", "uv.lock", ".release-please-manifest.json", "CHANGELOG.md"}
REQUIRED = {"ci-ok", "pr-title"}


class ReleaseError(RuntimeError):
    """An actionable release blocker; never bypass it."""


def run(*args: str, allowed_codes=(0,)) -> str:
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=120)
    if result.returncode not in allowed_codes:
        raise ReleaseError(result.stderr.strip() or result.stdout.strip() or f"{args[0]} failed")
    return result.stdout.strip()


def gh(*args: str):
    codes = (0, 1, 8) if args[:2] == ("pr", "checks") else (0,)
    return json.loads(run("gh", *args, allowed_codes=codes))


def wait_for(label, probe, deadline, interval):
    while True:
        value = probe()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise ReleaseError(f"Timed out waiting for {label}. Inspect GitHub and resume with the same arguments.")
        print(f"Waiting for {label}…", flush=True)
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def patch_after(tag: str) -> str:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        raise ReleaseError(f"Latest release tag is not a stable version: {tag}")
    major, minor, patch = map(int, match.groups())
    return f"{major}.{minor}.{patch + 1}"


def pr(number):
    return gh("pr", "view", str(number), "--json",
              "number,title,url,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,mergeStateStatus,mergeCommit,files")


def validate_checks(checks):
    failed = [row["name"] for row in checks if row["state"] in
              {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}]
    if failed:
        raise ReleaseError("Checks failed: " + ", ".join(failed) + ". Inspect the run; reruns are manual.")
    passed = {row["name"] for row in checks if row["state"] == "SUCCESS"}
    return REQUIRED <= passed and all(row["state"] in {"SUCCESS", "SKIPPED", "NEUTRAL"} for row in checks)


def merge(number, head, deadline, interval):
    def ready():
        current = pr(number)
        if current["state"] == "MERGED":
            return current
        if current["state"] != "OPEN" or current["isDraft"] or current["baseRefName"] != "main":
            raise ReleaseError(f"PR #{number} must be open, non-draft and target main.")
        if current["headRefOid"] != head:
            raise ReleaseError(f"PR #{number} changed after review. Run the script again to review the new head.")
        if current["mergeable"] == "CONFLICTING":
            raise ReleaseError(f"PR #{number} has merge conflicts.")
        checks = gh("pr", "checks", str(number), "--json", "name,state")
        if validate_checks(checks) and current["mergeable"] == "MERGEABLE" and current["mergeStateStatus"] == "CLEAN":
            return current
        return None

    current = wait_for(f"PR #{number} checks and mergeability", ready, deadline, interval)
    if current["state"] != "MERGED":
        run("gh", "pr", "merge", str(number), "--squash", "--match-head-commit", head)
        current = pr(number)
    print(f"Merged {current['url']}", flush=True)
    return current


def read_at(repo, ref, path):
    value = gh("api", f"repos/{repo}/contents/{path}?ref={ref}")
    if value.get("encoding") != "base64":
        raise ReleaseError(f"Cannot read {path} at {ref}.")
    return base64.b64decode(value["content"]).decode()


def validate_release(files, texts, version, baseline):
    if set(files) != FILES:
        raise ReleaseError("Release PR must change exactly: " + ", ".join(sorted(FILES)))
    project = tomllib.loads(texts["pyproject.toml"])["project"]["version"]
    packages = tomllib.loads(texts["uv.lock"])["package"]
    locked = [row["version"] for row in packages if row["name"] == "nexus-harness"]
    manifest = json.loads(texts[".release-please-manifest.json"])["."]
    if project != version or locked != [version] or manifest != version:
        raise ReleaseError(f"Generated versions must all equal the expected patch {version}.")
    # A generated bump must not smuggle dependency/configuration changes.
    for path in ("pyproject.toml", "uv.lock", ".release-please-manifest.json"):
        parse = json.loads if path.endswith(".json") else tomllib.loads
        before, after = parse(baseline[path]), parse(texts[path])
        if path == "pyproject.toml":
            after["project"]["version"] = before["project"]["version"]
        elif path == "uv.lock":
            previous = next(row["version"] for row in before["package"] if row["name"] == "nexus-harness")
            next(row for row in after["package"] if row["name"] == "nexus-harness")["version"] = previous
        else:
            after["."] = before["."]
        if before != after:
            raise ReleaseError(f"Unexpected non-version changes in {path}.")
    old_notes = baseline["CHANGELOG.md"].partition("\n")[2]
    if not texts["CHANGELOG.md"].endswith(old_notes):
        raise ReleaseError("Release PR rewrites existing changelog entries.")
    if f"## [{version}]" not in texts["CHANGELOG.md"]:
        raise ReleaseError(f"Changelog is missing {version}.")


def release_pr():
    rows = gh("pr", "list", "--base", "main", "--state", "open", "--limit", "100",
              "--json", "number,title,headRefName")
    rows = [row for row in rows if row["headRefName"].startswith("release-please--")]
    if len(rows) > 1:
        raise ReleaseError("Multiple release PRs are open; select the correct one manually.")
    return pr(rows[0]["number"]) if rows else None


def verify_publication(repo, version, sha, deadline, interval):
    def published():
        runs = gh("run", "list", "--workflow", "release", "--commit", sha, "--limit", "10",
                  "--json", "databaseId,status,conclusion,url")
        if not runs:
            return None
        latest = runs[0]
        if latest["status"] != "completed":
            return None
        if latest["conclusion"] != "success":
            raise ReleaseError(f"Publication workflow failed: {latest['url']}")
        jobs = gh("run", "view", str(latest["databaseId"]), "--json", "jobs")["jobs"]
        if not any(job["name"] == "publish" and job["conclusion"] == "success" for job in jobs):
            raise ReleaseError("Release workflow completed without successful publication.")
        release = gh("release", "view", f"v{version}", "--json", "tagName,url,assets,isDraft,isPrerelease")
        names = {asset["name"] for asset in release["assets"]}
        expected = {"install.sh", "install.ps1", "SHA256SUMS",
                    f"nexus_harness-{version}-py3-none-any.whl", f"nexus_harness-{version}.tar.gz"}
        if release["isDraft"] or release["isPrerelease"] or not expected <= names:
            raise ReleaseError("GitHub release is incomplete or missing expected assets.")
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/nexus-harness/{version}/json", timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if data["info"]["version"] != version:
            raise ReleaseError("PyPI returned a different version.")
        distributions = {row["filename"] for row in data["urls"]}
        if not expected - {"install.sh", "install.ps1", "SHA256SUMS"} <= distributions:
            raise ReleaseError("PyPI is missing a distribution.")
        print(f"Verified PyPI: https://pypi.org/project/nexus-harness/{version}/", flush=True)
        return release["url"]

    return wait_for("GitHub and PyPI publication", published, deadline, interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--change-pr", type=int, help="Prepared, tested fix PR to merge first (also accepts an already merged PR)")
    parser.add_argument("--verify", help="Resume publication verification for an already tagged version, e.g. 0.2.16")
    parser.add_argument("--execute", action="store_true", help="Merge reviewed PRs and verify publication; default only inspects")
    parser.add_argument("--timeout", type=int, default=1800, help="Total waiting limit in seconds (default: 1800)")
    parser.add_argument("--interval", type=int, default=20, help="Polling interval in seconds (default: 20)")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not 1 <= args.interval <= 60:
        parser.error("timeout must be positive; interval must be between 1 and 60")
    repo = gh("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    if args.verify:
        if not re.fullmatch(r"\d+\.\d+\.\d+", args.verify) or args.change_pr:
            parser.error("--verify requires a stable version and cannot be combined with --change-pr")
        if not args.execute:
            print(f"Inspection only. Would verify publication of v{args.verify}.")
            return 0
        sha = gh("api", f"repos/{repo}/commits/v{args.verify}")["sha"]
        print(verify_publication(repo, args.verify, sha, time.monotonic() + args.timeout, args.interval))
        return 0
    latest = gh("release", "view", "--json", "tagName")["tagName"]
    version = patch_after(latest)
    print(f"Repository: {repo}\nLatest release: {latest}\nExpected patch: {version}", flush=True)
    change = pr(args.change_pr) if args.change_pr else None
    if change:
        if not re.match(r"fix(?:\([^()]+\))?: ", change["title"]):
            raise ReleaseError("Change PR must have a fix: title for a patch release.")
        print(f"Change PR: {change['url']} ({change['state']})", flush=True)
    pending = release_pr()
    if pending:
        print(f"Release PR: {pending['url']} — {pending['title']}", flush=True)
    if not args.execute:
        if pending:
            print(run("gh", "pr", "diff", str(pending["number"])), flush=True)
        print("Inspection only. Use --execute to wait for checks, merge and verify publication.")
        return 0
    if run("git", "status", "--porcelain"):
        raise ReleaseError("Working tree is dirty. Commit and test changes before executing a release.")
    if not change and not pending:
        raise ReleaseError("No release PR exists. Supply --change-pr for a prepared fix PR; no empty commit is created automatically.")
    deadline = time.monotonic() + args.timeout
    if change:
        merge(change["number"], change["headRefOid"], deadline, args.interval)
    # release-please may still be updating a prior release PR after the merge.
    def updated():
        candidate = release_pr()
        if candidate is None:
            return None
        head = candidate["headRefOid"]
        base = gh("api", f"repos/{repo}/commits/main")["sha"]
        comparison = gh("api", f"repos/{repo}/compare/{base}...{head}")
        if comparison["merge_base_commit"]["sha"] != base:
            return None
        candidate["reviewedBase"] = base
        return candidate
    pending = wait_for("release-please PR based on current main", updated, deadline, args.interval)
    head = pending["headRefOid"]
    print(run("gh", "pr", "diff", str(pending["number"])), flush=True)
    texts = {path: read_at(repo, head, path) for path in FILES}
    baseline = {path: read_at(repo, pending["reviewedBase"], path) for path in FILES}
    validate_release([row["path"] for row in pending["files"]], texts, version, baseline)
    if pending["title"] != f"chore(main): release {version}":
        raise ReleaseError(f"Expected patch release {version}; inspect pending changes before requesting an override.")
    released = merge(pending["number"], head, deadline, args.interval)
    print(verify_publication(repo, version, released["mergeCommit"]["oid"], deadline, args.interval))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"Release stopped: {exc}", file=sys.stderr)
        sys.exit(1)
