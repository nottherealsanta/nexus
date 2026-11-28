mod common;
use common::*;
use nexus_tui::app::{PageAction, PageLine};
use nexus_tui::theme::{Mode, Tier};
use serde_json::Value;

#[test]
fn shared_direct_model_name_has_no_redundant_prefix_in_composer() {
    let scenario: Value =
        serde_json::from_str(include_str!("../../interface/scenarios/claude-direct.json")).unwrap();
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    app.state.model.sessions.get_mut(SESSION).unwrap().model =
        Some(scenario["model"]["ref"].as_str().unwrap().into());
    app.state.models = vec![scenario["model"].clone()];
    let (_, rows) = draw(&mut app, 160, 48);
    let all = rows.join("\n");
    assert!(all.contains("Claude Haiku 4.5 claude-direct"), "{all}");
    assert!(!all.contains("Direct · Claude Haiku"), "{all}");
}

#[test]
fn direct_compatibility_instruction_is_visible_in_system_prompt() {
    use nexus_tui::sync::SyncMsg;
    use nexus_tui::ui::Target;
    let scenario: Value =
        serde_json::from_str(include_str!("../../interface/scenarios/claude-direct.json")).unwrap();
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    let history = nexus_tui::api::History {
        session_id: SESSION.into(),
        messages: app.messages().to_vec(),
        starting: Some(scenario["starting"].clone()),
        ..Default::default()
    };
    app.apply_sync(SyncMsg::History(Box::new(history)));
    draw(&mut app, 160, 48);
    app.click(Target::ContextSection("System prompt".into()));
    let (_, rows) = draw(&mut app, 160, 48);
    let shown = rows.join("\n");
    assert!(
        shown.contains("You are Claude Code, Anthropic's official CLI for Claude."),
        "{shown}"
    );
    assert!(
        shown.contains("You are an AI agent running in Nexus."),
        "{shown}"
    );
}

#[test]
fn shared_direct_provider_note_and_actions_are_data_driven() {
    let scenario: Value =
        serde_json::from_str(include_str!("../../interface/scenarios/claude-direct.json")).unwrap();
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.state.providers = vec![scenario["provider"].clone()];
    let lines = app.provider_detail_lines("claude-direct");
    for note in scenario["provider"]["note"].as_str().unwrap().lines() {
        assert!(lines
            .iter()
            .any(|line| matches!(line, PageLine::Text(text, _) if text == note)));
    }
    assert!(lines.iter().any(|line| matches!(line, PageLine::Item { action: PageAction::Disconnect(id), .. } if id == "claude-direct")));
    app.state.providers[0]["connected"] = false.into();
    let lines = app.provider_detail_lines("claude-direct");
    assert!(lines.iter().any(
        |line| matches!(line, PageLine::Item { label, .. } if label == "Sign in with browser")
    ));
    assert!(lines.iter().any(|line| matches!(line, PageLine::Item { label, .. } if label == "Sign in with browser (1-year token)")));
    assert!(lines.iter().any(|line| matches!(line, PageLine::Item { label, .. } if label == "Use signed-in Claude Code profile (read-only)")));
    app.execute("app.sign_in");
    app.ui.provider_detail = Some("claude-direct".into());
    press(&mut app, &["esc"]);
    assert!(app.ui.provider_detail.is_none());
}
