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
            // composer and footer
            assert!(all.contains("Ask the agent"), "{all}");
            assert!(rows.last().unwrap().contains("live"));
            if w >= 100 {
                // tabs in manifest order, settings pinned at the bottom of the sidebar
                let tabs: Vec<usize> = m
                    .navigation
                    .left_sidebar
                    .tab
                    .iter()
                    .map(|t| col_of(&rows[1], &t.label))
                    .collect();
                assert!(tabs.windows(2).all(|p| p[0] < p[1]), "{tabs:?}");
                assert!(
                    rows[h as usize - 2].contains("Settings"),
                    "{:?}",
                    rows[h as usize - 2]
                );
                // right sidebar: session info and the Git status reason from the manifest
                assert!(all.contains("Session info") && all.contains("Git status"));
                let reason = m.navigation.right_sidebar.section[1]
                    .unavailable_reason
                    .as_deref()
                    .unwrap();
                assert!(all.contains(&reason[..20]), "git reason missing");
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
    // timeline = canvas, left sidebar = panel (values from interface/theme.toml)
    assert_eq!(buf[(60, 10)].bg, Color::Rgb(0x12, 0x13, 0x14));
    assert_eq!(buf[(3, 20)].bg, Color::Rgb(0x18, 0x1a, 0x1c));
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
    assert!(
        rows[1].contains("Sessions") && rows[1].contains("Files"),
        "{:?}",
        rows[1]
    );
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
        app.composer_ref().text(),
        "line one\nline two\n\u{65e5}\u{672c}\u{8a9e}"
    );
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(joined(&rows).contains("\u{65e5}\u{672c}\u{8a9e}"));
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
        .position(|r| r.contains("Ask the agent"))
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
