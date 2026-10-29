//! View state: what the UI needs on top of the reduced client model.
//!
//! `ConnectionState` is the single answer to "can I trust what I see": the last
//! view stays visible but read-only whenever the connection is not `Live`.

use std::time::Duration;

use crate::api::{Compat, Info, UpdateSide};
use crate::reduce::{Applied, Model, ResyncReason};
use crate::sync::{ResyncCause, SyncMsg};

#[derive(Debug, Clone, PartialEq)]
pub enum ConnectionState {
    /// Core is not running; the bootstrap is starting it (`nexus daemon start`).
    Starting,
    /// Looking for Core, handshaking, loading the snapshot.
    Connecting,
    /// The event stream is open and the model is current.
    Live,
    /// The connection dropped; retrying with backoff. The model is stale.
    Reconnecting {
        attempt: usize,
        retry_in: Duration,
        reason: String,
    },
    /// Core cannot be reached or started; still retrying slowly.
    Offline { reason: String },
    /// 401: the token is wrong. Waits for `daemon.json` to change.
    AuthFailed { reason: String },
    /// Different API major: halted, nothing is sent until Core changes.
    Incompatible { core: String, update: UpdateSide },
}

impl ConnectionState {
    pub fn is_live(&self) -> bool {
        matches!(self, Self::Live)
    }

    /// Mutations are only offered while live; otherwise the view is read-only.
    pub fn read_only(&self) -> bool {
        !self.is_live()
    }

    pub fn label(&self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Connecting => "connecting",
            Self::Live => "live",
            Self::Reconnecting { .. } => "reconnecting",
            Self::Offline { .. } => "offline",
            Self::AuthFailed { .. } => "auth failed",
            Self::Incompatible { .. } => "incompatible",
        }
    }

    pub fn detail(&self) -> String {
        match self {
            Self::Starting => "starting Nexus Core".into(),
            Self::Connecting => "connecting to Nexus Core".into(),
            Self::Live => "connected".into(),
            Self::Reconnecting {
                attempt,
                retry_in,
                reason,
            } => {
                format!(
                    "connection lost ({reason}); retry {attempt} in {:.1}s",
                    retry_in.as_secs_f64()
                )
            }
            Self::Offline { reason } => format!("Nexus Core is not reachable: {reason}"),
            Self::AuthFailed { reason } => format!("authentication failed: {reason}"),
            Self::Incompatible { core, update } => {
                let who = match update {
                    UpdateSide::Client => "update nexus-tui",
                    UpdateSide::Core => "update Nexus Core",
                };
                format!(
                    "incompatible API (Core {core}, client {}): {who}",
                    crate::api::CLIENT_API
                )
            }
        }
    }
}

/// A non-blocking notice shown above the view.
#[derive(Debug, Clone, PartialEq)]
pub enum Banner {
    /// Core has a newer API minor than this client: some features are missing.
    NewerCore {
        core: String,
    },
    /// Core does not report an API version yet; compatibility is unchecked.
    ApiUnreported,
    DaemonRestarted,
    Resynced(String),
}

pub type FileResponse = (
    String,
    Option<String>,
    String,
    Result<serde_json::Value, String>,
);

#[derive(Debug, Clone, PartialEq)]
pub struct AppState {
    pub model: Model,
    pub connection: ConnectionState,
    pub core: Option<Info>,
    pub banners: Vec<Banner>,
    /// Set by the reducer when it cannot continue; the app asks the supervisor
    /// for a resync.
    pub resync_wanted: Option<ResyncReason>,
    pub snapshots_applied: u64,
    pub events_applied: u64,
    pub duplicates: u64,
    /// Picker data, loaded on demand by the supervisor.
    pub models: Vec<serde_json::Value>,
    pub agents: Vec<serde_json::Value>,
    pub catalog_source: String,
    /// Core's default model reference, shown wherever a session sets none.
    pub default_model: String,
    pub config: Option<serde_json::Value>,
    pub context: std::collections::BTreeMap<String, serde_json::Value>,
    /// Core's starting context for the newest draft it was asked about: the request and its answer.
    pub draft_context: Option<(serde_json::Value, Result<serde_json::Value, String>)>,
    pub inputs: std::collections::BTreeMap<String, serde_json::Value>,
    pub config_error: Option<String>,
    /// Last worktree listing per session; validated against the current directory.
    pub worktrees: std::collections::BTreeMap<String, serde_json::Value>,
    /// Last worktree listing per project's own checkout (no session yet).
    pub project_worktrees: std::collections::BTreeMap<String, serde_json::Value>,
    /// Directory listings for the Files tree, keyed by relative path (`""` is
    /// the selected root). A failure is kept so an expanded folder can say why.
    pub file_dirs: std::collections::BTreeMap<String, Result<Vec<serde_json::Value>, String>>,
    /// Project/session the tree listings belong to, so a late response for an
    /// old selection is ignored.
    pub file_scope: Option<(String, Option<String>)>,
    pub git: Option<(String, Result<serde_json::Value, String>)>,
    pub file: Option<FileResponse>,
    /// Last diff read: session, path and the `/git/diff` body.
    pub diff: Option<(String, String, Result<serde_json::Value, String>)>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            model: Model::new(),
            connection: ConnectionState::Connecting,
            core: None,
            banners: Vec::new(),
            resync_wanted: None,
            snapshots_applied: 0,
            events_applied: 0,
            duplicates: 0,
            models: Vec::new(),
            agents: Vec::new(),
            catalog_source: String::new(),
            default_model: String::new(),
            config: None,
            context: std::collections::BTreeMap::new(),
            draft_context: None,
            inputs: std::collections::BTreeMap::new(),
            config_error: None,
            worktrees: std::collections::BTreeMap::new(),
            project_worktrees: std::collections::BTreeMap::new(),
            file_dirs: std::collections::BTreeMap::new(),
            file_scope: None,
            git: None,
            diff: None,
            file: None,
        }
    }
}

impl AppState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn apply(&mut self, msg: SyncMsg) {
        match msg {
            SyncMsg::Connection(state) => self.connection = state,
            SyncMsg::Inputs { session, data } => {
                self.inputs.insert(session, data);
            }
            SyncMsg::DraftContext { request, data } => self.draft_context = Some((request, data)),
            SyncMsg::Context { session, data } => {
                self.context.insert(session, data);
            }
            SyncMsg::Config(result) => match result {
                Ok(config) => {
                    self.config = Some(config);
                    self.config_error = None;
                }
                Err(error) => self.config_error = Some(error),
            },
            SyncMsg::Handshake { info, compat } => {
                self.banners
                    .retain(|b| !matches!(b, Banner::NewerCore { .. } | Banner::ApiUnreported));
                match compat {
                    Compat::NewerMinor { core } => self.banners.push(Banner::NewerCore {
                        core: core.to_string(),
                    }),
                    Compat::Unreported => self.banners.push(Banner::ApiUnreported),
                    Compat::Compatible | Compat::Incompatible { .. } => {}
                }
                self.core = Some(info);
            }
            SyncMsg::Snapshot { snapshot, cause } => {
                self.model.apply_snapshot(*snapshot);
                self.snapshots_applied += 1;
                self.resync_wanted = None;
                match cause {
                    ResyncCause::Initial | ResyncCause::Requested => {}
                    ResyncCause::DaemonRestarted => self.banners.push(Banner::DaemonRestarted),
                    ResyncCause::Cursor(c) => self.banners.push(Banner::Resynced(c)),
                    ResyncCause::Overflow => self
                        .banners
                        .push(Banner::Resynced("event stream overflow".into())),
                }
            }
            SyncMsg::History(history) => self.model.apply_history(*history),
            SyncMsg::Turns { session, page } => self.model.apply_turns(&session, *page),
            SyncMsg::Catalog {
                models,
                agents,
                source,
                default_model,
            } => {
                self.models = models;
                self.agents = agents;
                self.catalog_source = source;
                if !default_model.is_empty() {
                    self.default_model = default_model;
                }
            }
            // The app owns completion replies.
            SyncMsg::Completion { .. } => {}
            SyncMsg::Git { session, data } => self.git = Some((session, data)),
            SyncMsg::Diff {
                session,
                path,
                data,
            } => self.diff = Some((session, path, data)),
            SyncMsg::ProjectWorktrees { project, data } => match data {
                Ok(listing) => {
                    self.project_worktrees.insert(project, listing);
                }
                Err(_) => {
                    self.project_worktrees.remove(&project);
                }
            },
            // The TUI owns this view-only inventory cache; it is applied in
            // `App::apply_sync`, alongside the rest of the UI state.
            SyncMsg::SkillMcp { .. } => {}
            SyncMsg::Files {
                project,
                session,
                path,
                data,
                file,
            } => {
                if file {
                    self.file = Some((project, session, path, data));
                } else {
                    self.file_scope = Some((project, session));
                    let entries =
                        data.map(|value| value["entries"].as_array().cloned().unwrap_or_default());
                    self.file_dirs.insert(path, entries);
                }
            }
            SyncMsg::Event(event) => match self.model.apply_event(&event) {
                Applied::Applied => {
                    self.events_applied += 1;
                    if let Some(id) = &event.session_id {
                        match event.kind.as_str() {
                            "worktree.listed" => {
                                self.worktrees.insert(id.clone(), event.data.clone());
                            }
                            "session.directory_changed" | "session.deleted" => {
                                self.worktrees.remove(id);
                            }
                            _ => {}
                        }
                    }
                }
                Applied::Duplicate => self.duplicates += 1,
                Applied::NeedsResync(reason) => self.resync_wanted = Some(reason),
            },
        }
    }

    pub fn dismiss_banners(&mut self) {
        self.banners.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{ApiVersion, Frame, Snapshot};

    fn snapshot() -> Snapshot {
        serde_json::from_str(include_str!("../fixtures/real/snapshot.json")).unwrap()
    }

    #[test]
    fn only_live_is_writable() {
        assert!(!ConnectionState::Live.read_only());
        for s in [
            ConnectionState::Starting,
            ConnectionState::Connecting,
            ConnectionState::Reconnecting {
                attempt: 1,
                retry_in: Duration::from_secs(1),
                reason: "x".into(),
            },
            ConnectionState::Offline { reason: "x".into() },
            ConnectionState::AuthFailed { reason: "x".into() },
            ConnectionState::Incompatible {
                core: "2.0".into(),
                update: UpdateSide::Client,
            },
        ] {
            assert!(s.read_only(), "{}", s.label());
        }
    }

    #[test]
    fn incompatible_detail_names_the_side_to_update() {
        let d = ConnectionState::Incompatible {
            core: "2.0".into(),
            update: UpdateSide::Client,
        }
        .detail();
        assert!(d.contains("update nexus-tui"));
        let d = ConnectionState::Incompatible {
            core: "0.9".into(),
            update: UpdateSide::Core,
        }
        .detail();
        assert!(d.contains("update Nexus Core"));
    }

    #[test]
    fn handshake_banners_replace_each_other() {
        let mut s = AppState::new();
        let info = Info::default();
        s.apply(SyncMsg::Handshake {
            info: info.clone(),
            compat: Compat::Unreported,
        });
        s.apply(SyncMsg::Handshake {
            info: info.clone(),
            compat: Compat::NewerMinor {
                core: ApiVersion { major: 1, minor: 3 },
            },
        });
        assert_eq!(s.banners, [Banner::NewerCore { core: "1.3".into() }]);
        s.apply(SyncMsg::Handshake {
            info,
            compat: Compat::Compatible,
        });
        assert!(s.banners.is_empty());
    }

    #[test]
    fn snapshot_then_events_and_resync_flag() {
        let mut s = AppState::new();
        s.apply(SyncMsg::Event(sample_event(4, "log_c915bfc9b6d826c4")));
        assert!(s.resync_wanted.is_some());
        s.apply(SyncMsg::Snapshot {
            snapshot: Box::new(snapshot()),
            cause: ResyncCause::DaemonRestarted,
        });
        assert!(s.resync_wanted.is_none());
        assert_eq!(s.banners, [Banner::DaemonRestarted]);
        s.apply(SyncMsg::Event(sample_event(4, "log_c915bfc9b6d826c4")));
        s.apply(SyncMsg::Event(sample_event(4, "log_c915bfc9b6d826c4")));
        assert_eq!((s.events_applied, s.duplicates), (1, 1));
    }

    fn sample_event(id: i64, generation: &str) -> crate::api::Event {
        let v = serde_json::json!({"id": id, "type": "project.updated", "session_id": null, "cursor": format!("{generation}:{id}"),
            "data": {"id": "prj_1a17de160d3c070dfda", "name": "x", "revision": 2}});
        match Frame::from_value("project.updated", None, &v).unwrap() {
            Frame::Event(e) => e,
            _ => unreachable!(),
        }
    }
}
