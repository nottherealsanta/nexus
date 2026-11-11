# 1. Release and installation

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

## Merge what is unmerged

Rechecked 2026-11-09 against freshly fetched refs and GitHub. Release PR #100 merged; **no open PRs remain**. Older superseded PRs were already closed; their historical records are retained on GitHub, not deleted. Unrelated branch pruning remains separate.

| Item | What it is | Verdict and action |
| --- | --- | --- |
| **Local `main`** | Validated terminal/editor/fork/client work and final test report committed and pushed | [x] Full automated gate complete (section 4); release merge pulled fast-forward |
| PR #100 `release 0.12.0` | Reviewed seven-file version/changelog bump | [x] Merged 11-09 at `a17354d3`; release branch removed. Artifact and installation checks follow in 1.1 |
| `origin/fix/daemon-agents-restart` (`74daa3b`) | A clear error when no agent loads, and `install.sh` restarts a running daemon on update | **Already in `main`** (verified 2026-11-07: ancestor `74daa3b2`). Installer restart and missing-agent diagnostic regression tests pass (2026-11-07). |
| `origin/ci/nsh` (`9c5a127`) | Lazy native microphone lookup, unverified | **Superseded:** `main`'s `dictation.ts` already looks it up lazily. [ ] Delete the branch |
| `origin/feat/update-notice`, `fix/default-model-client`, `fix/native-binary-needs-host` | Sept/Oct branches from old Nexus's layout (`rust/tui`, `nexus/client`) | Do not apply to this tree. [ ] Decide whether the update notice is still wanted (it overlaps `nexus update`); otherwise delete |
| Worktrees `design/void`, `design/void-impl`, `polish-timeline` (`.claude/worktrees/`) | 0 commits ahead of `main`, 61–79 behind | [x] These worktrees are no longer registered. Their merged local branch refs remain pending prune approval |
| 101 other local branches reviewed | [reports/plan-branch-audit.md](../reports/plan-branch-audit.md): 42 have zero commits outside main; 59 contain unique history | [~] Review complete; automatic approval review blocked merged-branch pruning. User approval requested; no branches deleted |

### 1.1 Release and installation

- [x] **0.12.0 published (11-09):** four TUI binaries, Core wheel/sdist, `install.sh` and `SHA256SUMS`; all payload checksums independently verified. Release workflow [42564022608](https://github.com/nottherealsanta/nexus/actions/runs/42564022608) passed. Published installer succeeded in an isolated macOS arm64 home, including SDK source build, daemon start/stop and plain `nexus` PTY launch/quit. No real home was migrated or updated. Details: [validation report](../reports/release-0.12.0-validation.md).
- [ ] Execute the published installer on Linux; complete the wider platform runtime matrix below. No NSH web bundle is published by the existing workflow.
- [x] Correct the daemon info/version label (11-10, working tree): use installed `nexus10` distribution metadata instead of `0.10.0.dev0`, also for SSE `connected` and the CLI update fallback. Server/doctor/event-log suites: 93 passed; focused Ruff passed. No running daemon was restarted or deployed.
- [x] Linux: installer checks and `docs/development.md` list `pkg-config`, ALSA headers, a C toolchain, `git`, `uv`,
  Rust, Node 22; source install checks `/usr/bin/env`, which the Monty launcher explicitly executes. Real Linux kernel execution remains part of the platform matrix.
- [x] Remove `nexus-design-mockup/` (11-07); no leftover `nexus10` branch wording remains in `AGENTS.md` or `docs/development.md`.
- [ ] Bump to the next `-dev` version after published-release verification.
- [x] **`nexus update`:** reports only, following the default decision. Core CLI, launcher and offline update-check tests are implemented.
- [ ] Platform matrix: macOS arm64 from a clean home, then Linux x86_64 and aarch64 (install, `nexus doctor`, daemon,
  TUI, NSH web, a scripted turn, a kernel run).

### 1.2 Retire `nexus nsh tui`

Decided 2026-11-06: retire it, keep the code somewhere safe. The complete code is in the pushed tag
`archive/nsh-terminal-backend` (the `main` commit before the removal); restore it with
`git checkout archive/nsh-terminal-backend -- <path>`.

- [x] Tag the code (`archive/nsh-terminal-backend`, pushed 2026-11-06).
- [x] Remove `nexus nsh tui` from `scripts/nexus` and `npm run tui`, and the terminal-only code paths NSH grew for it
  (the `--target tui` test target, terminal-only glyph/hint branches). Keep everything web and macOS use.
- [x] Remove `reports/parity-audit/` and the terminal parts of `reports/nsh-tui-parity/` (the tag keeps them); fix
  `nexus-nsh/README.md` and `docs/development.md` where they describe it. Launcher tests go too.
- [x] Keep the shared-parity work that landed (scroll bar, segmented Settings, rail, overview, sidebar highlight).
- [x] Ledger: record that the NSH terminal target is retired and where the code is.

## History: consolidation checkpoint (2026-11-08)

The user requested consolidating all current work on `main`, updating this plan,
and stopping. This checkpoint does **not** complete the remaining plan. Existing
unchecked and partial items remain open; ZMX, browser work and CI stay deferred.

Included in the consolidation: Files mutations and leased watching; the protected
Git backend/API and initial paired Git UI; the vendored TUI editor and unified
NSH editor with find/replace; mobile capability guards; terminal input/layout
fixes; SSE-driven inspector invalidation; native Apple microphone/bootstrap
services; edit-tool matching/multi-edit; step-budget notices; and release cleanup.

**In progress, not release-verified:** the TUI editor's pointer/paste/find/replace
and Esc behavior, paired Git grouping/navigation/safety flows, matching Git/editor
screenshots, the full NSH suite on the consolidated tree, native audio/device and
GPUI runtime checks. The new native bootstrap honors explicit `NEXUS10_HOME`
without falling back to a personal daemon; its offline tests pass. No paid
provider checks or real-home migration/deployment were performed.

Historical consolidation validation (superseded by section 4's 11-09 rerun): Core **1255 passed, 1 skipped, 1 terminal-shutdown failure** (isolated rerun passes); TUI full run **825 passed, 15 failed, 9 ignored**, then three stale expectations corrected and their focused suites pass. **Twelve TUI shell/editor checks were still failing at that checkpoint; the 11-09 rerun passes**. NSH editor/Git **17/18**, with the remaining focus/undo test passing after its fix; no final full rerun. Builds, Clippy, Ruff and shared manifest/theme checks pass. See [the exact consolidation report](../reports/consolidation-2026-11-08.md) for commands, scope and remaining failures.
At that checkpoint, branch pruning and the release PR were pending. PR #100 has since merged (11-09); branch pruning remains separate and no unique-history branches were removed. The local `main` matched freshly fetched `origin/main` before the
consolidation commit.
