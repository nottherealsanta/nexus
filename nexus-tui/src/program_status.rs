//! OSC 7501 program status report encoding.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use std::collections::BTreeMap;
use std::io::{self, Write};

use crate::api::Event;
use crate::state::AppState;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Idle,
    Working,
    Done,
    Blocked,
    Error,
    Clear,
}

impl State {
    fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Working => "working",
            Self::Done => "done",
            Self::Blocked => "blocked",
            Self::Error => "error",
            Self::Clear => "clear",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Permission,
    Question,
    Auth,
}

impl Kind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Permission => "permission",
            Self::Question => "question",
            Self::Auth => "auth",
        }
    }
}

/// Construct one complete report. IDs must obey the protocol's path grammar.
/// Text is sanitized and truncated on UTF-8 boundaries before encoding.
pub fn report(
    id: &str,
    state: State,
    kind: Option<Kind>,
    title: &str,
    message: &str,
) -> Option<String> {
    if id.is_empty()
        || id.len() > 128
        || id.split('/').count() > 8
        || !id.split('/').all(|segment| {
            !segment.is_empty()
                && segment.len() <= 32
                && segment
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.+-".contains(&b))
        })
    {
        return None;
    }
    let mut result = format!("\x1b]7501;state={}:id={id}", state.as_str());
    if state != State::Clear {
        result.push_str(":app=nexus");
        if state == State::Blocked {
            if let Some(kind) = kind {
                result.push_str(":kind=");
                result.push_str(kind.as_str());
            }
        }
        for (key, text, limit) in [("title", title, 192), ("msg", message, 2048)] {
            let mut clean = String::new();
            for c in text.chars().filter(|c| !c.is_control()) {
                if clean.len() + c.len_utf8() > limit {
                    break;
                }
                clean.push(c);
            }
            if !clean.is_empty() {
                result.push(':');
                result.push_str(key);
                result.push('=');
                result.push_str(&STANDARD.encode(clean.as_bytes()));
            }
        }
    }
    result.push_str("\x1b\\");
    Some(result)
}

/// Terminal records owned by this client. Completed records survive normal exit.
#[derive(Default)]
pub struct Tracker {
    sent: BTreeMap<String, (State, String)>,
    outcomes: BTreeMap<String, State>,
}

impl Tracker {
    /// Call only for events accepted by the reducer, not duplicate replay.
    pub fn event(&mut self, event: &Event) {
        let Some(id) = event.session_id.as_ref() else {
            return;
        };
        match event.kind.as_str() {
            "session.status" if event.data["status"] == "busy" => {
                self.outcomes.remove(id);
            }
            "turn.summary" => {
                let state = match event.data["status"].as_str() {
                    Some("completed") => State::Done,
                    Some("failed") => State::Error,
                    Some("interrupted") => State::Idle,
                    _ => return,
                };
                self.outcomes.insert(id.clone(), state);
            }
            _ => {}
        }
    }

    /// A fresh snapshot contains active work but not authoritative outcomes for
    /// every session. Forget old outcomes rather than presenting stale ones.
    pub fn snapshot(&mut self) {
        self.outcomes.clear();
    }

    pub fn reconcile(&mut self, app: &AppState) -> Vec<String> {
        let mut desired = BTreeMap::new();
        if app.connection.is_live() {
            for session in app.model.sessions.values().filter(|s| !s.archived) {
                let id = format!("nexus/{}", session.id);
                let (state, kind, message) = if app
                    .model
                    .approvals
                    .values()
                    .any(|a| a.session_id == session.id)
                {
                    (State::Blocked, Some(Kind::Permission), "Approval needed")
                } else if app
                    .model
                    .questions
                    .values()
                    .any(|q| q.session_id == session.id)
                {
                    (State::Blocked, Some(Kind::Question), "Answer needed")
                } else if app.model.running.contains_key(&session.id)
                    || session.status.as_deref() == Some("busy")
                {
                    (State::Working, None, "Working")
                } else if let Some(&state) = self.outcomes.get(&session.id) {
                    (
                        state,
                        None,
                        match state {
                            State::Done => "Finished",
                            State::Error => "Failed",
                            _ => "Interrupted",
                        },
                    )
                } else {
                    continue;
                };
                if let Some(sequence) = report(&id, state, kind, &session.title, message) {
                    desired.insert(id, (state, sequence));
                }
            }
        }
        self.outcomes
            .retain(|id, _| app.model.sessions.get(id).is_some_and(|s| !s.archived));
        let mut output = Vec::new();
        for id in self.sent.keys().filter(|id| !desired.contains_key(*id)) {
            if let Some(clear) = report(id, State::Clear, None, "", "") {
                output.push(clear);
            }
        }
        for (id, entry) in &desired {
            if self.sent.get(id) != Some(entry) {
                output.push(entry.1.clone());
            }
        }
        self.sent = desired;
        output
    }

    /// Clear transient records while yielding the terminal. Keep done/error so
    /// a user can still discover them after exiting the TUI.
    pub fn release(&mut self) -> Vec<String> {
        let ids: Vec<_> = self
            .sent
            .iter()
            .filter(|(_, (state, _))| !matches!(state, State::Done | State::Error))
            .map(|(id, _)| id.clone())
            .collect();
        ids.into_iter()
            .filter_map(|id| {
                self.sent.remove(&id);
                report(&id, State::Clear, None, "", "")
            })
            .collect()
    }
}

impl Drop for Tracker {
    fn drop(&mut self) {
        emit(self.release());
    }
}

pub fn emit(sequences: Vec<String>) {
    if sequences.is_empty() {
        return;
    }
    let mut out = io::stdout().lock();
    for sequence in sequences {
        let _ = out.write_all(sequence.as_bytes());
    }
    let _ = out.flush();
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::ConnectionState;
    use serde_json::json;

    fn event(kind: &str, id: &str, data: serde_json::Value) -> Event {
        Event {
            id: 1,
            cursor: None,
            seq: None,
            time: 0.0,
            kind: kind.into(),
            session_id: Some(id.into()),
            data,
        }
    }

    fn live() -> AppState {
        let mut app = AppState::new();
        app.connection = ConnectionState::Live;
        for id in ["ses_a", "ses_b"] {
            let session =
                serde_json::from_value(json!({"id":id,"title":id,"status":"busy"})).unwrap();
            app.model.sessions.insert(id.into(), session);
        }
        app
    }

    #[test]
    fn lifecycle_reconciles_multiple_sessions_and_outcomes() {
        let mut app = live();
        let mut tracker = Tracker::default();
        assert_eq!(tracker.reconcile(&app).len(), 2);
        assert!(tracker.reconcile(&app).is_empty());
        app.model.approvals.insert(
            "p".into(),
            serde_json::from_value(json!({"id":"p","session_id":"ses_a"})).unwrap(),
        );
        let output = tracker.reconcile(&app);
        assert_eq!(output.len(), 1);
        assert!(output[0].contains("state=blocked:id=nexus/ses_a:app=nexus:kind=permission"));
        app.model.approvals.clear();
        app.model.questions.insert(
            "q".into(),
            serde_json::from_value(json!({"id":"q","session_id":"ses_a"})).unwrap(),
        );
        assert!(tracker.reconcile(&app)[0].contains("kind=question"));
        app.model.questions.clear();
        tracker.event(&event("turn.summary", "ses_a", json!({"status":"failed"})));
        assert!(tracker.reconcile(&app)[0].contains("state=working"));
        app.model.sessions.get_mut("ses_a").unwrap().status = Some("idle".into());
        assert!(tracker.reconcile(&app)[0].contains("state=error"));
        tracker.event(&event(
            "turn.summary",
            "ses_b",
            json!({"status":"interrupted"}),
        ));
        app.model.sessions.get_mut("ses_b").unwrap().status = Some("idle".into());
        assert!(tracker.reconcile(&app)[0].contains("state=idle"));
        let released = tracker.release();
        assert_eq!(released.len(), 1);
        assert!(released[0].contains("state=clear:id=nexus/ses_b"));
        assert!(tracker.sent.contains_key("nexus/ses_a"));
    }

    #[test]
    fn reconnect_snapshot_and_deletion_clear_stale_records() {
        let mut app = live();
        let mut tracker = Tracker::default();
        tracker.reconcile(&app);
        app.connection = ConnectionState::Connecting;
        assert_eq!(tracker.reconcile(&app).len(), 2);
        app.connection = ConnectionState::Live;
        assert_eq!(tracker.reconcile(&app).len(), 2);
        tracker.event(&event(
            "turn.summary",
            "ses_a",
            json!({"status":"completed"}),
        ));
        app.model.sessions.get_mut("ses_a").unwrap().status = Some("idle".into());
        assert!(tracker.reconcile(&app)[0].contains("state=done"));
        tracker.snapshot();
        assert!(tracker.reconcile(&app)[0].contains("state=clear:id=nexus/ses_a"));
        app.model.sessions.remove("ses_b");
        assert!(tracker.reconcile(&app)[0].contains("state=clear:id=nexus/ses_b"));
    }

    #[test]
    fn encodes_and_sanitizes_text() {
        assert_eq!(report("nexus/abc", State::Blocked, Some(Kind::Permission), "A\nB", "hi\x1b!"),
            Some("\x1b]7501;state=blocked:id=nexus/abc:app=nexus:kind=permission:title=QUI=:msg=aGkh\x1b\\".into()));
    }

    #[test]
    fn validates_ids_and_scopes_clear() {
        for id in [
            "",
            "/a",
            "a/",
            "a::b",
            "a//b",
            "012345678901234567890123456789012",
        ] {
            assert_eq!(report(id, State::Clear, None, "", ""), None);
        }
        assert_eq!(
            report("nexus/a", State::Clear, None, "ignored", "ignored").unwrap(),
            "\x1b]7501;state=clear:id=nexus/a\x1b\\"
        );
    }

    #[test]
    fn bounded_utf8_and_kind_only_for_blocked() {
        let sequence = report(
            "nexus/a",
            State::Working,
            Some(Kind::Auth),
            &"😀".repeat(100),
            &"😀".repeat(1000),
        )
        .unwrap();
        assert!(sequence.len() < 4096);
        assert!(!sequence.contains("kind="));
        let title = sequence
            .split("title=")
            .nth(1)
            .unwrap()
            .split(':')
            .next()
            .unwrap();
        assert_eq!(STANDARD.decode(title).unwrap().len(), 192);
        let msg = sequence
            .split("msg=")
            .nth(1)
            .unwrap()
            .trim_end_matches("\x1b\\");
        assert_eq!(STANDARD.decode(msg).unwrap().len(), 2048);
    }
}
