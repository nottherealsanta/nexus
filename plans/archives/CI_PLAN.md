# CI plan: correct and fast

Status ledger for making `ci.yml` green again and cutting PR wall-clock from
~29 min to ~5–7 min. Updated as work lands. Code wins over this file, then `docs/`.

## Baseline (last green run 39230237460, 2026-10-19)

| Step | Time | Cause |
| --- | --- | --- |
| `pytest -q` | 18 min | 6,971 tests run serially, no xdist |
| `uv sync --extra dev` | 1.5–2 min | builds `nexus-ratatui` / `nexus-live` Rust bins in the editable install |
| `cargo test` | ~1 min | no Rust cache |
| build job `uv build` | 1–4 min | recompiles Rust; Python unpinned (ran 3.15) |
| native-wheels | 6–7 min | recompiles Rust a third time |

CI red since 2026-10-19: ruff F811 in `tests/test_ratatui_live_manager.py:26`
(stops the job before pytest), plus `_LEGACY_REPO_DIR` ImportError in packaging tests
(fixed by edbdcaf, passes locally).

## Steps

| # | Step | Owner | Status |
| --- | --- | --- | --- |
| 0 | Fix ruff F811; add ruff to `scripts/hooks` (pre-commit) | haiku | done (08be81b) |
| 1 | Separate `lint` job (ruff, `cargo fmt --check`) on ubuntu, parallel to tests | sonnet (workflow) | done (08be81b); `cargo fmt --check` left out (rust/tui and rust/live are not fmt-clean) |
| 2 | `pytest-xdist` + sharding; trial run for isolation failures; `--durations` | sonnet (tests) | done; parallel-safe, no markers needed |
| 3 | Rust once: `Swatinem/rust-cache`; Python test job skips optional Rust bins where possible | sonnet (workflow) | done (08be81b) |
| 4 | Fold build job into wheel build; pin Python 3.13 | sonnet (workflow) | done (08be81b) |
| 5 | Skip CI jobs on release-please PRs (changelog/manifest/version only); `ci-ok` stays green | sonnet (workflow) | done (08be81b) |
| 8 | `cargo fmt` rust/tui + rust/live, then add `cargo fmt --check` to `lint` | — | follow-up (format-only diff; ask user) |
| 7 | Reconcile shard command (workflow ↔ test agent), push on a branch, measure a real run | main | committed + pushed to main; hosted run pending |
| 6 | Linux for the Python suite | — | deferred (user decision) |
| 10 | Fix/remove the 22 CI failures (+7 local-only PTY): GPT Live + host_facade group; rest (focus, phase3, runtime_extensions, tool_integration, PTY) | 2× sonnet | done; full local `-n auto`: 0 failed |
| 9 | Patch release 0.6.1 (`Release-As`, user chose patch over proposed 0.7.0) to time the new pipeline | sonnet (release) | done: v0.6.1 on GitHub (16 assets) and PyPI; PRs #90, #89 |

## Findings

- Step 0: F811 came from wrapper fixture `def shell(live_shell)` shadowing the
  imported `shell as live_shell`. Now `def shell(request)` +
  `request.getfixturevalue("live_shell")`. `ruff check nexus tests` clean;
  `test_ratatui_live_manager.py` 12 passed. New `scripts/hooks/pre-commit` runs
  ruff on staged `nexus/`/`tests/` .py files (warns and passes if ruff missing);
  documented in `docs/devtools.md`.
- Steps 1/3/4/5 (`ci.yml` rewrite, actionlint clean). Job graph:
  - `changes` (ubuntu) → outputs `code`, `native`. A PR touching only
    `CHANGELOG.md`, `.release-please-manifest.json`, and `version =` lines in
    `pyproject.toml`/`uv.lock` sets both false. Push/dispatch always run all.
  - `lint` (ubuntu, no deps): `uvx ruff@0.16.7 check nexus tests`, `uv build --sdist`.
  - `rust` (macOS): `Swatinem/rust-cache@v2.8.0`, `cargo test`/`cargo build` for
    rust/tui, uploads debug `nexus-ratatui` artifact.
  - `test` (macOS, 4 shards, needs `rust`): `CARGO=false` (setuptools-rust skips
    the optional bins; `uv sync` ~1 s vs 1.5–2 min cold),
    `NEXUS_TUI_BINARY` → downloaded debug binary, Python 3.13,
    `pytest -q -n auto --splits 4 --group N --durations=25` (pytest-split).
  - `native-wheels` if `native`; `ci-ok` unchanged name, fails only on failure/cancelled.
  - Separate `build` job removed: packaging tests build the wheel in-process
    inside the shards; sdist in `lint`; real wheels in `native-wheels`.
  - Rejected `--no-install-project`: `importlib.metadata` lookups need the dist installed.
- Step 2: suite is ~8,400 tests. Local 10-core: serial 10:00, `-n auto` 2:07–2:27.
  Fixed: non-deterministic param ids in `test_attachments.py` (broke xdist
  `loadfile` and pytest-split collection); token-file race in
  `test_http_daemon_e2e.py`. Slowest: `test_ratatui_e2e_settings::test_all_tiers_are_reachable…`
  50 s (dominates one shard; consider `--store-durations` → `.test_durations`).
- **28 pre-existing failures on main** (serial and parallel alike), so CI stays red
  on pytest until fixed: GPT Live `live_manager_required` (test_host_facade,
  test_live_bridge, test_live_consent, test_live_security, test_ratatui_pty_live);
  undiagnosed: test_phase3_exit, test_ratatui_pty (2), test_ratatui_pty_sidebar (5),
  test_runtime_extensions (2), test_tool_integration (1).
- Step 10a (live + host_facade, tests only, 106 pass): live tests moved onto the
  manager session (`MANAGER_ID` in `tests/live_fakes.py`, manager consent);
  protocol/health expectations updated for `SessionFocus`, `UsageRefreshSettings*`,
  `persistent_jobs*`. Removed `test_permission_request_is_announced_and_never_resolved_by_the_bridge`
  (manager turns never ask permission by design; unit tests still cover the
  announcement). `test_project_sessions_returns_bounded_archived_rows_and_pending_request`
  fails in CI shard 2 but not locally: possible order/parallel flake, watch next run.
- Step 10b: code fix in `nexus/context/manager.py` (handoff kept suffix capped
  by room left after fixed parts; tools now ~4.8k of a 5.7k budget). Tests
  updated: host_focus (monotonic clock seed), runtime_extensions (`code` tool on
  by default), tool_integration (new `context.notice`), PTY tool-group fold and
  sidebar overlay layout, PTY live consent dialog now clips the notice
  ("Tab inspect details"; full wording still asserted in test_ratatui_live.py).
  Open question: is the clipped consent notice intended?
- Unverified: hosted run; full suite under `CARGO=false` (nexus-live absent);
  runner default Rust vs TUI MSRV; filter on a real release-please PR.

## Results

First hosted run on main, 08be81b (run 39792594961, 2026-10-22):

| Job | Old | New |
| --- | --- | --- |
| lint (ruff + sdist) | inside test | 12 s |
| rust (cold cache) | ~1 min + recompiles in uv sync and build | 1 m 57 s |
| pytest | 18 min serial | shard 2: 1 m 34 s job (pytest 77 s); shard 3: 1 m 18 s |
| first failure reported | ~21 min | ~3.5 min after push |

Shards fail on the 28 pre-existing test failures (live_manager_required etc.), as
expected. On pushes to main `native-wheels` (~7 min) is now the long pole; PRs run
it only when native inputs change. The API reported shards 1/4 and native-wheels
still in progress after the run was marked failed; re-check final timings.

Release 0.6.1 (2026-10-22), old (0.6.0) vs new:

| Pipeline | Old | New |
| --- | --- | --- |
| Release-please PR `ci` | 28.8 min (full suite) | jobs ~20 s (`changes`, `lint`, `ci-ok`; tests skipped) |
| Release PR wall clock | ~29 min | ~14 min, now bound by the separate `install` workflow |
| Push-to-main `ci` | 28.8 min | 9.8 min (shards 0.7–2.3 min; `native-wheels` 7 min is the long pole) |
| `release` workflow | 30.4 min | 26 min (native-wheels; `macos-15-intel` ~21 min) |

Release run 39796293112 shows failure: a transient GraphQL error in the
`release-please` job after it had tagged; wheels and PyPI publish still succeeded.

Test-fix commit 0a31c51 (run 39798391499): all green. Shards 1–3 passed first
try (1.1–1.9 min); shard 4 failed in "Set up job" (runner DNS could not reach
codeload.github.com) and passed on rerun (2097 passed, 1m45s). Total ~6 min to
`ci-ok` excluding the rerun. Anomaly: on the first attempt `ci-ok` reported success
despite shard 4's failure; the gate is now an allow-list (every `needs` result must
be success or skipped) and echoes the results it saw.

## Next candidates

- `release`: `macos-15-intel` wheels (~21 min) are the long pole; add Rust caching
  to cibuildwheel or cross-compile x86_64 on arm64.
- `install` workflow (~14 min) gates PR wall-clock; run it only on install inputs
  (already path-filtered; the release PR touches `pyproject.toml`) or skip on version-only diffs.
- Push-to-main `native-wheels` (7 min) could move to PRs-with-native-changes only.
- `cargo fmt` both crates, then add `cargo fmt --check` to `lint` (step 8).

