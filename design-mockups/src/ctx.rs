//! Drawing context shared by every screen.
use crate::fixture::{View, World};
use nexus_widgets::*;
use ratatui::{buffer::Buffer, layout::Rect, style::Style};

pub struct Ctx<'a, 't> {
    pub buf: &'a mut Buffer,
    pub ui: &'a mut Ui<'t>,
    pub area: Rect,
    pub w: &'a World,
    pub v: &'a mut View,
    pub toasts: &'a ToastStack,
    pub state: usize,
}

impl<'a, 't> Ctx<'a, 't> {
    pub fn theme(&self) -> &'t Theme {
        self.ui.theme
    }
    pub fn base(&mut self, area: Rect) {
        let st = Style::default().fg(self.ui.theme.text).bg(self.ui.theme.bg);
        fill(self.buf, area, st);
    }
    pub fn text(&mut self, x: u16, y: u16, s: &str, st: Style, max: u16) -> u16 {
        put(self.buf, x, y, s, st, max)
    }
    pub fn dim(&self) -> Style {
        self.ui.theme.dim()
    }
}

pub fn hints(c: &mut Ctx, area: Rect, hs: &[(&str, &str)]) {
    key_hints(c.buf, c.ui, area, hs);
}
