//! D18 — empty, loading and offline states.
use super::{Shot, DL};
use crate::kit::{bold, P};
use crate::shell::{backdrop, Busy, Shell, Tab};

/// The half-block NEXUS wordmark: two rows of ▀▄█ glyphs, four cells per letter.
const MARK: [&str; 2] = ["█▀▄█  █▀▀▀  ▀▄▄▀  █  █  █▀▀▀", "█ ▀█  █▄▄▄  ▄▀▀▄  ▀▄▄▀  ▄▄▄█"];

fn empty_side(p: &mut P, lw: u16, find: &str, body: &[&str]) {
    let t = p.t;
    p.fill(0, 1, lw, p.h - 1, "surface.sidebar");
    p.put(2, 2, &[(t.g("search"), t.muted()), (find, t.muted())]);
    for (i, l) in body.iter().enumerate() {
        p.put(2, 4 + i as u16, &[(l, t.muted())]);
    }
    p.put(2, p.h - 2, &[("Settings", t.secondary())]);
    p.put_r(lw - 2, p.h - 2, &[("ctrl+,", t.muted())]);
}

fn empty(p: &mut P) {
    let t = p.t;
    let s = Shell { left: Some(Tab::Projects), left_w: 34, right: false, composer: false, crumbs: &[], open: None, ..Shell::default() };
    let c = s.draw(p);
    empty_side(p, 34, "  Filter projects", &["+ Add project"]);
    let cy = p.h / 2 - 4;
    for (i, row) in MARK.iter().enumerate() {
        p.put_c(c.bx, c.bw, cy + i as u16, &[(row, t.accent())]);
    }
    p.put_c(c.bx, c.bw, cy + 4, &[("No projects yet", bold(t.primary()))]);
    p.put_c(c.bx, c.bw, cy + 6, &[("ctrl+o", bold(t.secondary())), (" add a project · ", t.muted()), ("ctrl+p", bold(t.secondary())), (" commands", t.muted())]);
}

fn offline(p: &mut P) {
    let t = p.t;
    let s = Shell { left_w: 34, right_w: 36, busy: Busy::Offline, ..Shell::default() };
    let c = backdrop(p, &s);
    let pb = t.bg("surface.panel");
    let er = t.fg("status.error");
    p.fill(c.bx, 1, c.bw, 1, "surface.panel");
    p.put(c.x, 1, &[(t.g("error"), er.patch(pb)), (" Disconnected from Core", bold(er).patch(pb)), (" · retrying in 4s", t.muted().patch(pb))]);
    p.put_r(c.x + c.w, 1, &[("r", bold(t.secondary()).patch(pb)), (" retry", t.muted().patch(pb))]);
}

fn loading(p: &mut P) {
    let t = p.t;
    let s = Shell { left_w: 34, right_w: 36, ..Shell::default() };
    backdrop(p, &s);
    empty_side(p, 34, "  Search sessions", &["NEXUS-DEMO"]);
    p.put(2, 4, &[("NEXUS-DEMO", bold(t.muted()))]);
    p.put(2, 6, &[(t.g("running"), t.muted()), (" loading sessions…", t.muted())]);
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d18-empty", caption: "Empty workspace: wordmark, no projects", w: 200, h: 50, modes: DL, ascii: false, draw: empty },
        Shot { id: "d18-offline", caption: "Offline: header status, banner, draft kept", w: 200, h: 50, modes: DL, ascii: false, draw: offline },
        Shot { id: "d18-loading", caption: "Loading sessions without layout shift", w: 200, h: 50, modes: DL, ascii: false, draw: loading },
    ]
}
