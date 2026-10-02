# Release process

How Nexus (`nexus-harness` on PyPI) is released, end to end: the commit convention, CI,
the release PR, publishing, how users receive the update, and what to do when something
goes wrong. The rules that bind agents are in [AGENTS.md](../AGENTS.md); this is the full
reference. History of how it was built: `git log -- docs/release.md plans/release.md`.

## Overview

1. Every change lands on `main`. There is no `develop` or `beta` branch.
2. Commit subjects follow Conventional Commits. `release-please` reads them.
3. On each push to `main`, the `release` workflow opens or updates **one** release PR
   ("chore(main): release X.Y.Z") that bumps the version and writes the changelog.
4. **Nothing ships until the release PR is merged.** Merging tags `vX.Y.Z`, creates
   the GitHub release, builds, tests the wheel, and publishes to PyPI.
5. `install.sh` and `nexus update` install the newest PyPI release.

```text
main:  fix: A ──── fix: B ──── feat: C ──── docs: D ──── [merge release PR]
                                                              │
release PR "chore(main): release 0.2.0"                       ▼
  after A:  0.1.1   CHANGELOG: fix A                   tag v0.2.0 + GitHub release
  after B:  0.1.1   CHANGELOG: fix A, fix B            uv build → wheel checks
  after C:  0.2.0   CHANGELOG: + feat C                uv publish → PyPI
  after D:  (unchanged: docs is not releasable)        `nexus update` sees 0.2.0
```

## Raising a PR and bumping the version

### Release helper

`scripts/release.py` automates a prepared patch release using `git`, authenticated
`gh`, and Python 3.13+. Run it from this checkout; it operates on the checkout's
GitHub repository. It leaves local files and branches unchanged.

```sh
.venv/bin/python scripts/release.py                       # read-only inspection
.venv/bin/python scripts/release.py --change-pr 48         # inspect a prepared fix PR
.venv/bin/python scripts/release.py --change-pr 48 --execute
.venv/bin/python scripts/release.py --execute             # existing release PR
.venv/bin/python scripts/release.py --verify 0.2.16 --execute  # resume verification after tagging
```

Prepare, commit, test, push and open the change PR first. The helper requires a
clean worktree for release execution and a `fix:` change title. It waits for
`ci-ok`, `pr-title`, all other pending checks and clean mergeability, then squash
merges using the reviewed head commit. Failed checks stop execution; rerunning
CI remains a deliberate manual action.

The helper prints the complete generated release diff and requires exactly the
four release files, matching next-patch versions, preserved previous changelog
entries, and no non-version TOML/manifest changes. A stale release PR waits for
release-please to catch up with `main`. A changed PR head stops execution.
Release-please owns all version edits, tagging and publication. The helper checks
the publishing workflow, GitHub installers/checksums/distribution assets and the
exact version and distributions on PyPI before reporting completion.

Waiting is bounded to 30 minutes by default (`--timeout` seconds, `--interval`
1–60 seconds). Re-run with the same change PR after an interrupted pre-tag run;
use `--verify VERSION --execute` once the tag exists. The helper does not create
empty trigger commits, override minor bumps, repair CI, or automatically retry
failed publishing. Offline tests and read-only GitHub inspection are verified;
end-to-end automated merging has not been live-tested.

**Never edit the version by hand.** Not in `pyproject.toml`, `uv.lock`,
`.release-please-manifest.json` or `CHANGELOG.md`. The manifest must hold the last
*released* version, the one that has a git tag. Setting it to a version that has no
tag makes release-please compute nonsense (a hand-set 0.2.1 with only `v0.2.0` tagged
produced a release PR for 0.1.2). Give the PR a Conventional Commit title and
release-please picks the bump when the maintainer merges the release PR.

| Change | PR title type | Resulting bump |
| --- | --- | --- |
| Bug fix, perf, dependency | `fix:` / `perf:` / `deps:` | patch |
| New feature or breaking change (pre-1.0) | `feat:` / `feat!:` | **minor: ask the user first** |
| Docs, tests, refactor, CI, chores | `docs:` / `test:` / `refactor:` / `ci:` / `chore:` | none |

### Agent contract: a version-bump request includes the merge

When the user asks to "bump the version", "bump the patch number", or "release",
carry out the complete workflow below. Default to **X.Y.(Z+1)** from the latest
released version. The request authorizes creating, pushing, and merging the change
PR and the release PR, then verifying publication. Do not stop after opening a PR
or ask again for merge approval. A minor bump (the second number) still requires
explicit user approval; never silently ship one.

1. Fetch `origin` and inspect the latest release/tag, `origin/main`, and open PRs.
   Preserve unrelated work. Branch from up-to-date `origin/main` for any changes.
2. Use a `fix:` commit and PR title for the requested patch release. If there is no
   code change and no pending releasable commit, an empty `fix: trigger patch
   release` commit is sufficient. Reuse an existing suitable PR rather than creating
   duplicates. Never edit version files by hand.
3. Run `.venv/bin/python -m pytest -q` and `ruff check nexus tests` for code changes.
   For documentation-only changes, review the diff and validate links. Push the
   branch and open the change PR with `gh pr create`; the title becomes the squash
   commit subject.
4. Wait for the required `ci-ok` and `pr-title` checks and confirm the PR is
   mergeable (`gh pr view N --json mergeable,mergeStateStatus`). Docs-only CI may be
   skipped by the workflow; inspect the applicable checks and branch requirements.
   Merge with `gh pr merge N --squash` when requirements are satisfied. Do not bypass
   failed checks or branch protection.
5. Wait for the `release` workflow on `main` to create or update the release PR.
   Inspect its complete diff: the expected patch version must agree in
   `pyproject.toml`, `uv.lock`, and `.release-please-manifest.json`; `CHANGELOG.md`
   must list the changes since the last release. Only those four files should change.
   If it proposes a lower or repeated version, repair the underlying manifest/tag
   mismatch through a reviewed PR. If it proposes a minor bump, obtain user approval
   or use a `Release-As: X.Y.Z` commit body to request the intended patch after
   checking the pending changes; do not edit generated version files.
6. Wait for the release PR's required checks, confirm mergeability, and **merge the
   release PR** with `gh pr merge N --squash`. This starts tagging and publication.
7. Watch the resulting `release` workflow through completion. Verify the GitHub
   tag/release and its installer, checksums, and distribution assets, and confirm
   PyPI serves the exact new version. Report the released version, both PR links
   when applicable, and publication status.

A request is complete only after the release PR is merged and publication is
verified. If authentication, permissions, checks, merge conflicts, or publishing
block progress, report the exact blocker and completed steps; do not claim the
version was released. A user who explicitly requests a dry run authorizes only
inspection and a proposed plan, without pushes, PRs, merges, tags, or publication.

## Moving parts

| Piece | File / place | Role |
| --- | --- | --- |
| Test CI | `.github/workflows/ci.yml` | Lint, tests, wheel build check. `ci-ok` is the single required status. |
| PR title check | `.github/workflows/pr-title.yml` | Fails a PR whose title is not a Conventional Commit. |
| Commit hook | `scripts/hooks/commit-msg` | Same check locally. Enable once: `git config core.hooksPath scripts/hooks`. |
| Release workflow | `.github/workflows/release.yml` | release-please + publish. **Do not rename**: PyPI trusted publishing is bound to the file name. |
| release-please config | `release-please-config.json`, `.release-please-manifest.json` | Bump rules, changelog sections, `uv.lock` updater. The manifest holds the last released version. |
| Installer check | `.github/workflows/install.yml` | Tests `install.sh`/`install.ps1` when they change, and nightly against the published script. |
| Installer | `install.sh`, `install.ps1` | Default source is PyPI. |
| Updater | `nexus update` (`nexus/cli.py`, `nexus/host_support/install.py`) | Upgrades in place, migrates old git installs. |

## Commit convention

Commit subjects and PR titles use `type(scope)!: summary`. Allowed types: `feat`, `fix`,
`perf`, `deps`, `revert`, `docs`, `chore`, `refactor`, `test`, `ci`, `style`, `build`.
Don't edit `version` in `pyproject.toml` or `CHANGELOG.md` by hand; the release PR does.

| Subject | Pre-1.0 (now) | From 1.0 |
| --- | --- | --- |
| `fix:`, `perf:`, `deps:` | patch | patch |
| `feat:` | minor | minor |
| `feat!:` or a `BREAKING CHANGE:` footer | **minor** (`bump-minor-pre-major`) | major |
| `docs:`, `chore:`, `test:`, `refactor:`, `ci:`, `style:`, `build:` | no release | no release |
| not conventional | ignored, not in the changelog | ignored |

The changelog shows Features, Bug fixes, Performance, Dependencies and Reverts. `docs`
and the other types are hidden. The largest change since the last release decides the
bump.

Direct pushes to `main` are allowed. The local `commit-msg` hook enforces the
convention, and `pr-title` is the safety net for PRs. Squash merges use the PR title as
the commit subject (repo setting: "Pull request title"), so the title is what
release-please reads.

## Test CI

`ci.yml` runs on PRs and on pushes to `main` (skipping docs-only changes: `**.md`,
`docs/`, `plans/`, `artifacts/`) and by hand.

- `test`: one Linux job on Python 3.13. `uv sync --locked --extra dev`, `ruff check`,
  then `pytest -q`. `--locked` fails when `uv.lock` is stale, which is why the release
  PR must update it.
- `build`: `uv build`, then the wheel/installer tests
  (`tests/test_model_data_package.py`, `tests/test_install_script.py`). It needs
  `pytest-asyncio` because one installer test is async.
- `ci-ok`: passes if `test` and `build` did not fail. Require this name, not the jobs.
- Four Textual pilot test files are ignored in CI (`test_ui_tui.py`, `test_mock_tui.py`,
  `test_tui_integration_render.py`, `test_tui_model_selection_integration.py`): they are
  timing and terminal-size sensitive on shared runners. Run the full suite locally
  before committing.
- There is no macOS or 3.14 job; developer machines cover them. The Playwright checks
  (`tests/playwright_*.py`) are manual.
- Ruff rules are pinned in `pyproject.toml` (`E4`, `E9`, `F`, `E713`) so a new ruff
  release cannot change what fails.
- Push runs of `ci` queue behind each other; only PR runs cancel in progress. The suite
  takes about 6 minutes.

## One-time setup (already done for this repo)

Keep this list for rebuilding the setup or moving to a new repo.

1. **GitHub App** `nexus-release`, installed only on this repo, with Contents and
   Pull requests read & write, no webhook. Store its id as the variable
   `RELEASE_APP_ID` and its private key as the secret `RELEASE_APP_PRIVATE_KEY`. A PR
   opened with the default `GITHUB_TOKEN` does not start other workflows, so without
   the app token `ci-ok` would never run on the release PR. (A fine-grained PAT with the
   same permissions works but expires and belongs to a person.)
2. **PyPI pending publisher** (pypi.org → Account → Publishing):
   project `nexus-harness`, owner `nottherealsanta`, repository `nexus`, workflow
   `release.yml`, environment `pypi`. The project name must match `pyproject.toml`
   exactly: a wrong name here made the first publish fail.
3. **GitHub environment** `pypi` (Settings → Environments). No required reviewer:
   merging the release PR publishes.
4. **Merge settings**: squash only, default message "Pull request title", delete head
   branches automatically.
5. **`main` ruleset**: active, target the default branch, restrict deletions, block
   force pushes, require status checks `ci-ok` and `pr-title`, do **not** require a pull
   request, and list the maintainer as a bypass actor ("Always") so direct pushes work.
   `pr-title` only appears in the check picker after a PR exists.
6. **Local hook**: `git config core.hooksPath scripts/hooks` in every clone.

## release-please

Config lives in `release-please-config.json`:

- `release-type: python`, `package-name: nexus-harness`, `include-component-in-tag:
  false` (tags are `v0.2.0`, the format `install.sh --version` expects).
- `bump-minor-pre-major: true` (see the table above).
- `extra-files` runs the TOML updater on `uv.lock` with the jsonpath
  `$.package[?(@.name.value=='nexus-harness')].version`, so the release PR changes
  `pyproject.toml`, `uv.lock`, `CHANGELOG.md` and `.release-please-manifest.json`.
  There is no `__version__` in the package on purpose: `package_version()` reads the
  installed metadata.
- `bootstrap-sha` stops release-please from reading history from before it was set up.

## Cutting a release

1. Keep landing `fix:` / `feat:` commits on `main`. After each push the `release`
   workflow updates the release PR (title, version, changelog).
2. When ready, open the release PR and check the diff: only `pyproject.toml`, `uv.lock`,
   `CHANGELOG.md` and the manifest change, and `ci-ok` is green. Edit the PR text now if
   you want to; the next push to `main` regenerates it.
3. Merge it. For an authorized version-bump request, the agent performs this step
   without asking for a second approval. The same workflow run then:
   1. release-please creates tag `vX.Y.Z` and the GitHub release (changelog as notes).
   2. `publish` checks out the tag and verifies the tag equals the `pyproject.toml`
      version.
   3. `uv build`, then the wheel test step (web, TUI and agent assets are in the wheel).
   4. `uv publish --trusted-publishing always` (OIDC, no stored token; the `pypi`
      environment).
   5. Uploads `install.sh`, `install.ps1`, `SHA256SUMS` and the dists to the GitHub
      release.
4. Verify:
   - pypi.org/project/nexus-harness shows the new version
   - the GitHub release has the installer, `SHA256SUMS` and dists
   - `uv tool install nexus-harness && nexus --version` on a clean machine prints the
     version (PyPI's index can lag a minute or two, so `nexus update` may not see a
     brand-new release right away; `uv tool upgrade` has no refresh flag, so retry)
   - Python 3.12 or older refuses the wheel with "requires Python >=3.13"

### Overrides

- **Force a version:** a commit body line `Release-As: 1.0.0`. (The first release,
  0.1.0, used this with the manifest at `0.0.0`.)
- **Hotfix:** push the `fix:` commit, then merge the release PR straight away.
- **Force a release without a code change:** an empty `fix:` commit.

## When something goes wrong

| Problem | What to do |
| --- | --- |
| Publish failed after the tag exists | Actions → `release` → Run workflow with the tag (`v0.2.0`). Only `publish` runs. `uv publish` refuses a version PyPI already has, which is the safe outcome. |
| Publish fails with a trusted-publisher error | Compare the pending/active publisher on PyPI with the table in "One-time setup": project name, owner, repo, `release.yml`, `pypi`. |
| A bad release reached users | PyPI versions are immutable. Yank it on pypi.org (hides it from new resolves), then ship a `fix:` release. Users pin with `nexus update --version X` or `install.sh --version X`. |
| The release PR does not update | Check that the `release` run on `main` succeeded, and that the commits since the last tag are releasable types. Non-conventional subjects are ignored. |
| `ci-ok` does not run on the release PR | The app token is not being used (missing `RELEASE_APP_ID` or `RELEASE_APP_PRIVATE_KEY`). |
| `uv sync --locked` fails on the release PR | The `uv.lock` updater did not match. Run `uv lock` on the release branch and push. |
| A fix is missing from the changelog | Its subject was not conventional. The next release will not include it; write it up in the release PR text. |
| Scheduled workflows stopped | Public repos disable them after 60 days without activity. Re-enable from the Actions tab. |

## How users get the update

- **Installer** (`install.sh`, `install.ps1`): `NEXUS_SOURCE` defaults to `pypi`;
  `--version X` means `nexus-harness==X`. `--source git` keeps working with `--git-ref`
  (default `main`); with the git source `--version X` means tag `vX`.
- **`nexus update [--channel {stable,git}] [--ref R] [--version V] [--no-restart]`.**
  `install_source()` reads `direct_url.json` to tell `pypi`, `git`, `path` or `unknown`
  installs apart; `install_method()` says how (`uv-tool`, `editable`, `pip`).

  | Situation | Command |
  | --- | --- |
  | PyPI install, stable | `uv tool upgrade nexus-harness` |
  | Git install, stable (**migration**) | `uv tool install --force --refresh-package nexus-harness --python X.Y "nexus-harness[extras]"` (`--refresh-package` is valid on `install`, not on `upgrade`) |
  | `--version V` | `uv tool install --force --python X.Y "nexus-harness[extras]==V"` |
  | `--channel git [--ref R]` | `uv tool install --force --reinstall --python X.Y "nexus-harness[extras] @ git+https://github.com/nottherealsanta/nexus@R"` |
  | path / unknown source | refused; re-run the installer or use `--channel git` |

  Extras are read back from `uv-receipt.toml` (bounded to 64 KiB) and the running
  Python's `X.Y` is passed, so an update keeps both. The migration prints
  `Moving this install from git to PyPI releases (use --channel git to stay on git).`
  Editable and non-uv installs are refused as before. The daemon is restarted after the
  upgrade unless `--no-restart`. `--ref` needs `--channel git`; `--version` and
  `--channel git` cannot be combined. `update --version` stores into `dest="release"`
  because the top-level `--version` shares the name.
- **Developers** who want unreleased code use `nexus update --channel git`.

## Installer CI

- `install.yml` runs the installer matrix (macOS, Linux, two Docker images) only when
  an installer file changes, installing **this commit** with `--source git --git-ref
  "$GITHUB_SHA"` so unreleased code is tested.
- Nightly it runs only `published-script`: the real one-liner against PyPI, then
  `nexus --version` and `nexus update`. That catches a broken published script or
  release.

## Requirements and gotchas

- **Python 3.13 is the floor** (`requires-python = ">=3.13"`, `MIN_PYTHON`, installer
  default). `object.__setattr__` on a msgspec `Struct` raises `TypeError` on 3.11 and
  3.12; it is used in 33 places in 6 files. Supporting older Pythons means reworking
  those sites.
- The suite once passed only on the maintainer's Mac: avoid hard-coded timezones and
  temp-dir-length assumptions in tests.
- `_default_spawn` in `nexus/host/daemon.py` sends daemon stdout/stderr to `/dev/null`,
  so a crash before the log opens shows only "daemon exited before readiness". Run the
  daemon in the foreground to see it.
- Workflow action versions: `release.yml` uses `create-github-app-token@v3` and
  `release-please-action@v5`. `ci.yml` and `install.yml` still use `checkout@v4` and
  `setup-uv@v5` (Node 20 warnings only); bump them together.
- Actions cost: the repo is public, so minutes are free. If it goes private, the setup
  fits the 2,000-minute free tier (one Linux test job of about 6 minutes per push).

## Not planned

- No `develop`/`beta` branch or pre-release channel. PyPI pre-releases
  (`0.4.0b1`, `nexus update --pre`) could be added without changing the flow.
- No version derived from git tags (setuptools-scm); the version is a literal in
  `pyproject.toml` that release-please edits.
- No Homebrew, apt or Scoop packages.
- An "update available" notice (a cached, opt-out PyPI check surfaced in `nexus
  --version`, `nexus doctor` and the top bar of both surfaces, through a host
  `update_status` command) is designed but **not built**.

### Native terminal packaging on the migration branch

`feat/ratatui-prototype` adds a `setuptools-rust` binary build to the existing
backend. Wheels contain the Python package/assets plus `nexus-ratatui` in the
wheel's scripts area. The sdist contains Cargo.toml, Cargo.lock and Rust sources;
source installs require Rust ≥1.88 and the Cargo dependencies. Native wheels are
platform-specific. Local macOS arm64 build/install is verified; a complete wheel
matrix and Linux portability verification are still required before publishing
this branch. Release-please still owns the Python version.


Native terminal wheels are built by the reusable `native-wheels.yml` workflow
on Linux and macOS, each with x86-64 and arm64 runners, for Python 3.13 and
3.14. Cibuildwheel repairs Linux platform tags in manylinux/musllinux containers
and verifies the installed executable with `nexus-ratatui --version`. The release
publisher waits for every matrix job, downloads those wheels, and builds only
the source distribution locally. A failed platform job prevents publication.
CI also runs locked Cargo tests and the controlling-PTY Python check. The matrix
configuration has not yet been run on hosted CI; only the local macOS arm64
wheel has been installed and verified. Windows remains unsupported by the
installer.

On pull requests the wheel matrix runs only when native inputs change (`rust/`,
`pyproject.toml`, `MANIFEST.in`, `uv.lock`, `nexus/ui/ratatui/`, or the CI and
wheel workflows), decided by the `changes` job in `ci.yml`; pushes to `main` and
releases always run it, and a skipped matrix does not fail `ci-ok`. Building
from source (no matching wheel) needs a Rust toolchain of at least 1.88
(`rust-version` in `rust/tui/Cargo.toml`; install with rustup.rs). Without Rust,
setuptools-rust says so; with an older compiler Cargo names the minimum. Packaging follows the [setuptools-rust wheel guidance](https://setuptools-rust.readthedocs.io/en/latest/building_wheels.html).
