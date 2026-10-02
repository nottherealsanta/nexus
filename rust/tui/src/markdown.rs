//! Markdown to styled rows, following the Textual assistant text
//! (`app.tcss` `.timeline-assistant ...`): coloured headings, inline code on a
//! raised background, fences and quotes on the panel colour with a quote bar,
//! hanging-indent lists and aligned tables. HTML is literal; URLs stay visible.
use crate::render::Palette;
use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::{Modifier, Style},
    text::Span,
};
use unicode_width::UnicodeWidthStr;

/// One logical row before wrapping: a first-row prefix (wrapped rows hang under
/// its width) and an optional background that fills the whole row.
#[derive(Default)]
pub struct Row {
    pub prefix: Vec<Span<'static>>,
    pub spans: Vec<Span<'static>>,
    pub bg: Option<ratatui::style::Color>,
    pub blank: bool,
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
    fn fence(&mut self, body: &str, language: &str) {
        let bg = self.p.panel;
        let style = Style::default().fg(self.p.text).bg(bg);
        if !language.is_empty() {
            self.cur.push(Span::styled(
                language.to_string(),
                Style::default().fg(self.p.quiet).bg(bg),
            ));
            self.fence_row();
        }
        for line in body.trim_end_matches('\n').split('\n') {
            self.cur
                .push(Span::styled(line.replace('\t', "    "), style));
            self.fence_row();
        }
    }
    fn fence_row(&mut self) {
        let (mut prefix, _) = self.prefix();
        prefix.push(Span::styled(" ", Style::default().bg(self.p.panel)));
        let spans = std::mem::take(&mut self.cur);
        self.rows.push(Row {
            prefix,
            spans,
            bg: Some(self.p.panel),
            blank: false,
        });
    }
    fn finish_table(&mut self) {
        let Some(table) = self.table.take() else {
            return;
        };
        let columns = table.rows.iter().map(Vec::len).max().unwrap_or(0);
        let widths: Vec<usize> = (0..columns)
            .map(|c| {
                table
                    .rows
                    .iter()
                    .map(|row| row.get(c).map_or(0, |cell| cell.width()))
                    .max()
                    .unwrap_or(0)
            })
            .collect();
        let line = |cells: &[String]| {
            (0..columns)
                .map(|c| {
                    let cell = cells.get(c).map(String::as_str).unwrap_or("");
                    format!(
                        "{cell}{}",
                        " ".repeat(widths[c].saturating_sub(cell.width()))
                    )
                })
                .collect::<Vec<_>>()
                .join(" │ ")
        };
        for (i, cells) in table.rows.iter().enumerate() {
            let head = i < table.head_rows;
            let style = if head {
                Style::default().add_modifier(Modifier::BOLD)
            } else {
                Style::default()
            };
            self.cur.push(Span::styled(line(cells), style));
            self.flush();
            if head && i + 1 == table.head_rows {
                let rule = widths
                    .iter()
                    .map(|w| "─".repeat(*w))
                    .collect::<Vec<_>>()
                    .join("─┼─");
                self.cur
                    .push(Span::styled(rule, Style::default().fg(self.p.quiet)));
                self.flush();
            }
        }
        self.blank();
    }
}

pub fn lines(text: &str, p: &Palette, width: usize) -> Vec<Row> {
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
    };
    let mut fence_body = String::new();
    for event in Parser::new_ext(
        text,
        Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TABLES | Options::ENABLE_TASKLISTS,
    ) {
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
                w.fence(&body, &language);
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
            Event::Start(Tag::Link { dest_url, .. })
            | Event::Start(Tag::Image { dest_url, .. }) => {
                w.links.push(dest_url.to_string());
            }
            Event::End(TagEnd::Link) | Event::End(TagEnd::Image) => {
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
                    let mut style = Style::default().fg(p.warning).bg(p.element_hi);
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
    fn headings_use_textual_level_colours() {
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
    fn fences_carry_language_and_fill_with_panel_colour() {
        let p = Palette::new(false);
        let rows = lines("```rust\nlet x = 1;\n```", &p, 60);
        assert_eq!(plain(&rows[0]), " rust");
        assert_eq!(plain(&rows[1]), " let x = 1;");
        assert!(rows.iter().all(|row| row.bg == Some(p.panel)));
    }
    #[test]
    fn quotes_have_a_bar_and_tables_align() {
        assert_eq!(render("> quoted"), vec!["▌ quoted"]);
        let table = render("| a | bb |\n|---|---|\n| ccc | d |");
        assert_eq!(table, vec!["a   │ bb", "────┼───", "ccc │ d "]);
    }
    #[test]
    fn inline_code_has_its_own_background() {
        let p = Palette::new(false);
        let rows = lines("use `x` here", &p, 60);
        assert_eq!(rows[0].spans[1].style.bg, Some(p.element_hi));
    }
}
