//! iOS microphone and speaker for the Manager's Core-owned GPT Live call.
use std::sync::{
    atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use nsh_core::prelude::*;
use objc2_avf_audio::{
    AVAudioSession, AVAudioSessionCategoryOptions, AVAudioSessionCategoryPlayAndRecord,
    AVAudioSessionModeVoiceChat,
};
use tokio::sync::mpsc;

use crate::live_pcm::{CaptureFrames, Playback};
use crate::live_socket::{Input, LiveSocket, Output};

static ACTIVE: Mutex<Option<Arc<State>>> = Mutex::new(None);
static EPOCH: AtomicU64 = AtomicU64::new(0);

struct State {
    stop: AtomicBool,
    muted: AtomicBool,
    flush: AtomicBool,
    local: AtomicU32,
    remote: AtomicU32,
    echo_until: AtomicU64,
    lost: Mutex<Option<String>>,
    worker: AtomicBool,
    finished: AtomicBool,
}

impl State {
    fn new() -> Self {
        Self {
            stop: AtomicBool::new(false),
            muted: AtomicBool::new(false),
            flush: AtomicBool::new(false),
            local: AtomicU32::new(0),
            remote: AtomicU32::new(0),
            echo_until: AtomicU64::new(0),
            lost: Mutex::new(None),
            worker: AtomicBool::new(false),
            finished: AtomicBool::new(false),
        }
    }
}

pub(crate) fn is_busy() -> bool {
    ACTIVE.lock().map_or(true, |active| active.is_some())
}

fn release(state: &Arc<State>) {
    if let Ok(mut active) = ACTIVE.lock() {
        if active
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, state))
        {
            *active = None;
        }
    }
}

#[derive(JsObject)]
pub struct LiveLevels {
    pub local: f64,
    pub remote: f64,
    pub lost: Option<String>,
}

#[module]
pub struct LiveAudio;

#[module]
impl LiveAudio {
    pub fn available() -> bool {
        true
    }

    pub async fn start(url: String) -> Result<(), String> {
        let state = Arc::new(State::new());
        let epoch = EPOCH.load(Ordering::Acquire);
        {
            let mut active = ACTIVE.lock().map_err(|_| "Audio state is unavailable")?;
            if active.is_some() || crate::ios::capture_busy() {
                return Err("Another microphone recording is active".into());
            }
            *active = Some(state.clone());
        }
        let session = unsafe { AVAudioSession::sharedInstance() };
        let result = async {
            crate::ios::ensure_permission(&session).await?;
            if state.stop.load(Ordering::Acquire) || EPOCH.load(Ordering::Acquire) != epoch {
                return Err("Voice call cancelled".into());
            }
            let category = unsafe { AVAudioSessionCategoryPlayAndRecord }
                .ok_or("Call audio is unavailable")?;
            let mode = unsafe { AVAudioSessionModeVoiceChat }.ok_or("Call audio is unavailable")?;
            unsafe {
                session
                    .setCategory_mode_options_error(
                        category,
                        mode,
                        AVAudioSessionCategoryOptions::DefaultToSpeaker
                            | AVAudioSessionCategoryOptions::AllowBluetoothHFP,
                    )
                    .map_err(|e| e.localizedDescription().to_string())?;
                session
                    .setActive_error(true)
                    .map_err(|e| e.localizedDescription().to_string())?;
            }
            let (ready_tx, ready_rx) = futures_channel::oneshot::channel();
            let worker_state = state.clone();
            state.worker.store(true, Ordering::Release);
            if std::thread::Builder::new()
                .name("nexus-live-audio".into())
                .spawn(move || {
                    let runtime = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build();
                    match runtime {
                        Ok(runtime) => runtime.block_on(run(url, worker_state, ready_tx)),
                        Err(_) => {
                            let _ = ready_tx.send(Err("Could not start voice audio".into()));
                            let session = unsafe { AVAudioSession::sharedInstance() };
                            let _ = unsafe { session.setActive_error(false) };
                            worker_state.finished.store(true, Ordering::Release);
                            if worker_state.stop.load(Ordering::Acquire) {
                                release(&worker_state);
                            }
                        }
                    }
                })
                .is_err()
            {
                state.worker.store(false, Ordering::Release);
                return Err("Could not start voice audio".into());
            }
            ready_rx
                .await
                .map_err(|_| "Voice audio stopped while starting".to_owned())??;
            if state.stop.load(Ordering::Acquire) || EPOCH.load(Ordering::Acquire) != epoch {
                return Err("Voice call cancelled".into());
            }
            Ok(())
        }
        .await;
        if result.is_err() {
            state.stop.store(true, Ordering::Release);
            if !state.worker.load(Ordering::Acquire) {
                let _ = unsafe { session.setActive_error(false) };
                release(&state);
            } else if state.finished.load(Ordering::Acquire) {
                release(&state);
            }
        }
        result
    }

    pub fn stop() {
        EPOCH.fetch_add(1, Ordering::AcqRel);
        if let Ok(mut active) = ACTIVE.lock() {
            if let Some(state) = active.as_ref() {
                state.stop.store(true, Ordering::Release);
                if state.finished.load(Ordering::Acquire) {
                    *active = None;
                }
            }
        }
    }

    pub fn interrupt() {
        if let Ok(active) = ACTIVE.lock() {
            if let Some(state) = active.as_ref() {
                state.flush.store(true, Ordering::Release);
            }
        }
    }
    pub fn set_muted(muted: bool) {
        if let Ok(active) = ACTIVE.lock() {
            if let Some(state) = active.as_ref() {
                state.muted.store(muted, Ordering::Release);
            }
        }
    }
    pub fn levels() -> LiveLevels {
        if let Ok(active) = ACTIVE.lock() {
            if let Some(state) = active.as_ref() {
                return LiveLevels {
                    local: f32::from_bits(state.local.load(Ordering::Relaxed)) as f64,
                    remote: f32::from_bits(state.remote.load(Ordering::Relaxed)) as f64,
                    lost: state.lost.lock().ok().and_then(|mut lost| lost.take()),
                };
            }
        }
        LiveLevels {
            local: 0.0,
            remote: 0.0,
            lost: None,
        }
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

async fn run(
    url: String,
    state: Arc<State>,
    ready: futures_channel::oneshot::Sender<Result<(), String>>,
) {
    let result = open(url, state.clone(), ready).await;
    if let Err(error) = result {
        if !state.stop.load(Ordering::Acquire) {
            if let Ok(mut lost) = state.lost.lock() {
                *lost = Some(error);
            }
        }
    }
    let session = unsafe { AVAudioSession::sharedInstance() };
    let _ = unsafe { session.setActive_error(false) };
    state.finished.store(true, Ordering::Release);
    if state.stop.load(Ordering::Acquire) {
        release(&state);
    }
}

async fn open(
    url: String,
    state: Arc<State>,
    ready: futures_channel::oneshot::Sender<Result<(), String>>,
) -> Result<(), String> {
    let mut socket = match LiveSocket::connect(url).await {
        Ok(socket) => socket,
        Err(error) => {
            let _ = ready.send(Err(error.clone()));
            return Err(error);
        }
    };
    let (mic_tx, mut mic_rx) = mpsc::channel::<Vec<u8>>(25);
    let output = Arc::new(Mutex::new(Playback::new(48_000, 1).unwrap()));
    let streams = open_devices(state.clone(), mic_tx, output.clone());
    let (input, speaker) = match streams {
        Ok(streams) => streams,
        Err(error) => {
            let _ = ready.send(Err(error.clone()));
            return Err(error);
        }
    };
    if let Err(error) = input.play().and_then(|_| speaker.play()) {
        let error = format!("Could not start call audio: {error}");
        let _ = ready.send(Err(error.clone()));
        return Err(error);
    }
    if ready.send(Ok(())).is_err() {
        return Ok(());
    }
    let mut muted = false;
    loop {
        tokio::select! {
            audio = mic_rx.recv() => {
                let Some(audio) = audio else { return Err("The microphone stopped.".into()) };
                let next = state.muted.load(Ordering::Acquire);
                if next != muted { muted = next; if socket.input.send(Input::Mute(muted)).await.is_err() { return Err("The voice connection to Core closed.".into()); } }
                if !muted && socket.input.try_send(Input::Audio(audio)).is_err() && socket.input.is_closed() { return Err("The voice connection to Core closed.".into()); }
            }
            frame = socket.output.recv() => match frame {
                Some(Output::Audio(bytes)) => { if let Ok(mut buffer) = output.lock() { buffer.push(&bytes); } }
                Some(Output::Closed(reason)) => return Err(reason),
                None => return Err("The voice connection to Core closed.".into()),
            },
            _ = tokio::time::sleep(Duration::from_millis(40)) => {
                if state.stop.load(Ordering::Acquire) { break; }
                if state.flush.swap(false, Ordering::AcqRel) { if let Ok(mut buffer) = output.lock() { buffer.clear(); } }
            }
        }
    }
    Ok(())
}

fn open_devices(
    state: Arc<State>,
    mic: mpsc::Sender<Vec<u8>>,
    output: Arc<Mutex<Playback>>,
) -> Result<(cpal::Stream, cpal::Stream), String> {
    let host = cpal::default_host();
    let input = host
        .default_input_device()
        .ok_or("No microphone is available")?;
    let input_config = input
        .default_input_config()
        .map_err(|e| format!("Microphone unavailable: {e}"))?;
    if input_config.sample_format() != cpal::SampleFormat::F32 {
        return Err("Unsupported microphone format".into());
    }
    let config = input_config.config();
    let mut capture = CaptureFrames::new(config.sample_rate, config.channels as usize)
        .ok_or("Invalid microphone format")?;
    let mic_state = state.clone();
    let microphone = input
        .build_input_stream::<f32, _, _>(
            config,
            move |samples, _| {
                let level = if samples.is_empty() {
                    0.0
                } else {
                    (samples.iter().map(|v| v * v).sum::<f32>() / samples.len() as f32).sqrt()
                };
                mic_state
                    .local
                    .store((level * 3.0).min(1.0).to_bits(), Ordering::Relaxed);
                let echo = now_ms() < mic_state.echo_until.load(Ordering::Relaxed) && level <= 0.12;
                for frame in capture.push(samples) {
                    let _ = mic.try_send(if echo { vec![0; frame.len()] } else { frame });
                }
            },
            |_| {},
            None,
        )
        .map_err(|e| format!("Could not open microphone: {e}"))?;
    let speaker = host
        .default_output_device()
        .ok_or("No speaker is available")?;
    let output_config = speaker
        .default_output_config()
        .map_err(|e| format!("Speaker unavailable: {e}"))?;
    if output_config.sample_format() != cpal::SampleFormat::F32 {
        return Err("Unsupported speaker format".into());
    }
    let config = output_config.config();
    *output.lock().map_err(|_| "Voice playback unavailable")? =
        Playback::new(config.sample_rate, config.channels as usize)
            .ok_or("Invalid speaker format")?;
    let speaker_state = state;
    let stream = speaker
        .build_output_stream::<f32, _, _>(
            config,
            move |samples, _| {
                let level = output.lock().map_or(0.0, |mut buffer| buffer.fill(samples));
                speaker_state
                    .remote
                    .store((level * 3.0).min(1.0).to_bits(), Ordering::Relaxed);
                if level > 0.003 {
                    speaker_state
                        .echo_until
                        .store(now_ms().saturating_add(200), Ordering::Relaxed);
                }
            },
            |_| {},
            None,
        )
        .map_err(|e| format!("Could not open speaker: {e}"))?;
    Ok((microphone, stream))
}
