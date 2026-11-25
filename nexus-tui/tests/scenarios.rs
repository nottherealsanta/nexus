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

/// A shared ZMX child scenario loaded into the selected session, before any drawing.
fn zmx_loaded(data: &str) -> (Value, App) {
    let (scenario, mut app) = open(data);
    let session = app.state.model.sessions.get_mut(SESSION).unwrap();
    for (key, value) in scenario["session"].as_object().unwrap() {
        session.extra.insert(key.clone(), value.clone());
    }
    app.state.model.selected.as_mut().unwrap().starting = Some(scenario["starting"].clone());
    (scenario, app)
}

/// A shared ZMX child scenario: the session's `worker`, the frozen starting context, what shows and what must not.
fn zmx_scenario(data: &str) {
    let (scenario, mut app) = zmx_loaded(data);
    let shown = draw(&mut app, 160, 50).1.join("\n");
    for fact in strings(&scenario["expect"]) {
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
    for fact in strings(&scenario["absent"]) {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
    if scenario.get("expect_open").is_some() {
        // High detail opens every tool card, so the nested subagent and the tool outputs show.
        app.set_detail(nexus_tui::ui::Detail::High);
        app.execute("turn.unfold_all");
        let shown = draw(&mut app, 160, 50).1.join("\n");
        for fact in strings(&scenario["expect_open"]) {
            assert!(
                shown.contains(fact),
                "missing {fact:?} when open in:\n{shown}"
            );
        }
    }
}

#[test]
fn zmx_parallel_tools_and_claudes_own_subagent_nested_in_its_call() {
    zmx_scenario(include_str!(
        "../../interface/scenarios/zmx-parallel-tools.json"
    ));
}

#[test]
fn zmx_denied_action_shows_what_the_cli_refused() {
    zmx_scenario(include_str!("../../interface/scenarios/zmx-denial.json"));
}

#[test]
fn zmx_acp_denial_says_nexus_denied_it() {
    zmx_scenario(include_str!(
        "../../interface/scenarios/zmx-acp-denial.json"
    ));
}

#[test]
fn zmx_needs_attention_names_the_manual_step() {
    zmx_scenario(include_str!(
        "../../interface/scenarios/zmx-needs-attention.json"
    ));
}

#[test]
fn zmx_interrupted_turn_keeps_the_unknown_tool_outcome() {
    zmx_scenario(include_str!("../../interface/scenarios/zmx-interrupt.json"));
}

#[test]
fn zmx_recovered_turn_says_the_cli_may_still_be_running() {
    zmx_scenario(include_str!("../../interface/scenarios/zmx-recovered.json"));
}

/// A diagram cut by the top of the viewport (or the sticky user band) used to be
/// drawn from the first timeline row with its full height, so it ran past the
/// rows reserved for it and hid the prose below. Whatever the height, the text
/// after the diagram must stay whole.
#[test]
fn a_clipped_mermaid_diagram_never_covers_the_text_after_it() {
    let text = "Intro.\n\n```mermaid\nflowchart TD\n  Client --> Core --> Store\n  Core --> Cache\n```\n\nTAIL-ONE the cache keys on mtime\n\nTAIL-TWO and the test checks the hit.\n";
    let scenario = serde_json::json!({
        "messages": [
            { "id": "m1", "seq": 1, "created": 1, "role": "user", "parts": [{ "type": "text", "text": "Draw it.\nAnd more." }] },
            { "id": "m2", "seq": 2, "created": 2, "role": "assistant", "parts": [{ "type": "text", "text": text }] },
        ]
    });
    let (_, mut app) = open(&scenario.to_string());
    for height in 12..=40u16 {
        let rows = draw(&mut app, 120, height).1;
        let shown = rows.join("\n");
        for tail in [
            "TAIL-ONE the cache keys on mtime",
            "TAIL-TWO and the test checks the hit.",
        ] {
            let row = rows.iter().find(|row| row.contains(tail));
            if height >= 26 {
                assert!(
                    row.is_some(),
                    "{tail:?} hidden at height {height}:\n{shown}"
                );
            }
            if let Some(row) = row {
                assert!(
                    !row.contains('▀') && !row.contains('▄') && !row.contains('█'),
                    "diagram cells on the text row at height {height}:\n{shown}"
                );
            }
        }
    }
}
