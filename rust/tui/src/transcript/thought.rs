//! Reasoning rows (GROK_BUILD_TUI_PLAN §4.2).
//!
//! Running: `▾ Thinking… (2.4s)` — always that label, never the thought's own title, and
//! never animated — with a tail of the last [`TAIL_ROWS`] wrapped rows behind a heavy `┃` rail. Finished:
//! `▸ Thought for 3.2s`; opened, the whole trace sits behind a dim `│` rail. No amber, no
//! italic title, and no rail before the header.
use super::{line, look, wrap, Rows};
use crate::{bridge::Content, render::Palette};
use ratatui::{
    style::{Modifier, Style},
    text::Span,
};
use serde_json::Value;
use unicode_width::UnicodeWidthStr;

/// Wrapped rows of a running thought that stay visible.
pub const TAIL_ROWS: usize = 5;

/// The reasoning text: what the host opened, else the local copy.
fn body(b: &Content) -> &str {
    if b.detail.is_empty() {
        &b.local_detail
    } else {
        &b.detail
    }
}

/// `**heading**` on a whole line: the heading text.
fn heading(text: &str) -> Option<&str> {
    let trimmed = text.trim();
    (trimmed.len() > 4
        && trimmed.starts_with("**")
        && trimmed.ends_with("**")
        && !trimmed[2..trimmed.len() - 2].contains("**"))
    .then(|| &trimmed[2..trimmed.len() - 2])
}

/// Every wrapped row of the reasoning as styled cell rows.
fn wrapped(text: &str, room: usize, p: &Palette) -> Vec<Vec<super::Cell>> {
    let mut rows = Vec::new();
    for raw in text.lines() {
        let (shown, style) = match heading(raw) {
            Some(title) => (
                title.to_string(),
                Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
            ),
            None => (
                raw.replace("**", ""),
                Style::default().fg(p.quiet).add_modifier(Modifier::ITALIC),
            ),
        };
        rows.extend(wrap(&[Span::styled(shown, style)], room));
    }
    rows
}

/// The reasoning behind `lead`, all of it (an opened thought).
pub fn trace(out: &mut Rows, b: &Content, lead: &[Span<'static>], width: usize, p: &Palette) {
    let lead_width: usize = lead.iter().map(|s| s.content.width()).sum();
    for cells in wrapped(body(b), width.saturating_sub(lead_width).max(1), p) {
        out.push((
            line(lead.to_vec(), cells, None, Style::default()),
            b.operation.clone(),
        ));
    }
}

/// `▸ Thought for 3.2s` / `▾ …`, or the running header.
pub fn draw(out: &mut Rows, b: &Content, width: usize, p: &Palette, now_ms: u64) {
    let op: &Option<Value> = &b.operation;
    let glyphs = look::glyphs();
    if b.status == "running" {
        // No spinner or shimmer: thinking is a static, quiet row.
        let mut head = vec![
            Span::raw("    "),
            Span::styled(glyphs.open, Style::default().fg(p.quiet)),
            Span::raw(" "),
            Span::styled(
                "Thinking…",
                Style::default().fg(p.muted).add_modifier(Modifier::ITALIC),
            ),
        ];
        if let Some(elapsed) = look::live_elapsed(b.started_ms, now_ms) {
            head.push(Span::styled(
                format!(" ({elapsed})"),
                Style::default().fg(p.quiet),
            ));
        }
        out.push((ratatui::text::Line::from(head), op.clone()));
        let room = width.saturating_sub(6).max(1);
        let rows = wrapped(body(b), room, p);
        let hidden = rows.len().saturating_sub(TAIL_ROWS);
        if hidden > 0 {
            out.push((
                ratatui::text::Line::from(vec![
                    Span::raw("    "),
                    Span::styled(glyphs.rail, Style::default().fg(p.border)),
                    Span::styled(
                        format!(
                            " … {hidden} earlier line{}",
                            if hidden == 1 { "" } else { "s" }
                        ),
                        Style::default().fg(p.quiet),
                    ),
                ]),
                op.clone(),
            ));
        }
        for cells in rows.into_iter().skip(hidden) {
            let lead = vec![
                Span::raw("    "),
                Span::styled(glyphs.rail_heavy, Style::default().fg(p.border_strong)),
                Span::raw(" "),
            ];
            out.push((line(lead, cells, None, Style::default()), op.clone()));
        }
        return;
    }
    let opened = !body(b).is_empty();
    let (word, rest) = b.title.split_once(' ').unwrap_or((b.title.as_str(), ""));
    let mut head = vec![
        Span::raw("    "),
        Span::styled(
            if opened { glyphs.open } else { glyphs.closed },
            Style::default().fg(p.quiet),
        ),
        Span::raw(" "),
        Span::styled(
            word.to_string(),
            Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
        ),
    ];
    if !rest.is_empty() {
        head.push(Span::styled(
            format!(" {rest}"),
            Style::default().fg(p.quiet),
        ));
    }
    out.push((ratatui::text::Line::from(head), op.clone()));
    if opened {
        let lead = vec![
            Span::raw("    "),
            Span::styled(glyphs.rail, Style::default().fg(p.border)),
            Span::raw(" "),
        ];
        trace(out, b, &lead, width, p);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(rows: &Rows) -> Vec<String> {
        rows.iter().map(|(row, _)| row.to_string()).collect()
    }

    #[test]
    fn finished_thought_is_one_quiet_header_until_opened() {
        let p = Palette::new(false);
        let mut block = Content {
            kind: "thought".into(),
            title: "Thought for 3.2s".into(),
            text: "Exploring · 3 lines ▸".into(),
            status: "completed".into(),
            ..Default::default()
        };
        let mut rows = Vec::new();
        draw(&mut rows, &block, 60, &p, 0);
        assert_eq!(text(&rows), ["    ▸ Thought for 3.2s"]);
        assert_eq!(rows[0].0.spans[3].style.fg, Some(p.muted));
        assert_eq!(rows[0].0.spans[4].style.fg, Some(p.quiet));
        block.detail = "**Exploring setup**\nneed to check".into();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 60, &p, 0);
        let lines = text(&rows);
        assert_eq!(lines[0], "    ▾ Thought for 3.2s");
        assert_eq!(lines[1], "    │ Exploring setup");
        assert_eq!(lines[2], "    │ need to check");
        assert!(lines.iter().all(|l| !l.contains("**")));
        let italic = rows[2].0.spans.last().unwrap();
        assert!(italic.style.add_modifier.contains(Modifier::ITALIC));
        assert_ne!(italic.style.fg, Some(p.warning));
    }

    #[test]
    fn running_thought_says_thinking_with_elapsed_and_a_five_row_tail() {
        let p = Palette::new(false);
        let block = Content {
            kind: "thought".into(),
            title: "Thought for 9s".into(),
            status: "running".into(),
            started_ms: 10_000,
            detail: (1..=12)
                .map(|n| format!("step {n}"))
                .collect::<Vec<_>>()
                .join("\n"),
            ..Default::default()
        };
        let mut rows = Vec::new();
        draw(&mut rows, &block, 60, &p, 12_400);
        let lines = text(&rows);
        assert_eq!(lines[0], "    ▾ Thinking… (2.4s)");
        assert!(!lines[0].contains("Thought"));
        assert_eq!(lines[1], "    │ … 7 earlier lines");
        assert_eq!(lines.len(), 2 + TAIL_ROWS);
        assert_eq!(lines.last().unwrap(), "    ┃ step 12");
        assert!(
            rows.iter()
                .all(|(row, _)| row.spans.iter().all(|s| !look::is_live(s))),
            "thinking never animates"
        );
    }

    #[test]
    fn short_running_thought_announces_nothing_and_unknown_start_hides_time() {
        let p = Palette::new(false);
        let block = Content {
            kind: "thought".into(),
            status: "running".into(),
            detail: "one\ntwo".into(),
            ..Default::default()
        };
        let mut rows = Vec::new();
        draw(&mut rows, &block, 60, &p, 5_000);
        let lines = text(&rows);
        assert_eq!(lines.len(), 3);
        assert!(!lines[0].contains('('));
        assert!(lines.iter().all(|l| !l.contains("earlier")));
    }
}
