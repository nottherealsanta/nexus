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
    Projects,
}

impl Tab {
    pub fn id(self) -> &'static str {
        match self {
            Tab::Sessions => "sessions",
            Tab::Files => "files",
            Tab::Projects => "projects",
        }
    }
    pub fn from_id(id: &str) -> Option<Tab> {
        match id {
            "sessions" => Some(Tab::Sessions),
            "files" => Some(Tab::Files),
            "projects" => Some(Tab::Projects),
            _ => None,
        }
    }
}

/// How much of the agent's activity the timeline shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Detail {
    /// One line per activity group (`Ran 1 command, Read 2 files`).
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
    Agents,
    Efforts,
    Export,
    /// Where `/learn` puts the skills: this project or every project.
    LearnScope,
    Turns,
    /// Actions on the focused sidebar row (the manifest's `row_menu` placement).
    RowMenu,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaletteKind {
    Command(String),
    Provider(String, bool),
    Session(String),
    Project(String),
    Model(String),
    Agent(String),
    Effort(String),
    /// (format, scope): `markdown|json`, `session|tree`.
    Export(String, String),
    /// `project` or `global`, for the pending `/learn` source.
    Learn(String),
    /// A timeline block to jump to (turn, notice or diagnostic).
    Block(usize),
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OverlayKind {
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
    /// The full text of one alert (a toast or a past notification), wrapped
    /// and scrollable (DESIGN_PLAN.md D15). `back` is the notifications row
    /// it was opened from; Esc returns there.
    AlertDetail {
        id: u64,
        top: usize,
        back: Option<usize>,
    },
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

/// A deferred action that a confirm dialog performs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Confirmed {
    /// A model or agent switch that would make the next request start cold (K9).
    /// Exactly one of `model` and `agent` is set.
    Switch {
        session: String,
        model: Option<String>,
        agent: Option<String>,
        revision: i64,
    },
    DeleteSession(String),
    RemoveProject(String),
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
    ProviderKey,
    /// `[context] title_models`, comma separated; empty clears the list.
    TitleModels {
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

/// Everything the mouse can hit. Registered by the renderer as it draws.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    ToggleLeft,
    ToggleRight,
    Title,
    /// An open session's tab in the header, by position.
    SessionTab(usize),
    SessionTabClose(usize),
    /// An earlier crumb of the viewed child session's tab: index into its ancestors.
    SessionCrumb(usize),
    Notifications,
    Tab(Tab),
    SettingsEntry,
    SideRow(usize),
    /// The left sidebar's inner edge: drag to resize.
    SidebarEdge,
    /// Rail search (opens the session switcher).
    RailSearch,
    NewSession,
    /// The closing row of the Projects tab.
    AddProject,
    RightSection(usize),
    /// One changed file in the right sidebar's Git status, by row index.
    GitRow(usize),
    Timeline,
    ContextSection(String),
    /// One switch row in the starting-context inventory.
    ContextToggle(usize),
    ContextPreview {
        title: String,
        text: String,
    },
    /// One timeline block, by index in the last built timeline.
    Block(usize),
    CopyBlock(usize),
    /// The pinned copy of a user band (sticky header): scrolls back to the band, by block index.
    StickyUser(usize),
    JumpLatest,
    Composer,
    ComposerAgent,
    ComposerModel,
    ComposerEffort,
    ComposerContext,
    ComposerDirectory,
    ComposerWorktree,
    /// The Files tree root line (project checkout / session worktree).
    FilesRoot,
    Wait(WaitButton),
    /// Option `i` of the question the waiting card asks.
    WaitOption(usize),
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
