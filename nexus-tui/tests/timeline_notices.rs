//! Plan 35: single-item icons and the `Subagent done` notice, from the shared
//! `interface/scenarios/subagent-done.json` that NSH's scenarios test also loads.

mod common;
use common::*;

use nexus_tui::api::{History, Message, Session};
use nexus_tui::app::App;
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Focus;
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
    for value in scenario["sessions"].as_array().into_iter().flatten() {
        let mut value = value.clone();
        if value["parent_id"].is_string() {
            value["parent_id"] = SESSION.into();
        }
        let session: Session = serde_json::from_value(value).unwrap();
        app.state.model.sessions.insert(session.id.clone(), session);
    }
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
fn subagent_done_notice_and_single_item_icons() {
    let (scenario, mut app) = open(include_str!("../../interface/scenarios/subagent-done.json"));
    let shown = draw(&mut app, 160, 50).1.join("\n");
    for fact in strings(&scenario["expect"]) {
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
    for fact in strings(&scenario["absent"]) {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
    // A single finished shell shows the verb icon where the fold would be.
    assert!(shown.contains("$ Background Shell Done"), "{shown}");
    // The structured notice opens the modal, whose footer opens the child.
    let key = format!("notice:{}", scenario["notice"].as_str().unwrap());
    let index = app
        .blocks()
        .iter()
        .position(|b| b.key == key)
        .unwrap_or_else(|| panic!("no {key} block in {shown}"));
    app.ui.timeline_sel = Some(index);
    app.ui.timeline_sel_key = Some(key);
    app.ui.focus = Focus::Timeline;
    press(&mut app, &["enter"]);
    let modal = draw(&mut app, 160, 50).1.join("\n");
    for fact in strings(&scenario["expect_modal"]) {
        assert!(modal.contains(fact), "missing {fact:?} in modal:\n{modal}");
    }
    assert!(modal.contains("enter open session"), "{modal}");
    press(&mut app, &["enter"]);
    assert_eq!(
        app.state
            .model
            .selected
            .as_ref()
            .map(|s| s.session_id.as_str()),
        Some("ses_child")
    );
    assert!(
        app.ui.overlay.is_none(),
        "opening the child closes the modal"
    );
}
