//! First-run onboarding (plans/52): opens once per Core home, walks the manifest's steps and writes the choices to
//! config. Mirrors `nexus-nsh/onboarding.test.tsx`.

mod common;

use common::*;
use nexus_tui::app::{App, Effect};
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::sync::{Command as SyncCommand, SyncMsg};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{OverlayKind, PaletteMode, Target, View};
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

/// A tour whose catalog gives `codex/gpt-6-luna` effort levels, with `scout` preferring `low` in its agent file.
fn effort_app() -> App {
    let mut a = app();
    a.state.models[0]["efforts"] = json!(["low", "medium", "high"]);
    a.state.agents = vec![json!({
        "name": "scout", "mode": "subagent", "source": "builtin", "effort": "low"
    })];
    a.apply_sync(config(false));
    a.effects.clear();
    a
}

fn open_scout_model_picker(a: &mut App) {
    press(a, &["right", "right", "down", "enter"]);
    typing(a, "luna");
    press(a, &["enter"]);
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
fn a_model_with_efforts_opens_the_picker_and_escape_keeps_the_agent_default() {
    let mut a = effort_app();
    open_scout_model_picker(&mut a);
    let Some(OverlayKind::Palette(palette)) = a.ui.overlay.as_ref().map(|o| &o.kind) else {
        panic!("the effort picker should open after a model with levels");
    };
    assert_eq!(palette.mode, PaletteMode::AgentEfforts);
    let labels: Vec<String> = a.palette_items().into_iter().map(|i| i.label).collect();
    assert_eq!(labels, ["Agent default: low", "low", "medium", "high"]);
    // Esc leaves the agent-file default in place and keeps the model that was just chosen.
    press(&mut a, &["esc"]);
    assert!(a.ui.overlay.is_none());
    assert_eq!(a.ui.view, View::Onboarding);
    let patches = patches(&a);
    assert!(patches.iter().any(|(path, value)| {
        path == "agents.models" && value["scout"] == json!(["codex/gpt-6-luna"])
    }));
    assert!(!patches.iter().any(|(path, _)| path == "agents.efforts"));
    // With one model set, the row says what its effort will be without implying the provider's default.
    a.state.config.as_mut().unwrap()["values"]["agents"]["models"]["scout"] =
        json!(["codex/gpt-6-luna"]);
    let shown = text(&mut a);
    assert!(shown.contains("codex/gpt-6-luna"), "{shown}");
    assert!(shown.contains("effort Agent default: low"), "{shown}");
}

#[test]
fn choosing_effort_after_a_model_keeps_the_model_and_effort_writes() {
    let mut a = effort_app();
    open_scout_model_picker(&mut a);
    // The default is first, then low, medium, high.
    press(&mut a, &["down", "down", "enter"]);
    let patches = patches(&a);
    assert!(patches.iter().any(|(path, value)| {
        path == "agents.models" && value["scout"] == json!(["codex/gpt-6-luna"])
    }));
    assert!(patches.iter().any(|(path, value)| {
        path == "agents.efforts" && value["scout"]["codex/gpt-6-luna"] == json!("medium")
    }));
}

#[test]
fn the_onboarding_effort_line_opens_the_picker_when_tapped() {
    let mut a = effort_app();
    a.state.config.as_mut().unwrap()["values"]["agents"]["models"]["scout"] =
        json!(["codex/gpt-6-luna"]);
    press(&mut a, &["right", "right"]);
    draw(&mut a, 120, 36);
    let effort = a
        .hits
        .rect_of(&Target::Onboarding(
            nexus_tui::ui::OnboardingTarget::Effort(1),
        ))
        .expect("the scout effort line");
    click(&mut a, effort.x, effort.y);
    let Some(OverlayKind::Palette(palette)) = a.ui.overlay.as_ref().map(|o| &o.kind) else {
        panic!("the effort line should open the picker");
    };
    assert_eq!(palette.mode, PaletteMode::AgentEfforts);
}

#[test]
fn e_on_an_onboarding_model_row_opens_the_same_picker() {
    let mut a = effort_app();
    a.state.config.as_mut().unwrap()["values"]["agents"]["models"]["scout"] =
        json!(["codex/gpt-6-luna"]);
    press(&mut a, &["right", "right", "down", "e"]);
    let Some(OverlayKind::Palette(palette)) = a.ui.overlay.as_ref().map(|o| &o.kind) else {
        panic!("e should open the effort picker");
    };
    assert_eq!(palette.mode, PaletteMode::AgentEfforts);
    let labels: Vec<String> = a.palette_items().into_iter().map(|i| i.label).collect();
    assert_eq!(labels, ["Agent default: low", "low", "medium", "high"]);
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
    for _ in 0..8 {
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

fn search_status(a: &mut App, call: ApiCall, status: Value) {
    a.apply_done(ActionDone {
        call,
        result: Ok(Done {
            search_service: Some(status),
            ..Done::default()
        }),
    });
}

fn api_calls(a: &App) -> Vec<ApiCall> {
    a.effects
        .iter()
        .filter_map(|e| match e {
            Effect::Api(call) => Some(call.clone()),
            _ => None,
        })
        .collect()
}

#[test]
fn the_search_step_follows_the_agents_step_and_starts_search_in_the_background() {
    let mut a = app();
    a.apply_sync(config(false));
    press(&mut a, &["right", "right", "right"]);
    assert_eq!(a.ui.onboarding.step, 3);
    let shown = text(&mut a);
    assert!(shown.contains("Web search"), "{shown}");
    assert!(shown.contains("SearXNG"), "{shown}");
    assert!(shown.contains("on the machine running Nexus Core"), "{shown}");
    // Entering the step asks Core for the current state; it does not start anything blind.
    let status_call = api_calls(&a)
        .into_iter()
        .find(|c| *c == ApiCall::SearchServiceStatus)
        .expect("a status read");
    a.effects.clear();
    search_status(
        &mut a,
        status_call,
        json!({"state": "stopped", "url": "http://127.0.0.1:8080", "runtime": "docker", "detail": null}),
    );
    // A stopped service is started once, at once, without blocking the step.
    assert!(api_calls(&a).contains(&ApiCall::SearchServiceStart));
    assert!(text(&mut a).contains("Web search"));
    a.effects.clear();
    search_status(
        &mut a,
        ApiCall::SearchServiceStart,
        json!({"state": "starting", "url": "http://127.0.0.1:8080", "runtime": "docker", "detail": null}),
    );
    let shown = text(&mut a);
    assert!(shown.contains("starting…"), "{shown}");
    assert!(shown.contains("runtime docker"), "{shown}");
    assert!(
        a.ui.alerts.toasts.iter().any(|t| t.text.contains("is starting")),
        "the async start reports progress"
    );
    // The poll follows it to running.
    a.effects.clear();
    a.tick(std::time::Instant::now() + std::time::Duration::from_secs(3));
    assert!(api_calls(&a).contains(&ApiCall::SearchServiceStatus));
    search_status(
        &mut a,
        ApiCall::SearchServiceStatus,
        json!({"state": "running", "url": "http://127.0.0.1:8080", "runtime": "docker", "detail": null}),
    );
    let shown = text(&mut a);
    assert!(shown.contains("running"), "{shown}");
    // Next continues without waiting; skipping leaves the tour.
    press(&mut a, &["right"]);
    assert!(text(&mut a).contains("One timeline"));
    press(&mut a, &["s"]);
    assert_eq!(a.ui.view, View::Session);
}

#[test]
fn the_search_step_retry_starts_it_again_and_shows_an_error() {
    let mut a = app();
    a.apply_sync(config(false));
    press(&mut a, &["right", "right", "right"]);
    a.effects.clear();
    search_status(
        &mut a,
        ApiCall::SearchServiceStatus,
        json!({"state": "unavailable", "url": "", "runtime": "none", "detail": "No container runtime found."}),
    );
    let shown = text(&mut a);
    assert!(shown.contains("unavailable"), "{shown}");
    assert!(shown.contains("No container runtime found."), "{shown}");
    assert!(!api_calls(&a).contains(&ApiCall::SearchServiceStart));
    a.effects.clear();
    search_status(
        &mut a,
        ApiCall::SearchServiceStatus,
        json!({"state": "stopped", "url": "http://127.0.0.1:8080", "runtime": "docker", "detail": null}),
    );
    assert!(api_calls(&a).contains(&ApiCall::SearchServiceStart));
    // An error from a start is shown on the step and the Retry button starts it again.
    a.apply_done(ActionDone {
        call: ApiCall::SearchServiceStart,
        result: Err("boom".into()),
    });
    let shown = text(&mut a);
    assert!(shown.contains("boom"), "{shown}");
    assert!(a
        .ui
        .alerts
        .toasts
        .iter()
        .any(|t| t.text.contains("Web search did not start")));
    a.effects.clear();
    press(&mut a, &["enter"]);
    assert!(api_calls(&a).contains(&ApiCall::SearchServiceStart));
}

#[test]
fn the_shared_web_search_scenario_shows_the_same_state_in_the_tour() {
    let scenario: Value =
        serde_json::from_str(include_str!("../../interface/scenarios/web-search.json")).unwrap();
    let mut a = app();
    a.apply_sync(config(false));
    a.ui.search_service = Some(scenario["service"].clone());
    press(&mut a, &["right", "right", "right"]);
    let shown = text(&mut a);
    for fact in scenario["expect"].as_array().unwrap() {
        let fact = fact.as_str().unwrap();
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
    for fact in scenario["absent"].as_array().unwrap() {
        let fact = fact.as_str().unwrap();
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
    a.ui.search_service = Some(scenario["error_service"].clone());
    let shown = text(&mut a);
    assert!(shown.contains("Retry"), "{shown}");
    for fact in scenario["error_expect"].as_array().unwrap() {
        let fact = fact.as_str().unwrap();
        assert!(shown.contains(fact), "missing {fact:?} in:\n{shown}");
    }
}
