//! Verb groups (GROK_BUILD_TUI_PLAN §4): one quiet summary row per run of tool calls.
//!
//! Header: `▸ Ran 2 commands, Read 3 files · 1.8s · 1 failed`. While anything runs the
//! header shows the present-tense label brightly (`Running 1 command`) with the live
//! elapsed time; finished, the past-tense label is subdued, the seconds are quiet and the
//! failure count is a dark red (`error_deep`). Folded, only a live tail (`╰─ ⠋ Read path`)
//! shows under the header; a failure does not expand a group, the user opens it.
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
    let failed = m.status == "failed";
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
    } else if failed {
        glyphs.failed
    } else if open {
        look::corner_top()
    } else {
        glyphs.closed
    };
    let corner_tone = if failed {
        p.error
    } else if running {
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
            Style::default().fg(tone).add_modifier(Modifier::BOLD),
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
    let glyph = if running {
        look::SLOT
    } else if b.collapsed {
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
    let failure = (b.failures > 0).then(|| format!(" · {} failed", b.failures));
    let failure_width = failure.as_deref().map_or(0, |f| f.width());
    let mut duration = seconds.map(|s| format!(" · {s}"));
    // The failure count is reserved first; the duration only stays while the label keeps room.
    if let Some(d) = &duration {
        if budget.saturating_sub(LEAD + failure_width + d.width()) < MIN_LABEL {
            duration = None;
        }
    }
    let mut suffix: Vec<Span<'static>> = Vec::new();
    if let Some(duration) = duration {
        suffix.push(Span::styled(duration, Style::default().fg(p.quiet)));
    }
    if let Some(failure) = failure {
        let room = budget.saturating_sub(LEAD + 1);
        let text = if failure.width() <= budget.saturating_sub(LEAD + 1) {
            failure
        } else if failure.trim_start_matches([' ', '·']).trim_start().width() <= room {
            // Too narrow for the separator: keep the count readable instead.
            format!(" {}", failure.trim_start_matches([' ', '·']).trim_start())
        } else {
            truncate(failure.trim_start_matches([' ', '·']).trim_start(), room)
        };
        suffix.push(Span::styled(text, Style::default().fg(p.error_deep)));
    }
    let used: usize = suffix.iter().map(|s| s.content.width()).sum();
    let style = if running {
        look::live(Style::default().fg(p.text).add_modifier(Modifier::BOLD))
    } else {
        Style::default().fg(p.muted).add_modifier(Modifier::BOLD)
    };
    let mut spans = vec![
        gutter(),
        Span::styled(
            glyph,
            Style::default().fg(if running { p.text } else { p.quiet }),
        ),
        Span::raw(" "),
        Span::styled(truncate(label, budget.saturating_sub(LEAD + used)), style),
    ];
    spans.extend(suffix);
    Line::from(spans)
}

/// Draw one group at `now_ms` (the live elapsed time).
pub fn draw(out: &mut Rows, b: &Content, width: usize, p: &Palette, now_ms: u64) {
    out.push((header(b, width, p, now_ms), b.operation.clone()));
    // A failure never opens a group: folded stays folded (only the count shows in the header).
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
    fn finished_header_is_subdued_with_quiet_seconds_and_error_deep_failures() {
        let p = Palette::new(false);
        let mut block = group();
        block.failures = 2;
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        assert_eq!(
            plain(&rows),
            ["    ▸ Ran 2 commands, Read 3 files · 1.8s · 2 failed"]
        );
        let spans = &rows[0].0.spans;
        assert_eq!(spans[3].style.fg, Some(p.muted));
        assert!(!look::is_live(&spans[3]));
        assert_eq!(spans[4].style.fg, Some(p.quiet));
        assert_eq!(spans[5].style.fg, Some(p.error_deep));
        assert_ne!(p.error_deep, p.error);
        assert_eq!(rows[0].1, block.operation);
        let light = Palette::new(true);
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &light, 0);
        assert_eq!(rows[0].0.spans[5].style.fg, Some(light.error_deep));
        assert_ne!(light.error_deep, light.error);
    }

    #[test]
    fn running_header_is_bright_present_tense_with_live_seconds() {
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
            [format!(
                "    {} Running 1 command, Read 3 files · 2.4s",
                look::SLOT
            )]
        );
        let label = &rows[0].0.spans[3];
        assert_eq!(label.style.fg, Some(p.text));
        assert!(look::is_live(label));
    }

    #[test]
    fn running_header_glyph_is_the_spinner_slot_folded_or_open() {
        let p = Palette::new(false);
        let mut block = group();
        block.status = "running".into();
        for collapsed in [true, false] {
            block.collapsed = collapsed;
            let mut rows = Vec::new();
            draw(&mut rows, &block, 80, &p, 0);
            let glyph = &rows[0].0.spans[1];
            assert_eq!(glyph.content, look::SLOT);
            assert_eq!(glyph.style.fg, Some(p.text));
        }
        // Finished, the slot gives way to the disclosure glyph.
        block.status = "completed".into();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        assert_ne!(rows[0].0.spans[1].content, look::SLOT);
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
        assert!(lines[0].contains("1 failed"));
        assert_eq!(lines[1], format!("    ╰─ {} Read src/main.rs", look::SLOT));
        assert!(look::is_live(&rows[1].0.spans[5]));
        // Finished groups show no tail.
        block.status = "completed".into();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p, 0);
        assert_eq!(rows.len(), 1);
    }

    #[test]
    fn finished_group_with_failures_stays_collapsed_like_one_without() {
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
        assert!(plain(&b)[0].contains("· 1 failed"));
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
    fn narrow_header_keeps_the_failure_suffix_readable() {
        let p = Palette::new(false);
        let mut block = group();
        block.failures = 2;
        block.failed_members = vec![read("bad", "failed")];
        for width in [50usize, 30] {
            let mut rows = Vec::new();
            draw(&mut rows, &block, width, &p, 0);
            let lines = plain(&rows);
            assert!(lines[0].contains("2 failed"), "{width}: {lines:?}");
            assert!(
                rows.iter().all(|(row, _)| row.width() <= width),
                "{width}: {lines:?}"
            );
            assert!(lines[0].width() <= width);
            assert_eq!(
                rows[0].0.spans.last().unwrap().style.fg,
                Some(p.error_deep)
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
