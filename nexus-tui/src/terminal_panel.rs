//! State for the Core terminal panel.
use crate::terminal_api::TerminalResource;
use crate::terminal_screen::TerminalScreen;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminalFocus {
    List,
    Screen,
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
