//! The GPT Live call line: `● Live · phase · mm:ss   mic ▰▰▱ spk ▱▱   /live stop`, Muted badge.
//!
//! While a call is open it is the header row of the Live pane above the composer, with
//! `↑ N more` and the expand button on its right. Under the header, `PANE_ROWS - 1`
//! scrollable rows of the call (`pane_rows`, `pane_window`); expanded, the pane grows to
//! just below the top bar. With no rows to show (still connecting, or the call is on another
//! session so the mic is open but invisible here) the pane is the header alone, one row, with
//! `· in <session>` appended. The composer carries nothing of the call, so the editor stays
//! fully usable (typing during a call is ordinary steering). Python owns the state
//! (`nexus/ui/ratatui/live.py`); this module only formats it.
use crate::bridge::LiveBar;
use crate::render::Palette;
use ratatui::{
    style::{Modifier, Style},
    text::Span,
};
use unicode_width::UnicodeWidthStr;

const METER_CELLS: usize = 6;

/// Height of the Live pane above the composer: one header row and five rows of the call.
pub const PANE_ROWS: u16 = 6;

/// Rows reserved for the pane: only while a call is open, on a conversation page (not
/// Sessions or a sub agent page). `PANE_ROWS` with rows to show, else the header alone (1).
pub fn pane_rows(s: &crate::bridge::Snapshot) -> u16 {
    if !s.live.active || s.dashboard.is_some() || !s.agent_page.is_empty() {
        0
    } else if s.live.detail.is_empty() {
        1
    } else {
        PANE_ROWS
    }
}

/// `(back, start)` for `total` rows in a pane `height` tall scrolled `back` rows up from the
/// bottom: `back` clamped to what exists above, and the index of the first row shown.
pub fn pane_window(total: usize, height: usize, back: usize) -> (usize, usize) {
    let max_back = total.saturating_sub(height);
    let back = back.min(max_back);
    (back, max_back - back)
}

/// `mm:ss`, or `h:mm:ss` past an hour.
pub fn clock(seconds: u64) -> String {
    if seconds >= 3600 {
        format!(
            "{}:{:02}:{:02}",
            seconds / 3600,
            seconds / 60 % 60,
            seconds % 60
        )
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
    let filled =
        ((level.clamp(0.0, 1.0).sqrt() * METER_CELLS as f64).round() as usize).min(METER_CELLS);
    format!("{}{}", "▰".repeat(filled), "▱".repeat(METER_CELLS - filled))
}

pub fn now_epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// The call line's spans, trimmed to `width` columns. Pieces drop in this order until it fits:
/// the `/live stop` hint, then the model name, then the meters, then the model alone is
/// shortened; the clock, phase and Muted badge stay.
pub fn spans(
    live: &LiveBar,
    p: &Palette,
    spin: usize,
    now: f64,
    width: usize,
) -> Vec<Span<'static>> {
    let dot = if live.state == "starting" || live.state == "stopping" || spin % 8 < 4 {
        p.error
    } else {
        p.quiet
    };
    let head = |with_model: bool| {
        let mut head = String::from("Live");
        if with_model && !live.model.is_empty() {
            head.push_str(&format!(" · {}", live.model));
        }
        if !live.phase.is_empty() {
            head.push_str(&format!(" · {}", live.phase));
        }
        head.push_str(&format!(" · {}", clock(elapsed(live, now))));
        if !live.elsewhere.is_empty() {
            head.push_str(&format!(" · in {}", live.elsewhere));
        }
        head
    };
    let meters = format!(
        "   mic {} spk {}",
        meter(live.level_in),
        meter(live.level_out)
    );
    let hint = "   /live stop";
    let badge = if live.muted { " MUTED " } else { "" };
    let fixed = 2 + badge.width() + usize::from(live.muted);
    // (with model, with meters, with hint), most informative first.
    let variants = [
        (true, true, true),
        (true, true, false),
        (false, true, false),
        (true, false, false),
        (false, false, false),
    ];
    let (with_model, with_meters, with_hint) = variants
        .into_iter()
        .find(|&(m, l, h)| {
            fixed
                + head(m).width()
                + if l { meters.width() } else { 0 }
                + if h { hint.width() } else { 0 }
                <= width
        })
        .unwrap_or((false, false, false));
    let mut head = head(with_model);
    let room = width.saturating_sub(fixed);
    if head.width() > room {
        let mut cut = String::new();
        for ch in head.chars() {
            if cut.width() + 2 > room {
                break;
            }
            cut.push(ch);
        }
        head = format!("{cut}…");
    }
    let mut out = vec![
        Span::styled("● ", Style::default().fg(dot)),
        Span::styled(
            head,
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        ),
    ];
    if with_meters {
        out.push(Span::styled(meters, Style::default().fg(p.muted)));
    }
    if with_hint {
        out.push(Span::styled(hint.to_string(), Style::default().fg(p.quiet)));
    }
    if live.muted {
        out.push(Span::raw(" "));
        out.push(Span::styled(
            badge.to_string(),
            Style::default()
                .fg(p.background)
                .bg(p.warning)
                .add_modifier(Modifier::BOLD),
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
    fn pane_rows_only_for_an_open_call_with_rows() {
        let mut s = crate::bridge::Snapshot::default();
        assert_eq!(pane_rows(&s), 0);
        s.live = bar();
        assert_eq!(pane_rows(&s), 1, "no rows yet: the header alone");
        s.live.detail = "you\tYou\thi".into();
        assert_eq!(pane_rows(&s), PANE_ROWS);
        s.live.active = false;
        assert_eq!(pane_rows(&s), 0);
        s.live.active = true;
        s.agent_page = "agent".into();
        assert_eq!(pane_rows(&s), 0);
    }
    #[test]
    fn pane_window_follows_bottom_and_clamps() {
        assert_eq!(pane_window(12, 5, 0), (0, 7));
        assert_eq!(pane_window(12, 5, 3), (3, 4));
        assert_eq!(pane_window(12, 5, 99), (7, 0));
        assert_eq!(pane_window(3, 5, 2), (0, 0));
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
    fn header_line_has_the_call_state_and_fits_every_width() {
        let p = Palette::new(false);
        let mut live = bar();
        live.model.clear();
        for width in [90, 60, 40, 30, 20, 12] {
            let line = text(&spans(&live, &p, 0, 1161.0, width));
            assert!(line.width() <= width, "{width}: {line:?}");
            assert!(line.starts_with("● Live"), "{width}: {line:?}");
        }
        let wide = text(&spans(&live, &p, 0, 1161.0, 90));
        assert!(wide.contains("listening · 02:41") && wide.contains("/live stop"), "{wide}");
        let narrow = text(&spans(&live, &p, 0, 1161.0, 30));
        assert!(narrow.contains("02:41") && !narrow.contains("/live stop"), "{narrow}");
    }
    #[test]
    fn full_bar_names_model_phase_clock_and_meters() {
        let p = Palette::new(false);
        let line = text(&spans(&bar(), &p, 0, 1161.0, 100));
        assert!(
            line.starts_with("● Live · gpt-live-1-codex · listening · 02:41"),
            "{line}"
        );
        assert!(
            line.contains("mic ▰") && line.contains("spk ▱▱▱▱▱▱") && line.contains("/live stop")
        );
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
            assert!(
                line.width() <= width,
                "{width}: {line:?} is {}",
                line.width()
            );
            assert!(line.ends_with(" MUTED "), "{width}: {line:?}");
        }
    }
    #[test]
    fn narrow_bars_keep_the_meters_before_the_model_name() {
        let p = Palette::new(false);
        let line = text(&spans(&bar(), &p, 0, 1161.0, 52));
        assert!(
            line.width() <= 52 && line.contains("mic ▰") && line.contains("02:41"),
            "{line}"
        );
        assert!(!line.contains("gpt-live-1-codex"), "{line}");
        let line = text(&spans(&bar(), &p, 0, 1161.0, 100));
        assert!(
            line.contains("gpt-live-1-codex") && line.contains("mic"),
            "{line}"
        );
    }
    #[test]
    fn other_session_is_named() {
        let p = Palette::new(false);
        let mut live = bar();
        live.elsewhere = "work".into();
        assert!(text(&spans(&live, &p, 0, 1161.0, 120)).contains("· in work"));
    }
}
