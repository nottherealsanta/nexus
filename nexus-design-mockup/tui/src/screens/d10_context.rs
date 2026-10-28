//! D10 — the starting context and the new-session view.
use super::{common, Shot, DL};
use crate::kit::{bold, trunc, P};
use crate::shell::{group, Busy, Centre, Shell};
use ratatui::style::Modifier;

/// `◆ Title   tokens · meta` and an optional muted preview; returns rows used.
fn section(p: &mut P, c: Centre, y: u16, title: &str, tok: &str, meta: &str, preview: &str) -> u16 {
    let t = p.t;
    p.put(c.x, y, &[("◆ ", t.muted()), (&format!("{title:<16}"), bold(t.primary())), (&format!("{tok:>9}"), t.secondary()), ("  ·  ", t.muted()), (meta, t.muted())]);
    if preview.is_empty() {
        return 1;
    }
    p.put(c.x + 2, y + 1, &[(&trunc(preview, c.w - 2), t.muted())]);
    2
}

/// A 4-column inventory grid; names in `off` are struck through and muted.
fn grid(p: &mut P, c: Centre, y: u16, names: &[&str], off: &[&str]) -> u16 {
    let t = p.t;
    let cw = 20;
    for (i, n) in names.iter().enumerate() {
        let (x, yy) = (c.x + 2 + (i as u16 % 4) * cw, y + i as u16 / 4);
        let st = if off.contains(n) { t.muted().add_modifier(Modifier::CROSSED_OUT) } else { t.secondary() };
        p.put(x, yy, &[(n, st)]);
    }
    names.len().div_ceil(4) as u16
}

fn header(p: &mut P, c: Centre, y: u16) -> u16 {
    let t = p.t;
    p.put(c.x, y, &[("New session in ", bold(t.primary())), ("nexus-demo", bold(t.primary()))]);
    p.put(c.x, y + 1, &[("Build", bold(t.agent("build"))), (" · ", t.muted()), ("gpt-6.1-terra", t.primary()), (" codex", t.muted()), (" · ", t.muted()), ("medium", t.secondary()), (" · ", t.muted()), ("~/repos/nexus-demo", t.muted()), (" ", t.muted()), (t.g("branch"), t.muted()), (" main", t.secondary())]);
    let k = |s| (s, bold(t.secondary()));
    p.put(c.x, y + 2, &[k("Tab"), (" agent · ", t.muted()), k("ctrl+m"), (" model · ", t.muted()), k("ctrl+e"), (" effort · ", t.muted()), k("ctrl+d"), (" directory", t.muted())]);
    3
}

fn context(p: &mut P, c: Centre, mut y: u16) -> u16 {
    let t = p.t;
    y += section(p, c, y, "System prompt", "2,140 tok", "build · 2 sections", "You are Nexus, a provider-agnostic coding agent working in the user's workspace. You read, search, edit and run code …") + 1;
    y += section(p, c, y, "AGENTS.md", "3,880 tok", "~/repos/nexus · 1 file", "This branch (nexus10, Nexus 0.10) rebuilds Nexus from the provider wire up. Read PLAN.md first: principles, layout …") + 1;
    y += section(p, c, y, "Tools", "4,780 tok", "9 tools · 8 on", "");
    y += grid(p, c, y, &["read", "bash", "edit", "write", "ask", "code", "search", "session", "web"], &["web"]) + 1;
    y += section(p, c, y, "Skills", "6,780 tok", "7 skills · 2 sources", "");
    y += grid(p, c, y, &["review", "commit", "debug", "test-writer", "refactor", "release", "docs"], &[]) + 1;
    p.put(c.x, y, &[("Context total", bold(t.secondary())), (" · ", t.muted()), ("17,580 tokens", t.primary()), (" · ", t.muted()), ("8.8% of the window", t.muted())]);
    y + 1
}

fn new_session(p: &mut P) {
    let s = Shell { left_w: 34, right_w: 36, open: None, crumbs: &["nexus-demo", "New session"], busy: Busy::Idle, composer_focus: true, ..Shell::default() };
    let c = s.draw(p);
    let n = header(p, c, 2);
    context(p, c, 2 + n + 1);
}

fn folded(p: &mut P) {
    let c = Shell { left_w: 34, right_w: 36, ..Shell::default() }.draw(p);
    let t = p.t;
    common::turn_one(p, c, 2);
    // turn_one hardcodes 1.1k tokens; the plan's total is 17.6k.
    p.fill(c.bx, 2, c.bw, 1, "surface.canvas");
    group(p, c, 2, &[("Context", bold(t.secondary())), (" · 4 items · 17.6k tokens", t.muted())], "", false, false);
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d10-new-session", caption: "New session: header block, then the starting context as the content", w: 200, h: 50, modes: DL, ascii: false, draw: new_session },
        Shot { id: "d10-context-folded", caption: "After the first turn the context folds to one line at the timeline top", w: 200, h: 50, modes: DL, ascii: false, draw: folded },
    ]
}
