//! D04 — user message band: states sheet and the sticky header.
use super::d01_tokens::{caption, wrap_lines};
use super::{Shot, DL};
use crate::kit::{bold, width, Seg, P};
use crate::shell::{footer, group, prose, thought, Centre, Shell};

const LONG: &str = "The loader re-reads the manifest on every call. Can you cache it, keep the public API unchanged, and add a regression test that counts how many times the manifest is parsed? Please do not add new dependencies.";

/// The band (D04). `tag` goes before `#n`; folded shows one line and the turn summary on the right.
fn band(p: &mut P, c: Centre, y: u16, text: &str, tag: &str, n: u8, time: &str, focused: bool, folded: bool) -> u16 {
    let t = p.t;
    let rb = t.bg("surface.raised");
    let right = c.bx + c.bw - 2;
    let meta = if tag.is_empty() { format!("#{n} · {time}") } else { format!("{tag} · #{n} · {time}") };
    let tx = c.x + 2;
    let lines: Vec<String> = if folded { vec![text.to_string()] } else { wrap_lines(text, right - tx - width(&meta) - 3, right - tx) };
    let pad = if folded { 0 } else { 1 };
    let rows = lines.len() as u16 + pad * 2;
    p.fill(c.bx, y, c.bw, rows, "surface.raised");
    let rail = if focused { t.fg("interactive.focus") } else { t.muted() };
    p.vline(c.bx, y, rows, t.g("focus"), rail.patch(rb));
    let fy = y + pad;
    p.put(c.x, fy, &[(if folded { t.g("closed") } else { t.g("open") }, t.muted().patch(rb))]);
    if folded {
        let tail: Vec<Seg> = vec![("3 tools · 4.2s · ", t.muted().patch(rb)), (t.g("done"), t.fg("status.success").patch(rb))];
        let tw = crate::kit::segs_w(&tail);
        let l0 = crate::kit::trunc(&lines[0], right - tw - 3 - tx);
        p.put(tx, fy, &[(&l0, t.primary().patch(rb))]);
        p.put_r(right, fy, &tail);
        return rows;
    }
    for (i, l) in lines.iter().enumerate() {
        p.put(tx, fy + i as u16, &[(l, t.primary().patch(rb))]);
    }
    p.put_r(right, fy, &[(&meta, t.muted().patch(rb))]);
    if focused {
        p.put_r(right, fy + lines.len() as u16 - 1, &[("c", bold(t.secondary()).patch(rb)), (" copy", t.muted().patch(rb))]);
    }
    rows
}

fn states(p: &mut P) {
    let c = Shell { left_w: 34, right_w: 36, ..Shell::default() }.draw(p);
    let mut y = 2;
    for (cap, text, tag, n, time, focus, fold) in [
        ("open", "Go ahead, but keep the change small.", "", 2, "3:44 PM", false, false),
        ("focused · c copies the message", LONG, "", 1, "3:42 PM", true, false),
        ("folded · one line, turn summary on the right", LONG, "", 1, "3:42 PM", false, true),
        ("tag: steer", "Actually, skip the docs for now.", "steer", 3, "3:46 PM", false, false),
        ("tag: queued", "Then run the whole suite once more.", "queued", 4, "3:47 PM", false, false),
        ("tag: via voice", "Rename the cache attribute to something shorter.", "via voice", 5, "3:49 PM", false, false),
        ("tag: from parent", "Review the loader change for thread safety.", "from parent", 6, "3:51 PM", false, false),
    ] {
        caption(p, c.x, y, cap);
        y += 1;
        y += band(p, c, y, text, tag, n, time, focus, fold) + 1;
    }
}

const MSG: &str = "Refactor the loader so it caches the parsed manifest. The loader re-reads and re-parses the manifest on every call. Keep the public API unchanged. Add a regression test that counts parses. Do not add dependencies. Run the whole suite when done and report anything that fails.";

fn sticky(p: &mut P) {
    let s = Shell { left_w: 34, right_w: 36, busy: crate::shell::Busy::Idle, ..Shell::default() };
    let c = s.draw(p);
    let t = p.t;
    let rb = t.bg("surface.raised");
    // Scrolled content first, then the pinned copy over the top.
    let mut y = 2;
    p.put(c.x, y, &[("Edited 1 file  ", t.secondary())]);
    p.put(c.x + 15, y, &[("+12", t.fg("diff.add")), (" ", t.muted()), ("−3", t.fg("diff.del"))]);
    y += 1;
    thought(p, c, y + 1, "Checking the failed edit", "1.2s", false);
    y += 3;
    y += prose(p, c, y, "The first edit did not apply because the line had changed; I re-read the file and applied it. Both tests pass, and the cache is invalidated when the file's mtime changes.");
    y += 1;
    group(p, c, y, &[("Ran 1 command", t.secondary())], "3.4s", false, false);
    y += 2;
    y += prose(p, c, y, "Done. The manifest is parsed once per file version, and the new test asserts the parse count.");
    y += 1;
    footer(p, c, y, "build", "gpt-6.1-terra", &["medium", "3 tools", "4.2s", "12k in · 2k out", "$0.04"], "done");
    y += 2;
    y += band(p, c, y, "Go ahead, but keep the change small.", "", 2, "3:44 PM", false, false) + 1;
    thought(p, c, y, "Reading the test file", "0.8s", false);
    // Sticky copy: at most 3 text rows ending in `…`, then the ░ shade row.
    let right = c.bx + c.bw - 2;
    let tx = c.x + 2;
    let mut lines = wrap_lines(MSG, right - tx - 16, right - tx);
    let rows = 3.min(lines.len()) as u16;
    lines.truncate(rows as usize);
    let last = lines.last_mut().unwrap();
    *last = crate::kit::trunc(&format!("{last} …"), right - tx);
    if !last.ends_with('…') {
        last.push('…');
    }
    p.fill(c.bx, 1, c.bw, rows + 1, "surface.raised");
    p.vline(c.bx, 1, rows, t.g("focus"), t.muted().patch(rb));
    p.put(c.x, 1, &[(t.g("open"), t.muted().patch(rb))]);
    for (i, l) in lines.iter().enumerate() {
        p.put(tx, 1 + i as u16, &[(l, t.primary().patch(rb))]);
    }
    p.put_r(right, 1, &[("#1 · 3:42 PM", t.muted().patch(rb))]);
    let sy = 1 + rows;
    p.hline(c.bx, sy, c.bw, t.g("block_light"), t.fg("border.strong"));
    let msg = " ↑ 34 lines of this turn above ";
    p.put_r(c.bx + c.bw - 1, sy, &[(msg, t.muted())]);
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d04-user-band", caption: "User band: open, focused, folded, and the tags (steer, queued, via voice, from parent)", w: 200, h: 50, modes: DL, ascii: false, draw: states },
        Shot { id: "d04-sticky", caption: "Sticky user header: a scrolled turn with the band pinned under the header", w: 200, h: 50, modes: DL, ascii: false, draw: sticky },
    ]
}
