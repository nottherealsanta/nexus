//! Composer backed by the old Nexus grapheme editor.
use crate::editor::Editor;

pub type DisplayRows = Vec<Vec<(String, bool)>>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Composer {
    pub images: Vec<crate::images::Image>,
    editor: Box<Editor>,
    lines: Vec<String>,
}

impl Default for Composer {
    fn default() -> Self {
        Self {
            images: Vec::new(),
            editor: Box::new(Editor::default()),
            lines: vec![String::new()],
        }
    }
}

fn clean(s: &str) -> String {
    s.replace("\r\n", "\n")
        .replace('\r', "\n")
        .chars()
        .flat_map(|c| {
            match c {
                '\n' => "\n".to_owned(),
                '\t' => "    ".to_owned(),
                c if !c.is_control() => c.to_string(),
                _ => String::new(),
            }
            .chars()
            .collect::<Vec<_>>()
        })
        .collect()
}

impl Composer {
    fn refresh(&mut self) {
        self.lines = self.editor.text.split('\n').map(str::to_owned).collect();
    }
    pub fn new() -> Self {
        Self::default()
    }
    /// Display text may contain a collapsed paste marker.
    pub fn text(&self) -> String {
        self.editor.text.clone()
    }
    /// The actual text sent to Core, with all paste markers expanded.
    pub fn expanded_text(&self) -> String {
        self.editor.expanded()
    }
    pub fn is_empty(&self) -> bool {
        self.editor.text.is_empty()
    }
    pub fn has_text(&self) -> bool {
        !self.expanded_text().trim().is_empty()
    }
    pub fn clear(&mut self) {
        self.editor.clear();
        self.refresh();
    }
    pub fn set_text(&mut self, text: &str) {
        self.editor.clear();
        self.editor.insert(&clean(text));
        self.refresh();
    }
    pub fn cursor(&self) -> (usize, usize) {
        let before = &self.editor.text[..self.editor.cursor];
        let row = before.bytes().filter(|c| *c == b'\n').count();
        let col = before.rsplit('\n').next().unwrap_or("").chars().count();
        (row, col)
    }
    /// The display text before the cursor (completion looks at its last token).
    pub fn before_cursor(&self) -> &str {
        &self.editor.text[..self.editor.cursor]
    }
    /// Replace the `token` that ends at the cursor with `text`.
    pub fn replace_before_cursor(&mut self, token: &str, text: &str) {
        use unicode_segmentation::UnicodeSegmentation;
        for _ in token.graphemes(true) {
            self.editor.backspace();
        }
        self.editor.insert(&clean(text));
        self.refresh();
    }
    pub fn lines(&self) -> &[String] {
        &self.lines
    }
    pub fn insert_char(&mut self, c: char) {
        if !c.is_control() || c == '\n' {
            self.editor.insert(&c.to_string());
            self.refresh();
        }
    }
    pub fn insert_str(&mut self, s: &str) {
        self.editor.insert(&clean(s));
        self.refresh();
    }
    pub fn paste(&mut self, s: &str) {
        self.editor.paste(&clean(s));
        self.refresh();
    }
    pub fn newline(&mut self) {
        self.editor.insert("\n");
        self.refresh();
    }
    pub fn backspace(&mut self) {
        if self.editor.selection().is_none() {
            let before = &self.editor.text[..self.editor.cursor];
            if let Some(marker) = self
                .images
                .iter()
                .map(|image| format!("[{}]", image.name))
                .find(|marker| before.ends_with(marker))
            {
                self.editor
                    .select_range(self.editor.cursor - marker.len(), self.editor.cursor);
            }
        }
        self.editor.backspace();
        self.refresh();
    }
    pub fn delete(&mut self) {
        self.editor.delete();
        self.refresh();
    }
    pub fn delete_word_back(&mut self) {
        self.editor.select_move(true);
        self.editor.word(false);
        self.editor.backspace();
        self.refresh();
    }
    pub fn kill_to_line_start(&mut self) {
        self.editor.select_move(true);
        self.editor.home();
        self.editor.backspace();
        self.refresh();
    }
    pub fn word(&mut self, forward: bool) {
        self.editor.word(forward);
    }
    pub fn document_edge(&mut self, end: bool) {
        self.editor.cursor = if end { self.editor.text.len() } else { 0 };
    }
    pub fn left(&mut self) {
        self.editor.left();
    }
    pub fn right(&mut self) {
        self.editor.right();
    }
    pub fn up(&mut self) -> bool {
        let row = self.cursor().0;
        self.editor.vertical(false);
        self.cursor().0 < row
    }
    pub fn down(&mut self) -> bool {
        let row = self.cursor().0;
        self.editor.vertical(true);
        self.cursor().0 > row
    }
    pub fn home(&mut self) {
        self.editor.home();
    }
    pub fn end(&mut self) {
        self.editor.end();
    }
    pub fn select_move(&mut self, selecting: bool) {
        self.editor.select_move(selecting);
    }
    pub fn selection(&self) -> Option<(usize, usize)> {
        self.editor.selection()
    }
    pub fn selected_text(&self) -> Option<String> {
        let (start, end) = self.editor.selection()?;
        let mut selected = self.editor.clone();
        selected.text = selected.text[start..end].to_owned();
        selected.cursor = 0;
        selected.anchor = None;
        Some(selected.expanded())
    }
    pub fn undo(&mut self) {
        self.editor.undo();
        self.refresh();
    }
    pub fn redo(&mut self) {
        self.editor.redo();
        self.refresh();
    }
    pub fn take(&mut self) -> String {
        let text = self.editor.take();
        self.refresh();
        text
    }
    pub fn has_history(&self) -> bool {
        !self.editor.history.is_empty()
    }
    pub fn in_history(&self) -> bool {
        self.editor.history_index < self.editor.history.len()
    }
    pub fn history(&mut self, older: bool) {
        self.editor.history(older);
        self.refresh();
    }
    pub fn point_at(&self, width: u16, row: usize, column: u16) -> usize {
        self.editor.cursor_at_visual(width, row, column)
    }
    pub fn select_range(&mut self, start: usize, end: usize) {
        self.editor.select_range(start, end);
    }
    /// Visual rows with selection and paste-marker padding, matching the old editor.
    pub fn display_rows(&self, width: usize) -> (DisplayRows, (usize, usize)) {
        use unicode_segmentation::UnicodeSegmentation;
        let layout = self.editor.visual_layout(width.max(1));
        let selection = self.editor.selection();
        let mut rows = Vec::new();
        for range in &layout.rows {
            let mut row = Vec::new();
            for (offset, grapheme) in self.editor.text[range.clone()].grapheme_indices(true) {
                let at = range.start + offset;
                let selected =
                    selection.is_some_and(|(start, end)| at < end && at + grapheme.len() > start);
                let (before, after) = crate::editor::chip_padding(&layout.chips, at, grapheme);
                if before > 0 {
                    row.push((" ".into(), selected));
                }
                row.push((grapheme.to_owned(), selected));
                if after > 0 {
                    row.push((" ".into(), selected));
                }
            }
            rows.push(row);
        }
        let mut cursor = layout.cursor_position(self.editor.cursor);
        if cursor.1 >= width.max(1) && cursor.0 + 1 == rows.len() {
            rows.push(Vec::new());
            cursor = (rows.len() - 1, 0);
        }
        (rows, cursor)
    }
    pub fn layout(&self, width: usize) -> (Vec<String>, (usize, usize)) {
        let (rows, cursor) = self.display_rows(width);
        (
            rows.into_iter()
                .map(|row| row.into_iter().map(|(text, _)| text).collect())
                .collect(),
            cursor,
        )
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
    fn grapheme_movement_and_deletion_match_old_editor() {
        let mut c = Composer::new();
        c.insert_str("a👩‍💻e\u{301}");
        c.left();
        c.backspace();
        assert_eq!(c.text(), "ae\u{301}");
        c.delete();
        assert_eq!(c.text(), "a");
    }

    #[test]
    fn vertical_movement_never_splits_a_grapheme() {
        let mut c = Composer::new();
        c.insert_str("👩‍💻x\na");
        assert!(c.up());
        assert_eq!(c.cursor(), (0, 3));
        c.backspace();
        assert_eq!(c.text(), "x\na");
        c.down();
        c.end();
        c.up();
        assert_eq!(c.cursor(), (0, 1));
    }

    #[test]
    fn paste_markers_remain_unambiguous_after_edit_and_undo() {
        let mut c = Composer::new();
        let first = "aaa\nbbb\nccc";
        let second = "xxx\nyyy\nzzz";
        c.paste(first);
        c.paste(second);
        assert_eq!(c.expanded_text(), format!("{first}{second}"));
        c.backspace();
        assert_eq!(c.expanded_text(), first);
        c.undo();
        assert_eq!(c.expanded_text(), format!("{first}{second}"));
        c.redo();
        c.backspace();
        c.insert_str("[Pasted ~4 tokens]");
        assert_eq!(c.expanded_text(), "[Pasted ~4 tokens]");
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
