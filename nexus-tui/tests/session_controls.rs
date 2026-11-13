//! Plans 29–32: archive tab closure, `/archive`, `/search`, one-step
//! questions with a locked composer, queued-row actions and the worktree modal.
//!
//! Kept in its own file so the shared shell suite's pre-existing failures do
//! not hide these behaviours.

mod common;

use common::*;
use nexus_tui::actions::ApiCall;
use nexus_tui::api::Event;
use nexus_tui::app::{App, Effect};
use nexus_tui::sync::{ResyncCause, SyncMsg};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{Focus, OverlayKind, PaletteMode, PendingAction, Target, WorktreeCopy};

const GEN: &str = "log_c915bfc9b6d826c4";
const SCENARIO: &str = include_str!("../../interface/scenarios/session-controls.json");

fn app() -> App {
    with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36))
}

fn event(app: &mut App, _hint: i64, kind: &str, session: Option<&str>, data: serde_json::Value) {
    // The fixture turn ends at id 16; every new event gets a fresh id after it.
    let id = 1000 + app.state.events_applied as i64;
    app.apply_sync(SyncMsg::Event(Event {
        id,
        cursor: Some(format!("{GEN}:{id}")),
        seq: Some(id),
        time: id as f64,
        kind: kind.to_owned(),
        session_id: session.map(str::to_owned),
        data,
    }));
}

fn add_session(app: &mut App, id: &str, parent: Option<&str>, title: &str) {
    let project = app.current_project().unwrap();
    event(
        app,
        100,
        "session.created",
        Some(id),
        serde_json::json!({
            "id": id,
            "project_id": project,
            "directory": "/work/nexus-home/ws",
            "title": title,
            "agent": "build",
            "parent_id": parent,
            "status": "idle",
            "archived": false,
            "revision": 1,
            "created": 10.0,
            "updated": 10.0
        }),
    );
}

fn archive(app: &mut App, root: &str, sessions: &[&str]) {
    event(
        app,
        101,
        "session.archived",
        Some(root),
        serde_json::json!({
            "root": root,
            "sessions": sessions,
            "archived": true,
            "revisions": {}
        }),
    );
}

fn joined(rows: &[String]) -> String {
    rows.join("\n")
}

fn col_of(row: &str, needle: &str) -> u16 {
    let byte = row.find(needle).unwrap_or(0);
    row[..byte].chars().count() as u16
}

fn sent_answers(app: &App) -> Vec<serde_json::Value> {
    app.effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::Api(ApiCall::Answer { answers, .. }) => {
                Some(serde_json::Value::Array(answers.clone()))
            }
            _ => None,
        })
        .collect()
}

fn ask(app: &mut App, questions: serde_json::Value) {
    event(
        app,
        110,
        "question.asked",
        Some(SESSION),
        serde_json::json!({
            "id": "qst_controls",
            "session_id": SESSION,
            "call_id": "call_controls",
            "created": 1.0,
            "questions": questions
        }),
    );
}

fn scenario_question() -> serde_json::Value {
    let scenario: serde_json::Value = serde_json::from_str(SCENARIO).unwrap();
    scenario["question"]["questions"].clone()
}

// ---- 29. Archive closes its tab, /archive, /search --------------------------------

#[test]
fn archiving_a_root_closes_its_tab_and_moves_to_the_neighbour() {
    let mut app = app();
    add_session(&mut app, "ses_b", None, "Second");
    add_session(&mut app, "ses_b_child", Some("ses_b"), "Child");
    app.open_session("ses_b");
    assert_eq!(app.tab_ids(), vec![SESSION.to_owned(), "ses_b".to_owned()]);

    archive(&mut app, "ses_b", &["ses_b", "ses_b_child"]);

    assert_eq!(app.tab_ids(), vec![SESSION.to_owned()]);
    assert_eq!(app.viewed_id(), Some(SESSION));
}

#[test]
fn archiving_a_child_leaves_its_root_tab_open() {
    let mut app = app();
    add_session(&mut app, "ses_root", None, "Root");
    add_session(&mut app, "ses_kid", Some("ses_root"), "Kid");
    app.open_session("ses_root");
    archive(&mut app, "ses_kid", &["ses_kid"]);
    assert_eq!(
        app.tab_ids(),
        vec![SESSION.to_owned(), "ses_root".to_owned()]
    );
    assert_eq!(app.viewed_id(), Some("ses_root"));
}

#[test]
fn an_archived_snapshot_also_closes_the_tab() {
    let mut app = app();
    let mut snap = snapshot();
    for session in &mut snap.sessions {
        if session.id == SESSION {
            session.archived = true;
        }
    }
    app.apply_sync(SyncMsg::Snapshot {
        snapshot: Box::new(snap),
        cause: ResyncCause::Initial,
    });
    assert!(app.tab_ids().is_empty());
    assert_eq!(app.viewed_id(), None);
}

#[test]
fn the_archive_command_closes_the_tab_when_its_event_arrives() {
    let mut app = app();
    add_session(&mut app, "ses_b", None, "Second");
    app.open_session("ses_b");
    app.effects.clear();
    app.run_command("session.archive");
    assert!(app.effects.iter().any(|effect| matches!(
        effect,
        Effect::Api(ApiCall::Archive {
            session,
            archived: true
        }) if session == "ses_b"
    )));
    archive(&mut app, "ses_b", &["ses_b"]);
    assert_eq!(app.viewed_id(), Some(SESSION));
    assert_eq!(app.tab_ids(), vec![SESSION.to_owned()]);
}

#[test]
fn archive_is_a_slash_command_in_the_shared_manifest() {
    let mut app = app();
    typing(&mut app, "/arch");
    let rows = app.completion_rows().unwrap_or_default();
    assert!(rows.iter().any(|row| row.value == "/archive"), "{rows:?}");
}

#[test]
fn hovered_session_row_has_stacked_archive_and_menu_actions() {
    let mut app = app();
    draw(&mut app, 120, 36);
    let row = app.hits.rect_of(&Target::SideRow(0)).unwrap();
    app.mouse = Some((row.x + 3, row.y));
    draw(&mut app, 120, 36);
    let archive = app.hits.rect_of(&Target::ArchiveSession(SESSION.to_owned())).unwrap();
    let menu = app.hits.rect_of(&Target::SessionRowMenu(0)).unwrap();
    assert_eq!(archive.x, menu.x);
    assert_eq!(archive.y + 1, menu.y);
    click(&mut app, menu.x, menu.y);
    assert!(app.palette_items().iter().any(|item| item.label == "Archive session"));
    app.ui.overlay = None;
    draw(&mut app, 120, 36);
    click(&mut app, archive.x, archive.y);
    assert!(app.effects.iter().any(|effect| matches!(effect, Effect::Api(ApiCall::Archive { session, archived: true }) if session == SESSION)));
}

#[test]
fn row_menu_archive_refusal_keeps_the_tab_and_explains_why() {
    let mut app = app();
    draw(&mut app, 120, 36);
    let row = app.hits.rect_of(&Target::SideRow(0)).expect("session row");
    right_click(&mut app, row.x + 2, row.y);
    draw(&mut app, 120, 36);
    let index = app
        .palette_items()
        .iter()
        .position(|item| item.label == "Archive session")
        .expect("archive menu item");
    let item = app
        .hits
        .rect_of(&Target::PaletteItem(index))
        .expect("archive hit");
    click(&mut app, item.x + 1, item.y);
    let call = app
        .effects
        .iter()
        .find_map(|effect| match effect {
            Effect::Api(call @ ApiCall::Archive { archived: true, .. }) => Some(call.clone()),
            _ => None,
        })
        .expect("archive request");
    app.apply_done(nexus_tui::actions::ActionDone {
        call,
        result: Err("A background job is running".into()),
    });
    assert_eq!(app.viewed_id(), Some(SESSION));
    assert!(app
        .ui
        .alerts
        .toasts
        .iter()
        .any(|toast| toast.text.contains("A background job is running")));
}

#[test]
fn search_opens_the_sessions_switcher_with_a_focused_query() {
    let mut app = app();
    app.composer().set_text("/search");
    press(&mut app, &["enter"]);
    let Some(nexus_tui::ui::Overlay {
        kind: OverlayKind::Palette(p),
        ..
    }) = &app.ui.overlay
    else {
        panic!("sessions palette is not open: {:?}", app.ui.overlay);
    };
    assert_eq!(p.mode, PaletteMode::Sessions);
    assert!(p.query.text().is_empty());
    assert!(app.composer_ref().text().is_empty());

    // Typing filters immediately, with no second key or click.
    typing(&mut app, "New");
    let Some(nexus_tui::ui::Overlay {
        kind: OverlayKind::Palette(p),
        ..
    }) = &app.ui.overlay
    else {
        panic!("sessions palette closed");
    };
    assert_eq!(p.query.text(), "New");
    assert!(
        app.palette_items()
            .iter()
            .any(|item| item.label == "New session"),
        "{:?}",
        app.palette_items()
    );
}

#[test]
fn search_takes_its_argument_and_focuses_the_query() {
    let mut app = app();
    app.composer().set_text("/search migration");
    press(&mut app, &["enter"]);
    let Some(nexus_tui::ui::Overlay {
        kind: OverlayKind::Palette(p),
        ..
    }) = &app.ui.overlay
    else {
        panic!("sessions palette is not open");
    };
    assert_eq!(p.mode, PaletteMode::Sessions);
    assert_eq!(p.query.text(), "migration");
}

// ---- 30. Questions: one step, locked composer, focused free text -----------------

#[test]
fn one_single_choice_question_answers_on_a_click() {
    let mut app = app();
    ask(&mut app, scenario_question());
    assert_eq!(app.ui.focus, Focus::Waiting);
    let rows = draw(&mut app, 120, 36).1;
    let y = rows
        .iter()
        .position(|row| row.contains("Direct migration"))
        .expect("second option");
    click(&mut app, col_of(&rows[y], "Direct migration"), y as u16);
    assert_eq!(
        sent_answers(&app),
        vec![serde_json::json!([{"question_id": "approach", "selected": ["fast"], "text": null}])]
    );
}

#[test]
fn one_free_text_question_is_focused_and_sends_on_enter_without_a_review() {
    let mut app = app();
    ask(&mut app, scenario_question());
    let current = app.ask_current().unwrap();
    assert!(current.field(), "the free-text answer line has focus");
    let rows = draw(&mut app, 120, 36).1;
    let y = rows
        .iter()
        .position(|row| row.contains("Add context"))
        .expect("answer line");
    let panel = app.theme.color("surface.panel").unwrap();
    let raised = app.theme.color("surface.raised").unwrap();
    let x = app.regions.waiting.x + 5;
    assert_eq!(app.hits.at(x, y as u16), Some(&Target::WaitInput));
    let (buf, _) = draw(&mut app, 120, 36);
    assert_eq!(buf[(x, y as u16)].bg, panel);
    assert_ne!(buf[(x, y as u16)].bg, raised);

    typing(&mut app, "second thoughts");
    press(&mut app, &["enter"]);
    assert_eq!(
        sent_answers(&app),
        vec![
            serde_json::json!([{"question_id": "approach", "selected": [], "text": "second thoughts"}])
        ]
    );
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(!all.contains("enter submit"), "{all}");
}

#[test]
fn the_draft_is_locked_and_survives_the_question() {
    let draft = "Keep this unsent draft";
    let mut app = app();
    app.composer().set_text(draft);
    app.effects.clear();
    ask(&mut app, scenario_question());
    assert_eq!(app.ui.focus, Focus::Waiting);
    let rows = draw(&mut app, 120, 36).1;
    assert!(!joined(&rows).contains("Answer the question above"));
    assert!(joined(&rows).contains(draft), "the draft stays visible");

    typing(&mut app, "free answer");
    app.ui.focus = Focus::Composer;
    app.handle_paste(" plus paste");
    assert_eq!(app.composer_ref().expanded_text(), draft);
    assert_eq!(
        app.ask_current()
            .unwrap()
            .pick()
            .unwrap()
            .input
            .expanded_text(),
        "free answer plus paste"
    );
    app.run_command("composer.queue");
    app.run_command("session.send");
    assert_eq!(app.composer_ref().expanded_text(), draft);
    assert!(!app
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::Api(ApiCall::Prompt { .. }))));

    press(&mut app, &["enter"]);
    assert_eq!(
        sent_answers(&app),
        vec![
            serde_json::json!([{"question_id": "approach", "selected": [], "text": "free answer plus paste"}])
        ]
    );
    assert_eq!(app.composer_ref().expanded_text(), draft);
}

#[test]
fn an_empty_composer_shows_the_lock_reason() {
    let mut app = app();
    ask(&mut app, scenario_question());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("Answer the question above"), "{all}");
}

#[test]
fn one_multi_select_question_submits_once_without_a_review() {
    let mut app = app();
    ask(
        &mut app,
        serde_json::json!([{
            "id": "tags",
            "question": "Which tags?",
            "multiple": true,
            "allow_free_text": false,
            "options": [
                {"id": "a", "label": "alpha"},
                {"id": "b", "label": "beta"}
            ]
        }]),
    );
    press(&mut app, &["1", "2", "enter"]);
    assert_eq!(
        sent_answers(&app),
        vec![serde_json::json!([{"question_id": "tags", "selected": ["a", "b"], "text": null}])]
    );
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(!all.contains("enter submit"), "{all}");
}

#[test]
fn two_questions_keep_the_step_through_and_review_flow() {
    let mut app = app();
    ask(
        &mut app,
        serde_json::json!([
            {"id": "db", "question": "Which database?", "multiple": false,
             "allow_free_text": false, "options": [
                {"id": "pg", "label": "PostgreSQL"},
                {"id": "lite", "label": "SQLite"}
             ]},
            {"id": "notes", "question": "Anything else?"}
        ]),
    );
    press(&mut app, &["enter"]);
    assert!(sent_answers(&app).is_empty());
    typing(&mut app, "a note");
    press(&mut app, &["enter"]);
    assert!(sent_answers(&app).is_empty());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("1. Which database?") && all.contains("enter submit"),
        "{all}"
    );
    press(&mut app, &["enter"]);
    assert_eq!(
        sent_answers(&app),
        vec![serde_json::json!([
            {"question_id": "db", "selected": ["pg"], "text": null},
            {"question_id": "notes", "selected": [], "text": "a note"}
        ])]
    );
}

#[test]
fn a_question_in_another_session_does_not_lock_the_composer() {
    let mut app = app();
    add_session(&mut app, "ses_other", None, "Other");
    event(
        &mut app,
        111,
        "question.asked",
        Some("ses_other"),
        serde_json::json!({
            "id": "qst_other",
            "session_id": "ses_other",
            "call_id": "c",
            "created": 1.0,
            "questions": scenario_question()
        }),
    );
    assert!(!app.question_pending());
    typing(&mut app, "my own draft");
    assert_eq!(app.composer_ref().expanded_text(), "my own draft");
}

#[test]
fn answering_restores_the_composer_and_its_draft() {
    let draft = "Keep this unsent draft";
    let mut app = app();
    app.composer().set_text(draft);
    ask(&mut app, scenario_question());
    typing(&mut app, "answer");
    press(&mut app, &["enter"]);
    assert!(!sent_answers(&app).is_empty());
    event(
        &mut app,
        112,
        "question.replied",
        Some(SESSION),
        serde_json::json!({"id": "qst_controls", "answers": []}),
    );
    assert!(!app.question_pending());
    assert_eq!(app.ui.focus, Focus::Composer);
    assert_eq!(app.composer_ref().expanded_text(), draft);
    typing(&mut app, "!");
    assert_eq!(app.composer_ref().expanded_text(), format!("{draft}!"));
}

#[test]
fn switching_sessions_keeps_the_draft_and_returns_to_the_question() {
    let draft = "Draft for the first session";
    let mut app = app();
    app.composer().set_text(draft);
    ask(&mut app, scenario_question());
    add_session(&mut app, "ses_b", None, "Second");
    app.open_session("ses_b");
    assert_eq!(app.ui.focus, Focus::Composer);
    typing(&mut app, "second session draft");
    assert_eq!(app.composer_ref().expanded_text(), "second session draft");

    app.open_session(SESSION);
    assert_eq!(app.ui.focus, Focus::Waiting);
    assert_eq!(app.composer_ref().expanded_text(), draft);
    assert!(app.question_pending());
}

// ---- 31. Queued rows: edit, up, down, remove -------------------------------------

fn pending_app() -> App {
    let mut app = app();
    app.state.inputs.insert(
        SESSION.into(),
        serde_json::json!({"held": false, "inputs": [
            {"id": "q1", "mode": "queue", "parts": [{"type": "text", "text": "First queued message"}]},
            {"id": "q2", "mode": "queue", "parts": [{"type": "text", "text": "Second queued message"}]},
            {"id": "q3", "mode": "queue", "parts": [{"type": "text", "text": "Third queued message"}]},
            {"id": "s1", "mode": "steer", "parts": [{"type": "text", "text": "Pending steer message"}]}
        ]}),
    );
    app.effects.clear();
    app.ui.focus = Focus::Composer;
    app.ui.pending_selected = None;
    app
}

fn hover_row(app: &mut App, text: &str) -> usize {
    let rows = draw(app, 120, 36).1;
    let y = rows
        .iter()
        .position(|row| row.contains(text))
        .unwrap_or_else(|| panic!("{text} is not on screen: {rows:#?}"));
    let x = app.regions.composer.x + 6;
    app.mouse = Some((x, y as u16));
    let rows = draw(app, 120, 36).1;
    rows.iter().position(|row| row.contains(text)).unwrap()
}

fn action_rect(app: &App, index: usize, action: PendingAction) -> Option<ratatui::layout::Rect> {
    app.hits.rect_of(&Target::PendingAction(index, action))
}

fn input_call(app: &App) -> Option<&ApiCall> {
    app.effects.iter().find_map(|effect| match effect {
        Effect::Api(call @ ApiCall::Input { .. }) => Some(call),
        _ => None,
    })
}

#[test]
fn only_queued_rows_show_the_four_actions_in_order() {
    let mut app = pending_app();
    hover_row(&mut app, "Second queued message");
    let edit = action_rect(&app, 1, PendingAction::Edit).expect("edit");
    let up = action_rect(&app, 1, PendingAction::Up).expect("up");
    let down = action_rect(&app, 1, PendingAction::Down).expect("down");
    let remove = action_rect(&app, 1, PendingAction::Remove).expect("remove");
    assert!(edit.x < up.x && up.x < down.x && down.x < remove.x);

    app.ui.pending_selected = Some(3);
    hover_row(&mut app, "Pending steer message");
    for action in [
        PendingAction::Edit,
        PendingAction::Up,
        PendingAction::Down,
        PendingAction::Remove,
    ] {
        assert!(
            action_rect(&app, 3, action).is_none(),
            "steer rows have no action icons"
        );
    }
}

#[test]
fn up_and_down_are_bounded_within_the_queued_group() {
    let mut app = pending_app();
    hover_row(&mut app, "First queued message");
    assert!(action_rect(&app, 0, PendingAction::Up).is_none());
    assert!(action_rect(&app, 0, PendingAction::Down).is_some());

    hover_row(&mut app, "Third queued message");
    assert!(action_rect(&app, 2, PendingAction::Up).is_some());
    assert!(action_rect(&app, 2, PendingAction::Down).is_none());
}

#[test]
fn clicking_each_queued_action_runs_the_matching_key_action() {
    // Edit restores the row to the composer (the same call as Enter).
    let mut app = pending_app();
    hover_row(&mut app, "Second queued message");
    let edit = action_rect(&app, 1, PendingAction::Edit).unwrap();
    click(&mut app, edit.x, edit.y);
    assert!(matches!(
        input_call(&app),
        Some(ApiCall::Input { id, action, .. }) if id == "q2" && action == "remove"
    ));

    // Remove still asks first.
    let mut app = pending_app();
    hover_row(&mut app, "Second queued message");
    let remove = action_rect(&app, 1, PendingAction::Remove).unwrap();
    click(&mut app, remove.x, remove.y);
    assert!(input_call(&app).is_none());
    assert_eq!(
        app.ui.dialog.as_ref().map(|dialog| dialog.title.as_str()),
        Some("Remove pending input")
    );
    press(&mut app, &["right", "enter"]);
    assert!(matches!(
        input_call(&app),
        Some(ApiCall::Input { id, action, parts, .. })
            if id == "q2" && action == "delete" && parts.is_empty()
    ));

    // Up swaps with the previous queued row only.
    let mut app = pending_app();
    hover_row(&mut app, "Second queued message");
    let up = action_rect(&app, 1, PendingAction::Up).unwrap();
    click(&mut app, up.x, up.y);
    assert!(matches!(
        input_call(&app),
        Some(ApiCall::Input { id, action, before, .. })
            if id == "q2" && action == "move" && before.as_deref() == Some("q1")
    ));

    // Down swaps with the next queued row only.
    let mut app = pending_app();
    hover_row(&mut app, "Second queued message");
    let down = action_rect(&app, 1, PendingAction::Down).unwrap();
    click(&mut app, down.x, down.y);
    assert!(matches!(
        input_call(&app),
        Some(ApiCall::Input { id, action, before, .. })
            if id == "q3" && action == "move" && before.as_deref() == Some("q2")
    ));
}

// ---- 32. Worktree icon, tooltip and copyable modal --------------------------------

/// The shared scenario's linked worktree, for screenshots paired with NSH.
fn scenario_worktree(app: &mut App) {
    let scenario: serde_json::Value = serde_json::from_str(SCENARIO).unwrap();
    let directory = scenario["directory"].as_str().unwrap();
    app.state.model.sessions.get_mut(SESSION).unwrap().directory = directory.to_owned();
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "main": scenario["worktree_info"]["main_path"],
            "session_directory": directory,
            "worktrees": scenario["worktrees"]
        }),
    );
}

fn linked_worktree(app: &mut App) {
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "repository": "/work/nexus-home",
            "main": "/work/nexus-home",
            "session_directory": "/work/nexus-home/ws",
            "worktrees": [
                {"path": "/work/nexus-home", "branch": "main", "main": true},
                {"path": "/work/nexus-home/ws", "branch": "feature/session-controls", "main": false}
            ]
        }),
    );
}

#[test]
fn the_worktree_icon_has_a_tooltip_and_opens_the_compact_modal() {
    let mut app = app();
    linked_worktree(&mut app);
    assert_eq!(app.term("session.linked_worktree"), Some(true));
    draw(&mut app, 160, 50);
    let icon = app
        .hits
        .rect_of(&Target::ComposerWorktreeInfo)
        .expect("worktree icon hit target");
    app.mouse = Some((icon.x, icon.y));
    let all = joined(&draw(&mut app, 160, 50).1);
    assert!(all.contains("Worktree session"), "{all}");

    click(&mut app, icon.x, icon.y);
    let Some(nexus_tui::ui::Overlay {
        kind: OverlayKind::WorktreeInfo(info),
        ..
    }) = &app.ui.overlay
    else {
        panic!("worktree modal is not open");
    };
    assert_eq!(info.path, "/work/nexus-home/ws");
    assert_eq!(info.branch, "feature/session-controls");
    assert_eq!(info.main, "/work/nexus-home");

    let all = joined(&draw(&mut app, 160, 50).1);
    for text in [
        "This session is in a worktree.",
        "Worktree path",
        "Branch",
        "Main checkout",
        "/work/nexus-home/ws",
        "feature/session-controls",
        "/work/nexus-home",
        "esc close",
    ] {
        assert!(all.contains(text), "missing {text}: {all}");
    }
    assert!(!all.contains("Manage"), "{all}");
}

#[test]
fn the_modal_copies_the_worktree_path_with_a_toast() {
    let mut app = app();
    linked_worktree(&mut app);
    draw(&mut app, 160, 50);
    let icon = app.hits.rect_of(&Target::ComposerWorktreeInfo).unwrap();
    click(&mut app, icon.x, icon.y);
    app.effects.clear();
    draw(&mut app, 160, 50);
    let copy = app
        .hits
        .rect_of(&Target::WorktreeCopy(WorktreeCopy::Path))
        .expect("copy target");
    click(&mut app, copy.x, copy.y);
    assert!(app
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::Copy(path) if path == "/work/nexus-home/ws")));
    assert!(app
        .ui
        .alerts
        .toasts
        .iter()
        .any(|toast| toast.text == "Copied"));

    for (which, value) in [
        (WorktreeCopy::Branch, "feature/session-controls"),
        (WorktreeCopy::Main, "/work/nexus-home"),
    ] {
        draw(&mut app, 160, 50);
        let copy = app
            .hits
            .rect_of(&Target::WorktreeCopy(which))
            .unwrap_or_else(|| panic!("{which:?} copy target"));
        click(&mut app, copy.x, copy.y);
        assert!(app
            .effects
            .iter()
            .any(|effect| matches!(effect, Effect::Copy(text) if text == value)));
    }
}

#[test]
fn the_worktree_predicate_is_false_without_a_listing() {
    let mut app = app();
    assert_eq!(app.term("session.linked_worktree"), Some(false));
    linked_worktree(&mut app);
    assert_eq!(app.term("session.linked_worktree"), Some(true));
    assert_eq!(app.term("session.in_worktree"), Some(true));
}

#[test]
fn screenshots() {
    let Ok(dir) = std::env::var("NEXUS_SESSION_CONTROLS_SHOTS") else {
        return;
    };
    let shot = |name: &str, app: &mut App, size: (u16, u16)| {
        draw(app, size.0, size.1);
        let buf = nexus_tui::render::render_buffer(app, size.0, size.1);
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(
            format!("{dir}/{name}.svg"),
            nexus_tui::svg::to_svg(&buf, fg, bg),
        )
        .unwrap();
    };

    let mut question = app();
    scenario_worktree(&mut question);
    let scenario: serde_json::Value = serde_json::from_str(SCENARIO).unwrap();
    question
        .composer()
        .set_text(scenario["draft"].as_str().unwrap());
    ask(&mut question, scenario_question());
    shot("tui-question-lock-120x36-dark", &mut question, (120, 36));

    let mut queued = pending_app();
    scenario_worktree(&mut queued);
    hover_row(&mut queued, "Second queued message");
    shot("tui-queued-actions-120x36-dark", &mut queued, (120, 36));

    let mut held = pending_app();
    scenario_worktree(&mut held);
    held.state.inputs.get_mut(SESSION).unwrap()["held"] = true.into();
    held.ui.pending_selected = Some(2);
    shot("tui-queue-held-selected-120x36-dark", &mut held, (120, 36));

    let mut tooltip = app();
    scenario_worktree(&mut tooltip);
    draw(&mut tooltip, 120, 36);
    let icon = tooltip.hits.rect_of(&Target::ComposerWorktreeInfo).unwrap();
    tooltip.mouse = Some((icon.x, icon.y));
    shot("tui-worktree-tooltip-120x36-dark", &mut tooltip, (120, 36));

    let mut worktree = app();
    scenario_worktree(&mut worktree);
    draw(&mut worktree, 120, 36);
    let icon = worktree
        .hits
        .rect_of(&Target::ComposerWorktreeInfo)
        .unwrap();
    click(&mut worktree, icon.x, icon.y);
    shot("tui-worktree-modal-120x36-dark", &mut worktree, (120, 36));
}

#[test]
fn selected_held_queue_explains_and_sends_next() {
    let mut app = pending_app();
    app.state.inputs.get_mut(SESSION).unwrap()["held"] = true.into();
    app.ui.pending_selected = Some(2);
    let rows = draw(&mut app, 200, 40).1.join("\n");
    assert!(rows.contains("n send next"), "{rows}");
    press(&mut app, &["n"]);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Input { action, .. }) if action == "next")));
    app.effects.clear();
    draw(&mut app, 200, 40);
    let button = app
        .hits
        .rect_of(&Target::PendingNext)
        .expect("held queue button");
    click(&mut app, button.x, button.y);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Input { action, .. }) if action == "next")));
    app.effects.clear();
    ask(&mut app, scenario_question());
    draw(&mut app, 200, 40);
    assert!(app.hits.rect_of(&Target::PendingNext).is_none());
}

#[test]
fn the_project_fallback_accepts_a_linked_sibling_checkout() {
    let mut app = app();
    app.state.model.sessions.get_mut(SESSION).unwrap().directory = "/work/demo-feature".into();
    app.state.worktrees.clear();
    let project = app.current_project().unwrap();
    app.state.project_worktrees.insert(
        project,
        serde_json::json!({
            "project_directory": "/work/demo",
            "worktrees": [
                {"path": "/work/demo", "branch": "main", "main": true},
                {"path": "/work/demo-feature", "branch": "feature/session-controls", "main": false}
            ]
        }),
    );
    assert_eq!(app.term("session.linked_worktree"), Some(true));
    let info = app
        .worktree_info()
        .expect("project listing supplies the worktree");
    assert_eq!(info.path, "/work/demo-feature");
    assert_eq!(info.branch, "feature/session-controls");
    assert_eq!(info.main, "/work/demo");
}

#[test]
fn a_stale_session_listing_is_rejected() {
    let mut app = app();
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "main": "/work/nexus-home",
            "session_directory": "/work/elsewhere",
            "worktrees": [
                {"path": "/work/nexus-home", "branch": "main", "main": true},
                {"path": "/work/nexus-home/ws", "branch": "stale", "main": false}
            ]
        }),
    );
    assert_eq!(app.term("session.linked_worktree"), Some(false));
    assert!(app.worktree_info().is_none());
}

#[test]
fn archiving_removes_tabs_stored_for_a_background_project() {
    let mut app = app();
    let mut background = app.state.model.sessions[SESSION].clone();
    background.id = "ses_background".into();
    background.project_id = "prj_background".into();
    background.title = "Background".into();
    app.state
        .model
        .sessions
        .insert(background.id.clone(), background);
    app.ui.tabs.push("ses_background".into());
    archive(&mut app, "ses_background", &["ses_background"]);
    assert!(
        !app.ui.tabs.iter().any(|id| id == "ses_background"),
        "{:?}",
        app.ui.tabs
    );
    assert_eq!(app.tab_ids(), vec![SESSION.to_owned()]);
}

#[test]
fn a_multi_root_archive_never_opens_another_archived_tab() {
    let mut app = app();
    add_session(&mut app, "ses_b", None, "Second");
    add_session(&mut app, "ses_c", None, "Third");
    app.open_session("ses_b");
    app.open_session("ses_c");
    app.open_session(SESSION);
    archive(&mut app, SESSION, &[SESSION, "ses_b", "ses_c"]);
    assert!(app.ui.tabs.is_empty(), "{:?}", app.ui.tabs);
    assert_eq!(app.viewed_id(), None);
}

#[test]
fn the_next_tab_skips_an_archived_neighbour() {
    let mut app = app();
    add_session(&mut app, "ses_b", None, "Second");
    add_session(&mut app, "ses_c", None, "Third");
    app.open_session("ses_b");
    app.open_session("ses_c");
    app.open_session(SESSION);
    archive(&mut app, SESSION, &[SESSION, "ses_b"]);
    assert_eq!(app.viewed_id(), Some("ses_c"));
    assert_eq!(app.tab_ids(), vec!["ses_c".to_owned()]);
}

#[test]
fn a_long_worktree_path_wraps_and_scrolls_to_its_end() {
    let mut app = app();
    let long = format!(
        "/work/nexus-home/{}final-tail",
        "very-long-worktree-component/".repeat(24)
    );
    app.state.model.sessions.get_mut(SESSION).unwrap().directory = long.clone();
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "main": "/work/nexus-home",
            "session_directory": long,
            "worktrees": [
                {"path": "/work/nexus-home", "branch": "main", "main": true},
                {"path": long, "branch": "feature/session-controls", "main": false}
            ]
        }),
    );
    app.run_command("session.worktree_info");
    let rows = draw(&mut app, 80, 24).1;
    let all = joined(&rows);
    assert!(all.contains("scroll"), "{all}");
    let modal = app.hits.rect_of(&Target::Swallow).expect("modal body");
    let modal_rows = |app: &mut App| {
        draw(app, 80, 24).1[modal.y as usize..(modal.y + modal.height) as usize].join("\n")
    };
    assert!(
        !modal_rows(&mut app).contains("final-tail"),
        "the tail starts below the first page"
    );
    for _ in 0..20 {
        press(&mut app, &["pagedown"]);
    }
    assert!(
        modal_rows(&mut app).contains("final-tail"),
        "Page Down reaches the end of the full worktree path"
    );
    app.effects.clear();
    press(&mut app, &["enter"]);
    assert!(app.effects.iter().any(|effect| matches!(
        effect, Effect::Copy(path) if path == &long
    )));
}

#[test]
fn an_approval_in_front_keeps_the_composer_for_a_deny_note() {
    let mut app = app();
    event(
        &mut app,
        120,
        "permission.asked",
        Some(SESSION),
        serde_json::json!({
            "id": "per_controls",
            "session_id": SESSION,
            "tool": "bash",
            "call_id": "call_controls",
            "args": {"command": "rm -rf build"}
        }),
    );
    // A question waits behind the approval; the approval card is in view.
    ask(&mut app, scenario_question());
    assert!(!app.question_pending());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("Type a message"), "{all}");
    typing(&mut app, "do not remove the build cache");
    assert_eq!(
        app.composer_ref().expanded_text(),
        "do not remove the build cache"
    );
    press(&mut app, &["alt+w", "d"]);
    assert!(app.effects.iter().any(|effect| matches!(
        effect,
        Effect::Api(ApiCall::Reply {
            allow: false,
            note: Some(note),
            ..
        }) if note == "do not remove the build cache"
    )));
}

#[test]
fn queued_edit_is_blocked_while_a_question_is_visible() {
    let mut app = pending_app();
    ask(&mut app, scenario_question());
    app.effects.clear();
    hover_row(&mut app, "Second queued message");
    let edit = action_rect(&app, 1, PendingAction::Edit).expect("edit icon");
    click(&mut app, edit.x, edit.y);
    assert!(input_call(&app).is_none(), "no queued input may be removed");
    assert!(app.composer_ref().expanded_text().is_empty());
    assert!(app
        .ui
        .alerts
        .toasts
        .iter()
        .any(|toast| toast.text == "Answer the question above"));

    // The pending-input release key would send the queued turn; it is not a send.
    app.state.inputs.insert(
        SESSION.into(),
        serde_json::json!({"held": true, "inputs": [
            {"id": "q1", "mode": "queue", "parts": [{"type": "text", "text": "First queued message"}]}
        ]}),
    );
    app.ui.focus = Focus::Composer;
    press(&mut app, &["n"]);
    assert!(!app.effects.iter().any(|effect| matches!(
        effect,
        Effect::Api(ApiCall::Input { action, .. }) if action == "next"
    )));
}

#[test]
fn an_archived_tab_opened_on_purpose_survives_a_later_snapshot() {
    let mut app = app();
    app.state.model.sessions.get_mut(SESSION).unwrap().archived = true;
    let mut snap = snapshot();
    for session in &mut snap.sessions {
        if session.id == SESSION {
            session.archived = true;
        }
    }
    app.apply_sync(SyncMsg::Snapshot {
        snapshot: Box::new(snap),
        cause: ResyncCause::Initial,
    });
    assert_eq!(app.tab_ids(), vec![SESSION.to_owned()]);
    assert_eq!(app.viewed_id(), Some(SESSION));
}

#[test]
fn an_unrelated_archive_event_keeps_a_reopened_archived_tab() {
    let mut app = app();
    add_session(&mut app, "ses_archived", None, "Archived tab");
    app.state
        .model
        .sessions
        .get_mut("ses_archived")
        .unwrap()
        .archived = true;
    app.open_session("ses_archived");
    add_session(&mut app, "ses_c", None, "Third");
    app.open_session(SESSION);
    archive(&mut app, "ses_c", &["ses_c"]);
    assert_eq!(
        app.tab_ids(),
        vec![SESSION.to_owned(), "ses_archived".to_owned()]
    );
    assert_eq!(app.viewed_id(), Some(SESSION));
}

#[test]
fn a_failed_answer_keeps_the_answer_and_allows_one_retry() {
    let mut app = app();
    ask(
        &mut app,
        serde_json::json!([{
            "id": "text", "question": "Release note?", "options": [],
            "multiple": false, "allow_free_text": true
        }]),
    );
    typing(&mut app, "Preserve this answer");
    press(&mut app, &["enter"]);
    let call = app
        .effects
        .iter()
        .find_map(|effect| match effect {
            Effect::Api(call @ ApiCall::Answer { .. }) => Some(call.clone()),
            _ => None,
        })
        .expect("answer POST");
    let before = sent_answers(&app);
    app.apply_done(nexus_tui::actions::ActionDone {
        call,
        result: Err("Temporary answer failure".into()),
    });
    assert!(!app.ask_current().unwrap().sent);
    press(&mut app, &["enter"]);
    let after = sent_answers(&app);
    assert_eq!(after.len(), before.len() + 1);
    assert_eq!(after.last(), before.last());
    press(&mut app, &["enter"]);
    assert_eq!(sent_answers(&app).len(), after.len());
}

#[test]
fn enter_on_a_multiline_steer_restores_every_line() {
    let mut app = pending_app();
    app.state.inputs.insert(
        SESSION.into(),
        serde_json::json!({"held": false, "inputs": [
            {"id": "s1", "mode": "steer", "parts": [{"type": "text", "text": "one\ntwo\n\nfour"}]}
        ]}),
    );
    app.ui.pending_selected = Some(0);
    press(&mut app, &["enter"]);
    let call = input_call(&app).cloned().expect("remove call");
    app.apply_done(nexus_tui::actions::ActionDone {
        call,
        result: Ok(nexus_tui::actions::Done::default()),
    });
    assert_eq!(app.composer_ref().text(), "one\ntwo\n\nfour");
}
