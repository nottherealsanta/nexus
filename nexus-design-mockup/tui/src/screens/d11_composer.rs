//! D11 — the composer as a component sheet: idle, draft, busy, completion, narrow shedding, offline.
use super::{Shot, DL};
use crate::kit::{bold, Seg, P};
use crate::shell::{hints, meter, mrow, shimmer, modal};
use crate::theme::Mode;
use ratatui::style::Style;

pub struct Card<'a> {
    pub lines: &'a [&'a str],
    pub placeholder: &'a str,
    pub focus: bool,
    pub busy: bool,
    pub effort: bool,
    pub prov: bool,
    pub meter: bool,
}

impl Default for Card<'_> {
    fn default() -> Self {
        Card { lines: &[], placeholder: "Type a message…", focus: true, busy: false, effort: true, prov: true, meter: true }
    }
}

/// The composer card (half-row edges, input rows, blank, control row). Returns the first row below it.
pub fn card(p: &mut P, x: u16, y: u16, w: u16, c: &Card) -> u16 {
    let t = p.t;
    let n = c.lines.len().max(1) as u16;
    let pc = t.color("surface.panel");
    if t.mode != Mode::Mono {
        p.hline(x, y, w, if t.ascii { " " } else { "▄" }, Style::default().fg(pc));
        p.hline(x, y + n + 3, w, if t.ascii { " " } else { "▀" }, Style::default().fg(pc));
    } else {
        p.hline(x, y, w, "─", t.hair());
        p.hline(x, y + n + 3, w, "─", t.hair());
    }
    p.fill(x, y + 1, w, n + 2, "surface.panel");
    let pb = t.bg("surface.panel");
    let cur = Style::default().fg(pc).bg(t.color("text.primary"));
    let ix = x + 3;
    if c.lines.is_empty() {
        let (a, b) = c.placeholder.split_at(1);
        p.put(ix, y + 1, &[(a, if c.focus { cur } else { t.muted().patch(pb) }), (b, t.muted().patch(pb))]);
    } else {
        for (i, l) in c.lines.iter().enumerate() {
            let yy = y + 1 + i as u16;
            if i + 1 == c.lines.len() && c.focus {
                p.put(ix, yy, &[(l, t.primary().patch(pb)), (" ", cur)]);
            } else {
                p.put(ix, yy, &[(l, t.primary().patch(pb))]);
            }
        }
    }
    let cy = y + n + 2;
    let mut left: Vec<Seg> = vec![("Build", bold(t.agent("build")).patch(pb)), (" · ", t.muted().patch(pb)), ("gpt-6.1-terra", t.primary().patch(pb))];
    if c.prov {
        left.push((" codex", t.muted().patch(pb)));
    }
    if c.effort {
        left.push((" · ", t.muted().patch(pb)));
        left.push(("medium", t.secondary().patch(pb)));
    }
    p.put(ix, cy, &left);
    let right = x + w - 3;
    if c.busy {
        p.put_r(right, cy, &[("esc", bold(t.secondary()).patch(pb)), (" stop", t.muted().patch(pb))]);
    } else if c.meter {
        let lx = p.put_r(right, cy, &[("82k", t.secondary().patch(pb)), (" · 41%", t.muted().patch(pb))]);
        meter(p, lx - 12, cy, 10, 0.41, "build");
    } else {
        p.put_r(right, cy, &[("41%", t.muted().patch(pb))]);
    }
    y + n + 4
}

/// The project strip under the card.
pub fn strip(p: &mut P, x: u16, y: u16, w: u16, worktree: bool, idle: bool) {
    let t = p.t;
    let (branch, kind) = if worktree { (" feat/cache", ("worktree", bold(t.fg("agent.violet")))) } else { (" main", ("checkout", t.muted())) };
    p.put(x + 1, y, &[("nexus-demo", bold(t.primary())), ("  ", t.muted()), ("~/repos/nexus-demo", t.muted()), ("  ", t.muted()), (t.g("branch"), t.muted()), (branch, t.secondary()), ("  ", t.muted()), kind]);
    if idle {
        p.put_r(x + w - 1, y, &[("ctrl+p", t.secondary()), (" commands", t.muted())]);
    }
}

/// The busy row above the card: `◐ Working · 12s · ↓4.1k`, Working shimmering.
pub fn live_row(p: &mut P, x: u16, y: u16, frame: u32) {
    let t = p.t;
    let nx = p.put(x, y, &[(t.g("running"), t.primary()), (" ", t.muted())]);
    let nx = shimmer(p, nx, y, "Working", frame, Style::default());
    p.put(nx, y, &[(" · 12s · ↓4.1k", t.muted())]);
}

fn cap(p: &mut P, x: u16, y: u16, label: &str, note: &str) {
    let t = p.t;
    p.put(x, y, &[(label, bold(t.muted())), ("  ", t.muted()), (note, t.muted())]);
}

/// Completion popover anchored above the card's top edge (rows end at `edge - 1`).
fn pop(p: &mut P, x: u16, edge: u16, w: u16, rows: &[(&str, &str, &str)], sel: usize, hint: &[(&str, &str)], count: &str) {
    let t = p.t;
    let h = rows.len() as u16 + 4;
    let y = edge - h;
    modal(p, x, y, w, h, "", false);
    for (i, (name, desc, key)) in rows.iter().enumerate() {
        let on = i == sel;
        let ns = if on { bold(t.primary()) } else { t.primary() };
        mrow(p, x, y + 1 + i as u16, w, " ", t.muted(), &[(name, ns), ("  ", t.muted()), (desc, t.secondary())], &[(key, t.muted())], on);
    }
    hints(p, x, y + h - 2, w, hint, count);
}

fn sheet(p: &mut P) {
    let t = p.t;
    let (lx, rx, w) = (4u16, 104u16, 92u16);
    // Left column: idle, draft, busy, narrow shedding.
    let mut y = 1;
    cap(p, lx, y, "IDLE", "placeholder, cursor on the first letter, context meter right");
    y = card(p, lx, y + 1, w, &Card::default());
    strip(p, lx, y, w, false, true);
    y += 3;
    cap(p, lx, y, "DRAFT", "multi-line; the card grows, the control row stays at its foot");
    y = card(p, lx, y + 1, w, &Card { lines: &["Add a regression test that counts parses, and keep the change small.", "Also check that edits to the manifest still show up after the cache is warm."], ..Card::default() });
    strip(p, lx, y, w, false, true);
    y += 3;
    cap(p, lx, y, "BUSY", "live row above the card; the meter becomes `esc stop`");
    live_row(p, lx + 3, y + 1, 20);
    y = card(p, lx, y + 2, w, &Card { busy: true, ..Card::default() });
    strip(p, lx, y, w, true, false);
    y += 3;
    cap(p, lx, y, "NARROW", "sheds effort, then the provider, then the meter");
    y += 1;
    for (cw, c) in [(70u16, Card { effort: false, ..Card::default() }), (56, Card { effort: false, prov: false, ..Card::default() }), (44, Card { effort: false, prov: false, meter: false, ..Card::default() })] {
        y = card(p, lx, y, cw, &c) + 1;
    }
    // Right column: completions and offline.
    let mut y = 1;
    cap(p, rx, y, "/ COMPLETION", "popover on the card's top edge, ten rows at most");
    let rows = [("/model", "Change model", "ctrl+m"), ("/mcp", "MCP servers", ""), ("/compact", "Hand off to a new session", ""), ("/theme", "Change theme", ""), ("/rename", "Rename session", ""), ("/project", "Switch project", "alt+p")];
    let top = y + 2 + rows.len() as u16 + 4;
    let b = card(p, rx, top, w, &Card { lines: &["/m"], ..Card::default() });
    pop(p, rx, top, w, &rows, 0, &[("↑↓", "move"), ("tab", "complete"), ("esc", "close")], "6 of 24");
    strip(p, rx, b, w, false, true);
    y = b + 3;
    cap(p, rx, y, "@ FILE COMPLETION", "paths from the project; the basename is bold");
    let rows = [("loader.py", "src/nexus", ""), ("loader_cache.py", "src/nexus", ""), ("test_loader.py", "tests", ""), ("loader.md", "docs", ""), ("loaders.toml", "config", "")];
    let top = y + 2 + rows.len() as u16 + 4;
    let b = card(p, rx, top, w, &Card { lines: &["Please look at @load"], ..Card::default() });
    pop(p, rx, top, w, &rows, 1, &[("↑↓", "move"), ("tab", "insert"), ("esc", "close")], "5 of 41");
    strip(p, rx, b, w, false, true);
    y = b + 3;
    cap(p, rx, y, "OFFLINE", "the draft is kept while Core reconnects");
    let b = card(p, rx, y + 2, w, &Card { placeholder: "Reconnecting to Core… your draft is kept", focus: false, meter: false, ..Card::default() });
    strip(p, rx, b, w, false, false);
    let _ = t;
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d11-composer", caption: "Composer states: idle, draft, busy, completion, narrow, offline", w: 200, h: 50, modes: DL, ascii: false, draw: sheet }]
}
