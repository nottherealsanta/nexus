# Native client architecture

## Process model

`nexus chat --renderer ratatui` (or `--renderer auto` when the binary exists) runs
`nexus/ui/ratatui/run.py`, which finds the executable (`NEXUS_TUI_BINARY`, an
installed `nexus-ratatui`, or `rust/tui/target/{debug,release}/nexus-ratatui`) and
starts `prototype.run`. Python spawns the binary with piped stdin/stdout. Rust takes
terminal control through `/dev/tty` (Crossterm `use-dev-tty`), so the IPC pipes do not
collide with input. Closing the client never cancels daemon work.

## Wire format (schema 1)

- Python -> Rust: one compact JSON object per line (`separators=(",", ":")`), the
  *snapshot* (`bridge.rs: Snapshot`). `schema` must be 1; `revision` must not go
  backwards; `generation` changes when the session/panel context changes and resets
  Rust-local state (draft, scroll).
- Rust -> Python: one JSON object per line, `{"type": ..., ...}` plus `generation`.
  Python ignores actions whose generation is stale.

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

daemon event -> `controller.ingest` (reducer) -> `update()` -> `project()` (reuses
cached per-turn blocks) -> dedup check -> JSON line -> Rust parses newest snapshot ->
`Cache::update_content` re-wraps only changed blocks -> draw. Known cost: the whole
`blocks` array is resent per token (about 1 MiB at 1,000 turns). A per-turn patch
protocol (schema 2) is proposed in `plans/RATATUI_PLAN.md` but not built.

## Flow of a keypress

Crossterm key -> `main.rs` (leader state, editor, shortcut table) -> either local
(editor edit, scroll, selection) or an action line -> Python handler -> host command
-> new snapshot. Local-only state (draft text, scroll, filter, selection) lives in
Rust and resets on `generation` change; anything that must survive reconnect lives
in Python/host.

## Shared helpers (toolkit-free, used by Textual too)

`timeline.py` (tool row text, turn footer, agent label, task header/metrics),
`details.py` (session rows, modified files, MCP rows), `context_header.py` (header
blocks, agent colours, tool grouping), `completion.py`, `model_choice.py`,
`shortcuts.py`, `session_groups.py`, `voice_settings.py`, `session_controller.py`.
Add new pure logic here, not in `ui/ratatui/` and not in a Textual module.
