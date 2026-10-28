//! D13 — overlays: palette, settings, model picker, confirm. One modal recipe over a scrimmed shell.
use super::{Shot, DL, DLM};
use crate::kit::{bold, width, Seg, P};
use crate::shell::{backdrop, hints, modal, mrow, sel_row, Shell};
use ratatui::style::Style;

pub fn under(p: &mut P) {
    backdrop(p, &Shell { left_w: 34, right_w: 36, ..Shell::default() });
    p.scrim();
    // Mono: reversed cells need an explicit fg, or the SVG writer merges the whole row into one run.
    let fg = p.t.color("text.primary");
    for y in 0..p.h {
        for x in 0..p.w {
            let c = &mut p.buf[(x, y)];
            if c.modifier.contains(ratatui::style::Modifier::REVERSED) && c.fg == ratatui::style::Color::Reset {
                c.fg = fg;
            }
        }
    }
}

/// Search row and its hairline; returns the first body row.
fn search(p: &mut P, x: u16, y: u16, w: u16, query: &str, placeholder: &str) -> u16 {
    let t = p.t;
    let rb = t.bg("surface.raised");
    p.put(x + 3, y + 1, &[(t.g("search"), t.muted().patch(rb))]);
    if query.is_empty() {
        let (a, b) = placeholder.split_at(1);
        let cur = Style::default().fg(t.color("surface.raised")).bg(t.color("text.primary"));
        p.put(x + 6, y + 1, &[(a, cur), (b, t.muted().patch(rb))]);
    } else {
        p.put(x + 6, y + 1, &[(query, t.primary().patch(rb)), (" ", Style::default().bg(t.color("text.primary")))]);
    }
    p.hline(x + 1, y + 2, w - 2, t.g("rule"), t.hair().patch(rb));
    y + 3
}

fn heading(p: &mut P, x: u16, y: u16, s: &str) {
    let t = p.t;
    p.put(x + 3, y, &[(s, bold(t.muted()).patch(t.bg("surface.raised")))]);
}

fn palette(p: &mut P) {
    under(p);
    let t = p.t;
    let sections: [(&str, &[(&str, &str)]); 3] = [
        ("SESSION", &[("New session", "ctrl+t"), ("Switch session", "ctrl+k"), ("Rename session", "/rename"), ("Fold or unfold turn", "space"), ("Go to turn", "ctrl+g")]),
        ("MODEL AND AGENT", &[("Change model", "ctrl+m"), ("Change effort", "/effort"), ("Change agent", "/agent")]),
        ("APP", &[("Open settings", "ctrl+,"), ("Toggle left sidebar", "ctrl+b"), ("Toggle right sidebar", "alt+b"), ("Show keyboard shortcuts", "f1"), ("Change theme", "/theme"), ("Toggle wire view", "ctrl+w")]),
    ];
    let n: u16 = sections.iter().map(|(_, r)| r.len() as u16 + 1).sum::<u16>() + 2;
    let (w, h) = (64, n + 8);
    let (x, y) = ((p.w - w) / 2, 5);
    modal(p, x, y, w, h, "Command palette", true);
    let mut r = search(p, x, y + 2, w, "", "Type a command…");
    for (i, (name, rows)) in sections.iter().enumerate() {
        if i > 0 {
            r += 1;
        }
        heading(p, x, r, name);
        r += 1;
        for (label, key) in rows.iter() {
            let sel = *label == "Change model";
            let ks = if sel { t.secondary() } else { t.muted() };
            mrow(p, x, r, w, " ", t.muted(), &[(label, t.primary())], &[(key, ks)], sel);
            r += 1;
        }
    }
    hints(p, x, y + h - 2, w, &[("↑↓", "move"), ("enter", "run"), ("esc", "close")], "14 of 62");
}

fn settings(p: &mut P) {
    under(p);
    let t = p.t;
    let (w, h) = (88, 39);
    let (x, y) = ((p.w - w) / 2, 6);
    modal(p, x, y, w, h, "Settings", true);
    let rb = t.bg("surface.raised");
    let mut r = search(p, x, y + 2, w, "", "Filter settings…");
    let on = |b: bool| if b { ("on", t.primary()) } else { ("off", t.muted()) };
    // (label, value, value style, disabled)
    type Row<'a> = (&'a str, &'a str, Style, bool);
    let sections: Vec<(&str, Vec<Row>)> = vec![
        ("APPEARANCE", vec![
            ("Theme", "system", t.secondary(), false), ("Named theme", "—", t.muted(), true), ("Density", "comfortable", t.secondary(), false), ("Font", "system", t.secondary(), false),
            ("Font size", "14", t.secondary(), false), ("Monochrome mode", on(false).0, on(false).1, false), ("Animations", on(true).0, on(true).1, false), ("Sidebar", "full", t.secondary(), false),
            ("Details sidebar", on(true).0, on(true).1, false), ("Sessions shown", "this project", t.secondary(), false), ("Archived sessions", on(false).0, on(false).1, false),
        ]),
        ("PROVIDERS", vec![
            ("Providers", "codex · 1 signed in", t.secondary(), false), ("Sign in", "—", t.muted(), true), ("Sign out", "—", t.muted(), true),
            ("Provider default model", "gpt-6.1-terra", t.secondary(), false), ("Add OpenAI-compatible provider", "—", t.muted(), true),
        ]),
        ("MODELS", vec![("Default model", "codex/gpt-6.1-terra", t.secondary(), false), ("Default effort", "medium", t.secondary(), false), ("Model catalog", "214 models", t.secondary(), false), ("Refresh catalog", "enter", t.muted(), false)]),
        ("AGENTS", vec![("Loaded agents", "5", t.secondary(), false), ("Default root agent", "build", t.agent("build"), false)]),
    ];
    let mut sel_reason = "";
    for (i, (name, rows)) in sections.iter().enumerate() {
        if i > 0 {
            r += 1;
        }
        heading(p, x, r, name);
        r += 1;
        for (label, val, vs, off) in rows {
            let sel = *label == "Sign in";
            if sel {
                sel_reason = "Sign-in over the API is not in Core yet; use `nexus login` locally.";
            }
            let ls = if *off { t.muted() } else { t.primary() };
            mrow(p, x, r, w, " ", t.muted(), &[(label, ls)], &[(val, *vs)], sel);
            r += 1;
        }
    }
    p.put(x + 3, r, &[("▾ ", t.muted().patch(rb)), ("Tools · Skills · MCP · Context · Dictation · Keybindings · Notifications · About", t.muted().patch(rb))]);
    let fy = y + h - 5;
    p.hline(x + 1, fy, w - 2, t.g("rule"), t.hair().patch(rb));
    p.put(x + 3, fy + 1, &[("Sign in", bold(t.secondary()).patch(rb)), ("  ", rb), (sel_reason, t.muted().patch(rb))]);
    hints(p, x, y + h - 2, w, &[("↑↓", "move"), ("enter", "change"), ("tab", "section"), ("esc", "close")], "12 sections");
}

fn picker(p: &mut P) {
    under(p);
    let t = p.t;
    let (w, h) = (64, 22);
    let (x, y) = ((p.w - w) / 2, 8);
    modal(p, x, y, w, h, "Change model", true);
    let mut r = search(p, x, y + 2, w, "", "Search 214 models…");
    let groups: [(&str, &[(&str, &str, &str)]); 3] = [
        ("CODEX", &[("gpt-6.1-terra", "400k", "current"), ("gpt-6.1-mini", "400k", ""), ("gpt-6-codex", "272k", ""), ("gpt-5.5", "200k", "")]),
        ("ANTHROPIC", &[("claude-opus-5", "1M", ""), ("claude-sonnet-5-5", "1M", ""), ("claude-haiku-5", "200k", "")]),
        ("OPENROUTER · SIGN IN TO USE", &[("deepseek-v4", "256k", "off"), ("qwen3-coder-next", "256k", "off")]),
    ];
    for (i, (name, rows)) in groups.iter().enumerate() {
        if i > 0 {
            r += 1;
        }
        heading(p, x, r, name);
        r += 1;
        for (m, ctx, tag) in rows.iter() {
            let sel = *m == "gpt-6.1-mini";
            let off = *tag == "off";
            let (mark, ms) = if *tag == "current" { (t.g("done"), t.fg("status.success")) } else { (" ", t.muted()) };
            let ls = if off { t.muted() } else { t.primary() };
            mrow(p, x, r, w, mark, ms, &[(m, ls)], &[(ctx, if sel { t.secondary() } else { t.muted() })], sel);
            r += 1;
        }
    }
    hints(p, x, y + h - 2, w, &[("↑↓", "move"), ("enter", "select"), ("esc", "close")], "9 of 214");
}

fn confirm(p: &mut P) {
    under(p);
    let t = p.t;
    let (w, h) = (54, 12);
    let (x, y) = ((p.w - w) / 2, 16);
    modal(p, x, y, w, h, "Delete session?", true);
    let rb = t.bg("surface.raised");
    let n = p.wrap(x + 2, y + 3, w - 4, "“Fix flaky test in sync” and its 3 turns are removed from disk. Child sessions are kept. This cannot be undone.", t.primary().patch(rb));
    let by = y + 3 + n + 1;
    // Buttons: Cancel is the safe default (selected), Delete in status.error.
    let cancel: Vec<Seg> = vec![("[ ", t.muted()), ("Cancel", bold(t.primary())), (" ]", t.muted())];
    let cw = crate::kit::segs_w(&cancel);
    let bx = x + w - 2 - cw - 2 - width("[ Delete ]");
    p.fill(bx - 1, by, cw + 2, 1, "interactive.selection");
    p.put(bx - 1, by, &[(t.g("focus"), t.fg("interactive.focus").patch(t.bg("interactive.selection")))]);
    let cs: Vec<Seg> = cancel.iter().map(|(s, st)| (*s, st.patch(t.bg("interactive.selection")))).collect();
    p.put(bx, by, &cs);
    p.put(bx + cw + 2, by, &[("[ ", t.fg("status.error").patch(rb)), ("Delete", bold(t.fg("status.error")).patch(rb)), (" ]", t.fg("status.error").patch(rb))]);
    let _ = sel_row;
    hints(p, x, y + h - 2, w, &[("←→", "choose"), ("enter", "confirm"), ("esc", "cancel")], "");
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d13-palette", caption: "Command palette over the scrimmed shell", w: 200, h: 50, modes: DLM, ascii: false, draw: palette },
        Shot { id: "d13-settings", caption: "Settings in manifest order; disabled rows muted, reason in the footer", w: 200, h: 50, modes: DL, ascii: false, draw: settings },
        Shot { id: "d13-model-picker", caption: "Model picker", w: 200, h: 50, modes: DL, ascii: false, draw: picker },
        Shot { id: "d13-confirm", caption: "Confirm: Cancel is the safe default", w: 200, h: 50, modes: DL, ascii: false, draw: confirm },
    ]
}
