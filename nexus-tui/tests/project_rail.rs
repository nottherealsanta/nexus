//! PLAN 15.3: the project rail's saved order, three-tier status and removal.

mod common;
use common::*;
use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use nexus_tui::actions::ApiCall;
use nexus_tui::app::{App, Effect};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Target;

fn three_projects() -> App {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let first = app.current_project().unwrap();
    let mut base = app.state.model.projects[&first].clone();
    base.name = "Alpha".into();
    base.position = Some(0);
    app.state.model.projects.insert(first.clone(), base.clone());
    for (i, name) in ["Bravo", "Charlie"].iter().enumerate() {
        let mut p = base.clone();
        p.id = name.to_lowercase();
        p.name = (*name).into();
        p.created += 1.0 + i as f64;
        p.position = Some(1 + i as i64);
        app.state.model.projects.insert(p.id.clone(), p);
    }
    app.ui.left_open = false;
    app.effects.clear();
    app
}

fn mouse(app: &mut App, kind: MouseEventKind, column: u16, row: u16) {
    app.handle_mouse(MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    });
}

fn api_calls(app: &App) -> Vec<ApiCall> {
    app.effects
        .iter()
        .filter_map(|e| match e {
            Effect::Api(c) => Some(c.clone()),
            _ => None,
        })
        .collect()
}

#[test]
fn the_rail_follows_the_saved_order_and_hides_inactive_projects() {
    let mut app = three_projects();
    app.state.model.projects.get_mut("bravo").unwrap().position = Some(5);
    app.state.model.projects.get_mut("charlie").unwrap().status = Some("inactive".into());
    let first = app.current_project().unwrap();
    assert_eq!(app.rail_project_ids(), vec![first, "bravo".to_string()]);
    // An older Core sends neither field: creation order, all current.
    for p in app.state.model.projects.values_mut() {
        p.position = None;
        p.status = None;
    }
    assert_eq!(app.rail_project_ids().len(), 3);
    assert_eq!(app.rail_project_ids()[2], "charlie");
}

#[test]
fn dragging_a_tile_moves_it_and_a_click_still_selects() {
    let mut app = three_projects();
    draw(&mut app, 120, 36);
    let third = app.hits.rect_of(&Target::RailProject(2)).unwrap();
    let first = app.hits.rect_of(&Target::RailProject(0)).unwrap();
    mouse(
        &mut app,
        MouseEventKind::Down(MouseButton::Left),
        third.x + 1,
        third.y + 1,
    );
    mouse(
        &mut app,
        MouseEventKind::Drag(MouseButton::Left),
        first.x + 1,
        first.y + 1,
    );
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        app.ui.rail_drag.is_some_and(|d| d.active && d.to == 0),
        "{rows:?}"
    );
    mouse(
        &mut app,
        MouseEventKind::Up(MouseButton::Left),
        first.x + 1,
        first.y + 1,
    );
    match api_calls(&app).as_slice() {
        [ApiCall::MoveProject {
            project,
            before,
            after,
        }] => {
            assert_eq!(project, "charlie");
            assert_eq!(
                before.as_deref(),
                Some(app.current_project().unwrap().as_str())
            );
            assert!(after.is_none());
        }
        other => panic!("{other:?}"),
    }
    app.effects.clear();
    click(&mut app, third.x + 1, third.y + 1);
    assert!(api_calls(&app)
        .iter()
        .all(|c| !matches!(c, ApiCall::MoveProject { .. })));
    assert_eq!(app.current_project().as_deref(), Some("charlie"));
}

#[test]
fn esc_or_dropping_outside_the_rail_cancels_a_drag() {
    let mut app = three_projects();
    draw(&mut app, 120, 36);
    let third = app.hits.rect_of(&Target::RailProject(2)).unwrap();
    let first = app.hits.rect_of(&Target::RailProject(0)).unwrap();
    mouse(
        &mut app,
        MouseEventKind::Down(MouseButton::Left),
        third.x + 1,
        third.y + 1,
    );
    mouse(
        &mut app,
        MouseEventKind::Drag(MouseButton::Left),
        first.x + 1,
        first.y + 1,
    );
    press(&mut app, &["esc"]);
    mouse(
        &mut app,
        MouseEventKind::Up(MouseButton::Left),
        first.x + 1,
        first.y + 1,
    );
    assert!(api_calls(&app).is_empty());
    mouse(
        &mut app,
        MouseEventKind::Down(MouseButton::Left),
        third.x + 1,
        third.y + 1,
    );
    mouse(
        &mut app,
        MouseEventKind::Drag(MouseButton::Left),
        first.x + 1,
        first.y + 1,
    );
    mouse(&mut app, MouseEventKind::Drag(MouseButton::Left), 100, 10);
    mouse(&mut app, MouseEventKind::Up(MouseButton::Left), 100, 10);
    assert!(api_calls(&app).is_empty());
}

#[test]
fn alt_up_and_down_step_the_project_through_the_order() {
    let mut app = three_projects();
    let first = app.rail_project_ids()[0].clone();
    app.ui.project = Some("bravo".into());
    app.run_command("project.move_up");
    app.run_command("project.move_down");
    let calls = api_calls(&app);
    assert!(
        matches!(&calls[0], ApiCall::MoveProject { project, before: Some(b), after: None } if project == "bravo" && *b == first)
    );
    assert!(
        matches!(&calls[1], ApiCall::MoveProject { project, before: None, after: Some(a) } if project == "bravo" && a == "charlie")
    );
    // Boundaries: the first cannot move up, the last cannot move down.
    app.effects.clear();
    app.ui.project = Some("charlie".into());
    app.run_command("project.move_down");
    assert!(api_calls(&app).is_empty());
    app.ui.project = Some(first.clone());
    app.run_command("project.move_up");
    assert!(api_calls(&app).is_empty());
}

#[test]
fn status_commands_set_core_status_and_passive_dims_the_label() {
    let mut app = three_projects();
    app.ui.project = Some("bravo".into());
    app.run_command("project.status.passive");
    assert!(
        matches!(&api_calls(&app)[0], ApiCall::SetProjectStatus { project, status } if project == "bravo" && status == "passive")
    );
    app.state.model.projects.get_mut("bravo").unwrap().status = Some("passive".into());
    assert_eq!(app.project_status("bravo"), "passive");
    app.ui.project = Some(app.rail_project_ids()[0].clone()); // the open project's label is never dimmed
    let (buf, _) = draw(&mut app, 120, 36);
    let tile = app.hits.rect_of(&Target::RailProject(1)).unwrap();
    let cell = (tile.x..tile.right())
        .map(|x| &buf[(x, tile.y + 1)])
        .find(|c| c.symbol() == "B")
        .unwrap();
    assert!(cell.modifier.contains(ratatui::style::Modifier::DIM));
}

#[test]
fn removing_a_project_confirms_and_only_unregisters() {
    let mut app = three_projects();
    app.ui.project = Some("bravo".into());
    app.run_command("project.remove");
    let (_, rows) = draw(&mut app, 120, 36);
    let text = rows.join("\n");
    assert!(text.contains("Remove \"Bravo\" from Nexus?"), "{text}");
    assert!(text.contains("Files on disk are never touched."), "{text}");
    assert!(
        api_calls(&app).is_empty(),
        "nothing before the confirmation"
    );
    press(&mut app, &["right", "enter"]);
    assert!(
        matches!(&api_calls(&app)[..], [ApiCall::DeleteProject { project }] if project == "bravo")
    );
}

#[test]
fn the_project_picker_restores_an_inactive_project() {
    let mut app = three_projects();
    app.state.model.projects.get_mut("bravo").unwrap().status = Some("inactive".into());
    app.run_command("project.switch");
    let (_, rows) = draw(&mut app, 120, 36);
    let text = rows.join("\n");
    assert!(text.contains("inactive · "), "{text}");
    press(&mut app, &["down", "enter"]);
    let calls = api_calls(&app);
    assert!(
        calls.iter().any(|c| matches!(c, ApiCall::SetProjectStatus { project, status } if project == "bravo" && status == "current")),
        "{calls:?}\n{text}"
    );
}
