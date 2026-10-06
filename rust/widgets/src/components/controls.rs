//! Button, icon button, toggle, checkbox, radio, segmented, tabs, stepper.
use super::*;
use crate::hit::Part;
use crate::ui::{Response, Ui};
use ratatui::style::Modifier;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ButtonKind {
    Primary,
    Secondary,
    Danger,
    Ghost,
}

pub struct Button<'a> {
    pub id: &'a str,
    pub label: &'a str,
    pub kind: ButtonKind,
    pub disabled: bool,
    pub loading: bool,
}
impl<'a> Button<'a> {
    pub fn new(id: &'a str, label: &'a str, kind: ButtonKind) -> Self {
        Self { id, label, kind, disabled: false, loading: false }
    }
}
pub fn button_width(label: &str) -> u16 {
    (width(label) + 4) as u16
}

/// `[ label ]`. Loading keeps the width and swaps the label for a spinner.
pub fn button(buf: &mut Buffer, ui: &mut Ui, x: u16, y: u16, b: &Button) -> Response {
    let t = ui.theme;
    let w = button_width(b.label);
    let rect = Rect::new(x, y, w, 1);
    let r = ui.stop(b.id, rect);
    let h = ui.hover_of(b.id);
    let mut st = match b.kind {
        ButtonKind::Primary => Style::default().fg(t.bg).bg(mix(t.accent, t.text, h * 0.2)),
        ButtonKind::Secondary => Style::default().fg(t.text).bg(mix(t.element, t.element_hi, h)),
        ButtonKind::Danger => Style::default().fg(t.error).bg(mix(t.element, t.element_hi, h)),
        ButtonKind::Ghost => Style::default().fg(t.muted).bg(mix(t.surface, t.element_hi, h)),
    };
    if t.is_mono() {
        st = Style::default();
        if b.kind == ButtonKind::Primary {
            st = st.add_modifier(Modifier::BOLD);
        }
    }
    if b.disabled {
        st = t.quiet_style();
    }
    if r.focused {
        st = st.add_modifier(Modifier::REVERSED | Modifier::BOLD);
    }
    let label = if b.loading { ui.spinner().to_string() } else { b.label.to_string() };
    let inner = w as usize - 4;
    let (l, rr) = br(ui);
    let shown = format!("{l} {:^inner$} {rr}", truncate(&label, inner, ui.glyphs.ellipsis), inner = inner);
    put(buf, x, y, &shown, st, w);
    r
}

/// `[×]`, `[↑]`: three cells. `label` is for hint bars, not drawn.
pub fn icon_button(buf: &mut Buffer, ui: &mut Ui, x: u16, y: u16, id: &str, glyph: &str, danger: bool) -> Response {
    let t = ui.theme;
    let r = ui.stop(id, Rect::new(x, y, 3, 1));
    let h = ui.hover_of(id);
    let mut st = Style::default().fg(if danger { t.error } else { t.muted }).bg(mix(t.element, t.element_hi, h));
    let (l, rr) = br(ui);
    if r.focused {
        st = st.add_modifier(Modifier::REVERSED | Modifier::BOLD);
    }
    put(buf, x, y, &format!("{l}{glyph}{rr}"), st, 3);
    r
}

pub const TOGGLE_W: u16 = 8;

/// Pure drawing of a switch, right-aligned into an 8-cell box at `x`.
pub fn toggle_view(buf: &mut Buffer, ui: &Ui, x: u16, y: u16, on: bool, locked: bool, focused: bool) {
    let t = ui.theme;
    let g = ui.glyphs;
    let (l, rr) = br(ui);
    let chip = |fg, bg| Style::default().fg(fg).bg(bg);
    let (text, mut st) = if locked {
        (format!("{l}LOCKED{rr}"), if t.is_mono() { t.quiet_style() } else { chip(t.quiet, t.element) })
    } else if on {
        (format!("{l}{}{rr}", g.toggle_on), if t.is_mono() { t.strong(Style::default()) } else { chip(t.success, mix(t.surface, t.success, 0.22)).add_modifier(Modifier::BOLD) })
    } else {
        (format!("{l}{}{rr}", g.toggle_off), if t.is_mono() { t.dim() } else { chip(t.muted, t.element) })
    };
    if on && t.is_mono() {
        st = st.add_modifier(Modifier::BOLD);
    }
    if focused {
        st = st.add_modifier(Modifier::BOLD | Modifier::UNDERLINED);
    }
    let w = width(&text) as u16;
    put(buf, x + TOGGLE_W - w, y, &text, st, w);
}

pub struct Toggle<'a> {
    pub id: &'a str,
    pub label: &'a str,
    pub on: bool,
    /// Some(reason) draws LOCKED; the reason belongs in the description/hint bar.
    pub locked: Option<&'a str>,
}
/// Label left, switch right-aligned at the end of `area`.
pub fn toggle(buf: &mut Buffer, ui: &mut Ui, area: Rect, p: &Toggle) -> Response {
    let r = ui.stop(p.id, Rect::new(area.x, area.y, area.width, 1));
    let st = row_style(ui, p.id, r.focused, ui.theme.surface);
    fill(buf, Rect::new(area.x, area.y, area.width, 1), st);
    if r.focused {
        focus_bar(buf, ui, area.x, area.y, 1);
    }
    let lw = area.width.saturating_sub(TOGGLE_W + 4);
    let label_st = if r.focused { st.add_modifier(Modifier::BOLD) } else { st };
    put(buf, area.x + 2, area.y, &truncate(p.label, lw as usize, ui.glyphs.ellipsis), label_st, lw);
    toggle_view(buf, ui, area.x + area.width - TOGGLE_W - 1, area.y, p.on, p.locked.is_some(), r.focused);
    r
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Check {
    Off,
    On,
    Mixed,
}
pub fn checkbox(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, label: &str, state: Check) -> Response {
    let r = ui.stop(id, Rect::new(area.x, area.y, area.width, 1));
    let st = row_style(ui, id, r.focused, ui.theme.surface);
    fill(buf, Rect::new(area.x, area.y, area.width, 1), st);
    if r.focused {
        focus_bar(buf, ui, area.x, area.y, 1);
    }
    let g = ui.glyphs;
    let mark = match state {
        Check::Off => g.check_off,
        Check::On => g.check_on,
        Check::Mixed => g.check_mixed,
    };
    let mst = if state == Check::Off { st } else { st.fg(ui.theme.accent) };
    let mw = put(buf, area.x + 2, area.y, mark, mst, 3);
    let lst = if r.focused { st.add_modifier(Modifier::BOLD) } else { st };
    put(buf, area.x + 3 + mw, area.y, &truncate(label, area.width.saturating_sub(5 + mw) as usize, g.ellipsis), lst, area.width);
    r
}

pub struct RadioOption<'a> {
    pub label: &'a str,
    pub detail: &'a str,
}
/// Vertical radio group; each option is a focus stop `{id}:{i}`. Rows used: 1 per option.
pub fn radio_group(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, opts: &[RadioOption], selected: usize) -> u16 {
    for (i, o) in opts.iter().enumerate() {
        let oid = format!("{id}:{i}");
        let y = area.y + i as u16;
        if y >= area.y + area.height {
            break;
        }
        let r = ui.stop(&oid, Rect::new(area.x, y, area.width, 1));
        let st = row_style(ui, &oid, r.focused, ui.theme.surface);
        fill(buf, Rect::new(area.x, y, area.width, 1), st);
        if r.focused {
            focus_bar(buf, ui, area.x, y, 1);
        }
        let g = ui.glyphs;
        let on = i == selected;
        let m = put(buf, area.x + 2, y, if on { g.radio_on } else { g.radio_off }, if on { st.fg(ui.theme.accent) } else { st }, 3);
        let lab = if r.focused || on { st.add_modifier(Modifier::BOLD) } else { st };
        let lw = put(buf, area.x + 3 + m, y, o.label, lab, area.width);
        if !o.detail.is_empty() {
            let dx = area.x + 5 + m + lw;
            let room = (area.x + area.width).saturating_sub(dx);
            put(buf, dx, y, &truncate(o.detail, room as usize, g.ellipsis), ui.theme.dim().bg(st.bg.unwrap_or(ui.theme.bg)), room);
        }
    }
    opts.len() as u16
}

pub fn segmented_width(opts: &[&str]) -> u16 {
    let inner: usize = opts.iter().map(|o| width(o) + 2).sum::<usize>() + opts.len().saturating_sub(1);
    (inner + 2) as u16
}
/// `[ Dark │ Light │ System ]`; `focused` is the focused segment when the control has focus.
pub fn segmented_view(buf: &mut Buffer, ui: &mut Ui, x: u16, y: u16, id: &str, opts: &[&str], active: usize, focused: Option<usize>) -> u16 {
    let t = ui.theme;
    let sep = if ui.glyphs.ascii { "|" } else { "│" };
    let mut cx = x;
    let (l, rr) = br(ui);
    let strip = if t.is_mono() { Style::default() } else { Style::default().bg(t.element) };
    put(buf, cx, y, l, t.dim().patch(strip), 1);
    cx += 1;
    for (i, o) in opts.iter().enumerate() {
        if i > 0 {
            put(buf, cx, y, if t.is_mono() { sep } else { " " }, t.dim().patch(strip), 1);
            cx += 1;
        }
        let w = (width(o) + 2) as u16;
        let mut st = if i == active {
            if t.is_mono() {
                Style::default().add_modifier(Modifier::REVERSED)
            } else {
                Style::default().fg(t.bg).bg(t.accent)
            }
        } else {
            Style::default().fg(t.text).patch(strip)
        };
        if focused == Some(i) {
            st = st.add_modifier(Modifier::BOLD | Modifier::UNDERLINED);
        }
        put(buf, cx, y, &format!(" {o} "), st, w);
        ui.hits.add(Rect::new(cx, y, w, 1), id, Part::Named(format!("segment:{i}")));
        cx += w;
    }
    put(buf, cx, y, rr, t.dim().patch(strip), 1);
    cx + 1 - x
}
pub fn segmented(buf: &mut Buffer, ui: &mut Ui, x: u16, y: u16, id: &str, opts: &[&str], active: usize) -> Response {
    let w = segmented_width(opts);
    let r = ui.stop(id, Rect::new(x, y, w, 1));
    segmented_view(buf, ui, x, y, id, opts, active, if r.focused { Some(active) } else { None });
    r
}

pub struct Tab<'a> {
    pub label: &'a str,
    pub badge: &'a str,
}
/// Two rows: labels, then a rule with a heavy underline under the active tab.
pub fn tabs(buf: &mut Buffer, ui: &mut Ui, area: Rect, id: &str, tabs: &[Tab], active: usize) -> Response {
    let t = ui.theme;
    let r = ui.stop(id, Rect::new(area.x, area.y, area.width, 2));
    put(buf, area.x, area.y + 1, &ui.glyphs.rule.repeat(area.width as usize), Style::default().fg(t.border), area.width);
    let mut cx = area.x + 1;
    for (i, tab) in tabs.iter().enumerate() {
        let text = if tab.badge.is_empty() { tab.label.to_string() } else { format!("{} {}", tab.label, tab.badge) };
        let w = width(&text) as u16;
        let on = i == active;
        let mut st = if on { t.strong(Style::default().fg(t.accent)) } else { Style::default().fg(t.muted) };
        if on {
            st = st.add_modifier(Modifier::BOLD);
        }
        if r.focused && on {
            st = st.add_modifier(Modifier::UNDERLINED);
        }
        put(buf, cx, area.y, &text, st, w);
        if on {
            put(buf, cx.saturating_sub(1), area.y + 1, &ui.glyphs.tab_rule.repeat(w as usize + 2), Style::default().fg(t.accent), w + 2);
        }
        ui.hits.add(Rect::new(cx, area.y, w, 2), id, Part::Named(format!("tab:{i}")));
        cx += w + 3;
    }
    r
}

pub fn stepper_width(value: &str) -> u16 {
    (width(value) + 14) as u16
}
/// `[ − ]  60 s  [ + ]`
pub fn stepper(buf: &mut Buffer, ui: &mut Ui, x: u16, y: u16, id: &str, value: &str) -> Response {
    let t = ui.theme;
    let w = stepper_width(value);
    let r = ui.stop(id, Rect::new(x, y, w, 1));
    let minus = if ui.glyphs.ascii { "-" } else { "−" };
    let mut st = Style::default().fg(t.text).bg(t.element);
    if r.focused {
        st = st.add_modifier(Modifier::BOLD | Modifier::UNDERLINED);
    }
    let (l, rr) = br(ui);
    put(buf, x, y, &format!("{l} {minus} {rr}"), st, 5);
    ui.hits.add(Rect::new(x, y, 5, 1), id, Part::Named("dec".into()));
    put(buf, x + 5, y, &format!("  {value}  "), Style::default().fg(t.text), width(value) as u16 + 4);
    let px = x + 5 + width(value) as u16 + 4;
    put(buf, px, y, &format!("{l} + {rr}"), st, 5);
    ui.hits.add(Rect::new(px, y, 5, 1), id, Part::Named("inc".into()));
    r
}
