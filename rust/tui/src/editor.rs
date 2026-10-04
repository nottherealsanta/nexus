//! Bounded grapheme editor with selection and undo. No domain state lives here.
use unicode_segmentation::UnicodeSegmentation;
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
