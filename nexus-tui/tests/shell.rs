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
                // tab icons in manifest order in the header band over the
                // sidebar, settings pinned at the bottom
                let tabs: Vec<u16> = m
                    .navigation
                    .left_sidebar
                    .tab
                    .iter()
                    .map(|t| {
                        let tab = nexus_tui::ui::Tab::from_id(&t.id).unwrap();
                        let r = app.hits.rect_of(&Target::Tab(tab)).expect("tab icon");
                        assert_eq!(r.y, 0, "icons sit in the header band");
                        assert!(r.right() <= app.regions.left.unwrap().right());
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
                assert!(all.contains("SESSION") && all.contains("GIT"));
                assert!(all.contains("Loading Git status") || all.contains("Open a session"));
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
    for y in [composer.y + 2, composer.bottom() - 3] {
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
    assert_eq!(
        (top.symbol(), top.fg, top.bg),
        ("\u{2584}", panel, Color::Rgb(0x0b, 0x0b, 0x0b))
    );
    let bottom = &buf[(composer.x + 6, composer.bottom() - 2)];
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
    assert_eq!(buf[(27, 10)].bg, sidebar);
    // surfaces collapse in ANSI-16 and mono: the edge is drawn there
    for tier in [Tier::Ansi16, Tier::Mono] {
        let mut low = with_turn(opts(Mode::Dark, tier), (120, 36));
        let (buf, _) = draw(&mut low, 120, 36);
        assert_eq!(buf[(27, 10)].symbol(), "│", "{tier:?}");
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
        all.contains("Allow once")
            && all.contains("Allow for session")
            && all.contains("Deny with a note")
    );
    // a choice Core does not offer shows its reason (D14)
    assert!(all.contains("not offered by Core yet"), "{all}");
    assert!(all.contains("Core does not support notes yet"), "{all}");
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
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
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
    let (_, rows) = draw(&mut app, 160, 48);
    let all = joined(&rows);
    assert!(all.contains("Example Model codex"), "{all}");
    assert!(all.contains("10.0K \u{b7} 5%"), "{all}");
    app.state.models.clear();
    let (_, rows) = draw(&mut app, 160, 48);
    let all = joined(&rows);
    assert!(
        all.contains("example codex") && all.contains("10.0K"),
        "{all}"
    );
    assert!(!all.contains("5%"), "no window, no share: {all}");
}

#[test]
fn composer_capitalises_the_agent_and_shows_price_tier_occupancy() {
    use nexus_tui::api::Event;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
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
    let (_, rows) = draw(&mut app, 160, 48);
    let row = rows
        .iter()
        .find(|row| row.contains("Example Model codex"))
        .expect("composer control row");
    assert!(row.contains("Orchestrate"), "{row}");
    assert!(!row.contains("orchestrate"), "{row}");
    // Used and its share of the window; the palette hint is on the project strip.
    assert!(all_contains(&rows, "52.0K \u{b7} 5%"), "{row}");
    assert!(all_contains(&rows, "ctrl+p commands"));

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
    // while the agent and `esc stop` stay (busy swaps the meter for the stop hint).
    let (_, rows) = draw(&mut app, 60, 24);
    let all = joined(&rows);
    assert!(all.contains("Build"), "{all}");
    assert!(all.contains("esc stop"), "{all}");
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
    assert!(all.contains("build \u{b7} \u{2325} project-"), "{all}");
    assert!(
        all.contains("/work/nexus-home/ws  \u{2325} project-branch  worktree"),
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
    let row = |name: &str| rows.iter().find(|row| row.contains(name)).unwrap();
    let row = |name: &str| row(name).chars().take(28).collect::<String>();
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
    assert!(joined(&rows).contains("/work/nexus-home/ws  \u{2325} feature  worktree"));
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
    assert!(!joined(&rows).contains("/work/nexus-home/ws  \u{2325} feature  worktree"));
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
        .position(|line| line.contains("build · ⌥ main"))
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
    assert!(text.contains("⌥ main  ↑2  ↓1"), "{text}");
    let row = rows.iter().find(|l| l.contains("example.rs")).unwrap();
    assert!(
        row.contains("+3") && row.trim_end().ends_with("−2"),
        "{row:?}"
    );
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
    assert!(rows[top + 2].contains("Filter settings"));
    assert!(joined(&rows).contains("APPEARANCE"));
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
    assert!(text.contains("MODELS"));
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
        .any(|entry| entry.id == SESSION && entry.project.is_some() && entry.tree.is_none()));
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
        .position(|line| line.contains("build · ⌥ main"))
        .expect("parent row");
    let side: String = rows[row].chars().take(28).collect();
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
        .find(|line| line.contains("build · ⌥ feature"))
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
    // D12: the group label with its count right-aligned on the same row.
    for (heading, count) in [("Staged", "2"), ("Unstaged", "1"), ("Untracked", "1")] {
        assert!(
            lines.iter().any(|l| {
                let l = l.trim_end();
                l.contains(heading) && l.ends_with(&format!(" {count}"))
            }),
            "{heading} {count}: {text}"
        );
    }
    let both = lines.iter().find(|l| l.contains("both.rs")).unwrap();
    assert!(both.contains("+2") && both.contains("−1"), "{both:?}");
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
    assert!(lines[add].contains(" 8 + new line"), "{}", lines[add]);
    let x = col_of(&lines[add], "new line") as u16;
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
    // unfocused, the card says how to answer; focused, it lists its keys
    assert!(joined(&draw(&mut app, 120, 36).1).contains("alt+w answer"));
    press(&mut app, &["alt+w"]);
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(
        all.contains("Which database?")
            && all.contains("1 PostgreSQL")
            && all.contains("2 SQLite")
            && all.contains("enter submit"),
        "{all}"
    );
    assert!(!all.contains("arrives with"), "{all}");
    press(&mut app, &["2"]);
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
    use nexus_tui::api::Event;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    app.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/example".into());
    app.state.models = vec![serde_json::json!({
        "ref": "codex/example", "name": "Example", "context": 200000,
        "input_cost": 1.0, "output_cost": 10.0, "cache_read_cost": 0.2
    })];
    let events = &mut app.state.model.selected.as_mut().unwrap().events;
    for (id, input, cached, output) in [(100, 10_000, 0, 1000), (101, 50_000, 20_000, 2000)] {
        events.push(Event {
            id,
            cursor: None,
            seq: None,
            time: id as f64,
            kind: "usage".into(),
            session_id: Some(SESSION.into()),
            data: serde_json::json!({
                "input_tokens": input, "cached_input_tokens": cached, "output_tokens": output
            }),
        });
    }
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
    use nexus_tui::ui::Tab;
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.ui.tab = Tab::Projects;
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("+ Add project"));
    let add = app.hits.rect_of(&Target::AddProject).expect("add row");
    app.effects.clear();
    click(&mut app, add.x + 2, add.y);
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
    app.state.default_model = "codex/gpt-5".into();
    app.state.catalog_source = "cache (5 min old)".into();
    app.state.models = vec![serde_json::json!({"ref": "codex/gpt-5"})];
    app.state.agents = vec![
        serde_json::json!({"name": "build"}),
        serde_json::json!({"name": "plan"}),
    ];
    app.execute("app.settings");
    let (_, rows) = draw(&mut app, 120, 50);
    let all = joined(&rows);
    for needle in [
        "codex/gpt-5",
        "1 models",
        "cache (5 min old)",
        "build, plan",
    ] {
        assert!(all.contains(needle), "missing {needle:?}: {all}");
    }
    typing(&mut app, "default model");
    press(&mut app, &["enter"]);
    let toast = app
        .ui
        .alerts
        .history
        .first()
        .map(|n| n.text.clone())
        .unwrap_or_default();
    assert!(toast.contains("no default setting"), "{toast}");
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
fn tabs_replace_the_title_open_close_and_switch() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "second".into();
    other.title = "Second task".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        rows[0].contains("New session"),
        "the open session has a tab: {}",
        rows[0]
    );
    assert!(rows[0].contains('+'), "{}", rows[0]);
    app.open_session("second");
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(
        rows[0].contains("New session") && rows[0].contains("Second task"),
        "{}",
        rows[0]
    );
    // Clicking the other tab switches to it.
    let first = app.hits.rect_of(&Target::SessionTab(0)).expect("first tab");
    click(&mut app, first.x + 2, first.y);
    assert_eq!(app.viewed_id(), Some(SESSION));
    // Closing a tab keeps its session and moves to the neighbour.
    let close = app
        .hits
        .rect_of(&Target::SessionTabClose(0))
        .expect("close");
    draw(&mut app, 120, 36);
    click(&mut app, close.x, close.y);
    assert_eq!(app.viewed_id(), Some("second"));
    assert!(app.state.model.sessions.contains_key(SESSION));
    assert_eq!(app.tab_ids(), vec!["second".to_owned()]);
    // Keyboard: close the last tab and the new-session screen shows.
    press(&mut app, &["alt+x"]);
    assert!(app.viewed_id().is_none());
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(rows[0].contains("New session"), "{}", rows[0]);
}

#[test]
fn header_tabs_follow_the_chrome_band_styles() {
    // D02: the viewed tab is bold primary on the page surface, joined to the
    // page; other tabs are muted on the band; `+` ends the strip.
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    let mut other = app.state.model.sessions[SESSION].clone();
    other.id = "other".into();
    other.title = "Other session".into();
    app.state.model.sessions.insert(other.id.clone(), other);
    app.ui.tabs = vec![SESSION.to_owned(), "other".to_owned()];
    let (buf, rows) = draw(&mut app, 160, 40);
    let band = Color::Rgb(0x14, 0x14, 0x14);
    let page = Color::Rgb(0x0b, 0x0b, 0x0b);
    let at = |needle: &str| col_of(&rows[0], needle) as u16;
    let viewed = &buf[(at(app.state.model.sessions[SESSION].title.as_str()), 0)];
    assert_eq!(viewed.bg, page, "the viewed tab sits on the page surface");
    assert!(viewed.modifier.contains(ratatui::style::Modifier::BOLD));
    let others = &buf[(at("Other session"), 0)];
    assert_eq!(others.bg, band, "other tabs sit on the band");
    assert!(!others.modifier.contains(ratatui::style::Modifier::BOLD));
    assert_ne!(viewed.fg, others.fg, "viewed is primary, others muted");
    assert!(
        rows[0].contains(" + ") || rows[0].contains('+'),
        "{:?}",
        rows[0]
    );
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
    let mut prev = toggle.right();
    for tab in [
        nexus_tui::ui::Tab::Sessions,
        nexus_tui::ui::Tab::Files,
        nexus_tui::ui::Tab::Projects,
    ] {
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
fn a_viewed_child_session_shows_root_and_child_crumbs() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
    let mut parent = app.state.model.sessions[SESSION].clone();
    parent.id = "ses_parent".into();
    parent.title = "Root task".into();
    parent.parent_id = None;
    app.state.model.sessions.insert(parent.id.clone(), parent);
    {
        let child = app.state.model.sessions.get_mut(SESSION).unwrap();
        child.parent_id = Some("ses_parent".into());
        child.title = "Child task".into();
    }
    let (_, rows) = draw(&mut app, 160, 40);
    assert!(
        rows[0].contains("Root task \u{203a} Child task"),
        "{:?}",
        rows[0]
    );
    // the earlier crumb is clickable and opens the root
    let crumb = app.hits.rect_of(&Target::SessionCrumb(0)).expect("crumb");
    assert_eq!(crumb.y, 0);
    assert_eq!(app.hits.at(crumb.x, 0), Some(&Target::SessionCrumb(0)));
    app.click(Target::SessionCrumb(0));
    assert_eq!(app.viewed_id(), Some("ses_parent"));
}

// ---- the composer card (DESIGN_PLAN.md D11) ---------------------------------

/// The cells of one buffer row as text.
fn row_text(buf: &ratatui::buffer::Buffer, y: u16) -> String {
    (0..buf.area.width).map(|x| buf[(x, y)].symbol()).collect()
}

fn with_usage(app: &mut nexus_tui::app::App, input: u64) {
    use nexus_tui::api::Event;
    app.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/example".into());
    app.state.models = vec![
        serde_json::json!({"ref": "codex/example", "name": "Example Model", "context": 200000}),
    ];
    app.state
        .model
        .selected
        .as_mut()
        .unwrap()
        .events
        .push(Event {
            id: 900,
            cursor: None,
            seq: None,
            time: 1.0,
            kind: "usage".into(),
            session_id: Some(SESSION.into()),
            data: serde_json::json!({"input_tokens": input}),
        });
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
        // Live row, then the half-row edges at the card's width (inset 2).
        assert!(card(r.y).trim().is_empty(), "{w}: idle live row is blank");
        let top = &buf[(r.x + 2, r.y + 1)];
        assert_eq!(top.symbol(), "\u{2584}", "{w}");
        assert_eq!(buf[(r.x + 1, r.y + 1)].symbol(), " ", "{w}: inset");
        assert_eq!(buf[(r.right() - 3, r.y + 1)].symbol(), "\u{2584}", "{w}");
        assert_eq!(buf[(r.right() - 2, r.y + 1)].symbol(), " ", "{w}");
        // Input at x+3 inside the card, a blank row, the control row.
        let input = card(r.y + 2);
        assert!(input[input.char_indices().nth(x + 3).unwrap().0..].starts_with("Type a message"));
        assert!(card(r.y + 3).trim().is_empty(), "{w}: blank row");
        let control = card(r.bottom() - 3);
        assert!(control
            .chars()
            .skip(x + 3)
            .collect::<String>()
            .starts_with("Build"));
        assert_eq!(buf[(r.x + 2, r.bottom() - 2)].symbol(), "\u{2580}", "{w}");
        // Project strip on the canvas, flush with the bottom.
        assert!(card(r.bottom() - 1).contains("ctrl+p commands"), "{w}");
        assert_eq!(
            buf[(r.x + 6, r.bottom() - 1)].bg,
            Color::Rgb(0x0b, 0x0b, 0x0b)
        );
        assert_eq!(r.bottom(), h, "{w}: the composer ends at the bottom");
    }
}

/// Which of effort, provider, meter and `41%` the control row shows.
fn control_state(rows: &[String], row: usize) -> (bool, bool, bool, bool) {
    let line = &rows[row];
    (
        line.contains("default"),
        line.contains("Example Model codex"),
        line.contains('\u{2571}'),
        line.contains('%'),
    )
}

#[test]
fn narrow_control_rows_shed_effort_then_provider_then_meter() {
    let mut seen = std::collections::BTreeSet::new();
    for w in (50..=200).rev().step_by(2) {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (w, 36));
        with_usage(&mut app, 82_000);
        let (_, rows) = draw(&mut app, w, 36);
        let cy = app.regions.composer.bottom() as usize - 3;
        let (effort, provider, meter, figure) = control_state(&rows, cy);
        assert!(figure, "{w}: the context figure stays: {:?}", rows[cy]);
        assert!(
            meter || !provider,
            "{w}: the meter goes after the provider: {:?}",
            rows[cy]
        );
        assert!(
            !effort || provider,
            "{w}: effort before provider: {:?}",
            rows[cy]
        );
        assert!(rows[cy].contains("Build"), "{w}: {:?}", rows[cy]);
        seen.insert((effort, provider, meter));
    }
    for state in [
        (true, true, true),
        (false, true, true),
        (false, false, true),
        (false, false, false),
    ] {
        assert!(seen.contains(&state), "never shown: {state:?} in {seen:?}");
    }
}

#[test]
fn the_meter_has_ten_cells_filled_in_the_agent_colour() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (200, 50));
    with_usage(&mut app, 82_000); // 41% of 200k
    let (buf, rows) = draw(&mut app, 200, 50);
    let cy = app.regions.composer.bottom() - 3;
    let cells: Vec<u16> = (0..buf.area.width)
        .filter(|&x| buf[(x, cy)].symbol() == "\u{2571}")
        .collect();
    assert_eq!(cells.len(), 10, "{:?}", rows[cy as usize]);
    assert!(cells.windows(2).all(|p| p[1] == p[0] + 1), "contiguous");
    let agent = app.theme.agent_style("build").fg;
    let filled = cells
        .iter()
        .filter(|&&x| buf[(x, cy)].fg == agent.unwrap())
        .count();
    assert_eq!(filled, 5, "ceil(10 * 0.41)");
    let empty = app.theme.fg("border.default").fg.unwrap();
    assert!(cells[5..].iter().all(|&x| buf[(x, cy)].fg == empty));
    assert!(
        rows[cy as usize].contains("82.0K \u{b7} 41%"),
        "{:?}",
        rows[cy as usize]
    );
}

#[test]
fn mono_and_ascii_cards_use_rules_and_only_they_keep_the_focus_bar() {
    for tier in [Tier::Mono, Tier::Ansi16] {
        let mut app = with_turn(opts(Mode::Dark, tier), (120, 36));
        let (buf, _) = draw(&mut app, 120, 36);
        let r = app.regions.composer;
        let focused = app.ui.focus == nexus_tui::ui::Focus::Composer;
        let rule = app.theme.structure("rule");
        let (top, bottom) = (&buf[(r.x + 6, r.y + 1)], &buf[(r.x + 6, r.bottom() - 2)]);
        if tier == Tier::Mono {
            assert_eq!((top.symbol(), bottom.symbol()), (rule, rule));
        }
        let bar = buf[(r.x + 2, r.y + 2)].symbol() == app.theme.structure("focus_bar");
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
    assert_eq!(
        buf[(r.x + 6, r.y + 1)].symbol(),
        app.theme.structure("rule")
    );
    assert!(buf[(r.x + 6, r.y + 1)].symbol().is_ascii());
}

#[test]
fn busy_shows_the_working_row_and_esc_stop_not_the_meter() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    with_usage(&mut app, 82_000);
    busy(&mut app);
    let (buf, rows) = draw(&mut app, 160, 48);
    let r = app.regions.composer;
    let live = &rows[r.y as usize];
    assert!(live.contains("\u{25d0} Working"), "{live:?}");
    let control = &rows[r.bottom() as usize - 3];
    assert!(control.trim_end().ends_with("esc stop"), "{control:?}");
    assert!(!control.contains('\u{2571}'), "{control:?}");
    assert!(!rows[r.bottom() as usize - 1].contains("ctrl+p commands"));
    // No old busy marker inside the card.
    assert!(!rows[r.y as usize + 2].contains("Working"));
    let _ = buf;
}

#[test]
fn the_strip_marks_a_linked_worktree_in_bold_violet() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 48));
    app.state.worktrees.insert(
        SESSION.into(),
        serde_json::json!({
            "session_directory": "/work/nexus-home/ws",
            "worktrees": [{"path": "/work/nexus-home/ws", "branch": "feature", "main": false}]
        }),
    );
    let (buf, rows) = draw(&mut app, 160, 48);
    let y = app.regions.composer.bottom() - 1;
    let x = rows[y as usize].find("worktree").unwrap() as u16;
    let cell = &buf[(x, y)];
    assert_eq!(cell.fg, app.theme.fg("agent.violet").fg.unwrap());
    assert!(cell.modifier.contains(ratatui::style::Modifier::BOLD));
    assert!(!rows[y as usize].contains("checkout"));
}

#[test]
fn completion_is_a_borderless_block_as_wide_as_the_card_with_key_hints() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    typing(&mut app, "/");
    let (buf, rows) = draw(&mut app, 120, 36);
    let r = app.regions.composer;
    let raised = Color::Rgb(0x24, 0x24, 0x24);
    let hint = rows
        .iter()
        .position(|row| row.contains("move") && row.contains("tab complete"))
        .expect("key hints row");
    assert!(rows[hint].contains("esc close"), "{:?}", rows[hint]);
    assert_eq!(
        hint as u16 + 1,
        r.y + 1,
        "the hint row is last, on the card's top edge"
    );
    // As wide as the card, no border glyphs, flush with its edge.
    assert_eq!(buf[(r.x + 2, hint as u16)].bg, raised);
    assert_eq!(buf[(r.right() - 3, hint as u16)].bg, raised);
    assert_ne!(buf[(r.x + 1, hint as u16)].bg, raised);
    assert_ne!(buf[(r.right() - 2, hint as u16)].bg, raised);
    assert!(rows[hint]
        .chars()
        .all(|c| !"\u{2502}\u{250c}\u{2514}\u{256d}".contains(c)));
}

/// The left sidebar's text, cut at its width (the centre column starts after it).
fn sidebar(rows: &[String], width: usize) -> Vec<String> {
    rows.iter()
        .map(|r| r.chars().take(width).collect::<String>())
        .collect()
}

#[test]
fn session_rows_follow_d03_at_sidebar_widths_22_28_and_34() {
    for w in [22u16, 28, 34] {
        let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 40));
        app.ui.left_width = w;
        app.state.model.sessions.get_mut(SESSION).unwrap().title = "Refactor the loader".into();
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
        // search row, blank, project header, title line, detail line, spacer, new
        let key = col_of(&text[first], "ctrl+k");
        assert_eq!(
            text[first].find('⌕').map(|_| col_of(&text[first], "⌕")),
            Some(2)
        );
        assert!(
            key + 6 < w as usize,
            "{w}: key hint inside: {:?}",
            text[first]
        );
        assert!(text[first + 1].trim().is_empty(), "{w}");
        assert!(text[first + 2].trim_end().ends_with('X') && text[first + 2].starts_with("  X"));
        let title = &text[first + 3];
        let detail = &text[first + 4];
        assert!(text[first + 5].trim().is_empty(), "spacer {w}");
        assert!(text[first + 6].contains("+ New session"), "{w}");
        // mark slot at x=2, title at x=4, time right-aligned with a margin of 1.
        assert!(title.starts_with("  ●"), "{w}: unread mark: {title:?}");
        assert_eq!(col_of(title, "Refactor"), 4, "{w}");
        assert!(
            title.trim_end().ends_with('h') || title.trim_end().ends_with("now"),
            "{title:?}"
        );
        assert!(
            nexus_tui::text::width(title.trim_end()) < w as usize - 1,
            "{w}: margin"
        );
        // line 2: the agent, then the branch; the child count right-aligned.
        assert_eq!(col_of(detail, "build"), 4, "{w}");
        assert!(detail.trim_end().ends_with("1 ↳"), "{w}: {detail:?}");
        assert!(
            nexus_tui::text::width(detail.trim_end()) < w as usize - 1,
            "{w}"
        );
        if w >= 28 {
            assert!(detail.contains("⌥ main"), "{w}: {detail:?}");
        }
        // The open session carries the selection band on both lines.
        let band = buf[(w / 2, (first + 3) as u16)].bg;
        assert_eq!(band, buf[(w / 2, (first + 4) as u16)].bg, "{w}");
        assert_ne!(
            band,
            buf[(w / 2, (first + 2) as u16)].bg,
            "{w}: band vs header"
        );
        // Settings is pinned to the bottom row.
        assert!(
            text.last().unwrap().trim_start().starts_with("Settings"),
            "{w}"
        );
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
    // and the row draws that mark in the slot at x=2
    let (_, rows) = draw(&mut app, 120, 36);
    let row = sidebar(&rows, 27)
        .into_iter()
        .find(|l| l.contains("New session") && l.contains('◆'))
        .expect("waiting mark on the title row");
    assert_eq!(col_of(&row, "◆"), 2);
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
    for label in ["SESSION", "CONTEXT", "GIT"] {
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
fn the_rail_marks_the_open_session_with_the_selection_band() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.ui.left_width = 10;
    app.ui.left_rail = true;
    let (buf, _) = draw(&mut app, 120, 36);
    let band = app
        .hits
        .rect_of(&Target::SideRow(0))
        .expect("rail item for the open session");
    let selection = app.theme.bg("interactive.selection").bg;
    assert_eq!(Some(buf[(band.x + 1, band.y)].bg), selection);
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
            // the first row inside, `esc` its right end, hints the last row
            for y in r.y..r.bottom() {
                for x in r.x..r.right() {
                    let sym = buf[(x, y)].symbol();
                    let edge = x == r.x || x == r.right() - 1 || y == r.y || y == r.bottom() - 1;
                    assert!(!(edge && boxes.contains(&sym)), "{ctx}: border {sym}");
                }
            }
            let title = &rows[r.y as usize + 1];
            assert!(
                title.contains("esc") || which == "confirm",
                "{ctx}: {title}"
            );
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
fn settings_headings_are_muted_uppercase_and_the_selected_row_is_on_the_band() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_modal(&mut app, "settings");
    let (buf, rows) = draw(&mut app, 120, 36);
    let r = raised_box(&app, &buf);
    let head = rows.iter().position(|l| l.contains("APPEARANCE")).unwrap();
    let hx = col_of(&rows[head], "APPEARANCE") as u16;
    assert_eq!(
        buf[(hx, head as u16)].fg,
        app.theme.color("text.muted").unwrap()
    );
    assert_ne!(
        buf[(hx, head as u16)].fg,
        app.theme.color("agent.violet").unwrap()
    );
    // the selected row: selection band and the focus bar, not the accent fill
    let sel = app
        .hits
        .rect_of(&Target::SettingsRow(app.ui.settings_row))
        .unwrap();
    let selection = app.theme.color("interactive.selection").unwrap();
    for x in [sel.x, sel.x + 5, sel.right() - 1] {
        assert_eq!(buf[(x, sel.y)].bg, selection, "band at {x}");
    }
    assert_eq!(buf[(sel.x, sel.y)].symbol(), "▌");
    assert_ne!(
        buf[(sel.x + 5, sel.y)].bg,
        app.theme.color("interactive.accent").unwrap()
    );
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
}

#[test]
fn confirm_defaults_to_cancel_and_marks_delete_as_an_error() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    open_modal(&mut app, "confirm");
    let (buf, rows) = draw(&mut app, 120, 36);
    let y = rows.iter().position(|l| l.contains("[ Cancel ]")).unwrap();
    let cx = col_of(&rows[y], "[ Cancel ]") as u16;
    let dx = col_of(&rows[y], "[ Delete ]") as u16;
    let selection = app.theme.color("interactive.selection").unwrap();
    assert_eq!(buf[(cx + 2, y as u16)].bg, selection, "Cancel is selected");
    assert_eq!(buf[(cx - 1, y as u16)].symbol(), "▌", "focus bar");
    assert_eq!(
        buf[(dx + 2, y as u16)].fg,
        app.theme.color("status.error").unwrap()
    );
    assert_ne!(buf[(dx + 2, y as u16)].bg, selection);
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
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    for needle in [
        "New session in",
        "Build",
        "Tab agent \u{b7} ctrl+m model \u{b7} ctrl+e effort \u{b7} ctrl+d directory",
        "\u{25c6} System prompt",
        "Does the actual work.",
        "\u{25c6} AGENTS.md",
        "Read PLAN.md first: principles, layout.",
        "in the system prompt",
        "\u{25c6} Tools",
        "5 tools \u{b7} 5 on",
        "Context total \u{b7} ~",
    ] {
        assert!(all.contains(needle), "missing {needle:?}:\n{all}");
    }
    // Estimated until Core drafts land: every count carries `~`.
    assert!(all.contains("~") && all.contains(" tok"), "{all}");
    // The centred placeholder is gone, and there is no bar in the total row.
    assert!(!all.contains("Type a message below"), "{all}");
    let total = rows.iter().find(|l| l.contains("Context total")).unwrap();
    assert!(!total.contains('\u{2571}'), "{total}");
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
            .find(|l| l.contains("Context total"))
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
    assert!(all.contains("\u{25c6} AGENTS.md"), "{all}");
    assert!(all.contains("none in this project"), "{all}");
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
