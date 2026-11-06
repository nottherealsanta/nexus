mod common;
use common::*;
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::app::Effect;
use nexus_tui::dictation::Phase;
use nexus_tui::theme::{Mode, Tier};

fn transcribe(a: &mut nexus_tui::app::App, text: &str) {
    let generation = a.ui.dictation.begin(SESSION.to_owned());
    a.ui.dictation.anchor = a.composer_ref().text().len();
    a.ui.dictation.phase = Phase::Transcribing;
    a.apply_done(ActionDone {
        call: ApiCall::VoiceTranscribe {
            generation,
            partial: false,
            wav: vec![],
        },
        result: Ok(Done {
            transcript: Some(text.into()),
            ..Done::default()
        }),
    });
}

#[test]
fn voice_notice_is_visible_in_composer_and_sent_context() {
    let scenario: serde_json::Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/voice-transcript.json"
    ))
    .unwrap();
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    a.open_session(SESSION);
    a.effects.clear();
    a.composer()
        .set_text(&format!("{}\n", scenario["typed"].as_str().unwrap()));
    transcribe(&mut a, scenario["transcript"].as_str().unwrap());
    assert_eq!(
        a.composer_ref().text(),
        scenario["message"].as_str().unwrap()
    );
    a.state
        .model
        .selected
        .as_mut()
        .unwrap()
        .messages
        .push(nexus_tui::api::Message {
            id: "voice_message".into(),
            session_id: SESSION.into(),
            seq: 1,
            created: 1.0,
            role: "user".into(),
            parts: vec![serde_json::json!({"type": "text", "text": scenario["message"]})],
            delivery: None,
            origin: None,
        });
    let (buffer, rows) = draw(&mut a, 160, 48);
    assert_eq!(
        rows.join("\n")
            .matches(scenario["notice"].as_str().unwrap())
            .count(),
        2
    );
    if let Ok(path) = std::env::var("NEXUS_VOICE_SCREENSHOT") {
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(path, nexus_tui::svg::to_svg(&buffer, fg, bg)).unwrap();
    }
    press(&mut a, &["enter"]);
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::Prompt { text, .. }) if text == scenario["message"].as_str().unwrap())));
    assert!(a.composer_ref().is_empty());
}

#[test]
fn repeated_dictation_marks_once_and_empty_results_do_not_mark_typed_text() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.open_session(SESSION);
    a.composer().set_text("typed");
    transcribe(&mut a, "  ");
    assert_eq!(a.composer_ref().text(), "typed");
    transcribe(&mut a, "first");
    transcribe(&mut a, "second");
    let notice = nexus_tui::composer::VOICE_TRANSCRIPT_NOTICE;
    assert_eq!(
        a.composer_ref().text(),
        format!("{notice}\n\ntyped first second")
    );
    a.composer().clear();
    a.composer().insert_str("fresh typed draft");
    assert_eq!(a.composer_ref().text(), "fresh typed draft");
}
