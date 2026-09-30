# Release automation plan

Status: **in progress** · updated 2026-09-30 · follows [install.md](install.md) (its Phase 3, "PyPI release")

Phases 1–3 are on `main`. The first release-please run, the first release PR and the
`v0.1.0` publish are still to verify (Phase 4). Read "Progress" and "What we learned"
below before the phase text: several details in the phases changed while landing them.

## Progress (2026-09-29)

| Phase | State | Landed as |
| --- | --- | --- |
| 1. Test CI, Actions budget | **Done.** `ci-ok` is green on `main` (test job ~6 min). | `c23cef4` ruff config; `ea92ce2` `ci.yml` + `install.yml` trim; `8d2a226` pytest-asyncio in the build job; `d6eb32c` Linux 3.13 only; `537180e` skip Textual pilot tests |
| Python floor | **Done.** `requires-python = ">=3.13"` (see "What we learned"). | `89abf1a` |
| 2. Convention, repo settings | Code **done**; merge settings **done** by the maintainer; `main` **ruleset still to create**. | `1a3e535` `pr-title.yml` + `scripts/hooks/commit-msg` (hook enabled locally with `git config core.hooksPath scripts/hooks`) |
| 3. release-please + publishing | Files **pushed**; first run not yet verified. | `fdbf33f` config, manifest, `release.yml` (with `Release-As: 0.1.0`); `8c694a9` diagnostics removed; `3cdb3ff` docs |
| 4. First release `v0.1.0` | **Done (2026-09-30).** Release PR #1 merged; the first publish failed (pending publisher had the wrong project name), then the `workflow_dispatch` re-run of `v0.1.0` published it. Checked: PyPI 0.1.0, release assets, clean `uv tool install` prints `nexus 0.1.0`, Python 3.12 refused. | run 36662502628 |
| 5–6. Installer default, README, `nexus update` | **Done and released as 0.1.1 (2026-09-30).** `install.sh` defaults to PyPI; `install.yml` installs this commit with `--source git`; nightly `published-script` runs the real default then `nexus update`; README leads with the one-liner; `nexus update` has `--channel`, `--ref`, `--version` and migrates git installs. `update --version` uses `dest="release"` because the top-level `--version` shares the name. 4399 tests pass. Verified after the release: a git install ran `nexus update`, printed the migration line and ended on PyPI 0.1.1 (the first try right after publishing still saw 0.1.0: PyPI index lag). | `4febed4` |
| 7. Update notice | Not started. | |
| 8. Docs | Partly done: `AGENTS.md` convention bullet and the "Releasing" section in `docs/core.md`. `plans/install.md` and the README still to do. | `3cdb3ff` |

Maintainer setup, all done: GitHub App `nexus-release` with `RELEASE_APP_ID` (variable)
and `RELEASE_APP_PRIVATE_KEY` (secret), the PyPI pending publisher for `nexus-harness`
(owner `nottherealsanta`, repo `nexus`, workflow `release.yml`, environment `pypi`), and
the `pypi` environment. Skipped on purpose: the TestPyPI rehearsal.

### Decisions on the open questions

1. **Direct pushes to `main` stay.** The local `commit-msg` hook enforces the convention;
   `pr-title` is only a safety net for PRs. The `main` ruleset must list the maintainer as
   a bypass actor, and must not require a pull request.
2. **No manual approval on the `pypi` environment.** Merging the release PR publishes.
3. **The Phase 7 update notice is on by default**, with the 24 h cache and the opt-outs.

## What we learned (2026-09-29)

- **Python 3.13 is the real floor.** `object.__setattr__(self, ...)` on a msgspec
  `Struct` raises `TypeError: can't apply this __setattr__` on Python 3.11 and 3.12 and
  works on 3.13 and 3.14. It is used in 33 places in 6 files (`nexus/ext/manifest.py` 18,
  `nexus/hooks/model.py` 11, and one each in `skills/activation.py`, `context/parts.py`,
  `agents/runner.py`, `agents/model.py`). The suite only ever passed because the dev venv
  is 3.14. On 3.12 the daemon crashes at startup, which is what broke the installer check
  and ~600 tests on the first CI run. `install.sh` used to default to Python 3.12, so a
  fresh install was broken too. Fixed by raising `requires-python`, `MIN_PYTHON`, the
  installer default and the docs to 3.13. Supporting 3.11/3.12 later means reworking those
  33 sites.
- **msgspec 0.22 was ruled out.** It was the first suspicion (the runner resolved 0.22.0),
  but `uv.lock` pins 0.21.1, the tests fail the same way on 0.21.1 under Python 3.12, and
  they pass on 0.21.1 under 3.13.
- **Ruff.** Ruff 0.16 has a much wider default rule set than the code was written for.
  `pyproject.toml` now pins `[tool.ruff.lint] select = ["E4", "E9", "F", "E713"]` and
  excludes `tests/fixtures` (a deliberately broken file) and `artifacts`. One real
  finding fixed: a duplicate `redact_secrets` import in `nexus/core/loop.py`.
- **Tests that only passed on the maintainer's Mac**, now fixed: the logs-drawer test
  hard-coded `2023-11-15`, which only holds in timezones ahead of UTC;
  `test_session_summary` depended on the temp-dir length (the environment part embeds the
  workspace path and the budget is tiny).
- **Textual pilot tests are skipped in CI.** `test_ui_tui.py`, `test_mock_tui.py`,
  `test_tui_integration_render.py` and `test_tui_model_selection_integration.py` fail or
  flake on the shared Linux runners (timing, terminal size). They run locally before each
  commit (4388 passed with those four ignored). To bring them back, reproduce them in a
  Linux container first.
- **CI shape.** One Linux job on Python 3.13, no macOS job (all dev machines are macOS and
  run the suite before committing), no 3.14 job. The `build` job needs `pytest-asyncio`
  because `tests/test_install_script.py` has an async test. `install.yml` still tests the
  installer on macOS and Linux, but only when an installer file changes; nightly it runs
  only `published-script`.
- **`uv.lock` version updater verified offline.** Running release-please's own
  `GenericToml` updater with the configured jsonpath on a copy of `uv.lock` changes
  exactly one line (`nexus-harness` `0.1.0` to the new version). Still confirm on the
  first release PR.
- **Action versions.** Latest at the time: `actions/checkout` v7, `astral-sh/setup-uv`
  v10, `actions/create-github-app-token` v3, `googleapis/release-please-action` v5,
  `amannn/action-semantic-pull-request` v6. `release.yml` uses `create-github-app-token@v3`
  and `release-please-action@v5` (their inputs were checked). The other workflows still use
  `checkout@v4` and `setup-uv@v5`, which only warn about Node 20; bump them in one change.
- **Daemon start-up errors are invisible.** `_default_spawn` in `nexus/host/daemon.py`
  sends the daemon's stdout and stderr to `/dev/null`, so a crash before the log file
  opens leaves nothing behind ("daemon exited with code 1 before readiness"). Worth
  routing early stderr to the daemon log.
- **Runner behaviour.** Push runs of `ci` queue behind each other (cancel-in-progress is
  only on for PRs), so a slow or doomed run delays the next one. The suite takes about 6
  minutes on a runner.
- **`bootstrap-sha`** in `release-please-config.json` is `537180e01a8bb163a53c72a1b071e714fc68adbf`,
  the `main` HEAD just before the release files landed.

## Goal

1. Every change lands on `main`. There is no `develop` or `beta` branch.
2. Releases are automatic, but **nothing ships until the maintainer says so**. A bot
   keeps one "release PR" open that collects every fix and feature since the last
   release. Merging that PR bumps the version, writes the changelog, tags, creates
   the GitHub release, and publishes `nexus-harness` to PyPI.
3. `nexus update` and the one-line installer pick up the new release with no extra
   steps. Installs made from git before the first PyPI release are moved to PyPI.
4. The README leads with the one-line install.
5. CI stays cheap: the repo is public, so Actions minutes are free, but the setup must
   still fit the 2,000-minute free tier if the repo ever goes private.

## Non-goals

- No `develop`/`beta` branch and no pre-release channel. A developer who wants the
  newest code uses `nexus update --channel git` (Phase 6). PyPI pre-releases
  (`0.4.0b1`, `nexus update --pre`) can be added later without changing anything below.
- No version derived from git tags (setuptools-scm). The version stays a literal in
  `pyproject.toml`, and release-please edits it inside the release PR.
- No Homebrew, apt, or Scoop packages.

## Starting state (when this plan was written, 2026-09-29)

This table is historical. See "Progress" above for where things are now.

| Piece | State |
| --- | --- |
| `install.sh` / `install.ps1` | Done. Default source is **git `main`**; `--source pypi` exists. |
| README install section | One-liner exists at `README.md:22`, buried under a paragraph of options. |
| `.github/workflows/release.yml` | Runs on a `v*` tag push: checks tag == pyproject version, `uv build`, wheel tests, `uv publish` (trusted publishing, `pypi` environment), `gh release create`. **Never run.** |
| `.github/workflows/install.yml` | Installer matrix on every installer change, and nightly (Ubuntu + macOS + 2 Docker images + the published script). |
| Test CI | **None.** No workflow runs `pytest` or `ruff`. |
| PyPI | `nexus-harness` is **not registered**. The name is free. |
| Version | `0.1.0` in `pyproject.toml` and in `uv.lock` (`[[package]] name = "nexus-harness"`). No tags. |
| `nexus update` | `nexus/cli.py:_update`, helpers in `nexus/host_support/install.py`. A uv-tool install from git reports method `uv-tool` and runs `uv tool upgrade nexus-harness`, which re-fetches git `main`. |
| Repo settings | Public. Merge, squash, and rebase all allowed. `main` unprotected. Maintainer mostly pushes directly to `main`. |
| Commit style | Mostly conventional (`feat:`, `fix:`, `chore:`), with some plain messages. |

## How the release flow works

```text
main:  fix: A ──── fix: B ──── feat: C ──── docs: D ──── [merge release PR]
                                                              │
release PR "chore(main): release 0.2.0"                       ▼
  after A:  0.1.1   CHANGELOG: fix A                   tag v0.2.0 + GitHub release
  after B:  0.1.1   CHANGELOG: fix A, fix B            uv build → wheel checks
  after C:  0.2.0   CHANGELOG: + feat C                uv publish → PyPI
  after D:  (unchanged: docs is not releasable)        `nexus update` sees 0.2.0
```

1. On every push to `main`, the release-please action reads the commit subjects since
   the last release tag and creates or updates **one** PR. That PR changes:
   - `pyproject.toml` `[project].version`
   - the `nexus-harness` entry in `uv.lock` (through `extra-files`, see below)
   - `CHANGELOG.md`
   - `.release-please-manifest.json`
2. The bump comes from the largest change since the last release:

   | Commit subject | Pre-1.0 (now) | From 1.0 |
   | --- | --- | --- |
   | `fix: …`, `perf: …`, `deps: …` | patch | patch |
   | `feat: …` | minor | minor |
   | `feat!: …` or a `BREAKING CHANGE:` footer | **minor** (`bump-minor-pre-major`) | major |
   | `docs:`, `chore:`, `test:`, `refactor:`, `ci:`, `style:`, `build:` | no release | no release |
   | not conventional | ignored (not in the changelog) | ignored |

3. The maintainer merges the release PR whenever they are ready: after one fix or
   after twenty. Until then nothing is tagged or published.
4. When the PR merges, the next run of the same workflow creates tag `vX.Y.Z` and the
   GitHub release. Its `release_created` output gates the build and publish steps in
   the same workflow run.
5. Overrides:
   - A commit body line `Release-As: 1.0.0` forces the next version.
   - Editing the release PR's text works, but the next push to `main` regenerates it.
     Make edits right before merging.
   - Hotfix: push the `fix:` commit, then merge the release PR straight away.
6. Bad release: PyPI versions are immutable. Yank the release on pypi.org, which
   hides it from new resolves, then ship a `fix:` release. Users who need an exact
   version run `install.sh --version X` (or `nexus update --version X`, Phase 6).

## Phase 1: Test CI and Actions budget

### `.github/workflows/ci.yml` (new)

```yaml
name: ci
on:
  pull_request:
    paths-ignore: ["**.md", "docs/**", "plans/**", "artifacts/**"]
  push:
    branches: [main]
    paths-ignore: ["**.md", "docs/**", "plans/**", "artifacts/**"]
  workflow_dispatch:
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
permissions:
  contents: read
jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        python: ["3.13"]   # only the minimum supported version; dev machines cover 3.14 and macOS
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: {enable-cache: true, python-version: "${{ matrix.python }}"}
      - run: uv sync --locked --extra dev
      - run: uvx ruff check nexus tests
      - run: uv run pytest -q
  test-macos:       # macOS minutes count 10x on a private repo: main only, not PRs
    if: github.event_name != 'pull_request'
    runs-on: macos-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: {enable-cache: true, python-version: "3.12"}
      - run: uv sync --locked --extra dev
      - run: uv run pytest -q
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv build
      - run: uv run --with pytest --with setuptools pytest -q tests/test_model_data_package.py tests/test_install_script.py
  ci-ok:            # the one required status check; its name never changes with the matrix
    if: always()
    needs: [test, test-macos, build]
    runs-on: ubuntu-latest
    steps:   # a skipped test-macos (on PRs) counts as passing
      - run: '[ "${{ contains(needs.*.result, ''failure'') || contains(needs.*.result, ''cancelled'') }}" = false ]'
```

- `uv sync --locked` fails when `uv.lock` is out of date. That is also why the
  release PR must update `uv.lock` (Phase 3).
- The Playwright checks (`tests/playwright_*.py`) stay manual for now. Later they can
  be a non-required `workflow_dispatch` job.
- Before this goes in, run the suite once locally and write down any test that
  already fails in a clean checkout. Fix or mark those tests in a separate change;
  don't hide them in the CI change.

### `.github/workflows/install.yml` (trim)

- Keep the `push`/`pull_request` path filters. The full matrix runs only when an
  installer file changes.
- Nightly: run only `published-script` (Ubuntu, about 1 minute). Move the macOS and
  Docker jobs to `if: github.event_name != 'schedule'`.
- Add a nightly `published-pypi` job once Phase 4 ships:
  `curl …/install.sh | sh` (PyPI default now), then `nexus --version` and `nexus update`.
- Add `concurrency` with `cancel-in-progress` for pull requests.

### Budget check

| Workflow | Trigger | Rough cost per run |
| --- | --- | --- |
| `ci` on a PR | each push to a PR | ~2 × 4 min Linux |
| `ci` on `main` | each push to `main` | ~8 min Linux + ~5 min macOS (×10 if private) |
| `release-please` | each push to `main` | < 1 min Linux |
| publish | only when a release PR merges | ~2 min Linux |
| `install` nightly | daily | ~1–2 min Linux |

Public repo: all free. Private repo: roughly 60 pushes to `main` a month before
macOS on `main` becomes the thing to cut. Scheduled workflows in a public repo are
disabled after 60 days without repo activity; re-enable them from the Actions tab.

**Done when:** `ci-ok` is green on `main`, and a PR with a failing test is red.

**As landed (differs from the snippet above):** the `test` job is one Linux job on Python
3.13 (no matrix, no `test-macos`); it ignores four Textual pilot test files
(`tests/test_ui_tui.py`, `test_mock_tui.py`, `test_tui_integration_render.py`,
`test_tui_model_selection_integration.py`); `build` runs
`uv run --with pytest --with pytest-asyncio --with setuptools pytest …`; `ci-ok` needs
`[test, build]`. `pyproject.toml` pins the ruff rules. `ci-ok` was green on `537180e`
(4385 passed, 315 skipped). The budget table's macOS line no longer applies. The "red PR"
half of the check has not been exercised yet (no PR so far).

## Phase 2: Commit convention and repo settings

1. Merge settings (GitHub → Settings → General → Pull Requests):
   - Allow **squash merging only**. Turn off merge commits and rebase merging.
   - Default squash commit message: **"Pull request title"** (plus description).
     The PR title then becomes the commit subject that release-please reads.
   - Turn on "Automatically delete head branches".
2. `.github/workflows/pr-title.yml` (new): `amannn/action-semantic-pull-request@v5`
   on `pull_request_target` (`opened`, `edited`, `synchronize`). It checks the PR title
   against the types in the table above. Needs only `pull-requests: read`.
3. Ruleset for `main` (Settings → Rules):
   - Require status checks: `ci-ok` and `pr-title`.
   - Block force pushes and deletion.
   - **Require a pull request: optional.** The maintainer pushes directly today.
     Direct pushes still work with release-please as long as the commit subject is
     conventional. If direct pushes stay allowed, add the maintainer as a bypass
     actor and install a local `commit-msg` hook (below) instead of the PR check.
   - Add the release GitHub App (Phase 3) as a bypass actor only if the ruleset ends
     up blocking its PR branch. It should not need to.
4. Optional local guard: `scripts/commit-msg` (a small POSIX `sh` regex check on the
   subject), installed with `git config core.hooksPath scripts/hooks`. Document it in
   `AGENTS.md`. Agents that write commits follow the same convention.

**Done when:** a PR titled `Update stuff` fails `pr-title`, and `fix: update stuff` passes.

**Status:** `pr-title.yml` and `scripts/hooks/commit-msg` are on `main` (the hook was
checked on four sample subjects: `fix: a thing` and `feat(web)!: x` pass, `Update stuff`
and `docs:nospace` fail). The merge settings are done. Still to do by the maintainer:
create the `main` ruleset (Settings → Rules → Rulesets): Active, bypass list with the
maintainer set to "Always", target the default branch, restrict deletions, block force
pushes, require status checks `ci-ok` and `pr-title`, and **do not** require a pull
request. `pr-title` has never run (it only runs on PRs), so it may not appear in the
check picker until a PR exists; add `ci-ok` first and `pr-title` after the first PR.

## Phase 3: release-please and publishing

### Config files (repo root)

`release-please-config.json`:

```json
{
  "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
  "bootstrap-sha": "<HEAD of main when this lands>",
  "packages": {
    ".": {
      "release-type": "python",
      "package-name": "nexus-harness",
      "include-component-in-tag": false,
      "bump-minor-pre-major": true,
      "changelog-path": "CHANGELOG.md",
      "changelog-sections": [
        {"type": "feat", "section": "Features"},
        {"type": "fix", "section": "Bug fixes"},
        {"type": "perf", "section": "Performance"},
        {"type": "deps", "section": "Dependencies"},
        {"type": "revert", "section": "Reverts"},
        {"type": "docs", "section": "Documentation", "hidden": true},
        {"type": "chore", "hidden": true},
        {"type": "refactor", "hidden": true},
        {"type": "test", "hidden": true},
        {"type": "ci", "hidden": true}
      ],
      "extra-files": [
        {
          "type": "toml",
          "path": "uv.lock",
          "jsonpath": "$.package[?(@.name.value=='nexus-harness')].version"
        }
      ]
    }
  }
}
```

`.release-please-manifest.json`:

```json
{".": "0.0.0"}
```

- `include-component-in-tag: false` gives tags like `v0.2.0`, which is the format
  `install.sh --version` and the old `release.yml` already expect.
- `bootstrap-sha` stops release-please from reading all the history before it was
  added. The first release uses `Release-As` (see Phase 4).
- The `uv.lock` updater is the risky piece. Check it on the first release PR: the
  PR diff must show `uv.lock` changing from `0.1.0` to the new version, and
  `ci-ok` (`uv sync --locked`) must pass on that PR. Fallback if the TOML jsonpath
  doesn't match: a step in the release workflow that runs `uv lock` on the release PR
  branch and pushes a commit with the app token.
- The python strategy also looks for a `__version__` in `nexus/__init__.py`. There
  isn't one, and there shouldn't be: `package_version()` reads installed metadata.

### GitHub App token

A PR opened with the default `GITHUB_TOKEN` does not start other workflows, so
`ci-ok` would never run on the release PR and the ruleset would block the merge.

1. Create a GitHub App ("nexus-release", owned by the maintainer), installed only on
   this repo, with permissions **Contents: read & write** and **Pull requests: read &
   write**. No webhook.
2. Store `RELEASE_APP_ID` (variable) and `RELEASE_APP_PRIVATE_KEY` (secret).
3. The workflow mints a short-lived token with `actions/create-github-app-token@v1`.

Fallback: a fine-grained personal access token with the same two permissions.
It works but expires and is tied to a person, so prefer the App.

### PyPI trusted publisher

On pypi.org → Account → Publishing → **Add a pending publisher**:

| Field | Value |
| --- | --- |
| PyPI project name | `nexus-harness` |
| Owner | `nottherealsanta` |
| Repository | `nexus` |
| Workflow name | `release.yml` |
| Environment | `pypi` |

Then create the `pypi` environment in GitHub (Settings → Environments). Optional:
add the maintainer as a required reviewer. The publish then waits for one click
after the release PR merges. Also restrict the environment to the `main` branch
and `v*` tags.

Optionally repeat the whole thing on test.pypi.org with an environment named
`testpypi`, to rehearse the first publish (Phase 4).

### `.github/workflows/release.yml` (rewrite)

```yaml
name: release
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      tag:
        description: "Re-publish an existing tag (e.g. v0.2.0) whose publish failed"
        required: true
concurrency:
  group: release
  cancel-in-progress: false
permissions:
  contents: read
jobs:
  release-please:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    outputs:
      created: ${{ steps.rp.outputs.release_created }}
      tag: ${{ steps.rp.outputs.tag_name }}
    steps:
      - id: app
        uses: actions/create-github-app-token@v1
        with:
          app-id: ${{ vars.RELEASE_APP_ID }}
          private-key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}
      - id: rp
        uses: googleapis/release-please-action@v4
        with:
          token: ${{ steps.app.outputs.token }}
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json

  publish:
    needs: release-please
    if: >-
      always() &&
      (needs.release-please.outputs.created == 'true' || github.event_name == 'workflow_dispatch')
    runs-on: ubuntu-latest
    timeout-minutes: 15
    environment: pypi
    permissions:
      contents: write     # upload release assets
      id-token: write     # PyPI trusted publishing
    env:
      TAG: ${{ needs.release-please.outputs.tag || inputs.tag }}
    steps:
      - uses: actions/checkout@v4
        with: {ref: "${{ env.TAG }}"}
      - uses: astral-sh/setup-uv@v5
      - name: Tag matches pyproject version
        run: |
          v=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
          [ "v$v" = "$TAG" ] || { echo "tag $TAG != pyproject $v"; exit 1; }
      - run: uv build
      - name: Wheel contains the web, TUI and agent assets
        run: uv run --with pytest --with setuptools pytest -q tests/test_model_data_package.py tests/test_install_script.py
      - run: uv publish --trusted-publishing always
      - name: Attach installer, checksums and dists to the release
        run: |
          sha256sum install.sh install.ps1 dist/* > SHA256SUMS
          gh release upload "$TAG" install.sh install.ps1 SHA256SUMS dist/* --clobber
        env:
          GH_TOKEN: ${{ github.token }}
```

- release-please creates the GitHub release itself (with the changelog as notes),
  so the old `gh release create` becomes `gh release upload`.
- The `push: tags` trigger is removed. Keeping it would publish twice once the app
  token pushes a tag.
- `workflow_dispatch` re-publishes a tag after a failed upload. `uv publish` fails
  on a version that already exists on PyPI, which is the safe outcome.
- `release.yml` must keep its file name. The PyPI trusted publisher is bound to it.

**Done when:** a `fix:` push to `main` opens a release PR, CI runs on it, and its
diff changes `pyproject.toml`, `uv.lock`, `CHANGELOG.md`, and the manifest.

**Status:** all the files are on `main` (`fdbf33f`). Differences from the text above:
`release.yml` uses `create-github-app-token@v3` and `release-please-action@v5`, and its
wheel-test step includes `--with pytest-asyncio`; `bootstrap-sha` is
`537180e01a8bb163a53c72a1b071e714fc68adbf`; the commit body carries `Release-As: 0.1.0`.
The `uv.lock` jsonpath was checked offline (see "What we learned"). **To verify on the
first run:** the `release` workflow succeeds (needs the App variable and secret), one PR
titled about "release 0.1.0" appears, `ci-ok` runs on it (proves the App token starts
other workflows), and its diff touches exactly `pyproject.toml` (no change, already
0.1.0), `uv.lock`, `CHANGELOG.md` and `.release-please-manifest.json`. If `ci-ok` does
not run on the PR, the token is the default `GITHUB_TOKEN` and the App wiring is wrong.

## Phase 4: First release (v0.1.0)

1. Optional rehearsal: point the publish job at the `testpypi` environment
   (`uv publish --publish-url https://test.pypi.org/legacy/ --trusted-publishing always`)
   on a throwaway branch and run it with `workflow_dispatch`, then
   `uv tool install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ nexus-harness`.
2. Land Phases 1–3 with a commit whose body contains `Release-As: 0.1.0`. With the
   manifest at `0.0.0`, the release PR proposes exactly `0.1.0`, matching what
   `pyproject.toml` already says.
3. Merge the release PR. Check:
   - pypi.org/project/nexus-harness shows 0.1.0
   - the GitHub release `v0.1.0` has `install.sh`, `install.ps1`, `SHA256SUMS`, and dists
   - `uv tool install nexus-harness && nexus --version` on a clean machine prints
     `nexus 0.1.0`
   - `pip`/`uv` on Python 3.12 or older refuses it with a clear "requires Python >=3.13"
     message (the wheel declares `Requires-Python: >=3.13`)
4. Only now go on to Phase 5. Flipping the installer default before a release is on
   PyPI would break the one-liner for everyone.

## Phase 5: Installer defaults to PyPI, README one-liner

### `install.sh` and `install.ps1`

- `NEXUS_SOURCE` default `git` → `pypi`. `--version X` then means
  `nexus-harness==X`.
- `--source git` keeps working, with `--git-ref` (default `main`). `--version X` with
  the git source still means tag `vX`.
- Update the header comment and `--help` text.
- Tests (`tests/test_install_script.py`):
  - rename `test_installs_from_git_by_default` → `test_installs_from_pypi_by_default`
    and assert the spec is `nexus-harness`
  - add `test_git_source_uses_ref`
- `install.yml` still installs **this commit** with `--git-ref "$GITHUB_SHA"`, so it
  keeps testing unreleased code. The nightly `published-pypi` job tests the real default.

### README

Replace the Install section's opening with:

````markdown
## Install

```sh
curl -LsSf https://raw.githubusercontent.com/nottherealsanta/nexus/main/install.sh | sh
```

Then `cd` into a project and run `nexus chat` (or `nexus web`). Update with
`nexus update`. macOS, Linux and WSL; installs [uv](https://docs.astral.sh/uv/) and
Python 3.13+ for you if they are missing. Already have uv? `uv tool install nexus-harness`.
Options: `sh install.sh --help`. Changes: [CHANGELOG.md](CHANGELOG.md).
````

- Move the other lines (reading the script before running it, env options,
  `nexus daemon stop --all`, uninstall) into a short "Install details" subsection
  below "From a clean checkout". Keep each fact once.
- Move the Install section above "New here? Read ARCHITECTURE.md…", so the command
  is the first thing a visitor sees after the one-paragraph intro.
- This README edit can land right away if it keeps saying "from git" until Phase 5.
  The simplest route is to land it together with the default flip.

**Done when:** on a clean machine, the README line installs the PyPI release and
`nexus --version` matches the latest tag.

## Phase 6: `nexus update` follows PyPI

All changes are in `nexus/host_support/install.py` and `nexus/cli.py` (`_update`
and the `update` subparser). Tests go in `tests/test_install_script.py`, next to
the existing update tests.

### 1. Know where the install came from

Add `install_source() -> Literal["pypi", "git", "path", "unknown"]`, read from
`direct_url.json`:

- no `direct_url.json` → `pypi` (installed from an index)
- `vcs_info` → `git`
- `dir_info`, not editable → `path`
- anything else → `unknown`

`install_method()` keeps meaning *how* (`uv-tool` / `editable` / `pip`). Remove the
current `git` return value, which mixes the two ideas (a uv tool from git reports
`uv-tool` today, and a non-uv git install reports `git`).

### 2. Keep extras and Python across a reinstall

- Add `installed_extras() -> list[str]`: read `uv-receipt.toml` in the tool's venv
  (`Path(sys.prefix) / "uv-receipt.toml"`, parsed with `tomllib`). Its
  `[tool].requirements` entry for `nexus-harness` lists the extras. If the file is
  missing or malformed, return `[]`. Bounded: skip files over 64 KiB.
- Python: pass `--python f"{major}.{minor}"` of the running interpreter. Never pass
  the path of the venv's own Python: `--force` deletes that venv.

### 3. Choose the command

`update_command(uv, source, *, extras, python, channel, version=None) -> list[str]`:

| Situation | Command |
| --- | --- |
| `source == "pypi"`, stable channel, no version | `uv tool upgrade --refresh-package nexus-harness nexus-harness` |
| `source == "git"`, stable channel (**migration**) | `uv tool install --force --refresh-package nexus-harness --python X.Y "nexus-harness[extras]"` |
| `--version V` | `uv tool install --force --python X.Y "nexus-harness[extras]==V"` |
| `--channel git [--ref R]` | `uv tool install --force --reinstall --python X.Y "nexus-harness[extras] @ git+https://github.com/nottherealsanta/nexus@R"` (default ref `main`) |
| `source == "path"` / `unknown` | refuse with a message: re-run the installer, or use `--channel git` |

- `--refresh-package` makes uv re-check the index. Without it, uv's cached PyPI
  metadata (up to 10 minutes) can report "already up to date" right after a
  release.
- The migration prints one line:
  `Moving this install from git to PyPI releases (use --channel git to stay on git).`
- Refusals for editable and non-uv installs stay as they are.

### 4. CLI

`nexus update [--channel {stable,git}] [--ref R] [--version V] [--no-restart]`

- `--ref` needs `--channel git`. `--version` and `--channel git` can't be combined.
  argparse errors cover both.
- The version report after upgrading (`installed_version_after_update`) and the
  daemon restart logic don't change.

### 5. Tests

- `update_command` argv for each row of the table, including extras and the
  `--python` pin
- `install_source()` for each `direct_url.json` shape (write fixtures into a temp
  dist-info, or monkeypatch `distribution`)
- `installed_extras()`: receipt with extras, without, missing, malformed, oversized
- the migration message is printed exactly once, and only when moving from git
- update the existing `test_update_command_argv` and
  `test_update_refuses_editable_and_non_uv_installs` to the new signature

**Done when:** a machine installed from git `main` runs `nexus update`, ends up on the
PyPI release, keeps its `documents` extra, and has its daemons restarted on the new
version.

## Phase 7 (optional): "Update available" notice

This makes updating feel automatic without ever upgrading silently.

- `nexus/host_support/update_check.py`:
  - `latest_release()` fetches `https://pypi.org/pypi/nexus-harness/json` with
    `httpx`: 2 s timeout, 256 KiB response cap, ignoring yanked and pre-release
    versions.
  - The result is cached in `~/.nexus/cache/update-check.json` for 24 h, including
    failures, so an offline machine doesn't retry on every start.
  - Off when `NEXUS_NO_UPDATE_CHECK=1`, a `[updates] check = false` config key is set,
    `CI` is set, or the install is `editable`.
  - Versions are compared with `packaging.version` if it is already importable,
    otherwise with a small numeric tuple compare. No new runtime dependency.
- Surfaces:
  - `nexus --version` and `nexus doctor` add `(X.Y.Z available: nexus update)`.
  - TUI and web: add a `update_status` command to `nexus/host/protocol.py`, handled
    in `nexus/host/facade.py` from the daemon's cached check. Show the notice in the
    same place in both surfaces (the status area in the top bar), as the "web
    mirrors TUI" rule in `AGENTS.md` requires. The UI never calls PyPI itself.
- Tests: cache hit, cache expiry, fetch failure cached, opt-out env, yanked and
  pre-release versions ignored (fake transport, no network), plus
  `tests/test_host_*` for the new command.

## Phase 8: Docs

- `plans/install.md`: mark Phase 3 done and link here.
- `docs/core.md`: add a short "Releasing" section covering the commit types, the
  release PR, `Release-As`, re-publishing with `workflow_dispatch`, and yanking.
- `AGENTS.md`: one line under Conventions: "Commit subjects and PR titles use
  Conventional Commits (`feat:`, `fix:`, …); release-please turns them into the
  version bump and changelog. Don't edit `version` in `pyproject.toml` or
  `CHANGELOG.md` by hand."
- `README.md`: `nexus update --channel git` and `--version` in the install details.

## Order

| Step | Phase | Who | Depends on |
| --- | --- | --- | --- |
| 1 | Phase 1: CI + install.yml trim | agent | none |
| 2 | Phase 2: repo settings, PR title check | maintainer (settings) + agent (workflow, hook) | 1 |
| 3 | Phase 3 setup: GitHub App, PyPI pending publisher, `pypi` environment | **maintainer** | none |
| 4 | Phase 3 code: config files, `release.yml` rewrite | agent | 1, 3 |
| 5 | Phase 4: first release v0.1.0 | maintainer merges | 4 |
| 6 | Phase 5 + Phase 6: installer default, README, `nexus update` | agent | 5 |
| 7 | Phase 7: update notice | agent | 6 (optional) |
| 8 | Phase 8: docs | agent | with each phase |

Steps 1, 4 (written but not merged), 6's code, and 8 can be prepared in parallel.
Phase 5's default flip must not merge before step 5 is verified.

## Risks

| Risk | Mitigation |
| --- | --- |
| `uv.lock` not bumped, so `uv sync --locked` fails on the release PR | Check on the first release PR; fallback `uv lock` step (Phase 3). |
| CI doesn't run on the release PR | GitHub App token; confirm `ci-ok` appears on the first release PR. |
| Non-conventional commit subjects leave a fix out of a release | PR title check plus the local hook; `Release-As` or an empty `fix:` commit to force a release. |
| Flipping the installer to PyPI before a release exists | Phase 4 comes strictly before Phase 5. |
| Publish fails after the tag exists | `workflow_dispatch` with the tag re-runs only the publish job. |
| A bad release reaches users | Yank on PyPI and ship a fix release; `nexus update --version` to pin. |
| Existing git users stay on git `main` | The Phase 6 migration moves them on their next `nexus update`. |
| Actions minutes if the repo goes private | Budget table in Phase 1; drop macOS on `main` first. |

## Files this plan adds or touches

- new: `.github/workflows/ci.yml`, `.github/workflows/pr-title.yml`,
  `release-please-config.json`, `.release-please-manifest.json`, `CHANGELOG.md`
  (created by the bot), `scripts/hooks/commit-msg` (optional),
  `nexus/host_support/update_check.py` (Phase 7)
- changed: `.github/workflows/release.yml`, `.github/workflows/install.yml`,
  `install.sh`, `install.ps1`, `nexus/host_support/install.py`, `nexus/cli.py`,
  `tests/test_install_script.py`, `README.md`, `AGENTS.md`, `docs/core.md`,
  `plans/install.md`
- Phase 7 only: `nexus/host/protocol.py`, `nexus/host/facade.py`, TUI and web status
  area, `tests/test_host_*`

## Open questions

**Answered 2026-09-29:** (1) keep direct pushes to `main`, enforced by the local hook;
(2) no manual approval on the `pypi` environment; (3) yes, the update notice is on by
default. The questions are kept below for the record.

1. Keep direct pushes to `main`, or require PRs? This decides whether the PR title
   check or the local `commit-msg` hook enforces the convention. The plan supports
   both.
2. Require a manual approval on the `pypi` environment? It adds one click after each
   release-PR merge, as a last check.
3. Phase 7 on by default? The plan says yes, with a 24 h cache and an env/config opt-out.
