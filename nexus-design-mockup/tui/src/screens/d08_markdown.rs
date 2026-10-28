//! D08 — markdown and code fences: headings, inline code, lists, table, quote, link, fence.
use super::{Shot, DL};
use crate::kit::{bold, under, P};
use crate::shell::{code_style, Shell};

fn sheet(p: &mut P) {
    let c = Shell { left_w: 34, right_w: 36, ..Shell::default() }.draw(p);
    let t = p.t;
    let (x, w) = (c.x, c.w);
    let cs = code_style(p);
    let mut y = 2;
    // H1: bold primary, muted underline the width of the text.
    p.put(x, y, &[("Loader cache", bold(t.primary()))]);
    p.hline(x, y + 1, 12, t.g("rule"), t.muted());
    y += 3;
    p.put(x, y, &[("The loader now keeps one parsed manifest per file and reuses it until the file's mtime changes. ", t.primary()), (" parse_manifest ", cs), (" runs once", t.primary())]);
    p.put(x, y + 1, &[("per file version instead of once per request, and ", t.primary()), (" Loader.load ", cs), (" no longer touches the disk on a hit.", t.primary())]);
    y += 3;
    p.put(x, y, &[("What changed", bold(t.primary()))]);
    y += 1;
    for (ind, g, segs) in [
        (0, "•", vec![("A per-path cache on ", t.primary()), (" Loader ", cs), (", keyed by mtime", t.primary())]),
        (2, "◦", vec![("Invalidated when the file changes, so edits still show up", t.primary())]),
        (2, "◦", vec![("Cleared by ", t.primary()), (" Loader.reload() ", cs)]),
        (0, "•", vec![("A regression test that counts parses", t.primary())]),
    ] {
        p.put(x + ind, y, &[(g, t.secondary()), (" ", t.muted())]);
        p.put(x + ind + 2, y, &segs);
        y += 1;
    }
    y += 1;
    p.put(x, y, &[("Checks", bold(t.secondary()))]);
    y += 1;
    for (done, s) in [(true, "Cache hit skips the parse"), (true, "Edits invalidate the entry"), (false, "Thread-safety under contention")] {
        let (m, ms) = if done { ("[✓]", t.fg("status.success")) } else { ("[ ]", t.muted()) };
        p.put(x, y, &[(m, ms), (" ", t.muted()), (s, t.primary())]);
        y += 1;
    }
    y += 1;
    // Table: box-drawn in border.default, bold header.
    let cols = [24u16, 16, 16];
    let hr = |l: &str, m: &str, r: &str| {
        let mut s = String::from(l);
        for (i, w) in cols.iter().enumerate() {
            s.push_str(&"─".repeat(*w as usize + 2));
            s.push_str(if i + 1 == cols.len() { r } else { m });
        }
        s
    };
    p.put(x, y, &[(&hr("┌", "┬", "┐"), t.hair())]);
    let row = |p: &mut P, y: u16, cells: [&str; 3], head: bool| {
        let mut cx = x;
        p.put(cx, y, &[("│", t.hair())]);
        cx += 1;
        for (i, w) in cols.iter().enumerate() {
            let st = if head { bold(t.primary()) } else if i == 0 { t.primary() } else { t.secondary() };
            p.put(cx + 1, y, &[(cells[i], st)]);
            cx += w + 2;
            p.put(cx, y, &[("│", t.hair())]);
            cx += 1;
        }
    };
    row(p, y + 1, ["Case", "Before", "After"], true);
    p.put(x, y + 2, &[(&hr("├", "┼", "┤"), t.hair())]);
    row(p, y + 3, ["100 loads, one file", "100 parses", "1 parse"], false);
    row(p, y + 4, ["Edit between loads", "1 parse", "2 parses"], false);
    p.put(x, y + 5, &[(&hr("└", "┴", "┘"), t.hair())]);
    y += 7;
    // Link and block quote.
    p.put(x, y, &[("The cache lives in ", t.primary()), ("src/nexus/loader.py:42", under(t.fg("status.info"))), (", and the test is in ", t.primary()), ("tests/test_loader.py:18", under(t.fg("status.info"))), (".", t.primary())]);
    y += 2;
    p.put(x, y, &[("│", t.fg("border.strong")), (" Entries are never evicted; the cache is bounded by the number of manifests in the workspace.", t.secondary())]);
    y += 2;
    // Fence: panel block, language left, `c copy` right, code inset by 2, no rule and no rail.
    let code = ["def load(self, name: str) -> Manifest:", "    path = self.root / name", "    key = path.stat().st_mtime_ns", "    if (hit := self._cache.get(name)) and hit[0] == key:", "        return hit[1]"];
    let h = code.len() as u16 + 2;
    p.fill(x, y, w, h, "surface.panel");
    let pb = t.bg("surface.panel");
    p.put(x - 2, y + 1, &[(t.g("focus"), t.fg("interactive.focus"))]);
    p.put(x + 2, y, &[("python", t.muted().patch(pb))]);
    p.put_r(x + w - 2, y, &[("c", bold(t.secondary()).patch(pb)), (" copy", t.muted().patch(pb))]);
    for (i, l) in code.iter().enumerate() {
        p.put(x + 2, y + 1 + i as u16, &[(l, t.primary().patch(pb))]);
    }
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d08-markdown", caption: "Markdown: headings, inline code, lists, table, link, quote and a focused fence", w: 200, h: 50, modes: DL, ascii: false, draw: sheet }]
}
