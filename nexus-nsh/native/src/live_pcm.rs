//! PCM framing for the native Manager audio bridge.
use std::collections::VecDeque;

const RATE: u32 = 24_000;
const FRAME_SAMPLES: usize = 480;
const PLAYBACK_CUSHION: usize = RATE as usize * 120 / 1000;
const PLAYBACK_LIMIT: usize = RATE as usize * 2;

/// Bounded playback in the device's format. A short cushion absorbs network bursts.
pub struct Playback {
    samples: VecDeque<f32>,
    rate: u32,
    channels: usize,
    phase: u32,
    started: bool,
    fade: usize,
}

impl Playback {
    pub fn new(rate: u32, channels: usize) -> Option<Self> {
        (rate > 0 && channels > 0).then_some(Self {
            samples: VecDeque::new(),
            rate,
            channels,
            phase: 0,
            started: false,
            fade: 0,
        })
    }

    pub fn push(&mut self, bytes: &[u8]) {
        for chunk in bytes.chunks_exact(2) {
            if self.samples.len() == PLAYBACK_LIMIT {
                self.samples.pop_front();
            }
            self.samples
                .push_back(i16::from_le_bytes([chunk[0], chunk[1]]) as f32 / 32768.0);
        }
    }

    pub fn clear(&mut self) {
        self.samples.clear();
        self.phase = 0;
        self.started = false;
        self.fade = 0;
    }

    pub fn fill(&mut self, output: &mut [f32]) -> f32 {
        let mut sum = 0.0;
        let mut count = 0;
        for frame in output.chunks_mut(self.channels) {
            if !self.started && self.samples.len() >= PLAYBACK_CUSHION {
                self.started = true;
                self.fade = 0;
            }
            let sample = if self.started {
                if let Some(&value) = self.samples.front() {
                    self.fade = (self.fade + 1).min((self.rate as usize / 200).max(1));
                    let value = value * self.fade as f32 / (self.rate as f32 / 200.0).max(1.0);
                    self.phase += RATE;
                    while self.phase >= self.rate {
                        self.samples.pop_front();
                        self.phase -= self.rate;
                    }
                    value
                } else {
                    self.started = false;
                    self.phase = 0;
                    0.0
                }
            } else {
                0.0
            };
            for channel in frame {
                *channel = sample;
            }
            sum += sample * sample;
            count += 1;
        }
        if count == 0 {
            0.0
        } else {
            (sum / count as f32).sqrt()
        }
    }
}

/// Converts interleaved microphone samples into 20 ms, mono PCM16-LE frames.
/// Sample positions are carried across calls so device callback boundaries do not change timing.
pub struct CaptureFrames {
    source_rate: u32,
    channels: usize,
    frames_seen: u64,
    next_output: u64,
    previous: Option<f32>,
    pending: Vec<i16>,
    partial: Vec<f32>,
}

impl CaptureFrames {
    pub fn new(source_rate: u32, channels: usize) -> Option<Self> {
        (source_rate > 0 && channels > 0).then_some(Self {
            source_rate,
            channels,
            frames_seen: 0,
            next_output: 0,
            previous: None,
            pending: Vec::with_capacity(FRAME_SAMPLES),
            partial: Vec::new(),
        })
    }

    pub fn push(&mut self, input: &[f32]) -> Vec<Vec<u8>> {
        self.partial.extend_from_slice(input);
        let complete = self.partial.len() / self.channels * self.channels;
        let mono: Vec<f32> = self.partial[..complete]
            .chunks_exact(self.channels)
            .map(|frame| (frame.iter().sum::<f32>() / self.channels as f32).clamp(-1.0, 1.0))
            .collect();
        self.partial.drain(..complete);
        if mono.is_empty() {
            return Vec::new();
        }
        let start = self.frames_seen;
        let end = start + mono.len() as u64 - 1;
        let mut frames = Vec::new();
        loop {
            let position = self.next_output * u64::from(self.source_rate);
            let base = position / u64::from(RATE);
            let remainder = position % u64::from(RATE);
            if base > end || (base == end && remainder > 0) {
                break;
            }
            let fraction = remainder as f32 / RATE as f32;
            let value = if base < start {
                let Some(previous) = self.previous else { break };
                previous + (mono[0] - previous) * fraction
            } else {
                let index = (base - start) as usize;
                if index + 1 < mono.len() {
                    mono[index] + (mono[index + 1] - mono[index]) * fraction
                } else {
                    mono[index]
                }
            };
            let value = value.clamp(-1.0, 1.0);
            self.pending.push(if value <= -1.0 {
                i16::MIN
            } else {
                (value * i16::MAX as f32).round() as i16
            });
            self.next_output += 1;
            if self.pending.len() == FRAME_SAMPLES {
                frames.push(self.pending.drain(..).flat_map(i16::to_le_bytes).collect());
            }
        }
        self.frames_seen += mono.len() as u64;
        self.previous = mono.last().copied();
        frames
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn playback_waits_for_cushion_and_clear_discards_audio() {
        let mut playback = Playback::new(48_000, 2).unwrap();
        let frame = vec![0xff, 0x3f].repeat(480);
        playback.push(&frame);
        let mut out = vec![1.0; 960];
        assert_eq!(playback.fill(&mut out), 0.0);
        assert!(out.iter().all(|v| *v == 0.0));
        for _ in 0..5 {
            playback.push(&frame);
        }
        assert!(playback.fill(&mut out) > 0.0);
        assert!(out.chunks_exact(2).all(|v| v[0] == v[1]));
        playback.clear();
        assert_eq!(playback.fill(&mut out), 0.0);
    }

    #[test]
    fn captures_20ms_pcm16_from_stereo_48khz() {
        let mut capture = CaptureFrames::new(48_000, 2).unwrap();
        let frames = capture.push(&[0.5; 1920]);
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].len(), 960);
        assert_eq!(i16::from_le_bytes([frames[0][0], frames[0][1]]), 16384);
    }

    #[test]
    fn callback_boundaries_do_not_change_samples() {
        let samples: Vec<f32> = (0..10_000)
            .map(|i| ((i % 83) as f32 / 41.0) - 1.0)
            .collect();
        let mut whole = CaptureFrames::new(44_100, 1).unwrap();
        let expected = whole.push(&samples);
        let mut chunked = CaptureFrames::new(44_100, 1).unwrap();
        let actual: Vec<_> = samples
            .chunks(137)
            .flat_map(|part| chunked.push(part))
            .collect();
        assert_eq!(actual, expected);
    }

    #[test]
    fn incomplete_interleaved_frames_are_kept_for_the_next_callback() {
        let samples: Vec<f32> = (0..6000).map(|i| (i % 10) as f32 / 10.0).collect();
        let expected = CaptureFrames::new(48_000, 2).unwrap().push(&samples);
        let mut chunked = CaptureFrames::new(48_000, 2).unwrap();
        let actual: Vec<_> = samples
            .chunks(137)
            .flat_map(|part| chunked.push(part))
            .collect();
        assert_eq!(actual, expected);
    }

    #[test]
    fn rejects_unusable_formats_and_clamps_samples() {
        assert!(CaptureFrames::new(0, 1).is_none());
        assert!(CaptureFrames::new(48_000, 0).is_none());
        let mut capture = CaptureFrames::new(RATE, 1).unwrap();
        let frames = capture.push(&[-2.0; FRAME_SAMPLES]);
        assert_eq!(i16::from_le_bytes([frames[0][0], frames[0][1]]), i16::MIN);
    }
}
