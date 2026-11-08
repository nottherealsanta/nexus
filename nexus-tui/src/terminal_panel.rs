//! State for the Core terminal panel.
use crate::terminal_api::TerminalResource;
use crate::terminal_screen::TerminalScreen;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminalFocus {
    List,
    Screen,
}

#[derive(Debug)]
pub enum TerminalEvent {
    Attached {
        generation: u64,
        attachment: String,
    },
    Frame {
        generation: u64,
        frame: serde_json::Value,
    },
    Ended {
        generation: u64,
        error: String,
    },
}

pub struct TerminalPanel {
    pub terminals: Vec<TerminalResource>,
    pub selected: Option<String>,
    pub focus: TerminalFocus,
    pub screen: TerminalScreen,
    pub cursor: usize,
    pub creating: bool,
    pub loading: bool,
    pub client_id: String,
    pub generation: u64,
    pub attachment: Option<String>,
    pub next_input: u64,
    pub input_busy: bool,
    pub input_queue: std::collections::VecDeque<Vec<u8>>,
    pub size: Option<(u16, u16)>,
    pub resize_busy: bool,
    pub control_busy: bool,
    pub selection: Option<crate::selection::Selection>,
    pub selecting: bool,
}

pub fn screen_area(
    dock: ratatui::layout::Rect,
    read_only: bool,
    gap: bool,
) -> ratatui::layout::Rect {
    // One header row (title, hint and actions) and one tab row above the screen.
    let top = (2 + u16::from(read_only) + u16::from(gap)).min(dock.height);
    ratatui::layout::Rect::new(dock.x, dock.y + top, dock.width, dock.height - top)
}

pub fn key_bytes(key: crossterm::event::KeyEvent, application_cursor: bool) -> Option<Vec<u8>> {
    use crossterm::event::{KeyCode, KeyModifiers};
    let mut bytes = match key.code {
        KeyCode::Char(c) if key.modifiers.contains(KeyModifiers::CONTROL) && c.is_ascii() => {
            let c = c.to_ascii_lowercase() as u8;
            vec![match c {
                b'a'..=b'z' => c - b'a' + 1,
                b'@' | b' ' => 0,
                b'[' => 27,
                b'\\' => 28,
                b'^' => 30,
                b'_' => 31,
                b'?' => 127,
                _ => return None,
            }]
        }
        KeyCode::Char(c) => c.to_string().into_bytes(),
        KeyCode::Enter => vec![b'\r'],
        KeyCode::Esc => vec![27],
        KeyCode::Backspace => vec![127],
        KeyCode::Tab => vec![b'\t'],
        KeyCode::BackTab => b"\x1b[Z".to_vec(),
        KeyCode::Up => cursor_key(b'A', application_cursor),
        KeyCode::Down => cursor_key(b'B', application_cursor),
        KeyCode::Right => cursor_key(b'C', application_cursor),
        KeyCode::Left => cursor_key(b'D', application_cursor),
        KeyCode::Home => cursor_key(b'H', application_cursor),
        KeyCode::End => cursor_key(b'F', application_cursor),
        KeyCode::Delete => b"\x1b[3~".to_vec(),
        KeyCode::Insert => b"\x1b[2~".to_vec(),
        KeyCode::PageUp => b"\x1b[5~".to_vec(),
        KeyCode::PageDown => b"\x1b[6~".to_vec(),
        KeyCode::F(n) => match n {
            1 => b"\x1bOP".to_vec(),
            2 => b"\x1bOQ".to_vec(),
            3 => b"\x1bOR".to_vec(),
            4 => b"\x1bOS".to_vec(),
            5 => b"\x1b[15~".to_vec(),
            6 => b"\x1b[17~".to_vec(),
            7 => b"\x1b[18~".to_vec(),
            8 => b"\x1b[19~".to_vec(),
            9 => b"\x1b[20~".to_vec(),
            10 => b"\x1b[21~".to_vec(),
            11 => b"\x1b[23~".to_vec(),
            12 => b"\x1b[24~".to_vec(),
            _ => return None,
        },
        _ => return None,
    };
    if key.modifiers.contains(KeyModifiers::ALT) {
        bytes.insert(0, 27);
    }
    Some(bytes)
}

/// Encode a mouse event in the application's requested xterm protocol.
pub fn mouse_bytes(
    event: crossterm::event::MouseEvent,
    x: u16,
    y: u16,
    mode: vt100::MouseProtocolMode,
    encoding: vt100::MouseProtocolEncoding,
) -> Option<Vec<u8>> {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEventKind};
    use vt100::{MouseProtocolEncoding as Encoding, MouseProtocolMode as Mode};
    if mode == Mode::None {
        return None;
    }
    let button = |b| match b {
        MouseButton::Left => 0,
        MouseButton::Middle => 1,
        MouseButton::Right => 2,
    };
    let (mut code, release) = match event.kind {
        MouseEventKind::Down(b) => (button(b), false),
        MouseEventKind::Up(b) if mode != Mode::Press => (button(b), true),
        MouseEventKind::Drag(b) if matches!(mode, Mode::ButtonMotion | Mode::AnyMotion) => {
            (button(b) + 32, false)
        }
        MouseEventKind::Moved if mode == Mode::AnyMotion => (35, false),
        MouseEventKind::ScrollUp => (64, false),
        MouseEventKind::ScrollDown => (65, false),
        MouseEventKind::ScrollLeft => (66, false),
        MouseEventKind::ScrollRight => (67, false),
        _ => return Some(Vec::new()),
    };
    if event.modifiers.contains(KeyModifiers::SHIFT) {
        code += 4;
    }
    if event.modifiers.contains(KeyModifiers::ALT) {
        code += 8;
    }
    if event.modifiers.contains(KeyModifiers::CONTROL) {
        code += 16;
    }
    let x = u32::from(x) + 1;
    let y = u32::from(y) + 1;
    if encoding == Encoding::Sgr {
        return Some(
            format!("\x1b[<{code};{x};{y}{}", if release { 'm' } else { 'M' }).into_bytes(),
        );
    }
    if release {
        code = (code & !3) | 3;
    }
    let mut out = b"\x1b[M".to_vec();
    for n in [code + 32, x + 32, y + 32] {
        if encoding == Encoding::Default {
            if n > 255 {
                return Some(Vec::new());
            }
            out.push(n as u8);
        } else if let Some(c) = char::from_u32(n) {
            let mut buf = [0; 4];
            out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
        } else {
            return Some(Vec::new());
        }
    }
    Some(out)
}

fn cursor_key(final_byte: u8, application_cursor: bool) -> Vec<u8> {
    vec![27, if application_cursor { b'O' } else { b'[' }, final_byte]
}

impl Default for TerminalPanel {
    fn default() -> Self {
        Self {
            terminals: Vec::new(),
            selected: None,
            focus: TerminalFocus::List,
            screen: TerminalScreen::new(24, 80),
            cursor: 0,
            creating: false,
            loading: false,
            client_id: crate::api::RequestId::new().as_str().to_owned(),
            generation: 0,
            attachment: None,
            next_input: 1,
            input_busy: false,
            input_queue: std::collections::VecDeque::new(),
            size: None,
            resize_busy: false,
            control_busy: false,
            selection: None,
            selecting: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cursor_keys_follow_application_mode() {
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
        let up = KeyEvent::new(KeyCode::Up, KeyModifiers::NONE);
        assert_eq!(key_bytes(up, false).unwrap(), b"\x1b[A");
        assert_eq!(key_bytes(up, true).unwrap(), b"\x1bOA");
    }

    #[test]
    fn mouse_reports_requested_modes_and_encoding() {
        use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
        use vt100::{MouseProtocolEncoding as E, MouseProtocolMode as M};
        let mut event = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 0,
            row: 0,
            modifiers: KeyModifiers::NONE,
        };
        assert_eq!(mouse_bytes(event, 4, 2, M::None, E::Sgr), None);
        assert_eq!(
            mouse_bytes(event, 4, 2, M::PressRelease, E::Sgr).unwrap(),
            b"\x1b[<0;5;3M"
        );
        event.kind = MouseEventKind::Up(MouseButton::Left);
        assert_eq!(
            mouse_bytes(event, 4, 2, M::PressRelease, E::Sgr).unwrap(),
            b"\x1b[<0;5;3m"
        );
        assert!(mouse_bytes(event, 4, 2, M::Press, E::Default)
            .unwrap()
            .is_empty());
        event.kind = MouseEventKind::Drag(MouseButton::Left);
        assert!(mouse_bytes(event, 4, 2, M::PressRelease, E::Sgr)
            .unwrap()
            .is_empty());
        assert_eq!(
            mouse_bytes(event, 4, 2, M::ButtonMotion, E::Sgr).unwrap(),
            b"\x1b[<32;5;3M"
        );
    }

    #[test]
    fn starts_with_empty_list_and_list_focus() {
        let panel = TerminalPanel::default();
        assert!(panel.terminals.is_empty());
        assert!(panel.selected.is_none());
        assert_eq!(panel.focus, TerminalFocus::List);
        assert_eq!(panel.screen.seq, None);
    }
}
