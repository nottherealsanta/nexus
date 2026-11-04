//! The Manager's orb as a real picture, where the terminal has the kitty
//! graphics protocol (docs/manager-design.md 10.2).
//!
//! The same drawing as NSH's `orb.ts` (halo, ripples, lit body, drifting
//! lobes, bright core) rendered here with antialiasing and alpha into small RGBA
//! sprites. A sprite is transmitted once, under a fixed image id, the first time
//! its state, level, phase and presence are needed; after that an animation
//! frame is only a placement (`a=p`) and the delete of the previous placement,
//! a couple of dozen bytes. Placements sit above text (`z=1`), never move the
//! cursor (`C=1`), and go out with the frame, inside the synchronized update
//! `draw_terminal` already wraps it in, so they cannot tear.
//!
//! Nothing is ever queued while the orb is not on screen.

use std::collections::HashSet;
use std::fmt::Write;

use base64::{engine::general_purpose::STANDARD, Engine};

use crate::orb::{OrbState, HEIGHT, WIDTH};

/// Fixed image ids of the sprite set: `BASE` plus the sprite's index.
pub const BASE: u32 = 0x6d00_0000;
const LEVELS: u32 = 6;
const PHASES: u32 = 12;
const PRESENCES: u32 = 5;
const CHUNK: usize = 4096;
/// Seconds of lobe drift between two phase buckets.
const PHASE_STEP: f32 = 0.5;

/// What to draw: the state, how loud, how far through its drift, how present,
/// in which colours, and where (top-left cell of the 14 by 4 box).
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    pub state: OrbState,
    pub level: f32,
    pub t: f32,
    pub presence: f32,
    pub color: [u8; 3],
    pub white: [u8; 3],
    pub light: bool,
    pub col: u16,
    pub row: u16,
}

#[derive(Debug, Default)]
pub struct Sprites {
    supported: bool,
    font: (u16, u16),
    queue: String,
    sent: HashSet<u32>,
    placed: Option<(u32, u16, u16)>,
    palette: Option<([u8; 3], bool)>,
}

fn state_index(state: OrbState) -> u32 {
    match state {
        OrbState::Listening => 0,
        OrbState::Thinking => 1,
        OrbState::Speaking => 2,
        OrbState::Idle => 3,
    }
}

/// The sprite a request maps to: its fixed id and the exact parameters it is
/// drawn with (the buckets, not the raw values).
fn bucket(req: &Request) -> (u32, f32, f32, f32) {
    let live = matches!(req.state, OrbState::Listening | OrbState::Speaking);
    let lv = if live {
        (req.level.clamp(0.0, 1.0) * (LEVELS - 1) as f32).round() as u32
    } else {
        0
    };
    let moving = req.state != OrbState::Idle;
    let ph = if moving {
        ((req.t / PHASE_STEP) as u32) % PHASES
    } else {
        0
    };
    let pr = if req.presence >= 0.99 {
        PRESENCES - 1
    } else {
        ((req.presence.clamp(0.0, 1.0) * (PRESENCES - 1) as f32) as u32).min(PRESENCES - 2)
    };
    let index = (((state_index(req.state) * LEVELS + lv) * PHASES + ph) * PRESENCES) + pr;
    let level = lv as f32 / (LEVELS - 1) as f32;
    let presence = if pr == PRESENCES - 1 {
        1.0
    } else {
        pr as f32 / (PRESENCES - 1) as f32 + 0.125
    };
    (BASE + index, level, ph as f32 * PHASE_STEP, presence)
}

impl Sprites {
    /// Set once the terminal's capabilities are known.
    pub fn configure(&mut self, supported: bool, font: (u16, u16)) {
        self.supported = supported;
        if font.0 > 0 && font.1 > 0 {
            self.font = font;
        }
    }

    pub fn supported(&self) -> bool {
        self.supported && self.font.0 > 0 && self.font.1 > 0
    }

    /// The bytes to write after this frame (inside the synchronized update).
    pub fn take(&mut self) -> String {
        std::mem::take(&mut self.queue)
    }

    /// The terminal was handed back and cleared: everything is sent again.
    pub fn forget(&mut self) {
        self.placed = None;
        self.sent.clear();
        self.queue.clear();
    }

    /// The screen changed size: take the placement off so it is placed again.
    pub fn invalidate(&mut self) {
        self.sync(None);
    }

    /// Bring the terminal up to date: draw the sprite for `req`, or take the orb away.
    pub fn sync(&mut self, req: Option<&Request>) {
        if !self.supported() {
            return;
        }
        let Some(req) = req else {
            if let Some((id, _, _)) = self.placed.take() {
                let _ = write!(self.queue, "\x1b_Ga=d,d=i,i={id},q=2\x1b\\");
            }
            return;
        };
        let palette = (req.color, req.light);
        if self.palette != Some(palette) {
            // A new theme: every sprite is the wrong colour.
            let old: Vec<u32> = self.sent.drain().collect();
            for id in old {
                let _ = write!(self.queue, "\x1b_Ga=d,d=I,i={id},q=2\x1b\\");
            }
            self.placed = None;
            self.palette = Some(palette);
        }
        let (id, level, t, presence) = bucket(req);
        if !self.sent.contains(&id) {
            self.transmit(id, req, level, t, presence);
            self.sent.insert(id);
        }
        if self.placed == Some((id, req.col, req.row)) {
            return;
        }
        let _ = write!(
            self.queue,
            "\x1b7\x1b[{};{}H\x1b_Ga=p,i={id},p=1,c={WIDTH},r={HEIGHT},z=1,C=1,q=2\x1b\\\x1b8",
            req.row + 1,
            req.col + 1
        );
        // The new one is up before the old one goes, so there is no frame without an orb.
        if let Some((old, _, _)) = self.placed.replace((id, req.col, req.row)) {
            if old != id {
                let _ = write!(self.queue, "\x1b_Ga=d,d=i,i={old},q=2\x1b\\");
            }
        }
    }

    /// Take everything this has put on the screen away, for exit.
    pub fn shutdown(&mut self) -> String {
        if self.supported() {
            if let Some((id, _, _)) = self.placed.take() {
                let _ = write!(self.queue, "\x1b_Ga=d,d=i,i={id},q=2\x1b\\");
            }
            let old: Vec<u32> = self.sent.drain().collect();
            for id in old {
                let _ = write!(self.queue, "\x1b_Ga=d,d=I,i={id},q=2\x1b\\");
            }
        }
        self.take()
    }

    fn transmit(&mut self, id: u32, req: &Request, level: f32, t: f32, presence: f32) {
        let (w, h) = (
            u32::from(WIDTH) * u32::from(self.font.0),
            u32::from(HEIGHT) * u32::from(self.font.1),
        );
        let pixels = render(req, level, t, presence, w, h);
        let data = STANDARD.encode(&pixels);
        let mut chunks = data.as_bytes().chunks(CHUNK).peekable();
        let mut first = true;
        while let Some(chunk) = chunks.next() {
            let more = u8::from(chunks.peek().is_some());
            let chunk = std::str::from_utf8(chunk).unwrap_or_default();
            if first {
                let _ = write!(
                    self.queue,
                    "\x1b_Ga=t,i={id},f=32,s={w},v={h},q=2,m={more};{chunk}\x1b\\"
                );
                first = false;
            } else {
                let _ = write!(self.queue, "\x1b_Gq=2,m={more};{chunk}\x1b\\");
            }
        }
    }
}

// ---- drawing -----------------------------------------------------------------------------

type Rgb = [f32; 3];

fn smooth(x: f32) -> f32 {
    let x = x.clamp(0.0, 1.0);
    x * x * (3.0 - 2.0 * x)
}

fn mix(a: Rgb, b: Rgb, t: f32) -> Rgb {
    [
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    ]
}

/// A radial gradient stop list evaluated at distance `d`: colour and alpha, or
/// nothing outside the outer radius.
fn stops(d: f32, from: f32, to: f32, list: &[(f32, Rgb, f32)]) -> Option<(Rgb, f32)> {
    if d >= to || to <= from {
        return None;
    }
    let k = ((d - from) / (to - from)).clamp(0.0, 1.0);
    let mut prev = list[0];
    for &next in &list[1..] {
        if k <= next.0 {
            let u = if next.0 > prev.0 {
                (k - prev.0) / (next.0 - prev.0)
            } else {
                1.0
            };
            return Some((mix(prev.1, next.1, u), prev.2 + (next.2 - prev.2) * u));
        }
        prev = next;
    }
    Some((prev.1, prev.2))
}

/// Source-over onto a premultiplied pixel.
fn over(px: &mut [f32; 4], c: Rgb, a: f32, global: f32) {
    let a = (a * global).clamp(0.0, 1.0);
    for i in 0..3 {
        px[i] = c[i] * a + px[i] * (1.0 - a);
    }
    px[3] = a + px[3] * (1.0 - a);
}

/// The orb drawn as NSH's `drawOrb` does, into `w` by `h` straight-alpha RGBA.
/// 2x2 supersampling keeps every edge antialiased.
pub fn render(req: &Request, level: f32, t: f32, presence: f32, w: u32, h: u32) -> Vec<u8> {
    let tau = std::f32::consts::TAU;
    let col: Rgb = [
        f32::from(req.color[0]),
        f32::from(req.color[1]),
        f32::from(req.color[2]),
    ];
    let white: Rgb = [255.0, 255.0, 255.0];
    let core = mix(col, white, if req.light { 0.35 } else { 0.62 });
    let p = smooth(presence);
    let size = h as f32;
    let nominal = size * 0.30;
    let mut r = match req.state {
        OrbState::Listening | OrbState::Speaking => nominal * (0.8 + 0.3 * level),
        OrbState::Thinking => nominal * (0.84 + 0.05 * (t * tau / 2.6).sin()),
        OrbState::Idle => nominal * 0.8,
    };
    r *= 0.55 + 0.45 * p;
    let live = matches!(req.state, OrbState::Listening | OrbState::Speaking);
    let x = w as f32 / 2.0;
    let y = size * 0.56 + (1.0 - p) * size * 0.2;
    let (lx, ly) = (x - r * 0.18, y - r * 0.22);
    let drift = if req.state == OrbState::Thinking { 0.5 } else { 1.0 };
    let light = req.light;
    let level = if req.state == OrbState::Idle { 0.0 } else { level };
    let edge = |d: f32, whole: f32| smooth((d.min(whole - d)) / (whole * 0.2));

    let mut out = vec![0u8; (w * h * 4) as usize];
    for py in 0..h {
        for pxl in 0..w {
            let mut acc = [0.0f32; 4];
            for sy in 0..2 {
                for sx in 0..2 {
                    let (fx, fy) = (pxl as f32 + 0.25 + 0.5 * sx as f32, py as f32 + 0.25 + 0.5 * sy as f32);
                    let mut px = [0.0f32; 4];
                    let dist = |cx: f32, cy: f32| (fx - cx).hypot(fy - cy);
                    // 1. The halo.
                    if let Some((c, a)) = stops(
                        dist(x, y),
                        r * 0.2,
                        r * 2.15,
                        &[
                            (0.0, col, if light { 0.2 } else { 0.3 + 0.12 * level }),
                            (0.45, col, if light { 0.08 } else { 0.11 + 0.05 * level }),
                            (1.0, col, 0.0),
                        ],
                    ) {
                        over(&mut px, c, a, 1.0);
                    } else if dist(x, y) < r * 0.2 {
                        over(&mut px, col, if light { 0.2 } else { 0.3 + 0.12 * level }, 1.0);
                    }
                    // 2. Ripples that follow a voice.
                    if live {
                        for i in 0..2 {
                            let k = (t * 0.6 + i as f32 * 0.5) % 1.0;
                            let ring = r * (1.05 + 0.85 * k);
                            let band = r * 0.2;
                            let a = (std::f32::consts::PI * (k * 1.15).min(1.0)).sin()
                                * (0.05 + 0.22 * level);
                            if let Some((c, a)) = stops(
                                dist(x, y),
                                (ring - band).max(0.0),
                                ring + band,
                                &[(0.0, col, 0.0), (0.5, col, a), (1.0, col, 0.0)],
                            ) {
                                over(&mut px, c, a, 1.0);
                            }
                        }
                    }
                    // 3. The body, lit from above left.
                    let solid = if light { 0.82 } else { 0.94 };
                    let body_d = {
                        // A gradient whose focus is offset: distance from the focus, scaled so the rim stays at 1.1r.
                        let from_focus = dist(lx, ly);
                        let from_centre = dist(x, y);
                        0.5 * (from_focus + from_centre)
                    };
                    if let Some((c, a)) = stops(
                        body_d,
                        r * 0.06,
                        r * 1.1,
                        &[
                            (0.0, core, solid),
                            (0.2, mix(col, core, 0.45), solid * 0.94),
                            (0.55, col, solid * 0.58),
                            (0.82, col, solid * 0.2),
                            (1.0, col, 0.0),
                        ],
                    ) {
                        over(&mut px, c, a, 1.0);
                    } else if body_d < r * 0.06 {
                        over(&mut px, core, solid, 1.0);
                    }
                    // 4. Lobes.
                    if req.state != OrbState::Idle || true {
                        for i in 0..3 {
                            let a = t * (0.55 + i as f32 * 0.23) * drift + i as f32 * 2.1;
                            let d = r * (0.1 + 0.3 * level) * if i == 1 { 1.15 } else { 1.0 };
                            let (bx, by) = (x + a.cos() * d, y + (a * 1.17).sin() * d);
                            let lr = r * (0.62 + 0.26 * level);
                            if let Some((c, al)) = stops(
                                dist(bx, by),
                                0.0,
                                lr,
                                &[
                                    (0.0, mix(col, core, 0.3), 0.34 + 0.2 * level),
                                    (1.0, col, 0.0),
                                ],
                            ) {
                                over(&mut px, c, al, 1.0);
                            }
                        }
                    }
                    // 5. The bright core and its highlight.
                    if let Some((c, a)) = stops(
                        dist(lx, ly),
                        0.0,
                        r * 0.62,
                        &[
                            (0.0, white, if light { 0.6 } else { 0.74 }),
                            (0.45, core, 0.34),
                            (1.0, core, 0.0),
                        ],
                    ) {
                        over(&mut px, c, a, 1.0);
                    }
                    for i in 0..4 {
                        acc[i] += px[i];
                    }
                }
            }
            // Nothing is cut off at the box: fade to clear over the outer fifth.
            let window = edge(pxl as f32 + 0.5, w as f32) * edge(py as f32 + 0.5, size);
            let a = (acc[3] / 4.0) * p * window;
            let i = ((py * w + pxl) * 4) as usize;
            if a > 0.0 && acc[3] > 0.0 {
                for k in 0..3 {
                    // premultiplied -> straight
                    out[i + k] = (acc[k] / acc[3]).clamp(0.0, 255.0).round() as u8;
                }
                out[i + 3] = (a * 255.0).round() as u8;
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(state: OrbState, level: f32, col: u16) -> Request {
        Request {
            state,
            level,
            t: 1.2,
            presence: 1.0,
            color: [105, 183, 213],
            white: [255, 255, 255],
            light: false,
            col,
            row: 20,
        }
    }

    fn on() -> Sprites {
        let mut s = Sprites::default();
        s.configure(true, (8, 16));
        s
    }

    #[test]
    fn at_rest_nothing_is_ever_emitted() {
        let mut s = on();
        s.sync(None);
        s.sync(None);
        assert_eq!(s.take(), "");
        assert_eq!(s.shutdown(), "");
        // A terminal without the protocol never emits either.
        let mut off = Sprites::default();
        off.sync(Some(&request(OrbState::Listening, 0.5, 4)));
        assert_eq!(off.take(), "");
    }

    #[test]
    fn a_sprite_is_sent_once_then_only_placed() {
        let mut s = on();
        s.sync(Some(&request(OrbState::Listening, 0.5, 4)));
        let first = s.take();
        assert!(first.contains("a=t,i="), "transmitted");
        assert!(first.contains("f=32,s=112,v=64"), "{}", &first[..60]);
        assert!(first.contains("a=p,"), "placed");
        assert!(first.contains(&format!("c={WIDTH},r={HEIGHT},z=1,C=1")));
        assert!(first.contains("\x1b[21;5H"), "at the box's cell");
        // The same frame again: no bytes at all.
        s.sync(Some(&request(OrbState::Listening, 0.5, 4)));
        assert_eq!(s.take(), "");
        // Another level is a different sprite; going back to the first sends no pixels.
        s.sync(Some(&request(OrbState::Listening, 1.0, 4)));
        assert!(s.take().contains("a=t,"));
        s.sync(Some(&request(OrbState::Listening, 0.5, 4)));
        let back = s.take();
        assert!(!back.contains("a=t,"), "{back:?}");
        assert!(back.contains("a=p,") && back.contains("a=d,d=i,"));
        assert!(back.len() < 160, "a replace is a few dozen bytes: {}", back.len());
    }

    #[test]
    fn moving_replaces_the_placement_and_leaving_deletes_it() {
        let mut s = on();
        s.sync(Some(&request(OrbState::Speaking, 0.5, 4)));
        s.take();
        // A resize moves the box: placed again where it now is.
        s.sync(Some(&request(OrbState::Speaking, 0.5, 30)));
        let moved = s.take();
        assert!(moved.contains("\x1b[21;31H") && moved.contains("a=p,"));
        assert!(!moved.contains("a=t,"));
        s.invalidate();
        assert!(s.take().contains("a=d,d=i,"));
        s.sync(Some(&request(OrbState::Speaking, 0.5, 30)));
        assert!(s.take().contains("a=p,"), "re-placed after invalidate");
        s.sync(None);
        let gone = s.take();
        assert!(gone.contains("a=d,d=i,") && !gone.contains("a=p,"));
        // Nothing more once it is gone.
        s.sync(None);
        assert_eq!(s.take(), "");
    }

    #[test]
    fn shutdown_removes_the_placement_and_frees_the_images() {
        let mut s = on();
        s.sync(Some(&request(OrbState::Thinking, 0.0, 4)));
        s.take();
        let bytes = s.shutdown();
        assert!(bytes.contains("a=d,d=i,") && bytes.contains("a=d,d=I,"));
        assert_eq!(s.shutdown(), "");
    }

    #[test]
    fn the_picture_is_soft_and_clear_at_its_edges() {
        let req = request(OrbState::Listening, 0.7, 0);
        let (w, h) = (112u32, 64u32);
        let px = render(&req, 0.6, 1.0, 1.0, w, h);
        let alpha = |x: u32, y: u32| px[((y * w + x) * 4 + 3) as usize];
        assert!(alpha(w / 2, h / 2 + 4) > 200, "a bright core");
        assert_eq!(alpha(0, 0), 0);
        assert_eq!(alpha(w - 1, h - 1), 0);
        assert_eq!(alpha(0, h / 2), 0);
        // Alpha falls off smoothly: no step bigger than a fraction between neighbours.
        let steps = (w / 2..w - 1)
            .map(|x| i32::from(alpha(x, h / 2 + 4)) - i32::from(alpha(x + 1, h / 2 + 4)))
            .max()
            .unwrap();
        assert!(steps < 24, "soft edge: {steps}");
    }
}
