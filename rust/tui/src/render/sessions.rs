//! Compact, centered Sessions page (a bordered, Settings-like card, at most `PAGE_WIDTH`
//! wide and as tall as its content). Rows are grouped by last-updated time (Today,
//! Yesterday, This week, Earlier), one line each: the status glyph, the name, and the
//! last-used time. A `/ Search sessions` row sits
//! above the first group. Nothing expands inline and there is no preview pane or composer.
const PAGE_WIDTH: u16 = 72;

use crate::picker::{clock, pending, section, text, waited, Dashboard};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Clear, Paragraph},
    Frame,
};
use serde_json::Value;
use unicode_width::UnicodeWidthStr;

pub struct Colors {
    pub text: Color,
    pub muted: Color,
    pub quiet: Color,
    pub accent: Color,
    /// Selected row background (`element`).
    pub selected: Color,
    /// Hovered row background (`element_hi`).
    pub hover: Color,
    pub border: Color,
    pub border_strong: Color,
    pub purple: Color,
    pub warning: Color,
    /// Monochrome drops every colour, so the selected row is also marked by a leading bar.
    pub mono: bool,
}

impl Default for Colors {
    fn default() -> Self {
        Self {
            text: Color::Reset,
            muted: Color::Gray,
            quiet: Color::DarkGray,
            accent: Color::Cyan,
            selected: Color::DarkGray,
            hover: Color::DarkGray,
            border: Color::DarkGray,
            border_strong: Color::Gray,
            purple: Color::Magenta,
            warning: Color::Yellow,
            mono: false,
        }
    }
}

fn fit(value: &str, width: usize) -> String {
    if value.width() <= width {
        return value.to_owned();
    }
    let mut out = String::new();
    for c in value.chars() {
        if out.width() + 1 >= width {
            break;
        }
        out.push(c);
    }
    if width > 0 {
        out.push('…');
    }
    out
}

fn spans_width(spans: &[Span]) -> usize {
    spans.iter().map(|span| span.content.width()).sum()
}

fn rows_of(dashboard: &Value) -> &[Value] {
    dashboard
        .get("rows")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

fn age_of(row: &Value) -> &str {
    let age = text(row, "age");
    if age.is_empty() {
        text(row, "sub")
    } else {
        age
    }
}

/// Replaces the leading inset column with the focus bar (`>` in ASCII): the selection survives
/// Monochrome, where the row background is dropped.
fn mark_selected(line: &mut Line<'static>) {
    if let Some(first) = line.spans.first_mut() {
        *first = Span::styled(
            nexus_widgets::Glyphs::from_env().focus_bar,
            Style::default().add_modifier(Modifier::BOLD),
        );
    }
}

/// One band line: a one-column inset on both sides, `bg` filling between when given.
fn band(spans: Vec<Span<'static>>, width: usize, bg: Option<Color>) -> Line<'static> {
    let inner = width.saturating_sub(2);
    let used = spans_width(&spans);
    let mut out = vec![Span::raw(" ")];
    let style = |s: Span<'static>| match bg {
        Some(bg) => Span::styled(s.content, s.style.bg(bg)),
        None => s,
    };
    out.extend(spans.into_iter().map(style));
    if inner > used {
        out.push(style(Span::raw(" ".repeat(inner - used))));
    }
    out.push(Span::raw(" "));
    Line::from(out)
}

const SPIN: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/// The status marks: `(needs you, working frame, idle, archived)`; ASCII under `NEXUS_ASCII=1`.
fn marks(spin: usize) -> (&'static str, &'static str, &'static str, &'static str) {
    marks_for(nexus_widgets::Glyphs::from_env().ascii, spin)
}

fn marks_for(
    ascii: bool,
    spin: usize,
) -> (&'static str, &'static str, &'static str, &'static str) {
    if ascii {
        let frames = nexus_widgets::Glyphs::ascii().spinner;
        ("!", frames[spin % frames.len()], "o", "*")
    } else {
        ("◆", SPIN[spin % SPIN.len()], "◇", "●")
    }
}

fn status_glyph(status: &str, colors: &Colors, spin: usize) -> Span<'static> {
    let (input, working, idle, done) = marks(spin);
    match status {
        "working" => Span::styled(working, Style::default().fg(colors.purple)),
        "input" => Span::styled(input, Style::default().fg(colors.warning)),
        "done" => Span::styled(done, Style::default().fg(colors.muted)),
        _ => Span::styled(idle, Style::default().fg(colors.quiet)),
    }
}

/// `◆ N needs you | ⠋ N working | ◇ N idle | ● N archived`, in the dashboard's section order.
fn status_chips(
    shown: &[&Value],
    colors: &Colors,
    width: usize,
    spin: usize,
) -> Vec<Span<'static>> {
    let mut counts = [0usize; 4];
    for row in shown {
        counts[usize::from(section(row))] += 1;
    }
    let (input, working, idle, done) = marks(spin);
    let items = [
        (counts[0], input, "needs you", colors.warning),
        (counts[1], working, "working", colors.purple),
        (counts[2], idle, "idle", colors.quiet),
        (counts[3], done, "archived", colors.muted),
    ];
    let present: Vec<_> = items.into_iter().filter(|(count, ..)| *count > 0).collect();
    if present.is_empty() || width == 0 {
        return Vec::new();
    }
    let build = |labels: bool| -> Vec<Span<'static>> {
        let mut spans = Vec::new();
        for (index, (count, glyph, label, color)) in present.iter().enumerate() {
            if index > 0 {
                spans.push(Span::styled(
                    " │ ",
                    Style::default().fg(colors.border_strong),
                ));
            }
            let body = if labels {
                format!("{glyph} {count} {label}")
            } else {
                format!("{glyph} {count}")
            };
            spans.push(Span::styled(body, Style::default().fg(*color)));
        }
        spans
    };
    let full = build(true);
    if spans_width(&full) <= width {
        full
    } else {
        build(false)
    }
}

type RowLine = (Line<'static>, Option<usize>, Option<String>);

/// Word-wrap `text` to at most `max` rows of `width` columns; a cut is announced with `…`.
fn wrap(text: &str, width: usize, max: usize) -> Vec<String> {
    let width = width.max(1);
    let mut rows: Vec<String> = Vec::new();
    let mut row = String::new();
    for word in text.split_whitespace() {
        let word = if word.width() > width {
            fit(word, width)
        } else {
            word.to_owned()
        };
        if !row.is_empty() && row.width() + 1 + word.width() > width {
            rows.push(std::mem::take(&mut row));
        }
        if !row.is_empty() {
            row.push(' ');
        }
        row.push_str(&word);
    }
    if !row.is_empty() {
        rows.push(row);
    }
    if rows.len() > max {
        rows.truncate(max);
        if let Some(last) = rows.last_mut() {
            let marked = format!("{last} …");
            *last = fit(&marked, width);
        }
    }
    rows
}

/// The pinned Manager entry above the roots (docs/manager.md): title row with the `Voice M`
/// button, the one-line status (off / not signed in / ready), the pinned model with Live
/// readiness, the cross-session privacy line, then a closing rule. Empty when the host sent no
/// entry. Returns the rows and the `(column, width)` of the `Voice M` button within them.
fn manager_band(
    dashboard: &Value,
    colors: &Colors,
    width: usize,
    spin: usize,
) -> (Vec<Line<'static>>, Option<(usize, usize)>) {
    if !crate::picker::has_manager(dashboard) {
        return (Vec::new(), None);
    }
    let Some(entry) = dashboard
        .get("manager")
        .and_then(|value| serde_json::from_value::<crate::bridge::ManagerEntry>(value.clone()).ok())
    else {
        return (Vec::new(), None);
    };
    let quiet = Style::default().fg(colors.quiet);
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut title = vec![
        status_glyph(&entry.status, colors, spin),
        Span::raw(" "),
        Span::styled(
            "Manager",
            Style::default()
                .fg(colors.text)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            if entry.active {
                "  pinned · open now"
            } else {
                "  pinned · m opens"
            },
            quiet,
        ),
    ];
    let mut voice = None;
    let label = "Voice M";
    let used = spans_width(&title);
    if entry.enabled && width > used + label.width() + 1 {
        title.push(Span::raw(" ".repeat(width - used - label.width())));
        title.push(Span::styled(label, Style::default().fg(colors.purple)));
        voice = Some((width - label.width(), label.width()));
    }
    lines.push(Line::from(title));
    let tone = match entry.tone.as_str() {
        "warn" => colors.warning,
        "off" => colors.quiet,
        _ => colors.muted,
    };
    for row in wrap(&entry.summary, width.saturating_sub(2), 2) {
        lines.push(Line::from(vec![
            Span::raw("  "),
            Span::styled(row, Style::default().fg(tone)),
        ]));
    }
    let detail = if entry.enabled {
        let mut parts = vec![entry.model.clone()];
        if !entry.live.is_empty() {
            parts.push(entry.live.clone());
        }
        parts.join(" · ")
    } else {
        entry.notes.first().cloned().unwrap_or_default()
    };
    let mut rest = vec![detail];
    if entry.enabled && !entry.privacy.is_empty() {
        rest.push(entry.privacy.clone());
    }
    for text in rest {
        for row in wrap(&text, width.saturating_sub(2), 2) {
            lines.push(Line::from(vec![Span::raw("  "), Span::styled(row, quiet)]));
        }
    }
    lines.push(Line::from(Span::styled(
        "─".repeat(width),
        Style::default().fg(colors.border),
    )));
    (lines, voice)
}

pub fn draw(
    frame: &mut Frame,
    area: Rect,
    dashboard: &Value,
    state: &mut Dashboard,
    colors: &Colors,
    spin: usize,
) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64());
    draw_at(frame, area, dashboard, state, colors, spin, now);
}

/// `draw` with the wall clock injected (unix seconds), for the "waiting mm:ss" on Needs you rows.
pub fn draw_at(
    frame: &mut Frame,
    area: Rect,
    dashboard: &Value,
    state: &mut Dashboard,
    colors: &Colors,
    spin: usize,
    now: f64,
) {
    let (bg, fg) = if area.is_empty() {
        (Color::Reset, Color::Reset)
    } else {
        let cell = &frame.buffer_mut()[(area.x, area.y)];
        (cell.bg, cell.fg)
    };
    frame.render_widget(Clear, area);
    frame
        .buffer_mut()
        .set_style(area, Style::default().bg(bg).fg(fg));
    state.list_area = Rect::default();
    state.line_rows.clear();
    state.line_headings.clear();
    state.new_hit = Rect::default();
    state.worktree_hit = Rect::default();
    state.choose_hit = Rect::default();
    state.manager_hit = Rect::default();
    state.manager_live_hit = Rect::default();
    if area.height < 4 || area.width < 12 {
        return;
    }
    state.sync(dashboard);
    // A compact centered page, like a Settings page: the screen shows around it.
    let screen = area;
    let page_width = screen.width.min(PAGE_WIDTH).max(12);
    let all = rows_of(dashboard);
    let shown = state.filtered(all);
    let quiet = Style::default().fg(colors.quiet);
    let muted = Style::default().fg(colors.muted);
    // Inside the border; the header keeps two-column gutters, rows use `list_width`.
    let list_width = usize::from(page_width.saturating_sub(2));
    let width = list_width.saturating_sub(4);

    let chips = status_chips(&shown, colors, width / 2, spin);
    let chips_w = spans_width(&chips);
    let hint = "[t Tree]";
    let branch = text(dashboard, "branch");
    let path = text(dashboard, "path");
    let worktree = text(dashboard, "worktree");
    // Path first (text colour), then branch and worktree (quiet), as in the session top bar.
    let mut location = String::from(path);
    for part in [branch, worktree] {
        if !part.is_empty() {
            if !location.is_empty() {
                location.push(' ');
            }
            location.push_str(part);
        }
    }
    if state.this_project {
        location.push_str(" · this project");
    }
    if dashboard.get("truncated").and_then(Value::as_bool) == Some(true) {
        location.push_str(" · truncated");
    }
    let gap = usize::from(!chips.is_empty());
    let left_budget = width.saturating_sub(chips_w + gap);
    let show_hint = left_budget > hint.width() + 8;
    let text_budget = if show_hint {
        left_budget.saturating_sub(hint.width() + 1)
    } else {
        left_budget
    };
    let location = fit(&location, text_budget.max(1));
    let path_len = location
        .char_indices()
        .nth(path.chars().count())
        .map_or(location.len(), |(i, _)| i);
    let (head, tail) = location.split_at(path_len);
    let mut header = vec![Span::styled(
        head.to_owned(),
        Style::default().fg(colors.text),
    )];
    if !tail.is_empty() {
        header.push(Span::styled(tail.to_owned(), quiet));
    }
    if show_hint {
        header.push(Span::raw(" "));
        header.push(Span::styled(hint, quiet));
    }
    let used = spans_width(&header) + gap + chips_w;
    if width > used {
        header.push(Span::raw(" ".repeat(width - used)));
    }
    header.extend(chips);

    let show_work = width > 48;
    let work_key = show_work; // `Worktree Ctrl+w`: the shortcut is always shown with the button
    let mut right: Vec<Span> = Vec::new();
    if show_work {
        right.push(Span::styled("Worktree", Style::default().fg(colors.purple)));
        if work_key {
            right.push(Span::styled(" Ctrl+w", quiet));
        }
    }
    let new_label = "+ New Agent";
    let right_w = spans_width(&right);
    let show_new = new_label.width() + 2 + right_w <= width || right.is_empty();
    let mut actions: Vec<Span> = Vec::new();
    if show_new {
        actions.push(Span::styled(
            new_label,
            Style::default()
                .fg(colors.purple)
                .add_modifier(Modifier::BOLD),
        ));
    }
    let actions_w = spans_width(&actions);
    if right_w > 0 && actions_w + right_w < width {
        actions.push(Span::raw(" ".repeat(width - actions_w - right_w)));
        actions.extend(right);
    } else if right_w > 0 && actions.is_empty() {
        actions = right;
    }

    let selected_title = shown
        .iter()
        .find(|row| text(row, "id") == state.selected_id)
        .map_or("", |row| text(row, "title"));
    let footer = if let Some(draft) = &state.rename {
        Some(Line::from(vec![
            Span::styled("Rename: ", Style::default().fg(colors.accent)),
            Span::raw(draft.clone()),
            Span::styled("▏  Enter save · Esc cancel", quiet),
        ]))
    } else if state.confirm_archive.is_some() {
        Some(Line::from(Span::styled(
            format!(
                "Archive “{}”?  y or Enter confirms · any other key cancels",
                fit(selected_title, 40)
            ),
            Style::default().fg(colors.accent),
        )))
    } else if !state.notice.is_empty() {
        Some(Line::from(Span::styled(
            state.notice.clone(),
            Style::default().fg(colors.accent),
        )))
    } else if let Some(error) = dashboard
        .get("error")
        .and_then(Value::as_str)
        .filter(|e| !e.is_empty())
    {
        Some(Line::from(Span::styled(format!("Error: {error}"), quiet)))
    } else {
        None
    };

    // One line per session: status glyph, name, last-used time. Groups keep a heading.
    let mut lines: Vec<RowLine> = Vec::new();
    let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
    for (index, row) in shown.iter().enumerate() {
        let key = state.group_key(row);
        if groups.last().is_none_or(|(name, _)| name != &key) {
            groups.push((key, Vec::new()));
        }
        groups.last_mut().unwrap().1.push(index);
    }
    for (key, members) in &groups {
        let collapsed = state.is_collapsed(key);
        let glyphs = nexus_widgets::Glyphs::from_env();
        let chevron = if collapsed { glyphs.closed } else { glyphs.open };
        let label = format!("{chevron} {key}");
        let count = format!(" {}", members.len());
        lines.push((
            Line::from(vec![
                Span::raw("  "),
                Span::styled(label, muted),
                Span::styled(count, quiet),
            ]),
            None,
            Some(key.clone()),
        ));
        if collapsed {
            continue;
        }
        for index in members {
            let row = shown[*index];
            let id = text(row, "id");
            let is_selected = id == state.selected_id;
            let is_hover = !is_selected && id == state.hovered_id;
            let renaming = is_selected.then(|| state.rename.clone()).flatten();
            let bg = if is_selected {
                Some(colors.selected)
            } else if is_hover {
                Some(colors.hover)
            } else {
                None
            };
            let (mut title, _close) = title_spans(
                row,
                list_width,
                is_selected,
                renaming.as_deref(),
                colors,
                spin,
            );
            // `title_spans` already leads with the status glyph: only indent it here.
            title.insert(0, Span::raw("  "));
            let mut line = band(title, list_width, bg);
            if colors.mono && is_selected {
                mark_selected(&mut line);
            }
            lines.push((line, Some(*index), None));
            if let Some(request) = pending(row).filter(|_| section(row) == 0) {
                // Needs you: the question with its wait, then the keys that answer it.
                let inner = list_width.saturating_sub(2);
                let wait = format!(
                    "waiting {}",
                    clock(waited(
                        request.get("ts").and_then(Value::as_f64).unwrap_or(0.0),
                        now
                    ))
                );
                let room = inner.saturating_sub(4 + 1 + wait.width() + 2);
                let ask = fit(text(request, "summary"), room.max(1));
                let gap = inner.saturating_sub(4 + ask.width() + wait.width() + 2);
                lines.push((
                    band(
                        vec![
                            Span::raw("    "),
                            Span::styled(ask, Style::default().fg(colors.text)),
                            Span::raw(" ".repeat(gap.max(1))),
                            Span::styled(wait, quiet),
                        ],
                        list_width,
                        bg,
                    ),
                    Some(*index),
                    None,
                ));
                lines.push((
                    band(
                        vec![
                            Span::raw("    "),
                            Span::styled("Enter open  a allow once  d deny", quiet),
                        ],
                        list_width,
                        bg,
                    ),
                    Some(*index),
                    None,
                ));
            }
        }
    }
    if lines.is_empty() {
        let empty = if state.query.is_empty() {
            "No sessions yet · n starts one"
        } else {
            "No session matches the filter"
        };
        lines.push((
            Line::from(vec![Span::raw("  "), Span::styled(empty, muted)]),
            None,
            None,
        ));
    }
    // The pinned Manager sits between the search rule and the roots, with a rule of its own, so
    // it is never mistaken for (or sorted among) an ordinary session.
    let (manager_lines, manager_voice) = manager_band(dashboard, colors, width, spin);
    let manager_h = manager_lines.len() as u16;
    // Size the page to its content (header bands, list, footer, border), centered on screen
    // with at least one blank row above and below when the screen is tall enough.
    let chrome = 6 + manager_h + u16::from(footer.is_some()) + 2;
    let margin = if screen.height > chrome + 2 { 1 } else { 0 };
    let page_height = ((lines.len() + 1).min(usize::from(u16::MAX)) as u16)
        .saturating_add(chrome)
        .min(screen.height.saturating_sub(2 * margin))
        .max(chrome.min(screen.height));
    let page = Rect {
        x: screen.x + (screen.width - page_width) / 2,
        y: screen.y + (screen.height - page_height) / 2,
        width: page_width,
        height: page_height,
    };
    frame.render_widget(
        Block::bordered()
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(colors.border_strong)),
        page,
    );
    let inner = Rect {
        x: page.x + 1,
        y: page.y + 1,
        width: page.width.saturating_sub(2),
        height: page.height.saturating_sub(2),
    };
    let area = Rect {
        x: inner.x + 2,
        width: inner.width.saturating_sub(4),
        height: inner.height,
        ..inner
    };

    // header, blank, New Agent, blank, search, rule, list.
    let mut constraints = vec![
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ];
    if manager_h > 0 {
        constraints.push(Constraint::Length(manager_h));
    }
    constraints.push(Constraint::Min(1));
    if footer.is_some() {
        constraints.push(Constraint::Length(1));
    }
    let bands = Layout::default()
        .direction(Direction::Vertical)
        .constraints(constraints)
        .split(area);
    let list_band = 6 + usize::from(manager_h > 0);
    if manager_h > 0 {
        let band = bands[6];
        frame.render_widget(Paragraph::new(manager_lines), band);
        // Every row but the closing rule opens the Manager; `Voice M` starts a call there.
        state.manager_hit = Rect {
            height: band.height.min(manager_h.saturating_sub(1)),
            ..band
        };
        if let Some((offset, voice_width)) = manager_voice {
            state.manager_live_hit = Rect {
                x: band.x + offset as u16,
                y: band.y,
                width: voice_width as u16,
                height: band.height.min(1),
            };
        }
    }
    frame.render_widget(Paragraph::new(Line::from(header)), bands[0]);
    frame.render_widget(Paragraph::new(Line::from(actions)), bands[2]);
    if show_hint {
        let x = area.x + location.width() as u16 + 1;
        state.choose_hit = Rect {
            x,
            y: bands[0].y,
            width: hint.width() as u16,
            height: 1,
        };
    }
    if show_new {
        state.new_hit = Rect {
            x: area.x,
            y: bands[2].y,
            width: new_label.width() as u16,
            height: 1,
        };
    }
    if right_w > 0 && (show_new && actions_w + right_w < width || !show_new) {
        let start = area.x + area.width - right_w as u16;
        if show_work {
            let work_x = start;
            let work_w = "Worktree".width() + usize::from(work_key) * " Ctrl+w".width();
            state.worktree_hit = Rect {
                x: work_x,
                y: bands[2].y,
                width: work_w as u16,
                height: 1,
            };
        }
    }

    // Search row and its rule, like the Settings search.
    let search = if state.searching {
        Line::from(vec![
            Span::styled("/ ", Style::default().fg(colors.accent)),
            Span::raw(state.query.clone()),
            Span::styled("▏", Style::default().fg(colors.accent)),
        ])
    } else if !state.query.is_empty() {
        Line::from(vec![
            Span::styled("/ ", quiet),
            Span::raw(state.query.clone()),
            Span::styled(format!("  ({} match) · Esc clears", shown.len()), quiet),
        ])
    } else {
        Line::from(Span::styled("/ Search sessions", quiet))
    };
    frame.render_widget(Paragraph::new(search), bands[4]);
    state.search_hit = bands[4];
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            "─".repeat(usize::from(area.width)),
            Style::default().fg(colors.border),
        ))),
        bands[5],
    );

    let list = Rect {
        x: inner.x,
        width: inner.width,
        ..bands[list_band]
    };
    let height = usize::from(list.height).max(1);
    let selected_line = lines
        .iter()
        .position(|(_, index, _)| {
            index.is_some_and(|i| {
                shown
                    .get(i)
                    .is_some_and(|row| text(row, "id") == state.selected_id)
            })
        })
        .unwrap_or(0);
    let mut selected_end = selected_line;
    if lines
        .get(selected_line)
        .and_then(|(_, index, _)| *index)
        .is_some()
    {
        while selected_end + 1 < lines.len() && lines[selected_end + 1].1 == lines[selected_line].1
        {
            selected_end += 1;
        }
    }
    let wanted = if selected_line > 0 && lines[selected_line - 1].2.is_some() {
        selected_line - 1
    } else {
        selected_line
    };
    if wanted < state.list_top {
        state.list_top = wanted;
    } else if selected_end >= state.list_top + height {
        state.list_top = selected_end + 1 - height;
    }
    state.list_top = state.list_top.min(lines.len().saturating_sub(height));
    let top = state.list_top;
    let visible: Vec<_> = lines.into_iter().skip(top).take(height).collect();
    state.line_rows = visible.iter().map(|(_, index, _)| *index).collect();
    state.line_headings = visible
        .iter()
        .map(|(_, _, heading)| heading.clone())
        .collect();
    state.list_area = list;
    state.archive_hit = Rect::default();
    frame.render_widget(
        Paragraph::new(
            visible
                .into_iter()
                .map(|(line, _, _)| line)
                .collect::<Vec<_>>(),
        ),
        list,
    );
    if let Some(line) = footer {
        frame.render_widget(Paragraph::new(line), bands[list_band + 1]);
    }
}

/// One-line session row: status, title, and last-used time. Archive actions remain available
/// through the existing keyboard command and selected-row controls outside this compact row.
fn title_spans(
    row: &Value,
    width: usize,
    selected: bool,
    renaming: Option<&str>,
    colors: &Colors,
    spin: usize,
) -> (Vec<Span<'static>>, Option<(usize, usize)>) {
    let quiet = Style::default().fg(colors.quiet);
    let children = row
        .get("running_children")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let handoffs = row
        .get("handoff_count")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let mut tail = age_of(row).to_owned();
    if children > 0 {
        tail = format!("{children} running · {tail}");
    }
    if handoffs > 0 {
        tail = format!("{handoffs} handoffs · {tail}");
    }
    let title = match renaming {
        Some(draft) => format!("{draft}▏"),
        None => {
            let name = text(row, "name");
            if name.is_empty() { text(row, "title").to_owned() }
            else { format!("{} · {}", text(row, "title"), name) }
        },
    };
    // Inset (1+1), indent (2), glyph and space (2), a gap, and two trailing columns.
    let inner = width.saturating_sub(2);
    let room = inner.saturating_sub(2 + 2 + 1 + 2 + tail.width());
    let title = fit(&title, room.max(1));
    let close = None;
    let spans = vec![
        status_glyph(text(row, "status"), colors, spin),
        Span::raw(" "),
        Span::styled(
            title,
            Style::default().fg(if section(row) == 3 {
                colors.muted
            } else {
                colors.text
            }),
        ),
        Span::raw(" "),
        Span::styled(tail, quiet),
    ];
    let _ = (selected, quiet);
    (spans, close)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn render(width: u16, height: u16, dashboard: &Value, state: &mut Dashboard) -> String {
        let backend = ratatui::backend::TestBackend::new(width, height);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw(frame, frame.area(), dashboard, state, &Colors::default(), 0))
            .unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol().to_owned())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn sample() -> Value {
        json!({
            "selected": "a",
            "path": "~/repos/nexus",
            "branch": "main",
            "workspace": "/w",
            "rows": [
                {"id":"a","title":"Alpha","status":"idle","bucket":"Today","updated":9.0,"project":"nexus","branch":"main","age":"2m",
                 "last_user":"What should we work on?","last_agent":"Python sample: frozen Task"},
                {"id":"b","title":"Beta","status":"input","bucket":"Yesterday","updated":5.0,"project":"nexus","branch":"feat/ui","age":"now",
                 "worktree":"feat/ui","last_user":"Need a decision","last_agent":"Which option?"},
            ],
        })
    }

    #[test]
    fn draws_without_rows() {
        let screen = render(
            90,
            25,
            &json!({"rows":[],"path":"~/repos/nexus","branch":"main"}),
            &mut Dashboard::default(),
        );
        assert!(screen.contains("+ New Agent"));
        assert!(screen.contains("No sessions yet"));
    }

    #[test]
    fn wide_layout_is_a_full_width_list() {
        let mut state = Dashboard::default();
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("Alpha") && screen.contains("Beta"));
        assert!(screen.contains("/ Search sessions"));
        assert!(
            !screen.contains("Python sample: frozen Task"),
            "rows are one line: no summary"
        );
        assert!(!screen.contains("Response"), "no preview pane");
        assert!(screen.contains("+ New Agent") && screen.contains("Worktree Ctrl+w"));
        assert!(
            !screen.contains("Open Previous") && !screen.contains("/resume"),
            "Open Previous is gone: /resume stays a chat command"
        );
        assert!(screen.contains("main") && screen.contains("~/repos/nexus"));
        assert!(state.list_area.width >= 56);
        assert!(state.new_hit.width > 0 && state.worktree_hit.width > 0);
    }

    #[test]
    fn page_is_compact_and_centered() {
        let mut state = Dashboard::default();
        let screen = render(140, 40, &sample(), &mut state);
        let list = state.list_area;
        assert!(list.width <= PAGE_WIDTH, "not full width: {}", list.width);
        let left = list.x;
        let right = 140 - (list.x + list.width);
        assert_eq!(left, right, "horizontally centered");
        assert!(state.new_hit.x > 30 && state.search_hit.x > 30);
        let lines: Vec<&str> = screen.lines().collect();
        assert!(lines[0].trim().is_empty(), "not full-screen: top row empty");
        assert!(
            lines[39].trim().is_empty(),
            "not full-screen: bottom row empty"
        );
        assert!(
            screen.contains('╭') && screen.contains('╯'),
            "bordered card"
        );
    }

    #[test]
    fn each_session_is_one_line_with_status_icon_name_and_age() {
        let mut state = Dashboard::default();
        let screen = render(140, 30, &sample(), &mut state);
        let beta = screen.lines().find(|l| l.contains("Beta")).unwrap();
        assert!(beta.contains("◆ Beta") && beta.contains("now"), "{beta}");
        assert!(!beta.contains("feat/ui"), "no branch on the row: {beta}");
        let alpha = screen.lines().find(|l| l.contains("Alpha")).unwrap();
        assert!(alpha.contains("◇ Alpha") && alpha.contains("2m"), "{alpha}");
        assert_eq!(alpha.matches('◇').count(), 1, "one status glyph per row: {alpha}");
        assert_eq!(beta.matches('◆').count(), 1, "one status glyph per row: {beta}");
    }

    #[test]
    fn narrow_layout_has_list_only() {
        let mut state = Dashboard::default();
        let screen = render(50, 20, &sample(), &mut state);
        assert!(screen.contains("Alpha"));
        assert!(!screen.contains("Agent build"));
    }

    #[test]
    fn rows_map_to_click_targets() {
        let mut state = Dashboard::default();
        render(140, 30, &sample(), &mut state);
        // Each session row maps directly to its displayed row.
        assert_eq!(
            state
                .line_rows
                .iter()
                .flatten()
                .copied()
                .collect::<Vec<_>>(),
            [0, 1]
        );
        // Lines: "Needs you" heading, Beta, "Idle" heading, Alpha.
        state.click(state.list_area.x, state.list_area.y + 2, &sample());
        assert_eq!(state.selected_id, "a", "a heading click selects nothing");
        state.click(state.list_area.x, state.list_area.y + 1, &sample());
        assert_eq!(state.selected_id, "b");
        state.click(state.list_area.x, state.list_area.y + 3, &sample());
        assert_eq!(state.selected_id, "a");
    }

    fn lines_of(screen: &str) -> Vec<&str> {
        screen.lines().collect()
    }

    fn at(lines: &[&str], needle: &str) -> usize {
        lines
            .iter()
            .position(|l| l.contains(needle))
            .unwrap_or_else(|| panic!("{needle}"))
    }

    fn mixed() -> Value {
        json!({
            "selected": "w", "path": "~/repos/nexus", "branch": "main", "workspace": "/w",
            "rows": [
                {"id":"i","title":"Quiet","status":"idle","updated":3.0,"age":"3h"},
                {"id":"d","title":"Finished","status":"done","updated":8.0,"age":"1h"},
                {"id":"w","title":"Busy","status":"working","updated":5.0,"age":"now"},
                {"id":"n","title":"Ask","status":"input","state":"awaiting_permission","updated":4.0,"age":"2m",
                 "pending":{"request_id":"r1","kind":"permission","summary":"Allow Bash: git push origin main","ts":1000.0}},
                {"id":"x","title":"Gone","status":"archived","archived":true,"updated":1.0,"age":"9d"},
            ],
        })
    }

    fn render_at(
        width: u16,
        height: u16,
        dashboard: &Value,
        state: &mut Dashboard,
        now: f64,
    ) -> String {
        let backend = ratatui::backend::TestBackend::new(width, height);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| {
                draw_at(
                    frame,
                    frame.area(),
                    dashboard,
                    state,
                    &Colors::default(),
                    0,
                    now,
                )
            })
            .unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol().to_owned())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn sections_are_status_ordered_and_archived_starts_collapsed() {
        let mut state = Dashboard::default();
        let screen = render_at(100, 40, &mixed(), &mut state, 1065.0);
        let l = lines_of(&screen);
        let (needs, working, idle, archived) = (
            at(&l, "▾ Needs you"),
            at(&l, "▾ Working"),
            at(&l, "▾ Idle"),
            at(&l, "▸ Archived"),
        );
        assert!(
            needs < working && working < idle && idle < archived,
            "{screen}"
        );
        // Idle holds idle and done rows, newest prompt first.
        assert!(at(&l, "Finished") < at(&l, "Quiet") && idle < at(&l, "Finished"));
        assert!(
            !screen.contains("Gone"),
            "archived rows are collapsed by default"
        );
        state.archived_open = true;
        let open = render_at(100, 40, &mixed(), &mut state, 1065.0);
        assert!(open.contains("▾ Archived") && open.contains("Gone"));
    }

    #[test]
    fn needs_you_rows_show_the_question_the_wait_and_the_keys() {
        let mut state = Dashboard::default();
        let screen = render_at(100, 40, &mixed(), &mut state, 1065.0);
        let l = lines_of(&screen);
        let ask = at(&l, "◆ Ask");
        assert!(
            l[ask + 1].contains("Allow Bash: git push origin main")
                && l[ask + 1].contains("waiting 01:05"),
            "{screen}"
        );
        assert!(
            l[ask + 2].contains("Enter open  a allow once  d deny"),
            "{screen}"
        );
        // The three lines are one row for clicks.
        assert_eq!(
            state
                .line_rows
                .iter()
                .filter(|r| **r == state.line_rows[ask - usize::from(state.list_area.y)])
                .count(),
            3
        );
        // Other rows stay one line.
        assert!(!l[at(&l, "Busy") + 1].contains("Enter open"));
    }

    fn with_manager(entry: Value) -> Value {
        let mut dashboard = sample();
        dashboard["manager"] = entry;
        dashboard
    }

    fn ready_manager() -> Value {
        json!({
            "known": true, "enabled": true, "tone": "ok", "status": "idle",
            "session": "nexus-manager", "model": "github-copilot/gpt-6-luna",
            "summary": "Ready · 2 sessions launched", "live": "Voice ready", "live_ready": true,
            "privacy": "Voice calls also share other sessions' titles, status and reports with OpenAI",
        })
    }

    #[test]
    fn pinned_manager_sits_between_the_search_and_the_roots_and_is_not_a_row() {
        let mut state = Dashboard::default();
        let screen = render(100, 40, &with_manager(ready_manager()), &mut state);
        let l = lines_of(&screen);
        let (search, manager, first_group) = (
            at(&l, "/ Search sessions"),
            at(&l, "◇ Manager"),
            at(&l, "▾ Needs you"),
        );
        assert!(search < manager && manager < first_group, "{screen}");
        assert!(
            l[manager].contains("pinned · m opens") && l[manager].contains("Voice M"),
            "{}",
            l[manager]
        );
        assert!(screen.contains("Ready · 2 sessions launched"), "{screen}");
        assert!(
            screen.contains("github-copilot/gpt-6-luna") && screen.contains("Voice ready"),
            "model and Live readiness are visible: {screen}"
        );
        assert!(
            screen.contains("share other sessions' titles"),
            "the cross-session privacy line is visible: {screen}"
        );
        // A rule of its own closes the pinned entry before the first group.
        assert!(
            l[manager + 1..first_group].iter().any(|row| row.contains("────")),
            "{screen}"
        );
        // Ordinary roots keep their own rows; the manager is never one of them.
        assert_eq!(
            state.line_rows.iter().flatten().copied().collect::<Vec<_>>(),
            [0, 1]
        );
        assert!(state.manager_hit.width > 0 && state.manager_live_hit.width > 0);
        assert_eq!(state.manager_live_hit.y, state.manager_hit.y);
        let (x, y) = (state.manager_hit.x + 2, state.manager_hit.y);
        assert_eq!(
            state.click(x, y, &with_manager(ready_manager())),
            Some(json!({"kind":"manager_open"}))
        );
        let (x, y) = (state.manager_live_hit.x, state.manager_live_hit.y);
        assert_eq!(
            state.click(x, y, &with_manager(ready_manager())),
            Some(json!({"kind":"manager_live"}))
        );
    }

    #[test]
    fn a_disabled_manager_says_so_and_how_to_turn_it_on_without_a_voice_button() {
        let mut state = Dashboard::default();
        let entry = json!({
            "known": true, "enabled": false, "tone": "off", "status": "idle",
            "summary": "Off · [manager] enabled = false",
            "notes": ["Turn it on with [manager] enabled = true in nexus.toml, then restart the daemon."],
        });
        let screen = render(100, 40, &with_manager(entry.clone()), &mut state);
        assert!(screen.contains("◇ Manager"), "{screen}");
        assert!(screen.contains("Off · [manager] enabled = false"), "{screen}");
        assert!(screen.contains("enabled = true in nexus.toml"), "{screen}");
        assert!(!screen.contains("Voice M"), "no voice while off: {screen}");
        assert_eq!(state.manager_live_hit.width, 0);
        // The row still answers: opening it explains why it is off.
        let (x, y) = (state.manager_hit.x, state.manager_hit.y);
        assert_eq!(
            state.click(x, y, &with_manager(entry)),
            Some(json!({"kind":"manager_open"}))
        );
    }

    #[test]
    fn a_manager_without_copilot_shows_the_exact_sign_in_fix() {
        let mut state = Dashboard::default();
        let entry = json!({
            "known": true, "enabled": true, "tone": "warn", "status": "idle",
            "model": "github-copilot/gpt-6-luna", "live": "Voice ready",
            "summary": "Not signed in · sign in to GitHub Copilot under Settings > Providers",
        });
        let screen = render(100, 40, &with_manager(entry), &mut state);
        assert!(screen.contains("Not signed in"), "{screen}");
        assert!(screen.contains("Providers"), "{screen}");
        assert!(screen.contains("github-copilot/gpt-6-luna"), "{screen}");
    }

    #[test]
    fn no_manager_entry_draws_no_pinned_row() {
        let mut state = Dashboard::default();
        let screen = render(100, 40, &sample(), &mut state);
        assert!(!screen.contains("Manager") && !screen.contains("Voice M"));
        assert_eq!(state.manager_hit.width, 0);
        // An unread entry (no `known`, no summary) draws nothing either.
        let screen = render(100, 40, &with_manager(json!({})), &mut state);
        assert!(!screen.contains("Manager"));
    }

    #[test]
    fn the_pinned_manager_survives_a_small_terminal() {
        let mut state = Dashboard::default();
        let screen = render(50, 16, &with_manager(ready_manager()), &mut state);
        assert!(screen.contains("Manager"), "{screen}");
    }

    #[test]
    fn wrap_announces_a_cut() {
        assert_eq!(wrap("one two three", 7, 5), ["one two", "three"]);
        let cut = wrap("one two three four five six", 7, 2);
        assert_eq!(cut.len(), 2);
        assert!(cut[1].ends_with('…') && cut[1].width() <= 7, "{cut:?}");
    }

    #[test]
    fn chips_follow_section_order() {
        let mut state = Dashboard::default();
        let screen = render_at(100, 40, &mixed(), &mut state, 1065.0);
        // The narrow page keeps counts only (`◆ 1 │ ⠋ 1 │ ◇ 2 │ ● 1`); labels return when they fit.
        let header = screen.lines().find(|l| l.contains("◆ 1")).expect("chips");
        let (a, b, c, d) = (
            header.find("◆ 1").unwrap(),
            header.find("⠋ 1").unwrap(),
            header.find("◇ 2").unwrap(),
            header.find("● 1").unwrap(),
        );
        assert!(a < b && b < c && c < d, "{header}");
        let wide = status_chips(&[&mixed()["rows"][3]], &Colors::default(), 80, 0);
        let text: String = wide.iter().map(|s| s.content.to_string()).collect();
        assert_eq!(text, "◆ 1 needs you");
    }

    #[test]
    fn one_blank_row_separates_new_agent_from_the_rest() {
        let mut state = Dashboard::default();
        let screen = render_at(100, 40, &sample(), &mut state, 0.0);
        let l = lines_of(&screen);
        let new = at(&l, "+ New Agent");
        assert!(
            l[new + 1]
                .trim()
                .trim_matches(|c| c == '│')
                .trim()
                .is_empty(),
            "{screen}"
        );
        assert!(l[new + 2].contains("/ Search sessions"), "{screen}");
    }

    #[test]
    fn archived_heading_click_opens_the_section_and_enter_previews() {
        let mut state = Dashboard::default();
        render_at(100, 40, &mixed(), &mut state, 0.0);
        let heading = state
            .line_headings
            .iter()
            .position(|h| h.as_deref() == Some("Archived"))
            .unwrap();
        state.click(
            state.list_area.x,
            state.list_area.y + heading as u16,
            &mixed(),
        );
        assert!(state.archived_open);
        state.selected_id = "x".into();
        let op = state.key(
            crossterm::event::KeyEvent::new(
                crossterm::event::KeyCode::Enter,
                crossterm::event::KeyModifiers::NONE,
            ),
            &mixed(),
        );
        assert_eq!(op, Some(json!({"kind":"archived_preview","id":"x"})));
    }

    #[test]
    fn keeps_the_painted_theme_background() {
        let light = Color::Rgb(255, 255, 255);
        for width in [50u16, 90, 140] {
            let backend = ratatui::backend::TestBackend::new(width, 30);
            let mut terminal = ratatui::Terminal::new(backend).unwrap();
            let mut state = Dashboard::default();
            terminal
                .draw(|frame| {
                    let area = frame.area();
                    frame
                        .buffer_mut()
                        .set_style(area, Style::default().bg(light));
                    draw(frame, area, &sample(), &mut state, &Colors::default(), 0);
                })
                .unwrap();
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer[(10, 5)].bg, light, "width {width}");
            assert_eq!(
                buffer[(width - 1, 29)].bg,
                light,
                "footer bg, width {width}"
            );
            let footer: String = (0..width).map(|x| buffer[(x, 29)].symbol()).collect();
            assert!(!footer.contains("Esc close"), "width {width}: {footer}");
        }
    }

    #[test]
    fn archive_confirmation_is_visible() {
        let mut state = Dashboard::default();
        state.confirm_archive = Some("a".into());
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("Archive “Alpha”?"));
    }

    #[test]
    fn collapsed_heading_hides_its_rows() {
        let mut state = Dashboard::default();
        state.collapsed.insert("Idle".into());
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("Idle"));
        assert!(!screen.contains("Alpha"));
        assert!(screen.contains("Beta"));
    }

    #[test]
    fn selected_row_keeps_its_age_and_archive_is_available_from_the_keyboard() {
        let mut state = Dashboard::default();
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("now") && screen.contains("2m"));
        assert!(
            !screen.contains("[×]"),
            "rows remain compact and single-line"
        );
        assert_eq!(state.archive_hit.width, 0);
        assert!(state.search_hit.width > 0);
        state.click(state.search_hit.x, state.search_hit.y, &sample());
        assert!(state.searching);
    }

    #[test]
    fn search_filters_by_title_branch_project() {
        let mut state = Dashboard::default();
        state.query = "feat/ui".into();
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("Beta") && !screen.contains("Alpha"));
    }

    #[test]
    fn status_marks_have_ascii_twins_and_unicode_is_unchanged() {
        assert_eq!(marks_for(false, 0), ("◆", "⠋", "◇", "●"));
        let (input, working, idle, done) = marks_for(true, 5);
        for mark in [input, working, idle, done] {
            assert!(mark.is_ascii() && mark.chars().count() == 1, "{mark:?}");
        }
        assert_ne!(
            [input, idle, done].len(),
            0,
            "kept distinct: {input} {idle} {done}"
        );
        assert!(input != idle && idle != done && input != done);
    }

    #[test]
    fn monochrome_marks_the_selected_row_with_a_bar() {
        let draw_with = |mono: bool| {
            let mut state = Dashboard::default();
            let backend = ratatui::backend::TestBackend::new(100, 30);
            let mut terminal = ratatui::Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| {
                    draw(
                        frame,
                        frame.area(),
                        &sample(),
                        &mut state,
                        &Colors {
                            mono,
                            ..Colors::default()
                        },
                        0,
                    )
                })
                .unwrap();
            let buffer = terminal.backend().buffer().clone();
            let id = state.selected_id.clone();
            let bar = nexus_widgets::Glyphs::from_env().focus_bar;
            let rows: Vec<String> = (0..30)
                .map(|y| (0..100).map(|x| buffer[(x, y)].symbol().to_string()).collect())
                .collect();
            (id, bar, rows)
        };
        let (id, bar, rows) = draw_with(true);
        assert!(!id.is_empty(), "a row is selected");
        let marked = rows.iter().filter(|r| r.contains(bar)).count();
        assert_eq!(marked, 1, "exactly the selected row: {rows:#?}");
        let (_, _, rows) = draw_with(false);
        assert!(rows.iter().all(|r| !r.contains(bar)));
    }
}
