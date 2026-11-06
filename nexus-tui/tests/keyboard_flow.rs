//! PLAN 15.4: the keyboard flow from an empty composer, and the projects overview it ends in.

mod common;
use common::*;
use nexus_tui::actions::ApiCall;
use nexus_tui::app::{App, Effect};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{Focus, OverlayKind, Tab};

fn app() -> App {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 40));
    let first = app.current_project().unwrap();
    let mut base = app.state.model.projects[&first].clone();
    base.name = "Alpha".into();
    base.position = Some(0);
    app.state.model.projects.insert(first.clone(), base.clone());
    for (i, (name, status)) in [("Bravo", Some("passive")), ("Charlie", Some("inactive"))]
        .iter()
        .enumerate()
    {
        let mut p = base.clone();
        p.id = name.to_lowercase();
        p.name = (*name).into();
        p.created += 1.0 + i as f64;
        p.position = Some(1 + i as i64);
        p.status = status.map(Into::into);
        app.state.model.projects.insert(p.id.clone(), p);
    }
    let template_id = app
        .state
        .model
        .sessions
        .values()
        .find(|s| s.project_id == first)
        .unwrap()
        .id
        .clone();
    app.state
        .model
        .sessions
        .get_mut(&template_id)
        .unwrap()
        .title = "Alpha chat".into();
    app.state
        .model
        .sessions
        .get_mut(&template_id)
        .unwrap()
        .updated = 5.0;
    let template = app.state.model.sessions[&template_id].clone();
    for (id, project, title, archived, updated) in [
        ("s_alpha_old", first.as_str(), "Alpha old chat", true, 1.0),
        ("s_bravo", "bravo", "Bravo notes", false, 4.0),
        ("s_charlie", "charlie", "Charlie draft", false, 3.0),
    ] {
        let mut s = template.clone();
        s.id = id.into();
        s.project_id = project.into();
        s.title = title.into();
        s.archived = archived;
        s.updated = updated;
        s.parent_id = None;
        app.state.model.sessions.insert(id.into(), s);
    }
    app.ui.left_open = false;
    app.ui.focus = Focus::Composer;
    app.effects.clear();
    app
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

fn overview_text(app: &mut App) -> String {
    draw(app, 120, 40).1.join("\n")
}

#[test]
fn left_from_an_empty_composer_opens_the_sidebar_then_the_project_list_then_the_overview() {
    let mut app = app();
    press(&mut app, &["left"]);
    assert!(app.ui.left_open);
    assert_eq!((app.ui.focus, app.ui.tab), (Focus::Sidebar, Tab::Sessions));
    press(&mut app, &["left"]);
    assert_eq!((app.ui.focus, app.ui.tab), (Focus::Sidebar, Tab::Projects));
    assert!(app.holds("sidebar.projects_focus"));
    press(&mut app, &["left"]);
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::Projects(_))
    ));
}

#[test]
fn up_and_down_select_a_project_in_the_project_list() {
    let mut app = app();
    press(&mut app, &["left", "left"]);
    let at = app.cursor(Tab::Projects);
    press(&mut app, &["down"]);
    assert_eq!(app.cursor(Tab::Projects), at + 1);
    press(&mut app, &["up"]);
    assert_eq!(app.cursor(Tab::Projects), at);
}

#[test]
fn esc_and_right_step_back_out_in_reverse_order() {
    for out in ["esc", "right"] {
        let mut app = app();
        press(&mut app, &["left", "left", "left"]);
        press(&mut app, &[out]);
        assert!(app.ui.overlay.is_none(), "{out}: overview closes first");
        assert_eq!((app.ui.focus, app.ui.tab), (Focus::Sidebar, Tab::Projects));
        press(&mut app, &[out]);
        assert_eq!(
            (app.ui.focus, app.ui.tab),
            (Focus::Sidebar, Tab::Sessions),
            "{out}"
        );
        press(&mut app, &[out]);
        assert_eq!(app.ui.focus, Focus::Composer, "{out}");
    }
}

#[test]
fn left_with_text_in_the_composer_still_moves_the_cursor() {
    let mut app = app();
    typing(&mut app, "hello");
    press(&mut app, &["left"]);
    assert_eq!(app.ui.focus, Focus::Composer);
    assert!(!app.ui.left_open);
    assert_eq!(app.composer_ref().cursor(), (0, 4));
}

#[test]
fn the_overview_is_a_tree_of_active_sessions_with_inactive_projects_last() {
    let mut app = app();
    press(&mut app, &["left", "left", "left"]);
    let text = overview_text(&mut app);
    let line = |needle: &str| {
        text.lines()
            .position(|l| l.contains(needle))
            .unwrap_or_else(|| panic!("{needle}\n{text}"))
    };
    let group = text
        .lines()
        .position(|l| l.contains("│            Inactive "))
        .unwrap_or_else(|| panic!("group\n{text}"));
    assert!(
        line("Alpha  ·") < line("Bravo  ·")
            && line("Bravo  ·") < group
            && group < line("Charlie  ·"),
        "{text}"
    );
    assert!(text.contains("Bravo  · Passive"), "{text}");
    assert!(text.contains("└ Bravo notes · idle"), "{text}");
    assert!(text.contains("├ Alpha chat · idle"), "{text}");
    assert!(text.contains("Charlie  · Inactive  · r restore"), "{text}");
    // Archived sessions stay hidden until the project's toggle opens them.
    assert!(text.contains("▸ 1 archived"), "{text}");
    assert!(!text.contains("Alpha old chat"), "{text}");
}

#[test]
fn the_archived_toggle_shows_and_hides_a_projects_archived_sessions() {
    let mut app = app();
    press(&mut app, &["left", "left", "left"]);
    // Alpha, its one active session, then its archived toggle.
    press(&mut app, &["down", "down", "a"]);
    let text = overview_text(&mut app);
    assert!(
        text.contains("▾ 1 archived") && text.contains("Alpha old chat · archived"),
        "{text}"
    );
    press(&mut app, &["enter"]);
    let text = overview_text(&mut app);
    assert!(
        text.contains("▸ 1 archived") && !text.contains("Alpha old chat"),
        "{text}"
    );
}

#[test]
fn r_restores_an_inactive_project_in_one_action() {
    let mut app = app();
    press(&mut app, &["left", "left", "left", "end", "up"]);
    // The cursor is on Charlie's session: restore acts on its project.
    press(&mut app, &["r"]);
    let calls = api_calls(&app);
    assert!(
        calls.iter().any(|c| matches!(c, ApiCall::SetProjectStatus { project, status } if project == "charlie" && status == "current")),
        "{calls:?}"
    );
}

/// With `NEXUS_OVERVIEW_SHOTS=<dir>` the overview is written as SVG and text for the parity screenshots.
#[test]
fn overview_screenshots() {
    let Ok(dir) = std::env::var("NEXUS_OVERVIEW_SHOTS") else {
        return;
    };
    std::fs::create_dir_all(&dir).unwrap();
    let mut app = app();
    press(&mut app, &["left", "left", "left"]);
    for (name, keys) in [
        ("tui-overview", vec![]),
        ("tui-overview-archived", vec!["down", "down", "a"]),
    ] {
        press(&mut app, &keys);
        let (buffer, rows) = draw(&mut app, 120, 40);
        std::fs::write(
            format!("{dir}/{name}.svg"),
            nexus_tui::svg::to_svg(&buffer, "#ededed", "#0b0b0b"),
        )
        .unwrap();
        std::fs::write(format!("{dir}/{name}.txt"), rows.join("\n")).unwrap();
    }
}
