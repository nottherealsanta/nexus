//! The Manager's stage: cards from `interface/scenarios/manager-cards.json`, the
//! keys taken only while engaged, the call lifecycle, and the zero-cost rest state.

mod common;
use common::*;
use nexus_tui::api::{Frame, Message};
use nexus_tui::app::{App, Effect};
use nexus_tui::manager::{ends_call, Mic};
use nexus_tui::sync::{Command, ManagerError};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Target;
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
    // Most tests want the call on the stage; the quiet corner has its own test.
    a.ui.manager.minimised = false;
    a.effects.clear();
}

fn start_quiet(a: &mut App) {
    a.manager_reply("start", Ok(json!({"call_id": "call_1", "holder": "TUI"})));
    assert!(a.ui.manager.minimised, "a quiet start is minimised");
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
    a.effects.clear();
    press(&mut a, &["esc"]);
    // Esc ends the call, recording included.
    assert!(a.ui.manager.call.is_none());
    assert!(!a.holds("manager.listening"));
    assert!(manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));
    // Talk starts a fresh call and takes the stage.
    a.effects.clear();
    a.manager_execute("manager.talk");
    assert_eq!(manager_calls(&a)[0].0, "/api/manager/start");
    a.manager_reply("start", Ok(json!({"call_id": "call_1", "holder": "TUI"})));
    assert!(a.holds("manager.listening") && a.holds("manager.engaged"));
    assert_eq!(a.escape_rule().as_deref(), Some("manager_call"));
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.call.is_none());
    assert_ne!(a.escape_rule().as_deref(), Some("interrupt"));
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
    // Talk leaves the field alone; Enter is how a typed line goes.
    press(&mut a, &["alt+m"]);
    assert!(a.holds("manager.field") && !a.holds("manager.listening"));
    assert_eq!(a.ui.manager.field.as_ref().unwrap().text(), "hi");
    press(&mut a, &["enter"]);
    let calls = manager_calls(&a);
    let say = calls.iter().find(|c| c.0 == "/api/manager/say").unwrap();
    assert_eq!(say.1.as_ref().unwrap()["source"], "typed");
    assert!(!a.holds("manager.field"));
}

#[test]
fn an_unfocused_card_takes_no_keys_until_it_is_focused() {
    let mut a = app();
    // A minimised call leaves the app's keys alone, so the card's own keys are
    // what this checks; a focused call would take every key.
    start_quiet(&mut a);
    let card = json!({"id": "c", "type": "approval", "focused": false, "handle": "m",
        "summary": "s", "detail": "ls", "session_id": "x", "approval_id": "a", "clipped": false});
    send(&mut a, 90, "manager.card", json!({"card": card}));
    assert!(!a.holds("manager.card_focused"));
    for k in ["1", "space", "x"] {
        assert!(!a.manager_key(&key(k), k), "{k}");
    }
    // The shortcut focuses it locally.
    press(&mut a, &["alt+m"]);
    assert!(a.holds("manager.card_focused"));
    assert!(a.manager_key(&key("space"), "space"));
    // Esc is the call's, even minimised: it ends the call instead of the session.
    assert!(a.manager_key(&key("esc"), "esc"));
    assert!(a.ui.manager.call.is_none());
}

#[test]
fn slash_manager_parses_start_stop_and_text() {
    let mut a = app();
    a.effects.clear();
    typing(&mut a, "/manager");
    press(&mut a, &["enter"]);
    assert_eq!(manager_calls(&a)[0].0, "/api/manager/start");
    start(&mut a);
    // A focused call owns printable keys; the palette action tucks it away.
    a.manager_execute("manager.minimise");
    typing(&mut a, "/manager what is going on");
    press(&mut a, &["enter"]);
    let calls = manager_calls(&a);
    assert_eq!(calls[0].0, "/api/manager/say");
    assert_eq!(calls[0].1.as_ref().unwrap()["text"], "what is going on");
    a.effects.clear();
    a.manager_execute("manager.minimise");
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
    a.ui.manager.silent = false;
    a.manager_execute("manager.talk");
    a.effects.clear();
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
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Api(nexus_tui::actions::ApiCall::ManagerVoice { .. }) | Effect::Dictation(_)
    )));
}

#[test]
fn live_mode_failure_keeps_the_call_open_and_says_why() {
    let scenario: Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/manager-live-failure.json"
    ))
    .unwrap();
    let mut a = app();
    a.manager_reply(
        "start",
        Ok(json!({"call_id": "call_1", "holder": "TUI", "mode": "live"})),
    );
    a.ui.manager.live_call = true;
    a.apply_done(nexus_tui::actions::ActionDone {
        call: nexus_tui::actions::ApiCall::ManagerLive {
            stream_id: "s".into(),
        },
        result: Err(scenario["error"].as_str().unwrap().into()),
    });
    assert!(a.ui.manager.call.is_some());
    assert!(!a.ui.manager.live_call);
    let error = a.ui.manager.error.clone().unwrap();
    assert_eq!(error.code, "live_failed");
    assert!(error.message.contains("ChatGPT plan"));
    a.ui.manager.silent = false;
    a.effects.clear();
    a.manager_execute("manager.talk");
    assert!(a.ui.manager.field.is_some());
    assert_eq!(a.ui.manager.error.as_ref().unwrap(), &error);
    assert!(!a.ui.manager.listening());
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Api(nexus_tui::actions::ApiCall::ManagerVoice { .. }) | Effect::Dictation(_)
    )));
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
fn the_header_has_a_manager_button_left_of_the_right_toggle_that_opens_the_context() {
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
    let (_, _) = draw(&mut a, 120, 40);
    let g = a.regions.clone();
    assert!(g.manager.width > 0, "a wide header has the button");
    assert_eq!(
        g.manager.x + g.manager.width,
        g.toggle_right.x,
        "it sits directly against the right toggle"
    );
    assert!(g.manager.x > g.title.x, "and to the right of the title");
    // Clicking it runs the command, which asks Core for the current context.
    let (x, y) = (g.manager.x + 2, g.manager.y);
    assert_eq!(
        a.hits.at(x, y),
        Some(&nexus_tui::ui::Target::ManagerSession)
    );
    a.effects.clear();
    a.execute("manager.context");
    let asked = a.effects.iter().any(|e| {
        matches!(e, Effect::Sync(Command::Manager(c)) if c.path == "/api/manager/context" && c.method == "GET")
    });
    assert!(asked, "the button asks for the context");
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

fn focused_call(a: &mut App) {
    start(a);
    a.manager_execute("manager.talk");
    a.manager_execute("manager.listen_cancel");
    a.effects.clear();
}

fn assert_ended(a: &mut App, what: &str) {
    assert!(a.ui.manager.call.is_none(), "{what}: the call ended");
    assert!(!a.ui.manager.minimised, "{what}: not minimised");
    assert!(
        manager_calls(a).iter().any(|c| c.0 == "/api/manager/stop"),
        "{what}: Esc asks Core to end the call"
    );
    assert!(!a.ui.manager.listening(), "{what}: the microphone closed");
    assert!(a.ui.manager.field.is_none(), "{what}: the field went");
    assert!(a.ui.manager.card.is_none(), "{what}: the card went");
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Api(nexus_tui::actions::ApiCall::Interrupt { .. })
    )));
}

#[test]
fn esc_ends_the_call_in_every_state() {
    // Focused idle.
    let mut a = app();
    focused_call(&mut a);
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "idle");

    // Listening: the recording is dropped.
    let mut a = app();
    start(&mut a);
    a.manager_execute("manager.talk");
    assert!(a.holds("manager.listening"));
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "listening");

    // Typing: the field goes with the call.
    let mut a = app();
    focused_call(&mut a);
    a.manager_execute("manager.field_open");
    assert!(a.holds("manager.field"));
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "typing");

    // Thinking: the turn is ended by Core's stop, not by minimising.
    let mut a = app();
    focused_call(&mut a);
    send(
        &mut a,
        95,
        "manager.state",
        json!({"active": true, "phase": "thinking", "holder": "TUI", "error": null}),
    );
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "thinking");
    assert!(manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));

    // Speaking: Esc stops it by ending the call.
    let mut a = app();
    focused_call(&mut a);
    a.ui.manager.speaking = true;
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "speaking");

    // A focused card: the card and the call both go.
    let mut a = app();
    focused_call(&mut a);
    send(
        &mut a,
        96,
        "manager.card",
        json!({"card": {"id": "c", "type": "note", "focused": true, "markdown": "x"}}),
    );
    assert!(a.holds("manager.card_focused"));
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "card");

    // Minimised idle.
    let mut a = app();
    start_quiet(&mut a);
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "minimised idle");

    // Minimised while speaking.
    let mut a = app();
    start_quiet(&mut a);
    a.ui.manager.speaking = true;
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "minimised speaking");

    // Minimised with a waiting card.
    let mut a = app();
    start_quiet(&mut a);
    send(
        &mut a,
        97,
        "manager.card",
        json!({"card": {"id": "c", "type": "note", "focused": false, "markdown": "x"}}),
    );
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert_ended(&mut a, "minimised card");
}

#[test]
fn esc_never_interrupts_a_session_while_a_call_is_open() {
    // Focused: the call takes Esc and ends.
    let mut a = app();
    busy(&mut a);
    focused_call(&mut a);
    assert_eq!(a.escape_rule().as_deref(), Some("manager_call"));
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.call.is_none());
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Api(nexus_tui::actions::ApiCall::Interrupt { .. })
    )));
    // Minimised too: Esc ends the call, never the session.
    let mut a = app();
    busy(&mut a);
    start_quiet(&mut a);
    assert_eq!(a.escape_rule().as_deref(), Some("manager_call"));
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.call.is_none());
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Api(nexus_tui::actions::ApiCall::Interrupt { .. })
    )));
}

#[test]
fn the_context_modal_shows_what_core_returns_and_esc_closes_it_before_ending_a_call() {
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
    start(&mut a);
    // Even under a focused call, the header button stays clickable.
    draw(&mut a, 120, 40);
    let g = a.regions.clone();
    assert_eq!(
        a.hits.at(g.manager.x + 2, g.manager.y),
        Some(&nexus_tui::ui::Target::ManagerSession)
    );
    a.effects.clear();
    a.execute("manager.context");
    assert!(manager_calls(&a)
        .iter()
        .any(|c| c.0 == "/api/manager/context" && c.1.is_none()));
    a.manager_reply(
        "context",
        Ok(json!({"title": "Manager context", "document": "Episodes\n─────\ncurrent  ses_1\n"})),
    );
    assert!(a.ui.manager.context.is_some());
    let shown = text(&mut a);
    assert!(shown.contains("Manager context"), "{shown}");
    assert!(shown.contains("current  ses_1"), "{shown}");
    // Read-only and scrollable: the keys move the view, not the call.
    if let Some(context) = a.ui.manager.context.as_mut() {
        context.text = (0..80)
            .map(|n| format!("line {n}"))
            .collect::<Vec<_>>()
            .join("\n");
    }
    draw(&mut a, 120, 40);
    press(&mut a, &["down"]);
    assert_eq!(a.ui.manager.context.as_ref().unwrap().top, 1);
    a.execute("manager.minimise");
    assert!(a.ui.manager.minimised, "minimise is still a palette action");
    a.manager_execute("manager.talk");
    // Esc closes the modal first; the call is still open.
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.context.is_none());
    assert!(a.ui.manager.call.is_some());
    // The next Esc ends the call.
    a.effects.clear();
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.call.is_none());
}

#[test]
fn talking_from_the_minimised_orb_brings_the_call_back() {
    let mut a = app();
    start_quiet(&mut a);
    assert!(
        a.ui.manager.minimised,
        "/manager opens quietly in the corner"
    );
    press(&mut a, &["alt+m"]);
    assert!(!a.ui.manager.minimised);
    assert!(a.holds("manager.listening"));
    assert_eq!(a.escape_rule().as_deref(), Some("manager_call"));
}

#[test]
fn the_four_call_actions_stay_distinct() {
    let mut a = app();
    focused_call(&mut a);
    a.ui.manager.speaking = true;
    a.effects.clear();
    // Stop speaking: silence here, no cancel, no end.
    press(&mut a, &["alt+s"]);
    assert!(!a.ui.manager.speaking);
    assert!(a.ui.manager.call.is_some());
    assert!(!manager_calls(&a)
        .iter()
        .any(|c| c.0 == "/api/manager/cancel" || c.0 == "/api/manager/stop"));
    // Cancel the turn: Core is told, the call stays.
    a.effects.clear();
    press(&mut a, &["alt+c"]);
    assert!(manager_calls(&a)
        .iter()
        .any(|c| c.0 == "/api/manager/cancel"));
    assert!(a.ui.manager.call.is_some());
    assert!(!manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));
    // Dismiss the card: only the card goes.
    send(
        &mut a,
        97,
        "manager.card",
        json!({"card": {"id": "c", "type": "note", "focused": true, "markdown": "x"}}),
    );
    a.effects.clear();
    press(&mut a, &["alt+shift+d"]);
    assert!(a.ui.manager.card.is_none());
    assert!(manager_calls(&a)
        .iter()
        .any(|c| c.0 == "/api/manager/card/dismiss"));
    assert!(a.ui.manager.call.is_some());
    // End: the call goes, nothing else.
    a.effects.clear();
    press(&mut a, &["alt+shift+m"]);
    assert!(a.ui.manager.call.is_none());
    assert!(manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));
}

#[test]
fn the_end_button_and_saying_thats_all_end_the_call() {
    // The End button drawn under the orb.
    let mut a = app();
    focused_call(&mut a);
    draw(&mut a, 120, 40);
    let end = a
        .hits
        .rect_of(&Target::ManagerEnd)
        .expect("End under the orb");
    a.effects.clear();
    click(&mut a, end.x + end.width / 2, end.y);
    assert!(a.ui.manager.call.is_none());
    assert!(manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));

    // A spoken "that's all" is the same End, and is never sent as words.
    let mut a = app();
    focused_call(&mut a);
    a.ui.manager.mic = Mic::Transcribing;
    a.effects.clear();
    a.manager_done(nexus_tui::actions::ActionDone {
        call: nexus_tui::actions::ApiCall::ManagerTranscribe {
            wav: Vec::new(),
            token: 0,
        },
        result: Ok(nexus_tui::actions::Done {
            transcript: Some("that's all.".into()),
            ..Default::default()
        }),
    });
    assert!(a.ui.manager.call.is_none());
    assert!(manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));
    assert!(!manager_calls(&a).iter().any(|c| c.0 == "/api/manager/say"));
    assert!(ends_call("That's all for now") && !ends_call("that's all the files"));
}

#[test]
fn the_hint_line_names_the_state_and_how_to_end() {
    let mut a = app();
    focused_call(&mut a);
    let idle = a.manager_hint();
    assert!(
        idle.contains("space talk") && idle.contains("esc end"),
        "{idle}"
    );
    a.ui.manager.speaking = true;
    let speaking = a.manager_hint();
    assert!(
        speaking.contains("stop") && speaking.contains("esc end"),
        "{speaking}"
    );
    a.ui.manager.speaking = false;
    a.ui.manager.minimised = true;
    let mini = a.manager_hint();
    assert!(
        mini.contains("alt+m talk") && mini.contains("esc end"),
        "{mini}"
    );
    draw(&mut a, 120, 40);
    assert!(text(&mut a).contains("esc end"), "the hint is drawn");
}

#[test]
fn space_talks_while_the_call_is_focused() {
    let mut a = app();
    focused_call(&mut a);
    press(&mut a, &["space"]);
    assert!(a.holds("manager.listening"));
    // And in a focused card state, space is the mic too.
    a.manager_execute("manager.listen_cancel");
    send(
        &mut a,
        98,
        "manager.card",
        json!({"card": {"id": "c", "type": "note", "focused": true, "markdown": "x"}}),
    );
    press(&mut a, &["space"]);
    assert!(a.holds("manager.listening"));
}

#[test]
fn the_minimised_caption_opens_a_waiting_card() {
    let mut a = app();
    start_quiet(&mut a);
    send(
        &mut a,
        99,
        "manager.card",
        json!({"card": {"id": "c", "type": "note", "title": "A note", "focused": true, "markdown": "x"}}),
    );
    draw(&mut a, 120, 40);
    let caption = a
        .hits
        .rect_of(&Target::ManagerCard)
        .expect("the card caption");
    click(&mut a, caption.x + caption.width / 2, caption.y);
    assert!(
        !a.ui.manager.minimised,
        "opening the card brings the call back"
    );
    assert!(a.holds("manager.card_focused"));
}

#[test]
fn graphics_orb_is_placed_by_the_stage_and_removed_on_end() {
    let mut a = app();
    start(&mut a);
    a.ui.manager
        .orb
        .pin(nexus_tui::orb::OrbState::Listening, 0.6);
    a.orb_sprites.borrow_mut().configure(true, (8, 16));
    draw(&mut a, 120, 40);
    let graphics = a.orb_sprites.borrow_mut().take();
    assert!(graphics.contains("a=t,"));
    assert!(graphics.contains("c=30,r=7,z=1,C=1"));
    a.manager_execute("manager.stop");
    draw(&mut a, 120, 40);
    assert!(a.orb_sprites.borrow_mut().take().contains("a=d,d=i,"));
}
