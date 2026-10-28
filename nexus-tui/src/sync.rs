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

use serde_json::Value;
use tokio::sync::mpsc;

use crate::api::{
    check_compat, format_cursor, Compat, CoreApi, Event, Frame, History, Info, Snapshot, CLIENT_API,
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
    /// Picker data: connected providers' models and the available agents.
    Catalog {
        models: Vec<Value>,
        agents: Vec<Value>,
        source: String,
        /// Core's default model reference (`provider/id`), empty if unknown.
        default_model: String,
    },
    Files {
        project: String,
        session: Option<String>,
        path: String,
        data: Result<Value, String>,
        file: bool,
    },
    Git {
        session: String,
        data: Result<Value, String>,
    },
    Diff {
        session: String,
        path: String,
        data: Result<Value, String>,
    },
    /// A project's own worktrees, used before a session exists.
    ProjectWorktrees {
        project: String,
        data: Result<Value, String>,
    },
    Event(Event),
}

/// App -> supervisor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// Include this session's bounded message view in snapshots.
    Select(Option<String>),
    /// Fetch a fresh snapshot (the reducer could not continue).
    Resync,
    /// Load the model catalog and agent list for a project (picker data).
    Catalog(Option<String>),
    /// Fetching also publishes a worktree.listed event on Core.
    Worktrees(String),
    /// The project's own checkout, for a session that does not exist yet.
    ProjectWorktrees(String),
    Git(String),
    GitDiff {
        session: String,
        path: String,
    },
    Files {
        project: String,
        session: Option<String>,
        path: String,
        file: bool,
    },
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
    selected: Option<String>,
    /// Set by commands; consumed by the next pass.
    resync: Option<ResyncCause>,
    /// A model/agent picker asked for the catalog of this project.
    catalog: Option<Option<String>>,
    worktrees: Option<String>,
    project_worktrees: Vec<String>,
    git: Option<String>,
    git_diff: Option<(String, String)>,
    files: Option<(String, Option<String>, String, bool)>,
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
                self.selected = s;
                self.resync = Some(ResyncCause::Requested);
            }
            Command::Resync => self.resync = Some(ResyncCause::Requested),
            Command::Catalog(project) => self.catalog = Some(project),
            Command::Worktrees(session) => self.worktrees = Some(session),
            Command::ProjectWorktrees(project) => {
                if !self.project_worktrees.contains(&project) {
                    self.project_worktrees.push(project);
                }
            }
            Command::Git(session) => self.git = Some(session),
            Command::GitDiff { session, path } => self.git_diff = Some((session, path)),
            Command::Files {
                project,
                session,
                path,
                file,
            } => self.files = Some((project, session, path, file)),
            Command::Shutdown => self.shutdown = true,
        }
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
                        let wake = !matches!(c, Command::Select(_));
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

pub async fn run(
    cfg: SyncConfig,
    out: mpsc::UnboundedSender<SyncMsg>,
    cmds: mpsc::UnboundedReceiver<Command>,
) {
    let mut cx = Ctx {
        cfg,
        out,
        cmds,
        selected: None,
        resync: None,
        catalog: None,
        worktrees: None,
        project_worktrees: Vec::new(),
        git: None,
        git_diff: None,
        files: None,
        shutdown: false,
    };
    let mut backoff = cx.cfg.backoff.clone();
    let mut identity: Option<Identity> = None;
    let mut had_live = false;
    let mut pending: Option<ResyncCause> = Some(ResyncCause::Initial);
    let mut cursor: Option<String> = None;
    let mut bootstrap_tried = false;
    let mut last_error = String::new();

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
                    // Messages in the snapshot are bounded; the timeline wants
                    // the whole history and the diagnostics that carry turn
                    // durations and usage. Loaded before the stream opens, so
                    // nothing published meanwhile is lost: the snapshot's head
                    // is the resume cursor.
                    if let Some(sid) = cx.selected.clone() {
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

        // A request received while reconnecting must not be stranded until a
        // later command arrives on the live stream.
        if let Some(project) = cx.catalog.take() {
            if let Some((models, agents, source, default_model)) =
                load_catalog(&api, project.as_deref()).await
            {
                if !cx.send(SyncMsg::Catalog {
                    models,
                    agents,
                    source,
                    default_model,
                }) {
                    return;
                }
            }
        }
        if let Some(session) = cx.worktrees.take() {
            let _ = api.worktrees(&session).await;
        }
        for project in std::mem::take(&mut cx.project_worktrees) {
            let data = api
                .project_worktrees(&project)
                .await
                .map_err(|e| e.to_string());
            if !cx.send(SyncMsg::ProjectWorktrees { project, data }) {
                return;
            }
        }
        load_files(&mut cx, &api).await;
        load_git(&mut cx, &api).await;
        load_git_diff(&mut cx, &api).await;
        match stream(&mut cx, &api, &mut cursor, &mut backoff, &mut had_live).await {
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

async fn load_git(cx: &mut Ctx, api: &CoreApi) {
    if let Some(session) = cx.git.take() {
        let data = api.git_status(&session).await.map_err(|e| e.to_string());
        cx.send(SyncMsg::Git { session, data });
    }
}

async fn load_git_diff(cx: &mut Ctx, api: &CoreApi) {
    if let Some((session, path)) = cx.git_diff.take() {
        let data = api
            .git_diff(&session, &path)
            .await
            .map_err(|e| e.to_string());
        cx.send(SyncMsg::Diff {
            session,
            path,
            data,
        });
    }
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

/// Picker data: the connected providers' models and the agents that can run in
/// this project. Read-only; a failure just leaves the picker empty.
async fn load_catalog(
    api: &CoreApi,
    project: Option<&str>,
) -> Option<(Vec<Value>, Vec<Value>, String, String)> {
    let models = api.models().await.ok()?;
    let agents = api.agents(project).await.ok()?;
    let default_model = api
        .default_model()
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
        default_model,
    ))
}

/// Cap on retained-diagnostic events kept for one session. Messages are not
/// capped (the message endpoint returns all of them), so the timeline is still
/// complete. Paging runs to the end and keeps the newest events, so the kept
/// diagnostics join the live stream without a gap; the UI announces the rest.
pub const HISTORY_EVENT_CAP: usize = 20_000;

async fn load_history(api: &CoreApi, sid: &str) -> Option<History> {
    let messages = api.messages(sid).await.ok()?;
    let starting = api.snapshot_json(sid).await.ok();
    let mut pages = Vec::new();
    let mut after = 0i64;
    let mut failed = None;
    loop {
        match api.event_page(sid, after, 5000).await {
            Ok(page) => {
                let next = page_last_id(&page).unwrap_or(after);
                let more = page.get("more").and_then(Value::as_bool).unwrap_or(false);
                pages.push(page);
                if !more || next <= after {
                    break; // done, or no progress: never loop on a bad page
                }
                after = next;
            }
            Err(e) => {
                failed = Some((after, e.to_string()));
                break;
            }
        }
    }
    let (events, events_gap) = fold_event_pages(pages, failed, HISTORY_EVENT_CAP);
    Some(History {
        session_id: sid.to_owned(),
        messages,
        events,
        starting,
        events_gap,
    })
}

fn page_last_id(page: &Value) -> Option<i64> {
    page.get("events")?.as_array()?.last()?.get("id")?.as_i64()
}

/// Fold stored event pages into the newest `cap` events and a notice saying
/// what is missing: events expired by retention, events beyond the cap, or a
/// page that failed to load (`failed` is the last id read and the error).
pub fn fold_event_pages(
    pages: Vec<Value>,
    failed: Option<(i64, String)>,
    cap: usize,
) -> (Vec<Event>, Option<String>) {
    let mut events: Vec<Event> = Vec::new();
    let mut floor = 0i64;
    for page in &pages {
        if page.get("history_truncated").and_then(Value::as_bool) == Some(true) {
            floor = floor.max(page.get("floor").and_then(Value::as_i64).unwrap_or(0));
        }
        let rows = page.get("events").and_then(Value::as_array);
        events.extend(rows.into_iter().flatten().filter_map(Event::from_stored));
    }
    let mut notes = Vec::new();
    if floor > 0 {
        notes.push(format!(
            "diagnostics at or before event {floor} expired under retention"
        ));
    }
    if events.len() > cap {
        let dropped = events.len() - cap;
        events.drain(..dropped);
        notes.push(format!(
            "{dropped} older diagnostics are not loaded (only the newest {cap} are kept)"
        ));
    }
    if let Some((after, err)) = failed {
        notes.push(format!(
            "diagnostics after event {after} failed to load ({err}); some are missing"
        ));
    }
    let gap = (!notes.is_empty()).then(|| format!("{} (messages are complete)", notes.join("; ")));
    (events, gap)
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
                        *had_live = true;
                        progressed = true;
                        backoff.reset();
                        if !cx.state(ConnectionState::Live) {
                            return StreamEnd::Stop;
                        }
                    }
                    Ok(Frame::Overflow(_)) => return StreamEnd::Resync { cause: ResyncCause::Overflow, progressed },
                    Ok(Frame::Event(e)) => {
                        let next = e.cursor.clone().or_else(|| {
                            cursor.as_deref().and_then(crate::api::parse_cursor).map(|(g, _)| format_cursor(g, e.id))
                        });
                        if !cx.send(SyncMsg::Event(e)) {
                            return StreamEnd::Stop;
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
                    Some(c) => {
                        let was_catalog = matches!(c, Command::Catalog(_) | Command::Worktrees(_) | Command::ProjectWorktrees(_) | Command::Git(_) | Command::GitDiff { .. } | Command::Files { .. });
                        cx.handle(c);
                        if cx.shutdown { return StreamEnd::Stop; }
                        if let Some(project) = cx.catalog.take() {
                            if let Some((models, agents, source, default_model)) = load_catalog(api, project.as_deref()).await {
                                if !cx.send(SyncMsg::Catalog { models, agents, source, default_model }) {
                                    return StreamEnd::Stop;
                                }
                            }
                        }
                        if let Some(session) = cx.worktrees.take() {
                            let _ = api.worktrees(&session).await;
                        }
                        for project in std::mem::take(&mut cx.project_worktrees) {
                            let data = api.project_worktrees(&project).await.map_err(|e| e.to_string());
                            if !cx.send(SyncMsg::ProjectWorktrees { project, data }) {
                                return StreamEnd::Stop;
                            }
                        }
                        load_files(cx, api).await;
                        load_git(cx, api).await;
                        load_git_diff(cx, api).await;
                        if !was_catalog {
                            if let Some(cause) = cx.resync.take() {
                                return StreamEnd::Resync { cause, progressed };
                            }
                        }
                    }
                    None => return StreamEnd::Stop,
                }
            }
        }
    }
}
