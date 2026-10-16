//! Typed rows of a tool's relevant-only view (`nexus/ui_support/tool_signal.py`).
//!
//! The projection sends `[kind, text]` pairs: `text` plain, `code` a command, `out` program
//! output (optionally `N  line`), `add` an added line, `path` a `path:line: snippet` hit,
//! `kv` a `key: value` parameter, `md` prose, `error` a failure and `clip` an announced
//! omission. Colour carries the kind; there are no labels or badges.
use super::{line, wrap, Rows};
use crate::render::Palette;
use ratatui::{
    style::{Modifier, Style},
    text::Span,
};
use serde_json::Value;
use unicode_width::UnicodeWidthStr;

/// `"  12  text"` → (`"  12  "`, `"text"`) when the row starts with a line number.
fn numbered(text: &str) -> Option<(&str, &str)> {
    let trimmed = text.trim_start_matches(' ');
    let digits = trimmed.bytes().take_while(u8::is_ascii_digit).count();
    if digits == 0 || !trimmed[digits..].starts_with("  ") {
        return None;
    }
    let split = text.len() - trimmed.len() + digits + 2;
    Some((&text[..split], &text[split..]))
}

fn span(text: &str, style: Style) -> Span<'static> {
    Span::styled(text.to_string(), style)
}

/// The styled spans of one row of `kind`.
pub fn styled(kind: &str, text: &str, p: &Palette) -> Vec<Span<'static>> {
    let fg = |color| Style::default().fg(color);
    match kind {
        "code" => vec![span(text, fg(p.text).add_modifier(Modifier::BOLD))],
        "out" => match numbered(text) {
            Some((number, rest)) => vec![span(number, fg(p.quiet)), span(rest, fg(p.text))],
            None => vec![span(text, fg(p.muted))],
        },
        "add" => {
            let tint = |style: Style| style.bg(p.diff_add);
            match numbered(text) {
                Some((number, rest)) => vec![
                    span(number, tint(fg(p.quiet))),
                    span(rest, tint(fg(p.success))),
                ],
                None => vec![span(text, tint(fg(p.success)))],
            }
        }
        "path" => match text.split_once(':') {
            Some((path, rest)) => {
                // `path:line: snippet` — the location is quiet punctuation around the path.
                let (location, snippet) = rest.split_once(": ").map_or((rest, ""), |(a, b)| (a, b));
                let mut spans = vec![span(path, fg(p.cyan))];
                if snippet.is_empty() && !rest.contains(": ") {
                    spans.push(span(&format!(":{rest}"), fg(p.text)));
                } else {
                    spans.push(span(&format!(":{location}: "), fg(p.quiet)));
                    spans.push(span(snippet, fg(p.text)));
                }
                spans
            }
            None => vec![span(text, fg(p.cyan))],
        },
        "kv" => match text.split_once(": ") {
            Some((key, value)) => vec![
                span(key, fg(p.muted)),
                span(": ", fg(p.quiet)),
                span(value, fg(p.text)),
            ],
            None => vec![span(text, fg(p.muted))],
        },
        "md" => vec![span(text, fg(p.text))],
        "error" => vec![span(text, fg(p.error))],
        "clip" => vec![span(text, fg(p.quiet).add_modifier(Modifier::ITALIC))],
        _ => vec![span(text, fg(p.muted))],
    }
}

/// Draw the rows behind `lead` (the rail gutter). Long rows wrap; programs' own line
/// breaks (`out`, `code`, `add`) keep their column, prose gets a two-cell hanging indent.
pub fn rows(
    out: &mut Rows,
    signal: &[(String, String)],
    lead: &[Span<'static>],
    width: usize,
    p: &Palette,
    op: &Option<Value>,
) {
    let lead_width: usize = lead.iter().map(|s| s.content.width()).sum();
    let room = width.saturating_sub(lead_width).max(8);
    for (kind, text) in signal {
        let hang = !matches!(kind.as_str(), "out" | "code" | "add");
        for raw in text.split('\n') {
            for (index, cells) in wrap(&styled(kind, raw, p), room.saturating_sub(2))
                .into_iter()
                .enumerate()
            {
                let mut prefix = lead.to_vec();
                if index > 0 && hang {
                    prefix.push(Span::raw("  "));
                }
                out.push((line(prefix, cells, None, Style::default()), op.clone()));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn palette() -> Palette {
        Palette::new(false)
    }

    #[test]
    fn line_numbers_split_from_excerpts() {
        assert_eq!(numbered("  12  fn main()"), Some(("  12  ", "fn main()")));
        assert_eq!(numbered("12 not numbered"), None);
        assert_eq!(numbered("plain"), None);
    }

    #[test]
    fn kinds_pick_their_colours() {
        let p = palette();
        let color = |kind: &str, text: &str, index: usize| styled(kind, text, &p)[index].style.fg;
        assert_eq!(color("error", "exit 2", 0), Some(p.error));
        assert_eq!(color("clip", "… 3 lines", 0), Some(p.quiet));
        assert_eq!(color("add", "1  new", 1), Some(p.success));
        assert_eq!(styled("add", "1  new", &p)[1].style.bg, Some(p.diff_add));
        assert_eq!(color("path", "src/a.rs:4: hit", 0), Some(p.cyan));
        assert_eq!(color("path", "src/a.rs:4: hit", 2), Some(p.text));
        assert_eq!(color("kv", "pattern: x", 0), Some(p.muted));
        assert_eq!(color("kv", "pattern: x", 2), Some(p.text));
        assert!(styled("code", "$ ls", &p)[0]
            .style
            .add_modifier
            .contains(Modifier::BOLD));
    }

    #[test]
    fn rows_wrap_inside_the_width_and_keep_the_operation() {
        let p = palette();
        let op = Some(serde_json::json!({"kind": "block_toggle", "id": "c:detail"}));
        let lead = vec![Span::raw("    │ ")];
        let signal = vec![
            ("kv".to_string(), format!("path: {}", "word ".repeat(30))),
            ("out".to_string(), "界".repeat(30)),
        ];
        let mut out = Vec::new();
        rows(&mut out, &signal, &lead, 30, &p, &op);
        assert!(out.len() > 2);
        assert!(out
            .iter()
            .all(|(row, row_op)| row.width() <= 30 && row_op == &op));
        assert!(out
            .iter()
            .all(|(row, _)| row.to_string().starts_with("    │ ")));
    }
}
