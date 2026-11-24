"""`nexus update`: install a newer Nexus from a release or from a checkout.

The command refuses anything it cannot classify from PEP 610 install metadata. A release install hands off to the release's
own `install.sh`, pinned to the tag that was checked and verified against that tag's `SHA256SUMS`; the installer checks every
payload again. A checkout install only fast-forwards its upstream branch, then hands off to the checkout's `install.sh
--source`. Both installers restart a running Core daemon. The running process is replaced with `exec` so it is gone before
the installer rewrites the tool it runs from.

`--check` and `--json` stay report-only and never touch the install.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

import httpx

from . import doctor

RELEASE_DOWNLOAD = "https://github.com/nottherealsanta/nexus/releases/download"
_INSTALLER_URL = "https://github.com/nottherealsanta/nexus/releases/latest/download/install.sh"
_TAG = re.compile(r"v[0-9]+(?:\.[0-9]+)+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CHECKSUM_BYTES = 1 << 20
_MAX_INSTALLER_BYTES = 1 << 20
_TIMEOUT = 30.0

INSTRUCTIONS = (
    "Reinstall with install.sh, then run `nexus update` again:\n"
    f"  release:  curl -LsSf {_INSTALLER_URL} | sh\n"
    "  checkout: cd /path/to/nexus && ./install.sh --source"
)


class UpdateError(ValueError):
    """An update that was refused before anything was changed."""


@dataclass(frozen=True)
class Install:
    """How the running `nexus` was installed, as far as its PEP 610 metadata says."""

    kind: str  # "release", "checkout" or "unknown"
    root: Path | None = None
    detail: str = ""


@dataclass(frozen=True)
class Checkout:
    """A clean checkout and how far its branch is from its upstream."""

    root: Path
    branch: str
    upstream: str
    behind: int
    ahead: int
    head: str


def _unknown(detail: str) -> Install:
    return Install("unknown", detail=detail)


def _local_path(url: str | None) -> Path | None:
    parsed = urlparse(url or "")
    if parsed.scheme != "file":
        return None
    return Path(url2pathname(unquote(parsed.path))).resolve()


def _checkout_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if ((candidate / ".git").exists() and (candidate / "install.sh").is_file()
                and (candidate / "nexus-core" / "nexus").is_dir()):
            return candidate
    return None


def installed() -> Install:
    """Classify this install from the `nexus10` distribution's `direct_url.json`."""
    try:
        distribution = importlib.metadata.distribution("nexus10")
        raw = distribution.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return _unknown("Nexus is not installed as the `nexus10` distribution")
    except Exception as exc:  # noqa: BLE001 - a broken environment is not something an update may guess about
        return _unknown(f"could not read Nexus's install metadata: {exc}")
    if not raw:
        return _unknown("Nexus has no install source recorded (direct_url.json)")
    try:
        data = json.loads(raw)
    except ValueError:
        return _unknown("Nexus's install source (direct_url.json) is not valid JSON")
    if not isinstance(data, dict):
        return _unknown("Nexus's install source (direct_url.json) is not an object")
    if "dir_info" in data:
        path = _local_path(data.get("url"))
        if path is None:
            return _unknown("Nexus was installed from a non-local directory source")
        root = _checkout_root(path)
        if root is None:
            return _unknown(f"{path} is no longer a Nexus checkout with install.sh")
        return Install("checkout", root=root, detail=str(root))
    if "vcs_info" in data:
        return _unknown("Nexus was installed from a Git URL; this updater cannot verify its branch")
    if "archive_info" in data:
        return Install("release", detail="a release wheel")
    return _unknown("Nexus's install source (direct_url.json) has an unknown shape")


def _git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError as exc:
        raise UpdateError(f"git is not available: {exc}") from exc
    if done.returncode:
        message = (done.stderr or done.stdout).strip().splitlines()
        raise UpdateError(message[-1] if message else f"git {' '.join(args)} failed")
    return done.stdout.strip()


def _git_optional(root: Path, *args: str) -> str:
    """A git query whose failure means "no answer" (a detached HEAD, no upstream)."""
    try:
        return _git(root, *args)
    except UpdateError:
        return ""


def _in_progress(root: Path) -> str | None:
    for marker, name in (
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ):
        path = Path(_git(root, "rev-parse", "--git-path", marker))
        if not path.is_absolute():
            path = root / path
        if path.exists():
            return name
    git_dir = Path(_git(root, "rev-parse", "--git-path", "rebase-merge"))
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    if git_dir.exists():
        return "rebase"
    git_dir = Path(_git(root, "rev-parse", "--git-path", "rebase-apply"))
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    return "rebase" if git_dir.exists() else None


def checkout_state(root: Path, fetch: bool = False) -> Checkout:
    """Refuse unsafe checkouts before looking at upstream, then (optionally) fetch and compare."""
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    branch = _git_optional(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise UpdateError("the checkout is on a detached HEAD; switch to a branch and run `nexus update` again")
    upstream = _git_optional(top, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream:
        raise UpdateError(f"branch {branch} has no upstream; set one with `git branch --set-upstream-to`")
    operation = _in_progress(top)
    if operation:
        raise UpdateError(f"a {operation} is in progress in {top}; finish or abort it, then run `nexus update` again")
    dirty = _git(top, "status", "--porcelain", "--untracked-files=normal")
    if dirty:
        raise UpdateError(
            f"the checkout at {top} has local changes (including untracked files); commit, stash or remove them first.\n"
            "Nothing was changed."
        )
    if fetch:
        remote = _git_optional(top, "config", f"branch.{branch}.remote")
        if remote != ".":  # "." is a local upstream: there is nothing to fetch
            if not remote:
                remote = upstream.split("/", 1)[0]
            if remote.startswith("-"):
                raise UpdateError(f"branch {branch} names an unexpected remote {remote!r}; nothing was changed")
            _git(top, "fetch", "--quiet", "--", remote)
    behind = int(_git(top, "rev-list", "--count", f"HEAD..{upstream}")) if fetch else 0
    ahead = int(_git(top, "rev-list", "--count", f"{upstream}..HEAD")) if fetch else 0
    head = _git(top, "rev-parse", "--short", "HEAD")
    return Checkout(top, branch, upstream, behind, ahead, head)


def _confirm(action: str, yes: bool) -> bool:
    print(action)
    prefix = os.environ.get("PREFIX") or str(Path.home() / ".local")
    print(f"Installer prefix: {prefix} (set PREFIX if Nexus was installed elsewhere).")
    if yes:
        return True
    if not sys.stdin.isatty():
        raise UpdateError("confirmation needs a terminal; pass --yes to update without a prompt")
    return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


def _exec_installer(script: Path, *args: str) -> None:
    """Replace this process with the installer, which restarts a running daemon at the end."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("sh", ["sh", str(script), *args])


def _exec_installer_bytes(content: bytes, *args: str) -> None:
    """Run an already-verified installer from an unlinked temporary file."""
    fd, name = tempfile.mkstemp(prefix="nexus-update-", suffix=".sh")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        fd = os.open(name, os.O_RDONLY)
        os.unlink(name)
        if fd != 0:
            os.dup2(fd, 0)
            os.close(fd)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvp("sh", ["sh", "-s", "--", *args])
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _download(url: str, limit: int) -> bytes:
    """Fetch a small release asset, refusing anything over `limit`."""
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise UpdateError(f"{url} is larger than expected ({limit} bytes); nothing was installed")
            return bytes(body)
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not download {url}: {exc}") from exc


def _expected_sha(sums: bytes, name: str) -> str:
    for line in sums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        listed = parts[1].lstrip("*")
        if listed == name:
            digest = parts[0].lower()
            if not _SHA256.fullmatch(digest):
                raise UpdateError(f"SHA256SUMS lists an invalid checksum for {name}; nothing was installed")
            return digest
    raise UpdateError(f"this release's SHA256SUMS does not list {name}; nothing was installed")


def release_installer(tag: str) -> bytes:
    """Download and checksum-verify one release's `install.sh`, pinned to `tag`."""
    if not _TAG.fullmatch(tag):
        raise UpdateError(f"refusing an unexpected release tag {tag!r}")
    base = f"{RELEASE_DOWNLOAD}/{quote(tag, safe='')}"
    sums = _download(f"{base}/SHA256SUMS", _MAX_CHECKSUM_BYTES)
    expected = _expected_sha(sums, "install.sh")
    installer = _download(f"{base}/install.sh", _MAX_INSTALLER_BYTES)
    actual = hashlib.sha256(installer).hexdigest()
    if actual != expected:
        raise UpdateError("the downloaded install.sh does not match this release's SHA256SUMS; nothing was installed")
    return installer


def update_release(report: dict, release: dict | None, yes: bool) -> int:
    state = report.get("state")
    if state in ("none", "unknown"):
        raise UpdateError(str(report.get("detail") or "could not find a release to install"))
    if state != "available":
        print(report.get("detail") or "Nexus is up to date")
        return 0
    tag = str((release or {}).get("tag_name") or "")
    if not _TAG.fullmatch(tag):
        raise UpdateError(f"the latest release has an unexpected tag {tag!r}; nothing was installed")
    action = (
        f"Install Nexus {tag} from its release (all assets are checksum-verified) and restart a running Core daemon "
        "(active turns are interrupted)?\n"
        "If the restart is interrupted, the daemon stays stopped; run `nexus daemon start` to start it."
    )
    if not _confirm(action, yes):
        print("Nothing changed.")
        return 0
    installer = release_installer(tag)
    _exec_installer_bytes(installer, "--release", tag)
    return 0  # not reached


def update_checkout(root: Path, yes: bool) -> int:
    state = checkout_state(root)  # refuses dirty, detached, diverged-in-progress or upstream-less checkouts first
    script = state.root / "install.sh"
    if not script.is_file():
        raise UpdateError(f"{script} is missing; nothing was changed")
    action = (
        f"Fast-forward {state.branch} to {state.upstream} in {state.root}, then rebuild and reinstall from this checkout "
        "(this can take several minutes) and restart a running Core daemon (active turns are interrupted)?\n"
        "If the restart is interrupted, the daemon stays stopped; run `nexus daemon start` to start it."
    )
    if not _confirm(action, yes):
        print("Nothing changed.")
        return 0
    state = checkout_state(state.root, fetch=True)
    if state.ahead:
        raise UpdateError(
            f"{state.branch} has commits that are not on {state.upstream}; refusing to discard them.\nNothing was changed."
        )
    if not state.behind:
        print(f"{state.branch} is up to date with {state.upstream} ({state.head}). Reinstalling from this checkout.")
    else:
        _git(state.root, "merge", "--ff-only", "--quiet", "--", state.upstream)
        head = _git(state.root, "rev-parse", "--short", "HEAD")
        print(f"Fast-forwarded {state.branch} to {state.upstream} ({state.head} -> {head}). Reinstalling from this checkout.")
    _exec_installer(script, "--source")
    return 0  # not reached


def apply(current: str, yes: bool = False) -> int:
    """Update this install, or refuse with an explicit reason. `current` is the running version."""
    install = installed()
    if install.kind == "unknown":
        raise UpdateError(f"{install.detail}.\n{INSTRUCTIONS}\nNothing was changed.")
    if install.kind == "checkout" and install.root is not None:
        return update_checkout(install.root, yes)
    report, release = doctor.check_release(current)
    return update_release(report, release, yes)


def report(current: str, as_json: bool = False) -> int:
    """The non-mutating check: the only mode `--check` and `--json` use."""
    result = doctor.update_check(current)
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    print(result.get("detail") or "could not check for a release")
    if result.get("state") == "available":
        if result.get("url"):
            print(result["url"])
        print("Run `nexus update` to install it; `--check` and `--json` only report.")
    return 0


def run(current: str, check_only: bool = False, as_json: bool = False, yes: bool = False) -> int:
    if check_only or as_json:
        return report(current, as_json=as_json)
    return apply(current, yes=yes)
