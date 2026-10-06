//! Key → intent mapping (plan §6.2). Components return intents; owners act on them.
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Intent {
    Up,
    Down,
    Left,
    Right,
    Next,
    Prev,
    Home,
    End,
    PageUp,
    PageDown,
    Activate,
    Toggle,
    Escape,
    Search,
    MoveUp,
    MoveDown,
    Remove,
    Help,
    NextRegion,
    PrevRegion,
    NextTab,
    PrevTab,
    DismissToasts,
    Char(char),
}

/// `text_input`: a text field has focus, so `/`, `?` and letters are text.
pub fn intent(k: KeyEvent, text_input: bool) -> Option<Intent> {
    let alt = k.modifiers.contains(KeyModifiers::ALT);
    let ctrl = k.modifiers.contains(KeyModifiers::CONTROL);
    let shift = k.modifiers.contains(KeyModifiers::SHIFT);
    Some(match k.code {
        KeyCode::Up if alt => Intent::MoveUp,
        KeyCode::Down if alt => Intent::MoveDown,
        KeyCode::Up => Intent::Up,
        KeyCode::Down => Intent::Down,
        KeyCode::Left => Intent::Left,
        KeyCode::Right => Intent::Right,
        KeyCode::Tab => Intent::Next,
        KeyCode::BackTab => Intent::Prev,
        KeyCode::Home => Intent::Home,
        KeyCode::End => Intent::End,
        KeyCode::PageUp if ctrl => Intent::PrevTab,
        KeyCode::PageDown if ctrl => Intent::NextTab,
        KeyCode::PageUp => Intent::PageUp,
        KeyCode::PageDown => Intent::PageDown,
        KeyCode::Enter => Intent::Activate,
        KeyCode::Esc => Intent::Escape,
        KeyCode::Delete => Intent::Remove,
        KeyCode::F(6) if shift => Intent::PrevRegion,
        KeyCode::F(6) => Intent::NextRegion,
        KeyCode::Char(' ') if !text_input => Intent::Toggle,
        KeyCode::Char('/') if !text_input && !ctrl => Intent::Search,
        KeyCode::Char('?') if !text_input => Intent::Help,
        KeyCode::Char(c) if !ctrl && !alt => Intent::Char(c),
        _ => return None,
    })
}
