//! Hover-revealed Copy buttons on user cards and fenced code (docs/ratatui-parity.md).
//!
//! Every row of a copyable block carries the same small marker operation,
//! `{"kind":"copy","block":id,"fence":n|null,"operation":inner}`, so focus groups and
//! clicks elsewhere keep the inner operation. The button is painted over the blank
//! right end of the block's first row only while the pointer is over the block, and
//! the copied text is recovered from the snapshot at click time (rows never hold it).
use crate::{bridge::Content, render::Indexed};
use ratatui::{
    style::Style,
    text::{Line, Span},
};
use serde_json::{json, Value};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub const LABEL: &str = " Copy ";
/// Longest block scanned for its first row; longer blocks show no button.
const MAX_SCAN: usize = 20_000;

pub fn mark(block: &str, fence: Option<usize>, inner: &Option<Value>) -> Option<Value> {
    Some(json!({"kind":"copy","block":block,"fence":fence,"operation":inner}))
}

/// The operation a row performs apart from its Copy button.
pub fn inner(operation: &Option<Value>) -> Option<&Value> {
    let operation = operation.as_ref()?;
    if operation["kind"] != "copy" {
        return Some(operation);
    }
    let inner = &operation["operation"];
    (!inner.is_null()).then_some(inner)
}

fn key(operation: Option<&Option<Value>>) -> Option<(&Value, &Value)> {
    let operation = operation?.as_ref()?;
    (operation["kind"] == "copy").then(|| (&operation["block"], &operation["fence"]))
}

/// First row of the copyable block containing `row`.
pub fn first_row(operations: &Indexed<Option<Value>>, row: usize) -> Option<usize> {
    let wanted = key(operations.get(row))?;
    let mut first = row;
    while first > 0 && key(operations.get(first - 1)) == Some(wanted) {
        first -= 1;
        if row - first > MAX_SCAN {
            return None;
        }
    }
    Some(first)
}

/// Columns of the button on a block's first row, when that end of the row is blank.
/// A trailing unstyled blank (a card's right margin) stays outside the button.
pub fn columns(line: &Line<'_>) -> Option<(usize, usize)> {
    let margin: usize = line
        .spans
        .iter()
        .rev()
        .take_while(|span| span.style == Style::default() && span.content.trim().is_empty())
        .map(|span| span.content.width())
        .sum();
    let end = line
        .spans
        .iter()
        .map(|span| span.content.width())
        .sum::<usize>()
        .checked_sub(margin)?;
    let start = end.checked_sub(LABEL.width())?;
    let mut at = 0;
    for span in &line.spans {
        for grapheme in span.content.graphemes(true) {
            if at >= start && !matches!(grapheme, " " | "─") {
                return None;
            }
            at += grapheme.width();
        }
    }
    Some((start, end))
}

/// Paint the button over `start..start + LABEL` of `line`, keeping the row's
/// background and anything after the label (a card's right margin).
pub fn paint(line: Line<'static>, start: usize, style: Style) -> Line<'static> {
    let mut result = Line::default().style(line.style);
    let mut at = 0;
    let mut painted = false;
    let end = start + LABEL.width();
    for span in line.spans {
        let mut kept = String::new();
        for grapheme in span.content.graphemes(true) {
            if at < start || at >= end {
                kept.push_str(grapheme);
            } else if !painted {
                painted = true;
                if !kept.is_empty() {
                    result
                        .spans
                        .push(Span::styled(std::mem::take(&mut kept), span.style));
                }
                result
                    .spans
                    .push(Span::styled(LABEL, span.style.patch(style)));
            }
            at += grapheme.width();
        }
        if !kept.is_empty() {
            result.spans.push(Span::styled(kept, span.style));
        }
    }
    result
}

/// The button a click at `(row, column)` lands on: its block id and fence index.
pub fn hit(
    operations: &Indexed<Option<Value>>,
    lines: &Indexed<Line<'static>>,
    row: usize,
    column: usize,
) -> Option<(String, Option<usize>)> {
    let first = first_row(operations, row)?;
    if first != row {
        return None;
    }
    let (start, end) = columns(lines.get(row)?)?;
    if !(start..end).contains(&column) {
        return None;
    }
    let (block, fence) = key(operations.get(row))?;
    Some((
        block.as_str()?.to_string(),
        fence.as_u64().map(|n| n as usize),
    ))
}

/// What a button copies: the whole user message, or one code block's source.
pub fn text(blocks: &[Content], block: &str, fence: Option<usize>) -> Option<String> {
    let source = blocks
        .iter()
        .chain(blocks.iter().flat_map(|b| b.members.iter()))
        .find(|b| b.id == block)?;
    if let Some(fence) = fence {
        return crate::markdown::fence_sources(&source.text)
            .into_iter()
            .nth(fence);
    }
    // A host-folded card keeps only its first line (marked with " …").
    if source.collapsed {
        return Some(source.title.trim_end_matches(" …").to_string());
    }
    Some(if source.text.is_empty() {
        source.title.clone()
    } else {
        format!("{}\n{}", source.title, source.text)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::render::Palette;

    fn indexed(rows: crate::transcript::Rows) -> (Indexed<Option<Value>>, Indexed<Line<'static>>) {
        let (mut ops, mut lines) = (Indexed::default(), Indexed::default());
        for (line, op) in rows {
            lines.push(line);
            ops.push(op);
        }
        (ops, lines)
    }
    fn plain(line: &Line<'_>) -> String {
        line.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    #[test]
    fn user_card_button_sits_on_its_first_row_and_copies_the_message() {
        let block = Content {
            id: "m:user".into(),
            kind: "user".into(),
            title: "first line".into(),
            text: "second line".into(),
            number: 3,
            operation: Some(json!({"kind":"turn_toggle","id":"t"})),
            ..Default::default()
        };
        let (ops, lines) = indexed(crate::transcript::build(&block, 60, &Palette::new(false)));
        let last = ops.len() - 1;
        assert_eq!(first_row(&ops, last), Some(0));
        let (start, end) = columns(lines.get(0).unwrap()).unwrap();
        assert_eq!(end, 59, "the card's right margin stays outside the button");
        assert_eq!(hit(&ops, &lines, 0, start), Some(("m:user".into(), None)));
        assert_eq!(hit(&ops, &lines, 1, start), None);
        assert_eq!(hit(&ops, &lines, 0, start - 1), None);
        // Clicks elsewhere keep the fold action.
        assert_eq!(inner(ops.get(1).unwrap()).unwrap()["kind"], "turn_toggle");
        let painted = paint(lines.get(0).unwrap().clone(), start, Style::default());
        assert!(plain(&painted).ends_with(&format!("{LABEL} ")));
        assert_eq!(plain(&painted).width(), 60);
        assert_eq!(
            text(&[block], "m:user", None).as_deref(),
            Some("first line\nsecond line")
        );
    }

    #[test]
    fn each_code_fence_has_its_own_button_and_literal_source() {
        let source = "Intro\n\n```rust\nlet x = 1;\n```\n\nMiddle\n\n```\nplain\ttab\n```";
        let block = Content {
            id: "r".into(),
            kind: "markdown".into(),
            text: source.into(),
            ..Default::default()
        };
        let (ops, lines) = indexed(crate::transcript::build(&block, 60, &Palette::new(false)));
        let firsts: Vec<usize> = (0..ops.len())
            .filter(|&row| first_row(&ops, row) == Some(row))
            .collect();
        assert_eq!(firsts.len(), 2);
        for (n, row) in firsts.iter().enumerate() {
            let (start, _) = columns(lines.get(*row).unwrap()).expect("blank header end");
            assert_eq!(hit(&ops, &lines, *row, start), Some(("r".into(), Some(n))));
        }
        // Prose rows carry no marker and no focus target.
        assert_eq!(first_row(&ops, 0), None);
        assert_eq!(
            text(&[block.clone()], "r", Some(0)).as_deref(),
            Some("let x = 1;")
        );
        assert_eq!(text(&[block], "r", Some(1)).as_deref(), Some("plain\ttab"));
    }
}
