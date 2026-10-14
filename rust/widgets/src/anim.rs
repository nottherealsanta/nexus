//! Bounded, presentation-only animation: hover blend and toast timers.
//! Instants are passed in so tests use a fake clock.
use crate::theme::mix;
use ratatui::style::{Color, Style};
use ratatui::text::Span;
use std::time::{Duration, Instant};
use unicode_segmentation::UnicodeSegmentation;

pub const HOVER: Duration = Duration::from_millis(100);

/// 0.0..=1.0 blend that follows `hovered` over [`HOVER`].
#[derive(Clone, Copy, Debug)]
pub struct Blend {
    value: f32,
    at: Instant,
}
impl Blend {
    pub fn new(now: Instant) -> Self {
        Self { value: 0.0, at: now }
    }
    pub fn step(&mut self, hovered: bool, now: Instant) -> f32 {
        let dt = now.saturating_duration_since(self.at).as_secs_f32() / HOVER.as_secs_f32();
        self.at = now;
        self.value = if hovered { (self.value + dt).min(1.0) } else { (self.value - dt).max(0.0) };
        self.value
    }
    pub fn settled(&self, hovered: bool) -> bool {
        (hovered && self.value >= 1.0) || (!hovered && self.value <= 0.0)
    }
}

/// Wave speed in radians per tick (80 ms frames).
pub const WAVE_SPEED: f32 = 0.15;
/// Rows (columns for text) in one wave period.
pub const WAVE_ROWS: u16 = 32;
/// Speed of the "waiting on you" pulse.
pub const PULSE_SPEED: f32 = 0.08;

/// sin²(tick·speed + row/rows·2π) in 0..=1.
pub fn wave(tick: u64, row: u16, rows: u16, speed: f32) -> f32 {
    let phase = if rows == 0 { 0.0 } else { row as f32 / rows as f32 * std::f32::consts::TAU };
    // f64 keeps precision on long sessions.
    let t = tick as f64 * speed as f64 + phase as f64;
    (t.sin().powi(2) as f32).clamp(0.0, 1.0)
}

/// sin²(tick·speed) in 0..=1.
pub fn pulse(tick: u64, speed: f32) -> f32 {
    wave(tick, 0, 1, speed)
}

/// One span per grapheme; grapheme `i` is `mix(base, peak, wave(tick, i, WAVE_ROWS, WAVE_SPEED))`.
/// The concatenated text equals `text`.
pub fn shimmer_spans(text: &str, tick: u64, base: Color, peak: Color) -> Vec<Span<'static>> {
    text.graphemes(true)
        .enumerate()
        .map(|(i, g)| {
            let col = (i % WAVE_ROWS as usize) as u16;
            let c = mix(base, peak, wave(tick, col, WAVE_ROWS, WAVE_SPEED));
            Span::styled(g.to_string(), Style::default().fg(c))
        })
        .collect()
}

/// Which spinner frame to show; 80 ms per frame.
pub fn frame(now: Instant, start: Instant, count: usize) -> usize {
    if count == 0 {
        return 0;
    }
    (now.saturating_duration_since(start).as_millis() / 80) as usize % count
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wave_and_pulse_stay_in_range() {
        for tick in 0..500 {
            for row in 0..WAVE_ROWS {
                let w = wave(tick, row, WAVE_ROWS, WAVE_SPEED);
                assert!((0.0..=1.0).contains(&w), "{w}");
            }
            let p = pulse(tick, PULSE_SPEED);
            assert!((0.0..=1.0).contains(&p), "{p}");
        }
        assert!((0.0..=1.0).contains(&wave(7, 3, 0, 0.15)));
    }

    #[test]
    fn wave_is_periodic_in_rows_and_pulse_in_time() {
        // sin² has period π: speed π/10 repeats every 10 ticks.
        let speed = std::f32::consts::PI / 10.0;
        for tick in 0..20 {
            assert!((pulse(tick, speed) - pulse(tick + 10, speed)).abs() < 1e-4);
        }
        // Row phase wraps with the row count.
        assert!((wave(5, 0, 32, 0.15) - wave(5, 32, 32, 0.15)).abs() < 1e-4);
        assert_eq!(pulse(0, PULSE_SPEED), 0.0);
    }

    #[test]
    fn shimmer_keeps_text_one_span_per_grapheme() {
        let text = "ab e\u{301}東🙂";
        let spans = shimmer_spans(text, 3, Color::Rgb(0, 0, 0), Color::Rgb(255, 255, 255));
        assert_eq!(spans.len(), text.graphemes(true).count());
        let joined: String = spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(joined, text);
        assert!(shimmer_spans("", 0, Color::Reset, Color::Reset).is_empty());
    }
}
