//! Apple microphone capture: AVAudioRecorder writes a 16 kHz mono PCM16 WAV,
//! and the composer sends those bytes to Core's speech-to-text endpoint.

use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Mutex,
};

use block2::RcBlock;
use nsh_core::prelude::*;
use objc2::rc::Retained;
use objc2::runtime::{AnyObject, Bool};
use objc2::AnyThread;
#[cfg(target_os = "macos")]
use objc2::{class, msg_send};
use objc2_avf_audio::{
    AVAudioRecorder, AVFormatIDKey, AVLinearPCMBitDepthKey, AVLinearPCMIsBigEndianKey,
    AVLinearPCMIsFloatKey, AVNumberOfChannelsKey, AVSampleRateKey,
};
#[cfg(target_os = "ios")]
use objc2_avf_audio::{
    AVAudioSession, AVAudioSessionCategoryRecord, AVAudioSessionRecordPermission,
};
#[cfg(target_os = "macos")]
#[link(name = "AVFoundation", kind = "framework")]
extern "C" {}
use objc2_core_audio_types::kAudioFormatLinearPCM;
use objc2_foundation::{
    NSData, NSDictionary, NSNumber, NSString, NSTemporaryDirectory, NSURL, NSUUID,
};

const SAMPLE_RATE: f64 = 16_000.0;
const DENIED: &str =
    "Microphone permission was denied. Allow microphone access for Nexus in Settings and try again.";

struct Active {
    recorder: Retained<AVAudioRecorder>,
    url: Retained<NSURL>,
    path: String,
}

static ACTIVE: Mutex<Option<Active>> = Mutex::new(None);
static STARTING: AtomicBool = AtomicBool::new(false);
static CAPTURE_EPOCH: AtomicU64 = AtomicU64::new(0);
#[cfg(target_os = "ios")]
pub(crate) fn capture_busy() -> bool {
    STARTING.load(Ordering::Acquire) || ACTIVE.lock().map_or(true, |active| active.is_some())
}
struct StartGuard;
impl Drop for StartGuard {
    fn drop(&mut self) {
        STARTING.store(false, Ordering::Release);
    }
}

/// Microphone capture for the composer's dictation flow.
#[module]
pub struct Microphone;

#[module]
impl Microphone {
    /// Whether this build can capture audio.
    pub fn available() -> bool {
        true
    }

    /// Starts recording after asking for microphone permission.
    pub async fn start() -> Result<(), String> {
        #[cfg(target_os = "ios")]
        if crate::live_ios::is_busy() {
            return Err("Stop the Manager voice call before starting dictation".into());
        }
        if STARTING.swap(true, Ordering::AcqRel) {
            return Err("A dictation recording is already starting".into());
        }
        let _starting = StartGuard;
        let epoch = CAPTURE_EPOCH.load(Ordering::Acquire);
        if ACTIVE.lock().unwrap().is_some() {
            return Err("A dictation recording is already running".into());
        }

        #[cfg(target_os = "ios")]
        let session = unsafe { AVAudioSession::sharedInstance() };
        #[cfg(target_os = "ios")]
        ensure_permission(&session).await?;
        #[cfg(target_os = "macos")]
        ensure_permission().await?;

        // SAFETY: the category is a framework-owned static.
        #[cfg(target_os = "ios")]
        let category = unsafe { AVAudioSessionCategoryRecord }
            .ok_or("The record audio category is unavailable")?;
        // SAFETY: the session is the app's own; the category and active flag
        // are the documented way to open the microphone for recording.
        #[cfg(target_os = "ios")]
        unsafe {
            session
                .setCategory_error(category)
                .map_err(|error| error.localizedDescription().to_string())?;
            session
                .setActive_error(true)
                .map_err(|error| error.localizedDescription().to_string())?;
        }

        if CAPTURE_EPOCH.load(Ordering::Acquire) != epoch {
            end_session();
            return Err("Recording was cancelled".into());
        }

        let directory = NSTemporaryDirectory().to_string();
        let name = NSUUID::UUID().UUIDString().to_string();
        let path = format!("{directory}nexus-dictation-{name}.wav");
        let url = NSURL::fileURLWithPath(&NSString::from_str(&path));
        let settings = recording_settings();

        // SAFETY: settings name linear PCM at a supported rate; `url` is a
        // file URL in the app's temporary directory.
        let recorder = unsafe {
            AVAudioRecorder::initWithURL_settings_error(AVAudioRecorder::alloc(), &url, &settings)
        }
        .map_err(|error| {
            end_session();
            let _ = std::fs::remove_file(&path);
            error.localizedDescription().to_string()
        })?;

        // SAFETY: metering and recording are the recorder's documented API.
        let started = unsafe {
            recorder.setMeteringEnabled(true);
            recorder.recordForDuration(120.0)
        };
        if !started {
            let _ = std::fs::remove_file(&path);
            end_session();
            return Err("The microphone could not start recording".into());
        }

        let mut active = ACTIVE.lock().unwrap();
        if CAPTURE_EPOCH.load(Ordering::Acquire) != epoch {
            unsafe { recorder.stop() };
            let _ = std::fs::remove_file(&path);
            end_session();
            return Err("Recording was cancelled".into());
        }
        *active = Some(Active {
            recorder,
            url,
            path,
        });
        Ok(())
    }

    /// Stops recording and returns a 16 kHz mono PCM16 WAV.
    pub async fn stop() -> Result<Vec<u8>, String> {
        let active = ACTIVE.lock().unwrap().take();
        let Some(active) = active else {
            return Err("No dictation recording is running".into());
        };

        // SAFETY: stopping a recording recorder closes its file.
        unsafe { active.recorder.stop() };
        end_session();

        let data = NSData::dataWithContentsOfURL(&active.url);
        let _ = std::fs::remove_file(&active.path);
        let bytes = data.ok_or("The recording could not be read")?.to_vec();
        if bytes.len() <= 44 {
            return Err("The recording was empty".into());
        }
        Ok(bytes)
    }

    /// Stops recording and discards the file.
    pub async fn cancel() -> Result<(), String> {
        CAPTURE_EPOCH.fetch_add(1, Ordering::AcqRel);
        let active = ACTIVE.lock().unwrap().take();
        if let Some(active) = active {
            // SAFETY: stopping a recording recorder closes its file.
            unsafe { active.recorder.stop() };
            end_session();
            let _ = std::fs::remove_file(&active.path);
        }
        Ok(())
    }

    /// The current input level, 0 to 1.
    pub fn level() -> f64 {
        let active = ACTIVE.lock().unwrap();
        let Some(active) = active.as_ref() else {
            return 0.0;
        };
        // SAFETY: metering is enabled when recording starts.
        let decibels = unsafe {
            active.recorder.updateMeters();
            active.recorder.averagePowerForChannel(0)
        };
        (((decibels + 60.0) / 60.0).clamp(0.0, 1.0)) as f64
    }
}

/// Asks for the microphone once, resolving when the system answers.
#[cfg(target_os = "ios")]
#[allow(deprecated)]
pub(crate) async fn ensure_permission(session: &AVAudioSession) -> Result<(), String> {
    // SAFETY: reading the app's permission is the session's API.
    let status = unsafe { session.recordPermission() };
    if status == AVAudioSessionRecordPermission::Granted {
        return Ok(());
    }
    if status == AVAudioSessionRecordPermission::Denied {
        return Err(DENIED.into());
    }

    let (sender, receiver) = futures_channel::oneshot::channel::<bool>();
    let sender = Mutex::new(Some(sender));
    let block = RcBlock::new(move |granted: Bool| {
        if let Some(sender) = sender.lock().unwrap().take() {
            let _ = sender.send(granted.as_bool());
        }
    });
    // SAFETY: the system calls the completion once, on any queue.
    unsafe { session.requestRecordPermission(&block) };
    match receiver.await {
        Ok(true) => Ok(()),
        _ => Err(DENIED.into()),
    }
}

#[cfg(target_os = "macos")]
async fn ensure_permission() -> Result<(), String> {
    // AVMediaTypeAudio is the four-character media type "soun".
    let media = NSString::from_str("soun");
    let status: isize =
        unsafe { msg_send![class!(AVCaptureDevice), authorizationStatusForMediaType: &*media] };
    if status == 3 {
        return Ok(());
    }
    if status != 0 {
        return Err(DENIED.into());
    }
    let (sender, receiver) = futures_channel::oneshot::channel::<bool>();
    let sender = Mutex::new(Some(sender));
    let block = RcBlock::new(move |granted: Bool| {
        if let Some(sender) = sender.lock().unwrap().take() {
            let _ = sender.send(granted.as_bool());
        }
    });
    unsafe {
        let _: () = msg_send![class!(AVCaptureDevice), requestAccessForMediaType: &*media, completionHandler: &*block];
    }
    match receiver.await {
        Ok(true) => Ok(()),
        _ => Err(DENIED.into()),
    }
}

#[cfg(target_os = "macos")]
fn end_session() {}

/// The session is not needed outside a recording.
#[cfg(target_os = "ios")]
fn end_session() {
    // SAFETY: deactivating the app's own audio session is the documented
    // counterpart of `setActive:true`.
    let session = unsafe { AVAudioSession::sharedInstance() };
    // SAFETY: as above.
    let _ = unsafe { session.setActive_error(false) };
}

/// Linear PCM, 16 kHz, mono, 16-bit, so Core receives the WAV it transcribes.
fn recording_settings() -> Retained<NSDictionary<NSString, AnyObject>> {
    let format = NSNumber::numberWithUnsignedInt(kAudioFormatLinearPCM);
    let rate = NSNumber::numberWithDouble(SAMPLE_RATE);
    let channels = NSNumber::numberWithUnsignedInt(1);
    let depth = NSNumber::numberWithUnsignedInt(16);
    let float = NSNumber::numberWithBool(false);
    let big_endian = NSNumber::numberWithBool(false);

    // SAFETY: the settings keys are framework-owned statics.
    let keys: Vec<&NSString> = unsafe {
        [
            AVFormatIDKey,
            AVSampleRateKey,
            AVNumberOfChannelsKey,
            AVLinearPCMBitDepthKey,
            AVLinearPCMIsFloatKey,
            AVLinearPCMIsBigEndianKey,
        ]
    }
    .into_iter()
    .flatten()
    .collect();

    let values: Vec<Retained<AnyObject>> = vec![
        format.into(),
        rate.into(),
        channels.into(),
        depth.into(),
        float.into(),
        big_endian.into(),
    ];
    let values: Vec<&AnyObject> = values.iter().map(|value| &**value).collect();
    NSDictionary::from_slices(&keys, &values)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apple_capture_advertises_six_pcm_settings_without_opening_a_device() {
        assert!(Microphone::available());
        assert_eq!(recording_settings().len(), 6);
        assert_eq!(SAMPLE_RATE, 16_000.0);
    }
}
