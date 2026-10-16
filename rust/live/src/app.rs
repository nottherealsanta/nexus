//! The call state machine. [`run`] consumes parsed stdin input and produces
//! stdout events; it owns the peer and the audio devices and guarantees
//! exactly one `closed` event, emitted last, on every exit path.

use crate::audio::{AudioBackend, AudioGuard, Pipes};
use crate::codec::Encoder;
use crate::dsp::{now_ms, rms_i16, EchoGate, GateConfig, FRAME};
use crate::peer::{Peer, PeerEvent};
use crate::protocol::{parse_command, Command, Event, Input, Phase, MAX_LINE};
use std::time::Duration;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio::time::{interval, MissedTickBehavior};

#[derive(Debug, Clone, Copy, Default)]
pub struct Options {
    pub gate: GateConfig,
}

/// Levels are emitted every Nth 20 ms tick: 5 gives 10 Hz.
const LEVEL_EVERY: u32 = 5;

pub async fn run(
    mut inputs: UnboundedReceiver<Input>,
    events: UnboundedSender<Event>,
    mut backend: Box<dyn AudioBackend>,
    pipes: Pipes,
    opts: Options,
) {
    let emit = |e: Event| {
        let _ = events.send(e);
    };
    let (ptx, mut prx) = unbounded_channel::<PeerEvent>();
    let mut peer: Option<Peer> = None;
    let mut guard: Option<AudioGuard> = None;
    let mut encoder: Option<Encoder> = None;
    let mut gate = EchoGate::new(opts.gate);
    let mut muted = false;
    let mut paused = false;
    let (mut answered, mut connected, mut dc_open, mut ready) = (false, false, false, false);
    let mut phase = Phase::Listening;
    let mut tick = interval(Duration::from_millis(20));
    tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut n: u32 = 0;
    let (mut in_acc, mut in_n, mut last_levels_zero) = (0f32, 0u32, false);

    let reason: String = loop {
        tokio::select! {
            input = inputs.recv() => match input {
                None | Some(Input::Eof) => break "stdin_eof".to_string(),
                Some(Input::TooLong) => emit(Event::Error {
                    message: format!("stdin line exceeds {MAX_LINE} bytes; ignored"),
                }),
                Some(Input::Line(line)) => {
                    if line.trim().is_empty() {
                        continue;
                    }
                    match parse_command(&line) {
                        Err(message) => emit(Event::Error { message }),
                        Ok(Command::Stop) => break "stop".to_string(),
                        Ok(Command::Mute { on }) => muted = on,
                        Ok(Command::Pause { on }) => {
                            paused = on;
                            if on {
                                // Audio already received but not yet played is discarded.
                                if let Ok(mut p) = pipes.playback.lock() {
                                    p.clear();
                                }
                            }
                        }
                        Ok(Command::Offer) => {
                            if peer.is_some() {
                                emit(Event::Error { message: "offer already created".into() });
                                continue;
                            }
                            match start(&mut backend, &pipes, ptx.clone()).await {
                                Ok((p, g, e, sdp)) => {
                                    peer = Some(p);
                                    guard = Some(g);
                                    encoder = Some(e);
                                    emit(Event::Offer { sdp });
                                }
                                Err((reason, message)) => {
                                    emit(Event::Error { message });
                                    break reason;
                                }
                            }
                        }
                        Ok(Command::Answer { sdp }) => {
                            let Some(p) = peer.as_ref() else {
                                emit(Event::Error { message: "answer before offer".into() });
                                continue;
                            };
                            if answered {
                                emit(Event::Error { message: "answer already applied".into() });
                                continue;
                            }
                            match p.apply_answer(sdp).await {
                                Ok(()) => answered = true,
                                Err(message) => {
                                    emit(Event::Error { message: format!("answer rejected: {message}") });
                                    break "answer_rejected".to_string();
                                }
                            }
                        }
                    }
                }
            },
            Some(pe) = prx.recv() => match pe {
                PeerEvent::Connected => connected = true,
                PeerEvent::DataChannelOpen => dc_open = true,
                PeerEvent::DcError(message) => emit(Event::DcError { message }),
                PeerEvent::Closed(r) => break r,
            },
            _ = tick.tick(), if peer.is_some() => {
                let now = now_ms();
                let out = pipes.out_level.get();
                gate.observe_output(now, out);

                let mic = pipes.capture.lock().ok().and_then(|mut r| r.pop_frame(FRAME));
                let mic_rms = mic.as_deref().map(rms_i16).unwrap_or(0.0);
                let pcm = match mic {
                    Some(f) if !muted && !paused && gate.allow_mic(now, mic_rms) => f,
                    // Muted, paused, gated or starved: send silence so RTP keeps its clock.
                    _ => vec![0i16; FRAME],
                };
                if answered {
                    if let Some(enc) = encoder.as_mut() {
                        match enc.encode(&pcm) {
                            Ok(pkt) => {
                                if let Some(p) = peer.as_ref() {
                                    if let Err(m) = p.send_opus(pkt).await {
                                        eprintln!("nexus-live: send failed: {m}");
                                    }
                                }
                            }
                            Err(message) => emit(Event::Error { message }),
                        }
                    }
                }
                if paused {
                    // Keep the speaker empty while paused (the decoder keeps filling the ring).
                    if let Ok(mut p) = pipes.playback.lock() {
                        p.clear();
                    }
                }
                in_acc = in_acc.max(if muted || paused { 0.0 } else { mic_rms });
                in_n += 1;

                if !ready && answered && connected && dc_open {
                    ready = true;
                    emit(Event::Ready);
                    emit(Event::Phase { phase });
                }
                if ready {
                    let now_phase = if gate.speaking(now) { Phase::Speaking } else { Phase::Listening };
                    if now_phase != phase {
                        phase = now_phase;
                        emit(Event::Phase { phase });
                    }
                }

                n = n.wrapping_add(1);
                if n % LEVEL_EVERY == 0 {
                    let zero = in_acc == 0.0 && out == 0.0;
                    if !(zero && last_levels_zero) {
                        emit(Event::Levels { input: in_acc, out });
                    }
                    last_levels_zero = zero;
                    in_acc = 0.0;
                    in_n = 0;
                    let frames = pipes.capture.lock().map(|mut r| r.take_dropped_frames()).unwrap_or(0);
                    if frames > 0 {
                        emit(Event::Dropped { frames });
                    }
                }
            }
        }
    };
    let _ = in_n;

    // Close the mic first, then the peer; both are idempotent on drop.
    drop(guard.take());
    if let Some(p) = peer.take() {
        p.close().await;
    }
    emit(Event::Closed { reason });
}

type Started = (Peer, AudioGuard, Encoder, String);

async fn start(
    backend: &mut Box<dyn AudioBackend>,
    pipes: &Pipes,
    ptx: UnboundedSender<PeerEvent>,
) -> Result<Started, (String, String)> {
    let enc = Encoder::new().map_err(|e| ("codec_error".to_string(), e))?;
    let guard = backend.open(pipes).map_err(|e| ("audio_error".to_string(), format!("audio: {e}")))?;
    let peer = Peer::new(ptx, pipes.clone()).await.map_err(|e| ("peer_error".to_string(), format!("peer: {e}")))?;
    let sdp = peer.create_offer().await.map_err(|e| {
        ("offer_error".to_string(), format!("offer: {e}"))
    })?;
    Ok((peer, guard, enc, sdp))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::FakeBackend;
    use tokio::sync::mpsc::unbounded_channel;

    fn spawn_run() -> (UnboundedSender<Input>, UnboundedReceiver<Event>, tokio::task::JoinHandle<()>) {
        let (itx, irx) = unbounded_channel();
        let (etx, erx) = unbounded_channel();
        let h = tokio::spawn(run(irx, etx, Box::new(FakeBackend), Pipes::new(), Options::default()));
        (itx, erx, h)
    }

    async fn drain(mut erx: UnboundedReceiver<Event>) -> Vec<Event> {
        let mut v = Vec::new();
        while let Some(e) = erx.recv().await {
            v.push(e);
        }
        v
    }

    #[tokio::test]
    async fn eof_closes_exactly_once() {
        let (itx, erx, h) = spawn_run();
        itx.send(Input::Eof).unwrap();
        h.await.unwrap();
        let evs = drain(erx).await;
        assert_eq!(evs, vec![Event::Closed { reason: "stdin_eof".into() }]);
    }

    #[tokio::test]
    async fn pause_is_accepted_and_does_not_close() {
        let (itx, erx, h) = spawn_run();
        itx.send(Input::Line(r#"{"type":"pause","on":true}"#.into())).unwrap();
        itx.send(Input::Line(r#"{"type":"pause","on":false}"#.into())).unwrap();
        itx.send(Input::Line(r#"{"type":"stop"}"#.into())).unwrap();
        h.await.unwrap();
        assert_eq!(drain(erx).await, vec![Event::Closed { reason: "stop".into() }]);
    }

    #[tokio::test]
    async fn dropped_input_channel_also_closes_once() {
        let (itx, erx, h) = spawn_run();
        drop(itx);
        h.await.unwrap();
        let closed = drain(erx).await.into_iter().filter(|e| matches!(e, Event::Closed { .. })).count();
        assert_eq!(closed, 1);
    }

    #[tokio::test]
    async fn stop_is_idempotent_and_closed_is_last() {
        let (itx, erx, h) = spawn_run();
        for _ in 0..3 {
            let _ = itx.send(Input::Line(r#"{"type":"stop"}"#.into()));
        }
        let _ = itx.send(Input::Eof);
        h.await.unwrap();
        let evs = drain(erx).await;
        assert_eq!(evs, vec![Event::Closed { reason: "stop".into() }]);
    }

    #[tokio::test]
    async fn bad_input_reports_errors_and_keeps_running() {
        let (itx, mut erx, h) = spawn_run();
        itx.send(Input::TooLong).unwrap();
        itx.send(Input::Line("nope".into())).unwrap();
        itx.send(Input::Line(r#"{"type":"answer","sdp":"x"}"#.into())).unwrap();
        itx.send(Input::Line("  ".into())).unwrap();
        for _ in 0..3 {
            assert!(matches!(erx.recv().await, Some(Event::Error { .. })));
        }
        itx.send(Input::Line(r#"{"type":"stop"}"#.into())).unwrap();
        assert_eq!(erx.recv().await, Some(Event::Closed { reason: "stop".into() }));
        h.await.unwrap();
        assert!(erx.recv().await.is_none());
    }
}
