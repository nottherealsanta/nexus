//! The launch splash: the logo and a milestone bar until the first live frame.
//!
//! The logo is painted from the first frame; the ambient field and the intro
//! particles move on the clock alone, so a slow Core never freezes the screen.
//! Only the bar follows milestones. The five milestones are what the client can
//! observe while it starts: the client is up, Core answered, the
//! snapshot/config is in, the event stream is open, and the first live frame is
//! ready. Time never moves the bar.
//!
//! Once `ready` is reached the logo flashes and a `▓▒░` curtain wipes into the
//! first chat frame. With animations off the logo and bar are drawn in their
//! final state and the chat follows the first live frame with a straight cut.
//!
//! The palette comes from the theme: ink is `text.primary` (white in the dark
//! theme), the ambient field is `text.faint`, and the unfilled bar is
//! `border.strong`. The accent is deliberately not used on the splash.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

use crate::paint::LOGO;

/// The splash clock tick; redraws are paced by the main loop.
pub const FRAME: Duration = Duration::from_millis(8);
/// Milestone names, in order. Reaching step `n` (1-based) marks the first `n`.
pub const STEPS: [&str; 5] = ["daemon", "workspace", "providers", "session", "ready"];
const BAR_SEGMENTS: usize = 30;
/// Frames the bar takes to fill one fifth (200 ms).
const FILL_FRAMES: u32 = 25;
/// Frames the intro particles take to settle on the clock alone (480 ms); also
/// the least time before the flash.
const MIN_FRAMES: u32 = 60;
const FLASH_FRAMES: u32 = 20;
const CURTAIN_STEP: u32 = 4;
const CURTAIN_WIDTH: u32 = 3;
/// Share of the intro range over which particle start times are staggered.
const STAGGER: f32 = 0.6;
/// Frames per cell step of the scanning pulse on the unfilled bar while waiting.
const SCAN_FRAMES: u32 = 6;
/// Sub-cell steps per cell of the ambient field's position.
const AMBIENT_SUBCELL: i64 = 48;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// Intro particles settle while the bar fills.
    Loading,
    Flash,
    /// The wipe is on screen over the first chat frame.
    Curtain,
    Done,
}

/// Styles the splash needs. Passed in so the splash itself knows nothing about
/// the theme's token names.
pub struct Palette {
    pub logo: Style,
    pub ink: Style,
    pub dim: Style,
    pub muted: Style,
    pub border: Style,
    pub peak: Style,
    pub ascii: bool,
}

#[derive(Clone, Debug)]
pub struct Splash {
    start: Instant,
    marks: [Option<u32>; 5],
    animate: bool,
    dismissed: bool,
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

impl Splash {
    pub fn new(now: Instant, animate: bool) -> Self {
        Self {
            start: now,
            marks: [None; 5],
            animate,
            dismissed: false,
        }
    }

    pub fn set_animate(&mut self, animate: bool) {
        self.animate = animate;
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

    /// The splash is over: the normal shell is shown from now on.
    pub fn dismiss(&mut self) {
        self.dismissed = true;
    }

    pub fn reached(&self) -> usize {
        self.marks.iter().filter(|m| m.is_some()).count()
    }

    /// Bar fill, 0..=1: the k-th milestone fills the bar to k/5 over 200 ms.
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

    /// Intro particle settling, 0..=1: a function of the clock only, never a
    /// milestone.
    pub fn progress(&self, frame: u32) -> f32 {
        if !self.animate {
            return 1.0;
        }
        (frame as f32 / MIN_FRAMES as f32).min(1.0)
    }

    /// First frame at which bar and particles are both complete, once `ready`
    /// was reached.
    pub fn complete_frame(&self) -> Option<u32> {
        self.marks[4].map(|at| (at + FILL_FRAMES).max(MIN_FRAMES))
    }

    pub fn phase(&self, frame: u32, width: u16) -> Phase {
        if self.dismissed {
            return Phase::Done;
        }
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

    /// Whether the splash still animates and the main loop should keep drawing.
    pub fn animating(&self, now: Instant, width: u16) -> bool {
        self.animate && self.phase(self.frame(now), width) != Phase::Done
    }

    /// The splash alone covers the screen (the chat is not drawn yet).
    pub fn covers(&self, now: Instant, width: u16) -> bool {
        matches!(
            self.phase(self.frame(now), width),
            Phase::Loading | Phase::Flash
        )
    }

    /// Input is held while the chat is not on screen; during the wipe it already
    /// reaches the composer.
    pub fn holds_input(&self, now: Instant, width: u16) -> bool {
        self.covers(now, width)
    }

    /// Left edge of the curtain's leading column: 4 cells per 8 ms tick after
    /// the flash.
    fn curtain_col(&self, frame: u32) -> Option<u32> {
        let start = self.complete_frame()? + FLASH_FRAMES;
        Some(frame.saturating_sub(start) * CURTAIN_STEP)
    }

    fn step_name(&self) -> &'static str {
        STEPS[self.reached().min(STEPS.len() - 1)]
    }

    /// Still waiting on the milestones (animated, `ready` not reached): the scan
    /// pulse runs on the unfilled track.
    fn waiting(&self) -> bool {
        self.animate && self.marks[4].is_none()
    }

    /// Cell of ambient particle `index` in an `w` x `h` area at `frame`.
    fn ambient_cell(index: usize, frame: u32, w: u16, h: u16) -> (i32, i32) {
        let hv = hash(index + 1000);
        let (w, h) = (i64::from(w), i64::from(h));
        let vx = ((hv >> 32) % 7) as i64 - 3;
        let vx = if vx == 0 { 1 } else { vx };
        let vy = ((hv >> 44) % 5) as i64 - 2;
        let t = i64::from(frame);
        let x = ((hv % w as u64) as i64 * AMBIENT_SUBCELL + t * vx).rem_euclid(w * AMBIENT_SUBCELL);
        let y = ((((hv >> 20) % h as u64) as i64) * AMBIENT_SUBCELL + t * vy / 2)
            .rem_euclid(h * AMBIENT_SUBCELL);
        ((x / AMBIENT_SUBCELL) as i32, (y / AMBIENT_SUBCELL) as i32)
    }

    /// The ambient field: dim dots drifting across `area`, skipping `keep_clear`.
    fn ambient(&self, buf: &mut Buffer, area: Rect, frame: u32, keep_clear: Rect, p: &Palette) {
        let count = (usize::from(area.width) * usize::from(area.height) / 48).clamp(6, 96);
        for index in 0..count {
            let (cx, cy) = Self::ambient_cell(index, frame, area.width, area.height);
            let (x, y) = (i32::from(area.x) + cx, i32::from(area.y) + cy);
            if x >= i32::from(keep_clear.x)
                && x < i32::from(keep_clear.right())
                && y >= i32::from(keep_clear.y)
                && y < i32::from(keep_clear.bottom())
            {
                continue;
            }
            // A slow twinkle: each particle brightens for a third of a 1.5 s cycle.
            let bright = (u64::from(frame / 60) + hash(index + 1000) % 3).is_multiple_of(3);
            let (glyph, style) = match (bright, p.ascii) {
                (true, false) => ("•", p.ink),
                (false, false) => ("·", p.dim),
                (true, true) => ("o", p.ink),
                (false, true) => (".", p.dim),
            };
            put(buf, area, x, y, glyph, style);
        }
    }

    /// Draw the logo, its particles and the bar into `buf` (the whole `area`).
    pub fn render(&self, buf: &mut Buffer, area: Rect, frame: u32, p: &Palette) {
        if area.width == 0 || area.height == 0 {
            return;
        }
        let progress = self.progress(frame);
        let flash = match self.complete_frame() {
            Some(done) if self.animate && frame >= done && frame < done + FLASH_FRAMES => {
                let k = (frame - done) as f32 + 0.5;
                1.0 - (k / FLASH_FRAMES as f32 * 2.0 - 1.0).abs()
            }
            _ => 0.0,
        };
        // The logo, a blank row, the bar row, then the step name.
        let mark = &LOGO;
        let height = mark.len() as i32 + 3;
        let top = i32::from(area.y) + (i32::from(area.height) - height).max(0) / 2;
        let mark_width = mark.iter().map(|row| row.width()).max().unwrap_or(0) as i32;
        let left = i32::from(area.x) + (i32::from(area.width) - mark_width).max(0) / 2;

        let mut targets: Vec<(i32, i32, &str)> = Vec::new();
        for (row, text) in mark.iter().copied().enumerate() {
            for (col, (byte, ch)) in text.char_indices().enumerate() {
                if ch != ' ' {
                    let symbol = if p.ascii {
                        "#"
                    } else {
                        &text[byte..byte + ch.len_utf8()]
                    };
                    targets.push((left + col as i32, top + row as i32, symbol));
                }
            }
        }
        // Layer 1, clock only: the ambient field behind everything, clear of the
        // text block.
        if self.animate {
            let block = (BAR_SEGMENTS as i32 + 18).max(mark_width) + 4;
            let clear_left =
                (i32::from(area.x) + (i32::from(area.width) - block).max(0) / 2).max(0);
            let keep_clear = Rect::new(
                clear_left as u16,
                (top - 1).max(i32::from(area.y)) as u16,
                block.min(i32::from(area.width)) as u16,
                (height + 2).min(i32::from(area.height)) as u16,
            );
            self.ambient(buf, area, frame, keep_clear, p);
        }
        // Wordmark particles still in flight first. The logo itself is drawn
        // from the first frame: the terminal capability queries can freeze the
        // screen for a moment before the animation loop starts, and the launch
        // must be recognisable during that pause.
        for (index, (tx, ty, _symbol)) in targets.iter().enumerate() {
            let h = hash(index);
            let sx = i32::from(area.x) + (h % u64::from(area.width)) as i32;
            let sy = i32::from(area.y) + ((h >> 20) % u64::from(area.height)) as i32;
            let delay = ((h >> 40) % 17) as f32 / 16.0 * STAGGER;
            let t = ((progress - delay) / (1.0 - STAGGER)).clamp(0.0, 1.0);
            if t >= 1.0 {
                continue;
            }
            let e = ease(t);
            let x = sx + ((*tx - sx) as f32 * e).round() as i32;
            let y = sy + ((*ty - sy) as f32 * e).round() as i32;
            let glyph = match (t < 0.5, p.ascii) {
                (true, false) => "·",
                (false, false) => "•",
                (true, true) => ".",
                (false, true) => "o",
            };
            put(buf, area, x, y, glyph, p.dim);
        }
        let logo = if flash > 0.0 { p.peak } else { p.logo };
        for (x, y, symbol) in &targets {
            put(buf, area, *x, *y, symbol, logo);
        }

        // The bar: segments centred on their own, the percentage on their right
        // and the current step name centred on the row below.
        let pct = (self.bar(frame) * 100.0).round() as u32;
        let label = format!(" {pct:>3}%");
        let room = i32::from(area.width) - label.width() as i32;
        let segments = (BAR_SEGMENTS as i32).min(room.max(0)) as usize;
        let filled = (self.bar(frame) * segments as f32).round() as usize;
        // Centred; on a screen too narrow for the percentage beside it, shift
        // left to fit.
        let centred = (i32::from(area.width) - segments as i32).max(0) / 2;
        let shifted = (room - segments as i32).max(0);
        let mut x = i32::from(area.x) + centred.min(shifted);
        let y = top + mark.len() as i32 + 1;
        // Waiting: a pulse sweeps the unfilled track (colour only; the fill is
        // unchanged).
        let scan = (self.waiting() && segments > filled)
            .then(|| filled + (frame / SCAN_FRAMES) as usize % (segments - filled));
        for i in 0..segments {
            let (symbol, style) = match (i < filled, p.ascii) {
                (true, false) => ("▰", p.ink),
                (false, false) => ("▱", p.border),
                (true, true) => ("#", p.ink),
                (false, true) => ("-", p.border),
            };
            let style = if scan == Some(i) { p.muted } else { style };
            put(buf, area, x, y, symbol, style);
            x += 1;
        }
        for ch in label.chars() {
            let mut utf8 = [0u8; 4];
            put(buf, area, x, y, ch.encode_utf8(&mut utf8), p.muted);
            x += 1;
        }
        let name = self.step_name();
        let name_x = i32::from(area.x) + (i32::from(area.width) - name.width() as i32).max(0) / 2;
        for (i, ch) in name.chars().enumerate() {
            let mut utf8 = [0u8; 4];
            put(
                buf,
                area,
                name_x + i as i32,
                y + 1,
                ch.encode_utf8(&mut utf8),
                p.muted,
            );
        }
    }

    /// Wipe over a finished chat frame: cells right of the curtain still show
    /// the splash. Does nothing outside the curtain phase, so the last frame is
    /// exactly the chat buffer.
    pub fn overlay(&self, chat: &mut Buffer, now: Instant, p: &Palette) {
        let area = chat.area;
        let frame = self.frame(now);
        if self.phase(frame, area.width) != Phase::Curtain {
            return;
        }
        let Some(col) = self.curtain_col(frame) else {
            return;
        };
        let mut scratch = Buffer::empty(area);
        scratch.set_style(area, p.ink);
        self.render(&mut scratch, area, frame, p);
        for y in area.y..area.bottom() {
            for x in area.x..area.right() {
                if u32::from(x - area.x) >= col {
                    if let (Some(to), Some(from)) = (chat.cell_mut((x, y)), scratch.cell((x, y))) {
                        *to = from.clone();
                    }
                }
            }
        }
        let glyphs = if p.ascii {
            ["#", "=", "-"]
        } else {
            ["▓", "▒", "░"]
        };
        // The curtain is a full-height wipe.
        for y in area.y..area.bottom() {
            for (i, glyph) in glyphs.iter().enumerate() {
                let x = i32::from(area.x) + col as i32 - CURTAIN_WIDTH as i32 + i as i32;
                put(chat, area, x, i32::from(y), glyph, p.ink);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::style::Modifier;

    fn at(start: Instant, frame: u32) -> Instant {
        start + FRAME * frame
    }

    fn text(buf: &Buffer, y: u16) -> String {
        (0..buf.area.width).map(|x| buf[(x, y)].symbol()).collect()
    }

    fn rendered(s: &Splash, frame: u32, w: u16, h: u16) -> Buffer {
        let area = Rect::new(0, 0, w, h);
        let mut buf = Buffer::empty(area);
        let p = Palette {
            logo: Style::default(),
            ink: Style::default(),
            dim: Style::default(),
            muted: Style::default(),
            border: Style::default(),
            peak: Style::default(),
            ascii: false,
        };
        s.render(&mut buf, area, frame, &p);
        buf
    }

    #[test]
    fn bar_follows_milestones_not_the_clock() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        assert_eq!(s.bar(0), 0.0);
        assert_eq!(s.bar(500), 0.0, "time alone never moves the bar");
        s.reach(1, t0);
        assert_eq!(s.bar(0), 0.0);
        assert_eq!(s.bar(FILL_FRAMES), 0.2);
        s.reach(3, t0);
        assert_eq!(s.bar(FILL_FRAMES), 0.6);
        s.reach(5, at(t0, FILL_FRAMES));
        assert_eq!(
            s.bar(FILL_FRAMES),
            0.6,
            "the last step fills from its own frame"
        );
        assert_eq!(s.bar(FILL_FRAMES * 2), 1.0);
    }

    #[test]
    fn particles_and_background_do_not_read_the_milestones() {
        let t0 = Instant::now();
        let mut one = Splash::new(t0, true);
        one.reach(1, t0);
        let mut four = Splash::new(t0, true);
        four.reach(4, t0);
        // Outside the bar rows the two frames are identical: the logo and the
        // ambient field are a function of the clock alone.
        let a = rendered(&one, 30, 80, 24);
        let b = rendered(&four, 30, 80, 24);
        for y in 0..24 {
            if y == 14 || y == 15 {
                continue; // bar and step name
            }
            assert_eq!(text(&a, y), text(&b, y), "row {y}");
        }
    }

    #[test]
    fn reduced_motion_is_static_and_cuts_when_ready() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, false);
        s.reach(1, t0);
        assert_eq!(s.progress(0), 1.0);
        assert_eq!(s.bar(0), 0.2);
        assert_eq!(s.phase(0, 80), Phase::Loading);
        s.reach(5, t0);
        assert_eq!(s.phase(0, 80), Phase::Done);
        assert_eq!(s.bar(0), 1.0);
    }

    #[test]
    fn dismissed_splash_is_done_immediately() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.dismiss();
        assert_eq!(s.phase(0, 80), Phase::Done);
        assert!(!s.covers(t0, 80));
    }

    #[test]
    fn renders_at_any_size_without_panicking() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, t0);
        for (w, h) in [(1, 1), (5, 2), (20, 6), (35, 9), (80, 24), (200, 60)] {
            for frame in [0, 10, 60, 200] {
                let _ = rendered(&s, frame, w, h);
            }
        }
    }

    #[test]
    fn the_logo_is_white_ink_and_bold_at_rest() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(1, t0);
        let area = Rect::new(0, 0, 80, 24);
        let mut buf = Buffer::empty(area);
        let ink = Style::default().fg(ratatui::style::Color::Rgb(255, 255, 255));
        let p = Palette {
            logo: ink.add_modifier(Modifier::BOLD),
            ink,
            dim: Style::default(),
            muted: Style::default(),
            border: Style::default(),
            peak: ink.add_modifier(Modifier::BOLD),
            ascii: false,
        };
        s.render(&mut buf, area, 200, &p);
        let all = (0..24)
            .map(|y| text(&buf, y))
            .collect::<Vec<_>>()
            .join("\n");
        assert!(all.contains("_/"), "{all}");
        let (x, y) = (0..80)
            .flat_map(|x| (0..24).map(move |y| (x, y)))
            .find(|(x, y)| buf[(*x, *y)].symbol() == "_")
            .expect("a logo stroke");
        assert_eq!(buf[(x, y)].fg, ratatui::style::Color::Rgb(255, 255, 255));
        assert!(buf[(x, y)].modifier.contains(Modifier::BOLD));
    }
}
