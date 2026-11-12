//! The shell's behaviour: focus, the Esc precedence chain, command
//! availability, palette, input handling and the effects that leave the UI
//! (API calls, supervisor commands, clipboard, quit, suspend).
//!
//! Everything user-visible about a command comes from the manifest; a command is
//! offered only when it is wired here (`WIRED`) and its `when` terms hold.

use std::collections::{BTreeMap, HashMap, HashSet};

use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Instant;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;

use crate::actions::{ActionDone, ApiCall, Done, VoiceOperation, VoiceTarget};
use crate::api::{ClientView, Message, Session};
use crate::composer::Composer;
use crate::keys;
use crate::layout::{contains, Flags, HitMap, Regions};
use crate::manifest::{term, Command, Manifest};
use crate::state::{AppState, Banner, ConnectionState};
use crate::sync::{Command as SyncCommand, SyncMsg};
use crate::theme::{Mode, Theme, Tier};
use crate::ui::*;

/// Work the event loop performs on the shell's behalf.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    TerminalAttach {
        id: String,
        client: String,
        generation: u64,
    },
    TerminalStop,
    Quit,
    Suspend,
    Sync(SyncCommand),
    Api(ApiCall),
    Dictation(crate::dictation::CaptureCommand),
    DictationLoadingCue(bool),
    Copy(String),
    PasteImage(String),
    /// Suspend the TUI, open this path in `$VISUAL`/`$EDITOR`, and resume.
    OpenEditor(String),
    /// Open a URL in the platform browser (the inspector, documentation).
    OpenUrl(String),
    /// An OS notification through the terminal (OSC 9 or 777) for a session
    /// that needs the user while it is out of view.
    Notify(String),
    /// A notification cue: a session finished or needs the user.
    Sound(Sound),
}

/// The two notification cues (Settings › Notifications › Notification sounds).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sound {
    /// Every session, including subagents, that finished its turn.
    Finished,
    /// A session waiting for an approval or an answer.
    Input,
    /// The Manager has been thinking for a moment: a soft cue so silence never reads as a dropped call.
    Thinking,
    /// `alt+m` started a Manager call.
    ManagerStart,
    /// GPT-Live is connected: the voice is listening.
    LiveConnected,
}

/// Terminal notifications (Settings › Notifications › Terminal notifications).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Notify {
    Off,
    /// A session waits for approval or an answer.
    #[default]
    Waiting,
    /// Also a finished turn and a failed session.
    All,
}

impl Notify {
    pub fn label(self) -> &'static str {
        match self {
            Notify::Off => "off",
            Notify::Waiting => "waiting only",
            Notify::All => "waiting, done and errors",
        }
    }
}

/// Commands this shell implements against an existing Core API. Everything
/// else is shown disabled with a reason, never as a control that appears to work.
pub const WIRED: &[&str] = &[
    "terminal.open",
    "terminal.new",
    "terminal.acquire",
    "terminal.release",
    "terminal.focus_list",
    "palette.open",
    "app.help_keys",
    "app.quit",
    "app.settings",
    "app.theme",
    "app.reconnect",
    "notifications.open",
    "notifications.mark_all_read",
    "alert.dismiss",
    "alert.confirm",
    "alert.cancel",
    "text_edit.cancel",
    "overlay.close",
    "nav.drawer.close",
    "nav.back",
    "nav.root",
    "sidebar.step_back",
    "sidebar.left.toggle",
    "sidebar.right.toggle",
    "sidebar.tab.sessions",
    "sidebar.tab.files",
    "sidebar.tab.projects",
    "copy.file_path",
    "file.open_external",
    "file.insert_mention",
    "sidebar.tab.git",
    "git.discard",
    "git.branch_delete_force",
    "git.refresh",
    "git.stage",
    "git.unstage",
    "git.commit",
    "git.fetch",
    "git.pull",
    "git.push",
    "git.switch",
    "git.branch_create",
    "git.branch_delete",
    "files.new_file",
    "files.new_folder",
    "files.rename",
    "files.delete",
    "files.toggle_ignored",
    "files.root_toggle",
    "sidebar.focus",
    "git.open_diff",
    "composer.focus",
    "composer.undo",
    "composer.redo",
    "timeline.focus",
    "session.new",
    "session.open",
    "session.switch",
    "session.search",
    "project.switch",
    "project.open",
    "project.add",
    "project.remove",
    "project.move_up",
    "project.move_down",
    "project.status.current",
    "project.status.passive",
    "project.status.inactive",
    "project.overview",
    "sidebar.row_menu",
    "session.rename",
    "session.archive",
    "session.unarchive",
    "session.delete",
    "session.interrupt",
    "session.continue",
    "session.speak",
    "session.speak_stop",
    "session.handoff",
    "session.open_origin",
    "manager.talk",
    "manager.start",
    "manager.header",
    "manager.audio_toggle",
    "manager.mute_toggle",
    "manager.context",
    "manager.open_session",
    "manager.stop",
    "manager.minimise",
    "manager.turn_cancel",
    "manager.send",
    "manager.field_send",
    "manager.card_mic",
    "manager.listen_cancel",
    "manager.speech_stop",
    "manager.field_close",
    "manager.card_dismiss",
    "manager.field_open",
    "manager.card_pick",
    "voice.dictate",
    "search.start",
    "voice.download_stt",
    "voice.remove_stt",
    "voice.download_tts",
    "voice.remove_tts",
    "session.send",
    "composer.steer",
    "composer.queue",
    "session.export",
    "session.change_directory",
    "session.resources",
    "session.worktree_info",
    "turn.go_to",
    "composer.history_search",
    "model.change",
    "model.effort",
    "agent.change",
    "agent.cycle",
    "skills.learn",
    "composer.newline",
    "session.jump_latest",
    "session.copy_id",
    "session.open_inspector",
    "app.open_inspector",
    "session.child.open",
    "sessions.scope_toggle",
    "sessions.archived_filter",
    "turn.fold_toggle",
    "turn.fold_all_but_last",
    "turn.unfold_all",
    "turn.prev",
    "turn.next",
    "tool.expand_toggle",
    "tool.raw_toggle",
    "tool.show_all_output",
    "timeline.detail_cycle",
    "session.tab_close",
    "session.tab_next",
    "session.tab_prev",
    "tool.open_inspector",
    "copy.final_message",
    "copy.assistant_block",
    "copy.code_block",
    "copy.tool_output",
    "copy.user_message",
    "turn.load_older",
    "editor.save",
    "editor.reload",
    "editor.find",
    "editor.preview",
    "editor.close",
    "app.usage",
    "waiting.allow_once",
    "waiting.allow_session",
    "waiting.deny_note",
    "waiting.deny",
    "waiting.submit_answers",
    "waiting.next",
    "waiting.prev",
    "waiting.focus",
    "waiting.custom",
    "waiting.go_to",
];

/// Commands that change Core state: disabled while the connection is not live.
const MUTATING: &[&str] = &[
    "project.move_up",
    "project.move_down",
    "project.status.current",
    "project.status.passive",
    "project.status.inactive",
    "project.remove",
    "session.new",
    "session.rename",
    "session.archive",
    "session.unarchive",
    "session.delete",
    "session.interrupt",
    "session.continue",
    "session.speak",
    "session.speak_stop",
    "session.handoff",
    "session.btw",
    "voice.dictate",
    "search.start",
    "voice.download_stt",
    "voice.remove_stt",
    "voice.download_tts",
    "voice.remove_tts",
    "session.send",
    "session.export",
    "session.change_directory",
    "turn.go_to",
    "model.change",
    "model.effort",
    "agent.change",
    "agent.cycle",
    "skills.learn",
    "waiting.allow_once",
    "waiting.allow_session",
    "waiting.deny_note",
    "waiting.deny",
    "waiting.submit_answers",
];

const NOT_WIRED: &str = "Not available yet.";
pub(crate) const READ_ONLY: &str = "Read-only until Core is connected again.";
/// Why the composer cannot take text while a question waits in this session.
pub(crate) const QUESTION_LOCK: &str = "Answer the question above";
/// A second Ctrl+C this soon after the first quits the TUI.
const CTRL_C_QUIT_WINDOW: std::time::Duration = std::time::Duration::from_secs(2);
/// The timeline scroll bar stays at full strength this long after a scroll…
pub fn scrollbar_visible() -> std::time::Duration {
    std::time::Duration::from_millis(crate::theme::motion_durations().scrollbar_visible_ms)
}
/// …then dims for this long before it disappears.
pub fn scrollbar_fade() -> std::time::Duration {
    std::time::Duration::from_millis(crate::theme::motion_durations().scrollbar_fade_ms)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WaitKind {
    Approval,
    Question,
}

/// One question's in-progress answer. It survives page changes, so left/right
/// can revisit a question and the review can show what was written.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AskPick {
    /// Highlighted option.
    pub cursor: usize,
    /// Ticked option indices (multi-select) or the single picked option.
    pub chosen: Vec<usize>,
    /// The custom answer, edited inline in the card.
    pub input: Composer,
    /// True while the card's text field has focus (`alt+a` toggles it).
    pub field: bool,
}

/// Answers collected so far for one `ask` request.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AskDraft {
    /// One pick per question, in question order.
    pub picks: Vec<AskPick>,
    /// `0..questions.len()` is a question; `questions.len()` the review page.
    pub page: usize,
    /// The reply is in flight; a second submit would only get a 409.
    pub sent: bool,
    pub scroll: usize,
    pub scroll_max: usize,
}

/// The page the card shows now: a question, or the review when `review()`.
#[derive(Debug, Clone, PartialEq)]
pub struct AskCurrent {
    pub wait: WaitItem,
    pub page: usize,
    pub total: usize,
    pub questions: Vec<Value>,
    pub picks: Vec<AskPick>,
    pub sent: bool,
}

impl AskCurrent {
    /// The last page: every question and the answer given to it.
    pub fn review(&self) -> bool {
        self.page >= self.total
    }

    pub fn question(&self) -> Option<&Value> {
        self.questions.get(self.page)
    }

    pub fn pick(&self) -> Option<&AskPick> {
        self.picks.get(self.page)
    }

    pub fn options(&self) -> &[Value] {
        self.question()
            .and_then(|q| q["options"].as_array())
            .map_or(&[] as &[Value], Vec::as_slice)
    }

    pub fn multiple(&self) -> bool {
        self.question()
            .is_some_and(|q| q["multiple"].as_bool().unwrap_or(false))
    }

    /// True while the custom answer field has the card's focus. A question
    /// without options has nothing else to focus.
    pub fn field(&self) -> bool {
        !self.review() && (self.options().is_empty() || self.pick().is_some_and(|p| p.field))
    }

    /// The answer to question `i` as the review page shows it.
    pub fn answer(&self, i: usize) -> Option<String> {
        ask_pick_text(self.questions.get(i)?, self.picks.get(i)?)
    }

    /// The complete reply, in question order.
    pub fn answers(&self) -> Vec<Value> {
        ask_answers(&self.questions, &self.picks)
    }
}

/// The complete reply for `questions`, in question order.
fn ask_answers(questions: &[Value], picks: &[AskPick]) -> Vec<Value> {
    questions
        .iter()
        .enumerate()
        .map(|(i, q)| {
            let pick = picks.get(i).cloned().unwrap_or_default();
            let opts = q["options"]
                .as_array()
                .map_or(&[] as &[Value], Vec::as_slice);
            let mut chosen: Vec<usize> = pick
                .chosen
                .iter()
                .copied()
                .filter(|i| *i < opts.len())
                .collect();
            chosen.sort_unstable();
            chosen.dedup();
            let selected: Vec<Value> = chosen.iter().map(|i| opts[*i]["id"].clone()).collect();
            let text = pick.input.expanded_text();
            let text = text.trim();
            json!({
                "question_id": q["id"],
                "selected": selected,
                "text": if !text.is_empty() {
                    Value::from(text)
                } else {
                    Value::Null
                },
            })
        })
        .collect()
}

/// The selected option labels and free text of one answer, for the review.
fn ask_pick_text(q: &Value, pick: &AskPick) -> Option<String> {
    let opts = q["options"]
        .as_array()
        .map_or(&[] as &[Value], Vec::as_slice);
    let mut parts: Vec<String> = pick
        .chosen
        .iter()
        .filter_map(|i| opts.get(*i))
        .filter_map(|o| o["label"].as_str())
        .map(str::to_owned)
        .collect();
    let text = pick.input.expanded_text();
    let text = text.trim();
    if !text.is_empty() {
        if parts.is_empty() {
            parts.push(text.to_owned());
        } else {
            parts.push(format!("\"{text}\""));
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join(", "))
    }
}

/// Why the answer to `q` is not ready to send, if it is not.
fn ask_pick_problem(q: &Value, pick: &AskPick) -> Option<&'static str> {
    let opts = q["options"]
        .as_array()
        .map_or(&[] as &[Value], Vec::as_slice);
    let selected = pick.chosen.iter().filter(|i| **i < opts.len()).count();
    let text = !pick.input.expanded_text().trim().is_empty();
    if opts.is_empty() {
        if !text {
            return Some("Type your answer in the question card");
        }
    } else if q["multiple"].as_bool().unwrap_or(false) {
        if selected == 0 && !text {
            return Some("Pick at least one option (space or a number)");
        }
    } else {
        if selected > 1 {
            return Some("This question takes one option");
        }
        if selected == 0 && !text {
            return Some("Pick an option with up/down and enter, or type an answer");
        }
    }
    None
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WaitItem {
    pub kind: WaitKind,
    pub id: String,
    pub session_id: String,
    /// The title of the sub agent it waits in, when not the viewed one.
    pub child: Option<String>,
}

pub struct SideEntry {
    pub id: String,
    /// Project name header printed before this row (first row of a project).
    pub project: Option<String>,
    /// Worktree sub-group `(branch or folder, root path)` printed before this
    /// row: the first row of a linked worktree in a project with several roots.
    pub tree: Option<(String, String)>,
}

/// One line of the settings modal.
#[derive(Debug, Clone)]
pub enum SettingsItem {
    Heading(String),
    /// Read-only text, such as a configuration problem. Not selectable.
    Note(String),
    Row {
        row: Box<crate::manifest::Row>,
        /// The section's unavailable reason, which disables its rows.
        section_reason: Option<String>,
    },
}

fn row_matches(row: &crate::manifest::Row, query: &str) -> bool {
    row.label.to_lowercase().contains(query) || row.note.to_lowercase().contains(query)
}

/// The task tools a new session can be given, in the order the Tools row lists them.
pub const TOOLS: [&str; 12] = [
    "read",
    "grep",
    "glob",
    "ls",
    "bash",
    "edit",
    "patch",
    "write",
    "web_search",
    "web_fetch",
    "ask",
    "code",
];

/// The one-row entry a page section gets in the search results.
fn page_row(section: &crate::manifest::Section) -> crate::manifest::Row {
    crate::manifest::Row {
        id: format!("{}.page", section.id),
        label: section.label.clone(),
        kind: "page".into(),
        scope: Some("core".into()),
        clients: "both".into(),
        default: None,
        options: Vec::new(),
        command: None,
        unavailable_reason: None,
        note: section.note.clone(),
    }
}

/// How a line on a Settings page is drawn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Muted,
    Secondary,
    Warning,
    Error,
}

/// One line of a Settings page (Providers, Agents): headings and notes, and
/// the selectable items that act.
#[derive(Debug, Clone)]
pub enum PageLine {
    Heading(String),
    /// A heading with muted detail on the right (an agent's source and tools).
    Title(String, String),
    Text(String, Tone),
    Item {
        label: String,
        value: String,
        tone: Tone,
        action: PageAction,
    },
}

#[derive(Debug, Clone)]
pub enum PageAction {
    RefreshModels,
    /// Open one provider's detail modal.
    OpenProvider(String),
    /// Start one of the provider's sign-in methods (`browser`, `device`, `cli`, `local` or `api_key`).
    Connect {
        id: String,
        method: String,
    },
    Disconnect(String),
    CancelLogin(String),
    CopyCode(String),
    /// A sign-in link: Enter or a click opens it in the browser.
    OpenUrl(String),
    /// A manifest row drawn on the page (default agent and effort, unavailable rows).
    Setting(Box<crate::manifest::Row>),
    /// A ref in an agent's model list (`fallback` false) or fallback list.
    Ref {
        agent: String,
        fallback: bool,
        model: String,
    },
    Add {
        agent: String,
        fallback: bool,
    },
    /// Open one subagent's page (its models and fallbacks).
    OpenAgent(String),
}

/// The row that starts one provider sign-in method.
fn sign_in_label(method: &str) -> String {
    match method {
        "browser" => "Sign in with browser".into(),
        "device" => "Sign in with a device code".into(),
        "cli" => "Sign in with Claude Code".into(),
        "local" => "Use the local server".into(),
        other => format!("Sign in ({other})"),
    }
}

/// Which Git group a changed-file row belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GitGroup {
    Staged,
    Unstaged,
    Untracked,
}

impl GitGroup {
    pub fn label(self) -> &'static str {
        match self {
            GitGroup::Staged => "Staged",
            GitGroup::Unstaged => "Unstaged",
            GitGroup::Untracked => "Untracked",
        }
    }
}

/// One changed file in a Git group (a path can be in Staged and Unstaged).
#[derive(Debug, Clone, PartialEq)]
pub struct GitRow {
    pub group: GitGroup,
    pub path: String,
    pub status: String,
    pub added: u64,
    pub removed: u64,
}

/// The Files root header.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FilesHeader {
    pub name: String,
    pub branch: Option<String>,
    /// More than one checkout: the header switches between them.
    pub switchable: bool,
}

/// One drawable row of the Files tree.
#[derive(Debug, Clone, PartialEq)]
pub struct FileRow {
    /// Path relative to the selected root (also the key of its listing).
    pub path: String,
    pub name: String,
    /// `file`, `directory`, `symlink` or `error`.
    pub kind: String,
    pub depth: usize,
    pub expanded: bool,
    pub git_status: Option<String>,
    pub ignored: bool,
}

/// Where preferences are saved, what the file holds and what this run last
/// observed, so that only settings the user changed are written back.
pub struct PrefsSync {
    home: std::path::PathBuf,
    stored: crate::prefs::Prefs,
    seen: crate::prefs::Prefs,
}

/// The file view's `/` search: the query, whether it is still being typed
/// (letters edit it; otherwise `n`/`N` move between matches), and the index
/// of the current match.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FileSearch {
    pub query: String,
    pub typing: bool,
    pub hit: Option<usize>,
    pub replacement: String,
    pub replace_focus: bool,
    pub case_sensitive: bool,
}

/// The composer's as-you-type completion: what was asked of Core, what came back,
/// and which row is highlighted. Rows themselves are derived from the draft.
#[derive(Debug, Default)]
pub struct CompletionState {
    /// The highlighted row of the popup.
    pub index: usize,
    /// The token last seen at the cursor; a different one resets `index`.
    token: String,
    /// Esc hid the popup for exactly this token.
    hidden: Option<String>,
    /// Project and session the cache and pool belong to.
    scope: (Option<String>, Option<String>),
    /// Core replies by prefix (context + `@token`).
    cache: Vec<(String, Vec<String>)>,
    /// The empty-query listing that pads short menus.
    pool: Option<Vec<String>>,
    /// Prefixes and the pool already asked for in this scope.
    asked: Vec<String>,
    pool_asked: bool,
}

/// Replies kept per scope.
const COMPLETION_CACHE: usize = 64;
/// Hits asked of Core for a token, and the size of the padding listing.
const COMPLETION_HITS: u32 = 30;
const COMPLETION_POOL: u32 = 100;

/// Choices made before a session exists; they go into its creation request.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Draft {
    pub agent: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    /// Session-local tool, skill and MCP switches for the next session.
    pub disabled_tools: Vec<String>,
}

impl Draft {
    /// The session fields these choices set.
    pub fn fields(&self) -> Value {
        let mut f = serde_json::Map::new();
        if let Some(a) = &self.agent {
            f.insert("agent".into(), a.clone().into());
        }
        if let Some(m) = self.model.as_ref().filter(|m| !m.is_empty()) {
            f.insert("model".into(), m.clone().into());
        }
        if let Some(e) = self.effort.as_ref().filter(|e| !e.is_empty()) {
            f.insert("effort".into(), e.clone().into());
        }
        if !self.disabled_tools.is_empty() {
            f.insert("disabled_tools".into(), json!(self.disabled_tools));
        }
        Value::Object(f)
    }
}

/// Per-project data for the not-yet-created session screen.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ProjectInventory {
    pub skills: Vec<Value>,
    pub mcp: Vec<Value>,
    pub skills_error: Option<String>,
    pub mcp_error: Option<String>,
    /// `GET /api/project/{id}/mcp` failed (the global read may still have worked).
    pub project_mcp_error: Option<String>,
    /// Names defined in both project files; `.mcp.json` wins each one.
    pub project_mcp_duplicates: usize,
}

/// One row of the projects overview.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OverviewRow {
    Group(&'static str),
    Project(String),
    /// A project with no active root session.
    Empty(String),
    Session(String, bool),
    ArchivedToggle(String, usize),
}

#[derive(Debug, Clone)]
pub struct PendingFirstPrompt {
    pub text: String,
    pub images: usize,
    pub session: Option<String>,
    pub history_loaded: bool,
    pub choices: Draft,
}

/// The first-send pin: the session, and the scroll it applied once drawn (a different scroll means the
/// reader moved, which ends the pin).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FirstPin {
    pub session: String,
    pub applied: Option<Scroll>,
}

pub struct Ui {
    /// Sessions opened as header tabs, in the order they were opened.
    pub tabs: Vec<String>,
    pub detail: crate::ui::Detail,
    pub draft: Draft,
    pub first_prompts: HashMap<String, PendingFirstPrompt>,
    /// A session's first message is pinned to the top of the timeline until the reader scrolls.
    pub first_pin: Option<FirstPin>,
    pub completion: CompletionState,
    pub focus: Focus,
    pub left_open: bool,
    pub last_project_session: HashMap<String, String>,
    /// Width of the full left sidebar (dragged by its edge).
    pub left_width: u16,
    /// A drag of the left sidebar's edge is in progress.
    pub left_drag: bool,
    /// A project tile drag in progress.
    pub rail_drag: Option<crate::ui::RailDrag>,
    pub right_open: bool,
    /// The user (or a saved preference) chose the right sidebar's state, so a
    /// size change no longer picks it (D17: closed by default at 100-119).
    pub right_chosen: bool,
    /// `set_size` has run once: the construction size may be a placeholder.
    pub sized: bool,
    pub drawer: Option<Side>,
    pub tab: Tab,
    pub cursor: HashMap<Tab, usize>,
    pub side_top: usize,
    pub file_open: bool,
    /// The editor over the open file, when editing (K10).
    pub file_edit: Option<FileEdit>,
    pub file_scroll: usize,
    /// Show the source of a Markdown file instead of its rendered preview.
    pub file_source: bool,
    /// The Git diff open in the centre column: its path and group.
    pub diff_open: Option<(String, GitGroup)>,
    pub diff_scroll: usize,
    /// Cursor among the right sidebar's changed-file rows.
    pub git_cursor: usize,
    pub file_search: Option<FileSearch>,
    /// Files tree: expanded relative directories, ignored visibility, and
    /// whether to browse the project checkout instead of the session directory.
    pub files_expanded: std::collections::BTreeSet<String>,
    pub files_hide_ignored: bool,
    pub files_project_root: bool,
    pub composer_drag: Option<(usize, usize)>,
    pub scope_all: bool,
    pub show_archived: bool,
    pub unread_sessions: HashSet<String>,
    pub failed_sessions: HashSet<String>,
    pub notify: Notify,
    /// The local notification cues; `notifications.sound` in `tui.json`.
    pub sound: bool,
    pub view: View,
    pub pending_selected: Option<usize>,
    pub settings_row: usize,
    pub settings_search: Composer,
    /// The Settings section shown in the right pane, and whether the left rail has focus.
    pub settings_section: String,
    pub settings_nav_focus: bool,
    /// The tool chip highlighted by the Tools row's left/right keys.
    pub tool_cursor: usize,
    /// The Agents detail page drilled into (`agents/<name>`) and its selected item.
    pub settings_page: Option<String>,
    pub settings_page_row: usize,
    /// The Agents page row to return to from a subagent's page.
    pub settings_page_back: usize,
    /// The last `provider.login` state per provider: a pending sign-in shows its code on the Providers page.
    pub provider_logins: BTreeMap<String, Value>,
    /// The provider whose detail modal is open, and its selected row.
    pub provider_detail: Option<String>,
    pub provider_detail_row: usize,
    /// The agent list a model chosen in the palette is added to: (agent, fallback).
    pub pool_pick: Option<(String, bool)>,
    pub overlay: Option<Overlay>,
    pub btw: Option<crate::btw::Btw>,
    pub dialog: Option<Dialog>,
    pub dictation: crate::dictation::DictationUi,
    /// The Manager's indicator, caption and card (`manager.rs`).
    pub manager: crate::manager::ManagerUi,
    /// Last `/api/voice` response used by Settings and download confirmations.
    pub voice_status: Option<Value>,
    pub voice_status_error: Option<String>,
    pub voice_status_generation: u64,
    pub voice_status_loading: bool,
    /// Last `/api/search/service` response, its error, and a start in flight.
    pub search_service: Option<Value>,
    pub search_service_error: Option<String>,
    pub search_starting: bool,
    pub edit: Option<TextEdit>,
    pub alerts: Alerts,
    pub scroll: Scroll,
    pub scrolls: HashMap<String, Scroll>,
    /// Show a subdued timeline scroll bar while the user scrolls (`appearance.scrollbar`).
    pub scrollbar: bool,
    /// When the timeline was last scrolled, for the bar's short idle timeout.
    pub scroll_mark: Option<Instant>,
    pub drafts: HashMap<String, Composer>,
    pub image_pasting: HashSet<String>,
    /// The `/learn` source waiting for its project-or-global answer.
    pub learn_source: Option<String>,
    /// Core's search hits for the session switcher: the query they answer and the hits.
    pub search: Option<(String, Vec<Value>)>,
    /// An older page of messages is being read.
    pub loading_older: bool,
    /// A session whose automatic older-page read failed; it is not retried until Enter asks.
    pub older_failed: Option<String>,
    pub wait_idx: usize,
    /// The highlighted choice of the focused approval card (0 allow once, 1 allow for session, 2 deny, 3 deny with a note).
    pub wait_choice: usize,
    /// When each waiting request was first seen, for `waiting on you · 0:42`.
    pub wait_seen: HashMap<String, Instant>,
    /// The card and clock text last announced by `tick`.
    pub wait_shown: Option<(String, u64)>,
    /// One draft per pending `ask` request, so parallel asks keep their answers.
    pub asks: HashMap<String, AskDraft>,
    pub project: Option<String>,
    /// Content rows and viewport rows of the timeline at the last draw.
    pub timeline_total: usize,
    pub timeline_height: usize,
    /// Explicit fold state, by block key. Absent means the block's default
    /// (turns open, reasoning and tool detail closed, context open only before
    /// the first turn).
    pub timeline_open: HashMap<String, bool>,
    /// Keys of the activity groups that were live at the last draw, and the
    /// frame at which a group finished (its glyph flashes for `flash_frames()`).
    pub live_groups: std::collections::HashSet<String>,
    pub group_flash: HashMap<String, usize>,
    /// The block the keyboard is on, as an index into `timeline_blocks`.
    pub timeline_sel: Option<usize>,
    /// The selected block's key, so a fold (which changes line ranges and may
    /// add or remove blocks) keeps the selection on the same item.
    pub timeline_sel_key: Option<String>,
    /// Blocks of the last drawn timeline (selection and clicks use them).
    pub timeline_blocks: Vec<crate::timeline::Block>,
    /// Selected switch row in the starting-context inventory.
    pub context_cursor: usize,
    /// The directory Nexus was launched from, until it has been opened as a project.
    pub launch_dir: Option<String>,
    /// The launch directory's project is being opened: then show its new-session screen.
    pub launch_new: bool,
    /// Cached skill and MCP listings for the new-session preview, keyed by project.
    pub inventories: HashMap<String, ProjectInventory>,
    /// The last draft-context request sent to Core; a changed draft asks again.
    pub draft_asked: Option<Value>,
    /// The model picker was opened unprompted because no model was ever chosen; once per launch.
    pub model_prompted: bool,
    /// A `/learn` install is in flight, so its early `skills.changed` event is quiet.
    pub learn_install_pending: bool,
    /// Ignore the matching `skills.changed` event after this client's successful install.
    pub learn_suppress_until: Option<Instant>,
    pub last_skills_changed_toast: Option<Instant>,
    pub last_mcp_changed_toast: Option<Instant>,
    pub monochrome_before: Option<Tier>,
    /// Client preferences file state; `None` until `App::apply_prefs`.
    pub prefs: Option<PrefsSync>,
    /// The launch splash: logo, milestone bar and the wipe into the first
    /// live frame. It is over for the rest of the run once it finishes.
    pub splash: crate::splash::Splash,
    /// The session tab under the pointer and the frame its title marquee began.
    pub tab_hover: Option<(usize, usize)>,
}

pub struct App {
    file_watch: Option<(String, Option<String>, Instant)>,
    file_watch_client: String,
    pub file_watch_status: String,
    pub manifest: Arc<Manifest>,
    pub theme: Theme,
    pub state: AppState,
    pub ui: Ui,
    pub hits: HitMap,
    pub regions: Regions,
    pub effects: Vec<Effect>,
    pub terminal_open: bool,
    pub terminal_panel: crate::terminal_panel::TerminalPanel,
    pub terminal_selected: bool,
    pub terminal_focused: bool,
    pub terminal_controlled: bool,
    pub clock: Instant,
    pub size: (u16, u16),
    pub frame: usize,
    pub mouse: Option<(u16, u16)>,
    /// The view last handed to the supervisor; `sync_view` reports a change.
    view_sent: Option<ClientView>,
    /// The session `ui.scroll` belongs to, so another session's offset is never reused.
    scroll_session: Option<String>,
    pub timeline_screen: Option<ratatui::buffer::Buffer>,
    pub timeline_drag: Option<(crate::selection::Selection, Option<Target>)>,
    /// Rendered message bodies, keyed by content/width/theme (timeline.rs).
    pub layout: std::cell::RefCell<crate::timeline::LayoutCache>,
    /// Bumped whenever a sync message may change the timeline's model inputs.
    pub timeline_revision: std::cell::Cell<u64>,
    /// Last built timeline and the key it was built for.
    pub timeline_cache: std::cell::RefCell<Option<(u64, std::rc::Rc<crate::timeline::Timeline>)>>,
    /// The new-session starting-context rows and the key they were built for:
    /// assembling them clones the draft plan, catalog and every inventory row.
    pub preview_cache: std::cell::RefCell<Option<(u64, std::rc::Rc<Vec<crate::paint::CtxRow>>)>>,
    /// Test/diagnostic counter for real preview builds.
    pub preview_builds: std::cell::Cell<u64>,
    /// Test/diagnostic counter for real timeline builds.
    pub timeline_builds: std::cell::Cell<u64>,
    /// Terminal image protocol and the decoded states behind timeline previews.
    pub image_viewer: std::cell::RefCell<crate::images::Viewer>,
    /// The terminal's copy of the drawing modal's picture and strokes.
    pub annotate_live: std::cell::RefCell<crate::annotate::Live>,
    bindings: HashMap<(String, String), String>,
    shortcut_leader: Option<Instant>,
    /// Last kernel/shell refresh while a session tree was busy.
    last_resources_at: Option<Instant>,
    /// First Ctrl+C on an empty composer; a second within two seconds quits.
    ctrl_c: Option<Instant>,
}

/// Subsequence match of `query` in `text`, preferring the tightest window,
/// word starts and consecutive runs. `None` when it does not match or the
/// matched characters are spread too thin to be what the user meant.
fn fuzzy(query: &str, text: &str) -> Option<i32> {
    let q: Vec<char> = query.to_lowercase().chars().filter(|c| *c != ' ').collect();
    if q.is_empty() {
        return Some(0);
    }
    let t: Vec<char> = text.to_lowercase().chars().collect();
    let mut best: Option<i32> = None;
    for start in (0..t.len()).filter(|i| t[*i] == q[0]) {
        let (mut qi, mut score, mut last) = (0, 0i32, None::<usize>);
        for (i, c) in t.iter().enumerate().skip(start) {
            if qi < q.len() && *c == q[qi] {
                score += 10;
                if last == Some(i.wrapping_sub(1)) {
                    score += 15;
                }
                if i == 0 || !t[i - 1].is_alphanumeric() {
                    score += 20;
                }
                last = Some(i);
                qi += 1;
            }
        }
        if qi == q.len() {
            let span = last.unwrap_or(start) + 1 - start;
            if span <= 2 * q.len() + 2 {
                let s = score - span as i32 - (t.len() as i32) / 8;
                best = Some(best.map_or(s, |b| b.max(s)));
            }
        }
    }
    best
}

/// Whether applying `msg` can change what the next frame shows.
///
/// Conservative by construction: every variant and every unknown event kind
/// asks for a draw. Only raw provider traffic (`wire_event`, `wire_response`)
/// is invisible — the reducer drops both from the selected view's scratch (so
/// they never reach the timeline's diagnostics), `App::apply_sync` has no
/// branch for them, and they produce no toast, alert, sidebar mark or effect.
/// `wire_request` stays visible: its model feeds the composer and turn
/// metadata. An invisible message is still applied, advancing the reducer's
/// cursor and deduplication state; it just does not need a frame.
pub fn sync_msg_affects_view(msg: &SyncMsg) -> bool {
    match msg {
        SyncMsg::Event(event) => !matches!(event.kind.as_str(), "wire_event" | "wire_response"),
        _ => true,
    }
}

impl App {
    fn terminal_select(&mut self, index: usize) {
        if self.terminal_panel.input_busy || !self.terminal_panel.input_queue.is_empty() {
            self.toast(Level::Warning, "Wait for terminal input to be acknowledged");
            return;
        }
        let Some(terminal) = self.terminal_panel.terminals.get(index) else {
            return;
        };
        let id = terminal.id.clone();
        if self.terminal_panel.selected.as_deref() == Some(&id)
            && self.terminal_panel.attachment.is_some()
        {
            self.terminal_focused = true;
            self.terminal_panel.focus = crate::terminal_panel::TerminalFocus::Screen;
            return;
        }
        self.terminal_panel.generation += 1;
        self.terminal_panel.reconnect_at = None;
        self.terminal_panel.acquire_on_attach = true;
        self.terminal_controlled = false;
        self.terminal_panel.next_input = 1;
        self.terminal_panel.input_busy = false;
        self.terminal_panel.input_queue.clear();
        self.terminal_panel.resize_busy = false;
        self.terminal_panel.control_busy = false;
        self.terminal_panel.size = None;
        self.terminal_panel.selection = None;
        self.terminal_panel.screen =
            crate::terminal_screen::TerminalScreen::new(terminal.rows, terminal.cols);
        self.terminal_panel.selected = Some(id.clone());
        self.terminal_panel.attachment = None;
        self.terminal_selected = true;
        self.terminal_focused = true;
        self.terminal_panel.focus = crate::terminal_panel::TerminalFocus::Screen;
        self.effects.push(Effect::TerminalAttach {
            id,
            client: self.terminal_panel.client_id.clone(),
            generation: self.terminal_panel.generation,
        });
    }
    fn terminal_send_next(&mut self) {
        if !self.terminal_controlled || self.terminal_panel.input_busy {
            return;
        }
        let (Some(id), Some(attachment), Some(bytes)) = (
            self.terminal_panel.selected.clone(),
            self.terminal_panel.attachment.clone(),
            self.terminal_panel.input_queue.pop_front(),
        ) else {
            return;
        };
        self.terminal_panel.input_busy = true;
        self.api(ApiCall::TerminalInput {
            id,
            attachment,
            generation: self.terminal_panel.generation,
            seq: self.terminal_panel.next_input,
            bytes,
        });
    }

    fn terminal_input(&mut self, bytes: Vec<u8>) {
        if self.terminal_controlled && !bytes.is_empty() {
            for chunk in bytes.chunks(16 * 1024) {
                self.terminal_panel.input_queue.push_back(chunk.to_vec());
            }
            self.terminal_send_next();
        }
    }

    pub fn terminal_resize(&mut self) {
        if !self.terminal_open || !self.terminal_controlled || self.terminal_panel.resize_busy {
            return;
        }
        let (Some(r), Some(id), Some(attachment)) = (
            self.regions.terminal,
            self.terminal_panel.selected.clone(),
            self.terminal_panel.attachment.clone(),
        ) else {
            return;
        };
        let width = r.width.min(500);
        let height = crate::terminal_panel::screen_area(
            r,
            !self.terminal_controlled && self.terminal_panel.selected.is_some(),
            self.terminal_panel.screen.gap,
        )
        .height
        .min(200);
        if width == 0 || height == 0 || self.terminal_panel.size == Some((height, width)) {
            return;
        }
        self.terminal_panel.resize_busy = true;
        self.api(ApiCall::TerminalResize {
            id,
            attachment,
            generation: self.terminal_panel.generation,
            rows: height,
            cols: width,
        });
    }

    pub fn terminal_event(&mut self, event: crate::terminal_panel::TerminalEvent) {
        use crate::terminal_panel::TerminalEvent;
        match event {
            TerminalEvent::Attached {
                generation,
                attachment,
            } if generation == self.terminal_panel.generation && self.terminal_open => {
                self.terminal_panel.attachment = Some(attachment);
                if self.terminal_panel.acquire_on_attach {
                    self.execute("terminal.acquire");
                }
            }
            TerminalEvent::Frame { generation, frame }
                if generation == self.terminal_panel.generation && self.terminal_open =>
            {
                if let Err(error) = self.terminal_panel.screen.apply(&frame) {
                    self.toast(Level::Error, format!("Terminal stream: {error}"));
                    self.effects.push(Effect::TerminalStop);
                    self.terminal_panel.attachment = None;
                    self.terminal_controlled = false;
                    self.terminal_panel.input_queue.clear();
                } else if frame["type"] == "exit" {
                    self.terminal_controlled = false;
                    self.terminal_panel.input_queue.clear();
                    if let Some(item) = self
                        .terminal_panel
                        .terminals
                        .iter_mut()
                        .find(|item| Some(&item.id) == self.terminal_panel.selected.as_ref())
                    {
                        item.status = match frame["status"].as_str() {
                            Some("closed") => "closed",
                            Some("ended") => "ended",
                            _ => "exited",
                        }
                        .into();
                        item.controller = None;
                    }
                }
            }
            TerminalEvent::Ended { generation, error }
                if generation == self.terminal_panel.generation && self.terminal_open =>
            {
                let was_attached = self.terminal_panel.attachment.take().is_some();
                self.terminal_controlled = false;
                self.terminal_panel.input_queue.clear();
                if self.terminal_panel.input_busy {
                    self.toast(
                        Level::Warning,
                        "Terminal input outcome may be indeterminate after disconnecting",
                    );
                }
                if was_attached {
                    self.terminal_panel.reconnect_at =
                        Some(Instant::now() + std::time::Duration::from_millis(500));
                }
                self.toast(
                    Level::Error,
                    format!("Terminal disconnected: {error}. Take control again to type."),
                );
            }
            _ => {}
        }
    }
    pub fn new(state: AppState, theme: Theme, size: (u16, u16)) -> Self {
        let manifest = Arc::new(Manifest::load());
        let mut bindings = HashMap::new();
        for c in manifest.command.iter().filter(|c| c.for_tui()) {
            if let Some(ctx) = c.key_context.as_deref() {
                if ctx != "escape_chain" {
                    for k in c.tui_keys() {
                        bindings.insert((ctx.to_owned(), k.to_owned()), c.id.clone());
                    }
                }
            }
        }
        let tab =
            Tab::from_id(&manifest.navigation.left_sidebar.default_tab).unwrap_or(Tab::Sessions);
        let area = Rect::new(0, 0, size.0, size.1);
        let regions = Regions::compute(
            area,
            Flags {
                left_open: true,
                right_open: true,
                ..Flags::default()
            },
            |_| Default::default(),
        );
        let splash =
            crate::splash::Splash::new(Instant::now(), !theme.opts.reduced_motion && !theme.mono());
        App {
            file_watch: None,
            file_watch_client: format!("tui-{}", uuid::Uuid::new_v4()),
            file_watch_status: String::new(),
            manifest,
            theme,
            state,
            terminal_open: false,
            terminal_panel: crate::terminal_panel::TerminalPanel::default(),
            terminal_selected: false,
            terminal_focused: false,
            terminal_controlled: false,
            ui: Ui {
                tabs: Vec::new(),
                detail: crate::ui::Detail::default(),
                draft: Draft::default(),
                first_prompts: HashMap::new(),
                first_pin: None,
                focus: Focus::Composer,
                left_open: true,
                last_project_session: HashMap::new(),
                left_width: 0,
                left_drag: false,
                rail_drag: None,
                right_open: false,
                right_chosen: false,
                sized: false,
                drawer: None,
                tab,
                cursor: HashMap::new(),
                side_top: 0,
                file_open: false,
                file_edit: None,
                file_scroll: 0,
                file_source: false,
                diff_open: None,
                diff_scroll: 0,
                git_cursor: 0,
                file_search: None,
                files_expanded: std::collections::BTreeSet::new(),
                files_hide_ignored: false,
                files_project_root: true,
                composer_drag: None,
                scope_all: false,
                show_archived: false,
                unread_sessions: HashSet::new(),
                failed_sessions: HashSet::new(),
                notify: Notify::default(),
                sound: true,
                view: View::Session,
                pending_selected: None,
                settings_row: 0,
                settings_search: Composer::new(),
                settings_section: "appearance".into(),
                settings_nav_focus: true,
                tool_cursor: 0,
                settings_page: None,
                settings_page_row: 0,
                settings_page_back: 0,
                provider_logins: BTreeMap::new(),
                provider_detail: None,
                provider_detail_row: 0,
                pool_pick: None,
                overlay: None,
                btw: None,
                dialog: None,
                dictation: crate::dictation::DictationUi::default(),
                manager: crate::manager::ManagerUi::default(),
                voice_status: None,
                voice_status_error: None,
                voice_status_generation: 0,
                voice_status_loading: false,
                search_service: None,
                search_service_error: None,
                search_starting: false,
                edit: None,
                alerts: Alerts::default(),
                scroll: Scroll::default(),
                scrolls: HashMap::new(),
                scrollbar: true,
                scroll_mark: None,
                completion: CompletionState::default(),
                drafts: HashMap::new(),
                image_pasting: HashSet::new(),
                learn_source: None,
                wait_idx: 0,
                wait_choice: 0,
                loading_older: false,
                older_failed: None,
                search: None,
                wait_seen: HashMap::new(),
                wait_shown: None,
                asks: HashMap::new(),
                project: None,
                timeline_total: 0,
                timeline_height: 0,
                timeline_open: HashMap::new(),
                live_groups: std::collections::HashSet::new(),
                group_flash: HashMap::new(),
                timeline_sel: None,
                timeline_sel_key: None,
                timeline_blocks: Vec::new(),
                context_cursor: 0,
                launch_dir: None,
                launch_new: false,
                inventories: HashMap::new(),
                draft_asked: None,
                model_prompted: false,
                learn_install_pending: false,
                learn_suppress_until: None,
                last_skills_changed_toast: None,
                last_mcp_changed_toast: None,
                monochrome_before: None,
                prefs: None,
                splash,
                tab_hover: None,
            },
            hits: HitMap::default(),
            regions,
            effects: Vec::new(),
            clock: Instant::now(),
            size,
            frame: 0,
            mouse: None,
            view_sent: None,
            scroll_session: None,
            timeline_screen: None,
            timeline_drag: None,
            layout: std::cell::RefCell::new(crate::timeline::LayoutCache::default()),
            timeline_revision: std::cell::Cell::new(0),
            timeline_cache: std::cell::RefCell::new(None),
            preview_cache: std::cell::RefCell::new(None),
            preview_builds: std::cell::Cell::new(0),
            timeline_builds: std::cell::Cell::new(0),
            image_viewer: std::cell::RefCell::new(crate::images::Viewer::default()),
            annotate_live: std::cell::RefCell::new(crate::annotate::Live::default()),
            bindings,
            shortcut_leader: None,
            last_resources_at: None,
            ctrl_c: None,
        }
    }

    // ---- model queries -------------------------------------------------------

    pub fn writable(&self) -> bool {
        self.state.connection.is_live()
    }

    pub fn narrow(&self) -> bool {
        Regions::is_narrow(self.size.0)
    }

    /// The area the file or diff view occupies: the panel when one is open.
    fn view_rect(&self) -> ratatui::layout::Rect {
        self.regions.panel.unwrap_or(self.regions.timeline)
    }

    pub fn viewed_id(&self) -> Option<&str> {
        self.state
            .model
            .selected
            .as_ref()
            .map(|v| v.session_id.as_str())
            .filter(|id| self.state.model.sessions.contains_key(*id))
    }

    pub fn viewed(&self) -> Option<&Session> {
        self.viewed_id()
            .and_then(|id| self.state.model.sessions.get(id))
    }

    pub fn messages(&self) -> &[Message] {
        self.state
            .model
            .selected
            .as_ref()
            .map_or(&[], |v| v.messages.as_slice())
    }

    pub fn is_busy(&self, id: &str) -> bool {
        self.state.model.running.contains_key(id)
            || self
                .state
                .model
                .sessions
                .get(id)
                .and_then(|s| s.status.as_deref())
                == Some("busy")
    }

    pub fn current_project(&self) -> Option<String> {
        let m = &self.state.model;
        if let Some(p) = self
            .ui
            .project
            .as_ref()
            .filter(|p| m.projects.contains_key(*p))
        {
            return Some(p.clone());
        }
        if let Some(s) = self.viewed() {
            return Some(s.project_id.clone());
        }
        m.projects
            .values()
            .max_by(|a, b| a.updated.total_cmp(&b.updated))
            .map(|p| p.id.clone())
    }

    /// The open panel, using the shared `files`, `file` and `diff` ids. The
    /// left Files tab only counts while its sidebar is visible; Settings is a
    /// centre view, not a panel.
    fn panel_id(&self) -> Option<&'static str> {
        if self.ui.view == View::Settings {
            return None;
        }
        if self.ui.diff_open.is_some() {
            return Some("diff");
        }
        if self.ui.file_open || self.ui.file_edit.is_some() {
            return Some("file");
        }
        if self.ui.tab == Tab::Files && (self.ui.left_open || self.ui.drawer == Some(Side::Left)) {
            return Some("files");
        }
        None
    }

    /// What this client is showing. Ids and a panel label only: never a path,
    /// draft, scroll position or screen contents.
    pub fn client_view(&self) -> ClientView {
        let session = self.viewed_id().map(str::to_owned);
        // A session implies its project. Derive it from the session so a
        // transient project/session mismatch is never sent to Core.
        let project = session
            .as_deref()
            .and_then(|id| self.state.model.sessions.get(id))
            .map(|s| s.project_id.clone())
            .or_else(|| self.current_project());
        ClientView {
            project,
            session,
            panel: self.panel_id().map(str::to_owned),
        }
    }

    /// Tell the supervisor when the view changed. It owns the stream id, keeps
    /// the latest view across reconnects, and reports after `server.connected`.
    pub fn sync_view(&mut self) {
        let view = self.client_view();
        if self.view_sent.as_ref() == Some(&view) {
            return;
        }
        self.view_sent = Some(view.clone());
        self.effects.push(Effect::Sync(SyncCommand::View(view)));
    }

    /// Session switches in their visible order: ordinary tools, then skills,
    /// then MCP servers. The same list drives both context renderers and keys.
    pub fn context_items(&self) -> Vec<crate::paint::CtxItem> {
        let session = self.viewed();
        let disabled = session
            .map(|s| disabled_tools(Some(s)))
            .unwrap_or_else(|| self.ui.draft.disabled_tools.clone());
        let mut items = Vec::new();
        let mut push = |key: String, label: String, on: bool, locked: bool| {
            let index = items.len();
            items.push(crate::paint::CtxItem {
                index,
                key,
                label,
                on,
                locked,
            });
        };

        // Before a session exists the list is Core's own plan for it (`/api/draft/context`),
        // never a client-side guess; until Core answers there is nothing to list.
        let mut tools: Vec<String> = if session.is_some() {
            self.state
                .model
                .selected
                .as_ref()
                .and_then(|view| view.starting.as_ref())
                .and_then(|starting| starting["tools"].as_array())
                // The snapshot loads a moment after the first message; until then the plan
                // Core just answered for the draft is what the session was created from.
                .or_else(|| match &self.state.draft_context {
                    Some((_, Ok(plan))) => plan["tools"].as_array(),
                    _ => None,
                })
                .map(|list| {
                    list.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default()
        } else {
            self.draft_values("tools")
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        };
        if session.is_none() {
            // A draft plan contains only enabled tools. Keep switches visible while
            // a changed selection is being replanned and after a tool is disabled.
            if let (Some((asked, Ok(plan))), Some(mut now)) =
                (&self.state.draft_context, self.draft_request())
            {
                let mut previous = asked.clone();
                previous.as_object_mut().map(|o| o.remove("disabled_tools"));
                now.as_object_mut().map(|o| o.remove("disabled_tools"));
                if previous == now {
                    if let Some(list) = plan["tools"].as_array() {
                        for name in list.iter().filter_map(Value::as_str) {
                            if !tools.iter().any(|tool| tool == name) {
                                tools.push(name.to_owned());
                            }
                        }
                    }
                    if let Some(list) = asked["disabled_tools"].as_array() {
                        for name in list
                            .iter()
                            .filter_map(Value::as_str)
                            .filter(|n| !n.contains(':'))
                        {
                            if !tools.iter().any(|tool| tool == name) {
                                tools.push(name.to_owned());
                            }
                        }
                    }
                }
            }
            for name in disabled.iter().filter(|name| !name.contains(':')) {
                if !tools.contains(name) {
                    tools.push(name.clone());
                }
            }
        }
        for tool in tools {
            push(
                tool.clone(),
                tool.clone(),
                !disabled.iter().any(|key| key == &tool),
                false,
            );
        }

        let skill_values = if let Some(session) = session {
            session
                .extra
                .get("skills")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
        } else {
            self.draft_values("skills")
        };
        for skill in skill_values {
            let Some(name) = skill["name"].as_str() else {
                continue;
            };
            let key = format!("skill:{name}");
            let label = name.to_owned();
            let available = skill["state"].as_str() == Some("on")
                || (session.is_none()
                    && skill["state"].as_str() == Some("off")
                    && skill["locked"].as_bool() != Some(true));
            let on = available && !disabled.iter().any(|entry| entry == &key);
            push(key, label, on, skill["locked"].as_bool().unwrap_or(false));
        }

        let mcp_values: Vec<Value> = if let Some(session) = session {
            session
                .extra
                .get("mcp")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
        } else {
            self.draft_values("mcp")
        };
        if session.is_some() {
            for server in mcp_values {
                if let Some((key, label, on, locked)) =
                    self.mcp_item(Some(&server), None, &disabled)
                {
                    push(key, label, on, locked);
                }
            }
        } else {
            // Core's draft plan is the model context. The project inventory adds
            // the actual project-file rows Core plans away: disabled, failed and
            // invalid entries. They are shown off and locked so the section's
            // `(off)` count never claims they are in the model context.
            let inventory_rows = self.inventory_mcp_rows();
            let mut seen = HashSet::new();
            for server in &mcp_values {
                if let Some(name) = server["server"].as_str() {
                    seen.insert(name.to_owned());
                }
                let inventory_row = server["server"]
                    .as_str()
                    .and_then(|name| Self::inventory_row_for(&inventory_rows, name, Some(server)));
                if let Some((key, label, on, locked)) =
                    self.mcp_item(Some(server), inventory_row, &disabled)
                {
                    push(key, label, on, locked);
                }
            }
            for server in &inventory_rows {
                let Some(name) = server["server"].as_str() else {
                    continue;
                };
                if seen.contains(name) {
                    continue;
                }
                if let Some((key, label, on, locked)) = self.mcp_item(None, Some(server), &disabled)
                {
                    push(key, label, on, locked);
                }
            }
        }
        items
    }

    /// The global inventory plus this project's actual MCP-file rows from
    /// `GET /api/project/{id}/mcp`, looked up by the exact current project id.
    /// Rows include enabled, disabled, failed and invalid entries; this is a
    /// read and never starts a server. A project row wins over a global row of
    /// the same name (a disabled global default must not hide the project's
    /// enabled entry).
    fn inventory_mcp_rows(&self) -> Vec<Value> {
        let Some(project) = self.current_project() else {
            return Vec::new();
        };
        let Some(inventory) = self.ui.inventories.get(&project) else {
            return Vec::new();
        };
        let mut rows: Vec<Value> = inventory
            .mcp
            .iter()
            .filter(|row| {
                Self::is_project_mcp_row(row)
                    && row["project_id"]
                        .as_str()
                        .is_none_or(|id| id == project.as_str())
            })
            .cloned()
            .collect();
        for row in &inventory.mcp {
            if Self::is_project_mcp_row(row)
                || rows.iter().any(|kept| kept["server"] == row["server"])
            {
                continue;
            }
            rows.push(row.clone());
        }
        rows
    }

    fn refresh_project_mcp_inventory(&mut self, project: String) {
        self.ui.draft_asked = None;
        self.effects
            .push(Effect::Sync(SyncCommand::SkillMcp(project)));
    }

    /// Whether an inventory row belongs to a project MCP file rather than
    /// the user's global configuration.
    fn is_project_mcp_row(row: &Value) -> bool {
        row["source"].as_str() == Some("project") || row["project_id"].is_string()
    }

    /// The inventory row matching a draft row's scope. A project draft never
    /// falls back to a same-named global row: that row belongs to another
    /// scope and may be disabled while the project entry is enabled.
    fn inventory_row_for<'a>(
        rows: &'a [Value],
        name: &str,
        draft: Option<&Value>,
    ) -> Option<&'a Value> {
        let project_scope = draft.and_then(|row| row["scope"].as_str()) == Some("project");
        rows.iter().find(|row| {
            row["server"].as_str() == Some(name)
                && (!project_scope || Self::is_project_mcp_row(row))
        })
    }

    /// One row's effective state. A concrete inventory problem (invalid,
    /// disabled or failed) wins over a stale draft plan, so a row never claims
    /// to be in the model context when Core cannot currently run it.
    fn mcp_row_state(draft: Option<&Value>, inventory: Option<&Value>) -> String {
        let draft_state = draft.and_then(|row| row["state"].as_str());
        let inventory_state = inventory.and_then(|row| row["state"].as_str());
        inventory_state
            .filter(|state| matches!(*state, "invalid" | "disabled" | "failed" | "unavailable"))
            .or(draft_state)
            .or(inventory_state)
            .unwrap_or("unknown")
            .to_owned()
    }

    fn mcp_row_tools(draft: Option<&Value>, inventory: Option<&Value>) -> Option<u64> {
        draft
            .and_then(|row| row["tools"].as_u64())
            .or_else(|| inventory.and_then(|row| row["tools"].as_u64()))
    }

    fn append_mcp_tools(label: &mut String, draft: Option<&Value>, inventory: Option<&Value>) {
        match Self::mcp_row_tools(draft, inventory) {
            Some(tools) => label.push_str(&format!(" · {tools} tools")),
            None if draft.is_some() => label.push_str(" · tools unknown"),
            None => {}
        }
    }

    /// One MCP row as a switch. `draft` is Core's plan for the next session
    /// (what the model would receive); `inventory` is the real configuration
    /// row. Inventory-only rows are off and locked, and disabled, failed and
    /// invalid states are named explicitly instead of being called active.
    fn mcp_item(
        &self,
        draft: Option<&Value>,
        inventory: Option<&Value>,
        disabled: &[String],
    ) -> Option<(String, String, bool, bool)> {
        let row = draft.or(inventory)?;
        let name = row["server"].as_str()?;
        let key = format!("mcp:{name}");
        let draft_state = draft.and_then(|row| row["state"].as_str());
        let state = Self::mcp_row_state(draft, inventory);
        let error = inventory
            .and_then(|row| row["error"].as_str())
            .or_else(|| draft.and_then(|row| row["error"].as_str()))
            .filter(|error| !error.is_empty());
        let is_project = inventory.is_some_and(Self::is_project_mcp_row)
            || draft.and_then(|row| row["scope"].as_str()) == Some("project");
        let mut label = name.to_owned();
        match state.as_str() {
            "invalid" => {
                label.push_str(" · invalid");
                if let Some(error) = error {
                    label.push_str(&format!(": {error}"));
                }
            }
            "disabled" => label.push_str(if is_project {
                " · disabled in project MCP"
            } else {
                " · disabled in configuration"
            }),
            "failed" => {
                label.push_str(" · failed");
                if let Some(error) = error {
                    label.push_str(&format!(": {error}"));
                }
            }
            "unavailable" => {
                Self::append_mcp_tools(&mut label, draft, inventory);
                if let Some(error) = error {
                    label.push_str(&format!(" · unavailable: {error}"));
                } else {
                    label.push_str(" · unavailable");
                }
            }
            "idle" if draft.is_none() => label.push_str(if is_project {
                " · starts with the session"
            } else {
                " · available"
            }),
            "connected" => {
                label.push_str(" · connected");
                Self::append_mcp_tools(&mut label, draft, inventory);
            }
            "connecting" => label.push_str(" · connecting"),
            _ => {
                Self::append_mcp_tools(&mut label, draft, inventory);
                if draft.is_none() && !state.is_empty() && state != "on" && state != "off" {
                    label.push_str(&format!(" · {state}"));
                }
            }
        }
        // A concrete problem is never model context, even if a draft row of the
        // same name says on: that row may be another scope, and this entry must
        // not be shown as if it were active.
        let runnable = !matches!(
            state.as_str(),
            "invalid" | "disabled" | "failed" | "unavailable"
        );
        let on =
            runnable && draft_state == Some("on") && !disabled.iter().any(|entry| entry == &key);
        let locked = if !runnable {
            true
        } else if let Some(draft) = draft {
            draft["locked"].as_bool().unwrap_or(false)
        } else {
            // Inventory-only: it was not part of Core's plan for the session.
            true
        };
        Some((key, label, on, locked))
    }

    /// What a context entry is, for its info modal: title and text.
    /// The modal behind a context heading: Markdown for the prompt and the
    /// instruction file; a list of entries for tools, skills and servers.
    fn open_context_section(&mut self, section: &str, focus: Option<usize>, text: Option<String>) {
        if matches!(section, "Tools" | "Skills" | "MCP") {
            let items = self.context_items_for(section);
            let entries: Vec<crate::ui::ContextEntry> =
                items.iter().map(|item| self.context_entry(item)).collect();
            let open = focus.and_then(|index| items.iter().position(|item| item.index == index));
            self.open_overlay(OverlayKind::ContextEntries {
                title: section.to_owned(),
                entries,
                cursor: open.unwrap_or(0),
                open: None,
                top: 0,
            });
            if let Some(i) = open {
                self.context_entry_toggle(i);
            }
            return;
        }
        if let Some(text) = text {
            self.open_overlay(OverlayKind::ContextDoc {
                title: section.to_owned(),
                text,
                top: 0,
            });
        }
    }

    fn context_entry(&self, item: &crate::paint::CtxItem) -> crate::ui::ContextEntry {
        let info = |row: Option<Value>| {
            row.and_then(|row| row["description"].as_str().map(str::to_owned))
                .unwrap_or_default()
        };
        let session = self.viewed();
        if let Some(name) = item.key.strip_prefix("skill:") {
            let values = match session {
                Some(s) => s.extra.get("skills").and_then(Value::as_array).cloned(),
                None => Some(self.draft_values("skills")),
            }
            .unwrap_or_default();
            let row = values
                .into_iter()
                .find(|s| s["name"].as_str() == Some(name));
            return crate::ui::ContextEntry {
                name: name.to_owned(),
                description: info(row),
                full: None,
                markdown: true,
                fetch: Some("skill"),
            };
        }
        if let Some(name) = item.key.strip_prefix("mcp:") {
            let facts = self.context_item_info(item.index).map(|(_, text)| text);
            let description = facts
                .as_deref()
                .map(|text| text.lines().next().unwrap_or("").to_owned())
                .unwrap_or_default();
            return crate::ui::ContextEntry {
                name: name.to_owned(),
                description,
                full: facts,
                markdown: false,
                fetch: Some("mcp"),
            };
        }
        let definition = self
            .state
            .model
            .selected
            .as_ref()
            .and_then(|view| view.starting.as_ref())
            .map(|starting| starting["tool_definitions"][&item.key].clone())
            .filter(|definition| !definition.is_null());
        crate::ui::ContextEntry {
            name: item.key.clone(),
            description: definition
                .as_ref()
                .and_then(|d| d["description"].as_str())
                .map(|text| text.lines().next().unwrap_or("").to_owned())
                .unwrap_or_default(),
            full: Some(definition.map_or_else(
                || "The schema is recorded when the session starts.".to_owned(),
                |d| serde_json::to_string_pretty(&d).unwrap_or_default(),
            )),
            markdown: false,
            fetch: None,
        }
    }

    /// Open or close an entry; the first open asks Core for what it does not have.
    fn context_entry_toggle(&mut self, index: usize) {
        let project = self.current_project();
        let Some(Overlay {
            kind:
                OverlayKind::ContextEntries {
                    entries,
                    cursor,
                    open,
                    top,
                    ..
                },
            ..
        }) = self.ui.overlay.as_mut()
        else {
            return;
        };
        let Some(entry) = entries.get_mut(index) else {
            return;
        };
        *cursor = index;
        *top = 0;
        *open = (*open != Some(index)).then_some(index);
        let call = open
            .and(entry.fetch.take())
            .map(|kind| ApiCall::ContextEntry {
                kind,
                name: entry.name.clone(),
                project,
            });
        if let Some(call) = call {
            self.api(call);
        }
    }

    pub fn context_item_info(&self, index: usize) -> Option<(String, String)> {
        let item = self.context_items().into_iter().nth(index)?;
        let session = self.viewed();
        if let Some(name) = item.key.strip_prefix("skill:") {
            let values = match session {
                Some(s) => s.extra.get("skills").and_then(Value::as_array).cloned(),
                None => Some(self.draft_values("skills")),
            };
            let skill = values?
                .into_iter()
                .find(|s| s["name"].as_str() == Some(name))?;
            let mut text = skill["description"]
                .as_str()
                .filter(|d| !d.is_empty())
                .unwrap_or("No description.")
                .to_owned();
            for (label, key) in [("Scope", "scope"), ("Path", "path")] {
                if let Some(v) = skill[key].as_str().filter(|v| !v.is_empty()) {
                    text.push_str(&format!("\n\n{label}: {v}"));
                }
            }
            return Some((format!("Skill · {name}"), text));
        }
        if let Some(name) = item.key.strip_prefix("mcp:") {
            let values = match session {
                Some(s) => s.extra.get("mcp").and_then(Value::as_array).cloned(),
                None => Some(self.draft_values("mcp")),
            }
            .unwrap_or_default();
            let draft_row = values.iter().find(|s| s["server"].as_str() == Some(name));
            let inventory_rows = self.inventory_mcp_rows();
            let inventory_row = if session.is_none() {
                Self::inventory_row_for(&inventory_rows, name, draft_row).cloned()
            } else {
                None
            };
            let row = inventory_row.as_ref().or(draft_row)?;
            let state = Self::mcp_row_state(draft_row, inventory_row.as_ref());
            let is_project = inventory_row.as_ref().is_some_and(Self::is_project_mcp_row)
                || draft_row.and_then(|s| s["scope"].as_str()) == Some("project");
            let error = inventory_row
                .as_ref()
                .and_then(|s| s["error"].as_str())
                .or_else(|| draft_row.and_then(|s| s["error"].as_str()))
                .filter(|e| !e.is_empty());
            let file = inventory_row
                .as_ref()
                .and_then(|s| s["file"].as_str())
                .or_else(|| draft_row.and_then(|s| s["file"].as_str()))
                .filter(|f| !f.is_empty());
            let warnings: Vec<&str> = inventory_row
                .as_ref()
                .and_then(|s| s["warnings"].as_array())
                .or_else(|| draft_row.and_then(|s| s["warnings"].as_array()))
                .map(|list| list.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            let mut lines = vec![match state.as_str() {
                "on" if session.is_none() => {
                    "State: on (in the model context of the next session)".to_owned()
                }
                "on" => "State: on (in the model context)".to_owned(),
                "off" if session.is_none() => "State: switched off for the next session".to_owned(),
                "off" => "State: switched off for this session".to_owned(),
                "unavailable" => "State: unavailable".to_owned(),
                "idle" if is_project && session.is_none() => {
                    "State: idle; enabled entries start automatically with a new session".to_owned()
                }
                "idle" if session.is_none() => {
                    "State: idle; enabled servers start with a new session".to_owned()
                }
                "idle" => "State: idle (not connected)".to_owned(),
                "connected" => "State: connected".to_owned(),
                "connecting" => "State: connecting".to_owned(),
                "failed" => "State: failed".to_owned(),
                "disabled" => "State: disabled; it is never started".to_owned(),
                "invalid" => "State: invalid; it is never started".to_owned(),
                other => format!("State: {other}"),
            }];
            if let Some(e) = error {
                lines.push(format!("Error: {e}"));
            }
            if !warnings.is_empty() {
                lines.push(format!("Warnings: {}", warnings.join("; ")));
            }
            lines.push(
                if matches!(
                    state.as_str(),
                    "disabled" | "invalid" | "failed" | "unavailable"
                ) {
                    "Tools: none (not started)".to_owned()
                } else {
                    match Self::mcp_row_tools(draft_row, inventory_row.as_ref()) {
                        Some(n) => format!("Tools: {n}"),
                        None => "Tools: unknown until it connects".to_owned(),
                    }
                },
            );
            if is_project {
                lines.push("Scope: project".to_owned());
                if let Some(file) = file {
                    lines.push(format!("File: {file}"));
                }
            } else {
                lines.push(format!(
                    "Scope: {}",
                    row["scope"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or("global")
                ));
            }
            // The non-secret configuration facts Core returns for both scopes.
            if let Some(transport) = row["transport"].as_str().filter(|t| !t.is_empty()) {
                lines.push(format!("Transport: {transport}"));
            }
            if let Some(command) = row["command"].as_str().filter(|c| !c.is_empty()) {
                lines.push(format!("Command: {command}"));
            }
            if let Some(count) = row["arg_count"].as_u64() {
                lines.push(format!("Arguments: {count} (values not shown)"));
            }
            if let Some(cwd) = row["cwd"].as_str().filter(|c| !c.is_empty()) {
                lines.push(format!("Working directory: {cwd}"));
            }
            let names: Vec<&str> = row["env_names"]
                .as_array()
                .map(|list| list.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            if !names.is_empty() {
                lines.push(format!("Environment: {}", names.join(", ")));
            }
            if let Some(url) = row["url"].as_str().filter(|u| !u.is_empty()) {
                lines.push(format!("URL: {url}"));
            }
            let names: Vec<&str> = row["header_names"]
                .as_array()
                .map(|list| list.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            if !names.is_empty() {
                lines.push(format!("Headers: {}", names.join(", ")));
            }
            let include: Vec<&str> = row["include"]
                .as_array()
                .map(|list| list.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            if !include.is_empty() {
                lines.push(format!("Include tools: {}", include.join(", ")));
            }
            let exclude: Vec<&str> = row["exclude"]
                .as_array()
                .map(|list| list.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            if !exclude.is_empty() {
                lines.push(format!("Exclude tools: {}", exclude.join(", ")));
            }
            if let (Some(connect), Some(call)) = (
                row["connect_timeout"].as_u64(),
                row["call_timeout"].as_u64(),
            ) {
                lines.push(format!("Timeouts: connect {connect}s, call {call}s"));
            }
            if session.is_none() {
                lines.push(if is_project {
                    "Startup: enabled project entries start automatically when a session is created; this preview never starts one."
                        .to_owned()
                } else {
                    "Startup: enabled global servers start with a new session; this preview never starts one."
                        .to_owned()
                });
            }
            let where_file = file.map(|f| format!(" in {f}")).unwrap_or_default();
            let target = if session.is_none() {
                "the next session"
            } else {
                "this session"
            };
            match state.as_str() {
                "disabled" if is_project => lines.push(format!(
                    "Action: set \"enabled\": true for this entry{where_file}, then start a new session. Disabled entries are never started."
                )),
                "disabled" => lines.push(
                    "Action: enable this server in Settings → MCP, then start a new session. Disabled entries are never started."
                        .to_owned(),
                ),
                "invalid" if name == "(file)" => lines.push(format!(
                    "Action: fix {}{} so it parses; an invalid file starts no project server.",
                    file.unwrap_or("the project MCP file"),
                    if file.is_some() { "" } else { " in this project" },
                )),
                "invalid" => lines.push(format!(
                    "Action: fix or rename this entry{where_file}; invalid entries are never started."
                )),
                "failed" | "unavailable" => lines.push(
                    "Action: check the error above, then start a new session to retry.".to_owned(),
                ),
                "idle" if session.is_none() && is_project => lines.push(
                    "Action: no action needed; this enabled entry starts with a new session."
                        .to_owned(),
                ),
                "idle" if session.is_none() => lines.push(
                    "Action: no action needed; this enabled server starts with a new session."
                        .to_owned(),
                ),
                "idle" => lines.push(
                    "Action: start a new session to start and use it.".to_owned(),
                ),
                "on" => lines.push(format!(
                    "Action: press Enter or click this row to switch it off for {target}."
                )),
                "off" => lines.push(format!(
                    "Action: press Enter or click this row to switch it on for {target}."
                )),
                "connected" if draft_row.is_none() => lines.push(
                    "Action: connected now; start a new session to add it to the model context."
                        .to_owned(),
                ),
                _ => {}
            }
            return Some((format!("MCP · {name}"), lines.join("\n")));
        }
        let from_session = self
            .state
            .model
            .selected
            .as_ref()
            .and_then(|v| v.starting.as_ref())
            .map(|st| st["tool_definitions"][item.key.as_str()].clone())
            .filter(|d| !d.is_null());
        let def = from_session.or_else(|| {
            self.draft_context()
                .map(|d| d["tool_info"][item.key.as_str()].clone())
                .filter(|d| !d.is_null())
        });
        let text = def
            .as_ref()
            .and_then(|d| d["description"].as_str())
            .unwrap_or("No description loaded yet.")
            .to_owned();
        Some((format!("Tool · {}", item.key), text))
    }

    pub fn context_items_for(&self, section: &str) -> Vec<crate::paint::CtxItem> {
        self.context_items()
            .into_iter()
            .filter(|item| match section {
                "Tools" => !item.key.contains(':'),
                "Skills" => item.key.starts_with("skill:"),
                "MCP" => item.key.starts_with("mcp:"),
                _ => false,
            })
            .collect()
    }

    pub fn context_inventory_meta(&self, section: &str) -> String {
        if self.viewed().is_none() && self.draft_context().is_none() {
            let Some(project) = self.current_project() else {
                return "no project".into();
            };
            let Some(inventory) = self.ui.inventories.get(&project) else {
                return "loading".into();
            };
            if section == "Skills" {
                if let Some(error) = inventory.skills_error.as_ref() {
                    return format!("unavailable: {error}");
                }
            } else {
                if let Some(error) = inventory.project_mcp_error.as_ref() {
                    return format!("project inventory unavailable: {error}");
                }
                if let Some(error) = inventory.mcp_error.as_ref() {
                    return format!("unavailable: {error}");
                }
                if inventory.project_mcp_duplicates > 0 {
                    let count = inventory.project_mcp_duplicates;
                    return format!(
                        "{count} duplicate name{}; .mcp.json wins",
                        if count == 1 { "" } else { "s" }
                    );
                }
            }
        }
        if self.context_items_for(section).is_empty() {
            "none".into()
        } else {
            String::new()
        }
    }

    pub fn ancestors_of<'a>(&'a self, id: &'a str) -> Vec<&'a Session> {
        let mut chain = Vec::new();
        let mut cur = self.state.model.sessions.get(id);
        while let Some(s) = cur {
            chain.push(s);
            if chain.len() > 16 {
                break;
            }
            cur = s
                .parent_id
                .as_deref()
                .and_then(|p| self.state.model.sessions.get(p));
        }
        chain.reverse();
        chain
    }

    fn is_descendant(&self, id: &str, ancestor: &str) -> bool {
        self.ancestors_of(id).iter().any(|s| s.id == ancestor)
    }

    pub fn descendants(&self, id: &str) -> usize {
        self.state
            .model
            .sessions
            .keys()
            .filter(|s| s.as_str() != id && self.is_descendant(s, id))
            .count()
    }

    /// Copy one value from the open worktree modal through OSC 52.
    fn copy_worktree_value(&mut self, which: WorktreeCopy) {
        let Some(Overlay {
            kind: OverlayKind::WorktreeInfo(info),
            ..
        }) = &self.ui.overlay
        else {
            return;
        };
        let value = match which {
            WorktreeCopy::Path => &info.path,
            WorktreeCopy::Branch => &info.branch,
            WorktreeCopy::Main => &info.main,
        };
        if value.is_empty() {
            return;
        }
        self.effects.push(Effect::Copy(value.clone()));
        self.toast(Level::Success, "Copied");
    }

    /// The viewed session's worktree, from the session listing or the
    /// project-wide one. ``None`` outside Git (or before a listing arrives).
    pub fn worktree_info(&self) -> Option<WorktreeInfo> {
        let session = self.viewed()?;
        let directory = session.directory.as_str();
        let listing = self
            .state
            .worktrees
            .get(&session.id)
            .or_else(|| self.state.project_worktrees.get(&session.project_id))?;
        // A per-session listing can be stale after the session moved; the
        // project fallback has no session_directory. Matching the most specific
        // worktree path is enough, and it keeps a linked sibling checkout
        // outside the project directory visible.
        if listing["session_directory"]
            .as_str()
            .is_some_and(|root| root != directory)
        {
            return None;
        }
        let trees = listing["worktrees"].as_array()?;
        let tree = trees
            .iter()
            .filter(|tree| {
                tree["path"]
                    .as_str()
                    .is_some_and(|root| std::path::Path::new(directory).starts_with(root))
            })
            .max_by_key(|tree| tree["path"].as_str().map_or(0, str::len))?;
        let path = tree["path"].as_str()?.to_owned();
        let main = listing["main"]
            .as_str()
            .or_else(|| {
                trees
                    .iter()
                    .find(|tree| tree["main"].as_bool() == Some(true))
                    .and_then(|tree| tree["path"].as_str())
            })
            .unwrap_or("")
            .to_owned();
        Some(WorktreeInfo {
            path,
            branch: tree["branch"].as_str().unwrap_or("").to_owned(),
            main,
            linked: tree["main"].as_bool() == Some(false),
            top: 0,
        })
    }

    /// Approvals and questions waiting in the viewed session or its children.
    pub fn wait_items(&self) -> Vec<WaitItem> {
        let Some(vid) = self.viewed_id() else {
            return Vec::new();
        };
        let m = &self.state.model;
        let child = |sid: &str| {
            (sid != vid).then(|| {
                m.sessions
                    .get(sid)
                    .map(|s| s.title.clone())
                    .unwrap_or_default()
            })
        };
        let mut v = Vec::new();
        for a in m
            .approvals
            .values()
            .filter(|a| self.is_descendant(&a.session_id, vid))
        {
            v.push(WaitItem {
                kind: WaitKind::Approval,
                id: a.id.clone(),
                session_id: a.session_id.clone(),
                child: child(&a.session_id),
            });
        }
        for q in m
            .questions
            .values()
            .filter(|q| self.is_descendant(&q.session_id, vid))
        {
            v.push(WaitItem {
                kind: WaitKind::Question,
                id: q.id.clone(),
                session_id: q.session_id.clone(),
                child: child(&q.session_id),
            });
        }
        v
    }

    pub fn wait_focused(&self) -> Option<WaitItem> {
        let items = self.wait_items();
        items
            .get(self.ui.wait_idx.min(items.len().saturating_sub(1)))
            .cloned()
    }

    /// The waiting card in view is a question. Its keys own the composer, which
    /// is disabled until the question is answered or cancelled. An approval
    /// stays approval-only even when a question waits behind it.
    pub fn question_pending(&self) -> bool {
        self.wait_focused()
            .is_some_and(|w| w.kind == WaitKind::Question)
    }

    /// The focused wait's current page, when it is a question.
    pub fn ask_current(&self) -> Option<AskCurrent> {
        let wait = self
            .wait_focused()
            .filter(|w| w.kind == WaitKind::Question)?;
        let q = self.state.model.questions.get(&wait.id)?;
        let total = q.questions.len();
        let draft = self.ui.asks.get(&wait.id).cloned().unwrap_or_default();
        let (mut picks, page, sent) = (draft.picks, draft.page, draft.sent);
        picks.resize(total, AskPick::default());
        Some(AskCurrent {
            wait,
            page: page.min(total),
            total,
            questions: q.questions.clone(),
            picks,
            sent,
        })
    }

    /// Start the draft for a question the first time it is shown, focusing the
    /// answer field when the question asks for a string (a free-text option or
    /// a question with no options).
    fn start_question(&mut self, id: &str) {
        let Some(question) = self.state.model.questions.get(id) else {
            return;
        };
        let total = question.questions.len();
        let questions = question.questions.clone();
        let draft = self.ui.asks.entry(id.to_owned()).or_default();
        let fresh = draft.picks.is_empty();
        if draft.picks.len() != total {
            draft.picks.resize(total, AskPick::default());
        }
        if fresh {
            if let Some(pick) = draft.picks.first_mut() {
                if questions.first().is_some_and(|q| {
                    q["options"].as_array().is_none_or(Vec::is_empty)
                        || q["allow_free_text"].as_bool() == Some(true)
                }) {
                    pick.field = true;
                }
            }
        }
    }

    /// Prepare the drafts of every pending question in the viewed session's tree.
    fn prepare_questions(&mut self) {
        let ids: Vec<String> = self
            .wait_items()
            .into_iter()
            .filter(|w| w.kind == WaitKind::Question)
            .map(|w| w.id)
            .collect();
        for id in ids {
            self.start_question(&id);
        }
    }

    /// A question arrived and is the card in view: give it the keys so text
    /// goes straight into its answer field.
    fn focus_question_card(&mut self) {
        if self.ui.overlay.is_some() || self.ui.dialog.is_some() || self.ui.edit.is_some() {
            return;
        }
        if self.ask_current().is_some() {
            self.ui.focus = Focus::Waiting;
        }
    }

    /// A question was answered or cancelled: forget its draft and give the
    /// composer back when nothing else is waiting.
    fn question_closed(&mut self, id: &str) {
        self.ui.asks.remove(id);
        if self.ui.focus == Focus::Waiting
            && self.wait_focused().is_none()
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
        {
            self.ui.focus = Focus::Composer;
        }
    }

    /// The draft of request `id`, started blank on first use.
    fn ask_picks(&mut self, id: &str, total: usize) -> &mut AskDraft {
        let draft = self.ui.asks.entry(id.to_owned()).or_default();
        if draft.picks.len() != total {
            draft.picks.resize(total, AskPick::default());
        }
        if draft.page > total {
            draft.page = total;
        }
        draft
    }

    /// Tick (multi-select) or choose option `i` of the current question. Enter
    /// locks the answer and moves on; choosing alone never sends.
    pub fn ask_pick(&mut self, i: usize) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.sent || cur.review() || i >= cur.options().len() {
            return;
        }
        let multiple = cur.multiple();
        let d = self.ask_picks(&cur.wait.id, cur.total);
        let Some(pick) = d.picks.get_mut(cur.page) else {
            return;
        };
        pick.cursor = i;
        if multiple {
            match pick.chosen.iter().position(|c| *c == i) {
                Some(at) => {
                    pick.chosen.remove(at);
                }
                None => pick.chosen.push(i),
            }
        } else {
            pick.chosen = vec![i];
        }
    }

    fn ask_move(&mut self, down: bool) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.sent || cur.review() {
            return;
        }
        let last = cur.options().len().saturating_sub(1);
        let cursor = cur.pick().map_or(0, |p| p.cursor);
        let d = self.ask_picks(&cur.wait.id, cur.total);
        if let Some(pick) = d.picks.get_mut(cur.page) {
            pick.cursor = if down {
                (cursor + 1).min(last)
            } else {
                cursor.saturating_sub(1)
            };
        }
    }

    /// Focus the custom answer field (or the option list) on this page.
    fn ask_field(&mut self, on: bool) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.sent || cur.review() || cur.options().is_empty() {
            return;
        }
        let d = self.ask_picks(&cur.wait.id, cur.total);
        if let Some(pick) = d.picks.get_mut(cur.page) {
            pick.field = on;
        }
    }

    /// Edit the page's custom answer. The field takes focus on the first edit.
    fn ask_input_key(&mut self, ev: &KeyEvent, name: &str) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.sent || cur.review() {
            return;
        }
        let d = self.ask_picks(&cur.wait.id, cur.total);
        let Some(pick) = d.picks.get_mut(cur.page) else {
            return;
        };
        pick.field = true;
        if name == "ctrl+j" {
            pick.input.newline();
        } else {
            edit_composer(&mut pick.input, ev, name);
        }
    }

    /// Paste into the question card's custom answer field.
    fn ask_input_paste(&mut self, text: &str) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.sent || cur.review() {
            return;
        }
        let d = self.ask_picks(&cur.wait.id, cur.total);
        if let Some(pick) = d.picks.get_mut(cur.page) {
            pick.field = true;
            pick.input.paste(text);
        }
    }

    /// Move to the previous or next page: a question, or the review at the end.
    fn ask_page(&mut self, forward: bool) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.sent {
            return;
        }
        let target = if forward {
            (cur.page + 1).min(cur.total)
        } else {
            cur.page.saturating_sub(1)
        };
        if target == cur.page {
            return;
        }
        let draft = self.ask_picks(&cur.wait.id, cur.total);
        draft.page = target;
        draft.scroll = 0;
    }

    /// Enter on a question locks its answer and moves to the next page (the
    /// review after the last one); Enter on the review sends the reply.
    pub fn ask_confirm(&mut self) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if !self.writable() {
            return self.toast(Level::Warning, READ_ONLY);
        }
        if cur.sent {
            return;
        }
        if cur.review() {
            return self.ask_submit();
        }
        let opts = cur.options().to_vec();
        let mut pick = cur.pick().cloned().unwrap_or_default();
        let use_text = !pick.input.expanded_text().trim().is_empty();
        if !cur.multiple() && pick.chosen.is_empty() && !opts.is_empty() && !use_text {
            pick.chosen = vec![pick.cursor.min(opts.len() - 1)];
        }
        let Some(q) = cur.question() else {
            return;
        };
        if let Some(problem) = ask_pick_problem(q, &pick) {
            return self.toast(Level::Info, problem);
        }
        let d = self.ask_picks(&cur.wait.id, cur.total);
        if let Some(slot) = d.picks.get_mut(cur.page) {
            *slot = pick;
        }
        if cur.total == 1 {
            // One question answers in one step: no review page.
            return self.ask_submit();
        }
        d.page = cur.page + 1;
        d.scroll = 0;
        if let Some(next) = cur.questions.get(d.page) {
            if next["options"].as_array().is_none_or(Vec::is_empty)
                || next["allow_free_text"].as_bool() == Some(true)
            {
                if let Some(pick) = d.picks.get_mut(d.page) {
                    pick.field = true;
                }
            }
        }
    }

    /// Send every answer. A page that is not ready is shown instead of the POST
    /// being rejected by Core.
    pub fn ask_submit(&mut self) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if !self.writable() {
            return self.toast(Level::Warning, READ_ONLY);
        }
        if cur.sent {
            return;
        }
        let picks = cur.picks.clone();
        let problem = cur.questions.iter().enumerate().find_map(|(i, q)| {
            picks
                .get(i)
                .and_then(|p| ask_pick_problem(q, p))
                .map(|why| (i, why))
        });
        if let Some((page, why)) = problem {
            let d = self.ask_picks(&cur.wait.id, cur.total);
            d.picks = picks;
            d.page = page;
            return self.toast(Level::Info, why);
        }
        let answers = ask_answers(&cur.questions, &picks);
        let d = self.ask_picks(&cur.wait.id, cur.total);
        d.picks = picks;
        // A single question stays on its page while the reply is in flight;
        // it never shows a review.
        d.page = if cur.total == 1 { cur.page } else { cur.total };
        d.sent = true;
        self.api(ApiCall::Answer {
            session: cur.wait.session_id.clone(),
            id: cur.wait.id.clone(),
            answers,
        });
    }

    fn ask_scroll(&mut self, down: bool) {
        if let Some(cur) = self.ask_current() {
            let draft = self.ask_picks(&cur.wait.id, cur.total);
            draft.scroll = if down {
                (draft.scroll + 1).min(draft.scroll_max)
            } else {
                draft.scroll.saturating_sub(1)
            };
        }
    }

    fn ask_key(&mut self, ev: &KeyEvent, name: &str) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.sent {
            return;
        }
        let n_opts = cur.options().len();
        let field = cur.field();
        match name {
            "pageup" | "pagedown" => self.ask_scroll(name == "pagedown"),
            "alt+a" => self.ask_field(!field),
            "up" | "down" if n_opts == 0 || field => self.ask_input_key(ev, name),
            "up" => self.ask_move(false),
            "down" => self.ask_move(true),
            "alt+left" => self.ask_page(false),
            "alt+right" => self.ask_page(true),
            "left" | "right" => {
                let has_text = cur
                    .pick()
                    .is_some_and(|p| !p.input.expanded_text().trim().is_empty());
                if field || has_text {
                    self.ask_input_key(ev, name);
                } else {
                    self.ask_page(name == "right");
                }
            }
            "enter" => self.ask_confirm(),
            "space" if n_opts > 0 && !field => {
                let cursor = cur.pick().map_or(0, |p| p.cursor);
                self.ask_pick(cursor);
            }
            _ => {
                let digit = name
                    .parse::<usize>()
                    .ok()
                    .filter(|_| n_opts > 0 && !field && name.len() == 1);
                if let Some(d) = digit {
                    // 1..9 are options 1..9, 0 is the tenth
                    self.ask_pick((d + 9) % 10);
                } else if keys::printable(ev).is_some() || edit_key(name) {
                    self.ask_input_key(ev, name);
                }
            }
        }
    }

    /// Waits anywhere else (other sessions).
    pub fn waiting_elsewhere(&self) -> Vec<String> {
        let m = &self.state.model;
        let here: Vec<String> = self.wait_items().iter().map(|w| w.id.clone()).collect();
        let mut v: Vec<String> = m
            .approvals
            .values()
            .map(|a| (a.id.clone(), a.session_id.clone()))
            .chain(
                m.questions
                    .values()
                    .map(|q| (q.id.clone(), q.session_id.clone())),
            )
            .filter(|(id, _)| !here.contains(id))
            .map(|(_, s)| s)
            .collect();
        v.sort();
        v.dedup();
        v
    }

    /// The terminal notification an event earns under the Settings choice: a
    /// session waiting for approval or a question (unless off), and a finished
    /// turn or a failed session when the choice is all.
    fn notice(&self, root: &str, kind: &str) -> Option<String> {
        let what = match kind {
            "permission.asked" if self.ui.notify != Notify::Off => "needs approval",
            "question.asked" if self.ui.notify != Notify::Off => "has a question",
            "turn_end" if self.ui.notify == Notify::All => "finished",
            "session.error" if self.ui.notify == Notify::All => "failed",
            _ => return None,
        };
        let title = self
            .state
            .model
            .sessions
            .get(root)
            .map(|s| s.title.trim())
            .filter(|t| !t.is_empty())
            .unwrap_or("Session");
        Some(format!("Nexus: {title} {what}"))
    }

    /// The attention mark of a session row, by precedence: `waiting`, `error`,
    /// `running`, `unread`.
    pub fn session_mark(&self, id: &str) -> Option<&'static str> {
        if self.session_waiting(id) {
            Some("waiting")
        } else if self.ui.failed_sessions.contains(id) {
            Some("error")
        } else if self.is_busy(id) {
            Some("running")
        } else if self.ui.unread_sessions.contains(id) {
            Some("unread")
        } else {
            None
        }
    }

    /// What the `/sessions` modal and Session panel show for a session:
    /// `needs input`, `failed`, `running`, `finished`, `archived` or `idle`.
    pub fn session_state(&self, id: &str) -> &'static str {
        if self.session_waiting(id) {
            return "needs input";
        }
        if self.ui.failed_sessions.contains(id) {
            return "failed";
        }
        if self.is_busy(id) {
            return "running";
        }
        if self
            .state
            .model
            .sessions
            .get(id)
            .is_some_and(|s| s.archived)
        {
            return "archived";
        }
        if self.ui.unread_sessions.contains(id) {
            return "finished";
        }
        "idle"
    }

    pub fn session_waiting(&self, id: &str) -> bool {
        let m = &self.state.model;
        m.approvals
            .values()
            .any(|a| self.is_descendant(&a.session_id, id))
            || m.questions
                .values()
                .any(|q| self.is_descendant(&q.session_id, id))
    }

    fn last_turn_interrupted(&self) -> bool {
        let Some(v) = self.state.model.selected.as_ref() else {
            return false;
        };
        // The running turn's live events come after its newest summary.
        for e in v.scratch.iter().rev() {
            match e.kind.as_str() {
                "turn_end" => return false,
                "turn.recovered" => return true,
                "session.error" => return true,
                "session.status" if e.data["status"] == "busy" => return false,
                _ => {}
            }
        }
        if let Some(t) = v.turns.last() {
            return match t.status.as_str() {
                "completed" => false,
                "interrupted" | "failed" => true,
                // A running turn of an idle session was cut short (recovery pending).
                _ => !self.is_busy(&v.session_id),
            };
        }
        // No turn records: a conversation that ends on a user or tool message
        // was cut short; Core refuses with 409/400 otherwise.
        v.messages.last().is_some_and(|m| m.role != "assistant")
    }

    fn root_session(&self, id: &str) -> Option<String> {
        let mut current = self.state.model.sessions.get(id)?;
        for _ in 0..self.state.model.sessions.len() {
            match current.parent_id.as_deref() {
                Some(parent) => current = self.state.model.sessions.get(parent)?,
                None => return Some(current.id.clone()),
            }
        }
        None
    }

    fn tree_busy(&self, root: &str) -> bool {
        self.state
            .model
            .sessions
            .keys()
            .any(|id| self.is_busy(id) && self.root_session(id).as_deref() == Some(root))
    }

    pub fn session_worktree<'a>(&'a self, session: &Session) -> Option<&'a Value> {
        let listing = self
            .state
            .worktrees
            .get(&session.id)
            .filter(|listing| {
                listing["session_directory"].as_str() == Some(session.directory.as_str())
            })
            .or_else(|| self.state.project_worktrees.get(&session.project_id));
        listing
            .and_then(|listing| listing["worktrees"].as_array())
            .and_then(|trees| {
                trees
                    .iter()
                    .filter(|tree| {
                        tree["path"].as_str().is_some_and(|root| {
                            std::path::Path::new(&session.directory).starts_with(root)
                        })
                    })
                    .max_by_key(|tree| tree["path"].as_str().map_or(0, str::len))
            })
    }

    /// Session ids of the viewed session's tree (its root and every descendant).
    pub fn resource_tree(&self) -> HashSet<String> {
        let Some(id) = self.viewed_id() else {
            return HashSet::new();
        };
        let Some(root) = self.root_session(id) else {
            return HashSet::new();
        };
        self.state
            .model
            .sessions
            .keys()
            .filter(|sid| self.root_session(sid).as_deref() == Some(root.as_str()))
            .cloned()
            .collect()
    }

    /// Live kernels created by or used in the viewed session tree.
    pub fn view_kernels(&self) -> Vec<&Value> {
        let tree = self.resource_tree();
        self.state
            .kernels
            .iter()
            .filter(|kernel| {
                kernel["created_by"]
                    .as_str()
                    .is_some_and(|id| tree.contains(id))
                    || kernel["users"].as_array().is_some_and(|users| {
                        users
                            .iter()
                            .filter_map(Value::as_str)
                            .any(|id| tree.contains(id))
                    })
            })
            .collect()
    }

    /// Background bash jobs started by the viewed session tree.
    pub fn view_jobs(&self) -> Vec<&Value> {
        let tree = self.resource_tree();
        self.state
            .jobs
            .iter()
            .filter(|job| {
                job["root"].as_str().is_some_and(|id| tree.contains(id))
                    || job["session_id"]
                        .as_str()
                        .is_some_and(|id| tree.contains(id))
            })
            .collect()
    }

    /// `(live kernels, running background shells)` for the location strip. The
    /// shells are the agent's bash jobs; user terminals are counted separately
    /// by `terminal_shell_count`.
    pub fn resource_counts(&self) -> (usize, usize) {
        (
            self.view_kernels().len(),
            self.view_jobs()
                .iter()
                .filter(|job| job["status"].as_str() == Some("running"))
                .count(),
        )
    }

    /// User terminals in the dock.
    /// Stop the background shell selected in the Shells modal, if it is still running.
    fn stop_selected_shell(&mut self) {
        let Some(Overlay {
            kind: OverlayKind::Resources { kind, selected, .. },
            ..
        }) = &self.ui.overlay
        else {
            return;
        };
        if *kind == crate::ui::ResourceKind::Kernels {
            return;
        }
        let selected = *selected;
        let id = self
            .view_jobs()
            .get(selected)
            .filter(|job| job["status"].as_str() == Some("running"))
            .and_then(|job| job["id"].as_str())
            .map(str::to_owned);
        if let Some(id) = id {
            self.api(ApiCall::StopJob { id });
        }
    }

    pub fn terminal_shell_count(&self) -> usize {
        self.terminal_panel.terminals.len()
    }

    pub fn side_entries(&self) -> Vec<SideEntry> {
        let m = &self.state.model;
        let cur = self.current_project();
        let mut sessions: Vec<&Session> = m
            .sessions_recent()
            .into_iter()
            .filter(|s| s.parent_id.is_none())
            .filter(|s| self.ui.show_archived || !s.archived)
            .filter(|s| cur.as_deref() == Some(s.project_id.as_str()))
            .collect();
        let project_order = self.project_ids();
        let root = |s: &Session| {
            self.session_worktree(s)
                .and_then(|tree| tree["path"].as_str())
                .map(str::to_owned)
                .or_else(|| m.projects.get(&s.project_id).map(|p| p.path.clone()))
                .unwrap_or_else(|| s.directory.clone())
        };
        // Per project: the main checkout first, then linked worktrees by path.
        sessions.sort_by_key(|s| {
            let linked = self
                .session_worktree(s)
                .is_some_and(|tree| tree["main"].as_bool() == Some(false));
            (
                project_order
                    .iter()
                    .position(|id| id == &s.project_id)
                    .unwrap_or(usize::MAX),
                linked,
                root(s),
            )
        });
        let mut roots: HashMap<String, HashSet<String>> = HashMap::new();
        for session in &sessions {
            roots
                .entry(session.project_id.clone())
                .or_default()
                .insert(root(session));
        }
        let mut last: Option<(String, String)> = None;
        sessions
            .into_iter()
            .map(|s| {
                let worktree = root(s);
                let group = (s.project_id.clone(), worktree.clone());
                let multiple = roots
                    .get(&s.project_id)
                    .is_some_and(|items| items.len() > 1);
                let new_root = last.as_ref() != Some(&group);
                let linked = self
                    .session_worktree(s)
                    .is_some_and(|tree| tree["main"].as_bool() == Some(false));
                let tree = (new_root && multiple && linked).then(|| {
                    let label = self
                        .session_worktree(s)
                        .and_then(|tree| tree["branch"].as_str())
                        .filter(|name| !name.is_empty())
                        .or_else(|| {
                            std::path::Path::new(&worktree)
                                .file_name()
                                .and_then(|name| name.to_str())
                        })
                        .unwrap_or("worktree");
                    (label.to_owned(), worktree.clone())
                });
                last = Some(group);
                SideEntry {
                    id: s.id.clone(),
                    project: None,
                    tree,
                }
            })
            .collect()
    }

    pub fn project_ids(&self) -> Vec<String> {
        self.ordered_projects()
            .into_iter()
            .map(|p| p.id.clone())
            .collect()
    }

    pub fn rail_project_ids(&self) -> Vec<String> {
        self.ordered_projects()
            .into_iter()
            .filter(|p| self.project_status(&p.id) != crate::api::project_status::INACTIVE)
            .map(|p| p.id.clone())
            .collect()
    }

    /// Every user project in Core's saved order (`position`, with creation
    /// order as the fallback for an older Core that has no field).
    fn ordered_projects(&self) -> Vec<&crate::api::Project> {
        let mut v: Vec<_> = self.state.model.projects.values().collect();
        v.sort_by(|a, b| {
            (a.position.is_none(), a.position.unwrap_or(0))
                .cmp(&(b.position.is_none(), b.position.unwrap_or(0)))
                .then_with(|| a.created.total_cmp(&b.created))
                .then_with(|| a.id.cmp(&b.id))
        });
        v
    }

    /// `current`, `passive` or `inactive`; an older Core means current.
    pub fn project_status(&self, id: &str) -> &'static str {
        match self
            .state
            .model
            .projects
            .get(id)
            .and_then(|p| p.status.as_deref())
        {
            Some(crate::api::project_status::PASSIVE) => crate::api::project_status::PASSIVE,
            Some(crate::api::project_status::INACTIVE) => crate::api::project_status::INACTIVE,
            _ => crate::api::project_status::CURRENT,
        }
    }

    /// Rail initials are assigned in creation order, so moving a project never
    /// changes any project's label.
    pub(crate) fn rail_label_map(&self) -> HashMap<String, String> {
        let mut creation: Vec<_> = self.state.model.projects.values().collect();
        creation.sort_by(|a, b| {
            a.created
                .total_cmp(&b.created)
                .then_with(|| a.id.cmp(&b.id))
        });
        let labels = crate::render::rail_labels(creation.iter().map(|p| p.name.clone()));
        creation
            .into_iter()
            .map(|p| p.id.clone())
            .zip(labels)
            .collect()
    }

    /// Move a project one place in Core's saved order. `delta` is -1 or 1.
    pub fn move_project_step(&mut self, project: &str, delta: isize) {
        let ids = self.project_ids();
        let Some(index) = ids.iter().position(|id| id == project) else {
            return;
        };
        let target = index as isize + delta;
        if target < 0 || target >= ids.len() as isize {
            return;
        }
        let neighbour = &ids[target as usize];
        let (before, after) = if delta < 0 {
            (Some(neighbour.clone()), None)
        } else {
            (None, Some(neighbour.clone()))
        };
        self.api(ApiCall::MoveProject {
            project: project.to_owned(),
            before,
            after,
        });
    }

    pub fn set_project_status(&mut self, project: &str, status: &str) {
        if self.project_status(project) == status {
            return;
        }
        self.api(ApiCall::SetProjectStatus {
            project: project.to_owned(),
            status: status.to_owned(),
        });
    }

    fn list_len(&self, tab: Tab) -> usize {
        match tab {
            Tab::Sessions => self.side_entries().len(),
            Tab::Projects => self.state.model.projects.len(),
            Tab::Files => self.file_rows().len(),
            Tab::Git => self.project_git_rows().len(),
        }
    }

    pub fn cursor(&self, tab: Tab) -> usize {
        self.ui
            .cursor
            .get(&tab)
            .copied()
            .unwrap_or(0)
            .min(self.list_len(tab).saturating_sub(1))
    }

    // ---- terms and availability -----------------------------------------------

    /// Evaluate a `when` term. `None` means the term is not known to this client.
    pub fn term(&self, name: &str) -> Option<bool> {
        let busy = self.viewed_id().is_some_and(|id| self.is_busy(id));
        let has_session = self.viewed().is_some();
        let waiting = self.wait_focused();
        Some(match name {
            "always" => true,
            "has_project" => self.current_project().is_some(),
            "project.current" => self
                .current_project()
                .is_some_and(|id| self.project_status(&id) == crate::api::project_status::CURRENT),
            "project.passive" => self
                .current_project()
                .is_some_and(|id| self.project_status(&id) == crate::api::project_status::PASSIVE),
            "project.inactive" => self
                .current_project()
                .is_some_and(|id| self.project_status(&id) == crate::api::project_status::INACTIVE),
            "project.can_move_up" => self.current_project().is_some_and(|id| {
                self.project_ids().first().is_some_and(|first| *first != id)
                    && self.project_ids().contains(&id)
            }),
            "project.can_move_down" => self.current_project().is_some_and(|id| {
                self.project_ids().last().is_some_and(|last| *last != id)
                    && self.project_ids().contains(&id)
            }),
            "has_session" => has_session,
            "has_turns" => self.messages().iter().any(|m| m.role == "user"),
            "session.idle" => has_session && !busy,
            "session.busy" => has_session && busy,
            "session.interrupted" => has_session && !busy && self.last_turn_interrupted(),
            "session.archived" => self.viewed().is_some_and(|s| s.archived),
            "session.child" => self.viewed().is_some_and(|s| s.parent_id.is_some()),
            "session.continued" => self.viewed().is_some_and(|s| s.continued_from.is_some()),
            "session.in_worktree" => self.worktree_info().is_some(),
            "session.linked_worktree" => self.worktree_info().is_some_and(|w| w.linked),
            "has_waiting" => {
                !self.state.model.approvals.is_empty() || !self.state.model.questions.is_empty()
            }
            "waiting.approval" => waiting
                .as_ref()
                .is_some_and(|w| w.kind == WaitKind::Approval),
            "waiting.question" => waiting
                .as_ref()
                .is_some_and(|w| w.kind == WaitKind::Question),
            "waiting.multiple" => self.wait_items().len() > 1,
            "composer.has_text" => {
                self.composer_ref().has_text() || !self.composer_ref().images.is_empty()
            }
            "composer.empty" => {
                self.composer_ref().is_empty() && self.composer_ref().images.is_empty()
            }
            "sidebar.projects_focus" => {
                self.ui.focus == Focus::Sidebar
                    && self.ui.tab == Tab::Projects
                    && self.ui.overlay.is_none()
            }
            "has_queued" => !self.pending_inputs().is_empty(),
            "timeline.scrolled_up" => !self.ui.scroll.follow,
            "timeline.has_older" => self
                .state
                .model
                .selected
                .as_ref()
                .is_some_and(|v| v.messages_more),
            "tool.output_clipped_in_ui" => self
                .ui
                .timeline_sel
                .and_then(|i| self.ui.timeline_blocks.get(i))
                .is_some_and(|b| b.kind == crate::timeline::BlockKind::Tool && b.clipped),
            "focus.session_row" => {
                self.ui.focus == Focus::Sidebar
                    && self.ui.tab == Tab::Sessions
                    && self.list_len(Tab::Sessions) > 0
            }
            "focus.project" => {
                self.ui.focus == Focus::Sidebar
                    && self.ui.tab == Tab::Projects
                    && self.list_len(Tab::Projects) > 0
            }
            "focus.assistant_block" => {
                self.ui.focus == Focus::Timeline
                    && self
                        .ui
                        .timeline_sel
                        .and_then(|i| self.ui.timeline_blocks.get(i))
                        .is_some_and(|b| b.kind == crate::timeline::BlockKind::Assistant)
            }
            "focus.code_block" => {
                self.ui.focus == Focus::Timeline
                    && self
                        .ui
                        .timeline_sel
                        .and_then(|i| self.ui.timeline_blocks.get(i))
                        .is_some_and(|b| b.kind == crate::timeline::BlockKind::Code)
            }
            "focus.file_entry" => {
                self.ui.focus == Focus::Sidebar
                    && self
                        .selected_file()
                        .is_some_and(|r| r.kind == "file" || r.kind == "directory")
            }
            "focus.file" => {
                self.ui.focus == Focus::Sidebar
                    && self
                        .selected_file()
                        .is_some_and(|row| row.kind != "directory" && row.kind != "error")
            }
            "focus.git_file" => {
                (self.ui.tab == Tab::Git
                    && self.ui.focus == Focus::Sidebar
                    && self.project_git_rows().get(self.cursor(Tab::Git)).is_some())
                    || (self.ui.focus == Focus::Right
                        && self.git_rows().get(self.ui.git_cursor).is_some())
            }
            "focus.turn" | "focus.tool" | "focus.user_message" | "focus.child_card" => {
                use crate::timeline::BlockKind as K;
                let want = match name {
                    "focus.turn" => K::Turn,
                    "focus.tool" => K::Tool,
                    "focus.user_message" => K::User,
                    _ => K::Child,
                };
                self.ui.focus == Focus::Timeline
                    && self
                        .ui
                        .timeline_sel
                        .and_then(|i| self.ui.timeline_blocks.get(i))
                        .is_some_and(|b| {
                            b.kind == want || (name == "focus.child_card" && b.kind == K::Handoff)
                        })
            }
            "panel.file" => self.ui.file_open,
            "panel.diff" => self.ui.diff_open.is_some(),
            "terminal.open" => self.terminal_open,
            "terminal.selected" => self.terminal_selected,
            "terminal.focused" => self.terminal_focused,
            "terminal.controlled" => self.terminal_controlled,
            "editor.dirty" => self.ui.file_edit.as_ref().is_some_and(FileEdit::dirty),
            "editor.readonly" => self.ui.file_open && self.ui.file_edit.is_none(),
            "editor.markdown" => self.ui.file_open && self.file_is_markdown(),
            "view.settings" => self.ui.view == View::Settings,
            "overlay.open" => self.ui.overlay.is_some(),
            "dialog.open" => self.ui.dialog.is_some() || self.blocking().is_some(),
            "alert.open" => self.ui.focus == Focus::Alert,
            "drawer.open" => self.ui.drawer.is_some(),
            "layout.narrow" => self.narrow(),
            "connection.lost" => matches!(
                self.state.connection,
                ConnectionState::Reconnecting { .. }
                    | ConnectionState::Offline { .. }
                    | ConnectionState::AuthFailed { .. }
            ),
            "connection.no_daemon" => {
                matches!(self.state.connection, ConnectionState::Offline { .. })
            }
            "provider.signed_in" => false,
            "has_notifications" => !self.ui.alerts.history.is_empty(),
            "text_edit.active" => self.ui.edit.is_some(),
            n if n.starts_with("manager.") => self.manager_term(n),
            _ => return None,
        })
    }

    pub fn holds(&self, t: &str) -> bool {
        let (neg, name) = term(t);
        self.term(name).unwrap_or(false) != neg
    }

    /// `Err(reason)` when the command is disabled. Order: Core status, not
    /// wired, read-only, then the manifest's own condition.
    pub fn availability(&self, c: &Command) -> Result<(), String> {
        let reason = || {
            c.unavailable_reason
                .clone()
                .unwrap_or_else(|| "Not available right now.".into())
        };
        if matches!(c.status.as_deref(), Some("blocked_core" | "deferred")) {
            return Err(reason());
        }
        if !WIRED.contains(&c.id.as_str()) {
            return Err(NOT_WIRED.into());
        }
        if MUTATING.contains(&c.id.as_str()) && !self.writable() {
            return Err(READ_ONLY.into());
        }
        if let Some(reason) = self.voice_command_disabled_reason(&c.id) {
            return Err(reason);
        }
        if c.when.iter().all(|t| self.holds(t)) {
            Ok(())
        } else {
            Err(reason())
        }
    }

    pub fn blocking(&self) -> Option<(String, Vec<String>)> {
        match &self.state.connection {
            ConnectionState::Incompatible { .. } => Some((
                "Incompatible Core".into(),
                vec![
                    self.state.connection.detail(),
                    "Nothing is sent to Core until this is fixed. ctrl+q quits.".into(),
                ],
            )),
            _ => None,
        }
    }

    pub fn banners(&self) -> Vec<BannerItem> {
        let mut v = Vec::new();
        let c = &self.state.connection;
        let retry = || Some(("".into(), "retry".into(), "app.reconnect".into()));
        match c {
            ConnectionState::Starting | ConnectionState::Connecting => v.push(BannerItem {
                level: Level::Info,
                loading: true,
                text: c.detail(),
                action: None,
                hide: None,
            }),
            ConnectionState::Reconnecting { retry_in, .. } => v.push(BannerItem {
                level: Level::Error,
                loading: false,
                text: format!(
                    "Disconnected from Core {} retrying in {}s",
                    self.theme.structure("separator"),
                    retry_in.as_secs().max(1)
                ),
                action: retry(),
                hide: None,
            }),
            ConnectionState::Offline { .. } => v.push(BannerItem {
                level: Level::Error,
                loading: false,
                text: format!(
                    "Core is not reachable {} run `nexus daemon start`",
                    self.theme.structure("separator")
                ),
                action: retry(),
                hide: None,
            }),
            ConnectionState::AuthFailed { .. } => v.push(BannerItem {
                level: Level::Error,
                loading: false,
                text: c.detail(),
                action: None,
                hide: None,
            }),
            ConnectionState::Live | ConnectionState::Incompatible { .. } => {}
        }
        for (i, b) in self.state.banners.iter().enumerate() {
            let (level, text) = match b {
                Banner::ApiUnreported => (
                    Level::Warning,
                    "Core does not report an API version yet; compatibility is unchecked"
                        .to_owned(),
                ),
                Banner::NewerCore { core } => (
                    Level::Info,
                    format!(
                        "Core speaks API {core}, newer than this client; some features are missing"
                    ),
                ),
                Banner::DaemonRestarted => (
                    Level::Info,
                    "Core restarted; the view was resynced from a fresh snapshot".to_owned(),
                ),
                Banner::Resynced(why) => (Level::Info, format!("Resynced from Core ({why})")),
            };
            v.push(BannerItem {
                level,
                loading: false,
                text,
                action: None,
                hide: Some(i),
            });
        }
        let elsewhere = self.waiting_elsewhere().len();
        if elsewhere > 0 {
            v.push(BannerItem {
                level: Level::Info,
                loading: false,
                text: format!(
                    "{elsewhere} other session{} waiting for input",
                    if elsewhere == 1 { "" } else { "s" }
                ),
                action: Some(("".into(), "go".into(), "waiting.go_to".into())),
                hide: None,
            });
        }
        let rank = |l: Level| match l {
            Level::Error => 0,
            Level::Warning => 1,
            _ => 2,
        };
        v.sort_by_key(|b| rank(b.level));
        v
    }

    // ---- composer & drafts ------------------------------------------------------

    fn draft_key(&self) -> String {
        self.viewed_id().map(str::to_owned).unwrap_or_else(|| {
            Self::new_session_draft_key(self.ui.project.as_deref().unwrap_or(""))
        })
    }

    fn new_session_draft_key(project: &str) -> String {
        format!("new:{project}")
    }

    pub fn composer_ref(&self) -> &Composer {
        static EMPTY: std::sync::OnceLock<Composer> = std::sync::OnceLock::new();
        self.ui
            .drafts
            .get(&self.draft_key())
            .unwrap_or_else(|| EMPTY.get_or_init(Composer::new))
    }

    /// The draft as drawn: while dictating, the live words sit inside it at the
    /// position where recording began.
    pub fn composer_view(&self) -> std::borrow::Cow<'_, Composer> {
        let d = &self.ui.dictation;
        if d.preview.is_empty() || d.draft_key != self.draft_key() {
            return std::borrow::Cow::Borrowed(self.composer_ref());
        }
        let shown = d.shown_preview(!self.theme.opts.reduced_motion && !self.theme.mono());
        std::borrow::Cow::Owned(self.composer_ref().with_inserted(d.anchor, &shown))
    }

    pub fn composer(&mut self) -> &mut Composer {
        let k = self.draft_key();
        self.ui.drafts.entry(k).or_default()
    }

    fn restore_draft(&mut self, session: Option<&str>, text: &str) {
        let c = self
            .ui
            .drafts
            .entry(session.unwrap_or("").to_owned())
            .or_default();
        if c.is_empty() {
            c.set_text(text);
        } else {
            let rest = c.expanded_text();
            c.set_text(&format!("{text}\n{rest}"));
        }
    }

    /// Put a sent draft back exactly: a collapsed paste keeps its marker and
    /// preview instead of coming back as the whole pasted text.
    fn restore_composer(&mut self, session: Option<&str>, saved: &crate::composer::Composer) {
        let c = self
            .ui
            .drafts
            .entry(session.unwrap_or("").to_owned())
            .or_default();
        if c.is_empty() {
            let typed_images = std::mem::take(&mut c.images);
            *c = saved.clone();
            c.images = typed_images;
        } else {
            let rest = c.expanded_text();
            c.set_text(&format!("{}\n{rest}", saved.expanded_text()));
        }
    }

    /// Put removed pending input back in a session's composer: its text and
    /// images, with an existing draft below it on a new line. Parts from several
    /// inputs arrive in queue order, so steers and queued messages come back the
    /// way they were written.
    fn restore_input_parts(&mut self, session: &str, parts: &[serde_json::Value]) -> bool {
        let mut text = Vec::new();
        let mut images = Vec::new();
        for part in parts {
            if part["type"] == "text" {
                if let Some(t) = part["text"].as_str() {
                    if !t.starts_with("[Image ") {
                        text.push(t.to_owned());
                    }
                }
            } else if part["type"] == "image" {
                if let Ok(image) = serde_json::from_value::<crate::images::Image>(part.clone()) {
                    images.push(image);
                }
            }
        }
        text.extend(images.iter().map(|image| format!("[{}]", image.name)));
        if !text.is_empty() {
            self.restore_draft(Some(session), &text.join("\n"));
        }
        if !images.is_empty() {
            self.ui
                .drafts
                .entry(session.to_owned())
                .or_default()
                .images
                .extend(images);
        }
        !text.is_empty()
    }

    // ---- focus & overlays --------------------------------------------------------

    pub(crate) fn restore_point(&self) -> Restore {
        Restore {
            focus: self.ui.focus,
            scroll: self.ui.scroll,
        }
    }

    fn apply_restore(&mut self, r: Restore) {
        self.ui.focus = r.focus;
        self.ui.scroll = r.scroll;
    }

    pub fn focus_order(&self) -> Vec<Focus> {
        // The file and diff modal covers everything but its own view.
        let covered = self.ui.file_open || self.ui.diff_open.is_some();
        let mut v = vec![Focus::ToggleLeft];
        if self.ui.left_open && !self.narrow() && !covered {
            v.push(Focus::Sidebar);
        }
        v.push(Focus::Timeline);
        if !covered && !self.wait_items().is_empty() && self.ui.view == View::Session {
            v.push(Focus::Waiting);
        }
        if !covered && self.ui.view == View::Session {
            v.push(Focus::Composer);
        }
        if self.ui.right_open && !self.narrow() && !covered {
            v.push(Focus::Right);
        }
        v.push(Focus::ToggleRight);
        if self.has_alerts() {
            v.push(Focus::Alert);
        }
        v
    }

    fn has_alerts(&self) -> bool {
        !self.ui.alerts.toasts.is_empty() || self.banners().iter().any(|b| b.hide.is_some())
    }

    fn cycle_focus(&mut self, back: bool) {
        let order = self.focus_order();
        let at = order.iter().position(|f| *f == self.ui.focus).unwrap_or(0);
        let next = if back {
            (at + order.len() - 1) % order.len()
        } else {
            (at + 1) % order.len()
        };
        self.ui.focus = order[next];
        if self.ui.drawer.is_some() && !matches!(self.ui.focus, Focus::Sidebar | Focus::Right) {
            // keep focus inside an open drawer
            self.ui.focus = if self.ui.drawer == Some(Side::Left) {
                Focus::Sidebar
            } else {
                Focus::Right
            };
        }
    }

    pub fn open_overlay(&mut self, kind: OverlayKind) {
        let restore = match self.ui.overlay.take() {
            Some(o) => o.restore,
            None => self.restore_point(),
        };
        self.ui.overlay = Some(Overlay { kind, restore });
    }

    pub fn open_palette(&mut self, mode: PaletteMode) {
        self.open_overlay(OverlayKind::Palette(Palette {
            mode,
            query: Composer::new(),
            selected: 0,
            top: 0,
            model_info: false,
        }));
    }

    /// The sessions switcher with its search field focused, optionally
    /// prefilled by `/search <text>`. Typing filters without another key.
    fn open_sessions_search(&mut self, query: &str) {
        let mut q = Composer::new();
        if !query.trim().is_empty() {
            q.set_text(query);
        }
        self.open_overlay(OverlayKind::Palette(Palette {
            mode: PaletteMode::Sessions,
            query: q,
            selected: 0,
            top: 0,
            model_info: false,
        }));
        self.ask_search();
    }

    fn close_overlay(&mut self) {
        if let Some(btw) = self.ui.btw.take() {
            if btw.running {
                if let Some(session) = btw.session {
                    self.api(ApiCall::Interrupt { session });
                }
            }
        }
        if let Some(Overlay {
            kind: OverlayKind::ContextEntries {
                title, open, top, ..
            },
            ..
        }) = self.ui.overlay.as_mut()
        {
            if title == "Skills" && open.is_some() {
                *open = None;
                *top = 0;
                return;
            }
        }
        if let Some(Overlay {
            kind:
                OverlayKind::AlertDetail {
                    back: Some(selected),
                    ..
                },
            restore,
        }) = self
            .ui
            .overlay
            .take_if(|o| matches!(o.kind, OverlayKind::AlertDetail { back: Some(_), .. }))
        {
            self.ui.overlay = Some(Overlay {
                kind: OverlayKind::Notifications { selected },
                restore,
            });
            return;
        }
        if let Some(o) = self.ui.overlay.take() {
            self.apply_restore(o.restore);
        }
    }

    pub fn toast(&mut self, level: Level, text: impl Into<String>) {
        let now = self.clock;
        self.ui.alerts.push(level, text, now);
    }

    /// The session whose directory the Files tree browses. The project checkout
    /// is requested when the user chose it or there is no session.
    pub fn files_session(&self) -> Option<String> {
        if self.ui.files_project_root {
            return None;
        }
        let project = self.current_project();
        self.viewed()
            .filter(|session| Some(&session.project_id) == project.as_ref())
            .map(|session| session.id.clone())
    }

    /// Absolute directory the Files tree is rooted at.
    pub fn files_root(&self) -> Option<String> {
        let project = self.current_project();
        if !self.ui.files_project_root {
            if let Some(session) = self
                .viewed()
                .filter(|session| Some(&session.project_id) == project.as_ref())
            {
                if !session.directory.is_empty() {
                    return Some(session.directory.clone());
                }
            }
        }
        project.and_then(|p| self.state.model.projects.get(&p).map(|p| p.path.clone()))
    }

    /// The Files root header (DESIGN_PLAN.md D16): the checkout's name, its
    /// branch when Git reports one, and whether there are several checkouts to
    /// switch between (a click or `files.root_toggle`).
    pub fn files_header(&self) -> Option<FilesHeader> {
        let project = self.current_project()?;
        let root = self.files_root()?;
        let trees = self
            .state
            .project_worktrees
            .get(&project)
            .and_then(|listing| listing["worktrees"].as_array());
        let entry = trees.and_then(|trees| {
            trees
                .iter()
                .filter(|tree| {
                    tree["path"]
                        .as_str()
                        .is_some_and(|path| root.starts_with(path))
                })
                .max_by_key(|tree| tree["path"].as_str().map_or(0, str::len))
        });
        let name = if self.ui.files_project_root || self.viewed().is_none() {
            self.state.model.projects.get(&project)?.name.clone()
        } else {
            std::path::Path::new(&root)
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_else(|| root.clone())
        };
        Some(FilesHeader {
            name,
            branch: entry
                .and_then(|tree| tree["branch"].as_str())
                .filter(|branch| !branch.is_empty())
                .map(str::to_owned),
            switchable: trees.is_some_and(|trees| trees.len() >= 2),
        })
    }

    /// Rows of the Files tree in draw order, depth-first through expanded folders.
    pub fn file_rows(&self) -> Vec<FileRow> {
        let scope = (
            self.current_project().unwrap_or_default(),
            self.files_session(),
        );
        if self.state.file_scope.as_ref() != Some(&scope) {
            return Vec::new();
        }
        let mut out = Vec::new();
        self.walk_files("", 0, &mut out);
        out
    }

    fn walk_files(&self, dir: &str, depth: usize, out: &mut Vec<FileRow>) {
        let Some(listing) = self.state.file_dirs.get(dir) else {
            return;
        };
        let entries = match listing {
            Ok(entries) => entries,
            Err(error) => {
                out.push(FileRow {
                    path: dir.to_owned(),
                    name: error.clone(),
                    kind: "error".into(),
                    depth,
                    expanded: false,
                    git_status: None,
                    ignored: false,
                });
                return;
            }
        };
        for entry in entries {
            let name = entry["name"].as_str().unwrap_or("");
            if name == ".git" {
                continue;
            }
            let ignored = entry["ignored"].as_bool().unwrap_or(false);
            if ignored && self.ui.files_hide_ignored {
                continue;
            }
            let path = if dir.is_empty() {
                name.to_owned()
            } else {
                format!("{dir}/{name}")
            };
            let kind = entry["kind"].as_str().unwrap_or("file").to_owned();
            let expanded = kind == "directory" && self.ui.files_expanded.contains(&path);
            out.push(FileRow {
                path: path.clone(),
                name: name.to_owned(),
                kind: kind.clone(),
                depth,
                expanded,
                git_status: entry["git_status"]
                    .as_str()
                    .filter(|status| !status.is_empty())
                    .map(str::to_owned),
                ignored,
            });
            if expanded {
                self.walk_files(&path, depth + 1, out);
            }
        }
    }

    /// Changed files of the viewed session in draw order: Staged, Unstaged,
    /// then Untracked. Empty until Core's Git status for it has arrived.
    fn git_worktree(&self) -> Option<String> {
        let project = self.current_project()?;
        let session = self.viewed().filter(|s| s.project_id == project);
        let directory = session.map(|s| s.directory.as_str()).or_else(|| {
            self.state
                .model
                .projects
                .get(&project)
                .map(|p| p.path.as_str())
        })?;
        let listing = session
            .and_then(|s| self.state.worktrees.get(&s.id))
            .or_else(|| self.state.project_worktrees.get(&project))?;
        listing["worktrees"]
            .as_array()?
            .iter()
            .filter_map(|tree| tree["path"].as_str())
            .filter(|path| std::path::Path::new(directory).starts_with(path))
            .max_by_key(|path| path.len())
            .map(str::to_owned)
    }

    pub fn project_git_rows(&self) -> Vec<GitRow> {
        let Some((project, Ok(data))) = &self.state.project_git else {
            return Vec::new();
        };
        if self.current_project().as_ref() != Some(project) {
            return Vec::new();
        }
        let mut rows = Vec::new();
        for group in [GitGroup::Staged, GitGroup::Unstaged, GitGroup::Untracked] {
            for change in data["changes"].as_array().into_iter().flatten() {
                let status = change["status"].as_str().unwrap_or("");
                let untracked = status == "??";
                let member = match group {
                    GitGroup::Staged => change["staged"].as_bool().unwrap_or(false),
                    GitGroup::Unstaged => {
                        !untracked && change["unstaged"].as_bool().unwrap_or(false)
                    }
                    GitGroup::Untracked => untracked,
                };
                if member {
                    rows.push(GitRow {
                        group,
                        path: change["path"].as_str().unwrap_or("").into(),
                        status: status.into(),
                        added: 0,
                        removed: 0,
                    });
                }
            }
        }
        rows
    }

    pub fn git_rows(&self) -> Vec<GitRow> {
        let Some(id) = self.viewed_id() else {
            return Vec::new();
        };
        let Some(Ok(data)) = self
            .state
            .git
            .as_ref()
            .filter(|(session, _)| session == id)
            .map(|(_, result)| result)
        else {
            return Vec::new();
        };
        let changes = data["changes"].as_array().map_or(&[][..], Vec::as_slice);
        let mut rows = Vec::new();
        for group in [GitGroup::Staged, GitGroup::Unstaged, GitGroup::Untracked] {
            for change in changes {
                let status = change["status"].as_str().unwrap_or("");
                let untracked = status == "??";
                let member = match group {
                    GitGroup::Staged => change["staged"].as_bool().unwrap_or(false),
                    GitGroup::Unstaged => {
                        !untracked && change["unstaged"].as_bool().unwrap_or(false)
                    }
                    GitGroup::Untracked => untracked,
                };
                if member {
                    rows.push(GitRow {
                        group,
                        path: change["path"].as_str().unwrap_or("").to_owned(),
                        status: status.to_owned(),
                        added: change["added"].as_u64().unwrap_or(0),
                        removed: change["removed"].as_u64().unwrap_or(0),
                    });
                }
            }
        }
        rows
    }

    /// Open the diff of a changed-file row in the centre column.
    fn open_project_git_diff(&mut self, index: usize) {
        let Some(row) = self.project_git_rows().get(index).cloned() else {
            return;
        };
        let Some(project) = self.current_project() else {
            return;
        };
        if self.edit_blocks() {
            return;
        }
        self.ui.file_open = false;
        self.ui.file_edit = None;
        self.ui.diff_scroll = 0;
        self.state.diff = None;
        if row.group != GitGroup::Untracked {
            self.api(ApiCall::ProjectGitDiff {
                project,
                worktree: self.git_worktree(),
                path: row.path.clone(),
            });
        }
        self.ui.diff_open = Some((row.path, row.group));
        self.ui.focus = Focus::Timeline;
    }

    fn open_git_diff(&mut self, index: usize) {
        let Some(row) = self.git_rows().get(index).cloned() else {
            return;
        };
        let Some(session) = self.viewed_id().map(str::to_owned) else {
            return;
        };
        if self.edit_blocks() {
            return;
        }
        self.ui.git_cursor = index;
        self.ui.file_open = false;
        self.ui.file_edit = None;
        self.ui.diff_scroll = 0;
        self.state.diff = None;
        // Core's diff has no untracked contents, so there is nothing to ask.
        if row.group != GitGroup::Untracked {
            self.effects.push(Effect::Sync(SyncCommand::GitDiff {
                session,
                path: row.path.clone(),
            }));
        }
        self.ui.diff_open = Some((row.path, row.group));
        if self.narrow() {
            self.ui.drawer = None;
        }
        self.ui.focus = Focus::Timeline;
    }

    /// The selected Files row, when the Files tab has focus.
    pub fn selected_file(&self) -> Option<FileRow> {
        if self.ui.tab != Tab::Files {
            return None;
        }
        self.file_rows().get(self.cursor(Tab::Files)).cloned()
    }

    /// Absolute path of a Files row, under the current tree root.
    fn file_absolute(&self, row: &FileRow) -> Option<String> {
        let root = self.files_root()?;
        Some(
            std::path::Path::new(&root)
                .join(&row.path)
                .to_string_lossy()
                .into_owned(),
        )
    }

    fn request_files(&mut self, path: String, file: bool) {
        if let Some(project) = self.current_project() {
            if file {
                self.state.file = None;
            } else {
                self.state.file_dirs.remove(&path);
            }
            self.effects.push(Effect::Sync(SyncCommand::Files {
                project,
                session: self.files_session(),
                path,
                file,
            }));
        }
    }

    fn reset_files(&mut self) {
        self.state.file_dirs.clear();
        self.state.file_scope = None;
        self.state.file = None;
        self.ui.files_expanded.clear();
        self.ui.file_open = false;
        self.ui.file_edit = None;
        self.ui.file_scroll = 0;
        self.ui.diff_open = None;
        self.ui.git_cursor = 0;
        self.ui.cursor.insert(Tab::Files, 0);
        if self.ui.tab == Tab::Files {
            self.request_files(String::new(), false);
        }
    }

    fn set_tab(&mut self, tab: Tab) {
        self.ui.tab = tab;
        self.ui.side_top = 0;
        if tab == Tab::Files {
            self.request_files(String::new(), false);
        }
        if tab == Tab::Git {
            if let Some(project) = self.current_project() {
                self.api(ApiCall::ProjectGit {
                    project,
                    worktree: self.git_worktree(),
                });
            }
        }
        if self.narrow() {
            self.ui.drawer = Some(Side::Left);
        } else {
            self.ui.left_open = true;
        }
        self.ui.focus = Focus::Sidebar;
    }

    // ---- palette --------------------------------------------------------------------

    pub fn palette_items(&self) -> Vec<PaletteItem> {
        let Some(Overlay {
            kind: OverlayKind::Palette(p),
            ..
        }) = &self.ui.overlay
        else {
            return Vec::new();
        };
        let q = p.query.text();
        let mut scored: Vec<(i32, usize, PaletteItem)> = Vec::new();
        match p.mode {
            PaletteMode::Commands => {
                for (i, c) in self.manifest.command.iter().enumerate().filter(|(_, c)| {
                    c.in_palette()
                        && c.key_context.as_deref() != Some("dialog")
                        && c.id != "palette.open"
                }) {
                    let score = fuzzy(&q, &c.title)
                        .map(|s| s * 2)
                        .or_else(|| fuzzy(&q, &c.id))
                        .or_else(|| fuzzy(&q, &c.category));
                    let Some(score) = score else { continue };
                    let reason = self.availability(c).err();
                    let bonus = if reason.is_none() { 1000 } else { 0 };
                    scored.push((
                        score + if q.is_empty() { 0 } else { bonus / 10 },
                        i,
                        PaletteItem {
                            kind: PaletteKind::Command(c.id.clone()),
                            label: c.title.clone(),
                            hint: c.tui_key().unwrap_or("").to_owned(),
                            category: c.category.clone(),
                            reason,
                        },
                    ));
                }
            }
            PaletteMode::RowMenu => {
                let projects = self.ui.tab == Tab::Projects;
                let files = self.ui.tab == Tab::Files;
                let git = self.ui.tab == Tab::Git;
                for (i, c) in self.manifest.command.iter().enumerate().filter(|(_, c)| {
                    c.for_tui()
                        && c.placement.iter().any(|p| p == "row_menu")
                        && c.id.starts_with(if files {
                            "files."
                        } else if git {
                            "git."
                        } else if projects {
                            "project."
                        } else {
                            "session."
                        })
                }) {
                    let Some(score) = fuzzy(&q, &c.title) else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Command(c.id.clone()),
                            label: c.title.clone(),
                            hint: c.tui_key().unwrap_or("").to_owned(),
                            category: c.category.clone(),
                            reason: self.availability(c).err(),
                        },
                    ));
                }
            }
            PaletteMode::ProviderLogin | PaletteMode::ProviderLogout => {
                let sign_out = p.mode == PaletteMode::ProviderLogout;
                for (i, id) in ["codex", "copilot", "grok", "opencode"].iter().enumerate() {
                    if let Some(score) = fuzzy(&q, id) {
                        scored.push((
                            score,
                            i,
                            PaletteItem {
                                kind: PaletteKind::Provider((*id).into(), sign_out),
                                label: (*id).into(),
                                hint: String::new(),
                                category: "provider".into(),
                                reason: None,
                            },
                        ));
                    }
                }
            }
            PaletteMode::Sessions => {
                for (i, s) in self.state.model.sessions_recent().into_iter().enumerate() {
                    if s.parent_id.is_some()
                        || (!self.ui.scope_all
                            && self.current_project().as_deref() != Some(s.project_id.as_str()))
                    {
                        continue;
                    }
                    let Some(score) = fuzzy(&q, &s.title) else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Session(s.id.clone()),
                            label: s.title.clone(),
                            hint: crate::text::age(now_secs(), s.updated),
                            category: "session".into(),
                            reason: None,
                        },
                    ));
                }
                // Sessions whose user messages match, after the title matches.
                if let Some((asked, hits)) = self.ui.search.as_ref().filter(|(a, _)| a == q.trim())
                {
                    let _ = asked;
                    for (i, hit) in hits.iter().enumerate() {
                        let Some(id) = hit["session_id"].as_str() else {
                            continue;
                        };
                        let Some(s) = self.state.model.sessions.get(id) else {
                            continue;
                        };
                        if s.parent_id.is_some()
                            || scored
                                .iter()
                                .any(|(_, _, it)| it.kind == PaletteKind::Session(id.to_owned()))
                        {
                            continue;
                        }
                        let snippet = hit["snippet"]
                            .as_str()
                            .unwrap_or("")
                            .replace(['[', ']'], "");
                        scored.push((
                            i32::MIN / 2,
                            1000 + i,
                            PaletteItem {
                                kind: PaletteKind::Session(id.to_owned()),
                                label: s.title.clone(),
                                hint: crate::text::truncate(&snippet, 32, "…"),
                                category: "message".into(),
                                reason: None,
                            },
                        ));
                    }
                }
            }
            PaletteMode::Projects => {
                for (i, id) in self.project_ids().into_iter().enumerate() {
                    let p = &self.state.model.projects[&id];
                    let Some(score) = fuzzy(&q, &p.name) else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Project(id.clone()),
                            label: p.name.clone(),
                            hint: if self.project_status(&id) == crate::api::project_status::CURRENT
                            {
                                p.path.clone()
                            } else {
                                format!("{} · {}", self.project_status(&id), p.path)
                            },
                            category: "project".into(),
                            reason: None,
                        },
                    ));
                }
            }
            PaletteMode::Models => {
                let current = self.viewed().map_or_else(
                    || self.ui.draft.model.clone().unwrap_or_default(),
                    |s| s.model.clone().unwrap_or_default(),
                );
                let favorites = self.favorite_models();
                for (i, m) in self.state.models.iter().enumerate() {
                    if m["zmx"].as_bool() == Some(true) {
                        continue; // ZMX models are for subagent pools only
                    }
                    let r = m["ref"].as_str().unwrap_or_default();
                    let label = m["name"]
                        .as_str()
                        .filter(|n| !n.is_empty())
                        .unwrap_or_else(|| r.split_once('/').map_or(r, |(_, name)| name))
                        .to_owned();
                    let Some(score) = fuzzy(&q, &label).or_else(|| fuzzy(&q, r)) else {
                        continue;
                    };
                    let provider = r.split_once('/').map_or("", |(provider, _)| provider);
                    let favorite = favorites.iter().any(|f| f == r);
                    scored.push((
                        score,
                        if favorite {
                            i
                        } else {
                            i + self.state.models.len()
                        },
                        PaletteItem {
                            kind: PaletteKind::Model(r.to_owned()),
                            label: if favorite {
                                format!("★ {label}")
                            } else {
                                label
                            },
                            hint: provider.to_owned(),
                            category: if r == current { "current" } else { "model" }.into(),
                            reason: None,
                        },
                    ));
                }
                if self.state.models.is_empty() {
                    scored.push((
                        0,
                        0,
                        PaletteItem {
                            kind: PaletteKind::Model(String::new()),
                            label: "No models".into(),
                            hint: self.state.catalog_source.clone(),
                            category: "model".into(),
                            reason: Some(
                                "No provider is signed in, or the catalog has not loaded yet"
                                    .into(),
                            ),
                        },
                    ));
                }
            }
            PaletteMode::PoolModels => {
                let have = self
                    .ui
                    .pool_pick
                    .as_ref()
                    .map(|(agent, fallback)| self.agent_refs(agent, *fallback))
                    .unwrap_or_default();
                for (i, m) in self.state.models.iter().enumerate() {
                    let r = m["ref"].as_str().unwrap_or_default();
                    if r.is_empty() || have.iter().any(|h| h == r) {
                        continue;
                    }
                    let Some(score) =
                        fuzzy(&q, r).or_else(|| fuzzy(&q, m["name"].as_str().unwrap_or("")))
                    else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::PoolModel(r.to_owned()),
                            label: r.to_owned(),
                            hint: m["name"].as_str().unwrap_or_default().to_owned(),
                            category: "model".into(),
                            reason: None,
                        },
                    ));
                }
            }
            PaletteMode::Agents => {
                let current = self.current_agent();
                for (i, a) in self.state.agents.iter().enumerate() {
                    if !self.agent_eligible(a) {
                        continue;
                    }
                    let name = a["name"].as_str().unwrap_or("");
                    let Some(score) = fuzzy(&q, name) else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Agent(name.to_owned()),
                            label: name.to_owned(),
                            hint: a["mode"]
                                .as_str()
                                .map(|m| format!("{} · {}", a["source"].as_str().unwrap_or(""), m))
                                .unwrap_or_else(|| a["source"].as_str().unwrap_or("").to_owned()),
                            category: if name == current { "current" } else { "agent" }.into(),
                            reason: None,
                        },
                    ));
                }
            }
            PaletteMode::Turns => {
                for (i, b) in self.ui.timeline_blocks.iter().enumerate() {
                    if b.kind != crate::timeline::BlockKind::Turn {
                        continue;
                    }
                    let n = b.turn.map_or(0, |t| t + 1);
                    let label = format!("#{n}");
                    if let Some(score) = fuzzy(&q, &label) {
                        scored.push((
                            score,
                            i,
                            PaletteItem {
                                kind: PaletteKind::Block(i),
                                label,
                                hint: String::new(),
                                category: "turn".into(),
                                reason: None,
                            },
                        ));
                    }
                }
            }
            PaletteMode::History => {
                let entries = self.composer_ref().history_entries();
                let mut seen = std::collections::HashSet::new();
                // Newest first; the same prompt sent twice is listed once.
                for (i, text) in entries.iter().rev().enumerate() {
                    if text.trim().is_empty() || !seen.insert(text.as_str()) {
                        continue;
                    }
                    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
                    let Some(score) = fuzzy(&q, &flat) else {
                        continue;
                    };
                    let lines = text.lines().count();
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::History(text.clone()),
                            label: flat,
                            hint: if lines > 1 {
                                format!("{lines} lines")
                            } else {
                                String::new()
                            },
                            category: "prompt".into(),
                            reason: None,
                        },
                    ));
                }
            }
            PaletteMode::Export => {
                for (i, (format, scope, label)) in [
                    ("markdown", "session", "Markdown transcript"),
                    ("json", "session", "JSON (messages and events)"),
                    ("markdown", "tree", "Markdown transcript, whole tree"),
                    ("json", "tree", "JSON, whole tree"),
                ]
                .into_iter()
                .enumerate()
                {
                    let Some(score) = fuzzy(&q, label) else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Export(format.into(), scope.into()),
                            label: label.into(),
                            hint: scope.into(),
                            category: "export".into(),
                            reason: None,
                        },
                    ));
                }
            }
            PaletteMode::LearnScope => {
                for (i, (scope, label, hint)) in [
                    ("project", "This project", ".agents/skills"),
                    ("global", "Every project", "~/.agents/skills"),
                ]
                .into_iter()
                .enumerate()
                {
                    let Some(score) = fuzzy(&q, label) else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Learn(scope.into()),
                            label: label.into(),
                            hint: hint.into(),
                            category: "skills".into(),
                            reason: None,
                        },
                    ));
                }
            }
            PaletteMode::Efforts => {
                let mut opts: Vec<String> = Vec::new();
                let in_use = self.effective_model(self.viewed());
                if let Some(m) = self
                    .state
                    .models
                    .iter()
                    .find(|m| m["ref"].as_str() == Some(in_use.as_str()))
                {
                    for e in m["efforts"].as_array().into_iter().flatten() {
                        if let Some(e) = e.as_str() {
                            opts.push(e.to_owned());
                        }
                    }
                }
                if opts.is_empty() {
                    opts = ["low", "medium", "high"]
                        .iter()
                        .map(|s| s.to_string())
                        .collect();
                }
                opts.push(String::new()); // clear: follow the agent/provider default
                let current = self.current_effort();
                for (i, e) in opts.into_iter().enumerate() {
                    let label = if e.is_empty() {
                        "(default)".to_owned()
                    } else {
                        e.clone()
                    };
                    let Some(score) = fuzzy(&q, &label) else {
                        continue;
                    };
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Effort(e.clone()),
                            label,
                            hint: String::new(),
                            category: if e == current { "current" } else { "effort" }.into(),
                            reason: None,
                        },
                    ));
                }
            }
        }
        if matches!(p.mode, PaletteMode::Models | PaletteMode::History) {
            // favorites first, then newest first, as Core lists them; the
            // filter only removes rows
            scored.sort_by_key(|(_, i, _)| *i);
        } else if q.is_empty() {
            // manifest order, enabled commands first; the command palette is
            // grouped (D13), each group in its fixed place
            let grouped = p.mode == PaletteMode::Commands;
            scored.sort_by_key(|(_, i, it)| {
                (
                    if grouped {
                        crate::paint::palette_group_rank(&it.category)
                    } else {
                        0
                    },
                    it.reason.is_some(),
                    *i,
                )
            });
        } else {
            scored.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        }
        scored.into_iter().map(|(_, _, it)| it).collect()
    }

    /// Ask Core which sessions' user messages match the switcher's query, for
    /// the rows the title filter cannot find.
    fn ask_search(&mut self) {
        let Some(Overlay {
            kind: OverlayKind::Palette(p),
            ..
        }) = &self.ui.overlay
        else {
            return;
        };
        if p.mode != PaletteMode::Sessions {
            return;
        }
        let query = p.query.text().trim().to_owned();
        if query.chars().count() < 2 {
            self.ui.search = None;
            return;
        }
        let project = if self.ui.scope_all {
            None
        } else {
            self.current_project()
        };
        self.api(ApiCall::Search { query, project });
    }

    fn palette_move(&mut self, delta: isize) {
        let n = self.palette_items().len();
        if let Some(Overlay {
            kind: OverlayKind::Palette(p),
            ..
        }) = self.ui.overlay.as_mut()
        {
            p.selected = if n == 0 {
                0
            } else {
                (p.selected as isize + delta).clamp(0, n as isize - 1) as usize
            };
        }
    }

    fn favorite_models(&self) -> Vec<String> {
        self.state.config.as_ref().map_or_else(Vec::new, |c| {
            c["values"]["ui"]["favorite_models"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default()
        })
    }

    /// Star or unstar the highlighted model in the picker (`ui.favorite_models`).
    fn toggle_favorite_model(&mut self) {
        let Some(sel) = (match &self.ui.overlay {
            Some(Overlay {
                kind: OverlayKind::Palette(p),
                ..
            }) => Some(p.selected),
            _ => None,
        }) else {
            return;
        };
        let Some(PaletteKind::Model(r)) = self.palette_items().get(sel).map(|i| i.kind.clone())
        else {
            return;
        };
        let Some(revision) = self
            .state
            .config
            .as_ref()
            .and_then(|c| c["revision"].as_str().map(str::to_owned))
        else {
            return;
        };
        if r.is_empty() {
            return;
        }
        let mut favorites = self.favorite_models();
        if let Some(at) = favorites.iter().position(|f| *f == r) {
            favorites.remove(at);
        } else {
            favorites.push(r);
        }
        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
            revision,
            path: "ui.favorite_models".into(),
            value: serde_json::Value::Array(
                favorites
                    .into_iter()
                    .map(serde_json::Value::String)
                    .collect(),
            ),
        }));
    }

    fn palette_activate(&mut self, index: Option<usize>) {
        let items = self.palette_items();
        let sel = match (&self.ui.overlay, index) {
            (_, Some(i)) => i,
            (
                Some(Overlay {
                    kind: OverlayKind::Palette(p),
                    ..
                }),
                None,
            ) => p.selected,
            _ => return,
        };
        let Some(item) = items.get(sel).cloned() else {
            return;
        };
        if let Some(reason) = &item.reason {
            // disabled commands explain themselves and keep the palette open
            self.toast(Level::Warning, format!("{}: {reason}", item.label));
            return;
        }
        self.close_overlay();
        match item.kind {
            PaletteKind::Command(id) => self.execute(&id),
            PaletteKind::Provider(id, sign_out) => {
                if id == "opencode" && !sign_out {
                    let restore = self.restore_point();
                    self.ui.edit = Some(TextEdit {
                        label: "OpenCode API key".into(),
                        target: EditTarget::ProviderKey { id },
                        buf: Composer::new(),
                        restore,
                    });
                } else {
                    let method = self
                        .state
                        .providers
                        .iter()
                        .find(|p| p["id"].as_str() == Some(id.as_str()))
                        .and_then(|p| p["methods"].as_array())
                        .and_then(|m| m.first())
                        .and_then(|v| v.as_str())
                        .unwrap_or("device")
                        .to_owned();
                    self.api(ApiCall::Provider {
                        id,
                        sign_out,
                        method,
                        key: None,
                    });
                }
            }
            PaletteKind::Session(id) => self.open_session(&id),
            PaletteKind::Project(id) => {
                // The project picker is also the restore action for an inactive project.
                if self.project_status(&id) == crate::api::project_status::INACTIVE {
                    self.set_project_status(&id, crate::api::project_status::CURRENT);
                }
                self.select_project(&id)
            }
            PaletteKind::PoolModel(model) => {
                if let Some((agent, fallback)) = self.ui.pool_pick.take() {
                    let mut refs = self.agent_refs(&agent, fallback);
                    refs.push(model);
                    self.patch_agent_refs(&agent, fallback, refs);
                }
            }
            PaletteKind::Model(model) => {
                if let Some(s) = self.viewed() {
                    let (session, revision) = (s.id.clone(), s.revision);
                    if self.writable() {
                        self.api(ApiCall::SwitchCost {
                            session,
                            model: Some(model),
                            agent: None,
                            revision,
                        });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                } else {
                    self.ui.draft.model = Some(model);
                }
            }
            PaletteKind::Agent(agent) => self.apply_agent(agent),
            PaletteKind::Block(i) => self.select_block(i),
            PaletteKind::History(text) => {
                if self.question_pending() {
                    self.toast(Level::Info, QUESTION_LOCK);
                } else {
                    self.composer().set_text(&text);
                    self.ui.focus = Focus::Composer;
                }
            }
            PaletteKind::Learn(scope) => {
                if let (Some(source), Some(project)) =
                    (self.ui.learn_source.take(), self.current_project())
                {
                    self.toast(Level::Info, format!("Fetching {source}…"));
                    self.api(ApiCall::LearnPreview {
                        project,
                        source,
                        scope,
                    });
                }
            }
            PaletteKind::Export(format, scope) => {
                if let Some(s) = self.viewed() {
                    let session = s.id.clone();
                    if self.writable() {
                        self.api(ApiCall::Export {
                            session,
                            format,
                            scope,
                        });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                }
            }
            PaletteKind::Effort(effort) => {
                if let Some(s) = self.viewed() {
                    let (session, revision) = (s.id.clone(), s.revision);
                    if self.writable() {
                        self.api(ApiCall::SetEffort {
                            session,
                            effort,
                            revision,
                        });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                } else {
                    self.ui.draft.effort = Some(effort);
                }
            }
        }
    }

    /// Change how much activity the timeline shows. Blocks the reader opened
    /// or closed by hand follow the new level again.
    pub fn set_detail(&mut self, detail: crate::ui::Detail) {
        self.ui.detail = detail;
        if let Some(revision) = self
            .state
            .config
            .as_ref()
            .and_then(|c| c["revision"].as_str())
        {
            self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                revision: revision.to_owned(),
                path: "ui.detail".into(),
                value: serde_json::Value::String(detail.label().into()),
            }));
        }
        self.ui
            .timeline_open
            .retain(|key, _| !(key.starts_with("group:") || key.starts_with("tool:")));
        self.toast(Level::Info, format!("Detail: {}", detail.label()));
    }

    /// The agent of the viewed session, else the one chosen for the next.
    pub fn current_agent(&self) -> String {
        self.viewed().map_or_else(
            || {
                // Core's plan knows the configured default agent; the client does not.
                self.ui
                    .draft
                    .agent
                    .clone()
                    .or_else(|| self.draft_plan("agent"))
                    .unwrap_or_else(|| "build".into())
            },
            |s| s.agent.clone(),
        )
    }

    /// Whether the agent can own the composer's context: a child session chooses subagents,
    /// every other session and a draft choose primary agents.
    fn agent_eligible(&self, agent: &Value) -> bool {
        let mode = if self.viewed().is_some_and(|s| s.parent_id.is_some()) {
            "subagent"
        } else {
            "primary"
        };
        matches!(agent["mode"].as_str(), Some("all")) || agent["mode"].as_str() == Some(mode)
    }

    /// Shift+Tab: the next eligible agent in catalog order, wrapping at the end.
    fn cycle_agent(&mut self) {
        // The composer's shortcut never reaches behind a picker, modal, inline edit or the
        // completion list; those own the keys while they are up.
        if self.ui.overlay.is_some()
            || self.ui.dialog.is_some()
            || self.ui.edit.is_some()
            || self.completion_rows().is_some()
        {
            return;
        }
        let names: Vec<String> = self
            .state
            .agents
            .iter()
            .filter(|agent| self.agent_eligible(agent))
            .filter_map(|agent| agent["name"].as_str().map(str::to_owned))
            .collect();
        if names.is_empty() {
            return;
        }
        let current = self.current_agent();
        let next = names
            .iter()
            .position(|name| *name == current)
            .map_or(0, |at| (at + 1) % names.len());
        if names[next] != current {
            self.apply_agent(names[next].clone());
        }
    }

    /// Choose an agent exactly as the picker does: a session switches from the next model
    /// call (asking first when the cache would go cold); a draft's next session uses it.
    fn apply_agent(&mut self, agent: String) {
        if let Some(s) = self.viewed() {
            let (session, revision) = (s.id.clone(), s.revision);
            if self.writable() {
                self.api(ApiCall::SwitchCost {
                    session,
                    model: None,
                    agent: Some(agent),
                    revision,
                });
            } else {
                self.toast(Level::Warning, READ_ONLY);
            }
        } else {
            self.ui.draft.agent = Some(agent);
        }
    }

    /// The effort of the viewed session, else the one chosen for the next;
    /// empty when it follows the agent's or provider's default.
    pub fn current_effort(&self) -> String {
        self.viewed().map_or_else(
            || {
                self.ui
                    .draft
                    .effort
                    .clone()
                    .or_else(|| self.draft_session_field("effort"))
                    .unwrap_or_default()
            },
            |s| {
                s.extra
                    .get("effort")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned()
            },
        )
    }

    /// The model reference a session runs with: its own, else the agent
    /// file's. A draft with neither shows the last model selected anywhere.
    /// Empty when none was ever chosen: the interface asks for one.
    pub fn effective_model(&self, session: Option<&crate::api::Session>) -> String {
        if session.is_none() {
            if let Some(m) = self.ui.draft.model.as_deref().filter(|m| !m.is_empty()) {
                return m.to_owned();
            } // What the session will be created with, including the model remembered for this agent.
            if let Some(m) = self.draft_session_field("model") {
                return m;
            }
        }
        let agent = session.map_or_else(
            || {
                self.ui
                    .draft
                    .agent
                    .clone()
                    .unwrap_or_else(|| "build".into())
            },
            |s| s.agent.clone(),
        );
        if let Some(m) = session
            .and_then(|s| s.model.as_deref())
            .filter(|m| !m.is_empty())
        {
            return m.to_owned();
        }
        self.state
            .agents
            .iter()
            .find(|a| a["name"].as_str() == Some(agent.as_str()))
            .and_then(|a| a["model"].as_str())
            .filter(|m| !m.is_empty())
            .map_or_else(
                || {
                    if session.is_none() {
                        self.state.last_model.clone()
                    } else {
                        String::new()
                    }
                },
                str::to_owned,
            )
    }

    /// Open a picker and ask the supervisor for fresh catalog data.
    fn open_picker(&mut self, mode: PaletteMode) {
        let project = self.current_project();
        self.effects
            .push(Effect::Sync(SyncCommand::Catalog(project)));
        self.open_palette(mode);
    }

    // ---- actions ----------------------------------------------------------------------

    /// Run a command, explaining when it is disabled.
    pub fn run_command(&mut self, id: &str) {
        let Some(c) = self.manifest.command(id).cloned() else {
            return;
        };
        match self.availability(&c) {
            Ok(()) => self.execute(id),
            Err(reason) => self.toast(Level::Warning, format!("{}: {reason}", c.title)),
        }
    }

    fn api(&mut self, call: ApiCall) {
        self.effects.push(Effect::Api(call));
    }

    fn refresh_voice_settings_status(&mut self) {
        self.ui.voice_status_generation = self.ui.voice_status_generation.wrapping_add(1);
        self.ui.voice_status_error = None;
        self.ui.voice_status_loading = true;
        self.api(ApiCall::VoiceSettingsStatus {
            generation: self.ui.voice_status_generation,
        });
    }

    fn voice_status_for(&self, target: VoiceTarget) -> Option<&Value> {
        self.ui.voice_status.as_ref()?.get(target.id())
    }

    /// Why a model action cannot run in the current status. Settings and the
    /// command palette share this check so they cannot disagree about state.
    pub fn voice_action_reason(&self, row_or_command: &str) -> Option<String> {
        let (target, operation) = voice_action_spec(row_or_command)?;
        if self.ui.voice_status_loading {
            return Some("Voice status is refreshing; try again when it finishes.".into());
        }
        if let Some(error) = self.ui.voice_status_error.as_deref() {
            return Some(format!("Voice status unavailable: {error}"));
        }
        let Some(status) = self.voice_status_for(target) else {
            return Some("Voice status is loading; open Settings to refresh it.".into());
        };
        let state = status["state"].as_str().unwrap_or("unknown");
        match (operation, state) {
            (VoiceOperation::Download, "ready" | "loaded") => {
                Some("The model is already available in the Hugging Face cache.".into())
            }
            (VoiceOperation::Download, "downloading") => {
                Some("The model download is already in progress.".into())
            }
            (VoiceOperation::Remove, "absent") => {
                Some("The model is not present in the Hugging Face cache.".into())
            }
            (VoiceOperation::Remove, "downloading") => {
                Some("The model cannot be removed while it is downloading.".into())
            }
            _ => None,
        }
    }

    fn voice_command_disabled_reason(&self, command: &str) -> Option<String> {
        if command == "search.start" {
            return self.search_start_reason();
        }
        self.voice_action_reason(command)
    }

    fn refresh_search_service(&mut self) {
        self.api(ApiCall::SearchServiceStatus);
    }

    /// Why the search service cannot be started now. Settings and the palette share it.
    pub fn search_start_reason(&self) -> Option<String> {
        if self.ui.search_starting {
            return Some("The search service is starting.".into());
        }
        let status = self.ui.search_service.as_ref()?;
        match status["state"].as_str() {
            Some("running") => Some("The search service is already running.".into()),
            Some("unavailable") => Some(
                status["detail"]
                    .as_str()
                    .unwrap_or("The search service cannot be started here.")
                    .to_owned(),
            ),
            _ => None,
        }
    }

    /// Lines for the search status row: state, address, detail, runtime.
    pub fn search_status_lines(&self) -> Vec<String> {
        let Some(status) = self.ui.search_service.as_ref() else {
            return vec![if self.ui.search_service_error.is_some() {
                "status unavailable".into()
            } else {
                "loading status…".into()
            }];
        };
        let url = status["url"].as_str().unwrap_or("").to_owned();
        if self.ui.search_starting {
            return vec!["starting…".into(), url];
        }
        let label = match status["state"].as_str() {
            Some("running") => "running",
            Some("stopped") => "stopped",
            Some("absent") => "not set up",
            _ => "unavailable",
        };
        let mut lines = vec![label.to_owned(), url];
        if let Some(detail) = status["detail"].as_str() {
            lines.push(detail.to_owned());
        }
        if let Some(runtime) = status["runtime"].as_str() {
            lines.push(format!("runtime {runtime}"));
        }
        lines
    }

    fn apply_search_service(&mut self, call: ApiCall, result: Result<Done, String>) {
        let starting = call == ApiCall::SearchServiceStart;
        if starting {
            self.ui.search_starting = false;
        }
        match result {
            Ok(done) => {
                self.ui.search_service = done.search_service;
                self.ui.search_service_error = None;
                if starting {
                    self.toast(Level::Success, "Web search is running");
                }
            }
            Err(error) => {
                self.ui.search_service_error = Some(error.clone());
                if starting {
                    self.toast(Level::Error, format!("Web search did not start: {error}"));
                    self.refresh_search_service();
                }
            }
        }
    }

    fn start_search_service(&mut self) {
        if let Some(reason) = self.search_start_reason() {
            self.toast(Level::Warning, reason);
            return;
        }
        self.ui.search_starting = true;
        self.api(ApiCall::SearchServiceStart);
    }

    /// Compact status shown on the voice model's Settings row.
    pub fn voice_info_value(&self, target: VoiceTarget) -> String {
        let Some(status) = self.voice_status_for(target) else {
            return if self.ui.voice_status_error.is_some() {
                "status unavailable".into()
            } else {
                "loading status…".into()
            };
        };
        let state = status["state"].as_str().unwrap_or("unknown");
        let size = status["size"].as_u64().map(format_bytes);
        let progress = if state == "downloading" {
            let done = status["bytes_done"].as_u64().unwrap_or(0);
            let total = status["bytes_total"]
                .as_u64()
                .or_else(|| status["size"].as_u64())
                .unwrap_or(0);
            (total > 0).then(|| format!(" · {}%", (done.saturating_mul(100) / total).min(100)))
        } else {
            None
        };
        format!(
            "{}{}{}",
            state,
            progress.unwrap_or_default(),
            size.map_or_else(String::new, |size| format!(" · {size}")),
        )
    }

    /// Full selected-row details. The renderer wraps these onto the reserved
    /// detail lines so repository and cache paths remain inspectable.
    pub fn voice_info_details(&self, target: VoiceTarget) -> Vec<String> {
        let Some(status) = self.voice_status_for(target) else {
            return vec![self.ui.voice_status_error.as_ref().map_or_else(
                || "Waiting for GET /api/voice…".into(),
                |error| format!("Voice status unavailable: {error}"),
            )];
        };
        let text = |key: &str| {
            status[key]
                .as_str()
                .filter(|value| !value.is_empty())
                .unwrap_or("—")
        };
        let size = status["size"]
            .as_u64()
            .map_or_else(|| "—".to_owned(), format_bytes);
        let mut lines = vec![
            format!("State: {}", text("state")),
            format!("Repo: {}", text("repo")),
            format!("Size: {size}"),
            format!("Cache path: {}", text("cache_path")),
        ];
        if let Some(error) = status["error"].as_str().filter(|error| !error.is_empty()) {
            lines.push(format!("Error: {error}"));
        }
        if let Some(error) = self.ui.voice_status_error.as_deref() {
            lines.push(format!("Status refresh error: {error}"));
        }
        lines
    }

    fn ask_voice_model_download(&mut self, target: VoiceTarget) {
        if let Some(reason) = self.voice_action_reason(match target {
            VoiceTarget::Stt => "voice.download_stt",
            VoiceTarget::Tts => "voice.download_tts",
        }) {
            self.toast(Level::Warning, reason);
            return;
        }
        let status = self
            .voice_status_for(target)
            .cloned()
            .unwrap_or(Value::Null);
        let (repo, fallback_size) = voice_defaults(target);
        let repo = status["repo"].as_str().unwrap_or(repo).to_owned();
        let bytes = status["size"].as_u64().unwrap_or(fallback_size);
        self.voice_download_consent(target, repo, bytes, Confirmed::DownloadVoice(target));
    }

    fn voice_download_consent(
        &mut self,
        target: VoiceTarget,
        repo: String,
        bytes: u64,
        action: Confirmed,
    ) {
        self.ui.dialog = Some(Dialog {
            title: format!("Download {} model?", target.label()),
            body: vec![
                format!("Repository: {repo}"),
                format!("Download size: approximately {}", format_bytes(bytes)),
                "Downloads from Hugging Face into the standard cache.".into(),
            ],
            cancel: "Cancel".into(),
            confirm: "Download".into(),
            danger: false,
            action,
            button: 0,
            restore: self.restore_point(),
        });
    }

    fn ask_voice_model_remove(&mut self, target: VoiceTarget) {
        if let Some(reason) = self.voice_action_reason(match target {
            VoiceTarget::Stt => "voice.remove_stt",
            VoiceTarget::Tts => "voice.remove_tts",
        }) {
            self.toast(Level::Warning, reason);
            return;
        }
        let (default_repo, _) = voice_defaults(target);
        let repo = self
            .voice_status_for(target)
            .and_then(|status| status["repo"].as_str())
            .unwrap_or(default_repo)
            .to_owned();
        self.ui.dialog = Some(Dialog {
            title: "Remove voice model?".into(),
            body: vec![format!(
                "Only {repo} is removed from the Hugging Face cache."
            )],
            cancel: "Cancel".into(),
            confirm: "Remove".into(),
            danger: true,
            action: Confirmed::RemoveVoice { target, repo },
            button: 0,
            restore: self.restore_point(),
        });
    }

    fn begin_dictation(&mut self) {
        if self.ui.dictation.phase != crate::dictation::Phase::Idle {
            return;
        }
        if !self.writable() {
            self.toast(Level::Warning, READ_ONLY);
            return;
        }
        let generation = self.ui.dictation.begin(self.draft_key());
        self.ui.dictation.anchor = self.composer_ref().before_cursor().len();
        self.api(ApiCall::VoiceStatus { generation });
    }

    fn start_capture(&mut self, loaded: bool) {
        if self.ui.dictation.phase != crate::dictation::Phase::Starting {
            let Some(phase) = self
                .ui
                .dictation
                .phase
                .transition(crate::dictation::Transition::StatusReady)
            else {
                return;
            };
            self.ui.dictation.phase = phase;
        }
        self.prepare_capture(loaded);
    }

    fn prepare_capture(&mut self, loaded: bool) {
        self.ui.dictation.download = None;
        if !loaded {
            self.effects.push(Effect::DictationLoadingCue(true));
        }
        self.api(ApiCall::VoicePrepare {
            generation: self.ui.dictation.generation,
        });
    }

    fn launch_capture(&mut self) {
        self.effects
            .push(Effect::Dictation(crate::dictation::CaptureCommand::Start {
                generation: self.ui.dictation.generation,
            }));
    }

    fn stop_dictation(&mut self) {
        self.stop_dictation_with(false);
    }

    /// Stop capturing; `send` sends the draft once the final words are in.
    fn stop_dictation_with(&mut self, send: bool) {
        let Some(phase) = self
            .ui
            .dictation
            .phase
            .transition(crate::dictation::Transition::Stop)
        else {
            return;
        };
        self.ui.dictation.phase = phase;
        self.ui.dictation.send_after = send;
        self.effects
            .push(Effect::Dictation(crate::dictation::CaptureCommand::Stop {
                generation: self.ui.dictation.generation,
            }));
    }

    fn cancel_dictation(&mut self) {
        let generation = self.ui.dictation.generation;
        self.effects.push(Effect::DictationLoadingCue(false));
        if matches!(
            self.ui.dictation.phase,
            crate::dictation::Phase::Starting
                | crate::dictation::Phase::Recording
                | crate::dictation::Phase::Stopping
        ) {
            self.effects.push(Effect::Dictation(
                crate::dictation::CaptureCommand::Cancel { generation },
            ));
        }
        self.ui.dictation.cancel();
    }

    fn maybe_preview(&mut self) {
        if self.ui.dictation.phase == crate::dictation::Phase::Recording
            && self.ui.dictation.pacer.begin(self.ui.dictation.seconds)
        {
            self.effects.push(Effect::Dictation(
                crate::dictation::CaptureCommand::Preview {
                    generation: self.ui.dictation.generation,
                },
            ));
        }
    }

    fn send_final_if_ready(&mut self) {
        if !self.ui.dictation.pacer.in_flight() {
            if let Some(wav) = self.ui.dictation.final_wav.take() {
                let generation = self.ui.dictation.generation;
                self.api(ApiCall::VoiceTranscribe {
                    wav,
                    partial: false,
                    generation,
                });
            }
        }
    }

    fn ask_voice_download(&mut self, status: &Value) {
        let (default_repo, fallback_size) = voice_defaults(VoiceTarget::Stt);
        let repo = status["repo"].as_str().unwrap_or(default_repo).to_owned();
        let bytes = status["size"].as_u64().unwrap_or(fallback_size);
        self.ui.dictation.download = Some(crate::dictation::DownloadProgress {
            state: "consent".into(),
            bytes_done: 0,
            bytes_total: bytes,
            error: None,
        });
        self.voice_download_consent(VoiceTarget::Stt, repo, bytes, Confirmed::DownloadStt);
    }

    fn start_downloaded_capture(&mut self) {
        let Some(phase) = self
            .ui
            .dictation
            .phase
            .transition(crate::dictation::Transition::DownloadReady)
        else {
            return;
        };
        self.ui.dictation.phase = phase;
        self.prepare_capture(false);
    }

    fn apply_voice_progress(&mut self, data: &Value) {
        let state = data["state"].as_str().unwrap_or("downloading");
        let target = data
            .get("target")
            .and_then(Value::as_str)
            .filter(|target| matches!(*target, "stt" | "tts"))
            .or_else(|| {
                data.get("model")
                    .and_then(Value::as_str)
                    .filter(|model| matches!(*model, "stt" | "tts"))
            });
        let Some(target) = target else { return };
        let bytes_done = data["bytes_done"].as_u64().unwrap_or(0);
        let bytes_total = data["bytes_total"].as_u64().unwrap_or(0);
        let error = data["error"].as_str().map(str::to_owned);
        if let Some(status) = self
            .ui
            .voice_status
            .as_mut()
            .and_then(|voice| voice.get_mut(target))
            .and_then(Value::as_object_mut)
        {
            status.insert("state".into(), Value::String(state.to_owned()));
            status.insert("bytes_done".into(), Value::from(bytes_done));
            status.insert("bytes_total".into(), Value::from(bytes_total));
            status.insert(
                "error".into(),
                error.clone().map_or(Value::Null, Value::String),
            );
            self.ui.voice_status_error = None;
        }
        if (state == "ready" || state == "error") && self.ui.view == View::Settings {
            self.refresh_voice_settings_status();
        }
        if target != "stt" {
            return;
        }
        if state == "ready" && self.ui.dictation.phase == crate::dictation::Phase::Checking {
            // The status GET can race the completion event: honor a ready event
            // seen while this dictation's status request is pending.
            self.start_capture(false);
            return;
        }
        if self.ui.dictation.phase != crate::dictation::Phase::Downloading {
            return;
        }
        let progress = crate::dictation::DownloadProgress {
            state: state.to_owned(),
            bytes_done,
            bytes_total,
            error,
        };
        match state {
            "ready" => self.start_downloaded_capture(),
            "error" => {
                let message = progress
                    .error
                    .unwrap_or_else(|| "Voice model download failed".into());
                self.ui.dictation.download = None;
                if let Some(phase) = self
                    .ui
                    .dictation
                    .phase
                    .transition(crate::dictation::Transition::Failed)
                {
                    self.ui.dictation.phase = phase;
                }
                self.toast(Level::Error, message);
            }
            _ => self.ui.dictation.download = Some(progress),
        }
    }

    pub fn apply_capture_event(&mut self, event: crate::dictation::CaptureEvent) {
        use crate::dictation::{CaptureEvent, Phase, Transition};
        let manager_generation = match &event {
            CaptureEvent::Started { generation }
            | CaptureEvent::Cancelled { generation }
            | CaptureEvent::Failed { generation, .. }
            | CaptureEvent::CueWarning { generation, .. }
            | CaptureEvent::Level { generation, .. }
            | CaptureEvent::Preview { generation, .. }
            | CaptureEvent::Stopped { generation, .. } => *generation,
        };
        if manager_generation >= crate::manager::CAPTURE_BASE {
            return self.manager_capture(event);
        }
        let generation = match &event {
            CaptureEvent::Started { generation }
            | CaptureEvent::Cancelled { generation }
            | CaptureEvent::Failed { generation, .. }
            | CaptureEvent::CueWarning { generation, .. }
            | CaptureEvent::Level { generation, .. }
            | CaptureEvent::Preview { generation, .. }
            | CaptureEvent::Stopped { generation, .. } => *generation,
        };
        if generation != self.ui.dictation.generation {
            return;
        }
        match event {
            CaptureEvent::Started { .. } => {
                if let Some(phase) = self
                    .ui
                    .dictation
                    .phase
                    .transition(Transition::CaptureStarted)
                {
                    self.ui.dictation.phase = phase;
                }
            }
            CaptureEvent::Level { seconds, level, .. } => {
                if self.ui.dictation.phase == Phase::Recording {
                    self.ui.dictation.add_level(seconds, level);
                    self.maybe_preview();
                }
            }
            CaptureEvent::Preview { wav, .. } => {
                if matches!(self.ui.dictation.phase, Phase::Recording | Phase::Stopping) {
                    self.api(ApiCall::VoiceTranscribe {
                        wav,
                        partial: true,
                        generation,
                    });
                }
            }
            CaptureEvent::Stopped { seconds, wav, .. } => {
                if self.ui.dictation.phase == Phase::Recording {
                    if let Some(phase) = self.ui.dictation.phase.transition(Transition::Stop) {
                        self.ui.dictation.phase = phase;
                    }
                }
                if self.ui.dictation.phase != Phase::Stopping {
                    return;
                }
                if seconds < crate::dictation::MIN_SECONDS {
                    self.ui.dictation.phase = Phase::Idle;
                    self.ui.dictation.preview.clear();
                    self.toast(Level::Info, "Speak for at least 0.2 seconds to dictate");
                } else {
                    if let Some(phase) =
                        self.ui.dictation.phase.transition(Transition::AudioStopped)
                    {
                        self.ui.dictation.phase = phase;
                    }
                    self.ui.dictation.final_wav = Some(wav);
                    self.send_final_if_ready();
                }
            }
            CaptureEvent::Cancelled { .. } => self.cancel_dictation(),
            CaptureEvent::CueWarning { message, .. } => self.toast(Level::Warning, message),
            CaptureEvent::Failed { message, .. } => {
                self.ui.dictation.cancel();
                self.toast(Level::Error, message);
            }
        }
    }

    /// The header's tabs: the sessions opened so far that still exist, and the
    /// viewed one if it was reached some other way.
    /// Subagent sessions have no tab: they live in the strip under the tabs, and a
    /// viewed one keeps its root's tab active.
    pub fn tab_ids(&self) -> Vec<String> {
        let mut ids: Vec<String> = self
            .ui
            .tabs
            .iter()
            .filter(|id| {
                self.state.model.sessions.get(id.as_str()).is_some_and(|s| {
                    s.parent_id.is_none() && Some(&s.project_id) == self.ui.project.as_ref()
                })
            })
            .cloned()
            .collect();
        if let Some(id) = self.viewed_root() {
            if !ids.contains(&id)
                && self
                    .state
                    .model
                    .sessions
                    .get(&id)
                    .is_some_and(|s| Some(&s.project_id) == self.ui.project.as_ref())
            {
                ids.push(id);
            }
        }
        ids
    }

    /// The root of the viewed session's tree (itself when it has no parent).
    pub fn viewed_root(&self) -> Option<String> {
        self.viewed_id().and_then(|id| self.root_session(id))
    }

    /// Every session below `root`, oldest first: the strip's entries.
    pub fn subagents_of(&self, root: &str) -> Vec<&Session> {
        let mut family: Vec<&Session> = self
            .state
            .model
            .sessions
            .values()
            .filter(|s| s.id != root && self.is_descendant(&s.id, root))
            .collect();
        family.sort_by(|a, b| a.created.total_cmp(&b.created));
        family
    }

    /// Close a tab (the session stays); the neighbouring tab, or the new
    /// session screen, takes over when it was the one in view.
    pub fn close_tab(&mut self, id: &str) {
        let ids = self.tab_ids();
        let Some(at) = ids.iter().position(|t| t == id) else {
            return;
        };
        self.ui.tabs.retain(|t| t != id);
        if self.viewed_root().as_deref() != Some(id) {
            return;
        }
        let next = ids
            .get(at + 1)
            .or_else(|| at.checked_sub(1).and_then(|i| ids.get(i)));
        match next.cloned() {
            Some(next) => self.open_session(&next),
            None => self.show_new_session(),
        }
    }

    /// Close the tab of every archived session in the tree, including a root
    /// archived together with its descendants. Driven by the model after an
    /// archive event or a snapshot, so every source of archiving closes tabs
    /// (this client, another client, the Manager and Core's auto-archive).
    fn session_tree_archived(&self, id: &str) -> bool {
        self.state
            .model
            .sessions
            .get(id)
            .is_some_and(|s| self.ancestors_of(&s.id).iter().any(|a| a.archived))
    }

    /// Archived tabs among every stored project, limited to the tabs `include`
    /// accepts. The visible check covers a viewed root reached without a tab.
    fn archived_tabs_where(&self, include: impl Fn(&str) -> bool) -> Vec<String> {
        let visible = self.tab_ids();
        self.ui
            .tabs
            .iter()
            .chain(visible.iter())
            .filter(|id| self.session_tree_archived(id) && include(id))
            .cloned()
            .collect()
    }

    /// Close `doomed` tabs everywhere, then move off the viewed one to the first
    /// live visible neighbour (following, then preceding), or the empty state.
    fn close_tabs(&mut self, doomed: &[String]) {
        if doomed.is_empty() {
            return;
        }
        let visible = self.tab_ids();
        let viewed = self.viewed_root();
        let next = viewed.as_ref().and_then(|id| {
            if !doomed.contains(id) {
                return None;
            }
            let at = visible.iter().position(|tab| tab == id)?;
            // A snapshot can close several roots at once: take the first live tab.
            visible
                .iter()
                .skip(at + 1)
                .chain(visible.iter().take(at).rev())
                .find(|tab| !doomed.contains(tab))
                .cloned()
                .map(Some)
                .or(Some(None))
        });
        self.ui.tabs.retain(|id| !doomed.contains(id));
        let Some(next) = next else {
            return;
        };
        match next {
            Some(next) => self.open_session(&next),
            None => self.show_new_session(),
        }
    }

    /// Show the new-session screen: no session is created until a message is sent.
    fn show_new_session(&mut self) {
        if let Some(project) = &self.ui.project {
            self.ui
                .last_project_session
                .insert(project.clone(), String::new());
        }
        let from = self
            .scroll_session
            .clone()
            .unwrap_or_else(|| self.draft_key());
        self.ui.scrolls.insert(from, self.ui.scroll);
        self.state.model.select(None);
        self.effects.push(Effect::Sync(SyncCommand::Select(None)));
        self.ui.view = View::Session;
        self.ui.file_open = false;
        self.ui.file_edit = None;
        self.ui.diff_open = None;
        self.ui.draft = Draft::default();
        self.ui.timeline_sel = None;
        self.ui.timeline_sel_key = None;
        self.ui.context_cursor = 0;
        self.ui.scroll = Scroll {
            follow: false,
            top: 0,
        };
        self.scroll_session = None;
    }

    fn step_tab(&mut self, step: isize) {
        let ids = self.tab_ids();
        let Some(at) = self
            .viewed_root()
            .and_then(|id| ids.iter().position(|t| *t == id))
        else {
            return;
        };
        let to = (at as isize + step).rem_euclid(ids.len() as isize) as usize;
        if to != at {
            let id = ids[to].clone();
            self.open_session(&id);
        }
    }

    /// Unsaved editor text blocks leaving the file: say so and report true.
    fn edit_blocks(&mut self) -> bool {
        let dirty = self.ui.file_edit.as_ref().is_some_and(FileEdit::dirty);
        if dirty {
            self.toast(
                Level::Warning,
                "Unsaved changes in the editor: save (ctrl+s) or discard (esc) first",
            );
        }
        dirty
    }

    /// Close the file or diff panel. Unsaved text asks first; a search ends first.
    fn close_panel(&mut self) {
        if self.terminal_open {
            if self.terminal_panel.input_busy {
                self.toast(
                    Level::Warning,
                    "Terminal input outcome may be indeterminate after detaching",
                );
            }
            self.terminal_open = false;
            self.terminal_panel.generation += 1;
            self.terminal_panel.attachment = None;
            self.terminal_panel.selected = None;
            self.terminal_panel.focus = crate::terminal_panel::TerminalFocus::List;
            self.terminal_focused = false;
            self.terminal_selected = false;
            self.terminal_controlled = false;
            self.ui.focus = Focus::Composer;
            self.terminal_panel.input_queue.clear();
            self.terminal_panel.control_busy = false;
            self.terminal_controlled = false;
            self.terminal_panel.input_queue.clear();
            self.effects.push(Effect::TerminalStop);
            return;
        }
        if self.ui.file_search.take().is_some() {
            if let Some(edit) = &self.ui.file_edit {
                edit.buf.clear_marks();
            }
            return;
        }
        match self.ui.file_edit.as_ref().map(FileEdit::dirty) {
            Some(true) => self.ask_discard_edit(),
            Some(false) | None => {
                self.ui.file_edit = None;
                self.ui.file_open = false;
                self.ui.diff_open = None;
            }
        }
    }

    /// The composer's text area, below the pending inputs, image strip and
    /// collapsed paste cards: the same rect the composer is drawn in, so a
    /// click on a paste card falls through to its hit target.
    fn composer_text_inner(composer: Rect, images: usize, pastes: usize) -> Rect {
        crate::render::composer_inner_with(composer, images, pastes)
    }

    pub fn open_session(&mut self, id: &str) {
        if self.viewed_id() != Some(id) && self.edit_blocks() {
            return;
        }
        if let Some(root) = self.root_session(id) {
            if !self.ui.tabs.contains(&root) {
                self.ui.tabs.push(root.clone());
            }
            self.ui.unread_sessions.remove(&root);
        }
        self.ui.file_open = false;
        self.ui.file_edit = None;
        self.ui.diff_open = None;
        if self.viewed_id() == Some(id) && self.ui.view == View::Session {
            self.scroll_session = Some(id.to_owned());
            self.after_open();
            return;
        }
        let from = self
            .scroll_session
            .clone()
            .unwrap_or_else(|| self.draft_key());
        self.ui.scrolls.insert(from, self.ui.scroll);
        self.state.model.select(Some(id));
        // First, so the history load is not queued behind the other reads.
        self.effects
            .push(Effect::Sync(SyncCommand::Select(Some(id.to_owned()))));
        self.ui.scroll = self.ui.scrolls.get(id).copied().unwrap_or_default();
        self.scroll_session = Some(id.to_owned());
        self.ui.timeline_sel = None;
        self.ui.timeline_sel_key = None;
        self.ui.wait_idx = 0;
        if let Some(s) = self.state.model.sessions.get(id) {
            let project = s.project_id.clone();
            self.ui
                .last_project_session
                .insert(project.clone(), id.to_owned());
            self.ui.project = Some(project.clone());
            if !self.ui.inventories.contains_key(&project) {
                self.effects
                    .push(Effect::Sync(SyncCommand::SkillMcp(project.clone())));
            }
            if !self.state.project_worktrees.contains_key(&s.project_id) {
                self.effects
                    .push(Effect::Sync(SyncCommand::ProjectWorktrees(
                        s.project_id.clone(),
                    )));
            }
        }
        if !self.ui.files_project_root {
            self.reset_files();
        }
        self.refresh_history();
        self.ui.view = View::Session;
        self.effects
            .push(Effect::Sync(SyncCommand::Worktrees(id.to_owned())));
        self.effects
            .push(Effect::Sync(SyncCommand::Git(id.to_owned())));
        if self.state.models.is_empty() {
            self.effects
                .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
        }
        self.after_open();
    }

    fn after_open(&mut self) {
        self.ui.drawer = None;
        self.prepare_questions();
        self.ui.focus = if self.ask_current().is_some() {
            // A question owns the composer's keys until it is answered.
            Focus::Waiting
        } else {
            Focus::Composer
        };
    }

    /// Keep the one timeline scroll with the session it belongs to. `open_session`
    /// already saves and restores it; this catches selection changes made by a
    /// snapshot or a deletion, which would otherwise leave the previous session's
    /// offset and follow flag to pin its user band over the new session.
    fn adopt_view_scroll(&mut self) {
        let now = self.viewed_id().map(str::to_owned);
        if now.as_deref() == self.scroll_session.as_deref() {
            return;
        }
        if let Some(old) = self.scroll_session.take() {
            self.ui.scrolls.insert(old, self.ui.scroll);
            self.ui.scroll = now
                .as_ref()
                .and_then(|id| self.ui.scrolls.get(id).copied())
                .unwrap_or_default();
            self.ui.timeline_sel = None;
            self.ui.timeline_sel_key = None;
        }
        self.scroll_session = now;
    }

    fn select_project(&mut self, id: &str) {
        self.select_project_announcing(id, true);
    }

    /// Select without the "Project: name" toast: a startup or add selection
    /// already has its own notice, or needs none.
    fn select_project_quietly(&mut self, id: &str) {
        self.select_project_announcing(id, false);
    }

    fn select_project_announcing(&mut self, id: &str, announce: bool) {
        self.ui.project = Some(id.to_owned());
        self.ui.cursor.insert(Tab::Sessions, 0);
        self.ui.drawer = None;
        self.ui.files_project_root = true;
        self.reset_files();
        self.effects
            .push(Effect::Sync(SyncCommand::ProjectWorktrees(id.to_owned())));
        self.effects
            .push(Effect::Sync(SyncCommand::SkillMcp(id.to_owned())));
        if announce {
            self.toast(
                Level::Info,
                format!(
                    "Project: {}",
                    self.state
                        .model
                        .projects
                        .get(id)
                        .map_or(id, |p| p.name.as_str())
                ),
            );
        }
    }

    /// The project already registered at `path`, if the snapshot has one.
    fn project_at_path(&self, path: &str) -> Option<String> {
        let resolve =
            |p: &str| std::fs::canonicalize(p).unwrap_or_else(|_| std::path::PathBuf::from(p));
        let target = resolve(path);
        self.state
            .model
            .projects
            .values()
            .find(|project| resolve(&project.path) == target)
            .map(|project| project.id.clone())
    }

    /// Collapse the selected folder, or move to its parent row in the tree.
    fn files_up(&mut self) {
        let Some(row) = self.selected_file() else {
            return;
        };
        if row.kind == "directory" && row.expanded {
            self.ui.files_expanded.remove(&row.path);
            return;
        }
        let Some((parent, _)) = row.path.rsplit_once('/') else {
            return;
        };
        if let Some(index) = self.file_rows().iter().position(|row| row.path == parent) {
            self.ui.cursor.insert(Tab::Files, index);
        }
    }

    /// Expand the selected folder (Enter and right arrow).
    fn files_expand(&mut self) {
        let Some(row) = self.selected_file() else {
            return;
        };
        if row.kind != "directory" || row.expanded {
            return;
        }
        self.ui.files_expanded.insert(row.path.clone());
        if !self.state.file_dirs.contains_key(&row.path) {
            self.request_files(row.path.clone(), false);
        }
    }

    /// One row of the projects overview.
    pub fn open_projects_overview(&mut self) {
        if self.edit_blocks() {
            return;
        }
        self.ui.left_open = true;
        self.ui.tab = Tab::Projects;
        self.ui.focus = Focus::Sidebar;
        self.open_overlay(OverlayKind::Projects(crate::ui::ProjectsOverview::default()));
    }

    /// The overview's rows in Core's saved order: a project, its active root
    /// sessions, then its archived toggle (and expanded archived sessions).
    pub fn overview_rows(&self, o: &crate::ui::ProjectsOverview) -> Vec<OverviewRow> {
        let mut rows = Vec::new();
        let all = self.project_ids();
        let mut inactive = Vec::new();
        for id in all {
            if self.project_status(&id) == crate::api::project_status::INACTIVE {
                inactive.push(id);
            } else {
                self.overview_project_rows(&mut rows, o, &id);
            }
        }
        if !inactive.is_empty() {
            rows.push(OverviewRow::Group("Inactive"));
            for id in inactive {
                self.overview_project_rows(&mut rows, o, &id);
            }
        }
        rows
    }

    fn overview_project_rows(
        &self,
        rows: &mut Vec<OverviewRow>,
        o: &crate::ui::ProjectsOverview,
        id: &str,
    ) {
        rows.push(OverviewRow::Project(id.to_owned()));
        let mut sessions: Vec<_> = self
            .state
            .model
            .sessions
            .values()
            .filter(|s| s.project_id == id && s.parent_id.is_none())
            .collect();
        sessions.sort_by(|a, b| {
            b.updated
                .total_cmp(&a.updated)
                .then_with(|| a.id.cmp(&b.id))
        });
        let active: Vec<_> = sessions.iter().filter(|s| !s.archived).collect();
        let archived: Vec<_> = sessions.iter().filter(|s| s.archived).collect();
        if active.is_empty() {
            rows.push(OverviewRow::Empty(id.to_owned()));
        }
        for s in active {
            rows.push(OverviewRow::Session(s.id.clone(), false));
        }
        if !archived.is_empty() {
            rows.push(OverviewRow::ArchivedToggle(id.to_owned(), archived.len()));
            if o.archived.contains(id) {
                for s in archived {
                    rows.push(OverviewRow::Session(s.id.clone(), true));
                }
            }
        }
    }

    /// The project a row belongs to (for status and archived actions).
    fn overview_row_project(
        &self,
        o: &crate::ui::ProjectsOverview,
        index: usize,
    ) -> Option<String> {
        let rows = self.overview_rows(o);
        let row = rows.get(index)?;
        match row {
            OverviewRow::Project(id)
            | OverviewRow::Empty(id)
            | OverviewRow::ArchivedToggle(id, _) => Some(id.clone()),
            OverviewRow::Session(id, _) => self
                .state
                .model
                .sessions
                .get(id)
                .map(|s| s.project_id.clone()),
            OverviewRow::Group(_) => None,
        }
    }

    fn overview_move(&mut self, delta: isize) {
        let Some(Overlay {
            kind: OverlayKind::Projects(o),
            ..
        }) = self.ui.overlay.as_ref()
        else {
            return;
        };
        let rows = self.overview_rows(o);
        let (cursor, top) = (o.cursor, o.top);
        if rows.is_empty() {
            return;
        }
        let mut next = cursor as isize + delta;
        while next >= 0
            && (next as usize) < rows.len()
            && matches!(rows[next as usize], OverviewRow::Group(_))
        {
            next += delta.signum();
        }
        if next < 0 || (next as usize) >= rows.len() {
            return;
        }
        let next = next as usize;
        if let Some(Overlay {
            kind: OverlayKind::Projects(o),
            ..
        }) = self.ui.overlay.as_mut()
        {
            o.cursor = next;
            let height = 16usize;
            if o.cursor < top {
                o.top = o.cursor;
            } else if o.cursor >= o.top + height {
                o.top = o.cursor + 1 - height;
            }
        }
    }

    fn overview_toggle_archived(&mut self) {
        let id = self.ui.overlay.as_ref().and_then(|overlay| {
            let OverlayKind::Projects(o) = &overlay.kind else {
                return None;
            };
            self.overview_row_project(o, o.cursor)
        });
        let Some(id) = id else {
            return;
        };
        if let Some(Overlay {
            kind: OverlayKind::Projects(o),
            ..
        }) = self.ui.overlay.as_mut()
        {
            if !o.archived.remove(&id) {
                o.archived.insert(id);
            }
        }
    }

    fn overview_status(&mut self, status: &str) {
        let Some(Overlay {
            kind: OverlayKind::Projects(o),
            ..
        }) = self.ui.overlay.as_ref()
        else {
            return;
        };
        if let Some(id) = self.overview_row_project(o, o.cursor) {
            self.set_project_status(&id, status);
        }
    }

    fn overview_activate(&mut self) {
        let Some(Overlay {
            kind: OverlayKind::Projects(o),
            ..
        }) = self.ui.overlay.as_ref()
        else {
            return;
        };
        let Some(row) = self.overview_rows(o).get(o.cursor).cloned() else {
            return;
        };
        match row {
            OverviewRow::Session(id, _) => {
                self.close_overlay();
                self.open_session(&id);
            }
            OverviewRow::Project(id) | OverviewRow::Empty(id) => {
                self.close_overlay();
                self.select_project_quietly(&id);
                self.show_new_session();
                self.after_open();
                self.ui.tab = Tab::Sessions;
                self.ui.focus = Focus::Composer;
            }
            OverviewRow::ArchivedToggle(_, _) => self.overview_toggle_archived(),
            OverviewRow::Group(_) => {}
        }
    }

    /// Keys while the projects overview is open. Returns false for a key it
    /// does not use, so the ordinary overlay handling sees it.
    fn projects_overview_key(&mut self, name: &str) -> bool {
        match name {
            "up" | "k" => self.overview_move(-1),
            "down" | "j" => self.overview_move(1),
            "home" => {
                let first = self.ui.overlay.as_ref().and_then(|overlay| {
                    let OverlayKind::Projects(o) = &overlay.kind else {
                        return None;
                    };
                    self.overview_rows(o)
                        .iter()
                        .position(|r| !matches!(r, OverviewRow::Group(_)))
                });
                if let (
                    Some(first),
                    Some(Overlay {
                        kind: OverlayKind::Projects(o),
                        ..
                    }),
                ) = (first, self.ui.overlay.as_mut())
                {
                    o.cursor = first;
                    o.top = 0;
                }
            }
            "end" => {
                let last = self.ui.overlay.as_ref().and_then(|overlay| {
                    let OverlayKind::Projects(o) = &overlay.kind else {
                        return None;
                    };
                    self.overview_rows(o)
                        .iter()
                        .rposition(|r| !matches!(r, OverviewRow::Group(_)))
                });
                if let (
                    Some(last),
                    Some(Overlay {
                        kind: OverlayKind::Projects(o),
                        ..
                    }),
                ) = (last, self.ui.overlay.as_mut())
                {
                    o.cursor = last;
                }
            }
            "enter" | "space" => self.overview_activate(),
            "a" => self.overview_toggle_archived(),
            "1" => self.overview_status(crate::api::project_status::CURRENT),
            "2" => self.overview_status(crate::api::project_status::PASSIVE),
            "3" => self.overview_status(crate::api::project_status::INACTIVE),
            "r" => self.overview_status(crate::api::project_status::CURRENT),
            "right" | "l" => self.close_overlay(),
            _ => return false,
        }
        true
    }

    /// Switch to a rail project: its remembered session, or its newest root
    /// session the first time, or an empty new-session view.
    pub(crate) fn rail_activate(&mut self, index: usize) {
        if self.edit_blocks() {
            return;
        }
        let Some(project) = self.rail_project_ids().get(index).cloned() else {
            return;
        };
        if self.current_project().as_deref() == Some(project.as_str()) {
            self.ui.scope_all = false;
            if self.narrow() {
                if self.ui.drawer == Some(Side::Left) {
                    self.ui.drawer = None;
                } else {
                    self.set_tab(Tab::Sessions);
                }
            } else if self.ui.left_open {
                self.ui.left_open = false;
            } else {
                self.set_tab(Tab::Sessions);
            }
            return;
        }
        let remembered = self
            .ui
            .last_project_session
            .get(&project)
            .and_then(|id| self.state.model.sessions.get(id))
            .filter(|s| s.project_id == project && !s.archived)
            .map(|s| s.id.clone());
        let session = remembered.or_else(|| {
            if self.ui.last_project_session.contains_key(&project) {
                return None;
            }
            self.state
                .model
                .sessions
                .values()
                .filter(|s| s.project_id == project && s.parent_id.is_none() && !s.archived)
                .max_by(|a, b| {
                    a.updated
                        .total_cmp(&b.updated)
                        .then_with(|| a.id.cmp(&b.id))
                })
                .map(|s| s.id.clone())
        });
        self.select_project_quietly(&project);
        self.ui.scope_all = false;
        if let Some(id) = session {
            self.open_session(&id);
        } else {
            self.show_new_session();
            self.after_open();
        }
        self.set_tab(Tab::Sessions);
    }

    fn sidebar_activate(&mut self, index: usize) {
        match self.ui.tab {
            Tab::Sessions => {
                if let Some(e) = self.side_entries().get(index) {
                    let id = e.id.clone();
                    self.open_session(&id);
                }
            }
            Tab::Projects => {
                if let Some(id) = self.project_ids().get(index).cloned() {
                    self.select_project(&id);
                    self.ui.tab = Tab::Sessions;
                }
            }
            Tab::Git => {
                self.open_project_git_diff(index);
            }
            Tab::Files => {
                let Some(row) = self.file_rows().get(index).cloned() else {
                    return;
                };
                match row.kind.as_str() {
                    "directory" => {
                        if self.ui.files_expanded.contains(&row.path) {
                            self.ui.files_expanded.remove(&row.path);
                        } else {
                            self.ui.files_expanded.insert(row.path.clone());
                            if !self.state.file_dirs.contains_key(&row.path) {
                                self.request_files(row.path.clone(), false);
                            }
                        }
                    }
                    "file" | "symlink" => {
                        self.ui.diff_open = None;
                        self.ui.file_open = true;
                        self.ui.file_scroll = 0;
                        self.ui.file_source = false;
                        self.ui.file_search = None;
                        self.request_files(row.path.clone(), true);
                        self.ui.focus = Focus::Timeline;
                    }
                    _ => {}
                }
            }
        }
    }

    pub fn execute(&mut self, id: &str) {
        match id {
            "terminal.open" => {
                if self.terminal_open {
                    self.close_panel();
                    return;
                }
                if self.edit_blocks() {
                    return;
                }
                let Some(project) = self.current_project() else {
                    return;
                };
                self.terminal_open = true;
                self.terminal_panel.focus = crate::terminal_panel::TerminalFocus::List;
                self.ui.file_open = false;
                self.ui.diff_open = None;
                self.terminal_panel.terminals.clear();
                self.terminal_panel.cursor = 0;
                self.terminal_panel.loading = true;
                self.api(ApiCall::TerminalList { project });
            }
            "terminal.new" => {
                if self.terminal_panel.creating || self.terminal_panel.loading {
                    return;
                }
                if let (Some(project), Some(workspace)) =
                    (self.current_project(), self.files_root())
                {
                    self.terminal_panel.creating = true;
                    self.api(ApiCall::TerminalCreate {
                        project,
                        workspace,
                        session: self.files_session(),
                        client: self.terminal_panel.client_id.clone(),
                    });
                }
            }
            "terminal.rename" => {
                if let Some(terminal) = self
                    .terminal_panel
                    .terminals
                    .iter()
                    .find(|t| Some(&t.id) == self.terminal_panel.selected.as_ref())
                {
                    let mut buf = Composer::new();
                    buf.set_text(&terminal.name);
                    self.ui.edit = Some(TextEdit {
                        label: "Rename".into(),
                        target: EditTarget::RenameTerminal {
                            id: terminal.id.clone(),
                        },
                        buf,
                        restore: self.restore_point(),
                    });
                }
            }
            "terminal.attach" => {
                let text = self
                    .terminal_panel
                    .selection
                    .as_ref()
                    .filter(|s| s.dragged())
                    .map(|s| s.text())
                    .unwrap_or_default();
                if !text.is_empty() && self.writable() && !self.manager_debug_view() {
                    if let Some(source) = self
                        .terminal_panel
                        .terminals
                        .iter()
                        .find(|t| Some(&t.id) == self.terminal_panel.selected.as_ref())
                    {
                        let payload = crate::terminal_panel::terminal_evidence_with_context(
                            source,
                            &text,
                            self.terminal_panel.screen.seq,
                        );
                        self.ui.dialog = Some(Dialog {
                            title: "Attach terminal output".into(),
                            body: payload.lines().map(str::to_owned).collect(),
                            cancel: "Cancel".into(),
                            confirm: "Add to draft".into(),
                            danger: false,
                            button: 1,
                            action: Confirmed::AttachTerminalEvidence {
                                draft: self.draft_key(),
                                payload,
                                offset: 0,
                            },
                            restore: self.restore_point(),
                        });
                    }
                }
            }
            "terminal.copy" => {
                if let Some(selection) = &self.terminal_panel.selection {
                    if selection.dragged() {
                        let text = selection.text();
                        if !text.is_empty() {
                            self.effects.push(Effect::Copy(text));
                        }
                    }
                }
            }
            "terminal.close" => {
                if let Some(terminal) = self
                    .terminal_panel
                    .terminals
                    .iter()
                    .find(|t| Some(&t.id) == self.terminal_panel.selected.as_ref())
                {
                    let id = terminal.id.clone();
                    if terminal.status == "running" {
                        self.ui.dialog = Some(Dialog {
                            title: "Close running terminal?".into(),
                            body: vec![],
                            cancel: "Cancel".into(),
                            confirm: "Close terminal".into(),
                            danger: true,
                            action: Confirmed::CloseTerminal(id),
                            button: 0,
                            restore: self.restore_point(),
                        });
                    } else {
                        self.api(ApiCall::TerminalClose { id });
                    }
                }
            }
            "terminal.acquire" | "terminal.release" => {
                if self.terminal_panel.control_busy {
                    return;
                }
                if self.terminal_panel.input_busy || !self.terminal_panel.input_queue.is_empty() {
                    self.toast(Level::Warning, "Wait for terminal input to be acknowledged");
                    return;
                }
                if let (Some(terminal_id), Some(attachment)) = (
                    self.terminal_panel.selected.clone(),
                    self.terminal_panel.attachment.clone(),
                ) {
                    self.terminal_panel.control_busy = true;
                    self.api(ApiCall::TerminalControl {
                        id: terminal_id,
                        attachment,
                        generation: self.terminal_panel.generation,
                        acquire: id == "terminal.acquire",
                    });
                }
            }
            "terminal.focus_list" => {
                self.terminal_focused = false;
                self.terminal_panel.focus = crate::terminal_panel::TerminalFocus::List;
            }
            "palette.open" => {
                if matches!(
                    self.ui.overlay,
                    Some(Overlay {
                        kind: OverlayKind::Palette(_),
                        ..
                    })
                ) {
                    self.close_overlay();
                } else {
                    self.open_palette(PaletteMode::Commands);
                }
            }
            "app.help_keys" => self.open_overlay(OverlayKind::Help { top: 0 }),
            "editor.edit" => self.start_edit(),
            "editor.save" => self.save_file(),
            "editor.close" => self.close_panel(),
            "editor.preview" => self.toggle_file_preview(),
            "editor.find" => self.open_file_find(),
            "editor.reload" => {
                if self.ui.file_edit.as_ref().is_some_and(FileEdit::dirty) {
                    self.ask_reload_edit();
                } else {
                    self.reload_file();
                }
            }
            "app.usage" => {
                self.open_overlay(OverlayKind::Usage {
                    rows: vec![crate::usage::Row::Note("Reading provider usage…".into())],
                    top: 0,
                });
                self.api(ApiCall::Usage);
            }
            "app.quit" => self.effects.push(Effect::Quit),
            "app.settings" => {
                if self.ui.view == View::Settings {
                    self.ui.view = View::Session;
                    self.ui.focus = Focus::Composer;
                    self.ui.provider_detail = None;
                } else {
                    self.effects.push(Effect::Sync(SyncCommand::Config));
                    self.effects
                        .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
                    self.refresh_voice_settings_status();
                    self.refresh_search_service();
                    self.ui.view = View::Settings;
                    self.ui.settings_search = Composer::new();
                    self.ui.settings_row = 0;
                    self.ui.settings_page = None;
                    self.ui.provider_detail = None;
                    self.ui.focus = Focus::Timeline;
                    self.ui.drawer = None;
                }
            }
            "app.theme" => {
                let mut o = self.theme.opts;
                o.mode = match o.mode {
                    crate::theme::Mode::Dark => crate::theme::Mode::Light,
                    crate::theme::Mode::Light => crate::theme::Mode::Dark,
                };
                self.theme = self.theme.with(o);
            }
            "app.reconnect" => self.effects.push(Effect::Sync(SyncCommand::Resync)),
            "notifications.open" => {
                self.ui.alerts.mark_all_read();
                self.open_overlay(OverlayKind::Notifications { selected: 0 });
            }
            "notifications.mark_all_read" => self.ui.alerts.mark_all_read(),
            "alert.dismiss" => self.dismiss_alert(),
            "alert.confirm" => self.confirm_dialog(true),
            "alert.cancel" => self.confirm_dialog(false),
            "text_edit.cancel" => {
                if let Some(e) = self.ui.edit.take() {
                    self.apply_restore(e.restore);
                }
            }
            "overlay.close" => self.close_overlay(),
            "nav.drawer.close" => {
                if self.ui.drawer.take().is_some() {
                    self.ui.focus = if self.ui.drawer.is_none() {
                        self.drawer_opener()
                    } else {
                        Focus::Composer
                    };
                }
            }
            "sidebar.step_back" => self.set_tab(Tab::Sessions),
            "nav.back" => {
                if let Some(p) = self.viewed().and_then(|s| s.parent_id.clone()) {
                    self.open_session(&p);
                }
            }
            "nav.root" => {
                if let Some(root) = self
                    .viewed_root()
                    .filter(|_| self.viewed().is_some_and(|s| s.parent_id.is_some()))
                {
                    self.open_session(&root);
                }
            }
            "sidebar.left.toggle" => self.toggle_side(Side::Left),
            "sidebar.right.toggle" => self.toggle_side(Side::Right),
            "sidebar.tab.sessions" => self.set_tab(Tab::Sessions),
            "sidebar.tab.files" => self.set_tab(Tab::Files),
            "sidebar.tab.git" => self.set_tab(Tab::Git),
            "git.stage"
            | "git.unstage"
            | "git.discard"
            | "git.branch_delete_force"
            | "git.commit"
            | "git.fetch"
            | "git.pull"
            | "git.push"
            | "git.switch"
            | "git.branch_create"
            | "git.branch_delete" => {
                let Some(project) = self.current_project() else {
                    return;
                };
                let worktree = self.git_worktree();
                let action = id.trim_start_matches("git.").to_owned();
                if action == "stage" || action == "unstage" {
                    if let Some(row) = self.project_git_rows().get(self.cursor(Tab::Git)) {
                        self.api(ApiCall::ProjectGitAction {
                            project,
                            worktree,
                            action,
                            value: String::new(),
                            paths: vec![row.path.clone()],
                            confirmed_live: vec![],
                        });
                    }
                } else if action == "discard" {
                    if let Some(row) = self.project_git_rows().get(self.cursor(Tab::Git)) {
                        self.api(ApiCall::ProjectGitLive {
                            project,
                            worktree,
                            action,
                            value: String::new(),
                            paths: vec![row.path.clone()],
                        });
                    }
                } else if [
                    "commit",
                    "switch",
                    "branch_create",
                    "branch_delete",
                    "branch_delete_force",
                ]
                .contains(&action.as_str())
                {
                    self.ui.edit = Some(TextEdit {
                        label: match action.as_str() {
                            "commit" => "Commit message",
                            "switch" => "Switch branch",
                            "branch_create" => "Create branch",
                            _ => "Delete branch",
                        }
                        .into(),
                        target: EditTarget::ProjectGitValue {
                            project,
                            worktree,
                            action,
                        },
                        buf: Composer::new(),
                        restore: self.restore_point(),
                    });
                } else {
                    let value = if action == "push" {
                        self.state
                            .project_git
                            .as_ref()
                            .and_then(|(_, r)| r.as_ref().ok())
                            .and_then(|d| d["push_upstream"]["remote"].as_str())
                            .unwrap_or("")
                            .to_owned()
                    } else {
                        String::new()
                    };
                    self.api(ApiCall::ProjectGitLive {
                        project,
                        worktree,
                        action,
                        value,
                        paths: vec![],
                    });
                }
            }
            "git.refresh" => {
                if let Some(project) = self.current_project() {
                    self.api(ApiCall::ProjectGit {
                        project,
                        worktree: self.git_worktree(),
                    });
                }
            }
            "sidebar.tab.projects" => self.set_tab(Tab::Projects),
            "git.open_diff" => {
                if self.ui.tab == Tab::Git && self.ui.focus == Focus::Sidebar {
                    self.open_project_git_diff(self.cursor(Tab::Git));
                } else {
                    self.open_git_diff(self.ui.git_cursor);
                }
            }
            "sidebar.focus" => {
                if self.narrow() {
                    self.ui.drawer = Some(Side::Left);
                } else {
                    self.ui.left_open = true;
                }
                self.ui.focus = Focus::Sidebar;
            }
            "composer.focus" => {
                if self.ui.view == View::Settings {
                    self.ui.view = View::Session;
                }
                self.ui.focus = Focus::Composer;
            }
            "timeline.focus" => self.ui.focus = Focus::Timeline,
            "session.export" => self.open_palette(PaletteMode::Export),
            "skills.learn" => self.toast(
                Level::Info,
                "Type /learn followed by owner/repo, a GitHub URL or ./folder",
            ),
            "turn.go_to" => self.open_palette(PaletteMode::Turns),
            "composer.history_search" => self.open_palette(PaletteMode::History),
            // The new-session screen; the session exists once the first message is sent.
            "session.new" => {
                if self.edit_blocks() {
                    return;
                }
                self.show_new_session();
            }
            "session.open" => {
                let c = self.cursor(self.ui.tab);
                self.sidebar_activate(c);
            }
            "project.open" => {
                let c = self.cursor(Tab::Projects);
                self.sidebar_activate(c);
            }
            "project.add" => {
                let mut buf = Composer::new();
                // Start from the current project's parent: the usual sibling checkout.
                let start = self
                    .current_project()
                    .and_then(|p| self.state.model.projects.get(&p))
                    .and_then(|p| {
                        std::path::Path::new(&p.path)
                            .parent()
                            .map(|d| d.display().to_string())
                    })
                    .map(|d| if d.ends_with('/') { d } else { format!("{d}/") })
                    .unwrap_or_default();
                buf.set_text(&start);
                let restore = self.restore_point();
                self.ui.edit = Some(TextEdit {
                    label: "add project".into(),
                    target: EditTarget::AddProject,
                    buf,
                    restore,
                });
            }
            "project.remove" => self.ask_remove_project(),
            "project.move_up" => {
                if let Some(id) = self.menu_project() {
                    self.move_project_step(&id, -1);
                }
            }
            "project.move_down" => {
                if let Some(id) = self.menu_project() {
                    self.move_project_step(&id, 1);
                }
            }
            "project.status.current" => {
                if let Some(id) = self.menu_project() {
                    self.set_project_status(&id, crate::api::project_status::CURRENT);
                }
            }
            "project.status.passive" => {
                if let Some(id) = self.menu_project() {
                    self.set_project_status(&id, crate::api::project_status::PASSIVE);
                }
            }
            "project.status.inactive" => {
                if let Some(id) = self.menu_project() {
                    self.set_project_status(&id, crate::api::project_status::INACTIVE);
                }
            }
            "project.overview" => self.open_projects_overview(),
            "sidebar.row_menu" => {
                self.open_palette(PaletteMode::RowMenu);
            }
            "session.switch" => self.open_palette(PaletteMode::Sessions),
            "session.search" => self.open_sessions_search(""),
            "project.switch" => self.open_palette(PaletteMode::Projects),
            "app.sign_in" | "app.sign_out" => {
                if self.ui.view != View::Settings {
                    self.execute("app.settings");
                }
                self.ui.settings_section = "providers".into();
                self.ui.settings_nav_focus = false;
                self.ui.settings_page = None;
                self.ui.settings_page_row = 0;
            }
            "model.change" => self.open_picker(PaletteMode::Models),
            "model.effort" => self.open_picker(PaletteMode::Efforts),
            "agent.change" => self.open_picker(PaletteMode::Agents),
            "agent.cycle" => self.cycle_agent(),
            "session.rename" => {
                if let Some(s) = self.viewed() {
                    let (id, revision, title) = (s.id.clone(), s.revision, s.title.clone());
                    let mut buf = Composer::new();
                    buf.set_text(&title);
                    let restore = self.restore_point();
                    self.ui.edit = Some(TextEdit {
                        label: "rename".into(),
                        target: EditTarget::RenameSession { id, revision },
                        buf,
                        restore,
                    });
                }
            }
            "session.change_directory" => {
                if let Some(s) = self.viewed() {
                    let (id, dir) = (s.id.clone(), s.directory.clone());
                    let mut buf = Composer::new();
                    buf.set_text(&dir);
                    let restore = self.restore_point();
                    self.ui.edit = Some(TextEdit {
                        label: "directory".into(),
                        target: EditTarget::ChangeDirectory { id },
                        buf,
                        restore,
                    });
                }
            }
            "session.resources" => {
                self.effects.push(Effect::Sync(SyncCommand::Resources));
                self.open_overlay(OverlayKind::Resources {
                    top: 0,
                    kind: crate::ui::ResourceKind::All,
                    selected: 0,
                });
            }
            "session.worktree_info" => {
                if let Some(info) = self.worktree_info() {
                    self.open_overlay(OverlayKind::WorktreeInfo(info));
                }
            }
            "session.archive" | "session.unarchive" => {
                if let Some(sid) = self.viewed_id().map(str::to_owned) {
                    if id == "session.unarchive" {
                        self.ask_resume(Confirmed::Unarchive(sid));
                        return;
                    }
                    self.api(ApiCall::Archive {
                        session: sid,
                        archived: id == "session.archive",
                    });
                }
            }
            "session.delete" => self.ask_delete(),
            "session.interrupt" => {
                if let Some(id) = self.viewed_id().map(str::to_owned) {
                    self.api(ApiCall::Interrupt { session: id });
                }
            }
            "session.continue" => {
                if let Some(id) = self.viewed_id().map(str::to_owned) {
                    self.api(ApiCall::Continue { session: id });
                }
            }
            "session.speak" => {
                if let Some(id) = self.viewed_id().map(str::to_owned) {
                    self.api(ApiCall::Speak { session: id });
                }
            }
            "session.speak_stop" => self.api(ApiCall::SpeakStop),
            "session.handoff" => {
                if let Some(id) = self.viewed_id().map(str::to_owned) {
                    self.toast(Level::Info, "Writing the handoff…");
                    self.api(ApiCall::Handoff { session: id });
                }
            }
            "session.btw" => self.toast(Level::Info, "Usage: /btw your question"),
            "session.open_origin" => {
                let target = self.viewed().and_then(|s| s.continued_from.clone());
                match target {
                    Some(t) if self.state.model.sessions.contains_key(&t) => self.open_session(&t),
                    Some(_) => self.toast(
                        Level::Warning,
                        "That session is no longer in the list (deleted or archived)",
                    ),
                    None => {}
                }
            }
            id if id.starts_with("manager.") => self.manager_execute(id),
            "voice.dictate" => self.begin_dictation(),
            "search.start" => self.start_search_service(),
            "voice.download_stt" => self.ask_voice_model_download(VoiceTarget::Stt),
            "voice.download_tts" => self.ask_voice_model_download(VoiceTarget::Tts),
            "voice.remove_stt" => self.ask_voice_model_remove(VoiceTarget::Stt),
            "voice.remove_tts" => self.ask_voice_model_remove(VoiceTarget::Tts),
            "session.send" | "composer.steer" => self.send(),
            "composer.queue" => self.send_mode(Some("queue")),
            "composer.newline" => self.composer().newline(),
            "composer.undo" => self.composer().undo(),
            "composer.redo" => self.composer().redo(),
            "session.jump_latest" => {
                self.ui.scroll = Scroll::default();
            }
            "session.copy_id" => {
                if let Some(id) = self.viewed_id().map(str::to_owned) {
                    self.effects.push(Effect::Copy(id));
                    self.toast(Level::Success, "Copied session id");
                }
            }
            "turn.fold_toggle" | "tool.expand_toggle" => self.activate_block(),
            "session.child.open" => self.activate_block(),
            "turn.fold_all_but_last" => self.fold_turns(true),
            "turn.unfold_all" => {
                let keys: Vec<String> = self
                    .ui
                    .timeline_blocks
                    .iter()
                    .filter(|b| b.toggle)
                    .map(|b| b.key.clone())
                    .collect();
                for k in keys {
                    self.ui.timeline_open.insert(k, true);
                }
                self.fold_turns(false);
                if let Some(b) = self
                    .ui
                    .timeline_blocks
                    .iter()
                    .find(|b| b.kind == crate::timeline::BlockKind::Tool)
                {
                    self.ui.timeline_open.insert(b.key.clone(), true);
                }
                self.toast(Level::Info, "All turns unfolded");
            }
            "turn.prev" => self.adjacent_turn(false),
            "turn.next" => self.adjacent_turn(true),
            "tool.raw_toggle" => {
                if let Some(b) = self
                    .ui
                    .timeline_sel
                    .and_then(|i| self.ui.timeline_blocks.get(i))
                    .cloned()
                {
                    if b.kind == crate::timeline::BlockKind::Tool {
                        let raw = format!("{}:raw", b.key);
                        let now = self.ui.timeline_open.get(&raw).copied().unwrap_or(false);
                        self.ui.timeline_open.insert(b.key, true);
                        self.ui.timeline_open.insert(raw, !now);
                    }
                }
            }
            "timeline.detail_cycle" => self.set_detail(self.ui.detail.step(1)),
            "session.tab_close" => {
                if let Some(id) = self.viewed_id().map(str::to_owned) {
                    self.close_tab(&id);
                }
            }
            "session.tab_next" => self.step_tab(1),
            "session.tab_prev" => self.step_tab(-1),
            "tool.show_all_output" => {
                if let Some(b) = self
                    .ui
                    .timeline_sel
                    .and_then(|i| self.ui.timeline_blocks.get(i))
                    .cloned()
                {
                    if b.kind == crate::timeline::BlockKind::Tool && b.clipped {
                        let all = format!("{}:all", b.key);
                        let now = self.ui.timeline_open.get(&all).copied().unwrap_or(false);
                        self.ui.timeline_open.insert(all, !now);
                    }
                }
            }
            "app.open_inspector" | "session.open_inspector" | "tool.open_inspector" => {
                self.open_inspector()
            }
            "copy.final_message"
            | "copy.tool_output"
            | "copy.user_message"
            | "copy.assistant_block" => self.copy_selected(),
            "copy.code_block" => self.copy_selected(),
            "copy.file_path" => {
                let Some(row) = self
                    .selected_file()
                    .filter(|row| row.kind != "directory" && row.kind != "error")
                else {
                    self.toast(Level::Info, "Focus a file first");
                    return;
                };
                if let Some(path) = self.file_absolute(&row) {
                    self.effects.push(Effect::Copy(path));
                    self.toast(Level::Success, "Copied file path");
                }
            }
            "file.insert_mention" => {
                let Some(row) = self
                    .selected_file()
                    .filter(|row| row.kind != "directory" && row.kind != "error")
                else {
                    self.toast(Level::Info, "Focus a file first");
                    return;
                };
                self.composer().insert_str(&format!("@{} ", row.path));
                self.ui.focus = Focus::Composer;
                if self.narrow() {
                    self.ui.drawer = None;
                }
            }
            "file.open_external" => {
                let Some(row) = self
                    .selected_file()
                    .filter(|row| row.kind != "directory" && row.kind != "error")
                else {
                    self.toast(Level::Info, "Focus a file first");
                    return;
                };
                if let Some(path) = self.file_absolute(&row) {
                    self.effects.push(Effect::OpenEditor(path));
                }
            }
            "files.new_file" | "files.new_folder" | "files.rename" | "files.delete" => {
                let Some(project) = self.current_project() else {
                    return;
                };
                let session = self.files_session();
                let row = self.selected_file();
                if id == "files.delete" {
                    if let Some(row) = row {
                        if row.kind == "directory" {
                            self.api(ApiCall::FileCount {
                                project,
                                session,
                                path: row.path,
                            });
                        } else {
                            self.ask_delete_file(project, session, row.path, None);
                        }
                    }
                } else {
                    let action = match id {
                        "files.new_file" => "create_file",
                        "files.new_folder" => "create_dir",
                        _ => "rename",
                    };
                    let path = if action == "rename" {
                        row.map(|r| r.path).unwrap_or_default()
                    } else {
                        String::new()
                    };
                    let mut buf = Composer::new();
                    if action == "rename" {
                        buf.set_text(&path);
                    }
                    self.ui.edit = Some(TextEdit {
                        label: match action {
                            "create_file" => "New file",
                            "create_dir" => "New folder",
                            _ => "Rename file or folder",
                        }
                        .into(),
                        target: EditTarget::FilePath {
                            project,
                            session,
                            action: action.into(),
                            path,
                        },
                        buf,
                        restore: self.restore_point(),
                    });
                }
            }
            "files.toggle_ignored" => {
                self.ui.files_hide_ignored = !self.ui.files_hide_ignored;
                self.ui.cursor.insert(Tab::Files, 0);
                self.toast(
                    Level::Info,
                    if self.ui.files_hide_ignored {
                        "Ignored files hidden"
                    } else {
                        "Ignored files shown"
                    },
                );
            }
            "files.root_toggle" => {
                let count = self
                    .current_project()
                    .and_then(|project| self.state.project_worktrees.get(&project))
                    .and_then(|listing| listing["worktrees"].as_array())
                    .map_or(0, Vec::len);
                if count < 2 {
                    self.toast(Level::Info, "This project has one checkout");
                    return;
                }
                self.ui.files_project_root = !self.ui.files_project_root;
                self.reset_files();
                self.toast(
                    Level::Info,
                    if self.ui.files_project_root {
                        "Files: project checkout"
                    } else {
                        "Files: session checkout"
                    },
                );
            }
            "sessions.scope_toggle" => {
                self.ui.scope_all = !self.ui.scope_all;
                self.ui.cursor.insert(Tab::Sessions, 0);
                self.ui.side_top = 0;
            }
            "sessions.archived_filter" => {
                self.ui.show_archived = !self.ui.show_archived;
                self.ui.cursor.insert(Tab::Sessions, 0);
                self.ui.side_top = 0;
            }
            "waiting.allow_once"
            | "waiting.allow_session"
            | "waiting.deny"
            | "waiting.deny_note" => {
                if let Some(w) = self.wait_focused().filter(|w| w.kind == WaitKind::Approval) {
                    let allow = id.starts_with("waiting.allow");
                    let mut note = None;
                    if id == "waiting.deny_note" {
                        // The note is the composer's text, as an answer is.
                        let text = self.composer_ref().expanded_text().trim().to_owned();
                        if text.is_empty() {
                            self.toast(
                                Level::Warning,
                                "Type the note in the composer, then press d",
                            );
                            return;
                        }
                        self.composer().take();
                        note = Some(text);
                    }
                    self.api(ApiCall::Reply {
                        session: w.session_id,
                        id: w.id,
                        allow,
                        scope_session: id == "waiting.allow_session",
                        note,
                    });
                }
            }
            "turn.load_older" => {
                let first = self
                    .state
                    .model
                    .selected
                    .as_ref()
                    .and_then(|v| v.messages.first().map(|m| (v.session_id.clone(), m.seq)));
                if let (Some((session, before)), false) = (first, self.ui.loading_older) {
                    self.ui.loading_older = true;
                    self.ui.older_failed = None;
                    self.api(ApiCall::OlderMessages { session, before });
                }
            }
            "waiting.submit_answers" => self.ask_submit(),
            "waiting.scroll_up" => self.ask_scroll(false),
            "waiting.scroll_down" => self.ask_scroll(true),
            "waiting.next" => {
                self.ui.wait_idx += 1;
                self.ui.wait_choice = 0;
            }
            "waiting.prev" => {
                self.ui.wait_idx = self.ui.wait_idx.saturating_sub(1);
                self.ui.wait_choice = 0;
            }
            "waiting.focus" => {
                if !self.wait_items().is_empty() {
                    self.ui.focus = Focus::Waiting;
                } else {
                    self.execute("waiting.go_to");
                }
            }
            "waiting.custom" => {
                self.ui.focus = Focus::Waiting;
                let on = self.ask_current().is_some_and(|c| !c.field());
                self.ask_field(on);
            }
            "waiting.go_to" => {
                if let Some(first) = self.waiting_elsewhere().into_iter().next() {
                    self.open_session(&first);
                }
            }
            _ => {}
        }
        // keep the wait cursor in range
        let n = self.wait_items().len();
        if n > 0 {
            self.ui.wait_idx %= n;
        } else {
            self.ui.wait_idx = 0;
            if self.ui.focus == Focus::Waiting {
                self.ui.focus = Focus::Composer;
            }
        }
    }

    fn drawer_opener(&self) -> Focus {
        Focus::ToggleLeft
    }

    fn toggle_side(&mut self, side: Side) {
        if self.narrow() {
            if self.ui.drawer == Some(side) {
                self.ui.drawer = None;
                self.ui.focus = match side {
                    Side::Left => Focus::ToggleLeft,
                    Side::Right => Focus::ToggleRight,
                };
            } else {
                self.ui.drawer = Some(side);
                self.ui.focus = match side {
                    Side::Left => Focus::Sidebar,
                    Side::Right => Focus::Right,
                };
            }
            return;
        }
        let open = match side {
            Side::Left => &mut self.ui.left_open,
            Side::Right => &mut self.ui.right_open,
        };
        *open = !*open;
        if side == Side::Right {
            self.ui.right_chosen = true;
        }
        if !*open
            && matches!(
                (side, self.ui.focus),
                (Side::Left, Focus::Sidebar) | (Side::Right, Focus::Right)
            )
        {
            self.ui.focus = Focus::Composer;
        }
    }

    /// The full-text modal of alert `id` (D15).
    pub fn open_alert_detail(&mut self, id: u64, back: Option<usize>) {
        if self.ui.alerts.full(id).is_none() {
            return;
        }
        self.open_overlay(OverlayKind::AlertDetail { id, top: 0, back });
    }

    fn dismiss_alert(&mut self) {
        if let Some(i) = self.banners().iter().find_map(|b| b.hide) {
            if i < self.state.banners.len() {
                self.state.banners.remove(i);
            }
        } else if let Some(t) = self.ui.alerts.toasts.first().map(|t| t.id) {
            self.ui.alerts.dismiss(t);
        }
        if !self.has_alerts() {
            self.ui.focus = Focus::Composer;
        }
    }

    /// The project a Projects row menu or `project.remove` acts on: the focused
    /// row in the Projects tab, otherwise the current project.
    fn menu_project(&self) -> Option<String> {
        if self.ui.tab == Tab::Projects && self.ui.focus == Focus::Sidebar {
            if let Some(id) = self.project_ids().get(self.cursor(Tab::Projects)) {
                return Some(id.clone());
            }
        }
        self.current_project()
    }

    fn ask_remove_project(&mut self) {
        if let Some(id) = self.menu_project() {
            self.ask_remove_project_id(id);
        }
    }

    fn ask_remove_project_id(&mut self, id: String) {
        let Some(p) = self.state.model.projects.get(&id) else {
            return;
        };
        let sessions = self
            .state
            .model
            .sessions
            .values()
            .filter(|s| s.project_id == id)
            .count();
        let mut body = vec![format!("Remove \"{}\" from Nexus?", p.name)];
        if sessions > 0 {
            body.push(format!(
                "Its {sessions} session{} kept and return if you add the folder again.",
                if sessions == 1 { " is" } else { "s are" }
            ));
        }
        body.push("Files on disk are never touched.".into());
        let restore = self.restore_point();
        self.ui.dialog = Some(Dialog {
            title: "Remove project".into(),
            body,
            cancel: "Cancel".into(),
            confirm: "Remove".into(),
            danger: true,
            action: Confirmed::RemoveProject(id),
            button: 0,
            restore,
        });
    }

    fn ask_delete_file(
        &mut self,
        project: String,
        session: Option<String>,
        path: String,
        count: Option<u64>,
    ) {
        let mut body = vec![format!("Delete {path}?")];
        if let Some(n) = count {
            body.push(format!("Contains {n} items."));
        }
        body.push("This cannot be undone.".into());
        self.ui.dialog = Some(Dialog {
            title: "Delete file or folder".into(),
            body,
            cancel: "Cancel".into(),
            confirm: "Delete".into(),
            danger: true,
            action: Confirmed::DeleteFile {
                project,
                session,
                path,
            },
            button: 0,
            restore: self.restore_point(),
        });
    }

    fn ask_delete(&mut self) {
        let Some(s) = self.viewed() else { return };
        let n = self.descendants(&s.id);
        let mut body = vec![format!(
            "Delete \"{}\"{}?",
            s.title,
            if n > 0 {
                format!(" and its {n} sub agent(s)")
            } else {
                String::new()
            }
        )];
        body.push("This cannot be undone.".into());
        let restore = self.restore_point();
        let id = s.id.clone();
        self.ui.dialog = Some(Dialog {
            title: "Delete session".into(),
            body,
            cancel: "Cancel".into(),
            confirm: "Delete".into(),
            danger: true,
            action: Confirmed::DeleteSession(id),
            button: 0,
            restore,
        });
    }

    fn do_switch(&mut self, switch: Confirmed) {
        if let Confirmed::Switch {
            session,
            model,
            agent,
            revision,
        } = switch
        {
            match (model, agent) {
                (Some(model), _) => self.api(ApiCall::SetModel {
                    session,
                    model,
                    revision,
                }),
                (_, Some(agent)) => self.api(ApiCall::SetAgent {
                    session,
                    agent,
                    revision,
                }),
                _ => {}
            }
        }
    }

    fn confirm_dialog(&mut self, yes: bool) {
        if self.blocking().is_some() {
            return; // a blocking dialog cannot be dismissed
        }
        let Some(d) = self.ui.dialog.take() else {
            return;
        };
        self.apply_restore(d.restore);
        if yes {
            match d.action {
                Confirmed::ForceGitDelete {
                    project,
                    worktree,
                    value,
                    confirmed_live,
                } => self.api(ApiCall::ProjectGitAction {
                    project,
                    worktree,
                    action: "branch_delete_force".into(),
                    value,
                    paths: vec![],
                    confirmed_live,
                }),
                Confirmed::ProjectGitAction {
                    project,
                    worktree,
                    action,
                    value,
                    paths,
                    confirmed_live,
                } => {
                    if action == "branch_delete_force" {
                        self.ui.dialog = Some(Dialog {
                            title: "Force delete branch".into(),
                            body: vec![
                                format!("Force delete {value} even if unmerged?"),
                                "This cannot be undone.".into(),
                            ],
                            cancel: "Cancel".into(),
                            confirm: "Force delete".into(),
                            danger: true,
                            action: Confirmed::ForceGitDelete {
                                project,
                                worktree,
                                value,
                                confirmed_live,
                            },
                            button: 0,
                            restore: self.restore_point(),
                        });
                    } else {
                        self.api(ApiCall::ProjectGitAction {
                            project,
                            worktree,
                            action,
                            value,
                            paths,
                            confirmed_live,
                        });
                    }
                }
                Confirmed::DeleteFile {
                    project,
                    session,
                    path,
                } => self.api(ApiCall::FileMutation {
                    project,
                    session,
                    action: "delete".into(),
                    path,
                    to: None,
                }),
                Confirmed::CloseTerminal(id) => self.api(ApiCall::TerminalClose { id }),
                Confirmed::AttachTerminalEvidence { draft, payload, .. } => {
                    if self.draft_key() == draft && self.writable() && !self.manager_debug_view() {
                        let composer = self.composer();
                        let end = composer.text().len();
                        composer.select_range(end, end);
                        let separator = if end == 0 { "" } else { "\n\n" };
                        composer.insert_str(&format!("{separator}{payload}"));
                    } else {
                        self.toast(
                            Level::Warning,
                            "Draft changed or is read-only; capture terminal output again.",
                        );
                    }
                }
                Confirmed::ResumeArchived { session, mode } => {
                    if self.viewed_id() == Some(session.as_str()) {
                        self.send_mode_confirmed(mode.as_deref(), true);
                    }
                }
                Confirmed::Unarchive(session) => {
                    if self.writable() {
                        if let Some(root) = self.root_session(&session) {
                            self.api(ApiCall::Archive {
                                session: root,
                                archived: false,
                            });
                        }
                    }
                }
                switch @ Confirmed::Switch { .. } => self.do_switch(switch),
                Confirmed::DiscardEdit => {
                    self.ui.file_edit = None;
                    self.ui.file_open = false;
                }
                Confirmed::ReloadEdit => self.reload_file(),
                Confirmed::DeleteSession(id) => {
                    if self.writable() {
                        self.api(ApiCall::Delete { session: id });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                }
                Confirmed::RemoveInput { session, id } => {
                    self.api(ApiCall::Input {
                        session,
                        id,
                        action: "delete".into(),
                        before: None,
                        parts: vec![],
                    });
                }
                Confirmed::RemoveProject(id) => {
                    if self.writable() {
                        self.api(ApiCall::DeleteProject { project: id });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                }
                Confirmed::Disconnect(id) => self.api(ApiCall::Provider {
                    id,
                    sign_out: true,
                    method: String::new(),
                    key: None,
                }),
                Confirmed::DownloadStt => {
                    if self.ui.dictation.phase == crate::dictation::Phase::Consent {
                        if let Some(phase) = self
                            .ui
                            .dictation
                            .phase
                            .transition(crate::dictation::Transition::ConsentGranted)
                        {
                            self.ui.dictation.phase = phase;
                            self.api(ApiCall::VoiceDownload {
                                generation: self.ui.dictation.generation,
                            });
                        }
                    }
                }
                Confirmed::DownloadVoice(target) => {
                    self.api(ApiCall::VoiceModel {
                        target,
                        operation: VoiceOperation::Download,
                    });
                }
                Confirmed::RemoveVoice { target, .. } => {
                    self.api(ApiCall::VoiceModel {
                        target,
                        operation: VoiceOperation::Remove,
                    });
                }
            }
        } else if d.action == Confirmed::DownloadStt {
            self.cancel_dictation();
        }
    }

    fn send(&mut self) {
        if self.manager_debug_view() {
            self.toast(Level::Info, "Manager session · read-only debug view");
            return;
        }
        self.send_mode(None);
    }

    fn send_mode(&mut self, requested: Option<&str>) {
        self.send_mode_confirmed(requested, false);
    }

    fn ask_resume(&mut self, action: Confirmed) {
        let restore = self.restore_point();
        self.ui.dialog = Some(Dialog {
            title: "Resume archived session".into(),
            body: vec!["This moves the session and its children out of the archive. The next request may start with an uncached large context, which can be slower and cost more. Archived code kernels restart with fresh state.".into()],
            cancel: "Cancel".into(),
            confirm: "Resume".into(),
            danger: false,
            action,
            button: 0,
            restore,
        });
    }

    fn send_mode_confirmed(&mut self, requested: Option<&str>, resume: bool) {
        if self.question_pending() {
            // The draft is kept; the question card's answer is the only send.
            self.toast(Level::Info, QUESTION_LOCK);
            return;
        }
        if self.ui.image_pasting.contains(&self.draft_key()) {
            self.toast(Level::Info, "Preparing clipboard image…");
            return;
        }
        let text = self.composer_ref().text();
        if text.trim().is_empty() && self.composer_ref().images.is_empty() {
            // With a question waiting, Enter confirms the highlighted option
            // (or says the answer needs text) instead of being a silent no-op.
            if self.ask_current().is_some() {
                return self.ask_confirm();
            }
            return;
        }
        if let Some(command) = text.strip_prefix('!') {
            if !self.writable() {
                self.toast(Level::Warning, READ_ONLY);
                return;
            }
            let command = command.trim();
            match self.viewed_id().map(str::to_owned) {
                Some(session) if !command.is_empty() => {
                    self.composer().take();
                    self.api(ApiCall::Shell {
                        session,
                        command: command.to_owned(),
                    });
                }
                Some(_) => self.toast(Level::Info, "Type a command after ! to run it with bash"),
                None => self.toast(Level::Info, "Open a session to run a shell command"),
            }
            return;
        }
        // `/name args` runs the command with that slash name.
        if !text.contains('\n') {
            if let Some(rest) = text.trim().strip_prefix('/') {
                let (word, arg) = rest.split_once(' ').unwrap_or((rest, ""));
                if let Some(c) = self.manifest.slash(word).cloned() {
                    self.composer().take();
                    let arg = arg.trim();
                    if c.id == "manager.start" {
                        self.manager_slash(arg);
                        return;
                    }
                    if c.id == "session.search" {
                        self.open_sessions_search(arg);
                        return;
                    }
                    if c.id == "session.rename" && !arg.is_empty() {
                        if let Some(s) = self.viewed() {
                            let call = ApiCall::Rename {
                                session: s.id.clone(),
                                title: arg.to_owned(),
                                revision: s.revision,
                            };
                            if self.writable() {
                                self.api(call);
                            } else {
                                self.toast(Level::Warning, READ_ONLY);
                            }
                            return;
                        }
                    }
                    if c.id == "session.btw" && !arg.is_empty() {
                        if let Some(id) = self.viewed_id().map(str::to_owned) {
                            if self.writable() {
                                self.ui.btw =
                                    Some(crate::btw::Btw::new(id.clone(), arg.to_owned()));
                                self.open_overlay(OverlayKind::ContextDetail {
                                    title: format!("Side question · {arg}"),
                                    text: self.ui.btw.as_ref().unwrap().detail(),
                                    top: 0,
                                });
                                self.api(ApiCall::Btw {
                                    session: id,
                                    question: arg.to_owned(),
                                    request_id: self.ui.btw.as_ref().unwrap().request_id.clone(),
                                });
                            } else {
                                self.toast(Level::Warning, READ_ONLY);
                            }
                            return;
                        }
                    }
                    if c.id == "skills.learn" {
                        if self.availability(&c).is_err() {
                            return self.run_command(&c.id);
                        }
                        if arg.is_empty() {
                            self.toast(
                                Level::Info,
                                "Usage: /learn owner/repo, a GitHub URL, ./folder, or npx skills add …",
                            );
                        } else if !self.writable() {
                            self.toast(Level::Warning, READ_ONLY);
                        } else {
                            self.ui.learn_source = Some(arg.to_owned());
                            self.open_palette(PaletteMode::LearnScope);
                        }
                        return;
                    }
                    self.run_command(&c.id);
                    return;
                }
            }
        }
        if !self.writable() {
            self.toast(Level::Warning, READ_ONLY);
            return;
        }
        match self.viewed_id().map(str::to_owned) {
            Some(id) => {
                let archived = self.viewed().is_some_and(|s| s.archived);
                if archived && !resume {
                    self.ask_resume(Confirmed::ResumeArchived {
                        session: id,
                        mode: requested.map(str::to_owned),
                    });
                    return;
                }
                let unarchive = if archived {
                    self.root_session(&id)
                } else {
                    None
                };
                let mode = requested
                    .unwrap_or(if self.is_busy(&id) { "steer" } else { "send" })
                    .to_owned();
                let draft = Box::new(self.composer_ref().clone());
                let pastes = draft
                    .pastes()
                    .iter()
                    .map(|(marker, text)| ((*marker).to_owned(), (*text).to_owned()))
                    .collect();
                self.composer().take();
                let images = std::mem::take(&mut self.composer().images);
                self.api(ApiCall::Prompt {
                    session: id,
                    unarchive,
                    text,
                    images,
                    pastes,
                    mode,
                    draft: Some(draft),
                });
            }
            None => match self.current_project() {
                Some(project) => {
                    if self
                        .ui
                        .first_prompts
                        .get(&project)
                        .is_some_and(|p| p.session.is_none())
                    {
                        return;
                    }
                    let draft = Box::new(self.composer_ref().clone());
                    let pastes = draft
                        .pastes()
                        .iter()
                        .map(|(marker, text)| ((*marker).to_owned(), (*text).to_owned()))
                        .collect();
                    self.composer().take();
                    let fields = self.ui.draft.fields();
                    let images = std::mem::take(&mut self.composer().images);
                    self.ui.first_prompts.insert(
                        project.clone(),
                        PendingFirstPrompt {
                            text: text.clone(),
                            images: images.len(),
                            session: None,
                            history_loaded: false,
                            choices: self.ui.draft.clone(),
                        },
                    );
                    self.api(ApiCall::NewAndPrompt {
                        images,
                        pastes,
                        project,
                        text,
                        fields,
                        draft: Some(draft),
                    });
                }
                None => self.toast(
                    Level::Warning,
                    "No project yet: add one from the Projects tab, then it appears here.",
                ),
            },
        }
    }

    pub fn pending_first_prompt(&self) -> Option<&PendingFirstPrompt> {
        let project = self.current_project()?;
        let pending = self.ui.first_prompts.get(&project)?;
        (pending.session.as_deref() == self.viewed_id() && !self.first_prompt_confirmed(pending))
            .then_some(pending)
    }

    /// The line a pinned first message starts at, while its pin holds.
    pub fn first_pin_top(&self, blocks: &[crate::timeline::Block]) -> Option<usize> {
        let pin = self.ui.first_pin.as_ref()?;
        if self.viewed_id() != Some(pin.session.as_str()) {
            return None;
        }
        blocks
            .iter()
            .find(|block| {
                matches!(
                    block.kind,
                    crate::timeline::BlockKind::User | crate::timeline::BlockKind::Turn
                )
            })
            .map(|block| block.start)
    }

    fn first_prompt_confirmed(&self, pending: &PendingFirstPrompt) -> bool {
        self.messages().iter().any(|m| m.role == "user")
            && (pending.history_loaded
                || self
                    .state
                    .model
                    .selected
                    .as_ref()
                    .is_some_and(|s| s.starting.is_some()))
    }

    fn finish_first_prompt(&mut self) {
        let Some(project) = self.current_project() else {
            return;
        };
        let Some(pending) = self.ui.first_prompts.get(&project) else {
            return;
        };
        if pending.session.as_deref() != self.viewed_id() || !self.first_prompt_confirmed(pending) {
            return;
        }
        let pending = self.ui.first_prompts.remove(&project).unwrap();
        if let Some(session) = self.viewed_id().map(str::to_owned) {
            self.ui.first_pin = Some(FirstPin {
                session,
                applied: None,
            });
        }
        if self.ui.draft == pending.choices {
            self.ui.draft = Draft::default();
        }
    }

    /// Ask Core for the current project's prompt history; the reply refills the composer's recall list.
    pub fn refresh_history(&mut self) {
        if let Some(project) = self.current_project() {
            self.api(ApiCall::Prompts { project });
        }
    }

    // ---- sync & results ------------------------------------------------------------------

    /// Apply one sync message and report whether it may have changed the view.
    ///
    /// The flag is computed before the message moves, so callers can batch
    /// without cloning. `apply_sync` itself always runs exactly once.
    pub fn apply_sync_tracked(&mut self, msg: SyncMsg) -> bool {
        let affects_view = sync_msg_affects_view(&msg);
        self.apply_sync(msg);
        affects_view
    }

    /// The timeline for `width`, rebuilt only when one of its inputs changed.
    ///
    /// Model inputs are covered by `timeline_revision` (bumped by every sync
    /// message and result); the rest are hashed here: the viewed session, its
    /// width, resolved theme, focus/selection, fold/raw state and detail level.
    /// Animation is an input only when the last build baked a spinner, shimmer
    /// or flash into its lines, and wall-clock seconds only when it baked a live
    /// duration; a settled timeline survives ticks and other sessions' turns
    /// unchanged. Chrome-only changes (typing, scrolling, toasts, status)
    /// therefore reuse the lines, blocks and hit-target indices of the last
    /// build. Non-selected session events stay conservative: every sync message
    /// bumps the revision, so child cards and waits cannot go stale.
    pub fn timeline_for(&self, width: usize) -> std::rc::Rc<crate::timeline::Timeline> {
        let cache = self.timeline_cache.borrow();
        if let Some((cached, timeline)) = cache.as_ref() {
            // The animation frame and the wall clock only belong in the key when
            // the last build actually baked them into a glyph or a duration.
            // Otherwise a tick is a chrome-only change and reuse is exact.
            let key = self.timeline_cache_key(width, timeline.animated, timeline.clocked);
            if *cached == key {
                return std::rc::Rc::clone(timeline);
            }
        }
        drop(cache);
        let timeline =
            std::rc::Rc::new(crate::timeline::build(self, &self.theme, self.frame, width));
        // Key what was built, not what was assumed: the flags are only known
        // after the build, and they are exactly the inputs a later frame may
        // share or differ by.
        let key = self.timeline_cache_key(width, timeline.animated, timeline.clocked);
        *self.timeline_cache.borrow_mut() = Some((key, std::rc::Rc::clone(&timeline)));
        self.timeline_builds.set(self.timeline_builds.get() + 1);
        timeline
    }

    /// Drop the cached timeline explicitly (tests and direct model edits).
    pub fn invalidate_timeline(&self) {
        self.timeline_revision
            .set(self.timeline_revision.get().wrapping_add(1));
    }

    fn timeline_cache_key(&self, width: usize, animated: bool, clocked: bool) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        self.timeline_revision.get().hash(&mut h);
        self.viewed_id().hash(&mut h);
        width.hash(&mut h);
        crate::timeline::theme_fingerprint(&self.theme).hash(&mut h);
        (self.ui.detail as u8).hash(&mut h);
        (self.ui.view as u8).hash(&mut h);
        self.ui.focus.hash(&mut h);
        self.ui.timeline_sel.hash(&mut h);
        if animated && !self.theme.opts.reduced_motion {
            // The spinner/shimmer phase is only an input when the cached lines
            // actually baked one in (a settled session has no animated glyph).
            self.frame.hash(&mut h);
        }
        let mut opens: Vec<(&String, &bool)> = self.ui.timeline_open.iter().collect();
        opens.sort_by(|a, b| a.0.cmp(b.0));
        for (key, open) in opens {
            key.hash(&mut h);
            open.hash(&mut h);
        }
        let mut flash: Vec<(&String, &usize)> = self.ui.group_flash.iter().collect();
        flash.sort_by(|a, b| a.0.cmp(b.0));
        for (key, at) in flash {
            key.hash(&mut h);
            at.hash(&mut h);
        }
        let busy = self
            .state
            .model
            .selected
            .as_ref()
            .is_some_and(|view| self.is_busy(&view.session_id));
        if busy && clocked {
            // A running tool or busy child card shows wall-clock seconds; a turn
            // footer's duration comes from stored times and stays put.
            (crate::app::now_secs() as u64).hash(&mut h);
        }
        h.finish()
    }

    /// The key for the new-session starting-context preview.
    ///
    /// Every sync message bumps `timeline_revision`, which covers the catalog,
    /// the agent files, Core's draft plan and the project inventories; the
    /// local draft switches and the layout parameters are hashed here. The
    /// instruction file is read once per path (`render.rs`), so the project in
    /// the key covers it.
    pub fn preview_key(
        &self,
        width: usize,
        sep: &str,
        ell: &str,
        ascii: bool,
        window: Option<u64>,
    ) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        self.timeline_revision.get().hash(&mut h);
        self.viewed_id().hash(&mut h);
        width.hash(&mut h);
        sep.hash(&mut h);
        ell.hash(&mut h);
        ascii.hash(&mut h);
        window.hash(&mut h);
        self.current_project().hash(&mut h);
        self.current_agent().hash(&mut h);
        self.ui.draft.agent.hash(&mut h);
        self.ui.draft.model.hash(&mut h);
        self.ui.draft.disabled_tools.hash(&mut h);
        h.finish()
    }

    pub fn apply_sync(&mut self, msg: SyncMsg) {
        if let SyncMsg::History(history) = &msg {
            for pending in self.ui.first_prompts.values_mut() {
                if pending.session.as_deref() == Some(history.session_id.as_str()) {
                    pending.history_loaded = true;
                }
            }
        }
        self.timeline_revision
            .set(self.timeline_revision.get().wrapping_add(1));
        if let SyncMsg::SkillMcp {
            project,
            skills,
            mcp,
            project_mcp,
        } = msg
        {
            let mut servers: Vec<Value> = project_mcp
                .as_ref()
                .ok()
                .and_then(|value| value["servers"].as_array())
                .cloned()
                .unwrap_or_default();
            let duplicates = project_mcp
                .as_ref()
                .ok()
                .and_then(|value| value["duplicates"].as_array())
                .map_or(0, Vec::len);
            // The project endpoint is the actual MCP-file inventory, including
            // enabled, disabled, failed and invalid entries. Its rows win over a
            // global row of the same name: a disabled global default (such as
            // browser-use) must not hide the project's enabled entry. An enabled
            // global collision arrives as an invalid project row and still wins.
            if let Some(rows) = mcp
                .as_ref()
                .ok()
                .and_then(|value| value["servers"].as_array())
            {
                for row in rows {
                    if !servers
                        .iter()
                        .any(|server| server["server"] == row["server"])
                    {
                        servers.push(row.clone());
                    }
                }
            }
            self.ui.inventories.insert(
                project,
                ProjectInventory {
                    skills: skills
                        .as_ref()
                        .ok()
                        .and_then(|value| value["skills"].as_array())
                        .cloned()
                        .unwrap_or_default(),
                    mcp: servers,
                    skills_error: skills.err(),
                    mcp_error: mcp.err(),
                    project_mcp_error: project_mcp.err(),
                    project_mcp_duplicates: duplicates,
                },
            );
            return;
        }
        if let SyncMsg::Completion {
            project,
            session,
            query,
            limit,
            data,
        } = msg
        {
            return self.completion_reply(project, session, query, limit, data);
        }
        if let SyncMsg::Config(Ok(config)) = &msg {
            if let Some(detail) = config["values"]["ui"]["detail"]
                .as_str()
                .and_then(crate::ui::Detail::from_label)
            {
                self.ui.detail = detail;
            }
            if let Some(reduced) = config["values"]["ui"]["reduced_motion"].as_bool() {
                let mut options = self.theme.opts;
                options.reduced_motion = reduced;
                self.theme = self.theme.with(options);
            }
        }
        if let SyncMsg::Manager { tag, result } = msg {
            return self.manager_reply(&tag, result);
        }
        if let SyncMsg::Event(event) = &msg {
            if let Some(btw) = self.ui.btw.as_mut() {
                if btw.event(event) {
                    if let Some(Overlay {
                        kind: OverlayKind::ContextDetail { text, .. },
                        ..
                    }) = self.ui.overlay.as_mut()
                    {
                        *text = btw.detail();
                    }
                }
            }
            if event.session_id.is_some()
                && !matches!(
                    event.kind.as_str(),
                    "text_delta" | "reasoning_delta" | "wire_event" | "wire_response"
                )
            {
                let now = self.clock;
                if let Some(i) = self.inspector_mut() {
                    let relevant = event.kind == "session.created"
                        || event.session_id.as_deref() == Some(i.session.as_str())
                        || i.data()["tree"].as_array().is_some_and(|tree| {
                            tree.iter()
                                .any(|node| node["id"].as_str() == event.session_id.as_deref())
                        });
                    if relevant {
                        i.invalidate(now);
                    }
                }
            }
        }
        if matches!(&msg, SyncMsg::Snapshot { .. }) {
            let now = self.clock;
            if let Some(i) = self.inspector_mut() {
                i.invalidate(now);
            }
        }
        if let SyncMsg::Event(event) = &msg {
            if event.kind.starts_with("manager.") {
                self.manager_event(event);
            }
        }
        if matches!(&msg, SyncMsg::Snapshot { .. }) {
            self.manager_restore();
        }
        if let SyncMsg::Event(event) = &msg {
            if event.kind == "session.handoff" && event.data["reason"].as_str() == Some("auto") {
                if let Some(to) = event.data["to"].as_str() {
                    if event.session_id.as_deref() == self.viewed_id() {
                        self.toast(
                            Level::Info,
                            "Context was nearly full: continued in a new session",
                        );
                        self.open_session(to);
                    }
                }
            }
            if event.kind == "session.handoff_failed" {
                let error = event.data["error"].as_str().unwrap_or("unknown error");
                self.toast(Level::Warning, format!("Automatic handoff failed: {error}"));
            }
            if event.kind == "provider.login" {
                // Settings shows a pending sign-in's code on the provider's row; the rest is a toast.
                let provider = event.data["provider"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned();
                let status = event.data["status"].as_str().unwrap_or_default().to_owned();
                if status == "pending" && self.ui.settings_page.as_deref() != Some("providers") {
                    if let Some(message) = event.data["message"].as_str() {
                        self.toast(Level::Info, message.to_owned());
                    }
                }
                match status.as_str() {
                    "connected" => self.toast(Level::Success, format!("{provider} connected")),
                    "failed" => self.toast(
                        Level::Error,
                        event.data["message"]
                            .as_str()
                            .unwrap_or("Sign-in failed")
                            .to_owned(),
                    ),
                    _ => {}
                }
                if status == "signed_out" {
                    self.ui.provider_logins.remove(&provider);
                } else {
                    self.ui.provider_logins.insert(provider, event.data.clone());
                }
                if status != "pending" {
                    self.effects
                        .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
                }
            }
            if event.kind == "agents.changed" && self.ui.view == View::Settings {
                self.effects
                    .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
            }
        }
        let project_before = self.current_project();
        let global_event = match &msg {
            SyncMsg::Event(event) => Some((event.kind.clone(), event.data.clone())),
            _ => None,
        };
        let attention = match &msg {
            SyncMsg::Event(event) => event
                .session_id
                .as_deref()
                .and_then(|id| Some((self.root_session(id)?, id.to_owned())))
                .map(|(root, sid)| (root, sid, event.kind.clone(), event.data.clone())),
            _ => None,
        };
        let preserve_side = matches!(&msg, SyncMsg::Snapshot { .. })
            || matches!(&msg, SyncMsg::Event(event) if matches!(event.kind.as_str(), "worktree.listed" | "session.created" | "session.updated" | "session.deleted" | "session.archived" | "session.unarchived"));
        let focused_session = preserve_side
            .then(|| {
                self.side_entries()
                    .get(self.cursor(Tab::Sessions))
                    .map(|entry| entry.id.clone())
            })
            .flatten();
        let applied_before = self.state.events_applied;
        let snapshot = matches!(&msg, SyncMsg::Snapshot { .. });
        let question_asked = global_event
            .as_ref()
            .is_some_and(|(kind, _)| kind == "question.asked");
        let question_replied = global_event
            .as_ref()
            .filter(|(kind, _)| kind == "question.replied")
            .and_then(|(_, data)| data["id"].as_str().map(str::to_owned));
        let archive_event = global_event
            .as_ref()
            .is_some_and(|(kind, _)| kind == "session.archived");
        let before_archived: HashMap<String, bool> = if snapshot {
            self.state
                .model
                .sessions
                .iter()
                .map(|(id, session)| (id.clone(), session.archived))
                .collect()
        } else {
            HashMap::new()
        };
        let archive_ids: HashSet<String> = if archive_event {
            let mut ids = HashSet::new();
            if let Some((_, data)) = &global_event {
                if let Some(id) = data["root"].as_str() {
                    ids.insert(id.to_owned());
                }
                if let Some(list) = data["sessions"].as_array() {
                    ids.extend(list.iter().filter_map(|v| v.as_str()).map(str::to_owned));
                }
            }
            ids
        } else {
            HashSet::new()
        };
        let refresh = match &msg {
            SyncMsg::Event(event)
                if event.kind == "session.directory_changed"
                    && event.session_id.as_deref() == self.viewed_id() =>
            {
                event.session_id.clone()
            }
            _ => None,
        };
        let turn_closed = matches!(&msg, SyncMsg::Event(e) if e.kind == "turn.summary");
        let catalog = matches!(&msg, SyncMsg::Catalog { .. });
        let file_read = matches!(&msg, SyncMsg::Files { file: true, .. });
        let notice_event = match &msg {
            SyncMsg::Event(event) => Some((event.kind.clone(), event.session_id.clone())),
            _ => None,
        };
        let notice_root = notice_event
            .as_ref()
            .and_then(|(_, id)| id.as_deref())
            .and_then(|id| self.root_session(id));
        let was_busy = notice_root.as_deref().is_some_and(|id| self.tree_busy(id));
        self.state.apply(msg);
        if archive_event && self.state.events_applied > applied_before {
            // The event names every session it archived (root and descendants);
            // unrelated archived tabs the user reopened stay readable.
            let doomed = self.archived_tabs_where(|id| archive_ids.contains(id));
            self.close_tabs(&doomed);
        }
        if snapshot {
            // Close only tabs that became archived while this client was away
            // (or the initial snapshot's stale restored tabs). An archived
            // session the user opened on purpose stays open on later snapshots.
            let doomed =
                self.archived_tabs_where(|id| !before_archived.get(id).copied().unwrap_or(false));
            self.close_tabs(&doomed);
        }
        if self.state.events_applied > applied_before {
            if question_asked {
                if let Some((_, data)) = global_event
                    .as_ref()
                    .filter(|(kind, _)| kind == "question.asked")
                {
                    if let Some(id) = data["id"].as_str() {
                        self.start_question(id);
                    }
                }
                self.focus_question_card();
            }
            if let Some(id) = question_replied {
                self.question_closed(&id);
            }
        }
        if snapshot {
            self.prepare_questions();
        }
        self.adopt_view_scroll();
        // Launched from a directory: open it as a project (Core reuses one at the same path).
        if snapshot {
            if let Some(path) = self.ui.launch_dir.take() {
                match self.project_at_path(&path) {
                    // Already registered: open it silently, with no add call or toast.
                    Some(id) => {
                        self.select_project_quietly(&id);
                        self.show_new_session();
                    }
                    None => {
                        self.ui.launch_new = true;
                        self.api(ApiCall::AddProject { path });
                    }
                }
            }
        }
        if file_read {
            self.file_read();
        }
        if turn_closed || snapshot {
            self.refresh_history();
        }
        // Model names and the default model come with the catalog; ask once
        // the workspace is known so the composer never says "default".
        if snapshot && self.state.models.is_empty() && self.state.last_model.is_empty() {
            self.effects
                .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
        }
        // Nothing was ever chosen (Core refuses a draft without a model): ask first.
        if catalog
            && !self.ui.model_prompted
            && self.ui.overlay.is_none()
            && self.ui.view == View::Session
            && self.viewed().is_none()
            && !self.state.models.is_empty()
            && self.effective_model(None).is_empty()
        {
            self.ui.model_prompted = true;
            self.open_palette(PaletteMode::Models);
        }
        if self.state.events_applied > applied_before {
            if self.ui.sound {
                if let Some((kind, _)) = &notice_event {
                    if notice_root
                        .as_deref()
                        .is_some_and(|id| was_busy && !self.tree_busy(id))
                    {
                        self.effects.push(Effect::Sound(Sound::Finished));
                    }
                    if matches!(kind.as_str(), "permission.asked" | "question.asked") {
                        self.effects.push(Effect::Sound(Sound::Input));
                    }
                }
            }
            if let Some((kind, data)) = global_event {
                match kind.as_str() {
                    "skills.changed" => {
                        let own_learn = self.ui.learn_install_pending
                            || self
                                .ui
                                .learn_suppress_until
                                .is_some_and(|until| self.clock < until);
                        let debounce = self.ui.last_skills_changed_toast.is_some_and(|at| {
                            self.clock.saturating_duration_since(at)
                                < std::time::Duration::from_secs(3)
                        });
                        if !own_learn && !debounce {
                            self.toast(Level::Info, "Skills changed. New sessions will use them.");
                            self.ui.last_skills_changed_toast = Some(self.clock);
                        }
                        self.ui.draft_asked = None;
                        if let Some(project) = self.current_project() {
                            self.effects
                                .push(Effect::Sync(SyncCommand::SkillMcp(project)));
                        }
                    }
                    "mcp.changed" => {
                        let debounce = self.ui.last_mcp_changed_toast.is_some_and(|at| {
                            self.clock.saturating_duration_since(at)
                                < std::time::Duration::from_secs(3)
                        });
                        if !debounce {
                            self.toast(
                                Level::Info,
                                "MCP servers changed. New sessions will use them.",
                            );
                            self.ui.last_mcp_changed_toast = Some(self.clock);
                        }
                        let project = data["project_id"]
                            .as_str()
                            .map(str::to_owned)
                            .or_else(|| self.current_project());
                        if let Some(project) = project {
                            if data["scope"].as_str() == Some("project")
                                || data["project_id"].is_string()
                            {
                                self.refresh_project_mcp_inventory(project);
                            }
                        }
                    }
                    "git.changed" => {
                        if self.ui.tab == Tab::Git
                            && self.current_project().as_deref() == data["project_id"].as_str()
                        {
                            if let Some(project) = self.current_project() {
                                self.api(ApiCall::ProjectGit {
                                    project,
                                    worktree: self.git_worktree(),
                                });
                            }
                        }
                    }
                    "files.changed" => {
                        self.disk_files_changed(&data);
                        if data["source"].as_str() != Some("disk") {
                            self.file_changed(&data);
                        }
                    }
                    "files.watch" => {
                        if self.current_project().as_deref() == data["project_id"].as_str()
                            && self.files_root().as_deref() == data["workspace"].as_str()
                        {
                            self.file_watch_status =
                                data["status"].as_str().unwrap_or("").to_owned();
                        }
                    }
                    "mcp.failed" => self.toast(
                        Level::Warning,
                        format!(
                            "MCP server {} failed: {}",
                            data["server"].as_str().unwrap_or("unknown"),
                            data["error"].as_str().unwrap_or("unknown error")
                        ),
                    ),
                    "voice.progress" => self.apply_voice_progress(&data),
                    _ => {}
                }
            }
            if let Some((root, sid, kind, data)) = attention {
                let viewed_root = self.viewed_id().and_then(|id| self.root_session(id));
                // A failure belongs to the session whose turn failed, not its root: a
                // child's error already reaches the parent's model as a tool result, and
                // the parent answering it is not a failed session. A stop the user asked
                // for is not a failure.
                if kind == "session.error" && data["kind"] != "interrupted" {
                    self.ui.failed_sessions.insert(sid.clone());
                }
                if kind == "session.status" && data["status"] == "busy" {
                    self.ui.failed_sessions.remove(&sid);
                }
                if viewed_root.as_deref() != Some(root.as_str()) {
                    if let Some(text) = self.notice(&root, &kind) {
                        self.effects.push(Effect::Notify(text));
                    }
                }
                if viewed_root.as_deref() != Some(root.as_str())
                    && (kind == "session.error"
                        || (kind == "message.added" && data["role"] == "assistant"))
                {
                    self.ui.unread_sessions.insert(root);
                }
            }
        }
        if preserve_side {
            let entries = self.side_entries();
            let index = focused_session
                .as_ref()
                .and_then(|id| entries.iter().position(|entry| &entry.id == id))
                .unwrap_or_else(|| {
                    self.cursor(Tab::Sessions)
                        .min(entries.len().saturating_sub(1))
                });
            self.ui.cursor.insert(Tab::Sessions, index);
        }
        if let Some(id) = refresh {
            self.effects.push(Effect::Sync(SyncCommand::Worktrees(id)));
        }
        if snapshot {
            // One repository listing covers every session in a project (main
            // checkout and its linked worktrees), so sidebar branches load for
            // all rows without one Git call per session.
            let mut projects = self
                .state
                .model
                .sessions_recent()
                .into_iter()
                .map(|s| s.project_id.clone())
                .collect::<std::collections::BTreeSet<_>>();
            if let Some(project) = self.current_project() {
                projects.insert(project);
            }
            for project in projects {
                self.effects
                    .push(Effect::Sync(SyncCommand::ProjectWorktrees(project)));
            }
            if self.viewed_id().is_none() {
                if let Some(project) = self.current_project() {
                    self.effects
                        .push(Effect::Sync(SyncCommand::SkillMcp(project)));
                }
            }
        }
        if self.viewed_id().is_none() && project_before != self.current_project() {
            if let Some(project) = self.current_project() {
                self.effects
                    .push(Effect::Sync(SyncCommand::SkillMcp(project)));
            }
        }
        if let Some(gone) = self.state.model.selected_deleted.take() {
            self.toast(Level::Info, format!("Session {gone} was deleted"));
            self.ui.drafts.remove(&gone);
            self.ui.scrolls.remove(&gone);
            self.ui.scroll = Scroll::default();
        }
        if self.state.resync_wanted.take().is_some() {
            self.effects.push(Effect::Sync(SyncCommand::Resync));
        }
        self.finish_first_prompt();
    }

    pub fn apply_done(&mut self, done: ActionDone) {
        self.timeline_revision
            .set(self.timeline_revision.get().wrapping_add(1));
        match done.call.clone() {
            ApiCall::ProjectGitDiff { project, path, .. } => {
                self.state.diff = Some((
                    format!("project:{project}"),
                    path,
                    done.result.map(|d| d.project_git.unwrap_or_default()),
                ));
                return;
            }
            ApiCall::ProjectGitLive {
                project,
                worktree,
                action,
                value,
                paths,
            } => {
                match done.result {
                    Ok(result) => {
                        let data = result.git_live.unwrap_or_default();
                        let working = data["working"].as_array().cloned().unwrap_or_default();
                        let idle = data["idle"].as_array().cloned().unwrap_or_default();
                        let ids = working
                            .iter()
                            .filter_map(|s| s["id"].as_str().map(str::to_owned))
                            .collect();
                        let target = if paths.is_empty() {
                            value.clone()
                        } else {
                            paths.join(", ")
                        };
                        let mut body = vec![format!("{} {}?", action.replace('_', " "), target)];
                        if action == "discard" {
                            body.push("This cannot be undone.".into());
                        }
                        if action == "push" && !value.is_empty() {
                            body.push(format!("Set upstream on {value}."));
                        }
                        for (label, sessions) in [("Working", working), ("Idle", idle)] {
                            for session in sessions {
                                body.push(format!(
                                    "{}: {}",
                                    label,
                                    session["title"].as_str().unwrap_or("Untitled")
                                ));
                            }
                        }
                        self.ui.dialog = Some(Dialog {
                            title: action.replace('_', " "),
                            body,
                            cancel: "Cancel".into(),
                            confirm: "Continue".into(),
                            danger: true,
                            action: Confirmed::ProjectGitAction {
                                project,
                                worktree,
                                action,
                                value,
                                paths,
                                confirmed_live: ids,
                            },
                            button: 0,
                            restore: self.restore_point(),
                        });
                    }
                    Err(error) => {
                        self.toast(Level::Error, format!("Could not check sessions: {error}"))
                    }
                }
                return;
            }
            ApiCall::ProjectGitAction {
                project,
                worktree,
                action,
                value,
                paths,
                ..
            } => {
                match done.result {
                    Ok(result) if result.git_live.is_some() => self.api(ApiCall::ProjectGitLive {
                        project,
                        worktree,
                        action,
                        value,
                        paths,
                    }),
                    Ok(result) => {
                        self.state.project_git =
                            Some((project, Ok(result.project_git.unwrap_or_default())))
                    }
                    Err(error) if error.contains("worktree_live") => {
                        self.api(ApiCall::ProjectGitLive {
                            project,
                            worktree,
                            action,
                            value,
                            paths,
                        })
                    }
                    Err(error) => self.toast(Level::Error, format!("Git action failed: {error}")),
                }
                return;
            }
            ApiCall::ProjectGit { project, worktree } => {
                if self.current_project().as_ref() != Some(&project)
                    || self.git_worktree() != worktree
                {
                    return;
                }
                self.state.project_git = Some((
                    project,
                    done.result.map(|d| d.project_git.unwrap_or_default()),
                ));
                return;
            }
            ApiCall::FileWatch {
                project,
                session,
                release,
                ..
            } => {
                if !release
                    && self
                        .file_watch
                        .as_ref()
                        .is_some_and(|(p, s, _)| p == &project && s == &session)
                {
                    self.file_watch_status = match done.result {
                        Ok(result) => result.file_watch_status.unwrap_or_default(),
                        Err(_) => "disconnected".into(),
                    };
                }
                return;
            }
            ApiCall::FileCount {
                project,
                session,
                path,
            } => {
                match done.result {
                    Ok(result) => self.ask_delete_file(project, session, path, result.file_count),
                    Err(error) => {
                        self.toast(Level::Error, format!("Could not count folder: {error}"))
                    }
                }
                return;
            }
            ApiCall::FileMutation { .. } => {
                match done.result {
                    Ok(_) => {
                        self.state.file_dirs.clear();
                        self.ui.files_expanded.clear();
                        self.request_files(String::new(), false);
                    }
                    Err(error) => {
                        self.toast(Level::Error, format!("File operation failed: {error}"))
                    }
                }
                return;
            }
            ApiCall::TerminalControl {
                generation,
                acquire,
                ..
            } => {
                if generation == self.terminal_panel.generation && self.terminal_open {
                    self.terminal_panel.control_busy = false;
                    match done.result {
                        Ok(_) => {
                            self.terminal_controlled = acquire
                                && self.terminal_panel.attachment.is_some()
                                && self.terminal_panel.terminals.iter().any(|item| {
                                    Some(&item.id) == self.terminal_panel.selected.as_ref()
                                        && item.status == "running"
                                });
                            if !self.terminal_controlled {
                                self.terminal_panel.input_queue.clear();
                            }
                            self.terminal_panel.size = None;
                            self.terminal_resize();
                        }
                        Err(error) => {
                            self.toast(Level::Error, format!("Terminal control: {error}"))
                        }
                    }
                }
                return;
            }
            ApiCall::TerminalInput { generation, .. } => {
                if generation == self.terminal_panel.generation && self.terminal_open {
                    self.terminal_panel.input_busy = false;
                    match done.result {
                        Ok(_) => {
                            self.terminal_panel.next_input += 1;
                            self.terminal_send_next();
                        }
                        Err(error) => {
                            self.terminal_controlled = false;
                            self.terminal_panel.input_queue.clear();
                            self.terminal_panel.attachment = None;
                            self.effects.push(Effect::TerminalStop);
                            self.toast(
                                Level::Error,
                                format!("Terminal input outcome indeterminate: {error}"),
                            );
                        }
                    }
                }
                return;
            }
            ApiCall::TerminalResize {
                generation,
                rows,
                cols,
                ..
            } => {
                if generation == self.terminal_panel.generation && self.terminal_open {
                    self.terminal_panel.resize_busy = false;
                    match done.result {
                        Ok(_) => {
                            self.terminal_panel.size = Some((rows, cols));
                            self.terminal_resize();
                        }
                        Err(error) => self.toast(Level::Error, format!("Terminal resize: {error}")),
                    }
                }
                return;
            }
            ApiCall::TerminalList { project } => {
                if self.current_project().as_deref() == Some(project.as_str()) && self.terminal_open
                {
                    self.terminal_panel.loading = false;
                    match done.result {
                        Ok(result) => {
                            self.terminal_panel.terminals = result
                                .terminals
                                .unwrap_or_default()
                                .into_iter()
                                .filter(|item| item.status != "closed")
                                .collect();
                            if !self.terminal_panel.terminals.is_empty() {
                                let index = self
                                    .terminal_panel
                                    .terminals
                                    .iter()
                                    .position(|item| item.status == "running")
                                    .unwrap_or(0);
                                self.terminal_panel.cursor = index + 1;
                                self.terminal_select(index);
                            }
                        }
                        Err(error) => {
                            self.toast(Level::Error, format!("Could not list terminals: {error}"))
                        }
                    }
                }
                return;
            }
            ApiCall::TerminalCreate { project, .. } => {
                self.terminal_panel.creating = false;
                match done.result {
                    Ok(result)
                        if self.current_project().as_deref() == Some(project.as_str())
                            && self.terminal_open =>
                    {
                        if let Some(terminal) = result.terminal {
                            self.terminal_panel.terminals.push(terminal);
                            self.terminal_panel.cursor = self.terminal_panel.terminals.len();
                            self.terminal_select(self.terminal_panel.cursor - 1);
                        }
                    }
                    Err(error) => {
                        self.toast(Level::Error, format!("Could not create terminal: {error}"))
                    }
                    _ => {}
                }
                return;
            }
            ApiCall::TerminalRename { id, .. } => {
                match done.result {
                    Ok(result) => {
                        if let Some(updated) = result.terminal {
                            if let Some(item) = self
                                .terminal_panel
                                .terminals
                                .iter_mut()
                                .find(|t| t.id == id)
                            {
                                *item = updated;
                            }
                        }
                    }
                    Err(error) => {
                        self.toast(Level::Error, format!("Could not rename terminal: {error}"))
                    }
                }
                return;
            }
            ApiCall::TerminalClose { id } => {
                match done.result {
                    Ok(_) => {
                        self.terminal_panel.terminals.retain(|t| t.id != id);
                        if self.terminal_panel.selected.as_deref() == Some(&id) {
                            self.effects.push(Effect::TerminalStop);
                            self.terminal_panel.generation += 1;
                            self.terminal_panel.selected = None;
                            self.terminal_panel.attachment = None;
                            self.terminal_selected = false;
                            self.terminal_focused = false;
                            self.terminal_controlled = false;
                            if !self.terminal_panel.terminals.is_empty() {
                                self.terminal_select(0);
                            }
                        }
                    }
                    Err(error) => {
                        self.toast(Level::Error, format!("Could not close terminal: {error}"))
                    }
                }
                return;
            }
            ApiCall::VoiceStatus { generation } => {
                self.apply_voice_status(generation, done.result);
                return;
            }
            ApiCall::VoicePrepare { generation } => {
                if generation == self.ui.dictation.generation
                    && self.ui.dictation.phase == crate::dictation::Phase::Starting
                {
                    self.effects.push(Effect::DictationLoadingCue(false));
                    match done.result {
                        Ok(_) => self.launch_capture(),
                        Err(error) => {
                            self.ui.dictation.phase = crate::dictation::Phase::Idle;
                            self.toast(
                                Level::Error,
                                format!("Voice model loading failed: {error}"),
                            );
                        }
                    }
                }
                return;
            }
            ApiCall::VoiceSettingsStatus { generation } => {
                self.apply_voice_settings_status(generation, done.result);
                return;
            }
            call @ (ApiCall::SearchServiceStatus | ApiCall::SearchServiceStart) => {
                self.apply_search_service(call, done.result);
                return;
            }
            ApiCall::VoiceDownload { generation } => {
                self.apply_voice_download(generation, done.result);
                return;
            }
            ApiCall::VoiceModel { target, operation } => {
                self.apply_voice_model_action(target, operation, done.result);
                return;
            }
            ApiCall::VoiceTranscribe {
                partial,
                generation,
                ..
            } => {
                self.apply_transcription(generation, partial, done.result);
                return;
            }
            ApiCall::ManagerVoice { .. }
            | ApiCall::ManagerTranscribe { .. }
            | ApiCall::ManagerLive { .. }
            | ApiCall::ManagerSpeak { .. } => {
                self.manager_done(done);
                return;
            }
            ApiCall::ManagerSpeakStop => return,
            _ => {}
        }
        let name = done.call_name();
        if let ApiCall::LearnPreview {
            project,
            source,
            scope,
        } = done.call.clone()
        {
            match done.result {
                Ok(result) => {
                    if let Some(preview) = result.learn_preview {
                        self.apply_learn_preview(project, source, scope, preview);
                    } else {
                        self.toast(Level::Error, "Learn failed: Core returned no preview");
                    }
                }
                Err(error) => self.toast(Level::Error, format!("Learn failed: {error}")),
            }
            return;
        }
        if matches!(done.call, ApiCall::LearnInstall { .. }) {
            self.ui.learn_install_pending = false;
            if done.result.is_ok() {
                self.ui.learn_suppress_until = Some(self.clock + std::time::Duration::from_secs(5));
            }
        }
        if let ApiCall::ContextEntry { name, .. } = &done.call {
            let text = match &done.result {
                Ok(r) => Ok(r.answer.clone().unwrap_or_default()),
                Err(error) => Err(format!("Could not load this: {error}")),
            };
            if let Some(Overlay {
                kind: OverlayKind::ContextEntries { entries, .. },
                ..
            }) = self.ui.overlay.as_mut()
            {
                if let Some(entry) = entries.iter_mut().find(|entry| &entry.name == name) {
                    // A server's facts are already there; Core's answer adds to them.
                    entry.full = match (entry.full.take(), text) {
                        (Some(facts), Ok(more)) if !more.trim().is_empty() => {
                            Some(format!("{facts}\n\n{more}"))
                        }
                        (Some(facts), _) => Some(facts),
                        (None, Ok(more)) if more.trim().is_empty() => {
                            Some("Nothing more recorded.".to_owned())
                        }
                        (None, Ok(more)) | (None, Err(more)) => Some(more),
                    };
                }
            }
            return;
        }
        if let ApiCall::BtwState { request_id, .. } = &done.call {
            if let Some(btw) = self.ui.btw.as_mut().filter(|b| &b.request_id == request_id) {
                match done.result {
                    Ok(r) => {
                        if let Some(state) = r.btw_state {
                            btw.snapshot(&state);
                        }
                    }
                    Err(_) => {
                        btw.refreshing = false;
                    }
                }
                if let Some(Overlay {
                    kind: OverlayKind::ContextDetail { text, .. },
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    *text = btw.detail();
                }
            }
            return;
        }
        if let ApiCall::Btw { request_id, .. } = &done.call {
            match done.result {
                Ok(r) => {
                    if let Some(btw) = self.ui.btw.as_mut().filter(|b| &b.request_id == request_id)
                    {
                        btw.session = r.answer;
                        btw.inherited_context = r.btw_context.unwrap_or_default();
                    } else if let Some(session) = r.answer {
                        self.api(ApiCall::Interrupt { session });
                    }
                }
                Err(error) => {
                    if let Some(btw) = self.ui.btw.as_mut().filter(|b| &b.request_id == request_id)
                    {
                        btw.running = false;
                        btw.synced = true;
                        btw.answer = format!("Side question failed: {error}");
                        if let Some(Overlay {
                            kind: OverlayKind::ContextDetail { text, .. },
                            ..
                        }) = self.ui.overlay.as_mut()
                        {
                            *text = btw.detail();
                        }
                    }
                }
            }
            return;
        }
        if let ApiCall::SaveFile { content, .. } = &done.call {
            if let Some(edit) = self.ui.file_edit.as_mut() {
                edit.saving = false;
            }
            match done.result {
                Ok(r) if r.conflict.is_some() => {
                    if let Some(edit) = self.ui.file_edit.as_mut() {
                        edit.stale = true;
                    }
                    self.toast(
                        Level::Warning,
                        "The file changed on disk. Reload it (palette: Reload file); your text stays",
                    );
                }
                Ok(r) => {
                    if let (Some(edit), Some(saved)) = (self.ui.file_edit.as_mut(), r.saved) {
                        if let Some(revision) = saved["revision"].as_str() {
                            edit.revision = revision.to_owned();
                        }
                        edit.saved = content.clone();
                        edit.stale = false;
                    }
                    self.toast(Level::Success, "Saved");
                }
                Err(error) => self.toast(Level::Error, format!("Save failed: {error}")),
            }
            return;
        }
        if matches!(
            &done.call,
            ApiCall::Inspector { .. }
                | ApiCall::InspectorHead { .. }
                | ApiCall::InspectorRecord { .. }
        ) {
            self.inspector_done(done);
            return;
        }
        if matches!(&done.call, ApiCall::StopJob { .. }) {
            // The job list changes now; Core's own event may arrive later.
            self.effects.push(Effect::Sync(SyncCommand::Resources));
        }
        if matches!(&done.call, ApiCall::Usage | ApiCall::RefreshUsage) {
            match done.result {
                Ok(r) => {
                    let rows = crate::usage::rows(&r.usage.unwrap_or_default(), now_secs());
                    if let Some(OverlayKind::Usage { rows: shown, top }) =
                        self.ui.overlay.as_mut().map(|o| &mut o.kind)
                    {
                        *shown = rows;
                        *top = 0;
                    }
                }
                Err(error) => {
                    if matches!(
                        self.ui.overlay.as_ref().map(|o| &o.kind),
                        Some(OverlayKind::Usage { .. })
                    ) {
                        if matches!(&done.call, ApiCall::Usage) {
                            self.close_overlay();
                        }
                        self.toast(
                            Level::Error,
                            format!("Provider usage failed to load: {error}"),
                        );
                    }
                }
            }
            return;
        }
        if let ApiCall::SwitchCost {
            session,
            model,
            agent,
            revision,
        } = &done.call
        {
            // The check is advice: when it fails or finds nothing to lose, the switch goes ahead.
            let cost = done.result.ok().and_then(|r| r.switch_cost);
            let lost = cost.as_ref().is_some_and(|c| c["cache_lost"] == true);
            let switch = Confirmed::Switch {
                session: session.clone(),
                model: model.clone(),
                agent: agent.clone(),
                revision: *revision,
            };
            match (lost, cost) {
                (true, Some(cost)) => {
                    let reasons: Vec<String> = cost["reasons"]
                        .as_array()
                        .map(|a| {
                            a.iter()
                                .filter_map(|r| r.as_str().map(str::to_owned))
                                .collect()
                        })
                        .unwrap_or_default();
                    let cached = cost["cached_tokens"].as_u64().unwrap_or(0);
                    let mut body = reasons;
                    body.push(format!(
                        "The next request starts cold: about {cached} cached tokens are read again at full price."
                    ));
                    let restore = self.restore_point();
                    self.ui.dialog = Some(Dialog {
                        title: if model.is_some() {
                            "Switch model"
                        } else {
                            "Switch agent"
                        }
                        .into(),
                        body,
                        cancel: "Keep current".into(),
                        confirm: "Switch".into(),
                        danger: false,
                        action: switch,
                        button: 0,
                        restore,
                    });
                }
                _ => self.do_switch(switch),
            }
            return;
        }
        if let ApiCall::OlderMessages { session, .. } = &done.call {
            self.ui.loading_older = false;
            if let Ok(r) = done.result {
                if let Some((messages, more)) = r.older {
                    self.state.model.prepend_messages(session, messages, more);
                }
            } else if let Err(error) = done.result {
                self.ui.older_failed = Some(session.clone());
                self.toast(
                    Level::Error,
                    format!("Older messages failed to load: {error}"),
                );
            }
            return;
        }
        if let ApiCall::Search { query, .. } = &done.call {
            // Quiet: the title matches are already on screen.
            if let Ok(r) = done.result {
                let hits = r.search.and_then(|v| v["hits"].as_array().cloned());
                self.ui.search = Some((query.clone(), hits.unwrap_or_default()));
            }
            return;
        }
        if let ApiCall::Prompts { project } = &done.call {
            // Quiet: history is a convenience. Never replace it while a recalled entry is open.
            if let (Ok(r), false) = (&done.result, self.composer_ref().in_history()) {
                if self.current_project().as_deref() == Some(project.as_str()) {
                    self.composer()
                        .set_history(r.prompts.clone().unwrap_or_default());
                }
            }
            return;
        }
        match done.result {
            Ok(r) => {
                if matches!(done.call, ApiCall::Prompt { .. }) {
                    self.refresh_history();
                }
                if let ApiCall::Input {
                    session,
                    action,
                    parts,
                    ..
                } = &done.call
                {
                    if action == "remove" {
                        self.restore_input_parts(session, r.input_parts.as_ref().unwrap_or(parts));
                    }
                }
                if let ApiCall::Interrupt { session } = &done.call {
                    // The interrupt holds the queue; bring it back to the composer
                    // instead, so nothing is stranded and nothing is lost.
                    if let Some(parts) = &r.input_parts {
                        if self.restore_input_parts(session, parts)
                            && self.viewed_id() == Some(session.as_str())
                        {
                            self.ui.pending_selected = None;
                            self.ui.focus = Focus::Composer;
                        }
                    }
                }
                if let Some(c) = r.conflict {
                    self.toast(
                        Level::Warning,
                        format!("Changed elsewhere ({c}); nothing was overwritten"),
                    );
                } else if let Some(path) = &r.path {
                    self.toast(Level::Success, format!("Exported to {path}"));
                } else if let Some(message) = &r.message {
                    self.toast(Level::Success, message.clone());
                } else if !matches!(
                    done.call,
                    ApiCall::Prompt { .. } | ApiCall::NewAndPrompt { .. }
                ) {
                    self.toast(Level::Success, done.call.done_label());
                }
                if matches!(done.call, ApiCall::Provider { .. }) {
                    // connection state and the model list changed
                    self.effects
                        .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
                }
                if let Some(mut session) = r.session {
                    // The create reply is authoritative: keep the tab from the start instead of
                    // showing the new-session screen until the session's event arrives.
                    let old = self.state.model.sessions.get(&session.id);
                    if !old.is_some_and(|o| session.revision < o.revision) {
                        if session.status.is_none() {
                            session.status = old
                                .and_then(|o| o.status.clone())
                                .or_else(|| Some("idle".into()));
                        }
                        self.state
                            .model
                            .sessions
                            .insert(session.id.clone(), session);
                    }
                }
                if let Some(id) = r.open {
                    // events for the new session may not have arrived yet
                    if let ApiCall::NewAndPrompt {
                        project,
                        text,
                        images,
                        pastes,
                        draft,
                        ..
                    } = &done.call
                    {
                        if let Some(pending) = self.ui.first_prompts.get_mut(project) {
                            pending.session = Some(id.clone());
                        }
                        self.api(ApiCall::Prompt {
                            session: id.clone(),
                            unarchive: None,
                            text: text.clone(),
                            images: images.clone(),
                            pastes: pastes.clone(),
                            mode: "send".into(),
                            draft: draft.clone(),
                        });
                    }
                    self.open_session(&id);
                    self.finish_first_prompt();
                }
                if let Some(id) = r.project {
                    // The success toast already says "Project added".
                    self.select_project_quietly(&id);
                    if std::mem::take(&mut self.ui.launch_new) {
                        self.show_new_session();
                    }
                }
            }
            Err(e) => {
                self.ui
                    .first_prompts
                    .retain(|project, pending| match &done.call {
                        ApiCall::NewAndPrompt {
                            project: origin, ..
                        } => project != origin,
                        ApiCall::Prompt { session, .. } => {
                            pending.session.as_ref() != Some(session)
                        }
                        _ => true,
                    });
                if let ApiCall::Answer { id, .. } = &done.call {
                    // The question still waits: keep the picks so the review can
                    // be revisited instead of asking everything again.
                    if let Some(draft) = self.ui.asks.get_mut(id) {
                        draft.sent = false;
                    }
                }
                let key = match &done.call {
                    ApiCall::NewAndPrompt { project, .. } => Self::new_session_draft_key(project),
                    ApiCall::Prompt { session, .. } => session.clone(),
                    ApiCall::Shell { session, .. } => session.clone(),
                    _ => String::new(),
                };
                if let Some(saved) = done.call.saved_draft() {
                    self.restore_composer(Some(&key), saved);
                } else if let Some((_, text)) = done.call.draft() {
                    self.restore_draft(Some(&key), text);
                }
                match &done.call {
                    ApiCall::Prompt {
                        session, images, ..
                    } => self
                        .ui
                        .drafts
                        .entry(session.clone())
                        .or_default()
                        .images
                        .splice(0..0, images.clone())
                        .for_each(drop),
                    ApiCall::NewAndPrompt {
                        project, images, ..
                    } => self
                        .ui
                        .drafts
                        .entry(Self::new_session_draft_key(project))
                        .or_default()
                        .images
                        .splice(0..0, images.clone())
                        .for_each(drop),
                    _ => {}
                }
                self.toast(Level::Error, format!("{name}: {e}"));
            }
        }
    }

    fn apply_voice_settings_status(&mut self, generation: u64, result: Result<Done, String>) {
        if generation != self.ui.voice_status_generation {
            return;
        }
        self.ui.voice_status_loading = false;
        match result {
            Ok(done) => match done.voice.filter(|voice| voice.is_object()) {
                Some(voice) => {
                    self.ui.voice_status = Some(voice);
                    self.ui.voice_status_error = None;
                }
                None => {
                    let error = "Core returned no voice status".to_owned();
                    self.ui.voice_status_error = Some(error.clone());
                    self.toast(Level::Warning, format!("Voice status: {error}"));
                }
            },
            Err(error) => {
                self.ui.voice_status_error = Some(error.clone());
                self.toast(Level::Warning, format!("Voice status: {error}"));
            }
        }
    }

    fn apply_voice_model_action(
        &mut self,
        target: VoiceTarget,
        operation: VoiceOperation,
        result: Result<Done, String>,
    ) {
        match result {
            Ok(done) => {
                let response = done.voice.unwrap_or(Value::Null);
                self.merge_voice_status(target, &response);
                if response["state"].as_str() == Some("error") {
                    self.toast(
                        Level::Error,
                        response["error"]
                            .as_str()
                            .unwrap_or("Voice model action failed")
                            .to_owned(),
                    );
                } else {
                    match operation {
                        VoiceOperation::Download => self.toast(
                            Level::Info,
                            format!(
                                "Downloading {} model; progress is shown in Settings",
                                target.label()
                            ),
                        ),
                        VoiceOperation::Remove => {
                            let repo = response["repo"]
                                .as_str()
                                .or_else(|| {
                                    self.voice_status_for(target)
                                        .and_then(|status| status["repo"].as_str())
                                })
                                .unwrap_or("voice model");
                            let freed = response["freed"].as_u64().map_or_else(
                                || format!("Removed {repo} from the Hugging Face cache"),
                                |bytes| {
                                    format!(
                                        "Removed {repo} from the Hugging Face cache · freed {}",
                                        format_bytes(bytes)
                                    )
                                },
                            );
                            self.toast(Level::Success, freed);
                        }
                    }
                }
            }
            Err(error) => self.toast(
                Level::Error,
                format!("{} model action failed: {error}", target.label()),
            ),
        }
        // A download can start asynchronously and a remove can race another
        // client. Always read Core's authoritative state after the response.
        self.refresh_voice_settings_status();
    }

    fn merge_voice_status(&mut self, target: VoiceTarget, response: &Value) {
        let update = response
            .get("status")
            .filter(|status| status.is_object())
            .unwrap_or(response);
        let Some(fields) = update.as_object() else {
            return;
        };
        let voice = self.ui.voice_status.get_or_insert_with(|| {
            json!({
                "stt": {},
                "tts": {},
            })
        });
        let Some(model) = voice.get_mut(target.id()).and_then(Value::as_object_mut) else {
            return;
        };
        for (key, value) in fields {
            if key != "freed" {
                model.insert(key.clone(), value.clone());
            }
        }
    }

    fn apply_voice_status(&mut self, generation: u64, result: Result<Done, String>) {
        use crate::dictation::{DownloadProgress, Phase, Transition};
        if generation != self.ui.dictation.generation || self.ui.dictation.phase != Phase::Checking
        {
            return;
        }
        let status = match result {
            Ok(done) => done.voice.unwrap_or(Value::Null)["stt"].clone(),
            Err(error) => {
                self.ui.dictation.phase = Phase::Idle;
                self.toast(Level::Error, format!("Voice status failed: {error}"));
                return;
            }
        };
        match status["state"].as_str().unwrap_or("error") {
            "ready" | "loaded" => self.start_capture(status["state"].as_str() == Some("loaded")),
            "absent" => {
                if let Some(phase) = self.ui.dictation.phase.transition(Transition::StatusAbsent) {
                    self.ui.dictation.phase = phase;
                    self.ask_voice_download(&status);
                }
            }
            "downloading" => {
                if let Some(phase) = self
                    .ui
                    .dictation
                    .phase
                    .transition(Transition::StatusDownloading)
                {
                    self.ui.dictation.phase = phase;
                    self.ui.dictation.download = Some(DownloadProgress {
                        state: "downloading".into(),
                        bytes_done: status["bytes_done"].as_u64().unwrap_or(0),
                        bytes_total: status["size"].as_u64().unwrap_or(0),
                        error: None,
                    });
                    self.toast(Level::Info, "Speech model download is already in progress");
                }
            }
            "runtime_missing" | "unsupported" | "error" => {
                self.ui.dictation.phase = Phase::Idle;
                self.toast(
                    Level::Warning,
                    status["error"]
                        .as_str()
                        .unwrap_or("Speech recognition is not available on this platform"),
                );
            }
            other => {
                self.ui.dictation.phase = Phase::Idle;
                self.toast(Level::Warning, status["error"].as_str().unwrap_or(other));
            }
        }
    }

    fn apply_voice_download(&mut self, generation: u64, result: Result<Done, String>) {
        use crate::dictation::Phase;
        if generation != self.ui.dictation.generation
            || self.ui.dictation.phase != Phase::Downloading
        {
            return;
        }
        match result {
            Ok(done) => {
                let info = done.voice.unwrap_or(Value::Null);
                if info["state"].as_str() == Some("error") {
                    let error = info["error"]
                        .as_str()
                        .unwrap_or("STT model download failed");
                    self.ui.dictation.phase = Phase::Idle;
                    self.ui.dictation.download = None;
                    self.toast(Level::Error, error);
                } else {
                    self.toast(
                        Level::Info,
                        "Downloading speech model; progress is shown above the composer",
                    );
                }
            }
            Err(error) => {
                self.ui.dictation.phase = Phase::Idle;
                self.ui.dictation.download = None;
                self.toast(
                    Level::Error,
                    format!("Voice model download failed: {error}"),
                );
            }
        }
    }

    fn apply_transcription(
        &mut self,
        generation: u64,
        partial: bool,
        result: Result<Done, String>,
    ) {
        use crate::dictation::{Phase, Transition};
        if generation != self.ui.dictation.generation {
            return;
        }
        if partial {
            self.ui.dictation.pacer.finish();
            match result {
                Ok(done) => {
                    if let Some(text) = done.transcript.filter(|text| !text.trim().is_empty()) {
                        self.ui.dictation.set_preview(text);
                    }
                }
                Err(error) if error == "voice_busy" => {}
                Err(error) => {
                    self.toast(Level::Warning, format!("Dictation preview failed: {error}"))
                }
            }
            if self.ui.dictation.final_wav.is_some() {
                self.send_final_if_ready();
            } else {
                self.maybe_preview();
            }
            return;
        }
        if self.ui.dictation.phase != Phase::Transcribing {
            return;
        }
        match result {
            Ok(done) => {
                if let Some(text) = done.transcript.filter(|text| !text.trim().is_empty()) {
                    let key = self.ui.dictation.draft_key.clone();
                    let at = self.ui.dictation.anchor;
                    self.ui
                        .drafts
                        .entry(key)
                        .or_default()
                        .insert_transcript(at, &text);
                } else {
                    self.toast(Level::Info, "No speech was recognized");
                }
            }
            Err(error) => self.toast(Level::Error, format!("Dictation failed: {error}")),
        }
        if let Some(phase) = self.ui.dictation.phase.transition(Transition::Finished) {
            self.ui.dictation.phase = phase;
        }
        self.ui.dictation.preview.clear();
        self.ui.dictation.final_wav = None;
        self.ui.dictation.pacer.finish();
        let send = std::mem::take(&mut self.ui.dictation.send_after);
        if send && self.ui.dictation.draft_key == self.draft_key() {
            self.send();
        }
    }

    fn apply_learn_preview(
        &mut self,
        project: String,
        source: String,
        scope: String,
        preview: Value,
    ) {
        let found = preview["found"].as_array().cloned().unwrap_or_default();
        if found.is_empty() {
            let detail = preview["problems"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|problem| problem.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            self.toast(
                Level::Info,
                if detail.is_empty() {
                    format!("No skills found in {source}")
                } else {
                    format!("No skills found in {source}: {detail}")
                },
            );
            return;
        }
        let requested: Vec<String> = preview["requested"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        let names: Vec<String> = found
            .iter()
            .filter_map(|skill| skill["name"].as_str().map(str::to_owned))
            .collect();
        let requested_are_new = !requested.is_empty()
            && requested.iter().all(|name| names.contains(name))
            && found
                .iter()
                .filter(|skill| {
                    skill["name"]
                        .as_str()
                        .is_some_and(|name| requested.iter().any(|requested| requested == name))
                })
                .all(|skill| skill["installed"].as_bool() != Some(true));
        let fetch_id = preview["fetch_id"].as_str().unwrap_or_default().to_owned();
        if requested_are_new && !fetch_id.is_empty() {
            self.ui.learn_install_pending = true;
            self.api(ApiCall::LearnInstall {
                project,
                scope,
                fetch_id,
                skills: requested,
                overwrite: false,
                replacing: Vec::new(),
            });
            return;
        }
        let skills: Vec<LearnSkill> = found
            .into_iter()
            .filter_map(|skill| {
                Some(LearnSkill {
                    name: skill["name"].as_str()?.to_owned(),
                    description: skill["description"].as_str().unwrap_or_default().to_owned(),
                    installed: skill["installed"].as_bool().unwrap_or(false),
                })
            })
            .collect();
        if skills.is_empty() || fetch_id.is_empty() {
            self.toast(
                Level::Error,
                "Learn failed: Core returned an invalid preview",
            );
            return;
        }
        self.open_overlay(OverlayKind::LearnChecklist(LearnChecklist {
            fetch_id,
            source,
            scope,
            project,
            selected: skills.iter().map(|skill| !skill.installed).collect(),
            skills,
            cursor: 0,
        }));
    }

    fn learn_checklist_move(&mut self, delta: isize) {
        if let Some(Overlay {
            kind: OverlayKind::LearnChecklist(checklist),
            ..
        }) = self.ui.overlay.as_mut()
        {
            checklist.cursor = (checklist.cursor as isize + delta)
                .clamp(0, checklist.skills.len().saturating_sub(1) as isize)
                as usize;
        }
    }

    fn learn_checklist_key(&mut self, name: &str) {
        match name {
            "up" => self.learn_checklist_move(-1),
            "down" => self.learn_checklist_move(1),
            "space" => {
                if let Some(Overlay {
                    kind: OverlayKind::LearnChecklist(checklist),
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    if let Some(selected) = checklist.selected.get_mut(checklist.cursor) {
                        *selected = !*selected;
                    }
                }
            }
            "a" | "n" | "i" => {
                if let Some(Overlay {
                    kind: OverlayKind::LearnChecklist(checklist),
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    for selected in &mut checklist.selected {
                        match name {
                            "a" => *selected = true,
                            "n" => *selected = false,
                            _ => *selected = !*selected,
                        }
                    }
                }
            }
            "enter" => self.learn_checklist_install(),
            "esc" => self.close_overlay(),
            _ => {}
        }
    }

    fn learn_checklist_install(&mut self) {
        let Some(Overlay {
            kind: OverlayKind::LearnChecklist(checklist),
            ..
        }) = self.ui.overlay.as_ref()
        else {
            return;
        };
        let skills: Vec<String> = checklist
            .skills
            .iter()
            .zip(&checklist.selected)
            .filter(|(_, selected)| **selected)
            .map(|(skill, _)| skill.name.clone())
            .collect();
        if skills.is_empty() {
            self.toast(Level::Info, "Choose at least one skill");
            return;
        }
        let replacing: Vec<String> = checklist
            .skills
            .iter()
            .zip(&checklist.selected)
            .filter(|(skill, selected)| **selected && skill.installed)
            .map(|(skill, _)| skill.name.clone())
            .collect();
        let call = ApiCall::LearnInstall {
            project: checklist.project.clone(),
            scope: checklist.scope.clone(),
            fetch_id: checklist.fetch_id.clone(),
            skills,
            overwrite: !replacing.is_empty(),
            replacing,
        };
        self.close_overlay();
        self.ui.learn_install_pending = true;
        self.api(call);
    }

    /// Advance clocks. Returns true when something visible changed, so an idle
    /// terminal is not redrawn (and not written to) on every tick.
    /// The body that asks Core for the context a new session would start with.
    pub fn draft_request(&self) -> Option<Value> {
        if self.viewed_id().is_some() {
            return None;
        }
        let project = self.current_project()?;
        let mut body = self.ui.draft.fields();
        body["project"] = project.into();
        Some(body)
    }

    /// A top-level string of Core's plan for the draft (`agent`, `model`).
    fn draft_plan(&self, key: &str) -> Option<String> {
        let v = self.draft_context()?[key].as_str()?;
        (!v.is_empty()).then(|| v.to_owned())
    }

    /// A field the session would be created with (`model`, `effort`): set by the draft, the config or neither.
    fn draft_session_field(&self, key: &str) -> Option<String> {
        let v = self.draft_context()?["session"][key].as_str()?;
        (!v.is_empty()).then(|| v.to_owned())
    }

    /// One list (`tools`, `skills`, `mcp`) of Core's plan for the draft; empty until Core answers.
    pub fn draft_values(&self, key: &str) -> Vec<Value> {
        self.draft_context()
            .or_else(|| {
                if !matches!(key, "skills" | "mcp") {
                    return None;
                }
                let (asked, data) = self.state.draft_context.as_ref()?;
                let mut asked = asked.clone();
                let mut now = self.draft_request()?;
                asked.as_object_mut()?.remove("disabled_tools");
                now.as_object_mut()?.remove("disabled_tools");
                Self::same_draft_context(&asked, &now)
                    .then(|| data.as_ref().ok())
                    .flatten()
            })
            .and_then(|d| d[key].as_array().cloned())
            .unwrap_or_default()
    }

    /// Whether two draft requests start the same context. Reasoning effort steers the model but
    /// is not part of what it reads, so changing it keeps Core's last plan on screen.
    fn same_draft_context(asked: &Value, now: &Value) -> bool {
        fn without_effort(request: &Value) -> Value {
            let mut request = request.clone();
            if let Some(fields) = request.as_object_mut() {
                fields.remove("effort");
            }
            request
        }
        without_effort(asked) == without_effort(now)
    }

    /// Core's starting context when it answers the current draft; `None` while asking or after a failure.
    pub fn draft_context(&self) -> Option<&Value> {
        match (&self.state.draft_context, self.draft_request()) {
            (Some((asked, Ok(data))), Some(now)) if Self::same_draft_context(asked, &now) => {
                Some(data)
            }
            _ => None,
        }
    }

    fn renew_file_watch(&mut self, now: Instant) {
        let wanted =
            self.state.connection.is_live() && matches!(self.panel_id(), Some("files" | "file"));
        let target = if wanted {
            self.current_project().map(|p| (p, self.files_session()))
        } else {
            None
        };
        let same = self
            .file_watch
            .as_ref()
            .is_some_and(|(p, s, _)| target.as_ref() == Some(&(p.clone(), s.clone())));
        if !same {
            if let Some((project, session, _)) = self.file_watch.take() {
                self.api(ApiCall::FileWatch {
                    project,
                    session,
                    client: self.file_watch_client.clone(),
                    release: true,
                });
            }
            self.file_watch_status.clear();
        }
        if let Some((project, session)) = target {
            if self
                .file_watch
                .as_ref()
                .is_none_or(|(_, _, due)| now >= *due)
            {
                self.file_watch = Some((
                    project.clone(),
                    session.clone(),
                    now + std::time::Duration::from_secs(20),
                ));
                self.api(ApiCall::FileWatch {
                    project,
                    session,
                    client: self.file_watch_client.clone(),
                    release: false,
                });
            }
        }
    }

    fn disk_files_changed(&mut self, data: &Value) {
        if self.current_project().as_deref() != data["project_id"].as_str()
            || self.files_root().as_deref() != data["workspace"].as_str()
        {
            return;
        }
        if matches!(self.panel_id(), Some("files" | "file")) {
            let mut dirs: Vec<String> = self.ui.files_expanded.iter().cloned().collect();
            dirs.push(String::new());
            for dir in dirs {
                self.request_files(dir, false);
            }
        }
        if let Some((_, _, path, _)) = self.open_file_ref() {
            let changed = data["all"].as_bool() == Some(true)
                || data["polling"].as_bool() == Some(true)
                || data["omitted"].as_u64().unwrap_or(0) > 0
                || data["paths"]
                    .as_array()
                    .is_some_and(|paths| paths.iter().any(|p| p.as_str() == Some(path.as_str())));
            if changed {
                self.request_files(path, true);
            }
        }
    }

    pub fn tick(&mut self, now: Instant) -> bool {
        self.clock = now;
        if let Some(btw) = self.ui.btw.as_mut().filter(|b| {
            !b.synced
                && !b.refreshing
                && b.last_refresh.is_none_or(|at| {
                    now.saturating_duration_since(at) >= std::time::Duration::from_secs(1)
                })
        }) {
            if let Some(session) = btw.session.clone() {
                btw.refreshing = true;
                btw.last_refresh = Some(now);
                let request_id = btw.request_id.clone();
                self.api(ApiCall::BtwState {
                    session,
                    request_id,
                });
            }
        }
        self.renew_file_watch(now);
        let mut changed = false;
        if self.terminal_panel.reconnect_at.is_some_and(|at| now >= at) {
            self.terminal_panel.reconnect_at = None;
            if self.terminal_open {
                if let Some(index) = self.terminal_panel.terminals.iter().position(|item| {
                    Some(&item.id) == self.terminal_panel.selected.as_ref()
                        && item.status != "closed"
                }) {
                    self.terminal_panel.input_busy = false;
                    self.terminal_select(index);
                    self.terminal_panel.acquire_on_attach = false;
                    changed = true;
                }
            }
        }
        // The scroll bar fades out on the shared tick, then erases itself.
        if self.ui.scroll_mark.is_some() {
            changed = true;
            if self.scrollbar_phase(now).is_none() {
                self.ui.scroll_mark = None;
            }
        }
        // A running session shows everything: its older pages load without Enter.
        let auto_older = self.state.model.selected.as_ref().is_some_and(|v| {
            v.messages_more
                && self.is_busy(&v.session_id)
                && self.ui.older_failed.as_deref() != Some(v.session_id.as_str())
        });
        if auto_older && !self.ui.loading_older {
            self.execute("turn.load_older");
        }
        if let Some(body) = self.draft_request() {
            if self.ui.draft_asked.as_ref() != Some(&body) {
                self.ui.draft_asked = Some(body.clone());
                self.effects
                    .push(Effect::Sync(SyncCommand::DraftContext(body)));
            }
        }
        let recording = matches!(
            self.ui.dictation.phase,
            crate::dictation::Phase::Recording | crate::dictation::Phase::Stopping
        );
        let hovered_tab = self.mouse.and_then(|(x, y)| match self.hits.at(x, y) {
            Some(Target::SessionTab(i)) => Some(*i),
            _ => None,
        });
        if hovered_tab != self.ui.tab_hover.map(|(i, _)| i) {
            self.ui.tab_hover = hovered_tab.map(|i| (i, self.frame));
            changed = true;
        }
        let animated = !self.theme.opts.reduced_motion
            && (recording
                || !self.state.model.running.is_empty()
                || self
                    .state
                    .model
                    .sessions
                    .values()
                    .any(|s| s.status.as_deref() == Some("busy"))
                || self.ui.tab_hover.is_some()
                || !self.ui.group_flash.is_empty()
                || matches!(
                    self.state.connection,
                    ConnectionState::Starting | ConnectionState::Connecting
                ));
        if animated {
            self.frame = self.frame.wrapping_add(1);
            changed = true;
        }
        // Kernels appear during a code cell and shells start and finish between
        // turns; refresh while the viewed tree is busy or its list is open.
        let resources_open = matches!(
            self.ui.overlay.as_ref().map(|o| &o.kind),
            Some(OverlayKind::Resources { .. })
        );
        let busy_tree = self.viewed_id().is_some_and(|id| self.is_busy(id));
        if (busy_tree || resources_open)
            && self
                .last_resources_at
                .is_none_or(|at| now.duration_since(at) >= std::time::Duration::from_secs(2))
        {
            self.last_resources_at = Some(now);
            self.effects.push(Effect::Sync(SyncCommand::Resources));
        }
        self.inspector_poll(now);
        if !self.theme.opts.reduced_motion {
            changed |= self.ui.dictation.settle_tick();
        }
        changed |= self.note_waits(now);
        changed |= self.manager_tick(now);
        let held = self.mouse.and_then(|(x, y)| match self.hits.at(x, y) {
            Some(Target::ToastDismiss(id) | Target::ToastOpen(id)) => Some(*id),
            _ => None,
        });
        let before = self.ui.alerts.toasts.len();
        self.ui.alerts.expire(now, held);
        changed |= self.ui.alerts.toasts.len() != before;
        if self.ui.focus == Focus::Alert && !self.has_alerts() {
            self.ui.focus = Focus::Composer;
            changed = true;
        }
        // A tick follows every interaction closely enough, and the supervisor
        // keeps the latest view for the next `server.connected`.
        self.sync_view();
        changed
    }

    /// Start the clock of every waiting request that has none and drop the
    /// clocks of resolved ones. True when a card's `0:42` text changed.
    fn note_waits(&mut self, now: Instant) -> bool {
        let ids: Vec<String> = self.wait_items().into_iter().map(|w| w.id).collect();
        self.ui.wait_seen.retain(|k, _| ids.contains(k));
        for id in &ids {
            self.ui.wait_seen.entry(id.clone()).or_insert(now);
        }
        // Only the focused card shows a clock.
        let shown = self
            .wait_focused()
            .map(|w| (w.id.clone(), self.wait_secs(&w.id)));
        let changed = shown != self.ui.wait_shown;
        self.ui.wait_shown = shown;
        changed
    }

    /// Whole seconds request `id` has waited on the user.
    pub fn wait_secs(&self, id: &str) -> u64 {
        self.ui
            .wait_seen
            .get(id)
            .map_or(0, |t| self.clock.saturating_duration_since(*t).as_secs())
    }

    /// Note which groups of the freshly built timeline are live. A group that
    /// was live at the last draw and now is not has just finished: its glyph
    /// flashes the accent for `timeline::flash_frames()` (never with reduced motion).
    pub fn note_live_groups(&mut self, timeline: &crate::timeline::Timeline) {
        let now: std::collections::HashSet<String> = timeline.live_groups.iter().cloned().collect();
        if !self.theme.opts.reduced_motion {
            for key in self.ui.live_groups.difference(&now) {
                if timeline.blocks.iter().any(|b| &b.key == key) {
                    self.ui.group_flash.insert(key.clone(), self.frame);
                }
            }
        }
        self.ui.live_groups = now;
        let frame = self.frame;
        self.ui
            .group_flash
            .retain(|_, at| frame.wrapping_sub(*at) < crate::timeline::flash_frames());
    }

    pub fn set_size(&mut self, w: u16, h: u16) {
        self.size = (w, h);
        // The first real size picks the right sidebar's default (D17).
        self.ui.sized = true;
        // Targets from the old geometry are stale until the next draw.
        self.hits.clear();
        if !self.narrow() {
            self.ui.drawer = None;
        }
    }

    // ---- keyboard ---------------------------------------------------------------------------

    fn lookup(&self, ctx: &str, key: &str) -> Option<String> {
        self.bindings
            .get(&(ctx.to_owned(), key.to_owned()))
            .cloned()
    }

    fn focus_contexts(&self) -> Vec<String> {
        match self.ui.focus {
            Focus::Composer => {
                if self.viewed_id().is_some_and(|i| self.is_busy(i)) {
                    vec!["composer_busy".into(), "composer".into()]
                } else {
                    vec!["composer".into()]
                }
            }
            Focus::Timeline => vec!["timeline".into()],
            Focus::Right => vec!["sidebar.git".into()],
            Focus::Waiting => vec!["waiting".into()],
            Focus::Sidebar => vec![format!("sidebar.{}", self.ui.tab.id()), "sidebar".into()],
            _ => vec![],
        }
    }

    /// Number of lines in the open file (0 while it loads or fails).
    pub fn file_line_count(&self) -> usize {
        self.state
            .file
            .as_ref()
            .and_then(|(_, _, _, data)| data.as_ref().ok())
            .and_then(|data| data["content"].as_str())
            .map_or(0, |content| content.lines().count())
    }

    /// The open file is Markdown, so it can be read as prose.
    pub fn file_is_markdown(&self) -> bool {
        self.state
            .file
            .as_ref()
            .is_some_and(|(_, _, path, _)| is_markdown_path(path))
    }

    /// The rendered Markdown preview is what the file view shows.
    pub fn file_preview(&self) -> bool {
        self.ui.file_open && !self.ui.file_source && self.file_is_markdown()
    }

    /// The preview's rows and diagrams at `width`, cached until the text, width
    /// or theme changes.
    pub fn file_preview_rows(&self, width: usize) -> std::rc::Rc<crate::timeline::Document> {
        let content = self
            .ui
            .file_edit
            .as_ref()
            .map(|edit| edit.buf.text())
            .unwrap_or_else(|| {
                self.state
                    .file
                    .as_ref()
                    .and_then(|(_, _, _, data)| data.as_ref().ok())
                    .and_then(|data| data["content"].as_str())
                    .unwrap_or("")
                    .to_owned()
            });
        let palette = crate::markdown::Palette::from_theme(&self.theme);
        self.layout.borrow_mut().document(&content, width, &palette)
    }

    /// Indices of the open file's lines that contain the search query, ignoring ASCII case.
    pub fn file_matches(&self) -> Vec<usize> {
        let Some(query) = self
            .ui
            .file_search
            .as_ref()
            .map(|s| s.query.to_ascii_lowercase())
        else {
            return Vec::new();
        };
        if query.is_empty() {
            return Vec::new();
        }
        self.state
            .file
            .as_ref()
            .and_then(|(_, _, _, data)| data.as_ref().ok())
            .and_then(|data| data["content"].as_str())
            .map_or(Vec::new(), |content| {
                content
                    .lines()
                    .enumerate()
                    .filter(|(_, line)| line.to_ascii_lowercase().contains(&query))
                    .map(|(i, _)| i)
                    .collect()
            })
    }

    /// Keys for the file view's `/` search. Returns whether the key was taken.
    /// Keys of the editor (K10). Returns whether the key was taken; keys it does not
    /// know (global shortcuts) fall through.
    fn file_edit_key(&mut self, ev: &KeyEvent, name: &str) -> bool {
        if name == "ctrl+shift+p" && self.file_is_markdown() {
            self.toggle_file_preview();
            return true;
        }
        if name == "ctrl+s" {
            self.save_file();
            return true;
        }
        if name == "ctrl+f" {
            self.open_file_find();
            return true;
        }
        let area = crate::render::file_editor_area(self.view_rect(), self.ui.file_search.is_some());
        let Some(edit) = self.ui.file_edit.as_mut() else {
            return false;
        };
        if name == "ctrl+c" || name == "ctrl+x" {
            if let Some(text) = edit.buf.selection() {
                self.effects.push(Effect::Copy(text));
            }
        }
        edit.buf.key(*ev, area)
    }

    /// The open file's project, session scope, path and read revision.
    fn open_file_ref(&self) -> Option<(String, Option<String>, String, Value)> {
        let (project, session, path, result) = self.state.file.as_ref()?;
        Some((
            project.clone(),
            session.clone(),
            path.clone(),
            result.as_ref().ok()?.clone(),
        ))
    }

    fn start_edit(&mut self) {
        if self.ui.file_edit.is_some() {
            return;
        }
        let Some((_, _, path, data)) = self.open_file_ref() else {
            self.toast(Level::Warning, "The file is not loaded");
            return;
        };
        let Some(revision) = data["revision"].as_str().map(str::to_owned) else {
            self.toast(
                Level::Warning,
                "Core did not report a revision for this file",
            );
            return;
        };
        let saved = data["content"].as_str().unwrap_or("").to_owned();
        let buf = match crate::code_editor::CodeBuffer::new(&path, &saved, false, &self.theme) {
            Ok(buf) => buf,
            Err(error) => {
                self.toast(Level::Error, format!("Could not open editor: {error}"));
                return;
            }
        };
        self.ui.file_edit = Some(FileEdit {
            buf,
            revision,
            saved,
            top: 0,
            saving: false,
            reloading: false,
            stale: false,
            dragging: false,
        });
        self.ui.file_source = true;
    }

    fn save_file(&mut self) {
        let Some((project, session, path, _)) = self.open_file_ref() else {
            return;
        };
        if !self.writable() {
            return self.toast(Level::Warning, READ_ONLY);
        }
        let Some(edit) = self.ui.file_edit.as_mut() else {
            return;
        };
        if edit.saving {
            return;
        }
        if !edit.dirty() {
            return self.toast(Level::Info, "No unsaved changes");
        }
        edit.saving = true;
        let call = ApiCall::SaveFile {
            project,
            session,
            path,
            content: edit.buf.text(),
            revision: edit.revision.clone(),
        };
        self.api(call);
    }

    fn reload_file(&mut self) {
        let Some((_, _, path, _)) = self.open_file_ref() else {
            return;
        };
        if let Some(edit) = self.ui.file_edit.as_mut() {
            edit.reloading = true;
        }
        self.request_files(path, true);
    }

    fn ask_discard_edit(&mut self) {
        let restore = self.restore_point();
        self.ui.dialog = Some(Dialog {
            title: "Discard changes".into(),
            body: vec!["The file has unsaved changes. Leaving the editor drops them.".into()],
            cancel: "Keep editing".into(),
            confirm: "Discard".into(),
            danger: true,
            action: Confirmed::DiscardEdit,
            button: 0,
            restore,
        });
    }

    fn ask_reload_edit(&mut self) {
        let restore = self.restore_point();
        self.ui.dialog = Some(Dialog {
            title: "Reload file".into(),
            body: vec!["Reading the file again replaces your unsaved changes.".into()],
            cancel: "Keep editing".into(),
            confirm: "Reload".into(),
            danger: true,
            action: Confirmed::ReloadEdit,
            button: 0,
            restore,
        });
    }

    /// The file was read again: a requested reload replaces the editor's text.
    fn file_read(&mut self) {
        let Some((_, _, _, data)) = self.open_file_ref() else {
            return;
        };
        if self.ui.file_edit.is_none() {
            if self.ui.file_open {
                self.start_edit();
            }
            return;
        }
        let Some(edit) = self.ui.file_edit.as_mut() else {
            return;
        };
        if !edit.reloading {
            if data["revision"].as_str() == Some(edit.revision.as_str()) {
                return;
            }
            if edit.dirty() {
                edit.stale = true;
                return;
            }
        }
        edit.reloading = false;
        edit.stale = false;
        if let Some(revision) = data["revision"].as_str() {
            edit.revision = revision.to_owned();
        }
        let text = data["content"].as_str().unwrap_or("").to_owned();
        edit.buf.set_text(&text);
        edit.saved = text;
    }

    #[doc(hidden)]
    pub fn file_changed_for_test(&mut self, data: &Value) {
        self.file_changed(data);
    }

    /// `files.changed` for the open file: a clean editor or view follows it; unsaved text is
    /// kept and the editor says the file moved on.
    fn file_changed(&mut self, data: &Value) {
        let Some((project, _, path, current)) = self.open_file_ref() else {
            return;
        };
        if data["project_id"].as_str() != Some(project.as_str())
            || data["path"].as_str() != Some(path.as_str())
            || data["revision"].as_str() == current["revision"].as_str()
        {
            return;
        }
        match self
            .ui
            .file_edit
            .as_ref()
            .map(|e| (e.dirty(), e.revision.clone()))
        {
            // Our own save already moved the editor's revision on.
            Some((_, revision)) if data["revision"].as_str() == Some(revision.as_str()) => {}
            Some((true, _)) => {
                if let Some(edit) = self.ui.file_edit.as_mut() {
                    edit.stale = true;
                }
                self.toast(
                    Level::Warning,
                    "The file changed on disk: reload before saving",
                );
            }
            Some((false, _)) => self.reload_file(),
            None => self.request_files(path, true),
        }
    }

    fn editor_find_key(&mut self, ev: &KeyEvent, name: &str) -> bool {
        if name == "esc" {
            self.close_panel();
            return true;
        }
        let Some(search) = self.ui.file_search.as_mut() else {
            return false;
        };
        match name {
            "tab" | "shift+tab" => search.replace_focus = !search.replace_focus,
            "alt+c" => {
                search.case_sensitive = !search.case_sensitive;
                search.hit = None;
            }
            "backspace" => {
                if search.replace_focus {
                    search.replacement.pop();
                } else {
                    search.query.pop();
                    search.hit = None;
                }
            }
            "enter" | "shift+enter" | "ctrl+n" | "ctrl+p" | "ctrl+r" | "ctrl+shift+r" => {
                let edit = self.ui.file_edit.as_ref().unwrap();
                let ranges = edit.buf.find(&search.query, search.case_sensitive);
                if !ranges.is_empty() {
                    let i = match search.hit {
                        Some(i) if name == "shift+enter" || name == "ctrl+p" => {
                            (i + ranges.len() - 1) % ranges.len()
                        }
                        Some(i) if name == "ctrl+r" || name == "ctrl+shift+r" => {
                            i.min(ranges.len() - 1)
                        }
                        Some(i) => (i + 1) % ranges.len(),
                        None => 0,
                    };
                    if name == "ctrl+shift+r" {
                        for &(a, b) in ranges.iter().rev() {
                            edit.buf.replace(a, b, &search.replacement);
                        }
                        search.hit = None;
                    } else if name == "ctrl+r" {
                        let (a, b) = ranges[i];
                        edit.buf.replace(a, b, &search.replacement);
                        search.hit = None;
                    } else {
                        search.hit = Some(i);
                        let (a, b) = ranges[i];
                        edit.buf.select(a, b);
                    }
                }
            }
            _ => {
                if let Some(c) = keys::printable(ev) {
                    if search.replace_focus {
                        search.replacement.push(c);
                    } else {
                        search.query.push(c);
                        search.hit = None;
                    }
                } else {
                    return false;
                }
            }
        }
        let edit = self.ui.file_edit.as_ref().unwrap();
        edit.buf
            .mark(&edit.buf.find(&search.query, search.case_sensitive));
        true
    }

    fn file_search_key(&mut self, ev: &KeyEvent, name: &str) -> bool {
        let typed = keys::printable(ev);
        let Some(search) = self.ui.file_search.as_mut() else {
            if typed == Some('/') {
                // Search reads the source, so a preview steps aside.
                self.ui.file_source = true;
                self.ui.file_scroll = 0;
                self.ui.file_search = Some(FileSearch {
                    typing: true,
                    ..FileSearch::default()
                });
                return true;
            }
            return false;
        };
        let typing = search.typing;
        match typed {
            Some('n') if !typing => self.file_goto(true),
            Some('N') if !typing => self.file_goto(false),
            _ if name == "enter" => {
                search.typing = false;
                self.file_goto(true);
            }
            _ if name == "backspace" => {
                search.query.pop();
                search.typing = true;
                search.hit = None;
            }
            // Back to editing the same query.
            Some('/') if !typing => search.typing = true,
            // While not typing, j and k keep scrolling.
            Some(c) if typing || !matches!(c, 'j' | 'k') => {
                search.query.push(c);
                search.typing = true;
                search.hit = None;
            }
            _ => return false,
        }
        true
    }

    /// The file view's last scroll position at its current width.
    fn file_scroll_limit(&self) -> usize {
        let rect = self.view_rect();
        let page = crate::paint::file_page(rect.height);
        if self.file_preview() {
            return self
                .file_preview_rows(rect.width.saturating_sub(2) as usize)
                .lines
                .len()
                .saturating_sub(page);
        }
        let content = self
            .state
            .file
            .as_ref()
            .and_then(|(_, _, _, data)| data.as_ref().ok())
            .and_then(|data| data["content"].as_str())
            .unwrap_or("");
        crate::paint::file_scroll_max(
            content,
            crate::paint::file_text_room(rect.width, self.file_line_count()),
            page,
        )
    }

    /// Rendered prose or numbered source for a Markdown file.
    fn toggle_file_preview(&mut self) {
        if !self.file_is_markdown() {
            return;
        }
        self.ui.file_source = !self.ui.file_source;
        self.ui.file_scroll = 0;
        if !self.ui.file_source {
            self.ui.file_search = None;
        }
    }

    /// Find reads the source, so a preview steps aside before the bar opens.
    fn open_file_find(&mut self) {
        if !self.ui.file_open {
            return;
        }
        if self.file_is_markdown() {
            self.ui.file_source = true;
            self.ui.file_scroll = 0;
        }
        self.ui.file_search = Some(FileSearch {
            typing: true,
            ..FileSearch::default()
        });
    }

    /// Moves the search to the next or previous match and scrolls it to the top of the view.
    fn file_goto(&mut self, forward: bool) {
        let matches = self.file_matches();
        let scroll = self.ui.file_scroll;
        let Some(search) = self.ui.file_search.as_mut() else {
            return;
        };
        let count = matches.len();
        if count == 0 {
            search.hit = None;
            return;
        }
        let next = match (search.hit, forward) {
            (Some(i), true) => (i + 1) % count,
            (Some(i), false) => (i + count - 1) % count,
            (None, true) => matches.iter().position(|&line| line >= scroll).unwrap_or(0),
            (None, false) => matches
                .iter()
                .rposition(|&line| line < scroll)
                .unwrap_or(count - 1),
        };
        search.hit = Some(next);
        let max = self.file_scroll_limit();
        self.ui.file_scroll = matches[next].min(max);
    }

    pub fn handle_key(&mut self, ev: KeyEvent) {
        self.handle_key_inner(ev);
        let draft = self.composer();
        let text = draft.text();
        draft
            .images
            .retain(|image| text.contains(&format!("[{}]", image.name)));
        self.completion_sync();
    }

    fn handle_key_inner(&mut self, ev: KeyEvent) {
        // The dock toggle works even while the terminal screen owns the keys,
        // and is never handed to the shell. Other shortcuts still belong to
        // the terminal while its screen has focus.
        let terminal_toggle = keys::name(&ev)
            .is_some_and(|name| self.lookup("global", &name).as_deref() == Some("terminal.open"));
        if self.terminal_open
            && terminal_toggle
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
        {
            self.execute("terminal.open");
            return;
        }
        if self.terminal_open
            && self.terminal_focused
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
        {
            if ev.code == KeyCode::Char(']') && ev.modifiers.contains(KeyModifiers::CONTROL) {
                self.terminal_focused = false;
                self.terminal_panel.focus = crate::terminal_panel::TerminalFocus::List;
            } else if let Some(bytes) = crate::terminal_panel::key_bytes(
                ev,
                self.terminal_panel.screen.screen().application_cursor(),
            ) {
                self.terminal_input(bytes);
            }
            return;
        }
        let Some(name) = keys::name(&ev) else { return };
        if self.terminal_open
            && self.ui.overlay.is_none()
            && self.ui.edit.is_none()
            && self.ui.dialog.is_none()
            && name == "esc"
        {
            self.close_panel();
            return;
        }
        if self.terminal_open
            && self.ui.overlay.is_none()
            && self.ui.edit.is_none()
            && self.ui.dialog.is_none()
        {
            match name.as_str() {
                "up" => self.terminal_panel.cursor = self.terminal_panel.cursor.saturating_sub(1),
                "down" => {
                    self.terminal_panel.cursor =
                        (self.terminal_panel.cursor + 1).min(self.terminal_panel.terminals.len())
                }
                "enter" if self.terminal_panel.cursor == 0 => self.execute("terminal.new"),
                "c" => self.execute("terminal.acquire"),
                "r" => self.execute("terminal.release"),
                "enter" => self.terminal_select(self.terminal_panel.cursor - 1),
                _ => {}
            }
            return;
        }
        if name != "ctrl+c" {
            self.ctrl_c = None;
        }
        if self.ui.rail_drag.is_some() {
            if name == "esc" {
                self.ui.rail_drag = None;
                return;
            }
            self.ui.rail_drag = None;
        }
        // The Manager's shortcuts always, its other keys only while it is engaged.
        if self.manager_key(&ev, &name) {
            return;
        }
        if self.ui.dialog.is_none() && self.ui.dictation.phase != crate::dictation::Phase::Idle {
            use crate::dictation::Phase;
            let capturing = matches!(self.ui.dictation.phase, Phase::Starting | Phase::Recording);
            match name.as_str() {
                // Enter stops and sends; Esc stops and keeps the words; Esc
                // while transcribing discards them.
                "enter" => {
                    if capturing {
                        self.stop_dictation_with(true);
                    }
                    return;
                }
                "esc" if capturing => {
                    self.stop_dictation();
                    return;
                }
                "esc" if self.ui.dictation.phase != Phase::Consent => {
                    self.cancel_dictation();
                    return;
                }
                // Typing stops the capture and the key still applies.
                _ if capturing => self.stop_dictation(),
                _ => {}
            }
        }
        if self.ui.file_edit.is_some()
            && !self.file_preview()
            && self.ui.file_search.is_none()
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
            && matches!(
                name.as_str(),
                "ctrl+c" | "ctrl+x" | "ctrl+v" | "ctrl+z" | "ctrl+shift+z" | "ctrl+y"
            )
            && self.file_edit_key(&ev, &name)
        {
            return;
        }
        if self.ui.focus == Focus::Composer
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
            && name == "ctrl+v"
        {
            let key = self.draft_key();
            if self.ui.image_pasting.insert(key.clone()) {
                self.effects.push(Effect::PasteImage(key));
            }
            return;
        }
        match name.as_str() {
            "ctrl+z"
                if self.ui.focus == Focus::Composer
                    && self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none() =>
            {
                self.composer().undo();
                return;
            }
            "ctrl+shift+z" | "ctrl+y"
                if self.ui.focus == Focus::Composer
                    && self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none() =>
            {
                self.composer().redo();
                return;
            }
            "ctrl+z" => return self.effects.push(Effect::Suspend),
            "ctrl+c" => {
                let composer = self.ui.focus == Focus::Composer
                    && self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none();
                if composer && self.composer_ref().selection().is_some() {
                    if let Some(text) = self.composer_ref().selected_text() {
                        self.effects.push(Effect::Copy(text));
                    }
                    return;
                }
                if composer && !self.composer_ref().is_empty() {
                    self.composer().clear_to_history();
                    self.ctrl_c = Some(self.clock);
                    return;
                }
                let again = self.ctrl_c.is_some_and(|at| {
                    self.clock.saturating_duration_since(at) <= CTRL_C_QUIT_WINDOW
                });
                if again {
                    self.ctrl_c = None;
                    return self.effects.push(Effect::Quit);
                }
                self.ctrl_c = Some(self.clock);
                self.toast(Level::Info, "Press Ctrl+C again to quit");
            }
            _ => {}
        }
        if self.blocking().is_some() {
            // Esc is consumed by the dialog rule and does nothing; ctrl+q quits.
            if name == "ctrl+q" {
                self.effects.push(Effect::Quit);
            }
            return;
        }
        if self.ui.view == View::Settings
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
            && self.settings_key(&name, ev.code, ev.modifiers)
        {
            return;
        }
        // The popup owns Esc, Tab, Enter and the arrows while it is visible.
        if self.completion_key(&name) {
            return;
        }
        if self.ui.focus == Focus::Composer
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
            && self.pending_key(&name)
        {
            return;
        }
        if name == "esc" {
            if (self.ui.diff_open.is_some() || self.ui.file_open)
                && self.ui.overlay.is_none()
                && self.ui.dialog.is_none()
                && self.ui.edit.is_none()
            {
                // The first Esc ends a search, then leaves the editor (asking first when
                // dirty); the next one closes the file.
                self.close_panel();
                return;
            }
            return self.escape();
        }
        if self.ui.diff_open.is_some() && self.ui.overlay.is_none() && self.ui.dialog.is_none() {
            // The rows the view draws (split or unified at its width).
            let width = self.view_rect().width.saturating_sub(2) as usize;
            let patch = self.diff_lines().join("\n");
            let lines = crate::diff::Diff::from_patch(&patch)
                .render(width, &crate::diff::Look::default())
                .len();
            let page = self.view_rect().height.saturating_sub(2) as usize;
            let max = lines.saturating_sub(page);
            match name.as_str() {
                "up" | "k" => self.ui.diff_scroll = self.ui.diff_scroll.saturating_sub(1),
                "down" | "j" => self.ui.diff_scroll = (self.ui.diff_scroll + 1).min(max),
                "pageup" => self.ui.diff_scroll = self.ui.diff_scroll.saturating_sub(page),
                "pagedown" => self.ui.diff_scroll = (self.ui.diff_scroll + page).min(max),
                _ => {}
            }
            if matches!(
                name.as_str(),
                "up" | "k" | "down" | "j" | "pageup" | "pagedown"
            ) {
                return;
            }
        }
        if self.ui.file_edit.is_some()
            && self.ui.file_search.is_some()
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.editor_find_key(&ev, &name)
        {
            return;
        }
        if self.ui.file_edit.is_some()
            && !self.file_preview()
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
            && self.file_edit_key(&ev, &name)
        {
            return;
        }
        if self.ui.file_open && self.ui.overlay.is_none() && self.ui.dialog.is_none() {
            if self.file_search_key(&ev, &name) {
                return;
            }
            if name == "ctrl+f" {
                return self.open_file_find();
            }
            let page = crate::paint::file_page(self.view_rect().height);
            let max = self.file_scroll_limit();
            match name.as_str() {
                "up" | "k" => self.ui.file_scroll = self.ui.file_scroll.saturating_sub(1),
                "down" | "j" => self.ui.file_scroll = (self.ui.file_scroll + 1).min(max),
                "pageup" => self.ui.file_scroll = self.ui.file_scroll.saturating_sub(page),
                "pagedown" => self.ui.file_scroll = (self.ui.file_scroll + page).min(max),
                _ => {}
            }
            if matches!(
                name.as_str(),
                "up" | "k" | "down" | "j" | "pageup" | "pagedown"
            ) {
                return;
            }
        }
        if self.ui.dialog.is_some() {
            return self.dialog_key(&name);
        }
        if self.ui.edit.is_some() {
            return self.edit_key(&ev, &name);
        }
        if self.ui.overlay.is_some() && self.overlay_key(&ev, &name) {
            return;
        }
        // A viewed subagent's page uses Left as a back gesture, but never while a
        // text input, the composer, an editor or an overlay owns the key.
        if name == "left"
            && self.ui.view == View::Session
            && self.ui.focus != Focus::Composer
            && self.ui.focus != Focus::Waiting
            && self.ui.focus != Focus::Alert
            && !self.ui.file_open
            && self.ui.diff_open.is_none()
            && self.ui.file_edit.is_none()
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
            && self.viewed().is_some_and(|s| s.parent_id.is_some())
        {
            self.run_command("nav.root");
            return;
        }
        // Typing never reaches the page behind the file or diff modal.
        if (self.ui.file_open || self.ui.diff_open.is_some())
            && self.ui.overlay.is_none()
            && keys::printable(&ev).is_some()
        {
            return;
        }
        // Old Nexus's Ctrl+X leader and direct aliases, only outside modal input.
        if self.ui.overlay.is_none() && self.ui.dialog.is_none() && self.ui.edit.is_none() {
            if name == "ctrl+x" {
                self.shortcut_leader = Some(Instant::now());
                return;
            }
            if let Some(started) = self.shortcut_leader.take() {
                if started.elapsed().as_secs() < 3 {
                    let command = match name.as_str() {
                        "n" if matches!(ev.code, KeyCode::Char('N')) => Some("notifications.open"),
                        "m" => Some("model.change"),
                        "n" => Some("session.new"),
                        "o" | "b" => Some("session.switch"),
                        "g" => Some("agent.change"),
                        "s" => Some("app.settings"),
                        "v" => Some("voice.dictate"),
                        "l" => Some("sidebar.right.toggle"),
                        "r" => Some("app.reconnect"),
                        "?" => Some("app.help_keys"),
                        "t" => {
                            self.cycle_effort();
                            return;
                        }
                        _ if matches!(ev.code, KeyCode::Char('N')) => Some("notifications.open"),
                        _ => None,
                    };
                    if let Some(command) = command {
                        self.run_command(command);
                    }
                    return;
                }
            }
            match name.as_str() {
                "ctrl+o" | "ctrl+s" => {
                    self.run_command("session.switch");
                    return;
                }
                "ctrl+l" => {
                    self.run_command("sidebar.right.toggle");
                    return;
                }
                "ctrl+r" => {
                    self.run_command("composer.history_search");
                    return;
                }
                "ctrl+t" => {
                    self.cycle_effort();
                    return;
                }
                _ => {}
            }
        }
        if self.ui.focus == Focus::Composer
            && !self.question_pending()
            && matches!(name.as_str(), "alt+b" | "alt+f")
        {
            edit_buffer(self.composer(), &ev, &name, false);
            return;
        }
        if let Some(cmd) = self.lookup("global", &name) {
            return self.run_command(&cmd);
        }
        match name.as_str() {
            "f6" => return self.cycle_focus(false),
            "shift+f6" => return self.cycle_focus(true),
            "pageup" | "pagedown"
                if self.ui.focus == Focus::Waiting && self.ask_current().is_some() =>
            {
                self.ask_scroll(name == "pagedown");
                return;
            }
            "pageup" | "pagedown" if self.ui.view == View::Session => {
                let page = self.ui.timeline_height.saturating_sub(1).max(1) as isize;
                return self.scroll_timeline(if name == "pageup" { -page } else { page });
            }
            _ => {}
        }
        if self.question_pending() && self.ui.focus == Focus::Composer {
            if self.ask_current().is_some() {
                // The card owns the composer's keys; the draft is untouched.
                self.ui.focus = Focus::Waiting;
                return self.waiting_key(&ev, &name);
            }
            // A question is pending behind another wait: the composer stays
            // locked, but approval keys are not borrowed.
            if keys::printable(&ev).is_some()
                || edit_key(&name)
                || matches!(name.as_str(), "enter" | "space" | "backspace" | "delete")
            {
                return;
            }
        }
        match self.ui.focus {
            Focus::Composer => self.composer_key(&ev, &name),
            Focus::Timeline => self.timeline_key(&name),
            Focus::Sidebar => self.sidebar_key(&name),
            Focus::Waiting => self.waiting_key(&ev, &name),
            Focus::ToggleLeft | Focus::ToggleRight => {
                if matches!(name.as_str(), "enter" | "space") {
                    let side = if self.ui.focus == Focus::ToggleLeft {
                        Side::Left
                    } else {
                        Side::Right
                    };
                    self.toggle_side(side);
                    self.ui.focus = if side == Side::Left {
                        if self.ui.left_open && !self.narrow() {
                            Focus::ToggleLeft
                        } else {
                            self.ui.focus
                        }
                    } else {
                        self.ui.focus
                    };
                }
            }
            Focus::Alert => match name.as_str() {
                "enter" => {
                    let action = self.banners().into_iter().find_map(|b| b.action);
                    if let Some((_, _, cmd)) = action {
                        self.run_command(&cmd);
                    } else if let Some(id) = self.ui.alerts.toasts.first().map(|t| t.id) {
                        self.open_alert_detail(id, None);
                    }
                }
                "x" | "backspace" | "delete" => self.dismiss_alert(),
                _ => {}
            },
            Focus::Right => self.right_key(&name),
        }
        // `r` retries from the banner without moving focus.
    }

    /// The Esc precedence chain, exactly as the manifest orders it.
    pub fn escape(&mut self) {
        let rules = self.manifest.keymap.escape.clone();
        for rule in rules {
            if rule.when.iter().all(|t| self.holds(t)) {
                self.execute(&rule.action);
                return;
            }
        }
    }

    /// Which chain rule an Esc press would take now (for tests and hints).
    pub fn escape_rule(&self) -> Option<String> {
        self.manifest
            .keymap
            .escape
            .iter()
            .find(|r| r.when.iter().all(|t| self.holds(t)))
            .map(|r| r.id.clone())
    }

    fn dialog_key(&mut self, name: &str) {
        if let Some(Dialog {
            action:
                Confirmed::AttachTerminalEvidence {
                    offset, payload, ..
                },
            ..
        }) = self.ui.dialog.as_mut()
        {
            let width = 60_u16.min(self.size.0.saturating_sub(4)).saturating_sub(4) as usize;
            let rows = payload
                .lines()
                .flat_map(|line| crate::text::wrap(line, width))
                .count();
            let max = rows.saturating_sub(self.size.1.saturating_sub(8) as usize);
            *offset = (*offset).min(max);
            match name {
                "down" | "j" => {
                    *offset = (*offset + 1).min(max);
                    return;
                }
                "up" | "k" => {
                    *offset = offset.saturating_sub(1);
                    return;
                }
                "pagedown" => {
                    *offset = (*offset + 10).min(max);
                    return;
                }
                "pageup" => {
                    *offset = offset.saturating_sub(10);
                    return;
                }
                "home" => {
                    *offset = 0;
                    return;
                }
                _ => {}
            }
        }
        match name {
            "left" | "right" | "tab" | "shift+tab" | "h" | "l" => {
                if let Some(d) = self.ui.dialog.as_mut() {
                    d.button = 1 - d.button.min(1);
                }
            }
            "enter" => {
                let yes = self.ui.dialog.as_ref().is_some_and(|d| d.button == 1);
                self.confirm_dialog(yes);
            }
            _ => {
                if let Some(cmd) = self.lookup("global", name).filter(|c| c == "app.quit") {
                    self.run_command(&cmd);
                }
            }
        }
    }

    fn edit_key(&mut self, ev: &KeyEvent, name: &str) {
        let Some(e) = self.ui.edit.as_mut() else {
            return;
        };
        match name {
            "enter" => {
                let title = e.buf.text().trim().to_owned();
                let target = e.target.clone();
                let restore = e.restore;
                self.ui.edit = None;
                self.apply_restore(restore);
                if let EditTarget::ManagerModel { revision } = &target {
                    if !title.is_empty() {
                        self.manager_set_config(revision.clone(), "manager.model", json!(title));
                    }
                    return;
                }
                if let EditTarget::TitleModels { revision } = &target {
                    let refs: Vec<&str> = title
                        .split(',')
                        .map(str::trim)
                        .filter(|r| !r.is_empty())
                        .collect();
                    if !self.writable() {
                        self.toast(Level::Warning, READ_ONLY);
                    } else {
                        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                            revision: revision.clone(),
                            path: "context.title_models".into(),
                            value: json!(refs),
                        }));
                    }
                    return;
                }
                if title.is_empty() {
                    return;
                }
                if !self.writable() {
                    self.toast(Level::Warning, READ_ONLY);
                    return;
                }
                match target {
                    EditTarget::ProjectGitValue {
                        project,
                        worktree,
                        action,
                    } => self.api(ApiCall::ProjectGitLive {
                        project,
                        worktree,
                        action,
                        value: title,
                        paths: vec![],
                    }),
                    EditTarget::FilePath {
                        project,
                        session,
                        action,
                        path,
                    } => {
                        let (path, to) = if action == "rename" {
                            (path, Some(title))
                        } else {
                            (title, None)
                        };
                        self.api(ApiCall::FileMutation {
                            project,
                            session,
                            action,
                            path,
                            to,
                        });
                    }
                    EditTarget::RenameSession { id, revision } => self.api(ApiCall::Rename {
                        session: id,
                        title,
                        revision,
                    }),
                    EditTarget::RenameTerminal { id } => {
                        self.api(ApiCall::TerminalRename { id, name: title })
                    }
                    EditTarget::ChangeDirectory { id } => self.api(ApiCall::ChangeDirectory {
                        session: id,
                        directory: title,
                    }),
                    EditTarget::ProviderKey { id } => self.api(ApiCall::Provider {
                        id,
                        sign_out: false,
                        method: "api_key".into(),
                        key: Some(crate::actions::Secret(title)),
                    }),
                    EditTarget::TitleModels { .. } | EditTarget::ManagerModel { .. } => {}
                    EditTarget::AddProject => {
                        let path = match title.strip_prefix('~') {
                            Some(rest) if rest.is_empty() || rest.starts_with('/') => {
                                std::env::var("HOME")
                                    .map_or(title.clone(), |h| format!("{h}{rest}"))
                            }
                            _ => title,
                        };
                        self.api(ApiCall::AddProject { path });
                    }
                }
            }
            _ => edit_buffer(&mut e.buf, ev, name, false),
        }
    }

    fn overlay_key(&mut self, ev: &KeyEvent, name: &str) -> bool {
        if matches!(name, "i" | "c") {
            if let Some(btw) = self.ui.btw.as_mut() {
                if name == "i" {
                    btw.inherited_open = !btw.inherited_open;
                } else {
                    btw.context_open = !btw.context_open;
                }
                if let Some(Overlay {
                    kind: OverlayKind::ContextDetail { text, top, .. },
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    *text = btw.detail();
                    *top = 0;
                }
                return true;
            }
        }
        if matches!(
            self.ui.overlay.as_ref().map(|o| &o.kind),
            Some(OverlayKind::Inspector(_))
        ) {
            if name == "esc" {
                if self.inspector_escape() {
                    return true;
                }
            } else {
                self.inspector_key(ev, name);
                return true;
            }
        }
        if matches!(
            self.ui.overlay.as_ref().map(|o| &o.kind),
            Some(OverlayKind::Projects(_))
        ) {
            return self.projects_overview_key(name);
        }
        if self.annotation_mut().is_some() {
            self.annotate_key(name);
            return true;
        }
        if matches!(
            self.ui.overlay,
            Some(Overlay {
                kind: OverlayKind::LearnChecklist(_),
                ..
            })
        ) {
            self.learn_checklist_key(name);
            return true;
        }
        let is_palette = matches!(
            self.ui.overlay,
            Some(Overlay {
                kind: OverlayKind::Palette(_),
                ..
            })
        );
        if let Some(Overlay {
            kind:
                OverlayKind::ContextEntries {
                    title,
                    cursor,
                    open,
                    ..
                },
            ..
        }) = &self.ui.overlay
        {
            if title == "Skills" && open.is_none() {
                let cursor = *cursor;
                match name {
                    "space" => {
                        if let Some(item) = self.context_items_for("Skills").get(cursor) {
                            let index = item.index;
                            if !item.locked {
                                self.toggle_context_item(index);
                            }
                        }
                        return true;
                    }
                    "left" | "right" | "tab" | "shift+tab" => {
                        let columns = self.context_items_for("Skills").len().div_ceil(2) as isize;
                        self.overlay_move(match name {
                            "left" => -columns,
                            "right" => columns,
                            "shift+tab" => -1,
                            _ => 1,
                        });
                        return true;
                    }
                    _ => {}
                }
            }
        }
        match name {
            "tab"
                if matches!(
                    self.ui.overlay,
                    Some(Overlay {
                        kind: OverlayKind::Palette(Palette {
                            mode: PaletteMode::Sessions,
                            ..
                        }),
                        ..
                    })
                ) =>
            {
                self.ui.scope_all = !self.ui.scope_all;
                if let Some(Overlay {
                    kind: OverlayKind::Palette(p),
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    p.selected = 0;
                    p.top = 0;
                }
                self.ask_search();
            }
            "tab"
                if matches!(
                    self.ui.overlay,
                    Some(Overlay {
                        kind: OverlayKind::Palette(Palette {
                            mode: PaletteMode::Models,
                            ..
                        }),
                        ..
                    })
                ) =>
            {
                self.toggle_favorite_model();
            }
            "m" if matches!(
                self.ui.overlay,
                Some(Overlay {
                    kind: OverlayKind::Palette(Palette {
                        mode: PaletteMode::Models,
                        ..
                    }),
                    ..
                })
            ) =>
            {
                if let Some(Overlay {
                    kind: OverlayKind::Palette(p),
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    p.model_info = !p.model_info;
                }
            }
            "r" if matches!(
                self.ui.overlay,
                Some(Overlay {
                    kind: OverlayKind::Usage { .. },
                    ..
                })
            ) =>
            {
                self.api(ApiCall::RefreshUsage);
            }
            "s" if matches!(
                self.ui.overlay,
                Some(Overlay {
                    kind: OverlayKind::Resources { .. },
                    ..
                })
            ) =>
            {
                self.stop_selected_shell();
            }
            "up" => self.overlay_move(-1),
            "down" => self.overlay_move(1),
            "pageup" => self.overlay_move(-8),
            "pagedown" => self.overlay_move(8),
            "c" if matches!(
                self.ui.overlay,
                Some(Overlay {
                    kind: OverlayKind::AlertDetail { .. },
                    ..
                })
            ) =>
            {
                if let Some(Overlay {
                    kind: OverlayKind::AlertDetail { id, .. },
                    ..
                }) = &self.ui.overlay
                {
                    if let Some((_, text)) = self.ui.alerts.full(*id) {
                        let text = text.to_owned();
                        self.effects.push(Effect::Copy(text));
                        self.toast(Level::Success, "Copied");
                    }
                }
            }
            "enter"
                if matches!(
                    self.ui.overlay,
                    Some(Overlay {
                        kind: OverlayKind::ContextEntries { .. },
                        ..
                    })
                ) =>
            {
                if let Some(Overlay {
                    kind: OverlayKind::ContextEntries { cursor, .. },
                    ..
                }) = &self.ui.overlay
                {
                    let index = *cursor;
                    self.context_entry_toggle(index);
                }
            }
            "enter" => match &self.ui.overlay {
                Some(Overlay {
                    kind: OverlayKind::Palette(_),
                    ..
                }) => self.palette_activate(None),
                Some(Overlay {
                    kind: OverlayKind::AlertDetail { id, .. },
                    ..
                }) => {
                    let id = *id;
                    self.notice_open_session(id);
                }
                Some(Overlay {
                    kind: OverlayKind::Notifications { selected },
                    ..
                }) => {
                    let sel = *selected;
                    self.notice_activate(sel);
                }
                Some(Overlay {
                    kind: OverlayKind::WorktreeInfo(_),
                    ..
                }) => self.copy_worktree_value(WorktreeCopy::Path),
                Some(Overlay {
                    kind: OverlayKind::SubagentDone { session_id, .. },
                    ..
                }) => {
                    let id = session_id.clone();
                    if self.state.model.sessions.contains_key(&id) {
                        self.close_overlay();
                        self.open_session(&id);
                    }
                }
                _ => {}
            },
            "c" if matches!(
                self.ui.overlay,
                Some(Overlay {
                    kind: OverlayKind::WorktreeInfo(_),
                    ..
                })
            ) =>
            {
                self.copy_worktree_value(WorktreeCopy::Path);
            }
            _ if is_palette => {
                if self.lookup("global", name).is_some() && !keys::printable(ev).is_some() {
                    return false; // global shortcuts still work
                }
                let mut changed = false;
                if let Some(Overlay {
                    kind: OverlayKind::Palette(p),
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    edit_buffer(&mut p.query, ev, name, true);
                    changed = true;
                    p.selected = 0;
                    p.top = 0;
                }
                if changed {
                    self.ask_search();
                }
            }
            _ => return self.lookup("global", name).is_none(),
        }
        true
    }

    fn overlay_move(&mut self, delta: isize) {
        let n_notices = self.ui.alerts.history.len();
        let n_jobs = self.view_jobs().len();
        let area = self.regions.area;
        match self.ui.overlay.as_mut().map(|o| &mut o.kind) {
            // With shells listed, the arrows move the shell selection; the
            // modal scrolls to keep it in view.
            Some(OverlayKind::Resources { kind, selected, .. })
                if *kind != crate::ui::ResourceKind::Kernels && n_jobs > 0 =>
            {
                *selected = (*selected as isize + delta).clamp(0, n_jobs as isize - 1) as usize;
            }
            Some(OverlayKind::Palette(_)) => self.palette_move(delta),
            Some(OverlayKind::Notifications { selected }) => {
                *selected = (*selected as isize + delta)
                    .clamp(0, n_notices.saturating_sub(1) as isize)
                    as usize;
            }
            Some(
                OverlayKind::Help { top }
                | OverlayKind::AlertDetail { top, .. }
                | OverlayKind::Resources { top, .. },
            ) => {
                *top = (*top as isize + delta).max(0) as usize;
            }
            Some(OverlayKind::Inspector(i)) => {
                let page = i.page_mut();
                page.top = (page.top as isize + delta).max(0) as usize;
            }
            Some(OverlayKind::ContextDetail { text, top, .. }) => {
                let max = crate::render::context_detail_max(area, text);
                *top = (*top as isize + delta).clamp(0, max as isize) as usize;
            }
            Some(OverlayKind::ContextDoc { text, top, .. }) => {
                let max = crate::render::context_doc_max(area, &self.theme, text);
                *top = (*top as isize + delta).clamp(0, max as isize) as usize;
            }
            Some(OverlayKind::SubagentDone { text, top, .. }) => {
                let max = crate::render::subagent_done_max(area, text);
                *top = (*top as isize + delta).clamp(0, max as isize) as usize;
            }
            Some(OverlayKind::ContextEntries {
                title,
                entries,
                cursor,
                open,
                top,
                ..
            }) => {
                if title == "Skills" {
                    if let Some(index) = *open {
                        let text = entries[index].full.as_deref().unwrap_or("Loading…");
                        let max = crate::render::context_doc_max(area, &self.theme, text);
                        *top = (*top as isize + delta).clamp(0, max as isize) as usize;
                    } else {
                        *cursor = (*cursor as isize + delta)
                            .clamp(0, entries.len().saturating_sub(1) as isize)
                            as usize;
                    }
                    return;
                }
                // Inside an open entry the arrows read it first, then leave it.
                let extra = open.filter(|o| o == cursor).map_or(0, |_| {
                    crate::render::context_entry_overflow(area, &self.theme, entries, *cursor)
                });
                let inside = *top as isize + delta;
                if open.is_some_and(|o| o == *cursor) && (0..=extra as isize).contains(&inside) {
                    *top = inside as usize;
                } else {
                    *cursor = (*cursor as isize + delta)
                        .clamp(0, entries.len().saturating_sub(1) as isize)
                        as usize;
                    *top = 0;
                }
            }
            Some(OverlayKind::LearnChecklist(checklist)) => {
                checklist.cursor = (checklist.cursor as isize + delta)
                    .clamp(0, checklist.skills.len().saturating_sub(1) as isize)
                    as usize;
            }
            Some(OverlayKind::Usage { rows, top }) => {
                let max = crate::render::usage_scroll_max(area, rows);
                *top = (*top as isize + delta).clamp(0, max as isize) as usize;
            }
            Some(OverlayKind::WorktreeInfo(info)) => {
                let max = crate::render::worktree_scroll_max(area, info);
                info.top = ((info.top as isize + delta).clamp(0, max as isize)) as usize;
            }
            Some(OverlayKind::ImagePreview { .. })
            | Some(OverlayKind::Projects(_))
            | Some(OverlayKind::Annotate(_))
            | None => {}
        }
    }

    /// A notifications row opens the alert's full text (D15).
    fn notice_activate(&mut self, index: usize) {
        if let Some(id) = self.ui.alerts.history.get(index).map(|n| n.id) {
            if let Some(Overlay {
                kind: OverlayKind::Notifications { selected },
                ..
            }) = self.ui.overlay.as_mut()
            {
                *selected = index;
            }
            self.open_alert_detail(id, Some(index));
        }
    }

    /// Go to the session an alert came from, if it has one.
    fn notice_open_session(&mut self, alert: u64) {
        let session = self
            .ui
            .alerts
            .history
            .iter()
            .find(|n| n.id == alert)
            .and_then(|n| n.session.clone());
        if let Some(id) = session.filter(|id| self.state.model.sessions.contains_key(id)) {
            self.close_overlay();
            self.open_session(&id);
        }
    }

    fn cycle_effort(&mut self) {
        if !self.term("has_project").unwrap_or(false) {
            return;
        }
        self.open_picker(PaletteMode::Efforts);
        let items = self.palette_items();
        if !items.is_empty() {
            let current = items
                .iter()
                .position(|item| item.category == "current")
                .unwrap_or(items.len() - 1);
            self.palette_activate(Some((current + 1) % items.len()));
        }
    }

    pub fn pending_inputs(&self) -> Vec<serde_json::Value> {
        self.viewed_id()
            .and_then(|id| self.state.inputs.get(id))
            .and_then(|v| v["inputs"].as_array())
            .cloned()
            .unwrap_or_default()
    }

    fn pending_key(&mut self, name: &str) -> bool {
        // A question owns the keys; queued-row selection does not take them.
        if self.question_pending() && self.ui.focus == Focus::Composer {
            return false;
        }
        let inputs = self.pending_inputs();
        if inputs.is_empty() {
            self.ui.pending_selected = None;
            return false;
        }
        let empty = self.composer_ref().text().is_empty() && self.composer_ref().images.is_empty();
        if self.ui.pending_selected.is_none() && name == "up" && empty {
            self.ui.pending_selected = Some(inputs.len() - 1);
            return true;
        }
        let Some(selected) = self.ui.pending_selected else {
            return false;
        };
        let at = selected.min(inputs.len() - 1);
        match name {
            "up" => self.ui.pending_selected = Some(at.saturating_sub(1)),
            "down" if at + 1 < inputs.len() => self.ui.pending_selected = Some(at + 1),
            "down" | "esc" => self.ui.pending_selected = None,
            "enter" | "backspace" | "s" | "shift+up" | "shift+down" => {
                let item = &inputs[at];
                if name == "backspace" {
                    // Removing is forever; Enter edits the input back into the composer.
                    let text = item["parts"]
                        .as_array()
                        .and_then(|parts| parts.iter().find(|p| p["type"] == "text"))
                        .and_then(|p| p["text"].as_str())
                        .unwrap_or("[image]")
                        .lines()
                        .next()
                        .unwrap_or("");
                    let kind = if item["mode"] == "steer" {
                        "steer"
                    } else {
                        "queued message"
                    };
                    let Some(session) = self.viewed_id().map(str::to_owned) else {
                        return true;
                    };
                    let restore = self.restore_point();
                    self.ui.dialog = Some(Dialog {
                        title: "Remove pending input".into(),
                        body: vec![
                            format!("Remove the {kind} \"{text}\"?"),
                            "This cannot be undone.".into(),
                        ],
                        cancel: "Cancel".into(),
                        confirm: "Remove".into(),
                        danger: true,
                        action: Confirmed::RemoveInput {
                            session,
                            id: item["id"].as_str().unwrap_or("").to_owned(),
                        },
                        button: 0,
                        restore,
                    });
                    self.ui.pending_selected = None;
                    return true;
                }
                if name == "enter" && !empty {
                    self.toast(
                        Level::Warning,
                        "Clear the draft before restoring a pending input",
                    );
                    return true;
                }
                let Some(session) = self.viewed_id().map(str::to_owned) else {
                    return true;
                };
                let mut id = item["id"].as_str().unwrap_or("").to_owned();
                let action = match name {
                    "s" if item["mode"] == "queue" => "steer",
                    "shift+up" | "shift+down" => "move",
                    "enter" => "remove",
                    _ => return true,
                };
                let before = match name {
                    "shift+up" if at > 0 => inputs[at - 1]["id"].as_str().map(str::to_owned),
                    "shift+down" if at + 1 < inputs.len() => {
                        let before = Some(id.clone());
                        id = inputs[at + 1]["id"].as_str().unwrap_or("").to_owned();
                        before
                    }
                    "shift+up" | "shift+down" => return true,
                    _ => None,
                };
                let parts = item["parts"].as_array().cloned().unwrap_or_default();
                self.api(ApiCall::Input {
                    session,
                    id,
                    action: action.into(),
                    before,
                    parts,
                });
                if action == "remove" {
                    self.ui.pending_selected = None;
                }
            }
            "n" => self.pending_next(),
            _ => return false,
        }
        true
    }

    fn pending_next(&mut self) {
        if self.question_pending() || !self.writable() {
            return;
        }
        if self
            .state
            .inputs
            .get(self.viewed_id().unwrap_or(""))
            .is_some_and(|v| v["held"] == true)
        {
            if let Some(session) = self.viewed_id().map(str::to_owned) {
                self.api(ApiCall::Input {
                    session,
                    id: String::new(),
                    action: "next".into(),
                    before: None,
                    parts: vec![],
                });
            }
        }
    }

    /// Run one right-aligned action on queued input `index`. Steer rows have no
    /// actions; queued rows move only within the queued group.
    fn pending_action(&mut self, index: usize, action: PendingAction) {
        let inputs = self.pending_inputs();
        let Some(item) = inputs.get(index) else {
            return;
        };
        if item["mode"] != "queue" {
            return;
        }
        match action {
            PendingAction::Edit => {
                if self.question_pending() {
                    // Restoring a queued message would change the locked draft.
                    self.toast(Level::Info, QUESTION_LOCK);
                    return;
                }
                if !self.writable() {
                    self.toast(Level::Warning, READ_ONLY);
                    return;
                }
                if self.composer_ref().text().is_empty() && self.composer_ref().images.is_empty() {
                    let Some(session) = self.viewed_id().map(str::to_owned) else {
                        return;
                    };
                    let parts = item["parts"].as_array().cloned().unwrap_or_default();
                    self.api(ApiCall::Input {
                        session,
                        id: item["id"].as_str().unwrap_or("").to_owned(),
                        action: "remove".into(),
                        before: None,
                        parts,
                    });
                    self.ui.pending_selected = None;
                } else {
                    self.toast(
                        Level::Warning,
                        "Clear the draft before restoring a pending input",
                    );
                }
            }
            PendingAction::Remove => {
                let text = item["parts"]
                    .as_array()
                    .and_then(|parts| parts.iter().find(|p| p["type"] == "text"))
                    .and_then(|p| p["text"].as_str())
                    .unwrap_or("[image]")
                    .lines()
                    .next()
                    .unwrap_or("");
                let Some(session) = self.viewed_id().map(str::to_owned) else {
                    return;
                };
                let restore = self.restore_point();
                self.ui.dialog = Some(Dialog {
                    title: "Remove pending input".into(),
                    body: vec![
                        format!("Remove the queued message \"{text}\"?"),
                        "This cannot be undone.".into(),
                    ],
                    cancel: "Cancel".into(),
                    confirm: "Remove".into(),
                    danger: true,
                    action: Confirmed::RemoveInput {
                        session,
                        id: item["id"].as_str().unwrap_or("").to_owned(),
                    },
                    button: 0,
                    restore,
                });
                self.ui.pending_selected = None;
            }
            PendingAction::Up | PendingAction::Down => {
                let queued: Vec<usize> = inputs
                    .iter()
                    .enumerate()
                    .filter(|(_, item)| item["mode"] == "queue")
                    .map(|(i, _)| i)
                    .collect();
                let Some(position) = queued.iter().position(|i| *i == index) else {
                    return;
                };
                let (id, before, parts) = if action == PendingAction::Up {
                    let Some(previous) = position.checked_sub(1).and_then(|at| queued.get(at))
                    else {
                        return;
                    };
                    (
                        item["id"].as_str().unwrap_or("").to_owned(),
                        inputs[*previous]["id"].as_str().unwrap_or("").to_owned(),
                        item["parts"].as_array().cloned().unwrap_or_default(),
                    )
                } else {
                    let Some(next) = queued.get(position + 1) else {
                        return;
                    };
                    (
                        inputs[*next]["id"].as_str().unwrap_or("").to_owned(),
                        item["id"].as_str().unwrap_or("").to_owned(),
                        inputs[*next]["parts"]
                            .as_array()
                            .cloned()
                            .unwrap_or_default(),
                    )
                };
                let Some(session) = self.viewed_id().map(str::to_owned) else {
                    return;
                };
                self.api(ApiCall::Input {
                    session,
                    id,
                    action: "move".into(),
                    before: Some(before),
                    parts,
                });
            }
        }
    }

    fn composer_key(&mut self, ev: &KeyEvent, name: &str) {
        if let Some(cmd) = self
            .focus_contexts()
            .iter()
            .find_map(|ctx| self.lookup(ctx, name))
        {
            // Printable keys (`@`, `/`) are text unless their command is wired.
            let printable = keys::printable(ev).is_some();
            if !(printable && !WIRED.contains(&cmd.as_str())) {
                if cmd == "session.send" || cmd == "composer.steer" {
                    return self.send();
                }
                if cmd == "composer.queue" {
                    if self.composer_ref().has_text() || !self.composer_ref().images.is_empty() {
                        return self.send_mode(Some("queue"));
                    }
                    return;
                }
                return self.run_command(&cmd);
            }
        }
        match name {
            "left"
                if self.composer_ref().is_empty()
                    && self.composer_ref().images.is_empty()
                    && self.ui.view == View::Session
                    && self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none() =>
            {
                // From an empty composer: sessions -> projects -> overview.
                if self.narrow() {
                    self.ui.drawer = Some(Side::Left);
                } else {
                    self.ui.left_open = true;
                }
                self.ui.tab = Tab::Sessions;
                self.ui.focus = Focus::Sidebar;
            }
            "ctrl+j" => self.composer().newline(),
            "up" | "down" => {
                let down = name == "down";
                let (row, _) = self.composer_ref().cursor();
                let last = self.composer_ref().expanded_text().matches('\n').count();
                let recall = if down {
                    self.composer_ref().in_history() && row >= last
                } else {
                    row == 0
                };
                if recall && self.composer_ref().has_history() {
                    self.composer().history(!down);
                } else {
                    self.composer().select_move(false);
                    if down {
                        self.composer().down();
                    } else if !self.composer().up() {
                        self.ui.focus = Focus::Timeline;
                    }
                }
            }
            _ => edit_composer(self.composer(), ev, name),
        }
    }

    // ---- timeline blocks -------------------------------------------------------

    /// The blocks of the last frame (selection, toggles, clicks).
    pub fn blocks(&self) -> &[crate::timeline::Block] {
        &self.ui.timeline_blocks
    }

    fn block_default_open(&self, b: &crate::timeline::Block) -> bool {
        use crate::timeline::BlockKind::*;
        if let Some(open) = b.default_open {
            return open;
        }
        match b.kind {
            Turn | User | Assistant => true,
            Context => true,
            Notice | Footer | Handoff | Diagnostic | Reasoning | Group | Tool | Child | Older
            | Code => false,
        }
    }

    fn select_block(&mut self, idx: usize) {
        let Some(b) = self.ui.timeline_blocks.get(idx) else {
            return;
        };
        let before = self.ui.scroll;
        let start = b.start;
        let end = b.end;
        self.ui.timeline_sel = Some(idx);
        self.ui.timeline_sel_key = Some(b.key.clone());
        self.ui.focus = Focus::Timeline;
        let h = self.ui.timeline_height.max(1);
        let total = self.ui.timeline_total;
        let max_top = total.saturating_sub(h);
        let cur = if self.ui.scroll.follow {
            max_top
        } else {
            self.ui.scroll.top.min(max_top)
        };
        let top = if start < cur {
            start
        } else if end > cur + h {
            if end.saturating_sub(start) <= h {
                end.saturating_sub(h)
            } else {
                start
            }
        } else {
            cur
        }
        .min(max_top);
        self.ui.scroll = Scroll {
            follow: top >= max_top,
            top,
        };
        if self.ui.scroll != before {
            self.note_scroll();
        }
    }

    fn move_block(&mut self, delta: isize) {
        let n = self.ui.timeline_blocks.len();
        if n == 0 {
            return;
        }
        let cur = self.ui.timeline_sel.unwrap_or(0) as isize;
        let next = (cur + delta).clamp(0, n as isize - 1) as usize;
        self.select_block(next);
    }

    /// Enter/Space or a click on the selected block.
    fn activate_block(&mut self) {
        let Some(b) = self
            .ui
            .timeline_sel
            .and_then(|i| self.ui.timeline_blocks.get(i))
            .cloned()
        else {
            return;
        };
        if b.kind == crate::timeline::BlockKind::Older && b.key == "older-messages" {
            return self.run_command("turn.load_older");
        }
        if b.kind == crate::timeline::BlockKind::Handoff {
            if let Some(m) = self.message_by_prefix(b.target.as_deref().unwrap_or_default()) {
                let (from, summary) = crate::timeline::handoff_parts(&message_text(&m));
                self.open_overlay(OverlayKind::ContextDetail {
                    title: format!("Handoff from {from}"),
                    text: summary,
                    top: 0,
                });
            }
            return;
        }
        if matches!(
            b.kind,
            crate::timeline::BlockKind::Notice | crate::timeline::BlockKind::Footer
        ) {
            if let Some(info) = b.subagent.clone() {
                self.open_overlay(OverlayKind::SubagentDone {
                    session_id: info.session_id,
                    agent: info.agent,
                    title: info.title,
                    model: info.model,
                    duration: info.duration,
                    state: info.state,
                    text: b.target.clone().unwrap_or_default(),
                    top: 0,
                });
                return;
            }
            if let Some(text) = b.target.clone() {
                let title = text
                    .lines()
                    .next()
                    .unwrap_or("Notice")
                    .trim()
                    .trim_start_matches('[')
                    .trim_end_matches(']')
                    .to_owned();
                self.open_overlay(OverlayKind::ContextDetail {
                    title,
                    text,
                    top: 0,
                });
            }
            return;
        }
        if b.kind == crate::timeline::BlockKind::Child {
            if let Some(id) = b.target.clone() {
                self.open_session(&id);
                return;
            }
        }
        // Enter on an open tool whose output is clipped shows all of it first
        // (`↑ N earlier lines · enter show all`); the next Enter folds the tool.
        if b.kind == crate::timeline::BlockKind::Tool && b.clipped {
            let open = self
                .ui
                .timeline_open
                .get(&b.key)
                .copied()
                .unwrap_or(self.ui.detail == crate::ui::Detail::High);
            let all = format!("{}:all", b.key);
            if open && !self.ui.timeline_open.get(&all).copied().unwrap_or(false) {
                self.ui.timeline_open.insert(all, true);
                return;
            }
        }
        if b.toggle {
            let default = self.block_default_open(&b);
            let now = self
                .ui
                .timeline_open
                .entry(b.key.clone())
                .or_insert(default);
            *now = !*now;
        }
    }

    fn context_keyboard_active(&self) -> bool {
        if self.viewed_id().is_none() {
            return true;
        }
        self.ui
            .timeline_sel
            .and_then(|index| self.ui.timeline_blocks.get(index))
            .is_some_and(|block| block.kind == crate::timeline::BlockKind::Context)
    }

    fn context_key(&mut self, name: &str) -> bool {
        if !self.context_keyboard_active() || self.context_items().is_empty() {
            return false;
        }
        let last = self.context_items().len().saturating_sub(1);
        match name {
            "up" | "k" => self.ui.context_cursor = self.ui.context_cursor.saturating_sub(1),
            "down" | "j" => self.ui.context_cursor = (self.ui.context_cursor + 1).min(last),
            "enter" | "space" => self.toggle_context_item(self.ui.context_cursor),
            _ => return false,
        }
        self.reveal_context_cursor();
        true
    }

    /// Keep a keyboard-selected draft switch visible in the scrollable
    /// new-session context preview.
    fn reveal_context_cursor(&mut self) {
        let items = self.context_items();
        let Some(item) = items.get(self.ui.context_cursor) else {
            return;
        };
        let row = if item.key.starts_with("skill:") {
            7 + item.index
        } else if item.key.starts_with("mcp:") {
            9 + item.index
        } else {
            5 + item.index
        };
        let offset =
            self.state
                .model
                .selected
                .as_ref()
                .and_then(|view| view.starting.as_ref())
                .is_some_and(|starting| starting["origin"] == "reconstructed") as usize;
        let block_start = self
            .ui
            .timeline_blocks
            .iter()
            .find(|block| block.kind == crate::timeline::BlockKind::Context)
            .map_or(0, |block| block.start);
        let row = block_start + offset + row;
        let page = self.ui.timeline_height.max(1);
        let current_top = if self.viewed_id().is_some() && self.ui.scroll.follow {
            self.ui.timeline_total.saturating_sub(page)
        } else {
            self.ui.scroll.top
        };
        let top = if row < current_top {
            row
        } else if row >= current_top + page {
            row + 1 - page
        } else {
            current_top
        };
        if self.viewed_id().is_none() || top != current_top {
            self.ui.scroll = Scroll {
                follow: false,
                top: top.min(self.ui.timeline_total.saturating_sub(page)),
            };
        }
    }

    fn toggle_context_item(&mut self, index: usize) {
        let Some(item) = self.context_items().get(index).cloned() else {
            return;
        };
        let skill_index = self
            .context_items_for("Skills")
            .iter()
            .position(|item| item.index == index);
        if let Some(Overlay {
            kind:
                OverlayKind::ContextEntries {
                    title,
                    cursor,
                    open: None,
                    ..
                },
            ..
        }) = self.ui.overlay.as_mut()
        {
            if title == "Skills" {
                if let Some(index) = skill_index {
                    *cursor = index;
                }
            }
        }
        self.ui.context_cursor = index;
        self.ui.focus = Focus::Timeline;
        if self.viewed_id().is_some() {
            if let Some((block_index, block)) = self
                .ui
                .timeline_blocks
                .iter()
                .enumerate()
                .find(|(_, block)| block.kind == crate::timeline::BlockKind::Context)
            {
                self.ui.timeline_sel = Some(block_index);
                self.ui.timeline_sel_key = Some(block.key.clone());
            }
        }
        if item.locked {
            // A locked entry is often a configuration problem, not something a
            // new session can change. Open its facts instead of a dead-end
            // toast, especially in the new-session preview where "start a new
            // session" is already true.
            let informational = item.key.starts_with("mcp:") || self.viewed_id().is_none();
            if informational {
                if let Some((title, text)) = self.context_item_info(index) {
                    self.open_overlay(OverlayKind::ContextDetail {
                        title,
                        text,
                        top: 0,
                    });
                    return;
                }
            }
            let message = if self.viewed_id().is_none() {
                "This entry is not switchable from the preview; open its details to see its configuration and state."
            } else {
                "This item is locked for this session; start a new session to use it."
            };
            self.toast(Level::Info, message);
            return;
        }
        if !self.writable() {
            self.toast(Level::Warning, READ_ONLY);
            return;
        }
        if let Some(session) = self.viewed().cloned() {
            let mut disabled = disabled_tools(Some(&session));
            if item.on {
                if !disabled.iter().any(|key| key == &item.key) {
                    disabled.push(item.key);
                }
            } else {
                disabled.retain(|key| key != &item.key);
            }
            self.api(ApiCall::SetDisabledTools {
                session: session.id,
                disabled_tools: disabled,
                revision: session.revision,
            });
        } else if item.on {
            if !self
                .ui
                .draft
                .disabled_tools
                .iter()
                .any(|key| key == &item.key)
            {
                self.ui.draft.disabled_tools.push(item.key);
            }
        } else {
            self.ui.draft.disabled_tools.retain(|key| key != &item.key);
        }
    }

    fn adjacent_turn(&mut self, forward: bool) {
        let blocks = self.ui.timeline_blocks.clone();
        let cur = self.ui.timeline_sel.unwrap_or(0);
        let found = if forward {
            blocks
                .iter()
                .enumerate()
                .skip(cur + 1)
                .find(|(_, b)| b.kind == crate::timeline::BlockKind::Turn)
        } else {
            blocks
                .iter()
                .enumerate()
                .take(cur)
                .rev()
                .find(|(_, b)| b.kind == crate::timeline::BlockKind::Turn)
        };
        if let Some((i, _)) = found {
            self.select_block(i);
        }
    }

    fn fold_turns(&mut self, all_but_last: bool) {
        let blocks = self.ui.timeline_blocks.clone();
        let turns: Vec<usize> = blocks
            .iter()
            .enumerate()
            .filter(|(_, b)| b.kind == crate::timeline::BlockKind::Turn)
            .map(|(i, _)| i)
            .collect();
        for (n, i) in turns.iter().enumerate() {
            let key = blocks[*i].key.clone();
            let open = !all_but_last || n + 1 == turns.len();
            self.ui.timeline_open.insert(key, open);
        }
    }

    fn copy_selected(&mut self) {
        let Some(b) = self
            .ui
            .timeline_sel
            .and_then(|i| self.ui.timeline_blocks.get(i))
            .cloned()
        else {
            self.toast(Level::Info, "Select a timeline item first");
            return;
        };
        let text = match b.kind {
            crate::timeline::BlockKind::Tool => {
                let call = b.key.strip_prefix("tool:").unwrap_or_default();
                self.tool_result(call).unwrap_or_default()
            }
            crate::timeline::BlockKind::Assistant | crate::timeline::BlockKind::Code => {
                b.target.clone().unwrap_or_default()
            }
            crate::timeline::BlockKind::User | crate::timeline::BlockKind::Turn => self
                .message_by_prefix(b.target.as_deref().unwrap_or_default())
                .map(|m| message_text(&m))
                .unwrap_or_default(),
            _ => self.final_message().unwrap_or_default(),
        };
        if text.trim().is_empty() {
            self.toast(Level::Info, "Nothing to copy here");
            return;
        }
        self.effects.push(Effect::Copy(text));
        self.toast(Level::Success, "Copied");
    }

    fn message_by_prefix(&self, id: &str) -> Option<Message> {
        self.messages().iter().find(|m| m.id == id).cloned()
    }

    fn final_message(&self) -> Option<String> {
        self.messages()
            .iter()
            .rev()
            .find(|m| m.role == "assistant")
            .map(message_text)
            .filter(|t| !t.trim().is_empty())
    }

    fn tool_result(&self, call_id: &str) -> Option<String> {
        self.messages().iter().find_map(|m| {
            m.parts
                .iter()
                .find(|p| p["type"] == "tool_result" && p["call_id"].as_str() == Some(call_id))
                .map(|p| p["output"].as_str().unwrap_or("").to_owned())
        })
    }

    fn open_inspector(&mut self) {
        let Some(sid) = self
            .viewed_id()
            .map(str::to_owned)
            .or_else(|| self.state.model.sessions.keys().next().cloned())
        else {
            self.toast(Level::Info, "Select a session to inspect");
            return;
        };
        self.open_overlay(OverlayKind::Inspector(crate::inspector::Inspector::new(
            sid,
        )));
        self.refresh_inspector();
    }

    fn inspector_mut(&mut self) -> Option<&mut crate::inspector::Inspector> {
        match self.ui.overlay.as_mut().map(|o| &mut o.kind) {
            Some(OverlayKind::Inspector(i)) => Some(i),
            _ => None,
        }
    }

    /// Fetch the current page. Any response older than this request is ignored.
    pub fn refresh_inspector(&mut self) {
        let Some(i) = self.inspector_mut() else {
            return;
        };
        let generation = i.begin();
        let call = ApiCall::Inspector {
            session: i.session.clone(),
            query: i.query(),
            generation,
        };
        self.api(call);
    }

    fn inspector_act(&mut self, act: crate::inspector::Act) {
        use crate::inspector::Act;
        match act {
            Act::None => {}
            Act::Fetch => self.refresh_inspector(),
            Act::Copy(text) => self.effects.push(Effect::Copy(text)),
            Act::Record { kind, id, row } => {
                let Some(i) = self.inspector_mut() else {
                    return;
                };
                let call = ApiCall::InspectorRecord {
                    session: i.session.clone(),
                    target: i.target.clone(),
                    view: i.view,
                    kind,
                    id,
                    row,
                };
                self.api(call);
            }
        }
    }

    /// Keep the selected row inside the overlay's window.
    fn inspector_follow(&mut self) {
        let (width, height) =
            crate::render::inspector_metrics(Rect::new(0, 0, self.size.0, self.size.1));
        let Some(i) = self.inspector_mut() else {
            return;
        };
        let parts = i.parts(width, 0.0);
        let height = height.saturating_sub(parts.fixed.len()).max(3);
        if let Some(line) = parts.cursor {
            let page = i.page_mut();
            if line < page.top {
                page.top = line;
            } else if line >= page.top + height {
                page.top = line + 1 - height;
            }
        }
    }

    /// Check the head after a coalesced stream invalidation.
    fn inspector_poll(&mut self, now: Instant) {
        let Some(i) = self.inspector_mut() else {
            return;
        };
        if !i.should_poll(now) {
            return;
        }
        i.polled = Some(now);
        i.dirty_at = None;
        i.head_inflight = true;
        let call = ApiCall::InspectorHead {
            session: i.session.clone(),
            target: i.target.clone(),
            generation: i.generation,
        };
        self.api(call);
    }

    fn inspector_done(&mut self, done: ActionDone) {
        let clock = self.clock;
        let Some(i) = self.inspector_mut() else {
            return;
        };
        let mut refetch = false;
        match (&done.call, done.result) {
            (ApiCall::Inspector { generation, .. }, result) => {
                if i.generation != *generation {
                    return; // stale: navigation, filter, view change or a newer refresh won
                }
                i.loading = false;
                match result {
                    Ok(r) => {
                        let data = r.inspection.unwrap_or_default();
                        i.connected = true;
                        i.error.clear();
                        i.newer = false;
                        i.polled = Some(clock);
                        let gone = data["tree"].as_array().is_some_and(|t| {
                            !t.is_empty() && !t.iter().any(|n| n["id"].as_str() == Some(&i.target))
                        });
                        i.set_data(data, now_secs());
                        if gone && i.target != i.session {
                            i.target = i.session.clone();
                            refetch = true;
                        }
                    }
                    Err(e) => {
                        i.error = e;
                        i.connected = false;
                    }
                }
            }
            (ApiCall::InspectorHead { generation, .. }, result) => {
                i.head_inflight = false;
                if i.generation != *generation {
                    return;
                }
                match result {
                    Ok(r) => refetch = i.on_head(&r.inspection.unwrap_or_default()),
                    Err(_) => i.connected = false,
                }
            }
            (
                ApiCall::InspectorRecord {
                    target, view, row, ..
                },
                result,
            ) => match result {
                Ok(r) => i.accept_record(target, *view, row, &r.inspection.unwrap_or_default()),
                Err(e) => {
                    i.loading_full = None;
                    i.error = format!("Full record failed to load: {e}");
                }
            },
            _ => {}
        }
        if refetch {
            self.refresh_inspector();
        }
        self.inspector_follow();
    }

    /// Esc cancels an open filter field first, then closes the inspector.
    fn inspector_escape(&mut self) -> bool {
        match self.inspector_mut() {
            Some(i) if i.edit.is_some() => {
                i.edit = None;
                true
            }
            _ => false,
        }
    }

    fn inspector_key(&mut self, ev: &KeyEvent, name: &str) {
        use crate::inspector::Act;
        let Some(i) = self.inspector_mut() else {
            return;
        };
        if let Some((_, buffer)) = i.edit.as_mut() {
            match name {
                "enter" => {
                    let act = i.finish_edit();
                    self.inspector_act(act);
                }
                "backspace" => {
                    buffer.pop();
                }
                _ => {
                    if let Some(c) = keys::printable(ev) {
                        buffer.push(c);
                    }
                }
            }
            return;
        }
        if let Some(cmd) = self.lookup("inspector", name) {
            return self.inspector_command(&cmd);
        }
        let Some(i) = self.inspector_mut() else {
            return;
        };
        match name {
            "+" => i.zoom(true),
            "up" => i.page_mut().top = i.page().top.saturating_sub(1),
            "down" => i.page_mut().top += 1,
            "pageup" => i.page_mut().top = i.page().top.saturating_sub(8),
            "pagedown" => i.page_mut().top += 8,
            "home" => i.page_mut().top = 0,
            _ => {}
        }
        let _ = Act::None;
    }

    /// Run one `inspector.*` manifest command.
    pub fn inspector_command(&mut self, id: &str) {
        use crate::inspector::{Act, Field};
        let Some(i) = self.inspector_mut() else {
            return;
        };
        let act = match id {
            "inspector.view_timing" => i.set_view(0),
            "inspector.view_conversation" => i.set_view(1),
            "inspector.view_steps" => i.set_view(2),
            "inspector.view_requests" => i.set_view(3),
            "inspector.view_raw" => i.set_view(4),
            "inspector.view_questions" => i.set_view(5),
            "inspector.refresh" => Act::Fetch,
            "inspector.live_toggle" => i.toggle_live(),
            "inspector.session_next" => i.step_session(1),
            "inspector.session_prev" => i.step_session(-1),
            "inspector.search" => {
                i.begin_edit(Field::Q);
                Act::None
            }
            "inspector.filter_kind" => i.cycle("kind"),
            "inspector.filter_status" => i.cycle("status"),
            "inspector.filter_type" => {
                i.begin_edit(Field::Type);
                Act::None
            }
            "inspector.filter_clear" => i.clear_filters(),
            "inspector.select_next" => {
                i.select(1);
                Act::None
            }
            "inspector.select_prev" => {
                i.select(-1);
                Act::None
            }
            "inspector.expand" => {
                i.expand();
                Act::None
            }
            "inspector.load_full" => i.load_full(),
            "inspector.older" => i.older(),
            "inspector.newer" => i.newer_page(),
            "inspector.copy" => Act::Copy(i.copy_text()),
            "inspector.cache_info" => {
                i.info = !i.info;
                Act::None
            }
            "inspector.zoom_in" => {
                i.zoom(true);
                Act::None
            }
            "inspector.zoom_out" => {
                i.zoom(false);
                Act::None
            }
            "inspector.pan_left" => {
                i.pan(false);
                Act::None
            }
            "inspector.pan_right" => {
                i.pan(true);
                Act::None
            }
            "inspector.fit" => {
                i.fit();
                Act::None
            }
            _ => Act::None,
        };
        self.inspector_act(act);
        self.inspector_follow();
    }

    fn timeline_key(&mut self, name: &str) {
        if self.context_key(name) {
            return;
        }
        if let Some(cmd) = self.lookup("timeline", name) {
            // Left is the manifest's root-back command only while a child is viewed;
            // on a root session it keeps collapsing the selected block.
            let inactive_root_back =
                cmd == "nav.root" && !self.viewed().is_some_and(|s| s.parent_id.is_some());
            if WIRED.contains(&cmd.as_str()) && !inactive_root_back {
                return self.run_command(&cmd);
            }
        }
        let page = self.ui.timeline_height.saturating_sub(1).max(1) as isize;
        match name {
            "up" | "k" => self.move_block(-1),
            "down" | "j" => self.move_block(1),
            "home" => {
                if self.ui.timeline_blocks.is_empty() {
                    self.ui.scroll = Scroll {
                        follow: false,
                        top: 0,
                    };
                    self.note_scroll();
                } else {
                    self.select_block(0);
                }
            }
            "end" => {
                let n = self.ui.timeline_blocks.len();
                if n == 0 {
                    if self.viewed_id().is_none() {
                        self.ui.scroll = Scroll {
                            follow: false,
                            top: self
                                .ui
                                .timeline_total
                                .saturating_sub(self.ui.timeline_height),
                        };
                    } else {
                        self.ui.scroll = Scroll::default();
                    }
                    self.note_scroll();
                } else {
                    self.select_block(n - 1);
                }
            }
            "pageup" => self.scroll_timeline(-page),
            "pagedown" => self.scroll_timeline(page),
            // The manifest binds Enter to tool.expand_toggle and Space to
            // turn.fold_toggle; both act on the selected block.
            "space" | "enter" => self.activate_block(),
            "left" | "h" => {
                if let Some(b) = self
                    .ui
                    .timeline_sel
                    .and_then(|i| self.ui.timeline_blocks.get(i))
                    .cloned()
                {
                    if b.toggle {
                        self.ui.timeline_open.insert(b.key, false);
                    }
                }
            }
            "right" | "l" => {
                if let Some(b) = self
                    .ui
                    .timeline_sel
                    .and_then(|i| self.ui.timeline_blocks.get(i))
                    .cloned()
                {
                    if b.toggle {
                        self.ui.timeline_open.insert(b.key, true);
                    }
                }
            }
            "tab" => self.cycle_focus(false),
            "shift+tab" => self.cycle_focus(true),
            _ => {
                // Keys bound to the focused block's own context (a tool's
                // `r` raw, `o` show all, `i` inspector) follow the timeline's.
                let kind = self
                    .ui
                    .timeline_sel
                    .and_then(|i| self.ui.timeline_blocks.get(i))
                    .map(|b| b.kind);
                let own = match kind {
                    Some(crate::timeline::BlockKind::Tool) => Some("tool"),
                    Some(crate::timeline::BlockKind::Turn) => Some("turn"),
                    _ => None,
                };
                let cmd = self
                    .lookup("timeline", name)
                    .or_else(|| own.and_then(|ctx| self.lookup(ctx, name)));
                if let Some(cmd) = cmd {
                    self.run_command(&cmd);
                }
            }
        }
    }

    pub fn scroll_timeline(&mut self, delta: isize) {
        let max_top = self
            .ui
            .timeline_total
            .saturating_sub(self.ui.timeline_height);
        let preview = self.viewed_id().is_none();
        let cur = if self.ui.scroll.follow && !preview {
            max_top
        } else {
            self.ui.scroll.top.min(max_top)
        };
        let top = (cur as isize + delta).clamp(0, max_top as isize) as usize;
        self.ui.scroll = Scroll {
            follow: !preview && top >= max_top,
            top,
        };
        self.note_scroll();
    }

    /// Show the timeline scroll bar for a moment after the user scrolls.
    pub fn note_scroll(&mut self) {
        if self.ui.scrollbar {
            self.ui.scroll_mark = Some(self.clock);
        }
    }

    /// The bar's state after `now`: full, fading, or hidden (`None`).
    pub fn scrollbar_phase(&self, now: Instant) -> Option<bool> {
        let at = self.ui.scroll_mark?;
        let age = now.saturating_duration_since(at);
        if age < scrollbar_visible() {
            Some(false)
        } else if age < scrollbar_visible() + scrollbar_fade() {
            Some(true)
        } else {
            None
        }
    }

    fn sidebar_key(&mut self, name: &str) {
        let n = self.list_len(self.ui.tab);
        let c = self.cursor(self.ui.tab);
        let set = |s: &mut Self, v: usize| {
            s.ui.cursor.insert(s.ui.tab, v.min(n.saturating_sub(1)));
        };
        match name {
            "enter" if matches!(self.ui.tab, Tab::Files | Tab::Git) => self.sidebar_activate(c),
            "up" | "k" => set(self, c.saturating_sub(1)),
            "down" | "j" => set(self, c + 1),
            "home" => set(self, 0),
            "end" => set(self, n),
            "backspace" if self.ui.tab == Tab::Files => self.files_up(),
            "pageup" => set(self, c.saturating_sub(8)),
            "pagedown" => set(self, c + 8),
            "left" | "h" if self.ui.tab == Tab::Files => self.files_up(),
            "right" | "l" if self.ui.tab == Tab::Files => self.files_expand(),
            "left" | "h" if self.ui.tab == Tab::Sessions => self.set_tab(Tab::Projects),
            "left" | "h" if self.ui.tab == Tab::Projects => self.open_projects_overview(),
            "right" | "l" if self.ui.tab == Tab::Projects => self.set_tab(Tab::Sessions),
            "right" | "l" if self.ui.tab == Tab::Sessions => self.ui.focus = Focus::Composer,
            "left" | "h" => self.set_tab(Tab::Sessions),
            "right" | "l" => self.set_tab(Tab::Files),
            "tab" => self.cycle_focus(false),
            "shift+tab" => self.cycle_focus(true),
            _ => {
                if let Some(cmd) = self
                    .focus_contexts()
                    .iter()
                    .find_map(|ctx| self.lookup(ctx, name))
                {
                    self.run_command(&cmd);
                }
            }
        }
    }

    /// Keys in the right sidebar: move among changed files, Enter opens a diff.
    fn right_key(&mut self, name: &str) {
        let n = self.git_rows().len();
        let c = self.ui.git_cursor;
        match name {
            "up" | "k" => self.ui.git_cursor = c.saturating_sub(1),
            "down" | "j" => self.ui.git_cursor = (c + 1).min(n.saturating_sub(1)),
            "home" => self.ui.git_cursor = 0,
            "end" => self.ui.git_cursor = n.saturating_sub(1),
            "tab" => self.cycle_focus(false),
            "shift+tab" => self.cycle_focus(true),
            _ => {
                if let Some(cmd) = self
                    .focus_contexts()
                    .iter()
                    .find_map(|ctx| self.lookup(ctx, name))
                {
                    self.run_command(&cmd);
                }
            }
        }
    }

    /// The open diff's lines, in the group's part of Core's `/git/diff` body.
    pub fn diff_lines(&self) -> Vec<String> {
        let Some((path, group)) = &self.ui.diff_open else {
            return Vec::new();
        };
        let Some(Ok(data)) = self
            .state
            .diff
            .as_ref()
            .filter(|(session, p, _)| {
                (Some(session.as_str()) == self.viewed_id()
                    || self
                        .current_project()
                        .is_some_and(|id| session == &format!("project:{id}")))
                    && p == path
            })
            .map(|(_, _, result)| result)
        else {
            return Vec::new();
        };
        let part = match group {
            GitGroup::Staged => "staged",
            _ => "unstaged",
        };
        data[part]
            .as_str()
            .unwrap_or("")
            .lines()
            .map(str::to_owned)
            .collect()
    }

    fn waiting_key(&mut self, ev: &KeyEvent, name: &str) {
        let question = self.ask_current().is_some();
        if let Some(cmd) = self.lookup("waiting", name) {
            // An approval's letter keys must not eat a question's typing.
            let approval_only = matches!(
                cmd.as_str(),
                "waiting.allow_once"
                    | "waiting.allow_session"
                    | "waiting.deny"
                    | "waiting.deny_note"
            );
            if !(question && approval_only) {
                return self.run_command(&cmd);
            }
        }
        if question {
            return self.ask_key(ev, name);
        }
        if self
            .wait_focused()
            .is_some_and(|w| w.kind == WaitKind::Approval)
        {
            match name {
                "up" | "down" => {
                    let step = if name == "down" { 1 } else { 3 };
                    self.ui.wait_choice = (self.ui.wait_choice + step) % 4;
                    return;
                }
                "enter" => {
                    let id = match self.ui.wait_choice {
                        1 => "waiting.allow_session",
                        2 => "waiting.deny",
                        3 => "waiting.deny_note",
                        _ => "waiting.allow_once",
                    };
                    return self.run_command(id);
                }
                _ => {}
            }
        }
        if name == "left" {
            self.execute("waiting.prev");
        } else if name == "right" {
            self.execute("waiting.next");
        }
    }

    // ---- settings view ---------------------------------------------------------------------

    /// Sections shown in Settings' left rail, filtered by the search text.
    pub fn settings_sections(&self) -> Vec<crate::manifest::Section> {
        let query = self.ui.settings_search.text().to_lowercase();
        let query = query.trim();
        self.manifest
            .settings_for_tui()
            .into_iter()
            .filter(|section| section.page || !section.row.is_empty())
            .filter(|section| {
                query.is_empty()
                    || section.label.to_lowercase().contains(query)
                    || section.note.to_lowercase().contains(query)
                    || section.row.iter().any(|row| row_matches(row, query))
                    || self
                        .page_search_terms(section)
                        .iter()
                        .any(|term| term.to_lowercase().contains(query))
            })
            .collect()
    }

    /// The section the right pane shows. A filtered-away selection falls back to
    /// the first visible section until the search is cleared.
    pub fn settings_section(&self) -> Option<crate::manifest::Section> {
        let sections = self.settings_sections();
        sections
            .iter()
            .find(|section| section.id == self.ui.settings_section)
            .or_else(|| sections.first())
            .cloned()
    }

    /// The selected row of `settings_sections`; 0 when the selection is filtered out.
    pub fn settings_section_index(&self) -> usize {
        self.settings_sections()
            .iter()
            .position(|section| section.id == self.ui.settings_section)
            .unwrap_or(0)
    }

    /// The open page, if the right pane is showing one: an Agents detail page,
    /// or a section marked `page = true`. Search results always win.
    pub fn settings_open_page(&self) -> Option<String> {
        if !self.ui.settings_search.text().trim().is_empty() {
            return None;
        }
        self.ui
            .settings_page
            .clone()
            .or_else(|| self.settings_section().filter(|s| s.page).map(|s| s.id))
    }

    /// The settings rows in the right pane: the selected section, or every
    /// section that matches when the search is active.
    pub fn settings_items(&self) -> Vec<SettingsItem> {
        let query = self.ui.settings_search.text().to_lowercase();
        let query = query.trim();
        let mut out = Vec::new();
        // Configuration problems belong where Core's other state is explained,
        // not in a transient toast on every launch.
        let problems: Vec<String> = self
            .state
            .config_error
            .iter()
            .cloned()
            .chain(
                self.state
                    .config
                    .as_ref()
                    .and_then(|config| config["errors"].as_array())
                    .into_iter()
                    .flatten()
                    .filter_map(|error| error.as_str().map(str::to_owned)),
            )
            .filter(|error| query.is_empty() || error.to_lowercase().contains(query))
            .collect();
        if !problems.is_empty() {
            out.push(SettingsItem::Heading("Configuration".into()));
            out.extend(problems.into_iter().map(SettingsItem::Note));
        }
        if query.is_empty() {
            if let Some(section) = self.settings_section().filter(|s| !s.page) {
                for row in section.row {
                    out.push(SettingsItem::Row {
                        row: Box::new(row),
                        section_reason: section.unavailable_reason.clone(),
                    });
                }
            }
            return out;
        }
        for section in self.manifest.settings_for_tui() {
            let section_hit = section.label.to_lowercase().contains(query);
            if section.page {
                let hit = section_hit
                    || section.note.to_lowercase().contains(query)
                    || self
                        .page_search_terms(&section)
                        .iter()
                        .any(|t| t.to_lowercase().contains(query));
                if hit {
                    out.push(SettingsItem::Heading(section.label.clone()));
                    out.push(SettingsItem::Row {
                        row: Box::new(page_row(&section)),
                        section_reason: section.unavailable_reason.clone(),
                    });
                }
                continue;
            }
            let rows: Vec<_> = section
                .row
                .iter()
                .filter(|row| section_hit || row_matches(row, query))
                .cloned()
                .collect();
            if rows.is_empty() {
                continue;
            }
            out.push(SettingsItem::Heading(section.label.clone()));
            for row in rows {
                out.push(SettingsItem::Row {
                    row: Box::new(row),
                    section_reason: section.unavailable_reason.clone(),
                });
            }
        }
        out
    }

    /// Move the left rail to section `index` (in `settings_sections`), clearing search.
    pub fn select_settings_section(&mut self, index: usize) {
        if let Some(section) = self.settings_sections().get(index) {
            self.ui.settings_section = section.id.clone();
            self.ui.settings_page = None;
            self.ui.settings_page_row = 0;
            self.ui.settings_row = 0;
            self.ui.settings_search = Composer::new();
        }
    }

    /// Indexes of the selectable rows in `settings_items`.
    pub fn settings_rows(&self) -> Vec<usize> {
        self.settings_items()
            .iter()
            .enumerate()
            .filter(|(_, item)| matches!(item, SettingsItem::Row { .. }))
            .map(|(i, _)| i)
            .collect()
    }

    /// Keys while the settings modal is open. Tab moves between the section
    /// rail and the right pane; typing filters both. Esc clears the search,
    /// steps back to the rail, then closes. Returns false for keys the shell
    /// should still handle (palette, quit).
    fn settings_key(&mut self, name: &str, code: KeyCode, mods: KeyModifiers) -> bool {
        if self.ui.provider_detail.is_some() {
            return self.provider_detail_key(name, code);
        }
        if name == self.manifest.settings.navigation.section_key
            && (self.ui.settings_nav_focus || self.settings_open_page().is_some())
        {
            self.ui.settings_nav_focus = true;
            return true;
        }
        if !self.ui.settings_nav_focus
            && name == self.manifest.settings.navigation.previous_value_key
        {
            if self.settings_open_page().is_some() {
                return self.settings_page_key("left", code);
            }
            self.settings_change(self.ui.settings_row, -1);
            return true;
        }
        if !self.ui.settings_nav_focus && self.settings_open_page().is_some() {
            return self.settings_page_key(name, code);
        }
        if self.ui.settings_nav_focus {
            let sections = self.settings_sections();
            let last = sections.len().saturating_sub(1);
            let sel = self.settings_section_index().min(last);
            match name {
                "esc" => {
                    if self.ui.settings_search.text().is_empty() {
                        self.execute("app.settings");
                    } else {
                        self.ui.settings_search = Composer::new();
                        self.ui.settings_row = 0;
                    }
                }
                "up" => {
                    self.select_settings_section(sel.saturating_sub(1));
                }
                "down" => {
                    self.select_settings_section((sel + 1).min(last));
                }
                "tab" => {
                    self.ui.settings_nav_focus = false;
                    self.ui.settings_page_row = 0;
                }
                "home" => self.select_settings_section(0),
                "end" => self.select_settings_section(last),
                "enter" | "right" => {
                    self.ui.settings_nav_focus = false;
                    self.ui.settings_row = 0;
                    self.ui.settings_page_row = 0;
                }
                "left" => {}
                "backspace" => {
                    self.ui.settings_search.backspace();
                    self.ui.settings_row = 0;
                }
                _ => match code {
                    KeyCode::Char(c) if mods.is_empty() || mods == KeyModifiers::SHIFT => {
                        self.ui.settings_search.insert_char(c);
                        self.ui.settings_row = 0;
                        self.ui.settings_nav_focus = false;
                    }
                    _ => return false,
                },
            }
            return true;
        }
        let rows = self.settings_rows();
        let last = rows.len().saturating_sub(1);
        let sel = self.ui.settings_row.min(last);
        match name {
            "esc" => {
                if !self.ui.settings_search.text().is_empty() {
                    self.ui.settings_search = Composer::new();
                    self.ui.settings_row = 0;
                } else {
                    self.ui.settings_nav_focus = true;
                }
            }
            "up" => self.ui.settings_row = sel.saturating_sub(1),
            "down" => self.ui.settings_row = (sel + 1).min(last),
            "tab" | "shift+tab" => self.ui.settings_nav_focus = true,
            "pageup" => self.ui.settings_row = sel.saturating_sub(8),
            "pagedown" => self.ui.settings_row = (sel + 8).min(last),
            "home" => self.ui.settings_row = 0,
            "end" => self.ui.settings_row = last,
            "left" => {
                if self.settings_selected_has_value(sel) {
                    self.settings_change(sel, -1);
                }
            }
            "right" => {
                if self.settings_selected_has_value(sel) {
                    self.settings_change(sel, 1);
                }
            }
            "enter" => self.settings_activate(sel),
            "backspace" => {
                self.ui.settings_search.backspace();
                self.ui.settings_row = 0;
            }
            _ => match code {
                KeyCode::Char(c) if mods.is_empty() || mods == KeyModifiers::SHIFT => {
                    self.ui.settings_search.insert_char(c);
                    self.ui.settings_row = 0;
                }
                _ => return false,
            },
        }
        true
    }

    pub fn settings_wired(id: &str) -> bool {
        matches!(
            id,
            "providers.page"
                | "agents.page"
                | "agents.default_effort"
                | "agents.default_root"
                | "tools.default_permission"
                | "tools.defaults"
                | "tools.search_status"
                | "tools.search_start"
                | "appearance.theme"
                | "appearance.monochrome"
                | "appearance.animations"
                | "notifications.sound"
                | "notifications.terminal"
                | "appearance.sidebar"
                | "appearance.details"
                | "appearance.timeline_detail"
                | "appearance.scrollbar"
                | "appearance.session_scope"
                | "appearance.show_archived"
                | "mcp.full_definitions"
                | "mcp.max_listed_tools"
                | "zmx.claude_backend"
                | "zmx.antigravity"
                | "context.handoff_percent"
                | "context.usage_refresh_minutes"
                | "context.title_models"
                | "manager.enabled"
                | "manager.mode"
                | "manager.model"
                | "manager.proactive"
                | "manager.privacy"
                | "dictation.push_to_talk"
                | "dictation.stt_model"
                | "dictation.stt_download"
                | "dictation.stt_remove"
                | "dictation.tts_model"
                | "dictation.tts_download"
                | "dictation.tts_remove"
                | "about.versions"
                | "about.daemon"
                | "about.inspector"
                | "keybindings.list"
        )
    }

    /// The row at selectable position `index`, when it can be changed.
    fn settings_row_at(&mut self, index: usize) -> Option<crate::manifest::Row> {
        let items = self.settings_items();
        let at = *self.settings_rows().get(index)?;
        let SettingsItem::Row {
            row,
            section_reason,
        } = items.into_iter().nth(at)?
        else {
            return None;
        };
        if let Some(why) = self.settings_reason(&row, section_reason.as_deref()) {
            self.toast(Level::Warning, format!("{}: {why}", row.label));
            return None;
        }
        Some(*row)
    }

    /// The segmented options of a Settings value, and its current option: the
    /// same control NSH draws. `None` for rows that are not a choice or switch.
    pub fn settings_segments(
        &self,
        row: &crate::manifest::Row,
    ) -> Option<(Vec<&'static str>, String)> {
        let values = self.state.config.as_ref().map(|c| &c["values"]);
        let on_off = |on: bool| if on { "on" } else { "off" }.to_owned();
        let pair = |options: &[&'static str], current: String| Some((options.to_vec(), current));
        match row.id.as_str() {
            "appearance.theme" => pair(
                &["light", "dark"],
                match self.theme.opts.mode {
                    Mode::Dark => "dark",
                    Mode::Light => "light",
                }
                .to_owned(),
            ),
            "appearance.monochrome" => pair(&["off", "on"], on_off(self.theme.mono())),
            "appearance.animations" => {
                pair(&["on", "off"], on_off(!self.theme.opts.reduced_motion))
            }
            "appearance.sidebar" => pair(
                &["full", "rail"],
                if self.ui.left_open { "full" } else { "rail" }.to_owned(),
            ),
            "appearance.details" => pair(&["off", "on"], on_off(self.ui.right_open)),
            "appearance.session_scope" => pair(
                &["this project", "all projects"],
                if self.ui.scope_all {
                    "all projects"
                } else {
                    "this project"
                }
                .to_owned(),
            ),
            "appearance.show_archived" => pair(&["off", "on"], on_off(self.ui.show_archived)),
            "appearance.timeline_detail" => pair(
                &["low", "medium", "high"],
                self.ui.detail.label().to_owned(),
            ),
            "appearance.scrollbar" => pair(&["off", "on"], on_off(self.ui.scrollbar)),
            "notifications.sound" => pair(&["off", "on"], on_off(self.ui.sound)),
            "notifications.terminal" => pair(
                &["off", "waiting", "all"],
                match self.ui.notify {
                    Notify::Off => "off",
                    Notify::Waiting => "waiting",
                    Notify::All => "all",
                }
                .to_owned(),
            ),
            "mcp.full_definitions" => pair(
                &["off", "on"],
                on_off(
                    values
                        .and_then(|v| v["mcp"]["full_definitions"].as_bool())
                        .unwrap_or(false),
                ),
            ),
            "zmx.claude_backend" => pair(
                &["Agent SDK", "ZMX"],
                if values.and_then(|v| v["zmx"]["claude_backend"].as_str()) == Some("zmx") {
                    "ZMX"
                } else {
                    "Agent SDK"
                }
                .to_owned(),
            ),
            "zmx.antigravity" => pair(
                &["off", "ZMX"],
                if values
                    .and_then(|v| v["zmx"]["antigravity"].as_bool())
                    .unwrap_or(false)
                {
                    "ZMX"
                } else {
                    "off"
                }
                .to_owned(),
            ),
            "tools.default_permission" => pair(
                &["allow", "ask"],
                values
                    .and_then(|v| v["defaults"]["permission"].as_str())
                    .unwrap_or("allow")
                    .to_owned(),
            ),
            "manager.enabled" => pair(
                &["off", "on"],
                self.manager_setting_value("manager.enabled")?.to_owned(),
            ),
            "manager.mode" => pair(
                &["pipeline", "live"],
                self.manager_setting_value("manager.mode")?.to_owned(),
            ),
            "manager.proactive" => pair(
                &["off", "attention", "all"],
                self.manager_setting_value("manager.proactive")?.to_owned(),
            ),
            _ => None,
        }
    }

    /// Pick an option of a segmented Settings row, as clicking it in NSH does.
    fn settings_choose(&mut self, index: usize, option: usize) {
        let Some(row) = self.settings_row_at(index) else {
            return;
        };
        let Some((options, current)) = self.settings_segments(&row) else {
            return;
        };
        let Some(&chosen) = options.get(option) else {
            return;
        };
        if chosen == current {
            return;
        }
        self.settings_apply_row(&row, chosen);
    }

    /// Apply exactly one option of a segmented row.
    fn settings_apply_row(&mut self, row: &crate::manifest::Row, chosen: &str) {
        match row.id.as_str() {
            "appearance.theme" => {
                let mut options = self.theme.opts;
                options.mode = match chosen {
                    "light" => Mode::Light,
                    _ => Mode::Dark,
                };
                self.theme = self.theme.with(options);
            }
            "appearance.monochrome" => {
                if self.theme.mono() != (chosen == "on") {
                    self.toggle_monochrome();
                }
            }
            "appearance.animations" => {
                let reduced = chosen == "off";
                if self.theme.opts.reduced_motion != reduced {
                    let mut options = self.theme.opts;
                    options.reduced_motion = reduced;
                    self.theme = self.theme.with(options);
                }
            }
            "appearance.sidebar" => self.ui.left_open = chosen == "full",
            "appearance.details" => {
                self.ui.right_open = chosen == "on";
                self.ui.right_chosen = true;
            }
            "appearance.session_scope" => {
                if self.ui.scope_all != (chosen == "all projects") {
                    self.run_command("sessions.scope_toggle");
                }
            }
            "appearance.show_archived" => {
                if self.ui.show_archived != (chosen == "on") {
                    self.run_command("sessions.archived_filter");
                }
            }
            "appearance.timeline_detail" => {
                if let Some(detail) = crate::ui::Detail::from_label(chosen) {
                    self.set_detail(detail);
                }
            }
            "appearance.scrollbar" => {
                self.ui.scrollbar = chosen == "on";
                if !self.ui.scrollbar {
                    self.ui.scroll_mark = None;
                }
            }
            "notifications.sound" => self.ui.sound = chosen == "on",
            "notifications.terminal" => {
                self.ui.notify = match chosen {
                    "off" => Notify::Off,
                    "all" => Notify::All,
                    _ => Notify::Waiting,
                };
            }
            "mcp.full_definitions" => {
                self.patch_config_value("mcp.full_definitions", json!(chosen == "on"))
            }
            "tools.default_permission" => {
                self.patch_config_value("defaults.permission", json!(chosen))
            }
            "zmx.claude_backend" => self.patch_config_value(
                "zmx.claude_backend",
                json!(if chosen == "ZMX" { "zmx" } else { "sdk" }),
            ),
            "zmx.antigravity" => self.patch_config_value("zmx.antigravity", json!(chosen == "ZMX")),
            "manager.enabled" => self.patch_manager_value("manager.enabled", json!(chosen == "on")),
            "manager.mode" => self.patch_manager_value("manager.mode", json!(chosen)),
            "manager.proactive" => self.patch_manager_value("manager.proactive", json!(chosen)),
            _ => {}
        }
    }

    /// Patch one Core config value, when the configuration is loaded.
    fn patch_config_value(&mut self, path: &str, value: Value) {
        let Some(config) = &self.state.config else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        let Some(revision) = config["revision"].as_str().map(str::to_owned) else {
            return;
        };
        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
            revision,
            path: path.to_owned(),
            value,
        }));
    }

    /// Patch one Manager config value, respecting Core's read-only state.
    fn patch_manager_value(&mut self, path: &str, value: Value) {
        let Some(config) = &self.state.config else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        let Some(revision) = config["revision"].as_str().map(str::to_owned) else {
            return;
        };
        self.manager_set_config(revision, path, value);
    }

    /// Disabled reason shared by the Settings renderer and its key handlers.
    pub fn settings_reason(
        &self,
        row: &crate::manifest::Row,
        section_reason: Option<&str>,
    ) -> Option<String> {
        if let Some(reason) = row.unavailable_reason.as_deref().or(section_reason) {
            return Some(reason.to_owned());
        }
        // ZMX needs POSIX PTYs; Core says why when it cannot run here (NSH shows the same reason).
        if row.id.starts_with("zmx.") {
            if let Some(reason) = self
                .state
                .config
                .as_ref()
                .and_then(|c| c["zmx_unavailable"].as_str())
            {
                return Some(reason.to_owned());
            }
        }
        if !Self::settings_wired(&row.id) {
            return Some(NOT_WIRED.into());
        }
        if row.id == "tools.search_start" {
            return self.search_start_reason();
        }
        self.voice_action_reason(&row.id)
    }

    /// Supporting detail for the selected Settings row. Voice status fields
    /// come from Core, not local cache inspection.
    pub fn settings_details(
        &self,
        row: &crate::manifest::Row,
        section_reason: Option<&str>,
    ) -> Vec<String> {
        if let Some(reason) = self.settings_reason(row, section_reason) {
            return vec![reason];
        }
        match row.id.as_str() {
            "tools.defaults" => self.tools_grid_lines(),
            "tools.search_status" => self.search_status_lines(),
            "tools.search_start" => vec![
                "Starts the local SearXNG that web_search uses (Apple container, else Docker)."
                    .into(),
            ],
            "dictation.stt_model" => self.voice_info_details(VoiceTarget::Stt),
            "dictation.tts_model" => self.voice_info_details(VoiceTarget::Tts),
            "dictation.stt_download" => {
                vec!["Review the repository and size before downloading from Hugging Face.".into()]
            }
            "dictation.tts_download" => {
                vec!["Review the repository and size before downloading from Hugging Face.".into()]
            }
            "dictation.stt_remove" | "dictation.tts_remove" => {
                vec!["Confirms removal of only this repository from the Hugging Face cache.".into()]
            }
            _ => vec![row.note.clone()],
        }
    }

    /// Left/right: step a choice or flip a toggle.
    fn settings_change(&mut self, index: usize, step: isize) {
        let Some(row) = self.settings_row_at(index) else {
            return;
        };
        self.settings_change_row(row, step);
    }

    /// Whether the selected row has a value the arrow keys can move (a segment
    /// or a stepper). Actions and information rows ignore Left/Right.
    fn settings_selected_has_value(&self, index: usize) -> bool {
        let items = self.settings_items();
        let Some(at) = self.settings_rows().get(index).copied() else {
            return false;
        };
        let Some(SettingsItem::Row { row, .. }) = items.get(at) else {
            return false;
        };
        self.settings_segments(row).is_some()
            || matches!(
                row.id.as_str(),
                "agents.default_effort"
                    | "agents.default_root"
                    | "tools.defaults"
                    | "context.usage_refresh_minutes"
                    | "context.handoff_percent"
                    | "mcp.max_listed_tools"
            )
    }

    fn settings_change_row(&mut self, row: crate::manifest::Row, step: isize) {
        // A segmented value moves one option per arrow, wrapping at the ends so
        // Enter still cycles a two-option switch.
        if let Some((options, current)) = self.settings_segments(&row) {
            if !options.is_empty() {
                let at = options
                    .iter()
                    .position(|option| *option == current)
                    .unwrap_or(0) as isize;
                let next = (at + step.signum()).rem_euclid(options.len() as isize) as usize;
                if options[next] != current {
                    self.settings_apply_row(&row, options[next]);
                }
                return;
            }
        }
        match row.id.as_str() {
            "tools.defaults" => {
                let len = TOOLS.len() as isize;
                self.ui.tool_cursor =
                    (self.ui.tool_cursor as isize + step).rem_euclid(len) as usize;
            }
            "agents.default_effort" | "agents.default_root" | "tools.default_permission" => {
                let key = match row.id.as_str() {
                    "agents.default_effort" => "effort",
                    "agents.default_root" => "agent",
                    "tools.default_permission" => "permission",
                    _ => "model",
                };
                let mut choices = vec![String::new()];
                match key {
                    "model" => choices.extend(
                        self.state
                            .models
                            .iter()
                            .filter_map(|m| m["ref"].as_str().map(str::to_owned)),
                    ),
                    "agent" => choices.extend(
                        self.state
                            .agents
                            .iter()
                            .filter(|a| a["mode"].as_str() != Some("subagent"))
                            .filter_map(|a| a["name"].as_str().map(str::to_owned)),
                    ),
                    "permission" => {
                        choices.clear();
                        choices.extend(["allow", "ask"].iter().map(|s| s.to_string()));
                    }
                    _ => choices.extend(
                        ["low", "medium", "high", "xhigh"]
                            .iter()
                            .map(|s| s.to_string()),
                    ),
                }
                if let Some(config) = &self.state.config {
                    let current = config["values"]["defaults"][key].as_str().unwrap_or("");
                    let index = choices.iter().position(|s| s == current).unwrap_or(0) as isize;
                    let value =
                        choices[(index + step).rem_euclid(choices.len() as isize) as usize].clone();
                    if let Some(revision) = config["revision"].as_str() {
                        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                            revision: revision.to_owned(),
                            path: format!("defaults.{key}"),
                            value: serde_json::Value::String(value),
                        }));
                    }
                } else {
                    self.toast(Level::Warning, "Configuration is not loaded");
                }
            }
            "notifications.terminal" => {
                let modes = [Notify::Off, Notify::Waiting, Notify::All];
                let now = modes.iter().position(|m| *m == self.ui.notify).unwrap_or(1) as isize;
                self.ui.notify = modes[(now + step).rem_euclid(3) as usize];
            }
            "notifications.sound" => self.ui.sound = !self.ui.sound,
            "appearance.scrollbar" => {
                let _ = step;
                self.ui.scrollbar = !self.ui.scrollbar;
                if !self.ui.scrollbar {
                    self.ui.scroll_mark = None;
                }
            }
            "manager.enabled" | "manager.mode" | "manager.proactive" => {
                self.manager_setting_change(&row.id, step)
            }
            "manager.model" => self.manager_edit_model(),
            "manager.privacy" => {}
            "mcp.full_definitions" => {
                if let Some(config) = &self.state.config {
                    if let Some(revision) = config["revision"].as_str() {
                        let value = !config["values"]["mcp"]["full_definitions"]
                            .as_bool()
                            .unwrap_or(false);
                        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                            revision: revision.to_owned(),
                            path: "mcp.full_definitions".into(),
                            value: json!(value),
                        }));
                    }
                } else {
                    self.toast(Level::Warning, "Configuration is not loaded");
                }
            }
            "context.usage_refresh_minutes" => {
                if let Some(config) = &self.state.config {
                    if let Some(revision) = config["revision"].as_str() {
                        let current = config["values"]["usage"]["refresh_minutes"]
                            .as_i64()
                            .unwrap_or(10);
                        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                            revision: revision.to_owned(),
                            path: "usage.refresh_minutes".into(),
                            value: json!((current + step.signum() as i64).clamp(1, 1440)),
                        }));
                    }
                } else {
                    self.toast(Level::Warning, "Configuration is not loaded");
                }
            }
            "context.handoff_percent" => {
                if let Some(config) = &self.state.config {
                    if let Some(revision) = config["revision"].as_str() {
                        let current = config["values"]["context"]["handoff_percent"]
                            .as_i64()
                            .unwrap_or(80);
                        let value = (current + 5 * step.signum() as i64).clamp(0, 100);
                        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                            revision: revision.to_owned(),
                            path: "context.handoff_percent".into(),
                            value: json!(value),
                        }));
                    }
                } else {
                    self.toast(Level::Warning, "Configuration is not loaded");
                }
            }
            "mcp.max_listed_tools" => {
                if let Some(config) = &self.state.config {
                    if let Some(revision) = config["revision"].as_str() {
                        let current = config["values"]["mcp"]["max_listed_tools"]
                            .as_i64()
                            .unwrap_or(25);
                        let value = (current + step.signum() as i64).clamp(1, 1000);
                        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                            revision: revision.to_owned(),
                            path: "mcp.max_listed_tools".into(),
                            value: json!(value),
                        }));
                    }
                } else {
                    self.toast(Level::Warning, "Configuration is not loaded");
                }
            }
            "dictation.stt_download"
            | "dictation.stt_remove"
            | "dictation.tts_download"
            | "dictation.tts_remove" => {}
            "appearance.timeline_detail" => self.set_detail(self.ui.detail.step(step)),
            "appearance.sidebar" => {
                let _ = step;
                self.ui.left_open = !self.ui.left_open;
            }
            "keybindings.list" | "about.versions" | "about.daemon" => {}
            _ => self.settings_activate_row(row),
        }
    }

    /// Flip the highlighted tool in the Tools row's grid.
    fn toggle_default_tool(&mut self) {
        let name = TOOLS[self.ui.tool_cursor.min(TOOLS.len() - 1)];
        let Some(config) = &self.state.config else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        let Some(revision) = config["revision"].as_str().map(str::to_owned) else {
            return;
        };
        let mut disabled = config["values"]["tools"]["disabled"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        if disabled.iter().any(|v| v.as_str() == Some(name)) {
            disabled.retain(|v| v.as_str() != Some(name));
        } else {
            disabled.push(serde_json::Value::String(name.to_owned()));
        }
        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
            revision,
            path: "tools.disabled".into(),
            value: serde_json::Value::Array(disabled),
        }));
    }

    /// The Tools row's grid as wrapped lines: `●on` / `○off`, the arrow key's
    /// current tool in brackets, then the key hint.
    fn tools_grid_lines(&self) -> Vec<String> {
        let disabled: Vec<&str> = self
            .state
            .config
            .as_ref()
            .and_then(|c| c["values"]["tools"]["disabled"].as_array())
            .map(|list| list.iter().filter_map(|v| v.as_str()).collect())
            .unwrap_or_default();
        let cursor = self.ui.tool_cursor.min(TOOLS.len() - 1);
        let mut lines: Vec<String> = TOOLS
            .iter()
            .enumerate()
            .collect::<Vec<_>>()
            .chunks(4)
            .map(|chunk| {
                chunk
                    .iter()
                    .map(|(i, name)| {
                        let mark = if disabled.contains(name) {
                            "\u{25cb}"
                        } else {
                            "\u{25cf}"
                        };
                        if *i == cursor {
                            format!("[{mark}{name}]")
                        } else {
                            format!("{mark}{name}")
                        }
                    })
                    .collect::<Vec<_>>()
                    .join("  ")
            })
            .collect();
        lines.push(
            "\u{2190}/\u{2192} choose a tool \u{b7} Enter toggles it for new sessions".into(),
        );
        lines
    }

    /// The title-model list as one editable, comma-separated line.
    fn edit_title_models(&mut self) {
        let Some(config) = &self.state.config else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        let Some(revision) = config["revision"].as_str().map(str::to_owned) else {
            return;
        };
        let current = config["values"]["context"]["title_models"]
            .as_array()
            .map(|list| {
                list.iter()
                    .filter_map(|v| v.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .unwrap_or_default();
        let mut buf = Composer::new();
        buf.set_text(&current);
        let restore = self.restore_point();
        self.ui.edit = Some(TextEdit {
            label: "title models (provider/model, …)".into(),
            target: EditTarget::TitleModels { revision },
            buf,
            restore,
        });
    }

    fn settings_activate(&mut self, index: usize) {
        let Some(row) = self.settings_row_at(index) else {
            return;
        };
        self.settings_activate_row(row);
    }

    fn settings_activate_row(&mut self, row: crate::manifest::Row) {
        match row.id.as_str() {
            "providers.page" | "agents.page" => {
                self.ui.settings_section = row.id.trim_end_matches(".page").to_owned();
                self.ui.settings_page = None;
                self.ui.settings_page_row = 0;
                self.ui.settings_search = Composer::new();
                self.ui.settings_nav_focus = false;
                self.effects
                    .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
            }
            "tools.defaults" => self.toggle_default_tool(),
            "appearance.theme" => self.execute("app.theme"),
            "appearance.monochrome" => self.toggle_monochrome(),
            "appearance.animations" => {
                let mut options = self.theme.opts;
                options.reduced_motion = !options.reduced_motion;
                self.theme = self.theme.with(options);
            }
            "agents.default_effort"
            | "agents.default_root"
            | "tools.default_permission"
            | "zmx.claude_backend"
            | "zmx.antigravity"
            | "appearance.sidebar"
            | "notifications.sound"
            | "notifications.terminal"
            | "appearance.timeline_detail"
            | "appearance.scrollbar" => self.settings_change_row(row, 1),
            "mcp.full_definitions"
            | "mcp.max_listed_tools"
            | "context.handoff_percent"
            | "context.usage_refresh_minutes" => self.settings_change_row(row, 1),
            "context.title_models" => self.edit_title_models(),
            "manager.enabled" | "manager.mode" | "manager.proactive" => {
                self.settings_change_row(row, 1)
            }
            "manager.model" => self.manager_edit_model(),
            "tools.search_start" => self.run_command("search.start"),
            "dictation.stt_download" => self.run_command("voice.download_stt"),
            "dictation.stt_remove" => self.run_command("voice.remove_stt"),
            "dictation.tts_download" => self.run_command("voice.download_tts"),
            "dictation.tts_remove" => self.run_command("voice.remove_tts"),
            "appearance.details" => {
                self.ui.right_open = !self.ui.right_open;
                self.ui.right_chosen = true;
            }
            "appearance.session_scope" => self.run_command("sessions.scope_toggle"),
            "appearance.show_archived" => self.run_command("sessions.archived_filter"),
            "keybindings.list" => self.execute("app.help_keys"),
            "dictation.push_to_talk" => self.run_command("voice.dictate"),
            "about.inspector" => self.open_inspector(),
            _ => {}
        }
    }

    // ---- settings pages (Providers, Agents) ----------------------------------------------------

    /// What settings search matches inside a page: its rows, providers, agents and refs.
    fn page_search_terms(&self, section: &crate::manifest::Section) -> Vec<String> {
        let mut terms: Vec<String> = section.row.iter().map(|r| r.label.clone()).collect();
        match section.id.as_str() {
            "providers" => terms.extend(self.state.providers.iter().flat_map(|p| {
                [p["id"].as_str(), p["label"].as_str()]
                    .into_iter()
                    .flatten()
                    .map(str::to_owned)
            })),
            "agents" => {
                terms.extend(["models", "fallbacks", "subagents"].map(String::from));
                for agent in self.subagents() {
                    let name = agent["name"].as_str().unwrap_or_default().to_owned();
                    terms.extend(self.agent_refs(&name, false));
                    terms.extend(self.agent_refs(&name, true));
                    terms.push(name);
                }
            }
            _ => {}
        }
        terms
    }

    /// Agents that can run as a subagent, in catalog order.
    fn subagents(&self) -> Vec<&Value> {
        self.state
            .agents
            .iter()
            .filter(|a| matches!(a["mode"].as_str(), Some("subagent" | "all")))
            .collect()
    }

    /// `[agents.models]` (or `[agents.fallbacks]`) for one agent, from the config.
    fn agent_refs(&self, agent: &str, fallback: bool) -> Vec<String> {
        let key = if fallback { "fallbacks" } else { "models" };
        self.state
            .config
            .as_ref()
            .and_then(|c| c["values"]["agents"][key][agent].as_array())
            .map(|l| {
                l.iter()
                    .filter_map(|v| v.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// `[agents.efforts]` for one agent and model: the reasoning effort a subagent uses when it runs that model.
    fn agent_effort(&self, agent: &str, model: &str) -> Option<String> {
        self.state
            .config
            .as_ref()
            .and_then(|c| c["values"]["agents"]["efforts"][agent][model].as_str())
            .map(str::to_owned)
    }

    /// The reasoning efforts the catalog lists for a model; empty when it lists none.
    fn model_efforts(&self, model: &str) -> Vec<String> {
        self.state
            .models
            .iter()
            .find(|m| m["ref"].as_str() == Some(model))
            .and_then(|m| m["efforts"].as_array())
            .map(|l| {
                l.iter()
                    .filter_map(|v| v.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// What a pool or fallback row shows for effort: the chosen level, `default`, or nothing for a model without levels.
    fn effort_label(&self, agent: &str, model: &str) -> Option<String> {
        if self.model_efforts(model).is_empty() && self.agent_effort(agent, model).is_none() {
            return None;
        }
        Some(format!(
            "effort {}",
            self.agent_effort(agent, model)
                .unwrap_or_else(|| "default".into())
        ))
    }

    /// Step through effort levels; advancing past the last returns to `default`.
    fn step_agent_effort(&mut self, agent: &str, model: &str, by: isize) {
        let levels = self.model_efforts(model);
        if levels.is_empty() {
            self.toast(Level::Warning, format!("{model} lists no effort levels"));
            return;
        }
        let current = self.agent_effort(agent, model);
        let at = current
            .as_ref()
            .and_then(|c| levels.iter().position(|l| l == c))
            .map_or(0, |i| i + 1) as isize;
        let next = if by > 0 {
            (at + by).rem_euclid(levels.len() as isize + 1) as usize
        } else {
            (at + by).clamp(0, levels.len() as isize) as usize
        };
        if next as isize == at {
            return;
        }
        let chosen = (next > 0).then(|| levels[next - 1].clone());
        self.patch_agent_effort(agent, model, chosen);
    }

    fn ref_connected(&self, model: &str) -> bool {
        let provider = model.split_once('/').map_or("codex", |(p, _)| p);
        self.state
            .providers
            .iter()
            .any(|p| p["id"].as_str() == Some(provider) && p["connected"].as_bool() == Some(true))
    }

    fn provider_status(&self, p: &Value) -> String {
        let id = p["id"].as_str().unwrap_or_default();
        let connected = p["connected"].as_bool().unwrap_or(false);
        let key = p["methods"]
            .as_array()
            .is_some_and(|m| m.iter().any(|v| v.as_str() == Some("api_key")));
        let mut parts = vec![if !connected {
            "not connected".to_owned()
        } else if p["key_source"].as_str() == Some("env") {
            format!("using ${}", p["key_env"].as_str().unwrap_or("key"))
        } else {
            "connected".to_owned()
        }];
        if connected {
            let prefix = format!("{id}/");
            let n = self
                .state
                .models
                .iter()
                .filter(|m| m["ref"].as_str().is_some_and(|r| r.starts_with(&prefix)))
                .count();
            parts.push(format!("{n} model{}", if n == 1 { "" } else { "s" }));
        } else if key {
            parts.push("API key".into());
        }
        if p["verified"].as_bool() == Some(false) {
            parts.push("unverified".into());
        }
        parts.join(" \u{b7} ")
    }

    /// What a subagent with no models uses.
    fn agent_default(&self, agent: &Value) -> String {
        match agent["subagent"]["effective"]["model"]
            .as_str()
            .filter(|m| !m.is_empty())
        {
            Some(model) => format!("{model} (agent file)"),
            None => "parent's model".into(),
        }
    }

    /// One subagent's line on the Agents page: its models in brief, and a warning tone when some are skipped.
    fn agent_summary(&self, agent: &Value) -> (String, Tone) {
        let name = agent["name"].as_str().unwrap_or_default();
        let pool = self.agent_refs(name, false);
        let fallbacks = self.agent_refs(name, true).len();
        let skipped = self
            .agent_refs(name, false)
            .iter()
            .chain(self.agent_refs(name, true).iter())
            .filter(|r| !self.ref_connected(r))
            .count();
        let mut parts = vec![match pool.len() {
            0 => self.agent_default(agent),
            1 => pool[0].clone(),
            n => format!("{n} models"),
        }];
        if fallbacks > 0 {
            parts.push(format!(
                "{fallbacks} fallback{}",
                if fallbacks == 1 { "" } else { "s" }
            ));
        }
        if skipped > 0 {
            parts.push(format!("{skipped} skipped"));
        }
        let tone = if skipped > 0 {
            Tone::Warning
        } else if pool.is_empty() {
            Tone::Muted
        } else {
            Tone::Secondary
        };
        (parts.join(" \u{b7} "), tone)
    }

    /// The muted line above a page's key hints: the selected subagent's description, or what edits apply to.
    pub fn settings_page_detail(&self) -> Option<String> {
        match self.settings_page_action()? {
            PageAction::OpenAgent(name) => self
                .subagents()
                .into_iter()
                .find(|a| a["name"].as_str() == Some(name.as_str()))
                .and_then(|a| a["description"].as_str().map(str::to_owned)),
            PageAction::Ref { .. } | PageAction::Add { .. } => Some(
                "\u{2190}/\u{2192} sets this model's effort. Changes apply to new subagents; running ones keep what they started with.".into(),
            ),
            PageAction::Setting(row) => Some(row.note.clone()).filter(|n| !n.is_empty()),
            PageAction::OpenProvider(_) => {
                Some("Enter opens this provider's connection and sign-in details.".into())
            }
            PageAction::OpenUrl(_) => Some("Enter opens the link in your browser.".into()),
            PageAction::CopyCode(_) => Some("Enter copies the code to the clipboard.".into()),
            PageAction::Connect { method, .. } => Some(
                match method.as_str() {
                    "browser" => "Enter opens ChatGPT sign-in in your browser.",
                    "device" => "Enter signs in. A device code and link appear in this dialog.",
                    "api_key" => "Enter opens a masked field for the key.",
                    _ => "Enter starts this sign-in.",
                }
                .into(),
            ),
            PageAction::CancelLogin(_) => Some("Enter stops waiting for this sign-in.".into()),
            PageAction::Disconnect(_) => {
                Some("Enter signs out; listed models stay and are skipped.".into())
            }
            _ => None,
        }
    }

    /// The value on a page's entry in the section list.
    pub fn page_summary(&self, page: &str) -> String {
        match page {
            "providers" => {
                let shown: Vec<_> = self
                    .state
                    .providers
                    .iter()
                    .filter(|p| p["id"].as_str() != Some("mock"))
                    .collect();
                if shown.is_empty() {
                    return "loading\u{2026}".into();
                }
                let on = shown
                    .iter()
                    .filter(|p| p["connected"].as_bool() == Some(true))
                    .count();
                format!("{on} of {} connected", shown.len())
            }
            "agents" => {
                let subs = self.subagents();
                let own = subs
                    .iter()
                    .filter(|a| {
                        !self
                            .agent_refs(a["name"].as_str().unwrap_or_default(), false)
                            .is_empty()
                    })
                    .count();
                format!("{} subagents \u{b7} {own} with models", subs.len())
            }
            _ => String::new(),
        }
    }

    /// The open page's lines, top to bottom.
    pub fn settings_page_lines(&self) -> Vec<PageLine> {
        let mut out = Vec::new();
        let page = self.settings_open_page().unwrap_or_default();
        let page = page.as_str();
        let section = self
            .manifest
            .settings_for_tui()
            .into_iter()
            .find(|s| s.id == page);
        let rows = section.as_ref().map(|s| s.row.clone()).unwrap_or_default();
        let setting = |row: &crate::manifest::Row| PageLine::Item {
            label: row.label.clone(),
            value: String::new(),
            tone: Tone::Secondary,
            action: PageAction::Setting(Box::new(row.clone())),
        };
        if let Some(error) = &self.state.config_error {
            out.push(PageLine::Text(error.clone(), Tone::Error));
        }
        if let Some(config) = &self.state.config {
            for error in config["errors"].as_array().into_iter().flatten() {
                if let Some(error) = error.as_str() {
                    out.push(PageLine::Text(error.to_owned(), Tone::Error));
                }
            }
        }
        match page {
            "providers" => {
                out.push(PageLine::Item {
                    label: "Refresh models".into(),
                    value: if self.state.catalog_source.is_empty() {
                        String::new()
                    } else {
                        format!("catalog: {}", self.state.catalog_source)
                    },
                    tone: Tone::Muted,
                    action: PageAction::RefreshModels,
                });
                out.push(PageLine::Heading("Providers".into()));
                if self.state.providers.is_empty() {
                    out.push(PageLine::Text(
                        "Loading providers\u{2026}".into(),
                        Tone::Muted,
                    ));
                }
                for p in &self.state.providers {
                    let Some(id) = p["id"].as_str().filter(|id| *id != "mock") else {
                        continue;
                    };
                    let label = p["label"].as_str().unwrap_or(id).to_owned();
                    let login = self.ui.provider_logins.get(id);
                    let status = login.and_then(|l| l["status"].as_str()).unwrap_or("");
                    let connected = p["connected"].as_bool().unwrap_or(false);
                    out.push(PageLine::Item {
                        label,
                        value: if status == "pending" {
                            "sign-in pending \u{b7} open".to_owned()
                        } else if status == "failed" {
                            "sign-in failed \u{b7} open".to_owned()
                        } else {
                            format!("{} \u{b7} open", self.provider_status(p))
                        },
                        tone: if connected {
                            Tone::Secondary
                        } else {
                            Tone::Muted
                        },
                        action: PageAction::OpenProvider(id.into()),
                    });
                }
                for row in &rows {
                    out.push(setting(row));
                }
            }
            "agents" => {
                for row in &rows {
                    out.push(setting(row));
                }
                out.push(PageLine::Heading("Subagents".into()));
                for problem in &self.state.agent_problems {
                    let text = match (problem["path"].as_str(), problem["error"].as_str()) {
                        (Some(path), Some(error)) => format!("{path}: {error}"),
                        _ => problem.to_string(),
                    };
                    out.push(PageLine::Text(text, Tone::Error));
                }
                for agent in self.subagents() {
                    let name = agent["name"].as_str().unwrap_or_default().to_owned();
                    let (value, tone) = self.agent_summary(agent);
                    out.push(PageLine::Item {
                        label: name.clone(),
                        value,
                        tone,
                        action: PageAction::OpenAgent(name),
                    });
                }
            }
            page if page.starts_with("agents/") => {
                let name = &page["agents/".len()..];
                let Some(agent) = self
                    .subagents()
                    .into_iter()
                    .find(|a| a["name"].as_str() == Some(name))
                else {
                    out.push(PageLine::Text(
                        format!("No subagent named {name}."),
                        Tone::Error,
                    ));
                    return out;
                };
                out.push(PageLine::Text(
                    agent["description"].as_str().unwrap_or_default().to_owned(),
                    Tone::Muted,
                ));
                let pool = self.agent_refs(name, false);
                let eligible = pool.iter().filter(|r| self.ref_connected(r)).count();
                out.push(PageLine::Title(
                    "Models".into(),
                    "one picked at random".into(),
                ));
                if pool.is_empty() {
                    out.push(PageLine::Text(
                        format!("  none: {}", self.agent_default(agent)),
                        Tone::Muted,
                    ));
                }
                for model in &pool {
                    let on = self.ref_connected(model);
                    out.push(PageLine::Item {
                        label: model.clone(),
                        value: if on {
                            [
                                self.effort_label(name, model),
                                Some(format!("{:.0}%", 100.0 / eligible as f64)),
                            ]
                            .into_iter()
                            .flatten()
                            .collect::<Vec<_>>()
                            .join(" \u{b7} ")
                        } else {
                            "not connected \u{b7} skipped".into()
                        },
                        tone: if on { Tone::Secondary } else { Tone::Warning },
                        action: PageAction::Ref {
                            agent: name.to_owned(),
                            fallback: false,
                            model: model.clone(),
                        },
                    });
                }
                out.push(PageLine::Item {
                    label: "+ add model".into(),
                    value: String::new(),
                    tone: Tone::Muted,
                    action: PageAction::Add {
                        agent: name.to_owned(),
                        fallback: false,
                    },
                });
                out.push(PageLine::Title(
                    "Fallbacks".into(),
                    "tried in order on failure".into(),
                ));
                for (i, model) in self.agent_refs(name, true).iter().enumerate() {
                    let on = self.ref_connected(model);
                    out.push(PageLine::Item {
                        label: format!("{}. {model}", i + 1),
                        value: if on {
                            self.effort_label(name, model).unwrap_or_default()
                        } else {
                            "not connected \u{b7} skipped".into()
                        },
                        tone: if on { Tone::Secondary } else { Tone::Warning },
                        action: PageAction::Ref {
                            agent: name.to_owned(),
                            fallback: true,
                            model: model.clone(),
                        },
                    });
                }
                out.push(PageLine::Item {
                    label: "+ add fallback".into(),
                    value: String::new(),
                    tone: Tone::Muted,
                    action: PageAction::Add {
                        agent: name.to_owned(),
                        fallback: true,
                    },
                });
            }
            _ => {}
        }
        out
    }

    /// Indexes of the selectable lines of the open page.
    pub fn settings_page_items(&self) -> Vec<usize> {
        self.settings_page_lines()
            .iter()
            .enumerate()
            .filter(|(_, l)| matches!(l, PageLine::Item { .. }))
            .map(|(i, _)| i)
            .collect()
    }

    /// The open provider's detail modal: what it is, how it signs in, and the
    /// action on every row. The page outside stays a list of provider options.
    pub fn provider_detail_lines(&self, id: &str) -> Vec<PageLine> {
        let Some(p) = self
            .state
            .providers
            .iter()
            .find(|p| p["id"].as_str() == Some(id))
        else {
            return vec![PageLine::Text(
                format!("Provider {id} is not in the list."),
                Tone::Error,
            )];
        };
        let connected = p["connected"].as_bool().unwrap_or(false);
        let login = self.ui.provider_logins.get(id);
        let status = login.and_then(|l| l["status"].as_str()).unwrap_or("");
        let methods: Vec<&str> = p["methods"]
            .as_array()
            .map(|list| list.iter().filter_map(|v| v.as_str()).collect())
            .unwrap_or_default();
        let mut out = vec![PageLine::Text(self.provider_status(p), Tone::Secondary)];
        if let Some(note) = p["note"].as_str().filter(|n| !n.is_empty()) {
            let tone = if connected { Tone::Muted } else { Tone::Warning };
            out.push(PageLine::Text(note.to_owned(), tone));
        }
        if connected {
            if p["key_source"].as_str() == Some("env") {
                out.push(PageLine::Text(
                    format!("Using ${}", p["key_env"].as_str().unwrap_or("KEY")),
                    Tone::Muted,
                ));
            }
            out.push(PageLine::Item {
                label: "Disconnect".into(),
                value: "enter".into(),
                tone: Tone::Warning,
                action: PageAction::Disconnect(id.into()),
            });
            return out;
        }
        if status == "pending" {
            if let Some(url) = login.and_then(|l| l["verification_uri"].as_str()) {
                out.push(PageLine::Item {
                    label: url.to_owned(),
                    value: "enter opens".into(),
                    tone: Tone::Secondary,
                    action: PageAction::OpenUrl(url.to_owned()),
                });
            }
            if let Some(code) = login.and_then(|l| l["user_code"].as_str()) {
                out.push(PageLine::Item {
                    label: format!("code {code}"),
                    value: "enter copies".into(),
                    tone: Tone::Warning,
                    action: PageAction::CopyCode(code.into()),
                });
            }
            // A link or code already says what to do; otherwise show Core's
            // progress message (a browser flow publishes its URL as the link).
            let has_link = login.and_then(|l| l["verification_uri"].as_str()).is_some();
            let has_code = login.and_then(|l| l["user_code"].as_str()).is_some();
            if !has_link && !has_code {
                let message = login
                    .and_then(|l| l["message"].as_str())
                    .unwrap_or("Starting sign-in\u{2026}");
                out.push(PageLine::Text(message.to_owned(), Tone::Muted));
            }
            out.push(PageLine::Text(
                "Waiting for authorization\u{2026}".into(),
                Tone::Muted,
            ));
            out.push(PageLine::Item {
                label: "Cancel sign-in".into(),
                value: "enter".into(),
                tone: Tone::Muted,
                action: PageAction::CancelLogin(id.into()),
            });
            return out;
        }
        if status == "failed" {
            out.push(PageLine::Text(
                login
                    .and_then(|l| l["message"].as_str())
                    .unwrap_or("Sign-in failed")
                    .to_owned(),
                Tone::Error,
            ));
        }
        if methods.contains(&"api_key") {
            out.push(PageLine::Item {
                label: "Add API key".into(),
                value: "enter".into(),
                tone: Tone::Secondary,
                action: PageAction::Connect {
                    id: id.into(),
                    method: "api_key".into(),
                },
            });
            if let Some(hint) = p["key_hint"].as_str().filter(|h| !h.is_empty()) {
                out.push(PageLine::Text(hint.to_owned(), Tone::Muted));
            }
            if let Some(env) = p["key_env"].as_str().filter(|e| !e.is_empty()) {
                out.push(PageLine::Text(format!("Or set ${env}"), Tone::Muted));
            }
        }
        // One row per sign-in method, in the order Core lists them. Codex offers
        // a browser sign-in and a device code, so the user can pick either.
        for method in methods.iter().copied().filter(|m| *m != "api_key") {
            out.push(PageLine::Item {
                label: sign_in_label(method),
                value: "enter".into(),
                tone: Tone::Secondary,
                action: PageAction::Connect {
                    id: id.into(),
                    method: method.into(),
                },
            });
        }
        if methods.is_empty() {
            out.push(PageLine::Text(
                format!("Sign in from a terminal: nexus login {id}"),
                Tone::Muted,
            ));
        }
        out
    }

    pub fn provider_detail_items(&self, id: &str) -> Vec<usize> {
        self.provider_detail_lines(id)
            .iter()
            .enumerate()
            .filter(|(_, line)| matches!(line, PageLine::Item { .. }))
            .map(|(i, _)| i)
            .collect()
    }

    fn provider_detail_action(&self, id: &str) -> Option<PageAction> {
        let lines = self.provider_detail_lines(id);
        let items = self.provider_detail_items(id);
        let at = *items.get(
            self.ui
                .provider_detail_row
                .min(items.len().saturating_sub(1)),
        )?;
        match lines.into_iter().nth(at)? {
            PageLine::Item { action, .. } => Some(action),
            _ => None,
        }
    }

    fn provider_detail_key(&mut self, name: &str, _code: KeyCode) -> bool {
        let Some(id) = self.ui.provider_detail.clone() else {
            return false;
        };
        let last = self.provider_detail_items(&id).len().saturating_sub(1);
        match name {
            "esc" => self.ui.provider_detail = None,
            "up" => self.ui.provider_detail_row = self.ui.provider_detail_row.saturating_sub(1),
            "down" => self.ui.provider_detail_row = (self.ui.provider_detail_row + 1).min(last),
            "pageup" => self.ui.provider_detail_row = self.ui.provider_detail_row.saturating_sub(6),
            "pagedown" => self.ui.provider_detail_row = (self.ui.provider_detail_row + 6).min(last),
            "home" => self.ui.provider_detail_row = 0,
            "end" => self.ui.provider_detail_row = last,
            "enter" => {
                if let Some(action) = self.provider_detail_action(&id) {
                    self.page_activate(action);
                }
            }
            _ => {}
        }
        true
    }

    fn settings_page_action(&self) -> Option<PageAction> {
        let lines = self.settings_page_lines();
        let items = self.settings_page_items();
        let at = *items.get(self.ui.settings_page_row.min(items.len().saturating_sub(1)))?;
        match lines.into_iter().nth(at)? {
            PageLine::Item { action, .. } => Some(action),
            _ => None,
        }
    }

    /// Keys on a Settings page: arrows move, Enter acts, Left/Right change a
    /// setting, `d` removes a model, `K`/`J` reorder fallbacks, Esc goes back.
    fn settings_page_key(&mut self, name: &str, code: KeyCode) -> bool {
        let last = self.settings_page_items().len().saturating_sub(1);
        let sel = self.ui.settings_page_row.min(last);
        let action = self.settings_page_action();
        match name {
            "esc" | "backspace" => {
                self.ui.pool_pick = None;
                if self
                    .settings_open_page()
                    .as_deref()
                    .is_some_and(|p| p.starts_with("agents/"))
                {
                    self.ui.settings_page = Some("agents".into());
                    self.ui.settings_page_row = self.ui.settings_page_back;
                } else {
                    self.ui.settings_page = None;
                    self.ui.settings_nav_focus = true;
                    self.ui.settings_page_row = 0;
                }
            }
            "up" => self.ui.settings_page_row = sel.saturating_sub(1),
            "down" => self.ui.settings_page_row = (sel + 1).min(last),
            "tab" | "shift+tab" => self.ui.settings_nav_focus = true,
            "pageup" => self.ui.settings_page_row = sel.saturating_sub(8),
            "pagedown" => self.ui.settings_page_row = (sel + 8).min(last),
            "home" => self.ui.settings_page_row = 0,
            "end" => self.ui.settings_page_row = last,
            "enter" => {
                if let Some(action) = action {
                    self.page_activate(action);
                }
            }
            "left" | "right" => {
                let by = if name == "left" { -1 } else { 1 };
                match action {
                    Some(PageAction::Setting(row)) => {
                        if let Some(why) = self.settings_reason(&row, None) {
                            self.toast(Level::Warning, format!("{}: {why}", row.label));
                        } else {
                            self.settings_change_row(*row, by);
                        }
                    }
                    Some(PageAction::Ref { agent, model, .. }) => {
                        self.step_agent_effort(&agent, &model, by);
                    }
                    _ => {}
                }
            }
            "d" | "delete" => {
                if let Some(PageAction::Ref {
                    agent,
                    fallback,
                    model,
                }) = action
                {
                    let refs: Vec<String> = self
                        .agent_refs(&agent, fallback)
                        .into_iter()
                        .filter(|r| *r != model)
                        .collect();
                    self.patch_agent_refs(&agent, fallback, refs);
                }
            }
            _ if matches!(code, KeyCode::Char('K' | 'J'))
                || matches!(name, "shift+up" | "shift+down") =>
            {
                if let Some(PageAction::Ref {
                    agent,
                    fallback: true,
                    model,
                }) = action
                {
                    let up = code == KeyCode::Char('K') || name == "shift+up";
                    let mut refs = self.agent_refs(&agent, true);
                    if let Some(i) = refs.iter().position(|r| *r == model) {
                        let j = if up {
                            i.checked_sub(1)
                        } else {
                            Some(i + 1).filter(|j| *j < refs.len())
                        };
                        if let Some(j) = j {
                            refs.swap(i, j);
                            self.patch_agent_refs(&agent, true, refs);
                            self.ui.settings_page_row = if up {
                                sel.saturating_sub(1)
                            } else {
                                (sel + 1).min(last)
                            };
                        }
                    }
                }
            }
            _ => {}
        }
        true
    }

    fn page_activate(&mut self, action: PageAction) {
        match action {
            PageAction::RefreshModels => {
                self.effects
                    .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
                self.toast(Level::Info, "Refreshing the model catalog");
            }
            PageAction::OpenProvider(id) => {
                self.ui.provider_detail = Some(id);
                self.ui.provider_detail_row = 0;
            }
            PageAction::Connect { id, method } => {
                let provider = self
                    .state
                    .providers
                    .iter()
                    .find(|p| p["id"].as_str() == Some(id.as_str()));
                if method == "api_key" {
                    let label = provider
                        .and_then(|p| p["label"].as_str())
                        .unwrap_or(&id)
                        .to_owned();
                    let hint = provider
                        .and_then(|p| p["key_hint"].as_str())
                        .unwrap_or("")
                        .to_owned();
                    let restore = self.restore_point();
                    self.ui.edit = Some(TextEdit {
                        label: format!("{label} API key {hint}").trim_end().to_owned(),
                        target: EditTarget::ProviderKey { id },
                        buf: Composer::new(),
                        restore,
                    });
                } else {
                    self.ui
                        .provider_logins
                        .insert(id.clone(), json!({"status": "pending"}));
                    self.api(ApiCall::Provider {
                        id,
                        sign_out: false,
                        method,
                        key: None,
                    });
                }
            }
            PageAction::Disconnect(id) => {
                let users: Vec<String> = self
                    .subagents()
                    .iter()
                    .filter_map(|a| a["name"].as_str())
                    .filter(|name| {
                        [false, true].iter().any(|f| {
                            self.agent_refs(name, *f)
                                .iter()
                                .any(|r| r.split_once('/').map_or("codex", |(p, _)| p) == id)
                        })
                    })
                    .map(str::to_owned)
                    .collect();
                if users.is_empty() {
                    self.api(ApiCall::Provider {
                        id,
                        sign_out: true,
                        method: String::new(),
                        key: None,
                    });
                } else {
                    let restore = self.restore_point();
                    self.ui.dialog = Some(Dialog {
                        title: format!("Disconnect {id}?"),
                        body: vec![
                            format!("Models from {id} are listed for {}.", users.join(", ")),
                            "They stay in those lists and are skipped while it is not connected."
                                .into(),
                        ],
                        cancel: "Cancel".into(),
                        confirm: "Disconnect".into(),
                        danger: true,
                        action: Confirmed::Disconnect(id),
                        button: 0,
                        restore,
                    });
                }
            }
            PageAction::CancelLogin(id) => {
                self.ui.provider_logins.remove(&id);
                self.api(ApiCall::Provider {
                    id,
                    sign_out: true,
                    method: String::new(),
                    key: None,
                });
            }
            PageAction::CopyCode(code) => {
                self.effects.push(Effect::Copy(code));
                self.toast(Level::Success, "Code copied");
            }
            PageAction::OpenUrl(url) => {
                self.effects.push(Effect::OpenUrl(url));
            }
            PageAction::Setting(row) => {
                if let Some(why) = self.settings_reason(&row, None) {
                    self.toast(Level::Warning, format!("{}: {why}", row.label));
                } else {
                    self.settings_activate_row(*row);
                }
            }
            PageAction::Ref { .. } => {
                self.toast(Level::Info, "d removes it; K/J move a fallback");
            }
            PageAction::Add { agent, fallback } => {
                self.ui.pool_pick = Some((agent, fallback));
                self.open_palette(PaletteMode::PoolModels);
            }
            PageAction::OpenAgent(name) => {
                self.ui.settings_page_back = self.ui.settings_page_row;
                self.ui.settings_page = Some(format!("agents/{name}"));
                self.ui.settings_page_row = 0;
            }
        }
    }

    /// Write one agent's effort for one model; `None` clears it, and an agent with none left is removed.
    fn patch_agent_effort(&mut self, agent: &str, model: &str, effort: Option<String>) {
        let Some(config) = &self.state.config else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        let Some(revision) = config["revision"].as_str().map(str::to_owned) else {
            return;
        };
        let mut all = config["values"]["agents"]["efforts"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut mine = all
            .get(agent)
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        match effort {
            Some(level) => {
                mine.insert(model.to_owned(), json!(level));
            }
            None => {
                mine.remove(model);
            }
        }
        if mine.is_empty() {
            all.remove(agent);
        } else {
            all.insert(agent.to_owned(), Value::Object(mine));
        }
        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
            revision,
            path: "agents.efforts".into(),
            value: Value::Object(all),
        }));
    }

    /// Write one agent's model or fallback list; an empty list removes the entry.
    fn patch_agent_refs(&mut self, agent: &str, fallback: bool, refs: Vec<String>) {
        let key = if fallback { "fallbacks" } else { "models" };
        let Some(config) = &self.state.config else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        let Some(revision) = config["revision"].as_str().map(str::to_owned) else {
            return;
        };
        let mut all = config["values"]["agents"][key]
            .as_object()
            .cloned()
            .unwrap_or_default();
        if refs.is_empty() {
            all.remove(agent);
        } else {
            all.insert(agent.to_owned(), json!(refs));
        }
        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
            revision,
            path: format!("agents.{key}"),
            value: Value::Object(all),
        }));
    }

    fn toggle_monochrome(&mut self) {
        let mut options = self.theme.opts;
        match self.ui.monochrome_before.take() {
            Some(tier) => options.tier = tier,
            None => {
                self.ui.monochrome_before = Some(options.tier);
                options.tier = Tier::Mono;
            }
        }
        self.theme = self.theme.with(options);
    }

    // ---- preferences ----------------------------------------------------------------------------

    fn prefs_now(&self) -> crate::prefs::Prefs {
        use crate::prefs::{NotificationsPrefs, Prefs, ThemePrefs, UiPrefs};
        Prefs {
            ui: UiPrefs {
                left_width: Some(self.ui.left_width),
                left_rail: None,
                left_open: Some(self.ui.left_open),
                right_open: Some(self.ui.right_open),
                scrollbar: Some(self.ui.scrollbar),
                detail: Some(self.ui.detail.label().to_owned()),
            },
            theme: ThemePrefs {
                mode: Some(
                    match self.theme.opts.mode {
                        Mode::Dark => "dark",
                        Mode::Light => "light",
                    }
                    .into(),
                ),
                monochrome: Some(self.theme.opts.tier == Tier::Mono),
                animations: Some(!self.theme.opts.reduced_motion),
            },
            notifications: NotificationsPrefs {
                sound: Some(self.ui.sound),
            },
        }
    }

    /// Start from saved preferences, then keep `<home>/tui.json` up to date
    /// (`persist_prefs`). Flags and environment (`over`, and a reduced-motion
    /// or monochrome request already in the options) win over the file.
    pub fn apply_prefs(
        &mut self,
        home: &std::path::Path,
        prefs: crate::prefs::Prefs,
        over: crate::prefs::Overrides,
    ) {
        if let Some(w) = prefs.ui.left_width {
            self.ui.left_width = w;
        }
        if let Some(v) = prefs.ui.left_open {
            self.ui.left_open = v;
        }
        // Older preferences collapsed the sidebar to the rail, which now always shows.
        if prefs.ui.left_rail == Some(true) {
            self.ui.left_open = false;
        }
        if let Some(v) = prefs.ui.right_open {
            self.ui.right_chosen = true;
            self.ui.right_open = v;
        }
        if let Some(v) = prefs.ui.scrollbar {
            self.ui.scrollbar = v;
        }
        if let Some(d) = prefs
            .ui
            .detail
            .as_deref()
            .and_then(crate::ui::Detail::from_label)
        {
            self.ui.detail = d;
        }
        if let Some(sound) = prefs.notifications.sound {
            self.ui.sound = sound;
        }
        let mut options = self.theme.opts;
        if !over.mode {
            match prefs.theme.mode.as_deref() {
                Some("dark") => options.mode = Mode::Dark,
                Some("light") => options.mode = Mode::Light,
                _ => {}
            }
        }
        if !over.tier && options.tier != Tier::Mono && prefs.theme.monochrome == Some(true) {
            self.ui.monochrome_before = Some(options.tier);
            options.tier = Tier::Mono;
        }
        if prefs.theme.animations == Some(false) {
            options.reduced_motion = true;
        }
        self.theme = self.theme.with(options);
        self.ui.prefs = Some(PrefsSync {
            home: home.to_owned(),
            stored: prefs,
            seen: self.prefs_now(),
        });
    }

    /// Save the preferences when one changed since the last call. A sidebar
    /// edge drag is saved when the button is released.
    pub fn persist_prefs(&mut self) {
        if self.ui.left_drag || self.ui.prefs.is_none() {
            return;
        }
        let now = self.prefs_now();
        let Some(sync) = self.ui.prefs.as_mut() else {
            return;
        };
        if sync.seen == now {
            return;
        }
        crate::prefs::merge_changes(&mut sync.stored, &sync.seen, &now);
        sync.seen = now;
        let _ = crate::prefs::save(&sync.home, &sync.stored);
    }

    // ---- drawing on a composer image -------------------------------------------------------------

    fn annotation_mut(&mut self) -> Option<&mut crate::annotate::Annotation> {
        match self.ui.overlay.as_mut() {
            Some(Overlay {
                kind: OverlayKind::Annotate(annotation),
                ..
            }) => Some(annotation),
            _ => None,
        }
    }

    /// The pen: a press on the picture starts a stroke, and the stroke keeps
    /// the pointer until release, wherever it goes.
    fn annotate_mouse(&mut self, ev: &MouseEvent, pixel: (u32, u32)) -> bool {
        let Some(annotation) = self.annotation_mut() else {
            return false;
        };
        let canvas = annotation.canvas.get();
        let ink = annotation.ink;
        match ev.kind {
            MouseEventKind::Down(MouseButton::Left) if canvas.contains(pixel.0, pixel.1) => {
                let color = crate::theme::annotation_inks()[ink];
                annotation.begin(color, canvas.normalise(pixel.0, pixel.1));
                true
            }
            MouseEventKind::Drag(MouseButton::Left) if annotation.drawing => {
                annotation.extend(canvas.normalise(pixel.0, pixel.1));
                true
            }
            MouseEventKind::Up(MouseButton::Left) if annotation.drawing => {
                annotation.drawing = false;
                self.annotate_changed();
                true
            }
            _ => false,
        }
    }

    /// Terminals without live tiles redraw the whole picture.
    fn annotate_changed(&mut self) {
        if self.annotate_live.borrow().supported() {
            return;
        }
        if let Some(annotation) = self.annotation_mut() {
            annotation.refresh_flattened();
        }
    }

    fn annotate_ink(&mut self, ink: usize) {
        if ink < crate::theme::annotation_inks().len() {
            if let Some(annotation) = self.annotation_mut() {
                annotation.ink = ink;
            }
        }
    }

    fn annotate_undo(&mut self) {
        if let Some(annotation) = self.annotation_mut() {
            annotation.drawing = false;
            annotation.strokes.pop();
        }
        self.annotate_changed();
    }

    fn annotate_clear(&mut self) {
        if let Some(annotation) = self.annotation_mut() {
            annotation.drawing = false;
            annotation.strokes.clear();
        }
        self.annotate_changed();
    }

    /// The marks go into the draft image: the model and the timeline get the
    /// marked-up picture and nothing else. A failure keeps the modal open.
    fn annotate_done(&mut self) {
        let Some(annotation) = self.annotation_mut() else {
            return;
        };
        let (index, source) = (annotation.index, annotation.data_url.clone());
        match nexus_annotate::flatten(&source, &annotation.strokes) {
            Ok(data_url) => {
                // Marks can grow an image past the message limit that paste checked.
                let images = &mut self.composer().images;
                let others: usize = images
                    .iter()
                    .enumerate()
                    .filter(|(i, _)| *i != index)
                    .map(|(_, image)| image.data_url.len())
                    .sum();
                if others + data_url.len() > 8 * 1024 * 1024 * 4 / 3 + 256 {
                    self.toast(Level::Warning, "At most eight images and 8 MiB per message");
                    return;
                }
                if let Some(image) = images.get_mut(index) {
                    if image.data_url == source {
                        image.data_url = data_url;
                    }
                }
                self.close_overlay();
            }
            Err(error) => self.toast(Level::Error, error.to_string()),
        }
    }

    fn annotate_key(&mut self, name: &str) {
        match name {
            "1" | "2" | "3" | "4" => {
                self.annotate_ink(name.parse::<usize>().unwrap_or(1) - 1);
            }
            "u" => self.annotate_undo(),
            "x" => self.annotate_clear(),
            "enter" => self.annotate_done(),
            "esc" => self.close_overlay(),
            _ => {}
        }
    }

    // ---- mouse ----------------------------------------------------------------------------------

    pub fn handle_mouse(&mut self, mut ev: MouseEvent) {
        // In pixel mode the terminal reports pixels; everything but the pen
        // still thinks in cells.
        let pixel = {
            let live = self.annotate_live.borrow();
            let (cell_w, cell_h) = live.font();
            let (cell_w, cell_h) = (u32::from(cell_w), u32::from(cell_h));
            if live.pixel_mouse() {
                let pixel = (u32::from(ev.column), u32::from(ev.row));
                ev.column = (pixel.0 / cell_w).min(u32::from(u16::MAX)) as u16;
                ev.row = (pixel.1 / cell_h).min(u32::from(u16::MAX)) as u16;
                pixel
            } else {
                let middle = |cell: u16, size: u32| u32::from(cell) * size + size / 2;
                (middle(ev.column, cell_w), middle(ev.row, cell_h))
            }
        };
        self.mouse = Some((ev.column, ev.row));
        if self.annotate_mouse(&ev, pixel) {
            return;
        }
        if self.manager_mouse(&ev) {
            return;
        }
        if self.terminal_open
            && self.terminal_focused
            && self.terminal_controlled
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
            && self.ui.edit.is_none()
            && !ev.modifiers.contains(KeyModifiers::SHIFT)
        {
            if let Some(dock) = self.regions.terminal {
                let area = crate::terminal_panel::screen_area(
                    dock,
                    !self.terminal_controlled && self.terminal_panel.selected.is_some(),
                    self.terminal_panel.screen.gap,
                );
                if contains(area, ev.column, ev.row) {
                    let screen = self.terminal_panel.screen.screen();
                    if let Some(bytes) = crate::terminal_panel::mouse_bytes(
                        ev,
                        ev.column - area.x,
                        ev.row - area.y,
                        screen.mouse_protocol_mode(),
                        screen.mouse_protocol_encoding(),
                    ) {
                        if !bytes.is_empty() {
                            self.terminal_input(bytes);
                        }
                        return;
                    }
                }
            }
        }
        if self.file_edit_mouse(&ev) {
            return;
        }
        match ev.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                if self.terminal_open
                    && self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none()
                {
                    if let Some(dock) = self.regions.terminal {
                        let area = crate::terminal_panel::screen_area(
                            dock,
                            !self.terminal_controlled && self.terminal_panel.selected.is_some(),
                            self.terminal_panel.screen.gap,
                        );
                        if area.width > 0
                            && area.height > 0
                            && contains(area, ev.column, ev.row)
                            && !matches!(
                                self.hits.at(ev.column, ev.row),
                                Some(Target::TerminalTakeControl)
                            )
                            && self.terminal_selected
                        {
                            let mut screen = ratatui::buffer::Buffer::empty(area);
                            self.terminal_panel.screen.render(area, &mut screen);
                            let point = (ev.column, ev.row);
                            self.terminal_panel.selection = Some(crate::selection::Selection {
                                area,
                                screen,
                                start: point,
                                end: point,
                            });
                            self.terminal_panel.selecting = true;
                            self.terminal_focused = true;
                            self.terminal_panel.focus =
                                crate::terminal_panel::TerminalFocus::Screen;
                            return;
                        }
                    }
                }
                if self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none()
                    && self.blocking().is_none()
                    && self.ui.drawer.is_none()
                    && self.ui.view == View::Session
                    && !self.ui.file_open
                    && self.ui.diff_open.is_none()
                    && contains(self.regions.timeline, ev.column, ev.row)
                    && matches!(
                        self.hits.at(ev.column, ev.row),
                        Some(
                            Target::Timeline
                                | Target::Block(_)
                                | Target::BandToggle(_)
                                | Target::ContextSection(_)
                        )
                    )
                {
                    if let Some(screen) = self.timeline_screen.clone() {
                        let area = self.regions.timeline;
                        let point = (ev.column, ev.row);
                        let selection = crate::selection::Selection {
                            area,
                            screen,
                            start: point,
                            end: point,
                        };
                        self.timeline_drag =
                            Some((selection, self.hits.at(ev.column, ev.row).cloned()));
                        if matches!(
                            self.ui.focus,
                            Focus::Timeline
                                | Focus::Sidebar
                                | Focus::Right
                                | Focus::ToggleLeft
                                | Focus::ToggleRight
                        ) {
                            self.ui.focus = Focus::Composer;
                        }
                        return;
                    }
                }
                if self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none()
                    && self.ui.view == View::Session
                {
                    if let Some(Target::RailProject(i)) = self.hits.at(ev.column, ev.row).cloned() {
                        self.ui.rail_drag = Some(crate::ui::RailDrag {
                            from: i,
                            to: i,
                            start_row: ev.row,
                            active: false,
                            cancelled: false,
                        });
                        return;
                    }
                }
                let inner = Self::composer_text_inner(
                    crate::render::composer_without_pending(
                        self.regions.composer,
                        self.pending_inputs().len(),
                    ),
                    self.composer_ref().images.len(),
                    self.composer_ref().pastes().len(),
                );
                if self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.view == View::Session
                    && contains(inner, ev.column, ev.row)
                {
                    let (rows, (cursor_row, _)) = self.composer_ref().layout(inner.width as usize);
                    let top =
                        crate::render::visible_first(rows.len(), cursor_row, inner.height as usize);
                    let start = self.composer_ref().point_at(
                        inner.width,
                        top + usize::from(ev.row - inner.y),
                        ev.column - inner.x,
                    );
                    self.composer().select_range(start, start);
                    self.ui.composer_drag = Some((start, top));
                    self.ui.focus = Focus::Composer;
                    return;
                }
                if let Some(t) = self.hits.at(ev.column, ev.row).cloned() {
                    self.click(t);
                }
                // Typing always lands in the composer unless a modal is up; a
                // click leaves it there (a waiting card or alert keeps its keys).
                if self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.blocking().is_none()
                    && self.ui.view == View::Session
                    && matches!(
                        self.ui.focus,
                        Focus::Timeline
                            | Focus::Sidebar
                            | Focus::Right
                            | Focus::ToggleLeft
                            | Focus::ToggleRight
                    )
                {
                    self.ui.focus = Focus::Composer;
                }
            }
            MouseEventKind::Down(MouseButton::Right) => {
                if self.ui.overlay.is_some() || self.ui.dialog.is_some() {
                    return;
                }
                if let Some(Target::RailProject(i)) = self.hits.at(ev.column, ev.row).cloned() {
                    let project = self.rail_project_ids().get(i).cloned();
                    self.ui.focus = Focus::Sidebar;
                    self.ui.left_open = true;
                    self.ui.tab = Tab::Projects;
                    if let Some(index) =
                        project.and_then(|id| self.project_ids().iter().position(|p| *p == id))
                    {
                        self.ui.cursor.insert(Tab::Projects, index);
                    }
                    self.execute("sidebar.row_menu");
                } else if let Some(Target::SideRow(i)) = self.hits.at(ev.column, ev.row).cloned() {
                    self.ui.focus = Focus::Sidebar;
                    self.ui.cursor.insert(self.ui.tab, i);
                    if self.ui.tab == Tab::Sessions {
                        self.sidebar_activate(i);
                    }
                    self.execute("sidebar.row_menu");
                }
            }
            MouseEventKind::Drag(MouseButton::Left) if self.ui.rail_drag.is_some() => {
                let mut drag = self.ui.rail_drag.unwrap();
                if ev.row != drag.start_row {
                    drag.active = true;
                }
                if drag.active {
                    if let Some(rail) = self.regions.rail {
                        if !contains(rail, ev.column, ev.row) {
                            drag.cancelled = true;
                        } else {
                            drag.cancelled = false;
                            let bands: Vec<(usize, ratatui::layout::Rect)> =
                                crate::render::rail_layout(self, rail)
                                    .into_iter()
                                    .filter_map(|(item, band)| match item {
                                        crate::render::RailItem::Project(i) => Some((i, band)),
                                        _ => None,
                                    })
                                    .collect();
                            drag.to = bands
                                .iter()
                                .find(|(_, band)| band.y <= ev.row && ev.row < band.bottom())
                                .map(|(i, _)| *i)
                                .or_else(|| {
                                    let last = bands.last()?;
                                    (ev.row >= last.1.bottom()).then_some(last.0)
                                })
                                .unwrap_or(drag.to);
                        }
                    }
                }
                self.ui.rail_drag = Some(drag);
            }
            MouseEventKind::Drag(MouseButton::Left) if self.ui.left_drag => {
                let x0 = self.regions.left.map_or(0, |l| l.x);
                let want = ev.column.saturating_sub(x0) + 1;
                if want < crate::layout::RAIL_SNAP {
                    self.ui.left_open = false;
                    self.ui.left_drag = false;
                } else {
                    self.ui.left_width =
                        want.clamp(crate::layout::LEFT_MIN, crate::layout::LEFT_MAX);
                }
            }
            MouseEventKind::Drag(MouseButton::Left) => {
                if self.terminal_panel.selecting {
                    if let Some(selection) = &mut self.terminal_panel.selection {
                        selection.set_end(ev.column, ev.row);
                    }
                    return;
                }
                if let Some((selection, _)) = self.timeline_drag.as_mut() {
                    selection.set_end(ev.column, ev.row);
                    return;
                }
                if let Some((start, top)) = self.ui.composer_drag {
                    let inner = Self::composer_text_inner(
                        crate::render::composer_without_pending(
                            self.regions.composer,
                            self.pending_inputs().len(),
                        ),
                        self.composer_ref().images.len(),
                        self.composer_ref().pastes().len(),
                    );
                    let row = ev
                        .row
                        .saturating_sub(inner.y)
                        .min(inner.height.saturating_sub(1));
                    let column = ev
                        .column
                        .saturating_sub(inner.x)
                        .min(inner.width.saturating_sub(1));
                    let end =
                        self.composer_ref()
                            .point_at(inner.width, top + usize::from(row), column);
                    self.composer().select_range(start, end);
                }
            }
            MouseEventKind::Up(MouseButton::Left) => {
                if self.terminal_panel.selecting {
                    self.terminal_panel.selecting = false;
                    if let Some(selection) = &mut self.terminal_panel.selection {
                        selection.set_end(ev.column, ev.row);
                    }
                    return;
                }
                if let Some(drag) = self.ui.rail_drag.take() {
                    let ids = self.rail_project_ids();
                    if drag.active && !drag.cancelled && drag.to != drag.from {
                        if let (Some(source), Some(target)) = (ids.get(drag.from), ids.get(drag.to))
                        {
                            let (before, after) = if drag.to < drag.from {
                                (Some(target.clone()), None)
                            } else {
                                (None, Some(target.clone()))
                            };
                            self.api(ApiCall::MoveProject {
                                project: source.clone(),
                                before,
                                after,
                            });
                        }
                    } else if !drag.active {
                        self.rail_activate(drag.from);
                    }
                    return;
                }
                if let Some((mut selection, target)) = self.timeline_drag.take() {
                    selection.set_end(ev.column, ev.row);
                    if selection.dragged() {
                        let text = selection.text();
                        if !text.is_empty() {
                            self.effects.push(Effect::Copy(text));
                        }
                    } else if let Some(target) = target {
                        self.click(target);
                        if self.ui.overlay.is_none()
                            && self.ui.dialog.is_none()
                            && self.blocking().is_none()
                            && self.ui.view == View::Session
                            && matches!(
                                self.ui.focus,
                                Focus::Timeline
                                    | Focus::Sidebar
                                    | Focus::Right
                                    | Focus::ToggleLeft
                                    | Focus::ToggleRight
                            )
                        {
                            self.ui.focus = Focus::Composer;
                        }
                    }
                }
                self.ui.composer_drag = None;
                self.ui.left_drag = false;
            }
            MouseEventKind::ScrollUp => self.wheel(ev.column, ev.row, -3),
            MouseEventKind::ScrollDown => self.wheel(ev.column, ev.row, 3),
            _ => {}
        }
    }

    /// Clicks, drags and the wheel over the editor's text go to the editor.
    /// A drag that began there keeps selecting outside it until the release.
    fn file_edit_mouse(&mut self, ev: &MouseEvent) -> bool {
        if !self.ui.file_open
            || self.file_preview()
            || self.ui.diff_open.is_some()
            || self.ui.overlay.is_some()
            || self.ui.dialog.is_some()
            || self.blocking().is_some()
        {
            return false;
        }
        let Some(panel) = self.regions.panel else {
            return false;
        };
        let area = crate::render::file_editor_area(panel, self.ui.file_search.is_some());
        let Some(edit) = self.ui.file_edit.as_mut() else {
            return false;
        };
        let inside = contains(area, ev.column, ev.row);
        match ev.kind {
            MouseEventKind::Down(MouseButton::Left) if inside => {
                edit.dragging = true;
                edit.buf.mouse(*ev, area);
                true
            }
            MouseEventKind::Drag(MouseButton::Left) if edit.dragging => {
                let clamped = MouseEvent {
                    column: ev.column.clamp(area.x, area.right().saturating_sub(1)),
                    row: ev.row.clamp(area.y, area.bottom().saturating_sub(1)),
                    ..*ev
                };
                edit.buf.mouse(clamped, area);
                true
            }
            MouseEventKind::Up(MouseButton::Left) if edit.dragging => {
                edit.dragging = false;
                true
            }
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown
                if contains(panel, ev.column, ev.row) =>
            {
                edit.buf.mouse(*ev, area);
                true
            }
            _ => false,
        }
    }

    fn wheel(&mut self, x: u16, y: u16, delta: isize) {
        self.timeline_drag = None;
        if self.ui.overlay.is_some() {
            return self.overlay_move(delta.signum());
        }
        if self.ui.dialog.is_some() || self.blocking().is_some() {
            return;
        }
        let left = self.regions.left.or(self
            .regions
            .drawer
            .filter(|(s, _)| *s == Side::Left)
            .map(|(_, r)| r));
        if left.is_some_and(|r| contains(r, x, y)) && self.ui.view == View::Session {
            let n = self.list_len(self.ui.tab);
            let c = self.cursor(self.ui.tab) as isize;
            self.ui.cursor.insert(
                self.ui.tab,
                (c + delta.signum()).clamp(0, n.saturating_sub(1) as isize) as usize,
            );
        } else if self.regions.panel.is_some_and(|p| contains(p, x, y)) {
            let step = delta.signum();
            if self.ui.diff_open.is_some() {
                let width = self.view_rect().width.saturating_sub(2) as usize;
                let rows = crate::diff::Diff::from_patch(&self.diff_lines().join("\n"))
                    .render(width, &crate::diff::Look::default())
                    .len();
                let page = self.view_rect().height.saturating_sub(2) as usize;
                self.ui.diff_scroll = self
                    .ui
                    .diff_scroll
                    .saturating_add_signed(step)
                    .min(rows.saturating_sub(page));
            } else if self.ui.file_open {
                let max = self.file_scroll_limit();
                self.ui.file_scroll = self.ui.file_scroll.saturating_add_signed(step).min(max);
            }
        } else if contains(self.regions.timeline, x, y) && self.ui.view == View::Session {
            self.scroll_timeline(delta);
        }
    }

    pub fn click(&mut self, t: Target) {
        self.click_inner(t);
        self.completion_sync();
    }

    fn click_inner(&mut self, t: Target) {
        match t {
            Target::CompletionRow(i) => {
                self.ui.completion.index = i;
                self.completion_accept();
            }
            Target::Swallow => {}
            // The Manager's own targets are taken by `manager_mouse` before a click gets here.
            Target::ManagerInput
            | Target::ManagerVoice
            | Target::ManagerMute
            | Target::ManagerDebugSession
            | Target::ManagerDetails
            | Target::ManagerIndicator
            | Target::ManagerEnd
            | Target::ManagerScrim
            | Target::ManagerCard
            | Target::ManagerRow(_) => {}
            Target::OverviewRow(index) => {
                if let Some(Overlay {
                    kind: OverlayKind::Projects(o),
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    o.cursor = index;
                }
            }
            Target::Scrim => {
                if self.ui.provider_detail.is_some() {
                    self.ui.provider_detail = None;
                } else if self.annotation_mut().is_some() {
                    // A stray click must not throw the drawing away.
                } else if self.ui.manager.context.is_some() {
                    self.ui.manager.context = None;
                } else if self.ui.overlay.is_some() {
                    self.close_overlay();
                } else if self.ui.drawer.is_some() {
                    self.execute("nav.drawer.close");
                }
            }
            Target::ToggleLeft => {
                self.ui.focus = Focus::ToggleLeft;
                self.execute("sidebar.left.toggle");
            }
            Target::ToggleRight => {
                self.ui.focus = Focus::ToggleRight;
                self.execute("sidebar.right.toggle");
            }
            Target::Title => self.run_command("session.rename"),
            Target::SessionTab(i) => {
                let Some(id) = self.tab_ids().get(i).cloned() else {
                    return;
                };
                if self.viewed_root().as_deref() == Some(id.as_str())
                    && self.viewed().is_some_and(|v| v.parent_id.is_none())
                    && self.ui.view == View::Session
                {
                    self.run_command("session.rename");
                } else {
                    self.open_session(&id);
                }
            }
            Target::SessionRootBack => self.run_command("nav.root"),
            Target::SessionTabClose(i) => {
                if let Some(id) = self.tab_ids().get(i).cloned() {
                    self.close_tab(&id);
                }
            }
            Target::ArchiveSession(session) => {
                if self.writable() {
                    self.api(ApiCall::Archive {
                        session,
                        archived: true,
                    });
                } else {
                    self.toast(Level::Warning, READ_ONLY);
                }
            }
            Target::RemoveProject(id) => self.ask_remove_project_id(id),
            Target::Notifications => self.run_command("notifications.open"),
            Target::ManagerSession => self.run_command("manager.header"),
            Target::Tab(tab) => {
                let id = match tab {
                    Tab::Sessions => "sidebar.tab.sessions",
                    Tab::Files => "sidebar.tab.files",
                    Tab::Git => "sidebar.tab.git",
                    Tab::Projects => "sidebar.tab.projects",
                };
                self.run_command(id);
            }
            Target::SettingsEntry => self.run_command("app.settings"),
            Target::UsageRefresh => self.api(ApiCall::RefreshUsage),
            Target::ResourceJob(index) => {
                if let Some(Overlay {
                    kind: OverlayKind::Resources { selected, .. },
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    *selected = index;
                }
            }
            Target::ResourceStop(id) => self.api(ApiCall::StopJob { id }),
            Target::SideRow(i) => {
                self.ui.focus = Focus::Sidebar;
                self.ui.cursor.insert(self.ui.tab, i);
                self.sidebar_activate(i);
            }
            Target::RailProject(i) => self.rail_activate(i),
            Target::SidebarEdge => self.ui.left_drag = true,
            Target::RailSearch => self.run_command("session.switch"),
            Target::ArchivedFilter => self.run_command("sessions.archived_filter"),
            Target::SessionScope => self.run_command("sessions.scope_toggle"),
            Target::NewSession => self.run_command("session.new"),
            Target::AddProject => self.run_command("project.add"),
            Target::RightSection(_) => self.ui.focus = Focus::Right,
            Target::GitRow(i) => {
                self.ui.focus = Focus::Right;
                self.open_git_diff(i);
            }
            Target::Timeline => self.ui.focus = Focus::Timeline,
            Target::ContextPreview { title, text } => {
                self.open_context_section(&title, None, Some(text));
            }
            Target::InspectorRow(id) => {
                if let Some(i) = self.inspector_mut() {
                    i.pick(&id);
                }
                self.inspector_follow();
            }
            Target::InspectorSession(id) => {
                let act = self.inspector_mut().map(|i| i.set_target(id));
                if let Some(act) = act {
                    self.inspector_act(act);
                }
            }
            Target::ContextEntry(index) => self.context_entry_toggle(index),
            Target::ContextToggle(index) => self.toggle_context_item(index),
            Target::ContextInfo(index) => {
                let section = self.context_items().into_iter().nth(index).map(|item| {
                    match item.key.split_once(':') {
                        Some(("skill", _)) => "Skills",
                        Some(("mcp", _)) => "MCP",
                        _ => "Tools",
                    }
                });
                if let Some(section) = section {
                    self.open_context_section(section, Some(index), None);
                }
            }
            Target::ContextSection(section) => {
                if matches!(section.as_str(), "Tools" | "Skills" | "MCP") {
                    self.open_context_section(&section, None, None);
                } else if let Some(starting) = self
                    .state
                    .model
                    .selected
                    .as_ref()
                    .and_then(|view| view.starting.as_ref())
                {
                    let text = match section.as_str() {
                        "System prompt" => starting["system_prompt"]
                            .as_str()
                            .unwrap_or("No system prompt was recorded.")
                            .to_owned(),
                        "Workspace tools" => {
                            serde_json::to_string_pretty(&starting["workspace"]).unwrap_or_default()
                        }
                        _ => {
                            let file = &starting["instruction_file"];
                            let path = file["path"].as_str().unwrap_or("?");
                            let status = file["status"].as_str().unwrap_or("?");
                            let body = file["content"]
                                .as_str()
                                .or_else(|| file["text"].as_str())
                                .unwrap_or("No content recorded.");
                            format!("`{path}` · {status}\n\n{body}")
                        }
                    };
                    self.open_context_section(&section, None, Some(text));
                }
            }
            Target::Block(i) => {
                self.select_block(i);
                // A click expands a clipped reply but never folds an open one,
                // and a user band folds only from its left edge. Enter still
                // folds either.
                let kind = self.ui.timeline_blocks.get(i).map(|b| b.kind);
                let open_reply = kind == Some(crate::timeline::BlockKind::Assistant)
                    && self.ui.timeline_blocks.get(i).is_some_and(|b| {
                        self.ui
                            .timeline_open
                            .get(&b.key)
                            .copied()
                            .unwrap_or_else(|| self.block_default_open(b))
                    });
                if kind != Some(crate::timeline::BlockKind::Turn) && !open_reply {
                    self.activate_block();
                }
            }
            Target::BandToggle(i) => {
                self.select_block(i);
                self.activate_block();
            }
            Target::CopyBlock(i) => {
                self.select_block(i);
                self.copy_selected();
            }
            Target::StickyUser(i) => self.select_block(i),
            Target::ImagePreview { name, data_url } => {
                self.open_overlay(OverlayKind::ImagePreview { name, data_url });
            }
            Target::ComposerImage(index) => {
                if let Some(image) = self.composer_ref().images.get(index).cloned() {
                    self.open_overlay(OverlayKind::Annotate(Box::new(
                        crate::annotate::Annotation::new(index, image.name, image.data_url),
                    )));
                }
            }
            Target::AnnotateInk(ink) => self.annotate_ink(ink),
            Target::AnnotateUndo => self.annotate_undo(),
            Target::AnnotateClear => self.annotate_clear(),
            Target::AnnotateDone => self.annotate_done(),
            Target::TimelinePaste(paste) => {
                self.open_overlay(OverlayKind::ContextDetail {
                    title: "Pasted text".into(),
                    text: paste.text.clone(),
                    top: 0,
                });
            }
            Target::JumpLatest => self.run_command("session.jump_latest"),
            Target::Composer => self.ui.focus = Focus::Composer,
            Target::Command(id) => self.run_command(&id),
            Target::ComposerAgent => self.run_command("agent.change"),
            Target::ComposerModel => self.run_command("model.change"),
            Target::ComposerEffort => self.run_command("model.effort"),
            Target::ComposerContext => self.ui.focus = Focus::Composer,
            Target::ComposerDirectory => self.run_command("session.change_directory"),
            Target::ComposerWorktree => self.run_command("session.worktrees"),
            Target::ComposerWorktreeInfo => self.run_command("session.worktree_info"),
            Target::WorktreeCopy(which) => self.copy_worktree_value(which),
            Target::ComposerResources => {
                self.effects.push(Effect::Sync(SyncCommand::Resources));
                self.open_overlay(OverlayKind::Resources {
                    top: 0,
                    kind: crate::ui::ResourceKind::Kernels,
                    selected: 0,
                });
            }
            Target::ComposerShells => {
                self.effects.push(Effect::Sync(SyncCommand::Resources));
                self.open_overlay(OverlayKind::Resources {
                    top: 0,
                    kind: crate::ui::ResourceKind::Shells,
                    selected: 0,
                });
            }
            Target::ComposerTerminals => self.execute("terminal.open"),
            Target::TerminalNew => self.execute("terminal.new"),
            Target::TerminalRename => self.execute("terminal.rename"),
            Target::TerminalCloseSelected => self.execute("terminal.close"),
            Target::TerminalReleaseControl => self.execute("terminal.release"),
            Target::TerminalPanelClose => self.close_panel(),
            Target::TerminalCopy => self.execute("terminal.copy"),
            Target::TerminalAttach => self.execute("terminal.attach"),
            Target::TerminalTab(index) => {
                self.terminal_panel.cursor = index + 1;
                self.terminal_select(index);
            }
            Target::TerminalClose(index) => {
                if let Some(terminal) = self.terminal_panel.terminals.get(index) {
                    let previous = self.terminal_panel.selected.clone();
                    self.terminal_panel.selected = Some(terminal.id.clone());
                    self.execute("terminal.close");
                    self.terminal_panel.selected = previous;
                }
            }
            Target::TerminalTakeControl => self.execute("terminal.acquire"),
            Target::FilesRoot => self.run_command("files.root_toggle"),
            Target::PanelBack => self.close_panel(),
            Target::FilePreview => self.run_command("editor.preview"),
            Target::FileEdit => self.run_command("editor.edit"),
            Target::EditorFindInput => {
                if let Some(search) = self.ui.file_search.as_mut() {
                    search.replace_focus = false;
                }
            }
            Target::EditorReplaceInput => {
                if let Some(search) = self.ui.file_search.as_mut() {
                    search.replace_focus = true;
                }
            }
            Target::EditorFindClose => self.close_panel(),
            Target::EditorFindCase
            | Target::EditorFindPrevious
            | Target::EditorFindNext
            | Target::EditorReplaceOne
            | Target::EditorReplaceAll => {
                let name = match t {
                    Target::EditorFindCase => "alt+c",
                    Target::EditorFindPrevious => "ctrl+p",
                    Target::EditorFindNext => "ctrl+n",
                    Target::EditorReplaceOne => "ctrl+r",
                    _ => "ctrl+shift+r",
                };
                self.editor_find_key(&KeyEvent::new(KeyCode::Null, KeyModifiers::NONE), name);
            }
            Target::Wait(b) => {
                self.ui.focus = Focus::Waiting;
                self.run_command(match b {
                    WaitButton::Allow => "waiting.allow_once",
                    WaitButton::AllowSession => "waiting.allow_session",
                    WaitButton::Deny => "waiting.deny",
                    WaitButton::DenyNote => "waiting.deny_note",
                    WaitButton::Next => "waiting.next",
                    WaitButton::Submit => "waiting.submit_answers",
                });
            }
            Target::WaitOption(i) => {
                self.ui.focus = Focus::Waiting;
                if self.writable() {
                    self.ask_pick(i);
                    // A single single-choice question answers on the click.
                    if self
                        .ask_current()
                        .is_some_and(|c| c.total == 1 && !c.multiple() && !c.sent)
                    {
                        self.ask_submit();
                    }
                } else {
                    self.toast(Level::Warning, READ_ONLY);
                }
            }
            Target::WaitInput => {
                self.ui.focus = Focus::Waiting;
                if self.writable() {
                    self.ask_field(true);
                } else {
                    self.toast(Level::Warning, READ_ONLY);
                }
            }
            Target::PendingAction(index, action) => self.pending_action(index, action),
            Target::PendingNext => self.pending_next(),
            Target::Continue => self.run_command("session.continue"),
            Target::BannerAction(i) => {
                if let Some((_, _, cmd)) = self.banners().get(i).and_then(|b| b.action.clone()) {
                    self.run_command(&cmd);
                }
            }
            Target::BannerHide(i) => {
                if i < self.state.banners.len() {
                    self.state.banners.remove(i);
                }
            }
            Target::ToastDismiss(id) => self.ui.alerts.dismiss(id),
            Target::ToastOpen(id) => self.open_alert_detail(id, None),
            Target::PaletteItem(i) => self.palette_activate(Some(i)),
            Target::LearnSkill(i) => {
                if let Some(Overlay {
                    kind: OverlayKind::LearnChecklist(checklist),
                    ..
                }) = self.ui.overlay.as_mut()
                {
                    if let Some(selected) = checklist.selected.get_mut(i) {
                        checklist.cursor = i;
                        *selected = !*selected;
                    }
                }
            }
            Target::NoticeItem(i) => self.notice_activate(i),
            Target::DialogButton(i) => {
                if let Some(d) = self.ui.dialog.as_mut() {
                    d.button = i;
                }
                self.confirm_dialog(i == 1);
            }
            Target::SettingsRow(i) => {
                self.ui.settings_nav_focus = false;
                self.ui.settings_row = i;
                self.settings_activate(i);
            }
            Target::SettingsSegment { row, option } => {
                self.ui.settings_nav_focus = false;
                self.ui.settings_row = row;
                self.settings_choose(row, option);
            }
            Target::SettingsSection(i) => {
                self.select_settings_section(i);
                self.ui.settings_nav_focus = true;
            }
            Target::SettingsPageRow(i) => {
                self.ui.settings_nav_focus = false;
                self.ui.settings_page_row = i;
                if let Some(action) = self.settings_page_action() {
                    self.page_activate(action);
                }
            }
            Target::ProviderDetailRow(i) => {
                self.ui.provider_detail_row = i;
                if let Some(id) = self.ui.provider_detail.clone() {
                    if let Some(action) = self.provider_detail_action(&id) {
                        self.page_activate(action);
                    }
                }
            }
        }
    }

    pub fn apply_image_paste(
        &mut self,
        key: String,
        result: Result<Option<crate::images::Image>, String>,
    ) {
        self.ui.image_pasting.remove(&key);
        if self.question_pending() && key == self.draft_key() {
            self.toast(Level::Info, QUESTION_LOCK);
            return;
        }
        match result {
            Ok(Some(mut image)) => {
                let draft = self.ui.drafts.entry(key).or_default();
                let total: usize = draft.images.iter().map(|i| i.data_url.len()).sum();
                if draft.images.len() >= 8
                    || total + image.data_url.len() > 8 * 1024 * 1024 * 4 / 3 + 256
                {
                    self.toast(Level::Warning, "At most eight images and 8 MiB per message");
                } else {
                    let letter = (b'A'..=b'Z')
                        .map(char::from)
                        .find(|letter| {
                            !draft
                                .images
                                .iter()
                                .any(|i| i.name == format!("Image {letter}"))
                        })
                        .unwrap();
                    image.name = format!("Image {letter}");
                    let marker = format!("[{}]", image.name);
                    draft.insert_str(&marker);
                    draft.images.push(image);
                }
            }
            Ok(None) => self.toast(
                Level::Info,
                "No image on the clipboard; use your terminal's paste shortcut for text",
            ),
            Err(e) => self.toast(Level::Error, e),
        }
    }

    pub fn handle_paste(&mut self, text: &str) {
        if self.terminal_open
            && self.terminal_focused
            && self.ui.overlay.is_none()
            && self.ui.dialog.is_none()
        {
            let bytes = self.terminal_panel.screen.paste(text);
            self.terminal_input(bytes);
            return;
        }
        if let Some(Overlay {
            kind: OverlayKind::Palette(p),
            ..
        }) = self.ui.overlay.as_mut()
        {
            p.query.insert_str(&text.replace('\n', " "));
            return;
        }
        if let Some(e) = self.ui.edit.as_mut() {
            e.buf.insert_str(&text.replace('\n', " "));
            return;
        }
        if let (Some(edit), true) = (self.ui.file_edit.as_mut(), self.ui.file_open) {
            edit.buf.insert_str(text);
            return;
        }
        if self.ui.dialog.is_some() || self.ui.view == View::Settings {
            return;
        }
        if self.question_pending() {
            // The draft is locked; a paste is an answer to the card in view.
            if self.ask_current().is_some_and(|c| !c.sent && !c.review()) {
                self.ask_input_paste(text);
            }
            return;
        }
        if self.ui.focus == Focus::Waiting
            && self.ask_current().is_some_and(|c| !c.sent && !c.review())
        {
            self.ask_input_paste(text);
            return;
        }
        // Pasting into the composer from anywhere is the least surprising place.
        self.composer().paste(text);
        self.ui.focus = Focus::Composer;
        self.completion_sync();
    }
}

// ---- composer completion -----------------------------------------------------------

impl App {
    /// The token at the cursor when something may complete it: a `/command` as
    /// the draft's first word, or an `@path` anywhere. Nothing triggers while a
    /// question, dialog, palette or other text field is open.
    fn completion_token(&self) -> Option<String> {
        if self.ui.focus != Focus::Composer
            || self.ui.overlay.is_some()
            || self.ui.dialog.is_some()
            || self.ui.edit.is_some()
            || self.ui.view == View::Settings
            || self.blocking().is_some()
            || self.ask_current().is_some()
        {
            return None;
        }
        let composer = self.composer_ref();
        let before = composer.before_cursor();
        let token = crate::completion::token(before);
        let command = token.starts_with('/') && before == token && composer.text().starts_with('/');
        let file = token.starts_with('@') && !composer.text().starts_with('!');
        (command || file).then(|| token.to_owned())
    }

    fn completion_scope(&self) -> (Option<String>, Option<String>) {
        (self.current_project(), self.viewed_id().map(str::to_owned))
    }

    /// The rows of the visible popup, at most ten. `None` when it is hidden.
    pub fn completion_rows(&self) -> Option<Vec<CompletionRow>> {
        let token = self.completion_token()?;
        if self.ui.completion.hidden.as_deref() == Some(token.as_str()) {
            return None;
        }
        let rows = if token.starts_with('/') {
            self.command_completions(&token)
        } else {
            self.file_completions()?
        };
        (!rows.is_empty()).then_some(rows)
    }

    fn command_completions(&self, token: &str) -> Vec<CompletionRow> {
        let mut commands: Vec<&Command> = self
            .manifest
            .command
            .iter()
            .filter(|c| c.for_tui() && c.slash.is_some())
            .collect();
        commands.sort_by(|a, b| a.slash.cmp(&b.slash));
        let rows: Vec<(String, Vec<String>)> = commands
            .iter()
            .map(|c| {
                let name = c.slash.clone().unwrap_or_default();
                let names = std::iter::once(name.clone())
                    .chain(c.slash_aliases.iter().cloned())
                    .collect();
                (format!("/{name}"), names)
            })
            .collect();
        crate::completion::rank_menu(
            token.trim_start_matches('/'),
            &rows,
            crate::completion::MENU_LIMIT,
        )
        .into_iter()
        .filter_map(|value| {
            let c = commands
                .iter()
                .find(|c| c.slash.as_deref() == value.strip_prefix('/'))?;
            Some(CompletionRow {
                help: c.title.clone(),
                reason: self.availability(c).err(),
                value,
            })
        })
        .collect()
    }

    fn file_completions(&self) -> Option<Vec<CompletionRow>> {
        let state = &self.ui.completion;
        if state.scope != self.completion_scope() {
            return None;
        }
        let before = self.composer_ref().before_cursor();
        let mut paths = crate::completion::candidates(&state.cache, before)?;
        if let Some(pool) = &state.pool {
            let query = crate::completion::token(before).trim_start_matches('@');
            paths = crate::completion::file_menu(query, &paths, pool);
        }
        Some(
            paths
                .into_iter()
                .map(|value| CompletionRow {
                    value,
                    help: String::new(),
                    reason: None,
                })
                .collect(),
        )
    }

    /// After every edit or move: reset the highlight for a new token, drop a
    /// stale hide, and ask Core for the `@` token's candidates once.
    fn completion_sync(&mut self) {
        let token = self.completion_token();
        let state = &mut self.ui.completion;
        let current = token.clone().unwrap_or_default();
        if state.token != current {
            state.token = current;
            state.index = 0;
        }
        if state.hidden.is_some() && state.hidden != token {
            state.hidden = None;
        }
        let Some(token) = token.filter(|t| t.starts_with('@')) else {
            return;
        };
        let scope = self.completion_scope();
        let (Some(project), session) = scope.clone() else {
            return;
        };
        let state = &mut self.ui.completion;
        if state.scope != scope {
            *state = CompletionState {
                scope,
                ..CompletionState::default()
            };
        }
        let prefix = self.composer_ref().before_cursor().to_owned();
        let state = &mut self.ui.completion;
        let mut asks = Vec::new();
        if !state.pool_asked {
            state.pool_asked = true;
            asks.push((String::new(), COMPLETION_POOL));
        }
        if !state.asked.contains(&prefix) && !state.cache.iter().any(|(k, _)| *k == prefix) {
            state.asked.push(prefix);
            asks.push((token[1..].to_owned(), COMPLETION_HITS));
        }
        for (query, limit) in asks {
            self.effects.push(Effect::Sync(SyncCommand::Complete {
                project: project.clone(),
                session: session.clone(),
                query,
                limit,
            }));
        }
    }

    /// A Core reply: kept by prefix when it still answers the current token,
    /// otherwise dropped.
    fn completion_reply(
        &mut self,
        project: String,
        session: Option<String>,
        query: String,
        limit: u32,
        data: Result<Value, String>,
    ) {
        let paths: Option<Vec<String>> = data.ok().map(|v| {
            v["paths"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|p| p.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default()
        });
        let scope = (Some(project), session);
        if self.ui.completion.scope != scope || self.completion_scope() != scope {
            return;
        }
        if limit == COMPLETION_POOL {
            let state = &mut self.ui.completion;
            match paths {
                Some(paths) => state.pool = Some(paths),
                None => state.pool_asked = false,
            }
            return;
        }
        let prefix = self.composer_ref().before_cursor().to_owned();
        let token = crate::completion::token(&prefix);
        // A reply for a token that is no longer under the cursor is dropped.
        if token.strip_prefix('@') != Some(query.as_str()) {
            return;
        }
        let state = &mut self.ui.completion;
        state.asked.retain(|p| *p != prefix);
        if let Some(paths) = paths {
            state.cache.retain(|(k, _)| *k != prefix);
            state.cache.push((prefix, paths));
            let extra = state.cache.len().saturating_sub(COMPLETION_CACHE);
            state.cache.drain(..extra);
        }
    }

    /// Keys while the popup is visible. True when the key was taken.
    fn completion_key(&mut self, name: &str) -> bool {
        let Some(rows) = self.completion_rows() else {
            return false;
        };
        let last = rows.len() - 1;
        let index = self.ui.completion.index.min(last);
        match name {
            "up" => self.ui.completion.index = index.saturating_sub(1),
            "down" => self.ui.completion.index = (index + 1).min(last),
            "esc" => self.ui.completion.hidden = self.completion_token(),
            "tab" => self.completion_accept(),
            // The popup owns Shift+Tab too: it must not cycle the agent behind the list.
            "shift+tab" => {}
            "enter" => {
                let text = self.composer_ref().text();
                if text.starts_with('/') && !text.contains(char::is_whitespace) {
                    // A standalone `/command` runs the highlighted one, as if typed and sent.
                    self.composer().set_text(&rows[index].value);
                    self.send();
                } else {
                    self.completion_accept();
                }
            }
            _ => return false,
        }
        true
    }

    /// Replace the token with the highlighted value.
    fn completion_accept(&mut self) {
        let (Some(rows), Some(token)) = (self.completion_rows(), self.completion_token()) else {
            return;
        };
        let row = &rows[self.ui.completion.index.min(rows.len() - 1)];
        let text = crate::completion::accepted_completion(&token, &row.value);
        self.composer().replace_before_cursor(&token, &text);
        self.ui.focus = Focus::Composer;
    }
}

impl ActionDone {
    fn call_name(&self) -> &'static str {
        match self.call {
            ApiCall::FileWatch { .. } => "File watching disconnected",
            ApiCall::ProjectGit { .. }
            | ApiCall::ProjectGitDiff { .. }
            | ApiCall::ProjectGitLive { .. }
            | ApiCall::ProjectGitAction { .. } => "Git operation failed",
            ApiCall::FileMutation { .. } | ApiCall::FileCount { .. } => "File operation failed",
            ApiCall::TerminalList { .. }
            | ApiCall::TerminalCreate { .. }
            | ApiCall::TerminalRename { .. }
            | ApiCall::TerminalClose { .. }
            | ApiCall::TerminalControl { .. }
            | ApiCall::TerminalInput { .. }
            | ApiCall::TerminalResize { .. } => "Terminal failed",
            ApiCall::Input { .. } => "Pending input failed",
            ApiCall::Provider { .. } => "Provider sign-in failed",
            ApiCall::Prompt { .. } | ApiCall::NewAndPrompt { .. } => "Send failed",
            ApiCall::Continue { .. } => "Continue failed",
            ApiCall::Speak { .. } => "Speak failed",
            ApiCall::Handoff { .. } => "Handoff failed",
            ApiCall::Btw { .. } | ApiCall::BtwState { .. } => "Side question failed",
            ApiCall::ContextEntry { .. } => "Could not load the details",
            ApiCall::SpeakStop => "Stop failed",
            ApiCall::VoiceStatus { .. } => "Voice status failed",
            ApiCall::VoicePrepare { .. } => "Voice model loading failed",
            ApiCall::VoiceSettingsStatus { .. } => "Voice status failed",
            ApiCall::SearchServiceStatus => "Search status failed",
            ApiCall::SearchServiceStart => "Web search did not start",
            ApiCall::VoiceDownload { .. } => "Voice model download failed",
            ApiCall::VoiceModel { operation, .. } => match operation {
                VoiceOperation::Download => "Voice model download failed",
                VoiceOperation::Remove => "Voice model removal failed",
            },
            ApiCall::VoiceTranscribe { .. } => "Dictation failed",
            ApiCall::ManagerVoice { .. }
            | ApiCall::ManagerTranscribe { .. }
            | ApiCall::ManagerSpeak { .. }
            | ApiCall::ManagerLive { .. }
            | ApiCall::ManagerSpeakStop => "Manager voice failed",
            ApiCall::Interrupt { .. } => "Interrupt failed",
            ApiCall::Archive { .. } => "Archive failed",
            ApiCall::Rename { .. } => "Rename failed",
            ApiCall::Delete { .. } => "Delete failed",
            ApiCall::Reply { .. } => "Reply failed",
            ApiCall::Answer { .. } => "Answer failed",
            ApiCall::SetModel { .. } => "Model change failed",
            ApiCall::SetAgent { .. } => "Agent change failed",
            ApiCall::SetEffort { .. } => "Effort change failed",
            ApiCall::Export { .. } => "Export failed",
            ApiCall::LearnPreview { .. } | ApiCall::LearnInstall { .. } => "Learn failed",
            ApiCall::SetDisabledTools { .. } => "Session switch failed",
            ApiCall::ChangeDirectory { .. } => "Directory change failed",
            ApiCall::AddProject { .. } => "Add project failed",
            ApiCall::DeleteProject { .. } => "Remove project failed",
            ApiCall::MoveProject { .. } => "Move project failed",
            ApiCall::SetProjectStatus { .. } => "Project status change failed",
            ApiCall::Prompts { .. } | ApiCall::Search { .. } => "",
            ApiCall::OlderMessages { .. } => "Older messages failed to load",
            ApiCall::SwitchCost { .. } => "",
            ApiCall::Usage | ApiCall::RefreshUsage => "Provider usage failed to load",
            ApiCall::StopJob { .. } => "Could not stop the shell",
            ApiCall::Inspector { .. }
            | ApiCall::InspectorHead { .. }
            | ApiCall::InspectorRecord { .. } => "Inspector failed to load",
            ApiCall::SaveFile { .. } => "Save failed",
            ApiCall::Shell { .. } => "Shell command failed",
        }
    }
}

pub fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// `.md` and `.markdown` files open as rendered prose; everything else is source.
pub fn is_markdown_path(path: &str) -> bool {
    std::path::Path::new(path)
        .extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| {
            extension.eq_ignore_ascii_case("md") || extension.eq_ignore_ascii_case("markdown")
        })
}

pub(crate) fn format_bytes(bytes: u64) -> String {
    if bytes >= 1_000_000_000 {
        format!("{:.1} GB", bytes as f64 / 1_000_000_000.0)
    } else if bytes >= 1_000_000 {
        format!("{:.0} MB", bytes as f64 / 1_000_000.0)
    } else if bytes >= 1_000 {
        format!("{:.0} KB", bytes as f64 / 1_000.0)
    } else {
        format!("{bytes} bytes")
    }
}

fn voice_action_spec(id: &str) -> Option<(VoiceTarget, VoiceOperation)> {
    match id {
        "dictation.stt_download" | "voice.download_stt" => {
            Some((VoiceTarget::Stt, VoiceOperation::Download))
        }
        "dictation.stt_remove" | "voice.remove_stt" => {
            Some((VoiceTarget::Stt, VoiceOperation::Remove))
        }
        "dictation.tts_download" | "voice.download_tts" => {
            Some((VoiceTarget::Tts, VoiceOperation::Download))
        }
        "dictation.tts_remove" | "voice.remove_tts" => {
            Some((VoiceTarget::Tts, VoiceOperation::Remove))
        }
        _ => None,
    }
}

fn voice_defaults(target: VoiceTarget) -> (&'static str, u64) {
    match target {
        VoiceTarget::Stt => ("moondream/parakeet-redux", 179_005_408),
        VoiceTarget::Tts => ("gradium/phonon-31m-en", 65_000_000),
    }
}

/// The plain text of a message (user or assistant), for copying.
fn message_text(m: &Message) -> String {
    m.parts
        .iter()
        .filter(|p| p["type"] == "text")
        .map(|p| p["text"].as_str().unwrap_or(""))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Keys `edit_buffer` knows how to apply to a text field.
fn edit_key(name: &str) -> bool {
    matches!(
        name,
        "cmd+left"
            | "cmd+right"
            | "cmd+shift+left"
            | "cmd+shift+right"
            | "alt+left"
            | "ctrl+left"
            | "alt+shift+left"
            | "ctrl+shift+left"
            | "alt+b"
            | "alt+right"
            | "ctrl+right"
            | "alt+shift+right"
            | "ctrl+shift+right"
            | "alt+f"
            | "ctrl+home"
            | "ctrl+shift+home"
            | "ctrl+end"
            | "ctrl+shift+end"
            | "shift+left"
            | "shift+right"
            | "shift+up"
            | "shift+down"
            | "shift+home"
            | "shift+end"
            | "backspace"
            | "delete"
            | "left"
            | "right"
            | "home"
            | "ctrl+a"
            | "end"
            | "ctrl+e"
            | "ctrl+u"
            | "alt+backspace"
            | "up"
            | "down"
            | "ctrl+j"
    )
}

fn edit_composer(buf: &mut Composer, ev: &KeyEvent, name: &str) {
    let name = match name {
        "cmd+left" => "home",
        "cmd+right" => "end",
        "cmd+shift+left" => "shift+home",
        "cmd+shift+right" => "shift+end",
        _ => name,
    };
    edit_buffer(buf, ev, name, false);
}

/// Shared single-line/multi-line text editing keys.
pub(crate) fn edit_buffer(buf: &mut Composer, ev: &KeyEvent, name: &str, single_line: bool) {
    if let Some(c) = keys::printable(ev) {
        return buf.insert_char(c);
    }
    match name {
        "alt+left" | "ctrl+left" | "alt+shift+left" | "ctrl+shift+left" | "alt+b" => {
            buf.select_move(ev.modifiers.contains(KeyModifiers::SHIFT));
            buf.word(false);
        }
        "alt+right" | "ctrl+right" | "alt+shift+right" | "ctrl+shift+right" | "alt+f" => {
            buf.select_move(ev.modifiers.contains(KeyModifiers::SHIFT));
            buf.word(true);
        }
        "ctrl+home" | "ctrl+shift+home" | "ctrl+end" | "ctrl+shift+end" => {
            buf.select_move(ev.modifiers.contains(KeyModifiers::SHIFT));
            buf.document_edge(matches!(ev.code, KeyCode::End));
        }
        "shift+left" => {
            buf.select_move(true);
            buf.left();
        }
        "shift+right" => {
            buf.select_move(true);
            buf.right();
        }
        "shift+up" if !single_line => {
            buf.select_move(true);
            buf.up();
        }
        "shift+down" if !single_line => {
            buf.select_move(true);
            buf.down();
        }
        "shift+home" => {
            buf.select_move(true);
            buf.home();
        }
        "shift+end" => {
            buf.select_move(true);
            buf.end();
        }
        "backspace" => buf.backspace(),
        "delete" => buf.delete(),
        "left" => {
            buf.select_move(false);
            buf.left();
        }
        "right" => {
            buf.select_move(false);
            buf.right();
        }
        "home" | "ctrl+a" => {
            buf.select_move(false);
            buf.home();
        }
        "end" | "ctrl+e" => {
            buf.select_move(false);
            buf.end();
        }
        "ctrl+u" => buf.kill_to_line_start(),
        "alt+backspace" => buf.delete_word_back(),
        "up" if !single_line => {
            buf.up();
        }
        "down" if !single_line => {
            buf.down();
        }
        _ => {
            if let KeyCode::Char(_) = ev.code {
                // unbound modified key: ignore
            }
        }
    }
}

/// The tool switches that are off for a session (`disabled_tools`; Core may
/// send an array or a JSON string). A new session has none until Core drafts
/// carry them (PLAN.md Step 12a), so every tool reads as on.
pub fn disabled_tools(session: Option<&Session>) -> Vec<String> {
    let Some(value) = session.and_then(|s| s.extra.get("disabled_tools")) else {
        return Vec::new();
    };
    let parsed;
    let list = match value {
        serde_json::Value::String(text) => {
            parsed = serde_json::from_str::<serde_json::Value>(text).unwrap_or_default();
            &parsed
        }
        other => other,
    };
    list.as_array()
        .map(|v| {
            v.iter()
                .filter_map(|n| n.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}
