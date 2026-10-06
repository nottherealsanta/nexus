//! Transcript blocks to styled, wrapped rows, for the native timeline. Python decides order
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
    if width == 0 {
        return String::new();
    }
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
    let rail = color(&b.color, p.blue, p);
    let bar = |_: ()| {
        vec![
            Span::styled("  ", Style::default().bg(p.background)),
            Span::styled("│", Style::default().fg(rail).bg(p.panel)),
            Span::styled("  ", base),
        ]
    };
    let inner = width.saturating_sub(7).max(1);
    let op = &b.operation;
    out.push((line(bar(()), vec![], Some(width), base), op.clone()));
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
                // The fold chevron sits in the left margin, outside the card, barely visible;
                // the rail stays unbroken.
                let mut prefix = bar(());
                prefix[0] = Span::styled(
                    if b.collapsed { "▸ " } else { "▾ " },
                    Style::default().fg(p.border).bg(p.background),
                );
                prefix
            } else {
                bar(())
            },
            cells,
            Some(if i == 0 && !tag.is_empty() {
                5 + room
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
    // A folded turn's text is its stats line; it is drawn under the card, not in it.
    if !b.text.is_empty() && !b.collapsed {
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
        for cells in wrap(&spans, inner) {
            out.push((line(bar(()), cells, Some(width), base), chip_op.clone()));
        }
        out.push((
            line(
                bar(()),
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
    out.push((line(bar(()), vec![], Some(width), base), op.clone()));
    if b.collapsed && !b.text.is_empty() {
        // Outside the box, muted, aligned with the prompt text (5 columns in). The next
        // block's own gap supplies the blank line below.
        let outside = Style::default().fg(p.muted).bg(p.background);
        let indent = || vec![Span::styled("     ", Style::default().bg(p.background))];
        for text in b.text.lines() {
            for cells in wrap(&[Span::styled(text.to_string(), outside)], inner) {
                out.push((
                    line(
                        indent(),
                        cells,
                        Some(width),
                        Style::default().bg(p.background),
                    ),
                    op.clone(),
                ));
            }
        }
    }
}

/// An inline file diff with split columns: `path (+a, -r)`, then split rows with
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
    let padded = !matches!(
        b.kind.as_str(),
        "user" | "context_header" | "context" | "hints"
    );
    let mut rows = build_inner(b, width.saturating_sub(u16::from(padded)), p);
    if padded {
        for (line, _) in &mut rows {
            line.spans.insert(0, Span::raw(" "));
        }
    }
    rows
}

fn build_inner(b: &Content, width: u16, p: &Palette) -> Rows {
    let viewport_width = usize::from(width);
    let width = viewport_width.max(8);
    let mut out: Rows = (0..b.gap).map(|_| (Line::default(), None)).collect();
    let op = &b.operation;
    match b.kind.as_str() {
        "user" => user(&mut out, b, width, p),
        "context_header" => {
            // Compact, marker-free context rows. Counts sit beside their labels;
            // project/global counts retain their distinct tones inside one bracket.
            let lead = 5usize.min(viewport_width);
            // Agent colour for titles, plus a thin left rail like the user card's (header lines only).
            let rail = color(&b.color, p.blue, p);
            for (n, chip) in b.members.iter().enumerate() {
                if n > 0 {
                    out.push((Line::default(), None));
                }
                // Empty blocks arrive with the neutral colour and are drawn greyed out.
                let rail = if chip.color.is_empty() {
                    rail
                } else {
                    color(&chip.color, rail, p)
                };
                let mut parts = vec![Span::styled(
                    chip.title.clone(),
                    Style::default().fg(rail).add_modifier(Modifier::BOLD),
                )];
                if !chip.counts.is_empty() {
                    parts.push(Span::styled(" [", Style::default().fg(p.muted)));
                    for (index, count) in chip.counts.iter().enumerate() {
                        if index > 0 {
                            parts.push(Span::raw(" "));
                        }
                        parts.push(Span::styled(
                            count.to_string(),
                            Style::default().fg(if chip.counts.len() == 2 && index == 0 {
                                p.quiet
                            } else {
                                p.muted
                            }),
                        ));
                    }
                    parts.push(Span::styled("]", Style::default().fg(p.muted)));
                }
                let status = chip.status.replace(" tokens", "");
                if !status.is_empty() {
                    parts.push(Span::styled(
                        format!("  {status}"),
                        Style::default().fg(p.muted),
                    ));
                }
                let w: usize = parts.iter().map(|s| s.content.width()).sum();
                let row = serde_json::json!({"kind":"context_chips","chips":[
                    {"start":lead,"end":(lead + w).min(viewport_width),"operation":chip.operation}]});
                out.push((
                    Line::from(
                        [
                            Span::raw(" ".repeat(2.min(lead))),
                            Span::styled("│", Style::default().fg(rail)),
                            Span::raw(" ".repeat(lead.saturating_sub(3))),
                        ]
                        .into_iter()
                        .chain(parts)
                        .collect::<Vec<_>>(),
                    ),
                    Some(row),
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
            // (Python pads both columns to equal width, like the terminal's EmptyHints).
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
                4,
                width,
                op,
            );
        }
        "thought" => {
            // `Thought: 671ms` in amber, then the reasoning dimmed behind a left rule.
            let rule = |strong: bool| {
                vec![
                    Span::raw("    "),
                    Span::styled(
                        "│",
                        Style::default().fg(if strong { p.border_strong } else { p.border }),
                    ),
                    Span::raw(" "),
                ]
            };
            let mut head = vec![Span::styled(
                b.title.clone(),
                Style::default().fg(p.warning),
            )];
            if !b.text.is_empty() {
                head.push(Span::raw("  "));
                head.push(Span::styled(b.text.clone(), Style::default().fg(p.quiet)));
            }
            for cells in wrap(&head, width.saturating_sub(8)) {
                out.push((line(rule(false), cells, None, Style::default()), op.clone()));
            }
            if !b.detail.is_empty() {
                out.push((Line::default(), op.clone()));
                for text in b.detail.lines() {
                    let trimmed = text.trim();
                    let heading = trimmed.len() > 4
                        && trimmed.starts_with("**")
                        && trimmed.ends_with("**")
                        && !trimmed[2..trimmed.len() - 2].contains("**");
                    let (shown, style) = if heading {
                        (
                            trimmed[2..trimmed.len() - 2].to_string(),
                            Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
                        )
                    } else {
                        (text.replace("**", ""), Style::default().fg(p.quiet))
                    };
                    for cells in wrap(&[Span::styled(shown, style)], width.saturating_sub(8)) {
                        out.push((line(rule(true), cells, None, Style::default()), op.clone()));
                    }
                }
            }
        }
        "markdown" => {
            let total = width.saturating_sub(2);
            for row in markdown::lines(&b.text, p, total.saturating_sub(4)) {
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
            // No ✓/✗ and no failure count: failed calls recover on their own, and
            // the expanded member still shows the error output in full.
            let running = b.status == "running";
            let glyph = if running {
                "\u{e000}"
            } else if b.collapsed {
                "›"
            } else {
                "⌄"
            };
            let tone = if running {
                color(&b.color, p.blue, p)
            } else {
                p.muted
            };
            out.push((
                Line::from(vec![
                    Span::raw("    "),
                    Span::styled(glyph, Style::default().fg(tone)),
                    Span::raw(" "),
                    Span::styled(
                        truncate(&b.text, width.saturating_sub(6)),
                        Style::default().fg(p.muted),
                    ),
                ]),
                op.clone(),
            ));
            for member in &b.members {
                let text = if member.kind == "thought" {
                    if member.detail.is_empty() {
                        format!("{} · Enter for reasoning", member.title)
                    } else {
                        member.title.clone()
                    }
                } else if member.heading.is_empty() {
                    format!("{:<8} {}", member.title, member.text)
                } else {
                    member.heading.clone()
                };
                out.push((
                    Line::from(vec![
                        Span::styled("    ", Style::default().fg(p.muted)),
                        Span::styled(
                            truncate(&text, width.saturating_sub(4)),
                            Style::default().fg(p.quiet),
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
                                    vec![Span::styled("    │ ", Style::default().fg(p.border))],
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
                                    vec![Span::styled("    │ ", Style::default().fg(p.border))],
                                    labels.last().unwrap().clone(),
                                    None,
                                    Style::default(),
                                ),
                                operation.clone(),
                            ));
                        }
                        for (i, cells) in wrap(
                            &[Span::styled(value.to_string(), Style::default().fg(p.text))],
                            width.saturating_sub(6 + label_width),
                        )
                        .into_iter()
                        .enumerate()
                        {
                            let prefix = vec![
                                Span::styled("    │ ", Style::default().fg(p.border)),
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
                            width.saturating_sub(6),
                        ) {
                            out.push((
                                line(
                                    vec![Span::styled("    │ ", Style::default().fg(p.border))],
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
                    out.extend(build_inner(child, width as u16, p));
                }
            }
        }
        "task" => {
            // The host supplies only the latest activity, never arrow-joined history.
            let running = b.status == "running";
            let room = width.saturating_sub(6);
            let title = truncate(&b.title, room);
            let metrics = if running
                && !b.metrics.is_empty()
                && title.width() + b.metrics.width() + 2 <= room
            {
                format!(
                    "{}{}",
                    " ".repeat(room - title.width() - b.metrics.width()),
                    b.metrics
                )
            } else {
                String::new()
            };
            out.push((
                Line::from(vec![
                    Span::raw("    "),
                    Span::styled(
                        if running { "\u{e000}" } else { " " },
                        Style::default().fg(color(&b.color, p.blue, p)),
                    ),
                    Span::raw(" "),
                    Span::styled(title, Style::default().fg(color(&b.color, p.blue, p))),
                    Span::styled(metrics.clone(), Style::default().fg(p.muted)),
                ]),
                op.clone(),
            ));
            let activity = if running { &b.text } else { &b.metrics };
            for text in activity.lines() {
                out.push((
                    Line::styled(
                        format!("      {}", truncate(text, room)),
                        Style::default().fg(p.muted),
                    ),
                    op.clone(),
                ));
            }
            if running && metrics.is_empty() && !b.metrics.is_empty() {
                out.push((
                    Line::styled(
                        format!("      {}", truncate(&b.metrics, room)),
                        Style::default().fg(p.muted),
                    ),
                    op.clone(),
                ));
            }
            for text in b.detail.lines() {
                indented(
                    &mut out,
                    vec![Span::styled(text.to_string(), Style::default().fg(p.muted))],
                    6,
                    width,
                    op,
                );
            }
        }
        "tool" => {
            let tone = p.quiet;
            let gutter = "    ";
            for text in b.text.lines() {
                out.push((
                    Line::styled(
                        format!("{}{}", gutter, truncate(text, width.saturating_sub(6))),
                        Style::default().fg(tone),
                    ),
                    op.clone(),
                ));
            }
            for text in b.detail.lines() {
                indented(
                    &mut out,
                    vec![Span::styled(text.to_string(), Style::default().fg(p.muted))],
                    6,
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
            4,
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

    #[test]
    fn agent_reply_padding_includes_double_digit_tool_counts_and_nested_content() {
        let p = Palette::new(false);
        for kind in [
            "agent",
            "thought",
            "markdown",
            "tool_group",
            "tool",
            "diff",
            "summary",
            "collapsed",
            "error",
        ] {
            let block = Content {
                kind: kind.into(),
                title: "Agent".into(),
                text: "reply".into(),
                count: 12,
                members: vec![Content {
                    title: "read".into(),
                    detail: "  path: file.txt".into(),
                    members: vec![Content {
                        kind: "markdown".into(),
                        text: "nested reply".into(),
                        ..Default::default()
                    }],
                    ..Default::default()
                }],
                ..Default::default()
            };
            let expected = build_inner(&block, 39, &p);
            let actual = build(&block, 40, &p);
            assert_eq!(actual.len(), expected.len());
            for ((line, op), (inner, inner_op)) in actual.iter().zip(&expected) {
                assert_eq!(line.spans[0].content, " ");
                assert_eq!(&line.spans[1..], inner.spans.as_slice());
                assert_eq!(op, inner_op);
                assert!(line.width() <= 40);
            }
        }
    }

    fn text(rows: &[Vec<Cell>]) -> Vec<String> {
        rows.iter()
            .map(|row| row.iter().map(|c| c.g.as_str()).collect())
            .collect()
    }

    #[test]
    fn markdown_tables_fit_without_rewrapping() {
        let p = Palette::new(false);
        let text = "| Name | Type | Required | Default | Description |\n|---|---|---|---|---|\n| offset | integer | no | — | 1-based first line to return (default 1). |\n| csv_as_markdown | boolean | no | — | Convert a CSV file to Markdown instead of reading its raw UTF-8 text. |";
        let block = Content {
            kind: "markdown".into(),
            text: text.into(),
            ..Default::default()
        };
        for width in 40u16..160 {
            let rows = build(&block, width, &p);
            let lines: Vec<String> = rows.iter().map(|(l, _)| l.to_string()).collect();
            assert!(
                lines.iter().all(|l| l.trim().is_empty() || l.trim_start().starts_with(['┌', '│', '├', '└'])),
                "width {width}: {lines:#?}"
            );
        }
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
        let header: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(header.starts_with("▾ │ "));
        let number = rows[1]
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
        assert!(header.starts_with("▾ │  hello") && header.trim_end().ends_with("#3"));
        let all: String = rows
            .iter()
            .flat_map(|(l, _)| l.spans.iter())
            .map(|s| s.content.as_ref())
            .collect();
        assert!(all.contains("▣ image 1") && all.contains("Click to inspect attached context"));
    }

    #[test]
    fn user_cards_have_top_and_bottom_padding_even_when_collapsed() {
        let p = Palette::new(false);
        for collapsed in [false, true] {
            let block = Content {
                kind: "user".into(),
                title: "hello".into(),
                collapsed,
                operation: Some(serde_json::json!({"kind": "turn_toggle", "id": "t"})),
                ..Default::default()
            };
            let rows = build(&block, 40, &p);
            assert_eq!(rows.len(), 3);
            for index in [0, rows.len() - 1] {
                let (line, operation) = &rows[index];
                assert_eq!(line.width(), 40);
                let text: String = line.spans.iter().map(|s| s.content.as_ref()).collect();
                assert_eq!(text.trim(), "│");
                assert_eq!(line.spans.last().unwrap().style.bg, Some(p.panel));
                assert_eq!(operation, &block.operation);
            }
        }
    }

    #[test]
    fn thought_shows_amber_title_and_reasoning_behind_a_rule() {
        let p = Palette::new(false);
        let block = Content {
            kind: "thought".into(),
            title: "Thought: 671ms".into(),
            detail: "**Exploring setup**\nneed to check".into(),
            ..Default::default()
        };
        let rows = build(&block, 60, &p);
        let lines: Vec<String> = rows
            .iter()
            .map(|(l, _)| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect();
        assert!(lines[0].contains("│ Thought: 671ms"));
        assert!(lines
            .iter()
            .any(|l| l.contains("│ Exploring setup") && !l.contains("**")));
        assert_eq!(rows[0].0.spans.last().unwrap().style.fg, Some(p.warning));
    }

    #[test]
    fn markdown_streamed_code_preserves_source_and_wraps_unicode() {
        let source =
            "```rust\n界界界界界界界界界界 \u{1f469}\u{200d}\u{1f4bb} e\u{301} **literal**\n";
        let block = Content {
            kind: "assistant".into(),
            text: source.into(),
            operation: Some(serde_json::json!({"action": "copy", "text": source})),
            ..Default::default()
        };
        for width in [16, 24, 60] {
            let rows = build(&block, width, &Palette::new(false));
            assert!(rows
                .iter()
                .all(|(line, op)| line.width() <= width as usize && op == &block.operation));
            let displayed = rows
                .iter()
                .flat_map(|(line, _)| line.spans.iter())
                .map(|span| span.content.as_ref())
                .collect::<String>();
            assert!(displayed.contains("\u{1f469}\u{200d}\u{1f4bb}"));
            assert!(displayed.contains("e\u{301}"));
            assert_eq!(block.text, source);
        }
    }

    #[test]
    fn tasks_show_latest_activity_metrics_and_quiet_completion() {
        let p = Palette::new(false);
        let mut task = Content {
            kind: "task".into(),
            title: "Explore map files".into(),
            status: "running".into(),
            text: "Read config.json".into(),
            metrics: "3 calls · 2s".into(),
            operation: Some(serde_json::json!({"action":"task","key":"t1"})),
            gap: 1,
            ..Default::default()
        };
        for width in [20, 80] {
            let rows = build(&task, width, &p);
            assert_eq!(rows[0].0.width(), 1); // shared transcript inset on the gap
            assert!(rows[1..]
                .iter()
                .all(|(line, op)| line.width() <= width as usize && op == &task.operation));
            let rendered = rows
                .iter()
                .map(|(line, _)| {
                    line.spans
                        .iter()
                        .map(|span| span.content.as_ref())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n");
            assert!(rendered.contains("Read config"));
            assert!(rendered.contains("3 calls · 2s"));
            assert!(!rendered.contains('→'));
            if width == 80 {
                assert_eq!(rows[1].0.width(), 80);
            }
        }
        task.status = "failed".into();
        task.detail = "Error: missing file".into();
        let rows = build(&task, 80, &p);
        let rendered = rows
            .iter()
            .map(|(line, _)| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n");
        assert!(
            !rendered.contains('✓') && !rendered.contains('✗') && !rendered.contains('\u{e000}')
        );
        assert!(!rendered.contains("Read config"));
        assert!(rendered.contains("Error: missing file"));
    }

    #[test]
    fn context_hit_ranges_are_clipped_to_the_viewport() {
        let block = Content {
            kind: "context_header".into(),
            members: vec![Content {
                title: "界 Skills".into(),
                counts: vec![0, 2],
                operation: Some(serde_json::json!({"action":"context","key":"skills"})),
                ..Default::default()
            }],
            ..Default::default()
        };
        for width in [0, 3, 12, 80] {
            let rows = build(&block, width, &Palette::new(false));
            let range = &rows[0].1.as_ref().unwrap()["chips"][0];
            assert!(range["start"].as_u64().unwrap() <= range["end"].as_u64().unwrap());
            assert!(range["end"].as_u64().unwrap() <= width as u64);
        }
        assert_eq!(truncate("界", 0), "");
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
        assert_eq!(
            rows[2].0.style.fg,
            Some(p.quiet),
            "failed tools are not marked red"
        );
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

    #[test]
    fn failed_tool_groups_carry_no_marks_and_header_chips_own_click_ranges() {
        let p = Palette::new(false);
        let group = Content {
            kind: "tool_group".into(),
            status: "completed".into(),
            text: "Grep pattern=x".into(),
            count: 3,
            failures: 2,
            ..Default::default()
        };
        let text: String = build(&group, 60, &p)[0]
            .0
            .spans
            .iter()
            .map(|s| s.content.as_ref())
            .collect();
        assert!(
            !text.contains('✗') && !text.contains('✓') && !text.contains("failed"),
            "{text}"
        );
        let chip = |title: &str, counts: Vec<usize>, key: &str| Content {
            title: title.into(),
            counts,
            operation: Some(serde_json::json!({"kind":"context_show","key":key})),
            ..Default::default()
        };
        let header = Content {
            kind: "context_header".into(),
            members: vec![
                chip("Tools", vec![13], "tools"),
                chip("Skills", vec![1, 3], "skills"),
            ],
            ..Default::default()
        };
        let rows = build(&header, 80, &p);
        assert_eq!(rows.len(), 3, "one chip per row, a blank row between");
        assert!(rows[1].0.spans.is_empty() || rows[1].1.is_none());
        let line =
            |i: usize| -> String { rows[i].0.spans.iter().map(|s| s.content.as_ref()).collect() };
        // Marker-free labels share an inset; bracketed counts follow immediately.
        assert!(line(0).starts_with("  │  Tools [13]"), "{}", line(0));
        assert!(line(2).starts_with("  │  Skills [1 3]"), "{}", line(2));
        assert!(line(2).contains("Skills [1 3]"), "{}", line(2));
        assert!(rows[0].0.spans[3]
            .style
            .add_modifier
            .contains(Modifier::BOLD));
        assert!(!line(0).contains(['◈', '·']));
        for (i, key) in [(0, "tools"), (2, "skills")] {
            let op = rows[i].1.clone().unwrap();
            assert_eq!(op["kind"], "context_chips");
            let chips = op["chips"].as_array().unwrap();
            assert_eq!(chips.len(), 1);
            assert_eq!(chips[0]["start"], 5);
            assert!(chips[0]["end"].as_u64().unwrap() > 5);
            assert_eq!(chips[0]["operation"]["key"], key);
        }
        let project = rows[2].0.spans.iter().find(|s| s.content == "1").unwrap();
        assert_eq!(project.style.fg, Some(p.quiet), "project count is greyed");
    }
}
