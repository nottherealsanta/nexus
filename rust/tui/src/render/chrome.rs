//! Window chrome: the top bar and session tabs, the sessions sidebar and the details
//! sidebar rows (split out of `render.rs`; hit-testing shares these layouts).
use super::*;

/// Composer controls share their exact text layout with pointer hit testing.
pub(super) fn control_spans(
    s: &Snapshot,
    p: &Palette,
    width: usize,
    hover: &components::Hover,
    now: std::time::Instant,
) -> Vec<Span<'static>> {
    let layout = control_layout(s, width);
    let accent = crate::transcript::color(&s.agent_color, p.blue, p);
    let mut spans = Vec::new();
    for (i, (text, command)) in layout.left.iter().enumerate() {
        if i > 0 {
            spans.push(Span::styled(" · ", Style::default().fg(p.quiet)));
        }
        let foreground = match *command {
            "/agent" => accent,
            "/model" => p.text,
            _ => p.muted,
        };
        spans.push(Span::styled(
            text.clone(),
            components::button(
                p,
                foreground,
                components::State {
                    hover: hover.amount(command, now),
                    ..Default::default()
                },
            ),
        ));
    }
    spans.push(Span::raw(" ".repeat(layout.gap)));
    let state = components::State {
        hover: hover.amount("context", now),
        ..Default::default()
    };
    if layout.bars {
        let fill = s
            .context_window
            .filter(|window| *window > 0)
            .map(|window| {
                (s.context_used.unwrap_or(0) as f64 / window as f64 * 10.0)
                    .ceil()
                    .clamp(0.0, 10.0) as usize
            })
            .unwrap_or(0);
        for i in 0..10 {
            spans.push(Span::styled(
                "╱",
                components::button(p, if i < fill { accent } else { p.border }, state),
            ));
        }
        spans.push(Span::raw("  "));
    }
    spans.push(Span::styled(
        layout.text,
        components::button(p, p.quiet, state),
    ));
    spans
}

/// What a sidebar row opens when clicked.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum SidebarHit {
    New,
    Archived,
    Session(usize),
}
/// The sessions sidebar as styled rows, like the terminal's `SessionSidebar`: a New
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
    let mut out = vec![title("SESSION METADATA"), Line::default()];
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
                        format!("{:<11}", format!("{label}:"))
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
    if d.session.is_empty() {
        out.push(quiet("No session metadata available."));
    }
    out.push(Line::default());
    let mut file_rows: Vec<Option<usize>> = vec![None; out.len()];
    out.push(title(&format!(
        "CHANGES · {} {}",
        d.files.len(),
        if d.files.len() == 1 { "file" } else { "files" }
    )));
    if !d.files.is_empty() {
        out.push(Line::from(vec![
            Span::styled(
                format!("+{}", d.files.iter().map(|file| file.added).sum::<u64>()),
                Style::default().fg(p.success),
            ),
            Span::styled(
                format!(
                    " -{} lines",
                    d.files.iter().map(|file| file.removed).sum::<u64>()
                ),
                Style::default().fg(p.error),
            ),
        ]));
    }
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
        spans.push(Span::styled(
            base.to_string(),
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        ));
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
        } else {
            spans.push(Span::styled("+0 -0", Style::default().fg(p.quiet)));
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
        // A missing section falls back to `out.len()`, so clamp to keep every range ordered.
        "Session" => Some(0..files.min(mcp)),
        "Files" => Some(files..mcp.max(files)),
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

fn notice_width(s: &Snapshot, area: Rect) -> usize {
    crate::transcript::truncate(
        &s.update_notice,
        20.min(area.width as usize).saturating_sub(1),
    )
    .width()
}
fn breadcrumb_row(s: &Snapshot, area: Rect, p: &Palette, _spin: usize) -> Line<'static> {
    let width = area.width as usize;
    let notice = crate::transcript::truncate(&s.update_notice, notice_width(s, area));
    let suffix = if width == 0 { "" } else { " " };
    let reserved = suffix.width() + notice.width();
    // Match the composer inset (two cells) plus its three-cell rail padding.
    let indent = 5.min(width.saturating_sub(reserved));
    let available = width.saturating_sub(reserved + indent);
    let crumb = if available == 0 {
        String::new()
    } else {
        crate::transcript::truncate(&s.breadcrumb, available)
    };
    let gap = width.saturating_sub(crumb.width() + reserved + indent);
    Line::from(vec![
        Span::styled(
            format!("{}{crumb}{}", " ".repeat(indent), " ".repeat(gap)),
            Style::default().fg(p.quiet),
        ),
        Span::styled(notice.clone(), Style::default().fg(p.accent)),
        Span::styled(suffix, Style::default().fg(p.border_strong)),
    ])
}

#[cfg(test)]
mod chrome_tests {
    use super::*;

    #[test]
    fn breadcrumb_matches_editable_composer_column_and_keeps_suffix() {
        let s = Snapshot {
            breadcrumb: "~/repo › main".into(),
            ..Snapshot::default()
        };
        let p = Palette::new(false);
        let line = breadcrumb_row(&s, Rect::new(0, 0, 48, 1), &p, 0);
        let text: String = line
            .spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect();
        assert!(text.starts_with("     ~/repo › main"));
        assert_eq!(text.width(), 48);
        for width in 0..8 {
            assert!(
                breadcrumb_row(&s, Rect::new(0, 0, width as u16, 1), &p, 0).width() <= width.max(1)
            );
        }
    }

    #[test]
    fn details_sections_and_file_hit_rows_survive_expansion() {
        let s: Snapshot = serde_json::from_value(serde_json::json!({
            "details_panel": {
                "session": [["Title", "Test"]],
                "files": [{"path":"src/main.rs", "added":3, "removed":1,
                    "open":true, "diff":["+new", "-old"]}]
            }
        }))
        .unwrap();
        let (rows, hits) = details_rows(&s, &Palette::new(false), 36);
        let text = |line: &Line<'_>| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>()
        };
        assert_eq!(text(&rows[0]), "SESSION METADATA");
        assert!(rows.iter().any(|row| text(row).contains("Title:")));
        assert!(rows.iter().any(|row| text(row) == "CHANGES · 1 file"));
        let file_row = hits.iter().position(|hit| *hit == Some(0)).unwrap();
        assert!(text(&rows[file_row]).contains("src/main.rs"));
        assert!(rows[file_row]
            .spans
            .iter()
            .any(|span| span.content == "main.rs"
                && span.style.add_modifier.contains(Modifier::BOLD)));
        assert_eq!(text(&rows[file_row + 1]), "  +new");
        assert_eq!(hits[file_row + 1], None);
        assert_eq!(hits.len(), rows.len());
    }

    #[test]
    fn empty_details_have_explicit_status() {
        let (rows, hits) = details_rows(&Snapshot::default(), &Palette::new(false), 36);
        assert!(rows.iter().any(|row| row
            .spans
            .iter()
            .any(|span| span.content == "No files changed yet.")));
        assert!(hits.iter().all(Option::is_none));
    }
}
pub fn update_notice_at(s: &Snapshot, area: Rect, x: u16, y: u16) -> bool {
    if s.update_notice.is_empty() || !area.contains((x, y).into()) || y != area.y {
        return false;
    }
    let end = area.right().saturating_sub(1);
    x >= end.saturating_sub(notice_width(s, area) as u16) && x < end
}
