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
    /// `☰`: opens the Sessions dashboard (`/sessions`).
    Dashboard,
    /// `◫`: toggles the details sidebar.
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
    Worktree,
    Dashboard,
    Details,
    Gap,
}
/// Grok's in-session top bar, laid out once for drawing and hit-testing. Left:
/// `Title │ ~/path branch worktree` (path text colour, branch and worktree quiet; the
/// worktree only inside a linked worktree). Right: update notice, dashboard `☰` and details `◫`,
/// separated by `│`; the notice drops out before the title is squeezed below 12 columns.
/// Returns (start column, text, how to paint it).
/// Split the host breadcrumb into (path, branch). The projection sends `path:branch`
/// (`prototype._display_breadcrumb`); `path › branch` is accepted too. Git refs cannot
/// contain `:`, so the last one separates the branch.
fn partition(text: &str) -> (&str, &str) {
    text.split_once(" › ")
        .or_else(|| text.rsplit_once(':'))
        .unwrap_or((text, ""))
}
fn top_layout(s: &Snapshot, width: usize) -> Vec<(usize, String, Seg)> {
    let dash = " ☰ ".to_string();
    let notice =
        Some(crate::transcript::truncate(&s.update_notice, 28)).filter(|text| !text.is_empty());
    let sep = " │ ";
    let build = |use_notice: bool| {
        let mut out: Vec<(String, Seg)> = Vec::new();
        if use_notice {
            if let Some(text) = &notice {
                out.push((text.clone(), Seg::Notice));
                out.push((sep.into(), Seg::Sep));
            }
        }
        out.push((dash.clone(), Seg::Dashboard));
        out.push((" ".into(), Seg::Gap));
        out.push((" ◫ ".into(), Seg::Details));
        out.push(("  ".into(), Seg::Gap));
        out
    };
    let size = |v: &[(String, Seg)]| v.iter().map(|(t, _)| t.width()).sum::<usize>();
    let mut right = build(true);
    if width < size(&right) + 14 {
        right = build(false);
    }
    let right_width = size(&right);
    // Left: title, then `~/path branch worktree`.
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
    let title = if s.title.is_empty() {
        "New session"
    } else {
        s.title.as_str()
    };
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
        place(path, Seg::Path, &mut left, &mut room);
        if !branch.is_empty() && room > branch.width() + 2 {
            place(" ", Seg::Gap, &mut left, &mut room);
            place(branch, Seg::Branch, &mut left, &mut room);
        }
        if !s.worktree.is_empty() && room > s.worktree.width() + 2 {
            place(" ", Seg::Gap, &mut left, &mut room);
            place(&s.worktree, Seg::Worktree, &mut left, &mut room);
        }
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
/// Padded clickable cells of the top bar (`☰` and `◫`). On a sub-agent page the
/// only cell is the details toggle.
pub fn tab_cells(s: &Snapshot, width: usize) -> Vec<TabCell> {
    if !s.agent_page.is_empty() {
        return vec![TabCell {
            kind: TabHit::Details,
            start: width.saturating_sub(5),
            end: width.saturating_sub(2),
        }];
    }
    top_layout(s, width)
        .into_iter()
        .filter_map(|(start, text, seg)| match seg {
            Seg::Dashboard => Some((TabHit::Dashboard, start, start + text.width())),
            Seg::Details => Some((TabHit::Details, start, start + text.width())),
            _ => None,
        })
        .filter(|(_, start, end)| start < end && *end <= width)
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
            .saturating_sub(title.width() + model.width() + status.len() + 9);
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(vec![
                    Span::raw(format!("  {title}{} ", " ".repeat(gap))),
                    Span::styled(model, Style::default().fg(p.muted)),
                    Span::raw(format!("{status} ")),
                    Span::styled(" ◫ ", Style::default().fg(p.border_strong)),
                    Span::raw("  "),
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
            Seg::Branch | Seg::Worktree | Seg::Sep | Seg::Gap => background.fg(p.quiet),
            Seg::Path => background.fg(p.text),
            Seg::Notice => background.fg(p.accent),
            Seg::Dashboard | Seg::Details => background.fg(p.muted),
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
    fn top_bar_empty_title_uses_new_session_without_brand_or_id() {
        let s = Snapshot::default();
        let text = bar_text(&s, 100);
        assert!(text.contains("New session"), "{text}");
        assert!(
            !text.contains("Nexus") && !text.contains("opaque-session-id"),
            "{text}"
        );
    }

    #[test]
    fn top_bar_shows_title_path_branch_worktree_and_both_controls() {
        let s = Snapshot {
            title: "Fix the parser".into(),
            breadcrumb: "~/repo:main".into(),
            worktree: "wt-fix".into(),
            context_used: Some(72_000),
            context_window: Some(256_000),
            tabs: open_tabs(3, 1),
            ..Snapshot::default()
        };
        let text = bar_text(&s, 100);
        assert!(
            text.contains("Fix the parser │ ~/repo main wt-fix"),
            "{text}"
        );
        assert!(
            !text.contains("72K") && !text.contains('‹') && !text.contains('›'),
            "{text}"
        );
        assert!(text.contains("☰"), "{text}");
        assert!(text.contains("◫"), "{text}");
        assert!(!text.contains('⧠') && !text.contains('◨'), "{text}");
        let cells = tab_cells(&s, 100);
        let kinds: Vec<_> = cells.iter().map(|c| c.kind).collect();
        assert_eq!(kinds, [TabHit::Dashboard, TabHit::Details]);
        let dash = &cells[0];
        assert_eq!(
            &text
                .chars()
                .skip(dash.start)
                .take(dash.end - dash.start)
                .collect::<String>(),
            " ☰ "
        );
        assert!(text.ends_with(" ☰   ◫   "), "{text}");
        assert_eq!(cells[1].start, dash.end + 1);
        assert_eq!(cells[1].end, 98);
        let plain = Snapshot {
            worktree: String::new(),
            ..s
        };
        assert!(bar_text(&plain, 100).contains("Fix the parser │ ~/repo main "));
    }

    #[test]
    fn top_bar_keeps_the_notice_and_fits_any_width() {
        let s = Snapshot {
            title: "A long conversation title that keeps going".into(),
            breadcrumb: "~/a/very/long/path/to/the/repo › feature/branch".into(),
            update_notice: "update 9.9.9".into(),
            worktree: "feature-wt".into(),
            tabs: open_tabs(1, 0),
            ..Snapshot::default()
        };
        let text = bar_text(&s, 100);
        assert!(text.contains("update 9.9.9"));
        for width in 12..120u16 {
            let cells = tab_cells(&s, usize::from(width));
            assert!(
                cells.iter().all(|c| c.end <= usize::from(width).max(1)),
                "{width}"
            );
            let _ = bar_text(&s, width.max(1));
        }
        assert!(bar_text(&s, 30).contains("☰"));
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
