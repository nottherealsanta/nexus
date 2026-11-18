mod common;

use common::{draw, empty, opts, SESSION};
use nexus_tui::theme::{Mode, Tier};

#[test]
fn session_loading_disappears_when_history_arrives() {
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.open_session(SESSION);
    let (_, screen) = draw(&mut app, 120, 36);
    assert!(
        screen.iter().any(|line| line.contains("Loading session…")),
        "{screen:?}"
    );
    app.state.model.selected.as_mut().unwrap().loaded = true;
    let (_, screen) = draw(&mut app, 120, 36);
    assert!(
        !screen.iter().any(|line| line.contains("Loading session…")),
        "{screen:?}"
    );
}
