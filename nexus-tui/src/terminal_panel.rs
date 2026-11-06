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
}

pub fn key_bytes(key: crossterm::event::KeyEvent) -> Option<Vec<u8>> {
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
        KeyCode::Up => b"\x1b[A".to_vec(),
        KeyCode::Down => b"\x1b[B".to_vec(),
        KeyCode::Right => b"\x1b[C".to_vec(),
        KeyCode::Left => b"\x1b[D".to_vec(),
        KeyCode::Home => b"\x1b[H".to_vec(),
        KeyCode::End => b"\x1b[F".to_vec(),
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
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn starts_with_empty_list_and_list_focus() {
        let panel = TerminalPanel::default();
        assert!(panel.terminals.is_empty());
        assert!(panel.selected.is_none());
        assert_eq!(panel.focus, TerminalFocus::List);
        assert_eq!(panel.screen.seq, None);
    }
}
