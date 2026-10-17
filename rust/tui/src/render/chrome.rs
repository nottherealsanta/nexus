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
    Session(usize),
    Close(usize),
    New,
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
    Session(usize),
    Close(usize),
    New,
    Title,
    Branch,
    Path,
    Sep,
    /// Straight vertical rule between two session tabs.
    Divider,
    Notice,
    Worktree,
    Dashboard,
    Details,
    Gap,
}
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
    // At tiny widths keep room for the active tab and new-session control.
    // Drop padding, then controls rather than overlap the left-hand content.
    if !s.tabs.is_empty() && width < size(&right) + 12 {
        right.retain(|(_, seg)| *seg != Seg::Gap);
    }
    while size(&right) > width.saturating_sub(if s.tabs.is_empty() { 0 } else { 8 }) {
        if right.is_empty() {
            break;
        }
        right.remove(0);
    }
    let right_width = size(&right);
    // Left: a window of session tabs. The workspace breadcrumb has its own row below.
    let margin = if width < 12 { 0 } else { 2 };
    let room = width.saturating_sub(right_width + margin + usize::from(width >= 12));
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
    if s.tabs.is_empty() {
        place(
            &crate::transcript::truncate(title, 40.min(room)),
            Seg::Title,
            &mut left,
            &mut room,
        );
    } else {
        // Keep the active tab in the window. Other tabs retain their full compact
        // width; only the active tab is squeezed when the terminal is very narrow.
        let active = s.tabs.iter().position(|tab| tab.active).unwrap_or(0);
        let labels: Vec<String> = s
            .tabs
            .iter()
            .map(|tab| {
                let glyph = match tab.status.as_str() {
                    "working" => "⠋",
                    "input" => "●",
                    "done" => "✓",
                    _ => "·",
                };
                let title = if tab.title.is_empty() {
                    "New session"
                } else {
                    &tab.title
                };
                format!(" {glyph} {}", crate::transcript::truncate(title, 24))
            })
            .collect();
        let new_width = if room >= 4 { 3 } else { 0 };
        let budget = room.saturating_sub(new_width);
        // Label, close button and the divider that precedes the tab.
        let tab_width = |i: usize| labels[i].width() + 3 + 1;
        let mut first = active;
        let mut last = active + 1;
        let mut used = tab_width(active).min(budget);
        while first > 0 && used + tab_width(first - 1) <= budget {
            first -= 1;
            used += tab_width(first);
        }
        while last < labels.len() && used + tab_width(last) <= budget {
            used += tab_width(last);
            last += 1;
        }
        for (i, label) in labels.iter().enumerate().take(last).skip(first) {
            let full = tab_width(i).min(budget);
            let divider = usize::from(i > first && full > 1);
            let available = full - divider;
            if available == 0 {
                continue;
            }
            if divider == 1 {
                place("│", Seg::Divider, &mut left, &mut room);
            }
            let close_width = if available >= 4 { 3 } else { 0 };
            place(
                &crate::transcript::truncate(label, available - close_width),
                Seg::Session(i),
                &mut left,
                &mut room,
            );
            if close_width > 0 {
                place(" × ", Seg::Close(i), &mut left, &mut room);
            }
        }
    }
    if !s.tabs.is_empty() {
        place(" + ", Seg::New, &mut left, &mut room);
    }
    let mut out = Vec::new();
    let mut at = margin;
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
/// The geometry the chrome buttons (`☰`, `◫`, tabs) sit in. When it changes the buttons move
/// or disappear, and the next frame must repaint every cell (see `needs_full_repaint`).
pub type LayoutKey = (Rect, Rect, u16, u16);
pub fn layout_key(r: &Regions) -> LayoutKey {
    (r.tabs, r.details, r.transcript.x, r.transcript.width)
}
/// Whether moving from the previously painted layout to `next` needs a full repaint.
///
/// Ratatui rebuilds the whole buffer every frame, so its buffer cannot hold a stale glyph; it
/// then writes only the cells that differ from the previous frame. Residue after the buttons
/// move (`☰`, `◫`) can therefore only live in the terminal itself, e.g. a fallback-font glyph
/// drawn wider than its cell. This was not reproduced (no terminal was available); repainting
/// every cell when the layout moves (sidebar toggled, Sessions page opened or closed, resize)
/// removes any such residue regardless of its source.
pub fn needs_full_repaint(previous: Option<LayoutKey>, next: LayoutKey) -> bool {
    previous.is_some_and(|previous| previous != next)
}
/// The workspace row under the tabs: `path branch worktree`, aligned with the first tab label.
fn crumb_layout(s: &Snapshot, width: usize) -> Vec<(usize, String, Seg)> {
    let (path, branch) = partition(&s.breadcrumb);
    let at = if width < 12 { 0 } else { 3 };
    let mut room = width.saturating_sub(at + usize::from(width >= 12));
    let mut out: Vec<(usize, String, Seg)> = Vec::new();
    let mut column = at;
    for (text, seg) in [
        (path, Seg::Path),
        (branch, Seg::Branch),
        (s.worktree.as_str(), Seg::Worktree),
    ] {
        if text.is_empty() {
            continue;
        }
        if !out.is_empty() {
            if room < 2 {
                break;
            }
            out.push((column, " ".into(), Seg::Gap));
            column += 1;
            room -= 1;
        }
        if room == 0 {
            break;
        }
        let text = crate::transcript::truncate(text, room);
        let w = text.width();
        out.push((column, text, seg));
        column += w;
        room = room.saturating_sub(w);
    }
    out
}
/// Clickable session bodies, close buttons, new-session and sidebar controls. On a sub-agent page the
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
            Seg::Session(i) => Some((TabHit::Session(i), start, start + text.width())),
            Seg::Close(i) => Some((TabHit::Close(i), start, start + text.width())),
            Seg::New => Some((TabHit::New, start, start + text.width())),
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
            Rect { height: area.height.min(1), ..area },
        );
        return;
    }
    if area.height == 0 {
        return;
    }
    let width = usize::from(area.width);
    let background = Style::default().bg(p.background);
    let row = |layout: Vec<(usize, String, Seg)>| {
        let mut spans: Vec<Span<'static>> = Vec::new();
        let mut at = 0usize;
        for (start, text, seg) in layout {
            if start > at {
                spans.push(Span::styled(" ".repeat(start - at), background));
            }
            at = start + text.width();
            let style = match seg {
                Seg::Session(i) | Seg::Close(i) => {
                    if s.tabs[i].active {
                        background
                            .bg(p.panel)
                            .fg(p.text)
                            .add_modifier(Modifier::BOLD)
                    } else {
                        background.fg(p.muted)
                    }
                }
                Seg::New => background.fg(p.muted),
                Seg::Title => background.fg(p.text).add_modifier(Modifier::BOLD),
                Seg::Branch | Seg::Worktree | Seg::Sep | Seg::Gap | Seg::Divider => {
                    background.fg(p.quiet)
                }
                Seg::Path => background.fg(p.text),
                Seg::Notice => background.fg(p.accent),
                Seg::Dashboard | Seg::Details => background.fg(p.muted),
            };
            spans.push(Span::styled(text, style));
        }
        Line::from(spans)
    };
    let bar = Rect { height: 1, ..area };
    frame.render_widget(Paragraph::new(row(top_layout(s, width))).style(background), bar);
    if area.height > 1 {
        let crumbs = Rect { y: area.y + 1, height: 1, ..area };
        frame.render_widget(
            Paragraph::new(row(crumb_layout(s, width))).style(background),
            crumbs,
        );
    }
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

    /// The two top-bar rows: the tabs, then the workspace path and branch.
    fn bar_rows(s: &Snapshot, width: u16) -> Vec<String> {
        let backend = ratatui::backend::TestBackend::new(width, 2);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_top_bar(frame, s, frame.area(), &Palette::new(false), 0))
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..2)
            .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
            .collect()
    }
    /// The tab row.
    fn bar_text(s: &Snapshot, width: u16) -> String {
        bar_rows(s, width).remove(0)
    }

    fn open_tabs(count: usize, active: usize) -> Vec<crate::bridge::Session> {
        (0..count)
            .map(|i| crate::bridge::Session {
                id: format!("s{i}"),
                title: format!("Session {i}"),
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
        let rows = bar_rows(&s, 100);
        let text = rows[0].clone();
        assert!(text.contains("Session 1"), "{text}");
        assert!(
            !text.contains("~/repo") && !text.contains("main") && !text.contains("wt-fix"),
            "workspace info left the tab row: {text}"
        );
        assert!(rows[1].trim() == "~/repo main wt-fix", "{:?}", rows[1]);
        assert_eq!(rows[1].find('~'), Some(3), "aligned with the first tab label");
        assert!(
            !text.contains("72K") && !text.contains('‹') && !text.contains('›'),
            "{text}"
        );
        assert!(text.contains("☰"), "{text}");
        assert!(text.contains("◫"), "{text}");
        assert!(!text.contains('⧠') && !text.contains('◨'), "{text}");
        let cells = tab_cells(&s, 100);
        let kinds: Vec<_> = cells.iter().map(|c| c.kind).collect();
        assert!(kinds.contains(&TabHit::Session(1)));
        assert!(kinds.contains(&TabHit::Close(1)));
        assert!(kinds.contains(&TabHit::New));
        let dash = cells.iter().find(|c| c.kind == TabHit::Dashboard).unwrap();
        let details = cells.iter().find(|c| c.kind == TabHit::Details).unwrap();
        assert_eq!(
            &text
                .chars()
                .skip(dash.start)
                .take(dash.end - dash.start)
                .collect::<String>(),
            " ☰ "
        );
        assert!(text.ends_with(" ☰   ◫   "), "{text}");
        assert_eq!(details.start, dash.end + 1);
        assert_eq!(details.end, 98);
        let plain = Snapshot {
            worktree: String::new(),
            ..s
        };
        assert_eq!(bar_rows(&plain, 100)[1].trim(), "~/repo main");
    }

    #[test]
    fn tabs_are_separated_by_straight_dividers() {
        let s = Snapshot {
            tabs: open_tabs(3, 1),
            breadcrumb: "~/repo:main".into(),
            ..Snapshot::default()
        };
        let layout = top_layout(&s, 120);
        let dividers: Vec<_> = layout
            .iter()
            .filter(|(_, _, seg)| *seg == Seg::Divider)
            .collect();
        assert_eq!(dividers.len(), 2, "one between each pair of tabs: {layout:?}");
        assert!(dividers.iter().all(|(_, text, _)| text == "│"));
        let text = bar_text(&s, 120);
        assert_eq!(text.matches('│').count(), 2, "{text}");
        // Each divider sits directly between one tab's close button and the next tab.
        let cells = tab_cells(&s, 120);
        for (start, _, _) in dividers {
            let before = cells.iter().find(|c| c.end == *start).map(|c| c.kind);
            let after = cells.iter().find(|c| c.start == start + 1).map(|c| c.kind);
            assert!(matches!(before, Some(TabHit::Close(_))), "{before:?}");
            assert!(matches!(after, Some(TabHit::Session(_))), "{after:?}");
        }
        // Dividers are not clickable and do not shift the hit targets off their text.
        for cell in &cells {
            let drawn: String = text.chars().skip(cell.start).take(cell.end - cell.start).collect();
            assert!(!drawn.contains('│'), "{:?} covers a divider: {drawn:?}", cell.kind);
        }
        let single = Snapshot {
            tabs: open_tabs(1, 0),
            ..Snapshot::default()
        };
        assert!(!bar_text(&single, 120).contains('│'), "no divider around a lone tab");
    }

    #[test]
    fn workspace_row_truncates_and_never_overflows() {
        let s = Snapshot {
            breadcrumb: "~/a/very/long/path/to/the/repo › feature/branch".into(),
            worktree: "feature-wt".into(),
            tabs: open_tabs(2, 0),
            ..Snapshot::default()
        };
        for width in 1..120u16 {
            let mut end = 0;
            for (start, text, _) in crumb_layout(&s, usize::from(width)) {
                assert!(start >= end, "overlap at {width}");
                end = start + text.width();
                assert!(end <= usize::from(width), "overflow at {width}");
            }
            let _ = bar_rows(&s, width);
        }
        assert!(bar_rows(&s, 100)[1].contains("feature/branch"));
    }

    #[test]
    fn moving_the_chrome_buttons_forces_a_full_repaint() {
        let mut s = Snapshot {
            tabs: open_tabs(2, 0),
            details_sidebar: true,
            ..Snapshot::default()
        };
        let area = Rect::new(0, 0, 140, 30);
        let open = layout_key(&regions(area, &s, 8, false));
        s.details_sidebar = false;
        let closed = layout_key(&regions(area, &s, 8, false));
        assert!(needs_full_repaint(Some(open), closed), "closing the details sidebar");
        assert!(needs_full_repaint(Some(closed), open), "opening it");
        assert!(needs_full_repaint(Some(open), layout_key(&regions(Rect::new(0, 0, 120, 30), &s, 8, false))), "resize");
        s.dashboard = Some(serde_json::json!({"rows": []}));
        let sessions = layout_key(&regions(area, &s, 0, false));
        assert!(needs_full_repaint(Some(closed), sessions), "Sessions page");
        assert!(!needs_full_repaint(None, open), "first frame needs no extra clear");
        s.dashboard = None;
        s.title = "Another title".into();
        s.tabs[1].title = "Renamed".into();
        assert!(
            !needs_full_repaint(Some(closed), layout_key(&regions(area, &s, 8, false))),
            "ordinary text changes keep the incremental diff"
        );
    }

    #[test]
    fn session_tabs_keep_active_and_nonoverlapping_targets_at_every_width() {
        let mut s = Snapshot {
            tabs: open_tabs(8, 2),
            breadcrumb: "~/repo:main".into(),
            ..Default::default()
        };
        s.tabs[2].title = "界面 👩‍💻 active session with a long title".into();
        s.tabs[2].status = "input".into();
        for width in 1..180 {
            let layout = top_layout(&s, width);
            let mut end = 0;
            for (start, text, _) in &layout {
                assert!(*start >= end, "overlap at {width}: {layout:?}");
                end = start + text.width();
                assert!(end <= width, "overflow at {width}: {layout:?}");
            }
            let cells = tab_cells(&s, width);
            assert!(
                cells.iter().any(|c| c.kind == TabHit::Session(2)),
                "{width}: {layout:?}"
            );
            if width >= 24 {
                assert!(cells.iter().any(|c| c.kind == TabHit::Close(2)));
                assert!(cells.iter().any(|c| c.kind == TabHit::New));
                assert!(cells.iter().any(|c| c.kind == TabHit::Dashboard));
                assert!(cells.iter().any(|c| c.kind == TabHit::Details));
            }
        }
        let wide = bar_text(&s, 180);
        assert!(
            wide.contains('●') && wide.contains('×') && wide.contains('+'),
            "{wide}"
        );
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
