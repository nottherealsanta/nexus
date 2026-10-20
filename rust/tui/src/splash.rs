//! Splash: the launch sequence shown until the first chat frame (design overhaul step 12,
//! plate P-SPL).
//!
//! 1. **Two independent layers while loading.**
//!    - *Background and wordmark* are a pure function of the clock: a field of ambient
//!      particles drifts and twinkles for as long as the splash is up, and the wordmark's
//!      particles (one per non-blank cell) ease from pseudo-random cells to their places over
//!      the first `MIN_FRAMES`. Neither reads a milestone, so a stalled start never freezes
//!      the screen.
//!    - *The bar* (30 segments centred, the percentage on their right, the step name centred
//!      on the row below) follows **milestones only**: the k-th
//!      of five fills it to k/5. Time never moves it.
//!
//!    There is no maximum duration: a slow start keeps animating and waits.
//! 2. **Flash.** Once `ready` is reached (and the bar and intro are complete) the wordmark
//!    flashes once (160 ms).
//! 3. **Curtain.** A `▓▒░` wipe moves left to right at 500 cells per second from the splash
//!    into the first chat frame (`overlay`). Input is held only while the chat is not on
//!    screen (loading and flash); it reaches the composer during the wipe.
//!
//! The state machine is a pure function of "frames", which are 8 ms ticks since it was
//! created (continuous enough that motion is not stepped; the draw loop paces redraws at
//! 60 fps), so tests drive it with a fake clock. The first milestones are *client-side*; the
//! launcher also sends `{"startup":N}` lines (N up to 3, see `startup_step`) before the first
//! snapshot (see `STEPS`, and docs/ratatui-parity.md). The palette is dark blue (`INK`,
//! `DIM`), readable on the dark terminal; it does not use the app's orange accent. With
//! animations off the wordmark and bar are drawn in their final state and the chat follows
//! the first snapshot with a straight cut.
use crate::render::Palette;
use nexus_widgets::{theme::mix, Glyphs};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    Frame,
};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

/// The splash clock tick (125 Hz); redraws are paced separately at 60 fps.
pub const FRAME: Duration = Duration::from_millis(8);
/// The full NEXUS wordmark (also the particles' targets): every non-blank cell is a particle.
pub const WORDMARK: [&str; 8] = [
    "888b      88  88888888888  8b        d8  88        88   ad88888ba",
    "8888b     88  88            Y8,    ,8P   88        88  d8\"     \"8b",
    "88 `8b    88  88             `8b  d8'    88        88  Y8,",
    "88  `8b   88  88aaaaa          Y88P      88        88  `Y8aaaaa,",
    "88   `8b  88  88\"\"\"\"\"          d88b      88        88    `\"\"\"\"\"8b,",
    "88    `8b 88  88             ,8P  Y8,    88        88          `8b",
    "88     `8888  88            d8'    `8b   Y8a.    .a8P  Y8a     a8P",
    "88      `888  88888888888  8P        Y8   `\"Y8888Y\"'    \"Y88888P\"",
];
/// The small half-block wordmark, used when the screen is too small for `WORDMARK`.
pub const WORDMARK_SMALL: [&str; 2] = ["█▄ █ █▀▀ ▀▄▀ █ █ █▀", "█ ▀█ ██▄ █ █ █▄█ ▄█"];

/// Display width of the widest row.
fn mark_width(mark: &[&str]) -> i32 {
    mark.iter().map(|row| row.width()).max().unwrap_or(0) as i32
}

/// The full wordmark when it fits `area` (its width, plus the blank row, bar and step name
/// below it); otherwise the small one, which is clipped rather than panicking on tiny screens.
fn wordmark_for(area: Rect) -> &'static [&'static str] {
    if i32::from(area.width) >= mark_width(&WORDMARK)
        && i32::from(area.height) >= WORDMARK.len() as i32 + 3
    {
        &WORDMARK
    } else {
        &WORDMARK_SMALL
    }
}
/// Milestone names, in order. Reaching step `n` (1-based) marks the first `n` as done.
pub const STEPS: [&str; 5] = ["daemon", "workspace", "providers", "session", "ready"];
const BAR_SEGMENTS: usize = 30;
/// Frames the bar takes to fill one fifth (200 ms).
const FILL_FRAMES: u32 = 25;
/// Frames the wordmark takes to assemble, on the clock alone (480 ms); also the least time
/// before the flash.
const MIN_FRAMES: u32 = 60;
const FLASH_FRAMES: u32 = 20;
const CURTAIN_STEP: u32 = 4;
const CURTAIN_WIDTH: u32 = 3;
/// Share of the intro range over which particle start times are staggered.
const STAGGER: f32 = 0.6;
/// Frames per cell step of the scanning pulse on the unfilled bar while waiting.
const SCAN_FRAMES: u32 = 6;
/// Sub-cell steps per cell of the ambient field's position (so slow drifts still move).
const AMBIENT_SUBCELL: i64 = 48;
/// Dark blue ink: wordmark, filled bar, curtain. About 4.7:1 against the dark background.
pub const INK: Color = Color::Rgb(74, 126, 224);
/// Dim blue for the ambient field and the in-flight intro particles' start colour (~1.8:1).
pub const DIM: Color = Color::Rgb(38, 64, 122);

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

    /// Wordmark assembly, 0..=1: a function of the clock only. It never reads a milestone,
    /// so the bar stalling cannot freeze it (the bug the first versions had at 40%).
    pub fn progress(&self, frame: u32) -> f32 {
        if !self.animate {
            return 1.0;
        }
        (frame as f32 / MIN_FRAMES as f32).min(1.0)
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

    /// Still waiting on the milestones (animated, `ready` not reached): the scan pulse runs
    /// on the unfilled track. This is colour only; the percentage never moves for it.
    fn waiting(&self) -> bool {
        self.animate && self.marks[4].is_none()
    }

    /// Cell of ambient particle `index` in an `w` x `h` area at `frame`, relative to the
    /// area. It drifts at a fixed per-particle velocity (never zero) and wraps at the edges,
    /// so it depends on the clock alone: no milestone, no state, never still.
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

    /// The ambient field: dim dots drifting across `area`, skipping `keep_clear` so the
    /// wordmark and bar stay readable. Cells it paints are exactly those `ambient_cell` gives.
    fn ambient(
        &self,
        buf: &mut Buffer,
        area: Rect,
        frame: u32,
        keep_clear: Rect,
        ascii: bool,
        bg: Style,
    ) {
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
            let bright = (u64::from(frame / 60) + hash(index + 1000) % 3) % 3 == 0;
            let (glyph, color) = match (bright, ascii) {
                (true, false) => ("•", mix(DIM, INK, 0.5)),
                (false, false) => ("·", DIM),
                (true, true) => ("o", mix(DIM, INK, 0.5)),
                (false, true) => (".", DIM),
            };
            put(buf, area, x, y, glyph, bg.fg(color));
        }
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
        // Wordmark, a blank row, the bar row, then the step name.
        let mark = wordmark_for(area);
        let height = mark.len() as i32 + 3;
        let top = i32::from(area.y) + (i32::from(area.height) - height).max(0) / 2;
        let mark_width = mark_width(mark);
        let left = i32::from(area.x) + (i32::from(area.width) - mark_width).max(0) / 2;

        let mut targets: Vec<(i32, i32, &str)> = Vec::new();
        for (row, text) in mark.iter().copied().enumerate() {
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
        // Layer 1, clock only: the ambient field behind everything, clear of the text block.
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
            self.ambient(buf, area, frame, keep_clear, ascii, bg);
        }
        // Wordmark particles still in flight first, so settled cells are never covered by one.
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
            let x = sx + ((*tx - sx) as f32 * e).round() as i32;
            let y = sy + ((*ty - sy) as f32 * e).round() as i32;
            let glyph = match (t < 0.5, ascii) {
                (true, false) => "·",
                (false, false) => "•",
                (true, true) => ".",
                (false, true) => "o",
            };
            put(buf, area, x, y, glyph, bg.fg(mix(DIM, INK, t)));
        }
        let ink = mix(INK, p.text, flash);
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

        // The bar: the segments are centred on their own, the percentage sits on their right
        // and the current step name is centred on the row below.
        let pct = (self.bar(frame) * 100.0).round() as u32;
        let label = format!(" {pct:>3}%");
        let room = i32::from(area.width) - label.width() as i32;
        let segments = (BAR_SEGMENTS as i32).min(room.max(0)) as usize;
        let filled = (self.bar(frame) * segments as f32).round() as usize;
        // Centred; on a screen too narrow for the percentage beside it, shift left to fit.
        let centred = (i32::from(area.width) - segments as i32).max(0) / 2;
        let shifted = (room - segments as i32).max(0);
        let mut x = i32::from(area.x) + centred.min(shifted);
        let y = top + mark.len() as i32 + 1;
        // Waiting: a pulse sweeps the unfilled track (colour only, the fill is unchanged).
        let scan = (self.waiting() && segments > filled)
            .then(|| filled + (frame / SCAN_FRAMES) as usize % (segments - filled));
        for i in 0..segments {
            let (symbol, color) = match (i < filled, ascii) {
                (true, false) => ("▰", INK),
                (false, false) => ("▱", p.border_strong),
                (true, true) => ("#", INK),
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
                bg.fg(p.muted),
            );
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
                let style = Style::default().fg(INK).bg(p.background);
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

    /// A wordmark row without the ambient dots that may sit beside it.
    fn mark_text(buf: &Buffer, y: u16) -> String {
        text(buf, y).replace(['·', '•'], " ").trim().to_string()
    }

    fn bar_row(buf: &Buffer) -> u16 {
        (0..buf.area.height)
            .find(|&y| text(buf, y).contains('%'))
            .unwrap()
    }

    fn luminance(color: Color) -> f32 {
        let Color::Rgb(r, g, b) = color else {
            panic!("{color:?} is not rgb")
        };
        let lin = |c: u8| {
            let c = f32::from(c) / 255.0;
            if c <= 0.04045 {
                c / 12.92
            } else {
                ((c + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
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
        assert_eq!(
            startup_step("{\"startup\":5}"),
            Some(3),
            "never claims ready"
        );
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
        assert_eq!(
            s.phase(10_000, 80),
            Phase::Loading,
            "ready still needs a snapshot"
        );
    }

    #[test]
    fn bar_text_shows_percentage_and_step() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        let buf = rendered(&s, 60, 80, 12);
        let row = text(&buf, bar_row(&buf));
        assert!(row.contains("40%"), "{row:?}");
        assert!(
            !row.contains("providers"),
            "the name is not beside the bar: {row:?}"
        );
        assert_eq!(row.matches('▰').count(), 12, "{row:?}");
        assert_eq!(row.matches('▱').count(), 18, "{row:?}");
    }

    /// Columns of the first and last bar segment and of the `%` on the bar row.
    fn bar_span(buf: &Buffer, y: u16) -> (u16, u16, u16) {
        let xs = |f: &dyn Fn(&str) -> bool| -> Vec<u16> {
            (0..buf.area.width)
                .filter(|&x| f(buf[(x, y)].symbol()))
                .collect()
        };
        let seg = xs(&|s| matches!(s, "▰" | "▱" | "#" | "-"));
        let pct = xs(&|s| s == "%");
        (seg[0], *seg.last().unwrap(), pct[0])
    }

    #[test]
    fn bar_is_centred_with_the_percentage_right_and_the_name_centred_below() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        for w in [80u16, 81, 120] {
            let buf = rendered(&s, 60, w, 14);
            let y = bar_row(&buf);
            let (first, last, pct) = bar_span(&buf, y);
            assert_eq!(last - first + 1, 30);
            assert!(
                first.abs_diff(w - 1 - last) <= 1,
                "bar centred at width {w}"
            );
            assert!(pct > last, "percentage right of the bar at width {w}");
            let row = text(&buf, y);
            assert!(
                !row.chars().any(|c| c.is_alphabetic()),
                "no name on the bar row: {row:?}"
            );
            let below = text(&buf, y + 1).replace(['·', '•'], " ");
            let name = below.trim();
            assert!(name == "providers", "{below:?}");
            let start = below.find("providers").unwrap() as u16;
            assert!(
                start.abs_diff(w - (start + 9)) <= 1,
                "name centred at width {w}: {below:?}"
            );
            let above = text(&buf, y - 1).replace(['·', '•'], " ");
            assert!(
                above.trim().is_empty(),
                "blank row above the bar: {above:?}"
            );
        }
    }

    #[test]
    fn narrow_screens_keep_bar_percentage_and_name_visible_on_separate_rows() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        for w in [20u16, 24, 30, 35, 40] {
            let buf = rendered(&s, 60, w, 10);
            let y = bar_row(&buf);
            let row = text(&buf, y);
            assert!(row.contains("40%"), "w={w} {row:?}");
            let (first, last, pct) = bar_span(&buf, y);
            assert!(pct > last && last >= first, "w={w}");
            assert!(text(&buf, y + 1).contains("providers"), "w={w}");
        }
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
        // The art is drawn exactly, one row per line, at a fixed left edge.
        let rows: Vec<String> = (0..12).map(|y| text(&settled, y)).collect();
        let first = rows
            .iter()
            .position(|r| r.trim().replace(['·', '•'], " ").trim() == WORDMARK[0])
            .expect("first art row");
        let left = rows[first]
            .find(WORDMARK[0].chars().next().unwrap())
            .unwrap();
        for (i, art) in WORDMARK.iter().enumerate() {
            let row: String = rows[first + i]
                .chars()
                .skip(left)
                .take(art.chars().count())
                .collect();
            assert_eq!(&row, art, "art row {i}");
        }
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
    fn a_slow_start_waits_with_the_wordmark_assembled_and_the_bar_unfinished() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        assert_eq!(s.progress(1000), 1.0, "assembly runs on the clock alone");
        assert!(
            (s.bar(1000) - 0.4).abs() < 1e-6,
            "no maximum duration: the bar waits for the milestones"
        );
        assert_eq!(s.phase(1000, 80), Phase::Loading);
        assert!(s.covers(at(t0, 1000), 80));
    }

    #[test]
    fn assembly_and_background_do_not_read_the_milestones() {
        // The 40% freeze: the particles used to follow the bar, so a stalled bar stopped them.
        let t0 = Instant::now();
        let none = Splash::new(t0, true);
        let mut four = Splash::new(t0, true);
        four.reach(4, at(t0, 0));
        for frame in [0u32, 7, 30, 59, 60, 300, 4000] {
            assert_eq!(none.progress(frame), four.progress(frame), "frame {frame}");
        }
        for frame in [3u32, 30, 59, 300, 4000] {
            let (a, b) = (
                rendered(&none, frame, 120, 30),
                rendered(&four, frame, 120, 30),
            );
            let bar = bar_row(&b);
            assert_ne!(
                text(&a, bar),
                text(&b, bar),
                "only the bar differs ({frame})"
            );
            for y in (0..30).filter(|&y| y != bar && y != bar + 1) {
                assert_eq!(text(&a, y), text(&b, y), "row {y} at frame {frame}");
                for x in 0..120 {
                    assert_eq!(a[(x, y)].fg, b[(x, y)].fg, "colour ({x},{y}) at {frame}");
                }
            }
        }
    }

    #[test]
    fn stalled_at_forty_percent_the_background_never_stands_still() {
        // A restart that sits at 40% (host connected, session not bootstrapped) for ten
        // seconds: every 384 ms window must differ, and the bar must not move.
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        let bar_of = |frame: u32| {
            let buf = rendered(&s, frame, 120, 30);
            // Ambient dots may drift along the row beside the bar; only the bar's own text
            // must hold still.
            let bar = bar_row(&buf);
            format!("{}|{}", text(&buf, bar), text(&buf, bar + 1))
                .replace(['▰', '▱', '·', '•'], " ")
        };
        let mut frame = 100u32;
        while frame < 100 + 1250 {
            let (a, b) = (
                rendered(&s, frame, 120, 30),
                rendered(&s, frame + 48, 120, 30),
            );
            let bar = bar_row(&a);
            let rows = |buf: &Buffer| {
                (0..30u16)
                    .filter(|&y| y != bar && y != bar + 1)
                    .map(|y| text(buf, y))
                    .collect::<Vec<_>>()
            };
            assert_ne!(rows(&a), rows(&b), "background froze at frame {frame}");
            assert_eq!(bar_of(frame), bar_of(frame + 48), "text unchanged");
            assert!(bar_of(frame).contains(" 40%") && bar_of(frame).contains("providers"));
            assert_eq!(a.content.iter().filter(|c| c.symbol() == "▰").count(), 12);
            frame += 48;
        }
    }

    #[test]
    fn ambient_particles_stay_inside_the_area_and_always_move() {
        for (w, h) in [(80u16, 12u16), (120, 30), (7, 3), (1, 1)] {
            for index in 0..96 {
                for frame in [0u32, 1, 500, 100_000, u32::MAX / 2] {
                    let (x, y) = Splash::ambient_cell(index, frame, w, h);
                    assert!((0..i32::from(w)).contains(&x) && (0..i32::from(h)).contains(&y));
                }
            }
        }
        for index in 0..96 {
            for frame in [0u32, 333, 9000] {
                assert_ne!(
                    Splash::ambient_cell(index, frame, 80, 24),
                    Splash::ambient_cell(index, frame + 48, 80, 24),
                    "particle {index} still at {frame}"
                );
            }
        }
    }

    #[test]
    fn the_palette_is_dark_blue_and_readable_on_the_dark_background() {
        let p = Palette::new(false);
        let Color::Rgb(r, g, b) = INK else { panic!() };
        assert!(b > r && b > g && b >= 180, "INK is blue");
        let ratio = |c| (luminance(c) + 0.05) / (luminance(p.background) + 0.05);
        assert!(
            ratio(INK) >= 4.5,
            "wordmark and bar readable: {}",
            ratio(INK)
        );
        assert!(ratio(DIM) >= 1.5, "ambient visible: {}", ratio(DIM));
        assert!(ratio(DIM) < ratio(INK), "ambient stays behind the ink");
        // No cell of any phase uses an orange (red above blue) foreground.
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let done = s.complete_frame().unwrap();
        for frame in [1u32, 20, done - 1, done + 10, done + FLASH_FRAMES] {
            let buf = rendered(&s, frame, 100, 20);
            for cell in &buf.content {
                if let Color::Rgb(r, _, b) = cell.fg {
                    assert!(r <= b, "orange-ish fg {:?} at frame {frame}", cell.fg);
                }
            }
        }
        let mut chat = Buffer::empty(Rect::new(0, 0, 100, 20));
        s.overlay(&mut chat, at(t0, done + FLASH_FRAMES + 5), &p, false);
        assert!(chat
            .content
            .iter()
            .all(|c| !matches!(c.fg, Color::Rgb(r, _, b) if r > b)));
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
            let y = bar_row(buf);
            let row = text(buf, y);
            let bar_start = row.find(|c: char| c == '▰' || c == '▱').unwrap();
            let pct_end = row.find('%').unwrap() + 1;
            let name = text(buf, y + 1).replace(['·', '•'], " ").trim().to_string();
            format!(
                "{}:{}:{}",
                row.matches('▰').count(),
                &row[bar_start..pct_end],
                name
            )
        };
        assert_eq!(pct(&a), pct(&b), "same fill, same percentage and step");
        assert!(pct(&a).contains("20%") && pct(&a).contains("workspace"));
        assert!((s.bar(430) - 0.2).abs() < 1e-6);
        // Once ready the bar is full and stays so while the background keeps moving.
        s.reach(5, at(t0, 500));
        assert!((s.bar(600) - 1.0).abs() < 1e-6);
        assert_ne!(rendered(&s, 600, 120, 30), rendered(&s, 648, 120, 30));
    }

    #[test]
    fn background_keeps_moving_at_every_stalled_percentage() {
        // Whole-frame check that the rows above the bar (not just the bar pulse) keep
        // changing at each stalled milestone.
        let t0 = Instant::now();
        for step in 1..=4usize {
            let mut s = Splash::new(t0, true);
            s.reach(step, at(t0, 0));
            let particles = |frame: u32| {
                let buf = rendered(&s, frame, 120, 30);
                let bar = bar_row(&buf);
                (0..bar).map(|y| text(&buf, y)).collect::<Vec<_>>()
            };
            let frames: Vec<_> = (0..8).map(|i| particles(1000 + i * 40)).collect();
            let distinct = frames.iter().filter(|f| **f != frames[0]).count();
            assert!(
                distinct >= 4,
                "step {step}: particles froze ({distinct} differ)"
            );
        }
    }

    #[test]
    fn ambient_field_keeps_clear_of_the_text_and_is_off_without_animation() {
        let t0 = Instant::now();
        let s = Splash::new(t0, true);
        let dots = |buf: &Buffer, rows: std::ops::Range<u16>| {
            rows.map(|y| text(buf, y).matches(['·', '•']).count())
                .sum::<usize>()
        };
        let buf = rendered(&s, 500, 120, 30);
        assert!(dots(&buf, 0..30) > 10, "the field is drawn");
        let bar = bar_row(&buf);
        // The field may drift past the sides of the text block, never through it.
        let row: Vec<char> = text(&buf, bar).chars().collect();
        let first = row.iter().position(|c| matches!(c, '▰' | '▱')).unwrap();
        let last = row.iter().rposition(|c| *c == '%').unwrap();
        assert!(
            row[first..=last].iter().all(|c| !matches!(c, '·' | '•')),
            "bar span is clear"
        );
        let still = Splash::new(t0, false);
        assert_eq!(dots(&rendered(&still, 500, 120, 30), 0..30), 0);
        assert_eq!(
            rendered(&still, 500, 120, 30),
            rendered(&still, 900, 120, 30)
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
        assert!(
            FRAME * end < Duration::from_millis(1100),
            "{:?}",
            FRAME * end
        );
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
        assert!(a > 0 && a.abs_diff(b) <= 8, "{a} vs {b}");
    }

    #[test]
    fn flash_brightens_the_wordmark_once() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(5, at(t0, 0));
        let done = s.complete_frame().unwrap();
        let ink = |frame: u32| {
            let buf = rendered(&s, frame, 80, 12);
            (0..12u16)
                .flat_map(|y| (0..80u16).map(move |x| (x, y)))
                .find(|&(x, y)| buf[(x, y)].modifier.contains(Modifier::BOLD))
                .map(|(x, y)| buf[(x, y)].fg)
                .unwrap()
        };
        assert_eq!(ink(done - 1), INK);
        assert_ne!(
            ink(done + FLASH_FRAMES / 2),
            INK,
            "mid-flash is blended toward the peak"
        );
        assert_eq!(ink(done + FLASH_FRAMES), INK, "settles back");
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
    fn the_art_is_the_full_nexus_block_and_small_screens_get_the_small_mark() {
        assert_eq!(WORDMARK.len(), 8);
        let wide = mark_width(&WORDMARK);
        assert_eq!(wide, 66, "widest art row");
        let t0 = Instant::now();
        let mut s = Splash::new(t0, false);
        s.reach(2, at(t0, 0));
        let fits = |w: u16, h: u16| {
            let buf = rendered(&s, 0, w, h);
            (0..h).any(|y| text(&buf, y).contains(WORDMARK[0]))
        };
        assert!(fits(wide as u16, 11), "exact fit uses the full art");
        assert!(!fits(wide as u16 - 1, 11), "one column short falls back");
        assert!(!fits(120, 10), "one row short falls back");
        let small = rendered(&s, 0, 40, 10);
        assert!((0..10u16).any(|y| text(&small, y).contains(WORDMARK_SMALL[0])));
        assert!((0..10u16).any(|y| text(&small, y).contains("40%")));
    }

    #[test]
    fn renders_at_any_size_without_panicking() {
        let t0 = Instant::now();
        let mut s = Splash::new(t0, true);
        s.reach(2, at(t0, 0));
        for (w, h) in [
            (65u16, 11u16),
            (66, 11),
            (66, 10),
            (67, 12),
            (100, 11),
            (200, 60),
        ] {
            for frame in [0u32, 10, 80] {
                let _ = rendered(&s, frame, w, h);
                let _ = rendered_as(&s, frame, w, h, true);
            }
        }
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
