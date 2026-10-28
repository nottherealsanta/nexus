//! D15 — alerts: toasts top-right, a banner, jump to latest above the composer.
use super::{Shot, DL};
use crate::kit::{bold, P};
use crate::shell::{backdrop, Shell};
use ratatui::style::Style;

/// One toast: raised, level bar, glyph, bold title, `×`; message row; shrinking lifetime hairline.
fn toast(p: &mut P, x: u16, y: u16, w: u16, level: &str, glyph: &str, title: &str, msg: &str, life: f32) {
    let t = p.t;
    let rb = t.bg("surface.raised");
    let lc = t.fg(level);
    p.fill(x, y, w, 3, "surface.raised");
    p.vline(x, y, 3, t.g("focus"), lc.patch(rb));
    p.put(x + 2, y, &[(glyph, lc.patch(rb)), (" ", rb), (title, bold(t.primary()).patch(rb))]);
    p.put_r(x + w - 2, y, &[(t.g("close"), t.muted().patch(rb))]);
    p.put(x + 4, y + 1, &[(msg, t.secondary().patch(rb))]);
    let n = ((w - 3) as f32 * life) as u16;
    p.hline(x + 2, y + 2, n, "─", lc.patch(rb));
    p.hline(x + 2 + n, y + 2, w - 3 - n, "─", t.hair().patch(rb));
}

fn alerts(p: &mut P) {
    let t = p.t;
    let s = Shell { left_w: 34, right_w: 36, ..Shell::default() };
    let c = backdrop(p, &s);
    // Banner: one row at the top of the page, on the panel surface.
    let pb = t.bg("surface.panel");
    let wn = t.fg("status.warning");
    p.fill(c.bx, 1, c.bw, 1, "surface.panel");
    p.put(c.x, 1, &[("▲", wn.patch(pb)), (" Sign-in for codex expires in 2 days", bold(wn).patch(pb)), (" · ctrl+p, then Sign in to a provider", t.muted().patch(pb))]);
    p.put_r(c.x + c.w, 1, &[("r", bold(t.secondary()).patch(pb)), (" retry", t.muted().patch(pb)), (" · ", t.muted().patch(pb)), ("×", t.secondary().patch(pb)), (" hide", t.muted().patch(pb))]);
    // Toasts: newest at the bottom, at most three, then `+N more`.
    let w = 46;
    let x = c.bx + c.bw - 2 - w;
    toast(p, x, 3, w, "status.error", "✕", "Turn failed", "429 rate limited · retrying in 8s", 0.7);
    toast(p, x, 7, w, "status.success", "✓", "Copied session id", "ses_1a1c9f40e5a19", 0.35);
    toast(p, x, 11, w, "status.info", "●", "Core updated", "Nexus Core 0.10.3 is running", 0.85);
    p.put_r(x + w, 14, &[(" +2 more ", t.muted().patch(t.bg("surface.raised")))]);
    // Jump to latest: a raised pill above the composer card, right-aligned, clear of content.
    let y = c.bottom;
    let st = bold(t.secondary()).patch(t.bg("surface.raised"));
    let end = c.x + c.w;
    p.put_r(end, y, &[(" ", st), ("↓", st), (" 3 new", st), (" · ", t.muted().patch(t.bg("surface.raised"))), ("end", bold(t.secondary()).patch(t.bg("surface.raised"))), (" ", st)]);
    let _ = Style::default();
}

pub fn shots() -> Vec<Shot> {
    vec![Shot { id: "d15-alerts", caption: "Toasts, a banner and the jump-to-latest pill", w: 200, h: 50, modes: DL, ascii: false, draw: alerts }]
}
