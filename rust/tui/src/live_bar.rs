//! The GPT Live call line (P-LIV): `LISTENING   ▁▃▅█▆▃▂▁  gpt-live-1-codex  /live stop   02:41`.
//!
//! While a call is open it is the header row of the Live pane above the composer, with
//! `↑ N more` and the expand button on its right. The phase is in caps and bold, padded to 12
//! cells and followed by that phase's animation (a connecting dot sweep, the newest mic levels,
//! an orbit spinner with the agent turn, a damped wave while the voice speaks); the clock is
//! right-aligned. Under the header, `PANE_ROWS - 1` scrollable rows of the call (`pane_rows`,
//! `pane_window`, drawn by `captions`); expanded, the pane grows to just below the top bar.
//! With no rows to show (still connecting, or the call is on another session so the mic is open
//! but invisible here) the pane is the header alone, one row, with `in <session>` appended. The
//! composer carries nothing of the call, so the editor stays fully usable (typing during a call
//! is ordinary steering). Python owns the state (`nexus/ui/ratatui/live.py`); this module only
//! formats it.
use crate::bridge::LiveBar;
use crate::render::Palette;
use crate::transcript::Rows;
use nexus_widgets::{anim::wave, theme::mix};
use ratatui::{
    style::{Color, Modifier, Style},
    text::Span,
};
use serde_json::Value;
use unicode_width::UnicodeWidthStr;

/// Cells of the phase column, the mic history and the speaking wave.
const PHASE_WIDTH: usize = 12;
const MIC_CELLS: usize = 8;
const LEVELS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
const ASCII_LEVELS: [char; 8] = ['_', '.', ':', '-', '=', '+', '*', '#'];
/// Connecting sweep, one step per two frames.
const CONNECT: [&str; 6] = ["●○○", "●●○", "●●●", "○●●", "○○●", "○○○"];
const CONNECT_ASCII: [&str; 6] = ["#--", "##-", "###", "-##", "--#", "---"];
const ORBIT: [char; 8] = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷'];
const ORBIT_ASCII: [char; 4] = ['|', '/', '-', '\\'];

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

pub fn now_epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// The newest microphone levels (0..1, RMS), one per frame while a call is open. The snapshot
/// only carries the current `level_in`, so the history the listening meter draws is kept here.
#[derive(Default)]
pub struct Mic {
    levels: Vec<f32>,
    tick: Option<usize>,
}

impl Mic {
    /// Record `level` once per frame `tick` (redraws inside a frame do not add samples); a
    /// closed call forgets its history.
    pub fn observe(&mut self, live: &LiveBar, tick: usize) {
        if !live.active {
            self.levels.clear();
            self.tick = None;
        } else if self.tick != Some(tick) {
            self.tick = Some(tick);
            self.levels.push(live.level_in.clamp(0.0, 1.0) as f32);
            if self.levels.len() > MIC_CELLS {
                self.levels.remove(0);
            }
        }
    }
    pub fn levels(&self) -> &[f32] {
        &self.levels
    }
}

/// A level as one of eight bars. RMS of speech is small, so the scale is a square root.
fn level_cell(level: f32, ascii: bool) -> char {
    let at = (level.clamp(0.0, 1.0).sqrt() * 7.0).round() as usize;
    if ascii {
        ASCII_LEVELS[at.min(7)]
    } else {
        LEVELS[at.min(7)]
    }
}

/// Turn number of the newest delegation in the call's rows (`Sent to agent … → turn #N`).
pub fn latest_turn(detail: &str) -> Option<u64> {
    detail
        .lines()
        .filter_map(|row| {
            let mut parts = row.splitn(3, '\t');
            if parts.next()? != "sent" {
                return None;
            }
            let text = parts.nth(1)?;
            let at = text.rfind("turn #")?;
            text[at + "turn #".len()..]
                .chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
                .parse()
                .ok()
        })
        .last()
}

/// The phase to show: paused and muted win over what the helper last reported.
fn phase_name(live: &LiveBar) -> String {
    if live.paused {
        "paused".into()
    } else if live.muted && live.phase != "ended" {
        "muted".into()
    } else if live.phase.is_empty() {
        let guess = if live.state == "starting" {
            "connecting"
        } else {
            "listening"
        };
        guess.into()
    } else {
        live.phase.clone()
    }
}

fn phase_tone(phase: &str, p: &Palette) -> Color {
    match phase {
        "listening" => p.blue,
        "working" => p.purple,
        "speaking" => p.accent,
        "muted" | "paused" | "ended" => p.quiet,
        _ => p.muted,
    }
}

/// The phase's animation. With animation off it is drawn in its final state; the mic history is
/// data and is always shown.
fn animation(
    live: &LiveBar,
    phase: &str,
    p: &Palette,
    tick: usize,
    animate: bool,
    mic: &[f32],
    ascii: bool,
) -> Vec<Span<'static>> {
    let on = |fg: Color| Style::default().fg(fg).bg(p.element);
    match phase {
        "connecting" => {
            let frames = if ascii { CONNECT_ASCII } else { CONNECT };
            let at = if animate { tick / 2 % frames.len() } else { 2 };
            vec![Span::styled(frames[at], on(p.muted))]
        }
        "listening" => {
            let recent = &mic[mic.len().saturating_sub(MIC_CELLS)..];
            let cells: String = std::iter::repeat(0.0)
                .take(MIC_CELLS - recent.len())
                .chain(recent.iter().copied())
                .map(|level| level_cell(level, ascii))
                .collect();
            vec![Span::styled(cells, on(p.blue))]
        }
        "working" => {
            let glyph = match (animate, ascii) {
                (false, true) => ORBIT_ASCII[0],
                (false, false) => ORBIT[0],
                (true, true) => ORBIT_ASCII[tick % ORBIT_ASCII.len()],
                (true, false) => ORBIT[tick % ORBIT.len()],
            };
            let label = match latest_turn(&live.detail) {
                Some(turn) => format!(" agent turn #{turn} running"),
                None => " agent running".to_string(),
            };
            vec![
                Span::styled(glyph.to_string(), on(p.purple)),
                Span::styled(label, on(p.quiet)),
            ]
        }
        "speaking" => {
            // The louder the voice, the deeper the wave; silence leaves a flat line.
            let depth = live.level_out.clamp(0.0, 1.0).sqrt() as f32;
            (0..MIC_CELLS)
                .map(|i| {
                    let amount = if animate {
                        wave(tick as u64, i as u16, MIC_CELLS as u16, 0.4) * depth
                    } else {
                        0.0
                    };
                    Span::styled(
                        if ascii { "~" } else { "∿" },
                        on(mix(p.accent, p.element, amount)),
                    )
                })
                .collect()
        }
        "paused" => vec![Span::styled(
            format!("ends in {}", clock(live.pause_left.max(0.0).ceil() as u64)),
            on(p.quiet),
        )],
        "muted" => vec![Span::styled("mic off", on(p.quiet))],
        _ => Vec::new(),
    }
}

/// The call line's spans: exactly `width` columns on `p.element`. Pieces drop in this order
/// until it fits: the `/live stop` hint, the model name, the session the call is in, the
/// phase's animation, the clock (the phase is shortened last). `tick` is the frame clock,
/// `mic` the newest input levels (`Mic::levels`).
pub fn spans(
    live: &LiveBar,
    p: &Palette,
    tick: usize,
    now: f64,
    width: usize,
    animate: bool,
    mic: &[f32],
) -> Vec<Span<'static>> {
    let ascii = crate::transcript::look::glyphs().ascii;
    let on = |fg: Color| Style::default().fg(fg).bg(p.element);
    let phase = phase_name(live);
    let caps = phase.to_uppercase();
    let time = clock(elapsed(live, now));
    // The clock keeps the right edge with one cell before it, unless there is no room at all.
    let show_clock = width >= time.width() + 3;
    let room = if show_clock {
        width - time.width() - 1
    } else {
        width
    };

    let mut out = Vec::new();
    let mut used = 0;
    if caps.width() > room {
        let cut = crate::transcript::truncate(&caps, room);
        used = cut.width();
        out.push(Span::styled(
            cut,
            on(phase_tone(&phase, p)).add_modifier(Modifier::BOLD),
        ));
    } else {
        let column = if room >= PHASE_WIDTH {
            PHASE_WIDTH
        } else {
            caps.width()
        };
        used = used.max(column);
        let padded = format!("{caps:<column$}");
        out.push(Span::styled(
            padded,
            on(phase_tone(&phase, p)).add_modifier(Modifier::BOLD),
        ));
        let motion = animation(live, &phase, p, tick, animate, mic, ascii);
        let motion_width: usize = motion.iter().map(|span| span.content.width()).sum();
        let gap = usize::from(column == caps.width());
        if motion_width > 0 && used + gap + motion_width <= room {
            if gap == 1 {
                out.push(Span::styled(" ", on(p.quiet)));
            }
            out.extend(motion);
            used += gap + motion_width;
        }
        let hint = if live.paused {
            "Ctrl+X P resume"
        } else {
            "/live stop"
        };
        let mut extras = Vec::new();
        if !live.attention.is_empty() {
            // Kept ahead of the session and model so a waiting approval is the last to drop.
            let text = format!("{} {}", if ascii { "!" } else { "▲" }, live.attention);
            if used + 2 + text.width() <= room {
                out.push(Span::styled(
                    format!("  {text}"),
                    on(p.warning).add_modifier(Modifier::BOLD),
                ));
                used += 2 + text.width();
            }
        }
        if !live.elsewhere.is_empty() {
            extras.push(format!("in {}", live.elsewhere));
        }
        if !live.model.is_empty() {
            extras.push(live.model.clone());
        }
        extras.push(hint.to_string());
        for extra in extras {
            if used + 2 + extra.width() <= room {
                out.push(Span::styled(format!("  {extra}"), on(p.quiet)));
                used += 2 + extra.width();
            }
        }
    }
    let gap = width.saturating_sub(used + if show_clock { time.width() } else { 0 });
    out.push(Span::styled(" ".repeat(gap), on(p.quiet)));
    if show_clock {
        out.push(Span::styled(time, on(p.muted)));
    }
    out
}

/// The rows of the call under the header (captions). The card's own rows (`you`, `voice`,
/// `caption`, `sent`, `told`, `note`, `bad`; `tone TAB label TAB text`) draw as in the
/// transcript (`transcript::live_rows`) except:
/// - a partial `You…` caption is italic `p.muted` (final text stays `p.text`);
/// - the `Voice…` caption in progress reads word by word: spoken words `p.text`, the newest
///   word `p.accent` bold underlined. The host sends no playback position (not verified), so
///   the newest word of the delta stands for the current one and upcoming words are not shown;
/// - `Sent to agent` becomes a chip, ` → sent to agent ` (bold `p.background` on `p.warning`)
///   then ` turn #N · steer ` (`p.text` on `p.element`), followed by the text. It appears in
///   place: nothing about it depends on a frame.
pub fn captions(detail: &str, rail: Color, width: usize, p: &Palette, op: &Option<Value>) -> Rows {
    let mut out = Rows::new();
    for row in detail.lines() {
        let mut parts = row.splitn(3, '\t');
        let tone = parts.next().unwrap_or("");
        let label = parts.next().unwrap_or("");
        let text = parts.next().unwrap_or("");
        match (tone, label) {
            ("sent", label) if !label.is_empty() => {
                out.extend(sent_rows(label, text, rail, width, p, op))
            }
            ("caption", "Voice…") => out.extend(voice_rows(label, text, rail, width, p, op)),
            _ => {
                // A trailing newline keeps an empty detail line as one blank row.
                let mut rows =
                    crate::transcript::live_rows(&format!("{row}\n"), rail, 0, width, p, op);
                if tone == "caption" {
                    for span in rows.iter_mut().flat_map(|(line, _)| line.spans.iter_mut()) {
                        if span.style.add_modifier.contains(Modifier::ITALIC)
                            && span.style.fg == Some(p.quiet)
                        {
                            span.style.fg = Some(p.muted);
                        }
                    }
                }
                out.extend(rows);
            }
        }
    }
    out
}

/// `Sent to agent (steer)` + `text  → turn #3` as a chip row.
fn sent_rows(
    label: &str,
    text: &str,
    rail: Color,
    width: usize,
    p: &Palette,
    op: &Option<Value>,
) -> Rows {
    let ascii = crate::transcript::look::glyphs().ascii;
    let mode = label
        .split_once('(')
        .and_then(|(_, rest)| rest.strip_suffix(')'))
        .unwrap_or("");
    let marker = "→ turn #";
    let (body, turn) = match text.rfind(marker) {
        Some(at) => (text[..at].trim_end(), text[at + marker.len()..].trim()),
        None => (text, ""),
    };
    let arrow = if ascii { "->" } else { "→" };
    let sent = Span::styled(
        format!(" {arrow} sent to agent "),
        Style::default()
            .fg(p.background)
            .bg(p.warning)
            .add_modifier(Modifier::BOLD),
    );
    let detail = match (turn.is_empty(), mode.is_empty()) {
        (false, false) => format!(" turn #{turn} · {mode} "),
        (false, true) => format!(" turn #{turn} "),
        (true, false) => format!(" {mode} "),
        (true, true) => String::new(),
    };
    let mut chip = vec![sent];
    if !detail.is_empty() {
        chip.push(Span::styled(
            detail,
            Style::default().fg(p.text).bg(p.element),
        ));
    }
    let chip_width: usize = chip.iter().map(|span| span.content.width()).sum();
    let room = width.saturating_sub(2 + chip_width + 1).max(8);
    let body = [Span::styled(body.to_string(), Style::default().fg(p.text))];
    crate::transcript::wrap(&body, room)
        .into_iter()
        .enumerate()
        .map(|(i, cells)| {
            let mut prefix = vec![Span::styled("│ ", Style::default().fg(rail))];
            if i == 0 {
                prefix.extend(chip.clone());
                prefix.push(Span::raw(" "));
            } else {
                prefix.push(Span::raw(" ".repeat(chip_width + 1)));
            }
            (
                crate::transcript::line(prefix, cells, None, Style::default()),
                op.clone(),
            )
        })
        .collect()
}

/// The `Voice…` caption in progress, word by word.
fn voice_rows(
    label: &str,
    text: &str,
    rail: Color,
    width: usize,
    p: &Palette,
    op: &Option<Value>,
) -> Rows {
    let trimmed = text.trim_end();
    let split = trimmed.rfind(char::is_whitespace).map_or(0, |at| {
        at + trimmed[at..].chars().next().map_or(1, char::len_utf8)
    });
    let body = Style::default().fg(p.text);
    let words = [
        Span::styled(trimmed[..split].to_string(), body),
        Span::styled(
            trimmed[split..].to_string(),
            Style::default()
                .fg(p.accent)
                .add_modifier(Modifier::BOLD | Modifier::UNDERLINED),
        ),
        Span::styled(text[trimmed.len()..].to_string(), body),
    ];
    let label_width = label.width() + 2;
    let room = width.saturating_sub(2 + label_width).max(8);
    crate::transcript::wrap(&words, room)
        .into_iter()
        .enumerate()
        .map(|(i, cells)| {
            let mut prefix = vec![Span::styled("│ ", Style::default().fg(rail))];
            if i == 0 {
                prefix.push(Span::styled(
                    label.to_string(),
                    Style::default().fg(p.blue).add_modifier(Modifier::BOLD),
                ));
                prefix.push(Span::raw("  "));
            } else {
                prefix.push(Span::raw(" ".repeat(label_width)));
            }
            (
                crate::transcript::line(prefix, cells, None, Style::default()),
                op.clone(),
            )
        })
        .collect()
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
    fn header(
        live: &LiveBar,
        tick: usize,
        width: usize,
        animate: bool,
        mic: &[f32],
    ) -> Vec<Span<'static>> {
        spans(
            live,
            &Palette::new(false),
            tick,
            1161.0,
            width,
            animate,
            mic,
        )
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
    fn every_phase_has_a_bold_caps_label_padded_to_twelve_and_the_clock_at_the_right() {
        let p = Palette::new(false);
        for (phase, tone) in [
            ("connecting", p.muted),
            ("listening", p.blue),
            ("working", p.purple),
            ("speaking", p.accent),
            ("muted", p.quiet),
            ("paused", p.quiet),
            ("ended", p.quiet),
        ] {
            let mut live = bar();
            live.phase = phase.into();
            live.model.clear();
            let out = header(&live, 0, 60, true, &[]);
            let line = text(&out);
            assert_eq!(line.width(), 60, "{phase}: {line:?}");
            assert!(
                line.starts_with(&format!("{:<12}", phase.to_uppercase())),
                "{line:?}"
            );
            assert!(line.ends_with(" 02:41"), "{line:?}");
            assert_eq!(out[0].style.fg, Some(tone), "{phase}");
            assert!(out[0].style.add_modifier.contains(Modifier::BOLD));
            assert_eq!(out[0].style.bg, Some(p.element));
            assert_eq!(
                out.last().unwrap().style.fg,
                Some(p.muted),
                "clock is muted"
            );
        }
    }

    #[test]
    fn header_never_exceeds_the_width_and_keeps_the_phase() {
        let mut live = bar();
        live.elsewhere = "work".into();
        for phase in [
            "connecting",
            "listening",
            "working",
            "speaking",
            "muted",
            "paused",
            "ended",
        ] {
            live.phase = phase.into();
            for width in 0..=120 {
                let line = text(&header(&live, 3, width, true, &[0.2; 8]));
                assert!(line.width() <= width, "{phase} {width}: {line:?}");
                if width >= 12 {
                    assert!(
                        line.starts_with(&phase.to_uppercase()[..3]),
                        "{phase} {width}: {line:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn manager_approval_waiting_is_drawn_ahead_of_the_session() {
        let mut live = bar();
        live.manager = true;
        live.elsewhere = "Manager".into();
        live.attention = "approval waiting".into();
        let p = Palette::new(false);
        let out = header(&live, 0, 100, true, &[]);
        let line = text(&out);
        let chip = line.find("approval waiting").expect("chip shown");
        assert!(chip < line.find("in Manager").unwrap(), "{line:?}");
        let span = out
            .iter()
            .find(|s| s.content.contains("approval waiting"))
            .unwrap();
        assert_eq!(span.style.fg, Some(p.warning));
        assert!(span.style.add_modifier.contains(Modifier::BOLD));
        for width in 0..=100 {
            assert!(
                text(&header(&live, 0, width, true, &[])).width() <= width,
                "{width}"
            );
        }
        live.attention.clear();
        assert!(!text(&header(&live, 0, 100, true, &[])).contains("approval"));
    }

    #[test]
    fn narrow_headers_drop_extras_before_the_clock() {
        let live = bar();
        let wide = text(&header(&live, 0, 90, true, &[]));
        assert!(
            wide.contains("gpt-live-1-codex") && wide.contains("/live stop"),
            "{wide}"
        );
        let medium = text(&header(&live, 0, 40, true, &[]));
        assert!(
            !medium.contains("gpt-live-1-codex") && medium.ends_with("02:41"),
            "{medium}"
        );
        let tiny = text(&header(&live, 0, 20, true, &[]));
        assert!(
            tiny.starts_with("LISTENING") && tiny.ends_with("02:41"),
            "{tiny}"
        );
        let cramped = text(&header(&live, 0, 7, true, &[]));
        assert!(
            cramped.width() <= 7 && !cramped.contains("02:41"),
            "{cramped:?}"
        );
    }

    #[test]
    fn connecting_steps_every_second_frame_and_rests_on_the_last_dots_without_animation() {
        let mut live = bar();
        live.phase = "connecting".into();
        let dots = |tick, animate| {
            text(&header(&live, tick, 60, animate, &[]))
                .chars()
                .filter(|c| "●○".contains(*c))
                .collect::<String>()
        };
        assert_eq!(dots(0, true), "●○○");
        assert_eq!(dots(1, true), "●○○", "one step per two frames");
        assert_eq!(dots(2, true), "●●○");
        assert_eq!(dots(10, true), "○○○");
        assert_eq!(dots(12, true), "●○○", "the sweep repeats");
        assert_eq!(dots(0, false), "●●●");
        assert_eq!(dots(7, false), "●●●");
    }

    #[test]
    fn listening_meter_reads_the_ring_buffer_newest_last() {
        let live = bar();
        let meter = |mic: &[f32]| {
            text(&header(&live, 0, 60, true, mic))
                .chars()
                .filter(|c| LEVELS.contains(c))
                .collect::<String>()
        };
        assert_eq!(meter(&[]), "▁▁▁▁▁▁▁▁", "silence before any sample");
        assert_eq!(
            meter(&[1.0]),
            "▁▁▁▁▁▁▁█",
            "the newest sample is the rightmost"
        );
        assert_eq!(
            meter(&[0.0, 0.25, 1.0, 0.0, 1.0, 0.0, 0.25, 1.0]),
            "▁▅█▁█▁▅█"
        );
        let long = [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0];
        assert_eq!(meter(&long), "████████", "only the newest eight cells");
        let blue = header(&live, 0, 60, true, &[1.0]);
        assert!(blue
            .iter()
            .any(|s| s.style.fg == Some(Palette::new(false).blue)));
    }

    #[test]
    fn mic_history_keeps_eight_samples_one_per_frame_and_resets_with_the_call() {
        let mut mic = Mic::default();
        let mut live = bar();
        for tick in 0..12 {
            live.level_in = tick as f64 / 20.0;
            mic.observe(&live, tick);
            mic.observe(&live, tick); // a second draw inside the frame adds nothing
        }
        assert_eq!(mic.levels().len(), MIC_CELLS);
        assert!(
            (mic.levels()[7] - 0.55).abs() < 1e-6,
            "newest last: {:?}",
            mic.levels()
        );
        live.active = false;
        mic.observe(&live, 13);
        assert!(mic.levels().is_empty());
        live.active = true;
        mic.observe(&live, 13);
        assert_eq!(mic.levels().len(), 1);
    }

    #[test]
    fn working_shows_the_orbit_and_the_agent_turn() {
        let mut live = bar();
        live.phase = "working".into();
        let line = text(&header(&live, 0, 80, true, &[]));
        assert!(line.contains("⣾ agent running"), "{line}");
        live.detail = "sent\tSent to agent (steer)\tfix it  → turn #7".into();
        let line = text(&header(&live, 1, 80, true, &[]));
        assert!(line.contains("⣽ agent turn #7 running"), "{line}");
        assert!(text(&header(&live, 5, 80, false, &[])).contains("⣾ agent turn #7 running"));
        assert_eq!(
            latest_turn("sent\tSent to agent\ta → turn #2\nsent\tx\tb → turn #11"),
            Some(11)
        );
        assert_eq!(latest_turn("you\tYou\thi"), None);
    }

    #[test]
    fn speaking_wave_is_damped_by_the_output_level() {
        let p = Palette::new(false);
        let mut live = bar();
        live.phase = "speaking".into();
        let mut tones = |level: f64, tick: usize, animate: bool| {
            live.level_out = level;
            header(&live, tick, 60, animate, &[])
                .iter()
                .filter(|s| s.content.as_ref() == "∿")
                .map(|s| s.style.fg)
                .collect::<Vec<_>>()
        };
        let silent = tones(0.0, 3, true);
        assert_eq!(silent.len(), 8);
        assert!(
            silent.iter().all(|c| *c == Some(p.accent)),
            "silence is a flat accent line"
        );
        let loud = tones(1.0, 3, true);
        assert!(
            loud.iter().any(|c| *c != Some(p.accent)),
            "speech makes the wave"
        );
        assert!(
            loud.windows(2).any(|pair| pair[0] != pair[1]),
            "cells differ along the wave"
        );
        assert_ne!(loud, tones(1.0, 9, true), "it moves with the frame");
        assert!(
            tones(1.0, 9, false).iter().all(|c| *c == Some(p.accent)),
            "still without animation"
        );
    }

    #[test]
    fn muted_and_paused_are_static_and_paused_counts_down() {
        let mut live = bar();
        live.muted = true;
        live.phase = "muted".into();
        let a = text(&header(&live, 0, 70, true, &[0.5; 8]));
        assert_eq!(
            a,
            text(&header(&live, 9, 70, true, &[0.9; 8])),
            "no animation"
        );
        assert!(a.starts_with(&format!("{:<12}mic off", "MUTED")), "{a:?}");
        live.paused = true;
        live.pause_left = 95.0;
        live.phase = "paused".into();
        let line = text(&header(&live, 0, 70, true, &[]));
        assert!(
            line.starts_with(&format!("{:<12}ends in 01:35", "PAUSED")),
            "{line:?}"
        );
        assert!(
            line.contains("Ctrl+X P resume") && !line.contains("/live stop"),
            "{line:?}"
        );
        // A stale helper phase does not hide the pause or the mute.
        live.phase = "listening".into();
        assert!(text(&header(&live, 0, 70, true, &[])).starts_with("PAUSED"));
        live.paused = false;
        assert!(text(&header(&live, 0, 70, true, &[])).starts_with("MUTED"));
        live.phase = "ended".into();
        assert!(text(&header(&live, 0, 70, true, &[])).starts_with("ENDED"));
    }

    #[test]
    fn other_session_and_model_are_named_when_there_is_room() {
        let mut live = bar();
        live.elsewhere = "work".into();
        let line = text(&header(&live, 0, 120, true, &[]));
        assert!(
            line.contains("in work") && line.contains("gpt-live-1-codex"),
            "{line}"
        );
    }

    fn captions_of(detail: &str, width: usize) -> Rows {
        captions(
            detail,
            Palette::new(false).purple,
            width,
            &Palette::new(false),
            &None,
        )
    }
    fn row_text(rows: &Rows) -> Vec<String> {
        rows.iter().map(|(line, _)| line.to_string()).collect()
    }

    #[test]
    fn partial_you_is_italic_muted_and_final_text_is_plain() {
        let p = Palette::new(false);
        let rows = captions_of("caption\tYou…\tfix the bu\nyou\tYou\tfix the bug", 80);
        let partial = rows[0]
            .0
            .spans
            .iter()
            .find(|s| s.content.contains("fix the bu"))
            .unwrap();
        assert_eq!(partial.style.fg, Some(p.muted));
        assert!(partial.style.add_modifier.contains(Modifier::ITALIC));
        let done = rows[1]
            .0
            .spans
            .iter()
            .find(|s| s.content.contains("fix the bug"))
            .unwrap();
        assert_eq!(done.style.fg, Some(p.text));
        assert!(!done.style.add_modifier.contains(Modifier::ITALIC));
    }

    #[test]
    fn voice_caption_marks_the_newest_word() {
        let p = Palette::new(false);
        let rows = captions_of("caption\tVoice…\tI will run the tests now", 80);
        assert_eq!(row_text(&rows), ["│ Voice…  I will run the tests now"]);
        let spans = &rows[0].0.spans;
        let current = spans.iter().find(|s| s.content.as_ref() == "now").unwrap();
        assert_eq!(current.style.fg, Some(p.accent));
        assert!(current
            .style
            .add_modifier
            .contains(Modifier::BOLD | Modifier::UNDERLINED));
        let spoken = spans.iter().find(|s| s.content.contains("tests")).unwrap();
        assert_eq!(spoken.style.fg, Some(p.text));
        assert!(!spoken.content.contains("now"));
        // A single word is the current word; trailing space is not underlined.
        let one = captions_of("caption\tVoice…\tHello ", 80);
        let hello = one[0]
            .0
            .spans
            .iter()
            .find(|s| s.content.as_ref() == "Hello")
            .unwrap();
        assert_eq!(hello.style.fg, Some(p.accent));
    }

    #[test]
    fn sent_to_agent_is_a_chip_followed_by_the_text() {
        let p = Palette::new(false);
        let detail = "sent\tSent to agent (steer)\trun the tests  → turn #3";
        let rows = captions_of(detail, 80);
        assert_eq!(
            row_text(&rows),
            ["│  → sent to agent  turn #3 · steer  run the tests"]
        );
        let spans = &rows[0].0.spans;
        let chip = spans
            .iter()
            .find(|s| s.content.contains("sent to agent"))
            .unwrap();
        assert_eq!(
            (chip.style.fg, chip.style.bg),
            (Some(p.background), Some(p.warning))
        );
        assert!(chip.style.add_modifier.contains(Modifier::BOLD));
        let turn = spans
            .iter()
            .find(|s| s.content.contains("turn #3"))
            .unwrap();
        assert_eq!(
            (turn.style.fg, turn.style.bg),
            (Some(p.text), Some(p.element))
        );
        // In place: nothing about the row depends on a frame or a width above its text.
        assert_eq!(row_text(&captions_of(detail, 80)), row_text(&rows));
        assert_eq!(
            row_text(&captions_of("sent\tSent to agent (queue)\tlater", 80)),
            ["│  → sent to agent  queue  later"]
        );
    }

    #[test]
    fn sent_chip_wraps_under_itself_and_other_rows_keep_their_look() {
        let narrow = captions_of(
            "sent\tSent to agent (steer)\tone two three four five six seven → turn #12",
            60,
        );
        assert!(narrow.len() > 1);
        assert!(narrow.iter().all(|(line, _)| line.width() <= 60));
        let indent = format!("│ {}", " ".repeat(36));
        assert!(
            narrow[1].0.to_string().starts_with(&indent),
            "{:?}",
            row_text(&narrow)
        );
        let plain = captions_of(
            "told\tTold voice [final]\tdone\nnote\t\tstale\n\nbad\tError\tboom",
            60,
        );
        let reference = crate::transcript::live_rows(
            "told\tTold voice [final]\tdone\nnote\t\tstale\n\nbad\tError\tboom",
            Palette::new(false).purple,
            0,
            60,
            &Palette::new(false),
            &None,
        );
        assert_eq!(row_text(&plain), row_text(&reference));
    }
}
