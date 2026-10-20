# Plan 04: Add a skill with `npx skills` (project or global)

Status: implemented and verified (2026-10-20). Unit and integration tests passed (.venv/bin/python -m pytest exact feature suite: 781 passed; cargo test --locked rust/tui: 590 passed 2 ignored plus 58 dashboard passed). Ratatui Settings → Skills form, confirmation and result dialogs visually inspected in native captures (`artifacts/native-final`). Real isolated installation verified separately for `anthropics algorithmic-art` (`/tmp/nexus-verify-20261020T074825Z/artifacts/retry-live.log`). Source: root `TODO.md` item 1
("new skill add ( with npx skills ) ( project level or global level )").

Implementation notes: `nexus/host_support/skills_install.py`, `SkillsInstall`/`SkillsInstallResult`
(host protocol, `Client.skills_install`, facade dispatch, daemon slow command, gateway default-deny),
Ratatui Settings → Skills button → form → confirmation → background task. Pinned `skills@1.7.2`,
`-a claude-code`, staged folder `.claude/skills/` (from npm/GitHub docs read on the web; **unverified**
by a real run, so the staged folder and flags remain assumptions). Decisions that differ from §3:
overwrite moves the whole folder to settings trash (meta `kind: directory`); `tools/` is not
imported; frontmatter keys outside the six supported ones (e.g. `license`) make that skill fail with
a reason (parser not loosened). No CLI subcommand (optional; deferred). No `-l` list-only mode.
Tests: `tests/test_skills_install.py` (fake `npx` on PATH, no network), additions in
`tests/test_ratatui_settings_files.py`, `test_host_facade.py`, `test_gateway_policy.py`.

## 1. Intent

From Settings → Skills (Ratatui), the user can install a skill from a `npx skills`
source (for example `vercel-labs/agent-skills`), choosing **project** or **global**
scope. The skill lands where Nexus already discovers skills, and the normal hot
reload picks it up.

### Non-goals

- A Nexus skill marketplace, search, ratings or update/uninstall flows (removal
  keeps using the existing per-file Reset/trash).
- Running `npx` from the agent loop or as a model tool. This is a user action in
  Settings (and optionally a CLI subcommand), never a model action.
- Desktop (GPUI) work: paused per `AGENTS.md:8`. Web Settings parity is a later plan.
- Writing into other agents' folders (`.claude/skills/`, …).

## 2. Current evidence

- Discovery roots: `<workspace>/.agents/skills/` (and legacy `.nexus/skills/`),
  then `~/.nexus/skills/` (`docs/extensions.md:63-67`, `nexus/skills/manager.py:5`).
- Settings → Skills is a list/edit page only: `nexus/ui/ratatui/settings_pages/skills.py`
  delegates to `files.file_blocks`, which offers `Edit…`, `New file` and
  `Reset category…` (`nexus/ui/ratatui/settings_pages/files.py:46-56`). The
  Global/Project control already exists for skills (`files.py:15,23-26`).
- Settings writes are path-policed, bounded (512 items, 256 KiB bodies),
  hash-checked and redacting (`docs/extensions.md:192-195`,
  `nexus/host_support/settings_inventory.py`). There is no multi-file or
  directory write command.
- Skill resources: absolute symlinks or symlinks that escape the skill root are
  refused or listed as escaped (`nexus/skills/resources.py:14,300-352`).
- `npx skills add` (vercel-labs/skills; checked on the web 2026-10-20, not
  run here): `-g/--global`, `-a/--agent <agent>`, `-s/--skill <name>`, `-l/--list`,
  `--copy` (copy instead of symlinking into agent folders), `-y/--yes`. Default
  install *symlinks* into per-agent folders such as `.claude/skills/`. Exact
  target folders per agent are **not verified**.

## 3. Design

Recommendation: **stage, then import.** Run `npx skills add` into a fresh,
empty temporary directory with `--copy -y` (and an explicit `-a` agent whose
folder we then read), then Nexus itself copies the resulting skill directories
into the chosen Nexus root (`.agents/skills/` or `~/.nexus/skills/`). Deciding
trade-off: Nexus's global root (`~/.nexus/skills/`) is not a folder `npx skills -g`
knows, and the default symlink mode would produce links the resource resolver
refuses; staging makes the destination, path policy and conflict handling ours.

1. **Host command** `SkillsInstall(scope, source, skills: list[str] | None, overwrite: bool = False)`
   in `nexus/host/protocol.py`, handled in `nexus/host_support/skills_install.py` (new):
   - Validate `source` (owner/repo, `https://` git URL, or local path; no shell
     metacharacters; bounded length). argv only, never a shell.
   - Preflight: `npx` on `PATH`; otherwise return a clear `unavailable` result.
   - Run `npx --yes skills add <source> --copy -y -a <agent> [-s name…]` with
     `cwd` = a new empty temp dir, a minimal env (no Nexus credentials), a timeout
     (e.g. 120 s), bounded stdout/stderr (redacted, never sent to model context).
   - Discover `*/SKILL.md` under the staged agent folder; parse with the existing
     skill frontmatter parser; reject invalid skills individually.
   - Copy each skill directory into the target root through the settings path
     policy (`settings_scope.py`): refuse symlinks, `..`, files over the existing
     bounds; existing directory with the same name → `conflict` unless
     `overwrite`, in which case the old directory goes to settings trash first.
   - Trigger one extension rebuild; return installed/skipped/failed names and
     the `ReloadReport` summary.
   - Optional `list_only` (`-l`) mode to show what a source offers before
     installing.
2. **Client** method `skills_install(...)` in `nexus/client/protocol.py`.
3. **Ratatui** Settings → Skills: an `Add from npx skills…` button beside
   `New file` (skills only), opening a small form: source, optional skill names,
   scope (defaults to the page's Global/Project control), and a confirmation
   that states "runs `npx skills` (downloads and runs third-party code)". Show
   progress, then flash installed names and any per-skill errors.
4. **CLI** (optional, same host command): `nexus skills add <source> [--global] [--skill NAME]`.
5. **Security**: record in `docs/security.md` that installing runs `npx`
   (third-party code) under the user's account at the user's explicit request;
   installed skills are trusted extension data (`docs/extensions.md:10-11`).

## 4. Tests and docs

- `tests/test_skills_install.py` (new): fake `npx` script on `PATH` producing a
  staged layout; project and global targets; conflict and overwrite→trash;
  symlink/escape refused; invalid SKILL.md skipped with a reason; missing `npx`;
  timeout; argv never passed through a shell; stderr redacted and bounded.
- `tests/test_ratatui_settings_files.py`: the button exists on Skills only and
  carries the current scope.
- Docs: `docs/extensions.md` (Skills + Settings files), `docs/surfaces.md`
  (Settings), `docs/cli.md` if the CLI is added, `docs/security.md`,
  `docs/module-map.md` for the new module, `docs/decisions.md` (stage-then-import).

## 5. Acceptance criteria

- [x] From Settings → Skills (Project), installing a source puts each selected
      skill under `<workspace>/.agents/skills/<name>/` and it appears in the
      index on the next turn without a restart.
- [x] Same with Global → `~/.nexus/skills/<name>/`.
- [x] No symlinks are created in either root; nothing is written outside it.
- [x] Name conflicts are refused unless overwrite is confirmed; overwritten
      content is recoverable from settings trash.
- [x] Without `npx`, the action explains what is missing and changes nothing.
- [x] Offline tests pass (part of feature suite: 781 passed); real isolated install
      verified separately for `anthropics algorithmic-art` (`/tmp/nexus-verify-20261020T074825Z/artifacts/retry-live.log`).

## 6. Risks and open decisions

- **Which `-a` agent folder to read** after a staged `--copy` install is
  unverified; spike first (`npx skills add <repo> -l`, then a real install in a
  temp dir) and pin the agent and folder in code with a test.
- `npx` executes downloaded code: confirmation copy must say so; never auto-run.
- Network/proxy failures and slow installs: timeout and cancel must leave no
  partial skill in the target root (copy into a sibling temp dir, then rename).
- Open: should Settings → Skills also show where a skill came from (record
  `source` in a sidecar file)? Default: no, until requested.

Verification follow-up: bounded informational `license` scalars now supported;
compatibility/metadata mapping support remains out of scope. Real isolated install
verified separately for `anthropics algorithmic-art` (log:
`/tmp/nexus-verify-20261020T074825Z/artifacts/retry-live.log`).
Suite tests independently verified by parent:
`.venv/bin/python -m pytest` (781 passed) and `cargo test --manifest-path rust/tui/Cargo.toml --locked` (590 passed, 2 ignored + 58 dashboard passed).
Visual inspection: parent inspected native fixture captures in `artifacts/native-final`
(`fixture-skills-source-form.png`, `fixture-skills-confirmation.png`, `fixture-skills-result.png`).
Limitations: npm warning that Node 22.15 is below the requested 22.20 engine version for `skills@1.7.2`;
CLI subcommand deferred.
