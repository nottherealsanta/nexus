//! The Manager's header indicator: a field of braille cells with a few dots lit, travelled by
//! several slow sine waves (docs/manager.md).
//!
//! The old orb's state machine is kept: presence, the measured level and the eased change from one
//! state to the next. The drawing is now text. Idle, thinking, listening and speaking travel at
//! different speeds and brightnesses, all in grays. ASCII terminals keep one glyph per state
//! (`·`, `○`, `◉`); reduced motion freezes the field at its first frame.

use std::time::Instant;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};

use crate::theme::Theme;

/// The cells the indicator covers in the header: ten cells, one row.
pub const WIDTH: u16 = 10;
pub const HEIGHT: u16 = 1;

/// Enter and exit, from the header edge (fade).
const ENTER_SECS: f32 = 0.24;
const EXIT_SECS: f32 = 0.5;
/// State changes ease over about 200 ms.
const MIX_SECS: f32 = 0.2;
/// Fast attack, slow release.
const ATTACK_SECS: f32 = 0.06;
const RELEASE_SECS: f32 = 0.24;
/// One wave: vertical centre in dot rows, amplitude, frequency, drift, phase and how wide
/// (in dot rows) its line is. Three waves at different speeds never repeat.
const WAVES: [(f32, f32, f32, f32, f32, f32); 3] = [
    (1.05, 0.95, 0.55, 1.0, 0.0, 1.0),
    (2.35, 0.80, 0.43, -0.8, 1.9, 1.0),
    (1.70, 0.55, 0.80, 1.3, 3.7, 0.5),
];
/// Braille dot bits, [row][column].
const BITS: [[u8; 2]; 4] = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IndicatorState {
    Listening,
    Thinking,
    Speaking,
    /// The exchange is over: fades out in `manager.idle`.
    Idle,
}

impl IndicatorState {
    /// The word the caption uses where the indicator cannot say it (ASCII tiers).
    pub fn word(self) -> Option<&'static str> {
        match self {
            IndicatorState::Listening => Some("Listening"),
            IndicatorState::Thinking => Some("Thinking"),
            _ => None,
        }
    }

    /// How fast the waves travel, in radians of drift per second.
    fn speed(self) -> f32 {
        match self {
            IndicatorState::Idle => 1.1,
            IndicatorState::Thinking => 2.6,
            IndicatorState::Listening => 3.4,
            IndicatorState::Speaking => 5.2,
        }
    }

    /// How bright the dots are, 0 to 1. Every state stays gray.
    fn brightness(self) -> f32 {
        match self {
            IndicatorState::Idle => 0.55,
            IndicatorState::Thinking => 0.68,
            IndicatorState::Listening => 0.84,
            IndicatorState::Speaking => 1.0,
        }
    }
}

/// The animation state of the indicator: how present it is, how loud, which state
/// it is easing between, and the time its waves travel on.
#[derive(Debug, Clone)]
pub struct Indicator {
    pub presence: f32,
    pub level: f32,
    pub from: IndicatorState,
    pub to: IndicatorState,
    pub mix: f32,
    pub t: f32,
    last: Option<Instant>,
}

impl Default for Indicator {
    fn default() -> Self {
        Self {
            presence: 0.0,
            level: 0.0,
            from: IndicatorState::Idle,
            to: IndicatorState::Idle,
            mix: 1.0,
            t: 0.0,
            last: None,
        }
    }
}

fn ease(x: f32) -> f32 {
    let x = x.clamp(0.0, 1.0);
    x * x * (3.0 - 2.0 * x)
}

impl Indicator {
    /// Anything of it left to draw?
    pub fn visible(&self) -> bool {
        self.presence > 0.003
    }

    /// Still moving toward `target`? (The frame clock runs only then.)
    pub fn moving(&self, target: Option<IndicatorState>) -> bool {
        match target {
            // A call left open in the background is still: the header shows a
            // still field, and no frame clock stays awake for it.
            Some(IndicatorState::Idle) => {
                self.visible()
                    && (self.to != IndicatorState::Idle
                        || self.mix < 1.0
                        || self.presence < 1.0
                        || self.level > 0.01)
            }
            Some(_) => true,
            None => self.visible(),
        }
    }

    /// Freeze the indicator at a state and level: `--dump-frame` and tests use this
    /// so a frame does not depend on the clock.
    pub fn pin(&mut self, state: IndicatorState, level: f32) {
        self.presence = 1.0;
        self.level = level.clamp(0.0, 1.0);
        self.from = state;
        self.to = state;
        self.mix = 1.0;
        self.t = 3.1;
        self.last = None;
    }

    /// Move the animation on to `now`. `target` is the state to show, `None`
    /// when the exchange is over; `raw` is the measured level.
    pub fn advance(
        &mut self,
        now: Instant,
        target: Option<IndicatorState>,
        raw: f32,
        reduced: bool,
    ) {
        let dt = self
            .last
            .map_or(0.0, |last| {
                now.saturating_duration_since(last).as_secs_f32()
            })
            .min(0.1);
        self.last = Some(now);
        if reduced {
            // No motion: it is simply there or not, still, at a fixed brightness.
            self.presence = if target.is_some() { 1.0 } else { 0.0 };
            if let Some(state) = target {
                self.from = state;
                self.to = state;
                self.mix = 1.0;
            }
            self.level = match target {
                Some(IndicatorState::Listening | IndicatorState::Speaking) => 0.5,
                _ => 0.3,
            };
            self.t = 0.0;
            return;
        }
        match target {
            Some(state) => {
                if state != self.to {
                    self.from = self.to;
                    self.to = state;
                    self.mix = 0.0;
                }
                self.presence = (self.presence + dt / ENTER_SECS).min(1.0);
            }
            None => {
                if self.to != IndicatorState::Idle {
                    self.from = self.to;
                    self.to = IndicatorState::Idle;
                    self.mix = 0.0;
                }
                self.presence = (self.presence - dt / EXIT_SECS).max(0.0);
            }
        }
        self.mix = (self.mix + dt / MIX_SECS).min(1.0);
        let tau = if raw > self.level {
            ATTACK_SECS
        } else {
            RELEASE_SECS
        };
        let goal = if target.is_some() { raw } else { 0.0 };
        let k = 1.0 - (-dt / tau).exp();
        self.level += (goal - self.level) * k;
        self.t += dt * (1.0 + 1.4 * self.level);
    }
}

fn rgb(c: Color) -> Option<[f32; 3]> {
    match c {
        Color::Rgb(r, g, b) => Some([f32::from(r), f32::from(g), f32::from(b)]),
        _ => None,
    }
}

fn mix(a: [f32; 3], b: [f32; 3], k: f32) -> [f32; 3] {
    [
        a[0] + (b[0] - a[0]) * k,
        a[1] + (b[1] - a[1]) * k,
        a[2] + (b[2] - a[2]) * k,
    ]
}

fn color(c: [f32; 3]) -> Color {
    Color::Rgb(
        c[0].round().clamp(0.0, 255.0) as u8,
        c[1].round().clamp(0.0, 255.0) as u8,
        c[2].round().clamp(0.0, 255.0) as u8,
    )
}

/// Blend `top` over `bottom` by `alpha` (cells carry true colour only).
pub fn blend(bottom: Color, top: Color, alpha: f32) -> Color {
    match (rgb(bottom), rgb(top)) {
        (Some(b), Some(t)) => color(mix(b, t, alpha.clamp(0.0, 1.0))),
        _ if alpha >= 0.5 => top,
        _ => bottom,
    }
}

/// The gray the waves are drawn in right now: `text.muted` at the state's brightness.
fn wave_color(theme: &Theme, indicator: &Indicator) -> Option<[f32; 3]> {
    let muted = theme.color("text.muted").and_then(rgb)?;
    let canvas = theme
        .color("surface.canvas")
        .and_then(rgb)
        .unwrap_or([11.0; 3]);
    let k = indicator.from.brightness() * (1.0 - ease(indicator.mix))
        + indicator.to.brightness() * ease(indicator.mix);
    Some(mix(canvas, muted, k))
}

/// Where the indicator sits: `x` is its centre column, `bottom` the row just
/// below its only row.
#[derive(Debug, Clone, Copy)]
pub struct Anchor {
    pub x: u16,
    pub bottom: u16,
}

/// The cells the indicator may touch, clipped to `area`.
pub fn bounds(area: Rect, at: Anchor) -> Rect {
    bounds_sized(area, at, WIDTH, HEIGHT)
}

/// The cells an indicator of `width` x `height` may touch, clipped to `area`.
pub fn bounds_sized(area: Rect, at: Anchor, width: u16, height: u16) -> Rect {
    let x = at.x.saturating_sub(width / 2);
    let y = at.bottom.saturating_sub(height);
    Rect::new(x, y, width, height).intersection(area)
}

/// The glyph for a state where the field cannot be drawn (ASCII terminals).
pub fn glyph(state: IndicatorState, ascii: bool) -> &'static str {
    match (state, ascii) {
        (IndicatorState::Idle, false) => "·",
        (IndicatorState::Idle, true) => ".",
        (IndicatorState::Thinking, false) => "○",
        (IndicatorState::Thinking, true) => "o",
        (_, false) => "◉",
        (_, true) => "O",
    }
}

/// The braille cell for dot columns `index * 2` and `index * 2 + 1` at time `t`.
pub fn braille_cell(t: f32, state: IndicatorState, level: f32, index: usize) -> char {
    let time = t * state.speed();
    let amp = 0.78 + 0.22 * level.clamp(0.0, 1.0);
    let mut bits = 0u8;
    for (row, dots) in BITS.iter().enumerate() {
        for (col, bit) in dots.iter().enumerate() {
            let dx = (index * 2 + col) as f32;
            for (centre, a, freq, drift, phase, reach) in WAVES {
                let y = centre + a * amp * (dx * freq - time * drift + phase).sin();
                if (y - row as f32).abs() < 0.42 * reach {
                    bits |= bit;
                    break;
                }
            }
        }
    }
    char::from_u32(0x2800 + u32::from(bits)).unwrap_or(' ')
}

/// Paint the indicator over `buf`. Call after every other layer.
pub fn paint(buf: &mut Buffer, area: Rect, at: Anchor, indicator: &Indicator, theme: &Theme) {
    paint_sized(buf, area, at, WIDTH, HEIGHT, indicator, theme);
}

/// Paint an indicator of `width` x `height` cells over `buf`.
pub fn paint_sized(
    buf: &mut Buffer,
    area: Rect,
    at: Anchor,
    width: u16,
    height: u16,
    indicator: &Indicator,
    theme: &Theme,
) {
    if !indicator.visible() {
        return;
    }
    let state = if indicator.mix < 0.5 {
        indicator.from
    } else {
        indicator.to
    };
    let zone = bounds_sized(area, at, width, height);
    if zone.width == 0 || zone.height == 0 {
        return;
    }
    if theme.opts.ascii {
        paint_glyph(buf, area, at, height, state, theme);
        return;
    }
    let presence = ease(indicator.presence);
    let y = at
        .bottom
        .saturating_sub(1)
        .min(zone.bottom().saturating_sub(1));
    let base = wave_color(theme, indicator);
    // Indexed and mono tiers cannot blend a brightness; they still draw the field,
    // in the state's gray token, with idle dimmed.
    let flat = theme
        .fg(
            if matches!(state, IndicatorState::Listening | IndicatorState::Speaking) {
                "text.secondary"
            } else {
                "text.muted"
            },
        )
        .add_modifier(if matches!(state, IndicatorState::Idle) {
            Modifier::DIM
        } else {
            Modifier::empty()
        });
    for x in zone.x..zone.right() {
        let at_cell = &mut buf[(x, y)];
        let dot = braille_cell(indicator.t, state, indicator.level, usize::from(x - zone.x));
        let style = match base.and_then(|base| {
            rgb(at_cell.bg).map(|bg| {
                Style::default()
                    .fg(color(mix(bg, base, presence)))
                    .bg(color(bg))
            })
        }) {
            Some(style) => style,
            None => flat.bg(at_cell.bg),
        };
        at_cell.set_symbol(&dot.to_string());
        at_cell.set_style(style);
        at_cell.modifier = Modifier::empty();
    }
}

/// One glyph at the indicator's centre, in the state's colour.
fn paint_glyph(
    buf: &mut Buffer,
    area: Rect,
    at: Anchor,
    height: u16,
    state: IndicatorState,
    theme: &Theme,
) {
    let y = at.bottom.saturating_sub(height / 2 + 1);
    if !contains(area, at.x, y) {
        return;
    }
    let style = theme.fg("text.muted");
    let cell = &mut buf[(at.x, y)];
    cell.set_symbol(glyph(state, theme.opts.ascii));
    let bg = cell.bg;
    cell.set_style(style.bg(bg));
}

fn contains(area: Rect, x: u16, y: u16) -> bool {
    x >= area.x && x < area.right() && y >= area.y && y < area.bottom()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::{Options, Theme};

    fn painted(buf: &Buffer) -> Vec<char> {
        buf.content
            .iter()
            .filter_map(|c| c.symbol().chars().next())
            .filter(|c| ('\u{2800}'..='\u{28ff}').contains(c))
            .collect()
    }

    #[test]
    fn a_hidden_indicator_draws_nothing() {
        let theme = Theme::new(Options::default());
        let area = Rect::new(0, 0, 40, 12);
        let mut buf = Buffer::empty(area);
        paint(
            &mut buf,
            area,
            Anchor { x: 20, bottom: 1 },
            &Indicator::default(),
            &theme,
        );
        assert!(painted(&buf).is_empty());
    }

    #[test]
    fn a_pinned_indicator_is_mostly_dark_braille_in_its_box() {
        let theme = Theme::new(Options::default());
        let area = Rect::new(0, 0, 40, 12);
        let mut buf = Buffer::empty(area);
        for cell in buf.content.iter_mut() {
            cell.set_style(
                Style::default()
                    .fg(Color::Rgb(200, 200, 200))
                    .bg(Color::Rgb(11, 11, 11)),
            );
        }
        let mut indicator = Indicator::default();
        indicator.pin(IndicatorState::Listening, 0.6);
        paint(
            &mut buf,
            area,
            Anchor { x: 20, bottom: 1 },
            &indicator,
            &theme,
        );
        let cells = painted(&buf);
        assert_eq!(cells.len(), usize::from(WIDTH), "one braille cell each");
        let lit: u32 = cells
            .iter()
            .map(|c| ((*c as u32) - 0x2800).count_ones())
            .sum();
        assert!(lit > 4, "some dots are lit");
        assert!(
            lit < u32::from(WIDTH) * 6,
            "the fields are not blocks: {lit} of {} dots lit",
            u32::from(WIDTH) * 8
        );
        for x in 0..40u16 {
            if !(15..25).contains(&x) {
                assert_eq!(buf[(x, 0)].symbol(), " ", "outside the box at {x},0");
            }
        }
    }

    #[test]
    fn waves_move_and_speaking_is_faster_than_idle() {
        let a: String = (0..WIDTH as usize)
            .map(|i| braille_cell(0.0, IndicatorState::Idle, 0.3, i))
            .collect();
        let b: String = (0..WIDTH as usize)
            .map(|i| braille_cell(1.0, IndicatorState::Idle, 0.3, i))
            .collect();
        assert_ne!(a, b, "the field travels");
        assert!(
            IndicatorState::Speaking.speed() > IndicatorState::Idle.speed(),
            "the state reads in speed"
        );
        assert!(
            IndicatorState::Speaking.brightness() > IndicatorState::Idle.brightness(),
            "the state reads in brightness"
        );
    }

    #[test]
    fn a_text_cell_under_the_field_keeps_its_glyph() {
        let theme = Theme::new(Options::default());
        let area = Rect::new(0, 0, 40, 12);
        let mut buf = Buffer::empty(area);
        for cell in buf.content.iter_mut() {
            cell.set_symbol("x");
            cell.set_style(
                Style::default()
                    .fg(Color::Rgb(200, 200, 200))
                    .bg(Color::Rgb(11, 11, 11)),
            );
        }
        let mut indicator = Indicator::default();
        indicator.pin(IndicatorState::Speaking, 0.5);
        paint(
            &mut buf,
            area,
            Anchor { x: 20, bottom: 1 },
            &indicator,
            &theme,
        );
        assert_eq!(buf[(0, 0)].symbol(), "x");
        assert_eq!(buf[(30, 0)].symbol(), "x");
    }

    #[test]
    fn exit_takes_the_indicator_away() {
        let mut indicator = Indicator::default();
        indicator.pin(IndicatorState::Speaking, 0.5);
        let mut now = Instant::now();
        indicator.advance(now, None, 0.0, false);
        for _ in 0..30 {
            now += std::time::Duration::from_millis(33);
            indicator.advance(now, None, 0.0, false);
        }
        assert!(!indicator.visible());
    }

    #[test]
    fn indexed_tiers_still_draw_the_braille_field() {
        let theme = Theme::new(Options {
            tier: crate::theme::Tier::Ansi256,
            ..Options::default()
        });
        let area = Rect::new(0, 0, 40, 12);
        let mut buf = Buffer::empty(area);
        let mut indicator = Indicator::default();
        indicator.pin(IndicatorState::Speaking, 0.5);
        paint(
            &mut buf,
            area,
            Anchor { x: 20, bottom: 1 },
            &indicator,
            &theme,
        );
        assert_eq!(painted(&buf).len(), usize::from(WIDTH));
    }

    #[test]
    fn ascii_draws_one_glyph() {
        let theme = Theme::new(Options {
            ascii: true,
            ..Options::default()
        });
        let area = Rect::new(0, 0, 40, 12);
        let mut buf = Buffer::empty(area);
        let mut indicator = Indicator::default();
        indicator.pin(IndicatorState::Thinking, 0.0);
        paint(
            &mut buf,
            area,
            Anchor { x: 20, bottom: 1 },
            &indicator,
            &theme,
        );
        assert!(painted(&buf).is_empty());
        assert!(buf.content.iter().any(|c| c.symbol() == "o"));
    }
}
