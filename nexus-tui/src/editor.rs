//! Bounded grapheme editor with selection and undo. No domain state lives here.
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

/// Source ranges for visual rows. Soft breaks never change the editor's text;
/// renderers should render these slices rather than wrapping them again.
///
/// Attachment chips (`[image 1]`, `[document 2]`) draw with one padding cell on
/// each side. The padding is not stored text: it is counted in the width of the
/// chip's first and last grapheme, so wrapping, cursor columns and pointer
/// hit-testing all agree with what the renderer draws.
pub struct VisualLayout<'a> {
    text: &'a str,
    pub rows: Vec<std::ops::Range<usize>>,
    /// Source byte ranges of the attachment chips.
    pub chips: Vec<(usize, usize)>,
}

/// Padding cells drawn before and after the grapheme at source offset `at`.
pub fn chip_padding(chips: &[(usize, usize)], at: usize, grapheme: &str) -> (usize, usize) {
    (
        usize::from(chips.iter().any(|&(start, _)| start == at)),
        usize::from(chips.iter().any(|&(_, end)| end == at + grapheme.len())),
    )
}

impl VisualLayout<'_> {
    pub fn row_text(&self, row: usize) -> &str {
        &self.text[self.rows[row].clone()]
    }

    fn cell_width(&self, at: usize, grapheme: &str) -> usize {
        let (lead, trail) = chip_padding(&self.chips, at, grapheme);
        grapheme.width() + lead + trail
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
        let end = cursor.min(range.end);
        let mut column: usize = self.text[range.start..end]
            .grapheme_indices(true)
            .map(|(offset, g)| self.cell_width(range.start + offset, g))
            .sum();
        // The cursor cell sits after a chip's leading padding.
        if cursor < range.end && self.chips.iter().any(|&(start, _)| start == cursor) {
            column += 1;
        }
        (row, column)
    }

    pub fn cursor_at(&self, row: usize, column: usize) -> usize {
        let range = &self.rows[row];
        let mut columns = 0;
        let mut cursor = range.start;
        for (offset, grapheme) in self.row_text(row).grapheme_indices(true) {
            let width = self.cell_width(range.start + offset, grapheme);
            if columns + width > column {
                break;
            }
            columns += width;
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
                .next_back()
                .map(|(offset, _)| range.start + offset)
                .unwrap_or(range.start);
        }
        cursor
    }
}

const MAX_UNDO_BYTES: usize = 8 * 1024 * 1024;
type EditorSnapshot = (String, usize, Vec<(String, String)>);
#[derive(Default, Debug, Clone, PartialEq, Eq)]
pub struct Editor {
    pub text: String,
    pub cursor: usize,
    pub anchor: Option<usize>,
    pub history: Vec<String>,
    pub history_index: usize,
    history_scratch: Option<EditorSnapshot>,
    undo: Vec<EditorSnapshot>,
    redo: Vec<EditorSnapshot>,
    /// Collapsed pastes: (marker text, full content), expanded again on `take`.
    pastes: Vec<(String, String)>,
}
/// Pastes at or above this size collapse into a `[Pasted ~N tokens]` marker.
const COLLAPSE_CHARS: usize = 200;
const COLLAPSE_LINES: usize = 3;
impl Editor {
    fn paste_marker(content: &str) -> String {
        format!("[Pasted ~{} tokens]", (content.chars().count() / 4).max(1))
    }

    /// Insert pasted text. Large pastes become one atomic marker, like images;
    /// pasting the same content again right after its marker expands it in place.
    pub fn paste(&mut self, text: &str) {
        let big = text.chars().count() >= COLLAPSE_CHARS || text.lines().count() >= COLLAPSE_LINES;
        if !big {
            return self.insert(text);
        }
        let base = Self::paste_marker(text);
        let at = if self.selection().is_none() {
            self.pastes.iter().rposition(|(marker, content)| {
                content == text && self.text[..self.cursor].ends_with(marker)
            })
        } else {
            None
        };
        if let Some(at) = at {
            self.checkpoint();
            let marker = self.pastes[at].0.clone();
            let start = self.cursor - marker.len();
            self.text.replace_range(start..self.cursor, text);
            self.cursor = start + text.len();
            // Keep the entry only if another marker with it remains in the draft.
            if self.text.matches(&marker).count()
                < self.pastes.iter().filter(|(m, _)| *m == marker).count()
            {
                self.pastes.remove(at);
            }
            return;
        }
        let mut marker = base.clone();
        let mut number = 2;
        while self.pastes.iter().any(|(existing, _)| *existing == marker)
            || self.text.contains(&marker)
        {
            marker = format!("{} #{number}]", base.trim_end_matches(']'));
            number += 1;
        }
        self.insert(&marker);
        self.pastes.push((marker, text.to_string()));
    }

    /// The draft with every paste marker replaced by its full content.
    pub fn expanded(&self) -> String {
        let mut unused = self.pastes.clone();
        let mut out = String::new();
        let mut last = 0;
        for (start, end) in self.markers() {
            let marker = &self.text[start..end];
            if let Some(i) = unused.iter().position(|(m, _)| m == marker) {
                out.push_str(&self.text[last..start]);
                out.push_str(&unused.remove(i).1);
                last = end;
            }
        }
        out.push_str(&self.text[last..]);
        out
    }

    /// Wrap whitespace-delimited words using terminal display columns. Words
    /// that fit a row move intact; only oversized tokens split at grapheme
    /// boundaries. Whitespace is retained, including at soft row boundaries.
    pub fn visual_layout(&self, width: usize) -> VisualLayout<'_> {
        let width = width.max(1);
        let chips = self.attachment_chips();
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
                        let (at, g) = graphemes[index];
                        let (lead, trail) = chip_padding(&chips, logical_start + at, g);
                        token_width += g.width() + lead + trail;
                        index += 1;
                    }
                    if token_width <= width && columns + token_width > width && columns > 0 {
                        rows.push(row_start..logical_start + offset);
                        row_start = logical_start + offset;
                        columns = 0;
                    }
                    for &(offset, grapheme) in &graphemes[token_start..index] {
                        let (lead, trail) = chip_padding(&chips, logical_start + offset, grapheme);
                        let cell = grapheme.width() + lead + trail;
                        if columns + cell > width && columns > 0 {
                            rows.push(row_start..logical_start + offset);
                            row_start = logical_start + offset;
                            columns = 0;
                        }
                        columns += cell;
                    }
                } else {
                    let (lead, trail) = chip_padding(&chips, logical_start + offset, grapheme);
                    let cell = grapheme.width() + lead + trail;
                    if columns + cell > width && columns > 0 {
                        rows.push(row_start..logical_start + offset);
                        row_start = logical_start + offset;
                        columns = 0;
                    }
                    columns += cell;
                    index += 1;
                }
            }
            rows.push(row_start..logical_start + logical.len());
            logical_start += logical.len() + 1;
        }
        VisualLayout {
            text: &self.text,
            rows,
            chips,
        }
    }

    /// Source ranges of the image/document references (not paste markers).
    pub fn attachment_chips(&self) -> Vec<(usize, usize)> {
        self.markers()
            .into_iter()
            .filter(|&(start, _)| !self.text[start..].starts_with("[Pasted"))
            .collect()
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

    /// Source offset under a pointer at `(column, row)` inside a `width` x `height`
    /// editor viewport, in cells relative to its top-left. Mirrors the composer view:
    /// the viewport follows the cursor, and a cursor resting after a full row gets a
    /// spare trailing row. Points past the text clamp to the nearest row or row end,
    /// and a point inside an image/paste marker snaps to its nearer edge.
    pub fn cursor_at_point(&self, width: u16, height: u16, column: u16, row: u16) -> usize {
        let layout = self.visual_layout(usize::from(width.max(1)));
        let (mut cursor_row, cursor_column) = layout.cursor_position(self.cursor);
        let spare = self.cursor == layout.rows[cursor_row].end
            && cursor_column >= usize::from(width.max(1));
        let spare_row = cursor_row + 1;
        if spare {
            cursor_row += 1;
        }
        let top = cursor_row.saturating_sub(usize::from(height.max(1)) - 1);
        let visual = top + usize::from(row.min(height.saturating_sub(1)));
        let at = if spare && visual == spare_row {
            self.cursor
        } else {
            let source = if spare && visual > spare_row {
                visual - 1
            } else {
                visual
            };
            layout.cursor_at(source.min(layout.rows.len() - 1), usize::from(column))
        };
        match self
            .markers()
            .into_iter()
            .find(|(start, end)| *start < at && at < *end)
        {
            Some((start, end)) if at - start <= end - at => start,
            Some((_, end)) => end,
            None => at,
        }
    }

    /// Map a visual row and cell to a grapheme-safe source offset.
    pub fn cursor_at_visual(&self, width: u16, row: usize, column: u16) -> usize {
        let layout = self.visual_layout(usize::from(width.max(1)));
        let at = if row >= layout.rows.len() {
            self.text.len()
        } else {
            layout.cursor_at(row, usize::from(column))
        };
        match self
            .markers()
            .into_iter()
            .find(|(start, end)| *start < at && at < *end)
        {
            Some((start, end)) if at - start <= end - at => start,
            Some((_, end)) => end,
            None => at,
        }
    }
    pub fn select_range(&mut self, start: usize, end: usize) {
        self.cursor = end;
        self.anchor = (start != end).then_some(start);
    }

    /// Extend a pointer selection that began at source offset `start` to the point
    /// under the pointer. Dragging back onto `start` leaves a plain cursor.
    pub fn drag_to_point(&mut self, start: usize, width: u16, height: u16, column: u16, row: u16) {
        let at = self.cursor_at_point(width, height, column, row);
        self.cursor = at;
        self.anchor = (at != start).then_some(start);
    }

    fn image_marker(content: &str) -> bool {
        if let Some(value) = content.strip_prefix("Pasted ~") {
            let (amount, suffix) = value.split_once(" tokens").unwrap_or(("", ""));
            let valid_suffix = suffix.is_empty()
                || suffix
                    .strip_prefix(" #")
                    .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()));
            return !amount.is_empty()
                && amount.bytes().all(|b| b.is_ascii_digit())
                && valid_suffix;
        }
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
        self.undo
            .push((self.text.clone(), self.cursor, self.pastes.clone()));
        self.redo.clear();
        while self.undo.len() > 100
            || self
                .undo
                .iter()
                .map(|v| {
                    v.0.len()
                        + v.2
                            .iter()
                            .map(|(marker, text)| marker.len() + text.len())
                            .sum::<usize>()
                })
                .sum::<usize>()
                > MAX_UNDO_BYTES
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
    fn prune_pastes(&mut self) {
        self.pastes.retain(|(marker, _)| self.text.contains(marker));
    }
    fn erase_selection(&mut self) -> bool {
        if let Some((a, b)) = self
            .selection()
            .map(|range| self.expand_marker_selection(range))
        {
            self.text.replace_range(a..b, "");
            self.cursor = a;
            self.anchor = None;
            self.prune_pastes();
            true
        } else {
            // An anchor on the cursor selects nothing, but left set it would
            // turn into a stale selection once the edit moves the cursor.
            self.anchor = None;
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
                .next_back()
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
            self.prune_pastes();
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
            self.prune_pastes();
            return;
        }
        let start = self.cursor;
        self.right();
        let end = self.cursor;
        self.cursor = start;
        self.text.replace_range(start..end, "");
    }
    pub fn undo(&mut self) {
        if let Some((text, cursor, pastes)) = self.undo.pop() {
            self.redo.push((
                std::mem::replace(&mut self.text, text),
                self.cursor,
                std::mem::replace(&mut self.pastes, pastes),
            ));
            self.cursor = cursor;
            self.anchor = None;
        }
    }
    pub fn redo(&mut self) {
        if let Some((text, cursor, pastes)) = self.redo.pop() {
            self.undo.push((
                std::mem::replace(&mut self.text, text),
                self.cursor,
                std::mem::replace(&mut self.pastes, pastes),
            ));
            self.cursor = cursor;
            self.anchor = None;
        }
    }
    /// Empty the draft, keeping one undo step to bring it back.
    pub fn clear(&mut self) {
        self.checkpoint();
        self.text.clear();
        self.pastes.clear();
        self.cursor = 0;
        self.anchor = None;
    }
    /// Ctrl+C clearing a draft: empty it, keep one undo step, and put the
    /// cleared text in recall history so Up brings it back.
    pub fn clear_to_history(&mut self) {
        let text = self.expanded();
        self.clear();
        self.remember(text);
    }
    fn remember(&mut self, text: String) {
        self.history.push(text);
        if self.history.len() > 500 {
            self.history.remove(0);
        }
        self.history_index = self.history.len();
        self.history_scratch = None;
    }
    pub fn take(&mut self) -> String {
        let text = self.expanded();
        self.text.clear();
        self.pastes.clear();
        self.cursor = 0;
        self.anchor = None;
        self.undo.clear();
        self.redo.clear();
        self.remember(text.clone());
        text
    }
    pub fn set_history(&mut self, mut entries: Vec<String>) {
        let excess = entries.len().saturating_sub(500);
        entries.drain(..excess);
        self.history = entries;
        self.history_index = self.history.len();
        self.history_scratch = None;
    }
    pub fn history(&mut self, older: bool) {
        if older && self.history_index == self.history.len() && self.history_scratch.is_none() {
            self.history_scratch = Some((self.text.clone(), self.cursor, self.pastes.clone()));
        }
        self.history_index = if older {
            self.history_index.saturating_sub(1)
        } else {
            (self.history_index + 1).min(self.history.len())
        };
        if self.history_index == self.history.len() {
            if let Some((text, cursor, pastes)) = self.history_scratch.take() {
                self.text = text;
                self.cursor = cursor;
                self.pastes = pastes;
            }
        } else {
            self.text = self.history[self.history_index].clone();
            self.cursor = self.text.len();
            self.pastes.clear();
        }
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
    fn clear_to_history_keeps_the_cleared_text_for_recall() {
        let mut e = Editor::default();
        e.paste("one\ntwo\nthree");
        e.clear_to_history();
        assert!(e.text.is_empty() && e.cursor == 0);
        e.history(true);
        assert_eq!(e.text, "one\ntwo\nthree");
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
        assert_eq!(e.visual_layout(0).rows.len(), 1);
        assert_eq!(e.visual_layout(0).rows[0], 0..0);
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
    fn pointer_maps_wrapped_unicode_rows_to_source_offsets() {
        let mut e = Editor::default();
        e.insert("a abcdefghi 界界 e\u{301}🙂");
        e.cursor = 0;
        // Rows at width 5: "a abc", "defgh", "i ", "界界 ", "e\u{301}🙂"
        assert_eq!(e.cursor_at_point(5, 10, 3, 0), 3);
        assert_eq!(e.cursor_at_point(5, 10, 2, 1), e.text.find('f').unwrap());
        let wide = e.text.find("界界").unwrap();
        assert_eq!(e.cursor_at_point(5, 10, 0, 3), wide);
        // Either cell of a wide grapheme lands before it; the next one is after it.
        assert_eq!(e.cursor_at_point(5, 10, 1, 3), wide);
        assert_eq!(e.cursor_at_point(5, 10, 2, 3), wide + "界".len());
        assert_eq!(e.cursor_at_point(5, 10, 1, 4), e.text.find("🙂").unwrap());
        // Below the text clamps to the last row; far right clamps to the row end.
        assert_eq!(e.cursor_at_point(5, 10, 0, 9), e.text.rfind('e').unwrap());
        assert_eq!(e.cursor_at_point(5, 10, 4, 4), e.text.len());
    }

    #[test]
    fn pointer_follows_the_scrolled_viewport_and_spare_row() {
        let mut e = Editor::default();
        e.insert("aaaa\nbbbb\ncccc\ndddd");
        // Height 2 with the cursor on the last row shows "cccc" and "dddd".
        assert_eq!(
            e.cursor_at_point(8, 2, 1, 0),
            e.text.find("cccc").unwrap() + 1
        );
        assert_eq!(
            e.cursor_at_point(8, 2, 1, 1),
            e.text.find("dddd").unwrap() + 1
        );
        // A full row with the cursor after it shows a spare row for the cursor.
        let mut full = Editor::default();
        full.insert("abcd");
        assert_eq!(full.cursor_at_point(4, 5, 0, 1), 4);
        assert_eq!(full.cursor_at_point(4, 5, 2, 0), 2);
    }

    #[test]
    fn pointer_inside_a_marker_snaps_to_its_nearer_edge() {
        let mut e = Editor::default();
        e.insert("a[image 1]b");
        assert_eq!(e.cursor_at_point(40, 3, 3, 0), 1);
        assert_eq!(e.cursor_at_point(40, 3, 8, 0), 10);
    }

    #[test]
    fn dragging_selects_across_wrapped_unicode_rows() {
        let mut e = Editor::default();
        e.insert("a abcdefghi 界界 e\u{301}🙂");
        let start = e.cursor_at_point(5, 10, 2, 0);
        e.cursor = start;
        e.drag_to_point(start, 5, 10, 2, 3);
        let wide = e.text.find("界界").unwrap();
        assert_eq!(e.selection(), Some((2, wide + "界".len())));
        assert_eq!(e.selected(), "abcdefghi 界");
        // Dragging backwards and onto the start again collapses the selection.
        e.drag_to_point(start, 5, 10, 2, 0);
        assert_eq!(e.selection(), None);
        assert_eq!(e.anchor, None);
        e.drag_to_point(start, 5, 10, 0, 0);
        assert_eq!(e.selected(), "a ");
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
    fn an_empty_selection_does_not_outlive_the_next_edit() {
        // Alt+Backspace with nothing to delete leaves the anchor on the
        // cursor; repeated backspaces must not see it as a selection.
        let mut e = Editor::default();
        e.insert("abc");
        e.select_move(true);
        e.backspace();
        e.backspace();
        e.backspace();
        assert_eq!(e.text, "");
        assert_eq!(e.anchor, None);
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

    #[test]
    fn large_paste_collapses_and_repeat_expands() {
        let mut e = Editor::default();
        let big = "x".repeat(1370);
        e.paste(&big);
        assert_eq!(e.text, "[Pasted ~342 tokens]");
        e.paste("small");
        assert_eq!(e.text, "[Pasted ~342 tokens]small");
        e.text.truncate("[Pasted ~342 tokens]".len());
        e.cursor = e.text.len();
        e.paste(&big);
        assert_eq!(e.text, big);
        e.paste(&big);
        assert_eq!(e.text, format!("{big}[Pasted ~342 tokens]"));
        assert_eq!(e.take(), format!("{big}{big}"));
    }
}
