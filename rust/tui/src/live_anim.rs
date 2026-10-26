//! The GPT Live panel's open and close motion.
//!
//! The panel lives between the composer card and the bottom bar. Opening grows its height from
//! 0 to full (`OPEN`, ease-out cubic) so it looks like it slides out from underneath the
//! composer, header first; a short accent sweep runs along the header while it opens. Closing
//! reverses quickly (`CLOSE`, ease-in) using a ghost of the last call state, drawn as `ended`.
//! The clock is the TUI's own `Instant` passes (the same loop that drives spinners): `apply`
//! returns whether another frame is needed and is false once the motion has settled. With
//! animations off the panel jumps straight to its final state.
use crate::bridge::{LiveBar, Snapshot};
use std::time::{Duration, Instant};

pub const OPEN: Duration = Duration::from_millis(260);
pub const CLOSE: Duration = Duration::from_millis(140);
/// The accent sweep along the header after the panel starts opening.
pub const SWEEP: Duration = Duration::from_millis(560);

pub fn ease_out_cubic(t: f32) -> f32 {
    1.0 - (1.0 - t.clamp(0.0, 1.0)).powi(3)
}
pub fn ease_in_quad(t: f32) -> f32 {
    let t = t.clamp(0.0, 1.0);
    t * t
}
/// Rows shown for a panel `target` rows tall at `progress` 0..1; any progress shows one row.
pub fn height_at(target: u16, progress: f32) -> u16 {
    if progress <= 0.0 {
        0
    } else {
        ((f32::from(target) * progress.min(1.0)).ceil() as u16).clamp(1, target.max(1))
    }
}

#[derive(Default)]
pub struct LiveAnim {
    open: bool,
    since: Option<Instant>,
    from: f32,
    opened: Option<Instant>,
    /// Natural (collapsed) height of the panel and the last call state, kept for the close.
    pub target: u16,
    ghost: Option<LiveBar>,
    was_in_flight: bool,
}

impl LiveAnim {
    /// Progress 0..1 of the panel's height at `now`.
    pub fn progress(&self, now: Instant) -> f32 {
        let Some(since) = self.since else {
            return if self.open { 1.0 } else { 0.0 };
        };
        let dur = if self.open { OPEN } else { CLOSE };
        let t = now.saturating_duration_since(since).as_secs_f32() / dur.as_secs_f32();
        if self.open {
            self.from + (1.0 - self.from) * ease_out_cubic(t)
        } else {
            self.from * (1.0 - ease_in_quad(t))
        }
    }
    fn height_in_flight(&self, now: Instant) -> bool {
        self.since.is_some_and(|since| {
            now.saturating_duration_since(since) < if self.open { OPEN } else { CLOSE }
        })
    }
    /// 0..1 position of the accent sweep, or `None` when it is not running.
    pub fn sweep(&self, now: Instant) -> Option<f32> {
        let age = now.saturating_duration_since(self.opened?);
        (self.open && age < SWEEP).then(|| age.as_secs_f32() / SWEEP.as_secs_f32())
    }
    /// The call state to draw while the panel closes.
    pub fn ghost(&self) -> Option<&LiveBar> {
        self.ghost.as_ref()
    }
    /// Update the animation for `now` and set `s.live_reveal`. Returns true while another frame
    /// is needed (including the one frame that draws the settled state).
    pub fn apply(&mut self, s: &mut Snapshot, now: Instant, animate: bool) -> bool {
        let page = s.dashboard.is_none() && s.agent_page.is_empty();
        let want = s.live.active && page;
        if want {
            self.target = crate::live_bar::pane_target(s);
            self.ghost = Some(LiveBar {
                phase: "ended".into(),
                muted: false,
                paused: false,
                ..s.live.clone()
            });
        }
        if want != self.open {
            let at = self.progress(now);
            self.open = want;
            if !animate || !page {
                self.since = None;
                self.from = if want { 1.0 } else { 0.0 };
            } else {
                self.from = at;
                self.since = Some(now);
            }
            self.opened = (want && animate).then_some(now);
            if !want && self.progress(now) <= 0.0 {
                self.ghost = None;
            }
        }
        let progress = self.progress(now);
        let flight = self.height_in_flight(now) || self.sweep(now).is_some();
        if !self.height_in_flight(now) {
            self.since = None;
            self.from = if self.open { 1.0 } else { 0.0 };
            if !self.open {
                self.ghost = None;
            }
        }
        s.live_reveal = if self.open && self.since.is_none() {
            None
        } else if progress > 0.0 && self.ghost.is_some() {
            Some(height_at(self.target, progress))
        } else {
            Some(0)
        };
        let redraw = flight || self.was_in_flight;
        self.was_in_flight = flight;
        redraw
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call() -> Snapshot {
        let mut s = Snapshot::default();
        s.live.active = true;
        s.live.detail = "voice\tGPT\trow".into();
        s
    }
    fn ms(base: Instant, n: u64) -> Instant {
        base + Duration::from_millis(n)
    }

    #[test]
    fn opening_grows_from_zero_to_full_with_ease_out() {
        let (mut a, base) = (LiveAnim::default(), Instant::now());
        let mut s = Snapshot::default();
        assert!(!a.apply(&mut s, base, true));
        assert_eq!(s.live_reveal, Some(0));
        let mut s = call();
        assert!(a.apply(&mut s, base, true));
        assert_eq!(s.live_reveal, Some(0), "t = 0 shows nothing");
        a.apply(&mut s, ms(base, 65), true);
        let quarter = s.live_reveal.unwrap();
        assert!(quarter > 3, "ease-out is fast at the start: {quarter}");
        a.apply(&mut s, ms(base, 100), true);
        let mid = s.live_reveal.unwrap();
        assert!(mid >= quarter && mid < crate::live_bar::PANE_ROWS, "{mid}");
        a.apply(&mut s, ms(base, 300), true);
        assert_eq!(s.live_reveal, None, "settled: the natural height");
    }

    #[test]
    fn settled_panel_stops_requesting_frames() {
        let (mut a, base) = (LiveAnim::default(), Instant::now());
        let mut s = call();
        a.apply(&mut s, base, true);
        assert!(a.apply(&mut s, ms(base, 100), true));
        assert!(a.apply(&mut s, ms(base, 300), true), "sweep still runs");
        assert!(
            a.apply(&mut s, ms(base, 600), true),
            "one frame draws the final state"
        );
        assert!(!a.apply(&mut s, ms(base, 700), true));
        assert!(!a.apply(&mut s, ms(base, 5000), true));
    }

    #[test]
    fn reduced_motion_jumps_to_the_final_state() {
        let (mut a, base) = (LiveAnim::default(), Instant::now());
        let mut s = call();
        a.apply(&mut s, base, false);
        assert_eq!(s.live_reveal, None);
        assert!(!a.apply(&mut s, ms(base, 1), false));
        s.live.active = false;
        assert!(!a.apply(&mut s, ms(base, 2), false));
        assert_eq!(s.live_reveal, Some(0), "closes at once too");
        assert!(a.ghost().is_none());
    }

    #[test]
    fn closing_shrinks_quickly_from_a_ghost_of_the_call() {
        let (mut a, base) = (LiveAnim::default(), Instant::now());
        let mut s = call();
        a.apply(&mut s, base, true);
        a.apply(&mut s, ms(base, 400), true);
        s.live.active = false;
        s.live.detail.clear();
        a.apply(&mut s, ms(base, 410), true);
        assert_eq!(s.live_reveal, Some(crate::live_bar::PANE_ROWS));
        assert_eq!(a.ghost().map(|g| g.phase.as_str()), Some("ended"));
        a.apply(&mut s, ms(base, 480), true);
        let mid = s.live_reveal.unwrap();
        assert!(mid > 0 && mid < crate::live_bar::PANE_ROWS, "{mid}");
        a.apply(&mut s, ms(base, 600), true);
        assert_eq!(s.live_reveal, Some(0));
        assert!(a.ghost().is_none());
    }

    #[test]
    fn height_helpers() {
        assert_eq!(height_at(6, 0.0), 0);
        assert_eq!(height_at(6, 0.01), 1);
        assert_eq!(height_at(6, 1.0), 6);
        assert!(ease_out_cubic(0.5) > 0.5 && ease_out_cubic(1.0) == 1.0);
    }
}
