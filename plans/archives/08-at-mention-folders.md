# Plan 08: `@` completion for folders as well as files

Status: implemented in code, verified by unit/PTY tests (2026-10-20), and visually verified via contrast-enhanced crop (parent visual inspection of enhanced crop `artifacts/native-final/artifacts/10-folder-menu.png` shows selected `src/main.py` above composer query `@sr`; raw terminal text `artifacts/native-final/artifacts/10-folder-completion.txt` also contains `src/` and `src/main.py`, noting original palette is very low contrast). Delivered as
an explicit opt-in (`FileSearch.include_directories`, `complete(..., include_directories=)`,
action field `directories`); desktop/web unchanged. Source: root `TODO.md` item 5
("using @ for folder too instead of just files").

## 1. Intent

Typing `@` in the Ratatui composer offers matching **directories** (shown with a
trailing `/`) alongside files. Accepting a directory inserts `@path/` and keeps
completion open so the user can keep typing into it; accepting a file behaves
as today.

### Non-goals

- Expanding `@path` into attached content. Today `@` is completion only: the
  prompt carries the literal `@path` text and the model reads it with its tools
  (no mention expansion exists in `nexus/`; checked 2026-10-20). That stays.
- Desktop (GPUI) changes (paused, `AGENTS.md:8`) and web composer changes.
- Showing hidden, ignored, symlinked or read-denied directories.

## 2. Current evidence

- Host search: `search_files` walks the workspace, skipping hidden entries,
  symlinks, Git-ignored paths and read-deny roots; directories are traversed but
  never returned — only files matching the needle are appended
  (`nexus/host_support/workspace.py:20-112`, return condition at :107-110).
  `_git_visible` already returns visible directories (`workspace.py:103-105,121-`).
- Ranking: basename-prefix, basename, path; shallower first (`workspace.py:115-118`).
- Protocol: `FileSearch(query, limit=30)` → `FileSearchResult(paths)`
  (`nexus/host/protocol.py:706-710,1726-1727`); client `search_files`
  (`nexus/client/protocol.py:166-167`); facade `search_files`
  (`nexus/host/facade.py:260-264,1844-1846`).
- The only UI caller is `nexus/ui_support/completion.py:130-142` (`_files`), shared
  by Ratatui and (per `docs/ratatui-parity.md:1020`) the desktop.
- Ratatui acceptance always appends a space after the chosen value
  (`rust/tui/src/main.rs:2656-2665` for mouse; the keyboard path must be found
  and changed the same way).
- Tests: `tests/test_file_completion_host.py`, `tests/test_ratatui_completion.py`.

## 3. Design

1. **Protocol**: add `include_directories: bool = False` to `FileSearch`
   (default keeps old clients, including desktop, byte-identical). Client
   `search_files(query, limit, *, include_directories=False)`.
2. **Host**: when the flag is set, a visible directory whose relative path
   contains the needle is appended as `relative + "/"`; same deny/hidden/ignore
   rules; same `max_entries` bound. Ranking treats `name/` by its basename;
   ties put a directory before files of the same depth (so `src/` precedes
   `src/x.py`). The workspace root itself is never returned.
3. **Completion** (`ui_support/completion.py`): `_files` passes
   `include_directories=True` for the Ratatui path. Because the desktop also
   calls `complete`, either add a keyword the Ratatui caller sets, or accept that
   the desktop receives directory rows too — recommended: keyword, default off,
   so desktop behaviour does not change while it is paused.
4. **Ratatui** (`rust/tui/src/main.rs`, mouse and keyboard accept paths): when
   the accepted value ends with `/`, insert `@value` with **no** trailing space
   and request completion again for the new query; otherwise unchanged.
5. **Display**: directory rows keep the trailing `/`; no new glyph needed.

## 4. Tests and docs

- `tests/test_file_completion_host.py`: directories returned only with the flag;
  hidden/ignored/symlinked/denied directories excluded; trailing `/`; ranking
  ties; limit and `max_entries` still bound the result; old `FileSearch` without
  the field decodes to `False`.
- `tests/test_ratatui_completion.py`: `@sr` offers `src/`; accepting `src/`
  leaves `@src/` with no trailing space and the next query lists its children.
- Rust unit/PTY test for the no-space insertion on both accept paths.
- Docs: `docs/ratatui-parity.md` (composer row, `@` completion), `docs/surfaces.md`
  (completion contract), `docs/host.md` if commands' fields are listed there.

## 5. Acceptance criteria

- [x] `@` + a partial folder name lists that folder with a trailing `/` (verified by unit/PTY tests and visually verified via contrast-enhanced crop).
- [x] Choosing it inserts `@folder/` and immediately shows that folder's entries (verified by Rust/Python unit & PTY tests).
- [x] Files behave exactly as before (insert + trailing space).
- [x] No hidden, ignored, symlinked or denied directory ever appears.
- [x] Desktop and web behaviour unchanged.

## 6. Risks and open decisions

- Large repos: directories add candidates but not walk cost (they are already
  visited); keep the 30-row limit.
- Open: should the model get a hint that `@dir/` means "look in this folder"?
  Default: no prompt change; the literal path is self-explanatory.

## 7. Verification ledger

- **Parent test verification**:
  - Full Python feature suite: `.venv/bin/python -m pytest` passed (781 passed, including `tests/test_file_completion_host.py`, `tests/test_ratatui_completion.py`, `tests/test_ratatui_pty.py`).
  - `cargo test --manifest-path rust/tui/Cargo.toml --locked`: 590 passed, 0 failed, 2 ignored + 58 dashboard passed (`tests/dashboard_modules.rs`).
- **Parent visual inspection**:
  - Inspected native capture artifacts in `artifacts/native-final` (`review.html`, `artifacts/10-folder-completion.png`, `artifacts/10-folder-menu.png`, `artifacts/10-folder-completion.txt`).
  - Result: Visually verified via contrast-enhanced crop (`artifacts/native-final/artifacts/10-folder-menu.png`), which shows selected `src/main.py` above composer query `@sr`; raw terminal text `artifacts/native-final/artifacts/10-folder-completion.txt` also contains `src/` and `src/main.py` (noting original palette is very low contrast).
- **Limitations**:
  - Opt-in parameter `include_directories=True` is enabled for Ratatui only; desktop (paused) and web are unchanged.
- **Previous quick agent follow-up fix**:
  - `tests/test_ratatui_completion.py`: updated `BackgroundActions(shell, AsyncMock())` so `update` callback is awaitable.
  - `tests/test_ratatui_pty.py`: updated expected native completion actions to include `"directories": True`.
  - `tests/test_file_completion_host.py`: improved raw wire legacy `FileSearch` decode test to verify absent `include_directories` key (`b'{"type": "FileSearch", "query": "a"}'`).
