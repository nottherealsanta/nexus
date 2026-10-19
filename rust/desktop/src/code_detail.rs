//! Code-mode (`code` tool) detail: plan 03 §3.13 on the desktop.
//!
//! The host projects the same labelled sections to every native client
//! (`ui_support/tool_details.py`); this module only parses that text and gives
//! the code-mode sections their structure, matching the Ratatui client: the
//! program as a line-numbered code block, one card per nested call (status glyph,
//! tool, compact arguments, duration, size; denied rows show the rule; the full
//! arguments and result follow), and store writes as a small table. Nothing is
//! dropped: unknown rows fall through to the generic labelled rendering.
use crate::{theme, theme::Theme};
use gpui::{prelude::*, *};

/// One section of `sections_to_text`: an unindented title then indented rows.
#[derive(Debug, PartialEq)]
pub(crate) struct Section<'a> {
    pub title: &'a str,
    pub lines: Vec<&'a str>,
}

pub(crate) fn split_sections(detail: &str) -> Vec<Section<'_>> {
    let mut out: Vec<Section<'_>> = Vec::new();
    for line in detail.lines() {
        if !line.is_empty() && !line.starts_with(' ') {
            out.push(Section { title: line, lines: Vec::new() });
        } else if let Some(last) = out.last_mut() {
            last.lines.push(line);
        }
    }
    // Blank separators between sections belong to neither; padded code rows do.
    for section in &mut out {
        while section.lines.last().is_some_and(|l| l.is_empty()) {
            section.lines.pop();
        }
    }
    out
}

fn is_nested(title: &str) -> bool {
    title.starts_with("Nested call")
}
fn is_store(title: &str) -> bool {
    title.starts_with("Stored entries") || title.starts_with("Store writes")
}
/// True when `detail` carries any code-mode section worth structured rendering.
pub(crate) fn is_code_detail(detail: &str) -> bool {
    split_sections(detail)
        .iter()
        .any(|s| is_nested(s.title) || is_store(s.title) || is_header(s.title) || s.title == "Program status")
}

/// Program lines with the `code:` block indent removed. The host already
/// prefixes `N  ` line numbers (matching Monty tracebacks); the desktop must not
/// number them a second time, so the gutter is split off the text instead.
pub(crate) fn program_lines<'a>(section: &Section<'a>) -> Vec<(&'a str, &'a str)> {
    section
        .lines
        .iter()
        .filter(|l| !l.trim_end().ends_with("code:") || l.starts_with("    "))
        .map(|l| l.strip_prefix("    ").unwrap_or(l.trim_start()))
        .map(|l| {
            let digits = l.len() - l.trim_start_matches(|c: char| c.is_ascii_digit()).len();
            match l[digits..].strip_prefix("  ") {
                Some(text) if digits > 0 => (&l[..digits], text),
                _ => ("", l),
            }
        })
        .collect()
}

/// `(key, value)` rows of a Store writes / Stored entries section.
pub(crate) fn table_rows<'a>(section: &Section<'a>) -> Vec<(&'a str, &'a str)> {
    section
        .lines
        .iter()
        .map(|l| pair(l).unwrap_or(("", l.trim_start())))
        .collect()
}

fn is_header(title: &str) -> bool {
    title.starts_with("Code · ") || title.starts_with("code · ")
}

#[derive(Debug, PartialEq, Default)]
pub(crate) struct NestedHead<'a> {
    pub tool: &'a str,
    pub status: &'a str,
    pub duration: &'a str,
    pub size: &'a str,
    pub rule: &'a str,
    pub args: String,
    /// Rows not consumed by the header, kept verbatim for the full view.
    pub rest: Vec<&'a str>,
}

fn pair(line: &str) -> Option<(&str, &str)> {
    (line.starts_with("  ") && !line.starts_with("    "))
        .then(|| line.trim_start().split_once(": "))
        .flatten()
}

pub(crate) fn nested_head<'a>(section: &Section<'a>) -> NestedHead<'a> {
    let mut head = NestedHead::default();
    let mut args = Vec::new();
    for line in &section.lines {
        match pair(line) {
            Some(("Tool", v)) => head.tool = v,
            Some(("Kind", _)) => {}
            Some(("Status", v)) => head.status = v,
            Some(("Duration (ms)", v)) | Some(("Duration", v)) => head.duration = v,
            Some(("Bytes", v)) | Some(("Size", v)) => head.size = v,
            Some((label, v)) if label.to_ascii_lowercase().contains("rule") => {
                head.rule = v;
                head.rest.push(line);
            }
            Some((label, v)) if label.starts_with("Arguments") => {
                args.push(format!("{}={}", label.trim_start_matches("Arguments."), v));
                head.rest.push(line);
            }
            _ => head.rest.push(line),
        }
    }
    let joined = args.join(" ");
    head.args = if joined.chars().count() > 72 {
        format!("{}…", joined.chars().take(72).collect::<String>())
    } else {
        joined
    };
    head
}

/// `(icon, tone)` for a nested-call status; unknown words keep a neutral dot.
fn glyph(status: &str, t: Theme) -> (&'static str, Hsla) {
    match status {
        "ok" | "completed" | "done" => ("check", t.green),
        "error" | "failed" => ("x", t.red),
        "denied" => ("x", t.amber),
        "running" => ("refresh-cw", t.amber),
        _ => ("circle-dot", t.muted),
    }
}

fn code_block(lines: Vec<(&str, &str)>, t: Theme) -> AnyElement {
    let width = lines.iter().map(|(n, _)| n.len()).max().unwrap_or(0);
    div()
        .ml(px(theme::space::LG))
        .my_1()
        .p_2()
        .rounded(px(theme::radius::CONTROL))
        .bg(t.surface)
        .flex()
        .flex_col()
        .text_size(px(theme::size::SMALL))
        .line_height(px(theme::size::CODE_LINE))
        .font_family(theme::code_font())
        .children(lines.into_iter().map(|(n, text)| {
            div()
                .flex()
                .gap_3()
                .child(
                    div()
                        .flex_shrink_0()
                        .w(px(8. * width as f32 + 4.))
                        .text_color(t.text_tertiary)
                        .child(format!("{n:>width$}")),
                )
                .child(div().flex_1().min_w_0().text_color(t.text).child(if text.is_empty() {
                    " ".to_string()
                } else {
                    text.to_string()
                }))
        }))
        .into_any_element()
}

/// Two-column key/value table with a rule between rows.
fn table(rows: Vec<(&str, &str)>, t: Theme) -> AnyElement {
    div()
        .ml(px(theme::space::LG))
        .my_1()
        .rounded(px(theme::radius::CONTROL))
        .border_1()
        .border_color(t.border)
        .flex()
        .flex_col()
        .text_size(px(theme::size::SMALL))
        .line_height(px(theme::size::CODE_LINE))
        .font_family(theme::code_font())
        .children(rows.into_iter().enumerate().map(|(i, (k, v))| {
            div()
                .flex()
                .gap_3()
                .px_2()
                .when(i > 0, |d| d.border_t_1().border_color(t.border))
                .child(div().w(px(140.)).flex_shrink_0().truncate().text_color(t.muted).child(k.to_string()))
                .child(div().flex_1().min_w_0().text_color(t.text).child(v.to_string()))
        }))
        .into_any_element()
}

fn nested_card(section: &Section<'_>, t: Theme) -> AnyElement {
    let head = nested_head(section);
    let (name, tone) = glyph(head.status, t);
    let dim = |s: String| div().text_color(t.muted).child(s);
    let rest = head.rest.join("\n");
    div()
        .ml(px(theme::space::LG))
        .my_1()
        .flex()
        .flex_col()
        .child(
            div()
                .flex()
                .items_center()
                .gap_2()
                .text_size(px(theme::size::SMALL))
                .font_family(theme::code_font())
                .child(crate::icons::icon(name, tone))
                .child(div().font_weight(FontWeight::SEMIBOLD).text_color(t.text).child(head.tool.to_string()))
                .when(!head.args.is_empty(), |d| {
                    d.child(div().min_w_0().truncate().text_color(t.muted).child(head.args.clone()))
                })
                .when(head.status != "ok" && !head.status.is_empty(), |d| {
                    d.child(div().text_color(tone).child(head.status.to_string()))
                })
                .when(!head.rule.is_empty(), |d| d.child(dim(format!("rule {}", head.rule))))
                .when(!head.duration.is_empty(), |d| d.child(dim(format!("{} ms", head.duration))))
                .when(!head.size.is_empty(), |d| d.child(dim(format!("{} B", head.size)))),
        )
        .when(!rest.is_empty(), |d| {
            d.children(crate::transcript::detail_children(&rest, t))
        })
        .into_any_element()
}

/// Structured rendering of a `code` tool's detail text.
pub(crate) fn code_detail(detail: &str, t: Theme) -> AnyElement {
    let mut children: Vec<AnyElement> = Vec::new();
    for section in split_sections(detail) {
        if is_nested(section.title) {
            children.push(nested_card(&section, t));
            continue;
        }
        let title = div()
            .pt_1()
            .font_weight(FontWeight::SEMIBOLD)
            .text_size(px(theme::size::CAPTION))
            .text_color(t.muted)
            .child(section.title.to_string());
        if section.title == "Code" {
            children.push(title.into_any_element());
            children.push(code_block(program_lines(&section), t));
        } else if is_header(section.title) {
            // `code · <purpose>` is the card header; its rows are the call facts.
            children.push(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .pb_1()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_size(px(theme::size::UI))
                    .text_color(t.text)
                    .child(crate::icons::icon("code", t.brand))
                    .child(section.title.to_string())
                    .into_any_element(),
            );
            children.extend(crate::transcript::detail_children(&section.lines.join("\n"), t));
        } else if is_store(section.title) {
            children.push(title.into_any_element());
            children.push(table(table_rows(&section), t));
        } else {
            children.push(title.into_any_element());
            children.extend(crate::transcript::detail_children(&section.lines.join("\n"), t));
        }
    }
    div()
        .ml(px(theme::space::LG))
        .mb_1()
        .pl_3()
        .border_l_1()
        .border_color(t.border)
        .flex()
        .flex_col()
        .children(children)
        .into_any_element()
}

#[cfg(test)]
mod tests {
    use super::*;
    const DETAIL: &str = "Overview\n  Status: done\n\nCode\n  code:\n    1  x = 1\n    2  \n    3  print(x)\n\nNested call 1\n  Tool: Read\n  Kind: tool\n  Status: denied\n  Duration (ms): 12\n  Permission.rule: deny Read(.env)\n  Arguments.path: .env\n\nNested call 2\n  Tool: Grep\n  Status: ok\n  Arguments.pattern: foo\n  Output:\n    a.py:1";

    #[core::prelude::v1::test]
    fn sections_split_on_unindented_titles() {
        let s = split_sections(DETAIL);
        assert_eq!(s.iter().map(|s| s.title).collect::<Vec<_>>(), ["Overview", "Code", "Nested call 1", "Nested call 2"]);
        assert!(is_code_detail(DETAIL));
        assert!(!is_code_detail("Overview\n  Status: done"));
    }
    #[core::prelude::v1::test]
    fn program_is_numbered_with_blank_lines_kept() {
        let s = split_sections(DETAIL);
        let lines = program_lines(&s[1]);
        assert_eq!(lines, vec![("1", "x = 1"), ("2", ""), ("3", "print(x)")]);
    }
    #[core::prelude::v1::test]
    fn store_rows_form_a_two_column_table_and_header_is_recognised() {
        let d = "Code · scan\n  Status: ok\n\nStore writes\n  k: v\n  old: deleted";
        let s = split_sections(d);
        assert!(is_header(s[0].title) && is_code_detail(d));
        assert_eq!(table_rows(&s[1]), vec![("k", "v"), ("old", "deleted")]);
    }
    #[core::prelude::v1::test]
    fn denied_nested_row_shows_rule_and_compact_args() {
        let s = split_sections(DETAIL);
        let h = nested_head(&s[2]);
        assert_eq!((h.tool, h.status, h.duration), ("Read", "denied", "12"));
        assert_eq!(h.rule, "deny Read(.env)");
        assert_eq!(h.args, "path=.env");
        assert_eq!(h.rest.len(), 2);
        let ok = nested_head(&s[3]);
        assert_eq!(ok.rest, vec!["  Arguments.pattern: foo", "  Output:", "    a.py:1"]);
    }
}
