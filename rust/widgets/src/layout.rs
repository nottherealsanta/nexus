//! Breakpoints and small geometry helpers (plan §4.8, §5.3).
use ratatui::layout::Rect;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Size {
    Narrow,
    Medium,
    Wide,
}
pub fn size_of(width: u16) -> Size {
    if width < 90 {
        Size::Narrow
    } else if width < 140 {
        Size::Medium
    } else {
        Size::Wide
    }
}
pub fn inset(r: Rect, x: u16, y: u16) -> Rect {
    let dx = x.min(r.width / 2);
    let dy = y.min(r.height / 2);
    Rect::new(r.x + dx, r.y + dy, r.width - dx * 2, r.height - dy * 2)
}
pub fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width);
    let h = h.min(area.height);
    Rect::new(area.x + (area.width - w) / 2, area.y + (area.height - h) / 2, w, h)
}
pub fn row(r: Rect, y: u16) -> Rect {
    Rect::new(r.x, r.y + y, r.width, 1)
}
/// Split `r` into a left label column and right remainder.
pub fn split_label(r: Rect, label_w: u16) -> (Rect, Rect) {
    let l = label_w.min(r.width);
    (Rect::new(r.x, r.y, l, r.height), Rect::new(r.x + l, r.y, r.width - l, r.height))
}
