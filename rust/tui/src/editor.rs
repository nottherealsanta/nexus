//! Bounded grapheme editor with selection and undo. No domain state lives here.
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

/// Source ranges for visual rows. Soft breaks never change the editor's text;
/// renderers should render these slices rather than wrapping them again.
pub struct VisualLayout<'a> {
    text: &'a str,
    pub rows: Vec<std::ops::Range<usize>>,
}

impl VisualLayout<'_> {
    pub fn row_text(&self, row: usize) -> &str {
        &self.text[self.rows[row].clone()]
    }

    /// A cursor at a soft break belongs to the following row. At a hard break
    /// it belongs to the preceding row, until it moves past the newline.
    pub fn cursor_position(&self, cursor: usize) -> (usize, usize) {
        let row = self
            .rows
            .iter()
            .rposition(|range| range.start <= cursor)
            .unwrap_or(0);
        let range = &self.rows[row];
        let column = self.text[range.start..cursor.min(range.end)]
            .graphemes(true)
            .map(UnicodeWidthStr::width)
            .sum();
        (row, column)
    }

    pub fn cursor_at(&self, row: usize, column: usize) -> usize {
        let range = &self.rows[row];
        let mut columns = 0;
        let mut cursor = range.start;
        for (offset, grapheme) in self.row_text(row).grapheme_indices(true) {
            if columns + grapheme.width() > column {
                break;
            }
            columns += grapheme.width();
            cursor = range.start + offset + grapheme.len();
        }
        // A soft row's end is also the next row's start. Keep navigation on
        // the requested row rather than accidentally moving an extra row.
        if cursor == range.end
            && self
                .rows
                .get(row + 1)
                .is_some_and(|next| next.start == cursor)
        {
            cursor = self
                .row_text(row)
                .grapheme_indices(true)
                .last()
                .map(|(offset, _)| range.start + offset)
                .unwrap_or(range.start);
        }
        cursor
    }
}

const MAX_UNDO_BYTES: usize = 8 * 1024 * 1024;
#[derive(Default)]
pub struct Editor {
    pub text: String,
    pub cursor: usize,
    pub anchor: Option<usize>,
    pub history: Vec<String>,
    pub history_index: usize,
    undo: Vec<(String, usize)>,
    redo: Vec<(String, usize)>,
}
impl Editor {
    /// Wrap whitespace-delimited words using terminal display columns. Words
    /// that fit a row move intact; only oversized tokens split at grapheme
    /// boundaries. Whitespace is retained, including at soft row boundaries.
    pub fn visual_layout(&self, width: usize) -> VisualLayout<'_> {
        let width = width.max(1);
        let mut rows = Vec::new();
        let mut logical_start = 0;
        for logical in self.text.split('\n') {
            let mut row_start = logical_start;
            let mut columns = 0;
            let graphemes: Vec<_> = logical.grapheme_indices(true).collect();
            let mut index = 0;
            while index < graphemes.len() {
                let (offset, grapheme) = graphemes[index];
                if !grapheme.chars().all(char::is_whitespace) {
                    let token_start = index;
                    let mut token_width = 0;
                    while index < graphemes.len()
                        && !graphemes[index].1.chars().all(char::is_whitespace)
                    {
                        token_width += graphemes[index].1.width();
                        index += 1;
                    }
                    if token_width <= width && columns + token_width > width && columns > 0 {
                        rows.push(row_start..logical_start + offset);
                        row_start = logical_start + offset;
                        columns = 0;
                    }
                    for &(offset, grapheme) in &graphemes[token_start..index] {
                        if columns + grapheme.width() > width && columns > 0 {
                            rows.push(row_start..logical_start + offset);
                            row_start = logical_start + offset;
                            columns = 0;
                        }
                        columns += grapheme.width();
                    }
                } else {
                    if columns + grapheme.width() > width && columns > 0 {
                        rows.push(row_start..logical_start + offset);
                        row_start = logical_start + offset;
                        columns = 0;
                    }
                    columns += grapheme.width();
                    index += 1;
                }
            }
            rows.push(row_start..logical_start + logical.len());
            logical_start += logical.len() + 1;
        }
        VisualLayout {
            text: &self.text,
            rows,
        }
    }

    /// Navigate with exactly the same source ranges used for visual rendering.
    pub fn vertical_wrapped(&mut self, down: bool, width: usize) {
        let layout = self.visual_layout(width);
        let (row, column) = layout.cursor_position(self.cursor);
        let target = if down {
            (row + 1 < layout.rows.len()).then_some(row + 1)
        } else {
            row.checked_sub(1)
        };
        if let Some(target) = target {
            self.cursor = layout.cursor_at(target, column);
        }
    }

    fn image_marker(content: &str) -> bool {
        let number = content
            .strip_prefix("image #")
            .or_else(|| content.strip_prefix("image "))
            .or_else(|| content.strip_prefix("document "));
        number
            .filter(|number| !number.is_empty() && number.bytes().all(|b| b.is_ascii_digit()))
            .is_some_and(|number| number.bytes().any(|b| b != b'0'))
    }

    fn markers(&self) -> Vec<(usize, usize)> {
        self.text
            .match_indices('[')
            .filter_map(|(start, _)| {
                let close = self.text[start..].find(']')? + start;
                Self::image_marker(&self.text[start + 1..close]).then_some((start, close + 1))
            })
            .collect()
    }

    fn expand_marker_selection(&self, (mut start, mut end): (usize, usize)) -> (usize, usize) {
        for (marker_start, marker_end) in self.markers() {
            if marker_start < end && marker_end > start {
                start = start.min(marker_start);
                end = end.max(marker_end);
            }
        }
        (start, end)
    }

    fn marker_left(&self) -> Option<usize> {
        self.markers()
            .into_iter()
            .find(|(start, end)| *start < self.cursor && self.cursor <= *end)
            .map(|(start, _)| start)
    }

    fn marker_right(&self) -> Option<usize> {
        self.markers()
            .into_iter()
            .find(|(start, end)| *start <= self.cursor && self.cursor < *end)
            .map(|(_, end)| end)
    }

    fn checkpoint(&mut self) {
        self.undo.push((self.text.clone(), self.cursor));
        self.redo.clear();
        while self.undo.len() > 100
            || self.undo.iter().map(|v| v.0.len()).sum::<usize>() > MAX_UNDO_BYTES
        {
            self.undo.remove(0);
        }
    }
    pub fn selection(&self) -> Option<(usize, usize)> {
        self.anchor
            .filter(|a| *a != self.cursor)
            .map(|a| (a.min(self.cursor), a.max(self.cursor)))
    }
    pub fn select_move(&mut self, selecting: bool) {
        if selecting {
            if self.anchor.is_none() {
                self.anchor = Some(self.cursor);
            }
        } else {
            self.anchor = None;
        }
    }
    pub fn selected(&self) -> &str {
        self.selection()
            .map(|(a, b)| &self.text[a..b])
            .unwrap_or("")
    }
    fn erase_selection(&mut self) -> bool {
        if let Some((a, b)) = self
            .selection()
            .map(|range| self.expand_marker_selection(range))
        {
            self.text.replace_range(a..b, "");
            self.cursor = a;
            self.anchor = None;
            true
        } else {
            false
        }
    }
    pub fn insert(&mut self, text: &str) {
        self.checkpoint();
        self.erase_selection();
        self.text.insert_str(self.cursor, text);
        self.cursor += text.len();
    }
    pub fn left(&mut self) {
        if let Some(start) = self.marker_left() {
            self.cursor = start;
            return;
        }
        if self.cursor > 0 {
            self.cursor = self.text[..self.cursor]
                .grapheme_indices(true)
                .last()
                .unwrap()
                .0;
        }
    }
    pub fn right(&mut self) {
        if let Some(end) = self.marker_right() {
            self.cursor = end;
            return;
        }
        if self.cursor < self.text.len() {
            self.cursor += self.text[self.cursor..]
                .graphemes(true)
                .next()
                .unwrap()
                .len();
        }
    }
    pub fn word(&mut self, right: bool) {
        if right {
            while self.cursor < self.text.len()
                && !self.text[self.cursor..].starts_with(char::is_whitespace)
            {
                self.right();
            }
            while self.cursor < self.text.len()
                && self.text[self.cursor..].starts_with(char::is_whitespace)
            {
                self.right();
            }
        } else {
            while self.cursor > 0 && self.text[..self.cursor].ends_with(char::is_whitespace) {
                self.left();
            }
            while self.cursor > 0 && !self.text[..self.cursor].ends_with(char::is_whitespace) {
                self.left();
            }
        }
    }
    pub fn home(&mut self) {
        self.cursor = self.text[..self.cursor]
            .rfind('\n')
            .map(|i| i + 1)
            .unwrap_or(0);
    }
    pub fn end(&mut self) {
        self.cursor = self.text[self.cursor..]
            .find('\n')
            .map(|i| i + self.cursor)
            .unwrap_or(self.text.len());
    }
    pub fn vertical(&mut self, down: bool) {
        let start = self.text[..self.cursor]
            .rfind('\n')
            .map(|i| i + 1)
            .unwrap_or(0);
        let column = self.text[start..self.cursor].graphemes(true).count();
        let target = if down {
            self.text[self.cursor..]
                .find('\n')
                .map(|i| i + self.cursor + 1)
        } else {
            start
                .checked_sub(1)
                .map(|end| self.text[..end].rfind('\n').map(|i| i + 1).unwrap_or(0))
        };
        if let Some(target) = target {
            let end = self.text[target..]
                .find('\n')
                .map(|i| target + i)
                .unwrap_or(self.text.len());
            self.cursor = target
                + self.text[target..end]
                    .graphemes(true)
                    .take(column)
                    .map(str::len)
                    .sum::<usize>();
        }
    }
    pub fn backspace(&mut self) {
        self.checkpoint();
        if self.erase_selection() {
            return;
        }
        if let Some((start, end)) = self
            .markers()
            .into_iter()
            .find(|(start, end)| *start < self.cursor && self.cursor <= *end)
        {
            self.text.replace_range(start..end, "");
            self.cursor = start;
            return;
        }
        let end = self.cursor;
        self.left();
        self.text.replace_range(self.cursor..end, "");
    }
    pub fn delete(&mut self) {
        self.checkpoint();
        if self.erase_selection() {
            return;
        }
        if let Some((start, end)) = self
            .markers()
            .into_iter()
            .find(|(start, end)| *start <= self.cursor && self.cursor < *end)
        {
            self.text.replace_range(start..end, "");
            self.cursor = start;
            return;
        }
        let start = self.cursor;
        self.right();
        let end = self.cursor;
        self.cursor = start;
        self.text.replace_range(start..end, "");
    }
    pub fn undo(&mut self) {
        if let Some((text, cursor)) = self.undo.pop() {
            self.redo
                .push((std::mem::replace(&mut self.text, text), self.cursor));
            self.cursor = cursor;
            self.anchor = None;
        }
    }
    pub fn redo(&mut self) {
        if let Some((text, cursor)) = self.redo.pop() {
            self.undo
                .push((std::mem::replace(&mut self.text, text), self.cursor));
            self.cursor = cursor;
            self.anchor = None;
        }
    }
    /// Ctrl+C on a non-empty draft: empty it, keeping one undo step to bring it back.
    pub fn clear(&mut self) {
        self.checkpoint();
        self.text.clear();
        self.cursor = 0;
        self.anchor = None;
    }
    pub fn take(&mut self) -> String {
        let text = std::mem::take(&mut self.text);
        self.cursor = 0;
        self.anchor = None;
        self.undo.clear();
        self.redo.clear();
        self.history.push(text.clone());
        if self.history.len() > 500 {
            self.history.remove(0);
        }
        self.history_index = self.history.len();
        text
    }
    pub fn history(&mut self, older: bool) {
        self.history_index = if older {
            self.history_index.saturating_sub(1)
        } else {
            (self.history_index + 1).min(self.history.len())
        };
        self.text = self
            .history
            .get(self.history_index)
            .cloned()
            .unwrap_or_default();
        self.cursor = self.text.len();
        self.anchor = None;
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn clear_empties_the_draft_and_undo_restores_it() {
        let mut e = Editor::default();
        e.insert("half a thought");
        e.clear();
        assert!(e.text.is_empty() && e.cursor == 0);
        e.undo();
        assert_eq!(e.text, "half a thought");
    }
    #[test]
    fn visual_wrap_keeps_words_and_source_whitespace() {
        let mut e = Editor::default();
        e.insert("one two  three\nfour\n");
        let layout = e.visual_layout(7);
        let rows: Vec<_> = (0..layout.rows.len())
            .map(|row| layout.row_text(row))
            .collect();
        assert_eq!(rows, ["one two", "  three", "four", ""]);
        assert_eq!(e.text, "one two  three\nfour\n");

        e.text = "hello world".into();
        let layout = e.visual_layout(8);
        assert_eq!(layout.row_text(0), "hello ");
        assert_eq!(layout.row_text(1), "world");
        assert_eq!(layout.cursor_position(6), (1, 0));
        assert_eq!(layout.cursor_position(8), (1, 2));
        e.cursor = 2;
        e.vertical_wrapped(true, 8);
        assert_eq!(e.cursor, 8);
        e.vertical_wrapped(false, 8);
        assert_eq!(e.cursor, 2);
    }

    #[test]
    fn visual_wrap_splits_only_oversized_tokens_at_graphemes() {
        let mut e = Editor::default();
        e.insert("a abcdefghi 界界 e\u{301}🙂");
        let layout = e.visual_layout(5);
        let rows: Vec<_> = (0..layout.rows.len())
            .map(|row| layout.row_text(row))
            .collect();
        assert_eq!(rows, ["a abc", "defgh", "i ", "界界 ", "e\u{301}🙂"]);
        assert_eq!(rows.concat(), e.text);
        for row in &rows {
            assert!(row.width() <= 5);
        }
        let start = e.text.find("界界").unwrap();
        assert_eq!(layout.cursor_position(start + "界".len()), (3, 2));
        assert_eq!(layout.cursor_at(4, 2), e.text.find("🙂").unwrap());
        e.cursor = start + "界".len();
        e.vertical_wrapped(true, 5);
        assert_eq!(e.cursor, e.text.find("🙂").unwrap());
    }

    #[test]
    fn visual_navigation_clamps_without_crossing_soft_breaks() {
        let mut e = Editor::default();
        e.insert("abcd ef\nz");
        let layout = e.visual_layout(5);
        assert_eq!(layout.cursor_position(7), (1, 2));
        assert_eq!(layout.cursor_position(8), (2, 0));
        assert_eq!(layout.cursor_at(0, 100), 4);
        e.cursor = 7;
        e.vertical_wrapped(false, 5);
        assert_eq!(e.cursor, 2);
        e.vertical_wrapped(true, 5);
        assert_eq!(e.cursor, 7);
        e.vertical_wrapped(true, 5);
        assert_eq!(e.cursor, 9);
    }

    #[test]
    fn visual_wrap_handles_empty_narrow_and_wide_graphemes() {
        let mut e = Editor::default();
        assert_eq!(e.visual_layout(0).rows, [0..0]);
        e.insert("界🙂e\u{301}\n\n");
        let layout = e.visual_layout(0);
        let rows: Vec<_> = (0..layout.rows.len())
            .map(|row| layout.row_text(row))
            .collect();
        assert_eq!(rows, ["界", "🙂", "e\u{301}", "", ""]);
        assert_eq!(layout.cursor_position(e.text.len()), (4, 0));
        for row in 0..layout.rows.len() {
            assert!(e.text.is_char_boundary(layout.cursor_at(row, 1)));
        }
    }

    #[test]
    fn combining_and_emoji_are_whole_graphemes() {
        let mut e = Editor::default();
        e.insert("e\u{301}👨‍👩‍👧‍👦");
        e.backspace();
        assert_eq!(e.text, "e\u{301}");
        e.backspace();
        assert!(e.text.is_empty());
    }
    #[test]
    fn unicode_cursor() {
        let mut e = Editor::default();
        e.insert("hé🙂");
        e.left();
        e.backspace();
        assert_eq!(e.text, "h🙂");
        e.delete();
        assert_eq!(e.text, "h");
    }
    #[test]
    fn selection_undo_and_lines() {
        let mut e = Editor::default();
        e.insert("one\ntwo");
        e.home();
        e.vertical(false);
        assert_eq!(e.cursor, 0);
        e.select_move(true);
        e.right();
        e.right();
        e.insert("X");
        assert_eq!(e.text, "Xe\ntwo");
        e.undo();
        assert_eq!(e.text, "one\ntwo");
        e.redo();
        assert_eq!(e.text, "Xe\ntwo");
    }

    #[test]
    fn image_and_document_markers_are_atomic_to_navigate_and_delete() {
        let mut e = Editor::default();
        e.insert("a[image 12]b[image #3][document 4]c");
        e.cursor = 2;
        e.left();
        assert_eq!(e.cursor, 1);
        e.right();
        assert_eq!(e.cursor, 11);

        e.cursor = 6;
        e.backspace();
        assert_eq!(e.text, "ab[image #3][document 4]c");
        e.undo();
        assert_eq!(e.text, "a[image 12]b[image #3][document 4]c");

        e.cursor = 12;
        e.delete();
        assert_eq!(e.text, "a[image 12]b[document 4]c");
    }

    #[test]
    fn partial_marker_selection_removes_whole_marker_but_invalid_tokens_are_text() {
        let mut e = Editor::default();
        e.insert("x[image 0] [document nope] [image 2]y");
        let marker_start = e.text.find("[image 2]").unwrap();
        e.cursor = marker_start + 3;
        e.anchor = Some(marker_start + 1);
        e.backspace();
        assert_eq!(e.text, "x[image 0] [document nope] y");

        e.cursor = e.text.find("[image 0]").unwrap() + 3;
        e.left();
        assert_eq!(e.cursor, e.text.find("[image 0]").unwrap() + 2);
        e.cursor = e.text.find("[document nope]").unwrap() + 3;
        e.delete();
        assert_eq!(e.text, "x[image 0] [doument nope] y");
    }
}
