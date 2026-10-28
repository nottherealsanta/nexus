//! D01 — tokens and surfaces: the swatch sheet, plus small helpers shared by the D04–D10 modules.
use super::{Shot, DL};
use crate::kit::{bold, P};
use ratatui::style::{Color, Style};

/// `#rrggbb` of a token.
pub fn hex(p: &P, tok: &str) -> String {
    match p.t.color(tok) {
        Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
        _ => String::new(),
    }
}

/// Word-wrap `text`; the first line is `first` cells wide, the rest `w`.
pub fn wrap_lines(text: &str, first: u16, w: u16) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut line = String::new();
    for word in text.split(' ') {
        let cap = if out.is_empty() { first } else { w };
        let cand = if line.is_empty() { word.to_string() } else { format!("{line} {word}") };
        if crate::kit::width(&cand) > cap && !line.is_empty() {
            out.push(std::mem::take(&mut line));
            line = word.to_string();
        } else {
            line = cand;
        }
    }
    if !line.is_empty() {
        out.push(line);
    }
    out
}

/// Small muted caption for component sheets.
pub fn caption(p: &mut P, x: u16, y: u16, s: &str) {
    let st = p.t.muted();
    p.put(x, y, &[(s, st)]);
}

fn label(p: &mut P, x: u16, y: u16, s: &str) {
    let st = bold(p.t.muted());
    p.put(x, y, &[(s, st)]);
}

/// A colour chip, the token name and its hex.
fn chip(p: &mut P, x: u16, y: u16, tok: &str) {
    let t = p.t;
    let c = t.color(tok);
    p.put(x, y, &[("        ", Style::default().bg(c))]);
    let h = hex(p, tok);
    p.put(x + 10, y, &[(tok, t.primary()), ("  ", t.muted()), (&h, t.muted())]);
}

fn surface(p: &mut P, x: u16, y: u16, w: u16, tok: &str, role: &str) {
    let t = p.t;
    p.fill(x, y, w, 4, tok);
    let b = t.bg(tok);
    let h = hex(p, tok);
    p.put(x + 2, y + 1, &[(tok, bold(t.primary()).patch(b)), ("  ", b), (&h, t.muted().patch(b))]);
    p.put(x + 2, y + 2, &[(role, t.secondary().patch(b))]);
}

fn sheet(p: &mut P) {
    let t = p.t;
    let (a, b, c) = (3u16, 56u16, 109u16);
    let cw = 46u16;
    p.put(a, 1, &[("Tokens and surfaces", bold(t.primary())), ("  ·  D01  ·  chrome frames the page; accent is rationed", t.muted())]);

    // -- column A: surfaces, text
    label(p, a, 3, "SURFACES");
    surface(p, a, 5, cw, "surface.sidebar", "chrome · header band and both sidebars");
    surface(p, a, 10, cw, "surface.canvas", "page · the timeline");
    surface(p, a, 15, cw, "surface.panel", "panel · composer, fences, notices");
    surface(p, a, 20, cw, "surface.raised", "raised · user band, modals, toasts");
    p.put(a, 25, &[("surface.scrim", t.primary()), ("  50% blend of fg and bg toward canvas", t.muted())]);
    label(p, a, 28, "TEXT");
    let rows = [("text.primary", t.primary(), "titles, user and agent text"), ("text.secondary", t.secondary(), "arguments, summaries, labels"), ("text.muted", t.muted(), "metadata, counts, key hints")];
    for (i, (tok, st, note)) in rows.iter().enumerate() {
        let y = 30 + i as u16 * 3;
        let h = hex(p, tok);
        p.put(a, y, &[(tok, *st), ("  ", t.muted()), (&h, t.muted())]);
        p.put(a, y + 1, &[(note, *st)]);
    }
    label(p, a, 40, "SECTION LABEL");
    p.put(a, 42, &[("SESSION", bold(t.muted())), ("   uppercase, bold, muted", t.muted())]);

    // -- column B: accent, focus, selection, borders
    label(p, b, 3, "ACCENT AND FOCUS");
    chip(p, b, 5, "interactive.accent");
    chip(p, b, 7, "interactive.focus");
    chip(p, b, 9, "interactive.selection");
    chip(p, b, 11, "interactive.hover");
    p.put(b, 13, &[("Accent: the focus bar, the active tab mark,", t.muted())]);
    p.put(b, 14, &[("the waiting mark, links, the primary action.", t.muted())]);
    let sb = t.bg("interactive.selection");
    p.fill(b, 16, cw, 2, "interactive.selection");
    p.put(b, 16, &[(t.g("focus"), t.fg("interactive.focus")), (" ", t.muted()), ("Refactor the loader", bold(t.primary()).patch(sb))]);
    p.put(b + 2, 17, &[("build", t.agent("build").patch(sb)), (" · ⌥ main", t.muted().patch(sb))]);
    p.put_r(b + cw - 2, 16, &[("now", t.muted().patch(sb))]);
    label(p, b, 20, "BORDERS");
    chip(p, b, 22, "border.default");
    p.put(b, 23, &[("hairlines, tree guides, meter track", t.muted())]);
    p.hline(b, 24, cw, "─", t.hair());
    chip(p, b, 26, "border.strong");
    p.put(b + 42, 26, &[("proposed", bold(t.fg("status.warning")))]);
    p.put(b, 27, &[("modal frames, diff split rule, open tool corner", t.muted())]);
    p.hline(b, 28, cw, "─", t.fg("border.strong"));
    chip(p, b, 30, "border.focus");
    label(p, b, 33, "MODAL FRAME");
    p.fill(b, 35, 34, 5, "surface.raised");
    p.frame(b, 35, 34, 5, t.fg("border.strong"), true, &[("Commands", bold(t.primary()).patch(t.bg("surface.raised")))]);
    p.put(b + 2, 37, &[("⌕ ", t.muted().patch(t.bg("surface.raised"))), ("Search…", t.muted().patch(t.bg("surface.raised")))]);

    // -- column C: status, agents, diff
    label(p, c, 3, "STATUS");
    let st: [(&str, &str, &str); 7] = [("status.success", "✓", "done"), ("status.warning", "◼", "interrupted"), ("status.error", "✕", "error"), ("status.info", "●", "unread"), ("interactive.accent", "◆", "waiting"), ("text.primary", "◐", "running"), ("status.error", "⊘", "denied")];
    for (i, (tok, g, word)) in st.iter().enumerate() {
        let y = 5 + i as u16;
        let h = hex(p, tok);
        p.put(c, y, &[(g, t.fg(tok)), (" ", t.muted()), (word, t.fg(tok))]);
        p.put(c + 16, y, &[(tok, t.muted()), ("  ", t.muted()), (&h, t.muted())]);
    }
    label(p, c, 14, "AGENTS");
    let ag = [("orchestrate", "amber"), ("build", "blue"), ("quick", "green"), ("scout", "teal"), ("plan", "violet"), ("custom a", "rose"), ("custom b", "sky"), ("custom c", "gold")];
    for (i, (n, slot)) in ag.iter().enumerate() {
        let y = 16 + i as u16;
        let tok = format!("agent.{slot}");
        let h = hex(p, &tok);
        let s = t.fg(&tok);
        p.put(c, y, &[("▪ ", s), (n, bold(s))]);
        p.put(c + 16, y, &[(&tok, t.muted()), ("  ", t.muted()), (&h, t.muted())]);
    }
    label(p, c, 26, "DIFF");
    let (ab, db) = (t.bg("diff.add_bg"), t.bg("diff.del_bg"));
    p.fill(c, 28, cw, 1, "diff.del_bg");
    p.fill(c, 29, cw, 1, "diff.add_bg");
    p.put(c + 1, 28, &[("-", t.fg("diff.del").patch(db)), ("   return parse_manifest(path)", t.primary().patch(db))]);
    p.put(c + 1, 29, &[("+", t.fg("diff.add").patch(ab)), ("   return self._cached(path)", t.primary().patch(ab))]);
    let (h1, h2, h3, h4) = (hex(p, "diff.add"), hex(p, "diff.del"), hex(p, "diff.add_bg"), hex(p, "diff.del_bg"));
    p.put(c, 31, &[("diff.add ", t.fg("diff.add")), (&h1, t.muted()), ("   diff.add_bg ", t.muted()), (&h3, t.muted())]);
    p.put(c, 32, &[("diff.del ", t.fg("diff.del")), (&h2, t.muted()), ("   diff.del_bg ", t.muted()), (&h4, t.muted())]);
    p.put(c, 33, &[("Tinted text is primary; context is muted.", t.muted())]);
    label(p, c, 36, "NEW IN D01");
    p.put(c, 38, &[("border.strong", t.primary()), (": 256 238/250 · 16 bright_black/white", t.muted())]);
    p.put(c, 39, &[("surface.scrim", t.primary()), (": no colour, a blend", t.muted())]);
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d01-tokens", caption: "Tokens and surfaces: chrome, page, panel, raised, text, accent, status, agents, diff", w: 160, h: 46, modes: DL, ascii: false, draw: sheet }]
}
