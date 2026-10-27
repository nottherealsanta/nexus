//! The multi-line composer buffer: text, cursor and editing. Display widths
//! are measured in cells; wrapping is by character so the cursor maps exactly.

use crate::text::cw;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Composer {
    lines: Vec<String>,
    /// Cursor line and column (in chars).
    row: usize,
    col: usize,
}

impl Default for Composer {
    fn default() -> Self {
        Self {
            lines: vec![String::new()],
            row: 0,
            col: 0,
        }
    }
}

fn byte_at(s: &str, col: usize) -> usize {
    s.char_indices().nth(col).map_or(s.len(), |(i, _)| i)
}

fn len(s: &str) -> usize {
    s.chars().count()
}

impl Composer {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn text(&self) -> String {
        self.lines.join("\n")
    }

    pub fn is_empty(&self) -> bool {
        self.lines.len() == 1 && self.lines[0].is_empty()
    }

    pub fn has_text(&self) -> bool {
        !self.text().trim().is_empty()
    }

    pub fn clear(&mut self) {
        *self = Self::default();
    }

    pub fn set_text(&mut self, text: &str) {
        self.clear();
        self.insert_str(text);
    }

    pub fn cursor(&self) -> (usize, usize) {
        (self.row, self.col)
    }

    pub fn lines(&self) -> &[String] {
        &self.lines
    }

    pub fn insert_char(&mut self, c: char) {
        if c == '\n' {
            return self.newline();
        }
        if c.is_control() {
            return;
        }
        let at = byte_at(&self.lines[self.row], self.col);
        self.lines[self.row].insert(at, c);
        self.col += 1;
    }

    /// Insert pasted text; `\r\n` and `\r` become newlines, tabs become spaces
    /// and other control characters are dropped.
    pub fn insert_str(&mut self, s: &str) {
        let s = s.replace("\r\n", "\n").replace('\r', "\n");
        for c in s.chars() {
            match c {
                '\n' => self.newline(),
                '\t' => (0..4).for_each(|_| self.insert_char(' ')),
                c => self.insert_char(c),
            }
        }
    }

    pub fn newline(&mut self) {
        let at = byte_at(&self.lines[self.row], self.col);
        let rest = self.lines[self.row].split_off(at);
        self.lines.insert(self.row + 1, rest);
        self.row += 1;
        self.col = 0;
    }

    pub fn backspace(&mut self) {
        if self.col > 0 {
            let at = byte_at(&self.lines[self.row], self.col - 1);
            self.lines[self.row].remove(at);
            self.col -= 1;
        } else if self.row > 0 {
            let cur = self.lines.remove(self.row);
            self.row -= 1;
            self.col = len(&self.lines[self.row]);
            self.lines[self.row].push_str(&cur);
        }
    }

    pub fn delete(&mut self) {
        if self.col < len(&self.lines[self.row]) {
            let at = byte_at(&self.lines[self.row], self.col);
            self.lines[self.row].remove(at);
        } else if self.row + 1 < self.lines.len() {
            let next = self.lines.remove(self.row + 1);
            self.lines[self.row].push_str(&next);
        }
    }

    pub fn delete_word_back(&mut self) {
        while self.col > 0 && self.char_before() == Some(' ') {
            self.backspace();
        }
        while self.col > 0 && self.char_before().is_some_and(|c| c != ' ') {
            self.backspace();
        }
    }

    fn char_before(&self) -> Option<char> {
        self.lines[self.row].chars().nth(self.col.checked_sub(1)?)
    }

    pub fn kill_to_line_start(&mut self) {
        let at = byte_at(&self.lines[self.row], self.col);
        self.lines[self.row].drain(..at);
        self.col = 0;
    }

    pub fn left(&mut self) {
        if self.col > 0 {
            self.col -= 1;
        } else if self.row > 0 {
            self.row -= 1;
            self.col = len(&self.lines[self.row]);
        }
    }

    pub fn right(&mut self) {
        if self.col < len(&self.lines[self.row]) {
            self.col += 1;
        } else if self.row + 1 < self.lines.len() {
            self.row += 1;
            self.col = 0;
        }
    }

    pub fn up(&mut self) -> bool {
        if self.row == 0 {
            return false;
        }
        self.row -= 1;
        self.col = self.col.min(len(&self.lines[self.row]));
        true
    }

    pub fn down(&mut self) -> bool {
        if self.row + 1 >= self.lines.len() {
            return false;
        }
        self.row += 1;
        self.col = self.col.min(len(&self.lines[self.row]));
        true
    }

    pub fn home(&mut self) {
        self.col = 0;
    }

    pub fn end(&mut self) {
        self.col = len(&self.lines[self.row]);
    }

    /// Wrapped display rows at `width` cells, and the cursor's (row, cell).
    pub fn layout(&self, width: usize) -> (Vec<String>, (usize, usize)) {
        let width = width.max(1);
        let mut rows = Vec::new();
        let mut cursor = (0, 0);
        for (li, line) in self.lines.iter().enumerate() {
            let mut cur = String::new();
            let mut w = 0;
            for (ci, c) in line.chars().enumerate() {
                let c_w = cw(c);
                if w + c_w > width {
                    rows.push(std::mem::take(&mut cur));
                    w = 0;
                }
                if li == self.row && ci == self.col {
                    cursor = (rows.len(), w);
                }
                cur.push(c);
                w += c_w;
            }
            if li == self.row && self.col >= len(line) {
                if w >= width {
                    rows.push(std::mem::take(&mut cur));
                    cursor = (rows.len(), 0);
                } else {
                    cursor = (rows.len(), w);
                }
            }
            rows.push(cur);
        }
        (rows, cursor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edits_across_lines() {
        let mut c = Composer::new();
        c.insert_str("hello");
        c.newline();
        c.insert_str("wörld");
        assert_eq!(c.text(), "hello\nwörld");
        c.up();
        c.end();
        c.delete();
        assert_eq!(c.text(), "hellowörld");
        c.backspace();
        assert_eq!(c.text(), "hellwörld");
    }

    #[test]
    fn paste_normalises_newlines_and_controls() {
        let mut c = Composer::new();
        c.insert_str("a\r\nb\rc\td\u{1b}");
        assert_eq!(c.text(), "a\nb\nc    d");
        assert_eq!(c.cursor(), (2, 6));
    }

    #[test]
    fn layout_wraps_wide_characters_and_places_the_cursor() {
        let mut c = Composer::new();
        c.insert_str("日本語ab");
        let (rows, cur) = c.layout(5);
        assert_eq!(rows, ["日本", "語ab"]);
        assert_eq!(cur, (1, 4));
        let mut c = Composer::new();
        c.insert_str("abcd");
        let (rows, cur) = c.layout(4);
        assert_eq!(rows, ["abcd", ""]);
        assert_eq!(cur, (1, 0));
    }

    #[test]
    fn word_delete_and_line_kill() {
        let mut c = Composer::new();
        c.insert_str("one two three");
        c.delete_word_back();
        assert_eq!(c.text(), "one two ");
        c.kill_to_line_start();
        assert!(c.is_empty());
    }
}
