//! Plan 37: `/continue` and sends show a pending working row at once, driven by real events.
mod common;
use common::*;
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::api::Frame;
use nexus_tui::app::{App, Effect};
use nexus_tui::state::ConnectionState;
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};
use std::time::{Duration, Instant};

fn app() -> App {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.ui.detail = nexus_tui::ui::Detail::Low;
    a
}

fn event(a: &mut App, id: u64, kind: &str, data: serde_json::Value) {
    let v = serde_json::json!({"id": id, "type": kind, "session_id": SESSION,
        "cursor": format!("log_c915bfc9b6d826c4:{id}"), "data": data});
    if let Ok(Frame::Event(e)) = Frame::from_value(kind, None, &v) {
        a.apply_sync(SyncMsg::Event(e));
    }
}

fn text(a: &mut App) -> String {
    draw(a, 120, 36).1.join("\n")
}

fn continues(a: &App) -> Vec<(String, String)> {
    a.effects
        .iter()
        .filter_map(|e| match e {
            Effect::Api(ApiCall::Continue { session, key }) => {
                Some((session.clone(), key.as_str().to_owned()))
            }
            _ => None,
        })
        .collect()
}

/// An interrupted last turn, so `/continue` is offered.
fn interrupted(a: &mut App) {
    event(
        a,
        900,
        "session.error",
        serde_json::json!({"kind": "interrupted", "message": "interrupted by the user"}),
    );
}

#[test]
fn continue_shows_the_pending_row_in_the_same_frame_and_phases_follow_events() {
    let mut a = app();
    a.effects.clear();
    a.execute("session.continue");
    let first = text(&mut a);
    assert!(first.contains("Waiting for Core"), "{first}");
    assert_eq!(continues(&a).len(), 1, "sent at once while connected");
    assert!(a.is_busy(SESSION), "the composer behaves as busy");
    // Core accepted: still waiting for Core's events.
    a.apply_done(ActionDone {
        call: ApiCall::Continue {
            session: SESSION.into(),
            key: nexus_tui::api::RequestId::new(),
        },
        result: Ok(Done::default()),
    });
    assert!(text(&mut a).contains("Waiting for Core"));
    // busy: now the model.
    event(
        &mut a,
        901,
        "session.status",
        serde_json::json!({"status": "busy", "turn_id": "t_x"}),
    );
    let second = text(&mut a);
    assert!(second.contains("Waiting for the model"), "{second}");
    assert!(!second.contains("Waiting for Core"), "{second}");
    // First delta: the normal live turn takes over.
    event(
        &mut a,
        902,
        "text_delta",
        serde_json::json!({"request_id": "r", "text": "Hi"}),
    );
    assert!(!a.ui.pending_turns.contains_key(SESSION));
}

#[test]
fn a_network_wait_replaces_the_phase_text() {
    let mut a = app();
    interrupted(&mut a);
    a.execute("session.continue");
    event(
        &mut a,
        901,
        "session.status",
        serde_json::json!({"status": "busy", "turn_id": "t_x"}),
    );
    event(
        &mut a,
        902,
        "retry_scheduled",
        serde_json::json!({"attempt": 1, "next_attempt": 2, "delay": 8.0,
        "phase": "waiting_network", "reason": "codex: network error: ConnectError", "max_attempts": 3}),
    );
    let t = text(&mut a);
    assert!(t.contains("Waiting for network · retrying in 8.0s"), "{t}");
}

#[test]
fn a_refusal_removes_the_row_and_the_strip_gives_the_reason() {
    let mut a = app();
    interrupted(&mut a);
    a.execute("session.continue");
    assert!(a.ui.pending_turns.contains_key(SESSION));
    a.apply_done(ActionDone {
        call: ApiCall::Continue {
            session: SESSION.into(),
            key: nexus_tui::api::RequestId::new(),
        },
        result: Err("session has unfinished durable turn t_1".into()),
    });
    assert!(!a.ui.pending_turns.contains_key(SESSION));
    let t = text(&mut a);
    assert!(!t.contains("Waiting for Core"), "{t}");
    assert!(
        t.contains("Continue refused · session has unfinished"),
        "{t}"
    );
}

#[test]
fn with_core_disconnected_the_call_is_queued_then_sent_once_with_one_key() {
    let mut a = app();
    interrupted(&mut a);
    a.state.connection = ConnectionState::Offline {
        reason: "down".into(),
    };
    a.effects.clear();
    a.execute("session.continue");
    assert!(continues(&a).is_empty(), "not sent while disconnected");
    assert!(text(&mut a).contains("not connected"));
    a.state.connection = ConnectionState::Live;
    a.tick(Instant::now());
    let sent = continues(&a);
    assert_eq!(sent.len(), 1);
    a.tick(Instant::now());
    assert_eq!(continues(&a).len(), 1, "sent once");
    assert_eq!(
        a.ui.pending_turns[SESSION].key.as_ref().unwrap().as_str(),
        sent[0].1
    );
}

#[test]
fn esc_cancels_a_queued_continue_before_it_is_sent() {
    let mut a = app();
    interrupted(&mut a);
    a.state.connection = ConnectionState::Offline {
        reason: "down".into(),
    };
    a.effects.clear();
    a.execute("session.continue");
    assert!(a.ui.pending_turns.contains_key(SESSION));
    a.execute("session.interrupt");
    assert!(!a.ui.pending_turns.contains_key(SESSION));
    a.state.connection = ConnectionState::Live;
    a.tick(Instant::now());
    assert!(
        continues(&a).is_empty(),
        "a cancelled continue is never sent"
    );
    assert!(!a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Interrupt { .. }))));
}

#[test]
fn after_ten_seconds_without_an_answer_the_strip_offers_retry_with_the_same_key() {
    let mut a = app();
    interrupted(&mut a);
    a.effects.clear();
    a.execute("session.continue");
    let key = continues(&a)[0].1.clone();
    a.clock = Instant::now() + Duration::from_secs(11);
    let t = text(&mut a);
    assert!(t.contains("No answer from Core yet"), "{t}");
    assert!(t.contains("Retry"), "{t}");
    a.effects.clear();
    a.execute("session.continue"); // what pressing Retry runs
    assert_eq!(
        continues(&a),
        vec![(SESSION.to_owned(), key)],
        "resent with the same key"
    );
}

#[test]
fn a_prompt_send_gets_the_same_pending_row_until_the_turn_shows() {
    let mut a = app();
    a.effects.clear();
    typing(&mut a, "hello again");
    press(&mut a, &["enter"]);
    assert!(
        a.ui.pending_turns.contains_key(SESSION),
        "D3: sends get the pending row too"
    );
    assert!(text(&mut a).contains("Waiting for Core"));
    event(
        &mut a,
        910,
        "session.status",
        serde_json::json!({"status": "busy", "turn_id": "t_y"}),
    );
    assert!(text(&mut a).contains("Waiting for the model"));
    event(
        &mut a,
        911,
        "reasoning_delta",
        serde_json::json!({"request_id": "r", "text": "hm"}),
    );
    assert!(!a.ui.pending_turns.contains_key(SESSION));
}
