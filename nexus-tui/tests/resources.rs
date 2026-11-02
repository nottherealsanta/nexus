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
fn strip_counts_and_modal_list() {
    let mut app = with_resources();
    let (_, rows) = draw(&mut app, 160, 50);
    let strip = rows
        .iter()
        .find(|row| row.contains("kernel") && row.contains("shell"))
        .expect("the location strip has kernel and shell counts")
        .clone();
    assert!(strip.contains("1 kernel · 1 shell"), "strip: {strip:?}");

    let chip = app
        .hits
        .rect_of(&Target::ComposerResources)
        .expect("the counts are clickable");
    click(&mut app, chip.x + chip.width / 2, chip.y);
    let (_, rows) = draw(&mut app, 160, 50);
    let shown = rows.join("\n");
    assert!(shown.contains("Kernels and shells"), "{shown}");
    assert!(shown.contains("Kernels (1)"), "{shown}");
    assert!(shown.contains("krn_1"), "{shown}");
    assert!(shown.contains("answer: int = 42"), "{shown}");
    assert!(
        shown.contains("Background shells (1 live, 2 kept)"),
        "{shown}"
    );
    assert!(shown.contains("$ sleep 30"), "{shown}");
    assert!(shown.contains("$ printf done"), "{shown}");

    press(&mut app, &["esc"]);
    let (_, rows) = draw(&mut app, 160, 50);
    assert!(!rows.join("\n").contains("Kernels (1)"));
}

#[test]
fn no_kernel_or_shell_leaves_the_strip_unmarked() {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.open_session(SESSION);
    let (_, rows) = draw(&mut app, 160, 50);
    assert!(!rows.join("\n").contains("kernel"));
    assert!(app.hits.rect_of(&Target::ComposerResources).is_none());
}
