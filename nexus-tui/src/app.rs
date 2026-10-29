//! The shell's behaviour: focus, the Esc precedence chain, command
//! availability, palette, input handling and the effects that leave the UI
//! (API calls, supervisor commands, clipboard, quit, suspend).
//!
//! Everything user-visible about a command comes from the manifest; a command is
//! offered only when it is wired here (`WIRED`) and its `when` terms hold.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Instant;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;

use crate::actions::{ActionDone, ApiCall};
use crate::api::{Message, Session};
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
    Quit,
    Suspend,
    Sync(SyncCommand),
    Api(ApiCall),
    Copy(String),
    PasteImage(String),
    /// Suspend the TUI, open this path in `$VISUAL`/`$EDITOR`, and resume.
    OpenEditor(String),
    /// Open a URL in the platform browser (the inspector, documentation).
    OpenUrl(String),
    /// An OS notification through the terminal (OSC 9 or 777) for a session
    /// that needs the user while it is out of view.
    Notify(String),
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
    "sidebar.left.toggle",
    "sidebar.right.toggle",
    "sidebar.tab.sessions",
    "sidebar.tab.files",
    "sidebar.tab.projects",
    "copy.file_path",
    "file.open_external",
    "file.insert_mention",
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
    "project.switch",
    "project.open",
    "project.add",
    "project.remove",
    "sidebar.row_menu",
    "session.rename",
    "session.archive",
    "session.unarchive",
    "session.delete",
    "session.interrupt",
    "session.continue",
    "session.send",
    "session.export",
    "session.change_directory",
    "turn.go_to",
    "model.change",
    "model.effort",
    "agent.change",
    "composer.newline",
    "session.jump_latest",
    "session.copy_id",
    "session.open_inspector",
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
    "waiting.allow_once",
    "waiting.deny",
    "waiting.submit_answers",
    "waiting.next",
    "waiting.prev",
    "waiting.focus",
    "waiting.go_to",
];

/// Commands that change Core state: disabled while the connection is not live.
const MUTATING: &[&str] = &[
    "session.new",
    "session.rename",
    "session.archive",
    "session.unarchive",
    "session.delete",
    "session.interrupt",
    "session.continue",
    "session.send",
    "session.export",
    "session.change_directory",
    "turn.go_to",
    "model.change",
    "model.effort",
    "agent.change",
    "waiting.allow_once",
    "waiting.deny",
    "waiting.submit_answers",
];

const NOT_WIRED: &str = "Not available yet.";
const READ_ONLY: &str = "Read-only until Core is connected again.";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WaitKind {
    Approval,
    Question,
}

/// Answers collected so far for the focused `ask` request. It belongs to one
/// request id: another request (or none) starts from a blank draft.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AskDraft {
    pub request: String,
    /// Finished answers, in question order.
    pub answers: Vec<Value>,
    /// Highlighted option of the current question.
    pub cursor: usize,
    /// Ticked options of the current question (multi-select).
    pub chosen: Vec<usize>,
    /// The reply is in flight; a second submit would only get a 409.
    pub sent: bool,
}

/// The question the card shows now: the next unanswered one of the focused request.
#[derive(Debug, Clone, PartialEq)]
pub struct AskCurrent {
    pub wait: WaitItem,
    pub index: usize,
    pub total: usize,
    pub question: Value,
    pub draft: AskDraft,
}

impl AskCurrent {
    pub fn options(&self) -> &[Value] {
        self.question["options"]
            .as_array()
            .map_or(&[], Vec::as_slice)
    }

    pub fn multiple(&self) -> bool {
        self.question["multiple"].as_bool().unwrap_or(false)
    }

    pub fn allows_text(&self) -> bool {
        self.question["allow_free_text"]
            .as_bool()
            .unwrap_or(self.options().is_empty())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WaitItem {
    pub kind: WaitKind,
    pub id: String,
    pub session_id: String,
    /// The title of the child session it waits in, when not the viewed one.
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
    Row {
        row: Box<crate::manifest::Row>,
        /// The section's unavailable reason, which disables its rows.
        section_reason: Option<String>,
    },
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
        Value::Object(f)
    }
}

pub struct Ui {
    /// Sessions opened as header tabs, in the order they were opened.
    pub tabs: Vec<String>,
    pub detail: crate::ui::Detail,
    pub draft: Draft,
    pub completion: CompletionState,
    pub focus: Focus,
    pub left_open: bool,
    /// The docked left sidebar shows one letter per session instead of rows.
    pub left_rail: bool,
    /// Width of the full left sidebar (dragged by its edge).
    pub left_width: u16,
    /// A drag of the left sidebar's edge is in progress.
    pub left_drag: bool,
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
    pub file_scroll: usize,
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
    pub view: View,
    pub settings_row: usize,
    pub settings_search: Composer,
    pub overlay: Option<Overlay>,
    pub dialog: Option<Dialog>,
    pub edit: Option<TextEdit>,
    pub alerts: Alerts,
    pub scroll: Scroll,
    pub scrolls: HashMap<String, Scroll>,
    pub drafts: HashMap<String, Composer>,
    pub image_pasting: HashSet<String>,
    pub wait_idx: usize,
    /// The highlighted choice of the focused approval card (0 allow once, 2 deny).
    pub wait_choice: usize,
    /// When each waiting request was first seen, for `waiting on you · 0:42`.
    pub wait_seen: HashMap<String, Instant>,
    /// The card and clock text last announced by `tick`.
    pub wait_shown: Option<(String, u64)>,
    pub ask: AskDraft,
    pub project: Option<String>,
    /// Content rows and viewport rows of the timeline at the last draw.
    pub timeline_total: usize,
    pub timeline_height: usize,
    /// Explicit fold state, by block key. Absent means the block's default
    /// (turns open, reasoning and tool detail closed, context open only before
    /// the first turn).
    pub timeline_open: HashMap<String, bool>,
    /// Keys of the activity groups that were live at the last draw, and the
    /// frame at which a group finished (its glyph flashes for `FLASH_FRAMES`).
    pub live_groups: std::collections::HashSet<String>,
    pub group_flash: HashMap<String, usize>,
    /// The block the keyboard is on, as an index into `timeline_blocks`.
    pub timeline_sel: Option<usize>,
    /// The selected block's key, so a fold (which changes line ranges and may
    /// add or remove blocks) keeps the selection on the same item.
    pub timeline_sel_key: Option<String>,
    /// Blocks of the last drawn timeline (selection and clicks use them).
    pub timeline_blocks: Vec<crate::timeline::Block>,
    pub monochrome_before: Option<Tier>,
    /// Client preferences file state; `None` until `App::apply_prefs`.
    pub prefs: Option<PrefsSync>,
}

pub struct App {
    pub manifest: Arc<Manifest>,
    pub theme: Theme,
    pub state: AppState,
    pub ui: Ui,
    pub hits: HitMap,
    pub regions: Regions,
    pub effects: Vec<Effect>,
    pub clock: Instant,
    pub size: (u16, u16),
    pub frame: usize,
    pub mouse: Option<(u16, u16)>,
    bindings: HashMap<(String, String), String>,
    shortcut_leader: Option<Instant>,
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

impl App {
    pub fn new(state: AppState, theme: Theme, size: (u16, u16)) -> Self {
        let manifest = Arc::new(Manifest::load());
        let mut bindings = HashMap::new();
        for c in manifest.command.iter().filter(|c| c.for_tui()) {
            if let (Some(k), Some(ctx)) = (c.tui_key(), c.key_context.as_deref()) {
                if ctx != "escape_chain" {
                    bindings.insert((ctx.to_owned(), k.to_owned()), c.id.clone());
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
        App {
            manifest,
            theme,
            state,
            ui: Ui {
                tabs: Vec::new(),
                detail: crate::ui::Detail::default(),
                draft: Draft::default(),
                focus: Focus::Composer,
                left_open: true,
                left_rail: false,
                left_width: 0,
                left_drag: false,
                right_open: crate::layout::Breakpoint::of(size.0).right_open_by_default(),
                right_chosen: false,
                sized: false,
                drawer: None,
                tab,
                cursor: HashMap::new(),
                side_top: 0,
                file_open: false,
                file_scroll: 0,
                diff_open: None,
                diff_scroll: 0,
                git_cursor: 0,
                file_search: None,
                files_expanded: std::collections::BTreeSet::new(),
                files_hide_ignored: false,
                files_project_root: false,
                composer_drag: None,
                scope_all: false,
                show_archived: false,
                unread_sessions: HashSet::new(),
                failed_sessions: HashSet::new(),
                notify: Notify::default(),
                view: View::Session,
                settings_row: 0,
                settings_search: Composer::new(),
                overlay: None,
                dialog: None,
                edit: None,
                alerts: Alerts::default(),
                scroll: Scroll::default(),
                scrolls: HashMap::new(),
                completion: CompletionState::default(),
                drafts: HashMap::new(),
                image_pasting: HashSet::new(),
                wait_idx: 0,
                wait_choice: 0,
                wait_seen: HashMap::new(),
                wait_shown: None,
                ask: AskDraft::default(),
                project: None,
                timeline_total: 0,
                timeline_height: 0,
                timeline_open: HashMap::new(),
                live_groups: std::collections::HashSet::new(),
                group_flash: HashMap::new(),
                timeline_sel: None,
                timeline_sel_key: None,
                timeline_blocks: Vec::new(),
                monochrome_before: None,
                prefs: None,
            },
            hits: HitMap::default(),
            regions,
            effects: Vec::new(),
            clock: Instant::now(),
            size,
            frame: 0,
            mouse: None,
            bindings,
            shortcut_leader: None,
        }
    }

    // ---- model queries -------------------------------------------------------

    pub fn writable(&self) -> bool {
        self.state.connection.is_live()
    }

    pub fn narrow(&self) -> bool {
        Regions::is_narrow(self.size.0)
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

    /// The focused wait's current question, when it is a question.
    pub fn ask_current(&self) -> Option<AskCurrent> {
        let wait = self
            .wait_focused()
            .filter(|w| w.kind == WaitKind::Question)?;
        let q = self.state.model.questions.get(&wait.id)?;
        let draft = if self.ui.ask.request == wait.id {
            self.ui.ask.clone()
        } else {
            AskDraft::default()
        };
        let index = draft.answers.len();
        let question = q.questions.get(index)?.clone();
        Some(AskCurrent {
            index,
            total: q.questions.len(),
            question,
            draft,
            wait,
        })
    }

    /// The draft of request `id`, started blank when it belonged to another.
    fn ask_draft(&mut self, id: &str) -> &mut AskDraft {
        if self.ui.ask.request != id {
            self.ui.ask = AskDraft {
                request: id.to_owned(),
                ..AskDraft::default()
            };
        }
        &mut self.ui.ask
    }

    /// Tick (multi-select) or choose option `i` of the current question.
    pub fn ask_pick(&mut self, i: usize) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if cur.draft.sent || i >= cur.options().len() {
            return;
        }
        let multiple = cur.multiple();
        let d = self.ask_draft(&cur.wait.id);
        d.cursor = i;
        if multiple {
            match d.chosen.iter().position(|c| *c == i) {
                Some(at) => {
                    d.chosen.remove(at);
                }
                None => d.chosen.push(i),
            }
        } else {
            d.chosen = vec![i];
            self.ask_confirm(false);
        }
    }

    fn ask_move(&mut self, down: bool) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        let last = cur.options().len().saturating_sub(1);
        let d = self.ask_draft(&cur.wait.id);
        d.cursor = if down {
            (cur.draft.cursor + 1).min(last)
        } else {
            cur.draft.cursor.saturating_sub(1)
        };
    }

    /// Finish the current question from the ticked or highlighted option and,
    /// when the composer holds text and the question allows it, that text. The
    /// last question sends the whole reply. `typed` is a composer Enter: the text
    /// is the answer, so a question that takes no text says so instead.
    pub fn ask_confirm(&mut self, typed: bool) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        if !self.writable() {
            return self.toast(Level::Warning, READ_ONLY);
        }
        if cur.draft.sent {
            return;
        }
        let text = self.composer_ref().expanded_text().trim().to_owned();
        let opts = cur.options().to_vec();
        if typed && !text.is_empty() && !cur.allows_text() {
            return self.toast(
                Level::Info,
                "This question takes an option: press its number, or use up/down and enter",
            );
        }
        let use_text = !text.is_empty() && cur.allows_text();
        let mut chosen = cur.draft.chosen.clone();
        if !cur.multiple() && chosen.is_empty() && !opts.is_empty() && !use_text {
            chosen = vec![cur.draft.cursor.min(opts.len() - 1)];
        }
        if opts.is_empty() && !use_text {
            return self.toast(
                Level::Info,
                "Type the answer in the composer, then press enter",
            );
        }
        if cur.multiple() && chosen.is_empty() && !use_text {
            return self.toast(Level::Info, "Pick at least one option (number or space)");
        }
        chosen.sort_unstable();
        let selected: Vec<Value> = chosen
            .iter()
            .filter_map(|i| opts.get(*i).map(|o| o["id"].clone()))
            .collect();
        let answer = json!({
            "question_id": cur.question["id"],
            "selected": selected,
            "text": if use_text { Value::from(text) } else { Value::Null },
        });
        if use_text {
            self.composer().take();
        }
        let total = cur.total;
        let (session, id) = (cur.wait.session_id.clone(), cur.wait.id.clone());
        let d = self.ask_draft(&id);
        d.answers.push(answer);
        d.cursor = 0;
        d.chosen.clear();
        if d.answers.len() >= total {
            d.sent = true;
            let answers = d.answers.clone();
            self.api(ApiCall::Answer {
                session,
                id,
                answers,
            });
        }
    }

    fn ask_key(&mut self, ev: &KeyEvent, name: &str) {
        let Some(cur) = self.ask_current() else {
            return;
        };
        let n_opts = cur.options().len();
        match name {
            "up" => self.ask_move(false),
            "down" => self.ask_move(true),
            "enter" => self.ask_confirm(false),
            "space" if n_opts > 0 => self.ask_pick(cur.draft.cursor),
            _ => {
                let digit = name
                    .parse::<usize>()
                    .ok()
                    .filter(|_| n_opts > 0 && name.len() == 1);
                if let Some(d) = digit {
                    // 1..9 are options 1..9, 0 is the tenth
                    self.ask_pick((d + 9) % 10);
                } else if keys::printable(ev).is_some() && cur.allows_text() {
                    // typing starts the free-text answer in the composer
                    self.ui.focus = Focus::Composer;
                    self.composer_key(ev, name);
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

    pub fn side_entries(&self) -> Vec<SideEntry> {
        let m = &self.state.model;
        let cur = self.current_project();
        let mut sessions: Vec<&Session> = m
            .sessions_recent()
            .into_iter()
            .filter(|s| s.parent_id.is_none())
            .filter(|s| self.ui.show_archived || !s.archived)
            .filter(|s| self.ui.scope_all || cur.as_deref() == Some(s.project_id.as_str()))
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
                let new_project = last.as_ref().map(|g| &g.0) != Some(&group.0);
                let new_root = last.as_ref() != Some(&group);
                let linked = self
                    .session_worktree(s)
                    .is_some_and(|tree| tree["main"].as_bool() == Some(false));
                let project = new_project.then(|| {
                    m.projects
                        .get(&s.project_id)
                        .map_or(s.project_id.as_str(), |p| p.name.as_str())
                        .to_owned()
                });
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
                    project,
                    tree,
                }
            })
            .collect()
    }

    pub fn project_ids(&self) -> Vec<String> {
        let mut v: Vec<_> = self.state.model.projects.values().collect();
        v.sort_by(|a, b| {
            b.updated
                .total_cmp(&a.updated)
                .then_with(|| a.id.cmp(&b.id))
        });
        v.into_iter().map(|p| p.id.clone()).collect()
    }

    fn list_len(&self, tab: Tab) -> usize {
        match tab {
            Tab::Sessions => self.side_entries().len(),
            Tab::Projects => self.state.model.projects.len(),
            Tab::Files => self.file_rows().len(),
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
            "has_session" => has_session,
            "has_turns" => self.messages().iter().any(|m| m.role == "user"),
            "session.idle" => has_session && !busy,
            "session.busy" => has_session && busy,
            "session.interrupted" => has_session && !busy && self.last_turn_interrupted(),
            "session.archived" => self.viewed().is_some_and(|s| s.archived),
            "session.child" => self.viewed().is_some_and(|s| s.parent_id.is_some()),
            "session.in_worktree" => false,
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
            "has_queued" => false,
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
            "focus.file" => {
                self.ui.focus == Focus::Sidebar
                    && self
                        .selected_file()
                        .is_some_and(|row| row.kind != "directory" && row.kind != "error")
            }
            "focus.git_file" => {
                self.ui.focus == Focus::Right && self.git_rows().get(self.ui.git_cursor).is_some()
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
                        .is_some_and(|b| b.kind == want)
            }
            "view.editor" | "editor.dirty" => false,
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
                text: format!("{elsewhere} other session(s) waiting for input"),
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
        self.viewed_id().unwrap_or("").to_owned()
    }

    pub fn composer_ref(&self) -> &Composer {
        static EMPTY: std::sync::OnceLock<Composer> = std::sync::OnceLock::new();
        self.ui
            .drafts
            .get(&self.draft_key())
            .unwrap_or_else(|| EMPTY.get_or_init(Composer::new))
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

    // ---- focus & overlays --------------------------------------------------------

    fn restore_point(&self) -> Restore {
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
        let mut v = vec![Focus::ToggleLeft];
        if self.ui.left_open && !self.narrow() {
            v.push(Focus::Sidebar);
        }
        v.push(Focus::Timeline);
        if !self.wait_items().is_empty() && self.ui.view == View::Session {
            v.push(Focus::Waiting);
        }
        if self.ui.view == View::Session {
            v.push(Focus::Composer);
        }
        if self.ui.right_open && !self.narrow() {
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

    fn open_overlay(&mut self, kind: OverlayKind) {
        let restore = match self.ui.overlay.take() {
            Some(o) => o.restore,
            None => self.restore_point(),
        };
        self.ui.overlay = Some(Overlay { kind, restore });
    }

    fn open_palette(&mut self, mode: PaletteMode) {
        self.open_overlay(OverlayKind::Palette(Palette {
            mode,
            query: Composer::new(),
            selected: 0,
            top: 0,
            model_info: false,
        }));
    }

    fn close_overlay(&mut self) {
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
    fn files_session(&self) -> Option<String> {
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
    fn open_git_diff(&mut self, index: usize) {
        let Some(row) = self.git_rows().get(index).cloned() else {
            return;
        };
        let Some(session) = self.viewed_id().map(str::to_owned) else {
            return;
        };
        self.ui.git_cursor = index;
        self.ui.file_open = false;
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
        // The rail lists sessions only; another tab needs the full sidebar.
        if tab != Tab::Sessions {
            self.ui.left_rail = false;
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
                for (i, c) in self.manifest.command.iter().enumerate().filter(|(_, c)| {
                    c.for_tui()
                        && c.placement.iter().any(|p| p == "row_menu")
                        && c.id
                            .starts_with(if projects { "project." } else { "session." })
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
                            hint: p.path.clone(),
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
                for (i, m) in self.state.models.iter().enumerate() {
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
                    scored.push((
                        score,
                        i,
                        PaletteItem {
                            kind: PaletteKind::Model(r.to_owned()),
                            label,
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
            PaletteMode::Agents => {
                let current = self.current_agent();
                for (i, a) in self.state.agents.iter().enumerate() {
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
        if q.is_empty() {
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
                        target: EditTarget::ProviderKey,
                        buf: Composer::new(),
                        restore,
                    });
                } else {
                    self.api(ApiCall::Provider {
                        id,
                        sign_out,
                        key: None,
                    });
                }
            }
            PaletteKind::Session(id) => self.open_session(&id),
            PaletteKind::Project(id) => self.select_project(&id),
            PaletteKind::Model(model) => {
                if let Some(s) = self.viewed() {
                    let (session, revision) = (s.id.clone(), s.revision);
                    if self.writable() {
                        self.api(ApiCall::SetModel {
                            session,
                            model,
                            revision,
                        });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                } else {
                    let shown = if model.is_empty() {
                        "default".to_owned()
                    } else {
                        model.clone()
                    };
                    self.ui.draft.model = Some(model);
                    self.toast(
                        Level::Info,
                        format!("The new session will use model {shown}"),
                    );
                }
            }
            PaletteKind::Agent(agent) => {
                if let Some(s) = self.viewed() {
                    let (session, revision) = (s.id.clone(), s.revision);
                    if self.writable() {
                        self.api(ApiCall::SetAgent {
                            session,
                            agent,
                            revision,
                        });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                } else {
                    let shown = if agent.is_empty() {
                        "default".to_owned()
                    } else {
                        agent.clone()
                    };
                    self.ui.draft.agent = Some(agent);
                    self.toast(
                        Level::Info,
                        format!("The new session will use agent {shown}"),
                    );
                }
            }
            PaletteKind::Block(i) => self.select_block(i),
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
                    let shown = if effort.is_empty() {
                        "default".to_owned()
                    } else {
                        effort.clone()
                    };
                    self.ui.draft.effort = Some(effort);
                    self.toast(
                        Level::Info,
                        format!("The new session will use effort {shown}"),
                    );
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
                self.ui
                    .draft
                    .agent
                    .clone()
                    .unwrap_or_else(|| "build".into())
            },
            |s| s.agent.clone(),
        )
    }

    /// The effort of the viewed session, else the one chosen for the next;
    /// empty when it follows the agent's or provider's default.
    pub fn current_effort(&self) -> String {
        self.viewed().map_or_else(
            || self.ui.draft.effort.clone().unwrap_or_default(),
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
    /// file's, else Core's default. Empty only before the catalog loads.
    pub fn effective_model(&self, session: Option<&crate::api::Session>) -> String {
        if session.is_none() {
            if let Some(m) = self.ui.draft.model.as_deref().filter(|m| !m.is_empty()) {
                return m.to_owned();
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
            .map_or_else(|| self.state.default_model.clone(), str::to_owned)
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

    /// The header's tabs: the sessions opened so far that still exist, and the
    /// viewed one if it was reached some other way.
    pub fn tab_ids(&self) -> Vec<String> {
        let mut ids: Vec<String> = self
            .ui
            .tabs
            .iter()
            .filter(|id| self.state.model.sessions.contains_key(id.as_str()))
            .cloned()
            .collect();
        if let Some(id) = self.viewed_id() {
            if !ids.iter().any(|t| t == id) {
                ids.push(id.to_owned());
            }
        }
        ids
    }

    /// Close a tab (the session stays); the neighbouring tab, or the new
    /// session screen, takes over when it was the one in view.
    pub fn close_tab(&mut self, id: &str) {
        let ids = self.tab_ids();
        let Some(at) = ids.iter().position(|t| t == id) else {
            return;
        };
        self.ui.tabs = ids.iter().filter(|t| t.as_str() != id).cloned().collect();
        if self.viewed_id() != Some(id) {
            return;
        }
        let next = ids
            .get(at + 1)
            .or_else(|| at.checked_sub(1).and_then(|i| ids.get(i)));
        match next.cloned() {
            Some(next) => self.open_session(&next),
            None => {
                self.state.model.select(None);
                self.effects.push(Effect::Sync(SyncCommand::Select(None)));
                self.ui.view = View::Session;
                self.ui.timeline_sel = None;
            }
        }
    }

    fn step_tab(&mut self, step: isize) {
        let ids = self.tab_ids();
        let Some(at) = self
            .viewed_id()
            .and_then(|id| ids.iter().position(|t| t == id))
        else {
            return;
        };
        let to = (at as isize + step).rem_euclid(ids.len() as isize) as usize;
        if to != at {
            let id = ids[to].clone();
            self.open_session(&id);
        }
    }

    pub fn open_session(&mut self, id: &str) {
        if !self.ui.tabs.iter().any(|t| t == id) {
            self.ui.tabs.push(id.to_owned());
        }
        if let Some(root) = self.root_session(id) {
            self.ui.unread_sessions.remove(&root);
        }
        self.ui.file_open = false;
        self.ui.diff_open = None;
        if self.viewed_id() == Some(id) && self.ui.view == View::Session {
            self.after_open();
            return;
        }
        let from = self.draft_key();
        self.ui.scrolls.insert(from, self.ui.scroll);
        self.state.model.select(Some(id));
        self.ui.scroll = self.ui.scrolls.get(id).copied().unwrap_or_default();
        self.ui.timeline_sel = None;
        self.ui.timeline_sel_key = None;
        self.ui.wait_idx = 0;
        if let Some(s) = self.state.model.sessions.get(id) {
            self.ui.project = Some(s.project_id.clone());
            if !self.state.project_worktrees.contains_key(&s.project_id) {
                self.effects
                    .push(Effect::Sync(SyncCommand::ProjectWorktrees(
                        s.project_id.clone(),
                    )));
            }
        }
        self.ui.files_project_root = false;
        self.reset_files();
        self.effects
            .push(Effect::Sync(SyncCommand::Select(Some(id.to_owned()))));
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
        self.ui.focus = Focus::Composer;
    }

    fn select_project(&mut self, id: &str) {
        self.ui.project = Some(id.to_owned());
        self.ui.cursor.insert(Tab::Sessions, 0);
        self.ui.drawer = None;
        self.ui.files_project_root = true;
        self.reset_files();
        self.effects
            .push(Effect::Sync(SyncCommand::ProjectWorktrees(id.to_owned())));
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
            "app.quit" => self.effects.push(Effect::Quit),
            "app.settings" => {
                if self.ui.view == View::Settings {
                    self.ui.view = View::Session;
                    self.ui.focus = Focus::Composer;
                } else {
                    self.effects.push(Effect::Sync(SyncCommand::Config));
                    self.effects
                        .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
                    self.ui.view = View::Settings;
                    self.ui.settings_search = Composer::new();
                    self.ui.settings_row = 0;
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
            "nav.back" => {
                if let Some(p) = self.viewed().and_then(|s| s.parent_id.clone()) {
                    self.open_session(&p);
                }
            }
            "sidebar.left.toggle" => self.toggle_side(Side::Left),
            "sidebar.right.toggle" => self.toggle_side(Side::Right),
            "sidebar.tab.sessions" => self.set_tab(Tab::Sessions),
            "sidebar.tab.files" => self.set_tab(Tab::Files),
            "sidebar.tab.projects" => self.set_tab(Tab::Projects),
            "git.open_diff" => self.open_git_diff(self.ui.git_cursor),
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
            "turn.go_to" => self.open_palette(PaletteMode::Turns),
            "session.new" => {
                if let Some(project) = self.current_project() {
                    let fields = self.ui.draft.fields();
                    self.ui.draft = Draft::default();
                    self.api(ApiCall::NewSession { project, fields });
                }
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
            "sidebar.row_menu" => self.open_palette(PaletteMode::RowMenu),
            "session.switch" => self.open_palette(PaletteMode::Sessions),
            "project.switch" => self.open_palette(PaletteMode::Projects),
            "app.sign_in" => self.open_palette(PaletteMode::ProviderLogin),
            "app.sign_out" => self.open_palette(PaletteMode::ProviderLogout),
            "model.change" => self.open_picker(PaletteMode::Models),
            "model.effort" => self.open_picker(PaletteMode::Efforts),
            "agent.change" => self.open_picker(PaletteMode::Agents),
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
            "session.archive" | "session.unarchive" => {
                if let Some(sid) = self.viewed_id().map(str::to_owned) {
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
            "session.send" => self.send(),
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
            "session.open_inspector" | "tool.open_inspector" => self.open_inspector(),
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
            "waiting.allow_once" | "waiting.deny" => {
                if let Some(w) = self.wait_focused().filter(|w| w.kind == WaitKind::Approval) {
                    self.api(ApiCall::Reply {
                        session: w.session_id,
                        id: w.id,
                        allow: id == "waiting.allow_once",
                    });
                }
            }
            "waiting.submit_answers" => self.ask_confirm(false),
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
        let Some(id) = self.menu_project() else {
            return;
        };
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
        let mut body = vec![format!(
            "Remove \"{}\"{}?",
            p.name,
            if sessions > 0 {
                format!(" and its {sessions} session(s)")
            } else {
                String::new()
            }
        )];
        body.push("The directory and its files stay. This cannot be undone.".into());
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

    fn ask_delete(&mut self) {
        let Some(s) = self.viewed() else { return };
        let n = self.descendants(&s.id);
        let mut body = vec![format!(
            "Delete \"{}\"{}?",
            s.title,
            if n > 0 {
                format!(" and its {n} child session(s)")
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
                Confirmed::DeleteSession(id) => {
                    if self.writable() {
                        self.api(ApiCall::Delete { session: id });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                }
                Confirmed::RemoveProject(id) => {
                    if self.writable() {
                        self.api(ApiCall::DeleteProject { project: id });
                    } else {
                        self.toast(Level::Warning, READ_ONLY);
                    }
                }
            }
        }
    }

    fn send(&mut self) {
        if self.ui.image_pasting.contains(&self.draft_key()) {
            self.toast(Level::Info, "Preparing clipboard image…");
            return;
        }
        let text = self.composer_ref().expanded_text();
        if text.trim().is_empty() && self.composer_ref().images.is_empty() {
            return;
        }
        // `/name args` runs the command with that slash name.
        if !text.contains('\n') {
            if let Some(rest) = text.trim().strip_prefix('/') {
                let (word, arg) = rest.split_once(' ').unwrap_or((rest, ""));
                if let Some(c) = self.manifest.slash(word).cloned() {
                    self.composer().take();
                    let arg = arg.trim();
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
                    self.run_command(&c.id);
                    return;
                }
            }
        }
        if self.ask_current().is_some() {
            return self.ask_confirm(true);
        }
        if !self.writable() {
            self.toast(Level::Warning, READ_ONLY);
            return;
        }
        match self.viewed_id().map(str::to_owned) {
            Some(id) if self.is_busy(&id) => self.run_command("session.steer"),
            Some(id) => {
                self.composer().take();
                let images = std::mem::take(&mut self.composer().images);
                self.api(ApiCall::Prompt {
                    session: id,
                    text,
                    images,
                });
            }
            None => match self.current_project() {
                Some(project) => {
                    self.composer().take();
                    let fields = self.ui.draft.fields();
                    self.ui.draft = Draft::default();
                    let images = std::mem::take(&mut self.composer().images);
                    self.api(ApiCall::NewAndPrompt {
                        images,
                        project,
                        text,
                        fields,
                    });
                }
                None => self.toast(
                    Level::Warning,
                    "No project yet: add one from the Projects tab, then it appears here.",
                ),
            },
        }
    }

    // ---- sync & results ------------------------------------------------------------------

    pub fn apply_sync(&mut self, msg: SyncMsg) {
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
        if let SyncMsg::Config(Err(error)) = &msg {
            self.toast(Level::Warning, format!("Configuration: {error}"));
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
            if let Some(errors) = config["errors"].as_array().filter(|e| !e.is_empty()) {
                self.toast(
                    Level::Warning,
                    format!(
                        "Configuration: {}",
                        errors
                            .iter()
                            .filter_map(|e| e.as_str())
                            .collect::<Vec<_>>()
                            .join(", ")
                    ),
                );
            }
        }
        if let SyncMsg::Event(event) = &msg {
            if event.kind == "provider.login" {
                if let Some(message) = event.data["message"].as_str() {
                    self.toast(Level::Info, message.to_owned());
                } else if let Some(status) = event.data["status"].as_str() {
                    self.toast(Level::Info, format!("Provider sign-in: {status}"));
                }
            }
        }
        let attention = match &msg {
            SyncMsg::Event(event) => event
                .session_id
                .as_deref()
                .and_then(|id| self.root_session(id))
                .map(|root| (root, event.kind.clone(), event.data.clone())),
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
        let refresh = match &msg {
            SyncMsg::Event(event)
                if event.kind == "session.directory_changed"
                    && event.session_id.as_deref() == self.viewed_id() =>
            {
                event.session_id.clone()
            }
            _ => None,
        };
        self.state.apply(msg);
        // Model names and the default model come with the catalog; ask once
        // the workspace is known so the composer never says "default".
        if snapshot && self.state.models.is_empty() && self.state.default_model.is_empty() {
            self.effects
                .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
        }
        if self.state.events_applied > applied_before {
            if let Some((root, kind, data)) = attention {
                let viewed_root = self.viewed_id().and_then(|id| self.root_session(id));
                if kind == "session.error" {
                    self.ui.failed_sessions.insert(root.clone());
                }
                if kind == "session.status" && data["status"] == "busy" {
                    self.ui.failed_sessions.remove(&root);
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
    }

    pub fn apply_done(&mut self, done: ActionDone) {
        let name = done.call_name();
        match done.result {
            Ok(r) => {
                if let Some(c) = r.conflict {
                    self.toast(
                        Level::Warning,
                        format!("Changed elsewhere ({c}); nothing was overwritten"),
                    );
                } else if let Some(path) = &r.path {
                    self.toast(Level::Success, format!("Exported to {path}"));
                } else if !matches!(
                    done.call,
                    ApiCall::Prompt { .. } | ApiCall::NewAndPrompt { .. }
                ) {
                    self.toast(Level::Success, done.call.done_label());
                }
                if let Some(id) = r.open {
                    // events for the new session may not have arrived yet
                    self.open_session(&id);
                }
                if let Some(id) = r.project {
                    self.select_project(&id);
                }
            }
            Err(e) => {
                if matches!(done.call, ApiCall::Answer { .. }) {
                    // the question still waits: start the answers over
                    self.ui.ask = AskDraft::default();
                }
                if let Some((session, text)) = done.call.draft() {
                    let (session, text) = (session.map(str::to_owned), text.to_owned());
                    self.restore_draft(session.as_deref(), &text);
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
                    ApiCall::NewAndPrompt { images, .. } => self
                        .ui
                        .drafts
                        .entry(String::new())
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

    /// Advance clocks. Returns true when something visible changed, so an idle
    /// terminal is not redrawn (and not written to) on every tick.
    pub fn tick(&mut self, now: Instant) -> bool {
        self.clock = now;
        let mut changed = false;
        let animated = !self.theme.opts.reduced_motion
            && (!self.state.model.running.is_empty()
                || !self.ui.group_flash.is_empty()
                || matches!(
                    self.state.connection,
                    ConnectionState::Starting | ConnectionState::Connecting
                ));
        if animated {
            self.frame = self.frame.wrapping_add(1);
            changed = true;
        }
        changed |= self.note_waits(now);
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
    /// flashes the accent for `timeline::FLASH_FRAMES` (never with reduced motion).
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
            .retain(|_, at| frame.wrapping_sub(*at) < crate::timeline::FLASH_FRAMES);
    }

    pub fn set_size(&mut self, w: u16, h: u16) {
        self.size = (w, h);
        // The first real size picks the right sidebar's default (D17).
        if !self.ui.sized && !self.ui.right_chosen {
            self.ui.right_open = crate::layout::Breakpoint::of(w).right_open_by_default();
        }
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
    fn file_search_key(&mut self, ev: &KeyEvent, name: &str) -> bool {
        let typed = keys::printable(ev);
        let Some(search) = self.ui.file_search.as_mut() else {
            if typed == Some('/') {
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
        let page = crate::paint::file_page(self.regions.timeline.height);
        let max = self.file_line_count().saturating_sub(page);
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
        let Some(name) = keys::name(&ev) else { return };
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
            "ctrl+c"
                if self.ui.focus == Focus::Composer
                    && self.ui.overlay.is_none()
                    && self.ui.dialog.is_none()
                    && self.ui.edit.is_none()
                    && self.composer_ref().selection().is_some() =>
            {
                if let Some(text) = self.composer_ref().selected_text() {
                    self.effects.push(Effect::Copy(text));
                }
                return;
            }
            "ctrl+c" => return self.effects.push(Effect::Quit),
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
        if name == "esc" {
            if self.ui.diff_open.is_some()
                && self.ui.overlay.is_none()
                && self.ui.dialog.is_none()
                && self.ui.edit.is_none()
            {
                self.ui.diff_open = None;
                return;
            }
            if self.ui.file_open
                && self.ui.overlay.is_none()
                && self.ui.dialog.is_none()
                && self.ui.edit.is_none()
            {
                // The first Esc ends a search; the next one closes the file.
                if self.ui.file_search.take().is_none() {
                    self.ui.file_open = false;
                }
                return;
            }
            return self.escape();
        }
        if self.ui.diff_open.is_some() && self.ui.overlay.is_none() && self.ui.dialog.is_none() {
            // The rows the view draws (split or unified at its width).
            let width = self.regions.timeline.width.saturating_sub(2) as usize;
            let patch = self.diff_lines().join("\n");
            let lines = crate::diff::Diff::from_patch(&patch)
                .render(width, &crate::diff::Look::default())
                .len();
            let page = self.regions.timeline.height.saturating_sub(2) as usize;
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
        if self.ui.file_open && self.ui.overlay.is_none() && self.ui.dialog.is_none() {
            if self.file_search_key(&ev, &name) {
                return;
            }
            let lines = self.file_line_count();
            let page = crate::paint::file_page(self.regions.timeline.height);
            let max = lines.saturating_sub(page);
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
                    self.run_command("app.reconnect");
                    return;
                }
                "ctrl+t" => {
                    self.cycle_effort();
                    return;
                }
                _ => {}
            }
        }
        if self.ui.focus == Focus::Composer && matches!(name.as_str(), "alt+b" | "alt+f") {
            edit_buffer(self.composer(), &ev, &name, false);
            return;
        }
        if let Some(cmd) = self.lookup("global", &name) {
            return self.run_command(&cmd);
        }
        match name.as_str() {
            "f6" => return self.cycle_focus(false),
            "shift+f6" => return self.cycle_focus(true),
            "pageup" | "pagedown" if self.ui.view == View::Session => {
                let page = self.ui.timeline_height.saturating_sub(1).max(1) as isize;
                return self.scroll_timeline(if name == "pageup" { -page } else { page });
            }
            _ => {}
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
                if title.is_empty() {
                    return;
                }
                if !self.writable() {
                    self.toast(Level::Warning, READ_ONLY);
                    return;
                }
                match target {
                    EditTarget::RenameSession { id, revision } => self.api(ApiCall::Rename {
                        session: id,
                        title,
                        revision,
                    }),
                    EditTarget::ChangeDirectory { id } => self.api(ApiCall::ChangeDirectory {
                        session: id,
                        directory: title,
                    }),
                    EditTarget::ProviderKey => self.api(ApiCall::Provider {
                        id: "opencode".into(),
                        sign_out: false,
                        key: Some(crate::actions::Secret(title)),
                    }),
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
        let is_palette = matches!(
            self.ui.overlay,
            Some(Overlay {
                kind: OverlayKind::Palette(_),
                ..
            })
        );
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
                _ => {}
            },
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
                let _ = changed;
            }
            _ => return self.lookup("global", name).is_none(),
        }
        true
    }

    fn overlay_move(&mut self, delta: isize) {
        let n_notices = self.ui.alerts.history.len();
        let area = self.regions.area;
        match self.ui.overlay.as_mut().map(|o| &mut o.kind) {
            Some(OverlayKind::Palette(_)) => self.palette_move(delta),
            Some(OverlayKind::Notifications { selected }) => {
                *selected = (*selected as isize + delta)
                    .clamp(0, n_notices.saturating_sub(1) as isize)
                    as usize;
            }
            Some(OverlayKind::Help { top } | OverlayKind::AlertDetail { top, .. }) => {
                *top = (*top as isize + delta).max(0) as usize;
            }
            Some(OverlayKind::ContextDetail { text, top, .. }) => {
                let max = crate::render::context_detail_max(area, text);
                *top = (*top as isize + delta).clamp(0, max as isize) as usize;
            }
            None => {}
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

    fn composer_key(&mut self, ev: &KeyEvent, name: &str) {
        if let Some(cmd) = self
            .focus_contexts()
            .iter()
            .find_map(|ctx| self.lookup(ctx, name))
        {
            // Printable keys (`@`, `/`) are text unless their command is wired.
            let printable = keys::printable(ev).is_some();
            if !(printable && !WIRED.contains(&cmd.as_str())) {
                if cmd == "session.send" || cmd == "session.steer" {
                    return self.send();
                }
                return self.run_command(&cmd);
            }
        }
        match name {
            "ctrl+j" => self.composer().newline(),
            "up" | "down" => {
                let width = crate::render::composer_inner(
                    self.regions.composer,
                    !self.composer_ref().images.is_empty(),
                )
                .width
                .max(1) as usize;
                let single = self.composer_ref().layout(width).0.len() == 1;
                let down = name == "down";
                if (single || self.composer_ref().in_history()) && self.composer_ref().has_history()
                {
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
            _ => edit_buffer(self.composer(), ev, name, false),
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
            Notice | Diagnostic | Reasoning | Group | Tool | Child | Older | Code => false,
        }
    }

    fn select_block(&mut self, idx: usize) {
        let Some(b) = self.ui.timeline_blocks.get(idx) else {
            return;
        };
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
        let Some(info) = self.state.core.as_ref() else {
            self.toast(Level::Info, "Not connected to Core");
            return;
        };
        let sid = self.viewed_id().unwrap_or_default();
        let mut url = format!("{}/inspector", info.url.trim_end_matches('/'));
        if !sid.is_empty() {
            url.push_str(&format!("#{sid}"));
        }
        self.effects.push(Effect::OpenUrl(url));
    }

    fn timeline_key(&mut self, name: &str) {
        if let Some(cmd) = self.lookup("timeline", name) {
            if WIRED.contains(&cmd.as_str()) {
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
                } else {
                    self.select_block(0);
                }
            }
            "end" => {
                let n = self.ui.timeline_blocks.len();
                if n == 0 {
                    self.ui.scroll = Scroll::default();
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
        let cur = if self.ui.scroll.follow {
            max_top
        } else {
            self.ui.scroll.top.min(max_top)
        };
        let top = (cur as isize + delta).clamp(0, max_top as isize) as usize;
        self.ui.scroll = Scroll {
            follow: top >= max_top,
            top,
        };
    }

    fn sidebar_key(&mut self, name: &str) {
        let n = self.list_len(self.ui.tab);
        let c = self.cursor(self.ui.tab);
        let set = |s: &mut Self, v: usize| {
            s.ui.cursor.insert(s.ui.tab, v.min(n.saturating_sub(1)));
        };
        match name {
            "enter" if self.ui.tab == Tab::Files => self.sidebar_activate(c),
            "up" | "k" => set(self, c.saturating_sub(1)),
            "down" | "j" => set(self, c + 1),
            "home" => set(self, 0),
            "end" => set(self, n),
            "backspace" if self.ui.tab == Tab::Files => self.files_up(),
            "pageup" => set(self, c.saturating_sub(8)),
            "pagedown" => set(self, c + 8),
            "left" | "h" if self.ui.tab == Tab::Files => self.files_up(),
            "right" | "l" if self.ui.tab == Tab::Files => self.files_expand(),
            "left" | "h" => {
                let t = match self.ui.tab {
                    Tab::Sessions | Tab::Files => Tab::Sessions,
                    Tab::Projects => Tab::Files,
                };
                self.set_tab(t);
            }
            "right" | "l" => {
                let tab = match self.ui.tab {
                    Tab::Sessions => Tab::Files,
                    _ => Tab::Projects,
                };
                self.set_tab(tab);
            }
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
            .filter(|(session, p, _)| Some(session.as_str()) == self.viewed_id() && p == path)
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
        if let Some(cmd) = self.lookup("waiting", name) {
            return self.run_command(&cmd);
        }
        if self.ask_current().is_some() && !matches!(name, "left" | "right") {
            return self.ask_key(ev, name);
        }
        if self
            .wait_focused()
            .is_some_and(|w| w.kind == WaitKind::Approval)
        {
            match name {
                "up" | "down" => {
                    // only the offered choices (allow once, deny) take the cursor
                    self.ui.wait_choice = if self.ui.wait_choice == 0 { 2 } else { 0 };
                    return;
                }
                "enter" => {
                    let id = if self.ui.wait_choice == 2 {
                        "waiting.deny"
                    } else {
                        "waiting.allow_once"
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

    /// The settings list as drawn: section headings and rows, filtered by
    /// the search text (a section whose label matches keeps all its rows).
    pub fn settings_items(&self) -> Vec<SettingsItem> {
        let query = self.ui.settings_search.text().to_lowercase();
        let query = query.trim();
        let mut out = Vec::new();
        for section in self.manifest.settings_for_tui() {
            let section_hit = section.label.to_lowercase().contains(query);
            let rows: Vec<_> = section
                .row
                .iter()
                .filter(|row| {
                    query.is_empty()
                        || section_hit
                        || row.label.to_lowercase().contains(query)
                        || row.note.to_lowercase().contains(query)
                })
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

    /// Indexes of the selectable rows in `settings_items`.
    pub fn settings_rows(&self) -> Vec<usize> {
        self.settings_items()
            .iter()
            .enumerate()
            .filter(|(_, item)| matches!(item, SettingsItem::Row { .. }))
            .map(|(i, _)| i)
            .collect()
    }

    /// Keys while the settings modal is open. Typing filters; arrows move and
    /// change; Esc clears the search, then closes. Returns false for keys the
    /// shell should still handle (palette, quit).
    fn settings_key(&mut self, name: &str, code: KeyCode, mods: KeyModifiers) -> bool {
        let rows = self.settings_rows();
        let last = rows.len().saturating_sub(1);
        let sel = self.ui.settings_row.min(last);
        match name {
            "esc" => {
                if self.ui.settings_search.text().is_empty() {
                    self.execute("app.settings");
                } else {
                    self.ui.settings_search = Composer::new();
                    self.ui.settings_row = 0;
                }
            }
            "up" | "shift+tab" => self.ui.settings_row = sel.saturating_sub(1),
            "down" | "tab" => self.ui.settings_row = (sel + 1).min(last),
            "pageup" => self.ui.settings_row = sel.saturating_sub(8),
            "pagedown" => self.ui.settings_row = (sel + 8).min(last),
            "home" => self.ui.settings_row = 0,
            "end" => self.ui.settings_row = last,
            "left" => self.settings_change(sel, -1),
            "right" => self.settings_change(sel, 1),
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
            "providers.sign_in"
                | "providers.sign_out"
                | "models.default"
                | "providers.default_model"
                | "models.default_effort"
                | "agents.default_root"
                | "tools.default_permission"
                | "tools.default_read"
                | "tools.default_bash"
                | "tools.default_edit"
                | "tools.default_write"
                | "tools.default_ask"
                | "tools.default_code"
                | "appearance.theme"
                | "appearance.monochrome"
                | "appearance.animations"
                | "notifications.terminal"
                | "appearance.sidebar"
                | "appearance.details"
                | "appearance.timeline_detail"
                | "appearance.session_scope"
                | "appearance.show_archived"
                | "models.catalog"
                | "models.refresh"
                | "agents.list"
                | "about.versions"
                | "about.daemon"
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
        if let Some(why) = row
            .unavailable_reason
            .clone()
            .or(section_reason)
            .filter(|_| !matches!(row.id.as_str(), "providers.sign_in" | "providers.sign_out"))
        {
            self.toast(Level::Warning, format!("{}: {why}", row.label));
            return None;
        }
        if !Self::settings_wired(&row.id) {
            self.toast(Level::Warning, format!("{}: {NOT_WIRED}", row.label));
            return None;
        }
        Some(*row)
    }

    /// Left/right: step a choice or flip a toggle.
    fn settings_change(&mut self, index: usize, step: isize) {
        let Some(row) = self.settings_row_at(index) else {
            return;
        };
        match row.id.as_str() {
            id if id.starts_with("tools.default_") && id != "tools.default_permission" => {
                let name = id.trim_start_matches("tools.default_");
                if let Some(config) = &self.state.config {
                    let mut disabled = config["values"]["tools"]["disabled"]
                        .as_array()
                        .cloned()
                        .unwrap_or_default();
                    if disabled.iter().any(|v| v.as_str() == Some(name)) {
                        disabled.retain(|v| v.as_str() != Some(name));
                    } else {
                        disabled.push(serde_json::Value::String(name.to_owned()));
                    }
                    if let Some(revision) = config["revision"].as_str() {
                        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
                            revision: revision.to_owned(),
                            path: "tools.disabled".into(),
                            value: serde_json::Value::Array(disabled),
                        }));
                    }
                }
            }
            "models.default"
            | "providers.default_model"
            | "models.default_effort"
            | "agents.default_root"
            | "tools.default_permission" => {
                let key = match row.id.as_str() {
                    "models.default_effort" => "effort",
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
            "appearance.timeline_detail" => self.set_detail(self.ui.detail.step(step)),
            "appearance.sidebar" => {
                let modes = [(true, false), (true, true), (false, false)];
                let now = modes
                    .iter()
                    .position(|m| *m == (self.ui.left_open, self.ui.left_rail))
                    .unwrap_or(0) as isize;
                let (open, rail) = modes[(now + step).rem_euclid(3) as usize];
                self.ui.left_open = open;
                self.ui.left_rail = rail;
            }
            "keybindings.list" | "about.versions" | "about.daemon" | "models.catalog"
            | "models.refresh" | "agents.list" => {}
            _ => self.settings_activate(index),
        }
    }

    fn settings_activate(&mut self, index: usize) {
        let Some(row) = self.settings_row_at(index) else {
            return;
        };
        match row.id.as_str() {
            id if id.starts_with("tools.default_") && id != "tools.default_permission" => {
                self.settings_change(index, 1)
            }
            "appearance.theme" => self.execute("app.theme"),
            "appearance.monochrome" => self.toggle_monochrome(),
            "appearance.animations" => {
                let mut options = self.theme.opts;
                options.reduced_motion = !options.reduced_motion;
                self.theme = self.theme.with(options);
            }
            "models.default"
            | "providers.default_model"
            | "models.default_effort"
            | "agents.default_root"
            | "tools.default_permission"
            | "appearance.sidebar"
            | "notifications.terminal"
            | "appearance.timeline_detail" => self.settings_change(index, 1),
            "appearance.details" => {
                self.ui.right_open = !self.ui.right_open;
                self.ui.right_chosen = true;
            }
            "appearance.session_scope" => self.run_command("sessions.scope_toggle"),
            "appearance.show_archived" => self.run_command("sessions.archived_filter"),
            "keybindings.list" => self.execute("app.help_keys"),
            "models.catalog" => {
                self.execute("app.settings");
                self.execute("model.change");
            }
            "models.refresh" => {
                self.effects
                    .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
                self.toast(Level::Info, "Refreshing the model catalog");
            }
            "agents.list" => {
                self.execute("app.settings");
                self.execute("agent.change");
            }
            _ => {}
        }
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
        use crate::prefs::{Prefs, ThemePrefs, UiPrefs};
        Prefs {
            ui: UiPrefs {
                left_width: Some(self.ui.left_width),
                left_rail: Some(self.ui.left_rail),
                left_open: Some(self.ui.left_open),
                right_open: Some(self.ui.right_open),
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
        if let Some(v) = prefs.ui.left_rail {
            self.ui.left_rail = v;
        }
        if let Some(v) = prefs.ui.left_open {
            self.ui.left_open = v;
        }
        if let Some(v) = prefs.ui.right_open {
            self.ui.right_chosen = true;
            self.ui.right_open = v;
        }
        if let Some(d) = prefs
            .ui
            .detail
            .as_deref()
            .and_then(crate::ui::Detail::from_label)
        {
            self.ui.detail = d;
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

    // ---- mouse ----------------------------------------------------------------------------------

    pub fn handle_mouse(&mut self, ev: MouseEvent) {
        self.mouse = Some((ev.column, ev.row));
        match ev.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                let inner = crate::render::composer_inner(
                    self.regions.composer,
                    !self.composer_ref().images.is_empty(),
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
                if let Some(Target::SideRow(i)) = self.hits.at(ev.column, ev.row).cloned() {
                    self.ui.focus = Focus::Sidebar;
                    self.ui.cursor.insert(self.ui.tab, i);
                    if self.ui.tab == Tab::Sessions {
                        self.sidebar_activate(i);
                    }
                    self.execute("sidebar.row_menu");
                }
            }
            MouseEventKind::Drag(MouseButton::Left) if self.ui.left_drag => {
                let x0 = self.regions.left.map_or(0, |l| l.x);
                let want = ev.column.saturating_sub(x0) + 1;
                if want < crate::layout::RAIL_SNAP {
                    self.ui.left_rail = true;
                } else {
                    self.ui.left_rail = false;
                    self.ui.left_width =
                        want.clamp(crate::layout::LEFT_MIN, crate::layout::LEFT_MAX);
                }
            }
            MouseEventKind::Drag(MouseButton::Left) => {
                if let Some((start, top)) = self.ui.composer_drag {
                    let inner = crate::render::composer_inner(
                        self.regions.composer,
                        !self.composer_ref().images.is_empty(),
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
                self.ui.composer_drag = None;
                self.ui.left_drag = false;
            }
            MouseEventKind::ScrollUp => self.wheel(ev.column, ev.row, -3),
            MouseEventKind::ScrollDown => self.wheel(ev.column, ev.row, 3),
            _ => {}
        }
    }

    fn wheel(&mut self, x: u16, y: u16, delta: isize) {
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
            Target::Scrim => {
                if self.ui.overlay.is_some() {
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
                if self.viewed_id() == Some(id.as_str()) && self.ui.view == View::Session {
                    self.run_command("session.rename");
                } else {
                    self.open_session(&id);
                }
            }
            Target::SessionCrumb(i) => {
                let id = self
                    .viewed_id()
                    .and_then(|v| self.ancestors_of(v).get(i).map(|s| s.id.clone()));
                if let Some(id) = id {
                    self.open_session(&id);
                }
            }
            Target::SessionTabClose(i) => {
                if let Some(id) = self.tab_ids().get(i).cloned() {
                    self.close_tab(&id);
                }
            }
            Target::Notifications => self.run_command("notifications.open"),
            Target::Tab(tab) => {
                let id = match tab {
                    Tab::Sessions => "sidebar.tab.sessions",
                    Tab::Files => "sidebar.tab.files",
                    Tab::Projects => "sidebar.tab.projects",
                };
                self.run_command(id);
            }
            Target::SettingsEntry => self.run_command("app.settings"),
            Target::SideRow(i) => {
                self.ui.focus = Focus::Sidebar;
                self.ui.cursor.insert(self.ui.tab, i);
                self.sidebar_activate(i);
            }
            Target::SidebarEdge => self.ui.left_drag = true,
            Target::RailSearch => self.run_command("session.switch"),
            Target::NewSession => self.run_command("session.new"),
            Target::AddProject => self.run_command("project.add"),
            Target::RightSection(_) => self.ui.focus = Focus::Right,
            Target::GitRow(i) => {
                self.ui.focus = Focus::Right;
                self.open_git_diff(i);
            }
            Target::Timeline => self.ui.focus = Focus::Timeline,
            Target::ContextPreview { title, text } => {
                self.open_overlay(OverlayKind::ContextDetail {
                    title: format!("{title} · preview"),
                    text,
                    top: 0,
                })
            }
            Target::ContextSection(section) => {
                if let Some(starting) = self
                    .state
                    .model
                    .selected
                    .as_ref()
                    .and_then(|view| view.starting.as_ref())
                {
                    let value = match section.as_str() {
                        "System prompt" => &starting["system_prompt"],
                        "Tools" => &starting["tool_definitions"],
                        "Workspace tools" => &starting["workspace"],
                        _ => &starting["instruction_file"],
                    };
                    let text = if section == "Skills" || section == "MCP" {
                        format!("{section} are not available yet.")
                    } else if section != "System prompt"
                        && section != "Tools"
                        && section != "Workspace tools"
                    {
                        let path = value["path"].as_str().unwrap_or("unknown path");
                        let status = value["status"].as_str().unwrap_or("unknown");
                        let detail = value["content"]
                            .as_str()
                            .or_else(|| value["error"].as_str())
                            .unwrap_or("No content recorded.");
                        format!("{path} · {status}\n\n{detail}")
                    } else {
                        value.as_str().map(str::to_owned).unwrap_or_else(|| {
                            serde_json::to_string_pretty(value).unwrap_or_default()
                        })
                    };
                    let title = if section == "Tools" {
                        "Stored tool definitions".to_owned()
                    } else {
                        section
                    };
                    self.open_overlay(OverlayKind::ContextDetail {
                        title,
                        text,
                        top: 0,
                    });
                }
            }
            Target::Block(i) => {
                self.select_block(i);
                self.activate_block();
            }
            Target::CopyBlock(i) => {
                self.select_block(i);
                self.copy_selected();
            }
            Target::StickyUser(i) => self.select_block(i),
            Target::JumpLatest => self.run_command("session.jump_latest"),
            Target::Composer => self.ui.focus = Focus::Composer,
            Target::ComposerAgent => self.run_command("agent.change"),
            Target::ComposerModel => self.run_command("model.change"),
            Target::ComposerEffort => self.run_command("model.effort"),
            Target::ComposerContext => self.ui.focus = Focus::Composer,
            Target::ComposerDirectory => self.run_command("session.change_directory"),
            Target::ComposerWorktree => self.run_command("session.worktrees"),
            Target::FilesRoot => self.run_command("files.root_toggle"),
            Target::Wait(b) => {
                self.ui.focus = Focus::Waiting;
                self.run_command(match b {
                    WaitButton::Allow => "waiting.allow_once",
                    WaitButton::Deny => "waiting.deny",
                    WaitButton::Next => "waiting.next",
                    WaitButton::Submit => "waiting.submit_answers",
                });
            }
            Target::WaitOption(i) => {
                self.ui.focus = Focus::Waiting;
                if self.writable() {
                    self.ask_pick(i);
                } else {
                    self.toast(Level::Warning, READ_ONLY);
                }
            }
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
            Target::NoticeItem(i) => self.notice_activate(i),
            Target::DialogButton(i) => {
                if let Some(d) = self.ui.dialog.as_mut() {
                    d.button = i;
                }
                self.confirm_dialog(i == 1);
            }
            Target::SettingsRow(i) => {
                self.ui.settings_row = i;
                self.settings_activate(i);
            }
        }
    }

    pub fn apply_image_paste(
        &mut self,
        key: String,
        result: Result<Option<crate::images::Image>, String>,
    ) {
        self.ui.image_pasting.remove(&key);
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
        if self.ui.dialog.is_some() || self.ui.view == View::Settings {
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
            ApiCall::Provider { .. } => "Provider sign-in failed",
            ApiCall::Prompt { .. } | ApiCall::NewAndPrompt { .. } => "Send failed",
            ApiCall::NewSession { .. } => "New session failed",
            ApiCall::Continue { .. } => "Continue failed",
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
            ApiCall::ChangeDirectory { .. } => "Directory change failed",
            ApiCall::AddProject { .. } => "Add project failed",
            ApiCall::DeleteProject { .. } => "Remove project failed",
        }
    }
}

pub fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
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

/// Shared single-line/multi-line text editing keys.
fn edit_buffer(buf: &mut Composer, ev: &KeyEvent, name: &str, single_line: bool) {
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
