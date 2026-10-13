//! The GPT Live call bar: `● Live · model · phase · mm:ss`, level meters, Muted badge.
//!
//! Drawn in the blank row above the composer controls while a call is open, so the
//! editor stays fully usable (typing during a call is ordinary steering). Python owns
//! the state (`nexus/ui/ratatui/live.py`); this module only formats it.
use crate::bridge::LiveBar;
use crate::render::Palette;
use ratatui::{
    style::{Modifier, Style},
    text::Span,
};
use unicode_width::UnicodeWidthStr;

const METER_CELLS: usize = 6;

/// `mm:ss`, or `h:mm:ss` past an hour.
pub fn clock(seconds: u64) -> String {
    if seconds >= 3600 {
        format!("{}:{:02}:{:02}", seconds / 3600, seconds / 60 % 60, seconds % 60)
    } else {
        format!("{:02}:{:02}", seconds / 60, seconds % 60)
    }
}

/// Seconds since the call began; 0 while it is still connecting.
pub fn elapsed(live: &LiveBar, now_epoch: f64) -> u64 {
    if live.started <= 0.0 {
        0
    } else {
        (now_epoch - live.started).max(0.0) as u64
    }
}

/// A level (0..1, RMS) as a short bar. RMS of speech is small, so the scale is a square root.
pub fn meter(level: f64) -> String {
    let filled = ((level.clamp(0.0, 1.0).sqrt() * METER_CELLS as f64).round() as usize).min(METER_CELLS);
    format!("{}{}", "▰".repeat(filled), "▱".repeat(METER_CELLS - filled))
}

pub fn now_epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// The bar's spans, trimmed to `width` columns (the Muted badge is kept last).
pub fn spans(live: &LiveBar, p: &Palette, spin: usize, now: f64, width: usize) -> Vec<Span<'static>> {
    let dot = if live.state == "starting" || live.state == "stopping" || spin % 8 < 4 {
        p.error
    } else {
        p.quiet
    };
    let mut head = String::from("Live");
    if !live.model.is_empty() {
        head.push_str(&format!(" · {}", live.model));
    }
    if !live.phase.is_empty() {
        head.push_str(&format!(" · {}", live.phase));
    }
    head.push_str(&format!(" · {}", clock(elapsed(live, now))));
    if !live.elsewhere.is_empty() {
        head.push_str(&format!(" · in {}", live.elsewhere));
    }
    let meters = format!("   mic {} spk {}", meter(live.level_in), meter(live.level_out));
    let hint = "   /live stop";
    let badge = if live.muted { " MUTED " } else { "" };
    // Drop the optional pieces from the right until everything fits.
    let fixed = 2 + badge.width() + usize::from(live.muted);
    let mut optional = vec![meters.clone(), hint.to_string()];
    while !optional.is_empty() && fixed + head.width() + optional.iter().map(|s| s.width()).sum::<usize>() > width {
        optional.pop();
    }
    let room = width.saturating_sub(fixed + optional.iter().map(|s| s.width()).sum::<usize>());
    if head.width() > room {
        let mut cut = String::new();
        for ch in head.chars() {
            if cut.width() + 1 >= room {
                break;
            }
            cut.push(ch);
        }
        head = format!("{cut}…");
    }
    let mut out = vec![
        Span::styled("● ", Style::default().fg(dot)),
        Span::styled(head, Style::default().fg(p.text).add_modifier(Modifier::BOLD)),
    ];
    for (i, text) in optional.into_iter().enumerate() {
        out.push(Span::styled(text, Style::default().fg(if i == 0 { p.muted } else { p.quiet })));
    }
    if live.muted {
        out.push(Span::raw(" "));
        out.push(Span::styled(
            badge.to_string(),
            Style::default().fg(p.background).bg(p.warning).add_modifier(Modifier::BOLD),
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar() -> LiveBar {
        LiveBar {
            active: true,
            state: "active".into(),
            phase: "listening".into(),
            model: "gpt-live-1-codex".into(),
            started: 1000.0,
            level_in: 0.25,
            level_out: 0.0,
            ..Default::default()
        }
    }
    fn text(spans: &[Span]) -> String {
        spans.iter().map(|s| s.content.as_ref()).collect()
    }

    #[test]
    fn clock_formats_minutes_and_hours() {
        assert_eq!(clock(0), "00:00");
        assert_eq!(clock(161), "02:41");
        assert_eq!(clock(3725), "1:02:05");
    }
    #[test]
    fn elapsed_counts_from_the_start_and_is_zero_while_connecting() {
        let mut live = bar();
        assert_eq!(elapsed(&live, 1161.0), 161);
        live.started = 0.0;
        assert_eq!(elapsed(&live, 1161.0), 0);
        live.started = 2000.0; // a clock step backwards never underflows
        assert_eq!(elapsed(&live, 1161.0), 0);
    }
    #[test]
    fn meter_is_bounded_and_monotonic() {
        assert_eq!(meter(0.0), "▱▱▱▱▱▱");
        assert_eq!(meter(1.0), "▰▰▰▰▰▰");
        assert_eq!(meter(9.0).chars().count(), METER_CELLS);
        assert!(meter(0.25).matches('▰').count() > meter(0.01).matches('▰').count());
    }
    #[test]
    fn full_bar_names_model_phase_clock_and_meters() {
        let p = Palette::new(false);
        let line = text(&spans(&bar(), &p, 0, 1161.0, 100));
        assert!(line.starts_with("● Live · gpt-live-1-codex · listening · 02:41"), "{line}");
        assert!(line.contains("mic ▰") && line.contains("spk ▱▱▱▱▱▱") && line.contains("/live stop"));
        assert!(!line.contains("MUTED"));
    }
    #[test]
    fn muted_badge_survives_a_narrow_width() {
        let p = Palette::new(false);
        let mut live = bar();
        live.muted = true;
        live.phase = "muted".into();
        for width in [100, 60, 40, 24] {
            let line = text(&spans(&live, &p, 0, 1161.0, width));
            assert!(line.width() <= width, "{width}: {line:?} is {}", line.width());
            assert!(line.ends_with(" MUTED "), "{width}: {line:?}");
        }
    }
    #[test]
    fn other_session_is_named() {
        let p = Palette::new(false);
        let mut live = bar();
        live.elsewhere = "work".into();
        assert!(text(&spans(&live, &p, 0, 1161.0, 120)).contains("· in work"));
    }
}
