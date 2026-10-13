//! `nexus-live`: the audio/WebRTC helper behind Nexus GPT Live calls.
//!
//! No credentials and no network access beyond the WebRTC peer described by
//! the SDP it is given. See README.md for the stdio JSONL protocol.

pub mod app;
pub mod audio;
pub mod codec;
pub mod dsp;
pub mod peer;
pub mod protocol;
