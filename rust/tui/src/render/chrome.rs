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

/// What a top-bar cell is, for drawing and for mouse hit-testing.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum TabHit {
    /// `‹`: the previous open session.
    Prev,
    /// `›`: the next open session.
    Next,
    /// `[Dashboard]`: opens the Sessions dashboard (`/sessions`).
    Dashboard,
    /// Sub-agent page only: toggles the details sidebar.
    Details,
}
pub struct TabCell {
    pub kind: TabHit,
    pub start: usize,
    pub end: usize,
}
/// How a top-bar segment is painted.
#[derive(Clone, Copy, PartialEq, Debug)]
enum Seg {
    Title,
    Branch,
    Path,
    Sep,
    Notice,
    Counter,
    Arrow(TabHit),
    Index,
    Dashboard,
    Gap,
}
/// `72K`, `256K`, `1.0M`: token counts for the top-bar counter.
fn short_count(n: u64) -> String {
    match n {
        0..=999 => n.to_string(),
        1_000..=999_999 => format!("{}K", (n + 500) / 1000),
        _ => format!("{:.1}M", n as f64 / 1_000_000.0),
    }
}
/// The open tab to switch to from the current one: `step` is `-1` (`‹`) or `1` (`›`),
/// wrapping around; `None` with fewer than two open tabs.
pub fn cycle_tab(s: &Snapshot, step: isize) -> Option<&crate::bridge::Session> {
    let n = s.tabs.len();
    if n < 2 {
        return None;
    }
    let current = s.tabs.iter().position(|row| row.active).unwrap_or(0);
    s.tabs.get((current as isize + step).rem_euclid(n as isize) as usize)
}
/// Grok's in-session top bar, laid out once for drawing and hit-testing. Left:
/// `Title │ branch ~/path`. Right: update notice, `used / window`, `‹ i/n ›` (two or more
/// open sessions) and `[Dashboard]`, each separated by `│`. Optional pieces drop out
/// (notice first, then the nav, then the counter) before the title is squeezed below 12
/// columns. Returns (start column, text, how to paint it).
/// Split the host breadcrumb into (path, branch). The projection sends `path:branch`
/// (`prototype._display_breadcrumb`); `path › branch` is accepted too. Git refs cannot
/// contain `:`, so the last one separates the branch.
fn partition(text: &str) -> (&str, &str) {
    text.split_once(" › ")
        .or_else(|| text.rsplit_once(':'))
        .unwrap_or((text, ""))
}
fn top_layout(s: &Snapshot, width: usize) -> Vec<(usize, String, Seg)> {
    let dash = "[Dashboard]".to_string();
    let counter = match (s.context_used, s.context_window) {
        (Some(used), Some(window)) if window > 0 => {
            Some(format!("{} / {}", short_count(used), short_count(window)))
        }
        _ => None,
    };
    let nav = (s.tabs.len() > 1).then(|| {
        let at = s.tabs.iter().position(|row| row.active).unwrap_or(0) + 1;
        format!("{at}/{}", s.tabs.len())
    });
    let notice =
        Some(crate::transcript::truncate(&s.update_notice, 28)).filter(|text| !text.is_empty());
    let sep = " │ ";
    // Right group pieces in drop order: the first listed is dropped last.
    let build = |use_counter: bool, use_nav: bool, use_notice: bool| {
        let mut pieces: Vec<Vec<(String, Seg)>> = Vec::new();
        if use_notice {
            if let Some(text) = &notice {
                pieces.push(vec![(text.clone(), Seg::Notice)]);
            }
        }
        if use_counter {
            if let Some(text) = &counter {
                pieces.push(vec![(text.clone(), Seg::Counter)]);
            }
        }
        if use_nav {
            if let Some(text) = &nav {
                pieces.push(vec![
                    ("‹".into(), Seg::Arrow(TabHit::Prev)),
                    (format!(" {text} "), Seg::Index),
                    ("›".into(), Seg::Arrow(TabHit::Next)),
                ]);
            }
        }
        pieces.push(vec![(dash.clone(), Seg::Dashboard)]);
        let mut out: Vec<(String, Seg)> = Vec::new();
        for (i, piece) in pieces.into_iter().enumerate() {
            if i > 0 {
                out.push((sep.into(), Seg::Sep));
            }
            out.extend(piece);
        }
        out.push((" ".into(), Seg::Gap));
        out
    };
    let size = |v: &[(String, Seg)]| v.iter().map(|(t, _)| t.width()).sum::<usize>();
    let mut right = build(true, true, true);
    for flags in [(true, true, false), (false, true, false), (false, false, false)] {
        if width < size(&right) + 14 {
            right = build(flags.0, flags.1, flags.2);
        }
    }
    let right_width = size(&right);
    // Left: title, then `branch ~/path` from the breadcrumb (`path › branch`).
    let (path, branch) = partition(&s.breadcrumb);
    let room = width.saturating_sub(right_width + 2 + 1);
    let mut left: Vec<(String, Seg)> = Vec::new();
    let place = |text: &str, seg: Seg, left: &mut Vec<(String, Seg)>, room: &mut usize| {
        if text.is_empty() || *room == 0 {
            return;
        }
        let text = crate::transcript::truncate(text, *room);
        *room -= text.width();
        left.push((text, seg));
    };
    let mut room = room;
    let title = if s.title.is_empty() { "Nexus" } else { s.title.as_str() };
    // The title keeps at most 40 columns, and leaves space for the path when there is any.
    let title_cap = if path.is_empty() { 40 } else { room.min(40) };
    place(
        &crate::transcript::truncate(title, title_cap.min(room)),
        Seg::Title,
        &mut left,
        &mut room,
    );
    if !path.is_empty() && room > 8 {
        place(sep, Seg::Sep, &mut left, &mut room);
        if !branch.is_empty() && room > branch.width() + 6 {
            place(branch, Seg::Branch, &mut left, &mut room);
            place(" ", Seg::Gap, &mut left, &mut room);
        }
        place(path, Seg::Path, &mut left, &mut room);
    }
    let mut out = Vec::new();
    let mut at = 2usize.min(width);
    for (text, seg) in left {
        let w = text.width();
        out.push((at, text, seg));
        at += w;
    }
    let mut at = width.saturating_sub(right_width);
    for (text, seg) in right {
        let w = text.width();
        out.push((at, text, seg));
        at += w;
    }
    out
}
/// Clickable cells of the top bar (arrows and `[Dashboard]`). On a sub-agent page the
/// only cell is the details toggle.
pub fn tab_cells(s: &Snapshot, width: usize) -> Vec<TabCell> {
    if !s.agent_page.is_empty() {
        return vec![TabCell {
            kind: TabHit::Details,
            start: width.saturating_sub(3),
            end: width,
        }];
    }
    top_layout(s, width)
        .into_iter()
        .filter_map(|(start, text, seg)| match seg {
            Seg::Arrow(kind) => Some((kind, start, start + text.width())),
            Seg::Dashboard => Some((TabHit::Dashboard, start, start + text.width())),
            _ => None,
        })
        .map(|(kind, start, end)| TabCell { kind, start, end })
        .collect()
}
pub(super) fn draw_top_bar(frame: &mut Frame, s: &Snapshot, area: Rect, p: &Palette, _spin: usize) {
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
    let background = Style::default().bg(p.background);
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut at = 0usize;
    for (start, text, seg) in top_layout(s, width) {
        if start > at {
            spans.push(Span::styled(" ".repeat(start - at), background));
        }
        at = start + text.width();
        let style = match seg {
            Seg::Title => background.fg(p.text).add_modifier(Modifier::BOLD),
            Seg::Branch | Seg::Sep | Seg::Index | Seg::Gap => background.fg(p.quiet),
            Seg::Path => background.fg(p.text),
            Seg::Notice => background.fg(p.accent),
            Seg::Counter => background.fg(p.muted),
            Seg::Arrow(_) => background.fg(p.muted),
            Seg::Dashboard => background.fg(p.muted),
        };
        spans.push(Span::styled(text, style));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)).style(background), area);
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

/// The row below the composer card is a blank spacer: the path and branch live in the top
/// bar now, so it only keeps the page background.
pub(super) fn draw_workspace_bar(
    frame: &mut Frame,
    _s: &Snapshot,
    area: Rect,
    p: &Palette,
    _spin: usize,
) {
    frame.render_widget(
        Paragraph::new("").style(Style::default().bg(p.background)),
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

#[cfg(test)]
mod chrome_tests {
    use super::*;

    fn bar_text(s: &Snapshot, width: u16) -> String {
        let backend = ratatui::backend::TestBackend::new(width, 1);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_top_bar(frame, s, frame.area(), &Palette::new(false), 0))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect()
    }

    fn open_tabs(count: usize, active: usize) -> Vec<crate::bridge::Session> {
        (0..count)
            .map(|i| crate::bridge::Session {
                id: format!("s{i}"),
                workspace: "/w".into(),
                active: i == active,
                ..Default::default()
            })
            .collect()
    }

    #[test]
    fn top_bar_shows_title_branch_path_counter_nav_and_dashboard() {
        let s = Snapshot {
            title: "Fix the parser".into(),
            breadcrumb: "~/repo:main".into(),
            context_used: Some(72_000),
            context_window: Some(256_000),
            tabs: open_tabs(3, 1),
            ..Snapshot::default()
        };
        let text = bar_text(&s, 100);
        assert!(text.contains("Fix the parser │ main ~/repo"), "{text}");
        assert!(text.contains("72K / 256K │ ‹ 2/3 › │ [Dashboard]"), "{text}");
        let cells = tab_cells(&s, 100);
        let kinds: Vec<_> = cells.iter().map(|c| c.kind).collect();
        assert_eq!(kinds, [TabHit::Prev, TabHit::Next, TabHit::Dashboard]);
        let dash = &cells[2];
        assert_eq!(&text.chars().skip(dash.start).take(dash.end - dash.start).collect::<String>(), "[Dashboard]");
        assert_eq!(cycle_tab(&s, 1).unwrap().id, "s2");
        assert_eq!(cycle_tab(&s, -1).unwrap().id, "s0");
    }

    #[test]
    fn top_bar_hides_arrows_for_one_session_and_fits_any_width() {
        let s = Snapshot {
            title: "A long conversation title that keeps going".into(),
            breadcrumb: "~/a/very/long/path/to/the/repo › feature/branch".into(),
            update_notice: "update 9.9.9".into(),
            context_used: Some(1),
            context_window: Some(2_000_000),
            tabs: open_tabs(1, 0),
            ..Snapshot::default()
        };
        let text = bar_text(&s, 100);
        assert!(!text.contains('‹') && !text.contains('›'));
        assert!(text.contains("update 9.9.9"));
        assert!(cycle_tab(&s, 1).is_none());
        for width in 12..120u16 {
            let cells = tab_cells(&s, usize::from(width));
            assert!(cells.iter().all(|c| c.end <= usize::from(width).max(1)), "{width}");
            let _ = bar_text(&s, width.max(1));
        }
        assert!(bar_text(&s, 30).contains("[Dashboard]"));
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
/// The update notice in the top bar (`area` is the top-bar row) is under this point.
pub fn update_notice_at(s: &Snapshot, area: Rect, x: u16, y: u16) -> bool {
    if s.update_notice.is_empty() || !area.contains((x, y).into()) || y != area.y {
        return false;
    }
    let column = usize::from(x - area.x);
    top_layout(s, usize::from(area.width))
        .iter()
        .any(|(start, text, seg)| {
            *seg == Seg::Notice && column >= *start && column < start + text.width()
        })
}
