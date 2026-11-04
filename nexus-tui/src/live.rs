//! GPT-Live call audio (docs/manager-design.md section 5): the TUI only captures the microphone and plays speech.
//!
//! Core owns the call (its WebRTC peer to OpenAI, the sideband, the credentials and the Manager). This module moves
//! raw PCM16 mono 24 kHz over one WebSocket (`/api/manager/live/audio`): microphone frames up, speech frames down.
//! No WebRTC and no Opus here, so there is no helper binary. There is no echo cancellation: use headphones, or the
//! microphone is muted while speech plays (`half_duplex`). Not verified against the real service or real devices.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{mpsc as std_mpsc, Arc};
use std::time::{Duration, Instant};

use cpal::traits::StreamTrait;
use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

use crate::dictation::{open_input, AudioMessage, Resampler};

pub const RATE: u32 = 24_000;
/// What the user sees while a call has been lost: the reason, once.
pub type Lost = mpsc::UnboundedReceiver<String>;

/// Levels shared with the orb, 0 to 1. Cheap to clone and read from any thread.
#[derive(Clone, Default)]
pub struct Levels {
    pub speaking_until: Arc<std::sync::Mutex<Option<Instant>>>,
    mic: Arc<AtomicU32>,
    speech: Arc<AtomicU32>,
}

impl Levels {
    pub fn mic(&self) -> f32 {
        (f32::from_bits(self.mic.load(Ordering::Relaxed)) * 5.0).clamp(0.0, 1.0)
    }
    pub fn speaking(&self, now: Instant) -> bool {
        self.speaking_until
            .lock()
            .is_ok_and(|at| at.is_some_and(|at| now < at))
    }

    pub fn speech(&self) -> f32 {
        (f32::from_bits(self.speech.load(Ordering::Relaxed)) * 5.0).clamp(0.0, 1.0)
    }
}

impl std::fmt::Debug for Levels {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Levels")
    }
}

impl PartialEq for Levels {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.mic, &other.mic)
    }
}

fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    (samples.iter().map(|s| s * s).sum::<f32>() / samples.len() as f32).sqrt()
}

/// Half duplex follows audible samples consumed by the output device. A live
/// WebRTC track sends silence continuously, so a nonempty player is not speech.
#[derive(Clone)]
struct EchoGuard {
    start: Instant,
    until_ms: Arc<AtomicU64>,
}

impl EchoGuard {
    fn new() -> Self {
        Self {
            start: Instant::now(),
            until_ms: Arc::new(AtomicU64::new(0)),
        }
    }

    fn observe_at(&self, level: f32, now_ms: u64) {
        if level > 0.003 {
            self.until_ms
                .store(now_ms.saturating_add(200), Ordering::Relaxed);
        }
    }

    fn holds_at(&self, now_ms: u64, headphones: bool) -> bool {
        !headphones && now_ms < self.until_ms.load(Ordering::Relaxed)
    }
}

/// Meter the samples actually pulled by rodio, including silent frames.
struct SpeechMeter<S> {
    inner: S,
    gate: EchoGuard,
    levels: Levels,
    sum: f32,
    count: usize,
}

impl<S: rodio::Source> Iterator for SpeechMeter<S> {
    type Item = rodio::Sample;

    fn next(&mut self) -> Option<Self::Item> {
        let sample = self.inner.next();
        if let Some(value) = sample {
            self.sum += value * value;
            self.count += 1;
            if self.count >= 240 {
                let level = (self.sum / self.count as f32).sqrt();
                self.levels.speech.store(level.to_bits(), Ordering::Relaxed);
                if level > 0.01 {
                    *self.levels.speaking_until.lock().unwrap() =
                        Some(Instant::now() + Duration::from_millis(350));
                }
                self.gate
                    .observe_at(level, self.gate.start.elapsed().as_millis() as u64);
                self.sum = 0.0;
                self.count = 0;
            }
        }
        sample
    }
}

impl<S: rodio::Source> rodio::Source for SpeechMeter<S> {
    fn current_span_len(&self) -> Option<usize> {
        self.inner.current_span_len()
    }
    fn channels(&self) -> rodio::ChannelCount {
        self.inner.channels()
    }
    fn sample_rate(&self) -> rodio::SampleRate {
        self.inner.sample_rate()
    }
    fn total_duration(&self) -> Option<Duration> {
        self.inner.total_duration()
    }
}

/// One continuous source keeps the output resampler alive across network packets.
/// Collect 60 ms before starting (or recovering from an underrun).
#[derive(Default)]
struct AudioBuffer {
    samples: std::collections::VecDeque<f32>,
    started: bool,
}
impl AudioBuffer {
    fn push(&mut self, samples: Vec<f32>) {
        self.samples.extend(samples);
    }
    fn next_sample(&mut self) -> f32 {
        if !self.started {
            if self.samples.len() < RATE as usize * 60 / 1000 {
                return 0.0;
            }
            self.started = true;
        }
        match self.samples.pop_front() {
            Some(sample) => sample,
            None => {
                self.started = false;
                0.0
            }
        }
    }
    fn clear(&mut self) {
        self.samples.clear();
        self.started = false;
    }
}

struct LiveSource(Arc<std::sync::Mutex<AudioBuffer>>);
impl Iterator for LiveSource {
    type Item = f32;
    fn next(&mut self) -> Option<f32> {
        Some(self.0.lock().map_or(0.0, |mut buffer| buffer.next_sample()))
    }
}
impl rodio::Source for LiveSource {
    fn current_span_len(&self) -> Option<usize> {
        None
    }
    fn channels(&self) -> rodio::ChannelCount {
        std::num::NonZero::new(1).unwrap()
    }
    fn sample_rate(&self) -> rodio::SampleRate {
        std::num::NonZero::new(RATE).unwrap()
    }
    fn total_duration(&self) -> Option<Duration> {
        None
    }
}

pub fn to_pcm16(samples: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for s in samples {
        let v = (s.clamp(-1.0, 1.0) * 32767.0) as i16;
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

pub fn from_pcm16(bytes: &[u8]) -> Vec<f32> {
    bytes
        .chunks_exact(2)
        .map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0)
        .collect()
}

static CALL: std::sync::Mutex<Option<LiveAudio>> = std::sync::Mutex::new(None);
static LOST: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

/// Open the running call's audio for this stream: Core opens its own call first, then the socket and devices here.
/// Errors are fit for a toast. Half-duplex unless `NEXUS_LIVE_HEADPHONES=1`.
pub async fn open(http: &crate::transport::Http, stream_id: &str) -> Result<(), String> {
    close();
    http.call(
        reqwest::Method::POST,
        "/api/manager/live/start",
        &[],
        Some(&serde_json::json!({ "stream_id": stream_id })),
        None,
        None,
    )
    .await
    .map_err(|e| match e.api() {
        Some(a) => a.message.clone(),
        None => e.to_string(),
    })?;
    let url = http.socket_url("/api/manager/live/audio", &[("stream_id", stream_id)]);
    let headphones = std::env::var("NEXUS_LIVE_HEADPHONES").is_ok_and(|v| v == "1");
    let (audio, mut lost) = LiveAudio::start(url, headphones).await?;
    if let Ok(mut slot) = LOST.lock() {
        *slot = None;
    }
    tokio::spawn(async move {
        if let Some(why) = lost.recv().await {
            if let Ok(mut slot) = LOST.lock() {
                *slot = Some(why);
            }
        }
    });
    if let Ok(mut call) = CALL.lock() {
        *call = Some(audio);
    }
    Ok(())
}

/// Hang up this client's side. Idempotent.
pub fn close() {
    if let Some(audio) = CALL.lock().ok().and_then(|mut c| c.take()) {
        audio.stop();
    }
}

pub fn is_open() -> bool {
    CALL.lock().is_ok_and(|c| c.is_some())
}

pub fn levels() -> Option<Levels> {
    CALL.lock()
        .ok()
        .and_then(|c| c.as_ref().map(|a| a.levels.clone()))
}

pub fn interrupt() {
    if let Some(audio) = CALL
        .lock()
        .ok()
        .and_then(|c| c.as_ref().map(|a| a.flush.clone()))
    {
        audio.store(true, Ordering::SeqCst);
    }
}

/// The reason the call's audio ended on its own, once.
pub fn take_lost() -> Option<String> {
    LOST.lock().ok().and_then(|mut l| l.take())
}

/// A running call's audio. Dropping it (or `stop`) closes the socket and releases both devices.
pub struct LiveAudio {
    stop: Arc<AtomicBool>,
    muted: Arc<AtomicBool>,
    flush: Arc<AtomicBool>,
    pub levels: Levels,
    task: tokio::task::JoinHandle<()>,
}

impl LiveAudio {
    /// Open the socket and both devices. `url` is the full `ws(s)://.../api/manager/live/audio?...` address.
    /// Errors are fit for a toast. `lost` gets one message if the call's audio ends on its own.
    pub async fn start(url: String, headphones: bool) -> Result<(Self, Lost), String> {
        let (ws, _) = tokio::time::timeout(
            Duration::from_secs(10),
            tokio_tungstenite::connect_async(url),
        )
        .await
        .map_err(|_| "Core did not open the call's audio connection in time".to_owned())?
        .map_err(|e| format!("Could not open the call's audio connection to Core: {e}"))?;
        let stop = Arc::new(AtomicBool::new(false));
        let muted = Arc::new(AtomicBool::new(false));
        let flush = Arc::new(AtomicBool::new(false));
        let levels = Levels::default();
        let (mic_tx, mic_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        let (speech_tx, speech_rx) = std_mpsc::channel::<Vec<f32>>();
        let (ready_tx, ready_rx) = std_mpsc::channel::<Result<(), String>>();
        {
            let (stop, muted, flush, levels) =
                (stop.clone(), muted.clone(), flush.clone(), levels.clone());
            std::thread::spawn(move || {
                devices(
                    stop, muted, flush, levels, headphones, mic_tx, speech_rx, ready_tx,
                )
            });
        }
        match tokio::task::spawn_blocking(move || ready_rx.recv_timeout(Duration::from_secs(5)))
            .await
        {
            Ok(Ok(Ok(()))) => {}
            Ok(Ok(Err(e))) => return Err(e),
            _ => return Err("The audio devices did not open in time".into()),
        }
        let (lost_tx, lost) = mpsc::unbounded_channel();
        let task = tokio::spawn(socket(
            ws,
            stop.clone(),
            muted.clone(),
            mic_rx,
            speech_tx,
            lost_tx,
        ));
        Ok((
            Self {
                stop,
                muted,
                flush,
                levels,
                task,
            },
            lost,
        ))
    }

    pub fn set_muted(&self, muted: bool) {
        self.muted.store(muted, Ordering::SeqCst);
    }

    /// Drop speech that is queued on the speaker (barge-in).
    pub fn interrupt(&self) {
        self.flush.store(true, Ordering::SeqCst);
    }

    pub fn stop(self) {
        self.stop.store(true, Ordering::SeqCst);
        self.task.abort();
    }
}

impl Drop for LiveAudio {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.task.abort();
    }
}

async fn socket(
    ws: tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    stop: Arc<AtomicBool>,
    muted: Arc<AtomicBool>,
    mut mic: mpsc::UnboundedReceiver<Vec<u8>>,
    speech: std_mpsc::Sender<Vec<f32>>,
    lost: mpsc::UnboundedSender<String>,
) {
    let (mut tx, mut rx) = ws.split();
    let mut was_muted = false;
    let why = loop {
        tokio::select! {
            frame = mic.recv() => {
                let Some(frame) = frame else { break "The microphone stopped.".to_owned() };
                let now = muted.load(Ordering::SeqCst);
                if now != was_muted {
                    was_muted = now;
                    if tx.send(Message::text(if now { "mute" } else { "unmute" })).await.is_err() {
                        break "The voice connection to Core closed.".to_owned();
                    }
                }
                if !now && tx.send(Message::binary(frame)).await.is_err() {
                    break "The voice connection to Core closed.".to_owned();
                }
            }
            message = rx.next() => match message {
                Some(Ok(Message::Binary(data))) => { let _ = speech.send(from_pcm16(&data)); }
                Some(Ok(Message::Close(_))) | None => break "The voice connection to Core closed.".to_owned(),
                Some(Ok(_)) => {}
                Some(Err(e)) => break format!("The voice connection to Core failed: {e}"),
            },
            _ = tokio::time::sleep(Duration::from_millis(200)) => {
                if stop.load(Ordering::SeqCst) { return; }
            }
        }
    };
    if !stop.load(Ordering::SeqCst) {
        let _ = lost.send(why);
    }
    let _ = tx.close().await;
}

/// The audio device thread: cpal input to 24 kHz frames, rodio output of Core's speech. The handles are not `Send`,
/// so they live and die here.
#[allow(clippy::too_many_arguments)]
fn devices(
    stop: Arc<AtomicBool>,
    muted: Arc<AtomicBool>,
    flush: Arc<AtomicBool>,
    levels: Levels,
    headphones: bool,
    mic_tx: mpsc::UnboundedSender<Vec<u8>>,
    speech_rx: std_mpsc::Receiver<Vec<f32>>,
    ready: std_mpsc::Sender<Result<(), String>>,
) {
    let (audio_tx, audio_rx) = std_mpsc::channel::<AudioMessage>();
    let input = open_input(audio_tx);
    let (input_stream, rate, channels) = match input {
        Ok(v) => v,
        Err(e) => {
            let _ = ready.send(Err(e));
            return;
        }
    };
    let mut sink = match rodio::DeviceSinkBuilder::open_default_sink() {
        Ok(s) => s,
        Err(e) => {
            let _ = ready.send(Err(format!("No audio output: {e}")));
            return;
        }
    };
    sink.log_on_drop(false);
    let player = rodio::Player::connect_new(sink.mixer());
    let Some(mut resampler) = Resampler::to_rate(rate, channels, RATE) else {
        let _ = ready.send(Err("The microphone reported no usable format".into()));
        return;
    };
    if let Err(error) = input_stream.play() {
        let _ = ready.send(Err(format!("Could not start microphone input: {error}")));
        return;
    }
    let _ = ready.send(Ok(()));
    let mut pending: Vec<f32> = Vec::new();
    let gate = EchoGuard::new();
    let output = Arc::new(std::sync::Mutex::new(AudioBuffer::default()));
    player.append(SpeechMeter {
        inner: LiveSource(output.clone()),
        gate: gate.clone(),
        levels: levels.clone(),
        sum: 0.0,
        count: 0,
    });
    while !stop.load(Ordering::SeqCst) {
        if flush.swap(false, Ordering::SeqCst) {
            output.lock().unwrap().clear();
            while speech_rx.try_recv().is_ok() {}
            levels.speech.store(0f32.to_bits(), Ordering::Relaxed);
            gate.until_ms.store(0, Ordering::Relaxed);
            *levels.speaking_until.lock().unwrap() = None;
        }
        while let Ok(samples) = speech_rx.try_recv() {
            output.lock().unwrap().push(samples);
        }
        match audio_rx.recv_timeout(Duration::from_millis(10)) {
            Ok(AudioMessage::Samples(raw)) => {
                let mono = resampler.push(&raw);
                levels.mic.store(rms(&mono).to_bits(), Ordering::Relaxed);
                // Without echo cancellation the microphone hears the speaker: with no headphones, hold it
                // while speech is playing so the voice does not answer itself.
                let hold = gate.holds_at(gate.start.elapsed().as_millis() as u64, headphones);
                if hold {
                    pending.clear();
                } else {
                    pending.extend(mono);
                }
                while pending.len() >= 480 {
                    let frame: Vec<f32> = pending.drain(..480).collect();
                    if !muted.load(Ordering::SeqCst) {
                        let _ = mic_tx.send(to_pcm16(&frame));
                    } else {
                        let _ = mic_tx.send(Vec::new());
                    }
                }
            }
            Ok(AudioMessage::Failed(_)) | Err(std_mpsc::RecvTimeoutError::Disconnected) => break,
            Err(std_mpsc::RecvTimeoutError::Timeout) => {}
        }
    }
    drop(input_stream);
    player.stop();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn playback_spans_packets_and_absorbs_jitter_before_resuming() {
        let mut buffer = AudioBuffer::default();
        buffer.push(vec![0.25; 480]);
        assert_eq!(buffer.next_sample(), 0.0); // wait for the 60 ms cushion
        assert_eq!(buffer.samples.len(), 480); // no audio discarded
        buffer.push(vec![0.5; 960]);
        for _ in 0..480 {
            assert_eq!(buffer.next_sample(), 0.25);
        }
        for _ in 0..960 {
            assert_eq!(buffer.next_sample(), 0.5);
        }
        assert_eq!(buffer.next_sample(), 0.0); // underrun resets buffering
        buffer.push(vec![0.75; 480]);
        assert_eq!(buffer.next_sample(), 0.0);
        buffer.push(vec![1.0; 960]);
        assert_eq!(buffer.next_sample(), 0.75);
        buffer.clear();
        assert_eq!(buffer.next_sample(), 0.0);
    }

    #[test]
    fn the_speaking_colour_survives_short_pauses() {
        let levels = Levels::default();
        let now = Instant::now();
        *levels.speaking_until.lock().unwrap() = Some(now + Duration::from_millis(350));
        assert!(levels.speaking(now + Duration::from_millis(300)));
        assert!(!levels.speaking(now + Duration::from_millis(351)));
    }

    #[test]
    fn continuous_silent_playback_does_not_block_the_microphone() {
        let scenario: serde_json::Value = serde_json::from_str(include_str!(
            "../../interface/scenarios/manager-live-silence.json"
        ))
        .unwrap();
        let gate = EchoGuard::new();
        let levels = Levels::default();
        for _ in 0..scenario["silent_packets"].as_u64().unwrap() {
            let source = rodio::buffer::SamplesBuffer::new(
                std::num::NonZero::new(1).unwrap(),
                std::num::NonZero::new(RATE).unwrap(),
                vec![0.0; scenario["samples_per_packet"].as_u64().unwrap() as usize],
            );
            let meter = SpeechMeter {
                inner: source,
                gate: gate.clone(),
                levels: levels.clone(),
                sum: 0.0,
                count: 0,
            };
            assert_eq!(meter.count(), 480);
            assert!(!gate.holds_at(0, false));
        }
        assert_eq!(levels.speech(), 0.0);
        // Audible playback holds the mic, including a short echo tail; silence
        // cannot keep extending the hold. Headphones bypass the echo guard.
        gate.observe_at(0.1, 100);
        gate.observe_at(0.0, 250);
        assert!(gate.holds_at(299, false));
        assert!(!gate.holds_at(300, false));
        assert!(!gate.holds_at(150, true));
    }

    #[test]
    fn pcm_round_trips_within_one_step() {
        let samples = [0.0, 0.5, -0.5, 1.0, -1.0];
        let back = from_pcm16(&to_pcm16(&samples));
        for (a, b) in samples.iter().zip(back) {
            assert!((a - b).abs() < 0.001, "{a} vs {b}");
        }
    }

    #[test]
    fn an_odd_trailing_byte_is_ignored() {
        assert_eq!(from_pcm16(&[0, 0, 1]).len(), 1);
    }

    #[test]
    fn the_resampler_makes_24k_frames_from_48k_stereo() {
        let mut r = Resampler::to_rate(48_000, 2, RATE).unwrap();
        let out = r.push(&vec![0.25; 48_000 * 2]);
        assert_eq!(out.len(), 24_000);
    }

    #[test]
    fn levels_start_silent_and_scale_speech_to_the_range() {
        let levels = Levels::default();
        assert_eq!(levels.mic(), 0.0);
        levels.speech.store(0.1f32.to_bits(), Ordering::Relaxed);
        assert!((levels.speech() - 0.5).abs() < 0.001);
    }
}
