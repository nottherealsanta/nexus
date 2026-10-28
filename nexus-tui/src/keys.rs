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

/// Parse a key spec such as `ctrl+p down enter /mo` into key presses, one group
/// per space-separated token. A token is a key name in the manifest's syntax
/// (`ctrl+,`, `alt+2`, `esc`, `space`, `f1`), else literal text typed one
/// character at a time. Used by `--dump-frame --keys`.
pub fn parse_spec(spec: &str) -> Result<Vec<Vec<KeyEvent>>, String> {
    spec.split_whitespace().map(parse_token).collect()
}

fn named(key: &str) -> Option<KeyCode> {
    Some(match key {
        "space" => KeyCode::Char(' '),
        "enter" => KeyCode::Enter,
        "esc" => KeyCode::Esc,
        "tab" => KeyCode::Tab,
        "backspace" => KeyCode::Backspace,
        "delete" => KeyCode::Delete,
        "up" => KeyCode::Up,
        "down" => KeyCode::Down,
        "left" => KeyCode::Left,
        "right" => KeyCode::Right,
        "home" => KeyCode::Home,
        "end" => KeyCode::End,
        "pageup" => KeyCode::PageUp,
        "pagedown" => KeyCode::PageDown,
        _ => {
            let n = key.strip_prefix('f')?.parse::<u8>().ok()?;
            (1..=12).contains(&n).then_some(KeyCode::F(n))?
        }
    })
}

fn parse_token(token: &str) -> Result<Vec<KeyEvent>, String> {
    let mut mods = KeyModifiers::NONE;
    let mut rest = token;
    for (prefix, m) in [
        ("ctrl+", KeyModifiers::CONTROL),
        ("alt+", KeyModifiers::ALT),
        ("shift+", KeyModifiers::SHIFT),
        ("cmd+", KeyModifiers::SUPER),
    ] {
        if let Some(r) = rest.strip_prefix(prefix).filter(|r| !r.is_empty()) {
            mods |= m;
            rest = r;
        }
    }
    let mut chars = rest.chars();
    let single = match (chars.next(), chars.next()) {
        (Some(c), None) => Some(KeyCode::Char(c)),
        _ => None,
    };
    if let Some(code) = named(rest).or(single) {
        return Ok(vec![KeyEvent::new(code, mods)]);
    }
    if mods != KeyModifiers::NONE {
        return Err(format!("unknown key {token}"));
    }
    Ok(token
        .chars()
        .map(|c| KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE))
        .collect())
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

    #[test]
    fn spec_tokens_are_keys_or_text() {
        let spec = parse_spec("ctrl+p esc alt+2 ctrl+, /mo f1 shift+tab").unwrap();
        let names: Vec<Vec<String>> = spec
            .iter()
            .map(|t| t.iter().map(|k| name(k).unwrap()).collect())
            .collect();
        assert_eq!(
            names,
            [
                vec!["ctrl+p"],
                vec!["esc"],
                vec!["alt+2"],
                vec!["ctrl+,"],
                vec!["/", "m", "o"],
                vec!["f1"],
                vec!["shift+tab"],
            ]
        );
        assert!(parse_spec("").unwrap().is_empty());
        assert!(parse_spec("ctrl+nope").is_err());
    }
}
