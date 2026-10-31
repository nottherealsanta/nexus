//! Markdown to styled rows (DESIGN_PLAN.md D08): bold headings (H1 underlined),
//! inline code on the panel colour, fences as a panel block with a header row
//! and inset code (no rule, no rail), quotes behind a `│`, hanging-indent
//! lists and aligned tables. HTML is literal; URLs stay visible.
use crate::theme::Theme;
use crate::{mermaid, mermaid_text};
use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
use std::{
    cell::RefCell,
    collections::{hash_map::DefaultHasher, HashMap},
    hash::{Hash, Hasher},
    rc::Rc,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

/// Theme mapping for the old Nexus Markdown renderer.
pub struct Palette {
    text: Color,
    quiet: Color,
    muted: Color,
    accent: Color,
    border: Color,
    border_strong: Color,
    blue: Color,
    success: Color,
    warning: Color,
    panel: Color,
    ascii: bool,
}
impl Palette {
    pub fn from_theme(theme: &Theme) -> Self {
        let color = |name| theme.color(name).unwrap_or(Color::Reset);
        Self {
            text: color("text.primary"),
            quiet: color("text.muted"),
            muted: color("text.secondary"),
            accent: color("interactive.accent"),
            border: color("border.default"),
            border_strong: color("border.strong"),
            blue: color("status.info"),
            success: color("status.success"),
            warning: color("status.warning"),
            panel: color("surface.panel"),
            ascii: theme.opts.ascii,
        }
    }

    /// Every input (colour or glyph choice) that changes rendered Markdown.
    pub fn fingerprint(&self) -> u64 {
        let mut h = DefaultHasher::new();
        self.text.hash(&mut h);
        self.quiet.hash(&mut h);
        self.muted.hash(&mut h);
        self.accent.hash(&mut h);
        self.border.hash(&mut h);
        self.border_strong.hash(&mut h);
        self.blue.hash(&mut h);
        self.success.hash(&mut h);
        self.warning.hash(&mut h);
        self.panel.hash(&mut h);
        self.ascii.hash(&mut h);
        h.finish()
    }
}

// Display controls rather than passing terminal escape sequences through. The
// transcript keeps the original message separately for copying and caching.
fn terminal_safe(text: &str) -> String {
    text.chars()
        .map(|ch| {
            if ch.is_control() && ch != '\n' && ch != '\t' {
                format!("\\u{{{:04x}}}", ch as u32)
            } else {
                ch.to_string()
            }
        })
        .collect()
}

/// Word-wrap styled graphemes, adapted from old Nexus `transcript::wrap`.
///
/// Wrapping tracks borrowed grapheme slices and only allocates when a row is
/// turned into styled runs, merging adjacent cells that share a style. The
/// result is visually identical to per-grapheme spans but has far fewer
/// allocations and later clones (timeline caching depends on that).
pub fn wrap_spans(spans: &[Span<'static>], width: usize) -> Vec<Vec<Span<'static>>> {
    #[derive(Clone, Copy)]
    struct Cell<'a> {
        text: &'a str,
        style: Style,
        width: usize,
        space: bool,
    }

    let width = width.max(1);
    let mut rows: Vec<Vec<Cell<'_>>> = Vec::new();
    let mut row: Vec<Cell<'_>> = Vec::new();
    let mut cols = 0;
    let mut break_at = None;
    for span in spans {
        for grapheme in span.content.graphemes(true) {
            let w = grapheme.width();
            let space = grapheme.chars().all(char::is_whitespace);
            let mut dropped = false;
            while cols + w > width && !row.is_empty() {
                if space {
                    rows.push(std::mem::take(&mut row));
                    cols = 0;
                    break_at = None;
                    dropped = true;
                    break;
                }
                if let Some(at) = break_at.filter(|at| *at > 0 && *at < row.len()) {
                    let tail = row.split_off(at);
                    while row.last().is_some_and(|cell| cell.space) {
                        row.pop();
                    }
                    rows.push(std::mem::replace(&mut row, tail));
                    cols = row.iter().map(|cell| cell.width).sum();
                } else {
                    rows.push(std::mem::take(&mut row));
                    cols = 0;
                }
                break_at = None;
            }
            if dropped {
                continue;
            }
            row.push(Cell {
                text: grapheme,
                style: span.style,
                width: w,
                space,
            });
            cols += w;
            if space {
                break_at = Some(row.len());
            }
        }
    }
    rows.push(row);
    rows.into_iter()
        .map(|cells| {
            let mut runs: Vec<(String, Style)> = Vec::new();
            for cell in cells {
                if let Some((text, _)) = runs.last_mut().filter(|(_, s)| *s == cell.style) {
                    text.push_str(cell.text);
                } else {
                    runs.push((cell.text.to_owned(), cell.style));
                }
            }
            runs.into_iter()
                .map(|(text, style)| Span::styled(text, style))
                .collect()
        })
        .collect()
}

/// One logical row before wrapping: a first-row prefix (wrapped rows hang under
/// its width) and an optional background that fills the whole row.
#[derive(Default)]
pub struct Row {
    pub prefix: Vec<Span<'static>>,
    pub spans: Vec<Span<'static>>,
    pub bg: Option<ratatui::style::Color>,
    pub blank: bool,
    /// Index of the fenced code block this row draws (see [`fence_sources`]).
    pub fence: Option<usize>,
    pub image: Option<(String, usize, usize)>,
}

struct Level {
    ordered: Option<u64>,
}

struct Walker<'a> {
    p: &'a Palette,
    width: usize,
    rows: Vec<Row>,
    cur: Vec<Span<'static>>,
    marker: Option<String>,
    levels: Vec<Level>,
    quote: usize,
    bold: usize,
    italic: usize,
    strike: usize,
    code_block: Option<String>,
    heading: Option<HeadingLevel>,
    links: Vec<String>,
    /// `cur` length at each open link, to read back its visible text.
    link_starts: Vec<usize>,
    table: Option<Table>,
    item_depth: usize,
    fences: usize,
}

#[derive(Default)]
struct Table {
    rows: Vec<Vec<String>>,
    head_rows: usize,
    in_head: bool,
    cell: String,
}

impl<'a> Walker<'a> {
    fn indent(&self) -> usize {
        self.levels.len().saturating_sub(1) * 3 + if self.levels.is_empty() { 0 } else { 3 }
    }
    fn prefix(&mut self) -> (Vec<Span<'static>>, usize) {
        let mut spans = Vec::new();
        let mut hang = 0;
        if self.quote > 0 {
            let style = Style::default().fg(self.p.border_strong);
            let rail = if self.p.ascii { "| " } else { "│ " };
            for _ in 0..self.quote {
                spans.push(Span::styled(rail, style));
                hang += 2;
            }
        }
        let base = self.indent();
        let text_style = Style::default();
        match self.marker.take() {
            Some(marker) => {
                let lead = base.saturating_sub(3);
                spans.push(Span::styled(
                    format!("{}{marker}", " ".repeat(lead)),
                    text_style.fg(self.p.quiet),
                ));
            }
            None => spans.push(Span::styled(" ".repeat(base), text_style)),
        }
        hang += base;
        (spans, hang)
    }
    fn flush(&mut self) {
        if self.cur.is_empty() && self.marker.is_none() {
            return;
        }
        let (prefix, _) = self.prefix();
        let spans = std::mem::take(&mut self.cur);
        self.rows.push(Row {
            prefix,
            spans,
            bg: None,
            blank: false,
            image: None,
            fence: None,
        });
    }
    fn blank(&mut self) {
        if self.rows.last().is_some_and(|row| row.blank) || self.rows.is_empty() {
            return;
        }
        self.rows.push(Row {
            blank: true,
            ..Default::default()
        });
    }
    fn style(&self) -> Style {
        let mut style = Style::default();
        if self.quote > 0 {
            style = style.fg(self.p.muted);
        }
        if self.bold > 0 {
            style = style.add_modifier(Modifier::BOLD);
        }
        if self.italic > 0 {
            style = style.add_modifier(Modifier::ITALIC);
        }
        if self.strike > 0 {
            style = style.add_modifier(Modifier::CROSSED_OUT);
        }
        if let Some(level) = self.heading {
            let color = match level {
                HeadingLevel::H1 | HeadingLevel::H2 => self.p.text,
                _ => self.p.muted,
            };
            style = style.fg(color).add_modifier(Modifier::BOLD);
        }
        if !self.links.is_empty() {
            style = style.fg(self.p.blue).add_modifier(Modifier::UNDERLINED);
        }
        style
    }
    fn text(&mut self, value: &str, style: Style) {
        if let Some(table) = &mut self.table {
            table.cell.push_str(value);
            return;
        }
        for (i, part) in value.split('\n').enumerate() {
            if i > 0 {
                self.flush();
            }
            if !part.is_empty() {
                self.cur.push(Span::styled(part.to_string(), style));
            }
        }
    }
    fn fence(&mut self, body: &str, language: &str, closed: bool) {
        let index = self.fences;
        self.fences += 1;
        if closed
            && language == "mermaid"
            && !self.p.ascii
            && (self.mermaid_art(body, index) || self.mermaid_text(body, index))
        {
            return;
        }
        // As in old Nexus (ratatui-markdown's code block): a rounded header with
        // the language, each code line behind a `│`, a rounded footer, with no
        // background; the code is drawn in the warning colour.
        let panel = Style::default();
        let (top, rail, bottom) = if self.p.ascii {
            ("+-", "| ", "+-")
        } else {
            ("╭─", "│ ", "╰─")
        };
        let header = if language.is_empty() {
            top.to_owned()
        } else {
            format!("{top} {language}")
        };
        self.cur.push(Span::styled(header, panel.fg(self.p.quiet)));
        self.fence_row(index);
        for line in body.replace('\t', "    ").lines() {
            self.cur.push(Span::styled(rail, panel.fg(self.p.quiet)));
            self.cur
                .push(Span::styled(line.to_owned(), panel.fg(self.p.warning)));
            self.fence_row(index);
        }
        self.cur.push(Span::styled(bottom, panel.fg(self.p.quiet)));
        self.fence_row(index);
    }
    /// Push the diagram rows plus a quiet `◇ mermaid` label. `false` means the
    /// source cannot be drawn within the row width and nothing was pushed.
    fn mermaid_art(&mut self, body: &str, index: usize) -> bool {
        let avail = self.width.saturating_sub(self.quote * 2 + self.indent());
        let Some(art) = mermaid_cached(body, self.p, avail) else {
            return false;
        };
        for line in art.iter() {
            let (prefix, _) = self.prefix();
            let spans = line.spans.to_vec();
            self.rows.push(Row {
                prefix,
                spans,
                bg: None,
                blank: false,
                image: None,
                fence: Some(index),
            });
        }
        let label = Style::default().fg(self.p.quiet);
        self.cur
            .push(Span::styled("◇ mermaid · copy source", label));
        let (prefix, _) = self.prefix();
        let spans = std::mem::take(&mut self.cur);
        self.rows.push(Row {
            prefix,
            spans,
            bg: None,
            blank: false,
            image: None,
            fence: Some(index),
        });
        true
    }
    /// Pie / Gantt / timeline as text rows: a half-block raster of their wide SVG
    /// is unreadable (docs/ratatui-parity.md), so they never go through it.
    fn mermaid_text(&mut self, body: &str, index: usize) -> bool {
        let avail = self.width.saturating_sub(self.quote * 2 + self.indent());
        let Some(rows) = mermaid_text::render(body, avail) else {
            return false;
        };
        let p = self.p;
        let styled = |tone: mermaid_text::Tone| match tone {
            mermaid_text::Tone::Title => Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
            mermaid_text::Tone::Heading => Style::default().fg(p.text).add_modifier(Modifier::BOLD),
            mermaid_text::Tone::Label => Style::default().fg(p.text),
            mermaid_text::Tone::Bar => Style::default().fg(p.accent),
            mermaid_text::Tone::Quiet => Style::default().fg(p.quiet),
        };
        for row in rows {
            let (prefix, _) = self.prefix();
            let spans = row
                .into_iter()
                .map(|(text, tone)| Span::styled(text, styled(tone)))
                .collect();
            self.rows.push(Row {
                prefix,
                spans,
                bg: None,
                blank: false,
                image: None,
                fence: Some(index),
            });
        }
        let label = Style::default().fg(self.p.quiet);
        self.cur
            .push(Span::styled("◇ mermaid · copy source", label));
        let (prefix, _) = self.prefix();
        let spans = std::mem::take(&mut self.cur);
        self.rows.push(Row {
            prefix,
            spans,
            bg: None,
            blank: false,
            image: None,
            fence: Some(index),
        });
        true
    }
    fn fence_row(&mut self, index: usize) {
        let (mut prefix, _) = self.prefix();
        prefix.push(Span::raw("  "));
        let spans = std::mem::take(&mut self.cur);
        self.rows.push(Row {
            prefix,
            spans,
            bg: None,
            blank: false,
            image: None,
            fence: Some(index),
        });
    }
    fn finish_table(&mut self) {
        let Some(table) = self.table.take() else {
            return;
        };
        let columns = table.rows.iter().map(Vec::len).max().unwrap_or(0);
        if columns == 0 {
            return;
        }
        // Boxed table fitted to the row width: every cell wraps inside its
        // column, so no row is ever wrapped by the transcript (which would
        // tear the borders apart).
        let avail = self
            .width
            .saturating_sub(self.quote * 2 + self.indent())
            .max(columns * 4 + 1);
        let overhead = columns * 3 + 1;
        let budget = avail.saturating_sub(overhead).max(columns);
        let natural: Vec<usize> = (0..columns)
            .map(|c| {
                table
                    .rows
                    .iter()
                    .map(|row| row.get(c).map_or(0, |cell| cell.width()))
                    .max()
                    .unwrap_or(0)
                    .max(1)
            })
            .collect();
        let widths = fit_columns(&natural, &longest_words(&table.rows, columns), budget);
        let rule = |l: &str, m: &str, r: &str| {
            let body = widths
                .iter()
                .map(|w| "─".repeat(w + 2))
                .collect::<Vec<_>>()
                .join(m);
            Span::styled(format!("{l}{body}{r}"), Style::default().fg(self.p.quiet))
        };
        let border = Style::default().fg(self.p.quiet);
        self.cur.push(rule("┌", "┬", "┐"));
        self.flush();
        for (i, cells) in table.rows.iter().enumerate() {
            let head = i < table.head_rows;
            let style = if head {
                Style::default().add_modifier(Modifier::BOLD)
            } else {
                Style::default()
            };
            let wrapped: Vec<Vec<String>> = (0..columns)
                .map(|c| wrap_cell(cells.get(c).map_or("", String::as_str), widths[c]))
                .collect();
            let height = wrapped.iter().map(Vec::len).max().unwrap_or(1);
            for line in 0..height {
                self.cur.push(Span::styled("│", border));
                for c in 0..columns {
                    let text = wrapped[c].get(line).map_or("", String::as_str);
                    let pad = widths[c].saturating_sub(text.width());
                    self.cur
                        .push(Span::styled(format!(" {text}{} ", " ".repeat(pad)), style));
                    self.cur.push(Span::styled("│", border));
                }
                self.flush();
            }
            if head && i + 1 == table.head_rows {
                self.cur.push(rule("├", "┼", "┤"));
                self.flush();
            }
        }
        self.cur.push(rule("└", "┴", "┘"));
        self.flush();
        self.blank();
    }
}

fn longest_words(rows: &[Vec<String>], columns: usize) -> Vec<usize> {
    (0..columns)
        .map(|c| {
            rows.iter()
                .filter_map(|row| row.get(c))
                .flat_map(|cell| cell.split_whitespace())
                .map(|word| word.width())
                .max()
                .unwrap_or(1)
                .max(1)
        })
        .collect()
}

/// Column widths summing to at most `budget`: natural widths when they fit,
/// otherwise shrink the widest columns first, never below the longest word
/// (or 3) unless even that cannot fit.
fn fit_columns(natural: &[usize], words: &[usize], budget: usize) -> Vec<usize> {
    let mut widths = natural.to_vec();
    let floor: Vec<usize> = words
        .iter()
        .zip(natural)
        .map(|(w, n)| (*w).clamp(3, 16).min(*n))
        .collect();
    while widths.iter().sum::<usize>() > budget {
        let pick = (0..widths.len())
            .filter(|&c| widths[c] > floor[c])
            .max_by_key(|&c| widths[c]);
        match pick {
            Some(c) => widths[c] -= 1,
            None => break,
        }
    }
    // Last resort: the longest words alone do not fit.
    while widths.iter().sum::<usize>() > budget {
        match (0..widths.len())
            .filter(|&c| widths[c] > 1)
            .max_by_key(|&c| widths[c])
        {
            Some(c) => widths[c] -= 1,
            None => break,
        }
    }
    widths
}

fn wrap_cell(text: &str, width: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let mut cur = String::new();
    let mut cur_w = 0;
    for word in text.split_whitespace() {
        let mut word = word.to_string();
        while word.width() > width {
            if cur_w > 0 {
                lines.push(std::mem::take(&mut cur));
                cur_w = 0;
            }
            let mut head = String::new();
            let mut head_w = 0;
            let mut rest = String::new();
            for ch in word.chars() {
                let w = ch.width().unwrap_or(0);
                if rest.is_empty() && head_w + w <= width.max(1) {
                    head.push(ch);
                    head_w += w;
                } else {
                    rest.push(ch);
                }
            }
            if head.is_empty() {
                break;
            }
            lines.push(head);
            word = rest;
        }
        let w = word.width();
        if cur_w > 0 && cur_w + 1 + w > width {
            lines.push(std::mem::take(&mut cur));
            cur_w = 0;
        }
        if cur_w > 0 {
            cur.push(' ');
            cur_w += 1;
        }
        cur.push_str(&word);
        cur_w += w;
    }
    if cur_w > 0 || lines.is_empty() {
        lines.push(cur);
    }
    lines
}

type Art = Rc<Vec<Line<'static>>>;
const ART_CACHE_LIMIT: usize = 64;

thread_local! {
    // (source + palette hash, available width) -> drawn art, or None when the
    // diagram cannot be drawn there. Bounded: cleared when full.
    static ART_CACHE: RefCell<HashMap<(u64, usize), Option<Art>>> = RefCell::new(HashMap::new());
}

/// Lay a mermaid diagram out once per `(source, palette, width)`, so
/// scrolling and re-wrapping do not repeat the layout.
fn mermaid_cached(body: &str, p: &Palette, width: usize) -> Option<Art> {
    let mut hasher = DefaultHasher::new();
    body.hash(&mut hasher);
    [p.text, p.border_strong, p.quiet, p.muted].hash(&mut hasher);
    let key = (hasher.finish(), width);
    if let Some(hit) = ART_CACHE.with(|c| c.borrow().get(&key).cloned()) {
        return hit;
    }
    let styles = mermaid::MermaidStyles {
        border: Style::default().fg(p.border_strong),
        node_text: Style::default().fg(p.text),
        edge: Style::default().fg(p.quiet),
        edge_label: Style::default().fg(p.muted),
    };
    let art = mermaid::render(body, &styles, Some(width))
        .filter(|art| {
            art.styled_lines
                .iter()
                .all(|line| line.spans.iter().map(|s| s.content.width()).sum::<usize>() <= width)
        })
        .map(|art| Rc::new(art.styled_lines));
    ART_CACHE.with(|c| {
        let mut cache = c.borrow_mut();
        if cache.len() >= ART_CACHE_LIMIT {
            cache.clear();
        }
        cache.insert(key, art.clone());
    });
    art
}

/// Whether the fenced block spanning `block` (opening through closing line)
/// ends with a closing fence. pulldown-cmark ends an unclosed fence at the end
/// of the document, so the parser alone cannot tell.
fn fence_is_closed(block: &str) -> bool {
    let strip = |line: &str| -> String {
        line.trim_start_matches(|c: char| c == '>' || c.is_whitespace())
            .trim_end()
            .to_string()
    };
    let lines: Vec<String> = block.trim_end().lines().map(strip).collect();
    let (Some(open), Some(close)) = (lines.first(), lines.last()) else {
        return false;
    };
    if lines.len() < 2 {
        return false;
    }
    let Some(mark) = open.chars().next().filter(|c| *c == '`' || *c == '~') else {
        return false;
    };
    let run = open.chars().take_while(|c| *c == mark).count();
    let closing = close.chars().take_while(|c| *c == mark).count();
    closing >= run.max(3) && closing == close.chars().count()
}

/// The literal source of each fenced or indented code block, in the order
/// [`lines`] numbers them; Copy buttons recover their text from here.
pub fn fence_sources(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut body: Option<String> = None;
    for event in Parser::new_ext(
        text,
        Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TABLES | Options::ENABLE_TASKLISTS,
    ) {
        match event {
            Event::Start(Tag::CodeBlock(_)) => body = Some(String::new()),
            Event::Text(value) => {
                if let Some(body) = &mut body {
                    body.push_str(&value);
                }
            }
            Event::End(TagEnd::CodeBlock) => {
                if let Some(body) = body.take() {
                    out.push(body.trim_end_matches('\n').to_string());
                }
            }
            _ => {}
        }
    }
    out
}

pub fn lines(text: &str, p: &Palette, width: usize) -> Vec<Row> {
    let safe_text = terminal_safe(text);
    let mut w = Walker {
        p,
        width,
        rows: Vec::new(),
        cur: Vec::new(),
        marker: None,
        levels: Vec::new(),
        quote: 0,
        bold: 0,
        italic: 0,
        strike: 0,
        code_block: None,
        heading: None,
        links: Vec::new(),
        link_starts: Vec::new(),
        table: None,
        item_depth: 0,
        fences: 0,
    };
    let mut fence_body = String::new();
    for (event, range) in Parser::new_ext(
        &safe_text,
        Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TABLES | Options::ENABLE_TASKLISTS,
    )
    .into_offset_iter()
    {
        match event {
            Event::Start(Tag::Strong) => w.bold += 1,
            Event::End(TagEnd::Strong) => w.bold = w.bold.saturating_sub(1),
            Event::Start(Tag::Emphasis) => w.italic += 1,
            Event::End(TagEnd::Emphasis) => w.italic = w.italic.saturating_sub(1),
            Event::Start(Tag::Strikethrough) => w.strike += 1,
            Event::End(TagEnd::Strikethrough) => w.strike = w.strike.saturating_sub(1),
            Event::Start(Tag::Heading { level, .. }) => w.heading = Some(level),
            Event::End(TagEnd::Heading(level)) => {
                let underline = (level == HeadingLevel::H1).then(|| {
                    let text: usize = w.cur.iter().map(|s| s.content.width()).sum();
                    text.min(w.width.saturating_sub(w.indent() + w.quote * 2))
                        .max(1)
                });
                w.flush();
                w.heading = None;
                if let Some(len) = underline {
                    let rule = if p.ascii { "-" } else { "─" };
                    w.cur
                        .push(Span::styled(rule.repeat(len), Style::default().fg(p.quiet)));
                    w.flush();
                }
                w.blank();
            }
            Event::Start(Tag::BlockQuote(_)) => w.quote += 1,
            Event::End(TagEnd::BlockQuote(_)) => {
                w.flush();
                w.quote = w.quote.saturating_sub(1);
                w.blank();
            }
            Event::Start(Tag::CodeBlock(kind)) => {
                w.flush();
                fence_body.clear();
                w.code_block = Some(match kind {
                    CodeBlockKind::Fenced(language) => {
                        language.split_whitespace().next().unwrap_or("").to_string()
                    }
                    CodeBlockKind::Indented => String::new(),
                });
            }
            Event::End(TagEnd::CodeBlock) => {
                let language = w.code_block.take().unwrap_or_default();
                let body = std::mem::take(&mut fence_body);
                let closed = fence_is_closed(&safe_text[range]);
                w.fence(&body, &language, closed);
                w.blank();
            }
            Event::Start(Tag::List(start)) => {
                w.flush();
                w.levels.push(Level { ordered: start });
            }
            Event::End(TagEnd::List(_)) => {
                w.flush();
                w.levels.pop();
                if w.levels.is_empty() {
                    w.blank();
                }
            }
            Event::Start(Tag::Item) => {
                w.flush();
                w.item_depth += 1;
                let depth = w.levels.len();
                let marker = match w.levels.last_mut() {
                    Some(Level { ordered: Some(n) }) => {
                        let marker = format!("{n}. ");
                        *n += 1;
                        marker
                    }
                    _ => (if depth > 1 { "◦ " } else { "• " }).to_string(),
                };
                w.marker = Some(marker);
            }
            Event::End(TagEnd::Item) => {
                w.flush();
                w.marker = None;
                w.item_depth = w.item_depth.saturating_sub(1);
            }
            Event::Start(Tag::Image { dest_url, .. }) => {
                w.text("[image: ", w.style());
                w.links.push(dest_url.to_string());
            }
            Event::Start(Tag::Link { dest_url, .. }) => {
                w.links.push(dest_url.to_string());
                w.link_starts.push(w.cur.len());
            }
            Event::End(TagEnd::Image) => {
                if let Some(url) = w.links.pop() {
                    w.text(&format!(" ({url})]"), w.style().fg(p.quiet));
                }
            }
            Event::End(TagEnd::Link) => {
                let start = w.link_starts.pop().unwrap_or(0);
                let shown: String = w
                    .cur
                    .get(start..)
                    .unwrap_or_default()
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect();
                if let Some(url) = w.links.pop() {
                    // The URL stays visible (the reader sees where a link goes),
                    // except for a file path, which is the link text, and for a
                    // link whose text already is the URL.
                    if url.contains("://") && shown != url {
                        let style = w.style().fg(p.quiet);
                        w.text(&format!(" ({url})"), style);
                    }
                }
            }
            Event::End(TagEnd::Paragraph) => {
                w.flush();
                if w.item_depth == 0 {
                    w.blank();
                }
            }
            Event::Start(Tag::Table(_)) => {
                w.flush();
                w.table = Some(Table::default());
            }
            Event::End(TagEnd::Table) => w.finish_table(),
            Event::Start(Tag::TableHead) => {
                if let Some(t) = &mut w.table {
                    t.in_head = true;
                    t.rows.push(Vec::new());
                }
            }
            Event::End(TagEnd::TableHead) => {
                if let Some(t) = &mut w.table {
                    t.in_head = false;
                    t.head_rows = t.rows.len();
                }
            }
            Event::Start(Tag::TableRow) => {
                if let Some(t) = &mut w.table {
                    t.rows.push(Vec::new());
                }
            }
            Event::End(TagEnd::TableCell) => {
                if let Some(t) = &mut w.table {
                    let cell = std::mem::take(&mut t.cell);
                    if let Some(row) = t.rows.last_mut() {
                        row.push(cell);
                    }
                }
            }
            Event::Code(value) => {
                if w.table.is_some() {
                    w.text(&value, Style::default());
                } else {
                    // Primary text on the panel, padded by one space; never accent.
                    let mut style = Style::default().fg(p.text).bg(p.panel);
                    if w.bold > 0 {
                        style = style.add_modifier(Modifier::BOLD);
                    }
                    w.cur.push(Span::styled(format!(" {value} "), style));
                }
            }
            Event::Text(value) if w.code_block.is_some() => fence_body.push_str(&value),
            Event::Text(value) | Event::Html(value) | Event::InlineHtml(value) => {
                let style = w.style();
                w.text(&value, style);
            }
            Event::SoftBreak | Event::HardBreak => {
                if w.table.is_some() {
                    w.text(" ", Style::default());
                } else {
                    w.flush();
                }
            }
            Event::Rule => {
                w.flush();
                let rule = "─".repeat(w.width.saturating_sub(8).clamp(3, 60));
                w.cur
                    .push(Span::styled(rule, Style::default().fg(p.border)));
                w.flush();
                w.blank();
            }
            Event::TaskListMarker(done) => {
                let quiet = Style::default().fg(p.quiet);
                if done && !p.ascii {
                    w.cur.push(Span::styled("[", quiet));
                    w.cur
                        .push(Span::styled("✓", Style::default().fg(p.success)));
                    w.cur.push(Span::styled("] ", quiet));
                } else {
                    let mark = if done { "[x] " } else { "[ ] " };
                    w.cur.push(Span::styled(mark, quiet));
                }
            }
            _ => {}
        }
    }
    w.flush();
    while w.rows.last().is_some_and(|row| row.blank) {
        w.rows.pop();
    }
    w.rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::{Options, Theme};

    #[test]
    fn mermaid_diagram_keeps_model_visible_source_and_fallback() {
        let palette = Palette::from_theme(&Theme::new(Options::default()));
        let source = "```mermaid\ngraph TD\n  A[Start] --> B[Finish]\n```";
        let rows = lines(source, &palette, 60);
        let text = rows
            .iter()
            .flat_map(|row| row.prefix.iter().chain(&row.spans))
            .map(|span| span.content.as_ref())
            .collect::<String>();
        assert!(text.contains("◇ mermaid · copy source"), "{text}");
        assert!(!text.contains("A[Start] --> B[Finish]"), "{text}");
        assert_eq!(
            fence_sources(source),
            ["graph TD\n  A[Start] --> B[Finish]"]
        );
        let mono = Options {
            tier: crate::theme::Tier::Mono,
            ..Options::default()
        };
        let mono_rows = lines(source, &Palette::from_theme(&Theme::new(mono)), 60);
        let mono_text: String = mono_rows
            .iter()
            .flat_map(|row| row.spans.iter())
            .map(|span| span.content.as_ref())
            .collect();
        assert!(mono_text.contains("◇ mermaid · copy source"));
        let ascii = Options {
            ascii: true,
            ..Options::default()
        };
        let ascii_rows = lines(source, &Palette::from_theme(&Theme::new(ascii)), 60);
        let ascii_text: String = ascii_rows
            .iter()
            .flat_map(|row| row.spans.iter())
            .map(|span| span.content.as_ref())
            .collect();
        assert!(ascii_text.contains("A[Start] --> B[Finish]"));
        assert!(!ascii_text.contains("source below"));
        let unsupported = lines("```mermaid\nunsupported diagram\n```", &palette, 60);
        let text = unsupported
            .iter()
            .flat_map(|row| row.spans.iter())
            .map(|span| span.content.as_ref())
            .collect::<String>();
        assert!(text.contains("unsupported diagram"));
    }

    #[test]
    fn old_renderer_keeps_links_tables_and_fence_source_visible() {
        let palette = Palette::from_theme(&Theme::new(Options::default()));
        let source = "# Title\n\n[link](https://example.test)\n\n| A | B |\n|---|---|\n| one | two |\n\n```mermaid\ngraph TD\n```";
        let rows = lines(source, &palette, 60);
        let text = rows
            .iter()
            .flat_map(|row| row.prefix.iter().chain(&row.spans))
            .map(|span| span.content.as_ref())
            .collect::<String>();
        assert!(text.contains("Title") && text.contains("https://example.test"));
        assert!(text.contains("one") && text.contains("two") && text.contains("graph TD"));
        assert_eq!(fence_sources(source), ["graph TD"]);
    }

    fn row_text(row: &Row) -> String {
        row.prefix
            .iter()
            .chain(&row.spans)
            .map(|span| span.content.as_ref())
            .collect()
    }

    #[test]
    fn fence_is_a_panel_block_with_a_rounded_frame() {
        let theme = Theme::new(Options::default());
        let palette = Palette::from_theme(&theme);
        let rows = lines("```python\ndef f():\n    return 1\n```", &palette, 40);
        let text: Vec<String> = rows.iter().map(row_text).collect();
        assert_eq!(
            text,
            ["  ╭─ python", "  │ def f():", "  │     return 1", "  ╰─"],
            "header, railed code, footer"
        );
        assert!(rows.iter().all(|r| r.bg.is_none() && r.fence == Some(0)));
        // The frame is muted, the code warning-coloured.
        let muted = theme.color("text.muted");
        assert_eq!(rows[0].spans[0].style.fg, muted);
        assert_eq!(rows[1].spans[1].style.fg, theme.color("status.warning"));
        let bare = lines("```\nx\n```", &palette, 40);
        assert_eq!(row_text(&bare[0]), "  ╭─");
    }

    #[test]
    fn inline_code_is_primary_on_panel_padded_and_never_accent() {
        let theme = Theme::new(Options::default());
        let palette = Palette::from_theme(&theme);
        let rows = lines("use `Loader.load` here", &palette, 60);
        let code = rows[0]
            .spans
            .iter()
            .find(|s| s.content.contains("Loader.load"))
            .unwrap();
        assert_eq!(code.content, " Loader.load ");
        assert_eq!(code.style.fg, theme.color("text.primary"));
        assert_eq!(code.style.bg, theme.color("surface.panel"));
        assert_ne!(code.style.fg, theme.color("interactive.accent"));
    }

    #[test]
    fn headings_quotes_lists_and_links_follow_d08() {
        let theme = Theme::new(Options::default());
        let palette = Palette::from_theme(&theme);
        let src = "# Title\n\n### Small\n\n> quoted\n\n- one\n  - two\n\n[site](https://a.test) and [f](src/a.py:42)";
        let rows = lines(src, &palette, 60);
        let text: Vec<String> = rows.iter().map(row_text).collect();
        assert_eq!(text[0], "Title");
        assert_eq!(text[1], "─────", "H1 underline is as wide as the text");
        assert_eq!(rows[0].spans[0].style.fg, theme.color("text.primary"));
        assert!(rows[0].spans[0].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(rows[1].spans[0].style.fg, theme.color("text.muted"));
        let small = rows.iter().find(|r| row_text(r) == "Small").unwrap();
        assert_eq!(small.spans[0].style.fg, theme.color("text.secondary"));
        let quote = rows
            .iter()
            .find(|r| row_text(r).contains("quoted"))
            .unwrap();
        assert_eq!(quote.prefix[0].content, "│ ");
        assert_eq!(quote.prefix[0].style.fg, theme.color("border.strong"));
        assert!(text.iter().any(|t| t == "• one"));
        assert!(text.iter().any(|t| t.trim_start() == "◦ two"));
        let link = rows.last().unwrap();
        let site = link.spans.iter().find(|s| s.content == "site").unwrap();
        assert_eq!(site.style.fg, theme.color("status.info"));
        assert!(site.style.add_modifier.contains(Modifier::UNDERLINED));
        let all = row_text(link);
        assert!(all.contains("(https://a.test)"), "{all}");
        assert!(
            !all.contains("(src/a.py:42)"),
            "a file link shows only its text: {all}"
        );
    }
}
