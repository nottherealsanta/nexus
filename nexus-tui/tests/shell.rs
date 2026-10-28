//! Shell buffer tests: Ratatui `TestBackend` frames at the three review sizes,
//! built from the real/documented fixtures.

mod common;
use common::*;
use nexus_tui::manifest::Manifest;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Target;
use ratatui::style::Color;

const SIZES: [(u16, u16); 3] = [(80, 24), (120, 36), (160, 48)];

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
                head.starts_with(" \u{25e7}") || head.starts_with(" \u{25a1}"),
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
                // tab icons in manifest order at the top of the sidebar,
                // settings pinned at the bottom
                let tabs: Vec<u16> = m
                    .navigation
                    .left_sidebar
                    .tab
                    .iter()
                    .map(|t| {
                        let tab = nexus_tui::ui::Tab::from_id(&t.id).unwrap();
                        let r = app.hits.rect_of(&Target::Tab(tab)).expect("tab icon");
                        assert_eq!(r.y, 1, "icons sit on the sidebar's first row");
                        r.x
                    })
                    .collect();
                assert!(tabs.windows(2).all(|p| p[0] < p[1]), "{tabs:?}");
                assert!(
                    rows[h as usize - 1].contains("Settings"),
                    "{:?}",
                    rows[h as usize - 1]
                );
                // right sidebar: session info and the Git status reason from the manifest
                assert!(all.contains("Session info") && all.contains("Git status"));
                assert!(all.contains("Loading Git status") || all.contains("Open a session"));
            } else {
                assert!(!all.contains("Session info"), "narrow shows one pane");
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
    assert_eq!(buf[(29, 32)].bg, Color::Rgb(0x1e, 0x1e, 0x1e));
    assert!(
        buf.content
            .iter()
            .any(|cell| cell.bg == Color::Rgb(0x24, 0x24, 0x24)),
        "user block uses raised surface"
    );
    assert_eq!(buf[(27, 10)].symbol(), "│");
    assert_eq!(buf[(27, 10)].fg, Color::Rgb(0x2a, 0x2a, 0x2a));
    assert_eq!(buf[(92, 10)].symbol(), "│");
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
    assert!(rows[0].starts_with(" [|"), "{:?}", rows[0]);
}

#[test]
fn narrow_layout_uses_drawers_and_keeps_the_anchors() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (80, 24));
    draw(&mut app, 80, 24);
    press(&mut app, &["ctrl+b"]);
    let (_, rows) = draw(&mut app, 80, 24);
    // the drawer carries the tab icons
    use nexus_tui::ui::Tab;
    for tab in [Tab::Sessions, Tab::Files, Tab::Projects] {
        assert!(app.hits.rect_of(&Target::Tab(tab)).is_some(), "{tab:?}");
    }
    assert!(
        rows[0].starts_with(" \u{25e7}"),
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
    assert!(joined(&rows).contains("Session info"));
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
    let projects = app
        .hits
        .rect_of(&Target::Tab(nexus_tui::ui::Tab::Projects))
        .unwrap();
    click(&mut app, projects.x, projects.y);
    assert_eq!(app.ui.tab, nexus_tui::ui::Tab::Projects);
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
    assert!(joined(&rows).contains("Pasted ~"));
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
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(
        all.contains("Disconnected from Core") && all.contains("reconnecting in 4 s"),
        "{all}"
    );
    assert!(all.contains("+1"), "stacked banners count");
    assert!(rows[0].contains("reconnecting"));
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
        all.contains("Incompatible Core") && all.contains("update nexus-tui"),
        "{all}"
    );
    let _ = ApiVersion { major: 1, minor: 0 };
    // blocking: Esc does nothing, nothing else reacts
    press(&mut app, &["esc", "ctrl+p"]);
    assert!(app.ui.overlay.is_none());
    assert!(joined(&draw(&mut app, 120, 36).1).contains("Incompatible Core"));
}

#[test]
fn waiting_card_sits_above_the_composer_and_hides_unoffered_choices() {
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
        .position(|r| r.contains("Type a message"))
        .unwrap();
    assert!(wait_row < composer_row);
    let all = joined(&rows);
    assert!(
        all.contains("allow once")
            && all.contains("allow session")
            && all.contains("deny with note")
    );
    assert!(all.contains("not offered by Core"));
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

#[test]
fn composer_controls_and_stop_have_hit_targets() {
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
    busy(&mut app);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("esc stop"));
    assert!(app.hits.rect_of(&Target::Stop).is_some());
}

#[test]
fn composer_uses_latest_usage_and_catalog_window() {
    use nexus_tui::api::Event;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/example".into());
    app.state.models = vec![
        serde_json::json!({"ref": "codex/example", "name": "Example Model", "context": 200000}),
    ];
    let events = &mut app.state.model.selected.as_mut().unwrap().events;
    for (id, input, output) in [(100, 1000, 100), (101, 10000, 1000)] {
        events.push(Event {
            id,
            cursor: None,
            seq: None,
            time: id as f64,
            kind: "usage".into(),
            session_id: Some(SESSION.into()),
            data: serde_json::json!({"input_tokens": input, "output_tokens": output}),
        });
    }
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("Example Model codex"), "{all}");
    assert!(all.contains("10.0k · 5.0%"), "{all}");
    app.state.models.clear();
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(
        all.contains("example codex") && all.contains("10.0k"),
        "{all}"
    );
    assert!(!all.contains("5.0%"));
}

#[test]
fn composer_capitalises_the_agent_and_shows_price_tier_occupancy() {
    use nexus_tui::api::Event;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let session = app.state.model.sessions.get_mut(SESSION).unwrap();
    session.agent = "orchestrate".into();
    session.model = Some("codex/example".into());
    app.state.models = vec![serde_json::json!({
        "ref": "codex/example", "name": "Example Model",
        "context": 1_000_000, "tiers": [200_000]
    })];
    app.state
        .model
        .selected
        .as_mut()
        .unwrap()
        .events
        .push(Event {
            id: 300,
            cursor: None,
            seq: None,
            time: 1.0,
            kind: "usage".into(),
            session_id: Some(SESSION.into()),
            data: serde_json::json!({"input_tokens": 52000}),
        });
    let (_, rows) = draw(&mut app, 120, 36);
    let row = rows
        .iter()
        .find(|row| row.contains("Example Model codex"))
        .expect("composer control row");
    assert!(row.contains("Orchestrate"), "{row}");
    assert!(!row.contains("orchestrate"), "{row}");
    // Used, the next price tier and the window, as old Nexus showed them.
    assert!(all_contains(&rows, "52.0k · 26.0% · 5.2%"), "{row}");

    let context = app
        .hits
        .rect_of(&Target::ComposerContext)
        .expect("context numbers");
    app.mouse = Some((context.x + 1, context.y));
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("Context: 52.0k of 1.0M tokens"), "{all}");
    assert!(
        all.contains("26.0% at 200.0k") && all.contains("5.2% of window"),
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
    app.state.model.selected.as_mut().unwrap().events.extend([
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
    // while the agent and a context percentage stay.
    let (_, rows) = draw(&mut app, 60, 24);
    let all = joined(&rows);
    assert!(all.contains("Build"), "{all}");
    assert!(all.contains('%'), "{all}");
    assert!(!all.contains("default"), "{all}");
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
    assert!(all.contains("x \u{2325} project-branch"), "{all}");
    assert!(all.contains("project-branch  worktree"), "{all}");
}

#[test]
fn files_tree_expands_with_git_marks_and_hides_ignored() {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: Some(SESSION.into()),
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
    let row = |name: &str| {
        rows.iter()
            .find(|row| row.contains(name))
            .unwrap()
            .split('\u{2502}')
            .next()
            .unwrap()
    };
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
        session: Some(SESSION.into()),
        path: "src".into(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "b.rs", "kind": "file"}]})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("\u{25be} src"), "{all}");
    let child = rows.iter().find(|row| row.contains("b.rs")).unwrap();
    assert!(child.starts_with("   "), "child is indented: {child:?}");

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
        session: Some(SESSION.into()),
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
    assert!(all.contains("ws \u{b7} feature"), "{all}");
    assert!(app.hits.rect_of(&Target::FilesRoot).is_some());
    app.effects.clear();
    app.execute("files.root_toggle");
    assert!(app.ui.files_project_root);
    assert!(
        app.effects.iter().any(|effect| matches!(
            effect,
            Effect::Sync(Command::Files {
                session: None,
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
        !all.contains("worktree"),
        "main checkout is not labelled: {all}"
    );
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
    assert!(joined(&rows).contains("feature  worktree"));
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
    assert!(!joined(&rows).contains("feature  worktree"));
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
        .position(|line| line.contains("x ⌥ main"))
        .unwrap();
    assert!(rows[row - 1].contains("New session"));
    assert_eq!(app.hits.at(4, row as u16), Some(&Target::SideRow(0)));
    assert_eq!(app.hits.at(4, row as u16 - 1), Some(&Target::SideRow(0)));
}

#[test]
fn files_list_open_read_and_return_to_timeline() {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: Some(SESSION.into()),
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
        session: Some(SESSION.into()),
        path: "hello.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": "the file contents\n"})),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("the file contents"));
    press(&mut app, &["esc"]);
    assert!(!app.ui.file_open);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("Hello"));
}

/// Opens `hello.txt` in the file view with `content` loaded.
fn open_file_view(content: &str) -> nexus_tui::app::App {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let project = app.current_project().unwrap();
    app.execute("sidebar.tab.files");
    app.apply_sync(SyncMsg::Files {
        project: project.clone(),
        session: Some(SESSION.into()),
        path: String::new(),
        file: false,
        data: Ok(serde_json::json!({"entries": [{"name": "hello.txt", "kind": "file"}]})),
    });
    draw(&mut app, 120, 36);
    app.click(Target::SideRow(0));
    app.apply_sync(SyncMsg::Files {
        project,
        session: Some(SESSION.into()),
        path: "hello.txt".into(),
        file: true,
        data: Ok(serde_json::json!({"content": content})),
    });
    draw(&mut app, 120, 36);
    app
}

#[test]
fn file_view_search_filters_jumps_and_ends_before_closing() {
    let mut app = open_file_view("alpha\nbeta needle\ngamma\nNeedle again\n");
    press(&mut app, &["/"]);
    typing(&mut app, "needle");
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("/needle  2 matches"), "{rows:?}");

    press(&mut app, &["enter"]);
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(0));
    press(&mut app, &["n"]);
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(1));
    press(&mut app, &["n"]);
    assert_eq!(app.ui.file_search.as_ref().unwrap().hit, Some(0), "wraps");
    press(&mut app, &["N"]);
    assert_eq!(
        app.ui.file_search.as_ref().unwrap().hit,
        Some(1),
        "wraps back"
    );

    // Typing again edits the query, so n is a letter.
    press(&mut app, &["/"]);
    typing(&mut app, "n");
    assert_eq!(app.ui.file_search.as_ref().unwrap().query, "needlen");

    // The first Esc ends the search and keeps the file open.
    press(&mut app, &["esc"]);
    assert!(app.ui.file_search.is_none());
    assert!(app.ui.file_open);
    press(&mut app, &["esc"]);
    assert!(!app.ui.file_open);
}

#[test]
fn file_view_highlights_matches_and_announces_the_line_range() {
    let lines: String = (1..=100).map(|n| format!("line {n}\n")).collect();
    let mut app = open_file_view(&format!("{lines}target here\n"));
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("lines 1-"), "{all}");
    assert!(all.contains("of 101 · scroll"), "{all}");

    press(&mut app, &["pagedown"]);
    let scroll = app.ui.file_scroll;
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        joined(&rows).contains(&format!("lines {}-", scroll + 1)),
        "{rows:?}"
    );

    press(&mut app, &["/"]);
    typing(&mut app, "TARGET");
    press(&mut app, &["enter"]);
    let (buf, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("  1 matches"), "{rows:?}");
    let y = rows
        .iter()
        .position(|row| row.contains("target here"))
        .unwrap() as u16;
    let x = col_of(&rows[y as usize], "target") as u16;
    let highlighted = buf.cell((x, y)).unwrap().bg;
    let plain = buf.cell((x + 20, y)).unwrap().bg;
    assert_ne!(highlighted, plain, "matches use interactive.selection");
}

#[test]
fn git_sidebar_shows_status_from_the_selected_session() {
    use nexus_tui::sync::SyncMsg;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.apply_sync(SyncMsg::Git {
        session: SESSION.into(),
        data: Ok(serde_json::json!({
            "branch": "main", "ahead": 2, "behind": 1,
            "changes": [{"path": "example.rs", "added": 3, "removed": 2,
                         "staged": false, "unstaged": true, "status": " M"}]
        })),
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let text = joined(&rows);
    assert!(text.contains("main  ↑2 ↓1"));
    assert!(text.contains("example.rs +3 -2"));
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
    let (_, rows) = draw(&mut app, 120, 36);
    let top = rows
        .iter()
        .position(|r| r.contains("Settings") && r.contains("esc"))
        .unwrap();
    let close_x = col_of(&rows[top], "esc") as u16;
    assert_eq!(
        app.hits.at(close_x, top as u16),
        Some(&Target::SettingsEntry)
    );
    assert!(rows[top + 2].contains("Search"));
    assert!(joined(&rows).contains("Appearance"));
    app.click(Target::SettingsEntry);
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
    assert!(text.contains("Models"));
    assert!(!text.contains("Dictation"));
    press(&mut app, &["esc"]);
    assert!(app.ui.settings_search.text().is_empty());
    assert_eq!(app.ui.view, nexus_tui::ui::View::Settings);
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
    let error = serde_json::json!({"id": 201, "type": "session.error", "session_id": "other", "cursor": "log_c915bfc9b6d826c4:201", "data": {"error": "failed"}});
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
fn long_session_titles_fade_in_truecolour_and_use_ellipsis_in_256() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.state.model.sessions.get_mut(SESSION).unwrap().title =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".into();
    let (buf, rows) = draw(&mut app, 120, 36);
    let row = rows
        .iter()
        .position(|row| {
            row.chars()
                .take(27)
                .collect::<String>()
                .contains("ABCDEFGHIJKLMNOP")
        })
        .unwrap();
    let start = col_of(&rows[row], "ABCDEFGHIJKLMNOP") as u16;
    let visible = rows[row]
        .chars()
        .skip(start as usize)
        .take(23)
        .take_while(|c| c.is_ascii_uppercase())
        .count() as u16;
    assert!(visible >= 4);
    let last = buf[(start + visible - 1, row as u16)].fg;
    let earlier = buf[(start + visible - 4, row as u16)].fg;
    assert_ne!(last, earlier, "last cells fade toward the surface");
    let mut fallback = with_turn(opts(Mode::Dark, Tier::Ansi256), (120, 36));
    fallback
        .state
        .model
        .sessions
        .get_mut(SESSION)
        .unwrap()
        .title = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".into();
    let (_, rows) = draw(&mut fallback, 120, 36);
    assert!(rows
        .iter()
        .any(|row| row.contains("ABCDEFGHIJKLMNOP") && row.contains('…')));
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
        entries[app.cursor(Tab::Sessions)].heading.as_deref(),
        Some("feature")
    );
    assert!(entries
        .iter()
        .any(|entry| entry.id == SESSION && entry.heading.is_some()));
}

#[test]
fn settings_arrows_change_animations_monochrome_and_sidebar() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.execute("app.settings");
    press(&mut app, &["a", "n", "i", "m"]);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("Animations"));
    let before = app.theme.opts.reduced_motion;
    press(&mut app, &["right"]);
    assert_ne!(app.theme.opts.reduced_motion, before);
    press(&mut app, &["esc"]);
    press(&mut app, &["m", "o", "n", "o"]);
    draw(&mut app, 120, 36);
    app.click(Target::SettingsRow(0));
    assert_eq!(app.theme.opts.tier, Tier::Mono);
    press(&mut app, &["left"]);
    assert_eq!(app.theme.opts.tier, Tier::TrueColor);
    press(&mut app, &["esc"]);
    press(&mut app, &["s", "i", "d", "e", "b", "a", "r"]);
    press(&mut app, &["right"]);
    assert!(app.ui.left_open && app.ui.left_rail);
    press(&mut app, &["right"]);
    assert!(!app.ui.left_open);
    press(&mut app, &["right"]);
    assert!(app.ui.left_open && !app.ui.left_rail);
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
        .find(|line| line.contains("x ⌥ main"))
        .expect("parent row");
    assert!(row.contains("2 ↳ 3h"), "{row:?}");
    // Child sessions are not listed, so only the parent row has a meta.
    assert!(!rows.iter().any(|l| l.contains("1 ↳")), "{rows:#?}");

    // A long place is truncated and the meta survives.
    app.state.model.projects.values_mut().for_each(|p| {
        p.name = "a-very-long-project-name-that-cannot-fit".into();
    });
    let (_, rows) = draw(&mut app, 120, 36);
    let row = rows
        .iter()
        .find(|line| line.trim_start().starts_with("a-very-long"))
        .expect("long row");
    assert!(row.contains('…'), "{row:?}");
    assert!(row.contains("2 ↳ 3h"), "{row:?}");
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

fn git_fixture(app: &mut nexus_tui::app::App) {
    use nexus_tui::sync::SyncMsg;
    app.apply_sync(SyncMsg::Git {
        session: SESSION.into(),
        data: Ok(serde_json::json!({
            "branch": "main", "ahead": 0, "behind": 0,
            "changes": [
                {"path": "both.rs", "added": 2, "removed": 1,
                 "staged": true, "unstaged": true, "status": "MM", "old_path": null},
                {"path": "idx.rs", "added": 4, "removed": 0,
                 "staged": true, "unstaged": false, "status": "A ", "old_path": null},
                {"path": "new.txt", "added": 0, "removed": 0,
                 "staged": false, "unstaged": true, "status": "??", "old_path": null}
            ]
        })),
    });
}

#[test]
fn git_status_splits_staged_unstaged_and_untracked() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    git_fixture(&mut app);
    let rows: Vec<_> = app
        .git_rows()
        .iter()
        .map(|r| (r.group.label(), r.path.clone()))
        .collect();
    assert_eq!(
        rows,
        [
            ("Staged", "both.rs".to_owned()),
            ("Staged", "idx.rs".to_owned()),
            ("Unstaged", "both.rs".to_owned()),
            ("Untracked", "new.txt".to_owned()),
        ]
    );
    let (_, lines) = draw(&mut app, 120, 36);
    let text = joined(&lines);
    for heading in ["Staged 2", "Unstaged 1", "Untracked 1"] {
        assert!(text.contains(heading), "{heading}: {text}");
    }
    assert!(text.contains("both.rs +2 -1"));
    assert!(text.contains("new.txt"));
    let row = lines.iter().position(|l| l.contains("idx.rs")).unwrap();
    let x = col_of(&lines[row], "idx.rs") as u16;
    assert_eq!(app.hits.at(x, row as u16), Some(&Target::GitRow(1)));
}

#[test]
fn clicking_a_changed_file_opens_its_diff_and_esc_returns() {
    use nexus_tui::app::Effect;
    use nexus_tui::sync::{Command, SyncMsg};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    git_fixture(&mut app);
    draw(&mut app, 120, 36);
    app.effects.clear();
    app.click(Target::GitRow(2));
    assert!(app.ui.diff_open.is_some());
    assert!(app.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::GitDiff { session, path }) if session == SESSION && path == "both.rs"
    )));
    let (_, lines) = draw(&mut app, 120, 36);
    assert!(joined(&lines).contains("Loading diff"));
    app.apply_sync(SyncMsg::Diff {
        session: SESSION.into(),
        path: "both.rs".into(),
        data: Ok(serde_json::json!({
            "path": "both.rs",
            "staged": "+staged only\n",
            "unstaged": "diff --git a/both.rs b/both.rs\n@@ -1,2 +7,2 @@\n keep\n-old line\n+new line\n",
        })),
    });
    let (buf, lines) = draw(&mut app, 120, 36);
    let text = joined(&lines);
    assert!(text.contains("both.rs") && text.contains("Unstaged"));
    assert!(text.contains("new line") && text.contains("old line"));
    assert!(!text.contains("staged only"));
    let add = lines.iter().position(|l| l.contains("new line")).unwrap();
    let del = lines.iter().position(|l| l.contains("old line")).unwrap();
    assert!(lines[add].contains("   8  +new line"), "{}", lines[add]);
    let x = col_of(&lines[add], "+new line") as u16;
    let add_bg = buf[(x, add as u16)].bg;
    let del_bg = buf[(x, del as u16)].bg;
    assert_ne!(add_bg, del_bg);
    assert_ne!(add_bg, Color::Reset);
    press(&mut app, &["esc"]);
    assert!(app.ui.diff_open.is_none());
    let (_, lines) = draw(&mut app, 120, 36);
    assert!(joined(&lines).contains("Hello"));
}

#[test]
fn enter_in_the_right_sidebar_opens_the_selected_diff() {
    use nexus_tui::app::{Effect, GitGroup};
    use nexus_tui::sync::Command;
    use nexus_tui::ui::Focus;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    git_fixture(&mut app);
    draw(&mut app, 120, 36);
    app.ui.focus = Focus::Right;
    app.effects.clear();
    press(&mut app, &["down", "enter"]);
    assert_eq!(
        app.ui.diff_open,
        Some(("idx.rs".to_owned(), GitGroup::Staged))
    );
    assert!(app.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::GitDiff { path, .. }) if path == "idx.rs"
    )));
    // Untracked files have no Core diff: no request, a notice instead.
    press(&mut app, &["esc"]);
    app.ui.focus = Focus::Right;
    app.effects.clear();
    press(&mut app, &["end", "enter"]);
    assert!(app.effects.is_empty());
    let (_, lines) = draw(&mut app, 120, 36);
    assert!(joined(&lines).contains("Untracked file"));
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

const DB: &str = r#"{"id":"db","question":"Which database?","options":[{"id":"pg","label":"PostgreSQL"},{"id":"lite","label":"SQLite"}],"multiple":false,"allow_free_text":false}"#;

#[test]
fn question_card_shows_options_and_a_number_key_answers() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(
        all.contains("Which database?")
            && all.contains("1 PostgreSQL")
            && all.contains("2 SQLite")
            && all.contains("submit answers"),
        "{all}"
    );
    assert!(!all.contains("arrives with"), "{all}");
    press(&mut app, &["alt+w", "2"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([{"question_id": "db", "selected": ["lite"], "text": null}])]
    );
    // a second press while the reply is in flight sends nothing more
    press(&mut app, &["1", "enter"]);
    assert_eq!(answers_sent(&app).len(), 1);
}

#[test]
fn arrows_and_enter_pick_and_clicks_hit_options() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    press(&mut app, &["alt+w", "down", "down", "enter"]);
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
fn free_text_goes_through_the_composer_and_several_questions_send_once() {
    let notes = r#"{"id":"notes","question":"Anything else?"}"#;
    let multi = r#"{"id":"tags","question":"Which tags?","options":[{"id":"a","label":"alpha"},{"id":"b","label":"beta"}],"multiple":true,"allow_free_text":false}"#;
    let mut app = asking(&format!("[{DB},{multi},{notes}]"), (120, 36));
    press(&mut app, &["alt+w", "1"]);
    // multi-select: tick two, enter confirms
    press(&mut app, &["1", "2", "enter"]);
    assert!(answers_sent(&app).is_empty());
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Anything else?") && all.contains("question 3 of 3"),
        "{all}"
    );
    // typing from the card lands in the composer; Enter there answers
    typing(&mut app, "keep it small");
    assert_eq!(app.ui.focus, nexus_tui::ui::Focus::Composer);
    press(&mut app, &["enter"]);
    assert_eq!(
        answers_sent(&app),
        vec![serde_json::json!([
            {"question_id": "db", "selected": ["pg"], "text": null},
            {"question_id": "tags", "selected": ["a", "b"], "text": null},
            {"question_id": "notes", "selected": [], "text": "keep it small"}
        ])]
    );
    assert!(app.composer_ref().expanded_text().is_empty());
}

#[test]
fn typed_text_for_an_option_only_question_is_refused_and_kept() {
    let mut app = asking(&format!("[{DB}]"), (120, 36));
    press(&mut app, &["alt+w"]);
    app.ui.focus = nexus_tui::ui::Focus::Composer;
    typing(&mut app, "postgres please");
    press(&mut app, &["enter"]);
    assert!(answers_sent(&app).is_empty());
    assert_eq!(app.composer_ref().expanded_text(), "postgres please");
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
fn files_and_projects_tabs_expand_the_rail() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.ui.left_rail = true;
    app.execute("sidebar.tab.files");
    assert!(!app.ui.left_rail);
    app.ui.left_rail = true;
    app.execute("sidebar.tab.sessions");
    assert!(app.ui.left_rail);
}

#[test]
fn tab_icons_show_their_label_on_hover() {
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
    assert!(
        rows[r.y as usize + 1].contains("Files"),
        "{:?}",
        rows[r.y as usize + 1]
    );
}
