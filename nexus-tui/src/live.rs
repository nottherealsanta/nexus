//! GPT-Live call audio (MANAGER_PLAN.md section 5): the TUI only captures the microphone and plays speech.
//!
//! Core owns the call (its WebRTC peer to OpenAI, the sideband, the credentials and the Manager). This module moves
//! raw PCM16 mono 24 kHz over one WebSocket (`/api/manager/live/audio`): microphone frames up, speech frames down.
//! No WebRTC and no Opus here, so there is no helper binary. There is no echo cancellation: use headphones, or the
//! microphone is muted while speech plays (`half_duplex`). Not verified against the real service or real devices.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, mpsc as std_mpsc};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

use crate::dictation::{AudioMessage, Resampler, open_input};

pub const RATE: u32 = 24_000;
/// What the user sees while a call has been lost: the reason, once.
pub type Lost = mpsc::UnboundedReceiver<String>;

/// Levels shared with the orb, 0 to 1. Cheap to clone and read from any thread.
#[derive(Clone, Default)]
pub struct Levels {
    mic: Arc<AtomicU32>,
    speech: Arc<AtomicU32>,
}

impl Levels {
    pub fn mic(&self) -> f32 {
        (f32::from_bits(self.mic.load(Ordering::Relaxed)) * 5.0).clamp(0.0, 1.0)
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
    CALL.lock().ok().and_then(|c| c.as_ref().map(|a| a.levels.clone()))
}

pub fn interrupt() {
    if let Some(audio) = CALL.lock().ok().and_then(|c| c.as_ref().map(|a| a.flush.clone())) {
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
                devices(stop, muted, flush, levels, headphones, mic_tx, speech_rx, ready_tx)
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
        let task = tokio::spawn(socket(ws, stop.clone(), muted.clone(), mic_rx, speech_tx, lost_tx));
        Ok((
            Self { stop, muted, flush, levels, task },
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
    ws: tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
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
    let sink = match rodio::DeviceSinkBuilder::open_default_sink() {
        Ok(s) => s,
        Err(e) => {
            let _ = ready.send(Err(format!("No audio output: {e}")));
            return;
        }
    };
    let mut player = rodio::Player::connect_new(sink.mixer());
    let Some(mut resampler) = Resampler::to_rate(rate, channels, RATE) else {
        let _ = ready.send(Err("The microphone reported no usable format".into()));
        return;
    };
    let _ = ready.send(Ok(()));
    let (Some(one), Some(rate24)) = (std::num::NonZero::new(1u16), std::num::NonZero::new(RATE)) else {
        return;
    };
    let mut pending: Vec<f32> = Vec::new();
    let mut quiet_for = 0u32;
    while !stop.load(Ordering::SeqCst) {
        if flush.swap(false, Ordering::SeqCst) {
            player.stop();
            player = rodio::Player::connect_new(sink.mixer());
            while speech_rx.try_recv().is_ok() {}
            levels.speech.store(0f32.to_bits(), Ordering::Relaxed);
        }
        let mut speaking = false;
        while let Ok(samples) = speech_rx.try_recv() {
            levels.speech.store(rms(&samples).to_bits(), Ordering::Relaxed);
            speaking = true;
            player.append(rodio::buffer::SamplesBuffer::new(one, rate24, samples));
        }
        if speaking {
            quiet_for = 0;
        } else if player.empty() {
            quiet_for = quiet_for.saturating_add(1);
            if quiet_for > 3 {
                levels.speech.store(0f32.to_bits(), Ordering::Relaxed);
            }
        }
        match audio_rx.recv_timeout(Duration::from_millis(10)) {
            Ok(AudioMessage::Samples(raw)) => {
                let mono = resampler.push(&raw);
                levels.mic.store(rms(&mono).to_bits(), Ordering::Relaxed);
                // Without echo cancellation the microphone hears the speaker: with no headphones, hold it
                // while speech is playing so the voice does not answer itself.
                let hold = !headphones && !player.empty();
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
