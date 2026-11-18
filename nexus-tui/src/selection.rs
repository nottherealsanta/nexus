//! Mouse selection of the visible timeline cells.

use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier},
};
use unicode_width::UnicodeWidthStr;

pub struct TimelineSelection {
    pub area: Rect,
    start: (u16, usize),
    end: (u16, usize),
    rows: std::collections::BTreeMap<usize, Vec<ratatui::buffer::Cell>>,
}

impl TimelineSelection {
    pub fn new(area: Rect, screen: &Buffer, x: u16, y: u16, top: usize) -> Self {
        let point = (x, top + usize::from(y - area.y));
        let mut selection = Self {
            area,
            start: point,
            end: point,
            rows: Default::default(),
        };
        selection.capture(screen, top);
        selection
    }

    pub fn capture(&mut self, screen: &Buffer, top: usize) {
        if screen.area != self.area {
            return;
        }
        for y in self.area.y..self.area.bottom() {
            self.rows.insert(
                top + usize::from(y - self.area.y),
                (self.area.x..self.area.right())
                    .map(|x| screen[(x, y)].clone())
                    .collect(),
            );
        }
    }

    pub fn set_end(&mut self, x: u16, y: u16, top: usize) {
        self.end = (
            x.clamp(self.area.x, self.area.right() - 1),
            top + usize::from(y.clamp(self.area.y, self.area.bottom() - 1) - self.area.y),
        );
    }

    pub fn dragged(&self) -> bool {
        self.start != self.end
    }

    fn bounds(&self) -> ((u16, usize), (u16, usize)) {
        if (self.start.1, self.start.0) <= (self.end.1, self.end.0) {
            (self.start, self.end)
        } else {
            (self.end, self.start)
        }
    }

    fn row_bounds(&self, row: usize) -> Option<(u16, u16)> {
        let (start, end) = self.bounds();
        if row < start.1 || row > end.1 {
            return None;
        }
        Some((
            if row == start.1 { start.0 } else { self.area.x },
            if row == end.1 {
                end.0
            } else {
                self.area.right() - 1
            },
        ))
    }

    pub fn text(&self) -> String {
        let (start, end) = self.bounds();
        let mut result = Vec::new();
        for row in start.1..=end.1 {
            let Some(cells) = self.rows.get(&row) else {
                continue;
            };
            let (left, right) = self.row_bounds(row).unwrap();
            let mut text = String::new();
            let mut x = self.area.x;
            while x <= right {
                let symbol = cells[usize::from(x - self.area.x)].symbol();
                let width = UnicodeWidthStr::width(symbol).max(1) as u16;
                if x.saturating_add(width) > left {
                    text.push_str(symbol);
                }
                x = x.saturating_add(width);
            }
            result.push(text.trim_end().to_owned());
        }
        result.join("\n")
    }

    pub fn highlight(&self, buf: &mut Buffer, top: usize, theme: &crate::theme::Theme) {
        for y in self.area.y..self.area.bottom() {
            if let Some((left, right)) = self.row_bounds(top + usize::from(y - self.area.y)) {
                for x in left..=right {
                    let cell = &buf[(x, y)];
                    let style = match (theme.color("text.highlight"), cell.bg) {
                        (Some(Color::Rgb(r, g, b)), Color::Rgb(br, bg, bb)) => {
                            let alpha = crate::theme::text_selection_opacity().clamp(0.0, 1.0);
                            let mix = |base: u8, ink: u8| {
                                (f32::from(base) * (1.0 - alpha) + f32::from(ink) * alpha).round()
                                    as u8
                            };
                            cell.style()
                                .bg(Color::Rgb(mix(br, r), mix(bg, g), mix(bb, b)))
                        }
                        _ if theme.mono() => cell.style().add_modifier(Modifier::REVERSED),
                        _ => cell.style().patch(theme.bg("interactive.selection")),
                    };
                    buf[(x, y)].set_style(style);
                }
            }
        }
    }
}

#[derive(Clone, PartialEq)]
pub struct Selection {
    pub area: Rect,
    pub screen: Buffer,
    pub start: (u16, u16),
    pub end: (u16, u16),
}

impl Selection {
    pub fn set_end(&mut self, x: u16, y: u16) {
        self.end = (
            x.clamp(self.area.x, self.area.right() - 1),
            y.clamp(self.area.y, self.area.bottom() - 1),
        );
    }

    fn bounds(&self) -> ((u16, u16), (u16, u16)) {
        let key = |(x, y): (u16, u16)| (y, x);
        if key(self.start) <= key(self.end) {
            (self.start, self.end)
        } else {
            (self.end, self.start)
        }
    }

    pub fn dragged(&self) -> bool {
        self.start != self.end
    }

    fn row_bounds(&self, y: u16) -> Option<(u16, u16)> {
        let (start, end) = self.bounds();
        if y < start.1 || y > end.1 {
            return None;
        }
        Some((
            if y == start.1 { start.0 } else { self.area.x },
            if y == end.1 {
                end.0
            } else {
                self.area.right() - 1
            },
        ))
    }

    pub fn text(&self) -> String {
        let mut rows = Vec::new();
        for y in self.area.y..self.area.bottom() {
            let Some((left, right)) = self.row_bounds(y) else {
                continue;
            };
            let mut row = String::new();
            let mut x = self.area.x;
            while x <= right {
                let symbol = self.screen[(x, y)].symbol();
                let w = UnicodeWidthStr::width(symbol).max(1) as u16;
                if x.saturating_add(w) > left {
                    row.push_str(symbol);
                }
                x = x.saturating_add(w);
            }
            rows.push(row.trim_end().to_owned());
        }
        rows.join("\n")
    }

    pub fn highlight(&self, buf: &mut Buffer) {
        for y in self.area.y..self.area.bottom() {
            if let Some((left, right)) = self.row_bounds(y) {
                for x in left..=right {
                    let style = buf[(x, y)].style().add_modifier(Modifier::REVERSED);
                    buf[(x, y)].set_style(style);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selection_copies_both_directions_and_wide_characters() {
        let area = Rect::new(0, 0, 8, 2);
        let mut screen = Buffer::empty(area);
        screen.set_string(0, 0, "a界bc", ratatui::style::Style::default());
        screen.set_string(0, 1, "second", ratatui::style::Style::default());
        let mut s = Selection {
            area,
            screen,
            start: (2, 0),
            end: (2, 1),
        };
        assert_eq!(s.text(), "界bc\nsec");
        std::mem::swap(&mut s.start, &mut s.end);
        assert_eq!(s.text(), "界bc\nsec");
    }
}
