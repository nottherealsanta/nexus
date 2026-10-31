//! Mouse selection of the visible timeline cells.

use ratatui::{buffer::Buffer, layout::Rect, style::Modifier};
use unicode_width::UnicodeWidthStr;

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
