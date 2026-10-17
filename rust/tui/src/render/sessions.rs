//! Compact, centered Sessions page (a bordered, Settings-like card, at most `PAGE_WIDTH`
//! wide and as tall as its content). Rows are grouped by last-updated time (Today,
//! Yesterday, This week, Earlier), one line each: the status glyph, the name, and the
//! last-used time. A `/ Search sessions` row sits
//! above the first group. Nothing expands inline and there is no preview pane or composer.
const PAGE_WIDTH: u16 = 72;

use crate::picker::{section, section_label, text, Dashboard};
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

    // One line per session: status glyph, name, last-used time. Groups keep a heading.
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
    for (key, members) in &groups {
        let collapsed = state.collapsed.contains(key);
        let chevron = if collapsed { "▸" } else { "▾" };
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
            title.insert(0, status_glyph(text(row, "status"), colors, spin));
            title.insert(1, Span::raw(" "));
                title.insert(0, Span::raw("  "));
            lines.push((band(title, list_width, bg), Some(*index), None));
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
    // Size the page to its content (header bands, list, footer, border), centered on screen
    // with at least one blank row above and below when the screen is tall enough.
    let chrome = 5 + u16::from(footer.is_some()) + 2;
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

    let mut constraints = vec![
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
    let list_band = 5;
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
    frame.render_widget(Paragraph::new(search), bands[3]);
    state.search_hit = bands[3];
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled("─".repeat(usize::from(area.width)), Style::default().fg(colors.border)))),
        bands[4],
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
        frame.render_widget(Paragraph::new(line), bands[5]);
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
    let tail = age_of(row).to_owned();
    let title = match renaming {
        Some(draft) => format!("{draft}▏"),
        None => text(row, "title").to_owned(),
    };
    // Inset (1+1), indent (2), glyph and space (2), a gap, and two trailing columns.
    let inner = width.saturating_sub(2);
    let room = inner.saturating_sub(2 + 2 + 1 + 2 + tail.width());
    let title = fit(&title, room.max(1));
    let close = None;
    let spans = vec![
        status_glyph(text(row, "status"), colors, spin),
        Span::raw(" "),
        Span::styled(title, Style::default().fg(colors.text)),
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
        assert!(
            screen.contains("Alpha") && screen.contains("Beta")
        );
        assert!(screen.contains("/ Search sessions"));
        assert!(
            !screen.contains("Python sample: frozen Task"),
            "rows are one line: no summary"
        );
        assert!(!screen.contains("Response"), "no preview pane");
        assert!(screen.contains("+ New Agent") && screen.contains("Open Previous"));
        assert!(screen.contains("main") && screen.contains("~/repos/nexus"));
        assert!(state.list_area.width >= 56);
        assert!(state.new_hit.width > 0 && state.resume_hit.width > 0);
    }

    #[test]
    fn page_is_compact_and_centered() {
        let mut state = Dashboard::default();
        let screen = render(140, 40, &sample(), &mut state);
        let list = state.list_area;
        assert!(list.width <= PAGE_WIDTH, "not full width: {}", list.width);
        let left = list.x;
        let right = 140 - (list.x + list.width);
        assert_eq!(left, right,"horizontally centered");
        assert!(state.new_hit.x > 30 && state.search_hit.x > 30);
        let lines: Vec<&str> = screen.lines().collect();
        assert!(lines[0].trim().is_empty(), "not full-screen: top row empty");
        assert!(lines[39].trim().is_empty(), "not full-screen: bottom row empty");
        assert!(screen.contains('╭') && screen.contains('╯'), "bordered card");
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
        assert_eq!(state.line_rows.iter().flatten().copied().collect::<Vec<_>>(), [0, 1]);
        state.click(state.list_area.x, state.list_area.y + 2, &sample());
        assert_eq!(state.selected_id, "a");
        state.click(state.list_area.x, state.list_area.y + 1, &sample());
        assert_eq!(state.selected_id, "a");
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
    fn selected_row_keeps_its_age_and_archive_is_available_from_the_keyboard() {
        let mut state = Dashboard::default();
        let screen = render(140, 30, &sample(), &mut state);
        assert!(screen.contains("now") && screen.contains("2m"));
        assert!(!screen.contains("[×]"), "rows remain compact and single-line");
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

}
