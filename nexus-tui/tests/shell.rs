//! Shell buffer tests: Ratatui `TestBackend` frames at the three review sizes,
//! built from the real/documented fixtures.

mod common;
use common::*;
use nexus_tui::actions::ApiCall;
use nexus_tui::app::Effect;
use nexus_tui::manifest::Manifest;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Target;
use ratatui::style::Color;

const SIZES: [(u16, u16); 3] = [(80, 24), (120, 36), (160, 48)];

#[test]
fn session_sidebar_pins_new_search_and_archive_after_the_list() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let (_, rows) = draw(&mut app, 120, 36);
    let new_session = app
        .hits
        .rect_of(&Target::NewSession)
        .expect("new session hit");
    let search = app.hits.rect_of(&Target::RailSearch).expect("search hit");
    let session = app
        .hits
        .rect_of(&Target::SideRow(0))
        .expect("session row hit");
    let archive = app
        .hits
        .rect_of(&Target::ArchivedFilter)
        .expect("archive hit");
    assert!(session.y < new_session.y);
    assert_eq!(new_session.y + 1, search.y);
    assert_eq!(search.y + 1, archive.y);
    let text = rows.join("\n");
    assert!(text.contains("New session"), "{text}");
    assert!(text.contains("Search sessions"), "{text}");
    assert!(text.contains("Show archived"), "{text}");
}

#[test]
fn project_rail_pins_add_above_settings_with_sidebar_open_or_closed() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    for open in [true, false] {
        app.ui.left_open = open;
        draw(&mut app, 120, 36);
        let rail = app.regions.rail.expect("project rail");
        let add = app.hits.rect_of(&Target::AddProject).expect("add project");
        let settings = app
            .hits
            .rect_of(&Target::SettingsEntry)
            .expect("rail settings");
        assert_eq!(add.x, rail.x);
        assert_eq!(settings.x, rail.x);
        assert_eq!(add.y + 4, settings.y);
        assert_eq!(settings.bottom() + 1, rail.bottom());
    }
}

fn terminal_resource(id: &str, name: &str, status: &str, controller: Option<&str>) -> nexus_tui::terminal_api::TerminalResource {
    serde_json::from_value(serde_json::json!({
        "id": id, "name": name, "workspace": "/tmp", "status": status,
        "controller": controller, "rows": 24, "cols": 80
    }))
    .unwrap()
}

/// A terminal tab the way a created terminal gets one: listed, then opened in the focused group.
fn open_terminal(app: &mut nexus_tui::app::App, id: &str, name: &str, status: &str) {
    draw(app, 120, 36);
    app.terminal_panel
        .terminals
        .push(terminal_resource(id, name, status, None));
    app.terminal_created(id.to_owned());
}

fn created(app: &mut nexus_tui::app::App, id: &str, name: &str) {
    use nexus_tui::actions::{ActionDone, Done};
    let call = app
        .effects
        .iter()
        .rev()
        .find_map(|effect| match effect {
            Effect::Api(call @ ApiCall::TerminalCreate { .. }) => Some(call.clone()),
            _ => None,
        })
        .expect("create requested");
    app.apply_done(ActionDone {
        call,
        result: Ok(Done {
            terminal: Some(terminal_resource(id, name, "running", None)),
            ..Done::default()
        }),
    });
}

#[test]
fn terminal_command_opens_a_new_terminal_in_a_split_below() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    app.execute("terminal.open");
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::TerminalCreate { workspace, .. }) if !workspace.is_empty())));
    created(&mut app, "t1", "Build");
    let (_, rows) = draw(&mut app, 120, 36);
    // There is no dock: the terminal is a tab of a group below the session's.
    assert_eq!(app.groups().group_count(), 2);
    assert!(app.regions.panel.is_none());
    let body = app.regions.terminal.expect("the terminal's body");
    let session_group = app.ui.groups.rects[0].1;
    assert!(body.y > session_group.bottom(), "{body:?} below {session_group:?}");
    assert_eq!(app.active_tab().unwrap().kind, nexus_tui::groups::Kind::Terminal);
    let strip = &rows[(body.y - 1) as usize];
    assert!(strip.contains("Build"), "{strip:?}");
    let toolbar = &rows[body.y as usize];
    for glyph in ["✎", "[Attach]", "[Take control]", "⧉"] {
        assert!(toolbar.contains(glyph), "missing {glyph} in {toolbar:?}");
    }
    for gone in ["Terminals (", "Hide terminal panel", "Close panel"] {
        assert!(!rows.join("\n").contains(gone), "{gone}");
    }
    // The shortcut again focuses that tab instead of making another.
    app.execute("terminal.open");
    assert_eq!(app.groups().group_count(), 2);
}

#[test]
fn terminal_toolbar_tooltips_follow_the_pointer() {
    use crossterm::event::{KeyModifiers, MouseEvent, MouseEventKind};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Build", "running");
    draw(&mut app, 120, 36);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(!rows.join("\n").contains("Rename terminal"));
    for (target, label) in [
        (Target::TerminalRename, "Rename terminal"),
        (Target::TerminalTakeControl, "Take control"),
        (Target::TerminalCopy, "Copy output"),
    ] {
        let rect = app
            .hits
            .rect_of(&target)
            .unwrap_or_else(|| panic!("missing {target:?}"));
        app.handle_mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: rect.x + rect.width / 2,
            row: rect.y,
            modifiers: KeyModifiers::NONE,
        });
        let shown = draw(&mut app, 120, 36).1.join("\n");
        assert!(
            shown.contains(label),
            "missing tooltip {label:?} in:\n{shown}"
        );
    }
    let menu = app
        .hits
        .rect_of(&Target::GroupMenu(app.groups().focused))
        .expect("the group menu glyph");
    app.handle_mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: menu.x + 1,
        row: menu.y,
        modifiers: KeyModifiers::NONE,
    });
    assert!(draw(&mut app, 120, 36).1.join("\n").contains("Group actions"));
    let rect = app.hits.rect_of(&Target::TerminalTakeControl).unwrap();
    app.terminal_controlled = true;
    app.handle_mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: rect.x + rect.width / 2,
        row: rect.y,
        modifiers: KeyModifiers::NONE,
    });
    let shown = draw(&mut app, 120, 36).1.join("\n");
    assert!(shown.contains("[Release control]"), "{shown}");
}

#[test]
fn terminal_tabs_match_the_shared_scenario() {
    let scenario: serde_json::Value =
        serde_json::from_str(include_str!("../../interface/scenarios/terminal-dock.json")).unwrap();
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    draw(&mut app, 160, 48);
    for terminal in scenario["terminals"].as_array().unwrap() {
        let resource: nexus_tui::terminal_api::TerminalResource =
            serde_json::from_value(terminal.clone()).unwrap();
        let id = resource.id.clone();
        app.terminal_panel.terminals.push(resource);
        app.terminal_created(id);
    }
    let shown = draw(&mut app, 160, 48).1.join("\n");
    // The scenario's dock-era facts (`Terminals (1)`, the New / Close / Hide
    // actions) are gone; its terminal is a tab, and the agent's job is not one.
    assert!(shown.contains("Build"), "{shown}");
    for fact in ["Background job ready", "No terminal selected", "Close panel"] {
        assert!(!shown.contains(fact), "unexpected {fact:?} in:\n{shown}");
    }
    assert_eq!(app.terminal_panel.terminals.len(), 1);
    if let Ok(dir) = std::env::var("NEXUS_TERMINAL_DOCK_SHOTS") {
        let buffer = draw(&mut app, 144, 44).0;
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(
            format!("{dir}/tui-terminal-tab.svg"),
            nexus_tui::svg::to_svg(&buffer, fg, bg),
        )
        .unwrap();
    }
}

#[test]
fn terminal_list_drops_gone_tabs_and_new_creates_in_current_workspace() {
    use nexus_tui::actions::{ActionDone, Done};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    // The terminal list is read when a restored layout has terminal tabs to check.
    let call = ApiCall::TerminalList {
        project: app.current_project().unwrap(),
    };
    // A restored layout names a terminal that has since been closed.
    app.ui
        .groups
        .layout
        .open(nexus_tui::groups::Tab::terminal("closed"));
    app.apply_done(ActionDone {
        call,
        result: Ok(Done {
            terminals: Some([("closed", "closed"), ("exited", "exited"), ("live", "running")].into_iter().map(|(id, status)| {
                terminal_resource(id, "Terminal", status, None)
            }).collect()),
            ..Done::default()
        }),
    });
    assert_eq!(app.terminal_panel.terminals.len(), 2);
    assert!(!app
        .groups()
        .contains(&nexus_tui::groups::Tab::terminal("closed")));
    // Nothing opens by itself: terminals are tabs the user opened.
    assert!(app.terminal_panel.selected.is_none());
    app.execute("terminal.new");
    assert!(app.effects.iter().any(|effect| matches!(effect, Effect::Api(ApiCall::TerminalCreate { workspace, .. }) if !workspace.is_empty())));
}

#[test]
fn splitting_a_terminal_tab_creates_a_new_terminal_in_the_new_group() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    open_terminal(&mut app, "t1", "One", "running");
    draw(&mut app, 160, 48);
    let before = app.groups().group_count();
    app.effects.clear();
    app.execute("layout.split_right");
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::TerminalCreate { .. }))));
    created(&mut app, "t2", "Two");
    draw(&mut app, 160, 48);
    assert_eq!(app.groups().group_count(), before + 1);
    assert_eq!(
        app.active_tab(),
        Some(nexus_tui::groups::Tab::terminal("t2"))
    );
    // The first terminal's tab is still there, in its own group.
    assert!(app
        .groups()
        .contains(&nexus_tui::groups::Tab::terminal("t1")));
}

#[test]
fn selecting_terminal_attaches_and_renders_stream_output() {
    use nexus_tui::terminal_panel::TerminalEvent;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Shell", "running");
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::TerminalAttach { id, .. } if id == "t1")));
    let generation = app.terminal_panel.generation;
    app.terminal_event(TerminalEvent::Frame {
        generation,
        frame: serde_json::json!({"version":1,"seq":1,"type":"output","data":"aGVsbG8="}),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(rows.join("\n").contains("hello"));
}

#[test]
fn terminal_exit_revokes_input_and_updates_tab_immediately() {
    use nexus_tui::terminal_panel::TerminalEvent;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Shell", "running");
    app.terminal_controlled = true;
    app.terminal_panel
        .input_queue
        .push_back(b"must not send".to_vec());
    app.terminal_event(TerminalEvent::Frame {
        generation: app.terminal_panel.generation,
        frame: serde_json::json!({"version":1,"seq":1,"type":"exit","status":"exited"}),
    });
    assert!(!app.terminal_controlled);
    assert!(app.terminal_panel.input_queue.is_empty());
    assert_eq!(app.terminal_panel.terminals[0].status, "exited");
    assert_eq!(app.terminal_panel.terminals[0].controller, None);
    // The tab's dot is the ended one.
    let (_, rows) = draw(&mut app, 120, 36);
    let body = app.regions.terminal.unwrap();
    assert!(rows[(body.y - 1) as usize].contains("○"), "{:?}", rows[(body.y - 1) as usize]);
}

#[test]
fn terminal_attachment_automatically_requests_control() {
    use nexus_tui::terminal_panel::TerminalEvent;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    assert!(app.terminal_focused);
    let generation = app.terminal_panel.generation;
    app.terminal_event(TerminalEvent::Attached {
        generation,
        attachment: "a1".into(),
    });
    assert!(app.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::TerminalControl { acquire: true, attachment, .. }) if attachment == "a1")));
}

#[test]
fn terminal_rename_edits_the_selected_tab() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    app.execute("terminal.rename");
    app.ui.edit.as_mut().unwrap().buf.set_text("Build");
    press(&mut app, &["enter"]);
    assert!(app.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::TerminalRename { id, name }) if id == "t1" && name == "Build")));
}

#[test]
fn closing_a_running_terminal_tab_asks_and_then_terminates() {
    use nexus_tui::actions::{ActionDone, Done};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    app.execute("layout.tab_close");
    let dialog = app.ui.dialog.as_ref().unwrap();
    assert_eq!(dialog.title, "Close running terminal?");
    assert_eq!(dialog.confirm, "Close terminal");
    assert!(!app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::TerminalClose { .. }))));
    app.execute("alert.confirm");
    let call = app
        .effects
        .iter()
        .find_map(|e| match e {
            Effect::Api(call @ ApiCall::TerminalClose { id }) if id == "t1" => Some(call.clone()),
            _ => None,
        })
        .expect("the terminal is terminated");
    // The tab goes with the terminal.
    app.apply_done(ActionDone {
        call,
        result: Ok(Done::default()),
    });
    assert!(!app
        .groups()
        .contains(&nexus_tui::groups::Tab::terminal("t1")));
    assert!(app.terminal_panel.selected.is_none());
    // An exited terminal closes without the question.
    open_terminal(&mut app, "t2", "Done", "exited");
    app.effects.clear();
    app.execute("layout.tab_close");
    assert!(app.ui.dialog.is_none());
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::TerminalClose { id }) if id == "t2")));
}

#[test]
fn terminal_copy_requires_selection_and_explicit_action() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    use nexus_tui::terminal_panel::TerminalEvent;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    let generation = app.terminal_panel.generation;
    app.terminal_event(TerminalEvent::Frame { generation, frame: serde_json::json!({"version":1,"seq":1,"type":"output","data":"SGVsbG8bXTUyO2M7YzJWamNtVjAHRQ=="}) });
    draw(&mut app, 120, 36);
    let dock = app.regions.terminal.unwrap();
    let screen = nexus_tui::terminal_panel::screen_area(dock, true, false);
    let mouse = |kind, column| MouseEvent {
        kind,
        column,
        row: screen.y,
        modifiers: KeyModifiers::NONE,
    };
    app.handle_mouse(mouse(MouseEventKind::Down(MouseButton::Left), dock.x));
    app.handle_mouse(mouse(MouseEventKind::Drag(MouseButton::Left), dock.x + 4));
    app.handle_mouse(mouse(MouseEventKind::Up(MouseButton::Left), dock.x + 4));
    assert!(!app.effects.iter().any(|e| matches!(e, Effect::Copy(_))));
    app.execute("terminal.copy");
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Copy(text) if text == "Hello")));
}

#[test]
fn terminal_input_waits_for_ack_and_ctrl_bracket_returns_to_the_strip() {
    use nexus_tui::actions::{ActionDone, Done};
    use nexus_tui::terminal_panel::TerminalFocus;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    app.terminal_panel.attachment = Some("a1".into());
    app.terminal_controlled = true;
    app.terminal_focused = true;
    app.terminal_panel.focus = TerminalFocus::Screen;
    app.effects.clear();
    press(&mut app, &["a", "b"]);
    let calls: Vec<_> = app
        .effects
        .iter()
        .filter_map(|e| match e {
            Effect::Api(call @ ApiCall::TerminalInput { .. }) => Some(call.clone()),
            _ => None,
        })
        .collect();
    assert!(matches!(&calls[..], [ApiCall::TerminalInput { seq: 1, bytes, .. }] if bytes == b"a"));
    app.apply_done(ActionDone {
        call: calls[0].clone(),
        result: Ok(Done::default()),
    });
    assert!(app.effects.iter().any(
        |e| matches!(e, Effect::Api(ApiCall::TerminalInput { seq: 2, bytes, .. }) if bytes == b"b")
    ));
    app.handle_key(crossterm::event::KeyEvent::new(
        crossterm::event::KeyCode::Char(']'),
        crossterm::event::KeyModifiers::CONTROL,
    ));
    assert_eq!(app.terminal_panel.focus, TerminalFocus::List);
    assert!(!app.terminal_focused);
}

#[test]
fn terminal_ctrl_c_goes_to_shell_and_ctrl_bracket_is_not_sent() {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    app.terminal_panel.attachment = Some("a1".into());
    app.terminal_controlled = true;
    app.terminal_focused = true;
    app.effects.clear();
    app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::TerminalInput { bytes, .. }) if bytes == &[3])));
    assert!(!app.effects.iter().any(|e| matches!(e, Effect::Quit)));
    let before = app.effects.len();
    app.handle_key(KeyEvent::new(KeyCode::Char(']'), KeyModifiers::CONTROL));
    assert!(!app.terminal_focused);
    assert_eq!(app.effects.len(), before);
}

#[test]
fn terminal_toggle_key_focuses_the_terminal_tab_without_sending_and_leaves_ctrl_space_to_the_shell() {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    use nexus_tui::terminal_panel::TerminalFocus;

    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    let focus_terminal = |app: &mut nexus_tui::app::App| {
        app.terminal_panel.attachment = Some("a1".into());
        app.terminal_controlled = true;
        app.terminal_focused = true;
        app.terminal_panel.focus = TerminalFocus::Screen;
        app.effects.clear();
    };
    focus_terminal(&mut app);

    // Ctrl+Space is the shell's NUL, not a Nexus shortcut, even though a
    // legacy terminal may report Ctrl+` that way.
    app.handle_key(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::CONTROL));
    assert!(app.terminal_live());
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::TerminalInput { bytes, .. }) if bytes == &[0])));

    for key in ["ctrl+`", "ctrl+shift+`", "ctrl+~"] {
        focus_terminal(&mut app);
        let groups = app.groups().group_count();
        press(&mut app, &[key]);
        assert!(
            !app.effects
                .iter()
                .any(|e| matches!(e, Effect::Api(ApiCall::TerminalInput { .. }))),
            "{key} never reaches the shell"
        );
        assert!(
            !app.effects
                .iter()
                .any(|e| matches!(e, Effect::Api(ApiCall::TerminalClose { .. }))),
            "{key} does not terminate the terminal"
        );
        assert!(
            !app.effects.iter().any(|e| matches!(e, Effect::TerminalStop(_))),
            "{key} keeps the terminal attached"
        );
        assert!(app.terminal_live(), "{key} focuses the most recent terminal tab");
        assert_eq!(app.groups().group_count(), groups, "{key} opens no second terminal");
    }
}

#[test]
fn terminal_tab_c_takes_and_r_releases_control_when_the_strip_has_the_keys() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    app.terminal_panel.attachment = Some("a1".into());
    app.terminal_controlled = true;
    app.terminal_focused = false;
    app.effects.clear();
    press(&mut app, &["r"]);
    assert!(app.effects.iter().any(|effect| matches!(
        effect,
        Effect::Api(ApiCall::TerminalControl { acquire: false, .. })
    )));
    press(&mut app, &["enter"]);
    assert!(app.terminal_focused);
}

#[test]
fn terminal_resize_tracks_pane_geometry_only_under_control() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_terminal(&mut app, "t1", "Terminal", "running");
    app.terminal_panel.attachment = Some("a1".into());
    draw(&mut app, 120, 36);
    app.effects.clear();
    app.terminal_resize();
    assert!(!app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::TerminalResize { .. }))));
    app.terminal_controlled = true;
    app.terminal_resize();
    let first = app.effects.iter().find_map(|e| match e {
        Effect::Api(ApiCall::TerminalResize { rows, cols, .. }) => Some((*rows, *cols)),
        _ => None,
    });
    let (rows, cols) = first.expect("a resize under control");
    assert!(rows > 0 && cols > 0);
    // The pane's own size: the body less the toolbar row.
    let body = app.regions.terminal.unwrap();
    assert_eq!((rows, cols), (body.height - 1, body.width));
}

fn joined(rows: &[String]) -> String {
    rows.join("\n")
}

fn col_of(row: &str, needle: &str) -> usize {
    let at = row
        .find(needle)
        .unwrap_or_else(|| panic!("{needle:?} not in {row:?}"));
    nexus_tui::text::width(&row[..at])
}

#[test]
fn shell_anatomy_at_every_size_dark_and_mono() {
    let m = Manifest::load();
    for (w, h) in SIZES {
        for tier in [Tier::TrueColor, Tier::Mono] {
            let mut app = with_turn(opts(Mode::Dark, tier), (w, h));
            let (_, rows) = draw(&mut app, w, h);
            let all = joined(&rows);
            assert_eq!(rows.len(), h as usize);
            for r in &rows {
                assert!(nexus_tui::text::width(r) <= w as usize, "{w}x{h}: {r:?}");
            }
            // header: left toggle at the leading edge, right toggle trailing
            let head = &rows[0];
            assert!(
                head.trim_start().starts_with('\u{25e7}')
                    || head.trim_start().starts_with('\u{25a1}'),
                "{head:?}"
            );
            assert!(
                head.ends_with('\u{25a8}')
                    || head.ends_with('\u{25a1}')
                    || head.ends_with('\u{25e8}'),
                "{head:?}"
            );
            assert!(head.contains("New session"), "{head:?}");
            // the session's messages, in order
            let hi = all.find("hi").expect("user message");
            let hello = all.find("Hello").expect("assistant message");
            assert!(hi < hello);
            // Composer is visible; the old status footer is gone.
            assert!(all.contains("Type a message"), "{all}");
            assert!(!rows.last().unwrap().contains("✓ live"));
            if w >= 100 {
                // Tab icons in manifest order over the sidebar; Settings stays
                // pinned to the project rail, not a text row in the sidebar.
                let tabs: Vec<u16> = m
                    .navigation
                    .left_sidebar
                    .tab
                    .iter()
                    .filter(|t| t.id != "git" || app.regions.left.is_some_and(|r| r.width >= 33))
                    .map(|t| {
                        let tab = nexus_tui::ui::Tab::from_id(&t.id).unwrap();
                        let r = app.hits.rect_of(&Target::Tab(tab)).expect("tab icon");
                        assert_eq!(r.y, 0, "icons sit in the header band");
                        assert!(r.right() <= app.regions.left.unwrap().right());
                        r.x
                    })
                    .collect();
                assert!(tabs.windows(2).all(|p| p[0] < p[1]), "{tabs:?}");
                let settings = app
                    .hits
                    .rect_of(&Target::SettingsEntry)
                    .expect("rail Settings");
                assert_eq!(settings.x, app.regions.rail.unwrap().x);
                assert!(settings.y > h.saturating_sub(6));
                // right sidebar: session info only (Git status left it, plan 33)
                assert!(all.contains("SESSION") && !all.contains("GIT"));
            } else {
                assert!(!all.contains("SESSION"), "narrow shows one pane");
            }
        }
    }
}

#[test]
fn monochrome_frames_carry_no_colour() {
    for (w, h) in SIZES {
        let mut app = with_turn(opts(Mode::Dark, Tier::Mono), (w, h));
        let (buf, _) = draw(&mut app, w, h);
        for y in 0..h {
            for x in 0..w {
                let c = &buf[(x, y)];
                assert_eq!(
                    (c.fg, c.bg),
                    (Color::Reset, Color::Reset),
                    "({x},{y}) {:?}",
                    c.symbol()
                );
            }
        }
    }
}

#[test]
fn dark_frames_use_the_theme_file_surfaces() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let (buf, _) = draw(&mut app, 120, 36);
    // Timeline on the canvas, left sidebar on its own surface (interface/theme.toml).
    assert_eq!(buf[(60, 10)].bg, Color::Rgb(0x0b, 0x0b, 0x0b));
    assert_eq!(buf[(3, 20)].bg, Color::Rgb(0x14, 0x14, 0x14));
    let composer = app.regions.composer;
    let panel = Color::Rgb(0x1e, 0x1e, 0x1e);
    for y in [composer.y + 2, composer.bottom() - 4] {
        assert_eq!(buf[(composer.x + 6, y)].bg, panel, "input and control rows");
    }
    // No status band: nothing in the card is a darker surface (D11).
    assert!(
        !(composer.y..composer.bottom())
            .any(|y| buf[(composer.x + 6, y)].bg == Color::Rgb(0x14, 0x14, 0x14)),
        "no status band under the card"
    );
    // Half-row edges: the panel colour as the glyph over the canvas.
    let top = &buf[(composer.x + 6, composer.y + 1)];
    assert_eq!((top.symbol(), top.bg), (" ", panel));
    let bottom = &buf[(composer.x + 6, composer.bottom() - 3)];
    assert_eq!((bottom.symbol(), bottom.fg), ("\u{2580}", panel));
    assert!(
        buf.content
            .iter()
            .any(|cell| cell.bg == Color::Rgb(0x24, 0x24, 0x24)),
        "user block uses raised surface"
    );
    // The chrome: header band and both sidebars share surface.sidebar, the
    // surface step is the edge, so no rule is drawn between them (D01/D02).
    let sidebar = Color::Rgb(0x14, 0x14, 0x14);
    assert_eq!(buf[(110, 0)].bg, sidebar, "header band over the centre");
    assert_eq!(buf[(60, 0)].bg, sidebar);
    assert_eq!(buf[(100, 10)].bg, sidebar, "right sidebar");
    assert_eq!(buf[(27, 10)].symbol(), " ", "no edge rule in truecolor");
    assert_eq!(buf[(90, 10)].symbol(), " ");
    assert_eq!(buf[(32, 10)].bg, sidebar, "last column of the left sidebar");
    assert_eq!(
        buf[(33, 10)].bg,
        Color::Rgb(0x0b, 0x0b, 0x0b),
        "first timeline column"
    );
    // surfaces collapse in ANSI-16 and mono: the edge is drawn there
    for tier in [Tier::Ansi16, Tier::Mono] {
        let mut low = with_turn(opts(Mode::Dark, tier), (120, 36));
        let (buf, _) = draw(&mut low, 120, 36);
        assert_eq!(buf[(32, 10)].symbol(), "│", "{tier:?}");
        assert_eq!(buf[(90, 10)].symbol(), "│", "{tier:?}");
    }
    let mut light = with_turn(opts(Mode::Light, Tier::TrueColor), (120, 36));
    let (buf, _) = draw(&mut light, 120, 36);
    assert_eq!(buf[(60, 10)].bg, Color::Rgb(0xf7, 0xf7, 0xf5));
}

#[test]
fn ascii_mode_draws_only_ascii_chrome() {
    let mut o = opts(Mode::Dark, Tier::TrueColor);
    o.ascii = true;
    let mut app = with_turn(o, (120, 36));
    let (_, rows) = draw(&mut app, 120, 36);
    for r in &rows {
        assert!(r.is_ascii(), "{r:?}");
    }
    assert!(rows[0].trim_start().starts_with("[|"), "{:?}", rows[0]);
}

#[test]
fn narrow_layout_uses_drawers_and_keeps_the_anchors() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (80, 24));
    draw(&mut app, 80, 24);
    press(&mut app, &["ctrl+b"]);
    let (_, rows) = draw(&mut app, 80, 24);
    // the drawer carries the tab icons
    use nexus_tui::ui::Tab;
    for tab in [Tab::Sessions, Tab::Files] {
        assert!(app.hits.rect_of(&Target::Tab(tab)).is_some(), "{tab:?}");
    }
    assert!(
        rows[0].trim_start().starts_with('\u{25e7}'),
        "toggle shows open: {:?}",
        rows[0]
    );
    // Esc closes the drawer, and focus returns to the toggle that opened it
    press(&mut app, &["esc"]);
    assert!(app.ui.drawer.is_none());
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::ToggleLeft);
    let (_, rows) = draw(&mut app, 80, 24);
    assert!(!rows[1].contains("Files"));
    // the right toggle opens the right drawer from the trailing anchor
    press(&mut app, &["alt+b"]);
    let (_, rows) = draw(&mut app, 80, 24);
    assert!(joined(&rows).contains("SESSION"));
}

#[test]
fn hit_targets_follow_a_resize() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let old = app.hits.rect_of(&Target::ToggleRight).unwrap();
    assert_eq!(app.hits.at(old.x + 1, old.y), Some(&Target::ToggleRight));
    // resize: no stale target survives, even before the next draw
    app.set_size(80, 24);
    assert!(app.hits.is_empty());
    draw(&mut app, 80, 24);
    let new = app.hits.rect_of(&Target::ToggleRight).unwrap();
    assert!(new.x < old.x);
    assert_ne!(app.hits.at(old.x + 1, old.y), Some(&Target::ToggleRight));
    assert_eq!(app.hits.at(new.x, new.y), Some(&Target::ToggleRight));
    // clicking the new location works, the old one does not
    let before = app.ui.drawer;
    click(&mut app, old.x + 1, old.y);
    assert_eq!(app.ui.drawer, before);
    click(&mut app, new.x + 1, new.y);
    assert_eq!(app.ui.drawer, Some(nexus_tui::ui::Side::Right));
}

#[test]
fn mouse_and_keyboard_reach_the_same_places() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let left = app.hits.rect_of(&Target::ToggleLeft).unwrap();
    click(&mut app, left.x + 1, left.y);
    assert!(!app.ui.left_open);
    press(&mut app, &["ctrl+b"]);
    assert!(app.ui.left_open);
    draw(&mut app, 120, 36);
    let files = app
        .hits
        .rect_of(&Target::Tab(nexus_tui::ui::Tab::Files))
        .unwrap();
    click(&mut app, files.x, files.y);
    assert_eq!(app.ui.tab, nexus_tui::ui::Tab::Files);
    press(&mut app, &["alt+1"]);
    assert_eq!(app.ui.tab, nexus_tui::ui::Tab::Sessions);
}

#[test]
fn drafts_are_kept_per_session_and_survive_resize() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "draft for the session");
    press(&mut app, &["shift+enter"]);
    typing(&mut app, "second line");
    draw(&mut app, 120, 36);
    app.set_size(80, 24);
    let (_, rows) = draw(&mut app, 80, 24);
    assert!(joined(&rows).contains("draft for the session"));
    assert!(joined(&rows).contains("second line"));
    // another conversation (the unsent new session) has its own draft
    app.state.model.select(None);
    assert!(app.composer_ref().is_empty());
    typing(&mut app, "other");
    app.open_session(SESSION);
    assert_eq!(
        app.composer_ref().text(),
        "draft for the session\nsecond line"
    );
}

#[test]
fn bracketed_paste_lands_in_the_composer_with_newlines() {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.handle_paste("line one\r\nline two\n\u{65e5}\u{672c}\u{8a9e}");
    assert_eq!(
        app.composer_ref().expanded_text(),
        "line one\nline two\n\u{65e5}\u{672c}\u{8a9e}"
    );
    let (_, rows) = draw(&mut app, 120, 36);
    let shown = joined(&rows);
    assert!(shown.contains("line one"), "{shown}");
    assert!(
        !shown.contains("Pasted ~"),
        "a short multiline paste stays inline: {shown}"
    );
}

#[test]
fn wide_characters_never_overflow_a_row() {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (80, 24));
    let id = SESSION.to_owned();
    app.state.model.sessions.get_mut(&id).unwrap().title =
        "\u{65e5}\u{672c}\u{8a9e}\u{306e}\u{30bf}\u{30a4}\u{30c8}\u{30eb}".repeat(8);
    typing(&mut app, &"\u{65e5}\u{672c}".repeat(60));
    for (w, h) in SIZES {
        let (_, rows) = draw(&mut app, w, h);
        for r in rows {
            assert!(nexus_tui::text::width(&r) <= w as usize, "{r:?}");
        }
    }
}

#[test]
fn connection_states_become_banners_and_a_blocking_dialog() {
    use nexus_tui::api::{ApiVersion, Compat, Info, UpdateSide};
    use nexus_tui::state::ConnectionState as C;
    use nexus_tui::sync::SyncMsg;
    use std::time::Duration;

    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.apply_sync(SyncMsg::Handshake {
        info: Info::default(),
        compat: Compat::Unreported,
    });
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        joined(&rows).contains("does not report an API version"),
        "unreported banner"
    );

    app.apply_sync(SyncMsg::Connection(C::Reconnecting {
        attempt: 2,
        retry_in: Duration::from_secs(4),
        reason: "stream closed".into(),
    }));
    let (_, rows) = draw(&mut app, 140, 36);
    let all = joined(&rows);
    assert!(
        all.contains("Disconnected from Core") && all.contains("retrying in 4s"),
        "{all}"
    );
    assert!(all.contains("+1"), "stacked banners count");
    assert!(rows[0].contains("\u{2715} offline"), "{:?}", rows[0]);
    // read-only: mutating commands are disabled with a reason; the view stays
    assert!(all.contains("Hello"));
    let send = app.manifest.command("session.send").unwrap().clone();
    assert!(app.availability(&send).is_err());
    typing(&mut app, "hello");
    press(&mut app, &["enter"]);
    assert!(app.effects.is_empty(), "no API call while read-only");
    assert_eq!(app.composer_ref().text(), "hello", "draft kept");

    app.apply_sync(SyncMsg::Connection(C::Offline {
        reason: "refused".into(),
    }));
    assert!(joined(&draw(&mut app, 120, 36).1).contains("not reachable"));

    app.apply_sync(SyncMsg::Connection(C::Incompatible {
        core: "2.0".into(),
        update: UpdateSide::Client,
    }));
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(
        all.contains("Incompatible Core") && all.contains("update") && all.contains("nexus-tui"),
        "{all}"
    );
    let _ = ApiVersion { major: 1, minor: 0 };
    // blocking: Esc does nothing, nothing else reacts
    press(&mut app, &["esc", "ctrl+p"]);
    assert!(app.ui.overlay.is_none());
    assert!(joined(&draw(&mut app, 120, 36).1).contains("Incompatible Core"));
}

#[test]
fn waiting_card_sits_above_the_composer_and_offers_all_four_choices() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    // replay up to the approval only
    let mut app2 = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app2.open_session(SESSION);
    for e in events(TURN).into_iter().filter(|e| e.id <= 11) {
        app2.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    let (_, rows) = draw(&mut app2, 120, 36);
    let wait_row = rows
        .iter()
        .position(|r| r.contains("waiting"))
        .expect("waiting card");
    let composer_row = rows
        .iter()
        .position(|r| r.contains("Type a message") || r.contains("Answer the question above"))
        .unwrap();
    assert!(wait_row < composer_row);
    let all = joined(&rows);
    assert!(
        all.contains("Allow once")
            && all.contains("Allow for session")
            && all.contains("Deny with a note")
    );
    assert!(!all.contains("not offered by Core yet"), "{all}");
    assert!(all.contains("1 of 2"));
    // y allows once, through the focused card
    press(&mut app2, &["alt+w", "y"]);
    assert!(matches!(
        app2.effects.last(),
        Some(nexus_tui::app::Effect::Api(
            nexus_tui::actions::ApiCall::Reply { allow: true, .. }
        ))
    ));
    let _ = &mut app;
}

fn waiting_app() -> nexus_tui::app::App {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.open_session(SESSION);
    for e in events(TURN).into_iter().filter(|e| e.id <= 11) {
        app.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    draw(&mut app, 120, 36);
    app
}

#[test]
fn a_allows_for_the_session() {
    let mut app = waiting_app();
    press(&mut app, &["alt+w", "a"]);
    assert!(matches!(
        app.effects.last(),
        Some(nexus_tui::app::Effect::Api(
            nexus_tui::actions::ApiCall::Reply {
                allow: true,
                scope_session: true,
                note: None,
                ..
            }
        ))
    ));
}

#[test]
fn d_denies_with_the_composer_text_as_the_note() {
    let mut app = waiting_app();
    // no note yet: nothing is sent
    press(&mut app, &["alt+w", "d"]);
    assert!(!matches!(
        app.effects.last(),
        Some(nexus_tui::app::Effect::Api(
            nexus_tui::actions::ApiCall::Reply { .. }
        ))
    ));
    app.composer().insert_str("use ripgrep instead");
    press(&mut app, &["alt+w", "d"]);
    match app.effects.last() {
        Some(nexus_tui::app::Effect::Api(nexus_tui::actions::ApiCall::Reply {
            allow: false,
            note: Some(note),
            ..
        })) => assert_eq!(note, "use ripgrep instead"),
        other => panic!("{other:?}"),
    }
    assert!(app.composer().text().is_empty());
}

#[test]
fn composer_controls_have_hit_targets() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    for target in [
        Target::ComposerAgent,
        Target::ComposerModel,
        Target::ComposerEffort,
        Target::ComposerDirectory,
    ] {
        let r = app.hits.rect_of(&target).expect("composer control");
        assert_eq!(app.hits.at(r.x, r.y), Some(&target));
    }
}

#[test]
fn composer_uses_latest_usage_and_catalog_window() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    app.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/example".into());
    app.state.models = vec![
        serde_json::json!({"ref": "codex/example", "name": "Example Model", "context": 200000}),
    ];
    // Two requests: the newest one's input is the context in use.
    let mut turn = fixture_summary(
        "codex/example",
        serde_json::json!({"input": 1000, "cached": 0, "output": 100, "reasoning": 0}),
    );
    let mut newest = turn["requests"][0].clone();
    newest["id"] = "req_2".into();
    newest["usage"] =
        serde_json::json!({"input": 10000, "cached": 0, "output": 1000, "reasoning": 0});
    turn["requests"].as_array_mut().unwrap().push(newest);
    summarize(&mut app, 100, turn);
    let (_, rows) = draw(&mut app, 160, 48);
    let all = joined(&rows);
    assert!(all.contains("Example Model codex \u{b7} default"), "{all}");
    assert!(all.contains("10.0K \u{b7} 5.0%"), "{all}");
    app.state.models.clear();
    let (_, rows) = draw(&mut app, 160, 48);
    let all = joined(&rows);
    assert!(
        all.contains("example codex \u{b7} default") && all.contains("10.0K"),
        "{all}"
    );
    assert!(!all.contains("5%"), "no window, no share: {all}");
}

#[test]
fn composer_capitalises_the_agent_and_shows_price_tier_occupancy() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    let session = app.state.model.sessions.get_mut(SESSION).unwrap();
    session.agent = "orchestrate".into();
    session.model = Some("codex/example".into());
    app.state.models = vec![serde_json::json!({
        "ref": "codex/example", "name": "Example Model",
        "context": 1_000_000, "tiers": [200_000]
    })];
    summarize(
        &mut app,
        300,
        fixture_summary(
            "codex/example",
            serde_json::json!({"input": 52000, "cached": 0, "output": 0, "reasoning": 0}),
        ),
    );
    let (_, rows) = draw(&mut app, 160, 48);
    let row = rows
        .iter()
        .find(|row| row.contains("Example Model codex \u{b7} default"))
        .expect("composer control row");
    assert!(row.contains("Orchestrate"), "{row}");
    assert!(!row.contains("orchestrate"), "{row}");
    // Used and its share of the window; the palette hint is on the project strip.
    assert!(
        all_contains(&rows, "52.0K \u{b7} 26.0% \u{b7} 5.2%"),
        "{row}"
    );
    assert!(!all_contains(&rows, "ctrl+p commands"));

    let context = app
        .hits
        .rect_of(&Target::ComposerContext)
        .expect("context numbers");
    app.mouse = Some((context.x + 1, context.y));
    let (_, rows) = draw(&mut app, 160, 48);
    let all = joined(&rows);
    assert!(all.contains("Context: 52.0K of 1.0M tokens"), "{all}");
    assert!(
        all.contains("26.0% at 200.0K") && all.contains("5.2% of window"),
        "{all}"
    );
    assert!(all.contains("Estimated from the latest request"), "{all}");
}

fn all_contains(rows: &[String], needle: &str) -> bool {
    rows.iter().any(|row| row.contains(needle))
}

#[test]
fn composer_shows_a_pending_model_and_truncates_in_priority_order() {
    use nexus_tui::api::Event;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let session = app.state.model.sessions.get_mut(SESSION).unwrap();
    session.model = Some("codex/new".into());
    app.state.models = vec![
        serde_json::json!({"ref": "codex/example", "name": "Old Model", "context": 200000}),
        serde_json::json!({"ref": "codex/new", "name": "New Model", "context": 200000}),
    ];
    app.state.model.selected.as_mut().unwrap().scratch.extend([
        Event {
            id: 400,
            cursor: None,
            seq: None,
            time: 1.0,
            kind: "wire_request".into(),
            session_id: Some(SESSION.into()),
            data: serde_json::json!({"body": {"model": "example"}}),
        },
        Event {
            id: 401,
            cursor: None,
            seq: None,
            time: 2.0,
            kind: "usage".into(),
            session_id: Some(SESSION.into()),
            data: serde_json::json!({"input_tokens": 9000}),
        },
    ]);
    busy(&mut app);
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("(next call)"), "{all}");
    assert!(all.contains('\u{2192}'), "{all}");

    // Narrow: effort goes first, then the provider, then the model shortens,
    // while the agent stays; Esc interrupts, so there is no stop action.
    let (_, rows) = draw(&mut app, 60, 24);
    let all = joined(&rows);
    assert!(all.contains("Build"), "{all}");
    assert!(!all.contains("default"), "{all}");
}

#[test]
fn pending_queue_text_lines_up_with_the_input_and_its_marker_is_primary() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.inputs.insert(
        SESSION.into(),
        serde_json::json!({"held": false, "inputs": [
            {"id": "in_1", "mode": "queue", "parts": [{"type": "text", "text": "queued one"}]},
            {"id": "in_2", "mode": "steer", "parts": [{"type": "text", "text": "steer one"}]}
        ]}),
    );
    let (buf, rows) = draw(&mut app, 120, 36);
    let input_x = (app.regions.composer.x + 5) as usize;
    let primary = app.theme.color("text.primary").unwrap();
    for (text, mark) in [("queued one", "Q"), ("steer one", "S")] {
        let y = rows
            .iter()
            .position(|row| row.contains(text))
            .unwrap_or_else(|| panic!("{text:?} not in {rows:#?}"));
        let row = &rows[y];
        assert_eq!(col_of(row, text), input_x, "{row:?}");
        assert_eq!(col_of(row, mark), input_x - 2, "{row:?}");
        let cell = &buf[(input_x as u16 - 2, y as u16)];
        assert_eq!(cell.fg, primary, "{row:?}");
        assert!(
            !cell.modifier.contains(ratatui::style::Modifier::BOLD),
            "the S/Q mark is not bold: {row:?}"
        );
    }
}

#[test]
fn project_worktrees_supply_sidebar_and_location_branches_without_a_session_listing() {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.state.worktrees.clear();
    app.apply_sync(SyncMsg::ProjectWorktrees {
        project,
        data: Ok(serde_json::json!({
            "project_directory": "/work/nexus-home",
            "worktrees": [
                {"path": "/work/nexus-home", "branch": "trunk", "main": true},
                {"path": "/work/nexus-home/ws", "branch": "project-branch", "main": false}
            ]
        })),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("\u{2325} project-branch"), "{all}");
    assert!(
        all.contains("/work/nexus-home/ws  \u{2325} project-branch  ⑂"),
        "{all}"
    );
}

#[test]
fn files_tree_expands_with_git_marks_and_hides_ignored() {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [
            {"name": ".git", "kind": "directory", "ignored": true},
            {"name": "src", "kind": "directory"},
            {"name": "a.rs", "kind": "file", "git_status": "M"},
            {"name": "new.rs", "kind": "file", "git_status": "??"},
            {"name": "gone.rs", "kind": "file", "git_status": "D"},
            {"name": "target", "kind": "directory", "ignored": true}
        ]})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("\u{25b8} src"), "{all}");
    assert!(
        !all.contains(".git"),
        "the repository metadata is never a tree row"
    );
    let row = |name: &str| rows.iter().find(|row| row.contains(name)).unwrap();
    let row = |name: &str| row(name).chars().skip(6).take(28).collect::<String>();
    assert!(row("a.rs").trim_end().ends_with('M'), "{all}");
    assert!(row("new.rs").trim_end().ends_with('?'), "{all}");
    assert!(row("gone.rs").trim_end().ends_with('D'), "{all}");
    assert!(
        all.contains("target"),
        "ignored files show by default: {all}"
    );

    app.execute("files.toggle_ignored");
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        !joined(&rows).contains("target"),
        "ignored tree entry hidden"
    );
    app.execute("files.toggle_ignored");

    // Enter expands the selected folder and loads one level.
    press(&mut app, &["enter"]);
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: "src".into(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "b.rs", "kind": "file"}]})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("\u{25be} src"), "{all}");
    let child = rows.iter().find(|row| row.contains("b.rs")).unwrap();
    // one indent guide per ancestor, then the row indented under its folder
    assert!(
        child
            .chars()
            .skip(6)
            .collect::<String>()
            .starts_with(" \u{2502}"),
        "child has an indent guide: {child:?}"
    );
    assert!(
        col_of(child, "b.rs")
            > col_of(
                rows.iter().find(|r| r.contains("\u{25be} src")).unwrap(),
                "src"
            ),
        "child is indented: {child:?}"
    );

    press(&mut app, &["left"]);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(!joined(&rows).contains("b.rs"), "left collapses the folder");
}

#[test]
fn file_actions_copy_mention_and_switch_the_root() {
    use nexus_tui::app::Effect;
    use nexus_tui::sync::{Command, SyncMsg};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [
            {"name": "a.rs", "kind": "file", "git_status": "M"}
        ]})),
    });
    draw(&mut app, 120, 36);
    app.effects.clear();
    app.execute("copy.file_path");
    assert!(
        app.effects
            .iter()
            .any(|effect| matches!(effect, Effect::Copy(path) if path.ends_with("/a.rs"))),
        "{:?}",
        app.effects
    );
    assert!(app.composer_ref().is_empty());
    app.execute("file.insert_mention");
    assert_eq!(app.composer_ref().text(), "@a.rs ");
    app.effects.clear();
    app.execute("file.open_external");
    assert!(
        app.effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenEditor(path) if path.ends_with("/a.rs"))),
        "{:?}",
        app.effects
    );

    // Two checkouts make the root row meaningful.
    app.state.project_worktrees.insert(
        project.clone(),
        serde_json::json!({
            "project_directory": "/work/nexus-home",
            "worktrees": [
                {"path": "/work/nexus-home", "branch": "trunk", "main": true},
                {"path": "/work/nexus-home/ws", "branch": "feature", "main": false}
            ]
        }),
    );
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    // D16: the root header is the checkout name as written, then its branch
    assert!(all.contains("\u{25be} x"), "{all}");
    assert!(app.hits.rect_of(&Target::FilesRoot).is_some());
    app.effects.clear();
    app.execute("files.root_toggle");
    assert!(!app.ui.files_project_root);
    assert!(
        app.effects.iter().any(|effect| matches!(
            effect,
            Effect::Sync(Command::Files {
                session: Some(_),
                path,
                file: false,
                ..
            }) if path.is_empty()
        )),
        "{:?}",
        app.effects
    );
}

#[test]
fn new_session_location_uses_the_projects_own_worktrees() {
    use nexus_tui::sync::SyncMsg;
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    let path = app.state.model.projects[&project].path.clone();
    app.apply_sync(SyncMsg::ProjectWorktrees {
        project,
        data: Ok(serde_json::json!({
            "project_directory": path,
            "main": path,
            "worktrees": [{"path": path, "branch": "trunk", "main": true}]
        })),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("trunk"), "{all}");
    assert!(
        all.contains("trunk") && !all.contains("main checkout"),
        "a main checkout shows only its branch: {all}"
    );
    assert!(!all.contains("worktree"), "{all}");
}

#[test]
fn location_shows_only_the_current_worktree_and_invalidates_on_move() {
    use nexus_tui::api::Event;
    use nexus_tui::app::Effect;
    use nexus_tui::sync::{Command, SyncMsg};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "session_directory": "/work/nexus-home/ws",
            "worktrees": [{"path": "/work/nexus-home/ws", "branch": "feature", "main": false}]
        }),
    );
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("/work/nexus-home/ws  \u{2325} feature  ⑂"));
    assert!(app.hits.rect_of(&Target::ComposerWorktree).is_some());
    app.effects.clear();
    app.apply_sync(SyncMsg::Event(Event {
        id: 10000,
        cursor: None,
        seq: None,
        time: 1.0,
        kind: "session.directory_changed".into(),
        session_id: Some(SESSION.into()),
        data: serde_json::json!({"from": "/work/nexus-home/ws", "to": "/elsewhere"}),
    }));
    assert!(!app.state.worktrees.contains_key(SESSION));
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Sync(Command::Worktrees(id)) if id == SESSION)));
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(!joined(&rows).contains("/work/nexus-home/ws  \u{2325} feature  ⑂"));
}

#[test]
fn session_rows_show_project_and_cached_branch_and_both_lines_are_clickable() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "session_directory": "/work/nexus-home/ws",
            "worktrees": [{"path": "/work/nexus-home", "branch": "main"}]
        }),
    );
    let (_, rows) = draw(&mut app, 120, 36);
    let row = rows
        .iter()
        .position(|line| line.contains("⌥ main"))
        .unwrap();
    assert!(rows[row - 1].contains("New session"));
    assert_eq!(app.hits.at(10, row as u16), Some(&Target::SideRow(0)));
    assert_eq!(app.hits.at(10, row as u16 - 1), Some(&Target::SideRow(0)));
}

#[test]
fn files_list_open_read_and_return_to_timeline() {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "hello.txt", "kind": "file"}]})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("hello.txt"));
    app.click(Target::SideRow(0));
    assert!(app.ui.file_open);
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: "hello.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": "the file contents\n"})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("the file contents"));
    // Esc leaves a file alone (it is a tab); closing the tab returns to the session.
    press(&mut app, &["esc"]);
    assert!(app.ui.file_open);
    press(&mut app, &["alt+x"]);
    assert!(!app.ui.file_open);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("Hello"));
}

/// Opens `hello.txt` in the file view with `content` loaded.
fn open_file_view(content: &str) -> nexus_tui::app::App {
    open_named_file_view("hello.txt", content)
}

/// Opens `name` in the file view with `content` loaded.
fn open_named_file_view(name: &str, content: &str) -> nexus_tui::app::App {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": name, "kind": "file"}]})),
    });
    draw(&mut app, 120, 36);
    app.click(Target::SideRow(0));
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: name.into(),
        file: true,
        data: Ok(serde_json::json!({"content": content, "revision": "rev1"})),
    });
    draw(&mut app, 120, 36);
    app
}

fn save_calls(app: &nexus_tui::app::App) -> Vec<nexus_tui::actions::ApiCall> {
    app.effects
        .iter()
        .filter_map(|e| match e {
            nexus_tui::app::Effect::Api(c @ nexus_tui::actions::ApiCall::SaveFile { .. }) => {
                Some(c.clone())
            }
            _ => None,
        })
        .collect()
}

#[test]
fn the_editor_saves_with_the_revision_it_read_and_follows_the_new_one() {
    use nexus_tui::actions::{ActionDone, ApiCall, Done};
    let mut app = open_file_view("one\ntwo\n");
    assert!(app.ui.file_edit.is_some());
    assert!(joined(&draw(&mut app, 120, 36).1).contains("editing"));
    // Nothing to save yet.
    app.effects.clear();
    press(&mut app, &["ctrl+s"]);
    assert!(save_calls(&app).is_empty());
    typing(&mut app, "X");
    press(&mut app, &["enter"]);
    typing(&mut app, "y");
    assert!(app.ui.file_edit.as_ref().unwrap().dirty());
    press(&mut app, &["ctrl+s"]);
    let calls = save_calls(&app);
    let ApiCall::SaveFile {
        content,
        revision,
        path,
        ..
    } = &calls[0]
    else {
        panic!()
    };
    assert_eq!((path.as_str(), revision.as_str()), ("hello.txt", "rev1"));
    // The editor opens with the cursor at the top of the file.
    assert_eq!(content, "X\nyone\ntwo\n");
    app.apply_done(ActionDone {
        call: calls[0].clone(),
        result: Ok(Done {
            saved: Some(serde_json::json!({"revision": "rev2"})),
            ..Done::default()
        }),
    });
    let edit = app.ui.file_edit.as_ref().unwrap();
    assert_eq!(edit.revision, "rev2");
    assert!(!edit.dirty());
    // A second save is a new write against rev2.
    typing(&mut app, "z");
    app.effects.clear();
    press(&mut app, &["ctrl+s"]);
    let ApiCall::SaveFile { revision, .. } = &save_calls(&app)[0] else {
        panic!()
    };
    assert_eq!(revision, "rev2");
}

#[test]
fn a_stale_save_keeps_the_text_and_a_reload_replaces_it() {
    use nexus_tui::actions::{ActionDone, Done};
    use nexus_tui::sync::SyncMsg;
    let mut app = open_file_view("one\n");
    typing(&mut app, "mine");
    press(&mut app, &["ctrl+s"]);
    let call = save_calls(&app).remove(0);
    app.apply_done(ActionDone {
        call,
        result: Ok(Done {
            conflict: Some("the file changed".into()),
            ..Done::default()
        }),
    });
    assert!(app.ui.file_edit.as_ref().unwrap().stale);
    assert!(joined(&draw(&mut app, 120, 36).1).contains("changed on disk"));
    assert!(app
        .ui
        .file_edit
        .as_ref()
        .unwrap()
        .buf
        .text()
        .contains("mine"));
    // Reload asks first because the text is unsaved; confirming reads the file again.
    app.run_command("editor.reload");
    assert!(app.ui.dialog.is_some());
    press(&mut app, &["right", "enter"]);
    assert!(app.ui.file_edit.as_ref().unwrap().reloading);
    let project = app.current_project().unwrap();
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: "hello.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": "theirs\n", "revision": "rev9"})),
    });
    let edit = app.ui.file_edit.as_ref().unwrap();
    assert_eq!(
        (edit.buf.text().as_str(), edit.revision.as_str()),
        ("theirs\n", "rev9")
    );
    assert!(!edit.stale && !edit.dirty());
}

#[test]
fn closing_a_clean_editor_tab_is_immediate_and_esc_leaves_it_open() {
    let mut app = open_file_view("one\n");
    press(&mut app, &["esc"]);
    assert!(app.ui.file_open, "Esc no longer closes a file");
    press(&mut app, &["alt+x"]);
    assert!(app.ui.file_edit.is_none() && !app.ui.file_open);
    let mut app = open_file_view("one\n");
    typing(&mut app, "x");
    press(&mut app, &["alt+x"]);
    assert!(app.ui.dialog.is_some() && app.ui.file_edit.is_some());
    // Cancel (the last button) keeps the text and the tab.
    press(&mut app, &["left", "enter"]);
    assert!(app.ui.dialog.is_none() && app.ui.file_edit.is_some());
    press(&mut app, &["alt+x", "right", "enter"]);
    assert!(app.ui.file_edit.is_none() && !app.ui.file_open);
}

#[test]
fn an_outside_change_reloads_a_clean_editor_and_warns_a_dirty_one() {
    use nexus_tui::sync::SyncMsg;
    let mut app = open_file_view("one\n");
    let project = app.current_project().unwrap();
    let changed = |rev: &str| serde_json::json!({"project_id": project, "path": "hello.txt", "revision": rev, "source": "editor"});
    app.effects.clear();
    app.file_changed_for_test(&changed("rev5"));
    assert!(
        app.ui.file_edit.as_ref().unwrap().reloading,
        "a clean editor follows the file"
    );
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: "hello.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": "new\n", "revision": "rev5"})),
    });
    typing(&mut app, "m");
    app.file_changed_for_test(&changed("rev6"));
    let edit = app.ui.file_edit.as_ref().unwrap();
    assert!(edit.stale && edit.buf.text().contains('m') && !edit.reloading);
}

#[test]
fn editor_find_cycles_matches_and_ends_before_closing() {
    let mut app = open_file_view("alpha\nbeta needle\ngamma\nNeedle again\n");
    press(&mut app, &["ctrl+f"]);
    typing(&mut app, "needle");
    assert_eq!(app.ui.file_search.as_ref().unwrap().query, "needle");
    press(&mut app, &["enter"]);
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(0));
    press(&mut app, &["enter"]);
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(1));
    press(&mut app, &["enter"]);
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(0));
    press(&mut app, &["shift+enter"]);
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(1));
    typing(&mut app, "n");
    assert_eq!(app.ui.file_search.as_ref().unwrap().query, "needlen");
    press(&mut app, &["esc"]);
    assert!(app.ui.file_search.is_none() && app.ui.file_open);
    assert!(!app.ui.file_edit.as_ref().unwrap().dirty());
    press(&mut app, &["esc"]);
    assert!(app.ui.file_open, "a second Esc leaves the file tab alone");
}

#[test]
fn editor_find_case_sensitive_replace_all_and_undo() {
    let mut app = open_file_view("needle Needle needle\n");
    press(&mut app, &["ctrl+f"]);
    typing(&mut app, "needle");
    press(&mut app, &["alt+c", "tab"]);
    typing(&mut app, "thread");
    press(&mut app, &["ctrl+shift+r"]);
    assert_eq!(
        app.ui.file_edit.as_ref().unwrap().buf.text(),
        "thread Needle thread\n"
    );
    press(&mut app, &["esc", "ctrl+z", "ctrl+z"]);
    assert_eq!(
        app.ui.file_edit.as_ref().unwrap().buf.text(),
        "needle Needle needle\n"
    );
}

#[test]
fn editor_find_reveals_matches_outside_the_initial_viewport() {
    let lines: String = (1..=100).map(|n| format!("line {n}\n")).collect();
    let mut app = open_file_view(&format!("{lines}target here\n"));
    assert!(!joined(&draw(&mut app, 120, 36).1).contains("target here"));
    press(&mut app, &["ctrl+f"]);
    typing(&mut app, "TARGET");
    press(&mut app, &["enter"]);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("target here"), "{rows:?}");
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(0));
    assert!(!app.ui.file_edit.as_ref().unwrap().dirty());
}

#[test]
fn old_nexus_shimmer_keeps_graphemes_and_reduced_motion_static() {
    use nexus_tui::theme::shimmer;
    use ratatui::style::Style;
    let base = Style::default().fg(Color::Rgb(80, 80, 80));
    let peak = Style::default().fg(Color::Rgb(220, 220, 220));
    let text = "a👩‍💻e\u{301}";
    let animated = shimmer(text, 25, true, base, peak);
    assert_eq!(animated.len(), 3);
    assert_eq!(
        animated
            .iter()
            .map(|s| s.content.as_ref())
            .collect::<String>(),
        text
    );
    let static_text = shimmer(text, 25, false, base, peak);
    assert_eq!(static_text.len(), 1);
    assert_eq!(static_text[0].style, base);
}

#[test]
fn settings_is_a_centred_modal_over_the_session() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.execute("app.settings");
    let (buf, rows) = draw(&mut app, 120, 36);
    let top = raised_box(&app, &buf).y as usize + 1;
    assert!(!rows[top].contains("esc"));
    assert!(rows[top + 2].contains("Filter settings"));
    assert!(joined(&rows).contains("Appearance"));
    assert!(joined(&rows).contains("esc close"));
    press(&mut app, &["esc"]);
    assert_eq!(app.ui.view, nexus_tui::ui::View::Session);
}

#[test]
fn settings_search_filters_rows_and_esc_clears_then_closes() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.execute("app.settings");
    press(&mut app, &["m", "o", "d", "e", "l"]);
    let (_, rows) = draw(&mut app, 120, 36);
    let text = joined(&rows);
    assert!(text.contains("model"));
    // Model lists live on the Agents page: its entry matches.
    assert!(text.contains("AGENTS"), "{text}");
    assert!(!text.contains("MODELS"));
    assert!(!text.contains("Dictation"));
    press(&mut app, &["esc"]);
    assert!(app.ui.settings_search.text().is_empty());
    assert_eq!(app.ui.view, nexus_tui::ui::View::Settings);
    press(&mut app, &["esc"]); // back to the section rail
    assert!(app.ui.settings_nav_focus);
    press(&mut app, &["esc"]);
    assert_eq!(app.ui.view, nexus_tui::ui::View::Session);
}

#[test]
fn sidebar_tracks_unread_and_error_until_opened_or_retried() {
    use nexus_tui::api::Frame;
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "other".into();
    other.title = "Other session".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    let message = serde_json::json!({"id": 200, "type": "message.added", "session_id": "other", "cursor": "log_c915bfc9b6d826c4:200", "data": {"id": "m_other", "session_id": "other", "seq": 1, "created": 1.0, "role": "assistant", "parts": [{"type": "text", "text": "done"}]}});
    if let Ok(Frame::Event(event)) = Frame::from_value("message.added", None, &message) {
        app.apply_sync(SyncMsg::Event(event));
    }
    assert!(app.ui.unread_sessions.contains("other"));
    let stopped = serde_json::json!({"id": 200, "type": "session.error", "session_id": "other", "cursor": "log_c915bfc9b6d826c4:200", "data": {"kind": "interrupted", "message": "interrupted by the user"}});
    if let Ok(Frame::Event(event)) = Frame::from_value("session.error", None, &stopped) {
        app.apply_sync(SyncMsg::Event(event));
    }
    assert!(!app.ui.failed_sessions.contains("other"));
    let error = serde_json::json!({"id": 201,"type": "session.error", "session_id": "other", "cursor": "log_c915bfc9b6d826c4:201", "data": {"error": "failed"}});
    if let Ok(Frame::Event(event)) = Frame::from_value("session.error", None, &error) {
        app.apply_sync(SyncMsg::Event(event));
    }
    assert!(app.ui.failed_sessions.contains("other"));
    app.open_session("other");
    assert!(!app.ui.unread_sessions.contains("other"));
    let busy = serde_json::json!({"id": 202, "type": "session.status", "session_id": "other", "cursor": "log_c915bfc9b6d826c4:202", "data": {"status": "busy", "turn_id": "t"}});
    if let Ok(Frame::Event(event)) = Frame::from_value("session.status", None, &busy) {
        app.apply_sync(SyncMsg::Event(event));
    }
    assert!(!app.ui.failed_sessions.contains("other"));
}

#[test]
fn a_failed_child_does_not_mark_its_parent_failed() {
    use nexus_tui::api::Frame;
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut child = app.state.model.sessions[SESSION].clone();
    child.id = "child".into();
    child.title = "Build".into();
    child.parent_id = Some(SESSION.into());
    app.state.model.sessions.insert(child.id.clone(), child);
    let error = serde_json::json!({"id": 210, "type": "session.error", "session_id": "child", "cursor": "log_c915bfc9b6d826c4:210", "data": {"error": "HTTP 429"}});
    if let Ok(Frame::Event(event)) = Frame::from_value("session.error", None, &error) {
        app.apply_sync(SyncMsg::Event(event));
    }
    assert!(app.ui.failed_sessions.contains("child"));
    assert!(!app.ui.failed_sessions.contains(SESSION));
    assert_ne!(app.session_state(SESSION), "failed");
}

#[test]
fn long_session_titles_end_in_an_ellipsis_in_every_tier() {
    // D03 shows `han…`: the old truecolour fade gave way to the ellipsis.
    for tier in [Tier::TrueColor, Tier::Ansi256, Tier::Ansi16] {
        let mut app = with_turn(opts(Mode::Dark, tier), (120, 36));
        app.state.model.sessions.get_mut(SESSION).unwrap().title =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".into();
        let (_, rows) = draw(&mut app, 120, 36);
        let row = rows
            .iter()
            .skip(1)
            .find(|row| {
                row.chars()
                    .take(34)
                    .collect::<String>()
                    .contains("ABCDEFGHIJ")
            })
            .unwrap_or_else(|| panic!("{tier:?}: {rows:#?}"));
        let side: String = row.chars().take(34).collect();
        assert!(side.contains('…'), "{tier:?}: {side:?}");
        assert!(!side.contains("XYZ"), "{tier:?}: {side:?}");
    }
}

#[test]
fn selected_session_row_takes_the_rail_active_block_and_bar() {
    use ratatui::style::Modifier;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.model.sessions.get_mut(SESSION).unwrap().title = "Selected title".into();
    let (buf, rows) = draw(&mut app, 120, 36);
    let y = rows
        .iter()
        .position(|row| {
            row.chars()
                .take(34)
                .collect::<String>()
                .contains("Selected title")
        })
        .expect("the selected session row") as u16;
    let left = app.regions.left.expect("sidebar");
    let active = app.theme.bg("interactive.active");
    let bar = app.theme.fg("interactive.active_bar");
    let sidebar = app.theme.bg("surface.sidebar");
    assert_ne!(
        active.bg, sidebar.bg,
        "the block is lighter than the sidebar"
    );
    let x = (left.x..left.right())
        .find(|x| buf[(*x, y)].symbol() == "\u{258f}")
        .expect("the left accent bar");
    assert_eq!(buf[(x, y)].fg, bar.fg.unwrap());
    // Both lines of the row sit on the block, to its right edge.
    for line in [y, y + 1] {
        assert_eq!(buf[(x + 4, line)].bg, active.bg.unwrap(), "line {line}");
        assert_eq!(
            buf[(left.right() - 2, line)].bg,
            active.bg.unwrap(),
            "line {line}"
        );
    }
    // The title is white (`text.highlight`) and not bold.
    let tx = (x..left.right())
        .find(|x| buf[(*x, y)].symbol() == "S")
        .expect("title");
    let cell = &buf[(tx, y)];
    assert_eq!(cell.fg, app.theme.fg("text.highlight").fg.unwrap());
    assert!(!cell.modifier.contains(Modifier::BOLD));
}

#[test]
fn worktree_groups_keep_cursor_on_the_same_session_when_branch_data_arrives() {
    use nexus_tui::api::Frame;
    use nexus_tui::sync::SyncMsg;
    use nexus_tui::ui::Tab;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "worktree-session".into();
    other.directory = "/work/aaa".into();
    other.title = "Worktree task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    let index = app
        .side_entries()
        .iter()
        .position(|entry| entry.id == "worktree-session")
        .unwrap();
    app.ui.cursor.insert(Tab::Sessions, index);
    let value = serde_json::json!({"id": 220, "type": "worktree.listed", "session_id": "worktree-session", "cursor": "log_c915bfc9b6d826c4:220", "data": {
        "session_directory": "/work/aaa", "worktrees": [{"path": "/work/aaa", "branch": "feature", "main": false}]
    }});
    if let Ok(Frame::Event(event)) = Frame::from_value("worktree.listed", None, &value) {
        app.apply_sync(SyncMsg::Event(event));
    }
    let entries = app.side_entries();
    assert_eq!(entries[app.cursor(Tab::Sessions)].id, "worktree-session");
    assert_eq!(
        entries[app.cursor(Tab::Sessions)].tree,
        Some(("feature".to_owned(), "/work/aaa".to_owned()))
    );
    assert!(entries
        .iter()
        .any(|entry| entry.id == SESSION && entry.project.is_none() && entry.tree.is_none()));
}

#[test]
fn system_theme_can_be_selected_and_restored() {
    use nexus_tui::prefs::{self, Overrides, Prefs};
    let home = std::env::temp_dir().join(format!("nexus-theme-system-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&home).unwrap();
    let mut saved = Prefs::default();
    saved.theme.mode = Some("dark".into());
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.apply_prefs(&home, saved, Overrides::default());
    app.execute("app.settings");
    press(&mut app, &["t", "h", "e", "m", "e"]);
    let (_, rows) = draw(&mut app, 120, 36);
    let text = joined(&rows);
    assert!(text.contains("system") && text.contains("light") && text.contains("dark"), "{text}");
    press(&mut app, &["left", "left"]);
    assert!(app.theme_system);
    assert_eq!(app.theme.opts.mode, nexus_tui::theme::Options::from_env().mode);
    app.persist_prefs();
    let saved = prefs::load(&home);
    assert_eq!(saved.theme.mode.as_deref(), Some("system"));
    let mut restored = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    restored.apply_prefs(&home, saved, Overrides::default());
    assert!(restored.theme_system);
    assert_eq!(restored.theme.opts.mode, nexus_tui::theme::Options::from_env().mode);
    let _ = std::fs::remove_dir_all(home);
}

#[test]
fn settings_segments_move_with_left_right_and_esc_steps_back() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.execute("app.settings");
    press(&mut app, &["a", "n", "i", "m"]);
    let (_, rows) = draw(&mut app, 120, 36);
    let text = joined(&rows);
    assert!(text.contains("Animations"));
    assert!(text.contains(" on "), "segments show their options: {text}");
    assert!(
        text.contains(" off "),
        "segments show their options: {text}"
    );
    let before = app.theme.opts.reduced_motion;
    press(&mut app, &["right"]);
    assert_ne!(app.theme.opts.reduced_motion, before);
    assert!(!app.ui.settings_nav_focus, "Right moves the segment");
    press(&mut app, &["left"]);
    assert_eq!(app.theme.opts.reduced_motion, before, "Left steps back");
    press(&mut app, &["esc"]);
    press(&mut app, &["m", "o", "n", "o"]);
    draw(&mut app, 120, 36);
    app.click(Target::SettingsRow(0));
    assert_eq!(app.theme.opts.tier, Tier::Mono);
    press(&mut app, &["left"]);
    assert!(!app.ui.settings_nav_focus);
    assert_eq!(
        app.theme.opts.tier,
        Tier::TrueColor,
        "Left moves the segment instead of leaving the pane"
    );
    press(&mut app, &["right"]);
    assert_eq!(app.theme.opts.tier, Tier::Mono);
    // Tab returns to the section rail; Esc then closes.
    press(&mut app, &["tab"]);
    assert!(app.ui.settings_nav_focus);
    assert_eq!(
        app.theme.opts.tier,
        Tier::Mono,
        "Tab must not change a value"
    );
    press(&mut app, &["esc"]);
    press(&mut app, &["s", "i", "d", "e", "b", "a", "r"]);
    press(&mut app, &["right"]);
    assert!(!app.ui.left_open, "rail only");
    press(&mut app, &["right"]);
    assert!(app.ui.left_open, "the sidebar beside the rail");
}

#[test]
fn the_timeline_scroll_bar_shows_while_scrolling_then_fades_out() {
    use std::time::Duration;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    for n in 0..40 {
        let value = serde_json::json!({"id": 2000 + n, "type": "message.added", "session_id": SESSION,
            "cursor": format!("log_c915bfc9b6d826c4:{}", 2000 + n),
            "data": {"id": format!("msg_scroll_{n}"), "session_id": SESSION, "seq": 200 + n,
                     "created": 1793113500.0 + n as f64,
                     "role": "assistant", "parts": [{"type": "text", "text": format!("paragraph {n}")}]}});
        if let Ok(nexus_tui::api::Frame::Event(event)) =
            nexus_tui::api::Frame::from_value("message.added", None, &value)
        {
            app.apply_sync(nexus_tui::sync::SyncMsg::Event(event));
        }
    }
    let glyph = app.theme.structure("rail_heavy").to_owned();
    let start = app.clock;
    let r = app.regions.timeline;
    let (buf, _) = draw(&mut app, 120, 36);
    assert!(
        (r.y..r.bottom()).all(|y| buf[(r.right() - 1, y)].symbol() != glyph),
        "no bar before the reader scrolls"
    );
    press(&mut app, &["pageup"]);
    let (buf, _) = draw(&mut app, 120, 36);
    assert!(
        (r.y..r.bottom()).any(|y| buf[(r.right() - 1, y)].symbol() == glyph),
        "the bar appears while scrolling"
    );
    // It fades for a moment, then disappears on the shared tick.
    assert!(app.tick(start + Duration::from_millis(1100)));
    let (buf, _) = draw(&mut app, 120, 36);
    assert!((r.y..r.bottom()).any(|y| buf[(r.right() - 1, y)].symbol() == glyph));
    assert!(app.tick(start + Duration::from_millis(1600)));
    let (buf, _) = draw(&mut app, 120, 36);
    assert!(
        (r.y..r.bottom()).all(|y| buf[(r.right() - 1, y)].symbol() != glyph),
        "the bar hides shortly after"
    );
    // Turning it off in Settings keeps even a fresh scroll from showing it.
    app.ui.scrollbar = false;
    press(&mut app, &["pageup"]);
    let (buf, _) = draw(&mut app, 120, 36);
    assert!((r.y..r.bottom()).all(|y| buf[(r.right() - 1, y)].symbol() != glyph));
}

#[test]
fn session_rows_end_with_child_count_and_age_and_truncate_the_place_first() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let now = nexus_tui::app::now_secs();
    let parent = app.state.model.sessions.get_mut(SESSION).unwrap();
    parent.updated = now - 3.0 * 3600.0 - 30.0;
    let mut child = parent.clone();
    child.id = "ses_child".into();
    child.parent_id = Some(SESSION.into());
    child.updated = now - 5.0 * 60.0;
    let mut grandchild = child.clone();
    grandchild.id = "ses_grandchild".into();
    grandchild.parent_id = Some("ses_child".into());
    for s in [child, grandchild] {
        app.state.model.sessions.insert(s.id.clone(), s);
    }
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "session_directory": "/work/nexus-home/ws",
            "worktrees": [{"path": "/work/nexus-home", "branch": "main"}]
        }),
    );
    let (_, rows) = draw(&mut app, 120, 36);
    let row = rows
        .iter()
        .position(|line| line.contains("⌥ main"))
        .expect("parent row");
    let side: String = rows[row].chars().skip(6).take(28).collect();
    assert!(side.trim_end().ends_with("2 ↳"), "{side:?}");
    assert!(
        rows[row - 1].contains("3h"),
        "age on the title line: {:?}",
        rows[row - 1]
    );
    // Child sessions are not listed, so only the parent row has a meta.
    assert!(!rows.iter().any(|l| l.contains("1 ↳")), "{rows:#?}");

    // A long branch is truncated and the child count survives.
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "session_directory": "/work/nexus-home/ws",
            "worktrees": [{"path": "/work/nexus-home", "branch": "feature/a-very-long-branch-name-that-cannot-fit"}]
        }),
    );
    let (_, rows) = draw(&mut app, 120, 36);
    let row = rows
        .iter()
        .find(|line| line.contains("⌥ feature"))
        .expect("long row");
    assert!(row.contains('…'), "{row:?}");
    assert!(row.contains("2 ↳"), "{row:?}");
    assert!(!row.contains("that-cannot-fit"), "{row:?}");
}

#[test]
fn preferences_are_restored_and_saved_when_a_setting_changes() {
    use nexus_tui::prefs::{self, Overrides, Prefs};
    let home = std::env::temp_dir().join(format!("nexus-tui-shell-prefs-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&home);
    std::fs::create_dir_all(&home).unwrap();
    let mut saved = Prefs::default();
    saved.ui.left_width = Some(40);
    saved.ui.right_open = Some(false);
    saved.theme.mode = Some("light".into());
    saved.theme.monochrome = Some(true);
    saved.theme.animations = Some(false);
    prefs::save(&home, &saved).unwrap();

    let mut o = opts(Mode::Dark, Tier::TrueColor);
    o.reduced_motion = false;
    let mut app = empty(o, (120, 36));
    app.apply_prefs(&home, prefs::load(&home), Overrides::default());
    assert_eq!(app.ui.left_width, 40);
    assert!(!app.ui.right_open);
    assert_eq!(app.theme.opts.mode, Mode::Light);
    assert_eq!(app.theme.opts.tier, Tier::Mono);
    assert!(app.theme.opts.reduced_motion);
    // Nothing changed yet: the file is untouched.
    let before = std::fs::read(prefs::path(&home)).unwrap();
    app.persist_prefs();
    assert_eq!(std::fs::read(prefs::path(&home)).unwrap(), before);

    // A drag saves only on release.
    app.ui.left_drag = true;
    app.ui.left_width = 52;
    app.persist_prefs();
    assert_eq!(prefs::load(&home).ui.left_width, Some(40));
    app.ui.left_drag = false;
    app.persist_prefs();
    assert_eq!(prefs::load(&home).ui.left_width, Some(52));
    assert_eq!(prefs::load(&home).theme.mode.as_deref(), Some("light"));

    // A flag overrides the file for the run and is not written back.
    let mut flagged = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    flagged.apply_prefs(
        &home,
        prefs::load(&home),
        Overrides {
            mode: true,
            tier: true,
        },
    );
    assert_eq!(flagged.theme.opts.mode, Mode::Dark);
    assert_eq!(flagged.theme.opts.tier, Tier::TrueColor);
    flagged.ui.left_open = false;
    flagged.persist_prefs();
    let p = prefs::load(&home);
    assert_eq!(p.ui.left_open, Some(false));
    assert_eq!(p.theme.mode.as_deref(), Some("light"));
    assert_eq!(p.theme.monochrome, Some(true));
    let _ = std::fs::remove_dir_all(home);
}

const GEN: &str = "log_c915bfc9b6d826c4";

/// A session with one pending `ask` request of the given questions JSON.
fn asking(questions: &str, size: (u16, u16)) -> nexus_tui::app::App {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), size);
    app.open_session(SESSION);
    app.effects.clear();
    let data = format!(
        r#"{{"id":"qst_9","session_id":"{SESSION}","call_id":"call_9","created":1.0,"questions":{questions}}}"#
    );
    let sse = format!(
        "id: {GEN}:4\nevent: question.asked\ndata: {{\"id\":4,\"seq\":4,\"time\":1.0,\"type\":\"question.asked\",\"session_id\":\"{SESSION}\",\"data\":{data},\"cursor\":\"{GEN}:4\"}}\n\n"
    );
    for e in events(&sse) {
        app.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    app
}

fn answers_sent(app: &nexus_tui::app::App) -> Vec<serde_json::Value> {
    app.effects
        .iter()
        .filter_map(|e| match e {
            nexus_tui::app::Effect::Api(nexus_tui::actions::ApiCall::Answer {
                id,
                answers,
                ..
            }) => {
                assert_eq!(id, "qst_9");
                Some(serde_json::Value::Array(answers.clone()))
            }
            _ => None,
        })
        .collect()
}

#[test]
fn parallel_asks_keep_their_own_picks() {
    let notes = r#"{"id":"notes","question":"Anything else?"}"#;
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    let data = format!(
        r#"{{"id":"qst_99","session_id":"{SESSION}","call_id":"call_99","created":2.0,"questions":[{notes}]}}"#
    );
    let sse = format!(
        "id: {GEN}:5\nevent: question.asked\ndata: {{\"id\":5,\"seq\":5,\"time\":2.0,\"type\":\"question.asked\",\"session_id\":\"{SESSION}\",\"data\":{data},\"cursor\":\"{GEN}:5\"}}\n\n"
    );
    for e in events(&sse) {
        app.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    // Pick an option in the first ask, then answer the second one.
    press(&mut app, &["alt+w", "2"]);
    press(&mut app, &["tab"]);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("Anything else?"), "{all}");
    typing(&mut app, "second answer");
    // Back on the first ask its pick survived, and it still sends alone.
    press(&mut app, &["alt+w", "shift+tab"]);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Which database?") && all.contains("2 SQLite"),
        "{all}"
    );
    press(&mut app, &["enter", "enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": ["lite"], "text": null}])]
    );
}

const DB: &str = r#"{"id":"db","question":"Which database?","options":[{"id":"pg","label":"PostgreSQL"},{"id":"lite","label":"SQLite"}],"multiple":false,"allow_free_text":false}"#;

#[test]
fn question_card_shows_options_and_a_number_key_answers() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    // The card takes the keys on arrival, so typing goes straight to its answer.
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::Waiting);
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(
        all.contains("Which database?")
            && all.contains("1 PostgreSQL")
            && all.contains("2 SQLite")
            && all.contains("enter send"),
        "{all}"
    );
    assert!(!all.contains("arrives with"), "{all}");
    // a number picks; Enter sends the single question at once (no review)
    press(&mut app, &["2"]);
    assert!(answers_sent(&app).is_empty());
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": ["lite"], "text": null}])]
    );
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        !all.contains("enter submit") && !all.contains("review"),
        "{all}"
    );
    // a second press while the reply is in flight sends nothing more
    press(&mut app, &["enter"]);
    assert_eq!(answers_sent(&app).len(), 1);
}

#[test]
fn long_question_content_scrolls_and_drafts_survive_pages() {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/question-readability.json"
    ))
    .unwrap();
    let mut app = asking(&fixture["questions"].to_string(), (80, 24));
    press(&mut app, &["alt+w", "1", "enter"]);
    let mut seen = String::new();
    for _ in 0..100 {
        seen.push_str(&joined(&draw(&mut app, 80, 24).1));
        press(&mut app, &["pagedown"]);
    }
    let end = draw(&mut app, 80, 24);
    assert!(joined(&end.1).contains("Add context"));
    if let Ok(dir) = std::env::var("NEXUS_QUESTION_SHOTS") {
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(
            format!("{dir}/tui-narrow-end-verified.svg"),
            nexus_tui::svg::to_svg(&end.0, fg, bg),
        )
        .unwrap();
    }
    for text in [
        "For the second question",
        "review the state?",
        "Automatically roll back",
        "previous service configuration",
        "rollback includes",
        "sequence afterward",
        "Pause and wait",
        "explicit decision",
        "Add context",
    ] {
        assert!(seen.contains(text), "missing {text}: {seen}");
    }
    press(&mut app, &["alt+a"]);
    typing(&mut app, "my draft");
    press(&mut app, &["alt+left", "alt+right"]);
    assert_eq!(
        app.ask_current()
            .unwrap()
            .pick()
            .unwrap()
            .input
            .expanded_text(),
        "my draft"
    );
    press(&mut app, &["enter"]);
    let mut review = String::new();
    for _ in 0..100 {
        review.push_str(&joined(&draw(&mut app, 80, 24).1));
        press(&mut app, &["pagedown"]);
    }
    assert!(review.contains("operator to review the state"), "{review}");
    assert!(review.contains("my draft"), "{review}");
    assert!(review.contains("enter submit"), "{review}");
    assert!(review.contains("esc stop"), "{review}");
}

#[test]
fn question_card_can_scroll_on_a_short_terminal() {
    let q = serde_json::json!({"id":"short", "question":"Start of question. This question is long enough to wrap on a short terminal. End of question.", "options":[{"id":"a", "label":"An option with a long enough label to wrap", "preview":"A preview that ends with the final sentinel."}]});
    let mut app = asking(&format!("[{q}]"), (50, 16));
    press(&mut app, &["alt+w"]);
    let mut seen = String::new();
    for _ in 0..100 {
        seen.push_str(&joined(&draw(&mut app, 50, 16).1));
        press(&mut app, &["pagedown"]);
    }
    assert!(seen.contains("of question."), "{seen}");
    assert!(seen.contains("sentinel."), "{seen}");
    assert!(seen.contains("Add context"), "{seen}");
}

#[test]
fn enter_on_a_single_question_sends_it_at_once() {
    // Enter on the card (or from the composer) answers a single question in one
    // step; there is no review page to confirm.
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": ["pg"], "text": null}])]
    );
    press(&mut app, &["enter"]);
    assert_eq!(answers_sent(&app).len(), 1);
}

#[test]
fn arrows_and_enter_pick_and_clicks_hit_options() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    press(&mut app, &["down", "down", "enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": ["lite"], "text": null}])]
    );

    let mut app = asking(&format!("[{DB}]"), (120, 36));
    let (_, rows) = draw(&mut app, 120, 36);
    let row = rows
        .iter()
        .position(|r| r.contains("1 PostgreSQL"))
        .unwrap();
    let x = col_of(&rows[row], "PostgreSQL") as u16;
    assert_eq!(
        app.hits.at(x, row as u16),
        Some(&Target::WaitOption(0)),
        "option row is a hit target"
    );
    click(&mut app, x, row as u16);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": ["pg"], "text": null}])]
    );
}

#[test]
fn free_text_goes_through_the_card_and_several_questions_send_once() {
    let notes = r#"{"id":"notes","question":"Anything else?"}"#;
    let multi = r#"{"id":"tags","question":"Which tags?","options":[{"id":"a","label":"alpha"},{"id":"b","label":"beta"}],"multiple":true,"allow_free_text":false}"#;
    let mut app = asking(&format!("[{DB},{multi},{notes}]"), (120, 36));
    press(&mut app, &["alt+w", "1"]);
    // single-select: Enter locks the answer and moves to the next question
    press(&mut app, &["enter"]);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Which tags?") && all.contains("question 2 of 3"),
        "{all}"
    );
    // multi-select: tick two, Enter locks
    press(&mut app, &["1", "2", "enter"]);
    assert!(answers_sent(&app).is_empty());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Anything else?") && all.contains("question 3 of 3"),
        "{all}"
    );
    // typing from the card lands in its own answer field; Enter locks and shows
    // the review, and the message composer stays empty
    typing(&mut app, "keep it small");
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::Waiting);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("keep it small"), "{all}");
    press(&mut app, &["enter"]);
    assert!(answers_sent(&app).is_empty());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("1. Which database?")
            && all.contains("3. Anything else?")
            && all.contains("keep it small"),
        "{all}"
    );
    assert!(app.composer_ref().expanded_text().is_empty());
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([
            {"question_id": "db", "selected": ["pg"], "text": null},
            {"question_id": "tags", "selected": ["a", "b"], "text": null},
            {"question_id": "notes", "selected": [], "text": "keep it small"}
        ])]
    );
}

#[test]
fn alt_left_and_right_move_between_questions_and_keep_each_answer() {
    let notes = r#"{"id":"notes","question":"Anything else?"}"#;
    let mut app = asking(&format!("[{DB},{notes}]"), (120, 36));
    press(&mut app, &["alt+w", "2", "enter"]);
    typing(&mut app, "a note");
    // alt+left goes back to the first question with its pick still marked
    press(&mut app, &["alt+w", "alt+left"]);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Which database?") && all.contains("2 SQLite"),
        "{all}"
    );
    // alt+right returns to the text question with its answer restored
    press(&mut app, &["alt+right"]);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Anything else?") && all.contains("a note"),
        "{all}"
    );
    assert!(app.composer_ref().expanded_text().is_empty());
    // one more alt+right reaches the review; nothing is sent yet
    press(&mut app, &["alt+right"]);
    assert!(answers_sent(&app).is_empty());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("1. Which database?")
            && all.contains("SQLite")
            && all.contains("2. Anything else?")
            && all.contains("a note"),
        "{all}"
    );
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([
            {"question_id": "db", "selected": ["lite"], "text": null},
            {"question_id": "notes", "selected": [], "text": "a note"}
        ])]
    );
}

/// Foreground colours of the waiting card's heavy rail cells.
fn rail_colors(buf: &ratatui::buffer::Buffer) -> Vec<Color> {
    let area = buf.area;
    let mut v = Vec::new();
    for y in area.y..area.y + area.height {
        for x in area.x..area.x + area.width {
            let cell = &buf[(x, y)];
            if cell.symbol() == "\u{2503}" {
                v.push(cell.fg);
            }
        }
    }
    v
}

#[test]
fn arrows_space_and_enter_work_for_a_multi_select_question() {
    let multi = r#"{"id":"tags","question":"Which tags?","options":[{"id":"a","label":"alpha"},{"id":"b","label":"beta"}],"multiple":true,"allow_free_text":false}"#;
    let mut app = asking(&format!("[{multi}]"), (120, 36));
    // Down moves the cursor, Space ticks, Enter sends the single question once.
    press(&mut app, &["down", "space", "enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "tags", "selected": ["b"], "text": null}])]
    );
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        !all.contains("enter submit") && !all.contains("1. Which tags?"),
        "{all}"
    );
}

#[test]
fn multi_select_is_violet_and_the_review_rail_is_white() {
    let multi = r#"{"id":"tags","question":"Which tags?","options":[{"id":"a","label":"alpha"},{"id":"b","label":"beta"}],"multiple":true,"allow_free_text":false}"#;
    let notes = r#"{"id":"notes","question":"Anything else?"}"#;
    let mut app = asking(&format!("[{multi},{notes}]"), (120, 36));
    let (buf, _) = draw(&mut app, 120, 36);
    let violet = app.theme.color("interactive.multi").unwrap();
    let rails = rail_colors(&buf);
    assert!(
        !rails.is_empty() && rails.iter().all(|c| *c == violet),
        "{rails:?}"
    );

    press(&mut app, &["1", "enter"]);
    typing(&mut app, "a note");
    press(&mut app, &["enter"]);
    let (buf, _) = draw(&mut app, 120, 36);
    let white = app.theme.color("text.primary").unwrap();
    let rails = rail_colors(&buf);
    assert!(
        !rails.is_empty() && rails.iter().all(|c| *c == white),
        "{rails:?}"
    );
}

#[test]
fn typed_text_for_an_option_question_is_its_custom_answer() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    typing(&mut app, "postgres please");
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::Waiting);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("postgres please"), "{all}");
    assert!(app.composer_ref().expanded_text().is_empty());
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": [], "text": "postgres please"}])]
    );
}

#[test]
fn a_selected_option_and_custom_context_are_sent_together() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    press(&mut app, &["1", "alt+a"]);
    // The leading digit is text in the field, not another option pick.
    typing(&mut app, "2nd choice");
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": ["pg"], "text": "2nd choice"}])]
    );
}

#[test]
fn a_question_locks_the_composer_and_keeps_its_draft() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    app.composer().set_text("Keep this unsent draft");
    app.effects.clear();
    typing(&mut app, "postgres please");
    // Typing goes to the answer, never to the composer.
    assert_eq!(app.composer_ref().expanded_text(), "Keep this unsent draft");
    assert_eq!(
        app.ask_current()
            .unwrap()
            .pick()
            .unwrap()
            .input
            .expanded_text(),
        "postgres please"
    );
    // Steer and queue are disabled as well.
    app.run_command("composer.queue");
    app.run_command("session.send");
    assert!(!app.effects.iter().any(|e| matches!(
        e,
        nexus_tui::app::Effect::Api(nexus_tui::actions::ApiCall::Prompt { .. })
    )));
    assert_eq!(app.composer_ref().expanded_text(), "Keep this unsent draft");
    // Enter sends the answer; the draft is still there.
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": [], "text": "postgres please"}])]
    );
    assert_eq!(app.composer_ref().expanded_text(), "Keep this unsent draft");
}

#[test]
fn pasting_while_the_card_is_focused_goes_into_its_answer_field() {
    let notes = r#"{"id":"notes","question":"Anything else?"}"#;
    let mut app = asking(&format!("[{notes}]"), (120, 36));
    app.handle_paste("pasted answer");
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::Waiting);
    assert!(app.composer_ref().expanded_text().is_empty());
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![
            serde_json::json!([{"question_id": "notes", "selected": [], "text": "pasted answer"}])
        ]
    );
}

#[test]
fn escape_on_a_question_follows_the_manifest_chain() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    press(&mut app, &["alt+w"]);
    let rule = app.escape_rule();
    // not busy here, so the chain falls through to refocusing the composer
    assert_eq!(rule.as_deref(), Some("refocus"));
    press(&mut app, &["esc"]);
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::Composer);
    assert!(answers_sent(&app).is_empty());
}

#[test]
fn a_sidebar_tab_opens_the_sidebar_beside_the_rail() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.ui.left_open = false;
    app.execute("sidebar.tab.files");
    assert!(app.ui.left_open);
    app.ui.left_open = false;
    app.execute("sidebar.tab.sessions");
    assert!(app.ui.left_open);
}

#[test]
fn tab_labels_do_not_show_a_tooltip_on_hover() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let r = app
        .hits
        .rect_of(&Target::Tab(nexus_tui::ui::Tab::Files))
        .unwrap();
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(!rows[r.y as usize + 1].contains("Files"));
    app.mouse = Some((r.x + 1, r.y));
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(!rows[r.y as usize + 1].contains("Files"));
}

fn completes(app: &nexus_tui::app::App) -> Vec<(String, u32)> {
    app.effects
        .iter()
        .filter_map(|e| match e {
            nexus_tui::app::Effect::Sync(nexus_tui::sync::Command::Complete {
                query,
                limit,
                ..
            }) => Some((query.clone(), *limit)),
            _ => None,
        })
        .collect()
}

fn reply(app: &mut nexus_tui::app::App, query: &str, limit: u32, paths: &[&str]) {
    app.apply_sync(nexus_tui::sync::SyncMsg::Completion {
        project: app.current_project().unwrap(),
        session: Some(SESSION.into()),
        query: query.into(),
        limit,
        data: Ok(serde_json::json!({"query": query, "paths": paths})),
    });
}

fn rows_of(app: &nexus_tui::app::App) -> Vec<String> {
    app.completion_rows()
        .unwrap_or_default()
        .into_iter()
        .map(|r| r.value)
        .collect()
}

#[test]
fn slash_completion_ranks_commands_and_tab_accepts_the_highlighted_one() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "/mo");
    let (_, screen) = draw(&mut app, 120, 36);
    let rows = rows_of(&app);
    assert_eq!(rows.len(), 10, "{rows:?}");
    assert_eq!(rows[0], "/model");
    let at = screen
        .iter()
        .position(|r| r.contains("/model"))
        .expect("popup row");
    let composer = app.hits.rect_of(&Target::Composer).unwrap();
    assert!(at < composer.y as usize, "popup sits above the composer");
    assert!(
        screen[at].contains("Change model") || screen[at].contains("model"),
        "{:?}",
        screen[at]
    );
    press(&mut app, &["down", "tab"]);
    assert_eq!(app.composer_ref().text(), format!("{} ", rows[1]));
    // The accepted command has an argument slot now: no popup.
    assert!(app.completion_rows().is_none());
}

#[test]
fn enter_on_a_standalone_command_runs_the_highlighted_one() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "/mod");
    draw(&mut app, 120, 36);
    press(&mut app, &["enter"]);
    assert!(app.composer_ref().is_empty());
    assert!(app.ui.overlay.is_some(), "/model opened its picker");
}

#[test]
fn esc_hides_the_popup_for_that_token_only() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "/mo");
    assert!(app.completion_rows().is_some());
    press(&mut app, &["esc"]);
    assert!(app.completion_rows().is_none());
    assert_eq!(app.composer_ref().text(), "/mo", "Esc did not fall through");
    press(&mut app, &["backspace"]);
    assert!(
        app.completion_rows().is_some(),
        "a new token shows it again"
    );
}

#[test]
fn at_completion_asks_core_shows_paths_and_keeps_going_inside_directories() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.effects.clear();
    typing(&mut app, "see @");
    assert_eq!(completes(&app), [(String::new(), 100), (String::new(), 30)]);
    assert!(app.completion_rows().is_none(), "nothing before the reply");
    reply(&mut app, "", 30, &["src/", "README.md"]);
    let (_, screen) = draw(&mut app, 120, 36);
    assert!(all_contains(&screen, "src/") && all_contains(&screen, "README.md"));
    app.effects.clear();
    press(&mut app, &["tab"]);
    assert_eq!(app.composer_ref().text(), "see @src/");
    assert_eq!(completes(&app), [("src/".to_owned(), 30)]);
    reply(&mut app, "src/", 30, &["src/a.rs", "src/b.rs"]);
    assert_eq!(rows_of(&app)[..2], ["src/a.rs", "src/b.rs"]);
    press(&mut app, &["down", "enter"]);
    assert_eq!(app.composer_ref().text(), "see @src/b.rs ");
    assert!(app.completion_rows().is_none());
}

#[test]
fn at_completion_at_the_caret_preserves_the_rest_of_the_draft() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "see @45 afterwards");
    for _ in 0.." afterwards".len() {
        press(&mut app, &["left"]);
    }
    reply(&mut app, "45", 30, &["plans/45-tabs-and-splits.md"]);
    press(&mut app, &["enter"]);
    assert_eq!(
        app.composer_ref().text(),
        "see @plans/45-tabs-and-splits.md  afterwards"
    );
}

#[test]
fn short_at_replies_are_padded_from_the_empty_query_listing() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "@mian");
    let pool: Vec<String> = (0..15).map(|i| format!("lib/f{i}.rs")).collect();
    let pool: Vec<&str> = pool.iter().map(String::as_str).collect();
    reply(&mut app, "", 100, &pool);
    reply(&mut app, "mian", 30, &["src/main.rs"]);
    let rows = rows_of(&app);
    assert_eq!(rows.len(), 10);
    assert_eq!(rows[0], "src/main.rs");
}

#[test]
fn file_completion_matches_the_shared_ranking_scenario() {
    let scenario: serde_json::Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/file-completion.json"
    ))
    .unwrap();
    let strings = |name: &str| -> Vec<String> {
        scenario[name]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().to_owned())
            .collect()
    };
    assert_eq!(
        nexus_tui::completion::file_menu(
            scenario["query"].as_str().unwrap(),
            &strings("hits"),
            &strings("pool")
        ),
        strings("expected")
    );
}

#[test]
fn a_stale_reply_is_ignored_and_replies_stay_cached_while_typing() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "@a");
    typing(&mut app, "b");
    reply(&mut app, "a", 30, &["alpha.rs"]);
    assert!(app.completion_rows().is_none(), "reply for an old token");
    reply(&mut app, "ab", 30, &["abc.rs", "xab.rs"]);
    assert_eq!(rows_of(&app), ["abc.rs", "xab.rs"]);
    // Backspacing and typing again reuse the cached reply without a new request.
    app.effects.clear();
    typing(&mut app, "c");
    assert_eq!(rows_of(&app), ["abc.rs", "xab.rs"]);
    press(&mut app, &["backspace"]);
    assert!(completes(&app).iter().all(|(q, _)| q != "ab"));
    assert_eq!(rows_of(&app), ["abc.rs", "xab.rs"]);
}

#[test]
fn clicking_a_completion_row_accepts_it() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "@");
    reply(&mut app, "", 30, &["one.rs", "two.rs"]);
    draw(&mut app, 120, 36);
    let row = app.hits.rect_of(&Target::CompletionRow(1)).expect("row 1");
    click(&mut app, row.x + 3, row.y);
    assert_eq!(app.composer_ref().text(), "@two.rs ");
}

#[test]
fn nothing_completes_while_a_palette_is_open() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "/mo");
    assert!(app.completion_rows().is_some());
    app.execute("palette.open");
    assert!(app.completion_rows().is_none());
}

#[test]
fn session_info_totals_tokens_tools_context_and_cost() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    app.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/example".into());
    app.state.models = vec![serde_json::json!({
        "ref": "codex/example", "name": "Example", "context": 200000,
        "input_cost": 1.0, "output_cost": 10.0, "cache_read_cost": 0.2
    })];
    // Two requests of the stored turn; the ledger counts both.
    let mut turn = fixture_summary(
        "codex/example",
        serde_json::json!({"input": 10_000, "cached": 0, "output": 1000, "reasoning": 0}),
    );
    let mut second = turn["requests"][0].clone();
    second["id"] = "req_2".into();
    second["usage"] =
        serde_json::json!({"input": 50_000, "cached": 20_000, "output": 2000, "reasoning": 0});
    turn["requests"].as_array_mut().unwrap().push(second);
    summarize(&mut app, 100, turn);
    let (_, rows) = draw(&mut app, 160, 40);
    let all = joined(&rows);
    for needle in [
        "Effort",
        "Turns",
        "2 tools",
        "CONTEXT",
        "50k of 200k",
        "25%",
        "60k in",
        "3k out",
        "Cost",
    ] {
        assert!(all.contains(needle), "missing {needle:?}:\n{all}");
    }
    // 10K in + 1K out, then 30K fresh + 20K cached + 2K out.
    assert!(all.contains("$0.07"), "{all}");
}

#[test]
fn right_click_on_a_session_row_opens_its_action_menu() {
    use nexus_tui::ui::{Overlay, OverlayKind, PaletteMode};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let row = app
        .hits
        .rect_of(&Target::SideRow(0))
        .expect("a session row");
    right_click(&mut app, row.x + 2, row.y);
    assert!(matches!(
        app.ui.overlay,
        Some(Overlay { kind: OverlayKind::Palette(ref p), .. }) if p.mode == PaletteMode::RowMenu
    ));
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    for action in [
        "Rename session",
        "Archive session",
        "Delete session",
        "Export session",
        "Copy session id",
    ] {
        assert!(all.contains(action), "{action}: {all}");
    }
    assert!(!all.contains("Remove project"), "{all}");
}

#[test]
fn projects_can_be_added_and_removed_from_the_sidebar() {
    use nexus_tui::actions::ApiCall;
    use nexus_tui::app::Effect;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let add = app.hits.rect_of(&Target::AddProject).expect("add control");
    app.effects.clear();
    click(&mut app, add.x + 2, add.y + 1);
    assert!(app.ui.edit.is_some(), "an inline path field opens");
    // Replace the prefilled path.
    app.ui.edit.as_mut().unwrap().buf.set_text("");
    typing(&mut app, "/work/new-project");
    press(&mut app, &["enter"]);
    assert!(app.effects.iter().any(|e| matches!(
        e,
        Effect::Api(ApiCall::AddProject { path }) if path == "/work/new-project"
    )));
    // Remove asks first and names the sessions that go with it.
    app.ui.focus = nexus_tui::ui::Focus::Sidebar;
    app.execute("project.remove");
    let dialog = app.ui.dialog.clone().expect("a confirmation");
    assert!(
        dialog.danger && dialog.body.join(" ").contains("session"),
        "{dialog:?}"
    );
    app.effects.clear();
    press(&mut app, &["tab", "enter"]);
    assert!(
        app.effects
            .iter()
            .any(|e| matches!(e, Effect::Api(ApiCall::DeleteProject { .. }))),
        "{:?}",
        app.effects
    );
}

#[test]
fn settings_shows_what_core_reports_and_refuses_what_it_cannot_change() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    app.state.catalog_source = "cache (5 min old)".into();
    app.state.models = vec![serde_json::json!({"ref": "codex/gpt-5"})];
    app.state.providers = vec![
        serde_json::json!({"id": "codex", "label": "Codex", "connected": true, "verified": true, "methods": ["device"]}),
        serde_json::json!({"id": "groq", "label": "Groq", "connected": false, "verified": true, "methods": ["api_key"], "key_env": "GROQ_API_KEY"}),
    ];
    app.state.agents = vec![
        serde_json::json!({"name": "build", "mode": "all", "description": "Builds", "source": "builtin",
                           "subagent": {"effective": {"model": "", "source": "parent"}}}),
        serde_json::json!({"name": "orchestrate", "mode": "primary"}),
    ];
    app.execute("app.settings");
    let (_, rows) = draw(&mut app, 120, 50);
    let all = joined(&rows);
    for needle in ["Appearance", "Providers", "Agents", "13 sections"] {
        assert!(all.contains(needle), "missing {needle:?}: {all}");
    }
    // The Providers pane.
    app.ui.settings_section = "providers".into();
    app.ui.settings_nav_focus = false;
    app.ui.settings_page = None;
    assert_eq!(app.settings_open_page().as_deref(), Some("providers"));
    let all = joined(&draw(&mut app, 120, 50).1);
    for needle in [
        "Settings \u{203a} Providers",
        "catalog: cache (5 min old)",
        "Codex",
        "connected \u{b7} 1 model",
        "not connected \u{b7} API key",
        "esc back",
    ] {
        assert!(all.contains(needle), "missing {needle:?}: {all}");
    }
    press(&mut app, &["esc"]);
    assert_eq!(app.ui.settings_page, None);
    assert_eq!(app.ui.view, nexus_tui::ui::View::Settings);
    // The Agents pane refuses a change before the configuration has loaded.
    app.ui.settings_section = "agents".into();
    app.ui.settings_nav_focus = false;
    app.ui.settings_page = None;
    app.ui.settings_page_row = 0;
    let all = joined(&draw(&mut app, 120, 50).1);
    for needle in [
        "Default agent for new sessions",
        "SUBAGENTS",
        "build",
        "parent's model",
    ] {
        assert!(all.contains(needle), "missing {needle:?}: {all}");
    }
    assert!(
        !all.contains("orchestrate"),
        "primary agents are not listed"
    );
    assert!(
        !all.contains("+ add model"),
        "model lists are on each subagent's own page"
    );
    press(&mut app, &["enter"]);
    let toast = app
        .ui
        .alerts
        .history
        .first()
        .map(|n| n.text.clone())
        .unwrap_or_default();
    assert!(toast.contains("Configuration is not loaded"), "{toast}");
}

#[test]
fn agent_model_lists_show_odds_skip_disconnected_and_edit_the_config() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    app.state.models = vec![
        serde_json::json!({"ref": "codex/gpt-5"}),
        serde_json::json!({"ref": "codex/mini"}),
    ];
    app.state.providers = vec![
        serde_json::json!({"id": "codex", "label": "Codex", "connected": true, "methods": ["device"]}),
        serde_json::json!({"id": "groq", "label": "Groq", "connected": false, "methods": ["api_key"]}),
    ];
    app.state.agents =
        vec![serde_json::json!({"name": "scout", "mode": "subagent", "source": "builtin"})];
    app.state.config = Some(serde_json::json!({"revision": "r1", "values": {
        "defaults": {"effort": "", "agent": "", "permission": "allow"},
        "agents": {"models": {"scout": ["codex/gpt-5", "groq/x"]}, "fallbacks": {"scout": ["codex/mini", "groq/x"]}}}}));
    app.execute("app.settings");
    app.ui.settings_section = "agents".into();
    app.ui.settings_nav_focus = false;
    app.ui.settings_page = None;
    // One line per subagent; Enter opens its page.
    let all = joined(&draw(&mut app, 120, 50).1);
    assert!(
        all.contains("2 models \u{b7} 2 fallbacks \u{b7} 2 skipped"),
        "{all}"
    );
    press(&mut app, &["down", "down", "enter"]);
    assert_eq!(app.ui.settings_page.as_deref(), Some("agents/scout"));
    let all = joined(&draw(&mut app, 120, 50).1);
    for needle in [
        "Settings \u{203a} Agents \u{203a} scout",
        "MODELS",
        "codex/gpt-5",
        "100%",
        "groq/x",
        "not connected \u{b7} skipped",
        "1. codex/mini",
        "2. groq/x",
    ] {
        assert!(all.contains(needle), "missing {needle:?}: {all}");
    }
    let patch = |app: &nexus_tui::app::App| {
        app.effects.iter().rev().find_map(|e| match e {
            Effect::Sync(nexus_tui::sync::Command::PatchConfig { path, value, .. }) => {
                Some((path.clone(), value.clone()))
            }
            _ => None,
        })
    };
    // Items: two models, add, two fallbacks, add.
    app.effects.clear();
    press(&mut app, &["d"]);
    assert_eq!(
        patch(&app),
        Some((
            "agents.models".into(),
            serde_json::json!({"scout": ["groq/x"]})
        ))
    );
    press(&mut app, &["down", "down", "down", "J"]); // the first fallback moves down
    assert_eq!(
        patch(&app),
        Some((
            "agents.fallbacks".into(),
            serde_json::json!({"scout": ["groq/x", "codex/mini"]})
        ))
    );
    // Adding opens a palette of models not yet in the list.
    app.ui.settings_page_row = 2;
    press(&mut app, &["enter"]);
    let labels: Vec<String> = app.palette_items().into_iter().map(|i| i.label).collect();
    assert_eq!(labels, ["codex/mini"]);
    press(&mut app, &["enter"]);
    assert_eq!(
        patch(&app),
        Some((
            "agents.models".into(),
            serde_json::json!({"scout": ["codex/gpt-5", "groq/x", "codex/mini"]})
        ))
    );
    // Esc returns to the list on the same subagent, then to Settings.
    press(&mut app, &["esc"]);
    assert_eq!(
        (app.ui.settings_page.as_deref(), app.ui.settings_page_row),
        (Some("agents"), 2)
    );
    press(&mut app, &["esc"]);
    assert_eq!(app.ui.settings_page, None);
}

#[test]
fn agent_model_rows_step_effort_with_right_and_left_returns_to_sections() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    app.state.models = vec![
        serde_json::json!({"ref": "codex/gpt-5", "efforts": ["low", "medium", "high"]}),
        serde_json::json!({"ref": "codex/mini"}),
    ];
    app.state.providers = vec![
        serde_json::json!({"id": "codex", "label": "Codex", "connected": true, "methods": ["device"]}),
    ];
    app.state.agents =
        vec![serde_json::json!({"name": "scout", "mode": "subagent", "source": "builtin"})];
    app.state.config = Some(serde_json::json!({"revision": "r1", "values": {
        "defaults": {"effort": "", "agent": "", "permission": "allow"},
        "agents": {"models": {"scout": ["codex/gpt-5", "codex/mini"]}, "fallbacks": {},
                   "efforts": {"scout": {"codex/gpt-5": "low"}}}}}));
    app.execute("app.settings");
    app.ui.settings_section = "agents".into();
    app.ui.settings_nav_focus = false;
    app.ui.settings_page = None;
    draw(&mut app, 120, 50);
    press(&mut app, &["down", "down", "enter"]);
    assert_eq!(app.ui.settings_page.as_deref(), Some("agents/scout"));
    let all = joined(&draw(&mut app, 120, 50).1);
    assert!(all.contains("effort low \u{b7} 50%"), "{all}");
    assert!(!all.contains("effort default \u{b7} 50%\n"), "{all}");
    let patch = |app: &nexus_tui::app::App| {
        app.effects.iter().rev().find_map(|e| match e {
            Effect::Sync(nexus_tui::sync::Command::PatchConfig { path, value, .. }) => {
                Some((path.clone(), value.clone()))
            }
            _ => None,
        })
    };
    app.effects.clear();
    press(&mut app, &["right"]);
    assert_eq!(
        patch(&app),
        Some((
            "agents.efforts".into(),
            serde_json::json!({"scout": {"codex/gpt-5": "medium"}})
        ))
    );
    // Left returns to the heading without changing the effort.
    app.effects.clear();
    press(&mut app, &["left"]);
    assert!(app.ui.settings_nav_focus);
    assert_eq!(patch(&app), None);
    // Right from the last level wraps to the default and clears the entry.
    app.ui.settings_nav_focus = false;
    app.state.config.as_mut().unwrap()["values"]["agents"]["efforts"]["scout"]["codex/gpt-5"] =
        serde_json::json!("high");
    press(&mut app, &["right"]);
    assert_eq!(
        patch(&app),
        Some(("agents.efforts".into(), serde_json::json!({})))
    );
    // A model that lists no levels has nothing to step.
    app.effects.clear();
    press(&mut app, &["down", "right"]);
    assert_eq!(patch(&app), None);
}

#[test]
fn ctrl_r_searches_the_prompt_history_and_fills_the_composer() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 40));
    app.composer().set_history(vec![
        "fix the login bug".into(),
        "write the release notes\nwith two lines".into(),
        "fix the login bug".into(),
        "run the tests".into(),
    ]);
    app.composer().set_text("half typed draft");
    press(&mut app, &["ctrl+r"]);
    let all = joined(&draw(&mut app, 120, 40).1);
    assert!(
        all.contains("Prompt history") && all.contains("type to find an earlier prompt"),
        "{all}"
    );
    // Newest first, and the repeated prompt is listed once.
    assert_eq!(all.matches("fix the login bug").count(), 1, "{all}");
    assert!(all.find("run the tests").unwrap() < all.find("fix the login bug").unwrap());
    assert!(all.contains("2 lines"), "{all}");
    press(&mut app, &["r", "e", "l", "e", "a", "s", "e", "enter"]);
    assert_eq!(
        app.composer_ref().text(),
        "write the release notes\nwith two lines"
    );
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::Composer);
    // Esc leaves the draft alone.
    app.composer().set_text("keep me");
    press(&mut app, &["ctrl+r", "esc"]);
    assert_eq!(app.composer_ref().text(), "keep me");
}

#[test]
fn the_claude_provider_detail_shows_core_s_install_note() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    app.state.providers = vec![
        serde_json::json!({"id": "claude", "label": "Claude", "connected": false, "methods": ["browser"],
        "note": "Claude Code is not installed. Install Claude Code 2.1.248 or newer, then sign in."}),
    ];
    app.execute("app.sign_in");
    press(&mut app, &["down", "enter"]);
    assert_eq!(app.ui.provider_detail.as_deref(), Some("claude"));
    let all = joined(&draw(&mut app, 120, 50).1);
    assert!(
        all.contains("Claude Code is not installed") && all.contains("2.1.248"),
        "{all}"
    );
}

#[test]
fn a_pending_device_sign_in_shows_its_code_on_the_provider_row() {
    use nexus_tui::api::Frame;
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    app.state.providers = vec![
        serde_json::json!({"id": "copilot", "label": "GitHub Copilot", "connected": false, "methods": ["device"]}),
    ];
    app.execute("app.sign_in");
    assert_eq!(app.ui.settings_section, "providers");
    assert_eq!(app.settings_open_page().as_deref(), Some("providers"));
    app.effects.clear();
    press(&mut app, &["down", "enter"]);
    assert_eq!(app.ui.provider_detail.as_deref(), Some("copilot"));
    press(&mut app, &["enter"]);
    assert!(app.effects.iter().any(|e| matches!(e,
        Effect::Api(ApiCall::Provider { id, sign_out: false, method, .. }) if id == "copilot" && method == "device")), "{:?}", app.effects);
    let event = serde_json::json!({"id": 300, "type": "provider.login", "cursor": "log_c915bfc9b6d826c4:300", "data": {
        "provider": "copilot", "status": "pending", "message": "Open https://github.com/login/device and enter code AB-12",
        "verification_uri": "https://github.com/login/device", "user_code": "AB-12"}});
    if let Ok(Frame::Event(event)) = Frame::from_value("provider.login", None, &event) {
        app.apply_sync(SyncMsg::Event(event));
    }
    let all = joined(&draw(&mut app, 120, 50).1);
    assert!(
        all.contains("code AB-12") && all.contains("https://github.com/login/device"),
        "{all}"
    );
    // The modal lists the link first, then the code, then Cancel.
    app.ui.provider_detail_row = 1;
    press(&mut app, &["enter"]);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Copy(code) if code == "AB-12")));
    app.ui.provider_detail_row = 0;
    press(&mut app, &["enter"]);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::OpenUrl(url) if url == "https://github.com/login/device")));
}

#[test]
fn codex_offers_browser_and_device_sign_in() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    app.state.providers = vec![
        serde_json::json!({"id": "codex", "label": "Codex", "connected": false, "methods": ["browser", "device"]}),
    ];
    app.execute("app.sign_in");
    app.effects.clear();
    press(&mut app, &["down", "enter"]);
    assert_eq!(app.ui.provider_detail.as_deref(), Some("codex"));
    let all = joined(&draw(&mut app, 120, 50).1);
    assert!(
        all.contains("Sign in with browser") && all.contains("Sign in with a device code"),
        "{all}"
    );
    // Browser is first, and its row starts that method.
    press(&mut app, &["enter"]);
    assert!(app.effects.iter().any(|e| matches!(e,
        Effect::Api(ApiCall::Provider { id, sign_out: false, method, .. }) if id == "codex" && method == "browser")), "{:?}", app.effects);
    // The device row still starts the device flow.
    app.effects.clear();
    app.ui.provider_logins.clear();
    app.ui.provider_detail_row = 1;
    press(&mut app, &["enter"]);
    assert!(app.effects.iter().any(|e| matches!(e,
        Effect::Api(ApiCall::Provider { id, sign_out: false, method, .. }) if id == "codex" && method == "device")), "{:?}", app.effects);
}

#[test]
fn dragging_the_left_sidebar_edge_resizes_it() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let edge = app
        .hits
        .rect_of(&Target::SidebarEdge)
        .expect("an edge handle");
    let before = app.regions.left.unwrap().width;
    for (kind, column) in [
        (MouseEventKind::Down(MouseButton::Left), edge.x),
        (MouseEventKind::Drag(MouseButton::Left), edge.x + 8),
        (MouseEventKind::Up(MouseButton::Left), edge.x + 8),
    ] {
        app.handle_mouse(MouseEvent {
            kind,
            column,
            row: edge.y + 5,
            modifiers: KeyModifiers::NONE,
        });
    }
    draw(&mut app, 120, 36);
    assert!(app.regions.left.unwrap().width > before, "the sidebar grew");
}

#[test]
fn tabs_are_isolated_by_project_when_closed() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "other-project-session".into();
    other.project_id = "other-project".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.open_session(SESSION);
    app.open_session("other-project-session");
    assert_eq!(app.tab_ids(), vec!["other-project-session"]);
    app.close_tab("other-project-session");
    assert!(app.ui.tabs.contains(&SESSION.to_owned()));
    app.open_session(SESSION);
    assert_eq!(app.tab_ids(), vec![SESSION]);
}

#[test]
fn tabs_replace_the_title_open_close_and_switch() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "second".into();
    other.title = "Second task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    let (_, rows) = draw(&mut app, 120, 36);
    let g = app.groups().focused;
    let strip = app.ui.groups.rects[0].1.y as usize;
    assert!(
        rows[strip].contains("New session"),
        "the open session has a tab in the group's strip: {}",
        rows[strip]
    );
    assert!(rows[strip].contains('+'), "{}", rows[strip]);
    assert!(rows[strip].contains('⋯'), "{}", rows[strip]);
    assert!(
        !rows[0].contains("New session") || !rows[0].contains('+'),
        "the header no longer holds the tabs: {}",
        rows[0]
    );
    app.open_session("second");
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        rows[strip].contains("New session") && rows[strip].contains("Second task"),
        "{}",
        rows[strip]
    );
    // Clicking the other tab switches to it.
    let first = app.hits.rect_of(&Target::GroupTab(g, 0)).expect("first tab");
    click(&mut app, first.x + 2, first.y);
    assert_eq!(app.viewed_id(), Some(SESSION));
    // Closing a tab keeps its session and moves to the neighbour.
    draw(&mut app, 120, 36);
    let close = app
        .hits
        .rect_of(&Target::GroupTabClose(g, 0))
        .expect("close");
    click(&mut app, close.x, close.y);
    assert_eq!(app.viewed_id(), Some("second"));
    assert!(app.state.model.sessions.contains_key(SESSION));
    assert_eq!(app.tab_ids(), vec!["second".to_owned()]);
    // Keyboard: close the last tab and the new-session screen shows.
    press(&mut app, &["alt+x"]);
    assert!(app.viewed_id().is_none());
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        rows.iter().skip(1).take(2).any(|r| r.contains("New session")),
        "{}",
        rows[strip]
    );
}

#[test]
fn group_tabs_follow_the_chrome_band_styles() {
    // D02: the focused group's active tab is bold primary on the page surface,
    // joined to the page; other tabs are muted on the band; `+` ends the strip.
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "other".into();
    other.title = "Other session".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.ui.tabs = vec![SESSION.to_owned(), "other".to_owned()];
    let (buf, rows) = draw(&mut app, 160, 40);
    let strip = app.ui.groups.rects[0].1.y;
    let row = &rows[strip as usize];
    let band = Color::Rgb(0x14, 0x14, 0x14);
    let page = Color::Rgb(0x0b, 0x0b, 0x0b);
    let at = |needle: &str| col_of(row, needle) as u16;
    let viewed = &buf[(at(app.state.model.sessions[SESSION].title.as_str()), strip)];
    assert_eq!(viewed.bg, page, "the viewed tab sits on the page surface");
    assert!(viewed.modifier.contains(ratatui::style::Modifier::BOLD));
    let others = &buf[(at("Other session"), strip)];
    assert_eq!(others.bg, band, "other tabs sit on the band");
    assert!(!others.modifier.contains(ratatui::style::Modifier::BOLD));
    assert_ne!(viewed.fg, others.fg, "viewed is primary, others muted");
    assert!(row.contains('+'), "{row:?}");
    // the strip starts at the centre column, not over the sidebar
    let centre = app.regions.centre.x;
    assert!(at("Other session") > centre);
}

#[test]
fn sidebar_icons_sit_in_the_band_over_the_sidebar() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let (buf, rows) = draw(&mut app, 120, 36);
    let left = app.regions.left.unwrap();
    let toggle = app.hits.rect_of(&Target::ToggleLeft).unwrap();
    assert!(rows[0].contains("[Sessions] [Files]"), "{:?}", rows[0]);
    let title_row = rows
        .iter()
        .skip(2)
        .find(|row| row.contains("New session") && !row.contains("+ New session"))
        .unwrap();
    assert_eq!(
        col_of(&rows[0], "[Sessions]"),
        col_of(title_row, "New session")
    );
    let glyph_x = toggle.x + toggle.width / 2;
    assert_ne!(buf[(glyph_x, 0)].symbol(), " ");
    let mut prev = toggle.right();
    for tab in [nexus_tui::ui::Tab::Sessions, nexus_tui::ui::Tab::Files] {
        let r = app.hits.rect_of(&Target::Tab(tab)).unwrap();
        assert_eq!(r.y, 0);
        assert!(r.x >= prev && r.right() <= left.right(), "{r:?}");
        prev = r.right();
    }
    // the sidebar's first row is free for its own content: nothing but the
    // sessions list starts there (no icon row)
    assert!(
        !rows[1].chars().take(27).any(|c| c == '\u{2261}'),
        "{:?}",
        rows[1]
    );
    assert_eq!(buf[(1, 0)].bg, Color::Rgb(0x14, 0x14, 0x14));
}

#[test]
fn every_manifest_sidebar_tab_is_clickable_at_the_default_and_a_wide_sidebar() {
    // 28 is the default width: Git must fit there (it used to be omitted below 33).
    for width in [28u16, 40] {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
        app.ui.left_width = width;
        for entry in &Manifest::load().navigation.left_sidebar.tab {
            draw(&mut app, 160, 40);
            let tab = nexus_tui::ui::Tab::from_id(&entry.id).unwrap();
            let rect = app.hits.rect_of(&Target::Tab(tab)).expect("manifest tab");
            assert_eq!(rect.y, 0);
            assert!(
                rect.right() <= app.regions.left.unwrap().right(),
                "{width}: {}",
                entry.id
            );
            click(&mut app, rect.x, rect.y);
            assert_eq!(
                app.ui.tab, tab,
                "{width}: {} tab activates on click",
                entry.id
            );
        }
    }
}

#[test]
fn sidebar_search_shortcut_tracks_the_right_edge_when_resized() {
    for width in [24, 48] {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
        app.ui.left_width = width;
        let (_, rows) = draw(&mut app, 160, 40);
        let left = app.regions.left.unwrap();
        let row = rows.iter().find(|row| row.contains("ctrl+k")).unwrap();
        assert_eq!(
            col_of(row, "ctrl+k") + "ctrl+k".len(),
            left.right() as usize - 2
        );
    }
}

#[test]
fn a_viewed_subagent_keeps_the_root_tab_and_names_itself_below() {
    let mut app = subagent_fixture();
    let (_, rows) = draw(&mut app, 160, 40);
    // the group's strip names the root only; the child has no tab, and the
    // header's breadcrumb names the chain
    let strip = app.ui.groups.rects[0].1.y as usize;
    assert!(rows[strip].contains("Root task"), "{:?}", rows[strip]);
    assert!(!rows[strip].contains("Child task"), "{:?}", rows[strip]);
    assert!(rows[0].contains("Child task"), "{:?}", rows[0]);
    assert_eq!(app.tab_ids(), vec!["ses_parent".to_string()]);
    // The one heading row names the viewed child on the left and its four metadata
    // values on the right; there is no second subagent-tab row.
    let rail = app.regions.back.expect("root-back rail");
    assert_eq!(rail.width, 4);
    assert_eq!(rail.height, app.regions.centre.height);
    assert_eq!(rail.right(), app.regions.centre.x);
    assert_eq!(rail.y, app.regions.centre.y, "the rail spans the strip row too");
    let heading = centre_row(&app, &rows, app.regions.heading.y);
    assert!(heading.contains("Child task"), "{heading:?}");
    assert!(!heading.contains("Root task"), "{heading:?}");
    let mut at = heading.find("Child task").expect("title");
    for field in ["Scout", "gpt-6", "codex", "high"] {
        let next = heading
            .find(field)
            .unwrap_or_else(|| panic!("{field}: {heading:?}"));
        assert!(next > at, "{field} after the previous field: {heading:?}");
        at = next;
    }
    assert!(
        !rows[2].contains("Scout Child task"),
        "no subagent strip: {:?}",
        rows[2]
    );
    // A root session keeps the blank heading row and has no rail or metadata.
    app.open_session("ses_parent");
    let (_, rows) = draw(&mut app, 160, 40);
    assert!(app.regions.back.is_none());
    let heading = centre_row(&app, &rows, app.regions.heading.y);
    assert_eq!(heading.trim(), "", "{heading:?}");
    assert!(!rows[2].contains("Scout"), "{:?}", rows[2]);
}

/// A root session `ses_parent` with the fixture session as its viewed child.
fn subagent_fixture() -> nexus_tui::app::App {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    let mut parent = app.state.model.sessions[SESSION].clone();
    parent.id = "ses_parent".into();
    parent.title = "Root task".into();
    parent.parent_id = None;
    app.state.model.sessions.insert(parent.id.clone(), parent);
    {
        let child = app.state.model.sessions.get_mut(SESSION).unwrap();
        child.parent_id = Some("ses_parent".into());
        child.agent = "scout".into();
        child.title = "Child task".into();
        child.model = Some("codex/gpt-6".into());
        child
            .extra
            .insert("effort".into(), serde_json::Value::String("high".into()));
    }
    app
}

/// One centre-column row, without the sidebars.
fn centre_row(app: &nexus_tui::app::App, rows: &[String], y: u16) -> String {
    let centre = app.regions.centre;
    rows[y as usize]
        .chars()
        .skip(centre.x as usize)
        .take(centre.width as usize)
        .collect()
}

#[test]
fn the_root_back_rail_and_left_jump_straight_to_the_root() {
    let mut app = subagent_fixture();
    let mut grandchild = app.state.model.sessions[SESSION].clone();
    grandchild.id = "ses_grandchild".into();
    grandchild.title = "Grandchild task".into();
    grandchild.parent_id = Some(SESSION.into());
    app.state
        .model
        .sessions
        .insert(grandchild.id.clone(), grandchild);
    app.open_session("ses_grandchild");
    draw(&mut app, 160, 40);
    assert_eq!(app.viewed_id().as_deref(), Some("ses_grandchild"));
    let back = app
        .hits
        .rect_of(&Target::SessionRootBack)
        .expect("back rail");
    assert_eq!(back, app.regions.back.expect("rail region"));
    // The whole rail is the hit area, not only the arrow on the heading row.
    click(&mut app, back.x, back.bottom() - 1);
    assert_eq!(app.viewed_id().as_deref(), Some("ses_parent"));

    // Left is the same gesture from the timeline, and skips the intermediate parent.
    app.open_session("ses_grandchild");
    app.ui.focus = nexus_tui::ui::Focus::Timeline;
    press(&mut app, &["left"]);
    assert_eq!(app.viewed_id().as_deref(), Some("ses_parent"));
}

#[test]
fn the_root_back_rail_reserves_the_full_session_height_without_overlapping_content() {
    let mut app = subagent_fixture();
    let (buf, _) = draw(&mut app, 160, 40);
    let rail = app.regions.back.expect("rail");
    // The arrow sits in the middle of the four-cell strip, not on the heading row.
    let arrow_y = rail.y + (rail.height - 1) / 2;
    assert_eq!(buf[(rail.x + 1, arrow_y)].symbol(), "←");
    let centre = app.regions.centre;
    assert_eq!(rail.x, app.regions.left.unwrap().right());
    assert_eq!(rail.height, centre.height);
    assert_eq!(rail.bottom(), app.regions.composer.bottom());
    for region in [
        app.regions.heading,
        app.regions.timeline,
        app.regions.composer,
    ] {
        assert_eq!(region.x, centre.x, "content starts right of the rail");
        assert!(region.x >= rail.right(), "content never overlaps the rail");
    }

    // A terminal group below does not change the rail: it belongs to the session page.
    open_terminal(&mut app, "t1", "Build", "running");
    draw(&mut app, 160, 40);
    app.execute("layout.focus_up");
    draw(&mut app, 160, 40);
    let rail = app.regions.back.expect("rail with the terminal group");
    assert_eq!(rail.height, centre.height);
}

#[test]
fn long_heading_metadata_leaves_a_readable_child_title() {
    let mut app = subagent_fixture();
    {
        let child = app.state.model.sessions.get_mut(SESSION).unwrap();
        child.title = "Readable child title that metadata must not crush".into();
        child.model =
            Some("vendor/a-very-long-model-name-that-keeps-going-and-going-past-the-tab".into());
    }
    let (_, rows) = draw(&mut app, 120, 36);
    let heading = centre_row(&app, &rows, app.regions.heading.y);
    assert!(
        heading.trim_start().starts_with("Readable child title"),
        "{heading:?}"
    );
    assert!(
        heading.contains('…'),
        "metadata still compacts: {heading:?}"
    );
}

#[test]
fn left_still_edits_the_composer_while_a_subagent_is_viewed() {
    let mut app = subagent_fixture();
    app.composer().set_text("ab");
    app.ui.focus = nexus_tui::ui::Focus::Composer;
    press(&mut app, &["left"]);
    assert_eq!(app.viewed_id().as_deref(), Some(SESSION));
    assert_eq!(app.composer_ref().text(), "ab");
    assert_eq!(app.composer_ref().cursor(), (0, 1));
}

#[test]
fn a_viewed_fork_reads_fork_in_the_heading_metadata() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    let mut parent = app.state.model.sessions[SESSION].clone();
    parent.id = "ses_parent".into();
    parent.title = "Root task".into();
    parent.parent_id = None;
    app.state.model.sessions.insert(parent.id.clone(), parent);
    {
        let child = app.state.model.sessions.get_mut(SESSION).unwrap();
        child.parent_id = Some("ses_parent".into());
        // A fork runs as its parent's agent but is labelled `fork`.
        child.agent = "build".into();
        child.title = "Child task".into();
        child.fork = Some(nexus_tui::api::ForkInfo {
            parent_id: "ses_parent".into(),
            inherited: 0,
        });
    }
    let (_, rows) = draw(&mut app, 160, 40);
    let heading = centre_row(&app, &rows, app.regions.heading.y);
    assert!(heading.contains("Child task"), "{heading:?}");
    assert!(heading.contains("Fork"), "{heading:?}");
    assert!(!heading.contains("Build"), "{heading:?}");
}

// ---- the composer card (DESIGN_PLAN.md D11) ---------------------------------

/// The cells of one buffer row as text.
fn row_text(buf: &ratatui::buffer::Buffer, y: u16) -> String {
    (0..buf.area.width).map(|x| buf[(x, y)].symbol()).collect()
}

/// A stored turn whose request reported `input` tokens.
fn with_usage(app: &mut nexus_tui::app::App, input: u64) {
    app.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/example".into());
    app.state.models = vec![
        serde_json::json!({"ref": "codex/example", "name": "Example Model", "context": 200000}),
    ];
    // Below `busy`'s id 40, so a test can still mark the session busy.
    summarize(
        app,
        30,
        fixture_summary(
            "codex/example",
            serde_json::json!({"input": input, "cached": 0, "output": 0, "reasoning": 0}),
        ),
    );
}

#[test]
fn composer_rows_are_edge_input_blank_control_edge_strip_at_every_width() {
    for (w, h) in [(80, 24), (120, 36), (200, 50)] {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (w, h));
        with_usage(&mut app, 82_000);
        let (buf, _) = draw(&mut app, w, h);
        let r = app.regions.composer;
        let card = |y: u16| row_text(&buf, y);
        let x = r.x as usize + 2;
        // Live row, then a full top padding row at the card's width (inset 2).
        let live: String = (r.x..r.right()).map(|x| buf[(x, r.y)].symbol()).collect();
        assert!(
            live.trim().is_empty(),
            "{w}: idle live row is blank: {live:?}"
        );
        let top = &buf[(r.x + 2, r.y + 1)];
        assert_eq!(top.symbol(), " ", "{w}");
        assert_ne!(
            top.bg,
            buf[(r.x + 1, r.y + 1)].bg,
            "{w}: top padding surface"
        );
        assert_eq!(buf[(r.x + 1, r.y + 1)].symbol(), " ", "{w}: inset");
        assert_eq!(buf[(r.right() - 3, r.y + 1)].bg, top.bg, "{w}");
        assert_eq!(buf[(r.right() - 2, r.y + 1)].symbol(), " ", "{w}");
        // Input at x+3 inside the card, a blank row, the control row.
        let input = card(r.y + 2);
        assert!(input[input.char_indices().nth(x + 3).unwrap().0..].starts_with("Type a message"));
        assert!(
            card(r.y + 3).replace('\u{2502}', "").trim().is_empty(),
            "{w}: blank row"
        );
        let control = card(r.bottom() - 4);
        assert!(control
            .chars()
            .skip(x + 3)
            .collect::<String>()
            .starts_with("Build"));
        assert_eq!(buf[(r.x + 2, r.bottom() - 3)].symbol(), "\u{2580}", "{w}");
        // Project strip on the canvas, flush with the bottom.
        assert!(!card(r.bottom() - 2).contains("ctrl+p commands"), "{w}");
        assert_eq!(
            buf[(r.x + 6, r.bottom() - 2)].bg,
            Color::Rgb(0x0b, 0x0b, 0x0b)
        );
        assert_eq!(r.bottom(), h, "{w}: the composer ends at the bottom");
    }
}

#[test]
fn context_status_stays_visible_on_narrow_composers() {
    for w in (50..=200).step_by(2) {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (w, 36));
        with_usage(&mut app, 82_000);
        let (_, rows) = draw(&mut app, w, 36);
        let line = &rows[app.regions.composer.bottom() as usize - 4];
        assert!(
            line.contains("82.0K") && line.contains("41.0%"),
            "{w}: {line}"
        );
        assert!(!line.contains('╱'), "{line}");
    }
}

#[test]
fn mono_and_ascii_cards_use_rules_and_only_they_keep_the_focus_bar() {
    for tier in [Tier::Mono, Tier::Ansi16] {
        let mut app = with_turn(opts(Mode::Dark, tier), (120, 36));
        let (buf, _) = draw(&mut app, 120, 36);
        let r = app.regions.composer;
        let focused = app.ui.focus == nexus_tui::ui::Focus::Composer;
        let rule = app.theme.structure("rule");
        let (top, bottom) = (&buf[(r.x + 6, r.y + 1)], &buf[(r.x + 6, r.bottom() - 3)]);
        if tier == Tier::Mono {
            assert_eq!(top.symbol(), " ");
            assert_eq!(bottom.symbol(), rule);
        }
        let bar = buf[(r.x + 2, r.y + 3)].symbol() == app.theme.structure("focus_bar");
        assert_eq!(
            bar, focused,
            "{tier:?}: the focus bar is drawn when focused"
        );
    }
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let (buf, _) = draw(&mut app, 120, 36);
    let r = app.regions.composer;
    assert_ne!(
        buf[(r.x + 2, r.y + 2)].symbol(),
        app.theme.structure("focus_bar")
    );
    let mut ascii = opts(Mode::Dark, Tier::TrueColor);
    ascii.ascii = true;
    let mut app = with_turn(ascii, (120, 36));
    let (buf, _) = draw(&mut app, 120, 36);
    let r = app.regions.composer;
    assert_eq!(buf[(r.x + 6, r.y + 1)].symbol(), " ");
    assert!(buf[(r.x + 6, r.y + 1)].symbol().is_ascii());
}

#[test]
fn working_strip_aligns_with_usage_and_omits_output_tokens() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    with_usage(&mut app, 82_000);
    busy(&mut app);
    let (_, rows) = draw(&mut app, 160, 48);
    let control = rows.iter().find(|row| row.contains("82.0K")).unwrap();
    let strip = rows.iter().find(|row| row.contains("Working ·")).unwrap();
    assert!(strip.contains("Working"), "{strip}");
    assert!(!strip.contains('↓'), "{strip}");
    assert_eq!(
        control.trim_end().chars().count(),
        strip.trim_end().chars().count(),
        "control: {control:?}\nstrip: {strip:?}"
    );
}

#[test]
fn busy_shows_the_working_row_and_not_the_meter() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    with_usage(&mut app, 82_000);
    busy(&mut app);
    let (buf, rows) = draw(&mut app, 160, 48);
    let r = app.regions.composer;
    // `Working · 12s` sits below the card, by the strip, with no spinner glyph
    // (INTERFACE_PLAN "Composer", 2026-10-28 revision).
    let control_y = r.bottom() as usize - 4;
    let live_y = rows
        .iter()
        .position(|row| row.contains("Working \u{b7}"))
        .expect("a Working row");
    assert!(live_y > control_y, "{rows:?}");
    assert!(!rows[live_y].contains('\u{25d0}'), "{:?}", rows[live_y]);
    let control = &rows[control_y];
    assert!(!control.contains('\u{2571}'), "{control:?}");
    assert!(!rows[r.bottom() as usize - 1].contains("ctrl+p commands"));
    // No old busy marker inside the card.
    assert!(!rows[r.y as usize + 2].contains("Working"));
    let _ = buf;
}

#[test]
fn the_strip_marks_a_linked_worktree_with_a_muted_icon() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "session_directory": "/work/nexus-home/ws",
            "worktrees": [{"path": "/work/nexus-home/ws", "branch": "feature", "main": false}]
        }),
    );
    let (buf, rows) = draw(&mut app, 160, 48);
    let y = app.regions.composer.bottom() - 2;
    let x = col_of(&rows[y as usize], "⑂") as u16;
    let cell = &buf[(x, y)];
    assert_eq!(cell.fg, app.theme.fg("text.muted").fg.unwrap());
    assert!(!cell.modifier.contains(ratatui::style::Modifier::BOLD));
    assert!(!rows[y as usize].contains("worktree"));
    assert!(!rows[y as usize].contains("checkout"));
}

#[test]
fn completion_is_an_overlay_with_a_footer_and_a_focus_bar() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let before = draw(&mut app, 120, 36).1;
    typing(&mut app, "/");
    let timeline = app.regions.timeline;
    let (buf, rows) = draw(&mut app, 120, 36);
    let r = app.regions.composer;
    assert_eq!(
        app.regions.timeline, timeline,
        "the overlay takes no layout rows"
    );
    // The footer sits on the card's top edge row, left-aligned with the card.
    let footer = &rows[r.y as usize];
    assert!(footer.contains("\u{2191}\u{2193} move"), "{footer}");
    assert!(footer.contains("tab accept") && footer.contains("esc close"));
    assert!(footer.contains("1 of "), "{footer}");
    // Eight rows at most, the first selected with the focus bar in the card's column.
    let top = r.y - 8;
    assert_eq!(buf[(r.x + 2, top)].symbol(), "\u{258c}");
    assert_ne!(buf[(r.x + 2, top - 1)].symbol(), "\u{258c}");
    assert_ne!(buf[(r.x + 1, top)].symbol(), "\u{258c}");
    // Above the overlay the timeline is untouched.
    for y in 0..top as usize {
        assert_eq!(rows[y], before[y], "row {y}");
    }
}

/// The left sidebar's text, cut at its width (the centre column starts after it).
fn sidebar(rows: &[String], width: usize) -> Vec<String> {
    rows.iter()
        // The project rail takes the first cells of every body row.
        .map(|r| r.chars().skip(6).take(width).collect::<String>())
        .collect()
}

#[test]
fn session_rows_follow_d03_at_sidebar_widths_22_28_and_34() {
    for w in [22u16, 28, 34] {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
        app.ui.left_width = w;
        app.state.model.sessions.get_mut(SESSION).unwrap().title = "Refactor the loader".into();
        // Keep the fixture's age in the hour range regardless of the day this runs.
        app.state.model.sessions.get_mut(SESSION).unwrap().updated = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
            - 3600.0;
        app.ui.unread_sessions.insert(SESSION.into());
        let parent = app.state.model.sessions[SESSION].clone();
        let mut child = parent.clone();
        child.id = "ses_child".into();
        child.parent_id = Some(SESSION.into());
        app.state.model.sessions.insert(child.id.clone(), child);
        app.state.worktrees.insert(
            SESSION.into(),
            serde_json::json!({
                "session_directory": "/work/nexus-home/ws",
                "worktrees": [{"path": "/work/nexus-home", "branch": "main"}]
            }),
        );
        let (buf, rows) = draw(&mut app, 160, 40);
        let side = app.regions.left.expect("sidebar");
        assert_eq!(side.width, w, "width {w}");
        let text = sidebar(&rows, (w - 1) as usize);
        let first = text
            .iter()
            .position(|l| l.contains("⌕"))
            .expect("search row");
        // The list is above the pinned New / Search / Show archived footer.
        let key = col_of(&text[first], "ctrl+k");
        assert_eq!(
            text[first].find('⌕').map(|_| col_of(&text[first], "⌕")),
            Some(1)
        );
        assert!(
            key + 6 < w as usize,
            "{w}: key hint inside: {:?}",
            text[first]
        );
        let title_y = text
            .iter()
            .position(|line| line.contains("Refactor"))
            .expect("session title row");
        let title = &text[title_y];
        let detail = &text[title_y + 1];
        assert!(text[title_y + 2].trim().is_empty(), "spacer {w}");
        assert!(title_y + 2 < first, "list precedes footer {w}");
        assert!(text[first - 1].contains("+ New session"), "{w}");
        assert!(text[first + 1].contains("Show archived"), "{w}");
        // The open session's active bar at x=0, the mark slot at x=1, title at x=3, time
        // right-aligned with a margin of 1.
        assert!(title.starts_with("▏●"), "{w}: unread mark: {title:?}");
        assert_eq!(col_of(title, "Refactor"), 3, "{w}");
        assert!(
            title.trim_end().ends_with('h') || title.trim_end().ends_with("now"),
            "{title:?}"
        );
        assert!(
            nexus_tui::text::width(title.trim_end()) < w as usize - 1,
            "{w}: margin"
        );
        // line 2: the branch and the child count right-aligned.
        assert_eq!(col_of(detail, "⌥"), 3, "{w}");
        assert!(!detail.contains("build"), "{w}");
        assert!(detail.trim_end().ends_with("1 ↳"), "{w}: {detail:?}");
        assert!(
            nexus_tui::text::width(detail.trim_end()) < w as usize - 1,
            "{w}"
        );
        if w >= 28 {
            assert!(detail.contains("⌥ main"), "{w}: {detail:?}");
        }
        // The open session carries the active block on both lines, not on the spacer (PLAN 15.5).
        let band = buf[(w / 2, (title_y + 1) as u16)].bg;
        assert_eq!(band, buf[(w / 2, title_y as u16)].bg, "{w}: band vs header");
        assert_eq!(band, Color::Rgb(0x24, 0x24, 0x24), "{w}: active block");
        assert_eq!(
            buf[(w / 2, (title_y + 2) as u16)].bg,
            Color::Rgb(0x14, 0x14, 0x14),
            "{w}: the spacer stays on the sidebar"
        );
        // Settings is an icon on the separate project rail, not a sidebar row.
        assert!(!text.iter().any(|row| row.contains("Settings")), "{w}");
    }
}

#[test]
fn a_focused_session_row_has_the_bar_on_both_lines_and_headers_group_worktrees() {
    use nexus_tui::ui::Focus;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "worktree-session".into();
    other.directory = "/work/aaa".into();
    other.title = "Worktree task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.state.worktrees.insert(
        "worktree-session".into(),
        serde_json::json!({"session_directory": "/work/aaa",
            "worktrees": [{"path": "/work/aaa", "branch": "feat/cache", "main": false}]}),
    );
    app.ui.focus = Focus::Sidebar;
    let (_, rows) = draw(&mut app, 160, 40);
    let text = sidebar(&rows, 33);
    let tree = text
        .iter()
        .position(|l| l.contains("⌥ feat/cache"))
        .expect("worktree sub-group");
    assert_eq!(col_of(&text[tree], "⌥"), 2);
    assert!(
        text[tree].contains("/work/aaa") || text[tree].contains('…'),
        "{:?}",
        text[tree]
    );
    assert!(text[tree + 1].contains("Worktree task"));
    let cursor = app.cursor(nexus_tui::ui::Tab::Sessions);
    let id = &app.side_entries()[cursor].id;
    let title = text
        .iter()
        .position(|l| {
            l.contains(
                &app.state.model.sessions[id.as_str()]
                    .title
                    .chars()
                    .take(8)
                    .collect::<String>(),
            )
        })
        .unwrap();
    assert!(
        text[title].starts_with('▌') && text[title + 1].starts_with('▌'),
        "{text:#?}"
    );
}

#[test]
fn the_mark_slot_follows_attention_precedence() {
    // waiting > error > running > unread.
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    assert_eq!(app.session_mark(SESSION), None);
    app.ui.unread_sessions.insert(SESSION.into());
    assert_eq!(app.session_mark(SESSION), Some("unread"));
    busy(&mut app);
    assert_eq!(app.session_mark(SESSION), Some("running"));
    app.ui.failed_sessions.insert(SESSION.into());
    assert_eq!(app.session_mark(SESSION), Some("error"));
    app.state.model.approvals.insert(
        "perm".into(),
        serde_json::from_value(serde_json::json!({
            "id": "perm", "session_id": SESSION, "tool": "bash", "input": {}, "created": 1.0
        }))
        .unwrap(),
    );
    assert_eq!(app.session_mark(SESSION), Some("waiting"));
    // The mark has one blank cell before the title.
    let (_, rows) = draw(&mut app, 120, 36);
    let row = sidebar(&rows, 27)
        .into_iter()
        .find(|l| l.contains("New session") && l.contains('◆'))
        .expect("waiting mark on the title row");
    assert_eq!(col_of(&row, "New session") - col_of(&row, "◆"), 2);
}

#[test]
fn right_sidebar_uses_uppercase_labels_a_nine_cell_key_column_and_middle_truncation() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.model.sessions.get_mut(SESSION).unwrap().id =
        "ses_0123456789abcdefghijklmnopqrstuvwxyz".into();
    let (_, rows) = draw(&mut app, 120, 36);
    let right = app.regions.right.expect("right sidebar");
    let col = |row: &String| -> String { row.chars().skip(right.x as usize + 2).collect() };
    let lines: Vec<String> = rows.iter().map(col).collect();
    for label in ["SESSION", "CONTEXT"] {
        assert!(
            lines.iter().any(|l| l.trim_end() == label),
            "{label}: {lines:#?}"
        );
    }
    let id = lines.iter().find(|l| l.starts_with("Id")).expect("Id row");
    assert_eq!(
        col_of(id, "ses_"),
        9,
        "values start after a 9-cell key column: {id:?}"
    );
    assert!(
        id.contains('…') && id.trim_end().ends_with("wxyz"),
        "middle truncation: {id:?}"
    );
    // Core reports no usage here: the rows say so.
    for key in ["Used", "Tokens", "Cost"] {
        let row = lines.iter().find(|l| l.starts_with(key)).expect(key);
        assert!(row.contains("not reported"), "{row:?}");
    }
}

#[test]
fn the_row_menu_is_a_raised_popover_beside_the_row_and_delete_asks_to_confirm() {
    use nexus_tui::ui::{Overlay, OverlayKind, PaletteMode};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let row = app.hits.rect_of(&Target::SideRow(0)).expect("row");
    right_click(&mut app, row.x + 2, row.y);
    assert!(matches!(
        app.ui.overlay,
        Some(Overlay { kind: OverlayKind::Palette(ref p), .. }) if p.mode == PaletteMode::RowMenu
    ));
    let (buf, rows) = draw(&mut app, 120, 36);
    let delete = rows
        .iter()
        .position(|l| l.contains("Delete session"))
        .expect("Delete item");
    let x = col_of(&rows[delete], "Delete session") as u16;
    assert!(
        x < row.right() + 4,
        "popover sits beside the row, not centred: {x}"
    );
    assert!(
        rows.iter().all(|l| !l.contains("choose an action")),
        "no search row"
    );
    let menu_bg = buf[(x, delete as u16)].bg;
    let page_bg = buf[(100, 5)].bg;
    assert_ne!(menu_bg, page_bg, "raised surface");
    assert_eq!(
        buf[(x, delete as u16)].fg,
        app.theme.color("status.error").unwrap()
    );
    // Choosing Delete opens the existing confirmation.
    let rect = app.hits.rect_of(&Target::PaletteItem(
        app.palette_items()
            .iter()
            .position(|i| i.label == "Delete session")
            .unwrap(),
    ));
    let rect = rect.expect("item hit");
    click(&mut app, rect.x + 3, rect.y);
    assert!(app.ui.dialog.is_some(), "confirm dialog");
}

#[test]
fn the_rail_marks_the_open_project_with_the_selection_band() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.ui.left_width = 10;
    app.ui.left_open = false;
    let (buf, _) = draw(&mut app, 120, 36);
    let band = app
        .hits
        .rect_of(&Target::RailProject(0))
        .expect("rail item for the open project");
    assert_eq!(
        Some(buf[(band.x + 1, band.y + 1)].bg),
        app.theme.bg("surface.raised").bg
    );
    app.mouse = Some((band.x + 2, band.y + 1));
    let (buf, _) = draw(&mut app, 120, 36);
    for y in band.y..=band.y + 2 {
        assert_eq!(
            Some(buf[(band.x + 1, y)].bg),
            app.theme.bg("surface.raised").bg
        );
        assert_eq!(
            Some(buf[(band.right() - 2, y)].bg),
            app.theme.bg("surface.raised").bg
        );
        assert_eq!(buf[(band.x, y)].symbol(), "▏");
        assert_eq!(
            buf[(band.x, y)].fg,
            app.theme.color("text.primary").unwrap()
        );
    }
}

// ---- DESIGN_PLAN I7: one modal recipe ----------------------------------

/// The bounding box of the cells painted `surface.raised`; under a scrim
/// everything else is blended, so this is the modal block.
fn raised_box(app: &nexus_tui::app::App, buf: &ratatui::buffer::Buffer) -> ratatui::layout::Rect {
    let raised = app.theme.color("surface.raised").unwrap();
    let (mut x0, mut y0, mut x1, mut y1) = (u16::MAX, u16::MAX, 0, 0);
    for y in 0..buf.area.height {
        for x in 0..buf.area.width {
            if buf[(x, y)].bg == raised {
                x0 = x0.min(x);
                y0 = y0.min(y);
                x1 = x1.max(x + 1);
                y1 = y1.max(y + 1);
            }
        }
    }
    assert!(x1 > x0, "no raised block drawn");
    ratatui::layout::Rect::new(x0, y0, x1 - x0, y1 - y0)
}

fn open_modal(app: &mut nexus_tui::app::App, which: &str) {
    match which {
        "palette" => press(app, &["ctrl+p"]),
        "settings" => app.execute("app.settings"),
        "help" => app.execute("app.help_keys"),
        "confirm" => {
            press(app, &["ctrl+p"]);
            typing(app, "delete session");
            press(app, &["enter"]);
            assert!(app.ui.dialog.is_some(), "confirm dialog");
        }
        other => panic!("{other}"),
    }
}

#[test]
fn a_modal_scrims_the_whole_frame_halfway_toward_the_canvas() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let (before, _) = draw(&mut app, 120, 36);
    open_modal(&mut app, "palette");
    let (after, _) = draw(&mut app, 120, 36);
    let Some(Color::Rgb(cr, cg, cb)) = app.theme.color("surface.canvas") else {
        panic!("canvas is rgb in truecolor");
    };
    let half = |a: u8, b: u8| (u16::from(a) + u16::from(b)).div_ceil(2) as u8;
    let mut checked = 0;
    // the corners and edges are far from the block: every one is blended
    for (x, y) in [(0, 0), (119, 0), (0, 35), (119, 35), (60, 1), (118, 20)] {
        let (Color::Rgb(r, g, b), Color::Rgb(ar, ag, ab)) = (before[(x, y)].bg, after[(x, y)].bg)
        else {
            continue;
        };
        assert_eq!(
            (ar, ag, ab),
            (half(r, cr), half(g, cg), half(b, cb)),
            "bg at {x},{y}"
        );
        checked += 1;
    }
    assert!(checked >= 3, "enough rgb cells compared");
}

#[test]
fn modals_are_borderless_padded_blocks_within_two_cells_of_each_edge() {
    use ratatui::layout::Rect;
    let boxes = ["─", "│", "╭", "╮", "╰", "╯", "┌", "┐", "└", "┘"];
    for (w, h) in [(120u16, 36u16), (80, 24)] {
        for which in ["palette", "settings", "help", "confirm"] {
            let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (w, h));
            open_modal(&mut app, which);
            let (buf, rows) = draw(&mut app, w, h);
            let r = raised_box(&app, &buf);
            let ctx = format!("{which} at {w}x{h}: {r:?}");
            let frame = Rect::new(2, 2, w - 4, h - 4);
            assert!(frame.contains((r.x, r.y).into()), "{ctx}");
            assert!(
                r.right() <= frame.right() && r.bottom() <= frame.bottom(),
                "{ctx}"
            );
            assert!(u32::from(r.height) <= u32::from(h) * 7 / 10, "{ctx}");
            // padding: 2 cells left and right, a blank row top and bottom
            for x in r.x..r.right() {
                for y in [r.y, r.bottom() - 1] {
                    assert_eq!(buf[(x, y)].symbol(), " ", "{ctx}: padding row");
                }
            }
            for y in r.y..r.bottom() {
                for x in [r.x, r.x + 1, r.right() - 2, r.right() - 1] {
                    let sym = buf[(x, y)].symbol();
                    // only a selection bar may sit in the side padding
                    assert!(sym == " " || sym == "▌", "{ctx}: {sym:?} at {x},{y}");
                }
            }
            // no cell of the block carries a box-drawing border; the title is
            // the first row inside, hints the last row
            for y in r.y..r.bottom() {
                for x in r.x..r.right() {
                    let sym = buf[(x, y)].symbol();
                    let edge = x == r.x || x == r.right() - 1 || y == r.y || y == r.bottom() - 1;
                    assert!(!(edge && boxes.contains(&sym)), "{ctx}: border {sym}");
                }
            }
            let title = &rows[r.y as usize + 1];
            assert!(!title.contains("esc"), "{ctx}: title repeats Esc: {title}");
            assert_eq!(r.x, (w - r.width) / 2, "{ctx}: horizontally centred");
            assert_eq!(r.y, (h - r.height) / 2, "{ctx}: vertically centred");
            assert!(
                buf[(r.x + 2, r.y + 1)]
                    .modifier
                    .contains(ratatui::style::Modifier::BOLD),
                "{ctx}: bold title"
            );
            let hints = &rows[r.bottom() as usize - 2];
            assert!(hints.contains("esc"), "{ctx}: {hints}");
        }
    }
}

#[test]
fn settings_highlights_the_whole_selected_section_and_row_in_gray() {
    let scenario: serde_json::Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/settings-focus.json"
    ))
    .unwrap();
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_modal(&mut app, "settings");
    let (buf, rows) = draw(&mut app, 120, 36);
    let r = raised_box(&app, &buf);
    let nav = app.hits.rect_of(&Target::SettingsSection(0)).unwrap();
    let selection = app.theme.color("interactive.settings_selection").unwrap();
    for x in nav.x..nav.right() {
        assert_eq!(buf[(x, nav.y)].bg, selection);
    }
    assert_eq!(buf[(nav.x, nav.y)].symbol(), "▌");
    assert_eq!(
        buf[(nav.x, nav.y)].fg,
        app.theme.color("interactive.settings_focus").unwrap()
    );
    assert!(r.contains((nav.x, nav.y).into()));
    // The highlight includes the label; the segmented value draws its own
    // well and selection band over it.
    let sel = app
        .hits
        .rect_of(&Target::SettingsRow(app.ui.settings_row))
        .unwrap();
    for x in sel.x..sel.x + 5 {
        assert_eq!(buf[(x, sel.y)].bg, selection, "band at {x}");
    }
    assert_eq!(buf[(sel.x, sel.y)].symbol(), " ");
    assert_ne!(
        buf[(sel.x + 5, sel.y)].bg,
        app.theme.color("interactive.accent").unwrap()
    );
    let selected = app.theme.color("interactive.selection").unwrap();
    let seg_rects: Vec<_> = (0..3)
        .filter_map(|option| {
            app.hits
                .rect_of(&Target::SettingsSegment {
                    row: app.ui.settings_row,
                    option,
                })
                .map(|rect| (option, rect))
        })
        .collect();
    assert_eq!(seg_rects.len(), 3, "the Theme row is a segmented control");
    let lit: Vec<_> = seg_rects
        .iter()
        .filter(|(_, rect)| buf[(rect.x, rect.y)].bg == selected)
        .collect();
    assert_eq!(lit.len(), 1, "exactly one segment is selected");
    assert!(r.contains((sel.x, sel.y).into()));
    // a row that is not selected stays on the raised surface
    let other = app
        .hits
        .rect_of(&Target::SettingsRow(app.ui.settings_row + 1))
        .unwrap();
    assert_eq!(
        buf[(other.x + 5, other.y)].bg,
        app.theme.color("surface.raised").unwrap()
    );
    assert!(rows.join("\n").contains("Appearance"));
    press(&mut app, &[scenario["content_key"].as_str().unwrap()]);
    let (buf, _) = draw(&mut app, 120, 36);
    assert_eq!(buf[(sel.x, sel.y)].symbol(), "▌");
    assert_eq!(buf[(nav.x, nav.y)].symbol(), " ");
    app.effects.clear();
    press(&mut app, &[scenario["tui_return_key"].as_str().unwrap()]);
    assert!(app.ui.settings_nav_focus);
    assert!(
        app.effects.is_empty(),
        "Returning focus must not change a setting"
    );
    let (buf, _) = draw(&mut app, 120, 36);
    assert_eq!(buf[(nav.x, nav.y)].symbol(), "▌");
    assert_eq!(buf[(sel.x, sel.y)].symbol(), " ");
    assert_eq!(buf[(lit[0].1.x, lit[0].1.y)].bg, selected);
    press(&mut app, &[scenario["next_section_key"].as_str().unwrap()]);
    assert_eq!(
        app.ui.settings_section,
        scenario["next_section"].as_str().unwrap()
    );
}

#[test]
fn confirm_defaults_to_cancel_and_marks_delete_as_an_error() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_modal(&mut app, "confirm");
    let (buf, rows) = draw(&mut app, 120, 36);
    let y = rows.iter().position(|l| l.contains("[ Cancel ]")).unwrap();
    let cx = col_of(&rows[y], "[ Cancel ]") as u16;
    let dx = col_of(&rows[y], "[ Delete ]") as u16;
    let selection = app.theme.color("surface.raised").unwrap();
    assert_eq!(buf[(cx + 2, y as u16)].bg, selection, "Cancel has no fill");
    assert_eq!(buf[(cx - 1, y as u16)].symbol(), "▌", "focus bar");
    assert_eq!(
        buf[(dx + 2, y as u16)].fg,
        app.theme.color("status.error").unwrap()
    );
    assert_eq!(buf[(dx + 2, y as u16)].bg, selection);
    // right/left moves the default onto Delete
    press(&mut app, &["right"]);
    let (buf, _) = draw(&mut app, 120, 36);
    assert_eq!(buf[(dx + 2, y as u16)].bg, selection);
}

/// An app on the new-session view with a project at `dir` and the given agent.
fn new_session_app(dir: &std::path::Path, size: (u16, u16)) -> nexus_tui::app::App {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), size);
    app.state.agents = vec![serde_json::json!({
        "name": "build", "source": "builtin", "description": "Does the actual work.",
        "prompt": "# Harness\n\nYou are an AI agent running in Nexus.\n\n{{tools}}",
        "tools": ["read", "bash", "edit", "write"]
    })];
    for p in app.state.model.projects.values_mut() {
        p.path = dir.to_string_lossy().into_owned();
    }
    app
}

fn scratch_dir(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("nexus-tui-i9-{name}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn the_new_session_view_shows_the_header_block_and_d10_sections_with_agents_md() {
    let dir = scratch_dir("with");
    std::fs::write(
        dir.join("AGENTS.md"),
        "# Rules\n\nRead PLAN.md first: principles, layout.\n",
    )
    .unwrap();
    let mut app = new_session_app(&dir, (120, 36));
    let request = app.draft_request().unwrap();
    app.state.apply(nexus_tui::sync::SyncMsg::DraftContext {
        request,
        data: Ok(serde_json::json!({"tools": ["read", "grep", "glob", "ls", "bash"], "skills": [], "mcp": []})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    for needle in [
        "System prompt",
        "Skills",
        "MCP",
        "AGENTS.md",
        "Tools [5]",
        "total  ~",
    ] {
        assert!(all.contains(needle), "missing {needle:?}:\n{all}");
    }
    // No folded context line: the sections are the preview.
    assert!(!all.contains("Context ·"), "{all}");
    assert!(!all.contains("You are an AI agent running in Nexus."));
    // The description is for the pickers; the model never receives it.
    assert!(!all.contains("Does the actual work."), "{all}");
    // Estimated until Core drafts land: every count carries `~`.
    assert!(all.contains("~") && all.contains(" tok"), "{all}");
    // The centred placeholder is gone, and there is no bar in the total row.
    assert!(!all.contains("Type a message below"), "{all}");
    let total = rows.iter().find(|l| l.contains("total  ~")).unwrap();
    assert!(!total.contains('\u{2571}'), "{total}");
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn changing_effort_keeps_core_starting_context_on_screen() {
    let dir = scratch_dir("effort");
    let mut app = new_session_app(&dir, (120, 36));
    let request = app.draft_request().unwrap();
    app.state.apply(nexus_tui::sync::SyncMsg::DraftContext {
        request,
        data: Ok(serde_json::json!({
            "used": 3210,
            "window": 200000,
            "exact": false,
            "parts": {"instructions": 2500, "tools": 700, "instruction_file": 120},
            "tools": ["read", "bash"],
            "skills": [],
            "mcp": []
        })),
    });
    let (_, before) = draw(&mut app, 120, 36);
    assert!(joined(&before).contains("3,210 tok"), "{}", joined(&before));

    // Effort steers the model; it is not part of the starting context, so Core's
    // last plan stays on screen until the new one arrives (and has the same counts).
    app.ui.draft.effort = Some("high".into());
    assert!(
        app.draft_context().is_some(),
        "effort must not invalidate the plan"
    );
    let (_, after) = draw(&mut app, 120, 36);
    // The starting-context block does not move; only the effort label below it changes.
    let context = |rows: &[String]| {
        joined(rows)
            .split("Message to start")
            .next()
            .unwrap()
            .to_owned()
    };
    assert_eq!(context(&before), context(&after));
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn preview_context_headings_open_details_on_mouse_click() {
    use nexus_tui::ui::OverlayKind;
    let path = std::env::temp_dir();
    for heading in ["System prompt", "AGENTS.md", "Tools"] {
        let mut app = new_session_app(&path, (120, 36));
        let (_, rows) = draw(&mut app, 120, 36);
        let y = rows.iter().position(|row| row.contains(heading)).unwrap();
        let x = col_of(&rows[y], heading);
        click(&mut app, x as u16, y as u16);
        assert!(
            matches!(
                &app.ui.overlay.as_ref().map(|o| &o.kind),
                Some(OverlayKind::ContextDoc { .. } | OverlayKind::ContextEntries { .. })
            ),
            "{heading} must open a preview modal"
        );
    }
}

#[test]
fn new_session_preview_names_disabled_project_mcp_and_opens_its_details() {
    use nexus_tui::sync::SyncMsg;
    use nexus_tui::ui::OverlayKind;
    let dir = scratch_dir("mcp-inventory");
    let mut app = new_session_app(&dir, (160, 60));
    let project = app.current_project().unwrap();
    let request = app.draft_request().unwrap();
    app.state.apply(SyncMsg::DraftContext {
        request,
        data: Ok(serde_json::json!({
            "tools": [],
            "skills": [],
            "mcp": [{"server": "browser-use", "state": "on", "tools": 4, "locked": false, "scope": "project"}]
        })),
    });
    app.apply_sync(SyncMsg::SkillMcp {
        project: project.clone(),
        skills: Ok(serde_json::json!({"skills": []})),
        mcp: Ok(serde_json::json!({"servers": [
            {"server": "browser-use", "state": "disabled", "tools": 0, "enabled": false},
            {"server": "docs", "state": "connected", "tools": 2, "enabled": true}
        ]})),
        project_mcp: Ok(serde_json::json!({
            "project_id": project.clone(),
            "file": "/repo/.mcp.json",
            "exists": true,
            "revision": "rev",
            "error": null,
            "warnings": [],
            "servers": [
                {
                    "server": "browser-use", "state": "idle", "error": null,
                    "tools": 0, "enabled": true, "source": "project", "project_id": project.clone(),
                    "file": "/repo/.mcp.json", "transport": "stdio", "command": "npx",
                    "arg_count": 2, "cwd": "/repo", "env_names": ["TOKEN"]
                },
                {
                    "server": "switched-off", "state": "disabled", "error": null,
                    "tools": 0, "enabled": false, "source": "project", "project_id": project.clone(),
                    "file": "/repo/.mcp.json", "transport": "stdio", "command": "fake-server",
                    "arg_count": 1, "cwd": "/repo", "env_names": []
                },
                {
                    "server": "broken", "state": "invalid",
                    "error": "unknown key 'timeout'", "tools": 0, "enabled": false,
                    "source": "project", "project_id": project.clone(),
                    "file": "/repo/.mcp.json"
                },
                {
                    "server": "(file)", "state": "invalid",
                    "error": ".mcp.json is not valid JSON: trailing comma", "tools": 0,
                    "enabled": false, "source": "project", "project_id": project,
                    "file": "/repo/.mcp.json"
                }
            ]
        })),
    });
    let (_, rows) = draw(&mut app, 160, 60);
    let all = joined(&rows);
    assert!(
        all.contains("MCP [5]"),
        "the inventory is listed:
{all}"
    );
    assert!(
        !all.contains("MCP  none"),
        "the preview must not claim there are no MCP entries:\n{all}"
    );
    let live = rows
        .iter()
        .find(|row| row.contains("browser-use"))
        .expect("enabled project row");
    assert!(live.contains('●') && !live.contains('○'), "{live}");
    assert!(live.contains("4 tools"), "{live}");
    assert!(!live.contains("disabled"), "{live}");
    assert!(!live.contains("new session"), "{live}");
    let disabled = rows
        .iter()
        .find(|row| row.contains("switched-off"))
        .expect("disabled row");
    assert!(
        disabled.contains('○') && !disabled.contains('●'),
        "{disabled}"
    );
    assert!(disabled.contains("disabled in project MCP"), "{disabled}");
    assert!(
        !disabled.contains("new session"),
        "the preview is already the new session: {disabled}"
    );
    let invalid = rows
        .iter()
        .find(|row| row.contains("broken"))
        .expect("invalid row");
    assert!(invalid.contains("invalid"), "{invalid}");
    assert!(
        invalid.contains("unknown key 'timeout'"),
        "the error is actionable: {invalid}"
    );
    assert!(
        !invalid.contains("new session"),
        "an invalid entry is not available in a new session: {invalid}"
    );
    assert!(invalid.contains('○') && !invalid.contains('●'), "{invalid}");
    let file = rows
        .iter()
        .find(|row| row.contains("(file)"))
        .expect("whole-file invalid row");
    assert!(file.contains("not valid JSON"), "{file}");
    assert!(!file.contains("new session"), "{file}");

    // Clicking a disabled row explains its current configuration and the exact
    // action, rather than offering a locked "start a new session" dead end.
    let y = rows
        .iter()
        .position(|row| row.contains("switched-off"))
        .unwrap() as u16;
    let x = col_of(&rows[y as usize], "switched-off");
    click(&mut app, x as u16, y);
    let detail = match app.ui.overlay.as_ref().map(|o| &o.kind) {
        Some(OverlayKind::ContextEntries {
            entries,
            open: Some(i),
            ..
        }) => entries[*i].full.clone().unwrap_or_default(),
        other => panic!("disabled row must open its details, got {other:?}"),
    };
    assert!(detail.contains("State: disabled"), "{detail}");
    assert!(detail.contains("never started"), "{detail}");
    assert!(detail.contains("\"enabled\": true"), "{detail}");
    assert!(detail.contains("/repo/.mcp.json"), "{detail}");
    assert!(detail.contains("Command: fake-server"), "{detail}");
    assert!(detail.contains("Arguments: 1"), "{detail}");
    assert!(!detail.contains("approval"), "{detail}");
    assert!(!detail.contains("new session to use it"), "{detail}");
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn the_new_session_view_without_agents_md_says_none_and_counts_less() {
    let with = scratch_dir("a");
    std::fs::write(with.join("AGENTS.md"), "x".repeat(4000)).unwrap();
    let without = scratch_dir("b");
    let total = |dir: &std::path::Path| -> u64 {
        let mut app = new_session_app(dir, (120, 36));
        let all = joined(&draw(&mut app, 120, 36).1);
        let line = all
            .lines()
            .find(|l| l.contains("total  ~"))
            .unwrap()
            .to_owned();
        let digits: String = line
            .split("~")
            .nth(1)
            .unwrap()
            .chars()
            .take_while(|c| c.is_ascii_digit() || *c == ',')
            .filter(char::is_ascii_digit)
            .collect();
        digits.parse().unwrap()
    };
    let mut app = new_session_app(&without, (120, 36));
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("AGENTS.md"), "{all}");
    assert!(all.contains("unavailable or not read"), "{all}");
    assert!(
        total(&with) >= total(&without) + 1000,
        "AGENTS.md adds its estimate"
    );
    std::fs::remove_dir_all(&with).ok();
    std::fs::remove_dir_all(&without).ok();
}

#[test]
fn hovering_an_estimated_count_says_estimated_and_narrow_widths_clip_with_an_ellipsis() {
    let dir = scratch_dir("hover");
    let mut app = new_session_app(&dir, (80, 24));
    let (_, rows) = draw(&mut app, 80, 24);
    let y = rows
        .iter()
        .position(|l| l.contains("System prompt"))
        .expect("section");
    let x = rows[y].find('~').expect("estimate");
    let col = rows[y][..x].chars().count() as u16;
    app.mouse = Some((col, y as u16));
    let (_, rows) = draw(&mut app, 80, 24);
    assert!(joined(&rows).contains("estimated"), "{}", joined(&rows));
    std::fs::remove_dir_all(&dir).ok();
}

// ---- DESIGN_PLAN I10: files, narrow layouts and states -------------------------

#[test]
fn the_empty_workspace_shows_the_centred_logo_and_the_next_step() {
    use nexus_tui::paint::{logo_width, LOGO};
    for tier in [Tier::TrueColor, Tier::Mono] {
        let mut app = empty(opts(Mode::Dark, tier), (120, 36));
        app.state.model.projects.clear();
        let (buf, rows) = draw(&mut app, 120, 36);
        let all = joined(&rows);
        assert!(all.contains("No projects yet"), "{all}");
        assert!(
            all.contains("+ on the rail adds a project \u{b7} ctrl+p commands"),
            "{all}"
        );
        // five logo rows, one block: every row starts at the same column, so
        // the strokes keep their shape, and the block is centred on the page
        let centre = app.regions.timeline;
        let top = rows
            .iter()
            .position(|r| r.contains(LOGO[0].trim()))
            .expect("logo");
        let want = centre.x + 1 + ((centre.width - 2) as usize - logo_width()) as u16 / 2;
        for (i, row) in LOGO.iter().enumerate() {
            let y = top + i;
            let lead = row.len() - row.trim_start().len();
            assert_eq!(
                col_of(&rows[y], row.trim()) as u16,
                want + lead as u16,
                "logo row {i}: {:?}",
                rows[y]
            );
        }
        // accent, and bold in monochrome only
        let mark = (want..want + logo_width() as u16)
            .find(|x| buf[(*x, top as u16)].symbol() == "_")
            .unwrap();
        let cell = &buf[(mark, top as u16)];
        if tier == Tier::Mono {
            assert!(cell.modifier.contains(ratatui::style::Modifier::BOLD));
        } else {
            assert_eq!(cell.fg, app.theme.color("interactive.accent").unwrap());
            assert!(!cell.modifier.contains(ratatui::style::Modifier::BOLD));
        }
        // the title is below the logo, centred on the same axis
        let title = rows
            .iter()
            .position(|r| r.contains("No projects yet"))
            .unwrap();
        assert!(title > top + LOGO.len(), "title under the logo");
    }
}

#[test]
fn the_connecting_screen_uses_the_same_logo_rows() {
    use nexus_tui::state::ConnectionState as C;
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.connection = C::Connecting;
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    for row in nexus_tui::paint::LOGO {
        assert!(all.contains(row.trim()), "{row:?} missing:\n{all}");
    }
    assert!(all.contains("Connecting to Nexus Core"), "{all}");
    // a short page drops the logo rather than the text
    let (_, rows) = draw(&mut app, 120, 12);
    let all = joined(&rows);
    assert!(all.contains("Connecting to Nexus Core"), "{all}");
}

#[test]
fn a_cold_launch_shows_the_logo_without_a_progress_bar() {
    let mut app = nexus_tui::app::App::new(
        nexus_tui::state::AppState::new(),
        nexus_tui::theme::Theme::new(opts(Mode::Dark, Tier::TrueColor)),
        (120, 36),
    );
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    for row in nexus_tui::paint::LOGO {
        assert!(all.contains(row.trim()), "{row:?} missing:\n{all}");
    }
    // Plan 26: no bar, percentage or step label while Core starts.
    assert!(!all.contains("0%") && !all.contains("daemon"), "{all}");
}

#[test]
fn offline_says_so_in_the_header_the_banner_and_the_composer() {
    use nexus_tui::state::ConnectionState as C;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "draft");
    app.state.connection = C::Reconnecting {
        attempt: 1,
        retry_in: std::time::Duration::from_secs(4),
        reason: "stream closed".into(),
    };
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(rows[0].contains("\u{2715} offline"), "{:?}", rows[0]);
    assert!(
        all.contains("Disconnected from Core \u{b7} retrying in 4s"),
        "{all}"
    );
    // an empty composer says why it cannot send; the draft is never dropped
    app.composer().clear();
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        joined(&rows).contains("Reconnecting to Core\u{2026} your draft is kept"),
        "{}",
        joined(&rows)
    );
    // no projects loaded: the page does not claim there are none
    app.state.model.projects.clear();
    app.state.model.sessions.clear();
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("Not connected to Core"), "{all}");
    assert!(!all.contains("No projects yet"), "{all}");
}

#[test]
fn the_files_root_header_and_indent_guides_follow_d16() {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    let dir = app.files_root().unwrap();
    app.state.project_worktrees.insert(
        project.clone(),
        serde_json::json!({"project_directory": dir, "worktrees": [
            {"path": dir, "branch": "trunk", "main": true}
        ]}),
    );
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [
            {"name": "src", "kind": "directory"},
            {"name": "a.rs", "kind": "file", "git_status": " M"},
        ]})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let head = rows
        .iter()
        .find(|r| r.contains("\u{2325} trunk"))
        .expect("root header");
    let name = app.files_header().unwrap().name;
    assert!(head.contains(&name), "{head:?}");
    assert!(
        col_of(head, "\u{2325}") > col_of(head, &name) + name.len(),
        "the branch sits at the right: {head:?}"
    );
    assert!(
        !head.chars().take(28).any(|c| c == '\u{25be}'),
        "one checkout cannot be switched: {head:?}"
    );
    assert!(app.hits.rect_of(&Target::FilesRoot).is_none());

    // Enter expands `src`; its child gets one guide, a grandchild two.
    press(&mut app, &["enter"]);
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: "src".into(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "deep", "kind": "directory"}]})),
    });
    press(&mut app, &["down", "enter"]);
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: "src/deep".into(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "x.rs", "kind": "file"}]})),
    });
    let (buf, rows) = draw(&mut app, 120, 36);
    let y = rows.iter().position(|r| r.contains("x.rs")).unwrap();
    assert!(
        rows[y]
            .chars()
            .skip(6)
            .collect::<String>()
            .starts_with(" \u{2502} \u{2502}"),
        "two guides: {:?}",
        rows[y]
    );
    let border = app.theme.color("border.default").unwrap();
    assert_eq!(buf[(7, y as u16)].fg, border, "guides use border.default");
    let deep = rows.iter().position(|r| r.contains("deep")).unwrap();
    // (the selected row carries the focus bar in column 0)
    let guides = |row: &str| row.chars().skip(7).take(4).collect::<String>();
    assert_eq!(
        guides(&rows[deep]),
        "\u{2502} \u{25be} ",
        "{:?}",
        rows[deep]
    );

    // Git letters take their status colour through git_marker.
    let a = rows.iter().position(|r| r.contains("a.rs")).unwrap();
    let x = app.regions.left.unwrap().right() - 3;
    assert_eq!(buf[(x, a as u16)].symbol(), "M");
    assert_eq!(
        buf[(x, a as u16)].fg,
        app.theme.color("status.warning").unwrap()
    );
}

#[test]
fn a_file_is_a_tab_of_the_focused_group_at_every_width() {
    let mut app = open_file_view("hello\nworld\n");
    for (w, h) in [(160, 48), (120, 36), (80, 24)] {
        let (_, rows) = draw(&mut app, w, h);
        let panel = app.regions.panel.expect("the focused group's body");
        let group = app.ui.groups.rects[0].1;
        assert_eq!(panel, nexus_tui::layout::group_body(group), "{w}");
        assert!(panel.x > 0 && panel.right() <= w, "{w}");
        // The file's tab is in the strip; there is no modal frame or Back.
        assert!(rows[group.y as usize].contains("hello.txt"), "{w}");
        assert!(!rows.join("\n").contains("Back"), "{w}");
        assert!(app.hits.at(0, h - 1) != Some(&Target::PanelBack), "{w}");
        // The sidebar stays where it was.
        assert!(app.regions.left.is_some() || w < 100, "{w}");
    }
    assert!(app
        .groups()
        .contains(&nexus_tui::groups::Tab::file("hello.txt")));
    let before = app.composer_ref().text();
    press(&mut app, &["z"]);
    assert_eq!(
        app.composer_ref().text(),
        before,
        "typing stays out of the composer"
    );
    press(&mut app, &["alt+x"]);
    let dialog = app.ui.dialog.as_ref().expect("typed changes require confirmation");
    assert_eq!(dialog.title, "Save changes to hello.txt?");
    // Save, Discard, Cancel: Discard closes the tab and drops the text.
    press(&mut app, &["right", "enter"]);
    assert!(!app.ui.file_open, "discard closes the tab");
    assert!(!app
        .groups()
        .contains(&nexus_tui::groups::Tab::file("hello.txt")));
}

#[test]
fn long_file_lines_keep_the_last_line_reachable() {
    let long = "abcdefghij".repeat(30);
    let body: String = (0..40).map(|n| format!("{n} {long}\n")).collect();
    let mut app = open_file_view(&body);
    for _ in 0..40 {
        press(&mut app, &["down"]);
    }
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("39 abcdefghij"), "{rows:?}");
    assert_eq!(app.ui.file_edit.as_ref().unwrap().buf.text(), body);
}

#[test]
fn long_editor_lines_keep_the_end_reachable_and_editable() {
    let long = "abcdefghij".repeat(30);
    let mut app = open_file_view(&format!("{long}\nshort\n"));
    press(&mut app, &["end"]);
    typing(&mut app, "TAIL");
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("TAIL"), "{rows:?}");
    assert_eq!(
        app.ui.file_edit.as_ref().unwrap().buf.text(),
        format!("{long}TAIL\nshort\n")
    );
    press(&mut app, &["down", "home"]);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("short"), "{rows:?}");
}

#[test]
fn a_covered_timeline_shows_its_waiting_badge() {
    use nexus_tui::sync::SyncMsg;
    let mut app = waiting_app();
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "hello.txt", "kind": "file"}]})),
    });
    draw(&mut app, 120, 36);
    app.click(Target::SideRow(0));
    app.apply_sync(SyncMsg::Files {
        project,
        session: None,
        path: "hello.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": "hello\n", "revision": "rev1"})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    // The covered page's badge sits at the right end of the focused group's strip.
    let top = rows[app.regions.panel.unwrap().y as usize - 1].clone();
    assert!(top.contains("2 waiting"), "{top:?}");
}

#[test]
fn context_prompts_render_as_markdown_and_tools_list_their_descriptions() {
    use nexus_tui::app::App;
    use nexus_tui::sync::SyncMsg;
    use nexus_tui::ui::OverlayKind;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let history = nexus_tui::api::History {
        session_id: SESSION.into(),
        messages: app.messages().to_vec(),
        starting: Some(serde_json::json!({
            "origin": "created",
            "system_prompt": "# Rules\n\nUse **care**.",
            "tools": ["read", "bash"],
            "tool_definitions": {
                "read": {"description": "Read a file.", "input_schema": {"type": "object", "marker": "schema-marker"}},
                "bash": {"description": "Run a command."}
            },
            "agent": {"name": "build"},
            "instruction_file": {"path": "/work/AGENTS.md", "status": "loaded", "bytes": 4, "content": "text"},
            "workspace": {"entry": true, "active": false}
        })),
        ..Default::default()
    };
    app.apply_sync(SyncMsg::History(Box::new(history)));
    draw(&mut app, 120, 36);

    app.click(Target::ContextSection("System prompt".into()));
    let (_, rows) = draw(&mut app, 120, 36);
    let shown = joined(&rows);
    assert!(
        shown.contains("Use care."),
        "bold markers are rendered away: {shown}"
    );
    assert!(!shown.contains("**care**"), "{shown}");
    press(&mut app, &["esc"]);

    app.click(Target::ContextSection("Tools".into()));
    let (_, rows) = draw(&mut app, 120, 36);
    let shown = joined(&rows);
    assert!(
        shown.contains("Read a file.") && shown.contains("Run a command."),
        "{shown}"
    );
    assert!(
        !shown.contains("schema-marker"),
        "the schema waits to be opened: {shown}"
    );
    let row = app
        .hits
        .rect_of(&Target::ContextEntry(0))
        .expect("an entry row");
    click(&mut app, row.x + 1, row.y);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        joined(&rows).contains("schema-marker"),
        "opening shows the schema"
    );
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::ContextEntries { open: Some(_), .. })
    ));
    let _ = App::viewed_id;
}

#[test]
fn a_skill_entry_asks_core_for_its_full_text() {
    use nexus_tui::actions::ApiCall;
    use nexus_tui::ui::{ContextEntry, OverlayKind};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.open_overlay(OverlayKind::ContextEntries {
        title: "Skills".into(),
        entries: vec![ContextEntry {
            name: "design".into(),
            description: "Draws things.".into(),
            full: None,
            markdown: true,
            fetch: Some("skill"),
        }],
        cursor: 0,
        open: None,
        top: 0,
    });
    app.effects.clear();
    press(&mut app, &["enter"]);
    assert!(
        format!("{:?}", app.effects).contains("ContextEntry"),
        "{:?}",
        app.effects
    );
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("Loading"));
    let _ = std::mem::size_of::<ApiCall>();
}

#[test]
fn the_editor_has_a_header_a_gutter_and_a_find_replace_row() {
    let lines: String = (1..=40).map(|n| format!("line {n} needle\n")).collect();
    let mut app = open_file_view(&lines);
    let (buf, rows) = draw(&mut app, 120, 36);
    let r = app.regions.panel.unwrap();
    let head = &rows[r.y as usize];
    assert!(
        head.contains("hello.txt") && head.contains("editing"),
        "{head:?}"
    );
    let at = col_of(head, "hello.txt") as u16;
    assert!(buf[(at, r.y)]
        .modifier
        .contains(ratatui::style::Modifier::BOLD));
    let first = rows
        .iter()
        .position(|r| r.contains("line 1 needle"))
        .unwrap();
    assert!(rows[first].contains("1"));
    assert!(rows[first + 9].contains("10") && rows[first + 9].contains("line 10 needle"));
    assert!(!joined(&rows).contains("line 40 needle"));
    press(&mut app, &["ctrl+f"]);
    typing(&mut app, "needle");
    press(&mut app, &["enter", "enter", "tab"]);
    typing(&mut app, "replacement");
    let (buf, rows) = draw(&mut app, 120, 36);
    for n in [2, 3] {
        let y = rows
            .iter()
            .position(|r| r.contains(&format!("line {n} needle")))
            .unwrap();
        let x = col_of(&rows[y], "needle") as u16;
        assert_ne!(
            buf[(x, y as u16)].bg,
            buf[(x - 1, y as u16)].bg,
            "match {n} is highlighted"
        );
    }
    let all = joined(&rows);
    assert!(
        all.contains("needle") && all.contains("replacement"),
        "{all}"
    );
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(1));
    assert_eq!(
        app.ui.file_search.as_ref().unwrap().replacement,
        "replacement"
    );
}

#[test]
fn markdown_files_open_as_editable_source_with_an_explicit_preview() {
    let content = "# Title\n\nSome **bold** text.\n";
    let mut app = open_named_file_view("hello.md", content);
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("# Title") && all.contains("**bold**"), "{all}");
    assert!(all.contains("ctrl+shift+p preview"), "{all}");
    press(&mut app, &["ctrl+shift+p"]);
    assert!(app.file_preview());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Some bold text.") && !all.contains("# Title"),
        "{all}"
    );
    press(&mut app, &["ctrl+f"]);
    assert!(app.ui.file_source && app.ui.file_search.is_some());
    press(&mut app, &["esc"]);
    assert_eq!(app.ui.file_edit.as_ref().unwrap().buf.text(), content);
    typing(&mut app, "x");
    assert!(app.ui.file_edit.as_ref().unwrap().dirty());
}

/// `NEXUS_EDITOR_SHOTS=<dir> cargo test --test shell editor_parity_shots -- --ignored`.
#[test]
#[ignore]
fn editor_parity_shots() {
    let Ok(dir) = std::env::var("NEXUS_EDITOR_SHOTS") else {
        return;
    };
    std::fs::create_dir_all(&dir).unwrap();
    let content =
        "# Editor parity\n\nneedle Needle needle\nEnd inserts after the last character.\n";
    let mut app = open_named_file_view("hello.md", content);
    let write = |app: &mut nexus_tui::app::App, name: &str| {
        let (buf, rows) = draw(app, 120, 36);
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(
            format!("{dir}/{name}.svg"),
            nexus_tui::svg::to_svg(&buf, fg, bg),
        )
        .unwrap();
        std::fs::write(format!("{dir}/{name}.txt"), joined(&rows)).unwrap();
    };
    write(&mut app, "tui-editor-source");
    press(&mut app, &["ctrl+f"]);
    typing(&mut app, "needle");
    press(&mut app, &["alt+c", "enter", "enter", "tab"]);
    typing(&mut app, "thread");
    write(&mut app, "tui-editor-find");
    press(&mut app, &["ctrl+shift+r", "esc", "ctrl+z", "ctrl+z"]);
    assert_eq!(app.ui.file_edit.as_ref().unwrap().buf.text(), content);
    press(&mut app, &["ctrl+shift+p"]);
    assert!(app.file_preview());
    write(&mut app, "tui-editor-preview");
    if let Ok(shared_dir) = std::env::var("NEXUS_EDITOR_SHARED_SHOTS") {
        std::fs::create_dir_all(&shared_dir).unwrap();
        let mut shared = open_named_file_view("AGENTS.md", "# Title\n\nSome **bold** text.\n");
        for name in ["tui-source", "tui-find"] {
            if name == "tui-find" {
                press(&mut shared, &["ctrl+f"]);
                typing(&mut shared, "bold");
            }
            let (buf, rows) = draw(&mut shared, 120, 36);
            let (fg, bg) = nexus_tui::svg::page_colors(false);
            std::fs::write(
                format!("{shared_dir}/{name}.svg"),
                nexus_tui::svg::to_svg(&buf, fg, bg),
            )
            .unwrap();
            std::fs::write(format!("{shared_dir}/{name}.txt"), joined(&rows)).unwrap();
        }
    }
}

#[test]
fn the_wheel_scrolls_the_editor_and_a_click_places_the_cursor() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    let body: String = (1..=120).map(|n| format!("line {n:03}\n")).collect();
    let mut app = open_named_file_view("long.txt", &body);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("line 001"));
    let panel = app.regions.panel.expect("the file panel");
    let mouse = |kind, column, row| MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    };
    let (cx, cy) = (panel.x + panel.width / 2, panel.y + panel.height / 2);
    for _ in 0..10 {
        app.handle_mouse(mouse(MouseEventKind::ScrollDown, cx, cy));
        draw(&mut app, 120, 36);
    }
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("line 040"), "scrolled down: {all}");
    assert!(!all.contains("line 002"), "{all}");
    // A click in the text moves the cursor there, and typing lands at it.
    app.handle_mouse(mouse(MouseEventKind::Down(MouseButton::Left), cx, cy));
    app.handle_mouse(mouse(MouseEventKind::Up(MouseButton::Left), cx, cy));
    typing(&mut app, "!");
    let text = app.ui.file_edit.as_ref().unwrap().buf.text();
    assert!(
        text.contains('!') && !text.starts_with('!'),
        "typed at the click"
    );
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains('!'), "the view stays on the click: {all}");
}

#[test]
fn a_file_fills_its_group_below_the_strip() {
    let mut app = open_named_file_view("hello.md", "# Title\n");
    draw(&mut app, 160, 40);
    let panel = app.regions.panel.expect("the file panel");
    let group = app.ui.groups.rects[0].1;
    assert_eq!(panel.y, group.y + 1, "under the strip");
    assert_eq!(panel.bottom(), 40, "to the bottom of the window");
}

#[test]
fn the_markdown_preview_draws_mermaid_diagrams() {
    let content = "# Plan\n\n```mermaid\ngraph TD\n  A[Org] --> B[Team]\n```\n\nAfter.\n";
    let mut app = open_named_file_view("plan.md", content);
    press(&mut app, &["ctrl+shift+p"]);
    assert!(app.file_preview());
    let document = app.file_preview_rows(100);
    assert_eq!(document.images.len(), 1, "the diagram is an image");
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(!all.contains("graph TD"), "not the source: {all}");
    assert!(all.contains("After."), "{all}");
}

#[test]
fn markdown_preview_scrolls_to_the_end() {
    let body: String = (0..80)
        .map(|n| format!("Paragraph {n} has enough words to take a row of its own.\n\n"))
        .collect();
    let mut app = open_named_file_view("long.md", &body);
    let r = app.regions.panel.expect("the file panel");
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("Paragraph 0"), "{}", joined(&rows));
    for _ in 0..20 {
        press(&mut app, &["pagedown"]);
    }
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("Paragraph 79"), "the end is reachable: {all}");
    assert!(
        !all.contains("Paragraph 0 "),
        "the first paragraph scrolled away: {all}"
    );
    assert_eq!(r, app.regions.panel.unwrap());
}

#[test]
fn the_right_sidebar_starts_closed_at_every_width_and_still_opens() {
    for width in [100u16, 120, 160, 200] {
        let mut state = nexus_tui::state::AppState::new();
        state.apply(nexus_tui::sync::SyncMsg::Connection(
            nexus_tui::state::ConnectionState::Live,
        ));
        let mut app = nexus_tui::app::App::new(
            state,
            nexus_tui::theme::Theme::new(opts(Mode::Dark, Tier::TrueColor)),
            (width, 40),
        );
        draw(&mut app, width, 40);
        assert!(app.regions.right.is_none(), "{width}");
    }
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (110, 30));
    let (_, rows) = draw(&mut app, 110, 30);
    assert!(app.regions.right.is_none(), "closed by default");
    assert!(app.regions.left.is_some(), "the left stays docked");
    assert!(!app.regions.narrow);
    assert!(!joined(&rows).contains("SESSION"));
    press(&mut app, &["ctrl+l"]);
    let (_, rows) = draw(&mut app, 110, 30);
    assert!(app.regions.right.is_some(), "the toggle docks it");
    assert!(joined(&rows).contains("SESSION"));
    assert!(app.regions.centre.width >= nexus_tui::layout::MIN_CENTRE);
}

#[test]
fn a_narrow_drawer_sits_over_the_scrim_and_hit_tests_inside_and_out() {
    use nexus_tui::ui::Side;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (80, 24));
    let (plain, _) = draw(&mut app, 80, 24);
    let probe = (60, 12);
    let before = plain[probe].fg;
    press(&mut app, &["ctrl+b"]);
    let (buf, rows) = draw(&mut app, 80, 24);
    let (side, rect) = app.regions.drawer.expect("drawer");
    assert_eq!((side, rect.width), (Side::Left, 34));
    // the page behind is dimmed by the scrim; the header keeps its toggles
    assert_ne!(buf[probe].fg, before, "scrim over the centre");
    assert!(rows[0].contains("New session"), "{:?}", rows[0]);
    assert_eq!(buf[(1, 0)].fg, plain[(1, 0)].fg, "the header is not dimmed");
    // inside the drawer: its own targets; outside: the scrim, which closes it
    assert!(app
        .hits
        .at(rect.x + 3, rect.y + 3)
        .is_some_and(|t| t != &Target::Scrim));
    assert_eq!(
        app.hits.at(rect.right() + 2, rect.y + 3),
        Some(&Target::Scrim)
    );
    assert_eq!(
        app.hits
            .at(rect.right() - 1, rect.bottom() - 1)
            .map(|t| t != &Target::Scrim),
        Some(true)
    );
    click(&mut app, rect.right() + 2, rect.y + 3);
    assert!(
        app.ui.drawer.is_none(),
        "a click on the scrim closes the drawer"
    );
    // the right drawer is 36 cells and sits at the right edge
    // alt+b is word-back while the composer has focus, which a click leaves it with
    app.run_command("sidebar.right.toggle");
    draw(&mut app, 80, 24);
    let (side, rect) = app.regions.drawer.unwrap();
    assert_eq!((side, rect.width, rect.right()), (Side::Right, 36, 80));
    assert_eq!(app.hits.at(8, rect.y + 3), Some(&Target::Scrim));
}

#[test]
fn the_narrow_header_keeps_the_toggles_and_a_left_truncated_crumb() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (60, 24));
    let (_, rows) = draw(&mut app, 60, 24);
    let head = &rows[0];
    assert!(
        head.trim_start().starts_with('\u{25e7}') || head.trim_start().starts_with('\u{25a1}'),
        "{head:?}"
    );
    assert!(head.contains("New session"), "{head:?}");
    // no tab strip: no `+` new-session tab and no close crosses
    assert!(
        !head.contains('\u{d7}') && !head.contains(" + "),
        "{head:?}"
    );
    // a long title keeps its end and loses its start
    let id = app.viewed_id().unwrap().to_owned();
    app.state.model.sessions.get_mut(&id).unwrap().title =
        "An exceedingly long session title that cannot fit the header".into();
    let (_, rows) = draw(&mut app, 60, 24);
    let head = &rows[0];
    assert!(
        head.contains("\u{2026}") && head.contains("cannot fit the header"),
        "{head:?}"
    );
    assert!(!head.contains("An exceedingly"), "{head:?}");
    // the wide layout has the tab in the group's strip, not the header
    app.set_size(120, 36);
    let (_, rows) = draw(&mut app, 120, 36);
    let strip = app.ui.groups.rects[0].1.y as usize;
    assert!(rows[strip].contains('\u{d7}'), "{:?}", rows[strip]);
    assert!(!rows[0].contains('\u{d7}'), "{:?}", rows[0]);
}

#[test]
fn the_palette_groups_commands_under_uppercase_muted_headings() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    press(&mut app, &["ctrl+p"]);
    let (_, text) = draw(&mut app, 120, 36);
    assert!(text.iter().any(|r| r.contains("SESSION")), "{text:#?}");
    assert!(text.iter().any(|r| r.contains("Switch session")));
    // a filtered palette is a flat ranked list, no headings
    typing(&mut app, "tgl left");
    let (_, text) = draw(&mut app, 120, 36);
    assert!(!text.iter().any(|r| r.contains("MODEL AND AGENT")));
}

#[test]
fn session_search_toggles_project_scope_without_losing_query() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "other".into();
    other.project_id = "another-project".into();
    other.title = "Find this other session".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.run_command("session.switch");
    press(&mut app, &["f", "i", "n", "d"]);
    assert!(app.palette_items().is_empty());
    press(&mut app, &["tab"]);
    assert_eq!(app.palette_items()[0].label, "Find this other session");
    press(&mut app, &["tab"]);
    assert!(app.palette_items().is_empty());
}

#[test]
fn session_search_uses_the_tab_status_glyph_instead_of_state_words() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.run_command("session.switch");
    let (_, rows) = draw(&mut app, 120, 36);
    let y = app
        .hits
        .rect_of(&Target::PaletteItem(0))
        .expect("session row")
        .y as usize;
    assert!(rows[y].contains('\u{b7}'), "{:?}", rows[y]);
    assert!(!rows[y].contains("idle"), "{:?}", rows[y]);

    busy(&mut app);
    let (_, rows) = draw(&mut app, 120, 36);
    let y = app
        .hits
        .rect_of(&Target::PaletteItem(0))
        .expect("session row")
        .y as usize;
    assert!(rows[y].contains('\u{25d0}'), "{:?}", rows[y]);
    assert!(!rows[y].contains("running"), "{:?}", rows[y]);
}

#[test]
fn session_search_leads_each_row_with_its_state_glyph() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.run_command("session.switch");
    let (_, rows) = draw(&mut app, 120, 36);
    let y = app
        .hits
        .rect_of(&Target::PaletteItem(0))
        .expect("session row")
        .y as usize;
    let row = &rows[y];
    let glyph = row.find('\u{b7}').unwrap_or_else(|| panic!("{row:?}"));
    let title = row.find("New session").unwrap_or_else(|| panic!("{row:?}"));
    assert!(glyph < title, "the glyph leads the title: {row:?}");
}

#[test]
fn session_search_adds_core_message_hits_after_the_title_matches() {
    use nexus_tui::actions::{ActionDone, ApiCall, Done};
    use nexus_tui::app::Effect;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "other".into();
    other.title = "Unrelated title".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.run_command("session.switch");
    app.effects.clear();
    press(&mut app, &["l", "o", "g", "i", "n"]);
    let asked = app.effects.iter().rev().find_map(|e| match e {
        Effect::Api(ApiCall::Search { query, .. }) => Some(query.clone()),
        _ => None,
    });
    assert_eq!(asked.as_deref(), Some("login"));
    assert!(app.palette_items().is_empty());
    app.apply_done(ActionDone {
        call: ApiCall::Search {
            query: "login".into(),
            project: None,
        },
        result: Ok(Done {
            search: Some(serde_json::json!({"hits": [
                {"session_id": "other", "kind": "message", "snippet": "fix the [login] bug"}]})),
            ..Done::default()
        }),
    });
    let items = app.palette_items();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].label, "Unrelated title");
    assert!(items[0].hint.contains("login"), "{:?}", items[0].hint);
    // an answer to an older query is ignored
    press(&mut app, &["x"]);
    assert!(app.palette_items().is_empty());
}

#[test]
fn load_older_reads_the_page_before_the_oldest_message_and_prepends_it() {
    use nexus_tui::actions::{ActionDone, ApiCall, Done};
    use nexus_tui::app::Effect;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let view = app.state.model.selected.as_mut().unwrap();
    view.messages_more = true;
    let first = view.messages[0].clone();
    let loaded = view.messages.len();
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("older messages are not loaded"));
    app.effects.clear();
    app.run_command("turn.load_older");
    app.run_command("turn.load_older"); // a second press while one is in flight does nothing
    let asked: Vec<_> = app
        .effects
        .iter()
        .filter_map(|e| match e {
            Effect::Api(ApiCall::OlderMessages { session, before }) => {
                Some((session.clone(), *before))
            }
            _ => None,
        })
        .collect();
    assert_eq!(asked, vec![(SESSION.to_owned(), first.seq)]);
    let mut older = first.clone();
    older.id = "older_1".into();
    older.seq = first.seq - 1;
    app.apply_done(ActionDone {
        call: ApiCall::OlderMessages {
            session: SESSION.into(),
            before: first.seq,
        },
        result: Ok(Done {
            older: Some((vec![older], false)),
            ..Done::default()
        }),
    });
    let view = app.state.model.selected.as_ref().unwrap();
    assert_eq!(view.messages.len(), loaded + 1);
    assert_eq!(view.messages[0].id, "older_1");
    assert!(!view.messages_more);
}

#[test]
fn a_model_switch_that_loses_the_cache_asks_first() {
    use nexus_tui::actions::{ActionDone, ApiCall, Done};
    use nexus_tui::app::Effect;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.models = vec![serde_json::json!({"ref": "scripted/other", "name": "other-model"})];
    app.run_command("model.change");
    for c in "other".chars() {
        press(&mut app, &[&c.to_string()]);
    }
    app.effects.clear();
    press(&mut app, &["enter"]);
    let asked = app.effects.iter().find_map(|e| match e {
        Effect::Api(call @ ApiCall::SwitchCost { .. }) => Some(call.clone()),
        _ => None,
    });
    let asked = asked.expect("the switch asks Core what it costs first");
    assert!(!app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::SetModel { .. }))));
    app.effects.clear();
    let cost = serde_json::json!({"reasons": ["a different model"], "cached_tokens": 1234, "cache_lost": true});
    app.apply_done(ActionDone {
        call: asked.clone(),
        result: Ok(Done {
            switch_cost: Some(cost),
            ..Done::default()
        }),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        joined(&rows).contains("1234 cached tokens"),
        "{}",
        joined(&rows)
    );
    // Cancel is the focused choice: Enter keeps the model.
    press(&mut app, &["enter"]);
    assert!(!app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::SetModel { .. }))));
    // Asked again and confirmed, the switch goes out.
    app.apply_done(ActionDone {
        call: asked.clone(),
        result: Ok(Done {
            switch_cost: Some(serde_json::json!({"reasons": ["a different model"], "cached_tokens": 5, "cache_lost": true})),
            ..Done::default()
        }),
    });
    press(&mut app, &["right", "enter"]);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::SetModel { .. }))));
    // Nothing cached: no question.
    app.effects.clear();
    app.apply_done(ActionDone {
        call: asked,
        result: Ok(Done {
            switch_cost: Some(
                serde_json::json!({"reasons": [], "cached_tokens": 0, "cache_lost": false}),
            ),
            ..Done::default()
        }),
    });
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::SetModel { .. }))));
}

#[test]
fn bottom_rule_shows_context_and_activity() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    with_usage(&mut app, 82_000);
    let (buf, _) = draw(&mut app, 160, 48);
    let r = app.regions.composer;
    let y = r.bottom() - 1;
    let accent = app.theme.agent_style("build").fg.unwrap();
    let active = (r.x + 2..r.right() - 2)
        .filter(|&x| buf[(x, y)].fg == accent)
        .count();
    let width = (r.width - 4) as usize;
    assert!((active as f64 / width as f64 - 0.41).abs() < 0.02);
    busy(&mut app);
    let (buf, _) = draw(&mut app, 160, 48);
    // The pulse has soft edges: cells fading to the rule colour around its core.
    let rule = app.theme.fg("border.default").fg.unwrap();
    let lit = (r.x + 2..r.right() - 2)
        .filter(|&x| buf[(x, y)].fg != rule)
        .count();
    assert!(lit > 0 && lit <= width / 5 + 4, "{lit}");
}

#[test]
fn clicking_the_timeline_focuses_the_composer_unless_a_modal_is_open() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    use nexus_tui::ui::Focus;
    let click = |app: &mut nexus_tui::app::App, x: u16, y: u16| {
        app.handle_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    };
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    let t = app.regions.timeline;
    app.run_command("timeline.focus");
    assert_eq!(app.ui.focus, Focus::Timeline);
    click(&mut app, t.x + 10, t.y + 3);
    assert_eq!(app.ui.focus, Focus::Composer);

    app.run_command("palette.open");
    app.ui.focus = Focus::Timeline;
    click(&mut app, t.x + 10, t.y + 3);
    assert_ne!(app.ui.focus, Focus::Composer, "a modal keeps its own focus");
}

#[test]
fn slash_sessions_opens_the_session_switcher() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.composer().set_text("/sessions");
    press(&mut app, &["enter"]);
    assert!(app.ui.overlay.is_some(), "the switcher is open");
    assert!(app.composer_ref().text().is_empty());
}

#[test]
fn pressing_the_open_project_in_the_rail_toggles_the_sidebar() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project_id = app.current_project().unwrap();
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "other-project".into();
    other.project_id = "another-project".into();
    other.title = "Elsewhere".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.ui.left_open = false;
    draw(&mut app, 120, 36);
    let rail = app.hits.rect_of(&Target::RailProject(0)).unwrap();
    click(&mut app, rail.x + 2, rail.y + 1);
    assert!(app.ui.left_open);
    assert!(app.ui.overlay.is_none());
    assert!(!app.ui.scope_all);
    draw(&mut app, 120, 36);
    click(&mut app, rail.x + 2, rail.y + 1);
    assert!(!app.ui.left_open);
    assert_eq!(app.current_project().as_deref(), Some(project_id.as_str()));
    assert_eq!(app.viewed_id(), Some(SESSION));
}

#[test]
fn project_rail_labels_activity_and_session_restoration() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project_id = app.current_project().unwrap();
    let mut project = app.state.model.projects[&project_id].clone();
    app.state.model.projects.get_mut(&project_id).unwrap().name = "Nexus".into();
    project.id = "second".into();
    project.name = "Notes".into();
    project.created += 1.0;
    app.state.model.projects.insert(project.id.clone(), project);
    app.ui.left_open = false;
    let (buf, _) = draw(&mut app, 120, 36);
    let first = app.hits.rect_of(&Target::RailProject(0)).unwrap();
    let second = app.hits.rect_of(&Target::RailProject(1)).unwrap();
    assert_eq!(first.y, 2, "one empty row follows the header");
    let label = |y: u16| -> String {
        (first.x + 1..first.right() - 1)
            .map(|x| buf[(x, y)].symbol().to_owned())
            .collect::<String>()
            .trim()
            .to_owned()
    };
    assert_eq!(label(first.y + 1), "N");
    assert_eq!(label(second.y + 1), "NT");
    // The two-letter label "NT" is centred, so the middle cell holds its second letter.
    assert_eq!(
        buf[(second.x + second.width / 2, second.y + 1)].symbol(),
        "T"
    );
    let x = (second.x..second.right())
        .find(|x| buf[(*x, second.y + 1)].symbol() == "N")
        .unwrap();
    assert_eq!(
        buf[(x, second.y + 1)].fg,
        app.theme.color("text.muted").unwrap()
    );

    click(&mut app, second.x + 2, second.y + 1);
    assert_eq!(app.current_project().as_deref(), Some("second"));
    assert_eq!(app.viewed_id(), None);
    app.composer().set_text("Notes draft");
    draw(&mut app, 120, 36);
    click(&mut app, first.x + 2, first.y + 1);
    assert_eq!(app.viewed_id(), Some(SESSION));
    app.run_command("session.new");
    assert!(app.composer_ref().text().is_empty());
    app.composer().set_text("Nexus draft");

    draw(&mut app, 120, 36);
    click(&mut app, second.x + 2, second.y + 1);
    assert_eq!(app.current_project().as_deref(), Some("second"));
    assert_eq!(app.viewed_id(), None);
    assert_eq!(app.composer_ref().text(), "Notes draft");
    draw(&mut app, 120, 36);
    click(&mut app, first.x + 2, first.y + 1);
    assert_eq!(app.viewed_id(), None);
    assert_eq!(app.composer_ref().text(), "Nexus draft");

    let mut session = app.state.model.sessions[SESSION].clone();
    session.id = "recent".into();
    session.project_id = "second".into();
    session.updated = nexus_tui::app::now_secs() - 1800.0;
    app.state.model.sessions.insert(session.id.clone(), session);
    app.ui.focus = nexus_tui::ui::Focus::Composer;
    app.ui.project = Some(project_id);
    let (buf, _) = draw(&mut app, 120, 36);
    assert_eq!(
        buf[(x, second.y + 1)].fg,
        app.theme.color("text.secondary").unwrap()
    );
    app.state.model.sessions.get_mut("recent").unwrap().updated -= 3600.0;
    let (buf, _) = draw(&mut app, 120, 36);
    assert_eq!(
        buf[(x, second.y + 1)].fg,
        app.theme.color("text.muted").unwrap()
    );
}

#[test]
fn project_rail_counters_have_colors_overflow_and_hover_details() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    let template = app.state.model.sessions[SESSION].clone();
    app.state.model.sessions.clear();
    app.state.model.running.clear();
    app.state.model.approvals.clear();
    app.state.model.questions.clear();
    app.ui.unread_sessions.clear();
    for (kind, count) in [
        ("attention", 10),
        ("finished", 2),
        ("running", 3),
        ("idle", 4),
    ] {
        for n in 0..count {
            let mut session = template.clone();
            session.id = format!("{kind}-{n}");
            session.parent_id = None;
            session.archived = false;
            match kind {
                "attention" => {
                    let approval = serde_json::from_value(
                        serde_json::json!({"id": session.id, "session_id": session.id}),
                    )
                    .unwrap();
                    app.state
                        .model
                        .approvals
                        .insert(session.id.clone(), approval);
                }
                "finished" => {
                    app.ui.unread_sessions.insert(session.id.clone());
                }
                "running" => {
                    app.state
                        .model
                        .running
                        .insert(session.id.clone(), "turn".into());
                }
                _ => {}
            }
            app.state.model.sessions.insert(session.id.clone(), session);
        }
    }
    app.ui.left_open = false;
    let (buf, _) = draw(&mut app, 120, 50);
    let band = app.hits.rect_of(&Target::RailProject(0)).unwrap();
    // One counter per corner: attention and unread on top, running and idle below.
    let row = |y: u16| -> String {
        (band.x..band.right())
            .map(|x| buf[(x, y)].symbol())
            .collect()
    };
    let (left, right) = (band.x + 1, band.right() - 2);
    assert_eq!(right - left, 2, "one cell separates the counters");
    assert_eq!(buf[(left, band.y)].symbol(), "*", "{:?}", row(band.y));
    assert_eq!(buf[(right, band.y)].symbol(), "2", "{:?}", row(band.y));
    assert_eq!(
        buf[(left, band.y + 2)].symbol(),
        "3",
        "{:?}",
        row(band.y + 2)
    );
    assert_eq!(
        buf[(right, band.y + 2)].symbol(),
        "4",
        "{:?}",
        row(band.y + 2)
    );
    let cell = |y: u16, symbol: &str| -> u16 {
        (band.x..band.right())
            .find(|x| buf[(*x, y)].symbol() == symbol)
            .unwrap()
    };
    for (y, symbol, token) in [
        (band.y, "*", "interactive.multi"),
        (band.y, "2", "status.info"),
        (band.y + 2, "3", "status.success"),
        (band.y + 2, "4", "text.primary"),
    ] {
        assert_eq!(
            buf[(cell(y, symbol), y)].fg,
            app.theme.color(token).unwrap()
        );
    }
    app.mouse = Some((band.x + 2, band.y + 1));
    let (buf, _) = draw(&mut app, 120, 50);
    let screen: String = buf.content.iter().map(|cell| cell.symbol()).collect();
    for label in ["10 attention", "2 unread", "3 running", "4 idle"] {
        assert!(screen.contains(label), "missing {label}");
    }
    assert!(!screen.contains("means 10"));
    let center = band.x + band.width.saturating_sub(1) / 2;
    assert_eq!(
        buf[(center, band.y + 1)].fg,
        app.theme.color("text.primary").unwrap()
    );
}

#[test]
fn project_rail_counts_keep_their_corners_when_others_are_zero() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 50));
    let template = app.state.model.sessions[SESSION].clone();
    app.state.model.sessions.clear();
    app.state.model.running.clear();
    app.state.model.approvals.clear();
    app.state.model.questions.clear();
    app.ui.unread_sessions.clear();
    for (kind, count) in [("finished", 2), ("running", 1), ("idle", 6)] {
        for n in 0..count {
            let mut session = template.clone();
            session.id = format!("{kind}-{n}");
            session.parent_id = None;
            session.archived = false;
            match kind {
                "finished" => {
                    app.ui.unread_sessions.insert(session.id.clone());
                }
                "running" => {
                    app.state
                        .model
                        .running
                        .insert(session.id.clone(), "turn".into());
                }
                _ => {}
            }
            app.state.model.sessions.insert(session.id.clone(), session);
        }
    }
    app.ui.left_open = false;
    let (buf, _) = draw(&mut app, 120, 50);
    let band = app.hits.rect_of(&Target::RailProject(0)).unwrap();
    // Unread keeps the top right corner though attention is zero, and running
    // and idle keep theirs.
    let (left, right) = (band.x + 1, band.right() - 2);
    assert_eq!(buf[(left, band.y)].symbol(), " ");
    assert_eq!(buf[(right, band.y)].symbol(), "2");
    assert_eq!(buf[(left, band.y + 2)].symbol(), "1");
    assert_eq!(buf[(right, band.y + 2)].symbol(), "6");
}
#[test]
fn the_rail_popup_sits_beside_the_rail_with_the_sidebar_open_or_closed() {
    for open in [true, false] {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
        app.ui.left_open = open;
        draw(&mut app, 120, 36);
        let band = app.hits.rect_of(&Target::RailProject(0)).unwrap();
        app.mouse = Some((band.x + 2, band.y + 1));
        let (_, rows) = draw(&mut app, 120, 36);
        let row = rows
            .iter()
            .find(|row| row.contains("attention"))
            .unwrap_or_else(|| panic!("open {open}: {rows:#?}"));
        // The rail's five cells, then the popup's padding and the count.
        assert_eq!(col_of(row, "attention"), 9, "open {open}: {row:?}");
    }
}

#[test]
fn focusing_the_sidebar_opens_no_rail_popup() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.ui.focus = nexus_tui::ui::Focus::Sidebar;
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(!joined(&rows).contains("needs attention"));
}

#[test]
fn project_rail_draws_a_three_cell_rule_under_a_project_without_counts() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project_id = app.current_project().unwrap();
    let mut empty = app.state.model.projects[&project_id].clone();
    empty.id = "empty".into();
    empty.name = "Empty".into();
    empty.created += 1.0;
    app.state.model.projects.insert(empty.id.clone(), empty);
    app.ui.left_open = false;
    let (buf, _) = draw(&mut app, 120, 36);
    let band = app.hits.rect_of(&Target::RailProject(1)).unwrap();
    let row: String = (band.x..band.right())
        .map(|x| buf[(x, band.y + 2)].symbol())
        .collect();
    assert!(row.contains("───"), "{row:?}");
    assert_eq!(row.matches('─').count(), 3, "{row:?}");
    let x = (band.x..band.right())
        .find(|x| buf[(*x, band.y + 2)].symbol() == "─")
        .unwrap();
    for offset in 0..3 {
        assert_eq!(
            buf[(x + offset, band.y + 2)].fg,
            app.theme.color("border.default").unwrap()
        );
    }
}

#[test]
fn file_operations_prompt_and_confirm_before_mutation() {
    use nexus_tui::actions::{ActionDone, Done};
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "folder", "kind": "directory"}]})),
    });
    app.execute("files.new_file");
    typing(&mut app, "new.txt");
    press(&mut app, &["enter"]);
    assert!(app.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::FileMutation { action, path, .. }) if action == "create_file" && path == "new.txt")));
    app.effects.clear();
    app.execute("files.delete");
    let call = app
        .effects
        .iter()
        .find_map(|e| match e {
            Effect::Api(c @ ApiCall::FileCount { .. }) => Some(c.clone()),
            _ => None,
        })
        .unwrap();
    app.effects.clear();
    app.apply_done(ActionDone {
        call,
        result: Ok(Done {
            file_count: Some(2),
            ..Done::default()
        }),
    });
    assert!(app
        .ui
        .dialog
        .as_ref()
        .unwrap()
        .body
        .iter()
        .any(|line| line.contains("2 items")));
    press(&mut app, &["right", "enter"]);
    assert!(app.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::FileMutation { action, path, .. }) if action == "delete" && path == "folder")));
}

#[test]
fn disk_file_reads_follow_clean_edits_and_preserve_dirty_edits() {
    use nexus_tui::sync::SyncMsg;
    let scenario: serde_json::Value =
        serde_json::from_str(include_str!("../../interface/scenarios/file-watch.json")).unwrap();
    let mut app = open_file_view(scenario["initial"].as_str().unwrap());
    let project = app.current_project().unwrap();
    let read = |content: &str, revision: &str| SyncMsg::Files {
        project: project.clone(),
        session: None,
        path: "hello.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": content, "revision": revision})),
    };
    app.apply_sync(read(scenario["updated"].as_str().unwrap(), "rev-clean"));
    assert_eq!(
        app.ui.file_edit.as_ref().unwrap().buf.text(),
        scenario["updated"].as_str().unwrap()
    );
    typing(&mut app, scenario["draft"].as_str().unwrap());
    let draft = app.ui.file_edit.as_ref().unwrap().buf.text();
    app.apply_sync(read("later disk text\n", "rev-later"));
    let edit = app.ui.file_edit.as_ref().unwrap();
    assert_eq!(edit.buf.text(), draft);
    assert!(edit.stale && edit.dirty());
}

#[test]
fn files_watch_is_leased_only_while_its_surface_is_visible() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.execute("sidebar.tab.files");
    app.effects.clear();
    app.tick(app.clock);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::FileWatch { release: false, .. }))));
    app.effects.clear();
    app.execute("sidebar.tab.sessions");
    app.tick(app.clock);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::FileWatch { release: true, .. }))));
    app.file_watch_status = "polling".into();
    app.execute("sidebar.tab.files");
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("Polling every 3 seconds"));
}

fn git_tab_app(changes: serde_json::Value) -> nexus_tui::app::App {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.ui.left_open = true;
    app.ui.tab = nexus_tui::ui::Tab::Git;
    app.state.project_git = Some((
        project,
        Ok(serde_json::json!({
            "worktree": "/work/nexus-plan-40", "branch": "feature",
            "changes": changes,
            "branches": [{"name": "one", "current": true}, {"name": "two", "current": false}],
        })),
    ));
    app
}

#[test]
fn git_tab_without_changes_says_so_and_has_no_branch_list() {
    let mut app = git_tab_app(serde_json::json!([]));
    let (_, lines) = draw(&mut app, 120, 36);
    let text = lines.join("\n");
    assert!(text.contains("nexus-plan-40 · feature"), "{text}");
    assert!(text.contains("No changes"));
    assert!(!text.contains("Branches") && !text.contains("two"));
}

#[test]
fn git_tab_groups_by_top_level_folder_and_expands_on_enter() {
    let change = |path: &str, status: &str, staged: bool, unstaged: bool, a: u64, r: u64| {
        serde_json::json!({"path": path, "status": status, "staged": staged,
            "unstaged": unstaged, "added": a, "removed": r})
    };
    let mut app = git_tab_app(serde_json::json!([
        change("plans/a.md", "M ", true, false, 10, 2),
        change("src/x.rs", "MM", true, true, 5, 1),
        change("src/y.rs", " M", false, true, 1, 0),
        change("README.md", "??", false, true, 0, 0),
    ]));
    let (_, lines) = draw(&mut app, 120, 36);
    let text = lines.join("\n");
    assert!(text.contains("4 files changed · +16 −3"), "{text}");
    assert!(text.contains("2 staged · 2 unstaged"), "{text}");
    assert!(
        text.contains("▸ src/  2") && text.contains("▸ plans/  1"),
        "{text}"
    );
    assert!(!text.contains("x.rs"), "folders start collapsed");
    app.ui.focus = nexus_tui::ui::Focus::Sidebar;
    app.ui.cursor.insert(nexus_tui::ui::Tab::Git, 2);
    press(&mut app, &["enter"]);
    let (_, lines) = draw(&mut app, 120, 36);
    let text = lines.join("\n");
    assert!(
        text.contains("▾ src/  2") && text.contains("src/x.rs") && text.contains("src/y.rs"),
        "{text}"
    );
    press(&mut app, &["enter"]);
    let (_, lines) = draw(&mut app, 120, 36);
    assert!(
        !lines.join("\n").contains("src/x.rs"),
        "Enter on the folder collapses it"
    );
}
