//! View state of the shell: focus, overlays, dialogs, scroll, alerts and the
//! mouse targets. No rendering and no Core calls here.

use std::time::{Duration, Instant};

use crate::composer::Composer;

/// Where the keyboard is. Exactly one element has focus; overlays record the
/// focus they replaced (`Restore`) and give it back when they close.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Focus {
    ToggleLeft,
    Sidebar,
    /// The project rail, reached with Left from the session list.
    Rail,
    Timeline,
    Waiting,
    Composer,
    Right,
    ToggleRight,
    /// A toast or banner (dismissible with Esc).
    Alert,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Side {
    Left,
    Right,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Tab {
    Sessions,
    Files,
    Git,
}

impl Tab {
    pub fn id(self) -> &'static str {
        match self {
            Tab::Sessions => "sessions",
            Tab::Files => "files",
            Tab::Git => "git",
        }
    }
    pub fn from_id(id: &str) -> Option<Tab> {
        match id {
            "sessions" => Some(Tab::Sessions),
            "files" => Some(Tab::Files),
            "git" => Some(Tab::Git),
            _ => None,
        }
    }
}

/// How much of the agent's activity the timeline shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Detail {
    /// One line per activity group (`3 thoughts, 50 commands, 8 tools, 9 reads`).
    Low,
    /// Finished groups fold to their summary; a live group shows its newest
    /// members as a tree.
    #[default]
    Medium,
    /// Every tool expanded, long output capped to its ends.
    High,
}

impl Detail {
    pub const ALL: [Detail; 3] = [Detail::Low, Detail::Medium, Detail::High];

    pub fn label(self) -> &'static str {
        match self {
            Detail::Low => "low",
            Detail::Medium => "medium",
            Detail::High => "high",
        }
    }

    pub fn from_label(label: &str) -> Option<Detail> {
        Self::ALL.into_iter().find(|d| d.label() == label)
    }

    /// The level `step` places along low, medium, high (wrapping).
    pub fn step(self, step: isize) -> Detail {
        let now = Self::ALL.iter().position(|d| *d == self).unwrap_or(1) as isize;
        Self::ALL[(now + step).rem_euclid(3) as usize]
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum View {
    Session,
    Settings,
}

/// Timeline scroll: following the latest item, or pinned `top` lines from the
/// start of the content (so new content below does not move what you read).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Scroll {
    pub follow: bool,
    pub top: usize,
}

impl Default for Scroll {
    fn default() -> Self {
        Self {
            follow: true,
            top: 0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResourceKind {
    All,
    Kernels,
    Shells,
}

/// What an overlay replaced: the focus and the scroll positions it must give back.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Restore {
    pub focus: Focus,
    pub scroll: Scroll,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaletteMode {
    Commands,
    ProviderLogin,
    ProviderLogout,
    Sessions,
    Projects,
    Models,
    /// A model for an agent's list in Settings (`AppUi::pool_pick` says which).
    PoolModels,
    Agents,
    Efforts,
    Export,
    /// Where `/learn` puts the skills: this project or every project.
    LearnScope,
    Turns,
    /// Earlier prompts of this project (`ctrl+r`): choosing one fills the composer.
    History,
    /// Actions on the focused sidebar row (the manifest's `row_menu` placement).
    RowMenu,
    /// The focused group's menu behind the strip's glyph (plan 45).
    GroupMenu,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaletteKind {
    Command(String),
    Provider(String, bool),
    Session(String),
    Project(String),
    Model(String),
    PoolModel(String),
    Agent(String),
    Effort(String),
    /// (format, scope): `markdown|json`, `session|tree`.
    Export(String, String),
    /// `project` or `global`, for the pending `/learn` source.
    Learn(String),
    /// A timeline block to jump to (turn, notice or diagnostic).
    Block(usize),
    /// An earlier prompt, in full.
    History(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaletteItem {
    pub kind: PaletteKind,
    pub label: String,
    /// Key hint (commands) or meta (sessions, projects).
    pub hint: String,
    pub category: String,
    /// `Some` when disabled: shown beside the label, never hidden.
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Palette {
    pub mode: PaletteMode,
    pub query: Composer,
    pub selected: usize,
    pub top: usize,
    pub model_info: bool,
}

/// One tool, skill or server in the context entries modal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextEntry {
    pub name: String,
    pub description: String,
    /// What opening it shows; `None` until Core has answered.
    pub full: Option<String>,
    pub markdown: bool,
    /// `skill` or `mcp`: where to ask Core for the full text.
    pub fetch: Option<&'static str>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OverlayKind {
    Inspector(crate::inspector::Inspector),
    Palette(Palette),
    LearnChecklist(LearnChecklist),
    Notifications {
        selected: usize,
    },
    Help {
        top: usize,
    },
    ContextDetail {
        title: String,
        text: String,
        top: usize,
    },
    /// A finished subagent notice: its facts, the exact model text, and a way
    /// to open the child session. Enter opens it; Esc closes.
    SubagentDone {
        session_id: String,
        agent: String,
        title: String,
        model: String,
        duration: String,
        state: String,
        text: String,
        top: usize,
    },
    /// A system prompt or instruction file, rendered as Markdown.
    ContextDoc {
        title: String,
        text: String,
        top: usize,
    },
    /// The tools, skills or servers of the starting context: each with its
    /// description, and its full definition under the row when opened.
    ContextEntries {
        title: String,
        entries: Vec<ContextEntry>,
        cursor: usize,
        open: Option<usize>,
        /// Rows scrolled inside the open entry.
        top: usize,
    },
    /// The full-size view of an attachment preview, over the scrim.
    ImagePreview {
        name: String,
        data_url: String,
    },
    /// Drawing on a composer image.
    Annotate(Box<crate::annotate::Annotation>),
    /// Usage of every connected provider (`/usage`, ctrl+u); `r` reads it again.
    Usage {
        rows: Vec<crate::usage::Row>,
        top: usize,
    },
    /// Live kernels and background shells of the viewed session tree.
    Resources {
        top: usize,
        kind: ResourceKind,
        /// The selected background shell (index into the viewed jobs); `s` stops it.
        selected: usize,
    },
    /// The full text of one alert (a toast or a past notification), wrapped
    /// and scrollable (DESIGN_PLAN.md D15). `back` is the notifications row
    /// it was opened from; Esc returns there.
    AlertDetail {
        id: u64,
        top: usize,
        back: Option<usize>,
    },
    /// Every project in its saved order, with active sessions and an archived
    /// toggle per project. Inactive projects sit in their own group.
    Projects(ProjectsOverview),
    /// The compact linked-worktree details: full path, branch and the main
    /// checkout, each copyable.
    WorktreeInfo(WorktreeInfo),
}

/// What the worktree modal shows. It is copied when the modal opens so the
/// values cannot change under the pointer.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct WorktreeInfo {
    pub path: String,
    pub branch: String,
    pub main: String,
    /// False when the session is in the repository's main checkout.
    pub linked: bool,
    /// First body row shown while the modal scrolls.
    pub top: usize,
}

/// Selection and expansion of the projects overview.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ProjectsOverview {
    /// Index into `App::overview_rows`.
    pub cursor: usize,
    pub top: usize,
    /// Projects whose archived sessions are expanded.
    pub archived: std::collections::HashSet<String>,
}

/// A rail drag in progress: its source and current insertion target.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RailDrag {
    pub from: usize,
    pub to: usize,
    pub start_row: u16,
    pub active: bool,
    pub cancelled: bool,
}

/// One candidate returned by `/api/skill/preview`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LearnSkill {
    pub name: String,
    pub description: String,
    pub installed: bool,
}

/// Selection state for the second `/learn` modal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LearnChecklist {
    pub fetch_id: String,
    pub source: String,
    pub scope: String,
    pub project: String,
    pub skills: Vec<LearnSkill>,
    pub selected: Vec<bool>,
    pub cursor: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Overlay {
    pub kind: OverlayKind,
    pub restore: Restore,
}

/// The built-in editor over the open file (K10): the text being edited and the revision it was read at.
#[derive(Debug)]
pub struct FileEdit {
    pub buf: crate::code_editor::CodeBuffer,
    /// The `revision` the last read or save returned; a write is refused if the file moved on.
    pub revision: String,
    /// The text at that revision, to tell whether the buffer is dirty.
    pub saved: String,
    pub top: usize,
    pub saving: bool,
    /// A reload was asked: the next read of the file replaces the buffer.
    pub reloading: bool,
    /// The file changed on disk under unsaved text, or a save was refused.
    pub stale: bool,
    /// A left-button drag that began in the text is selecting.
    pub dragging: bool,
}

impl FileEdit {
    pub fn dirty(&self) -> bool {
        self.buf.text() != self.saved
    }
}

/// A deferred action that a confirm dialog performs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Confirmed {
    ForceGitDelete {
        project: String,
        worktree: Option<String>,
        value: String,
        confirmed_live: Vec<String>,
    },
    ProjectGitAction {
        project: String,
        worktree: Option<String>,
        action: String,
        value: String,
        paths: Vec<String>,
        confirmed_live: Vec<String>,
    },
    DeleteFile {
        project: String,
        session: Option<String>,
        path: String,
    },
    CloseTerminal(String),
    /// Closing a file tab with unsaved text: Save, Discard or Cancel.
    CloseDirty {
        group: crate::groups::GroupId,
        tab: crate::groups::Tab,
    },
    /// Closing a group that holds unsaved files: Save all, Discard all or Cancel.
    CloseDirtyGroup { group: crate::groups::GroupId },
    AttachTerminalEvidence {
        draft: String,
        payload: String,
        offset: usize,
    },
    ResumeArchived {
        session: String,
        mode: Option<String>,
    },
    Unarchive(String),
    /// Read the file again and drop the unsaved text.
    ReloadEdit,
    /// A model or agent switch that would make the next request start cold (K9).
    /// Exactly one of `model` and `agent` is set.
    Switch {
        session: String,
        model: Option<String>,
        agent: Option<String>,
        revision: i64,
    },
    DeleteSession(String),
    /// Remove one pending input for good, rather than editing it back into the composer.
    RemoveInput {
        session: String,
        id: String,
    },
    RemoveProject(String),
    /// Sign out of a provider whose models are listed for subagents.
    Disconnect(String),
    DownloadStt,
    DownloadVoice(crate::actions::VoiceTarget),
    RemoveVoice {
        target: crate::actions::VoiceTarget,
        repo: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Dialog {
    pub title: String,
    pub body: Vec<String>,
    pub cancel: String,
    pub confirm: String,
    pub danger: bool,
    pub action: Confirmed,
    /// 0 = cancel (the safe choice is first and focused), 1 = confirm.
    pub button: usize,
    pub restore: Restore,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EditTarget {
    ProjectGitValue {
        project: String,
        worktree: Option<String>,
        action: String,
    },
    FilePath {
        project: String,
        session: Option<String>,
        action: String,
        path: String,
    },
    RenameTerminal {
        id: String,
    },
    RenameSession {
        id: String,
        revision: i64,
    },
    /// Move the session to an existing directory (applied at a safe boundary).
    ChangeDirectory {
        id: String,
    },
    /// Add a project by directory path.
    AddProject,
    /// A provider's API key (masked while typed).
    ProviderKey {
        id: String,
    },
    /// `[context] title_models`, comma separated; empty clears the list.
    TitleModels {
        revision: String,
    },
    /// `[manager] model`, one `provider/model` ref.
    ManagerModel {
        revision: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TextEdit {
    /// What the inline field edits ("rename", "directory").
    pub label: String,
    pub target: EditTarget,
    pub buf: Composer,
    pub restore: Restore,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Info,
    Success,
    Warning,
    Error,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Toast {
    pub id: u64,
    pub level: Level,
    pub text: String,
    pub born: Instant,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notice {
    pub id: u64,
    pub level: Level,
    pub text: String,
    pub session: Option<String>,
    pub read: bool,
}

/// Toasts drawn at once; the rest wait behind `+N more`.
pub const SHOWN: usize = 3;
/// Toasts kept in all, drawn or queued.
const QUEUE: usize = 9;

/// A toast's title and body from its full text: the first line is the title
/// and the rest the body; a single line splits at its first `: ` when that
/// comes early (`Export failed: disk full`).
pub fn split_title(text: &str) -> (String, String) {
    if let Some((head, rest)) = text.split_once('\n') {
        return (head.trim().to_owned(), rest.trim().to_owned());
    }
    match text.split_once(": ") {
        Some((head, rest)) if head.chars().count() <= 32 && !rest.is_empty() => {
            (head.to_owned(), rest.to_owned())
        }
        _ => (text.to_owned(), String::new()),
    }
}

/// Toasts expire; every one is also kept as a notice (the notifications drawer).
#[derive(Debug, Default, Clone)]
pub struct Alerts {
    pub toasts: Vec<Toast>,
    pub history: Vec<Notice>,
    next: u64,
}

impl Alerts {
    pub fn push(&mut self, level: Level, text: impl Into<String>, now: Instant) {
        self.push_for(level, text, None, now)
    }

    pub fn push_for(
        &mut self,
        level: Level,
        text: impl Into<String>,
        session: Option<String>,
        now: Instant,
    ) {
        let text = text.into();
        self.next += 1;
        let id = self.next;
        self.history.insert(
            0,
            Notice {
                id,
                level,
                text: text.clone(),
                session,
                read: false,
            },
        );
        self.history.truncate(100);
        self.toasts.push(Toast {
            id,
            level,
            text,
            born: now,
        });
        // Three are drawn, the rest queue behind `+N more`; the oldest goes
        // past `QUEUE` (it stays in the history).
        while self.toasts.len() > QUEUE {
            self.toasts.remove(0);
        }
    }

    pub fn lifetime(level: Level) -> Duration {
        if level == Level::Error {
            Duration::from_secs(8)
        } else {
            Duration::from_secs(4)
        }
    }

    /// Drop expired toasts, except the one the pointer or focus holds.
    pub fn expire(&mut self, now: Instant, held: Option<u64>) {
        // A queued toast starts its lifetime when it is first drawn.
        for t in self.toasts.iter_mut().skip(SHOWN) {
            t.born = now;
        }
        self.toasts
            .retain(|t| held == Some(t.id) || now.duration_since(t.born) < Self::lifetime(t.level));
    }

    /// The full text and level of the alert `id`, toast or history entry.
    pub fn full(&self, id: u64) -> Option<(Level, &str)> {
        self.history
            .iter()
            .find(|n| n.id == id)
            .map(|n| (n.level, n.text.as_str()))
            .or_else(|| {
                self.toasts
                    .iter()
                    .find(|t| t.id == id)
                    .map(|t| (t.level, t.text.as_str()))
            })
    }

    pub fn dismiss(&mut self, id: u64) {
        self.toasts.retain(|t| t.id != id);
    }

    pub fn unread(&self) -> usize {
        self.history.iter().filter(|n| !n.read).count()
    }

    pub fn mark_all_read(&mut self) {
        self.history.iter_mut().for_each(|n| n.read = true);
    }
}

/// A banner derived from state each frame; stays until its condition clears.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BannerItem {
    pub level: Level,
    pub loading: bool,
    pub text: String,
    /// (key hint, label, command id)
    pub action: Option<(String, String, String)>,
    /// Index into `AppState::banners` when the user can hide it.
    pub hide: Option<usize>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WaitButton {
    Allow,
    AllowSession,
    Deny,
    DenyNote,
    Next,
    Submit,
}

/// The four actions on a queued message row.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PendingAction {
    Edit,
    Up,
    Down,
    Remove,
}

/// One copyable value in the worktree modal.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum WorktreeCopy {
    Path,
    Branch,
    Main,
}

/// Everything the mouse can hit. Registered by the renderer as it draws.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    Command(String),
    ToggleLeft,
    ToggleRight,
    Title,
    /// A tab in a group's strip, by group and position; its close control;
    /// the strip's `+` (new session) and `⋯` (group menu); the body (click to
    /// focus); a divider between groups (index into the last drawn dividers);
    /// the marker that other groups are hidden.
    GroupTab(crate::groups::GroupId, usize),
    GroupTabClose(crate::groups::GroupId, usize),
    GroupNew(crate::groups::GroupId),
    GroupMenu(crate::groups::GroupId),
    GroupBody(crate::groups::GroupId),
    /// The composer of a session an unfocused group shows: focus that group and its composer.
    PeerComposer(crate::groups::GroupId),
    GroupDivider(usize),
    /// The archive control on a hovered sidebar session row.
    ArchiveSession(String),
    SessionRowMenu(usize),
    /// The `×` on a hovered project row: remove it (after confirming).
    RemoveProject(String),
    /// The heading row's compact back control: return to the viewed session's root.
    SessionRootBack,
    Notifications,
    /// The header's Manager button.
    ManagerSession,
    Tab(Tab),
    SettingsEntry,
    UsageRefresh,
    SideRow(usize),
    /// The left sidebar's inner edge: drag to resize.
    SidebarEdge,
    /// Rail search (opens the session switcher).
    RailSearch,
    ArchivedFilter,
    /// A row of the context entries modal.
    ContextEntry(usize),
    /// An inspector row (by record id) and a session-tree entry (by session id).
    InspectorRow(String),
    InspectorSession(String),
    /// A project on the rail, by its place in creation order.
    RailProject(usize),
    SessionScope,
    NewSession,
    /// The add-project control on the rail.
    AddProject,
    RightSection(usize),
    /// One changed file in the right sidebar's Git status, by row index.
    Timeline,
    ContextSection(String),
    /// One switch row in the starting-context inventory.
    ContextToggle(usize),
    /// The name of a tool, skill or MCP server in the context: show what it is.
    ContextInfo(usize),
    ContextPreview {
        title: String,
        text: String,
    },
    /// One timeline block, by index in the last built timeline.
    Block(usize),
    /// The left edge of a user band: fold or unfold it.
    BandToggle(usize),
    CopyBlock(usize),
    /// The pinned copy of a user band (sticky header): scrolls back to the band, by block index.
    StickyUser(usize),
    /// An attachment preview (timeline or composer): open its full-size modal.
    ImagePreview {
        name: String,
        data_url: String,
    },
    /// A composer thumbnail: draw on it.
    ComposerImage(usize),
    /// A pen colour in the drawing modal.
    AnnotateInk(usize),
    AnnotateUndo,
    AnnotateClear,
    AnnotateDone,
    /// A collapsed paste marker in a user message: open its full text.
    TimelinePaste(std::rc::Rc<crate::timeline::PasteText>),
    JumpLatest,
    Composer,
    ComposerAgent,
    ComposerModel,
    ComposerEffort,
    ComposerContext,
    ComposerDirectory,
    ComposerWorktree,
    /// The location strip's linked-worktree icon: show the worktree modal.
    ComposerWorktreeInfo,
    /// The kernel and running-shell counts on the location strip.
    ComposerResources,
    ComposerShells,
    ComposerTerminals,
    /// A background shell row in the Shells modal: select it.
    ResourceJob(usize),
    /// A running background shell's `[stop]` action: kill the job.
    ResourceStop(String),
    TerminalCopy,
    TerminalAttach,
    TerminalTakeControl,
    TerminalReleaseControl,
    TerminalRename,
    /// The Files tree root line (project checkout / session worktree).
    FilesRoot,
    /// The panel header's `‹ Back`: close the file or diff panel.
    PanelBack,
    /// The panel header's `p source` / `p preview`: switch a Markdown file.
    FilePreview,
    /// The panel header's `e edit`: start the file editor.
    FileEdit,
    EditorFindInput,
    EditorReplaceInput,
    EditorFindCase,
    EditorFindPrevious,
    EditorFindNext,
    EditorFindClose,
    EditorReplaceOne,
    EditorReplaceAll,
    Wait(WaitButton),
    /// Option `i` of the question the waiting card asks.
    WaitOption(usize),
    /// The question card's custom answer field.
    WaitInput,
    /// One action on queued pending input `i`, right-aligned on its row.
    PendingAction(usize, PendingAction),
    PendingNext,
    /// Copy one value out of the open worktree modal.
    WorktreeCopy(WorktreeCopy),
    Continue,
    BannerAction(usize),
    BannerHide(usize),
    ToastDismiss(u64),
    /// Open the full text of a toast.
    ToastOpen(u64),
    PaletteItem(usize),
    LearnSkill(usize),
    NoticeItem(usize),
    DialogButton(usize),
    /// A surface that swallows clicks (dialogs, palette body, drawers).
    Swallow,
    /// Outside an overlay: clicking closes it.
    Scrim,
    /// Row `i` of the composer's completion popup.
    CompletionRow(usize),
    /// A row of the settings list, by index into `App::settings_items`.
    SettingsRow(usize),
    /// One option of a segmented Settings row, by row index and option position.
    SettingsSegment {
        row: usize,
        option: usize,
    },
    /// A section in Settings' left rail, by index into `App::settings_sections`.
    SettingsSection(usize),
    /// A selectable item on a Settings page, by position among the page's items.
    SettingsPageRow(usize),
    /// A row of a provider's detail modal.
    ProviderDetailRow(usize),
    /// The Manager's indicator: a click talks, or stops the live voice.
    ManagerIndicator,
    ManagerVoice,
    ManagerMute,
    /// The Manager's End button: a click ends the call.
    ManagerEnd,
    ManagerDetails,
    ManagerInput,
    ManagerDebugSession,
    /// Outside a focused Manager card: a click dismisses it.
    ManagerScrim,
    /// An unfocused Manager card: a click focuses it.
    ManagerCard,
    /// One row of the projects overview.
    OverviewRow(usize),
    /// Row `n` of a focused Manager card: a click opens the session.
    ManagerRow(usize),
}

/// One row of the composer's completion popup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletionRow {
    /// The text that is accepted: `/name` or a workspace path.
    pub value: String,
    /// Muted help beside the value (a command's title); empty for paths.
    pub help: String,
    /// `Some` for a command that is off right now.
    pub reason: Option<String>,
}
