//! Jump-to-latest pill: shown on the transcript's last row while the view is scrolled up with
//! content below it. Clicking it (or Ctrl+End) returns to the bottom and follows again.

/// The pill text and whether it announces new content (drawn in the accent colour), or `None`
/// at the bottom. `away_len` is the transcript length when the user scrolled away.
pub fn label(below: usize, away_len: usize, len: usize) -> Option<(String, bool)> {
    if below == 0 {
        return None;
    }
    if len > away_len {
        return Some(("↓ New messages · Ctrl+End".into(), true));
    }
    let noun = if below == 1 { "line" } else { "lines" };
    Some((format!("↓ {below} more {noun} · Ctrl+End to jump"), false))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hidden_at_the_bottom_and_counts_lines_when_scrolled_up() {
        assert_eq!(label(0, 10, 10), None);
        assert_eq!(label(1, 10, 10).unwrap(), ("↓ 1 more line · Ctrl+End to jump".into(), false));
        assert_eq!(label(7, 10, 10).unwrap().0, "↓ 7 more lines · Ctrl+End to jump");
    }

    #[test]
    fn new_content_while_away_switches_to_the_new_messages_state() {
        assert_eq!(label(7, 10, 14).unwrap(), ("↓ New messages · Ctrl+End".into(), true));
        assert_eq!(label(0, 10, 14), None);
    }
}
