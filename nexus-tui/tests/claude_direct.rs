mod common;
use common::*;
use nexus_tui::app::{PageAction, PageLine};
use nexus_tui::theme::{Mode, Tier};
use serde_json::Value;

#[test]
fn shared_direct_provider_note_and_actions_are_data_driven() {
    let scenario: Value = serde_json::from_str(include_str!("../../interface/scenarios/claude-direct.json")).unwrap();
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.state.providers = vec![scenario["provider"].clone()];
    let lines = app.provider_detail_lines("claude-direct");
    for note in scenario["provider"]["note"].as_str().unwrap().lines() {
        assert!(lines.iter().any(|line| matches!(line, PageLine::Text(text, _) if text == note)));
    }
    assert!(lines.iter().any(|line| matches!(line, PageLine::Item { action: PageAction::Disconnect(id), .. } if id == "claude-direct")));
    app.state.providers[0]["connected"] = false.into();
    let lines = app.provider_detail_lines("claude-direct");
    assert!(lines.iter().any(|line| matches!(line, PageLine::Item { label, .. } if label == "Sign in with browser")));
    assert!(lines.iter().any(|line| matches!(line, PageLine::Item { label, .. } if label == "Sign in with browser (1-year token)")));
    assert!(lines.iter().any(|line| matches!(line, PageLine::Item { label, .. } if label == "Use signed-in Claude Code profile (read-only)")));
    app.execute("app.sign_in");
    app.ui.provider_detail = Some("claude-direct".into());
    press(&mut app, &["esc"]);
    assert!(app.ui.provider_detail.is_none());
}
