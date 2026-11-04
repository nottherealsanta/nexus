//! The Manager's orb (docs/manager-design.md 10.2): a soft luminous sphere drawn with
//! half-block cells. Every subpixel has its own radial falloff and is blended
//! over the colours already in the buffer, so there is no hard edge and text
//! under the fringe survives. Two or three slowly drifting lobes deform the
//! edge with the level. It is painted after every other layer.
//!
//! Lower colour tiers, monochrome and ASCII use one static glyph per state
//! instead (`·`, `○`, `◉`); the caption says the state in words there.

use std::time::Instant;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};

use crate::theme::{Theme, Tier};

/// The cells a minimised orb may cover: 10 wide, 3 high (10 by 6 half-block
/// pixels). A focused call uses `focused_size` instead.
pub const WIDTH: u16 = 10;
pub const HEIGHT: u16 = 3;

/// The cells a focused orb may cover: larger, centred in the stage. It scales
/// with the terminal but stays wide rather than tall, as the glow does.
pub fn focused_size(area: Rect) -> (u16, u16) {
    let width = (area.width / 4).clamp(18, 34);
    let height = (width / 4).clamp(5, 8);
    (width, height)
}

/// Orb enter and exit, from the composer edge (scale and fade).
const ENTER_SECS: f32 = 0.24;
const EXIT_SECS: f32 = 0.5;
/// State changes ease over about 200 ms.
const MIX_SECS: f32 = 0.2;
/// Fast attack, slow release (docs/manager-design.md 10.2).
const ATTACK_SECS: f32 = 0.06;
const RELEASE_SECS: f32 = 0.24;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrbState {
    Listening,
    Thinking,
    Speaking,
    /// The exchange is over: shrinks and fades out in `manager.idle`.
    Idle,
}

impl OrbState {
    pub fn token(self) -> &'static str {
        match self {
            OrbState::Listening => "manager.listening",
            OrbState::Thinking => "manager.thinking",
            OrbState::Speaking => "manager.speaking",
            OrbState::Idle => "manager.idle",
        }
    }

    /// The word the caption uses where the orb cannot say it (reduced motion, glyph tiers).
    pub fn word(self) -> Option<&'static str> {
        match self {
            OrbState::Listening => Some("Listening"),
            OrbState::Thinking => Some("Thinking"),
            _ => None,
        }
    }
}

/// The animation state of the orb: how present it is, how loud, which state
/// it is easing between, and the time its lobes drift on.
#[derive(Debug, Clone)]
pub struct Orb {
    pub presence: f32,
    pub level: f32,
    pub from: OrbState,
    pub to: OrbState,
    pub mix: f32,
    pub t: f32,
    last: Option<Instant>,
}

impl Default for Orb {
    fn default() -> Self {
        Self {
            presence: 0.0,
            level: 0.0,
            from: OrbState::Idle,
            to: OrbState::Idle,
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

impl Orb {
    /// Anything of it left to draw?
    pub fn visible(&self) -> bool {
        self.presence > 0.003
    }

    /// Still moving toward `target`? (The frame clock runs only then.)
    pub fn moving(&self, target: Option<OrbState>) -> bool {
        match target {
            // An idle orb is still once it has arrived: a call open in the
            // background must not keep a frame clock awake.
            Some(OrbState::Idle) => {
                self.visible()
                    && (self.to != OrbState::Idle
                        || self.mix < 1.0
                        || self.presence < 1.0
                        || self.level > 0.01)
            }
            Some(_) => true,
            None => self.visible(),
        }
    }

    /// Freeze the orb at a state and level: `--dump-frame` and tests use this
    /// so a frame does not depend on the clock.
    pub fn pin(&mut self, state: OrbState, level: f32) {
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
    pub fn advance(&mut self, now: Instant, target: Option<OrbState>, raw: f32, reduced: bool) {
        let dt = self
            .last
            .map_or(0.0, |last| {
                now.saturating_duration_since(last).as_secs_f32()
            })
            .min(0.1);
        self.last = Some(now);
        if reduced {
            // No motion: it is simply there or not, still, at a fixed size.
            self.presence = if target.is_some() { 1.0 } else { 0.0 };
            if let Some(state) = target {
                self.from = state;
                self.to = state;
                self.mix = 1.0;
            }
            self.level = match target {
                Some(OrbState::Listening | OrbState::Speaking) => 0.5,
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
                if self.to != OrbState::Idle {
                    self.from = self.to;
                    self.to = OrbState::Idle;
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

/// The colour the orb is drawn in right now: the state it left, easing to the one it is in.
pub(crate) fn state_color(theme: &Theme, orb: &Orb) -> Option<[f32; 3]> {
    let of = |s: OrbState| theme.color(s.token()).and_then(rgb);
    let (a, b) = (of(orb.from)?, of(orb.to)?);
    Some(mix(a, b, ease(orb.mix)))
}

/// Per-subpixel sample: alpha and how close to the bright core.
struct Shape {
    /// Where the centre sits in the box, in half-block rows from its top.
    centre_y: f32,
    radius: f32,
    deform: f32,
    t: f32,
    presence: f32,
    width: f32,
    height: f32,
}

impl Shape {
    fn sample(&self, dx: f32, dy: f32) -> (f32, f32) {
        // A little wider than tall: the glow spreads along the composer's edge.
        let r = dx.hypot(dy);
        let theta = dy.atan2(dx);
        // Three lobes at different speeds and sizes, so the edge never repeats.
        let lobes = 0.55 * (2.0 * theta + self.t * 0.9).sin()
            + 0.30 * (3.0 * theta - self.t * 1.3).sin()
            + 0.15 * (5.0 * theta + self.t * 2.1).sin();
        let edge = (self.radius * (1.0 + self.deform * lobes)).max(0.2);
        let u = r / edge;
        let body = (-u * u * 1.7).exp();
        let halo = (-(r / (edge * 2.3)).powi(2)).exp() * 0.34;
        let core = (-(u / 0.58).powi(2)).exp();
        // The box has no visible edge: everything fades to nothing before it ends.
        let window = |d: f32, half: f32| ease(((half - d.abs()) / (half * 0.45)).clamp(0.0, 1.0));
        let alpha = (body + halo * (1.0 - body))
            * self.presence
            * window(dx, self.width / 2.0)
            * window(dy + self.centre_y - self.height, self.height);
        (alpha.clamp(0.0, 1.0), core)
    }
}

/// Where the orb sits: `x` is its centre column, `bottom` the row just below
/// its last cell (the top edge of the composer).
#[derive(Debug, Clone, Copy)]
pub struct Anchor {
    pub x: u16,
    pub bottom: u16,
}

/// The cells the orb may touch, clipped to `area`.
pub fn bounds(area: Rect, at: Anchor) -> Rect {
    bounds_sized(area, at, WIDTH, HEIGHT)
}

/// The cells an orb of `width` x `height` may touch, clipped to `area`.
pub fn bounds_sized(area: Rect, at: Anchor, width: u16, height: u16) -> Rect {
    let x = at.x.saturating_sub(width / 2);
    let y = at.bottom.saturating_sub(height);
    Rect::new(x, y, width, height).intersection(area)
}

/// The glyph for a state where the orb is not drawn as a picture.
pub fn glyph(state: OrbState, ascii: bool) -> &'static str {
    match (state, ascii) {
        (OrbState::Idle, false) => "·",
        (OrbState::Idle, true) => ".",
        (OrbState::Thinking, false) => "○",
        (OrbState::Thinking, true) => "o",
        (_, false) => "◉",
        (_, true) => "O",
    }
}

/// Paint the orb over `buf`. Call after every other layer.
pub fn paint(buf: &mut Buffer, area: Rect, at: Anchor, orb: &Orb, theme: &Theme) {
    paint_sized(buf, area, at, WIDTH, HEIGHT, orb, theme);
}

/// Paint an orb of `width` x `height` cells over `buf`.
pub fn paint_sized(
    buf: &mut Buffer,
    area: Rect,
    at: Anchor,
    width: u16,
    height: u16,
    orb: &Orb,
    theme: &Theme,
) {
    if !orb.visible() {
        return;
    }
    let state = if orb.mix < 0.5 { orb.from } else { orb.to };
    let picture = theme.opts.tier == Tier::TrueColor && !theme.opts.ascii;
    if !picture {
        paint_glyph(buf, area, at, height, orb, theme, state);
        return;
    }
    let Some(base) = state_color(theme, orb) else {
        paint_glyph(buf, area, at, height, orb, theme, state);
        return;
    };
    let white = theme
        .color("text.primary")
        .and_then(rgb)
        .unwrap_or([255.0; 3]);
    let bright = mix(base, white, 0.72);
    let canvas = theme
        .color("surface.canvas")
        .and_then(rgb)
        .unwrap_or([11.0, 11.0, 11.0]);

    // Level reads as size and as how far the lobes lean out. Thinking has no
    // level: it breathes slowly at a medium size.
    let breathing = 0.5 + 0.5 * (orb.t * 2.0 * std::f32::consts::PI / 3.4).sin();
    let (level, deform) = match orb.to {
        OrbState::Thinking => (0.30 + 0.28 * breathing, 0.07 + 0.04 * breathing),
        OrbState::Idle => (0.2, 0.06),
        _ => (orb.level, 0.07 + 0.2 * orb.level),
    };
    let rise = ease(orb.presence);
    let radius_max = (f32::from(width) / 2.0).min(f32::from(height)) * 0.60;
    let scale = 0.35 + 0.65 * rise;
    let mut shape = Shape {
        centre_y: 0.0,
        radius: radius_max * scale * (0.58 + 0.42 * level),
        deform,
        t: orb.t,
        presence: rise,
        width: f32::from(width),
        height: f32::from(height),
    };
    let origin_x = f32::from(at.x) - f32::from(width) / 2.0;
    let origin_y = f32::from(at.bottom) - f32::from(height);
    let cx = f32::from(width) / 2.0;
    // Rises from the composer edge as it arrives.
    let cy = f32::from(height) + (1.0 - rise) * 3.0;
    shape.centre_y = cy;
    let zone = bounds_sized(area, at, width, height);
    for y in zone.y..zone.bottom() {
        for x in zone.x..zone.right() {
            let sx = f32::from(x) - origin_x;
            let sy = (f32::from(y) - origin_y) * 2.0;
            let (top_a, top_core) = shape.sample(sx + 0.5 - cx, sy + 0.5 - cy);
            let (bot_a, bot_core) = shape.sample(sx + 0.5 - cx, sy + 1.5 - cy);
            let strongest = top_a.max(bot_a);
            if strongest < 0.015 {
                continue;
            }
            let cell = &mut buf[(x, y)];
            let under_bg = rgb(cell.bg).unwrap_or(canvas);
            let under_fg = rgb(cell.fg).unwrap_or(white);
            let text = {
                let s = cell.symbol();
                !s.trim().is_empty() && !matches!(s, "▀" | "▄" | "█" | "▌" | "▐")
            };
            let tone = |core: f32| mix(base, bright, core * 0.95);
            if text && strongest < 0.5 {
                // The fringe: tint the cell, keep the glyph readable.
                let k = (top_a + bot_a) * 0.5;
                let tint = tone((top_core + bot_core) * 0.5);
                cell.bg = color(mix(under_bg, tint, k * 0.55));
                cell.fg = color(mix(under_fg, tint, k * 0.22));
                continue;
            }
            let top = mix(under_bg, tone(top_core), top_a);
            let bottom = mix(under_bg, tone(bot_core), bot_a);
            cell.set_symbol("▀");
            cell.set_style(Style::default().fg(color(top)).bg(color(bottom)));
            cell.modifier = Modifier::empty();
        }
    }
}

/// One glyph at the orb's centre, in the state's colour.
fn paint_glyph(
    buf: &mut Buffer,
    area: Rect,
    at: Anchor,
    height: u16,
    orb: &Orb,
    theme: &Theme,
    state: OrbState,
) {
    let y = at.bottom.saturating_sub(height / 2 + 1);
    if !contains(area, at.x, y) {
        return;
    }
    let style = theme.fg(state.token());
    let cell = &mut buf[(at.x, y)];
    cell.set_symbol(glyph(state, theme.opts.ascii));
    let bg = cell.bg;
    cell.set_style(style.bg(bg));
    let _ = orb;
}

fn contains(area: Rect, x: u16, y: u16) -> bool {
    x >= area.x && x < area.right() && y >= area.y && y < area.bottom()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::{Options, Theme};

    fn paint_to_buffer(orb: &Orb, theme: &Theme) -> Buffer {
        let area = Rect::new(0, 0, 40, 12);
        let mut buf = Buffer::empty(area);
        for cell in buf.content.iter_mut() {
            cell.set_style(
                Style::default()
                    .fg(Color::Rgb(200, 200, 200))
                    .bg(Color::Rgb(11, 11, 11)),
            );
        }
        paint(&mut buf, area, Anchor { x: 20, bottom: 10 }, orb, theme);
        buf
    }

    fn painted(buf: &Buffer) -> usize {
        buf.content.iter().filter(|c| c.symbol() == "▀").count()
    }

    #[test]
    fn a_hidden_orb_draws_nothing() {
        let theme = Theme::new(Options::default());
        let buf = paint_to_buffer(&Orb::default(), &theme);
        assert_eq!(painted(&buf), 0);
    }

    #[test]
    fn a_pinned_orb_has_a_soft_edge_and_stays_in_its_box() {
        let theme = Theme::new(Options::default());
        let mut orb = Orb::default();
        orb.pin(OrbState::Listening, 0.6);
        let buf = paint_to_buffer(&orb, &theme);
        assert!(painted(&buf) > 20, "the orb is drawn");
        let zone = bounds(buf.area, Anchor { x: 20, bottom: 10 });
        for y in 0..12u16 {
            for x in 0..40u16 {
                if !contains(zone, x, y) {
                    assert_eq!(buf[(x, y)].symbol(), " ", "outside the box at {x},{y}");
                }
            }
        }
        // The centre is the listening colour brightened; the rim blends into the canvas.
        let centre = buf[(20, 8)].fg;
        let rim = buf[(20 - 6, 8)].bg;
        assert_ne!(centre, Color::Rgb(11, 11, 11));
        assert!(matches!(rim, Color::Rgb(..)));
    }

    #[test]
    fn levels_move_fast_up_and_slowly_down() {
        let mut orb = Orb::default();
        let start = Instant::now();
        orb.advance(start, Some(OrbState::Listening), 1.0, false);
        let mut now = start;
        for _ in 0..3 {
            now += std::time::Duration::from_millis(30);
            orb.advance(now, Some(OrbState::Listening), 1.0, false);
        }
        let risen = orb.level;
        assert!(risen > 0.55, "attack is about 60 ms: {risen}");
        for _ in 0..3 {
            now += std::time::Duration::from_millis(30);
            orb.advance(now, Some(OrbState::Listening), 0.0, false);
        }
        assert!(
            orb.level > risen * 0.4,
            "release is slower than attack: {}",
            orb.level
        );
    }

    #[test]
    fn a_text_cell_under_the_fringe_keeps_its_glyph() {
        let theme = Theme::new(Options::default());
        let mut orb = Orb::default();
        orb.pin(OrbState::Speaking, 0.5);
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
        paint(&mut buf, area, Anchor { x: 20, bottom: 10 }, &orb, &theme);
        // Far out on the row the halo is faint: the text is still there.
        assert_eq!(buf[(20 + 6, 9)].symbol(), "x");
    }

    #[test]
    fn exit_takes_the_orb_away() {
        let mut orb = Orb::default();
        orb.pin(OrbState::Speaking, 0.5);
        let mut now = Instant::now();
        orb.advance(now, None, 0.0, false);
        for _ in 0..30 {
            now += std::time::Duration::from_millis(33);
            orb.advance(now, None, 0.0, false);
        }
        assert!(!orb.visible());
    }

    #[test]
    fn low_tiers_draw_one_glyph() {
        let theme = Theme::new(Options {
            tier: Tier::Ansi16,
            ..Options::default()
        });
        let mut orb = Orb::default();
        orb.pin(OrbState::Thinking, 0.0);
        let buf = paint_to_buffer(&orb, &theme);
        assert_eq!(painted(&buf), 0);
        assert!(buf.content.iter().any(|c| c.symbol() == "○"));
    }
}
