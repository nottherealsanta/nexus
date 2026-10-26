//! The Manager screen's managed-sessions board (plan 10 §4.1): one shared layout for
//! drawing and hit-testing, the pattern of `chrome::agent_layout`/`top_layout`.
//!
//! Python owns the rows (`nexus/ui/ratatui/manager_board.py`); this module only places
//! and paints them. The board sits between the top bar and the transcript while the
//! manager is in front: a collapsible heading plus one row per managed root, newest
//! first, bounded to `BOARD_LIMIT` rows with `+N more · /sessions`. A row click sends
//! its `session_open` operation (the ordinary switch; `Ctrl+X .` returns); a heading
//! click sends `manager_board_toggle`.
use super::*;
use crate::bridge::{ManagerBoard, Snapshot};
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
};
use serde_json::Value;

/// Rows drawn before `+N more · /sessions` (mirrors `manager_board.BOARD_LIMIT`;
/// kept beside `bridge::BOARD_ROWS`, which the layout height uses).
pub const BOARD_LIMIT: usize = crate::bridge::BOARD_ROWS;

/// One laid-out board row: where it is drawn and what a click on it sends.
pub struct BoardHit {
    pub y: u16,
    pub x0: u16,
    pub x1: u16,
    pub operation: Option<Value>,
}

/// Heading plus drawn rows for `board` in `area`: the shared layout behind drawing
/// (`draw_board`) and pointer hit-testing (`hit`).
pub fn layout(board: &ManagerBoard, area: Rect) -> Vec<BoardHit> {
    let mut out = Vec::new();
    if area.height == 0 || area.width == 0 {
        return out;
    }
    out.push(BoardHit {
        y: area.y,
        x0: area.x,
        x1: area.right(),
        operation: board.toggle.clone(),
    });
    if board.collapsed {
        return out;
    }
    for (index, row) in board.rows.iter().take(BOARD_LIMIT).enumerate() {
        let y = area.y + 1 + index as u16;
        if y >= area.bottom() {
            break;
        }
        out.push(BoardHit {
            y,
            x0: area.x,
            x1: area.right(),
            operation: row.operation.clone(),
        });
    }
    out
}

/// `Some(operation)` when `(x, y)` is on a board row or the heading, else `None`.
pub fn hit(board: &ManagerBoard, area: Rect, x: u16, y: u16) -> Option<Value> {
    layout(board, area)
        .into_iter()
        .find(|hit| hit.y == y && x >= hit.x0 && x < hit.x1)
        .and_then(|hit| hit.operation)
}

fn status_glyph(status: &str) -> &'static str {
    // The plan's board mock: `● working`, `✓ done`, `○ idle`; attention reads `⚑ needs you`.
    let ascii = crate::transcript::look::glyphs().ascii;
    let (plain, ascii_mark) = match status {
        "working" => ("●", "*"),
        "input" | "done" => ("✓", "v"),
        _ => ("○", "o"),
    };
    if ascii {
        ascii_mark
    } else {
        plain
    }
}

/// `Ns`/`Nm`/`Nh`/`Nd` age of the board's newest row, or `updating…` before the first join.
fn board_age(board: &ManagerBoard) -> String {
    if board.updated_at <= 0.0 {
        return "updating…".into();
    }
    let seconds = (crate::live_bar::now_epoch() - board.updated_at).max(0.0) as u64;
    if seconds < 45 {
        "just now".into()
    } else if seconds < 3600 {
        format!("{}m ago", seconds / 60)
    } else if seconds < 86400 {
        format!("{}h ago", seconds / 3600)
    } else {
        format!("{}d ago", seconds / 86400)
    }
}

pub(super) fn draw_board(
    frame: &mut Frame,
    s: &Snapshot,
    area: Rect,
    p: &Palette,
    focus: Option<usize>,
) {
    let board = match s.manager_board.as_ref() {
        Some(board) if s.manager.active => board,
        _ => return,
    };
    let background = Style::default().bg(p.background);
    let Some(head) = layout(board, area).first().map(|hit| hit.y) else {
        return;
    };
    let width = usize::from(area.width);
    let chevron = if board.collapsed { "▸" } else { "▾" };
    let mut title = format!(
        "{} Managed sessions ({}) · {}",
        chevron,
        board.total,
        board_age(board)
    );
    if board.missing > 0 {
        title.push_str(&format!(" · {} updating", board.missing));
    }
    frame.render_widget(
        Paragraph::new(Line::from(vec![Span::styled(
            crate::transcript::truncate(&title, width),
            background.fg(p.text).add_modifier(
                Modifier::BOLD
                    | if focus == Some(0) {
                        Modifier::REVERSED
                    } else {
                        Modifier::empty()
                    },
            ),
        )]))
        .style(background),
        Rect { height: 1, ..area },
    );
    if board.collapsed {
        return;
    }
    for (index, row) in board.rows.iter().take(BOARD_LIMIT).enumerate() {
        let y = head + 1 + index as u16;
        if y >= area.bottom() {
            break;
        }
        let mut text = format!("  {} {}", status_glyph(&row.status), row.title);
        let meta = [
            row.status.clone(),
            row.age.clone(),
            if row.attention.is_empty() {
                String::new()
            } else {
                format!(
                    "{} {}",
                    if crate::transcript::look::glyphs().ascii {
                        "!"
                    } else {
                        "⚑"
                    },
                    row.attention
                )
            },
        ]
        .into_iter()
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("  ");
        if !meta.is_empty() {
            text.push_str(&format!("   {meta}"));
        }
        if !row.sub.is_empty() {
            text.push_str(&format!(" · {}", row.sub));
        }
        let attention = !row.attention.is_empty();
        frame.render_widget(
            Paragraph::new(Line::from(vec![Span::styled(
                crate::transcript::truncate(&text, width),
                background
                    .fg(if attention { p.warning } else { p.text })
                    .add_modifier(if focus == Some(index + 1) {
                        Modifier::REVERSED
                    } else {
                        Modifier::empty()
                    }),
            )]))
            .style(background),
            Rect {
                y,
                height: 1,
                ..area
            },
        );
    }
    let drawn = board.rows.len().min(BOARD_LIMIT);
    if board.more > 0 {
        let y = head + 1 + drawn as u16;
        if y < area.bottom() {
            frame.render_widget(
                Paragraph::new(Line::from(vec![Span::styled(
                    crate::transcript::truncate(
                        &format!("  +{} more · /sessions", board.more),
                        width,
                    ),
                    background.fg(p.quiet),
                )]))
                .style(background),
                Rect {
                    y,
                    height: 1,
                    ..area
                },
            );
        }
    }
}

#[cfg(test)]
mod board_tests {
    use super::*;
    use crate::bridge::{ManagerBoard, ManagerBoardRow};
    use unicode_width::UnicodeWidthStr;

    fn board(rows: usize, more: u64, collapsed: bool) -> ManagerBoard {
        ManagerBoard {
            total: rows as u64 + more,
            collapsed,
            updated_at: 1.0,
            toggle: Some(serde_json::json!({"kind": "manager_board_toggle"})),
            rows: (0..rows)
                .map(|i| ManagerBoardRow {
                    id: format!("s{i}"),
                    title: format!("Session {i}"),
                    status: if i == 0 { "working".into() } else { "idle".into() },
                    sub: "2 messages".into(),
                    age: "2m".into(),
                    attention: if i == 1 { "needs you".into() } else { String::new() },
                    operation: Some(
                        serde_json::json!({"kind": "session_open", "workspace": "/w", "id": format!("s{i}")}),
                    ),
                })
                .collect(),
            more,
            missing: 0,
        }
    }

    fn paint(board: &ManagerBoard, width: u16, height: u16) -> Vec<String> {
        let area = Rect::new(0, 0, width, height);
        let backend = ratatui::backend::TestBackend::new(width, height);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        let mut s = Snapshot::default();
        s.manager.active = true;
        s.manager_board = Some(board.clone());
        terminal
            .draw(|frame| draw_board(frame, &s, area, &Palette::new(false), None))
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..height)
            .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
            .collect()
    }

    #[test]
    fn heading_rows_and_overflow_draw_and_hit_the_same_cells() {
        let full = board(8, 2, false);
        let area = Rect::new(4, 2, 100, 8);
        let hits = layout(&full, area);
        // Heading plus the six drawn rows; the overflow row carries no operation.
        assert_eq!(hits.len(), 1 + BOARD_LIMIT);
        assert_eq!(hits[0].y, 2);
        assert_eq!(hits[0].x0, 4);
        assert_eq!(hits[0].x1, 104);
        for (i, hit) in hits.iter().enumerate().skip(1) {
            assert_eq!(hit.y, 3 + (i - 1) as u16);
        }
        // Hit-testing is the layout: every drawn row resolves its own open operation.
        for i in 0..BOARD_LIMIT {
            let op = hit(&full, area, 50, 3 + i as u16).unwrap();
            assert_eq!(op["id"], format!("s{i}"));
        }
        assert_eq!(
            hit(&full, area, 50, 2).unwrap()["kind"],
            "manager_board_toggle"
        );
        assert!(hit(&full, area, 50, 9).is_none());
        let lines = paint(&full, 100, 8);
        assert!(lines[0].contains("Managed sessions (10)"), "{lines:?}");
        assert!(lines[0].contains('▾'), "{lines:?}");
        assert!(
            lines[1].contains("fix") || lines[1].contains("Session 0"),
            "{lines:?}"
        );
        assert!(lines[1].contains('●'), "{lines:?}");
        assert!(lines[2].contains("needs you"), "{lines:?}");
        assert!(lines[7].contains("+2 more · /sessions"), "{lines:?}");
    }

    #[test]
    fn collapsed_board_is_one_row_and_still_toggles() {
        let small = board(3, 0, true);
        let area = Rect::new(0, 0, 100, 1);
        let hits = layout(&small, area);
        assert_eq!(hits.len(), 1);
        assert_eq!(
            hit(&small, area, 10, 0).unwrap()["kind"],
            "manager_board_toggle"
        );
        let lines = paint(&small, 100, 1);
        assert!(lines[0].contains('▸'), "{lines:?}");
        assert!(lines[0].contains("Managed sessions (3)"), "{lines:?}");
    }

    #[test]
    fn narrow_and_ascii_boards_clip_without_panicking() {
        let full = board(8, 2, false);
        for width in [10u16, 20, 40] {
            let lines = paint(&full, width, 8);
            assert!(lines[0].contains("Managed"), "{lines:?}");
            for line in &lines {
                assert!(line.width() <= width as usize, "{line:?}");
            }
        }
        crate::transcript::look::glyphs();
        let lines = paint(&full, 100, 8);
        assert!(lines[1].chars().any(|c| c == '●' || c == '*'), "{lines:?}");
    }

    #[test]
    fn board_age_labels_staleness() {
        let mut fresh = board(1, 0, false);
        fresh.updated_at = crate::live_bar::now_epoch();
        assert_eq!(board_age(&fresh), "just now");
        fresh.updated_at = 0.0;
        assert_eq!(board_age(&fresh), "updating…");
        fresh.updated_at = crate::live_bar::now_epoch() - 3600.0 * 26.0;
        assert_eq!(board_age(&fresh), "1d ago");
    }
}
