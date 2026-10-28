//! D07 — reasoning: finished, open, running and encrypted.
use super::d01_tokens::{caption, wrap_lines};
use super::{Shot, DL};
use crate::kit::{bold, italic, P};
use crate::shell::{thought, Shell};

const OPEN: &str = "Looking for where the manifest is parsed. parse_manifest is called from Loader.load on every request, so a cache keyed on the file's mtime would keep edits visible. I should check whether anything else mutates the manifest after it is parsed before sharing one instance between callers.";
const LIVE: &str = "Plan: memoize parse_manifest per path and invalidate on mtime. First check every caller of Loader.load, because sharing one manifest instance is only safe if nobody mutates it. Search shows four call sites and none of them write to the result, so sharing is fine. Next the cache itself: a dict keyed by path that stores the mtime and the parsed manifest, with a lock around the miss path so two threads do not both parse. The first edit failed because the line changed under me. I will re-read src/nexus/loader.py around the cache lookup, then apply the change again with the new line numbers. After that the regression test needs to count parses, so I want a fixture that writes a manifest to tmp_path and a spy on parse_manifest. The mtime resolution on some filesystems is coarse, which could make two quick edits look identical, so the key should include the file size as well. I will keep that in the test as a second case so the cache cannot hide a stale manifest.";

fn sheet(p: &mut P) {
    let c = Shell { left_w: 34, right_w: 36, busy: crate::shell::Busy::Running(9), ..Shell::default() }.draw(p);
    let t = p.t;
    let mut y = 2;
    caption(p, c.x, y, "finished · folded, the preview is the first summary line");
    thought(p, c, y + 1, "Looking for where the manifest is parsed", "2.1s", false);
    y += 3;
    caption(p, c.x, y, "open · the summary behind a rail");
    thought(p, c, y + 1, "", "2.1s", true);
    y += 2;
    let lines = wrap_lines(OPEN, c.w - 4, c.w - 4);
    for (i, l) in lines.iter().enumerate() {
        let yy = y + i as u16;
        p.put(c.x + 2, yy, &[(t.g("rail"), t.hair()), (" ", t.muted()), (l, italic(t.muted()))]);
    }
    y += lines.len() as u16 + 1;
    caption(p, c.x, y, "running · the last 5 wrapped rows");
    p.put(c.x, y + 1, &[(t.g("open"), t.muted()), (" ", t.muted()), ("Thinking…", bold(t.muted())), (" 2.4s", t.muted())]);
    let all = wrap_lines(LIVE, c.w - 4, c.w - 4);
    let all = &all[..];
    let n = all.len();
    let shown = &all[n.saturating_sub(5)..];
    p.put(c.x + 2, y + 2, &[(t.g("rail"), t.hair()), (" ", t.muted()), (t.g("ellipsis"), t.muted()), (&format!(" {} earlier lines", n - shown.len()), t.muted())]);
    for (i, l) in shown.iter().enumerate() {
        p.put(c.x + 2, y + 3 + i as u16, &[(t.g("rail_heavy"), t.fg("border.strong")), (" ", t.muted()), (l, italic(t.muted()))]);
    }
    y += 3 + shown.len() as u16 + 1;
    caption(p, c.x, y, "encrypted · no expansion");
    thought(p, c, y + 1, "", "2.1s", false);
    let x = c.x + 2 + crate::kit::width("Thought for 2.1s");
    p.put(x, y + 1, &[(" · ", t.muted()), ("encrypted, replayed to the provider", italic(t.muted()))]);
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d07-reasoning", caption: "Reasoning: finished, open, running, encrypted", w: 200, h: 50, modes: DL, ascii: false, draw: sheet }]
}
