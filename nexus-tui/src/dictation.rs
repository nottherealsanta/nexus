//! TUI speech capture and transcription preparation.
//!
//! Adapted from old Nexus `nexus/ui_support/voice_capture.py` and
//! `rust/tui/src/live_wave.rs`; capture stays local, while Core transcribes the
//! WAV. Device handles are created and dropped on the dedicated capture thread.

use std::collections::VecDeque;
use std::f32::consts::PI;
use std::num::NonZero;
use std::sync::mpsc as std_mpsc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::Sample;
use tokio::sync::mpsc;

pub const TARGET_RATE: u32 = 16_000;
pub const MAX_SECONDS: usize = 120;
pub const MIN_SECONDS: f64 = 0.2;
pub const PREVIEW_STEP_SECONDS: f64 = 0.4;
/// Ticks (125 ms) a changed preview word takes to resolve.
pub const SETTLE_FRAMES: u8 = 3;
const MAX_SAMPLES: usize = MAX_SECONDS * TARGET_RATE as usize;
const LEVEL_WINDOW: usize = TARGET_RATE as usize / 20;
const CUE_RATE: u32 = 44_100;
const CUE_NOTES: [(&str, [f32; 2]); 5] = [
    ("start", [660.0, 880.0]),
    ("stop", [880.0, 587.0]),
    ("loading", [440.0, 440.0]),
    // Old Nexus's completion cue, then a higher two-note cue for a wait.
    ("complete", [660.0, 1046.5]),
    ("input", [880.0, 1174.7]),
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Phase {
    #[default]
    Idle,
    Checking,
    Consent,
    Downloading,
    Starting,
    Recording,
    Stopping,
    Transcribing,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transition {
    Dictate,
    StatusReady,
    StatusAbsent,
    StatusDownloading,
    ConsentGranted,
    DownloadStarted,
    DownloadReady,
    CaptureStarted,
    Stop,
    AudioStopped,
    Finished,
    Cancel,
    Failed,
}

impl Phase {
    /// Valid state-machine transition. Invalid or stale events are ignored.
    pub fn transition(self, event: Transition) -> Option<Self> {
        use Phase::*;
        use Transition::*;
        match (self, event) {
            (Idle, Dictate) => Some(Checking),
            (Checking, StatusReady | DownloadReady) => Some(Starting),
            (Checking, StatusAbsent) => Some(Consent),
            (Checking, StatusDownloading) => Some(Downloading),
            (Consent, ConsentGranted) => Some(Downloading),
            (Downloading, DownloadStarted) => Some(Downloading),
            (Downloading, DownloadReady) => Some(Starting),
            (Starting, CaptureStarted) => Some(Recording),
            (Recording, Stop) => Some(Stopping),
            (Stopping, AudioStopped) => Some(Transcribing),
            (Transcribing, Finished) => Some(Idle),
            (phase, Cancel | Failed) if phase != Idle => Some(Idle),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct PreviewPacer {
    last_request_seconds: f64,
    in_flight: bool,
}

impl PreviewPacer {
    pub fn can_request(&self, captured_seconds: f64) -> bool {
        !self.in_flight && captured_seconds - self.last_request_seconds >= PREVIEW_STEP_SECONDS
    }

    pub fn begin(&mut self, captured_seconds: f64) -> bool {
        if !self.can_request(captured_seconds) {
            return false;
        }
        self.last_request_seconds = captured_seconds;
        self.in_flight = true;
        true
    }

    pub fn finish(&mut self) {
        self.in_flight = false;
    }

    pub fn in_flight(&self) -> bool {
        self.in_flight
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DownloadProgress {
    pub state: String,
    pub bytes_done: u64,
    pub bytes_total: u64,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DictationUi {
    pub phase: Phase,
    pub generation: u64,
    /// Key into the per-session draft map, captured before recording starts.
    pub draft_key: String,
    pub preview: String,
    /// Byte offset in the draft where the words land (the cursor when recording began).
    pub anchor: usize,
    /// Enter was pressed: send the draft once the transcript is in.
    pub send_after: bool,
    /// Chars of `preview` (up to the last whole word) unchanged by the latest update.
    pub stable: usize,
    /// Frames left for the changed words to resolve; counts down on the 125 ms tick.
    pub settle: u8,
    pub seconds: f64,
    pub level: f32,
    pub levels: VecDeque<f32>,
    pub pacer: PreviewPacer,
    /// The stopped recording waits here if a partial request is still in flight.
    pub final_wav: Option<Vec<u8>>,
    pub download: Option<DownloadProgress>,
}

impl Default for DictationUi {
    fn default() -> Self {
        Self {
            phase: Phase::Idle,
            generation: 0,
            draft_key: String::new(),
            preview: String::new(),
            anchor: 0,
            send_after: false,
            stable: 0,
            settle: 0,
            seconds: 0.0,
            level: 0.0,
            levels: VecDeque::with_capacity(32),
            pacer: PreviewPacer::default(),
            final_wav: None,
            download: None,
        }
    }
}

impl DictationUi {
    pub fn begin(&mut self, draft_key: String) -> u64 {
        self.anchor = 0;
        self.send_after = false;
        self.generation = self.generation.wrapping_add(1);
        self.draft_key = draft_key;
        self.preview.clear();
        self.settle = 0;
        self.seconds = 0.0;
        self.level = 0.0;
        self.levels.clear();
        self.pacer = PreviewPacer::default();
        self.final_wav = None;
        self.download = None;
        self.phase = Phase::Checking;
        self.generation
    }

    pub fn cancel(&mut self) -> u64 {
        self.send_after = false;
        self.generation = self.generation.wrapping_add(1);
        self.phase = Phase::Idle;
        self.preview.clear();
        self.settle = 0;
        self.final_wav = None;
        self.download = None;
        self.pacer.finish();
        self.generation
    }

    /// Replace the preview; words that changed resolve over `SETTLE_FRAMES` ticks.
    pub fn set_preview(&mut self, text: String) {
        if self.preview == text {
            return;
        }
        let common = self
            .preview
            .chars()
            .zip(text.chars())
            .take_while(|(a, b)| a == b)
            .count();
        self.stable = text
            .chars()
            .take(common)
            .enumerate()
            .filter(|(_, c)| c.is_whitespace())
            .map(|(i, _)| i + 1)
            .last()
            .unwrap_or(0);
        self.preview = text;
        self.settle = SETTLE_FRAMES;
    }

    /// The preview as drawn: changed ASCII letters are scrambled until they resolve.
    /// Stable words, whitespace and Unicode stay readable.
    pub fn shown_preview(&self, animate: bool) -> String {
        if !animate || self.settle == 0 {
            return self.preview.clone();
        }
        let age = usize::from(SETTLE_FRAMES - self.settle);
        self.preview
            .chars()
            .enumerate()
            .map(|(i, c)| {
                if i >= self.stable && c.is_ascii_alphabetic() && (i + age) % 3 != age {
                    (b'a' + ((i * 17 + age * 11 + usize::from(self.settle) * 7) % 26) as u8) as char
                } else {
                    c
                }
            })
            .collect()
    }

    /// One 125 ms tick; true while the words are still resolving.
    pub fn settle_tick(&mut self) -> bool {
        if self.settle == 0 {
            return false;
        }
        self.settle -= 1;
        true
    }

    pub fn add_level(&mut self, seconds: f64, level: f32) {
        self.seconds = seconds;
        self.level = level.clamp(0.0, 1.0);
        if self.levels.len() == 32 {
            self.levels.pop_front();
        }
        self.levels.push_back(self.level);
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum CaptureEvent {
    Started {
        generation: u64,
    },
    Level {
        generation: u64,
        seconds: f64,
        level: f32,
    },
    Preview {
        generation: u64,
        seconds: f64,
        wav: Vec<u8>,
    },
    Stopped {
        generation: u64,
        seconds: f64,
        wav: Vec<u8>,
    },
    Cancelled {
        generation: u64,
    },
    Failed {
        generation: u64,
        message: String,
    },
    CueWarning {
        generation: u64,
        message: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureCommand {
    Start { generation: u64 },
    Preview { generation: u64 },
    Stop { generation: u64 },
    Cancel { generation: u64 },
}

enum Control {
    Preview,
    Stop,
    Cancel,
}

enum AudioMessage {
    Samples(Vec<f32>),
    Failed(String),
}

/// A control handle only. The CPAL stream is intentionally not returned from its thread.
pub struct CaptureHandle {
    generation: u64,
    control: std_mpsc::Sender<Control>,
    thread: Option<JoinHandle<()>>,
}

impl CaptureHandle {
    pub fn start(generation: u64, events: mpsc::UnboundedSender<CaptureEvent>) -> Self {
        let (control, controls) = std_mpsc::channel();
        let worker_events = events.clone();
        let thread = thread::Builder::new()
            .name("nexus-dictation".into())
            .spawn(move || capture(generation, controls, events))
            .ok();
        if thread.is_none() {
            let _ = worker_events.send(CaptureEvent::Failed {
                generation,
                message: "Could not start the microphone capture thread".into(),
            });
        }
        Self {
            generation,
            control,
            thread,
        }
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn send(&self, command: CaptureCommand) {
        let control = match command {
            CaptureCommand::Preview { generation } if generation == self.generation => {
                Some(Control::Preview)
            }
            CaptureCommand::Stop { generation } if generation == self.generation => {
                Some(Control::Stop)
            }
            CaptureCommand::Cancel { generation } if generation == self.generation => {
                Some(Control::Cancel)
            }
            _ => None,
        };
        if let Some(control) = control {
            let _ = self.control.send(control);
        }
    }

    pub fn join(mut self) {
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Drop for CaptureHandle {
    fn drop(&mut self) {
        if self.thread.is_some() {
            let _ = self.control.send(Control::Cancel);
        }
    }
}

/// Stateful linear resampling of interleaved audio to a mono 16 kHz stream.
#[derive(Debug, Clone)]
pub struct Resampler {
    source_rate: u32,
    channels: usize,
    frames_seen: u64,
    next_output: u64,
    previous: Option<f32>,
}

impl Resampler {
    pub fn new(source_rate: u32, channels: usize) -> Option<Self> {
        (source_rate > 0 && channels > 0).then_some(Self {
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
            let base = numerator / u64::from(TARGET_RATE);
            let remainder = numerator % u64::from(TARGET_RATE);
            if base > end || (base == end && remainder > 0) {
                break;
            }
            let fraction = remainder as f32 / TARGET_RATE as f32;
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

#[derive(Debug)]
pub struct PcmRecorder {
    resampler: Resampler,
    samples: Vec<i16>,
    level_sum: f64,
    level_count: usize,
    last_level: f32,
    levels: VecDeque<f32>,
}

impl PcmRecorder {
    pub fn new(source_rate: u32, channels: usize) -> Option<Self> {
        Some(Self {
            resampler: Resampler::new(source_rate, channels)?,
            samples: Vec::new(),
            level_sum: 0.0,
            level_count: 0,
            last_level: 0.0,
            levels: VecDeque::with_capacity(32),
        })
    }

    pub fn push(&mut self, interleaved: &[f32]) {
        for sample in self.resampler.push(interleaved) {
            if self.samples.len() >= MAX_SAMPLES {
                break;
            }
            let sample = sample.clamp(-1.0, 1.0);
            self.samples.push(if sample <= -1.0 {
                i16::MIN
            } else {
                (sample * f32::from(i16::MAX)).round() as i16
            });
            self.level_sum += f64::from(sample * sample);
            self.level_count += 1;
            if self.level_count == LEVEL_WINDOW {
                self.last_level = (self.level_sum / self.level_count as f64).sqrt() as f32;
                if self.levels.len() == 32 {
                    self.levels.pop_front();
                }
                self.levels.push_back(self.last_level);
                self.level_sum = 0.0;
                self.level_count = 0;
            }
        }
    }

    pub fn seconds(&self) -> f64 {
        self.samples.len() as f64 / f64::from(TARGET_RATE)
    }

    pub fn is_full(&self) -> bool {
        self.samples.len() >= MAX_SAMPLES
    }

    pub fn level(&self) -> f32 {
        self.last_level
    }

    pub fn levels(&self) -> &VecDeque<f32> {
        &self.levels
    }

    pub fn wav(&self) -> Vec<u8> {
        encode_wav(&self.samples)
    }
}

/// Encode mono 16 kHz signed PCM16 samples in a standard little-endian WAV.
pub fn encode_wav(samples: &[i16]) -> Vec<u8> {
    let data_len = samples.len().saturating_mul(2).min(u32::MAX as usize) as u32;
    let riff_len = 36u32.saturating_add(data_len);
    let mut wav = Vec::with_capacity(44 + data_len as usize);
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&riff_len.to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes());
    wav.extend_from_slice(&TARGET_RATE.to_le_bytes());
    wav.extend_from_slice(&(TARGET_RATE * 2).to_le_bytes());
    wav.extend_from_slice(&2u16.to_le_bytes());
    wav.extend_from_slice(&16u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_len.to_le_bytes());
    for sample in samples.iter().take(data_len as usize / 2) {
        wav.extend_from_slice(&sample.to_le_bytes());
    }
    wav
}

fn cue_pcm(kind: &str) -> Vec<f32> {
    let Some((_, notes)) = CUE_NOTES.iter().find(|(name, _)| *name == kind) else {
        return Vec::new();
    };
    notes
        .iter()
        .flat_map(|frequency| {
            let count = (CUE_RATE as f32 * 0.07) as usize;
            (0..count).map(move |i| {
                let envelope = (PI * i as f32 / count as f32).sin().powi(2);
                (9000.0 / 32768.0)
                    * envelope
                    * (2.0 * PI * frequency * i as f32 / CUE_RATE as f32).sin()
            })
        })
        .collect()
}

fn sounds_off(variable: &str) -> bool {
    std::env::var(variable)
        .ok()
        .is_some_and(|v| matches!(v.to_ascii_lowercase().as_str(), "off" | "0" | "false"))
}

fn play_cue_now(kind: &str, wait: bool) -> Result<(), String> {
    if !CUE_NOTES.iter().any(|(name, _)| *name == kind) {
        return Ok(());
    }
    let mut samples = vec![0.0; CUE_RATE as usize / 4];
    samples.extend(cue_pcm(kind));
    let task = thread::spawn(move || {
        let mut sink = rodio::DeviceSinkBuilder::from_default_device()
            .and_then(|builder| builder.open_sink_or_fallback())
            .map_err(|error| format!("Could not open the audio output device: {error:?}"))?;
        // The default drop notice goes to stderr and would paint over the TUI.
        sink.log_on_drop(false);
        let player = rodio::Player::connect_new(sink.mixer());
        player.append(rodio::buffer::SamplesBuffer::new(
            NonZero::new(1u16).expect("one channel"),
            NonZero::new(CUE_RATE).expect("cue sample rate"),
            samples,
        ));
        while !player.empty() {
            thread::sleep(Duration::from_millis(10));
        }
        // The mixer can consume the queue before the device has played its buffer.
        thread::sleep(Duration::from_millis(250));
        Ok(())
    });
    if wait {
        task.join()
            .unwrap_or_else(|_| Err("Audio output thread failed".into()))
    } else {
        Ok(())
    }
}

fn play_cue(kind: &str, wait: bool) -> Result<(), String> {
    if sounds_off("NEXUS_VOICE_SOUNDS") {
        return Ok(());
    }
    play_cue_now(kind, wait)
}

/// The notification cue for a session finishing or waiting for the user.
/// `NEXUS_COMPLETION_SOUNDS=off` disables both, as in old Nexus.
pub fn play_notification_cue(kind: &str) {
    if sounds_off("NEXUS_COMPLETION_SOUNDS") {
        return;
    }
    let _ = play_cue_now(kind, false);
}

pub fn play_loading_cue() {
    let _ = play_cue("loading", false);
}

fn capture(
    generation: u64,
    controls: std_mpsc::Receiver<Control>,
    events: mpsc::UnboundedSender<CaptureEvent>,
) {
    // Play the start cue before opening the microphone so the tone is never recorded.
    if let Err(message) = play_cue("start", true) {
        let _ = events.send(CaptureEvent::CueWarning {
            generation,
            message,
        });
    }
    let (audio_tx, audio_rx) = std_mpsc::channel();
    let opened = open_input(audio_tx);
    let (stream, sample_rate, channels) = match opened {
        Ok(opened) => opened,
        Err(message) => {
            let _ = events.send(CaptureEvent::Failed {
                generation,
                message,
            });
            return;
        }
    };
    if stream.play().is_err() {
        let _ = events.send(CaptureEvent::Failed {
            generation,
            message: "Could not start the microphone input stream".into(),
        });
        return;
    }
    let Some(mut recorder) = PcmRecorder::new(sample_rate, channels) else {
        let _ = events.send(CaptureEvent::Failed {
            generation,
            message: "The microphone reported an invalid audio format".into(),
        });
        return;
    };
    let _ = events.send(CaptureEvent::Started { generation });
    let mut last_update = Instant::now();
    loop {
        match audio_rx.recv_timeout(Duration::from_millis(30)) {
            Ok(AudioMessage::Samples(samples)) => recorder.push(&samples),
            Ok(AudioMessage::Failed(message)) => {
                drop(stream);
                let _ = events.send(CaptureEvent::Failed {
                    generation,
                    message,
                });
                return;
            }
            Err(std_mpsc::RecvTimeoutError::Disconnected) => {
                drop(stream);
                let _ = events.send(CaptureEvent::Failed {
                    generation,
                    message: "Microphone input stopped unexpectedly".into(),
                });
                return;
            }
            Err(std_mpsc::RecvTimeoutError::Timeout) => {}
        }
        while let Ok(message) = audio_rx.try_recv() {
            match message {
                AudioMessage::Samples(samples) => recorder.push(&samples),
                AudioMessage::Failed(message) => {
                    drop(stream);
                    let _ = events.send(CaptureEvent::Failed {
                        generation,
                        message,
                    });
                    return;
                }
            }
        }
        if last_update.elapsed() >= Duration::from_millis(100) {
            last_update = Instant::now();
            let _ = events.send(CaptureEvent::Level {
                generation,
                seconds: recorder.seconds(),
                level: recorder.level(),
            });
        }
        if recorder.is_full() {
            finish_capture(generation, recorder, stream, events);
            return;
        }
        match controls.try_recv() {
            Ok(Control::Preview) => {
                let _ = events.send(CaptureEvent::Preview {
                    generation,
                    seconds: recorder.seconds(),
                    wav: recorder.wav(),
                });
            }
            Ok(Control::Stop) => {
                finish_capture(generation, recorder, stream, events);
                return;
            }
            Ok(Control::Cancel) => {
                drop(stream);
                let _ = play_cue("stop", false);
                let _ = events.send(CaptureEvent::Cancelled { generation });
                return;
            }
            Err(std_mpsc::TryRecvError::Disconnected) => {
                drop(stream);
                let _ = events.send(CaptureEvent::Cancelled { generation });
                return;
            }
            Err(std_mpsc::TryRecvError::Empty) => {}
        }
    }
}

fn finish_capture(
    generation: u64,
    recorder: PcmRecorder,
    stream: cpal::Stream,
    events: mpsc::UnboundedSender<CaptureEvent>,
) {
    let seconds = recorder.seconds();
    let wav = recorder.wav();
    drop(stream);
    let _ = play_cue("stop", false);
    let _ = events.send(CaptureEvent::Stopped {
        generation,
        seconds,
        wav,
    });
}

fn open_input(audio: std_mpsc::Sender<AudioMessage>) -> Result<(cpal::Stream, u32, usize), String> {
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
    let data_tx = audio.clone();
    let errors_tx = audio;
    let stream = match format {
        cpal::SampleFormat::F32 => input_stream::<f32>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::F64 => input_stream::<f64>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::I8 => input_stream::<i8>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::I16 => input_stream::<i16>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::I24 => input_stream::<cpal::I24>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::I32 => input_stream::<i32>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::I64 => input_stream::<i64>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::U8 => input_stream::<u8>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::U16 => input_stream::<u16>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::U24 => input_stream::<cpal::U24>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::U32 => input_stream::<u32>(&device, config, data_tx, errors_tx),
        cpal::SampleFormat::U64 => input_stream::<u64>(&device, config, data_tx, errors_tx),
        other => return Err(format!("Unsupported microphone sample format: {other}")),
    }
    .map_err(|e| format!("Could not open microphone input: {e}"))?;
    Ok((stream, rate, channels))
}

fn input_stream<T>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    samples_tx: std_mpsc::Sender<AudioMessage>,
    errors_tx: std_mpsc::Sender<AudioMessage>,
) -> Result<cpal::Stream, cpal::Error>
where
    T: cpal::SizedSample + Copy,
    f32: cpal::FromSample<T>,
{
    device.build_input_stream::<T, _, _>(
        config,
        move |data, _| {
            let samples = data
                .iter()
                .map(|sample| f32::from_sample(*sample))
                .collect();
            let _ = samples_tx.send(AudioMessage::Samples(samples));
        },
        move |error| {
            let _ = errors_tx.send(AudioMessage::Failed(error.to_string()));
        },
        None,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resampler_downmixes_stereo_and_resamples_to_16khz() {
        let mut resampler = Resampler::new(48_000, 2).unwrap();
        let mut output = Vec::new();
        // Two seconds split at a non-even callback boundary.
        let frames: Vec<f32> = (0..96_000)
            .flat_map(|i| {
                let sample = (i as f32 / 96_000.0 * 2.0 - 1.0).clamp(-1.0, 1.0);
                [sample, sample]
            })
            .collect();
        for chunk in frames.chunks(514) {
            output.extend(resampler.push(chunk));
        }
        assert_eq!(output.len(), 32_000);
        assert!((*output.first().unwrap() + 1.0).abs() < 0.001);
        assert!((*output.last().unwrap() - 1.0).abs() < 0.001);
    }

    #[test]
    fn recorder_keeps_pcm16_mono_and_caps_at_two_minutes() {
        let mut recorder = PcmRecorder::new(TARGET_RATE, 2).unwrap();
        recorder.push(&[1.0, 1.0, -1.0, -1.0]);
        assert_eq!(recorder.seconds(), 2.0 / TARGET_RATE as f64);
        let wav = recorder.wav();
        assert_eq!(&wav[..4], b"RIFF");
        assert_eq!(
            u32::from_le_bytes(wav[24..28].try_into().unwrap()),
            TARGET_RATE
        );
        assert_eq!(u16::from_le_bytes(wav[22..24].try_into().unwrap()), 1);
        assert_eq!(
            i16::from_le_bytes(wav[44..46].try_into().unwrap()),
            i16::MAX
        );
        assert_eq!(
            i16::from_le_bytes(wav[46..48].try_into().unwrap()),
            i16::MIN
        );

        let mut bounded = PcmRecorder::new(TARGET_RATE, 1).unwrap();
        bounded.push(&vec![0.25; MAX_SAMPLES + 100]);
        assert!(bounded.is_full());
        assert_eq!(bounded.seconds(), MAX_SECONDS as f64);
    }

    #[test]
    fn changed_preview_words_resolve_and_keep_stable_text() {
        let mut ui = DictationUi::default();
        ui.set_preview("hello word".into());
        ui.settle = 0;
        ui.set_preview("hello world".into());
        let first = ui.shown_preview(true);
        assert!(first.starts_with("hello "));
        assert_ne!(first, "hello world");
        assert_eq!(ui.shown_preview(false), "hello world");
        while ui.settle_tick() {}
        assert_eq!(ui.shown_preview(true), "hello world");
        ui.set_preview("你好 🌍".into());
        assert_eq!(ui.shown_preview(true), "你好 🌍");
    }

    #[test]
    fn preview_requests_are_single_flight_and_need_four_tenths_new_audio() {
        let mut pacer = PreviewPacer::default();
        assert!(!pacer.can_request(0.399));
        assert!(pacer.begin(0.4));
        assert!(!pacer.begin(10.0), "never overlap a preview");
        pacer.finish();
        assert!(!pacer.can_request(0.799));
        assert!(pacer.can_request(0.8));
    }

    #[test]
    fn dictation_state_machine_keeps_cancel_and_completion_separate() {
        let phase = Phase::Idle.transition(Transition::Dictate).unwrap();
        assert_eq!(phase, Phase::Checking);
        let phase = phase.transition(Transition::StatusAbsent).unwrap();
        assert_eq!(phase, Phase::Consent);
        let phase = phase.transition(Transition::ConsentGranted).unwrap();
        assert_eq!(phase, Phase::Downloading);
        let phase = phase.transition(Transition::DownloadReady).unwrap();
        assert_eq!(phase, Phase::Starting);
        let phase = phase.transition(Transition::CaptureStarted).unwrap();
        assert_eq!(phase, Phase::Recording);
        let phase = phase.transition(Transition::Cancel).unwrap();
        assert_eq!(phase, Phase::Idle);
        assert_eq!(Phase::Idle.transition(Transition::Cancel), None);

        let phase = Phase::Recording.transition(Transition::Stop).unwrap();
        let phase = phase.transition(Transition::AudioStopped).unwrap();
        assert_eq!(phase.transition(Transition::Finished), Some(Phase::Idle));
    }

    #[test]
    fn recorder_encodes_nonempty_voice_capture_at_the_core_minimum() {
        let samples = vec![0i16; (MIN_SECONDS * TARGET_RATE as f64) as usize];
        let wav = encode_wav(&samples);
        assert_eq!(wav.len(), 44 + samples.len() * 2);
        assert_eq!(
            u32::from_le_bytes(wav[40..44].try_into().unwrap()) as usize,
            samples.len() * 2
        );
    }

    #[test]
    fn start_and_stop_cues_are_two_soft_notes() {
        for cue in ["start", "stop"] {
            let samples = cue_pcm(cue);
            assert_eq!(samples.len(), 2 * (CUE_RATE as f32 * 0.07) as usize);
            assert!(samples.first().is_some_and(|sample| sample.abs() < 1e-5));
            assert!(samples.last().is_some_and(|sample| sample.abs() < 1e-5));
            assert!(samples.iter().any(|sample| sample.abs() > 0.1));
        }
    }

    #[test]
    #[ignore = "plays through the default audio output"]
    fn plays_dictation_cues_on_real_output() {
        for cue in ["loading", "start", "stop"] {
            play_cue(cue, true).expect("audio output should be available");
            thread::sleep(Duration::from_millis(500));
        }
    }

    /// Requires an available microphone and OS permission. `cpal::Stream` is
    /// created and destroyed only on its dedicated thread.
    #[test]
    #[ignore]
    fn captures_from_a_real_audio_device() {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let handle = CaptureHandle::start(1, tx);
        let started = tokio::runtime::Runtime::new().unwrap().block_on(async {
            tokio::time::timeout(Duration::from_secs(5), async {
                loop {
                    match rx.recv().await {
                        Some(CaptureEvent::Started { .. }) => return true,
                        Some(CaptureEvent::Failed { message, .. }) => panic!("{message}"),
                        None => return false,
                        _ => {}
                    }
                }
            })
            .await
            .unwrap_or(false)
        });
        assert!(started);
        handle.send(CaptureCommand::Stop { generation: 1 });
        handle.join();
    }
}
