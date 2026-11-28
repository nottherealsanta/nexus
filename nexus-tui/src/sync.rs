//! The connection supervisor: discovery, handshake, snapshot, SSE with durable
//! cursors, reconnect with backoff, resync and daemon-restart detection.
//!
//! It runs as one tokio task and talks to the app through two channels:
//! `SyncMsg` out (state, snapshot, events, in order) and `Command` in. It owns
//! no UI state; `AppState::apply` folds the messages into the model.
//!
//! Lifecycle of one pass of the loop (mirrors `nexus-core/nexus/client.py`
//! `watch`, plus the INTERFACE_PLAN restart rules):
//!
//! 1. read `daemon.json` (start Core through the documented command if absent
//!    or stale and bootstrapping is on);
//! 2. `GET /api/info`: 401 -> auth failure (wait for the token to change); a
//!    major mismatch halts with no further requests; otherwise go;
//! 3. a changed pid/token (a restarted Core), a first connect, a cursor failure,
//!    `bus.overflow` or a requested resync -> `GET /api/snapshot`, then resume
//!    strictly after its watermark;
//! 4. open `GET /api/event` with `Last-Event-ID`. Events are forwarded in order;
//!    the cursor advances only after an event was handed to the app. A drop
//!    goes back to step 1 after a bounded backoff, so every reconnect re-reads
//!    the token and re-handshakes.
//!
//! The stream is unfiltered, so ids are contiguous; dedup and "gaps are normal"
//! live in the reducer, which would be the same for a filtered stream.

use std::path::PathBuf;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::api::{
    check_compat, format_cursor, ClientView, Compat, CoreApi, Event, Frame, History, Info,
    Snapshot, TurnPage, CLIENT_API,
};
use crate::discovery::{self, DaemonInfo, Identity};
use crate::state::ConnectionState;
use crate::transport::{Backoff, Error, Http};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResyncCause {
    Initial,
    DaemonRestarted,
    /// The server rejected our cursor (`cursor_generation`, `history_unavailable`, ...).
    Cursor(String),
    Overflow,
    Requested,
}

/// Supervisor -> app. Order is meaningful: a `Snapshot` precedes the events
/// that follow its watermark.
#[derive(Debug, Clone, PartialEq)]
pub enum SyncMsg {
    Connection(ConnectionState),
    Config(Result<Value, String>),
    Context {
        session: String,
        data: Value,
    },
    /// Core's starting context for the request body that was asked.
    DraftContext {
        request: Value,
        data: Result<Value, String>,
    },
    Inputs {
        session: String,
        data: Value,
    },
    Handshake {
        info: Info,
        compat: Compat,
    },
    Snapshot {
        snapshot: Box<Snapshot>,
        cause: ResyncCause,
    },
    /// The selected session's full stored history, sent after the snapshot and
    /// before live events. The app ignores it when the selection changed.
    History(Box<History>),
    /// Turn summaries fetched after a reconnect (`/turn?after=`), with the
    /// fresh ledger. The app ignores it when the selection changed.
    Turns {
        session: String,
        page: Box<TurnPage>,
    },
    /// Picker data: connected providers' models and the available agents, plus
    /// the provider table and agent-file problems for the Settings pages.
    Catalog {
        models: Vec<Value>,
        agents: Vec<Value>,
        source: String,
        /// The model last selected anywhere (`provider/id`), empty when none was chosen.
        last_model: String,
        providers: Vec<Value>,
        problems: Vec<Value>,
    },
    Files {
        project: String,
        session: Option<String>,
        path: String,
        data: Result<Value, String>,
        file: bool,
    },
    /// Reply to `Command::Complete`: `{"query", "paths"}` from files/search.
    Completion {
        project: String,
        session: Option<String>,
        query: String,
        limit: u32,
        data: Result<Value, String>,
    },
    /// A project's own worktrees, used before a session exists.
    ProjectWorktrees {
        project: String,
        data: Result<Value, String>,
    },
    /// Live kernels and background bash jobs, compact (no cell bodies or output).
    Resources {
        kernels: Result<Vec<Value>, String>,
        jobs: Result<Vec<Value>, String>,
    },
    /// Project skills, global MCP servers and this project's MCP files' inventory
    /// for a new-session preview. None of the reads starts a server.
    SkillMcp {
        project: String,
        skills: Result<Value, String>,
        mcp: Result<Value, String>,
        project_mcp: Result<Value, String>,
    },
    Event(Event),
    /// The answer to a `Command::Manager`: `tag` is the caller's own label.
    Manager {
        tag: String,
        result: Result<Value, ManagerError>,
    },
    /// A recorder task reply (status, list, one recording, transcript, …).
    Recorder(RecorderUpdate),
}

/// The current stream's id, for the one route that streams audio back
/// (`/api/voice/speak` with text must name the holder's stream). Never logged.
static STREAM: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

pub(crate) fn current_stream() -> Option<String> {
    STREAM.lock().ok().and_then(|s| s.clone())
}

fn remember_stream(id: Option<&str>) {
    if let Ok(mut s) = STREAM.lock() {
        *s = id.map(str::to_owned);
    }
}

/// Why a Manager route failed: the status and `code` Core gave, or status 0
/// when the call never reached it.
#[derive(Debug, Clone, PartialEq)]
pub struct ManagerError {
    pub status: u16,
    pub code: Option<String>,
    pub message: String,
    /// The error body as Core sent it (`consent_required` carries the notice).
    pub body: Value,
}

/// One command for the recorder task. Tokens stay inside the driver; the app
/// only ever names a recording, a title and an idempotency key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecorderAction {
    Start {
        title: Option<String>,
        idempotency_key: String,
    },
    Stop,
    /// A new event stream is live: recover create intents from an earlier run
    /// before any Start can reach Core.
    Connected,
    List,
    Detail {
        id: String,
    },
    /// Read every durable transcript page for one recording.
    Transcript {
        id: String,
    },
    Retry {
        id: String,
    },
    Delete {
        id: String,
    },
    Recover,
    /// Copy the durable export; `complete` is Core's completion marker.
    CopyTranscript {
        id: String,
    },
}

/// Recorder task -> app. Public recording state is still applied from the
/// durable `recorder.*` events; these are the reads and this client's capture.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoverableAudio {
    pub id: String,
    pub accepted_samples: u64,
    pub pending_samples: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum RecorderUpdate {
    Status {
        owner: crate::ui::RecorderOwner,
        recoverable: Vec<String>,
        warnings: Vec<String>,
    },
    List(Result<Vec<Value>, String>),
    Detail(Result<Value, String>),
    Transcript {
        id: String,
        page: Result<Value, String>,
    },
    Retry(Result<(), String>),
    Copied {
        text: String,
        complete: bool,
    },
    Deleted {
        id: String,
        result: Result<(), String>,
    },
    Recovered(Result<crate::recorder_driver::RecoveryReport, String>),
    /// Startup create-intent recovery, with the number of intents still on
    /// disk. `ready` is false while any intent remains or Core needs a restart.
    Startup {
        result: Result<crate::recorder_driver::RecoveryReport, String>,
        pending: usize,
        ready: bool,
    },
    /// This client's bounded spool holds audio that can still be uploaded.
    Recoverable(Vec<RecoverableAudio>),
    /// A read or copy failed; capture is unaffected.
    Error(String),
    Failed {
        kind: String,
        message: String,
    },
    /// A new event stream opened: re-read snapshots and rebind the capture.
    Reconnected,
}

/// A Manager route that needs this stream's `stream_id`. The supervisor owns
/// that bearer capability, so it adds it to `body` itself and the app never sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagerCall {
    pub tag: String,
    pub method: &'static str,
    pub path: String,
    pub body: Option<Value>,
    pub with_stream: bool,
}

/// App -> supervisor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// View this session: load its history (in the background while the
    /// stream is live) and include it in later snapshots. No resync.
    Select(Option<String>),
    /// Sessions besides the selected one that visible tabs show: their
    /// history is loaded now and again after every resync (plan 45).
    Watch(Vec<String>),
    /// What the user is looking at: project, session and panel ids only. Kept
    /// across reconnects and reported after the next `server.connected`.
    View(ClientView),
    /// Fetch a fresh snapshot (the reducer could not continue).
    Resync,
    Config,
    Context(String),
    /// The starting context a new session would have: `project` plus the draft's fields.
    DraftContext(Value),
    PatchConfig {
        revision: String,
        path: String,
        value: Value,
    },
    /// Load the model catalog and agent list for a project (picker data).
    Catalog(Option<String>),
    /// Fetching also publishes a worktree.listed event on Core.
    Worktrees(String),
    /// The project's own checkout, for a session that does not exist yet.
    ProjectWorktrees(String),
    /// Refresh live kernels and background bash jobs.
    Resources,
    SkillMcp(String),
    Files {
        project: String,
        session: Option<String>,
        path: String,
        file: bool,
    },
    /// Path candidates for an `@` token (the composer's completion popup).
    Complete {
        project: String,
        session: Option<String>,
        query: String,
        limit: u32,
    },
    /// A Manager route call (start, say, activity, navigate result, ...).
    Manager(ManagerCall),
    /// Recorder captures, reads and recovery run in their own task.
    Recorder(RecorderAction),
    Shutdown,
}

#[derive(Debug, Clone)]
pub struct SyncConfig {
    pub home: PathBuf,
    /// Start Core when none answers (the local bootstrap).
    pub bootstrap: bool,
    pub backoff: Backoff,
    /// Poll interval while parked on auth failure or an incompatible Core, and
    /// while Core is offline and cannot be started.
    pub park_interval: Duration,
    pub message_limit: u32,
}

impl SyncConfig {
    pub fn new(home: PathBuf) -> Self {
        Self {
            home,
            bootstrap: true,
            backoff: Backoff::standard(),
            park_interval: Duration::from_secs(2),
            message_limit: 200,
        }
    }
}

pub struct SyncHandle {
    pub commands: mpsc::UnboundedSender<Command>,
    pub messages: mpsc::UnboundedReceiver<SyncMsg>,
}

pub fn spawn(cfg: SyncConfig) -> SyncHandle {
    let (out_tx, out_rx) = mpsc::unbounded_channel();
    let (cmd_tx, cmd_rx) = mpsc::unbounded_channel();
    tokio::spawn(run(cfg, out_tx, cmd_rx));
    SyncHandle {
        commands: cmd_tx,
        messages: out_rx,
    }
}

struct Ctx {
    cfg: SyncConfig,
    out: mpsc::UnboundedSender<SyncMsg>,
    cmds: mpsc::UnboundedReceiver<Command>,
    /// Self-asserted label for this process's view reports; stable across
    /// reconnects so a new stream can report from the same client.
    client_id: String,
    /// The current stream's capability from `server.connected`, never logged.
    stream_id: Option<String>,
    /// The latest view from the app, kept while disconnected.
    view: Option<ClientView>,
    /// The view the current stream already accepted, so an unchanged view is
    /// not re-sent.
    view_reported: Option<ClientView>,
    /// Sequence of view reports; a late result from an older request is ignored.
    view_seq: u64,
    /// The one report being sent beside the stream, if any. Serializing reports
    /// means an older view can never land after a newer one on the same stream.
    view_inflight: Option<ViewRequest>,
    /// A transient failure's next attempt.
    view_retry_at: Option<tokio::time::Instant>,
    /// Bounded delays between failed view reports.
    view_backoff: Backoff,
    selected: Option<String>,
    /// Set by commands; consumed by the next pass.
    resync: Option<ResyncCause>,
    /// A model/agent picker asked for the catalog of this project.
    catalog: Option<Option<String>>,
    config: bool,
    context: Option<String>,
    /// Context reads run beside the stream, one at a time; requests made
    /// meanwhile wait here, one per session.
    context_reads: std::sync::Arc<std::sync::Mutex<ContextReads>>,
    draft: Option<Value>,
    inputs: Option<String>,
    /// A selected session whose history has not been loaded yet.
    history: Option<String>,
    /// Watched sessions and those whose history is still to load.
    watch: Vec<String>,
    watch_load: Vec<String>,
    config_patch: Option<(String, String, Value)>,
    worktrees: Option<String>,
    project_worktrees: Vec<String>,
    resources: bool,
    skill_mcp: Option<String>,
    files: Option<(String, Option<String>, String, bool)>,
    complete: Option<(String, Option<String>, String, u32)>,
    /// The selected session and its newest closed turn, the anchor of
    /// `/turn?after=` after a reconnect.
    newest_turn: Option<(String, String)>,
    /// The recorder task's command queue; `None` once it is gone.
    recorder: Option<mpsc::UnboundedSender<RecorderAction>>,
    shutdown: bool,
}

impl Ctx {
    fn send(&self, msg: SyncMsg) -> bool {
        self.out.send(msg).is_ok()
    }
    fn state(&self, s: ConnectionState) -> bool {
        self.send(SyncMsg::Connection(s))
    }
    fn handle(&mut self, cmd: Command) {
        match cmd {
            Command::Select(s) => {
                self.context = s.clone();
                self.inputs = s.clone();
                self.history = s.clone();
                self.selected = s;
                self.newest_turn = None;
            }
            Command::Watch(ids) => {
                for id in &ids {
                    if !self.watch.contains(id) && !self.watch_load.contains(id) {
                        self.watch_load.push(id.clone());
                    }
                }
                self.watch_load.retain(|id| ids.contains(id));
                self.watch = ids;
            }
            Command::View(view) => {
                self.view = Some(view);
                // A real change is sent at once instead of waiting out a
                // previous transient failure's backoff.
                self.view_retry_at = None;
            }
            Command::Resync => self.resync = Some(ResyncCause::Requested),
            Command::Config => self.config = true,
            Command::Context(session) => self.context = Some(session),
            Command::DraftContext(request) => self.draft = Some(request),
            Command::PatchConfig {
                revision,
                path,
                value,
            } => self.config_patch = Some((revision, path, value)),
            Command::Catalog(project) => self.catalog = Some(project),
            Command::Worktrees(session) => self.worktrees = Some(session),
            Command::ProjectWorktrees(project) => {
                if !self.project_worktrees.contains(&project) {
                    self.project_worktrees.push(project);
                }
            }
            Command::Resources => self.resources = true,
            Command::SkillMcp(project) => self.skill_mcp = Some(project),
            Command::Files {
                project,
                session,
                path,
                file,
            } => self.files = Some((project, session, path, file)),
            Command::Complete {
                project,
                session,
                query,
                limit,
            } => self.complete = Some((project, session, query, limit)),
            // Reached only while no stream is open (the stream loop intercepts it).
            Command::Manager(call) => {
                self.send(SyncMsg::Manager {
                    tag: call.tag,
                    result: Err(ManagerError {
                        status: 0,
                        code: None,
                        message: "Core is not connected".into(),
                        body: Value::Null,
                    }),
                });
            }
            Command::Recorder(action) => {
                if let Some(recorder) = &self.recorder {
                    let _ = recorder.send(action);
                }
            }
            Command::Shutdown => self.shutdown = true,
        }
    }

    /// Run a Manager call beside the stream, with this stream's `stream_id`.
    fn run_manager(&self, api: &CoreApi, call: ManagerCall) {
        let out = self.out.clone();
        let mut body = call.body.clone().unwrap_or_else(|| serde_json::json!({}));
        if call.with_stream {
            let Some(stream_id) = self.stream_id.clone() else {
                let _ = out.send(SyncMsg::Manager {
                    tag: call.tag,
                    result: Err(ManagerError {
                        status: 0,
                        code: None,
                        message: "Core is not connected".into(),
                        body: Value::Null,
                    }),
                });
                return;
            };
            body["stream_id"] = Value::String(stream_id);
        }
        let api = api.clone();
        tokio::spawn(async move {
            let method = match call.method {
                "GET" => reqwest::Method::GET,
                _ => reqwest::Method::POST,
            };
            let body_ref = (call.method != "GET").then_some(&body);
            let result = api
                .http
                .call(method, &call.path, &[], body_ref, None, None)
                .await
                .map(|r| r.body)
                .map_err(|e| match e.api() {
                    Some(a) => ManagerError {
                        status: a.status,
                        code: a.code.clone(),
                        message: a.message.clone(),
                        body: a.body.clone(),
                    },
                    None => ManagerError {
                        status: 0,
                        code: None,
                        message: e.to_string(),
                        body: Value::Null,
                    },
                });
            let _ = out.send(SyncMsg::Manager {
                tag: call.tag,
                result,
            });
        });
    }

    /// Sleep, but wake early for a command. Returns false on shutdown.
    async fn pause(&mut self, d: Duration) -> bool {
        let sleep = tokio::time::sleep(d);
        tokio::pin!(sleep);
        loop {
            tokio::select! {
                () = &mut sleep => break,
                cmd = self.cmds.recv() => match cmd {
                    Some(c) => {
                        let wake = !matches!(c, Command::Select(_) | Command::View(_) | Command::Manager(_));
                        self.handle(c);
                        if self.shutdown { return false; }
                        if wake { break; }
                    }
                    None => { self.shutdown = true; return false; }
                },
            }
        }
        !self.shutdown
    }

    /// Hand the latest unreported view to one background POST.
    ///
    /// At most one report is in flight, so replies for the same stream cannot
    /// arrive out of order and make an older view win. A report never runs in
    /// the event loop: the stream keeps consuming events while it is sent.
    fn start_view_report(&mut self, api: &CoreApi, tx: &mpsc::UnboundedSender<ViewResult>) {
        if self.view_inflight.is_some() {
            return;
        }
        let (Some(stream_id), Some(view)) = (self.stream_id.clone(), self.view.clone()) else {
            return;
        };
        if self.view_reported.as_ref() == Some(&view) {
            return;
        }
        if self
            .view_retry_at
            .is_some_and(|at| tokio::time::Instant::now() < at)
        {
            return;
        }
        self.view_retry_at = None;
        self.view_seq = self.view_seq.wrapping_add(1);
        let request = ViewRequest {
            seq: self.view_seq,
            stream_id,
            view,
        };
        self.view_inflight = Some(request.clone());
        let (api, client_id, tx) = (api.clone(), self.client_id.clone(), tx.clone());
        tokio::spawn(async move { send_view_report(api, client_id, request, tx).await });
    }

    /// Fold a background result into the view state. A result for an older
    /// request or for a stream this client no longer reports on is ignored.
    fn finish_view_report(&mut self, result: ViewResult) -> ViewAction {
        if self.view_inflight.as_ref().map(|r| r.seq) != Some(result.seq) {
            return ViewAction::None;
        }
        self.view_inflight = None;
        if self.stream_id.as_deref() != Some(result.stream_id.as_str()) {
            return ViewAction::None;
        }
        match result.outcome {
            ReportOutcome::Sent => {
                self.view_reported = Some(result.view);
                self.view_retry_at = None;
                self.view_backoff.reset();
                ViewAction::None
            }
            ReportOutcome::Stale => {
                self.stream_id = None;
                self.view_reported = None;
                self.view_retry_at = None;
                ViewAction::None
            }
            ReportOutcome::Mismatch => {
                self.stream_id = None;
                self.view_reported = None;
                self.view_retry_at = None;
                ViewAction::Mismatch
            }
            ReportOutcome::Failed => {
                if self.view.as_ref() != Some(&result.view) {
                    // A newer view is waiting: that is a new report, not a
                    // retry of the one that failed, so send it at once.
                    self.view_retry_at = None;
                    ViewAction::None
                } else {
                    let delay = self.view_backoff.next_delay();
                    self.view_retry_at = Some(tokio::time::Instant::now() + delay);
                    ViewAction::Retry(delay)
                }
            }
        }
    }
}

/// One view report handed to a background task. Deliberately not `Debug`: the
/// stream id it carries is a bearer capability.
#[derive(Clone)]
struct ViewRequest {
    seq: u64,
    stream_id: String,
    view: ClientView,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ReportOutcome {
    Sent,
    Stale,
    Mismatch,
    Failed,
}

struct ViewResult {
    seq: u64,
    stream_id: String,
    view: ClientView,
    outcome: ReportOutcome,
}

/// What folding one result asks the stream loop to do.
enum ViewAction {
    None,
    /// Retry after this delay.
    Retry(Duration),
    /// The stream belongs to another client: reopen it.
    Mismatch,
}

/// A view report must never hold up the event stream.
const VIEW_REPORT_TIMEOUT: Duration = Duration::from_secs(3);

async fn send_view_report(
    api: CoreApi,
    client_id: String,
    request: ViewRequest,
    tx: mpsc::UnboundedSender<ViewResult>,
) {
    let result = tokio::time::timeout(
        VIEW_REPORT_TIMEOUT,
        api.report_client_view(&request.stream_id, &client_id, &request.view),
    )
    .await;
    let outcome = match result {
        Ok(Ok(_)) => ReportOutcome::Sent,
        Ok(Err(Error::Api(e))) if e.status == 409 && e.code.as_deref() == Some("stream_closed") => {
            ReportOutcome::Stale
        }
        Ok(Err(Error::Api(e)))
            if e.status == 409 && e.code.as_deref() == Some("client_mismatch") =>
        {
            ReportOutcome::Mismatch
        }
        Ok(Err(_)) | Err(_) => ReportOutcome::Failed,
    };
    let _ = tx.send(ViewResult {
        seq: request.seq,
        stream_id: request.stream_id,
        view: request.view,
        outcome,
    });
}

enum Handshake {
    Ready {
        api: CoreApi,
        daemon: DaemonInfo,
        info: Box<Info>,
    },
    /// Retry the loop after the usual backoff.
    Retry,
    Stop,
}

fn recorder_owner(
    status: &crate::recorder_driver::RecorderDriverStatus,
    recoverable: &[String],
) -> crate::ui::RecorderOwner {
    use crate::recorder::CaptureState;
    use crate::recorder_view::{CapturePhase, SpoolKind};
    let phase = match status.capture.state {
        CaptureState::Starting => CapturePhase::Starting,
        CaptureState::Recording => CapturePhase::Recording,
        CaptureState::Stopping => CapturePhase::Stopping,
        CaptureState::Stopped => CapturePhase::Stopped,
        CaptureState::Interrupted => CapturePhase::Interrupted,
        CaptureState::Failed => CapturePhase::Failed,
        CaptureState::Idle => CapturePhase::Idle,
    };
    let spool = if status.spool_state.is_some() {
        SpoolKind::Persistent
    } else {
        SpoolKind::None
    };
    crate::ui::RecorderOwner {
        phase,
        recording_id: status.capture.recording_id.clone(),
        captured_samples: status.capture.received_samples,
        pending_samples: status.capture.pending_samples,
        spool,
        recoverable: status
            .capture
            .recording_id
            .as_ref()
            .is_some_and(|id| recoverable.contains(id)),
        reason: status.capture.last_error.clone(),
        level: f64::from(status.capture.level.clamp(0.0, 1.0)),
        capture_open: status.capture.capture_open,
    }
}

fn recoverable_spools(home: &std::path::Path) -> Vec<String> {
    let Ok(store) = crate::recorder_spool::SpoolStore::new(home) else {
        return Vec::new();
    };
    let Ok(rows) = store.list() else {
        return Vec::new();
    };
    rows.into_iter()
        .filter(|row| {
            matches!(
                row.state,
                Some(crate::recorder_spool::SpoolState::Interrupted)
            )
        })
        .map(|row| row.id)
        .collect()
}

fn recoverable_audio(home: &std::path::Path) -> Vec<RecoverableAudio> {
    let Ok(store) = crate::recorder_spool::SpoolStore::new(home) else {
        return Vec::new();
    };
    let Ok(rows) = store.list() else {
        return Vec::new();
    };
    rows.into_iter()
        .filter(|row| {
            matches!(
                row.state,
                Some(crate::recorder_spool::SpoolState::Interrupted)
            )
        })
        .map(|row| RecoverableAudio {
            id: row.id,
            accepted_samples: row.accepted_samples,
            pending_samples: row.pending_samples,
        })
        .collect()
}

fn pending_start_count(home: &std::path::Path) -> usize {
    crate::recorder_spool::SpoolStore::new(home)
        .ok()
        .and_then(|store| store.pending_start_ids().ok())
        .map_or(0, |keys| keys.len())
}

/// Look up every create intent left by an earlier run. Never creates and never
/// reuses a key; a recording it finds is sealed at its durable offset (an empty
/// recording included) rather than being deleted.
async fn startup_recovery(
    driver: &mut Option<TuiRecorderDriver>,
    home: &std::path::Path,
) -> (
    Result<crate::recorder_driver::RecoveryReport, String>,
    usize,
) {
    let result = match driver.as_mut() {
        Some(driver) => driver
            .recover_pending_starts()
            .await
            .map_err(|error| error.to_string()),
        None => Err("Core is not reachable".to_owned()),
    };
    (result, pending_start_count(home))
}

/// Why Start must stay closed until a later startup: Core needs a restart, or
/// an intent could not be resolved and is still on disk.
fn startup_block(
    result: &Result<crate::recorder_driver::RecoveryReport, String>,
    pending: usize,
) -> Option<String> {
    match result {
        Err(error) => Some(error.clone()),
        Ok(report) => {
            if let Some(issue) = report.issues.iter().find(|issue| {
                issue.kind == crate::recorder_driver::RecoveryIssueKind::CoreRestartRequired
            }) {
                return Some(issue.detail.clone());
            }
            if pending > 0 {
                return Some(
                    report
                        .issues
                        .first()
                        .map(|issue| issue.detail.clone())
                        .unwrap_or_else(|| {
                            "A previous recording start is still being recovered.".into()
                        }),
                );
            }
            None
        }
    }
}

type TuiRecorderDriver =
    crate::recorder_driver::RecorderDriver<crate::recorder_driver::CoreRecorderApi>;

async fn recorder_task(
    home: std::path::PathBuf,
    out: mpsc::UnboundedSender<SyncMsg>,
    mut commands: mpsc::UnboundedReceiver<RecorderAction>,
    client_id: String,
) {
    use std::time::Instant;
    let started = Instant::now();
    let mut driver: Option<TuiRecorderDriver> = None;
    let mut last_sent = Instant::now();
    let mut last_status: Option<crate::recorder_driver::RecorderDriverStatus> = None;
    let mut last_rebind = Instant::now();
    let mut startup_checked = false;
    let mut startup_block_reason: Option<String> = None;
    let mut tick = tokio::time::interval(Duration::from_millis(200));
    let send = |update: RecorderUpdate| {
        let _ = out.send(SyncMsg::Recorder(update));
    };

    let ensure_driver = |driver: &mut Option<TuiRecorderDriver>| -> Result<(), ()> {
        if driver.is_some() {
            return Ok(());
        }
        let Some(api) = crate::actions::api_for(&home) else {
            send(RecorderUpdate::Failed {
                kind: "connection".into(),
                message: "Core is not reachable".into(),
            });
            return Err(());
        };
        let store = match crate::recorder_spool::SpoolStore::new(&home) {
            Ok(store) => store,
            Err(error) => {
                send(RecorderUpdate::Failed {
                    kind: "spool".into(),
                    message: error.to_string(),
                });
                return Err(());
            }
        };
        let controller = crate::recorder::RecorderController::microphone(
            crate::recorder::RecorderConfig::default(),
        );
        *driver = Some(crate::recorder_driver::RecorderDriver::new(
            controller,
            crate::recorder_driver::CoreRecorderApi::new(api),
            store,
        ));
        Ok(())
    };

    let mut publish = |driver: &mut Option<TuiRecorderDriver>| {
        let Some(d) = driver.as_mut() else {
            return;
        };
        let mut status = d.status();
        let warnings = d.take_warnings();
        status.warnings.clear();
        let recoverable = recoverable_spools(&home);
        if last_status.as_ref() != Some(&status) || last_sent.elapsed() >= Duration::from_secs(1) {
            send(RecorderUpdate::Status {
                owner: recorder_owner(&status, &recoverable),
                recoverable,
                warnings,
            });
            last_status = Some(status);
            last_sent = Instant::now();
        }
    };

    loop {
        tokio::select! {
            command = commands.recv() => {
                let Some(command) = command else { return; };
                match command {
                    RecorderAction::Connected => {
                        if ensure_driver(&mut driver).is_err() {
                            startup_checked = true;
                            startup_block_reason = Some("Core is not reachable".into());
                            send(RecorderUpdate::Startup {
                                result: Err("Core is not reachable".into()),
                                pending: 0,
                                ready: false,
                            });
                            continue;
                        }
                        let (result, pending) = startup_recovery(&mut driver, &home).await;
                        startup_block_reason = startup_block(&result, pending);
                        startup_checked = true;
                        let ready = startup_block_reason.is_none();
                        send(RecorderUpdate::Startup { result, pending, ready });
                        send(RecorderUpdate::Recoverable(recoverable_audio(&home)));
                        publish(&mut driver);
                    }
                    RecorderAction::Start { title, idempotency_key } => {
                        if !startup_checked {
                            if ensure_driver(&mut driver).is_err() {
                                continue;
                            }
                            let (result, pending) = startup_recovery(&mut driver, &home).await;
                            startup_block_reason = startup_block(&result, pending);
                            startup_checked = true;
                            let ready = startup_block_reason.is_none();
                            send(RecorderUpdate::Startup { result, pending, ready });
                            send(RecorderUpdate::Recoverable(recoverable_audio(&home)));
                            publish(&mut driver);
                        }
                        if let Some(reason) = &startup_block_reason {
                            send(RecorderUpdate::Failed {
                                kind: "startup".into(),
                                message: reason.clone(),
                            });
                            continue;
                        }
                        if ensure_driver(&mut driver).is_err() {
                            continue;
                        }
                        let Some(stream_id) = current_stream() else {
                            send(RecorderUpdate::Failed {
                                kind: "connection".into(),
                                message: "Core is not connected".into(),
                            });
                            continue;
                        };
                        let Some(d) = driver.as_mut() else { continue; };
                        let mut request = crate::recorder::StartRequest::new(
                            stream_id,
                            client_id.clone(),
                            idempotency_key,
                        );
                        request.title = title;
                        if let Err(error) = d.start(started.elapsed(), request) {
                            send(RecorderUpdate::Failed {
                                kind: "start".into(),
                                message: format!("{error:?}"),
                            });
                        }
                        let _ = d.step(started.elapsed()).await;
                        // A create whose response was lost keeps its intent on
                        // disk. Resolve it now: seal/find the recording (empty
                        // ones included) before another Start is allowed.
                        if pending_start_count(&home) > 0 {
                            let (result, pending) = startup_recovery(&mut driver, &home).await;
                            startup_block_reason = startup_block(&result, pending);
                            startup_checked = true;
                            let ready = startup_block_reason.is_none();
                            send(RecorderUpdate::Startup { result, pending, ready });
                            send(RecorderUpdate::Recoverable(recoverable_audio(&home)));
                        }
                        publish(&mut driver);
                    }
                    RecorderAction::Stop => {
                        if let Some(d) = driver.as_mut() {
                            if let Err(error) = d.stop(started.elapsed()) {
                                send(RecorderUpdate::Failed {
                                    kind: "stop".into(),
                                    message: format!("{error:?}"),
                                });
                            }
                            let _ = d.step(started.elapsed()).await;
                        }
                        publish(&mut driver);
                    }
                    RecorderAction::List => {
                        let update = match crate::actions::api_for(&home) {
                            Some(api) => match api.http.get("/api/recordings", &[("limit".into(), "50".into())]).await {
                                Ok(value) => RecorderUpdate::List(Ok(value["recordings"].as_array().cloned().unwrap_or_default())),
                                Err(error) => RecorderUpdate::List(Err(error.to_string())),
                            },
                            None => RecorderUpdate::List(Err("Core is not reachable".into())),
                        };
                        send(update);
                        send(RecorderUpdate::Recoverable(recoverable_audio(&home)));
                    }
                    RecorderAction::Detail { id } => {
                        let update = match crate::actions::api_for(&home) {
                            Some(api) => match api.recorder_get(&id).await {
                                Ok(value) => RecorderUpdate::Detail(Ok(value)),
                                Err(error) => RecorderUpdate::Detail(Err(error.to_string())),
                            },
                            None => RecorderUpdate::Detail(Err("Core is not reachable".into())),
                        };
                        send(update);
                    }
                    RecorderAction::Transcript { id } => {
                        let page = match crate::actions::api_for(&home) {
                            Some(api) => read_transcript_pages(&api, &id).await,
                            None => Err("Core is not reachable".to_owned()),
                        };
                        send(RecorderUpdate::Transcript { id, page });
                    }
                    RecorderAction::Retry { id } => {
                        let result = match crate::actions::api_for(&home) {
                            Some(api) => api.http.call(
                                reqwest::Method::POST,
                                &format!("/api/recordings/{id}/transcript/retry"),
                                &[],
                                Some(&serde_json::json!({"scope": "failed"})),
                                None,
                                None,
                            ).await.map(|_| ()).map_err(|error| error.to_string()),
                            None => Err("Core is not reachable".to_owned()),
                        };
                        send(RecorderUpdate::Retry(result));
                    }
                    RecorderAction::Delete { id } => {
                        let result = match crate::actions::api_for(&home) {
                            Some(api) => api.http.call(
                                reqwest::Method::DELETE,
                                &format!("/api/recordings/{id}"),
                                &[],
                                None,
                                None,
                                None,
                            ).await.map(|_| ()).map_err(|error| error.to_string()),
                            None => Err("Core is not reachable".to_owned()),
                        };
                        send(RecorderUpdate::Deleted { id, result });
                    }
                    RecorderAction::CopyTranscript { id } => {
                        let result = copy_transcript(&home, &id).await;
                        send(result);
                    }
                    RecorderAction::Recover => {
                        if ensure_driver(&mut driver).is_err() {
                            continue;
                        }
                        let result = match driver.as_mut() {
                            Some(d) => d.recover_pending().await.map_err(|error| error.to_string()),
                            None => Err("Core is not reachable".to_owned()),
                        };
                        send(RecorderUpdate::Recovered(result));
                        send(RecorderUpdate::Recoverable(recoverable_audio(&home)));
                        publish(&mut driver);
                    }
                }
            }
            _ = tick.tick() => {
                if let Some(d) = driver.as_mut() {
                    if let Some(stream_id) = current_stream() {
                        let snapshot = d.snapshot();
                        if snapshot.rebind_required
                            && last_rebind.elapsed() >= Duration::from_secs(2)
                        {
                            let _ = d.rebind(stream_id);
                            last_rebind = Instant::now();
                        }
                    }
                    for _ in 0..16 {
                        match d.step(started.elapsed()).await {
                            Ok(true) => {}
                            Ok(false) => break,
                            Err(error) => {
                                send(RecorderUpdate::Failed {
                                    kind: "driver".into(),
                                    message: error.to_string(),
                                });
                                break;
                            }
                        }
                    }
                    publish(&mut driver);
                }
            }
        }
    }
}

async fn read_transcript_pages(api: &CoreApi, id: &str) -> Result<Value, String> {
    let mut after: i64 = -1;
    let mut segments: Vec<Value> = Vec::new();
    let mut provisional = Value::Null;
    let mut state = Value::Null;
    let mut complete = false;
    for _ in 0..40 {
        let page = api
            .http
            .get(
                &format!("/api/recordings/{id}/transcript"),
                &[
                    ("after".into(), after.to_string()),
                    ("limit".into(), "200".into()),
                ],
            )
            .await
            .map_err(|error| error.to_string())?;
        let rows = page["segments"].as_array().cloned().unwrap_or_default();
        let count = rows.len();
        segments.extend(rows);
        if provisional.is_null() {
            provisional = page["live"]["provisional"].clone();
        }
        state = page["state"].clone();
        complete = page["complete"].as_bool().unwrap_or(false);
        let next = page["next_after"].as_i64().unwrap_or(after);
        if count == 0 || next <= after {
            break;
        }
        after = next;
        if count < 200 {
            break;
        }
    }
    Ok(serde_json::json!({
        "segments": segments,
        "live": {"provisional": provisional},
        "state": state,
        "complete": complete,
    }))
}

async fn copy_transcript(home: &std::path::Path, id: &str) -> RecorderUpdate {
    let Some(api) = crate::actions::api_for(home) else {
        return RecorderUpdate::Error("Core is not reachable".into());
    };
    match api
        .http
        .get(&format!("/api/recordings/{id}/transcript.json"), &[])
        .await
    {
        Ok(value) => {
            let complete = value["complete"].as_bool().unwrap_or(false);
            let text = value["segments"]
                .as_array()
                .map(|rows| {
                    rows.iter()
                        .filter(|row| row["status"].as_str() != Some("skipped"))
                        .filter_map(|row| row["text"].as_str())
                        .filter(|text| !text.is_empty())
                        .collect::<Vec<_>>()
                        .join(" ")
                })
                .unwrap_or_default();
            RecorderUpdate::Copied { text, complete }
        }
        Err(error) => RecorderUpdate::Error(error.to_string()),
    }
}

pub async fn run(
    cfg: SyncConfig,
    out: mpsc::UnboundedSender<SyncMsg>,
    cmds: mpsc::UnboundedReceiver<Command>,
) {
    let home = cfg.home.clone();
    let client_id = format!("tui-{}", uuid::Uuid::new_v4().simple());
    let recorder_client_id = client_id.clone();
    let (recorder, recorder_commands) = mpsc::unbounded_channel();
    let recorder_out = out.clone();
    let mut cx = Ctx {
        cfg,
        out,
        cmds,
        client_id,
        stream_id: None,
        view: None,
        view_reported: None,
        view_seq: 0,
        view_inflight: None,
        view_retry_at: None,
        view_backoff: Backoff::new(&[1.0, 2.0, 4.0, 8.0, 16.0, 30.0]),
        selected: None,
        resync: None,
        catalog: None,
        config: true,
        context: None,
        context_reads: Default::default(),
        draft: None,
        inputs: None,
        history: None,
        watch: Vec::new(),
        watch_load: Vec::new(),
        config_patch: None,
        worktrees: None,
        project_worktrees: Vec::new(),
        resources: false,
        skill_mcp: None,
        files: None,
        complete: None,
        newest_turn: None,
        recorder: Some(recorder),
        shutdown: false,
    };
    // The task holds its own receiver; dropping it stops the recorder task.
    tokio::spawn(recorder_task(
        home,
        recorder_out,
        recorder_commands,
        recorder_client_id,
    ));
    let mut backoff = cx.cfg.backoff.clone();
    let mut identity: Option<Identity> = None;
    let mut had_live = false;
    let mut pending: Option<ResyncCause> = Some(ResyncCause::Initial);
    let mut cursor: Option<String> = None;
    let mut bootstrap_tried = false;
    let mut last_error = String::new();
    // A dropped stream resumes from its cursor; the selected session's turn
    // summaries are re-read too (`/turn?after=`), so none can be missed.
    let mut refetch_turns = false;

    cx.state(ConnectionState::Connecting);
    loop {
        if cx.shutdown {
            return;
        }
        let (api, daemon, info) =
            match handshake(&mut cx, &mut bootstrap_tried, &mut last_error).await {
                Handshake::Ready { api, daemon, info } => (api, daemon, *info),
                Handshake::Stop => return,
                Handshake::Retry => {
                    if !cx
                        .pause(cx.cfg.park_interval.min(backoff.next_delay()))
                        .await
                    {
                        return;
                    }
                    continue;
                }
            };
        bootstrap_tried = false;

        // Daemon restart: a different pid or token means a new daemon.
        let now = daemon.identity();
        if identity.as_ref().is_some_and(|prev| *prev != now) {
            pending = Some(ResyncCause::DaemonRestarted);
        }
        identity = Some(now);
        if let Some(cause) = cx.resync.take() {
            pending.get_or_insert(cause);
        }
        // A reconnect to a replaced log would be rejected by the cursor check;
        // detect it here too so we resync before asking.
        if let Some((generation, _)) = cursor.as_deref().and_then(crate::api::parse_cursor) {
            if !info.log.generation.is_empty() && generation != info.log.generation {
                pending.get_or_insert(ResyncCause::Cursor("cursor_generation".into()));
            }
        }
        let compat = check_compat(info.api_version, CLIENT_API);
        cx.send(SyncMsg::Handshake { info, compat });

        let resyncing = pending.is_some();
        if let Some(cause) = pending.take() {
            match api
                .snapshot(cx.selected.as_deref(), Some(cx.cfg.message_limit))
                .await
            {
                Ok(snapshot) => {
                    cursor = Some(snapshot.watermark.head_cursor.clone());
                    if cursor.as_deref().is_none_or(str::is_empty) {
                        cursor = Some(format_cursor(
                            &snapshot.watermark.generation,
                            snapshot.watermark.head,
                        ));
                    }
                    cx.send(SyncMsg::Snapshot {
                        snapshot: Box::new(snapshot),
                        cause,
                    });
                    cx.resources = true;
                    // Messages in the snapshot are bounded; the timeline wants
                    // the whole history and the diagnostics that carry turn
                    // durations and usage. Loaded before the stream opens, so
                    // nothing published meanwhile is lost: the snapshot's head
                    // is the resume cursor.
                    if let Some(sid) = cx.selected.clone() {
                        cx.inputs = Some(sid.clone());
                        cx.history = None;
                        if let Some(h) = load_history(&api, &sid).await {
                            cx.newest_turn = h
                                .turns
                                .iter()
                                .rev()
                                .find(|t| t.status != "running")
                                .map(|t| (sid.clone(), t.id.clone()));
                            cx.send(SyncMsg::History(Box::new(h)));
                        }
                    }
                    // Watched views may have missed events: read them again.
                    cx.watch_load = cx.watch.clone();
                    for sid in std::mem::take(&mut cx.watch_load) {
                        if let Some(h) = load_history(&api, &sid).await {
                            cx.send(SyncMsg::History(Box::new(h)));
                        }
                    }
                }
                Err(e) => {
                    pending = Some(cause);
                    last_error = e.to_string();
                    if e.is_auth() {
                        cx.state(ConnectionState::AuthFailed {
                            reason: last_error.clone(),
                        });
                        if !wait_for_change(&mut cx, &identity).await {
                            return;
                        }
                    } else if !reconnect_pause(&mut cx, &mut backoff, &last_error, had_live).await {
                        return;
                    }
                    continue;
                }
            }
        }

        if !resyncing && std::mem::take(&mut refetch_turns) {
            if let Some(sid) = cx.selected.clone() {
                let anchor = cx
                    .newest_turn
                    .as_ref()
                    .filter(|(s, _)| *s == sid)
                    .map(|(_, t)| t.clone());
                let page = match anchor {
                    Some(after) => load_turns_after(&api, &sid, &after).await,
                    None => api.turns(&sid, None, None, Some(TURN_PAGE)).await.ok(),
                };
                if let Some(page) = page {
                    if let Some(t) = page.turns.iter().rev().find(|t| t.status != "running") {
                        cx.newest_turn = Some((sid.clone(), t.id.clone()));
                    }
                    cx.send(SyncMsg::Turns {
                        session: sid,
                        page: Box::new(page),
                    });
                }
            }
        }

        // A selection made while reconnecting, with no snapshot this pass.
        if let Some(sid) = cx.history.take() {
            if let Some(h) = load_history(&api, &sid).await {
                cx.send(SyncMsg::History(Box::new(h)));
            }
        }
        for sid in std::mem::take(&mut cx.watch_load) {
            if let Some(h) = load_history(&api, &sid).await {
                cx.send(SyncMsg::History(Box::new(h)));
            }
        }
        load_config(&mut cx, &api).await;
        load_context(&mut cx, &api);
        load_draft(&mut cx, &api).await;
        load_resources(&mut cx, &api).await;
        load_inputs(&mut cx, &api).await;
        // A request received while reconnecting must not be stranded until a
        // later command arrives on the live stream.
        if let Some(project) = cx.catalog.take() {
            if let Some((models, agents, source, last_model, providers, problems)) =
                load_catalog(&api, project.as_deref()).await
            {
                if !cx.send(SyncMsg::Catalog {
                    models,
                    agents,
                    source,
                    last_model,
                    providers,
                    problems,
                }) {
                    return;
                }
            }
        }
        spawn_slow_reads(&mut cx, &api);
        load_skill_mcp(&mut cx, &api).await;
        load_files(&mut cx, &api).await;
        let end = stream(&mut cx, &api, &mut cursor, &mut backoff, &mut had_live).await;
        // A view is bound to the stream that issued its id: never report the
        // old one while reconnecting. The next `server.connected` sets a new id.
        cx.stream_id = None;
        cx.view_reported = None;
        cx.view_inflight = None;
        cx.view_retry_at = None;
        cx.view_backoff.reset();
        match end {
            StreamEnd::Stop => return,
            StreamEnd::Resync { cause, progressed } => {
                // Resync at once, but never hot-loop on a server that keeps rejecting us.
                pending = Some(cause);
                if progressed {
                    backoff.reset();
                } else if !cx.pause(backoff.next_delay()).await {
                    return;
                }
            }
            StreamEnd::Dropped(reason) => {
                last_error = reason;
                refetch_turns = true;
                if !reconnect_pause(&mut cx, &mut backoff, &last_error, had_live).await {
                    return;
                }
            }
            StreamEnd::AuthFailed(reason) => {
                cx.state(ConnectionState::AuthFailed { reason });
                if !wait_for_change(&mut cx, &identity).await {
                    return;
                }
            }
        }
    }
}

/// Git and worktree reads take tens of ms each; they run beside the stream
/// so the next command (often a switch, at launch too) is not held up.
fn spawn_slow_reads(cx: &mut Ctx, api: &CoreApi) {
    if let Some(session) = cx.worktrees.take() {
        let api = api.clone();
        tokio::spawn(async move {
            let _ = api.worktrees(&session).await;
        });
    }
    for project in std::mem::take(&mut cx.project_worktrees) {
        let (api, out) = (api.clone(), cx.out.clone());
        tokio::spawn(async move {
            let data = api
                .project_worktrees(&project)
                .await
                .map_err(|e| e.to_string());
            let _ = out.send(SyncMsg::ProjectWorktrees { project, data });
        });
    }
}

/// Refresh the listings used by a not-yet-created session. The endpoints are
/// independent, so a failure in one keeps the others available. The project
/// MCP read is keyed by the exact project id the preview is for; it returns
/// enabled, disabled, failed and invalid entries without spawning any server.
async fn load_skill_mcp(cx: &mut Ctx, api: &CoreApi) {
    let Some(project) = cx.skill_mcp.take() else {
        return;
    };
    let query = [("project", project.clone())];
    let (skills, mcp, project_mcp) = tokio::join!(
        api.http.get("/api/skill", &query),
        api.http.get("/api/mcp", &[]),
        api.project_mcp(&project),
    );
    let msg = SyncMsg::SkillMcp {
        project,
        skills: skills.map_err(|e| e.to_string()),
        mcp: mcp.map_err(|e| e.to_string()),
        project_mcp: project_mcp.map_err(|e| e.to_string()),
    };
    cx.send(msg);
}

async fn load_files(cx: &mut Ctx, api: &CoreApi) {
    if let Some((project, session, path, file)) = cx.files.take() {
        let result = if file {
            api.file(&project, session.as_deref(), &path).await
        } else {
            api.files(&project, session.as_deref(), &path).await
        };
        cx.send(SyncMsg::Files {
            project,
            session,
            path,
            file,
            data: result.map_err(|e| e.to_string()),
        });
    }
}

async fn load_completion(cx: &mut Ctx, api: &CoreApi) {
    if let Some((project, session, query, limit)) = cx.complete.take() {
        let result = api
            .files_search(&project, session.as_deref(), &query, limit)
            .await;
        cx.send(SyncMsg::Completion {
            project,
            session,
            query,
            limit,
            data: result.map_err(|e| e.to_string()),
        });
    }
}

/// Picker data: the connected providers' models and the agents that can run in
/// this project. Read-only; a failure just leaves the picker empty.
async fn load_inputs(cx: &mut Ctx, api: &CoreApi) {
    if let Some(session) = cx.inputs.take() {
        if let Ok(data) = api
            .http
            .get(&format!("/api/session/{session}/input"), &[])
            .await
        {
            cx.send(SyncMsg::Inputs { session, data });
        }
    }
}

#[derive(Default)]
struct ContextReads {
    running: bool,
    waiting: Vec<String>,
}

/// Start the requested context read without waiting for it. Core builds the
/// whole request body for one, so a burst of events coalesces into one read
/// per session, and a switch is never held up behind them.
fn load_context(cx: &mut Ctx, api: &CoreApi) {
    let Some(session) = cx.context.take() else {
        return;
    };
    {
        let mut reads = cx.context_reads.lock().unwrap();
        if reads.running {
            if !reads.waiting.contains(&session) {
                reads.waiting.push(session);
            }
            return;
        }
        reads.running = true;
    }
    let (api, out, reads) = (api.clone(), cx.out.clone(), cx.context_reads.clone());
    tokio::spawn(async move {
        let mut session = session;
        loop {
            if let Ok(data) = api
                .http
                .get(
                    &format!("/api/session/{session}/context"),
                    &[("summary", "1".to_owned())],
                )
                .await
            {
                let _ = out.send(SyncMsg::Context { session, data });
            }
            let mut r = reads.lock().unwrap();
            if r.waiting.is_empty() {
                r.running = false;
                return;
            }
            session = r.waiting.remove(0);
        }
    });
}

async fn load_draft(cx: &mut Ctx, api: &CoreApi) {
    if let Some(request) = cx.draft.take() {
        let data = api
            .http
            .call(
                reqwest::Method::POST,
                "/api/draft/context",
                &[],
                Some(&request),
                None,
                None,
            )
            .await
            .map(|r| r.body)
            .map_err(|e| e.to_string());
        cx.send(SyncMsg::DraftContext { request, data });
    }
}

async fn load_resources(cx: &mut Ctx, api: &CoreApi) {
    if !std::mem::take(&mut cx.resources) {
        return;
    }
    let (kernels, jobs) = tokio::join!(api.kernels(), api.bash_jobs());
    cx.send(SyncMsg::Resources {
        kernels: kernels.map_err(|e| e.to_string()),
        jobs: jobs.map_err(|e| e.to_string()),
    });
}

async fn load_config(cx: &mut Ctx, api: &CoreApi) {
    if let Some((revision, path, value)) = cx.config_patch.take() {
        let body =
            serde_json::json!({"expected_revision": revision, "set": {path: value}, "unset": []});
        let result = api
            .http
            .call(
                reqwest::Method::PATCH,
                "/api/config",
                &[],
                Some(&body),
                None,
                None,
            )
            .await
            .map(|r| r.body)
            .map_err(|e| e.to_string());
        cx.send(SyncMsg::Config(result));
        cx.config = true;
    }
    if std::mem::take(&mut cx.config) {
        cx.send(SyncMsg::Config(
            api.http
                .get("/api/config", &[])
                .await
                .map_err(|e| e.to_string()),
        ));
    }
}

/// What `SyncMsg::Catalog` carries, in field order.
type CatalogData = (
    Vec<Value>,
    Vec<Value>,
    String,
    String,
    Vec<Value>,
    Vec<Value>,
);

async fn load_catalog(api: &CoreApi, project: Option<&str>) -> Option<CatalogData> {
    let models = api.models().await.ok()?;
    let mut agents = api.agents(project).await.ok()?;
    // The list omits the system prompt body; the new-session context preview
    // shows its first line, so fetch each agent's prompt (a miss leaves none).
    if let Some(list) = agents.get_mut("agents").and_then(Value::as_array_mut) {
        for agent in list {
            let Some(name) = agent["name"].as_str().map(str::to_owned) else {
                continue;
            };
            if let Some(prompt) = api
                .agent(&name, project)
                .await
                .ok()
                .and_then(|a| a["prompt"].as_str().map(str::to_owned))
            {
                agent["prompt"] = Value::String(prompt);
            }
        }
    }
    let last_model = api
        .last_model()
        .await
        .ok()
        .and_then(|v| v["model"].as_str().map(str::to_owned))
        .unwrap_or_default();
    Some((
        models
            .get("models")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
        agents
            .get("agents")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
        models
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        last_model,
        api.providers()
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default(),
        agents
            .get("problems")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
    ))
}

/// Turn summaries are read 200 to a page (Core's maximum) when a reconnect catches up.
const TURN_PAGE: u32 = 200;
/// Pages of turn summaries a reconnect reads.
const TURN_PAGES: usize = 10;

/// Messages a session opens with; `turn.load_older` reads this many more each time.
pub const MESSAGE_PAGE: u32 = 200;

/// The session's history in one `GET …/open` request: the row, the starting context, the newest
/// page of messages and exactly the turn summaries covering them. Older history is paged with its
/// turns on scroll. The event log is not read.
async fn load_history(api: &CoreApi, sid: &str) -> Option<History> {
    let started = std::time::Instant::now();
    let (h, open_ms) = load_open(api, sid).await;
    crate::perf::event(
        "history",
        json!({
            "session": sid,
            "ms": crate::perf::ms(started.elapsed()),
            "messages": h.messages.len(),
            "turns": h.turns.len(),
            "error": h.turns_error,
            "phases": { "open_ms": open_ms, "requests": 1 },
        }),
    );
    Some(h)
}

async fn load_open(api: &CoreApi, sid: &str) -> (History, f64) {
    let started = std::time::Instant::now();
    let bundle = api.open(sid, MESSAGE_PAGE).await;
    let open_ms = crate::perf::ms(started.elapsed());
    // A bundle that cannot be read must never leave the timeline silently blank: say why (plan 10).
    let h = match bundle {
        Ok(b) => History {
            session_id: sid.to_owned(),
            messages: b.messages,
            messages_more: b.messages_more,
            starting: b.starting,
            session: b.session,
            turns: b.turns,
            more_before: b.turns_more_before,
            floor: b.floor.map(|f| f.seq),
            usage: b.usage,
            turns_error: None,
        },
        Err(e) => History {
            session_id: sid.to_owned(),
            turns_error: Some(format!("messages failed to load ({e})")),
            ..Default::default()
        },
    };
    (h, open_ms)
}

/// After a dropped stream resumed from its cursor: the turns that started
/// after `after` (the newest closed turn the app knows), every page.
async fn load_turns_after(api: &CoreApi, sid: &str, after: &str) -> Option<TurnPage> {
    let mut out = api
        .turns(sid, Some(after), None, Some(TURN_PAGE))
        .await
        .ok()?;
    let mut pages = 1;
    while out.more_after && pages < TURN_PAGES {
        let Some(last) = out.turns.last().map(|t| t.id.clone()) else {
            break;
        };
        let Ok(mut page) = api.turns(sid, Some(&last), None, Some(TURN_PAGE)).await else {
            break;
        };
        out.turns.append(&mut page.turns);
        out.more_after = page.more_after;
        out.usage = page.usage;
        out.floor = page.floor;
        pages += 1;
    }
    Some(out)
}

async fn reconnect_pause(
    cx: &mut Ctx,
    backoff: &mut Backoff,
    reason: &str,
    had_live: bool,
) -> bool {
    let delay = backoff.next_delay();
    let state = if had_live {
        ConnectionState::Reconnecting {
            attempt: backoff.failures(),
            retry_in: delay,
            reason: reason.to_owned(),
        }
    } else {
        ConnectionState::Offline {
            reason: reason.to_owned(),
        }
    };
    cx.state(state);
    cx.pause(delay).await
}

/// Park until `daemon.json` shows a different daemon (pid/token) or a command
/// asks us to retry. Returns false on shutdown.
async fn wait_for_change(cx: &mut Ctx, identity: &Option<Identity>) -> bool {
    loop {
        if !cx.pause(cx.cfg.park_interval).await {
            return false;
        }
        let now = discovery::read_info(&cx.cfg.home).map(|d| d.identity());
        if now.is_some() && now != *identity {
            return true;
        }
        if cx.resync.is_some() {
            return true;
        }
    }
}

async fn handshake(cx: &mut Ctx, bootstrap_tried: &mut bool, last_error: &mut String) -> Handshake {
    let daemon = match discovery::read_info(&cx.cfg.home) {
        Some(d) => d,
        None => match bootstrap(cx, bootstrap_tried, last_error).await {
            Some(d) => d,
            None => return Handshake::Retry,
        },
    };
    let http = match Http::new(&daemon.url, &daemon.token) {
        Ok(h) => h,
        Err(e) => {
            cx.state(ConnectionState::Offline {
                reason: e.to_string(),
            });
            return Handshake::Retry;
        }
    };
    let api = CoreApi::new(http);
    match api.info().await {
        Ok(info) => {
            if let Compat::Incompatible { core, update } =
                check_compat(info.api_version, CLIENT_API)
            {
                // Halt: nothing past /api/info is sent until Core changes.
                cx.state(ConnectionState::Incompatible {
                    core: core.to_string(),
                    update,
                });
                let id = Some(daemon.identity());
                let compat = Compat::Incompatible { core, update };
                cx.send(SyncMsg::Handshake { info, compat });
                return if wait_for_change(cx, &id).await {
                    Handshake::Retry
                } else {
                    Handshake::Stop
                };
            }
            Handshake::Ready {
                api,
                daemon,
                info: Box::new(info),
            }
        }
        Err(e) if e.is_auth() => {
            *last_error = e.to_string();
            cx.state(ConnectionState::AuthFailed {
                reason: e.to_string(),
            });
            let id = Some(daemon.identity());
            if wait_for_change(cx, &id).await {
                Handshake::Retry
            } else {
                Handshake::Stop
            }
        }
        Err(e) => {
            // Unreachable: a stale daemon.json (crashed daemon) or a restart in progress.
            *last_error = e.to_string();
            if bootstrap(cx, bootstrap_tried, last_error).await.is_some() {
                return Handshake::Retry;
            }
            if !matches!(e, Error::Network(_)) {
                cx.state(ConnectionState::Offline {
                    reason: e.to_string(),
                });
            }
            Handshake::Retry
        }
    }
}

/// Start Core once per outage. `None` means we could not (or may not) start it.
async fn bootstrap(cx: &mut Ctx, tried: &mut bool, last_error: &mut String) -> Option<DaemonInfo> {
    if !cx.cfg.bootstrap || *tried {
        let reason = if last_error.is_empty() {
            "Core is not running (no daemon.json)".to_owned()
        } else {
            last_error.clone()
        };
        cx.state(ConnectionState::Offline { reason });
        return None;
    }
    *tried = true;
    cx.state(ConnectionState::Starting);
    match discovery::start_core(&cx.cfg.home).await {
        Ok(d) => Some(d),
        Err(e) => {
            *last_error = e.clone();
            cx.state(ConnectionState::Offline { reason: e });
            None
        }
    }
}

enum StreamEnd {
    Stop,
    /// `progressed`: the stream got going (Connected or an event) before this.
    Resync {
        cause: ResyncCause,
        progressed: bool,
    },
    Dropped(String),
    AuthFailed(String),
}

async fn stream(
    cx: &mut Ctx,
    api: &CoreApi,
    cursor: &mut Option<String>,
    backoff: &mut Backoff,
    had_live: &mut bool,
) -> StreamEnd {
    let mut events = match api.http.events(None, cursor.as_deref()).await {
        Ok(s) => s,
        Err(Error::Api(e)) if e.is_resync() => {
            let code = e.code.clone().unwrap_or_else(|| "resync".into());
            return StreamEnd::Resync {
                cause: ResyncCause::Cursor(code),
                progressed: false,
            };
        }
        Err(e) if e.is_auth() => return StreamEnd::AuthFailed(e.to_string()),
        Err(e) => return StreamEnd::Dropped(e.to_string()),
    };
    let mut progressed = false;
    // Reports run beside the stream. One in flight at a time keeps the latest
    // view ordering; results wake the loop without delaying event delivery.
    let (report_tx, mut report_rx) = mpsc::unbounded_channel();
    let retry_sleep = tokio::time::sleep(Duration::from_secs(3600));
    tokio::pin!(retry_sleep);
    loop {
        tokio::select! {
            frame = events.next() => {
                let sse = match frame {
                    Ok(Some(f)) => f,
                    Ok(None) => return StreamEnd::Dropped("the event stream ended".into()),
                    Err(e) => return StreamEnd::Dropped(e.to_string()),
                };
                match Frame::from_sse(&sse) {
                    Ok(Frame::Connected(c)) => {
                        let own_generation = cursor.as_deref().and_then(crate::api::parse_cursor).map(|(g, _)| g.to_owned());
                        if own_generation.is_some_and(|g| g != c.generation) {
                            return StreamEnd::Resync { cause: ResyncCause::Cursor("cursor_generation".into()), progressed: false };
                        }
                        // Every connection has its own ephemeral identity, so a
                        // reconnect always reports the latest view again.
                        remember_stream(c.stream_id.as_deref());
                        cx.stream_id = c.stream_id;
                        let _ = cx.send(SyncMsg::Recorder(RecorderUpdate::Reconnected));
                        if let Some(recorder) = &cx.recorder {
                            let _ = recorder.send(RecorderAction::Connected);
                        }
                        cx.view_reported = None;
                        cx.view_inflight = None;
                        cx.view_retry_at = None;
                        cx.view_backoff.reset();
                        *had_live = true;
                        progressed = true;
                        backoff.reset();
                        if !cx.state(ConnectionState::Live) {
                            return StreamEnd::Stop;
                        }
                        // Register the stream before fetching the checkpoint: deltas
                        // produced during this read are queued and deduplicated by seq.
                        if let Some(sid) = cx.selected.clone() {
                            if let Ok(page) = api.turns(&sid, None, None, Some(1)).await {
                                cx.send(SyncMsg::Turns { session: sid, page: Box::new(page) });
                            }
                        }
                        cx.start_view_report(api, &report_tx);
                    }
                    Ok(Frame::Overflow(_)) => return StreamEnd::Resync { cause: ResyncCause::Overflow, progressed },
                    Ok(Frame::Event(e)) => {
                        if e.kind.starts_with("input.") { if let Some(id) = e.session_id.as_ref() { cx.inputs = Some(id.clone()); load_inputs(cx, api).await; } }
                        if e.kind == "config.changed" { cx.config = true; load_config(cx, api).await; }
                        let resources_changed = matches!(e.kind.as_str(), "code.cell" | "kernel.deleted" | "bash.started" | "bash.finished");
                        if matches!(e.kind.as_str(), "turn.summary" | "message.added" | "session.updated") {
                            if let Some(id) = e.session_id.as_ref().filter(|id| cx.selected.as_ref() == Some(*id)) {
                                cx.context = Some(id.clone());
                                load_context(cx, api);
                            }
                        }
                        if e.kind == "turn.summary" && e.data["status"].as_str() != Some("running") {
                            if let (Some(sid), Some(turn)) = (e.session_id.as_deref(), e.data["id"].as_str()) {
                                if cx.selected.as_deref() == Some(sid) {
                                    cx.newest_turn = Some((sid.to_owned(), turn.to_owned()));
                                }
                            }
                        }
                        let next = e.cursor.clone().or_else(|| {
                            cursor.as_deref().and_then(crate::api::parse_cursor).filter(|_| e.id > 0).map(|(g, _)| format_cursor(g, e.id))
                        });
                        if !cx.send(SyncMsg::Event(e)) {
                            return StreamEnd::Stop;
                        }
                        if resources_changed {
                            cx.resources = true;
                            load_resources(cx, api).await;
                        }
                        // Advance only after the app has the event.
                        if next.is_some() {
                            *cursor = next;
                        }
                        progressed = true;
                        backoff.reset();
                    }
                    Err(_) => return StreamEnd::Resync { cause: ResyncCause::Cursor("undecodable event".into()), progressed },
                }
            }
            cmd = cx.cmds.recv() => {
                match cmd {
                    Some(Command::Manager(call)) => {
                        cx.run_manager(api, call);
                        continue;
                    }
                    Some(c) => {
                        let view_changed = matches!(&c, Command::View(_));
                        let was_catalog = matches!(c, Command::Config | Command::Context(_) | Command::DraftContext(_) | Command::PatchConfig { .. } | Command::Catalog(_) | Command::Worktrees(_) | Command::ProjectWorktrees(_) | Command::Resources | Command::SkillMcp(_) | Command::Files { .. } | Command::Complete { .. });
                        cx.handle(c);
                        if cx.shutdown { return StreamEnd::Stop; }
                        if view_changed {
                            // A view report reads nothing and loads nothing; it
                            // runs in the background and never stalls the stream.
                            cx.start_view_report(api, &report_tx);
                            continue;
                        }
                        // History loads beside the stream, which keeps applying
                        // events; the reducer merges both in either order.
                        if let Some(sid) = cx.history.take() {
                            let (api, out) = (api.clone(), cx.out.clone());
                            tokio::spawn(async move {
                                if let Some(h) = load_history(&api, &sid).await {
                                    let _ = out.send(SyncMsg::History(Box::new(h)));
                                }
                            });
                        }
                        for sid in std::mem::take(&mut cx.watch_load) {
                            let (api, out) = (api.clone(), cx.out.clone());
                            tokio::spawn(async move {
                                if let Some(h) = load_history(&api, &sid).await {
                                    let _ = out.send(SyncMsg::History(Box::new(h)));
                                }
                            });
                        }
                        load_config(cx, api).await;
                        load_context(cx, api);
                        load_draft(cx, api).await;
                        load_resources(cx, api).await;
                        load_inputs(cx, api).await;
                        if let Some(project) = cx.catalog.take() {
                            if let Some((models, agents, source, last_model, providers, problems)) = load_catalog(api, project.as_deref()).await {
                                if !cx.send(SyncMsg::Catalog { models, agents, source, last_model, providers, problems }) {
                                    return StreamEnd::Stop;
                                }
                            }
                        }
                        spawn_slow_reads(cx, api);
                        load_skill_mcp(cx, api).await;
                        load_files(cx, api).await;
                        load_completion(cx, api).await;
                        if !was_catalog {
                            if let Some(cause) = cx.resync.take() {
                                return StreamEnd::Resync { cause, progressed };
                            }
                        }
                    }
                    None => return StreamEnd::Stop,
                }
            }
            result = report_rx.recv() => {
                if let Some(result) = result {
                    match cx.finish_view_report(result) {
                        ViewAction::Mismatch => return StreamEnd::Resync { cause: ResyncCause::Requested, progressed },
                        ViewAction::Retry(delay) => {
                            retry_sleep.as_mut().reset(tokio::time::Instant::now() + delay);
                        }
                        ViewAction::None => {}
                    }
                    cx.start_view_report(api, &report_tx);
                }
            }
            () = &mut retry_sleep, if cx.view_retry_at.is_some() => {
                cx.view_retry_at = None;
                cx.start_view_report(api, &report_tx);
            }
        }
    }
}

#[cfg(test)]
mod recorder_startup_tests {
    use super::*;
    use crate::recorder_driver::{RecoveryIssue, RecoveryIssueKind, RecoveryReport};

    #[test]
    fn restart_required_blocks_start_with_the_shared_detail() {
        let detail =
            "Core's recorder create guard is full; restart Core before creating another recording";
        let report = RecoveryReport {
            attempted: 1,
            recovered: 0,
            truncated: false,
            issues: vec![RecoveryIssue {
                recording_id: None,
                kind: RecoveryIssueKind::CoreRestartRequired,
                detail: detail.into(),
            }],
        };
        assert_eq!(startup_block(&Ok(report), 1).as_deref(), Some(detail));
    }

    #[test]
    fn an_outstanding_create_intent_is_counted_and_blocks_start() {
        let home = std::env::temp_dir().join(format!(
            "nexus-recorder-startup-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&home).unwrap();
        let store = crate::recorder_spool::SpoolStore::new(&home).unwrap();
        let pending = store
            .begin_pending_start(
                "K1",
                crate::recorder_spool::Secret::new("recovery-capability"),
                "tui",
                None,
            )
            .unwrap();
        assert_eq!(pending_start_count(&home), 1);
        let report = RecoveryReport {
            attempted: 1,
            recovered: 0,
            truncated: false,
            issues: Vec::new(),
        };
        assert_eq!(
            startup_block(&Ok(report), pending_start_count(&home)).as_deref(),
            Some("A previous recording start is still being recovered.")
        );
        drop(pending);
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn a_resolved_create_intent_unblocks_start() {
        let report = RecoveryReport {
            attempted: 1,
            recovered: 1,
            truncated: false,
            issues: Vec::new(),
        };
        assert!(startup_block(&Ok(report), 0).is_none());
        assert_eq!(
            startup_block(&Err("Core is not reachable".into()), 0).as_deref(),
            Some("Core is not reachable")
        );
    }
}
