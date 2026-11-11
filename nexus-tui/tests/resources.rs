//! The location strip's kernel and shell counts and their details modal.

mod common;
use common::*;

use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Target;
use serde_json::json;

fn with_resources() -> nexus_tui::app::App {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.open_session(SESSION);
    app.state.kernels = vec![json!({
        "id": "krn_1", "created_by": SESSION, "created": 9.0, "cell_count": 2,
        "busy": true, "users": [SESSION],
        "variables": [{"name": "answer", "type": "int", "repr": "42"}],
        "cells_clipped": 0
    })];
    app.state.jobs = vec![
        json!({
            "id": "job_1", "session_id": SESSION, "root": SESSION, "command": "sleep 30",
            "directory": "/work/demo", "started": 9.0, "finished": null, "duration_s": 3.0,
            "status": "running", "exit_code": null, "timeout": null,
            "output_chars": 0, "dropped_chars": 0
        }),
        json!({
            "id": "job_2", "session_id": SESSION, "root": SESSION, "command": "printf done",
            "directory": "/work/demo", "started": 5.0, "finished": 6.0, "duration_s": 1.0,
            "status": "done", "exit_code": 0, "timeout": null,
            "output_chars": 4, "dropped_chars": 0
        }),
    ];
    app
}

#[test]
fn indicators_stay_centered_when_working_starts() {
    let mut app = with_resources();
    let (_, rows) = draw(&mut app, 160, 50);
    let line = rows.iter().find(|row| row.contains("1 shell")).unwrap();
    let start = line[..line.find("kernel").unwrap()].chars().count();
    let end = line[..line.find("1 shell").unwrap()].chars().count() + "1 shell".len();
    let center = app.regions.composer.x as usize + app.regions.composer.width as usize / 2;
    assert!(((start + end) / 2).abs_diff(center) <= 1, "{line}");
    busy(&mut app);
    let (_, rows) = draw(&mut app, 160, 50);
    let line = rows.iter().find(|row| row.contains("1 shell")).unwrap();
    assert_eq!(line[..line.find("kernel").unwrap()].chars().count(), start);
}

#[test]
fn strip_counts_and_modal_list() {
    let mut app = with_resources();
    let (_, rows) = draw(&mut app, 160, 50);
    let strip = rows
        .iter()
        .find(|row| row.contains("kernel"))
        .expect("the location strip has a kernel indicator")
        .clone();
    assert!(strip.contains("1 shell"), "strip: {strip:?}");
    assert!(app.hits.rect_of(&Target::ComposerTerminals).is_none());

    let chip = app
        .hits
        .rect_of(&Target::ComposerResources)
        .expect("the counts are clickable");
    click(&mut app, chip.x + chip.width / 2, chip.y);
    let (_, rows) = draw(&mut app, 160, 50);
    let shown = rows.join("\n");
    assert!(!shown.contains("Background shells ("), "{shown}");
    assert!(shown.contains("Kernels (1)"), "{shown}");
    assert!(shown.contains("krn_1"), "{shown}");
    assert!(shown.contains("answer: int = 42"), "{shown}");
    press(&mut app, &["esc"]);
    let (_, rows) = draw(&mut app, 160, 50);
    assert!(!rows.join("\n").contains("Kernels (1)"));
    let chip = app.hits.rect_of(&Target::ComposerShells).unwrap();
    click(&mut app, chip.x, chip.y);
    let shown = draw(&mut app, 160, 50).1.join("\n");
    assert!(!app.terminal_open, "{shown}");
    assert!(!shown.contains("Kernels (1)"), "{shown}");
    assert!(
        shown.contains("Background shells (1 live, 2 kept)"),
        "{shown}"
    );
    assert!(shown.contains("$ sleep 30"), "{shown}");
}

#[test]
fn background_and_terminal_shells_are_separate_and_clickable() {
    let mut app = with_resources();
    app.terminal_panel.terminals.push(
        serde_json::from_value(json!({
            "id": "t1", "name": "Shell", "workspace": "/tmp", "status": "running",
            "controller": null, "rows": 24, "cols": 80
        }))
        .unwrap(),
    );
    let (_, rows) = draw(&mut app, 160, 50);
    let shown = rows.join("\n");
    assert!(shown.contains("1 shell"), "{shown}");
    assert!(shown.contains("1 terminal shell"), "{shown}");
    assert!(app.hits.rect_of(&Target::ComposerResources).is_some());
    assert!(app.hits.rect_of(&Target::ComposerTerminals).is_some());
}

#[test]
fn background_shell_opens_the_jobs_list_without_a_kernel() {
    let mut app = with_resources();
    app.state.kernels.clear();
    let (_, rows) = draw(&mut app, 160, 50);
    assert!(rows.join("\n").contains("1 shell"));
    let chip = app.hits.rect_of(&Target::ComposerShells).unwrap();
    click(&mut app, chip.x, chip.y);
    assert!(!app.terminal_open);
    assert!(draw(&mut app, 160, 50)
        .1
        .join("\n")
        .contains("Background shells (1 live, 2 kept)"));
}

#[test]
fn a_running_job_and_a_user_terminal_are_counted_separately() {
    let mut app = with_resources();
    app.terminal_panel.terminals.push(
        serde_json::from_value(json!({
            "id":"t1", "name":"sleep 30", "workspace":"/tmp", "status":"running",
            "controller":null, "rows":24, "cols":80
        }))
        .unwrap(),
    );
    assert_eq!(app.resource_counts().1, 1);
    assert_eq!(app.terminal_shell_count(), 1);
    let (_, rows) = draw(&mut app, 160, 50);
    let shown = rows.join("\n");
    assert!(shown.contains("1 terminal shell"), "{shown}");
    assert!(shown.contains("1 shell"), "{shown}");
    let chip = app.hits.rect_of(&Target::ComposerTerminals).unwrap();
    click(&mut app, chip.x, chip.y);
    assert!(app.terminal_open);
}

#[test]
fn no_kernel_or_shell_leaves_the_strip_unmarked() {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.open_session(SESSION);
    let (_, rows) = draw(&mut app, 160, 50);
    assert!(!rows.join("\n").contains("kernel"));
    assert!(!rows.join("\n").contains("0 shell"));
    assert!(app.hits.rect_of(&Target::ComposerResources).is_none());
    assert!(app.hits.rect_of(&Target::ComposerTerminals).is_none());
}
