//! Transcript blocks to styled, wrapped rows, matching the Textual timeline
//! (`nexus/ui/tui/timeline.py`, `app.tcss` `.timeline-*`). Python decides order
//! and blank-row gaps; this module only draws and wraps at word boundaries.
use crate::{bridge::Content, markdown, render::Palette};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
use serde_json::Value;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub type Rows = Vec<(Line<'static>, Option<Value>)>;

pub struct Cell {
    g: String,
    w: usize,
    style: Style,
    space: bool,
}

/// Wrap styled text to `width` columns, breaking at spaces when possible and
/// inside a word only when it is longer than a row. Graphemes stay whole.
pub fn wrap(spans: &[Span<'static>], width: usize) -> Vec<Vec<Cell>> {
    let width = width.max(1);
    let mut rows: Vec<Vec<Cell>> = Vec::new();
    let mut row: Vec<Cell> = Vec::new();
    let mut cols = 0usize;
    let mut brk: Option<usize> = None;
    for span in spans {
        for g in span.content.graphemes(true) {
            let w = g.width();
            let space = g.chars().all(char::is_whitespace);
            let mut dropped = false;
            while cols + w > width && !row.is_empty() {
                if space {
                    rows.push(std::mem::take(&mut row));
                    cols = 0;
                    brk = None;
                    dropped = true;
                    break;
                }
                if let Some(at) = brk.filter(|at| *at > 0 && *at < row.len()) {
                    let tail = row.split_off(at);
                    while row.last().is_some_and(|cell| cell.space) {
                        row.pop();
                    }
                    rows.push(std::mem::replace(&mut row, tail));
                    cols = row.iter().map(|cell| cell.w).sum();
                } else {
                    rows.push(std::mem::take(&mut row));
                    cols = 0;
                }
                brk = None;
            }
            if dropped {
                continue;
            }
            row.push(Cell { g: g.to_string(), w, style: span.style, space });
            cols += w;
            if space {
                brk = Some(row.len());
            }
        }
    }
    rows.push(row);
    rows
}

/// Join a prefix and wrapped cells into a line, optionally padded with `fill`
/// (a background) to `total` columns.
pub fn line(
    prefix: Vec<Span<'static>>,
    cells: Vec<Cell>,
    total: Option<usize>,
    base: Style,
) -> Line<'static> {
    let mut spans = prefix;
    let mut used: usize = spans.iter().map(|s| s.content.width()).sum();
    let mut text = String::new();
    let mut style: Option<Style> = None;
    for cell in cells {
        let cell_style = base.patch(cell.style);
        if style != Some(cell_style) {
            if let Some(previous) = style {
                spans.push(Span::styled(std::mem::take(&mut text), previous));
            }
            style = Some(cell_style);
        }
        text.push_str(&cell.g);
        used += cell.w;
    }
    if let Some(previous) = style {
        spans.push(Span::styled(text, previous));
    }
    if let Some(total) = total {
        if used < total {
            spans.push(Span::styled(" ".repeat(total - used), base));
        }
    }
    Line::from(spans)
}

fn truncate(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_string();
    }
    let mut out = String::new();
    let mut used = 0;
    for g in text.graphemes(true) {
        if used + g.width() + 1 > width {
            break;
        }
        out.push_str(g);
        used += g.width();
    }
    out + "…"
}

fn color(value: &str, fallback: Color, p: &Palette) -> Color {
    match value {
        "$nx-blue" => return p.blue,
        "$nx-accent" => return p.accent,
        "$nx-purple" => return p.purple,
        "$nx-success" => return p.success,
        "$nx-warning" => return p.warning,
        "$nx-cyan" => return p.cyan,
        "$nx-label-neutral" => return Color::Rgb(138, 138, 138),
        _ => {}
    }
    let hex = value.strip_prefix('#').unwrap_or("");
    if hex.len() == 6 {
        if let Ok(n) = u32::from_str_radix(hex, 16) {
            return Color::Rgb((n >> 16) as u8, (n >> 8) as u8, n as u8);
        }
    }
    fallback
}

fn indented(out: &mut Rows, spans: Vec<Span<'static>>, indent: usize, width: usize, op: &Option<Value>) {
    for cells in wrap(&spans, width.saturating_sub(indent + 2)) {
        out.push((line(vec![Span::raw(" ".repeat(indent))], cells, None, Style::default()), op.clone()));
    }
}

fn user(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let base = Style::default().fg(p.text).bg(p.panel);
    let bar = |_: ()| vec![Span::styled("▌", Style::default().fg(p.blue).bg(p.panel)), Span::styled("  ", base)];
    let inner = width.saturating_sub(5).max(1);
    let op = &b.operation;
    let blank = || (line(bar(()), vec![], Some(width), base), op.clone());
    out.push(blank());
    let tag = if b.number > 0 { format!(" #{} ", b.number) } else { String::new() };
    let room = if tag.is_empty() { inner } else { inner.saturating_sub(tag.width() + 1).max(8) };
    let head = vec![
        Span::styled(if b.collapsed { "▶" } else { "▼" }, Style::default().fg(p.border_strong)),
        Span::raw("  "),
        Span::raw(b.title.clone()),
    ];
    for (i, cells) in wrap(&head, room).into_iter().enumerate() {
        let mut row = line(bar(()), cells, Some(if i == 0 && !tag.is_empty() { 3 + room } else { width }), base);
        if i == 0 && !tag.is_empty() {
            row.spans.push(Span::styled(" ", base));
            row.spans.push(Span::styled(tag.clone(), Style::default().fg(p.accent).bg(p.element_hi).add_modifier(Modifier::BOLD)));
            let used: usize = row.spans.iter().map(|s| s.content.width()).sum();
            if used < width {
                row.spans.push(Span::styled(" ".repeat(width - used), base));
            }
        }
        out.push((row, op.clone()));
    }
    if !b.text.is_empty() {
        for text in b.text.lines() {
            for cells in wrap(&[Span::raw(text.to_string())], inner) {
                out.push((line(bar(()), cells, Some(width), base), op.clone()));
            }
        }
    }
    if !b.chips.is_empty() {
        let chip_op = b.chip_operation.clone().or_else(|| op.clone());
        out.push((line(bar(()), vec![], Some(width), base), chip_op.clone()));
        let mut spans = Vec::new();
        for chip in &b.chips {
            spans.push(Span::styled(format!(" ▣ {chip} "), Style::default().fg(p.blue).bg(p.element_hi).add_modifier(Modifier::BOLD)));
            spans.push(Span::raw("  "));
        }
        for cells in wrap(&spans, inner.saturating_sub(3)) {
            let mut prefix = bar(());
            prefix.push(Span::styled("   ", base));
            out.push((line(prefix, cells, Some(width), base), chip_op.clone()));
        }
        let mut prefix = bar(());
        prefix.push(Span::styled("   ", base));
        out.push((
            line(prefix, wrap(&[Span::styled("Click to inspect attached context", Style::default().fg(p.quiet))], inner).remove(0), Some(width), base),
            chip_op,
        ));
    }
    out.push(blank());
}

fn diff(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let op = &b.operation;
    let left: Vec<_> = b.before.lines().collect();
    let right: Vec<_> = b.after.lines().collect();
    indented(out, vec![Span::styled(b.title.clone(), Style::default().fg(p.muted))], 2, width, op);
    let half = (width.saturating_sub(7) / 2).max(1);
    for i in 0..left.len().max(right.len()) {
        let a = wrap(&[Span::raw(left.get(i).copied().unwrap_or("").to_string())], half);
        let c = wrap(&[Span::raw(right.get(i).copied().unwrap_or("").to_string())], half);
        for j in 0..a.len().max(c.len()) {
            let text = |rows: &Vec<Vec<Cell>>| rows.get(j).map(|r| r.iter().map(|c| c.g.as_str()).collect::<String>()).unwrap_or_default();
            let (x, y) = (text(&a), text(&c));
            let pad = " ".repeat(half.saturating_sub(x.width()));
            out.push((
                Line::from(vec![
                    Span::raw("  "),
                    Span::styled(format!("{x}{pad}"), Style::default().fg(p.error)),
                    Span::styled(" │ ", Style::default().fg(p.border_strong)),
                    Span::styled(y, Style::default().fg(p.success)),
                ]),
                op.clone(),
            ));
        }
    }
}

pub fn build(b: &Content, width: u16, p: &Palette) -> Rows {
    let width = usize::from(width).max(8);
    let mut out: Rows = (0..b.gap).map(|_| (Line::default(), None)).collect();
    let op = &b.operation;
    match b.kind.as_str() {
        "user" => user(&mut out, b, width, p),
        "context" => {
            let chip = color(&b.color, Color::Rgb(138, 138, 138), p);
            let mut head = vec![Span::styled(
                format!(" {} ", b.title),
                Style::default().fg(p.background).bg(chip).add_modifier(Modifier::BOLD),
            )];
            if !b.status.is_empty() {
                head.push(Span::styled(format!(" {}", b.status), Style::default().fg(p.quiet)));
            }
            indented(&mut out, head, 2, width, op);
            let tone = if b.text.is_empty() { p.quiet } else { p.muted };
            for text in b.text.lines() {
                indented(&mut out, vec![Span::styled(text.to_string(), Style::default().fg(tone))], 4, width, op);
            }
        }
        "hints" => {
            // Tips for an empty session: "keys\ttext" rows, centred as one block
            // (Python pads both columns to equal width, like Textual's EmptyHints).
            for row in b.text.lines() {
                let (keys, text) = row.split_once('\t').unwrap_or(("", row));
                let used = keys.width() + 2 + text.width();
                let spans = vec![
                    Span::raw(" ".repeat(width.saturating_sub(used) / 2)),
                    Span::styled(keys.to_string(), Style::default().fg(p.muted).add_modifier(Modifier::BOLD)),
                    Span::raw("  "),
                    Span::styled(text.to_string(), Style::default().fg(p.quiet)),
                ];
                out.push((Line::from(spans), None));
            }
        }
        "agent" => {
            let c = color(&b.color, p.blue, p);
            indented(&mut out, vec![
                Span::styled("◆", Style::default().fg(c)),
                Span::raw(" "),
                Span::styled(b.title.clone(), Style::default().fg(c).add_modifier(Modifier::BOLD)),
            ], 2, width, op);
        }
        "thought" => {
            indented(&mut out, vec![
                Span::styled("◇", Style::default().fg(p.purple)),
                Span::raw(" "),
                Span::styled(b.title.clone(), Style::default().fg(p.muted).add_modifier(Modifier::ITALIC)),
                Span::raw("  "),
                Span::styled(b.text.clone(), Style::default().fg(p.quiet)),
            ], 2, width, op);
            if !b.detail.is_empty() {
                out.push((Line::default(), op.clone()));
                for text in b.detail.lines() {
                    indented(&mut out, vec![Span::styled(text.to_string(), Style::default().fg(p.muted))], 2, width, op);
                }
            }
        }
        "markdown" => {
            let mut rows = markdown::lines(&b.text);
            while rows.last().is_some_and(|row| row.spans.iter().all(|s| s.content.trim().is_empty())) {
                rows.pop();
            }
            for row in rows {
                if row.spans.iter().all(|s| s.content.is_empty()) {
                    out.push((Line::default(), op.clone()));
                } else {
                    indented(&mut out, row.spans, 4, width, op);
                }
            }
        }
        "tool" => {
            let tone = if b.status == "failed" { p.error } else { p.quiet };
            for text in b.text.lines() {
                out.push((
                    Line::styled(format!("  {}", truncate(text, width.saturating_sub(4))), Style::default().fg(tone)),
                    op.clone(),
                ));
            }
            for text in b.detail.lines() {
                indented(&mut out, vec![Span::styled(text.to_string(), Style::default().fg(p.muted))], 4, width, op);
            }
        }
        "summary" => {
            let text = truncate(&b.text, width.saturating_sub(4));
            let pad = width.saturating_sub(2 + text.width());
            out.push((Line::styled(format!("{}{}", " ".repeat(pad), text), Style::default().fg(p.quiet)), op.clone()));
        }
        "collapsed" => indented(&mut out, vec![
            Span::styled(b.title.clone(), Style::default().fg(p.muted)),
            Span::raw("  "),
            Span::styled(b.text.clone(), Style::default().fg(p.quiet)),
        ], 4, width, op),
        "error" => indented(&mut out, vec![Span::styled(b.text.clone(), Style::default().fg(p.error))], 2, width, op),
        "diff" => diff(&mut out, b, width, p),
        _ => {
            if !b.title.is_empty() {
                indented(&mut out, vec![Span::styled(b.title.clone(), Style::default().fg(p.accent).add_modifier(Modifier::BOLD))], 2, width, op);
            }
            for text in b.text.lines() {
                indented(&mut out, vec![Span::styled(text.to_string(), Style::default().fg(p.text))], 2, width, op);
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(rows: &[Vec<Cell>]) -> Vec<String> {
        rows.iter().map(|row| row.iter().map(|c| c.g.as_str()).collect()).collect()
    }

    #[test]
    fn wraps_at_words_without_leading_spaces() {
        let rows = wrap(&[Span::raw("run nexus doctor or nexus run now")], 11);
        let lines = text(&rows);
        assert_eq!(lines, ["run nexus", "doctor or", "nexus run", "now"]);
        assert!(lines.iter().all(|l| !l.starts_with(' ') && l.width() <= 11));
        // A space landing exactly on the margin is dropped, not carried over.
        assert_eq!(text(&wrap(&[Span::raw("abcde fghij")], 5)), ["abcde", "fghij"]);
    }

    #[test]
    fn long_words_and_wide_graphemes_break_whole() {
        let lines = text(&wrap(&[Span::raw("aaaaaaaaaaaa")], 5));
        assert_eq!(lines, ["aaaaa", "aaaaa", "aa"]);
        let wide = text(&wrap(&[Span::raw("日本語日本語")], 5));
        assert!(wide.iter().all(|l| l.width() <= 5));
    }

    #[test]
    fn user_card_fills_width_and_right_aligns_number() {
        let p = Palette::new(false);
        let block = Content {
            kind: "user".into(),
            title: "hello".into(),
            number: 3,
            chips: vec!["image 1".into()],
            ..Default::default()
        };
        let rows = build(&block, 40, &p);
        assert!(rows.iter().all(|(line, _)| line.width() == 40));
        let header: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(header.contains("▼  hello") && header.trim_end().ends_with("#3"));
        let all: String = rows.iter().flat_map(|(l, _)| l.spans.iter()).map(|s| s.content.as_ref()).collect();
        assert!(all.contains("▣ image 1") && all.contains("Click to inspect attached context"));
    }

    #[test]
    fn gaps_context_chips_and_tool_failures() {
        let p = Palette::new(false);
        let tool = Content { kind: "tool".into(), status: "failed".into(), text: "$ ls\nsecond".into(), gap: 2, ..Default::default() };
        let rows = build(&tool, 30, &p);
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[2].0.style.fg, Some(p.error));
        let context = Content { kind: "context".into(), title: "MCP".into(), text: "Project 0 | Global 1".into(), status: "~12 tokens".into(), color: "$nx-blue".into(), ..Default::default() };
        let rows = build(&context, 40, &p);
        assert_eq!(rows[0].0.spans[1].style.bg, Some(p.blue));
        assert_eq!(rows.len(), 2);
    }
}
