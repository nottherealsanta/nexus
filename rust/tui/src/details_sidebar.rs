//! Right details sidebar: Session, Changes, MCP and Logs beside (or, on a narrow terminal, over
//! the edge of) the conversation.
//!
//! It mirrors the left sidebar (`sidebar.rs`): wrapped ` Label ` tabs, a rule, a body, a hint row
//! and a draggable divider. Python owns every datum (`details_panel` in the snapshot); this
//! module only places, paints and hit-tests it. Layout is pure: `geometry` derives every
//! rectangle from the area and the snapshot, and painting, tab clicks, the Logs copy-ID
//! target and Changes row selection all read it, so a click lands on what is drawn. Each tab
//! builds its own rows (`session_rows`, `changes_rows`, `mcp_rows`, `log_rows`); nothing is
//! cut out of a combined list by matching section titles.
use crate::bridge::Snapshot;
use crate::render::Palette;
use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    layout::{Position, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};
use serde_json::{json, Value};
use unicode_width::UnicodeWidthStr;

/// `(stored id, label)`. The id is what Python persists and the bridge carries (`details_tab`);
/// the Changes tab keeps its historical `Files` id so saved preferences stay valid.
pub const TABS: [(&str, &str); 7] = [
    ("Session", "Session"),
    ("Files", "Changes"),
    ("Browser", "Files"),
    ("Git", "Git"),
    ("MCP", "MCP"),
    ("Jobs", "Jobs"),
    ("Logs", "Logs"),
];
/// Mirrors `nexus/ui_support/details.py` (pinned by `tests/test_ratatui_details_sidebar.py`).
pub const MIN_WIDTH: u16 = 24;
pub const MAX_WIDTH: u16 = 60;
pub const DEFAULT_WIDTH: u16 = 40;
/// From this terminal width the sidebar is docked: the conversation never drops below
/// `MIN_CONVERSATION`. Narrower terminals get the drawer, which leaves `DRAWER_CONVERSATION`.
pub const DRAWER_BELOW: u16 = 100;
pub const MIN_CONVERSATION: u16 = 56;
pub const DRAWER_CONVERSATION: u16 = 24;

/// A stored or dragged width, bounded to `MIN_WIDTH..=MAX_WIDTH` (0 means "not set").
pub fn clamp_width(width: u16) -> u16 {
    if width == 0 {
        DEFAULT_WIDTH
    } else {
        width.clamp(MIN_WIDTH, MAX_WIDTH)
    }
}

/// Columns the sidebar takes from the right edge of a terminal `total` columns wide.
pub fn width_for(s: &Snapshot, total: u16) -> u16 {
    if !s.details_sidebar {
        return 0;
    }
    let wanted = clamp_width(s.details_panel.width);
    if total >= DRAWER_BELOW {
        wanted.min(total.saturating_sub(MIN_CONVERSATION))
    } else {
        wanted
            .min(total)
            .min(total.saturating_sub(DRAWER_CONVERSATION.min(total)))
    }
}

/// The widest the sidebar may be dragged in a terminal `total` columns wide.
pub fn max_width(total: u16) -> u16 {
    if total >= DRAWER_BELOW {
        total
            .saturating_sub(MIN_CONVERSATION)
            .clamp(MIN_WIDTH, MAX_WIDTH)
    } else {
        total.saturating_sub(DRAWER_CONVERSATION).min(MAX_WIDTH)
    }
}

/// The width a drag to terminal `column` asks for (the divider is the sidebar's first column).
pub fn width_at(column: u16, right: u16, total: u16) -> u16 {
    right
        .saturating_sub(column)
        .min(max_width(total))
        .max(MIN_WIDTH)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TabCell {
    pub tab: usize,
    pub row: u16,
    pub x: u16,
    pub width: u16,
}

/// Tab labels (` Label `, one column apart) wrapped onto as many rows as the width needs.
pub fn tab_cells(inner: u16) -> (Vec<TabCell>, u16) {
    let mut cells = Vec::new();
    let (mut x, mut row) = (0u16, 0u16);
    for (tab, (_, label)) in TABS.iter().enumerate() {
        let width = label.width() as u16 + 2;
        if x > 0 && x.saturating_add(width) > inner {
            row += 1;
            x = 0;
        }
        cells.push(TabCell {
            tab,
            row,
            x,
            width: width.min(inner.saturating_sub(x)),
        });
        x = x.saturating_add(width + 1);
    }
    (cells, row + 1)
}

/// The column inside the divider and the right margin; `None` when too small to lay out.
fn inner_of(area: Rect) -> Option<Rect> {
    let inner = Rect {
        x: area.x + 1,
        y: area.y,
        width: area.width.saturating_sub(2),
        height: area.height,
    };
    (inner.width >= 8 && inner.height >= 6).then_some(inner)
}

/// Index into `TABS` of the tab the snapshot shows (a stored `Files` or a `Changes` both match).
pub fn active(s: &Snapshot) -> usize {
    TABS.iter()
        .position(|(id, label)| {
            *id == s.details_panel.tab || (*label == "Changes" && s.details_panel.tab == "Changes")
        })
        .unwrap_or(0)
}

/// Every tab's rectangle in the drawn sidebar `area`, with its index into `TABS`.
pub fn tab_rects(area: Rect) -> Vec<(Rect, usize)> {
    let Some(inner) = inner_of(area) else {
        return Vec::new();
    };
    let (cells, _) = tab_cells(inner.width);
    cells
        .iter()
        .map(|cell| {
            (
                Rect::new(inner.x + cell.x, inner.y + 1 + cell.row, cell.width, 1),
                cell.tab,
            )
        })
        .collect()
}

/// The stored id of the tab under a click or the pointer.
pub fn tab_at(area: Rect, x: u16, y: u16) -> Option<&'static str> {
    tab_rects(area)
        .into_iter()
        .find(|(rect, _)| rect.contains(Position::new(x, y)))
        .map(|(_, tab)| TABS[tab].0)
}

pub type Rows = (Vec<Line<'static>>, Vec<Option<usize>>);

/// Everything placed inside the sidebar: painting and hit-testing share it.
pub struct Geometry {
    pub tabs: Vec<(Rect, usize)>,
    pub rule: Rect,
    /// Logs only: the session facts above the log tail, one entry index per wrapped line.
    pub header: Rect,
    pub header_lines: Vec<(usize, Line<'static>)>,
    pub body: Rect,
    pub footer: Rect,
}

pub fn geometry(area: Rect, s: &Snapshot, p: &Palette) -> Option<Geometry> {
    let inner = inner_of(area)?;
    let (cells, tab_rows) = tab_cells(inner.width);
    let rule_y = inner.y + 1 + tab_rows;
    let top = rule_y + 1;
    let footer_y = area.bottom().saturating_sub(1);
    if footer_y <= top {
        return None;
    }
    let available = footer_y - top;
    let x = inner.x + 1;
    let width = inner.width - 1;
    let mut header_lines = if TABS[active(s)].0 == "Logs" {
        log_header(s, p, width)
    } else {
        Vec::new()
    };
    // The log tail always keeps three rows.
    header_lines.truncate(usize::from(available.saturating_sub(3)));
    let header_height = header_lines.len() as u16;
    Some(Geometry {
        tabs: cells
            .iter()
            .map(|cell| {
                (
                    Rect::new(inner.x + cell.x, inner.y + 1 + cell.row, cell.width, 1),
                    cell.tab,
                )
            })
            .collect(),
        rule: Rect::new(inner.x, rule_y, inner.width, 1),
        header: Rect::new(x, top, width, header_height),
        header_lines,
        body: Rect::new(x, top + header_height, width, available - header_height),
        footer: Rect::new(inner.x, footer_y, inner.width, 1),
    })
}

/// The first row of `rows` drawn in a body `height` rows tall: Logs follow the tail (scrolled
/// back by `logs_scroll`), the other tabs start at `scroll`.
pub fn first_row(
    s: &Snapshot,
    rows: usize,
    height: u16,
    scroll: usize,
    logs_scroll: usize,
) -> usize {
    let height = usize::from(height);
    if TABS[active(s)].0 == "Logs" {
        rows.saturating_sub(height).saturating_sub(logs_scroll)
    } else {
        scroll.min(rows.saturating_sub(height))
    }
}

/// The furthest a non-Logs tab scrolls for `rows` rows in the sidebar drawn in `area`.
pub fn max_scroll(area: Rect, s: &Snapshot, p: &Palette, rows: usize) -> usize {
    geometry(area, s, p).map_or(0, |g| rows.saturating_sub(usize::from(g.body.height)))
}

/// The changed file (index into `details_panel.files`) whose row is drawn at `(x, y)`.
#[allow(clippy::too_many_arguments)]
pub fn file_at(
    area: Rect,
    s: &Snapshot,
    p: &Palette,
    rows: &Rows,
    scroll: usize,
    logs_scroll: usize,
    x: u16,
    y: u16,
) -> Option<usize> {
    let g = geometry(area, s, p)?;
    if TABS[active(s)].0 != "Files" || !g.body.contains(Position::new(x, y)) {
        return None;
    }
    let first = first_row(s, rows.0.len(), g.body.height, scroll, logs_scroll);
    rows.1
        .get(first + usize::from(y - g.body.y))
        .copied()
        .flatten()
}

/// Whether `(x, y)` is on the Logs header's Session line (the copy-ID target).
pub fn copy_id_at(area: Rect, s: &Snapshot, p: &Palette, x: u16, y: u16) -> bool {
    let Some(g) = geometry(area, s, p) else {
        return false;
    };
    g.header.contains(Position::new(x, y))
        && g.header_lines
            .get(usize::from(y - g.header.y))
            .is_some_and(|(entry, _)| *entry == 0)
}

// ----- rows ---------------------------------------------------------------------------

fn title(p: &Palette, text: &str) -> Line<'static> {
    Line::styled(
        text.to_string(),
        Style::default().fg(p.quiet).add_modifier(Modifier::BOLD),
    )
}

fn quiet(p: &Palette, text: &str) -> Line<'static> {
    Line::styled(text.to_string(), Style::default().fg(p.quiet))
}

/// The rows of the active tab for a body `width` columns wide, and for each row the changed
/// file it toggles (only the Changes tab has any).
pub fn rows(s: &Snapshot, p: &Palette, width: u16) -> Rows {
    match TABS[active(s)].0 {
        "Files" => changes_rows(s, p),
        "MCP" => plain(mcp_rows(s, p)),
        "Jobs" => plain(jobs_rows(s, p, width)),
        "Logs" => plain(log_rows(s, p, width)),
        _ => plain(session_rows(s, p, width)),
    }
}

fn plain(lines: Vec<Line<'static>>) -> Rows {
    let hits = vec![None; lines.len()];
    (lines, hits)
}

fn session_rows(s: &Snapshot, p: &Palette, width: u16) -> Vec<Line<'static>> {
    let d = &s.details_panel;
    let mut out = vec![title(p, "SESSION METADATA"), Line::default()];
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
        out.push(quiet(p, "No session metadata available."));
    }
    out
}

fn changes_rows(s: &Snapshot, p: &Palette) -> Rows {
    let d = &s.details_panel;
    let mut out = vec![title(
        p,
        &format!(
            "CHANGES · {} {}",
            d.files.len(),
            if d.files.len() == 1 { "file" } else { "files" }
        ),
    )];
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
        out.push(quiet(p, "No files changed yet."));
    }
    let mut hits: Vec<Option<usize>> = vec![None; out.len()];
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
        out.push(Line::from(spans));
        hits.push(Some(index));
        if file.open {
            for row in &file.diff {
                let tone = match row.chars().next() {
                    Some('+') => p.success,
                    Some('-') => p.error,
                    Some('@') | Some('…') => p.quiet,
                    _ => p.muted,
                };
                out.push(Line::styled(format!("  {row}"), Style::default().fg(tone)));
                hits.push(None);
            }
            if file.diff.is_empty() {
                out.push(quiet(
                    p,
                    if file.created {
                        "  New file; no diff preview reported."
                    } else {
                        "  No diff preview reported."
                    },
                ));
                hits.push(None);
            }
        }
    }
    if !d.files_summary.is_empty() {
        out.push(quiet(p, &d.files_summary));
        hits.push(None);
    }
    (out, hits)
}

fn mcp_rows(s: &Snapshot, p: &Palette) -> Vec<Line<'static>> {
    let mut out = vec![title(p, "MCP SERVERS"), Line::default()];
    let color = |name: &str| match name {
        "success" => p.success,
        "error" | "plain-error" => p.error,
        "warning" => p.warning,
        _ => p.quiet,
    };
    for (tone, text, note) in &s.details_panel.mcp {
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
    if s.details_panel.mcp.is_empty() {
        out.push(quiet(p, "No MCP servers reported."));
    }
    out
}

/// The job the Jobs tab has selected (Python owns the selection; the first row by default).
pub fn selected_job(s: &Snapshot) -> Option<&crate::bridge::JobRow> {
    let rows = &s.details_panel.jobs.rows;
    rows.iter()
        .find(|row| row.selected)
        .or_else(|| rows.first())
}

/// The id of the job a step of `delta` rows from the selection would select.
pub fn neighbour_job(s: &Snapshot, delta: isize) -> Option<&str> {
    let rows = &s.details_panel.jobs.rows;
    let at = selected_job(s).and_then(|job| rows.iter().position(|row| row.id == job.id))?;
    let to = at.saturating_add_signed(delta).min(rows.len() - 1);
    Some(rows[to].id.as_str())
}

fn jobs_rows(s: &Snapshot, p: &Palette, width: u16) -> Vec<Line<'static>> {
    let jobs = &s.details_panel.jobs;
    let wrap = |text: &str, style: Style, indent: &str| -> Vec<Line<'static>> {
        crate::transcript::wrap(
            &[Span::styled(text.to_string(), style)],
            usize::from(width).saturating_sub(indent.width()).max(1),
        )
        .into_iter()
        .map(|cells| {
            crate::transcript::line(
                vec![Span::raw(indent.to_string())],
                cells,
                None,
                Style::default(),
            )
        })
        .collect()
    };
    if let Some(log) = &jobs.log {
        let name = jobs
            .rows
            .iter()
            .find(|row| row.id == log.id)
            .map_or(log.id.as_str(), |row| row.command.as_str());
        let mut out = vec![title(p, "JOB LOG")];
        out.extend(wrap(name, Style::default().fg(p.muted), ""));
        out.push(Line::default());
        match log.status.as_str() {
            "loading" => out.push(quiet(p, "Loading log…")),
            "ok" => {
                if log.clipped {
                    out.push(quiet(p, "… earlier output not shown"));
                }
                if log.lines.is_empty() {
                    out.push(quiet(p, "The log is empty."));
                }
                for line in &log.lines {
                    if line.is_empty() {
                        out.push(Line::default());
                    } else {
                        out.extend(wrap(line, Style::default().fg(p.text), ""));
                    }
                }
            }
            _ => out.push(Line::styled(
                if log.message.is_empty() {
                    "The log is unavailable.".to_string()
                } else {
                    log.message.clone()
                },
                Style::default().fg(p.error),
            )),
        }
        return out;
    }
    let mut out = vec![title(
        p,
        &format!(
            "JOBS · {} running{}",
            jobs.running,
            if jobs.truncated {
                " · list clipped"
            } else {
                ""
            }
        ),
    )];
    out.push(Line::default());
    if !jobs.error.is_empty() {
        out.extend(wrap(
            &format!("Refresh failed: {}", jobs.error),
            Style::default().fg(p.error),
            "",
        ));
    }
    if jobs.rows.is_empty() {
        out.push(quiet(
            p,
            if jobs.loaded {
                "No shell jobs in this session."
            } else {
                "Loading jobs…"
            },
        ));
    }
    for row in &jobs.rows {
        let tone = match row.status.as_str() {
            "running" => p.accent,
            "completed" => p.success,
            "failed" | "timed_out" => p.error,
            _ => p.warning,
        };
        let mark = if row.selected { "▸ " } else { "  " };
        let command = if row.selected {
            Style::default().fg(p.text).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(p.text)
        };
        let mut head = wrap(&row.command, command, "  ");
        if let Some(first) = head.first_mut() {
            first.spans[0] = Span::styled(mark, Style::default().fg(p.accent));
        }
        out.extend(head);
        let mut facts = vec![
            Span::raw("  "),
            Span::styled(row.label.clone(), Style::default().fg(tone)),
        ];
        for part in [row.source.as_str(), row.mode.as_str(), row.facts.as_str()] {
            if !part.is_empty() {
                facts.push(Span::styled(
                    format!(" · {part}"),
                    Style::default().fg(p.quiet),
                ));
            }
        }
        out.push(Line::from(facts));
    }
    if !jobs.notice.is_empty() {
        out.push(Line::default());
        out.extend(wrap(&jobs.notice, Style::default().fg(p.warning), ""));
    }
    out
}

fn log_rows(s: &Snapshot, p: &Palette, width: u16) -> Vec<Line<'static>> {
    s.logs
        .iter()
        .flat_map(|row| {
            crate::transcript::wrap(&[Span::raw(row.clone())], usize::from(width))
                .into_iter()
                .map(|cells| {
                    crate::transcript::line(vec![], cells, None, Style::default().fg(p.text))
                })
        })
        .collect()
}

/// The Logs header: the session facts, wrapped; each line remembers which fact it belongs to.
/// The first fact is the session ID and carries the copy mark.
fn log_header(s: &Snapshot, p: &Palette, width: u16) -> Vec<(usize, Line<'static>)> {
    s.details_panel
        .logs_header
        .iter()
        .enumerate()
        .flat_map(|(entry, (label, value))| {
            crate::transcript::wrap(
                &[Span::styled(
                    format!(
                        "{label:<10}{value}{}",
                        if entry == 0 && label == "Session" {
                            " ⧉"
                        } else {
                            ""
                        }
                    ),
                    Style::default().fg(p.muted),
                )],
                usize::from(width),
            )
            .into_iter()
            .map(move |cells| {
                (
                    entry,
                    crate::transcript::line(vec![], cells, None, Style::default()),
                )
            })
        })
        .collect()
}

// ----- input --------------------------------------------------------------------------

/// What a mouse event did: whether the sidebar consumed it, a bridge action to send and a width
/// to show right now (the action persists it on release).
#[derive(Default)]
pub struct Out {
    pub consumed: bool,
    pub action: Option<Value>,
    pub width: Option<u16>,
}

#[derive(Default)]
pub struct DetailsUi {
    /// The divider is being dragged.
    pub dragging: bool,
    hot: bool,
}

impl DetailsUi {
    /// The divider is under the pointer or being dragged (it paints in the accent colour).
    pub fn hot(&self) -> bool {
        self.dragging || self.hot
    }

    /// Called every loop: drag state lives only while the sidebar is shown.
    pub fn sync(&mut self, s: &Snapshot) {
        if !s.details_sidebar {
            self.dragging = false;
            self.hot = false;
        }
    }

    /// The divider's hover and drag. `area` is the sidebar of the last frame and `total` the
    /// terminal width (for the drag bound). Every other event is left to the caller.
    pub fn mouse(&mut self, ev: MouseEvent, area: Rect, total: u16) -> Out {
        if self.dragging {
            match ev.kind {
                MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Moved => {
                    return Out {
                        consumed: true,
                        width: Some(width_at(ev.column, area.right(), total)),
                        ..Out::default()
                    };
                }
                MouseEventKind::Up(MouseButton::Left) => {
                    self.dragging = false;
                    let width = width_at(ev.column, area.right(), total);
                    return Out {
                        consumed: true,
                        width: Some(width),
                        action: Some(json!({"type":"details_width","width":width})),
                    };
                }
                _ => {}
            }
        }
        let divider = Rect::new(area.x, area.y, 1, area.height);
        let hot = area.width > 0 && divider.contains(Position::new(ev.column, ev.row));
        let changed = hot != self.hot;
        self.hot = hot;
        if hot && matches!(ev.kind, MouseEventKind::Down(MouseButton::Left)) {
            self.dragging = true;
            return Out {
                consumed: true,
                ..Out::default()
            };
        }
        Out {
            consumed: changed,
            ..Out::default()
        }
    }
}

// ----- drawing ------------------------------------------------------------------------

fn hint(s: &Snapshot) -> &'static str {
    match TABS[active(s)].0 {
        "Jobs" if s.details_panel.jobs.log.is_some() => "↑↓ scroll · Esc back · [ ] tab",
        "Jobs" => "↑↓ select · Enter log · s stop · b bg",
        "Files" => "↑↓ move · Enter diff · [ ] tab · Esc close",
        "Logs" => "↑↓ scroll · c copy ID · [ ] tab · Esc close",
        "MCP" => "↑↓ scroll · [ ] tab · Esc close",
        _ => "↑↓ scroll · c copy ID · [ ] tab · Esc close",
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

/// Paints the sidebar into `area`. `rows` are the active tab's rows for the body width of
/// `geometry` (see `Cache::detail_rows`); `hot` colours the divider.
#[allow(clippy::too_many_arguments)]
pub fn draw(
    frame: &mut Frame,
    area: Rect,
    s: &Snapshot,
    p: &Palette,
    rows: &Rows,
    scroll: usize,
    logs_scroll: usize,
    hot: bool,
    mono: bool,
) {
    frame.render_widget(Clear, area);
    let edge = if hot { p.accent } else { p.border_strong };
    frame.render_widget(
        Block::default()
            .borders(Borders::LEFT)
            .border_style(Style::default().fg(edge))
            .style(Style::default().bg(p.panel).fg(p.text)),
        area,
    );
    let Some(g) = geometry(area, s, p) else {
        return;
    };
    let current = active(s);
    for (rect, tab) in &g.tabs {
        let style = if *tab == current {
            let mut style = Style::default()
                .fg(p.text)
                .bg(p.element)
                .add_modifier(Modifier::BOLD);
            if mono {
                style = style.add_modifier(Modifier::UNDERLINED);
            }
            style
        } else {
            Style::default().fg(p.muted)
        };
        frame.render_widget(
            Paragraph::new(Span::styled(format!(" {} ", TABS[*tab].1), style)),
            *rect,
        );
    }
    frame.render_widget(
        Paragraph::new(Line::styled(
            "─".repeat(usize::from(g.rule.width)),
            Style::default().fg(p.border),
        )),
        g.rule,
    );
    frame.render_widget(
        Paragraph::new(Line::styled(
            fit(hint(s), usize::from(g.footer.width)),
            Style::default().fg(p.quiet),
        )),
        g.footer,
    );
    if g.header.height > 0 {
        frame.render_widget(
            Paragraph::new(
                g.header_lines
                    .iter()
                    .map(|(_, line)| line.clone())
                    .collect::<Vec<_>>(),
            ),
            g.header,
        );
    }
    let first = first_row(s, rows.0.len(), g.body.height, scroll, logs_scroll);
    frame.render_widget(
        Paragraph::new(
            rows.0
                .iter()
                .skip(first)
                .take(usize::from(g.body.height))
                .cloned()
                .collect::<Vec<_>>(),
        ),
        g.body,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{backend::TestBackend, Terminal};

    fn snapshot(tab: &str) -> Snapshot {
        serde_json::from_value(json!({
            "details_sidebar": true,
            "logs": ["first log", "second log", "third log"],
            "details_panel": {
                "tab": tab,
                "logs_header": [["Session", "abc123"], ["Daemon pid", "42"]],
                "session": [["Title", "Test"], ["Model", "gpt"]],
                "files": [
                    {"path": "src/main.rs", "added": 3, "removed": 1, "open": true,
                     "diff": ["@@ -1 +1 @@", "-old", "+new"]},
                    {"path": "b.rs", "created": true}
                ],
                "files_summary": "+3 -1 across 2 files",
                "mcp": [["success", "cvc", "4 tools"], ["plain-error", "broken", ""]]
            }
        }))
        .unwrap()
    }

    fn mouse(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    fn text(line: &Line<'_>) -> String {
        line.spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect()
    }

    fn screen(s: &Snapshot, area: Rect, scroll: usize, logs_scroll: usize) -> Vec<String> {
        let p = Palette::new(false);
        let mut terminal = Terminal::new(TestBackend::new(area.right(), area.bottom())).unwrap();
        terminal
            .draw(|frame| {
                let width = geometry(area, s, &p).map_or(0, |g| g.body.width);
                let rows = rows(s, &p, width);
                draw(frame, area, s, &p, &rows, scroll, logs_scroll, false, false);
            })
            .unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..area.bottom())
            .map(|y| {
                (area.x..area.right())
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect()
            })
            .collect()
    }

    #[test]
    fn width_is_bounded_and_the_drawer_keeps_its_old_semantics() {
        let mut s = snapshot("Session");
        assert_eq!(width_for(&s, 200), 40, "no stored width uses the default");
        s.details_panel.width = 5;
        assert_eq!(width_for(&s, 200), MIN_WIDTH);
        s.details_panel.width = 500;
        assert_eq!(width_for(&s, 200), MAX_WIDTH);
        s.details_panel.width = 0;
        assert_eq!(width_for(&s, 100), 40);
        assert_eq!(width_for(&s, 80), 40, "the drawer is 40 wide at 80 columns");
        assert_eq!(
            width_for(&s, 50),
            26,
            "and leaves the conversation 24 columns"
        );
        for total in 0..=240u16 {
            for wanted in [0, 24, 40, 60] {
                s.details_panel.width = wanted;
                let width = width_for(&s, total);
                assert!(width <= total, "{total}");
                if total >= DRAWER_BELOW {
                    assert!(
                        total - width >= MIN_CONVERSATION,
                        "{total}/{wanted}: {width}"
                    );
                    assert!(width >= MIN_WIDTH);
                } else {
                    assert!(total - width >= DRAWER_CONVERSATION.min(total), "{total}");
                }
            }
        }
        s.details_sidebar = false;
        assert_eq!(width_for(&s, 200), 0, "closed takes no columns");
    }

    #[test]
    fn dragging_is_bounded_and_the_divider_is_the_first_column() {
        assert_eq!(width_at(160, 200, 200), 40);
        assert_eq!(width_at(199, 200, 200), MIN_WIDTH);
        assert_eq!(width_at(0, 200, 200), MAX_WIDTH);
        assert_eq!(width_at(0, 100, 100), 100 - MIN_CONVERSATION);
        assert_eq!(
            width_at(0, 80, 80),
            56,
            "a drawer may grow while 24 columns remain"
        );
        assert_eq!(width_at(0, 20, 20), MIN_WIDTH, "never below the minimum");
    }

    #[test]
    fn the_divider_hovers_drags_and_persists_on_release() {
        let area = Rect::new(160, 0, 40, 30);
        let mut ui = DetailsUi::default();
        let out = ui.mouse(mouse(MouseEventKind::Moved, 160, 5), area, 200);
        assert!(out.consumed && ui.hot() && out.action.is_none());
        let out = ui.mouse(mouse(MouseEventKind::Moved, 170, 5), area, 200);
        assert!(
            out.consumed && !ui.hot(),
            "leaving the divider redraws once"
        );
        let out = ui.mouse(mouse(MouseEventKind::Moved, 171, 5), area, 200);
        assert!(!out.consumed, "plain movement is left to the caller");
        let out = ui.mouse(
            mouse(MouseEventKind::Down(MouseButton::Left), 170, 5),
            area,
            200,
        );
        assert!(
            !out.consumed && !ui.dragging,
            "only the divider starts a drag"
        );
        ui.mouse(
            mouse(MouseEventKind::Down(MouseButton::Left), 160, 5),
            area,
            200,
        );
        assert!(ui.dragging);
        let out = ui.mouse(
            mouse(MouseEventKind::Drag(MouseButton::Left), 150, 5),
            area,
            200,
        );
        assert_eq!((out.consumed, out.width), (true, Some(50)));
        assert!(out.action.is_none(), "nothing is persisted mid-drag");
        let out = ui.mouse(
            mouse(MouseEventKind::Drag(MouseButton::Left), 5, 5),
            area,
            200,
        );
        assert_eq!(out.width, Some(MAX_WIDTH));
        let out = ui.mouse(
            mouse(MouseEventKind::Up(MouseButton::Left), 145, 5),
            area,
            200,
        );
        assert!(!ui.dragging);
        assert_eq!(out.width, Some(55));
        assert_eq!(
            out.action,
            Some(json!({"type":"details_width","width":55})),
            "release persists the width"
        );
    }

    #[test]
    fn tab_labels_wrap_to_fit_and_never_overlap() {
        for inner in 10..=60u16 {
            let (cells, rows) = tab_cells(inner);
            assert_eq!(cells.len(), TABS.len());
            assert_eq!(rows, cells.iter().map(|c| c.row).max().unwrap() + 1);
            for cell in &cells {
                assert!(
                    cell.x + cell.width <= inner,
                    "{inner}: {cell:?} past the edge"
                );
                assert_eq!(cell.width, TABS[cell.tab].1.width() as u16 + 2, "{inner}");
            }
            for a in &cells {
                for b in &cells {
                    if a.tab < b.tab && a.row == b.row {
                        assert!(a.x + a.width < b.x, "{inner}: {a:?} touches {b:?}");
                    }
                }
            }
        }
        assert_eq!(tab_cells(60).1, 1, "seven tabs fit at 60 inner columns");
        assert_eq!(tab_cells(38).1, 2, "and wrap at the default width");
        assert!(tab_cells(24).1 >= 2, "and wrap at the minimum");
    }

    #[test]
    fn tabs_paint_where_they_are_hit_tested_at_every_width() {
        for width in [24u16, 30, 40, 60] {
            let area = Rect::new(7, 0, width, 24);
            let s = snapshot("Session");
            let lines = screen(&s, area, 0, 0);
            for (rect, tab) in tab_rects(area) {
                let painted: String = lines[usize::from(rect.y)]
                    .chars()
                    .skip(usize::from(rect.x - area.x))
                    .take(usize::from(rect.width))
                    .collect();
                assert_eq!(painted, format!(" {} ", TABS[tab].1), "{width}");
                assert_eq!(tab_at(area, rect.x, rect.y), Some(TABS[tab].0));
                assert_eq!(tab_at(area, rect.right() - 1, rect.y), Some(TABS[tab].0));
            }
            assert_eq!(tab_at(area, area.x, 1), None, "the divider is not a tab");
        }
        let area = Rect::new(0, 0, 24, 24);
        assert_eq!(
            tab_rects(area).iter().map(|(r, _)| r.y).max(),
            Some(3),
            "wrapped"
        );
    }

    #[test]
    fn each_tab_shows_only_its_own_rows() {
        let p = Palette::new(false);
        let all = |tab: &str| -> Vec<String> {
            rows(&snapshot(tab), &p, 36).0.iter().map(text).collect()
        };
        let session = all("Session");
        assert_eq!(session[0], "SESSION METADATA");
        assert!(session.iter().any(|row| row.contains("Title:")));
        assert!(!session
            .iter()
            .any(|row| row.contains("CHANGES") || row.contains("src/main.rs")));
        assert!(!session.iter().any(|row| row.contains("MCP SERVERS")));

        let changes = all("Files");
        assert_eq!(changes[0], "CHANGES · 2 files");
        assert!(changes.iter().any(|row| row.contains("src/main.rs")));
        assert!(!changes
            .iter()
            .any(|row| row.contains("Title:") || row.contains("MCP")));
        assert_eq!(
            rows(&snapshot("Changes"), &p, 36).0.len(),
            changes.len(),
            "alias"
        );

        let mcp = all("MCP");
        assert_eq!(mcp[0], "MCP SERVERS");
        assert!(mcp
            .iter()
            .any(|row| row.contains("cvc") && row.contains("4 tools")));
        assert!(mcp.iter().any(|row| row == "broken"));
        assert!(!mcp
            .iter()
            .any(|row| row.contains("CHANGES") || row.contains("Title:")));

        let logs = all("Logs");
        assert_eq!(logs, ["first log", "second log", "third log"]);

        let empty = rows(&Snapshot::default(), &p, 36);
        assert_eq!(
            text(&empty.0[0]),
            "SESSION METADATA",
            "an unknown tab is Session"
        );
        let (rows_empty, hits) = changes_rows(&Snapshot::default(), &p);
        assert!(rows_empty
            .iter()
            .any(|row| text(row) == "No files changed yet."));
        assert!(hits.iter().all(Option::is_none));
    }

    fn jobs_snapshot(log: Value) -> Snapshot {
        serde_json::from_value(json!({
            "details_sidebar": true,
            "details_panel": {"tab": "Jobs", "jobs": {
                "loaded": true, "running": 1, "notice": "Stopped the job.",
                "rows": [
                    {"id": "j1", "command": "pytest -q", "status": "running", "label": "running",
                     "source": "agent", "mode": "foreground", "facts": "5s",
                     "log": true, "stop": true, "background": true, "selected": true},
                    {"id": "j2", "command": "make build", "status": "failed", "label": "failed",
                     "source": "you", "facts": "2s · exit 2"}
                ],
                "log": log
            }}
        }))
        .unwrap()
    }

    #[test]
    fn jobs_list_shows_state_selection_and_never_raw_json() {
        let p = Palette::new(false);
        let s = jobs_snapshot(Value::Null);
        let rows: Vec<String> = rows(&s, &p, 36).0.iter().map(text).collect();
        assert_eq!(rows[0], "JOBS · 1 running");
        assert!(rows.iter().any(|row| row == "▸ pytest -q"), "{rows:?}");
        assert!(rows.iter().any(|row| row == "  make build"));
        assert!(rows
            .iter()
            .any(|row| row == "  running · agent · foreground · 5s"));
        assert!(rows.iter().any(|row| row == "  failed · you · 2s · exit 2"));
        assert!(rows.iter().any(|row| row == "Stopped the job."));
        assert!(!rows
            .iter()
            .any(|row| row.contains('{') || row.contains("\"id\"")));
        assert_eq!(selected_job(&s).map(|job| job.id.as_str()), Some("j1"));
        assert_eq!(neighbour_job(&s, 1), Some("j2"));
        assert_eq!(neighbour_job(&s, -1), Some("j1"), "clamped at the top");
        assert_eq!(neighbour_job(&s, 9), Some("j2"), "clamped at the bottom");
        let empty = Snapshot::default();
        assert_eq!(neighbour_job(&empty, 1), None);
        let mut idle = jobs_snapshot(Value::Null);
        idle.details_panel.jobs.rows.clear();
        let rows: Vec<String> = rows_text(&idle, &p);
        assert!(rows
            .iter()
            .any(|row| row == "No shell jobs in this session."));
        idle.details_panel.jobs.loaded = false;
        assert!(rows_text(&idle, &p)
            .iter()
            .any(|row| row == "Loading jobs…"));
    }

    fn rows_text(s: &Snapshot, p: &Palette) -> Vec<String> {
        rows(s, p, 36).0.iter().map(text).collect()
    }

    #[test]
    fn the_job_log_viewer_shows_a_bounded_tail_or_the_reason_it_cannot() {
        let p = Palette::new(false);
        let ok = jobs_snapshot(json!({"id": "j1", "status": "ok", "clipped": true,
                                      "lines": ["one", "", "two"]}));
        let rows = rows_text(&ok, &p);
        assert_eq!(rows[0], "JOB LOG");
        assert_eq!(rows[1], "pytest -q");
        assert!(rows.iter().any(|row| row == "… earlier output not shown"));
        assert_eq!(&rows[rows.len() - 3..], ["one", "", "two"]);
        assert_eq!(hint(&ok), "↑↓ scroll · Esc back · [ ] tab");
        let gone =
            jobs_snapshot(json!({"id": "j1", "status": "unavailable", "message": "Log removed."}));
        assert!(rows_text(&gone, &p).iter().any(|row| row == "Log removed."));
        let loading = jobs_snapshot(json!({"id": "j1", "status": "loading"}));
        assert!(rows_text(&loading, &p)
            .iter()
            .any(|row| row == "Loading log…"));
        assert!(hint(&jobs_snapshot(Value::Null)).contains("Enter log"));
    }

    #[test]
    fn changes_hit_rows_follow_expansion_and_the_drawn_scroll() {
        let p = Palette::new(false);
        let s = snapshot("Files");
        let (lines, hits) = changes_rows(&s, &p);
        assert_eq!(lines.len(), hits.len());
        let first = hits.iter().position(|hit| *hit == Some(0)).unwrap();
        assert!(text(&lines[first]).starts_with("▾ M ") && text(&lines[first]).contains("main.rs"));
        assert_eq!(text(&lines[first + 1]), "  @@ -1 +1 @@");
        assert_eq!(hits[first + 1], None, "diff rows do not toggle");
        let second = hits.iter().position(|hit| *hit == Some(1)).unwrap();
        assert!(text(&lines[second]).starts_with("▸ A "));
        assert_eq!(second, first + 4);

        // Whatever the scroll, the row hit at a position is the file drawn there.
        let area = Rect::new(60, 0, 40, 12);
        let rows = rows(&s, &p, geometry(area, &s, &p).unwrap().body.width);
        let g = geometry(area, &s, &p).unwrap();
        for scroll in [0usize, 1, 2, 5, 40] {
            let drawn = screen(&s, area, scroll, 0);
            for y in g.body.y..g.body.bottom() {
                let hit = file_at(area, &s, &p, &rows, scroll, 0, g.body.x, y);
                let row = drawn[usize::from(y)].trim_end();
                assert_eq!(
                    row.contains("main.rs"),
                    hit == Some(0),
                    "scroll {scroll} row {y}: {row}"
                );
                assert_eq!(
                    row.contains("b.rs"),
                    hit == Some(1),
                    "scroll {scroll} row {y}: {row}"
                );
            }
        }
        assert_eq!(
            file_at(area, &s, &p, &rows, 0, 0, area.x, g.body.y),
            None,
            "the divider"
        );
        let other = snapshot("Session");
        assert_eq!(
            file_at(area, &other, &p, &rows, 0, 0, g.body.x, g.body.y + 1),
            None
        );
    }

    #[test]
    fn logs_copy_target_and_tail_agree_with_the_wrapped_tabs() {
        let p = Palette::new(false);
        let s = snapshot("Logs");
        for width in [24u16, 40] {
            let area = Rect::new(20, 0, width, 20);
            let g = geometry(area, &s, &p).unwrap();
            assert_eq!(
                g.header.y,
                g.rule.y + 1,
                "the header sits right under the rule"
            );
            let drawn = screen(&s, area, 0, 0);
            let session_row = &drawn[usize::from(g.header.y)];
            assert!(
                session_row.contains("Session") && session_row.contains("⧉"),
                "{session_row}"
            );
            assert!(copy_id_at(area, &s, &p, g.header.x, g.header.y));
            assert!(
                !copy_id_at(area, &s, &p, g.header.x, g.header.y + 1),
                "Daemon pid is not the ID"
            );
            assert!(!copy_id_at(area, &s, &p, g.header.x, g.rule.y));
            let tail: Vec<&String> = drawn[usize::from(g.body.y)..usize::from(g.body.bottom())]
                .iter()
                .filter(|row| row.contains("log"))
                .collect();
            assert_eq!(tail.len(), 3, "the tail shows every line when it fits");
        }
        assert!(!copy_id_at(
            Rect::new(0, 0, 40, 20),
            &snapshot("Session"),
            &p,
            1,
            4
        ));
        // Scrolling back reveals earlier lines; the tail follows by default.
        let rows = vec![Line::raw("a"); 30];
        assert_eq!(first_row(&s, rows.len(), 10, 0, 0), 20);
        assert_eq!(first_row(&s, rows.len(), 10, 0, 7), 13);
        assert_eq!(first_row(&s, rows.len(), 10, 0, 99), 0);
        assert_eq!(
            first_row(&snapshot("Session"), 30, 10, 99, 0),
            20,
            "clamped"
        );
    }

    fn open_left(width: u16) -> Snapshot {
        let mut s = snapshot("Session");
        s.sidebar.available = true;
        s.sidebar.open = true;
        s.sidebar.tab = "Sessions".into();
        s.sidebar.width = width;
        s
    }

    #[test]
    fn both_sidebars_never_overlap_the_conversation_or_each_other() {
        for left in [false, true] {
            for wanted in [0u16, 24, 40, 60] {
                for total in 20..=240u16 {
                    let mut s = open_left(60);
                    s.sidebar.open = left;
                    s.details_panel.width = wanted;
                    let r = crate::render::regions(Rect::new(0, 0, total, 40), &s, 8, false);
                    if r.details.width > 0 {
                        assert_eq!(r.details.right(), total, "{total}: right edge");
                        assert_eq!(r.details.width, width_for(&s, total), "{total}");
                        assert!(
                            r.transcript.right() <= r.details.x,
                            "{total}: under the sidebar"
                        );
                    }
                    if r.sidebar.width > 0 {
                        assert_eq!(
                            r.sidebar,
                            crate::sidebar::selector_area(Rect::new(0, 0, total, 40))
                        );
                    }
                    assert_eq!(
                        crate::render::sidebar_available(Rect::new(0, 0, total, 40), &s),
                        total - r.details.width,
                        "{total}: the left drag bound starts left of the sidebar"
                    );
                }
            }
        }
        let mut s = open_left(34);
        s.details_panel.width = 52;
        let r = crate::render::regions(Rect::new(0, 0, 200, 40), &s, 8, false);
        assert_eq!((r.sidebar.width, r.details.width), (90, 52));
        assert_eq!(r.transcript.width, 200 - 52);
        let small = crate::render::regions(Rect::new(0, 0, 80, 24), &s, 8, false);
        assert_eq!(
            small.sidebar.width, 76,
            "the selector remains an overlay in drawer layout"
        );
        // Below DRAWER_BELOW the requested 52 is honoured, leaving DRAWER_CONVERSATION or more.
        assert_eq!((small.details.width, small.transcript.width), (52, 28));
        assert!(small.transcript.width >= DRAWER_CONVERSATION);
        s.details_panel.width = 60;
        let tight = crate::render::regions(Rect::new(0, 0, 80, 24), &s, 8, false);
        assert_eq!(
            (tight.details.width, tight.transcript.width),
            (56, 24),
            "bounded by the drawer minimum"
        );
    }

    #[test]
    fn a_resized_sidebar_moves_the_conversation_and_hit_regions_with_it() {
        let mut s = snapshot("Files");
        s.details_panel.width = 56;
        let r = crate::render::regions(Rect::new(0, 0, 160, 40), &s, 8, false);
        assert_eq!(
            (r.details.x, r.details.width, r.transcript.width),
            (104, 56, 104)
        );
        let p = Palette::new(false);
        let g = geometry(r.details, &s, &p).unwrap();
        assert!(g.body.x > r.details.x && g.body.right() < r.details.right());
        for (rect, _) in &g.tabs {
            assert!(rect.x > r.details.x && rect.right() < r.details.right());
        }
    }
}
