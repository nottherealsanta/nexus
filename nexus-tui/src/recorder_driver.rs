//! Recorder transport/spool driver (plan 54, C-TUI).
//!
//! This module connects the deterministic [`RecorderController`] to Core over
//! HTTP and to the durable [`crate::recorder_spool`] store. It owns no UI and no
//! microphone: the controller opens capture only after the driver creates the
//! Core recording and the durable spool.
//!
//! Every `PutChunk` is written to the spool **before** it is sent. Core's ack
//! then advances the spool. A chunk is never treated as saved merely because it
//! was captured in memory. Lease loss stops the controller and marks the spool
//! interrupted; later recovery uses only the stored recovery capability.
//!
//! Secrets (lease/recovery tokens) travel only in headers through
//! [`RecorderApi`] implementations. Driver diagnostics and recovery reports
//! redact them.

use std::fmt;
use std::future::Future;
use std::pin::Pin;
use std::time::{Duration, Instant};

use serde_json::Value;
use tokio::sync::watch;

use crate::api::CoreApi;
use crate::recorder::{
    CaptureState, RecorderCommand, RecorderController, RecorderEvent, RecorderSnapshot,
    RecorderTransportError, RecordingState, StartError, StartRequest, StopError,
};
use crate::recorder_spool::{
    RecoveryAction, Secret, ServerBaseline, SpoolApp, SpoolError, SpoolMeta, SpoolSession,
    SpoolState, SpoolStore,
};
use crate::transport::Error as TransportError;

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;
pub type ApiResult<T> = std::result::Result<T, RecorderTransportError>;

/// Bounded number of recovery sessions one pass handles.
pub const MAX_RECOVERY_SESSIONS: usize = 32;
const MAX_WARNINGS: usize = 16;
/// Warn at start when Core has less than this much recording space (ten minutes).
const LOW_DISK_SECONDS: u64 = 600;
/// Marker the app turns into the manifest's `recorder.low_disk` text.
pub const LOW_DISK_PREFIX: &str = "low_disk:";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChunkAck {
    pub durable_offset: u64,
    pub next_sequence: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordingSnapshot {
    pub recording: RecordingState,
    pub active: bool,
    pub sealed: bool,
}

/// The transport surface the driver needs. Implementations must add the
/// recorder-specific headers/body themselves; the driver never places a token in
/// a URL or a diagnostic.
pub trait RecorderApi: Send + Sync {
    fn create<'a>(
        &'a self,
        request: &'a StartRequest,
        idempotency_key: &'a str,
        recovery: &'a str,
    ) -> BoxFuture<'a, ApiResult<RecordingState>>;

    /// Lookup-only create. `CreateNotFound` means the key was withdrawn and no
    /// recording exists; `CreateWithdrawn` means another create/resume won.
    fn resume<'a>(
        &'a self,
        client_id: &'a str,
        idempotency_key: &'a str,
        recovery: &'a str,
        title: Option<&'a str>,
    ) -> BoxFuture<'a, ApiResult<RecordingSnapshot>>;

    fn put_chunk<'a>(
        &'a self,
        recording_id: &'a str,
        sequence: u64,
        start: u64,
        lease: &'a str,
        stream_id: &'a str,
        sha256: &'a str,
        body: &'a [u8],
    ) -> BoxFuture<'a, ApiResult<ChunkAck>>;

    fn heartbeat<'a>(
        &'a self,
        recording_id: &'a str,
        lease: &'a str,
        stream_id: &'a str,
    ) -> BoxFuture<'a, ApiResult<()>>;

    fn rebind<'a>(
        &'a self,
        recording_id: &'a str,
        lease: &'a str,
        stream_id: &'a str,
        client_id: &'a str,
    ) -> BoxFuture<'a, ApiResult<()>>;

    fn stop<'a>(
        &'a self,
        recording_id: &'a str,
        lease: &'a str,
        stream_id: &'a str,
        final_sample_count: u64,
    ) -> BoxFuture<'a, ApiResult<RecordingState>>;

    fn get<'a>(&'a self, recording_id: &'a str) -> BoxFuture<'a, ApiResult<RecordingSnapshot>>;

    fn recover<'a>(
        &'a self,
        recording_id: &'a str,
        recovery_token: &'a str,
        client_id: &'a str,
        final_sample_count: u64,
    ) -> BoxFuture<'a, ApiResult<()>>;

    fn recover_chunk<'a>(
        &'a self,
        recording_id: &'a str,
        sequence: u64,
        start: u64,
        recovery_token: &'a str,
        client_id: &'a str,
        sha256: &'a str,
        body: &'a [u8],
    ) -> BoxFuture<'a, ApiResult<()>>;
}

/// Real Core implementation. The HTTP methods below are in `CoreApi`; this
/// adapter only maps wire JSON into the controller/spool types.
#[derive(Clone)]
pub struct CoreRecorderApi {
    pub api: CoreApi,
}

impl CoreRecorderApi {
    pub fn new(api: CoreApi) -> Self {
        Self { api }
    }
}

impl RecorderApi for CoreRecorderApi {
    fn create<'a>(
        &'a self,
        request: &'a StartRequest,
        idempotency_key: &'a str,
        recovery: &'a str,
    ) -> BoxFuture<'a, ApiResult<RecordingState>> {
        Box::pin(async move {
            let value = self
                .api
                .recorder_create(
                    &request.stream_id,
                    &request.client_id,
                    request.title.as_deref(),
                    idempotency_key,
                    recovery,
                )
                .await
                .map_err(api_error)?;
            recording_from_value(&value)
        })
    }

    fn resume<'a>(
        &'a self,
        client_id: &'a str,
        idempotency_key: &'a str,
        recovery: &'a str,
        title: Option<&'a str>,
    ) -> BoxFuture<'a, ApiResult<RecordingSnapshot>> {
        Box::pin(async move {
            let value = self
                .api
                .recorder_resume(client_id, idempotency_key, recovery, title)
                .await
                .map_err(api_error)?;
            snapshot_from_value(&value)
        })
    }

    fn put_chunk<'a>(
        &'a self,
        recording_id: &'a str,
        sequence: u64,
        start: u64,
        lease: &'a str,
        stream_id: &'a str,
        sha256: &'a str,
        body: &'a [u8],
    ) -> BoxFuture<'a, ApiResult<ChunkAck>> {
        Box::pin(async move {
            let value = self
                .api
                .recorder_put_chunk(
                    recording_id,
                    sequence,
                    start,
                    lease,
                    stream_id,
                    sha256,
                    body,
                )
                .await
                .map_err(api_error)?;
            chunk_ack_from_value(&value, sequence)
        })
    }

    fn heartbeat<'a>(
        &'a self,
        recording_id: &'a str,
        lease: &'a str,
        stream_id: &'a str,
    ) -> BoxFuture<'a, ApiResult<()>> {
        Box::pin(async move {
            self.api
                .recorder_heartbeat(recording_id, lease, stream_id)
                .await
                .map_err(api_error)
                .map(|_| ())
        })
    }

    fn rebind<'a>(
        &'a self,
        recording_id: &'a str,
        lease: &'a str,
        stream_id: &'a str,
        client_id: &'a str,
    ) -> BoxFuture<'a, ApiResult<()>> {
        Box::pin(async move {
            self.api
                .recorder_rebind(recording_id, lease, stream_id, client_id)
                .await
                .map_err(api_error)
                .map(|_| ())
        })
    }

    fn stop<'a>(
        &'a self,
        recording_id: &'a str,
        lease: &'a str,
        stream_id: &'a str,
        final_sample_count: u64,
    ) -> BoxFuture<'a, ApiResult<RecordingState>> {
        Box::pin(async move {
            let value = self
                .api
                .recorder_stop(recording_id, lease, stream_id, final_sample_count)
                .await
                .map_err(api_error)?;
            recording_from_value(&value)
        })
    }

    fn get<'a>(&'a self, recording_id: &'a str) -> BoxFuture<'a, ApiResult<RecordingSnapshot>> {
        Box::pin(async move {
            let value = self
                .api
                .recorder_get(recording_id)
                .await
                .map_err(api_error)?;
            snapshot_from_value(&value)
        })
    }

    fn recover<'a>(
        &'a self,
        recording_id: &'a str,
        recovery_token: &'a str,
        client_id: &'a str,
        final_sample_count: u64,
    ) -> BoxFuture<'a, ApiResult<()>> {
        Box::pin(async move {
            self.api
                .recorder_recover(
                    recording_id,
                    recovery_token,
                    client_id,
                    final_sample_count,
                    None,
                )
                .await
                .map_err(api_error)
                .map(|_| ())
        })
    }

    fn recover_chunk<'a>(
        &'a self,
        recording_id: &'a str,
        sequence: u64,
        start: u64,
        recovery_token: &'a str,
        client_id: &'a str,
        sha256: &'a str,
        body: &'a [u8],
    ) -> BoxFuture<'a, ApiResult<()>> {
        Box::pin(async move {
            self.api
                .recorder_recover_chunk(
                    recording_id,
                    sequence,
                    start,
                    recovery_token,
                    client_id,
                    None,
                    sha256,
                    body,
                )
                .await
                .map_err(api_error)
                .map(|_| ())
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DriverError {
    Start(StartError),
    Stop(StopError),
    Spool(SpoolError),
    Recovery(String),
}

impl fmt::Display for DriverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Start(error) => write!(f, "could not start recorder: {error:?}"),
            Self::Stop(error) => write!(f, "could not stop recorder: {error:?}"),
            Self::Spool(error) => write!(f, "recorder spool: {error}"),
            Self::Recovery(message) => write!(f, "recorder recovery: {message}"),
        }
    }
}

impl std::error::Error for DriverError {}

impl From<SpoolError> for DriverError {
    fn from(error: SpoolError) -> Self {
        Self::Spool(error)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct RecorderDriverStatus {
    pub capture: RecorderSnapshot,
    pub spool_state: Option<SpoolState>,
    pub spool_pending_samples: u64,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecoveryIssueKind {
    Api,
    Spool,
    Conflict,
    Gap,
    Active,
    MissingRecording,
    CoreRestartRequired,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryIssue {
    pub recording_id: Option<String>,
    pub kind: RecoveryIssueKind,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RecoveryReport {
    pub attempted: usize,
    pub recovered: usize,
    pub truncated: bool,
    pub issues: Vec<RecoveryIssue>,
}

enum RecoverOne {
    Recovered,
    Issue(RecoveryIssue),
}

/// A dequeued command whose future may be dropped by cancellation. It carries
/// only non-secret identifiers so `run` can turn cancellation into the matching
/// controller event instead of silently losing the command.
enum InFlight {
    Create(u64),
    PutChunk(u64, u64),
    Heartbeat(u64),
    Stop(u64),
    Rebind(u64),
}

impl InFlight {
    fn is_create(&self) -> bool {
        matches!(self, Self::Create(_))
    }

    fn from_command(command: &RecorderCommand) -> Self {
        match command {
            RecorderCommand::Create { generation, .. } => Self::Create(*generation),
            RecorderCommand::PutChunk {
                generation,
                sequence,
                ..
            } => Self::PutChunk(*generation, *sequence),
            RecorderCommand::Heartbeat { generation, .. } => Self::Heartbeat(*generation),
            RecorderCommand::Stop { generation, .. } => Self::Stop(*generation),
            RecorderCommand::Rebind { generation, .. } => Self::Rebind(*generation),
        }
    }

    fn aborted(self) -> RecorderEvent {
        match self {
            Self::Create(generation) => RecorderEvent::CreateFailed {
                generation,
                error: RecorderTransportError::Permanent(
                    "recorder driver was cancelled while create was in flight; Core may hold a recording whose recovery capability was not persisted"
                        .into(),
                ),
            },
            Self::PutChunk(generation, sequence) => RecorderEvent::ChunkFailed {
                generation,
                sequence,
                error: RecorderTransportError::Transient(
                    "recorder driver was cancelled during chunk upload".into(),
                ),
            },
            Self::Heartbeat(generation) => RecorderEvent::HeartbeatFailed {
                generation,
                error: RecorderTransportError::Transient(
                    "recorder driver was cancelled during heartbeat".into(),
                ),
            },
            Self::Stop(generation) => RecorderEvent::StopFailed {
                generation,
                error: RecorderTransportError::Transient(
                    "recorder driver was cancelled during stop".into(),
                ),
            },
            Self::Rebind(generation) => RecorderEvent::RebindFailed {
                generation,
                error: RecorderTransportError::Transient(
                    "recorder driver was cancelled during rebind".into(),
                ),
            },
        }
    }
}

/// Drives one controller. `run` is the long-lived task; `step` executes at most
/// one controller command and is the deterministic test entry point.
pub struct RecorderDriver<A: RecorderApi> {
    controller: RecorderController,
    api: A,
    spool_store: SpoolStore,
    spool: Option<SpoolSession>,
    warnings: Vec<String>,
    in_flight: Option<InFlight>,
}

impl<A: RecorderApi> RecorderDriver<A> {
    pub fn new(controller: RecorderController, api: A, spool_store: SpoolStore) -> Self {
        Self {
            controller,
            api,
            spool_store,
            spool: None,
            warnings: Vec::new(),
            in_flight: None,
        }
    }

    pub fn start(&mut self, now: Duration, request: StartRequest) -> Result<u64, StartError> {
        self.controller.start(now, request)
    }

    pub fn stop(&mut self, now: Duration) -> Result<(), StopError> {
        self.controller.stop(now)
    }

    /// Rebind a detached capture to a new event stream. The subsequent
    /// `Rebind` command is executed by `run`/`step`.
    pub fn rebind(&mut self, stream_id: impl Into<String>) -> Result<(), StopError> {
        self.controller.rebind(stream_id)
    }

    pub fn snapshot(&self) -> RecorderSnapshot {
        self.controller.snapshot()
    }

    pub fn state(&self) -> CaptureState {
        self.controller.state()
    }

    pub fn spool_meta(&self) -> Option<&SpoolMeta> {
        self.spool.as_ref().map(SpoolSession::meta)
    }

    pub fn status(&self) -> RecorderDriverStatus {
        RecorderDriverStatus {
            capture: self.controller.snapshot(),
            spool_state: self.spool.as_ref().map(|spool| spool.meta().state),
            spool_pending_samples: self.spool.as_ref().map_or(0, SpoolSession::pending_samples),
            warnings: self.warnings.clone(),
        }
    }

    pub fn take_warnings(&mut self) -> Vec<String> {
        std::mem::take(&mut self.warnings)
    }

    /// Run until cancellation. Each iteration executes at most one command; a
    /// pending HTTP request is cancelled by dropping the step future.
    pub async fn run(&mut self, mut cancel: watch::Receiver<bool>) -> Result<(), DriverError> {
        let started = Instant::now();
        loop {
            if *cancel.borrow() {
                return Ok(());
            }
            let worked = tokio::select! {
                result = self.step(started.elapsed()) => result?,
                changed = cancel.changed() => {
                    // The step future was dropped. Do not lose the command it
                    // had already dequeued: feed the matching transient event so
                    // the controller retries (or reports the create blocker).
                    let create_cancelled = self
                        .in_flight
                        .as_ref()
                        .is_some_and(InFlight::is_create);
                    self.abort_in_flight(started.elapsed());
                    if create_cancelled {
                        // A user cancel must not leave Core's create in limbo.
                        // This is the lookup/seal path, bounded and off the UI.
                        let _ = tokio::time::timeout(
                            Duration::from_secs(5),
                            self.recover_pending_starts(),
                        )
                        .await;
                    }
                    if changed.is_err() || *cancel.borrow() {
                        return Ok(());
                    }
                    continue;
                }
            };
            if worked {
                tokio::task::yield_now().await;
            } else {
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(25)) => {}
                    changed = cancel.changed() => {
                        if changed.is_err() || *cancel.borrow() {
                            return Ok(());
                        }
                    }
                }
            }
        }
    }

    /// Execute at most one queued controller command. Returns whether one ran.
    pub async fn step(&mut self, now: Duration) -> Result<bool, DriverError> {
        self.controller.tick(now);
        let Some(command) = self.controller.poll_command() else {
            return Ok(false);
        };
        self.in_flight = Some(InFlight::from_command(&command));
        self.execute(command, now).await;
        self.in_flight = None;
        self.controller.tick(now);
        Ok(true)
    }

    fn abort_in_flight(&mut self, now: Duration) {
        if let Some(in_flight) = self.in_flight.take() {
            self.controller.handle(now, in_flight.aborted());
        }
    }

    async fn execute(&mut self, command: RecorderCommand, now: Duration) {
        match command {
            RecorderCommand::Create {
                generation,
                request,
            } => self.execute_create(now, generation, request).await,
            RecorderCommand::PutChunk {
                generation,
                recording_id,
                lease,
                stream_id,
                sequence,
                start,
                bytes,
                sha256,
            } => {
                self.execute_put(
                    now,
                    generation,
                    recording_id,
                    lease,
                    stream_id,
                    sequence,
                    start,
                    bytes,
                    sha256,
                )
                .await;
            }
            RecorderCommand::Heartbeat {
                generation,
                recording_id,
                lease,
                stream_id,
            } => {
                self.execute_heartbeat(now, generation, recording_id, lease, stream_id)
                    .await
            }
            RecorderCommand::Stop {
                generation,
                recording_id,
                lease,
                stream_id,
                final_sample_count,
            } => {
                self.execute_stop(
                    now,
                    generation,
                    recording_id,
                    lease,
                    stream_id,
                    final_sample_count,
                )
                .await;
            }
            RecorderCommand::Rebind {
                generation,
                recording_id,
                lease,
                stream_id,
                client_id,
            } => {
                self.execute_rebind(now, generation, recording_id, lease, stream_id, client_id)
                    .await;
            }
        }
    }

    async fn execute_create(&mut self, now: Duration, generation: u64, request: StartRequest) {
        let (key, recovery) = match new_start_ids() {
            Ok(ids) => ids,
            Err(error) => {
                self.controller.handle(
                    now,
                    RecorderEvent::CreateFailed {
                        generation,
                        error: RecorderTransportError::Permanent(error),
                    },
                );
                return;
            }
        };
        let pending = match self.spool_store.begin_pending_start(
            &key,
            recovery.clone(),
            &request.client_id,
            request.title.as_deref(),
        ) {
            Ok(pending) => pending,
            Err(error) => {
                self.controller.handle(
                    now,
                    RecorderEvent::CreateFailed {
                        generation,
                        error: RecorderTransportError::Permanent(format!(
                            "could not persist the recorder create intent: {error}"
                        )),
                    },
                );
                return;
            }
        };

        let mut recording = None;
        for attempt in 0..3 {
            match self.api.create(&request, &key, recovery.expose()).await {
                Ok(value) => {
                    recording = Some(value);
                    break;
                }
                Err(error) if error.is_transient() && attempt < 2 => {
                    tokio::time::sleep(Duration::from_millis(100 * (attempt + 1))).await;
                }
                Err(error) => {
                    // Keep the pending record: a later resume lookup can still
                    // find the recording if Core committed the create.
                    drop(pending);
                    self.controller
                        .handle(now, RecorderEvent::CreateFailed { generation, error });
                    return;
                }
            }
        }
        let Some(recording) = recording else {
            drop(pending);
            self.controller.handle(
                now,
                RecorderEvent::CreateFailed {
                    generation,
                    error: RecorderTransportError::Transient(
                        "recorder create did not complete".into(),
                    ),
                },
            );
            return;
        };

        if matches!(recording.lease, None) || recording.capture_state != "recording" {
            // A replay can resolve to an interrupted/sealed recording without a
            // lease. Never open the microphone; finish recovery and leave the
            // controller idle.
            let detail = self
                .finish_replayed_create(&recording, &request, &recovery)
                .await;
            if let Some(message) = detail {
                drop(pending);
                self.controller.handle(
                    now,
                    RecorderEvent::CreateFailed {
                        generation,
                        error: RecorderTransportError::Permanent(message),
                    },
                );
            } else {
                let _ = pending.finish();
                self.controller.handle(
                    now,
                    RecorderEvent::CreateFailed {
                        generation,
                        error: RecorderTransportError::Permanent(
                            "the create replayed an interrupted recording; it was sealed without opening the microphone"
                                .into(),
                        ),
                    },
                );
            }
            return;
        }

        if let Some(seconds) = recording.free_seconds.filter(|s| *s < LOW_DISK_SECONDS) {
            self.warn(format!(
                "{LOW_DISK_PREFIX}{}",
                crate::recorder_view::format_elapsed(seconds * 16_000)
            ));
        }
        let lease = recording.lease.clone().expect("checked above");
        let mut meta = SpoolMeta::new(
            recording.id.clone(),
            request.client_id.clone(),
            recording.generation,
            recovery.expose().to_owned(),
            Some(request.stream_id.clone()),
        )
        .with_app(SpoolApp::Tui);
        // An active idempotent replay can already have durable chunks. Resume
        // sequence/offset numbers from Core instead of assuming a fresh zero.
        meta.durable_offset = recording.durable_offset;
        meta.accepted_samples = recording.durable_offset;
        meta.received_samples = recording.durable_offset;
        meta.next_sequence = recording.chunks;
        match self.spool_store.create(meta) {
            Ok(spool) => {
                self.spool = Some(spool);
                if let Err(error) = pending.finish() {
                    self.warn(format!(
                        "spool is durable but the pending create record could not be removed: {error}"
                    ));
                }
                // The recovery capability and spool are now durable. Cancellation
                // after this point cannot lose them.
                self.in_flight = None;
                self.controller.handle(
                    now,
                    RecorderEvent::Created {
                        generation,
                        recording,
                    },
                );
            }
            Err(error) => {
                // Keep the pending record: recovery can find and seal the
                // recording after the failed spool write. Best-effort stop makes
                // the common case leave no active lease.
                let cleanup = self
                    .api
                    .stop(
                        &recording.id,
                        &lease,
                        &request.stream_id,
                        recording.durable_offset,
                    )
                    .await;
                let detail = match cleanup {
                    Ok(_) => format!(
                        "could not create the durable recorder spool: {error}; the Core recording was sealed before capture"
                    ),
                    Err(stop_error) => format!(
                        "could not create the durable recorder spool: {error}; the cleanup stop also failed ({stop_error:?}), so the pending create remains for recovery"
                    ),
                };
                self.warn(detail.clone());
                drop(pending);
                self.controller.handle(
                    now,
                    RecorderEvent::CreateFailed {
                        generation,
                        error: RecorderTransportError::Permanent(detail),
                    },
                );
            }
        }
    }

    /// Seal/delete a replayed create that has no active lease. Returns an error
    /// message when recovery could not complete (the pending record is kept).
    async fn finish_replayed_create(
        &mut self,
        recording: &RecordingState,
        request: &StartRequest,
        recovery: &Secret,
    ) -> Option<String> {
        match self.spool_store.open(&recording.id) {
            Ok(_) | Err(SpoolError::Locked) => return None,
            Err(_) => {}
        }
        if recording.final_sample_count.is_none() {
            if let Err(error) = self
                .api
                .recover(
                    &recording.id,
                    recovery.expose(),
                    &request.client_id,
                    recording.durable_offset,
                )
                .await
            {
                return Some(format!(
                    "the create replayed an interrupted recording and recovery failed: {error:?}"
                ));
            }
        }
        None
    }

    async fn execute_put(
        &mut self,
        now: Duration,
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
        sequence: u64,
        start: u64,
        bytes: Vec<u8>,
        sha256: String,
    ) {
        let samples = (bytes.len() / 2) as u64;
        let Some(spool) = self.spool.as_mut() else {
            self.controller.handle(
                now,
                RecorderEvent::ChunkFailed {
                    generation,
                    sequence,
                    error: RecorderTransportError::Permanent(
                        "no durable spool is attached to this capture".into(),
                    ),
                },
            );
            return;
        };
        if let Err(error) = spool.append_chunk(sequence, start, samples, &bytes, &sha256) {
            let error = chunk_spool_error(error);
            self.controller.handle(
                now,
                RecorderEvent::ChunkFailed {
                    generation,
                    sequence,
                    error,
                },
            );
            return;
        }

        let result = self
            .api
            .put_chunk(
                &recording_id,
                sequence,
                start,
                &lease,
                &stream_id,
                &sha256,
                &bytes,
            )
            .await;
        match result {
            Ok(ack) => {
                if let Some(spool) = self.spool.as_mut() {
                    if let Err(error) = spool.acknowledge(ack.durable_offset, ack.next_sequence) {
                        self.warn(format!(
                            "Core acknowledged a chunk but the spool checkpoint could not advance: {error}"
                        ));
                    }
                }
                self.controller.handle(
                    now,
                    RecorderEvent::ChunkAcked {
                        generation,
                        sequence,
                        durable_offset: ack.durable_offset,
                    },
                );
            }
            Err(error) => {
                if matches!(error, RecorderTransportError::LeaseLost(_)) {
                    self.mark_spool_interrupted();
                }
                self.controller.handle(
                    now,
                    RecorderEvent::ChunkFailed {
                        generation,
                        sequence,
                        error,
                    },
                );
            }
        }
    }

    async fn execute_heartbeat(
        &mut self,
        now: Duration,
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
    ) {
        match self.api.heartbeat(&recording_id, &lease, &stream_id).await {
            Ok(()) => self
                .controller
                .handle(now, RecorderEvent::HeartbeatOk { generation }),
            Err(error) => {
                if matches!(error, RecorderTransportError::LeaseLost(_)) {
                    self.mark_spool_interrupted();
                }
                self.controller
                    .handle(now, RecorderEvent::HeartbeatFailed { generation, error });
            }
        }
    }

    async fn execute_stop(
        &mut self,
        now: Duration,
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
        final_sample_count: u64,
    ) {
        match self
            .api
            .stop(&recording_id, &lease, &stream_id, final_sample_count)
            .await
        {
            Ok(recording) => {
                let final_count = recording.final_sample_count.unwrap_or(final_sample_count);
                if let Some(spool) = self.spool.as_mut() {
                    if let Err(error) = spool.mark_stopped(final_count) {
                        self.warn(format!(
                            "Core stopped the recording but the spool state could not be updated: {error}"
                        ));
                    }
                }
                self.controller.handle(
                    now,
                    RecorderEvent::Stopped {
                        generation,
                        recording,
                    },
                );
            }
            Err(error) => {
                if matches!(error, RecorderTransportError::LeaseLost(_)) {
                    self.mark_spool_interrupted();
                }
                self.controller
                    .handle(now, RecorderEvent::StopFailed { generation, error });
            }
        }
    }

    async fn execute_rebind(
        &mut self,
        now: Duration,
        generation: u64,
        recording_id: String,
        lease: String,
        stream_id: String,
        client_id: String,
    ) {
        match self
            .api
            .rebind(&recording_id, &lease, &stream_id, &client_id)
            .await
        {
            Ok(()) => self.controller.handle(
                now,
                RecorderEvent::Rebound {
                    generation,
                    stream_id,
                },
            ),
            Err(error) => {
                if matches!(error, RecorderTransportError::LeaseLost(_)) {
                    self.mark_spool_interrupted();
                }
                self.controller
                    .handle(now, RecorderEvent::RebindFailed { generation, error });
            }
        }
    }

    fn mark_spool_interrupted(&mut self) {
        let Some(spool) = self.spool.as_mut() else {
            return;
        };
        if matches!(
            spool.meta().state,
            SpoolState::Stopped | SpoolState::Recovered
        ) {
            return;
        }
        let accepted = spool.meta().accepted_samples;
        let received = self.controller.snapshot().received_samples;
        if let Err(error) = spool.mark_interrupted(Some(accepted), Some(received)) {
            self.warn(format!(
                "could not persist the interrupted spool state: {error}"
            ));
        }
    }

    fn warn(&mut self, message: impl Into<String>) {
        if self.warnings.len() >= MAX_WARNINGS {
            self.warnings.remove(0);
        }
        self.warnings.push(message.into());
    }

    /// Recover every interrupted spool, bounded to [`MAX_RECOVERY_SESSIONS`].
    /// A plan with a gap/conflict is reported and not uploaded, never hidden.
    pub async fn recover_pending(&mut self) -> Result<RecoveryReport, DriverError> {
        let summaries = self.spool_store.list()?;
        let mut report = RecoveryReport {
            truncated: summaries.len() > MAX_RECOVERY_SESSIONS,
            ..RecoveryReport::default()
        };
        for summary in summaries.into_iter().take(MAX_RECOVERY_SESSIONS) {
            if summary.app == Some(SpoolApp::Nsh) {
                continue;
            }
            if summary.error.is_some() {
                report.attempted += 1;
                report.issues.push(RecoveryIssue {
                    recording_id: Some(summary.id),
                    kind: RecoveryIssueKind::Spool,
                    detail: summary.error.unwrap_or_default(),
                });
                continue;
            }
            if summary.state == Some(SpoolState::Recovered) {
                continue;
            }
            report.attempted += 1;
            match self.recover_one(&summary.id).await {
                Ok(RecoverOne::Recovered) => report.recovered += 1,
                Ok(RecoverOne::Issue(issue)) => report.issues.push(issue),
                Err(DriverError::Spool(SpoolError::Locked)) => {
                    // A live capture (this client or another) owns the spool.
                    report.attempted = report.attempted.saturating_sub(1);
                }
                Err(DriverError::Spool(error)) => report.issues.push(RecoveryIssue {
                    recording_id: Some(summary.id),
                    kind: RecoveryIssueKind::Spool,
                    detail: error.to_string(),
                }),
                Err(error) => report.issues.push(RecoveryIssue {
                    recording_id: Some(summary.id),
                    kind: RecoveryIssueKind::Api,
                    detail: error.to_string(),
                }),
            }
        }
        Ok(report)
    }

    /// Clean up create intents persisted before a POST that may or may not have
    /// committed. Uses Core's lookup-only resume; never creates and never reuses K.
    pub async fn recover_pending_starts(&mut self) -> Result<RecoveryReport, DriverError> {
        let keys = self.spool_store.pending_start_ids()?;
        let mut report = RecoveryReport {
            truncated: keys.len() > MAX_RECOVERY_SESSIONS,
            ..RecoveryReport::default()
        };
        for key in keys.into_iter().take(MAX_RECOVERY_SESSIONS) {
            let Some(pending) = self.spool_store.open_pending_start(&key)? else {
                continue;
            };
            if pending.record().app != SpoolApp::Tui {
                continue;
            }
            report.attempted += 1;
            match self.recover_one_start(pending).await {
                Ok(RecoverOne::Recovered) => report.recovered += 1,
                Ok(RecoverOne::Issue(issue)) => report.issues.push(issue),
                Err(DriverError::Spool(error)) => report.issues.push(RecoveryIssue {
                    recording_id: None,
                    kind: RecoveryIssueKind::Spool,
                    detail: error.to_string(),
                }),
                Err(error) => report.issues.push(RecoveryIssue {
                    recording_id: None,
                    kind: RecoveryIssueKind::Api,
                    detail: error.to_string(),
                }),
            }
        }
        Ok(report)
    }

    async fn recover_one_start(
        &mut self,
        pending: crate::recorder_spool::PendingStartGuard,
    ) -> Result<RecoverOne, DriverError> {
        let record = pending.record().clone();
        let snapshot = match self
            .api
            .resume(
                &record.client_id,
                &record.key,
                record.recovery.expose(),
                record.title.as_deref(),
            )
            .await
        {
            Ok(snapshot) => snapshot,
            Err(RecorderTransportError::CreateNotFound) => {
                // Nothing was created; Core has now withdrawn K. Delete only.
                pending.finish()?;
                return Ok(RecoverOne::Recovered);
            }
            Err(RecorderTransportError::CreateWithdrawn) => {
                // Another create/resume owns K and no recording was made by this
                // intent. Delete it; K must never be reused.
                pending.finish()?;
                return Ok(RecoverOne::Recovered);
            }
            Err(RecorderTransportError::CreateGuardFull) => {
                return Ok(RecoverOne::Issue(RecoveryIssue {
                    recording_id: None,
                    kind: RecoveryIssueKind::CoreRestartRequired,
                    detail:
                        "Core's recorder create guard is full; restart Core before creating another recording"
                            .into(),
                }));
            }
            Err(error) => {
                return Ok(RecoverOne::Issue(RecoveryIssue {
                    recording_id: None,
                    kind: RecoveryIssueKind::Api,
                    detail: error.message(),
                }));
            }
        };

        let id = &snapshot.recording.id;
        match self.spool_store.open(id) {
            // A crash after the spool was created: normal spool recovery owns it.
            Ok(_) | Err(SpoolError::Locked) => {
                pending.finish()?;
                return Ok(RecoverOne::Recovered);
            }
            Err(_) => {}
        }
        let result = if snapshot.active {
            match snapshot.recording.lease.as_deref() {
                Some(lease) => self
                    .api
                    .stop(id, lease, "", snapshot.recording.durable_offset)
                    .await
                    .map(|_| ()),
                None => Err(RecorderTransportError::Permanent(
                    "active recording has no lease to stop".into(),
                )),
            }
        } else if snapshot.recording.final_sample_count.is_none() {
            self.api
                .recover(
                    id,
                    record.recovery.expose(),
                    &record.client_id,
                    snapshot.recording.durable_offset,
                )
                .await
        } else {
            Ok(())
        };
        match result {
            Ok(()) => {
                pending.finish()?;
                Ok(RecoverOne::Recovered)
            }
            Err(error) => Ok(RecoverOne::Issue(RecoveryIssue {
                recording_id: Some(id.clone()),
                kind: RecoveryIssueKind::Api,
                detail: error.message(),
            })),
        }
    }

    async fn recover_one(&mut self, recording_id: &str) -> Result<RecoverOne, DriverError> {
        let mut spool = self.spool_store.open(recording_id)?;
        let snapshot = match self.api.get(recording_id).await {
            Ok(snapshot) => snapshot,
            Err(error) => {
                let kind = match &error {
                    RecorderTransportError::LeaseLost(_) => RecoveryIssueKind::MissingRecording,
                    _ => RecoveryIssueKind::Api,
                };
                return Ok(RecoverOne::Issue(RecoveryIssue {
                    recording_id: Some(recording_id.to_owned()),
                    kind,
                    detail: error.message(),
                }));
            }
        };
        if snapshot.active {
            return Ok(RecoverOne::Issue(RecoveryIssue {
                recording_id: Some(recording_id.to_owned()),
                kind: RecoveryIssueKind::Active,
                detail: "the recording is still active".into(),
            }));
        }
        let baseline = ServerBaseline {
            durable_offset: snapshot.recording.durable_offset,
            next_sequence: snapshot.recording.chunks,
            final_sample_count: snapshot.recording.final_sample_count,
            sealed: snapshot.sealed,
        };
        let plan = spool.reconcile(Some(baseline))?;
        if !plan.conflicts.is_empty() {
            return Ok(RecoverOne::Issue(RecoveryIssue {
                recording_id: Some(recording_id.to_owned()),
                kind: RecoveryIssueKind::Conflict,
                detail: plan
                    .conflicts
                    .iter()
                    .map(|conflict| conflict.detail.clone())
                    .collect::<Vec<_>>()
                    .join("; "),
            }));
        }
        if !plan.gaps.is_empty() {
            return Ok(RecoverOne::Issue(RecoveryIssue {
                recording_id: Some(recording_id.to_owned()),
                kind: RecoveryIssueKind::Gap,
                detail: plan
                    .gaps
                    .iter()
                    .map(|gap| format!("gap at {} ({} samples)", gap.start, gap.samples))
                    .collect::<Vec<_>>()
                    .join("; "),
            }));
        }
        for action in plan.actions() {
            let result = match action {
                RecoveryAction::Seal {
                    recording_id,
                    client_id,
                    recovery_token,
                    final_sample_count,
                } => {
                    self.api
                        .recover(
                            &recording_id,
                            recovery_token.expose(),
                            &client_id,
                            final_sample_count,
                        )
                        .await
                }
                RecoveryAction::Upload {
                    recording_id,
                    client_id,
                    recovery_token,
                    sequence,
                    start,
                    sha256,
                    bytes,
                    ..
                } => {
                    self.api
                        .recover_chunk(
                            &recording_id,
                            sequence,
                            start,
                            recovery_token.expose(),
                            &client_id,
                            &sha256,
                            &bytes,
                        )
                        .await
                }
            };
            if let Err(error) = result {
                return Ok(RecoverOne::Issue(RecoveryIssue {
                    recording_id: Some(recording_id.to_owned()),
                    kind: RecoveryIssueKind::Api,
                    detail: error.message(),
                }));
            }
        }
        spool.mark_recovered()?;
        Ok(RecoverOne::Recovered)
    }
}

fn api_error(error: TransportError) -> RecorderTransportError {
    match error {
        TransportError::Network(message) => RecorderTransportError::Transient(message),
        TransportError::Decode(message) => {
            RecorderTransportError::Permanent(format!("unexpected recorder response: {message}"))
        }
        TransportError::Api(error) => {
            let code = error.code.as_deref().unwrap_or_default();
            let durable_offset = error.body.get("durable_offset").and_then(Value::as_u64);
            match code {
                "recorder_busy" | "recording_busy" => RecorderTransportError::Busy(error.message),
                "recovery_required" => RecorderTransportError::Permanent(error.message),
                "create_not_found" => RecorderTransportError::CreateNotFound,
                "create_withdrawn" => RecorderTransportError::CreateWithdrawn,
                "create_guard_full" => RecorderTransportError::CreateGuardFull,
                "lease_expired" | "invalid_lease" => {
                    RecorderTransportError::LeaseLost(error.message)
                }
                "stream_detached" => RecorderTransportError::StreamDetached(error.message),
                "recording_sealed" => RecorderTransportError::Sealed { durable_offset },
                "chunk_conflict" | "noncontiguous" | "beyond_sealed" | "final_count_conflict" => {
                    RecorderTransportError::Conflict {
                        code: code.to_owned(),
                        message: error.message,
                        durable_offset,
                    }
                }
                "recorder_backpressure" => RecorderTransportError::Transient(error.message),
                "not_owner" => RecorderTransportError::Auth(error.message),
                _ if error.status == 401 || error.status == 403 => {
                    RecorderTransportError::Auth(error.message)
                }
                _ if error.status >= 500 => RecorderTransportError::Transient(error.message),
                _ if error.status == 400 || error.status == 409 => {
                    RecorderTransportError::Permanent(error.message)
                }
                _ => RecorderTransportError::Permanent(error.message),
            }
        }
    }
}

fn chunk_spool_error(error: SpoolError) -> RecorderTransportError {
    match error {
        SpoolError::Io { .. } => RecorderTransportError::Transient(format!("spool: {error}")),
        other => RecorderTransportError::Permanent(format!("spool: {other}")),
    }
}

/// K is a UUIDv4 lookup key. R is a full 32-byte OS-CSPRNG capability filled by
/// `getrandom` 0.4.3 (getentropy/getrandom/arc4random as appropriate), encoded as
/// 43 base64url characters. Core stores only sha256(R).
fn new_start_ids() -> Result<(String, Secret), String> {
    let key = uuid::Uuid::new_v4().simple().to_string();
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|error| {
        format!("could not obtain the recovery capability from the OS CSPRNG: {error}")
    })?;
    let recovery = base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, bytes);
    Ok((key, Secret::new(recovery)))
}

fn state_object(value: &Value) -> &Value {
    value.get("recording").unwrap_or(value)
}

fn recording_from_value(value: &Value) -> ApiResult<RecordingState> {
    let state = state_object(value);
    let id = state
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| RecorderTransportError::Permanent("recording response has no id".into()))?;
    Ok(RecordingState {
        id: id.to_owned(),
        generation: state.get("generation").and_then(Value::as_u64).unwrap_or(1),
        lease: state
            .get("lease_token")
            .or_else(|| state.get("lease"))
            .and_then(Value::as_str)
            .map(str::to_owned),
        recovery_token: state
            .get("recovery_token")
            .and_then(Value::as_str)
            .map(str::to_owned),
        durable_offset: state
            .get("durable_offset")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        chunks: state.get("chunks").and_then(Value::as_u64).unwrap_or(0),
        capture_state: state
            .get("capture_state")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_owned(),
        final_sample_count: state.get("final_sample_count").and_then(Value::as_u64),
        free_seconds: value
            .get("free_seconds")
            .and_then(Value::as_f64)
            .map(|seconds| seconds.max(0.0) as u64),
    })
}

fn chunk_ack_from_value(value: &Value, sequence: u64) -> ApiResult<ChunkAck> {
    let state = state_object(value);
    let durable_offset = state
        .get("durable_offset")
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            RecorderTransportError::Permanent("chunk response has no durable offset".into())
        })?;
    let next_sequence = state
        .get("chunks")
        .and_then(Value::as_u64)
        .unwrap_or_else(|| sequence.saturating_add(1));
    Ok(ChunkAck {
        durable_offset,
        next_sequence,
    })
}

fn snapshot_from_value(value: &Value) -> ApiResult<RecordingSnapshot> {
    let recording = recording_from_value(value)?;
    let state = state_object(value);
    let active = state
        .get("active")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let sealed = !active && recording.final_sample_count.is_some();
    Ok(RecordingSnapshot {
        recording,
        active,
        sealed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::CoreApi;
    use crate::recorder::{CaptureEvent, CaptureSource, RecorderConfig};
    use crate::recorder_spool::{Secret, SpoolMeta, SpoolStore};
    use crate::transport::Http;
    use std::collections::{BTreeMap, VecDeque};
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::Semaphore;

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
            self.0.lock().unwrap().events.push_back(CaptureEvent::Audio(
                crate::recorder::AudioBlock {
                    start_sample,
                    samples,
                },
            ));
        }

        fn starts(&self) -> usize {
            self.0.lock().unwrap().starts
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
            state.events.push_back(CaptureEvent::Finished);
        }

        fn drain(&mut self) -> Vec<CaptureEvent> {
            self.0.lock().unwrap().events.drain(..).collect()
        }
    }

    struct TempRoot(std::path::PathBuf);

    impl TempRoot {
        fn new(name: &str) -> Self {
            let mut path = std::env::temp_dir();
            path.push(format!(
                "nexus-recorder-driver-test-{}-{name}",
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for TempRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn recording(id: &str, lease: &str) -> RecordingState {
        RecordingState {
            id: id.into(),
            generation: 1,
            lease: Some(lease.into()),
            recovery_token: Some("recovery-token-0123456789".into()),
            durable_offset: 0,
            chunks: 0,
            capture_state: "recording".into(),
            final_sample_count: None,
            free_seconds: None,
        }
    }

    #[derive(Clone)]
    struct FakeApi(Arc<Mutex<FakeState>>);

    struct FakeState {
        created: RecordingState,
        snapshot: RecordingSnapshot,
        create_calls: usize,
        put_calls: Vec<(u64, u64, String, usize)>,
        put_error: Option<RecorderTransportError>,
        heartbeat_error: Option<RecorderTransportError>,
        stop_calls: Vec<u64>,
        stop_error: Option<RecorderTransportError>,
        rebind_calls: Vec<String>,
        rebind_error: Option<RecorderTransportError>,
        recover_calls: Vec<(String, u64)>,
        recover_chunk_calls: Vec<(u64, u64)>,
        put_entered: Option<Arc<Semaphore>>,
        put_release: Option<Arc<Semaphore>>,
        create_recoveries: Vec<String>,
        create_invocations: Vec<(String, String)>,
        create_errors: VecDeque<RecorderTransportError>,
        create_entered: Option<Arc<Semaphore>>,
        create_release: Option<Arc<Semaphore>>,
        resume_result: Option<ApiResult<RecordingSnapshot>>,
        resume_calls: usize,
        resume_invocations: Vec<(String, String)>,
    }

    impl FakeApi {
        fn new(created: RecordingState) -> Self {
            let snapshot = RecordingSnapshot {
                recording: created.clone(),
                active: false,
                sealed: false,
            };
            Self(Arc::new(Mutex::new(FakeState {
                created,
                snapshot,
                create_calls: 0,
                put_calls: Vec::new(),
                put_error: None,
                heartbeat_error: None,
                stop_calls: Vec::new(),
                stop_error: None,
                rebind_calls: Vec::new(),
                rebind_error: None,
                recover_calls: Vec::new(),
                recover_chunk_calls: Vec::new(),
                put_entered: None,
                put_release: None,
                create_recoveries: Vec::new(),
                create_invocations: Vec::new(),
                create_errors: VecDeque::new(),
                create_entered: None,
                create_release: None,
                resume_result: None,
                resume_calls: 0,
                resume_invocations: Vec::new(),
            })))
        }

        fn set_create_gate(&self, entered: Arc<Semaphore>, release: Arc<Semaphore>) {
            let mut state = self.0.lock().unwrap();
            state.create_entered = Some(entered);
            state.create_release = Some(release);
        }

        fn push_create_error(&self, error: RecorderTransportError) {
            self.0.lock().unwrap().create_errors.push_back(error);
        }

        fn create_calls(&self) -> usize {
            self.0.lock().unwrap().create_calls
        }

        fn set_resume_result(&self, result: ApiResult<RecordingSnapshot>) {
            self.0.lock().unwrap().resume_result = Some(result);
        }

        fn create_recoveries(&self) -> Vec<String> {
            self.0.lock().unwrap().create_recoveries.clone()
        }

        fn create_invocations(&self) -> Vec<(String, String)> {
            self.0.lock().unwrap().create_invocations.clone()
        }

        fn resume_calls(&self) -> usize {
            self.0.lock().unwrap().resume_calls
        }

        fn resume_invocations(&self) -> Vec<(String, String)> {
            self.0.lock().unwrap().resume_invocations.clone()
        }

        fn set_put_error(&self, error: RecorderTransportError) {
            self.0.lock().unwrap().put_error = Some(error);
        }

        fn set_put_gate(&self, entered: Arc<Semaphore>, release: Arc<Semaphore>) {
            let mut state = self.0.lock().unwrap();
            state.put_entered = Some(entered);
            state.put_release = Some(release);
        }

        fn clear_put_gate(&self) {
            let mut state = self.0.lock().unwrap();
            state.put_entered = None;
            state.put_release = None;
        }

        fn set_heartbeat_error(&self, error: RecorderTransportError) {
            self.0.lock().unwrap().heartbeat_error = Some(error);
        }

        fn set_stop_error(&self, error: RecorderTransportError) {
            self.0.lock().unwrap().stop_error = Some(error);
        }

        fn puts(&self) -> Vec<(u64, u64, String, usize)> {
            self.0.lock().unwrap().put_calls.clone()
        }

        fn stop_calls(&self) -> Vec<u64> {
            self.0.lock().unwrap().stop_calls.clone()
        }

        fn rebind_calls(&self) -> Vec<String> {
            self.0.lock().unwrap().rebind_calls.clone()
        }

        fn recovery_calls(&self) -> (Vec<(String, u64)>, Vec<(u64, u64)>) {
            let state = self.0.lock().unwrap();
            (
                state.recover_calls.clone(),
                state.recover_chunk_calls.clone(),
            )
        }
    }

    impl RecorderApi for FakeApi {
        fn create<'a>(
            &'a self,
            _request: &'a StartRequest,
            idempotency_key: &'a str,
            recovery: &'a str,
        ) -> BoxFuture<'a, ApiResult<RecordingState>> {
            Box::pin(async move {
                let (entered, release) = {
                    let mut state = self.0.lock().unwrap();
                    state.create_calls += 1;
                    state.create_recoveries.push(recovery.to_owned());
                    state
                        .create_invocations
                        .push((idempotency_key.to_owned(), recovery.to_owned()));
                    (state.create_entered.clone(), state.create_release.clone())
                };
                if let Some(entered) = entered {
                    entered.add_permits(1);
                }
                if let Some(release) = release {
                    let _ = release.acquire().await;
                }
                let (error, created) = {
                    let mut state = self.0.lock().unwrap();
                    (state.create_errors.pop_front(), state.created.clone())
                };
                if let Some(error) = error {
                    return Err(error);
                }
                Ok(created)
            })
        }

        fn resume<'a>(
            &'a self,
            _client_id: &'a str,
            idempotency_key: &'a str,
            recovery: &'a str,
            _title: Option<&'a str>,
        ) -> BoxFuture<'a, ApiResult<RecordingSnapshot>> {
            Box::pin(async move {
                let mut state = self.0.lock().unwrap();
                state.resume_calls += 1;
                state
                    .resume_invocations
                    .push((idempotency_key.to_owned(), recovery.to_owned()));
                state
                    .resume_result
                    .clone()
                    .unwrap_or_else(|| Ok(state.snapshot.clone()))
            })
        }

        fn put_chunk<'a>(
            &'a self,
            _recording_id: &'a str,
            sequence: u64,
            start: u64,
            _lease: &'a str,
            _stream_id: &'a str,
            sha256: &'a str,
            body: &'a [u8],
        ) -> BoxFuture<'a, ApiResult<ChunkAck>> {
            Box::pin(async move {
                let (entered, release) = {
                    let mut state = self.0.lock().unwrap();
                    state
                        .put_calls
                        .push((sequence, start, sha256.to_owned(), body.len()));
                    (state.put_entered.clone(), state.put_release.clone())
                };
                if let Some(entered) = entered {
                    entered.add_permits(1);
                }
                if let Some(release) = release {
                    let _ = release.acquire().await;
                }
                let state = self.0.lock().unwrap();
                if let Some(error) = state.put_error.clone() {
                    return Err(error);
                }
                let samples = (body.len() / 2) as u64;
                Ok(ChunkAck {
                    durable_offset: start.saturating_add(samples),
                    next_sequence: sequence.saturating_add(1),
                })
            })
        }

        fn heartbeat<'a>(
            &'a self,
            _recording_id: &'a str,
            _lease: &'a str,
            _stream_id: &'a str,
        ) -> BoxFuture<'a, ApiResult<()>> {
            Box::pin(async move {
                self.0
                    .lock()
                    .unwrap()
                    .heartbeat_error
                    .clone()
                    .map_or(Ok(()), Err)
            })
        }

        fn rebind<'a>(
            &'a self,
            _recording_id: &'a str,
            _lease: &'a str,
            stream_id: &'a str,
            _client_id: &'a str,
        ) -> BoxFuture<'a, ApiResult<()>> {
            Box::pin(async move {
                let mut state = self.0.lock().unwrap();
                state.rebind_calls.push(stream_id.to_owned());
                state.rebind_error.clone().map_or(Ok(()), Err)
            })
        }

        fn stop<'a>(
            &'a self,
            _recording_id: &'a str,
            _lease: &'a str,
            _stream_id: &'a str,
            final_sample_count: u64,
        ) -> BoxFuture<'a, ApiResult<RecordingState>> {
            Box::pin(async move {
                let mut state = self.0.lock().unwrap();
                state.stop_calls.push(final_sample_count);
                if let Some(error) = state.stop_error.clone() {
                    return Err(error);
                }
                let mut recording = state.created.clone();
                recording.lease = None;
                recording.capture_state = "stopped".into();
                recording.final_sample_count = Some(final_sample_count);
                recording.durable_offset = final_sample_count;
                Ok(recording)
            })
        }

        fn get<'a>(
            &'a self,
            _recording_id: &'a str,
        ) -> BoxFuture<'a, ApiResult<RecordingSnapshot>> {
            Box::pin(async move { Ok(self.0.lock().unwrap().snapshot.clone()) })
        }

        fn recover<'a>(
            &'a self,
            recording_id: &'a str,
            _recovery_token: &'a str,
            _client_id: &'a str,
            final_sample_count: u64,
        ) -> BoxFuture<'a, ApiResult<()>> {
            Box::pin(async move {
                self.0
                    .lock()
                    .unwrap()
                    .recover_calls
                    .push((recording_id.to_owned(), final_sample_count));
                Ok(())
            })
        }

        fn recover_chunk<'a>(
            &'a self,
            _recording_id: &'a str,
            sequence: u64,
            start: u64,
            _recovery_token: &'a str,
            _client_id: &'a str,
            _sha256: &'a str,
            _body: &'a [u8],
        ) -> BoxFuture<'a, ApiResult<()>> {
            Box::pin(async move {
                self.0
                    .lock()
                    .unwrap()
                    .recover_chunk_calls
                    .push((sequence, start));
                Ok(())
            })
        }
    }

    fn driver(
        root: &TempRoot,
        created: RecordingState,
    ) -> (RecorderDriver<FakeApi>, FakeApi, MockCapture) {
        let mock = MockCapture::default();
        let controller = RecorderController::new(RecorderConfig::default(), Box::new(mock.clone()));
        let api = FakeApi::new(created);
        let store = SpoolStore::new(&root.0).unwrap();
        (
            RecorderDriver::new(controller, api.clone(), store),
            api,
            mock,
        )
    }

    fn start_request() -> StartRequest {
        StartRequest::new("stream-1", "tui", "create-key-1")
    }

    #[tokio::test]
    async fn create_persists_spool_before_opening_the_microphone() {
        let root = TempRoot::new("create");
        let (mut driver, _api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        driver.start(Duration::ZERO, start_request()).unwrap();
        assert!(driver.step(Duration::ZERO).await.unwrap());

        assert_eq!(mock.starts(), 1);
        assert_eq!(driver.state(), CaptureState::Recording);
        let meta = driver.spool_meta().unwrap();
        assert_eq!(meta.id, "0123456789abcdef0123456789abcdef");
        assert!(!format!("{:?}", meta).contains(meta.recovery_token.expose()));
    }

    #[tokio::test]
    async fn chunk_is_spooled_then_sent_and_acknowledged() {
        let root = TempRoot::new("chunk");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();

        let bytes = vec![0x11; 16_000 * 2];
        mock.emit_audio(0, vec![1; 16_000]);
        driver.step(Duration::from_millis(10)).await.unwrap();

        let puts = api.puts();
        assert_eq!(puts.len(), 1);
        assert_eq!(puts[0].0, 0);
        assert_eq!(puts[0].1, 0);
        assert_eq!(puts[0].3, bytes.len());
        assert_eq!(driver.spool_meta().unwrap().pending_samples(), 0);
        assert_eq!(driver.snapshot().durable_offset, 16_000);
    }

    #[tokio::test]
    async fn lease_loss_stops_capture_and_marks_the_spool_interrupted() {
        let root = TempRoot::new("lease");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        api.set_put_error(RecorderTransportError::LeaseLost("lease_expired".into()));
        mock.emit_audio(0, vec![1; 16_000]);
        driver.step(Duration::from_millis(10)).await.unwrap();

        assert_eq!(driver.state(), CaptureState::Interrupted);
        assert_eq!(driver.spool_meta().unwrap().state, SpoolState::Interrupted);
        assert_eq!(driver.spool_meta().unwrap().pending_samples(), 16_000);
        assert!(matches!(
            driver.stop(Duration::from_millis(20)),
            Err(StopError::LeaseLost)
        ));
    }

    #[tokio::test]
    async fn stop_is_sent_and_marks_the_spool_stopped() {
        let root = TempRoot::new("stop");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        mock.emit_audio(0, vec![1; 16_000]);
        driver.step(Duration::from_millis(10)).await.unwrap();

        driver.stop(Duration::from_millis(20)).unwrap();
        for now in [20, 30, 40] {
            driver.step(Duration::from_millis(now)).await.unwrap();
        }
        assert_eq!(api.stop_calls(), vec![16_000]);
        assert_eq!(driver.state(), CaptureState::Stopped);
        assert_eq!(driver.spool_meta().unwrap().state, SpoolState::Stopped);
    }

    #[tokio::test]
    async fn stream_detached_rebinds_then_seals() {
        let root = TempRoot::new("rebind");
        let (mut driver, api, _mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        api.set_heartbeat_error(RecorderTransportError::StreamDetached(
            "stream_detached".into(),
        ));
        driver.step(Duration::from_secs(5)).await.unwrap();
        assert!(driver.snapshot().rebind_required);

        driver.rebind("stream-2").unwrap();
        driver.step(Duration::from_secs(5)).await.unwrap();
        assert_eq!(api.rebind_calls(), vec!["stream-2".to_owned()]);
        for now in [5, 6, 7] {
            driver.step(Duration::from_secs(now)).await.unwrap();
        }
        assert!(!api.stop_calls().is_empty());
    }

    #[tokio::test]
    async fn recovery_seals_then_uploads_and_marks_the_spool_recovered() {
        let root = TempRoot::new("recovery");
        let id = "0123456789abcdef0123456789abcdef";
        let (mut first, first_api, mock) = driver(&root, recording(id, "lease-1"));
        first.start(Duration::ZERO, start_request()).unwrap();
        first.step(Duration::ZERO).await.unwrap();
        first_api.set_put_error(RecorderTransportError::LeaseLost("lease_expired".into()));
        mock.emit_audio(0, vec![1; 16_000]);
        first.step(Duration::from_millis(10)).await.unwrap();
        drop(first);

        let mut recovered = recording(id, "lease-1");
        recovered.lease = None;
        let fake = FakeApi::new(recovered);
        fake.0.lock().unwrap().snapshot = RecordingSnapshot {
            recording: RecordingState {
                lease: None,
                recovery_token: None,
                durable_offset: 0,
                chunks: 0,
                capture_state: "interrupted".into(),
                final_sample_count: None,
                free_seconds: None,
                ..recording(id, "")
            },
            active: false,
            sealed: false,
        };
        let store = SpoolStore::new(&root.0).unwrap();
        let controller =
            RecorderController::new(RecorderConfig::default(), Box::new(MockCapture::default()));
        let mut driver = RecorderDriver::new(controller, fake.clone(), store);
        let report = driver.recover_pending().await.unwrap();
        assert_eq!(report.recovered, 1, "{report:?}");
        assert!(report.issues.is_empty(), "{report:?}");
        let (recoveries, chunks) = fake.recovery_calls();
        assert_eq!(recoveries.len(), 1);
        assert_eq!(recoveries[0].1, 16_000);
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0], (0, 0));
        assert!(!format!("{report:?}").contains("recovery-token-0123456789"));
        let reopened = SpoolStore::new(&root.0).unwrap().open(id).unwrap();
        assert_eq!(reopened.meta().state, SpoolState::Recovered);
    }

    #[tokio::test]
    async fn spool_resumes_from_an_active_replay_checkpoint() {
        let root = TempRoot::new("replay");
        let mut created = recording("0123456789abcdef0123456789abcdef", "lease-1");
        created.durable_offset = 16_000;
        created.chunks = 1;
        let (mut driver, api, mock) = driver(&root, created);
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        assert_eq!(driver.spool_meta().unwrap().durable_offset, 16_000);
        assert_eq!(driver.spool_meta().unwrap().next_sequence, 1);
        assert_eq!(driver.spool_meta().unwrap().accepted_samples, 16_000);

        mock.emit_audio(16_000, vec![1; 16_000]);
        driver.step(Duration::from_millis(10)).await.unwrap();
        let puts = api.puts();
        assert_eq!(puts.len(), 1);
        assert_eq!(puts[0].0, 1);
        assert_eq!(puts[0].1, 16_000);
    }

    #[tokio::test]
    async fn recovery_conflict_is_reported_and_nothing_is_uploaded() {
        let root = TempRoot::new("recovery-conflict");
        let id = "0123456789abcdef0123456789abcdef";
        let (mut first, first_api, mock) = driver(&root, recording(id, "lease-1"));
        first.start(Duration::ZERO, start_request()).unwrap();
        first.step(Duration::ZERO).await.unwrap();
        first_api.set_put_error(RecorderTransportError::LeaseLost("lease_expired".into()));
        mock.emit_audio(0, vec![1; 16_000]);
        first.step(Duration::from_millis(10)).await.unwrap();
        drop(first);

        let mut recovered = recording(id, "lease-1");
        recovered.lease = None;
        let fake = FakeApi::new(recovered);
        fake.0.lock().unwrap().snapshot = RecordingSnapshot {
            recording: RecordingState {
                lease: None,
                recovery_token: None,
                durable_offset: 8_000,
                chunks: 0,
                capture_state: "interrupted".into(),
                final_sample_count: None,
                free_seconds: None,
                ..recording(id, "")
            },
            active: false,
            sealed: false,
        };
        let store = SpoolStore::new(&root.0).unwrap();
        let controller =
            RecorderController::new(RecorderConfig::default(), Box::new(MockCapture::default()));
        let mut driver = RecorderDriver::new(controller, fake.clone(), store);
        let report = driver.recover_pending().await.unwrap();
        assert_eq!(report.recovered, 0);
        assert!(report
            .issues
            .iter()
            .any(|issue| issue.kind == RecoveryIssueKind::Conflict));
        let (recoveries, chunks) = fake.recovery_calls();
        assert!(recoveries.is_empty());
        assert!(chunks.is_empty());
    }

    #[tokio::test]
    async fn spool_create_failure_surfaces_cleanup_stop_failure() {
        let root = TempRoot::new("create-spool-failure");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        // Make the spool creation fail before the driver returns Created.
        std::fs::create_dir_all(
            root.0
                .join("recorder-spool")
                .join("0123456789abcdef0123456789abcdef"),
        )
        .unwrap();
        api.set_stop_error(RecorderTransportError::Transient("network down".into()));
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();

        assert_eq!(mock.starts(), 0, "the microphone must never open");
        assert_eq!(driver.state(), CaptureState::Idle);
        let error = driver.snapshot().last_error.unwrap_or_default();
        assert!(error.contains("cleanup stop also failed"), "{error}");
    }

    fn pending_key() -> &'static str {
        "0123456789abcdef0123456789abcdef"
    }

    fn pending_recovery() -> Secret {
        Secret::new("client-recovery-capability-0123456789abcdef")
    }

    fn leave_pending(root: &TempRoot) {
        let store = SpoolStore::new(&root.0).unwrap();
        let guard = store
            .begin_pending_start(pending_key(), pending_recovery(), "tui", None)
            .unwrap();
        drop(guard);
    }

    fn recovery_driver(root: &TempRoot, fake: FakeApi) -> RecorderDriver<FakeApi> {
        recovery_driver_with_capture(root, fake).0
    }

    fn recovery_driver_with_capture(
        root: &TempRoot,
        fake: FakeApi,
    ) -> (RecorderDriver<FakeApi>, MockCapture) {
        let store = SpoolStore::new(&root.0).unwrap();
        let mock = MockCapture::default();
        let controller = RecorderController::new(RecorderConfig::default(), Box::new(mock.clone()));
        (RecorderDriver::new(controller, fake, store), mock)
    }

    #[tokio::test]
    async fn create_transient_retry_reuses_the_same_key_and_recovery_capability() {
        let root = TempRoot::new("create-retry");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        api.push_create_error(RecorderTransportError::Transient("timeout".into()));
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        assert_eq!(api.create_calls(), 2);
        let recoveries = api.create_recoveries();
        assert_eq!(recoveries.len(), 2);
        assert_eq!(recoveries[0], recoveries[1]);
        assert_eq!(mock.starts(), 1);
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn create_guard_full_keeps_intent_and_reports_restart() {
        let root = TempRoot::new("create-guard-full");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        api.push_create_error(RecorderTransportError::CreateGuardFull);
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        assert_eq!(api.create_calls(), 1);
        assert_eq!(mock.starts(), 0);
        assert_eq!(driver.state(), CaptureState::Idle);
        assert!(driver
            .snapshot()
            .last_error
            .unwrap_or_default()
            .contains("restart Core"));
        assert_eq!(
            SpoolStore::new(&root.0)
                .unwrap()
                .pending_start_ids()
                .unwrap()
                .len(),
            1
        );
    }

    #[tokio::test]
    async fn pending_start_404_deletes_the_intent_without_reusing_the_key() {
        let root = TempRoot::new("pending-404");
        leave_pending(&root);
        let fake = FakeApi::new(recording("0123456789abcdef0123456789abcdef", "lease-1"));
        fake.set_resume_result(Err(RecorderTransportError::CreateNotFound));
        let mut driver = recovery_driver(&root, fake);
        let report = driver.recover_pending_starts().await.unwrap();
        assert_eq!(report.recovered, 1, "{report:?}");
        assert!(report.issues.is_empty(), "{report:?}");
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn pending_start_active_lease_is_stopped_at_the_durable_offset() {
        let root = TempRoot::new("pending-active");
        leave_pending(&root);
        let mut resumed = recording("0123456789abcdef0123456789abcdef", "lease-1");
        resumed.durable_offset = 0;
        let fake = FakeApi::new(resumed.clone());
        fake.set_resume_result(Ok(RecordingSnapshot {
            recording: resumed,
            active: true,
            sealed: false,
        }));
        let mut driver = recovery_driver(&root, fake.clone());
        let report = driver.recover_pending_starts().await.unwrap();
        assert_eq!(report.recovered, 1, "{report:?}");
        assert_eq!(fake.resume_calls(), 1);
        assert_eq!(fake.stop_calls(), vec![0]);
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn pending_start_interrupted_is_sealed_with_the_client_capability() {
        let root = TempRoot::new("pending-interrupted");
        leave_pending(&root);
        let mut resumed = recording("0123456789abcdef0123456789abcdef", "lease-1");
        resumed.lease = None;
        resumed.durable_offset = 320;
        resumed.final_sample_count = None;
        let fake = FakeApi::new(resumed.clone());
        fake.set_resume_result(Ok(RecordingSnapshot {
            recording: resumed,
            active: false,
            sealed: false,
        }));
        let mut driver = recovery_driver(&root, fake.clone());
        let report = driver.recover_pending_starts().await.unwrap();
        assert_eq!(report.recovered, 1, "{report:?}");
        let (recoveries, _) = fake.recovery_calls();
        assert_eq!(
            recoveries,
            vec![("0123456789abcdef0123456789abcdef".into(), 320)]
        );
        assert!(!format!("{report:?}").contains("client-recovery-capability"));
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn pending_start_guard_full_keeps_intent_for_a_restart() {
        let root = TempRoot::new("pending-guard-full");
        leave_pending(&root);
        let fake = FakeApi::new(recording("0123456789abcdef0123456789abcdef", "lease-1"));
        fake.set_resume_result(Err(RecorderTransportError::CreateGuardFull));
        let mut driver = recovery_driver(&root, fake);
        let report = driver.recover_pending_starts().await.unwrap();
        assert_eq!(report.recovered, 0);
        assert!(report
            .issues
            .iter()
            .any(|issue| issue.kind == RecoveryIssueKind::CoreRestartRequired));
        assert_eq!(
            SpoolStore::new(&root.0)
                .unwrap()
                .pending_start_ids()
                .unwrap(),
            vec![pending_key().to_owned()]
        );
    }

    #[tokio::test]
    async fn pending_start_with_an_existing_spool_only_deletes_the_intent() {
        let root = TempRoot::new("pending-spool");
        leave_pending(&root);
        let id = "0123456789abcdef0123456789abcdef";
        let store = SpoolStore::new(&root.0).unwrap();
        let mut spool = store
            .create(SpoolMeta::new(
                id,
                "tui",
                1,
                pending_recovery().expose().to_owned(),
                Some("stream".into()),
            ))
            .unwrap();
        spool.mark_interrupted(Some(0), Some(0)).unwrap();
        drop(spool);
        let fake = FakeApi::new(recording(id, "lease-1"));
        let mut driver = recovery_driver(&root, fake.clone());
        let report = driver.recover_pending_starts().await.unwrap();
        assert_eq!(report.recovered, 1, "{report:?}");
        assert!(fake.recovery_calls().0.is_empty());
        assert!(fake.stop_calls().is_empty());
        assert!(store.pending_start_ids().unwrap().is_empty());
    }

    #[tokio::test]
    async fn pending_write_failure_makes_no_create_call() {
        let root = TempRoot::new("pending-write-fail");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        std::fs::write(root.0.join("recorder-spool").join("starts"), b"x").unwrap();
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        assert_eq!(api.create_calls(), 0);
        assert_eq!(mock.starts(), 0);
        assert_eq!(driver.state(), CaptureState::Idle);
    }

    async fn committed_but_lost_create(
        root: &TempRoot,
    ) -> (String, FakeApi, RecorderDriver<FakeApi>, MockCapture) {
        let id = "0123456789abcdef0123456789abcdef";
        let (mut driver, api, mock) = driver(root, recording(id, "lease-1"));
        api.push_create_error(RecorderTransportError::Permanent(
            "connection dropped after Core committed the create".into(),
        ));
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        assert_eq!(api.create_calls(), 1);
        assert_eq!(mock.starts(), 0);
        assert_eq!(driver.state(), CaptureState::Idle);
        assert_eq!(
            SpoolStore::new(&root.0)
                .unwrap()
                .pending_start_ids()
                .unwrap()
                .len(),
            1
        );
        (id.to_owned(), api, driver, mock)
    }

    #[tokio::test]
    async fn committed_create_lost_response_active_lease_is_stopped_without_duplicate() {
        let root = TempRoot::new("lost-response-active");
        let (id, first_api, first, _first_mock) = committed_but_lost_create(&root).await;
        drop(first);
        let created = first_api.create_invocations()[0].clone();

        let mut resumed = recording(&id, "lease-1");
        resumed.durable_offset = 0;
        let fake = FakeApi::new(resumed.clone());
        fake.set_resume_result(Ok(RecordingSnapshot {
            recording: resumed,
            active: true,
            sealed: false,
        }));
        let (mut second, mock) = recovery_driver_with_capture(&root, fake.clone());
        let report = second.recover_pending_starts().await.unwrap();
        assert_eq!(report.recovered, 1, "{report:?}");
        assert_eq!(fake.create_calls(), 0);
        assert_eq!(fake.resume_invocations(), vec![created]);
        assert_eq!(fake.stop_calls(), vec![0]);
        assert_eq!(mock.starts(), 0);
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn committed_create_after_core_restart_interrupted_is_recovered_without_duplicate() {
        let root = TempRoot::new("lost-response-restart");
        let (id, first_api, first, _first_mock) = committed_but_lost_create(&root).await;
        drop(first);
        let created = first_api.create_invocations()[0].clone();

        let mut interrupted = recording(&id, "");
        interrupted.lease = None;
        interrupted.durable_offset = 320;
        interrupted.final_sample_count = None;
        let fake = FakeApi::new(interrupted.clone());
        fake.set_resume_result(Ok(RecordingSnapshot {
            recording: interrupted,
            active: false,
            sealed: false,
        }));
        let (mut second, mock) = recovery_driver_with_capture(&root, fake.clone());
        let report = second.recover_pending_starts().await.unwrap();
        assert_eq!(report.recovered, 1, "{report:?}");
        assert_eq!(fake.create_calls(), 0);
        assert_eq!(fake.resume_invocations(), vec![created]);
        let (recoveries, _) = fake.recovery_calls();
        assert_eq!(recoveries, vec![(id.to_owned(), 320)]);
        assert!(fake.stop_calls().is_empty());
        assert_eq!(mock.starts(), 0);
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn cancelling_create_with_active_recording_stops_it_without_duplicate() {
        let root = TempRoot::new("cancel-create-active");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        let entered = Arc::new(Semaphore::new(0));
        let release = Arc::new(Semaphore::new(0));
        api.set_create_gate(entered.clone(), release.clone());
        let mut resumed = recording("0123456789abcdef0123456789abcdef", "lease-1");
        resumed.durable_offset = 0;
        api.set_resume_result(Ok(RecordingSnapshot {
            recording: resumed,
            active: true,
            sealed: false,
        }));
        driver.start(Duration::ZERO, start_request()).unwrap();
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let handle = tokio::spawn(async move {
            let result = driver.run(cancel_rx).await;
            (driver, result)
        });
        let _permit = entered.acquire().await.unwrap();
        cancel_tx.send(true).unwrap();
        let (driver, result) = handle.await.unwrap();
        result.unwrap();
        assert_eq!(api.create_calls(), 1);
        assert_eq!(api.resume_calls(), 1);
        assert_eq!(api.create_invocations(), api.resume_invocations());
        assert_eq!(api.stop_calls(), vec![0]);
        assert_eq!(mock.starts(), 0);
        assert_eq!(driver.state(), CaptureState::Idle);
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn cancelling_create_after_core_restart_recovers_interrupted_without_duplicate() {
        let root = TempRoot::new("cancel-create-interrupted");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        let entered = Arc::new(Semaphore::new(0));
        let release = Arc::new(Semaphore::new(0));
        api.set_create_gate(entered.clone(), release.clone());
        let mut interrupted = recording("0123456789abcdef0123456789abcdef", "");
        interrupted.lease = None;
        interrupted.durable_offset = 320;
        interrupted.final_sample_count = None;
        api.set_resume_result(Ok(RecordingSnapshot {
            recording: interrupted,
            active: false,
            sealed: false,
        }));
        driver.start(Duration::ZERO, start_request()).unwrap();
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let handle = tokio::spawn(async move {
            let result = driver.run(cancel_rx).await;
            (driver, result)
        });
        let _permit = entered.acquire().await.unwrap();
        cancel_tx.send(true).unwrap();
        let (driver, result) = handle.await.unwrap();
        result.unwrap();
        assert_eq!(api.create_calls(), 1);
        assert_eq!(api.resume_calls(), 1);
        assert_eq!(api.create_invocations(), api.resume_invocations());
        let (recoveries, _) = api.recovery_calls();
        assert_eq!(
            recoveries,
            vec![("0123456789abcdef0123456789abcdef".to_owned(), 320)]
        );
        assert!(api.stop_calls().is_empty());
        assert_eq!(mock.starts(), 0);
        assert_eq!(driver.state(), CaptureState::Idle);
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[test]
    fn start_ids_use_a_full_32_byte_os_csprng_capability() {
        let (key1, recovery1) = new_start_ids().unwrap();
        let (key2, recovery2) = new_start_ids().unwrap();
        assert_eq!(key1.len(), 32);
        assert!(key1
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()));
        assert_eq!(recovery1.expose().len(), 43);
        assert!(recovery1
            .expose()
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_')));
        assert_ne!(key1, key2);
        assert_ne!(recovery1.expose(), recovery2.expose());
    }

    #[tokio::test]
    async fn cancellation_aborts_an_inflight_chunk_with_a_retry_event() {
        let root = TempRoot::new("cancel-inflight");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        mock.emit_audio(0, vec![1; 16_000]);

        let entered = Arc::new(Semaphore::new(0));
        let release = Arc::new(Semaphore::new(0));
        api.set_put_gate(entered.clone(), release.clone());
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let handle = tokio::spawn(async move {
            let result = driver.run(cancel_rx).await;
            (driver, result)
        });
        let _permit = entered.acquire().await.unwrap();
        cancel_tx.send(true).unwrap();
        let (mut driver, result) = handle.await.unwrap();
        result.unwrap();

        // The command was not silently lost: the controller got a transient
        // chunk failure and can retry, while the spool keeps the chunk.
        assert!(driver.in_flight.is_none());
        assert_eq!(driver.state(), CaptureState::Recording);
        assert_eq!(driver.spool_meta().unwrap().pending_samples(), 16_000);
        assert_eq!(api.puts().len(), 1);
        api.clear_put_gate();
        for seconds in 1..=8 {
            if api.puts().len() == 2 {
                break;
            }
            driver.step(Duration::from_secs(seconds)).await.unwrap();
        }
        assert_eq!(api.puts().len(), 2);
        assert_eq!(driver.spool_meta().unwrap().pending_samples(), 0);
    }

    #[tokio::test]
    async fn spool_failure_stops_without_uploading_or_claiming_audio() {
        let root = TempRoot::new("spool-failure");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        driver.start(Duration::ZERO, start_request()).unwrap();
        driver.step(Duration::ZERO).await.unwrap();
        let id = driver.spool_meta().unwrap().id.clone();
        let chunk = root
            .0
            .join("recorder-spool")
            .join(id)
            .join("chunks")
            .join("00000000.pcm");
        std::fs::write(&chunk, b"not a chunk").unwrap();

        mock.emit_audio(0, vec![1; 16_000]);
        driver.step(Duration::from_millis(10)).await.unwrap();
        for now in [20, 30, 40] {
            driver.step(Duration::from_millis(now)).await.unwrap();
        }

        assert!(api.puts().is_empty(), "no chunk may be uploaded");
        assert_eq!(api.stop_calls(), vec![0]);
        assert_eq!(driver.state(), CaptureState::Failed);
        assert_eq!(driver.spool_meta().unwrap().state, SpoolState::Stopped);
        // The local write failed, so the controller keeps the unacknowledged
        // in-memory chunk visible; it is not reported as saved.
        assert_eq!(driver.snapshot().pending_samples, 16_000);
        assert_eq!(driver.spool_meta().unwrap().pending_samples(), 0);
    }

    #[tokio::test]
    async fn cancelling_an_inflight_create_runs_the_resume_cleanup() {
        let root = TempRoot::new("cancel-create");
        let (mut driver, api, mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        let entered = Arc::new(Semaphore::new(0));
        let release = Arc::new(Semaphore::new(0));
        api.set_create_gate(entered.clone(), release.clone());
        api.set_resume_result(Err(RecorderTransportError::CreateNotFound));
        driver.start(Duration::ZERO, start_request()).unwrap();
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let handle = tokio::spawn(async move {
            let result = driver.run(cancel_rx).await;
            (driver, result)
        });
        let _permit = entered.acquire().await.unwrap();
        cancel_tx.send(true).unwrap();
        let (driver, result) = handle.await.unwrap();
        result.unwrap();
        assert_eq!(api.create_calls(), 1);
        assert_eq!(api.resume_calls(), 1);
        assert_eq!(mock.starts(), 0);
        assert_eq!(driver.state(), CaptureState::Idle);
        assert!(SpoolStore::new(&root.0)
            .unwrap()
            .pending_start_ids()
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn run_observes_cancellation_without_commands() {
        let root = TempRoot::new("cancel");
        let (mut driver, _api, _mock) = driver(
            &root,
            recording("0123456789abcdef0123456789abcdef", "lease-1"),
        );
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        driver.run(rx).await.unwrap();
    }

    #[tokio::test]
    async fn create_sends_client_recovery_and_idempotency_headers() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (captured_tx, captured_rx) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            let mut buffer = [0u8; 4096];
            let (header_end, content_length) = loop {
                let read = socket.read(&mut buffer).await.unwrap();
                assert!(read > 0, "connection closed before headers");
                request.extend_from_slice(&buffer[..read]);
                if let Some(position) = request.windows(4).position(|window| window == b"\r\n\r\n")
                {
                    let headers = String::from_utf8_lossy(&request[..position]).to_string();
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0);
                    break (position, length);
                }
            };
            while request.len() < header_end + 4 + content_length {
                let read = socket.read(&mut buffer).await.unwrap();
                assert!(read > 0, "connection closed before body");
                request.extend_from_slice(&buffer[..read]);
            }
            let headers_text = String::from_utf8_lossy(&request[..header_end]).to_string();
            let mut lines = headers_text.lines();
            let request_line = lines.next().unwrap_or_default().to_owned();
            let headers: BTreeMap<String, String> = lines
                .filter_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    Some((name.to_ascii_lowercase(), value.trim().to_owned()))
                })
                .collect();
            let body = request[header_end + 4..header_end + 4 + content_length].to_vec();
            let _ = captured_tx.send((request_line, headers, body));
            let payload = br#"{"id":"0123456789abcdef0123456789abcdef","generation":1,"lease":"lease-1","recovery_token":null,"recovery":"client","durable_offset":0,"chunks":0,"capture_state":"recording","final_sample_count":null}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                payload.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
            socket.write_all(payload).await.unwrap();
        });

        let http = Http::new(&format!("http://{address}"), "").unwrap();
        let api = CoreRecorderApi::new(CoreApi::new(http));
        let request = StartRequest::new("stream-1", "tui", "ignored-request-key");
        let recording = api
            .create(
                &request,
                "client-key-1",
                "client-recovery-R-0123456789abcdef",
            )
            .await
            .unwrap();
        server.await.unwrap();
        assert_eq!(recording.lease.as_deref(), Some("lease-1"));
        assert!(recording.recovery_token.is_none());
        let (request_line, headers, body) = captured_rx.await.unwrap();
        assert_eq!(request_line, "POST /api/recordings HTTP/1.1");
        assert_eq!(
            headers.get("x-recorder-recovery").map(String::as_str),
            Some("client-recovery-R-0123456789abcdef")
        );
        assert_eq!(
            headers.get("idempotency-key").map(String::as_str),
            Some("client-key-1")
        );
        let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(body["mode"], "create");
        assert_eq!(body["stream_id"], "stream-1");
        assert_eq!(body["client_id"], "tui");
    }

    #[tokio::test]
    async fn binary_chunk_put_has_the_required_headers_and_body() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (captured_tx, captured_rx) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            let mut buffer = [0u8; 4096];
            let (header_end, content_length) = loop {
                let read = socket.read(&mut buffer).await.unwrap();
                assert!(read > 0, "connection closed before headers");
                request.extend_from_slice(&buffer[..read]);
                if let Some(position) = request.windows(4).position(|window| window == b"\r\n\r\n")
                {
                    let headers = String::from_utf8_lossy(&request[..position]).to_string();
                    let content_length = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0);
                    break (position, content_length);
                }
            };
            while request.len() < header_end + 4 + content_length {
                let read = socket.read(&mut buffer).await.unwrap();
                assert!(read > 0, "connection closed before body");
                request.extend_from_slice(&buffer[..read]);
            }
            let headers_text = String::from_utf8_lossy(&request[..header_end]).to_string();
            let mut lines = headers_text.lines();
            let request_line = lines.next().unwrap_or_default().to_owned();
            let headers: BTreeMap<String, String> = lines
                .filter_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    Some((name.to_ascii_lowercase(), value.trim().to_owned()))
                })
                .collect();
            let body = request[header_end + 4..header_end + 4 + content_length].to_vec();
            let _ = captured_tx.send((request_line, headers, body));
            let payload = br#"{"recording":{"id":"0123456789abcdef0123456789abcdef","generation":1,"durable_offset":16000,"chunks":1},"chunk":{}}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                payload.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
            socket.write_all(payload).await.unwrap();
        });

        let http = Http::new(&format!("http://{address}"), "").unwrap();
        let api = CoreRecorderApi::new(CoreApi::new(http));
        let bytes = vec![0x22; 32_000];
        let hash = "ab".repeat(32);
        let ack = api
            .put_chunk(
                "0123456789abcdef0123456789abcdef",
                0,
                0,
                "lease-token",
                "stream-1",
                &hash,
                &bytes,
            )
            .await
            .unwrap();
        server.await.unwrap();
        assert_eq!(ack.durable_offset, 16_000);
        assert_eq!(ack.next_sequence, 1);
        let (request_line, headers, body) = captured_rx.await.unwrap();
        assert_eq!(
            request_line,
            "PUT /api/recordings/0123456789abcdef0123456789abcdef/chunks/0 HTTP/1.1"
        );
        assert_eq!(
            headers.get("content-type").map(String::as_str),
            Some("application/octet-stream")
        );
        assert_eq!(
            headers.get("x-recorder-lease").map(String::as_str),
            Some("lease-token")
        );
        assert_eq!(
            headers.get("x-recorder-stream").map(String::as_str),
            Some("stream-1")
        );
        assert_eq!(headers.get("x-sample-start").map(String::as_str), Some("0"));
        assert_eq!(
            headers.get("x-content-sha256").map(String::as_str),
            Some(hash.as_str())
        );
        assert_eq!(body, bytes);
    }
}
