//! Transcript text projections and small state helpers.
//!
//! Keep the source transcript intact for copy/export. Projections in this
//! module are presentation-only and make intentional omissions explicit.

/// Message-level text selectable as one logical message. Includes all source
/// blocks and preserves tool labels/output instead of silently dropping them.
pub fn message_source<'a>(blocks: impl IntoIterator<Item = (&'a str, &'a str)>) -> String {
    let mut result = String::new();
    for (kind, text) in blocks {
        if text.is_empty() {
            continue;
        }
        if !result.is_empty() {
            result.push_str("\n\n");
        }
        if kind == "tool" || kind == "tool_group" || kind == "thought" {
            result.push_str(if kind == "thought" { "Thought" } else { "Tool" });
            result.push_str(":\n");
        } else if kind != "user" && kind != "markdown" && kind != "text" {
            result.push_str(kind);
            result.push_str(":\n");
        }
        result.push_str(text);
    }
    result
}

/// Auto-scroll while the viewport still reaches the newest item. `visible_end`
/// is the exclusive end of the list's visible range, so reaching the last item
/// (`visible_end >= item_count`) keeps follow on; scrolling above it turns it
/// off. This tolerates sub-item nudges that a per-pixel check would break.
pub fn follow_from_visible(visible_end: usize, item_count: usize) -> bool {
    item_count > 0 && visible_end >= item_count
}

/// A concise fold summary that always announces the number of hidden lines.
pub fn folded_code_summary(code: &str) -> String {
    format!("Show code ({} lines)", code.lines().count())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projection_keeps_labelled_tool_output() {
        let text = message_source([
            ("user", "Question"),
            ("tool", "Read: file.rs\nfull output"),
            ("markdown", "Answer"),
        ]);
        assert_eq!(
            text,
            "Question\n\nTool:\nRead: file.rs\nfull output\n\nAnswer"
        );
    }

    #[test]
    fn follow_tracks_whether_the_newest_item_is_visible() {
        assert!(follow_from_visible(10, 10));
        assert!(follow_from_visible(11, 10));
        assert!(!follow_from_visible(9, 10));
        assert!(!follow_from_visible(0, 0));
    }

    #[test]
    fn fold_summary_announces_hidden_lines() {
        assert_eq!(folded_code_summary("one\ntwo\n"), "Show code (2 lines)");
    }
}
