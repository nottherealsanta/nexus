# Textual app (`nexus chat`)

The terminal chat shell. It is a **pure host client**: every session, model,
agent, permission and turn operation is a host command over the Unix socket.
It renders the reduced `ConversationView` and never reads session files.
Background on the host is in [core.md](core.md).

## Launch path

`nexus chat` → `cli.py:_chat_entry` / `_chat` → creates a UDS host client
(`client/protocol.py:Client`) → `ui/tui/run.py:run(client, session=…, reconnect=…)`
→ `NexusTextualApp.run_async()`. It requires stdin/stdout TTYs. Non-interactive
callers use `nexus run` (`ui/cli/run.py`) or JSONL (`ui/jsonl.py`).

Textual is pinned (`textual==8.2.8`, `textual-diff-view==0.1.5` in `pyproject.toml`).

## File map

| File | What it owns |
| --- | --- |
| `ui/tui/app.py` | `NexusTextualApp`: layout (`compose`), the `SHORTCUTS` table (single source for `BINDINGS` and the shortcut screen), slash-command dispatch (`_dispatch_chat_command`), the command palette providers, session switching, inline pickers for model, agent and effort, the permission flow, the logs drawer and its polling, the context preview, reconnect, and `_sync_status`/`_sync_agent`/`_sync_timeline`. |
| `ui/tui/controller.py` | `TuiController`: the event bridge and reducer seam. `bootstrap`, `start_turn`, `resume`, `ingest(event)` (applies `view.reduce.apply`), `switch_session`, model/agent/effort selection with metadata refresh, `find_agent`, `cancel`. Holds `view: ConversationView`. |
| `ui/tui/timeline.py` | `ConversationTimeline.set_view(view)` reconciles `TurnWidget`s by durable IDs. `UserMessage`, `AssistantMessage` (streaming Markdown), `ToolActivityWidget` (tool card, expand, diff from `ToolCallView.diff` only), `TaskActivityWidget` + `AgentActivityLink` (inline subagents). |
| `ui/tui/agent_picker.py` | `AgentPickerPanel` (inline picker for agents, models and effort; posts `Selected`/`Cancelled`) and the modal `AgentPicker`. |
| `ui/tui/agent_transcript.py` | `AgentTranscriptScreen`: the live child-agent transcript. |
| `ui/tui/agent_row.py` | Selectable row for one `AgentView`. |
| `ui/tui/permission.py` | `PermissionScreen`: the attended approval choices. Arbitration stays daemon-side. |
| `ui/tui/messages.py` | Typed Textual messages (`EventReceived`, `TurnFinished`, `PermissionRequested`, `AgentPickerRequested`, …). |
| `ui/tui/keys.py` | `NexusDriver`: xterm `modifyOtherKeys` / Kitty CSI-u decoding, so Shift+Enter, Ctrl+Enter and Ctrl+J insert newlines. |
| `ui/tui/theme.py` | `NEXUS_DARK` Textual theme. |
| `ui/tui/app.tcss` | All Textual CSS for the shell. |
| `ui/tui/widgets.py` | Re-exports widgets from `ui_support/tui_widgets.py`. |
| `ui_support/tui_widgets.py` | `ChatInput`/`ChatEditor` (composer, completion popup, `@` file completion), `RootAgentBar`, `ConnectionStatus`, `ContextPreview`, `ContextDetailsScreen`, `LogsDrawer`, `WorktreesScreen`, `WorktreeConfirmScreen`, `context_usage`. |
| `ui_support/timeline.py`, `context.py`, `text.py` | Pure formatting: timeline filtering, context and usage projections, control-safe and redacted text. |
| `ui/cli/commands.py` | Slash commands as data (`SPECS`, `BY_NAME`, `parse`, `help_text`). The TUI palette and the CLI share it. |
| `ui/cli/details.py` | `detail_lines(session, view)`, used by `/details` and the status line. |

## Common changes

- **New slash command:** add a spec to `ui/cli/commands.py`, then a branch in `NexusTextualApp._dispatch_chat_command`. The palette picks it up from `SPECS`.
- **New key binding:** add a row to `SHORTCUTS` in `app.py`. Keys the editor consumes are handled in `on_key` instead.
- **Show new data:** it must come from `ConversationView` (extend `view/model.py` and `view/reduce.py`) or from a host command. See "Adding a capability" in [core.md](core.md).
- **Tool card rendering:** `ToolActivityWidget` in `timeline.py`. Keep all subagent activity inline at the initiating Task call.
- **Styling:** `app.tcss` and `theme.py`. Spacing is covered by `tests/test_tui_spacing.py` and `tests/test_tui_layout.py`.
- Race hygiene: async work re-checks `self.controller.session` and `_agent_metadata_revision` before it applies results. Keep that pattern (`tests/test_tui_session_switch_race.py`).

## Testing

- Pilot tests: `tests/test_ui_tui.py`, `test_tui_*` (activity, completion, composer_agent, functional_journeys, integration_render, keys, layout, markdown, metadata, model picker, session switch race, spacing).
- Deterministic fixtures without a daemon: `tests/tui_acceptance_fixture.py`, `tests/visual_tui_demo.py` (`VisualDemoApp`, states such as `empty`, `transcript`, `permission`, `picker`, `functional`, `reference`).
- In a browser: `tests/browser_serve.py` serves the real shell through textual-serve and adds a Shift/Ctrl+Enter bridge. `tests/playwright_tui_check.py` drives it. `tests/visual_tui_check.py` captures screenshots to `artifacts/visual-tui/`.
- Against a scripted model through the real stack: `tests/mock_llm_serve.py` and `tests/playwright_mock_llm_check.py`.
- The layering rule (`tests/test_ui_layering.py`) keeps Textual imports inside `ui/tui/`. The `ui/` line budget is 5,000 physical lines (`tests/test_phase3_exit.py`).
