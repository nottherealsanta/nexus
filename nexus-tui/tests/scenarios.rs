//! Shared scenarios: `interface/scenarios/*.json`, which NSH's `scenarios.test.tsx` also loads.

mod common;
use common::*;

use nexus_tui::api::{History, Message};
use nexus_tui::app::App;
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Options, Theme, Tier};
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

#[test]
fn mermaid() {
    let (scenario, mut app) = open(include_str!("../../interface/scenarios/mermaid.json"));
    let shown = draw(&mut app, 200, 120).1.join("\n");
    for fact in strings(&scenario["expect"]) {
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
    for fact in strings(&scenario["absent"]) {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
    // The flowchart is the shared PNG; the pie is the shared text renderer.
    let text = scenario["messages"][1]["parts"][0]["text"]
        .as_str()
        .unwrap();
    let palette = nexus_tui::markdown::Palette::from_theme(&Theme::new(Options::default()));
    let rows = nexus_tui::markdown::lines(text, &palette, 80);
    assert!(
        rows.iter().any(|row| row.image.is_some()),
        "the flowchart did not reserve an image row"
    );
    assert!(
        rows.iter().any(|row| row
            .spans
            .iter()
            .any(|span| span.content.contains("Traffic"))),
        "the pie did not fall back to text rows"
    );
}

#[test]
fn zmx_child_card_discloses_the_incomplete_context_and_hides_nexus_prompt() {
    let (scenario, mut app) = open(include_str!("../../interface/scenarios/zmx-child.json"));
    let session = app.state.model.sessions.get_mut(SESSION).unwrap();
    for (key, value) in scenario["session"].as_object().unwrap() {
        session.extra.insert(key.clone(), value.clone());
    }
    app.state.model.selected.as_mut().unwrap().starting = Some(scenario["starting"].clone());
    let shown = draw(&mut app, 160, 50).1.join("\n");
    for fact in strings(&scenario["expect"]) {
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
    for fact in strings(&scenario["absent"]) {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
}

#[test]
fn zmx_models_never_reach_the_root_model_picker() {
    use nexus_tui::ui::PaletteMode;
    let (scenario, mut app) = open(include_str!("../../interface/scenarios/zmx-child.json"));
    app.state.models = scenario["models"].as_array().unwrap().clone();
    app.open_palette(PaletteMode::Models);
    let shown = draw(&mut app, 160, 50).1.join("\n");
    assert!(shown.contains("GPT 6"), "{shown}");
    for fact in strings(&scenario["root_picker_absent"]) {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
    app.open_palette(PaletteMode::PoolModels);
    assert!(draw(&mut app, 160, 50)
        .1
        .join("\n")
        .contains("agy/gemini-3.8-flash-high"));
}
