//! D06 — activity groups and tool lines: folded, live (shimmer), expanded, member detail.
use super::d01_tokens::caption;
use super::{Shot, DLM};
use crate::kit::{bold, Seg, P};
use crate::shell::{group, tool, Centre, Shell};
use crate::theme::Mode;
use ratatui::style::Style;

/// One frame of the shimmer: muted to primary around `peak`, smoothstep falloff, reach 4.
fn shimmer(p: &P, text: &str, peak: f32) -> Vec<(String, Style)> {
    let t = p.t;
    text.chars()
        .enumerate()
        .map(|(i, ch)| {
            let d = ((i as f32 - peak).abs() / 4.0).min(1.0);
            let k = 1.0 - d;
            let k = k * k * (3.0 - 2.0 * k);
            let st = if t.mode == Mode::Mono {
                if k > 0.5 { t.primary() } else { t.muted() }
            } else {
                Style::default().fg(t.mix("text.muted", "text.primary", k))
            };
            (ch.to_string(), bold(st))
        })
        .collect()
}

fn live(p: &mut P, c: Centre, y: u16) -> u16 {
    let t = p.t;
    p.put(c.x, y, &[(t.g("open"), t.muted()), (" ", t.muted())]);
    let sh = shimmer(p, "Working", 3.0);
    let segs: Vec<Seg> = sh.iter().map(|(s, st)| (s.as_str(), *st)).collect();
    p.put(c.x + 2, y, &segs);
    p.put_r(c.x + c.w, y, &[("12s", t.muted())]);
    p.put(c.x + 2, y + 1, &[(t.g("ellipsis"), t.muted()), (" 3 earlier", t.muted())]);
    let q = t.secondary();
    tool(p, c, y + 2, false, "Read", "src/nexus/loader.py", &[("1-120", t.muted())], "0.1s", "done");
    tool(p, c, y + 3, false, "Read", "tests/test_loader.py", &[("1-64", t.muted())], "0.1s", "done");
    tool(p, c, y + 4, false, "Search", "\"parse_manifest\" in src", &[("4 matches", q)], "0.2s", "done");
    tool(p, c, y + 5, false, "Edit", "src/nexus/loader.py", &[("+12", t.fg("diff.add")), (" ", t.muted()), ("−3", t.fg("diff.del"))], "0.1s", "done");
    tool(p, c, y + 6, true, "Shell", "pytest tests/test_loader.py -q", &[], "3.4s", "running");
    7
}

/// The old-Nexus detail box for one member; returns rows used.
fn detail(p: &mut P, c: Centre, y: u16) -> u16 {
    let t = p.t;
    let strong = t.fg("border.strong");
    let x = c.x + 2;
    let end = c.x + c.w;
    p.put(x, y, &[("┌", strong), (" ", t.muted()), ("$", t.secondary()), (" ", t.muted()), ("Shell", bold(t.secondary())), ("  ", t.muted()), ("pytest tests/test_loader.py -q", t.secondary())]);
    p.put_r(end, y, &[("3.4s", t.muted())]);
    let chip = t.bg("surface.panel");
    p.put_r(end - 6, y, &[(" raw ", t.secondary().patch(chip))]);
    let mut rows: Vec<(Vec<Seg>, bool)> = Vec::new();
    rows.push((vec![("$ ", t.muted()), ("uv run pytest tests/test_loader.py -q \\", t.primary())], false));
    rows.push((vec![("    -k \"cache or manifest\"", t.primary())], false));
    rows.push((vec![("↑ 214 earlier lines", t.muted()), (" · ", t.muted()), ("enter", bold(t.secondary())), (" show all", t.muted())], false));
    let out = ["tests/test_loader.py::test_load_returns_manifest PASSED", "tests/test_loader.py::test_load_reparses_after_edit PASSED", "tests/test_loader.py::test_cache_hit_skips_parse PASSED", "tests/test_loader.py::test_cache_invalidates_on_mtime PASSED", "tests/test_loader.py::test_missing_file_raises PASSED", "", "==================== 5 passed in 0.42s ===================="];
    for o in out {
        rows.push((vec![(o, if o.contains("passed") { t.fg("status.success") } else { t.secondary() })], false));
    }
    let n = rows.len();
    for (i, (segs, _)) in rows.iter().enumerate() {
        let yy = y + 1 + i as u16;
        let rail = if i + 1 == n { "╰" } else { "│" };
        p.put(x, yy, &[(rail, strong)]);
        p.put_clip(x + 2, yy, segs, end);
    }
    1 + n as u16
}

fn sheet(p: &mut P) {
    let c = Shell { left_w: 34, right_w: 36, busy: crate::shell::Busy::Running(12), ..Shell::default() }.draw(p);
    let t = p.t;
    let mut y = 2;
    caption(p, c.x, y, "folded · done");
    group(p, c, y + 1, &[("Read 2 files", t.secondary()), (", ", t.muted()), ("ran 1 command", t.secondary())], "1.4s", false, false);
    y += 3;
    caption(p, c.x, y, "folded · a failure stays visible");
    group(p, c, y + 1, &[("Edited 1 file", t.secondary()), ("  ", t.muted()), ("+12", t.fg("diff.add")), (" ", t.muted()), ("−3", t.fg("diff.del")), (" · ", t.muted()), (t.g("error"), t.fg("status.error")), (" 1 failed", t.fg("status.error"))], "0.9s", false, false);
    y += 3;
    caption(p, c.x, y, "live · Working shimmers, newest 5 members, one running");
    y += 1 + live(p, c, y + 1) + 1;
    caption(p, c.x, y, "expanded · enter on a member opens its detail");
    y += 1;
    group(p, c, y, &[("Read 1 file", t.secondary()), (", ", t.muted()), ("searched 1 time", t.secondary()), (", ", t.muted()), ("edited 1 file", t.secondary()), (", ", t.muted()), ("ran 1 command", t.secondary()), (" · ", t.muted()), (t.g("error"), t.fg("status.error")), (" 1 failed", t.fg("status.error"))], "4.2s", true, false);
    tool(p, c, y + 1, false, "Read", "src/nexus/loader.py", &[("1-120", t.muted())], "0.1s", "done");
    tool(p, c, y + 2, false, "Search", "\"parse_manifest\" in src", &[("4 matches", t.secondary())], "0.2s", "done");
    tool(p, c, y + 3, false, "Edit", "src/nexus/loader.py", &[("old text not found", t.fg("status.error"))], "0.0s", "error");
    tool(p, c, y + 4, false, "Edit", "src/nexus/loader.py", &[("+12", t.fg("diff.add")), (" ", t.muted()), ("−3", t.fg("diff.del"))], "0.1s", "done");
    detail(p, c, y + 5);
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d06-groups", caption: "Activity groups: folded, live shimmer, expanded members, member detail box", w: 200, h: 50, modes: DLM, ascii: false, draw: sheet }]
}
