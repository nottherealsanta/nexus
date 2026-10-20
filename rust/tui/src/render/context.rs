//! Bounded context inventories. Host text is newline-delimited complete labels;
//! detail dialogs remain host-owned via the unchanged context_show operation.
use super::Palette;
use crate::{bridge::Content, transcript::Rows};
use ratatui::{
    style::Style,
    text::{Line, Span},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

fn columns(key: &str, width: usize) -> usize {
    match key {
        "tools" => (width / 20).clamp(1, 5),
        "skills" => {
            if width >= 40 {
                2
            } else {
                1
            }
        }
        _ => 1,
    }
}

fn clipped(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_owned();
    }
    let mut result = String::new();
    for g in text.graphemes(true) {
        if result.width() + g.width() > width.saturating_sub(1) {
            break;
        }
        result.push_str(g);
    }
    if width > 0 {
        result.push('…');
    }
    result
}

fn inventory(block: &Content, width: usize, inset: usize, palette: &Palette) -> Rows {
    let inset = inset.min(width.saturating_sub(1));
    // Keep inventories readable on wide terminals instead of distributing short
    // names across the entire viewport. Match the heading's content gutter.
    let width = width.saturating_sub(inset).clamp(1, 100);
    let key = block.id.strip_prefix("context:").unwrap_or("");
    let count = columns(key, width);
    let labels: Vec<_> = block.text.lines().filter(|s| !s.is_empty()).collect();
    let shown = labels.len().min(count * 5);
    let cell = width / count;
    let mut rows = Vec::new();
    for chunk in labels[..shown].chunks(count) {
        let mut spans = vec![Span::raw(" ".repeat(inset))];
        for (i, label) in chunk.iter().enumerate() {
            let value = clipped(label, cell.saturating_sub(2).max(1));
            let padding = if i + 1 < chunk.len() {
                cell.saturating_sub(value.width())
            } else {
                0
            };
            spans.push(Span::styled(
                format!("{value}{}", " ".repeat(padding)),
                Style::default().fg(palette.text),
            ));
        }
        rows.push((Line::from(spans), block.operation.clone()));
    }
    for text in [block.local_preview.clone()] {
        if !text.is_empty() {
            rows.push((
                Line::from(vec![
                    Span::raw(" ".repeat(inset)),
                    Span::styled(clipped(&text, width), Style::default().fg(palette.muted)),
                ]),
                block.operation.clone(),
            ));
        }
    }
    rows
}

/// One muted line under the System prompt / AGENTS.md heading: the first line of
/// the text, clipped with an ellipsis. The full text stays in the host dialog.
fn preview(block: &Content, width: usize, inset: usize, palette: &Palette) -> Rows {
    let text = block
        .text
        .lines()
        .find(|l| !l.trim().is_empty())
        .unwrap_or("")
        .trim();
    if text.is_empty() {
        return Vec::new();
    }
    let inset = inset.min(width.saturating_sub(1));
    let room = width.saturating_sub(inset).clamp(1, 100);
    vec![(
        Line::from(vec![
            Span::raw(" ".repeat(inset)),
            Span::styled(clipped(text, room), Style::default().fg(palette.muted)),
        ]),
        block.operation.clone(),
    )]
}

/// The member's token count: the host's number, else the figure of its label.
fn count_of(member: &Content) -> Option<u64> {
    member
        .tokens
        .count()
        .or_else(|| parse_figure(&member.token_label()?))
}

/// `348`, `1.2K`, `12k`, `1.5M`, `17,580` (a leading `~` allowed) as a number; `?` is none.
fn parse_figure(text: &str) -> Option<u64> {
    let text = text.trim().trim_start_matches('~').replace(',', "");
    let (digits, scale) = match text.chars().last()? {
        'k' | 'K' => (&text[..text.len() - 1], 1e3),
        'm' | 'M' => (&text[..text.len() - 1], 1e6),
        _ => (text.as_str(), 1.0),
    };
    let value: f64 = digits.parse().ok()?;
    (value >= 0.0).then(|| (value * scale).round() as u64)
}

/// `17,580`.
fn grouped(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// `total  17,580 tok · 8.8% of the window`; the share is left out when the window is unknown.
fn total_rows(total: u64, window: Option<u64>, width: u16, palette: &Palette) -> Rows {
    let quiet = Style::default().fg(palette.quiet);
    let muted = Style::default().fg(palette.muted);
    let mut spans = vec![
        Span::styled("    total  ", quiet),
        Span::styled(grouped(total), muted),
        Span::styled(" tok", muted),
    ];
    if let Some(window) = window.filter(|&w| w > 0) {
        let share = total as f64 / window as f64 * 100.0;
        spans.push(Span::styled(format!(" · {share:.1}% of the window"), quiet));
    }
    crate::transcript::wrap(&spans, usize::from(width))
        .into_iter()
        .map(|cells| {
            (
                crate::transcript::line(Vec::new(), cells, None, Style::default()),
                None,
            )
        })
        .collect()
}

pub(super) fn build(block: &Content, width: u16, palette: &Palette, window: Option<u64>) -> Rows {
    if block.kind == "context_header" {
        let mut rows = Vec::new();
        let counts: Vec<Option<u64>> = block.members.iter().map(count_of).collect();
        let total: u64 = counts.iter().flatten().sum();
        for (index, member) in block.members.iter().enumerate() {
            if member.collapsed {
                let active =
                    !member.text.is_empty() || member.count > 0 || member.local_preview != "~0";
                let tone = if active { palette.muted } else { palette.quiet };
                let tokens = member.token_label().unwrap_or_else(|| "?".into());
                let mut title = Style::default().fg(tone);
                if active {
                    title = title.add_modifier(ratatui::style::Modifier::BOLD);
                }
                let counts = member.counts_label();
                let mut spans = vec![Span::raw("    "), Span::styled(member.title.clone(), title)];
                if !counts.is_empty() {
                    spans.push(Span::styled(
                        format!(" {counts}"),
                        Style::default().fg(tone),
                    ));
                }
                // Items are told apart by a blank row, as in the open form.
                if index > 0 {
                    rows.push((Line::default(), None));
                }
                let line = Line::from(
                    spans
                        .into_iter()
                        .chain([Span::styled(
                            format!(" {tokens}"),
                            Style::default().fg(if active {
                                palette.quiet
                            } else {
                                palette.border_strong
                            }),
                        )])
                        .collect::<Vec<_>>(),
                );
                for line in crate::transcript::wrap(&line.spans, width as usize) {
                    let drawn = crate::transcript::line(Vec::new(), line, None, Style::default());
                    let operation = member.operation.as_ref().map(|op| {
                        serde_json::json!({
                            "kind":"context_chips", "default":op,
                            "chips":[{"start":2, "end":drawn.width(), "operation":op}]
                        })
                    });
                    rows.push((drawn, operation));
                }
                continue;
            }
            let mut heading = block.clone();
            let mut chip = member.clone();
            chip.text.clear();
            chip.color.clear();
            heading.color.clear();
            heading.members = vec![chip];
            if index > 0 {
                rows.push((Line::default(), None));
            }
            heading.gap = if index == 0 { block.gap } else { 0 };
            rows.extend(crate::transcript::build(&heading, width, palette));
            if matches!(
                member.id.as_str(),
                "context:tools" | "context:skills" | "context:mcp"
            ) {
                rows.extend(inventory(member, usize::from(width), 5, palette));
            } else if matches!(member.id.as_str(), "context:system" | "context:agents") {
                rows.extend(preview(member, usize::from(width), 5, palette));
            }
        }
        if total > 0 {
            rows.push((Line::default(), None));
            rows.extend(total_rows(total, window, width, palette));
        }
        rows
    } else if block.kind == "context"
        && matches!(
            block.id.as_str(),
            "context:tools" | "context:skills" | "context:mcp"
        )
    {
        let mut heading = block.clone();
        heading.text.clear();
        let mut rows = crate::transcript::build(&heading, width, palette);
        rows.extend(inventory(block, usize::from(width), 4, palette));
        rows
    } else {
        crate::transcript::build(block, width, palette)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn closed_headers_wrap_and_keep_clicks_on_every_row() {
        let palette = Palette::new(false);
        let block = Content {
            kind: "context_header".into(),
            members: vec![Content {
                title: "A long System prompt header".into(),
                collapsed: true,
                status: "~348 tokens".into(),
                color: "#ff0000".into(),
                operation: Some(
                    serde_json::json!({"kind":"context_toggle", "id":"context:system"}),
                ),
                ..Default::default()
            }],
            ..Default::default()
        };
        let rows = build(&block, 16, &palette, None);
        assert!(rows.len() > 1);
        assert!(rows.iter().all(|(line, _)| line.width() <= 16));
        let member_rows = rows.iter().take_while(|(line, _)| line.width() > 0);
        assert!(member_rows.clone().count() > 1);
        assert!(member_rows.into_iter().all(|(_, op)| op.is_some()));
        let text: String = rows.iter().map(|(line, _)| line.to_string()).collect();
        assert!(text.contains("348") && !text.contains('~'));
        assert!(!text.contains("tokens"));
        assert!(rows
            .iter()
            .flat_map(|(line, _)| &line.spans)
            .all(|span| span.style.fg != Some(ratatui::style::Color::Rgb(255, 0, 0))));
    }
    #[test]
    fn closed_headers_show_counts_and_plain_tokens_with_a_blank_row_between() {
        let palette = Palette::new(false);
        let member = |title: &str, counts: Vec<usize>| Content {
            title: title.into(),
            collapsed: true,
            tokens: "~1.2K".into(),
            active: true,
            counts,
            ..Default::default()
        };
        let block = Content {
            kind: "context_header".into(),
            members: vec![
                member("Tools", vec![12]),
                member("Skills", vec![2, 3]),
                member("MCP", vec![]),
            ],
            ..Default::default()
        };
        let lines: Vec<_> = build(&block, 80, &palette, None)
            .iter()
            .map(|(line, _)| line.to_string())
            .collect();
        // The total row follows and omits the share without a window.
        assert_eq!(
            lines,
            [
                "    Tools [12] 1.2K".to_string(),
                String::new(),
                "    Skills [2 3] 1.2K".to_string(),
                String::new(),
                "    MCP 1.2K".to_string(),
                String::new(),
                "    total  3,600 tok".to_string(),
            ]
        );
    }

    fn sized(title: &str, tokens: &str) -> Content {
        Content {
            title: title.into(),
            collapsed: true,
            tokens: tokens.into(),
            active: true,
            ..Default::default()
        }
    }
    fn header(members: Vec<Content>) -> Content {
        Content {
            kind: "context_header".into(),
            members,
            ..Default::default()
        }
    }
    fn texts(rows: &Rows) -> Vec<String> {
        rows.iter().map(|(line, _)| line.to_string()).collect()
    }

    #[test]
    fn total_row_reports_the_share_of_the_window_when_known() {
        let palette = Palette::new(false);
        let block = header(vec![sized("A", "~12,000"), sized("B", "~5,580")]);
        let with = texts(&build(&block, 100, &palette, Some(200_000)));
        assert_eq!(
            with.last().unwrap(),
            "    total  17,580 tok · 8.8% of the window"
        );
        let without = texts(&build(&block, 100, &palette, None));
        assert_eq!(without.last().unwrap(), "    total  17,580 tok");
        let none = header(vec![sized("A", "?")]);
        assert!(!texts(&build(&none, 100, &palette, None))
            .iter()
            .any(|line| line.contains("total")));
    }

    #[test]
    fn header_draws_no_bars_and_is_identical_across_ticks() {
        let palette = Palette::new(false);
        let block = header(vec![
            sized("A", "~1.0K"),
            sized("B", "~500"),
            sized("C", "~0"),
        ]);
        let first = texts(&build(&block, 80, &palette, Some(10_000)));
        std::thread::sleep(std::time::Duration::from_millis(200));
        let second = texts(&build(&block, 80, &palette, Some(10_000)));
        assert_eq!(
            first, second,
            "no animation: every tick draws the same rows"
        );
        for line in &first {
            assert!(
                !line.chars().any(|c| "█▓▒░▚".contains(c))
                    && !["###", "%%%", "+++", "..."]
                        .iter()
                        .any(|run| line.contains(run)),
                "bar glyph in {line:?}"
            );
        }
        assert_eq!(first[0], "    A 1.0K");
    }

    #[test]
    fn narrow_terminals_keep_every_row_in_width() {
        let palette = Palette::new(false);
        let block = header(vec![
            sized("System prompt", "~348"),
            sized("Tools", "~1.2K"),
        ]);
        for width in [12, 20, 28, 40] {
            let rows = build(&block, width, &palette, Some(1000));
            assert!(rows
                .iter()
                .all(|(line, _)| line.width() <= usize::from(width)));
        }
    }

    #[test]
    fn figures_parse_and_format_like_the_host() {
        assert_eq!(parse_figure("~1.2K"), Some(1200));
        assert_eq!(parse_figure("17,580"), Some(17_580));
        assert_eq!(parse_figure("2.1k"), Some(2100));
        assert_eq!(parse_figure("1.5M"), Some(1_500_000));
        assert_eq!(parse_figure("?"), None);
        assert_eq!(grouped(17_580), "17,580");
        assert_eq!(grouped(999), "999");
    }

    #[test]
    fn responsive_bounds_and_operations() {
        let palette = Palette::new(false);
        for (width, expected) in [(20, 5), (60, 15), (80, 20), (100, 25)] {
            let block = Content {
                id: "context:tools".into(),
                text: (0..30)
                    .map(|i| format!("tool_{i}"))
                    .collect::<Vec<_>>()
                    .join("\n"),
                operation: Some(serde_json::json!({"kind":"context_show", "key":"tools"})),
                ..Default::default()
            };
            let rows = inventory(&block, width, 0, &palette);
            assert_eq!(rows.len(), 5, "capped at five rows of {expected} labels");
            assert!(rows
                .iter()
                .all(|(line, op)| line.width() <= width && op == &block.operation));
        }
    }
    #[test]
    fn skills_and_mcp_caps_preserve_unicode_width() {
        let palette = Palette::new(false);
        for (key, shown) in [("skills", 10), ("mcp", 5)] {
            let block = Content {
                id: format!("context:{key}"),
                text: vec!["界👩‍💻 very long inventory name"; 12].join("\n"),
                ..Default::default()
            };
            let rows = inventory(&block, 40, 0, &palette);
            assert!(rows.len() <= 5, "{key} stays capped (shown {shown})");
            assert!(rows.iter().all(|(line, _)| line.width() <= 40));
        }
    }

    #[test]
    fn compact_inventory_matches_heading_gutter_without_zero_omitted_noise() {
        let palette = Palette::new(false);
        let member = Content {
            id: "context:tools".into(),
            title: "Tools".into(),
            counts: vec![2],
            text: "read\nwrite".into(),
            ..Default::default()
        };
        let block = Content {
            kind: "context_header".into(),
            members: vec![member],
            ..Default::default()
        };
        let rows = build(&block, 160, &palette, None);
        assert_eq!(rows.len(), 2);
        assert!(rows[0].0.to_string().starts_with("     Tools [2]"));
        assert!(rows[1].0.to_string().starts_with("     read"));
        assert!(rows[1].0.width() <= 105);
        assert!(!rows.iter().any(|row| row.0.to_string().contains("omitted")));
    }

    #[test]
    fn system_and_agents_previews_always_show_under_their_heading() {
        let palette = Palette::new(false);
        let member = |id: &str, title: &str, text: &str| Content {
            id: id.into(),
            title: title.into(),
            text: text.into(),
            status: "~2.1k tokens".into(),
            operation: Some(serde_json::json!({"kind":"context_show","key":"system"})),
            ..Default::default()
        };
        let block = Content {
            kind: "context_header".into(),
            members: vec![
                member(
                    "context:system",
                    "System prompt",
                    "You are Nexus, a provider-agnostic agent.",
                ),
                member("context:agents", "AGENTS.md", ""),
            ],
            ..Default::default()
        };
        let rows = build(&block, 80, &palette, None);
        let line = |i: usize| rows[i].0.to_string();
        assert!(line(0).starts_with("     System prompt"), "{}", line(0));
        assert!(line(1).starts_with("     You are Nexus"), "{}", line(1));
        assert!(rows[1].1.is_some(), "the preview opens the same dialog");
        assert!(line(3).starts_with("     AGENTS.md"));
        let narrow = build(&block, 30, &palette, None);
        assert!(
            narrow.iter().all(|(l, _)| l.width() <= 30),
            "clipped, never overflowing"
        );
        assert!(
            narrow[1].0.to_string().ends_with('…'),
            "clipping is announced"
        );
    }
}
