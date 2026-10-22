# 09. Config and security

## 1. Config keys

Scopes: **user** = `~/.nexus/nexus.toml`; **project** = `<project root>/.agents/nexus.toml`
(file 02 §4: always the project root, never a worktree's copy). "user-only" keys are ignored
with a warning in project config, the same rule as `worker_dangerously_skip_permissions`
(`docs/decisions.md`).

### 1.1 `[daemon]` (file 01; user-only)

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `"shared"` after F1's flip (`"per-workspace"` before) | also `NEXUS_DAEMON` env |
| `idle_timeout` | `300` | seconds; per project and for the process |
| `max_projects` | `32` | loaded projects (LRU eviction of idle ones) |
| `max_processes` | `16` | process slots across projects (shell jobs, Git, setup, checks) |
| `max_requests_per_provider` | `6` | in-flight model requests per provider |
| `max_turns` | `32` | safety ceiling only |

### 1.2 `[worktrees]`

| Key | Default | Scope | File |
| --- | --- | --- | --- |
| `sessions` | `false` | user, project | 03 — new root sessions start in a worktree |
| `subagents` | `true` | user, project | 05 — writer subagents may get worktrees |
| `base` | `"current"` | project | 03 — default base branch (`current` = main checkout's branch) |
| `max_worktrees` | `16` | user-only | 03 — live Nexus worktrees per project |
| `commit_max_files` | `2000` | user, project | 03/04 — size guard |
| `commit_max_file_mb` | `50` | user, project | 03/04 |
| `checkpoints` | `true` | user, project | 04 — hidden refs for main/external sessions |
| `checkpoint_keep` | `50` | user-only | 04 — sessions whose refs are kept |
| `setup`, `setup_shell`, `setup_timeout`, `setup_on_fail`, `setup_for_subagents` | none, `false`, `300`, `"fail"`, `"copy_only"` | project (user allowed) | 07 |
| `copy`, `copy_max_mb`, `share_dirs` | `[]`, `20`, `[]` | project | 07 |
| `submodules` | `"none"` | project | 07 |
| `land` | `"local"` | project | 06 — `local` or `pr` |
| `land_history` | `"squash"` | project | 06 — `squash`, `merge`, `ff` |
| `land_checks` | `[]` | project | 06 — list of argv lists |
| `land_checks_timeout` | `600` | project | 06 — max 3600 |
| `watch_interval` | `30` | user-only | 06 — seconds; target ref polling while sessions exist |
| `remote` | `"origin"` | project | 06 PR mode |
| `pr_branch_prefix` | `""` | project | 06 PR mode |
| `fetch_interval` | `0` (off) | user-only | 06 PR mode |
| `remove_on_land` | `true` | user, project | 07 |
| `remove_on_purge` | `true` | user, project | 07 |
| `remove_idle_days` | `0` (off) | user-only | 07 |

Agent frontmatter: `isolation: worktree | auto | none` (file 05 §3).

Settings UI: a **Worktrees** page in the native Settings (`nexus/ui/ratatui/settings_pages/`,
new `worktrees.py`) with the two toggles at the top (D1), then base, land mode/history, checks,
setup (with its trust state), cleanup. Each key's help text in `ui_support/settings_help.py`.

## 2. Security and trust

| Rule | Where enforced |
| --- | --- |
| Nexus writes only `refs/heads/nexus/*` and `refs/nexus/*`, except the target ref at a confirmed land | a single `git_refs.py` helper (new, in `nexus/agents/`) that every ref update goes through; asserts the prefix; CAS (`update-ref <ref> <new> <old>`) always |
| The main checkout's working tree and index are changed only by `merge --ff-only` at a confirmed land of a clean checkout | land code path only (file 06 §5 step 9); a test asserts no other code path runs Git with `-C <main>` and a mutating subcommand |
| No `reset`, `stash`, `checkout`, `clean`, `gc` by Nexus in any user checkout | static test over `nexus/agents/git*.py`, `worktrees.py`, `worktree_*.py` argv constants |
| Git runs with the controlled environment | `worktrees._git_environment` (`nexus/agents/worktrees.py:274`): allow-listed proxy vars, no inherited `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` except where Nexus sets them (checkpoints) |
| No push without explicit per-push confirmation; no "always allow" | PR mode (file 06 §8); `bypass permissions` does not bypass it (as `docs/security.md:135` for worktree refusals) |
| Credentials for Git/`gh` stay with Git/`gh` | Nexus never reads credential helpers or tokens; it runs the tools |
| `PathGuard`: a worktree session cannot write the main checkout or another worktree; a child cannot write its parent's worktree | `tools/permissions.py` `for_worktree` generalised (file 02) |
| Setup command and land checks are code from the repository | trust prompt once per digest for `setup` (file 07 §1.3); `land_checks` run only on an explicit user Land, shown before running in the preview |
| Base-moved notes contain agent-written subjects | fenced as model output in the context note (file 06 §4) |
| Hello `env` travels inward only over the `0600` socket; never echoed, logged or returned | file 01 §4 (shared-daemon plan §4) |
| One process hosts many projects' extensions | documented trust widening (shared-daemon plan C4) in `docs/security.md` |
| Checkpoint refs live in the user's repository | documented: `git push --mirror` would push them; `refs/nexus/*` can be deleted any time with `git for-each-ref --format='delete %(refname)' refs/nexus | git update-ref --stdin` |
| Harness commits skip project hooks (`--no-verify`) and signing | stated in `docs/security.md` and the Settings page; land does not run hooks either (file 06 §10 Q3) |

## 3. Docs

`docs/config.md` (both tables), `docs/security.md` (§2 rows), `docs/decisions.md` (ref
ownership rule, `--no-verify`, trust prompt), `docs/extensions.md` (project `.agents/` read from
the project root only).

## 4. Tests

- `tests/test_config_worktrees.py`: defaults, scopes, user-only keys ignored in project config
  with a warning, validation bounds.
- `tests/test_worktree_git_safety.py`: the static argv checks above; the ref helper refuses
  non-`nexus/` refs; CAS failure surfaces as `target_moved`.
- `tests/test_ratatui_settings_worktrees.py`: Settings page rows and toggles.

## 5. Open questions

1. `sessions = false` by default (recommended for the first release; flip after feedback) vs
   `true`.
2. `subagents = true` by default once F5 ships (recommended; only affects sessions already in a
   worktree) vs `false`.
