//! Spoken answers: `POST /api/voice/speak` streams a mono PCM16 WAV and this
//! plays it as it arrives. Core never plays audio itself, so this works for a
//! remote Core as well.

use std::num::NonZero;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use futures_util::StreamExt;
use serde_json::json;

use crate::transport::Http;

/// The answer being spoken, so `speak.stop` can reach it from anywhere.
static CURRENT: Mutex<Option<Arc<AtomicBool>>> = Mutex::new(None);

const WAV_HEADER: usize = 44;

/// Stop the answer being spoken here. Returns whether there was one.
pub fn stop_local() -> bool {
    match CURRENT.lock().ok().and_then(|c| c.clone()) {
        Some(flag) => {
            flag.store(true, Ordering::SeqCst);
            true
        }
        None => false,
    }
}

/// Reassembles PCM16 samples from arbitrary byte chunks.
#[derive(Default)]
struct Pcm {
    carry: Option<u8>,
}

impl Pcm {
    fn samples(&mut self, bytes: &[u8]) -> Vec<f32> {
        let mut data = Vec::with_capacity(bytes.len() + 1);
        data.extend(self.carry.take());
        data.extend_from_slice(bytes);
        if data.len() % 2 == 1 {
            self.carry = data.pop();
        }
        data.chunks_exact(2)
            .map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0)
            .collect()
    }
}

type Player = (
    mpsc::Sender<Vec<f32>>,
    std::thread::JoinHandle<Result<(), String>>,
);

/// Parse the sample rate out of the streaming WAV header.
fn header_rate(header: &[u8]) -> Option<u32> {
    (header.len() >= WAV_HEADER && &header[..4] == b"RIFF" && &header[8..12] == b"WAVE")
        .then(|| u32::from_le_bytes([header[24], header[25], header[26], header[27]]))
        .filter(|r| *r > 0)
}

/// Speak the latest answer of `session`, returning when it has been played or stopped.
pub async fn speak(http: &Http, session: &str) -> Result<(), String> {
    let stop = Arc::new(AtomicBool::new(false));
    if let Ok(mut current) = CURRENT.lock() {
        if current.is_some() {
            return Err("Already speaking".into());
        }
        *current = Some(stop.clone());
    }
    let result = run(http, session, &stop).await;
    if let Ok(mut current) = CURRENT.lock() {
        *current = None;
    }
    result
}

async fn run(http: &Http, session: &str, stop: &Arc<AtomicBool>) -> Result<(), String> {
    let resp = http
        .post_stream("/api/voice/speak", &json!({ "session_id": session }))
        .await
        .map_err(|e| e.to_string())?;
    let mut body = resp.bytes_stream();
    let mut header: Vec<u8> = Vec::new();
    let mut pcm = Pcm::default();
    let mut player: Option<Player> = None;
    let mut failure = None;
    loop {
        if stop.load(Ordering::SeqCst) {
            break;
        }
        let next = tokio::select! {
            n = body.next() => n,
            _ = tokio::time::sleep(Duration::from_millis(100)) => continue,
        };
        let Some(chunk) = next else { break };
        let chunk = match chunk {
            Ok(c) => c,
            Err(e) => {
                failure = Some(e.to_string());
                break;
            }
        };
        let mut rest: &[u8] = &chunk;
        if player.is_none() {
            let take = (WAV_HEADER - header.len()).min(rest.len());
            header.extend_from_slice(&rest[..take]);
            rest = &rest[take..];
            if header.len() < WAV_HEADER {
                continue;
            }
            let Some(rate) = header_rate(&header) else {
                failure = Some("Core sent audio that is not a WAV".into());
                break;
            };
            player = Some(start_player(rate, stop.clone()));
        }
        if let Some((tx, _)) = &player {
            let samples = pcm.samples(rest);
            if !samples.is_empty() && tx.send(samples).is_err() {
                break;
            }
        }
    }
    if stop.load(Ordering::SeqCst) {
        let _ = http
            .call(
                reqwest::Method::POST,
                "/api/voice/speak/stop",
                &[],
                Some(&json!({})),
                None,
                None,
            )
            .await;
    }
    let played = match player {
        Some((tx, handle)) => {
            drop(tx);
            tokio::task::spawn_blocking(move || {
                handle.join().unwrap_or(Err("audio thread failed".into()))
            })
            .await
            .map_err(|e| e.to_string())?
        }
        None => Ok(()),
    };
    match failure {
        Some(f) => Err(f),
        None => played,
    }
}

/// Audio output lives on its own thread: the device handle is not `Send`.
fn start_player(
    rate: u32,
    stop: Arc<AtomicBool>,
) -> (
    mpsc::Sender<Vec<f32>>,
    std::thread::JoinHandle<Result<(), String>>,
) {
    let (tx, rx) = mpsc::channel::<Vec<f32>>();
    let handle = std::thread::spawn(move || {
        let sink = rodio::DeviceSinkBuilder::open_default_sink()
            .map_err(|e| format!("No audio output: {e}"))?;
        let player = rodio::Player::connect_new(sink.mixer());
        let (Some(channels), Some(rate)) = (NonZero::new(1u16), NonZero::new(rate)) else {
            return Err("bad audio format".to_owned());
        };
        loop {
            match rx.recv_timeout(Duration::from_millis(50)) {
                Ok(samples) => {
                    player.append(rodio::buffer::SamplesBuffer::new(channels, rate, samples))
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
            if stop.load(Ordering::SeqCst) {
                player.stop();
                return Ok(());
            }
        }
        while !player.empty() {
            if stop.load(Ordering::SeqCst) {
                player.stop();
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        Ok(())
    });
    (tx, handle)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pcm_survives_odd_chunk_boundaries() {
        let mut pcm = Pcm::default();
        let bytes = [0x00, 0x40, 0x00, 0xc0];
        let mut out = pcm.samples(&bytes[..1]);
        out.extend(pcm.samples(&bytes[1..3]));
        out.extend(pcm.samples(&bytes[3..]));
        assert_eq!(out, vec![0.5, -0.5]);
    }

    #[test]
    fn header_rate_reads_a_wav_and_refuses_others() {
        let mut h = vec![0u8; 44];
        h[..4].copy_from_slice(b"RIFF");
        h[8..12].copy_from_slice(b"WAVE");
        h[24..28].copy_from_slice(&24000u32.to_le_bytes());
        assert_eq!(header_rate(&h), Some(24000));
        h[0] = b'X';
        assert_eq!(header_rate(&h), None);
    }
}

#[cfg(test)]
mod live {
    /// Plays through the real audio device: `NEXUS_SPEAK_URL`, `_TOKEN` and `_SESSION`
    /// name a Core whose session has a completed answer.
    #[tokio::test]
    #[ignore]
    async fn speaks_a_real_answer() {
        let (url, token, session) = (
            std::env::var("NEXUS_SPEAK_URL").unwrap(),
            std::env::var("NEXUS_SPEAK_TOKEN").unwrap(),
            std::env::var("NEXUS_SPEAK_SESSION").unwrap(),
        );
        let http = crate::transport::Http::new(&url, &token).unwrap();
        super::speak(&http, &session).await.unwrap();
    }
}
