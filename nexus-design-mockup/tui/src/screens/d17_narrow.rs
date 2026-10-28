//! D17 — responsive behaviour: 80x24, its drawer,.
use super::{Shot, DL};
use crate::kit::{bold, trunc, P};
use crate::shell::{agent_title, code_style, group, prose, prose_segs, thought, user_block, Busy, Centre, Shell};
use ratatui::{buffer::Buffer, layout::Rect};

/// Narrow header: toggles and a breadcrumb truncated from the left.
fn header(p: &mut P) {
    let t = p.t;
    let w = p.w;
    p.fill(0, 0, w, 1, "surface.sidebar");
    p.put(1, 0, &[(t.g("closed_box"), t.secondary())]);
    p.put(4, 0, &[("… ", t.muted()), (t.g("crumb"), t.muted()), (" Refactor the loader", bold(t.primary()))]);
    p.put_r(w - 10, 0, &[("◆", t.accent()), (" waiting", t.accent())]);
    p.put_r(w - 1, 0, &[(t.g("unread"), t.fg("status.info")), (" 1", t.muted()), ("  ", t.muted()), (t.g("closed_box"), t.secondary())]);
}

fn base(p: &mut P) -> Centre {
    let t = p.t;
    let s = Shell { left: None, right: false, busy: Busy::Idle, ..Shell::default() };
    let c = s.draw(p);
    header(p);
    let mut y = 2;
    y += user_block(p, c, y, &["The loader re-reads the manifest on every call.", "Can you cache it and add a test?"], 1, "3:42 PM", false, false) + 1;
    thought(p, c, y, "Looking for where the manifest is parsed", "2.1s", false);
    y += 2;
    y += prose(p, c, y, "I'll start by finding where the manifest is parsed.") + 1;
    group(p, c, y, &[("Read 2 files", t.secondary()), (", ", t.muted()), ("ran 1 command", t.secondary())], "1.4s", false, false);
    y += 2;
    let cs = code_style(p);
    prose_segs(p, c, y, &[(" parse_manifest ", cs), (" is now memoized on the file's mtime.", t.primary())]);
    y += 1;
    y += prose(p, c, y, "I added a regression test that counts parses. Both tests pass.") + 1;
    // The footer wraps from the left onto a second row; the status stays on the last row.
    let end = c.x + c.w;
    p.put_r(end, y, &[(t.g("square"), t.agent("build")), (" ", t.muted()), (agent_title("build"), bold(t.agent("build"))), (" · gpt-6.1-terra · medium · 3 tools · 4.2s", t.muted())]);
    p.put_r(end, y + 1, &[("12k in · 2k out · $0.04 · ", t.muted()), (t.g("done"), t.fg("status.success")), (" done", t.fg("status.success"))]);
    let _ = trunc;
    c
}

fn narrow(p: &mut P) {
    base(p);
}

fn drawer(p: &mut P) {
    base(p);
    p.scrim();
    let t = p.t;
    let (w, h) = (p.w, p.h);
    // The docked shell at 34 columns, drawn into a scratch buffer; its left part becomes the drawer.
    let mut tmp = Buffer::empty(Rect::new(0, 0, w, h));
    {
        let mut q = P::new(&mut tmp, t);
        q.fill(0, 0, w, h, "surface.canvas");
        Shell { left_w: 34, right: false, ..Shell::default() }.draw(&mut q);
    }
    for y in 0..h {
        for x in 0..34 {
            p.buf[(x, y)] = tmp[(x, y)].clone();
        }
        p.put(34, y, &[("│", t.fg("border.strong"))]);
    }
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d17-80x24", caption: "80x24: one pane, toggles and breadcrumb only, wrapped footer", w: 80, h: 24, modes: DL, ascii: false, draw: narrow },
        Shot { id: "d17-80x24-drawer", caption: "80x24 with the left drawer over a scrimmed page", w: 80, h: 24, modes: DL, ascii: false, draw: drawer },
    ]
}
