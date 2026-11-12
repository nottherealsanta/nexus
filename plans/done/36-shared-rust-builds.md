# 36. Shared Rust build folder for worktrees of any project

User request (2026-11-11): every worktree Nexus works in should share Rust build output with the other worktrees of
the same repository, so a new worktree does not compile from nothing. For this repository it already landed
(repo-root `.cargo/config.toml`, see [docs/development.md](../../docs/development.md#shared-rust-build-folder)); this
item extends it to the projects Nexus sessions work in. **Landed 2026-11-12** (`nexus/cargo_env.py`, see
[docs/daemon.md](../../docs/daemon.md#shared-rust-build-folder-for-session-workspaces)).

## Approach

Stock Cargo's `build.build-dir` (stable since Cargo 1.91), not the unofficial `huacnlee/cargo` fork, which replaces
the user's `cargo` binary and auto-updates. Intermediate artifacts go to one folder per repository; each worktree
keeps its own `target/` with its final binaries, so worktrees never overwrite each other's executables.

When a session's workspace is inside a Git repository, Core adds to the environment of the processes it starts there:

```
CARGO_BUILD_BUILD_DIR=<cargo home>/build/nexus-<short hash of the repository's git common dir>
```

The common dir (`git rev-parse --git-common-dir`, resolved) is the same for the main checkout and all its linked
worktrees, so they share one folder, and two unrelated repositories never do.

## Steps

- [x] One helper (`nexus/cargo_env.py`; finds the common dir by reading `.git` files, no subprocess) that returns the extra environment for a workspace, cached per
  path; empty outside Git.
- [x] Apply it where Core spawns workspace processes: foreground bash (`nexus/tools/bash.py` `run`), background jobs
  (`runtime.py` job spawn), integrated terminals (`terminals.py`, the `env` built before `Popen`), and the `code`
  kernel if it shells out (it does not: kernels start with an empty environment), plus composer `!` commands.
- [x] Never override what the user chose: skip when `CARGO_BUILD_BUILD_DIR` or `CARGO_TARGET_DIR` is already in the
  daemon's environment. A project's own `.cargo/config.toml` `build-dir` is overridden by the environment variable,
  so also skip when the workspace's Cargo config sets `build-dir` (check the config files Cargo would read, from the
  workspace up to the filesystem root).
- [x] Setting: on by default; `tools.shared_rust_builds = false` in config.toml turns it off (config file only, no
  settings-screen row, so no client change). Documented in docs/daemon.md.
- [x] Is the added environment part of the model's context? It is not something the model receives, so nothing new
  in the timeline. Mention it in the bash tool's description only if the model needs to know where artifacts go
  (it does not: final binaries stay in `target/`).
- [x] Tests with a scratch repo and two worktrees: same value in both, different repos differ, user overrides kept.
- [x] Docs: docs/daemon.md (process environment), docs/tools.md (bash), porting-ledger row (old Nexus did not do this).

## Notes

- Cleanup (decided 2026-11-12): nothing removes the folders automatically; `nexus doctor` shows a `rust builds` row
  with their count, total size and `rm -rf ~/.cargo/build/nexus-*`.
- Builds in two worktrees at the same moment wait on Cargo's lock for the shared folder. Acceptable for now; Cargo's
  finer-grained locking (unstable) would remove the wait when it stabilises.
- blaze: dropped (see [decisions.md](../decisions.md), 2026-11-11).

Not verified: a real cargo build in two worktrees of another project through a running daemon (the tests check the
variable reaches bash; Cargo's own handling of `build.build-dir` is upstream behaviour).
