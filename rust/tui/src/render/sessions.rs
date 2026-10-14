//! Full-width Sessions dashboard (Grok style). Rows are grouped by status, two lines each
//! (title and branch, then a one-line summary). Nothing expands inline: the selected or
//! hovered session's `Response` preview is a bordered box at the bottom of this region,
//! directly above the composer and as wide as its card. The composer is the normal
//! composer; this module does not draw one.
use crate::picker::{section, section_label, text, Dashboard};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};
use serde_json::Value;
use unicode_width::UnicodeWidthStr;

pub type MarkdownFn = Box<dyn Fn(&str, usize) -> Vec<Line<'static>>>;

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
    /// Renders the preview's Markdown tail at a width; plain wrapped text without it.
    pub markdown: Option<MarkdownFn>,
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
            markdown: None,
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

fn wrap_lines(value: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut lines = Vec::new();
    let mut rest = value.trim();
    while !rest.is_empty() && lines.len() < 12 {
        if rest.width() <= width {
            lines.push(rest.to_owned());
            break;
        }
        let mut end = 0;
        let mut last_space = None;
        let mut acc = 0;
        for (index, ch) in rest.char_indices() {
            let w = unicode_width::UnicodeWidthChar::width(ch).unwrap_or(0);
            if acc + w > width && end > 0 {
                break;
            }
            acc += w;
            end = index + ch.len_utf8();
            if ch == ' ' {
                last_space = Some(end);
            }
        }
        let cut = last_space.filter(|at| *at > 0).unwrap_or(end.max(1));
        let (head, tail) = rest.split_at(cut.min(rest.len()));
        lines.push(head.trim_end().to_owned());
        rest = tail.trim_start();
    }
    lines
}

fn take_lines(value: &str, width: usize, max_lines: usize) -> Vec<String> {
    let mut lines = wrap_lines(value, width);
    if lines.len() <= max_lines {
        return lines;
    }
    lines.truncate(max_lines);
    if let Some(last) = lines.last_mut() {
        if !last.ends_with('…') {
            *last = fit(last, width.max(1));
            if !last.ends_with('…') {
                last.push('…');
            }
        }
    }
    lines
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

fn tail_field(row: &Value, preview: &Value, key: &str) -> String {
    let matched = preview.as_object().is_some()
        && preview.get("session").and_then(Value::as_str) == Some(text(row, "id"));
    if matched {
        if key == "activity" {
            return text(preview, key).to_owned();
        }
        let value = text(preview, key);
        if !value.is_empty() {
            return value.to_owned();
        }
    }
    text(row, key).to_owned()
}

/// First line of Markdown source as plain text: markers stripped, cut at the first
/// sentence end when that leaves a usable summary.
fn plain_summary(source: &str) -> String {
    let line = source
        .lines()
        .map(|l| l.trim().trim_start_matches(['#', '>', '-', '*', '+', ' ']))
        .find(|l| !l.is_empty() && !l.starts_with("```"))
        .unwrap_or("");
    let line = line.trim_start_matches(|c: char| c.is_ascii_digit()).trim_start_matches(". ");
    let mut out = line.replace("**", "").replace("__", "").replace(['`', '*'], "");
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
                spans.push(Span::styled(" │ ", Style::default().fg(colors.border_strong)));
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
    frame.buffer_mut().set_style(area, Style::default().bg(bg).fg(fg));
    state.list_area = Rect::default();
    state.preview_area = Rect::default();
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
    let area = Rect { x: area.x + 2, width: area.width - 4, ..area };
    let all = rows_of(dashboard);
    let shown = state.filtered(all);
    let quiet = Style::default().fg(colors.quiet);
    let muted = Style::default().fg(colors.muted);
    let width = usize::from(area.width);

    let chips = status_chips(&shown, colors, width / 2);
    let chips_w = spans_width(&chips);
    let hint = "[Choose Ctrl+L]";
    let mut location = String::new();
    let branch = text(dashboard, "branch");
    let path = text(dashboard, "path");
    if !branch.is_empty() {
        location.push_str(branch);
        location.push(' ');
    }
    location.push_str(path);
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
    let mut header = vec![Span::styled(location.clone(), Style::default().fg(colors.text))];
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
            Style::default().fg(colors.purple).add_modifier(Modifier::BOLD),
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
        Some(Line::from(Span::styled(state.notice.clone(), Style::default().fg(colors.accent))))
    } else if let Some(error) = dashboard.get("error").and_then(Value::as_str).filter(|e| !e.is_empty()) {
        Some(Line::from(Span::styled(format!("Preview error: {error}"), quiet)))
    } else if state.searching {
        Some(Line::from(vec![
            Span::styled("/ ", Style::default().fg(colors.accent)),
            Span::raw(state.query.clone()),
            Span::styled("▏", Style::default().fg(colors.accent)),
        ]))
    } else if !state.query.is_empty() {
        Some(Line::from(Span::styled(
            format!("/ {}  ({} match) · Esc clears", state.query, shown.len()),
            quiet,
        )))
    } else {
        None
    };

    let mut constraints = vec![
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

    let list = Rect { x: full.x, width: full.width, ..bands[4] };
    let target_id = if !state.hovered_id.is_empty() {
        state.hovered_id.clone()
    } else {
        state.selected_id.clone()
    };
    let preview = &dashboard["preview"];
    let target = shown.iter().find(|row| text(row, "id") == target_id).copied();
    // The Response box sits at the bottom of this region, above the composer.
    let box_height = match target {
        Some(row) if list.height >= 12 => {
            Some(preview_lines(row, preview, usize::from(area.width), usize::from(list.height / 2), colors))
        }
        _ => None,
    };
    let (list, preview_rect) = match &box_height {
        Some((lines, _)) => {
            let height = (lines.len() as u16 + 2).min(list.height.saturating_sub(5));
            (
                Rect { height: list.height - height - 1, ..list },
                Some(Rect { x: area.x, y: list.bottom() - height, width: area.width, height }),
            )
        }
        None => (list, None),
    };
    let mut lines: Vec<RowLine> = Vec::new();
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
            let id = text(row, "id");
            let is_selected = id == state.selected_id;
            let is_hover = !is_selected && id == state.hovered_id;
            let renaming = is_selected.then(|| state.rename.clone()).flatten();
            let user = tail_field(row, preview, "last_user");
            let agent = tail_field(row, preview, "last_agent");
            let activity = tail_field(row, preview, "activity");
            let summary = one_line(row, &user, &agent, &activity);
            let bg = if is_selected {
                Some(colors.selected)
            } else if is_hover {
                Some(colors.hover)
            } else {
                None
            };
            let rail = if is_selected {
                Span::styled("│", Style::default().fg(colors.border_strong))
            } else {
                Span::raw(" ")
            };
            let mut title = title_line(row, list_width, is_selected, renaming.as_deref(), colors, spin);
            title.spans.insert(0, rail.clone());
            title.spans.insert(1, Span::raw("   "));
            let mut second = Line::from(vec![
                rail,
                Span::raw("     "),
                Span::styled(
                    fit(&summary, list_width.saturating_sub(9).max(1)),
                    muted,
                ),
            ]);
            if let Some(bg) = bg {
                title = title.style(Style::default().bg(bg));
                second = second.style(Style::default().bg(bg));
            }
            lines.push((title, Some(*index), None));
            lines.push((second, Some(*index), None));
            lines.push((Line::default(), None, None));
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
            index.is_some_and(|i| shown.get(i).is_some_and(|row| text(row, "id") == state.selected_id))
        })
        .unwrap_or(0);
    let mut selected_end = selected_line;
    if lines.get(selected_line).and_then(|(_, index, _)| *index).is_some() {
        while selected_end + 1 < lines.len() && lines[selected_end + 1].1 == lines[selected_line].1 {
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
    state.line_headings = visible.iter().map(|(_, _, heading)| heading.clone()).collect();
    state.list_area = list;
    frame.render_widget(
        Paragraph::new(visible.into_iter().map(|(line, _, _)| line).collect::<Vec<_>>()),
        list,
    );
    if let (Some(rect), Some((lines, age))) = (preview_rect, box_height) {
        state.preview_area = rect;
        let block = Block::default()
            .borders(Borders::ALL)
            .border_set(ratatui::symbols::border::PLAIN)
            .border_style(Style::default().fg(colors.border));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let mut body = lines;
        body.truncate(usize::from(inner.height));
        if let Some(first) = body.first_mut() {
            let used = spans_width(&first.spans);
            let room = usize::from(inner.width).saturating_sub(used + age.width() + 2);
            first.spans.push(Span::raw(" ".repeat(room + 1)));
            first.spans.push(Span::styled(age, quiet));
        }
        frame.render_widget(Paragraph::new(body), inner);
    }
    if let Some(line) = footer {
        frame.render_widget(Paragraph::new(line), bands[5]);
    }
}

/// The Response box body (header, user message, Markdown tail, duration) and the age.
fn preview_lines(
    row: &Value,
    preview: &Value,
    width: usize,
    max_rows: usize,
    colors: &Colors,
) -> (Vec<Line<'static>>, String) {
    let quiet = Style::default().fg(colors.quiet);
    let muted = Style::default().fg(colors.muted);
    let user = tail_field(row, preview, "last_user");
    let agent = tail_field(row, preview, "last_agent");
    let activity = tail_field(row, preview, "activity");
    let text_w = width.saturating_sub(4).max(1);
    let pad = || Span::raw(" ");
    let mut lines = vec![Line::from(vec![pad(), Span::styled("Response", quiet)])];
    let worked = {
        let matched = preview.get("session").and_then(Value::as_str) == Some(text(row, "id"));
        let value = if matched { text(preview, "worked") } else { "" };
        if value.is_empty() { text(row, "worked") } else { value }.to_owned()
    };
    let reserved = 1 + usize::from(!worked.is_empty());
    let budget = max_rows.saturating_sub(2 + reserved).max(2);
    if user.is_empty() && agent.is_empty() && activity.is_empty() {
        lines.push(Line::from(vec![pad(), Span::styled("no messages yet", quiet)]));
    }
    if !user.is_empty() {
        let wrapped = take_lines(&user, text_w.saturating_sub(2).max(1), 2);
        let clipped = wrapped.last().is_some_and(|l| l.ends_with('…'));
        for (i, line) in wrapped.into_iter().enumerate() {
            let mark = if i == 0 { "❯ " } else { "  " };
            lines.push(Line::from(vec![
                pad(),
                Span::styled(mark, Style::default().fg(colors.accent)),
                Span::styled(line, Style::default().fg(colors.text).add_modifier(Modifier::BOLD)),
            ]));
        }
        if clipped {
            lines.push(Line::from(vec![pad(), Span::styled("…", quiet)]));
        }
    }
    let room = budget.saturating_sub(lines.len().saturating_sub(1));
    if !agent.is_empty() && room > 0 {
        let rows: Vec<Line<'static>> = match &colors.markdown {
            Some(render) => render(&agent, text_w),
            None => wrap_lines(&agent, text_w)
                .into_iter()
                .map(|l| Line::from(Span::styled(l, muted)))
                .collect(),
        };
        let skip = if rows.len() > room { rows.len() - room.saturating_sub(1) } else { 0 };
        let mut shown: Vec<Line<'static>> = rows.into_iter().skip(skip).collect();
        if skip > 0 {
            shown.insert(0, Line::from(Span::styled("…", quiet)));
        }
        for mut line in shown {
            line.spans.insert(0, pad());
            lines.push(line);
        }
    }
    if !activity.is_empty() {
        lines.push(Line::from(vec![
            pad(),
            Span::styled(fit(&activity, text_w), Style::default().fg(colors.purple)),
        ]));
    }
    if !worked.is_empty() {
        lines.push(Line::from(vec![pad(), Span::styled(format!("Worked for {worked}"), quiet)]));
    }
    (lines, age_of(row).to_owned())
}

fn title_line(
    row: &Value,
    width: usize,
    selected: bool,
    renaming: Option<&str>,
    colors: &Colors,
    spin: usize,
) -> Line<'static> {
    let quiet = Style::default().fg(colors.quiet);
    let age = age_of(row).to_owned();
    let meta = meta_of(row);
    let title = match renaming {
        Some(draft) => format!("{draft}▏"),
        None => text(row, "title").to_owned(),
    };
    // Gutters (2 + 2), glyph and space, and one space before the age.
    let room = width.saturating_sub(4 + 2 + 1 + 2 + age.width());
    let meta = if title.width() + meta.width() > room {
        String::new()
    } else {
        meta
    };
    let title = fit(&title, room.saturating_sub(meta.width()).max(1));
    let pad = width
        .saturating_sub(4 + 2 + 2 + title.width() + meta.width() + age.width())
        .max(1);
    Line::from(vec![
        status_glyph(text(row, "status"), colors, spin),
        Span::raw(" "),
        Span::styled(
            title,
            Style::default().fg(colors.text).add_modifier(if selected {
                Modifier::BOLD
            } else {
                Modifier::empty()
            }),
        ),
        Span::styled(meta, quiet),
        Span::raw(" ".repeat(pad)),
        Span::styled(age, quiet),
        Span::raw("  "),
    ])
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
                {"id":"a","title":"Alpha","status":"idle","day":"Today","project":"nexus","branch":"main","age":"2m",
                 "last_user":"What should we work on?","last_agent":"Python sample: frozen Task"},
                {"id":"b","title":"Beta","status":"input","day":"Today","project":"nexus","branch":"feat/ui","age":"now",
                 "worktree":"feat/ui","last_user":"Need a decision","last_agent":"Which option?"},
            ],
            "preview": {"session":"a","title":"Alpha","state":"idle","last_user":"What should we work on?","last_agent":"Python sample: frozen Task"},
        })
    }

    #[test]
    fn summary_is_plain_first_line() {
        assert_eq!(plain_summary("# Hello **world** from `mock`. More text follows\n- item"), "Hello world from mock.");
        assert_eq!(plain_summary("```\ncode"), "code");
    }

    #[test]
    fn draws_without_rows() {
        let screen = render(90, 25, &json!({"rows":[],"path":"~/repos/nexus","branch":"main"}), &mut Dashboard::default());
        assert!(screen.contains("+ New Agent"));
        assert!(screen.contains("No sessions yet"));
    }

    #[test]
    fn wide_layout_is_a_full_width_list() {
        let mut state = Dashboard::default();
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("Needs input") && screen.contains("Alpha") && screen.contains("Idle"));
        assert!(screen.contains("What should we work on?"));
        assert!(screen.contains("Response") && screen.contains("❯ What should we work on?"));
        assert!(screen.contains("+ New Agent") && screen.contains("Open Previous"));
        assert!(screen.contains("main") && screen.contains("~/repos/nexus"));
        assert_eq!(state.preview_area.width, 136);
        assert_eq!(state.preview_area.bottom(), 30);
        assert!(state.list_area.width >= 56);
        assert!(state.new_hit.width > 0 && state.resume_hit.width > 0);
    }

    #[test]
    fn narrow_layout_has_list_only() {
        let mut state = Dashboard::default();
        let screen = render(50, 20, &sample(), &mut state);
        assert!(screen.contains("Alpha"));
        assert!(!screen.contains("Agent build"));
        assert_eq!(state.preview_area.width, 46);
    }

    #[test]
    fn rows_map_to_click_targets() {
        let mut state = Dashboard::default();
        render(140, 30, &sample(), &mut state);
        assert_eq!(state.line_rows[..3], [None, None, Some(0)]);
        let op = state.click(state.list_area.x, state.list_area.y + 2, &sample());
        assert_eq!(op.unwrap()["id"], "b");
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
                    frame.buffer_mut().set_style(area, Style::default().bg(light));
                    draw(frame, area, &sample(), &mut state, &Colors::default(), 0);
                })
                .unwrap();
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer[(10, 5)].bg, light, "width {width}");
            assert_eq!(buffer[(width - 1, 29)].bg, light, "footer bg, width {width}");
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
}
