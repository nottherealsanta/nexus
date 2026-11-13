//! Tabs and splits (plan 45): groups of tabs in the centre, the Ctrl+X leader
//! commands, the group menu, drag and drop, dividers, the narrow collapse, the
//! shared file buffer, the unsaved prompt and the saved layout.

mod common;
use common::*;
use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::app::{App, Effect};
use nexus_tui::groups::{Kind, Side, Tab};
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Target;

fn app_at(w: u16, h: u16) -> App {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (w, h));
    draw(&mut app, w, h);
    app
}

fn mouse(app: &mut App, kind: MouseEventKind, x: u16, y: u16, modifiers: KeyModifiers) {
    app.handle_mouse(MouseEvent {
        kind,
        column: x,
        row: y,
        modifiers,
    });
}

fn left(kind: fn(MouseButton) -> MouseEventKind) -> MouseEventKind {
    kind(MouseButton::Left)
}

/// Open `name` as a file tab with `content` loaded, the way a Files row does.
fn open_file(app: &mut App, name: &str, content: &str) {
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": name, "kind": "file"}]})),
    });
    draw(app, 160, 48);
    app.click(Target::SideRow(0));
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: name.into(),
        file: true,
        data: Ok(serde_json::json!({"content": content, "revision": "rev1"})),
    });
    draw(app, 160, 48);
}

fn strip_text(app: &App, rows: &[String], group: u32) -> String {
    let rect = app
        .ui
        .groups
        .rects
        .iter()
        .find(|(g, _)| *g == group)
        .map(|(_, r)| *r)
        .unwrap();
    rows[rect.y as usize][..]
        .chars()
        .skip(rect.x as usize)
        .take(rect.width as usize)
        .collect()
}

#[test]
fn split_right_and_down_show_the_session_again_in_new_groups() {
    let mut app = app_at(160, 48);
    assert_eq!(app.groups().group_count(), 1);
    app.execute("layout.split_right");
    let (_, rows) = draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
    let rects = app.ui.groups.rects.clone();
    assert!(rects[0].1.x < rects[1].1.x, "{rects:?}");
    // Both strips name the session; the divider sits between them.
    for (g, _) in &rects {
        assert!(strip_text(&app, &rows, *g).contains("New session"), "{g}");
    }
    assert_eq!(app.ui.groups.dividers.len(), 1);
    assert!(app
        .hits
        .rect_of(&Target::GroupDivider(0))
        .is_some_and(|r| r.width == 1));
    // The focused (new) group holds the live page; the other is a stub.
    assert_eq!(app.groups().focused, rects[1].0);
    assert!(app.regions.composer.x >= rects[1].1.x);
    app.execute("layout.split_down");
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 3);
    assert_eq!(app.viewed_id(), Some(SESSION), "same session, same state");
}

#[test]
fn leader_keys_drive_the_layout_commands() {
    let mut app = app_at(160, 48);
    press(&mut app, &["ctrl+x", "\\"]);
    assert_eq!(app.groups().group_count(), 2);
    press(&mut app, &["ctrl+x", "-"]);
    assert_eq!(app.groups().group_count(), 3);
    draw(&mut app, 160, 48);
    // Focus by geometry: the third group is below the second; left goes to the first.
    let focused = app.groups().focused;
    press(&mut app, &["ctrl+x", "up"]);
    assert_ne!(app.groups().focused, focused);
    press(&mut app, &["ctrl+x", "left"]);
    assert_eq!(app.groups().focused, 1);
    // Join right merges group 1 into its neighbour: one split closes.
    press(&mut app, &["ctrl+x", "shift+right"]);
    assert_eq!(app.groups().group_count(), 2);
    // Close tab: the session's tab goes from the focused group (the group stays or goes with its last).
    let before = app.groups().group_count();
    press(&mut app, &["ctrl+x", "w"]);
    assert!(app.groups().group_count() <= before);
    // Anything else after the leader is not a layout command.
    let count = app.groups().group_count();
    press(&mut app, &["ctrl+x", "q"]);
    assert_eq!(app.groups().group_count(), count);
}

#[test]
fn a_split_that_would_leave_a_pane_under_the_minimum_is_refused_with_a_toast() {
    // 120 columns: the centre is 57 wide, enough for two 20-cell panes and no more.
    let mut app = app_at(120, 36);
    app.execute("layout.split_right");
    assert_eq!(app.groups().group_count(), 2);
    draw(&mut app, 120, 36);
    let before = app.groups().clone();
    app.execute("layout.split_right");
    assert_eq!(app.groups().group_count(), 2, "refused");
    assert_eq!(app.groups(), &before, "nothing changed");
    let toast = app
        .ui
        .alerts
        .toasts
        .last()
        .map(|t| t.text.clone())
        .unwrap_or_default();
    assert_eq!(
        toast,
        "Not enough room to split here (a pane needs at least 20 × 5)."
    );
    // Height: 5 + a gutter + 5 is the least a column split needs.
    let mut app = app_at(160, 11);
    app.execute("layout.split_down");
    assert_eq!(app.groups().group_count(), 1, "the centre is only 10 rows");
    let mut app = app_at(160, 12);
    app.execute("layout.split_down");
    assert_eq!(app.groups().group_count(), 2, "5 + 1 + 5 fits in 11");
}

#[test]
fn join_is_offered_only_with_a_neighbour_and_appends_the_tabs() {
    let mut app = app_at(160, 48);
    // One group: join is unavailable and absent from the menu.
    let join = app.manifest.command("layout.join_right").unwrap().clone();
    assert!(app.availability(&join).is_err());
    let labels =
        |app: &App| -> Vec<String> { app.group_menu_items().into_iter().map(|(l, _)| l).collect() };
    assert!(!labels(&app).contains(&"Join right".to_string()));
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    app.execute("layout.focus_left");
    assert!(app.availability(&join).is_ok());
    assert!(labels(&app).contains(&"Join right".to_string()));
    assert!(!labels(&app).contains(&"Join down".to_string()));
    let tabs_before = app.groups().tabs().len();
    app.execute("layout.join_right");
    assert_eq!(app.groups().group_count(), 1);
    // The same session was in both groups: it appears once.
    assert_eq!(app.groups().tabs().len(), tabs_before - 1);
    // With nothing to join, the toast says so.
    app.execute("layout.join_right");
    let say = app.ui.alerts.toasts.last().unwrap().text.clone();
    assert!(
        say.contains("to the right") || say.contains("right"),
        "{say}"
    );
}

#[test]
fn moving_a_tab_to_the_group_beside_it_and_closing_the_last_tab_remove_the_empty_group() {
    let mut app = app_at(160, 48);
    open_file(&mut app, "a.txt", "alpha\n");
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    // Group 2 holds a.txt (active); move it left: group 2 empties and goes.
    app.execute("layout.move_left");
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 1);
    let group = app.groups().focused_group();
    assert!(group.tabs.contains(&Tab::file("a.txt")));
    assert_eq!(group.active_tab(), Some(&Tab::file("a.txt")));
}

#[test]
fn the_group_menu_lists_the_actions_and_runs_them() {
    let mut app = app_at(160, 48);
    let g = app.groups().focused;
    let menu = app.hits.rect_of(&Target::GroupMenu(g)).expect("the glyph");
    click(&mut app, menu.x + 1, menu.y);
    let (_, rows) = draw(&mut app, 160, 48);
    let all = rows.join("\n");
    for label in ["Split right", "Split down", "New terminal", "Close tab"] {
        assert!(all.contains(label), "{label} in:\n{all}");
    }
    assert!(!all.contains("Join right"), "no neighbour, no join:\n{all}");
    // The first row is Split right.
    press(&mut app, &["enter"]);
    assert_eq!(app.groups().group_count(), 2);
    // The keyboard route: Ctrl+X then `.`.
    press(&mut app, &["ctrl+x", "left", "ctrl+x", "."]);
    let all = draw(&mut app, 160, 48).1.join("\n");
    assert!(
        all.contains("Join right") || all.contains("Join down"),
        "{all}"
    );
    press(&mut app, &["esc"]);
}

#[test]
fn dragging_a_tab_onto_a_body_edge_splits_and_shows_the_drop_zone() {
    let mut app = app_at(160, 48);
    // A second session tab so the first can leave its group.
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "second".into();
    other.title = "Second task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.open_session("second");
    draw(&mut app, 160, 48);
    let g = app.groups().focused;
    let tab = app.hits.rect_of(&Target::GroupTab(g, 1)).unwrap();
    let body = nexus_tui::layout::group_body(app.ui.groups.rects[0].1);
    // Press on the tab, move over the right edge of the body: the zone lights up.
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        tab.x + 2,
        tab.y,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        body.right() - 3,
        body.y + body.height / 2,
        KeyModifiers::NONE,
    );
    let (buf, _) = draw(&mut app, 160, 48);
    let zone = nexus_tui::groups::zone_rect(body, nexus_tui::groups::Zone::Right);
    let reversed = |x, y| {
        buf[(x, y)]
            .modifier
            .contains(ratatui::style::Modifier::REVERSED)
    };
    assert!(
        reversed(zone.x + zone.width / 2, zone.y + zone.height / 2),
        "the drop zone is highlighted"
    );
    assert!(!reversed(body.x + 2, body.y + 2), "only the zone is");
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        body.right() - 3,
        body.y + body.height / 2,
        KeyModifiers::NONE,
    );
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
    assert_eq!(
        app.groups().focused_group().tabs,
        vec![Tab::session("second")]
    );
    assert!(app.ui.groups.drag.is_none());
}

#[test]
fn tabs_reorder_in_a_strip_move_between_strips_and_alt_copies() {
    let mut app = app_at(160, 48);
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "second".into();
    other.title = "Second task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.open_session("second");
    draw(&mut app, 160, 48);
    let g = app.groups().focused;
    let first = app.hits.rect_of(&Target::GroupTab(g, 0)).unwrap();
    let second = app.hits.rect_of(&Target::GroupTab(g, 1)).unwrap();
    // Drag the first tab past the second: they swap.
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        first.x + 2,
        first.y,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        second.right() - 1,
        second.y,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        second.right() - 1,
        second.y,
        KeyModifiers::NONE,
    );
    assert_eq!(
        app.groups().focused_group().tabs,
        vec![Tab::session("second"), Tab::session(SESSION)]
    );
    // A split, then Alt+drag a tab to the other strip: a copy; plain drag: a move.
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    let rects = app.ui.groups.rects.clone();
    let (a, b) = (rects[0].0, rects[1].0);
    let tab = app
        .hits
        .rect_of(&Target::GroupTab(a, 1))
        .expect("tab 1 of group a");
    let target = rects[1].1;
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        tab.x + 2,
        tab.y,
        KeyModifiers::ALT,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        target.x + 3,
        target.y,
        KeyModifiers::ALT,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        target.x + 3,
        target.y,
        KeyModifiers::ALT,
    );
    assert!(
        app.groups().group(a).unwrap().tabs.len() == 2,
        "Alt copies, the source keeps its tab"
    );
    let _ = b;
    assert_eq!(app.groups().focused, b);
}

#[test]
fn escape_cancels_a_drag() {
    let mut app = app_at(160, 48);
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    let rects = app.ui.groups.rects.clone();
    let tab = app.hits.rect_of(&Target::GroupTab(rects[0].0, 0)).unwrap();
    let before = app.groups().clone();
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        tab.x + 2,
        tab.y,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        rects[1].1.x + 5,
        rects[1].1.y + 8,
        KeyModifiers::NONE,
    );
    assert!(app.ui.groups.drag.is_some());
    press(&mut app, &["esc"]);
    assert!(app.ui.groups.drag.is_none());
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        rects[1].1.x + 5,
        rects[1].1.y + 8,
        KeyModifiers::NONE,
    );
    assert_eq!(app.groups(), &before, "nothing dropped");
    let toast = app.ui.alerts.toasts.last().unwrap().text.clone();
    assert_eq!(toast, "Drag cancelled.");
}

#[test]
fn dragging_a_divider_resizes_within_the_minimum_and_resizes_the_terminal_pty() {
    let mut app = app_at(160, 48);
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    let d = app.ui.groups.dividers[0].rect;
    let before = app.ui.groups.rects[0].1.width;
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        d.x,
        d.y + 3,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        d.x + 20,
        d.y + 3,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        d.x + 20,
        d.y + 3,
        KeyModifiers::NONE,
    );
    draw(&mut app, 160, 48);
    let after = app.ui.groups.rects[0].1.width;
    assert!(after > before, "{before} -> {after}");
    // All the way left: the left pane keeps 20 cells.
    let d = app.ui.groups.dividers[0].rect;
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        d.x,
        d.y + 3,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        0,
        d.y + 3,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        0,
        d.y + 3,
        KeyModifiers::NONE,
    );
    draw(&mut app, 160, 48);
    assert_eq!(app.ui.groups.rects[0].1.width, nexus_tui::groups::MIN_W);

    // A terminal pane resizes its PTY when the pane does (controller only).
    let mut app = app_at(160, 48);
    app.ui
        .groups
        .placements
        .push_back(nexus_tui::app::tabs::Placement::Split(Side::Right));
    app.terminal_panel.terminals.push(
        serde_json::from_value(serde_json::json!({
            "id":"t1", "name":"Shell", "workspace":"/tmp", "status":"running",
            "controller":null, "rows":24, "cols":80
        }))
        .unwrap(),
    );
    app.terminal_created("t1".into());
    app.terminal_panel.attachment = Some("a1".into());
    app.terminal_controlled = true;
    draw(&mut app, 160, 48);
    app.terminal_resize();
    let size = |app: &App| {
        app.effects.iter().rev().find_map(|e| match e {
            Effect::Api(ApiCall::TerminalResize { rows, cols, .. }) => Some((*rows, *cols)),
            _ => None,
        })
    };
    let first = size(&app).expect("the first resize");
    // The request completes; the divider then moves and the next draw asks again.
    let call = app
        .effects
        .iter()
        .rev()
        .find_map(|e| match e {
            Effect::Api(c @ ApiCall::TerminalResize { .. }) => Some(c.clone()),
            _ => None,
        })
        .unwrap();
    app.apply_done(ActionDone {
        call,
        result: Ok(Done::default()),
    });
    app.effects.clear();
    let d = app.ui.groups.dividers[0].rect;
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        d.x,
        d.y + 3,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        d.x - 15,
        d.y + 3,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        d.x - 15,
        d.y + 3,
        KeyModifiers::NONE,
    );
    draw(&mut app, 160, 48);
    app.terminal_resize();
    let second = size(&app).expect("a resize after the pane changed");
    assert_ne!(first.1, second.1);
    assert!(second.1 > first.1, "the pane grew: {first:?} -> {second:?}");
}

#[test]
fn narrow_windows_show_only_the_focused_group_and_say_others_are_hidden() {
    let mut app = app_at(160, 48);
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    let saved = app.groups().clone();
    // A window too narrow for two 20-cell panes shows the focused one alone.
    app.ui.right_open = false;
    app.ui.left_open = false;
    let (_, rows) = draw(&mut app, 38, 24);
    assert_eq!(app.ui.groups.rects.len(), 1, "only the focused group");
    assert!(app.ui.groups.dividers.is_empty());
    assert_eq!(app.ui.groups.rects[0].0, app.groups().focused);
    let all = rows.join("\n");
    assert!(all.contains("1 group hidden"), "{all}");
    // The layout is untouched; widening brings every group back.
    assert_eq!(app.groups(), &saved);
    app.ui.left_open = true;
    app.ui.right_open = true;
    let (_, rows) = draw(&mut app, 160, 48);
    assert_eq!(app.ui.groups.rects.len(), 2);
    assert!(!rows.join("\n").contains("group hidden"));
}

#[test]
fn a_file_in_two_groups_shares_one_buffer_and_one_dirty_dot() {
    let mut app = app_at(160, 48);
    open_file(&mut app, "a.txt", "alpha\n");
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
    typing(&mut app, "ZED");
    let (_, rows) = draw(&mut app, 160, 48);
    // Both strips show the dirty dot on a.txt.
    for (g, _) in app.ui.groups.rects.clone() {
        let strip = strip_text(&app, &rows, g);
        assert!(strip.contains("a.txt") && strip.contains('●'), "{strip:?}");
    }
    // The other group's mirror shows the typed text: one buffer.
    let all = rows.join("\n");
    assert_eq!(
        all.matches("ZED").count(),
        2,
        "both panes show the text:\n{all}"
    );
    assert!(app.file_dirty("a.txt"));
}

#[test]
fn closing_a_dirty_file_asks_save_discard_cancel() {
    let mut app = app_at(160, 48);
    open_file(&mut app, "a.txt", "alpha\n");
    typing(&mut app, "Z");
    app.execute("layout.tab_close");
    let (_, rows) = draw(&mut app, 160, 48);
    let all = rows.join("\n");
    assert!(all.contains("Save changes to a.txt?"), "{all}");
    assert!(
        all.contains("Your changes will be lost if you discard them."),
        "{all}"
    );
    for button in ["[ Save ]", "[ Discard ]", "[ Cancel ]"] {
        assert!(all.contains(button), "{button}:\n{all}");
    }
    let save = all.find("[ Save ]").unwrap();
    let discard = all.find("[ Discard ]").unwrap();
    assert!(save < discard, "Save first");
    // Cancel keeps the tab and the text.
    press(&mut app, &["right", "right", "enter"]);
    assert!(app.ui.dialog.is_none());
    assert!(app.groups().contains(&Tab::file("a.txt")));
    assert!(app.file_dirty("a.txt"));
    // Save writes, and the tab closes when the save lands.
    app.execute("layout.tab_close");
    press(&mut app, &["enter"]);
    let call = app
        .effects
        .iter()
        .find_map(|e| match e {
            Effect::Api(c @ ApiCall::SaveFile { .. }) => Some(c.clone()),
            _ => None,
        })
        .expect("Save writes the file");
    assert!(
        app.groups().contains(&Tab::file("a.txt")),
        "not closed before the save lands"
    );
    app.apply_done(ActionDone {
        call,
        result: Ok(Done {
            saved: Some(serde_json::json!({"revision": "rev2"})),
            ..Done::default()
        }),
    });
    assert!(!app.groups().contains(&Tab::file("a.txt")));
    // Discard drops the text and closes.
    open_file(&mut app, "b.txt", "beta\n");
    typing(&mut app, "Q");
    app.execute("layout.tab_close");
    press(&mut app, &["right", "enter"]);
    assert!(!app.groups().contains(&Tab::file("b.txt")));
    assert!(!app.file_dirty("b.txt"));
}

#[test]
fn unsaved_text_survives_switching_to_another_tab_and_back() {
    let mut app = app_at(160, 48);
    open_file(&mut app, "a.txt", "alpha\n");
    typing(&mut app, "KEEP");
    assert!(app.file_dirty("a.txt"));
    // Back to the session tab, then to the file again.
    let g = app.groups().focused;
    let session_tab = app.hits.rect_of(&Target::GroupTab(g, 0)).unwrap();
    click(&mut app, session_tab.x + 2, session_tab.y);
    draw(&mut app, 160, 48);
    assert_eq!(app.active_tab().unwrap().kind, Kind::Session);
    assert!(
        app.file_dirty("a.txt"),
        "still dirty while it is not in front"
    );
    let file_tab = app.hits.rect_of(&Target::GroupTab(g, 1)).unwrap();
    click(&mut app, file_tab.x + 2, file_tab.y);
    // The read comes back from Core; the buffer is given back to the tab.
    let project = app.current_project().unwrap();
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: "a.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": "alpha\n", "revision": "rev1"})),
    });
    let (_, rows) = draw(&mut app, 160, 48);
    assert!(rows.join("\n").contains("KEEP"), "{}", rows.join("\n"));
    assert!(app.file_dirty("a.txt"));
}

#[test]
fn ctrl_click_on_a_file_row_opens_it_in_the_group_to_the_right() {
    let mut app = app_at(160, 48);
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "a.txt", "kind": "file"}]})),
    });
    draw(&mut app, 160, 48);
    let row = app.hits.rect_of(&Target::SideRow(0)).unwrap();
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        row.x + 4,
        row.y,
        KeyModifiers::CONTROL,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        row.x + 4,
        row.y,
        KeyModifiers::CONTROL,
    );
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
    assert_eq!(app.groups().focused_group().tabs, vec![Tab::file("a.txt")]);
    let rects = app.ui.groups.rects.clone();
    assert_eq!(
        app.groups().focused,
        rects[1].0,
        "to the right of the session"
    );
    // A plain click opens in the focused group: no third group.
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        row.x + 4,
        row.y,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        row.x + 4,
        row.y,
        KeyModifiers::NONE,
    );
    assert_eq!(app.groups().group_count(), 2);
}

#[test]
fn clicking_a_group_focuses_it_and_its_tab_becomes_the_live_page() {
    let mut app = app_at(160, 48);
    open_file(&mut app, "a.txt", "alpha\n");
    app.execute("layout.split_right"); // right group shows a.txt, focused
    draw(&mut app, 160, 48);
    let rects = app.ui.groups.rects.clone();
    // Click into the left group (it holds the file too, as the original).
    click(&mut app, rects[0].1.x + 10, rects[0].1.y + 10);
    assert_eq!(app.groups().focused, rects[0].0);
    // And onto its session tab: that group now shows the session page.
    let session = app.hits.rect_of(&Target::GroupTab(rects[0].0, 0)).unwrap();
    click(&mut app, session.x + 2, session.y);
    draw(&mut app, 160, 48);
    assert!(app.session_focused());
    assert!(app.regions.composer.x >= rects[0].1.x);
    assert!(app.regions.composer.right() <= rects[0].1.right());
}

#[test]
fn the_layout_is_saved_per_project_and_restored_without_dead_tabs() {
    let dir = std::env::temp_dir().join(format!("nexus-layout-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let new_app = || {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
        app.apply_prefs(
            &dir,
            nexus_tui::prefs::load(&dir),
            nexus_tui::prefs::Overrides::default(),
        );
        app
    };
    let mut app = new_app();
    draw(&mut app, 160, 48);
    app.execute("layout.split_right");
    // A file that is not on disk and a terminal that will be gone.
    app.ui.groups.layout.open(Tab::file("gone.txt"));
    app.ui.groups.layout.open(Tab::terminal("t9"));
    app.persist_prefs();
    let saved = std::fs::read_to_string(nexus_tui::prefs::path(&dir)).unwrap();
    assert!(saved.contains("\"layouts\""), "{saved}");
    assert!(saved.contains("gone.txt"), "{saved}");
    let before = app.groups().group_count();
    assert_eq!(before, 2);

    // A new run restores the two groups; the missing file is dropped quietly.
    let mut app = new_app();
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
    assert!(!app.groups().contains(&Tab::file("gone.txt")));
    assert!(
        app.groups().contains(&Tab::terminal("t9")),
        "kept until the terminals are listed"
    );
    let call = app
        .effects
        .iter()
        .find_map(|e| match e {
            Effect::Api(c @ ApiCall::TerminalList { .. }) => Some(c.clone()),
            _ => None,
        })
        .expect("the terminal list is read to check the tab");
    app.apply_done(ActionDone {
        call,
        result: Ok(Done {
            terminals: Some(Vec::new()),
            ..Done::default()
        }),
    });
    assert!(!app.groups().contains(&Tab::terminal("t9")));
    assert!(app.ui.alerts.toasts.is_empty(), "dropped quietly");
    // A corrupt layout in the file is ignored, not fatal.
    std::fs::write(
        nexus_tui::prefs::path(&dir),
        r#"{"layouts": {"x": {"root": 3}}, "ui": {"left_open": false}}"#,
    )
    .unwrap();
    let prefs = nexus_tui::prefs::load(&dir);
    assert!(prefs.layouts.is_empty());
    assert_eq!(prefs.ui.left_open, Some(false));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn closing_a_session_tab_in_a_split_keeps_the_other_group() {
    let mut app = app_at(160, 48);
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "second".into();
    other.title = "Second task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.open_session("second");
    draw(&mut app, 160, 48);
    // Drop the second tab on the right edge: two groups, one tab each.
    let g = app.groups().focused;
    let tab = app.hits.rect_of(&Target::GroupTab(g, 1)).unwrap();
    let body = nexus_tui::layout::group_body(app.ui.groups.rects[0].1);
    mouse(
        &mut app,
        left(MouseEventKind::Down),
        tab.x + 2,
        tab.y,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Drag),
        body.right() - 2,
        body.y + 6,
        KeyModifiers::NONE,
    );
    mouse(
        &mut app,
        left(MouseEventKind::Up),
        body.right() - 2,
        body.y + 6,
        KeyModifiers::NONE,
    );
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
    // Closing the focused tab removes its group; the other takes the whole centre.
    app.execute("session.tab_close");
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 1);
    assert_eq!(app.ui.groups.rects[0].1, app.regions.centre);
    assert_eq!(app.viewed_id(), Some(SESSION));
    assert_eq!(app.tab_ids(), vec![SESSION.to_owned()]);
}

fn add_terminal(app: &mut App, id: &str, placement: nexus_tui::app::tabs::Placement) {
    app.terminal_panel.terminals.push(
        serde_json::from_value(serde_json::json!({
            "id": id, "name": id, "workspace": "/tmp", "status": "running",
            "controller": null, "rows": 24, "cols": 80
        }))
        .unwrap(),
    );
    app.ui.groups.placements.push_back(placement);
    app.terminal_created(id.into());
}

#[test]
fn every_visible_terminal_streams_but_only_the_focused_one_has_the_keys() {
    use nexus_tui::app::tabs::Placement;
    use nexus_tui::terminal_panel::TerminalEvent;
    let mut app = app_at(160, 48);
    add_terminal(&mut app, "t1", Placement::Split(Side::Down));
    draw(&mut app, 160, 48);
    let g1 = app.terminal_panel.generation;
    app.terminal_event(TerminalEvent::Attached {
        generation: g1,
        attachment: "a1".into(),
    });
    app.effects.clear();
    add_terminal(&mut app, "t2", Placement::Split(Side::Right));
    draw(&mut app, 160, 48);
    // t1 was parked, not detached: no stop, no second attach for it.
    assert!(!app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::TerminalStop(_))));
    assert_eq!(app.terminal_panel.parked.len(), 1);
    assert_eq!(app.terminal_panel.selected.as_deref(), Some("t2"));
    let g2 = app.terminal_panel.generation;
    assert_ne!(g1, g2);
    // Each stream lands on its own screen.
    app.terminal_event(TerminalEvent::Frame {
        generation: g1,
        frame: serde_json::json!({"version":1,"seq":1,"type":"output","data":"b25l"}),
    });
    app.terminal_event(TerminalEvent::Frame {
        generation: g2,
        frame: serde_json::json!({"version":1,"seq":1,"type":"output","data":"dHdv"}),
    });
    let shown = draw(&mut app, 160, 48).1.join("\n");
    assert!(shown.contains("one") && shown.contains("two"), "{shown}");
    // Only the focused one has the keys.
    assert!(app.terminal_live());
    // Focusing the other group makes its terminal the attached one, taking control on its attachment.
    app.effects.clear();
    app.execute("layout.focus_left");
    draw(&mut app, 160, 48);
    assert_eq!(app.terminal_panel.selected.as_deref(), Some("t1"));
    assert!(app.effects.iter().any(|e| matches!(e,
        Effect::Api(ApiCall::TerminalControl { attachment, acquire: true, .. }) if attachment == "a1")));
    assert!(app.terminal_panel.parked.iter().any(|p| p.id == "t2"));
    assert!(draw(&mut app, 160, 48).1.join("\n").contains("two"));
    // A terminal that is no longer drawn detaches: close its tab.
    app.effects.clear();
    app.ui.groups.layout.remove_resource(&Tab::terminal("t2"));
    draw(&mut app, 160, 48);
    assert!(app.terminal_panel.parked.is_empty());
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::TerminalStop(g) if *g == g2)));
}

#[test]
fn a_session_group_becoming_active_parks_the_terminal_and_a_hidden_one_detaches() {
    use nexus_tui::app::tabs::Placement;
    let mut app = app_at(160, 48);
    add_terminal(&mut app, "t1", Placement::Split(Side::Down));
    draw(&mut app, 160, 48);
    app.effects.clear();
    app.execute("layout.focus_up");
    draw(&mut app, 160, 48);
    // Its group is still drawn: it keeps streaming, read-only.
    assert_eq!(app.terminal_panel.parked.len(), 1);
    assert!(app.terminal_panel.selected.is_none());
    assert!(!app.terminal_live());
    // Squeeze the window (10 rows cannot hold two panes) so only the focused group is drawn: the terminal detaches.
    app.ui.left_open = false;
    app.ui.right_open = false;
    draw(&mut app, 60, 11);
    draw(&mut app, 60, 11); // the next frame sees the collapse
    assert!(app.terminal_panel.parked.is_empty());
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::TerminalStop(_))));
}

#[test]
fn the_layout_follows_the_viewed_sessions_worktree() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    let listing = serde_json::json!({"worktrees": [{"path": "/wt/a"}, {"path": "/wt/b"}]});
    app.state.worktrees.insert(SESSION.into(), listing);
    app.state.model.sessions.get_mut(SESSION).unwrap().directory = "/wt/a".into();
    draw(&mut app, 160, 48);
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
    // The session moves to the other worktree: that worktree's layout (fresh) is shown.
    app.state.model.sessions.get_mut(SESSION).unwrap().directory = "/wt/b".into();
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 1);
    // And back: the first worktree's two groups return.
    app.state.model.sessions.get_mut(SESSION).unwrap().directory = "/wt/a".into();
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), 2);
}

/// Two session tabs side by side: SESSION focused (live), "second" in the other group.
fn two_sessions() -> App {
    let mut app = app_at(160, 48);
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "second".into();
    other.title = "Second task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.open_session("second");
    draw(&mut app, 160, 48);
    app.execute("layout.split_right");
    draw(&mut app, 160, 48);
    // Focus the left group on the first session; the right one keeps "second".
    app.ui.groups.layout.activate(1, 0);
    draw(&mut app, 160, 48);
    assert_eq!(app.viewed_id(), Some(SESSION));
    app
}

fn history_of_second(app: &App, from: &str, to: &str) -> nexus_tui::api::History {
    fn swap(v: &mut serde_json::Value, from: &str, to: &str) {
        match v {
            serde_json::Value::String(s) => *s = s.replace(from, to),
            serde_json::Value::Array(a) => a.iter_mut().for_each(|x| swap(x, from, to)),
            serde_json::Value::Object(o) => o.values_mut().for_each(|x| swap(x, from, to)),
            _ => {}
        }
    }
    let mut messages = app.messages().to_vec();
    for m in &mut messages {
        m.session_id = "second".into();
        m.parts.iter_mut().for_each(|p| swap(p, from, to));
    }
    nexus_tui::api::History {
        session_id: "second".into(),
        messages,
        ..Default::default()
    }
}

#[test]
fn an_unfocused_session_tab_shows_its_own_live_timeline() {
    let mut app = two_sessions();
    // The other group's session is watched and pinned.
    assert_eq!(app.ui.groups.watching, vec!["second".to_owned()]);
    assert!(app.state.model.pinned.contains(&"second".to_owned()));
    assert!(app.effects.iter().any(|e| matches!(e,
        Effect::Sync(nexus_tui::sync::Command::Watch(ids)) if ids == &vec!["second".to_owned()])));
    // Its history arrives and the group shows it, beside the live session.
    let history = history_of_second(&app, "Hello", "Greetings");
    app.apply_sync(SyncMsg::History(Box::new(history)));
    let (_, rows) = draw(&mut app, 160, 48);
    let right = app
        .ui
        .groups
        .rects
        .iter()
        .find(|(g, _)| *g != app.groups().focused)
        .unwrap()
        .1;
    let in_right = |rows: &[String], needle: &str| {
        rows.iter().any(|r| {
            r.chars()
                .skip(right.x as usize)
                .take(right.width as usize)
                .collect::<String>()
                .contains(needle)
        })
    };
    assert!(in_right(&rows, "Greetings"), "{}", rows.join("\n"));
    assert!(
        rows.join("\n").contains("Hello"),
        "the live session still shows its own"
    );
    // The viewed session is untouched by the other view.
    assert_eq!(app.viewed_id(), Some(SESSION));
    assert!(app
        .messages()
        .iter()
        .all(|m| !format!("{m:?}").contains("Greetings")));
    // A later update to the other session shows live too.
    let history = history_of_second(&app, "Hello", "Salutations");
    app.apply_sync(SyncMsg::History(Box::new(history)));
    let (_, rows) = draw(&mut app, 160, 48);
    assert!(in_right(&rows, "Salutations") && !in_right(&rows, "Greetings"));
    // Events reach the pinned view as they reach the selected one.
    let before = app
        .state
        .model
        .cached
        .iter()
        .find(|v| v.session_id == "second")
        .unwrap()
        .messages
        .len();
    let mut n = 0;
    for mut e in events(TURN) {
        if e.kind == "message.added" {
            n += 1;
            e.id += 10_000 + n;
            e.cursor = None;
            e.session_id = Some("second".into());
            e.data["session_id"] = "second".into();
            e.data["id"] = format!("late_{n}").into();
            e.data["seq"] = (100 + n).into();
            app.apply_sync(SyncMsg::Event(e));
        }
    }
    let after = app
        .state
        .model
        .cached
        .iter()
        .find(|v| v.session_id == "second")
        .unwrap()
        .messages
        .len();
    assert!(after > before, "{before} -> {after}");
}

#[test]
fn hiding_a_session_tab_unsubscribes_its_view() {
    let mut app = two_sessions();
    assert_eq!(app.ui.groups.watching.len(), 1);
    app.effects.clear();
    // Close the other group's tab: nothing shows "second" any more.
    app.ui
        .groups
        .layout
        .remove_resource(&Tab::session("second"));
    app.ui.tabs.retain(|t| t != "second");
    draw(&mut app, 160, 48);
    assert!(app.ui.groups.watching.is_empty());
    assert!(app.state.model.pinned.is_empty());
    assert!(app.effects.iter().any(|e| matches!(e,
        Effect::Sync(nexus_tui::sync::Command::Watch(ids)) if ids.is_empty())));
    // Squeezed so only the focused group is drawn: a hidden group's session is not watched.
    let mut app = two_sessions();
    app.ui.left_open = false;
    app.ui.right_open = false;
    draw(&mut app, 38, 24);
    draw(&mut app, 38, 24);
    assert!(app.ui.groups.watching.is_empty());
}
