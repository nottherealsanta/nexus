# 41. Terminal is the user's; the bash tool is separate

User request (2026-11-11): "plan to have terminal different from shell tool, separate them please, terminal is only
for user".

This reverses the 2026-11-08 decision in [02](done/02-terminal.md) ("running background bash jobs appear in the terminal
dock and allow interactive input") and the porting-ledger row "Background bash jobs were non-interactive pipe
processes, separate from terminal resources". After this change:

- **The terminal dock holds only terminals a user opened.** Nothing the agent runs appears there, and nothing in the
  dock is reachable by the agent (already true: no agent typing, no agent reads; explicit **Attach selected output**
  stays, because the user does it).
- **The bash tool never creates a terminal resource.** Foreground and background `bash` are pipe processes
  (stdin `/dev/null`, stdout+stderr combined), as they were before 11-08 and as they still are today without a
  terminal service. `bash_output`, `bash_kill`, the job limits and the `→ Background Shell Done` notice are unchanged
  except that output no longer carries PTY echo, CRLF or escape sequences.
- **Background jobs get their own indicator again** (the existing `N shell` button in the location strip, which today
  only counts "legacy" pipe jobs), opening the existing resources list (output view, kill). It is no longer "legacy".

Desktop/web TUI and NSH in the same change; mobile has neither surface.

## Today (where they are joined)

- Core `nexus-core/nexus/bash_jobs.py` `_start` (~153–167): with `terminal_service` and a `project_id`, a job is a
  `TerminalService.create(..., creator="background bash", command=…, background_job_id=…, output_listener=job.retain,
  turn_id=…, tool_call_id=…)` resource, renamed to the command label; `_watch`, `_run`, `_settle` branch on
  `job.terminal`; `Job.to_json` adds `terminal_id`. Wired in `server.py` ~432 (`runtime.jobs.terminal_service = …`).
- Core `nexus-core/nexus/terminals.py`: `command`, `background_job_id`, `output_listener`, `turn_id`/`tool_call_id`
  parameters, the `/bin/bash -c` branch (~161), the "running until job output drained" status rule (~377), and
  `DIAGNOSTIC_FIELDS`.
- Core `nexus-core/nexus/inspector_data.py` ~280–380: terminal `resource_spans` and the `terminal` Timing lane, which
  only ever exist for job-linked terminals (manual terminals have no tool provenance).
- TUI `nexus-tui/src/app.rs` ~2996–3030: `resource_counts` excludes jobs with `terminal_id`;
  `terminal_shell_count` adds linked jobs not yet listed. `terminal_api.rs` / `terminal_panel.rs` carry
  `background_job_id` (and show it in evidence provenance).
- NSH `nexus-nsh/src/ui/composer.tsx` ~758–848: `legacyShells()` dedupes against terminals; `core/terminal.ts`,
  `ui/terminal.tsx`, `terminal-evidence.ts` carry `background_job_id` / tool provenance.
- Fixtures/tests: `interface/scenarios/terminal-dock.json` (`term_job`), `interface/scenarios/terminal-evidence.json`,
  `tests/test_bash_jobs.py`, `tests/test_terminals_http.py`, TUI/NSH terminal tests.
- Docs: `docs/terminals.md` (job sections, agent-control table, diagnostic provenance), `docs/tools.md` (background
  jobs), `docs/api.md`, `docs/inspector.md`, `docs/porting-ledger.md` row 30, `interface/manifest.toml` notes.

## Decisions (user accepted every bracketed default, 2026-11-11)

- [x] Background jobs lose interactive input (the user can no longer type into a server the agent started). [yes;
  that is the point of the split]
- [x] Background jobs go back to pipes, not a private PTY. A PTY without a dock would only add echo/escape noise to
  `bash_output`; programs that need a TTY can use `script`/`unbuffer`. [pipes]
- [x] Inspector: drop the `terminal` Timing lane and `resource_spans` (user terminals have no tool provenance), and
  instead give background jobs a lane from their own `turn_id`/`tool_call_id`/start/finish. [replace with a `job` lane;
  can be a follow-up]. **Outcome: the terminal lane and `resource_spans` are removed; the `job` lane is NOT built and
  stays a follow-up** (jobs live only in daemon memory, so the inspector, which reads the store, would need a runtime
  plumbing step and a retention decision). Jobs now record `turn_id`/`tool_call_id` so that lane is cheap later.
- [x] Evidence provenance keeps only resource id/name, project/workspace and the stream watermark; drop session, turn,
  tool-call and background-job ids (they can no longer be set). [yes]
- [x] A terminal's optional `session_id` stays (it only validates the workspace when opened from a session). [keep]
- [x] Terminal metadata persisted before this change with a `background_job_id`: mark `closed` on load so it leaves the
  list. [yes]

## Work

- [x] Core: remove the terminal branch from `BashJobs` (`terminal_service`, `Job.terminal`, `terminal_id`, the
  `_watch`/`_run`/`_settle` branches) and the wiring in `server.py`. Background jobs always use the pipe path.
- [x] Core: strip job support from `terminals.py` (`command`, `background_job_id`, `output_listener`,
  `turn_id`/`tool_call_id`, the drain-before-exit status rule); `creator` is always a client. Reject or ignore those
  fields in `terminal_api.py`; migrate old linked metadata to `closed`.
- [x] Core: inspector per the decision above; keep `terminal.updated` only if anything still listens to it (it was
  emitted for linked resources only), otherwise remove it.
- [x] Core tests: rewrite the PTY-job cases in `test_bash_jobs.py` as pipe-job cases (output, kill, timeout, notice,
  no echo/CRLF); drop job-link cases from `test_terminals_http.py`; add "running `bash background:true` creates no
  terminal resource".
- [x] TUI: `resource_counts` counts every running job; `terminal_shell_count` is `terminals.len()`; drop
  `background_job_id` from `terminal_api.rs` and evidence provenance; the shell indicator opens the jobs list.
- [x] NSH: same in `composer.tsx` (rename `legacyShells` → `runningShells`, no dedupe), `core/terminal.ts`,
  `ui/terminal.tsx`, `terminal-evidence.ts`.
- [x] Scenarios/fixtures: remove `term_job` from `terminal-dock.json`; a job appears only under the shell indicator.
  Update `terminal-evidence.json` provenance.
- [x] Docs: `docs/terminals.md` (the dock is user-only; delete the background-job paragraphs, simplify the
  agent-control table), `docs/tools.md` (pipe jobs only), `docs/api.md`, `docs/inspector.md`, manifest notes. Ledger:
  replace row 30 with "Terminal dock is user-only; agent bash jobs are pipe processes with their own indicator
  (2026-11-11)". Add the reversal to [decisions.md](decisions.md) and a note at the 11-08 decision in
  [02](done/02-terminal.md).
- [x] Verify: `uv run pytest -q`, ruff, TUI and NSH test suites; paired TUI/NSH screenshots of the dock with a user
  terminal while a background job runs (job only under `1 shell`, dock shows `Terminals (1)`).

## Landed (2026-11-11)

- Core: `bash_jobs.py` is pipe-only (`Job.turn_id`/`tool_call_id` kept for the future lane; no `terminal_id`).
  `terminals.py` lost `command`, `background_job_id`, `output_listener`, `turn_id`/`tool_call_id`, `on_change`,
  `DIAGNOSTIC_FIELDS` and the drain-before-exit rule; `terminal.updated` is gone (nothing listened to it). Old linked
  metadata is marked `closed` on load and loses its provenance keys. `terminal_api.py` ignores unknown fields.
- Inspector: `terminal` removed from the lane/record kinds, the status filter and both clients' legends; fixture
  `inspector-terminals.json` and its TUI/NSH tests removed.
- TUI/NSH: `N shell` counts every running job and opens the jobs list in both (the TUI chip used to open the dock);
  `N terminal shell` is the terminal count; evidence provenance is id/name/project/workspace/stream_seq only.
- Scenarios: `terminal-dock.json` has one terminal and a `background_job` that is not in the dock;
  `terminal-evidence.json` lost its provenance fields.
- Shells modal stop (user, 2026-11-11: "shell background should be visible but separate and in the modal, user should
  be able to stop them"): both clients select a shell with ↑/↓ and stop a running one with `s` or `[stop]` / **Stop**
  (`DELETE /api/bash/{id}`), toast `Shell stopped`, list refreshed. Tests: TUI `resources` 7/7, NSH
  `resources.test.tsx` 5/5; TUI text render and NSH screenshot compared (same structure, hints and selection).
- Follow-up: the inspector `job` lane (not built; see the decision above).
- Paired TUI/NSH screenshots: **not verified** (not taken in this change); the scenario tests assert the same facts
  in both clients (`Terminals (1)`, `1 shell`, `1 terminal shell`, job only under the shell indicator).
