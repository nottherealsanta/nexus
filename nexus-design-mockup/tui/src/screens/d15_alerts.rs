//! D15 — alerts: toasts top-right, a banner, jump to latest above the composer.
use super::{Shot, DL};
use crate::kit::{bold, P};
use crate::shell::{backdrop, Shell};
use ratatui::style::Style;

/// One toast: raised, level bar, glyph, bold title, `×`; message row. No lifetime hairline.
fn toast(p: &mut P, x: u16, y: u16, w: u16, level: &str, glyph: &str, title: &str, msg: &str, life: f32) {
    let t = p.t;
    let rb = t.bg("surface.raised");
    let lc = t.fg(level);
    let _ = life;
    p.fill(x, y, w, 4, "surface.raised");
    p.vline(x, y, 4, t.g("focus"), lc.patch(rb));
    p.put(x + 2, y + 1, &[(glyph, lc.patch(rb)), (" ", rb), (title, bold(t.primary()).patch(rb))]);
    p.put_r(x + w - 2, y + 1, &[(t.g("close"), t.muted().patch(rb))]);
    p.put(x + 4, y + 2, &[(msg, t.secondary().patch(rb))]);
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
    toast(p, x, 8, w, "status.success", "✓", "Copied session id", "ses_1a1c9f40e5a19", 0.35);
    toast(p, x, 13, w, "status.info", "●", "Core updated", "Nexus Core 0.10.3 is running", 0.85);
    p.put_r(x + w, 17, &[(" +2 more ", t.muted().patch(t.bg("surface.raised")))]);
    // Jump to latest: a raised pill above the composer card, right-aligned, clear of content.
    let y = c.bottom;
    let st = bold(t.secondary()).patch(t.bg("surface.raised"));
    let end = c.x + c.w;
    p.put_r(end, y, &[(" ", st), ("↓", st), (" 3 new", st), (" · ", t.muted().patch(t.bg("surface.raised"))), ("end", bold(t.secondary()).patch(t.bg("surface.raised"))), (" ", st)]);
    let _ = Style::default();
}

/// The full text of a toast, opened with enter or a click: a borderless modal titled by the toast.
fn detail(p: &mut P) {
    super::d13_overlays::under(p);
    let t = p.t;
    let (w, h) = (86, 24);
    let (x, y) = ((p.w - w) / 2, 8);
    crate::shell::modal(p, x, y, w, h, "Turn failed", true);
    let rb = t.bg("surface.raised");
    let mut r = y + 3;
    let paras: [(&str, bool); 6] = [
        ("provider: 429 rate limited. The request to codex/gpt-6.1-terra was rejected because the organization exceeded its tokens-per-minute quota for this model. Nexus will retry automatically in 8s (attempt 2 of 5).", false),
        ("The provider returned this body:", false),
        ("{\"error\": {\"type\": \"rate_limit_exceeded\", \"code\": \"rate_limit_exceeded\", \"message\": \"Rate limit reached for gpt-6.1-terra in organization org-7Qd2xK on tokens per min (TPM): Limit 800000, Used 784210, Requested 31804. Please try again in 1.5s.\", \"param\": null}}", true),
        ("Request id req_9f2c4a71b0e84d5c. Retry-After: 8. The turn is paused, not lost: the 3 tool results already received are kept in the context.", false),
        ("If this keeps happening, switch to a model with more headroom (ctrl+m) or wait for the window to reset. Rate limits are per organization, so other sessions using the same sign-in count against the same quota.", false),
        ("", false),
    ];
    for (s, mono) in paras {
        if s.is_empty() {
            continue;
        }
        let st = if mono { t.secondary() } else { t.primary() };
        r += chunk(p, x + 2, r, w - 4, s, st.patch(rb), mono) + 1;
    }
    crate::shell::hints(p, x, y + h - 2, w, &[("c", "copy"), ("esc", "close")], "");
    let _ = (bold, r);
}

/// Wrap `text` to `w` cells; `hard` breaks at the width (for JSON without spaces).
fn chunk(p: &mut P, x: u16, y: u16, w: u16, text: &str, st: Style, hard: bool) -> u16 {
    if !hard {
        return p.wrap(x, y, w, text, st);
    }
    let chars: Vec<char> = text.chars().collect();
    let mut n = 0;
    for (i, part) in chars.chunks(w as usize).enumerate() {
        let s: String = part.iter().collect();
        p.put(x, y + i as u16, &[(&s, st)]);
        n += 1;
    }
    n
}

pub fn shots() -> Vec<Shot> {
    vec![
        Shot { id: "d15-alerts", caption: "Toasts, a banner and the jump-to-latest pill", w: 200, h: 50, modes: DL, ascii: false, draw: alerts },
        Shot { id: "d15-toast-detail", caption: "Enter or click on a toast opens its full text in a borderless modal", w: 200, h: 50, modes: DL, ascii: false, draw: detail },
    ]
}
