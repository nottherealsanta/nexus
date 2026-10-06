//! The one Sessions surface: `/session`, `/sessions`, `/archived` and the left
//! sidebar all open this (plan §9.2).
use crate::ctx::*;
use crate::fixture::*;
use nexus_widgets::lists::*;
use nexus_widgets::*;
use ratatui::{layout::Rect, style::Style};

pub const FILTERS: [&str; 3] = ["Active", "All", "Archived"];

pub fn visible<'a>(w: &'a World, v: &View) -> Vec<&'a Session> {
    let q = v.sess_search.value.to_lowercase();
    w.sessions
        .iter()
        .filter(|s| match v.sess_filter {
            0 => !s.archived,
            2 => s.archived,
            _ => true,
        })
        .filter(|s| q.is_empty() || fuzzy(&s.title.to_lowercase(), &q) || s.workspace.contains(&q) || s.id.starts_with(&q))
        .collect()
}
fn fuzzy(hay: &str, q: &str) -> bool {
    let mut it = hay.chars();
    q.chars().all(|c| it.any(|h| h == c))
}

/// Draw the panel into `area` (docked sidebar or drawer). `title_bar` draws the frame.
pub fn panel(c: &mut Ctx, area: Rect) {
    let t = c.theme();
    let (inner, hint_row) = modal(c.buf, c.ui, area, &Modal { title: "Sessions", id: "sessions:close" });
    let rows = visible(c.w, c.v);
    let total = c.w.sessions.iter().filter(|s| match c.v.sess_filter { 0 => !s.archived, 2 => s.archived, _ => true }).count();
    // search field
    let st = c.v.sess_search.clone();
    let r = search_field(c.buf, c.ui, Rect::new(inner.x, inner.y, inner.width, 1), "sessions:search", &st, Some((rows.len(), total)));
    c.v.sess_search_focus = r.focused;
    // filter tabs
    let tabs_t: Vec<Tab> = FILTERS.iter().map(|f| Tab { label: f, badge: "" }).collect();
    tabs(c.buf, c.ui, Rect::new(inner.x, inner.y + 1, inner.width, 2), "sessions:filter", &tabs_t, c.v.sess_filter);
    let list_area = Rect::new(inner.x, inner.y + 3, inner.width, inner.height.saturating_sub(5));
    if rows.is_empty() {
        let msg = if c.v.sess_search.value.is_empty() { "No archived sessions." } else { "No sessions match." };
        empty_state(c.buf, c.ui, list_area, "sessions:empty", msg, if c.v.sess_filter == 2 { "Show active" } else { "Clear search" });
    } else {
        let mut last_group = String::new();
        let items: Vec<ListItem> = rows
            .iter()
            .map(|s| {
                let g = if s.workspace != last_group { last_group = s.workspace.clone(); s.workspace.clone() } else { s.workspace.clone() };
                let renaming = c.v.rename.as_ref().filter(|_| rows.iter().position(|x| x.id == s.id) == Some(c.v.sess_sel));
                ListItem {
                    id: format!("session:{}", s.id),
                    group: g,
                    title: renaming.map(|r| format!("{}▏", r.value)).unwrap_or_else(|| if c.v.selected.contains(&s.id) { format!("{} {}", c.ui.glyphs.check_on, s.title) } else { s.title.clone() }),
                    sub: s.sub.clone(),
                    meta: s.age.clone(),
                    dot: Some(s.dot),
                    active: s.id == "s1",
                    actions: if s.archived { vec!["Open".into(), "Unarchive".into(), "Delete".into()] } else { vec!["Open".into(), "Rename".into(), "Fork".into(), "Archive".into()] },
                }
            })
            .collect();
        let mut ls = c.v.sess_scroll;
        if let Some(cur) = c.ui.focus.current().and_then(|f| f.strip_prefix("session:")) {
            if let Some(i) = rows.iter().position(|s| s.id == cur) {
                c.v.sess_sel = i;
            }
        }
        ls.selected = c.v.sess_sel;
        list(c.buf, c.ui, list_area, &items, &mut ls);
        c.v.sess_scroll = ls;
        let more = if c.v.sess_filter == 2 { total.saturating_sub(rows.len()) } else { 0 };
        if more > 0 {
            put(c.buf, inner.x + 2, inner.y + inner.height - 2, &format!("{} {more} not shown by the filter", c.ui.glyphs.ellipsis), t.dim(), inner.width);
        }
    }
    let bw = button_width("+ New session");
    button(c.buf, c.ui, inner.x + 1, inner.y + inner.height - 1, &Button::new("sessions:new", "+ New session", ButtonKind::Secondary));
    let _ = bw;
    let sel_count = c.v.selected.len();
    if sel_count > 0 {
        key_hints(c.buf, c.ui, hint_row, &[("a", &format!("archive {sel_count}")), ("Esc", "clear selection")]);
    } else {
        key_hints(c.buf, c.ui, hint_row, &[("↑↓", "move"), ("Enter", "open"), ("o", "new tab"), ("r", "rename"), ("a", "archive"), ("Space", "select"), ("?", "keys")]);
    }
    let _ = Style::default();
}
