//! iOS microphone capture: AVAudioRecorder writes a 16 kHz mono PCM16 WAV,
//! and the composer sends those bytes to Core's speech-to-text endpoint.

use std::sync::Mutex;

use block2::RcBlock;
use nsh_core::prelude::*;
use objc2::rc::Retained;
use objc2::runtime::{AnyObject, Bool};
use objc2::AnyThread;
use objc2_avf_audio::{
    AVAudioRecorder, AVAudioSession, AVAudioSessionCategoryRecord, AVAudioSessionRecordPermission,
    AVFormatIDKey, AVLinearPCMBitDepthKey, AVLinearPCMIsBigEndianKey, AVLinearPCMIsFloatKey,
    AVNumberOfChannelsKey, AVSampleRateKey,
};
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
        if ACTIVE.lock().unwrap().is_some() {
            return Err("A dictation recording is already running".into());
        }

        let session = unsafe { AVAudioSession::sharedInstance() };
        ensure_permission(&session).await?;

        // SAFETY: the category is a framework-owned static.
        let category = unsafe { AVAudioSessionCategoryRecord }
            .ok_or("The record audio category is unavailable")?;
        // SAFETY: the session is the app's own; the category and active flag
        // are the documented way to open the microphone for recording.
        unsafe {
            session
                .setCategory_error(category)
                .map_err(|error| error.localizedDescription().to_string())?;
            session
                .setActive_error(true)
                .map_err(|error| error.localizedDescription().to_string())?;
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
        .map_err(|error| error.localizedDescription().to_string())?;

        // SAFETY: metering and recording are the recorder's documented API.
        let started = unsafe {
            recorder.setMeteringEnabled(true);
            recorder.record()
        };
        if !started {
            end_session();
            return Err("The microphone could not start recording".into());
        }

        *ACTIVE.lock().unwrap() = Some(Active {
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

        let data =
            NSData::dataWithContentsOfURL(&active.url).ok_or("The recording could not be read")?;
        let bytes = data.to_vec();
        let _ = std::fs::remove_file(&active.path);
        if bytes.len() <= 44 {
            return Err("The recording was empty".into());
        }
        Ok(bytes)
    }

    /// Stops recording and discards the file.
    pub async fn cancel() -> Result<(), String> {
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
#[allow(deprecated)]
async fn ensure_permission(session: &AVAudioSession) -> Result<(), String> {
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

/// The session is not needed outside a recording.
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
