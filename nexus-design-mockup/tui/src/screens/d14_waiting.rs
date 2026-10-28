//! D14 — the waiting card docked above the composer: approval and question.
use super::{Shot, DL, DLM};
use crate::kit::{bold, Seg, P};
use crate::shell::{chip, group, tool, user_block, Busy, Centre, Shell};
use ratatui::style::Style;

/// Shell with a short timeline ending in a waiting tool; returns the centre.
fn scene(p: &mut P) -> Centre {
    let t = p.t;
    let s = Shell { left_w: 34, right_w: 36, busy: Busy::Waiting, ..Shell::default() };
    let c = s.draw(p);
    group(p, c, 2, &[("Context", bold(t.secondary())), (" · 4 items · 1.1k tokens", t.muted())], "", false, false);
    group(p, c, 4, &[("Read 2 files, ran 1 command", t.secondary())], "1.4s", false, false);
    group(p, c, 6, &[("Edited 1 file", t.secondary()), ("  ", t.muted()), ("+12", t.fg("diff.add")), (" ", t.muted()), ("−3", t.fg("diff.del"))], "0.9s", false, false);
    let y = 8 + user_block(p, c, 8, &["Run the loader tests, then fix whatever fails."], 2, "3:44 PM", false, false) + 1;
    group(p, c, y, &[("Working", bold(t.secondary()))], "42s", true, false);
    tool(p, c, y + 1, false, "Read", "tests/test_loader.py", &[("1-86", t.muted())], "0.1s", "done");
    tool(p, c, y + 2, true, "Shell", "pytest tests/test_loader.py -q --maxfail=1", &[], "", "waiting");
    c
}

/// Card shell: panel with the heavy accent rail; returns (x, y, w, h) of the card.
fn dock(p: &mut P, c: Centre, h: u16) -> (u16, u16, u16) {
    let t = p.t;
    let y = c.bottom + 1 - h;
    p.fill(c.x, y, c.w, h, "surface.panel");
    p.vline(c.x, y, h, t.g("rail_heavy"), t.accent().patch(t.bg("surface.panel")));
    (c.x + 3, y, c.w)
}

fn header(p: &mut P, c: Centre, x: u16, y: u16, title: &str, count: &str) {
    let t = p.t;
    let pb = t.bg("surface.panel");
    p.put(x, y, &[(t.g("waiting"), t.accent().patch(pb)), (" ", pb), (title, bold(t.primary()).patch(pb))]);
    p.put_r(c.x + c.w - 2, y, &[(count, t.muted().patch(pb))]);
    p.put(x, y + 1, &[("waiting on you · 0:42", t.muted().patch(pb))]);
}

fn kv(p: &mut P, x: u16, y: u16, k: &str, v: &[Seg]) {
    let t = p.t;
    let pb = t.bg("surface.panel");
    p.put(x, y, &[(k, t.muted().patch(pb))]);
    let vv: Vec<Seg> = v.iter().map(|(s, st)| (*s, st.patch(pb))).collect();
    p.put(x + 9, y, &vv);
}

fn approval(p: &mut P) {
    let c = scene(p);
    let t = p.t;
    let pb = t.bg("surface.panel");
    let (x, y, _) = dock(p, c, 15);
    header(p, c, x, y + 1, "Allow bash?", "1 of 2 · tab next");
    kv(p, x, y + 4, "command", &[("pytest tests/test_loader.py -q --maxfail=1", t.primary())]);
    kv(p, x, y + 5, "cwd", &[("~/repos/nexus-demo", t.secondary())]);
    kv(p, x, y + 6, "policy", &[("ask", t.secondary()), (" · bash is not on the allow list", t.muted())]);
    let choices: [(&str, &str, &str); 4] = [("y", "Allow once", ""), ("a", "Allow for session", ""), ("n", "Deny", ""), ("m", "Deny with a note", "Core does not support notes yet")];
    for (i, (k, label, why)) in choices.iter().enumerate() {
        let yy = y + 8 + i as u16;
        let sel = i == 0;
        let bgs = if sel { t.bg("interactive.selection") } else { pb };
        if sel {
            p.fill(c.x + 1, yy, c.w - 1, 1, "interactive.selection");
            if t.mode == crate::theme::Mode::Mono {
                p.tint(c.x + 1, yy, c.w - 1, t.primary());
            }
        }
        p.put(x, yy, &[(if sel { t.g("closed") } else { " " }, t.accent().patch(bgs))]);
        let off = !why.is_empty();
        if off {
            p.put(x + 3, yy, &[(" ", bgs), (k, t.muted().patch(bgs)), (" ", bgs), (" ", bgs), (label, t.muted().patch(bgs)), ("  ", bgs), (why, t.muted().patch(bgs))]);
        } else {
            let cx = chip(p, x + 3, yy, k, "surface.raised");
            p.put(cx + 2, yy, &[(label, if sel { bold(t.primary()) } else { t.primary() }.patch(bgs))]);
        }
    }
    p.put(x, y + 13, &[("↑↓", bold(t.secondary()).patch(pb)), (" choose · ", t.muted().patch(pb)), ("enter", bold(t.secondary()).patch(pb)), (" confirm · ", t.muted().patch(pb)), ("esc", bold(t.secondary()).patch(pb)), (" deny", t.muted().patch(pb))]);
}

fn question(p: &mut P) {
    let c = scene(p);
    let t = p.t;
    let pb = t.bg("surface.panel");
    let (x, y, _) = dock(p, c, 18);
    header(p, c, x, y + 1, "Nexus asks", "1 of 2 · tab next");
    let q = |p: &mut P, yy: u16, s: &str| {
        p.put(x, yy, &[(s, bold(t.primary()).patch(pb))]);
    };
    let opt = |p: &mut P, yy: u16, on: bool, mark: &str, label: &str, sel: bool| {
        let bgs = if sel { t.bg("interactive.selection") } else { pb };
        if sel {
            p.fill(c.x + 1, yy, c.w - 1, 1, "interactive.selection");
            if t.mode == crate::theme::Mode::Mono {
                p.tint(c.x + 1, yy, c.w - 1, t.primary());
            }
        }
        let _ = on;
        p.put(x, yy, &[(if sel { t.g("closed") } else { " " }, t.accent().patch(bgs)), (" ", bgs), (mark, (if on { t.primary() } else { t.muted() }).patch(bgs)), (" ", bgs), (label, t.primary().patch(bgs))]);
    };
    q(p, y + 4, "How should the loader detect a changed manifest?");
    opt(p, y + 5, true, "(*)", "Compare the file's mtime", true);
    opt(p, y + 6, false, "( )", "Hash the contents on every call", false);
    opt(p, y + 7, false, "( )", "Never; clear the cache on restart", false);
    q(p, y + 9, "Which extras should I add?");
    opt(p, y + 10, true, "[x]", "A regression test that counts parses", false);
    opt(p, y + 11, false, "[ ]", "A note in docs/loader.md", false);
    // Free-text field on the raised surface.
    let fy = y + 13;
    p.fill(x + 2, fy, c.w - 8, 1, "surface.raised");
    p.put(x + 3, fy, &[("Or type your own answer…", t.muted().patch(t.bg("surface.raised")))]);
    p.put(x, y + 16, &[("enter", bold(t.secondary()).patch(pb)), (" submit · ", t.muted().patch(pb)), ("tab", bold(t.secondary()).patch(pb)), (" next question · ", t.muted().patch(pb)), ("esc", bold(t.secondary()).patch(pb)), (" skip", t.muted().patch(pb))]);
    let _ = Style::default();
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d14-approval", caption: "Approval card for bash, docked above the composer", w: 200, h: 50, modes: DLM, ascii: false, draw: approval },
        Shot { id: "d14-question", caption: "Question card: options and free text", w: 200, h: 50, modes: DL, ascii: false, draw: question },
    ]
}
