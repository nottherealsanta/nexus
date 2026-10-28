//! D05 — turn footer: right-aligned, last row of the turn, wraps from the left when narrow.
use super::d01_tokens::caption;
use super::{Shot, DL};
use crate::kit::{bold, width, P};
use crate::shell::{agent_title, Centre, Shell};
use ratatui::style::Style;

type Unit = Vec<(String, Style)>;

fn u(s: &str, st: Style) -> Unit {
    vec![(s.to_string(), st)]
}

/// Footer rows ending at `end`, at most `avail` cells wide.
/// Returns the rows used.
fn foot(p: &mut P, end: u16, avail: u16, y: u16, agent: &str, model: &str, mids: &[&str], status: &str, tail: &str) -> u16 {
    let t = p.t;
    let mut units: Vec<Unit> = vec![vec![(t.g("square").into(), t.agent(agent)), (" ".into(), t.muted()), (agent_title(agent).into(), bold(t.agent(agent)))], u(model, t.secondary())];
    units.extend(mids.iter().map(|m| u(m, t.muted())));
    let _ = status; // no end-status word or glyph; only the `/continue` hint remains
    if !tail.is_empty() {
        units.push(u(tail, t.muted()));
    }
    let uw = |x: &Unit| x.iter().map(|(s, _)| width(s)).sum::<u16>();
    let mut rows: Vec<Vec<Unit>> = vec![vec![]];
    let mut cur = 0u16;
    for un in units {
        let w = uw(&un);
        let add = if rows.last().unwrap().is_empty() { w } else { w + 3 };
        if cur + add > avail && !rows.last().unwrap().is_empty() {
            rows.push(vec![]);
            cur = w;
        } else {
            cur += add;
        }
        rows.last_mut().unwrap().push(un);
    }
    for (i, row) in rows.iter().enumerate() {
        let mut segs: Vec<(&str, Style)> = Vec::new();
        for (j, un) in row.iter().enumerate() {
            if j > 0 {
                segs.push((" · ", t.muted()));
            }
            segs.extend(un.iter().map(|(s, st)| (s.as_str(), *st)));
        }
        p.put_r(end, y + i as u16, &segs);
    }
    rows.len() as u16
}

fn sheet(p: &mut P) {
    let c = Shell { left_w: 34, right_w: 36, ..Shell::default() }.draw(p);
    let t = p.t;
    let end = c.x + c.w;
    let mids = ["medium", "3 tools", "4.2s", "12k in · 2k out", "$0.04"];
    let mut y = 2;
    let mut block = |p: &mut P, cap: &str, text: &str, f: &dyn Fn(&mut P, u16) -> u16| {
        caption(p, c.x, y, cap);
        p.put(c.x, y + 2, &[(text, t.primary())]);
        let n = f(p, y + 4);
        y += 4 + n + 1;
    };
    block(p, "normal", "The first edit did not apply because the line had changed; I re-read the file and applied it.", &|p, y| foot(p, end, c.w, y, "build", "gpt-6.1-terra", &mids, "done", ""));
    block(p, "model changed mid-turn · a → b", "Switched to the larger model for the test run.", &|p, y| foot(p, end, c.w, y, "build", "gpt-6.0-terra → gpt-6.1-terra", &["medium", "5 tools", "1m 05s", "31k in · 8k cached · 4k out", "$0.21"], "done", ""));
    block(p, "interrupted · the hint becomes `enter continue` when the composer is empty", "I'll add the regression test next, starting with", &|p, y| foot(p, end, c.w, y, "build", "gpt-6.1-terra", &["medium", "1 tool", "0.4s", "9k in · 310 out"], "interrupted", "/continue"));
    block(p, "error · the first line of the error sits on a muted row above", "Running the suite now.", &|p, y| {
        let msg = "provider: 429 rate limited · retry after 4s";
        p.put_r(end, y, &[(msg, t.muted())]);
        1 + foot(p, end, c.w, y + 1, "orchestrate", "gpt-6.1-terra", &["high", "2 tools", "1h 02m", "88k in · 21k cached · 6k out"], "error", "")
    });
    // Narrow: a 56-cell column, wrapping from the left.
    caption(p, c.x, y, "narrow · 56 cells: wraps from the left onto a second right-aligned row");
    y += 2;
    let nc = Centre { w: 56, ..c };
    let nend = nc.x + nc.w;
    for yy in y..y + 4 {
        p.put(nend + 1, yy, &[(t.g("tick"), t.hair())]);
    }
    p.put(c.x, y, &[("Both tests pass.", t.primary())]);
    foot(p, nend, 56, y + 2, "build", "gpt-6.1-terra", &mids, "done", "");
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d05-footer", caption: "Turn footer (no end-status word): normal, model change, interrupted with /continue, error line above, narrow", w: 200, h: 50, modes: DL, ascii: false, draw: sheet }]
}
