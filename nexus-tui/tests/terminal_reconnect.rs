mod common;
use common::*;
use nexus_tui::{
    actions::ApiCall,
    app::Effect,
    terminal_panel::TerminalEvent,
    theme::{Mode, Tier},
};

#[test]
fn terminal_reconnects_read_only_without_replaying_pending_input() {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    draw(&mut app, 120, 36);
    app.terminal_panel.terminals.push(
        serde_json::from_value(serde_json::json!({
            "id":"t1", "name":"Terminal", "workspace":"/tmp", "status":"running",
            "controller":null, "rows":24, "cols":80
        }))
        .unwrap(),
    );
    app.terminal_created("t1".into());
    let generation = app.terminal_panel.generation;
    app.terminal_event(TerminalEvent::Attached {
        generation,
        attachment: "old".into(),
    });
    app.terminal_controlled = true;
    app.terminal_panel.input_busy = true;
    app.terminal_panel
        .input_queue
        .push_back(b"never replay".to_vec());
    app.effects.clear();
    app.terminal_event(TerminalEvent::Ended {
        generation,
        error: "network lost".into(),
    });
    assert!(!app.terminal_controlled);
    assert!(app.terminal_panel.input_queue.is_empty());
    let retry_at = app.terminal_panel.reconnect_at.unwrap();
    app.tick(retry_at);
    assert!(app
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::TerminalAttach { id, .. } if id == "t1")));
    app.effects.clear();
    app.terminal_event(TerminalEvent::Attached {
        generation: app.terminal_panel.generation,
        attachment: "new".into(),
    });
    assert!(!app.terminal_controlled);
    assert_eq!(app.terminal_panel.next_input, 1);
    assert!(!app.effects.iter().any(|effect| matches!(
        effect,
        Effect::Api(ApiCall::TerminalControl { .. } | ApiCall::TerminalInput { .. })
    )));
    app.execute("terminal.acquire");
    assert!(app.effects.iter().any(|effect| matches!(effect,
        Effect::Api(ApiCall::TerminalControl { attachment, acquire: true, .. }) if attachment == "new")));
}
