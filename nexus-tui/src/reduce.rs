//! Event reduction: the client model and how snapshots and events change it.
//!
//! Pure and synchronous. The model is a cache of what Core says; it never
//! invents state. Rules (Step 11 contracts):
//!
//! * A snapshot replaces everything and sets the watermark. Events at or below
//!   `last_id` are duplicates and are dropped (dedup by durable id).
//! * Ids may jump: a filtered stream, or unrelated events, skip numbers. A gap is
//!   never a reason to resync. `seq` is never used for ordering or dedup.
//! * An event from another log generation, or any event before the first
//!   snapshot, cannot be applied: the reducer asks for a resync.
//! * Every event of the selected session is kept in `SelectedView::events`
//!   (wire events are never dropped by the client).

use std::collections::BTreeMap;

use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::api::{parse_cursor, Approval, Event, Message, Project, Question, Session, Snapshot};

/// Why the reducer cannot continue from the current state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResyncReason {
    NoSnapshot,
    GenerationChanged {
        have: String,
        got: String,
    },
    /// Stream control: `bus.overflow`.
    Overflow,
    /// The server rejected the cursor (`cursor_generation`, `history_unavailable`, ...).
    Cursor(String),
}

impl std::fmt::Display for ResyncReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoSnapshot => write!(f, "no snapshot yet"),
            Self::GenerationChanged { have, got } => {
                write!(f, "event log changed ({have} -> {got})")
            }
            Self::Overflow => write!(f, "event stream overflowed"),
            Self::Cursor(code) => write!(f, "cursor rejected ({code})"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Applied {
    /// The event changed the model (or was recorded in the selected view).
    Applied,
    /// Already applied (id at or below the watermark/last applied id).
    Duplicate,
    NeedsResync(ResyncReason),
}

/// Text being streamed for a session's running step; cleared when the message
/// it becomes is added, at a new step, or when the session goes idle.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Streaming {
    pub text: String,
    pub reasoning: String,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct SelectedView {
    pub session_id: String,
    pub messages: Vec<Message>,
    pub messages_more: bool,
    /// Every event of this session since the snapshot, in id order, unfiltered.
    pub events: Vec<Event>,
    /// `GET /api/session/{id}/snapshot`: the frozen starting context. `None`
    /// until loaded (or when Core has no snapshot for the session yet).
    pub starting: Option<Value>,
    /// Why the loaded diagnostics are incomplete; the timeline announces it.
    /// Messages are complete either way.
    pub events_gap: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Model {
    pub generation: Option<String>,
    /// Highest durable event id applied (the snapshot head after a resync).
    pub last_id: i64,
    pub projects: BTreeMap<String, Project>,
    pub sessions: BTreeMap<String, Session>,
    /// session id -> running turn id.
    pub running: BTreeMap<String, String>,
    pub approvals: BTreeMap<String, Approval>,
    pub questions: BTreeMap<String, Question>,
    pub streaming: BTreeMap<String, Streaming>,
    pub selected: Option<SelectedView>,
    /// Set when the selected session was deleted by an event; the UI should
    /// say so and move on. Cleared by `select`.
    pub selected_deleted: Option<String>,
}

impl Model {
    pub fn new() -> Self {
        Self::default()
    }

    /// The cursor to resume from: `<generation>:<last applied id>`.
    pub fn cursor(&self) -> Option<String> {
        self.generation
            .as_ref()
            .map(|g| crate::api::format_cursor(g, self.last_id))
    }

    pub fn select(&mut self, session_id: Option<&str>) {
        self.selected_deleted = None;
        self.selected = session_id.map(|id| SelectedView {
            session_id: id.to_owned(),
            ..Default::default()
        });
    }

    /// Sessions, most recently updated first.
    pub fn sessions_recent(&self) -> Vec<&Session> {
        let mut v: Vec<&Session> = self.sessions.values().collect();
        v.sort_by(|a, b| {
            b.updated
                .total_cmp(&a.updated)
                .then_with(|| a.id.cmp(&b.id))
        });
        v
    }

    /// Replace all state with a consistent snapshot; events after
    /// `watermark.head` follow.
    pub fn apply_snapshot(&mut self, s: Snapshot) {
        let wanted = self.selected.as_ref().map(|v| v.session_id.clone());
        *self = Model {
            generation: Some(s.watermark.generation.clone()),
            last_id: s.watermark.head,
            projects: s.projects.into_iter().map(|p| (p.id.clone(), p)).collect(),
            ..Default::default()
        };
        for turn in s.running {
            self.running.insert(turn.session_id.clone(), turn.id);
        }
        for mut session in s.sessions {
            if session.status.is_none() {
                let busy = self.running.contains_key(&session.id);
                session.status = Some(if busy { "busy" } else { "idle" }.into());
            }
            self.sessions.insert(session.id.clone(), session);
        }
        // Pending waits come only from the snapshot: stale ones are discarded.
        self.approvals = s.approvals.into_iter().map(|a| (a.id.clone(), a)).collect();
        self.questions = s.questions.into_iter().map(|q| (q.id.clone(), q)).collect();
        self.selected = match (s.selected, wanted) {
            (Some(sel), _) => Some(SelectedView {
                session_id: sel.session_id,
                messages: sel.messages,
                messages_more: sel.messages_more,
                events: Vec::new(),
                ..Default::default()
            }),
            (None, Some(id)) if self.sessions.contains_key(&id) => Some(SelectedView {
                session_id: id,
                ..Default::default()
            }),
            _ => None,
        };
    }

    /// Fold a session's fully loaded history into the selected view.
    ///
    /// The snapshot carries only the newest bounded messages; history replaces
    /// them with every stored message. Messages and diagnostics already applied
    /// from the live stream are kept, and duplicates are dropped by id.
    pub fn apply_history(&mut self, h: crate::api::History) {
        let Some(view) = self.selected.as_mut() else {
            return;
        };
        if view.session_id != h.session_id {
            return;
        }
        // Messages applied live after the fetch are kept, ordered by seq.
        let mut messages = h.messages;
        for m in &view.messages {
            if !messages.iter().any(|x| x.id == m.id) {
                let at = messages.partition_point(|x| x.seq <= m.seq);
                messages.insert(at, m.clone());
            }
        }
        view.messages = messages;
        view.messages_more = false;
        let newest = h.events.last().map(|e| e.id).unwrap_or(0);
        let mut events = h.events;
        events.extend(view.events.iter().filter(|e| e.id > newest).cloned());
        events.sort_by_key(|e| e.id);
        events.dedup_by_key(|e| e.id);
        view.events = events;
        view.events_gap = h.events_gap;
        view.starting = h.starting;
    }

    pub fn apply_event(&mut self, event: &Event) -> Applied {
        let Some(have) = self.generation.clone() else {
            return Applied::NeedsResync(ResyncReason::NoSnapshot);
        };
        if let Some((got, _)) = event.cursor.as_deref().and_then(parse_cursor) {
            if got != have {
                return Applied::NeedsResync(ResyncReason::GenerationChanged {
                    have,
                    got: got.to_owned(),
                });
            }
        }
        if event.id <= self.last_id {
            return Applied::Duplicate;
        }
        self.last_id = event.id;
        self.reduce(event);
        if let (Some(view), Some(sid)) = (self.selected.as_mut(), event.session_id.as_deref()) {
            if view.session_id == sid {
                view.events.push(event.clone());
            }
        }
        Applied::Applied
    }

    fn reduce(&mut self, e: &Event) {
        let sid = e.session_id.clone();
        match e.kind.as_str() {
            "project.created" | "project.updated" => {
                if let Some(p) = decode::<Project>(&e.data) {
                    if self
                        .projects
                        .get(&p.id)
                        .is_none_or(|old| p.revision >= old.revision)
                    {
                        self.projects.insert(p.id.clone(), p);
                    }
                }
            }
            "project.deleted" => {
                if let Some(id) = str_field(&e.data, "id") {
                    self.projects.remove(&id);
                    let orphans: Vec<String> = self
                        .sessions
                        .values()
                        .filter(|s| s.project_id == id)
                        .map(|s| s.id.clone())
                        .collect();
                    for sid in orphans {
                        self.drop_session(&sid);
                    }
                }
            }
            "session.created" | "session.updated" => {
                if let Some(mut s) = decode::<Session>(&e.data) {
                    let old = self.sessions.get(&s.id);
                    if old.is_some_and(|o| s.revision < o.revision) {
                        return; // a stale copy of a newer session
                    }
                    if s.status.is_none() {
                        s.status = old
                            .and_then(|o| o.status.clone())
                            .or_else(|| Some("idle".into()));
                    }
                    self.sessions.insert(s.id.clone(), s);
                }
            }
            "session.deleted" => {
                if let Some(id) = str_field(&e.data, "id").or(sid) {
                    self.drop_session(&id);
                }
            }
            "session.archived" | "session.unarchived" => {
                let archived = e.kind == "session.archived";
                if let Some(ids) = e.data.get("sessions").and_then(Value::as_array) {
                    for id in ids.iter().filter_map(Value::as_str) {
                        if let Some(s) = self.sessions.get_mut(id) {
                            s.archived = archived;
                        }
                    }
                }
            }
            "session.status" => {
                let Some(sid) = sid else { return };
                let status = str_field(&e.data, "status").unwrap_or_default();
                if let Some(s) = self.sessions.get_mut(&sid) {
                    s.status = Some(status.clone());
                }
                if status == "busy" {
                    if let Some(turn) = str_field(&e.data, "turn_id") {
                        self.running.insert(sid, turn);
                    }
                } else {
                    self.running.remove(&sid);
                    self.streaming.remove(&sid);
                }
            }
            "turn.recovered" => {
                if let Some(sid) = sid {
                    self.running.remove(&sid);
                    self.streaming.remove(&sid);
                }
            }
            "message.added" => {
                let Some(m) = decode::<Message>(&e.data) else {
                    return;
                };
                let sid = if m.session_id.is_empty() {
                    sid.unwrap_or_default()
                } else {
                    m.session_id.clone()
                };
                if m.role == "assistant" {
                    self.streaming.remove(&sid);
                }
                if let Some(view) = self.selected.as_mut().filter(|v| v.session_id == sid) {
                    if !view.messages.iter().any(|x| x.id == m.id) {
                        let at = view.messages.partition_point(|x| x.seq <= m.seq);
                        view.messages.insert(at, m);
                    }
                }
            }
            "step_start" => {
                if let Some(sid) = sid {
                    self.streaming.remove(&sid);
                }
            }
            "text_delta" | "reasoning_delta" => {
                if let (Some(sid), Some(text)) = (sid, e.data.get("text").and_then(Value::as_str)) {
                    let s = self.streaming.entry(sid).or_default();
                    if e.kind == "text_delta" {
                        &mut s.text
                    } else {
                        &mut s.reasoning
                    }
                    .push_str(text);
                }
            }
            "permission.asked" => {
                if let Some(a) = decode::<Approval>(&e.data) {
                    self.approvals.insert(a.id.clone(), a);
                }
            }
            "permission.replied" => {
                if let Some(id) = str_field(&e.data, "id") {
                    self.approvals.remove(&id);
                }
            }
            "question.asked" => {
                if let Some(q) = decode::<Question>(&e.data) {
                    self.questions.insert(q.id.clone(), q);
                }
            }
            "question.replied" => {
                if let Some(id) = str_field(&e.data, "id") {
                    self.questions.remove(&id);
                }
            }
            _ => {} // wire/tool/diagnostic events only land in the selected view
        }
    }

    fn drop_session(&mut self, id: &str) {
        self.sessions.remove(id);
        self.running.remove(id);
        self.streaming.remove(id);
        self.approvals.retain(|_, a| a.session_id != id);
        self.questions.retain(|_, q| q.session_id != id);
        if self.selected.as_ref().is_some_and(|v| v.session_id == id) {
            self.selected = None;
            self.selected_deleted = Some(id.to_owned());
        }
    }
}

fn decode<T: DeserializeOwned>(v: &Value) -> Option<T> {
    serde_json::from_value(v.clone()).ok()
}

fn str_field(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::Frame;
    use crate::transport::SseParser;

    const SNAPSHOT: &str = include_str!("../fixtures/real/snapshot.json");
    const TURN: &str = include_str!("../fixtures/documented/turn.sse");
    const LIFECYCLE: &str = include_str!("../fixtures/documented/lifecycle.sse");
    const SESSION: &str = "ses_1a17de160eccc428a38";

    fn frames(sse: &str) -> Vec<Frame> {
        SseParser::new()
            .feed(sse.as_bytes())
            .iter()
            .map(|f| Frame::from_sse(f).unwrap())
            .collect()
    }

    fn events(sse: &str) -> Vec<Event> {
        frames(sse)
            .into_iter()
            .filter_map(|f| {
                if let Frame::Event(e) = f {
                    Some(e)
                } else {
                    None
                }
            })
            .collect()
    }

    fn model() -> Model {
        let mut m = Model::new();
        m.select(Some(SESSION));
        m.apply_snapshot(serde_json::from_str(SNAPSHOT).unwrap());
        m
    }

    #[test]
    fn snapshot_sets_watermark_and_derives_status() {
        let m = model();
        assert_eq!(m.last_id, 3);
        assert_eq!(m.cursor().unwrap(), "log_c915bfc9b6d826c4:3");
        assert_eq!(m.sessions[SESSION].status.as_deref(), Some("idle"));
        assert_eq!(m.selected.as_ref().unwrap().session_id, SESSION);
    }

    #[test]
    fn events_before_a_snapshot_need_one() {
        let e = &events(TURN)[0];
        assert_eq!(
            Model::new().apply_event(e),
            Applied::NeedsResync(ResyncReason::NoSnapshot)
        );
    }

    #[test]
    fn a_full_turn_reduces_and_tolerates_id_gaps() {
        let mut m = model();
        let evs = events(TURN);
        let mut seen_busy = false;
        for e in &evs {
            assert_eq!(m.apply_event(e), Applied::Applied, "event {}", e.id);
            if e.kind == "session.status" && e.id == 4 {
                seen_busy = true;
                assert_eq!(m.sessions[SESSION].status.as_deref(), Some("busy"));
                assert_eq!(m.running[SESSION], "trn_1");
            }
            if e.id == 9 {
                assert_eq!(m.streaming[SESSION].text, "Hello");
            }
            if e.id == 11 {
                assert_eq!((m.approvals.len(), m.questions.len()), (1, 1));
            }
        }
        assert!(seen_busy);
        assert_eq!(m.last_id, 16);
        assert!(m.running.is_empty() && m.streaming.is_empty());
        assert!(m.approvals.is_empty() && m.questions.is_empty());
        assert_eq!(m.sessions[SESSION].status.as_deref(), Some("idle"));
        let view = m.selected.as_ref().unwrap();
        assert_eq!(
            view.messages
                .iter()
                .map(|m| m.role.as_str())
                .collect::<Vec<_>>(),
            ["user", "assistant"]
        );
        // Wire/loop events are retained, none dropped.
        assert_eq!(view.events.len(), evs.len());
        assert!(view.events.iter().any(|e| e.kind == "text_delta"));
    }

    #[test]
    fn replayed_events_are_deduplicated_by_id() {
        let mut m = model();
        let evs = events(TURN);
        for e in &evs {
            m.apply_event(e);
        }
        let before = m.clone();
        for e in &evs {
            assert_eq!(m.apply_event(e), Applied::Duplicate);
        }
        assert_eq!(m, before);
    }

    #[test]
    fn events_at_or_below_the_snapshot_watermark_are_duplicates() {
        let mut m = model();
        let mut e = events(TURN)[0].clone();
        e.id = 3;
        e.cursor = Some("log_c915bfc9b6d826c4:3".into());
        assert_eq!(m.apply_event(&e), Applied::Duplicate);
    }

    #[test]
    fn a_foreign_generation_forces_resync() {
        let mut m = model();
        let mut e = events(TURN)[0].clone();
        e.cursor = Some("log_other:9".into());
        assert_eq!(
            m.apply_event(&e),
            Applied::NeedsResync(ResyncReason::GenerationChanged {
                have: "log_c915bfc9b6d826c4".into(),
                got: "log_other".into()
            })
        );
        assert_eq!(m.last_id, 3);
    }

    #[test]
    fn seq_is_not_used_for_ordering() {
        // A restarted daemon resets seq; ids keep increasing.
        let mut m = model();
        let mut a = events(TURN)[0].clone();
        a.seq = Some(900);
        let mut b = events(TURN)[1].clone();
        b.seq = Some(1);
        assert_eq!(m.apply_event(&a), Applied::Applied);
        assert_eq!(m.apply_event(&b), Applied::Applied);
    }

    #[test]
    fn lifecycle_events_and_tombstones() {
        let mut m = model();
        let evs = events(LIFECYCLE);
        m.apply_event(&evs[0]);
        assert_eq!(m.sessions[SESSION].title, "Renamed");
        assert_eq!(m.sessions[SESSION].revision, 2);
        m.apply_event(&evs[1]);
        assert!(m.sessions[SESSION].archived);
        m.apply_event(&evs[2]);
        assert!(m.sessions.is_empty());
        assert!(m.selected.is_none());
        assert_eq!(m.selected_deleted.as_deref(), Some(SESSION));
        m.apply_event(&evs[3]);
        assert!(m.projects.is_empty());
    }

    #[test]
    fn a_stale_session_copy_does_not_overwrite_a_newer_one() {
        let mut m = model();
        let evs = events(LIFECYCLE);
        m.apply_event(&evs[0]); // revision 2
        let mut stale = evs[0].clone();
        stale.id = 30;
        stale.cursor = Some("log_c915bfc9b6d826c4:30".into());
        stale.data["revision"] = 1.into();
        stale.data["title"] = "Old".into();
        m.apply_event(&stale);
        assert_eq!(m.sessions[SESSION].title, "Renamed");
    }

    #[test]
    fn snapshot_discards_stale_waits_and_streaming() {
        let mut m = model();
        for e in events(TURN).iter().take(6) {
            m.apply_event(e);
        }
        assert!(!m.approvals.is_empty());
        m.apply_snapshot(serde_json::from_str(SNAPSHOT).unwrap());
        assert!(m.approvals.is_empty() && m.streaming.is_empty() && m.running.is_empty());
        assert_eq!(m.last_id, 3);
        // The selection survives the resync and is re-filled by the snapshot's selected view.
        assert_eq!(m.selected.as_ref().unwrap().session_id, SESSION);
    }

    #[test]
    fn running_turns_in_a_snapshot_mark_sessions_busy() {
        let mut snap: Snapshot = serde_json::from_str(SNAPSHOT).unwrap();
        snap.running.push(
            serde_json::from_value(
                serde_json::json!({"id": "trn_9", "session_id": SESSION, "status": "running"}),
            )
            .unwrap(),
        );
        let mut m = Model::new();
        m.apply_snapshot(snap);
        assert_eq!(m.sessions[SESSION].status.as_deref(), Some("busy"));
        assert_eq!(m.running[SESSION], "trn_9");
    }

    #[test]
    fn project_delete_removes_its_sessions() {
        let mut m = model();
        let mut e = events(LIFECYCLE)[3].clone();
        e.id = 5;
        e.cursor = None;
        m.apply_event(&e);
        assert!(m.sessions.is_empty());
    }
}
