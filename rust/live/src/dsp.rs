//! Audio math with no device or network dependencies: RMS, the bounded sample
//! ring, a streaming linear resampler and the half-duplex echo gate.

use std::collections::VecDeque;
use std::sync::OnceLock;
use std::time::Instant;

pub const SAMPLE_RATE: u32 = 48_000;
/// 20 ms at 48 kHz mono.
pub const FRAME: usize = 960;
/// Input ring capacity: 200 ms. The capture ring keeps the newest audio when
/// it overflows, so this bounds how stale transmitted mic audio can be.
pub const INPUT_RING_SAMPLES: usize = SAMPLE_RATE as usize / 5;
/// Playback buffer capacity: 200 ms (oldest audio is dropped beyond it). Large
/// enough to ride out normal scheduling jitter, small enough that a backlog
/// never delays the assistant's voice noticeably.
pub const PLAYBACK_SAMPLES: usize = 9_600;

/// Milliseconds since the first call in this process (monotonic).
pub fn now_ms() -> u64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_millis() as u64
}

/// Normalised RMS in 0.0..=1.0.
pub fn rms_i16(samples: &[i16]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum: f64 = samples
        .iter()
        .map(|s| {
            let v = *s as f64 / 32768.0;
            v * v
        })
        .sum();
    (sum / samples.len() as f64).sqrt() as f32
}

/// Bounded FIFO of mono 48 kHz samples that counts what it had to drop.
#[derive(Debug)]
pub struct SampleRing {
    buf: VecDeque<i16>,
    cap: usize,
    dropped_samples: u64,
    reported_samples: u64,
}

impl SampleRing {
    pub fn new(cap: usize) -> Self {
        Self { buf: VecDeque::new(), cap, dropped_samples: 0, reported_samples: 0 }
    }

    pub fn len(&self) -> usize {
        self.buf.len()
    }

    /// Drop everything buffered (pause discards audio the voice was still sending).
    pub fn clear(&mut self) {
        self.buf.clear();
    }

    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }

    /// Append, dropping (and counting) the samples that do not fit. Returns
    /// the number dropped. Keeps the oldest audio: a stalled consumer should
    /// not make the ring grow, and the newest data is what is discarded.
    pub fn push(&mut self, samples: &[i16]) -> usize {
        let room = self.cap - self.buf.len();
        let take = room.min(samples.len());
        self.buf.extend(&samples[..take]);
        let dropped = samples.len() - take;
        self.dropped_samples += dropped as u64;
        dropped
    }

    /// Append, discarding the oldest samples to make room (low-latency
    /// playback and mic capture). Returns the number dropped.
    pub fn push_keep_latest(&mut self, samples: &[i16]) -> usize {
        let before = self.dropped_samples;
        self.push_drop_oldest(samples);
        (self.dropped_samples - before) as usize
    }

    /// Append, discarding the oldest samples to make room (low-latency playback).
    pub fn push_drop_oldest(&mut self, samples: &[i16]) {
        // Samples cut from the front of an oversized input count as dropped too.
        self.dropped_samples += samples.len().saturating_sub(self.cap) as u64;
        let samples = if samples.len() > self.cap { &samples[samples.len() - self.cap..] } else { samples };
        let over = (self.buf.len() + samples.len()).saturating_sub(self.cap);
        for _ in 0..over {
            self.buf.pop_front();
        }
        self.dropped_samples += over as u64;
        self.buf.extend(samples);
    }

    pub fn pop_frame(&mut self, n: usize) -> Option<Vec<i16>> {
        if self.buf.len() < n {
            return None;
        }
        Some(self.buf.drain(..n).collect())
    }

    pub fn pop_upto(&mut self, n: usize) -> Vec<i16> {
        let n = n.min(self.buf.len());
        self.buf.drain(..n).collect()
    }

    /// Whole frames dropped since the last call (remainders carry over).
    pub fn take_dropped_frames(&mut self) -> u64 {
        let pending = self.dropped_samples - self.reported_samples;
        let frames = pending / FRAME as u64;
        self.reported_samples += frames * FRAME as u64;
        frames
    }
}

/// Streaming linear-interpolation resampler for mono f32 audio.
#[derive(Debug, Clone)]
pub struct LinearResampler {
    step: f64,
    pos: f64,
    prev: f32,
}

impl LinearResampler {
    pub fn new(from_hz: u32, to_hz: u32) -> Self {
        Self { step: from_hz as f64 / to_hz as f64, pos: 0.0, prev: 0.0 }
    }

    pub fn process(&mut self, input: &[f32], out: &mut Vec<f32>) {
        if input.is_empty() {
            return;
        }
        let ext_len = input.len() + 1;
        let at = |i: usize| if i == 0 { self.prev } else { input[i - 1] };
        while (self.pos.floor() as usize) + 1 < ext_len {
            let i = self.pos.floor() as usize;
            let frac = (self.pos - i as f64) as f32;
            let (a, b) = (at(i), at(i + 1));
            out.push(a + (b - a) * frac);
            self.pos += self.step;
        }
        self.pos -= (ext_len - 1) as f64;
        self.prev = input[input.len() - 1];
    }
}

pub fn i16_to_f32(s: i16) -> f32 {
    s as f32 / 32768.0
}

pub fn f32_to_i16(s: f32) -> i16 {
    (s.clamp(-1.0, 1.0) * 32767.0) as i16
}

#[derive(Debug, Clone, Copy)]
pub struct GateConfig {
    /// Output RMS above which the assistant counts as speaking.
    pub out_threshold: f32,
    /// Mic RMS above which the mic is let through even while it speaks.
    pub barge_in: f32,
    /// How long the gate stays closed after the output goes quiet.
    pub hangover_ms: u64,
}

impl Default for GateConfig {
    fn default() -> Self {
        Self { out_threshold: 0.01, barge_in: 0.12, hangover_ms: 300 }
    }
}

/// Half-duplex echo gate: while the speaker is active (plus a hangover) mic
/// frames are suppressed unless they are loud enough to be a barge-in.
#[derive(Debug)]
pub struct EchoGate {
    cfg: GateConfig,
    last_active_ms: Option<u64>,
}

impl EchoGate {
    pub fn new(cfg: GateConfig) -> Self {
        Self { cfg, last_active_ms: None }
    }

    pub fn observe_output(&mut self, now_ms: u64, out_rms: f32) {
        if out_rms > self.cfg.out_threshold {
            self.last_active_ms = Some(now_ms);
        }
    }

    /// The assistant is audible (or just stopped within the hangover).
    pub fn speaking(&self, now_ms: u64) -> bool {
        self.last_active_ms.is_some_and(|t| now_ms.saturating_sub(t) < self.cfg.hangover_ms)
    }

    pub fn allow_mic(&self, now_ms: u64, mic_rms: f32) -> bool {
        !self.speaking(now_ms) || mic_rms > self.cfg.barge_in
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rms_of_silence_and_full_scale() {
        assert_eq!(rms_i16(&[]), 0.0);
        assert_eq!(rms_i16(&[0; 960]), 0.0);
        let r = rms_i16(&[i16::MAX, i16::MIN + 1, i16::MAX, i16::MIN + 1]);
        assert!((r - 1.0).abs() < 0.01);
    }

    #[test]
    fn ring_overflow_is_counted_in_whole_frames() {
        let mut r = SampleRing::new(INPUT_RING_SAMPLES);
        assert_eq!(r.push(&vec![1; INPUT_RING_SAMPLES]), 0);
        assert_eq!(r.len(), INPUT_RING_SAMPLES);
        // 2.5 frames over capacity: two whole frames reported, half carried.
        assert_eq!(r.push(&vec![2; FRAME * 2 + FRAME / 2]), FRAME * 2 + FRAME / 2);
        assert_eq!(r.take_dropped_frames(), 2);
        assert_eq!(r.take_dropped_frames(), 0);
        r.push(&vec![2; FRAME / 2]);
        assert_eq!(r.take_dropped_frames(), 1);
        // Oldest audio is preserved.
        assert_eq!(r.pop_frame(FRAME).unwrap(), vec![1; FRAME]);
        assert_eq!(r.len(), INPUT_RING_SAMPLES - FRAME);
        assert!(SampleRing::new(10).pop_frame(11).is_none());
    }

    #[test]
    fn capture_overflow_keeps_newest_and_counts_drops() {
        let mut r = SampleRing::new(INPUT_RING_SAMPLES);
        assert_eq!(r.push_keep_latest(&vec![1; INPUT_RING_SAMPLES]), 0);
        assert_eq!(r.push_keep_latest(&vec![2; FRAME]), FRAME);
        assert_eq!(r.len(), INPUT_RING_SAMPLES);
        assert_eq!(r.take_dropped_frames(), 1);
        // The oldest frame was evicted; the newest is at the tail.
        let all = r.pop_upto(INPUT_RING_SAMPLES);
        assert!(all[..INPUT_RING_SAMPLES - FRAME].iter().all(|s| *s == 1));
        assert!(all[INPUT_RING_SAMPLES - FRAME..].iter().all(|s| *s == 2));
    }

    #[test]
    fn oversized_push_keeps_only_the_tail_and_counts_everything_cut() {
        let mut r = SampleRing::new(4);
        r.push_keep_latest(&[1, 2]);
        // Cut: incoming [3, 4] (oversized head) plus evicted [1, 2] = 4.
        assert_eq!(r.push_keep_latest(&[3, 4, 5, 6, 7, 8]), 2 + 2);
        assert_eq!(r.pop_upto(10), vec![5, 6, 7, 8]);
    }

    #[test]
    fn buffer_bounds_stay_low_latency() {
        assert_eq!(PLAYBACK_SAMPLES, 200 * SAMPLE_RATE as usize / 1000);
        assert!(INPUT_RING_SAMPLES <= 200 * SAMPLE_RATE as usize / 1000);
    }

    #[test]
    fn playback_drop_oldest_keeps_newest() {
        let mut r = SampleRing::new(4);
        r.push_drop_oldest(&[1, 2, 3]);
        r.push_drop_oldest(&[4, 5, 6]);
        assert_eq!(r.pop_upto(10), vec![3, 4, 5, 6]);
    }

    #[test]
    fn resampler_changes_length_proportionally_and_keeps_dc() {
        for (from, to) in [(44_100u32, 48_000u32), (48_000, 16_000), (48_000, 48_000), (16_000, 48_000)] {
            let mut rs = LinearResampler::new(from, to);
            let mut out = Vec::new();
            for _ in 0..50 {
                rs.process(&vec![0.5f32; from as usize / 100], &mut out);
            }
            let expected = (to as usize / 100) * 50;
            assert!((out.len() as i64 - expected as i64).abs() <= 3, "{from}->{to}: {} vs {expected}", out.len());
            assert!(out.iter().skip(4).all(|v| (v - 0.5).abs() < 1e-4));
        }
    }

    #[test]
    fn resampler_preserves_a_low_tone() {
        let mut rs = LinearResampler::new(44_100, 48_000);
        let input: Vec<f32> = (0..4410)
            .map(|i| (2.0 * std::f32::consts::PI * 440.0 * i as f32 / 44_100.0).sin() * 0.5)
            .collect();
        let mut out = Vec::new();
        rs.process(&input, &mut out);
        let rms = (out.iter().map(|v| v * v).sum::<f32>() / out.len() as f32).sqrt();
        assert!((rms - 0.3535).abs() < 0.02, "rms {rms}");
    }

    #[test]
    fn gate_suppresses_during_output_and_hangover_but_allows_barge_in() {
        let mut g = EchoGate::new(GateConfig::default());
        assert!(g.allow_mic(0, 0.0));
        g.observe_output(1000, 0.2);
        assert!(g.speaking(1000));
        assert!(!g.allow_mic(1000, 0.05));
        assert!(g.allow_mic(1000, 0.5), "loud mic is a barge-in");
        // Output goes quiet: still closed through the hangover.
        g.observe_output(1100, 0.0);
        assert!(!g.allow_mic(1299, 0.05));
        assert!(g.allow_mic(1300, 0.05));
        assert!(!g.speaking(1300));
        // Quiet output never opens the gate.
        g.observe_output(5000, 0.005);
        assert!(g.allow_mic(5000, 0.05));
    }
}
