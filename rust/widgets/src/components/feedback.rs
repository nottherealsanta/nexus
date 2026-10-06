//! Toasts, meter, progress, callout, empty state, key hints, modal, scrollbar.
use super::*;
use crate::hit::Part;
use crate::theme::Level;
use crate::ui::Ui;
use ratatui::style::Modifier;
use std::time::{Duration, Instant};

pub const MAX_VISIBLE: usize = 3;
pub const MAX_KEPT: usize = 20;

#[derive(Clone, Debug)]
pub struct Toast {
    pub id: u64,
    pub level: Level,
    pub title: String,
    pub body: String,
    pub key: String,
    pub action: String,
    pub count: u32,
    pub age: Duration,
    pub born: Duration,
}
impl Toast {
    pub fn lifetime(&self) -> Duration {
        Duration::from_secs(match self.level {
            Level::Info => 4,
            Level::Success => 3,
            Level::Warning => 8,
            Level::Error => 12,
        })
    }
}

/// Toast queue with a local clock: timers pause while hovered, same-key toasts
/// within 2 s merge (`×2`), at most [`MAX_KEPT`] are retained.
#[derive(Default)]
pub struct ToastStack {
    pub toasts: Vec<Toast>,
    pub history: Vec<Toast>,
    next: u64,
    last_id: u64,
    last_tick: Option<Instant>,
    clock: Duration,
}
impl ToastStack {
    /// Add a toast whose id is assigned by the producer (the Python host); ids at or
    /// below `last_id` were already shown and are ignored. Returns true when added.
    pub fn ingest(&mut self, id: u64, level: Level, title: &str, body: &str, key: &str, action: &str) -> bool {
        if id <= self.last_id {
            return false;
        }
        self.last_id = id;
        self.next = self.next.max(id);
        self.push_inner(Some(id), level, title, body, key, action);
        true
    }
    pub fn push(&mut self, level: Level, title: &str, body: &str, key: &str, action: &str) -> u64 {
        self.push_inner(None, level, title, body, key, action)
    }
    fn push_inner(&mut self, id: Option<u64>, level: Level, title: &str, body: &str, key: &str, action: &str) -> u64 {
        if !key.is_empty() {
            let clock = self.clock;
            if let Some(t) = self.toasts.iter_mut().find(|t| t.key == key && clock.saturating_sub(t.born) < Duration::from_secs(2)) {
                t.count += 1;
                t.age = Duration::ZERO;
                t.title = title.to_string();
                t.body = body.to_string();
                return t.id;
            }
        }
        let id = match id {
            Some(id) => id,
            None => {
                self.next += 1;
                self.next
            }
        };
        let t = Toast { id, level, title: title.into(), body: body.into(), key: key.into(), action: action.into(), count: 1, age: Duration::ZERO, born: self.clock };
        self.history.push(t.clone());
        if self.history.len() > 50 {
            self.history.remove(0);
        }
        self.toasts.push(t);
        if self.toasts.len() > MAX_KEPT {
            self.toasts.remove(0);
        }
        id
    }
    /// Advance timers; `hovered` pauses that toast. Returns true if anything expired.
    pub fn tick(&mut self, now: Instant, hovered: Option<u64>) -> bool {
        let dt = self.last_tick.map(|t| now.saturating_duration_since(t)).unwrap_or_default();
        self.last_tick = Some(now);
        self.clock += dt;
        let before = self.toasts.len();
        for t in self.toasts.iter_mut() {
            if Some(t.id) != hovered {
                t.age += dt;
            }
        }
        self.toasts.retain(|t| t.age < t.lifetime());
        self.toasts.len() != before
    }
    pub fn dismiss(&mut self, id: u64) {
        self.toasts.retain(|t| t.id != id);
    }
    pub fn dismiss_all(&mut self) {
        self.toasts.clear();
    }
    pub fn visible(&self) -> impl Iterator<Item = &Toast> {
        let n = self.toasts.len();
        self.toasts.iter().skip(n.saturating_sub(MAX_VISIBLE)).rev()
    }
    pub fn hidden(&self) -> usize {
        self.toasts.len().saturating_sub(MAX_VISIBLE)
    }
}

pub fn toast_height(t: &Toast) -> u16 {
    1 + (!t.body.is_empty()) as u16 + (!t.action.is_empty()) as u16 + 1
}
/// Where one toast sits, plus its close button and action button (if any).
#[derive(Clone, Copy, Debug)]
pub struct ToastRect {
    pub id: u64,
    pub rect: Rect,
    pub close: Rect,
    pub action: Option<Rect>,
}
/// Pure layout, shared by drawing and mouse hit-testing so they cannot disagree.
/// Floats at the top-right of `area`; never covers more than `area`.
pub fn toast_layout(area: Rect, stack: &ToastStack) -> Vec<ToastRect> {
    let narrow = area.width < 90;
    let w = if narrow { area.width.saturating_sub(2) } else { (area.width * 40 / 100).clamp(32, 48) }.min(area.width);
    let margin = if narrow { 1 } else { 2 }.min(area.width - w);
    let x = area.x + area.width - w - margin;
    let mut y = area.y;
    let mut out = Vec::new();
    for to in stack.visible() {
        let h = toast_height(to);
        if y + h > area.y + area.height {
            break;
        }
        let action = (!to.action.is_empty()).then(|| Rect::new(x + 4, y + 1 + (!to.body.is_empty()) as u16, (width(&to.action) + 2) as u16, 1));
        out.push(ToastRect { id: to.id, rect: Rect::new(x, y, w, h), close: Rect::new(x + w.saturating_sub(4), y, 3, 1), action });
        y += h;
    }
    out
}

/// Draw the stack floating at the top-right of `area` (plan §8.1). Never reflows
/// the page: it paints over cells. Returns the rects drawn (newest first).
pub fn toast_stack(buf: &mut Buffer, ui: &mut Ui, area: Rect, stack: &ToastStack) -> Vec<(u64, Rect)> {
    let t = ui.theme;
    let g = ui.glyphs;
    let narrow = area.width < 90;
    let w = if narrow { area.width.saturating_sub(2) } else { (area.width * 40 / 100).clamp(32, 48) }.min(area.width);
    let x = area.x + area.width - w - if narrow { 1 } else { 2 }.min(area.width - w);
    let mut y = area.y;
    let mut out = Vec::new();
    for to in stack.visible() {
        let h = toast_height(to);
        if y + h > area.y + area.height {
            break;
        }
        let rect = Rect::new(x, y, w, h);
        let st = Style::default().fg(t.text).bg(t.raised);
        fill(buf, rect, st);
        let c = to.level.color(t);
        let bar = t.strong(Style::default().fg(c).bg(t.raised));
        for k in 0..h {
            put(buf, x, y + k, g.focus_bar, bar, 1);
        }
        let glyph = match to.level {
            Level::Info => g.info,
            Level::Success => g.ok,
            Level::Warning => g.warn,
            Level::Error => g.err,
        };
        put(buf, x + 2, y, glyph, bar.add_modifier(Modifier::BOLD), 1);
        let count = if to.count > 1 { format!("  ×{}", to.count) } else { String::new() };
        let tw = w.saturating_sub(9);
        let title = truncate(&to.title, tw as usize, g.ellipsis);
        let used = put(buf, x + 4, y, &title, st.add_modifier(Modifier::BOLD), tw);
        put(buf, x + 4 + used, y, &count, t.dim().bg(t.raised), 6);
        let id = format!("toast:{}", to.id);
        let (l, rr) = br(ui);
        put(buf, x + w - 4, y, &format!("{l}{}{rr}", g.close), Style::default().fg(t.muted).bg(t.raised), 3);
        // Body first: later registrations win, so the close button stays clickable.
        ui.hits.add(rect, &id, Part::Body);
        ui.hits.add(Rect::new(x + w - 4, y, 3, 1), &id, Part::Named("close".into()));
        let mut ly = y + 1;
        if !to.body.is_empty() {
            put(buf, x + 4, ly, &truncate(&to.body, (w - 6) as usize, g.ellipsis), t.dim().bg(t.raised), w - 6);
            ly += 1;
        }
        if !to.action.is_empty() {
            let label = if t.is_mono() { format!("[ {} ]", to.action) } else { format!(" {} ", to.action) };
            let ast = if t.is_mono() { Style::default() } else { Style::default().fg(t.accent).bg(t.element) };
            put(buf, x + 4, ly, &label, ast, w - 6);
            ui.hits.add(Rect::new(x + 4, ly, width(&label) as u16, 1), &id, Part::Named("action".into()));
            ly += 1;
        }
        // Progress hairline: shortens as the lifetime runs out.
        let left = 1.0 - (to.age.as_secs_f32() / to.lifetime().as_secs_f32()).clamp(0.0, 1.0);
        let n = ((w - 2) as f32 * left).round() as usize;
        let rule = if g.ascii { "-" } else { "─" };
        // Deliberately faint: a hint of time left, not an animation to watch.
        let faint = if t.is_mono() { Style::default().bg(t.raised).add_modifier(Modifier::DIM) } else { Style::default().fg(crate::theme::mix(t.raised, c, 0.22)).bg(t.raised) };
        put(buf, x + 1, ly, &rule.repeat(n), faint, w - 2);
        out.push((to.id, rect));
        y += h;
    }
    let hidden = stack.hidden();
    if hidden > 0 && y < area.y + area.height {
        let rect = Rect::new(x, y, w, 1);
        fill(buf, rect, Style::default().bg(t.raised));
        put(buf, x + 2, y, &format!("+{hidden} more"), t.dim().bg(t.raised), w - 3);
        ui.hits.add(rect, "toast:more", Part::Body);
    }
    out
}

/// `▕████▌   ▏ 46%` style meter; `marks` are threshold fractions (0..1).
pub fn meter(buf: &mut Buffer, ui: &Ui, x: u16, y: u16, w: u16, fraction: f32, marks: &[f32]) {
    let t = ui.theme;
    let n = w as usize;
    let filled = (n as f32 * fraction.clamp(0.0, 1.0)).round() as usize;
    let color = if fraction > 0.9 { t.error } else if fraction > 0.7 { t.warning } else { t.accent };
    for i in 0..n {
        let on = i < filled;
        let mark = marks.iter().any(|m| ((n as f32 * m).round() as usize) == i);
        let (s, st) = if mark {
            (if ui.glyphs.ascii { "|" } else { "┊" }, Style::default().fg(t.muted))
        } else if on {
            (ui.glyphs.bar_full, Style::default().fg(color))
        } else {
            (ui.glyphs.bar_empty, Style::default().fg(t.border_strong))
        };
        put(buf, x + i as u16, y, s, st, 1);
    }
}
pub fn progress(buf: &mut Buffer, ui: &Ui, area: Rect, fraction: f32, label: &str) {
    let lw = width(label) as u16 + 1;
    let bw = area.width.saturating_sub(lw + 6).max(4);
    meter(buf, ui, area.x, area.y, bw, fraction, &[]);
    put(buf, area.x + bw + 1, area.y, &format!("{:>3}%", (fraction * 100.0).round() as u32), Style::default().fg(ui.theme.text), 4);
    put(buf, area.x + bw + 6, area.y, label, ui.theme.dim(), area.width.saturating_sub(bw + 6));
}

pub fn spinner_row(buf: &mut Buffer, ui: &Ui, area: Rect, text: &str) {
    put(buf, area.x, area.y, ui.spinner(), Style::default().fg(ui.theme.accent), 1);
    put(buf, area.x + 2, area.y, &truncate(text, area.width.saturating_sub(2) as usize, ui.glyphs.ellipsis), ui.theme.dim(), area.width);
}

/// Inline persistent condition (not a toast). Optional action button label.
pub fn callout(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, level: Level, text: &str, action: &str) {
    let t = ui.theme;
    let c = level.color(t);
    let g = ui.glyphs;
    let glyph = match level {
        Level::Info => g.info,
        Level::Success => g.ok,
        Level::Warning => g.warn,
        Level::Error => g.err,
    };
    put(buf, area.x, area.y, glyph, t.strong(Style::default().fg(c).add_modifier(Modifier::BOLD)), 1);
    let aw = if action.is_empty() { 0 } else { width(action) as u16 + 6 };
    let tw = area.width.saturating_sub(3 + aw);
    put(buf, area.x + 2, area.y, &truncate(text, tw as usize, g.ellipsis), Style::default().fg(t.text), tw);
    if !action.is_empty() {
        button(buf, ui, area.x + area.width - aw + 1, area.y, &Button::new(id, action, ButtonKind::Secondary));
    }
}

pub fn empty_state(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, message: &str, action: &str) {
    let mw = width(message) as u16;
    let y = area.y + area.height / 2;
    put(buf, area.x + area.width.saturating_sub(mw) / 2, y, message, ui.theme.dim(), area.width);
    if !action.is_empty() && y + 2 < area.y + area.height {
        let bw = button_width(action);
        button(buf, ui, area.x + area.width.saturating_sub(bw) / 2, y + 2, &Button::new(id, action, ButtonKind::Primary));
    }
}

/// One-row hint bar: `key label   key label`, clipped from the right with an ellipsis.
pub fn key_hints(buf: &mut Buffer, ui: &Ui, area: Rect, hints: &[(&str, &str)]) {
    let t = ui.theme;
    let mut x = area.x + 1;
    let end = area.x + area.width;
    for (k, l) in hints {
        let need = (width(k) + 1 + width(l) + 3) as u16;
        if x + need > end {
            put(buf, x, area.y, ui.glyphs.ellipsis, t.dim(), 1);
            return;
        }
        x += put(buf, x, area.y, k, Style::default().fg(t.text).add_modifier(Modifier::BOLD), 20) + 1;
        x += put(buf, x, area.y, l, t.dim(), 40) + 3;
    }
}

pub fn scrollbar(buf: &mut Buffer, ui: &Ui, x: u16, y: u16, h: u16, total: usize, offset: usize, visible: usize) {
    if total <= visible || h == 0 {
        return;
    }
    let t = ui.theme;
    let thumb = ((h as usize * visible) / total).max(1) as u16;
    let top = ((h as usize - thumb as usize) * offset / (total - visible).max(1)) as u16;
    let (track, th) = if ui.glyphs.ascii { ("|", "#") } else { ("│", "┃") };
    for k in 0..h {
        let on = k >= top && k < top + thumb;
        put(buf, x, y + k, if on { th } else { track }, Style::default().fg(if on { t.muted } else { t.border }), 1);
    }
}

pub struct Modal<'a> {
    pub title: &'a str,
    pub id: &'a str,
}
/// Draw a modal frame: title left, `[×] esc` right, hint row at the bottom.
/// Returns (inner content rect, hint row rect).
pub fn modal(buf: &mut Buffer, ui: &mut Ui, area: Rect, m: &Modal) -> (Rect, Rect) {
    let t = ui.theme;
    let g = ui.glyphs;
    fill(buf, area, Style::default().fg(t.text).bg(t.raised));
    draw_box(buf, ui, area, t.border_strong);
    put(buf, area.x + 2, area.y, &format!(" {} ", truncate(m.title, area.width.saturating_sub(16) as usize, g.ellipsis)), Style::default().fg(t.text).bg(t.raised).add_modifier(Modifier::BOLD), area.width - 4);
    let (l, rr) = br(ui);
    let close = if ui.theme.is_mono() { format!(" {l}{}{rr} esc ", g.close) } else { format!(" {} esc ", g.close) };
    put_right(buf, area.x + area.width - 2, area.y, &close, Style::default().fg(t.muted).bg(t.raised));
    ui.hits.add(Rect::new(area.x + area.width - 2 - width(&close) as u16, area.y, width(&close) as u16, 1), m.id, Part::Named("close".into()));
    let inner = Rect::new(area.x + 1, area.y + 1, area.width - 2, area.height.saturating_sub(3));
    let hints = Rect::new(area.x + 1, area.y + area.height - 2, area.width - 2, 1);
    (inner, hints)
}
