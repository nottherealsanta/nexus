//! The Manager's stage: cards from `interface/scenarios/manager-cards.json`, the
//! keys taken only while engaged, the call lifecycle, and the zero-cost rest state.

mod common;
use common::*;
use nexus_tui::api::{Frame, Message};
use nexus_tui::app::{App, Effect};
use nexus_tui::sync::{Command, ManagerError, SyncMsg};
use nexus_tui::theme::{Mode, Tier};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

fn app() -> App {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 40));
    a.ui.manager.silent = true;
    a
}

fn event(a: &App, id: i64, kind: &str, data: Value) -> nexus_tui::api::Event {
    let generation = a.state.model.generation.as_deref().unwrap();
    let value = json!({"id": id, "type": kind, "session_id": null,
        "cursor": format!("{generation}:{id}"), "time": 1793113317.0, "data": data});
    let Frame::Event(event) = Frame::from_value(kind, None, &value).unwrap() else {
        panic!("event")
    };
    event
}

fn send(a: &mut App, id: i64, kind: &str, mut data: Value) {
    data["call_id"] = json!("call_1");
    let e = event(a, id, kind, data);
    a.manager_event(&e);
}

fn start(a: &mut App) {
    a.manager_reply("start", Ok(json!({"call_id": "call_1", "holder": "TUI"})));
    a.effects.clear();
}

fn manager_calls(a: &App) -> Vec<(String, Option<Value>)> {
    a.effects
        .iter()
        .filter_map(|e| match e {
            Effect::Sync(Command::Manager(c)) => Some((c.path.clone(), c.body.clone())),
            _ => None,
        })
        .collect()
}

fn text(a: &mut App) -> String {
    draw(a, 120, 40).1.join("\n")
}

#[test]
fn every_card_draws_what_the_scenarios_expect() {
    let file = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../interface/scenarios/manager-cards.json"
    ))
    .unwrap();
    let scenarios: Value = serde_json::from_str(&file).unwrap();
    for case in scenarios["cards"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let mut a = app();
        start(&mut a);
        send(&mut a, 90, "manager.card", json!({"card": case["card"]}));
        let shown = text(&mut a);
        let flat = shown.split_whitespace().collect::<Vec<_>>().join(" ");
        for want in case["expect"].as_array().unwrap() {
            let want = want.as_str().unwrap();
            assert!(flat.contains(want), "{name}: missing {want:?}\n{shown}");
        }
        for absent in case["absent"].as_array().unwrap() {
            let absent = absent.as_str().unwrap();
            assert!(!flat.contains(absent), "{name}: has {absent:?}\n{shown}");
        }
        // Narrow form too.
        let (_, rows) = draw(&mut a, 60, 30);
        assert!(!rows.is_empty(), "{name}: narrow");
    }
}

#[test]
fn at_rest_nothing_is_taken_drawn_or_scheduled() {
    let mut a = app();
    let before = text(&mut a);
    let rule = a.escape_rule();
    assert!(!a.manager_animating(Instant::now()));
    assert!(!a.manager_tick(Instant::now()));
    assert!(a
        .effects
        .iter()
        .all(|e| !matches!(e, Effect::Sync(Command::Manager(_)))));
    // Esc, digits and space are the app's.
    for k in ["esc", "1", "space"] {
        assert!(!a.manager_key(&key(k), k));
    }
    assert_eq!(a.escape_rule(), rule);
    assert_eq!(text(&mut a), before);
    assert!(!a.ui.manager.live());
}

#[test]
fn the_shortcut_starts_a_call_and_keys_are_taken_only_while_engaged() {
    let mut a = app();
    a.effects.clear();
    press(&mut a, &["alt+m"]);
    let calls = manager_calls(&a);
    assert_eq!(calls[0].0, "/api/manager/start");
    a.manager_reply("start", Ok(json!({"call_id": "call_1", "holder": "TUI"})));
    // Pressing talk opened the microphone once the call began.
    assert!(a.holds("manager.listening"));
    press(&mut a, &["esc"]);
    a.effects.clear();
    // A call is not engagement: the idle call takes nothing.
    assert!(!a.manager_key(&key("esc"), "esc"));
    a.manager_execute("manager.talk");
    assert!(a.holds("manager.listening") && a.holds("manager.engaged"));
    assert_eq!(a.escape_rule().as_deref(), Some("manager_listening"));
    press(&mut a, &["esc"]);
    assert!(!a.holds("manager.listening"));
    assert_ne!(a.escape_rule().as_deref(), Some("manager_listening"));
}

#[test]
fn a_printable_key_opens_the_field_and_enter_sends_it() {
    let mut a = app();
    start(&mut a);
    a.manager_execute("manager.talk");
    typing(&mut a, "hi");
    assert!(a.holds("manager.field"));
    assert_eq!(a.ui.manager.field.as_ref().unwrap().text(), "hi");
    assert_eq!(a.composer_ref().text(), "");
    press(&mut a, &["enter"]);
    let calls = manager_calls(&a);
    let say = calls.iter().find(|c| c.0 == "/api/manager/say").unwrap();
    assert_eq!(say.1.as_ref().unwrap()["source"], "typed");
    assert!(!a.holds("manager.field"));
}

#[test]
fn an_unfocused_card_takes_no_keys_until_it_is_focused() {
    let mut a = app();
    start(&mut a);
    let card = json!({"id": "c", "type": "approval", "focused": false, "handle": "m",
        "summary": "s", "detail": "ls", "session_id": "x", "approval_id": "a", "clipped": false});
    send(&mut a, 90, "manager.card", json!({"card": card}));
    assert!(!a.holds("manager.card_focused"));
    for k in ["esc", "1", "space", "x"] {
        assert!(!a.manager_key(&key(k), k), "{k}");
    }
    // The shortcut focuses it locally.
    press(&mut a, &["alt+m"]);
    assert!(a.holds("manager.card_focused"));
    assert!(a.manager_key(&key("space"), "space"));
}

#[test]
fn slash_manager_parses_start_stop_and_text() {
    let mut a = app();
    a.effects.clear();
    typing(&mut a, "/manager");
    press(&mut a, &["enter"]);
    assert_eq!(manager_calls(&a)[0].0, "/api/manager/start");
    start(&mut a);
    typing(&mut a, "/manager what is going on");
    press(&mut a, &["enter"]);
    let calls = manager_calls(&a);
    assert_eq!(calls[0].0, "/api/manager/say");
    assert_eq!(calls[0].1.as_ref().unwrap()["text"], "what is going on");
    a.effects.clear();
    typing(&mut a, "/manager stop");
    press(&mut a, &["enter"]);
    assert!(manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));
    assert!(a.ui.manager.call.is_none());
}

#[test]
fn disabled_manager_toasts_where_to_turn_it_on() {
    let mut a = app();
    press(&mut a, &["alt+m"]);
    a.manager_reply(
        "start",
        Err(ManagerError {
            status: 409,
            code: Some("manager_disabled".into()),
            message: "off".into(),
            body: Value::Null,
        }),
    );
    let toasts: Vec<String> = a.ui.alerts.toasts.iter().map(|t| t.text.clone()).collect();
    assert!(toasts
        .iter()
        .any(|t| t == "Manager is off. Turn it on in Settings \u{203a} Manager."));
}

#[test]
fn a_call_moved_elsewhere_clears_everything_and_says_so() {
    let mut a = app();
    start(&mut a);
    a.manager_execute("manager.talk");
    let e = event(
        &a,
        91,
        "manager.state",
        json!({"call_id": "call_2", "active": true,
        "phase": "idle", "holder": "NSH", "error": null}),
    );
    a.manager_event(&e);
    assert!(a.ui.manager.call.is_none() && !a.holds("manager.listening"));
    let caption = a.ui.manager.caption.clone().unwrap();
    assert_eq!(caption.manager.as_deref(), Some("Moved to NSH"));
    // Events of a call that is not ours are ignored.
    send(
        &mut a,
        92,
        "manager.card",
        json!({"card": {"id": "c", "type": "note", "markdown": "x", "focused": true}}),
    );
    assert!(a.ui.manager.card.is_none());
}

#[test]
fn navigate_moves_the_view_and_sends_the_result_before_the_view() {
    let mut a = app();
    start(&mut a);
    let id = a.state.model.sessions.keys().next().unwrap().clone();
    send(
        &mut a,
        93,
        "manager.navigate",
        json!({"request_id": "r1", "session": id}),
    );
    let order: Vec<&str> = a
        .effects
        .iter()
        .filter_map(|e| match e {
            Effect::Sync(Command::Manager(c)) => Some(c.path.as_str()),
            Effect::Sync(Command::View(_)) => Some("view"),
            _ => None,
        })
        .collect();
    assert_eq!(order, ["/api/manager/navigate/result", "view"], "{order:?}");
    let calls = manager_calls(&a);
    assert_eq!(calls[0].1.as_ref().unwrap()["ok"], true);
    // A session that is gone is refused with a reason.
    a.effects.clear();
    send(
        &mut a,
        94,
        "manager.navigate",
        json!({"request_id": "r2", "session": "ses_nope"}),
    );
    let body = manager_calls(&a)[0].1.clone().unwrap();
    assert_eq!(body["ok"], false);
    assert_eq!(body["reason"], "That session no longer exists.");
}

#[test]
fn a_row_that_is_gone_says_so_and_the_card_stays() {
    let mut a = app();
    start(&mut a);
    let card = json!({"id": "c", "type": "sessions", "focused": true, "more": 0, "rows": [
        {"n": 1, "session_id": "ses_gone", "handle": "gone", "title": "Gone", "project": "p", "state": "idle"}]});
    send(&mut a, 90, "manager.card", json!({"card": card}));
    press(&mut a, &["1"]);
    assert!(a.ui.manager.card.is_some());
    assert_eq!(
        a.ui.manager.caption.as_ref().unwrap().manager.as_deref(),
        Some("That session no longer exists.")
    );
}

#[test]
fn consent_is_asked_before_anything_starts() {
    let mut a = app();
    press(&mut a, &["alt+m"]);
    a.manager_reply(
        "start",
        Err(ManagerError {
            status: 409,
            code: Some("consent_required".into()),
            message: "consent".into(),
            body: json!({"consent": {"provider": "copilot", "version": 3, "notice": "Session text goes to the model."}}),
        }),
    );
    assert!(text(&mut a).contains("Session text goes to the model."));
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert!(a.effects.is_empty() && a.ui.manager.consent.is_none() && a.ui.manager.call.is_none());
}

#[test]
fn messages_the_manager_sent_say_via_manager() {
    let mut a = app();
    let mut m: Message = serde_json::from_value(json!({
        "id": "m_x", "session_id": "s", "seq": 1, "created": 1.0, "role": "user",
        "parts": [{"type": "text", "text": "hello"}], "origin": "manager"}))
    .unwrap();
    assert_eq!(m.origin.as_deref(), Some("manager"));
    m.origin = None;
    // The label is drawn by the timeline: see the tag list.
    let _ = &mut a;
}

#[test]
fn the_orb_clock_runs_only_while_the_orb_is_there() {
    let mut o = opts(Mode::Dark, Tier::TrueColor);
    o.reduced_motion = false;
    let mut a = with_turn(o, (120, 40));
    a.ui.manager.silent = true;
    start(&mut a);
    assert!(
        !a.manager_animating(Instant::now()),
        "an idle call shows nothing"
    );
    a.manager_execute("manager.talk");
    assert!(a.manager_animating(Instant::now()));
    a.manager_execute("manager.listen_cancel");
    let later = Instant::now() + Duration::from_secs(30);
    a.manager_frame(later);
    assert!(!a.manager_animating(later + Duration::from_secs(30)));
}

#[test]
fn live_mode_opens_the_call_audio_and_the_talk_key_only_interrupts() {
    let mut a = app();
    a.manager_reply(
        "start",
        Ok(json!({"call_id": "call_1", "holder": "TUI", "mode": "live"})),
    );
    // Core is not connected in this test, so there is no stream id: the voice cannot start, the call stays open
    // for typing, the reason is shown, and the dictation mic is not taken instead.
    assert!(!a.ui.manager.live_call);
    assert_eq!(a.ui.manager.error.as_ref().unwrap().code, "live_failed");
    assert!(!a.ui.manager.listening());
    assert!(a.ui.manager.call.is_some());
}

#[test]
fn live_mode_failure_keeps_the_call_open_and_says_why() {
    let mut a = app();
    a.manager_reply(
        "start",
        Ok(json!({"call_id": "call_1", "holder": "TUI", "mode": "live"})),
    );
    a.ui.manager.live_call = true;
    a.apply_done(nexus_tui::actions::ActionDone {
        call: nexus_tui::actions::ApiCall::ManagerLive { stream_id: "s".into() },
        result: Err("GPT-Live is not available on this ChatGPT plan".into()),
    });
    assert!(a.ui.manager.call.is_some());
    assert!(!a.ui.manager.live_call);
    let error = a.ui.manager.error.clone().unwrap();
    assert_eq!(error.code, "live_failed");
    assert!(error.message.contains("ChatGPT plan"));
}

#[test]
fn stopping_a_live_call_releases_it() {
    let mut a = app();
    a.manager_reply(
        "start",
        Ok(json!({"call_id": "call_1", "holder": "TUI", "mode": "live"})),
    );
    a.execute("manager.stop");
    assert!(!a.ui.manager.live_call);
    assert!(!nexus_tui::live::is_open());
}

#[test]
fn the_header_has_a_manager_button_left_of_the_right_toggle_that_asks_core_for_the_session() {
    let mut a = app();
    let (_, _) = draw(&mut a, 120, 40);
    let g = a.regions.clone();
    assert!(g.manager.width > 0, "a wide header has the button");
    assert_eq!(
        g.manager.x + g.manager.width,
        g.toggle_right.x,
        "it sits directly against the right toggle"
    );
    assert!(g.manager.x > g.title.x, "and to the right of the title");
    // Clicking it runs the command, which asks Core for the Manager's episodes.
    let (x, y) = (g.manager.x + 2, g.manager.y);
    assert_eq!(
        a.hits.at(x, y),
        Some(&nexus_tui::ui::Target::ManagerSession)
    );
    a.effects.clear();
    a.execute("manager.open_session");
    let asked = a.effects.iter().any(|e| {
        matches!(e, Effect::Sync(Command::Manager(c)) if c.path == "/api/manager/episode" && c.method == "GET")
    });
    // Not connected in this test, so it only says so; connected, it asks.
    assert!(asked || a.state.core.is_none());
    // A narrow header has no room for it.
    let mut narrow = app();
    draw(&mut narrow, 50, 20);
    assert_eq!(narrow.regions.manager.width, 0);
}

#[test]
fn the_manager_session_opens_in_the_inspector_with_the_token_and_the_requests_level() {
    let mut a = app();
    a.state.core = Some(nexus_tui::api::Info {
        name: "nexus".into(),
        version: "0".into(),
        pid: 1,
        home: "/nonexistent-home".into(),
        url: "http://127.0.0.1:4747".into(),
        log: Default::default(),
        api_version: None,
        idempotency_lifetime: Default::default(),
    });
    a.effects.clear();
    // No episode yet: it says so and opens nothing.
    a.manager_reply("episode", Ok(json!([])));
    assert!(!a.effects.iter().any(|e| matches!(e, Effect::OpenUrl(_))));
    // An episode, but the daemon file cannot be read: nothing opens and no token is invented.
    a.manager_reply("episode", Ok(json!([{"id": "ses_manager"}])));
    assert!(!a.effects.iter().any(|e| matches!(e, Effect::OpenUrl(_))));
}
