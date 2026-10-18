//! Splash: the launch sequence shown until the first chat frame (design overhaul step 12,
//! plate P-SPL).
//!
//! 1. **Converge and load, together.** Every non-blank cell of the half-block wordmark is a
//!    particle that starts at a deterministic pseudo-random cell and eases to its place while
//!    a 30-segment bar fills under it. Both follow *milestones*, not a timer: the particles'
//!    progress is `min(bar, elapsed / 8 frames)`, so they arrive when the bar completes and a
//!    fast start still shows the motion. There is no maximum: a slow start just waits.
//! 2. **Flash.** The wordmark flashes once (160 ms).
//! 3. **Curtain.** A `▓▒░` wipe moves left to right at 500 cells per second from the splash
//!    into the first chat frame (`overlay`). Input is held only while the chat is not on
//!    screen (loading and flash); it reaches the composer during the wipe.
//!
//! The state machine is a pure function of "frames", which are 8 ms ticks since it was
//! created (continuous enough that motion is not stepped; the draw loop paces redraws at
//! 60 fps), so tests drive it with a fake clock. The first version stepped every 80 ms and
//! held input for ~4 s after the session loaded, which looked laggy and swallowed typing. The first milestones are *client-side*; the launcher
//! also sends `{"startup":N}` lines (N up to 3, see `startup_step`) before the first snapshot.
//! While waiting the splash idles (sway and scan pulse) without moving the percentage (see
//! `STEPS`, and docs/ratatui-parity.md). With
//! animations off the wordmark and bar are drawn in their final state and the chat follows
//! the first snapshot with a straight cut.
use crate::render::Palette;
use nexus_widgets::{theme::mix, Glyphs};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Modifier, Style},
    Frame,
};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

/// The splash clock tick (125 Hz); redraws are paced separately at 60 fps.
pub const FRAME: Duration = Duration::from_millis(8);
/// The small half-block wordmark (also the particles' targets).
pub const WORDMARK: [&str; 2] = ["█▄ █ █▀▀ ▀▄▀ █ █ █▀", "█ ▀█ ██▄ █ █ █▄█ ▄█"];
/// Milestone names, in order. Reaching step `n` (1-based) marks the first `n` as done.
pub const STEPS: [&str; 5] = ["daemon", "workspace", "providers", "session", "ready"];
const BAR_SEGMENTS: usize = 30;
/// Frames the bar takes to fill one fifth (200 ms).
const FILL_FRAMES: u32 = 25;
/// Least frames before the particles may all have arrived (480 ms).
const MIN_FRAMES: u32 = 60;
const FLASH_FRAMES: u32 = 20;
const CURTAIN_STEP: u32 = 4;
const CURTAIN_WIDTH: u32 = 3;
/// Share of the progress range over which particle start times are staggered.
const STAGGER: f32 = 0.6;
/// Radians per frame of the idle sway while waiting (one turn in about 2.5 s).
const IDLE_RATE: f32 = 0.02;
/// Frames per cell step of the scanning pulse on the unfilled bar while waiting.
const SCAN_FRAMES: u32 = 6;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// Particles converge while the bar fills.
    Loading,
    Flash,
    /// The wipe is on screen over the first chat frame.
    Curtain,
    Done,
}

#[derive(Clone, Debug)]
pub struct Splash {
    start: Instant,
    marks: [Option<u32>; 5],
    animate: bool,
}

fn ease(t: f32) -> f32 {
    1.0 - (1.0 - t.clamp(0.0, 1.0)).powi(3)
}

/// Deterministic per-particle hash (splitmix64 finaliser over the index).
fn hash(index: usize) -> u64 {
    let mut x = (index as u64 + 1).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    x ^= x >> 30;
    x = x.wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x ^= x >> 27;
    x = x.wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^ (x >> 31)
}

fn put(buf: &mut Buffer, area: Rect, x: i32, y: i32, symbol: &str, style: Style) {
    if x < i32::from(area.x)
        || y < i32::from(area.y)
        || x >= i32::from(area.right())
        || y >= i32::from(area.bottom())
    {
        return;
    }
    if let Some(cell) = buf.cell_mut((x as u16, y as u16)) {
        cell.set_symbol(symbol).set_style(style);
    }
}

/// The milestone a host startup line reports: `{"startup":N}` before the first snapshot, sent
/// by the launcher as the host connects and the session loads. Only the first three
/// milestones can be reported this way; `session` and `ready` stay client-observed, so a
/// line can never claim the chat is ready. `None` for anything else (a snapshot).
pub fn startup_step(line: &str) -> Option<usize> {
    if !line.starts_with("{\"startup\":") || line.len() > 64 {
        return None;
    }
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    let step = value.as_object()?.get("startup")?.as_u64()?;
    Some((step as usize).clamp(1, 3))
}

impl Splash {
    pub fn new(now: Instant, animate: bool) -> Self {
        Self {
            start: now,
            marks: [None; 5],
            animate,
        }
    }

    /// Whether the sequence animates (false: static wordmark and bar, then a straight cut).
    pub fn animated(&self) -> bool {
        self.animate
    }

    pub fn frame(&self, now: Instant) -> u32 {
        (now.saturating_duration_since(self.start).as_millis() / FRAME.as_millis()) as u32
    }

    /// Milestone `step` (1..=5) happened: it and every earlier one count as reached.
    pub fn reach(&mut self, step: usize, now: Instant) {
        let at = self.frame(now);
        for mark in self.marks.iter_mut().take(step.min(STEPS.len())) {
            mark.get_or_insert(at);
        }
    }

    pub fn reached(&self) -> usize {
        self.marks.iter().filter(|m| m.is_some()).count()
    }

    /// Bar fill, 0..=1: the k-th milestone fills the bar to k/5 over three frames.
    pub fn bar(&self, frame: u32) -> f32 {
        if !self.animate {
            return self.reached() as f32 / STEPS.len() as f32;
        }
        self.marks
            .iter()
            .enumerate()
            .filter_map(|(k, mark)| {
                let at = (*mark)?;
                let t = frame.saturating_sub(at) as f32 / FILL_FRAMES as f32;
                Some((k + 1) as f32 / STEPS.len() as f32 * ease(t))
            })
            .fold(0.0, f32::max)
    }

    /// Particle progress: follows the bar, but never faster than the minimum duration.
    pub fn progress(&self, frame: u32) -> f32 {
        if !self.animate {
            return 1.0;
        }
        self.bar(frame)
            .min((frame as f32 / MIN_FRAMES as f32).min(1.0))
    }

    /// First frame at which bar and particles are both complete, once `ready` was reached.
    pub fn complete_frame(&self) -> Option<u32> {
        self.marks[4].map(|at| (at + FILL_FRAMES).max(MIN_FRAMES))
    }

    pub fn phase(&self, frame: u32, width: u16) -> Phase {
        if !self.animate {
            return if self.marks[4].is_some() {
                Phase::Done
            } else {
                Phase::Loading
            };
        }
        let Some(done) = self.complete_frame() else {
            return Phase::Loading;
        };
        if frame < done {
            Phase::Loading
        } else if frame < done + FLASH_FRAMES {
            Phase::Flash
        } else if self
            .curtain_col(frame)
            .is_some_and(|col| col < u32::from(width) + CURTAIN_WIDTH)
        {
            Phase::Curtain
        } else {
            Phase::Done
        }
    }

    /// Cells the splash alone covers the screen (the chat is not drawn yet).
    pub fn covers(&self, now: Instant, width: u16) -> bool {
        matches!(
            self.phase(self.frame(now), width),
            Phase::Loading | Phase::Flash
        )
    }

    /// Input is held while the chat is not on screen (loading and flash); during the wipe it
    /// already reaches the composer.
    pub fn holds_input(&self, now: Instant, width: u16) -> bool {
        matches!(
            self.phase(self.frame(now), width),
            Phase::Loading | Phase::Flash
        )
    }

    /// The wipe has left the screen: the splash can be dropped.
    pub fn finished(&self, now: Instant, width: u16) -> bool {
        self.phase(self.frame(now), width) == Phase::Done
    }

    /// Left edge of the curtain's leading column: 4 cells per 8 ms tick after the flash.
    fn curtain_col(&self, frame: u32) -> Option<u32> {
        let start = self.complete_frame()? + FLASH_FRAMES;
        Some(frame.saturating_sub(start) * CURTAIN_STEP)
    }

    /// The step name beside the percentage.
    fn step_name(&self) -> &'static str {
        STEPS[self.reached().min(STEPS.len() - 1)]
    }

    /// Still waiting on the milestones (animated, `ready` not reached): the sequence idles
    /// instead of freezing. This is motion only; the percentage never moves for it.
    fn waiting(&self) -> bool {
        self.animate && self.marks[4].is_none()
    }

    /// Idle sway of a particle in flight, in cells: a slow orbit around its path position,
    /// fading out as it nears its target. Zero once `ready` is reached, so the arrival and
    /// the flash are exactly the milestone-driven motion.
    fn drift(&self, frame: u32, h: u64, e: f32) -> (i32, i32) {
        if !self.waiting() {
            return (0, 0);
        }
        let phase = (h % 628) as f32 / 100.0;
        let angle = frame as f32 * IDLE_RATE + phase;
        let reach = (1.0 - e) * 2.0;
        (
            (angle.sin() * reach * 1.5).round() as i32,
            (angle.cos() * reach * 0.75).round() as i32,
        )
    }

    /// Draw wordmark, particles and bar into `buf` (the whole `area`).
    pub fn render(&self, buf: &mut Buffer, area: Rect, frame: u32, p: &Palette, ascii: bool) {
        buf.set_style(area, Style::default().bg(p.background).fg(p.text));
        if area.width == 0 || area.height == 0 {
            return;
        }
        let bg = Style::default().bg(p.background);
        let progress = self.progress(frame);
        let flash = match self.complete_frame() {
            Some(done) if self.animate && frame >= done && frame < done + FLASH_FRAMES => {
                let k = (frame - done) as f32 + 0.5;
                1.0 - (k / FLASH_FRAMES as f32 * 2.0 - 1.0).abs()
            }
            _ => 0.0,
        };
        let height = WORDMARK.len() as i32 + 2;
        let top = i32::from(area.y) + (i32::from(area.height) - height).max(0) / 2;
        let mark_width = WORDMARK[0].width() as i32;
        let left = i32::from(area.x) + (i32::from(area.width) - mark_width).max(0) / 2;

        let mut targets: Vec<(i32, i32, &str)> = Vec::new();
        for (row, text) in WORDMARK.into_iter().enumerate() {
            for (col, (byte, ch)) in text.char_indices().enumerate() {
                if ch != ' ' {
                    let symbol = if ascii {
                        "#"
                    } else {
                        &text[byte..byte + ch.len_utf8()]
                    };
                    targets.push((left + col as i32, top + row as i32, symbol));
                }
            }
        }
        // Particles still in flight first, so settled cells are never covered by one.
        let mut settled = Vec::new();
        for (index, (tx, ty, symbol)) in targets.iter().enumerate() {
            let h = hash(index);
            let sx = i32::from(area.x) + (h % u64::from(area.width)) as i32;
            let sy = i32::from(area.y) + ((h >> 20) % u64::from(area.height)) as i32;
            let delay = ((h >> 40) % 17) as f32 / 16.0 * STAGGER;
            let t = ((progress - delay) / (1.0 - STAGGER)).clamp(0.0, 1.0);
            if t >= 1.0 {
                settled.push((*tx, *ty, *symbol));
                continue;
            }
            let e = ease(t);
            let (dx, dy) = self.drift(frame, h, e);
            let x = sx + ((*tx - sx) as f32 * e).round() as i32 + dx;
            let y = sy + ((*ty - sy) as f32 * e).round() as i32 + dy;
            let glyph = match (t < 0.5, ascii) {
                (true, false) => "·",
                (false, false) => "•",
                (true, true) => ".",
                (false, true) => "o",
            };
            put(buf, area, x, y, glyph, bg.fg(mix(p.quiet, p.accent, t)));
        }
        let ink = mix(p.accent, p.text, flash);
        for (x, y, symbol) in settled {
            put(
                buf,
                area,
                x,
                y,
                symbol,
                bg.fg(ink).add_modifier(Modifier::BOLD),
            );
        }

        // The bar: segments, percentage and the current step name.
        let pct = (self.bar(frame) * 100.0).round() as u32;
        let label = format!("  {pct:>3}%  {}", self.step_name());
        let room = i32::from(area.width) - label.width() as i32;
        let segments = (BAR_SEGMENTS as i32).min(room.max(0)) as usize;
        let filled = (self.bar(frame) * segments as f32).round() as usize;
        let total = segments as i32 + label.width() as i32;
        let mut x = i32::from(area.x) + (i32::from(area.width) - total).max(0) / 2;
        let y = top + WORDMARK.len() as i32 + 1;
        // Waiting: a pulse sweeps the unfilled track (colour only, the fill is unchanged).
        let scan = (self.waiting() && segments > filled).then(|| {
            filled + (frame / SCAN_FRAMES) as usize % (segments - filled)
        });
        for i in 0..segments {
            let (symbol, color) = match (i < filled, ascii) {
                (true, false) => ("▰", p.accent),
                (false, false) => ("▱", p.border_strong),
                (true, true) => ("#", p.accent),
                (false, true) => ("-", p.border_strong),
            };
            let color = if scan == Some(i) { p.muted } else { color };
            put(buf, area, x, y, symbol, bg.fg(color));
            x += 1;
        }
        for ch in label.chars() {
            let mut utf8 = [0u8; 4];
            put(buf, area, x, y, ch.encode_utf8(&mut utf8), bg.fg(p.muted));
            x += 1;
        }
    }

    /// Splash alone: the frame while loading and flashing.
    pub fn draw(&self, frame: &mut Frame, now: Instant, p: &Palette) {
        let area = frame.area();
        self.render(
            frame.buffer_mut(),
            area,
            self.frame(now),
            p,
            Glyphs::from_env().ascii,
        );
    }

    /// Wipe over a finished chat frame: cells right of the curtain still show the splash.
    /// Does nothing outside the curtain phase, so the last frame is exactly the chat buffer.
    pub fn overlay(&self, chat: &mut Buffer, now: Instant, p: &Palette, ascii: bool) {
        let area = chat.area;
        let frame = self.frame(now);
        if self.phase(frame, area.width) != Phase::Curtain {
            return;
        }
        let Some(col) = self.curtain_col(frame) else {
            return;
        };
        let mut scratch = Buffer::empty(area);
        self.render(&mut scratch, area, frame, p, ascii);
        for y in area.y..area.bottom() {
            for x in area.x..area.right() {
                if u32::from(x - area.x) >= col {
                    if let (Some(to), Some(from)) = (chat.cell_mut((x, y)), scratch.cell((x, y))) {
                        *to = from.clone();
                    }
                }
            }
        }
        let glyphs = if ascii {
            ["#", "=", "-"]
        } else {
            ["▓", "▒", "░"]
        };
        // The curtain is a full-height wipe.
        for y in area.y..area.bottom() {
            for (i, glyph) in glyphs.iter().enumerate() {
                let x = i32::from(area.x) + col as i32 - CURTAIN_WIDTH as i32 + i as i32;
                let style = Style::default().fg(p.accent).bg(p.background);
                put(chat, area, x, i32::from(y), glyph, style);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(start: Instant, frame: u32) -> Instant {
        start + FRAME * frame
    }

    fn text(buf: &Buffer, y: u16) -> String {
        (0..buf.area.width).map(|x| buf[(x, y)].symbol()).collect()
    }

    fn rendered(s: &Splash, frame: u32, w: u16, h: u16) -> Buffer {
        rendered_as(s, frame, w, h, false)
    }

    fn rendered_as(s: &Splash, frame: u32, w: u16, h: u16, ascii: bool) -> Buffer {
        let area = Rect::new(0, 0, w, h);
        let mut buf = Buffer::empty(area);
        s.render(&mut buf, area, frame, &Palette::new(false), ascii);
        buf
    }

    #[test]
    fn bar_follows_milestones_not_the_clock() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        assert_eq!(s.bar(0), 0.0);
        assert_eq!(
            s.bar(500),
            0.0,
            "no milestone, no progress, however long it takes"
        );
        s.reach(1, at(t0, 0));
        assert!(s.bar(1) > 0.0 && s.bar(FILL_FRAMES) - 0.2 < 1e-6);
        assert!((s.bar(40) - 0.2).abs() < 1e-6, "one of five steps");
        s.reach(3, at(t0, 10));
        assert!((s.bar(50) - 0.6).abs() < 1e-6);
        assert_eq!(s.step_name(), "session");
        s.reach(5, at(t0, 20));
        assert!((s.bar(60) - 1.0).abs() < 1e-6);
        assert_eq!(s.step_name(), "ready");
        let mut last = 0.0;
        for f in 0..80 {
            assert!(s.bar(f) >= last, "monotone at {f}");
            last = s.bar(f);
        }
    }

    #[test]
    fn startup_lines_report_only_host_milestones() {
        assert_eq!(startup_step("{\"startup\":2}"), Some(2));
        assert_eq!(startup_step("{\"startup\":3}"), Some(3));
        assert_eq!(startup_step("{\"startup\":5}"), Some(3), "never claims ready");
        assert_eq!(startup_step("{\"startup\":0}"), Some(1));
        assert_eq!(startup_step("{\"schema\":3,\"startup\":2}"), None);
        assert_eq!(startup_step("{\"startup\":\"x\"}"), None);
        assert_eq!(startup_step("{\"startup\":2,\"blocks\":[]}x"), None);
    }

    #[test]
    fn host_progress_moves_the_bar_before_any_snapshot() {
        // The bar used to sit at 20% (client only) until the first snapshot, however far the
        // host had got.
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(1, t0);
        assert!((s.bar(200) - 0.2).abs() < 1e-6);
        s.reach(startup_step("{\"startup\":2}").unwrap(), at(t0, 200));
        assert!((s.bar(200 + FILL_FRAMES) - 0.4).abs() < 1e-6);
        s.reach(startup_step("{\"startup\":3}").unwrap(), at(t0, 300));
        assert!((s.bar(300 + FILL_FRAMES) - 0.6).abs() < 1e-6);
        assert_eq!(s.phase(10_000, 80), Phase::Loading, "ready still needs a snapshot");
    }

    #[test]
    fn bar_text_shows_percentage_and_step() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        let buf = rendered(&s, 60, 80, 12);
        let row = (0..12)
            .map(|y| text(&buf, y))
            .find(|row| row.contains('%'))
            .unwrap();
        assert!(row.contains("40%") && row.contains("providers"), "{row:?}");
        assert_eq!(row.matches('▰').count(), 12, "{row:?}");
        assert_eq!(row.matches('▱').count(), 18, "{row:?}");
    }

    #[test]
    fn particles_reach_their_targets_when_progress_is_one() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let done = s.complete_frame().unwrap();
        assert_eq!(
            done, MIN_FRAMES,
            "a fast start still lasts the minimum duration"
        );
        assert!(s.progress(done) >= 1.0 - 1e-6);
        let settled = rendered(&s, done, 80, 12);
        let wordmark: Vec<String> = (0..12)
            .map(|y| text(&settled, y))
            .filter(|row| row.contains('█') || row.contains('▀') || row.contains('▄'))
            .collect();
        assert_eq!(wordmark.len(), 2);
        assert_eq!(wordmark[0].trim(), WORDMARK[0]);
        assert_eq!(wordmark[1].trim(), WORDMARK[1]);
        let early = rendered(&s, 1, 80, 12);
        let early_rows: Vec<String> = (0..12).map(|y| text(&early, y)).collect();
        assert!(
            early_rows
                .iter()
                .any(|r| r.contains('·') || r.contains('•')),
            "particles in flight"
        );
        assert!(
            !early_rows.iter().any(|r| r.trim() == WORDMARK[0]),
            "not assembled yet"
        );
    }

    #[test]
    fn a_slow_start_waits_with_particles_unfinished() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        assert!(
            s.progress(1000) < 1.0,
            "no maximum duration: it waits for the milestones"
        );
        assert_eq!(s.phase(1000, 80), Phase::Loading);
        assert!(s.covers(at(t0, 1000), 80));
    }

    #[test]
    fn waiting_at_twenty_percent_keeps_moving_without_inventing_progress() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(1, at(t0, 0));
        // Long after the bar settled at 20%, the daemon is still connecting.
        let (a, b) = (rendered(&s, 400, 80, 12), rendered(&s, 430, 80, 12));
        assert_ne!(a, b, "particles and the scan pulse keep animating");
        let pct = |buf: &Buffer| {
            let row = (0..12)
                .map(|y| text(buf, y))
                .find(|row| row.contains('%'))
                .unwrap();
            let bar_start = row.find(|c: char| c == '▰' || c == '▱').unwrap();
            let pct_pos = row.find('%').unwrap();
            let after_pct = &row[pct_pos + 1..];
            let step_start = pct_pos + 1 + after_pct.find(|c: char| c.is_alphabetic()).unwrap();
            let step_end = step_start
                + row[step_start..]
                    .find(|c: char| !c.is_alphabetic())
                    .unwrap_or(row.len() - step_start);
            let bar_and_label = &row[bar_start..step_end];
            format!("{}:{}", row.matches('▰').count(), bar_and_label)
        };
        assert_eq!(pct(&a), pct(&b), "same fill, same percentage and step");
        assert!(pct(&a).contains("20%") && pct(&a).contains("workspace"));
        assert!((s.bar(430) - 0.2).abs() < 1e-6 && s.progress(430) < 1.0);
        // The motion is idle only: once ready, frames are a pure function of the milestones.
        s.reach(5, at(t0, 500));
        assert_eq!(s.drift(520, 12345, 0.0), (0, 0));
        assert_eq!(rendered(&s, 600, 80, 12), rendered(&s, 610, 80, 12));
    }

    #[test]
    fn idle_sway_is_bounded_and_fades_near_the_target() {
        let t0 = Instant::now();
        let s = Splash::new(t0, true);
        for frame in 0..400 {
            for h in [1u64, 77, 31_337, u64::MAX] {
                let (dx, dy) = s.drift(frame, h, 0.0);
                assert!(dx.abs() <= 3 && dy.abs() <= 2, "{dx},{dy}");
                assert_eq!(s.drift(frame, h, 1.0), (0, 0), "settled particles do not sway");
            }
        }
        assert_eq!(
            Splash::new(t0, false).drift(10, 5, 0.0),
            (0, 0),
            "no animation, no sway"
        );
    }

    #[test]
    fn phases_run_loading_flash_curtain_done_and_hold_input_until_done() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let done = s.complete_frame().unwrap();
        assert_eq!(s.phase(done - 1, 80), Phase::Loading);
        assert_eq!(s.phase(done, 80), Phase::Flash);
        assert_eq!(s.phase(done + FLASH_FRAMES - 1, 80), Phase::Flash);
        assert_eq!(s.phase(done + FLASH_FRAMES, 80), Phase::Curtain);
        let width = 80u32;
        let last = done + FLASH_FRAMES + (width + CURTAIN_WIDTH) / CURTAIN_STEP;
        assert_eq!(s.phase(last - 1, 80), Phase::Curtain);
        assert_eq!(s.phase(last + 1, 80), Phase::Done);
        assert!(s.holds_input(at(t0, done + 2), 80));
        assert!(
            !s.holds_input(at(t0, done + FLASH_FRAMES + 3), 80),
            "input flows during the wipe"
        );
        assert!(!s.finished(at(t0, done + FLASH_FRAMES + 3), 80));
        assert!(s.finished(at(t0, last + 1), 80));
        assert!(!s.holds_input(at(t0, last + 1), 80));
        assert!(s.covers(at(t0, done), 80) && !s.covers(at(t0, done + FLASH_FRAMES), 80));
    }

    #[test]
    fn curtain_moves_four_cells_per_tick() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let start = s.complete_frame().unwrap() + FLASH_FRAMES;
        assert_eq!(s.curtain_col(start), Some(0));
        assert_eq!(s.curtain_col(start + 1), Some(4));
        assert_eq!(s.curtain_col(start + 10), Some(40));
    }

    #[test]
    fn the_whole_sequence_after_ready_is_short() {
        // A fresh start reaches ready at once; at 140 columns the user waits well under a
        // second and a half for the chat, and typing is accepted before that.
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let done = s.complete_frame().unwrap();
        let end = done + FLASH_FRAMES + (140 + CURTAIN_WIDTH) / CURTAIN_STEP + 1;
        assert!(FRAME * end < Duration::from_millis(1100), "{:?}", FRAME * end);
        assert!(s.finished(at(t0, end), 140));
        // A late milestone (a slow session) still ends the same short way, never earlier.
        let mut late = Splash::new(t0, true);
        late.reach(5, at(t0, 5000));
        assert!(!late.finished(at(t0, 5000), 140));
        assert!(late.finished(at(t0, 5000 + end), 140));
    }

    #[test]
    fn motion_is_continuous_between_redraws() {
        // At the 16 ms redraw pace, no particle jumps more than a few cells between two draws.
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let area = Rect::new(0, 0, 140, 40);
        let p = Palette::new(false);
        let cells = |frame: u32| {
            let mut buf = Buffer::empty(area);
            s.render(&mut buf, area, frame, &p, false);
            (0..40u16)
                .flat_map(|y| (0..140u16).map(move |x| (x, y)))
                .filter(|&(x, y)| matches!(buf[(x, y)].symbol(), "·" | "•"))
                .count()
        };
        let (a, b) = (cells(10), cells(12));
        assert!(a > 0 && a.abs_diff(b) <= 4, "{a} vs {b}");
    }

    #[test]
    fn flash_brightens_the_wordmark_once() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let done = s.complete_frame().unwrap();
        let p = Palette::new(false);
        let ink = |frame: u32| {
            let buf = rendered(&s, frame, 80, 12);
            (0..12u16)
                .flat_map(|y| (0..80u16).map(move |x| (x, y)))
                .find(|&(x, y)| buf[(x, y)].symbol() == "█")
                .map(|(x, y)| buf[(x, y)].fg)
                .unwrap()
        };
        assert_eq!(ink(done - 1), p.accent);
        assert_ne!(
            ink(done + FLASH_FRAMES / 2),
            p.accent,
            "mid-flash is blended toward the peak"
        );
        assert_eq!(ink(done + FLASH_FRAMES), p.accent, "settles back");
    }

    #[test]
    fn overlay_wipes_left_to_right_and_the_last_frame_is_the_chat() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let start = s.complete_frame().unwrap() + FLASH_FRAMES;
        let area = Rect::new(0, 0, 40, 10);
        let p = Palette::new(false);
        let chat = || {
            let mut buf = Buffer::empty(area);
            buf.set_style(area, Style::default().bg(p.panel));
            for y in 0..10 {
                buf.set_string(0, y, "c".repeat(40), Style::default());
            }
            buf
        };
        let before = chat();
        let mut at_start = chat();
        s.overlay(&mut at_start, at(t0, start - 1), &p, false);
        assert_eq!(at_start, before, "before the curtain the chat is untouched");

        let mut mid = chat();
        s.overlay(&mut mid, at(t0, start + 5), &p, false); // column 20
        assert_eq!(mid[(0, 0)].symbol(), "c", "left of the curtain is chat");
        assert_eq!(mid[(17, 3)].symbol(), "▓");
        assert_eq!(mid[(18, 3)].symbol(), "▒");
        assert_eq!(mid[(19, 3)].symbol(), "░");
        assert_ne!(
            mid[(30, 3)].symbol(),
            "c",
            "right of it is still the splash"
        );
        assert_eq!(mid[(30, 3)].bg, p.background);

        let mut end = chat();
        s.overlay(&mut end, at(t0, start + 40), &p, false);
        assert_eq!(end, before, "the final frame equals the chat buffer");
    }

    #[test]
    fn without_animation_the_final_state_is_static_and_the_chat_cuts_straight_in() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, false);
        assert_eq!(s.phase(0, 80), Phase::Loading);
        let early = rendered(&s, 0, 80, 12);
        let rows: Vec<String> = (0..12).map(|y| text(&early, y)).collect();
        assert!(
            rows.iter().any(|r| r.trim() == WORDMARK[0]),
            "wordmark drawn assembled"
        );
        assert!(
            !rows.iter().any(|r| r.contains('·') || r.contains('•')),
            "no particles"
        );
        s.reach(5, at(t0, 0));
        assert_eq!(s.phase(0, 80), Phase::Done, "no flash, no curtain");
        assert!(!s.holds_input(t0, 80));
    }

    #[test]
    fn ascii_glyph_set_has_no_unicode() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        for frame in [2, 60] {
            let buf = rendered_as(&s, frame, 80, 12, true);
            let all: String = (0..12).map(|y| text(&buf, y)).collect();
            assert!(all.is_ascii(), "{all:?}");
            assert!(all.contains('#') && all.contains('-'));
        }
        s.reach(5, at(t0, 0));
        let area = Rect::new(0, 0, 40, 6);
        let mut chat = Buffer::empty(area);
        s.overlay(
            &mut chat,
            at(t0, s.complete_frame().unwrap() + FLASH_FRAMES + 3),
            &Palette::new(false),
            true,
        );
        assert!((0..6u16).all(|y| (0..40u16).all(|x| chat[(x, y)].symbol().is_ascii())));
        assert_eq!(
            chat[(9, 0)].symbol(),
            "#",
            "the curtain's first column (col 12 - 3)"
        );
        assert_eq!(chat[(10, 0)].symbol(), "=");
        assert_eq!(chat[(11, 0)].symbol(), "-");
    }

    #[test]
    fn renders_at_any_size_without_panicking() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        for step in [0usize, 2, 5] {
            if step > 0 {
                s.reach(step, at(t0, 0));
            }
            for w in 1..60u16 {
                for h in 1..9u16 {
                    for frame in [0u32, 3, 8, 12, 20, 80] {
                        let _ = rendered(&s, frame, w, h);
                        let area = Rect::new(0, 0, w, h);
                        let mut chat = Buffer::empty(area);
                        s.overlay(&mut chat, at(t0, frame), &Palette::new(false), false);
                    }
                }
            }
        }
    }
}
