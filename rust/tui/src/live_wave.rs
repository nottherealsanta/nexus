//! The GPT Live call's motion band: a row of block-height bars (`▁▂▃▄▅▆▇█`) whose motion
//! depends on the call state, replacing the old dot sweep, braille orbit and `∿` ripple.
//!
//! - listening: a calm breathing swell; the microphone lifts the bars as the user speaks
//!   (newest level on the right, eased so it never jumps);
//! - speaking: a fuller travelling wave in the accent colour, brightest in the middle and
//!   dimmer at the edges, deeper with the voice level;
//! - connecting / working: a narrow bright shimmer travelling left to right;
//! - muted / ended: a flat static line.
//!
//! Everything is a pure function of the frame number `frame` (the 30 Hz activity clock), so the
//! motion is smooth, needs no state of its own and costs nothing while idle (`wants_frames`).
//! Without animation (reduced motion, `NO_COLOR`) each state rests in a static pose; in ASCII
//! mode the bars are `_.:-=+*#`. The band is `cells_for(room)` wide and never exceeds `room`.
use crate::render::Palette;
use nexus_widgets::theme::mix;
use ratatui::{style::Style, text::Span};

pub const MAX_CELLS: usize = 14;
pub const MIN_CELLS: usize = 4;
const LEVELS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
const ASCII_LEVELS: [char; 8] = ['_', '.', ':', '-', '=', '+', '*', '#'];

/// Which motion a call state gets.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Listening,
    Speaking,
    Shimmer,
    Flat,
}

/// The motion for a phase; `None` for phases that show text instead (paused).
pub fn kind_for(phase: &str) -> Option<Kind> {
    match phase {
        "listening" => Some(Kind::Listening),
        "speaking" => Some(Kind::Speaking),
        "connecting" | "working" => Some(Kind::Shimmer),
        "muted" | "ended" => Some(Kind::Flat),
        _ => None,
    }
}

/// Whether the call is in a state that moves, so the 30 Hz clock should draw frames.
pub fn wants_frames(live: &crate::bridge::LiveBar) -> bool {
    live.active && !live.paused && !live.muted && live.phase != "ended"
}

/// Cells for a band given `room` free columns: at most `MAX_CELLS`, none below `MIN_CELLS`.
pub fn cells_for(room: usize) -> usize {
    if room < MIN_CELLS {
        0
    } else {
        room.min(MAX_CELLS)
    }
}

/// What the band reads besides the clock.
pub struct Inputs<'a> {
    /// Microphone history, newest last (0..1 RMS).
    pub history: &'a [f32],
    /// Eased microphone and voice levels (0..1 RMS).
    pub level_in: f32,
    pub level_out: f32,
}

fn bell(i: usize, n: usize) -> f32 {
    ((i as f32 + 0.5) / n as f32 * std::f32::consts::PI).sin()
}

/// Bar heights 0..1 for `cells` bars. `animate` false gives the resting pose.
pub fn heights(kind: Kind, cells: usize, frame: usize, animate: bool, input: &Inputs) -> Vec<f32> {
    let t = frame as f32;
    let tau = std::f32::consts::TAU;
    (0..cells)
        .map(|i| {
            let x = i as f32;
            match kind {
                Kind::Flat => 0.0,
                Kind::Listening => {
                    let back = cells - 1 - i;
                    let hist = input
                        .history
                        .len()
                        .checked_sub(1 + back)
                        .map_or(0.0, |at| input.history[at].clamp(0.0, 1.0).sqrt());
                    if !animate {
                        return hist;
                    }
                    // Breathing: a slow swell (~5 s) with a gentle ripple, never above ~30%.
                    let breath = 0.5 + 0.5 * (t * 0.042).sin();
                    let ripple = 0.5 + 0.5 * (x * 0.9 - t * 0.12).sin();
                    let calm = 0.06 + 0.24 * breath * (0.35 + 0.65 * ripple);
                    let live = input.level_in.clamp(0.0, 1.0).sqrt()
                        * (0.45 + 0.55 * (0.5 + 0.5 * (x * 0.7 - t * 0.35).sin()));
                    calm.max(0.5 * hist + 0.5 * live)
                }
                Kind::Speaking => {
                    let depth = 0.45 + 0.55 * input.level_out.clamp(0.0, 1.0).sqrt();
                    let envelope = 0.55 + 0.45 * bell(i, cells);
                    if !animate {
                        return 0.5 * envelope;
                    }
                    let a = 0.5 + 0.5 * (x * 0.8 - t * 0.28).sin();
                    let b = 0.5 + 0.5 * (x * 1.9 + t * 0.17).sin();
                    (0.12 + 0.88 * (0.65 * a + 0.35 * b)) * depth * envelope
                }
                Kind::Shimmer => {
                    // A narrow highlight crossing the band left to right, a little off-screen
                    // at both ends so it enters and leaves smoothly.
                    let span = cells as f32 + 6.0;
                    let at = if animate {
                        (t * 0.22) % span - 3.0
                    } else {
                        (cells as f32) / 2.0
                    };
                    let d = x - at;
                    0.1 + 0.9 * (-(d * d) / 3.0).exp()
                }
            }
            .clamp(0.0, 1.0)
        })
        .collect()
}

/// The band as one span per bar, on `p.element`.
pub fn band(
    kind: Kind,
    cells: usize,
    frame: usize,
    animate: bool,
    ascii: bool,
    tone: ratatui::style::Color,
    p: &Palette,
    input: &Inputs,
) -> Vec<Span<'static>> {
    let table = if ascii { ASCII_LEVELS } else { LEVELS };
    heights(kind, cells, frame, animate, input)
        .into_iter()
        .enumerate()
        .map(|(i, v)| {
            let glyph = table[((v * 7.0).round() as usize).min(7)];
            let fg = match kind {
                Kind::Flat => p.quiet,
                // Dim at the edges, full accent in the middle, brighter as the bar rises.
                Kind::Speaking => mix(
                    p.element,
                    tone,
                    0.35 + 0.65 * bell(i, cells) * (0.5 + 0.5 * v),
                ),
                Kind::Shimmer => mix(p.quiet, tone, v),
                Kind::Listening => mix(p.quiet, tone, 0.45 + 0.55 * v),
            };
            Span::styled(glyph.to_string(), Style::default().fg(fg).bg(p.element))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quiet() -> Inputs<'static> {
        Inputs {
            history: &[],
            level_in: 0.0,
            level_out: 0.0,
        }
    }
    fn loud() -> Inputs<'static> {
        Inputs {
            history: &[0.2, 0.5, 0.9],
            level_in: 0.6,
            level_out: 0.6,
        }
    }
    fn text(spans: &[Span]) -> String {
        spans.iter().map(|s| s.content.as_ref()).collect()
    }
    fn draw(kind: Kind, cells: usize, frame: usize, animate: bool, ascii: bool) -> String {
        let p = Palette::new(false);
        text(&band(
            kind,
            cells,
            frame,
            animate,
            ascii,
            p.accent,
            &p,
            &loud(),
        ))
    }

    #[test]
    fn frames_differ_over_time_while_animated() {
        for kind in [Kind::Listening, Kind::Speaking, Kind::Shimmer] {
            let frames: std::collections::HashSet<_> =
                (0..60).map(|f| draw(kind, 12, f, true, false)).collect();
            assert!(
                frames.len() > 6,
                "{kind:?} should move, saw {}",
                frames.len()
            );
        }
        assert_eq!(
            draw(Kind::Flat, 12, 0, true, false),
            draw(Kind::Flat, 12, 40, true, false)
        );
    }

    #[test]
    fn motion_is_smooth_between_adjacent_frames() {
        for kind in [Kind::Listening, Kind::Speaking, Kind::Shimmer] {
            for f in 0..120 {
                let a = heights(kind, 12, f, true, &loud());
                let b = heights(kind, 12, f + 1, true, &loud());
                for (x, y) in a.iter().zip(&b) {
                    assert!((x - y).abs() < 0.3, "{kind:?} frame {f}: {x} -> {y}");
                }
            }
        }
    }

    #[test]
    fn each_state_has_its_own_pattern() {
        let k = |kind| draw(kind, 12, 17, true, false);
        let all = [
            k(Kind::Listening),
            k(Kind::Speaking),
            k(Kind::Shimmer),
            k(Kind::Flat),
        ];
        for i in 0..all.len() {
            for j in i + 1..all.len() {
                assert_ne!(all[i], all[j], "{i} vs {j}");
            }
        }
        assert_eq!(kind_for("listening"), Some(Kind::Listening));
        assert_eq!(kind_for("speaking"), Some(Kind::Speaking));
        assert_eq!(kind_for("connecting"), Some(Kind::Shimmer));
        assert_eq!(kind_for("working"), Some(Kind::Shimmer));
        assert_eq!(kind_for("muted"), Some(Kind::Flat));
        assert_eq!(kind_for("ended"), Some(Kind::Flat));
        assert_eq!(kind_for("paused"), None);
        // Listening breathes low when silent, and the mic lifts it.
        let calm = heights(Kind::Listening, 12, 30, true, &quiet());
        assert!(calm.iter().all(|v| *v <= 0.31), "{calm:?}");
        let up = heights(Kind::Listening, 12, 30, true, &loud());
        assert!(up.iter().cloned().fold(0.0, f32::max) > 0.6);
        // Speaking is fuller than listening, and the shimmer travels left to right.
        let mean = |v: Vec<f32>| v.iter().sum::<f32>() / v.len() as f32;
        assert!(mean(heights(Kind::Speaking, 12, 9, true, &loud())) > mean(calm));
        let peak = |f| {
            let h = heights(Kind::Shimmer, 12, f, true, &quiet());
            h.iter()
                .position(|v| *v == h.iter().cloned().fold(0.0, f32::max))
                .unwrap()
        };
        assert!(peak(20) < peak(40) && peak(40) < peak(60));
    }

    #[test]
    fn speaking_gradient_is_dimmer_at_the_edges() {
        let p = Palette::new(false);
        let spans = band(Kind::Speaking, 12, 5, false, false, p.accent, &p, &loud());
        let lum = |s: &Span| match s.style.fg {
            Some(ratatui::style::Color::Rgb(r, g, b)) => r as u32 + g as u32 + b as u32,
            _ => 0,
        };
        let (edge, mid) = (lum(&spans[0]), lum(&spans[6]));
        assert_ne!(edge, mid);
    }

    #[test]
    fn reduced_motion_is_static_and_ascii_has_no_blocks() {
        for kind in [Kind::Listening, Kind::Speaking, Kind::Shimmer, Kind::Flat] {
            assert_eq!(
                draw(kind, 12, 0, false, false),
                draw(kind, 12, 77, false, false),
                "{kind:?}"
            );
            for f in [0, 13, 50] {
                let a = draw(kind, 12, f, true, true);
                assert!(a.is_ascii() && a.chars().count() == 12, "{kind:?}: {a:?}");
            }
            let still = draw(kind, 12, 0, false, true);
            assert_eq!(still, draw(kind, 12, 31, false, true));
            assert!(still.is_ascii());
        }
    }

    #[test]
    fn width_adapts_and_never_overflows() {
        assert_eq!(cells_for(0), 0);
        assert_eq!(cells_for(MIN_CELLS - 1), 0);
        assert_eq!(cells_for(MIN_CELLS), MIN_CELLS);
        assert_eq!(cells_for(9), 9);
        assert_eq!(cells_for(200), MAX_CELLS);
        for room in 0..40 {
            let n = cells_for(room);
            assert!(n <= room);
            let p = Palette::new(false);
            let spans = band(Kind::Speaking, n, 3, true, false, p.accent, &p, &loud());
            assert_eq!(spans.len(), n);
        }
    }

    #[test]
    fn frames_are_only_wanted_while_the_call_moves() {
        let mut live = crate::bridge::LiveBar {
            active: true,
            phase: "listening".into(),
            ..Default::default()
        };
        assert!(wants_frames(&live));
        live.phase = "speaking".into();
        assert!(wants_frames(&live));
        live.muted = true;
        assert!(!wants_frames(&live));
        live.muted = false;
        live.paused = true;
        assert!(!wants_frames(&live));
        live.paused = false;
        live.phase = "ended".into();
        assert!(!wants_frames(&live));
        live.phase = "listening".into();
        live.active = false;
        assert!(!wants_frames(&live), "idle");
    }

    /// `cargo test dump_frames -- --ignored` writes text frames of every state to
    /// `artifacts/live-panel-anim/bands.txt` (for review; not part of the suite).
    #[test]
    #[ignore]
    fn dump_frames() {
        let mut out = String::new();
        let mic = [
            0.02, 0.05, 0.3, 0.6, 0.2, 0.05, 0.4, 0.8, 0.5, 0.1, 0.05, 0.02, 0.3, 0.7,
        ];
        for (name, kind, input) in [
            (
                "listening, quiet (breathing)",
                Kind::Listening,
                Inputs {
                    history: &[],
                    level_in: 0.0,
                    level_out: 0.0,
                },
            ),
            (
                "listening, user speaking (mic)",
                Kind::Listening,
                Inputs {
                    history: &mic,
                    level_in: 0.5,
                    level_out: 0.0,
                },
            ),
            (
                "speaking (assistant)",
                Kind::Speaking,
                Inputs {
                    history: &[],
                    level_in: 0.0,
                    level_out: 0.5,
                },
            ),
            (
                "connecting / working (shimmer)",
                Kind::Shimmer,
                Inputs {
                    history: &[],
                    level_in: 0.0,
                    level_out: 0.0,
                },
            ),
            (
                "muted / ended (flat)",
                Kind::Flat,
                Inputs {
                    history: &[],
                    level_in: 0.0,
                    level_out: 0.0,
                },
            ),
        ] {
            out += &format!("== {name}, 14 cells, one row per 4th frame at 30 Hz\n");
            for f in (0..48).step_by(4) {
                let p = Palette::new(false);
                out += &format!(
                    "f{f:02}  {}   ascii {}\n",
                    text(&band(kind, 14, f, true, false, p.accent, &p, &input)),
                    text(&band(kind, 14, f, true, true, p.accent, &p, &input))
                );
            }
            out += "\n";
        }
        let dir = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../artifacts/live-panel-anim"
        );
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(format!("{dir}/bands.txt"), out).unwrap();
    }
}
