//! D03 — sessions sidebar: groups, worktree sub-groups, row menu, rail mode.
use super::{Shot, DL};
use crate::kit::{bold, mid, segs_w, trunc, width, Seg, P};
use crate::shell::{backdrop, modal, shimmer, SessionRow, Shell, Tab};
use ratatui::style::Style;

const LW: u16 = 34;
const OPEN: u8 = 1;
const FOCUS: u8 = 2;
const HOVER: u8 = 4;

enum It {
    Head(&'static str),
    Sub(&'static str, &'static str),
    Row(SessionRow, u8),
    New,
}

fn row(title: &'static str, agent: &'static str, place: &'static str, when: &'static str, mark: &'static str, children: u8) -> SessionRow {
    SessionRow { title, agent, place, when, mark, children }
}

/// Repaint the left sidebar from `items`; returns the y of every session row.
fn side(p: &mut P, items: Vec<It>) -> Vec<u16> {
    let t = p.t;
    p.fill(0, 1, LW, p.h - 1, "surface.sidebar");
    p.put(2, 2, &[(t.g("search"), t.muted()), ("  Search sessions", t.muted())]);
    p.put_r(LW - 2, 2, &[("ctrl+k", t.muted())]);
    let mut y = 4;
    let mut ys = Vec::new();
    for it in items {
        match it {
            It::Head(n) => {
                p.put(2, y, &[(n, bold(t.muted()))]);
                y += 1;
            }
            It::Sub(b, path) => {
                p.put(2, y, &[(t.g("branch"), t.secondary()), (" ", t.secondary()), (b, t.secondary())]);
                p.put_r(LW - 2, y, &[(&mid(path, 16), t.muted())]);
                y += 1;
            }
            It::New => {
                p.put(4, y, &[("+ New session", t.muted())]);
                y += 2;
            }
            It::Row(s, f) => {
                ys.push(y);
                let open = f & OPEN != 0;
                if open {
                    p.fill(0, y, LW, 2, "interactive.selection");
                } else if f & HOVER != 0 {
                    p.fill(0, y, LW, 2, "interactive.hover");
                }
                if f & FOCUS != 0 {
                    p.vline(0, y, 2, t.g("focus"), t.fg("interactive.focus"));
                }
                let bgs = if open { t.bg("interactive.selection") } else if f & HOVER != 0 { t.bg("interactive.hover") } else { Style::default() };
                let (mark, ms) = match s.mark {
                    "waiting" => (t.g("waiting"), t.accent()),
                    "running" => (t.g("running"), t.primary()),
                    "error" => (t.g("error"), t.fg("status.error")),
                    "unread" => (t.g("unread"), t.fg("status.info")),
                    _ => (" ", t.muted()),
                };
                p.put(2, y, &[(mark, ms.patch(bgs))]);
                let title = trunc(s.title, LW - 7 - width(s.when));
                if s.mark == "running" {
                    shimmer(p, 4, y, &title, 22, bgs);
                } else {
                    p.put(4, y, &[(&title, if open { bold(t.primary()) } else { t.primary() }.patch(bgs))]);
                }
                p.put_r(LW - 2, y, &[(s.when, t.muted().patch(bgs))]);
                let mut l2: Vec<Seg> = vec![(s.agent, t.agent(s.agent).patch(bgs)), (" · ", t.muted().patch(bgs)), (t.g("branch"), t.muted().patch(bgs)), (" ", bgs), (s.place, t.muted().patch(bgs))];
                if s.children > 0 {
                    let kids = format!("{} {}", s.children, t.g("child"));
                    p.put_r(LW - 2, y + 1, &[(&kids, t.muted().patch(bgs))]);
                }
                if segs_w(&l2) > LW - 10 {
                    l2.truncate(1);
                }
                p.put(4, y + 1, &l2);
                y += 3;
            }
        }
    }
    p.put(2, p.h - 2, &[("Settings", t.secondary())]);
    p.put_r(LW - 2, p.h - 2, &[("ctrl+,", t.muted())]);
    ys
}

fn base(p: &mut P) {
    let s = Shell { left_w: LW, right_w: 36, ..Shell::default() };
    backdrop(p, &s);
}

fn sessions(p: &mut P) {
    base(p);
    side(p, vec![
        It::Head("NEXUS-DEMO"),
        It::Row(row("Refactor the loader", "build", "main", "now", "", 0), OPEN),
        It::Row(row("Review the CJK width handling", "orchestrate", "main", "3m", "waiting", 2), FOCUS),
        It::Row(row("Release notes for 0.10", "plan", "docs/0.10", "1h", "running", 0), 0),
        It::Row(row("Fix flaky test in sync", "build", "main", "2h", "error", 0), HOVER),
        It::Row(row("Spike: terminal harness", "scout", "main", "1d", "unread", 1), 0),
        It::New,
    ]);
}

fn menu(p: &mut P) {
    base(p);
    let ys = side(p, vec![
        It::Head("NEXUS-DEMO"),
        It::Row(row("Refactor the loader", "build", "main", "now", "", 0), OPEN),
        It::Row(row("Review the CJK width handling", "orchestrate", "main", "3m", "waiting", 2), FOCUS),
        It::Row(row("Fix flaky test in sync", "build", "main", "2h", "error", 0), 0),
        It::Sub("feat/cache", "~/…/demo-cache"),
        It::Row(row("Memoize the manifest parse", "build", "feat/cache", "12m", "running", 0), 0),
        It::Row(row("Cache invalidation notes", "plan", "feat/cache", "1h", "", 0), 0),
        It::Sub("docs/0.10", "~/…/demo-docs"),
        It::Row(row("Release notes for 0.10", "plan", "docs/0.10", "1h", "", 0), 0),
        It::New,
    ]);
    let t = p.t;
    // Row menu: a small raised popover beside the focused row.
    let (x, y, w) = (LW - 6, ys[1], 30);
    modal(p, x, y, w, 7, "", false);
    let rb = t.bg("surface.raised");
    let items: [(&str, &str, bool); 5] = [("Rename", "r", false), ("Archive", "a", false), ("Delete", "d", true), ("Copy id", "y", false), ("Open inspector", "i", false)];
    for (i, (l, k, danger)) in items.iter().enumerate() {
        let yy = y + 1 + i as u16;
        let sel = i == 0;
        let ls = if *danger { t.fg("status.error") } else { t.primary() };
        if sel {
            crate::shell::sel_row(p, x + 1, yy, w - 2);
        }
        let b = if sel { t.bg("interactive.selection") } else { rb };
        p.put(x + 3, yy, &[(l, ls.patch(b))]);
        p.put_r(x + w - 3, yy, &[(k, t.muted().patch(b))]);
    }
}

fn rail(p: &mut P) {
    let s = Shell { left_w: 8, right_w: 36, focus_row: Some(1), ..Shell::default() };
    let c = s.draw_rail(p);
    let y = crate::screens::common::turn_one(p, c, 2);
    crate::shell::user_block(p, c, y, &["Go ahead, but keep the change small.", "日本語のコメントも残してください。"], 2, "3:44 PM", false, false);
}

pub fn shots() -> Vec<Shot> {
    let _ = Tab::Sessions;
    vec![
        Shot { id: "d03-sessions", caption: "Sessions sidebar: open (band), focus (bar), hover, marks, shimmering running title", w: 200, h: 50, modes: DL, ascii: false, draw: sessions },
        Shot { id: "d03-sessions-worktrees-menu", caption: "Worktree sub-groups and the row menu", w: 200, h: 50, modes: DL, ascii: false, draw: menu },
        Shot { id: "d03-rail", caption: "Rail mode: three-row items", w: 200, h: 50, modes: DL, ascii: false, draw: rail },
    ]
}
