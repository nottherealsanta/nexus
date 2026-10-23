//! Transcript blocks to styled, wrapped rows, for the native timeline. Python decides order
//! and blank-row gaps; this module only draws and wraps at word boundaries.
//!
//! Activity drawing lives in submodules, each pure over `Content` and `Palette`:
//! `groups` (verb groups), `thought` (reasoning), `tools` (standalone tool rows),
//! `signal` (a tool's relevant-only rows), `header` (context headers) and `look` (glyphs,
//! time format and the live-run marker the draw loop animates).
use crate::{bridge::Content, markdown, render::Palette};
pub(crate) mod fold;
pub(crate) mod groups;
mod header;
pub mod look;
mod route;
mod signal;
mod thought;
mod tools;
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
use serde_json::{json, Value};
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

/// Unwrapped cells for one row of text.
fn plain_cells(text: &str, style: Style) -> Vec<Cell> {
    text.graphemes(true)
        .map(|g| Cell {
            g: g.to_string(),
            w: g.width(),
            style,
            space: g.chars().all(char::is_whitespace),
        })
        .collect()
}

/// `image 1 · a.png` → `Image 1 · a.png`.
fn capitalized(text: &str) -> String {
    let mut chars = text.chars();
    chars
        .next()
        .map(|first| first.to_uppercase().chain(chars).collect())
        .unwrap_or_default()
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

fn user(out: &mut Rows, b: &Content, full: usize, p: &Palette) {
    // One cell of right margin keeps the card off the column edge (added per row below,
    // unstyled, so the Copy button stays inside the card).
    let width = full.saturating_sub(1).max(1);
    let first = out.len();
    user_card(out, b, width, p);
    if full > width {
        for (row, _) in &mut out[first..] {
            row.spans.push(Span::raw(" "));
        }
    }
}

/// A delivered background subagent report: agent, task and status on one heading row, then at
/// most two preview rows (each clipped with `…`) and a hint naming what was left out. Every row
/// opens the full report (`operation`).
fn agent_report(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    const LEAD: &str = "  ⮑ ";
    let op = &b.operation;
    // Preview rows and the hint sit under the heading content, right after the icon.
    let body = " ".repeat(LEAD.width());
    let room = width.saturating_sub(body.width() + 1).max(1);
    let name = if b.agent_name.is_empty() {
        "subagent"
    } else {
        b.agent_name.as_str()
    };
    // Same name treatment as a subagent call's first line: capitalized, in the agent's colour
    // (error tint on failure), then the task two columns after it.
    let failed = b.status == "failed";
    let name_color = if failed {
        p.error
    } else {
        color(&b.color, p.blue, p)
    };
    let shown_name = capitalize(name);
    let used = LEAD.width() + shown_name.width();
    let mut head = vec![
        Span::styled(LEAD, Style::default().fg(p.quiet)),
        Span::styled(shown_name, Style::default().fg(name_color)),
    ];
    let mut tail = String::new();
    if !b.task.is_empty() {
        tail.push_str("  ");
        tail.push_str(&b.task);
    }
    if !b.status.is_empty() {
        tail.push_str(" · ");
        tail.push_str(&b.status);
    }
    head.push(Span::styled(
        truncate(&tail, width.saturating_sub(used + 1)),
        Style::default().fg(p.muted),
    ));
    out.push((Line::from(head), op.clone()));
    let mut clipped = b.count;
    for text in b
        .text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .take(2)
    {
        let shown = truncate(text, room);
        clipped += usize::from(shown != text);
        out.push((
            Line::from(vec![
                Span::raw(body.clone()),
                Span::styled(shown, Style::default().fg(p.text)),
            ]),
            op.clone(),
        ));
    }
    if clipped > 0 {
        let hint = if b.count > 0 {
            format!(
                "… {} more line{} · open for the full report",
                b.count,
                if b.count == 1 { "" } else { "s" }
            )
        } else {
            "… clipped · open for the full report".to_string()
        };
        out.push((
            Line::from(vec![
                Span::raw(body),
                Span::styled(truncate(&hint, room), Style::default().fg(p.quiet)),
            ]),
            op.clone(),
        ));
    }
}

fn user_card(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let base = Style::default().fg(p.text).bg(p.element);
    // The composer's card shape: panel background and padding, no coloured left rail.
    // The rail's column stays panel-coloured so the prompt text does not move.
    let bar = |_: ()| {
        vec![
            Span::styled("  ", Style::default().bg(p.background)),
            Span::styled(" ", base),
            Span::styled("  ", base),
        ]
    };
    let inner = width.saturating_sub(7).max(1);
    // Every card row carries the Copy marker; the button sits on this first row.
    let op = &crate::copy_button::mark(&b.id, None, &b.operation);
    out.push((line(bar(()), vec![], Some(width), base), op.clone()));
    // A turn started by a GPT Live delegation says so beside its number.
    let mut tags = Vec::new();
    if b.status == "live" {
        tags.push("via voice".to_string());
    } else if b.status == "steer" {
        tags.push("steer".to_string());
    } else if b.status == "report" {
        tags.push("subagent report".to_string());
    } else if b.status == "followup" {
        tags.push("from root".to_string());
    }
    if b.number > 0 {
        tags.push(format!("#{}", b.number));
    }
    if !b.sent_at.is_empty() {
        tags.push(b.sent_at.clone());
    }
    let tag = if tags.is_empty() {
        String::new()
    } else {
        format!(" {} ", tags.join(" · "))
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
                // The fold chevron sits in the left margin, outside the card, barely visible.
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
            let (quiet, clock) = if b.sent_at.is_empty() {
                (tag.as_str(), "")
            } else {
                let split = tag.len() - b.sent_at.len() - 1;
                (&tag[..split], &tag[split..])
            };
            row.spans
                .push(Span::styled(quiet.to_string(), base.fg(p.quiet)));
            if !clock.is_empty() {
                row.spans
                    .push(Span::styled(clock.to_string(), base.fg(p.muted)));
            }
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
        let chip_op = crate::copy_button::mark(
            &b.id,
            None,
            &b.chip_operation.clone().or_else(|| b.operation.clone()),
        );
        out.push((line(bar(()), vec![], Some(width), base), chip_op.clone()));
        // Each chip is its own click target (`context_chips` ranges): an image chip
        // opens that image, others the message page. Chips wrap whole, never split.
        let chip_style = Style::default()
            .fg(p.blue)
            .bg(p.element)
            .add_modifier(Modifier::BOLD);
        let mut rows: Vec<Vec<(String, Option<Value>)>> = vec![Vec::new()];
        let mut used = 0;
        for (index, chip) in b.chips.iter().enumerate() {
            let label = capitalized(chip);
            let label = if label.starts_with("Image ") {
                label.split(" · ").next().unwrap_or(&label)
            } else {
                &label
            };
            let mut label = format!(" ▣ {label} ");
            if label.width() > inner {
                label = truncate(&label, inner);
            }
            let w = label.width();
            if used > 0 && used + w > inner {
                rows.push(Vec::new());
                used = 0;
            }
            used += w + 2;
            let operation = b
                .chip_operations
                .get(index)
                .cloned()
                .or_else(|| b.chip_operation.clone());
            rows.last_mut().unwrap().push((label, operation));
        }
        for chips in rows {
            let mut cells = Vec::new();
            let mut ranges = Vec::new();
            let mut at = 5;
            for (label, operation) in chips {
                let w = label.width();
                ranges.push(json!({"start": at, "end": at + w, "operation": operation}));
                cells.extend(plain_cells(&label, chip_style));
                cells.extend(plain_cells("  ", Style::default()));
                at += w + 2;
            }
            let row = Some(json!({
                "kind": "context_chips",
                "chips": ranges,
                "default": b.chip_operation.clone().or_else(|| b.operation.clone()),
            }));
            out.push((
                line(bar(()), cells, Some(width), base),
                crate::copy_button::mark(&b.id, None, &row),
            ));
        }
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

/// One file change or question, never grouped: `› Edit a.py  +2 −1`, then (open) the
/// unified diff with added lines green and removed lines red, or the call's details.
fn change(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let op = &b.operation;
    let running = b.status == "running";
    let failed = b.status == "failed";
    let glyph = if running {
        "\u{e000}"
    } else if b.collapsed {
        "›"
    } else {
        "⌄"
    };
    let mut counts = vec![];
    if b.files > 1 {
        counts.push(Span::raw("  "));
        counts.push(Span::styled(
            format!("{} files", b.files),
            Style::default().fg(p.muted),
        ));
    }
    for (n, sign, tone) in [(b.added, "+", p.success), (b.removed, "−", p.error)] {
        if n > 0 {
            counts.push(Span::raw(if counts.is_empty() { "  " } else { " " }));
            counts.push(Span::styled(
                format!("{sign}{n}"),
                Style::default().fg(tone),
            ));
        }
    }
    let mut head = vec![
        Span::raw("    "),
        Span::styled(
            glyph,
            Style::default().fg(if running { p.text } else { p.muted }),
        ),
        Span::raw(" "),
        Span::styled(b.text.clone(), Style::default().fg(p.muted)),
    ];
    head.extend(counts);
    if !b.heading.is_empty() {
        head.push(Span::styled(
            format!(" · {}", b.heading),
            Style::default().fg(if failed { p.error } else { p.quiet }),
        ));
    }
    for (i, cells) in wrap(&head[1..], width.saturating_sub(6))
        .into_iter()
        .enumerate()
    {
        let lead = if i == 0 { "    " } else { "      " };
        out.push((
            line(vec![Span::raw(lead)], cells, None, Style::default()),
            op.clone(),
        ));
    }
    if b.collapsed {
        return;
    }
    let rows_op = b.output_operation.clone().or_else(|| op.clone());
    let digits = b
        .diff_lines
        .iter()
        .map(|row| row.1)
        .max()
        .unwrap_or(0)
        .to_string()
        .len()
        .max(2);
    let total = width.saturating_sub(2);
    let room = total.saturating_sub(6 + digits + 3).max(4);
    for (kind, number, text) in &b.diff_lines {
        let (sign, fg, bg) = match kind.as_str() {
            "add" => ("+", p.success, Some(p.diff_add)),
            "del" => ("-", p.error, Some(p.diff_del)),
            "ctx" => (" ", p.muted, None),
            "sep" => {
                out.push((
                    Line::styled("      ⋯", Style::default().fg(p.quiet)),
                    rows_op.clone(),
                ));
                continue;
            }
            "file" => {
                indented(
                    out,
                    vec![Span::styled(
                        text.clone(),
                        Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
                    )],
                    6,
                    width,
                    &rows_op,
                );
                continue;
            }
            _ => {
                indented(
                    out,
                    vec![Span::styled(text.clone(), Style::default().fg(p.quiet))],
                    6,
                    width,
                    &rows_op,
                );
                continue;
            }
        };
        let base = bg.map(|bg| Style::default().bg(bg)).unwrap_or_default();
        let shown = if *number == 0 {
            " ".repeat(digits)
        } else {
            format!("{number:>digits$}")
        };
        for (i, cells) in wrap(&[Span::styled(text.clone(), Style::default().fg(fg))], room)
            .into_iter()
            .enumerate()
        {
            let prefix = vec![
                Span::raw("      "),
                Span::styled(
                    if i == 0 {
                        format!("{shown} {sign} ")
                    } else {
                        " ".repeat(digits + 3)
                    },
                    base.fg(if i == 0 { fg } else { p.quiet }),
                ),
            ];
            out.push((line(prefix, cells, Some(total), base), rows_op.clone()));
        }
    }
    for text in b.detail.lines() {
        indented(
            out,
            vec![Span::styled(
                text.to_string(),
                Style::default().fg(if text.starts_with(' ') {
                    p.text
                } else {
                    p.muted
                }),
            )],
            6,
            width,
            op,
        );
    }
}

/// The GPT Live timeline card. `b.detail` holds one `tone TAB label TAB text` row per line
/// (`nexus/ui_support/live_card.py`); the card opens and closes by sending `b.operation`.
fn live_card(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let op = &b.operation;
    let rail = color(&b.color, p.purple, p);
    let glyph = if b.collapsed { "›" } else { "⌄" };
    let mut head = vec![
        Span::styled(glyph.to_string(), Style::default().fg(p.muted)),
        Span::raw(" "),
        Span::styled(
            " Live ",
            Style::default()
                .fg(p.background)
                .bg(rail)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" "),
        Span::styled(b.title.clone(), Style::default().fg(p.text)),
    ];
    if !b.status.is_empty() {
        head.push(Span::styled(
            format!(" · {}", b.status),
            Style::default().fg(if b.status == "ended" {
                p.quiet
            } else {
                p.success
            }),
        ));
    }
    indented(out, head, 2, width, op);
    out.extend(live_rows(&b.detail, rail, 4, width, p, op));
}

/// Labelled Live rows (`tone TAB label TAB text` per line) behind a `│` rail, `indent`
/// columns in, wrapped to `width` with a hanging indent. Shared by the timeline card and
/// the Live pane below the composer.
pub fn live_rows(
    detail: &str,
    rail: Color,
    indent: usize,
    width: usize,
    p: &Palette,
    op: &Option<Value>,
) -> Rows {
    let mut out = Rows::new();
    for row in detail.lines() {
        let mut parts = row.splitn(3, '\t');
        let tone = parts.next().unwrap_or("");
        let label = parts.next().unwrap_or("");
        let text = parts.next().unwrap_or("");
        let (label_style, text_style) = match tone {
            "you" => (Style::default().fg(p.muted), Style::default().fg(p.text)),
            "voice" => (Style::default().fg(p.blue), Style::default().fg(p.text)),
            "caption" => (
                Style::default().fg(p.quiet),
                Style::default().fg(p.quiet).add_modifier(Modifier::ITALIC),
            ),
            "sent" => (Style::default().fg(p.accent), Style::default().fg(p.text)),
            "told" => (Style::default().fg(p.cyan), Style::default().fg(p.muted)),
            "bad" => (Style::default().fg(p.error), Style::default().fg(p.error)),
            "note" => (Style::default().fg(p.quiet), Style::default().fg(p.quiet)),
            _ => (Style::default().fg(p.quiet), Style::default().fg(p.muted)),
        };
        // Hanging indent: wrapped text lines up under the first row's text, not under the label.
        let label_width = if label.is_empty() {
            2
        } else {
            label.width() + 2
        };
        let room = width.saturating_sub(indent + 2 + label_width).max(8);
        let mut wrapped = wrap(&[Span::styled(text.to_string(), text_style)], room);
        if wrapped.is_empty() {
            wrapped.push(Vec::new()); // a blank line keeps its row
        }
        for (i, cells) in wrapped.into_iter().enumerate() {
            let mut prefix = vec![
                Span::raw(" ".repeat(indent)),
                Span::styled("│ ", Style::default().fg(rail)),
            ];
            if i == 0 && !label.is_empty() {
                prefix.push(Span::styled(
                    label.to_string(),
                    label_style.add_modifier(Modifier::BOLD),
                ));
                prefix.push(Span::raw("  "));
            } else {
                prefix.push(Span::raw(" ".repeat(label_width)));
            }
            out.push((line(prefix, cells, None, Style::default()), op.clone()));
        }
    }
    out
}

pub fn build(b: &Content, width: u16, p: &Palette) -> Rows {
    let padded = !matches!(
        b.kind.as_str(),
        "user" | "context_header" | "context" | "hints"
    );
    let mut rows = build_inner(b, width.saturating_sub(u16::from(padded)), p);
    if padded {
        for (line, op) in &mut rows {
            if let Some(image) = op.as_mut().and_then(|op| op.get_mut("inline_image")) {
                image["indent"] = json!(image["indent"].as_u64().unwrap_or(4) + 1);
            }
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
        "agent_report" => agent_report(&mut out, b, width, p),
        "context_header" => header::draw(&mut out, b, viewport_width, p),
        "live" => live_card(&mut out, b, width, p),
        "context" => header::chip(&mut out, b, width, p),
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
        // The transcript names no agent: only the composer and a subagent's own row do.
        "thought" => thought::draw(&mut out, b, width, p, look::now_ms()),
        "markdown" => {
            let total = width.saturating_sub(2);
            // Per pushed row: leading spans to skip when it is prose (the write head only
            // glows prose; code and tables keep their colours), `None` otherwise.
            let mut prose: Vec<Option<usize>> = Vec::new();
            let first_row = out.len();
            for row in markdown::lines(&b.text, p, total.saturating_sub(4)) {
                if row.blank {
                    out.push((Line::default(), op.clone()));
                    prose.push(None);
                    continue;
                }
                if let Some((source, width, strip)) = row.image {
                    let mut operation = crate::copy_button::mark(&b.id, row.fence, op).unwrap();
                    operation["inline_image"] = json!({"source":source,"width":width,"row":strip,"indent":4 + row.prefix.iter().map(|s| s.content.width()).sum::<usize>()});
                    out.push((
                        Line::from(vec![Span::raw(" ".repeat(total))]),
                        Some(operation),
                    ));
                    prose.push(None);
                    continue;
                }
                let is_prose = row.fence.is_none()
                    && row.bg.is_none()
                    && !row
                        .spans
                        .first()
                        .and_then(|span| span.content.chars().next())
                        .is_some_and(|c| ('\u{2500}'..='\u{257f}').contains(&c));
                let prefix_width: usize = row.prefix.iter().map(|span| span.content.width()).sum();
                let room = total.saturating_sub(4 + prefix_width).max(1);
                let base = row.bg.map(|bg| Style::default().bg(bg)).unwrap_or_default();
                let row_op = match row.fence {
                    Some(fence) => crate::copy_button::mark(&b.id, Some(fence), op),
                    None => op.clone(),
                };
                for (i, cells) in wrap(&row.spans, room).into_iter().enumerate() {
                    let mut lead = vec![Span::raw("    ")];
                    if i == 0 {
                        lead.extend(row.prefix.iter().cloned());
                    } else {
                        // Hanging indent: wrapped rows line up under the first row's text.
                        lead.push(Span::styled(" ".repeat(prefix_width), base));
                    }
                    prose.push(is_prose.then_some(lead.len()));
                    out.push((
                        line(lead, cells, row.bg.map(|_| total), base),
                        row_op.clone(),
                    ));
                }
            }
            if b.streaming {
                // The glow is part of the row (rows are rebuilt as text arrives); the draw loop
                // cools it when animation is off. Only the trailing prose glows: blank rows
                // are skipped, a code block or table ends the head.
                let mut seen = 0;
                for (index, skip) in prose.iter().enumerate().rev() {
                    let row = &mut out[first_row + index].0;
                    match skip {
                        Some(skip) => {
                            seen = look::write_head(
                                row,
                                *skip,
                                seen,
                                look::HEAD_LEN,
                                p.text,
                                p.accent,
                            );
                            if seen >= look::HEAD_LEN {
                                break;
                            }
                        }
                        None if row.spans.is_empty() => {}
                        None => break,
                    }
                }
            }
        }
        "tool_group" => groups::draw(&mut out, b, width, p),
        "task" => {
            let running = b.status == "running";
            let failed = b.status == "failed" || b.detail.starts_with("Error:");
            let agent_color = color(&b.color, p.blue, p);
            // Not expandable, so no disclosure mark: a failure tints the name instead.
            let name_color = if failed { p.error } else { agent_color };

            let raw_name = if !b.agent_name.is_empty() {
                b.agent_name.as_str()
            } else {
                let title = &b.title;
                match title.find(" Subagent") {
                    Some(at) => &title[..at],
                    None => "task",
                }
            };
            // Tool-group rows start with a 4-column gutter and a two-column "▸ " slot; the
            // task row keeps the gutter and puts its status icon plus a space where the slot
            // is, so its name lands on the column where a group's verb text starts. The icon
            // is the animated spinner while running and a full braille cell once done, both
            // at half the agent colour against the background (a failure tints from error).
            const GUTTER: &str = "    ";
            const LEAD: &str = "      ";
            const LEAD_WIDTH: usize = LEAD.len();
            let icon = if running { look::SLOT } else { "\u{28ff}" };
            let icon_tone = nexus_widgets::theme::mix(
                if failed { p.error } else { agent_color },
                p.background,
                0.5,
            );
            let name_field = truncate(&capitalize(raw_name), width.saturating_sub(LEAD_WIDTH));

            let raw_task = if !b.task.is_empty() {
                b.task.as_str()
            } else {
                match b.title.find(" — ") {
                    Some(at) => &b.title[at + 4..],
                    None => b.title.as_str(),
                }
            };

            let prefix_width = LEAD_WIDTH + name_field.width() + 2;
            let mut head = vec![
                Span::raw(GUTTER),
                Span::styled(icon, Style::default().fg(icon_tone)),
                Span::raw(" "),
                Span::styled(name_field, Style::default().fg(name_color)),
            ];

            let room1 = width.saturating_sub(prefix_width);
            if !raw_task.is_empty() && room1 > 0 {
                head.push(Span::raw("  "));
                let task_text = truncate(raw_task, room1);
                let task_style = if running {
                    Style::default().fg(p.text)
                } else {
                    Style::default().fg(p.muted)
                };
                head.push(Span::styled(task_text, task_style));
            }
            out.push((Line::from(head), op.clone()));

            let mut model_part = String::new();
            let model = b.model.trim();
            let effort = b.effort.trim();
            let tier = b.tier.trim();
            if !model.is_empty() {
                model_part.push_str(model);
                if !effort.is_empty() {
                    model_part.push('-');
                    model_part.push_str(effort);
                }
                if !tier.is_empty() {
                    model_part.push_str(&format!(" ({tier})"));
                }
            } else if !tier.is_empty() {
                if !effort.is_empty() {
                    model_part.push_str(&format!("{effort} ({tier})"));
                } else {
                    model_part.push_str(&format!("({tier})"));
                }
            }

            let tools_part = b.tool_calls.map(|calls| {
                if calls == 1 {
                    "1 tool".to_string()
                } else {
                    format!("{calls} tools")
                }
            });

            let tokens_part = b
                .tokens
                .count()
                .map(|tok| format!("{} tokens", compact_tokens(tok)));

            let time_part = if running {
                if b.started_ms > 0 {
                    let now = look::now_ms();
                    if now >= b.started_ms {
                        Some(format_duration(now - b.started_ms))
                    } else {
                        None
                    }
                } else {
                    None
                }
            } else if b.elapsed_ms > 0 {
                Some(format_duration(b.elapsed_ms))
            } else {
                None
            };

            let mut pieces: Vec<(String, Style)> = Vec::new();
            if !b.state.is_empty() {
                // The child outlives its runs: say whether it is idle, stopped or interrupted.
                let tone = if b.state.starts_with("stopped") || b.state.starts_with("interrupted") {
                    p.warning
                } else {
                    p.muted
                };
                pieces.push((b.state.clone(), Style::default().fg(tone)));
            }
            if !model_part.is_empty() {
                pieces.push((model_part, Style::default().fg(p.quiet)));
            }
            if let Some(tools) = tools_part {
                pieces.push((tools, Style::default().fg(p.quiet)));
            }
            if let Some(tokens) = tokens_part {
                pieces.push((tokens, Style::default().fg(p.quiet)));
            }
            // An external worker runs its own tools: its count is a darker gray than the rest.
            // ANSI DarkGray is no darker than `quiet` on the night theme, so scale `quiet`.
            if let Some(calls) = b.external_tool_calls {
                let noun = if calls == 1 { "tool" } else { "tools" };
                let darker = match p.quiet {
                    Color::Rgb(r, g, b) => {
                        let dim = |c: u8| (u16::from(c) * 3 / 4) as u8;
                        Color::Rgb(dim(r), dim(g), dim(b))
                    }
                    _ => Color::DarkGray,
                };
                pieces.push((
                    format!("{calls} external {noun}"),
                    Style::default().fg(darker),
                ));
            }
            if let Some(time_str) = time_part {
                let time_style = if running {
                    Style::default().fg(p.muted)
                } else {
                    Style::default().fg(p.quiet)
                };
                pieces.push((time_str, time_style));
            }

            let mut line2_spans = vec![Span::raw(LEAD)];
            let sep = " \u{b7} ";
            let mut total_width = 0;
            let room2 = width.saturating_sub(LEAD.len() + 1);
            for (i, (text, style)) in pieces.into_iter().enumerate() {
                let prefix = if i > 0 { sep } else { "" };
                let full = format!("{prefix}{text}");
                if total_width + full.width() > room2 {
                    let remaining = room2.saturating_sub(total_width);
                    if remaining > 0 {
                        let truncated = truncate(&full, remaining);
                        if !truncated.is_empty() {
                            line2_spans.push(Span::styled(truncated, style));
                        }
                    }
                    break;
                }
                total_width += full.width();
                if i > 0 {
                    line2_spans.push(Span::styled(sep, Style::default().fg(p.quiet)));
                }
                line2_spans.push(Span::styled(text, style));
            }
            if line2_spans.len() > 1 {
                out.push((Line::from(line2_spans), op.clone()));
            }
            if let Some(route) = &b.route {
                route::draw(&mut out, route, width, p, op);
            }

            for text in b.detail.lines() {
                indented(
                    &mut out,
                    vec![Span::styled(text.to_string(), Style::default().fg(p.muted))],
                    5,
                    width,
                    op,
                );
            }
        }
        "tool" => tools::draw(&mut out, b, width, p),
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
        "external" => {
            // External Claude Code worker: a labelled authority/outcome card. Attention
            // (warning) and failure (error) are distinct from the neutral running/done card.
            let tone = match b.status.as_str() {
                "attention" => p.warning,
                "failed" => p.error,
                "done" => p.success,
                _ => p.accent,
            };
            let marker = if b.status == "attention" {
                "\u{25B2} "
            } else {
                "\u{25C6} "
            };
            indented(
                &mut out,
                vec![Span::styled(
                    format!("{marker}{}", b.title),
                    Style::default().fg(tone).add_modifier(Modifier::BOLD),
                )],
                2,
                width,
                op,
            );
            for text in b.text.lines() {
                let (label, value) = text.split_once(": ").unwrap_or(("", text));
                let mut spans = Vec::new();
                if !label.is_empty() {
                    spans.push(Span::styled(
                        format!("{label}: "),
                        Style::default().fg(p.muted),
                    ));
                }
                spans.push(Span::styled(
                    value.to_string(),
                    Style::default().fg(if b.status == "attention" && label.starts_with("Needs") {
                        p.warning
                    } else {
                        p.text
                    }),
                ));
                indented(&mut out, spans, 4, width, op);
            }
        }
        "error" => indented(
            &mut out,
            vec![Span::styled(b.text.clone(), Style::default().fg(p.error))],
            4,
            width,
            op,
        ),
        "diff" => diff(&mut out, b, width, p),
        "change" => change(&mut out, b, width, p),
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

fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
    }
}

pub fn compact_tokens(value: u64) -> String {
    if value >= 1_000_000 {
        let val = value as f64 / 1_000_000.0;
        let s = format!("{val:.1}M");
        s.replace(".0M", "M")
    } else if value >= 1_000 {
        let val = value as f64 / 1_000.0;
        let s = format!("{val:.1}K");
        s.replace(".0K", "K")
    } else {
        value.to_string()
    }
}

pub fn format_duration(ms: u64) -> String {
    if ms < 60_000 {
        format!("{:.1}s", ms as f64 / 1000.0)
    } else if ms < 3_600_000 {
        format!("{}m {}s", ms / 60_000, (ms / 1000) % 60)
    } else {
        format!("{}h {}m", ms / 3_600_000, (ms / 60_000) % 60)
    }
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
            "change",
            "summary",
            "collapsed",
            "error",
            "live",
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
    fn external_worker_card_is_labelled_and_attention_is_distinct() {
        let p = Palette::new(false);
        let card = |status: &str| {
            Content {
            kind: "external".into(),
            status: status.into(),
            title: "Claude Code needs attention".into(),
            text: "Needs attention: trust this folder yourself with `cd /w && claude`\nUsage: unknown (this is not zero)".into(),
            ..Default::default()
        }
        };
        let render = |status: &str| build(&card(status), 60, &p);
        let attention = render("attention");
        let lines: Vec<String> = attention.iter().map(|(l, _)| l.to_string()).collect();
        assert!(
            lines[0].contains("Claude Code needs attention"),
            "{lines:#?}"
        );
        let joined = lines.join(" ");
        assert!(
            joined.contains("Needs attention:") && joined.contains("claude"),
            "{joined}"
        );
        assert!(joined.contains("Usage: unknown"), "{joined}");
        let color = |rows: &Rows| rows[0].0.spans.iter().find_map(|s| s.style.fg);
        assert_eq!(color(&attention), Some(p.warning));
        assert_eq!(color(&render("failed")), Some(p.error));
        assert_ne!(color(&attention), color(&render("running")));
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
                lines.iter().all(
                    |l| l.trim().is_empty() || l.trim_start().starts_with(['┌', '│', '├', '└'])
                ),
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

    fn member(kind: &str, status: &str, title: &str, text: &str) -> Content {
        Content {
            kind: kind.into(),
            status: status.into(),
            title: title.into(),
            text: text.into(),
            ..Default::default()
        }
    }

    #[test]
    fn rows_stay_inside_the_pane_and_wrap_at_words() {
        let p = Palette::new(false);
        let sentence = "The spawn path and settings surface are laid against the edge of the pane and must wrap.";
        let markdown = Content {
            kind: "markdown".into(),
            text: format!(
                "{sentence}\n\n- {sentence}\n\n> {sentence}\n\n```\n{sentence}\n{sentence}\n```"
            ),
            ..Default::default()
        };
        let running = Content {
            kind: "tool_group".into(),
            status: "running".into(),
            running_label: "Working".into(),
            members: vec![
                member(
                    "tool",
                    "completed",
                    "Glob",
                    "pattern=nexus/**/events*.py path=/Users/santa/repos/nexus",
                ),
                member(
                    "tool",
                    "running",
                    "Bash",
                    "rg -n \"router.resol|router_resolve|resolve_route\" nexus rust",
                ),
            ],
            ..Default::default()
        };
        let finished = Content {
            kind: "tool_group".into(),
            status: "completed".into(),
            collapsed: true,
            text: "Read 2 files, Ran 1 command, Searched 3 patterns, Listed 4 folders".into(),
            elapsed_label: "6.6s".into(),
            ..Default::default()
        };
        let signal = Content {
            kind: "tool".into(),
            status: "failed".into(),
            text: "Code code purpose=Locate usage, router, resolver and spawn paths".into(),
            signal: vec![
                (
                    "text".into(),
                    "… 1. grep path=\"nexus\", pattern=\"def resolve_router_target\"".into(),
                ),
                (
                    "out".into(),
                    "1  files = grep(path=\"nexus\", pattern=\"def resolve_router\")".into(),
                ),
                (
                    "error".into(),
                    "ToolError (line 4): Not a directory: nexus/router/resolve.py/__init__".into(),
                ),
            ],
            ..Default::default()
        };
        let detail = Content {
            kind: "tool".into(),
            text: "$ cargo test".into(),
            detail: "  Command: cargo test --workspace --offline -- --nocapture transcript\nplain row that is quite long and must wrap too".into(),
            ..Default::default()
        };
        let mut group = running.clone();
        group.collapsed = false;
        group.members[0].signal = signal.signal.clone();
        group.members[0].heading = "→ Glob".into();
        let blocks = [markdown, running, finished, signal, detail, group];
        for width in [40u16, 60, 80, 100] {
            for block in &blocks {
                for (row, _) in build(block, width, &p) {
                    assert!(
                        row.width() <= usize::from(width),
                        "{} at {width}: {:?} is {} wide",
                        block.kind,
                        row.to_string(),
                        row.width()
                    );
                }
            }
            let rows = build(&blocks[0], width, &p);
            let joined: Vec<String> = rows.iter().map(|(l, _)| l.to_string()).collect();
            let words: Vec<&str> = sentence.split(' ').collect();
            let flat = joined
                .iter()
                .map(|l| l.trim())
                .collect::<Vec<_>>()
                .join(" ");
            assert!(flat.contains(&words[words.len() - 2..].join(" ")), "{flat}");
            for line in &joined {
                for word in line.split_whitespace() {
                    // Rows may break after whitespace or start with a bullet/quote marker, but
                    // a word must never be split ("settin" / "gs").
                    assert!(
                        words.contains(&word)
                            || word.starts_with(['-', '>', '│', '•'])
                            || word.chars().count() < 3,
                        "{word:?} broke mid-word at {width}: {joined:#?}"
                    );
                }
            }
        }
    }

    #[test]
    fn user_card_chips_open_their_own_attachment() {
        let p = Palette::new(false);
        let image = serde_json::json!({"kind":"submitted_image","id":"m","index":2});
        let page = serde_json::json!({"kind":"message_page","id":"m"});
        let block = Content {
            id: "m:user".into(),
            kind: "user".into(),
            title: "see [image 1]".into(),
            chips: vec!["image 1 · a.png".into(), "document 2 · b.md · 1 KB".into()],
            chip_operation: Some(page.clone()),
            chip_operations: vec![image.clone(), page.clone()],
            ..Default::default()
        };
        let rows = build(&block, 80, &p);
        let (line, op) = rows
            .iter()
            .find(|(line, _)| line.to_string().contains("▣ Image 1"))
            .unwrap();
        let text = line.to_string();
        assert!(
            !text.contains("a.png"),
            "image filenames stay hidden: {text}"
        );
        assert!(
            text.contains("▣ Image 1") && text.contains("Document 2 · b.md · 1 KB"),
            "{text}"
        );
        let title_row = rows
            .iter()
            .position(|(l, _)| l.to_string().contains("see [image 1]"));
        let chip_row = rows
            .iter()
            .position(|(l, _)| l.to_string().contains("▣ Image 1"));
        assert!(title_row < chip_row, "chips sit below the message text");
        let inner = crate::copy_button::inner(op).unwrap();
        assert_eq!(inner["kind"], "context_chips");
        let ranges = inner["chips"].as_array().unwrap();
        assert_eq!(ranges.len(), 2);
        let start = ranges[0]["start"].as_u64().unwrap() as usize;
        let column = text.chars().take_while(|c| *c != '▣').count() - 1;
        assert_eq!(start, column, "range starts at the chip's first cell");
        assert_eq!(ranges[0]["operation"], image);
        assert_eq!(ranges[1]["operation"], page);
        assert_eq!(inner["default"], page);
    }

    #[test]
    fn user_card_clock_is_muted_after_quiet_number() {
        let p = Palette::new(false);
        let b = Content {
            kind: "user".into(),
            title: "hello".into(),
            number: 3,
            sent_at: "12:36 AM".into(),
            ..Default::default()
        };
        let rows = build(&b, 60, &p);
        let header = &rows[1].0;
        let text: String = header.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.contains("#3 · 12:36 AM"));
        assert_eq!(
            header
                .spans
                .iter()
                .find(|s| s.content.contains("12:36 AM"))
                .unwrap()
                .style
                .fg,
            Some(p.muted)
        );
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
        let rows = build(&block, 41, &p);
        assert!(rows.iter().all(|(line, _)| line.width() == 41));
        // One unstyled cell of right margin keeps the card off the column edge.
        assert!(rows.iter().all(|(line, _)| {
            let last = line.spans.last().unwrap();
            last.content == " " && last.style == Style::default()
        }));
        let header: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        // Composer-style card: chevron in the margin, panel padding, no coloured rail.
        assert!(header.starts_with("▾ ") && !header.contains('│'));
        let number = rows[1]
            .0
            .spans
            .iter()
            .find(|span| span.content.contains("#3"))
            .unwrap();
        assert_eq!(number.style.fg, Some(p.quiet));
        assert_eq!(
            number.style.bg,
            Some(p.element),
            "number blends into the card without a badge"
        );
        assert!(header.starts_with("▾    hello") && header.trim_end().ends_with("#3"));
        let all: String = rows
            .iter()
            .flat_map(|(l, _)| l.spans.iter())
            .map(|s| s.content.as_ref())
            .collect();
        assert!(all.contains("▣ Image 1") && !all.contains("Click an attachment"));
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
                assert_eq!(text.trim(), "", "padding rows are blank panel, no rail");
                let card = &line.spans[line.spans.len() - 2];
                assert_eq!(
                    card.style.bg,
                    Some(p.element),
                    "the card fills up to its margin"
                );
                assert_eq!(
                    crate::copy_button::inner(operation),
                    block.operation.as_ref()
                );
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
        // Opened: quiet header (no amber, no rail before it), reasoning behind a dim rail.
        assert_eq!(lines[0], "     ▾ Thought: 671ms");
        assert!(lines
            .iter()
            .any(|l| l.contains("│ Exploring setup") && !l.contains("**")));
        assert!(rows
            .iter()
            .flat_map(|(l, _)| &l.spans)
            .all(|s| s.style.fg != Some(p.warning)));
        assert_eq!(rows[0].0.spans.last().unwrap().style.fg, Some(p.quiet));
    }

    #[test]
    fn mermaid_image_rows_keep_copy_source_and_streaming_falls_back() {
        crate::mermaid_image::tests::wait_raster("mindmap\n  root((Root))\n    Child", 33);
        let source = "```mermaid\nmindmap\n  root((Root))\n    Child\n```";
        let mut block = Content {
            id: "chart".into(),
            kind: "markdown".into(),
            text: source.into(),
            operation: Some(json!({"action":"copy"})),
            ..Default::default()
        };
        let rows = build(&block, 40, &Palette::new(false));
        let image = rows
            .iter()
            .find_map(|(_, op)| op.as_ref().filter(|op| op.get("inline_image").is_some()))
            .unwrap();
        assert_eq!(image["fence"], 0);
        assert_eq!(
            crate::copy_button::text(&[block.clone()], "chart", Some(0)).unwrap(),
            "mindmap\n  root((Root))\n    Child"
        );
        assert!(rows.iter().all(|(line, _)| line.width() <= 40));
        block.text = "```mermaid\nmindmap\n  root((Root))".into();
        block.streaming = true;
        assert!(build(&block, 40, &Palette::new(false))
            .iter()
            .all(|(_, op)| op.as_ref().and_then(|op| op.get("inline_image")).is_none()));
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
    fn subagent_marks_and_durations_running_vs_done() {
        let p = Palette::new(false);
        let agent_col = color("#ff0000", p.blue, &p);

        // Running: spinner span has agent colour, time is live in p.muted
        let running_task = Content {
            kind: "task".into(),
            agent_name: "explorer".into(),
            task: "search".into(),
            model: "gpt-6".into(),
            status: "running".into(),
            color: "#ff0000".into(),
            started_ms: look::now_ms().saturating_sub(4500),
            ..Default::default()
        };
        let rows = build(&running_task, 80, &p);
        let line1 = &rows[0].0;
        let tint = nexus_widgets::theme::mix(agent_col, p.background, 0.5);
        // Pad, gutter, icon, space, name.
        assert_eq!(line1.spans[2].content, look::SLOT);
        assert_eq!(line1.spans[2].style.fg, Some(tint));
        let name_span = &line1.spans[4];
        assert_eq!(name_span.content, "Explorer");
        assert_eq!(name_span.style.fg, Some(agent_col));
        let line2 = &rows[1].0;
        let time_span = line2.spans.last().unwrap();
        assert!(time_span.content.ends_with('s'));
        assert_eq!(time_span.style.fg, Some(p.muted));

        // Done: full braille cell at the same tint, time is total in p.quiet
        let done_task = Content {
            kind: "task".into(),
            agent_name: "explorer".into(),
            task: "search".into(),
            model: "gpt-6".into(),
            status: "completed".into(),
            color: "#ff0000".into(),
            elapsed_ms: 3200,
            ..Default::default()
        };
        let rows = build(&done_task, 80, &p);
        let line1 = &rows[0].0;
        assert_eq!(line1.spans[2].content, "\u{28ff}");
        assert_eq!(line1.spans[2].style.fg, Some(tint));
        assert_eq!(line1.spans[4].content, "Explorer");
        let line2 = &rows[1].0;
        let time_span = line2.spans.last().unwrap();
        assert_eq!(time_span.content, "3.2s");
        assert_eq!(time_span.style.fg, Some(p.quiet));

        // Failed: the name turns p.error
        let failed_task = Content {
            kind: "task".into(),
            agent_name: "explorer".into(),
            status: "failed".into(),
            color: "#ff0000".into(),
            ..Default::default()
        };
        let rows = build(&failed_task, 80, &p);
        let name_span = &rows[0].0.spans[4];
        assert_eq!(name_span.content, "Explorer");
        assert_eq!(name_span.style.fg, Some(p.error));
        assert_eq!(rows[0].0.spans[2].content, "\u{28ff}");
        assert_eq!(
            rows[0].0.spans[2].style.fg,
            Some(nexus_widgets::theme::mix(p.error, p.background, 0.5))
        );
    }

    #[test]
    fn subagent_row_layout_two_lines_and_alignment() {
        let p = Palette::new(false);
        // Short, ten-character and long names are all shown whole, never padded or cut at ten.
        for (raw_name, shown) in [
            ("bob", "Bob"),
            ("researcher", "Researcher"),
            ("reallylongsubagentname", "Reallylongsubagentname"),
        ] {
            for status in ["running", "done", "failed"] {
                let task = Content {
                    kind: "task".into(),
                    agent_name: raw_name.into(),
                    task: "find all calls".into(),
                    status: status.into(),
                    color: "#ff0000".into(),
                    ..Default::default()
                };
                let rows = build(&task, 80, &p);
                let text: String = rows[0].0.spans.iter().map(|s| s.content.as_ref()).collect();
                // Transcript pad (1) + group gutter (4) + icon and a space: the name starts at
                // column 7, where a tool group's verb text starts after "▸ ".
                let icon = if status == "running" {
                    look::SLOT
                } else {
                    "\u{28ff}"
                };
                assert_eq!(text, format!("     {icon} {shown}  find all calls"));
                assert_eq!(
                    text.chars()
                        .take(7)
                        .map(|c| c.to_string().width())
                        .sum::<usize>(),
                    7
                );
                for glyph in ["\u{203a}", "\u{25c6}", ">", "*"] {
                    assert!(!text.contains(glyph), "no chevron or mark: {text:?}");
                }
                assert!(!text.contains('…'));
            }
        }
    }

    #[test]
    fn subagent_long_name_clips_to_the_terminal_width() {
        let p = Palette::new(false);
        let task = Content {
            kind: "task".into(),
            agent_name: "reallylongsubagentname".into(),
            task: "find all calls".into(),
            status: "running".into(),
            ..Default::default()
        };
        let rows = build(&task, 14, &p);
        let text: String = rows[0].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.width() <= 14, "{text:?}");
        assert_eq!(text, format!("     {} Really…", look::SLOT));
        assert!(text.ends_with('…'), "{text:?}");

        // Widening the terminal shows more characters up to the width rather than capping at 10.
        let rows = build(&task, 20, &p);
        let text: String = rows[0].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.width() <= 20, "{text:?}");
        assert_eq!(text, format!("     {} Reallylongsu…", look::SLOT));
        assert!(text.ends_with('…'), "{text:?}");
    }

    #[test]
    fn subagent_external_tool_count_sits_on_row_two_in_dark_gray() {
        let p = Palette::new(false);
        let task = Content {
            kind: "task".into(),
            agent_name: "claude".into(),
            task: "review".into(),
            elapsed_ms: 3200,
            status: "done".into(),
            external_tool_calls: Some(7),
            ..Default::default()
        };
        let rows = build(&task, 80, &p);
        assert_eq!(rows.len(), 2);
        let row1: String = rows[0].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(!row1.contains("external"));
        let row2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(row2, "       7 external tools · 3.2s");
        let count = rows[1]
            .0
            .spans
            .iter()
            .find(|s| s.content.contains("external"))
            .unwrap();
        let luma = |c: Option<Color>| match c {
            Some(Color::Rgb(r, g, b)) => u32::from(r) + u32::from(g) + u32::from(b),
            other => panic!("expected rgb, got {other:?}"),
        };
        // Darker than the other row-two text, on both themes.
        for light in [false, true] {
            let p = Palette::new(light);
            let rows = build(&task, 80, &p);
            let count = rows[1]
                .0
                .spans
                .iter()
                .find(|s| s.content.contains("external"))
                .unwrap();
            assert!(luma(count.style.fg) < luma(Some(p.quiet)));
        }
        assert_ne!(count.style.fg, Some(p.quiet));
        assert_ne!(count.style.fg, Some(p.muted));

        let one = Content {
            external_tool_calls: Some(1),
            ..task
        };
        let rows = build(&one, 80, &p);
        let row2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(row2.contains("1 external tool "), "{row2:?}");
    }

    #[test]
    fn subagent_line2_omits_tools_and_tokens_when_absent() {
        let p = Palette::new(false);
        let mut task = Content {
            kind: "task".into(),
            agent_name: "explorer".into(),
            task: "search".into(),
            model: "gpt-6-mini".into(),
            effort: "medium".into(),
            tier: "low".into(),
            elapsed_ms: 3200,
            status: "done".into(),
            color: "#ff0000".into(),
            ..Default::default()
        };
        // Absent tools and tokens
        let rows = build(&task, 80, &p);
        assert_eq!(rows.len(), 2);
        let line2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(line2, "       gpt-6-mini-medium (low) · 3.2s");
        assert!(!line2.contains("tool"));
        assert!(!line2.contains("token"));

        // With tools only
        task.tool_calls = Some(4);
        let rows = build(&task, 80, &p);
        let line2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(line2, "       gpt-6-mini-medium (low) · 4 tools · 3.2s");

        // With tokens only (10K tokens)
        task.tool_calls = None;
        task.tokens = 10_000.into();
        let rows = build(&task, 80, &p);
        let line2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(line2, "       gpt-6-mini-medium (low) · 10K tokens · 3.2s");

        // With both tools and tokens
        task.tool_calls = Some(4);
        let rows = build(&task, 80, &p);
        let line2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(
            line2,
            "       gpt-6-mini-medium (low) · 4 tools · 10K tokens · 3.2s"
        );
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
            Some(p.error),
            "a failed standalone tool row is red"
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
        // Grey chip, never the agent colour (spans: indent, chip, tokens).
        assert_eq!(rows[0].0.spans[1].style.bg, Some(p.element));
        assert!(rows
            .iter()
            .flat_map(|(line, _)| &line.spans)
            .all(|span| span.style.bg != Some(p.blue)));
        assert_eq!(rows.len(), 2);
    }

    #[test]
    fn standalone_edit_header_shows_coloured_line_counts() {
        let p = Palette::new(false);
        let edit = Content {
            kind: "change".into(),
            status: "completed".into(),
            text: "Edit src/main.rs".into(),
            path: "src/main.rs".into(),
            added: 12,
            removed: 3,
            files: 1,
            collapsed: true,
            ..Default::default()
        };
        let spans = &build(&edit, 60, &p)[0].0.spans;
        let text: String = spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.ends_with("Edit src/main.rs  +12 −3"), "{text}");
        assert!(spans
            .iter()
            .any(|s| s.content == "+12" && s.style.fg == Some(p.success)));
        assert!(spans
            .iter()
            .any(|s| s.content == "−3" && s.style.fg == Some(p.error)));
    }

    #[test]
    fn multi_file_change_header_shows_file_count_before_counts() {
        let p = Palette::new(false);
        let multi = Content {
            kind: "change".into(),
            status: "completed".into(),
            text: "Patch".into(),
            path: "a.rs, b.rs, c.rs".into(),
            added: 5,
            removed: 2,
            files: 3,
            collapsed: true,
            ..Default::default()
        };
        let spans = &build(&multi, 60, &p)[0].0.spans;
        let text: String = spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.ends_with("Patch  3 files +5 −2"), "{text}");
        assert!(spans
            .iter()
            .any(|s| s.content == "3 files" && s.style.fg == Some(p.muted)));
        let single = Content { files: 1, ..multi };
        let text: String = build(&single, 60, &p)[0]
            .0
            .spans
            .iter()
            .map(|s| s.content.as_ref())
            .collect();
        assert!(!text.contains("files") && text.ends_with("+5 −2"), "{text}");
    }

    #[test]
    fn open_change_draws_added_lines_green_and_removed_lines_red() {
        let p = Palette::new(false);
        let edit = Content {
            kind: "change".into(),
            status: "completed".into(),
            text: "Edit a.md".into(),
            added: 2,
            removed: 1,
            diff_lines: vec![
                ("ctx".into(), 1, "keep".into()),
                ("del".into(), 2, "old".into()),
                ("add".into(), 2, "new".into()),
                ("add".into(), 3, "more".into()),
            ],
            ..Default::default()
        };
        let rows = build(&edit, 40, &p);
        assert_eq!(rows.len(), 5);
        let find = |needle: &str| {
            rows.iter()
                .flat_map(|(line, _)| line.spans.iter())
                .find(|s| s.content == needle)
                .unwrap_or_else(|| panic!("{needle}"))
                .style
        };
        assert_eq!(find("new").fg, Some(p.success));
        assert_eq!(find("new").bg, Some(p.diff_add));
        assert_eq!(find("old").fg, Some(p.error));
        assert_eq!(find("old").bg, Some(p.diff_del));
        assert_eq!(find("keep").fg, Some(p.muted));
        for (line, _) in &rows {
            assert!(line.width() <= 40);
        }
        let folded = Content {
            collapsed: true,
            ..edit
        };
        assert_eq!(build(&folded, 40, &p).len(), 1);
    }

    #[test]
    fn failed_tool_groups_omit_counts_and_header_chips_own_click_ranges() {
        let p = Palette::new(false);
        let group = Content {
            kind: "tool_group".into(),
            status: "completed".into(),
            text: "Grep pattern=x".into(),
            count: 3,
            failures: 2,
            ..Default::default()
        };
        let rows = build(&group, 60, &p);
        let spans = &rows[0].0.spans;
        let text: String = spans.iter().map(|s| s.content.as_ref()).collect();
        // Failure details are not counted in the heading; the group disclosure stays subdued.
        assert!(!text.contains('✗') && !text.contains('✓'), "{text}");
        assert!(text.ends_with("Grep pattern=x"), "{text}");
        assert!(!text.contains("failed"), "{text}");
        assert_eq!(spans[1].style.fg, None);
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
        assert!(line(0).starts_with("     Tools [13]"), "{}", line(0));
        assert!(line(2).starts_with("     Skills [1 3]"), "{}", line(2));
        assert!(line(2).contains("Skills [1 3]"), "{}", line(2));
        assert!(rows[0].0.spans[1]
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

    fn plain(rows: &Rows) -> Vec<String> {
        rows.iter().map(|(line, _)| line.to_string()).collect()
    }

    #[test]
    fn live_card_draws_labelled_rows_and_wraps_long_text() {
        let p = Palette::new(false);
        let op = serde_json::json!({"kind":"live_toggle","id":"c1"});
        let block = Content {
            kind: "live".into(),
            title: "gpt-live-1-codex · sol".into(),
            status: "active".into(),
            collapsed: true,
            operation: Some(op.clone()),
            detail: "dim\tStarted\tinstructions: bundled\nyou\tYou\tcheck the readme\nsent\tSent to agent (queue)\tcheck the readme  → turn #3\ntold\tTold voice [final]\t".to_owned()
                + &"word ".repeat(40)
                + "\nbad\tEnded\tuser · 02:41",
            ..Default::default()
        };
        let rows = build(&block, 60, &p);
        let text = plain(&rows).join("\n");
        assert!(
            text.contains("› ")
                && text.contains(" Live ")
                && text.contains("gpt-live-1-codex · sol · active"),
            "{text}"
        );
        for label in [
            "Started",
            "You",
            "Sent to agent (queue)",
            "→ turn #3",
            "Told voice [final]",
            "Ended",
        ] {
            assert!(text.contains(label), "{label} missing in\n{text}");
        }
        assert!(plain(&rows)
            .iter()
            .all(|line| unicode_width::UnicodeWidthStr::width(line.as_str()) <= 60));
        assert!(
            rows.iter().all(|(_, row_op)| row_op.as_ref() == Some(&op)),
            "every row toggles the card"
        );
        assert!(
            plain(&rows).iter().filter(|l| l.contains("word")).count() > 1,
            "long text wraps"
        );
    }

    #[test]
    fn user_card_says_via_voice_beside_its_number() {
        let p = Palette::new(false);
        let mut block = Content {
            kind: "user".into(),
            title: "check the readme".into(),
            number: 3,
            ..Default::default()
        };
        assert!(!plain(&build(&block, 60, &p))
            .join("\n")
            .contains("via voice"));
        block.status = "live".into();
        let text = plain(&build(&block, 60, &p)).join("\n");
        assert!(text.contains("via voice · #3"), "{text}");
    }

    #[test]
    fn user_card_labels_a_subagent_report_and_a_root_follow_up() {
        let p = Palette::new(false);
        let mut block = Content {
            kind: "user".into(),
            title: "report · build-1 · explorer · run 2 · completed".into(),
            status: "report".into(),
            ..Default::default()
        };
        let text = plain(&build(&block, 80, &p)).join("\n");
        assert!(
            text.contains("report · build-1 · explorer · run 2 · completed"),
            "{text}"
        );
        assert!(text.contains("subagent report"), "{text}");
        assert!(!text.contains('#'), "a report takes no turn number: {text}");
        block.status = "followup".into();
        block.title = "follow-up from root · build-1 · run 2".into();
        let text = plain(&build(&block, 80, &p)).join("\n");
        assert!(text.contains("from root"), "{text}");
    }

    #[test]
    fn agent_report_is_compact_names_the_agent_and_announces_the_clip() {
        let p = Palette::new(false);
        let op = serde_json::json!({"kind":"message_page","id":"m"});
        let mut block = Content {
            kind: "agent_report".into(),
            agent_name: "explorer".into(),
            task: "find the loader".into(),
            status: "completed".into(),
            text: "first line\nsecond line".into(),
            count: 5,
            operation: Some(op.clone()),
            ..Default::default()
        };
        let rows = build(&block, 60, &p);
        let text = plain(&rows);
        assert_eq!(
            rows.len(),
            4,
            "heading, two preview rows, clip hint: {text:?}"
        );
        assert!(
            text[0].contains("Explorer  find the loader · completed"),
            "{text:?}"
        );
        assert!(text[1].contains("first line") && text[2].contains("second line"));
        assert!(text[3].contains("5 more lines"), "{text:?}");
        assert!(!text.join("\n").contains('#'), "no turn number");
        assert!(
            rows.iter().all(|(_, o)| o.as_ref() == Some(&op)),
            "every row opens the report"
        );
        // `build` adds one outer pad column before the icon.
        assert!(text[0].starts_with("   ⮑ Explorer"), "{text:?}");
        let name_col = text[0][..text[0].find("Explorer").unwrap()].width();
        for row in &text[1..] {
            let content = row.trim_start();
            assert_eq!(
                row.width() - content.width(),
                name_col,
                "aligned under the name: {text:?}"
            );
        }
        let span = rows[0]
            .0
            .spans
            .iter()
            .find(|s| s.content == "Explorer")
            .unwrap();
        assert_eq!(
            span.style.fg,
            Some(p.blue),
            "unset colour falls back like a task row"
        );
        block.color = "$nx-purple".into();
        let rows = build(&block, 60, &p);
        let span = rows[0]
            .0
            .spans
            .iter()
            .find(|s| s.content == "Explorer")
            .unwrap();
        assert_eq!(span.style.fg, Some(p.purple), "agent colour");
        block.text = "   padded first\n\n      padded second".into();
        let text = plain(&build(&block, 60, &p));
        assert!(text[1].starts_with("     padded first"), "{text:?}");
        assert!(text[2].starts_with("     padded second"), "{text:?}");
        block.count = 0;
        block.text = "only".into();
        assert_eq!(build(&block, 60, &p).len(), 2, "nothing clipped, no hint");
        block.text = "x".repeat(200);
        let text = plain(&build(&block, 40, &p));
        assert!(
            text[1].ends_with('…') && text[2].contains("clipped"),
            "{text:?}"
        );
    }

    #[test]
    fn task_card_names_the_state_of_an_idle_or_stopped_child() {
        let p = Palette::new(false);
        let mut task = Content {
            kind: "task".into(),
            agent_name: "explorer".into(),
            task: "search".into(),
            model: "gpt-6-mini".into(),
            status: "done".into(),
            state: "idle · run 2".into(),
            ..Default::default()
        };
        let rows = build(&task, 80, &p);
        let line2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(line2.contains("idle · run 2 · gpt-6-mini"), "{line2}");
        task.state = "stopped by system: evicted".into();
        let rows = build(&task, 80, &p);
        let line2: String = rows[1].0.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(line2.contains("stopped by system: evicted"), "{line2}");
    }
}
