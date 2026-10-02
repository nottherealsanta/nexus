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

#[derive(Clone)]
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
            row.push(Cell {
                g: g.to_string(),
                w,
                style: span.style,
                space,
            });
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

pub fn truncate(text: &str, width: usize) -> String {
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

pub(crate) fn color(value: &str, fallback: Color, p: &Palette) -> Color {
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

fn indented(
    out: &mut Rows,
    spans: Vec<Span<'static>>,
    indent: usize,
    width: usize,
    op: &Option<Value>,
) {
    for cells in wrap(&spans, width.saturating_sub(indent + 2)) {
        out.push((
            line(
                vec![Span::raw(" ".repeat(indent))],
                cells,
                None,
                Style::default(),
            ),
            op.clone(),
        ));
    }
}

fn user(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let base = Style::default().fg(p.text).bg(p.panel);
    let bar = |_: ()| {
        vec![
            Span::styled("  ", Style::default().bg(p.background)),
            Span::styled("│", Style::default().fg(p.blue).bg(p.panel)),
            Span::styled(" ", base),
        ]
    };
    let inner = width.saturating_sub(6).max(1);
    let op = &b.operation;
    let tag = if b.number > 0 {
        format!(" #{} ", b.number)
    } else {
        String::new()
    };
    let room = if tag.is_empty() {
        inner
    } else {
        inner.saturating_sub(tag.width() + 1).max(8)
    };
    let head = vec![Span::raw(b.title.clone())];
    for (i, cells) in wrap(&head, room).into_iter().enumerate() {
        let mut row = line(
            if i == 0 {
                vec![
                    Span::styled("  ", Style::default().bg(p.background)),
                    Span::styled(
                        if b.collapsed { "▸ " } else { "▾ " },
                        Style::default().fg(p.blue).bg(p.panel),
                    ),
                ]
            } else {
                bar(())
            },
            cells,
            Some(if i == 0 && !tag.is_empty() {
                4 + room
            } else {
                width
            }),
            base,
        );
        if i == 0 && !tag.is_empty() {
            row.spans.push(Span::styled(" ", base));
            row.spans.push(Span::styled(
                tag.clone(),
                Style::default().fg(p.muted).bg(p.panel),
            ));
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
            spans.push(Span::styled(
                format!(" ▣ {chip} "),
                Style::default()
                    .fg(p.blue)
                    .bg(p.element_hi)
                    .add_modifier(Modifier::BOLD),
            ));
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
            line(
                prefix,
                wrap(
                    &[Span::styled(
                        "Click to inspect attached context",
                        Style::default().fg(p.quiet),
                    )],
                    inner,
                )
                .remove(0),
                Some(width),
                base,
            ),
            chip_op,
        ));
    }
}

/// An inline file diff like textual-diff-view: `path (+a, -r)`, then split rows with
/// real line numbers, removed lines tinted red on the left and added lines green on
/// the right, long lines wrapped inside their column, hunks separated by `⋯`.
fn diff(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let op = &b.operation;
    let counts = vec![
        Span::styled(b.title.clone(), Style::default().fg(p.muted)),
        Span::styled(" (", Style::default().fg(p.muted)),
        Span::styled(
            format!("+{}", b.added),
            Style::default().fg(p.success).add_modifier(Modifier::BOLD),
        ),
        Span::styled(", ", Style::default().fg(p.muted)),
        Span::styled(
            format!("-{}", b.removed),
            Style::default().fg(p.error).add_modifier(Modifier::BOLD),
        ),
        Span::styled(")", Style::default().fg(p.muted)),
    ];
    indented(out, counts, 2, width, op);
    let digits = b
        .diff_rows
        .iter()
        .map(|r| r.0.max(r.2))
        .max()
        .unwrap_or(0)
        .to_string()
        .len()
        .max(2);
    let total = width.saturating_sub(2);
    let half = total.saturating_sub(2 * (digits + 2) + 1) / 2;
    let half = half.max(4);
    let number = |n: u32| {
        if n == 0 {
            " ".repeat(digits)
        } else {
            format!("{n:>digits$}")
        }
    };
    for (old_no, old, new_no, new, kind) in &b.diff_rows {
        match kind.as_str() {
            "sep" => {
                out.push((
                    Line::styled(format!("  {}", "⋯"), Style::default().fg(p.quiet)),
                    op.clone(),
                ));
                continue;
            }
            "clip" => {
                out.push((
                    Line::styled(format!("  {old}"), Style::default().fg(p.quiet)),
                    op.clone(),
                ));
                continue;
            }
            _ => {}
        }
        let removed = matches!(kind.as_str(), "del" | "change");
        let added = matches!(kind.as_str(), "add" | "change");
        let side = |text: &str, tint: Option<Color>, fg: Color| {
            let base = tint.map(|bg| Style::default().bg(bg)).unwrap_or_default();
            let rows = wrap(
                &[Span::styled(text.to_string(), Style::default().fg(fg))],
                half,
            );
            (base, rows)
        };
        let (left_base, left) = side(
            old,
            removed.then_some(p.diff_del),
            if removed { p.text } else { p.muted },
        );
        let (right_base, right) = side(
            new,
            added.then_some(p.diff_add),
            if added { p.text } else { p.muted },
        );
        for i in 0..left.len().max(right.len()) {
            let cells = |rows: &Vec<Vec<Cell>>, base: Style| {
                let row = rows
                    .get(i)
                    .map(|r| {
                        r.iter()
                            .map(|c| Cell {
                                g: c.g.clone(),
                                w: c.w,
                                style: c.style,
                                space: c.space,
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                line(vec![], row, Some(half), base)
            };
            let mut spans = vec![Span::raw("  ")];
            let gutter = |n: u32, shown: bool, tint: Option<Color>| {
                let base = Style::default().fg(p.quiet);
                Span::styled(
                    format!("{} ", if shown { number(n) } else { " ".repeat(digits) }),
                    tint.map(|bg| base.bg(bg)).unwrap_or(base),
                )
            };
            spans.push(gutter(*old_no, i == 0, removed.then_some(p.diff_del)));
            spans.extend(cells(&left, left_base).spans);
            spans.push(Span::styled("│", Style::default().fg(p.border_strong)));
            spans.push(gutter(*new_no, i == 0, added.then_some(p.diff_add)));
            spans.extend(cells(&right, right_base).spans);
            out.push((Line::from(spans), op.clone()));
        }
    }
}

pub fn build(b: &Content, width: u16, p: &Palette) -> Rows {
    let width = usize::from(width).max(8);
    let mut out: Rows = (0..b.gap).map(|_| (Line::default(), None)).collect();
    let op = &b.operation;
    match b.kind.as_str() {
        "user" => user(&mut out, b, width, p),
        "context_header" => {
            let mut spans = Vec::new();
            for chip in &b.members {
                spans.push(Span::styled(
                    "▌",
                    Style::default().fg(color(&chip.color, p.blue, p)),
                ));
                spans.push(Span::styled(
                    format!("{} {}   ", chip.title, chip.status.replace(" tokens", "")),
                    Style::default().fg(p.muted),
                ));
            }
            for cells in wrap(&spans, width.saturating_sub(4)) {
                out.push((
                    line(vec![Span::raw("    ")], cells, None, Style::default()),
                    op.clone(),
                ));
            }
        }
        "context" => {
            let chip = color(&b.color, Color::Rgb(138, 138, 138), p);
            let mut head = vec![Span::styled(
                format!(" {} ", b.title),
                Style::default()
                    .fg(p.background)
                    .bg(chip)
                    .add_modifier(Modifier::BOLD),
            )];
            if !b.status.is_empty() {
                head.push(Span::styled(
                    format!(" {}", b.status),
                    Style::default().fg(p.quiet),
                ));
            }
            indented(&mut out, head, 2, width, op);
            let tone = if b.text.is_empty() { p.quiet } else { p.muted };
            for text in b.text.lines() {
                indented(
                    &mut out,
                    vec![Span::styled(text.to_string(), Style::default().fg(tone))],
                    4,
                    width,
                    op,
                );
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
                    Span::styled(
                        keys.to_string(),
                        Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
                    ),
                    Span::raw("  "),
                    Span::styled(text.to_string(), Style::default().fg(p.quiet)),
                ];
                out.push((Line::from(spans), None));
            }
        }
        "agent" => {
            let c = color(&b.color, p.blue, p);
            indented(
                &mut out,
                vec![
                    Span::styled("◆", Style::default().fg(c)),
                    Span::raw(" "),
                    Span::styled(
                        b.title.clone(),
                        Style::default().fg(c).add_modifier(Modifier::BOLD),
                    ),
                ],
                2,
                width,
                op,
            );
        }
        "thought" => {
            indented(
                &mut out,
                vec![
                    Span::styled("◇", Style::default().fg(p.purple)),
                    Span::raw(" "),
                    Span::styled(
                        b.title.clone(),
                        Style::default().fg(p.muted).add_modifier(Modifier::ITALIC),
                    ),
                    Span::raw("  "),
                    Span::styled(b.text.clone(), Style::default().fg(p.quiet)),
                ],
                2,
                width,
                op,
            );
            if !b.detail.is_empty() {
                out.push((Line::default(), op.clone()));
                for text in b.detail.lines() {
                    indented(
                        &mut out,
                        vec![Span::styled(text.to_string(), Style::default().fg(p.muted))],
                        2,
                        width,
                        op,
                    );
                }
            }
        }
        "markdown" => {
            let total = width.saturating_sub(2);
            for row in markdown::lines(&b.text, p, width) {
                if row.blank {
                    out.push((Line::default(), op.clone()));
                    continue;
                }
                let prefix_width: usize = row.prefix.iter().map(|span| span.content.width()).sum();
                let room = total.saturating_sub(4 + prefix_width).max(1);
                let base = row.bg.map(|bg| Style::default().bg(bg)).unwrap_or_default();
                for (i, cells) in wrap(&row.spans, room).into_iter().enumerate() {
                    let mut lead = vec![Span::raw("    ")];
                    if i == 0 {
                        lead.extend(row.prefix.iter().cloned());
                    } else {
                        // Hanging indent: wrapped rows line up under the first row's text.
                        lead.push(Span::styled(" ".repeat(prefix_width), base));
                    }
                    out.push((line(lead, cells, row.bg.map(|_| total), base), op.clone()));
                }
            }
        }
        "tool_group" => {
            let glyph = if b.failures > 0 {
                "✗"
            } else if b.status == "running" {
                "\u{e000}"
            } else if b.failures > 0 {
                "✗"
            } else {
                "✓"
            };
            let tone = if b.failures > 0 {
                p.error
            } else if b.status == "running" {
                color(&b.color, p.blue, p)
            } else {
                p.success
            };
            let count = if b.count > 1 {
                format!("{:>2}", b.count.min(99))
            } else {
                "  ".into()
            };
            let suffix = if b.failures > 0 {
                format!(" · {} failed", b.failures)
            } else {
                String::new()
            };
            out.push((
                Line::from(vec![
                    Span::styled(glyph, Style::default().fg(tone)),
                    Span::styled(count, Style::default().fg(p.muted)),
                    Span::raw(" "),
                    Span::styled(
                        truncate(&b.text, width.saturating_sub(4 + suffix.width())),
                        Style::default().fg(p.text),
                    ),
                    Span::styled(suffix, Style::default().fg(p.error)),
                ]),
                op.clone(),
            ));
            for member in &b.members {
                let text = format!("{:<8} {}", member.title, member.text);
                out.push((
                    Line::from(vec![
                        Span::styled(
                            format!(
                                "  {} ",
                                if member.batch_glyph.is_empty() {
                                    "▸"
                                } else {
                                    "∥"
                                }
                            ),
                            Style::default().fg(p.muted),
                        ),
                        Span::styled(
                            truncate(&text, width.saturating_sub(4)),
                            Style::default().fg(if member.status == "failed" {
                                p.error
                            } else {
                                p.muted
                            }),
                        ),
                    ]),
                    member.operation.clone(),
                ));
                for row in member.detail.lines() {
                    let operation = member
                        .output_operation
                        .clone()
                        .or_else(|| member.operation.clone());
                    let label_value = if row.starts_with("  ") && !row.starts_with("    ") {
                        row.trim_start().split_once(": ")
                    } else {
                        None
                    };
                    if let Some((label, value)) = label_value {
                        let label_width = 12;
                        let labels = wrap(
                            &[Span::styled(
                                label.to_string(),
                                Style::default().fg(p.muted),
                            )],
                            label_width,
                        );
                        for cells in labels.iter().take(labels.len().saturating_sub(1)) {
                            // Preserve long parameter keys before the value.
                            out.push((
                                line(
                                    vec![Span::styled("  │ ", Style::default().fg(p.border))],
                                    cells.clone(),
                                    None,
                                    Style::default(),
                                ),
                                operation.clone(),
                            ));
                        }
                        let last_label = if label.width() <= label_width {
                            label
                        } else {
                            ""
                        };
                        if label.width() > label_width {
                            out.push((
                                line(
                                    vec![Span::styled("  │ ", Style::default().fg(p.border))],
                                    labels.last().unwrap().clone(),
                                    None,
                                    Style::default(),
                                ),
                                operation.clone(),
                            ));
                        }
                        for (i, cells) in wrap(
                            &[Span::styled(value.to_string(), Style::default().fg(p.text))],
                            width.saturating_sub(4 + label_width),
                        )
                        .into_iter()
                        .enumerate()
                        {
                            let prefix = vec![
                                Span::styled("  │ ", Style::default().fg(p.border)),
                                Span::styled(
                                    if i == 0 {
                                        format!("{last_label:<label_width$}")
                                    } else {
                                        " ".repeat(label_width)
                                    },
                                    Style::default().fg(p.muted),
                                ),
                            ];
                            out.push((
                                line(prefix, cells, None, Style::default()),
                                operation.clone(),
                            ));
                        }
                    } else {
                        let text = row.strip_prefix("  ").unwrap_or(row);
                        for cells in wrap(
                            &[Span::styled(
                                text.to_string(),
                                Style::default().fg(if row.starts_with(' ') {
                                    p.text
                                } else {
                                    p.muted
                                }),
                            )],
                            width.saturating_sub(4),
                        ) {
                            out.push((
                                line(
                                    vec![Span::styled("  │ ", Style::default().fg(p.border))],
                                    cells,
                                    None,
                                    Style::default(),
                                ),
                                operation.clone(),
                            ));
                        }
                    }
                }
                for child in &member.members {
                    out.extend(build(child, width as u16, p));
                }
            }
        }
        "tool" => {
            let tone = if b.status == "failed" {
                p.error
            } else {
                p.quiet
            };
            let gutter = match b.batch_glyph.as_str() {
                "┌" | "│" | "└" => format!(" {}", b.batch_glyph),
                _ => "  ".to_string(),
            };
            for text in b.text.lines() {
                out.push((
                    Line::styled(
                        format!("{}{}", gutter, truncate(text, width.saturating_sub(4))),
                        Style::default().fg(tone),
                    ),
                    op.clone(),
                ));
            }
            for text in b.detail.lines() {
                indented(
                    &mut out,
                    vec![Span::styled(text.to_string(), Style::default().fg(p.muted))],
                    4,
                    width,
                    op,
                );
            }
        }
        "summary" => {
            let text = truncate(&b.text, width.saturating_sub(4));
            let pad = width.saturating_sub(2 + text.width());
            out.push((
                Line::styled(
                    format!("{}{}", " ".repeat(pad), text),
                    Style::default().fg(p.quiet),
                ),
                op.clone(),
            ));
        }
        "collapsed" => indented(
            &mut out,
            vec![
                Span::styled(b.title.clone(), Style::default().fg(p.muted)),
                Span::raw("  "),
                Span::styled(b.text.clone(), Style::default().fg(p.quiet)),
            ],
            4,
            width,
            op,
        ),
        "error" => indented(
            &mut out,
            vec![Span::styled(b.text.clone(), Style::default().fg(p.error))],
            2,
            width,
            op,
        ),
        "diff" => diff(&mut out, b, width, p),
        _ => {
            if !b.title.is_empty() {
                indented(
                    &mut out,
                    vec![Span::styled(
                        b.title.clone(),
                        Style::default().fg(p.accent).add_modifier(Modifier::BOLD),
                    )],
                    2,
                    width,
                    op,
                );
            }
            for text in b.text.lines() {
                indented(
                    &mut out,
                    vec![Span::styled(text.to_string(), Style::default().fg(p.text))],
                    2,
                    width,
                    op,
                );
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(rows: &[Vec<Cell>]) -> Vec<String> {
        rows.iter()
            .map(|row| row.iter().map(|c| c.g.as_str()).collect())
            .collect()
    }

    #[test]
    fn wraps_at_words_without_leading_spaces() {
        let rows = wrap(&[Span::raw("run nexus doctor or nexus run now")], 11);
        let lines = text(&rows);
        assert_eq!(lines, ["run nexus", "doctor or", "nexus run", "now"]);
        assert!(lines.iter().all(|l| !l.starts_with(' ') && l.width() <= 11));
        // A space landing exactly on the margin is dropped, not carried over.
        assert_eq!(
            text(&wrap(&[Span::raw("abcde fghij")], 5)),
            ["abcde", "fghij"]
        );
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
        let header: String = rows[0].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(header.starts_with("  ▾ "));
        let number = rows[0]
            .0
            .spans
            .iter()
            .find(|span| span.content.contains("#3"))
            .unwrap();
        assert_eq!(number.style.fg, Some(p.muted));
        assert_eq!(
            number.style.bg,
            Some(p.panel),
            "number blends into the card without a badge"
        );
        assert!(header.starts_with("  ▾ hello") && header.trim_end().ends_with("#3"));
        let all: String = rows
            .iter()
            .flat_map(|(l, _)| l.spans.iter())
            .map(|s| s.content.as_ref())
            .collect();
        assert!(all.contains("▣ image 1") && all.contains("Click to inspect attached context"));
    }

    #[test]
    fn parallel_tools_keep_the_same_text_column() {
        let p = Palette::new(false);
        for width in [12, 60, 120] {
            for glyph in ["", "┌", "│", "└"] {
                let block = Content {
                    kind: "tool".into(),
                    text: "✓ read README.md\n  0 tool calls · 7.5s".into(),
                    batch_glyph: glyph.into(),
                    operation: Some(serde_json::json!({"kind": "tool_page", "id": "c"})),
                    ..Default::default()
                };
                let rows = build(&block, width, &p);
                let first: String = rows[0].0.spans.iter().map(|s| s.content.as_ref()).collect();
                let second: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
                assert_eq!(first.chars().nth(2), Some('✓'));
                assert_eq!(second.chars().nth(4), Some('0'));
                assert!(rows
                    .iter()
                    .all(|(line, op)| line.width() <= width as usize && op == &block.operation));
            }
        }
    }

    #[test]
    fn gaps_context_chips_and_tool_failures() {
        let p = Palette::new(false);
        let tool = Content {
            kind: "tool".into(),
            status: "failed".into(),
            text: "$ ls\nsecond".into(),
            gap: 2,
            ..Default::default()
        };
        let rows = build(&tool, 30, &p);
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[2].0.style.fg, Some(p.error));
        let context = Content {
            kind: "context".into(),
            title: "MCP".into(),
            text: "Project 0 | Global 1".into(),
            status: "~12 tokens".into(),
            color: "$nx-blue".into(),
            ..Default::default()
        };
        let rows = build(&context, 40, &p);
        assert_eq!(rows[0].0.spans[1].style.bg, Some(p.blue));
        assert_eq!(rows.len(), 2);
    }
}
