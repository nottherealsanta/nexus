//! First-run onboarding (plans/52): opens once per Core home, walks the manifest's steps and writes the choices to
//! config. Mirrors `nexus-nsh/onboarding.test.tsx`.

mod common;

use common::*;
use nexus_tui::app::{App, Effect};
use nexus_tui::sync::{Command as SyncCommand, SyncMsg};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{OverlayKind, Target, View};
use serde_json::{json, Value};

fn app() -> App {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.apply_sync(SyncMsg::Catalog {
        models: vec![
            json!({"ref": "codex/gpt-6-luna", "name": "GPT-6 Luna", "efforts": []}),
            json!({"ref": "codex/gpt-6", "name": "GPT-6", "efforts": []}),
        ],
        agents: vec![],
        source: "t".into(),
        last_model: String::new(),
        providers: vec![
            json!({"id": "codex", "label": "Codex", "connected": true, "verified": true, "methods": ["browser"]}),
            json!({"id": "claude", "label": "Claude", "connected": false, "verified": true, "methods": ["cli"]}),
        ],
        problems: vec![],
    });
    a.effects.clear();
    a
}

fn config(onboarded: bool) -> SyncMsg {
    SyncMsg::Config(Ok(json!({
        "revision": "r1",
        "values": {
            "defaults": {}, "agents": {"models": {}, "fallbacks": {}, "efforts": {}},
            "ui": {"detail": "medium", "reduced_motion": true, "onboarded": onboarded}
        },
        "errors": []
    })))
}

fn patches(a: &App) -> Vec<(String, Value)> {
    a.effects
        .iter()
        .filter_map(|e| match e {
            Effect::Sync(SyncCommand::PatchConfig { path, value, .. }) => {
                Some((path.clone(), value.clone()))
            }
            _ => None,
        })
        .collect()
}

fn text(a: &mut App) -> String {
    draw(a, 120, 36).1.join("\n")
}

#[test]
fn opens_when_the_home_has_not_finished_it_and_not_when_it_has() {
    let mut fresh = app();
    fresh.apply_sync(config(false));
    assert_eq!(fresh.ui.view, View::Onboarding);
    assert!(text(&mut fresh).contains("Welcome to Nexus"));

    let mut done = app();
    done.apply_sync(config(true));
    assert_eq!(done.ui.view, View::Session);

    // Closing it does not bring it back when the config reloads in the same run.
    fresh.press_skip();
    fresh.apply_sync(config(false));
    assert_eq!(fresh.ui.view, View::Session);
}

trait Tour {
    fn press_skip(&mut self);
}
impl Tour for App {
    fn press_skip(&mut self) {
        press(self, &["s"]);
    }
}

#[test]
fn walks_the_steps_and_fills_command_keys() {
    let mut a = app();
    a.apply_sync(config(false));
    press(&mut a, &["enter"]);
    let shown = text(&mut a);
    assert!(shown.contains("Connect a provider"), "{shown}");
    assert!(shown.contains("Codex"), "{shown}");
    press(&mut a, &["esc"]);
    let shown = text(&mut a);
    assert!(shown.contains("Welcome to Nexus"), "{shown}");
    assert!(shown.contains("ctrl+p"), "{shown}");
    assert!(!shown.contains("{key:"), "{shown}");
    // Esc on the first step stays put; Skip is explicit.
    press(&mut a, &["esc"]);
    assert_eq!(a.ui.view, View::Onboarding);
    assert_eq!(a.ui.onboarding.step, 0);
}

#[test]
fn an_agent_row_writes_its_model_and_skip_marks_it_done() {
    let mut a = app();
    a.apply_sync(config(false));
    press(&mut a, &["right", "right"]);
    let shown = text(&mut a);
    assert!(shown.contains("luna, haiku or hydrogen"), "{shown}");
    // Rows: default agent, scout, quick, plan, build.
    press(&mut a, &["down", "enter"]);
    assert!(matches!(
        a.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::Palette(_))
    ));
    typing(&mut a, "luna");
    press(&mut a, &["enter"]);
    let all = patches(&a);
    let (path, value) = all
        .iter()
        .find(|(p, _)| p == "agents.models")
        .expect("a models patch");
    assert_eq!(path, "agents.models");
    assert_eq!(value["scout"], json!(["codex/gpt-6-luna"]));
    a.effects.clear();
    press(&mut a, &["s"]);
    assert_eq!(a.ui.view, View::Session);
    assert!(patches(&a)
        .iter()
        .any(|(p, v)| p == "ui.onboarded" && *v == json!(true)));
}

#[test]
fn the_default_agent_row_cycles_and_writes_defaults_agent() {
    let mut a = app();
    a.apply_sync(config(false));
    press(&mut a, &["right", "right", "enter"]);
    assert!(patches(&a)
        .iter()
        .any(|(p, v)| p == "defaults.agent" && *v == json!("orchestrate")));
}

#[test]
fn with_no_provider_connected_the_model_rows_say_why() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.apply_sync(config(false));
    press(&mut a, &["right", "right"]);
    let shown = text(&mut a);
    assert!(shown.contains("No provider is connected yet"), "{shown}");
    a.effects.clear();
    press(&mut a, &["down", "enter"]);
    assert!(a.ui.overlay.is_none(), "no picker without a provider");
    assert!(a
        .ui
        .alerts
        .toasts
        .iter()
        .any(|t| t.text.contains("No provider")));
}

#[test]
fn enter_on_the_provider_step_opens_the_connection_modal() {
    let mut a = app();
    a.apply_sync(config(false));
    press(&mut a, &["right", "down", "enter"]);
    assert_eq!(a.ui.provider_detail.as_deref(), Some("claude"));
    press(&mut a, &["esc"]);
    assert!(a.ui.provider_detail.is_none());
    assert_eq!(
        a.ui.onboarding.step, 1,
        "Esc closed the modal, not the step"
    );
}

#[test]
fn the_last_step_finishes_and_opens_add_project_when_there_is_none() {
    let mut a = app();
    a.apply_sync(config(false));
    for _ in 0..7 {
        press(&mut a, &["right"]);
    }
    let shown = text(&mut a);
    assert!(shown.contains("You're ready"), "{shown}");
    press(&mut a, &["enter"]);
    assert_eq!(a.ui.view, View::Session);
    assert!(patches(&a).iter().any(|(p, _)| p == "ui.onboarded"));
}

#[test]
fn clicks_reach_next_back_and_skip() {
    let mut a = app();
    a.apply_sync(config(false));
    draw(&mut a, 120, 36);
    let next = a
        .hits
        .rect_of(&Target::Onboarding(nexus_tui::ui::OnboardingTarget::Next))
        .expect("next");
    click(&mut a, next.x, next.y);
    assert_eq!(a.ui.onboarding.step, 1);
    draw(&mut a, 120, 36);
    let back = a
        .hits
        .rect_of(&Target::Onboarding(nexus_tui::ui::OnboardingTarget::Back))
        .expect("back");
    click(&mut a, back.x, back.y);
    assert_eq!(a.ui.onboarding.step, 0);
    draw(&mut a, 120, 36);
    let skip = a
        .hits
        .rect_of(&Target::Onboarding(nexus_tui::ui::OnboardingTarget::Skip))
        .expect("skip");
    click(&mut a, skip.x, skip.y);
    assert_eq!(a.ui.view, View::Session);
}

#[test]
fn the_slash_command_and_settings_replay_it() {
    let mut a = app();
    a.apply_sync(config(true));
    a.run_command("app.onboarding");
    assert_eq!(a.ui.view, View::Onboarding);
    // Settings does not open over it.
    a.execute("app.settings");
    assert_eq!(a.ui.view, View::Onboarding);
}
