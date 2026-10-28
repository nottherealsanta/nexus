//! D09 — diffs: split view (wide reading column) and unified view (narrow).
use super::{Shot, DL};
use crate::kit::{bold, trunc, P};
use crate::shell::{Centre, Shell};

/// A diff line: context, deletion or addition.
#[derive(Clone, Copy, PartialEq)]
enum K {
    Ctx,
    Del,
    Add,
}
use K::*;

struct Hunk {
    old: u16,
    new: u16,
    title: &'static str,
    lines: &'static [(K, &'static str)],
}

const LOADER: &[Hunk] = &[
    Hunk { old: 10, new: 10, title: "class Loader:", lines: &[
        (Ctx, "    def __init__(self, root: Path) -> None:"),
        (Ctx, "        self.root = root"),
        (Add, "        self._cache: dict[str, tuple[int, Manifest]] = {}"),
        (Ctx, ""),
        (Ctx, "    def load(self, name: str) -> Manifest:"),
        (Ctx, "        path = self.root / name"),
        (Del, "        return parse_manifest(path)"),
        (Add, "        key = path.stat().st_mtime_ns"),
        (Add, "        hit = self._cache.get(name)"),
        (Add, "        if hit and hit[0] == key:"),
        (Add, "            return hit[1]"),
        (Add, "        manifest = parse_manifest(path)"),
        (Add, "        self._cache[name] = (key, manifest)"),
        (Add, "        return manifest"),
    ] },
    Hunk { old: 41, new: 49, title: "def reload(self) -> None:", lines: &[
        (Ctx, "    def reload(self) -> None:"),
        (Del, "        self._manifest = None"),
        (Del, "        self._load_all()"),
        (Add, "        self._cache.clear()"),
        (Add, "        self._load_all()"),
        (Add, "        log.debug(\"loader cache cleared\")"),
        (Add, "        self._stats.reloads += 1"),
        (Ctx, "        return None"),
    ] },
];

const TEST: &[Hunk] = &[Hunk { old: 24, new: 24, title: "", lines: &[
    (Ctx, "def test_load_returns_manifest(tmp_path):"),
    (Ctx, "    ..."),
    (Add, ""),
    (Add, "def test_cache_hit_skips_parse(tmp_path, monkeypatch):"),
    (Add, "    calls = []"),
    (Add, "    monkeypatch.setattr(loader, \"parse_manifest\", lambda p: calls.append(p) or {})"),
    (Add, "    ld = Loader(tmp_path)"),
    (Add, "    ld.load(\"a\"); ld.load(\"a\")"),
    (Add, "    assert len(calls) == 1"),
] }];

fn counts(h: &[Hunk]) -> (usize, usize) {
    let n = |k| h.iter().flat_map(|x| x.lines).filter(|(q, _)| *q == k).count();
    (n(Add), n(Del))
}

fn file_header(p: &mut P, c: Centre, y: u16, path: &str, hunks: &[Hunk]) {
    let t = p.t;
    let (a, d) = counts(hunks);
    p.put(c.x, y, &[(path, bold(t.secondary())), ("  ", t.muted()), (&format!("+{a}"), t.fg("diff.add")), (" ", t.muted()), (&format!("−{d}"), t.fg("diff.del"))]);
}

fn hunk_title(h: &Hunk) -> String {
    let (mut o, mut n) = (0, 0);
    for (k, _) in h.lines {
        if *k != Add {
            o += 1;
        }
        if *k != Del {
            n += 1;
        }
    }
    let tail = if h.title.is_empty() { String::new() } else { format!(" {}", h.title) };
    format!("@@ -{},{o} +{},{n} @@{tail}", h.old, h.new)
}

/// Split view rows: old number | old text │ new number | new text. Returns the next row.
fn split(p: &mut P, c: Centre, mut y: u16, limit: u16, hunks: &[Hunk]) -> u16 {
    let t = p.t;
    let half = (c.w - 1) / 2;
    let (lx, rx) = (c.x, c.x + half + 1);
    let rw = c.w - half - 1;
    let hatch = if t.ascii { "/" } else { "╱" };
    for h in hunks {
        if y >= limit {
            return y;
        }
        p.put(c.x, y, &[("⋯ ", t.muted()), (&hunk_title(h), t.muted())]);
        y += 1;
        let (mut o, mut n) = (h.old, h.new);
        for (k, s) in h.lines {
            if y >= limit {
                return y;
            }
            let (lt, rt) = match k {
                Ctx => (Some(o), Some(n)),
                Del => (Some(o), None),
                Add => (None, Some(n)),
            };
            for (side, num, x, w) in [(0, lt, lx, half), (1, rt, rx, rw)] {
                match num {
                    Some(no) => {
                        let tint = match (k, side) {
                            (Del, 0) => Some("diff.del_bg"),
                            (Add, 1) => Some("diff.add_bg"),
                            _ => None,
                        };
                        if let Some(bg) = tint {
                            p.fill(x, y, w, 1, bg);
                        }
                        let b = tint.map(|b| t.bg(b)).unwrap_or_default();
                        let (ns, ts) = if tint.is_some() { (t.muted().patch(b), t.primary().patch(b)) } else { (t.muted().patch(b), t.muted().patch(b)) };
                        p.put(x, y, &[(&format!("{no:>4}"), ns)]);
                        let mark = match (k, side) {
                            (Del, 0) => Some(("-", t.fg("diff.del").patch(b))),
                            (Add, 1) => Some(("+", t.fg("diff.add").patch(b))),
                            _ => None,
                        };
                        if let Some((m, ms)) = mark {
                            p.put(x + 5, y, &[(m, ms)]);
                        }
                        p.put_clip(x + 7, y, &[(&trunc(s, w - 8), ts)], x + w);
                    }
                    None => p.hline(x, y, w, hatch, t.hair()),
                }
            }
            p.put(c.x + half, y, &[("│", t.fg("border.strong"))]);
            if *k != Add {
                o += 1;
            }
            if *k != Del {
                n += 1;
            }
            y += 1;
        }
        y += 1;
    }
    y
}

/// Unified view: new line numbers, `+`/`-`, text on the tint.
fn unified(p: &mut P, c: Centre, mut y: u16, limit: u16, hunks: &[Hunk]) -> u16 {
    let t = p.t;
    for h in hunks {
        if y >= limit {
            return y;
        }
        p.put(c.x, y, &[("⋯ ", t.muted()), (&hunk_title(h), t.muted())]);
        y += 1;
        let (mut o, mut n) = (h.old, h.new);
        let _ = &mut o;
        for (k, s) in h.lines {
            if y >= limit {
                return y;
            }
            let tint = match k {
                Del => Some("diff.del_bg"),
                Add => Some("diff.add_bg"),
                Ctx => None,
            };
            if let Some(bg) = tint {
                p.fill(c.x, y, c.w, 1, bg);
            }
            let b = tint.map(|b| t.bg(b)).unwrap_or_default();
            let num = if *k == Del { "    ".to_string() } else { format!("{n:>4}") };
            let (m, ms) = match k {
                Del => ("-", t.fg("diff.del").patch(b)),
                Add => ("+", t.fg("diff.add").patch(b)),
                Ctx => (" ", t.muted()),
            };
            let ts = if tint.is_some() { t.primary().patch(b) } else { t.muted() };
            p.put(c.x, y, &[(&num, t.muted().patch(b)), (" ", b), (m, ms), (" ", b)]);
            p.put_clip(c.x + 7, y, &[(&trunc(s, c.w - 8), ts)], c.x + c.w);
            if *k != Del {
                n += 1;
            }
            y += 1;
        }
        y += 1;
    }
    y
}

fn wide(p: &mut P) {
    let c = Shell { left_w: 34, right_w: 36, ..Shell::default() }.draw(p);
    let t = p.t;
    p.put(c.x, 2, &[(t.g("open"), t.muted()), (" ", t.muted()), ("Edited 2 files", t.secondary())]);
    file_header(p, c, 4, "src/nexus/loader.py", LOADER);
    let y = split(p, c, 5, c.bottom, LOADER);
    file_header(p, c, y, "tests/test_loader.py", TEST);
    split(p, c, y + 1, c.bottom, TEST);
}

fn narrow(p: &mut P) {
    let c = Shell::default().draw(p);
    let t = p.t;
    p.put(c.x, 2, &[(t.g("open"), t.muted()), (" ", t.muted()), ("Edited 2 files", t.secondary())]);
    file_header(p, c, 4, "src/nexus/loader.py", LOADER);
    unified(p, c, 5, c.bottom, LOADER);
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d09-diff-split", caption: "Diff, split view (reading column ≥ 110 cells): old | new, hatch on the empty side", w: 200, h: 50, modes: DL, ascii: false, draw: wide },
        Shot { id: "d09-diff-unified", caption: "Diff, unified view (narrow column): new line numbers, +/- and tints", w: 120, h: 36, modes: DL, ascii: false, draw: narrow },
    ]
}
