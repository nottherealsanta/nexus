//! The WebRTC peer: one sendrecv Opus audio track plus the `oai-events` data
//! channel. Only error events from the data channel are forwarded; everything
//! else on it is ignored (the daemon's sideband owns control and transcripts).

use crate::audio::Pipes;
use crate::codec::Decoder;
use crate::dsp::{rms_i16, FRAME};
use bytes::Bytes;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc::UnboundedSender;
use webrtc::api::interceptor_registry::register_default_interceptors;
use webrtc::api::media_engine::{MediaEngine, MIME_TYPE_OPUS};
use webrtc::api::{APIBuilder, API};
use webrtc::data_channel::data_channel_message::DataChannelMessage;
use webrtc::data_channel::RTCDataChannel;
use webrtc::interceptor::registry::Registry;
use webrtc::media::Sample;
use webrtc::ice_transport::ice_connection_state::RTCIceConnectionState;
use webrtc::peer_connection::configuration::RTCConfiguration;
use webrtc::peer_connection::peer_connection_state::RTCPeerConnectionState;
use webrtc::peer_connection::sdp::session_description::RTCSessionDescription;
use webrtc::peer_connection::RTCPeerConnection;
use webrtc::rtp_transceiver::rtp_codec::{RTCRtpCodecCapability, RTCRtpCodecParameters, RTPCodecType};
use webrtc::track::track_local::track_local_static_sample::TrackLocalStaticSample;
use webrtc::track::track_local::TrackLocal;
use webrtc::track::track_remote::TrackRemote;

pub const DATA_CHANNEL_LABEL: &str = "oai-events";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PeerEvent {
    Connected,
    DataChannelOpen,
    DcError(String),
    /// Failed or closed; the call is over.
    Closed(String),
}

pub fn opus_capability() -> RTCRtpCodecCapability {
    RTCRtpCodecCapability {
        mime_type: MIME_TYPE_OPUS.to_owned(),
        clock_rate: 48_000,
        channels: 2,
        sdp_fmtp_line: "minptime=10;useinbandfec=1".to_owned(),
        rtcp_feedback: vec![],
    }
}

/// An API with Opus registered (shared by the helper and the loopback tests).
pub fn new_api() -> Result<API, String> {
    let mut m = MediaEngine::default();
    m.register_codec(
        RTCRtpCodecParameters { capability: opus_capability(), payload_type: 111, ..Default::default() },
        RTPCodecType::Audio,
    )
    .map_err(|e| e.to_string())?;
    let registry = register_default_interceptors(Registry::new(), &mut m).map_err(|e| e.to_string())?;
    Ok(APIBuilder::new().with_media_engine(m).with_interceptor_registry(registry).build())
}

/// If `text` is a data channel event of type `error`, return its message.
pub fn dc_error_message(text: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(text).ok()?;
    if v.get("type")?.as_str()? != "error" {
        return None;
    }
    let msg = v
        .pointer("/error/message")
        .or_else(|| v.get("message"))
        .and_then(|m| m.as_str())
        .map(str::to_owned)
        .unwrap_or_else(|| v.to_string());
    Some(msg.chars().take(2000).collect())
}

/// Wire the standard handlers common to both ends of a data channel.
pub fn watch_data_channel(dc: &Arc<RTCDataChannel>, events: UnboundedSender<PeerEvent>) {
    let open_tx = events.clone();
    dc.on_open(Box::new(move || {
        let _ = open_tx.send(PeerEvent::DataChannelOpen);
        Box::pin(async {})
    }));
    dc.on_message(Box::new(move |msg: DataChannelMessage| {
        if let Ok(text) = std::str::from_utf8(&msg.data) {
            if let Some(m) = dc_error_message(text) {
                let _ = events.send(PeerEvent::DcError(m));
            }
        }
        Box::pin(async {})
    }));
}

pub struct Peer {
    pc: Arc<RTCPeerConnection>,
    track: Arc<TrackLocalStaticSample>,
    // Held so the channel is not dropped while the call is live.
    _dc: Arc<RTCDataChannel>,
}

impl Peer {
    pub async fn new(events: UnboundedSender<PeerEvent>, pipes: Pipes) -> Result<Self, String> {
        let api = new_api()?;
        let pc = Arc::new(api.new_peer_connection(RTCConfiguration::default()).await.map_err(|e| e.to_string())?);

        let track = Arc::new(TrackLocalStaticSample::new(opus_capability(), "audio".into(), "nexus-live".into()));
        pc.add_track(track.clone() as Arc<dyn TrackLocal + Send + Sync>).await.map_err(|e| e.to_string())?;

        let dc = pc.create_data_channel(DATA_CHANNEL_LABEL, None).await.map_err(|e| e.to_string())?;
        watch_data_channel(&dc, events.clone());

        let state_tx = events.clone();
        pc.on_peer_connection_state_change(Box::new(move |s: RTCPeerConnectionState| {
            eprintln!("nexus-live: peer connection state {s}");
            match s {
                RTCPeerConnectionState::Connected => {
                    let _ = state_tx.send(PeerEvent::Connected);
                }
                RTCPeerConnectionState::Failed => {
                    let _ = state_tx.send(PeerEvent::Closed("peer_failed".into()));
                }
                RTCPeerConnectionState::Closed => {
                    let _ = state_tx.send(PeerEvent::Closed("peer_closed".into()));
                }
                _ => {}
            }
            Box::pin(async {})
        }));

        // State names are useful when diagnosing negotiation failures; avoid logging
        // SDP, candidates, addresses, credentials, or other call-specific data.
        pc.on_ice_connection_state_change(Box::new(move |s: RTCIceConnectionState| {
            eprintln!("nexus-live: ICE connection state {s}");
            Box::pin(async {})
        }));

        pc.on_track(Box::new(move |remote: Arc<TrackRemote>, _, _| {
            let pipes = pipes.clone();
            tokio::spawn(play_remote(remote, pipes));
            Box::pin(async {})
        }));

        Ok(Self { pc, track, _dc: dc })
    }

    /// Non-trickle offer: ICE gathering finishes before the SDP is returned.
    pub async fn create_offer(&self) -> Result<String, String> {
        let offer = self.pc.create_offer(None).await.map_err(|e| e.to_string())?;
        let mut gathered = self.pc.gathering_complete_promise().await;
        self.pc.set_local_description(offer).await.map_err(|e| e.to_string())?;
        tokio::time::timeout(Duration::from_secs(10), gathered.recv())
            .await
            .map_err(|_| "ICE gathering timed out".to_string())?;
        self.pc.local_description().await.map(|d| d.sdp).ok_or_else(|| "no local description".to_string())
    }

    pub async fn apply_answer(&self, sdp: String) -> Result<(), String> {
        let desc = RTCSessionDescription::answer(sdp).map_err(|e| e.to_string())?;
        self.pc.set_remote_description(desc).await.map_err(|e| e.to_string())
    }

    pub async fn send_opus(&self, packet: Vec<u8>) -> Result<(), String> {
        self.track
            .write_sample(&Sample { data: Bytes::from(packet), duration: Duration::from_millis(20), ..Default::default() })
            .await
            .map_err(|e| e.to_string())
    }

    pub async fn close(&self) {
        let _ = tokio::time::timeout(Duration::from_secs(2), self.pc.close()).await;
    }
}

/// Decode the remote Opus track into the playback ring until it ends.
pub async fn play_remote(remote: Arc<TrackRemote>, pipes: Pipes) {
    let mut dec = match Decoder::new() {
        Ok(d) => d,
        Err(e) => {
            eprintln!("nexus-live: {e}");
            return;
        }
    };
    while let Ok((pkt, _)) = remote.read_rtp().await {
        if pkt.payload.is_empty() {
            continue;
        }
        match dec.decode(&pkt.payload) {
            Ok(pcm) => {
                let rms = rms_i16(&pcm);
                pipes.out_level.set(rms);
                if let Ok(mut p) = pipes.playback.lock() {
                    p.push_drop_oldest(&pcm);
                }
            }
            Err(e) => eprintln!("nexus-live: {e} (frame {FRAME})"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_only_error_events() {
        assert_eq!(
            dc_error_message(r#"{"type":"error","error":{"message":"bad"}}"#).as_deref(),
            Some("bad")
        );
        assert_eq!(dc_error_message(r#"{"type":"error","message":"flat"}"#).as_deref(), Some("flat"));
        assert!(dc_error_message(r#"{"type":"response.created"}"#).is_none());
        assert!(dc_error_message("garbage").is_none());
    }
}
