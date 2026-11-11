mod common;
use common::*;
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::api::{Event, Message};
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};
use serde_json::Value;

fn scenario() -> Value {
    serde_json::from_str(include_str!(
        "../../interface/scenarios/session-status-btw.json"
    ))
    .unwrap()
}

fn app() -> nexus_tui::app::App {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    let data = scenario();
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = data["messages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| {
            let mut m: Message = serde_json::from_value(m.clone()).unwrap();
            m.session_id = SESSION.into();
            m
        })
        .collect();
    view.turns = vec![serde_json::from_value(data["turn"].clone()).unwrap()];
    view.scratch.clear();
    a.state.model.running.clear();
    a.state.model.streaming.clear();
    a
}

#[test]
fn status_is_not_context_and_clears_when_busy() {
    let mut a = app();
    let data = scenario();
    let timeline = nexus_tui::timeline::build(&a, &a.theme, 0, 160);
    let text = timeline
        .lines
        .iter()
        .map(|l| {
            l.spans
                .iter()
                .map(|s| s.content.as_ref())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n");
    for fact in data["timeline_absent"].as_array().unwrap() {
        assert!(!text.contains(fact.as_str().unwrap()), "{text}");
    }
    let shown = draw(&mut a, 160, 50).1.join("\n");
    for fact in data["status_expect"].as_array().unwrap() {
        assert!(shown.contains(fact.as_str().unwrap()), "{shown}");
    }
    a.state
        .model
        .running
        .insert(SESSION.into(), "running".into());
    assert!(!draw(&mut a, 160, 50)
        .1
        .join("\n")
        .contains("Interrupted by the user"));
    a.state.model.running.clear();
    assert!(draw(&mut a, 160, 50)
        .1
        .join("\n")
        .contains("Interrupted by the user"));
}

#[test]
fn side_question_stream_and_inherited_context_are_separate_from_the_parent() {
    let mut a = app();
    let before = a.messages().to_vec();
    typing(&mut a, "/btw Which port?");
    press(&mut a, &["enter"]);
    let request_id = a.ui.btw.as_ref().unwrap().request_id.clone();
    a.apply_done(ActionDone {
        call: ApiCall::Btw {
            session: SESSION.into(),
            question: "Which port?".into(),
            request_id,
        },
        result: Ok(Done {
            answer: Some("ses_btw".into()),
            btw_context: Some("inherited prefix proof".into()),
            ..Done::default()
        }),
    });
    for e in scenario()["events"].as_array().unwrap() {
        let event = Event {
            id: e["id"].as_i64().unwrap_or(0),
            cursor: None,
            seq: None,
            time: 3.0,
            kind: e["type"].as_str().unwrap().into(),
            session_id: Some("ses_btw".into()),
            data: e["data"].clone(),
        };
        a.apply_sync(SyncMsg::Event(event.clone()));
        a.apply_sync(SyncMsg::Event(event));
    }
    let shown = draw(&mut a, 160, 50).1.join("\n");
    for fact in scenario()["btw_expect"].as_array().unwrap() {
        assert!(shown.contains(fact.as_str().unwrap()), "{shown}");
    }
    assert!(!shown.contains("inherited prefix proof"));
    assert_eq!(a.ui.btw.as_ref().unwrap().messages.len(), 4);
    press(&mut a, &["i"]);
    assert!(draw(&mut a, 160, 50)
        .1
        .join("\n")
        .contains("inherited prefix proof"));
    assert_eq!(a.messages(), before.as_slice());
    press(&mut a, &["esc"]);
    assert!(a.ui.btw.is_none());
    assert_eq!(a.messages(), before.as_slice());
}

#[test]
fn closing_cancels_only_the_side_run_and_late_start_responses_are_cancelled() {
    let mut a = app();
    typing(&mut a, "/btw first");
    press(&mut a, &["enter"]);
    let first = a.ui.btw.as_ref().unwrap().request_id.clone();
    press(&mut a, &["esc"]);
    typing(&mut a, "/btw second");
    press(&mut a, &["enter"]);
    a.apply_done(ActionDone {
        call: ApiCall::Btw {
            session: SESSION.into(),
            question: "first".into(),
            request_id: first,
        },
        result: Ok(Done {
            answer: Some("ses_old".into()),
            ..Done::default()
        }),
    });
    assert!(a.ui.btw.as_ref().unwrap().session.is_none());
    assert!(a.effects.iter().any(|e| matches!(e, nexus_tui::app::Effect::Api(ApiCall::Interrupt { session }) if session == "ses_old")));
    a.ui.btw.as_mut().unwrap().session = Some("ses_new".into());
    press(&mut a, &["esc"]);
    assert!(a.effects.iter().any(|e| matches!(e, nexus_tui::app::Effect::Api(ApiCall::Interrupt { session }) if session == "ses_new")));
}

#[test]
fn continuation_navigation_is_gone_but_continue_and_origin_remain() {
    let a = app();
    assert!(a.manifest.command("session.open_continuation").is_none());
    assert!(a.manifest.command("session.continue").is_some());
    assert!(a.manifest.command("session.open_origin").is_some());
}
