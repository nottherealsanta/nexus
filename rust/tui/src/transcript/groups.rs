//! Verb groups (GROK_BUILD_TUI_PLAN §4): one quiet summary row per run of tool calls.
//!
//! Header: `▸ Ran 2 commands, Read 3 files · 1.8s`, in a light weight with no failure or
//! thought counts and no spinner or animation. While anything runs the label is the
//! present-tense one (`Running 1 command`) with the live elapsed time; finished, it is
//! subdued and the seconds are quiet. Failures are shown on their individual member rows.
//! Folded, only a live tail (`╰─ ⠋ Read path`) shows under the header; a failure does not
//! expand a group.
//!
//! An expanded member is a small box: `┌ Read path`, the relevant rows or reasoning behind
//! a `│` rail, then `╰ 0.8s` in italics. A collapsed member is one row, `▸ Read path  0.8s`.
use super::{look, thought, tools, truncate, Rows};
use crate::{bridge::Content, render::Palette};
use ratatui::{
    style::{Modifier, Style},
    text::{Line, Span},
};
use unicode_width::UnicodeWidthStr;

/// `Read` and `rest` of a member row; a thought is `Thought` and `for 3.2s`.
fn verb_rest(m: &Content) -> (String, String) {
    let heading = m.heading.trim_start_matches(['→', ' ']);
    let (verb, rest) = if m.kind == "thought" {
        m.title.split_once(' ').unwrap_or((m.title.as_str(), ""))
    } else if !m.title.is_empty() {
        (m.title.as_str(), m.text.as_str())
    } else {
        heading.split_once(' ').unwrap_or((heading, ""))
    };
    let rest = tools::relevant(rest.lines().next().unwrap_or(""));
    (super::capitalized(verb), rest.to_string())
}

/// Seconds of a finished member, from the projection's `elapsed_ms`.
fn took(m: &Content) -> Option<String> {
    (m.elapsed_ms > 0 && m.status != "running").then(|| seconds(m.elapsed_ms))
}

/// Member times are always seconds (`0.8s`), never milliseconds; minutes keep the shared form.
fn seconds(ms: u64) -> String {
    if ms < 60_000 {
        format!("{:.1}s", ms.max(100) as f64 / 1000.0)
    } else {
        look::format_time(ms)
    }
}

/// Columns kept free at the right edge of a header row.
const RIGHT_MARGIN: usize = 1;

fn gutter() -> Span<'static> {
    Span::raw("    ")
}

/// One member, collapsed or expanded.
fn member(out: &mut Rows, m: &Content, width: usize, p: &Palette) {
    let glyphs = look::glyphs();
    let running = m.status == "running";
    let (verb, rest) = verb_rest(m);
    let open = if m.kind == "thought" {
        !m.detail.is_empty()
    } else {
        tools::expanded(m)
    };
    let op = tools::with_inspect(&m.operation, &m.inspect_operation);
    let tone = tools::tone(&m.status, p);
    let corner = if running {
        look::SLOT
    } else if open {
        look::corner_top()
    } else {
        glyphs.closed
    };
    let corner_tone = if running {
        p.text
    } else {
        p.border_strong
    };
    let time = took(m);
    // A collapsed row ends with its seconds; the text gives way before they do.
    let reserve = match (&time, open) {
        (Some(time), false) => time.width() + 2,
        _ => 0,
    };
    let mut head = vec![
        gutter(),
        Span::styled(corner, Style::default().fg(corner_tone)),
        Span::raw(" "),
        Span::styled(
            truncate(&verb, width.saturating_sub(7)),
            Style::default().fg(tone),
        ),
    ];
    let shown = truncate(&rest, width.saturating_sub(11 + reserve));
    if !shown.is_empty() {
        head.push(Span::styled(format!(" {shown}"), Style::default().fg(tone)));
    }
    if reserve > 0 {
        let used: usize = head.iter().map(|s| s.content.width()).sum();
        if used + reserve <= width {
            head.push(Span::styled(
                format!("  {}", time.as_deref().unwrap_or("")),
                Style::default().fg(p.quiet).add_modifier(Modifier::ITALIC),
            ));
        }
    }
    out.push((Line::from(head), op.clone()));
    if !open {
        return;
    }
    let rail = vec![
        gutter(),
        Span::styled(glyphs.rail, Style::default().fg(p.border)),
        Span::raw(" "),
    ];
    if m.kind == "thought" {
        thought::trace(out, m, &rail, width, p);
    } else {
        tools::body(out, m, &rail, width, p, &op);
    }
    for child in &m.members {
        out.extend(super::build_inner(child, width as u16, p));
    }
    if let Some(time) = time {
        out.push((
            Line::from(vec![
                gutter(),
                Span::styled(look::corner_end(), Style::default().fg(p.border)),
                Span::raw(" "),
                Span::styled(
                    time,
                    Style::default().fg(p.quiet).add_modifier(Modifier::ITALIC),
                ),
            ]),
            op,
        ));
    }
}

/// The live member under a folded, running header: `╰─ ⠋ Read path`.
fn tail(out: &mut Rows, m: &Content, width: usize, p: &Palette) {
    let glyphs = look::glyphs();
    let (verb, rest) = verb_rest(m);
    let live = |style: Style| look::live(style);
    let mut row = vec![
        gutter(),
        Span::styled(glyphs.tree_end, Style::default().fg(p.border_strong)),
        Span::raw(" "),
        Span::styled(look::SLOT, Style::default().fg(p.text)),
        Span::raw(" "),
        Span::styled(
            truncate(&verb, width.saturating_sub(9)),
            live(Style::default().fg(p.text).add_modifier(Modifier::BOLD)),
        ),
    ];
    let shown = truncate(&rest, width.saturating_sub(14));
    if !shown.is_empty() {
        row.push(Span::styled(
            format!(" {shown}"),
            Style::default().fg(p.muted),
        ));
    }
    out.push((
        Line::from(row),
        tools::with_inspect(&m.operation, &m.inspect_operation),
    ));
}

/// The header row of a group.
fn header(b: &Content, width: usize, p: &Palette, now_ms: u64) -> Line<'static> {
    let glyphs = look::glyphs();
    let running = b.status == "running";
    // A group never spins: only an individual tool animates. The disclosure glyph stays.
    let glyph = if b.collapsed {
        glyphs.closed
    } else {
        glyphs.open
    };
    let label = if running && !b.running_label.is_empty() {
        &b.running_label
    } else {
        &b.text
    };
    let seconds = if running {
        look::live_elapsed(b.started_ms, now_ms)
    } else if !b.elapsed_label.is_empty() {
        Some(b.elapsed_label.clone())
    } else {
        (b.elapsed_ms > 0).then(|| look::format_time(b.elapsed_ms))
    };
    // Gutter, glyph and space lead the row; the right edge keeps a margin for the
    // scrollbar so the suffix is never clipped by the pane.
    const LEAD: usize = 6;
    const MIN_LABEL: usize = 8;
    let budget = width.saturating_sub(RIGHT_MARGIN);
    let mut duration = seconds.map(|s| format!(" · {s}"));
    // The duration only stays while the label keeps room.
    if let Some(d) = &duration {
        if budget.saturating_sub(LEAD + d.width()) < MIN_LABEL {
            duration = None;
        }
    }
    let mut suffix: Vec<Span<'static>> = Vec::new();
    if let Some(duration) = duration {
        suffix.push(Span::styled(duration, Style::default().fg(p.quiet)));
    }
    let used: usize = suffix.iter().map(|s| s.content.width()).sum();
    // Light weight, no count: a failed member only tints the glyph; the member row says it.
    let style = Style::default().fg(if running { p.text } else { p.muted });
    let glyph_tone = if running { p.text } else { p.quiet };
    let mut spans = vec![
        gutter(),
        Span::styled(glyph, Style::default().fg(glyph_tone)),
        Span::raw(" "),
        Span::styled(truncate(label, budget.saturating_sub(LEAD + used)), style),
    ];
    spans.extend(suffix);
    Line::from(spans)
}

/// Draw one group at `now_ms` (the live elapsed time).
pub fn draw(out: &mut Rows, b: &Content, width: usize, p: &Palette, now_ms: u64) {
    out.push((header(b, width, p, now_ms), b.operation.clone()));
    // A failure never opens a group; failure details are shown on the individual member rows.
    if !b.collapsed && !b.members.is_empty() {
        for m in &b.members {
            member(out, m, width, p);
        }
    } else if b.status == "running" {
        if let Some(live) = b.live_member.as_deref() {
            tail(out, live, width, p);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn plain(rows: &Rows) -> Vec<String> {
        rows.iter().map(|(row, _)| row.to_string()).collect()
    }

    fn read(id: &str, status: &str) -> Content {
        Content {
            id: id.into(),
            kind: "tool".into(),
            title: "Read".into(),
            text: "src/main.rs".into(),
            heading: "→ Read src/main.rs".into(),
            status: status.into(),
            elapsed_ms: 800,
            operation: Some(json!({"kind":"block_toggle","id":format!("{id}:detail")})),
            inspect_operation: Some(json!({"kind":"tool_details","id":id})),
            ..Default::default()
        }
    }

    fn group() -> Content {
        Content {
            id: "g".into(),
            kind: "tool_group".into(),
            text: "Ran 2 commands, Read 3 files".into(),
            running_label: "Running 1 command, Read 3 files".into(),
            status: "completed".into(),
            collapsed: true,
            count: 5,
            elapsed_ms: 1_800,
            elapsed_label: "1.8s".into(),
            operation: Some(json!({"kind":"block_toggle","id":"g"})),
            ..Default::default()
        }
    }

    #[test]
    fn finished_header_is_light_with_quiet_seconds_and_no_failure_signal() {
        let p = Palette::new(false);
        let mut block = group();
        block.failures = 2;
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        assert_eq!(
            plain(&rows),
            ["    ▸ Ran 2 commands, Read 3 files · 1.8s"]
        );
        let spans = &rows[0].0.spans;
        assert_eq!(spans[3].style.fg, Some(p.muted));
        assert!(!spans[3].style.add_modifier.contains(Modifier::BOLD));
        assert!(!look::is_live(&spans[3]));
        assert_eq!(spans[4].style.fg, Some(p.quiet));
        // The heading is quiet; failure status belongs on the individual tool row.
        assert_eq!(spans[1].style.fg, Some(p.quiet));
        assert_eq!(rows[0].1, block.operation);
        block.failures = 0;
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        assert_eq!(rows[0].0.spans[1].style.fg, Some(p.quiet));
    }

    #[test]
    fn running_header_is_present_tense_with_live_seconds_but_never_animated() {
        let p = Palette::new(false);
        let mut block = group();
        block.status = "running".into();
        block.started_ms = 4_000;
        block.elapsed_ms = 0;
        block.elapsed_label.clear();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 6_400);
        assert_eq!(
            plain(&rows),
            ["    ▸ Running 1 command, Read 3 files · 2.4s"]
        );
        let label = &rows[0].0.spans[3];
        assert_eq!(label.style.fg, Some(p.text));
        assert!(!look::is_live(label));
    }

    #[test]
    fn running_header_keeps_the_disclosure_glyph_folded_or_open() {
        let p = Palette::new(false);
        let mut block = group();
        block.status = "running".into();
        for (collapsed, glyph) in [(true, "▸"), (false, "▾")] {
            block.collapsed = collapsed;
            let mut rows = Vec::new();
            draw(&mut rows, &block, 80, &p, 0);
            assert_eq!(rows[0].0.spans[1].content, glyph);
            assert!(rows[0].0.spans.iter().all(|s| !look::is_live(s)));
        }
    }

    #[test]
    fn folded_running_group_shows_only_the_live_tail() {
        let p = Palette::new(false);
        let mut block = group();
        block.status = "running".into();
        block.failures = 1;
        block.failed_members = vec![read("bad", "failed")];
        block.live_member = Some(Box::new(read("now", "running")));
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        let lines = plain(&rows);
        assert_eq!(lines.len(), 2, "{lines:#?}");
        assert!(!lines[0].contains("failed"));
        assert_eq!(lines[1], format!("    ╰─ {} Read src/main.rs", look::SLOT));
        assert!(look::is_live(&rows[1].0.spans[5]));
        // Finished groups show no tail.
        block.status = "completed".into();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        assert_eq!(rows.len(), 1);
    }

    #[test]
    fn finished_group_with_failures_stays_collapsed_and_subdued() {
        let p = Palette::new(false);
        let mut clean = group();
        clean.members = vec![read("ok", "completed")];
        let mut failed = clean.clone();
        failed.failures = 1;
        failed.members = vec![read("bad", "failed")];
        failed.failed_members = vec![read("bad", "failed")];
        let (mut a, mut b) = (Vec::new(), Vec::new());
        draw(&mut a, &clean, 80, &p, 0);
        draw(&mut b, &failed, 80, &p, 0);
        assert_eq!(a.len(), 1);
        assert_eq!(b.len(), 1, "{:?}", plain(&b));
        assert!(plain(&b)[0].starts_with("    ▸ "));
        assert!(!plain(&b)[0].contains("failed"));
        assert_eq!(plain(&a), plain(&b));
        assert_eq!(b[0].0.spans[1].style.fg, Some(p.quiet));
    }

    #[test]
    fn failed_member_stays_red_inside_the_group() {
        let p = Palette::new(false);
        let mut block = group();
        block.collapsed = false;
        block.failures = 1;
        block.members = vec![read("bad", "failed")];
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        let lines = plain(&rows);
        assert!(!lines[0].contains("failed"));
        assert!(lines[1].contains("Read src/main.rs"), "{lines:?}");
        assert!(rows[1].0.spans.iter().any(|s| s.style.fg == Some(p.error)));
    }

    #[test]
    fn expanded_members_are_boxes_with_rails_and_italic_seconds() {
        let p = Palette::new(false);
        let mut block = group();
        block.collapsed = false;
        let mut open = read("a", "completed");
        open.signal = vec![
            ("out".into(), "1  fn main() {}".into()),
            ("clip".into(), "… 112 lines".into()),
        ];
        let closed = read("b", "completed");
        block.members = vec![open, closed];
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        assert_eq!(
            plain(&rows),
            [
                "    ▾ Ran 2 commands, Read 3 files · 1.8s",
                "    ┌ Read src/main.rs",
                "    │ 1  fn main() {}",
                "    │ … 112 lines",
                "    ╰ 0.8s",
                "    ▸ Read src/main.rs  0.8s",
            ]
        );
        let last = rows[4].0.spans.last().unwrap();
        assert!(last.style.add_modifier.contains(Modifier::ITALIC));
        assert_eq!(rows[1].1.as_ref().unwrap()["inspect_operation"]["id"], "a");
        // Column alignment: connectors share the header glyph's column.
        for index in [1, 2, 4, 5] {
            assert!(plain(&rows)[index].starts_with("    "));
            assert!(!plain(&rows)[index].starts_with("     "));
        }
    }

    #[test]
    fn thought_members_open_to_their_trace() {
        let p = Palette::new(false);
        let mut block = group();
        block.collapsed = false;
        block.members = vec![Content {
            id: "t".into(),
            kind: "thought".into(),
            title: "Thought for 1.1s".into(),
            detail: "**Plan**\nread first".into(),
            elapsed_ms: 1_100,
            status: "completed".into(),
            ..Default::default()
        }];
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        let lines = plain(&rows);
        assert_eq!(lines[1], "    ┌ Thought for 1.1s");
        assert_eq!(lines[2], "    │ Plan");
        assert_eq!(lines[3], "    │ read first");
        assert_eq!(lines[4], "    ╰ 1.1s");
    }

    #[test]
    fn narrow_header_clips_inside_the_width_and_keeps_the_seconds_when_they_fit() {
        let p = Palette::new(false);
        let mut block = group();
        block.failures = 2;
        block.failed_members = vec![read("bad", "failed")];
        for width in [50usize, 30] {
            let mut rows = Vec::new();
            draw(&mut rows, &block, width, &p, 0);
            let lines = plain(&rows);
            assert!(!lines[0].contains("failed"), "{width}: {lines:?}");
            assert!(
                rows.iter().all(|(row, _)| row.width() <= width),
                "{width}: {lines:?}"
            );
            assert_eq!(lines.len(), 1, "{width}: {lines:?}");
        }
        let mut rows = Vec::new();
        draw(&mut rows, &block, 50, &p, 0);
        assert!(plain(&rows)[0].contains("· 1.8s"));
    }

    #[test]
    fn rows_never_exceed_the_width() {
        let p = Palette::new(false);
        let mut block = group();
        block.collapsed = false;
        block.failures = 1;
        let mut long = read("a", "completed");
        long.text = "界".repeat(40);
        block.members = vec![long];
        for width in [12, 20, 40] {
            let mut rows = Vec::new();
            draw(&mut rows, &block, width, &p, 0);
            assert!(
                rows.iter().all(|(row, _)| row.width() <= width),
                "{width}: {:?}",
                plain(&rows)
            );
        }
    }
}
