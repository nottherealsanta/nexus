mod common;
use common::*;
use nexus_tui::actions::ApiCall;
use nexus_tui::app::Effect;
use nexus_tui::theme::{Mode, Tier};

#[test]
fn archived_send_confirms_and_cancel_keeps_the_draft() {
    let scenario: serde_json::Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/resume-archived.json"
    ))
    .unwrap();
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.open_session(SESSION);
    a.state.model.sessions.get_mut(SESSION).unwrap().archived = true;
    a.effects.clear();
    typing(&mut a, scenario["prompt"].as_str().unwrap());
    press(&mut a, &["enter"]);
    let d = a.ui.dialog.as_ref().unwrap();
    assert_eq!(d.title, scenario["title"].as_str().unwrap());
    assert_eq!(d.body.join(" "), scenario["body"].as_str().unwrap());
    assert_eq!(d.button, 0);
    if let Ok(path) = std::env::var("NEXUS_RESUME_SCREENSHOT") {
        let (buffer, _) = draw(&mut a, 120, 36);
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(path, nexus_tui::svg::to_svg(&buffer, fg, bg)).unwrap();
    }
    assert!(!a.effects.iter().any(|e| matches!(e, Effect::Api(_))));
    press(&mut a, &["esc"]);
    assert!(a.ui.dialog.is_none());
    assert_eq!(
        a.composer_ref().text(),
        scenario["prompt"].as_str().unwrap()
    );
    press(&mut a, &["enter", "right", "enter"]);
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::Prompt { unarchive: Some(root), text, .. }) if root == SESSION && text == scenario["prompt"].as_str().unwrap())));
}

#[test]
fn explicit_unarchive_also_warns_without_sending() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.open_session(SESSION);
    a.state.model.sessions.get_mut(SESSION).unwrap().archived = true;
    a.effects.clear();
    a.execute("session.unarchive");
    assert_eq!(
        a.ui.dialog.as_ref().unwrap().title,
        "Resume archived session"
    );
    press(&mut a, &["right", "enter"]);
    assert!(a.effects.iter().any(|e| matches!(
        e,
        Effect::Api(ApiCall::Archive {
            archived: false,
            ..
        })
    )));
    assert!(!a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Prompt { .. }))));
}

#[test]
fn archived_child_send_unarchives_the_root_tree() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut child = a.state.model.sessions.get(SESSION).unwrap().clone();
    child.id = "ses_archived_child".into();
    child.parent_id = Some(SESSION.into());
    child.archived = true;
    a.state.model.sessions.insert(child.id.clone(), child);
    a.state.model.sessions.get_mut(SESSION).unwrap().archived = true;
    a.open_session("ses_archived_child");
    a.effects.clear();
    typing(&mut a, "Continue the child");
    press(&mut a, &["enter", "right", "enter"]);
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::Prompt { session, unarchive: Some(root), .. }) if session == "ses_archived_child" && root == SESSION)));
}
