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
    Sessions,
    Projects,
    Models,
    Agents,
    Efforts,
    Export,
    Turns,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaletteKind {
    Command(String),
    Session(String),
    Project(String),
    Model(String),
    Agent(String),
    Effort(String),
    /// (format, scope): `markdown|json`, `session|tree`.
    Export(String, String),
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
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OverlayKind {
    Palette(Palette),
    Notifications { selected: usize },
    Help { top: usize },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Overlay {
    pub kind: OverlayKind,
    pub restore: Restore,
}

/// A deferred action that a confirm dialog performs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Confirmed {
    DeleteSession(String),
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
        // At most three stacked; the oldest goes (it stays in the history).
        while self.toasts.len() > 3 {
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
        self.toasts
            .retain(|t| held == Some(t.id) || now.duration_since(t.born) < Self::lifetime(t.level));
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
    Deny,
    Next,
    Submit,
}

/// Everything the mouse can hit. Registered by the renderer as it draws.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    ToggleLeft,
    ToggleRight,
    Title,
    Notifications,
    Tab(Tab),
    SettingsEntry,
    SideRow(usize),
    /// The left sidebar's inner edge: drag to resize.
    SidebarEdge,
    /// Rail search (opens the session switcher).
    RailSearch,
    NewSession,
    RightSection(usize),
    /// One changed file in the right sidebar's Git status, by row index.
    GitRow(usize),
    Timeline,
    /// One timeline block, by index in the last built timeline.
    Block(usize),
    CopyBlock(usize),
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
    Stop,
    Continue,
    BannerAction(usize),
    BannerHide(usize),
    ToastDismiss(u64),
    PaletteItem(usize),
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
