//! Bounded, presentation-only animation: hover blend and toast timers.
//! Instants are passed in so tests use a fake clock.
use std::time::{Duration, Instant};

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

/// Which spinner frame to show; 80 ms per frame.
pub fn frame(now: Instant, start: Instant, count: usize) -> usize {
    if count == 0 {
        return 0;
    }
    (now.saturating_duration_since(start).as_millis() / 80) as usize % count
}
