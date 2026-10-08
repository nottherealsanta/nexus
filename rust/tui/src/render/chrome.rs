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
    /// A project · day heading; the index is its first session. Clicking it shows
    /// only that session's project (again: all projects).
    Heading(usize),
}

/// The project a session belongs to: a linked worktree's repository, else its
/// workspace. Filtering, chips and colors all key on this path.
pub fn project_key(row: &crate::bridge::Session) -> &str {
    if row.repo.is_empty() {
        &row.workspace
    } else {
        &row.repo
    }
}

/// A project's display name: the host's label, else the project folder name.
pub fn project_label(row: &crate::bridge::Session) -> String {
    if !row.project.is_empty() {
        return row.project.clone();
    }
    let key = project_key(row);
    std::path::Path::new(key)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| key.to_string())
}

/// A stable color per project (by its path), so a project reads the same in its
/// headings and its filter chip.
pub fn project_color(p: &Palette, key: &str) -> Color {
    let hues = [p.blue, p.purple, p.success, p.warning, p.accent, p.error];
    let hash = key.bytes().fold(0xcbf2_9ce4_8422_2325u64, |h, b| {
        (h ^ u64::from(b)).wrapping_mul(0x100_0000_01b3)
    });
    hues[(hash % hues.len() as u64) as usize]
}

/// Day headings use one color, distinct from every project color.
fn day_color(p: &Palette) -> Color {
    p.cyan
}

/// The projects in the session list, most recently active first: (key, label,
/// number of worktrees with sessions).
pub fn session_projects(s: &Snapshot) -> Vec<(String, String, usize)> {
    let mut out: Vec<(String, String, Vec<&str>)> = Vec::new();
    for row in &s.sessions {
        let key = project_key(row);
        let at = match out.iter().position(|(k, _, _)| k == key) {
            Some(at) => at,
            None => {
                out.push((key.to_string(), project_label(row), Vec::new()));
                out.len() - 1
            }
        };
        if !row.worktree.is_empty() && !out[at].2.contains(&row.worktree.as_str()) {
            out[at].2.push(&row.worktree);
        }
    }
    out.into_iter()
        .map(|(key, label, trees)| (key, label, trees.len()))
        .collect()
}

/// The project filter in effect: a picked project that left the list (archived,
/// deleted) no longer filters.
pub fn picked_project<'a>(s: &Snapshot, project: &'a str) -> &'a str {
    if s.sessions.iter().any(|row| project_key(row) == project) {
        project
    } else {
        ""
    }
}

/// At most this many rows of project chips sit under the sidebar title.
const CHIP_ROWS: usize = 3;
const CHIP_LABEL: usize = 20;

/// The project filter chips laid out in `width` columns: for each chip row, the
/// chips as (start column, end column, project key — empty for "All", label). A
/// project with worktrees reads `name +Nwt` (N worktrees, grouped under it). One
/// layout serves drawing and clicks. Projects that do not fit end in a `+N more`
/// chip with no target (`None`).
pub fn project_chips(
    s: &Snapshot,
    width: usize,
) -> Vec<Vec<(usize, usize, Option<String>, String)>> {
    let projects = session_projects(s);
    if projects.is_empty() || width < 8 {
        return Vec::new();
    }
    let mut chips: Vec<(Option<String>, String)> = vec![(Some(String::new()), "All".into())];
    chips.extend(projects.into_iter().map(|(key, label, trees)| {
        let label = crate::transcript::truncate(&label, CHIP_LABEL);
        (
            Some(key),
            if trees > 0 {
                format!("{label} +{trees}wt")
            } else {
                label
            },
        )
    }));
    let total = chips.len();
    let mut rows: Vec<Vec<(usize, usize, Option<String>, String)>> = vec![Vec::new()];
    let mut x = 0;
    for (index, (key, label)) in chips.into_iter().enumerate() {
        let size = label.width() + 2;
        if x > 0 && x + size > width {
            if rows.len() == CHIP_ROWS {
                // Out of room: replace chips from the end of the last row with `+N more`.
                let last = rows.last_mut().unwrap();
                let mut hidden = total - index;
                loop {
                    let more = format!("+{hidden} more");
                    let end = last.last().map_or(0, |chip| chip.1 + 1);
                    if end + more.width() + 2 <= width || last.is_empty() {
                        last.push((end, end + more.width() + 2, None, more));
                        break;
                    }
                    last.pop();
                    hidden += 1;
                }
                return rows;
            }
            rows.push(Vec::new());
            x = 0;
        }
        rows.last_mut().unwrap().push((x, x + size, key, label));
        x += size + 1;
    }
    rows
}

/// Chip rows as styled lines; `project` is the selected project key (empty: All).
pub fn project_chip_lines(
    s: &Snapshot,
    p: &Palette,
    width: usize,
    project: &str,
) -> Vec<Line<'static>> {
    let project = picked_project(s, project);
    project_chips(s, width)
        .into_iter()
        .map(|row| {
            let mut spans = Vec::new();
            let mut x = 0;
            for (start, end, key, label) in row {
                spans.push(Span::raw(" ".repeat(start.saturating_sub(x))));
                let style = match key.as_deref() {
                    None => Style::default().fg(p.quiet),
                    Some(key) => {
                        let fg = if key.is_empty() {
                            p.text
                        } else {
                            project_color(p, key)
                        };
                        if key == project {
                            Style::default()
                                .fg(fg)
                                .bg(p.element_hi)
                                .add_modifier(Modifier::BOLD)
                        } else {
                            Style::default().fg(fg).bg(p.element)
                        }
                    }
                };
                spans.push(Span::styled(format!(" {label} "), style));
                x = end;
            }
            Line::from(spans)
        })
        .collect()
}

/// Rows above the session list: the title, then the project chips and a rule (when
/// there are chips), else one blank row.
pub fn sessions_list_top(s: &Snapshot, region: Rect) -> u16 {
    let chips = project_chips(s, usize::from(region.width.saturating_sub(3))).len() as u16;
    if chips == 0 {
        2
    } else {
        chips + 2
    }
}

/// Session list rows visible in a sidebar `region` (the filter box is the last row).
pub fn sessions_list_height(s: &Snapshot, region: Rect) -> usize {
    usize::from(
        region
            .height
            .saturating_sub(sessions_list_top(s, region) + 1),
    )
    .max(1)
}

/// The index into `session_sidebar` rows under screen `row`, for list scroll `scroll`
/// (row 0 is the fixed title, so the list starts at index 1).
pub fn sessions_row_at(s: &Snapshot, region: Rect, scroll: usize, row: u16) -> Option<usize> {
    let offset = usize::from(row.checked_sub(region.y + sessions_list_top(s, region))?);
    (offset < sessions_list_height(s, region)).then_some(scroll + 1 + offset)
}

/// The project chip under a click in the sidebar `region`: `Some(key)` (empty for
/// All), or `None` when the click is not on a chip.
pub fn project_chip_at(s: &Snapshot, region: Rect, column: u16, row: u16) -> Option<String> {
    let chips = project_chips(s, usize::from(region.width.saturating_sub(3)));
    let line = chips.get(usize::from(row.checked_sub(region.y + 1)?))?;
    let x = usize::from(column.checked_sub(region.x + 1)?);
    line.iter()
        .find(|chip| x >= chip.0 && x < chip.1)
        .and_then(|chip| chip.2.clone())
}

/// Indexes of the sessions the sidebar shows for `filter` (title, id, workspace,
/// project, worktree or group, case-insensitive) within `project` (a project key;
/// empty: all). Keyboard selection and drawing share this one list.
pub fn visible_sessions(s: &Snapshot, filter: &str, project: &str) -> Vec<usize> {
    let needle = filter.to_lowercase();
    let project = picked_project(s, project);
    (0..s.sessions.len())
        .filter(|i| {
            let row = &s.sessions[*i];
            (project.is_empty() || project_key(row) == project)
                && (needle.is_empty()
                    || format!(
                        "{} {} {} {} {} {}",
                        row.title, row.id, row.workspace, row.project, row.worktree, row.group
                    )
                    .to_lowercase()
                    .contains(&needle))
        })
        .collect()
}

/// The sessions sidebar as styled rows: a New session button, `SESSIONS N`, colored
/// `project › worktree · day` headings and one row per session (status glyph and
/// title) with a left bar on the current session. `selected` is the
/// keyboard-selected session id (drawn with a raised row and a bar).
#[allow(clippy::too_many_arguments)]
pub fn session_sidebar(
    s: &Snapshot,
    p: &Palette,
    width: usize,
    spin: usize,
    filter: &str,
    project: &str,
    _editing: bool,
    selected: Option<&str>,
) -> Vec<(Line<'static>, Option<SidebarHit>)> {
    let pad = |text: String, style: Style| {
        let used = text.width();
        Span::styled(
            format!("{text}{}", " ".repeat(width.saturating_sub(used))),
            style,
        )
    };
    let mut rows = vec![(
        Line::styled("▌ Sessions", Style::default().fg(p.text)),
        Some(SidebarHit::New),
    )];
    let project = picked_project(s, project);
    let shown = visible_sessions(s, filter, project);
    let mut count = vec![Span::styled(
        if filter.is_empty() && project.is_empty() {
            format!("SESSIONS {}", s.sessions.len())
        } else {
            format!("SESSIONS {} of {}", shown.len(), s.sessions.len())
        },
        Style::default().fg(p.quiet).add_modifier(Modifier::BOLD),
    )];
    if let Some(row) = s
        .sessions
        .iter()
        .find(|row| !project.is_empty() && project_key(row) == project)
    {
        count.push(Span::styled(" · ", Style::default().fg(p.quiet)));
        count.push(Span::styled(
            crate::transcript::truncate(&project_label(row), width.saturating_sub(20)),
            Style::default()
                .fg(project_color(p, project))
                .add_modifier(Modifier::BOLD),
        ));
    }
    rows.push((Line::from(count), None));
    let mut previous = "";
    for i in shown {
        let session = &s.sessions[i];
        if session.group != previous {
            // A blank row before each group keeps one-line rows easy to scan.
            rows.push((Line::default(), None));
            let bold = Style::default().add_modifier(Modifier::BOLD);
            let heading = if session.day.is_empty() {
                Line::styled(session.group.clone(), bold.fg(p.muted))
            } else {
                // `project › worktree · day`; with a project picked, its name is in
                // the count row above, so headings drop it.
                let mut spans = Vec::new();
                let tree = if session.worktree.is_empty() {
                    String::new()
                } else {
                    format!("› {}", session.worktree)
                };
                let room = width.saturating_sub(session.day.width() + 3);
                if project.is_empty() {
                    let name = crate::transcript::truncate(
                        &project_label(session),
                        room.saturating_sub(if tree.is_empty() { 0 } else { 6 })
                            .max(4),
                    );
                    let used = name.width();
                    spans.push(Span::styled(
                        name,
                        bold.fg(project_color(p, project_key(session))),
                    ));
                    if !tree.is_empty() {
                        spans.push(Span::styled(
                            format!(
                                " {}",
                                crate::transcript::truncate(&tree, room.saturating_sub(used + 1))
                            ),
                            Style::default().fg(p.muted),
                        ));
                    }
                } else if !tree.is_empty() {
                    spans.push(Span::styled(
                        crate::transcript::truncate(&tree, room),
                        Style::default().fg(p.muted),
                    ));
                }
                if !spans.is_empty() {
                    spans.push(Span::styled(" · ", Style::default().fg(p.quiet)));
                }
                spans.push(Span::styled(
                    session.day.clone(),
                    Style::default().fg(day_color(p)),
                ));
                Line::from(spans)
            };
            rows.push((heading, Some(SidebarHit::Heading(i))));
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
        let picked = selected == Some(session.id.as_str());
        let bg = if picked {
            p.element_hi
        } else if session.active {
            p.element
        } else {
            p.panel
        };
        let bar = Span::styled(
            if session.active || picked {
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
        let bold = if session.active || picked {
            Modifier::BOLD
        } else {
            Modifier::empty()
        };
        let title = crate::transcript::truncate(&session.title, width.saturating_sub(3));
        let fg = if session.active || picked {
            p.text
        } else {
            p.muted
        };
        rows.push((
            Line::from(vec![
                bar,
                Span::styled(format!("{glyph} "), Style::default().fg(tone).bg(bg)),
                pad(title, Style::default().fg(fg).bg(bg).add_modifier(bold)),
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
                    " ▌ ".into(),
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
    // Start at the composer card's `▎` rail (its two-cell inset), not its text column.
    let indent = 2.min(width.saturating_sub(reserved));
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
    fn breadcrumb_starts_at_the_composer_rail_and_keeps_suffix() {
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
        assert!(text.starts_with("  ~/repo › main"));
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
