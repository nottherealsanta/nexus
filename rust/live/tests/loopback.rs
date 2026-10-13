//! In-process loopback: the helper's `run` loop against a second webrtc-rs
//! peer acting as the OpenAI side. Uses the fake audio backend, so it needs no
//! devices. Proves: non-trickle offer, answer, `ready`, data channel open,
//! error-only forwarding, and an Opus frame round-tripping in both directions.

use bytes::Bytes;
use nexus_live::app::{run, Options};
use nexus_live::audio::{FakeBackend, Pipes};
use nexus_live::codec::{Decoder, Encoder};
use nexus_live::dsp::{rms_i16, FRAME};
use nexus_live::peer::{new_api, opus_capability, DATA_CHANNEL_LABEL};
use nexus_live::protocol::{Event, Input};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver};
use tokio::time::timeout;
use webrtc::api::media_engine::MIME_TYPE_OPUS;
use webrtc::media::Sample;
use webrtc::peer_connection::configuration::RTCConfiguration;
use webrtc::peer_connection::sdp::session_description::RTCSessionDescription;
use webrtc::track::track_local::track_local_static_sample::TrackLocalStaticSample;
use webrtc::track::track_local::TrackLocal;

fn tone(amp: f32) -> Vec<i16> {
    (0..FRAME)
        .map(|i| ((2.0 * std::f32::consts::PI * 440.0 * i as f32 / 48_000.0).sin() * amp * 32767.0) as i16)
        .collect()
}

async fn next_event(rx: &mut UnboundedReceiver<Event>) -> Event {
    timeout(Duration::from_secs(20), rx.recv()).await.expect("event timeout").expect("events closed")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn helper_and_test_peer_connect_and_exchange_opus() {
    let pipes = Pipes::new();
    let (itx, irx) = unbounded_channel();
    let (etx, mut erx) = unbounded_channel();
    let helper = tokio::spawn(run(irx, etx, Box::new(FakeBackend), pipes.clone(), Options::default()));

    // The fake "microphone": a steady tone, one frame per 20 ms.
    let mic = pipes.capture.clone();
    let mic_task = tokio::spawn(async move {
        loop {
            mic.lock().unwrap().push(&tone(0.5));
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    });

    itx.send(Input::Line(r#"{"type":"offer"}"#.into())).unwrap();
    let Event::Offer { sdp: offer_sdp } = next_event(&mut erx).await else { panic!("expected offer") };
    assert!(offer_sdp.contains("m=audio") && offer_sdp.contains("opus/48000"));
    assert!(offer_sdp.contains("m=application"), "data channel section");
    assert!(offer_sdp.contains("a=candidate"), "ICE fully gathered before the offer is emitted");

    // The OpenAI side.
    let pc = Arc::new(new_api().unwrap().new_peer_connection(RTCConfiguration::default()).await.unwrap());
    let (rms_tx, mut rms_rx) = unbounded_channel::<f32>();
    pc.on_track(Box::new(move |remote, _, _| {
        let rms_tx = rms_tx.clone();
        tokio::spawn(async move {
            let mut dec = Decoder::new().unwrap();
            while let Ok((pkt, _)) = remote.read_rtp().await {
                if let Ok(pcm) = dec.decode(&pkt.payload) {
                    let _ = rms_tx.send(rms_i16(&pcm));
                }
            }
        });
        Box::pin(async {})
    }));
    pc.on_data_channel(Box::new(move |dc| {
        assert_eq!(dc.label(), DATA_CHANNEL_LABEL);
        let dc2 = dc.clone();
        dc.on_open(Box::new(move || {
            Box::pin(async move {
                let _ = dc2.send_text(r#"{"type":"response.audio.delta","delta":"ignored"}"#.to_string()).await;
                let _ = dc2.send_text(r#"{"type":"error","error":{"message":"boom"}}"#.to_string()).await;
            })
        }));
        Box::pin(async {})
    }));
    let track = Arc::new(TrackLocalStaticSample::new(
        webrtc::rtp_transceiver::rtp_codec::RTCRtpCodecCapability { mime_type: MIME_TYPE_OPUS.into(), ..opus_capability() },
        "audio".into(),
        "test".into(),
    ));
    pc.add_track(track.clone() as Arc<dyn TrackLocal + Send + Sync>).await.unwrap();
    pc.set_remote_description(RTCSessionDescription::offer(offer_sdp).unwrap()).await.unwrap();
    let answer = pc.create_answer(None).await.unwrap();
    let mut gathered = pc.gathering_complete_promise().await;
    pc.set_local_description(answer).await.unwrap();
    let _ = gathered.recv().await;
    let answer_sdp = pc.local_description().await.unwrap().sdp;

    // Remote speech: a tone, so the helper's decode, level and gate paths run.
    let speech = tokio::spawn({
        let track = track.clone();
        async move {
            let mut enc = Encoder::new().unwrap();
            loop {
                let pkt = enc.encode(&tone(0.5)).unwrap();
                let _ = track
                    .write_sample(&Sample { data: Bytes::from(pkt), duration: Duration::from_millis(20), ..Default::default() })
                    .await;
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }
    });

    itx.send(Input::Line(serde_json::json!({"type":"answer","sdp":answer_sdp}).to_string())).unwrap();

    let (mut ready, mut dc_error, mut out_level, mut speaking) = (false, None, 0f32, false);
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    while !(ready && dc_error.is_some() && out_level > 0.05 && speaking) {
        assert!(
            tokio::time::Instant::now() < deadline,
            "timed out: ready={ready} dc_error={dc_error:?} out_level={out_level} speaking={speaking}"
        );
        match next_event(&mut erx).await {
            Event::Ready => ready = true,
            Event::DcError { message } => dc_error = Some(message),
            Event::Levels { out, .. } => out_level = out_level.max(out),
            Event::Phase { phase } => speaking |= phase == nexus_live::protocol::Phase::Speaking,
            Event::Error { message } => panic!("unexpected error: {message}"),
            Event::Closed { reason } => panic!("closed early: {reason}"),
            _ => {}
        }
    }
    assert_eq!(dc_error.as_deref(), Some("boom"), "only the error event is forwarded");

    // helper -> peer: Opus mic audio decodes with energy on the other side.
    let mut heard = 0f32;
    for _ in 0..200 {
        match timeout(Duration::from_secs(10), rms_rx.recv()).await {
            Ok(Some(r)) => heard = heard.max(r),
            _ => break,
        }
        if heard > 0.1 {
            break;
        }
    }
    assert!(heard > 0.1, "peer heard helper audio, rms {heard}");
    // peer -> helper: decoded audio reached the playback buffer.
    assert!(pipes.out_level.get() > 0.05 || !pipes.playback.lock().unwrap().is_empty());

    itx.send(Input::Line(r#"{"type":"stop"}"#.into())).unwrap();
    itx.send(Input::Line(r#"{"type":"stop"}"#.into())).unwrap();
    let mut closed = Vec::new();
    while let Ok(Some(e)) = timeout(Duration::from_secs(10), erx.recv()).await {
        if let Event::Closed { reason } = e {
            closed.push(reason);
        }
    }
    assert_eq!(closed, vec!["stop".to_string()]);
    helper.await.unwrap();
    mic_task.abort();
    speech.abort();
    let _ = pc.close().await;
}
