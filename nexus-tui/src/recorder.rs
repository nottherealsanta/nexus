//! Standalone recorder capture controller (plan 54, milestone D-TUI-cap).
//!
//! This module is deliberately independent of dictation. It owns a long-running
//! 16 kHz mono PCM16 capture pipeline:
//!
//! ```text
//! capture source -> chunker (~1 s, integer sample offsets) -> bounded spool
//!                -> sequential upload -> transport commands
//! ```
//!
//! The controller is a deterministic state machine. It performs no I/O itself:
//! capture is behind [`CaptureSource`] and every upload, heartbeat and stop is
//! emitted as a [`RecorderCommand`] for a transport driver to execute. Tests use
//! a mock capture and feed [`RecorderEvent`]s back, so they need no microphone,
//! daemon or network and use an explicit monotonic `now`.
//!
//! Honesty rules implemented here:
//!
//! * Constructing the controller or calling [`RecorderController::start`] does
//!   not open the microphone. The device opens only after Core grants the lease
//!   (`Created`); a rejected start never touches it.
//! * A chunk is counted as saved only when Core acknowledges it. The spool holds
//!   at most [`RecorderConfig::spool_capacity_samples`] (30 s by default); when
//!   it is full, capture stops and the dropped range is recorded in
//!   [`RecorderSnapshot::losses`], never silently discarded.
//! * Callback starvation, a source gap, an xrun/route change, a queue overflow,
//!   a device failure and a lost lease stop capture. Known lost ranges carry an
//!   exact sample count; an xrun or starvation is reported with `samples: None`
//!   rather than an invented number. A block that arrives beyond a gap is
//!   reported as lost too, never spliced onto contiguous audio.
//! * A lost lease stops capture immediately and emits no further capture
//!   commands. Spooled audio stays visible as pending (recoverable later), not
//!   acknowledged.
//! * `stream_detached` stops capture but is explicitly rebindable with the
//!   still-valid lease; it is not treated as a permanent lease loss.
//! * Stop releases the microphone first. The capture thread drops the device,
//!   drains every queued callback block and sends an explicit `Finished`; only
//!   then does the controller flush the final chunk and drain the spool. There
//!   is no wall-clock settle guess.
//! * Both capture queues are bounded. The audio callback uses `try_send` and a
//!   full queue is reported as an explicit overflow cut, never unbounded growth.
//!   Overflow latches an invalid gate in both producers so later audio is
//!   dropped rather than queued after the loss. The raw callback queue reports
//!   unknown sample loss (interleaved device-rate values are not 16 kHz mono
//!   samples); only the post-resample event queue carries an exact count.
//! * Retries reuse the same sequence, start offset and content hash, so a
//!   duplicate upload is idempotent at Core.
//!
//! This controller is I/O-free. The TUI transport driver lives in
//! [`crate::recorder_driver`]; it executes these commands over Core's HTTP API
//! and persists chunks in [`crate::recorder_spool`]. Recovery uploads after
//! lease loss are planned by the spool and executed by the driver. UI/transcript
//! integration remains outside this module. [`RecorderController::microphone`]
//! is the production capture source.
//!
//! Known create/recovery boundary: Core returns the recovery capability only in
//! the create response. A process crash or cancellation between that response
//! and the durable spool commit can lose the capability; the end-to-end design
//! for closing that window is still under investigation.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use cpal::traits::StreamTrait;

use crate::audio_capture::{
    acquire_mic, open_input_bounded, smoothed_level, BoundedAudioMessage, FramePump, FrameSink,
    MicGuard, MicOwner, RECORDER_INPUT_QUEUE_BLOCKS, TARGET_RATE,
};

/// Microphone blocks delivered by [`CaptureSource`] are already mono 16 kHz.
pub const SAMPLE_RATE: u32 = TARGET_RATE;
/// Default chunk length. One second is 32 000 PCM16 bytes.
pub const CHUNK_SAMPLES: u64 = SAMPLE_RATE as u64;
/// Default bounded spool: 30 seconds of audio.
pub const SPOOL_SAMPLES: u64 = SAMPLE_RATE as u64 * 30;
/// Bounded queue between the recorder capture thread and the controller.
pub const RECORDER_EVENT_QUEUE_BLOCKS: usize = 64;

/// Capture lifecycle. Persistence and transcription have separate state in the
/// eventual UI; this controller only owns capture/upload.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureState {
    /// No recording has been started since construction.
    Idle,
    /// Core is creating the recording and granting the lease. The microphone is
    /// still closed.
    Starting,
    /// The lease is held and the microphone is open.
    Recording,
    /// The microphone was released; acknowledged audio is still draining.
    Stopping,
    /// Capture stopped normally and the final sample count was acknowledged.
    Stopped,
    /// Capture was cut short (gap, device failure, spool full, or lost lease).
    /// `stop_finished` says whether the committed prefix was sealed.
    Interrupted,
    /// A non-recoverable error stopped capture. Spooled audio is retained for
    /// inspection/recovery but is not acknowledged.
    Failed,
}

impl CaptureState {
    pub fn is_active(self) -> bool {
        matches!(self, Self::Starting | Self::Recording | Self::Stopping)
    }
}

/// A bounded, explicitly represented missing-audio range.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioLoss {
    /// Recording-relative first sample that is not represented in the upload.
    pub start: u64,
    /// Exact missing samples when known. `None` for callback starvation, where
    /// the controller knows audio was absent but not how much the device would
    /// have produced.
    pub samples: Option<u64>,
    pub kind: LossKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LossKind {
    /// The source delivered a block with a non-contiguous start offset.
    SourceGap,
    /// Audio delivered after an unbridgeable cut cannot be uploaded.
    DroppedAfterCut,
    /// The capture callback stopped delivering frames while the device was open.
    CallbackStarved,
    /// A cpal xrun/route change makes the exact sample timeline unknown.
    Discontinuity,
    /// A bounded capture queue overflowed before the controller saw the audio.
    CaptureQueueOverflow,
    /// The bounded pending spool could not accept more audio.
    SpoolFull,
    /// The capture source or device failed.
    DeviceFailed,
    /// The event stream detached; the lease may still be rebindable.
    StreamDetached,
    /// The lease was lost; audio that never reached the spool is unrecoverable.
    LeaseLost,
    /// Audio arrived after the stop tail was flushed.
    AfterStop,
    /// The stop acknowledged less audio than was declared.
    UploadFailed,
}

/// One mockable capture implementation for deterministic tests and the cpal
/// adapter used by the TUI.
pub trait CaptureSource: Send {
    /// Open the device. Called only after Core grants the lease.
    fn start(&mut self) -> Result<(), String>;
    /// Release the device promptly. Idempotent; safe to call while stopped.
    fn stop(&mut self);
    /// Take frames/events produced since the previous call. Never blocks.
    fn drain(&mut self) -> Vec<CaptureEvent>;
    /// Smoothed microphone RMS, 0..1. The default keeps test/other sources silent.
    fn level(&self) -> f32 {
        0.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaptureEvent {
    Audio(AudioBlock),
    Failed(String),
    /// The device is torn down and every queued callback block has been drained.
    /// This is the explicit end-of-capture signal; the controller must not use
    /// a wall-clock settle delay instead.
    Finished,
    /// Bounded capture queues dropped audio. `dropped_samples` and
    /// `dropped_from` are exact only for the post-resample event queue; the raw
    /// device-rate callback queue reports `None` for both, because its
    /// interleaved value count is not a 16 kHz mono sample count.
    Overflow {
        dropped_samples: Option<u64>,
        /// Recording-relative first sample the producer failed to queue.
        dropped_from: Option<u64>,
        source: OverflowSource,
    },
    /// A cpal xrun or route change: audio may be missing and the exact
    /// timeline is unknown.
    Discontinuity(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OverflowSource {
    /// The bounded audio-callback queue was full.
    AudioQueue,
    /// The bounded capture-event queue was full.
    EventQueue,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioBlock {
    /// Recording-relative sample offset of the first sample.
    pub start_sample: u64,
    pub samples: Vec<i16>,
}

#[derive(Debug, Clone)]
pub struct RecorderConfig {
    /// Samples per chunk. The last chunk of a recording may be shorter.
    pub chunk_samples: u64,
    /// Maximum samples retained in the pending spool, including the chunk
    /// currently uploaded.
    pub spool_capacity_samples: u64,
    /// Nominal heartbeat period (Core expires a lease after 20 s).
    pub heartbeat_interval: Duration,
    /// First retry delay; doubles per attempt up to `retry_max`.
    pub retry_base: Duration,
    pub retry_max: Duration,
    /// No audio for this long while the device is open means capture starved.
    pub starvation_timeout: Duration,
}

impl Default for RecorderConfig {
    fn default() -> Self {
        Self {
            chunk_samples: CHUNK_SAMPLES,
            spool_capacity_samples: SPOOL_SAMPLES,
            heartbeat_interval: Duration::from_secs(5),
            retry_base: Duration::from_millis(250),
            retry_max: Duration::from_secs(5),
            starvation_timeout: Duration::from_secs(2),
        }
    }
}

impl RecorderConfig {
    fn chunk_samples(&self) -> u64 {
        self.chunk_samples.max(1)
    }

    fn capacity(&self) -> u64 {
        self.spool_capacity_samples.max(self.chunk_samples().max(1))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StartRequest {
    pub stream_id: String,
    pub client_id: String,
    pub idempotency_key: String,
    pub title: Option<String>,
}

impl StartRequest {
    pub fn new(
        stream_id: impl Into<String>,
        client_id: impl Into<String>,
        idempotency_key: impl Into<String>,
    ) -> Self {
        Self {
            stream_id: stream_id.into(),
            client_id: client_id.into(),
            idempotency_key: idempotency_key.into(),
            title: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StartError {
    /// The controller already holds a starting/recording/stopping capture.
    AlreadyActive,
    /// A previous interrupted capture still has spooled audio. Do not silently
    /// discard it by starting another recording.
    UnrecoveredAudio {
        chunks: usize,
        samples: u64,
    },
    InvalidRequest(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopError {
    /// The lease was lost, so the recording cannot be sealed by this client.
    LeaseLost,
    /// The event stream detached; rebind first so the stop can be delivered.
    StreamDetached,
}

/// A recording state returned by Core. Only the fields the controller needs are
/// modelled; the transport driver maps the public API JSON into this shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordingState {
    pub id: String,
    pub generation: u64,
    /// Present on create while the lease is active.
    pub lease: Option<String>,
    pub recovery_token: Option<String>,
    pub durable_offset: u64,
    /// Durable chunk count, used to resume sequence numbering on an active
    /// idempotent create replay.
    pub chunks: u64,
    pub capture_state: String,
    pub final_sample_count: Option<u64>,
    /// Core's free space in seconds of recording, reported on create.
    pub free_seconds: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecorderTransportError {
    /// Network failure, timeout or `503 recorder_backpressure`: retry.
    Transient(String),
    /// Another client holds the single active capture. The microphone was never
    /// opened.
    Busy(String),
    /// `lease_expired`: capture cannot continue; only recovery remains.
    LeaseLost(String),
    /// `stream_detached`: stop the microphone, but the lease may still be
    /// rebound to a new event stream. This is not a permanent failure.
    StreamDetached(String),
    /// Lookup-only create: no recording exists for the persisted key.
    CreateNotFound,
    /// Lookup-only create found the key withdrawn by another create/resume.
    CreateWithdrawn,
    /// Core's bounded withdrawn-key guard is full. Pending create intent must be
    /// kept; a Core restart is required before creates can proceed.
    CreateGuardFull,
    /// Authentication/authorization failure. Retrying cannot help.
    Auth(String),
    /// Core already sealed this recording.
    Sealed {
        durable_offset: Option<u64>,
    },
    /// A conflicting duplicate or non-contiguous offset. Preserve audio and
    /// report the gap; do not pretend the chunk was accepted.
    Conflict {
        code: String,
        message: String,
        durable_offset: Option<u64>,
    },
    Permanent(String),
}

impl RecorderTransportError {
    pub fn is_transient(&self) -> bool {
        matches!(self, Self::Transient(_))
    }

    pub fn is_lease_lost(&self) -> bool {
        matches!(self, Self::LeaseLost(_))
    }

    pub fn message(&self) -> String {
        match self {
            Self::Transient(m)
            | Self::Busy(m)
            | Self::LeaseLost(m)
            | Self::StreamDetached(m)
            | Self::Auth(m)
            | Self::Permanent(m) => m.clone(),
            Self::Sealed { .. } => "the recording is already sealed".into(),
            Self::CreateNotFound => "no recording exists for this create key".into(),
            Self::CreateWithdrawn => "this create key was withdrawn by a resume lookup".into(),
            Self::CreateGuardFull => {
                "Core's recorder create guard is full; restart Core before starting another recording"
                    .into()
            }
            Self::Conflict { message, .. } => message.clone(),
        }
    }
}

/// One action for the transport driver. Commands are sequential: exactly one
/// chunk upload is outstanding at a time.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecorderCommand {
    Create {
        generation: u64,
        request: StartRequest,
    },
    PutChunk {
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
        sequence: u64,
        start: u64,
        bytes: Vec<u8>,
        sha256: String,
    },
    Heartbeat {
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
    },
    Stop {
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
        final_sample_count: u64,
    },
    /// Bind a detached capture to a new event stream using the still-valid
    /// lease. This only drains/seals audio captured before detach; it never
    /// resumes the microphone.
    Rebind {
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
        client_id: String,
    },
}

/// Transport results for the commands above. Events for an old generation are
/// ignored, so a late chunk acknowledgement can never attach to a new recording.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecorderEvent {
    Created {
        generation: u64,
        recording: RecordingState,
    },
    CreateFailed {
        generation: u64,
        error: RecorderTransportError,
    },
    ChunkAcked {
        generation: u64,
        sequence: u64,
        durable_offset: u64,
    },
    ChunkFailed {
        generation: u64,
        sequence: u64,
        error: RecorderTransportError,
    },
    HeartbeatOk {
        generation: u64,
    },
    HeartbeatFailed {
        generation: u64,
        error: RecorderTransportError,
    },
    Stopped {
        generation: u64,
        recording: RecordingState,
    },
    StopFailed {
        generation: u64,
        error: RecorderTransportError,
    },
    Rebound {
        generation: u64,
        stream_id: String,
    },
    RebindFailed {
        generation: u64,
        error: RecorderTransportError,
    },
}

/// What the UI/status layer can show. `pending_samples` are captured and
/// spooled but not yet acknowledged; `losses` are audio that is not in the
/// upload at all.
#[derive(Debug, Clone, PartialEq)]
pub struct RecorderSnapshot {
    pub generation: u64,
    pub state: CaptureState,
    pub recording_id: Option<String>,
    pub capture_open: bool,
    /// The capture source confirmed the device is torn down and queued audio
    /// was drained. The final tail may only be flushed after this is true.
    pub capture_finished: bool,
    /// The recording accepted audio up to the last cut. False after any
    /// discontinuity, overflow, lost lease or explicit stop teardown.
    pub accepting_audio: bool,
    pub uploads_enabled: bool,
    /// A detached stream can be rebound with the current lease.
    pub rebind_required: bool,
    pub stop_requested: bool,
    pub stop_finished: bool,
    /// Contiguous sample offset acknowledged by Core.
    pub durable_offset: u64,
    /// End of the audio admitted to chunks/spool (excludes losses).
    pub accepted_samples: u64,
    /// End of the audio delivered by the source, including dropped audio.
    pub received_samples: u64,
    pub pending_samples: u64,
    pub pending_chunks: usize,
    pub in_flight_chunk: Option<u64>,
    /// Smoothed microphone level, 0..1. Zero while capture is closed.
    pub level: f32,
    pub losses: Vec<AudioLoss>,
    /// Sum of losses whose size is known.
    pub lost_samples: u64,
    /// At least one loss has an unknown size (callback starvation).
    pub unknown_loss: bool,
    pub last_error: Option<String>,
}

/// A capture controller. Does not access the microphone on construction.
pub struct RecorderController {
    config: RecorderConfig,
    capture: Box<dyn CaptureSource>,
    state: CaptureState,
    generation: u64,
    request: Option<StartRequest>,
    recording_id: Option<String>,
    lease: Option<String>,
    recovery_token: Option<String>,
    stream_id: Option<String>,
    server_generation: u64,
    durable_offset: u64,
    accepted_through: u64,
    received_through: u64,
    next_sequence: u64,
    partial: Vec<i16>,
    spool: Spool,
    in_flight: Option<u64>,
    upload_retry_at: Duration,
    heartbeat_in_flight: bool,
    heartbeat_attempts: u32,
    next_heartbeat: Duration,
    stop_requested: bool,
    capture_finished: bool,
    tail_flushed: bool,
    stop_in_flight: bool,
    stop_retry_at: Duration,
    stop_finished: bool,
    uploads_enabled: bool,
    accepting_audio: bool,
    cut_loss_kind: LossKind,
    rebind_required: bool,
    rebind_in_flight: bool,
    interrupted: bool,
    failed: bool,
    abandoned: bool,
    capture_open: bool,
    last_audio_at: Option<Duration>,
    last_error: Option<String>,
    losses: Vec<AudioLoss>,
    lost_samples: u64,
    unknown_loss: bool,
    now: Duration,
    commands: VecDeque<RecorderCommand>,
}

impl RecorderController {
    pub fn new(config: RecorderConfig, capture: Box<dyn CaptureSource>) -> Self {
        Self {
            config,
            capture,
            state: CaptureState::Idle,
            generation: 0,
            request: None,
            recording_id: None,
            lease: None,
            recovery_token: None,
            stream_id: None,
            server_generation: 1,
            durable_offset: 0,
            accepted_through: 0,
            received_through: 0,
            next_sequence: 0,
            partial: Vec::new(),
            spool: Spool::default(),
            in_flight: None,
            upload_retry_at: Duration::ZERO,
            heartbeat_in_flight: false,
            heartbeat_attempts: 0,
            next_heartbeat: Duration::ZERO,
            stop_requested: false,
            capture_finished: false,
            tail_flushed: false,
            stop_in_flight: false,
            stop_retry_at: Duration::ZERO,
            stop_finished: false,
            uploads_enabled: false,
            accepting_audio: false,
            cut_loss_kind: LossKind::DroppedAfterCut,
            rebind_required: false,
            rebind_in_flight: false,
            interrupted: false,
            failed: false,
            abandoned: false,
            capture_open: false,
            last_audio_at: None,
            last_error: None,
            losses: Vec::new(),
            lost_samples: 0,
            unknown_loss: false,
            now: Duration::ZERO,
            commands: VecDeque::new(),
        }
    }

    /// Production controller over the cpal capture primitive. Construction is
    /// inert; the device opens only on [`CaptureSource::start`] after Core
    /// grants a lease.
    pub fn microphone(config: RecorderConfig) -> Self {
        Self::new(config, Box::new(MicrophoneCapture::default()))
    }

    pub fn state(&self) -> CaptureState {
        self.state
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn snapshot(&self) -> RecorderSnapshot {
        RecorderSnapshot {
            generation: self.generation,
            state: self.state,
            recording_id: self.recording_id.clone(),
            capture_open: self.capture_open,
            capture_finished: self.capture_finished,
            accepting_audio: self.accepting_audio,
            uploads_enabled: self.uploads_enabled,
            rebind_required: self.rebind_required,
            stop_requested: self.stop_requested,
            stop_finished: self.stop_finished,
            durable_offset: self.durable_offset,
            accepted_samples: self.accepted_through,
            received_samples: self.received_through,
            pending_samples: self.spool.samples.saturating_add(self.partial.len() as u64),
            pending_chunks: self.spool.chunks.len(),
            in_flight_chunk: self.in_flight,
            level: if self.capture_open {
                self.capture.level().clamp(0.0, 1.0)
            } else {
                0.0
            },
            losses: self.losses.clone(),
            lost_samples: self.lost_samples,
            unknown_loss: self.unknown_loss,
            last_error: self.last_error.clone(),
        }
    }

    /// Begin a recording. This only emits a `Create` command; the microphone
    /// opens when Core answers `Created` with a lease.
    pub fn start(&mut self, now: Duration, request: StartRequest) -> Result<u64, StartError> {
        if matches!(
            self.state,
            CaptureState::Starting | CaptureState::Recording | CaptureState::Stopping
        ) {
            return Err(StartError::AlreadyActive);
        }
        if !self.spool.chunks.is_empty() {
            return Err(StartError::UnrecoveredAudio {
                chunks: self.spool.chunks.len(),
                samples: self.spool.samples,
            });
        }
        if request.stream_id.trim().is_empty()
            || request.client_id.trim().is_empty()
            || request.idempotency_key.trim().is_empty()
        {
            return Err(StartError::InvalidRequest(
                "stream_id, client_id and idempotency_key are required".into(),
            ));
        }

        self.generation = self.generation.wrapping_add(1);
        self.state = CaptureState::Starting;
        self.request = Some(request.clone());
        self.recording_id = None;
        self.lease = None;
        self.recovery_token = None;
        self.stream_id = Some(request.stream_id.clone());
        self.server_generation = 1;
        self.durable_offset = 0;
        self.accepted_through = 0;
        self.received_through = 0;
        self.next_sequence = 0;
        self.partial.clear();
        self.spool.clear();
        self.in_flight = None;
        self.upload_retry_at = now;
        self.heartbeat_in_flight = false;
        self.heartbeat_attempts = 0;
        self.next_heartbeat = now + self.config.heartbeat_interval;
        self.stop_requested = false;
        self.capture_finished = false;
        self.tail_flushed = false;
        self.stop_in_flight = false;
        self.stop_retry_at = now;
        self.stop_finished = false;
        self.uploads_enabled = false;
        self.accepting_audio = false;
        self.cut_loss_kind = LossKind::DroppedAfterCut;
        self.rebind_required = false;
        self.rebind_in_flight = false;
        self.interrupted = false;
        self.failed = false;
        self.abandoned = false;
        self.capture_open = false;
        self.last_audio_at = None;
        self.last_error = None;
        self.losses.clear();
        self.lost_samples = 0;
        self.unknown_loss = false;
        self.now = now;
        self.commands.clear();

        self.emit(RecorderCommand::Create {
            generation: self.generation,
            request,
        });
        Ok(self.generation)
    }

    /// Request stop. Releases the microphone before draining, then flushes the
    /// short tail and waits for acknowledged uploads before declaring the final
    /// sample count. Repeated calls are idempotent.
    pub fn stop(&mut self, now: Duration) -> Result<(), StopError> {
        self.now = now;
        if self.abandoned {
            return Err(StopError::LeaseLost);
        }
        if self.rebind_required {
            return Err(StopError::StreamDetached);
        }
        match self.state {
            CaptureState::Idle | CaptureState::Stopped => Ok(()),
            CaptureState::Starting => {
                // Core may still answer Created. Do not open the microphone when
                // it does; publish the zero-length recording instead.
                self.stop_requested = true;
                self.tail_flushed = false;
                Ok(())
            }
            _ => {
                if self.capture_open {
                    self.capture.stop();
                    self.capture_open = false;
                }
                if !self.stop_requested {
                    self.stop_requested = true;
                    self.tail_flushed = false;
                }
                if !self.failed && !self.interrupted {
                    self.state = CaptureState::Stopping;
                }
                self.pump(now);
                Ok(())
            }
        }
    }

    /// Consume capture frames, advance timers and emit any due uploads,
    /// heartbeats or the final stop. Call regularly (e.g. each UI tick) with a
    /// monotonic clock. `now` is never used for sample positions.
    pub fn tick(&mut self, now: Duration) {
        self.pump(now);
    }

    pub fn poll_command(&mut self) -> Option<RecorderCommand> {
        self.commands.pop_front()
    }

    /// Feed a transport result back. Events from an older generation are
    /// ignored, which makes late callbacks harmless after stop/restart.
    pub fn handle(&mut self, now: Duration, event: RecorderEvent) {
        self.now = now;
        match event {
            RecorderEvent::Created {
                generation,
                recording,
            } => self.on_created(now, generation, recording),
            RecorderEvent::CreateFailed { generation, error } => {
                if generation == self.generation {
                    self.on_create_failed(error);
                }
            }
            RecorderEvent::ChunkAcked {
                generation,
                sequence,
                durable_offset,
            } => {
                if generation == self.generation {
                    self.on_chunk_acked(now, sequence, durable_offset);
                }
            }
            RecorderEvent::ChunkFailed {
                generation,
                sequence,
                error,
            } => {
                if generation == self.generation {
                    self.on_chunk_failed(now, sequence, error);
                }
            }
            RecorderEvent::HeartbeatOk { generation } => {
                if generation == self.generation {
                    self.heartbeat_in_flight = false;
                    self.heartbeat_attempts = 0;
                    self.next_heartbeat = now + self.config.heartbeat_interval;
                    if self
                        .last_error
                        .as_deref()
                        .is_some_and(|e| e.starts_with("heartbeat"))
                    {
                        self.last_error = None;
                    }
                }
            }
            RecorderEvent::HeartbeatFailed { generation, error } => {
                if generation == self.generation {
                    self.on_heartbeat_failed(now, error);
                }
            }
            RecorderEvent::Stopped {
                generation,
                recording,
            } => {
                if generation == self.generation {
                    self.on_stopped(recording);
                }
            }
            RecorderEvent::StopFailed { generation, error } => {
                if generation == self.generation {
                    self.on_stop_failed(now, error);
                }
            }
            RecorderEvent::Rebound {
                generation,
                stream_id,
            } => {
                if generation == self.generation {
                    self.on_rebound(now, stream_id);
                }
            }
            RecorderEvent::RebindFailed { generation, error } => {
                if generation == self.generation {
                    self.on_rebind_failed(now, error);
                }
            }
        }
        self.pump(now);
    }

    fn on_created(&mut self, now: Duration, generation: u64, recording: RecordingState) {
        if generation != self.generation || self.state != CaptureState::Starting {
            return;
        }
        self.recording_id = Some(recording.id.clone());
        self.server_generation = recording.generation;
        self.recovery_token = recording.recovery_token.clone();
        self.durable_offset = recording.durable_offset;
        self.accepted_through = recording.durable_offset;
        self.received_through = recording.durable_offset;
        self.next_sequence = recording.chunks;

        let Some(lease) = recording.lease.clone() else {
            // An idempotent replay whose lease already ended exposes no token.
            // Do not open the microphone; the caller must use recovery.
            self.state = CaptureState::Interrupted;
            self.interrupted = true;
            self.abandoned = true;
            self.uploads_enabled = false;
            self.accepting_audio = false;
            self.last_error =
                Some("the recording has no active lease; recovery is required".into());
            return;
        };
        self.lease = Some(lease);

        if self.stop_requested {
            // Stop arrived while Create was in flight: never touch the device.
            // There is no stream to tear down, so the no-device finish is what
            // lets the stop drain and seal.
            self.state = CaptureState::Stopping;
            self.capture_finished = true;
            self.tail_flushed = true;
            self.accepting_audio = false;
            self.stop_retry_at = now;
            return;
        }

        self.last_audio_at = Some(now);
        match self.capture.start() {
            Ok(()) => {
                self.capture_open = true;
                self.state = CaptureState::Recording;
                self.uploads_enabled = true;
                self.accepting_audio = true;
                self.next_heartbeat = now + self.config.heartbeat_interval;
                self.upload_retry_at = now;
            }
            Err(message) => {
                self.failed = true;
                self.state = CaptureState::Failed;
                self.uploads_enabled = false;
                self.accepting_audio = false;
                self.last_error = Some(format!("microphone unavailable: {message}"));
                self.stop_requested = true;
                // start() failed before a stream existed, so there is nothing
                // left that a later Finished could drain.
                self.capture_finished = true;
                self.tail_flushed = true;
                self.stop_retry_at = now;
            }
        }
    }

    fn on_create_failed(&mut self, error: RecorderTransportError) {
        self.state = CaptureState::Idle;
        self.uploads_enabled = false;
        self.accepting_audio = false;
        self.capture_open = false;
        self.stop_requested = false;
        self.last_error = Some(error.message());
    }

    fn on_chunk_acked(&mut self, now: Duration, sequence: u64, durable_offset: u64) {
        // After a lost lease the recording can only be recovered, never treated
        // as an acknowledged capture. A late success is safest ignored.
        if self.abandoned {
            return;
        }
        if self.in_flight == Some(sequence) {
            self.in_flight = None;
        }
        if let Some(front) = self.spool.front() {
            if front.sequence == sequence {
                self.spool.pop_front();
            }
        }
        // Reconnect/server truth wins. Chunks wholly below it are already saved.
        if durable_offset > self.durable_offset {
            self.durable_offset = durable_offset;
        }
        while self
            .spool
            .front()
            .is_some_and(|chunk| chunk.start + chunk.samples <= self.durable_offset)
        {
            self.spool.pop_front();
        }
        if self.durable_offset > self.accepted_through {
            self.accepted_through = self.durable_offset;
        }
        self.upload_retry_at = now;
        if self
            .last_error
            .as_deref()
            .is_some_and(|e| e.starts_with("upload"))
        {
            self.last_error = None;
        }
    }

    fn on_chunk_failed(&mut self, now: Duration, sequence: u64, error: RecorderTransportError) {
        if self.in_flight != Some(sequence) {
            return;
        }
        self.in_flight = None;
        match error {
            RecorderTransportError::Transient(message) => {
                let attempts = if let Some(front) = self.spool.front_mut() {
                    front.attempts = front.attempts.saturating_add(1);
                    front.attempts
                } else {
                    0
                };
                self.upload_retry_at = now + self.retry_delay(attempts);
                self.last_error = Some(format!("upload retry: {message}"));
            }
            RecorderTransportError::LeaseLost(message) => self.abandon(now, message),
            RecorderTransportError::StreamDetached(message) => self.detach(now, message),
            RecorderTransportError::Sealed { durable_offset } => {
                if let Some(offset) = durable_offset {
                    self.durable_offset = self.durable_offset.max(offset);
                }
                self.seal_without_draining();
            }
            other => self.fail(now, other.message()),
        }
    }

    fn on_heartbeat_failed(&mut self, now: Duration, error: RecorderTransportError) {
        self.heartbeat_in_flight = false;
        match error {
            RecorderTransportError::Transient(message) => {
                self.heartbeat_attempts = self.heartbeat_attempts.saturating_add(1);
                self.next_heartbeat = now + self.retry_delay(self.heartbeat_attempts);
                self.last_error = Some(format!("heartbeat retry: {message}"));
            }
            RecorderTransportError::LeaseLost(message) => self.abandon(now, message),
            RecorderTransportError::StreamDetached(message) => self.detach(now, message),
            other => self.fail(now, other.message()),
        }
    }

    fn on_stopped(&mut self, recording: RecordingState) {
        self.stop_in_flight = false;
        self.stop_finished = true;
        self.tail_flushed = true;
        self.capture_open = false;
        self.uploads_enabled = false;
        self.accepting_audio = false;
        self.rebind_required = false;
        self.rebind_in_flight = false;
        self.heartbeat_in_flight = false;
        if recording.durable_offset > self.durable_offset {
            self.durable_offset = recording.durable_offset;
        }
        if self.durable_offset > self.accepted_through {
            self.accepted_through = self.durable_offset;
        }
        if let Some(final_samples) = recording.final_sample_count {
            if final_samples > self.durable_offset {
                self.record_loss(
                    self.durable_offset,
                    Some(final_samples - self.durable_offset),
                    LossKind::UploadFailed,
                );
            }
        }
        self.state = if self.failed {
            CaptureState::Failed
        } else if self.interrupted || self.abandoned {
            CaptureState::Interrupted
        } else {
            CaptureState::Stopped
        };
    }

    fn on_stop_failed(&mut self, now: Duration, error: RecorderTransportError) {
        self.stop_in_flight = false;
        match error {
            RecorderTransportError::Transient(message) => {
                self.stop_retry_at = now + self.retry_delay(1);
                self.last_error = Some(format!("stop retry: {message}"));
            }
            RecorderTransportError::LeaseLost(message) => {
                self.abandon(now, message);
            }
            RecorderTransportError::StreamDetached(message) => self.detach(now, message),
            RecorderTransportError::Sealed { durable_offset } => {
                if let Some(offset) = durable_offset {
                    self.durable_offset = self.durable_offset.max(offset);
                }
                self.seal_without_draining();
            }
            other => {
                self.failed = true;
                self.state = CaptureState::Failed;
                self.stop_requested = false;
                self.uploads_enabled = false;
                self.accepting_audio = false;
                self.last_error = Some(other.message());
            }
        }
    }

    fn seal_without_draining(&mut self) {
        self.stop_finished = true;
        self.stop_requested = true;
        self.tail_flushed = true;
        self.uploads_enabled = false;
        self.accepting_audio = false;
        self.capture_open = false;
        self.state = if self.failed {
            CaptureState::Failed
        } else if self.interrupted || self.abandoned {
            CaptureState::Interrupted
        } else {
            CaptureState::Stopped
        };
    }

    fn pump(&mut self, now: Duration) {
        self.now = now;
        self.drain_capture(now);

        if self.state == CaptureState::Recording
            && self.capture_open
            && !self.stop_requested
            && !self.config.starvation_timeout.is_zero()
        {
            if let Some(last) = self.last_audio_at {
                if now.saturating_sub(last) >= self.config.starvation_timeout {
                    self.record_loss(self.received_through, None, LossKind::CallbackStarved);
                    self.cut_capture(
                        now,
                        LossKind::CallbackStarved,
                        "microphone stopped delivering audio",
                    );
                }
            }
        }

        // Never guess that teardown has finished from a wall-clock delay. The
        // capture source drains after dropping the device and then signals
        // Finished; only that lets the final tail be flushed.
        if self.stop_requested && !self.tail_flushed && !self.abandoned && self.capture_finished {
            if self.failed {
                self.discard_partial(LossKind::UploadFailed);
            } else {
                self.flush_partial();
            }
            self.tail_flushed = true;
        }

        self.maybe_heartbeat(now);
        self.maybe_upload(now);
        self.maybe_stop(now);
    }

    fn drain_capture(&mut self, now: Duration) {
        for event in self.capture.drain() {
            match event {
                CaptureEvent::Audio(block) => self.ingest_audio(now, block),
                CaptureEvent::Failed(message) => {
                    // A fatal callback error is not teardown. Stop the source
                    // and wait for its explicit Finished after the drain.
                    self.accepting_audio = false;
                    if !self.failed && !self.abandoned && !self.stop_finished {
                        self.record_loss(self.received_through, None, LossKind::DeviceFailed);
                        self.cut_capture(
                            now,
                            LossKind::DeviceFailed,
                            format!("microphone input failed: {message}"),
                        );
                    }
                }
                CaptureEvent::Finished => {
                    self.capture_finished = true;
                    self.accepting_audio = false;
                    self.cut_loss_kind = LossKind::AfterStop;
                    if !self.stop_requested
                        && !self.stop_finished
                        && !self.failed
                        && !self.abandoned
                        && !self.rebind_required
                    {
                        self.record_loss(self.received_through, None, LossKind::DeviceFailed);
                        self.cut_capture(
                            now,
                            LossKind::DeviceFailed,
                            "microphone input ended unexpectedly",
                        );
                    }
                }
                CaptureEvent::Overflow {
                    dropped_samples,
                    dropped_from,
                    source,
                } => {
                    let source_label = match source {
                        OverflowSource::AudioQueue => "the audio callback queue",
                        OverflowSource::EventQueue => "the capture event queue",
                    };
                    if !self.failed && !self.abandoned && !self.stop_finished {
                        // Only the event queue carries an exact post-resample
                        // offset. Use it when the safe queued prefix was ingested
                        // up to that point; otherwise report unknown rather than
                        // attributing the loss to the wrong range.
                        let exact = if source == OverflowSource::EventQueue {
                            match (dropped_from, dropped_samples) {
                                (Some(from), Some(samples)) if from == self.received_through => {
                                    Some((from, samples))
                                }
                                _ => None,
                            }
                        } else {
                            None
                        };
                        let (start, samples, detail) = match exact {
                            Some((from, samples)) => (
                                from,
                                Some(samples),
                                format!("{samples} samples starting at sample {from}"),
                            ),
                            None => (self.received_through, None, "an unknown range".to_owned()),
                        };
                        self.record_loss(start, samples, LossKind::CaptureQueueOverflow);
                        self.cut_capture(
                            now,
                            LossKind::CaptureQueueOverflow,
                            format!("{source_label} overflowed and dropped {detail}"),
                        );
                    }
                }
                CaptureEvent::Discontinuity(message) => {
                    if !self.failed && !self.abandoned && !self.stop_finished {
                        self.record_loss(self.received_through, None, LossKind::Discontinuity);
                        self.cut_capture(
                            now,
                            LossKind::Discontinuity,
                            format!("microphone discontinuity: {message}"),
                        );
                    }
                }
            }
        }
    }

    fn ingest_audio(&mut self, now: Duration, block: AudioBlock) {
        if block.samples.is_empty() {
            return;
        }
        if !matches!(
            self.state,
            CaptureState::Recording | CaptureState::Stopping | CaptureState::Interrupted
        ) {
            return;
        }
        if !self.accepting_audio || self.tail_flushed || self.abandoned {
            let end = block.start_sample + block.samples.len() as u64;
            self.received_through = self.received_through.max(end);
            let kind = if self.tail_flushed {
                LossKind::AfterStop
            } else {
                self.cut_loss_kind
            };
            self.record_loss(block.start_sample, Some(block.samples.len() as u64), kind);
            return;
        }

        let mut start = block.start_sample;
        let mut offset = 0usize;
        if start < self.received_through {
            // Late/overlapping callback: keep only the unseen suffix.
            let skip = (self.received_through - start) as usize;
            if skip >= block.samples.len() {
                return;
            }
            offset = skip;
            start = self.received_through;
        } else if start > self.received_through {
            let gap = start - self.received_through;
            self.record_loss(self.received_through, Some(gap), LossKind::SourceGap);
            self.received_through = start + block.samples.len() as u64;
            // The arriving block is beyond an unbridgeable gap. Core only
            // accepts contiguous uploads, so this block cannot be saved either;
            // report the whole loss, not just the missing gap.
            self.record_loss(
                start,
                Some(block.samples.len() as u64),
                LossKind::DroppedAfterCut,
            );
            self.cut_capture(
                now,
                LossKind::DroppedAfterCut,
                format!("audio source skipped {gap} samples"),
            );
            return;
        }

        let count = block.samples.len() - offset;
        self.received_through = start + count as u64;
        self.last_audio_at = Some(now);
        let chunk_samples = self.config.chunk_samples();
        let mut index = offset;
        while index < block.samples.len() {
            let space = chunk_samples.saturating_sub(self.partial.len() as u64) as usize;
            let take = space.min(block.samples.len() - index);
            self.partial
                .extend_from_slice(&block.samples[index..index + take]);
            index += take;
            if self.partial.len() as u64 == chunk_samples {
                let chunk_start = self.accepted_through;
                let samples = std::mem::take(&mut self.partial);
                if !self.enqueue_chunk(chunk_start, samples) {
                    return;
                }
            }
        }
    }

    fn enqueue_chunk(&mut self, start: u64, samples: Vec<i16>) -> bool {
        let sample_count = samples.len() as u64;
        let bytes = pcm16_bytes(&samples);
        let chunk = SpoolChunk {
            sequence: self.next_sequence,
            start,
            samples: sample_count,
            sha256: sha256_hex(&bytes),
            bytes,
            attempts: 0,
        };
        if self.spool.try_push(chunk, self.config.capacity()) {
            self.next_sequence = self.next_sequence.saturating_add(1);
            self.accepted_through = start + sample_count;
            true
        } else {
            let end = self.received_through.max(start + sample_count);
            self.record_loss(start, Some(end - start), LossKind::SpoolFull);
            self.cut_capture(
                self.now,
                LossKind::SpoolFull,
                "the pending recorder spool is full",
            );
            false
        }
    }

    fn flush_partial(&mut self) -> bool {
        if self.partial.is_empty() {
            return true;
        }
        let start = self.accepted_through;
        let samples = std::mem::take(&mut self.partial);
        self.enqueue_chunk(start, samples)
    }

    fn discard_partial(&mut self, kind: LossKind) {
        if self.partial.is_empty() {
            return;
        }
        let samples = self.partial.len() as u64;
        self.partial.clear();
        self.record_loss(self.accepted_through, Some(samples), kind);
    }

    fn maybe_upload(&mut self, now: Duration) {
        if !self.uploads_enabled
            || self.abandoned
            || self.stop_finished
            || self.in_flight.is_some()
            || now < self.upload_retry_at
            || !matches!(
                self.state,
                CaptureState::Recording
                    | CaptureState::Stopping
                    | CaptureState::Interrupted
                    | CaptureState::Failed
            )
        {
            return;
        }
        let Some((recording_id, lease, stream_id)) = self.command_context() else {
            return;
        };
        let Some(front) = self.spool.front() else {
            return;
        };
        let command = RecorderCommand::PutChunk {
            generation: self.generation,
            recording_id,
            lease,
            stream_id,
            sequence: front.sequence,
            start: front.start,
            bytes: front.bytes.clone(),
            sha256: front.sha256.clone(),
        };
        let sequence = front.sequence;
        self.in_flight = Some(sequence);
        self.emit(command);
    }

    fn maybe_heartbeat(&mut self, now: Duration) {
        if self.abandoned
            || self.stop_finished
            || self.rebind_required
            || self.heartbeat_in_flight
            || now < self.next_heartbeat
            || !matches!(
                self.state,
                CaptureState::Recording | CaptureState::Stopping | CaptureState::Interrupted
            )
        {
            return;
        }
        let Some((recording_id, lease, stream_id)) = self.command_context() else {
            return;
        };
        self.heartbeat_in_flight = true;
        self.emit(RecorderCommand::Heartbeat {
            generation: self.generation,
            recording_id,
            lease,
            stream_id,
        });
    }

    fn maybe_stop(&mut self, now: Duration) {
        if !self.stop_requested
            || self.stop_finished
            || self.stop_in_flight
            || self.abandoned
            || self.rebind_required
            || !self.tail_flushed
            || now < self.stop_retry_at
        {
            return;
        }
        let Some((recording_id, lease, stream_id)) = self.command_context() else {
            return;
        };
        let final_sample_count = if self.uploads_enabled {
            if !self.spool.is_empty() || self.in_flight.is_some() {
                return;
            }
            self.accepted_through
        } else {
            if self.in_flight.is_some() {
                return;
            }
            self.durable_offset
        }
        .max(self.durable_offset);
        self.stop_in_flight = true;
        self.emit(RecorderCommand::Stop {
            generation: self.generation,
            recording_id,
            lease,
            stream_id,
            final_sample_count,
        });
    }

    fn command_context(&self) -> Option<(String, String, String)> {
        Some((
            self.recording_id.clone()?,
            self.lease.clone()?,
            self.stream_id.clone()?,
        ))
    }

    /// Stop capture after an unbridgeable cut. Audio delivered after this point
    /// is reported as loss; it is never spliced across the discontinuity.
    fn cut_capture(&mut self, now: Duration, loss_kind: LossKind, message: impl Into<String>) {
        if self.failed || self.abandoned {
            return;
        }
        if self.capture_open {
            self.capture.stop();
            self.capture_open = false;
        }
        self.accepting_audio = false;
        self.cut_loss_kind = loss_kind;
        self.state = CaptureState::Interrupted;
        self.interrupted = true;
        self.uploads_enabled = true;
        self.last_error = Some(message.into());
        if !self.stop_requested {
            self.stop_requested = true;
            self.tail_flushed = false;
            self.stop_retry_at = now;
        }
        self.tick_after_change(now);
    }

    fn abandon(&mut self, now: Duration, message: impl Into<String>) {
        if self.capture_open {
            self.capture.stop();
            self.capture_open = false;
        }
        // Audio still in the partial buffer never reached the spool and cannot
        // be recovered. Spooled (but unacknowledged) audio stays pending and is
        // reported as such; it is not falsely marked saved.
        self.discard_partial(LossKind::LeaseLost);
        self.abandoned = true;
        self.interrupted = true;
        self.accepting_audio = false;
        self.cut_loss_kind = LossKind::LeaseLost;
        self.state = CaptureState::Interrupted;
        self.uploads_enabled = false;
        self.stop_requested = false;
        self.heartbeat_in_flight = false;
        self.in_flight = None;
        self.last_error = Some(message.into());
        self.tick_after_change(now);
    }

    fn fail(&mut self, now: Duration, message: impl Into<String>) {
        if self.capture_open {
            self.capture.stop();
            self.capture_open = false;
        }
        self.discard_partial(LossKind::UploadFailed);
        self.failed = true;
        self.state = CaptureState::Failed;
        self.uploads_enabled = false;
        self.accepting_audio = false;
        self.cut_loss_kind = LossKind::UploadFailed;
        self.last_error = Some(message.into());
        self.heartbeat_in_flight = false;
        if !self.stop_requested {
            self.stop_requested = true;
            self.tail_flushed = true;
            self.stop_retry_at = now;
        }
        self.tick_after_change(now);
    }

    /// `stream_detached` stops the device but leaves the lease valid. The
    /// recording is explicitly interrupted and exposes `rebind_required`; it is
    /// not abandoned or treated as a permanent loss.
    fn detach(&mut self, now: Duration, message: impl Into<String>) {
        if self.abandoned || self.stop_finished {
            return;
        }
        if self.capture_open {
            self.capture.stop();
            self.capture_open = false;
        }
        self.record_loss(self.received_through, None, LossKind::StreamDetached);
        self.accepting_audio = false;
        self.cut_loss_kind = LossKind::StreamDetached;
        self.state = CaptureState::Interrupted;
        self.interrupted = true;
        self.uploads_enabled = false;
        self.stop_requested = false;
        self.heartbeat_in_flight = false;
        self.in_flight = None;
        self.rebind_required = true;
        self.last_error = Some(message.into());
        self.tick_after_change(now);
    }

    pub fn rebind(&mut self, stream_id: impl Into<String>) -> Result<(), StopError> {
        if self.abandoned {
            return Err(StopError::LeaseLost);
        }
        if !self.rebind_required {
            return Err(StopError::StreamDetached);
        }
        if self.rebind_in_flight {
            return Ok(());
        }
        let stream_id = stream_id.into();
        let Some(recording_id) = self.recording_id.clone() else {
            return Err(StopError::StreamDetached);
        };
        let Some(lease) = self.lease.clone() else {
            return Err(StopError::LeaseLost);
        };
        let client_id = self
            .request
            .as_ref()
            .map(|request| request.client_id.clone())
            .unwrap_or_default();
        self.rebind_in_flight = true;
        self.emit(RecorderCommand::Rebind {
            generation: self.generation,
            recording_id,
            lease,
            stream_id,
            client_id,
        });
        Ok(())
    }

    fn on_rebound(&mut self, now: Duration, stream_id: String) {
        if !self.rebind_in_flight {
            return;
        }
        self.rebind_in_flight = false;
        self.rebind_required = false;
        self.stream_id = Some(stream_id);
        // Rebind only lets already-captured audio drain. Capture stays stopped,
        // so this cannot silently resume across the detached interval.
        self.uploads_enabled = true;
        self.state = CaptureState::Interrupted;
        self.interrupted = true;
        self.stop_requested = true;
        self.tail_flushed = false;
        self.stop_retry_at = now;
        self.tick_after_change(now);
    }

    fn on_rebind_failed(&mut self, now: Duration, error: RecorderTransportError) {
        self.rebind_in_flight = false;
        match error {
            RecorderTransportError::LeaseLost(message) => self.abandon(now, message),
            RecorderTransportError::Transient(message) => {
                self.last_error = Some(format!("rebind retry: {message}"));
            }
            RecorderTransportError::StreamDetached(message) => {
                self.last_error = Some(format!("rebind retry: {message}"));
            }
            other => self.fail(now, other.message()),
        }
    }

    fn tick_after_change(&mut self, now: Duration) {
        // Do not recursively drain capture here: callers are already inside an
        // event/pump. Just schedule what the state change makes possible.
        self.maybe_heartbeat(now);
        self.maybe_upload(now);
        self.maybe_stop(now);
    }

    fn retry_delay(&self, attempt: u32) -> Duration {
        let exponent = attempt.saturating_sub(1).min(5);
        let base = self.config.retry_base.as_millis().max(1) as u64;
        let cap = self.config.retry_max.as_millis().max(1) as u64;
        Duration::from_millis(base.saturating_mul(1u64 << exponent).min(cap))
    }

    fn record_loss(&mut self, start: u64, samples: Option<u64>, kind: LossKind) {
        if let Some(samples) = samples {
            self.lost_samples = self.lost_samples.saturating_add(samples);
        } else {
            self.unknown_loss = true;
        }
        if let Some(last) = self.losses.last_mut() {
            if last.kind == kind
                && last.start + last.samples.unwrap_or(0) == start
                && last.samples.is_some()
                && samples.is_some()
            {
                last.samples = Some(last.samples.unwrap_or(0) + samples.unwrap_or(0));
                return;
            }
        }
        self.losses.push(AudioLoss {
            start,
            samples,
            kind,
        });
    }

    fn emit(&mut self, command: RecorderCommand) {
        self.commands.push_back(command);
    }
}

#[derive(Default)]
struct Spool {
    chunks: VecDeque<SpoolChunk>,
    samples: u64,
}

impl Spool {
    fn try_push(&mut self, chunk: SpoolChunk, capacity: u64) -> bool {
        if self.samples.saturating_add(chunk.samples) > capacity {
            return false;
        }
        self.samples = self.samples.saturating_add(chunk.samples);
        self.chunks.push_back(chunk);
        true
    }

    fn pop_front(&mut self) -> Option<SpoolChunk> {
        let chunk = self.chunks.pop_front()?;
        self.samples = self.samples.saturating_sub(chunk.samples);
        Some(chunk)
    }

    fn front(&self) -> Option<&SpoolChunk> {
        self.chunks.front()
    }

    fn front_mut(&mut self) -> Option<&mut SpoolChunk> {
        self.chunks.front_mut()
    }

    fn is_empty(&self) -> bool {
        self.chunks.is_empty()
    }

    fn clear(&mut self) {
        self.chunks.clear();
        self.samples = 0;
    }
}

#[derive(Debug)]
struct SpoolChunk {
    sequence: u64,
    start: u64,
    samples: u64,
    sha256: String,
    bytes: Vec<u8>,
    attempts: u32,
}

fn pcm16_bytes(samples: &[i16]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    bytes
}

// ---------------------------------------------------------------------------
// Production capture source
// ---------------------------------------------------------------------------

/// cpal capture on a dedicated thread. The device is opened by `start`, never
/// by construction, and stops by dropping the stream on that thread.
pub struct MicrophoneCapture {
    events: std_mpsc::Receiver<CaptureEvent>,
    events_tx: std_mpsc::SyncSender<CaptureEvent>,
    control: Option<std_mpsc::Sender<()>>,
    thread: Option<JoinHandle<()>>,
    open: bool,
    audio_overflow_blocks: Option<Arc<AtomicU64>>,
    event_overflow_samples: Option<Arc<AtomicU64>>,
    event_overflow_start: Option<Arc<AtomicU64>>,
    failure: Option<Arc<OnceLock<String>>>,
    failure_reported: bool,
    discontinuities: Option<Arc<AtomicU64>>,
    level: Arc<AtomicU32>,
}

impl Default for MicrophoneCapture {
    fn default() -> Self {
        let (events_tx, events) = std_mpsc::sync_channel(RECORDER_EVENT_QUEUE_BLOCKS);
        Self {
            events,
            events_tx,
            control: None,
            thread: None,
            open: false,
            audio_overflow_blocks: None,
            event_overflow_samples: None,
            event_overflow_start: None,
            failure: None,
            failure_reported: false,
            discontinuities: None,
            level: Arc::new(AtomicU32::new(0)),
        }
    }
}

impl MicrophoneCapture {
    fn retire_thread(&mut self) {
        if let Some(thread) = self.thread.take() {
            let _ = thread::Builder::new()
                .name("nexus-recorder-reaper".into())
                .spawn(move || {
                    let _ = thread.join();
                });
        }
    }
}

impl CaptureSource for MicrophoneCapture {
    fn start(&mut self) -> Result<(), String> {
        if self.open {
            return Ok(());
        }
        self.retire_thread();
        // Acquire before spawning so a competing dictation/Manager capture
        // fails without ever touching the device.
        let guard = match acquire_mic(MicOwner::Recorder) {
            Ok(guard) => guard,
            Err(message) => return Err(message),
        };
        self.level.store(0f32.to_bits(), Ordering::Relaxed);
        // A fresh channel per session means a late Finished/Failed from the
        // previous thread can never be mistaken for this session's signal.
        let (events_tx, events) = std_mpsc::sync_channel(RECORDER_EVENT_QUEUE_BLOCKS);
        self.events = events;
        self.events_tx = events_tx.clone();

        let audio_overflow_blocks = Arc::new(AtomicU64::new(0));
        let event_overflow_samples = Arc::new(AtomicU64::new(0));
        let event_overflow_start = Arc::new(AtomicU64::new(u64::MAX));
        let failure = Arc::new(OnceLock::new());
        let discontinuities = Arc::new(AtomicU64::new(0));
        let invalid = Arc::new(AtomicBool::new(false));
        let (status_tx, status_rx) = std_mpsc::channel();
        let (control_tx, control_rx) = std_mpsc::channel();
        let events = events_tx;
        let thread_audio_overflow = audio_overflow_blocks.clone();
        let thread_event_overflow = event_overflow_samples.clone();
        let thread_event_overflow_start = event_overflow_start.clone();
        let thread_failure = failure.clone();
        let thread_discontinuities = discontinuities.clone();
        let thread_invalid = invalid.clone();
        let thread_level = self.level.clone();
        let thread = thread::Builder::new()
            .name("nexus-recorder-capture".into())
            .spawn(move || {
                capture_thread(
                    control_rx,
                    events,
                    status_tx,
                    thread_audio_overflow,
                    thread_event_overflow,
                    thread_event_overflow_start,
                    thread_failure,
                    thread_discontinuities,
                    thread_invalid,
                    thread_level,
                    guard,
                )
            })
            .map_err(|error| format!("could not start the recorder capture thread: {error}"))?;
        self.audio_overflow_blocks = Some(audio_overflow_blocks);
        self.event_overflow_samples = Some(event_overflow_samples);
        self.event_overflow_start = Some(event_overflow_start);
        self.failure = Some(failure);
        self.failure_reported = false;
        self.discontinuities = Some(discontinuities);

        match status_rx.recv_timeout(Duration::from_secs(5)) {
            Ok(Ok(())) => {
                self.control = Some(control_tx);
                self.thread = Some(thread);
                self.open = true;
                Ok(())
            }
            Ok(Err(message)) => {
                let _ = control_tx.send(());
                self.thread = Some(thread);
                Err(message)
            }
            Err(_) => {
                let _ = control_tx.send(());
                self.thread = Some(thread);
                Err("timed out opening the microphone".into())
            }
        }
    }

    fn stop(&mut self) {
        if !self.open {
            return;
        }
        if let Some(control) = self.control.take() {
            let _ = control.send(());
        }
        self.open = false;
        self.level.store(0f32.to_bits(), Ordering::Relaxed);
        self.retire_thread();
    }

    fn level(&self) -> f32 {
        f32::from_bits(self.level.load(Ordering::Relaxed))
    }

    fn drain(&mut self) -> Vec<CaptureEvent> {
        // Collect first, then read every latch. An overflow can happen while
        // audio is already queued. A known event-queue overflow keeps its safe
        // queued prefix ahead of the cut; unknown/fatal cases cut first.
        let queued: Vec<CaptureEvent> = self.events.try_iter().collect();

        let mut cut_first = Vec::new();
        if let Some(failure) = self.failure.as_ref() {
            if !self.failure_reported {
                if let Some(message) = failure.get().cloned() {
                    self.failure_reported = true;
                    cut_first.push(CaptureEvent::Failed(message));
                }
            }
        }
        let audio_blocks = self
            .audio_overflow_blocks
            .as_ref()
            .map_or(0, |counter| counter.swap(0, Ordering::Relaxed));
        if audio_blocks > 0 {
            cut_first.push(CaptureEvent::Overflow {
                // Raw callback blocks are interleaved device-rate values, not
                // 16 kHz mono samples: neither count nor position is known.
                dropped_samples: None,
                dropped_from: None,
                source: OverflowSource::AudioQueue,
            });
        }
        let discontinuities = self
            .discontinuities
            .as_ref()
            .map_or(0, |counter| counter.swap(0, Ordering::Relaxed));
        if discontinuities > 0 {
            cut_first.push(CaptureEvent::Discontinuity(format!(
                "xrun/device change reported by cpal ({discontinuities} time(s))"
            )));
        }
        let event_samples = self
            .event_overflow_samples
            .as_ref()
            .map_or(0, |counter| counter.swap(0, Ordering::Relaxed));
        let event_start = self
            .event_overflow_start
            .as_ref()
            .map_or(u64::MAX, |start| start.swap(u64::MAX, Ordering::Relaxed));
        let event_start = (event_start != u64::MAX).then_some(event_start);

        if !cut_first.is_empty() {
            // Audio-queue/failure/discontinuity has an unknown or invalidating
            // boundary: cut before any queued audio. The exact event-queue
            // count, if any, is still reported, but not used to attribute a
            // range to the wrong samples.
            if event_samples > 0 {
                cut_first.push(CaptureEvent::Overflow {
                    dropped_samples: Some(event_samples),
                    dropped_from: event_start,
                    source: OverflowSource::EventQueue,
                });
            }
            cut_first.extend(queued);
            return cut_first;
        }

        if event_samples == 0 {
            return queued;
        }
        let Some(first_lost) = event_start else {
            let mut out = vec![CaptureEvent::Overflow {
                dropped_samples: Some(event_samples),
                dropped_from: None,
                source: OverflowSource::EventQueue,
            }];
            out.extend(queued);
            return out;
        };

        // The queue is FIFO and BlockSink latches the first failed start. Every
        // queued audio block before that point is a safe contiguous prefix.
        let mut out = Vec::with_capacity(queued.len() + 1);
        let mut after = Vec::new();
        for event in queued {
            match &event {
                CaptureEvent::Audio(block) if block.start_sample < first_lost => out.push(event),
                _ => after.push(event),
            }
        }
        out.push(CaptureEvent::Overflow {
            dropped_samples: Some(event_samples),
            dropped_from: Some(first_lost),
            source: OverflowSource::EventQueue,
        });
        out.extend(after);
        out
    }
}

impl Drop for MicrophoneCapture {
    fn drop(&mut self) {
        if let Some(control) = self.control.take() {
            let _ = control.send(());
        }
        // The capture thread owns the stream and exits on the control signal;
        // do not block the dropping thread on device teardown.
        self.retire_thread();
    }
}

fn capture_thread(
    control: std_mpsc::Receiver<()>,
    events: std_mpsc::SyncSender<CaptureEvent>,
    status: std_mpsc::Sender<Result<(), String>>,
    audio_overflow_blocks: Arc<AtomicU64>,
    event_overflow_samples: Arc<AtomicU64>,
    event_overflow_start: Arc<AtomicU64>,
    failure: Arc<OnceLock<String>>,
    discontinuities: Arc<AtomicU64>,
    invalid: Arc<AtomicBool>,
    level: Arc<AtomicU32>,
    _guard: MicGuard,
) {
    let (audio_tx, audio_rx) =
        std_mpsc::sync_channel::<BoundedAudioMessage>(RECORDER_INPUT_QUEUE_BLOCKS);
    let (stream, rate, channels) = match open_input_bounded(
        audio_tx,
        audio_overflow_blocks,
        failure,
        discontinuities,
        invalid.clone(),
    ) {
        Ok(opened) => opened,
        Err(message) => {
            let _ = status.send(Err(message));
            return;
        }
    };
    if let Err(error) = stream.play() {
        let _ = status.send(Err(format!(
            "could not start the microphone input stream: {error}"
        )));
        return;
    }
    let Some(mut pump) = FramePump::new(
        rate,
        channels,
        BlockSink {
            events: events.clone(),
            next_sample: 0,
            overflow: event_overflow_samples,
            first_lost: event_overflow_start,
            invalid: invalid.clone(),
            level,
        },
    ) else {
        let _ = status.send(Err("the microphone reported an invalid format".into()));
        return;
    };
    if status.send(Ok(())).is_err() {
        return;
    }

    loop {
        match control.try_recv() {
            Ok(()) | Err(std_mpsc::TryRecvError::Disconnected) => {
                // Stop: tear the device down first, then drain every callback
                // block that was queued before teardown completed.
                drop(stream);
                drain_after_teardown(&audio_rx, &mut pump);
                let _ = events.send(CaptureEvent::Finished);
                return;
            }
            Err(std_mpsc::TryRecvError::Empty) => {}
        }
        match audio_rx.recv_timeout(Duration::from_millis(30)) {
            Ok(BoundedAudioMessage::Samples(samples)) => pump.push_block(&samples),
            Err(std_mpsc::RecvTimeoutError::Disconnected) => {
                drop(stream);
                drain_after_teardown(&audio_rx, &mut pump);
                // Finished is the sole teardown signal; an unexpected end is
                // handled by the controller as a device failure.
                let _ = events.send(CaptureEvent::Finished);
                return;
            }
            Err(std_mpsc::RecvTimeoutError::Timeout) => {}
        }
    }
}

/// After the device is dropped, every callback block already in the bounded
/// input queue is still part of the recording. Pump them before signalling
/// `Finished`; this is why Stop must not break out of the capture loop at the
/// control priority.
fn drain_after_teardown(
    audio: &std_mpsc::Receiver<BoundedAudioMessage>,
    pump: &mut FramePump<BlockSink>,
) {
    while let Ok(BoundedAudioMessage::Samples(samples)) = audio.try_recv() {
        pump.push_block(&samples);
    }
}

struct BlockSink {
    events: std_mpsc::SyncSender<CaptureEvent>,
    next_sample: u64,
    overflow: Arc<AtomicU64>,
    /// First mono 16 kHz sample that could not be queued, or `u64::MAX`.
    first_lost: Arc<AtomicU64>,
    invalid: Arc<AtomicBool>,
    level: Arc<AtomicU32>,
}

impl FrameSink for BlockSink {
    fn push_frame(&mut self, frames: &[f32]) {
        if frames.is_empty() {
            return;
        }
        let samples: Vec<i16> = frames
            .iter()
            .map(|sample| {
                let sample = sample.clamp(-1.0, 1.0);
                if sample <= -1.0 {
                    i16::MIN
                } else {
                    (sample * f32::from(i16::MAX)).round() as i16
                }
            })
            .collect();
        let rms =
            (frames.iter().map(|sample| sample * sample).sum::<f32>() / frames.len() as f32).sqrt();
        let previous = f32::from_bits(self.level.load(Ordering::Relaxed));
        self.level
            .store(smoothed_level(previous, rms).to_bits(), Ordering::Relaxed);
        let count = samples.len() as u64;
        let start_sample = self.next_sample;
        self.next_sample = self.next_sample.saturating_add(count);
        if self.invalid.load(Ordering::Relaxed) {
            self.first_lost.fetch_min(start_sample, Ordering::Relaxed);
            self.overflow.fetch_add(count, Ordering::Relaxed);
            return;
        }
        match self.events.try_send(CaptureEvent::Audio(AudioBlock {
            start_sample,
            samples,
        })) {
            Ok(()) | Err(std_mpsc::TrySendError::Disconnected(_)) => {}
            Err(std_mpsc::TrySendError::Full(event)) => {
                self.invalid.store(true, Ordering::Relaxed);
                self.first_lost.fetch_min(start_sample, Ordering::Relaxed);
                let dropped = match event {
                    CaptureEvent::Audio(block) => block.samples.len() as u64,
                    _ => count,
                };
                self.overflow.fetch_add(dropped, Ordering::Relaxed);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// SHA-256 (chunk content hash)
// ---------------------------------------------------------------------------

const SHA256_K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/// Lowercase hex SHA-256. The chunk API requires the exact digest of the PCM
/// body (`X-Content-SHA256`); using the same bytes on retry keeps uploads
/// idempotent.
fn sha256_hex(data: &[u8]) -> String {
    let mut state: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let bit_len = (data.len() as u64).saturating_mul(8);
    let mut message = Vec::with_capacity(data.len() + 72);
    message.extend_from_slice(data);
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_len.to_be_bytes());

    for block in message.chunks_exact(64) {
        let mut w = [0u32; 64];
        for (index, word) in block.chunks_exact(4).enumerate() {
            w[index] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for index in 16..64 {
            let s0 = w[index - 15].rotate_right(7)
                ^ w[index - 15].rotate_right(18)
                ^ (w[index - 15] >> 3);
            let s1 = w[index - 2].rotate_right(17)
                ^ w[index - 2].rotate_right(19)
                ^ (w[index - 2] >> 10);
            w[index] = w[index - 16]
                .wrapping_add(s0)
                .wrapping_add(w[index - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = state;
        for index in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = h
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(SHA256_K[index])
                .wrapping_add(w[index]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        state[0] = state[0].wrapping_add(a);
        state[1] = state[1].wrapping_add(b);
        state[2] = state[2].wrapping_add(c);
        state[3] = state[3].wrapping_add(d);
        state[4] = state[4].wrapping_add(e);
        state[5] = state[5].wrapping_add(f);
        state[6] = state[6].wrapping_add(g);
        state[7] = state[7].wrapping_add(h);
    }

    let mut out = String::with_capacity(64);
    for word in state {
        out.push_str(&format!("{word:08x}"));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier, Mutex};

    #[derive(Default)]
    struct MockState {
        starts: usize,
        stops: usize,
        events: VecDeque<CaptureEvent>,
    }

    #[derive(Clone, Default)]
    struct MockCapture(Arc<Mutex<MockState>>);

    impl MockCapture {
        fn emit_audio(&self, start_sample: u64, samples: Vec<i16>) {
            self.0
                .lock()
                .unwrap()
                .events
                .push_back(CaptureEvent::Audio(AudioBlock {
                    start_sample,
                    samples,
                }));
        }

        fn emit(&self, event: CaptureEvent) {
            self.0.lock().unwrap().events.push_back(event);
        }

        fn starts(&self) -> usize {
            self.0.lock().unwrap().starts
        }

        fn stops(&self) -> usize {
            self.0.lock().unwrap().stops
        }
    }

    impl CaptureSource for MockCapture {
        fn start(&mut self) -> Result<(), String> {
            self.0.lock().unwrap().starts += 1;
            Ok(())
        }

        fn stop(&mut self) {
            let mut state = self.0.lock().unwrap();
            state.stops += 1;
            // The production thread drains queued callback audio after device
            // teardown, then sends Finished. The mock appends it after any
            // already queued audio so stop tests exercise that order.
            state.events.push_back(CaptureEvent::Finished);
        }

        fn drain(&mut self) -> Vec<CaptureEvent> {
            self.0.lock().unwrap().events.drain(..).collect()
        }
    }

    fn config() -> RecorderConfig {
        RecorderConfig {
            starvation_timeout: Duration::from_secs(10),
            ..RecorderConfig::default()
        }
    }

    fn controller() -> (RecorderController, MockCapture) {
        let mock = MockCapture::default();
        (
            RecorderController::new(config(), Box::new(mock.clone())),
            mock,
        )
    }

    fn start(controller: &mut RecorderController, now: Duration) -> u64 {
        controller
            .start(now, StartRequest::new("stream-1", "tui", "key-1"))
            .unwrap()
    }

    fn recording(lease: &str) -> RecordingState {
        RecordingState {
            id: "0123456789abcdef0123456789abcdef".into(),
            generation: 1,
            lease: Some(lease.into()),
            recovery_token: Some("recovery-token".into()),
            durable_offset: 0,
            chunks: 0,
            capture_state: "recording".into(),
            final_sample_count: None,
            free_seconds: None,
        }
    }

    fn create(controller: &mut RecorderController, now: Duration, mock: &MockCapture) {
        let generation = controller.generation();
        controller.handle(
            now,
            RecorderEvent::Created {
                generation,
                recording: recording("lease-token"),
            },
        );
        assert_eq!(mock.starts(), 1);
        assert_eq!(controller.state(), CaptureState::Recording);
    }

    fn take_commands(controller: &mut RecorderController) -> Vec<RecorderCommand> {
        std::iter::from_fn(|| controller.poll_command()).collect()
    }

    fn take_put(controller: &mut RecorderController) -> RecorderCommand {
        take_commands(controller)
            .into_iter()
            .find(|command| matches!(command, RecorderCommand::PutChunk { .. }))
            .expect("a chunk upload")
    }

    fn ack(controller: &mut RecorderController, now: Duration, sequence: u64, durable_offset: u64) {
        let generation = controller.generation();
        controller.handle(
            now,
            RecorderEvent::ChunkAcked {
                generation,
                sequence,
                durable_offset,
            },
        );
    }

    #[test]
    fn sha256_matches_known_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            sha256_hex(b"The quick brown fox jumps over the lazy dog"),
            "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592"
        );
    }

    struct LevelCapture {
        level: Arc<AtomicU32>,
        stopped: bool,
        events: VecDeque<CaptureEvent>,
    }

    impl CaptureSource for LevelCapture {
        fn start(&mut self) -> Result<(), String> {
            Ok(())
        }

        fn stop(&mut self) {
            if !self.stopped {
                self.stopped = true;
                self.events.push_back(CaptureEvent::Finished);
            }
        }

        fn drain(&mut self) -> Vec<CaptureEvent> {
            self.events.drain(..).collect()
        }

        fn level(&self) -> f32 {
            f32::from_bits(self.level.load(Ordering::Relaxed))
        }
    }

    #[test]
    fn recorder_snapshot_exposes_level_and_zeroes_it_when_capture_closes() {
        let level = Arc::new(AtomicU32::new(0.5f32.to_bits()));
        let mut controller = RecorderController::new(
            config(),
            Box::new(LevelCapture {
                level: level.clone(),
                stopped: false,
                events: VecDeque::new(),
            }),
        );
        controller
            .start(Duration::ZERO, StartRequest::new("s", "tui", "k"))
            .unwrap();
        let generation = controller.generation();
        controller.handle(
            Duration::ZERO,
            RecorderEvent::Created {
                generation,
                recording: recording("lease"),
            },
        );
        assert!((controller.snapshot().level - 0.5).abs() < 0.001);
        controller.stop(Duration::from_millis(10)).unwrap();
        controller.tick(Duration::from_millis(20));
        assert_eq!(controller.snapshot().level, 0.0);
    }

    #[test]
    fn recorder_capture_refuses_when_another_feature_owns_the_microphone() {
        let _test = crate::audio_capture::mic_test_guard();
        let held =
            crate::audio_capture::acquire_mic(crate::audio_capture::MicOwner::Dictation).unwrap();
        let error = MicrophoneCapture::default().start().unwrap_err();
        assert!(error.contains("dictation"), "{error}");
        drop(held);
    }

    #[test]
    fn block_sink_level_is_smoothed_rms() {
        let (events, _receiver) = std_mpsc::sync_channel(4);
        let level = Arc::new(AtomicU32::new(0));
        let mut sink = BlockSink {
            events,
            next_sample: 0,
            overflow: Arc::new(AtomicU64::new(0)),
            first_lost: Arc::new(AtomicU64::new(u64::MAX)),
            invalid: Arc::new(AtomicBool::new(false)),
            level: level.clone(),
        };
        sink.push_frame(&[0.5; 100]);
        let value = f32::from_bits(level.load(Ordering::Relaxed));
        assert!((value - crate::audio_capture::smoothed_level(0.0, 0.5)).abs() < 0.001);
        assert!(value > 0.0 && value < 0.5);
    }

    #[test]
    fn construction_and_start_do_not_open_the_microphone() {
        let (mut controller, mock) = controller();
        assert_eq!(mock.starts(), 0);
        let generation = start(&mut controller, Duration::ZERO);
        assert_eq!(generation, 1);
        assert_eq!(mock.starts(), 0);
        assert_eq!(mock.stops(), 0);
        assert_eq!(controller.state(), CaptureState::Starting);
        assert!(matches!(
            take_commands(&mut controller).as_slice(),
            [RecorderCommand::Create { generation: 1, .. }]
        ));
    }

    #[test]
    fn create_rejection_never_opens_the_microphone() {
        let (mut controller, mock) = controller();
        let generation = start(&mut controller, Duration::ZERO);
        controller.handle(
            Duration::from_secs(1),
            RecorderEvent::CreateFailed {
                generation,
                error: RecorderTransportError::Busy("another client is recording".into()),
            },
        );
        assert_eq!(mock.starts(), 0);
        assert_eq!(mock.stops(), 0);
        assert_eq!(controller.state(), CaptureState::Idle);
        assert!(controller
            .snapshot()
            .last_error
            .as_deref()
            .unwrap()
            .contains("another client"));
    }

    #[test]
    fn chunker_uses_exact_sample_offsets_and_idempotent_hashes() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);

        // 2.5 s in awkward block sizes.
        let mut emitted = 0u64;
        for block in [7usize, 5_000, 1, 20_000, 14_992] {
            mock.emit_audio(emitted, vec![100; block]);
            emitted += block as u64;
            controller.tick(Duration::from_millis(10));
        }
        assert_eq!(emitted, 40_000);

        let first = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence,
            start,
            bytes,
            sha256,
            ..
        } = first
        else {
            unreachable!()
        };
        assert_eq!(sequence, 0);
        assert_eq!(start, 0);
        assert_eq!(bytes.len(), 32_000);
        assert_eq!(sha256, sha256_hex(&bytes));

        ack(&mut controller, Duration::from_millis(20), 0, 16_000);
        let second = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence,
            start,
            bytes,
            ..
        } = second
        else {
            unreachable!()
        };
        assert_eq!(sequence, 1);
        assert_eq!(start, 16_000);
        assert_eq!(bytes.len(), 32_000);
    }

    #[test]
    fn transient_failure_retries_the_same_chunk_after_backoff() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![1; 16_000]);
        controller.tick(Duration::from_millis(10));

        let first = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence,
            start,
            bytes,
            sha256,
            ..
        } = first
        else {
            unreachable!()
        };
        let generation = controller.generation();
        controller.handle(
            Duration::from_millis(20),
            RecorderEvent::ChunkFailed {
                generation,
                sequence,
                error: RecorderTransportError::Transient("network down".into()),
            },
        );
        assert!(take_commands(&mut controller).is_empty());

        // The retry delay is 250 ms for the first failure.
        controller.tick(Duration::from_millis(200));
        assert!(take_commands(&mut controller).is_empty());
        controller.tick(Duration::from_millis(300));
        let retry = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence: retry_sequence,
            start: retry_start,
            bytes: retry_bytes,
            sha256: retry_sha,
            ..
        } = retry
        else {
            unreachable!()
        };
        assert_eq!(retry_sequence, sequence);
        assert_eq!(retry_start, start);
        assert_eq!(retry_bytes, bytes);
        assert_eq!(retry_sha, sha256);
    }

    #[test]
    fn spool_is_bounded_and_overflow_is_reported_then_drained() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);

        // 31 s at once exceeds the 30 s spool.
        mock.emit_audio(0, vec![2; SAMPLE_RATE as usize * 31]);
        controller.tick(Duration::from_millis(10));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(snapshot.pending_samples, SPOOL_SAMPLES);
        assert_eq!(mock.stops(), 1);
        assert!(snapshot.losses.iter().any(|loss| {
            loss.kind == LossKind::SpoolFull && loss.samples == Some(SAMPLE_RATE as u64)
        }));
        assert_eq!(snapshot.accepted_samples, SPOOL_SAMPLES);
        assert_eq!(snapshot.received_samples, SAMPLE_RATE as u64 * 31);

        // Drain the 30 accepted seconds; the lost second is never uploaded.
        let mut sequence = 0;
        loop {
            let command = take_put(&mut controller);
            let RecorderCommand::PutChunk {
                sequence: current, ..
            } = command
            else {
                unreachable!()
            };
            assert_eq!(current, sequence);
            ack(
                &mut controller,
                Duration::from_secs(1),
                current,
                (current + 1) * SAMPLE_RATE as u64,
            );
            sequence += 1;
            if sequence == 30 {
                break;
            }
        }
        let commands = take_commands(&mut controller);
        let stop = commands
            .iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(*final_sample_count),
                _ => None,
            })
            .expect("stop after draining");
        assert_eq!(stop, SPOOL_SAMPLES);
        assert_eq!(controller.snapshot().losses.len(), 1);
    }

    #[test]
    fn lease_loss_stops_capture_and_never_acknowledges_pending_audio() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![3; 16_000]);
        controller.tick(Duration::from_millis(10));
        assert!(matches!(
            take_put(&mut controller),
            RecorderCommand::PutChunk { .. }
        ));

        let generation = controller.generation();
        controller.handle(
            Duration::from_millis(20),
            RecorderEvent::HeartbeatFailed {
                generation,
                error: RecorderTransportError::LeaseLost("lease_expired".into()),
            },
        );

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert!(!snapshot.capture_open);
        assert_eq!(mock.stops(), 1);
        assert_eq!(snapshot.durable_offset, 0);
        assert_eq!(snapshot.accepted_samples, 16_000);
        assert!(!snapshot.stop_finished);
        // Pending audio stays visible for recovery; no stop command can be sent
        // without a lease.
        assert!(take_commands(&mut controller).is_empty());
        controller.tick(Duration::from_secs(60));
        assert!(matches!(
            controller.stop(Duration::from_secs(61)),
            Err(StopError::LeaseLost)
        ));
    }

    #[test]
    fn stop_releases_the_microphone_then_drains_before_sealing() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![4; 40_000]);
        controller.tick(Duration::from_millis(10));

        let first = take_put(&mut controller);
        let RecorderCommand::PutChunk { sequence, .. } = first else {
            unreachable!()
        };
        assert_eq!(sequence, 0);

        controller.stop(Duration::from_millis(20)).unwrap();
        // The microphone is released before the upload drain continues.
        assert_eq!(mock.stops(), 1);
        assert!(!controller.snapshot().capture_open);
        controller.tick(Duration::from_millis(20));
        let commands = take_commands(&mut controller);
        assert!(commands
            .iter()
            .all(|command| !matches!(command, RecorderCommand::Stop { .. })));

        ack(&mut controller, Duration::from_millis(30), 0, 16_000);
        let second = take_put(&mut controller);
        let RecorderCommand::PutChunk { sequence, .. } = second else {
            unreachable!()
        };
        assert_eq!(sequence, 1);
        ack(&mut controller, Duration::from_millis(40), 1, 32_000);

        let tail = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence,
            start,
            bytes,
            ..
        } = tail
        else {
            unreachable!()
        };
        assert_eq!(sequence, 2);
        assert_eq!(start, 32_000);
        assert_eq!(bytes.len(), 16_000); // 0.5 s tail, 8 000 samples
        ack(&mut controller, Duration::from_millis(50), 2, 40_000);

        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("stop after the drain");
        assert_eq!(stop, 40_000);
        assert_eq!(controller.state(), CaptureState::Stopping);

        let generation = controller.generation();
        controller.handle(
            Duration::from_millis(60),
            RecorderEvent::Stopped {
                generation,
                recording: RecordingState {
                    lease: None,
                    durable_offset: 40_000,
                    final_sample_count: Some(40_000),
                    capture_state: "stopped".into(),
                    ..recording("lease-token")
                },
            },
        );
        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Stopped);
        assert!(snapshot.stop_finished);
        assert!(snapshot.losses.is_empty());
        assert_eq!(snapshot.durable_offset, 40_000);
    }

    #[test]
    fn callback_starvation_is_reported_as_unknown_loss_and_stops_capture() {
        let mock = MockCapture::default();
        let mut controller = RecorderController::new(
            RecorderConfig {
                starvation_timeout: Duration::from_millis(500),
                ..RecorderConfig::default()
            },
            Box::new(mock.clone()),
        );
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        controller.tick(Duration::from_millis(600));
        // The mock signals Finished from stop(); the next tick performs the
        // teardown drain and seals the interrupted prefix.
        controller.tick(Duration::from_millis(700));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(mock.stops(), 1);
        assert!(snapshot.unknown_loss);
        assert!(snapshot
            .losses
            .iter()
            .any(|loss| { loss.kind == LossKind::CallbackStarved && loss.samples.is_none() }));
        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("interrupted capture still seals its empty prefix");
        assert_eq!(stop, 0);
    }

    #[test]
    fn source_gap_records_the_exact_missing_range() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![5; 1_000]);
        controller.tick(Duration::from_millis(10));
        mock.emit_audio(1_500, vec![6; 500]);
        controller.tick(Duration::from_millis(20));
        controller.tick(Duration::from_millis(30));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(mock.stops(), 1);
        // The gap and the arriving block beyond it are both explicitly lost.
        assert_eq!(snapshot.losses[0].start, 1_000);
        assert_eq!(snapshot.losses[0].samples, Some(500));
        assert_eq!(snapshot.losses[0].kind, LossKind::SourceGap);
        assert_eq!(snapshot.losses[1].start, 1_500);
        assert_eq!(snapshot.losses[1].samples, Some(500));
        assert_eq!(snapshot.losses[1].kind, LossKind::DroppedAfterCut);
        // The 1 s before the gap sits in the partial (final) chunk; the lost
        // ranges are excluded from the upload.
        assert_eq!(snapshot.accepted_samples, 1_000);
        assert_eq!(snapshot.pending_samples, 1_000);
        assert_eq!(snapshot.received_samples, 2_000);
    }

    #[test]
    fn late_or_foreign_generation_acks_are_ignored() {
        let (mut controller, mock) = controller();
        let first = start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![7; 16_000]);
        controller.tick(Duration::from_millis(10));
        let RecorderCommand::PutChunk { sequence, .. } = take_put(&mut controller) else {
            unreachable!()
        };
        let generation = controller.generation();
        controller.handle(
            Duration::from_millis(20),
            RecorderEvent::HeartbeatFailed {
                generation,
                error: RecorderTransportError::LeaseLost("lease_expired".into()),
            },
        );
        // The spool is still pending; clearing it would silently discard audio.
        assert!(matches!(
            controller.start(
                Duration::from_millis(30),
                StartRequest::new("stream-2", "tui", "key-2"),
            ),
            Err(StartError::UnrecoveredAudio { .. })
        ));

        controller.handle(
            Duration::from_millis(40),
            RecorderEvent::ChunkAcked {
                generation: first,
                sequence,
                durable_offset: 16_000,
            },
        );
        controller.handle(
            Duration::from_millis(50),
            RecorderEvent::ChunkAcked {
                generation: first + 1,
                sequence,
                durable_offset: 16_000,
            },
        );
        assert_eq!(controller.snapshot().durable_offset, 0);
    }

    #[test]
    fn stop_drains_queued_callback_audio_after_device_teardown() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        // The block is still queued when Stop is requested. It must not be
        // dropped by a control-priority teardown.
        mock.emit_audio(0, vec![9; 16_000]);

        controller.stop(Duration::from_millis(10)).unwrap();
        let put = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence,
            start,
            bytes,
            ..
        } = put
        else {
            unreachable!()
        };
        assert_eq!(sequence, 0);
        assert_eq!(start, 0);
        assert_eq!(bytes.len(), 32_000);
        ack(&mut controller, Duration::from_millis(20), 0, 16_000);

        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("the queued callback audio was sealed");
        assert_eq!(stop, 16_000);
    }

    #[test]
    fn source_gap_reports_the_gap_and_every_block_beyond_it() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![1; 1_000]);
        mock.emit_audio(1_500, vec![2; 500]);
        mock.emit_audio(3_000, vec![3; 500]);
        controller.tick(Duration::from_millis(10));
        controller.tick(Duration::from_millis(20));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(mock.stops(), 1);
        assert_eq!(snapshot.lost_samples, 1_500);
        assert_eq!(snapshot.losses.len(), 3);
        assert_eq!(snapshot.losses[0].kind, LossKind::SourceGap);
        assert_eq!(snapshot.losses[0].samples, Some(500));
        assert_eq!(snapshot.losses[1].kind, LossKind::DroppedAfterCut);
        assert_eq!(snapshot.losses[1].samples, Some(500));
        assert_eq!(snapshot.losses[2].kind, LossKind::DroppedAfterCut);
        assert_eq!(snapshot.losses[2].samples, Some(500));
        assert_eq!(snapshot.accepted_samples, 1_000);
        assert_eq!(snapshot.pending_samples, 1_000);
        assert_eq!(snapshot.received_samples, 3_500);
    }

    #[test]
    fn cpal_discontinuity_stops_capture_with_unknown_loss() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit(CaptureEvent::Discontinuity(
            "an xrun invalidated the sample timeline".into(),
        ));
        controller.tick(Duration::from_millis(10));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(mock.stops(), 1);
        assert!(snapshot.unknown_loss);
        assert!(snapshot
            .losses
            .iter()
            .any(|loss| loss.kind == LossKind::Discontinuity && loss.samples.is_none()));

        // The explicit capture-finished signal lets it seal the empty prefix.
        controller.tick(Duration::from_millis(20));
        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("discontinuity still seals the committed prefix");
        assert_eq!(stop, 0);
    }

    #[test]
    fn failed_capture_is_not_finished_until_the_explicit_finished_signal() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit(CaptureEvent::Failed("device lost".into()));
        controller.tick(Duration::from_millis(10));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert!(!snapshot.capture_finished);
        assert_eq!(mock.stops(), 1);
        // No Stop command yet: the controller must not guess that teardown and
        // queued-audio drain have completed.
        assert!(take_commands(&mut controller)
            .iter()
            .all(|command| !matches!(command, RecorderCommand::Stop { .. })));

        // The mock's stop queued Finished; the next tick is the real boundary.
        controller.tick(Duration::from_millis(20));
        assert!(controller.snapshot().capture_finished);
        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("Finished allows sealing the committed prefix");
        assert_eq!(stop, 0);
    }

    #[test]
    fn bounded_queue_overflow_is_explicit_and_stops_capture() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit(CaptureEvent::Overflow {
            dropped_samples: None,
            dropped_from: None,
            source: OverflowSource::AudioQueue,
        });
        controller.tick(Duration::from_millis(10));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(mock.stops(), 1);
        assert!(snapshot.unknown_loss);
        assert!(snapshot
            .losses
            .iter()
            .any(|loss| loss.kind == LossKind::CaptureQueueOverflow && loss.samples.is_none()));

        controller.tick(Duration::from_millis(20));
        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("overflow still seals the committed prefix");
        assert_eq!(stop, 0);
    }

    #[test]
    fn event_overflow_keeps_the_safe_queued_prefix_before_its_exact_cut() {
        let mut capture = MicrophoneCapture::default();
        for start in [0u64, 10] {
            capture
                .events_tx
                .try_send(CaptureEvent::Audio(AudioBlock {
                    start_sample: start,
                    samples: vec![0; 10],
                }))
                .unwrap();
        }
        // These blocks were queued before the overflow, but a racing post-loss
        // block is also present after the latched first-lost offset.
        capture
            .events_tx
            .try_send(CaptureEvent::Audio(AudioBlock {
                start_sample: 20,
                samples: vec![0; 10],
            }))
            .unwrap();
        capture.event_overflow_samples = Some(Arc::new(AtomicU64::new(20)));
        capture.event_overflow_start = Some(Arc::new(AtomicU64::new(20)));

        let events = capture.drain();
        assert!(matches!(
            events.first(),
            Some(CaptureEvent::Audio(AudioBlock {
                start_sample: 0,
                ..
            }))
        ));
        assert!(matches!(
            events.get(1),
            Some(CaptureEvent::Audio(AudioBlock {
                start_sample: 10,
                ..
            }))
        ));
        assert!(matches!(
            events.get(2),
            Some(CaptureEvent::Overflow {
                dropped_samples: Some(20),
                dropped_from: Some(20),
                source: OverflowSource::EventQueue,
            })
        ));
        assert!(matches!(
            events.get(3),
            Some(CaptureEvent::Audio(AudioBlock {
                start_sample: 20,
                ..
            }))
        ));
        // The counters are consumed, not replayed forever.
        assert!(capture.drain().is_empty());
    }

    #[test]
    fn audio_queue_overflow_cuts_before_queued_audio_without_a_fake_range() {
        let mut capture = MicrophoneCapture::default();
        capture
            .events_tx
            .try_send(CaptureEvent::Audio(AudioBlock {
                start_sample: 0,
                samples: vec![0; 10],
            }))
            .unwrap();
        capture.audio_overflow_blocks = Some(Arc::new(AtomicU64::new(1)));

        let events = capture.drain();
        assert!(matches!(
            events.first(),
            Some(CaptureEvent::Overflow {
                dropped_samples: None,
                dropped_from: None,
                source: OverflowSource::AudioQueue,
            })
        ));
        assert!(matches!(events.get(1), Some(CaptureEvent::Audio(_))));
        assert!(capture.drain().is_empty());
    }

    #[test]
    fn pre_overflow_queued_prefix_is_saved_before_the_exact_cut() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![1; 100]);
        mock.emit_audio(100, vec![2; 100]);
        mock.emit(CaptureEvent::Overflow {
            dropped_samples: Some(50),
            dropped_from: Some(200),
            source: OverflowSource::EventQueue,
        });
        controller.tick(Duration::from_millis(10));
        // The mock stop triggered by the cut queues Finished; the next tick
        // flushes the safe prefix as the final short chunk.
        controller.tick(Duration::from_millis(20));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(snapshot.accepted_samples, 200);
        assert_eq!(snapshot.pending_samples, 200);
        assert_eq!(snapshot.losses.len(), 1);
        assert_eq!(snapshot.losses[0].start, 200);
        assert_eq!(snapshot.losses[0].samples, Some(50));
        assert_eq!(snapshot.lost_samples, 50);

        let put = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence, start, ..
        } = put
        else {
            unreachable!()
        };
        assert_eq!(sequence, 0);
        assert_eq!(start, 0);
        ack(&mut controller, Duration::from_millis(30), 0, 200);
        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("the safe prefix is sealed");
        assert_eq!(stop, 200);
    }

    #[test]
    fn event_queue_overflow_carries_exact_16k_samples() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit(CaptureEvent::Overflow {
            dropped_samples: Some(777),
            dropped_from: Some(0),
            source: OverflowSource::EventQueue,
        });
        controller.tick(Duration::from_millis(10));

        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert_eq!(snapshot.lost_samples, 777);
        assert!(snapshot
            .losses
            .iter()
            .any(|loss| loss.kind == LossKind::CaptureQueueOverflow && loss.samples == Some(777)));
    }

    #[test]
    fn teardown_drain_forwards_blocks_queued_before_the_stop() {
        let (audio, audio_rx) = std_mpsc::sync_channel(4);
        let (events, receiver) = std_mpsc::sync_channel(8);
        let mut pump = FramePump::new(
            16_000,
            1,
            BlockSink {
                events,
                next_sample: 0,
                overflow: Arc::new(AtomicU64::new(0)),
                first_lost: Arc::new(AtomicU64::new(u64::MAX)),
                invalid: Arc::new(AtomicBool::new(false)),
                level: Arc::new(AtomicU32::new(0)),
            },
        )
        .unwrap();
        audio
            .try_send(BoundedAudioMessage::Samples(vec![0.25; 160]))
            .unwrap();
        drop(audio);

        drain_after_teardown(&audio_rx, &mut pump);

        match receiver.try_recv() {
            Ok(CaptureEvent::Audio(block)) => {
                assert_eq!(block.start_sample, 0);
                assert_eq!(block.samples.len(), 160);
            }
            other => panic!("queued callback audio was not drained: {other:?}"),
        }
    }

    #[test]
    fn bounded_event_queue_reports_overflow_without_blocking() {
        let (events, receiver) = std_mpsc::sync_channel(1);
        let overflow = Arc::new(AtomicU64::new(0));
        let first_lost = Arc::new(AtomicU64::new(u64::MAX));
        let invalid = Arc::new(AtomicBool::new(false));
        let mut sink = BlockSink {
            events,
            next_sample: 0,
            overflow: overflow.clone(),
            first_lost: first_lost.clone(),
            invalid: invalid.clone(),
            level: Arc::new(AtomicU32::new(0)),
        };
        sink.push_frame(&[0.1; 100]);
        sink.push_frame(&[0.2; 50]);
        // Once invalid, later audio is dropped and counted, never queued.
        sink.push_frame(&[0.3; 25]);

        assert!(invalid.load(Ordering::Relaxed));
        assert_eq!(overflow.load(Ordering::Relaxed), 75);
        assert_eq!(first_lost.load(Ordering::Relaxed), 100);
        assert_eq!(sink.next_sample, 175);
        assert!(matches!(receiver.try_recv(), Ok(CaptureEvent::Audio(_))));
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn concurrent_producer_overflow_latches_gate_before_drain() {
        let (events, receiver) = std_mpsc::sync_channel(1);
        let overflow = Arc::new(AtomicU64::new(0));
        let first_lost = Arc::new(AtomicU64::new(u64::MAX));
        let invalid = Arc::new(AtomicBool::new(false));
        let sink = Arc::new(Mutex::new(BlockSink {
            events,
            next_sample: 0,
            overflow: overflow.clone(),
            first_lost: first_lost.clone(),
            invalid: invalid.clone(),
            level: Arc::new(AtomicU32::new(0)),
        }));
        sink.lock().unwrap().push_frame(&[0.1; 10]);
        let barrier = Arc::new(Barrier::new(2));
        let producer = {
            let sink = sink.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                let mut sink = sink.lock().unwrap();
                // The queue is full: this overflow latches the gate, and the
                // following block must never be queued.
                sink.push_frame(&[0.2; 10]);
                sink.push_frame(&[0.3; 10]);
            })
        };
        barrier.wait();
        producer.join().unwrap();

        assert!(invalid.load(Ordering::Relaxed));
        assert_eq!(overflow.load(Ordering::Relaxed), 20);
        assert_eq!(first_lost.load(Ordering::Relaxed), 10);
        let received = receiver.try_recv().expect("the pre-overflow block");
        assert!(matches!(received, CaptureEvent::Audio(_)));
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn stream_detached_is_rebindable_and_never_falsely_permanent() {
        let (mut controller, mock) = controller();
        start(&mut controller, Duration::ZERO);
        create(&mut controller, Duration::ZERO, &mock);
        mock.emit_audio(0, vec![8; 8_000]);
        controller.tick(Duration::from_millis(10));

        let generation = controller.generation();
        controller.handle(
            Duration::from_millis(20),
            RecorderEvent::HeartbeatFailed {
                generation,
                error: RecorderTransportError::StreamDetached("stream_detached".into()),
            },
        );
        let snapshot = controller.snapshot();
        assert_eq!(snapshot.state, CaptureState::Interrupted);
        assert!(snapshot.rebind_required);
        assert_eq!(snapshot.pending_samples, 8_000);
        assert_eq!(mock.stops(), 1);
        assert!(matches!(
            controller.stop(Duration::from_millis(30)),
            Err(StopError::StreamDetached)
        ));

        // The explicit teardown signal is independent of detach.
        controller.tick(Duration::from_millis(30));
        assert!(controller.snapshot().capture_finished);

        controller.rebind("stream-2").unwrap();
        let rebind = take_commands(&mut controller)
            .into_iter()
            .find(|command| matches!(command, RecorderCommand::Rebind { .. }))
            .expect("a rebind command");
        let RecorderCommand::Rebind { stream_id, .. } = rebind else {
            unreachable!()
        };
        assert_eq!(stream_id, "stream-2");

        controller.handle(
            Duration::from_millis(40),
            RecorderEvent::Rebound {
                generation,
                stream_id: "stream-2".into(),
            },
        );
        let put = take_put(&mut controller);
        let RecorderCommand::PutChunk {
            sequence, start, ..
        } = put
        else {
            unreachable!()
        };
        assert_eq!(sequence, 0);
        assert_eq!(start, 0);
        ack(&mut controller, Duration::from_millis(50), 0, 8_000);

        let stop = take_commands(&mut controller)
            .into_iter()
            .find_map(|command| match command {
                RecorderCommand::Stop {
                    final_sample_count, ..
                } => Some(final_sample_count),
                _ => None,
            })
            .expect("rebind lets the pre-detach tail drain and seal");
        assert_eq!(stop, 8_000);
    }
}
