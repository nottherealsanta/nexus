//! Key names in the manifest's syntax (`ctrl+p`, `alt+enter`, `esc`) and the
//! conversion from terminal key events.

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};

/// Canonical name of a key event, or `None` for releases and unnamed keys.
/// Modifier order is `ctrl alt shift`, then the key; `shift` is implicit in
/// printable characters.
pub fn name(ev: &KeyEvent) -> Option<String> {
    if ev.kind == KeyEventKind::Release {
        return None;
    }
    let mut m = ev.modifiers;
    let key = match ev.code {
        KeyCode::Char(' ') => "space".to_owned(),
        KeyCode::Char(c) => {
            m.remove(KeyModifiers::SHIFT);
            c.to_lowercase().collect()
        }
        KeyCode::Enter => "enter".into(),
        KeyCode::Esc => "esc".into(),
        KeyCode::Tab => "tab".into(),
        KeyCode::BackTab => {
            m.insert(KeyModifiers::SHIFT);
            "tab".into()
        }
        KeyCode::Backspace => "backspace".into(),
        KeyCode::Delete => "delete".into(),
        KeyCode::Up => "up".into(),
        KeyCode::Down => "down".into(),
        KeyCode::Left => "left".into(),
        KeyCode::Right => "right".into(),
        KeyCode::Home => "home".into(),
        KeyCode::End => "end".into(),
        KeyCode::PageUp => "pageup".into(),
        KeyCode::PageDown => "pagedown".into(),
        KeyCode::F(n) => format!("f{n}"),
        _ => return None,
    };
    let mut out = String::new();
    if m.contains(KeyModifiers::SUPER) {
        out.push_str("cmd+");
    }
    if m.contains(KeyModifiers::CONTROL) {
        out.push_str("ctrl+");
    }
    if m.contains(KeyModifiers::ALT) {
        out.push_str("alt+");
    }
    if m.contains(KeyModifiers::SHIFT) {
        out.push_str("shift+");
    }
    out.push_str(&key);
    Some(out)
}

/// The printable character of an unmodified (or shifted) key press.
pub fn printable(ev: &KeyEvent) -> Option<char> {
    match ev.code {
        KeyCode::Char(c)
            if !ev
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SUPER) =>
        {
            Some(c)
        }
        _ => None,
    }
}

/// How a manifest key reads in a hint: `ctrl+p` stays `ctrl+p`; `esc` stays.
pub fn hint(key: &str) -> &str {
    key
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(code: KeyCode, m: KeyModifiers) -> KeyEvent {
        KeyEvent::new(code, m)
    }

    #[test]
    fn names_follow_the_manifest_syntax() {
        assert_eq!(
            name(&ev(KeyCode::Char('p'), KeyModifiers::CONTROL)).unwrap(),
            "ctrl+p"
        );
        assert_eq!(
            name(&ev(KeyCode::Char(','), KeyModifiers::CONTROL)).unwrap(),
            "ctrl+,"
        );
        assert_eq!(
            name(&ev(KeyCode::Enter, KeyModifiers::ALT)).unwrap(),
            "alt+enter"
        );
        assert_eq!(
            name(&ev(KeyCode::Enter, KeyModifiers::SHIFT)).unwrap(),
            "shift+enter"
        );
        assert_eq!(
            name(&ev(KeyCode::BackTab, KeyModifiers::NONE)).unwrap(),
            "shift+tab"
        );
        assert_eq!(
            name(&ev(KeyCode::Char('Y'), KeyModifiers::SHIFT)).unwrap(),
            "y"
        );
        assert_eq!(
            name(&ev(KeyCode::Char('1'), KeyModifiers::ALT)).unwrap(),
            "alt+1"
        );
        assert_eq!(name(&ev(KeyCode::F(1), KeyModifiers::NONE)).unwrap(), "f1");
        assert_eq!(name(&ev(KeyCode::Esc, KeyModifiers::NONE)).unwrap(), "esc");
    }

    #[test]
    fn printable_excludes_control_and_alt() {
        assert_eq!(
            printable(&ev(KeyCode::Char('a'), KeyModifiers::NONE)),
            Some('a')
        );
        assert_eq!(
            printable(&ev(KeyCode::Char('A'), KeyModifiers::SHIFT)),
            Some('A')
        );
        assert_eq!(
            printable(&ev(KeyCode::Char('a'), KeyModifiers::CONTROL)),
            None
        );
        assert_eq!(printable(&ev(KeyCode::Char('a'), KeyModifiers::ALT)), None);
    }
}
