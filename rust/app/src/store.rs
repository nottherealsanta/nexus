//! App state: workspace, session list, selected session, the live wire `view`
//! (+ `seq`) and connection status. Components read these signals and call
//! the `Store` methods; nothing else talks to the network.
//!
//! The live loop per selected session: `session-view` snapshot, then repeated
//! `session-poll` long-polls whose frames are applied in one `batch`
//! ([`crate::ops`]). A resync frame, an op failure or an HTTP error
//! re-snapshots (backoff 1s -> 10s). Selecting another session cancels the
//! loop; results are also compared with the selected id so a late answer for a
//! previous session is dropped. Writes are never retried.

use std::cell::RefCell;
use std::time::Duration;

use nsh_core::ui::prelude::*;
use serde_json::{Value, json};

use crate::api::{Api, ApiError};
use crate::ops::{Applied, apply_frame};
use crate::present::sessions::{SessionItem, new_session_id, parse_sessions};

/// Long-poll wait asked of the daemon, seconds (it clamps to 25).
const POLL_WAIT: u32 = 25;
const SESSION_LIST_EVERY: Duration = Duration::from_secs(5);

/// Connection state shown in the top bar.
#[derive(Clone, Debug, PartialEq)]
pub enum Conn {
    Connecting,
    Live,
    /// Retrying after an error (message).
    Reconnecting(String),
    /// Auth missing/expired or launch environment missing (message).
    Offline(String),
}

thread_local! {
    static LIVE: RefCell<Option<Spawned<()>>> = const { RefCell::new(None) };
}

/// `Copy` bundle of the app's signals plus the [`Api`].
#[derive(Clone, Copy)]
pub struct Store {
    pub api: Api,
    pub workspace: Signal<String>,
    pub sessions: Signal<Vec<SessionItem>>,
    pub selected: Signal<Option<String>>,
    /// Wire view of the selected session (`Null` until the first snapshot).
    pub view: Signal<Value>,
    /// Last applied `seq` of the selected session.
    pub seq: Signal<u64>,
    pub conn: Signal<Conn>,
    /// Last user-visible action error (send/new-session failures); empty when none.
    pub notice: Signal<String>,
    list_in_flight: Signal<bool>,
    /// Owner of the root component: tasks started from event handlers run
    /// under it so recycled rows cannot cancel them.
    root: Owner,
}

fn session_poll_path(id: &str, cursor: u64) -> String {
    format!("session-poll?session={id}&from_seq={cursor}&wait={POLL_WAIT}")
}

impl Store {
    pub fn new(api: Api) -> Self {
        Store {
            api,
            workspace: signal(String::new()),
            sessions: signal(Vec::new()),
            selected: signal(None),
            view: signal(Value::Null),
            seq: signal(0),
            conn: signal(Conn::Connecting),
            notice: signal(String::new()),
            list_in_flight: signal(false),
            root: Owner::current(),
        }
    }

    fn spawn_root<T: 'static>(self, fut: impl std::future::Future<Output = T> + 'static) -> Spawned<T> {
        self.root.run(|| spawn(fut))
    }

    /// Marks the app unusable with `message` (e.g. missing launch environment).
    pub fn offline(&self, message: impl Into<String>) {
        self.conn.set(Conn::Offline(message.into()));
    }

    /// Connects (ticket redeem / bootstrap), then keeps the session list fresh.
    /// Call once from the root component.
    pub fn start(self) {
        spawn(async move {
            loop {
                match self.api.connect().await {
                    Ok(b) => {
                        self.workspace.set(b.workspace);
                        self.conn.set(Conn::Live);
                        break;
                    }
                    Err(e) if e.unauthorized() || e.status == 400 => {
                        self.offline(auth_message(&e));
                        return;
                    }
                    Err(e) => self.conn.set(Conn::Reconnecting(e.message)),
                }
                timer(Duration::from_secs(2)).await;
            }
            self.refresh_sessions();
        });
        // Owned by the root component: ends with it.
        let _ = set_interval(SESSION_LIST_EVERY, move || self.refresh_sessions());
    }

    /// Fetches `SessionList` (skipped while one is in flight or before connect).
    pub fn refresh_sessions(self) {
        if self.list_in_flight.get_untracked() || self.conn.with_untracked(|c| matches!(c, Conn::Connecting | Conn::Offline(_))) {
            return;
        }
        self.list_in_flight.set(true);
        self.spawn_root(async move {
            match self.api.command(json!({"type": "SessionList"})).await {
                Ok(v) => self.sessions.set(parse_sessions(&v)),
                Err(e) if e.unauthorized() => self.offline(auth_message(&e)),
                Err(_) => {}
            }
            self.list_in_flight.set(false);
        });
    }

    /// Switches the selected session: resets the view and restarts the live loop.
    pub fn select(self, id: String) {
        if self.selected.get_untracked().as_deref() == Some(id.as_str()) {
            return;
        }
        LIVE.with(|l| {
            if let Some(task) = l.borrow_mut().take() {
                task.cancel();
            }
        });
        batch(|| {
            self.selected.set(Some(id.clone()));
            self.view.set(Value::Null);
            self.seq.set(0);
        });
        let task = self.spawn_root(self.live_loop(id));
        LIVE.with(|l| *l.borrow_mut() = Some(task));
    }

    /// Creates a session (`SessionOpen` create+recover) and selects it.
    pub fn new_session(self) {
        let id = new_session_id(nsh_core::time::unix_ms());
        self.spawn_root(async move {
            let cmd = json!({"type": "SessionOpen", "session": id, "create": true, "recover": true});
            match self.api.command(cmd).await {
                Ok(_) => {
                    self.notice.set(String::new());
                    self.select(id);
                    self.refresh_sessions();
                }
                Err(e) => self.notice.set(format!("New session failed: {e}")),
            }
        });
    }

    /// Sends a command once; the caller decides what a failure means.
    pub async fn command(self, command: Value) -> Result<Value, ApiError> {
        let r = self.api.command(command).await;
        if let Err(e) = &r {
            if e.unauthorized() {
                self.offline(auth_message(e));
            }
        }
        r
    }

    fn is_current(self, id: &str) -> bool {
        self.selected.get_untracked().as_deref() == Some(id)
    }

    async fn live_loop(self, id: String) {
        let mut backoff = 1u64;
        loop {
            // 1. snapshot
            let snap = self.api.get_json(&format!("session-view?session={id}")).await;
            if !self.is_current(&id) {
                return;
            }
            let mut cursor = match snap {
                Ok(v) => {
                    let seq = v.get("seq").and_then(Value::as_u64).unwrap_or(0);
                    let view = v.get("view").cloned().unwrap_or(Value::Null);
                    batch(|| {
                        self.view.set(view);
                        self.seq.set(seq);
                        self.conn.set(Conn::Live);
                    });
                    backoff = 1;
                    seq
                }
                Err(e) => {
                    if e.unauthorized() {
                        self.offline(auth_message(&e));
                        return;
                    }
                    if e.status == 404 {
                        // Not open in this daemon yet: open (idempotent), then retry.
                        let open = json!({"type": "SessionOpen", "session": id, "create": true, "recover": true});
                        let _ = self.api.command(open).await;
                    }
                    self.conn.set(Conn::Reconnecting(e.message));
                    timer(Duration::from_secs(backoff)).await;
                    backoff = (backoff * 2).min(10);
                    if !self.is_current(&id) {
                        return;
                    }
                    continue;
                }
            };
            // 2. long-poll until resync or error
            loop {
                let started = nsh_core::time::unix_ms();
                let polled = self.api.get_json(&session_poll_path(&id, cursor)).await;
                if !self.is_current(&id) {
                    return;
                }
                let frames = match polled {
                    Ok(v) => v.get("frames").and_then(Value::as_array).cloned().unwrap_or_default(),
                    Err(e) => {
                        if e.unauthorized() {
                            self.offline(auth_message(&e));
                            return;
                        }
                        self.conn.set(Conn::Reconnecting(e.message));
                        timer(Duration::from_secs(backoff)).await;
                        backoff = (backoff * 2).min(10);
                        break;
                    }
                };
                let mut resync = false;
                if !frames.is_empty() {
                    batch(|| {
                        self.view.update(|view| {
                            for frame in &frames {
                                if let Applied::Resync(_) = apply_frame(view, &mut cursor, frame) {
                                    resync = true;
                                    break;
                                }
                            }
                        });
                        self.seq.set(cursor);
                        self.conn.set(Conn::Live);
                    });
                }
                if resync {
                    // Re-snapshot, with backoff so a failing feed cannot spin.
                    timer(Duration::from_secs(backoff)).await;
                    backoff = (backoff * 2).min(10);
                    break;
                }
                if !frames.is_empty() {
                    backoff = 1;
                }
                // Guard against a server that answers empty polls instantly.
                if frames.is_empty() && nsh_core::time::unix_ms() - started < 500.0 {
                    timer(Duration::from_secs(1)).await;
                }
            }
        }
    }
}

fn auth_message(e: &ApiError) -> String {
    if e.unauthorized() {
        "Session expired or not signed in: run `nexus web` / `nexus app` again".to_owned()
    } else {
        e.message.clone()
    }
}
