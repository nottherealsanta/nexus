//! Durable, bounded recorder spool and recovery planning shared by Nexus clients
//! (plan 54, D-TUI-cap continuation and W1).
//!
//! The live recorder controller keeps only a bounded in-memory queue. This module
//! is the client-side durable mirror: it stores unacknowledged PCM16 chunks and
//! the recovery capability under the client Nexus home, survives a client
//! crash/restart, reconciles incomplete writes, and produces recovery-only
//! uploads for `POST /api/recordings/{id}/recover` and
//! `PUT /api/recordings/{id}/recover/chunks/{seq}`.
//!
//! Layout under `<client Nexus home>/recorder-spool/`:
//!
//! ```text
//! <recording-id>.lock          0600, held for the live capture/recovery
//! <recording-id>/
//!   meta                        0600, atomic text metadata
//!   extras.json                 0600, optional bounded JSON sidecar
//!   chunks/                     0700
//!     00000000.pcm              0600, header + raw PCM16 body
//! starts/
//!   <key>.start                 0600, a create intent persisted before POST
//!   <key>.lock                  0600, held from before the intent through create
//! ```
//!
//! Chunk files are the durable truth for spooled audio. `meta` is a hint that is
//! repaired from the chunk scan on open: a crash after a chunk rename but before
//! the metadata update adopts the chunk; a crash after an acknowledgement but
//! before file deletion drops already-saved files. Metadata is written with
//! tmp + fsync + rename + parent fsync. `append_chunk` returns `Ok` only after
//! that commit, so a caller never treats an unsaved chunk as durable.
//!
//! The recovery token is a bearer capability. It is stored only in the 0600
//! `meta` file and never appears in `Debug` output or summaries. Recovery never
//! invents missing chunks: a gap, a corrupted chunk, a sequence mismatch or a
//! server checkpoint that disagrees with the local spool is reported as a
//! conflict/gap, and uploads stop at the first discontinuity.
//!
//! A live spool is protected by an exclusive OS lock (`std::fs::File::try_lock`,
//! flock/LockFileEx). `create`/`open` acquire it first, so a second process (or a
//! second window) sees [`SpoolError::Locked`] instead of repairing a live capture.
//! Lock files live beside the data and are never unlinked while the data exists;
//! [`SpoolStore::prune_locks`] removes only lock files whose data is gone and
//! whose lock it can acquire. Each record carries an [`SpoolApp`] tag (a missing
//! tag means [`SpoolApp::Tui`]) so clients auto-recover only their own records.
//!
//! This module deliberately has no transport or HTTP dependency. The next
//! transport driver:
//!
//! 1. on `Created`, creates [`SpoolSession`] with the recording id, client id,
//!    generation and recovery token;
//! 2. on each outbound chunk, calls [`SpoolSession::append_chunk`] before
//!    relying on it, then uploads the returned body;
//! 3. on a durable acknowledgement, calls [`SpoolSession::acknowledge`];
//! 4. on interruption/lease loss, calls [`SpoolSession::mark_interrupted`];
//! 5. on start-up, opens/reconciles every spool and executes
//!    [`RecoveryPlan::actions`] after querying Core's authoritative
//!    `durable_offset`/`chunks`/`final_sample_count` as a [`ServerBaseline`].

use std::collections::{BTreeMap, HashSet};
use std::ffi::CString;
use std::fmt;
use std::fs::{self, File, OpenOptions, TryLockError};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
#[cfg(unix)]
use std::os::unix::io::{AsRawFd, FromRawFd};

pub const SAMPLE_RATE: u32 = 16_000;
pub const SPOOL_SECONDS: u64 = 30;
pub const MAX_SPOOL_SAMPLES: u64 = SPOOL_SECONDS * SAMPLE_RATE as u64;
pub const MAX_CHUNK_BYTES: usize = 64_000;
/// The smallest free-space headroom a caller should keep for a spool.
pub const SPOOL_MIN_FREE_BYTES: u64 = 8 * 1024 * 1024;

const META_VERSION: u32 = 1;
const META_NAME: &str = "meta";
const CHUNKS_NAME: &str = "chunks";
const SPOOL_DIR_NAME: &str = "recorder-spool";
const STARTS_NAME: &str = "starts";
const EXTRAS_NAME: &str = "extras.json";
const MAX_EXTRAS_BYTES: usize = 64 * 1024;
const LOCK_SUFFIX: &str = ".lock";
const CHUNK_MAGIC: &[u8; 8] = b"NXSPOOL1";
const CHUNK_VERSION: u32 = 1;
const CHUNK_HEADER_LEN: usize = 72;
const TEMP_PREFIX: &str = ".tmp-";
/// Hard read cap during reconciliation. A 30 s spool is normally ~30 files;
/// this prevents a corrupted directory from making recovery unbounded.
const MAX_SCAN_CHUNKS: usize = 1024;
const MAX_SCAN_BYTES: u64 =
    MAX_SPOOL_SAMPLES * 2 + MAX_SCAN_CHUNKS as u64 * CHUNK_HEADER_LEN as u64;
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);
/// In-process marks for held lock paths. The OS lock is authoritative across
/// processes; this makes a second open in one process deterministically locked
/// before the syscall on platforms with process-scoped locks.
static LOCK_PATHS: std::sync::LazyLock<Mutex<HashSet<PathBuf>>> =
    std::sync::LazyLock::new(|| Mutex::new(HashSet::new()));

#[cfg(test)]
thread_local! {
    static CRASH_POINT: std::cell::RefCell<Option<&'static str>> =
        const { std::cell::RefCell::new(None) };
}

/// Test-only crash simulation at a durability boundary. Outside unit tests this
/// is a no-op and compiles away.
#[cfg(test)]
fn crash_point(name: &str) {
    let hit = CRASH_POINT.with(|point| *point.borrow() == Some(name));
    if hit {
        panic!("simulated recorder spool crash at {name}");
    }
}

#[cfg(not(test))]
#[inline]
fn crash_point(_name: &str) {}

pub type Result<T> = std::result::Result<T, SpoolError>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpoolError {
    Io {
        path: String,
        message: String,
    },
    InvalidId,
    NotFound(String),
    AlreadyExists(String),
    InvalidMeta(String),
    InvalidChunk(String),
    ChunkTooLarge {
        size: u64,
        max: u64,
    },
    HashMismatch {
        expected: String,
        actual: String,
    },
    SpoolFull {
        pending: u64,
        requested: u64,
        capacity: u64,
    },
    Noncontiguous {
        expected_sequence: u64,
        expected_start: u64,
        sequence: u64,
        start: u64,
    },
    /// Another live recorder (process or window) holds this spool or pending
    /// start. The spool was not opened, repaired or modified.
    Locked,
    NotCapturing(SpoolState),
    Conflict(String),
}

impl fmt::Display for SpoolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, message } => write!(f, "{path}: {message}"),
            Self::InvalidId => write!(f, "recording id must be 32 lowercase hex characters"),
            Self::NotFound(id) => write!(f, "spool {id} was not found"),
            Self::AlreadyExists(id) => write!(f, "spool {id} already exists"),
            Self::InvalidMeta(message) => write!(f, "invalid spool metadata: {message}"),
            Self::InvalidChunk(message) => write!(f, "invalid spool chunk: {message}"),
            Self::ChunkTooLarge { size, max } => {
                write!(f, "spool chunk is {size} bytes; the maximum is {max}")
            }
            Self::HashMismatch { expected, actual } => {
                write!(f, "chunk hash mismatch: expected {expected}, got {actual}")
            }
            Self::SpoolFull {
                pending,
                requested,
                capacity,
            } => write!(
                f,
                "spool is full: {pending} pending + {requested} requested > {capacity} samples"
            ),
            Self::Noncontiguous {
                expected_sequence,
                expected_start,
                sequence,
                start,
            } => write!(
                f,
                "chunk does not continue the spool: expected sequence {expected_sequence} start {expected_start}, got sequence {sequence} start {start}"
            ),
            Self::Locked => write!(f, "the recording spool is held by another recorder"),
            Self::NotCapturing(state) => write!(f, "spool is {state:?}, not capturing"),
            Self::Conflict(message) => write!(f, "{message}"),
        }
    }
}

impl std::error::Error for SpoolError {}

/// A recovery token whose `Debug` representation is always redacted.
#[derive(Clone, PartialEq, Eq)]
pub struct Secret(String);

impl Secret {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    pub fn expose(&self) -> &str {
        &self.0
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(redacted)")
    }
}

/// The client that owns a spool or create intent. Records written before this
/// tag existed are TUI records. A client auto-recovers only its own tag; locks
/// are honoured for every tag.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum SpoolApp {
    #[default]
    Tui,
    Nsh,
}

impl SpoolApp {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Tui => "tui",
            Self::Nsh => "nsh",
        }
    }

    fn parse(value: &str) -> Result<Self> {
        match value {
            "tui" => Ok(Self::Tui),
            "nsh" => Ok(Self::Nsh),
            _ => Err(SpoolError::InvalidMeta(format!("unknown app {value}"))),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpoolState {
    Capturing,
    Interrupted,
    Stopped,
    Recovered,
}

impl SpoolState {
    fn as_str(self) -> &'static str {
        match self {
            Self::Capturing => "capturing",
            Self::Interrupted => "interrupted",
            Self::Stopped => "stopped",
            Self::Recovered => "recovered",
        }
    }

    fn parse(value: &str) -> Result<Self> {
        match value {
            "capturing" => Ok(Self::Capturing),
            "interrupted" => Ok(Self::Interrupted),
            "stopped" => Ok(Self::Stopped),
            "recovered" => Ok(Self::Recovered),
            _ => Err(SpoolError::InvalidMeta(format!("unknown state {value}"))),
        }
    }
}

/// Metadata persisted next to the chunk files.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpoolMeta {
    pub version: u32,
    pub id: String,
    pub app: SpoolApp,
    pub client_id: String,
    pub generation: u64,
    pub recovery_token: Secret,
    pub stream_id: Option<String>,
    pub durable_offset: u64,
    pub next_sequence: u64,
    pub accepted_samples: u64,
    pub received_samples: u64,
    pub final_sample_count: Option<u64>,
    pub state: SpoolState,
    pub created_at: u64,
}

impl SpoolMeta {
    pub fn new(
        id: impl Into<String>,
        client_id: impl Into<String>,
        generation: u64,
        recovery_token: impl Into<String>,
        stream_id: Option<String>,
    ) -> Self {
        Self {
            version: META_VERSION,
            id: id.into(),
            app: SpoolApp::Tui,
            client_id: client_id.into(),
            generation,
            recovery_token: Secret::new(recovery_token),
            stream_id,
            durable_offset: 0,
            next_sequence: 0,
            accepted_samples: 0,
            received_samples: 0,
            final_sample_count: None,
            state: SpoolState::Capturing,
            created_at: unix_seconds(),
        }
    }

    pub fn pending_samples(&self) -> u64 {
        self.accepted_samples.saturating_sub(self.durable_offset)
    }

    pub fn with_app(mut self, app: SpoolApp) -> Self {
        self.app = app;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpoolSummary {
    pub id: String,
    pub app: Option<SpoolApp>,
    pub client_id: Option<String>,
    pub state: Option<SpoolState>,
    pub durable_offset: u64,
    pub accepted_samples: u64,
    pub final_sample_count: Option<u64>,
    pub pending_samples: u64,
    pub chunks: usize,
    pub error: Option<String>,
}

/// A create intent persisted before Core is called. It carries the client-owned
/// recovery capability R and lookup key K; Debug redacts the capability.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingStart {
    pub key: String,
    pub app: SpoolApp,
    pub recovery: Secret,
    pub client_id: String,
    pub title: Option<String>,
    pub created_at: u64,
}

/// Holds the per-key advisory lock while a create is in flight or recovery is
/// inspecting the record. Dropping it releases the lock but leaves the record
/// for crash recovery; `finish` removes both after the recording is bound/sealed.
pub struct PendingStartGuard {
    path: PathBuf,
    lock: LockFile,
    record: PendingStart,
    finished: bool,
}

impl PendingStartGuard {
    pub fn record(&self) -> &PendingStart {
        &self.record
    }

    pub fn finish(mut self) -> Result<()> {
        let dir = self.path.parent().map(Path::to_path_buf);
        remove_file_if_present(&self.path)?;
        if let Some(dir) = dir {
            sync_dir(&dir)?;
        }
        self.finished = true;
        Ok(())
    }
}

impl Drop for PendingStartGuard {
    fn drop(&mut self) {
        let _ = self.finished;
        // Closing the fd releases the advisory lock. Unfinished records remain
        // for crash recovery.
        let _ = &self.lock;
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredChunk {
    pub sequence: u64,
    pub start: u64,
    pub samples: u64,
    pub sha256: String,
    /// Raw PCM16 body, without the spool header.
    pub bytes: Vec<u8>,
}

impl StoredChunk {
    pub fn end(&self) -> u64 {
        self.start.saturating_add(self.samples)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ServerBaseline {
    /// Core's authoritative durable contiguous sample offset.
    pub durable_offset: u64,
    /// Core's next expected sequence (`chunks` count for a contiguous prefix).
    pub next_sequence: u64,
    /// Core's sealed final count, when it has one.
    pub final_sample_count: Option<u64>,
    /// True when Core already has a sealed recording (no recover call needed).
    pub sealed: bool,
}

impl ServerBaseline {
    fn local(meta: &SpoolMeta, chunks: &[StoredChunk]) -> Self {
        // `meta.next_sequence` is the next append sequence. After an
        // acknowledgement, the first unacknowledged chunk is what Core expects
        // next for recovery (files wholly below the durable offset may still be
        // waiting for cleanup).
        let next_sequence = chunks
            .iter()
            .filter(|chunk| chunk.end() > meta.durable_offset)
            .map(|chunk| chunk.sequence)
            .min()
            .unwrap_or(meta.next_sequence);
        Self {
            durable_offset: meta.durable_offset,
            next_sequence,
            final_sample_count: meta.final_sample_count,
            sealed: matches!(meta.state, SpoolState::Stopped | SpoolState::Recovered),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GapCause {
    /// A chunk sequence/offset is absent from the spool.
    MissingSpooled,
    /// The declared final count extends beyond the last spooled chunk.
    UnsavedTail,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryGap {
    pub start: u64,
    pub samples: u64,
    pub cause: GapCause,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConflictKind {
    ServerBehindLocal,
    BaselineInsideChunk,
    SequenceMismatch,
    ChunkHashMismatch,
    ChunkCorrupt(String),
    ChunkOversize,
    AggregateOverfull,
    MetaAhead,
    FinalBeforeDurable,
    FinalCountMismatch,
    BeyondFinal,
    UnexpectedFile,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryConflict {
    pub kind: ConflictKind,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryPlan {
    pub id: String,
    pub client_id: String,
    pub generation: u64,
    pub recovery_token: Secret,
    pub state: SpoolState,
    pub baseline: ServerBaseline,
    pub final_sample_count: u64,
    pub needs_seal: bool,
    /// Contiguous chunks safe to upload before any reported gap/conflict.
    pub uploads: Vec<StoredChunk>,
    /// Gaps that stop the upload at the reported point.
    pub gaps: Vec<RecoveryGap>,
    /// Truthful problems the transport driver must surface, not hide.
    pub conflicts: Vec<RecoveryConflict>,
    /// Declared final samples not represented by the spooled contiguous run.
    pub unsaved_samples: u64,
}

impl RecoveryPlan {
    /// The two recovery-only wire actions the next transport driver executes.
    ///
    /// The driver must check `conflicts`/`gaps` before uploading; `uploads` only
    /// contains the contiguous prefix that can truthfully be recovered.
    pub fn actions(&self) -> Vec<RecoveryAction> {
        let mut actions = Vec::new();
        if self.needs_seal {
            actions.push(RecoveryAction::Seal {
                recording_id: self.id.clone(),
                client_id: self.client_id.clone(),
                recovery_token: self.recovery_token.clone(),
                final_sample_count: self.final_sample_count,
            });
        }
        for chunk in &self.uploads {
            actions.push(RecoveryAction::Upload {
                recording_id: self.id.clone(),
                client_id: self.client_id.clone(),
                recovery_token: self.recovery_token.clone(),
                sequence: chunk.sequence,
                start: chunk.start,
                samples: chunk.samples,
                sha256: chunk.sha256.clone(),
                bytes: chunk.bytes.clone(),
            });
        }
        actions
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecoveryAction {
    Seal {
        recording_id: String,
        client_id: String,
        recovery_token: Secret,
        final_sample_count: u64,
    },
    Upload {
        recording_id: String,
        client_id: String,
        recovery_token: Secret,
        sequence: u64,
        start: u64,
        samples: u64,
        sha256: String,
        bytes: Vec<u8>,
    },
}

/// Owns the spool root under the client Nexus home.
pub struct SpoolStore {
    root: PathBuf,
    capacity_samples: u64,
}

impl SpoolStore {
    /// `client_home` is `discovery::home()`; the spool root is
    /// `<client_home>/recorder-spool`.
    pub fn new(client_home: impl AsRef<Path>) -> Result<Self> {
        Self::at_root(client_home.as_ref().join(SPOOL_DIR_NAME), MAX_SPOOL_SAMPLES)
    }

    /// Open/create an explicit spool root. Tests use this with a scratch home.
    pub fn at_root(root: impl Into<PathBuf>, capacity_samples: u64) -> Result<Self> {
        let root = root.into();
        ensure_private_dir(&root)?;
        Ok(Self {
            root,
            capacity_samples: capacity_samples.max(1),
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn capacity_samples(&self) -> u64 {
        self.capacity_samples
    }

    pub fn create(&self, meta: SpoolMeta) -> Result<SpoolSession> {
        validate_meta(&meta)?;
        let lock = self.acquire_spool_lock(&meta.id)?;
        let dir = self.root.join(&meta.id);
        if dir.exists() {
            return Err(SpoolError::AlreadyExists(meta.id));
        }
        ensure_private_dir(&dir)?;
        let chunks_dir = dir.join(CHUNKS_NAME);
        if let Err(error) = ensure_private_dir(&chunks_dir) {
            let _ = fs::remove_dir_all(&dir);
            return Err(error);
        }
        let chunk_dir = match ChunkDir::open(&chunks_dir) {
            Ok(chunk_dir) => chunk_dir,
            Err(error) => {
                let _ = fs::remove_dir_all(&dir);
                return Err(error);
            }
        };
        sync_dir(&self.root)?;
        sync_dir(&dir)?;
        if let Err(error) = write_meta(&dir, &meta) {
            let _ = fs::remove_dir_all(&dir);
            return Err(error);
        }
        Ok(SpoolSession {
            dir,
            chunks_dir,
            chunk_dir,
            lock,
            meta,
            capacity_samples: self.capacity_samples,
            repaired: false,
            repair_conflicts: Vec::new(),
        })
    }

    /// Open for restart/recovery. This repairs metadata from the chunk files and
    /// converts a crashed `capturing` spool into an explicit interruption. A
    /// spool locked by another live recorder is never opened or repaired.
    pub fn open(&self, id: &str) -> Result<SpoolSession> {
        validate_id(id)?;
        let lock = self.acquire_spool_lock(id)?;
        let dir = self.root.join(id);
        if !dir.exists() {
            return Err(SpoolError::NotFound(id.to_owned()));
        }
        ensure_private_dir(&dir)?;
        let meta = read_meta(&dir)?;
        let chunks_dir = dir.join(CHUNKS_NAME);
        let chunk_dir = ChunkDir::open(&chunks_dir)?;
        let mut session = SpoolSession {
            dir,
            chunks_dir,
            chunk_dir,
            lock,
            meta,
            capacity_samples: self.capacity_samples,
            repaired: false,
            repair_conflicts: Vec::new(),
        };
        session.repair_local()?;
        Ok(session)
    }

    fn spool_lock_path(&self, id: &str) -> PathBuf {
        self.root.join(format!("{id}{LOCK_SUFFIX}"))
    }

    fn acquire_spool_lock(&self, id: &str) -> Result<LockFile> {
        match acquire_lock(&self.spool_lock_path(id))? {
            Some(lock) => Ok(lock),
            None => Err(SpoolError::Locked),
        }
    }

    pub fn list(&self) -> Result<Vec<SpoolSummary>> {
        if !self.root.exists() {
            return Ok(Vec::new());
        }
        let mut summaries = Vec::new();
        for entry in read_dir(&self.root)? {
            let name = entry.file_name();
            let Some(id) = name.to_str() else {
                continue;
            };
            if validate_id(id).is_err() {
                continue;
            }
            let path = entry.path();
            let metadata = match fs::symlink_metadata(&path) {
                Ok(metadata) => metadata,
                Err(error) => {
                    summaries.push(error_summary(id, spool_io(&path, error)));
                    continue;
                }
            };
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                continue;
            }
            match open_summary(&path, self.capacity_samples) {
                Ok(summary) => summaries.push(summary),
                Err(error) => summaries.push(error_summary(id, error)),
            }
        }
        summaries.sort_by(|a, b| a.id.cmp(&b.id));
        Ok(summaries)
    }

    fn starts_dir(&self) -> Result<PathBuf> {
        let dir = self.root.join(STARTS_NAME);
        ensure_private_dir(&dir)?;
        Ok(dir)
    }

    /// Persist K and R durably before the create POST, and hold the per-key lock
    /// for the lifetime of the returned guard.
    pub fn begin_pending_start(
        &self,
        key: &str,
        recovery: Secret,
        client_id: &str,
        title: Option<&str>,
    ) -> Result<PendingStartGuard> {
        self.begin_pending_start_as(SpoolApp::Tui, key, recovery, client_id, title)
    }

    /// As [`Self::begin_pending_start`], tagging the intent for a client.
    pub fn begin_pending_start_as(
        &self,
        app: SpoolApp,
        key: &str,
        recovery: Secret,
        client_id: &str,
        title: Option<&str>,
    ) -> Result<PendingStartGuard> {
        validate_pending_key(key)?;
        let dir = self.starts_dir()?;
        let lock_path = dir.join(format!("{key}{LOCK_SUFFIX}"));
        let lock = acquire_lock(&lock_path)?.ok_or(SpoolError::Locked)?;
        let path = dir.join(format!("{key}.start"));
        if path.exists() {
            return Err(SpoolError::Conflict(
                "a pending start with this idempotency key already exists".into(),
            ));
        }
        let record = PendingStart {
            key: key.to_owned(),
            app,
            recovery,
            client_id: client_id.to_owned(),
            title: title.map(str::to_owned),
            created_at: unix_seconds(),
        };
        write_pending(&path, &record)?;
        Ok(PendingStartGuard {
            path,
            lock,
            record,
            finished: false,
        })
    }

    pub fn pending_start_ids(&self) -> Result<Vec<String>> {
        let dir = self.starts_dir()?;
        let mut keys = Vec::new();
        for entry in read_dir(&dir)? {
            let name = entry.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            let Some(key) = name.strip_suffix(".start") else {
                continue;
            };
            if validate_pending_key(key).is_ok() {
                keys.push(key.to_owned());
            }
        }
        keys.sort();
        Ok(keys)
    }

    /// Open an unlocked pending record. `None` means another process holds the
    /// lock (or the record disappeared); the caller should skip it.
    pub fn open_pending_start(&self, key: &str) -> Result<Option<PendingStartGuard>> {
        validate_pending_key(key)?;
        let dir = self.starts_dir()?;
        let lock_path = dir.join(format!("{key}{LOCK_SUFFIX}"));
        let path = dir.join(format!("{key}.start"));
        if !path.exists() {
            return Ok(None);
        }
        let Some(lock) = acquire_lock(&lock_path)? else {
            return Ok(None);
        };
        match read_pending(&path) {
            Ok(record) => Ok(Some(PendingStartGuard {
                path,
                lock,
                record,
                finished: false,
            })),
            Err(error) => Err(error),
        }
    }

    pub fn remove_pending_start(&self, key: &str) -> Result<()> {
        validate_pending_key(key)?;
        let dir = self.starts_dir()?;
        let lock_path = dir.join(format!("{key}{LOCK_SUFFIX}"));
        let _lock = acquire_lock(&lock_path)?.ok_or(SpoolError::Locked)?;
        remove_file_if_present(&dir.join(format!("{key}.start")))?;
        sync_dir(&dir)
    }

    /// Explicit deletion after the recording and spool are no longer needed.
    /// The caller must have stopped/finished it; this is not a recovery path.
    pub fn remove(&self, id: &str) -> Result<()> {
        validate_id(id)?;
        let _lock = self.acquire_spool_lock(id)?;
        remove_spool_dir(&self.root.join(id))
    }

    /// Remove lock files whose protected data is gone. A lock that is held, or
    /// whose file was replaced after locking, is left alone: a racing opener's
    /// inode check is what makes this safe.
    pub fn prune_locks(&self) -> Result<usize> {
        if !self.root.exists() {
            return Ok(0);
        }
        let mut removed = 0usize;
        let mut seen = 0usize;
        let mut dirs = vec![self.root.clone()];
        let starts = self.root.join(STARTS_NAME);
        if starts.is_dir() {
            dirs.push(starts);
        }
        for dir in dirs {
            for entry in read_dir(&dir)? {
                seen += 1;
                if seen > MAX_SCAN_CHUNKS {
                    break;
                }
                let name = entry.file_name();
                let Some(name) = name.to_str() else {
                    continue;
                };
                let Some(stem) = name.strip_suffix(LOCK_SUFFIX) else {
                    continue;
                };
                if is_protected_lock(&self.root, &dir, stem) {
                    continue;
                }
                let path = entry.path();
                let Some(_lock) = acquire_lock(&path)? else {
                    continue;
                };
                // Re-check under the lock: another process may have created the
                // data since the scan.
                if is_protected_lock(&self.root, &dir, stem) {
                    continue;
                }
                remove_file_if_present(&path)?;
                sync_dir(&dir)?;
                removed += 1;
            }
        }
        Ok(removed)
    }

    /// Free bytes on the filesystem holding the spool root, for the caller's
    /// minimum-headroom decision. Unsupported platforms fail closed.
    #[cfg(unix)]
    pub fn free_bytes(&self) -> Result<u64> {
        use std::os::unix::ffi::OsStrExt;
        let path = CString::new(self.root.as_os_str().as_bytes())
            .map_err(|_| SpoolError::Conflict("spool root has an embedded NUL".into()))?;
        let mut stats: libc::statvfs = unsafe { std::mem::zeroed() };
        let result = unsafe { libc::statvfs(path.as_ptr(), &mut stats) };
        if result != 0 {
            return Err(spool_io(&self.root, io::Error::last_os_error()));
        }
        Ok((stats.f_bavail as u64).saturating_mul(stats.f_frsize as u64))
    }

    #[cfg(not(unix))]
    pub fn free_bytes(&self) -> Result<u64> {
        Err(SpoolError::Conflict(
            "free-space probing is unavailable on this platform".into(),
        ))
    }

    pub fn has_min_free_space(&self) -> Result<bool> {
        Ok(self.free_bytes()? >= SPOOL_MIN_FREE_BYTES)
    }
}

fn is_protected_lock(root: &Path, dir: &Path, stem: &str) -> bool {
    if dir == root {
        root.join(stem).is_dir()
    } else {
        dir.join(format!("{stem}.start")).is_file()
    }
}

fn remove_spool_dir(dir: &Path) -> Result<()> {
        match fs::symlink_metadata(&dir) {
            Ok(metadata) if metadata.file_type().is_symlink() => Err(SpoolError::Conflict(
                "refusing to delete a symlinked spool directory".into(),
            )),
            Ok(metadata) if metadata.is_dir() => {
                fs::remove_dir_all(&dir).map_err(|error| spool_io(&dir, error))
            }
            Ok(_) => Err(SpoolError::Conflict("spool path is not a directory".into())),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(spool_io(&dir, error)),
        }
}

/// One recording's durable spool. Hold it while capturing to append/ack; open it
/// after a restart to reconcile and build a recovery plan.
pub struct SpoolSession {
    dir: PathBuf,
    chunks_dir: PathBuf,
    chunk_dir: ChunkDir,
    lock: LockFile,
    meta: SpoolMeta,
    capacity_samples: u64,
    repaired: bool,
    repair_conflicts: Vec<RecoveryConflict>,
}

impl SpoolSession {
    pub fn meta(&self) -> &SpoolMeta {
        &self.meta
    }

    pub fn pending_samples(&self) -> u64 {
        self.meta.pending_samples()
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Remove this spool while its lock is still held. The sibling lock file is
    /// left for [`SpoolStore::prune_locks`].
    pub fn discard(self) -> Result<()> {
        let Self { dir, lock, .. } = self;
        remove_spool_dir(&dir)?;
        drop(lock);
        Ok(())
    }

    /// Persist the optional, bounded JSON sidecar. It is opaque here; only
    /// object-shaped JSON within the size bound is accepted.
    pub fn save_extras(&mut self, json: &str) -> Result<()> {
        validate_extras(json)?;
        crash_point("before_extras");
        atomic_write(&self.dir.join(EXTRAS_NAME), json.as_bytes())?;
        crash_point("extras_committed");
        Ok(())
    }

    pub fn load_extras(&self) -> Result<Option<String>> {
        read_extras(&self.dir)
    }

    /// Append a chunk to the durable spool. The body is raw PCM16. Success means
    /// both the chunk file and the updated metadata are fsynced and renamed.
    pub fn append_chunk(
        &mut self,
        sequence: u64,
        start: u64,
        samples: u64,
        body: &[u8],
        sha256: &str,
    ) -> Result<()> {
        self.chunk_dir.verify()?;
        if self.meta.state != SpoolState::Capturing {
            return Err(SpoolError::NotCapturing(self.meta.state));
        }
        validate_chunk_shape(samples, body)?;
        let digest = sha256_hex(body);
        if !digest.eq_ignore_ascii_case(sha256) || sha256.len() != 64 {
            return Err(SpoolError::HashMismatch {
                expected: sha256.to_owned(),
                actual: digest,
            });
        }
        let pending = self.meta.pending_samples();
        if pending.saturating_add(samples) > self.capacity_samples {
            return Err(SpoolError::SpoolFull {
                pending,
                requested: samples,
                capacity: self.capacity_samples,
            });
        }

        let name = format!("{sequence:08}.pcm");
        let path = chunk_path(&self.chunks_dir, sequence);
        match fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(SpoolError::Conflict(format!(
                    "chunk file {} is a symlink",
                    path.display()
                )));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(spool_io(&path, error)),
        }
        if path.exists() {
            let existing = read_chunk_file(&self.chunk_dir, &name, sequence)?;
            if existing.start == start
                && existing.samples == samples
                && existing.bytes == body
                && existing.sha256.eq_ignore_ascii_case(&digest)
            {
                if start == self.meta.accepted_samples {
                    self.meta.accepted_samples = start.saturating_add(samples);
                    self.meta.next_sequence = sequence.saturating_add(1);
                    self.meta.received_samples =
                        self.meta.received_samples.max(self.meta.accepted_samples);
                    write_meta(&self.dir, &self.meta)?;
                }
                return Ok(());
            }
            return Err(SpoolError::Conflict(format!(
                "chunk sequence {sequence} already exists with different content"
            )));
        }

        if sequence != self.meta.next_sequence || start != self.meta.accepted_samples {
            return Err(SpoolError::Noncontiguous {
                expected_sequence: self.meta.next_sequence,
                expected_start: self.meta.accepted_samples,
                sequence,
                start,
            });
        }

        let chunk = StoredChunk {
            sequence,
            start,
            samples,
            sha256: digest,
            bytes: body.to_vec(),
        };
        write_chunk_file(&self.chunk_dir, &chunk)?;
        crash_point("chunk_committed");
        self.meta.accepted_samples = self.meta.accepted_samples.saturating_add(samples);
        self.meta.next_sequence = sequence.saturating_add(1);
        self.meta.received_samples = self.meta.received_samples.max(self.meta.accepted_samples);
        write_meta(&self.dir, &self.meta)?;
        Ok(())
    }

    /// Record a durable Core acknowledgement. Metadata is advanced before
    /// acknowledged chunk files are deleted, so a crash between those steps
    /// leaves harmless duplicates that the next open removes.
    pub fn acknowledge(&mut self, durable_offset: u64, next_sequence: u64) -> Result<()> {
        if durable_offset < self.meta.durable_offset {
            return Err(SpoolError::Conflict(format!(
                "durable offset went backwards: {} -> {durable_offset}",
                self.meta.durable_offset
            )));
        }
        if durable_offset > self.meta.accepted_samples {
            return Err(SpoolError::Conflict(format!(
                "durable offset {durable_offset} exceeds spooled audio {}",
                self.meta.accepted_samples
            )));
        }
        self.meta.durable_offset = durable_offset;
        self.meta.next_sequence = self.meta.next_sequence.max(next_sequence);
        write_meta(&self.dir, &self.meta)?;
        remove_acked_chunks(&self.chunk_dir, durable_offset)?;
        Ok(())
    }

    /// Persist that capture ended without a Core stop acknowledgement, so only
    /// the recovery capability may upload pre-existing spooled audio.
    pub fn mark_interrupted(
        &mut self,
        final_sample_count: Option<u64>,
        received_samples: Option<u64>,
    ) -> Result<()> {
        if matches!(self.meta.state, SpoolState::Stopped | SpoolState::Recovered) {
            return Err(SpoolError::Conflict(format!(
                "cannot interrupt a {} spool",
                self.meta.state.as_str()
            )));
        }
        let final_count = final_sample_count.unwrap_or(self.meta.accepted_samples);
        if final_count < self.meta.durable_offset {
            return Err(SpoolError::Conflict(format!(
                "final sample count {final_count} is before durable offset {}",
                self.meta.durable_offset
            )));
        }
        if let Some(received) = received_samples {
            self.meta.received_samples = self.meta.received_samples.max(received);
        }
        self.meta.final_sample_count = Some(final_count);
        self.meta.state = SpoolState::Interrupted;
        write_meta(&self.dir, &self.meta)
    }

    pub fn mark_stopped(&mut self, final_sample_count: u64) -> Result<()> {
        if final_sample_count < self.meta.durable_offset {
            return Err(SpoolError::Conflict(format!(
                "final sample count {final_sample_count} is before durable offset {}",
                self.meta.durable_offset
            )));
        }
        self.meta.final_sample_count = Some(final_sample_count);
        self.meta.state = SpoolState::Stopped;
        write_meta(&self.dir, &self.meta)
    }

    pub fn mark_recovered(&mut self) -> Result<()> {
        self.meta.state = SpoolState::Recovered;
        write_meta(&self.dir, &self.meta)
    }

    pub fn chunks(&self) -> Result<Vec<StoredChunk>> {
        Ok(scan_chunks(
            &self.chunk_dir,
            self.capacity_samples,
            self.meta.durable_offset,
        )?
        .chunks)
    }

    /// Repair metadata from chunk files. `open` calls this automatically; it is
    /// also available for an explicit restart/reconciliation step.
    pub fn repair_local(&mut self) -> Result<()> {
        self.chunk_dir.verify()?;
        let scan = scan_chunks(
            &self.chunk_dir,
            self.capacity_samples,
            self.meta.durable_offset,
        )?;
        self.repair_conflicts.extend(scan.conflicts);
        let mut chunks = scan.chunks;
        chunks.sort_by_key(|chunk| (chunk.start, chunk.sequence));

        let mut accepted = self.meta.durable_offset;
        for chunk in &chunks {
            if chunk.end() <= accepted {
                continue;
            }
            if chunk.start == accepted {
                accepted = chunk.end();
            } else {
                break;
            }
        }
        if self.meta.accepted_samples > accepted {
            self.repair_conflicts.push(RecoveryConflict {
                kind: ConflictKind::MetaAhead,
                detail: format!(
                    "metadata claimed {} spooled samples but chunk files only prove {accepted}",
                    self.meta.accepted_samples
                ),
            });
        }
        self.meta.accepted_samples = accepted;
        if let Some(max_sequence) = chunks.iter().map(|chunk| chunk.sequence).max() {
            self.meta.next_sequence = self.meta.next_sequence.max(max_sequence.saturating_add(1));
        }

        if self.meta.state == SpoolState::Capturing {
            self.meta.state = SpoolState::Interrupted;
            if self.meta.final_sample_count.is_none() {
                self.meta.final_sample_count = Some(self.meta.accepted_samples);
            }
        }
        if self.meta.final_sample_count.is_none()
            && !matches!(self.meta.state, SpoolState::Recovered)
        {
            self.meta.final_sample_count = Some(self.meta.accepted_samples);
        }
        if let Some(final_count) = self.meta.final_sample_count {
            if final_count < self.meta.durable_offset {
                self.repair_conflicts.push(RecoveryConflict {
                    kind: ConflictKind::FinalBeforeDurable,
                    detail: format!(
                        "final sample count {final_count} is before durable {}",
                        self.meta.durable_offset
                    ),
                });
                self.meta.final_sample_count = Some(self.meta.durable_offset);
            }
        }

        self.meta.received_samples = self.meta.received_samples.max(self.meta.accepted_samples);
        write_meta(&self.dir, &self.meta)?;
        remove_acked_chunks(&self.chunk_dir, self.meta.durable_offset)?;
        self.repaired = true;
        Ok(())
    }

    /// Build a recovery-only plan. Without a server baseline the local
    /// checkpoint is used; after a restart the driver should pass Core's
    /// authoritative durable offset, chunk count and sealed final count.
    pub fn reconcile(&mut self, baseline: Option<ServerBaseline>) -> Result<RecoveryPlan> {
        if !self.repaired {
            self.repair_local()?;
        }
        self.chunk_dir.verify()?;
        let scan = scan_chunks(
            &self.chunk_dir,
            self.capacity_samples,
            self.meta.durable_offset,
        )?;
        let mut conflicts = std::mem::take(&mut self.repair_conflicts);
        conflicts.extend(scan.conflicts);
        let baseline = baseline.unwrap_or_else(|| ServerBaseline::local(&self.meta, &scan.chunks));

        let local_final = self.meta.final_sample_count;
        if let (Some(local), Some(core)) = (local_final, baseline.final_sample_count) {
            if local != core {
                conflicts.push(RecoveryConflict {
                    kind: ConflictKind::FinalCountMismatch,
                    detail: format!(
                        "local final sample count {local} disagrees with Core's sealed count {core}; Core is authoritative"
                    ),
                });
            }
        }
        // A sealed Core final count is authoritative. Never prefer a local
        // count that could make recovery claim a different recording length.
        let mut final_sample_count = baseline
            .final_sample_count
            .or(local_final)
            .unwrap_or(self.meta.accepted_samples);
        if baseline.sealed && baseline.final_sample_count.is_none() {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::FinalCountMismatch,
                detail: "Core reports a sealed recording without a final sample count".into(),
            });
        }
        if final_sample_count < baseline.durable_offset {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::FinalBeforeDurable,
                detail: format!(
                    "final sample count {final_sample_count} is before server durable {}",
                    baseline.durable_offset
                ),
            });
            final_sample_count = baseline.durable_offset;
        }
        let needs_seal = !baseline.sealed
            && !matches!(self.meta.state, SpoolState::Stopped | SpoolState::Recovered);

        let mut uploads = Vec::new();
        let mut gaps = Vec::new();
        let mut unsaved_samples = 0u64;

        if self.meta.state == SpoolState::Recovered {
            // Already recovered; no upload is needed. Keep the plan for status.
        } else if baseline.durable_offset < self.meta.durable_offset {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::ServerBehindLocal,
                detail: format!(
                    "server durable offset {} is behind local durable {}",
                    baseline.durable_offset, self.meta.durable_offset
                ),
            });
        } else {
            let mut expected_start = baseline.durable_offset;
            let mut expected_sequence = baseline.next_sequence;
            for chunk in &scan.chunks {
                if chunk.end() <= expected_start {
                    continue;
                }
                if chunk.start < expected_start {
                    conflicts.push(RecoveryConflict {
                        kind: ConflictKind::BaselineInsideChunk,
                        detail: format!(
                            "server durable offset {expected_start} falls inside chunk sequence {} ({}..{})",
                            chunk.sequence,
                            chunk.start,
                            chunk.end()
                        ),
                    });
                    break;
                }
                if chunk.start > expected_start {
                    gaps.push(RecoveryGap {
                        start: expected_start,
                        samples: chunk.start - expected_start,
                        cause: GapCause::MissingSpooled,
                    });
                    break;
                }
                if chunk.sequence != expected_sequence {
                    conflicts.push(RecoveryConflict {
                        kind: ConflictKind::SequenceMismatch,
                        detail: format!(
                            "chunk start {} has sequence {}, expected {expected_sequence}",
                            chunk.start, chunk.sequence
                        ),
                    });
                    break;
                }
                if chunk.end() > final_sample_count {
                    conflicts.push(RecoveryConflict {
                        kind: ConflictKind::BeyondFinal,
                        detail: format!(
                            "chunk sequence {} ends at {}, beyond final {final_sample_count}",
                            chunk.sequence,
                            chunk.end()
                        ),
                    });
                    break;
                }
                expected_start = chunk.end();
                expected_sequence = expected_sequence.saturating_add(1);
                uploads.push(chunk.clone());
            }

            if final_sample_count > expected_start {
                unsaved_samples = final_sample_count - expected_start;
                if let Some(gap) = gaps.first_mut() {
                    // Nothing after the first gap can be recovered in order, so
                    // report the whole unrecovered suffix, including spooled
                    // chunks stranded beyond the gap.
                    gap.samples = final_sample_count.saturating_sub(gap.start);
                } else {
                    gaps.push(RecoveryGap {
                        start: expected_start,
                        samples: unsaved_samples,
                        cause: if final_sample_count > self.meta.accepted_samples {
                            GapCause::UnsavedTail
                        } else {
                            GapCause::MissingSpooled
                        },
                    });
                }
            }
        }

        Ok(RecoveryPlan {
            id: self.meta.id.clone(),
            client_id: self.meta.client_id.clone(),
            generation: self.meta.generation,
            recovery_token: self.meta.recovery_token.clone(),
            state: self.meta.state,
            baseline,
            final_sample_count,
            needs_seal,
            uploads,
            gaps,
            conflicts,
            unsaved_samples,
        })
    }
}

/// List every spool summary. Corrupt sessions are reported with `error`, not
/// hidden and not guessed back into a usable state.
fn open_summary(dir: &Path, capacity_samples: u64) -> Result<SpoolSummary> {
    let meta = read_meta(dir)?;
    if meta.id
        != dir
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or_default()
    {
        return Err(SpoolError::InvalidMeta(
            "directory name does not match metadata id".into(),
        ));
    }
    let chunk_dir = ChunkDir::open(&dir.join(CHUNKS_NAME))?;
    let scan = scan_chunks(&chunk_dir, capacity_samples, meta.durable_offset)?;
    let error = (!scan.conflicts.is_empty()).then(|| {
        scan.conflicts
            .iter()
            .map(|conflict| conflict.detail.clone())
            .collect::<Vec<_>>()
            .join("; ")
    });
    Ok(SpoolSummary {
        id: meta.id.clone(),
        app: Some(meta.app),
        client_id: Some(meta.client_id.clone()),
        state: Some(meta.state),
        durable_offset: meta.durable_offset,
        accepted_samples: meta.accepted_samples,
        final_sample_count: meta.final_sample_count,
        pending_samples: meta.pending_samples(),
        chunks: scan.chunks.len(),
        error,
    })
}

fn error_summary(id: &str, error: SpoolError) -> SpoolSummary {
    SpoolSummary {
        id: id.to_owned(),
        app: None,
        client_id: None,
        state: None,
        durable_offset: 0,
        accepted_samples: 0,
        final_sample_count: None,
        pending_samples: 0,
        chunks: 0,
        error: Some(error.to_string()),
    }
}

fn validate_meta(meta: &SpoolMeta) -> Result<()> {
    validate_id(&meta.id)?;
    if meta.version != META_VERSION {
        return Err(SpoolError::InvalidMeta(format!(
            "unsupported version {}",
            meta.version
        )));
    }
    if meta.client_id.trim().is_empty() || meta.client_id.len() > 128 {
        return Err(SpoolError::InvalidMeta("invalid client id".into()));
    }
    let token_len = meta.recovery_token.expose().len();
    if !(16..=512).contains(&token_len) {
        return Err(SpoolError::InvalidMeta("invalid recovery token".into()));
    }
    if meta
        .stream_id
        .as_ref()
        .is_some_and(|value| value.len() > 128)
    {
        return Err(SpoolError::InvalidMeta("invalid stream id".into()));
    }
    if meta.durable_offset > meta.accepted_samples {
        return Err(SpoolError::InvalidMeta(
            "durable offset exceeds accepted samples".into(),
        ));
    }
    if let Some(final_count) = meta.final_sample_count {
        if final_count < meta.durable_offset {
            return Err(SpoolError::InvalidMeta(
                "final sample count is before durable offset".into(),
            ));
        }
    }
    Ok(())
}

fn validate_id(id: &str) -> Result<()> {
    if id.len() == 32
        && id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        Ok(())
    } else {
        Err(SpoolError::InvalidId)
    }
}

fn validate_chunk_shape(samples: u64, body: &[u8]) -> Result<()> {
    if samples == 0 {
        return Err(SpoolError::InvalidChunk("chunk is empty".into()));
    }
    let expected = usize::try_from(samples)
        .ok()
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| SpoolError::InvalidChunk("sample count is too large".into()))?;
    if body.len() != expected {
        return Err(SpoolError::InvalidChunk(format!(
            "body is {} bytes for {samples} samples",
            body.len()
        )));
    }
    if body.len() > MAX_CHUNK_BYTES {
        return Err(SpoolError::InvalidChunk(format!(
            "body exceeds {MAX_CHUNK_BYTES} bytes"
        )));
    }
    Ok(())
}

fn spool_io(path: &Path, error: io::Error) -> SpoolError {
    SpoolError::Io {
        path: path.display().to_string(),
        message: error.to_string(),
    }
}

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs())
}

fn chunk_path(chunks_dir: &Path, sequence: u64) -> PathBuf {
    chunks_dir.join(format!("{sequence:08}.pcm"))
}

fn validate_pending_key(key: &str) -> Result<()> {
    if key.is_empty()
        || key.len() > 128
        || !key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        return Err(SpoolError::Conflict(
            "invalid pending-start idempotency key".into(),
        ));
    }
    Ok(())
}

fn open_lock_file(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(unix)]
    {
        options.mode(0o600);
        options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let file = options.open(path).map_err(|error| spool_io(path, error))?;
    #[cfg(unix)]
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|error| spool_io(path, error))?;
    Ok(file)
}

/// A held exclusive lock. The OS lock is released on drop; the lock file stays.
struct LockFile {
    path: PathBuf,
    file: File,
}

impl Drop for LockFile {
    fn drop(&mut self) {
        let _ = self.file.unlock();
        if let Ok(mut held) = LOCK_PATHS.lock() {
            held.remove(&self.path);
        }
    }
}

/// Acquire an exclusive lock for `path`. `None` means another recorder (in this
/// process or another) holds it. The lock file is created if absent and never
/// unlinked here.
fn acquire_lock(path: &Path) -> Result<Option<LockFile>> {
    for attempt in 0..2u32 {
        {
            let mut held = LOCK_PATHS
                .lock()
                .map_err(|_| SpoolError::Conflict("spool lock registry is poisoned".into()))?;
            if held.contains(path) {
                return Ok(None);
            }
            held.insert(path.to_path_buf());
        }
        let outcome = try_acquire_once(path, attempt);
        match outcome {
            Ok(AttemptedLock::Acquired(lock)) => return Ok(Some(lock)),
            Ok(AttemptedLock::Held) => {
                release_reservation(path);
                return Ok(None);
            }
            Ok(AttemptedLock::Replaced) => {
                release_reservation(path);
            }
            Err(error) => {
                release_reservation(path);
                return Err(error);
            }
        }
    }
    Err(SpoolError::Conflict(
        "lock file was replaced while it was locked".into(),
    ))
}

enum AttemptedLock {
    Acquired(LockFile),
    Held,
    Replaced,
}

fn try_acquire_once(path: &Path, attempt: u32) -> Result<AttemptedLock> {
    let file = open_lock_file(path)?;
    if !try_lock_exclusive(&file, path)? {
        return Ok(AttemptedLock::Held);
    }
    #[cfg(test)]
    maybe_replace_lock_for_test(path, attempt);
    if !lock_identity_matches(&file, path)? {
        return Ok(AttemptedLock::Replaced);
    }
    Ok(AttemptedLock::Acquired(LockFile {
        path: path.to_path_buf(),
        file,
    }))
}

fn release_reservation(path: &Path) {
    if let Ok(mut held) = LOCK_PATHS.lock() {
        held.remove(path);
    }
}

fn try_lock_exclusive(file: &File, path: &Path) -> Result<bool> {
    match file.try_lock() {
        Ok(()) => Ok(true),
        Err(TryLockError::WouldBlock) => Ok(false),
        Err(TryLockError::Error(error)) => Err(spool_io(path, error)),
    }
}

#[cfg(unix)]
fn lock_identity_matches(file: &File, path: &Path) -> Result<bool> {
    let held = file.metadata().map_err(|error| spool_io(path, error))?;
    let current = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(spool_io(path, error)),
    };
    Ok(current.is_file() && current.dev() == held.dev() && current.ino() == held.ino())
}

#[cfg(not(unix))]
fn lock_identity_matches(file: &File, path: &Path) -> Result<bool> {
    let _ = file;
    Ok(fs::symlink_metadata(path).is_ok_and(|metadata| metadata.is_file()))
}

#[cfg(test)]
thread_local! {
    static REPLACE_LOCK_ATTEMPTS: std::cell::Cell<Option<u32>> =
        const { std::cell::Cell::new(None) };
}

#[cfg(test)]
fn maybe_replace_lock_for_test(path: &Path, attempt: u32) {
    let replace = REPLACE_LOCK_ATTEMPTS.with(|value| {
        value
            .get()
            .is_some_and(|attempts| attempt < attempts)
    });
    if replace {
        let old = path.with_file_name(format!(
            "{}.replaced-{attempt}",
            path.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("lock")
        ));
        let _ = fs::rename(path, &old);
        let _ = fs::write(path, b"replacement");
    }
}

fn remove_file_if_present(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(spool_io(path, error)),
    }
}

fn write_pending(path: &Path, record: &PendingStart) -> Result<()> {
    atomic_write(path, encode_pending(record).as_bytes())
}

fn read_pending(path: &Path) -> Result<PendingStart> {
    reject_symlink(path)?;
    let text = fs::read_to_string(path).map_err(|error| spool_io(path, error))?;
    decode_pending(&text)
}

fn encode_pending(record: &PendingStart) -> String {
    let mut out = String::new();
    out.push_str("version=1\n");
    out.push_str(&format!("key={}\n", hex_string(&record.key)));
    out.push_str(&format!("app={}\n", record.app.as_str()));
    out.push_str(&format!(
        "recovery={}\n",
        hex_string(record.recovery.expose())
    ));
    out.push_str(&format!("client_id={}\n", hex_string(&record.client_id)));
    out.push_str(&format!(
        "title={}\n",
        record
            .title
            .as_deref()
            .map(hex_string)
            .unwrap_or_else(|| "-".into())
    ));
    out.push_str(&format!("created_at={}\n", record.created_at));
    out
}

fn decode_pending(text: &str) -> Result<PendingStart> {
    let mut fields = BTreeMap::new();
    for line in text.lines() {
        let Some((key, value)) = line.split_once('=') else {
            return Err(SpoolError::InvalidMeta("malformed pending start".into()));
        };
        fields.insert(key.to_owned(), value.to_owned());
    }
    let required = |key: &str| -> Result<&str> {
        fields
            .get(key)
            .map(String::as_str)
            .ok_or_else(|| SpoolError::InvalidMeta(format!("pending start is missing {key}")))
    };
    if required("version")? != "1" {
        return Err(SpoolError::InvalidMeta(
            "unsupported pending-start version".into(),
        ));
    }
    let title = required("title")?;
    let record = PendingStart {
        key: from_hex_string(required("key")?)?,
        app: match fields.get("app") {
            Some(value) => SpoolApp::parse(value)?,
            None => SpoolApp::Tui,
        },
        recovery: Secret::new(from_hex_string(required("recovery")?)?),
        client_id: from_hex_string(required("client_id")?)?,
        title: if title == "-" {
            None
        } else {
            Some(from_hex_string(title)?)
        },
        created_at: required("created_at")?
            .parse()
            .map_err(|_| SpoolError::InvalidMeta("bad pending-start time".into()))?,
    };
    validate_pending_key(&record.key)?;
    if record.recovery.is_empty() {
        return Err(SpoolError::InvalidMeta(
            "pending start has no recovery capability".into(),
        ));
    }
    Ok(record)
}

/// An opened chunks directory. File operations use `openat` relative to this
/// handle (Unix), so a path swapped for a symlink after the handle is opened
/// cannot redirect reads, writes, renames or unlinks. `verify` still checks the
/// path identity so a swapped directory is reported rather than silently used.
struct ChunkDir {
    path: PathBuf,
    file: File,
}

impl ChunkDir {
    fn open(path: &Path) -> Result<Self> {
        let identity = match fs::symlink_metadata(path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(SpoolError::Conflict(format!(
                    "{} is a symlink",
                    path.display()
                )));
            }
            Ok(metadata) if !metadata.is_dir() => {
                return Err(SpoolError::Conflict(format!(
                    "{} is not a directory",
                    path.display()
                )));
            }
            #[cfg(unix)]
            Ok(metadata) => Some((metadata.dev(), metadata.ino())),
            #[cfg(not(unix))]
            Ok(_) => None,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return Err(SpoolError::NotFound(path.display().to_string()));
            }
            Err(error) => return Err(spool_io(path, error)),
        };
        Self::open_checked(path, identity)
    }

    fn open_checked(path: &Path, expected_identity: Option<(u64, u64)>) -> Result<Self> {
        #[cfg(unix)]
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|error| spool_io(path, error))?;
        #[cfg(not(unix))]
        let file = File::open(path).map_err(|error| spool_io(path, error))?;
        let metadata = file.metadata().map_err(|error| spool_io(path, error))?;
        if !metadata.is_dir() {
            return Err(SpoolError::Conflict(format!(
                "{} is not a directory",
                path.display()
            )));
        }
        #[cfg(unix)]
        if let Some((expected_dev, expected_ino)) = expected_identity {
            if metadata.dev() != expected_dev || metadata.ino() != expected_ino {
                return Err(SpoolError::Conflict(format!(
                    "{} changed between the precheck and open",
                    path.display()
                )));
            }
        }
        Ok(Self {
            path: path.to_path_buf(),
            file,
        })
    }

    fn path(&self) -> &Path {
        &self.path
    }

    fn verify(&self) -> Result<()> {
        let metadata =
            fs::symlink_metadata(&self.path).map_err(|error| spool_io(&self.path, error))?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(SpoolError::Conflict(format!(
                "{} was replaced by a symlink or non-directory",
                self.path.display()
            )));
        }
        #[cfg(unix)]
        {
            let held = self
                .file
                .metadata()
                .map_err(|error| spool_io(&self.path, error))?;
            if metadata.dev() != held.dev() || metadata.ino() != held.ino() {
                return Err(SpoolError::Conflict(format!(
                    "{} was replaced during the spool operation",
                    self.path.display()
                )));
            }
        }
        Ok(())
    }

    fn file_name(name: &str) -> Result<CString> {
        if name.is_empty() || name == "." || name == ".." || name.contains('/') {
            return Err(SpoolError::InvalidChunk("unsafe chunk file name".into()));
        }
        CString::new(name).map_err(|_| SpoolError::InvalidChunk("unsafe chunk file name".into()))
    }

    fn read_file(&self, name: &str) -> Result<File> {
        self.verify()?;
        let name = Self::file_name(name)?;
        #[cfg(unix)]
        {
            let fd = unsafe {
                libc::openat(
                    self.file.as_raw_fd(),
                    name.as_ptr(),
                    libc::O_RDONLY
                        | libc::O_NOFOLLOW
                        | libc::O_NONBLOCK
                        | libc::O_NOCTTY
                        | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(spool_io(&self.path, io::Error::last_os_error()));
            }
            // SAFETY: fd is a fresh owned descriptor from openat.
            Ok(unsafe { File::from_raw_fd(fd) })
        }
        #[cfg(not(unix))]
        {
            let path = self.path.join(name.to_string_lossy().as_ref());
            reject_symlink(&path)?;
            let metadata = fs::symlink_metadata(&path).map_err(|error| spool_io(&path, error))?;
            if !metadata.is_file() {
                return Err(SpoolError::InvalidChunk(
                    "chunk is not a regular file".into(),
                ));
            }
            File::open(&path).map_err(|error| spool_io(&path, error))
        }
    }

    fn write_atomic(&self, name: &str, bytes: &[u8]) -> Result<()> {
        self.verify()?;
        let name = Self::file_name(name)?;
        let temp_name = format!(
            "{TEMP_PREFIX}{}-{}-{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed),
            name.to_string_lossy()
        );
        let temp = Self::file_name(&temp_name)?;
        #[cfg(unix)]
        {
            let fd = unsafe {
                libc::openat(
                    self.file.as_raw_fd(),
                    temp.as_ptr(),
                    libc::O_WRONLY
                        | libc::O_CREAT
                        | libc::O_EXCL
                        | libc::O_NOFOLLOW
                        | libc::O_CLOEXEC,
                    0o600 as libc::c_uint,
                )
            };
            if fd < 0 {
                return Err(spool_io(&self.path, io::Error::last_os_error()));
            }
            // SAFETY: fd is a fresh owned descriptor from openat.
            let mut file = unsafe { File::from_raw_fd(fd) };
            let result = (|| {
                file.write_all(bytes)
                    .map_err(|error| spool_io(&self.path, error))?;
                crash_point("tmp_written");
                file.sync_all()
                    .map_err(|error| spool_io(&self.path, error))?;
                crash_point("tmp_fsynced");
                file.set_permissions(fs::Permissions::from_mode(0o600))
                    .map_err(|error| spool_io(&self.path, error))?;
                let renamed = unsafe {
                    libc::renameat(
                        self.file.as_raw_fd(),
                        temp.as_ptr(),
                        self.file.as_raw_fd(),
                        name.as_ptr(),
                    )
                };
                if renamed != 0 {
                    return Err(spool_io(&self.path, io::Error::last_os_error()));
                }
                crash_point("chunk_renamed");
                crash_point("before_chunk_dir_sync");
                self.file
                    .sync_all()
                    .map_err(|error| spool_io(&self.path, error))
            })();
            if result.is_err() {
                let _ = unsafe { libc::unlinkat(self.file.as_raw_fd(), temp.as_ptr(), 0) };
            }
            result
        }
        #[cfg(not(unix))]
        {
            let _ = temp;
            atomic_write(&self.path.join(name.to_string_lossy().as_ref()), bytes)
        }
    }

    fn remove_file(&self, name: &str) -> Result<()> {
        self.verify()?;
        let name = Self::file_name(name)?;
        #[cfg(unix)]
        {
            let removed = unsafe { libc::unlinkat(self.file.as_raw_fd(), name.as_ptr(), 0) };
            if removed != 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::NotFound {
                    return Ok(());
                }
                return Err(spool_io(&self.path, error));
            }
            Ok(())
        }
        #[cfg(not(unix))]
        {
            match fs::remove_file(self.path.join(name.to_string_lossy().as_ref())) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(spool_io(&self.path, error)),
            }
        }
    }

    fn sync(&self) -> Result<()> {
        self.file
            .sync_all()
            .map_err(|error| spool_io(&self.path, error))
    }
}

fn ensure_private_dir(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(SpoolError::Conflict(format!(
                "{} is a symlink",
                path.display()
            )));
        }
        Ok(metadata) if !metadata.is_dir() => {
            return Err(SpoolError::Conflict(format!(
                "{} is not a directory",
                path.display()
            )));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            fs::create_dir_all(path).map_err(|error| spool_io(path, error))?;
        }
        Err(error) => return Err(spool_io(path, error)),
    }
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .map_err(|error| spool_io(path, error))?;
    Ok(())
}

fn sync_dir(path: &Path) -> Result<()> {
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|error| spool_io(path, error))
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let Some(parent) = path.parent() else {
        return Err(SpoolError::Conflict("path has no parent".into()));
    };
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return Err(SpoolError::Conflict("path has no file name".into()));
    };
    let temp = parent.join(format!(
        "{TEMP_PREFIX}{}-{}-{name}",
        std::process::id(),
        TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let mut file = options
            .open(&temp)
            .map_err(|error| spool_io(&temp, error))?;
        file.write_all(bytes)
            .map_err(|error| spool_io(&temp, error))?;
        crash_point("tmp_written");
        file.sync_all().map_err(|error| spool_io(&temp, error))?;
        crash_point("tmp_fsynced");
        #[cfg(unix)]
        fs::set_permissions(&temp, fs::Permissions::from_mode(0o600))
            .map_err(|error| spool_io(&temp, error))?;
        fs::rename(&temp, path).map_err(|error| spool_io(path, error))?;
        crash_point("renamed");
        crash_point("before_dir_sync");
        #[cfg(unix)]
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| spool_io(path, error))?;
        sync_dir(parent)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

fn write_meta(dir: &Path, meta: &SpoolMeta) -> Result<()> {
    validate_meta(meta)?;
    atomic_write(&dir.join(META_NAME), encode_meta(meta).as_bytes())
}

fn read_meta(dir: &Path) -> Result<SpoolMeta> {
    let path = dir.join(META_NAME);
    reject_symlink(&path)?;
    let text = fs::read_to_string(&path).map_err(|error| {
        if error.kind() == io::ErrorKind::NotFound {
            SpoolError::InvalidMeta(format!("{} is missing", path.display()))
        } else {
            spool_io(&path, error)
        }
    })?;
    decode_meta(&text)
}

fn read_extras(dir: &Path) -> Result<Option<String>> {
    let path = dir.join(EXTRAS_NAME);
    reject_symlink(&path)?;
    let metadata = match fs::metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(spool_io(&path, error)),
    };
    if !metadata.is_file() || metadata.len() > MAX_EXTRAS_BYTES as u64 {
        return Err(SpoolError::InvalidMeta("invalid recorder extras file".into()));
    }
    let bytes = fs::read(&path).map_err(|error| spool_io(&path, error))?;
    if bytes.len() > MAX_EXTRAS_BYTES {
        return Err(SpoolError::InvalidMeta("invalid recorder extras file".into()));
    }
    let text =
        String::from_utf8(bytes).map_err(|_| SpoolError::InvalidMeta("extras is not UTF-8".into()))?;
    validate_extras(&text)?;
    Ok(Some(text))
}

fn validate_extras(json: &str) -> Result<()> {
    if json.len() > MAX_EXTRAS_BYTES {
        return Err(SpoolError::Conflict(format!(
            "recorder extras exceed {MAX_EXTRAS_BYTES} bytes"
        )));
    }
    let mut check = JsonCheck {
        bytes: json.as_bytes(),
        index: 0,
    };
    check.skip_ws();
    if check.peek() != Some(b'{') {
        return Err(SpoolError::Conflict(
            "recorder extras must be a JSON object".into(),
        ));
    }
    check.value(0)?;
    check.skip_ws();
    if check.index != check.bytes.len() {
        return Err(SpoolError::Conflict(
            "recorder extras has trailing JSON".into(),
        ));
    }
    Ok(())
}

/// A small, bounded JSON syntax check for the opaque extras sidecar. The spool
/// never interprets the fields; it only guarantees a single object-shaped value.
struct JsonCheck<'a> {
    bytes: &'a [u8],
    index: usize,
}

impl JsonCheck<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.index).copied()
    }

    fn bump(&mut self) -> Option<u8> {
        let byte = self.peek()?;
        self.index += 1;
        Some(byte)
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.index += 1;
        }
    }

    fn bad(&self) -> SpoolError {
        SpoolError::Conflict(format!(
            "recorder extras is not valid JSON at byte {}",
            self.index
        ))
    }

    fn value(&mut self, depth: usize) -> Result<()> {
        if depth > 32 {
            return Err(SpoolError::Conflict(
                "recorder extras is nested too deeply".into(),
            ));
        }
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.object(depth + 1),
            Some(b'[') => self.array(depth + 1),
            Some(b'"') => self.string(),
            Some(b't') => self.literal(b"true"),
            Some(b'f') => self.literal(b"false"),
            Some(b'n') => self.literal(b"null"),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.bad()),
        }
    }

    fn object(&mut self, depth: usize) -> Result<()> {
        self.bump();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.bump();
            return Ok(());
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(self.bad());
            }
            self.string()?;
            self.skip_ws();
            if self.bump() != Some(b':') {
                return Err(self.bad());
            }
            self.value(depth)?;
            self.skip_ws();
            match self.bump() {
                Some(b',') => continue,
                Some(b'}') => return Ok(()),
                _ => return Err(self.bad()),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Result<()> {
        self.bump();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.bump();
            return Ok(());
        }
        loop {
            self.value(depth)?;
            self.skip_ws();
            match self.bump() {
                Some(b',') => continue,
                Some(b']') => return Ok(()),
                _ => return Err(self.bad()),
            }
        }
    }

    fn string(&mut self) -> Result<()> {
        if self.bump() != Some(b'"') {
            return Err(self.bad());
        }
        loop {
            match self.bump() {
                Some(b'"') => return Ok(()),
                Some(b'\\') => match self.bump() {
                    Some(b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't') => {}
                    Some(b'u') => {
                        for _ in 0..4 {
                            if !matches!(self.bump(), Some(b'0'..=b'9' | b'a'..=b'f' | b'A'..=b'F'))
                            {
                                return Err(self.bad());
                            }
                        }
                    }
                    _ => return Err(self.bad()),
                },
                Some(byte) if byte >= 0x20 => {}
                _ => return Err(self.bad()),
            }
        }
    }

    fn literal(&mut self, literal: &[u8]) -> Result<()> {
        for expected in literal {
            if self.bump() != Some(*expected) {
                return Err(self.bad());
            }
        }
        Ok(())
    }

    fn number(&mut self) -> Result<()> {
        if self.peek() == Some(b'-') {
            self.bump();
        }
        match self.bump() {
            Some(b'0') => {}
            Some(b'1'..=b'9') => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.index += 1;
                }
            }
            _ => return Err(self.bad()),
        }
        if self.peek() == Some(b'.') {
            self.bump();
            if !matches!(self.bump(), Some(b'0'..=b'9')) {
                return Err(self.bad());
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.index += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.bump();
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.bump();
            }
            if !matches!(self.bump(), Some(b'0'..=b'9')) {
                return Err(self.bad());
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.index += 1;
            }
        }
        Ok(())
    }
}

fn encode_meta(meta: &SpoolMeta) -> String {
    let mut out = String::new();
    let line = |out: &mut String, key: &str, value: &str| {
        out.push_str(key);
        out.push('=');
        out.push_str(value);
        out.push('\n');
    };
    line(&mut out, "version", &meta.version.to_string());
    line(&mut out, "id", &hex_string(&meta.id));
    line(&mut out, "app", meta.app.as_str());
    line(&mut out, "client_id", &hex_string(&meta.client_id));
    line(&mut out, "generation", &meta.generation.to_string());
    line(
        &mut out,
        "recovery_token",
        &hex_string(meta.recovery_token.expose()),
    );
    line(
        &mut out,
        "stream_id",
        &meta
            .stream_id
            .as_deref()
            .map(hex_string)
            .unwrap_or_else(|| "-".into()),
    );
    line(&mut out, "durable_offset", &meta.durable_offset.to_string());
    line(&mut out, "next_sequence", &meta.next_sequence.to_string());
    line(
        &mut out,
        "accepted_samples",
        &meta.accepted_samples.to_string(),
    );
    line(
        &mut out,
        "received_samples",
        &meta.received_samples.to_string(),
    );
    line(
        &mut out,
        "final_sample_count",
        &meta
            .final_sample_count
            .map(|value| value.to_string())
            .unwrap_or_else(|| "-".into()),
    );
    line(&mut out, "state", meta.state.as_str());
    line(&mut out, "created_at", &meta.created_at.to_string());
    out
}

fn decode_meta(text: &str) -> Result<SpoolMeta> {
    let mut fields = BTreeMap::new();
    for line in text.lines() {
        let Some((key, value)) = line.split_once('=') else {
            return Err(SpoolError::InvalidMeta("malformed line".into()));
        };
        if fields.insert(key.to_owned(), value.to_owned()).is_some() {
            return Err(SpoolError::InvalidMeta(format!("duplicate key {key}")));
        }
    }
    let required = |key: &str| -> Result<&str> {
        fields
            .get(key)
            .map(String::as_str)
            .ok_or_else(|| SpoolError::InvalidMeta(format!("missing key {key}")))
    };
    let number = |key: &str| -> Result<u64> {
        required(key)?
            .parse()
            .map_err(|_| SpoolError::InvalidMeta(format!("bad number for {key}")))
    };
    let optional_hex = |key: &str| -> Result<Option<String>> {
        let value = required(key)?;
        if value == "-" {
            Ok(None)
        } else {
            Ok(Some(from_hex_string(value)?))
        }
    };
    let meta = SpoolMeta {
        version: required("version")?
            .parse()
            .map_err(|_| SpoolError::InvalidMeta("bad version".into()))?,
        id: from_hex_string(required("id")?)?,
        app: match fields.get("app") {
            Some(value) => SpoolApp::parse(value)?,
            None => SpoolApp::Tui,
        },
        client_id: from_hex_string(required("client_id")?)?,
        generation: number("generation")?,
        recovery_token: Secret::new(from_hex_string(required("recovery_token")?)?),
        stream_id: optional_hex("stream_id")?,
        durable_offset: number("durable_offset")?,
        next_sequence: number("next_sequence")?,
        accepted_samples: number("accepted_samples")?,
        received_samples: number("received_samples")?,
        final_sample_count: {
            let value = required("final_sample_count")?;
            if value == "-" {
                None
            } else {
                Some(
                    value
                        .parse()
                        .map_err(|_| SpoolError::InvalidMeta("bad final count".into()))?,
                )
            }
        },
        state: SpoolState::parse(required("state")?)?,
        created_at: number("created_at")?,
    };
    validate_meta(&meta)?;
    Ok(meta)
}

fn reject_symlink(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => Err(SpoolError::Conflict(format!(
            "{} is a symlink",
            path.display()
        ))),
        Ok(_) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(spool_io(path, error)),
    }
}

fn read_dir(path: &Path) -> Result<Vec<fs::DirEntry>> {
    fs::read_dir(path)
        .map_err(|error| spool_io(path, error))?
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(|error| spool_io(path, error))
}

fn write_chunk_file(chunk_dir: &ChunkDir, chunk: &StoredChunk) -> Result<()> {
    validate_chunk_shape(chunk.samples, &chunk.bytes)?;
    if chunk.sha256.len() != 64 {
        return Err(SpoolError::InvalidChunk("bad sha256".into()));
    }
    let digest = sha256_hex(&chunk.bytes);
    if !digest.eq_ignore_ascii_case(&chunk.sha256) {
        return Err(SpoolError::HashMismatch {
            expected: chunk.sha256.clone(),
            actual: digest,
        });
    }
    let name = format!("{:08}.pcm", chunk.sequence);
    chunk_dir.write_atomic(&name, &encode_chunk(chunk)?)
}

fn encode_chunk(chunk: &StoredChunk) -> Result<Vec<u8>> {
    let digest = hex_to_bytes(&chunk.sha256)?;
    let mut out = Vec::with_capacity(CHUNK_HEADER_LEN + chunk.bytes.len());
    out.extend_from_slice(CHUNK_MAGIC);
    out.extend_from_slice(&CHUNK_VERSION.to_le_bytes());
    out.extend_from_slice(&chunk.sequence.to_le_bytes());
    out.extend_from_slice(&chunk.start.to_le_bytes());
    out.extend_from_slice(&chunk.samples.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&digest);
    debug_assert_eq!(out.len(), CHUNK_HEADER_LEN);
    out.extend_from_slice(&chunk.bytes);
    Ok(out)
}

fn read_chunk_file(
    chunk_dir: &ChunkDir,
    name: &str,
    expected_sequence: u64,
) -> Result<StoredChunk> {
    let mut file = chunk_dir.read_file(name)?;
    let max_size = (CHUNK_HEADER_LEN + MAX_CHUNK_BYTES) as u64;
    let metadata = file
        .metadata()
        .map_err(|error| spool_io(chunk_dir.path(), error))?;
    if !metadata.is_file() {
        return Err(SpoolError::InvalidChunk(
            "chunk is not a regular file".into(),
        ));
    }
    let size = metadata.len();
    if size > max_size {
        return Err(SpoolError::ChunkTooLarge {
            size,
            max: max_size,
        });
    }
    let mut data = Vec::with_capacity(size.min(max_size) as usize);
    Read::by_ref(&mut file)
        .take(max_size + 1)
        .read_to_end(&mut data)
        .map_err(|error| spool_io(chunk_dir.path(), error))?;
    if data.len() as u64 > max_size {
        return Err(SpoolError::ChunkTooLarge {
            size: data.len() as u64,
            max: max_size,
        });
    }
    if data.len() < CHUNK_HEADER_LEN {
        return Err(SpoolError::InvalidChunk(
            "file is shorter than its header".into(),
        ));
    }
    if &data[..8] != CHUNK_MAGIC {
        return Err(SpoolError::InvalidChunk("bad chunk magic".into()));
    }
    let version = u32::from_le_bytes(data[8..12].try_into().expect("four bytes"));
    if version != CHUNK_VERSION {
        return Err(SpoolError::InvalidChunk(format!(
            "unsupported chunk version {version}"
        )));
    }
    let sequence = u64::from_le_bytes(data[12..20].try_into().expect("eight bytes"));
    let start = u64::from_le_bytes(data[20..28].try_into().expect("eight bytes"));
    let samples = u64::from_le_bytes(data[28..36].try_into().expect("eight bytes"));
    let digest = &data[40..72];
    if sequence != expected_sequence {
        return Err(SpoolError::InvalidChunk(format!(
            "header sequence {sequence} does not match file {expected_sequence}"
        )));
    }
    let body = data[CHUNK_HEADER_LEN..].to_vec();
    validate_chunk_shape(samples, &body)?;
    let actual = sha256_hex(&body);
    if !actual.eq_ignore_ascii_case(&hex_encode(digest)) {
        return Err(SpoolError::HashMismatch {
            expected: hex_encode(digest),
            actual,
        });
    }
    Ok(StoredChunk {
        sequence,
        start,
        samples,
        sha256: hex_encode(digest),
        bytes: body,
    })
}

struct ChunkScan {
    chunks: Vec<StoredChunk>,
    conflicts: Vec<RecoveryConflict>,
}

fn scan_chunks(
    chunk_dir: &ChunkDir,
    capacity_samples: u64,
    durable_offset: u64,
) -> Result<ChunkScan> {
    let mut chunks = Vec::new();
    let mut conflicts = Vec::new();
    chunk_dir.verify()?;
    let entries = match fs::read_dir(chunk_dir.path()) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(ChunkScan { chunks, conflicts });
        }
        Err(error) => return Err(spool_io(chunk_dir.path(), error)),
    };
    let mut seen = 0usize;
    let mut total_bytes = 0u64;
    let mut pending_samples = 0u64;
    for entry in entries {
        let entry = entry.map_err(|error| spool_io(chunk_dir.path(), error))?;
        seen += 1;
        if seen > MAX_SCAN_CHUNKS {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::AggregateOverfull,
                detail: format!("chunks directory has more than {MAX_SCAN_CHUNKS} entries"),
            });
            break;
        }
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::UnexpectedFile,
                detail: "non-utf8 chunk file name".into(),
            });
            continue;
        };
        if name.starts_with(TEMP_PREFIX) {
            if let Err(error) = chunk_dir.remove_file(name) {
                conflicts.push(RecoveryConflict {
                    kind: ConflictKind::UnexpectedFile,
                    detail: format!("could not remove temp file {name}: {error}"),
                });
            }
            continue;
        }
        let path = entry.path();
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            if metadata.file_type().is_symlink() {
                conflicts.push(RecoveryConflict {
                    kind: ConflictKind::UnexpectedFile,
                    detail: format!("{} is a symlink", path.display()),
                });
                continue;
            }
        }
        let Some(stem) = name.strip_suffix(".pcm") else {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::UnexpectedFile,
                detail: format!("unexpected file {name}"),
            });
            continue;
        };
        let Ok(sequence) = stem.parse::<u64>() else {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::UnexpectedFile,
                detail: format!("unexpected chunk file name {name}"),
            });
            continue;
        };
        match read_chunk_file(chunk_dir, name, sequence) {
            Ok(chunk) => {
                let file_bytes = CHUNK_HEADER_LEN as u64 + chunk.bytes.len() as u64;
                if total_bytes.saturating_add(file_bytes) > MAX_SCAN_BYTES {
                    conflicts.push(RecoveryConflict {
                        kind: ConflictKind::AggregateOverfull,
                        detail: format!(
                            "chunk files exceed the {MAX_SCAN_BYTES}-byte reconciliation bound"
                        ),
                    });
                    break;
                }
                total_bytes = total_bytes.saturating_add(file_bytes);
                if chunk.end() > durable_offset {
                    pending_samples = pending_samples.saturating_add(chunk.samples);
                    if pending_samples > capacity_samples {
                        conflicts.push(RecoveryConflict {
                            kind: ConflictKind::AggregateOverfull,
                            detail: format!(
                                "pending chunks exceed the {capacity_samples}-sample spool bound"
                            ),
                        });
                        break;
                    }
                }
                chunks.push(chunk);
            }
            Err(SpoolError::ChunkTooLarge { .. }) => conflicts.push(RecoveryConflict {
                kind: ConflictKind::ChunkOversize,
                detail: format!("{name} is larger than any valid recorder chunk"),
            }),
            Err(SpoolError::HashMismatch { .. }) => conflicts.push(RecoveryConflict {
                kind: ConflictKind::ChunkHashMismatch,
                detail: format!("{name} failed its content hash"),
            }),
            Err(error) => conflicts.push(RecoveryConflict {
                kind: ConflictKind::ChunkCorrupt(name.to_owned()),
                detail: error.to_string(),
            }),
        }
    }
    chunks.sort_by_key(|chunk| (chunk.start, chunk.sequence));
    let mut seen_sequences = BTreeMap::new();
    let mut seen_starts = BTreeMap::new();
    chunks.retain(|chunk| {
        if let Some(previous) = seen_sequences.insert(chunk.sequence, chunk.start) {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::SequenceMismatch,
                detail: format!(
                    "duplicate sequence {} at {previous} and {}",
                    chunk.sequence, chunk.start
                ),
            });
            return false;
        }
        if let Some(previous) = seen_starts.insert(chunk.start, chunk.sequence) {
            conflicts.push(RecoveryConflict {
                kind: ConflictKind::SequenceMismatch,
                detail: format!(
                    "duplicate start {} for sequences {previous} and {}",
                    chunk.start, chunk.sequence
                ),
            });
            return false;
        }
        true
    });
    Ok(ChunkScan { chunks, conflicts })
}

fn remove_acked_chunks(chunk_dir: &ChunkDir, durable_offset: u64) -> Result<()> {
    chunk_dir.verify()?;
    let entries = match fs::read_dir(chunk_dir.path()) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(spool_io(chunk_dir.path(), error)),
    };
    let mut removed = false;
    let mut seen = 0usize;
    for entry in entries {
        let entry = entry.map_err(|error| spool_io(chunk_dir.path(), error))?;
        seen += 1;
        if seen > MAX_SCAN_CHUNKS {
            break;
        }
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let Some(stem) = name.strip_suffix(".pcm") else {
            continue;
        };
        let Ok(sequence) = stem.parse::<u64>() else {
            continue;
        };
        match read_chunk_file(chunk_dir, name, sequence) {
            Ok(chunk) if chunk.end() <= durable_offset => {
                chunk_dir.remove_file(name)?;
                removed = true;
            }
            Ok(_) => {}
            Err(_) => {}
        }
    }
    if removed {
        chunk_dir.sync()?;
    }
    Ok(())
}

fn hex_string(value: &str) -> String {
    hex_encode(value.as_bytes())
}

fn from_hex_string(value: &str) -> Result<String> {
    let bytes = hex_to_bytes(value)?;
    String::from_utf8(bytes).map_err(|_| SpoolError::InvalidMeta("invalid UTF-8 field".into()))
}

fn hex_to_bytes(value: &str) -> Result<Vec<u8>> {
    if value.len() % 2 != 0 {
        return Err(SpoolError::InvalidMeta("odd-length hex value".into()));
    }
    let mut out = Vec::with_capacity(value.len() / 2);
    let bytes = value.as_bytes();
    for pair in bytes.chunks_exact(2) {
        let high = nibble(pair[0])?;
        let low = nibble(pair[1])?;
        out.push((high << 4) | low);
    }
    Ok(out)
}

fn nibble(byte: u8) -> Result<u8> {
    match byte {
        b'0'..=b'9' => Ok(byte - b'0'),
        b'a'..=b'f' => Ok(byte - b'a' + 10),
        b'A'..=b'F' => Ok(byte - b'A' + 10),
        _ => Err(SpoolError::InvalidMeta("bad hex digit".into())),
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

// SHA-256 for chunk integrity. The transport uses the same digest in
// `X-Content-SHA256`, so recovery cannot upload a chunk whose bytes changed.
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
        let mut words = [0u32; 64];
        for (index, word) in block.chunks_exact(4).enumerate() {
            words[index] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for index in 16..64 {
            let s0 = words[index - 15].rotate_right(7)
                ^ words[index - 15].rotate_right(18)
                ^ (words[index - 15] >> 3);
            let s1 = words[index - 2].rotate_right(17)
                ^ words[index - 2].rotate_right(19)
                ^ (words[index - 2] >> 10);
            words[index] = words[index - 16]
                .wrapping_add(s0)
                .wrapping_add(words[index - 7])
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
                .wrapping_add(words[index]);
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
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(1);

    struct TempRoot(PathBuf);

    impl TempRoot {
        fn new(name: &str) -> Self {
            let mut path = std::env::temp_dir();
            path.push(format!(
                "nexus-recorder-spool-test-{}-{}-{name}",
                std::process::id(),
                TEST_COUNTER.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for TempRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn id() -> &'static str {
        "0123456789abcdef0123456789abcdef"
    }

    fn meta() -> SpoolMeta {
        SpoolMeta::new(id(), "tui", 1, "recovery-token-0123456789", None)
    }

    fn chunk(sequence: u64, value: u8) -> (u64, Vec<u8>, String) {
        let start = sequence * SAMPLE_RATE as u64;
        let body = vec![value; SAMPLE_RATE as usize * 2];
        let hash = sha256_hex(&body);
        (start, body, hash)
    }

    fn append(session: &mut SpoolSession, sequence: u64) {
        let (start, body, hash) = chunk(sequence, sequence as u8);
        session
            .append_chunk(sequence, start, SAMPLE_RATE as u64, &body, &hash)
            .unwrap();
    }

    #[cfg(unix)]
    fn mode(path: &Path) -> u32 {
        fs::metadata(path).unwrap().permissions().mode() & 0o777
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
    }

    #[test]
    fn chunks_and_metadata_are_private_and_round_trip() {
        let root = TempRoot::new("permissions");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        let dir = session.dir.clone();
        let chunks = dir.join(CHUNKS_NAME);
        let chunk_path = chunk_path(&chunks, 0);
        let meta_path = dir.join(META_NAME);
        #[cfg(unix)]
        {
            assert_eq!(mode(&root.0.join(SPOOL_DIR_NAME)), 0o700);
            assert_eq!(mode(&dir), 0o700);
            assert_eq!(mode(&chunks), 0o700);
            assert_eq!(mode(&chunk_path), 0o600);
            assert_eq!(mode(&meta_path), 0o600);
        }
        session.acknowledge(SAMPLE_RATE as u64, 1).unwrap();
        assert!(!chunk_path.exists());
        assert_eq!(session.pending_samples(), 0);
        drop(session);

        let reopened = store.open(id()).unwrap();
        assert_eq!(reopened.meta().durable_offset, SAMPLE_RATE as u64);
        assert_eq!(reopened.meta().state, SpoolState::Interrupted);
        assert_eq!(reopened.meta().final_sample_count, Some(SAMPLE_RATE as u64));
    }

    #[test]
    fn spool_is_bounded_to_thirty_seconds() {
        let root = TempRoot::new("bound");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        for sequence in 0..SPOOL_SECONDS {
            append(&mut session, sequence);
        }
        assert_eq!(session.pending_samples(), MAX_SPOOL_SAMPLES);
        let (start, body, hash) = chunk(SPOOL_SECONDS, 7);
        let error = session
            .append_chunk(SPOOL_SECONDS, start, SAMPLE_RATE as u64, &body, &hash)
            .unwrap_err();
        assert!(matches!(
            error,
            SpoolError::SpoolFull {
                capacity: MAX_SPOOL_SAMPLES,
                ..
            }
        ));
        assert_eq!(session.meta().accepted_samples, MAX_SPOOL_SAMPLES);
    }

    #[test]
    fn duplicate_is_idempotent_but_different_content_conflicts() {
        let root = TempRoot::new("duplicate");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        append(&mut session, 0); // same bytes/hash: idempotent

        let (start, mut body, _) = chunk(0, 9);
        body[0] ^= 0xff;
        let changed_hash = sha256_hex(&body);
        let error = session
            .append_chunk(0, start, SAMPLE_RATE as u64, &body, &changed_hash)
            .unwrap_err();
        assert!(matches!(error, SpoolError::Conflict(_)));
        assert_eq!(session.meta().accepted_samples, SAMPLE_RATE as u64);
    }

    #[test]
    fn crash_after_chunk_rename_before_meta_is_repaired_from_chunk_files() {
        let root = TempRoot::new("repair");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        // Simulate the crash window: chunk durable, metadata still at zero.
        let mut stale = session.meta().clone();
        stale.accepted_samples = 0;
        stale.next_sequence = 0;
        write_meta(&session.dir, &stale).unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        assert_eq!(reopened.meta().accepted_samples, SAMPLE_RATE as u64);
        assert_eq!(reopened.meta().next_sequence, 1);
        let plan = reopened.reconcile(None).unwrap();
        assert_eq!(plan.uploads.len(), 1);
        assert_eq!(plan.uploads[0].sequence, 0);
        assert!(plan.gaps.is_empty());
    }

    #[test]
    fn meta_ahead_of_chunks_is_reported_and_not_filled_in() {
        let root = TempRoot::new("meta-ahead");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        let mut ahead = session.meta().clone();
        ahead.accepted_samples = 2 * SAMPLE_RATE as u64;
        ahead.next_sequence = 2;
        write_meta(&session.dir, &ahead).unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert!(plan
            .conflicts
            .iter()
            .any(|conflict| conflict.kind == ConflictKind::MetaAhead));
        assert_eq!(plan.uploads.len(), 1);
        assert_eq!(plan.final_sample_count, SAMPLE_RATE as u64);
    }

    #[test]
    fn recovery_uses_server_baseline_and_skips_already_durable_chunks() {
        let root = TempRoot::new("baseline");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        append(&mut session, 1);
        session.acknowledge(SAMPLE_RATE as u64, 1).unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened
            .reconcile(Some(ServerBaseline {
                durable_offset: 2 * SAMPLE_RATE as u64,
                next_sequence: 2,
                final_sample_count: Some(2 * SAMPLE_RATE as u64),
                sealed: false,
            }))
            .unwrap();
        assert!(plan.uploads.is_empty());
        assert!(plan.gaps.is_empty());
        assert_eq!(plan.final_sample_count, 2 * SAMPLE_RATE as u64);
        assert!(plan.needs_seal);
        assert_eq!(plan.actions().len(), 1); // seal only
    }

    #[test]
    fn server_baseline_inside_a_chunk_is_a_conflict() {
        let root = TempRoot::new("inside");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened
            .reconcile(Some(ServerBaseline {
                durable_offset: SAMPLE_RATE as u64 / 2,
                next_sequence: 0,
                final_sample_count: None,
                sealed: false,
            }))
            .unwrap();
        assert!(plan.uploads.is_empty());
        assert!(plan
            .conflicts
            .iter()
            .any(|conflict| conflict.kind == ConflictKind::BaselineInsideChunk));
    }

    #[test]
    fn an_absent_chunk_reports_a_gap_and_stops_before_later_chunks() {
        let root = TempRoot::new("gap");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        append(&mut session, 1);
        // Write sequence 3 directly, leaving the sequence 2 range absent.
        let (start, body, hash) = chunk(3, 3);
        write_chunk_file(
            &session.chunk_dir,
            &StoredChunk {
                sequence: 3,
                start,
                samples: SAMPLE_RATE as u64,
                sha256: hash,
                bytes: body,
            },
        )
        .unwrap();
        session.meta.final_sample_count = Some(4 * SAMPLE_RATE as u64);
        write_meta(&session.dir, &session.meta).unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert_eq!(plan.uploads.len(), 2);
        assert_eq!(plan.gaps.len(), 1);
        assert_eq!(plan.gaps[0].start, 2 * SAMPLE_RATE as u64);
        // The whole suffix after the gap is unrecovered, including the spooled
        // chunk stranded beyond it.
        assert_eq!(plan.gaps[0].samples, 2 * SAMPLE_RATE as u64);
        assert_eq!(plan.unsaved_samples, 2 * SAMPLE_RATE as u64);
    }

    #[test]
    fn final_beyond_spooled_audio_is_an_explicit_unsaved_tail() {
        let root = TempRoot::new("unsaved");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        session
            .mark_interrupted(Some(2 * SAMPLE_RATE as u64), Some(3 * SAMPLE_RATE as u64))
            .unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert_eq!(plan.uploads.len(), 1);
        assert_eq!(plan.final_sample_count, 2 * SAMPLE_RATE as u64);
        assert_eq!(plan.unsaved_samples, SAMPLE_RATE as u64);
        assert_eq!(plan.gaps[0].cause, GapCause::UnsavedTail);
    }

    #[test]
    fn damaged_chunk_is_conflict_not_an_upload() {
        let root = TempRoot::new("corrupt");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        let path = chunk_path(&session.chunks_dir, 0);
        let mut bytes = fs::read(&path).unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 0xff;
        fs::write(&path, bytes).unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert!(plan.uploads.is_empty());
        assert!(plan.conflicts.iter().any(|conflict| matches!(
            conflict.kind,
            ConflictKind::ChunkHashMismatch | ConflictKind::ChunkCorrupt(_)
        )));
    }

    #[test]
    fn temp_files_are_removed_on_open_and_never_count_as_chunks() {
        let root = TempRoot::new("temp");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        let temp = session.chunks_dir.join(".tmp-999-00000001.pcm");
        fs::write(&temp, b"partial").unwrap();
        drop(session);

        let reopened = store.open(id()).unwrap();
        assert!(!temp.exists());
        assert_eq!(reopened.chunks().unwrap().len(), 1);
    }

    #[test]
    fn recovery_capability_is_redacted_from_debug() {
        let root = TempRoot::new("secret");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        let debug = format!("{:?}", session.meta());
        assert!(!debug.contains(session.meta().recovery_token.expose()));
        assert!(debug.contains("Secret(redacted)"));

        let plan = session.reconcile(None).unwrap();
        let debug = format!("{:?}", plan.actions());
        assert!(!debug.contains(session.meta().recovery_token.expose()));
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_chunks_directory_is_rejected() {
        let root = TempRoot::new("symlink-dir");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        let chunks = session.chunks_dir.clone();
        fs::remove_dir_all(&chunks).unwrap();
        let outside = root.0.join("outside");
        fs::create_dir_all(&outside).unwrap();
        std::os::unix::fs::symlink(&outside, &chunks).unwrap();
        drop(session);

        assert!(matches!(store.open(id()), Err(SpoolError::Conflict(_))));
        let summaries = store.list().unwrap();
        assert!(summaries
            .iter()
            .any(|summary| summary.id == id() && summary.error.is_some()));
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_chunk_file_is_not_read() {
        let root = TempRoot::new("symlink-file");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        let outside = root.0.join("outside.pcm");
        let (body, hash) = {
            let (_, body, hash) = chunk(1, 1);
            (body, hash)
        };
        write_chunk_file(
            &session.chunk_dir,
            &StoredChunk {
                sequence: 1,
                start: SAMPLE_RATE as u64,
                samples: SAMPLE_RATE as u64,
                sha256: hash,
                bytes: body,
            },
        )
        .unwrap();
        let path = chunk_path(&session.chunks_dir, 0);
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(&outside, &path).unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert!(plan
            .conflicts
            .iter()
            .any(|conflict| conflict.kind == ConflictKind::UnexpectedFile));
    }

    #[test]
    fn oversized_chunk_is_rejected_before_body_read() {
        let root = TempRoot::new("oversize");
        let store = SpoolStore::new(&root.0).unwrap();
        let session = store.create(meta()).unwrap();
        let path = chunk_path(&session.chunks_dir, 0);
        fs::write(&path, vec![0u8; CHUNK_HEADER_LEN + MAX_CHUNK_BYTES + 1]).unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert!(plan
            .conflicts
            .iter()
            .any(|conflict| conflict.kind == ConflictKind::ChunkOversize));
        assert!(plan.uploads.is_empty());

        let summaries = store.list().unwrap();
        let summary = summaries.iter().find(|summary| summary.id == id()).unwrap();
        assert!(summary.error.is_some(), "list must not show a clean spool");
    }

    #[cfg(unix)]
    #[test]
    fn fifo_chunk_is_rejected_without_blocking() {
        let root = TempRoot::new("fifo");
        let store = SpoolStore::new(&root.0).unwrap();
        let session = store.create(meta()).unwrap();
        let path = chunk_path(&session.chunks_dir, 0);
        let name = std::ffi::CString::new(path.to_str().unwrap()).unwrap();
        let result = unsafe { libc::mkfifo(name.as_ptr(), 0o600 as libc::mode_t) };
        assert_eq!(result, 0);
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert!(plan
            .conflicts
            .iter()
            .any(|conflict| matches!(conflict.kind, ConflictKind::ChunkCorrupt(_))));
    }

    #[cfg(unix)]
    #[test]
    fn chunks_directory_identity_is_checked_after_open() {
        let root = TempRoot::new("identity");
        let first = root.0.join("first");
        let second = root.0.join("second");
        fs::create_dir_all(&first).unwrap();
        fs::create_dir_all(&second).unwrap();
        let metadata = fs::symlink_metadata(&first).unwrap();
        let identity = Some((metadata.dev(), metadata.ino()));
        let old = root.0.join("first-old");
        fs::rename(&first, &old).unwrap();
        fs::rename(&second, &first).unwrap();

        assert!(matches!(
            ChunkDir::open_checked(&first, identity),
            Err(SpoolError::Conflict(_))
        ));
    }

    #[test]
    fn aggregate_overfull_directory_is_bounded_and_reported() {
        let root = TempRoot::new("overfull");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        for sequence in 0..SPOOL_SECONDS {
            append(&mut session, sequence);
        }
        // Write more valid chunks than the 30 s spool permits.
        for sequence in SPOOL_SECONDS..SPOOL_SECONDS + 2 {
            let (start, body, hash) = chunk(sequence, 7);
            write_chunk_file(
                &session.chunk_dir,
                &StoredChunk {
                    sequence,
                    start,
                    samples: SAMPLE_RATE as u64,
                    sha256: hash,
                    bytes: body,
                },
            )
            .unwrap();
        }
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened.reconcile(None).unwrap();
        assert!(plan
            .conflicts
            .iter()
            .any(|conflict| conflict.kind == ConflictKind::AggregateOverfull));
        assert!(plan.uploads.len() <= SPOOL_SECONDS as usize);
    }

    #[test]
    fn core_sealed_final_count_is_authoritative_over_local() {
        let root = TempRoot::new("final-mismatch");
        let store = SpoolStore::new(&root.0).unwrap();
        let mut session = store.create(meta()).unwrap();
        append(&mut session, 0);
        session
            .mark_interrupted(Some(SAMPLE_RATE as u64), Some(SAMPLE_RATE as u64))
            .unwrap();
        drop(session);

        let mut reopened = store.open(id()).unwrap();
        let plan = reopened
            .reconcile(Some(ServerBaseline {
                durable_offset: 0,
                next_sequence: 0,
                final_sample_count: Some(2 * SAMPLE_RATE as u64),
                sealed: true,
            }))
            .unwrap();
        assert!(plan
            .conflicts
            .iter()
            .any(|conflict| conflict.kind == ConflictKind::FinalCountMismatch));
        assert_eq!(plan.final_sample_count, 2 * SAMPLE_RATE as u64);
        assert_eq!(plan.uploads.len(), 1);
        assert_eq!(plan.unsaved_samples, SAMPLE_RATE as u64);
    }

    #[test]
    fn pending_start_is_private_locked_and_removed_on_finish() {
        let root = TempRoot::new("pending");
        let store = SpoolStore::new(&root.0).unwrap();
        let key = "0123456789abcdef0123456789abcdef";
        let guard = store
            .begin_pending_start(
                key,
                Secret::new("client-recovery-capability-0123456789abcdef"),
                "tui",
                Some("a title"),
            )
            .unwrap();
        assert!(!format!("{:?}", guard.record()).contains(guard.record().recovery.expose()));
        #[cfg(unix)]
        {
            let starts = root.0.join(SPOOL_DIR_NAME).join("starts");
            assert_eq!(mode(&starts), 0o700);
            assert_eq!(mode(&starts.join(format!("{key}.start"))), 0o600);
            assert_eq!(mode(&starts.join(format!("{key}.lock"))), 0o600);
        }
        assert!(matches!(
            store.begin_pending_start(
                key,
                Secret::new("client-recovery-capability-0123456789abcdef"),
                "tui",
                None
            ),
            Err(SpoolError::Locked)
        ));
        assert_eq!(store.pending_start_ids().unwrap(), vec![key.to_owned()]);
        assert!(store.open_pending_start(key).unwrap().is_none());
        // `starts` is a sibling directory, not a 32-hex recording id.
        assert!(store.list().unwrap().is_empty());

        drop(guard);
        let reopened = store.open_pending_start(key).unwrap().unwrap();
        assert_eq!(reopened.record().client_id, "tui");
        assert_eq!(reopened.record().title.as_deref(), Some("a title"));
        reopened.finish().unwrap();
        assert!(store.pending_start_ids().unwrap().is_empty());
        assert!(store.open_pending_start(key).unwrap().is_none());
    }

    #[test]
    fn dropped_pending_start_survives_for_crash_recovery() {
        let root = TempRoot::new("pending-crash");
        let store = SpoolStore::new(&root.0).unwrap();
        let key = "fedcba9876543210fedcba9876543210";
        let guard = store
            .begin_pending_start(
                key,
                Secret::new("client-recovery-capability-0123456789abcdef"),
                "tui",
                None,
            )
            .unwrap();
        drop(guard);
        assert_eq!(store.pending_start_ids().unwrap(), vec![key.to_owned()]);
        let reopened = store.open_pending_start(key).unwrap().unwrap();
        assert_eq!(reopened.record().key, key);
        reopened.finish().unwrap();
    }

    #[test]
    fn invalid_ids_and_missing_meta_are_rejected() {
        let root = TempRoot::new("invalid");
        let store = SpoolStore::new(&root.0).unwrap();
        assert!(matches!(
            store.open("../escape"),
            Err(SpoolError::InvalidId)
        ));
        let bad = SpoolMeta::new("../escape", "tui", 1, "token", None);
        assert!(matches!(store.create(bad), Err(SpoolError::InvalidId)));

        let orphan = store.root().join(id());
        fs::create_dir_all(orphan.join(CHUNKS_NAME)).unwrap();
        assert!(matches!(
            store.open(id()),
            Err(SpoolError::InvalidMeta(message)) if message.contains("meta")
        ));
    }
}
