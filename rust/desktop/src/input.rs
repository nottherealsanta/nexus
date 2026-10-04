//! Native UTF-16/IME input with Unicode selection and multiline wrapping.
//! Input-handler pattern adapted from GPUI's Apache-2.0 input example.
#[cfg(test)]
use core::prelude::v1::test;
use gpui::{prelude::*, *};
use std::ops::Range;
use unicode_segmentation::UnicodeSegmentation;

actions!(
    editor,
    [
        Backspace,
        Delete,
        Left,
        Right,
        SelectLeft,
        SelectRight,
        SelectAll,
        Home,
        End,
        WordLeft,
        WordRight,
        SelectWordLeft,
        SelectWordRight,
        LineStart,
        LineEnd,
        SelectLineStart,
        SelectLineEnd,
        SelectUp,
        SelectDown,
        Paste,
        Cut,
        Copy,
        Newline,
        Submit,
        Up,
        Down,
        Undo,
        Redo
    ]
);

pub enum InputEvent {
    Changed,
    Submitted,
    Navigate(i32),
    ClipboardImage,
}
pub struct Input {
    pub focus: FocusHandle,
    pub content: String,
    pub placeholder: String,
    pub secret: bool,
    pub readonly: bool,
    pub menu: bool,
    pub tab_enabled: bool,
    pub atomic_markers: bool,
    pub font_size: f32,
    pub highlights: Vec<(Range<usize>, HighlightStyle)>,
    pub links: Vec<(Range<usize>, String)>,
    pub foreground: Hsla,
    pub muted: Hsla,
    pub accent: Hsla,
    selected: Range<usize>,
    reversed: bool,
    marked: Option<Range<usize>>,
    selecting: bool,
    layout: Vec<(usize, WrappedLine, Bounds<Pixels>)>,
    undo: Vec<String>,
    redo: Vec<String>,
}
impl EventEmitter<InputEvent> for Input {}
impl Focusable for Input {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus.clone()
    }
}

impl Input {
    pub fn new(placeholder: &str, cx: &mut Context<Self>) -> Self {
        Self {
            focus: cx.focus_handle(),
            content: String::new(),
            placeholder: placeholder.into(),
            secret: false,
            readonly: false,
            menu: false,
            tab_enabled: true,
            atomic_markers: false,
            font_size: 14.,
            highlights: vec![],
            links: vec![],
            foreground: crate::theme::Theme::new(false).text,
            muted: crate::theme::Theme::new(false).muted,
            accent: crate::theme::Theme::new(false).accent,
            selected: 0..0,
            reversed: false,
            marked: None,
            selecting: false,
            layout: vec![],
            undo: vec![],
            redo: vec![],
        }
    }
    pub fn set(&mut self, text: String, cx: &mut Context<Self>) {
        self.content = text;
        self.selected = self.content.len()..self.content.len();
        self.marked = None;
        self.undo.clear();
        self.redo.clear();
        cx.notify();
    }
    pub fn insert(&mut self, text: &str, window: &mut Window, cx: &mut Context<Self>) {
        self.replace_text_in_range(None, text, window, cx);
    }
    pub(crate) fn cursor(&self) -> usize {
        if self.reversed {
            self.selected.start
        } else {
            self.selected.end
        }
    }
    fn move_to(&mut self, offset: usize, cx: &mut Context<Self>) {
        self.selected = offset..offset;
        self.reversed = false;
        cx.notify();
    }
    fn select_to(&mut self, offset: usize, cx: &mut Context<Self>) {
        let anchor = if self.reversed {
            self.selected.end
        } else {
            self.selected.start
        };
        self.selected = anchor.min(offset)..anchor.max(offset);
        self.reversed = offset < anchor;
        cx.notify();
    }
    fn marker_ranges(&self) -> Vec<Range<usize>> {
        if !self.atomic_markers {
            return Vec::new();
        }
        self.content
            .match_indices('[')
            .filter_map(|(start, _)| {
                let close = start + self.content[start..].find(']')?;
                let token = &self.content[start + 1..close];
                let number = token
                    .strip_prefix("image #")
                    .or_else(|| token.strip_prefix("image "))
                    .or_else(|| token.strip_prefix("document "))?;
                (!number.is_empty()
                    && number.bytes().all(|b| b.is_ascii_digit())
                    && number.bytes().any(|b| b != b'0'))
                .then_some(start..close + 1)
            })
            .collect()
    }
    fn previous(&self, offset: usize) -> usize {
        if let Some(marker) = self
            .marker_ranges()
            .iter()
            .find(|r| r.start < offset && offset <= r.end)
        {
            return marker.start;
        }
        self.content
            .grapheme_indices(true)
            .rev()
            .find_map(|(i, _)| (i < offset).then_some(i))
            .unwrap_or(0)
    }
    fn next(&self, offset: usize) -> usize {
        if let Some(marker) = self
            .marker_ranges()
            .iter()
            .find(|r| r.start <= offset && offset < r.end)
        {
            return marker.end;
        }
        self.content
            .grapheme_indices(true)
            .find_map(|(i, _)| (i > offset).then_some(i))
            .unwrap_or(self.content.len())
    }
    fn left(&mut self, _: &Left, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(
            if self.selected.is_empty() {
                self.previous(self.cursor())
            } else {
                self.selected.start
            },
            cx,
        );
    }
    fn right(&mut self, _: &Right, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(
            if self.selected.is_empty() {
                self.next(self.cursor())
            } else {
                self.selected.end
            },
            cx,
        );
    }
    fn select_left(&mut self, _: &SelectLeft, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.previous(self.cursor()), cx);
    }
    fn select_right(&mut self, _: &SelectRight, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.next(self.cursor()), cx);
    }
    fn all(&mut self, _: &SelectAll, _: &mut Window, cx: &mut Context<Self>) {
        self.selected = 0..self.content.len();
        cx.notify();
    }
    fn home(&mut self, _: &Home, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(0, cx);
    }
    fn end(&mut self, _: &End, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.content.len(), cx);
    }
    fn word_left_offset(&self) -> usize {
        self.content[..self.cursor()]
            .split_word_bound_indices()
            .rev()
            .find(|(_, word)| word.chars().any(|c| c.is_alphanumeric() || c == '_'))
            .map(|(offset, _)| offset)
            .unwrap_or(0)
    }
    fn word_right_offset(&self) -> usize {
        let cursor = self.cursor();
        self.content[cursor..]
            .split_word_bound_indices()
            .find(|(_, word)| word.chars().any(|c| c.is_alphanumeric() || c == '_'))
            .map(|(offset, word)| cursor + offset + word.len())
            .unwrap_or(self.content.len())
    }
    fn line_start_offset(&self) -> usize {
        self.content[..self.cursor()]
            .rfind('\n')
            .map(|i| i + 1)
            .unwrap_or(0)
    }
    fn line_end_offset(&self) -> usize {
        self.content[self.cursor()..]
            .find('\n')
            .map(|i| self.cursor() + i)
            .unwrap_or(self.content.len())
    }
    fn word_left(&mut self, _: &WordLeft, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.word_left_offset(), cx);
    }
    fn word_right(&mut self, _: &WordRight, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.word_right_offset(), cx);
    }
    fn select_word_left(&mut self, _: &SelectWordLeft, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.word_left_offset(), cx);
    }
    fn select_word_right(&mut self, _: &SelectWordRight, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.word_right_offset(), cx);
    }
    fn line_start(&mut self, _: &LineStart, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.line_start_offset(), cx);
    }
    fn line_end(&mut self, _: &LineEnd, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.line_end_offset(), cx);
    }
    fn select_line_start(&mut self, _: &SelectLineStart, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.line_start_offset(), cx);
    }
    fn select_line_end(&mut self, _: &SelectLineEnd, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.line_end_offset(), cx);
    }
    fn select_up(&mut self, _: &SelectUp, _: &mut Window, cx: &mut Context<Self>) {
        self.vertical(-1., true, cx);
    }
    fn select_down(&mut self, _: &SelectDown, _: &mut Window, cx: &mut Context<Self>) {
        self.vertical(1., true, cx);
    }
    fn backspace(&mut self, _: &Backspace, w: &mut Window, cx: &mut Context<Self>) {
        if self.selected.is_empty() {
            self.select_to(self.previous(self.cursor()), cx);
        }
        self.replace_text_in_range(None, "", w, cx);
    }
    fn delete(&mut self, _: &Delete, w: &mut Window, cx: &mut Context<Self>) {
        if self.selected.is_empty() {
            self.select_to(self.next(self.cursor()), cx);
        }
        self.replace_text_in_range(None, "", w, cx);
    }
    fn paste(&mut self, _: &Paste, w: &mut Window, cx: &mut Context<Self>) {
        if self.readonly {
            return;
        }
        if let Some(item) = cx.read_from_clipboard() {
            if let Some(text) = item.text() {
                self.replace_text_in_range(None, &text, w, cx);
            } else if item
                .entries()
                .iter()
                .any(|entry| matches!(entry, ClipboardEntry::Image(_)))
            {
                cx.emit(InputEvent::ClipboardImage);
            }
        }
    }
    fn copy(&mut self, _: &Copy, _: &mut Window, cx: &mut Context<Self>) {
        if !self.secret && !self.selected.is_empty() {
            cx.write_to_clipboard(ClipboardItem::new_string(
                self.content[self.selected.clone()].into(),
            ));
        }
    }
    fn cut(&mut self, _: &Cut, w: &mut Window, cx: &mut Context<Self>) {
        self.copy(&Copy, w, cx);
        self.replace_text_in_range(None, "", w, cx);
    }
    fn newline(&mut self, _: &Newline, w: &mut Window, cx: &mut Context<Self>) {
        self.replace_text_in_range(None, "\n", w, cx);
    }
    fn submit(&mut self, _: &Submit, _: &mut Window, cx: &mut Context<Self>) {
        if self.marked.is_none() && !self.readonly {
            cx.emit(InputEvent::Submitted);
        }
    }
    fn undo(&mut self, _: &Undo, _: &mut Window, cx: &mut Context<Self>) {
        if let Some(text) = self.undo.pop() {
            self.redo.push(std::mem::replace(&mut self.content, text));
            self.move_to(self.content.len(), cx);
            cx.emit(InputEvent::Changed);
        }
    }
    fn redo(&mut self, _: &Redo, _: &mut Window, cx: &mut Context<Self>) {
        if let Some(text) = self.redo.pop() {
            self.undo.push(std::mem::replace(&mut self.content, text));
            self.move_to(self.content.len(), cx);
            cx.emit(InputEvent::Changed);
        }
    }
    fn vertical(&mut self, direction: f32, selecting: bool, cx: &mut Context<Self>) {
        let cursor = self.cursor();
        let position = self.layout.iter().find_map(|(start, line, bounds)| {
            (cursor >= *start && cursor <= start + line.len())
                .then(|| {
                    line.position_for_index(cursor - start, px(24.))
                        .map(|p| bounds.origin + p)
                })
                .flatten()
        });
        if let Some(p) = position {
            let offset = self.index_at(point(p.x, p.y + px(24. * direction)));
            if selecting {
                self.select_to(offset, cx);
            } else {
                self.move_to(offset, cx);
            }
        }
    }
    fn up(&mut self, _: &Up, _: &mut Window, cx: &mut Context<Self>) {
        if self.menu {
            cx.emit(InputEvent::Navigate(-1));
        } else {
            self.vertical(-1., false, cx);
        }
    }
    fn down(&mut self, _: &Down, _: &mut Window, cx: &mut Context<Self>) {
        if self.menu {
            cx.emit(InputEvent::Navigate(1));
        } else {
            self.vertical(1., false, cx);
        }
    }
    fn index_at(&self, position: Point<Pixels>) -> usize {
        for (start, line, bounds) in &self.layout {
            if position.y < bounds.bottom() {
                let local = position - bounds.origin;
                let index = line
                    .closest_index_for_position(local, px(24.))
                    .unwrap_or_else(|i| i);
                return (*start + index).min(self.content.len());
            }
        }
        self.content.len()
    }
    fn mouse_down(&mut self, event: &MouseDownEvent, w: &mut Window, cx: &mut Context<Self>) {
        w.focus(&self.focus);
        let index = self.index_at(event.position);
        if event.modifiers.secondary() {
            if let Some((_, url)) = self.links.iter().find(|(r, url)| {
                r.contains(&index)
                    && (url.starts_with("https://")
                        || url.starts_with("http://")
                        || url.starts_with("mailto:"))
            }) {
                cx.open_url(url);
                return;
            }
        }
        self.selecting = true;
        if event.click_count == 3 {
            self.selected = 0..self.content.len();
        } else if event.click_count == 2 {
            let word = self
                .content
                .unicode_word_indices()
                .find(|(start, word)| index >= *start && index <= start + word.len());
            if let Some((start, word)) = word {
                self.selected = start..start + word.len();
            }
        } else if event.modifiers.shift {
            self.select_to(index, cx);
        } else {
            self.move_to(index, cx);
        }
        cx.notify();
    }
    fn mouse_up(&mut self, _: &MouseUpEvent, _: &mut Window, _: &mut Context<Self>) {
        self.selecting = false;
    }
    fn mouse_move(&mut self, event: &MouseMoveEvent, _: &mut Window, cx: &mut Context<Self>) {
        if self.selecting {
            self.select_to(self.index_at(event.position), cx);
        }
    }
    fn runs(&self, text: &str, style: &TextStyle, placeholder: bool) -> Vec<TextRun> {
        let mut points = vec![0, text.len()];
        if !placeholder && !self.secret {
            for (range, _) in &self.highlights {
                points.push(range.start.min(text.len()));
                points.push(range.end.min(text.len()));
            }
        }
        points.sort_unstable();
        points.dedup();
        points
            .windows(2)
            .filter(|r| r[0] < r[1])
            .map(|r| {
                let mut style = style.clone();
                style.color = if placeholder {
                    self.muted
                } else {
                    self.foreground
                };
                if !placeholder && !self.secret {
                    for (range, highlight) in &self.highlights {
                        if range.contains(&r[0]) {
                            style = style.highlight(*highlight);
                        }
                    }
                }
                TextRun {
                    len: r[1] - r[0],
                    font: style.font(),
                    color: style.color,
                    background_color: style.background_color,
                    underline: style.underline,
                    strikethrough: style.strikethrough,
                }
            })
            .collect()
    }
    fn from_utf16(&self, offset: usize) -> usize {
        utf16_to_byte(&self.content, offset)
    }
    fn to_utf16(&self, offset: usize) -> usize {
        self.content[..offset.min(self.content.len())]
            .encode_utf16()
            .count()
    }
    fn from_range(&self, r: &Range<usize>) -> Range<usize> {
        self.from_utf16(r.start)..self.from_utf16(r.end)
    }
    fn to_range(&self, r: &Range<usize>) -> Range<usize> {
        self.to_utf16(r.start)..self.to_utf16(r.end)
    }
}
pub fn utf16_to_byte(text: &str, offset: usize) -> usize {
    let mut count = 0;
    for (i, ch) in text.char_indices() {
        if count >= offset {
            return i;
        }
        count += ch.len_utf16();
    }
    text.len()
}
impl EntityInputHandler for Input {
    fn text_for_range(
        &mut self,
        r: Range<usize>,
        actual: &mut Option<Range<usize>>,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<String> {
        let r = self.from_range(&r);
        *actual = Some(self.to_range(&r));
        Some(self.content[r].into())
    }
    fn selected_text_range(
        &mut self,
        _: bool,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<UTF16Selection> {
        Some(UTF16Selection {
            range: self.to_range(&self.selected),
            reversed: self.reversed,
        })
    }
    fn marked_text_range(&self, _: &mut Window, _: &mut Context<Self>) -> Option<Range<usize>> {
        self.marked.as_ref().map(|r| self.to_range(r))
    }
    fn unmark_text(&mut self, _: &mut Window, _: &mut Context<Self>) {
        self.marked = None;
    }
    fn replace_text_in_range(
        &mut self,
        r: Option<Range<usize>>,
        text: &str,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.readonly {
            return;
        }
        let mut r = r
            .as_ref()
            .map(|r| self.from_range(r))
            .or(self.marked.clone())
            .unwrap_or(self.selected.clone());
        for marker in self.marker_ranges() {
            if marker.start < r.end && marker.end > r.start {
                r = r.start.min(marker.start)..r.end.max(marker.end);
            }
        }
        if self.content.len() - r.len() + text.len() > 1_000_000 {
            return;
        }
        self.undo.push(self.content.clone());
        while self.undo.len() > 64
            || self.undo.iter().map(String::len).sum::<usize>() > 8 * 1024 * 1024
        {
            self.undo.remove(0);
        }
        self.redo.clear();
        self.content.replace_range(r.clone(), text);
        self.move_to(r.start + text.len(), cx);
        self.marked = None;
        cx.emit(InputEvent::Changed);
    }
    fn replace_and_mark_text_in_range(
        &mut self,
        r: Option<Range<usize>>,
        text: &str,
        selected: Option<Range<usize>>,
        w: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.readonly {
            return;
        }
        let range = r
            .as_ref()
            .map(|r| self.from_range(r))
            .or(self.marked.clone())
            .unwrap_or(self.selected.clone());
        if self.content.len() - (range.end - range.start) + text.len() > 1_000_000 {
            return;
        }
        self.replace_text_in_range(r, text, w, cx);
        self.marked = (!text.is_empty()).then_some(range.start..range.start + text.len());
        if let Some(selected) = selected {
            self.selected = range.start + utf16_to_byte(text, selected.start)
                ..range.start + utf16_to_byte(text, selected.end);
        }
        cx.notify();
    }
    fn bounds_for_range(
        &mut self,
        r: Range<usize>,
        _: Bounds<Pixels>,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<Bounds<Pixels>> {
        let r = self.from_range(&r);
        self.layout.iter().find_map(|(start, line, bounds)| {
            if r.start < *start || r.start > start + line.len() {
                return None;
            }
            let p = line.position_for_index(r.start - start, px(24.))?;
            Some(Bounds::new(bounds.origin + p, size(px(2.), px(24.))))
        })
    }
    fn character_index_for_point(
        &mut self,
        p: Point<Pixels>,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<usize> {
        Some(self.to_utf16(self.index_at(p)))
    }
}

struct InputElement {
    entity: Entity<Input>,
}
struct Painted {
    lines: Vec<WrappedLine>,
    quads: Vec<PaintQuad>,
}
impl IntoElement for InputElement {
    type Element = Self;
    fn into_element(self) -> Self {
        self
    }
}
impl Element for InputElement {
    type RequestLayoutState = ();
    type PrepaintState = Painted;
    fn id(&self) -> Option<ElementId> {
        None
    }
    fn source_location(&self) -> Option<&'static std::panic::Location<'static>> {
        None
    }
    fn request_layout(
        &mut self,
        _: Option<&GlobalElementId>,
        _: Option<&InspectorElementId>,
        w: &mut Window,
        cx: &mut App,
    ) -> (LayoutId, ()) {
        let input = self.entity.read(cx);
        let text: SharedString = if input.content.is_empty() {
            input.placeholder.clone().into()
        } else if input.secret {
            "•".repeat(input.content.chars().count()).into()
        } else {
            input.content.clone().into()
        };
        let style = w.text_style();
        let font_size = style.font_size.to_pixels(w.rem_size());
        let system = w.text_system().clone();
        let runs = input.runs(&text, &style, false);
        let mut style = Style::default();
        style.size.width = relative(1.).into();
        let id = w.request_measured_layout(style, move |known, available, _, _| {
            let width = known.width.unwrap_or(match available.width {
                AvailableSpace::Definite(p) => p,
                _ => px(600.),
            });
            let lines = system
                .shape_text(text.clone(), font_size, &runs, Some(width), None)
                .unwrap_or_default();
            let height = lines
                .iter()
                .map(|l| l.size(px(24.)).height)
                .fold(px(0.), |a, b| a + b)
                .max(px(24.));
            size(width, height)
        });
        (id, ())
    }
    fn prepaint(
        &mut self,
        _: Option<&GlobalElementId>,
        _: Option<&InspectorElementId>,
        bounds: Bounds<Pixels>,
        _: &mut (),
        w: &mut Window,
        cx: &mut App,
    ) -> Painted {
        let input = self.entity.read(cx);
        let style = w.text_style();
        let text: SharedString = if input.content.is_empty() {
            input.placeholder.clone().into()
        } else if input.secret {
            "•".repeat(input.content.chars().count()).into()
        } else {
            input.content.clone().into()
        };
        let runs = input.runs(&text, &style, input.content.is_empty());
        let lines: Vec<_> = w
            .text_system()
            .shape_text(
                text,
                style.font_size.to_pixels(w.rem_size()),
                &runs,
                Some(bounds.size.width),
                None,
            )
            .unwrap_or_default()
            .into_iter()
            .collect();
        let mut quads = vec![];
        let mut start = 0;
        let mut y = bounds.top();
        if !input.secret {
            for line in &lines {
                if input.selected.is_empty() && input.focus.is_focused(w) && !input.readonly {
                    if input.cursor() >= start && input.cursor() <= start + line.len() {
                        if let Some(p) = line.position_for_index(input.cursor() - start, px(24.)) {
                            quads.push(fill(
                                Bounds::new(
                                    point(bounds.left() + p.x, y + p.y),
                                    size(px(1.5), px(24.)),
                                ),
                                input.accent,
                            ));
                        }
                    }
                } else if !input.selected.is_empty()
                    && input.selected.end > start
                    && input.selected.start <= start + line.len()
                {
                    let from = input.selected.start.saturating_sub(start).min(line.len());
                    let to = input.selected.end.saturating_sub(start).min(line.len());
                    if let (Some(a), Some(b)) = (
                        line.position_for_index(from, px(24.)),
                        line.position_for_index(to, px(24.)),
                    ) {
                        let mut row_y = a.y;
                        while row_y <= b.y {
                            let x1 = if row_y == a.y { a.x } else { px(0.) };
                            let x2 = if row_y == b.y { b.x } else { bounds.size.width };
                            quads.push(fill(
                                Bounds::new(
                                    point(bounds.left() + x1, y + row_y),
                                    size((x2 - x1).max(px(1.)), px(24.)),
                                ),
                                input.accent.opacity(0.25),
                            ));
                            row_y += px(24.);
                        }
                    }
                }
                start += line.len() + 1;
                y += line.size(px(24.)).height;
            }
        }
        Painted { lines, quads }
    }
    fn paint(
        &mut self,
        _: Option<&GlobalElementId>,
        _: Option<&InspectorElementId>,
        bounds: Bounds<Pixels>,
        _: &mut (),
        painted: &mut Painted,
        w: &mut Window,
        cx: &mut App,
    ) {
        let focus = self.entity.read(cx).focus.clone();
        w.handle_input(
            &focus,
            ElementInputHandler::new(bounds, self.entity.clone()),
            cx,
        );
        for quad in painted.quads.drain(..) {
            w.paint_quad(quad);
        }
        let mut y = bounds.top();
        let mut start = 0;
        let mut layout = vec![];
        for line in &painted.lines {
            let height = line.size(px(24.)).height;
            let origin = point(bounds.left(), y);
            line.paint(origin, px(24.), TextAlign::Left, None, w, cx)
                .ok();
            layout.push((
                start,
                line.clone(),
                Bounds::new(origin, size(bounds.size.width, height)),
            ));
            start += line.len() + 1;
            y += height;
        }
        self.entity.update(cx, |input, _| input.layout = layout);
    }
}
impl Render for Input {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        self.focus = self
            .focus
            .clone()
            .tab_stop(self.tab_enabled && !self.readonly);
        div()
            .id("input")
            .w_full()
            .key_context("Editor")
            .track_focus(&self.focus)
            .cursor_text()
            .on_action(cx.listener(Self::backspace))
            .on_action(cx.listener(Self::delete))
            .on_action(cx.listener(Self::left))
            .on_action(cx.listener(Self::right))
            .on_action(cx.listener(Self::select_left))
            .on_action(cx.listener(Self::select_right))
            .on_action(cx.listener(Self::all))
            .on_action(cx.listener(Self::word_left))
            .on_action(cx.listener(Self::word_right))
            .on_action(cx.listener(Self::select_word_left))
            .on_action(cx.listener(Self::select_word_right))
            .on_action(cx.listener(Self::line_start))
            .on_action(cx.listener(Self::line_end))
            .on_action(cx.listener(Self::select_line_start))
            .on_action(cx.listener(Self::select_line_end))
            .on_action(cx.listener(Self::select_up))
            .on_action(cx.listener(Self::select_down))
            .on_action(cx.listener(Self::home))
            .on_action(cx.listener(Self::end))
            .on_action(cx.listener(Self::paste))
            .on_action(cx.listener(Self::copy))
            .on_action(cx.listener(Self::cut))
            .on_action(cx.listener(Self::newline))
            .on_action(cx.listener(Self::submit))
            .on_action(cx.listener(Self::up))
            .on_action(cx.listener(Self::down))
            .on_action(cx.listener(Self::undo))
            .on_action(cx.listener(Self::redo))
            .on_mouse_down(MouseButton::Left, cx.listener(Self::mouse_down))
            .on_mouse_up(MouseButton::Left, cx.listener(Self::mouse_up))
            .on_mouse_up_out(MouseButton::Left, cx.listener(Self::mouse_up))
            .on_mouse_move(cx.listener(Self::mouse_move))
            .text_size(px(self.font_size))
            .line_height(px(24.))
            .child(InputElement {
                entity: cx.entity(),
            })
    }
}
pub fn bindings(cx: &mut App) {
    cx.bind_keys([
        KeyBinding::new("backspace", Backspace, Some("Editor")),
        KeyBinding::new("delete", Delete, Some("Editor")),
        KeyBinding::new("left", Left, Some("Editor")),
        KeyBinding::new("right", Right, Some("Editor")),
        KeyBinding::new("shift-left", SelectLeft, Some("Editor")),
        KeyBinding::new("shift-right", SelectRight, Some("Editor")),
        KeyBinding::new("cmd-a", SelectAll, Some("Editor")),
        KeyBinding::new("ctrl-a", SelectAll, Some("Editor")),
        KeyBinding::new("cmd-v", Paste, Some("Editor")),
        KeyBinding::new("ctrl-v", Paste, Some("Editor")),
        KeyBinding::new("cmd-c", Copy, Some("Editor")),
        KeyBinding::new("cmd-x", Cut, Some("Editor")),
        KeyBinding::new("cmd-z", Undo, Some("Editor")),
        KeyBinding::new("cmd-shift-z", Redo, Some("Editor")),
        KeyBinding::new("alt-left", WordLeft, Some("Editor")),
        KeyBinding::new("ctrl-left", WordLeft, Some("Editor")),
        KeyBinding::new("alt-right", WordRight, Some("Editor")),
        KeyBinding::new("ctrl-right", WordRight, Some("Editor")),
        KeyBinding::new("alt-shift-left", SelectWordLeft, Some("Editor")),
        KeyBinding::new("alt-shift-right", SelectWordRight, Some("Editor")),
        KeyBinding::new("ctrl-shift-left", SelectWordLeft, Some("Editor")),
        KeyBinding::new("ctrl-shift-right", SelectWordRight, Some("Editor")),
        KeyBinding::new("cmd-left", LineStart, Some("Editor")),
        KeyBinding::new("cmd-right", LineEnd, Some("Editor")),
        KeyBinding::new("cmd-shift-left", SelectLineStart, Some("Editor")),
        KeyBinding::new("cmd-shift-right", SelectLineEnd, Some("Editor")),
        KeyBinding::new("home", LineStart, Some("Editor")),
        KeyBinding::new("end", LineEnd, Some("Editor")),
        KeyBinding::new("ctrl-home", Home, Some("Editor")),
        KeyBinding::new("ctrl-end", End, Some("Editor")),
        KeyBinding::new("shift-up", SelectUp, Some("Editor")),
        KeyBinding::new("shift-down", SelectDown, Some("Editor")),
        KeyBinding::new("ctrl-z", Undo, Some("Editor")),
        KeyBinding::new("ctrl-y", Redo, Some("Editor")),
        KeyBinding::new("shift-enter", Newline, Some("Editor")),
        KeyBinding::new("enter", Submit, Some("Editor")),
        KeyBinding::new("up", Up, Some("Editor")),
        KeyBinding::new("down", Down, Some("Editor")),
    ]);
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn utf16_offsets_handle_emoji_and_non_latin() {
        let text = "a🦀नexus";
        assert_eq!(utf16_to_byte(text, 1), 1);
        assert_eq!(utf16_to_byte(text, 3), 5);
        assert_eq!(utf16_to_byte(text, 4), 8);
        assert_eq!(utf16_to_byte(text, 999), text.len());
    }
}

#[cfg(test)]
mod native_tests {
    use super::*;
    #[gpui::test]
    fn attachment_markers_delete_atomically_and_undo_restores_them(cx: &mut TestAppContext) {
        let window = cx.add_window(|_, cx| Input::new("", cx));
        window
            .update(cx, |input, w, cx| {
                input.atomic_markers = true;
                input.set("hello [image 2][document 3]".into(), cx);
                input.backspace(&Backspace, w, cx);
                assert_eq!(input.content, "hello [image 2]");
                input.undo(&Undo, w, cx);
                assert_eq!(input.content, "hello [image 2][document 3]");
                input.selected = 9..11;
                input.delete(&Delete, w, cx);
                assert_eq!(input.content, "hello [document 3]");
                assert_eq!(input.cursor(), 6);
            })
            .unwrap();
    }
    #[gpui::test]
    fn word_and_line_selection_keep_unicode_boundaries(cx: &mut TestAppContext) {
        let window = cx.add_window(|_, cx| Input::new("", cx));
        window
            .update(cx, |input, w, cx| {
                input.set("hello 🦀\n世界 next".into(), cx);
                input.word_left(&WordLeft, w, cx);
                assert_eq!(&input.content[input.cursor()..], "next");
                input.select_word_left(&SelectWordLeft, w, cx);
                // Unicode word segmentation treats CJK ideographs as individual words.
                assert_eq!(&input.content[input.selected.clone()], "界 ");
                input.line_start(&LineStart, w, cx);
                assert_eq!(&input.content[input.cursor()..], "世界 next");
                input.select_line_end(&SelectLineEnd, w, cx);
                assert_eq!(&input.content[input.selected.clone()], "世界 next");
            })
            .unwrap();
    }
    #[gpui::test]
    fn native_editor_keeps_multiline_text_and_undo(cx: &mut TestAppContext) {
        let window = cx.add_window(|_, cx| Input::new("", cx));
        window
            .update(cx, |input, w, cx| {
                input.insert("日本語 🦀\nsecond line", w, cx);
                assert_eq!(input.content, "日本語 🦀\nsecond line");
                input.backspace(&Backspace, w, cx);
                assert_eq!(input.content, "日本語 🦀\nsecond lin");
                input.undo(&Undo, w, cx);
                assert_eq!(input.content, "日本語 🦀\nsecond line");
                input.all(&SelectAll, w, cx);
                input.insert("replacement", w, cx);
                assert_eq!(input.content, "replacement");
            })
            .unwrap();
    }
    #[gpui::test]
    fn ime_replaces_marked_text_without_corrupting_unicode(cx: &mut TestAppContext) {
        let window = cx.add_window(|_, cx| Input::new("", cx));
        window
            .update(cx, |input, w, cx| {
                input.insert("🦀", w, cx);
                input.replace_and_mark_text_in_range(None, "に", Some(1..1), w, cx);
                assert_eq!(input.content, "🦀に");
                assert_eq!(input.marked_text_range(w, cx), Some(2..3));
                input.replace_text_in_range(None, "日本語", w, cx);
                assert_eq!(input.content, "🦀日本語");
                assert!(input.marked.is_none());
            })
            .unwrap();
    }
    #[gpui::test]
    fn readonly_text_can_select_but_cannot_be_edited(cx: &mut TestAppContext) {
        let window = cx.add_window(|_, cx| Input::new("", cx));
        window
            .update(cx, |input, w, cx| {
                input.set("visible tool result".into(), cx);
                input.readonly = true;
                input.all(&SelectAll, w, cx);
                input.insert("overwrite", w, cx);
                assert_eq!(input.content, "visible tool result");
                assert_eq!(input.selected, 0..19);
            })
            .unwrap();
    }
}
