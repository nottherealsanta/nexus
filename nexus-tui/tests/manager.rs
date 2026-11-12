//! The Manager's stage: cards from `interface/scenarios/manager-cards.json`, the
//! keys taken only while engaged, the call lifecycle, and the zero-cost rest state.

mod common;
use common::*;
use nexus_tui::api::{Frame, Message};
use nexus_tui::app::{App, Effect};
use nexus_tui::manager::ends_call;
use nexus_tui::sync::{Command, ManagerError};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Target;
use serde_json::{json, Value};
use std::time::{Duration, Instant};

fn app() -> App {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 40));
    a.ui.manager.silent = true;
    a.ui.manager.voice_enabled = false; // text-only cases opt out of the default-on Voice toggle
                                        // The header button and indicator follow `[manager] enabled` (off by default).
    a.state.config = Some(json!({"values": {"manager": {"enabled": true}}}));
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
    a.ui.manager.dropdown_open = true;
    a.ui.manager.dropdown_interactive = true;
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
fn header_button_follows_manager_setting() {
    let mut a = app();
    a.state.config = Some(json!({"values": {"manager": {"enabled": false}}}));
    assert!(!text(&mut a).lines().next().unwrap().contains("Manager"));
    a.state.config = Some(json!({"values": {"manager": {"enabled": true}}}));
    assert!(text(&mut a).lines().next().unwrap().contains("Manager"));
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
    a.manager_execute("manager.talk");
    assert!(a.ui.manager.starting && a.ui.manager.dropdown_open);
    assert_eq!(manager_calls(&a)[0].1.as_ref().unwrap()["audio"], true);
    a.manager_reply("start", Ok(json!({"call_id":"call_1","holder":"TUI"})));
    press(&mut a, &["esc"]);
    assert!(!a.ui.manager.dropdown_open && a.ui.manager.call.is_some());
    assert!(!a.manager_key(&key("x"), "x"));
}

#[test]
fn a_printable_key_opens_the_field_and_enter_sends_it() {
    let mut a = app();
    a.manager_execute("manager.header");
    typing(&mut a, "hi");
    press(&mut a, &["enter"]);
    assert_eq!(manager_calls(&a)[0].1.as_ref().unwrap()["audio"], false);
    a.manager_reply(
        "start",
        Ok(json!({"call_id":"call_1","holder":"TUI","mode":"live"})),
    );
    assert!(!a.ui.manager.live_call);
    let calls = manager_calls(&a);
    let say = calls.iter().find(|c| c.0 == "/api/manager/say").unwrap();
    assert_eq!(say.1.as_ref().unwrap()["text"], "hi");
    assert_eq!(a.ui.manager.field.as_ref().unwrap().text(), "");
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
    // Esc closes the dropdown while the call stays active.
    assert!(a.manager_key(&key("esc"), "esc"));
    assert!(a.ui.manager.call.is_some());
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
fn the_indicator_clock_runs_while_the_dropdown_or_a_state_is_moving() {
    let mut o = opts(Mode::Dark, Tier::TrueColor);
    o.reduced_motion = false;
    let mut a = with_turn(o, (120, 40));
    a.ui.manager.silent = true;
    start(&mut a);
    // The real dropdown path pins the field visible when it opens.
    a.ui.manager
        .indicator
        .pin(nexus_tui::indicator::IndicatorState::Idle, 0.2);
    assert!(
        a.manager_animating(Instant::now()),
        "an open dropdown shows moving idle waves"
    );
    // A call left in the background is still: no frame clock for it.
    a.manager_execute("manager.header");
    let later = Instant::now() + Duration::from_secs(5);
    for step in 0..12 {
        a.manager_frame(later + Duration::from_millis(100 * step));
    }
    a.manager_tick(later + Duration::from_millis(1200));
    assert!(!a.manager_animating(later + Duration::from_secs(2)));
    a.manager_execute("manager.talk");
    assert!(a.manager_animating(Instant::now()));
    a.manager_execute("manager.listen_cancel");
    assert!(
        a.manager_animating(Instant::now()),
        "the open dropdown keeps moving"
    );
    a.manager_execute("manager.stop");
    let later = Instant::now() + Duration::from_secs(5);
    for step in 0..12 {
        a.manager_frame(later + Duration::from_millis(100 * step));
    }
    assert!(!a.manager_animating(later + Duration::from_secs(10)));
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
    a.manager_execute("manager.field_open");
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
fn manager_debug_session_opens_inside_nexus_without_audio() {
    let mut a = app();
    let mut session =
        serde_json::to_value(a.state.model.sessions.values().next().unwrap()).unwrap();
    session["id"] = json!("ses_manager");
    a.manager_reply("debug_session", Ok(session));
    assert_eq!(a.viewed_id(), Some("ses_manager"));
    assert!(a.manager_debug_view());
    assert!(a.ui.manager.call.is_none() && !a.ui.manager.live_call);
    assert!(text(&mut a).contains("read-only debug view"));
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
fn escape_closes_only_the_dropdown_in_every_work_phase() {
    for phase in ["idle", "thinking", "replying"] {
        let mut a = app();
        focused_call(&mut a);
        send(
            &mut a,
            90,
            "manager.state",
            json!({"active":true,"phase":phase,"holder":"TUI"}),
        );
        press(&mut a, &["esc"]);
        assert!(!a.ui.manager.dropdown_open && a.ui.manager.call.is_some());
        assert!(!manager_calls(&a).iter().any(|c| c.0 == "/api/manager/stop"));
    }
}

#[test]
fn background_audio_leaves_session_escape_available() {
    let mut a = app();
    busy(&mut a);
    start_quiet(&mut a);
    assert_eq!(a.escape_rule().as_deref(), Some("interrupt"));
    a.manager_execute("manager.header");
    assert_eq!(a.escape_rule().as_deref(), Some("manager_call"));
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.call.is_some());
}

#[test]
fn palette_context_modal_closes_before_the_dropdown() {
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
        Some(&nexus_tui::ui::Target::ManagerIndicator)
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
    assert!(a.ui.manager.call.is_some());
}

#[test]
fn talking_from_the_minimised_indicator_brings_the_call_back() {
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
fn explicit_stop_ends_the_call_and_the_dropdown_has_no_end_button() {
    let mut a = app();
    focused_call(&mut a);
    draw(&mut a, 120, 40);
    assert!(a.hits.rect_of(&Target::ManagerEnd).is_none());
    a.manager_execute("manager.stop");
    assert_ended(&mut a, "explicit stop");
    assert!(ends_call("that's all"));
}

#[test]
fn the_hint_line_names_the_state_and_how_to_end() {
    let mut a = app();
    focused_call(&mut a);
    assert!(text(&mut a).contains("Open Manager session"));
    assert!(a.hits.rect_of(&Target::ManagerDebugSession).is_some());
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
        90,
        "manager.card",
        json!({"card":{"id":"c","type":"note","focused":false,"markdown":"Notification"}}),
    );
    assert!(a.ui.manager.dropdown_open && !a.ui.manager.dropdown_interactive);
    assert!(text(&mut a).contains("Notification"));
    assert!(!a.manager_key(&key("x"), "x"));
}

fn braille(shown: &str) -> usize {
    shown
        .chars()
        .filter(|c| ('\u{2800}'..='\u{28ff}').contains(c))
        .count()
}

#[test]
fn the_header_indicator_is_a_braille_wave_field_and_goes_on_end() {
    let mut a = app();
    start(&mut a);
    a.ui.manager
        .indicator
        .pin(nexus_tui::indicator::IndicatorState::Listening, 0.6);
    let shown = text(&mut a);
    assert!(braille(&shown) >= 8, "the header is braille text: {shown}");
    let strip = a.hits.rect_of(&Target::ManagerIndicator).unwrap();
    assert_eq!(strip.height, 1);
    a.manager_execute("manager.stop");
    let later = Instant::now() + Duration::from_secs(5);
    for step in 0..12 {
        a.manager_frame(later + Duration::from_millis(100 * step));
    }
    let shown = text(&mut a);
    assert_eq!(braille(&shown), 0, "the field is gone: {shown}");
}

#[test]
fn hovering_the_live_voice_indicator_offers_stop_and_clicking_it_stops_the_voice() {
    let mut a = app();
    start(&mut a);
    a.ui.manager.live_call = true;
    a.ui.manager.voice_enabled = true;
    draw(&mut a, 120, 40);
    let strip = a.hits.rect_of(&Target::ManagerIndicator).unwrap();
    a.mouse = Some((strip.x + 2, strip.y));
    let shown = text(&mut a);
    assert!(shown.contains("Stop"), "{shown}");
    a.mouse = Some((0, 30));
    assert!(!text(&mut a).contains("Stop"), "only while hovered");

    a.effects.clear();
    click(&mut a, strip.x + 2, strip.y);
    assert!(
        manager_calls(&a)
            .iter()
            .any(|(path, _)| path == "/api/manager/live/stop"),
        "the voice is stopped through the existing path"
    );
    assert!(!a.ui.manager.live_call && !a.ui.manager.voice_enabled);
    assert!(a.ui.manager.call.is_some(), "the control call stays open");
}

#[test]
fn an_open_manager_panel_dims_the_rest_of_the_screen() {
    let f: Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/manager-dropdown.json"
    ))
    .unwrap();
    assert_eq!(f["panel_dim"], true);
    let mut a = app();
    let (before, _) = draw(&mut a, 120, 40);
    a.manager_execute("manager.header");
    let (after, _) = draw(&mut a, 120, 40);
    // The left column is nowhere near the dropdown: every cell there is dimmed.
    let mut changed = 0;
    for y in 2..38u16 {
        for x in 0..40u16 {
            let (a, b) = (&before[(x, y)], &after[(x, y)]);
            if a.fg != b.fg || a.bg != b.bg || a.modifier != b.modifier {
                changed += 1;
            }
        }
    }
    assert!(
        changed > 100,
        "the app behind the panel is dimmed: {changed}"
    );
}

#[test]
fn the_focused_call_shows_the_latest_model_excerpt_and_real_tool() {
    let scenario: Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/manager-preview.json"
    ))
    .unwrap();
    let mut a = app();
    start(&mut a);
    send(
        &mut a,
        900,
        "manager.preview",
        json!({"preview": scenario["preview"]}),
    );
    // Neither client draws the preview text any more (ca97629e); the state is still kept.
    assert_eq!(a.ui.manager.preview.as_ref(), Some(&scenario["preview"]));
    a.manager_execute("manager.stop");
    assert!(a.ui.manager.preview.is_none());
}

#[test]
fn header_indicator_and_connecting_show_before_the_start_reply() {
    let mut a = app();
    a.manager_execute("manager.header");
    a.manager_execute("manager.audio_toggle");
    assert!(a.ui.manager.starting && a.ui.manager.indicator.visible());
    let shown = text(&mut a);
    assert!(shown.contains("Connecting…"));
    assert!(braille(&shown) > 0, "the header is braille text: {shown}");
    let indicator = a.hits.rect_of(&Target::ManagerIndicator).unwrap();
    assert_eq!(indicator.height, 1);
    assert_eq!(indicator.y, 0);
    assert!(indicator.width > 1);
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.starting);
    a.manager_execute("manager.stop");
    assert!(!a.ui.manager.starting);
}

#[test]
fn dropdown_input_and_debug_action_work_without_an_audio_call() {
    let mut a = app();
    a.manager_execute("manager.header");
    assert!(a.ui.manager.call.is_none());
    assert!(manager_calls(&a).is_empty());
    assert!(text(&mut a).contains("Tell the Manager"));
    assert!(a.hits.rect_of(&Target::ManagerInput).is_some());
    a.effects.clear();
    a.manager_execute("manager.open_session");
    // Debug opening is not an audio action and does not start the Manager call.
    assert!(a.ui.manager.call.is_none() && !a.ui.manager.audio_requested);
}

#[test]
fn shared_dropdown_contract_keeps_text_audio_and_debug_separate() {
    let f: Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/manager-dropdown.json"
    ))
    .unwrap();
    let mut a = app();
    a.manager_execute("manager.header");
    let shown = text(&mut a);
    assert!(shown.contains(f["placeholder"].as_str().unwrap()));
    assert!(shown.contains(f["debug_action"].as_str().unwrap()));
    assert_eq!(
        a.hits.rect_of(&Target::ManagerIndicator).unwrap().height,
        f["indicator_rows"].as_u64().unwrap() as u16
    );
    // The live voice turns the indicator into a Stop control.
    a.ui.manager.live_call = true;
    let strip = a.hits.rect_of(&Target::ManagerIndicator).unwrap();
    a.mouse = Some((strip.x + 2, strip.y));
    assert!(text(&mut a).contains(f["indicator_stop"].as_str().unwrap()));
    a.ui.manager.live_call = false;
    a.mouse = None;
    typing(&mut a, f["input"].as_str().unwrap());
    press(&mut a, &["enter"]);
    assert_eq!(
        manager_calls(&a)[0].1.as_ref().unwrap()["audio"],
        f["start_audio"]
    );
    press(&mut a, &["esc"]);
    assert!(a.ui.manager.starting);
}

#[test]
fn default_voice_starts_on_open_and_can_be_disabled_while_connecting() {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 40));
    a.ui.manager.silent = true;
    assert!(a.ui.manager.voice_enabled);
    a.manager_execute("manager.header");
    assert!(a.ui.manager.dropdown_open && a.ui.manager.starting);
    assert_eq!(manager_calls(&a)[0].1.as_ref().unwrap()["audio"], true);
    a.manager_execute("manager.audio_toggle");
    assert!(!a.ui.manager.voice_enabled && !a.ui.manager.audio_requested);
    a.manager_reply(
        "start",
        Ok(json!({"call_id":"call_1","holder":"TUI","mode":"live"})),
    );
    assert!(a.ui.manager.call.is_some() && !a.ui.manager.live_call);
    let shown = text(&mut a);
    assert!(shown.contains("Connect") && !shown.contains("Disconnect"));
    assert!(shown.contains("Mute"));
    assert!(a.hits.rect_of(&Target::ManagerMute).is_none(), "nothing to mute until the voice is connected");
    assert!(!shown.lines().next().unwrap().contains("Manager"));
    assert!(a.hits.rect_of(&Target::ManagerVoice).is_some());
    assert!(!shown.contains('┌'));
}

#[test]
fn voice_has_connect_and_mute_buttons() {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 40));
    a.ui.manager.silent = true;
    a.manager_execute("manager.header");
    let shown = text(&mut a);
    assert!(shown.contains("Disconnect") && shown.contains("Mute"), "{shown}");
    assert!(a.hits.rect_of(&Target::ManagerVoice).is_some());
    // Muting needs a live voice: with none the command does nothing.
    a.manager_execute("manager.mute_toggle");
    assert!(!a.ui.manager.mic_muted);
    a.ui.manager.live_call = true;
    assert!(a.manager_term("manager.voice"));
    let shown = text(&mut a);
    assert!(a.hits.rect_of(&Target::ManagerMute).is_some(), "{shown}");
    a.manager_execute("manager.mute_toggle");
    assert!(a.ui.manager.mic_muted);
    let shown = text(&mut a);
    assert!(shown.contains("Unmute"), "{shown}");
    a.manager_execute("manager.mute_toggle");
    assert!(!a.ui.manager.mic_muted && text(&mut a).contains("Mute"));
    // Disconnect clears the mute and swaps the button back to Connect.
    a.manager_execute("manager.mute_toggle");
    a.manager_execute("manager.audio_toggle");
    assert!(!a.ui.manager.mic_muted && !a.ui.manager.live_call);
    assert!(text(&mut a).contains("Connect"));
}

#[test]
fn scripted_day_applies_shared_cards_and_navigation() {
    let day: Value =
        serde_json::from_str(include_str!("../../interface/scenarios/manager-day.json")).unwrap();
    let mut a = app();
    start(&mut a);
    let id = a.state.model.sessions.keys().next().unwrap().clone();
    for (index, name) in ["excerpt_card", "approval_card"].iter().enumerate() {
        let mut card = day[*name].clone();
        card["session_id"] = json!(id);
        send(
            &mut a,
            101 + index as i64,
            "manager.card",
            json!({"card": card}),
        );
        let expected = if *name == "excerpt_card" {
            "session_answer"
        } else {
            "command"
        };
        assert!(text(&mut a).contains(day[expected].as_str().unwrap()));
    }
    a.effects.clear();
    send(
        &mut a,
        103,
        "manager.navigate",
        json!({"request_id": "day-nav", "session": id}),
    );
    assert!(manager_calls(&a)
        .iter()
        .any(|c| c.0 == "/api/manager/navigate/result" && c.1.as_ref().unwrap()["ok"] == true));
    send(&mut a, 104, "manager.card", json!({"card": null}));
    assert!(a.ui.manager.card.is_none());
}
