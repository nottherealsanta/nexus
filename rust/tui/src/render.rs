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
pub mod appearance;
mod chrome;
pub mod components;
mod context;
mod dialogs;
// Reused by later modals (Settings): see `dialogs::dim_backdrop`.
#[allow(unused_imports)]
pub use dialogs::{dim_area, dim_backdrop, BACKDROP_BLEND};
pub mod notifications;
mod sessions;
pub mod toasts;
pub use chrome::*;
pub use dialogs::*;
#[derive(Clone)]
pub struct Palette {
    pub background: Color,
    pub text: Color,
    pub muted: Color,
    pub quiet: Color,
    pub accent: Color,
    pub panel: Color,
    /// The composer card's background (`#1C1C1C` in the dark theme).
    pub composer: Color,
    pub element: Color,
    pub element_hi: Color,
    pub dialog: Color,
    pub border: Color,
    /// The top bar's closing rule: about 40% of the way from the background to `border`, so it
    /// sits closer to the background than the line under the tabs.
    pub rule_faint: Color,
    pub border_strong: Color,
    pub diff_del: Color,
    pub diff_add: Color,
    pub blue: Color,
    pub purple: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
    /// Darker red for the failed-call count on a folded tool group (legible on both themes).
    pub error_deep: Color,
    pub cyan: Color,
}
impl Palette {
    pub fn new(light: bool) -> Self {
        let rgb = |r, g, b| Color::Rgb(r, g, b);
        if light {
            // Grok Day (reference `theme/grokday.rs`), Nexus accent kept.
            Self {
                background: rgb(245, 245, 245),
                text: rgb(38, 38, 38),
                muted: rgb(118, 118, 118),
                quiet: rgb(142, 142, 142),
                accent: rgb(200, 103, 47),
                panel: rgb(245, 245, 245),
                composer: rgb(238, 238, 238),
                element: rgb(228, 228, 228),
                element_hi: rgb(218, 218, 218),
                dialog: rgb(245, 245, 245),
                border: rgb(200, 200, 205),
                rule_faint: rgb(227, 227, 230),
                border_strong: rgb(165, 165, 175),
                diff_del: rgb(245, 218, 222),
                diff_add: rgb(218, 242, 220),
                blue: rgb(47, 100, 210),
                purple: rgb(108, 62, 178),
                success: rgb(55, 142, 35),
                warning: rgb(162, 118, 18),
                error: rgb(205, 48, 72),
                error_deep: rgb(160, 28, 48),
                cyan: rgb(0, 130, 170),
            }
        } else {
            // Grok Night (reference `theme/groknight.rs`), Nexus accent kept.
            Self {
                background: rgb(20, 20, 20),
                text: rgb(225, 225, 225),
                muted: rgb(120, 120, 120),
                quiet: rgb(88, 88, 88),
                accent: rgb(250, 178, 131),
                panel: rgb(20, 20, 20),
                composer: rgb(28, 28, 28),
                element: rgb(36, 36, 36),
                element_hi: rgb(44, 44, 44),
                dialog: rgb(20, 20, 20),
                border: rgb(50, 50, 55),
                rule_faint: rgb(32, 32, 35),
                border_strong: rgb(80, 80, 88),
                diff_del: rgb(66, 14, 20),
                diff_add: rgb(6, 56, 6),
                blue: rgb(122, 162, 247),
                purple: rgb(187, 154, 247),
                success: rgb(158, 206, 106),
                warning: rgb(224, 175, 104),
                error: rgb(247, 118, 142),
                error_deep: rgb(196, 66, 88),
                cyan: rgb(125, 207, 255),
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
impl<T> Clone for Indexed<T> {
    fn clone(&self) -> Self {
        Self {
            parts: self.parts.clone(),
            offsets: self.offsets.clone(),
            length: self.length,
        }
    }
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
/// Keyboard focus on a waiting message (by id, so it follows a reorder or promotion) and the
/// control chip the mouse is holding down (`(id, op)`; drawn `p.muted`, `✕` in `p.error`).
#[derive(Default)]
pub struct QueueUi {
    pub focus: Option<String>,
    pub press: Option<(String, &'static str)>,
    /// Monochrome drops every colour, so a pressed chip is also reversed (set each frame).
    pub mono: bool,
}
/// `(up, down, remove)` marks of the queue controls; ASCII twins under `NEXUS_ASCII=1`.
fn queue_marks(ascii: bool) -> (&'static str, &'static str, &'static str) {
    if ascii {
        ("^", "v", "x")
    } else {
        ("↑", "↓", "✕")
    }
}
impl QueueUi {
    /// Drops a focus or press whose message is no longer listed (ran, was removed, collapsed).
    pub fn prune(&mut self, listed: &[String]) {
        if self.focus.as_ref().is_some_and(|id| !listed.contains(id)) {
            self.focus = None;
        }
        if self
            .press
            .as_ref()
            .is_some_and(|(id, _)| !listed.contains(id))
        {
            self.press = None;
        }
    }
}
/// What a key does while a waiting message has keyboard focus.
#[derive(Debug, PartialEq, Eq)]
pub enum QueueKey {
    /// Move focus to this message.
    Focus(String),
    /// Send `queue_edit`; afterwards focus `then` (`None` leaves the list).
    Op(String, &'static str, Option<String>),
    /// Up from the first row: hand focus to the transcript blocks (or leave).
    Prev,
    /// Consumed and leaves the list.
    Leave,
    /// Consumed, nothing to do (a disabled move, an interrupt that is already pending).
    Stay,
    /// Not a queue key: drop the focus and let the key do its normal job.
    Pass,
}
/// Queue indices of the rows drawn above the composer, top to bottom.
pub fn queue_listed(composer: Rect, s: &Snapshot) -> Vec<usize> {
    if !s.agent_page.is_empty() {
        return Vec::new();
    }
    above_layout(composer, s)
        .items
        .into_iter()
        .map(|(i, _)| i)
        .collect()
}
/// Keys on the focused row (`listed` from [`queue_listed`]): Up/Down/Tab/j/k move; Enter or `s`
/// promotes (queued -> steer, steer -> interrupt); Delete, Backspace or `x` removes; Alt+Up/Down
/// reorders queued rows only; Esc leaves.
pub fn queue_key(
    s: &Snapshot,
    listed: &[usize],
    focus: &str,
    code: crossterm::event::KeyCode,
    mods: crossterm::event::KeyModifiers,
) -> QueueKey {
    use crossterm::event::{KeyCode, KeyModifiers};
    let Some(at) = listed.iter().position(|i| s.queue_items[*i].id == focus) else {
        return QueueKey::Pass;
    };
    let item = &s.queue_items[listed[at]];
    let id_at = |pos: usize| s.queue_items[listed[pos]].id.clone();
    let alt = mods.contains(KeyModifiers::ALT);
    match code {
        KeyCode::Up | KeyCode::Down if alt => {
            let (op, ok) = if code == KeyCode::Up {
                ("up", item.can_up)
            } else {
                ("down", item.can_down)
            };
            if !is_steering(item) && ok {
                QueueKey::Op(item.id.clone(), op, Some(item.id.clone()))
            } else {
                QueueKey::Stay
            }
        }
        _ if mods.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => QueueKey::Pass,
        KeyCode::Up | KeyCode::BackTab | KeyCode::Char('k') => {
            if at == 0 {
                QueueKey::Prev
            } else {
                QueueKey::Focus(id_at(at - 1))
            }
        }
        KeyCode::Down | KeyCode::Tab | KeyCode::Char('j') => {
            if at + 1 < listed.len() {
                QueueKey::Focus(id_at(at + 1))
            } else {
                QueueKey::Leave
            }
        }
        KeyCode::Home => QueueKey::Focus(id_at(0)),
        KeyCode::End => QueueKey::Focus(id_at(listed.len() - 1)),
        KeyCode::Enter | KeyCode::Char('s') => {
            if item.mode == "interrupt" || item.tag == "I" {
                QueueKey::Stay
            } else if is_steering(item) {
                QueueKey::Op(item.id.clone(), "interrupt", Some(item.id.clone()))
            } else {
                QueueKey::Op(item.id.clone(), "steer", Some(item.id.clone()))
            }
        }
        KeyCode::Delete | KeyCode::Backspace | KeyCode::Char('x') => {
            let then = if at + 1 < listed.len() {
                Some(id_at(at + 1))
            } else if at > 0 {
                Some(id_at(at - 1))
            } else {
                None
            };
            QueueKey::Op(item.id.clone(), "remove", then)
        }
        KeyCode::Esc => QueueKey::Leave,
        _ => QueueKey::Pass,
    }
}
/// A promoted waiting message (queued -> steer -> interrupt) flashes once: `FLASH_FRAMES` frames
/// of the 30 Hz clock. Client-only and visual; the durable change is `input.mode_changed`.
#[derive(Default)]
pub struct QueueFlash {
    seen: std::collections::HashMap<String, String>,
    started: std::collections::HashMap<String, std::time::Instant>,
}
const FLASH_FRAMES: f32 = 12.0;
const FLASH_FRAME: std::time::Duration = std::time::Duration::from_millis(33);
impl QueueFlash {
    /// Notes every item whose mode changed since the last call; new items never flash.
    pub fn observe(
        &mut self,
        items: &[crate::bridge::QueueItem],
        now: std::time::Instant,
        animate: bool,
    ) {
        for item in items {
            if animate
                && self
                    .seen
                    .get(&item.id)
                    .is_some_and(|mode| *mode != item.mode)
            {
                self.started.insert(item.id.clone(), now);
            }
        }
        self.seen = items
            .iter()
            .map(|i| (i.id.clone(), i.mode.clone()))
            .collect();
        self.started.retain(|id, at| {
            self.seen.contains_key(id)
                && now.duration_since(*at) < FLASH_FRAME.mul_f32(FLASH_FRAMES)
        });
    }
    /// True while a flash is in flight (the frame clock must keep running).
    pub fn live(&self, now: std::time::Instant, animate: bool) -> bool {
        animate
            && self
                .started
                .values()
                .any(|at| now.duration_since(*at) < FLASH_FRAME.mul_f32(FLASH_FRAMES))
    }
    /// Remaining strength 1.0 -> 0.0 of one item's flash.
    fn strength(&self, id: &str, now: std::time::Instant) -> f32 {
        self.started.get(id).map_or(0.0, |at| {
            let frames = now.duration_since(*at).as_secs_f32() / FLASH_FRAME.as_secs_f32();
            (1.0 - frames / FLASH_FRAMES).clamp(0.0, 1.0)
        })
    }
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
    /// Finished tool groups settling: statuses seen and the folds in flight (step 5), and
    /// the transient look each fold currently gives its group (swapped in by `update_content`).
    folds: crate::transcript::fold::Folds,
    fold_overlay: std::collections::HashMap<String, crate::bridge::Content>,
    blocks: Vec<crate::bridge::Content>,
    pub operations: Indexed<Option<serde_json::Value>>,
    pub image_protocol: Option<ratatui_image::protocol::StatefulProtocol>,
    image_source: String,
    /// Where the last frame drew the preview image (monochrome keeps its colours), and the
    /// screen it was drawn on (a resize wipes terminal graphics).
    image_area: Rect,
    image_screen: Rect,
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
    pub live_scroll: usize,
    /// Rows the Live pane above the composer is scrolled up from its bottom (0 follows the
    /// newest row), and the most it can be scrolled in the last frame (the wheel clamps to it).
    pub live_back: usize,
    pub live_max_back: usize,
    /// The pane's `expand`/`collapse` button from the last frame.
    pub live_button: Option<Rect>,
    /// Open/close motion of the Live panel (`live_anim`).
    pub live_anim: crate::live_anim::LiveAnim,
    /// Offset actually drawn in the last frame (pinned prompt, live bottom or frozen scroll).
    pub current_offset: usize,
    pub prompt_pin: crate::session::PromptPin,
    /// Pinned user message drawn by the last frame: its rectangle and the real card's top row.
    pub sticky: Option<(Rect, usize)>,
    /// Jump-to-latest pill drawn by the last frame, and the transcript length when the user
    /// scrolled away (new content after that is announced).
    pub jump_hit: Option<Rect>,
    away_len: Option<usize>,
    pub animate: bool,
    /// Monochrome preference: the drawn frame loses all colour.
    pub mono: bool,
    /// Persisted Appearance checkboxes (`~/.config/nexus/tui.json`).
    pub appearance: crate::config::Appearance,
    /// Checkbox rows drawn by the last frame (empty when the overlay is hidden); click region.
    pub appearance_rows: Rect,
    /// Settings root list state (search, cursor, scroll, root vs drilled page).
    pub settings_root: crate::settings_page::root::RootState,
    /// Focused Appearance checkbox row (Up/Down, Space/Enter).
    pub appearance_cursor: usize,
    /// Animation frame for the running-tool slot (`SPINNER_SLOT`).
    pub spin: usize,
    /// Independent 30 Hz clock for smooth activity motion.
    pub activity_frame: usize,
    /// The context window the header's share row was built with (rebuilt when it changes).
    pub context_window: Option<u64>,
    /// Promoted waiting messages flashing once in their new section (step 15).
    pub queue_flash: QueueFlash,
    /// Keyboard focus on a waiting message and the control held under the mouse (step 15).
    pub queue_ui: QueueUi,
    /// The newest microphone levels of the Live header's listening meter (step 11).
    pub mic: crate::live_bar::Mic,
    /// The draft is non-empty: empty-session hints keep their rows but go blank.
    pub typing: bool,
    pub content_elapsed: std::time::Duration,
    pub content_blocks: usize,
    pub content_reset: bool,
    details_chrome: Option<(u64, u16, (Vec<Line<'static>>, Vec<Option<usize>>))>,
    built_typing: bool,
    /// Line range of the transcript block focused with the keyboard (highlighted).
    pub focus: Option<(usize, usize)>,
    pub pointer: Option<(u16, u16)>,
    pub component_hover: components::Hover,
    pub toasts: toasts::Toasts,
    /// The parsed one-page Settings area and its client-side state (focus, scroll, popups).
    pub settings_page: Option<(u64, std::sync::Arc<crate::settings_page::Page>)>,
    pub page_state: crate::settings_page::PageState,
    /// Sessions dashboard state (query, selection, scroll); reset whenever the host hides it.
    pub dashboard: crate::picker::Dashboard,
    /// The left sidebar's presentation state (focus, drag, selections, Sessions filter).
    pub sidebar: crate::sidebar::SidebarUi,
    /// The right details sidebar's divider state (hover, drag).
    pub details: crate::details_sidebar::DetailsUi,
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
    let Some(operation) = operation.and_then(crate::copy_button::inner) else {
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
        // Copy markers do not split or create focus blocks; only the inner action counts.
        let operation = crate::copy_button::inner(operation);
        match operation {
            Some(op) if previous == Some(op) => out.last_mut().unwrap().1 = index,
            Some(_) => out.push((index, index)),
            None => {}
        }
        previous = operation;
    }
    out
}
pub const SPINNER_SLOT: char = '\u{e000}';
const SPINNER: [&str; 8] = ["⠋", "⠙", "⠸", "⢰", "⣠", "⣄", "⡆", "⠇"];
/// Running turns, tools, sessions and usage refreshes need ~8 Hz redraws.
pub fn animating(s: &Snapshot) -> bool {
    s.panel_loading
        || matches!(s.voice_phase.as_str(), "recording" | "transcribing")
        || s.live.active
        // An open permission prompt counts up "waiting on you · mm:ss".
        || s.prompt.as_ref().is_some_and(|prompt| !dialogs::prompt_rows(prompt).is_empty())
        || matches!(s.status.as_str(), "running" | "active" | "working")
        || s.blocks.iter().any(|block| {
            block.text.contains(SPINNER_SLOT)
                || (matches!(block.kind.as_str(), "tool_group" | "change" | "task")
                    && block.status == "running")
        })
        || s.sessions
            .iter()
            .chain(s.tabs.iter())
            .any(|row| row.status == "working")
}
/// The meter as drawn: with animations off the working fifth rests in the middle instead of
/// travelling, and in Monochrome (where colour is dropped) the lit cells are heavy `━` (`=` in
/// ASCII) so the working fifth and the context fill still read by shape.
fn activity_meter_for(
    s: &Snapshot,
    p: &Palette,
    width: usize,
    tick: usize,
    animate: bool,
    mono: bool,
) -> Vec<Span<'static>> {
    let spans = activity_meter(s, p, width, if animate { tick } else { 90 });
    if !mono {
        return spans;
    }
    let heavy = if crate::transcript::look::glyphs().ascii {
        "="
    } else {
        "━"
    };
    spans
        .into_iter()
        .map(|span| {
            if span.style.fg == Some(p.border) {
                span
            } else {
                Span::styled(heavy, span.style)
            }
        })
        .collect()
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

/// Overlay voice recording square (cell 2) and elapsed time (cells 4-8) onto activity rule spans.
pub fn overlay_activity_recording(
    spans: Vec<Span<'static>>,
    voice_phase: &str,
    p: &Palette,
    spin: usize,
    elapsed: u64,
    animate: bool,
    ascii: bool,
) -> Vec<Span<'static>> {
    if voice_phase != "recording" || spans.is_empty() {
        return spans;
    }
    let square_glyph = if ascii { "#" } else { "■" };
    let square_tone = if !animate || spin % 8 < 4 {
        p.error
    } else {
        nexus_widgets::theme::mix(p.error, p.panel, 0.55)
    };
    let mm = (elapsed / 60).min(99);
    let ss = elapsed % 60;
    let time_str = format!("{mm:02}:{ss:02}");

    let mut cells: Vec<Span<'static>> = Vec::with_capacity(spans.len());
    for span in spans {
        if span.content.chars().count() <= 1 {
            cells.push(span);
        } else {
            let style = span.style;
            for ch in span.content.chars() {
                cells.push(Span::styled(ch.to_string(), style));
            }
        }
    }

    if cells.len() > 2 {
        cells[2] = Span::styled(square_glyph, Style::default().fg(square_tone));
    }
    if cells.len() >= 9 {
        let digit_or_colon = |c: char| -> &'static str {
            match c {
                '0' => "0",
                '1' => "1",
                '2' => "2",
                '3' => "3",
                '4' => "4",
                '5' => "5",
                '6' => "6",
                '7' => "7",
                '8' => "8",
                '9' => "9",
                ':' => ":",
                _ => " ",
            }
        };
        for (i, ch) in time_str.chars().enumerate() {
            cells[4 + i] = Span::styled(digit_or_colon(ch), Style::default().fg(p.error));
        }
    }
    cells
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
    /// Drop the preview image's protocol state. Kitty-style protocols transmit the pixels
    /// only on the first render of a protocol and afterwards draw bare placeholders, so a
    /// protocol that outlives the terminal's copy (screen cleared, resized, panel closed)
    /// paints blank cells. The next draw decodes and transmits afresh.
    pub fn forget_image(&mut self) {
        self.image_protocol = None;
        self.image_source.clear();
        self.image_area = Rect::default();
    }

    /// Rows laid out for the block at `index` (its whole card), `None` if it has no rows.
    pub fn block_rows(&self, index: usize) -> Option<usize> {
        self.active_parts
            .get(index)?
            .as_ref()
            .map(|part| part.lines.len())
    }

    pub fn prompt_top(&self, turn: &str) -> Option<usize> {
        self.operations.iter().position(|op| {
            op.as_ref()
                .is_some_and(|op| op["prompt_turn"].as_str() == Some(turn))
        })
    }

    /// The page for this snapshot, parsed once per revision.
    pub fn page_for(
        &mut self,
        revision: u64,
        raw: &serde_json::Value,
    ) -> std::sync::Arc<crate::settings_page::Page> {
        if let Some((rev, page)) = &self.settings_page {
            if *rev == revision {
                return page.clone();
            }
        }
        let page = std::sync::Arc::new(crate::settings_page::Page::from_value(raw));
        self.settings_page = Some((revision, page.clone()));
        page
    }
    pub fn detail_rows(
        &mut self,
        s: &Snapshot,
        p: &Palette,
        width: u16,
    ) -> &crate::details_sidebar::Rows {
        // Stored as a pair below to share exactly the drawing/hit-test rows.
        let same = self.details_chrome.as_ref().is_some_and(|old| {
            old.0 == s.revision.wrapping_add(s.ui_local_revision << 32) && old.1 == width
        });
        if !same {
            let (lines, hits) = crate::details_sidebar::rows(s, p, width);
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
    /// Whether a finished tool group is still settling (its rows leave, its header flashes).
    /// The frame clock must keep running while this is true; it ends by itself.
    pub fn folding(&self) -> bool {
        self.folds.is_live()
    }
    /// Advance the fold transient of finished tool groups to now (see `transcript::fold`).
    pub fn step_folds(&mut self, blocks: &[crate::bridge::Content]) {
        self.step_folds_at(blocks, std::time::Instant::now());
    }
    pub fn step_folds_at(&mut self, blocks: &[crate::bridge::Content], now: std::time::Instant) {
        self.folds.observe(blocks, now, self.animate);
        let (mut overlay, mut touched) = self.folds.frames(blocks, now);
        // A group the user opened mid-fold just shows its boxes.
        overlay.retain(|id, _| !self.disclosure.is_open(id));
        touched.extend(self.fold_overlay.keys().cloned());
        self.fold_overlay = overlay;
        touched.sort();
        touched.dedup();
        for id in touched {
            if let Some(index) = blocks.iter().position(|block| block.id == id) {
                self.note_patch(Some(index));
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
            // A finished group mid-fold is drawn through its transient look.
            let folding = self.fold_overlay.get(&source.id).cloned();
            let source = folding.as_ref().unwrap_or(source);
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
                let mut rows = context::build(&block, width, palette, self.context_window);
                if source.kind == "user" && !source.turn_id.is_empty() {
                    if let Some((_, op)) = rows.iter_mut().find(|(_, op)| op.is_some()) {
                        if let Some(op) = op.as_mut().and_then(|op| op.as_object_mut()) {
                            op.insert("prompt_turn".into(), source.turn_id.clone().into());
                        }
                    }
                }
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
        self.details_chrome = None;
    }
}
#[derive(Default, Clone, Copy)]
pub struct Regions {
    pub transcript: Rect,
    pub composer: Rect,
    pub details: Rect,
    /// The left sidebar column (zero width while closed or when the conversation has no room).
    pub sidebar: Rect,
    pub tabs: Rect,
    pub context: Rect,
    /// The GPT Live panel between the composer and the bottom bar (zero height when no call
    /// is open; while it opens or closes only the revealed rows).
    pub live: Rect,
    /// The bottom bar under everything else: the activity meter row (empty on the Sessions
    /// dashboard and sub agent pages).
    pub bottom_bar: Rect,
}
/// Composer height: the editor grows with its wrapped content up to the terminal's
/// `max-height: 22` (9-row resting layout), leaving the
/// transcript at least four rows.
/// Most pending messages listed in the Queued section before a `+N more` row.
const QUEUE_VISIBLE: usize = 4;
/// Whether the running turn's status line is drawn above the composer.
pub fn status_visible(s: &Snapshot) -> bool {
    s.agent_page.is_empty()
        && (slot_waiting(s)
            || s.agents_running > 0
            || s.turn_status
                .as_ref()
                .is_some_and(|t| !t.activity.is_empty()))
}
/// The session's turn is held back by the daemon's global concurrent-turn cap.
fn slot_waiting(s: &Snapshot) -> bool {
    s.slot_wait.as_ref().is_some_and(|w| w.waiting > 0)
}
/// Rows of the unboxed Queued section (blank separator under the status line, header,
/// listed messages, `+N more`).
pub fn queue_rows(s: &Snapshot) -> u16 {
    let groups = queue_groups(s);
    if groups.is_empty() {
        return 0;
    }
    let listed: usize = if s.queue_collapsed {
        0
    } else {
        let limit = group_limit(groups.len());
        groups
            .iter()
            .map(|g| g.len().min(limit) + usize::from(g.len() > limit))
            .sum()
    };
    (usize::from(status_visible(s)) + groups.len() + listed) as u16
}
/// Steering messages (`steer` / `interrupt`) are delivered into the running turn; everything
/// else waits for the turn to finish.
fn is_steering(item: &crate::bridge::QueueItem) -> bool {
    matches!(item.mode.as_str(), "steer" | "interrupt") || matches!(item.tag.as_str(), "S" | "I")
}
/// Queue indices of the Steering section, then of the Queued section (empty ones omitted).
/// Each keeps the real queue order; `#n` labels are the real queue positions.
fn queue_groups(s: &Snapshot) -> Vec<Vec<usize>> {
    let (steer, queued): (Vec<usize>, Vec<usize>) =
        (0..s.queue_items.len()).partition(|i| is_steering(&s.queue_items[*i]));
    [steer, queued]
        .into_iter()
        .filter(|g| !g.is_empty())
        .collect()
}
/// Messages listed per section; two sections share the room a single one would use.
fn group_limit(groups: usize) -> usize {
    if groups > 1 {
        QUEUE_VISIBLE / 2
    } else {
        QUEUE_VISIBLE
    }
}
/// Where each row above the card sits (top to bottom: status line, blank, then per section
/// `▾ Steering N` / `▾ Queued N`, `#n text` rows, `+N more`), directly on the card.
#[derive(Default)]
pub struct AboveLayout {
    pub status: Option<Rect>,
    /// `(steering, row)` of each section header, Steering first.
    pub headers: Vec<(bool, Rect)>,
    /// `(queue index, row)` of each listed message.
    pub items: Vec<(usize, Rect)>,
    /// `(steering, hidden count, row)` of each `+N more`.
    pub more: Vec<(bool, usize, Rect)>,
}
pub fn above_layout(composer: Rect, s: &Snapshot) -> AboveLayout {
    let rows = composer_rows(composer, s);
    let x = rows[1].x + 2;
    let width = rows[1].width.saturating_sub(4);
    let mut y = rows[0].y;
    let end = rows[0].bottom();
    let take = |y: &mut u16| {
        let row = Rect::new(x, *y, width, 1);
        let fits = *y < end;
        *y += 1;
        fits.then_some(row)
    };
    let mut out = AboveLayout::default();
    if status_visible(s) {
        out.status = take(&mut y).map(|row| {
            let inset = row.width.min(2);
            Rect::new(row.x + inset, row.y, row.width - inset, row.height)
        });
    }
    let groups = queue_groups(s);
    if !groups.is_empty() && status_visible(s) {
        y += 1;
    }
    let limit = group_limit(groups.len());
    for group in &groups {
        let steering = is_steering(&s.queue_items[group[0]]);
        if let Some(row) = take(&mut y) {
            out.headers.push((steering, row));
        }
        if s.queue_collapsed {
            continue;
        }
        for index in group.iter().take(limit) {
            if let Some(row) = take(&mut y) {
                out.items.push((*index, row));
            }
        }
        if group.len() > limit {
            if let Some(row) = take(&mut y) {
                out.more.push((steering, group.len() - limit, row));
            }
        }
    }
    out
}
/// Controls of one pending message, left to right: `(gap before, width, op)`.
/// Queued: `↑ ↓  [Steer]  ✕`; steering: `[Interrupt]  ✕` (no arrows, only queued items are
/// reordered); interrupt: `✕`.
fn queue_buttons(item: &crate::bridge::QueueItem) -> &'static [(u16, u16, &'static str)] {
    if item.mode == "interrupt" || item.tag == "I" {
        &[(0, 1, "remove")]
    } else if is_steering(item) {
        &[(0, 11, "interrupt"), (2, 1, "remove")]
    } else {
        &[
            (0, 1, "up"),
            (1, 1, "down"),
            (2, 7, "steer"),
            (2, 1, "remove"),
        ]
    }
}
/// Columns the controls take, so the hovered row's text truncates before them.
fn queue_buttons_width(item: &crate::bridge::QueueItem) -> u16 {
    queue_buttons(item).iter().map(|(gap, w, _)| gap + w).sum()
}
/// The control cells of each listed message: `(row, queue index, [(x, width, op)])`. `✕` always
/// sits at `row.right() - 2`; the other buttons are laid out to its left.
fn queue_controls(
    composer: Rect,
    s: &Snapshot,
) -> Vec<(u16, usize, Vec<(u16, u16, &'static str)>)> {
    above_layout(composer, s)
        .items
        .into_iter()
        .filter(|(index, row)| row.width >= queue_buttons_width(&s.queue_items[*index]) + 8)
        .map(|(index, row)| {
            let item = &s.queue_items[index];
            let mut x = (row.right().saturating_sub(1)).saturating_sub(queue_buttons_width(item));
            let cells = queue_buttons(item)
                .iter()
                .map(|(gap, width, op)| {
                    x += gap;
                    let cell = (x, *width, *op);
                    x += width;
                    cell
                })
                .collect();
            (row.y, index, cells)
        })
        .collect()
}
/// Which pending message and action (`up`, `down`, `remove`) a click lands on.
pub fn queue_control_at(
    composer: Rect,
    s: &Snapshot,
    x: u16,
    y: u16,
) -> Option<(String, &'static str)> {
    if !s.agent_page.is_empty() {
        return None;
    }
    queue_controls(composer, s)
        .into_iter()
        .find(|(row, _, _)| *row == y)
        .and_then(|(_, index, cells)| {
            cells
                .iter()
                .find(|(cell, width, _)| x >= *cell && x < cell + width)
                .filter(|(_, _, op)| queue_move_enabled(&s.queue_items[index], op))
                .map(|(_, _, op)| (s.queue_items[index].id.clone(), *op))
        })
}
/// Moves that would not change the message's place in its section are disabled (and drawn blank).
fn queue_move_enabled(item: &crate::bridge::QueueItem, op: &str) -> bool {
    match op {
        "up" => item.can_up,
        "down" => item.can_down,
        _ => true,
    }
}
/// True when a click on `(x, y)` hits a section header (caret or label): collapse/expand.
pub fn queue_header_at(composer: Rect, s: &Snapshot, x: u16, y: u16) -> bool {
    s.agent_page.is_empty()
        && above_layout(composer, s)
            .headers
            .iter()
            .any(|(_, row)| y == row.y && x >= row.x && x < row.x + 18.min(row.width))
}
/// `2.0s` under a minute, then `4m40s`, `1h02m`.
pub fn fmt_activity(secs: f64) -> String {
    let secs = secs.max(0.0);
    if secs < 60.0 {
        format!("{secs:.1}s")
    } else {
        fmt_turn(secs)
    }
}
pub fn fmt_turn(secs: f64) -> String {
    let total = secs.max(0.0) as u64;
    match total {
        0..=59 => format!("{:.1}s", secs.max(0.0)),
        60..=3599 => format!("{}m {}s", total / 60, total % 60),
        _ => format!("{}h {}m", total / 3600, total % 3600 / 60),
    }
}
/// `842`, `1.2k`, `121k`, `1.4M`.
pub fn fmt_tokens(n: u64) -> String {
    match n {
        0..=999 => n.to_string(),
        1000..=9999 => format!("{:.1}k", n as f64 / 1000.0),
        10_000..=999_999 => format!("{}k", n / 1000),
        _ => format!("{:.1}M", n as f64 / 1_000_000.0),
    }
}
/// The right-aligned `4m40s ↓121k [stop]` text and the byte offset of `[stop]`.
fn status_right(s: &Snapshot, now: f64) -> Option<(String, usize)> {
    let t = s.turn_status.as_ref()?;
    let mut text = fmt_turn(now - t.turn_since);
    if t.output_tokens > 0 {
        text += &format!(
            " ↓{}{}",
            if t.output_estimated { "~" } else { "" },
            fmt_tokens(t.output_tokens)
        );
    }
    text.push(' ');
    let stop_at = text.len(); // byte offset: the text holds multi-byte arrows
    text += "[stop]";
    Some((text, stop_at))
}
/// The `[stop]` button's cells; a click cancels the turn like Esc Esc / Ctrl+C.
pub fn stop_at(composer: Rect, s: &Snapshot, x: u16, y: u16) -> bool {
    let Some(row) = above_layout(composer, s).status else {
        return false;
    };
    let Some((text, at)) = status_right(s, crate::live_bar::now_epoch()) else {
        return false;
    };
    if slot_waiting(s) && s.slot_wait.is_some() {
        return false; // the waiting line has no button
    }
    // Same placement as `draw_status_line`: `∴`, label, gap, then the right text.
    let label = status_label(s, usize::from(row.width), text.width());
    let used = 2 + label.width();
    let gap = usize::from(row.width).saturating_sub(used + text.width());
    let start = usize::from(row.x) + used + gap + text[..at].width();
    y == row.y && usize::from(x) >= start && x < row.right()
}
/// The activity label, truncated so the right-hand text keeps its room.
fn status_label(s: &Snapshot, row_width: usize, right_width: usize) -> String {
    let activity = s
        .turn_status
        .as_ref()
        .map(|t| t.activity.as_str())
        .unwrap_or_default();
    let left_room = row_width.saturating_sub(right_width + 2);
    crate::transcript::truncate(&format!("{activity}…"), left_room.saturating_sub(10))
}
/// Motion-clock frames (30 Hz) one shimmer sweep takes across the activity word: 1.8 s.
const SHIMMER_FRAMES: usize = 54;
/// Half-width, in graphemes, of the shimmer's soft falloff.
const SHIMMER_REACH: f32 = 4.0;
/// `(light, dark)` greys for the status shimmer: the lighter and darker of `p.text` and
/// `p.quiet`. Dark theme: text is light grey, quiet dark grey. Light theme: quiet is the
/// light grey and text the dark grey. Never `p.purple` or `p.accent`.
fn shimmer_greys(p: &Palette) -> (Color, Color) {
    let sum = |c: Color| match c {
        Color::Rgb(r, g, b) => u32::from(r) + u32::from(g) + u32::from(b),
        _ => 0,
    };
    if sum(p.text) >= sum(p.quiet) {
        (p.text, p.quiet)
    } else {
        (p.quiet, p.text)
    }
}
/// The activity word as one span per grapheme, always its real characters (THK-4, step 6).
/// A highlight sweeps left to right once every `SHIMMER_FRAMES` motion frames and each
/// grapheme blends dark grey (base) to light grey (highlight) by its distance from it, with
/// a soft `SHIMMER_REACH` falloff. Animation off: the word in the base grey. Monochrome:
/// plain text.
fn shimmer_spans(
    word: &str,
    frame: usize,
    animate: bool,
    mono: bool,
    p: &Palette,
) -> Vec<Span<'static>> {
    if mono {
        return vec![Span::raw(word.to_owned())];
    }
    let (light, dark) = shimmer_greys(p);
    if !animate {
        return vec![Span::styled(word.to_owned(), Style::default().fg(dark))];
    }
    let count = word.graphemes(true).count() as f32;
    let travel = count + 2.0 * SHIMMER_REACH;
    let head = (frame % SHIMMER_FRAMES) as f32 / SHIMMER_FRAMES as f32 * travel - SHIMMER_REACH;
    word.graphemes(true)
        .enumerate()
        .map(|(i, g)| {
            let near = (1.0 - (i as f32 - head).abs() / SHIMMER_REACH).clamp(0.0, 1.0);
            let amount = near * near * (3.0 - 2.0 * near); // smoothstep
            Span::styled(
                g.to_owned(),
                Style::default().fg(nexus_widgets::theme::mix(dark, light, amount)),
            )
        })
        .collect()
}
fn draw_status_line(
    frame: &mut Frame,
    row: Rect,
    s: &Snapshot,
    p: &Palette,
    frame_tick: usize,
    animate: bool,
    mono: bool,
    hover_stop: bool,
) {
    if let Some(w) = s.slot_wait.as_ref().filter(|_| slot_waiting(s)) {
        let label = crate::transcript::truncate(
            "Waiting for a free turn slot",
            usize::from(row.width).saturating_sub(2),
        );
        let counts = format!(" ({} of {} running)", w.running, w.limit);
        let room = usize::from(row.width).saturating_sub(2 + label.width());
        let counts = crate::transcript::truncate(&counts, room);
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled("⋯", Style::default().fg(p.quiet)),
                Span::raw(" "),
                Span::styled(label, Style::default().fg(p.quiet)),
                Span::styled(counts, Style::default().fg(p.quiet)),
            ])),
            row,
        );
        return;
    }
    if s.turn_status.is_none() {
        // The root is idle but background subagents still run: say so, and where to stop them.
        if s.agents_running > 0 {
            let noun = if s.agents_running == 1 { "agent" } else { "agents" };
            let text = crate::transcript::truncate(
                &format!("{} {noun} running · /tasks to open or stop", s.agents_running),
                usize::from(row.width).saturating_sub(2),
            );
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled("⋯", Style::default().fg(p.quiet)),
                    Span::raw(" "),
                    Span::styled(text, Style::default().fg(p.quiet)),
                ])),
                row,
            );
        }
        return;
    }
    let now = crate::live_bar::now_epoch();
    let (right, stop) = status_right(s, now).unwrap_or_default();
    let label = status_label(s, usize::from(row.width), right.width());
    // `∴` and the activity word, shimmering grey on the 30 Hz motion clock (THK-4). Only
    // colours change, so the label keeps its width and `stop_at` keeps its geometry.
    let mark = if crate::transcript::look::glyphs().ascii {
        ":"
    } else {
        "∴"
    };
    let mut spans = vec![
        Span::styled(mark, Style::default().fg(shimmer_greys(p).1)),
        Span::raw(" "),
    ];
    spans.extend(shimmer_spans(&label, frame_tick, animate, mono, p));
    // The only elapsed time is the right-hand turn clock.
    let used = 2 + label.width();
    let gap = usize::from(row.width).saturating_sub(used + right.width());
    spans.push(Span::raw(" ".repeat(gap)));
    let (info, button) = right.split_at(stop);
    spans.push(Span::styled(info.to_string(), Style::default().fg(p.quiet)));
    // Hovering the button lights exactly its cells: error colour on the element background.
    spans.push(Span::styled(
        button.to_string(),
        if hover_stop {
            Style::default()
                .fg(p.error)
                .bg(p.element)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(p.quiet)
        },
    ));
    frame.render_widget(Paragraph::new(Line::from(spans)), row);
}
/// Grok's unboxed Queued section plus the status line, on the composer rail.
fn draw_above_card(
    frame: &mut Frame,
    composer: Rect,
    s: &Snapshot,
    p: &Palette,
    frame_tick: usize,
    animate: bool,
    mono: bool,
    pointer: Option<(u16, u16)>,
    flash: &QueueFlash,
    ui: &QueueUi,
) {
    let layout = above_layout(composer, s);
    let now = std::time::Instant::now();
    let over =
        |row: Rect| pointer.is_some_and(|(x, y)| y == row.y && x >= row.x && x < row.right());
    if let Some(row) = layout.status {
        let hover_stop = pointer.is_some_and(|(x, y)| stop_at(composer, s, x, y));
        draw_status_line(frame, row, s, p, frame_tick, animate, mono, hover_stop);
    }
    let groups = queue_groups(s);
    for (steering, row) in &layout.headers {
        let count = groups
            .iter()
            .find(|g| is_steering(&s.queue_items[g[0]]) == *steering)
            .map_or(0, Vec::len);
        let caret = if s.queue_collapsed { "▸" } else { "▾" };
        let label = if *steering { "Steering" } else { "Queued" };
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(
                    format!("{caret} {label}"),
                    Style::default()
                        .fg(if *steering { p.accent } else { p.muted })
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(format!(" {count}"), Style::default().fg(p.quiet)),
            ])),
            *row,
        );
    }
    for (index, row) in &layout.items {
        let item = &s.queue_items[*index];
        let hot = over(*row) || ui.focus.as_deref() == Some(item.id.as_str());
        let held = |op: &str| {
            ui.press
                .as_ref()
                .is_some_and(|(id, held_op)| *id == item.id && *held_op == op)
        };
        let tone = if is_steering(item) { p.accent } else { p.quiet };
        let prefix = format!("  #{} ", index + 1);
        let buttons = usize::from(queue_buttons_width(item));
        let controls = if hot { buttons + 2 } else { 0 };
        let room = usize::from(row.width).saturating_sub(prefix.width() + controls + 1);
        let text = crate::transcript::truncate(&item.text, room);
        let mut spans = vec![
            Span::styled(prefix.clone(), Style::default().fg(tone)),
            Span::styled(text.clone(), Style::default().fg(p.text)),
        ];
        if hot {
            let used = prefix.width() + text.width();
            let gap = usize::from(row.width).saturating_sub(used + buttons + 1);
            spans.push(Span::raw(" ".repeat(gap)));
            let (up, down, cross) = queue_marks(crate::transcript::look::glyphs().ascii);
            let reversed = |op: &str| {
                if ui.mono && held(op) {
                    Modifier::REVERSED
                } else {
                    Modifier::empty()
                }
            };
            let chip_for = |op: &str| {
                Style::default()
                    .fg(p.text)
                    .bg(if held(op) { p.muted } else { p.element_hi })
                    .add_modifier(Modifier::BOLD | reversed(op))
            };
            for (before, _, op) in queue_buttons(item) {
                spans.push(Span::raw(" ".repeat(usize::from(*before))));
                spans.push(match *op {
                    // A disabled move is blank, not a dimmed arrow, so it never looks clickable.
                    "up" => Span::styled(
                        if item.can_up { up } else { " " },
                        Style::default().fg(p.text),
                    ),
                    "down" => Span::styled(
                        if item.can_down { down } else { " " },
                        Style::default().fg(p.text),
                    ),
                    "steer" => Span::styled(" Steer ", chip_for("steer")),
                    "interrupt" => Span::styled(" Interrupt ", chip_for("interrupt")),
                    _ => Span::styled(
                        cross,
                        Style::default()
                            .fg(if held("remove") { p.error } else { p.muted })
                            .add_modifier(reversed("remove")),
                    ),
                });
            }
        }
        let glow = flash.strength(&item.id, now);
        let style = if hot {
            Style::default().bg(p.element)
        } else if glow > 0.0 {
            Style::default().bg(nexus_widgets::theme::mix(
                p.background,
                p.accent,
                0.22 * glow,
            ))
        } else {
            Style::default()
        };
        frame.render_widget(Paragraph::new(Line::from(spans)).style(style), *row);
    }
    for (_, hidden, row) in &layout.more {
        frame.render_widget(
            Paragraph::new(Line::styled(
                format!("  +{hidden} more"),
                Style::default().fg(p.quiet),
            )),
            *row,
        );
    }
}
/// Rows above the composer card: the status line, queued messages, and a blank margin row.
/// The final blank row keeps activity/status controls visually separate from the composer.
fn above_card_rows(s: &Snapshot) -> u16 {
    let status = u16::from(status_visible(s));
    let queue = queue_rows(s);
    status + queue + u16::from(queue == 0)
}
pub fn composer_height(area: Rect, draft: &Editor, s: &Snapshot) -> u16 {
    // The Sessions dashboard has no composer; its list takes the rows.
    if s.dashboard.is_some() {
        return 0;
    }
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
    // Five fixed rows plus the blank row that always separates the text from the controls.
    let wanted = 5 + rows.clamp(1, 22) as u16 + above_card_rows(s).max(1);
    wanted
        .min(
            area.height
                .saturating_sub(4 + 4 + crate::live_bar::pane_rows(s)),
        )
        .min(area.height)
        .max(area.height.min(7))
}
/// Settings → Layout → Centered conversation: the widest the conversation column gets.
pub const CENTERED_WIDTH: u16 = 80;
/// Terminals shorter than this drop the thin line between the tabs and the workspace row.
pub const TOP_LINE_MIN_HEIGHT: u16 = 24;
/// Columns the left sidebar and the conversation share: everything left of the details sidebar
/// (or its drawer). The sidebar's drag bound and `regions` both start from it.
pub fn sidebar_available(area: Rect, s: &Snapshot) -> u16 {
    area.width - crate::details_sidebar::width_for(s, area.width)
}
pub fn regions(area: Rect, s: &Snapshot, composer_height: u16, _logs_open: bool) -> Regions {
    // The Sessions dashboard draws its own header on row 0 and takes the width the details
    // sidebar leaves.
    let dash = s.dashboard.is_some();
    // The details sidebar's width is the stored preference (24..=60, default 40), bounded by
    // the room the conversation needs (`details_sidebar::width_for`).
    let details_width = crate::details_sidebar::width_for(s, area.width);
    let right = if area.width >= crate::details_sidebar::DRAWER_BELOW {
        details_width
    } else {
        0
    };
    let columns = Layout::horizontal([Constraint::Min(1), Constraint::Length(right)]).split(area);
    // Below 100 columns the details drawer takes the right edge, and the conversation reflows
    // into the column to its left so no row is clipped. A tiny terminal shrinks the drawer so
    // the conversation keeps at least 24 columns.
    let drawer = s.details_sidebar && area.width < crate::details_sidebar::DRAWER_BELOW;
    let drawer_width = if drawer { details_width } else { 0 };
    let mut middle = columns[0];
    if drawer {
        middle.width = area.width - drawer_width;
    }
    // The selector overlays the conversation; it never reserves a left column.
    let sidebar = if s.sidebar.open {
        crate::sidebar::selector_area(area)
    } else {
        Rect::default()
    };
    if s.centered_layout && middle.width > CENTERED_WIDTH && !dash {
        middle.x += (middle.width - CENTERED_WIDTH) / 2;
        middle.width = CENTERED_WIDTH;
    }
    // The Live panel sits under the composer card, above the bottom bar (the activity meter
    // row). Its rows come out of the transcript, never the composer: it is capped so the
    // transcript keeps one row, and expanded it takes every row the transcript can spare.
    // The session bar is four rows (tabs, thin line, workspace row, thin rule), three on short
    // terminals (no line); the dashboard has none.
    let bar: u16 = if dash {
        0
    } else if area.height < TOP_LINE_MIN_HEIGHT {
        3
    } else {
        4
    };
    let foot = u16::from(composer_height > 3);
    let fixed = 1 + bar + composer_height;
    let spare = middle.height.saturating_sub(fixed + 1);
    let mut pane = if composer_height > 0 {
        crate::live_bar::pane_rows(s)
    } else {
        0
    };
    pane = if pane > 1 && s.live_expanded {
        pane.max(spare)
    } else {
        pane.min(spare)
    };
    // One empty row above the top bar (and the Sessions header). In a session the bar is three
    // rows: the tabs, the workspace path and branch, then a thin `─` rule. The rule stands in
    // for the old blank row below the bar (a terminal cannot allocate half a row; the rule
    // glyph sits mid-cell, so it reads as half a row of air plus a line), so the transcript
    // starts on the same row as before.
    let center = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(bar),
        Constraint::Length(0),
        Constraint::Min(1),
        Constraint::Length(composer_height - foot),
        Constraint::Length(pane),
        Constraint::Length(foot),
    ])
    .split(middle);
    Regions {
        tabs: center[1],
        sidebar,
        details: if drawer {
            Rect::new(area.right() - drawer_width, area.y, drawer_width, area.height)
        } else {
            columns[1]
        },
        context: Rect::default(),
        transcript: center[3],
        composer: center[4],
        live: center[5],
        bottom_bar: center[6],
    }
}
/// The GPT Live panel under the composer card. It is laid out at its full height off screen and
/// only the revealed top rows are copied in, so opening slides it out from beneath the composer
/// header first (`live_anim`); while it moves its text fades in and an accent band sweeps along
/// the header. The pane scroll state (`live_back`, `live_button`) belongs to the full layout.
fn draw_live_panel(
    frame: &mut Frame,
    s: &Snapshot,
    live: &crate::bridge::LiveBar,
    r: &Regions,
    p: &Palette,
    spin: usize,
    cache: &mut Cache,
) {
    use ratatui::widgets::Widget as _;
    let target = if s.live_reveal.is_some() {
        cache.live_anim.target.max(r.live.height)
    } else {
        r.live.height
    };
    let full = Rect::new(r.live.x, r.live.y, r.live.width, target);
    let mut buf = ratatui::buffer::Buffer::empty(full);
    Block::default()
        .style(Style::default().bg(p.background).fg(p.text))
        .render(full, &mut buf);
    let rows = composer_rows(r.composer, s);
    let x = rows[1].x + 2;
    let width = rows[1].width.saturating_sub(4);
    let now_epoch = crate::live_bar::now_epoch();
    // The motion band runs on the smooth 30 Hz clock, and rests at frame 0 without animation.
    let motion = if cache.animate { cache.activity_frame } else { 0 };
    if target == 1 {
        // No rows to show (connecting, or the call is on another session): the header alone.
        Paragraph::new(Line::from(crate::live_bar::spans(
            live,
            p,
            motion,
            now_epoch,
            usize::from(width),
            cache.animate,
            &cache.mic,
        )))
        .style(Style::default().bg(p.element))
        .render(Rect::new(x, full.y, width, 1), &mut buf);
    } else {
        // The open call's rows: a header (label, `↑ N more`, expand button) and a scrollable
        // pane below it, in line with the composer card.
        let all = crate::live_bar::captions(
            &live.detail,
            p.purple,
            usize::from(width),
            p,
            &live.operation,
        );
        let body = Rect::new(x, full.y + 1, width, target - 1);
        let (back, start) =
            crate::live_bar::pane_window(all.len(), usize::from(body.height), cache.live_back);
        cache.live_back = back;
        cache.live_max_back = all.len().saturating_sub(usize::from(body.height));
        let header = Rect::new(x, full.y, width, 1);
        let label = if s.live_expanded {
            "⤡ collapse"
        } else {
            "⤢ expand"
        };
        let bw = (label.width() as u16 + 2).min(header.width);
        let button = Rect::new(header.right() - bw, header.y, bw, 1);
        cache.live_button = Some(button);
        let more = if start > 0 {
            format!("↑ {start} more  ")
        } else {
            String::new()
        };
        // The call's phase strip (phase, animation, clock) takes what the right side leaves.
        let left_width =
            usize::from(header.width).saturating_sub(more.width() + usize::from(bw) + 1);
        let mut head = crate::live_bar::spans(
            live,
            p,
            motion,
            now_epoch,
            left_width,
            cache.animate,
            &cache.mic,
        );
        let used: usize = head.iter().map(|span| span.width()).sum();
        let gap = usize::from(header.width).saturating_sub(used + more.width() + usize::from(bw));
        head.push(Span::raw(" ".repeat(gap)));
        head.push(Span::styled(more, Style::default().fg(p.quiet)));
        Paragraph::new(Line::from(head))
            .style(Style::default().bg(p.element))
            .render(header, &mut buf);
        Paragraph::new(Span::styled(
            format!(" {label} "),
            Style::default().fg(p.muted).bg(p.element_hi),
        ))
        .render(button, &mut buf);
        let end = (start + usize::from(body.height)).min(all.len());
        let lines: Vec<Line> = all[start..end]
            .iter()
            .map(|(line, _)| line.clone())
            .collect();
        Paragraph::new(lines).render(body, &mut buf);
        let below = all.len() - end;
        if below > 0 {
            let text = format!("↓ {below} newer");
            let w = (text.width() as u16).min(body.width);
            let rect = Rect::new(body.right() - w, body.bottom() - 1, w, 1);
            Clear.render(rect, &mut buf);
            Paragraph::new(Span::styled(text, Style::default().fg(p.quiet))).render(rect, &mut buf);
        }
    }
    if cache.animate {
        let now = std::time::Instant::now();
        let progress = cache.live_anim.progress(now);
        if progress < 1.0 {
            // Text fades in (out on close) with the height.
            for cell in buf.content.iter_mut() {
                cell.fg = nexus_widgets::theme::mix(p.background, cell.fg, progress);
            }
        }
        if let Some(t) = cache.live_anim.sweep(now) {
            // An accent band runs along the header, brightest at the start and fading out.
            let span = f32::from(width) + 8.0;
            let centre = t * span - 4.0;
            for col in 0..width {
                let d = (f32::from(col) - centre).abs();
                if d < 4.0 {
                    let strength = (1.0 - d / 4.0) * (1.0 - t * 0.6);
                    let cell = &mut buf[(x + col, full.y)];
                    cell.bg = nexus_widgets::theme::mix(cell.bg, p.accent, 0.6 * strength);
                    cell.fg = nexus_widgets::theme::mix(cell.fg, p.text, 0.5 * strength);
                }
            }
        }
    }
    let out = frame.buffer_mut();
    for y in r.live.y..r.live.bottom().min(full.bottom()) {
        for col in r.live.x..r.live.right() {
            out[(col, y)] = buf[(col, y)].clone();
        }
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
/// The launch splash (`splash.rs`): converging wordmark and milestone bar, drawn on the plain
/// dark background until the first chat frame, so the terminal answers the launch at once.
pub fn draw_splash(frame: &mut Frame, splash: &crate::splash::Splash, now: std::time::Instant) {
    splash.draw(frame, now, &Palette::new(false));
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
    logs_scroll: usize,
    details_scroll: usize,
) -> Regions {
    let p = Palette::new(s.theme == "nexus-light");
    // Animations off: spinners rest on their first frame (the clock still runs for counters).
    let spin = if cache.animate { cache.spin } else { 0 };
    frame.render_widget(
        Block::default().style(Style::default().bg(p.background).fg(p.text)),
        frame.area(),
    );
    let mut r = regions(
        frame.area(),
        s,
        composer_height(frame.area(), draft, s),
        logs_open,
    );
    if s.dashboard.is_none() {
        draw_top_bar(frame, s, r.tabs, &p, spin);
    }
    cache.content_elapsed = std::time::Duration::ZERO;
    cache.typing = !draft.text.is_empty();
    cache.page = s.agent_page.clone();
    let animate = cache.animate;
    cache
        .queue_flash
        .observe(&s.queue_items, std::time::Instant::now(), animate);
    let listed: Vec<String> = queue_listed(r.composer, s)
        .into_iter()
        .map(|i| s.queue_items[i].id.clone())
        .collect();
    cache.queue_ui.prune(&listed);
    cache.queue_ui.mono = cache.mono;
    if s.blocks.is_empty() {
        cache.update(&s.lines, r.transcript.width, &p);
    } else {
        cache.step_folds(&s.blocks);
        // A new window rebuilds the header's share row.
        if std::mem::replace(&mut cache.context_window, s.context_window) != s.context_window {
            cache.reset();
        }
        cache.update_content(&[], &s.blocks, r.transcript.width, &p);
    }
    // While scrolled away from the bottom the jump pill owns the transcript's last row, so the
    // viewport shrinks by one and no content is covered.
    let pill_row = if !follow
        && s.dashboard.is_none()
        && s.panel_title.is_empty()
        && r.transcript.width > 4
        && r.transcript.height > 1
    {
        let full = r.transcript.height as usize;
        let max = crate::session::max_offset(cache.lines.len(), full);
        scroll.min(max) + full < cache.lines.len()
    } else {
        false
    };
    let pill_rect = if pill_row {
        r.transcript.height -= 1;
        Some(Rect::new(
            r.transcript.x,
            r.transcript.bottom(),
            r.transcript.width,
            1,
        ))
    } else {
        None
    };
    let height = r.transcript.height as usize;
    cache.pad = crate::session::tail_padding(cache.lines.len(), height);
    cache.max_scroll = crate::session::max_offset(cache.lines.len(), height);
    cache.live_scroll = crate::session::live_offset(cache.lines.len(), height);
    let pin = if follow {
        let scope = crate::session::Scope {
            session: s.composer_key.clone(),
            page: s.agent_page.clone(),
            generation: s.generation,
        };
        let mut pin = std::mem::take(&mut cache.prompt_pin);
        let target = pin.target(&scope, &s.blocks, cache, height);
        cache.prompt_pin = pin;
        target
    } else {
        cache.prompt_pin.release();
        None
    };
    let offset = if follow {
        pin.unwrap_or(cache.live_scroll)
    } else {
        scroll.min(cache.max_scroll)
    };
    cache.current_offset = offset;
    // The Copy button of the block under the pointer, and whether the pointer is on it.
    let copy_hover = cache
        .pointer
        .filter(|&(x, y)| {
            s.panel_title.is_empty() && s.prompt.is_none() && r.transcript.contains((x, y).into())
        })
        .and_then(|(x, y)| {
            let row = offset + usize::from(y - r.transcript.y);
            let first = crate::copy_button::first_row(&cache.operations, row)?;
            let column = usize::from(x - r.transcript.x);
            let on =
                crate::copy_button::hit(&cache.operations, &cache.lines, row, column).is_some();
            Some((first, on))
        });
    frame.render_widget(
        Paragraph::new(
            cache
                .lines
                .range(offset, r.transcript.height as usize)
                .enumerate()
                .map(|(row, line)| {
                    let mut drawn = with_spinner(line, spin);
                    if cache.animate
                        && line
                            .spans
                            .iter()
                            .any(|span| span.content.contains(SPINNER_SLOT))
                    {
                        let amount = nexus_widgets::anim::wave(
                            spin as u64,
                            row as u16,
                            nexus_widgets::anim::WAVE_ROWS,
                            nexus_widgets::anim::WAVE_SPEED,
                        );
                        let tone = nexus_widgets::theme::mix(p.muted, p.text, amount);
                        for span in &mut drawn.spans {
                            if span.content.contains(SPINNER[spin % SPINNER.len()]) {
                                span.style.fg = Some(tone);
                            }
                        }
                    }
                    // Only the visible rows are animated; colour-free modes skip it.
                    if cache.animate && !cache.mono {
                        drawn = crate::transcript::look::animate_row(
                            &drawn,
                            cache.activity_frame as u64,
                            row as u16,
                            p.muted,
                            p.text,
                        );
                    }
                    // The streaming write head glows only while animating in colour.
                    if (!cache.animate || cache.mono)
                        && drawn.spans.iter().any(crate::transcript::look::is_head)
                    {
                        drawn = crate::transcript::look::cool_head(&drawn, p.text);
                    }
                    let mut line = drawn;
                    if let Some((_, on)) = copy_hover.filter(|(first, _)| *first == offset + row) {
                        if let Some((start, _)) = crate::copy_button::columns(&line) {
                            let style = if on {
                                Style::default().fg(p.text).bg(p.element_hi)
                            } else {
                                Style::default().fg(p.muted)
                            };
                            line = crate::copy_button::paint(line, start, style);
                        }
                    }
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
    // The scrollbar spans what scrolling can reach (down to the last content row, or a pinned
    // prompt below it), not the tail padding, so its thumb touches the end when scrolling stops.
    let reach = cache.live_scroll.max(offset);
    if reach > 0 && r.transcript.width > 2 {
        // A thin scrollbar on the transcript's right edge, like the terminal's.
        let mut state = ratatui::widgets::ScrollbarState::new(reach).position(offset);
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
    cache.sticky = None;
    if s.dashboard.is_none() && s.panel_title.is_empty() && offset > 0 {
        let mut tops = std::collections::HashMap::new();
        for (row, op) in cache.operations.iter().enumerate().take(offset + height) {
            if let Some(turn) = op.as_ref().and_then(|op| op["prompt_turn"].as_str()) {
                // Some cards put the operation on their padding, others on the title.
                // Include top padding exactly once, never a preceding answer row.
                let is_padding = cache.lines.get(row).is_some_and(|line| {
                    line.spans.iter().all(|span| span.content.trim().is_empty())
                });
                tops.insert(
                    turn.to_string(),
                    if is_padding {
                        row
                    } else {
                        row.saturating_sub(1)
                    },
                );
            }
        }
        let cards: Vec<(usize, usize)> = s
            .blocks
            .iter()
            .enumerate()
            .filter(|(_, b)| b.kind == "user" && !b.turn_id.is_empty())
            .filter_map(|(i, b)| Some((*tops.get(&b.turn_id)?, cache.block_rows(i)?)))
            .collect();
        if let Some(pin) = crate::sticky::select(&cards, offset, cache.lines.len()) {
            let skip = pin.rows - pin.shown;
            let lines: Vec<Line<'static>> = (skip..pin.rows)
                .filter_map(|k| {
                    let at = if k == 0 {
                        pin.first
                    } else if k + 1 == pin.rows {
                        pin.last
                    } else {
                        pin.first + k
                    };
                    let line = cache.lines.get(at)?.clone();
                    Some(if k + 1 == pin.rows {
                        crate::sticky::indicator(
                            line,
                            crate::sticky::above(&pin, offset),
                            p.border_strong,
                            p.muted,
                            crate::transcript::look::glyphs().ascii,
                        )
                    } else if pin.clipped && k + 2 == pin.rows {
                        crate::sticky::ellipsize(line)
                    } else {
                        line
                    })
                })
                .collect();
            let rect = Rect::new(
                r.transcript.x,
                r.transcript.y,
                r.transcript
                    .width
                    .saturating_sub(u16::from(reach > 0 && r.transcript.width > 2)),
                lines.len() as u16,
            );
            frame.render_widget(
                Block::default().style(Style::default().bg(p.background)),
                rect,
            );
            frame.render_widget(Paragraph::new(lines), rect);
            cache.sticky = Some((rect, cards[pin.card].0));
        }
    }
    cache.jump_hit = None;
    let below = cache.lines.len().saturating_sub(offset + height);
    if follow || below == 0 || s.dashboard.is_some() || !s.panel_title.is_empty() {
        cache.away_len = None;
    } else if let Some(row) = pill_rect.filter(|_| height > 0) {
        let away = *cache.away_len.get_or_insert(cache.lines.len());
        if let Some((text, fresh)) = crate::jump::label(below, away, cache.lines.len()) {
            let w = (text.width() as u16 + 2).min(row.width.saturating_sub(2));
            let rect = Rect::new(row.x + (row.width - w) / 2, row.y, w, 1);
            let tone = if fresh { p.purple } else { p.muted };
            frame.render_widget(
                Paragraph::new(Line::from(Span::styled(
                    format!(" {text} "),
                    Style::default().fg(tone).bg(p.element),
                ))),
                rect,
            );
            cache.jump_hit = Some(rect);
        }
    }
    // The Sessions dashboard replaces the transcript region while the host shows it.
    if let Some(dashboard) = &s.dashboard {
        sessions::draw(
            frame,
            r.transcript,
            dashboard,
            &mut cache.dashboard,
            &sessions::Colors {
                text: p.text,
                muted: p.muted,
                quiet: p.quiet,
                accent: p.accent,
                panel: p.composer,
                selected: p.accent,
                hover: p.element_hi,
                border: p.border,
                border_strong: p.border_strong,
                purple: p.purple,
                warning: p.warning,
                mono: cache.mono,
            },
            spin,
        );
    }
    if s.dashboard.is_none() {
        let rows = composer_rows(r.composer, s);
        let shell = is_shell_draft(draft);
        // The composer draws on its own background; `panel` stays for other surfaces.
        let cp = Palette {
            panel: p.composer,
            ..p.clone()
        };
        // The chat input card keeps its existing padding, without a colour rail.
        let box_area = Rect {
            x: rows[1].x + 2,
            width: rows[1].width.saturating_sub(4),
            y: rows[1].y,
            height: rows[1].height + 2, // editor, controls and one row of bottom padding
        };
        if !s.agent_page.is_empty() {
            frame.render_widget(
                Paragraph::new("Sub agent · read-only · Esc or ↑ returns to the parent")
                    .style(Style::default().fg(p.muted).bg(cp.panel))
                    .block(Block::default().padding(ratatui::widgets::Padding::new(2, 2, 1, 0))),
                r.composer,
            );
        } else {
            frame.render_widget(
                Block::default().style(Style::default().bg(cp.panel)),
                box_area,
            );
            let half = box_area.bottom().saturating_sub(1);
            if box_area.height > 2 {
                let width = box_area.width;
                frame.render_widget(
                    Paragraph::new("▀".repeat(usize::from(width)))
                        .style(Style::default().fg(cp.panel).bg(p.background)),
                    Rect {
                        x: box_area.x,
                        y: half,
                        width,
                        height: 1,
                    },
                );
            }
            let editor_area = Rect {
                x: box_area.x + 3,
                y: rows[1].y + 1,
                width: box_area.width.saturating_sub(5),
                // One row of top padding and one blank row above the agent/model controls.
                height: rows[1].height.saturating_sub(2).max(1),
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
            if displayed.text == "!" && draft.cursor == 1 {
                frame.render_widget(
                    Paragraph::new(Line::from(vec![
                        Span::styled(
                            "!",
                            Style::default().fg(p.warning).add_modifier(Modifier::BOLD),
                        ),
                        Span::styled("type a bash command", Style::default().fg(p.quiet)),
                    ]))
                    .style(Style::default().bg(cp.panel)),
                    editor_area,
                );
            } else if displayed.text.is_empty() {
                frame.render_widget(
                    Paragraph::new(Line::from(vec![
                        Span::styled("T", Style::default().bg(p.text).fg(p.background)),
                        Span::styled("ype a message…", Style::default().fg(p.quiet)),
                    ]))
                    .style(Style::default().bg(cp.panel)),
                    editor_area,
                );
            } else {
                frame.render_widget(
                    Paragraph::new(editor_view(
                        &displayed,
                        false,
                        &cp,
                        editor_area.width,
                        editor_area.height,
                    ))
                    .style(Style::default().bg(cp.panel)),
                    editor_area,
                );
            }
            // Top to bottom above the card: status line, Queued section, a blank margin.
            // Pending attachments are the highlighted `[Image N]` chips inside the editor.
            draw_above_card(
                frame,
                r.composer,
                s,
                &cp,
                cache.activity_frame,
                cache.animate,
                cache.mono,
                cache.pointer,
                &cache.queue_flash,
                &cache.queue_ui,
            );
            if shell {
                // The blank row between the text and the controls names the mode.
                frame.render_widget(
                    Paragraph::new(Line::from(vec![
                        Span::styled(
                            " ! bash ",
                            Style::default()
                                .fg(p.background)
                                .bg(p.warning)
                                .add_modifier(Modifier::BOLD),
                        ),
                        Span::styled(SHELL_HINT, Style::default().fg(p.warning)),
                    ]))
                    .style(Style::default().bg(cp.panel)),
                    Rect {
                        x: box_area.x + 3,
                        y: rows[2].y.saturating_sub(1),
                        width: box_area.width.saturating_sub(5),
                        height: 1,
                    },
                );
            }
            let controls = Rect {
                x: box_area.x + 3,
                y: rows[2].y,
                width: box_area.width.saturating_sub(5),
                height: 1,
            };
            frame.render_widget(
                Paragraph::new(Line::from(chrome::control_spans(
                    s,
                    &cp,
                    controls.width as usize,
                    &cache.component_hover,
                    std::time::Instant::now(),
                )))
                .style(Style::default().bg(cp.panel)),
                controls,
            );
            let meter_area = inset(r.bottom_bar, 2, 2);
            let spans = activity_meter_for(
                s,
                &p,
                usize::from(meter_area.width),
                cache.activity_frame,
                cache.animate,
                cache.mono,
            );
            let spans = overlay_activity_recording(
                spans,
                &s.voice_phase,
                &p,
                spin,
                cache.voice_elapsed,
                cache.animate,
                nexus_widgets::Glyphs::from_env().ascii,
            );
            frame.render_widget(Paragraph::new(Line::from(spans)), meter_area);
        }
    }
    // While the panel closes the call is already over: the ghost of its last state stays drawn
    // (as `ended`) until the rows are gone (`live_anim`).
    let ghost = if s.live.active {
        None
    } else {
        cache.live_anim.ghost().cloned()
    };
    let live = ghost.as_ref().unwrap_or(&s.live);
    cache.mic.observe(live, cache.spin, cache.activity_frame);
    cache.live_button = None;
    if r.live.height > 0 && s.dashboard.is_none() && r.live.width > 0 {
        draw_live_panel(frame, s, live, &r, &p, spin, cache);
    } else {
        cache.live_back = 0;
        cache.live_max_back = 0;
    }
    if r.details.width > 0 {
        // Tabs, rule, body and hint share one pure layout with the mouse handlers
        // (`details_sidebar::geometry`); the rows are cached per body width.
        let (hot, mono) = (cache.details.hot(), cache.mono);
        let width = crate::details_sidebar::geometry(r.details, s, &p).map_or(0, |g| g.body.width);
        let rows = cache.detail_rows(s, &p, width);
        crate::details_sidebar::draw(
            frame,
            r.details,
            s,
            &p,
            rows,
            details_scroll,
            logs_scroll,
            hot,
            mono,
        );
        if matches!(s.details_panel.tab.as_str(), "Browser" | "Git") {
            if let Some(g) = crate::details_sidebar::geometry(r.details, s, &p) {
                let browser = crate::sidebar::details_snapshot(s);
                if s.details_panel.tab == "Browser" {
                    cache.sidebar.draw_files(frame, g.body, &browser, &p, mono);
                } else {
                    cache.sidebar.draw_git(frame, g.body, &browser, &p);
                }
            }
        }
    }
    if r.sidebar.width > 0 && s.panel_title.is_empty() && s.prompt.is_none() && s.form.is_none() {
        dim_backdrop(frame, &p);
        cache.sidebar.draw(frame, r.sidebar, s, &p, spin, cache.mono);
    }
    if !s.panel_title.is_empty() || s.prompt.is_some() {
        // One dimmed backdrop under every modal kind (panels, Settings, pickers, prompts).
        dim_backdrop(frame, &p);
    }
    if s.panel_title.is_empty() || s.panel_format != "image" || s.preview_image.is_empty() {
        cache.forget_image();
    }
    if !s.panel_title.is_empty() {
        let area = panel_area(panel_host(&r, s), s);
        let title = if s.panel_loading {
            format!(
                "{} {} · refreshing",
                SPINNER[spin % SPINNER.len()],
                s.panel_title
            )
        } else {
            s.panel_title.clone()
        };
        // Settings: one root list; drilled pages say `Settings › Area` in the frame title.
        cache.settings_root.track(s.nav.is_some());
        let settings_root = s.nav.is_some() && cache.settings_root.active;
        let title = match &s.nav {
            Some(nav) if !settings_root => {
                let area_label = usize::try_from(nav.selected)
                    .ok()
                    .and_then(|at| nav.items.get(at))
                    .map(|item| item.0.as_str())
                    .unwrap_or("");
                format!("Settings › {area_label}")
            }
            Some(_) => "Settings".to_string(),
            None => title,
        };
        let inner = dialog_frame(frame, area, &title, &p, s.panel_layout == "drawer");
        cache.appearance_rows = Rect::default();
        if let (true, Some(nav)) = (settings_root, &s.nav) {
            let rows = crate::settings_page::root::build(
                nav,
                &crate::config::checkboxes(&cache.appearance),
                &cache.settings_root.query,
                s,
            );
            crate::settings_page::root::draw(frame, inner, &mut cache.settings_root, &rows, &p);
        } else if s.panel_format == "image" && !s.preview_image.is_empty() {
            let areas = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(inner);
            frame.render_widget(
                Paragraph::new(s.panel_lines.join("\n")).style(Style::default().fg(p.text)),
                areas[0],
            );
            if cache.image_screen != frame.area() {
                cache.forget_image();
                cache.image_screen = frame.area();
            }
            cache.image_area = areas[1];
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
                        // Detected once at startup (graphics.rs); never queried here,
                        // where the input reader already owns the terminal.
                        crate::graphics::picker().new_resize_protocol(image)
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
        } else if let Some(raw) = s.settings_page.as_ref() {
            let page = cache.page_for(s.revision, raw);
            let mut state = std::mem::take(&mut cache.page_state);
            crate::settings_page::draw(
                frame,
                inner,
                &page,
                &mut state,
                &p,
                s.theme == "nexus-light",
            );
            cache.page_state = state;
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
        // A structured permission (`title`) draws the P-PRM layout; anything else keeps `lines`.
        let structured = !prompt_rows(prompt).is_empty();
        let title_rect = Rect {
            x: area.x + 2,
            y: area.y,
            width: area.width.saturating_sub(4),
            height: 1,
        };
        if structured {
            frame.render_widget(
                Paragraph::new(prompt_title_line(prompt, &p)).style(Style::default().bg(p.panel)),
                title_rect,
            );
        } else {
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
                title_rect,
            );
        }
        let (content_area, choices_area) = prompt_regions(area, prompt.choices.len());
        let content_lines: Vec<Line> = if structured {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0.0, |d| d.as_secs_f64());
            prompt_body_lines(prompt, &p, now)
        } else {
            let mut content = Cache::default();
            content.update(&prompt.lines, content_area.width, &p);
            content.lines.iter().cloned().collect()
        };
        frame.render_widget(
            Paragraph::new(
                content_lines
                    .into_iter()
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
                if structured {
                    let chip = prompt_choice_line(c, on, &p);
                    let style = components::selectable(
                        &p,
                        components::State {
                            disabled: c.disabled,
                            selected: false,
                            hover: cache.component_hover.amount_id(
                                components::HoverId::PromptChoice(i),
                                std::time::Instant::now(),
                            ),
                            ..Default::default()
                        },
                    );
                    return Line::from(chip).style(style);
                }
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
    // Toasts float above everything, including dialogs; they never take layout.
    cache
        .toasts
        .notes
        .draw(frame, &p, r.tabs, std::time::Instant::now(), cache.animate);
    cache.toasts.draw(frame, s, &p, r.transcript);
    if cache.mono {
        // Monochrome preference: drop every foreground/background colour, keep modifiers.
        // The preview image is the exception: half blocks and Kitty placeholders carry the
        // picture in cell colours, so stripping them leaves a blank panel.
        let keep = cache.image_area;
        let buffer = frame.buffer_mut();
        let (left, top, width) = (buffer.area.x, buffer.area.y, usize::from(buffer.area.width));
        for (index, cell) in buffer.content.iter_mut().enumerate() {
            let at = ratatui::layout::Position::new(
                left + (index % width) as u16,
                top + (index / width) as u16,
            );
            if keep.contains(at) {
                continue;
            }
            cell.set_fg(ratatui::style::Color::Reset);
            cell.set_bg(ratatui::style::Color::Reset);
        }
    }
    r
}

/// The Settings list has the Appearance area selected (it gets the local checkbox overlay).
pub fn appearance_selected(s: &Snapshot) -> bool {
    s.nav.as_ref().is_some_and(|nav| {
        usize::try_from(nav.selected)
            .ok()
            .and_then(|at| nav.items.get(at))
            .is_some_and(|item| !item.2 && item.1 == "appearance")
    })
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
    fn open_modals_dim_the_whole_frame_behind_them() {
        let p = Palette::new(false);
        let mut cache = Cache::default();
        let paint = |s: &Snapshot, cache: &mut Cache| {
            let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
            terminal
                .draw(|frame| {
                    draw(
                        frame,
                        s,
                        &Editor::default(),
                        &Editor::default(),
                        &Editor::default(),
                        "",
                        0,
                        0,
                        0,
                        cache,
                        true,
                        false,
                        false,
                        false,
                        0,
                        0,
                    );
                })
                .unwrap();
            terminal.backend().buffer().clone()
        };
        let mut s = Snapshot::default();
        // Top-left cell of the top bar: page background, never covered by a modal.
        let open = paint(&s, &mut cache);
        assert_eq!(open[(0, 0)].bg, p.background);
        s.panel_title = "Model".into();
        s.panel_lines = vec!["x".into()];
        let dimmed = paint(&s, &mut cache);
        assert_eq!(
            dimmed[(0, 0)].bg,
            p.background,
            "blending the background with itself"
        );
        // Text on the backdrop recedes toward the background.
        let fg = |b: &ratatui::buffer::Buffer| b[(2, 0)].fg;
        assert_ne!(fg(&open), fg(&dimmed));
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
        s.details_panel.tab = "Files".into();
        let (lines, files) = crate::details_sidebar::rows(&s, &Palette::new(false), 40);
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
    fn usage_meter_rows_colour_only_the_bar_and_the_reserve() {
        let p = Palette::new(false);
        let lines = vec!["  Weekly  ━━━┃───  20% used · 30% in reserve · resets in 3d".to_string()];
        let rows = toned_lines(&lines, &["meter-ok".to_string()], 120, &p);
        let spans = &rows[0].spans;
        let find = |text: &str| spans.iter().find(|s| s.content.contains(text)).unwrap();
        assert_eq!(
            find("Weekly").style.fg,
            Some(p.text),
            "the label is neutral"
        );
        assert_eq!(
            find("━━━").style.fg,
            Some(p.success),
            "used cells take the tone"
        );
        assert_eq!(find("┃").style.fg, Some(p.text), "the pace marker is plain");
        assert_eq!(
            find("───").style.fg,
            Some(p.border_strong),
            "the track is dim"
        );
        assert_eq!(find("in reserve").style.fg, Some(p.success));
        assert_eq!(find("resets").style.fg, Some(p.muted));
        let text: String = spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(text.trim_start(), lines[0].trim_start(), "no text is lost");
        let over = vec!["  Monthly  ━━━━  80% used · 30% over pace".to_string()];
        let rows = toned_lines(&over, &["meter-warn".to_string()], 120, &p);
        let over_span = rows[0]
            .spans
            .iter()
            .find(|s| s.content.contains("over pace"))
            .unwrap();
        assert_eq!(over_span.style.fg, Some(p.warning));
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
        let mut terminal = Terminal::new(TestBackend::new(100, 50)).unwrap();
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
                );
            })
            .unwrap();
        let row_of = |needle: &str| {
            (0..50u16)
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
            row_of("Layout") > row_of("General") && row_of("Providers") > row_of("Configure"),
            "the root list shows every area under its section header"
        );
        assert!(
            (0..50u16).all(|y| !(0..100u16)
                .map(|x| terminal.backend().buffer()[(x, y)].symbol().to_string())
                .collect::<String>()
                .contains("Panels hide")),
            "a drilled page is not drawn while the root list is showing"
        );
        let _ = col_of;
    }
    #[test]
    fn scrolling_down_through_a_long_transcript_reaches_the_bottom_and_the_thumb_agrees() {
        let blocks: Vec<_> = (0..200)
            .map(|i| serde_json::json!({"id": format!("m{i}"), "kind": "markdown", "text": format!("line {i}"), "gap": 1}))
            .collect();
        let s: Snapshot = serde_json::from_value(serde_json::json!({
            "schema": 1, "revision": 1, "title": "T", "status": "idle", "blocks": blocks,
        }))
        .unwrap();
        let mut terminal = Terminal::new(TestBackend::new(80, 40)).unwrap();
        let mut cache = Cache::default();
        let frame_at = |terminal: &mut Terminal<TestBackend>,
                        cache: &mut Cache,
                        scroll: usize,
                        follow: bool| {
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
                        scroll,
                        0,
                        cache,
                        follow,
                        false,
                        false,
                        false,
                        0,
                        0,
                    );
                })
                .unwrap();
        };
        let (mut scroll, mut follow) = (0usize, false);
        frame_at(&mut terminal, &mut cache, scroll, follow);
        let mut last = scroll;
        for _ in 0..400 {
            (scroll, follow) = crate::session::scroll_down(scroll, 3, cache.live_scroll, follow);
            frame_at(&mut terminal, &mut cache, scroll, follow);
            assert!(
                cache.current_offset >= last,
                "scrolling down never moves up"
            );
            last = cache.current_offset;
            scroll = cache.current_offset;
        }
        assert!(follow, "the bottom resumes following");
        assert_eq!(cache.current_offset, cache.live_scroll);
        assert!(cache.live_scroll > 100);
        // The thumb touches the bottom of the track: nothing is left below.
        let x = 79u16;
        let thumb_rows: Vec<u16> = (0..40u16)
            .filter(|&y| terminal.backend().buffer()[(x, y)].symbol() == "▐")
            .collect();
        let bottom = *thumb_rows.last().expect("a scrollbar thumb");
        let track_bottom = (0..40u16)
            .rev()
            .find(|&y| {
                let sym = terminal.backend().buffer()[(x, y)].symbol().to_string();
                sym == "▐" || sym == " "
            })
            .unwrap();
        assert!(
            track_bottom - bottom <= 12,
            "thumb at {bottom}, track ends {track_bottom}"
        );
    }
    #[test]
    fn a_long_transcript_scrolls_exactly_the_buffer_past_the_last_row() {
        let blocks: Vec<_> = (0..200)
            .map(|i| serde_json::json!({"id": format!("m{i}"), "kind": "markdown", "text": format!("line {i}"), "gap": 1}))
            .collect();
        let s: Snapshot = serde_json::from_value(serde_json::json!({
            "schema": 1, "revision": 1, "title": "T", "status": "idle", "blocks": blocks,
        }))
        .unwrap();
        let mut terminal = Terminal::new(TestBackend::new(80, 40)).unwrap();
        let mut cache = Cache::default();
        let mut frame_at = |scroll: usize, follow: bool, cache: &mut Cache| {
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
                        scroll,
                        0,
                        cache,
                        follow,
                        false,
                        false,
                        false,
                        0,
                        0,
                    );
                })
                .unwrap();
            terminal.backend().buffer().clone()
        };
        let (mut scroll, mut follow) = (0usize, false);
        frame_at(scroll, follow, &mut cache);
        for _ in 0..400 {
            (scroll, follow) = crate::session::scroll_down(scroll, 3, cache.live_scroll, follow);
            frame_at(scroll, follow, &mut cache);
            scroll = cache.current_offset;
        }
        let content_bottom = cache.lines.len();
        let viewport = content_bottom - (cache.live_scroll - crate::session::BOTTOM_BUFFER);
        // The offset is exactly BUFFER rows past the offset that shows the last content row.
        assert_eq!(
            cache.current_offset,
            content_bottom - viewport + crate::session::BOTTOM_BUFFER
        );
        assert!(follow, "the bottom resumes following");
        assert_eq!(cache.current_offset, cache.live_scroll);
        let buf = frame_at(scroll, follow, &mut cache);
        let x = 79u16;
        let thumb: Vec<u16> = (0..40u16)
            .filter(|&y| buf[(x, y)].symbol() == "▐")
            .collect();
        let area = ratatui::layout::Rect::new(0, 0, 80, 40);
        let tr = regions(
            area,
            &s,
            composer_height(area, &Editor::default(), &s),
            false,
        )
        .transcript;
        assert_eq!(
            thumb.last().copied().unwrap(),
            tr.bottom() - 1,
            "thumb at the track end"
        );
        // Further scrolling is a no-op.
        let (again, f2) = crate::session::scroll_down(scroll, 10, cache.live_scroll, follow);
        assert_eq!((again, f2), (scroll, true));
        frame_at(again, f2, &mut cache);
        assert_eq!(cache.current_offset, scroll);
    }
    #[test]
    fn a_pill_offers_the_jump_to_latest_only_while_scrolled_up() {
        let blocks: Vec<_> = (0..30)
            .map(|i| serde_json::json!({"id": format!("m{i}"), "kind": "markdown", "text": format!("line {i}"), "gap": 1}))
            .collect();
        let s: Snapshot = serde_json::from_value(serde_json::json!({
            "schema": 1, "revision": 1, "title": "T", "status": "idle", "blocks": blocks,
        }))
        .unwrap();
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let mut cache = Cache::default();
        let frame_at = |terminal: &mut Terminal<TestBackend>,
                        scroll: usize,
                        follow: bool,
                        cache: &mut Cache| {
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
                        scroll,
                        0,
                        cache,
                        follow,
                        false,
                        false,
                        false,
                        0,
                        0,
                    );
                })
                .unwrap();
        };
        frame_at(&mut terminal, 0, true, &mut cache);
        assert!(cache.jump_hit.is_none(), "following the bottom");
        frame_at(&mut terminal, 0, false, &mut cache);
        let rect = cache.jump_hit.expect("scrolled up");
        let bottom = regions(
            Rect::new(0, 0, 80, 24),
            &s,
            composer_height(Rect::new(0, 0, 80, 24), &Editor::default(), &s),
            false,
        )
        .transcript
        .bottom();
        assert_eq!(rect.bottom(), bottom, "on the transcript's last row");
        // The pill's row is reserved: the last content row sits just above it.
        let row_text = |y: u16| -> String {
            (0..80u16)
                .map(|x| terminal.backend().buffer()[(x, y)].symbol().to_string())
                .collect()
        };
        // Blocks are separated by a blank gap row, so the row just above the pill is either the
        // last text row or that gap, depending on the viewport height's parity.
        assert!(
            row_text(rect.y - 1).contains("line ") || row_text(rect.y - 2).contains("line "),
            "content ends right above the pill"
        );
        assert!(
            !row_text(rect.y).contains("line "),
            "no transcript text shares the pill row"
        );
        let text: String = (rect.x..rect.right())
            .map(|x| {
                terminal.backend().buffer()[(x, rect.y)]
                    .symbol()
                    .to_string()
            })
            .collect();
        assert!(text.contains("more lines"), "{text}");
        frame_at(&mut terminal, 1000, false, &mut cache);
        assert!(cache.jump_hit.is_none(), "clamped to the bottom");
    }
    #[test]
    fn a_scrolled_turn_pins_its_user_message_and_the_top_bar_has_spacing() {
        let mut blocks = Vec::new();
        for turn in 1..=3 {
            blocks.push(serde_json::json!({"id": format!("u{turn}"), "kind": "user", "turn_id": format!("t{turn}"), "title": format!("question number {turn}"), "number": turn}));
            blocks.push(serde_json::json!({"id": format!("m{turn}"), "kind": "markdown", "text": (0..12).map(|i| format!("answer {turn} line {i}")).collect::<Vec<_>>().join("\n\n")}));
        }
        let s: Snapshot = serde_json::from_value(serde_json::json!({
            "schema": 1, "revision": 1, "title": "T", "status": "idle", "breadcrumb": "~/repo:main", "blocks": blocks,
        }))
        .unwrap();
        let bgs = std::cell::RefCell::new(Vec::new());
        let screen = |scroll: usize, cache: &mut Cache| {
            let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
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
                        scroll,
                        0,
                        cache,
                        false,
                        false,
                        false,
                        false,
                        0,
                        0,
                    );
                })
                .unwrap();
            *bgs.borrow_mut() = (0..24u16)
                .map(|y| terminal.backend().buffer()[(5, y)].bg)
                .collect();
            (0..24u16)
                .map(|y| {
                    (0..80u16)
                        .map(|x| terminal.backend().buffer()[(x, y)].symbol().to_string())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
        };
        let mut cache = Cache::default();
        let top = screen(0, &mut cache);
        assert!(top[0].trim().is_empty(), "spacing row above the top bar");
        assert!(
            !top[1].trim().is_empty() && !top[3].trim().is_empty(),
            "tabs on row 1 and the workspace row on row 3"
        );
        for y in [2, 4] {
            assert!(
                top[y].contains('─') && top[y].trim_matches(|c| c == ' ' || c == '─').is_empty(),
                "thin line on row {y} (between the tabs and the crumbs, and closing the bar): {:?}",
                top[y]
            );
        }
        assert!(cache.sticky.is_none(), "at the top there is no pin");
        let mid = screen(10, &mut cache);
        let (rect, row) = cache.sticky.expect("pinned card");
        // The transcript starts right after the bar's spacing row.
        let t = 5usize;
        assert_eq!(rect.y as usize, t, "pinned at the top of the transcript");
        assert!(mid[t + 1].contains("question number 1"), "{:?}", &mid[..9]);
        assert!(
            mid[t].chars().take(78).all(|c| c == ' '),
            "top padding row of the band"
        );
        assert!(
            mid[t + 2].starts_with('░')
                && mid[t + 2].contains("of this turn above")
                && !mid[t + 2].contains("question"),
            "shaded bottom padding row of the pinned band, counting the turn rows above: {:?}",
            mid[t + 2]
        );
        assert_eq!(rect.height, 3, "padding + one text row + padding");
        let band = Palette::new(false).element;
        for y in t as u16..t as u16 + 2 {
            assert_eq!(
                bgs.borrow()[y as usize],
                band,
                "row {y} is on the element band"
            );
        }
        assert_eq!(row, cache.prompt_top("t1").unwrap());
        let later = screen(cache.prompt_top("t2").unwrap() + 3, &mut cache);
        assert!(
            later[t + 1].contains("question number 2"),
            "{:?}",
            &later[..9]
        );
        assert_eq!(
            later
                .iter()
                .filter(|l| l.contains("question number 2"))
                .count(),
            1
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
            "details_panel": {"tab": "Files", "session": [["Status", "running"]], "files": [{"path": "src/parser.py", "added": 1, "removed": 1}], "files_summary": "+1 -1 across 1 file"},
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
                // Below 100 columns the details drawer leaves the conversation 24 columns, which
                // truncates the agent name ("B…"); the full name needs the wider layouts.
                assert!(
                    screen.contains(if width < 100 { "B…" } else { "Build" }),
                    "{at}: the runtime row"
                );
                if width >= 170 {
                    assert!(
                        screen.contains("Fix the parser"),
                        "{at}: active session tab"
                    );
                    assert!(!screen.contains('‹'), "{at}: no session arrows");
                    assert!(screen.contains('▤'), "{at}: dashboard button");
                }
                assert!(!screen.contains("SESSIONS"), "{at}: no sessions sidebar");
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
        cache.animate = true;
        cache.voice_levels = vec![0.0, 0.5, 1.0];
        cache.voice_elapsed = 65;
        cache.voice_text = s.voice_preview.clone();
        cache.spin = 3;
        let p = Palette::new(false);
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
                );
            })
            .unwrap();

        let area = Rect::new(0, 0, 100, 24);
        let r = regions(
            area,
            &s,
            composer_height(area, &Editor::default(), &s),
            false,
        );
        let rows = composer_rows(r.composer, &s);
        let meter_area = inset(r.bottom_bar, 2, 2);

        // (a) no '■' inside the composer card area (rows[0]..=rows[3])
        for y in r.composer.y..r.bottom_bar.y {
            for x in r.composer.x..r.composer.right() {
                assert_ne!(
                    terminal.backend().buffer()[(x, y)].symbol(),
                    "■",
                    "composer area must not contain recording square at ({x}, {y})"
                );
            }
        }

        // (b) rule row contains '■' at cell 2 and the time at cells 4-8
        let rule_cell = |cell_idx: u16| {
            terminal.backend().buffer()[(meter_area.x + cell_idx, meter_area.y)]
                .symbol()
                .to_string()
        };
        assert_eq!(rule_cell(2), "■", "cell 2 of rule row must be ■");
        let time_cells: String = (4..=8).map(rule_cell).collect();
        assert_eq!(
            time_cells, "01:05",
            "cells 4-8 must show mm:ss (65s -> 01:05)"
        );

        // (c) cell 3 is still a rule glyph
        assert_eq!(rule_cell(3), "─", "cell 3 must still be a rule glyph");

        // (d) both blink colours are red-family (p.error or its mix)
        let fg_bright = terminal.backend().buffer()[(meter_area.x + 2, meter_area.y)].fg;
        assert_eq!(fg_bright, p.error, "spin 3 should be p.error");

        // Spin 4 triggers the dim blink phase
        cache.spin = 4;
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
                );
            })
            .unwrap();
        let fg_dim = terminal.backend().buffer()[(meter_area.x + 2, meter_area.y)].fg;
        let expected_dim = nexus_widgets::theme::mix(p.error, p.panel, 0.55);
        assert_eq!(fg_dim, expected_dim, "spin 4 should be mixed red");

        let is_red = |c: Color| match c {
            Color::Rgb(r, g, b) => r > g && r > b,
            _ => false,
        };
        assert!(
            is_red(fg_bright),
            "bright blink must be red-family: {fg_bright:?}"
        );
        assert!(is_red(fg_dim), "dim blink must be red-family: {fg_dim:?}");
        assert_ne!(fg_bright, p.accent, "blink must never be accent");
        assert_ne!(fg_dim, p.accent, "blink must never be accent");
        assert_ne!(fg_bright, p.quiet, "blink must never be quiet");
        assert_ne!(fg_dim, p.quiet, "blink must never be quiet");
    }

    #[test]
    fn activity_meter_pure_postpass_recording_and_transcribing() {
        let p = Palette::new(false);
        let s = Snapshot {
            context_used: Some(60),
            context_window: Some(100),
            ..Default::default()
        };
        let original_spans = activity_meter(&s, &p, 15, 0);

        // While transcribing, neither mark is shown (composer preview already shows it)
        let transcribing = overlay_activity_recording(
            original_spans.clone(),
            "transcribing",
            &p,
            0,
            7,
            true,
            false,
        );
        assert_eq!(transcribing, original_spans);

        // While idle, neither mark is shown
        let idle = overlay_activity_recording(original_spans.clone(), "", &p, 0, 7, true, false);
        assert_eq!(idle, original_spans);

        // While recording: square at cell 2, rule at cell 3, mm:ss at cells 4-8
        let recording =
            overlay_activity_recording(original_spans.clone(), "recording", &p, 0, 7, true, false);
        assert_eq!(recording.len(), 15);
        assert_eq!(recording[0].content, "─");
        assert_eq!(recording[1].content, "─");
        assert_eq!(recording[2].content, "■");
        assert_eq!(recording[2].style.fg, Some(p.error));
        assert_eq!(recording[3].content, "─");
        assert_eq!(
            recording[3].style.fg, original_spans[3].style.fg,
            "cell 3 keeps original tone"
        );

        let time_str: String = recording[4..=8]
            .iter()
            .map(|s| s.content.as_ref())
            .collect();
        assert_eq!(time_str, "00:07");
        for span in &recording[4..=8] {
            assert_eq!(span.style.fg, Some(p.error));
        }

        // Remaining cells (9..15) keep their original rule glyph and tones
        for i in 9..15 {
            assert_eq!(recording[i].content, "─");
            assert_eq!(recording[i].style.fg, original_spans[i].style.fg);
        }
    }

    #[test]
    fn activity_meter_overlay_animation_off_is_static() {
        let p = Palette::new(false);
        let s = Snapshot::default();
        let spans = activity_meter(&s, &p, 15, 0);

        // With animate=false, even at spin=4 (where it would dim), the square stays p.error
        let static_spans = overlay_activity_recording(
            spans,
            "recording",
            &p,
            4,
            7,
            false, // animate off
            false,
        );
        assert_eq!(static_spans[2].content, "■");
        assert_eq!(static_spans[2].style.fg, Some(p.error));
    }

    #[test]
    fn activity_meter_overlay_ascii_fallback() {
        let p = Palette::new(false);
        let s = Snapshot::default();
        let spans = activity_meter(&s, &p, 15, 0);

        // With ascii=true, square is '#'
        let ascii_spans = overlay_activity_recording(
            spans,
            "recording",
            &p,
            0,
            7,
            true,
            true, // ascii
        );
        assert_eq!(ascii_spans[2].content, "#");
        assert_eq!(ascii_spans[2].style.fg, Some(p.error));
        assert_eq!(ascii_spans[3].content, "─");
        let time_str: String = ascii_spans[4..=8]
            .iter()
            .map(|s| s.content.as_ref())
            .collect();
        assert_eq!(time_str, "00:07");
    }

    #[test]
    fn activity_meter_overlay_narrow_widths() {
        let p = Palette::new(false);
        let s = Snapshot::default();

        for width in [0usize, 1, 2, 3, 4, 5, 8, 9, 10, 15, 80] {
            let spans = activity_meter(&s, &p, width, 0);
            let res = overlay_activity_recording(spans, "recording", &p, 0, 65, true, false);
            assert_eq!(
                res.len(),
                width,
                "spans count must equal width for width={width}"
            );
            let total_chars: usize = res.iter().map(|s| s.content.chars().count()).sum();
            assert_eq!(
                total_chars, width,
                "total chars must equal width for width={width}"
            );

            if width < 3 {
                // Not enough room for square at cell 2
                assert!(res.iter().all(|s| s.content == "─"));
            } else if width < 4 {
                // Width 3: cell 2 is square
                assert_eq!(res[2].content, "■");
            } else if width < 9 {
                // Width 4..8: cell 2 is square, cell 3 is rule, remaining cells are rule
                assert_eq!(res[2].content, "■");
                assert_eq!(res[3].content, "─");
                for s in &res[4..] {
                    assert_eq!(s.content, "─");
                }
            } else {
                // Width >= 9: cell 2 is square, cell 3 is rule, cells 4-8 are mm:ss
                assert_eq!(res[2].content, "■");
                assert_eq!(res[3].content, "─");
                let time_str: String = res[4..=8].iter().map(|s| s.content.as_ref()).collect();
                assert_eq!(time_str, "01:05");
                for s in &res[9..] {
                    assert_eq!(s.content, "─");
                }
            }
        }
    }
    #[test]
    fn running_slot_is_replaced_per_frame() {
        let line = Line::from(vec![Span::raw(format!("{SPINNER_SLOT} Bash · ls"))]);
        assert_eq!(with_spinner(&line, 0).spans[0].content, "⠋ Bash · ls");
        assert_eq!(with_spinner(&line, 9).spans[0].content, "⠙ Bash · ls");
        assert_eq!(with_spinner(&line, 11).spans[0].content, "⢰ Bash · ls");
        let s = Snapshot::default();
        assert!(!animating(&s));
    }
    #[test]
    fn spinner_animates_four_rows_of_braille_dots_and_matches_completed_glyph() {
        // Unicode Braille pattern 8-dot matrix (2 columns x 4 rows):
        // Col 1: dot 1 (0x01), dot 2 (0x02), dot 3 (0x04), dot 7 (0x40)
        // Col 2: dot 4 (0x08), dot 5 (0x10), dot 6 (0x20), dot 8 (0x80)
        // Row 1: dots 1, 4 (0x01 | 0x08 = 0x09)
        // Row 2: dots 2, 5 (0x02 | 0x10 = 0x12)
        // Row 3: dots 3, 6 (0x04 | 0x20 = 0x24)
        // Row 4: dots 7, 8 (0x40 | 0x80 = 0xC0)
        const ROW1: u32 = 0x01 | 0x08;
        const ROW2: u32 = 0x02 | 0x10;
        const ROW3: u32 = 0x04 | 0x20;
        const ROW4: u32 = 0x40 | 0x80;

        assert_eq!(SPINNER.len(), 8, "8-dot perimeter frames has 8 steps");

        let mut row1_participates = false;
        let mut row2_participates = false;
        let mut row3_participates = false;
        let mut row4_participates = false;
        let mut union_dots: u32 = 0;

        for (i, glyph) in SPINNER.iter().enumerate() {
            let ch = glyph.chars().next().expect("glyph character");
            let pattern = (ch as u32).saturating_sub(0x2800);
            assert!(
                pattern <= 0xff,
                "frame {i} ({glyph}) must be a valid Braille pattern"
            );
            if pattern & ROW1 != 0 {
                row1_participates = true;
            }
            if pattern & ROW2 != 0 {
                row2_participates = true;
            }
            if pattern & ROW3 != 0 {
                row3_participates = true;
            }
            if pattern & ROW4 != 0 {
                row4_participates = true;
            }
            union_dots |= pattern;

            // Existing replacement mechanism works for every frame
            let line = Line::from(vec![Span::raw(format!("{SPINNER_SLOT} subagent"))]);
            let rendered = with_spinner(&line, i);
            assert_eq!(rendered.spans[0].content, format!("{glyph} subagent"));
        }

        assert!(row1_participates, "row 1 dots must participate");
        assert!(row2_participates, "row 2 dots must participate");
        assert!(row3_participates, "row 3 dots must participate");
        assert!(
            row4_participates,
            "fourth-row dots (dots 7 and 8) must participate"
        );

        // The union of dots across all 8 perimeter frames covers all 4 rows (2x4),
        // exactly matching the completed subagent cell '⣿' (U+28FF).
        assert_eq!(union_dots, 0xff);
        assert_eq!(char::from_u32(0x2800 + union_dots), Some('⣿'));
    }
    #[test]
    fn composer_grows_with_content_and_is_capped() {
        let area = Rect::new(0, 0, 80, 60);
        let mut draft = Editor::default();
        assert_eq!(composer_height(area, &draft, &Snapshot::default()), 7);
        draft.insert(&"line\n".repeat(9));
        assert_eq!(composer_height(area, &draft, &Snapshot::default()), 6 + 10);
        draft.insert(&"line\n".repeat(60));
        assert_eq!(composer_height(area, &draft, &Snapshot::default()), 6 + 22);
        assert_eq!(
            composer_height(Rect::new(0, 0, 80, 14), &draft, &Snapshot::default()),
            7,
            "the transcript keeps its rows with a two-row top bar"
        );
        let mut queued = Snapshot::default();
        queued.queue_items = vec![queue_item("q1", "Q", "a"), queue_item("s1", "S", "b")];
        assert_eq!(
            composer_height(Rect::new(0, 0, 80, 60), &Editor::default(), &queued),
            7 + 3,
            "a steering and a queued message each get a section header and a row"
        );
        queued.turn_status = Some(crate::bridge::TurnStatus {
            activity: "Thinking".into(),
            ..Default::default()
        });
        assert_eq!(
            composer_height(Rect::new(0, 0, 80, 60), &Editor::default(), &queued),
            7 + 3 + 2,
            "a running turn adds the status line and a blank separator row"
        );
    }
    #[test]
    fn activity_meter_sits_directly_under_the_composer_card() {
        for width in [60, 120, 180] {
            for details_sidebar in [false, true] {
                for recording in [false, true] {
                    let s = Snapshot {
                        title: "Test".into(),
                        breadcrumb: "/workspace · main · worktree".into(),
                        agent: "Agent".into(),
                        details_sidebar,
                        voice_phase: if recording {
                            "recording".into()
                        } else {
                            "".into()
                        },
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
                    assert_eq!(rows.len(), 4, "no spacer row under the card");
                    assert_eq!(rows[3].bottom(), r.composer.bottom());
                    assert_eq!(r.composer.bottom(), r.bottom_bar.y);
                    assert_eq!(r.bottom_bar.height, 1);
                    let mut terminal = Terminal::new(TestBackend::new(width, 40)).unwrap();
                    let mut cache = Cache::default();
                    cache.voice_elapsed = 7;
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
                            );
                        })
                        .unwrap();
                    let meter_area = inset(r.bottom_bar, 2, 2);
                    let buffer = terminal.backend().buffer();
                    if recording {
                        assert_eq!(buffer[(meter_area.x + 2, r.bottom_bar.y)].symbol(), "■");
                        assert_eq!(buffer[(meter_area.x + 3, r.bottom_bar.y)].symbol(), "─");
                        let time: String = (4..=8)
                            .map(|i| buffer[(meter_area.x + i, r.bottom_bar.y)].symbol().to_string())
                            .collect();
                        assert_eq!(time, "00:07");
                    } else {
                        assert_eq!(buffer[(meter_area.x + 2, r.bottom_bar.y)].symbol(), "─");
                        assert_eq!(buffer[(meter_area.x + 3, r.bottom_bar.y)].symbol(), "─");
                    }
                    if r.tabs.height > 0 {
                        assert_eq!(r.tabs.height, 4);
                        let row = |y: u16| -> String {
                            (r.tabs.x..r.tabs.right())
                                .map(|x| terminal.backend().buffer()[(x, y)].symbol())
                                .collect()
                        };
                        let top = row(r.tabs.y);
                        if r.details.width == 0 {
                            assert!(top.contains('▤'), "dashboard button: {top}");
                        }
                        assert!(!top.contains("/workspace"), "path left the tab row: {top}");
                        let crumbs = row(r.tabs.y + 2);
                        assert!(
                            crumbs.contains("/workspace"),
                            "path under the tabs : {crumbs}"
                        );
                        // The narrow details drawer sits beside the bar, never over it; only
                        // the bar's own columns carry the rule.
                        let bar_right = if r.details.x > r.tabs.x && r.details.x < r.tabs.right() {
                            r.details.x
                        } else {
                            r.tabs.right()
                        };
                        let rule: String = (r.tabs.x..bar_right)
                            .map(|x| terminal.backend().buffer()[(x, r.tabs.y + 3)].symbol())
                            .collect();
                        assert!(
                            rule.contains('─')
                                && rule.trim_matches(|c| c == ' ' || c == '─').is_empty(),
                            "thin rule under the workspace row: {rule}"
                        );
                        assert_eq!(r.transcript.y, r.tabs.bottom(), "rule is the margin");
                    }
                    assert_eq!(
                        buffer[(rows[2].x + 2, rows[2].y)].symbol(),
                        " ",
                        "card has no left colour rail"
                    );
                    assert_eq!(buffer[(rows[2].x + 5, rows[2].y)].symbol(), "A");
                }
            }
        }
    }
    #[test]
    fn top_bar_line_sits_between_the_tabs_and_the_crumbs() {
        let glyph = if crate::transcript::look::glyphs().ascii { "-" } else { "─" };
        for light in [false, true] {
            let p = Palette::new(light);
            for centered in [false, true] {
                for (width, height) in [(60u16, 24u16), (100, 30), (140, 40), (100, 23), (100, 12)] {
                    let s = Snapshot {
                        title: "Test".into(),
                        breadcrumb: "/workspace · main".into(),
                        theme: if light { "nexus-light" } else { "nexus-dark" }.into(),
                        centered_layout: centered,
                        ..Default::default()
                    };
                    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
                    let mut cache = Cache::default();
                    let mut regions_out = None;
                    terminal
                        .draw(|frame| {
                            regions_out = Some(draw(
                                frame, &s, &Editor::default(), &Editor::default(),
                                &Editor::default(), "", 0, 0, 0, &mut cache, true, false,
                                false, false, 0, 0,
                            ));
                        })
                        .unwrap();
                    let r = regions_out.unwrap();
                    let buffer = terminal.backend().buffer();
                    let row = |y: u16| -> String {
                        (r.tabs.x..r.tabs.right()).map(|x| buffer[(x, y)].symbol()).collect()
                    };
                    let ctx = format!("light={light} centered={centered} {width}x{height}");
                    if height < TOP_LINE_MIN_HEIGHT {
                        assert_eq!(r.tabs.height, 3, "{ctx}: line row dropped");
                        assert!(row(r.tabs.y + 1).contains("/workspace"), "{ctx}: crumbs move up");
                        continue;
                    }
                    assert_eq!(r.tabs.height, 4, "{ctx}");
                    let line = row(r.tabs.y + 1);
                    assert!(
                        line.contains(glyph) && line.trim_matches(|c| c == ' ' || c == glyph.chars().next().unwrap()).is_empty(),
                        "{ctx}: line row {line:?}"
                    );
                    let x = r.tabs.x + r.tabs.width / 2;
                    assert_eq!(buffer[(x, r.tabs.y + 1)].fg, p.border, "{ctx}: rule colour");
                    assert_eq!(buffer[(x, r.tabs.y + 3)].fg, p.rule_faint, "{ctx}: closing rule colour");
                    assert!(!row(r.tabs.y).contains(glyph), "{ctx}: tabs row has no line");
                    assert!(row(r.tabs.y + 2).contains("/workspace"), "{ctx}: crumbs under the line");
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
    // Chips carry one padding cell on each side (counted by the layout, not stored).
    // Secret input pads the same cells but never styles them.
    let chips = if secret {
        Vec::new()
    } else {
        layout.chips.clone()
    };
    let mut rows = Vec::with_capacity(layout.rows.len());
    for (row, range) in layout.rows.iter().enumerate() {
        let mut line = Line::default();
        for (offset, grapheme) in layout.row_text(row).grapheme_indices(true) {
            let at = range.start + offset;
            let chip = chips.iter().find(|&&(start, end)| at >= start && at < end);
            let base = if selection.is_some_and(|(start, end)| at >= start && at < end) {
                Style::default().bg(palette.accent).fg(palette.background)
            } else if chip.is_some() {
                Style::default().bg(palette.element_hi).fg(palette.accent)
            } else {
                Style::default().fg(palette.text)
            };
            let style = if at == editor.cursor {
                cursor_style
            } else {
                base
            };
            let (lead, trail) = crate::editor::chip_padding(&layout.chips, at, grapheme);
            if lead == 1 {
                line.spans.push(Span::styled(" ", base));
            }
            let text = if secret {
                "•".to_string()
            } else if chip.is_some_and(|&(start, _)| at == start + 1) {
                // Same width as the stored `[image 1]`, so wrapping and the cursor map 1:1.
                grapheme.to_uppercase()
            } else if grapheme.chars().any(char::is_control) {
                grapheme.chars().flat_map(char::escape_default).collect()
            } else {
                grapheme.to_string()
            };
            line.spans.push(Span::styled(text, style));
            if trail == 1 {
                line.spans.push(Span::styled(" ", base));
            }
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
        // The padded `[Image` (7 cells) no longer fits after "send ", so it wraps whole.
        assert_eq!(rows[0].to_string(), "send ");
        assert_eq!(rows[1].to_string(), " [Image ");
        assert_eq!(rows[2].to_string(), "#1]  ");
        assert_eq!(rows[3].to_string(), "next");
        assert!(rows[1]
            .spans
            .iter()
            .all(|span| span.style.bg == Some(palette.accent)));
        assert!(rows[2].spans[..4]
            .iter()
            .all(|span| span.style.bg == Some(palette.accent)));
        assert_eq!(rows[2].spans[4].style.bg, Some(palette.text));
        editor.backspace();
        assert_eq!(editor.text, "send \nnext");
    }

    #[test]
    fn attachment_references_draw_as_padded_highlighted_chips() {
        let mut editor = Editor::default();
        editor.insert("see [image 1] and [document 2] [image 0] [x]");
        editor.cursor = 0;
        let palette = Palette::new(false);
        let rows = editor_view(&editor, false, &palette, 80, u16::MAX);
        let text = rows[0].to_string();
        assert!(
            text.starts_with("see  [Image 1]  and  [Document 2]  [image 0] [x]"),
            "{text}"
        );
        assert_eq!(
            editor.text, "see [image 1] and [document 2] [image 0] [x]",
            "padding is not stored"
        );
        // Spans are one ASCII cell each, so display columns index them directly.
        let chip = |needle: &str| -> Vec<_> {
            let at = text.find(needle).unwrap();
            rows[0].spans[at - 1..at + needle.len() + 1]
                .iter()
                .map(|s| s.style.bg)
                .collect()
        };
        assert!(chip("[Image 1]")
            .iter()
            .all(|bg| *bg == Some(palette.element_hi)));
        assert!(chip("[Document 2]")
            .iter()
            .all(|bg| *bg == Some(palette.element_hi)));
        assert!(
            chip("[image 0]").iter().all(|bg| bg.is_none()),
            "invalid numbers stay plain"
        );
        // The cursor inside a chip still wins over the chip highlight.
        editor.cursor = 5;
        let rows = editor_view(&editor, false, &palette, 80, u16::MAX);
        assert_eq!(rows[0].spans[6].style.bg, Some(palette.text));
        // Pointer mapping uses the padded columns: the `[` cell is column 5.
        let layout = editor.visual_layout(80);
        assert_eq!(layout.cursor_position(4), (0, 5));
        assert_eq!(layout.cursor_position(13), (0, 15));
        assert_eq!(layout.cursor_at(0, 3), 3);
        // Column 18 is the `d` of "and" (source offset 16); column 21 the second chip's `[`.
        assert_eq!(editor.cursor_at_point(80, 3, 18, 0), 16);
        assert_eq!(editor.cursor_at_point(80, 3, 21, 0), 18);
        // Secret input never reveals or styles references.
        let rows = editor_view(&editor, true, &palette, 80, u16::MAX);
        assert!(rows[0].to_string().chars().all(|c| c == '•' || c == ' '));
    }

    #[test]
    fn stop_button_highlights_only_its_cells_on_hover() {
        use ratatui::{backend::TestBackend, Terminal};
        let now = crate::live_bar::now_epoch();
        let mut s = Snapshot::default();
        s.status = "running".into();
        s.turn_status = Some(crate::bridge::TurnStatus {
            activity: "Working".into(),
            activity_since: now,
            turn_since: now - 5.0,
            ..Default::default()
        });
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut cache = Cache::default();
        let paint = |terminal: &mut Terminal<TestBackend>,
                     pointer: Option<(u16, u16)>,
                     cache: &mut Cache|
         -> Rect {
            cache.pointer = pointer;
            let mut composer = Rect::default();
            terminal
                .draw(|frame| {
                    composer = draw(
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
                    )
                    .composer;
                })
                .unwrap();
            composer
        };
        let composer = paint(&mut terminal, None, &mut cache);
        let row = above_layout(composer, &s).status.unwrap();
        let p = Palette::new(false);
        let bg_of =
            |terminal: &Terminal<TestBackend>, x: u16| terminal.backend().buffer()[(x, row.y)].bg;
        let stop_x = row.right() - 2;
        assert_ne!(
            bg_of(&terminal, stop_x),
            p.element,
            "idle button is not highlighted"
        );
        paint(&mut terminal, Some((stop_x, row.y)), &mut cache);
        let first = row.right() - 6;
        for x in first..row.right() {
            assert_eq!(bg_of(&terminal, x), p.element, "[stop] cell {x} lit");
        }
        assert_ne!(
            bg_of(&terminal, first - 1),
            p.element,
            "the gap before it stays plain"
        );
        assert!(stop_at(composer, &s, first, row.y) && !stop_at(composer, &s, first - 1, row.y));
        paint(&mut terminal, Some((first - 1, row.y)), &mut cache);
        assert_ne!(
            bg_of(&terminal, first),
            p.element,
            "hovering the clock does not light the button"
        );
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
    fn shell_drafts_show_a_bash_indicator_and_warning_rail() {
        use ratatui::{backend::TestBackend, Terminal};
        let s = Snapshot::default();
        let p = Palette::new(false);
        let mut cache = Cache::default();
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        for (text, shell) in [
            ("!git status", true),
            ("!", true),
            ("git status", false),
            ("", false),
        ] {
            let mut editor = Editor::default();
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
                    )
                    .composer;
                })
                .unwrap();
            let rows = composer_rows(composer, &s);
            let buffer = terminal.backend().buffer();
            let line = |y: u16| -> String {
                (composer.x..composer.right())
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect()
            };
            let hint = line(rows[2].y - 1);
            assert_eq!(hint.contains("! bash"), shell, "{text:?}: {hint:?}");
            assert_eq!(hint.contains("output goes to context"), shell, "{text:?}");
            if shell {
                assert!((composer.x..composer.right())
                    .any(|x| buffer[(x, rows[2].y - 1)].fg == p.warning));
            }
            if text == "!" {
                assert!(line(rows[1].y + 1).contains("type a bash command"));
            }
        }
    }

    #[test]
    fn composer_keeps_a_blank_row_above_the_controls() {
        use ratatui::{backend::TestBackend, Terminal};
        let s = Snapshot::default();
        let mut cache = Cache::default();
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        for text in ["", "one", "one\ntwo", "one\ntwo\nthree\nfour"] {
            let mut editor = Editor::default();
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
                    )
                    .composer;
                })
                .unwrap();
            let rows = composer_rows(composer, &s);
            let buffer = terminal.backend().buffer();
            let row = |y: u16| -> String {
                (composer.x + 5..composer.x + composer.width.saturating_sub(4))
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect()
            };
            let last = text.lines().last().unwrap_or("Type a message…");
            assert_eq!(row(rows[2].y - 2).trim_end(), last, "{text:?}");
            assert!(
                row(rows[2].y - 1).trim().is_empty(),
                "{text:?}: gap row is blank"
            );
            let pad = rows[2].y + 1;
            let cell = &buffer[(composer.x + 5, pad)];
            assert_eq!(
                cell.symbol(),
                "▀",
                "half-row card padding below the controls"
            );
            let palette = Palette::new(false);
            assert_eq!((cell.fg, cell.bg), (palette.composer, palette.background));
            assert_eq!(palette.composer, Color::Rgb(0x1c, 0x1c, 0x1c));
            assert_eq!(
                buffer[(composer.x + 5, rows[2].y)].bg,
                palette.composer,
                "card background"
            );
            assert_eq!(
                buffer[(composer.x + 2, pad)].symbol(),
                "▀",
                "card edge has no coloured rail"
            );
        }
    }

    #[test]
    fn queued_section_is_unboxed_with_hover_controls_and_collapses() {
        use ratatui::{backend::TestBackend, Terminal};
        let mut s = Snapshot::default();
        // Real queue order: two steering messages, then three queued ones.
        s.queue_items = [
            ("s0", "S", false, true),
            ("s1", "S", true, false),
            ("q0", "Q", false, true),
            ("q1", "Q", true, true),
            ("q2", "Q", true, false),
        ]
        .into_iter()
        .map(|(id, tag, can_up, can_down)| crate::bridge::QueueItem {
            can_up,
            can_down,
            ..queue_item(id, tag, id)
        })
        .collect();
        let mut cache = Cache::default();
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut composer = Rect::default();
        let mut paint = |s: &Snapshot, cache: &mut Cache, composer: &mut Rect| {
            terminal
                .draw(|frame| {
                    *composer = draw(
                        frame,
                        s,
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
                    )
                    .composer;
                })
                .unwrap();
            terminal.backend().buffer().clone()
        };
        let buffer = paint(&s, &mut cache, &mut composer);
        let text = |y: u16| -> String {
            (composer.x..composer.right())
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect()
        };
        let card = composer_rows(composer, &s)[1].y;
        assert!(
            text(card - 1).contains("+1 more"),
            "the queue sits directly on the card"
        );
        let header = text(card - 7);
        assert!(header.trim_start().starts_with("▾ Steering 2"), "{header}");
        assert!(!header.contains('╭'), "no box");
        assert!(text(card - 6).contains("#1 s0"), "{}", text(card - 6));
        assert!(text(card - 5).contains("#2 s1"));
        assert!(
            text(card - 4).trim_start().starts_with("▾ Queued 3"),
            "{}",
            text(card - 4)
        );
        assert!(text(card - 3).contains("#3 q0"), "real queue positions");
        assert!(text(card - 2).contains("#4 q1"));
        assert!(
            !text(card - 2).contains('✕'),
            "controls only on the hovered row"
        );
        // Hovering a queued message with neighbours on both sides shows ↑ ↓ ✕ at the right.
        let row = card - 2;
        cache.pointer = Some((composer.x + 6, row));
        let buffer = paint(&s, &mut cache, &mut composer);
        let cells: Vec<u16> = (composer.x..composer.right())
            .filter(|x| ["↑", "↓", "✕"].contains(&buffer[(*x, row)].symbol()))
            .collect();
        assert_eq!(cells.len(), 3);
        let hits: Vec<_> = cells
            .iter()
            .map(|x| queue_control_at(composer, &s, *x, row))
            .collect();
        assert_eq!(
            hits,
            [
                Some(("q1".into(), "up")),
                Some(("q1".into(), "down")),
                Some(("q1".into(), "remove"))
            ]
        );
        assert_eq!(queue_control_at(composer, &s, cells[0] + 1, row), None);
        assert_eq!(
            queue_control_at(composer, &s, cells[0], card - 1),
            None,
            "+N more row"
        );
        // A queued row draws ↑ ↓ [Steer] ✕; the chip is one hit target.
        let (span_x, span_right) = (composer.x, composer.right());
        let line_at = move |buffer: &ratatui::buffer::Buffer, y: u16| -> String {
            (span_x..span_right)
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect()
        };
        let drawn = line_at(&buffer, row);
        assert!(drawn.contains("↑ ↓   Steer   ✕"), "{drawn}");
        let chip = drawn.find("Steer").unwrap();
        let chip_x = composer.x + drawn[..chip].chars().count() as u16;
        for dx in 0..5 {
            assert_eq!(
                queue_control_at(composer, &s, chip_x + dx, row),
                Some(("q1".into(), "steer"))
            );
        }
        // Steering rows have no arrows: `Interrupt ✕`, and nothing reorders them.
        let first = card - 6;
        cache.pointer = Some((composer.x + 6, first));
        let buffer = paint(&s, &mut cache, &mut composer);
        let drawn = line_at(&buffer, first);
        assert!(drawn.contains("Interrupt   ✕"), "{drawn}");
        assert!(!drawn.contains('↑') && !drawn.contains('↓'), "{drawn}");
        let chip = drawn.find("Interrupt").unwrap();
        let chip_x = composer.x + drawn[..chip].chars().count() as u16;
        assert_eq!(
            queue_control_at(composer, &s, chip_x, first),
            Some(("s0".into(), "interrupt"))
        );
        assert_eq!(
            queue_control_at(composer, &s, cells[2], first),
            Some(("s0".into(), "remove"))
        );
        // An interrupt item that is already pending offers only ✕.
        s.queue_items[1].mode = "interrupt".into();
        s.queue_items[1].tag = "I".into();
        cache.pointer = Some((composer.x + 6, card - 5));
        let buffer = paint(&s, &mut cache, &mut composer);
        let drawn = line_at(&buffer, card - 5);
        assert!(
            drawn.contains('✕') && !drawn.contains("Interrupt"),
            "{drawn}"
        );
        assert_eq!(
            queue_control_at(composer, &s, cells[2], card - 5),
            Some(("s1".into(), "remove"))
        );
        assert_eq!(queue_control_at(composer, &s, cells[1], card - 5), None);
        // A section's caret row collapses both lists down to their headers.
        assert!(queue_header_at(composer, &s, composer.x + 2, card - 7));
        assert!(queue_header_at(composer, &s, composer.x + 2, card - 4));
        assert!(!queue_header_at(composer, &s, composer.x + 2, card - 5));
        s.queue_collapsed = true;
        assert_eq!(queue_rows(&s), 2);
        let buffer = paint(&s, &mut cache, &mut composer);
        let card = composer_rows(composer, &s)[1].y;
        let line = |y: u16| -> String {
            (composer.x..composer.right())
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect()
        };
        assert!(
            line(card - 2).trim_start().starts_with("▸ Steering 2"),
            "{}",
            line(card - 2)
        );
        assert!(
            line(card - 1).trim_start().starts_with("▸ Queued 3"),
            "{}",
            line(card - 1)
        );
    }

    #[test]
    fn status_line_shows_waiting_for_a_free_turn_slot_above_the_queue() {
        use ratatui::{backend::TestBackend, Terminal};
        let mut s = Snapshot::default();
        s.slot_wait = Some(crate::bridge::SlotWait {
            waiting: 1,
            running: 4,
            limit: 4,
        });
        s.queue_items = vec![queue_item("q1", "Q", "held back message")];
        let mut cache = Cache::default();
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut composer = Rect::default();
        terminal
            .draw(|frame| {
                composer = draw(
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
                    false,
                    false,
                    false,
                    false,
                    0,
                    0,
                )
                .composer;
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let text = |y: u16| -> String {
            (composer.x..composer.right())
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect()
        };
        let layout = above_layout(composer, &s);
        let status = text(layout.status.unwrap().y);
        assert!(
            status.contains("⋯ Waiting for a free turn slot (4 of 4 running)"),
            "{status}"
        );
        assert!(!status.contains("[stop]"), "{status}");
        assert!(
            text(layout.status.unwrap().y + 1).trim().is_empty(),
            "blank row"
        );
        assert!(text(layout.headers[0].1.y).contains("▾ Queued 1"));
        assert!(text(layout.items[0].1.y).contains("#1 held back message"));
        s.slot_wait = None;
        assert!(!status_visible(&s));
    }

    #[test]
    fn idle_root_with_running_agents_shows_the_agents_status_row() {
        let mut s = Snapshot::default();
        assert!(!status_visible(&s));
        s.agents_running = 2;
        assert!(status_visible(&s), "the row appears with no turn running");
        s.agent_page = "child".into();
        assert!(!status_visible(&s), "not on a subagent page");
    }

    #[test]
    fn one_blank_row_separates_the_status_line_from_the_composer() {
        use ratatui::{backend::TestBackend, Terminal};
        let now = crate::live_bar::now_epoch();
        let mut s = Snapshot::default();
        s.status = "running".into();
        s.turn_status = Some(crate::bridge::TurnStatus {
            activity: "Waiting for response".into(),
            activity_since: now - 2.0,
            turn_since: now - 5.0,
            output_tokens: 10,
            output_estimated: false,
        });
        let mut cache = Cache::default();
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut composer = Rect::default();
        terminal
            .draw(|frame| {
                composer = draw(
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
                    false,
                    false,
                    false,
                    false,
                    0,
                    0,
                )
                .composer;
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let text = |y: u16| -> String {
            (composer.x..composer.right())
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect()
        };
        let status = above_layout(composer, &s).status.unwrap().y;
        let card = composer_rows(composer, &s)[1].y;
        assert!(text(status).contains("Waiting for response"));
        assert_eq!(card, status + 2, "exactly one row between status and card");
        assert!(text(status + 1).trim().is_empty(), "blank row");
        s.turn_status = None;
        assert_eq!(above_card_rows(&s), 1, "idle keeps the single margin row");
        s.turn_status = Some(crate::bridge::TurnStatus {
            activity: "Waiting for response".into(),
            ..Default::default()
        });
        s.queue_items.clear();
        assert_eq!(above_card_rows(&s), 2, "status has one following blank row");
    }

    #[test]
    fn status_line_shows_activity_elapsed_tokens_and_stop() {
        use ratatui::{backend::TestBackend, Terminal};
        let now = crate::live_bar::now_epoch();
        let mut s = Snapshot::default();
        s.status = "running".into();
        s.turn_status = Some(crate::bridge::TurnStatus {
            activity: "Waiting for response".into(),
            activity_since: now - 2.0,
            turn_since: now - 280.0,
            output_tokens: 121_000,
            output_estimated: false,
        });
        s.queue_items = vec![queue_item("q1", "Q", "composer should remain")];
        let mut cache = Cache::default();
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut composer = Rect::default();
        terminal
            .draw(|frame| {
                composer = draw(
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
                    false,
                    false,
                    false,
                    false,
                    0,
                    0,
                )
                .composer;
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let text = |y: u16| -> String {
            (composer.x..composer.right())
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect()
        };
        let layout = above_layout(composer, &s);
        let row = layout.status.unwrap();
        let p = Palette::new(false);
        assert_eq!(row.x, composer.x + 4);
        assert_eq!(
            row.right(),
            composer.right() - 2,
            "metrics and stop stay right-aligned"
        );
        assert_eq!(
            buffer[(row.x, row.y)].fg,
            shimmer_greys(&p).1,
            "the ∴ mark is the base dark grey"
        );
        assert_eq!(
            buffer[(row.x + 2, row.y)].fg,
            shimmer_greys(&p).1,
            "animations off: the word in the base grey"
        );
        let status = text(row.y);
        assert!(status.contains("∴ Waiting for response…"), "{status}");
        assert!(
            !status.contains("2.0s") && !status.contains("response… 2"),
            "{status}"
        );
        assert!(
            status.trim_end().ends_with("4m 40s ↓121k [stop]"),
            "{status}"
        );
        assert!(
            text(layout.status.unwrap().y + 1).trim().is_empty(),
            "blank row"
        );
        assert!(text(layout.headers[0].1.y).contains("▾ Queued 1"));
        assert!(text(layout.items[0].1.y).contains("#1 composer should remain"));
        let row = layout.status.unwrap();
        let stop_x = row.right() - 3;
        assert!(stop_at(composer, &s, stop_x, row.y));
        assert!(!stop_at(composer, &s, row.x + 3, row.y));
        assert!(!stop_at(composer, &s, stop_x, row.y + 1));
        s.turn_status.as_mut().unwrap().output_estimated = true;
        let (est, _) = status_right(&s, now).unwrap();
        assert!(est.contains("↓~121k [stop]"), "{est}");
        s.turn_status = None;
        assert!(above_layout(composer, &s).status.is_none());
        assert_eq!(fmt_activity(2.04), "2.0s");
        assert_eq!(fmt_turn(3725.0), "1h 2m");
        assert_eq!(fmt_tokens(1234), "1.2k");
    }

    #[test]
    fn composer_card_sits_directly_under_the_rows_above_it() {
        use ratatui::{backend::TestBackend, Terminal};
        let mut cache = Cache::default();
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut queued = Snapshot::default();
        queued.queue_items = vec![queue_item("q1", "Q", "Queued · a")];
        queued.attachment_lines = vec!["img.png".into()];
        for s in [Snapshot::default(), queued] {
            let mut composer = Rect::default();
            terminal
                .draw(|frame| {
                    composer = draw(
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
                        false,
                        false,
                        false,
                        false,
                        0,
                        0,
                    )
                    .composer;
                })
                .unwrap();
            let buffer = terminal.backend().buffer();
            let card = composer_rows(composer, &s)[1].y;
            let line = |y: u16| -> String {
                (composer.x..composer.right())
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect()
            };
            if s.queue_items.is_empty() {
                // Nothing above the card: one blank margin row separates it from the transcript.
                assert!(line(card - 1).trim().is_empty(), "margin row");
            } else {
                // The Queued section sits directly on the card: no blank row between.
                assert!(line(card - 1).contains("#1"), "{:?}", line(card - 1));
                assert!(line(card - 2).contains("▾ Queued 1"));
                assert_eq!(composer.y, card - 2);
            }
            assert!((composer.y..card).all(|y| !line(y).contains("img.png")));
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

/// Shell mode: a draft starting with `!` runs the rest as bash in the workspace.
pub fn is_shell_draft(draft: &Editor) -> bool {
    draft.text.starts_with('!')
}
const SHELL_HINT: &str = " Enter runs in the workspace · output goes to context";
pub fn composer_rows(area: Rect, s: &Snapshot) -> Vec<Rect> {
    Layout::vertical([
        Constraint::Length(above_card_rows(s).max(1)), // a lone row is the blank margin above the card
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(1), // the card's bottom padding row
    ])
    .split(area)
    .to_vec()
}

pub fn inset(a: Rect, left: u16, right: u16) -> Rect {
    Rect {
        x: a.x + left,
        width: a.width.saturating_sub(left + right),
        ..a
    }
}

struct ControlLayout {
    left: Vec<(String, &'static str)>,
    text: String,
    bars: bool,
    gap: usize,
    context_start: usize,
}
fn control_layout(s: &Snapshot, width: usize) -> ControlLayout {
    // The manager session has no role, model choice or effort: its labels are shown, not clickable.
    let manager = s.manager.active;
    let (agent_command, model_command) = if manager {
        ("", "")
    } else {
        ("/agent", "/model")
    };
    let mut provider = !s.provider.is_empty() && !manager;
    let mut effort = !s.effort.is_empty() && !manager;
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
        let mut left = vec![(agent, agent_command), (s.model.clone(), model_command)];
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
            return (!command.is_empty()).then_some(command);
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
        let area = Rect::new(0, 15, 100, 12);
        for queued in [false, true] {
            if queued {
                s.queue_items.push(queue_item("q1", "Q", "queued"));
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

    #[test]
    fn manager_session_has_no_role_model_or_effort_controls() {
        let mut s = Snapshot::default();
        s.agent = "manager".into();
        s.model = "gpt-6-luna".into();
        s.provider = "github-copilot".into();
        s.effort = "high".into();
        s.manager.active = true;
        let area = Rect::new(0, 15, 100, 12);
        let y = composer_rows(area, &s)[2].y;
        // The label and model are still drawn, but nothing opens /agent, /model or /effort.
        let layout = control_layout(&s, area.width.saturating_sub(9) as usize);
        let texts: Vec<_> = layout.left.iter().map(|(text, _)| text.as_str()).collect();
        assert_eq!(texts, ["Manager", "gpt-6-luna"]);
        for x in 0..area.width {
            assert_eq!(composer_control_at(area, &s, x, y), None, "x={x}");
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
    // Animations off: the transcribed words are drawn as they are, never scrambled first.
    if !cache.animate {
        return text.to_string();
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
        cache.animate = true;
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
        // Animations off: the new words show at once.
        cache.animate = false;
        cache.spin = 20;
        assert_eq!(
            animated_voice_preview(&mut cache, "hello wxrld"),
            "hello wxrld"
        );
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
                        false, false, false, 0, 0,
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
    fn finished_group_folds_frame_by_frame_then_expires() {
        use crate::transcript::fold::{FLASH_FRAMES, FRAME};
        let group = |status: &str, rev: &str, members: usize| Content {
            id: "g".into(),
            kind: "tool_group".into(),
            status: status.into(),
            collapsed: true,
            rev: rev.into(),
            text: "Read 3 files".into(),
            running_label: "Working".into(),
            operation: Some(json!({"kind":"block_toggle","id":"g"})),
            members: (0..members)
                .map(|i| Content {
                    id: format!("m{i}"),
                    kind: "tool".into(),
                    title: "Read".into(),
                    text: format!("f{i}"),
                    status: "completed".into(),
                    ..Default::default()
                })
                .collect(),
            ..Default::default()
        };
        let p = Palette::new(false);
        let mut cache = Cache::default();
        cache.animate = true;
        let now = std::time::Instant::now();
        let running = vec![group("running", "1", 3)];
        cache.step_folds_at(&running, now);
        cache.update_content(&[], &running, 80, &p);
        assert!(!cache.folding());
        let full = cache.lines.len();
        assert_eq!(full, 4, "header and three tree rows");
        let done = vec![group("completed", "2", 0)];
        let rows_at = |frames: u32, cache: &mut Cache| {
            cache.step_folds_at(&done, now + FRAME * frames);
            cache.update_content(&[], &done, 80, &p);
            cache.lines.len()
        };
        assert_eq!(rows_at(0, &mut cache), 4, "the first frame keeps every row");
        assert!(cache.folding());
        assert_eq!(rows_at(1, &mut cache), 3);
        assert_eq!(rows_at(2, &mut cache), 2);
        assert_eq!(rows_at(3, &mut cache), 1, "rows gone, the header flashes");
        assert!(cache.folding());
        assert_eq!(rows_at(3 + FLASH_FRAMES as u32, &mut cache), 1);
        assert!(!cache.folding(), "the transient expires on its own");
        // Animations off: a finished group never folds.
        let mut still = Cache::default();
        still.step_folds_at(&running, now);
        still.step_folds_at(&done, now);
        assert!(!still.folding());
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
    fn centered_layout_caps_the_conversation_column() {
        let mut s = Snapshot::default();
        let area = Rect::new(0, 0, 200, 40);
        assert!(regions(area, &s, 8, false).transcript.width > CENTERED_WIDTH);
        s.centered_layout = true;
        let r = regions(area, &s, 8, false);
        assert_eq!(
            (r.transcript.width, r.composer.width),
            (CENTERED_WIDTH, CENTERED_WIDTH)
        );
        assert_eq!(r.transcript.x, (200 - CENTERED_WIDTH) / 2);
        let narrow = regions(Rect::new(0, 0, 60, 40), &s, 8, false);
        assert_eq!(
            narrow.transcript.width, 60,
            "narrow terminals keep their full width"
        );
    }
    #[test]
    fn narrow_details_drawer_keeps_the_conversation_beside_it() {
        let mut s = Snapshot {
            details_sidebar: true,
            ..Default::default()
        };
        let r = regions(Rect::new(0, 0, 80, 24), &s, 8, false);
        assert_eq!((r.details.width, r.transcript.width), (40, 40));
        for part in [r.transcript, r.composer, r.tabs] {
            assert!(part.right() <= r.details.x, "{part:?} vs {:?}", r.details);
        }
        let r = regions(Rect::new(0, 0, 120, 24), &s, 8, false);
        assert_eq!((r.details.width, r.transcript.width), (40, 80));
        let r = regions(Rect::new(0, 0, 50, 24), &s, 8, false);
        assert!(r.transcript.width >= 24);
        assert!(r.transcript.right() <= r.details.x);
        s.details_sidebar = false;
        let r = regions(Rect::new(0, 0, 60, 24), &s, 8, false);
        assert_eq!(r.transcript.width, 60);
    }
    #[test]
    fn details_sidebar_shows_beside_the_sessions_dashboard() {
        let area = Rect::new(0, 0, 160, 40);
        let mut s = Snapshot {
            details_sidebar: true,
            dashboard: Some(serde_json::json!({"rows": []})),
            ..Default::default()
        };
        let r = regions(area, &s, 8, false);
        assert_eq!(r.details.width, 40);
        assert_eq!(r.tabs.height, 0, "the dashboard draws its own header row");
        assert_eq!(
            r.transcript.width, 120,
            "the dashboard uses the remaining width"
        );
        s.details_sidebar = false;
        assert_eq!(regions(area, &s, 8, false).details.width, 0);
    }
    #[test]
    fn sessions_dashboard_has_no_composer() {
        let area = Rect::new(0, 0, 100, 30);
        let s = Snapshot {
            dashboard: Some(serde_json::json!({"rows": []})),
            ..Default::default()
        };
        assert_eq!(composer_height(area, &Editor::default(), &s), 0);
        let r = regions(area, &s, 0, false);
        assert_eq!(r.composer.height, 0);
        assert!(r.transcript.height >= 28);
    }
    #[test]
    fn clicking_the_composer_context_text_is_a_context_hit() {
        let s = Snapshot {
            context_label: "6.2K · 3.1%".into(),
            context_used: Some(6200),
            context_window: Some(200000),
            ..Default::default()
        };
        let area = Rect::new(0, 20, 100, 8);
        let row = composer_rows(area, &s)[2].y;
        let layout = control_layout(&s, area.width.saturating_sub(9) as usize);
        let x = area.x + 5 + layout.context_start as u16;
        assert!(composer_context_at(area, &s, x, row));
        assert!(!composer_context_at(area, &s, x, row + 1));
    }
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

#[cfg(test)]
fn queue_item(id: &str, tag: &str, text: &str) -> crate::bridge::QueueItem {
    crate::bridge::QueueItem {
        id: id.into(),
        tag: tag.into(),
        mode: if tag == "S" { "steer" } else { "queue" }.into(),
        text: text.into(),
        ..Default::default()
    }
}

#[cfg(test)]
mod queue_flash_tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn a_promoted_message_flashes_once_and_expires() {
        let now = Instant::now();
        let mut flash = QueueFlash::default();
        flash.observe(&[queue_item("q1", "Q", "a")], now, true);
        assert!(!flash.live(now, true), "a new item never flashes");
        flash.observe(&[queue_item("q1", "S", "a")], now, true);
        assert!(flash.live(now, true), "the promotion starts a flash");
        assert!(flash.strength("q1", now) > 0.99);
        let mid = now + FLASH_FRAME * 6;
        assert!((flash.strength("q1", mid) - 0.5).abs() < 0.05, "fades out");
        let later = now + FLASH_FRAME * 13;
        assert!(!flash.live(later, true), "ends by itself");
        flash.observe(&[queue_item("q1", "S", "a")], later, true);
        assert!(!flash.live(later, true), "no repeat without another change");
        assert!(
            !flash.live(now + Duration::from_millis(1), false),
            "animations off: no flash"
        );
    }

    #[test]
    fn animations_off_never_start_a_flash() {
        let now = Instant::now();
        let mut flash = QueueFlash::default();
        flash.observe(&[queue_item("q1", "Q", "a")], now, false);
        flash.observe(&[queue_item("q1", "S", "a")], now, false);
        assert!(!flash.live(now, false) && flash.strength("q1", now) == 0.0);
    }
}

#[cfg(test)]
mod queue_focus_tests {
    use super::*;
    use crossterm::event::{KeyCode as K, KeyModifiers as M};
    use ratatui::{backend::TestBackend, Terminal};

    fn snapshot() -> Snapshot {
        let mut s = Snapshot::default();
        s.queue_items = [
            ("s0", "S", false, false),
            ("q0", "Q", false, true),
            ("q1", "Q", true, false),
        ]
        .into_iter()
        .map(|(id, tag, can_up, can_down)| crate::bridge::QueueItem {
            can_up,
            can_down,
            ..queue_item(id, tag, id)
        })
        .collect();
        s
    }
    fn key(s: &Snapshot, focus: &str, code: K, mods: M) -> QueueKey {
        queue_key(s, &[0, 1, 2], focus, code, mods)
    }
    fn op(id: &str, op: &'static str, then: Option<&str>) -> QueueKey {
        QueueKey::Op(id.into(), op, then.map(String::from))
    }

    #[test]
    fn enter_and_s_promote_one_step() {
        let s = snapshot();
        for code in [K::Enter, K::Char('s')] {
            assert_eq!(key(&s, "q0", code, M::NONE), op("q0", "steer", Some("q0")));
            assert_eq!(
                key(&s, "s0", code, M::NONE),
                op("s0", "interrupt", Some("s0"))
            );
        }
        let mut s = snapshot();
        s.queue_items[0].mode = "interrupt".into();
        s.queue_items[0].tag = "I".into();
        assert_eq!(
            key(&s, "s0", K::Enter, M::NONE),
            QueueKey::Stay,
            "already pending"
        );
    }

    #[test]
    fn delete_backspace_and_x_remove_and_refocus_a_neighbour() {
        let s = snapshot();
        for code in [K::Delete, K::Backspace, K::Char('x')] {
            assert_eq!(key(&s, "q0", code, M::NONE), op("q0", "remove", Some("q1")));
        }
        assert_eq!(
            key(&s, "q1", K::Delete, M::NONE),
            op("q1", "remove", Some("q0")),
            "last row: previous"
        );
        let only = {
            let mut s = snapshot();
            s.queue_items.truncate(1);
            s
        };
        assert_eq!(
            queue_key(&only, &[0], "s0", K::Char('x'), M::NONE),
            op("s0", "remove", None)
        );
    }

    #[test]
    fn alt_arrows_reorder_queued_rows_only() {
        let s = snapshot();
        assert_eq!(key(&s, "q0", K::Down, M::ALT), op("q0", "down", Some("q0")));
        assert_eq!(key(&s, "q1", K::Up, M::ALT), op("q1", "up", Some("q1")));
        assert_eq!(
            key(&s, "q0", K::Up, M::ALT),
            QueueKey::Stay,
            "edge: disabled move"
        );
        assert_eq!(
            key(&s, "s0", K::Down, M::ALT),
            QueueKey::Stay,
            "steering never reorders"
        );
    }

    #[test]
    fn arrows_move_focus_and_the_edges_hand_it_on() {
        let s = snapshot();
        assert_eq!(
            key(&s, "q0", K::Down, M::NONE),
            QueueKey::Focus("q1".into())
        );
        assert_eq!(
            key(&s, "q0", K::Char('k'), M::NONE),
            QueueKey::Focus("s0".into())
        );
        assert_eq!(key(&s, "s0", K::Up, M::NONE), QueueKey::Prev);
        assert_eq!(key(&s, "q1", K::Down, M::NONE), QueueKey::Leave);
        assert_eq!(key(&s, "q1", K::Esc, M::NONE), QueueKey::Leave);
        assert_eq!(
            key(&s, "q1", K::Char('a'), M::NONE),
            QueueKey::Pass,
            "typing leaves and types"
        );
        assert_eq!(
            key(&s, "q1", K::Char('s'), M::CONTROL),
            QueueKey::Pass,
            "Ctrl keys keep their meaning"
        );
        assert_eq!(key(&s, "gone", K::Enter, M::NONE), QueueKey::Pass);
    }

    #[test]
    fn prune_drops_focus_and_press_of_a_message_that_left() {
        let mut ui = QueueUi {
            focus: Some("a".into()),
            press: Some(("b".into(), "remove")),
            ..Default::default()
        };
        ui.prune(&["a".into(), "b".into()]);
        assert!(ui.focus.is_some() && ui.press.is_some());
        ui.prune(&["c".into()]);
        assert!(ui.focus.is_none() && ui.press.is_none());
    }

    fn paint(s: &Snapshot, cache: &mut Cache) -> (Rect, ratatui::buffer::Buffer) {
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut composer = Rect::default();
        terminal
            .draw(|frame| {
                composer = draw(
                    frame,
                    s,
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
                )
                .composer;
            })
            .unwrap();
        (composer, terminal.backend().buffer().clone())
    }

    #[test]
    fn monochrome_marks_a_pressed_chip_with_a_reversed_cell_and_ascii_has_twins() {
        let s = snapshot();
        let mut cache = Cache::default();
        cache.mono = true;
        cache.queue_ui.focus = Some("q1".into());
        let row_of = |buffer: &ratatui::buffer::Buffer, composer: Rect, sym: &str| {
            let y = composer_rows(composer, &snapshot())[1].y - 1;
            let x = (composer.x..composer.right()).find(|x| buffer[(*x, y)].symbol() == sym);
            (x, y)
        };
        let (composer, buffer) = paint(&s, &mut cache);
        let (x, y) = row_of(&buffer, composer, "✕");
        let x = x.expect("controls on the focused row");
        assert!(!buffer[(x, y)].modifier.contains(Modifier::REVERSED));
        cache.queue_ui.press = Some(("q1".into(), "remove"));
        let (_, buffer) = paint(&s, &mut cache);
        assert!(
            buffer[(x, y)].modifier.contains(Modifier::REVERSED),
            "pressed is visible without colour"
        );
        assert_eq!(queue_marks(true), ("^", "v", "x"));
        assert!(queue_marks(true).0.is_ascii() && queue_marks(true).2.is_ascii());
        assert_eq!(queue_marks(false), ("↑", "↓", "✕"));
    }

    #[test]
    fn focus_shows_the_hover_controls_and_pressed_chips_change_colour() {
        let s = snapshot();
        let mut cache = Cache::default();
        let (composer, buffer) = paint(&s, &mut cache);
        let card = composer_rows(composer, &s)[1].y;
        let row = card - 1; // q1, the last listed row
        let find = |buffer: &ratatui::buffer::Buffer, y: u16, sym: &str| {
            (composer.x..composer.right()).find(|x| buffer[(*x, y)].symbol() == sym)
        };
        assert!(
            find(&buffer, row, "✕").is_none(),
            "no pointer, no focus: no controls"
        );
        cache.queue_ui.focus = Some("q1".into());
        let (_, buffer) = paint(&s, &mut cache);
        let x = find(&buffer, row, "✕").expect("a focused row shows the controls");
        let p = Palette::new(false);
        assert_eq!(buffer[(x, row)].fg, p.muted);
        // The chip and the cross while the mouse holds them.
        cache.queue_ui.press = Some(("q1".into(), "remove"));
        let (_, buffer) = paint(&s, &mut cache);
        assert_eq!(buffer[(x, row)].fg, p.error, "pressed cross is p.error");
        cache.queue_ui.press = Some(("q1".into(), "steer"));
        let (_, buffer) = paint(&s, &mut cache);
        let chip = find(&buffer, row, "S").expect("Steer chip");
        assert_eq!(buffer[(chip, row)].bg, p.muted, "pressed chip is p.muted");
        assert_eq!(buffer[(x, row)].fg, p.muted, "the cross is not pressed");
        cache.queue_ui.press = None;
        let (_, buffer) = paint(&s, &mut cache);
        assert_eq!(buffer[(chip, row)].bg, p.element_hi);
    }
}

#[cfg(test)]
mod animations_off_tests {
    use super::*;
    use crate::bridge::Content;
    use ratatui::{backend::TestBackend, Terminal};

    fn rich() -> Snapshot {
        let mut s = Snapshot::default();
        s.status = "running".into();
        s.voice_phase = "recording".into();
        s.voice_preview = "hello".into();
        s.live.active = true;
        s.live.state = "active".into();
        s.live.phase = "listening".into();
        s.live.started = 1.0;
        s.context_window = Some(200_000);
        s.tabs = vec![crate::bridge::Session {
            id: "t1".into(),
            title: "Busy tab".into(),
            status: "working".into(),
            active: true,
            ..Default::default()
        }];
        s.queue_items = vec![
            queue_item("s0", "S", "steer one"),
            queue_item("q0", "Q", "queued one"),
        ];
        s.blocks = vec![
            Content {
                id: "m".into(),
                kind: "markdown".into(),
                text: "\u{e000} Bash · ls\nan answer that is still streaming".into(),
                rev: "1".into(),
                ..Default::default()
            },
            Content {
                id: "g".into(),
                kind: "tool_group".into(),
                status: "running".into(),
                collapsed: true,
                running_label: "Working".into(),
                rev: "1".into(),
                members: vec![Content {
                    id: "t".into(),
                    kind: "tool".into(),
                    title: "Read".into(),
                    text: "src/main.rs".into(),
                    status: "running".into(),
                    ..Default::default()
                }],
                ..Default::default()
            },
        ];
        s
    }
    fn screen(s: &Snapshot, cache: &mut Cache, size: (u16, u16)) -> Vec<String> {
        let mut terminal = Terminal::new(TestBackend::new(size.0, size.1)).unwrap();
        terminal
            .draw(|frame| {
                draw(
                    frame,
                    s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..size.1)
            .map(|y| {
                (0..size.0)
                    .map(|x| {
                        let cell = &buffer[(x, y)];
                        format!("{}{:?}{:?}", cell.symbol(), cell.fg, cell.bg)
                    })
                    .collect::<Vec<_>>()
                    .join("|")
            })
            .collect()
    }

    #[test]
    fn the_activity_meter_rests_without_animation_and_reads_by_shape_in_mono() {
        let mut s = Snapshot::default();
        s.status = "running".into();
        let p = Palette::new(false);
        let at = |tick, animate, mono| {
            activity_meter_for(&s, &p, 20, tick, animate, mono)
                .iter()
                .map(|span| format!("{}{:?}", span.content, span.style.fg))
                .collect::<Vec<_>>()
        };
        assert_eq!(at(0, false, false), at(77, false, false), "static when off");
        assert_ne!(at(0, true, false), at(40, true, false), "moves when on");
        let shapes: String = activity_meter_for(&s, &p, 20, 0, false, true)
            .iter()
            .map(|span| span.content.to_string())
            .collect();
        assert!(shapes.contains('━') && shapes.contains('─'), "{shapes}");
        assert_eq!(
            shapes.chars().filter(|c| *c == '━').count(),
            4,
            "a fifth of 20"
        );
    }

    /// With animations off nothing may depend on the frame clock: every row of a busy screen
    /// (recording mark, activity word and meter, live strip, folding group, write head, queue)
    /// is the same at tick 0 and a much later tick, apart from the braille spinner itself.
    #[test]
    fn the_screen_is_the_same_at_every_tick() {
        let s = rich();
        let mut cache = Cache::default();
        cache.animate = false;
        cache.voice_levels = vec![0.2, 0.9, 0.4];
        cache.voice_elapsed = 7;
        let first = screen(&s, &mut cache, (100, 30));
        cache.spin = 53;
        cache.activity_frame = 701;
        let later = screen(&s, &mut cache, (100, 30));
        let differing: Vec<usize> = (0..first.len()).filter(|&y| first[y] != later[y]).collect();
        let strip = |rows: &[String]| {
            differing
                .iter()
                .map(|&y| {
                    rows[y]
                        .split('|')
                        .map(|cell| {
                            cell.split("Rgb")
                                .next()
                                .unwrap_or("")
                                .split("Reset")
                                .next()
                                .unwrap_or("")
                        })
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
        };
        // The same two ticks do move the screen with animations on (the test has teeth).
        cache.animate = true;
        cache.spin = 0;
        cache.activity_frame = 0;
        let on_first = screen(&s, &mut cache, (100, 30));
        cache.spin = 53;
        cache.activity_frame = 701;
        let on_later = screen(&s, &mut cache, (100, 30));
        assert_ne!(on_first, on_later);
        assert!(
            differing.is_empty(),
            "rows {differing:?} move with the clock while animations are off:\n{:#?}\n{:#?}",
            strip(&first),
            strip(&later)
        );
    }
}

#[cfg(test)]
mod live_pane_tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    fn call(rows: usize) -> Snapshot {
        let mut s = Snapshot::default();
        s.live.active = true;
        s.live.state = "active".into();
        s.live.operation = Some(serde_json::json!({"kind": "live_toggle"}));
        s.live.detail = (0..rows)
            .map(|i| format!("voice\tGPT\trow number {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        s
    }
    fn paint(s: &Snapshot, cache: &mut Cache) -> (Regions, Vec<String>) {
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut regions = Regions::default();
        terminal
            .draw(|frame| {
                regions = draw(
                    frame,
                    s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer().clone();
        let lines = (0..40)
            .map(|y| {
                (0..100)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect()
            })
            .collect();
        (regions, lines)
    }

    #[test]
    fn pane_sits_between_the_composer_and_the_bottom_bar_and_shrinks_the_transcript() {
        let area = Rect::new(0, 0, 100, 40);
        let mut s = Snapshot::default();
        let before = regions(area, &s, 8, false);
        s = call(3);
        let r = regions(area, &s, 8, false);
        assert_eq!(r.live.height, crate::live_bar::PANE_ROWS);
        assert_eq!(r.transcript.bottom(), r.composer.y, "the transcript pays, not the composer");
        assert_eq!(r.live.y, r.composer.bottom(), "directly under the composer");
        assert_eq!(r.live.bottom(), r.bottom_bar.y, "directly above the bottom bar");
        assert!(r.live.bottom() <= r.bottom_bar.y && r.bottom_bar.height == 1);
        assert_eq!(r.composer.height, before.composer.height);
        assert_eq!(
            r.transcript.height + crate::live_bar::PANE_ROWS,
            before.transcript.height
        );
    }

    #[test]
    fn expanded_pane_reaches_the_top_bar_and_leaves_one_transcript_row() {
        let area = Rect::new(0, 0, 100, 40);
        let mut s = call(30);
        s.live_expanded = true;
        let r = regions(area, &s, 8, false);
        assert_eq!(r.transcript.height, 1);
        assert_eq!(r.composer.y, r.transcript.bottom());
        assert_eq!(r.live.y, r.composer.bottom());
        assert_eq!(r.live.bottom(), r.bottom_bar.y);
        assert!(r.live.height > crate::live_bar::PANE_ROWS);
    }

    #[test]
    fn newest_rows_show_with_a_more_notice_and_scrolling_announces_newer() {
        let mut cache = Cache::default();
        let s = call(12);
        let (r, lines) = paint(&s, &mut cache);
        let at = |y: u16| lines[usize::from(y)].clone();
        assert!(at(r.live.y).contains("LISTENING"), "{}", at(r.live.y));
        assert!(at(r.live.y).contains("▁▁▁▁▁▁▁▁"), "{}", at(r.live.y));
        assert!(at(r.live.y).contains("↑ 7 more"), "{}", at(r.live.y));
        assert!(at(r.live.y).contains("⤢ expand"));
        assert!(at(r.live.bottom() - 1).contains("row number 11"));
        assert!(!lines.iter().any(|l| l.contains("row number 6")));
        assert!(cache.live_button.is_some_and(|b| b.y == r.live.y));
        assert_eq!(cache.live_max_back, 7);
        cache.live_back = 99;
        let (r, lines) = paint(&s, &mut cache);
        assert_eq!(cache.live_back, 7);
        assert!(lines[usize::from(r.live.y + 1)].contains("row number 0"));
        assert!(lines[usize::from(r.live.bottom() - 1)].contains("↓ 7 newer"));
        assert!(!lines[usize::from(r.live.y)].contains("more"));
    }

    #[test]
    fn revealed_rows_come_from_the_transcript_and_show_the_header_first() {
        let area = Rect::new(0, 0, 100, 40);
        let mut s = call(12);
        let full = regions(area, &s, 8, false);
        for shown in 1..crate::live_bar::PANE_ROWS {
            s.live_reveal = Some(shown);
            let r = regions(area, &s, 8, false);
            assert_eq!(r.live.height, shown);
            assert_eq!(r.live.y, r.composer.bottom());
            assert_eq!(r.live.bottom(), r.bottom_bar.y);
            assert_eq!(r.composer.height, full.composer.height);
            assert_eq!(r.bottom_bar, full.bottom_bar, "the bottom bar never moves");
            assert_eq!(r.composer.bottom() + shown, r.bottom_bar.y, "the composer rises as it grows");
            assert_eq!(r.transcript.height, full.transcript.height + (6 - shown));
        }
        let mut cache = Cache::default();
        s.live_reveal = Some(2);
        cache.live_anim.target = crate::live_bar::PANE_ROWS;
        let (r, lines) = paint(&s, &mut cache);
        assert!(lines[usize::from(r.live.y)].contains("LISTENING"));
        assert!(lines[usize::from(r.live.y + 1)].contains("row number 7"), "top rows first");
        assert!(!lines.iter().any(|l| l.contains("row number 11")), "the rest is still hidden");
        assert!(lines[usize::from(r.bottom_bar.y)].contains('─'), "the bottom bar stays put");
        s.live_reveal = Some(0);
        assert_eq!(regions(area, &s, 8, false).live.height, 0);
    }

    #[test]
    fn tiny_terminals_keep_the_composer_and_bar_on_screen() {
        for light in [false, true] {
            for centered in [false, true] {
                for drawer in [false, true] {
                    for (w, h) in [(40u16, 10u16), (60, 12), (60, 20), (100, 24)] {
                        let mut s = call(12);
                        s.theme = if light { "nexus-light" } else { "nexus-dark" }.into();
                        s.centered_layout = centered;
                        s.details_sidebar = drawer;
                        let area = Rect::new(0, 0, w, h);
                        let ch = composer_height(area, &Editor::default(), &s);
                        let r = regions(area, &s, ch, false);
                        if h >= 12 {
                            assert!(r.live.bottom() <= r.bottom_bar.y, "{w}x{h}");
                            assert!(r.bottom_bar.bottom() <= h, "{w}x{h} bar on screen");
                            assert!(r.transcript.height >= 1, "{w}x{h} transcript keeps a row");
                            assert_eq!(r.live.y, r.composer.bottom(), "{w}x{h}");
                            assert_eq!(r.composer.height + r.bottom_bar.height, ch, "{w}x{h}");
                        }
                        let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
                        let mut cache = Cache::default();
                        terminal
                            .draw(|f| {
                                draw(
                                    f, &s, &Editor::default(), &Editor::default(),
                                    &Editor::default(), "", 0, 0, 0, &mut cache, true, false,
                                    false, false, 0, 0,
                                );
                            })
                            .unwrap();
                    }
                }
            }
        }
    }

    #[test]
    fn button_label_follows_the_expanded_state() {
        let mut cache = Cache::default();
        let mut s = call(12);
        s.live_expanded = true;
        let (r, lines) = paint(&s, &mut cache);
        assert!(lines[usize::from(r.live.y)].contains("⤡ collapse"));
        let b = cache.live_button.unwrap();
        assert!(r.live.contains((b.x, b.y).into()));
        s.live.active = false;
        let (r, _) = paint(&s, &mut cache);
        assert_eq!(r.live.height, 0);
        assert!(cache.live_button.is_none());
    }

    #[test]
    fn muted_badge_and_the_count_share_the_header_on_a_narrow_screen() {
        let mut cache = Cache::default();
        let mut s = call(12);
        s.live.muted = true;
        s.live.phase = "muted".into();
        s.live.started = crate::live_bar::now_epoch() - 12.0;
        let (r, lines) = paint(&s, &mut cache);
        let head = &lines[usize::from(r.live.y)];
        assert!(
            head.contains("MUTED")
                && head.contains("00:12")
                && head.contains("↑ 7 more")
                && head.contains("⤢ expand"),
            "{head}"
        );
    }

    #[test]
    fn composer_carries_nothing_of_the_call() {
        let mut cache = Cache::default();
        let (_, lines) = paint(&call(12), &mut cache);
        let r = regions(Rect::new(0, 0, 100, 40), &call(12), 8, false);
        for y in r.composer.y..r.composer.bottom() {
            assert!(
                !lines[usize::from(y)].contains("LISTENING"),
                "{}",
                lines[usize::from(y)]
            );
        }
    }

    #[test]
    fn a_call_on_another_session_is_a_one_row_header() {
        let mut cache = Cache::default();
        let mut s = call(0);
        s.live.elsewhere = "work".into();
        s.live.started = crate::live_bar::now_epoch() - 12.0;
        let (r, lines) = paint(&s, &mut cache);
        assert_eq!(r.live.height, 1);
        assert_eq!(r.live.y, r.composer.bottom());
        let head = &lines[usize::from(r.live.y)];
        assert!(
            head.contains("LISTENING") && head.contains("in work") && head.contains("/live stop"),
            "{head}"
        );
        assert!(cache.live_button.is_none());
        s.live_expanded = true;
        assert_eq!(
            regions(Rect::new(0, 0, 100, 40), &s, 8, false).live.height,
            1
        );
    }
}

#[cfg(test)]
mod image_preview_tests {
    use super::*;
    use ratatui::{backend::TestBackend, buffer::Buffer, style::Color, Terminal};

    /// A real, decodable PNG (not placeholder bytes).
    fn png() -> String {
        let image = image::RgbaImage::from_fn(32, 32, |x, y| {
            image::Rgba([255, (x * 8) as u8, (y * 8) as u8, 255])
        });
        let mut bytes = Vec::new();
        image::DynamicImage::ImageRgba8(image)
            .write_to(&mut std::io::Cursor::new(&mut bytes), image::ImageFormat::Png)
            .unwrap();
        base64::engine::general_purpose::STANDARD.encode(bytes)
    }

    fn snapshot(open: bool) -> Snapshot {
        let mut s = Snapshot::default();
        if open {
            s.panel_title = "Submitted image".into();
            s.panel_format = "image".into();
            s.panel_lines = vec!["Media type: image/png".into(), "Size: 1 bytes".into()];
            s.preview_image = png();
        }
        s
    }

    fn paint(terminal: &mut Terminal<TestBackend>, s: &Snapshot, cache: &mut Cache) -> Buffer {
        terminal
            .draw(|frame| {
                draw(
                    frame,
                    s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                );
            })
            .unwrap();
        terminal.backend().buffer().clone()
    }

    /// Cells of the image area that carry the picture: a glyph drawn in a real colour.
    fn painted(buffer: &Buffer, area: Rect) -> usize {
        (area.top()..area.bottom())
            .flat_map(|y| (area.left()..area.right()).map(move |x| (x, y)))
            .filter(|&(x, y)| {
                let cell = &buffer[(x, y)];
                cell.symbol() != " " && !matches!(cell.fg, Color::Reset)
            })
            .count()
    }

    #[test]
    fn a_decodable_image_paints_its_area() {
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut cache = Cache::default();
        let buffer = paint(&mut terminal, &snapshot(true), &mut cache);
        assert!(cache.image_protocol.is_some(), "the PNG decodes");
        assert!(cache.image_area.area() > 0);
        assert!(painted(&buffer, cache.image_area) > 0, "picture cells are not blank");
    }

    #[test]
    fn monochrome_keeps_the_picture_colours_but_strips_the_rest() {
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut cache = Cache::default();
        cache.mono = true;
        let buffer = paint(&mut terminal, &snapshot(true), &mut cache);
        assert!(painted(&buffer, cache.image_area) > 0, "mono must not blank the picture");
        let area = cache.image_area;
        assert!(buffer.content().iter().enumerate().all(|(index, cell)| {
            let (x, y) = ((index % 100) as u16, (index / 100) as u16);
            area.contains((x, y).into()) || matches!(cell.fg, Color::Reset)
        }));
    }

    #[test]
    fn closing_the_panel_or_resizing_makes_the_next_open_transmit_again() {
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        let mut cache = Cache::default();
        paint(&mut terminal, &snapshot(true), &mut cache);
        assert!(cache.image_protocol.is_some());
        paint(&mut terminal, &snapshot(false), &mut cache);
        assert!(cache.image_protocol.is_none(), "a closed preview keeps no protocol state");
        assert!(cache.image_source.is_empty());
        paint(&mut terminal, &snapshot(true), &mut cache);
        assert!(cache.image_protocol.is_some(), "reopening decodes afresh");
        let first = cache.image_screen;
        terminal.backend_mut().resize(90, 30);
        let buffer = paint(&mut terminal, &snapshot(true), &mut cache);
        assert_ne!(cache.image_screen, first, "a resize is noticed");
        assert!(cache.image_protocol.is_some());
        assert!(painted(&buffer, cache.image_area) > 0);
    }
}

#[cfg(test)]
mod thinking_shimmer_tests {
    use super::*;

    fn text_of(spans: &[Span<'static>]) -> String {
        spans.iter().map(|s| s.content.as_ref()).collect()
    }

    #[test]
    fn shimmer_text_is_the_real_word_at_every_tick() {
        for light in [false, true] {
            let p = Palette::new(light);
            for word in ["Waiting for response…", "界 wide 👩‍💻 mix…", "a", ""] {
                for tick in 0..SHIMMER_FRAMES * 2 {
                    for animate in [true, false] {
                        let spans = shimmer_spans(word, tick, animate, false, &p);
                        assert_eq!(text_of(&spans), word);
                    }
                }
            }
        }
    }

    #[test]
    fn shimmer_colours_stay_inside_the_grey_pair() {
        for light in [false, true] {
            let p = Palette::new(light);
            let (hi, lo) = shimmer_greys(&p);
            assert_ne!(hi, lo);
            let channel = |c: Color| match c {
                Color::Rgb(r, _, _) => r,
                _ => panic!("rgb expected"),
            };
            let (min, max) = (channel(hi).min(channel(lo)), channel(hi).max(channel(lo)));
            for tick in 0..SHIMMER_FRAMES {
                for span in shimmer_spans("Waiting for response…", tick, true, false, &p) {
                    let fg = span.style.fg.unwrap();
                    assert_ne!(fg, p.purple);
                    assert_ne!(fg, p.accent);
                    let Color::Rgb(r, g, b) = fg else { panic!() };
                    assert!(r == g && g == b, "grey: {fg:?}");
                    assert!((min..=max).contains(&r), "{fg:?} outside {min}..={max}");
                }
            }
        }
    }

    #[test]
    fn shimmer_moves_when_animated_and_rests_when_not() {
        let p = Palette::new(false);
        let word = "Waiting for response…";
        let colours = |tick, animate| -> Vec<Option<Color>> {
            shimmer_spans(word, tick, animate, false, &p)
                .iter()
                .map(|s| s.style.fg)
                .collect()
        };
        assert_ne!(colours(10, true), colours(20, true));
        assert_eq!(colours(10, true), colours(10 + SHIMMER_FRAMES, true));
        assert_eq!(colours(10, false), colours(20, false));
        let rest = shimmer_spans(word, 10, false, false, &p);
        assert_eq!(rest.len(), 1);
        assert_eq!(rest[0].style.fg, Some(shimmer_greys(&p).1));
    }

    #[test]
    fn shimmer_is_plain_text_in_monochrome() {
        let p = Palette::new(false);
        let spans = shimmer_spans("Thinking…", 7, true, true, &p);
        assert_eq!(spans.len(), 1);
        assert_eq!(spans[0].content, "Thinking…");
        assert_eq!(spans[0].style.fg, None);
    }
}
