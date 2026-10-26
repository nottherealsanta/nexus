//! Left sidebar: Projects, Sessions, Files and Git tabs beside the conversation.
//!
//! Python owns every datum (`nexus/ui/ratatui/sidebar.py`) and sends it in the `sidebar`
//! snapshot block; this module only places, paints and hit-tests it, and turns keys and
//! clicks into `{"type":"sidebar",...}` actions or Sessions-dashboard operations. It never
//! reads the filesystem or runs Git. The Sessions tab reuses `picker::Dashboard` for
//! filtering, selection, rename and archive confirmation. Painting records the geometry
//! (tab cells, list rows, divider) that mouse handling uses, so both share one layout.
use crate::bridge::Snapshot;
use crate::editor::Editor;
use crate::input::edit;
use crate::picker::{self, Dashboard};
use crate::render::Palette;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    layout::{Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Clear, Paragraph},
    Frame,
};
use serde_json::{json, Value};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

pub const TABS: [&str; 2] = ["Projects", "Sessions"];
/// Mirrors `nexus/ui_support/sidebar.py` (pinned by `tests/test_ratatui_sidebar.py`).
pub const MIN_WIDTH: u16 = 24;
pub const MAX_WIDTH: u16 = 60;
pub const DEFAULT_WIDTH: u16 = 34;
/// The conversation never gets narrower than this because of the sidebar.
pub const MIN_CONVERSATION: u16 = 56;

const SPIN: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/// A stored or dragged width, bounded to `MIN_WIDTH..=MAX_WIDTH` (0 means "not set").
pub fn clamp_width(width: u16) -> u16 {
    if width == 0 {
        DEFAULT_WIDTH
    } else {
        width.clamp(MIN_WIDTH, MAX_WIDTH)
    }
}

/// Columns the sidebar takes out of `available` (the columns left of the details sidebar):
/// 0 while closed, or when the conversation would drop below `MIN_CONVERSATION`.
pub fn width_for(s: &Snapshot, available: u16) -> u16 {
    if !s.sidebar.open {
        return 0;
    }
    let room = available.saturating_sub(MIN_CONVERSATION);
    let width = clamp_width(s.sidebar.width).min(room);
    if width < MIN_WIDTH {
        0
    } else {
        width
    }
}

/// The widest the sidebar may be dragged given `available` columns.
pub fn max_width(available: u16) -> u16 {
    available
        .saturating_sub(MIN_CONVERSATION)
        .clamp(MIN_WIDTH, MAX_WIDTH)
}

/// The width a drag to terminal `column` asks for (the divider sits on the last column).
pub fn width_at(column: u16, area: Rect, available: u16) -> u16 {
    column
        .saturating_sub(area.x)
        .saturating_add(1)
        .clamp(MIN_WIDTH, max_width(available))
}

/// Centered selector geometry, including on small terminals.
pub fn selector_area(area: Rect) -> Rect {
    let width = area.width.saturating_sub(4).min(90);
    let height = area.height.saturating_sub(4).min(32);
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}

/// A browser view of the shared host payload without changing selector state.
pub fn details_snapshot(s: &Snapshot) -> Snapshot {
    let mut view = Snapshot {
        sidebar: s.sidebar.clone(),
        ..Snapshot::default()
    };
    view.sidebar.tab = if s.details_panel.tab == "Browser" {
        "Files"
    } else {
        "Git"
    }
    .into();
    view
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TabCell {
    pub tab: usize,
    pub row: u16,
    pub x: u16,
    pub width: u16,
}

/// Tab labels (` Label `, one column apart) wrapped onto as many rows as the width needs.
/// Painting and hit-testing both read this, so a click lands on what is drawn.
pub fn tab_cells(inner: u16) -> (Vec<TabCell>, u16) {
    let mut cells = Vec::new();
    let (mut x, mut row) = (0u16, 0u16);
    for (tab, label) in TABS.iter().enumerate() {
        let width = label.width() as u16 + 2;
        if x > 0 && x.saturating_add(width) > inner {
            row += 1;
            x = 0;
        }
        cells.push(TabCell {
            tab,
            row,
            x,
            width: width.min(inner.saturating_sub(x)),
        });
        x = x.saturating_add(width + 1);
    }
    (cells, row + 1)
}

fn tab_index(s: &Snapshot) -> usize {
    TABS.iter()
        .position(|tab| *tab == s.sidebar.tab)
        .unwrap_or(1)
}

fn act(op: &str) -> Value {
    json!({"type":"sidebar","op":op})
}

fn tab_message(tab: &str) -> Value {
    json!({"type":"sidebar","op":"tab","tab":tab})
}

/// What an input event did: whether the sidebar consumed it, a bridge action to send, a
/// Sessions-dashboard operation to send (with the generation) and a width to show right now.
#[derive(Default)]
pub struct Out {
    pub consumed: bool,
    pub action: Option<Value>,
    pub operation: Option<Value>,
    pub width: Option<u16>,
}

impl Out {
    fn eat() -> Self {
        Self {
            consumed: true,
            ..Self::default()
        }
    }
    fn action(value: Value) -> Self {
        Self {
            consumed: true,
            action: Some(value),
            ..Self::default()
        }
    }
    fn operation(value: Value) -> Self {
        Self {
            consumed: true,
            operation: Some(value),
            ..Self::default()
        }
    }
}

#[derive(Default)]
pub struct SidebarUi {
    /// Keep an opened session's selector dismissed until the host acknowledges the close.
    session_dismissed: bool,
    /// Keys go to the sidebar first (set by a click or a request to the open sidebar; Esc leaves).
    pub focus: bool,
    /// The divider is being dragged.
    pub dragging: bool,
    divider_hot: bool,
    /// The Sessions tab's presentation state (query, selection, rename, archive confirmation).
    pub dash: Dashboard,
    divider: Rect,
    tab_hits: Vec<(Rect, usize)>,
    list: Rect,
    line_items: Vec<Option<usize>>,
    project_edit: Option<Editor>,
    proj_sel: usize,
    proj_top: usize,
    file_sel: usize,
    file_top: usize,
    file_path: String,
    /// The last click on a Files row (path, time), to recognise a double-click on the same file.
    last_file_click: Option<(String, Instant)>,
    git_top: usize,
}

/// Two clicks on the same file row within this interval open the file preview.
const DOUBLE_CLICK: Duration = Duration::from_millis(500);

fn rows_of(value: &Value) -> &[Value] {
    value
        .get("rows")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    picker::text(value, key)
}

fn flag(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool) == Some(true)
}

fn fit(value: &str, width: usize) -> String {
    if value.width() <= width {
        return value.to_owned();
    }
    let mut out = String::new();
    for c in value.chars() {
        if out.width() + 1 >= width {
            break;
        }
        out.push(c);
    }
    if width > 0 {
        out.push('…');
    }
    out
}

/// Whether anything in the open sidebar animates (working rows spin, waits tick).
pub fn animating(s: &Snapshot) -> bool {
    if !s.sidebar.open {
        return false;
    }
    let sessions = s.sidebar.sessions.as_ref().is_some_and(|dashboard| {
        rows_of(dashboard)
            .iter()
            .any(|row| text(row, "status") == "working" || picker::pending(row).is_some())
    });
    let projects = rows_of(&s.sidebar.projects)
        .iter()
        .any(|row| row.get("rank").and_then(Value::as_u64) == Some(1));
    sessions || projects
}

/// One list line: `prefix`, the truncated `main` text, then `right` flush right (dropped when
/// there is no room). `bg` fills the whole line; `bar` replaces the first column with the
/// focus bar so a selection survives monochrome.
#[allow(clippy::too_many_arguments)]
fn row_line(
    mut prefix: Vec<Span<'static>>,
    main: &str,
    main_style: Style,
    right: &str,
    right_style: Style,
    width: usize,
    bg: Option<Color>,
    bar: bool,
) -> Line<'static> {
    let prefix_width: usize = prefix.iter().map(|span| span.content.width()).sum();
    let right = if !right.is_empty() && prefix_width + right.width() + 1 < width {
        right
    } else {
        ""
    };
    let reserved = if right.is_empty() {
        0
    } else {
        right.width() + 1
    };
    let main = fit(main, width.saturating_sub(prefix_width + reserved));
    let used = prefix_width + main.width() + right.width();
    if bar {
        if let Some(first) = prefix.first_mut() {
            *first = Span::styled(
                nexus_widgets::Glyphs::from_env().focus_bar,
                Style::default().add_modifier(Modifier::BOLD),
            );
        }
    }
    let mut spans = prefix;
    spans.push(Span::styled(main, main_style));
    spans.push(Span::raw(" ".repeat(width.saturating_sub(used))));
    if !right.is_empty() {
        spans.push(Span::styled(right.to_owned(), right_style));
    }
    if let Some(bg) = bg {
        for span in &mut spans {
            span.style = span.style.bg(bg);
        }
    }
    Line::from(spans)
}

/// The status mark of a project or session row: needs-you, working (spinner), idle.
fn mark(rank: u64, spin: usize, p: &Palette) -> Span<'static> {
    let ascii = nexus_widgets::Glyphs::from_env().ascii;
    match rank {
        0 => Span::styled(
            if ascii { "!" } else { "◆" },
            Style::default().fg(p.warning),
        ),
        1 => {
            let frame = if ascii {
                let frames = nexus_widgets::Glyphs::ascii().spinner;
                frames[spin % frames.len()]
            } else {
                SPIN[spin % SPIN.len()]
            };
            Span::styled(frame, Style::default().fg(p.purple))
        }
        _ => Span::styled(if ascii { "o" } else { "◇" }, Style::default().fg(p.quiet)),
    }
}

fn session_rank(row: &Value) -> u64 {
    match text(row, "status") {
        "input" => 0,
        "working" => 1,
        _ => 2,
    }
}

/// Moves `sel` for a navigation key over `len` items; false when `code` is not one.
fn move_selection(sel: &mut usize, len: usize, code: KeyCode) -> bool {
    let nav = matches!(
        code,
        KeyCode::Up
            | KeyCode::Down
            | KeyCode::PageUp
            | KeyCode::PageDown
            | KeyCode::Home
            | KeyCode::End
    );
    if !nav || len == 0 {
        return nav;
    }
    *sel = (*sel).min(len - 1);
    match code {
        KeyCode::Up => *sel = sel.saturating_sub(1),
        KeyCode::Down => *sel = (*sel + 1).min(len - 1),
        KeyCode::PageUp => *sel = sel.saturating_sub(10),
        KeyCode::PageDown => *sel = (*sel + 10).min(len - 1),
        KeyCode::Home => *sel = 0,
        _ => *sel = len - 1,
    }
    true
}

impl SidebarUi {
    /// Consume sidebar paste rather than leaking it into the conversation draft.
    pub fn paste(&mut self, text: &str, s: &Snapshot) -> bool {
        if !self.is_open(s) || !self.focus {
            return false;
        }
        if s.sidebar.tab == "Projects" {
            if let Some(editor) = self.project_edit.as_mut() {
                let text: String = text.chars().filter(|c| !c.is_control()).collect();
                if editor.text.len() + text.len() <= 4096 {
                    editor.insert(&text);
                }
            }
        }
        true
    }

    pub fn is_open(&self, s: &Snapshot) -> bool {
        s.sidebar.open && !self.session_dismissed
    }

    fn session_operation(&mut self, operation: Value) -> Out {
        if operation.get("kind").and_then(Value::as_str) == Some("session_open") {
            self.session_dismissed = true;
            self.focus = false;
            self.dragging = false;
        }
        Out::operation(operation)
    }

    /// A new snapshot may ask the already-open sidebar for keyboard focus (the host bumps
    /// `sidebar.focus`). Opening it never takes focus: the composer keeps the keys until a click.
    pub fn take_host_focus(&mut self, prev: &Snapshot, next: &Snapshot) {
        if next.sidebar.open && next.sidebar.reveal != prev.sidebar.reveal {
            self.session_dismissed = false; // `/sessions` after opening a session shows the sidebar again
        }
        if self.is_open(next) && (!prev.sidebar.open || next.sidebar.focus != prev.sidebar.focus) {
            self.focus = true;
        }
    }

    /// Called every loop: local state lives only while the host shows the sidebar.
    pub fn sync(&mut self, s: &Snapshot) {
        if !s.sidebar.open || !flag(&s.sidebar.projects, "browser_open") {
            self.project_edit = None;
        }
        if !s.sidebar.open {
            self.session_dismissed = false;
            if self.focus || self.dragging {
                self.focus = false;
                self.dragging = false;
            }
            if self.dash.selected_id.is_empty() && self.dash.query.is_empty() {
                return;
            }
            self.dash = Dashboard::default();
        } else if s.sidebar.sessions.is_none() {
            self.dash = Dashboard::default();
        }
    }

    // ----- keys -----------------------------------------------------------------------

    /// One key while the sidebar has focus. `draft_empty` lets the Sessions list own letters
    /// only when the composer has nothing to type into.
    pub fn key(&mut self, key: KeyEvent, s: &Snapshot, draft_empty: bool) -> Out {
        let control = key.modifiers.contains(KeyModifiers::CONTROL);
        if control && key.code == KeyCode::Char('b') {
            return Out::action(act("toggle"));
        }
        let tab = s.sidebar.tab.as_str();
        let editing = (tab == "Projects" && self.project_edit.is_some())
            || tab == "Sessions"
                && (self.dash.searching
                    || self.dash.rename.is_some()
                    || self.dash.confirm_archive.is_some());
        if !editing {
            match key.code {
                KeyCode::Tab => {
                    return Out::action(tab_message(TABS[(tab_index(s) + 1) % TABS.len()]))
                }
                KeyCode::BackTab => {
                    return Out::action(tab_message(
                        TABS[(tab_index(s) + TABS.len() - 1) % TABS.len()],
                    ))
                }
                KeyCode::Esc if tab == "Projects" && flag(&s.sidebar.projects, "browser_open") => {
                    self.proj_sel = 0;
                    return Out::action(act("cancel_project"));
                }
                KeyCode::Esc if !(tab == "Sessions" && !self.dash.query.is_empty()) => {
                    self.focus = false;
                    return Out::action(act("close"));
                }
                _ => {}
            }
        }
        match tab {
            "Projects" => self.projects_key(key, s),
            "Files" => self.files_key(key, s),
            "Git" => self.git_key(key),
            _ => {
                let Some(dashboard) = s.sidebar.sessions.as_ref() else {
                    return self.unowned(&key);
                };
                if self.dash.owns(&key, draft_empty) {
                    return match self.dash.key(key, dashboard) {
                        Some(operation) => self.session_operation(operation),
                        None => Out::eat(),
                    };
                }
                self.unowned(&key)
            }
        }
    }

    /// A key no tab wants: typing hands focus back to the composer and falls through to it.
    fn unowned(&mut self, key: &KeyEvent) -> Out {
        if let KeyCode::Char(_) = key.code {
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
            {
                self.focus = false;
            }
        }
        Out::default()
    }

    fn activate_picker(&mut self, s: &Snapshot) -> Out {
        let data = &s.sidebar.projects;
        if !flag(data, "browser_open") {
            if self.proj_sel == 0 {
                self.proj_sel = 0;
                return Out::action(act("add_project"));
            }
            let rows = rows_of(data);
            let Some(row) = rows.get(self.proj_sel - 1) else {
                return Out::eat();
            };
            if text(row, "workspace").is_empty() {
                return Out::eat();
            }
            return Out::action(
                json!({"type":"sidebar","op":"open_project","workspace":text(row,"workspace")}),
            );
        }
        if self.proj_sel == 0 {
            let mut editor = Editor::default();
            editor.insert(text(data, "browser_path"));
            self.project_edit = Some(editor);
            return Out::eat();
        }
        if self.proj_sel == 1 {
            return Out::action(
                json!({"type":"sidebar","op":"select_project","path":text(data,"browser_path")}),
            );
        }
        let Some(row) = data
            .get("browser_rows")
            .and_then(Value::as_array)
            .and_then(|r| r.get(self.proj_sel - 2))
        else {
            return Out::eat();
        };
        if text(row, "kind") != "dir" {
            return Out::eat();
        }
        // Enter (and single click) on a subdirectory opens it as a project
        // right away instead of merely browsing into it, so adding a new
        // directory takes one step. The `..` entry still navigates upward;
        // Right-arrow drills into a folder for a look before opening it.
        if text(row, "name") == ".." {
            return Out::action(
                json!({"type":"sidebar","op":"browse_project","path":text(row,"path")}),
            );
        }
        Out::action(json!({"type":"sidebar","op":"select_project","path":text(row,"path")}))
    }
    fn projects_key(&mut self, key: KeyEvent, s: &Snapshot) -> Out {
        if !flag(&s.sidebar.projects, "browser_open") {
            let len = rows_of(&s.sidebar.projects).len() + 1;
            return match key.code {
                KeyCode::Char('a') => {
                    self.proj_sel = 0;
                    self.activate_picker(s)
                }
                KeyCode::Enter | KeyCode::Right => self.activate_picker(s),
                KeyCode::Char('r') => Out::action(act("refresh")),
                _ if move_selection(&mut self.proj_sel, len, key.code) => Out::eat(),
                _ => self.unowned(&key),
            };
        }
        if let Some(draft) = self.project_edit.as_mut() {
            match key.code {
                KeyCode::Esc => self.project_edit = None,
                KeyCode::Enter => {
                    let path = self.project_edit.take().unwrap_or_default().text;
                    return Out::action(json!({"type":"sidebar","op":"project_path","path":path}));
                }
                KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    *draft = Editor::default();
                }
                _ => {
                    if !matches!(key.code, KeyCode::Char(c) if draft.text.len() + c.len_utf8() > 4096)
                    {
                        edit(draft, key, false);
                    }
                }
            }
            return Out::eat();
        }
        let len = s
            .sidebar
            .projects
            .get("browser_rows")
            .and_then(Value::as_array)
            .map_or(2, |r| r.len() + 2);
        match key.code {
            KeyCode::Char('/') | KeyCode::Char('a') => {
                self.proj_sel = 0;
                self.activate_picker(s)
            }
            KeyCode::Enter => self.activate_picker(s),
            KeyCode::Right => {
                // Right drills into the highlighted folder for a look around
                // without opening it; Enter (or click) opens it as a project.
                if self.proj_sel >= 2 {
                    if let Some(row) = s
                        .sidebar
                        .projects
                        .get("browser_rows")
                        .and_then(Value::as_array)
                        .and_then(|rows| rows.get(self.proj_sel - 2))
                        .filter(|row| text(row, "kind") == "dir")
                    {
                        return Out::action(
                            json!({"type":"sidebar","op":"browse_project","path":text(row,"path")}),
                        );
                    }
                }
                self.activate_picker(s)
            }
            KeyCode::Left | KeyCode::Backspace => {
                let parent = s
                    .sidebar
                    .projects
                    .get("browser_rows")
                    .and_then(Value::as_array)
                    .and_then(|rows| rows.first())
                    .filter(|row| text(row, "name") == "..");
                match parent {
                    Some(row) => Out::action(
                        json!({"type":"sidebar","op":"browse_project","path":text(row,"path")}),
                    ),
                    None => Out::eat(),
                }
            }
            _ if move_selection(&mut self.proj_sel, len, key.code) => Out::eat(),
            _ => self.unowned(&key),
        }
    }

    fn files_key(&mut self, key: KeyEvent, s: &Snapshot) -> Out {
        let rows = rows_of(&s.sidebar.files);
        if move_selection(&mut self.file_sel, rows.len(), key.code) {
            return Out::eat();
        }
        match key.code {
            KeyCode::Enter | KeyCode::Right => match rows.get(self.file_sel) {
                Some(row) if key.code == KeyCode::Enter && text(row, "kind") == "file" => {
                    self.open_file(row)
                }
                Some(row) => self.open_directory(row),
                None => Out::eat(),
            },
            KeyCode::Left | KeyCode::Backspace => {
                match rows.iter().find(|row| text(row, "kind") == "up") {
                    Some(row) => self.open_directory(row),
                    None => Out::eat(),
                }
            }
            KeyCode::Char('r') if key.modifiers.is_empty() => Out::action(act("refresh")),
            _ => self.unowned(&key),
        }
    }

    /// Opens a listed folder (or the parent row); files and links do nothing.
    fn open_directory(&mut self, row: &Value) -> Out {
        if matches!(text(row, "kind"), "dir" | "up") {
            return Out::action(json!({"type":"sidebar","op":"dir","path":text(row, "path")}));
        }
        Out::eat()
    }

    /// Asks the host to show a listed file in the read-only modal (the host reads it).
    fn open_file(&mut self, row: &Value) -> Out {
        self.last_file_click = None;
        Out::action(json!({"type":"sidebar","op":"file","path":text(row, "path")}))
    }

    /// A click on a Files row: folders open at once; a file is only selected, and a second click
    /// on the same file within `DOUBLE_CLICK` previews it. Another row restarts the interval.
    fn click_file_row(&mut self, row: &Value, now: Instant) -> Out {
        if text(row, "kind") != "file" {
            self.last_file_click = None;
            return self.open_directory(row);
        }
        let path = text(row, "path");
        let double = self.last_file_click.as_ref().is_some_and(|(last, at)| {
            last == path && now.saturating_duration_since(*at) <= DOUBLE_CLICK
        });
        if double {
            return self.open_file(row);
        }
        self.last_file_click = Some((path.to_owned(), now));
        Out::eat()
    }

    fn git_key(&mut self, key: KeyEvent) -> Out {
        match key.code {
            KeyCode::Up => self.git_top = self.git_top.saturating_sub(1),
            KeyCode::Down => self.git_top += 1,
            KeyCode::PageUp => self.git_top = self.git_top.saturating_sub(10),
            KeyCode::PageDown => self.git_top += 10,
            KeyCode::Home => self.git_top = 0,
            KeyCode::End => self.git_top = usize::MAX / 2,
            KeyCode::Char('r') if key.modifiers.is_empty() => return Out::action(act("refresh")),
            _ => return self.unowned(&key),
        }
        Out::eat()
    }

    // ----- mouse ----------------------------------------------------------------------

    /// One mouse event. `area` is the sidebar column of the last frame and `available` the
    /// columns it may share with the conversation (for the drag bound).
    pub fn mouse(&mut self, ev: MouseEvent, s: &Snapshot, area: Rect, available: u16) -> Out {
        let at = Position::new(ev.column, ev.row);
        if self.dragging {
            match ev.kind {
                MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Moved => {
                    return Out {
                        consumed: true,
                        width: Some(width_at(ev.column, area, available)),
                        ..Out::default()
                    };
                }
                MouseEventKind::Up(MouseButton::Left) => {
                    self.dragging = false;
                    let width = width_at(ev.column, area, available);
                    return Out {
                        consumed: true,
                        width: Some(width),
                        action: Some(json!({"type":"sidebar","op":"width","width":width})),
                        ..Out::default()
                    };
                }
                _ => {}
            }
        }
        let hot = self.divider.contains(at);
        let changed = hot != self.divider_hot;
        self.divider_hot = hot;
        if !area.contains(at) {
            if matches!(ev.kind, MouseEventKind::Down(MouseButton::Left)) {
                self.focus = false;
            }
            if !self.dash.hovered_id.is_empty() {
                self.dash.leave();
            }
            return if changed { Out::eat() } else { Out::default() };
        }
        match ev.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                self.focus = true;
                if hot {
                    self.dragging = true;
                    return Out::eat();
                }
                if let Some((_, tab)) = self.tab_hits.iter().find(|(rect, _)| rect.contains(at)) {
                    return Out::action(tab_message(TABS[*tab]));
                }
                self.click(ev, s)
            }
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                self.wheel(matches!(ev.kind, MouseEventKind::ScrollDown), ev, s)
            }
            MouseEventKind::Moved => {
                if s.sidebar.tab == "Sessions" {
                    if let Some(dashboard) = s.sidebar.sessions.as_ref() {
                        self.dash.hover(ev.column, ev.row, dashboard);
                    }
                }
                Out::eat()
            }
            _ => Out::eat(),
        }
    }

    /// Files/Git reuse the bounded host payloads and browser interactions in the right panel.
    pub fn details_mouse(&mut self, ev: MouseEvent, s: &Snapshot, body: Rect) -> Out {
        self.divider = Rect::default();
        self.tab_hits.clear();
        self.mouse(ev, s, body, body.width)
    }

    fn list_row_at(&self, x: u16, y: u16) -> Option<usize> {
        if !self.list.contains(Position::new(x, y)) {
            return None;
        }
        self.line_items
            .get(usize::from(y - self.list.y))
            .copied()
            .flatten()
    }

    fn click(&mut self, ev: MouseEvent, s: &Snapshot) -> Out {
        match s.sidebar.tab.as_str() {
            "Projects" => {
                if let Some(index) = self.list_row_at(ev.column, ev.row) {
                    self.proj_sel = index;
                    return self.activate_picker(s);
                }
                Out::eat()
            }
            "Files" => {
                if let Some(index) = self.list_row_at(ev.column, ev.row) {
                    self.file_sel = index;
                    if let Some(row) = rows_of(&s.sidebar.files).get(index) {
                        return self.click_file_row(row, Instant::now());
                    }
                }
                Out::eat()
            }
            "Git" => Out::eat(),
            _ => match s.sidebar.sessions.as_ref() {
                Some(dashboard) => match self.dash.click(ev.column, ev.row, dashboard) {
                    Some(operation) => self.session_operation(operation),
                    None => Out::eat(),
                },
                None => Out::eat(),
            },
        }
    }

    fn wheel(&mut self, down: bool, ev: MouseEvent, s: &Snapshot) -> Out {
        match s.sidebar.tab.as_str() {
            "Projects" => {
                let len = s
                    .sidebar
                    .projects
                    .get("browser_rows")
                    .and_then(Value::as_array)
                    .map_or(2, |r| r.len() + 2);
                self.proj_sel = Self::scrolled(self.proj_sel, len, down);
            }
            "Files" => {
                self.file_sel =
                    Self::scrolled(self.file_sel, rows_of(&s.sidebar.files).len(), down);
            }
            "Git" => {
                self.git_top = if down {
                    self.git_top + 3
                } else {
                    self.git_top.saturating_sub(3)
                };
            }
            _ => {
                if let Some(dashboard) = s.sidebar.sessions.as_ref() {
                    self.dash.wheel(ev.column, ev.row, down, dashboard);
                }
            }
        }
        Out::eat()
    }

    fn scrolled(sel: usize, len: usize, down: bool) -> usize {
        if len == 0 {
            0
        } else if down {
            (sel + 3).min(len - 1)
        } else {
            sel.saturating_sub(3)
        }
    }

    // ----- drawing --------------------------------------------------------------------

    pub fn draw(
        &mut self,
        frame: &mut Frame,
        area: Rect,
        s: &Snapshot,
        p: &Palette,
        spin: usize,
        mono: bool,
    ) {
        self.divider = Rect::new(area.right().saturating_sub(1), area.y, 1, area.height);
        self.tab_hits.clear();
        self.list = Rect::default();
        self.line_items.clear();
        frame.render_widget(Clear, area);
        frame.render_widget(
            Block::default().style(Style::default().bg(p.composer)),
            area,
        );
        let inner = Rect {
            x: area.x + 2,
            y: area.y + 1,
            width: area.width.saturating_sub(4),
            height: area.height.saturating_sub(2),
        };
        if inner.width < 8 || inner.height < 6 {
            return;
        }
        let (cells, rows) = tab_cells(inner.width);
        let active = tab_index(s);
        for cell in &cells {
            let rect = Rect::new(inner.x + cell.x, inner.y + 1 + cell.row, cell.width, 1);
            let style = if cell.tab == active {
                let mut style = Style::default()
                    .fg(p.text)
                    .bg(p.element)
                    .add_modifier(Modifier::BOLD);
                if mono {
                    style = style.add_modifier(Modifier::UNDERLINED);
                }
                style
            } else {
                Style::default().fg(p.muted)
            };
            frame.render_widget(
                Paragraph::new(Span::styled(format!(" {} ", TABS[cell.tab]), style)),
                rect,
            );
            self.tab_hits.push((rect, cell.tab));
        }
        let rule_y = inner.y + 1 + rows;
        let body_y = rule_y + 1;
        let footer_y = inner.bottom().saturating_sub(1);
        frame.render_widget(
            Paragraph::new(Line::styled(
                "─".repeat(usize::from(inner.width)),
                Style::default().fg(p.border),
            )),
            Rect::new(inner.x, rule_y, inner.width, 1),
        );
        let hint = self.hint(s);
        frame.render_widget(
            Paragraph::new(Line::styled(
                fit(hint, usize::from(inner.width)),
                Style::default().fg(p.quiet),
            )),
            Rect::new(inner.x, footer_y, inner.width, 1),
        );
        if footer_y <= body_y {
            return;
        }
        let body = Rect::new(inner.x, body_y, inner.width, footer_y - body_y);
        match s.sidebar.tab.as_str() {
            "Projects" => self.draw_projects(frame, body, s, p, spin, mono),
            "Files" => self.draw_files(frame, body, s, p, mono),
            "Git" => self.draw_git(frame, body, s, p),
            _ => self.draw_sessions(frame, body, s, p, spin, mono),
        }
    }

    fn hint(&self, s: &Snapshot) -> &'static str {
        if !self.focus {
            return "Click to focus · Ctrl+B hides";
        }
        match s.sidebar.tab.as_str() {
            "Projects" if !flag(&s.sidebar.projects, "browser_open") => {
                "↑↓ move · Enter open · a Add project · Esc close"
            }
            "Projects" => {
                "/ edit path · Enter open · → drill in · ← parent · Open folder starts session"
            }
            "Files" => "↑↓ move · Enter open/view · ← up · r refresh · Esc back",
            "Git" => "↑↓ scroll · r refresh · Tab tab · Esc back",
            _ => "↑↓ move · Enter open · / filter · d archive · Esc back",
        }
    }

    fn note(frame: &mut Frame, body: Rect, message: &str, style: Style) {
        frame.render_widget(
            Paragraph::new(Line::styled(fit(message, usize::from(body.width)), style)),
            Rect::new(body.x, body.y, body.width, 1),
        );
    }

    /// Scrolls `lines` so the selected line shows, paints the window and records the rows.
    fn paint_list(
        &mut self,
        frame: &mut Frame,
        body: Rect,
        lines: Vec<(Line<'static>, Option<usize>)>,
        selected: Option<usize>,
        top: usize,
    ) -> usize {
        let height = usize::from(body.height).max(1);
        let mut top = top.min(lines.len().saturating_sub(height));
        if let Some(line) = selected {
            if line < top {
                top = line;
            } else if line >= top + height {
                top = line + 1 - height;
            }
        }
        let visible: Vec<_> = lines.into_iter().skip(top).take(height).collect();
        self.list = body;
        self.line_items = visible.iter().map(|(_, item)| *item).collect();
        frame.render_widget(
            Paragraph::new(
                visible
                    .into_iter()
                    .map(|(line, _)| line)
                    .collect::<Vec<_>>(),
            ),
            body,
        );
        top
    }

    fn draw_projects(
        &mut self,
        frame: &mut Frame,
        body: Rect,
        s: &Snapshot,
        p: &Palette,
        _spin: usize,
        mono: bool,
    ) {
        let data = &s.sidebar.projects;
        let quiet = Style::default().fg(p.quiet);
        let picking = flag(data, "browser_open");
        let body = if picking {
            frame.render_widget(Clear, body);
            let block = Block::bordered().title(" Add project · Esc cancel ");
            let inner = block.inner(body);
            frame.render_widget(block, body);
            inner
        } else {
            body
        };
        let mut labels = vec![
            match self.project_edit.as_ref() {
                Some(editor) => format!(
                    "Path: {}▏{}",
                    &editor.text[..editor.cursor],
                    &editor.text[editor.cursor..]
                ),
                None => format!("Path: {}", text(data, "browser_path")),
            },
            "Open this folder · new session".to_owned(),
        ];
        if let Some(rows) = data.get("browser_rows").and_then(Value::as_array) {
            labels.extend(rows.iter().map(|r| {
                format!(
                    "{}{}",
                    text(r, "name"),
                    if text(r, "kind") == "dir" {
                        " /"
                    } else {
                        " · file (not selectable)"
                    }
                )
            }));
        }
        if !picking {
            labels = vec!["+ Add project".to_owned()];
            labels.extend(rows_of(data).iter().map(|row| {
                format!(
                    "{}{}{}",
                    "  ".repeat(row.get("depth").and_then(Value::as_u64).unwrap_or(0) as usize),
                    if flag(row, "current") { "● " } else { "" },
                    text(row, "label")
                )
            }));
        }
        self.proj_sel = self.proj_sel.min(labels.len().saturating_sub(1));
        let mut lines = Vec::new();
        let mut selected = None;
        for (i, label) in labels.iter().enumerate() {
            let active = i == self.proj_sel;
            if active {
                selected = Some(lines.len());
            }
            lines.push((
                row_line(
                    vec![Span::styled(if active { "› " } else { "  " }, quiet)],
                    label,
                    Style::default().fg(p.text),
                    "",
                    quiet,
                    usize::from(body.width),
                    if active { Some(p.element) } else { None },
                    mono && active,
                ),
                Some(i),
            ));
        }
        if picking && !text(data, "browser_error").is_empty() {
            lines.push((
                Line::styled(
                    fit(text(data, "browser_error"), usize::from(body.width)),
                    Style::default().fg(p.error),
                ),
                None,
            ));
        }
        if picking && flag(data, "browser_truncated") {
            lines.push((Line::styled("… more entries not shown", quiet), None));
        }
        self.proj_top = self.paint_list(frame, body, lines, selected, self.proj_top);
    }

    pub fn draw_files(
        &mut self,
        frame: &mut Frame,
        body: Rect,
        s: &Snapshot,
        p: &Palette,
        mono: bool,
    ) {
        let data = &s.sidebar.files;
        let quiet = Style::default().fg(p.quiet);
        let rows = rows_of(data);
        let error = text(data, "error");
        if rows.is_empty() && !flag(data, "loaded") {
            Self::note(frame, body, "Loading files…", quiet);
            return;
        }
        if text(data, "path") != self.file_path {
            self.file_path = text(data, "path").to_owned();
            self.file_sel = 0;
            self.file_top = 0;
            self.last_file_click = None;
        }
        let width = usize::from(body.width);
        let glyphs = nexus_widgets::Glyphs::from_env();
        let mut lines: Vec<(Line<'static>, Option<usize>)> = vec![(
            Line::styled(
                fit(text(data, "label"), width),
                Style::default().fg(p.muted),
            ),
            None,
        )];
        if !error.is_empty() {
            lines.push((
                Line::styled(
                    fit(&format!("Error: {error}"), width),
                    Style::default().fg(p.error),
                ),
                None,
            ));
        }
        if rows.is_empty() && error.is_empty() {
            lines.push((Line::styled("Empty folder", quiet), None));
        }
        self.file_sel = self.file_sel.min(rows.len().saturating_sub(1));
        let mut selected_line = None;
        for (index, row) in rows.iter().enumerate() {
            let is_selected = index == self.file_sel;
            let (marker, main_style) = match text(row, "kind") {
                "up" => ("↑ ".to_owned(), Style::default().fg(p.muted)),
                "dir" => (format!("{} ", glyphs.closed), Style::default().fg(p.text)),
                "link" => ("↪ ".to_owned(), quiet),
                _ => ("  ".to_owned(), Style::default().fg(p.muted)),
            };
            let name = if text(row, "kind") == "dir" {
                format!("{}/", text(row, "name"))
            } else {
                text(row, "name").to_owned()
            };
            let bg = if is_selected { Some(p.element) } else { None };
            if is_selected {
                selected_line = Some(lines.len());
            }
            lines.push((
                row_line(
                    vec![Span::styled(marker, Style::default().fg(p.accent))],
                    &name,
                    main_style,
                    "",
                    quiet,
                    width,
                    bg,
                    mono && is_selected,
                ),
                Some(index),
            ));
        }
        if flag(data, "truncated") {
            lines.push((Line::styled("… more entries not shown", quiet), None));
        }
        let top = self.paint_list(frame, body, lines, selected_line, self.file_top);
        self.file_top = top;
    }

    pub fn draw_git(&mut self, frame: &mut Frame, body: Rect, s: &Snapshot, p: &Palette) {
        let data = &s.sidebar.git;
        let quiet = Style::default().fg(p.quiet);
        if !flag(data, "loaded") {
            Self::note(frame, body, "Loading status…", quiet);
            return;
        }
        let width = usize::from(body.width);
        let mut lines: Vec<Line<'static>> = Vec::new();
        let error = text(data, "error");
        if !error.is_empty() {
            lines.push(Line::styled(
                fit(&format!("Error: {error}"), width),
                Style::default().fg(p.error),
            ));
        }
        if !flag(data, "available") {
            let message = text(data, "message");
            lines.push(Line::styled(
                fit(
                    if message.is_empty() {
                        "Git status is unavailable"
                    } else {
                        message
                    },
                    width,
                ),
                Style::default().fg(p.muted),
            ));
        } else {
            lines.push(Line::styled(
                fit(text(data, "summary"), width),
                Style::default().fg(p.text),
            ));
            if rows_of(data).is_empty() {
                lines.push(Line::styled(fit(text(data, "message"), width), quiet));
            }
            lines.push(Line::raw(""));
            for row in rows_of(data) {
                if text(row, "kind") == "heading" {
                    lines.push(Line::styled(
                        fit(text(row, "text"), width),
                        Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
                    ));
                    continue;
                }
                let tone = match text(row, "code") {
                    "A" => p.success,
                    "M" | "T" | "R" | "C" => p.warning,
                    "D" | "U" => p.error,
                    _ => p.quiet,
                };
                let label = format!("{:<10}", text(row, "label"));
                let room = width.saturating_sub(label.width() + 1);
                lines.push(Line::from(vec![
                    Span::styled(label, Style::default().fg(tone)),
                    Span::raw(" "),
                    Span::styled(
                        fit(
                            match text(row, "name") {
                                "" => text(row, "path"),
                                name => name,
                            },
                            room,
                        ),
                        Style::default().fg(p.text),
                    ),
                ]));
            }
            if flag(data, "truncated") {
                lines.push(Line::styled("… more changes not shown", quiet));
            }
        }
        let height = usize::from(body.height).max(1);
        self.git_top = self.git_top.min(lines.len().saturating_sub(height));
        let visible: Vec<_> = lines.into_iter().skip(self.git_top).take(height).collect();
        frame.render_widget(Paragraph::new(visible), body);
    }

    fn draw_sessions(
        &mut self,
        frame: &mut Frame,
        body: Rect,
        s: &Snapshot,
        p: &Palette,
        spin: usize,
        mono: bool,
    ) {
        let quiet = Style::default().fg(p.quiet);
        let muted = Style::default().fg(p.muted);
        let dash = &mut self.dash;
        dash.list_area = Rect::default();
        dash.line_rows.clear();
        dash.line_headings.clear();
        dash.search_hit = Rect::default();
        dash.new_hit = Rect::default();
        dash.archive_hit = Rect::default();
        dash.worktree_hit = Rect::default();
        dash.choose_hit = Rect::default();
        dash.manager_hit = Rect::default();
        dash.manager_live_hit = Rect::default();
        let Some(dashboard) = s.sidebar.sessions.as_ref() else {
            Self::note(frame, body, "Loading sessions…", quiet);
            return;
        };
        if body.height < 4 {
            return;
        }
        dash.sync(dashboard);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64());
        let width = usize::from(body.width);
        let shown = dash.filtered(rows_of(dashboard));
        let selected_title = shown
            .iter()
            .find(|row| text(row, "id") == dash.selected_id)
            .map_or("", |row| text(row, "title"));
        let footer = if let Some(draft) = &dash.rename {
            Some(Line::from(vec![
                Span::styled("Rename: ", Style::default().fg(p.accent)),
                Span::raw(draft.clone()),
                Span::styled("▏", Style::default().fg(p.accent)),
            ]))
        } else if dash.confirm_archive.is_some() {
            Some(Line::styled(
                fit(
                    &format!("Archive “{}”? y/Enter confirms", fit(selected_title, 20)),
                    width,
                ),
                Style::default().fg(p.accent),
            ))
        } else if !dash.notice.is_empty() {
            Some(Line::styled(
                fit(&dash.notice, width),
                Style::default().fg(p.accent),
            ))
        } else {
            dashboard
                .get("error")
                .and_then(Value::as_str)
                .filter(|e| !e.is_empty())
                .map(|e| Line::styled(fit(&format!("Error: {e}"), width), quiet))
        };

        let search = if dash.searching {
            Line::from(vec![
                Span::styled("/ ", Style::default().fg(p.accent)),
                Span::raw(dash.query.clone()),
                Span::styled("▏", Style::default().fg(p.accent)),
            ])
        } else if !dash.query.is_empty() {
            Line::from(vec![
                Span::styled("/ ", quiet),
                Span::raw(fit(&dash.query, width.saturating_sub(14))),
                Span::styled(format!("  ({}) Esc", shown.len()), quiet),
            ])
        } else {
            Line::styled("/ Search sessions", quiet)
        };
        let search_rect = Rect::new(body.x, body.y, body.width, 1);
        frame.render_widget(Paragraph::new(search), search_rect);
        dash.search_hit = search_rect;
        // Keep the former New Agent row as blank spacing, without an invisible action.
        let footer_rows = u16::from(footer.is_some());
        let list_top_y = body.y + 2;
        let list_bottom = body.bottom().saturating_sub(footer_rows);
        if list_bottom <= list_top_y {
            return;
        }
        let list = Rect::new(body.x, list_top_y, body.width, list_bottom - list_top_y);

        type RowLine = (Line<'static>, Option<usize>, Option<String>);
        let mut lines: Vec<RowLine> = Vec::new();
        let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
        for (index, row) in shown.iter().enumerate() {
            let key = dash.group_key(row);
            if groups.last().is_none_or(|(name, _)| name != &key) {
                groups.push((key, Vec::new()));
            }
            groups.last_mut().unwrap().1.push(index);
        }
        let glyphs = nexus_widgets::Glyphs::from_env();
        for (key, members) in &groups {
            let collapsed = dash.is_collapsed(key);
            let chevron = if collapsed {
                glyphs.closed
            } else {
                glyphs.open
            };
            lines.push((
                Line::from(vec![
                    Span::styled(format!("{chevron} {key}"), muted),
                    Span::styled(format!(" {}", members.len()), quiet),
                ]),
                None,
                Some(key.clone()),
            ));
            if collapsed {
                continue;
            }
            for index in members {
                let row = shown[*index];
                let id = text(row, "id");
                let is_selected = id == dash.selected_id;
                let is_hover = !is_selected && id == dash.hovered_id;
                let bg = if is_selected {
                    Some(p.element)
                } else if is_hover {
                    Some(p.element_hi)
                } else {
                    None
                };
                let renaming = is_selected.then(|| dash.rename.clone()).flatten();
                let title = match &renaming {
                    Some(draft) => format!("{draft}▏"),
                    None => text(row, "title").to_owned(),
                };
                let done = matches!(text(row, "status"), "done" | "archived");
                let glyph = if done {
                    Span::styled(
                        if glyphs.ascii { "*" } else { "●" },
                        Style::default().fg(p.muted),
                    )
                } else {
                    mark(session_rank(row), spin, p)
                };
                let age = {
                    let age = text(row, "age");
                    if age.is_empty() {
                        text(row, "sub")
                    } else {
                        age
                    }
                };
                let children = row
                    .get("running_children")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                let right = if children > 0 {
                    format!("{children} running · {age}")
                } else {
                    age.to_owned()
                };
                let style = if done {
                    Style::default().fg(p.muted)
                } else {
                    Style::default().fg(p.text)
                };
                lines.push((
                    row_line(
                        vec![Span::raw(" "), glyph, Span::raw(" ")],
                        &title,
                        style,
                        &right,
                        quiet,
                        width,
                        bg,
                        mono && is_selected,
                    ),
                    Some(*index),
                    None,
                ));
                if let Some(request) = picker::pending(row).filter(|_| picker::section(row) == 0) {
                    let wait = format!(
                        "waiting {}",
                        picker::clock(picker::waited(
                            request.get("ts").and_then(Value::as_f64).unwrap_or(0.0),
                            now
                        ))
                    );
                    lines.push((
                        row_line(
                            vec![Span::raw("   ")],
                            text(request, "summary"),
                            Style::default().fg(p.text),
                            &wait,
                            quiet,
                            width,
                            bg,
                            false,
                        ),
                        Some(*index),
                        None,
                    ));
                    lines.push((
                        row_line(
                            vec![Span::raw("   ")],
                            "Enter open  a allow once  d deny",
                            quiet,
                            "",
                            quiet,
                            width,
                            bg,
                            false,
                        ),
                        Some(*index),
                        None,
                    ));
                }
            }
        }
        if lines.is_empty() {
            let empty = if dash.query.is_empty() {
                "No sessions yet · n starts one"
            } else {
                "No session matches the filter"
            };
            lines.push((Line::styled(empty, muted), None, None));
        }

        let height = usize::from(list.height).max(1);
        let selected_line = lines
            .iter()
            .position(|(_, index, _)| {
                index.is_some_and(|i| {
                    shown
                        .get(i)
                        .is_some_and(|row| text(row, "id") == dash.selected_id)
                })
            })
            .unwrap_or(0);
        let mut selected_end = selected_line;
        if lines
            .get(selected_line)
            .and_then(|(_, index, _)| *index)
            .is_some()
        {
            while selected_end + 1 < lines.len()
                && lines[selected_end + 1].1 == lines[selected_line].1
            {
                selected_end += 1;
            }
        }
        let wanted = if selected_line > 0 && lines[selected_line - 1].2.is_some() {
            selected_line - 1
        } else {
            selected_line
        };
        if wanted < dash.list_top {
            dash.list_top = wanted;
        } else if selected_end >= dash.list_top + height {
            dash.list_top = selected_end + 1 - height;
        }
        dash.list_top = dash.list_top.min(lines.len().saturating_sub(height));
        let visible: Vec<_> = lines.into_iter().skip(dash.list_top).take(height).collect();
        dash.line_rows = visible.iter().map(|(_, index, _)| *index).collect();
        dash.line_headings = visible
            .iter()
            .map(|(_, _, heading)| heading.clone())
            .collect();
        dash.list_area = list;
        frame.render_widget(
            Paragraph::new(
                visible
                    .into_iter()
                    .map(|(line, _, _)| line)
                    .collect::<Vec<_>>(),
            ),
            list,
        );
        if let Some(line) = footer {
            frame.render_widget(
                Paragraph::new(line),
                Rect::new(body.x, body.bottom() - 1, body.width, 1),
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bridge::Sidebar;
    use crossterm::event::KeyEventKind;
    use ratatui::{backend::TestBackend, Terminal};

    fn open(tab: &str, width: u16) -> Snapshot {
        Snapshot {
            sidebar: Sidebar {
                available: true,
                open: true,
                tab: tab.into(),
                width,
                ..Sidebar::default()
            },
            ..Snapshot::default()
        }
    }

    fn key(code: KeyCode) -> KeyEvent {
        let mut event = KeyEvent::new(code, KeyModifiers::NONE);
        event.kind = KeyEventKind::Press;
        event
    }

    fn mouse(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    fn screen(ui: &mut SidebarUi, s: &Snapshot, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| {
                let area = frame.area();
                ui.draw(frame, area, s, &Palette::new(false), 0, false);
            })
            .unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn width_is_bounded_and_yields_to_the_conversation() {
        let mut s = open("Sessions", 34);
        assert_eq!(width_for(&s, 200), 34);
        s.sidebar.width = 5;
        assert_eq!(
            width_for(&s, 200),
            MIN_WIDTH,
            "a stored width below the minimum is raised"
        );
        s.sidebar.width = 500;
        assert_eq!(
            width_for(&s, 200),
            MAX_WIDTH,
            "and one above the maximum is lowered"
        );
        s.sidebar.width = 0;
        assert_eq!(
            width_for(&s, 200),
            DEFAULT_WIDTH,
            "no stored width uses the default"
        );
        for available in 0..=200u16 {
            s.sidebar.width = 60;
            let width = width_for(&s, available);
            assert!(
                width == 0 || available - width >= MIN_CONVERSATION,
                "{available}: sidebar {width}"
            );
            assert!(width == 0 || width >= MIN_WIDTH);
        }
        assert_eq!(
            width_for(&s, MIN_CONVERSATION + MIN_WIDTH - 1),
            0,
            "no room: hidden"
        );
        assert_eq!(width_for(&s, MIN_CONVERSATION + MIN_WIDTH), MIN_WIDTH);
        s.sidebar.open = false;
        assert_eq!(width_for(&s, 200), 0, "closed takes no columns");
    }

    #[test]
    fn dragging_is_bounded_by_the_room_left_for_the_conversation() {
        let area = Rect::new(0, 0, 34, 30);
        assert_eq!(
            width_at(33, area, 200),
            34,
            "the divider column is the last one"
        );
        assert_eq!(width_at(0, area, 200), MIN_WIDTH);
        assert_eq!(width_at(150, area, 200), MAX_WIDTH);
        assert_eq!(width_at(150, area, 100), 100 - MIN_CONVERSATION);
    }

    #[test]
    fn projects_tab_a_key_asks_the_host_to_add_a_project_by_path() {
        let s = projects_snapshot();
        let mut ui = SidebarUi::default();
        assert!(screen(&mut ui, &s, 60, 24).contains("Add project"));
        assert_eq!(
            ui.key(key(KeyCode::Char('a')), &s, true).action,
            Some(act("add_project"))
        );
        assert!(ui.project_edit.is_none());
        assert_eq!(
            ui.key(key(KeyCode::Enter), &s, true).action,
            Some(act("add_project")),
            "the selected Add project row must activate with Enter"
        );
        let y = ui
            .line_items
            .iter()
            .position(|item| *item == Some(0))
            .unwrap();
        assert_eq!(
            ui.click(
                MouseEvent {
                    kind: MouseEventKind::Down(MouseButton::Left),
                    column: ui.list.x,
                    row: ui.list.y + y as u16,
                    modifiers: KeyModifiers::NONE,
                },
                &s
            )
            .action,
            Some(act("add_project")),
            "mouse and keyboard use the same Add action"
        );
        ui.proj_sel = 1;
        assert_eq!(
            ui.key(key(KeyCode::Enter), &s, true).action,
            Some(json!({"type":"sidebar","op":"open_project","workspace":"/repo"}))
        );
    }

    #[test]
    fn host_reveal_shows_a_sidebar_hidden_after_a_session_open() {
        let s = open("Sessions", 34);
        let mut ui = SidebarUi::default();
        ui.session_operation(json!({"kind":"session_open","id":"a","workspace":"/repo"}));
        assert!(
            !ui.is_open(&s),
            "opening a session hides the sidebar locally"
        );
        ui.take_host_focus(&s, &s);
        assert!(!ui.is_open(&s), "an unchanged snapshot keeps it hidden");
        let mut next = open("Sessions", 34);
        next.sidebar.reveal = 1; // `/sessions` while the host still thinks it is open
        ui.take_host_focus(&s, &next);
        assert!(ui.is_open(&next));
    }

    #[test]
    fn project_path_uses_text_editor_and_owns_paste() {
        let mut s = open("Projects", 34);
        s.sidebar.projects = json!({"browser_open":true,"browser_path":"/repo"});
        let mut ui = SidebarUi::default();
        ui.focus = true;
        ui.key(key(KeyCode::Char('/')), &s, true);
        ui.key(key(KeyCode::Home), &s, true);
        ui.key(key(KeyCode::Delete), &s, true);
        assert!(ui.paste("/tmp/", &s));
        ui.key(key(KeyCode::End), &s, true);
        ui.key(key(KeyCode::Left), &s, true);
        ui.key(key(KeyCode::Backspace), &s, true);
        let out = ui.key(key(KeyCode::Enter), &s, true);
        assert_eq!(
            out.action,
            Some(json!({"type":"sidebar","op":"project_path","path":"/tmp/reo"}))
        );
        assert!(ui.paste("must not reach composer", &s));
        assert!(ui.project_edit.is_none());
        ui.focus = false;
        assert!(!ui.paste("composer", &s));
    }

    #[test]
    fn project_parent_at_filesystem_root_does_not_enter_first_child() {
        let mut s = open("Projects", 34);
        s.sidebar.projects = json!({"browser_open":true,"browser_path":"/","browser_rows":[
            {"name":"child","kind":"dir","path":"/child"}
        ]});
        let mut ui = SidebarUi::default();
        ui.focus = true;
        assert!(ui.key(key(KeyCode::Left), &s, true).action.is_none());
        assert!(ui.key(key(KeyCode::Backspace), &s, true).action.is_none());
    }

    #[test]
    fn tab_labels_wrap_to_fit_and_never_overlap() {
        for inner in 10..=60u16 {
            let (cells, rows) = tab_cells(inner);
            assert_eq!(cells.len(), TABS.len());
            assert_eq!(rows, cells.iter().map(|c| c.row).max().unwrap() + 1);
            for cell in &cells {
                assert!(
                    cell.x + cell.width <= inner,
                    "{inner}: {cell:?} past the edge"
                );
                assert_eq!(
                    cell.width,
                    TABS[cell.tab].width() as u16 + 2,
                    "{inner}: label clipped"
                );
            }
            for a in &cells {
                for b in &cells {
                    if a.tab < b.tab && a.row == b.row {
                        assert!(a.x + a.width < b.x, "{inner}: {a:?} touches {b:?}");
                    }
                }
            }
        }
        assert_eq!(tab_cells(60).1, 1, "wide sidebars keep one row");
        assert!(tab_cells(12).1 >= 2, "a narrow one wraps");
    }

    #[test]
    fn painted_tabs_and_hit_cells_agree() {
        let s = open("Projects", 28);
        let mut ui = SidebarUi::default();
        let text = screen(&mut ui, &s, 28, 24);
        let (cells, _) = tab_cells(24);
        for cell in &cells {
            let rect = ui.tab_hits[cell.tab].0;
            assert_eq!((rect.x, rect.width), (2 + cell.x, cell.width));
            assert_eq!(usize::from(rect.y), 2 + usize::from(cell.row));
            let row: Vec<char> = text
                .lines()
                .nth(usize::from(rect.y))
                .unwrap()
                .chars()
                .collect();
            let drawn: String = row[usize::from(rect.x)..usize::from(rect.right())]
                .iter()
                .collect();
            assert_eq!(drawn, format!(" {} ", TABS[cell.tab]));
        }
    }

    #[test]
    fn opening_the_selector_takes_requested_keyboard_focus() {
        let closed = Snapshot::default();
        let mut opened = open("Sessions", 34);
        opened.sidebar.focus = 1;
        let mut ui = SidebarUi::default();
        ui.take_host_focus(&closed, &opened);
        assert!(ui.focus, "the modal takes focus when opened");
        let mut asked = open("Sessions", 34);
        asked.sidebar.focus = 2;
        ui.take_host_focus(&opened, &asked);
        assert!(
            ui.focus,
            "an explicit request to the open sidebar still focuses it"
        );
    }

    #[test]
    fn clicking_a_tab_label_asks_for_that_tab() {
        let s = open("Projects", 34);
        let mut ui = SidebarUi::default();
        screen(&mut ui, &s, 34, 24);
        let area = Rect::new(0, 0, 34, 24);
        for index in 0..TABS.len() {
            let rect = ui.tab_hits[index].0;
            let out = ui.mouse(
                mouse(MouseEventKind::Down(MouseButton::Left), rect.x + 1, rect.y),
                &s,
                area,
                200,
            );
            assert!(out.consumed);
            assert_eq!(out.action, Some(tab_message(TABS[index])));
            assert!(ui.focus, "a click focuses the sidebar");
        }
    }

    #[test]
    fn dragging_the_divider_resizes_within_bounds_and_saves_on_release() {
        let s = open("Sessions", 34);
        let mut ui = SidebarUi::default();
        screen(&mut ui, &s, 34, 24);
        let area = Rect::new(0, 0, 34, 24);
        let down = ui.mouse(
            mouse(MouseEventKind::Down(MouseButton::Left), 33, 5),
            &s,
            area,
            200,
        );
        assert!(down.consumed && ui.dragging && down.action.is_none());
        let drag = ui.mouse(
            mouse(MouseEventKind::Drag(MouseButton::Left), 80, 9),
            &s,
            area,
            200,
        );
        assert_eq!(drag.width, Some(MAX_WIDTH));
        let drag = ui.mouse(
            mouse(MouseEventKind::Drag(MouseButton::Left), 2, 9),
            &s,
            area,
            200,
        );
        assert_eq!(drag.width, Some(MIN_WIDTH));
        let up = ui.mouse(
            mouse(MouseEventKind::Up(MouseButton::Left), 40, 9),
            &s,
            area,
            200,
        );
        assert!(!ui.dragging);
        assert_eq!(
            up.action,
            Some(json!({"type":"sidebar","op":"width","width":41}))
        );
        let narrow = ui.mouse(
            mouse(MouseEventKind::Down(MouseButton::Left), 33, 5),
            &s,
            area,
            90,
        );
        assert!(narrow.consumed);
        let up = ui.mouse(
            mouse(MouseEventKind::Up(MouseButton::Left), 70, 5),
            &s,
            area,
            90,
        );
        assert_eq!(up.width, Some(90 - MIN_CONVERSATION));
    }

    #[test]
    fn a_click_outside_drops_focus_and_is_not_consumed() {
        let s = open("Sessions", 34);
        let mut ui = SidebarUi::default();
        screen(&mut ui, &s, 34, 24);
        ui.focus = true;
        let out = ui.mouse(
            mouse(MouseEventKind::Down(MouseButton::Left), 60, 5),
            &s,
            Rect::new(0, 0, 34, 24),
            200,
        );
        assert!(!out.consumed && !ui.focus);
    }

    #[test]
    fn keys_switch_tabs_and_escape_leaves() {
        let s = open("Projects", 34);
        let mut ui = SidebarUi::default();
        ui.focus = true;
        assert_eq!(
            ui.key(key(KeyCode::Tab), &s, true).action,
            Some(tab_message("Sessions"))
        );
        assert_eq!(
            ui.key(key(KeyCode::BackTab), &s, true).action,
            Some(tab_message("Sessions")),
            "Shift+Tab wraps backwards"
        );
        let esc = ui.key(key(KeyCode::Esc), &s, true);
        assert!(esc.consumed && !ui.focus);
        let ctrl_b = ui.key(
            KeyEvent::new(KeyCode::Char('b'), KeyModifiers::CONTROL),
            &s,
            true,
        );
        assert_eq!(ctrl_b.action, Some(act("toggle")));
    }

    #[test]
    fn typing_in_a_list_tab_returns_focus_to_the_composer() {
        let s = open("Git", 34);
        let mut ui = SidebarUi::default();
        ui.focus = true;
        let out = ui.key(key(KeyCode::Char('x')), &s, true);
        assert!(!out.consumed, "the composer gets the character");
        assert!(!ui.focus);
    }

    fn sessions_snapshot() -> Snapshot {
        let mut s = open("Sessions", 34);
        s.sidebar.sessions = Some(json!({"rows":[
            {"id":"a","title":"Alpha","workspace":"/w","status":"idle","age":"2m","updated":2.0},
            {"id":"b","title":"Beta","workspace":"/w","status":"working","age":"now","updated":3.0},
        ],"selected":"a","query":""}));
        s
    }

    #[test]
    fn sessions_tab_filters_selects_and_opens_through_the_dashboard() {
        let s = sessions_snapshot();
        let mut ui = SidebarUi::default();
        ui.focus = true;
        let text = screen(&mut ui, &s, 34, 24);
        assert!(
            text.contains("Alpha") && text.contains("Beta") && text.contains("Search sessions")
        );
        let out = ui.key(key(KeyCode::Char('/')), &s, true);
        assert!(out.consumed && ui.dash.searching, "/ starts the filter");
        let out = ui.key(key(KeyCode::Char('b')), &s, true);
        assert_eq!(
            out.operation,
            Some(json!({"kind":"sessions_search","query":"b"}))
        );
        ui.key(key(KeyCode::Esc), &s, true);
        assert!(!ui.dash.searching);
        let open = ui.key(key(KeyCode::Enter), &s, true);
        assert_eq!(
            open.operation,
            Some(json!({"kind":"session_open","id":"a","workspace":"/w"}))
        );
        assert!(
            !ui.key(key(KeyCode::Down), &s, false).consumed,
            "a draft in the composer keeps its arrow keys"
        );
    }

    #[test]
    fn sessions_rows_are_clickable_where_they_are_drawn() {
        let s = sessions_snapshot();
        let mut ui = SidebarUi::default();
        let text = screen(&mut ui, &s, 34, 24);
        let row = text.lines().position(|line| line.contains("Beta")).unwrap() as u16;
        let area = Rect::new(0, 0, 34, 24);
        let first = ui.mouse(
            mouse(MouseEventKind::Down(MouseButton::Left), 5, row),
            &s,
            area,
            200,
        );
        assert!(first.consumed && first.operation.is_none());
        assert_eq!(ui.dash.selected_id, "b");
        assert!(!text.contains("+ New Agent"));
        assert_eq!(ui.dash.new_hit, Rect::default());
        let blank_y = ui.dash.search_hit.y + 1;
        assert!(text
            .lines()
            .nth(usize::from(blank_y))
            .unwrap()
            .trim()
            .is_empty());
        assert_eq!(ui.dash.list_area.y, blank_y + 1);
        let blank = ui.mouse(
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                ui.dash.search_hit.x + 1,
                blank_y,
            ),
            &s,
            area,
            200,
        );
        assert!(blank.operation.is_none());
        assert_eq!(ui.dash.selected_id, "b");
    }

    fn projects_snapshot() -> Snapshot {
        let mut s = open("Projects", 34);
        s.sidebar.projects = json!({"loaded":true,"rows":[
            {"kind":"project","key":"/repo","depth":0,"label":"repo","count":3,"age":"5m","rank":1,"expandable":true,"current":true,"workspace":"/repo"},
            {"kind":"checkout","key":"/repo|/repo","parent":"/repo","depth":1,"label":"main","count":2,"age":"5m","rank":1,"workspace":"/repo","session":"s1"},
            {"kind":"checkout","key":"/repo|/wt","parent":"/repo","depth":1,"label":"feature-x","count":1,"age":"1h","rank":2,"workspace":"/wt","session":"s2"},
            {"kind":"project","key":"/solo","depth":0,"label":"solo","count":1,"age":"2d","rank":2,"expandable":false,"workspace":"/solo","session":"s3"},
        ]});
        s
    }

    #[test]
    fn project_picker_escape_returns_to_list_and_path_escape_only_cancels_edit() {
        let mut s = projects_snapshot();
        s.sidebar.projects = json!({"browser_open":true,"browser_path":"/repo","browser_rows":[]});
        let mut ui = SidebarUi::default();
        ui.key(key(KeyCode::Char('/')), &s, true);
        assert!(ui.project_edit.is_some());
        assert!(ui.key(key(KeyCode::Esc), &s, true).action.is_none());
        assert!(ui.project_edit.is_none());
        assert_eq!(
            ui.key(key(KeyCode::Esc), &s, true).action,
            Some(act("cancel_project"))
        );
    }

    #[test]
    fn projects_browser_layout_and_navigation() {
        let mut s = projects_snapshot();
        s.sidebar.projects = json!({"browser_open":true,"browser_path":"/repo","browser_rows":[
            {"name":"..","kind":"dir","path":"/"},
            {"name":"src","kind":"dir","path":"/repo/src"},
            {"name":"main.py","kind":"file","path":"/repo/main.py"}
        ]});
        let mut ui = SidebarUi::default();
        let text = screen(&mut ui, &s, 60, 24);
        assert!(text.contains("Path: /repo") && text.contains("main.py"));
        ui.proj_sel = 3;
        assert_eq!(
            ui.key(key(KeyCode::Enter), &s, true).action,
            Some(json!({"type":"sidebar","op":"select_project","path":"/repo/src"})),
            "Enter on a directory row opens it as a project directly"
        );
        assert_eq!(
            ui.key(key(KeyCode::Right), &s, true).action,
            Some(json!({"type":"sidebar","op":"browse_project","path":"/repo/src"})),
            "Right still drills into a directory for a look before opening it"
        );
        ui.proj_sel = 2;
        assert_eq!(
            ui.key(key(KeyCode::Enter), &s, true).action,
            Some(json!({"type":"sidebar","op":"browse_project","path":"/"})),
            "Enter on the `..` row navigates up instead of opening the parent"
        );
        ui.proj_sel = 4;
        assert!(ui.key(key(KeyCode::Enter), &s, true).action.is_none());
        assert_eq!(
            ui.key(key(KeyCode::Left), &s, true).action,
            Some(json!({"type":"sidebar","op":"browse_project","path":"/"}))
        );
        ui.proj_sel = 1;
        assert_eq!(
            ui.key(key(KeyCode::Enter), &s, true).action,
            Some(json!({"type":"sidebar","op":"select_project","path":"/repo"}))
        );
    }

    #[test]
    fn files_navigate_through_listed_folders_only() {
        let mut s = open("Files", 34);
        s.sidebar.files = json!({"loaded":true,"path":"src","label":"repo/src","rows":[
            {"name":"..","path":"","kind":"up","size":""},
            {"name":"core","path":"src/core","kind":"dir","size":""},
            {"name":"main.rs","path":"src/main.rs","kind":"file","size":"2K"},
            {"name":"link","path":"src/link","kind":"link","size":""},
        ]});
        let mut ui = SidebarUi::default();
        ui.focus = true;
        let text = screen(&mut ui, &s, 34, 24);
        assert!(text.contains("repo/src") && text.contains("core/") && text.contains("main.rs"));
        assert!(!text.contains("2K"), "file size column is omitted: {text}");
        ui.key(key(KeyCode::Down), &s, true);
        assert_eq!(
            ui.key(key(KeyCode::Enter), &s, true).action,
            Some(json!({"type":"sidebar","op":"dir","path":"src/core"}))
        );
        ui.key(key(KeyCode::Down), &s, true);
        assert_eq!(
            ui.key(key(KeyCode::Enter), &s, true).action,
            Some(json!({"type":"sidebar","op":"file","path":"src/main.rs"})),
            "Enter on a file asks the host for its preview, never a folder change"
        );
        ui.key(key(KeyCode::Down), &s, true);
        assert!(
            ui.key(key(KeyCode::Enter), &s, true).action.is_none(),
            "links are never followed"
        );
        assert_eq!(
            ui.key(key(KeyCode::Backspace), &s, true).action,
            Some(json!({"type":"sidebar","op":"dir","path":""})),
            "Backspace goes to the parent"
        );
    }

    fn click_at(ui: &mut SidebarUi, s: &Snapshot, index: usize) -> Out {
        let line = ui
            .line_items
            .iter()
            .position(|item| *item == Some(index))
            .expect("row is drawn");
        let at = mouse(
            MouseEventKind::Down(MouseButton::Left),
            ui.list.x + 2,
            ui.list.y + line as u16,
        );
        ui.mouse(at, s, Rect::new(0, 0, 34, 24), 200)
    }

    #[test]
    fn double_clicking_the_same_file_previews_it_but_single_clicks_only_select() {
        let mut s = open("Files", 34);
        s.sidebar.files = json!({"loaded":true,"path":"src","label":"repo/src","rows":[
            {"name":"core","path":"src/core","kind":"dir","size":""},
            {"name":"a.rs","path":"src/a.rs","kind":"file","size":"1K"},
            {"name":"b.rs","path":"src/b.rs","kind":"file","size":"1K"},
            {"name":"link","path":"src/link","kind":"link","size":""},
        ]});
        let mut ui = SidebarUi::default();
        screen(&mut ui, &s, 34, 24);
        let first = click_at(&mut ui, &s, 1);
        assert!(
            first.consumed && first.action.is_none(),
            "one click selects only"
        );
        assert_eq!(ui.file_sel, 1);
        // A different file within the interval is a new first click, not a double-click.
        assert!(click_at(&mut ui, &s, 2).action.is_none());
        assert_eq!(ui.file_sel, 2);
        let second = click_at(&mut ui, &s, 2);
        assert_eq!(
            second.action,
            Some(json!({"type":"sidebar","op":"file","path":"src/b.rs"}))
        );
        assert!(
            click_at(&mut ui, &s, 2).action.is_none(),
            "the pair is consumed"
        );
        // Folders keep opening on a single click and reset the pair; links do nothing.
        assert_eq!(
            click_at(&mut ui, &s, 0).action,
            Some(json!({"type":"sidebar","op":"dir","path":"src/core"}))
        );
        assert!(click_at(&mut ui, &s, 3).action.is_none());
        assert!(
            click_at(&mut ui, &s, 3).action.is_none(),
            "links never preview"
        );
    }

    #[test]
    fn a_slow_second_click_is_not_a_double_click() {
        let row = json!({"name":"a.rs","path":"src/a.rs","kind":"file"});
        let mut ui = SidebarUi::default();
        let start = Instant::now();
        assert!(ui.click_file_row(&row, start).action.is_none());
        let late = start + DOUBLE_CLICK + Duration::from_millis(1);
        assert!(ui.click_file_row(&row, late).action.is_none(), "too slow");
        let quick = late + Duration::from_millis(100);
        assert!(
            ui.click_file_row(&row, quick).action.is_some(),
            "the late click started a new pair"
        );
    }

    #[test]
    fn files_render_filenames_without_file_sizes() {
        let mut s = open("Files", 34);
        s.sidebar.files = json!({"loaded":true,"path":"","label":"repo/","rows":[
            {"name":"AGENTS.md","path":"AGENTS.md","kind":"file","size":"10.5K"},
            {"name":"README.md","path":"README.md","kind":"file","size":"1K"},
            {"name":"src","path":"src","kind":"dir","size":""},
        ]});
        let mut ui = SidebarUi::default();
        ui.focus = true;
        let text = screen(&mut ui, &s, 34, 24);
        for want in ["AGENTS.md", "README.md", "src/"] {
            assert!(
                text.contains(want),
                "{want:?} missing in files list:\n{text}"
            );
        }
        for unwanted in ["10.5K", "1K"] {
            assert!(
                !text.contains(unwanted),
                "{unwanted:?} size should not be rendered:\n{text}"
            );
        }
    }

    #[test]
    fn git_status_is_labelled_and_read_only() {
        let mut s = open("Git", 40);
        s.sidebar.git = json!({"loaded":true,"available":true,"summary":"main → origin/main · ahead 1 · behind 0",
        "rows":[
            {"kind":"heading","text":"Staged changes · 1"},
            {"kind":"entry","code":"A","label":"added","path":"new.rs"},
            {"kind":"heading","text":"Untracked · 1"},
            {"kind":"entry","code":"?","label":"untracked","path":"notes.txt"},
            {"kind":"entry","code":"M","label":"modified","path":"docs/config.md","name":"config.md"},
        ]});
        let mut ui = SidebarUi::default();
        ui.focus = true;
        let text = screen(&mut ui, &s, 44, 24);
        for want in [
            "main → origin/main",
            "Staged changes · 1",
            "added",
            "new.rs",
            "untracked",
            "notes.txt",
            "config.md",
        ] {
            assert!(text.contains(want), "{want:?} missing:\n{text}");
        }
        assert!(
            !text.contains("docs/config.md"),
            "a row shows the base name only:\n{text}"
        );
        // Every key either scrolls, refreshes or is ignored: nothing can change the repository.
        for code in [
            KeyCode::Enter,
            KeyCode::Delete,
            KeyCode::Char('d'),
            KeyCode::Char('a'),
        ] {
            let out = ui.key(key(code), &s, true);
            assert!(out.action.is_none() && out.operation.is_none(), "{code:?}");
        }
        s.sidebar.git = json!({"loaded":true,"available":false,"message":"This workspace is not a Git repository"});
        assert!(screen(&mut ui, &s, 44, 24).contains("not a Git repository"));
    }

    #[test]
    fn narrow_and_tiny_columns_do_not_panic() {
        let s = sessions_snapshot();
        for (width, height) in [(0u16, 0u16), (3, 3), (9, 5), (12, 8), (24, 7), (60, 40)] {
            let mut ui = SidebarUi::default();
            for tab in TABS {
                let mut s = s.clone_for_test();
                s.sidebar.tab = tab.into();
                let mut terminal =
                    Terminal::new(TestBackend::new(width.max(1), height.max(1))).unwrap();
                terminal
                    .draw(|frame| {
                        let area = frame.area();
                        ui.draw(frame, area, &s, &Palette::new(false), 0, false);
                    })
                    .unwrap();
            }
        }
    }

    #[test]
    fn selecting_a_session_dismisses_until_the_host_closes_it() {
        for click in [false, true] {
            let mut s = sessions_snapshot();
            let mut cache = crate::render::Cache::default();
            let (regions, lines) = paint_all(&s, 120, 32, &mut cache);
            assert!(regions.sidebar.width > 0);
            let out = if click {
                let row = lines
                    .iter()
                    .position(|line| line.contains("Alpha"))
                    .unwrap() as u16;
                let event = mouse(
                    MouseEventKind::Down(MouseButton::Left),
                    regions.sidebar.x + 5,
                    row,
                );
                let first = cache.sidebar.mouse(event, &s, regions.sidebar, 120);
                assert!(first.operation.is_none());
                assert!(cache.sidebar.is_open(&s), "a single click only selects");
                cache.sidebar.mouse(event, &s, regions.sidebar, 120)
            } else {
                cache.sidebar.focus = true;
                cache.sidebar.key(key(KeyCode::Enter), &s, true)
            };
            assert_eq!(
                out.operation,
                Some(json!({"kind":"session_open","id":"a","workspace":"/w"}))
            );
            assert!(!cache.sidebar.is_open(&s));
            assert!(!cache.sidebar.focus);
            let (regions, _) = paint_all(&s, 120, 32, &mut cache);
            assert_eq!(regions.sidebar, Rect::default());

            cache.sidebar.sync(&s);
            let mut stale = sessions_snapshot();
            stale.sidebar.focus += 1;
            cache.sidebar.take_host_focus(&s, &stale);
            assert!(!cache.sidebar.focus);
            assert!(
                !cache.sidebar.is_open(&s),
                "stale open snapshots must not reopen"
            );
            let (regions, _) = paint_all(&s, 120, 32, &mut cache);
            assert_eq!(regions.sidebar, Rect::default());

            s.sidebar.open = false;
            cache.sidebar.sync(&s);
            s.sidebar.open = true;
            assert!(cache.sidebar.is_open(&s), "a later host open is allowed");
            let (regions, _) = paint_all(&s, 120, 32, &mut cache);
            assert!(regions.sidebar.width > 0);
        }
    }

    /// A whole frame through `render::draw`, as the event loop paints it.
    fn paint_all(
        s: &Snapshot,
        width: u16,
        height: u16,
        cache: &mut crate::render::Cache,
    ) -> (crate::render::Regions, Vec<String>) {
        use crate::editor::Editor;
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        let mut regions = crate::render::Regions::default();
        terminal
            .draw(|frame| {
                regions = crate::render::draw(
                    frame,
                    s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer().clone();
        let lines = (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect()
            })
            .collect();
        (regions, lines)
    }

    #[test]
    fn selector_overlays_without_reserving_conversation_columns() {
        for width in 0..=240u16 {
            for height in [0, 3, 24, 40] {
                let mut s = open("Sessions", 34);
                s.details_sidebar = true;
                let area = Rect::new(0, 0, width, height);
                let opened = crate::render::regions(area, &s, 8, false);
                assert!(opened.sidebar.right() <= width && opened.sidebar.bottom() <= height);
                s.sidebar.open = false;
                let closed = crate::render::regions(area, &s, 8, false);
                assert_eq!(opened.transcript, closed.transcript);
                assert_eq!(opened.details, closed.details);
            }
        }
    }

    #[test]
    fn selector_background_is_grey_in_both_themes() {
        for light in [false, true] {
            let s = open("Sessions", 34);
            let mut ui = SidebarUi::default();
            let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
            let palette = Palette::new(light);
            terminal
                .draw(|frame| {
                    let area = frame.area();
                    ui.draw(frame, area, &s, &palette, 0, false);
                })
                .unwrap();
            let expected = if light {
                ratatui::style::Color::Rgb(238, 238, 238)
            } else {
                ratatui::style::Color::Rgb(28, 28, 28)
            };
            // Blank padding and unselected content inherit the selector surface.
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer[(0, 0)].bg, expected);
            assert_eq!(buffer[(2, 5)].bg, expected);
        }
    }

    #[test]
    fn selector_has_blank_padding_and_no_right_border() {
        let s = open("Sessions", 34);
        let mut ui = SidebarUi::default();
        let mut terminal = Terminal::new(TestBackend::new(84, 28)).unwrap();
        let area = Rect::new(2, 2, 80, 24);
        terminal
            .draw(|frame| ui.draw(frame, area, &s, &Palette::new(false), 0, false))
            .unwrap();
        let buffer = terminal.backend().buffer();
        for y in area.y..area.bottom() {
            for x in [area.x, area.x + 1, area.right() - 2, area.right() - 1] {
                assert_eq!(buffer[(x, y)].symbol(), " ", "padding at {x},{y}");
            }
        }
        for x in area.x..area.right() {
            for y in [area.y, area.bottom() - 1] {
                assert_eq!(buffer[(x, y)].symbol(), " ", "padding at {x},{y}");
            }
        }
        assert_eq!(ui.tab_hits[0].0.x, area.x + 2);
        assert_eq!(ui.tab_hits[0].0.y, area.y + 2);
    }

    #[test]
    fn selector_paints_at_its_modal_hit_regions() {
        let s = open("Projects", 34);
        let mut cache = crate::render::Cache::default();
        let (drawn, lines) = paint_all(&s, 140, 40, &mut cache);
        assert_eq!(drawn.sidebar, selector_area(Rect::new(0, 0, 140, 40)));
        assert!(lines.join("\n").contains("Projects"));
        for (rect, _) in &cache.sidebar.tab_hits {
            assert!(rect.x >= drawn.sidebar.x && rect.right() <= drawn.sidebar.right());
        }
    }

    impl Snapshot {
        fn clone_for_test(&self) -> Snapshot {
            Snapshot {
                sidebar: self.sidebar.clone(),
                ..Snapshot::default()
            }
        }
    }
}
