//! Opus encode/decode for 20 ms, 48 kHz, mono frames. Uses `unsafe-libopus`
//! (libopus transpiled to Rust) so the build needs no C toolchain or CMake.

use crate::dsp::{FRAME, SAMPLE_RATE};
use unsafe_libopus::{
    opus_decode, opus_decoder_create, opus_decoder_destroy, opus_encode, opus_encoder_create,
    opus_encoder_destroy, OpusDecoder, OpusEncoder, OPUS_APPLICATION_VOIP,
};

pub struct Encoder(*mut OpusEncoder);
// The state is owned exclusively and only used through &mut self.
unsafe impl Send for Encoder {}

impl Encoder {
    pub fn new() -> Result<Self, String> {
        let mut err = 0i32;
        let p = unsafe { opus_encoder_create(SAMPLE_RATE as i32, 1, OPUS_APPLICATION_VOIP, &mut err) };
        if p.is_null() || err != 0 {
            return Err(format!("opus encoder init failed ({err})"));
        }
        Ok(Self(p))
    }

    /// Encode exactly one 20 ms frame.
    pub fn encode(&mut self, pcm: &[i16]) -> Result<Vec<u8>, String> {
        if pcm.len() != FRAME {
            return Err(format!("opus frame must be {FRAME} samples, got {}", pcm.len()));
        }
        let mut out = vec![0u8; 1275];
        let n = unsafe { opus_encode(self.0, pcm.as_ptr(), FRAME as i32, out.as_mut_ptr(), out.len() as i32) };
        if n < 0 {
            return Err(format!("opus encode failed ({n})"));
        }
        out.truncate(n as usize);
        Ok(out)
    }
}

impl Drop for Encoder {
    fn drop(&mut self) {
        unsafe { opus_encoder_destroy(self.0) }
    }
}

pub struct Decoder(*mut OpusDecoder);
unsafe impl Send for Decoder {}

impl Decoder {
    pub fn new() -> Result<Self, String> {
        let mut err = 0i32;
        let p = unsafe { opus_decoder_create(SAMPLE_RATE as i32, 1, &mut err) };
        if p.is_null() || err != 0 {
            return Err(format!("opus decoder init failed ({err})"));
        }
        Ok(Self(p))
    }

    /// Decode one packet to mono 48 kHz PCM (stereo packets are downmixed).
    pub fn decode(&mut self, packet: &[u8]) -> Result<Vec<i16>, String> {
        let mut pcm = vec![0i16; 5760];
        let n = unsafe { opus_decode(self.0, packet.as_ptr(), packet.len() as i32, pcm.as_mut_ptr(), pcm.len() as i32, 0) };
        if n < 0 {
            return Err(format!("opus decode failed ({n})"));
        }
        pcm.truncate(n as usize);
        Ok(pcm)
    }
}

impl Drop for Decoder {
    fn drop(&mut self) {
        unsafe { opus_decoder_destroy(self.0) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dsp::rms_i16;

    pub fn tone(freq: f32, amp: f32, frames: usize) -> Vec<i16> {
        (0..FRAME * frames)
            .map(|i| ((2.0 * std::f32::consts::PI * freq * i as f32 / 48_000.0).sin() * amp * 32767.0) as i16)
            .collect()
    }

    #[test]
    fn frame_round_trips_with_energy() {
        let mut e = Encoder::new().unwrap();
        let mut d = Decoder::new().unwrap();
        let pcm = tone(440.0, 0.5, 10);
        let mut last = Vec::new();
        for f in pcm.chunks(FRAME) {
            let pkt = e.encode(f).unwrap();
            assert!(!pkt.is_empty());
            last = d.decode(&pkt).unwrap();
            assert_eq!(last.len(), FRAME);
        }
        assert!(rms_i16(&last) > 0.2, "rms {}", rms_i16(&last));
        assert!(e.encode(&[0; 10]).is_err());
    }
}
