//! List, ordered list, collapsible section, setting row, key/value, table, badges.
use super::*;
use crate::hit::Part;
use crate::ui::{Response, Ui};
use ratatui::style::{Color, Modifier};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dot {
    Ok,
    Work,
    Idle,
    Err,
}
pub fn dot(ui: &Ui, d: Dot) -> (&'static str, Color) {
    let g = ui.glyphs;
    let t = ui.theme;
    match d {
        Dot::Ok => (g.dot_ok, t.success),
        Dot::Work => (g.dot_work, t.warning),
        Dot::Idle => (g.dot_idle, t.quiet),
        Dot::Err => (g.dot_err, t.error),
    }
}

pub struct ListItem {
    pub id: String,
    pub group: String,
    pub title: String,
    pub sub: String,
    pub meta: String,
    pub dot: Option<Dot>,
    pub active: bool,
    /// Inline actions shown when the row has focus.
    pub actions: Vec<String>,
}
#[derive(Default, Clone, Copy)]
pub struct ListState {
    pub selected: usize,
    pub scroll: usize,
}

fn row_height(item: &ListItem, focused: bool) -> u16 {
    1 + (!item.sub.is_empty()) as u16 + (focused && !item.actions.is_empty()) as u16
}

/// Virtualised list with group headings, two-line rows and focus-revealed actions.
/// Returns the number of items drawn. Only visible rows are drawn (plan §7.13).
pub fn list(buf: &mut Buffer, ui: &mut Ui, area: Rect, items: &[ListItem], st: &mut ListState) -> usize {
    let t = ui.theme;
    if items.is_empty() || area.height == 0 {
        return 0;
    }
    st.selected = st.selected.min(items.len() - 1);
    if st.selected < st.scroll {
        st.scroll = st.selected;
    }
    // Scroll forward until the selected row fits.
    loop {
        let mut used = 0u16;
        let mut last = st.scroll;
        let mut prev_group = if st.scroll > 0 { items[st.scroll - 1].group.clone() } else { String::new() };
        for (i, it) in items.iter().enumerate().skip(st.scroll) {
            let head = (it.group != prev_group && !it.group.is_empty()) as u16;
            used += head + row_height(it, i == st.selected);
            prev_group = it.group.clone();
            if used > area.height {
                break;
            }
            last = i;
        }
        if st.selected <= last || st.scroll >= st.selected {
            break;
        }
        st.scroll += 1;
    }
    let mut y = area.y;
    let end = area.y + area.height;
    let mut drawn = 0;
    let mut prev_group = if st.scroll > 0 { items[st.scroll - 1].group.clone() } else { String::new() };
    for (i, it) in items.iter().enumerate().skip(st.scroll) {
        let focused = i == st.selected;
        let rh = row_height(it, focused);
        let head = (it.group != prev_group && !it.group.is_empty()) as u16;
        if y + head + rh > end {
            break;
        }
        if head == 1 {
            put(buf, area.x + 1, y, &it.group, t.strong(Style::default().fg(t.muted).add_modifier(Modifier::BOLD)), area.width.saturating_sub(2));
            y += 1;
        }
        prev_group = it.group.clone();
        let rect = Rect::new(area.x, y, area.width, rh);
        ui.focus.register(&it.id);
        ui.hits.add(rect, &it.id, Part::Body);
        let bst = row_style(ui, &it.id, focused, t.surface);
        fill(buf, rect, bst);
        if focused {
            focus_bar(buf, ui, area.x, y, rh);
        }
        let mut x = area.x + 2;
        if let Some(d) = it.dot {
            let (g, c) = dot(ui, d);
            put(buf, x, y, g, bst.fg(c), 1);
            x += 2;
        }
        let meta_w = width(&it.meta) as u16;
        let tw = (area.x + area.width).saturating_sub(x + meta_w + 2);
        let tst = if it.active { t.strong(bst.fg(t.accent)).add_modifier(Modifier::BOLD) } else if focused { bst.add_modifier(Modifier::BOLD) } else { bst };
        put(buf, x, y, &truncate(&it.title, tw as usize, ui.glyphs.ellipsis), tst, tw);
        if meta_w > 0 {
            put_right(buf, area.x + area.width - 1, y, &it.meta, t.dim().bg(bst.bg.unwrap_or(t.bg)));
        }
        let mut ly = y + 1;
        if !it.sub.is_empty() {
            let sw = (area.x + area.width).saturating_sub(x + 1);
            put(buf, x, ly, &truncate(&it.sub, sw as usize, ui.glyphs.ellipsis), t.dim().bg(bst.bg.unwrap_or(t.bg)), sw);
            ly += 1;
        }
        if focused && !it.actions.is_empty() {
            let mut ax = x;
            for (k, a) in it.actions.iter().enumerate() {
                let (l, rr) = br(ui);
                let label = format!("{l}{a}{rr}");
                let w = width(&label) as u16;
                if ax + w > area.x + area.width {
                    break;
                }
                let chip = if t.is_mono() { Style::default() } else { Style::default().fg(t.muted).bg(t.element) };
                put(buf, ax, ly, &label, chip, w);
                ui.hits.add(Rect::new(ax, ly, w, 1), &it.id, Part::Named(format!("action:{k}")));
                ax += w + 1;
            }
        }
        y += rh;
        drawn += 1;
    }
    let remaining = items.len().saturating_sub(st.scroll + drawn);
    if remaining > 0 && y < end {
        put(buf, area.x + 2, end - 1, &format!("{} {remaining} more", ui.glyphs.ellipsis), t.dim(), area.width);
    }
    drawn
}

#[derive(Default, Clone, Copy)]
pub struct OrderedItem<'a> {
    pub label: &'a str,
    /// Overrides the default tag (`in use` for the first row, `fallback` after).
    pub tag: &'a str,
    /// Extra note: "not connected", "no key", …; non-empty notes are warnings.
    pub note: &'a str,
}
pub fn ordered_list_height(n: usize) -> u16 {
    n as u16 + 1
}
/// Reorderable model list: row 1 is `in use`, others `fallback`. Rows are focus
/// stops `{id}:{i}`; the trailing row is `{id}:add`. Buttons carry parts up/down/remove.
pub fn ordered_list(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, items: &[OrderedItem], add_label: &str) -> u16 {
    let t = ui.theme;
    let g = ui.glyphs;
    // Tags line up in one column after the longest visible label.
    let label_w = items.iter().map(|i| width(i.label)).max().unwrap_or(0).min(area.width.saturating_sub(40) as usize) as u16;
    for (i, it) in items.iter().enumerate() {
        let y = area.y + i as u16;
        if y >= area.y + area.height {
            return i as u16;
        }
        let rid = format!("{id}:{i}");
        let r = ui.stop(&rid, Rect::new(area.x, y, area.width, 1));
        let st = row_style(ui, &rid, r.focused, t.surface);
        fill(buf, Rect::new(area.x, y, area.width, 1), st);
        if r.focused {
            focus_bar(buf, ui, area.x, y, 1);
        }
        let right = area.x + area.width;
        let btn_w = 3 * 3 + 2;
        put(buf, area.x + 2, y, &format!("{}", i + 1), st, 2);
        put(buf, area.x + 4, y, g.handle, t.dim().bg(st.bg.unwrap_or(t.bg)), 2);
        let tag = if !it.tag.is_empty() { it.tag } else if i == 0 { "in use" } else { "fallback" };
        let lab = if r.focused { st.add_modifier(Modifier::BOLD) } else { st };
        put(buf, area.x + 7, y, &truncate(it.label, label_w as usize, g.ellipsis), lab, label_w);
        let mut tx = area.x + 7 + label_w + 3;
        let tag_st = match tag {
            "in use" => t.strong(st.fg(t.success)),
            "skipped" => st.fg(t.warning),
            _ => t.dim().bg(st.bg.unwrap_or(t.bg)),
        };
        tx += put(buf, tx, y, tag, tag_st, 12) + 1;
        if !it.note.is_empty() {
            put(buf, tx + 1, y, &format!("· {}", it.note), st.fg(t.warning), 24);
        }
        let bx = right.saturating_sub(btn_w);
        for (k, (name, gl)) in [("up", g.up), ("down", g.down), ("remove", g.close)].iter().enumerate() {
            let x = bx + k as u16 * 4;
            let bst = if *name == "remove" { st.fg(t.error) } else { st.fg(t.muted) };
            let (l, rr) = br(ui);
            put(buf, x, y, &format!("{l}{gl}{rr}"), bst, 3);
            ui.hits.add(Rect::new(x, y, 3, 1), &rid, Part::Named((*name).into()));
        }
    }
    let ay = area.y + items.len() as u16;
    if ay < area.y + area.height {
        let aid = format!("{id}:add");
        let r = ui.stop(&aid, Rect::new(area.x, ay, area.width, 1));
        let st = row_style(ui, &aid, r.focused, t.surface);
        fill(buf, Rect::new(area.x, ay, area.width, 1), st);
        if r.focused {
            focus_bar(buf, ui, area.x, ay, 1);
        }
        put(buf, area.x + 7, ay, &if t.is_mono() { format!("[ + {add_label} ]") } else { format!("+ {add_label}") }, st.fg(t.accent), area.width.saturating_sub(7));
    }
    ordered_list_height(items.len())
}

/// Collapsible section header with a summary so collapsed sections hide nothing.
pub fn section(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, title: &str, summary: &str, summary_color: Option<Color>, open: bool) -> Response {
    let t = ui.theme;
    let r = ui.stop(id, Rect::new(area.x, area.y, area.width, 1));
    let st = row_style(ui, id, r.focused, t.bg);
    fill(buf, Rect::new(area.x, area.y, area.width, 1), st);
    if r.focused {
        focus_bar(buf, ui, area.x, area.y, 1);
    }
    let chev = if open { ui.glyphs.open } else { ui.glyphs.closed };
    put(buf, area.x + 1, area.y, chev, st.fg(t.muted), 1);
    let tw = put(buf, area.x + 3, area.y, title, st.add_modifier(Modifier::BOLD), area.width.saturating_sub(4));
    let sw = width(summary) as u16;
    let room = area.width.saturating_sub(tw + 6);
    if sw > 0 {
        let s = truncate(summary, room as usize, ui.glyphs.ellipsis);
        put_right(buf, area.x + area.width - 1, area.y, &s, Style::default().fg(summary_color.unwrap_or(t.muted)).bg(st.bg.unwrap_or(t.bg)));
    }
    r
}

pub fn scope_color(ui: &Ui, scope: &str) -> Color {
    let t = ui.theme;
    match scope {
        "global" => t.purple,
        "project" => t.cyan,
        "session" => t.blue,
        _ => t.quiet,
    }
}
pub fn badge(buf: &mut Buffer, ui: &Ui, right: u16, y: u16, text: &str, color: Color) -> u16 {
    let st = if ui.theme.is_mono() { Style::default().add_modifier(Modifier::DIM) } else { Style::default().fg(color) };
    put_right(buf, right, y, text, st)
}

pub const SCOPE_W: u16 = 9;

/// Label, control and scope on one row, description beneath. `control` draws into
/// the rect it is given (right-aligned, `control_w` wide) and learns if the row is focused.
pub fn setting_row(
    buf: &mut Buffer,
    ui: &mut Ui,
    area: Rect,
    id: &str,
    label: &str,
    description: &str,
    scope: &str,
    control_w: u16,
    control: impl FnOnce(&mut Buffer, &mut Ui, Rect, bool),
) -> u16 {
    let t = ui.theme;
    let h = 1 + (!description.is_empty()) as u16;
    let rect = Rect::new(area.x, area.y, area.width, h);
    let r = ui.stop(id, rect);
    let st = row_style(ui, id, r.focused, t.bg);
    fill(buf, rect, st);
    if r.focused {
        focus_bar(buf, ui, area.x, area.y, h);
    }
    let right = area.x + area.width;
    if !scope.is_empty() {
        badge(buf, ui, right - 1, area.y, scope, scope_color(ui, scope));
    }
    let cw = control_w.min(area.width.saturating_sub(SCOPE_W + 12));
    let cx = right.saturating_sub(SCOPE_W + 1 + cw);
    let lw = cx.saturating_sub(area.x + 3);
    let lab = if r.focused { st.add_modifier(Modifier::BOLD) } else { st };
    put(buf, area.x + 2, area.y, &truncate(label, lw as usize, ui.glyphs.ellipsis), lab, lw);
    control(buf, ui, Rect::new(cx, area.y, cw, 1), r.focused);
    if !description.is_empty() {
        let dw = area.width.saturating_sub(5);
        put(buf, area.x + 4, area.y + 1, &truncate(description, dw as usize, ui.glyphs.ellipsis), t.dim().bg(st.bg.unwrap_or(t.bg)), dw);
    }
    h
}

pub fn kv(buf: &mut Buffer, ui: &Ui, area: Rect, key: &str, value: &str, key_w: u16) {
    let t = ui.theme;
    put(buf, area.x, area.y, &truncate(key, key_w.saturating_sub(1) as usize, ui.glyphs.ellipsis), t.dim(), key_w);
    let vw = area.width.saturating_sub(key_w);
    put(buf, area.x + key_w, area.y, &truncate(value, vw as usize, ui.glyphs.ellipsis), Style::default().fg(t.text), vw);
}

/// Table with a header; a column of width 0 takes the remaining space.
pub fn table(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, cols: &[(&str, u16)], rows: &[Vec<String>], selected: usize, scroll: usize) -> usize {
    let t = ui.theme;
    let fixed: u16 = cols.iter().map(|c| c.1).sum::<u16>() + cols.len() as u16;
    let flex = area.width.saturating_sub(fixed + 2).max(4);
    let widths: Vec<u16> = cols.iter().map(|c| if c.1 == 0 { flex } else { c.1 }).collect();
    let mut x = area.x + 2;
    for (c, w) in cols.iter().zip(&widths) {
        put(buf, x, area.y, c.0, t.strong(Style::default().fg(t.muted).add_modifier(Modifier::BOLD)), *w);
        x += w + 1;
    }
    put(buf, area.x, area.y + 1, &ui.glyphs.rule.repeat(area.width as usize), Style::default().fg(t.border), area.width);
    let body = area.height.saturating_sub(2) as usize;
    for (k, row) in rows.iter().skip(scroll).take(body).enumerate() {
        let i = scroll + k;
        let y = area.y + 2 + k as u16;
        let rid = format!("{id}:{i}");
        let r = ui.stop(&rid, Rect::new(area.x, y, area.width, 1));
        let focused = r.focused || i == selected;
        let st = row_style(ui, &rid, focused, t.bg);
        fill(buf, Rect::new(area.x, y, area.width, 1), st);
        if focused {
            focus_bar(buf, ui, area.x, y, 1);
        }
        let mut x = area.x + 2;
        for (cell, w) in row.iter().zip(&widths) {
            put(buf, x, y, &truncate(cell, *w as usize, ui.glyphs.ellipsis), st, *w);
            x += w + 1;
        }
    }
    let more = rows.len().saturating_sub(scroll + body);
    if more > 0 {
        put(buf, area.x + 2, area.y + area.height - 1, &format!("{} {more} more", ui.glyphs.ellipsis), t.dim(), area.width);
    }
    rows.len().min(body)
}
