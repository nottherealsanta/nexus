//! Shared scenarios: `interface/scenarios/*.json`, which NSH's `scenarios.test.tsx` also loads.

mod common;
use common::*;

use nexus_tui::api::{History, Message};
use nexus_tui::app::App;
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};
use serde_json::Value;

fn open(data: &str) -> (Value, App) {
    let scenario: Value = serde_json::from_str(data).unwrap();
    let messages: Vec<Message> = serde_json::from_value::<Vec<Value>>(scenario["messages"].clone())
        .unwrap()
        .into_iter()
        .map(|mut m| {
            m["session_id"] = SESSION.into();
            serde_json::from_value(m).unwrap()
        })
        .collect();
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.open_session(SESSION);
    app.effects.clear();
    app.apply_sync(SyncMsg::History(Box::new(History {
        session_id: SESSION.into(),
        messages,
        ..Default::default()
    })));
    (scenario, app)
}

fn strings(v: &Value) -> Vec<&str> {
    v.as_array()
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

#[test]
fn activity_group() {
    let (scenario, mut app) = open(include_str!(
        "../../interface/scenarios/activity-group.json"
    ));
    let shown = draw(&mut app, 160, 50).1.join("\n");
    for fact in strings(&scenario["expect"]) {
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
    for fact in strings(&scenario["absent"]) {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
    app.execute("turn.unfold_all");
    let shown = draw(&mut app, 160, 50).1.join("\n");
    for fact in strings(&scenario["expect_open"]) {
        assert!(
            shown.contains(fact),
            "missing {fact:?} when open in:\n{shown}"
        );
    }
}

#[test]
fn collapsed_paste() {
    let (scenario, mut app) = open(include_str!(
        "../../interface/scenarios/collapsed-paste.json"
    ));
    let shown = draw(&mut app, 160, 50).1.join("\n");
    for fact in strings(&scenario["expect"]) {
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
    for fact in strings(&scenario["absent"]) {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
}
