//! Scroll container: draws content into an off-screen buffer, keeps the focused
//! stop visible, blits the window, and translates hit rectangles (plan §7.21).
use super::*;
use crate::ui::Ui;

#[derive(Default, Clone, Copy, Debug)]
pub struct Scroll {
    pub offset: u16,
}

/// `draw` receives a scratch buffer and a rect `(0, 0, area.width - 1, MAX)`; it
/// returns the content height used. Returns that height.
pub fn scrolled(buf: &mut Buffer, ui: &mut Ui, area: Rect, scroll: &mut Scroll, draw: impl FnOnce(&mut Buffer, &mut Ui, Rect) -> u16) -> u16 {
    const MAX: u16 = 240;
    let w = area.width.saturating_sub(1).max(1);
    let mut scratch = Buffer::empty(Rect::new(0, 0, w, MAX));
    fill(&mut scratch, Rect::new(0, 0, w, MAX), Style::default().fg(ui.theme.text).bg(ui.theme.bg));
    let start = ui.hits.len();
    let h = draw(&mut scratch, ui, Rect::new(0, 0, w, MAX)).min(MAX);
    // Keep the focused stop visible with a one-row margin.
    if let Some(r) = ui.focus.current().and_then(|id| ui.hits.rect_of(id)) {
        let (top, bottom) = (r.y, r.y + r.height);
        if top < scroll.offset + 1 {
            scroll.offset = top.saturating_sub(1);
        } else if bottom + 1 > scroll.offset + area.height {
            scroll.offset = bottom + 1 - area.height;
        }
    }
    scroll.offset = scroll.offset.min(h.saturating_sub(area.height));
    for y in 0..area.height {
        for x in 0..w {
            let src = scratch[(x, scroll.offset + y)].clone();
            buf[(area.x + x, area.y + y)] = src;
        }
    }
    ui.hits.translate_from(start, area.x as i32, area.y as i32 - scroll.offset as i32, area);
    if h > area.height {
        scrollbar(buf, ui, area.x + area.width - 1, area.y, area.height, h as usize, scroll.offset as usize, area.height as usize);
    }
    h
}
