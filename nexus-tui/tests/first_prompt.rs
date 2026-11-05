mod common;

use common::*;
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::app::{App, Effect};
use nexus_tui::state::ConnectionState;
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};

fn scenario() -> serde_json::Value {
    serde_json::from_str(include_str!("../../interface/scenarios/first-prompt.json")).unwrap()
}

fn sending() -> (App, ApiCall) {
    let s = scenario();
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (170, 50));
    a.ui.project = a.current_project();
    a.run_command("session.new");
    a.ui.draft.agent = Some(s["agent"].as_str().unwrap().into());
    a.ui.draft.model = Some(s["model"].as_str().unwrap().into());
    a.effects.clear();
    a.composer().set_text(s["text"].as_str().unwrap());
    press(&mut a, &["enter"]);
    let call = a
        .effects
        .iter()
        .find_map(|e| match e {
            Effect::Api(c @ ApiCall::NewAndPrompt { .. }) => Some(c.clone()),
            _ => None,
        })
        .unwrap();
    (a, call)
}

fn created(a: &mut App, call: ApiCall) -> ApiCall {
    let mut session = a.state.model.sessions[SESSION].clone();
    session.id = "ses_instant".into();
    session.agent = "plan".into();
    session.model = Some("codex/gpt-5".into());
    a.apply_done(ActionDone {
        call,
        result: Ok(Done {
            open: Some(session.id.clone()),
            session: Some(session),
            ..Done::default()
        }),
    });
    a.effects
        .iter()
        .find_map(|e| match e {
            Effect::Api(c @ ApiCall::Prompt { .. }) => Some(c.clone()),
            _ => None,
        })
        .unwrap()
}

#[test]
fn first_prompt_is_visible_before_any_network_reply_and_keeps_its_header() {
    let (mut a, call) = sending();
    let s = scenario();
    assert!(
        a.messages().is_empty(),
        "pending sends are not model context"
    );
    assert!(a.composer_ref().is_empty());
    for stage in ["creating", "created"] {
        let (buf, lines) = draw(&mut a, 170, 50);
        let text = lines.join("\n");
        for fact in s["expect"].as_array().unwrap() {
            assert!(text.contains(fact.as_str().unwrap()), "{stage}: {text}");
        }
        for fact in s["absent"].as_array().unwrap() {
            assert!(!text.contains(fact.as_str().unwrap()), "{stage}: {text}");
        }
        assert_eq!(a.current_agent(), "plan");
        if let Ok(dir) = std::env::var("NEXUS_FIRST_PROMPT_SHOTS") {
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(
                format!("{dir}/tui-{stage}.svg"),
                nexus_tui::svg::to_svg(&buf, "#ededed", "#0b0b0b"),
            )
            .unwrap();
        }
        if stage == "creating" {
            created(&mut a, call.clone());
        }
    }
    let pending = a.pending_first_prompt().unwrap().text.clone();
    a.state.model.selected.as_mut().unwrap().messages.push(
        serde_json::from_value(serde_json::json!({
            "id": "msg_first", "session_id": "ses_instant", "seq": 1, "role": "user",
            "parts": [{"type": "text", "text": pending}], "created": 1
        }))
        .unwrap(),
    );
    a.apply_sync(SyncMsg::Connection(ConnectionState::Live));
    assert!(
        a.pending_first_prompt().is_some(),
        "do not flash an empty header while history loads"
    );
    a.state.model.selected.as_mut().unwrap().starting = Some(serde_json::json!({}));
    a.apply_sync(SyncMsg::Connection(ConnectionState::Live));
    assert!(a.pending_first_prompt().is_none());
    assert!(a.ui.first_prompts.is_empty());
    assert_eq!(a.ui.draft, Default::default());
    assert_eq!(a.messages().len(), 1);
}

#[test]
fn creation_failure_restores_the_draft_and_allows_retry() {
    let (mut a, call) = sending();
    a.apply_done(ActionDone {
        call,
        result: Err("MCP startup failed".into()),
    });
    assert!(a.pending_first_prompt().is_none());
    assert_eq!(
        a.composer_ref().text(),
        scenario()["text"].as_str().unwrap()
    );
    assert_eq!(a.ui.draft.agent.as_deref(), Some("plan"));
    a.effects.clear();
    press(&mut a, &["enter"]);
    assert!(a.pending_first_prompt().is_some());
}

#[test]
fn prompt_failure_after_creation_restores_into_the_created_session() {
    let (mut a, call) = sending();
    let prompt = created(&mut a, call);
    a.apply_done(ActionDone {
        call: prompt,
        result: Err("send failed".into()),
    });
    assert_eq!(a.viewed_id(), Some("ses_instant"));
    assert!(a.pending_first_prompt().is_none());
    assert_eq!(
        a.composer_ref().text(),
        scenario()["text"].as_str().unwrap()
    );
}

#[test]
fn repeated_enter_during_creation_does_not_create_another_session() {
    let (mut a, _) = sending();
    a.effects.clear();
    a.composer().set_text("another message");
    press(&mut a, &["enter"]);
    assert_eq!(a.composer_ref().text(), "another message");
    assert!(!a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::NewAndPrompt { .. }))));
}

#[test]
fn pending_first_prompt_remains_visible_at_narrow_sizes() {
    for (width, height) in [(80, 24), (120, 36), (170, 50)] {
        let (mut a, _) = sending();
        let (_, lines) = draw(&mut a, width, height);
        let text = lines.join("\n");
        assert!(text.contains("Sending…"), "{width}x{height}: {text}");
        assert!(
            text.contains("Make the first message instant"),
            "{width}x{height}: {text}"
        );
    }
}
