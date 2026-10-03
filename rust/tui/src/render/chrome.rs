//! Window chrome: the top bar and session tabs, the sessions sidebar and the details
//! sidebar rows (split out of `render.rs`; hit-testing shares these layouts).
use super::*;

/// What a sidebar row opens when clicked.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum SidebarHit {
    New,
    Archived,
    Session(usize),
}
/// The sessions sidebar as styled rows, like Textual's `SessionSidebar`: a New
/// session button, `SESSIONS N`, day/project headings and two-line cards (glyph
/// and title; status words and age) with a left bar on the current session.
pub fn session_sidebar(
    s: &Snapshot,
    p: &Palette,
    width: usize,
    spin: usize,
    filter: &str,
    _editing: bool,
) -> Vec<(Line<'static>, Option<SidebarHit>)> {
    let pad = |text: String, style: Style| {
        let used = text.width();
        Span::styled(
            format!("{text}{}", " ".repeat(width.saturating_sub(used))),
            style,
        )
    };
    let mut rows = vec![(
        Line::styled("☰ Sessions", Style::default().fg(p.text)),
        Some(SidebarHit::New),
    )];
    let needle = filter.to_lowercase();
    let shown: Vec<usize> = (0..s.sessions.len())
        .filter(|i| {
            let row = &s.sessions[*i];
            needle.is_empty()
                || format!("{} {} {} {}", row.title, row.id, row.workspace, row.group)
                    .to_lowercase()
                    .contains(&needle)
        })
        .collect();
    rows.push((
        Line::styled(
            if needle.is_empty() {
                format!("SESSIONS {}", s.sessions.len())
            } else {
                format!("SESSIONS {} of {}", shown.len(), s.sessions.len())
            },
            Style::default().fg(p.quiet).add_modifier(Modifier::BOLD),
        ),
        None,
    ));
    let mut previous = "";
    for i in shown {
        let session = &s.sessions[i];
        if session.group != previous {
            rows.push((
                Line::styled(
                    session.group.clone(),
                    Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
                ),
                None,
            ));
            previous = &session.group;
        }
        let tone = match session.status.as_str() {
            "working" => p.accent,
            "input" => p.warning,
            "done" => p.quiet,
            _ => p.quiet,
        };
        let glyph = match session.status.as_str() {
            "working" => SPINNER[spin % SPINNER.len()],
            "input" => "●",
            "done" => "·",
            _ => "·",
        };
        let bg = if session.active { p.element } else { p.panel };
        let bar = Span::styled(
            if session.active {
                "▌"
            } else if s
                .tabs
                .iter()
                .any(|tab| tab.id == session.id && tab.workspace == session.workspace)
            {
                "◦"
            } else {
                " "
            },
            Style::default().fg(p.accent).bg(bg),
        );
        let bold = if session.active {
            Modifier::BOLD
        } else {
            Modifier::empty()
        };
        let room = width.saturating_sub(3);
        let title = crate::transcript::truncate(&session.title, room);
        let sub = crate::transcript::truncate(
            if session.sub.is_empty() {
                &session.state
            } else {
                &session.sub
            },
            room,
        );
        rows.push((
            Line::from(vec![
                bar.clone(),
                Span::styled(format!("{glyph} "), Style::default().fg(tone).bg(bg)),
                pad(title, Style::default().fg(p.text).bg(bg).add_modifier(bold)).clone(),
            ]),
            Some(SidebarHit::Session(i)),
        ));
        rows.push((
            Line::from(vec![
                bar,
                Span::styled("  ", Style::default().bg(bg)),
                pad(sub, Style::default().fg(tone).bg(bg)),
            ]),
            Some(SidebarHit::Session(i)),
        ));
    }
    if s.sessions_truncated {
        rows.push((
            Line::styled("[Session list truncated]", Style::default().fg(p.warning)),
            None,
        ));
    }
    if !s.archived_label.is_empty() {
        rows.push((
            Line::styled(s.archived_label.clone(), Style::default().fg(p.quiet)),
            Some(SidebarHit::Archived),
        ));
    }
    rows
}
/// What a top-bar cell is, for drawing and for mouse hit-testing.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum TabHit {
    Sessions,
    Tab(usize),
    New,
    Details,
}
pub struct TabCell {
    pub kind: TabHit,
    pub start: usize,
    pub end: usize,
}
/// Column layout of the tab row: toggle at 0, tabs from 2 (each ` ` + glyph + title
/// + ` × `), then `+` and the details toggle at the right. When tabs do not fit,
/// the leftmost ones scroll out so the current tab stays visible.
pub fn tab_cells(s: &Snapshot, width: usize) -> Vec<TabCell> {
    if !s.agent_page.is_empty() {
        return vec![TabCell {
            kind: TabHit::Details,
            start: width.saturating_sub(3),
            end: width,
        }];
    }
    let mut cells = vec![TabCell {
        kind: TabHit::Sessions,
        start: 0,
        end: 3.min(width),
    }];
    let avail = width.saturating_sub(4 + 7);
    let sizes: Vec<usize> = s
        .tabs
        .iter()
        .map(|row| 6 + crate::transcript::truncate(&row.title, 24).width())
        .collect();
    let current = s.tabs.iter().position(|row| row.active).unwrap_or(0);
    let mut first = 0;
    while first < current
        && sizes[first..=current.min(sizes.len() - 1)]
            .iter()
            .sum::<usize>()
            > avail
    {
        first += 1;
    }
    let mut x = 4;
    for (index, size) in sizes.iter().enumerate().skip(first) {
        if x + size > 4 + avail {
            break;
        }
        cells.push(TabCell {
            kind: TabHit::Tab(index),
            start: x,
            end: x + size,
        });
        x += size;
    }
    cells.push(TabCell {
        kind: TabHit::New,
        start: width.saturating_sub(7),
        end: width.saturating_sub(4),
    });
    cells.push(TabCell {
        kind: TabHit::Details,
        start: width.saturating_sub(3),
        end: width,
    });
    cells
}
pub(super) fn draw_top_bar(frame: &mut Frame, s: &Snapshot, area: Rect, p: &Palette, spin: usize) {
    if !s.agent_page.is_empty() {
        let status = if s.status == "running" {
            "Working"
        } else {
            "Done"
        };
        let model = format!("{} · ", s.model);
        let title = crate::transcript::truncate(
            &s.title,
            usize::from(area.width).saturating_sub(16 + model.width()),
        );
        let gap = usize::from(area.width)
            .saturating_sub(title.width() + model.width() + status.len() + 7);
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(vec![
                    Span::raw(format!("  {title}{} ", " ".repeat(gap))),
                    Span::styled(model, Style::default().fg(p.muted)),
                    Span::raw(format!("{status} ")),
                    Span::styled("▐", Style::default().fg(p.border_strong)),
                ]),
                Line::styled(
                    "─".repeat(usize::from(area.width)),
                    Style::default().fg(p.background),
                ),
            ])
            .style(Style::default().fg(p.text).bg(p.panel)),
            area,
        );
        return;
    }
    if area.height == 0 {
        return;
    }
    let width = usize::from(area.width);
    let background = Style::default().bg(p.panel);
    // Row 0: sessions toggle, tabs, new, details toggle (hit-testing uses the same `tab_cells`).
    let mut tabs: Vec<Span<'static>> = Vec::new();
    let mut at = 0usize;
    let put = |tabs: &mut Vec<Span<'static>>,
               at: &mut usize,
               start: usize,
               text: String,
               style: Style| {
        if start > *at {
            tabs.push(Span::styled(" ".repeat(start - *at), background));
            *at = start;
        }
        *at += text.width();
        tabs.push(Span::styled(text, style));
    };
    for cell in tab_cells(s, width) {
        match cell.kind {
            TabHit::Sessions => {
                put(
                    &mut tabs,
                    &mut at,
                    cell.start,
                    " ☰ ".into(),
                    background.fg(if s.sessions_sidebar {
                        p.accent
                    } else {
                        p.border_strong
                    }),
                );
                if s.tabs.is_empty() {
                    put(
                        &mut tabs,
                        &mut at,
                        4,
                        crate::transcript::truncate(&s.title, width.saturating_sub(12)),
                        background.fg(p.text).add_modifier(Modifier::BOLD),
                    );
                }
            }
            TabHit::Details => put(
                &mut tabs,
                &mut at,
                cell.start,
                " ▐ ".into(),
                background.fg(if s.details_sidebar {
                    p.accent
                } else {
                    p.border_strong
                }),
            ),
            TabHit::New => put(
                &mut tabs,
                &mut at,
                cell.start,
                " + ".into(),
                background.fg(p.muted),
            ),
            TabHit::Tab(index) => {
                let row = &s.tabs[index];
                let bg = if row.active {
                    Style::default().bg(p.panel)
                } else {
                    background
                };
                let tone = match row.status.as_str() {
                    "working" => p.accent,
                    "input" => p.warning,
                    "done" => p.blue,
                    _ => p.quiet,
                };
                let glyph = match row.status.as_str() {
                    "working" => SPINNER[spin % SPINNER.len()],
                    "input" => "●",
                    "done" => "●",
                    _ => "·",
                };
                let title = crate::transcript::truncate(&row.title, 24);
                let title_style = if row.active {
                    bg.fg(p.text).add_modifier(Modifier::BOLD)
                } else {
                    bg.fg(p.quiet)
                };
                put(&mut tabs, &mut at, cell.start, " ".into(), bg);
                put(
                    &mut tabs,
                    &mut at,
                    cell.start + 1,
                    format!("{glyph} "),
                    bg.fg(tone),
                );
                put(&mut tabs, &mut at, cell.start + 3, title, title_style);
                put(
                    &mut tabs,
                    &mut at,
                    cell.end - 3,
                    " × ".into(),
                    bg.fg(p.quiet),
                );
            }
        }
    }
    frame.render_widget(Paragraph::new(Line::from(tabs)).style(background), area);
}
/// Details sidebar rows, and for each row the modified file it toggles (if any).
pub fn details_rows(
    s: &Snapshot,
    p: &Palette,
    width: u16,
) -> (Vec<Line<'static>>, Vec<Option<usize>>) {
    let title = |text: &str| {
        Line::styled(
            text.to_string(),
            Style::default().fg(p.quiet).add_modifier(Modifier::BOLD),
        )
    };
    let quiet = |text: &str| Line::styled(text.to_string(), Style::default().fg(p.quiet));
    let d = &s.details_panel;
    let mut out = vec![title("SESSION")];
    for (label, value) in &d.session {
        for (i, cells) in crate::transcript::wrap(
            &[Span::styled(value.clone(), Style::default().fg(p.text))],
            width.saturating_sub(11) as usize,
        )
        .into_iter()
        .enumerate()
        {
            out.push(crate::transcript::line(
                vec![Span::styled(
                    if i == 0 {
                        format!("{label:<11}")
                    } else {
                        " ".repeat(11)
                    },
                    Style::default().fg(p.muted),
                )],
                cells,
                None,
                Style::default(),
            ));
        }
    }
    out.push(Line::default());
    let mut file_rows: Vec<Option<usize>> = vec![None; out.len()];
    out.push(title(&if d.files.is_empty() {
        "MODIFIED FILES".to_string()
    } else {
        format!("MODIFIED FILES  {}", d.files.len())
    }));
    out.push(Line::default());
    if d.files.is_empty() {
        out.push(quiet("No files changed yet."));
    }
    file_rows.resize(out.len(), None);
    for (index, file) in d.files.iter().enumerate() {
        let (head, base) = file
            .path
            .rsplit_once('/')
            .map_or(("", file.path.as_str()), |(h, b)| (h, b));
        let mut spans = vec![
            Span::styled(
                if file.open { "▾ " } else { "▸ " },
                Style::default().fg(p.quiet),
            ),
            Span::styled(
                if file.created { "A " } else { "M " },
                Style::default().fg(if file.created { p.success } else { p.warning }),
            ),
        ];
        if !head.is_empty() {
            spans.push(Span::styled(
                format!("{head}/"),
                Style::default().fg(p.quiet),
            ));
        }
        spans.push(Span::raw(base.to_string()));
        spans.push(Span::raw("  "));
        if file.added > 0 || file.removed > 0 {
            spans.push(Span::styled(
                format!("+{}", file.added),
                Style::default().fg(p.success),
            ));
            spans.push(Span::styled(
                format!(" -{}", file.removed),
                Style::default().fg(p.error),
            ));
        } else if file.created {
            spans.push(Span::styled("new", Style::default().fg(p.success)));
        }
        file_rows.resize(out.len(), None);
        out.push(Line::from(spans));
        file_rows.push(Some(index));
        if file.open {
            for row in &file.diff {
                let tone = match row.chars().next() {
                    Some('+') => p.success,
                    Some('-') => p.error,
                    Some('@') | Some('…') => p.quiet,
                    _ => p.muted,
                };
                out.push(Line::styled(format!("  {row}"), Style::default().fg(tone)));
            }
            if file.diff.is_empty() {
                out.push(quiet(if file.created {
                    "  New file; no diff preview reported."
                } else {
                    "  No diff preview reported."
                }));
            }
        }
    }
    file_rows.resize(out.len(), None);
    if !d.files_summary.is_empty() {
        out.push(quiet(&d.files_summary));
    }
    out.push(Line::default());
    out.push(title("MCP SERVERS"));
    out.push(Line::default());
    for (tone, text, note) in &d.mcp {
        let color = |name: &str| match name {
            "success" => p.success,
            "error" | "plain-error" => p.error,
            "warning" => p.warning,
            _ => p.quiet,
        };
        if tone.starts_with("plain-") {
            out.push(Line::styled(text.clone(), Style::default().fg(color(tone))));
        } else {
            out.push(Line::from(vec![
                Span::styled("● ", Style::default().fg(color(tone))),
                Span::raw(text.clone()),
                Span::styled(format!("  {note}"), Style::default().fg(p.quiet)),
            ]));
        }
    }
    file_rows.resize(out.len(), None);
    let text = |line: &Line| {
        line.spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect::<String>()
    };
    let files = out
        .iter()
        .position(|line| text(line).starts_with("MODIFIED FILES"))
        .unwrap_or(out.len());
    let mcp = out
        .iter()
        .position(|line| text(line) == "MCP SERVERS")
        .unwrap_or(out.len());
    let range = match d.tab.as_str() {
        "Session" => Some(0..files),
        "Files" => Some(files..mcp),
        "MCP" => Some(mcp..out.len()),
        _ => None,
    };
    if d.tab == "Logs" {
        out.clear();
        out.extend(s.logs.iter().flat_map(|row| {
            crate::transcript::wrap(&[Span::raw(row.clone())], width as usize)
                .into_iter()
                .map(|cells| {
                    crate::transcript::line(vec![], cells, None, Style::default().fg(p.text))
                })
        }));
        file_rows = vec![None; out.len()];
    } else if let Some(range) = range {
        out = out[range.clone()].to_vec();
        file_rows = file_rows[range].to_vec();
    }
    (out, file_rows)
}

/// Workspace metadata below the composer and above the activity meter.
pub(super) fn draw_workspace_bar(
    frame: &mut Frame,
    s: &Snapshot,
    area: Rect,
    p: &Palette,
    spin: usize,
) {
    frame.render_widget(
        Paragraph::new(breadcrumb_row(s, area, p, spin)).style(Style::default().bg(p.background)),
        area,
    );
}

pub fn top_cells(s: &Snapshot, area: Rect) -> Vec<TabCell> {
    tab_cells(s, area.width as usize)
}
pub fn details_tab_at(area: Rect, x: u16) -> Option<&'static str> {
    let mut start = area.x + 2;
    for tab in ["Session", "Files", "MCP", "Logs"] {
        let end = start + tab.len() as u16 + 1;
        if x >= start && x < end {
            return Some(tab);
        }
        start = end;
    }
    None
}

fn breadcrumb_status(s: &Snapshot, spin: usize) -> String {
    match s.status.as_str() {
        "running" | "active" | "working" => format!("{} running", SPINNER[spin % SPINNER.len()]),
        "awaiting_input" | "awaiting_permission" | "input" => "● needs input".into(),
        "" | "idle" => "idle".into(),
        other => other.replace('_', " "),
    }
}
fn notice_width(s: &Snapshot, area: Rect) -> usize {
    let reserved = breadcrumb_status(s, 0).width() + 3;
    crate::transcript::truncate(
        &s.update_notice,
        20.min((area.width as usize).saturating_sub(reserved)),
    )
    .width()
}
fn breadcrumb_row(s: &Snapshot, area: Rect, p: &Palette, spin: usize) -> Line<'static> {
    let width = area.width as usize;
    let status = breadcrumb_status(s, spin);
    let notice = crate::transcript::truncate(&s.update_notice, notice_width(s, area));
    let suffix = " ";
    let reserved =
        status.width() + suffix.width() + notice.width() + if notice.is_empty() { 0 } else { 3 };
    // Match the composer inset (two cells) plus its three-cell rail padding.
    let indent = 5;
    let crumb = crate::transcript::truncate(&s.breadcrumb, width.saturating_sub(reserved + indent));
    let gap = width.saturating_sub(crumb.width() + reserved + indent);
    Line::from(vec![
        Span::styled(
            format!("{}{crumb}{}", " ".repeat(indent), " ".repeat(gap)),
            Style::default().fg(p.muted),
        ),
        Span::styled(notice.clone(), Style::default().fg(p.accent)),
        Span::styled(
            if notice.is_empty() { "" } else { " · " },
            Style::default().fg(p.quiet),
        ),
        Span::styled(
            status,
            Style::default().fg(
                if matches!(s.status.as_str(), "running" | "active" | "working") {
                    crate::transcript::color(&s.agent_color, p.blue, p)
                } else {
                    p.muted
                },
            ),
        ),
        Span::styled(suffix, Style::default().fg(p.border_strong)),
    ])
}
pub fn update_notice_at(s: &Snapshot, area: Rect, x: u16, y: u16) -> bool {
    if s.update_notice.is_empty() || !area.contains((x, y).into()) || y != area.y {
        return false;
    }
    let end = area
        .right()
        .saturating_sub((breadcrumb_status(s, 0).width() + 3 + 1) as u16);
    x >= end.saturating_sub(notice_width(s, area) as u16) && x < end
}
