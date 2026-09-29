# One-line install plan

Status: implemented (phases 1-4; phase 3 waits on the first tagged release) · 2026-09-29

## Goal

A new user installs Nexus with one command and nothing else set up before it:

```sh
curl -LsSf https://raw.githubusercontent.com/nottherealsanta/nexus/main/install.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/nottherealsanta/nexus/main/install.ps1 | iex"
```

After it finishes, `nexus` is on `PATH`. The user runs `nexus chat` in a project and
the existing first-run setup (`nexus/host_support/setup.py`) asks them to connect a
provider. The installer never asks for a provider or credentials.

The script:

1. Finds `uv`, or installs it with Astral's official installer if it is missing.
2. Uses `uv` to get a suitable Python (≥3.11). uv downloads a managed Python when the
   system has none, so the user does not need Python beforehand.
3. Installs Nexus as an isolated uv tool: `uv tool install nexus-harness`. This gives
   it its own venv and puts the `nexus` shim in uv's tool bin directory.
4. Makes sure that bin directory is on `PATH`.
5. Stops any running daemons left from an older version, then runs a quick
   `nexus doctor` smoke check and prints next steps.

Running the same line again upgrades Nexus in place.

## Non-goals

- No Homebrew, apt, or Scoop packages yet. They can wrap the same artifact later.
- No bundled Python or standalone binary (PyInstaller and similar). uv-managed
  Python covers the "no Python installed" case.
- No provider or credential setup in the installer. That is the job of
  first-run setup and `nexus auth`, which keep credentials in the daemon and keychain.
- No system-wide install and no `sudo`. Everything goes in the user's home directory.

## Why uv tool install

| Option | Verdict |
| --- | --- |
| `pip install --user` | Breaks on PEP 668 "externally managed" system Pythons (Homebrew, Debian), and depends on whatever Python is installed. Rejected. |
| `pipx` | Works, but needs Python and pipx first. That is two prerequisites, not zero. |
| **`uv tool install`** | One static binary, installs Python itself, isolated venv per tool, `uv tool upgrade`/`uninstall` for free, fast. The repo already uses uv (`uv.lock`, `uv sync`). **Chosen.** |

## Distribution source

The installer takes a *package spec* and passes it to `uv tool install`. It supports
two sources in this order:

1. **PyPI (target):** `nexus-harness` (the name in `pyproject.toml`). Before this works,
   check the name is free on PyPI and publish it (see Phase 3).
2. **Git (bootstrap, and for pinned or dev installs):**
   `git+https://github.com/nottherealsanta/nexus@<ref>`. This needs the repo to be
   public and `git` on the machine. The script checks for `git` only when it uses this
   source.

Phase 1 ships with the Git source as the default, so it works before any PyPI
release. Phase 3 changes the default to PyPI. This is one line in each script.

### Package data check

The Git and wheel installs must include the non-Python assets declared in
`[tool.setuptools.package-data]`: the models.dev catalogue, web assets, `.tcss`, and
agent markdown. `tests/test_model_data_package.py` already builds a wheel and checks
the model data. Extend it (or add `tests/test_install_wheel.py`) to also check for:
`nexus/ui/web/index.html`, `nexus/ui/web/js/app.js`, `nexus/ui/tui/*.tcss`, and
`nexus/agents/data/*.md`. A missing web asset would only show up at runtime after a
one-line install, so a test should catch it first.

## Script design: `install.sh` (macOS, Linux, WSL)

Put it at the repo root so the raw GitHub URL stays short and stable. POSIX `sh`, not
bash, because `curl … | sh` runs under `dash` on Debian and Ubuntu.

### Environment overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXUS_VERSION` | latest | Pin a release, e.g. `0.2.0` → `nexus-harness==0.2.0` (or a git tag on the Git source). |
| `NEXUS_SOURCE` | `pypi` (after Phase 3; `git` before) | `pypi`, `git`, or a full package spec / local path for testing. |
| `NEXUS_GIT_REF` | `main` | Branch, tag, or commit for the Git source. |
| `NEXUS_PYTHON` | `3.12` | Python version uv installs or uses for the tool venv. Must be ≥3.11. |
| `NEXUS_EXTRAS` | empty | e.g. `documents` → `nexus-harness[documents]`. |
| `NEXUS_NO_MODIFY_PATH` | unset | Skip shell profile edits (passed through to uv's installer as `UV_NO_MODIFY_PATH`). |
| `NEXUS_NO_DOCTOR` | unset | Skip the post-install smoke check (for CI and images). |
| `NEXUS_YES` | unset | Non-interactive. The script never prompts anyway; this is reserved. |

It also accepts flags as `sh -s -- --version 0.2.0 --git main --no-modify-path`.
Flags win over environment variables.

### Steps

```text
main()
  set -eu
  parse_args "$@"
  detect_platform            # uname -s / -m; reject unsupported (e.g. 32-bit) with a clear message
  need_cmd curl || need_cmd wget   # downloader for uv's installer
  ensure_uv                  # see below
  ensure_python              # uv python find ">=3.11" || uv python install "$NEXUS_PYTHON"
  stop_running_daemons       # only if an old `nexus` is already installed (upgrade path)
  install_nexus              # uv tool install --force --python "$NEXUS_PYTHON" "$SPEC"
  ensure_path                # uv tool update-shell unless NEXUS_NO_MODIFY_PATH
  smoke_test                 # "$BIN/nexus" doctor --json >/dev/null (non-fatal, see below)
  print_next_steps
```

Wrap the whole body in a `main` function and call it on the last line. Then a download
cut off halfway runs nothing instead of half a script. This is the standard
`curl | sh` safety pattern (rustup and uv both use it).

#### `ensure_uv`

1. If `uv` is on `PATH`, use it. Also check `~/.local/bin/uv` and `~/.cargo/bin/uv`,
   the two usual install spots, because a fresh uv is not on `PATH` in the current
   shell yet.
2. If uv is older than a minimum (`uv --version` below `0.5.0`, when `tool` and
   `python install` were stable), run `uv self update`. If that fails because uv was
   installed by Homebrew or pip, warn and continue.
3. Otherwise install it:
   `curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=$X sh`,
   then set `UV="$HOME/.local/bin/uv"` (honour `XDG_BIN_HOME` / `UV_INSTALL_DIR` if set).
4. Use the absolute `$UV` path for the rest of the script. Never rely on `PATH`
   being refreshed.

#### `install_nexus`

```sh
"$UV" tool install --force --python "$NEXUS_PYTHON" "$SPEC"
```

- `--force` makes it an idempotent install-or-upgrade and replaces an existing
  `nexus` shim, for example one from an old `pip install -e .`.
- Use `--python` rather than hoping the system Python is ≥3.11. uv downloads a
  managed build if needed.
- Resolution uses the published wheel's metadata, not `uv.lock`. `uv tool install`
  ignores project locks. The pinned `textual==8.2.8` / `textual-diff-view==0.1.5` /
  `httpcore==1.0.9` already in `[project.dependencies]` keep the known-good TUI
  pair. Keep them pinned there.
- Find the bin dir with `"$UV" tool dir --bin`.

#### `stop_running_daemons` (upgrade safety)

Daemons are per workspace and keep running old code after an upgrade. The protocol
handshake (`nexus/host/daemon.py`, `version_mismatch`) turns a protocol bump into a
clean error. A same-protocol upgrade still leaves old code running until the daemon
restarts.

- Before upgrading, if `nexus` already exists, run a new command:
  `nexus daemon stop --all` (see Code changes). It stops every live daemon listed
  under `~/.nexus/daemon/`.
- If the old version has no `--all`, fall back gracefully: print "restart running
  sessions with `nexus daemon restart`" and continue. Never kill processes by name.

#### `smoke_test`

Run `"$BIN/nexus" --version`, then `"$BIN/nexus" doctor` with `--workspace` set to a
temp directory. That checks imports, package data, and the config path without
touching the user's real workspace or starting a long-lived daemon. If it fails,
print a warning, the log path, and the doctor output, but still exit 0: the install
itself succeeded. `--strict` makes the failure fatal, for CI.

(Check whether `doctor` starts a daemon. If it does, run `nexus daemon stop` for the
temp workspace afterwards, or add a `--no-daemon` offline mode.)

#### `print_next_steps`

```text
Nexus 0.2.0 installed → ~/.local/bin/nexus

  cd your-project
  nexus chat        # terminal UI; first run asks you to connect a provider
  nexus web         # same thing in the browser

Restart your shell (or run: source ~/.zshrc) if `nexus` is not found.
Upgrade: rerun this command, or `uv tool upgrade nexus-harness`
Uninstall: uv tool uninstall nexus-harness && rm -rf ~/.nexus   # second part deletes sessions
```

Only print the "restart your shell" line when the bin directory was not already on
`PATH` at the start.

### Output and errors

- Plain, prefixed lines (`nexus-install: …`). Colour only when stdout is a TTY and
  `NO_COLOR` is unset.
- Each failure names the step and gives a fix: "uv install failed: check your network
  or install uv manually from https://docs.astral.sh/uv/".
- Exit codes: 0 ok, 1 generic, 2 unsupported platform, 3 uv failed, 4 nexus install
  failed.

## Script design: `install.ps1` (Windows)

This mirrors `install.sh` step for step:

- `ensure_uv`: `Get-Command uv`, else
  `irm https://astral.sh/uv/install.ps1 | iex`, then use
  `$env:USERPROFILE\.local\bin\uv.exe`.
- Same env overrides (`$env:NEXUS_VERSION`, …).
- `uv tool install --force --python 3.12 $spec`, then `uv tool update-shell`.
- Check first: does the daemon work on Windows at all? Unix sockets, `fcntl` locks,
  and keyring backend all need checking. If not, `install.ps1` points the user at
  WSL and exits 2, and support is tracked separately. Look at
  `nexus/host/daemon.py` and the lock code before claiming Windows support.

## Security

- Host the scripts in the repo and serve them from `raw.githubusercontent.com` over
  HTTPS. Later, point a short vanity URL (e.g. `nexus.sh/install`) at the same file
  with a redirect only, so there is one source of truth.
- Only fetch from two origins: our repo and `astral.sh`. Don't pipe any other
  third-party script.
- No `sudo`, no writes outside `$HOME`. Refuse to run as root unless
  `NEXUS_ALLOW_ROOT=1`, for Docker, where root is normal.
- Publish a SHA-256 of each release's `install.sh` in the GitHub release notes for
  people who download it, read it, then run it. Document the two-step form in the
  README:
  ```sh
  curl -LsSf …/install.sh -o install.sh && less install.sh && sh install.sh
  ```
- The installer never reads, writes, or prompts for credentials. That keeps the
  "credentials never leave the daemon" posture from AGENTS.md.

## Code changes in Nexus

These are small, and each has a test next to its peers:

1. **`nexus --version`**: top-level flag in `build_parser()` (`nexus/cli.py`). Prints
   `importlib.metadata.version("nexus-harness")`, reusing `_server_version()` logic
   from `nexus/host/daemon.py` through a shared helper in `nexus/util/` or `nexus/config/`
   to respect layering. Test in `tests/test_cli.py`.
2. **`nexus daemon stop --all`**: stop every live daemon recorded under
   `~/.nexus/daemon/`. It goes through the existing stop path per workspace and reports
   how many it stopped. Test in `tests/test_host_daemon.py`, using temp workspaces.
3. **Doctor: install hygiene section**: report the install method (uv tool / pip /
   editable), the interpreter path, and warn when more than one `nexus` is on `PATH`
   (`shutil.which` over `PATH` entries). That warning catches the "old editable
   install shadows the new one" case. Test in `tests/test_doctor_mismatches.py`.
4. **Doctor: stale daemon warning**: if a live daemon's `server` version (already sent
   in the hello) differs from the client's package version, say
   "daemon is running 0.1.0, client is 0.2.0 → `nexus daemon restart`".
5. **README "Install" section**: lead with the one-liner. Keep the
   `pip install -e .` / `uv sync --extra dev` instructions under "From source". Also
   update `docs/core.md` if it mentions install. The user-facing wording must match
   `print_next_steps`.

Stay under the line budgets: `nexus/cli.py` and `nexus/host/` are near their caps, so
put the doctor logic in `nexus/host_support/doctor.py`.

## Release pipeline (Phase 3)

- Add a `.github/workflows/release.yml` triggered by a `v*` tag: `uv build`, run the
  wheel-content test against the built wheel, then `uv publish` with PyPI trusted
  publishing (OIDC, no stored token).
- Tag version equals `pyproject.toml` version. Check it in the workflow and fail if
  they differ.
- Attach `install.sh`, `install.ps1`, and their SHA-256 sums to the GitHub release.
- After the first PyPI release, change the scripts' default `NEXUS_SOURCE` from
  `git` to `pypi`.

## Testing the installer

`tests/` must stay offline, so split the tests:

**Offline (pytest, runs everywhere):**
- `tests/test_install_script.py`
  - `sh -n install.sh` (syntax) and, if available, `shellcheck install.sh`
    (skip if the binary is missing).
  - Run `install.sh` with a fake `uv` on `PATH` (a stub script that logs its argv to
    a file) and `NEXUS_SOURCE=/path/to/repo`. Assert the exact
    `uv tool install --force --python 3.12 …` invocation, env/flag precedence,
    and that `NEXUS_NO_MODIFY_PATH` skips `update-shell`.
  - "uv missing" path: stub `curl` to emit a fake uv installer that drops the stub
    `uv` into `$HOME/.local/bin`. Assert the script finds it by absolute path.
  - Truncation safety: running only the first N lines of the file does nothing (no
    `main` call).
- Wheel-content test (above).

**Live / CI matrix (`.github/workflows/install.yml`, not in the pytest suite):**
- `ubuntu-latest`, `macos-latest` (and `windows-latest` once supported), each with:
  - clean runner → `sh install.sh` with `NEXUS_SOURCE=git NEXUS_GIT_REF=$GITHUB_SHA`
    → `nexus --version` → `nexus doctor`.
  - Docker `debian:stable-slim` and `alpine` (no Python, no curl → the script should
    fail with a clear "need curl or wget" message; alpine also covers musl, where
    uv's managed Python must work).
  - Upgrade: install the previous tag, start a daemon (`nexus daemon status`
    autostarts it), rerun the installer at HEAD, and assert the old daemon was stopped.
  - Rerun twice → second run is a no-op upgrade and exits 0.
- Nightly job against the live raw URL on `main` to catch a broken published script.

## Phases

| Phase | Deliverable | Done when |
| --- | --- | --- |
| **1. Script (Git source)** | `install.sh`, `nexus --version`, offline installer tests, README one-liner pointing at Git source | Fresh macOS and Ubuntu machines go from nothing to `nexus chat` with one line; offline tests pass |
| **2. Upgrade hygiene** | `nexus daemon stop --all`, doctor install/stale-daemon checks, upgrade CI job | Rerunning the one-liner on a machine with running daemons leaves no old-code daemon |
| **3. PyPI release** | release workflow, trusted publishing, default source → PyPI, wheel-content test | `curl … \| sh` installs from PyPI; `NEXUS_VERSION` pins work |
| **4. Windows** | `install.ps1` (or a documented WSL route if the daemon is Unix-only) | CI `windows-latest` job green, or README says "use WSL" |
| **5. Nice-to-haves** | short vanity URL, Homebrew tap wrapping the PyPI package, `nexus self update` that shells out to `uv tool upgrade` | as needed |

## Open questions

Resolved 2026-09-29:

- The repo `github.com/nottherealsanta/nexus` is public, so the Git source and the raw
  script URL work. Phase 1 can go ahead as written.
- The PyPI name `nexus-harness` is available (not yet registered). Claim it early:
  publish a first release (or a 0.1.0 placeholder) so nobody takes it before Phase 3.

Still open:

1. Default Python for the tool venv: 3.12 (safest wheel coverage) or 3.13?
2. Should the `documents` extra be offered interactively? The current plan says no
   prompts. Users pass `NEXUS_EXTRAS=documents`.
3. Does the daemon run on native Windows today? This decides Phase 4's scope.

## Files this plan adds or touches

- new `install.sh`, `install.ps1` (repo root)
- new `tests/test_install_script.py`, extended `tests/test_model_data_package.py`
- new `.github/workflows/install.yml`, `.github/workflows/release.yml`
- `nexus/cli.py` (`--version`, `daemon stop --all`)
- `nexus/host_support/doctor.py` (install hygiene, stale daemon)
- `README.md` (Install section), `docs/core.md` (if it mentions install)

## Implementation notes

- `nexus update`: upgrades through `uv tool upgrade nexus-harness` (git installs use
  `uv tool install --force --reinstall`), then stops every daemon and restarts the
  ones that were running with the freshly installed binary. It refuses editable and
  non-uv installs with a pointer to the right command. `--no-restart` skips the restart.
- The helpers live in `nexus/host_support/install.py` so `nexus/host/` stays under its
  line budget. `nexus doctor` gains an `install` section (version, method, duplicate
  `nexus` binaries, stale daemons).
- `install.ps1` only explains the WSL route: the daemon needs Unix sockets.
