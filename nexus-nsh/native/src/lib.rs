//! Native services the NSH client needs on a platform whose runtime has no
//! browser APIs: microphone capture for dictation.

use nsh_core::prelude::*;

#[cfg(target_os = "ios")]
mod ios;
#[cfg(target_os = "ios")]
pub use ios::Microphone;

/// Microphone capture for the composer's dictation flow.
#[cfg(not(target_os = "ios"))]
#[module]
pub struct Microphone;

#[cfg(not(target_os = "ios"))]
#[module]
impl Microphone {
    /// Whether this build can capture audio (iOS only).
    pub fn available() -> bool {
        false
    }

    /// Starts recording after asking for microphone permission.
    pub async fn start() -> Result<(), String> {
        Err("Microphone capture is not available in this build".into())
    }

    /// Stops recording and returns a 16 kHz mono PCM16 WAV.
    pub async fn stop() -> Result<Vec<u8>, String> {
        Err("Microphone capture is not available in this build".into())
    }

    /// Stops recording and discards the file.
    pub async fn cancel() -> Result<(), String> {
        Ok(())
    }

    /// The current input level, 0 to 1.
    pub fn level() -> f64 {
        0.0
    }
}

/// The crate's UI set: `Microphone` from `nexus/native`.
#[native_module("nexus/native")]
fn native(registry: &mut Registry) {
    registry.module::<Microphone>();
}
