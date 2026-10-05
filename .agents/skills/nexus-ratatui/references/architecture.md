# Native client architecture

## Process model

`nexus chat --renderer ratatui` (or `--renderer auto` when the binary exists) runs
`nexus/ui/ratatui/run.py`, which finds the executable (`NEXUS_TUI_BINARY`, an
installed `nexus-ratatui`, or `rust/tui/target/{debug,release}/nexus-ratatui`) and
starts `prototype.run`. Python spawns the binary with piped stdin/stdout. Rust takes
terminal control through `/dev/tty` (Crossterm `use-dev-tty`), so the IPC pipes do not
collide with input. Closing the client never cancels daemon work.

## Wire format

The live terminal emits schema 3 section deltas: omitted fields retain prior
values; empty/null fields clear them. `reset` starts a session/page, `blocks_from`
replaces a dependent suffix, and history supports initial/appended values. Schema
1/2 remain readable. Never discard dependent patches. One-shot insert/restore
fields are not retained by omission.

Rust → Python carries host actions and sequenced UI-persistence actions. Disclosure
never writes stdout. Rust owns session/page LRU disclosure choices, optimistic
sidebar/tab/file/log choices and static command completion. Python owns canonical
session reduction, labelled content/redaction and host commands. Preferences write
off-loop. See `disclosure.rs`, `local_ui.rs`, `wire.py`, `stream_projection.py` and
`ui_support/native_schedule.py`.

### Snapshot fields (see `bridge.rs`)

| Group | Fields |
| --- | --- |
| Transcript | `blocks[]` (typed, see below), `lines` (legacy fallback, normally `[]`) |
| Chrome | `title`, `status`, `breadcrumb`, `tabs[]`, `sessions[]`, `theme`, `sessions_sidebar`, `details_sidebar`, `context_preview` |
| Details sidebar | `details_panel {session[[label,value]], files[], files_summary, mcp[[tone,text,note]]}` |
| Composer | `agent`, `model`, `provider`, `effort`, `context_usage`, `attachments`, `attachment_lines`, `history`, `restore`, `insert`, `insert_kind`, `auto_send_insert` |
| Overlays | `panel_title`+`panel_lines`, `items[]` (pickers), `prompt {kind,id,lines,choices[]}`, `form`, `logs[]` |
| Voice / completion | `voice_phase`, `voice_preview`, `voice_level`, `completions[]`, `completion_query` |

### Block kinds (`Content` in `bridge.rs`, drawn by `transcript.rs`)

`user` (card: title=first line, text=rest, `number`, `collapsed`, `chips`,
`chip_operation`), `thought` (title, text=suffix, `detail`=expanded body), `agent`
(title, `color`), `markdown`, `tool` (text may be several lines, `status`, `detail`
for verbose output), `diff` (before/after), `summary` (right-aligned footer),
`collapsed`, `error`, `context` (title chip, text body, `status`=token count,
`color` token), and a generic `literal` (title + text). Every block may carry `gap`
(blank rows before), `operation` (what a click does) and `id` (cache key).

## Actions (Rust -> Python)

`submit` (mode queue|steer|interrupt), `command` (slash command text), `complete`,
`operation` (click on a block row), `context_header`, `session_open`, `tab_close`,
`toggle` (sidebars/preferences), `logs`, `answer` (prompt choice/free text),
`pick` / `favorite` (menu items), `form_draft` / `save` (forms), `voice_stop`,
`cancel`, `cycle_effort`, `cycle_agent`, `clipboard`, `copy_text`, `quit`. Dispatch
lives in the run loop of `prototype.py`; menu operations are handled by
`Workflows.operate` (`workflows.py`).

## Flow of a streamed token

Host event → immediate ingest → 16 ms coalesced update → changed-tail root
projection for ordinary text/thought deltas → section/suffix delta → Rust merge
→ changed-block wrapping → viewport draw. Other events and child pages retain
full projection. Trace both runtimes and record large first-load costs.

## Flow of an interaction

Draft edits, scrolling, selection, disclosure and static command completion stay
local. Sidebar/tab/file/log changes redraw locally, then send a sequenced action;
Rust protects newer choices from stale echoes while Python persists. Session/child
fetches and agent commands still use the host. Disclosure is client-lifetime state;
durable sessions remain daemon-owned.

## Shared presentation helpers

`timeline.py` (tool row text, turn footer, agent label, task header/metrics),
`details.py` (session rows, modified files, MCP rows), `context_header.py` (header
blocks, agent colours, tool grouping), `completion.py`, `model_choice.py`,
`shortcuts.py`, `session_groups.py`, `voice_settings.py`, `session_controller.py`.
Add reusable pure presentation logic here.
