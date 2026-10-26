# NSH desktop and web: TUI parity acceptance ledger

**Status: work in progress; parity is not complete.** The target is all native
Ratatui TUI functionality in both the native NSH desktop target and NSH web,
with a similar design language and the same information and actions. This
includes settings, providers, worktrees, Manager, voice, Live and speech; none
is deferred out of acceptance. This ledger is an audit and acceptance checklist,
not a claim that either target has passed.

The source of truth for current behavior is the TUI code in `nexus/ui/ratatui/`
and `rust/tui/`, its command catalog `nexus/ui/cli/commands.py` and shared
helpers in `nexus/ui_support/`. Consult [ratatui-parity.md](ratatui-parity.md)
and [surfaces.md](surfaces.md), but where a historical description conflicts
with code, code wins. Host commands are defined in `nexus/host/protocol.py`;
clients use the host contract, not session files or managers. All host text is
untrusted. Show every parameter, output and context block labelled and readable,
and announce clipping. Preserve durable replay and guard stale asynchronous
responses. No work in paused `rust/desktop/` (GPUI) is implied.

## Current baseline

`rust/app/src/lib.rs` is a small browser-API client: ticket/bootstrap, a
flattened session list, typed ID open/create, snapshot refresh on SSE notices,
plain message send and stop. `rust/client-core/src/lib.rs` constructs those
four commands and recursively renders labelled paths, including empty/null
values; it is not the TUI presentation port. Stop preserves the queue. The
native entry point exists in `rust/app/src/main.rs`, but the shared app uses
browser APIs directly; native connection and desktop behavior have not been
verified. The existing `tests/playwright_nsh_check.py` exercises the narrow
browser path against a fake facade, not full parity or real-provider behavior.
See [web.md](web.md) for the current browser baseline.

## How to use this ledger

Every row is outstanding on **both** targets until the feature and its required
functional *and* design checks pass on that target. `☐` means not established;
it does not distinguish unimplemented from implemented but unverified. Record
test and visual evidence alongside a row before marking it complete. A passing
browser test does not establish native behavior, or vice versa. Requirements in
one row include all applicable TUI actions, states and errors, not only the
examples listed. Platform-specific input, clipboard, audio or window mechanics
may differ; available user functionality and shared layout must not silently
diverge.

## Foundation and navigation

| Feature and acceptance | Host commands / TUI reference | Native | Web |
| --- | --- | :---: | :---: |
| Connect securely, bootstrap, open a session, stream/reconnect and resnapshot; preserve active target across failed or stale requests, show errors, never retry writes automatically, closing a client does not cancel work. | `SessionOpen`, `SessionState`, `SessionSubscribe`; `nexus/ui/ratatui/controller.py`, `nexus/ui_support/session_controller.py` | ☐ | ☐ |
| Shared responsive shell: top bar, tabs, status and breadcrumb; session and details sidebars, conversation, context, composer, dialogs and empty-session hints. Tabs switch/close without deleting or stopping work; show background status and completion notifications. | `ProjectSessionsList`, `Doctor`; `rust/tui/src/render/chrome.rs`, `nexus/ui/ratatui/prototype.py` | ☐ | ☐ |
| Projects and sessions: recent list, bounded search, grouping, open/create, cross-project navigation, rename title, archive/unarchive, trash/restore, fork, export, ownership tree and read-only child inspection. Show truncation and errors; keep session identity distinct from title. | `ProjectList`, `ProjectOpen`, `ProjectSessionsList`, `ProjectSessionOpen`, `SessionResolve`, `SessionOpen`, `SessionSearch`, `SessionArchive`, `SessionUnarchive`, `SessionDelete`, `SessionRestore`, `SessionRename`, `SessionFork`, `SessionExport`, `SessionTree`, `SessionInspect`; `sidebar.py`, `ownership.py`, `workflows.py` | ☐ | ☐ |
| Files and Git sidebar: directory/project picker, workspace file browsing and preview, status and diff; retain host path restrictions and bounded results. | `ProjectDirectoryList`, `DirectoryList`, `FilePreview`, `GitStatus`, `GitDiff`; `nexus/ui/ratatui/sidebar.py` | ☐ | ☐ |
| Session handoff and readback, including navigation and notices. | `SessionHandoff`, `SessionHandoffRead`; `nexus/ui/ratatui/actions.py` | ☐ | ☐ |

## Conversation and composer

| Feature and acceptance | Host commands / TUI reference | Native | Web |
| --- | --- | :---: | :---: |
| Reduced, live and replayed transcript: user turns, agent replies, streaming, thoughts, tool/activity groups, errors, turn usage, fold/expand, follow-latest, copy and announced clipping. Include code-mode/kernel and external-worker rows; preserve ordered content and all details. | Snapshot/events; `nexus/ui/ratatui/prototype.py`, `verb_groups.py`, `codemode.py`, `rust/tui/src/transcript/`, `nexus/ui_support/timeline.py`, `tool_details.py` | ☐ | ☐ |
| Render Markdown, code, tables, links and diffs safely, including streaming/incomplete content, copyable source and explicit fallbacks; inspect complete labelled tool parameters, progress, output, errors and metrics. | Reduced view and durable tool diffs; `rust/tui/src/markdown.rs`, `nexus/ui_support/tool_details.py`, `timeline.py` | ☐ | ☐ |
| Context header and inspector: system prompt, environment, AGENTS.md, MEMORY.md, skills, tools and MCP, previews, counts, token accounting and usage. Inspect complete available content, distinguish unavailable data, honor post-first-turn locks and child read-only context. | `ContextInspect`, `ToolsList`, `SkillInspect`, `McpServerShow`, `ContextExtensionSelect`, `ContextMcpLoadingSelect`, `McpServerRestart`, `ExtensionsReload`; `nexus/ui/ratatui/context_sections.py`, `nexus/ui_support/context_header.py`, `context.py` | ☐ | ☐ |
| Composer: multiline editing, history, selection, per-session draft preservation, slash and `@` file/directory completion, agent/model/effort controls, shell `!` mode and usage/activity display. | `FileSearch`, `SessionShell`, `ModelsList`, `ModelSelect`, `ReasoningEffortSelect`, `AgentsList`, `AgentSelect`; `nexus/ui/ratatui/actions.py`, `nexus/ui_support/completion.py`, `rust/tui/src/editor.rs` | ☐ | ☐ |
| Send while idle or running: steer, queue and interrupt, visible pending inputs, reorder/promote/remove, restoration of removed steering content and attachments, stop/cancel and returned queue behavior. Preserve draft on failed sends. | `SessionEnqueue`, `SessionCancel`, `SessionQueueMove`, `SessionQueueRemove`, `SessionQueueSetMode`; `nexus/ui/ratatui/actions.py`, `prototype.py` | ☐ | ☐ |
| Attach files/images: prepare, label, inspect, remove and send up to host limits; stable numbered references and submitted attachment inspection; retain errors and session-change guards. | `AttachmentPrepare`, `AttachmentPreview`, `SessionEnqueue`; `nexus/ui/ratatui/actions.py`, `workflows.py` | ☐ | ☐ |
| Permission and question prompts: all available decisions, disabled choices, question options/free text, first-responder arbitration and feedback, nested prompts and safe focus. | `PermissionResolve`, `QuestionAnswer`; `nexus/ui/ratatui/permission_prompt.py`, `nexus/ui_support/prompts.py` | ☐ | ☐ |
| Subagents: cards and statuses, metrics and route, read-only child conversation and actual child context, nested navigation, stop/close and historical origin labels. | `AgentTranscript`, `AgentCancel`, `AgentClose`, `SessionInspect`; `nexus/ui/ratatui/workflows.py`, `ownership.py` | ☐ | ☐ |

## Settings and operational workflows

| Feature and acceptance | Host commands / TUI reference | Native | Web |
| --- | --- | :---: | :---: |
| Model/agent selection: searchable model picker, supported effort, details, favorites/recents/sort/refresh, agent picker and session selection with host locks. | `ModelsList`, `ModelsRefresh`, `ModelShow`, `ModelSelect`, `ReasoningEffortSelect`, `AgentsList`, `AgentCurrent`, `AgentSelect`, `AgentReset`; `nexus/ui_support/model_choice.py`, `nexus/ui/ratatui/actions.py` | ☐ | ☐ |
| First-run setup and provider management: status, browser/device/code/key flows, cancellation, logout, refresh and errors; keep credentials masked and inward-only. Provider usage shows cached/fresh/error state and limits. | `SetupStatus`, `SetupSave`, `ProvidersStatus`, `ProviderLogin`, `ProviderLoginPoll`, `ProviderLoginCancel`, `ProviderLoginCode`, `ProviderKeySet`, `ProviderLogout`, `ProviderModelsRefresh`, `ProvidersUsage`; `nexus/ui/ratatui/settings_pages/providers.py` | ☐ | ☐ |
| Settings navigation and all TUI areas: appearance, layout, keyboard, workspace, providers, models, titles, voice, agents, tools, MCP, skills, hooks, config and soul as applicable in current TUI. Scoped editing, validation, autosave, hash conflicts, reset/delete, unsaved drafts and confirmations must match. | `SettingsInventory`, `SettingsRead`, `SettingsWrite`, `SettingsReset`, `SettingsDelete`, `AgentDefaultSet`, `DefaultModelSettings`, `DefaultModelSet`, `ModelTiers`, `ModelTierSet`, `ModelTierReset`, `SessionTitleSettings`, `SessionTitleSettingsSet`; `nexus/ui/ratatui/settings_pages/`, `tier_pages.py`, `workflows.py` | ☐ | ☐ |
| MCP and skills management: session toggles and locks, server and tool inspection, restart/refresh, persistent loading/enabled settings, masked add-server confirmation, skills install and overwrite confirmation, extension reload and diagnostics. | `SettingsMcpAdd`, `SettingsMcpEnabledSet`, `SettingsMcpLoadingSet`, `SkillsInstall`, `ExtensionsList`, `ExtensionsReload`, `McpServerShow`, `McpServerRestart`, `ContextExtensionSelect`; `nexus/ui/ratatui/context_sections.py`, `workflows.py` | ☐ | ☐ |
| Worktrees and Git: session root/status/move/trust, integration, turn diff/revert and child review, paginated review, acknowledgement, integrate/remove/discard including explicit force and host confirmation tokens. | `SessionWorktreeList`, `SessionWorktreeMove`, `SessionWorktreeStatus`, `SessionWorktreeTrust`, `SessionIntegrate`, `SessionTurnDiff`, `SessionTurnRevert`, `WorktreeList`, `WorktreeInspect`, `WorktreeReview`, `WorktreeAcknowledge`, `WorktreeIntegrate`, `WorktreeRemove`, `WorktreeDiscard`; `nexus/ui/ratatui/worktree_pages.py`, `worktree_bar.py`, `workflows.py` | ☐ | ☐ |
| Manager entry, status, board, open/return and follow behavior, including Manager-specific restrictions and separate Live consent. | `ManagerState`, `ManagerOpen`, `LiveConsent`; `nexus/ui/ratatui/manager.py`, `manager_board.py`, `manager_follow.py` | ☐ | ☐ |
| Dictation: readiness and download consent, bounded capture, preview, final insertion, stop/send/discard and settings. | `VoiceStatus`, `VoicePrepare`, `VoiceTranscribe`, `VoiceCancel`, `VoiceRemove`; `nexus/ui/ratatui/voice.py`, `settings_pages/voice.py` | ☐ | ☐ |
| GPT Live: consent and revocation, start/stop, mute/pause, call status, levels and transcript/captions; separate Manager consent and errors. | `LiveStatus`, `LiveStart`, `LiveStop`, `LiveMute`, `LivePause`, `LiveConsent`, `LiveHelperEvent`; `nexus/ui/ratatui/live.py`, `nexus/ui_support/live_card.py` | ☐ | ☐ |
| Spoken output: status, download consent, speak latest answer and stop. | `SpeechStatus`, `SpeechPrepare`, `Speak`, `SpeakStop`; `nexus/ui/ratatui/speak_pages.py` | ☐ | ☐ |
| Details sidebar, logs, jobs, notices and notifications: inspect files/MCP/session information; bounded logs and job output, stop/background jobs, completion/unread indicators and notification history. | `LogsRead`, `SessionJobs`, `SessionJobLog`, `SessionJobStop`, `SessionJobBackground`, `UpdateStatus`, `DatabaseNotice`; `nexus/ui/ratatui/logs.py`, `jobs.py`, `rust/tui/src/details_sidebar.rs` | ☐ | ☐ |
| Full slash catalog, aliases, completion, command palette and keyboard/leader actions, including `/restart`, `/reload`, `/reload-extensions`, `/review`, `/merge`, `/commit`, `/copy`, `/theme`, `/verbose`, help and dev-only `/mock`. Preserve path-vs-command parsing and report unsupported platform operations rather than silently sending them as prompts. Trace each action to its actual host or local workflow. | `nexus/ui/cli/commands.py`, `nexus/ui/ratatui/actions.py`, `nexus/ui_support/shortcuts.py`; dev commands `MockList`, `MockStart`, `MockClean` | ☐ | ☐ |

## Required verification for every completed row

- **Functional, on each target:** exercise normal, empty, loading, error,
  disabled/locked and relevant concurrent/stale-response states. Verify actual
  host command arguments and results, replay/reconnect where durable state is
  involved, and that closing a client does not stop daemon work. Test sensitive
  actions and confirmation tokens, and never expose credentials.
- **Design, on each target:** compare the same populated fixture with the
  current TUI for regions, hierarchy, labels, states, inspectability and
  discoverability. Inspect dark/light and wide/narrow layouts; verify keyboard,
  focus, pointer and dismissal paths and announced clipping. Preserve all
  information, not merely a raw JSON dump or flattened debug output.
- **Cross-target:** check the shared layout at the same sizes on native NSH and
  web, with platform differences limited to necessary OS/browser mechanics.
  Add focused automated tests and browser/native journey or screenshot evidence
  as applicable. Hardware audio, real sign-in and live-provider behavior must
  be separately verified or explicitly remain unverified.

## Work boundaries and ordering

Foundation (transport, shared state and presentation) enables the other rows;
conversation/prompts and composer/queue, navigation/chrome, settings/providers,
worktrees, Manager, and audio are separable feature boundaries over that shared
host contract. This ordering is not a reduction of scope: every row is required
on both targets. Keep target-specific capabilities in platform/transport seams,
and keep shared protocol and presentation independent of NSH where practical.
The plan's milestones and historical scope are not a substitute for this full
acceptance ledger.
