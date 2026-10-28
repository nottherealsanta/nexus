//! Shared builders for shell tests: an `App` fed from the fixtures.
#![allow(dead_code)]

use nexus_tui::api::{Frame, Snapshot};
use nexus_tui::app::App;
use nexus_tui::state::{AppState, ConnectionState};
use nexus_tui::sync::{ResyncCause, SyncMsg};
use nexus_tui::theme::{Mode, Options, Theme, Tier};
use nexus_tui::transport::SseParser;

pub const SNAPSHOT: &str = include_str!("../../fixtures/real/snapshot.json");
pub const TURN: &str = include_str!("../../fixtures/documented/turn.sse");
pub const SESSION: &str = "ses_1a17de160eccc428a38";

pub fn opts(mode: Mode, tier: Tier) -> Options {
    Options {
        mode,
        tier,
        ascii: false,
        reduced_motion: true,
    }
}

pub fn events(sse: &str) -> Vec<nexus_tui::api::Event> {
    let mut p = SseParser::new();
    p.feed(sse.as_bytes())
        .iter()
        .filter_map(|f| match Frame::from_sse(f) {
            Ok(Frame::Event(e)) => Some(e),
            _ => None,
        })
        .collect()
}

pub fn snapshot() -> Snapshot {
    serde_json::from_str(SNAPSHOT).unwrap()
}

/// An empty workspace: connected, the fixture's project and one empty session.
pub fn empty(o: Options, size: (u16, u16)) -> App {
    let mut state = AppState::new();
    state.apply(SyncMsg::Connection(ConnectionState::Live));
    state.apply(SyncMsg::Snapshot {
        snapshot: Box::new(snapshot()),
        cause: ResyncCause::Initial,
    });
    App::new(state, Theme::new(o), size)
}

/// The fixture session selected with the documented turn applied (messages, an
/// approval and a question waiting is cleared by their replies, so none wait).
pub fn with_turn(o: Options, size: (u16, u16)) -> App {
    let mut app = empty(o, size);
    app.open_session(SESSION);
    app.effects.clear();
    for e in events(TURN) {
        app.apply_sync(SyncMsg::Event(e));
    }
    app
}

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};

/// A key event from the manifest's key syntax (`ctrl+p`, `alt+enter`, `esc`, `a`).
pub fn key(name: &str) -> KeyEvent {
    let mut mods = KeyModifiers::NONE;
    let mut parts: Vec<&str> = name.split('+').collect();
    let last = parts.pop().unwrap();
    for p in parts {
        mods |= match p {
            "ctrl" => KeyModifiers::CONTROL,
            "alt" => KeyModifiers::ALT,
            "shift" => KeyModifiers::SHIFT,
            _ => panic!("modifier {p}"),
        };
    }
    let code = match last {
        "esc" => KeyCode::Esc,
        "enter" => KeyCode::Enter,
        "tab" => KeyCode::Tab,
        "up" => KeyCode::Up,
        "down" => KeyCode::Down,
        "left" => KeyCode::Left,
        "right" => KeyCode::Right,
        "backspace" => KeyCode::Backspace,
        "pagedown" => KeyCode::PageDown,
        "end" => KeyCode::End,
        "pageup" => KeyCode::PageUp,
        "f6" => KeyCode::F(6),
        s if s.chars().count() == 1 => KeyCode::Char(s.chars().next().unwrap()),
        s => panic!("key {s}"),
    };
    KeyEvent::new(code, mods)
}

pub fn press(app: &mut App, names: &[&str]) {
    for n in names {
        app.handle_key(key(n));
    }
}

pub fn typing(app: &mut App, s: &str) {
    for c in s.chars() {
        app.handle_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
    }
}

pub fn click(app: &mut App, x: u16, y: u16) {
    app.handle_mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: x,
        row: y,
        modifiers: KeyModifiers::NONE,
    });
}

/// Draw and return the buffer (for colour assertions) and its text rows.
pub fn draw(app: &mut App, w: u16, h: u16) -> (ratatui::buffer::Buffer, Vec<String>) {
    use ratatui::backend::TestBackend;
    app.set_size(w, h);
    let mut term = ratatui::Terminal::new(TestBackend::new(w, h)).unwrap();
    term.draw(|f| nexus_tui::render::render(f, app)).unwrap();
    let buf = term.backend().buffer().clone();
    let rows = nexus_tui::render::buffer_rows(&buf);
    (buf, rows)
}

pub fn busy(app: &mut App) {
    let v = serde_json::json!({"id": 40, "type": "session.status", "session_id": SESSION, "cursor": "log_c915bfc9b6d826c4:40",
        "data": {"status": "busy", "turn_id": "trn_9", "origin": "prompt"}});
    if let Ok(Frame::Event(e)) = Frame::from_value("session.status", None, &v) {
        app.apply_sync(SyncMsg::Event(e));
    }
}

pub fn right_click(app: &mut App, x: u16, y: u16) {
    app.handle_mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Right),
        column: x,
        row: y,
        modifiers: KeyModifiers::NONE,
    });
}
