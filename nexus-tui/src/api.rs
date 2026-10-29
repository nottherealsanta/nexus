//! Typed requests and responses for the Core endpoints the TUI needs.
//!
//! Wire shapes follow `docs/api.md` and were checked against a real daemon
//! (`fixtures/real/`). Types are deliberately lenient: unknown fields are kept,
//! optional fields default, and the raw snapshot session rows (which differ
//! from `Session` JSON: no `status`/`children`, `archived` as 0/1,
//! `disabled_tools` as a JSON string) deserialize into the same struct.

use std::collections::BTreeMap;
use std::time::{Duration, SystemTime};

use reqwest::Method;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Map, Value};

use crate::transport::{ApiError, Error, Http, SseFrame};

/// The Core API major/minor this client was written against.
pub const CLIENT_API: ApiVersion = ApiVersion { major: 1, minor: 2 };

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct ApiVersion {
    pub major: u32,
    pub minor: u32,
}

impl std::fmt::Display for ApiVersion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}.{}", self.major, self.minor)
    }
}

impl ApiVersion {
    /// Accepts `"1.2"`, `"1"`, `{"major":1,"minor":2}` or the number `1.2`.
    pub fn from_value(v: &Value) -> Option<Self> {
        match v {
            Value::String(s) => Self::parse(s),
            Value::Number(n) => Self::parse(&n.to_string()),
            Value::Object(o) => Some(Self {
                major: u32::try_from(o.get("major")?.as_u64()?).ok()?,
                minor: o
                    .get("minor")
                    .and_then(Value::as_u64)
                    .and_then(|m| u32::try_from(m).ok())
                    .unwrap_or(0),
            }),
            _ => None,
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        let mut it = s.trim().trim_start_matches('v').split('.');
        let major = it.next()?.parse().ok()?;
        let minor = it.next().map_or(Some(0), |m| m.parse().ok())?;
        Some(Self { major, minor })
    }
}

/// Which side must be updated after a major mismatch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpdateSide {
    Client,
    Core,
}

/// The outcome of the `/api/info` handshake.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Compat {
    /// Same major, same or older minor.
    Compatible,
    /// Same major, Core has a newer minor: carry on, show a banner.
    NewerMinor { core: ApiVersion },
    /// Core does not report an API version yet (Step 12a): carry on, with a note.
    Unreported,
    /// Different major: halt, send nothing further, say who updates.
    Incompatible {
        core: ApiVersion,
        update: UpdateSide,
    },
}

pub fn check_compat(core: Option<ApiVersion>, client: ApiVersion) -> Compat {
    match core {
        None => Compat::Unreported,
        Some(c) if c.major == client.major => {
            if c.minor > client.minor {
                Compat::NewerMinor { core: c }
            } else {
                Compat::Compatible
            }
        }
        Some(c) => Compat::Incompatible {
            core: c,
            update: if c.major > client.major {
                UpdateSide::Client
            } else {
                UpdateSide::Core
            },
        },
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct LogInfo {
    #[serde(default)]
    pub generation: String,
    #[serde(default)]
    pub head: i64,
    #[serde(default)]
    pub head_cursor: String,
    #[serde(default)]
    pub floor: i64,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Info {
    pub name: String,
    pub version: String,
    pub pid: i64,
    pub home: String,
    pub url: String,
    pub log: LogInfo,
    pub api_version: Option<ApiVersion>,
    /// `idempotency.lifetime_seconds`: how long a keyed retry stays safe.
    pub idempotency_lifetime: Option<Duration>,
}

impl Info {
    pub fn from_value(v: &Value) -> Result<Self, Error> {
        let s = |k: &str| {
            v.get(k)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned()
        };
        if v.get("name").and_then(Value::as_str) != Some("nexus") {
            return Err(Error::Decode(
                "/api/info does not look like Nexus Core".into(),
            ));
        }
        Ok(Self {
            name: s("name"),
            version: s("version"),
            pid: v.get("pid").and_then(Value::as_i64).unwrap_or(0),
            home: s("home"),
            url: s("url"),
            log: v
                .get("log")
                .map(|l| serde_json::from_value(l.clone()))
                .transpose()
                .map_err(dec)?
                .unwrap_or_default(),
            api_version: v
                .get("api_version")
                .or_else(|| v.get("api"))
                .and_then(ApiVersion::from_value),
            idempotency_lifetime: v
                .pointer("/idempotency/lifetime_seconds")
                .and_then(Value::as_f64)
                .filter(|s| *s > 0.0)
                .map(Duration::from_secs_f64),
        })
    }
}

fn dec(e: serde_json::Error) -> Error {
    Error::Decode(e.to_string())
}

fn bool_or_int<'de, D: Deserializer<'de>>(d: D) -> Result<bool, D::Error> {
    Ok(match Value::deserialize(d)? {
        Value::Bool(b) => b,
        Value::Number(n) => n.as_i64().unwrap_or(0) != 0,
        _ => false,
    })
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Project {
    pub id: String,
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub created: f64,
    #[serde(default)]
    pub updated: f64,
    #[serde(default)]
    pub revision: i64,
}

/// A session as `Session` JSON or as a raw snapshot row.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Session {
    pub id: String,
    #[serde(default)]
    pub project_id: String,
    #[serde(default)]
    pub directory: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub agent: String,
    #[serde(default)]
    pub parent_id: Option<String>,
    #[serde(default)]
    pub parent_call_id: Option<String>,
    #[serde(default)]
    pub launched_by: String,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub permission: String,
    /// `busy` or `idle`; absent in snapshot rows (derived from running turns).
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default, deserialize_with = "bool_or_int")]
    pub archived: bool,
    #[serde(default)]
    pub revision: i64,
    #[serde(default)]
    pub created: f64,
    #[serde(default)]
    pub updated: f64,
    /// Everything else, unchanged (tools, snapshot, workspace, ...).
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Message {
    pub id: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub seq: i64,
    #[serde(default)]
    pub created: f64,
    pub role: String,
    /// Parts exactly as sent (including `raw` wire items); never reshaped.
    #[serde(default)]
    pub parts: Vec<Value>,
    #[serde(default)]
    pub delivery: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Turn {
    pub id: String,
    pub session_id: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub started: f64,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Approval {
    pub id: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub tool: String,
    #[serde(default)]
    pub call_id: String,
    #[serde(default)]
    pub args: Value,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Question {
    pub id: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub call_id: String,
    #[serde(default)]
    pub created: f64,
    #[serde(default)]
    pub questions: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct Watermark {
    #[serde(default)]
    pub generation: String,
    #[serde(default)]
    pub head: i64,
    #[serde(default)]
    pub floor: i64,
    #[serde(default)]
    pub head_cursor: String,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct Selected {
    pub session_id: String,
    #[serde(default)]
    pub messages: Vec<Message>,
    #[serde(default)]
    pub messages_limit: i64,
    #[serde(default)]
    pub messages_more: bool,
    #[serde(default)]
    pub oldest_seq: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct Snapshot {
    pub watermark: Watermark,
    #[serde(default)]
    pub projects: Vec<Project>,
    #[serde(default)]
    pub sessions: Vec<Session>,
    #[serde(default)]
    pub running: Vec<Turn>,
    #[serde(default)]
    pub approvals: Vec<Approval>,
    #[serde(default)]
    pub questions: Vec<Question>,
    #[serde(default)]
    pub selected: Option<Selected>,
}

/// The full stored history of one session, loaded after the snapshot: every
/// saved message, the summaries of its turns (`GET /api/session/{id}/turn`,
/// every page) and the frozen starting-context snapshot. The event log is not
/// read.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct History {
    pub session_id: String,
    pub messages: Vec<Message>,
    pub starting: Option<Value>,
    /// Oldest to newest. Empty when the request failed (`turns_error`).
    pub turns: Vec<TurnSummary>,
    /// Older turns exist that were not loaded (the page cap was reached).
    pub more_before: bool,
    /// Messages below this seq have no turn summary (pruned turns, or history
    /// from before turns were recorded).
    pub floor: Option<i64>,
    pub usage: UsageLedger,
    /// Why the turn summaries are missing or incomplete; the timeline says so.
    pub turns_error: Option<String>,
}

/// Token counts as Core reports them. A request or attempt without a report
/// has `None` (unknown), never zeros.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
pub struct Usage {
    #[serde(default)]
    pub input: u64,
    #[serde(default)]
    pub cached: u64,
    #[serde(default)]
    pub output: u64,
    #[serde(default)]
    pub reasoning: u64,
}

impl Usage {
    pub fn add(&mut self, other: &Usage) {
        self.input += other.input;
        self.cached += other.cached;
        self.output += other.output;
        self.reasoning += other.reasoning;
    }
}

/// One provider attempt of a request (retries included).
#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct AttemptSummary {
    #[serde(default)]
    pub n: u32,
    #[serde(default)]
    pub status: String,
    /// Seconds waited before this attempt (a retry's backoff).
    #[serde(default)]
    pub delay: Option<f64>,
    #[serde(default)]
    pub started: Option<f64>,
    #[serde(default)]
    pub finished: Option<f64>,
    /// Why this attempt (or the one before it, for a retry) failed.
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub usage: Option<Usage>,
}

/// One provider request (one model step) of a turn.
#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct RequestSummary {
    pub id: String,
    #[serde(default)]
    pub step: u32,
    #[serde(default)]
    pub status: String,
    /// The `provider/model` ref the request ran with.
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub started: Option<f64>,
    #[serde(default)]
    pub finished: Option<f64>,
    #[serde(default)]
    pub stop: Option<String>,
    #[serde(default)]
    pub error: Option<Value>,
    /// The assistant message this request produced.
    #[serde(default)]
    pub message_id: Option<String>,
    #[serde(default)]
    pub usage: Option<Usage>,
    #[serde(default)]
    pub reasoning_seconds: Option<f64>,
    #[serde(default)]
    pub attempts: Vec<AttemptSummary>,
}

/// One tool call of a turn: timing and outcome; output stays in the messages.
#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct ToolSummary {
    pub call_id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub started: Option<f64>,
    #[serde(default)]
    pub finished: Option<f64>,
    #[serde(default)]
    pub is_error: bool,
}

/// What a turn ran, cost and how it ended (`docs/api.md`, turn summaries).
#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct TurnSummary {
    pub id: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub started: f64,
    #[serde(default)]
    pub finished: Option<f64>,
    /// The last message seq before the turn: its messages have greater seqs.
    #[serde(default)]
    pub start_seq: i64,
    #[serde(default)]
    pub steps: u64,
    #[serde(default)]
    pub stop: Option<String>,
    /// `{stop, steps}` once the loop ended the turn, else `None`.
    #[serde(default)]
    pub ended: Option<Value>,
    #[serde(default)]
    pub error: Option<Value>,
    #[serde(default)]
    pub recovery: Option<String>,
    #[serde(default)]
    pub requests: Vec<RequestSummary>,
    #[serde(default)]
    pub tools: Vec<ToolSummary>,
}

/// One model's row of the session spend ledger.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
pub struct LedgerRow {
    #[serde(default)]
    pub requests: u64,
    #[serde(flatten)]
    pub usage: Usage,
}

/// The session's spend per `provider/model` (`unknown` for a request with no
/// recorded model). It covers pruned turns; `since` is when counting began.
#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct UsageLedger {
    #[serde(default)]
    pub since: Option<f64>,
    #[serde(default)]
    pub models: BTreeMap<String, LedgerRow>,
}

impl UsageLedger {
    /// The ledger key of a request's model.
    pub fn key(model: Option<&str>) -> String {
        model
            .filter(|m| !m.is_empty())
            .unwrap_or("unknown")
            .to_owned()
    }

    /// What one turn's requests add to the ledger, per model key.
    pub fn of_turn(turn: &TurnSummary) -> BTreeMap<String, LedgerRow> {
        let mut out: BTreeMap<String, LedgerRow> = BTreeMap::new();
        for r in &turn.requests {
            if let Some(u) = &r.usage {
                let row = out.entry(Self::key(r.model.as_deref())).or_default();
                row.requests += 1;
                row.usage.add(u);
            }
        }
        out
    }

    /// Replace a turn's counted contribution `old` with `new`.
    pub fn replace(
        &mut self,
        old: &BTreeMap<String, LedgerRow>,
        new: &BTreeMap<String, LedgerRow>,
        now: f64,
    ) {
        for (model, row) in old {
            if let Some(have) = self.models.get_mut(model) {
                have.requests = have.requests.saturating_sub(row.requests);
                have.usage.input = have.usage.input.saturating_sub(row.usage.input);
                have.usage.cached = have.usage.cached.saturating_sub(row.usage.cached);
                have.usage.output = have.usage.output.saturating_sub(row.usage.output);
                have.usage.reasoning = have.usage.reasoning.saturating_sub(row.usage.reasoning);
            }
        }
        for (model, row) in new {
            let have = self.models.entry(model.clone()).or_default();
            have.requests += row.requests;
            have.usage.add(&row.usage);
        }
        if !new.is_empty() && self.since.is_none() {
            self.since = Some(now);
        }
    }

    pub fn total(&self) -> Usage {
        let mut u = Usage::default();
        for row in self.models.values() {
            u.add(&row.usage);
        }
        u
    }
}

/// `{"seq": n}`: messages below `seq` have no turn summary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
pub struct Floor {
    pub seq: i64,
}

/// One page of `GET /api/session/{id}/turn`, oldest turn first.
#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
pub struct TurnPage {
    #[serde(default)]
    pub turns: Vec<TurnSummary>,
    #[serde(default)]
    pub more_before: bool,
    #[serde(default)]
    pub more_after: bool,
    #[serde(default)]
    pub floor: Option<Floor>,
    #[serde(default)]
    pub usage: UsageLedger,
}

/// `<generation>:<id>`. The generation may not contain `:`; split at the last one.
pub fn parse_cursor(cursor: &str) -> Option<(&str, i64)> {
    let (gen, id) = cursor.rsplit_once(':')?;
    (!gen.is_empty()).then_some(())?;
    Some((gen, id.parse().ok().filter(|i| *i >= 0)?))
}

pub fn format_cursor(generation: &str, id: i64) -> String {
    format!("{generation}:{id}")
}

/// A durable log event (every SSE frame except `server.connected` and
/// `bus.overflow`).
#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    pub id: i64,
    pub cursor: Option<String>,
    /// The daemon's own counter. Not a cursor; restarts with the daemon.
    pub seq: Option<i64>,
    pub time: f64,
    pub kind: String,
    /// `None` for global events.
    pub session_id: Option<String>,
    pub data: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Connected {
    pub version: String,
    pub generation: String,
    pub head: i64,
    pub head_cursor: String,
    pub floor: i64,
    pub resumed_from: Option<i64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Overflow {
    pub dropped_after: Option<i64>,
    pub generation: Option<String>,
    pub head: Option<i64>,
}

/// One decoded SSE frame.
#[derive(Debug, Clone, PartialEq)]
pub enum Frame {
    Connected(Connected),
    Event(Event),
    /// Stream control: continuity is broken; resume or resync.
    Overflow(Overflow),
}

impl Frame {
    pub fn from_sse(f: &SseFrame) -> Result<Self, Error> {
        let v: Value = serde_json::from_str(&f.data).map_err(dec)?;
        Self::from_value(&f.event, f.id.as_deref(), &v)
    }

    pub fn from_value(name: &str, sse_id: Option<&str>, v: &Value) -> Result<Self, Error> {
        let kind = v.get("type").and_then(Value::as_str).unwrap_or(name);
        let s = |k: &str| v.get(k).and_then(Value::as_str).map(str::to_owned);
        let i = |k: &str| v.get(k).and_then(Value::as_i64);
        match kind {
            "server.connected" => Ok(Frame::Connected(Connected {
                version: s("version").unwrap_or_default(),
                generation: s("generation").unwrap_or_default(),
                head: i("head").unwrap_or(0),
                head_cursor: s("head_cursor").unwrap_or_default(),
                floor: i("floor").unwrap_or(0),
                resumed_from: i("resumed_from"),
            })),
            "bus.overflow" => {
                let d = v.get("data").unwrap_or(v);
                Ok(Frame::Overflow(Overflow {
                    dropped_after: d.get("dropped_after").and_then(Value::as_i64),
                    generation: d
                        .get("generation")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    head: d.get("head").and_then(Value::as_i64),
                }))
            }
            _ => {
                let cursor = s("cursor").or_else(|| sse_id.map(str::to_owned));
                let id = i("id")
                    .or_else(|| cursor.as_deref().and_then(parse_cursor).map(|(_, id)| id))
                    .ok_or_else(|| Error::Decode(format!("event {kind} has no durable id")))?;
                Ok(Frame::Event(Event {
                    id,
                    cursor,
                    seq: i("seq"),
                    time: v.get("time").and_then(Value::as_f64).unwrap_or(0.0),
                    kind: kind.to_owned(),
                    session_id: s("session_id"),
                    data: v.get("data").cloned().unwrap_or(Value::Null),
                }))
            }
        }
    }
}

/// An `Idempotency-Key` for one user action: 1-128 characters from
/// `A-Za-z0-9._:-`. Generate once per action, reuse it for every retry of that
/// action, never for a different one.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RequestId(String);

impl RequestId {
    pub fn new() -> Self {
        Self(uuid::Uuid::new_v4().simple().to_string())
    }
    pub fn parse(s: &str) -> Option<Self> {
        let ok = !s.is_empty()
            && s.len() <= 128
            && s.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '-'));
        ok.then(|| Self(s.to_owned()))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for RequestId {
    fn default() -> Self {
        Self::new()
    }
}

/// A keyed mutation the user started: keep it until it is resolved so a lost
/// response can be retried with the same key, but only inside the server's
/// deduplication lifetime (otherwise the retry might not be deduplicated).
#[derive(Debug, Clone)]
pub struct PendingMutation {
    pub key: RequestId,
    pub issued: SystemTime,
}

impl PendingMutation {
    pub fn new() -> Self {
        Self {
            key: RequestId::new(),
            issued: SystemTime::now(),
        }
    }

    /// `lifetime` is `Info::idempotency_lifetime`. The server counts it from
    /// terminal resolution, so measuring from issue time is conservative for
    /// resolved entries and pessimistic for unresolved ones (which never expire).
    pub fn retry_safe(&self, lifetime: Option<Duration>, now: SystemTime) -> bool {
        match lifetime {
            None => true,
            Some(l) => now.duration_since(self.issued).map_or(true, |age| age < l),
        }
    }
}

impl Default for PendingMutation {
    fn default() -> Self {
        Self::new()
    }
}

/// The result of a mutation that can lose a revision race.
#[derive(Debug, Clone, PartialEq)]
pub enum Outcome<T> {
    Done {
        value: T,
        replayed: bool,
    },
    /// 409 `revision_conflict`: someone else changed it. Re-read the resource
    /// (its `current_revision`), let the user decide, then retry with a new key.
    Conflict {
        current_revision: Option<i64>,
        message: String,
    },
}

impl<T> Outcome<T> {
    pub fn map<U>(self, f: impl FnOnce(T) -> U) -> Outcome<U> {
        match self {
            Outcome::Done { value, replayed } => Outcome::Done {
                value: f(value),
                replayed,
            },
            Outcome::Conflict {
                current_revision,
                message,
            } => Outcome::Conflict {
                current_revision,
                message,
            },
        }
    }
}

/// The endpoints the TUI uses, over an authenticated `Http`.
#[derive(Clone)]
pub struct CoreApi {
    pub http: Http,
}

impl CoreApi {
    pub fn new(http: Http) -> Self {
        Self { http }
    }

    pub async fn info(&self) -> Result<Info, Error> {
        Info::from_value(&self.http.get("/api/info", &[]).await?)
    }

    pub async fn snapshot(
        &self,
        session: Option<&str>,
        message_limit: Option<u32>,
    ) -> Result<Snapshot, Error> {
        let mut q = Vec::new();
        if let Some(s) = session {
            q.push(("session", s.to_owned()));
        }
        if let Some(n) = message_limit {
            q.push(("message_limit", n.to_string()));
        }
        serde_json::from_value(self.http.get("/api/snapshot", &q).await?).map_err(dec)
    }

    pub async fn projects(&self) -> Result<Vec<Project>, Error> {
        serde_json::from_value(self.http.get("/api/project", &[]).await?).map_err(dec)
    }

    pub async fn sessions(&self, archived: &str) -> Result<Vec<Session>, Error> {
        serde_json::from_value(
            self.http
                .get("/api/session", &[("archived", archived.to_owned())])
                .await?,
        )
        .map_err(dec)
    }

    pub async fn session(&self, id: &str) -> Result<Session, Error> {
        serde_json::from_value(self.http.get(&format!("/api/session/{id}"), &[]).await?)
            .map_err(dec)
    }

    /// A mutating call. A stale revision becomes `Outcome::Conflict`; every
    /// other HTTP error stays an `Err`.
    async fn mutate(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
        key: &RequestId,
        expected_revision: Option<i64>,
    ) -> Result<Outcome<Value>, Error> {
        match self
            .http
            .call(
                method,
                path,
                &[],
                body.as_ref(),
                Some(key.as_str()),
                expected_revision,
            )
            .await
        {
            Ok(r) => Ok(Outcome::Done {
                value: r.body,
                replayed: r.replayed,
            }),
            Err(Error::Api(e)) => match e.revision_conflict() {
                Some(current_revision) => Ok(Outcome::Conflict {
                    current_revision,
                    message: e.message,
                }),
                None => Err(Error::Api(e)),
            },
            Err(e) => Err(e),
        }
    }

    pub async fn create_project(
        &self,
        path: &str,
        name: Option<&str>,
        key: &RequestId,
    ) -> Result<Outcome<Project>, Error> {
        let mut body = json!({ "path": path });
        if let Some(n) = name {
            body["name"] = json!(n);
        }
        self.mutate(Method::POST, "/api/project", Some(body), key, None)
            .await?
            .try_decode()
    }

    pub async fn create_session(
        &self,
        project: &str,
        fields: Value,
        key: &RequestId,
    ) -> Result<Outcome<Session>, Error> {
        let mut body = fields;
        body["project"] = json!(project);
        self.mutate(Method::POST, "/api/session", Some(body), key, None)
            .await?
            .try_decode()
    }

    /// Update session fields (`title`, `model`, ...); `expected_revision` makes
    /// a stale write a `Conflict` instead of a silent overwrite.
    pub async fn patch_session(
        &self,
        id: &str,
        fields: Value,
        expected_revision: Option<i64>,
        key: &RequestId,
    ) -> Result<Outcome<Session>, Error> {
        self.mutate(
            Method::PATCH,
            &format!("/api/session/{id}"),
            Some(fields),
            key,
            expected_revision,
        )
        .await?
        .try_decode()
    }

    pub async fn rename_project(
        &self,
        id: &str,
        name: &str,
        expected_revision: Option<i64>,
        key: &RequestId,
    ) -> Result<Outcome<Project>, Error> {
        self.mutate(
            Method::PATCH,
            &format!("/api/project/{id}"),
            Some(json!({ "name": name })),
            key,
            expected_revision,
        )
        .await?
        .try_decode()
    }

    /// Delete a project and its sessions (`DELETE /api/project/{id}`).
    pub async fn delete_project(&self, id: &str, key: &RequestId) -> Result<Outcome<()>, Error> {
        Ok(self
            .mutate(
                Method::DELETE,
                &format!("/api/project/{id}"),
                None,
                key,
                None,
            )
            .await?
            .map(|_| ()))
    }

    pub async fn delete_session(&self, id: &str, key: &RequestId) -> Result<Outcome<()>, Error> {
        Ok(self
            .mutate(
                Method::DELETE,
                &format!("/api/session/{id}"),
                None,
                key,
                None,
            )
            .await?
            .map(|_| ()))
    }

    pub async fn set_archived(
        &self,
        id: &str,
        archived: bool,
        key: &RequestId,
    ) -> Result<Outcome<Session>, Error> {
        let verb = if archived { "archive" } else { "unarchive" };
        self.mutate(
            Method::POST,
            &format!("/api/session/{id}/{verb}"),
            Some(json!({})),
            key,
            None,
        )
        .await?
        .try_decode()
    }

    /// Start a turn. 409 (busy) is an `Err`; the key is not consumed by it.
    pub async fn prompt(
        &self,
        id: &str,
        text: &str,
        key: &RequestId,
    ) -> Result<Outcome<Value>, Error> {
        self.mutate(
            Method::POST,
            &format!("/api/session/{id}/prompt"),
            Some(json!({ "text": text })),
            key,
            None,
        )
        .await
    }

    pub async fn prompt_images(
        &self,
        id: &str,
        text: &str,
        images: &[crate::images::Image],
        mode: &str,
        key: &RequestId,
    ) -> Result<Outcome<Value>, Error> {
        self.mutate(
            Method::POST,
            &format!("/api/session/{id}/prompt"),
            Some(json!({ "text": text, "images": images, "mode": mode })),
            key,
            None,
        )
        .await
    }

    pub async fn continue_turn(&self, id: &str, key: &RequestId) -> Result<Outcome<Value>, Error> {
        self.mutate(
            Method::POST,
            &format!("/api/session/{id}/continue"),
            Some(json!({})),
            key,
            None,
        )
        .await
    }

    pub async fn interrupt(&self, id: &str, key: &RequestId) -> Result<Outcome<()>, Error> {
        Ok(self
            .mutate(
                Method::POST,
                &format!("/api/session/{id}/interrupt"),
                Some(json!({})),
                key,
                None,
            )
            .await?
            .map(|_| ()))
    }

    pub async fn reply_permission(
        &self,
        session: &str,
        id: &str,
        allow: bool,
        key: &RequestId,
    ) -> Result<Outcome<()>, Error> {
        let reply = if allow { "allow" } else { "deny" };
        Ok(self
            .mutate(
                Method::POST,
                &format!("/api/session/{session}/permission/{id}"),
                Some(json!({ "reply": reply })),
                key,
                None,
            )
            .await?
            .map(|_| ()))
    }

    pub async fn reply_question(
        &self,
        session: &str,
        id: &str,
        answers: Value,
        key: &RequestId,
    ) -> Result<Outcome<()>, Error> {
        Ok(self
            .mutate(
                Method::POST,
                &format!("/api/session/{session}/question/{id}"),
                Some(json!({ "answers": answers })),
                key,
                None,
            )
            .await?
            .map(|_| ()))
    }

    /// One page of turn summaries: the newest `limit`, or the page after or
    /// before a known turn id (`after` and `before` are exclusive).
    pub async fn turns(
        &self,
        session: &str,
        after: Option<&str>,
        before: Option<&str>,
        limit: Option<u32>,
    ) -> Result<TurnPage, Error> {
        let mut q = Vec::new();
        if let Some(a) = after {
            q.push(("after", a.to_owned()));
        }
        if let Some(b) = before {
            q.push(("before", b.to_owned()));
        }
        if let Some(n) = limit {
            q.push(("limit", n.to_string()));
        }
        serde_json::from_value(
            self.http
                .get(&format!("/api/session/{session}/turn"), &q)
                .await?,
        )
        .map_err(dec)
    }

    /// Move the session to an existing directory. Returns the session and the
    /// change record (which may say it is pending until the next turn boundary).
    pub async fn change_directory(
        &self,
        session: &str,
        directory: &str,
        key: &RequestId,
    ) -> Result<Outcome<Value>, Error> {
        self.mutate(
            Method::POST,
            &format!("/api/session/{session}/directory"),
            Some(json!({ "directory": directory })),
            key,
            None,
        )
        .await
    }

    /// A session export. `format` is `markdown` or `json`; `scope` is
    /// `session` or `tree`. Returns the document and Core's filename.
    pub async fn export(
        &self,
        session: &str,
        format: &str,
        scope: &str,
    ) -> Result<(String, Option<String>), Error> {
        self.http
            .get_text(
                &format!("/api/session/{session}/export"),
                &[("format", format.to_owned()), ("scope", scope.to_owned())],
            )
            .await
    }

    /// List one workspace directory level.
    pub async fn files(
        &self,
        project: &str,
        session: Option<&str>,
        path: &str,
    ) -> Result<Value, Error> {
        let mut query = vec![("path", path.to_owned())];
        if let Some(session) = session {
            query.push(("session", session.to_owned()));
        }
        self.http
            .get(&format!("/api/project/{project}/files"), &query)
            .await
    }

    /// Workspace path candidates for an `@` completion token (files and
    /// directories; directories end with `/`).
    pub async fn files_search(
        &self,
        project: &str,
        session: Option<&str>,
        query: &str,
        limit: u32,
    ) -> Result<Value, Error> {
        let mut params = vec![
            ("q", query.to_owned()),
            ("limit", limit.to_string()),
            ("directories", "1".to_owned()),
        ];
        if let Some(session) = session {
            params.push(("session", session.to_owned()));
        }
        self.http
            .get(&format!("/api/project/{project}/files/search"), &params)
            .await
    }

    /// Read a UTF-8 workspace file.
    pub async fn file(
        &self,
        project: &str,
        session: Option<&str>,
        path: &str,
    ) -> Result<Value, Error> {
        let mut query = vec![("path", path.to_owned())];
        if let Some(session) = session {
            query.push(("session", session.to_owned()));
        }
        self.http
            .get(&format!("/api/project/{project}/file"), &query)
            .await
    }

    pub async fn git_status(&self, session: &str) -> Result<Value, Error> {
        self.http
            .get(&format!("/api/session/{session}/git"), &[])
            .await
    }

    /// Staged and unstaged diffs of one changed path.
    pub async fn git_diff(&self, session: &str, path: &str) -> Result<Value, Error> {
        self.http
            .get(
                &format!("/api/session/{session}/git/diff"),
                &[("path", path.to_owned())],
            )
            .await
    }

    /// List worktrees for a session's directory. Outside Git Core returns an error.
    pub async fn worktrees(&self, session: &str) -> Result<Value, Error> {
        self.http
            .get(&format!("/api/session/{session}/worktrees"), &[])
            .await
    }

    /// Worktrees of a project's own checkout, before a session exists.
    pub async fn project_worktrees(&self, project: &str) -> Result<Value, Error> {
        self.http
            .get(&format!("/api/project/{project}/worktrees"), &[])
            .await
    }

    /// The connected providers' model catalog (`{source, models}`).
    pub async fn models(&self) -> Result<Value, Error> {
        self.http.get("/api/model", &[]).await
    }

    /// Core's default model reference (`{model}`), used when a session sets none.
    pub async fn default_model(&self) -> Result<Value, Error> {
        self.http.get("/api/model/default", &[]).await
    }

    /// Agents available to a project (`{agents, problems}`).
    pub async fn agents(&self, project: Option<&str>) -> Result<Value, Error> {
        let query: Vec<(&str, String)> = project
            .map(|p| vec![("project", p.to_owned())])
            .unwrap_or_default();
        self.http.get("/api/agent", &query).await
    }

    /// One agent with its `prompt` (the system prompt body).
    pub async fn agent(&self, name: &str, project: Option<&str>) -> Result<Value, Error> {
        let query: Vec<(&str, String)> = project
            .map(|p| vec![("project", p.to_owned())])
            .unwrap_or_default();
        self.http.get(&format!("/api/agent/{name}"), &query).await
    }

    pub async fn set_model_rev(
        &self,
        session: &str,
        model: &str,
        revision: Option<i64>,
        key: &RequestId,
    ) -> Result<Outcome<Value>, Error> {
        self.mutate(
            Method::POST,
            &format!("/api/session/{session}/model"),
            Some(json!({ "model": model })),
            key,
            revision,
        )
        .await
    }

    pub async fn set_agent_rev(
        &self,
        session: &str,
        agent: &str,
        revision: Option<i64>,
        key: &RequestId,
    ) -> Result<Outcome<Value>, Error> {
        self.mutate(
            Method::POST,
            &format!("/api/session/{session}/agent"),
            Some(json!({ "agent": agent })),
            key,
            revision,
        )
        .await
    }

    /// The full frozen starting context (system prompt, tools, instructions).
    pub async fn snapshot_json(&self, session: &str) -> Result<Value, Error> {
        self.http
            .get(&format!("/api/session/{session}/snapshot"), &[])
            .await
    }

    pub async fn messages(&self, session: &str) -> Result<Vec<Message>, Error> {
        serde_json::from_value(
            self.http
                .get(&format!("/api/session/{session}/message"), &[])
                .await?,
        )
        .map_err(dec)
    }
}

impl Outcome<Value> {
    fn try_decode<T: serde::de::DeserializeOwned>(self) -> Result<Outcome<T>, Error> {
        match self {
            Outcome::Done { value, replayed } => Ok(Outcome::Done {
                value: serde_json::from_value(value).map_err(dec)?,
                replayed,
            }),
            Outcome::Conflict {
                current_revision,
                message,
            } => Ok(Outcome::Conflict {
                current_revision,
                message,
            }),
        }
    }
}

/// Group sessions by id for the model; helper kept here because both rows and
/// events produce `Session`.
pub fn index_sessions(sessions: Vec<Session>) -> BTreeMap<String, Session> {
    sessions.into_iter().map(|s| (s.id.clone(), s)).collect()
}

/// A cursor failure carries the head to resume from.
pub fn resync_head(e: &ApiError) -> Option<String> {
    e.body
        .get("head_cursor")
        .and_then(Value::as_str)
        .map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;

    const INFO: &str = include_str!("../fixtures/real/info.json");
    const SNAPSHOT: &str = include_str!("../fixtures/real/snapshot.json");
    const REPLAY: &str = include_str!("../fixtures/real/replay.sse");

    #[test]
    fn real_info_decodes() {
        let info = Info::from_value(&serde_json::from_str(INFO).unwrap()).unwrap();
        assert_eq!(info.version, "0.10.0.dev0");
        assert!(info.log.generation.starts_with("log_"));
        assert_eq!(
            info.idempotency_lifetime,
            Some(Duration::from_secs(604_800))
        );
        assert_eq!(info.api_version, None); // not reported yet by Core
    }

    #[test]
    fn info_rejects_other_servers() {
        assert!(Info::from_value(&json!({"name": "something-else"})).is_err());
    }

    #[test]
    fn real_snapshot_rows_decode() {
        let s: Snapshot = serde_json::from_str(SNAPSHOT).unwrap();
        assert_eq!(s.watermark.head, 3);
        assert_eq!(s.projects[0].name, "x");
        let row = &s.sessions[0];
        assert_eq!(row.status, None);
        assert!(!row.archived);
        assert_eq!(row.revision, 1);
        assert!(s.selected.is_none());
    }

    #[test]
    fn archived_accepts_bool_or_int() {
        let a: Session = serde_json::from_value(json!({"id": "s", "archived": 1})).unwrap();
        let b: Session =
            serde_json::from_value(json!({"id": "s", "archived": true, "status": "busy"})).unwrap();
        assert!(a.archived && b.archived);
        assert_eq!(b.status.as_deref(), Some("busy"));
    }

    #[test]
    fn real_replay_decodes_frames() {
        let mut p = crate::transport::SseParser::new();
        let frames: Vec<Frame> = p
            .feed(REPLAY.as_bytes())
            .iter()
            .map(|f| Frame::from_sse(f).unwrap())
            .collect();
        assert!(
            matches!(&frames[0], Frame::Connected(c) if c.resumed_from == Some(0) && c.head >= 2)
        );
        let Frame::Event(e) = &frames[1] else {
            panic!()
        };
        assert_eq!(
            (e.id, e.kind.as_str(), e.session_id.clone()),
            (1, "project.created", None)
        );
        assert_eq!(parse_cursor(e.cursor.as_deref().unwrap()).unwrap().1, 1);
        let Frame::Event(e) = &frames[2] else {
            panic!()
        };
        assert_eq!(e.session_id.as_deref(), e.data["id"].as_str());
    }

    #[test]
    fn overflow_frame_has_no_id() {
        let v = json!({"type": "bus.overflow", "id": null, "session_id": null,
                       "data": {"dropped_after": 4210, "generation": "log_a", "head": 4300}});
        let Frame::Overflow(o) = Frame::from_value("bus.overflow", None, &v).unwrap() else {
            panic!()
        };
        assert_eq!((o.dropped_after, o.head), (Some(4210), Some(4300)));
    }

    #[test]
    fn event_without_durable_id_is_an_error() {
        assert!(Frame::from_value("text_delta", None, &json!({"type": "text_delta"})).is_err());
        let ok = Frame::from_value("x", Some("g:9"), &json!({"type": "x"})).unwrap();
        assert!(matches!(ok, Frame::Event(e) if e.id == 9));
    }

    #[test]
    fn cursors() {
        assert_eq!(parse_cursor("log_ab:12"), Some(("log_ab", 12)));
        assert_eq!(parse_cursor("log_ab"), None);
        assert_eq!(parse_cursor(":3"), None);
        assert_eq!(parse_cursor("g:-1"), None);
        assert_eq!(format_cursor("g", 5), "g:5");
    }

    #[test]
    fn version_handshake() {
        let c = ApiVersion { major: 1, minor: 2 };
        assert_eq!(
            check_compat(Some(ApiVersion { major: 1, minor: 2 }), c),
            Compat::Compatible
        );
        assert_eq!(
            check_compat(Some(ApiVersion { major: 1, minor: 0 }), c),
            Compat::Compatible
        );
        assert_eq!(
            check_compat(Some(ApiVersion { major: 1, minor: 5 }), c),
            Compat::NewerMinor {
                core: ApiVersion { major: 1, minor: 5 }
            }
        );
        assert_eq!(
            check_compat(Some(ApiVersion { major: 2, minor: 0 }), c),
            Compat::Incompatible {
                core: ApiVersion { major: 2, minor: 0 },
                update: UpdateSide::Client
            }
        );
        assert_eq!(
            check_compat(Some(ApiVersion { major: 0, minor: 9 }), c),
            Compat::Incompatible {
                core: ApiVersion { major: 0, minor: 9 },
                update: UpdateSide::Core
            }
        );
        assert_eq!(check_compat(None, c), Compat::Unreported);
    }

    #[test]
    fn version_parsing() {
        assert_eq!(
            ApiVersion::parse("1.3"),
            Some(ApiVersion { major: 1, minor: 3 })
        );
        assert_eq!(
            ApiVersion::parse("2"),
            Some(ApiVersion { major: 2, minor: 0 })
        );
        assert_eq!(
            ApiVersion::from_value(&json!({"major": 1, "minor": 4})),
            Some(ApiVersion { major: 1, minor: 4 })
        );
        assert_eq!(ApiVersion::from_value(&json!("x")), None);
    }

    #[test]
    fn request_ids_follow_the_documented_syntax() {
        assert!(RequestId::parse(&RequestId::new().0).is_some());
        assert!(RequestId::parse("").is_none());
        assert!(RequestId::parse(&"a".repeat(129)).is_none());
        assert!(RequestId::parse("bad key").is_none());
        assert!(RequestId::parse("a.b_c:d-e").is_some());
    }

    #[test]
    fn retry_safety_window() {
        let m = PendingMutation::new();
        let day = Duration::from_secs(86_400);
        assert!(m.retry_safe(Some(7 * day), m.issued + day));
        assert!(!m.retry_safe(Some(7 * day), m.issued + 8 * day));
        assert!(m.retry_safe(None, m.issued + 99 * day));
    }
}
