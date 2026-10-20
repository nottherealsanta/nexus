//! Sessions dashboard state, filtering and keys, independent of generic menu pickers.
//!
//! The host owns the rows; this module owns only presentation state
//! (query, selection, scroll, rename and archive confirmation). Every host-visible effect
//! is returned as an operation for the caller to send.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::layout::{Position, Rect};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::time::{Duration, Instant};

const DOUBLE_CLICK: Duration = Duration::from_millis(300);
const QUERY_LIMIT: usize = 256;
const TITLE_LIMIT: usize = 200;

#[derive(Default)]
pub struct Dashboard {
    pub query: String,
    /// Query echoed by the host for these rows. None supports older hosts.
    response_query: Option<String>,
    /// `/` was pressed: printable keys edit the query instead of acting as shortcuts.
    pub searching: bool,
    pub selected: usize,
    pub selected_id: String,
    pub rename: Option<String>,
    /// Session id awaiting archive confirmation.
    pub confirm_archive: Option<String>,
    pub by_project: bool,
    pub list_top: usize,
    pub notice: String,
    /// Geometry recorded by the last draw, for click and wheel mapping.
    pub list_area: Rect,
    pub line_rows: Vec<Option<usize>>,
    /// Heading label for a list line that toggles a group, else None.
    pub line_headings: Vec<Option<String>>,
    /// Row under the pointer. Hover expands it and does not move the selection.
    pub hovered_id: String,
    /// Heading labels, without the count, whose rows are hidden.
    pub collapsed: HashSet<String>,
    /// Archived rows are collapsed until the user opens their heading.
    pub archived_open: bool,
    /// When set, only rows in `workspace` (or its repo) stay in the list.
    pub this_project: bool,
    pub workspace: String,
    /// The `/ Search sessions` row, and the `[×]` on the selected row.
    pub search_hit: Rect,
    pub archive_hit: Rect,
    pub new_hit: Rect,
    pub worktree_hit: Rect,
    pub choose_hit: Rect,
    /// The pinned Manager entry (its rows open it) and its `Voice` button; empty when not drawn.
    pub manager_hit: Rect,
    pub manager_live_hit: Rect,
    last_click: Option<(usize, Instant)>,
}

pub fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Working or waiting on the user: such a session is never archived from here.
pub fn is_busy(row: &Value) -> bool {
    matches!(text(row, "status"), "working" | "input")
        || matches!(
            text(row, "state"),
            "running" | "awaiting_input" | "awaiting_permission"
        )
}

/// Status section of a row: Needs you (`input`), Working, Idle (`idle` and `done`), Archived.
pub fn section(row: &Value) -> u8 {
    if row.get("archived").and_then(Value::as_bool) == Some(true)
        || text(row, "status") == "archived"
    {
        return 3;
    }
    match text(row, "status") {
        "input" => 0,
        "working" => 1,
        _ => 2,
    }
}

pub fn section_label(section: u8) -> &'static str {
    match section {
        0 => "Needs you",
        1 => "Working",
        2 => "Idle",
        _ => "Archived",
    }
}

/// The unanswered permission request of a "Needs you" row, if the host sent one.
pub fn pending(row: &Value) -> Option<&Value> {
    row.get("pending")
        .filter(|p| !text(p, "request_id").is_empty())
}

/// Whole seconds since `since` (unix seconds), clamped at zero; `now` is injected for tests.
pub fn waited(since: f64, now: f64) -> u64 {
    if since <= 0.0 {
        return 0;
    }
    (now - since).max(0.0) as u64
}

/// `mm:ss` (or `h:mm:ss` past an hour).
pub fn clock(seconds: u64) -> String {
    if seconds >= 3600 {
        format!(
            "{}:{:02}:{:02}",
            seconds / 3600,
            seconds / 60 % 60,
            seconds % 60
        )
    } else {
        format!("{:02}:{:02}", seconds / 60, seconds % 60)
    }
}

/// Fallback for hosts without an echoed server query.
fn searchable(row: &Value) -> String {
    [
        "title",
        "project",
        "worktree",
        "branch",
        "last_agent",
        "last_user",
        "workspace",
        "repo",
    ]
    .iter()
    .map(|key| text(row, key))
    .collect::<Vec<_>>()
    .join(" ")
    .to_lowercase()
}

fn rows_of(dashboard: &Value) -> &[Value] {
    dashboard
        .get("rows")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

/// Whether the host sent a Manager entry to pin above the sessions (older hosts send none).
pub fn has_manager(dashboard: &Value) -> bool {
    dashboard.get("manager").is_some_and(|entry| {
        entry.get("known").and_then(Value::as_bool) == Some(true)
            || !text(entry, "summary").is_empty()
    })
}

impl Dashboard {
    /// Rows in display order (stable-sorted into sections unless grouped by project).
    pub fn filtered<'a>(&self, rows: &'a [Value]) -> Vec<&'a Value> {
        let query = self.query.to_lowercase();
        let mut shown: Vec<&Value> = rows
            .iter()
            .filter(|row| {
                if text(row, "id").is_empty() {
                    return false;
                }
                if self.this_project && !self.workspace.is_empty() {
                    let here = self.workspace.as_str();
                    if text(row, "workspace") != here && text(row, "repo") != here {
                        return false;
                    }
                }
                match &self.response_query {
                    Some(echo) => echo == &self.query,
                    None => query.is_empty() || searchable(row).contains(&query),
                }
            })
            .collect();
        // Status sections always; newest prompt first inside them (the host's newest-archived
        // order is kept for Archived, and project order for the by-project view).
        let by_project = self.by_project;
        shown.sort_by(|a, b| {
            let updated = |row: &Value| row.get("updated").and_then(Value::as_f64).unwrap_or(0.0);
            let (sa, sb) = (section(a), section(b));
            let order = sa.cmp(&sb);
            if by_project || sa == 3 {
                return order;
            }
            order.then(
                updated(b)
                    .partial_cmp(&updated(a))
                    .unwrap_or(std::cmp::Ordering::Equal),
            )
        });
        shown
    }

    /// Keep the selection on the same session across host refreshes.
    pub fn sync(&mut self, dashboard: &Value) {
        self.workspace = text(dashboard, "workspace").to_owned();
        self.response_query = dashboard
            .get("query")
            .and_then(Value::as_str)
            .map(str::to_owned);
        if self.selected_id.is_empty() {
            self.selected_id = dashboard
                .get("selected")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
        }
        let shown = self.filtered(rows_of(dashboard));
        let (index, id) = match shown
            .iter()
            .position(|row| text(row, "id") == self.selected_id)
        {
            Some(index) => (index, self.selected_id.clone()),
            None => {
                let index = self.selected.min(shown.len().saturating_sub(1));
                let id = shown
                    .get(index)
                    .map_or("", |row| text(row, "id"))
                    .to_owned();
                (index, id)
            }
        };
        self.selected = index;
        self.selected_id = id;
        if !self.hovered_id.is_empty()
            && !shown.iter().any(|row| text(row, "id") == self.hovered_id)
        {
            self.hovered_id.clear();
        }
    }

    /// Called every frame: keeps the selection on the same session after the host
    /// removed or reordered rows.
    pub fn reconcile(&mut self, dashboard: &Value) {
        self.sync(dashboard);
    }

    fn select(&mut self, index: usize, dashboard: &Value) -> Option<Value> {
        let id = {
            let shown = self.filtered(rows_of(dashboard));
            let index = index.min(shown.len().saturating_sub(1));
            self.selected = index;
            shown
                .get(index)
                .map_or("", |row| text(row, "id"))
                .to_owned()
        };
        self.selected_id = id;
        None
    }

    /// Heading a row sits under: its status section, with its project inside when grouped by project.
    pub fn group_key(&self, row: &Value) -> String {
        let label = section_label(section(row));
        if self.by_project && !text(row, "group").is_empty() {
            format!("{label} · {}", text(row, "group"))
        } else {
            label.to_owned()
        }
    }

    /// Archived headings start collapsed; every other heading starts open.
    pub fn is_collapsed(&self, key: &str) -> bool {
        if key.starts_with("Archived") {
            !self.archived_open
        } else {
            self.collapsed.contains(key)
        }
    }

    fn toggle_heading(&mut self, key: String) {
        if key.starts_with("Archived") {
            self.archived_open = !self.archived_open;
        } else if !self.collapsed.remove(&key) {
            self.collapsed.insert(key);
        }
    }

    fn visible_indices(&self, dashboard: &Value) -> Vec<usize> {
        self.filtered(rows_of(dashboard))
            .iter()
            .enumerate()
            .filter(|(_, row)| !self.is_collapsed(&self.group_key(row)))
            .map(|(index, _)| index)
            .collect()
    }

    fn move_by(&mut self, delta: isize, dashboard: &Value) -> Option<Value> {
        let visible = self.visible_indices(dashboard);
        if visible.is_empty() {
            return None;
        }
        let pos = visible
            .iter()
            .position(|index| *index == self.selected)
            .unwrap_or(0);
        let next = if delta < 0 {
            pos.saturating_sub(delta.unsigned_abs())
        } else {
            (pos + delta as usize).min(visible.len() - 1)
        };
        self.select(visible[next], dashboard)
    }

    /// A keyboard move. Up from the top-most row activates the search bar instead.
    fn key_move(&mut self, delta: isize, dashboard: &Value) -> Option<Value> {
        let visible = self.visible_indices(dashboard);
        if delta == -1 && visible.first().is_none_or(|first| *first == self.selected) {
            self.searching = true;
            return None;
        }
        self.move_by(delta, dashboard)
    }

    fn open_op(row: &Value) -> Value {
        if section(row) == 3 {
            // An archived session previews first; "Resume" there unarchives and opens it.
            return json!({"kind":"archived_preview","id":text(row, "id")});
        }
        let workspace = Some(text(row, "workspace"))
            .filter(|w| !w.is_empty())
            .unwrap_or_else(|| text(row, "repo"));
        json!({"kind":"session_open","id":text(row, "id"),"workspace":workspace})
    }

    fn selected_row<'a>(&self, dashboard: &'a Value) -> Option<&'a Value> {
        self.filtered(rows_of(dashboard))
            .into_iter()
            .find(|row| text(row, "id") == self.selected_id)
    }

    fn query_changed(&mut self, _dashboard: &Value) -> Option<Value> {
        self.selected = 0;
        self.selected_id.clear();
        self.list_top = 0;
        Some(json!({"kind":"sessions_search","query":self.query}))
    }

    /// One key while the dashboard is visible. The dashboard owns every key: search owns
    /// printable keys (including q/r/d/n) once `/` was pressed, rename and the archive
    /// confirmation own their keys, and shortcuts apply otherwise.
    pub fn key(&mut self, key: KeyEvent, dashboard: &Value) -> Option<Value> {
        self.notice.clear();
        self.sync(dashboard);
        let plain = !key
            .modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT);
        let control = key.modifiers.contains(KeyModifiers::CONTROL);

        if self.rename.is_some() {
            let id = self.selected_id.clone();
            match key.code {
                KeyCode::Esc => self.rename = None,
                KeyCode::Enter => {
                    let title = self.rename.take().unwrap_or_default();
                    let title = title.trim().to_owned();
                    let unchanged = self
                        .selected_row(dashboard)
                        .is_some_and(|row| text(row, "title") == title);
                    if !title.is_empty() && !unchanged && !id.is_empty() {
                        return Some(json!({"kind":"session_rename","id":id,"title":title}));
                    }
                }
                KeyCode::Backspace => {
                    if let Some(title) = &mut self.rename {
                        title.pop();
                    }
                }
                KeyCode::Char(c) if plain => {
                    if let Some(title) = &mut self.rename {
                        if title.chars().count() < TITLE_LIMIT {
                            title.push(c);
                        }
                    }
                }
                _ => {}
            }
            return None;
        }

        if let Some(id) = self.confirm_archive.take() {
            if matches!(key.code, KeyCode::Enter | KeyCode::Char('y')) && id == self.selected_id {
                return Some(json!({"kind":"session_archive","id":id}));
            }
            return None;
        }

        if self.searching {
            match key.code {
                KeyCode::Esc => {
                    self.searching = false;
                    if self.query.is_empty() {
                        return None;
                    }
                    self.query.clear();
                    return self.query_changed(dashboard);
                }
                KeyCode::Enter | KeyCode::Down => {
                    // Into the results: the first match becomes the keyboard selection.
                    self.searching = false;
                    let first = self
                        .visible_indices(dashboard)
                        .first()
                        .copied()
                        .unwrap_or(0);
                    return self.select(first, dashboard);
                }
                KeyCode::Backspace => {
                    self.query.pop();
                    return self.query_changed(dashboard);
                }
                KeyCode::Char(c) if plain && self.query.chars().count() < QUERY_LIMIT => {
                    self.query.push(c);
                    return self.query_changed(dashboard);
                }
                KeyCode::Up | KeyCode::PageUp | KeyCode::PageDown => return None,
                KeyCode::Char('k') if control => return None,
                _ if !control => return None,
                _ => {}
            }
        }

        let searching = self.searching;
        let shortcut = |c: char| key.code == KeyCode::Char(c) && plain && !searching;
        match key.code {
            KeyCode::Esc => {
                if !self.query.is_empty() {
                    self.query.clear();
                    return self.query_changed(dashboard);
                }
                return Some(json!({"kind":"sessions_close"}));
            }
            KeyCode::Char('c' | 's') if control => return Some(json!({"kind":"sessions_close"})),
            KeyCode::Char('n') if control => return Some(json!({"kind":"session_new"})),
            // Ctrl+L stays the global details-sidebar toggle on this page.
            KeyCode::Char('l') if key.modifiers.contains(KeyModifiers::ALT) => {
                self.this_project = !self.this_project;
                self.list_top = 0;
                self.sync(dashboard);
                return None;
            }
            KeyCode::Char('w') if control => return Some(json!({"kind":"worktrees"})),
            KeyCode::Char('k') if control => {
                self.searching = true;
                return None;
            }
            KeyCode::Up => return self.key_move(-1, dashboard),
            KeyCode::Down => return self.key_move(1, dashboard),
            KeyCode::PageUp => return self.key_move(-10, dashboard),
            KeyCode::PageDown => return self.key_move(10, dashboard),
            KeyCode::Home => {
                let visible = self.visible_indices(dashboard);
                return self.select(visible.first().copied().unwrap_or(0), dashboard);
            }
            KeyCode::End => {
                let visible = self.visible_indices(dashboard);
                return self.select(visible.last().copied().unwrap_or(0), dashboard);
            }
            KeyCode::Enter => {
                return self.selected_row(dashboard).map(Self::open_op);
            }
            KeyCode::Delete => return self.ask_archive(dashboard),
            _ => {}
        }
        if shortcut('k') {
            return self.key_move(-1, dashboard);
        }
        if shortcut('j') {
            return self.key_move(1, dashboard);
        }
        if shortcut('/') {
            self.searching = true;
            return None;
        }
        if shortcut('g') {
            self.by_project = !self.by_project;
            self.list_top = 0;
            self.sync(dashboard);
            return None;
        }
        if shortcut('q') {
            return Some(json!({"kind":"sessions_close"}));
        }
        if shortcut('n') {
            return Some(json!({"kind":"session_new"}));
        }
        // The pinned Manager: `m` opens it, `M` opens it and starts a voice call there.
        if has_manager(dashboard) {
            if shortcut('m') {
                return Some(json!({"kind":"manager_open"}));
            }
            if shortcut('M') {
                return Some(json!({"kind":"manager_live"}));
            }
        }
        if shortcut('t') {
            return self
                .selected_row(dashboard)
                .map(|row| json!({"kind":"ownership_tree","id":text(row, "id")}));
        }
        if shortcut('r') {
            let title = self
                .selected_row(dashboard)
                .map(|row| text(row, "title").to_owned());
            self.rename = title;
            return None;
        }
        if shortcut('a') {
            return self.answer_pending(dashboard, "allow_once");
        }
        if shortcut('d') {
            if self
                .selected_row(dashboard)
                .is_some_and(|row| pending(row).is_some())
            {
                return self.answer_pending(dashboard, "deny_once");
            }
            return self.ask_archive(dashboard);
        }
        // There is no composer here: any other printable key starts the filter.
        if let KeyCode::Char(c) = key.code {
            if plain && !searching && !c.is_control() && !c.is_whitespace() {
                self.searching = true;
                self.query.push(c);
                return self.query_changed(dashboard);
            }
        }
        None
    }

    /// `a` / `d` on a Needs you row: answer its pending permission without opening the session.
    fn answer_pending(&mut self, dashboard: &Value, decision: &str) -> Option<Value> {
        let row = self.selected_row(dashboard)?;
        let request = text(pending(row)?, "request_id");
        Some(
            json!({"kind":"session_permission","id":text(row, "id"),"request_id":request,"decision":decision}),
        )
    }

    /// Whether the dashboard consumes this key. A nonempty composer draft keeps letters,
    /// Backspace, Tab and Enter so the existing composer can send to the selected session.
    pub fn owns(&self, key: &KeyEvent, draft_empty: bool) -> bool {
        if self.rename.is_some() || self.confirm_archive.is_some() || self.searching {
            return true;
        }
        let plain = !key
            .modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SHIFT);
        let control = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Esc | KeyCode::Delete => true,
            KeyCode::Up
            | KeyCode::Down
            | KeyCode::PageUp
            | KeyCode::PageDown
            | KeyCode::Home
            | KeyCode::End => draft_empty,
            KeyCode::Char('k') if control => true,
            KeyCode::Enter if plain && draft_empty => true,
            KeyCode::Char('c') if control => draft_empty,
            KeyCode::Char('s' | 'n' | 'w') if control => true,
            KeyCode::Char('l') if key.modifiers.contains(KeyModifiers::ALT) => true,
            // Shortcuts and, with no composer, every other printable key (it starts the filter).
            KeyCode::Char(c) if draft_empty && !c.is_control() && !c.is_whitespace() => !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT),
            _ => false,
        }
    }

    /// Pointer motion only highlights the row under it. It never changes the selection.
    pub fn hover(&mut self, x: u16, y: u16, dashboard: &Value) -> Option<Value> {
        self.sync(dashboard);
        self.hovered_id = self
            .row_at(x, y)
            .and_then(|index| {
                self.filtered(rows_of(dashboard))
                    .get(index)
                    .map(|row| text(row, "id").to_owned())
            })
            .unwrap_or_default();
        None
    }

    /// The pointer left the list: the highlight goes away.
    pub fn leave(&mut self) -> Option<Value> {
        self.hovered_id.clear();
        None
    }

    /// Archiving asks first, and a working or waiting session cannot be archived.
    fn ask_archive(&mut self, dashboard: &Value) -> Option<Value> {
        let row = self.selected_row(dashboard)?;
        if section(row) == 3 {
            self.notice = "Already archived — Enter previews and resumes it".into();
        } else if is_busy(row) {
            self.notice = "A working session can't be archived — wait for it to finish".into();
        } else {
            self.confirm_archive = Some(text(row, "id").to_owned());
        }
        None
    }

    fn row_at(&self, x: u16, y: u16) -> Option<usize> {
        let at = Position::new(x, y);
        if !self.list_area.contains(at) {
            return None;
        }
        self.line_rows
            .get(usize::from(y - self.list_area.y))
            .copied()
            .flatten()
    }

    fn heading_at(&self, x: u16, y: u16) -> Option<String> {
        let at = Position::new(x, y);
        if !self.list_area.contains(at) {
            return None;
        }
        self.line_headings
            .get(usize::from(y - self.list_area.y))
            .cloned()
            .flatten()
    }

    /// A left click: selects the row under the pointer; a second click on it opens it.
    pub fn click(&mut self, x: u16, y: u16, dashboard: &Value) -> Option<Value> {
        self.notice.clear();
        self.sync(dashboard);
        let at = Position::new(x, y);
        if self.new_hit.contains(at) {
            return Some(json!({"kind":"session_new"}));
        }
        if self.worktree_hit.contains(at) {
            return Some(json!({"kind":"worktrees"}));
        }
        if self.manager_live_hit.contains(at) {
            return Some(json!({"kind":"manager_live"}));
        }
        if self.manager_hit.contains(at) {
            return Some(json!({"kind":"manager_open"}));
        }
        if self.search_hit.contains(at) {
            self.searching = true;
            return None;
        }
        if self.archive_hit.contains(at) {
            return self.ask_archive(dashboard);
        }
        if self.choose_hit.contains(at) {
            self.this_project = !self.this_project;
            self.list_top = 0;
            self.sync(dashboard);
            return None;
        }
        let Some(index) = self.row_at(x, y) else {
            if let Some(heading) = self.heading_at(x, y) {
                self.toggle_heading(heading);
            }
            return None;
        };
        let double = self
            .last_click
            .is_some_and(|(last, at)| last == index && at.elapsed() < DOUBLE_CLICK);
        if double {
            self.last_click = None;
            self.select(index, dashboard);
            return self.selected_row(dashboard).map(Self::open_op);
        }
        self.last_click = Some((index, Instant::now()));
        self.select(index, dashboard)
    }

    /// The wheel over the list moves the selection.
    pub fn wheel(&mut self, x: u16, y: u16, down: bool, dashboard: &Value) -> Option<Value> {
        self.sync(dashboard);
        let at = Position::new(x, y);
        if self.list_area.contains(at) {
            return self.move_by(if down { 1 } else { -1 }, dashboard);
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn key(c: KeyCode) -> KeyEvent {
        KeyEvent::new(c, KeyModifiers::NONE)
    }
    fn dash(rows: Value) -> Value {
        json!({"rows": rows, "selected": "a"})
    }
    fn two() -> Value {
        dash(json!([
            {"id":"a","title":"Alpha","workspace":"/a","status":"idle","day":"Today"},
            {"id":"b","title":"Beta","workspace":"/b","status":"idle","day":"Today"},
        ]))
    }
    #[test]
    fn tree_shortcut_is_read_only_and_targets_selected_root() {
        let rows = two();
        let mut picker = Dashboard::default();
        assert_eq!(
            picker.key(key(KeyCode::Char('t')), &rows),
            Some(json!({"kind":"ownership_tree","id":"a"}))
        );
    }
    #[test]
    fn arrows_move_locally_enter_opens() {
        let rows = two();
        let mut picker = Dashboard::default();
        assert!(picker.key(key(KeyCode::Down), &rows).is_none());
        let open = picker.key(key(KeyCode::Enter), &rows).unwrap();
        assert_eq!(
            (open["id"].as_str(), open["workspace"].as_str()),
            (Some("b"), Some("/b"))
        );
    }
    #[test]
    fn search_owns_shortcut_letters() {
        let rows = dash(json!([{"id":"a","title":"square","status":"idle"}]));
        let mut picker = Dashboard::default();
        picker.key(key(KeyCode::Char('/')), &rows);
        for c in ['s', 'q', 'r'] {
            picker.key(key(KeyCode::Char(c)), &rows);
        }
        assert_eq!(picker.query, "sqr");
        assert!(picker.rename.is_none());
        assert!(picker.confirm_archive.is_none());
    }
    #[test]
    fn fallback_filter_matches_workspace() {
        let rows = dash(json!([
            {"id":"secret-id","title":"Alpha","workspace":"/hidden/path","status":"idle"},
        ]));
        let picker = Dashboard {
            query: "hidden".into(),
            ..Default::default()
        };
        assert_eq!(picker.filtered(rows_of(&rows)).len(), 1);
        let picker = Dashboard {
            query: "alpha".into(),
            ..Default::default()
        };
        assert_eq!(picker.filtered(rows_of(&rows)).len(), 1);
    }
    #[test]
    fn archive_asks_and_refuses_busy_rows() {
        let rows = dash(json!([
            {"id":"a","title":"Alpha","status":"idle"},
            {"id":"b","title":"Beta","status":"working"},
        ]));
        let mut picker = Dashboard::default();
        assert!(picker.key(key(KeyCode::Char('d')), &rows).is_none());
        assert_eq!(picker.confirm_archive.as_deref(), Some("a"));
        let op = picker.key(key(KeyCode::Char('y')), &rows).unwrap();
        assert_eq!(op["kind"], "session_archive");
        // Working rows sort first, so select it and ask again.
        let mut picker = Dashboard {
            selected_id: "b".into(),
            ..Default::default()
        };
        picker.key(key(KeyCode::Char('d')), &rows);
        assert!(picker.confirm_archive.is_none());
        assert!(!picker.notice.is_empty());
    }
    #[test]
    fn archive_confirmation_cancels_on_other_keys() {
        let rows = two();
        let mut picker = Dashboard::default();
        picker.key(key(KeyCode::Char('d')), &rows);
        assert!(picker.key(key(KeyCode::Esc), &rows).is_none());
        assert!(picker.confirm_archive.is_none());
    }
    #[test]
    fn rename_saves_and_escape_cancels() {
        let rows = two();
        let mut picker = Dashboard::default();
        picker.key(key(KeyCode::Char('r')), &rows);
        picker.key(key(KeyCode::Char('!')), &rows);
        let op = picker.key(key(KeyCode::Enter), &rows).unwrap();
        assert_eq!(op["title"], "Alpha!");
        picker.key(key(KeyCode::Char('r')), &rows);
        assert!(picker.key(key(KeyCode::Esc), &rows).is_none());
        assert!(picker.rename.is_none());
    }
    #[test]
    fn selection_follows_the_session_across_refresh() {
        let mut picker = Dashboard::default();
        picker.key(key(KeyCode::Down), &two());
        let reordered = dash(json!([
            {"id":"c","title":"Gamma","status":"idle","day":"Today"},
            {"id":"b","title":"Beta","status":"idle","day":"Today"},
        ]));
        picker.sync(&reordered);
        assert_eq!((picker.selected, picker.selected_id.as_str()), (1, "b"));
    }
    #[test]
    fn escape_clears_filter_before_closing() {
        let rows = two();
        let mut picker = Dashboard {
            query: "be".into(),
            ..Default::default()
        };
        assert_eq!(
            picker.key(key(KeyCode::Esc), &rows).unwrap(),
            json!({"kind":"sessions_search","query":""})
        );
        assert!(picker.query.is_empty());
        assert_eq!(
            picker.key(key(KeyCode::Esc), &rows).unwrap()["kind"],
            "sessions_close"
        );
    }
    #[test]
    fn remote_search_trusts_matches_and_rejects_stale_rows() {
        let mut picker = Dashboard::default();
        let mut rows = two();
        rows["query"] = json!("");
        picker.key(key(KeyCode::Char('/')), &rows);
        assert_eq!(
            picker.key(key(KeyCode::Char('z')), &rows).unwrap(),
            json!({"kind":"sessions_search","query":"z"})
        );
        picker.sync(&rows);
        assert!(picker.filtered(rows_of(&rows)).is_empty());
        rows["query"] = json!("z");
        rows["rows"] = json!([{"id":"snippet-match","title":"No visible match"}]);
        picker.reconcile(&rows);
        assert_eq!(picker.filtered(rows_of(&rows)).len(), 1);
        assert_eq!(picker.selected_id, "snippet-match");
    }
    #[test]
    fn composer_text_keeps_letters_and_arrows_stay() {
        let picker = Dashboard::default();
        let letter = key(KeyCode::Char('n'));
        assert!(!picker.owns(&letter, false));
        assert!(picker.owns(&letter, true));
        assert!(!picker.owns(&key(KeyCode::Down), false));
        assert!(picker.owns(&key(KeyCode::Down), true));
        let enter = key(KeyCode::Enter);
        assert!(!picker.owns(&enter, false));
        assert!(picker.owns(&enter, true));
    }
    #[test]
    fn hover_highlights_without_touching_selection() {
        let rows = two();
        let mut picker = Dashboard::default();
        picker.sync(&rows);
        picker.list_area = Rect {
            x: 0,
            y: 0,
            width: 10,
            height: 4,
        };
        picker.line_rows = vec![Some(0), Some(1)];
        assert!(picker.hover(0, 1, &rows).is_none());
        assert_eq!(picker.selected_id, "a");
        assert_eq!(picker.hovered_id, "b");
        assert!(picker.leave().is_none());
        assert!(picker.hovered_id.is_empty());
    }
    #[test]
    fn up_from_the_top_row_opens_search_and_down_returns() {
        let rows = two();
        let mut picker = Dashboard::default();
        assert!(picker.key(key(KeyCode::Up), &rows).is_none());
        assert!(picker.searching);
        picker.key(key(KeyCode::Char('b')), &rows);
        assert_eq!(picker.query, "b");
        assert!(picker.key(key(KeyCode::Down), &rows).is_none());
        assert!(!picker.searching);
        assert_eq!(picker.selected_id, "b");
        picker.key(
            KeyEvent::new(KeyCode::Char('k'), KeyModifiers::CONTROL),
            &rows,
        );
        assert!(picker.searching, "Ctrl+K searches");
        picker.key(key(KeyCode::Esc), &rows);
        assert!(!picker.searching && picker.query.is_empty());
    }
    #[test]
    fn status_sections_sort_newest_prompt_first() {
        let rows = dash(json!([
            {"id":"x","title":"Gone","status":"archived","archived":true,"updated":9.0},
            {"id":"i1","title":"I1","status":"idle","updated":1.0},
            {"id":"d","title":"Done","status":"done","updated":7.0},
            {"id":"w","title":"Work","status":"working","updated":2.0},
            {"id":"n2","title":"Ask older","status":"input","updated":3.0},
            {"id":"n1","title":"Ask newer","status":"input","updated":8.0},
        ]));
        let picker = Dashboard::default();
        let shown = picker.filtered(rows_of(&rows));
        let ids: Vec<_> = shown.iter().map(|r| text(r, "id").to_owned()).collect();
        assert_eq!(ids, ["n1", "n2", "w", "d", "i1", "x"]);
        let names: Vec<_> = shown.iter().map(|r| picker.group_key(r)).collect();
        assert_eq!(
            names,
            [
                "Needs you",
                "Needs you",
                "Working",
                "Idle",
                "Idle",
                "Archived"
            ]
        );
    }
    #[test]
    fn archived_starts_collapsed_and_is_skipped_by_keyboard_moves() {
        let rows = dash(json!([
            {"id":"a","title":"Alpha","status":"idle","updated":2.0},
            {"id":"x","title":"Gone","status":"archived","archived":true},
        ]));
        let mut picker = Dashboard::default();
        assert!(picker.is_collapsed("Archived") && !picker.is_collapsed("Idle"));
        picker.key(key(KeyCode::End), &rows);
        assert_eq!(picker.selected_id, "a", "collapsed rows are not selectable");
        picker.toggle_heading("Archived".into());
        picker.key(key(KeyCode::End), &rows);
        assert_eq!(picker.selected_id, "x");
        assert_eq!(
            picker.key(key(KeyCode::Enter), &rows),
            Some(json!({"kind":"archived_preview","id":"x"}))
        );
        assert!(picker.key(key(KeyCode::Char('d')), &rows).is_none());
        assert!(picker.confirm_archive.is_none(), "already archived");
    }
    fn needs_you() -> Value {
        dash(json!([
            {"id":"a","title":"Ask","status":"input","state":"awaiting_permission","workspace":"/w",
             "pending":{"request_id":"r1","kind":"permission","summary":"Allow Bash: ls","ts":5.0}},
            {"id":"b","title":"Quiet","status":"idle","workspace":"/w"},
        ]))
    }
    #[test]
    fn a_and_d_answer_the_pending_request_without_opening_the_session() {
        let rows = needs_you();
        let mut picker = Dashboard::default();
        assert_eq!(
            picker.key(key(KeyCode::Char('a')), &rows),
            Some(
                json!({"kind":"session_permission","id":"a","request_id":"r1","decision":"allow_once"})
            )
        );
        assert_eq!(
            picker.key(key(KeyCode::Char('d')), &rows),
            Some(
                json!({"kind":"session_permission","id":"a","request_id":"r1","decision":"deny_once"})
            )
        );
        assert!(picker.confirm_archive.is_none() && picker.notice.is_empty());
        // A row without a pending request ignores `a`.
        picker.key(key(KeyCode::Down), &rows);
        assert_eq!(picker.selected_id, "b");
        assert!(picker.key(key(KeyCode::Char('a')), &rows).is_none());
    }
    #[test]
    fn typing_filters_without_a_composer() {
        let rows = needs_you();
        let mut picker = Dashboard::default();
        // `z` is no shortcut: it starts the filter, and the dashboard owns the key.
        assert!(picker.owns(&key(KeyCode::Char('z')), true));
        let op = picker.key(key(KeyCode::Char('z')), &rows).unwrap();
        assert_eq!(op, json!({"kind":"sessions_search","query":"z"}));
        assert!(picker.searching);
        picker.key(key(KeyCode::Char('a')), &rows);
        assert_eq!(picker.query, "za", "once searching, shortcut letters type");
    }
    fn with_manager(mut rows: Value) -> Value {
        rows["manager"] = json!({"known": true, "enabled": true, "summary": "Ready"});
        rows
    }
    #[test]
    fn m_opens_the_manager_and_shift_m_starts_voice_only_when_the_host_sent_one() {
        let mut picker = Dashboard::default();
        // Older hosts send no entry: `m` is just the start of a filter.
        assert_eq!(
            picker.key(key(KeyCode::Char('m')), &two()),
            Some(json!({"kind":"sessions_search","query":"m"}))
        );
        let rows = with_manager(two());
        let mut picker = Dashboard::default();
        assert_eq!(
            picker.key(key(KeyCode::Char('m')), &rows),
            Some(json!({"kind":"manager_open"}))
        );
        assert_eq!(
            picker.key(
                KeyEvent::new(KeyCode::Char('M'), KeyModifiers::SHIFT),
                &rows
            ),
            Some(json!({"kind":"manager_live"}))
        );
        // While searching, the letter types.
        picker.key(key(KeyCode::Char('/')), &rows);
        picker.key(key(KeyCode::Char('m')), &rows);
        assert_eq!(picker.query, "m");
    }
    #[test]
    fn clicking_the_pinned_manager_opens_it_and_its_voice_button_starts_a_call() {
        let rows = with_manager(two());
        let mut picker = Dashboard::default();
        picker.manager_hit = Rect::new(2, 3, 40, 3);
        picker.manager_live_hit = Rect::new(34, 3, 8, 1);
        assert_eq!(
            picker.click(5, 4, &rows),
            Some(json!({"kind":"manager_open"}))
        );
        assert_eq!(
            picker.click(36, 3, &rows),
            Some(json!({"kind":"manager_live"})),
            "the button wins over the row it sits on"
        );
    }
    #[test]
    fn waiting_clock_is_mm_ss() {
        assert_eq!(clock(waited(100.0, 165.0)), "01:05");
        assert_eq!(clock(waited(100.0, 90.0)), "00:00");
        assert_eq!(clock(waited(0.0, 500.0)), "00:00");
        assert_eq!(clock(3725), "1:02:05");
    }
    #[test]
    fn this_project_keeps_only_the_current_workspace() {
        let rows = dash(json!([
            {"id":"a","title":"Alpha","workspace":"/w","status":"idle"},
            {"id":"b","title":"Beta","workspace":"/other","repo":"/w","status":"idle"},
            {"id":"c","title":"Gamma","workspace":"/else","status":"idle"},
        ]));
        let picker = Dashboard {
            this_project: true,
            workspace: "/w".into(),
            ..Default::default()
        };
        let shown = picker.filtered(rows_of(&rows));
        assert_eq!(shown.len(), 2);
        assert!(shown.iter().all(|row| text(row, "id") != "c"));
    }
}
