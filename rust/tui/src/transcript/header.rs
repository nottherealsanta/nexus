//! Context header rows (GROK_BUILD_TUI_PLAN §8B/§8C): grey, never the agent colour.
//!
//! An active header is `muted` with a bold title and a `quiet` token figure; an inactive
//! one (nothing to send) is `quiet` with a plain title and a darker figure. The `│` rail
//! shares the title's tone. The closed one-row form lives in `render/context.rs`; this is
//! the open form and the single-block chip.
use super::{indented, Rows};
use crate::{bridge::Content, render::Palette};
use ratatui::{
    style::{Modifier, Style},
    text::{Line, Span},
};
use unicode_width::UnicodeWidthStr;

/// Title and rail tone, and the darker tone of the figure beside it.
pub fn tones(active: bool, p: &Palette) -> (ratatui::style::Color, ratatui::style::Color) {
    if active {
        (p.muted, p.quiet)
    } else {
        (p.quiet, p.border_strong)
    }
}

/// An open header strip: one row per member, `│ Title [counts]  ~tokens`, each a click target.
pub fn draw(out: &mut Rows, b: &Content, viewport_width: usize, p: &Palette) {
    // Counts sit beside their labels; project/global counts keep distinct tones in one bracket.
    let lead = 5usize.min(viewport_width);
    for (n, chip) in b.members.iter().enumerate() {
        if n > 0 {
            out.push((Line::default(), None));
        }
        let active = chip.is_active();
        let (tone, figure) = tones(active, p);
        let mut title = Style::default().fg(tone);
        if active {
            title = title.add_modifier(Modifier::BOLD);
        }
        let mut parts = vec![Span::styled(chip.title.clone(), title)];
        if !chip.counts.is_empty() {
            parts.push(Span::styled(" [", Style::default().fg(tone)));
            for (index, count) in chip.counts.iter().enumerate() {
                if index > 0 {
                    parts.push(Span::raw(" "));
                }
                parts.push(Span::styled(
                    count.to_string(),
                    Style::default().fg(if chip.counts.len() == 2 && index == 0 {
                        p.quiet
                    } else {
                        tone
                    }),
                ));
            }
            parts.push(Span::styled("]", Style::default().fg(tone)));
        }
        if let Some(tokens) = chip.token_label() {
            parts.push(Span::styled(
                format!("  {tokens}"),
                Style::default().fg(figure),
            ));
        }
        let w: usize = parts.iter().map(|s| s.content.width()).sum();
        let row = serde_json::json!({"kind":"context_chips","chips":[
            {"start":lead,"end":(lead + w).min(viewport_width),"operation":chip.operation}]});
        out.push((
            Line::from(
                [
                    Span::raw(" ".repeat(2.min(lead))),
                    Span::styled("│", Style::default().fg(tone)),
                    Span::raw(" ".repeat(lead.saturating_sub(3))),
                ]
                .into_iter()
                .chain(parts)
                .collect::<Vec<_>>(),
            ),
            Some(row),
        ));
    }
}

/// A standalone context block: a grey `element` chip with the title, the figure, then its text.
pub fn chip(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let active = b.is_active();
    let (tone, figure) = tones(active, p);
    let mut head = vec![Span::styled(
        format!(" {} ", b.title),
        Style::default()
            .fg(if active { p.text } else { p.muted })
            .bg(p.element)
            .add_modifier(Modifier::BOLD),
    )];
    if let Some(tokens) = b.token_label() {
        head.push(Span::styled(
            format!(" {tokens}"),
            Style::default().fg(figure),
        ));
    }
    indented(out, head, 2, width, &b.operation);
    for text in b.text.lines() {
        indented(
            out,
            vec![Span::styled(text.to_string(), Style::default().fg(tone))],
            4,
            width,
            &b.operation,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn member(title: &str, tokens: &str, active: bool) -> Content {
        Content {
            title: title.into(),
            tokens: tokens.into(),
            active,
            color: "#ff0000".into(),
            operation: Some(serde_json::json!({"kind":"context_toggle","id":"context:x"})),
            ..Default::default()
        }
    }

    #[test]
    fn active_and_inactive_headers_use_two_greys_and_never_the_agent_colour() {
        let p = Palette::new(false);
        let block = Content {
            kind: "context_header".into(),
            color: "#ff0000".into(),
            members: vec![member("Tools", "~4.4K", true), member("MCP", "~0", false)],
            ..Default::default()
        };
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        let spans = |i: usize| &rows[i].0.spans;
        // Row 0 is Tools, row 1 the blank spacer, row 2 MCP.
        assert_eq!(spans(0)[1].style.fg, Some(p.muted), "rail");
        assert_eq!(spans(0)[3].style.fg, Some(p.muted), "title");
        assert!(spans(0)[3].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(spans(0)[4].style.fg, Some(p.quiet), "figure");
        assert_eq!(spans(2)[1].style.fg, Some(p.quiet));
        assert_eq!(spans(2)[3].style.fg, Some(p.quiet));
        assert!(!spans(2)[3].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(spans(2)[4].style.fg, Some(p.border_strong));
        assert!(rows
            .iter()
            .flat_map(|(line, _)| &line.spans)
            .all(|span| span.style.fg != Some(ratatui::style::Color::Rgb(255, 0, 0))));
        assert!(rows[0].0.to_string().ends_with("Tools  4.4K"));
        assert!(!rows[0].0.to_string().contains('~'));
    }

    #[test]
    fn standalone_chip_is_grey_not_agent_coloured() {
        let p = Palette::new(false);
        let mut block = member("MCP", "~12", true);
        block.kind = "context".into();
        block.text = "Project 0 | Global 1".into();
        let mut rows = Vec::new();
        chip(&mut rows, &block, 40, &p);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0.spans[1].style.bg, Some(p.element));
        assert_eq!(rows[0].0.spans[1].style.fg, Some(p.text));
        assert_eq!(rows[0].0.spans[2].style.fg, Some(p.quiet));
        block.active = false;
        let mut rows = Vec::new();
        chip(&mut rows, &block, 40, &p);
        assert_eq!(rows[0].0.spans[1].style.fg, Some(p.muted));
    }
}
