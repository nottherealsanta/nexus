//! Verb groups (GROK_BUILD_TUI_PLAN §4): one quiet summary row per run of tool calls.
//!
//! Header, finished: `▸ Ran 2 commands, Read 3 files · 1.8s`, in a light weight with no
//! failure or thought counts; subdued, the seconds quiet. While anything runs it is the
//! literal `▾ Working` (no count, no time, no spinner) whose text shimmers (a [`look::live`]
//! span). A running group shows its newest [`TREE_ROWS`] members even when folded, as a
//! grey tree (`├─ Read path` / `╰─ Read path`, `… N earlier` first when clipped): no icons,
//! no status colour; a failed member ends in a quiet ` · failed`. Failures never turn red
//! and never expand a group. When the group finishes, `transcript::fold` removes the rows
//! one by one and the header flashes (`Content::flash`) before settling to muted.
//!
//! An expanded member is a small box: `┌ Read path  0.8s`, the relevant rows or reasoning
//! behind a `│` rail that closes as `╰` on the last row. A collapsed member is one row,
//! `▸ Read path  0.8s`. The seconds always ride on the head row; a running shell counts up
//! from `started_ms`. Every row of a member carries its toggle and `inspect_operation`.
use super::{look, thought, tools, truncate, Rows};
use crate::{bridge::Content, render::Palette};
use nexus_widgets::theme::mix;
use ratatui::{
    style::{Modifier, Style},
    text::{Line, Span},
};
use unicode_width::UnicodeWidthStr;

/// Members a running group shows under its folded header (the rest is `… N earlier`).
pub const TREE_ROWS: usize = 5;

/// The quiet word a failed member row ends with (never red: decision 2 of the overhaul).
const FAILED_NOTE: &str = " · failed";

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

/// Wall clock in epoch milliseconds, for the live time of a running shell.
fn now_ms() -> u64 {
    (crate::live_bar::now_epoch() * 1000.0) as u64
}

fn is_shell(m: &Content) -> bool {
    matches!(m.title.to_lowercase().as_str(), "bash" | "shell" | "run")
}

/// Seconds of a member: `elapsed_ms` once finished; a running shell counts up from the
/// projection's `started_ms` against `now_ms` (the projection holds no clock).
fn took(m: &Content, now_ms: u64) -> Option<String> {
    if m.status == "running" {
        return (is_shell(m) && m.started_ms > 0 && now_ms > m.started_ms)
            .then(|| seconds(now_ms - m.started_ms));
    }
    (m.elapsed_ms > 0).then(|| seconds(m.elapsed_ms))
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
fn member(out: &mut Rows, m: &Content, width: usize, p: &Palette, now_ms: u64) {
    let glyphs = look::glyphs();
    let running = m.status == "running";
    let (verb, rest) = verb_rest(m);
    let open = if m.kind == "thought" {
        !m.detail.is_empty()
    } else {
        tools::expanded(m)
    };
    let op = tools::with_inspect(&m.operation, &m.inspect_operation);
    // Grey whatever happened; a failure is only the quiet word at the end of the row.
    let tone = p.muted;
    let corner = if running {
        look::SLOT
    } else if open {
        look::corner_top()
    } else {
        glyphs.closed
    };
    let corner_tone = if running { p.text } else { p.border_strong };
    // The seconds ride on the head row, open or closed (a thought's title already says them).
    let time = took(m, now_ms).filter(|_| !(open && m.kind == "thought"));
    // The text gives way before the seconds do.
    let reserve = time.as_ref().map_or(0, |time| time.width() + 2);
    let mut head = vec![
        gutter(),
        Span::styled(corner, Style::default().fg(corner_tone)),
        Span::raw(" "),
        Span::styled(
            truncate(&verb, width.saturating_sub(7)),
            Style::default().fg(tone),
        ),
    ];
    let note = (m.status == "failed").then_some(FAILED_NOTE);
    let note_width = note.map_or(0, |note| note.width());
    let shown = truncate(&rest, width.saturating_sub(11 + reserve + note_width));
    if !shown.is_empty() {
        head.push(Span::styled(format!(" {shown}"), Style::default().fg(tone)));
    }
    if let Some(note) = note {
        head.push(Span::styled(note, Style::default().fg(p.quiet)));
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
    let first_body = out.len();
    if m.kind == "thought" {
        thought::trace(out, m, &rail, width, p);
    } else if m.signal.is_empty() && m.output_operation.is_some() {
        // Every body row belongs to the member's own toggle (and its `i` inspector), also
        // the labelled-details fallback, which a standalone row ties to its output toggle.
        let mut attached = m.clone();
        attached.output_operation = None;
        tools::body(out, &attached, &rail, width, p, &op);
    } else {
        tools::body(out, m, &rail, width, p, &op);
    }
    // The box closes on its last body row (`╰ …`), so no row is spent on a footer.
    if m.members.is_empty() && out.len() > first_body {
        if let Some((line, _)) = out.last_mut() {
            if let Some(span) = line.spans.get_mut(1) {
                if span.content == glyphs.rail {
                    span.content = look::corner_end().into();
                }
            }
        }
    }
    for child in &m.members {
        out.extend(super::build_inner(child, width as u16, p));
    }
}

/// The newest members of a running, folded group as a grey tree: `├─ Read path`, the last
/// one `╰─`, and `… N earlier` first when the host clipped or more than [`TREE_ROWS`] came.
/// No icons, no status colour; the running member's text shimmers.
fn tree(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let glyphs = look::glyphs();
    let from = b.members.len().saturating_sub(TREE_ROWS);
    let earlier = b.hidden + from;
    if earlier > 0 {
        out.push((
            Line::from(vec![
                gutter(),
                Span::styled(
                    truncate(&format!("… {earlier} earlier"), width.saturating_sub(4)),
                    Style::default().fg(p.quiet),
                ),
            ]),
            b.operation.clone(),
        ));
    }
    for (index, m) in b.members[from..].iter().enumerate() {
        let last = from + index + 1 == b.members.len();
        let (verb, rest) = verb_rest(m);
        let text = if rest.is_empty() {
            verb
        } else {
            format!("{verb} {rest}")
        };
        let note = (m.status == "failed").then_some(FAILED_NOTE);
        let note_width = note.map_or(0, |note| note.width());
        let style = Style::default().fg(p.muted);
        let style = if m.status == "running" {
            look::live(style)
        } else {
            style
        };
        let mut row = vec![
            gutter(),
            Span::styled(
                if last {
                    glyphs.tree_end
                } else {
                    glyphs.tree_mid
                },
                Style::default().fg(p.border),
            ),
            Span::raw(" "),
            Span::styled(
                truncate(&text, width.saturating_sub(7 + note_width + RIGHT_MARGIN)),
                style,
            ),
        ];
        if let Some(note) = note {
            row.push(Span::styled(note, Style::default().fg(p.quiet)));
        }
        out.push((
            Line::from(row),
            tools::with_inspect(&b.operation, &m.inspect_operation),
        ));
    }
}

/// The header row of a group.
fn header(b: &Content, width: usize, p: &Palette) -> Line<'static> {
    let glyphs = look::glyphs();
    let running = b.status == "running";
    // Running, the tree (or boxes) hang open under `Working`; finished and folded it closes.
    let glyph = if running || !b.collapsed {
        glyphs.open
    } else {
        glyphs.closed
    };
    let label = if running {
        if b.running_label.is_empty() {
            "Working"
        } else {
            b.running_label.as_str()
        }
    } else {
        b.text.as_str()
    };
    // Running: no count, no time. The clock appears once the group has finished.
    let seconds = if running {
        None
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
    // The fold's flash (accent -> muted) tints the label and the glyph for a few frames.
    let flash = f32::from(b.flash.min(100)) / 100.0;
    let style = Style::default().fg(mix(p.muted, p.accent, flash));
    let glyph_tone = if running {
        p.muted
    } else {
        mix(p.quiet, p.accent, flash)
    };
    // The running label shimmers muted -> text on the draw loop's clock.
    let style = if running { look::live(style) } else { style };
    let mut spans = vec![
        gutter(),
        Span::styled(glyph, Style::default().fg(glyph_tone)),
        Span::raw(" "),
        Span::styled(truncate(label, budget.saturating_sub(LEAD + used)), style),
    ];
    spans.extend(suffix);
    Line::from(spans)
}

/// Draw one group: its header, then the member boxes when opened, or the tree of the
/// newest members while it runs folded.
pub fn draw(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    out.push((header(b, width, p), b.operation.clone()));
    // A failure never opens a group; failure details are shown on the individual member rows.
    if !b.collapsed && !b.members.is_empty() {
        let now = now_ms();
        for m in &b.members {
            member(out, m, width, p, now);
        }
    } else if b.status == "running" && !b.members.is_empty() {
        tree(out, b, width, p);
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
            running_label: "Working".into(),
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
        draw(&mut rows, &block, 80, &p);
        assert_eq!(plain(&rows), ["    ▸ Ran 2 commands, Read 3 files · 1.8s"]);
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
        draw(&mut rows, &block, 80, &p);
        assert_eq!(rows[0].0.spans[1].style.fg, Some(p.quiet));
    }

    fn running(members: usize) -> Content {
        let mut block = group();
        block.status = "running".into();
        block.started_ms = 4_000;
        block.elapsed_ms = 0;
        block.elapsed_label.clear();
        block.members = (0..members)
            .map(|i| {
                let mut m = read(&format!("m{i}"), "completed");
                m.text = format!("src/f{i}.rs");
                m.heading = format!("→ Read src/f{i}.rs");
                m
            })
            .collect();
        if let Some(last) = block.members.last_mut() {
            last.status = "running".into();
        }
        block
    }

    #[test]
    fn running_header_is_exactly_working_with_no_count_time_or_spinner() {
        let p = Palette::new(false);
        let mut block = running(0);
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        assert_eq!(plain(&rows), ["    ▾ Working"]);
        assert!(!plain(&rows)[0].chars().any(|c| c.is_ascii_digit()));
        assert!(!plain(&rows)[0].contains(look::SLOT));
        let label = &rows[0].0.spans[3];
        assert_eq!(label.style.fg, Some(p.muted));
        assert!(look::is_live(label), "the label shimmers");
        assert_eq!(rows[0].0.spans[1].style.fg, Some(p.muted));
        // The chevron is open whether the user folded the group or not.
        block.collapsed = false;
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        assert_eq!(rows[0].0.spans[1].content, "▾");
    }

    #[test]
    fn running_group_folded_draws_a_grey_tree_of_its_members() {
        let p = Palette::new(false);
        let block = running(3);
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        assert_eq!(
            plain(&rows),
            [
                "    ▾ Working",
                "    ├─ Read src/f0.rs",
                "    ├─ Read src/f1.rs",
                "    ╰─ Read src/f2.rs",
            ]
        );
        for (row, _) in &rows[1..] {
            assert_eq!(row.spans[1].style.fg, Some(p.border));
            assert_eq!(row.spans[3].style.fg, Some(p.muted));
        }
        // Only the running member shimmers; clicking a row still toggles the group.
        assert!(!look::is_live(&rows[1].0.spans[3]));
        assert!(look::is_live(&rows[3].0.spans[3]));
        assert_eq!(rows[2].1.as_ref().unwrap()["id"], "g");
        assert_eq!(rows[2].1.as_ref().unwrap()["inspect_operation"]["id"], "m1");
    }

    #[test]
    fn tree_has_no_icons_or_status_colours() {
        let p = Palette::new(false);
        let mut block = running(4);
        block.members[1].status = "failed".into();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        let text = plain(&rows).join("\n");
        for glyph in ["✓", "✗", "✔", "✘", look::SLOT] {
            assert!(!text.contains(glyph), "{glyph}: {text}");
        }
        for (row, _) in &rows {
            for span in &row.spans {
                assert_ne!(span.style.fg, Some(p.error));
                assert_ne!(span.style.fg, Some(p.success));
            }
        }
    }

    #[test]
    fn seven_members_show_two_earlier_and_the_newest_five() {
        let p = Palette::new(false);
        let block = running(7);
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        let lines = plain(&rows);
        assert_eq!(lines.len(), 1 + 1 + TREE_ROWS, "{lines:#?}");
        assert_eq!(lines[1], "    … 2 earlier");
        assert_eq!(rows[1].0.spans[1].style.fg, Some(p.quiet));
        assert_eq!(lines[2], "    ├─ Read src/f2.rs");
        assert_eq!(lines[6], "    ╰─ Read src/f6.rs");
        // The host may already have clipped: its count joins ours.
        let mut clipped = running(5);
        clipped.hidden = 3;
        let mut rows = Vec::new();
        draw(&mut rows, &clipped, 80, &p);
        assert_eq!(plain(&rows)[1], "    … 3 earlier");
        clipped.members.truncate(2);
        clipped.hidden = 0;
        let mut rows = Vec::new();
        draw(&mut rows, &clipped, 80, &p);
        assert!(!plain(&rows).iter().any(|line| line.contains("earlier")));
    }

    #[test]
    fn failed_member_row_ends_in_a_quiet_failed_word() {
        let p = Palette::new(false);
        let mut block = running(3);
        block.members[0].status = "failed".into();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        let lines = plain(&rows);
        assert_eq!(lines[1], "    ├─ Read src/f0.rs · failed");
        let note = rows[1].0.spans.last().unwrap();
        assert_eq!(note.content, " · failed");
        assert_eq!(note.style.fg, Some(p.quiet));
        // Narrow panes clip the target, never the note.
        let mut rows = Vec::new();
        draw(&mut rows, &block, 20, &p);
        assert!(plain(&rows)[1].ends_with(" · failed"));
        assert!(rows.iter().all(|(row, _)| row.width() <= 20));
    }

    #[test]
    fn finished_group_has_no_tree_and_the_flash_tints_its_header() {
        let p = Palette::new(false);
        let mut block = running(3);
        block.status = "completed".into();
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        assert_eq!(rows.len(), 1);
        assert!(plain(&rows)[0].starts_with("    ▸ Ran 2 commands"));
        block.flash = 100;
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        assert_eq!(rows[0].0.spans[3].style.fg, Some(p.accent));
        block.flash = 50;
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        let tone = rows[0].0.spans[3].style.fg;
        assert_ne!(tone, Some(p.accent));
        assert_ne!(tone, Some(p.muted));
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
        draw(&mut a, &clean, 80, &p);
        draw(&mut b, &failed, 80, &p);
        assert_eq!(a.len(), 1);
        assert_eq!(b.len(), 1, "{:?}", plain(&b));
        assert!(plain(&b)[0].starts_with("    ▸ "));
        assert!(!plain(&b)[0].contains("failed"));
        assert_eq!(plain(&a), plain(&b));
        assert_eq!(b[0].0.spans[1].style.fg, Some(p.quiet));
    }

    #[test]
    fn failed_member_inside_an_opened_group_is_grey_with_a_quiet_word() {
        let p = Palette::new(false);
        let mut block = group();
        block.collapsed = false;
        block.failures = 1;
        block.members = vec![read("bad", "failed")];
        let mut rows = Vec::new();
        draw(&mut rows, &block, 80, &p);
        let lines = plain(&rows);
        assert!(!lines[0].contains("failed"));
        // The quiet word comes before the seconds a collapsed row always ends with.
        assert!(lines[1].contains("Read src/main.rs · failed"), "{lines:?}");
        assert!(lines[1].ends_with(" · failed  0.8s"), "{lines:?}");
        assert!(rows[1].0.spans.iter().all(|s| s.style.fg != Some(p.error)));
        assert_eq!(rows[1].0.spans.last().unwrap().style.fg, Some(p.quiet));
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
        draw(&mut rows, &block, 80, &p);
        assert_eq!(
            plain(&rows),
            [
                "    ▾ Ran 2 commands, Read 3 files · 1.8s",
                "    ┌ Read src/main.rs  0.8s",
                "    │ 1  fn main() {}",
                "    ╰ … 112 lines",
                "    ▸ Read src/main.rs  0.8s",
            ]
        );
        let last = rows[1].0.spans.last().unwrap();
        assert!(last.style.add_modifier.contains(Modifier::ITALIC));
        // Head and every body row share the member's toggle and its inspector.
        for index in [1, 2, 3] {
            let op = rows[index].1.as_ref().unwrap();
            assert_eq!(op["id"], "a:detail");
            assert_eq!(op["inspect_operation"]["id"], "a");
        }
        // Column alignment: connectors share the header glyph's column.
        for index in [1, 2, 3, 4] {
            assert!(plain(&rows)[index].starts_with("    "));
            assert!(!plain(&rows)[index].starts_with("     "));
        }
    }

    #[test]
    fn running_shell_counts_up_from_its_start_and_a_finished_one_shows_its_time() {
        let p = Palette::new(false);
        let mut shell = read("s", "running");
        shell.title = "Bash".into();
        shell.text = "sleep 9".into();
        shell.elapsed_ms = 0;
        shell.started_ms = 10_000;
        shell.signal = vec![("code".into(), "$ sleep 9".into())];
        let mut rows = Vec::new();
        member(&mut rows, &shell, 80, &p, 13_200);
        assert_eq!(
            plain(&rows),
            [
                format!("    {} Bash sleep 9  3.2s", look::SLOT),
                "    ╰ $ sleep 9".to_string()
            ]
        );
        // A running read has no live clock, and a finished shell shows its recorded time.
        let mut reading = read("r", "running");
        reading.elapsed_ms = 0;
        reading.started_ms = 10_000;
        let mut rows = Vec::new();
        member(&mut rows, &reading, 80, &p, 13_200);
        assert!(!plain(&rows)[0].contains("3.2s"));
        shell.status = "completed".into();
        shell.elapsed_ms = 2_500;
        let mut rows = Vec::new();
        member(&mut rows, &shell, 80, &p, 99_000);
        assert!(plain(&rows)[0].ends_with("Bash sleep 9  2.5s"));
    }

    #[test]
    fn detail_fallback_rows_stay_on_the_members_toggle() {
        let p = Palette::new(false);
        let mut open = read("a", "completed");
        open.detail = "  Label: value\nplain".into();
        open.output_operation = Some(json!({"kind":"block_toggle","id":"a:output"}));
        let mut rows = Vec::new();
        member(&mut rows, &open, 80, &p, 0);
        assert!(rows.len() >= 3);
        for (_, op) in &rows {
            assert_eq!(op.as_ref().unwrap()["id"], "a:detail");
            assert_eq!(op.as_ref().unwrap()["inspect_operation"]["id"], "a");
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
        draw(&mut rows, &block, 80, &p);
        let lines = plain(&rows);
        assert_eq!(lines[1], "    ┌ Thought for 1.1s");
        assert_eq!(lines[2], "    │ Plan");
        assert_eq!(lines[3], "    ╰ read first");
        assert_eq!(lines.len(), 4);
    }

    #[test]
    fn narrow_header_clips_inside_the_width_and_keeps_the_seconds_when_they_fit() {
        let p = Palette::new(false);
        let mut block = group();
        block.failures = 2;
        block.failed_members = vec![read("bad", "failed")];
        for width in [50usize, 30] {
            let mut rows = Vec::new();
            draw(&mut rows, &block, width, &p);
            let lines = plain(&rows);
            assert!(!lines[0].contains("failed"), "{width}: {lines:?}");
            assert!(
                rows.iter().all(|(row, _)| row.width() <= width),
                "{width}: {lines:?}"
            );
            assert_eq!(lines.len(), 1, "{width}: {lines:?}");
        }
        let mut rows = Vec::new();
        draw(&mut rows, &block, 50, &p);
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
            draw(&mut rows, &block, width, &p);
            assert!(
                rows.iter().all(|(row, _)| row.width() <= width),
                "{width}: {:?}",
                plain(&rows)
            );
        }
    }
}
