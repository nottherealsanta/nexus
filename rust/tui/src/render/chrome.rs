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

/// Top-bar button glyphs: one-cell, text-presentation marks from the Geometric Shapes block
/// (no emoji presentation, no Dingbats: those can fall back to a wider font glyph that overflows
/// its cell and leaves residue after a redraw, see `needs_full_repaint`). `NEXUS_ASCII=1` uses
/// plain ASCII twins. All three come from the U+25A0 squares so they share size and baseline
/// (the earlier `◈` and block-element `▐` did not): Sessions `▤`, details sidebar `▥` (vertical panes), Manager `▣`
/// idle and `■` while the manager view is in front. Font coverage of these
/// shapes is not verified beyond the screenshot font.
fn pick(ascii: bool, plain: &'static str, ascii_mark: &'static str) -> &'static str {
    if ascii {
        ascii_mark
    } else {
        plain
    }
}
fn dashboard_glyph() -> &'static str {
    pick(crate::transcript::look::glyphs().ascii, "▤", "=")
}
fn details_glyph() -> &'static str {
    pick(crate::transcript::look::glyphs().ascii, "▥", "|")
}
fn manager_glyph(open: bool) -> &'static str {
    let ascii = crate::transcript::look::glyphs().ascii;
    match (open, ascii) {
        (false, false) => "▣",
        (true, false) => "■",
        (false, true) => "o",
        (true, true) => "*",
    }
}
/// What a top-bar cell is, for drawing and for mouse hit-testing.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum TabHit {
    Back,
    Session(usize),
    Close(usize),
    New,
    /// `▣`: toggles the Manager view (`manager_toggle`); right before the dashboard button.
    Manager,
    /// `▤`: opens the Sessions dashboard (`/sessions`).
    Dashboard,
    /// `▥`: toggles the details sidebar.
    Details,
    /// `◔ N`: opens the notifications drawer.
    Notifications,
}
pub struct TabCell {
    pub kind: TabHit,
    pub start: usize,
    pub end: usize,
}
/// How a top-bar segment is painted.
#[derive(Clone, Copy, PartialEq, Debug)]
enum Seg {
    Back,
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
    Manager,
    Dashboard,
    Details,
    /// The notifications counter.
    Counter,
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
    top_layout_at(s, width, 0)
}
/// `top_layout` with the working-tab spinner on animation frame `spin` (all frames are one cell
/// wide, so geometry and click ranges do not depend on it).
fn top_layout_at(s: &Snapshot, width: usize, spin: usize) -> Vec<(usize, String, Seg)> {
    let dash = format!(" {} ", dashboard_glyph());
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
        if let Some(count) = s.notifications {
            out.push((
                super::notifications::counter_text(count, s.notifications_pop),
                Seg::Counter,
            ));
            out.push((" ".into(), Seg::Gap));
        }
        if s.manager.shown() {
            let glyph = manager_glyph(s.manager.active);
            out.push((format!(" {glyph} "), Seg::Manager));
            out.push((" ".into(), Seg::Gap));
        }
        out.push((dash.clone(), Seg::Dashboard));
        out.push((" ".into(), Seg::Gap));
        out.push((format!(" {} ", details_glyph()), Seg::Details));
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
    // The manager view names itself through the pressed right-hand toggle, not a left title.
    let title = if s.manager.active {
        ""
    } else if s.title.is_empty() {
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
                    "working" => SPINNER[spin % SPINNER.len()],
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
/// The geometry the chrome buttons (`▤`, `▐`, tabs) sit in. When it changes the buttons move
/// or disappear, and the next frame must repaint every cell (see `needs_full_repaint`).
pub type LayoutKey = (Rect, Rect, u16, u16);
pub fn layout_key(r: &Regions) -> LayoutKey {
    (r.tabs, r.details, r.transcript.x, r.transcript.width)
}
/// Whether moving from the previously painted layout to `next` needs a full repaint.
///
/// Ratatui rebuilds the whole buffer every frame, so its buffer cannot hold a stale glyph; it
/// then writes only the cells that differ from the previous frame. Residue after the buttons
/// move (`▤`, `▐`) can therefore only live in the terminal itself, e.g. a fallback-font glyph
/// drawn wider than its cell. This was not reproduced (no terminal was available); repainting
/// every cell when the layout moves (sidebar toggled, Sessions page opened or closed, resize)
/// removes any such residue regardless of its source.
pub fn needs_full_repaint(previous: Option<LayoutKey>, next: LayoutKey) -> bool {
    previous.is_some_and(|previous| previous != next)
}
/// The workspace row under the tabs: `path branch worktree`, aligned with the first tab label.
fn crumb_layout(s: &Snapshot, width: usize) -> Vec<(usize, String, Seg)> {
    // Presentation only: the manager has no workspace of its own to show.
    if s.manager.active {
        return Vec::new();
    }
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
/// Agent header runs are shared by painting and hit testing. Drop optional right
/// content before sacrificing either navigation target; all budgets are cells.
fn agent_layout(s: &Snapshot, width: usize) -> Vec<(usize, String, Seg)> {
    let mut out = Vec::new();
    if width < 3 {
        return out;
    }
    out.push((0, " ← ".into(), Seg::Back));
    let mut right = Vec::new();
    if width >= 7 {
        let mut end = if width >= 9 { width - 2 } else { width };
        right.push((end - 3, format!(" {} ", details_glyph()), Seg::Details));
        end -= 3;
        let status = if s.status == "running" {
            "Working "
        } else {
            "Done "
        };
        if end >= 4 + status.width() {
            end -= status.width();
            right.push((end, status.into(), Seg::Title));
            let model = format!("{} · ", s.model);
            if end >= 4 + model.width() {
                end -= model.width();
                right.push((end, model, Seg::Path));
            }
        }
        let title = crate::transcript::truncate(&s.title, end.saturating_sub(5));
        if !title.is_empty() {
            out.push((4, title, Seg::Title));
        }
    } else if width > 4 {
        out.push((
            4,
            crate::transcript::truncate(&s.title, width - 4),
            Seg::Title,
        ));
    }
    right.reverse();
    out.extend(right);
    out
}
/// Clickable session bodies, close buttons, new-session and sidebar controls. On a sub-agent page the
/// only cell is the details toggle.
pub fn tab_cells(s: &Snapshot, width: usize) -> Vec<TabCell> {
    (if !s.agent_page.is_empty() {
        agent_layout(s, width)
    } else {
        top_layout(s, width)
    })
    .into_iter()
    .filter_map(|(start, text, seg)| match seg {
        Seg::Back => Some((TabHit::Back, start, start + text.width())),
        Seg::Session(i) => Some((TabHit::Session(i), start, start + text.width())),
        Seg::Close(i) => Some((TabHit::Close(i), start, start + text.width())),
        Seg::New => Some((TabHit::New, start, start + text.width())),
        Seg::Manager => Some((TabHit::Manager, start, start + text.width())),
        Seg::Dashboard => Some((TabHit::Dashboard, start, start + text.width())),
        Seg::Details => Some((TabHit::Details, start, start + text.width())),
        Seg::Counter => Some((TabHit::Notifications, start, start + text.width())),
        _ => None,
    })
    .filter(|(_, start, end)| start < end && *end <= width)
    .map(|(kind, start, end)| TabCell { kind, start, end })
    .collect()
}
pub(super) fn draw_top_bar(frame: &mut Frame, s: &Snapshot, area: Rect, p: &Palette, spin: usize) {
    if !s.agent_page.is_empty() {
        let mut spans = Vec::new();
        let mut at = 0;
        for (start, text, seg) in agent_layout(s, usize::from(area.width)) {
            spans.push(Span::raw(" ".repeat(start.saturating_sub(at))));
            at = start + text.width();
            let fg = match seg {
                Seg::Back | Seg::Path | Seg::Details => p.muted,
                _ => p.text,
            };
            spans.push(Span::styled(text, Style::default().fg(fg)));
        }
        frame.render_widget(
            Paragraph::new(Line::from(spans)).style(Style::default().fg(p.text).bg(p.panel)),
            Rect {
                height: area.height.min(1),
                ..area
            },
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
                Seg::Back | Seg::Dashboard | Seg::Details => background.fg(p.muted),
                // Pressed while the manager view is in front.
                Seg::Manager if s.manager.active => background
                    .bg(p.panel)
                    .fg(p.accent)
                    .add_modifier(Modifier::BOLD),
                Seg::Manager => background.fg(p.muted),
                // Warn while something is unread, quiet otherwise; the change pop is bold
                // in the text colour (the peak of the quiet→text range).
                Seg::Counter if s.notifications_pop => {
                    background.fg(p.text).add_modifier(Modifier::BOLD)
                }
                Seg::Counter if s.notifications.unwrap_or(0) > 0 => background.fg(p.warning),
                Seg::Counter => background.fg(p.quiet),
            };
            spans.push(Span::styled(text, style));
        }
        Line::from(spans)
    };
    let bar = Rect { height: 1, ..area };
    let tabs_layout = top_layout_at(s, width, spin);
    frame.render_widget(
        Paragraph::new(row(tabs_layout)).style(background),
        bar,
    );
    // Four rows: tabs, a thin line, the workspace row, then the closing rule. Three rows
    // (short terminals) drop the line. The line shares the closing rule's extent; the closing rule is fainter (`rule_faint`).
    let full = area.height > 3;
    let inset = if width < 12 { 0 } else { 2 };
    let hline = |y: u16, color: ratatui::style::Color| {
        let glyph = if crate::transcript::look::glyphs().ascii { "-" } else { "─" };
        (
            Paragraph::new(Line::from(vec![
                Span::styled(" ".repeat(inset), background),
                Span::styled(
                    glyph.repeat(width.saturating_sub(2 * inset)),
                    background.fg(color),
                ),
            ]))
            .style(background),
            Rect {
                y,
                height: 1,
                ..area
            },
        )
    };
    if full && !s.manager.active {
        let (w, r) = hline(area.y + 1, p.border);
        frame.render_widget(w, r);
    }
    // The workspace row sits under the tabs (and the line).
    if area.height > 1 {
        let crumbs = Rect {
            y: area.y + if full { 2 } else { 1 },
            height: 1,
            ..area
        };
        frame.render_widget(
            Paragraph::new(row(crumb_layout(s, width))).style(background),
            crumbs,
        );
    }
    // A thin rule closes the bar. `─` is drawn mid-cell, so the row reads as half a row of air
    // above a line. Same inset as the tabs; absent in the manager view (no workspace to divide).
    let rule_y = if full { 3 } else { 2 };
    if area.height > rule_y && !s.manager.active {
        let (w, r) = hline(area.y + rule_y, p.rule_faint);
        frame.render_widget(w, r);
    }
}
pub fn top_cells(s: &Snapshot, area: Rect) -> Vec<TabCell> {
    tab_cells(s, area.width as usize)
}

#[cfg(test)]
mod chrome_tests {
    use super::*;

    #[test]
    fn agent_header_geometry_matches_unicode_buffers() {
        for title in ["Agent", "代理任务 🌍🚀", "👩‍💻 long title 世界"] {
            for model in ["model", "模型 🚀 very long model"] {
                for width in 0..=160u16 {
                    let mut s = Snapshot::default();
                    s.agent_page = "agent".into();
                    s.title = title.into();
                    s.model = model.into();
                    let runs = agent_layout(&s, usize::from(width));
                    let mut end = 0;
                    for (start, text, _) in &runs {
                        assert!(*start >= end);
                        end = start + text.width();
                        assert!(end <= usize::from(width));
                    }
                    let backend = ratatui::backend::TestBackend::new(width, 2);
                    let mut terminal = ratatui::Terminal::new(backend).unwrap();
                    terminal
                        .draw(|f| draw_top_bar(f, &s, f.area(), &Palette::new(false), 0))
                        .unwrap();
                    let cells = tab_cells(&s, usize::from(width));
                    assert_eq!(cells.iter().any(|c| c.kind == TabHit::Back), width >= 3);
                    assert_eq!(cells.iter().any(|c| c.kind == TabHit::Details), width >= 7);
                    for cell in cells {
                        let glyph = if cell.kind == TabHit::Back {
                            "←"
                        } else {
                            details_glyph()
                        };
                        assert_eq!(
                            terminal.backend().buffer()[((cell.start + 1) as u16, 0)].symbol(),
                            glyph
                        );
                    }
                }
            }
        }
    }

    /// The two top-bar rows: the tabs, then the workspace path and branch directly beneath.
    fn bar_rows(s: &Snapshot, width: u16) -> Vec<String> {
        let backend = ratatui::backend::TestBackend::new(width, 2);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_top_bar(frame, s, frame.area(), &Palette::new(false), 0))
            .unwrap();
        let buffer = terminal.backend().buffer();
        let row = |y: u16| -> String { (0..width).map(|x| buffer[(x, y)].symbol()).collect() };
        vec![row(0), row(1)]
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
    fn manager_view_hides_left_title_path_and_branch() {
        let mut s = Snapshot::default();
        s.breadcrumb = "~/repo:main".into();
        s.title = "Fix bug".into();
        s.manager.known = true;
        s.manager.enabled = true;
        s.manager.active = true;
        for tabs in [open_tabs(2, 0), Vec::new()] {
            s.tabs = tabs;
            let rows = bar_rows(&s, 100);
            assert!(!rows[0].contains("Manager"), "{:?}", rows[0]);
            assert!(!rows[0].contains("Fix bug"), "{:?}", rows[0]);
            assert!(rows[0].contains('■'), "{:?}", rows[0]);
            assert!(!rows[1].contains("~/repo"), "{:?}", rows[1]);
            assert!(!rows[1].contains("main"), "{:?}", rows[1]);
            assert!(crumb_layout(&s, 100).is_empty());
        }
    }

    #[test]
    fn ordinary_session_keeps_title_path_and_branch() {
        let mut s = Snapshot::default();
        s.breadcrumb = "~/repo:main".into();
        s.title = "Fix bug".into();
        let rows = bar_rows(&s, 100);
        assert!(rows[0].contains("Fix bug"), "{:?}", rows[0]);
        assert!(rows[1].contains("~/repo"), "{:?}", rows[1]);
        assert!(rows[1].contains("main"), "{:?}", rows[1]);
    }

    #[test]
    fn crumb_row_is_adjacent_and_columns_match_layout() {
        let mut s = Snapshot::default();
        s.breadcrumb = "~/repo:main".into();
        s.tabs = open_tabs(2, 0);
        let rows = bar_rows(&s, 100);
        // Path starts at column 3, branch one gap cell after it.
        assert_eq!(rows[1].find("~/repo"), Some(3), "{:?}", rows[1]);
        assert_eq!(
            rows[1].find("main"),
            Some(3 + "~/repo".len() + 1),
            "{:?}",
            rows[1]
        );
        let layout = crumb_layout(&s, 100);
        assert_eq!(layout[0].0, 3);
        // Tab hit cells live on the first row only and are unaffected by the crumb row.
        assert!(tab_cells(&s, 100)
            .iter()
            .any(|c| c.kind == TabHit::Session(0)));
        // Normal sessions use two rows; the second row is blank only for the manager.
        s.manager.active = true;
        assert!(bar_rows(&s, 100)[1].trim().is_empty());
    }

    fn manager_snapshot(active: bool) -> Snapshot {
        let mut s = Snapshot {
            tabs: open_tabs(2, 0),
            ..Snapshot::default()
        };
        s.manager.known = true;
        s.manager.enabled = true;
        s.manager.active = active;
        s
    }

    #[test]
    fn manager_toggle_sits_right_before_the_dashboard_button() {
        let s = manager_snapshot(false);
        let text = bar_rows(&s, 100)[0].clone();
        let cells = tab_cells(&s, 100);
        let manager = cells.iter().find(|c| c.kind == TabHit::Manager).unwrap();
        let dash = cells.iter().find(|c| c.kind == TabHit::Dashboard).unwrap();
        assert_eq!(manager.end + 1, dash.start, "{text}");
        let glyph = |cell: &TabCell| {
            text.chars()
                .skip(cell.start)
                .take(cell.end - cell.start)
                .collect::<String>()
        };
        assert_eq!(glyph(manager), " ▣ ");
        // It is never a session tab.
        assert!(cells
            .iter()
            .all(|c| !matches!(c.kind, TabHit::Session(_)) || c.start < manager.start));
    }

    #[test]
    fn manager_toggle_is_absent_until_the_host_reports_a_manager() {
        let mut s = manager_snapshot(false);
        s.manager = Default::default();
        assert!(tab_cells(&s, 100).iter().all(|c| c.kind != TabHit::Manager));
    }

    #[test]
    fn manager_toggle_reads_pressed_while_the_manager_is_in_front() {
        let paint = |s: &Snapshot| {
            let backend = ratatui::backend::TestBackend::new(100, 3);
            let mut terminal = ratatui::Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_top_bar(frame, s, frame.area(), &Palette::new(false), 0))
                .unwrap();
            let cell = tab_cells(s, 100)
                .into_iter()
                .find(|c| c.kind == TabHit::Manager)
                .unwrap();
            terminal.backend().buffer()[(cell.start as u16 + 1, 0)].clone()
        };
        let p = Palette::new(false);
        let idle = paint(&manager_snapshot(false));
        let pressed = paint(&manager_snapshot(true));
        assert_eq!(idle.symbol(), "▣");
        assert_eq!(idle.fg, p.muted);
        assert_eq!(pressed.symbol(), "■");
        assert_eq!(pressed.fg, p.accent);
        assert_eq!(pressed.bg, p.panel);
    }

    #[test]
    fn closing_rule_is_fainter_than_the_line_under_the_tabs() {
        for light in [false, true] {
            let p = Palette::new(light);
            let s = Snapshot::default();
            let mut terminal =
                ratatui::Terminal::new(ratatui::backend::TestBackend::new(100, 4)).unwrap();
            terminal.draw(|f| draw_top_bar(f, &s, f.area(), &p, 0)).unwrap();
            let buf = terminal.backend().buffer();
            assert_eq!(buf[(10, 1)].fg, p.border, "line under the tabs");
            assert_eq!(buf[(10, 3)].fg, p.rule_faint, "closing rule");
            assert_ne!(p.rule_faint, p.border);
            assert_ne!(p.rule_faint, p.quiet);
            assert_ne!(p.rule_faint, p.element);
        }
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
        assert_eq!(
            rows[1].find('~'),
            Some(3),
            "aligned with the first tab label"
        );
        assert!(
            !text.contains("72K") && !text.contains('‹') && !text.contains('›'),
            "{text}"
        );
        assert!(text.contains(dashboard_glyph()), "{text}");
        assert!(text.contains(details_glyph()), "{text}");
        assert!(
            !text.contains(['☰', '◫', '⧠', '◨', '▐', '◈']),
            "wide/fallback-prone glyphs: {text}"
        );
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
            " ▤ "
        );
        assert!(text.ends_with(" ▤   ▥   "), "{text}");
        assert_eq!(details.start, dash.end + 1);
        assert_eq!(details.end, 98);
        let plain = Snapshot {
            worktree: String::new(),
            ..s
        };
        assert_eq!(bar_rows(&plain, 100)[1].trim(), "~/repo main");
    }

    #[test]
    fn working_tab_spinner_advances_with_the_frame() {
        let mut tabs = open_tabs(2, 0);
        tabs[1].status = "working".into();
        let s = Snapshot {
            tabs,
            ..Snapshot::default()
        };
        let row = |spin: usize| {
            let backend = ratatui::backend::TestBackend::new(80, 3);
            let mut terminal = ratatui::Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_top_bar(frame, &s, frame.area(), &Palette::new(false), spin))
                .unwrap();
            let buffer = terminal.backend().buffer();
            (0..80u16)
                .map(|x| buffer[(x, 0)].symbol().to_string())
                .collect::<String>()
        };
        for spin in 0..SPINNER.len() {
            let text = row(spin);
            assert!(text.contains(SPINNER[spin]), "frame {spin}: {text}");
            assert_eq!(
                SPINNER.iter().filter(|g| text.contains(**g)).count(),
                1,
                "only the working tab spins: {text}"
            );
        }
        let widths = |spin: usize| top_layout_at(&s, 80, spin).len();
        assert_eq!(widths(0), widths(5), "geometry is frame independent");
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
        assert_eq!(
            dividers.len(),
            2,
            "one between each pair of tabs: {layout:?}"
        );
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
            let drawn: String = text
                .chars()
                .skip(cell.start)
                .take(cell.end - cell.start)
                .collect();
            assert!(
                !drawn.contains('│'),
                "{:?} covers a divider: {drawn:?}",
                cell.kind
            );
        }
        let single = Snapshot {
            tabs: open_tabs(1, 0),
            ..Snapshot::default()
        };
        assert!(
            !bar_text(&single, 120).contains('│'),
            "no divider around a lone tab"
        );
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
        assert!(
            needs_full_repaint(Some(open), closed),
            "closing the details sidebar"
        );
        assert!(needs_full_repaint(Some(closed), open), "opening it");
        assert!(
            needs_full_repaint(
                Some(open),
                layout_key(&regions(Rect::new(0, 0, 120, 30), &s, 8, false))
            ),
            "resize"
        );
        s.dashboard = Some(serde_json::json!({"rows": []}));
        let sessions = layout_key(&regions(area, &s, 0, false));
        assert!(needs_full_repaint(Some(closed), sessions), "Sessions page");
        assert!(
            !needs_full_repaint(None, open),
            "first frame needs no extra clear"
        );
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
        assert!(bar_text(&s, 30).contains(dashboard_glyph()));
    }

    #[test]
    fn chrome_button_glyphs_are_single_cell_text_presentation() {
        for glyph in ["←", dashboard_glyph(), details_glyph()] {
            assert_eq!(glyph.chars().count(), 1, "{glyph}");
            assert_eq!(glyph.width(), 1, "{glyph}");
            assert!(!glyph.contains(['\u{fe0f}', '\u{200d}']), "{glyph}");
            let c = glyph.chars().next().unwrap();
            assert!(
                ('\u{2190}'..='\u{25ff}').contains(&c),
                "{glyph} must stay in the arrows/math/box/shape blocks, not emoji ranges"
            );
        }
        for width in 24..160 {
            for cell in tab_cells(
                &Snapshot {
                    tabs: open_tabs(2, 0),
                    ..Snapshot::default()
                },
                width,
            ) {
                if matches!(cell.kind, TabHit::Dashboard | TabHit::Details) {
                    assert_eq!(cell.end - cell.start, 3, "space, glyph, space at {width}");
                }
            }
        }
    }

    #[test]
    fn notification_counter_is_a_top_bar_button_left_of_the_sessions_button() {
        let mut s = Snapshot {
            tabs: open_tabs(2, 0),
            notifications: Some(3),
            ..Snapshot::default()
        };
        let text = bar_text(&s, 100);
        assert!(
            text.contains(" ◔ 3 ") && text.ends_with(" ▤   ▥   "),
            "{text}"
        );
        let cells = tab_cells(&s, 100);
        let counter = cells
            .iter()
            .find(|c| c.kind == TabHit::Notifications)
            .unwrap();
        let dash = cells.iter().find(|c| c.kind == TabHit::Dashboard).unwrap();
        assert_eq!(counter.end + 1, dash.start);
        assert_eq!(counter.end - counter.start, 5);
        s.notifications_pop = true;
        let popped = bar_text(&s, 100);
        assert!(popped.contains("(◔ 3)"), "{popped}");
        assert_eq!(popped.width(), text.width(), "the pop never moves anything");
        s.notifications = None;
        assert!(!tab_cells(&s, 100)
            .iter()
            .any(|c| c.kind == TabHit::Notifications));
        // The counter is the first thing to go when the bar is narrow.
        s.notifications = Some(0);
        s.notifications_pop = false;
        for width in 1..120usize {
            let mut end = 0;
            for (start, text, _) in top_layout(&s, width) {
                assert!(start >= end && start + text.width() <= width, "{width}");
                end = start + text.width();
            }
        }
        let narrow = tab_cells(&s, 24);
        assert!(narrow.iter().any(|c| c.kind == TabHit::Dashboard));
        assert!(narrow.iter().any(|c| c.kind == TabHit::Details));
    }

    #[test]
    fn notification_counter_colours_follow_unread_state() {
        let p = Palette::new(false);
        let colour = |count: usize, pop: bool| {
            let s = Snapshot {
                notifications: Some(count),
                notifications_pop: pop,
                ..Snapshot::default()
            };
            let backend = ratatui::backend::TestBackend::new(100, 3);
            let mut terminal = ratatui::Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_top_bar(frame, &s, frame.area(), &p, 0))
                .unwrap();
            let buffer = terminal.backend().buffer();
            let at = (0..100u16)
                .find(|x| buffer[(*x, 0)].symbol() == "◔")
                .unwrap();
            (
                buffer[(at, 0)].fg,
                buffer[(at, 0)].modifier.contains(Modifier::BOLD),
            )
        };
        assert_eq!(colour(0, false), (p.quiet, false));
        assert_eq!(colour(2, false), (p.warning, false));
        assert_eq!(colour(3, true), (p.text, true));
    }

    #[test]
    fn details_sections_and_file_hit_rows_survive_expansion() {
        let s: Snapshot = serde_json::from_value(serde_json::json!({
            "details_panel": {
                "tab": "Files",
                "session": [["Title", "Test"]],
                "files": [{"path":"src/main.rs", "added":3, "removed":1,
                    "open":true, "diff":["+new", "-old"]}]
            }
        }))
        .unwrap();
        let (rows, hits) = crate::details_sidebar::rows(&s, &Palette::new(false), 36);
        let text = |line: &Line<'_>| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>()
        };
        assert_eq!(text(&rows[0]), "CHANGES · 1 file");
        assert!(!rows.iter().any(|row| text(row).contains("Title:")));
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
        let mut s = Snapshot::default();
        s.details_panel.tab = "Files".into();
        let (rows, hits) = crate::details_sidebar::rows(&s, &Palette::new(false), 36);
        assert!(rows.iter().any(|row| row
            .spans
            .iter()
            .any(|span| span.content == "No files changed yet.")));
        assert!(hits.iter().all(Option::is_none));
    }
}
/// The update notice in the top bar (`area` is the top-bar row) is under this point.
pub fn update_notice_at(s: &Snapshot, area: Rect, x: u16, y: u16) -> bool {
    if !s.agent_page.is_empty()
        || s.update_notice.is_empty()
        || !area.contains((x, y).into())
        || y != area.y
    {
        return false;
    }
    let column = usize::from(x - area.x);
    top_layout(s, usize::from(area.width))
        .iter()
        .any(|(start, text, seg)| {
            *seg == Seg::Notice && column >= *start && column < start + text.width()
        })
}
