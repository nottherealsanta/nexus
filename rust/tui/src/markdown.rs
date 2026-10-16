//! Markdown to styled rows, following the canonical assistant text
//! (`app.tcss` `.timeline-assistant ...`): coloured headings, colour-only inline
//! code, fences and quotes on the panel colour with a quote bar,
//! hanging-indent lists and aligned tables. HTML is literal; URLs stay visible.
use crate::mermaid;
use crate::render::Palette;
use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
use ratatui_markdown::{
    markdown::{MarkdownBlock, MarkdownRenderer},
    theme::{Generation, RichTextTheme},
};
use std::{
    cell::RefCell,
    collections::{hash_map::DefaultHasher, HashMap},
    hash::{Hash, Hasher},
    rc::Rc,
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

// ratatui-markdown 0.3.6 (MIT OR Apache-2.0) shares our ratatui 0.29 types.
// Reuse its fenced-code presentation, not its lossy line-oriented parser or
// character-count wrapping. CommonMark structure and cell wrapping stay local.
struct MarkdownTheme<'a>(&'a Palette);
macro_rules! theme_colors {
    ($($method:ident => $field:ident),* $(,)?) => {
        $(fn $method(&self) -> Color { self.0.$field })*
    };
}
impl RichTextTheme for MarkdownTheme<'_> {
    fn generation(&self) -> Generation {
        Generation(0)
    }
    theme_colors! {
        get_text_color => text, get_muted_text_color => quiet,
        get_primary_color => accent, get_popup_selected_background => element_hi,
        get_popup_selected_text_color => text, get_border_color => border,
        get_focused_border_color => border_strong, get_secondary_color => purple,
        get_info_color => blue, get_background_color => background,
        get_json_key_color => blue, get_json_string_color => success,
        get_json_number_color => warning, get_json_bool_color => purple,
        get_json_null_color => quiet, get_accent_yellow => warning,
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
    fn bg(&self) -> Option<ratatui::style::Color> {
        (self.quote > 0).then_some(self.p.panel)
    }
    fn prefix(&mut self) -> (Vec<Span<'static>>, usize) {
        let mut spans = Vec::new();
        let mut hang = 0;
        let bg = self.bg();
        if self.quote > 0 {
            let mut style = Style::default().fg(self.p.accent);
            if let Some(bg) = bg {
                style = style.bg(bg);
            }
            for _ in 0..self.quote {
                spans.push(Span::styled("▌ ", style));
                hang += 2;
            }
        }
        let base = self.indent();
        let text_style = bg.map(|bg| Style::default().bg(bg)).unwrap_or_default();
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
            bg: self.bg(),
            blank: false,
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
        if let Some(bg) = self.bg() {
            style = style.bg(bg);
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
                HeadingLevel::H1 => self.p.accent,
                HeadingLevel::H2 => self.p.purple,
                HeadingLevel::H3 => self.p.success,
                _ => self.p.warning,
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
        // Closed mermaid fences draw as Unicode art on the transcript
        // background when the diagram fits; everything else (still streaming,
        // unsupported type, parse failure, too wide) is an ordinary code block.
        if closed && language == "mermaid" && self.mermaid_art(body, index) {
            return;
        }
        // ratatui-markdown without its `mermaid` feature renders a mermaid
        // fence as zero lines, swallowing the source. Render it as `text`
        // and relabel the header so the original language stays visible.
        let relabel = language == "mermaid";
        let render_language = if relabel { "text" } else { language };
        let block = MarkdownBlock::code_block(render_language, body.replace('\t', "    "));
        for (n, mut line) in MarkdownRenderer::new(self.width)
            .render(&[block], &MarkdownTheme(self.p))
            .into_iter()
            .enumerate()
        {
            if relabel && n == 0 {
                if let Some(span) = line.spans.iter_mut().find(|s| s.content.contains("text")) {
                    let mut label = span.content.replacen("text", "mermaid", 1);
                    // Keep the row width: absorb the 3 extra cells from padding.
                    for _ in 0..3 {
                        if let Some(at) = label.rfind("  ") {
                            label.remove(at);
                        }
                    }
                    span.content = label.into();
                }
            }
            self.cur.extend(line.spans.into_iter().map(|mut span| {
                span.style = span.style.bg(self.p.panel);
                span
            }));
            self.fence_row(index);
        }
    }
    /// Push the diagram rows plus a quiet `◇ mermaid` label. `false` means the
    /// source cannot be drawn within the row width and nothing was pushed.
    fn mermaid_art(&mut self, body: &str, index: usize) -> bool {
        let avail = self.width.saturating_sub(self.quote * 2 + self.indent());
        let Some(art) = mermaid_cached(body, self.p, avail) else {
            return false;
        };
        let bg = self.bg();
        for line in art.iter() {
            let (prefix, _) = self.prefix();
            let spans = line
                .spans
                .iter()
                .cloned()
                .map(|mut span| {
                    if let Some(bg) = bg {
                        span.style = span.style.bg(bg);
                    }
                    span
                })
                .collect();
            self.rows.push(Row {
                prefix,
                spans,
                bg,
                blank: false,
                fence: Some(index),
            });
        }
        let mut label = Style::default().fg(self.p.quiet);
        if let Some(bg) = bg {
            label = label.bg(bg);
        }
        self.cur.push(Span::styled("◇ mermaid", label));
        let (prefix, _) = self.prefix();
        let spans = std::mem::take(&mut self.cur);
        self.rows.push(Row {
            prefix,
            spans,
            bg,
            blank: false,
            fence: Some(index),
        });
        true
    }
    fn fence_row(&mut self, index: usize) {
        let (mut prefix, _) = self.prefix();
        prefix.push(Span::styled(" ", Style::default().bg(self.p.panel)));
        let spans = std::mem::take(&mut self.cur);
        self.rows.push(Row {
            prefix,
            spans,
            bg: Some(self.p.panel),
            blank: false,
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
        title: Style::default().fg(p.muted),
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
            Event::End(TagEnd::Heading(_)) => {
                w.flush();
                w.heading = None;
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
            }
            Event::End(TagEnd::Image) => {
                if let Some(url) = w.links.pop() {
                    w.text(&format!(" ({url})]"), w.style().fg(p.quiet));
                }
            }
            Event::End(TagEnd::Link) => {
                if let Some(url) = w.links.pop() {
                    let style = w.style().fg(p.quiet);
                    w.text(&format!(" ({url})"), style);
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
                    let mut style = Style::default().fg(p.warning);
                    if w.bold > 0 {
                        style = style.add_modifier(Modifier::BOLD);
                    }
                    w.cur.push(Span::styled(value.to_string(), style));
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
                let mark = if done { "[x] " } else { "[ ] " };
                w.cur.push(Span::styled(mark, Style::default().fg(p.quiet)));
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
    fn plain(row: &Row) -> String {
        row.prefix
            .iter()
            .chain(row.spans.iter())
            .map(|s| s.content.as_ref())
            .collect()
    }
    fn render(text: &str) -> Vec<String> {
        lines(text, &Palette::new(false), 60)
            .iter()
            .map(|row| if row.blank { String::new() } else { plain(row) })
            .collect()
    }
    #[test]
    fn wide_tables_wrap_inside_their_columns() {
        let text = "| Name | Description |\n|---|---|\n| pattern | Shell glob relative to the root, e.g. '**/*.py' and more words to force wrapping |";
        let rows = render(text);
        assert!(rows.iter().all(|r| r.width() <= 60), "{rows:#?}");
        assert!(rows.len() > 5, "{rows:#?}");
        assert!(rows.iter().all(|r| r.starts_with(['┌', '│', '├', '└'])));
        assert!(rows.join(" ").contains("wrapping"));
    }
    #[test]
    fn tables_never_exceed_any_width() {
        let text = "| Name | Type | Required | Default | Description |\n|---|---|---|---|---|\n| path | string | yes | — | File or directory to read, relative to the workspace. |\n| csv_as_markdown | boolean | no | — | Convert a CSV file to Markdown instead of reading its raw UTF-8 text. |";
        for width in 30..140 {
            for row in lines(text, &Palette::new(false), width) {
                let w: usize = row.spans.iter().map(|s| s.content.width()).sum();
                assert!(w <= width, "width {width}: row {w}");
            }
        }
    }
    #[test]
    fn literal_html_and_link_target() {
        let rows = lines(
            "**bold** [link](https://example.com)\n\n<environment>literal</environment>",
            &Palette::new(false),
            60,
        );
        let text = rows.iter().map(plain).collect::<Vec<_>>().join("\n");
        assert!(text.contains("https://example.com"));
        assert!(text.contains("<environment>literal</environment>"));
        assert!(rows[0].spans[0].style.add_modifier.contains(Modifier::BOLD));
    }
    #[test]
    fn headings_use_level_colours() {
        let p = Palette::new(false);
        let rows = lines("# one\n\n## two\n\n### three", &p, 60);
        let colours: Vec<_> = rows
            .iter()
            .filter(|r| !r.blank)
            .map(|r| r.spans[0].style.fg)
            .collect();
        assert_eq!(
            colours,
            vec![Some(p.accent), Some(p.purple), Some(p.success)]
        );
    }
    #[test]
    fn lists_nest_with_markers_and_hanging_indent() {
        assert_eq!(
            render("- a\n  - b\n- c\n\n1. x\n2. y"),
            vec!["• a", "   ◦ b", "• c", "", "1. x", "2. y"]
        );
    }
    #[test]
    fn inline_styles_tasks_rules_and_reference_links() {
        let p = Palette::new(false);
        let rows = lines("**bold** *italic* ~~gone~~ [label][ref]\n\n- [x] done\n- [ ] pending\n\n---\n\n[ref]: https://example.com", &p, 60);
        for (text, modifier) in [
            ("bold", Modifier::BOLD),
            ("italic", Modifier::ITALIC),
            ("gone", Modifier::CROSSED_OUT),
        ] {
            assert!(rows
                .iter()
                .flat_map(|row| &row.spans)
                .any(|span| span.content == text && span.style.add_modifier.contains(modifier)));
        }
        let text = rows.iter().map(plain).collect::<Vec<_>>().join("\n");
        for expected in [
            "label (https://example.com)",
            "[x] done",
            "[ ] pending",
            "────",
        ] {
            assert!(text.contains(expected), "missing {expected}: {text}");
        }
    }
    #[test]
    fn fences_carry_language_and_fill_with_panel_colour() {
        let p = Palette::new(false);
        let rows = lines("```rust\nlet x = 1;\n```", &p, 60);
        assert_eq!(plain(&rows[0]), " ╭─ rust");
        assert_eq!(plain(&rows[1]), " │ let x = 1;");
        assert!(rows.iter().all(|row| row.bg == Some(p.panel)));
    }
    #[test]
    fn terminal_only_fallbacks_keep_content() {
        let text = render("![diagram](https://example.com/image.png)\n\n![](empty.png)\n\n<div>raw HTML</div>\n\n```mermaid\ngraph TD\n  A[Start] --> B[End]\n```\n\n```mermaid\npie\n\"a\" : 1\n```\n\n```unknown\nopaque source\n```").join("\n");
        for expected in [
            "[image: diagram (https://example.com/image.png)]",
            "[image:  (empty.png)]",
            "<div>raw HTML</div>",
            // Valid diagram: drawn as art plus the quiet label.
            "┌",
            "Start",
            "◇ mermaid",
            // Unsupported diagram type: ordinary code block keeping its source.
            "pie",
            "\"a\" : 1",
            "unknown",
            "opaque source",
        ] {
            assert!(text.contains(expected), "missing {expected}: {text}");
        }
    }
    #[test]
    fn streaming_and_controls_are_visible_without_terminal_escapes() {
        let text = render("```rust\nlet unfinished = \"界\";\n\u{1b}[31m\u{7}").join("\n");
        assert!(text.contains("let unfinished = \"界\";"));
        assert!(text.contains("\\u{001b}[31m\\u{0007}"));
        assert!(!text.contains('\u{1b}'));
        assert!(!text.contains('\u{7}'));
    }
    #[test]
    fn upstream_code_is_not_truncated_or_reparsed_as_inline_markdown() {
        let body = "界界界界界界界界界界 **literal** [x](url)";
        let rows = lines(&format!("```text\n{body}\n```"), &Palette::new(false), 8);
        assert!(rows
            .iter()
            .map(plain)
            .collect::<Vec<_>>()
            .join("\n")
            .contains(body));
    }
    #[test]
    fn quotes_have_a_bar_and_tables_align() {
        assert_eq!(render("> quoted"), vec!["▌ quoted"]);
        let table = render("| a | bb |\n|---|---|\n| ccc | d |");
        assert_eq!(
            table,
            vec![
                "┌─────┬────┐",
                "│ a   │ bb │",
                "├─────┼────┤",
                "│ ccc │ d  │",
                "└─────┴────┘"
            ]
        );
    }
    #[test]
    fn inline_code_changes_colour_without_a_background() {
        for light in [false, true] {
            let p = Palette::new(light);
            let rows = lines("use `x` here", &p, 60);
            let style = rows[0].spans[1].style;
            assert_eq!(style.fg, Some(p.warning));
            assert_eq!(style.bg, None);
            assert_eq!(rows[0].bg, None);

            let rows = lines("> use **`x`** here", &p, 60);
            let style = rows[0].spans[1].style;
            assert_eq!(style.fg, Some(p.warning));
            assert_eq!(style.bg, None);
            assert!(style.add_modifier.contains(Modifier::BOLD));
            assert_eq!(rows[0].bg, Some(p.panel));
        }
    }

    fn mermaid_rows(source: &str, width: usize) -> Vec<Row> {
        lines(
            &format!("```mermaid\n{source}\n```"),
            &Palette::new(false),
            width,
        )
    }
    fn drawn(rows: &[Row]) -> bool {
        rows.iter().any(|r| plain(r).contains('┌'))
            && rows.iter().all(|r| r.bg != Some(Palette::new(false).panel))
    }
    fn as_code_block(rows: &[Row]) -> bool {
        rows.iter().all(|r| r.bg == Some(Palette::new(false).panel))
    }
    const WIDE: &str = "graph TD\n  R[root] --> A[service_alpha]\n  R --> B[service_bravo]\n  R --> C[service_charlie]\n  R --> D[service_delta]\n  R --> E[service_echo]\n  R --> F[service_foxtrot]";

    #[test]
    fn mermaid_flowcharts_and_sequences_draw_art() {
        for source in [
            "graph TD\n  A[Start] --> B[End]",
            "graph LR\n  A[Start] --> B[End]",
            "sequenceDiagram\n  Alice->>Bob: hello",
        ] {
            let rows = mermaid_rows(source, 80);
            assert!(
                drawn(&rows),
                "{source}: {:#?}",
                rows.iter().map(plain).collect::<Vec<_>>()
            );
            assert!(rows.iter().all(|r| r.fence == Some(0)));
            assert!(plain(rows.last().unwrap()).contains("◇ mermaid"));
            assert!(rows.iter().all(|r| plain(r).width() <= 80));
        }
    }
    #[test]
    fn mermaid_cjk_labels_fit() {
        let rows = mermaid_rows("graph TD\n  A[开始] --> B[结束]", 40);
        assert!(drawn(&rows));
        assert!(rows.iter().all(|r| plain(r).width() <= 40));
        assert!(rows.iter().map(plain).collect::<String>().contains("开始"));
    }
    #[test]
    fn mermaid_unsupported_unclosed_and_extra_tokens() {
        assert!(as_code_block(&mermaid_rows("pie\n\"a\" : 1", 80)));
        let open = lines("```mermaid\ngraph TD\n  A --> B", &Palette::new(false), 80);
        assert!(as_code_block(&open));
        let themed = lines(
            "```mermaid theme=dark\ngraph TD\n  A --> B\n```",
            &Palette::new(false),
            80,
        );
        assert!(drawn(&themed));
        let tilde = lines(
            "~~~mermaid\ngraph TD\n  A --> B\n~~~",
            &Palette::new(false),
            80,
        );
        assert!(drawn(&tilde));
    }
    #[test]
    fn mermaid_invalid_fallback_keeps_original_body() {
        let rows = mermaid_rows("pie\n\"a\" : 1", 80);
        let text = rows.iter().map(plain).collect::<Vec<_>>().join("\n");
        assert!(text.contains("pie") && text.contains("\"a\" : 1"), "{text}");
        assert!(
            text.contains("mermaid") && !text.contains("◇ mermaid"),
            "{text}"
        );
        assert!(as_code_block(&rows));
        let open = lines("```mermaid\ngraph TD\n  A --> B", &Palette::new(false), 80);
        let open = open.iter().map(plain).collect::<Vec<_>>().join("\n");
        assert!(
            open.contains("graph TD") && open.contains("A --> B"),
            "{open}"
        );
        // A later fence after drawn art still falls back with its own source.
        let both = lines(
            "```mermaid\ngraph TD\n  A --> B\n```\n\n```mermaid\npie\n\"a\" : 1\n```",
            &Palette::new(false),
            80,
        );
        let text = both.iter().map(plain).collect::<Vec<_>>().join("\n");
        assert!(text.contains("◇ mermaid") && text.contains("pie"), "{text}");
        assert_eq!(
            crate::markdown::fence_sources("```mermaid\npie\n\"a\" : 1\n```"),
            ["pie\n\"a\" : 1"]
        );
    }
    #[test]
    fn mermaid_streaming_matches_one_shot() {
        let full = "text\n\n```mermaid\ngraph TD\n  A --> B\n```\n";
        let one_shot: Vec<String> = lines(full, &Palette::new(false), 60)
            .iter()
            .map(plain)
            .collect();
        let mut last = Vec::new();
        for end in (0..=full.len()).filter(|i| full.is_char_boundary(*i)) {
            last = lines(&full[..end], &Palette::new(false), 60)
                .iter()
                .map(plain)
                .collect();
        }
        assert_eq!(last, one_shot);
    }
    #[test]
    fn mermaid_wide_lr_retries_top_down() {
        let source = "graph LR\n  A[service_alpha] --> B[service_bravo] --> C[service_charlie] --> D[service_delta] --> E[service_echo]";
        let rows = mermaid_rows(source, 60);
        assert!(
            drawn(&rows),
            "{:#?}",
            rows.iter().map(plain).collect::<Vec<_>>()
        );
        assert!(rows.iter().any(|r| plain(r).contains('▼')));
    }
    #[test]
    fn mermaid_too_wide_either_way_and_resize_switch() {
        assert!(as_code_block(&mermaid_rows(WIDE, 40)));
        assert!(drawn(&mermaid_rows(WIDE, 160)));
        assert!(as_code_block(&mermaid_rows(WIDE, 40)));
    }
    #[test]
    fn mermaid_copy_source_is_not_the_art() {
        let text = format!("```mermaid\n{WIDE}\n```");
        assert_eq!(fence_sources(&text), vec![WIDE.to_string()]);
        assert!(drawn(&lines(&text, &Palette::new(false), 160)));
    }
    #[test]
    fn mermaid_fuzzed_bodies_never_panic() {
        let tokens = [
            "graph TD",
            "graph LR",
            "flowchart RL",
            "A",
            "B[x]",
            "-->",
            "-.->",
            "==>",
            "subgraph s",
            "end",
            "stateDiagram-v2",
            "[*]",
            ": ev",
            "classDiagram",
            "class A",
            "A <|-- B",
            "erDiagram",
            "A ||--o{ B : has",
            "sequenceDiagram",
            "Alice->>Bob: hi",
            "Note over A: n",
            "界",
            "|lbl|",
            "((",
            "))",
            "{",
            "}",
            ";",
            "\n",
            "\n",
            " ",
        ];
        let mut seed = 0x2545_f491_4f6c_dd1du64;
        let start = std::time::Instant::now();
        for _ in 0..300 {
            let mut body = String::new();
            for _ in 0..(seed % 40) {
                seed = seed
                    .wrapping_mul(6364136223846793005)
                    .wrapping_add(1442695040888963407);
                body.push_str(tokens[(seed >> 33) as usize % tokens.len()]);
                body.push(' ');
            }
            seed = seed
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let width = 20 + (seed >> 40) as usize % 120;
            let _ = mermaid_rows(&body.replace("```", ""), width);
        }
        assert!(start.elapsed() < std::time::Duration::from_secs(20));
    }
}
