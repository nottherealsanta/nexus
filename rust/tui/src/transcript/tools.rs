//! Standalone tool rows and the shared expanded body (GROK_BUILD_TUI_PLAN §5).
//!
//! A tool row shows only what the tool did: no status word, no badge. Failures are red, a
//! running call is bright, everything else is quiet; rails are neutral. Expanded, the row
//! shows the projection's relevant-only `signal` rows; when there are none (`/verbose`,
//! older hosts) it falls back to the full labelled `detail`. The full details stay one key
//! away: every row of the tool carries `inspect_operation` for `i`.
use super::{look, signal, truncate, Rows};
use crate::{bridge::Content, render::Palette};
use ratatui::{
    style::Style,
    text::{Line, Span},
};
use serde_json::Value;
use unicode_width::UnicodeWidthStr;

/// Status words the projection appends to a row that has no summary yet.
const STATUS_WORDS: [&str; 5] = ["running", "pending", "queued", "requested", "completed"];

/// Drop a trailing ` · running` style status word: the spinner and colour already say it.
pub fn relevant(text: &str) -> &str {
    for word in STATUS_WORDS {
        let suffix = format!(" · {word}");
        if let Some(head) = text.strip_suffix(suffix.as_str()) {
            return head;
        }
    }
    text
}

/// `operation` carrying `inspect` as `inspect_operation`, so a focused row can open the
/// full details (`i`). Other keys are untouched, so toggles keep matching their block.
pub fn with_inspect(operation: &Option<Value>, inspect: &Option<Value>) -> Option<Value> {
    let (Some(Value::Object(map)), Some(inspect)) = (operation, inspect) else {
        return operation.clone();
    };
    let mut map = map.clone();
    map.insert("inspect_operation".into(), inspect.clone());
    Some(Value::Object(map))
}

/// Whether the row shows a body: relevant rows or full details.
pub fn expanded(b: &Content) -> bool {
    b.shell.is_some() || !b.signal.is_empty() || !b.detail.is_empty()
}

/// The body behind `lead`: the relevant rows, else the labelled details.
pub fn body(
    out: &mut Rows,
    b: &Content,
    lead: &[Span<'static>],
    width: usize,
    p: &Palette,
    op: &Option<Value>,
) {
    if let Some(shell) = &b.shell {
        shell_body(out, b, shell, lead, width, p, op);
        return;
    }
    if !b.signal.is_empty() {
        signal::rows(out, &b.signal, lead, width, p, op);
        return;
    }
    let rows_op = with_inspect(&b.output_operation, &b.inspect_operation).or_else(|| op.clone());
    details(out, &b.detail, lead, width, p, &rows_op);
}

/// A fixed two-row command and four-row tail, shared by standalone and grouped tools.
fn shell_body(
    out: &mut Rows,
    b: &Content,
    shell: &Value,
    lead: &[Span<'static>],
    width: usize,
    p: &Palette,
    op: &Option<Value>,
) {
    let room = width
        .saturating_sub(lead.iter().map(|s| s.content.width()).sum())
        .max(1);
    let strings = |key: &str| -> Vec<&str> {
        shell[key]
            .as_array()
            .map(|v| v.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default()
    };
    let mut input = Vec::new();
    for text in strings("input") {
        input.extend(super::wrap(&signal::styled("code", text, p), room));
    }
    let clipped = input.len() > 2 || shell["input_clipped"].as_bool().unwrap_or(false);
    input.truncate(2);
    let input_op = with_inspect(&b.input_operation, &b.inspect_operation).or_else(|| op.clone());
    if clipped {
        let count = shell["input_lines"].as_u64().unwrap_or(0);
        let suffix = format!("… · {count} lines ▸ view");
        let last = input.pop().unwrap_or_default();
        let text: String = last.iter().map(|s| s.g.as_str()).collect();
        let shown = format!(
            "{}{}",
            truncate(&text, room.saturating_sub(suffix.width())),
            suffix
        );
        input.push(
            super::wrap(&signal::styled("code", &truncate(&shown, room), p), room)
                .pop()
                .unwrap_or_default(),
        );
    }
    while input.len() < 2 {
        input.push(Vec::new());
    }
    for cells in input {
        out.push((
            super::line(lead.to_vec(), cells, None, Style::default()),
            input_op.clone(),
        ));
    }
    let mut tail = Vec::new();
    for text in strings("output_tail") {
        tail.extend(super::wrap(&signal::styled("shell_out", text, p), room));
    }
    let earlier = shell["output_lines"]
        .as_u64()
        .unwrap_or(0)
        .saturating_sub(4);
    let output_op = with_inspect(&b.output_operation, &b.inspect_operation).or_else(|| op.clone());
    if earlier > 0 || tail.len() > 4 {
        let label = if earlier > 0 {
            format!("↑ {earlier} earlier lines ▸")
        } else {
            "↑ earlier wrapped rows ▸".into()
        };
        out.push((
            super::line(
                lead.to_vec(),
                super::wrap(&signal::styled("clip", &truncate(&label, room), p), room)
                    .pop()
                    .unwrap_or_default(),
                None,
                Style::default(),
            ),
            output_op.clone(),
        ));
    }
    if tail.is_empty() && b.status == "running" {
        tail.extend(super::wrap(&signal::styled("out", "running…", p), room));
    }
    let start = tail.len().saturating_sub(4);
    let mut visible = tail.into_iter().skip(start).collect::<Vec<_>>();
    while visible.len() < 4 {
        visible.push(Vec::new());
    }
    for cells in visible {
        out.push((
            super::line(lead.to_vec(), cells, None, Style::default()),
            output_op.clone(),
        ));
    }
    if let Some(failure) = shell["failure"].as_str().filter(|s| !s.is_empty()) {
        signal::rows(out, &[("error".into(), failure.into())], lead, width, p, op);
    }
}

/// The full labelled details (`tool_details`): `  Label: value` rows in a 12-column
/// label gutter, other rows as plain text. Shown under `/verbose` and by older hosts.
fn details(
    out: &mut Rows,
    text: &str,
    lead: &[Span<'static>],
    width: usize,
    p: &Palette,
    op: &Option<Value>,
) {
    const LABEL: usize = 12;
    let lead_width: usize = lead.iter().map(|s| s.content.width()).sum();
    for row in text.lines() {
        let pair = if row.starts_with("  ") && !row.starts_with("    ") {
            row.trim_start().split_once(": ")
        } else {
            None
        };
        let Some((label, value)) = pair else {
            let shown = row.strip_prefix("  ").unwrap_or(row);
            let tone = if row.starts_with(' ') {
                p.text
            } else {
                p.muted
            };
            for cells in super::wrap(
                &[Span::styled(shown.to_string(), Style::default().fg(tone))],
                width.saturating_sub(lead_width).max(1),
            ) {
                out.push((
                    super::line(lead.to_vec(), cells, None, Style::default()),
                    op.clone(),
                ));
            }
            continue;
        };
        let labels = super::wrap(
            &[Span::styled(
                label.to_string(),
                Style::default().fg(p.muted),
            )],
            LABEL,
        );
        // A long key wraps over its own rows before the first value row.
        let own_rows = if label.width() > LABEL {
            labels.len()
        } else {
            labels.len() - 1
        };
        for cells in labels.iter().take(own_rows) {
            out.push((
                super::line(lead.to_vec(), cells.clone(), None, Style::default()),
                op.clone(),
            ));
        }
        let first = if label.width() <= LABEL { label } else { "" };
        let values = super::wrap(
            &[Span::styled(value.to_string(), Style::default().fg(p.text))],
            width.saturating_sub(lead_width + LABEL).max(1),
        );
        for (index, cells) in values.into_iter().enumerate() {
            let mut prefix = lead.to_vec();
            prefix.push(Span::styled(
                if index == 0 {
                    format!("{first:<w$}", w = LABEL)
                } else {
                    " ".repeat(LABEL)
                },
                Style::default().fg(p.muted),
            ));
            out.push((
                super::line(prefix, cells, None, Style::default()),
                op.clone(),
            ));
        }
    }
}

/// The tone of a tool's heading: red when it failed, bright while it runs.
pub fn tone(status: &str, p: &Palette) -> ratatui::style::Color {
    match status {
        "failed" => p.error,
        "running" => p.text,
        _ => p.quiet,
    }
}

/// A tool row that stands alone (not inside a verb group).
pub fn draw(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let op = with_inspect(&b.operation, &b.inspect_operation);
    let style = Style::default().fg(tone(&b.status, p));
    for (index, text) in b.text.lines().enumerate() {
        let text = if index == 0 { relevant(text) } else { text };
        out.push((
            Line::styled(
                format!("    {}", truncate(text, width.saturating_sub(6))),
                style,
            ),
            op.clone(),
        ));
    }
    if expanded(b) {
        let rail = vec![
            Span::raw("    "),
            Span::styled(look::glyphs().rail, Style::default().fg(p.border)),
            Span::raw(" "),
        ];
        body(out, b, &rail, width, p, &op);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn plain(rows: &Rows) -> Vec<String> {
        rows.iter().map(|(row, _)| row.to_string()).collect()
    }

    #[test]
    fn status_words_are_dropped_but_summaries_stay() {
        assert_eq!(relevant("→ Read a.rs · running"), "→ Read a.rs");
        assert_eq!(relevant("$ cargo test · 0.8s"), "$ cargo test · 0.8s");
        assert_eq!(relevant("Bash · completed"), "Bash");
    }

    #[test]
    fn inspect_rides_on_the_toggle_without_changing_its_identity() {
        let toggle = Some(json!({"kind":"block_toggle","id":"c:detail"}));
        let inspect = Some(json!({"kind":"tool_details","id":"c"}));
        let row = with_inspect(&toggle, &inspect).unwrap();
        assert_eq!(row["kind"], "block_toggle");
        assert_eq!(row["id"], "c:detail");
        assert_eq!(row["inspect_operation"], inspect.clone().unwrap());
        assert_eq!(with_inspect(&None, &inspect), None);
        assert_eq!(with_inspect(&toggle, &None), toggle);
    }

    #[test]
    fn detail_rows_carry_the_output_operation_with_the_explicit_inspector() {
        let p = Palette::new(false);
        let tool = Content {
            kind: "tool".into(),
            text: "$ cargo test".into(),
            detail: "  Call ID: abc\nplain".into(),
            operation: Some(json!({"kind":"block_toggle","id":"c:detail"})),
            output_operation: Some(json!({"kind":"block_toggle","id":"c:output"})),
            inspect_operation: Some(json!({"kind":"tool_details","id":"x"})),
            ..Default::default()
        };
        let mut rows = Vec::new();
        draw(&mut rows, &tool, 40, &p);
        assert_eq!(rows.len(), 3);
        let head = rows[0].1.as_ref().unwrap();
        assert_eq!(head["id"], "c:detail");
        assert_eq!(head["inspect_operation"]["id"], "x");
        for (_, op) in &rows[1..] {
            let op = op.as_ref().unwrap();
            assert_eq!(op["kind"], "block_toggle");
            assert_eq!(op["id"], "c:output");
            assert_eq!(
                op["inspect_operation"],
                json!({"kind":"tool_details","id":"x"})
            );
        }
    }

    #[test]
    fn failed_rows_are_red_running_bright_and_signal_rows_replace_the_dump() {
        let p = Palette::new(false);
        let mut tool = Content {
            kind: "tool".into(),
            status: "failed".into(),
            text: "$ cargo test".into(),
            signal: vec![
                ("code".into(), "$ cargo test".into()),
                ("error".into(), "exit 2".into()),
            ],
            detail: "Call ID: abc".into(),
            operation: Some(json!({"kind":"block_toggle","id":"c:detail"})),
            inspect_operation: Some(json!({"kind":"tool_details","id":"c"})),
            ..Default::default()
        };
        let mut rows = Vec::new();
        draw(&mut rows, &tool, 40, &p);
        let lines = plain(&rows);
        assert_eq!(
            lines,
            ["    $ cargo test", "    │ $ cargo test", "    │ exit 2"]
        );
        assert_eq!(rows[0].0.style.fg, Some(p.error));
        assert!(rows
            .iter()
            .all(|(_, op)| op.as_ref().unwrap()["inspect_operation"]["id"] == "c"));
        assert!(!lines.join("\n").contains("Call ID"));
        tool.status = "running".into();
        tool.signal.clear();
        let mut rows = Vec::new();
        draw(&mut rows, &tool, 40, &p);
        assert_eq!(rows[0].0.style.fg, Some(p.text));
        assert!(
            plain(&rows).join("\n").contains("Call ID: abc"),
            "full details fall back"
        );
    }

    #[test]
    fn shell_window_has_fixed_rows_and_separate_targets() {
        let b = Content {
            status: "running".into(),
            shell: Some(json!({"input":["$ one", "  two"], "input_lines":8,
                "input_clipped":true, "output_tail":["a","b","c","d","e"], "output_lines":5})),
            input_operation: Some(json!({"kind":"tool_input"})),
            output_operation: Some(json!({"kind":"block_toggle"})),
            ..Default::default()
        };
        let mut out = Vec::new();
        body(&mut out, &b, &[], 60, &Palette::new(false), &None);
        let lines = plain(&out);
        assert_eq!(lines.len(), 7);
        assert!(lines[1].contains("8 lines ▸ view"));
        assert!(lines[2].contains("1 earlier lines"));
        assert_eq!(lines[3], "b");
        assert_eq!(lines[6], "e");
        assert_eq!(out[1].1.as_ref().unwrap()["kind"], "tool_input");
        assert_eq!(out[3].1.as_ref().unwrap()["kind"], "block_toggle");
    }
}
