# 36. Shared Rust build folder for worktrees of any project

User request (2026-11-11): every worktree Nexus works in should share Rust build output with the other worktrees of
the same repository, so a new worktree does not compile from nothing. For this repository it already landed
(repo-root `.cargo/config.toml`, see [docs/development.md](../docs/development.md#shared-rust-build-folder)); this
item extends it to the projects Nexus sessions work in. Not started.

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

- [ ] One helper (for example in `nexus/worktrees.py`) that returns the extra environment for a workspace, cached per
  path; empty outside Git.
- [ ] Apply it where Core spawns workspace processes: foreground bash (`nexus/tools/bash.py` `run`), background jobs
  (`runtime.py` job spawn), integrated terminals (`terminals.py`, the `env` built before `Popen`), and the `code`
  kernel if it shells out.
- [ ] Never override what the user chose: skip when `CARGO_BUILD_BUILD_DIR` or `CARGO_TARGET_DIR` is already in the
  daemon's environment. A project's own `.cargo/config.toml` `build-dir` is overridden by the environment variable,
  so also skip when the workspace's Cargo config sets `build-dir` (check the config files Cargo would read, from the
  workspace up to the filesystem root).
- [ ] Setting: on by default, with a config switch to turn it off (name to decide; document in docs/daemon.md).
- [ ] Is the added environment part of the model's context? It is not something the model receives, so nothing new
  in the timeline. Mention it in the bash tool's description only if the model needs to know where artifacts go
  (it does not: final binaries stay in `target/`).
- [ ] Tests with a scratch repo and two worktrees: same value in both, different repos differ, user overrides kept.
- [ ] Docs: docs/daemon.md (process environment), docs/tools.md (bash), porting-ledger row (old Nexus did not do this).

## Open questions

- Cleanup: nothing removes these folders. Options: none (document `rm -rf ~/.cargo/build/nexus-*`), or a
  `nexus doctor` row showing their size. Decide before landing.
- Builds in two worktrees at the same moment wait on Cargo's lock for the shared folder. Acceptable for now; Cargo's
  finer-grained locking (unstable) would remove the wait when it stabilises.
- blaze: dropped (see [decisions.md](decisions.md), 2026-11-11).
