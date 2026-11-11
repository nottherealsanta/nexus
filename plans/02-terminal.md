# 2. Terminal inside Nexus

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

Side job (user, 2026-11-08): **implemented and visually checked** full question,
option-label and preview wrapping with narrow/short scrolling in both clients.
TUI footer hints wrap in reserved rows; NSH's reading hint no longer overlaps
scrolling content. [Screenshots and interaction evidence](../reports/question-readability/README.md).
The existing paged-TUI versus single-card-NSH question interaction is not redesigned here.

Goal (user, 2026-11-06): a real interactive terminal in Nexus, in the TUI and in NSH (desktop and web, never mobile),
with the same operations in both. Not the old `nexus nsh tui` launcher and not a list of bash jobs. Design inputs:
[reports/section14/spikes.md](../reports/section14/spikes.md) (PTY + pyte, `vt100`, `@xterm/headless` all proven in
spikes) and the archived 14.2/14.3.

**Reversed 2026-11-11 (user, [plan 41](41-terminal-user-only.md)): the dock is user-only and agent bash jobs are pipe processes again with their own `N shell` indicator; the paragraph below is history.**

Decision (user, 2026-11-08): running background bash jobs appear in the terminal dock and allow interactive input, rather than being a separate shell indicator. **Core integration implemented:** project-backed jobs share one PTY with the terminal service, preserving `bash_output`, `bash_kill`, output limits, completion notices and explicit single-writer control. Job metadata links `terminal_id`; terminal metadata links `background_job_id`. Without a terminal service/project, the legacy non-interactive pipe fallback remains. PTY echo/CRLF/escape bytes may appear in captured job output. Focused Core terminal/job/inspector tests: **58 passed**. Paired real-Core smoke checks and screenshots pass; the previously reported TUI 404 was not reproduced. [Terminal work and remaining release gates](../reports/terminal-work-2026-11-08.md).

Defaults to confirm at the first design review (change any of them freely):

- **Core owns the PTY** (`nexus-core/nexus/terminals.py`): clients are viewers/controllers, never process owners.
  Resources have opaque ids, a project/workspace, a creator, status and timestamps. Background jobs use the same resources, linked by job and terminal ids.
- **Parser strategy:** Core keeps a bounded raw ring; clients parse it locally (`vt100` in the TUI, xterm.js in NSH).
  Replay after discard resets the parser and announces the gap; it cannot restore state lost with discarded bytes.
  pyte is an optional text snapshot, not a reconnect checkpoint (user decision).
- **One active writer;** many viewers. Control is explicit, shown, and released on disconnect, never handed to an agent.
- **Closing a panel detaches; Close terminates.** A Core restart marks terminals ended, never "resumed".
- **Interactive terminal output is not in model context** unless the user attaches it explicitly (a normal visible message with provenance). Background-job output retains its existing `bash_output` and completion-notice paths into context, including echoed user input. No agent typing in the first version.
- **Access:** loopback or authenticated only. No remote shell over the tokenless non-loopback mode. Escape sequences
  that write the clipboard, open files or launch URLs are gated behind a user action.

Work:

- [x] Shared resource contract following the defaults above (no new client-specific behavior); API, versioned data stream with sequence
  numbers, bounded replay, gap/resync and idempotent input are documented and tested. Design inputs remain available for user review.
  See `docs/api.md` and `docs/terminals.md`; paired screenshots are available for user review.
- [x] Core PTY service: spawn in the chosen workspace, process group, resize, binary-safe I/O, UTF-8 split across
  chunks, signals and exit codes, bounded scrollback with an announced discard, orphan cleanup, limits.
  Core and HTTP/SSE are implemented and tested; normal shutdown of a separately grouped foreground job and
  abrupt-Core-death SIGHUP delivery to that group are verified on macOS. Other platforms remain unverified.
  Slow-viewer overflow immediately revokes control; restart clears all controller identities (11-08).
  Focused terminal/job suites: **40 passed**. A 16 MiB output profile retained 1 MiB, discarded 15 MiB,
  completed in 9.846 s (1.63 MiB/s), and measured 12.55 ms maximum timer delay under concurrent suite load.
- [x] TUI: panel with a terminal tab and resource list; keys arbitrated with global shortcuts (Esc, Ctrl chords,
  paste, mouse); selection/copy through OSC 52.
- [x] NSH: the same panel, tabs, list and keys (xterm.js in a WebView on web/macOS; spike the focus and key capture
  against NSH's keymap first). Manifest commands in `interface/manifest.toml`. Web key capture and actual PTY input are verified;
  native AppKit WebView runtime is not. Both clients reconnect read-only and require Take control again.
- [x] Tests on real PTYs in an isolated workspace: shell I/O, alt-screen program, resize, split byte
  sequences, high-volume output, two clients, control contention, reconnect, exit, close, Core shutdown.
- [x] Docs: `docs/api.md`, `docs/terminals.md`, daemon/tools/inspector docs and both READMEs; paired dock, evidence and inspector screenshots;
  behavior ledger updated. No new client-specific differences introduced.
- [x] Terminal parts of 9.22: immutable selection preview → draft → ordinary send; exact UTF-8 clipping/provenance and untrusted-data encoding;
  exact-tool-linked inspector lifecycle bars and sanitized change invalidation. Interactive agent typing remains deliberately unavailable.
- [ ] Remaining release verification: Linux/other platforms, native AppKit WebView and authenticated remote failure/reconnect matrix.
  The final consolidated automated suite passed on 11-09 (section 4), superseding the 11-08 timeout/failure notes;
  later working-tree changes still need their own full gate. See the report rather than treating implementation ticks as platform approval.
- Exit: a terminal is not done if it renders only line-oriented output.
