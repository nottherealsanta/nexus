//! Text input, select (closed + popup), search field.
use super::*;
use crate::hit::Part;
use crate::ui::{Response, Ui};
use ratatui::style::Modifier;

/// Single-line editor state (grapheme-aware cursor; secrets are masked on draw).
#[derive(Clone, Default, Debug)]
pub struct TextState {
    pub value: String,
    /// Cursor as a grapheme index.
    pub cursor: usize,
}
impl TextState {
    pub fn new(v: &str) -> Self {
        Self { value: v.to_string(), cursor: v.graphemes(true).count() }
    }
    fn byte_at(&self, g: usize) -> usize {
        self.value.grapheme_indices(true).nth(g).map(|(i, _)| i).unwrap_or(self.value.len())
    }
    pub fn insert(&mut self, c: char) {
        let at = self.byte_at(self.cursor);
        self.value.insert(at, c);
        self.cursor += 1;
    }
    pub fn backspace(&mut self) {
        if self.cursor == 0 {
            return;
        }
        let (a, b) = (self.byte_at(self.cursor - 1), self.byte_at(self.cursor));
        self.value.replace_range(a..b, "");
        self.cursor -= 1;
    }
    pub fn left(&mut self) {
        self.cursor = self.cursor.saturating_sub(1);
    }
    pub fn right(&mut self) {
        self.cursor = (self.cursor + 1).min(self.value.graphemes(true).count());
    }
    pub fn clear(&mut self) {
        self.value.clear();
        self.cursor = 0;
    }
}

pub struct TextInput<'a> {
    pub id: &'a str,
    pub state: &'a TextState,
    pub placeholder: &'a str,
    pub secret: bool,
    /// Inline validation message, drawn under the field by the caller's row.
    pub error: &'a str,
    pub editing: bool,
}

pub fn text_input(buf: &mut Buffer, ui: &mut Ui, x: u16, y: u16, w: u16, p: &TextInput) -> Response {
    let t = ui.theme;
    let r = ui.stop(p.id, Rect::new(x, y, w, 1));
    let bg = if r.focused { t.element_hi } else { t.element };
    let st = Style::default().fg(t.text).bg(bg);
    fill(buf, Rect::new(x, y, w, 1), st);
    let inner = w.saturating_sub(2);
    let shown: String = if p.state.value.is_empty() {
        String::new()
    } else if p.secret {
        "•".repeat(p.state.value.graphemes(true).count().min(inner as usize))
    } else {
        p.state.value.clone()
    };
    if shown.is_empty() {
        put(buf, x + 1, y, &truncate(p.placeholder, inner as usize, ui.glyphs.ellipsis), t.dim().bg(bg), inner);
    } else {
        put(buf, x + 1, y, &truncate(&shown, inner as usize, ui.glyphs.ellipsis), st, inner);
    }
    if p.editing || r.focused {
        let cx = x + 1 + (p.state.cursor as u16).min(inner.saturating_sub(1));
        if cx < x + w {
            buf[(cx, y)].set_style(st.add_modifier(Modifier::REVERSED));
        }
    }
    r
}

pub fn search_field(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, state: &TextState, count: Option<(usize, usize)>) -> Response {
    let t = ui.theme;
    let r = ui.stop(id, Rect::new(area.x, area.y, area.width, 1));
    let st = Style::default().fg(t.text).bg(if r.focused { t.element_hi } else { t.element });
    fill(buf, Rect::new(area.x, area.y, area.width, 1), st);
    put(buf, area.x + 1, area.y, ui.glyphs.search, t.dim().bg(st.bg.unwrap()), 1);
    let tail = count.map(|(a, b)| format!("{a} of {b}")).unwrap_or_default();
    let room = area.width.saturating_sub(4 + width(&tail) as u16);
    if state.value.is_empty() {
        put(buf, area.x + 3, area.y, "Search", t.dim().bg(st.bg.unwrap()), room);
    } else {
        put(buf, area.x + 3, area.y, &truncate(&state.value, room as usize, ui.glyphs.ellipsis), st, room);
    }
    if !tail.is_empty() {
        put_right(buf, area.x + area.width - 1, area.y, &tail, t.dim().bg(st.bg.unwrap()));
    }
    if r.focused {
        let cx = area.x + 3 + (state.cursor as u16).min(room.saturating_sub(1));
        buf[(cx, area.y)].set_style(st.add_modifier(Modifier::REVERSED));
    }
    r
}

/// Closed select drawn without registering a focus stop (for rows that are the stop).
pub fn select_view(buf: &mut Buffer, ui: &Ui, x: u16, y: u16, w: u16, value: &str, focused: bool, hover: f32) {
    let t = ui.theme;
    let mut st = Style::default().fg(t.text).bg(mix(t.element, t.element_hi, hover));
    if focused {
        st = st.add_modifier(Modifier::BOLD | Modifier::UNDERLINED);
    }
    fill(buf, Rect::new(x, y, w, 1), st);
    let (l, rr) = br(ui);
    put(buf, x, y, l, st, 1);
    let inner = w.saturating_sub(5) as usize;
    put(buf, x + 2, y, &truncate(value, inner, ui.glyphs.ellipsis), st, inner as u16);
    put(buf, x + w - 3, y, &format!(" {}{rr}", ui.glyphs.caret), st, 3);
}

/// Closed select: `[ value            ▾]`, right-aligned control of width `w`.
pub fn select(buf: &mut Buffer, ui: &mut Ui, x: u16, y: u16, w: u16, id: &str, value: &str) -> Response {
    let r = ui.stop(id, Rect::new(x, y, w, 1));
    let h = ui.hover_of(id);
    select_view(buf, ui, x, y, w, value, r.focused, h);
    r
}

/// Popup list under (or above) `anchor`. Rows are focus stops `{id}:{i}` so keys
/// and mouse share the focus ring; more than `max_rows` scrolls and announces.
pub fn select_popup(buf: &mut Buffer, ui: &mut Ui, bounds: Rect, anchor: Rect, id: &str, options: &[&str], current: usize, highlighted: usize, max_rows: u16) -> Rect {
    let t = ui.theme;
    let n = options.len() as u16;
    let shown = n.min(max_rows);
    let more = n > shown;
    let h = shown + 2 + if more { 1 } else { 0 };
    let w = options.iter().map(|o| width(o) as u16).max().unwrap_or(8).max(anchor.width.saturating_sub(2)) + 6;
    let w = w.min(bounds.width);
    let below = anchor.y + 1 + h <= bounds.y + bounds.height;
    let y = if below { anchor.y + 1 } else { anchor.y.saturating_sub(h) };
    let x = (anchor.x + anchor.width).saturating_sub(w).max(bounds.x);
    let rect = Rect::new(x, y, w, h);
    fill(buf, rect, Style::default().fg(t.text).bg(t.raised));
    draw_box(buf, ui, rect, t.border_strong);
    let start = if highlighted >= shown as usize { highlighted + 1 - shown as usize } else { 0 };
    for k in 0..shown as usize {
        let i = start + k;
        let Some(o) = options.get(i) else { break };
        let oid = format!("{id}:{i}");
        let row = Rect::new(x + 1, y + 1 + k as u16, w - 2, 1);
        let focused = i == highlighted;
        ui.hits.add(row, &oid, Part::Body);
        let st = Style::default().fg(t.text).bg(if focused { t.focus_bg } else { t.raised });
        fill(buf, row, st);
        if focused {
            focus_bar(buf, ui, row.x, row.y, 1);
        }
        let mark = if i == current { ui.glyphs.dot_ok } else { " " };
        put(buf, row.x + 2, row.y, mark, st.fg(t.accent), 1);
        put(buf, row.x + 4, row.y, &truncate(o, (row.width - 5) as usize, ui.glyphs.ellipsis), if focused { st.add_modifier(Modifier::BOLD) } else { st }, row.width - 5);
    }
    if more {
        let left = options.len() - (start + shown as usize);
        put(buf, x + 2, y + 1 + shown, &format!("{} {} more", ui.glyphs.ellipsis, left.max(start)), t.dim().bg(t.raised), w - 4);
    }
    rect
}

/// Light single-line box using the glyph set's rule characters.
pub fn draw_box(buf: &mut Buffer, ui: &Ui, r: Rect, color: ratatui::style::Color) {
    if r.width < 2 || r.height < 2 {
        return;
    }
    let st = Style::default().fg(color).bg(ui.theme.raised);
    let (h, v, tl, tr, bl, br) = if ui.glyphs.ascii { ("-", "|", "+", "+", "+", "+") } else { ("─", "│", "┌", "┐", "└", "┘") };
    put(buf, r.x, r.y, &format!("{tl}{}{tr}", h.repeat(r.width as usize - 2)), st, r.width);
    put(buf, r.x, r.y + r.height - 1, &format!("{bl}{}{br}", h.repeat(r.width as usize - 2)), st, r.width);
    for y in r.y + 1..r.y + r.height - 1 {
        put(buf, r.x, y, v, st, 1);
        put(buf, r.x + r.width - 1, y, v, st, 1);
    }
}
