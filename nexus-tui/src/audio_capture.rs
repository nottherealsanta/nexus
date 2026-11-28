//! Continuous microphone capture primitives shared by the TUI's voice features.
//!
//! The device is opened on a dedicated capture thread, and every callback block
//! is downmixed to mono, resampled to [`TARGET_RATE`] and handed to a
//! [`FrameSink`] as it arrives. Dictation caps its own sink at two minutes; a
//! long-running recorder can use the same primitives without a cap.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait};
use cpal::Sample;

pub const TARGET_RATE: u32 = 16_000;
/// Bounded queue between the audio callback and the recorder capture thread.
pub(crate) const RECORDER_INPUT_QUEUE_BLOCKS: usize = 64;
/// How long a replacement dictation/Manager capture waits for the retired
/// worker to drop its guard. This runs on the capture thread, never the UI.
pub const DICTATION_RELEASE_TIMEOUT: Duration = Duration::from_millis(500);
pub const DICTATION_RELEASE_STEP: Duration = Duration::from_millis(10);

/// The three TUI features that can hold the microphone. Dictation, the
/// Manager's GPT-Live call and the standalone recorder share one process-wide
/// owner so two features can never open the device at the same time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MicOwner {
    Dictation,
    Manager,
    Recorder,
}

impl MicOwner {
    pub fn label(self) -> &'static str {
        match self {
            Self::Dictation => "dictation",
            Self::Manager => "the Manager call",
            Self::Recorder => "the recorder",
        }
    }
}

struct MicState {
    owner: MicOwner,
    generation: u64,
}

static MIC_STATE: Mutex<Option<MicState>> = Mutex::new(None);
static MIC_GENERATION: AtomicU64 = AtomicU64::new(1);
#[cfg(test)]
pub(crate) static MIC_TEST_LOCK: Mutex<()> = Mutex::new(());

/// Serialize tests that touch the process-wide microphone owner. This also
/// clears a state left by an earlier panicking test.
#[cfg(test)]
pub(crate) fn mic_test_guard() -> std::sync::MutexGuard<'static, ()> {
    let guard = MIC_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    MIC_STATE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .take();
    guard
}

/// RAII microphone ownership. Drop releases only the exact generation it
/// acquired, so a delayed/stale guard can never free a newer owner.
#[must_use = "dropping the guard releases the microphone immediately"]
pub struct MicGuard {
    owner: MicOwner,
    generation: u64,
}

impl MicGuard {
    pub fn owner(&self) -> MicOwner {
        self.owner
    }
}

impl Drop for MicGuard {
    fn drop(&mut self) {
        let mut state = MIC_STATE
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state
            .as_ref()
            .is_some_and(|current| current.generation == self.generation)
        {
            *state = None;
        }
    }
}

impl std::fmt::Debug for MicGuard {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MicGuard")
            .field("owner", &self.owner)
            .finish()
    }
}

/// Acquire exclusive microphone ownership. The device must not be opened unless
/// this returns a guard; hold it for the whole capture lifetime.
pub fn acquire_mic(owner: MicOwner) -> Result<MicGuard, String> {
    let mut state = MIC_STATE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if let Some(current) = state.as_ref() {
        return Err(format!(
            "The microphone is already in use by {}.",
            current.owner.label()
        ));
    }
    let generation = MIC_GENERATION.fetch_add(1, Ordering::SeqCst);
    *state = Some(MicState { owner, generation });
    Ok(MicGuard { owner, generation })
}

/// Current owner, for status/errors. Never opens the device.
/// Bounded handoff for a new capture that replaces a retired dictation worker.
/// The old worker may still be releasing the device; wait only while it is
/// dictation. Any other owner (recorder or Manager live) fails immediately.
/// Runs on a capture/device thread, never the UI thread.
pub fn acquire_mic_after_dictation(
    owner: MicOwner,
    timeout: Duration,
    step: Duration,
) -> Result<MicGuard, String> {
    let deadline = Instant::now() + timeout;
    loop {
        match acquire_mic(owner) {
            Ok(guard) => return Ok(guard),
            Err(error) => {
                if current_mic_owner() != Some(MicOwner::Dictation) {
                    return Err(error);
                }
                if Instant::now() >= deadline {
                    return Err(format!(
                        "{error} The previous dictation capture is still closing."
                    ));
                }
                std::thread::sleep(step.min(deadline.saturating_duration_since(Instant::now())));
            }
        }
    }
}

pub fn current_mic_owner() -> Option<MicOwner> {
    MIC_STATE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .as_ref()
        .map(|state| state.owner)
}

/// Smoothed RMS 0..1 for the UI level meters. Call once per delivered frame
/// block; the release is slower than the attack so brief syllables stay visible.
pub fn smoothed_level(previous: f32, rms: f32) -> f32 {
    let previous = previous.clamp(0.0, 1.0);
    let rms = rms.clamp(0.0, 1.0);
    let factor = if rms > previous { 0.55 } else { 0.2 };
    (previous + (rms - previous) * factor).clamp(0.0, 1.0)
}

pub(crate) enum AudioMessage {
    Samples(Vec<f32>),
    Failed(String),
}

/// Bounded recorder input. The callback only ever calls `try_send`; when the
/// queue is full it records the dropped sample count and never blocks or grows.
pub(crate) enum BoundedAudioMessage {
    Samples(Vec<f32>),
}

/// Receives mono frames at [`TARGET_RATE`] in capture order.
pub trait FrameSink: Send {
    fn push_frame(&mut self, frames: &[f32]);
}

impl<F> FrameSink for F
where
    F: FnMut(&[f32]) + Send,
{
    fn push_frame(&mut self, frames: &[f32]) {
        self(frames);
    }
}

/// Stateful linear resampling of interleaved audio to a mono 16 kHz stream.
#[derive(Debug, Clone)]
pub struct Resampler {
    target_rate: u32,
    source_rate: u32,
    channels: usize,
    frames_seen: u64,
    next_output: u64,
    previous: Option<f32>,
}

impl Resampler {
    pub fn new(source_rate: u32, channels: usize) -> Option<Self> {
        Self::to_rate(source_rate, channels, TARGET_RATE)
    }

    /// Like `new`, to mono at `target_rate` (the GPT-Live call sends 24 kHz).
    pub fn to_rate(source_rate: u32, channels: usize, target_rate: u32) -> Option<Self> {
        (source_rate > 0 && channels > 0 && target_rate > 0).then_some(Self {
            target_rate,
            source_rate,
            channels,
            frames_seen: 0,
            next_output: 0,
            previous: None,
        })
    }

    pub fn push(&mut self, interleaved: &[f32]) -> Vec<f32> {
        let mono: Vec<f32> = interleaved
            .chunks_exact(self.channels)
            .map(|frame| (frame.iter().sum::<f32>() / self.channels as f32).clamp(-1.0, 1.0))
            .collect();
        if mono.is_empty() {
            return Vec::new();
        }
        let start = self.frames_seen;
        let end = start + mono.len() as u64 - 1;
        let mut out = Vec::new();
        loop {
            let numerator = self.next_output * u64::from(self.source_rate);
            let base = numerator / u64::from(self.target_rate);
            let remainder = numerator % u64::from(self.target_rate);
            if base > end || (base == end && remainder > 0) {
                break;
            }
            let fraction = remainder as f32 / self.target_rate as f32;
            let sample = if base < start {
                let Some(previous) = self.previous else {
                    break;
                };
                previous + (mono[0] - previous) * fraction
            } else {
                let index = (base - start) as usize;
                if index + 1 < mono.len() {
                    mono[index] + (mono[index + 1] - mono[index]) * fraction
                } else {
                    mono[index]
                }
            };
            out.push(sample.clamp(-1.0, 1.0));
            self.next_output += 1;
        }
        self.frames_seen += mono.len() as u64;
        self.previous = mono.last().copied();
        out
    }
}

/// One continuous capture: resamples incoming device blocks and forwards the
/// mono frames to its sink, carrying resampler phase across block boundaries.
#[derive(Debug)]
pub struct FramePump<S> {
    resampler: Resampler,
    sink: S,
}

impl<S: FrameSink> FramePump<S> {
    pub fn new(source_rate: u32, channels: usize, sink: S) -> Option<Self> {
        Some(Self {
            resampler: Resampler::new(source_rate, channels)?,
            sink,
        })
    }

    /// Resample one interleaved callback block into the sink. Empty output is
    /// not delivered.
    pub fn push_block(&mut self, interleaved: &[f32]) {
        let frames = self.resampler.push(interleaved);
        if !frames.is_empty() {
            self.sink.push_frame(&frames);
        }
    }

    pub fn sink(&self) -> &S {
        &self.sink
    }

    pub fn sink_mut(&mut self) -> &mut S {
        &mut self.sink
    }

    pub fn into_sink(self) -> S {
        self.sink
    }
}

pub(crate) fn open_input(
    audio: std_mpsc::Sender<AudioMessage>,
) -> Result<(cpal::Stream, u32, usize), String> {
    open_input_with(audio.clone(), audio)
}

/// Recorder input with a bounded callback queue. Overflow and non-fatal device
/// discontinuities are reported through the shared counters so the recorder can
/// stop and represent the loss instead of silently continuing.
pub(crate) fn open_input_bounded(
    audio: std_mpsc::SyncSender<BoundedAudioMessage>,
    overflow_blocks: Arc<AtomicU64>,
    failure: Arc<OnceLock<String>>,
    discontinuities: Arc<AtomicU64>,
    invalid: Arc<AtomicBool>,
) -> Result<(cpal::Stream, u32, usize), String> {
    let sink = BoundedAudioSink {
        audio,
        overflow_blocks,
        failure,
        discontinuities,
        invalid,
    };
    open_input_with(sink.clone(), sink)
}

fn open_input_with<S>(samples_tx: S, errors_tx: S) -> Result<(cpal::Stream, u32, usize), String>
where
    S: AudioSink,
{
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| "No microphone input device is available".to_owned())?;
    let supported = device
        .default_input_config()
        .map_err(|e| format!("No microphone input device is available: {e}"))?;
    let format = supported.sample_format();
    let config = supported.config();
    let rate = config.sample_rate;
    let channels = config.channels as usize;
    let stream = match format {
        cpal::SampleFormat::F32 => input_stream::<f32, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::F64 => input_stream::<f64, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::I8 => input_stream::<i8, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::I16 => input_stream::<i16, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::I24 => {
            input_stream::<cpal::I24, _>(&device, config, samples_tx, errors_tx)
        }
        cpal::SampleFormat::I32 => input_stream::<i32, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::I64 => input_stream::<i64, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::U8 => input_stream::<u8, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::U16 => input_stream::<u16, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::U24 => {
            input_stream::<cpal::U24, _>(&device, config, samples_tx, errors_tx)
        }
        cpal::SampleFormat::U32 => input_stream::<u32, _>(&device, config, samples_tx, errors_tx),
        cpal::SampleFormat::U64 => input_stream::<u64, _>(&device, config, samples_tx, errors_tx),
        other => return Err(format!("Unsupported microphone sample format: {other}")),
    }
    .map_err(|e| format!("Could not open microphone input: {e}"))?;
    Ok((stream, rate, channels))
}

trait AudioSink: Clone + Send + 'static {
    fn samples(&self, samples: Vec<f32>);
    fn failed(&self, message: String);
    fn discontinuity(&self, _message: String) {}
}

impl AudioSink for std_mpsc::Sender<AudioMessage> {
    fn samples(&self, samples: Vec<f32>) {
        let _ = self.send(AudioMessage::Samples(samples));
    }

    fn failed(&self, message: String) {
        let _ = self.send(AudioMessage::Failed(message));
    }
}

#[derive(Clone)]
struct BoundedAudioSink {
    audio: std_mpsc::SyncSender<BoundedAudioMessage>,
    /// Dropped callback blocks. The callback data is raw interleaved
    /// device-rate audio, so its value count is not a 16 kHz mono sample count.
    overflow_blocks: Arc<AtomicU64>,
    failure: Arc<OnceLock<String>>,
    discontinuities: Arc<AtomicU64>,
    /// Latched for the rest of this capture once anything makes the timeline
    /// invalid. Later callback blocks are dropped, never enqueued after loss.
    invalid: Arc<AtomicBool>,
}

impl AudioSink for BoundedAudioSink {
    fn samples(&self, samples: Vec<f32>) {
        if self.invalid.load(Ordering::Relaxed) {
            self.overflow_blocks.fetch_add(1, Ordering::Relaxed);
            return;
        }
        if let Err(std_mpsc::TrySendError::Full(_)) =
            self.audio.try_send(BoundedAudioMessage::Samples(samples))
        {
            self.invalid.store(true, Ordering::Relaxed);
            self.overflow_blocks.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn failed(&self, message: String) {
        self.invalid.store(true, Ordering::Relaxed);
        let _ = self.failure.set(message);
    }

    fn discontinuity(&self, _message: String) {
        self.invalid.store(true, Ordering::Relaxed);
        self.discontinuities.fetch_add(1, Ordering::Relaxed);
    }
}

fn input_stream<T, S>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    samples_tx: S,
    errors_tx: S,
) -> Result<cpal::Stream, cpal::Error>
where
    T: cpal::SizedSample + Copy,
    f32: cpal::FromSample<T>,
    S: AudioSink,
{
    device.build_input_stream::<T, _, _>(
        config,
        move |data, _| {
            let samples = data
                .iter()
                .map(|sample| f32::from_sample(*sample))
                .collect();
            samples_tx.samples(samples);
        },
        move |error: cpal::Error| {
            let kind = error.kind();
            if stream_error_is_fatal(kind) {
                errors_tx.failed(error.to_string());
            } else if stream_error_is_discontinuity(kind) {
                errors_tx.discontinuity(error.to_string());
            }
        },
        None,
    )
}

/// Whether a stream error callback means capture has stopped. A buffer xrun, a route
/// change cpal already followed, or a refused real-time promotion is a glitch at worst:
/// the stream keeps delivering audio, so dictation must not abort on it.
pub(crate) fn stream_error_is_fatal(kind: cpal::ErrorKind) -> bool {
    !matches!(
        kind,
        cpal::ErrorKind::Xrun | cpal::ErrorKind::DeviceChanged | cpal::ErrorKind::RealtimeDenied
    )
}

/// An xrun or route change invalidates the exact sample timeline: audio may be
/// missing. Dictation ignores this for continuity, but the recorder must report
/// an unknown discontinuity and stop rather than pretend the samples arrived.
pub(crate) fn stream_error_is_discontinuity(kind: cpal::ErrorKind) -> bool {
    matches!(kind, cpal::ErrorKind::Xrun | cpal::ErrorKind::DeviceChanged)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    struct Collector(Vec<f32>);

    impl FrameSink for Collector {
        fn push_frame(&mut self, frames: &[f32]) {
            self.0.extend_from_slice(frames);
        }
    }

    #[test]
    fn mic_ownership_is_exclusive_and_released_by_raii() {
        let _test = mic_test_guard();
        let guard = acquire_mic(MicOwner::Recorder).unwrap();
        assert_eq!(current_mic_owner(), Some(MicOwner::Recorder));
        let error = acquire_mic(MicOwner::Dictation).unwrap_err();
        assert!(error.contains("recorder"));
        drop(guard);
        assert_eq!(current_mic_owner(), None);
        let dictation = acquire_mic(MicOwner::Dictation).unwrap();
        assert_eq!(current_mic_owner(), Some(MicOwner::Dictation));
        drop(dictation);
    }

    #[test]
    fn concurrent_mic_acquisition_has_exactly_one_winner() {
        let _test = mic_test_guard();
        let start = Arc::new(std::sync::Barrier::new(8));
        let held = Arc::new(std::sync::Barrier::new(8));
        let mut threads = Vec::new();
        for _ in 0..8 {
            let start = start.clone();
            let held = held.clone();
            threads.push(std::thread::spawn(move || {
                start.wait();
                let guard = acquire_mic(MicOwner::Manager).ok();
                held.wait();
                guard.is_some()
            }));
        }
        let winners = threads
            .into_iter()
            .map(|thread| thread.join().unwrap())
            .filter(|won| *won)
            .count();
        assert_eq!(winners, 1);
        // The winner thread dropped its guard, so the next acquisition works.
        let guard = acquire_mic(MicOwner::Recorder).unwrap();
        drop(guard);
    }

    #[test]
    fn a_stale_guard_never_releases_a_newer_owner() {
        let _test = mic_test_guard();
        let stale = acquire_mic(MicOwner::Recorder).unwrap();
        {
            let mut state = MIC_STATE.lock().unwrap();
            let generation = MIC_GENERATION.fetch_add(1, Ordering::SeqCst);
            *state = Some(MicState {
                owner: MicOwner::Manager,
                generation,
            });
        }
        drop(stale);
        assert_eq!(current_mic_owner(), Some(MicOwner::Manager));
        MIC_STATE.lock().unwrap().take();
    }

    #[test]
    fn manager_waits_boundedly_for_a_retired_dictation_worker() {
        let _test = mic_test_guard();
        let held = acquire_mic(MicOwner::Dictation).unwrap();
        let waiter = std::thread::spawn(move || {
            acquire_mic_after_dictation(
                MicOwner::Manager,
                Duration::from_millis(500),
                Duration::from_millis(5),
            )
        });
        std::thread::sleep(Duration::from_millis(30));
        drop(held);
        let guard = waiter.join().unwrap().unwrap();
        assert_eq!(guard.owner(), MicOwner::Manager);
        drop(guard);
    }

    #[test]
    fn handoff_does_not_wait_for_a_different_owner() {
        let _test = mic_test_guard();
        let held = acquire_mic(MicOwner::Recorder).unwrap();
        let started = Instant::now();
        let error = acquire_mic_after_dictation(
            MicOwner::Manager,
            Duration::from_secs(1),
            Duration::from_millis(5),
        )
        .unwrap_err();
        assert!(started.elapsed() < Duration::from_millis(100));
        assert!(error.contains("recorder"), "{error}");
        drop(held);
    }

    #[test]
    fn dictation_handoff_times_out_with_a_clear_message() {
        let _test = mic_test_guard();
        let _held = acquire_mic(MicOwner::Dictation).unwrap();
        let error = acquire_mic_after_dictation(
            MicOwner::Dictation,
            Duration::from_millis(20),
            Duration::from_millis(2),
        )
        .unwrap_err();
        assert!(error.contains("still closing"), "{error}");
    }

    #[test]
    fn level_smoothing_attacks_fast_and_releases_slower() {
        let attack = smoothed_level(0.0, 0.5);
        let release = smoothed_level(0.5, 0.0);
        // Both move toward the new RMS; attack covers a larger fraction of the
        // starting gap than release.
        assert!(attack > 0.5 - release);
        assert!((attack - 0.275).abs() < 0.001);
        assert!((release - 0.4).abs() < 0.001);
    }

    #[test]
    fn stream_glitches_do_not_abort_capture() {
        assert!(!stream_error_is_fatal(cpal::ErrorKind::Xrun));
        assert!(!stream_error_is_fatal(cpal::ErrorKind::DeviceChanged));
        assert!(!stream_error_is_fatal(cpal::ErrorKind::RealtimeDenied));
        assert!(stream_error_is_fatal(cpal::ErrorKind::DeviceNotAvailable));
        assert!(stream_error_is_fatal(cpal::ErrorKind::StreamInvalidated));
    }

    #[test]
    fn recorder_discontinuities_are_reported_but_dictation_ignores_them() {
        assert!(stream_error_is_discontinuity(cpal::ErrorKind::Xrun));
        assert!(stream_error_is_discontinuity(
            cpal::ErrorKind::DeviceChanged
        ));
        assert!(!stream_error_is_discontinuity(
            cpal::ErrorKind::RealtimeDenied
        ));
        assert!(!stream_error_is_discontinuity(
            cpal::ErrorKind::DeviceNotAvailable
        ));
    }

    #[test]
    fn bounded_recorder_input_counts_blocks_not_device_rate_values() {
        let (audio, receiver) = std_mpsc::sync_channel(1);
        let overflow_blocks = Arc::new(AtomicU64::new(0));
        let invalid = Arc::new(AtomicBool::new(false));
        let sink = BoundedAudioSink {
            audio,
            overflow_blocks: overflow_blocks.clone(),
            failure: Arc::new(OnceLock::new()),
            discontinuities: Arc::new(AtomicU64::new(0)),
            invalid: invalid.clone(),
        };

        // 48 kHz stereo interleaved values are not 16 kHz mono samples. The
        // bounded callback counts dropped blocks, never a fake sample total.
        sink.samples(vec![0.0; 48_000 * 2]);
        sink.samples(vec![0.0; 48_000 * 2]);
        // The gate is latched: subsequent post-loss callback blocks are also
        // dropped and counted, not queued for the consumer to accept.
        sink.samples(vec![0.0; 48_000 * 2]);

        assert!(invalid.load(Ordering::Relaxed));
        assert_eq!(overflow_blocks.load(Ordering::Relaxed), 2);
        match receiver.try_recv() {
            Ok(BoundedAudioMessage::Samples(samples)) => {
                assert_eq!(samples.len(), 48_000 * 2)
            }
            _ => panic!("the first block should remain queued"),
        }
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn frame_pump_delivers_downmixed_resampled_frames_to_its_sink() {
        let mut pump = FramePump::new(48_000, 2, Collector(Vec::new())).unwrap();
        let frames: Vec<f32> = (0..96_000)
            .flat_map(|i| {
                let sample = (i as f32 / 96_000.0 * 2.0 - 1.0).clamp(-1.0, 1.0);
                [sample, sample]
            })
            .collect();
        for chunk in frames.chunks(514) {
            pump.push_block(chunk);
        }
        let output = pump.into_sink().0;
        assert_eq!(output.len(), 32_000);
        assert!((*output.first().unwrap() + 1.0).abs() < 0.001);
        assert!((*output.last().unwrap() - 1.0).abs() < 0.001);
    }

    #[test]
    fn frame_pump_carries_phase_across_odd_blocks_for_a_simulated_hour() {
        // 44.1 kHz stereo in uneven blocks for an hour must yield exactly
        // 44.1 kHz / 16 kHz × 3600 s samples with no drift across boundaries.
        const SOURCE_RATE: u32 = 44_100;
        const SECONDS: usize = 3600;
        let mut pump = FramePump::new(SOURCE_RATE, 2, Collector(Vec::new())).unwrap();
        let total_frames = SOURCE_RATE as usize * SECONDS;
        let blocks = [257usize, 512, 1024, 63, 4096];
        let mut remaining = total_frames;
        let mut index = 0;
        while remaining > 0 {
            let frames = blocks[index % blocks.len()].min(remaining);
            index += 1;
            let block = vec![0.25f32; frames * 2];
            pump.push_block(&block);
            remaining -= frames;
        }
        assert_eq!(pump.into_sink().0.len(), TARGET_RATE as usize * SECONDS);
    }

    #[test]
    fn frame_pump_does_not_deliver_empty_frames() {
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = calls.clone();
        let mut pump = FramePump::new(48_000, 2, move |frames: &[f32]| {
            if !frames.is_empty() {
                counter.fetch_add(1, Ordering::SeqCst);
            }
        })
        .unwrap();
        for _ in 0..10 {
            pump.push_block(&[]);
        }
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        pump.push_block(&[0.0, 0.0]);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
}
