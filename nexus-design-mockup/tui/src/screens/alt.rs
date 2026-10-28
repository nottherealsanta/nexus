//! Alternatives for the chooser (options.json): the non-recommended options,
//! drawn as variations of the recommended shell. Ids are `alt-dNN-name`.
use super::{common, d02_shell, Shot, DL};
use crate::kit::{bold, italic, P};
use crate::shell::{prose, user_block, Busy, Centre, Shell, SESSIONS};
use crate::theme::{Mode, Th};
use ratatui::style::Style;

/// Palette overrides for the D01 alternatives, applied before drawing.
pub fn palette(id: &str, t: &mut Th) {
    let light = t.mode == Mode::Light;
    if id.starts_with("alt-d01-oldpalette") {
        // Old Nexus "Grok Night" (rust/tui/src/render.rs) and its light twin.
        let v: &[(&str, &str, &str)] = &[
            ("surface.canvas", "#141414", "#f5f5f5"),
            ("surface.sidebar", "#141414", "#f5f5f5"),
            ("surface.panel", "#1c1c1c", "#eeeeee"),
            ("surface.raised", "#242424", "#e4e4e4"),
            ("text.primary", "#e1e1e1", "#262626"),
            ("text.secondary", "#a0a0a0", "#4a4a4a"),
            ("text.muted", "#787878", "#767676"),
            ("border.default", "#323237", "#d4d4d4"),
            ("interactive.accent", "#fab283", "#c8672f"),
            ("interactive.focus", "#fab283", "#c8672f"),
            ("border.focus", "#fab283", "#c8672f"),
            ("interactive.selection", "#2c2c2c", "#e4e4e4"),
            ("interactive.hover", "#242424", "#ebebeb"),
            ("status.success", "#9ece6a", "#2f7a1f"),
            ("status.error", "#f7768e", "#c0283f"),
            ("status.info", "#7aa2f7", "#2a5fc4"),
            ("agent.blue", "#7aa2f7", "#2459ac"),
        ];
        for (k, d, l) in v {
            t.set(k, if light { l } else { d });
        }
    }
    if id.starts_with("alt-d01-coolfocus") {
        let c = if light { "#2a5fc4" } else { "#7aa7f0" };
        t.set("interactive.focus", c);
        t.set("border.focus", c);
    }
}

fn wide() -> Shell {
    Shell { left_w: 34, right_w: 36, ..Shell::default() }
}

fn caption(p: &mut P, x: u16, y: u16, s: &str) {
    let t = p.t;
    p.put(x, y, &[(s, italic(t.muted()))]);
}

// ---------------------------------------------------------------- D01

fn coolfocus(p: &mut P) {
    let s = Shell { focus_row: Some(1), ..wide() };
    let c = s.draw(p);
    let y = common::turn_one(p, c, 2);
    user_block(p, c, y, &["Go ahead, but keep the change small."], 2, "3:44 PM", true, false);
}

// ---------------------------------------------------------------- D02

fn tabs(p: &mut P) {
    let s = Shell { left: None, right_w: 36, ..Shell::default() };
    let c = s.draw(p);
    let t = p.t;
    // Old Nexus top bar: open sessions as tabs, then a location row and a rule.
    p.fill(0, 0, p.w, 2, "surface.canvas");
    let mut x = 2;
    let tab_list: &[(&str, &str, bool)] = &[("·", "Refactor the loader", true), ("◆", "Review the CJK width handling", false), ("⠹", "Release notes for 0.10", false)];
    for (g, title, active) in tab_list {
        let st = if *active { bold(t.primary()).patch(t.bg("surface.panel")) } else { t.muted() };
        let gs = match *g {
            "◆" => t.fg("status.warning"),
            "⠹" => t.primary(),
            _ => t.muted(),
        };
        let bg = if *active { t.bg("surface.panel") } else { Style::default() };
        x = p.put(x, 0, &[(" ", bg), (g, gs.patch(bg)), (" ", bg), (title, st), (" ", bg), ("×", t.muted().patch(bg)), (" ", bg)]);
        x = p.put(x, 0, &[("│", t.muted())]);
    }
    p.put(x, 0, &[(" + ", t.muted())]);
    p.put_r(p.w - 2, 0, &[("● 1", t.fg("status.warning")), ("  ▣  ▤  ▥", t.muted())]);
    p.put(2, 1, &[("~/repos/nexus-demo", t.primary()), ("  ·  ", t.muted()), ("main", t.muted()), ("  ·  ", t.muted()), ("checkout", t.muted())]);
    p.put_r(c.bx + c.bw - 2, 1, &[("Idle", t.muted())]);
    p.hline(2, 2, c.bx + c.bw - 4, "─", t.hair());
    let cc = Centre { bx: 0, bw: c.bx + c.bw, x: 2, w: c.bx + c.bw - 4, top: 3, bottom: c.bottom };
    let y = common::turn_one(p, cc, 4);
    user_block(p, cc, y, &["Go ahead, but keep the change small."], 2, "3:44 PM", false, false);
}

// ---------------------------------------------------------------- D03

fn status_groups(p: &mut P) {
    let s = wide();
    let c = s.draw(p);
    common::turn_one(p, c, 2);
    let t = p.t;
    let lw = 34;
    p.fill(0, 1, lw, p.h - 3, "surface.sidebar");
    p.put(2, 2, &[(t.g("search"), t.muted()), ("  Search sessions", t.muted())]);
    p.put_r(lw - 2, 2, &[("ctrl+k", t.muted())]);
    let groups: &[(&str, &[(usize, &str)])] = &[("Needs you", &[(1, "waiting")]), ("Working", &[(2, "running")]), ("Idle", &[(0, ""), (3, "error"), (4, "unread")])];
    let mut y = 4;
    for (name, rows) in groups {
        let n = format!(" {}", rows.len());
        p.put(2, y, &[("▾ ", t.muted()), (name, bold(t.fg("agent.violet"))), (Box::leak(n.into_boxed_str()), t.muted())]);
        y += 1;
        for (i, mark) in rows.iter() {
            let r = &SESSIONS[*i];
            let open = *i == 0;
            if open {
                p.fill(0, y, lw, 1, "interactive.selection");
            }
            let bgs = if open { t.bg("interactive.selection") } else { Style::default() };
            let (g, gs) = match *mark {
                "waiting" => ("◆", t.fg("status.warning")),
                "running" => ("⠹", t.fg("agent.violet")),
                "error" => ("✕", t.fg("status.error")),
                "unread" => ("●", t.fg("status.info")),
                _ => ("◇", t.muted()),
            };
            p.put(2, y, &[(g, gs.patch(bgs))]);
            let title = crate::kit::trunc(r.title, lw - 10);
            p.put(4, y, &[(&title, if open { bold(t.primary()) } else { t.primary() }.patch(bgs))]);
            p.put_r(lw - 2, y, &[(r.when, t.muted().patch(bgs))]);
            y += 1;
        }
        y += 1;
    }
    p.put(4, y, &[("+ New session", t.muted())]);
}

// ---------------------------------------------------------------- D04

/// Old Nexus user card: inset 2, no rail, fold marker dim in the gutter.
fn inset_card(p: &mut P, c: Centre, y: u16, lines: &[&str], n: u8, time: &str) -> u16 {
    let t = p.t;
    let rows = lines.len() as u16 + 2;
    let x = c.x;
    let w = c.w;
    p.fill(x, y, w, rows, "surface.raised");
    let rb = t.bg("surface.raised");
    p.put(x - 2, y + 1, &[(t.g("open"), t.hair())]);
    let tag = format!(" #{n} · {time} ");
    for (i, l) in lines.iter().enumerate() {
        p.put(x + 3, y + 1 + i as u16, &[(l, t.primary().patch(rb))]);
    }
    p.put_r(x + w - 1, y + 1, &[(Box::leak(tag.into_boxed_str()), t.muted().patch(rb))]);
    rows
}

fn inset(p: &mut P) {
    let s = wide();
    let c = s.draw(p);
    let t = p.t;
    p.fill(c.bx, 1, c.bw, c.bottom - 1, "surface.canvas");
    let mut y = 2;
    y += inset_card(p, c, y, &["The loader re-reads the manifest on every call. Can you cache it and add a test?"], 1, "3:42 PM") + 1;
    y += prose(p, c, y, "I'll start by finding where the manifest is parsed.") + 1;
    crate::shell::group(p, c, y, &[("Read 2 files", t.secondary()), (", ", t.muted()), ("ran 1 command", t.secondary())], "1.4s", false, false);
    y += 2;
    y += prose(p, c, y, "Done: parse_manifest is memoized on the file's mtime and a regression test counts parses.") + 1;
    crate::shell::footer(p, c, y, "build", "gpt-6.1-terra", &["3 tools", "4.2s"], "done");
    y += 2;
    inset_card(p, c, y, &["Go ahead, but keep the change small.", "日本語のコメントも残してください。"], 2, "3:44 PM");
}

// ---------------------------------------------------------------- D05

fn footer_left(p: &mut P) {
    let s = wide();
    let c = s.draw(p);
    let t = p.t;
    p.fill(c.bx, 1, c.bw, c.bottom - 1, "surface.canvas");
    let mut y = 2;
    y += user_block(p, c, y, &["The loader re-reads the manifest on every call. Can you cache it and add a test?"], 1, "3:42 PM", false, false) + 1;
    y += prose(p, c, y, "Done: parse_manifest is memoized on the file's mtime and a regression test counts parses.") + 1;
    p.hline(c.x, y, c.w, t.g("rule"), t.hair());
    y += 1;
    p.put(c.x, y, &[("codex/gpt-6.1-terra · medium · 4.2s · 2 calls · 3 tools · 12k in 9k cached 2k out · $0.04 · ", t.muted()), (t.g("done"), t.fg("status.success")), (" done", t.fg("status.success"))]);
    y += 2;
    y += user_block(p, c, y, &["Now run the whole suite."], 2, "3:44 PM", false, false) + 1;
    y += prose(p, c, y, "The suite was interrupted while pytest was running.") + 1;
    p.hline(c.x, y, c.w, t.g("rule"), t.hair());
    p.put(c.x, y + 1, &[("codex/gpt-6.1-terra · medium · 12.0s · 1 call · 1 tool · ", t.muted()), (t.g("interrupted"), t.fg("status.warning")), (" interrupted", t.fg("status.warning"))]);
}

// ---------------------------------------------------------------- D06 / D07

fn status_only(p: &mut P) {
    let s = wide();
    let c = s.draw(p);
    let t = p.t;
    p.fill(c.bx, 1, c.bw, c.bottom - 1, "surface.canvas");
    let mut y = 2;
    y += user_block(p, c, y, &["The loader re-reads the manifest on every call. Can you cache it and add a test?"], 1, "3:42 PM", false, false) + 1;
    crate::shell::group(p, c, y, &[("Read 2 files", t.secondary()), (", ", t.muted()), ("searched 1", t.secondary()), (", ", t.muted()), ("edited 1 file", t.secondary())], "1.6s", true, false);
    y += 1;
    let rows: &[(&str, &str, &str, &str, &str)] = &[
        ("done", "Read", "src/nexus/loader.py  1-120", "120 lines", "0.1s"),
        ("done", "Search", "\"parse_manifest\" in src", "4 matches", "0.2s"),
        ("done", "Read", "tests/test_loader.py", "48 lines", "0.1s"),
        ("error", "Edit", "src/nexus/loader.py", "line changed since read", "0.3s"),
        ("running", "Shell", "pytest tests/test_loader.py -q", "", "3.4s"),
    ];
    for (i, (st, verb, arg, sum, dur)) in rows.iter().enumerate() {
        let tree = if i + 1 == rows.len() { t.g("tree_end") } else { t.g("tree_mid") };
        let (g, gs) = match *st {
            "done" => (t.g("done"), t.fg("status.success")),
            "error" => (t.g("error"), t.fg("status.error")),
            _ => (t.g("running"), t.primary()),
        };
        let ss = if *st == "error" { t.fg("status.error") } else { t.muted() };
        p.put(c.x + 2, y, &[(tree, t.hair()), (" ", t.muted()), (g, gs), (" ", t.muted()), (verb, bold(t.secondary())), (" ", t.muted()), (arg, t.secondary()), ("  ", t.muted()), (sum, ss)]);
        p.put_r(c.x + c.w, y, &[(dur, t.muted())]);
        y += 1;
    }
    caption(p, c.x, y + 1, "status glyph per member instead of the verb glyph (→ ← $ *)");
}

fn diamond(p: &mut P) {
    let s = wide();
    let c = s.draw(p);
    let t = p.t;
    p.fill(c.bx, 1, c.bw, c.bottom - 1, "surface.canvas");
    let mut y = 2;
    y += user_block(p, c, y, &["how to start the daemon"], 2, "3:42 PM", false, false) + 1;
    let it = italic(t.muted());
    p.put(c.x, y, &[("◇ ", t.muted()), ("I'll check the project setup first", it), (" ▸", t.muted())]);
    y += 1;
    p.put(c.x, y, &[("* ", t.muted()), ("Glob \"**/*\" in . (100 matches)", t.secondary())]);
    p.put(c.x, y + 1, &[("* ", t.muted()), ("Grep \"daemon|serve\" in . (56 matches)", t.secondary())]);
    p.put(c.x, y + 2, &[("→ ", t.muted()), ("Read README.md [offset=90, limit=38]", t.secondary())]);
    p.put(c.x, y + 3, &[("Thought", t.secondary()), (" · 402ms", t.muted())]);
    y += 5;
    y += prose(p, c, y, "Run `nexus daemon start` from the repo root; `nexus daemon stop` stops it.") + 1;
    crate::shell::footer(p, c, y, "build", "gpt-6.1-terra", &["11.6s"], "done");
}

// ---------------------------------------------------------------- D10

fn chips(p: &mut P) {
    let s = Shell { open: None, crumbs: &["nexus-demo", "New session"], ..wide() };
    let c = s.draw(p);
    let t = p.t;
    let chip = |p: &mut P, y: u16, label: &str, tok: &str| {
        let st = bold(t.fg("surface.canvas")).bg(t.color("text.secondary"));
        let x = p.put(c.x, y, &[(" ", st), (label, st), (" ", st)]);
        p.put(x + 1, y, &[(tok, t.muted())]);
    };
    let mut y = 3;
    chip(p, y, "System prompt", "~2,140 tokens");
    p.put(c.x + 2, y + 1, &[("You are Nexus, a provider-agnostic coding agent working in the user's workspace.  … +38 more lines", t.secondary())]);
    y += 3;
    chip(p, y, "Tools", "~4,780 tokens");
    p.put(c.x + 2, y + 1, &[("read     bash     edit     write     ask     code     session     workspace", t.primary())]);
    y += 3;
    chip(p, y, "AGENTS.md", "~3,880 tokens");
    p.put(c.x + 2, y + 1, &[("~/repos/nexus-demo/AGENTS.md", t.secondary())]);
    y += 3;
    chip(p, y, "Skills", "none");
    y += 2;
    chip(p, y, "MCP", "not configured");
}

// ---------------------------------------------------------------- D11

fn rail(p: &mut P) {
    let s = Shell { composer: false, ..wide() };
    let c = s.draw(p);
    let y = common::turn_one(p, c, 2);
    let _ = y;
    let t = p.t;
    let y0 = p.h - 7;
    let (x, w) = (c.x, c.w);
    p.fill(x, y0, w, 5, "surface.panel");
    let pb = t.bg("surface.panel");
    p.vline(x, y0, 5, t.g("focus"), t.fg("interactive.focus").patch(pb));
    p.put(x + 3, y0 + 1, &[("Type a message…", t.muted().patch(pb))]);
    p.put(x + 3, y0 + 3, &[("Build", bold(t.agent("build")).patch(pb)), ("  gpt-6.1-terra", t.primary().patch(pb)), (" codex", t.muted().patch(pb)), ("  medium", t.secondary().patch(pb))]);
    let mx = x + w - 30;
    p.hline(mx, y0 + 3, 8, "━", t.accent().patch(pb));
    p.hline(mx + 8, y0 + 3, 12, "━", t.hair().patch(pb));
    p.put(mx + 21, y0 + 3, &[("41%", t.secondary().patch(pb)), (" 82k", t.muted().patch(pb))]);
    p.put(x + 1, y0 + 6, &[("nexus-demo", bold(t.primary())), ("  ~/repos/nexus-demo  ⌥ main  checkout", t.muted())]);
}

// ---------------------------------------------------------------- D12

fn details_tabs(p: &mut P) {
    let s = wide();
    let c = s.draw(p);
    common::turn_one(p, c, 2);
    let t = p.t;
    let rw = 36;
    let x0 = p.w - rw;
    p.fill(x0, 1, rw, p.h - 1, "surface.sidebar");
    let mut x = x0 + 1;
    for (i, name) in ["Session", "Changes", "Git", "Jobs"].iter().enumerate() {
        let st = if i == 0 { bold(t.primary()).patch(t.bg("surface.raised")) } else { t.muted() };
        let bg = if i == 0 { t.bg("surface.raised") } else { Style::default() };
        x = p.put(x, 2, &[(" ", bg), (name, st), (" ", bg)]) + 1;
    }
    p.hline(x0 + 1, 3, rw - 2, "─", t.hair());
    p.put(x0 + 2, 5, &[("SESSION METADATA", bold(t.muted()))]);
    let rows: &[(&str, &str)] = &[("Status:", "idle"), ("Agent:", "Build"), ("Model:", "gpt-6.1-terra codex"), ("Effort:", "medium"), ("Turns:", "2"), ("Tool calls:", "3"), ("Context:", "82k (41%)"), ("Started:", "3:40 PM"), ("Id:", "ses_1a1…5a19")];
    for (i, (k, v)) in rows.iter().enumerate() {
        let y = 7 + i as u16;
        p.put(x0 + 2, y, &[(k, t.muted())]);
        p.put(x0 + 14, y, &[(v, t.primary())]);
    }
    p.put(x0 + 2, p.h - 2, &[("tab next section · enter copy", t.muted())]);
}

// ---------------------------------------------------------------- D13

fn accentfill(p: &mut P) {
    d02_shell::main_view(p);
    p.scrim();
    let t = p.t;
    let (w, h) = (88u16, 30u16);
    let x = (p.w - w) / 2;
    let y = (p.h - h) / 2;
    p.fill(x, y, w, h, "surface.raised");
    p.frame(x, y, w, h, t.fg("border.default"), true, &[("Settings", bold(t.primary()))]);
    let rb = t.bg("surface.raised");
    p.put_r(x + w - 3, y, &[(" esc ", t.muted().patch(rb))]);
    p.put(x + 3, y + 2, &[("Search", t.muted().patch(rb))]);
    let sections: &[(&str, &[(&str, &str)])] = &[
        ("Appearance", &[("Theme", "dark"), ("Monochrome mode", "off"), ("Animations", "on"), ("Sidebar", "full"), ("Details sidebar", "on")]),
        ("Providers", &[("Sign in", "unavailable"), ("Sign out", "unavailable")]),
        ("Models", &[("Default model", "gpt-6.1-terra"), ("Default effort", "medium")]),
    ];
    let mut yy = y + 4;
    let mut first = true;
    for (name, rows) in sections {
        p.put(x + 3, yy, &[(name, bold(t.fg("agent.violet")).patch(rb))]);
        yy += 1;
        for (k, v) in rows.iter() {
            if first {
                let st = bold(t.fg("interactive.on_accent")).bg(t.color("interactive.accent"));
                p.hline(x + 1, yy, w - 2, " ", st);
                p.put(x + 1, yy, &[("● ", st), (k, st)]);
                p.put_r(x + w - 3, yy, &[(v, st)]);
                first = false;
            } else {
                let dis = *v == "unavailable";
                p.put(x + 3, yy, &[(k, if dis { t.muted() } else { t.primary() }.patch(rb))]);
                p.put_r(x + w - 3, yy, &[(v, if dis { t.muted() } else { t.secondary() }.patch(rb))]);
            }
            yy += 1;
        }
        yy += 1;
    }
    p.put(x + 3, y + h - 2, &[("←/→ change  ·  enter select  ·  type to search", t.muted().patch(rb))]);
}

// ---------------------------------------------------------------- D14

fn buttons(p: &mut P) {
    let s = Shell { busy: Busy::Waiting, ..wide() };
    let c = s.draw(p);
    common::turn_one(p, c, 2);
    let t = p.t;
    let h = 8;
    let y0 = c.bottom - h;
    p.fill(c.x, y0, c.w, h, "surface.panel");
    let pb = t.bg("surface.panel");
    p.vline(c.x, y0, h, t.g("rail_heavy"), t.accent().patch(pb));
    p.put(c.x + 2, y0 + 1, &[(t.g("waiting"), t.accent().patch(pb)), (" waiting", t.accent().patch(pb)), (" · ", t.muted().patch(pb)), ("bash", bold(t.primary()).patch(pb)), ("  pytest tests/test_loader.py -q", t.secondary().patch(pb))]);
    p.put_r(c.x + c.w - 2, y0 + 1, &[("1 of 2  tab next", t.muted().patch(pb))]);
    p.put(c.x + 4, y0 + 2, &[("cwd ~/repos/nexus-demo · policy ask", t.muted().patch(pb))]);
    let btns: &[(&str, bool, bool)] = &[("allow once", true, true), ("allow session", false, false), ("deny", false, false), ("deny with note", false, false)];
    let mut x = c.x + 4;
    for (label, focus, def) in btns {
        let st = if *focus { Style::default().fg(t.color("surface.panel")).bg(t.color("text.primary")) } else if *def { bold(t.primary()).patch(pb) } else { t.primary().patch(pb) };
        let txt = format!("[ {label} ]");
        x = p.put(x, y0 + 4, &[(Box::leak(txt.into_boxed_str()), st)]) + 2;
    }
    p.put(c.x + 4, y0 + 6, &[("a once · s session · d deny · n note", t.muted().patch(pb))]);
}

// ---------------------------------------------------------------- D15

fn toasts_bottom(p: &mut P) {
    d02_shell::main_view(p);
    let t = p.t;
    let (lw, rw) = (34u16, 36u16);
    let right = p.w - rw - 2;
    let w = 44;
    let x = right - w;
    let base = p.h - 10;
    let items: &[(&str, &str, &str)] = &[("status.success", "✓", "Copied session id"), ("status.warning", "!", "Retrying provider (attempt 2 of 3)"), ("status.error", "✕", "Export failed: disk full")];
    for (i, (tok, g, title)) in items.iter().enumerate() {
        let y = base - (items.len() - i) as u16 * 2 + 1;
        p.fill(x, y, w, 1, "surface.raised");
        let rb = t.bg("surface.raised");
        p.put(x, y, &[(t.g("focus"), t.fg(tok).patch(rb)), (" ", rb), (g, bold(t.fg(tok)).patch(rb)), (" ", rb), (title, bold(t.primary()).patch(rb))]);
        p.put_r(x + w - 1, y, &[("×", t.muted().patch(rb))]);
    }
    let _ = lw;
    caption(p, x, base, "toasts sit right above the composer card");
}

// ---------------------------------------------------------------- registry

pub fn shots() -> Vec<Shot> {
    let s = |id, caption, draw: fn(&mut P)| Shot { id, caption, w: 200, h: 50, modes: DL, ascii: false, draw };
    vec![
        s("alt-d01-oldpalette", "D01 B: old Nexus Grok Night palette", d02_shell::main_view),
        s("alt-d01-coolfocus", "D01 C: cool focus colour", coolfocus),
        s("alt-d02-tabs", "D02 B: old tab strip and location row", tabs),
        s("alt-d03-status-groups", "D03 B: status groups", status_groups),
        s("alt-d04-inset", "D04 B: inset card", inset),
        s("alt-d05-left", "D05 B: footer left under a hairline", footer_left),
        s("alt-d06-status", "D06 B: status glyphs only", status_only),
        s("alt-d07-diamond", "D07 B: ◇ preview line", diamond),
        s("alt-d10-chips", "D10 B: inverted label chips", chips),
        s("alt-d11-rail", "D11 B: card with focus rail", rail),
        s("alt-d12-tabs", "D12 B: tabbed details", details_tabs),
        s("alt-d13-accentfill", "D13 B: accent fill, violet headings", accentfill),
        s("alt-d14-buttons", "D14 B: button row", buttons),
        s("alt-d15-bottom", "D15 B: toasts bottom-right", toasts_bottom),
    ]
}
