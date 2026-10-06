//! Native shell layout and bounded-width cell cache (feasibility §6–7).
use crate::{bridge::Snapshot, editor::Editor};
use base64::Engine as _;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
/// Nexus dark/light tokens, defined by the native palette.
mod chrome;
pub mod components;
mod context;
mod dialogs;
pub use chrome::*;
pub use dialogs::*;
pub struct Palette {
    pub background: Color,
    pub text: Color,
    pub muted: Color,
    pub quiet: Color,
    pub accent: Color,
    pub panel: Color,
    pub element: Color,
    pub element_hi: Color,
    pub dialog: Color,
    pub border: Color,
    pub border_strong: Color,
    pub diff_del: Color,
    pub diff_add: Color,
    pub blue: Color,
    pub purple: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
    pub cyan: Color,
}
impl Palette {
    pub fn new(light: bool) -> Self {
        let rgb = |r, g, b| Color::Rgb(r, g, b);
        if light {
            Self {
                background: rgb(255, 255, 255),
                text: rgb(27, 27, 27),
                muted: rgb(85, 85, 85),
                quiet: rgb(138, 138, 138),
                accent: rgb(200, 103, 47),
                panel: rgb(245, 245, 244),
                element: rgb(236, 236, 234),
                element_hi: rgb(226, 226, 223),
                dialog: rgb(255, 255, 255),
                border: rgb(220, 220, 216),
                border_strong: rgb(185, 185, 180),
                diff_del: rgb(253, 226, 228),
                diff_add: rgb(220, 245, 226),
                blue: rgb(47, 111, 214),
                purple: rgb(122, 82, 199),
                success: rgb(38, 128, 68),
                warning: rgb(168, 98, 0),
                error: rgb(194, 58, 74),
                cyan: rgb(14, 116, 144),
            }
        } else {
            Self {
                background: rgb(11, 11, 11),
                text: rgb(238, 238, 238),
                muted: rgb(163, 163, 163),
                quiet: rgb(111, 111, 111),
                accent: rgb(250, 178, 131),
                panel: rgb(20, 20, 20),
                element: rgb(30, 30, 30),
                element_hi: rgb(40, 40, 40),
                dialog: rgb(20, 20, 20),
                border: rgb(44, 44, 44),
                border_strong: rgb(72, 72, 72),
                diff_del: rgb(62, 27, 33),
                diff_add: rgb(24, 56, 36),
                blue: rgb(92, 156, 245),
                purple: rgb(157, 124, 216),
                success: rgb(127, 216, 143),
                warning: rgb(245, 167, 66),
                error: rgb(224, 108, 117),
                cyan: rgb(86, 212, 221),
            }
        }
    }
}
/// Wrapped parts with prefix offsets: viewport lookup copies only visible rows.
pub struct Indexed<T> {
    parts: Vec<std::sync::Arc<Vec<T>>>,
    offsets: Vec<usize>,
    length: usize,
}
impl<T> Default for Indexed<T> {
    fn default() -> Self {
        Self {
            parts: Vec::new(),
            offsets: Vec::new(),
            length: 0,
        }
    }
}
impl<T> Indexed<T> {
    pub fn len(&self) -> usize {
        self.length
    }
    pub fn clear(&mut self) {
        self.parts.clear();
        self.offsets.clear();
        self.length = 0;
    }
    pub fn push(&mut self, value: T) {
        self.offsets.push(self.length);
        self.parts.push(std::sync::Arc::new(vec![value]));
        self.length += 1;
    }
    pub fn get(&self, index: usize) -> Option<&T> {
        if index >= self.length {
            return None;
        }
        let part = self
            .offsets
            .partition_point(|offset| *offset <= index)
            .saturating_sub(1);
        self.parts[part].get(index - self.offsets[part])
    }
    pub fn iter(&self) -> impl Iterator<Item = &T> {
        self.parts.iter().flat_map(|part| part.iter())
    }
    pub fn range(&self, start: usize, count: usize) -> impl Iterator<Item = &T> {
        (start..start.saturating_add(count).min(self.length)).filter_map(|index| self.get(index))
    }
    fn truncate_parts(&mut self, length: usize) {
        self.parts.truncate(length);
        self.offsets.truncate(length);
        self.length = self
            .parts
            .last()
            .zip(self.offsets.last())
            .map_or(0, |(part, offset)| offset + part.len());
    }
    fn append_part(&mut self, part: std::sync::Arc<Vec<T>>) {
        self.offsets.push(self.length);
        self.length += part.len();
        self.parts.push(part);
    }
    fn set_parts(&mut self, parts: Vec<std::sync::Arc<Vec<T>>>) {
        let first = self
            .parts
            .iter()
            .zip(&parts)
            .take_while(|(old, new)| std::sync::Arc::ptr_eq(old, new))
            .count();
        self.offsets.truncate(first);
        self.length = self.parts.iter().take(first).map(|part| part.len()).sum();
        for part in parts.iter().skip(first) {
            self.offsets.push(self.length);
            self.length += part.len();
        }
        self.parts = parts;
    }
}
impl<T> From<Vec<T>> for Indexed<T> {
    fn from(rows: Vec<T>) -> Self {
        let mut result = Self::default();
        result.set_parts(vec![std::sync::Arc::new(rows)]);
        result
    }
}
impl<T> std::ops::Index<usize> for Indexed<T> {
    type Output = T;
    fn index(&self, index: usize) -> &T {
        self.get(index).expect("row index")
    }
}

#[derive(Default, Clone)]
struct Part {
    block: crate::bridge::Content,
    width: u16,
    lines: std::sync::Arc<Vec<Line<'static>>>,
    operations: std::sync::Arc<Vec<Option<serde_json::Value>>>,
}
#[derive(Default)]
pub struct Cache {
    pub disclosure: crate::disclosure::Disclosure,
    pub settings_nav_focus: bool,
    dirty_from: Option<usize>,
    dirty_until: Option<usize>,
    patch_known: bool,
    active_parts: Vec<Option<std::sync::Arc<Part>>>,
    active_page: String,
    part_offsets: Vec<usize>,
    built_verbose: bool,
    hints_index: Option<usize>,
    parts: std::collections::HashMap<String, std::sync::Arc<Part>>,
    blocks: Vec<crate::bridge::Content>,
    pub operations: Indexed<Option<serde_json::Value>>,
    pub image_protocol: Option<ratatui_image::protocol::StatefulProtocol>,
    image_source: String,
    source: Vec<String>,
    width: u16,
    pub lines: Indexed<Line<'static>>,
    /// Blank rows below the content (sub-agent pages keep ~30% of the viewport free).
    pub pad: usize,
    /// Page whose blocks are being laid out (`""` is the conversation); parts of other
    /// pages stay cached so Esc back to the parent does not re-wrap it.
    pub page: String,
    /// Largest useful scroll offset from the last draw; key and wheel handlers clamp to it.
    pub max_scroll: usize,
    /// Animation frame for the running-tool slot (`SPINNER_SLOT`).
    pub spin: usize,
    /// Independent 30 Hz clock for smooth activity motion.
    pub activity_frame: usize,
    /// The draft is non-empty: empty-session hints keep their rows but go blank.
    pub typing: bool,
    pub content_elapsed: std::time::Duration,
    pub content_blocks: usize,
    pub content_reset: bool,
    session_chrome: Option<(
        u64,
        u16,
        String,
        bool,
        usize,
        Vec<(Line<'static>, Option<SidebarHit>)>,
    )>,
    details_chrome: Option<(u64, u16, (Vec<Line<'static>>, Vec<Option<usize>>))>,
    built_typing: bool,
    /// Line range of the transcript block focused with the keyboard (highlighted).
    pub focus: Option<(usize, usize)>,
    pub pointer: Option<(u16, u16)>,
    pub component_hover: components::Hover,
    /// Sessions sidebar filter text and whether it is being edited.
    pub filter: String,
    pub filtering: bool,
    /// Recent microphone levels (0..1) and seconds since recording began, for the voice strip.
    pub voice_levels: Vec<f32>,
    pub voice_elapsed: u64,
    pub voice_cursor: Option<usize>,
    panel_body: Option<(Vec<String>, Vec<String>, String, u16, Vec<Line<'static>>)>,
    voice_text: String,
    voice_changed_at: usize,
    voice_stable_prefix: usize,
    /// Mouse selection in transcript coordinates: (line, column) start and end.
    pub selection: Option<((usize, usize), (usize, usize))>,
}
/// Order two (line, column) points.
pub fn ordered(a: (usize, usize), b: (usize, usize)) -> ((usize, usize), (usize, usize)) {
    if a <= b {
        (a, b)
    } else {
        (b, a)
    }
}
/// Plain text of the selected transcript rows (trailing spaces trimmed per row).
pub fn selected_text(cache: &Cache) -> String {
    let Some((a, b)) = cache.selection else {
        return String::new();
    };
    let ((first, from), (last, to)) = ordered(a, b);
    let mut out = Vec::new();
    for index in first..=last.min(cache.lines.len().saturating_sub(1)) {
        let Some(line) = cache.lines.get(index) else {
            break;
        };
        let mut column = 0usize;
        let mut text = String::new();
        for span in &line.spans {
            for g in span.content.graphemes(true) {
                let width = g.width();
                let start = if index == first { from } else { 0 };
                let end = if index == last { to } else { usize::MAX };
                if column + width > start && column < end {
                    text.push_str(g);
                }
                column += width;
            }
        }
        out.push(text.trim_end().to_string());
    }
    out.join("\n")
}
/// Reverse the columns `from..to` of a line (a selection highlight).
fn highlight(line: Line<'static>, from: usize, to: usize) -> Line<'static> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut column = 0usize;
    for span in line.spans {
        let mut plain = String::new();
        let mut marked = String::new();
        let flush = |plain: &mut String,
                     marked: &mut String,
                     spans: &mut Vec<Span<'static>>,
                     style: Style| {
            if !plain.is_empty() {
                spans.push(Span::styled(std::mem::take(plain), style));
            }
            if !marked.is_empty() {
                spans.push(Span::styled(
                    std::mem::take(marked),
                    style.add_modifier(Modifier::REVERSED),
                ));
            }
        };
        let mut in_range = false;
        for g in span.content.graphemes(true) {
            let width = g.width();
            let inside = column + width > from && column < to;
            if inside != in_range {
                flush(&mut plain, &mut marked, &mut spans, span.style);
                in_range = inside;
            }
            if inside {
                marked.push_str(g)
            } else {
                plain.push_str(g)
            }
            column += width;
        }
        flush(&mut plain, &mut marked, &mut spans, span.style);
    }
    Line::from(spans)
}

/// Highlight only the clickable columns of a context row, using its click map.
fn context_hover(
    line: Line<'static>,
    operation: Option<&Option<serde_json::Value>>,
    column: usize,
    background: Color,
) -> Line<'static> {
    let Some(operation) = operation.and_then(Option::as_ref) else {
        return line;
    };
    if operation["kind"] != "context_chips" {
        return line;
    }
    let range = operation["chips"].as_array().and_then(|chips| {
        chips.iter().find_map(|chip| {
            let start = chip["start"].as_u64()? as usize;
            let end = chip["end"].as_u64()? as usize;
            (start <= column && column < end).then_some((start, end))
        })
    });
    let Some((start, end)) = range else {
        return line;
    };
    let mut result = Line::default().style(line.style);
    let mut at = 0;
    for span in line.spans {
        for grapheme in span.content.graphemes(true) {
            let width = UnicodeWidthStr::width(grapheme);
            let style = if at < end && at + width > start {
                span.style.bg(background)
            } else {
                span.style
            };
            result.spans.push(Span::styled(grapheme.to_owned(), style));
            at += width;
        }
    }
    result
}
/// Keyboard-focusable transcript blocks: runs of consecutive lines that open the
/// same operation (what a click on them would do), as `(first line, last line)`.
pub fn targets(cache: &Cache) -> Vec<(usize, usize)> {
    let mut out: Vec<(usize, usize)> = Vec::new();
    let mut previous: Option<&serde_json::Value> = None;
    for (index, operation) in cache.operations.iter().enumerate() {
        match operation {
            Some(op) if previous == Some(op) => out.last_mut().unwrap().1 = index,
            Some(_) => out.push((index, index)),
            None => {}
        }
        previous = operation.as_ref();
    }
    out
}
pub const SPINNER_SLOT: char = '\u{e000}';
const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
/// Running turns, tools, sessions and usage refreshes need ~8 Hz redraws.
pub fn animating(s: &Snapshot) -> bool {
    s.panel_loading
        || matches!(s.voice_phase.as_str(), "recording" | "transcribing")
        || matches!(s.status.as_str(), "running" | "active" | "working")
        || s.blocks.iter().any(|block| {
            block.text.contains(SPINNER_SLOT)
                || (block.kind == "tool_group" && block.status == "running")
        })
        || s.sessions
            .iter()
            .chain(s.tabs.iter())
            .any(|row| row.status == "working")
}
/// Agent-colored moving fifth while working; a quiet rule while idle.
fn activity_meter(s: &Snapshot, p: &Palette, width: usize, tick: usize) -> Vec<Span<'static>> {
    let running = matches!(s.status.as_str(), "running" | "active" | "working");
    let accent = crate::transcript::color(&s.agent_color, p.blue, p);
    let size = (width / 5).max(1);
    let travel = width.saturating_sub(size).max(1);
    let phase = (tick % 180) as f64 / 180.0;
    let start = travel as f64 * (1.0 - (phase * std::f64::consts::TAU).cos()) / 2.0;
    (0..width)
        .map(|i| {
            let tone = if running {
                let coverage = ((i + 1) as f64).min(start + size as f64) - (i as f64).max(start);
                let amount = coverage.clamp(0.0, 1.0);
                match (p.border, accent) {
                    (Color::Rgb(r, g, b), Color::Rgb(br, bg, bb)) => {
                        let mix = |a: u8, z: u8| {
                            (a as f64 + (z as f64 - a as f64) * amount).round() as u8
                        };
                        Color::Rgb(mix(r, br), mix(g, bg), mix(b, bb))
                    }
                    _ => {
                        if amount > 0.5 {
                            accent
                        } else {
                            p.border
                        }
                    }
                }
            } else {
                let window = s.context_window.filter(|window| *window > 0);
                let ratio = window
                    .map(|window| s.context_used.unwrap_or(0) as f64 / window as f64)
                    .unwrap_or(0.0)
                    .clamp(0.0, 1.0);
                let soft = s.context_tiers.first().copied().unwrap_or(0);
                let amount = if soft > 0 && s.context_used.unwrap_or(0) >= soft {
                    (s.context_used.unwrap_or(0) as f64 / window.unwrap_or(1) as f64)
                        .clamp(0.0, 1.0)
                } else if soft > 0 {
                    (s.context_used.unwrap_or(0) as f64 / soft as f64).clamp(0.0, 1.0)
                } else {
                    ratio
                };
                if (i as f64) < amount * width as f64 {
                    accent
                } else {
                    p.border
                }
            };
            Span::styled("─", Style::default().fg(tone))
        })
        .collect()
}
fn with_spinner(line: &Line<'static>, frame: usize) -> Line<'static> {
    if !line
        .spans
        .iter()
        .any(|span| span.content.contains(SPINNER_SLOT))
    {
        return line.clone();
    }
    let glyph = SPINNER[frame % SPINNER.len()];
    let mut line = line.clone();
    for span in &mut line.spans {
        if span.content.contains(SPINNER_SLOT) {
            span.content = span.content.replace(SPINNER_SLOT, glyph).into();
        }
    }
    line
}
fn same_block(a: &crate::bridge::Content, b: &crate::bridge::Content) -> bool {
    if a.rev.is_empty() || b.rev.is_empty() {
        a == b
    } else {
        a.id == b.id && a.rev == b.rev
    }
}
impl Cache {
    pub fn session_rows(
        &mut self,
        s: &Snapshot,
        p: &Palette,
        width: u16,
    ) -> &[(Line<'static>, Option<SidebarHit>)] {
        let key = (
            s.revision,
            width,
            self.filter.clone(),
            self.filtering,
            self.spin,
        );
        let same = self.session_chrome.as_ref().is_some_and(|old| {
            (old.0, old.1, &old.2, old.3, old.4) == (key.0, key.1, &key.2, key.3, key.4)
        });
        if !same {
            self.session_chrome = Some((
                key.0,
                key.1,
                key.2,
                key.3,
                key.4,
                session_sidebar(
                    s,
                    p,
                    width as usize,
                    self.spin,
                    &self.filter,
                    self.filtering,
                ),
            ));
        }
        &self.session_chrome.as_ref().unwrap().5
    }
    pub fn detail_rows(
        &mut self,
        s: &Snapshot,
        p: &Palette,
        width: u16,
    ) -> &(Vec<Line<'static>>, Vec<Option<usize>>) {
        // Stored as a pair below to share exactly the drawing/hit-test rows.
        let same = self.details_chrome.as_ref().is_some_and(|old| {
            old.0 == s.revision.wrapping_add(s.ui_local_revision << 32) && old.1 == width
        });
        if !same {
            let (lines, hits) = details_rows(s, p, width);
            self.details_chrome = Some((
                s.revision.wrapping_add(s.ui_local_revision << 32),
                width,
                (lines, hits),
            ));
        }
        &self.details_chrome.as_ref().unwrap().2
    }

    pub fn update(&mut self, source: &[String], width: u16, palette: &Palette) {
        if self.source == source && self.width == width {
            return;
        }
        self.source = source.to_vec();
        self.width = width;
        self.lines.clear();
        for block in source {
            for line in block.lines() {
                let style = if line.starts_with('+') {
                    Style::default().fg(Color::Green)
                } else if line.starts_with('-') {
                    Style::default().fg(Color::Red)
                } else if line.starts_with("Turn ")
                    || line.starts_with("assistant")
                    || line.starts_with('#')
                {
                    Style::default()
                        .fg(palette.accent)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(palette.text)
                };
                let mut current = String::new();
                let mut columns = 0;
                for g in line.graphemes(true) {
                    let w = g.width();
                    if columns + w > usize::from(width.max(1)) && !current.is_empty() {
                        self.lines
                            .push(Line::styled(std::mem::take(&mut current), style));
                        columns = 0;
                    }
                    current.push_str(g);
                    columns += w;
                }
                self.lines.push(Line::styled(current, style));
            }
        }
    }
    pub fn note_patch(&mut self, from: Option<usize>) {
        self.patch_known = true;
        if from.is_some() {
            self.dirty_until = None;
        }
        if let Some(from) = from {
            self.dirty_from = Some(self.dirty_from.map_or(from, |old| old.min(from)));
        }
    }
    pub fn invalidate_disclosure(&mut self) {
        let from = self.disclosure.changed_from.take();
        let until = self.disclosure.changed_until.take();
        self.dirty_until = if self.dirty_from.is_none() {
            until
        } else {
            self.dirty_until.zip(until).map(|(old, new)| old.max(new))
        };
        if let Some(from) = from {
            self.dirty_from = Some(self.dirty_from.map_or(from, |old| old.min(from)));
        }
        self.patch_known = true;
    }
    pub fn update_content(
        &mut self,
        context: &[String],
        blocks: &[crate::bridge::Content],
        width: u16,
        palette: &Palette,
    ) {
        let began = std::time::Instant::now();
        self.content_elapsed = std::time::Duration::ZERO;
        self.content_blocks = 0;
        let reset = self.width != width
            || self.source != context
            || self.active_page != self.disclosure.scope
            || self.built_verbose != self.disclosure.verbose;
        if self.typing != self.built_typing {
            if let Some(index) = self.hints_index {
                let prior = self.dirty_from;
                self.dirty_from = Some(prior.map_or(index, |old| old.min(index)));
                if prior.is_none() {
                    self.dirty_until = Some(index + 1);
                }
            }
            self.built_typing = self.typing;
        }
        self.content_reset = reset;
        if reset || self.dirty_from.is_some() || !self.patch_known {
            // A turn leaving the newest-two window folds without any patch touching it.
            if let Some(index) = self.disclosure.sync_window(blocks) {
                if !reset {
                    self.dirty_from = Some(self.dirty_from.map_or(index, |old| old.min(index)));
                    self.dirty_until = None;
                    self.patch_known = true;
                }
            }
        }
        let start = if reset {
            0
        } else if let Some(from) = self.dirty_from {
            from.min(self.active_parts.len())
        } else if self.patch_known {
            return;
        } else {
            self.blocks
                .iter()
                .zip(blocks)
                .take_while(|(old, new)| same_block(old, new))
                .count()
        };
        if !reset && start == blocks.len() && self.blocks.len() == blocks.len() {
            return;
        }
        self.dirty_from = None;
        self.active_page = self.disclosure.scope.clone();
        self.built_typing = self.typing;
        self.built_verbose = self.disclosure.verbose;
        let stop = if reset {
            blocks.len()
        } else {
            self.dirty_until
                .take()
                .unwrap_or(blocks.len())
                .min(blocks.len())
        };
        let prefix_count = if reset {
            0
        } else {
            self.part_offsets
                .get(start)
                .copied()
                .unwrap_or(self.lines.parts.len())
        };
        let end_count = if stop == blocks.len() || reset {
            self.lines.parts.len()
        } else {
            self.part_offsets
                .get(stop)
                .copied()
                .unwrap_or(self.lines.parts.len())
        };
        let saved_parts = if stop < blocks.len() && stop < self.active_parts.len() {
            self.active_parts.split_off(stop)
        } else {
            Vec::new()
        };
        let saved_blocks = if stop < blocks.len() && stop < self.blocks.len() {
            self.blocks.split_off(stop)
        } else {
            Vec::new()
        };
        // Move untouched suffix row parts; only prefix offsets shift after disclosure.
        let saved_lines = self.lines.parts.split_off(end_count);
        let saved_operations = self.operations.parts.split_off(end_count);
        self.active_parts.truncate(start);
        self.blocks.truncate(start);
        self.part_offsets.truncate(start);
        self.lines.truncate_parts(prefix_count);
        self.operations.truncate_parts(prefix_count);
        if reset && !context.is_empty() {
            let mut rows = Vec::new();
            for row in context {
                for text in row.lines() {
                    for cells in
                        crate::transcript::wrap(&[Span::raw(text.to_owned())], usize::from(width))
                    {
                        rows.push(crate::transcript::line(
                            vec![],
                            cells,
                            None,
                            Style::default().fg(palette.muted),
                        ));
                    }
                }
            }
            self.operations
                .append_part(std::sync::Arc::new(vec![None; rows.len()]));
            self.lines.append_part(std::sync::Arc::new(rows));
        }
        self.content_blocks = stop - start;
        if reset {
            self.hints_index = blocks.iter().position(|block| block.kind == "hints");
        }
        for source in blocks.iter().take(stop).skip(start) {
            self.part_offsets.push(self.lines.parts.len());
            let part = self.disclosure.block(source).map(|block| {
                let key = format!("{}|{}", self.disclosure.scope, source.id);
                if let Some(part) = self.parts.get(&key).filter(|part| {
                    part.width == width
                        && same_block(&part.block, &block)
                        && !(block.kind == "hints" && self.typing)
                }) {
                    return part.clone();
                }
                let block = if self.typing && block.kind == "hints" {
                    let mut hidden = block.into_owned();
                    if !hidden.rev.is_empty() {
                        hidden.rev.push_str(":typing");
                    }
                    hidden.text = hidden
                        .text
                        .lines()
                        .map(|_| "\t")
                        .collect::<Vec<_>>()
                        .join("\n");
                    std::borrow::Cow::Owned(hidden)
                } else {
                    block
                };
                let rows = context::build(&block, width, palette);
                let part = std::sync::Arc::new(Part {
                    block: if block.rev.is_empty() {
                        block.into_owned()
                    } else {
                        crate::bridge::Content {
                            id: block.id.clone(),
                            rev: block.rev.clone(),
                            ..Default::default()
                        }
                    },
                    width,
                    operations: std::sync::Arc::new(
                        rows.iter().map(|(_, op)| op.clone()).collect(),
                    ),
                    lines: std::sync::Arc::new(rows.into_iter().map(|(line, _)| line).collect()),
                });
                if self.parts.len() >= 32768 {
                    self.parts.clear();
                }
                self.parts.insert(key, part.clone());
                part
            });
            if let Some(part) = &part {
                self.lines.append_part(part.lines.clone());
                self.operations.append_part(part.operations.clone());
            }
            self.active_parts.push(part);
            self.blocks.push(if source.rev.is_empty() {
                source.clone()
            } else {
                crate::bridge::Content {
                    id: source.id.clone(),
                    rev: source.rev.clone(),
                    ..Default::default()
                }
            });
        }
        let mut line_suffix = saved_lines.into_iter();
        let mut operation_suffix = saved_operations.into_iter();
        for part in saved_parts {
            self.part_offsets.push(self.lines.parts.len());
            if part.is_some() {
                self.lines.append_part(line_suffix.next().unwrap());
                self.operations
                    .append_part(operation_suffix.next().unwrap());
            }
            self.active_parts.push(part);
        }
        self.blocks.extend(saved_blocks);
        self.source = context.to_vec();
        self.width = width;
        self.content_elapsed = began.elapsed();
    }
    pub fn reset(&mut self) {
        self.width = 0;
        self.dirty_from = Some(0);
        self.parts.clear();
        self.session_chrome = None;
        self.details_chrome = None;
    }
}
#[derive(Default, Clone, Copy)]
pub struct Regions {
    pub transcript: Rect,
    pub composer: Rect,
    pub sessions: Rect,
    pub details: Rect,
    pub tabs: Rect,
    pub workspace: Rect,
    pub context: Rect,
}
/// Composer height: the editor grows with its wrapped content up to the terminal's
/// `max-height: 22` (9-row resting layout), leaving the
/// transcript at least four rows.
/// Rows above the editor for queued messages (the terminal's input-queue preview).
pub fn queue_rows(s: &Snapshot) -> u16 {
    s.queue_lines.len().min(4) as u16
}
pub fn composer_height(area: Rect, draft: &Editor, s: &Snapshot) -> u16 {
    if !s.agent_page.is_empty() {
        return 3;
    }
    let width = regions(area, s, 0, false).composer.width.saturating_sub(9);
    let mut displayed = Editor::default();
    displayed.text = draft.text.clone();
    displayed.cursor = draft.cursor;
    if matches!(s.voice_phase.as_str(), "recording" | "transcribing") {
        displayed.text.insert_str(draft.cursor, &s.voice_preview);
        displayed.cursor += s.voice_preview.len();
    }
    let rows = editor_view(&displayed, false, &Palette::new(false), width, u16::MAX).len();
    let wanted = 6 + rows.clamp(2, 22) as u16 + queue_rows(s);
    wanted
        .min(area.height.saturating_sub(2 + 4))
        .min(area.height)
        .max(area.height.min(7))
}
pub fn regions(area: Rect, s: &Snapshot, composer_height: u16, _logs_open: bool) -> Regions {
    let both = area.width >= 130;
    let details_wins = s.last_opened != "sessions";
    let left = if s.sessions_sidebar
        && area.width >= 90
        && (!s.details_sidebar || both || !details_wins)
    {
        30
    } else {
        0
    };
    let right = if s.details_sidebar
        && area.width >= 100
        && (!s.sessions_sidebar || both || details_wins)
    {
        40
    } else {
        0
    };
    let columns = Layout::horizontal([
        Constraint::Length(left),
        Constraint::Min(1),
        Constraint::Length(right),
    ])
    .split(area);
    let top = if !s.agent_page.is_empty() {
        1
    } else if left > 0 {
        0
    } else {
        1
    };
    let center = Layout::vertical([
        Constraint::Length(top),
        Constraint::Min(1),
        Constraint::Length(composer_height),
    ])
    .split(columns[1]);
    let drawer = s.details_sidebar && area.width < 100;
    Regions {
        tabs: center[0],
        workspace: Rect::new(
            center[2].x,
            center[2].bottom().saturating_sub(2),
            center[2].width,
            1,
        ),
        sessions: columns[0],
        details: if drawer {
            Rect::new(
                area.right().saturating_sub(area.width.min(40)),
                area.y,
                area.width.min(40),
                area.height,
            )
        } else {
            columns[2]
        },
        context: Rect::default(),
        transcript: center[1],
        composer: center[2],
    }
}
pub fn editor_text(editor: &Editor, secret: bool, palette: &Palette) -> Vec<Line<'static>> {
    let mut lines = vec![Line::default()];
    let selection = editor.selection();
    // The cursor is a highlighted cell over the character it sits on (a space at the end of
    // a line), so it never shifts the text. Selections use the accent colour instead.
    let cursor_style = Style::default().bg(palette.text).fg(palette.background);
    for (i, g) in editor.text.grapheme_indices(true) {
        let at_cursor = i == editor.cursor;
        if g == "\n" {
            if at_cursor {
                lines
                    .last_mut()
                    .unwrap()
                    .spans
                    .push(Span::styled(" ", cursor_style));
            }
            lines.push(Line::default());
            continue;
        }
        let style = if at_cursor {
            cursor_style
        } else if selection.is_some_and(|(a, b)| i >= a && i < b) {
            Style::default().bg(palette.accent).fg(palette.background)
        } else {
            Style::default().fg(palette.text)
        };
        lines.last_mut().unwrap().spans.push(Span::styled(
            if secret {
                "•".to_string()
            } else {
                if g.chars().any(char::is_control) {
                    g.chars().flat_map(char::escape_default).collect()
                } else {
                    g.to_string()
                }
            },
            style,
        ));
    }
    if editor.cursor >= editor.text.len() {
        lines
            .last_mut()
            .unwrap()
            .spans
            .push(Span::styled(" ", cursor_style));
    }
    lines
}
pub fn draw(
    frame: &mut Frame,
    s: &Snapshot,
    draft: &Editor,
    form: &Editor,
    answer: &Editor,
    filter: &str,
    selection: usize,
    scroll: usize,
    panel_scroll: usize,
    cache: &mut Cache,
    follow: bool,
    logs_open: bool,
    _leader: bool,
    panel_detail: bool,
    sessions_scroll: usize,
    logs_scroll: usize,
    details_scroll: usize,
) -> Regions {
    let p = Palette::new(s.theme == "nexus-light");
    frame.render_widget(
        Block::default().style(Style::default().bg(p.background).fg(p.text)),
        frame.area(),
    );
    let r = regions(
        frame.area(),
        s,
        composer_height(frame.area(), draft, s),
        logs_open,
    );
    draw_top_bar(frame, s, r.tabs, &p, cache.spin);
    if r.sessions.width > 0 {
        let inner = usize::from(r.sessions.width.saturating_sub(3));
        let height = usize::from(r.sessions.height.saturating_sub(3));
        let mut lines: Vec<Line<'static>> = cache
            .session_rows(s, &p, inner as u16)
            .iter()
            .skip(1 + sessions_scroll)
            .take(height)
            .map(|(line, _)| line.clone())
            .collect();
        lines.resize(height, Line::default());
        frame.render_widget(
            Paragraph::new(lines)
                .block(
                    Block::default()
                        .borders(Borders::RIGHT)
                        .border_style(Style::default().fg(p.border_strong))
                        .padding(ratatui::widgets::Padding::new(1, 1, 2, 0)),
                )
                .style(Style::default().bg(p.panel)),
            r.sessions,
        );
    }
    cache.content_elapsed = std::time::Duration::ZERO;
    cache.typing = !draft.text.is_empty();
    cache.page = s.agent_page.clone();
    if s.blocks.is_empty() {
        cache.update(&s.lines, r.transcript.width, &p);
    } else {
        cache.update_content(&[], &s.blocks, r.transcript.width, &p);
    }
    let height = r.transcript.height as usize;
    // Sub-agent pages are read-only: leave 30% of the viewport below the last row.
    cache.pad = if s.agent_page.is_empty() {
        0
    } else {
        height * 3 / 10
    };
    let total = cache.lines.len() + cache.pad;
    cache.max_scroll = total.saturating_sub(height);
    let offset = if follow {
        cache.max_scroll
    } else {
        scroll.min(cache.max_scroll)
    };
    frame.render_widget(
        Paragraph::new(
            cache
                .lines
                .range(offset, r.transcript.height as usize)
                .enumerate()
                .map(|(row, line)| {
                    let mut line = with_spinner(line, cache.spin);
                    if s.panel_title.is_empty() && s.prompt.is_none() {
                        if let Some((x, y)) = cache.pointer {
                            if r.transcript.contains((x, y).into())
                                && usize::from(y - r.transcript.y) == row
                            {
                                line = context_hover(
                                    line,
                                    cache.operations.get(offset + row),
                                    usize::from(x - r.transcript.x),
                                    p.element_hi,
                                );
                            }
                        }
                    }
                    if let Some((a, b)) = cache.selection {
                        let ((first, from), (last, to)) = ordered(a, b);
                        let at = offset + row;
                        if (first..=last).contains(&at) {
                            let start = if at == first { from } else { 0 };
                            let end = if at == last { to } else { usize::MAX };
                            line = highlight(line, start, end);
                        }
                    }
                    match cache.focus {
                        Some((first, last)) if (first..=last).contains(&(offset + row)) => {
                            let mut line = line;
                            for span in &mut line.spans {
                                span.style = span.style.bg(p.element_hi);
                            }
                            line.style = line.style.bg(p.element_hi);
                            line
                        }
                        _ => line,
                    }
                })
                .collect::<Vec<_>>(),
        ),
        r.transcript,
    );
    if total > height && r.transcript.width > 2 {
        // A thin scrollbar on the transcript's right edge, like the terminal's.
        let mut state = ratatui::widgets::ScrollbarState::new(cache.max_scroll).position(offset);
        frame.render_stateful_widget(
            ratatui::widgets::Scrollbar::new(ratatui::widgets::ScrollbarOrientation::VerticalRight)
                .begin_symbol(None)
                .end_symbol(None)
                .track_symbol(Some(" "))
                .thumb_symbol("▐")
                .thumb_style(Style::default().fg(p.border_strong))
                .track_style(Style::default().bg(p.background)),
            r.transcript,
            &mut state,
        );
    }
    let rows = composer_rows(r.composer, s);
    let inset = |a: Rect, left: u16, right: u16| Rect {
        x: a.x + left,
        width: a.width.saturating_sub(left + right),
        ..a
    };
    let agent_color = crate::transcript::color(&s.agent_color, p.blue, &p);
    // The chat input box: agent-colored left bar spanning editor and runtime rows.
    let box_area = Rect {
        x: rows[1].x + 2,
        width: rows[1].width.saturating_sub(4),
        y: rows[1].y,
        height: rows[1].height + 2, // editor, controls and one row of bottom padding
    };
    if !s.agent_page.is_empty() {
        frame.render_widget(
            Paragraph::new("Sub agent · read-only · Esc or ↑ returns to the parent")
                .style(Style::default().fg(p.muted).bg(p.panel))
                .block(Block::default().padding(ratatui::widgets::Padding::new(2, 2, 1, 0))),
            r.composer,
        );
    } else {
        // A heavy agent-coloured rail, then a sliver of background, then the card.
        frame.render_widget(
            Block::default().style(Style::default().bg(p.panel)),
            Rect {
                x: box_area.x + 1,
                width: box_area.width.saturating_sub(1),
                ..box_area
            },
        );
        for y in box_area.y..box_area.y + box_area.height {
            frame.render_widget(
                Paragraph::new("▎").style(Style::default().fg(agent_color).bg(p.background)),
                Rect {
                    x: box_area.x,
                    y,
                    width: 1,
                    height: 1,
                },
            );
        }
        let editor_area = Rect {
            x: box_area.x + 3,
            y: rows[1].y + 1,
            width: box_area.width.saturating_sub(5),
            height: rows[1].height.saturating_sub(1),
        };
        let preview = if matches!(s.voice_phase.as_str(), "recording" | "transcribing") {
            animated_voice_preview(cache, &s.voice_preview)
        } else {
            String::new()
        };
        let mut displayed = Editor::default();
        displayed.text = draft.text.clone();
        displayed.cursor = draft.cursor;
        if preview.is_empty() {
            displayed.anchor = draft.anchor; // the selection highlight needs the anchor
        }
        if !preview.is_empty() {
            let at = cache
                .voice_cursor
                .unwrap_or(draft.cursor)
                .min(draft.text.len());
            if draft.text.is_char_boundary(at) {
                let prefix = if at > 0 && !draft.text[..at].ends_with(char::is_whitespace) {
                    " "
                } else {
                    ""
                };
                let suffix = if at < draft.text.len()
                    && !draft.text[at..].starts_with(char::is_whitespace)
                {
                    " "
                } else {
                    ""
                };
                let addition = format!("{prefix}{preview}{suffix}");
                displayed.text.insert_str(at, &addition);
                if displayed.cursor >= at {
                    displayed.cursor += addition.len();
                }
            }
        }
        if displayed.text.is_empty() {
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled("T", Style::default().bg(p.text).fg(p.background)),
                    Span::styled("ype a message…", Style::default().fg(p.quiet)),
                ]))
                .style(Style::default().bg(p.panel)),
                editor_area,
            );
        } else {
            frame.render_widget(
                Paragraph::new(editor_view(
                    &displayed,
                    false,
                    &p,
                    editor_area.width,
                    editor_area.height,
                ))
                .style(Style::default().bg(p.panel)),
                editor_area,
            );
        }
        frame.render_widget(
            Paragraph::new({
                let mut lines: Vec<Line<'static>> = s
                    .queue_lines
                    .iter()
                    .take(4)
                    .map(|row| Line::styled(row.clone(), Style::default().fg(p.quiet)))
                    .collect();
                lines.push(Line::styled(
                    s.attachment_lines.join(" · "),
                    Style::default().fg(p.accent),
                ));
                lines
            }),
            inset(rows[0], 2, 2),
        );
        let controls = Rect {
            x: box_area.x + 3,
            y: rows[2].y,
            width: box_area.width.saturating_sub(5),
            height: 1,
        };
        frame.render_widget(
            Paragraph::new(Line::from(chrome::control_spans(
                s,
                &p,
                controls.width as usize,
                &cache.component_hover,
                std::time::Instant::now(),
            )))
            .style(Style::default().bg(p.panel)),
            controls,
        );
        chrome::draw_workspace_bar(frame, s, r.workspace, &p, cache.spin);
        let meter_area = inset(rows[5], 2, 2);
        frame.render_widget(
            Paragraph::new(Line::from(activity_meter(
                s,
                &p,
                usize::from(meter_area.width),
                cache.activity_frame,
            ))),
            meter_area,
        );
    }
    if r.sessions.width > 0 {
        let title = format!(
            "☰ Sessions{}+",
            " ".repeat(r.sessions.width.saturating_sub(13) as usize)
        );
        frame.render_widget(
            Paragraph::new(title).style(Style::default().fg(p.text).bg(p.panel)),
            Rect::new(
                r.sessions.x + 1,
                r.sessions.y,
                r.sessions.width.saturating_sub(2),
                1,
            ),
        );
        let field = if cache.filter.is_empty() && !cache.filtering {
            "Filter sessions".into()
        } else {
            format!("{}{}", cache.filter, if cache.filtering { "█" } else { "" })
        };
        frame.render_widget(
            Paragraph::new(field).style(Style::default().fg(p.quiet).bg(p.panel)),
            Rect::new(
                r.sessions.x + 1,
                r.sessions.bottom().saturating_sub(1),
                r.sessions.width.saturating_sub(2),
                1,
            ),
        );
    }
    if r.details.width > 0 {
        frame.render_widget(Clear, r.details);
        frame.render_widget(
            Block::default()
                .borders(Borders::LEFT)
                .border_style(Style::default().fg(p.border_strong))
                .style(Style::default().bg(p.panel)),
            r.details,
        );
        let strip = Rect {
            x: r.details.x + 2,
            y: r.details.y,
            width: r.details.width.saturating_sub(3),
            height: 1,
        };
        let mut spans = Vec::new();
        for tab in ["Session", "Files", "MCP", "Logs"] {
            spans.push(Span::styled(
                format!("{tab} "),
                Style::default()
                    .fg(if s.details_panel.tab == tab {
                        p.text
                    } else {
                        p.muted
                    })
                    .add_modifier(if s.details_panel.tab == tab {
                        Modifier::UNDERLINED
                    } else {
                        Modifier::empty()
                    }),
            ));
        }

        let header_lines: Vec<Line<'static>> = if s.details_panel.tab == "Logs" {
            s.details_panel
                .logs_header
                .iter()
                .flat_map(|(label, value)| {
                    crate::transcript::wrap(
                        &[Span::styled(
                            format!(
                                "{label:<10}{value}{}",
                                if label == "Session" { " ⧉" } else { "" }
                            ),
                            Style::default().fg(p.muted),
                        )],
                        r.details.width.saturating_sub(5) as usize,
                    )
                    .into_iter()
                    .map(|cells| crate::transcript::line(vec![], cells, None, Style::default()))
                })
                .collect()
        } else {
            vec![]
        };
        let header_height = (header_lines.len() as u16).min(r.details.height.saturating_sub(4));
        let body_area = Rect {
            y: r.details.y + header_height,
            height: r.details.height.saturating_sub(header_height),
            ..r.details
        };
        frame.render_widget(
            Paragraph::new({
                let rows = cache.detail_rows(s, &p, r.details.width.saturating_sub(5));
                let offset = if s.details_panel.tab == "Logs" {
                    rows.0
                        .len()
                        .saturating_sub(body_area.height.saturating_sub(2) as usize)
                        .saturating_sub(logs_scroll)
                } else {
                    details_scroll.min(
                        rows.0
                            .len()
                            .saturating_sub(r.details.height.saturating_sub(1) as usize),
                    )
                };
                rows.0.iter().skip(offset).cloned().collect::<Vec<_>>()
            })
            .block(
                Block::default()
                    .borders(Borders::LEFT)
                    .border_style(Style::default().fg(p.border_strong))
                    .padding(ratatui::widgets::Padding::new(2, 2, 1, 0)),
            )
            .style(Style::default().bg(p.panel).fg(p.text)),
            body_area,
        );
        if header_height > 0 {
            frame.render_widget(
                Paragraph::new(header_lines).style(Style::default().bg(p.panel)),
                Rect {
                    x: r.details.x + 3,
                    y: r.details.y + 1,
                    width: r.details.width.saturating_sub(5),
                    height: header_height,
                },
            );
            frame.render_widget(
                Paragraph::new("│".repeat(1))
                    .style(Style::default().fg(p.border_strong).bg(p.panel)),
                Rect::new(r.details.x, r.details.y, 1, header_height + 1),
            );
        }
        frame.render_widget(
            Paragraph::new(Line::from(spans)).style(Style::default().bg(p.panel)),
            strip,
        );
    }
    if !s.panel_title.is_empty() {
        let area = panel_area(r.transcript, s);
        let title = if s.panel_loading {
            format!(
                "{} {} · refreshing",
                SPINNER[cache.spin % SPINNER.len()],
                s.panel_title
            )
        } else {
            s.panel_title.clone()
        };
        let mut inner = dialog_frame(frame, area, &title, &p, s.panel_layout == "drawer");
        if let Some(nav) = &s.nav {
            let list = nav_rect(area);
            let lines: Vec<Line<'static>> = nav
                .items
                .iter()
                .enumerate()
                .take(list.height as usize)
                .map(|(i, (label, _, heading))| {
                    if *heading {
                        Line::styled(
                            label.clone(),
                            Style::default()
                                .fg(p.quiet)
                                .bg(p.dialog)
                                .add_modifier(Modifier::BOLD),
                        )
                    } else if i as i64 == nav.selected {
                        Line::styled(
                            format!(
                                "{:<w$}",
                                format!(
                                    "{} {label}",
                                    if cache.settings_nav_focus {
                                        "▶"
                                    } else {
                                        "▸"
                                    }
                                ),
                                w = usize::from(list.width)
                            ),
                            components::selectable(
                                &p,
                                components::State {
                                    selected: true,
                                    focused: cache.settings_nav_focus,
                                    hover: cache.component_hover.amount_id(
                                        components::HoverId::SettingsNav(i),
                                        std::time::Instant::now(),
                                    ),
                                    ..Default::default()
                                },
                            ),
                        )
                    } else {
                        Line::styled(
                            format!("  {label}"),
                            components::selectable(
                                &p,
                                components::State {
                                    hover: cache.component_hover.amount_id(
                                        components::HoverId::SettingsNav(i),
                                        std::time::Instant::now(),
                                    ),
                                    ..Default::default()
                                },
                            ),
                        )
                    }
                })
                .collect();
            frame.render_widget(
                Paragraph::new(lines).style(Style::default().bg(p.dialog)),
                list,
            );
            let taken = list.width + 2;
            inner = Rect {
                x: inner.x + taken,
                width: inner.width.saturating_sub(taken),
                ..inner
            };
        }
        if s.panel_format == "image" && !s.preview_image.is_empty() {
            let areas = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(inner);
            frame.render_widget(
                Paragraph::new(s.panel_lines.join("\n")).style(Style::default().fg(p.text)),
                areas[0],
            );
            if cache.image_source != s.preview_image {
                cache.image_protocol = base64::engine::general_purpose::STANDARD
                    .decode(&s.preview_image)
                    .ok()
                    .and_then(|bytes| {
                        let mut reader = image::ImageReader::new(std::io::Cursor::new(bytes))
                            .with_guessed_format()
                            .ok()?;
                        let mut limits = image::Limits::default();
                        limits.max_image_width = Some(8192);
                        limits.max_image_height = Some(8192);
                        limits.max_alloc = Some(128 * 1024 * 1024);
                        reader.limits(limits);
                        reader.decode().ok()
                    })
                    .map(|image| {
                        // The input reader already owns the terminal. Querying here
                        // would race it for capability responses and stall rendering.
                        let picker = ratatui_image::picker::Picker::from_fontsize((10, 20));
                        picker.new_resize_protocol(image)
                    });
                cache.image_source.clone_from(&s.preview_image);
            }
            if let Some(protocol) = cache.image_protocol.as_mut() {
                frame.render_stateful_widget(
                    ratatui_image::StatefulImage::new(),
                    areas[1],
                    protocol,
                );
            } else {
                frame.render_widget(
                    Paragraph::new("Image could not be decoded for preview")
                        .style(Style::default().fg(p.muted)),
                    areas[1],
                );
            }
        } else if let Some(f) = &s.form {
            let areas = Layout::vertical([Constraint::Min(1), Constraint::Length(2)]).split(inner);
            frame.render_widget(
                Paragraph::new(editor_view(
                    form,
                    f.secret,
                    &p,
                    areas[0].width,
                    areas[0].height,
                ))
                .style(Style::default().bg(p.dialog)),
                areas[0],
            );
            frame.render_widget(
                Paragraph::new(format!(
                    "{}\nCtrl+S save · Ctrl+D delete · Escape close",
                    f.status
                ))
                .style(Style::default().fg(p.accent).bg(p.dialog)),
                areas[1],
            );
        } else if !s.items.is_empty() && !panel_detail {
            dialogs::draw_menu(
                frame,
                s,
                inner,
                filter,
                selection,
                &p,
                &cache.component_hover,
            );
        } else {
            let rebuild =
                cache
                    .panel_body
                    .as_ref()
                    .is_none_or(|(source, tones, format, width, _)| {
                        source != &s.panel_lines
                            || tones != &s.panel_tones
                            || format != &s.panel_format
                            || *width != inner.width
                    });
            if rebuild {
                let mut panel = Cache::default();
                if s.panel_format == "markdown" {
                    let block = crate::bridge::Content {
                        kind: "markdown".into(),
                        text: s.panel_lines.join("\n"),
                        ..Default::default()
                    };
                    panel.lines = crate::transcript::build(&block, inner.width, &p)
                        .into_iter()
                        .map(|(line, _)| line)
                        .collect::<Vec<_>>()
                        .into();
                } else if s.panel_tones.len() == s.panel_lines.len() && !s.panel_tones.is_empty() {
                    panel.lines =
                        toned_lines(&s.panel_lines, &s.panel_tones, inner.width, &p).into();
                } else {
                    panel.update(&s.panel_lines, inner.width, &p);
                }
                cache.panel_body = Some((
                    s.panel_lines.clone(),
                    s.panel_tones.clone(),
                    s.panel_format.clone(),
                    inner.width,
                    panel.lines.iter().cloned().collect(),
                ));
            }
            let body = &cache.panel_body.as_ref().unwrap().4;
            let offset = panel_scroll.min(body.len().saturating_sub(inner.height as usize));
            frame.render_widget(
                Paragraph::new(
                    body.iter()
                        .skip(offset)
                        .take(inner.height as usize)
                        .cloned()
                        .collect::<Vec<_>>(),
                )
                .style(Style::default().bg(p.dialog)),
                inner,
            );
        }
    }
    if let Some(prompt) = &s.prompt {
        // The terminal shows approvals and questions as a short panel above the
        // composer with the transcript still visible.
        let area = prompt_area(r.transcript, prompt);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Block::default().style(Style::default().bg(p.panel).fg(p.text)),
            area,
        );
        let title = if prompt.kind == "permission" {
            "Permission requested"
        } else {
            "Question"
        };
        frame.render_widget(
            Paragraph::new(title).style(
                Style::default()
                    .fg(p.accent)
                    .bg(p.panel)
                    .add_modifier(Modifier::BOLD),
            ),
            Rect {
                x: area.x + 2,
                y: area.y,
                width: area.width.saturating_sub(4),
                height: 1,
            },
        );
        let (content_area, choices_area) = prompt_regions(area, prompt.choices.len());
        let mut content = Cache::default();
        content.update(&prompt.lines, content_area.width, &p);
        frame.render_widget(
            Paragraph::new(
                content
                    .lines
                    .iter()
                    .cloned()
                    .skip(panel_scroll)
                    .take(content_area.height as usize)
                    .collect::<Vec<_>>(),
            )
            .style(Style::default().bg(p.panel)),
            content_area,
        );
        let mut lines: Vec<Line> = prompt
            .choices
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let on = i == selection;
                let text = format!(
                    " {} [{}] {}{}",
                    if on { "▸" } else { " " },
                    c.key,
                    c.label,
                    if c.disabled { " (unavailable)" } else { "" }
                );
                let pad = usize::from(choices_area.width).saturating_sub(text.width());
                let style = components::selectable(
                    &p,
                    components::State {
                        disabled: c.disabled,
                        selected: on,
                        hover: cache.component_hover.amount_id(
                            components::HoverId::PromptChoice(i),
                            std::time::Instant::now(),
                        ),
                        ..Default::default()
                    },
                );
                Line::styled(format!("{text}{}", " ".repeat(pad)), style)
            })
            .collect();
        if prompt.kind == "question" {
            lines.push(Line::from(vec![
                Span::styled(" Answer ", Style::default().fg(p.quiet)),
                Span::styled(format!("{}█", answer.text), Style::default().fg(p.text)),
                Span::styled("   Enter submit", Style::default().fg(p.quiet)),
            ]));
        }
        lines.push(Line::styled(
            " ↑/↓ choose · Enter select · keys pick · Esc deny",
            Style::default().fg(p.quiet),
        ));
        frame.render_widget(
            Paragraph::new(lines).style(Style::default().bg(p.panel)),
            choices_area,
        );
    }
    if s.voice_phase == "recording" {
        let tone = if cache.spin % 8 < 4 {
            p.accent
        } else {
            p.quiet
        };
        frame.render_widget(
            Paragraph::new("■").style(Style::default().fg(tone).bg(p.panel)),
            Rect::new(box_area.x + 3, rows[5].y, 1, 1),
        );
    }
    r
}

fn truncate_width(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_string();
    }
    if width == 0 {
        return String::new();
    }
    let mut out = String::new();
    let mut used = 0;
    for grapheme in text.graphemes(true) {
        let gw = grapheme.width();
        if used + gw > width {
            break;
        }
        out.push_str(grapheme);
        used += gw;
    }
    out
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unread_completed_tab_has_a_solid_blue_dot() {
        let mut s = Snapshot::default();
        s.tabs = vec![crate::bridge::Session {
            id: "finished".into(),
            title: "Finished agent".into(),
            status: "done".into(),
            ..Default::default()
        }];
        let p = Palette::new(false);
        let backend = ratatui::backend::TestBackend::new(80, 2);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| {
                draw_top_bar(frame, &s, frame.area(), &p, 0);
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let dot = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == "●")
            .unwrap();
        assert_eq!(dot.fg, p.blue);
    }
    use ratatui::{backend::TestBackend, Terminal};
    #[test]
    fn context_hover_matches_click_bounds_and_preserves_foreground() {
        let p = Palette::new(false);
        let line = Line::from(vec![
            Span::raw(" "),
            Span::styled(
                "◈ Title  ",
                Style::default().fg(p.accent).add_modifier(Modifier::BOLD),
            ),
            Span::raw(" "),
        ]);
        let operation = Some(serde_json::json!({
            "kind": "context_chips",
            "chips": [{"start": 1, "end": 10, "name": "Instructions"}]
        }));
        for column in [1, 5, 9] {
            let hovered = context_hover(line.clone(), Some(&operation), column, p.element_hi);
            assert_eq!(hovered.spans[0].style.bg, None);
            assert_eq!(hovered.spans[10].style.bg, None);
            for span in &hovered.spans[1..10] {
                assert_eq!(span.style.bg, Some(p.element_hi));
                assert_eq!(span.style.fg, Some(p.accent));
                assert!(span.style.add_modifier.contains(Modifier::BOLD));
            }
        }
        for column in [0, 10, 20] {
            assert_eq!(
                context_hover(line.clone(), Some(&operation), column, p.element_hi),
                line
            );
        }
        assert_eq!(context_hover(line.clone(), None, 1, p.element_hi), line);
        let other = Some(serde_json::json!({"kind": "tool"}));
        assert_eq!(
            context_hover(line.clone(), Some(&other), 1, p.element_hi),
            line
        );
    }
    #[test]
    fn context_hover_follows_pointer_and_clears_on_exit() {
        let s: Snapshot = serde_json::from_value(serde_json::json!({
            "schema": 1, "revision": 1,
            "blocks": [{"id": "header", "kind": "context_header", "members": [
                {"id": "instructions", "title": "Instructions", "text": "1 file", "operation": {"kind": "context_show", "key": "instructions"}}
            ]}]
        })).unwrap();
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        let mut cache = Cache::default();
        let mut paint = |cache: &mut Cache| {
            let mut regions = Regions::default();
            terminal
                .draw(|frame| {
                    regions = draw(
                        frame,
                        &s,
                        &Editor::default(),
                        &Editor::default(),
                        &Editor::default(),
                        "",
                        0,
                        0,
                        0,
                        cache,
                        false,
                        false,
                        false,
                        false,
                        0,
                        0,
                        0,
                    );
                })
                .unwrap();
            (regions, terminal.backend().buffer().clone())
        };
        let (regions, _) = paint(&mut cache);
        let row = (0..cache.lines.len())
            .find(|row| {
                cache
                    .operations
                    .get(*row)
                    .and_then(Option::as_ref)
                    .is_some_and(|op| op["kind"] == "context_chips")
            })
            .unwrap();
        let operation = cache.operations.get(row).unwrap().as_ref().unwrap();
        let column = operation["chips"][0]["start"].as_u64().unwrap() as u16;
        let cell = (
            regions.transcript.x + column,
            regions.transcript.y + row as u16,
        );
        cache.pointer = Some(cell);
        let (_, hovered) = paint(&mut cache);
        assert_eq!(hovered[cell].bg, Palette::new(false).element_hi);
        cache.pointer = Some((0, 0));
        let (_, cleared) = paint(&mut cache);
        assert_ne!(cleared[cell].bg, Palette::new(false).element_hi);
    }
    #[test]
    fn activity_rule_is_quiet_when_idle_and_agent_colored_when_running() {
        let p = Palette::new(false);
        let mut s = Snapshot {
            context_used: Some(60),
            context_window: Some(100),
            context_marks: vec![80],
            ..Default::default()
        };
        // Idle: the rule shows context fill (60 of 100 -> 6 of 10 cells in the
        // agent colour); the rest stays the quiet border colour.
        let meter = activity_meter(&s, &p, 10, 0);
        assert!(meter.iter().all(|span| span.content == "─"));
        assert!(meter[..6].iter().all(|span| span.style.fg == Some(p.blue)));
        assert!(meter[6..]
            .iter()
            .all(|span| span.style.fg == Some(p.border)));
        let empty = Snapshot::default();
        assert!(activity_meter(&empty, &p, 10, 0)
            .iter()
            .all(|span| span.style.fg == Some(p.border)));
        s.status = "running".into();
        let first = activity_meter(&s, &p, 10, 0);
        let next = activity_meter(&s, &p, 10, 3);
        assert_eq!(
            first
                .iter()
                .filter(|span| span.style.fg == Some(p.blue))
                .count(),
            2
        );
        assert_ne!(first, next);
        assert!(next.iter().all(|span| span.content == "─"));
    }
    #[test]
    fn hints_blank_while_typing_but_keep_their_rows() {
        let palette = Palette::new(false);
        let blocks = vec![crate::bridge::Content {
            id: "empty-hints".into(),
            kind: "hints".into(),
            gap: 4,
            text: "esc\tstop\n  /\tcommands".into(),
            ..Default::default()
        }];
        let mut cache = Cache::default();
        cache.update_content(&[], &blocks, 80, &palette);
        let shown = cache.lines.len();
        assert_eq!(shown, 6);
        assert!(cache.lines[4]
            .spans
            .iter()
            .any(|span| span.content.contains("stop")));
        cache.typing = true;
        cache.update_content(&[], &blocks, 80, &palette);
        assert_eq!(cache.lines.len(), shown);
        assert!(!cache.lines[4]
            .spans
            .iter()
            .any(|span| span.content.contains("stop")));
    }
    #[test]
    fn modified_file_rows_expand_with_a_colored_diff_and_map_to_files() {
        use crate::bridge::FileChange;
        let mut s = Snapshot::default();
        s.details_panel.files = vec![
            FileChange {
                path: "src/a.rs".into(),
                added: 1,
                removed: 1,
                open: true,
                diff: vec!["@@ -1 +1 @@".into(), "-old".into(), "+new".into()],
                ..Default::default()
            },
            FileChange {
                path: "b.rs".into(),
                created: true,
                ..Default::default()
            },
        ];
        let (lines, files) = details_rows(&s, &Palette::new(false), 40);
        assert_eq!(lines.len(), files.len());
        let first = files.iter().position(|f| *f == Some(0)).unwrap();
        let text: String = lines[first]
            .spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect();
        assert!(text.starts_with("▾ M ") && text.contains("a.rs"));
        assert_eq!(lines[first + 2].spans[0].content, "  -old");
        assert_eq!(files[first + 1], None, "diff rows do not toggle");
        let second: String = lines[files.iter().position(|f| *f == Some(1)).unwrap()]
            .spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect();
        assert!(second.starts_with("▸ A "));
    }
    #[test]
    fn session_cards_show_status_words_and_map_clicks() {
        use crate::bridge::Session;
        let mut s = Snapshot::default();
        s.sessions = vec![
            Session {
                group: "Today".into(),
                id: "a".into(),
                title: "Fix bug".into(),
                status: "working".into(),
                sub: "working now · just now".into(),
                active: true,
                ..Default::default()
            },
            Session {
                group: "Today".into(),
                id: "b".into(),
                title: "Docs".into(),
                status: "done".into(),
                sub: "finished · 5m ago".into(),
                ..Default::default()
            },
        ];
        assert!(animating(&s));
        let rows = session_sidebar(&s, &Palette::new(false), 27, 1, "", false);
        let text = |i: usize| {
            rows[i]
                .0
                .spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>()
        };
        assert_eq!(text(0), "☰ Sessions");
        assert_eq!(rows[0].1, Some(SidebarHit::New));
        assert_eq!(text(1), "SESSIONS 2");
        let first = rows
            .iter()
            .position(|(_, hit)| *hit == Some(SidebarHit::Session(0)))
            .unwrap();
        assert!(text(first).starts_with("▌⠙ Fix bug"), "{}", text(first));
        assert!(text(first + 1).contains("working now · just now"));
        assert!(text(first + 2).starts_with(" · Docs"));
        let filtered = session_sidebar(&s, &Palette::new(false), 27, 1, "docs", true);
        let titles: Vec<String> = filtered
            .iter()
            .filter(|(_, hit)| matches!(hit, Some(SidebarHit::Session(_))))
            .map(|(l, _)| {
                l.spans
                    .iter()
                    .map(|x| x.content.as_ref())
                    .collect::<String>()
            })
            .collect();
        assert_eq!(titles.len(), 2, "one session card of two lines matches");
        assert!(titles[0].contains("Docs"));
        s.archived_label = "Archived · 3".into();
        let rows = session_sidebar(&s, &Palette::new(false), 27, 1, "", false);
        assert_eq!(rows.last().unwrap().1, Some(SidebarHit::Archived));
    }
    #[test]
    fn tabs_scroll_to_keep_the_current_one_and_map_to_cells() {
        use crate::bridge::Session;
        let tab = |id: &str, active: bool| Session {
            id: id.into(),
            title: format!("session {id} title"),
            active,
            ..Default::default()
        };
        let mut s = Snapshot::default();
        s.tabs = (0..6).map(|i| tab(&i.to_string(), i == 5)).collect();
        let cells = tab_cells(&s, 60);
        assert_eq!(cells.first().unwrap().kind, TabHit::Sessions);
        assert!(
            cells.iter().any(|c| c.kind == TabHit::Tab(5)),
            "the current tab stays visible"
        );
        assert!(
            !cells.iter().any(|c| c.kind == TabHit::Tab(0)),
            "older tabs scroll out"
        );
        assert_eq!(cells[cells.len() - 2].kind, TabHit::New);
        assert!(cells.iter().all(|c| c.end <= 60));
        s.tabs.truncate(1);
        s.tabs[0].active = true;
        let cells = tab_cells(&s, 120);
        assert_eq!(
            (cells[1].start, cells[1].end),
            (4, 4 + 6 + "session 0 title".len())
        );
    }
    #[test]
    fn inline_diff_has_counts_numbers_tints_and_hunk_gaps() {
        let p = Palette::new(false);
        let row = |a: u32, b: &str, c: u32, d: &str, k: &str| {
            (a, b.to_string(), c, d.to_string(), k.to_string())
        };
        let block = crate::bridge::Content {
            id: "d".into(),
            kind: "diff".into(),
            title: "src/a.py".into(),
            added: 1,
            removed: 1,
            diff_rows: vec![
                row(9, "keep", 9, "keep", "ctx"),
                row(10, "old", 10, "new", "change"),
                row(0, "", 0, "", "sep"),
                row(40, "tail", 40, "tail", "ctx"),
            ],
            ..Default::default()
        };
        let rows = crate::transcript::build(&block, 60, &p);
        let text = |i: usize| {
            rows[i]
                .0
                .spans
                .iter()
                .map(|s| s.content.as_ref())
                .collect::<String>()
        };
        assert_eq!(text(0), "   src/a.py (+1, -1)");
        assert!(
            text(1).contains(" 9 keep") && text(1).contains("│ 9 keep"),
            "{}",
            text(1)
        );
        assert!(
            text(2).contains("10 old") && text(2).contains("│10 new"),
            "{}",
            text(2)
        );
        assert!(rows[2]
            .0
            .spans
            .iter()
            .any(|s| s.style.bg == Some(p.diff_del)));
        assert!(rows[2]
            .0
            .spans
            .iter()
            .any(|s| s.style.bg == Some(p.diff_add)));
        assert_eq!(text(3), "   ⋯");
        assert!(text(4).contains("40 tail"));
    }
    #[test]
    fn tool_detail_lines_are_toned_and_wrapped_inside_the_dialog() {
        let p = Palette::new(false);
        let lines = vec![
            "PARAMETERS".to_string(),
            "  path: src/a.py".into(),
            "  +added line that is rather long indeed".into(),
        ];
        let tones = vec!["title".to_string(), "kv".into(), "add".into()];
        let rows = toned_lines(&lines, &tones, 20, &p);
        assert!(rows[0]
            .spans
            .iter()
            .any(|s| s.style.add_modifier.contains(Modifier::BOLD)));
        assert_eq!(
            rows[1]
                .spans
                .iter()
                .map(|s| s.content.as_ref())
                .collect::<String>(),
            "  path: src/a.py"
        );
        assert_eq!(rows[1].spans[1].style.fg, Some(p.quiet), "the label is dim");
        assert!(rows.len() > 3, "the long added line wrapped");
        assert!(rows.iter().all(|r| r
            .spans
            .iter()
            .map(|s| s.content.chars().count())
            .sum::<usize>()
            <= 20));
    }
    #[test]
    fn keyboard_targets_are_runs_of_lines_with_the_same_operation() {
        let op = |id: &str| Some(serde_json::json!({"kind": "tool_page", "id": id}));
        let mut cache = Cache::default();
        cache.operations = vec![
            None,
            op("a"),
            op("a"),
            None,
            op("b"),
            op("c"),
            op("c"),
            None,
        ]
        .into();
        assert_eq!(targets(&cache), vec![(1, 2), (4, 4), (5, 6)]);
        assert!(targets(&Cache::default()).is_empty());
    }
    #[test]
    fn sidebar_width_policy_retains_preferences_and_uses_full_height() {
        for width in [80, 120, 170, 220] {
            for left in [false, true] {
                for right in [false, true] {
                    for last in ["sessions", "details"] {
                        let s = Snapshot {
                            sessions_sidebar: left,
                            details_sidebar: right,
                            last_opened: last.into(),
                            ..Default::default()
                        };
                        let r = regions(Rect::new(0, 0, width, 50), &s, 7, false);
                        if r.sessions.width > 0 {
                            assert_eq!(r.sessions.height, 50);
                            assert_eq!(r.sessions.y, 0);
                            assert_eq!(r.tabs.height, 0);
                        }
                        if r.details.width > 0 {
                            assert_eq!(r.details.height, 50);
                            assert_eq!(r.details.y, 0);
                        }
                        if width >= 170 {
                            assert_eq!(r.sessions.width > 0, left);
                            assert_eq!(r.details.width > 0, right);
                        }
                        if width == 120 && left && right {
                            assert_eq!(r.details.width > 0, last == "details");
                            assert_eq!(r.sessions.width > 0, last == "sessions");
                        }
                        if width == 80 && right {
                            assert_eq!(r.details.right(), 80);
                            assert_eq!(logs_region(&r), r.details);
                        }
                    }
                }
            }
        }
    }
    #[test]
    fn selection_extracts_text_by_columns_and_reverses_the_highlight() {
        let mut cache = Cache::default();
        cache.lines = vec![
            Line::from("hello world"),
            Line::from("  second line  "),
            Line::from("third"),
        ]
        .into();
        cache.selection = Some(((0, 6), (2, 3)));
        assert_eq!(selected_text(&cache), "world\n  second line\nthi");
        cache.selection = Some(((1, 8), (1, 2))); // reversed drag
        assert_eq!(selected_text(&cache), "second");
    }
    #[test]
    fn highlight_splits_spans_at_the_selected_columns() {
        let line = highlight(Line::from("abcdef"), 2, 4);
        let parts: Vec<(String, bool)> = line
            .spans
            .iter()
            .map(|s| {
                (
                    s.content.to_string(),
                    s.style.add_modifier.contains(Modifier::REVERSED),
                )
            })
            .collect();
        assert_eq!(
            parts,
            vec![
                ("ab".to_string(), false),
                ("cd".to_string(), true),
                ("ef".to_string(), false)
            ]
        );
    }
    #[test]
    fn picker_group_headings_do_not_change_item_selection() {
        let mut s = Snapshot::default();
        s.panel_title = "Models".into();
        let item = |label: &str, group: &str| crate::bridge::Item {
            label: label.into(),
            group: group.into(),
            ..Default::default()
        };
        s.items = vec![
            item("a", "Favorites"),
            item("b", "Recent"),
            item("c", "Recent"),
        ];
        let mut terminal = Terminal::new(TestBackend::new(80, 30)).unwrap();
        let mut cache = Cache::default();
        terminal
            .draw(|frame| {
                draw(
                    frame,
                    &s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    2,
                    0,
                    0,
                    &mut cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                    0,
                );
            })
            .unwrap();
        let screen: Vec<String> = (0..30)
            .map(|y| {
                (0..80)
                    .map(|x| terminal.backend().buffer()[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect();
        let at = |needle: &str| {
            screen
                .iter()
                .position(|row| row.contains(needle))
                .unwrap_or_else(|| panic!("{needle} missing"))
        };
        assert!(at("Favorites") < at(" a") && at(" a") < at("Recent") && at("Recent") < at(" b"));
        let bg = |row: usize| terminal.backend().buffer()[(40, row as u16)].bg;
        assert_ne!(
            bg(at(" c")),
            bg(at(" b")),
            "selection 2 is the third item, not the third line"
        );
        assert_eq!(
            screen.iter().filter(|row| row.contains("Recent")).count(),
            1,
            "one heading per group"
        );
    }
    #[test]
    fn settings_area_list_steps_over_headings_and_draws_beside_the_page() {
        use crate::bridge::Nav;
        let nav = Nav {
            items: vec![
                ("GENERAL".into(), "".into(), true),
                ("Appearance".into(), "appearance".into(), false),
                ("Layout".into(), "layout".into(), false),
                ("CONFIGURE".into(), "".into(), true),
                ("Providers".into(), "providers".into(), false),
                ("Speech".into(), "speech".into(), false),
            ],
            selected: 2,
        };
        assert_eq!(
            nav_step(&nav, true),
            Some(4),
            "the CONFIGURE heading is skipped"
        );
        assert_eq!(nav_step(&nav, false), Some(1));
        assert_eq!(
            nav_step(
                &Nav {
                    selected: 1,
                    ..Nav {
                        items: nav.items.clone(),
                        selected: 0
                    }
                },
                false
            ),
            None
        );
        let mut s = Snapshot::default();
        s.panel_title = "Layout".into();
        s.panel_lines = vec!["Panels hide automatically".into()];
        s.nav = Some(nav);
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        let mut cache = Cache::default();
        terminal
            .draw(|frame| {
                draw(
                    frame,
                    &s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    &mut cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                    0,
                );
            })
            .unwrap();
        let row_of = |needle: &str| {
            (0..30u16)
                .find(|y| {
                    (0..100u16)
                        .map(|x| terminal.backend().buffer()[(x, *y)].symbol().to_string())
                        .collect::<String>()
                        .contains(needle)
                })
                .unwrap_or_else(|| panic!("{needle} missing"))
        };
        let col_of = |needle: &str| {
            let y = row_of(needle);
            let line: String = (0..100u16)
                .map(|x| terminal.backend().buffer()[(x, y)].symbol().to_string())
                .collect();
            line.find(needle).unwrap()
        };
        assert!(
            row_of("▸ Layout") > row_of("GENERAL") && row_of("Providers") > row_of("CONFIGURE")
        );
        assert!(
            col_of("Panels hide") > col_of("Providers"),
            "the page sits to the right of the list"
        );
    }
    fn rich_snapshot(light: bool) -> Snapshot {
        serde_json::from_value(serde_json::json!({
            "schema": 1, "revision": 1, "title": "Nexus · s1", "status": "running",
            "theme": if light { "nexus-light" } else { "nexus-dark" },
            "sessions_sidebar": true, "details_sidebar": true,
            "breadcrumb": "/work/project › main", "agent": "build", "model": "gpt-6", "provider": "openai", "effort": "high",
            "context_usage": "12K (4%)",
            "tabs": [{"id": "s1", "title": "Fix the parser", "workspace": "/w", "status": "working", "active": true},
                     {"id": "s2", "title": "Docs", "workspace": "/w", "status": "done"}],
            "sessions": [{"group": "w · Today", "id": "s1", "title": "Fix the parser", "workspace": "/w", "state": "running", "status": "working", "sub": "working now · just now", "active": true}],
            "blocks": [
                {"id": "u", "kind": "user", "title": "please fix it", "number": 1, "operation": {"kind": "x"}},
                {"id": "t", "kind": "tool", "text": "⠋ Edit src/parser.py", "status": "running"},
                {"id": "d", "kind": "diff", "title": "src/parser.py", "added": 1, "removed": 1,
                 "diff_rows": [[3, "old", 3, "new", "change"]]},
                {"id": "m", "kind": "markdown", "text": "# Done\n\nUse `parse()` here.\n\n- one\n- two"}],
            "details_panel": {"session": [["Status", "running"]], "files": [{"path": "src/parser.py", "added": 1, "removed": 1}], "files_summary": "+1 -1 across 1 file"},
            "prompt": {"kind": "permission", "id": "p", "lines": ["Tool: Edit"], "choices": [{"label": "Allow once", "value": "allow_once", "key": "y", "disabled": false}]}
        })).unwrap()
    }
    #[test]
    fn rich_screen_draws_at_narrow_medium_and_wide_sizes_in_both_themes() {
        for (width, height) in [(60u16, 24u16), (120, 40), (200, 50)] {
            for light in [false, true] {
                let s = rich_snapshot(light);
                let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
                let mut cache = Cache::default();
                terminal
                    .draw(|frame| {
                        draw(
                            frame,
                            &s,
                            &Editor::default(),
                            &Editor::default(),
                            &Editor::default(),
                            "",
                            0,
                            0,
                            0,
                            &mut cache,
                            true,
                            false,
                            false,
                            false,
                            0,
                            0,
                            0,
                        );
                    })
                    .unwrap();
                let screen: String = (0..height)
                    .map(|y| {
                        (0..width)
                            .map(|x| terminal.backend().buffer()[(x, y)].symbol().to_string())
                            .collect::<String>()
                            + "\n"
                    })
                    .collect();
                let at = format!("{width}x{height} light={light}");
                assert!(
                    screen.contains("Permission requested"),
                    "{at}: the prompt is always reachable"
                );
                assert!(screen.contains("Allow once"), "{at}");
                assert!(screen.contains("Build"), "{at}: the runtime row");
                if width >= 170 {
                    assert!(screen.contains("Fix the parser"), "{at}: current session");
                }
                if width >= 170 {
                    assert!(screen.contains("SESSIONS 1"), "{at}: sessions sidebar");
                }
                if width >= 170 {
                    assert!(
                        screen.contains("CHANGES · 1 file") && screen.contains("src/parser.py"),
                        "{at}: details sidebar"
                    );
                }
            }
        }
    }
    #[test]
    fn voice_preview_is_inline_with_only_a_recording_square() {
        let mut s = Snapshot::default();
        s.voice_phase = "recording".into();
        s.voice_preview = "hello wor".into();
        let mut cache = Cache::default();
        cache.voice_levels = vec![0.0, 0.5, 1.0];
        cache.voice_elapsed = 65;
        cache.voice_text = s.voice_preview.clone();
        cache.spin = 3;
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        terminal
            .draw(|frame| {
                draw(
                    frame,
                    &s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    &mut cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                    0,
                );
            })
            .unwrap();
        let screen: String = (0..24u16)
            .map(|y| {
                (0..100u16)
                    .map(|x| terminal.backend().buffer()[(x, y)].symbol().to_string())
                    .collect::<String>()
                    + "\n"
            })
            .collect();
        assert!(
            screen.contains("hello wor") && screen.contains("■"),
            "{screen}"
        );
        assert!(!screen.contains("Recording") && !screen.contains("Esc cancel"));
    }
    #[test]
    fn running_slot_is_replaced_per_frame() {
        let line = Line::from(vec![Span::raw(format!("{SPINNER_SLOT} Bash · ls"))]);
        assert_eq!(with_spinner(&line, 0).spans[0].content, "⠋ Bash · ls");
        assert_eq!(with_spinner(&line, 11).spans[0].content, "⠙ Bash · ls");
        let s = Snapshot::default();
        assert!(!animating(&s));
    }
    #[test]
    fn composer_grows_with_content_and_is_capped() {
        let area = Rect::new(0, 0, 80, 60);
        let mut draft = Editor::default();
        assert_eq!(composer_height(area, &draft, &Snapshot::default()), 8);
        draft.insert(&"line\n".repeat(9));
        assert_eq!(composer_height(area, &draft, &Snapshot::default()), 6 + 10);
        draft.insert(&"line\n".repeat(60));
        assert_eq!(composer_height(area, &draft, &Snapshot::default()), 6 + 22);
        assert_eq!(
            composer_height(Rect::new(0, 0, 80, 14), &draft, &Snapshot::default()),
            8,
            "the transcript keeps its rows with a single-row top bar"
        );
        let mut queued = Snapshot::default();
        queued.queue_lines = vec!["Queued · a".into(), "Steering · b".into()];
        assert_eq!(
            composer_height(Rect::new(0, 0, 80, 60), &Editor::default(), &queued),
            10,
            "queued messages get rows above the editor"
        );
    }
    #[test]
    fn workspace_bar_is_between_composer_controls_and_activity() {
        for width in [60, 120, 180] {
            for sessions_sidebar in [false, true] {
                let s = Snapshot {
                    breadcrumb: "/workspace · main · worktree".into(),
                    agent: "Agent".into(),
                    sessions_sidebar,
                    ..Default::default()
                };
                let area = Rect::new(0, 0, width, 40);
                let r = regions(
                    area,
                    &s,
                    composer_height(area, &Editor::default(), &s),
                    false,
                );
                let rows = composer_rows(r.composer, &s);
                assert_eq!(r.workspace, rows[4]);
                assert_eq!(rows[3].bottom(), r.workspace.y);
                assert_eq!(r.workspace.bottom(), rows[5].y);
                let mut terminal = Terminal::new(TestBackend::new(width, 40)).unwrap();
                let mut cache = Cache::default();
                terminal
                    .draw(|frame| {
                        draw(
                            frame,
                            &s,
                            &Editor::default(),
                            &Editor::default(),
                            &Editor::default(),
                            "",
                            0,
                            0,
                            0,
                            &mut cache,
                            true,
                            false,
                            false,
                            false,
                            0,
                            0,
                            0,
                        );
                    })
                    .unwrap();
                let row: String = (r.workspace.x..r.workspace.right())
                    .map(|x| terminal.backend().buffer()[(x, r.workspace.y)].symbol())
                    .collect();
                assert!(row.contains("/workspace") && !row.contains("idle"), "{row}");
                assert!(!row.contains('▐'), "no duplicate details toggle: {row}");
                if r.tabs.height > 0 {
                    assert_eq!(r.tabs.height, 1, "top bar has no separator row");
                    let top: String = (r.tabs.x..r.tabs.right())
                        .map(|x| terminal.backend().buffer()[(x, r.tabs.y)].symbol())
                        .collect();
                    assert!(top.contains('▐'), "top-bar toggle remains: {top}");
                }
                let buffer = terminal.backend().buffer();
                assert_eq!(buffer[(r.workspace.x + 5, r.workspace.y)].symbol(), "/");
                assert_eq!(buffer[(r.workspace.x + 5, rows[2].y)].symbol(), "A");
                for x in r.workspace.x..r.workspace.right() {
                    assert_eq!(
                        buffer[(x, r.workspace.y)].bg,
                        Palette::new(false).background
                    );
                }
            }
        }
    }
    #[test]
    fn narrow_layout_and_safe_wrap() {
        let mut terminal = Terminal::new(TestBackend::new(60, 20)).unwrap();
        let s = Snapshot {
            schema: 1,
            title: "Nexus".into(),
            lines: vec!["hello world".into()],
            ..Default::default()
        };
        let mut cache = Cache::default();
        terminal
            .draw(|frame| {
                draw(
                    frame,
                    &s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    &mut cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                    0,
                );
            })
            .unwrap();
        assert!(terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|c| c.symbol() == "h"));
    }
}

fn editor_view(
    editor: &Editor,
    secret: bool,
    palette: &Palette,
    width: u16,
    height: u16,
) -> Vec<Line<'static>> {
    let layout = editor.visual_layout(usize::from(width.max(1)));
    let (mut cursor_row, cursor_column) = layout.cursor_position(editor.cursor);
    let selection = editor.selection();
    let cursor_style = Style::default().bg(palette.text).fg(palette.background);
    let mut rows = Vec::with_capacity(layout.rows.len());
    for (row, range) in layout.rows.iter().enumerate() {
        let mut line = Line::default();
        for (offset, grapheme) in layout.row_text(row).grapheme_indices(true) {
            let at = range.start + offset;
            let style = if at == editor.cursor {
                cursor_style
            } else if selection.is_some_and(|(start, end)| at >= start && at < end) {
                Style::default().bg(palette.accent).fg(palette.background)
            } else {
                Style::default().fg(palette.text)
            };
            let text = if secret {
                "•".to_string()
            } else if grapheme.chars().any(char::is_control) {
                grapheme.chars().flat_map(char::escape_default).collect()
            } else {
                grapheme.to_string()
            };
            line.spans.push(Span::styled(text, style));
        }
        rows.push(line);
    }
    // Newline and end-of-input cursors need a spare cell. On a full row,
    // expose that cell on a trailing row without rewrapping the source text.
    if editor.cursor == layout.rows[cursor_row].end {
        if cursor_column >= usize::from(width.max(1)) {
            rows.insert(cursor_row + 1, Line::default());
            cursor_row += 1;
        }
        rows[cursor_row].spans.push(Span::styled(" ", cursor_style));
    }
    let offset = cursor_row.saturating_sub(height.saturating_sub(1) as usize);
    rows.into_iter()
        .skip(offset)
        .take(height as usize)
        .collect()
}

#[cfg(test)]
mod editor_layout_tests {
    use super::*;
    #[test]
    fn word_wrapped_rows_preserve_source_selection_and_cursor() {
        let mut editor = Editor::default();
        editor.insert("one two three");
        editor.cursor = 8;
        editor.anchor = Some(4);
        let palette = Palette::new(false);
        let rows = editor_view(&editor, false, &palette, 10, u16::MAX);
        assert_eq!(rows[0].to_string(), "one two ");
        assert_eq!(rows[1].to_string(), "three");
        assert!(rows[0].spans[4..]
            .iter()
            .all(|span| span.style.bg == Some(palette.accent)));
        assert_eq!(rows[1].spans[0].style.bg, Some(palette.text));
        editor.vertical_wrapped(false, 10);
        assert_eq!(editor.cursor, 0);
        editor.vertical_wrapped(true, 10);
        assert_eq!(editor.cursor, 8);
    }

    #[test]
    fn wrapped_attachment_spans_and_hard_break_cursor_keep_source_offsets() {
        let mut editor = Editor::default();
        editor.insert("send [image #1]\nnext");
        editor.cursor = 15;
        editor.anchor = Some(5);
        let palette = Palette::new(false);
        let rows = editor_view(&editor, false, &palette, 11, u16::MAX);
        assert_eq!(rows[0].to_string(), "send [image");
        assert_eq!(rows[1].to_string(), " #1] ");
        assert_eq!(rows[2].to_string(), "next");
        assert!(rows[0].spans[5..]
            .iter()
            .all(|span| span.style.bg == Some(palette.accent)));
        assert!(rows[1].spans[..4]
            .iter()
            .all(|span| span.style.bg == Some(palette.accent)));
        assert_eq!(rows[1].spans[4].style.bg, Some(palette.text));
        editor.backspace();
        assert_eq!(editor.text, "send \nnext");
    }

    #[test]
    fn composer_height_counts_word_wrapped_source_rows() {
        let s = Snapshot::default();
        let mut editor = Editor::default();
        editor.insert("aaaa bbbb cccc dddd");
        let area = Rect::new(0, 0, 17, 40);
        assert_eq!(editor.visual_layout(8).rows.len(), 4);
        assert_eq!(composer_height(area, &editor, &s), 10);
    }

    #[test]
    fn composer_cursor_stays_visible_before_typing_and_after_clear() {
        use ratatui::{backend::TestBackend, Terminal};
        for light in [false, true] {
            let s = Snapshot {
                theme: if light { "nexus-light" } else { "nexus-dark" }.into(),
                ..Default::default()
            };
            let p = Palette::new(light);
            let mut editor = Editor::default();
            let mut cache = Cache::default();
            let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
            for text in ["", "hello", ""] {
                editor.take();
                editor.insert(text);
                let mut composer = Rect::default();
                terminal
                    .draw(|frame| {
                        composer = draw(
                            frame,
                            &s,
                            &editor,
                            &Editor::default(),
                            &Editor::default(),
                            "",
                            0,
                            0,
                            0,
                            &mut cache,
                            false,
                            false,
                            false,
                            false,
                            0,
                            0,
                            0,
                        )
                        .composer;
                    })
                    .unwrap();
                let rows = composer_rows(composer, &s);
                let cursor = &terminal.backend().buffer()
                    [(composer.x + 5 + text.len() as u16, rows[1].y + 1)];
                assert_eq!(cursor.symbol(), if text.is_empty() { "T" } else { " " });
                assert_eq!(cursor.bg, p.text);
                assert_eq!(cursor.fg, p.background);
            }
        }
    }

    #[test]
    fn long_line_keeps_cursor_visible_and_graphemes_whole() {
        let mut editor = Editor::default();
        editor.insert(&"🧑🏽‍💻a".repeat(80));
        let rows = editor_view(&editor, false, &Palette::new(false), 12, 3);
        assert!(rows.len() <= 3);
        assert!(rows.iter().all(|row| row.width() <= 12));
        assert!(rows
            .iter()
            .flat_map(|row| row.spans.iter())
            .any(|span| span.style.bg == Some(Palette::new(false).text)));
    }
}

#[cfg(test)]
mod history_benchmark {
    use super::*;
    #[test]
    #[ignore = "manual projection/cache microbenchmark; reports timings without a flaky threshold"]
    fn thousand_turns() {
        let mut blocks: Vec<crate::bridge::Content> = (0..1000)
            .map(|i| crate::bridge::Content {
                id: i.to_string(),
                kind: "markdown".into(),
                title: format!("Turn {i}"),
                text: "Result with **formatted** context.\n".repeat(20),
                ..Default::default()
            })
            .collect();
        let mut cache = Cache::default();
        let palette = Palette::new(false);
        let start = std::time::Instant::now();
        cache.update_content(&[], &blocks, 100, &palette);
        let cold = start.elapsed();
        blocks.last_mut().unwrap().text.push_str("stream delta");
        let start = std::time::Instant::now();
        cache.update_content(&[], &blocks, 100, &palette);
        println!(
            "1000-turn cache: cold={cold:?}, one changed={:?}, rows={}",
            start.elapsed(),
            cache.lines.len()
        );
        assert_eq!(cache.operations.len(), cache.lines.len());
        assert!(cache.lines.iter().any(|row| row
            .spans
            .iter()
            .any(|span| span.content.contains("stream delta"))));
    }
}

pub fn composer_rows(area: Rect, s: &Snapshot) -> Vec<Rect> {
    Layout::vertical([
        Constraint::Length(1 + queue_rows(s)),
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .split(area)
    .to_vec()
}

struct ControlLayout {
    left: Vec<(String, &'static str)>,
    text: String,
    bars: bool,
    gap: usize,
    context_start: usize,
}
fn control_layout(s: &Snapshot, width: usize) -> ControlLayout {
    let mut provider = !s.provider.is_empty();
    let mut effort = !s.effort.is_empty();
    let mut numbers: Vec<&str> = s
        .context_label
        .split(" · ")
        .filter(|text| !text.is_empty())
        .collect();
    if numbers.is_empty() {
        numbers.push("unavailable");
    }
    let mut bars = false;
    loop {
        let mut agent = s.agent.clone();
        if let Some(first) = agent.get_mut(0..1) {
            first.make_ascii_uppercase();
        }
        let mut left = vec![(agent, "/agent"), (s.model.clone(), "/model")];
        if provider {
            left[1].0 += &format!(" {}", s.provider);
        }
        if effort {
            left.push((s.effort.clone(), "/effort"));
        }
        let mut text = numbers.join(" · ");
        if bars && s.context_window.unwrap_or(0) == 0 {
            text += " ?";
        }
        let left_width = left.iter().map(|(text, _)| text.width()).sum::<usize>()
            + left.len().saturating_sub(1) * 3;
        let right_width = text.width() + if bars { 12 } else { 0 };
        if left_width + right_width + 2 <= width
            || (!provider && !effort && numbers.len() <= 1 && !bars)
        {
            let available = width.saturating_sub(right_width + 2);
            if left_width > available {
                let name_width = left[0].0.width().min(available);
                left[0].0 = crate::transcript::truncate(&left[0].0, name_width);
                left[1].0 = crate::transcript::truncate(
                    &left[1].0,
                    available.saturating_sub(name_width + 3),
                );
                if left[1].0.is_empty() {
                    left.pop();
                }
            }
            let used = left.iter().map(|(text, _)| text.width()).sum::<usize>()
                + left.len().saturating_sub(1) * 3;
            if right_width > width {
                text = crate::transcript::truncate(&text, width);
            }
            let context_start = width.saturating_sub(text.width() + if bars { 12 } else { 0 });
            return ControlLayout {
                left,
                text,
                bars,
                gap: context_start.saturating_sub(used),
                context_start,
            };
        }
        if provider {
            provider = false;
        } else if effort {
            effort = false;
        } else if numbers.len() > 2 {
            numbers.pop();
        } else if bars {
            bars = false;
        } else if numbers.len() > 1 {
            numbers.pop();
        }
    }
}
pub fn composer_context_at(area: Rect, s: &Snapshot, x: u16, y: u16) -> bool {
    if !s.agent_page.is_empty() || y != composer_rows(area, s)[2].y {
        return false;
    }
    let layout = control_layout(s, area.width.saturating_sub(9) as usize);
    x >= area.x + 5 + layout.context_start as u16 && x < area.right().saturating_sub(4)
}
pub fn composer_control_at(area: Rect, s: &Snapshot, x: u16, y: u16) -> Option<&'static str> {
    if !s.agent_page.is_empty() || y != composer_rows(area, s)[2].y {
        return None;
    }
    let layout = control_layout(s, area.width.saturating_sub(9) as usize);
    let mut start = area.x + 5;
    for (text, command) in layout.left {
        let end = start.saturating_add(text.width() as u16);
        if x >= start && x < end {
            return Some(command);
        }
        start = end + 3;
    }
    None
}

#[cfg(test)]
mod composer_control_tests {
    use super::*;
    #[test]
    fn controls_follow_runtime_row_and_queue_height() {
        let mut s = Snapshot::default();
        s.agent = "root".into();
        s.model = "model".into();
        s.provider = "vendor".into();
        s.effort = "high".into();
        let area = Rect::new(0, 15, 100, 9);
        for queued in [false, true] {
            if queued {
                s.queue_lines.push("queued".into());
            }
            let y = composer_rows(area, &s)[2].y;
            for (x, command) in [
                (5, "/agent"),
                (12, "/model"),
                (18, "/model"),
                (27, "/effort"),
            ] {
                assert_eq!(composer_control_at(area, &s, x, y), Some(command));
                assert_eq!(composer_control_at(area, &s, x, y + 1), None);
            }
        }
    }
}

/// Briefly resolve changed ASCII letters; stable words and Unicode stay readable.
fn animated_voice_preview(cache: &mut Cache, text: &str) -> String {
    if cache.voice_text != text {
        let common = cache
            .voice_text
            .chars()
            .zip(text.chars())
            .take_while(|(a, b)| a == b)
            .count();
        cache.voice_stable_prefix = text
            .chars()
            .take(common)
            .enumerate()
            .filter(|(_, c)| c.is_whitespace())
            .map(|(i, _)| i + 1)
            .last()
            .unwrap_or(0);
        cache.voice_text = text.to_string();
        cache.voice_changed_at = cache.spin;
    }
    let age = cache.spin.saturating_sub(cache.voice_changed_at);
    text.chars()
        .enumerate()
        .map(|(i, c)| {
            if age < 3
                && i >= cache.voice_stable_prefix
                && c.is_ascii_alphabetic()
                && (i + age) % 3 != age
            {
                (b'a' + ((i * 17 + cache.spin * 11) % 26) as u8) as char
            } else {
                c
            }
        })
        .collect()
}

#[cfg(test)]
mod voice_animation_tests {
    use super::*;
    #[test]
    fn changed_words_resolve_and_preserve_stable_text() {
        let mut cache = Cache::default();
        cache.voice_text = "hello word".into();
        cache.spin = 10;
        let first = animated_voice_preview(&mut cache, "hello world");
        assert!(first.starts_with("hello "));
        assert_ne!(first, "hello world");
        cache.spin = 13;
        assert_eq!(
            animated_voice_preview(&mut cache, "hello world"),
            "hello world"
        );
        assert_eq!(animated_voice_preview(&mut cache, "你好 🌍"), "你好 🌍");
    }
}

#[cfg(test)]
mod redesign_regressions {
    use super::*;
    use crate::bridge::Content;
    use ratatui::{backend::TestBackend, Terminal};
    #[test]
    fn revision_cache_reuses_history_and_only_rewraps_changed_tail() {
        let p = Palette::new(false);
        let mut cache = Cache::default();
        let mut blocks = vec![
            Content {
                id: "old".into(),
                rev: "1".into(),
                kind: "markdown".into(),
                text: "history".into(),
                ..Default::default()
            },
            Content {
                id: "tail".into(),
                rev: "1".into(),
                kind: "markdown".into(),
                text: "stream".into(),
                ..Default::default()
            },
        ];
        cache.update_content(&[], &blocks, 100, &p);
        let history = cache.parts["|old"].clone();
        blocks[1].text.push_str("ing");
        blocks[1].rev = "2".into();
        cache.update_content(&[], &blocks, 100, &p);
        assert!(std::sync::Arc::ptr_eq(&history, &cache.parts["|old"]));
        assert!(cache.lines.range(0, 100).any(|line| line
            .spans
            .iter()
            .any(|span| span.content.contains("streaming"))));
        assert_eq!(cache.lines.len(), cache.operations.len());
        let text = cache
            .lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .map(|span| span.content.as_ref())
            .collect::<String>();
        cache.update_content(&[], &blocks, 40, &p);
        assert!(!std::sync::Arc::ptr_eq(&history, &cache.parts["|old"]));
        assert!(text.contains("history"));
    }
    #[test]
    fn context_cluster_and_controls_never_overlap_and_share_hit_targets() {
        let s = Snapshot {
            agent: "build".into(),
            model: "opus-5.5".into(),
            provider: "anthropic".into(),
            effort: "high".into(),
            context_label: "52K · 26% · 5.2%".into(),
            context_used: Some(52000),
            context_window: Some(1000000),
            ..Default::default()
        };
        for width in [30, 50, 80, 120, 200] {
            let area = Rect::new(7, 3, width, 7);
            let row = composer_rows(area, &s)[2].y;
            let layout = control_layout(&s, width.saturating_sub(9) as usize);
            assert!(
                chrome::control_spans(
                    &s,
                    &Palette::new(false),
                    width.saturating_sub(9) as usize,
                    &components::Hover::default(),
                    std::time::Instant::now()
                )
                .iter()
                .map(|span| span.content.width())
                .sum::<usize>()
                    <= width.saturating_sub(9) as usize
            );
            for x in area.x..area.right() {
                assert!(
                    !(composer_control_at(area, &s, x, row).is_some()
                        && composer_context_at(area, &s, x, row))
                );
            }
            assert!(layout.text.contains("52K"));
        }
    }
    #[test]
    #[ignore = "manual 2,000-turn streaming/scroll frame benchmark"]
    fn streaming_2000_turns_300_scroll_events() {
        let mut s = Snapshot {
            schema: 2,
            status: "running".into(),
            agent: "build".into(),
            model: "test".into(),
            ..Default::default()
        };
        for i in 0..2000 {
            for (suffix, kind, text) in [
                ("user", "user", "Inspect the parser"),
                (
                    "reply",
                    "markdown",
                    "I will inspect the parser and preserve all diagnostics.",
                ),
                ("tool", "tool_group", "Read nexus/parse.py · 412 lines"),
            ] {
                s.blocks.push(Content {
                    id: format!("{i}:{suffix}"),
                    rev: "1".into(),
                    kind: kind.into(),
                    title: if kind == "user" {
                        text.into()
                    } else {
                        String::new()
                    },
                    text: text.into(),
                    gap: 1,
                    count: 7,
                    status: "completed".into(),
                    ..Default::default()
                });
            }
        }
        let mut terminal = Terminal::new(TestBackend::new(200, 50)).unwrap();
        let mut cache = Cache::default();
        let mut samples = Vec::new();
        let draft = Editor::default();
        let mut scroll = 0;
        for event in 0..301 {
            let start = std::time::Instant::now();
            // 300 wheel events/s with 50 streaming snapshots/s: a new tail every sixth event.
            if event % 6 == 0 {
                let tail = s.blocks.last_mut().unwrap();
                tail.rev = event.to_string();
                tail.text = format!("Read nexus/parse.py · {} lines", event);
                s.revision += 1;
            }
            scroll = (scroll + 3) % 8000;
            terminal
                .draw(|frame| {
                    draw(
                        frame, &s, &draft, &draft, &draft, "", 0, scroll, 0, &mut cache, false,
                        false, false, false, 0, 0, 0,
                    );
                })
                .unwrap();
            if event > 0 {
                samples.push(start.elapsed().as_secs_f64() * 1000.0);
            }
        }
        samples.sort_by(f64::total_cmp);
        eprintln!("2000 turns, 300 wheel events, simulated 50 snapshots/s: p50 {:.3} p95 {:.3} max {:.3} ms (TestBackend, no real terminal flush)",samples[150],samples[285],samples[299]);
    }
}

#[cfg(test)]
mod local_cache_regressions {
    use super::*;
    use crate::bridge::Content;
    use serde_json::json;
    #[test]
    fn local_click_only_projects_affected_group_and_reuses_suffix() {
        let mut cache = Cache::default();
        let op = json!({"kind":"block_toggle","id":"g"});
        let blocks = vec![
            Content {
                id: "before".into(),
                kind: "markdown".into(),
                text: "before".into(),
                rev: "1".into(),
                ..Default::default()
            },
            Content {
                id: "g".into(),
                kind: "tool_group".into(),
                local_ui: true,
                operation: Some(op.clone()),
                rev: "1".into(),
                members: vec![Content {
                    id: "member".into(),
                    text: "tool".into(),
                    ..Default::default()
                }],
                ..Default::default()
            },
            Content {
                id: "after".into(),
                kind: "markdown".into(),
                text: "after".into(),
                rev: "1".into(),
                ..Default::default()
            },
        ];
        let p = Palette::new(false);
        cache.note_patch(Some(0));
        cache.update_content(&[], &blocks, 100, &p);
        let before = cache.active_parts[0].clone().unwrap();
        let after = cache.active_parts[2].clone().unwrap();
        assert!(cache.disclosure.toggle(&op, &blocks));
        cache.invalidate_disclosure();
        cache.update_content(&[], &blocks, 100, &p);
        assert_eq!(cache.content_blocks, 1);
        assert!(std::sync::Arc::ptr_eq(
            &before,
            cache.active_parts[0].as_ref().unwrap()
        ));
        assert!(std::sync::Arc::ptr_eq(
            &after,
            cache.active_parts[2].as_ref().unwrap()
        ));
        cache.typing = true;
        cache.update_content(&[], &blocks, 100, &p);
        assert_eq!(cache.content_blocks, 0);
    }

    #[test]
    fn a_third_turn_refolds_the_oldest_without_a_patch_over_it() {
        let user = |turn: &str| Content {
            id: format!("{turn}:user"),
            kind: "user".into(),
            title: format!("prompt {turn}"),
            local_ui: true,
            turn_id: turn.into(),
            rev: "1".into(),
            operation: Some(json!({"kind":"turn_toggle","id":turn})),
            ..Default::default()
        };
        let reply = |turn: &str| Content {
            id: format!("{turn}:reply"),
            kind: "markdown".into(),
            text: format!("reply {turn}"),
            turn_id: turn.into(),
            rev: "1".into(),
            ..Default::default()
        };
        let p = Palette::new(false);
        let mut cache = Cache::default();
        let mut blocks = vec![user("t1"), reply("t1"), user("t2"), reply("t2")];
        cache.note_patch(Some(0));
        cache.update_content(&[], &blocks, 100, &p);
        assert!(cache.active_parts.iter().all(|part| part.is_some()));
        let t2_user = cache.active_parts[2].clone().unwrap();
        // The host patch only covers the new turn (from block 4).
        blocks.extend([user("t3"), reply("t3")]);
        cache.note_patch(Some(4));
        cache.update_content(&[], &blocks, 100, &p);
        assert!(cache.active_parts[1].is_none(), "t1 reply must fold away");
        assert!(cache.active_parts[3].is_some() && cache.active_parts[5].is_some());
        // t2 stays open and, being outside the rebuilt range's changes, keeps its rows.
        assert_eq!(cache.active_parts[2].as_ref().unwrap().lines, t2_user.lines);
    }
}

#[cfg(test)]
mod selection_highlight_tests {
    use super::*;
    #[test]
    fn selected_text_gets_the_accent_background() {
        let p = Palette::new(false);
        let mut editor = Editor::default();
        editor.insert("one two");
        editor.anchor = Some(4);
        let lines = editor_text(&editor, false, &p);
        let selected: String = lines[0]
            .spans
            .iter()
            .filter(|span| span.style.bg == Some(p.accent))
            .map(|span| span.content.to_string())
            .collect();
        assert_eq!(selected, "two");
    }
}
