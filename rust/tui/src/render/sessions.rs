//! Full-width Sessions dashboard (Grok style). Rows are grouped by last-updated time
//! (Today, Yesterday, This week, Earlier), two lines each (`◇ Title · branch project … age`,
//! then a one-line summary) between a blank padding row above and below, all part of the
//! row's band, hit target and selection background. A `/ Search sessions` row sits
//! above the first group. Nothing expands inline and there is no preview pane: the list
//! uses the whole region above the composer. The composer is the normal composer; this
//! module does not draw one.
use crate::picker::{section, section_label, text, Dashboard};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Clear, Paragraph},
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

fn meta_of(row: &Value) -> String {
    let branch = text(row, "branch");
    let project = text(row, "project");
    match (branch.is_empty(), project.is_empty()) {
        (true, true) => String::new(),
        (false, true) => format!(" · {branch}"),
        (true, false) => format!(" · {project}"),
        (false, false) => format!(" · {branch} {project}"),
    }
}

/// First line of Markdown source as plain text: markers stripped, cut at the first
/// sentence end when that leaves a usable summary.
fn plain_summary(source: &str) -> String {
    let line = source
        .lines()
        .map(|l| l.trim().trim_start_matches(['#', '>', '-', '*', '+', ' ']))
        .find(|l| !l.is_empty() && !l.starts_with("```"))
        .unwrap_or("");
    let line = line
        .trim_start_matches(|c: char| c.is_ascii_digit())
        .trim_start_matches(". ");
    let mut out = line
        .replace("**", "")
        .replace("__", "")
        .replace(['`', '*'], "");
    if let Some(at) = out.find(". ") {
        if at >= 12 {
            out.truncate(at + 1);
        }
    }
    out.trim().to_owned()
}

fn one_line(row: &Value, user: &str, agent: &str, activity: &str) -> String {
    let (user, agent) = (plain_summary(user), plain_summary(agent));
    let (user, agent, activity) = (user.as_str(), agent.as_str(), activity);
    if !activity.is_empty() {
        return activity.to_owned();
    }
    if text(row, "status") == "working" && agent.is_empty() {
        return "Waiting for response...".to_owned();
    }
    if !agent.is_empty() {
        agent.to_owned()
    } else {
        user.to_owned()
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

fn status_glyph(status: &str, colors: &Colors, spin: usize) -> Span<'static> {
    match status {
        "working" => Span::styled(SPIN[spin % SPIN.len()], Style::default().fg(colors.purple)),
        "input" => Span::styled("◆", Style::default().fg(colors.warning)),
        "done" => Span::styled("●", Style::default().fg(colors.muted)),
        _ => Span::styled("◇", Style::default().fg(colors.quiet)),
    }
}

fn status_chips(shown: &[&Value], colors: &Colors, width: usize) -> Vec<Span<'static>> {
    let mut working = 0usize;
    let mut input = 0usize;
    let mut idle = 0usize;
    let mut done = 0usize;
    for row in shown {
        match text(row, "status") {
            "working" => working += 1,
            "input" => input += 1,
            "done" => done += 1,
            _ => idle += 1,
        }
    }
    let items = [
        (working, "⁙", "working", colors.purple),
        (input, "◆", "needs input", colors.warning),
        (idle, "◇", "idle", colors.quiet),
        (done, "●", "done", colors.muted),
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

pub fn draw(
    frame: &mut Frame,
    area: Rect,
    dashboard: &Value,
    state: &mut Dashboard,
    colors: &Colors,
    spin: usize,
) {
    let full = area;
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
    state.resume_hit = Rect::default();
    state.worktree_hit = Rect::default();
    state.choose_hit = Rect::default();
    if area.height < 4 || area.width < 12 {
        return;
    }
    state.sync(dashboard);
    // Two-column gutters on both sides; row backgrounds and the rail use `full`.
    let area = Rect {
        x: area.x + 2,
        width: area.width - 4,
        ..area
    };
    let all = rows_of(dashboard);
    let shown = state.filtered(all);
    let quiet = Style::default().fg(colors.quiet);
    let muted = Style::default().fg(colors.muted);
    let width = usize::from(area.width);

    let chips = status_chips(&shown, colors, width / 2);
    let chips_w = spans_width(&chips);
    let hint = "[Choose Alt+L]";
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

    let (resume, work_key) = if width > 76 {
        ("Open Previous /resume", true)
    } else if width > 48 {
        ("Open Previous", false)
    } else {
        ("", false)
    };
    let show_work = width > 48;
    let mut right: Vec<Span> = Vec::new();
    if !resume.is_empty() {
        right.push(Span::styled(resume, quiet));
    }
    if show_work {
        if !right.is_empty() {
            right.push(Span::styled(" | ", quiet));
        }
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

    let mut constraints = vec![
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(1),
    ];
    if footer.is_some() {
        constraints.push(Constraint::Length(1));
    }
    let bands = Layout::default()
        .direction(Direction::Vertical)
        .constraints(constraints)
        .split(area);
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
        if !resume.is_empty() {
            state.resume_hit = Rect {
                x: start,
                y: bands[2].y,
                width: resume.width() as u16,
                height: 1,
            };
        }
        if show_work {
            let work_x = if resume.is_empty() {
                start
            } else {
                start + resume.width() as u16 + 3
            };
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
        x: full.x,
        width: full.width,
        ..bands[6]
    };
    let mut lines: Vec<RowLine> = Vec::new();
    let mut archive_at: Option<(usize, (usize, usize))> = None;
    let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
    for (index, row) in shown.iter().enumerate() {
        let key = if state.by_project {
            text(row, "group").to_owned()
        } else {
            section_label(section(row)).to_owned()
        };
        if groups.last().is_none_or(|(name, _)| name != &key) {
            groups.push((key, Vec::new()));
        }
        groups.last_mut().unwrap().1.push(index);
    }
    let list_width = usize::from(list.width);
    for (key, members) in &groups {
        if !lines.is_empty() && lines.last().is_some_and(|(_, index, _)| index.is_some()) {
            lines.push((Line::default(), None, None));
        }
        let collapsed = state.collapsed.contains(key);
        let chevron = if collapsed { "▸" } else { "▾" };
        let label = format!("{chevron} {key}");
        let count = format!(" {}", members.len());
        let used = 2 + label.width() + count.width() + 1;
        let rule = "─".repeat(list_width.saturating_sub(used + 2));
        lines.push((
            Line::from(vec![
                Span::raw("  "),
                Span::styled(label, muted.add_modifier(Modifier::BOLD)),
                Span::styled(count, quiet),
                Span::styled(format!(" {rule}"), Style::default().fg(colors.border)),
            ]),
            None,
            Some(key.clone()),
        ));
        if collapsed {
            continue;
        }
        lines.push((Line::default(), None, None));
        for index in members {
            let row = shown[*index];
            let summary = one_line(
                row,
                text(row, "last_user"),
                text(row, "last_agent"),
                text(row, "activity"),
            );
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
            let (mut title, close) = title_spans(
                row,
                list_width,
                is_selected,
                renaming.as_deref(),
                colors,
                spin,
            );
            lines.push((band(Vec::new(), list_width, bg), Some(*index), None));
            if let Some(close) = close {
                archive_at = Some((lines.len(), close));
            }
            let second = vec![
                Span::raw("     "),
                Span::styled(fit(&summary, list_width.saturating_sub(9).max(1)), quiet),
            ];
            title.insert(0, Span::raw("  "));
            lines.push((band(title, list_width, bg), Some(*index), None));
            lines.push((band(second, list_width, bg), Some(*index), None));
            lines.push((band(Vec::new(), list_width, bg), Some(*index), None));
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
    if let Some((at, (start, len))) = archive_at {
        if at >= top && at < top + height {
            state.archive_hit = Rect {
                x: list.x + start as u16,
                y: list.y + (at - top) as u16,
                width: len as u16,
                height: 1,
            };
        }
    }
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
        frame.render_widget(Paragraph::new(line), bands[7]);
    }
}

/// Title row content (after the 2-column indent) and, on the selected row, the `[×]`
/// archive target as (start column, width) within the list.
fn title_spans(
    row: &Value,
    width: usize,
    selected: bool,
    renaming: Option<&str>,
    colors: &Colors,
    spin: usize,
) -> (Vec<Span<'static>>, Option<(usize, usize)>) {
    let quiet = Style::default().fg(colors.quiet);
    let tail = if selected {
        "[×]".to_owned()
    } else {
        age_of(row).to_owned()
    };
    let meta = meta_of(row);
    let title = match renaming {
        Some(draft) => format!("{draft}▏"),
        None => text(row, "title").to_owned(),
    };
    // Inset (1+1), indent (2), glyph and space (2), a gap, and two trailing columns.
    let inner = width.saturating_sub(2);
    let room = inner.saturating_sub(2 + 2 + 1 + 2 + tail.width());
    let meta = if title.width() + meta.width() > room {
        String::new()
    } else {
        meta
    };
    let title = fit(&title, room.saturating_sub(meta.width()).max(1));
    let pad = inner
        .saturating_sub(2 + 2 + title.width() + meta.width() + tail.width() + 2)
        .max(1);
    let close = selected.then(|| (1 + 2 + 2 + title.width() + meta.width() + pad, tail.width()));
    let spans = vec![
        status_glyph(text(row, "status"), colors, spin),
        Span::raw(" "),
        Span::styled(title, Style::default().fg(colors.text)),
        Span::styled(meta, quiet),
        Span::raw(" ".repeat(pad)),
        Span::styled(tail, quiet),
        Span::raw("  "),
    ];
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
        assert!(
            screen.contains("Today") && screen.contains("Alpha") && screen.contains("Yesterday")
        );
        assert!(screen.contains("/ Search sessions"));
        assert!(
            screen.contains("Python sample: frozen Task"),
            "second row summary"
        );
        assert!(!screen.contains("Response"), "no preview pane");
        assert!(screen.contains("+ New Agent") && screen.contains("Open Previous"));
        assert!(screen.contains("main") && screen.contains("~/repos/nexus"));
        assert!(state.list_area.width >= 56);
        assert!(state.new_hit.width > 0 && state.resume_hit.width > 0);
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
        assert_eq!(
            state.line_rows[..5],
            [None, None, Some(0), Some(0), Some(0)]
        );
        assert_eq!(state.line_rows[5], Some(0), "top and bottom padding");
        state.click(state.list_area.x, state.list_area.y + 2, &sample());
        assert_eq!(state.selected_id, "a");
        state.click(state.list_area.x, state.list_area.y + 9, &sample());
        assert_eq!(state.selected_id, "b");
    }

    #[test]
    fn each_row_has_a_top_padding_line_inside_its_band() {
        let mut state = Dashboard::default();
        render(140, 30, &sample(), &mut state);
        let rows = &state.line_rows;
        // heading, spacer, then 4 lines per session: pad, title, summary, pad
        assert_eq!(rows[..2], [None, None]);
        assert_eq!(rows[2..6], [Some(0); 4]);
        // the second session's band starts after any group separator/heading
        let start = rows.iter().position(|row| *row == Some(1)).unwrap();
        assert!(start >= 6);
        assert_eq!(rows[start..start + 4], [Some(1); 4]);
        assert_eq!(rows.iter().filter(|row| **row == Some(1)).count(), 4);
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
        state.collapsed.insert("Today".into());
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("Today"));
        assert!(!screen.contains("Alpha"));
        assert!(screen.contains("Beta"));
    }

    #[test]
    fn selected_row_shows_a_clickable_close_instead_of_the_age() {
        let mut state = Dashboard::default();
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("[×]") && screen.contains("now"));
        assert!(!screen.contains("2m"), "selected age is replaced");
        assert_eq!(state.archive_hit.width, 3);
        let op = state.click(state.archive_hit.x, state.archive_hit.y, &sample());
        assert!(op.is_none());
        assert_eq!(state.confirm_archive.as_deref(), Some("a"));
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
    fn plain_summary_is_first_line() {
        assert_eq!(
            plain_summary("# Hello **world** from `mock`. More text follows\n- item"),
            "Hello world from mock."
        );
        assert_eq!(plain_summary("```\ncode"), "code");
    }
}
