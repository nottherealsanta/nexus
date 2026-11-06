mod common;
use common::*;
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::app::Effect;
use nexus_tui::inspector::Inspector;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::OverlayKind;
use serde_json::Value;

fn scenario() -> Value {
    serde_json::from_str(include_str!("../../interface/scenarios/inspector.json")).unwrap()
}

#[test]
fn native_inspector_opens_fetches_and_ignores_late_results() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.run_command("session.open_inspector");
    let call = app
        .effects
        .iter()
        .find_map(|e| match e {
            Effect::Api(c @ ApiCall::Inspector { .. }) => Some(c.clone()),
            _ => None,
        })
        .unwrap();
    assert!(!app.effects.iter().any(|e| matches!(e, Effect::OpenUrl(_))));
    app.apply_done(ActionDone {
        call: call.clone(),
        result: Ok(Done {
            inspection: Some(scenario()),
            ..Done::default()
        }),
    });
    let text = draw(&mut app, 160, 50).1.join("\n");
    for fact in ["Session inspector", "Cached 750", "Hit 75", "Scout", "bash"] {
        assert!(text.contains(fact), "missing {fact}: {text}");
    }
    press(&mut app, &["i"]);
    assert!(draw(&mut app, 160, 50)
        .1
        .join("\n")
        .contains("not a prediction"));
    press(&mut app, &["i", "j", "enter"]);
    if let Ok(path) = std::env::var("NEXUS_INSPECTOR_SHOT") {
        let buffer = draw(&mut app, 160, 50).0;
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(path, nexus_tui::svg::to_svg(&buffer, fg, bg)).unwrap();
    }
    press(&mut app, &["2"]);
    assert!(
        matches!(app.ui.overlay.as_ref().map(|o| &o.kind), Some(OverlayKind::Inspector(i)) if i.view == 1 && i.loading)
    );
    app.apply_done(ActionDone {
        call,
        result: Ok(Done {
            inspection: Some(scenario()),
            ..Done::default()
        }),
    });
    assert!(
        matches!(app.ui.overlay.as_ref().map(|o| &o.kind), Some(OverlayKind::Inspector(i)) if i.data.is_null())
    );
    press(&mut app, &["esc"]);
    assert!(app.ui.overlay.is_none());
}

#[test]
fn shared_gantt_zoom_details_and_unknown_cache() {
    let mut i = Inspector::new("ses_inspector".into());
    i.loading = false;
    i.data = scenario();
    let text = i.text(90);
    assert!(text.contains("+0.00s — +5.00s — +10.00s"));
    assert!(text.contains("######"));
    i.zoom = 2;
    i.pan = 10;
    assert!(i.text(90).contains("+5.00s — +7.50s — +10.00s"));
    i.cursor = 1;
    i.details = true;
    assert!(i.text(90).contains("Model request: 1000 input, 750 cached"));
    i.data = Value::Null;
    assert!(i.text(60).contains("Cached unknown"));
}
