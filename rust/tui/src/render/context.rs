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

pub(super) fn build(block: &Content, width: u16, palette: &Palette) -> Rows {
    if block.kind == "context_header" {
        let mut rows = Vec::new();
        for (index, member) in block.members.iter().enumerate() {
            if index > 0 {
                rows.push((Line::default(), None));
            }
            let mut heading = block.clone();
            let mut chip = member.clone();
            chip.text.clear();
            heading.members = vec![chip];
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
        let rows = build(&block, 160, &palette);
        assert_eq!(rows.len(), 2);
        assert!(rows[0].0.to_string().starts_with("  │  Tools [2]"));
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
        let rows = build(&block, 80, &palette);
        let line = |i: usize| rows[i].0.to_string();
        assert!(line(0).starts_with("  │  System prompt"), "{}", line(0));
        assert!(line(1).starts_with("     You are Nexus"), "{}", line(1));
        assert!(rows[1].1.is_some(), "the preview opens the same dialog");
        assert!(line(2).is_empty() && line(3).starts_with("  │  AGENTS.md"));
        let narrow = build(&block, 30, &palette);
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
