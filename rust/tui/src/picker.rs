//! Sessions dashboard state, filtering and keys, independent of generic menu pickers.
//!
//! The host owns the rows and the preview; this module owns only presentation state
//! (query, selection, scroll, rename and archive confirmation). Every host-visible effect
//! is returned as an operation for the caller to send.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::layout::{Position, Rect};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::time::{Duration, Instant};

const DOUBLE_CLICK: Duration = Duration::from_millis(300);
const QUERY_LIMIT: usize = 80;
const TITLE_LIMIT: usize = 200;

#[derive(Default)]
pub struct Dashboard {
    pub query: String,
    /// `/` was pressed: printable keys edit the query instead of acting as shortcuts.
    pub searching: bool,
    pub selected: usize,
    pub selected_id: String,
    /// The session whose preview was last requested (or the host's initial selection).
    pub requested: String,
    pub rename: Option<String>,
    /// Session id awaiting archive confirmation.
    pub confirm_archive: Option<String>,
    pub by_project: bool,
    pub preview_scroll: usize,
    pub list_top: usize,
    pub notice: String,
    /// Geometry recorded by the last draw, for click and wheel mapping.
    pub list_area: Rect,
    pub preview_area: Rect,
    pub line_rows: Vec<Option<usize>>,
    /// Heading label for a list line that toggles a group, else None.
    pub line_headings: Vec<Option<String>>,
    /// Row under the pointer. Hover expands it and does not move the selection.
    pub hovered_id: String,
    /// Heading labels, without the count, whose rows are hidden.
    pub collapsed: HashSet<String>,
    /// When set, only rows in `workspace` (or its repo) stay in the list.
    pub this_project: bool,
    pub workspace: String,
    pub new_hit: Rect,
    pub resume_hit: Rect,
    pub worktree_hit: Rect,
    pub choose_hit: Rect,
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

/// State-grouping section of a row: needs input, working, idle, done.
pub fn section(row: &Value) -> u8 {
    match text(row, "status") {
        "input" => 0,
        "working" => 1,
        "done" => 3,
        _ => 2,
    }
}

pub fn section_label(section: u8) -> &'static str {
    match section {
        0 => "Needs input",
        1 => "Working",
        2 => "Idle",
        _ => "Done",
    }
}

/// Only what the list shows. The workspace path is not searchable.
fn searchable(row: &Value) -> String {
    [
        "title",
        "project",
        "worktree",
        "sub",
        "status",
        "state",
        "day",
        "last_user",
        "last_agent",
        "activity",
        "branch",
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
                query.is_empty() || searchable(row).contains(&query)
            })
            .collect();
        if !self.by_project {
            shown.sort_by_key(|row| section(row));
        }
        shown
    }

    /// Keep the selection on the same session across host refreshes.
    pub fn sync(&mut self, dashboard: &Value) {
        self.workspace = text(dashboard, "workspace").to_owned();
        if self.selected_id.is_empty() {
            self.selected_id = dashboard
                .get("selected")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
        }
        if self.requested.is_empty() {
            self.requested = text(dashboard, "selected").to_owned();
        }
        let shown = self.filtered(rows_of(dashboard));
        let (index, id) = match shown.iter().position(|row| text(row, "id") == self.selected_id) {
            Some(index) => (index, self.selected_id.clone()),
            None => {
                let index = self.selected.min(shown.len().saturating_sub(1));
                let id = shown.get(index).map_or("", |row| text(row, "id")).to_owned();
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

    fn preview_target(&self) -> &str {
        if !self.hovered_id.is_empty() {
            &self.hovered_id
        } else {
            &self.selected_id
        }
    }

    fn preview_for_selection(&mut self) -> Option<Value> {
        let target = self.preview_target().to_owned();
        if target.is_empty() || target == self.requested {
            return None;
        }
        self.preview_scroll = 0;
        self.requested = target.clone();
        Some(json!({"kind":"session_preview","id":target}))
    }

    /// Called every frame: when the selection moved without a key (the host removed or
    /// reordered rows), ask for the preview of the session now selected.
    pub fn reconcile(&mut self, dashboard: &Value) -> Option<Value> {
        self.sync(dashboard);
        self.preview_for_selection()
    }

    fn select(&mut self, index: usize, dashboard: &Value) -> Option<Value> {
        let id = {
            let shown = self.filtered(rows_of(dashboard));
            let index = index.min(shown.len().saturating_sub(1));
            self.selected = index;
            shown.get(index).map_or("", |row| text(row, "id")).to_owned()
        };
        self.selected_id = id;
        self.preview_for_selection()
    }

    fn group_key(&self, row: &Value) -> String {
        if self.by_project {
            text(row, "group").to_owned()
        } else {
            section_label(section(row)).to_owned()
        }
    }

    fn visible_indices(&self, dashboard: &Value) -> Vec<usize> {
        self.filtered(rows_of(dashboard))
            .iter()
            .enumerate()
            .filter(|(_, row)| !self.collapsed.contains(&self.group_key(row)))
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

    fn open_op(row: &Value) -> Value {
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

    fn query_changed(&mut self, dashboard: &Value) -> Option<Value> {
        self.selected = 0;
        self.selected_id.clear();
        self.list_top = 0;
        self.sync(dashboard);
        self.preview_for_selection()
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
                    if self.query.is_empty() {
                        self.searching = false;
                    } else {
                        self.query.clear();
                        return self.query_changed(dashboard);
                    }
                    return None;
                }
                KeyCode::Enter => {
                    self.searching = false;
                    return None;
                }
                KeyCode::Backspace => {
                    self.query.pop();
                    return self.query_changed(dashboard);
                }
                KeyCode::Char(c) if plain && self.query.chars().count() < QUERY_LIMIT => {
                    self.query.push(c);
                    return self.query_changed(dashboard);
                }
                KeyCode::Up | KeyCode::Down | KeyCode::PageUp | KeyCode::PageDown => {}
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
            KeyCode::Char('l') if control => {
                self.this_project = !self.this_project;
                self.list_top = 0;
                self.sync(dashboard);
                return None;
            }
            KeyCode::Char('w') if control => return Some(json!({"kind":"worktrees"})),
            KeyCode::Up => return self.move_by(-1, dashboard),
            KeyCode::Down => return self.move_by(1, dashboard),
            KeyCode::PageUp => return self.move_by(-10, dashboard),
            KeyCode::PageDown => return self.move_by(10, dashboard),
            KeyCode::Home => {
                let visible = self.visible_indices(dashboard);
                return self.select(visible.first().copied().unwrap_or(0), dashboard);
            }
            KeyCode::End => {
                let visible = self.visible_indices(dashboard);
                return self.select(visible.last().copied().unwrap_or(0), dashboard);
            }
            KeyCode::Enter => {
                return self
                    .selected_row(dashboard)
                    .map(Self::open_op);
            }
            KeyCode::Delete => return self.ask_archive(dashboard),
            _ => {}
        }
        if shortcut('k') {
            return self.move_by(-1, dashboard);
        }
        if shortcut('j') {
            return self.move_by(1, dashboard);
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
        if shortcut('r') {
            let title = self
                .selected_row(dashboard)
                .map(|row| text(row, "title").to_owned());
            self.rename = title;
            return None;
        }
        if shortcut('d') {
            return self.ask_archive(dashboard);
        }
        None
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
            KeyCode::Esc
            | KeyCode::Up
            | KeyCode::Down
            | KeyCode::PageUp
            | KeyCode::PageDown
            | KeyCode::Home
            | KeyCode::End
            | KeyCode::Delete => true,
            KeyCode::Enter if plain && draft_empty => true,
            KeyCode::Char('c') if control => draft_empty,
            KeyCode::Char('s' | 'n' | 'l' | 'w') if control => true,
            KeyCode::Char('k' | 'j' | '/' | 'g' | 'q' | 'n' | 'r' | 'd') if plain && draft_empty => {
                true
            }
            _ => false,
        }
    }

    /// Pointer motion. Changes which row is expanded and asks for its preview.
    /// The keyboard selection, which is what the composer sends to, stays put.
    pub fn hover(&mut self, x: u16, y: u16, dashboard: &Value) -> Option<Value> {
        self.sync(dashboard);
        let id = self
            .row_at(x, y)
            .and_then(|index| {
                self.filtered(rows_of(dashboard))
                    .get(index)
                    .map(|row| text(row, "id").to_owned())
            })
            .unwrap_or_default();
        if id == self.hovered_id {
            return None;
        }
        self.hovered_id = id;
        self.preview_for_selection()
    }

    /// The pointer left the list. The selected session's preview comes back.
    pub fn leave(&mut self) -> Option<Value> {
        if self.hovered_id.is_empty() {
            return None;
        }
        self.hovered_id.clear();
        self.preview_for_selection()
    }

    /// Archiving asks first, and a working or waiting session cannot be archived.
    fn ask_archive(&mut self, dashboard: &Value) -> Option<Value> {
        let row = self.selected_row(dashboard)?;
        if is_busy(row) {
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
        if self.resume_hit.contains(at) {
            return Some(json!({"kind":"archived","query":""}));
        }
        if self.worktree_hit.contains(at) {
            return Some(json!({"kind":"worktrees"}));
        }
        if self.choose_hit.contains(at) {
            self.this_project = !self.this_project;
            self.list_top = 0;
            self.sync(dashboard);
            return None;
        }
        let Some(index) = self.row_at(x, y) else {
            if let Some(heading) = self.heading_at(x, y) {
                if !self.collapsed.remove(&heading) {
                    self.collapsed.insert(heading);
                }
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

    /// The wheel scrolls the preview over the right pane, otherwise it moves the selection.
    pub fn wheel(&mut self, x: u16, y: u16, down: bool, dashboard: &Value) -> Option<Value> {
        self.sync(dashboard);
        let at = Position::new(x, y);
        if self.preview_area.contains(at) {
            self.preview_scroll = if down {
                self.preview_scroll + 3
            } else {
                self.preview_scroll.saturating_sub(3)
            };
            return None;
        }
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
    fn arrows_preview_enter_opens() {
        let rows = two();
        let mut picker = Dashboard::default();
        assert_eq!(
            picker.key(key(KeyCode::Down), &rows).unwrap()["kind"],
            "session_preview"
        );
        let open = picker.key(key(KeyCode::Enter), &rows).unwrap();
        assert_eq!((open["id"].as_str(), open["workspace"].as_str()), (Some("b"), Some("/b")));
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
    fn filter_matches_visible_fields_only() {
        let rows = dash(json!([
            {"id":"secret-id","title":"Alpha","workspace":"/hidden/path","status":"idle"},
        ]));
        let picker = Dashboard {
            query: "hidden".into(),
            ..Default::default()
        };
        assert!(picker.filtered(rows_of(&rows)).is_empty());
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
        picker.key(key(KeyCode::Esc), &rows);
        assert!(picker.query.is_empty());
        assert_eq!(
            picker.key(key(KeyCode::Esc), &rows).unwrap()["kind"],
            "sessions_close"
        );
    }
    #[test]
    fn composer_text_keeps_letters_and_arrows_stay() {
        let picker = Dashboard::default();
        let letter = key(KeyCode::Char('n'));
        assert!(!picker.owns(&letter, false));
        assert!(picker.owns(&letter, true));
        assert!(picker.owns(&key(KeyCode::Down), false));
        let enter = key(KeyCode::Enter);
        assert!(!picker.owns(&enter, false));
        assert!(picker.owns(&enter, true));
    }
    #[test]
    fn hover_requests_preview_without_moving_selection() {
        let rows = two();
        let mut picker = Dashboard::default();
        picker.sync(&rows);
        picker.list_area = Rect { x: 0, y: 0, width: 10, height: 4 };
        picker.line_rows = vec![Some(0), Some(1)];
        let op = picker.hover(0, 1, &rows).unwrap();
        assert_eq!(op["id"], "b");
        assert_eq!(picker.selected_id, "a");
        assert_eq!(picker.hovered_id, "b");
        assert!(picker.hover(0, 1, &rows).is_none());
        let back = picker.leave().unwrap();
        assert_eq!(back["id"], "a");
        assert!(picker.hovered_id.is_empty());
    }
    #[test]
    fn this_project_keeps_only_the_current_workspace() {
        let rows = dash(json!([
            {"id":"a","title":"Alpha","workspace":"/w","status":"idle"},
            {"id":"b","title":"Beta","workspace":"/other","repo":"/w","status":"idle"},
            {"id":"c","title":"Gamma","workspace":"/else","status":"idle"},
        ]));
        let picker = Dashboard { this_project: true, workspace: "/w".into(), ..Default::default() };
        let shown = picker.filtered(rows_of(&rows));
        assert_eq!(shown.len(), 2);
        assert!(shown.iter().all(|row| text(row, "id") != "c"));
    }
}
