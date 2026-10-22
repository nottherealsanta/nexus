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
| 0 | Fix ruff F811; add ruff to `scripts/hooks` (pre-commit) | haiku | done (uncommitted) |
| 1 | Separate `lint` job (ruff, `cargo fmt --check`) on ubuntu, parallel to tests | sonnet (workflow) | done, uncommitted; `cargo fmt --check` left out (rust/tui and rust/live are not fmt-clean) |
| 2 | `pytest-xdist` + sharding; trial run for isolation failures; `--durations` | sonnet (tests) | done; parallel-safe, no markers needed |
| 3 | Rust once: `Swatinem/rust-cache`; Python test job skips optional Rust bins where possible | sonnet (workflow) | done, uncommitted |
| 4 | Fold build job into wheel build; pin Python 3.13 | sonnet (workflow) | done, uncommitted |
| 5 | Skip CI jobs on release-please PRs (changelog/manifest/version only); `ci-ok` stays green | sonnet (workflow) | done, uncommitted |
| 8 | `cargo fmt` rust/tui + rust/live, then add `cargo fmt --check` to `lint` | — | follow-up (format-only diff; ask user) |
| 7 | Reconcile shard command (workflow ↔ test agent), push on a branch, measure a real run | main | committed + pushed to main; hosted run pending |
| 6 | Linux for the Python suite | — | deferred (user decision) |

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
- Unverified: hosted run; full suite under `CARGO=false` (nexus-live absent);
  runner default Rust vs TUI MSRV; filter on a real release-please PR.

## Results

(none yet)
