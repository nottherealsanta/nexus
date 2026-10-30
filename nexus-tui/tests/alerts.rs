//! The waiting card and alerts (DESIGN_PLAN.md D14 and D15): card heights,
//! toast stacking, the jump pill and the toast-detail modal.

mod common;
use common::*;
use nexus_tui::app::App;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{Focus, Level, OverlayKind, Target};

fn joined(rows: &[String]) -> String {
    rows.join("\n")
}

fn session_app(size: (u16, u16)) -> App {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), size);
    app.open_session(SESSION);
    app.effects.clear();
    app
}

fn send(app: &mut App, kind: &str, data: serde_json::Value) {
    let v = serde_json::json!({"id": 50, "seq": 50, "time": 1.0, "type": kind,
        "session_id": SESSION, "data": data, "cursor": "log_c915bfc9b6d826c4:50"});
    if let Ok(nexus_tui::api::Frame::Event(e)) = nexus_tui::api::Frame::from_value(kind, None, &v) {
        app.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
}

fn approving(tool: &str, args: serde_json::Value, size: (u16, u16)) -> App {
    let mut app = session_app(size);
    send(
        &mut app,
        "permission.asked",
        serde_json::json!({"id": "per_9", "session_id": SESSION, "tool": tool,
            "call_id": "call_9", "args": args}),
    );
    app
}

fn long_patch() -> serde_json::Value {
    let old: Vec<String> = (0..30).map(|n| format!("old line {n}")).collect();
    let new: Vec<String> = (0..30).map(|n| format!("new line {n}")).collect();
    serde_json::json!({"path": "src/loader.py", "old_string": old.join("\n"),
        "new_string": new.join("\n")})
}

#[test]
fn approval_card_height_is_header_table_choices_and_hint() {
    // pad, title, waiting row, blank, 1 parameter, blank, 4 choices, blank, hint, pad
    let mut app = approving("bash", serde_json::json!({"command": "ls"}), (120, 36));
    let (_, rows) = draw(&mut app, 120, 36);
    assert_eq!(app.regions.waiting.height, 13);
    let all = joined(&rows);
    assert!(all.contains("Allow bash?"), "{all}");
    assert!(all.contains("waiting on you · 0:00"), "{all}");
    assert!(all.contains("command  ls"), "{all}");
    // the heavy rail runs the card's height
    let rail = rows.iter().filter(|r| r.contains('\u{2503}')).count();
    assert_eq!(rail, 13);
    // the choice list: key chips, the selected row first
    let row = rows.iter().find(|r| r.contains("Allow once")).unwrap();
    assert!(row.contains("\u{25b8}") && row.contains(" y "), "{row}");
    assert!(joined(&rows).contains(" n  Deny"), "{all}");
}

#[test]
fn a_diff_is_capped_at_twelve_rows_and_gives_way_when_the_card_is_short() {
    let mut app = approving("edit", long_patch(), (160, 48));
    let (_, rows) = draw(&mut app, 160, 48);
    // pad, head, sub, blank, path, blank, 11 diff rows + the note, blank, 4, blank, hint, pad
    assert_eq!(app.regions.waiting.height, 26);
    let all = joined(&rows);
    assert!(all.contains("path  src/loader.py"), "{all}");
    assert!(!all.contains("old_string"), "{all}");
    assert!(all.contains("more rows"), "{all}");
    let diff_rows = rows
        .iter()
        .filter(|r| r.contains("old line") || r.contains("new line"))
        .count();
    assert!(diff_rows <= 12, "{diff_rows}");

    // at 80x24 the diff shrinks first; every choice stays
    let mut app = approving("edit", long_patch(), (80, 24));
    let (_, rows) = draw(&mut app, 80, 24);
    let all = joined(&rows);
    assert!(app.regions.waiting.height < 26);
    assert!(app.regions.timeline.height >= 3);
    for want in [
        "Allow once",
        "Allow for session",
        "Deny",
        "Deny with a note",
    ] {
        assert!(all.contains(want), "{want}: {all}");
    }
}

#[test]
fn all_four_choices_are_targets_and_the_cursor_walks_them() {
    let mut app = approving("bash", serde_json::json!({"command": "ls"}), (120, 36));
    let (_, rows) = draw(&mut app, 120, 36);
    for label in ["Allow for session", "Deny with a note"] {
        let y = rows.iter().position(|r| r.contains(label)).unwrap() as u16;
        let x = rows[y as usize].find(label).unwrap() as u16;
        assert!(
            matches!(app.hits.at(x, y), Some(Target::Wait(_))),
            "{label}"
        );
        assert!(!rows[y as usize].contains("not offered"), "{label}");
    }
    // down, down reaches Deny and enter denies
    press(&mut app, &["alt+w", "down", "down", "enter"]);
    assert!(matches!(
        app.effects.last(),
        Some(nexus_tui::app::Effect::Api(
            nexus_tui::actions::ApiCall::Reply { allow: false, .. }
        ))
    ));
}

#[test]
fn question_card_marks_single_and_multiple_choice_and_offers_free_text() {
    let mut app = session_app((120, 36));
    let q = serde_json::json!({"id": "qst_9", "session_id": SESSION, "call_id": "c", "created": 1.0,
        "questions": [
            {"id": "db", "question": "Which database?", "multiple": false, "allow_free_text": true,
             "options": [{"id": "pg", "label": "PostgreSQL"}, {"id": "lite", "label": "SQLite"}]},
            {"id": "x", "question": "Extras?", "multiple": true,
             "options": [{"id": "a", "label": "alpha"}]}]});
    send(&mut app, "question.asked", q);
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("Nexus asks"), "{all}");
    assert!(all.contains("question 1 of 2"), "{all}");
    assert!(all.contains("1 PostgreSQL"), "{all}");
    assert!(!all.contains("( )"), "{all}");
    assert!(!all.contains("waiting on you · 0:00"), "{all}");
    assert!(all.contains("Or type your own answer"), "{all}");
    // pad, head, sub, blank, question, 2 options, blank, field, blank, hint, pad
    assert_eq!(app.regions.waiting.height, 12);
    press(&mut app, &["down", "enter"]);
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("Extras?"), "{all}");
    assert!(all.contains("[ ] 1 alpha"), "{all}");
    typing(&mut app, " ");
    assert!(joined(&draw(&mut app, 120, 36).1).contains("[x] 1 alpha"));
}

fn toast_titles(rows: &[String]) -> Vec<usize> {
    rows.iter()
        .enumerate()
        .filter(|(_, r)| r.contains("Toast "))
        .map(|(i, _)| i)
        .collect()
}

#[test]
fn toasts_stack_top_right_oldest_first_and_queue_behind_plus_n_more() {
    let mut app = session_app((120, 36));
    for n in 1..=5 {
        app.toast(Level::Info, format!("Toast {n}: body line {n}"));
    }
    let (_, rows) = draw(&mut app, 120, 36);
    let titles = toast_titles(&rows);
    assert_eq!(titles.len(), 3, "{}", joined(&rows));
    assert!(rows[titles[0]].contains("Toast 1") && rows[titles[2]].contains("Toast 3"));
    // under the header, in the page column's top-right corner
    let top = app.regions.timeline.y as usize;
    assert_eq!(titles[0], top);
    // title row, body row, a gap
    assert_eq!(titles[1], top + 3);
    assert!(rows[top + 1].contains("body line 1"));
    let right = app.regions.centre.right() as usize;
    let end = rows[top].find('\u{d7}').unwrap();
    assert!(nexus_tui::text::width(&rows[top][..end]) + 2 <= right);
    assert!(joined(&rows).contains("+2 more"), "{}", joined(&rows));
    // the level bar and no lifetime hairline: nothing under the stack but the tag
    assert!(rows[top].contains('\u{258c}'));
    // dismissing the first brings the queue forward
    let x = rows[top].find('\u{d7}').unwrap() as u16;
    click(
        &mut app,
        nexus_tui::text::width(&rows[top][..x as usize]) as u16,
        top as u16,
    );
    let (_, rows) = draw(&mut app, 120, 36);
    assert!(!joined(&rows).contains("Toast 1"));
    assert!(joined(&rows).contains("Toast 4"));
    assert!(joined(&rows).contains("+1 more"));
}

const LONG: &str = "provider: 429 rate limited. The request was rejected because the organization exceeded its tokens-per-minute quota for this model.\n\nThe provider returned this body:\n{\"error\": {\"type\": \"rate_limit_exceeded\"}}";

#[test]
fn a_click_on_a_toast_opens_its_full_text_and_c_copies_it() {
    let mut app = session_app((120, 36));
    app.toast(Level::Error, format!("Turn failed\n{LONG}"));
    let (_, rows) = draw(&mut app, 120, 36);
    let top = app.regions.timeline.y as usize;
    let x = rows[top].find("Turn failed").unwrap();
    let x = nexus_tui::text::width(&rows[top][..x]) as u16;
    click(&mut app, x, top as u16);
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::AlertDetail { .. })
    ));
    let (_, rows) = draw(&mut app, 120, 36);
    let all = joined(&rows);
    assert!(all.contains("Turn failed"), "{all}");
    assert!(
        all.contains("quota for this model"),
        "wrapped, whole: {all}"
    );
    assert!(all.contains("The provider returned this body:"), "{all}");
    assert!(all.contains("c copy") && all.contains("esc close"), "{all}");
    press(&mut app, &["c"]);
    assert!(app.effects.iter().any(
        |e| matches!(e, nexus_tui::app::Effect::Copy(t) if t.contains("quota for this model"))
    ));
    press(&mut app, &["esc"]);
    assert!(app.ui.overlay.is_none());
}

#[test]
fn enter_on_a_focused_toast_opens_the_same_detail_and_the_drawer_does_too() {
    let mut app = session_app((120, 36));
    app.toast(Level::Warning, format!("Slow provider\n{LONG}"));
    draw(&mut app, 120, 36);
    app.ui.focus = Focus::Alert;
    press(&mut app, &["enter"]);
    let Some(OverlayKind::AlertDetail { id, .. }) = app.ui.overlay.as_ref().map(|o| o.kind.clone())
    else {
        panic!("enter opens the detail");
    };
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(
        all.contains("Slow provider") && all.contains("quota for this model"),
        "{all}"
    );
    press(&mut app, &["esc"]);
    assert!(app.ui.overlay.is_none());
    // the notifications drawer opens the same detail for an entry; Esc goes back
    app.execute("notifications.open");
    press(&mut app, &["enter"]);
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::AlertDetail { id: got, back: Some(0), .. }) if *got == id
    ));
    press(&mut app, &["esc"]);
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::Notifications { .. })
    ));
}

#[test]
fn a_long_alert_scrolls_in_its_detail() {
    let mut app = session_app((120, 36));
    let text: Vec<String> = (0..80).map(|n| format!("detail line {n}")).collect();
    app.toast(Level::Info, format!("Many lines\n{}", text.join("\n")));
    draw(&mut app, 120, 36);
    app.ui.focus = Focus::Alert;
    press(&mut app, &["enter"]);
    let first = joined(&draw(&mut app, 120, 36).1);
    assert!(first.contains("detail line 0") && !first.contains("detail line 79"));
    press(
        &mut app,
        &[
            "pagedown", "pagedown", "pagedown", "pagedown", "pagedown", "pagedown", "pagedown",
            "pagedown", "pagedown", "pagedown", "pagedown", "pagedown",
        ],
    );
    let later = joined(&draw(&mut app, 120, 36).1);
    assert!(later.contains("detail line 79"), "{later}");
}

fn add_message(a: &mut App, n: i64) {
    let v = serde_json::json!({"id": 1000 + n, "type": "message.added", "session_id": SESSION,
        "cursor": format!("log_c915bfc9b6d826c4:{}", 1000 + n),
        "data": {"id": format!("msg_long_{n}"), "session_id": SESSION, "seq": 100 + n,
                 "created": 1793113500.0 + n as f64,
                 "role": "assistant", "parts": [{"type": "text", "text": format!("paragraph {n}")}]}});
    if let Ok(nexus_tui::api::Frame::Event(e)) =
        nexus_tui::api::Frame::from_value("message.added", None, &v)
    {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
}

#[test]
fn the_jump_pill_sits_on_the_row_above_the_composer_card_at_80x24() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (80, 24));
    for n in 0..60 {
        add_message(&mut app, n);
    }
    let (_, rows) = draw(&mut app, 80, 24);
    assert!(!joined(&rows).contains("new"), "following: no pill");
    app.ui.focus = Focus::Timeline;
    app.scroll_timeline(-10);
    let (_, rows) = draw(&mut app, 80, 24);
    let pill = app.hits.rect_of(&Target::JumpLatest).expect("pill");
    let composer = app.regions.composer;
    assert_eq!(pill.y, composer.y, "the row above the card");
    assert!(
        pill.y >= app.regions.timeline.bottom(),
        "never over the timeline"
    );
    // right-aligned with the card's edge, two cells in from the column's
    assert_eq!(pill.right(), composer.right() - 2);
    let row = &rows[pill.y as usize];
    assert!(row.contains("new") && row.contains("end"), "{row}");
    assert!(row.contains('\u{2193}'), "{row}");
    // the card follows directly: its top padding row (no `▄` edge since the
    // padding revision, nexus-tui README), then the input
    assert!(rows[pill.y as usize + 1].trim().is_empty(), "{rows:?}");
    assert!(!rows[pill.y as usize + 1].contains('\u{2584}'));
    assert!(
        rows[pill.y as usize + 2].contains("Type a message"),
        "{rows:?}"
    );
    // a click jumps and follows again
    click(&mut app, pill.x + 1, pill.y);
    assert!(app.ui.scroll.follow);
}

#[test]
fn a_banner_is_one_row_at_the_top_of_the_page_above_the_timeline() {
    use nexus_tui::state::ConnectionState as C;
    let mut app = session_app((120, 36));
    let before = {
        draw(&mut app, 120, 36);
        app.regions.timeline
    };
    app.apply_sync(nexus_tui::sync::SyncMsg::Connection(C::Offline {
        reason: "refused".into(),
    }));
    let (_, rows) = draw(&mut app, 120, 36);
    let banner = app.regions.banner;
    assert_eq!(banner.height, 1);
    assert_eq!(banner.y, app.regions.centre.y);
    assert_eq!(
        app.regions.timeline.y,
        before.y + 1,
        "it pushes the timeline down"
    );
    assert!(
        rows[banner.y as usize].contains("not reachable"),
        "{rows:?}"
    );
}

#[test]
fn the_card_counts_how_long_it_has_waited_on_you() {
    use std::time::Duration;
    let mut app = approving("bash", serde_json::json!({"command": "ls"}), (120, 36));
    let t0 = std::time::Instant::now();
    app.tick(t0);
    assert!(
        !app.tick(t0 + Duration::from_millis(400)),
        "no visible change inside a second"
    );
    assert!(
        app.tick(t0 + Duration::from_secs(42)),
        "the clock text changed"
    );
    let all = joined(&draw(&mut app, 120, 36).1);
    assert!(all.contains("waiting on you · 0:42"), "{all}");
}

/// Writes colour SVGs of the cards and alerts for visual review:
/// `I8_SHOTS=/some/dir cargo test --test alerts -- --ignored shots`.
/// No Core has a pending approval in the seeded harness, so these frames come
/// from fixtures.
#[test]
#[ignore]
fn shots() {
    let Ok(dir) = std::env::var("I8_SHOTS") else {
        return;
    };
    let write = |name: &str, app: &mut App, size: (u16, u16), light: bool| {
        draw(app, size.0, size.1);
        let buf = nexus_tui::render::render_buffer(app, size.0, size.1);
        let (fg, bg) = nexus_tui::svg::page_colors(light);
        std::fs::write(
            format!("{dir}/{name}.svg"),
            nexus_tui::svg::to_svg(&buf, fg, bg),
        )
        .unwrap();
    };
    for (mode, light) in [(Mode::Dark, false), (Mode::Light, true)] {
        let tag = if light { "light" } else { "dark" };
        let mk = |size| {
            let mut a = empty(opts(mode, Tier::TrueColor), size);
            a.open_session(SESSION);
            a
        };
        let perm = |a: &mut App, tool: &str, args: serde_json::Value| {
            send(
                a,
                "permission.asked",
                serde_json::json!({"id": "per_9", "session_id": SESSION, "tool": tool,
                    "call_id": "call_9", "args": args}),
            );
        };
        for size in [(120, 36), (80, 24)] {
            let mut a = mk(size);
            perm(
                &mut a,
                "bash",
                serde_json::json!({"command": "pytest tests/test_loader.py -q --maxfail=1", "cwd": "~/repos/nexus-demo"}),
            );
            a.tick(std::time::Instant::now());
            a.ui.focus = Focus::Waiting;
            write(
                &format!("approval-{}x{}-{tag}", size.0, size.1),
                &mut a,
                size,
                light,
            );
            let mut a = mk(size);
            perm(&mut a, "edit", long_patch());
            a.ui.focus = Focus::Waiting;
            write(
                &format!("approval-diff-{}x{}-{tag}", size.0, size.1),
                &mut a,
                size,
                light,
            );
        }
        let mut a = mk((120, 36));
        let q = serde_json::json!({"id": "qst_9", "session_id": SESSION, "call_id": "c", "created": 1.0,
            "questions": [{"id": "db", "question": "How should the loader detect a changed manifest?", "multiple": false, "allow_free_text": true,
             "options": [{"id": "a", "label": "Compare the file's mtime"}, {"id": "b", "label": "Hash the contents on every call"}, {"id": "c", "label": "Never; clear the cache on restart"}]},
             {"id": "x", "question": "Which extras?", "multiple": true,
             "options": [{"id": "a", "label": "A regression test"}]}]});
        send(&mut a, "question.asked", q);
        a.ui.focus = Focus::Waiting;
        write(&format!("question-120x36-{tag}"), &mut a, (120, 36), light);

        let mut a = with_turn(opts(mode, Tier::TrueColor), (120, 36));
        a.toast(Level::Error, format!("Turn failed\n{LONG}"));
        a.toast(Level::Success, "Copied session id\nses_1a1c9f40e5a19");
        a.toast(Level::Info, "Core updated\nNexus Core 0.10.3 is running");
        a.toast(Level::Warning, "Detail: high");
        a.toast(Level::Info, "Another one");
        write(&format!("toasts-120x36-{tag}"), &mut a, (120, 36), light);
        let mut b = with_turn(opts(mode, Tier::TrueColor), (120, 36));
        b.toast(Level::Error, format!("Turn failed\n{LONG}"));
        draw(&mut b, 120, 36);
        b.ui.focus = Focus::Alert;
        press(&mut b, &["enter"]);
        write(
            &format!("toast-detail-120x36-{tag}"),
            &mut b,
            (120, 36),
            light,
        );

        let mut p = with_turn(opts(mode, Tier::TrueColor), (120, 36));
        for n in 0..60 {
            add_message(&mut p, n);
        }
        draw(&mut p, 120, 36);
        p.ui.focus = Focus::Timeline;
        p.scroll_timeline(-10);
        write(&format!("jump-120x36-{tag}"), &mut p, (120, 36), light);
    }
}
