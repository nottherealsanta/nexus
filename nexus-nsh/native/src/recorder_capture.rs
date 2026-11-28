//! Continuous 16 kHz mono PCM16 capture for the standalone recorder (plan 54,
//! milestone A2).
//!
//! A capture runs on its own cpal thread, so callbacks never touch the client's
//! UI thread. Audio is resampled and framed into 20 ms blocks (320 samples,
//! 640 bytes) and kept in a bounded 30 s ring. A slow consumer loses the
//! oldest complete frames; the loss is reported per read and cumulatively,
//! never hidden. Sample offsets are absolute, so a dropped region appears as a
//! jump in `sample_offset` and is explained by `dropped_samples`.
//!
//! Desktop targets with cpal (macOS, Linux, Windows) build the capture path.
//! iOS, Android and web report `available() == false`: the recorder is a
//! desktop feature. Only macOS is exercised in this repository so far; the
//! Linux and Windows paths are compile-time parity work, not a support claim.
//!
//! The capture holds a shared microphone lease from `mic_owner` from before
//! permission is asked until the device thread closes the stream. Dictation
//! and the Manager's live call take the same lease, so the three cannot overlap
//! and the check-and-claim has no race.
//!
//! `stop` snapshots a capture and the worker that owns it under one lock.
//! Only that worker marks the capture finished and releases the lease; every
//! concurrent stop waits for the same completion rather than closing early or
//! joining a worker installed by a later restart.

// The desktop capture path is compiled out on mobile and web; the module still
// registers there so the client can ask `available()`.
#![cfg_attr(
    not(any(target_os = "macos", target_os = "linux", target_os = "windows")),
    allow(dead_code)
)]

use std::collections::VecDeque;
use std::sync::{
    atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
    Arc, Condvar, Mutex,
};
use std::time::Duration;

use crate::mic_owner::{self, MicLease, MicOwner};

use nsh_core::prelude::*;

pub const SAMPLE_RATE: u32 = 16_000;
pub const FRAME_SAMPLES: usize = 320;
pub const FRAME_BYTES: usize = FRAME_SAMPLES * 2;
pub const FRAMES_PER_SECOND: u32 = SAMPLE_RATE / FRAME_SAMPLES as u32;
pub const MAX_BUFFER_SECONDS: usize = 30;
pub const RING_FRAMES: usize = FRAMES_PER_SECOND as usize * MAX_BUFFER_SECONDS;

/// What `start` allocated: the Core chunk uploader keys its recording by
/// `generation` and asks for at most `max_frames` per read.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct RecorderStart {
    pub generation: u64,
    pub sample_rate: u32,
    pub channels: u32,
    pub frame_samples: u32,
    pub max_frames: u32,
}

/// A drained block of complete 20 ms frames.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct RecorderChunk {
    pub generation: u64,
    /// Delivery order, starting at zero for each capture.
    pub seq: u64,
    /// Absolute sample position of the first byte, counting dropped samples.
    pub sample_offset: u64,
    pub samples: u32,
    /// PCM16-LE, always a whole number of frames.
    pub bytes: Vec<u8>,
    /// Frames lost to the bounded ring since the capture started.
    pub dropped_samples: u64,
    /// The part of `dropped_samples` reported since the previous read.
    pub dropped_since_previous: u64,
}

/// What the capture accepted and what is still buffered when `stop` returns.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct RecorderStop {
    pub generation: u64,
    /// Samples produced by the resampler, including frames later dropped.
    pub captured_samples: u64,
    /// Samples handed to the consumer before this stop.
    pub delivered_samples: u64,
    /// Samples evicted unread from the bounded ring.
    pub dropped_samples: u64,
    /// Complete frames left for a final read after `stop`.
    pub buffered_frames: u32,
    /// Resampled samples still held in the incomplete final 20 ms frame.
    ///
    /// These samples are not part of `buffered_frames`. The full final frame
    /// header is never padded; `tail_bytes` carries their exact PCM16-LE
    /// samples so a client can preserve them instead of dropping the end of
    /// the recording.
    pub tail_samples: u32,
    /// PCM16-LE samples for `tail_samples`; empty when there is no tail.
    pub tail_bytes: Vec<u8>,
    /// Set when the device stream failed before `stop` was requested.
    pub error: Option<String>,
}

/// Bounded frame queue. `base_frame` is the absolute index of the oldest frame
/// still buffered, so it also advances when overflow evicts a frame.
#[derive(Debug)]
struct Ring {
    frames: VecDeque<Vec<u8>>,
    capacity: usize,
    base_frame: u64,
    next_frame: u64,
    dropped_frames: u64,
    seq: u64,
    reported_drops: u64,
}

impl Ring {
    fn new(capacity: usize) -> Self {
        Self {
            frames: VecDeque::with_capacity(capacity.min(16)),
            capacity: capacity.max(1),
            base_frame: 0,
            next_frame: 0,
            dropped_frames: 0,
            seq: 0,
            reported_drops: 0,
        }
    }

    fn push(&mut self, frame: Vec<u8>) {
        debug_assert_eq!(frame.len(), FRAME_BYTES);
        while self.frames.len() >= self.capacity {
            self.frames.pop_front();
            self.base_frame += 1;
            self.dropped_frames += 1;
        }
        self.frames.push_back(frame);
        self.next_frame += 1;
    }

    fn read(&mut self, max_frames: usize) -> RecorderChunk {
        let count = max_frames.min(self.frames.len());
        let sample_offset = self.base_frame * FRAME_SAMPLES as u64;
        let mut bytes = Vec::with_capacity(count * FRAME_BYTES);
        for _ in 0..count {
            if let Some(frame) = self.frames.pop_front() {
                bytes.extend_from_slice(&frame);
            }
        }
        self.base_frame += count as u64;
        let dropped_samples = self.dropped_frames * FRAME_SAMPLES as u64;
        let dropped_since_previous =
            (self.dropped_frames - self.reported_drops) * FRAME_SAMPLES as u64;
        self.reported_drops = self.dropped_frames;
        let seq = self.seq;
        if count > 0 {
            self.seq += 1;
        }
        RecorderChunk {
            generation: 0,
            seq,
            sample_offset,
            samples: (count * FRAME_SAMPLES) as u32,
            bytes,
            dropped_samples,
            dropped_since_previous,
        }
    }

    fn dropped_samples(&self) -> u64 {
        self.dropped_frames * FRAME_SAMPLES as u64
    }

    fn delivered_samples(&self) -> u64 {
        (self.base_frame - self.dropped_frames) * FRAME_SAMPLES as u64
    }
}

/// Converts interleaved device samples into 16 kHz mono PCM16 20 ms frames.
/// Position is carried across callbacks, so callback boundaries do not change
/// the sample timeline. The final incomplete output frame is held, not emitted.
struct Resampler {
    source_rate: u32,
    channels: usize,
    samples_seen: u64,
    next_output: u64,
    previous: Option<f32>,
    pending: Vec<i16>,
    partial: Vec<f32>,
}

impl Resampler {
    fn new(source_rate: u32, channels: usize) -> Option<Self> {
        (source_rate > 0 && channels > 0).then_some(Self {
            source_rate,
            channels,
            samples_seen: 0,
            next_output: 0,
            previous: None,
            pending: Vec::with_capacity(FRAME_SAMPLES),
            partial: Vec::new(),
        })
    }

    fn push(&mut self, input: &[f32]) -> Vec<Vec<u8>> {
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
        let start = self.samples_seen;
        let end = start + mono.len() as u64 - 1;
        let mut frames = Vec::new();
        loop {
            let position = self.next_output * u64::from(self.source_rate);
            let base = position / u64::from(SAMPLE_RATE);
            let remainder = position % u64::from(SAMPLE_RATE);
            if base > end || (base == end && remainder > 0) {
                break;
            }
            let fraction = remainder as f32 / SAMPLE_RATE as f32;
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
        self.samples_seen += mono.len() as u64;
        self.previous = mono.last().copied();
        frames
    }

    /// PCM16-LE bytes for the final partial frame that has not been emitted.
    fn pending_bytes(&self) -> Vec<u8> {
        self.pending
            .iter()
            .flat_map(|sample| sample.to_le_bytes())
            .collect()
    }
}

struct Capture {
    generation: u64,
    running: AtomicBool,
    finished: Mutex<bool>,
    finished_cv: Condvar,
    stop: AtomicBool,
    level: AtomicU32,
    ring: Mutex<Ring>,
    tail: Mutex<Vec<u8>>,
    error: Mutex<Option<String>>,
    lease: Mutex<Option<MicLease>>,
}

impl Capture {
    fn new(generation: u64) -> Arc<Self> {
        Arc::new(Self {
            generation,
            running: AtomicBool::new(true),
            finished: Mutex::new(false),
            finished_cv: Condvar::new(),
            stop: AtomicBool::new(false),
            level: AtomicU32::new(0),
            ring: Mutex::new(Ring::new(RING_FRAMES)),
            tail: Mutex::new(Vec::new()),
            error: Mutex::new(None),
            lease: Mutex::new(None),
        })
    }

    #[cfg(test)]
    fn with_capacity(generation: u64, frames: usize) -> Arc<Self> {
        let capture = Self::new(generation);
        *capture.ring.lock().unwrap() = Ring::new(frames);
        capture
    }

    fn push(&self, frames: Vec<Vec<u8>>) {
        if self.stop.load(Ordering::Acquire) || !self.running.load(Ordering::Acquire) {
            return;
        }
        if let Ok(mut ring) = self.ring.lock() {
            for frame in frames {
                ring.push(frame);
            }
        }
    }

    fn set_level(&self, level: f32) {
        self.level
            .store(level.clamp(0.0, 1.0).to_bits(), Ordering::Relaxed);
    }

    fn set_tail(&self, tail: Vec<u8>) {
        if let Ok(mut slot) = self.tail.lock() {
            *slot = tail;
        }
    }

    fn read(&self, generation: u64, max_frames: u32) -> Result<RecorderChunk, String> {
        if generation != self.generation {
            return Err("The recorder generation is not the active capture".into());
        }
        if max_frames == 0 {
            return Err("max_frames must be at least one".into());
        }
        let mut ring = self
            .ring
            .lock()
            .map_err(|_| "Recorder buffer is unavailable")?;
        let mut chunk = ring.read((max_frames as usize).min(RING_FRAMES));
        chunk.generation = self.generation;
        Ok(chunk)
    }

    fn request_stop(&self) {
        self.stop.store(true, Ordering::Release);
    }

    fn hold_microphone(&self, lease: MicLease) -> bool {
        let mut slot = self
            .lease
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if slot.is_some() {
            return false;
        }
        *slot = Some(lease);
        true
    }

    fn release_microphone(&self) {
        let mut lease = self
            .lease
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let held = lease.take();
        drop(lease);
        drop(held);
    }

    /// Called only by the worker after it has dropped the device stream.
    fn finish(&self) {
        // The worker only reaches here after dropping the stream. Release the
        // microphone before waking waiters, so a completed `stop` implies the
        // device and its lease are already free.
        self.running.store(false, Ordering::Release);
        self.release_microphone();
        self.set_level(0.0);
        {
            let mut finished = self
                .finished
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *finished = true;
        }
        self.finished_cv.notify_all();
    }

    fn is_finished(&self) -> bool {
        *self
            .finished
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn wait_finished(&self, timeout: Duration) -> bool {
        let finished = self
            .finished
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if *finished {
            return true;
        }
        match self.finished_cv.wait_timeout(finished, timeout) {
            Ok((finished, _)) => *finished,
            Err(poisoned) => *poisoned.into_inner().0,
        }
    }

    fn fail(&self, error: String) {
        if let Ok(mut slot) = self.error.lock() {
            if slot.is_none() {
                *slot = Some(error);
            }
        }
        self.request_stop();
    }

    fn stop_info(&self) -> Result<RecorderStop, String> {
        let ring = self
            .ring
            .lock()
            .map_err(|_| "Recorder buffer is unavailable")?;
        let tail = self
            .tail
            .lock()
            .map_err(|_| "Recorder tail is unavailable")?
            .clone();
        let tail_samples = (tail.len() / 2) as u32;
        Ok(RecorderStop {
            generation: self.generation,
            captured_samples: ring.next_frame * FRAME_SAMPLES as u64 + u64::from(tail_samples),
            delivered_samples: ring.delivered_samples(),
            dropped_samples: ring.dropped_samples(),
            buffered_frames: ring.frames.len() as u32,
            tail_samples,
            tail_bytes: tail,
            error: self.error.lock().ok().and_then(|error| error.clone()),
        })
    }
}

struct Active {
    capture: Arc<Capture>,
    worker: Mutex<Option<std::thread::JoinHandle<()>>>,
}

/// Closes a capture when the worker thread returns, including on panic. The
/// guard is created inside the worker, so only worker shutdown can mark a
/// capture finished and release its microphone lease.
struct FinishOnDrop(Arc<Capture>);

impl Drop for FinishOnDrop {
    fn drop(&mut self) {
        self.0.finish();
    }
}

/// The capture and the worker that belongs to it, taken together under the
/// same lock so a concurrent restart cannot substitute another worker.
struct StopTarget {
    capture: Arc<Capture>,
    worker: Option<std::thread::JoinHandle<()>>,
}

fn take_stop_target() -> Result<StopTarget, String> {
    let slot = ACTIVE.lock().map_err(|_| "Recorder state is unavailable")?;
    let active = slot.as_ref().ok_or("No recorder capture is running")?;
    let worker = active
        .worker
        .lock()
        .map_err(|_| "Recorder worker state is unavailable")?
        .take();
    Ok(StopTarget {
        capture: active.capture.clone(),
        worker,
    })
}

fn complete_stop(target: StopTarget) -> Result<RecorderStop, String> {
    target.capture.request_stop();
    match target.worker {
        Some(worker) => {
            let _ = worker.join();
        }
        None => {
            // Another stop is joining the same worker; wait for that worker to
            // close the device instead of marking the capture finished here.
            if !target.capture.wait_finished(Duration::from_secs(30)) {
                return Err("The recorder did not stop in time".into());
            }
        }
    }
    if !target.capture.is_finished() {
        return Err("The recorder worker stopped without closing the capture".into());
    }
    target.capture.stop_info()
}

static ACTIVE: Mutex<Option<Active>> = Mutex::new(None);
static GENERATION: AtomicU64 = AtomicU64::new(0);
static STARTING: AtomicBool = AtomicBool::new(false);

struct StartGuard;

impl Drop for StartGuard {
    fn drop(&mut self) {
        STARTING.store(false, Ordering::Release);
    }
}

/// Cancellation-safe startup: while `start` awaits the worker, dropping the
/// future asks the worker to stop instead of leaving an unowned capture with
/// its lease held. Disarmed only after the capture is installed in `ACTIVE`.
struct StartupGuard {
    capture: Arc<Capture>,
    armed: bool,
}

impl StartupGuard {
    fn new(capture: Arc<Capture>) -> Self {
        Self {
            capture,
            armed: true,
        }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for StartupGuard {
    fn drop(&mut self) {
        if self.armed {
            self.capture.request_stop();
        }
    }
}

/// Runs the readiness wait under a [`StartupGuard`]. The caller disarms the
/// guard after `install`; any error or cancelled await leaves it armed.
async fn await_ready_guarded(
    capture: Arc<Capture>,
    ready: std::sync::mpsc::Receiver<Result<(), String>>,
) -> Result<StartupGuard, String> {
    let guard = StartupGuard::new(capture);
    wait_ready(ready).await?;
    Ok(guard)
}

fn install(active: Active) -> Result<(), String> {
    let mut slot = ACTIVE.lock().map_err(|_| "Recorder state is unavailable")?;
    if let Some(current) = slot.as_ref() {
        if current.capture.running.load(Ordering::Acquire) {
            return Err("A recorder capture is already running".into());
        }
    }
    *slot = Some(active);
    Ok(())
}

fn active_capture(generation: u64) -> Result<Arc<Capture>, String> {
    let slot = ACTIVE.lock().map_err(|_| "Recorder state is unavailable")?;
    let active = slot.as_ref().ok_or("No recorder capture is running")?;
    if active.capture.generation != generation {
        return Err("The recorder generation is not the active capture".into());
    }
    Ok(active.capture.clone())
}

fn busy_error(owner: MicOwner) -> String {
    match owner {
        MicOwner::Dictation => "Stop dictation before starting the recorder".into(),
        MicOwner::Live => "Stop the Manager voice call before starting the recorder".into(),
        MicOwner::Recorder => "A recorder capture is already running".into(),
    }
}

#[cfg(any(target_os = "macos", target_os = "linux", target_os = "windows"))]
async fn wait_ready(ready: std::sync::mpsc::Receiver<Result<(), String>>) -> Result<(), String> {
    use std::sync::mpsc::TryRecvError;

    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    loop {
        match ready.try_recv() {
            Ok(Ok(())) => return Ok(()),
            Ok(Err(error)) => return Err(error),
            Err(TryRecvError::Disconnected) => {
                return Err("Recorder audio stopped while starting".into());
            }
            Err(TryRecvError::Empty) => {
                if tokio::time::Instant::now() >= deadline {
                    return Err("The microphone did not start in time".into());
                }
                // Yield to the client's runtime while the worker opens audio;
                // startup never blocks the UI thread.
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
async fn wait_ready(_ready: std::sync::mpsc::Receiver<Result<(), String>>) -> Result<(), String> {
    Err("Recorder capture is not available on this platform".into())
}

async fn ensure_microphone_permission() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        macos_permission().await
    }
    #[cfg(not(target_os = "macos"))]
    {
        Ok(())
    }
}

#[cfg(target_os = "macos")]
async fn macos_permission() -> Result<(), String> {
    use block2::RcBlock;
    use objc2::runtime::Bool;
    use objc2::{class, msg_send};
    use objc2_foundation::NSString;

    const DENIED: &str =
        "Microphone permission was denied. Allow microphone access for Nexus in Settings and try again.";
    // AVMediaTypeAudio is the four-character media type "soun".
    let media = NSString::from_str("soun");
    // SAFETY: querying authorisation is the documented AVCaptureDevice API.
    let status: isize =
        unsafe { msg_send![class!(AVCaptureDevice), authorizationStatusForMediaType: &*media] };
    if status == 3 {
        return Ok(());
    }
    if status != 0 {
        return Err(DENIED.into());
    }
    let (sender, receiver) = futures_channel::oneshot::channel::<bool>();
    let sender = Mutex::new(Some(sender));
    let block = RcBlock::new(move |granted: Bool| {
        if let Some(sender) = sender.lock().unwrap().take() {
            let _ = sender.send(granted.as_bool());
        }
    });
    // SAFETY: the system calls the completion exactly once.
    unsafe {
        let _: () = msg_send![class!(AVCaptureDevice), requestAccessForMediaType: &*media, completionHandler: &*block];
    }
    match receiver.await {
        Ok(true) => Ok(()),
        _ => Err(DENIED.into()),
    }
}

#[cfg(any(target_os = "macos", target_os = "linux", target_os = "windows"))]
mod desktop {
    use std::sync::mpsc;

    use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

    use super::{Capture, FinishOnDrop, FRAME_BYTES};

    pub const SUPPORTED: bool = true;

    pub fn devices() -> Vec<String> {
        let host = cpal::default_host();
        let Ok(devices) = host.input_devices() else {
            return Vec::new();
        };
        devices
            .filter_map(|device| device.description().ok().map(|d| d.name().to_owned()))
            .collect()
    }

    /// Opens the stream inside the worker thread: some backends make the
    /// stream non-`Send`, and this thread owns it for the capture's lifetime.
    pub fn spawn(
        capture: std::sync::Arc<Capture>,
        device: Option<String>,
    ) -> Result<
        (
            std::thread::JoinHandle<()>,
            mpsc::Receiver<Result<(), String>>,
        ),
        String,
    > {
        let (ready_tx, ready_rx) = mpsc::channel();
        let worker = std::thread::Builder::new()
            .name("nexus-recorder-capture".into())
            .spawn(move || {
                let _finish = FinishOnDrop(capture.clone());
                run(capture, device, ready_tx);
            })
            .map_err(|error| format!("Could not start recorder audio: {error}"))?;
        Ok((worker, ready_rx))
    }

    fn run(
        capture: std::sync::Arc<Capture>,
        device: Option<String>,
        ready: mpsc::Sender<Result<(), String>>,
    ) {
        let stream = match open(&capture, device) {
            Ok(stream) => {
                if ready.send(Ok(())).is_err() {
                    return;
                }
                stream
            }
            Err(error) => {
                capture.fail(error.clone());
                // No stream was opened. Close the capture before reporting the
                // failure, so the lease is free for an immediate retry.
                capture.finish();
                let _ = ready.send(Err(error));
                return;
            }
        };
        while !capture.stop.load(std::sync::atomic::Ordering::Acquire) {
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        drop(stream);
        // `FinishOnDrop` closes the capture after the stream is gone.
    }

    fn open(
        capture: &std::sync::Arc<Capture>,
        name: Option<String>,
    ) -> Result<cpal::Stream, String> {
        let host = cpal::default_host();
        let device = match name {
            Some(name) if !name.is_empty() => host
                .input_devices()
                .map_err(|error| format!("Microphones are unavailable: {error}"))?
                .find(|device| {
                    device
                        .description()
                        .is_ok_and(|description| description.name() == name.as_str())
                })
                .ok_or_else(|| format!("No microphone named {name}"))?,
            _ => host
                .default_input_device()
                .ok_or("No microphone is available")?,
        };
        let supported = device
            .default_input_config()
            .map_err(|error| format!("Microphone is unavailable: {error}"))?;
        let config = supported.config();
        let resampler = super::Resampler::new(config.sample_rate, config.channels as usize)
            .ok_or("The microphone format is unsupported")?;
        let capture = capture.clone();
        let stream = match supported.sample_format() {
            cpal::SampleFormat::F32 => build::<f32, _>(&device, &config, capture, resampler, |v| v),
            cpal::SampleFormat::I16 => build::<i16, _>(&device, &config, capture, resampler, |v| {
                f32::from(v) / 32768.0
            }),
            cpal::SampleFormat::U16 => build::<u16, _>(&device, &config, capture, resampler, |v| {
                (f32::from(v) - 32768.0) / 32768.0
            }),
            format => return Err(format!("Unsupported microphone format: {format:?}")),
        }
        .map_err(|error| format!("Could not open microphone: {error}"))?;
        stream
            .play()
            .map_err(|error| format!("Could not start microphone: {error}"))?;
        Ok(stream)
    }

    fn build<T, F>(
        device: &cpal::Device,
        config: &cpal::StreamConfig,
        capture: std::sync::Arc<Capture>,
        mut resampler: super::Resampler,
        convert: F,
    ) -> Result<cpal::Stream, cpal::Error>
    where
        T: cpal::SizedSample + Send + 'static,
        F: Fn(T) -> f32 + Send + 'static,
    {
        let error_capture = capture.clone();
        device.build_input_stream::<T, _, _>(
            config.clone(),
            move |input, _| {
                if input.is_empty() {
                    capture.set_level(0.0);
                    return;
                }
                let mut samples = Vec::with_capacity(input.len());
                let mut energy = 0.0f32;
                for &sample in input {
                    let value = convert(sample).clamp(-1.0, 1.0);
                    energy += value * value;
                    samples.push(value);
                }
                let rms = (energy / samples.len() as f32).sqrt();
                capture.set_level((rms * 3.0).min(1.0));
                let frames = resampler.push(&samples);
                debug_assert!(frames.iter().all(|frame| frame.len() == FRAME_BYTES));
                capture.push(frames);
                capture.set_tail(resampler.pending_bytes());
            },
            move |error| error_capture.fail(format!("Microphone stream failed: {error}")),
            None,
        )
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
mod desktop {
    use std::sync::{mpsc, Arc};

    use super::Capture;

    pub const SUPPORTED: bool = false;

    pub fn devices() -> Vec<String> {
        Vec::new()
    }

    pub fn spawn(
        _capture: Arc<Capture>,
        _device: Option<String>,
    ) -> Result<
        (
            std::thread::JoinHandle<()>,
            mpsc::Receiver<Result<(), String>>,
        ),
        String,
    > {
        Err("Recorder capture is not available on this platform".into())
    }
}

/// Continuous microphone capture for the standalone recorder.
#[module]
pub struct RecorderMic;

#[module]
impl RecorderMic {
    /// Whether this build contains a continuous desktop capture path.
    pub fn available() -> bool {
        desktop::SUPPORTED
    }

    /// Input device names, for a picker. Empty when the host cannot list them.
    pub fn devices() -> Vec<String> {
        desktop::devices()
    }

    /// Starts a capture after asking for microphone permission. An empty
    /// `device` uses the host's default input.
    pub async fn start(device: Option<String>) -> Result<RecorderStart, String> {
        if !desktop::SUPPORTED {
            return Err("Recorder capture is not available on this platform".into());
        }
        if STARTING.swap(true, Ordering::AcqRel) {
            return Err("A recorder capture is already starting".into());
        }
        let _starting = StartGuard;
        // Claim the microphone before the permission prompt, so a concurrent
        // dictation or Manager call cannot start while this one is pending.
        let lease = mic_owner::try_acquire(MicOwner::Recorder).map_err(busy_error)?;
        {
            let slot = ACTIVE.lock().map_err(|_| "Recorder state is unavailable")?;
            if let Some(active) = slot.as_ref() {
                if active.capture.running.load(Ordering::Acquire) {
                    return Err("A recorder capture is already running".into());
                }
            }
        }
        ensure_microphone_permission().await?;
        let generation = GENERATION.fetch_add(1, Ordering::AcqRel) + 1;
        let capture = Capture::new(generation);
        if !capture.hold_microphone(lease) {
            return Err("Recorder state is unavailable".into());
        }
        let (worker, ready) = desktop::spawn(capture.clone(), device)?;
        let mut startup = match await_ready_guarded(capture.clone(), ready).await {
            Ok(startup) => startup,
            Err(error) => {
                // The guard already asked the worker to stop. Detach it: a
                // timeout may still be inside the device open, and the worker
                // closes and releases when that call returns.
                drop(worker);
                return Err(error);
            }
        };
        let active = Active {
            capture: capture.clone(),
            worker: Mutex::new(Some(worker)),
        };
        if let Err(error) = install(active) {
            // The armed startup guard requests a stop as it drops here.
            return Err(error);
        }
        startup.disarm();
        Ok(RecorderStart {
            generation,
            sample_rate: SAMPLE_RATE,
            channels: 1,
            frame_samples: FRAME_SAMPLES as u32,
            max_frames: RING_FRAMES as u32,
        })
    }

    /// Drains at most `max_frames` complete 20 ms frames. Reading after
    /// `stop` drains what the ring still holds.
    pub fn read(generation: u64, max_frames: u32) -> Result<RecorderChunk, String> {
        active_capture(generation)?.read(generation, max_frames)
    }

    /// The current input level, 0 to 1.
    pub fn level() -> f64 {
        ACTIVE
            .lock()
            .ok()
            .and_then(|slot| {
                slot.as_ref()
                    .map(|active| f32::from_bits(active.capture.level.load(Ordering::Relaxed)))
            })
            .unwrap_or(0.0) as f64
    }

    /// Stops the device, waits for its worker to close it, and reports the
    /// final sample counts. Buffered frames stay readable until the next start.
    pub fn stop() -> Result<RecorderStop, String> {
        complete_stop(take_stop_target()?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    static LIFECYCLE: Mutex<()> = Mutex::new(());

    fn frame(seed: u8) -> Vec<u8> {
        vec![seed; FRAME_BYTES]
    }

    fn reset() {
        *ACTIVE.lock().unwrap() = None;
        STARTING.store(false, Ordering::Release);
    }

    type Gate = Arc<(Mutex<bool>, Condvar)>;

    fn gate() -> Gate {
        Arc::new((Mutex::new(false), Condvar::new()))
    }

    fn open_gate(gate: &Gate) {
        let (open, changed) = &**gate;
        *open.lock().unwrap() = true;
        changed.notify_all();
    }

    /// A worker that waits for `stop`, optionally waits for a test gate, then
    /// closes the capture exactly as the real device thread does.
    fn spawn_test_worker(capture: Arc<Capture>, gate: Option<Gate>) -> std::thread::JoinHandle<()> {
        std::thread::Builder::new()
            .name("nexus-recorder-test".into())
            .spawn(move || {
                while !capture.stop.load(Ordering::Acquire) {
                    std::thread::yield_now();
                }
                if let Some(gate) = gate {
                    let (open, changed) = &*gate;
                    let mut open = open.lock().unwrap();
                    while !*open {
                        open = changed.wait(open).unwrap();
                    }
                }
                capture.finish();
            })
            .unwrap()
    }

    fn install_test_capture(capture: Arc<Capture>, gate: Option<Gate>) {
        let worker = spawn_test_worker(capture.clone(), gate);
        install(Active {
            capture,
            worker: Mutex::new(Some(worker)),
        })
        .unwrap();
    }

    fn install_owned_test_capture(generation: u64, gate: Option<Gate>) -> Arc<Capture> {
        let capture = Capture::new(generation);
        let lease = mic_owner::try_acquire(MicOwner::Recorder).unwrap();
        assert!(capture.hold_microphone(lease));
        install_test_capture(capture.clone(), gate);
        capture
    }

    fn wait_for(mut ready: impl FnMut() -> bool) -> bool {
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while std::time::Instant::now() < deadline {
            if ready() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        ready()
    }

    #[test]
    fn resamples_to_contiguous_16khz_frames_across_callback_boundaries() {
        let samples: Vec<f32> = (0..96_000)
            .map(|i| ((i % 97) as f32 / 48.5) - 1.0)
            .collect();
        let mut whole = Resampler::new(48_000, 1).unwrap();
        let expected: Vec<Vec<u8>> = whole.push(&samples);
        let mut chunked = Resampler::new(48_000, 1).unwrap();
        let actual: Vec<Vec<u8>> = samples
            .chunks(137)
            .flat_map(|part| chunked.push(part))
            .collect();
        assert_eq!(actual, expected);
        assert!(actual.iter().all(|frame| frame.len() == FRAME_BYTES));
        // Two seconds at 48 kHz become two seconds at 16 kHz, less at most the
        // one incomplete output frame the resampler holds.
        let samples_out = actual.len() * FRAME_SAMPLES;
        assert!((32_000 - FRAME_SAMPLES..=32_000).contains(&samples_out));
    }

    #[test]
    fn stereo_44100_downsamples_without_losing_sample_timeline() {
        let input: Vec<f32> = (0..44_100 * 2)
            .map(|i| if i % 2 == 0 { 0.25 } else { 0.75 })
            .collect();
        let mut resampler = Resampler::new(44_100, 2).unwrap();
        let frames: Vec<Vec<u8>> = input
            .chunks(1000)
            .flat_map(|part| resampler.push(part))
            .collect();
        let samples: usize = frames.len() * FRAME_SAMPLES;
        assert!((15_680..=16_000).contains(&samples));
        let first = i16::from_le_bytes([frames[0][0], frames[0][1]]);
        assert!((first - 16_384).abs() <= 1);
    }

    #[test]
    fn one_minute_at_44100_stereo_drifts_less_than_one_frame() {
        let mut resampler = Resampler::new(44_100, 2).unwrap();
        let mut frames = 0usize;
        let second = vec![0.25; 44_100 * 2];
        for _ in 0..60 {
            frames += resampler.push(&second).len();
        }
        let samples = frames * FRAME_SAMPLES;
        assert!((960_000 - FRAME_SAMPLES..=960_000).contains(&samples));
    }

    #[test]
    fn stop_exposes_the_incomplete_final_frame_as_an_exact_tail() {
        let input: Vec<f32> = vec![0.25; 44_101];
        let mut resampler = Resampler::new(44_100, 1).unwrap();
        let frames = resampler.push(&input);
        let tail = resampler.pending_bytes();
        let tail_samples = tail.len() / 2;
        assert!((1..FRAME_SAMPLES).contains(&tail_samples));
        assert_eq!(frames.len() * FRAME_SAMPLES + tail_samples, 16_001);

        let capture = Capture::new(61);
        capture.set_tail(tail.clone());
        capture.push(frames.clone());
        let stop = capture.stop_info().unwrap();
        assert_eq!(stop.tail_samples as usize, tail_samples);
        assert_eq!(stop.tail_bytes, tail);
        assert_eq!(stop.captured_samples, 16_001);
        assert_eq!(stop.delivered_samples, 0);
        assert_eq!(stop.buffered_frames, frames.len() as u32);
    }

    #[test]
    fn a_consumer_that_keeps_up_never_reports_overflow() {
        let capture = Capture::with_capacity(9, 2);
        for round in 0..4u8 {
            capture.push(vec![frame(round * 2), frame(round * 2 + 1)]);
            let chunk = capture.read(9, 2).unwrap();
            assert_eq!(chunk.samples, 2 * FRAME_SAMPLES as u32);
            assert_eq!(chunk.dropped_samples, 0);
            assert_eq!(
                chunk.sample_offset,
                u64::from(round) * 2 * FRAME_SAMPLES as u64
            );
        }
        let stop = capture.stop_info().unwrap();
        assert_eq!(stop.captured_samples, 8 * FRAME_SAMPLES as u64);
        assert_eq!(stop.delivered_samples, stop.captured_samples);
        assert_eq!(stop.dropped_samples, 0);
        assert_eq!(stop.buffered_frames, 0);
    }

    #[test]
    fn bounded_ring_drops_oldest_frames_and_reports_every_loss() {
        let capture = Capture::with_capacity(7, 3);
        capture.push(vec![frame(1), frame(2), frame(3), frame(4), frame(5)]);
        let first = capture.read(7, 10).unwrap();
        assert_eq!(first.seq, 0);
        assert_eq!(first.samples, 3 * FRAME_SAMPLES as u32);
        assert_eq!(first.bytes, [frame(3), frame(4), frame(5)].concat());
        assert_eq!(first.sample_offset, 2 * FRAME_SAMPLES as u64);
        assert_eq!(first.dropped_samples, 2 * FRAME_SAMPLES as u64);
        assert_eq!(first.dropped_since_previous, 2 * FRAME_SAMPLES as u64);

        capture.push(vec![frame(6)]);
        let second = capture.read(7, 1).unwrap();
        assert_eq!(second.seq, 1);
        assert_eq!(second.sample_offset, 5 * FRAME_SAMPLES as u64);
        assert_eq!(second.samples, FRAME_SAMPLES as u32);
        assert_eq!(second.dropped_samples, first.dropped_samples);
        assert_eq!(second.dropped_since_previous, 0);

        let stop = capture.stop_info().unwrap();
        assert_eq!(stop.captured_samples, 6 * FRAME_SAMPLES as u64);
        assert_eq!(stop.dropped_samples, 2 * FRAME_SAMPLES as u64);
        assert_eq!(
            stop.delivered_samples,
            stop.captured_samples - stop.dropped_samples
        );
        assert_eq!(stop.buffered_frames, 0);
    }

    #[test]
    fn reads_only_complete_frames_and_refuses_a_foreign_generation() {
        let capture = Capture::new(42);
        capture.push(vec![frame(9)]);
        assert!(capture.read(41, 1).is_err());
        assert!(capture.read(42, 0).is_err());
        let chunk = capture.read(42, 1).unwrap();
        assert_eq!(chunk.generation, 42);
        assert_eq!(chunk.bytes.len(), FRAME_BYTES);
        let empty = capture.read(42, 1).unwrap();
        assert_eq!(empty.samples, 0);
        assert_eq!(empty.seq, 1);
        assert!(empty.bytes.is_empty());
        assert_eq!(empty.sample_offset, FRAME_SAMPLES as u64);
    }

    #[test]
    fn lifecycle_refuses_double_start_stops_once_and_allows_restart() {
        let _guard = LIFECYCLE.lock().unwrap();
        reset();

        let first = Capture::new(1);
        install(Active {
            capture: first.clone(),
            worker: Mutex::new(None),
        })
        .unwrap();
        assert!(install(Active {
            capture: Capture::new(2),
            worker: Mutex::new(None),
        })
        .is_err());

        first.push(vec![frame(1), frame(2)]);
        first.set_level(0.5);
        assert_eq!(first.read(1, 10).unwrap().samples, 2 * FRAME_SAMPLES as u32);

        first.request_stop();
        first.finish();
        assert_eq!(first.stop_info().unwrap().buffered_frames, 0);
        assert_eq!(
            first.stop_info().unwrap().captured_samples,
            2 * FRAME_SAMPLES as u64
        );
        assert!(!first.running.load(Ordering::Acquire));
        assert_eq!(first.read(1, 10).unwrap().samples, 0);

        // A stopped capture is replaced by the next start.
        install(Active {
            capture: Capture::new(3),
            worker: Mutex::new(None),
        })
        .unwrap();
        assert_eq!(active_capture(3).unwrap().generation, 3);
        assert!(active_capture(1).is_err());

        reset();
    }

    #[test]
    fn unread_audio_remains_readable_after_stop() {
        let capture = Capture::new(5);
        capture.push(vec![frame(4), frame(5)]);
        capture.request_stop();
        capture.finish();
        let stop = capture.stop_info().unwrap();
        assert_eq!(stop.buffered_frames, 2);
        assert_eq!(stop.delivered_samples, 0);
        assert_eq!(
            capture.read(5, 10).unwrap().samples,
            2 * FRAME_SAMPLES as u32
        );
        let after = capture.stop_info().unwrap();
        assert_eq!(after.buffered_frames, 0);
        assert_eq!(after.delivered_samples, 2 * FRAME_SAMPLES as u64);
    }

    #[test]
    fn a_stream_failure_is_reported_and_stops_the_capture() {
        let capture = Capture::new(8);
        capture.fail("device disconnected".into());
        assert!(capture.stop.load(Ordering::Acquire));
        capture.finish();
        let stop = capture.stop_info().unwrap();
        assert_eq!(stop.error.as_deref(), Some("device disconnected"));
    }

    #[test]
    #[cfg(any(target_os = "macos", target_os = "linux", target_os = "windows"))]
    fn start_refuses_while_another_owner_has_the_microphone() {
        let _guard = mic_owner::TEST_LOCK.lock().unwrap();
        mic_owner::reset_for_test();
        reset();
        let dictation = mic_owner::try_acquire(MicOwner::Dictation).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let error = runtime.block_on(RecorderMic::start(None)).unwrap_err();
        assert_eq!(error, "Stop dictation before starting the recorder");
        drop(dictation);
        let live = mic_owner::try_acquire(MicOwner::Live).unwrap();
        let error = runtime.block_on(RecorderMic::start(None)).unwrap_err();
        assert_eq!(
            error,
            "Stop the Manager voice call before starting the recorder"
        );
        drop(live);
        reset();
        mic_owner::reset_for_test();
    }

    #[test]
    fn stopping_a_capture_releases_the_shared_microphone_lease() {
        let _guard = mic_owner::TEST_LOCK.lock().unwrap();
        mic_owner::reset_for_test();
        reset();
        let capture = install_owned_test_capture(11, None);
        assert_eq!(
            mic_owner::try_acquire(MicOwner::Dictation).unwrap_err(),
            MicOwner::Recorder
        );
        assert_eq!(
            mic_owner::try_acquire(MicOwner::Live).unwrap_err(),
            MicOwner::Recorder
        );
        assert!(RecorderMic::stop().is_ok());
        assert!(capture.is_finished());
        assert_eq!(mic_owner::current(), None);
        let next = mic_owner::try_acquire(MicOwner::Dictation).unwrap();
        drop(next);
        reset();
        mic_owner::reset_for_test();
    }

    #[test]
    fn concurrent_stops_wait_for_one_worker_and_agree() {
        let _guard = mic_owner::TEST_LOCK.lock().unwrap();
        mic_owner::reset_for_test();
        reset();
        let capture = install_owned_test_capture(21, None);
        capture.push(vec![frame(1), frame(2)]);
        let barrier = Arc::new(std::sync::Barrier::new(5));
        let mut threads = Vec::new();
        for _ in 0..4 {
            let barrier = barrier.clone();
            threads.push(std::thread::spawn(move || {
                barrier.wait();
                RecorderMic::stop()
            }));
        }
        barrier.wait();
        let stops: Vec<_> = threads
            .into_iter()
            .map(|thread| thread.join().unwrap().unwrap())
            .collect();
        assert!(stops.iter().all(|stop| stop.generation == 21));
        assert!(stops
            .iter()
            .all(|stop| stop.captured_samples == 2 * FRAME_SAMPLES as u64));
        assert!(capture.is_finished());
        assert_eq!(mic_owner::current(), None);
        reset();
        mic_owner::reset_for_test();
    }

    #[test]
    fn only_worker_shutdown_releases_the_microphone_lease() {
        let _guard = mic_owner::TEST_LOCK.lock().unwrap();
        mic_owner::reset_for_test();
        reset();
        let gate = gate();
        let capture = install_owned_test_capture(31, Some(gate.clone()));
        let stop = std::thread::spawn(RecorderMic::stop);
        assert!(wait_for(|| capture.stop.load(Ordering::Acquire)));
        // The stop caller is joining the worker, which is still holding the
        // device open behind the gate. Nothing may release the lease yet.
        assert_eq!(mic_owner::current(), Some(MicOwner::Recorder));
        assert!(!capture.is_finished());
        open_gate(&gate);
        let stopped = stop.join().unwrap().unwrap();
        assert_eq!(stopped.generation, 31);
        assert!(capture.is_finished());
        assert_eq!(mic_owner::current(), None);
        reset();
        mic_owner::reset_for_test();
    }

    #[test]
    fn a_stop_target_cannot_join_a_replacement_worker() {
        let _guard = mic_owner::TEST_LOCK.lock().unwrap();
        mic_owner::reset_for_test();
        reset();
        let gate = gate();
        let old = install_owned_test_capture(41, Some(gate.clone()));
        // Snapshot old under one lock, as `stop` does, before it finishes.
        let target = take_stop_target().unwrap();
        old.request_stop();
        open_gate(&gate);
        assert!(wait_for(|| old.is_finished()));
        assert_eq!(mic_owner::current(), None);

        let replacement = install_owned_test_capture(42, None);
        let stopped = complete_stop(target).unwrap();
        assert_eq!(stopped.generation, 41);
        // The replacement worker belongs to a new capture and was not joined.
        assert!(replacement.running.load(Ordering::Acquire));
        assert!(!replacement.stop.load(Ordering::Acquire));
        assert!(!replacement.is_finished());
        assert_eq!(mic_owner::current(), Some(MicOwner::Recorder));

        let stopped = RecorderMic::stop().unwrap();
        assert_eq!(stopped.generation, 42);
        assert!(replacement.is_finished());
        assert_eq!(mic_owner::current(), None);
        reset();
        mic_owner::reset_for_test();
    }

    #[test]
    #[cfg(any(target_os = "macos", target_os = "linux", target_os = "windows"))]
    fn startup_readiness_does_not_block_the_runtime() {
        let (sender, receiver) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(500));
            let _ = sender.send(Ok(()));
        });
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let ticked = Arc::new(Mutex::new(None));
        let tick_task = ticked.clone();
        let started = std::time::Instant::now();
        runtime.block_on(async move {
            let ready = wait_ready(receiver);
            let tick = async move {
                tokio::time::sleep(Duration::from_millis(10)).await;
                *tick_task.lock().unwrap() = Some(std::time::Instant::now());
            };
            let (ready, ()) = tokio::join!(ready, tick);
            ready.unwrap();
        });
        worker.join().unwrap();
        let at = ticked.lock().unwrap().unwrap();
        assert!(
            at.duration_since(started) < Duration::from_millis(300),
            "waiting for microphone readiness blocked the runtime"
        );
    }

    #[test]
    fn cancelling_startup_requests_stop_and_keeps_the_lease_until_closure() {
        let _guard = mic_owner::TEST_LOCK.lock().unwrap();
        mic_owner::reset_for_test();
        reset();
        let gate = gate();
        let capture = Capture::new(51);
        let lease = mic_owner::try_acquire(MicOwner::Recorder).unwrap();
        assert!(capture.hold_microphone(lease));
        let worker = spawn_test_worker(capture.clone(), Some(gate.clone()));
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        // No readiness answer: the timeout drops the start future while it is
        // awaiting the worker, exercising cancellation rather than an error.
        let cancelled = runtime.block_on(async {
            tokio::time::timeout(
                Duration::from_millis(50),
                await_ready_guarded(capture.clone(), ready_rx),
            )
            .await
        });
        assert!(cancelled.is_err());
        drop(ready_tx);
        assert!(capture.stop.load(Ordering::Acquire));
        assert_eq!(mic_owner::current(), Some(MicOwner::Recorder));
        assert!(!capture.is_finished());

        open_gate(&gate);
        worker.join().unwrap();
        assert!(capture.is_finished());
        assert_eq!(mic_owner::current(), None);
        reset();
        mic_owner::reset_for_test();
    }

    #[test]
    fn a_disarmed_startup_guard_leaves_the_installed_capture_running() {
        let _guard = mic_owner::TEST_LOCK.lock().unwrap();
        mic_owner::reset_for_test();
        reset();
        let capture = Capture::new(52);
        let lease = mic_owner::try_acquire(MicOwner::Recorder).unwrap();
        assert!(capture.hold_microphone(lease));
        let worker = spawn_test_worker(capture.clone(), None);
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        ready_tx.send(Ok(())).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let mut startup = runtime
            .block_on(await_ready_guarded(capture.clone(), ready_rx))
            .unwrap();
        install(Active {
            capture: capture.clone(),
            worker: Mutex::new(Some(worker)),
        })
        .unwrap();
        startup.disarm();
        drop(startup);
        assert!(!capture.stop.load(Ordering::Acquire));
        assert!(!capture.is_finished());
        assert_eq!(mic_owner::current(), Some(MicOwner::Recorder));

        assert!(RecorderMic::stop().is_ok());
        assert!(capture.is_finished());
        assert_eq!(mic_owner::current(), None);
        reset();
        mic_owner::reset_for_test();
    }

    #[test]
    fn platform_capability_is_explicit() {
        assert_eq!(RecorderMic::available(), desktop::SUPPORTED);
        assert_eq!(
            desktop::SUPPORTED,
            cfg!(any(
                target_os = "macos",
                target_os = "linux",
                target_os = "windows"
            ))
        );
    }
}
