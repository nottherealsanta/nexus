//! Components (plan §7). Grouped by family:
//! `controls` (button, toggle, checkbox, radio, segmented, tabs, stepper),
//! `inputs` (text input, select, search), `lists` (list, ordered list, section,
//! setting row, key/value, table, badge), `feedback` (toast, meter, progress,
//! callout, empty state, key hints, modal, scrollbar).
use crate::{theme::mix, ui::Ui};
use ratatui::{buffer::Buffer, layout::Rect, style::Style};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub mod controls;
pub mod feedback;
pub mod inputs;
pub mod lists;

pub use controls::*;
pub use feedback::*;
pub use inputs::*;
pub use lists::*;

pub fn width(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

/// Fit `s` into `w` cells, ending in `ell` when clipped (clipping is announced).
pub fn truncate(s: &str, w: usize, ell: &str) -> String {
    if width(s) <= w {
        return s.to_string();
    }
    let ew = width(ell);
    if w <= ew {
        return ell.chars().take(w).collect();
    }
    let mut out = String::new();
    let mut used = 0;
    for g in s.graphemes(true) {
        let gw = width(g);
        if used + gw + ew > w {
            break;
        }
        out.push_str(g);
        used += gw;
    }
    out.push_str(ell);
    out
}

/// Write `s` at (x, y) clipped to `max` cells and to the buffer; returns cells used.
pub fn put(buf: &mut Buffer, x: u16, y: u16, s: &str, style: Style, max: u16) -> u16 {
    let area = buf.area;
    if y < area.y || y >= area.y + area.height || x >= area.x + area.width {
        return 0;
    }
    let max = max.min(area.x + area.width - x);
    let mut used = 0u16;
    for g in s.graphemes(true) {
        let gw = width(g) as u16;
        if gw == 0 {
            continue;
        }
        if used + gw > max {
            break;
        }
        buf[(x + used, y)].set_symbol(g).set_style(style);
        for k in 1..gw {
            buf[(x + used + k, y)].set_symbol("").set_style(style);
        }
        used += gw;
    }
    used
}

/// Right-aligned text ending at `right` (exclusive).
pub fn put_right(buf: &mut Buffer, right: u16, y: u16, s: &str, style: Style) -> u16 {
    let w = width(s) as u16;
    let x = right.saturating_sub(w);
    put(buf, x, y, s, style, w)
}

pub fn fill(buf: &mut Buffer, r: Rect, style: Style) {
    let a = buf.area;
    for y in r.y..(r.y + r.height).min(a.y + a.height) {
        for x in r.x..(r.x + r.width).min(a.x + a.width) {
            buf[(x, y)].set_symbol(" ").set_style(style);
        }
    }
}

/// Row background for the focus/hover state (colour only, never geometry).
pub fn row_style(ui: &Ui, id: &str, focused: bool, base_bg: ratatui::style::Color) -> Style {
    let t = ui.theme;
    let h = ui.hover_of(id);
    let bg = if focused {
        t.focus_bg
    } else {
        mix(base_bg, t.element_hi, h * 0.6)
    };
    Style::default().fg(t.text).bg(bg)
}

/// Draw the focus bar at column `x` for `rows` rows starting at `y`.
pub fn focus_bar(buf: &mut Buffer, ui: &Ui, x: u16, y: u16, rows: u16) {
    let st = ui.theme.strong(Style::default().fg(ui.theme.accent));
    for k in 0..rows {
        put(buf, x, y + k, ui.glyphs.focus_bar, st, 1);
    }
}
