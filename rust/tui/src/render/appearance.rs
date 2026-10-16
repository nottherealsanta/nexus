//! Local Appearance checkboxes shown under the Settings → Appearance page. Pure state helpers
//! (cursor, keys, clicks) plus the row painter; persistence stays in `config`.
use crate::config::{self, Appearance};
use crate::render::Palette;
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};

/// Rows painted (one per `config::checkboxes` entry).
pub const ROWS: u16 = 2;
/// Direct-toggle accelerators shown beside each row (Alt+A / Alt+M).
const KEYS: [&str; 2] = ["Alt+A", "Alt+M"];

/// What a key does to the checkbox group.
#[derive(Debug, PartialEq, Eq)]
pub enum Act {
    Pass,
    /// Cursor moved; nothing to persist.
    Move(usize),
    /// Toggle the checkbox with this config key.
    Toggle(&'static str),
}

fn key_at(row: usize) -> Option<&'static str> {
    config::checkboxes(&Appearance::default())
        .get(row)
        .map(|c| c.0)
}

/// Interpret a key. `plain` is true when no Alt-style accelerator is involved.
pub fn key(cursor: usize, code: crossterm::event::KeyCode, alt: bool) -> Act {
    use crossterm::event::KeyCode::*;
    let last = usize::from(ROWS) - 1;
    match code {
        Char('a') if alt => Act::Toggle("animate_activity"),
        Char('m') if alt => Act::Toggle("mono"),
        _ if alt => Act::Pass,
        Up => Act::Move(cursor.saturating_sub(1)),
        Down => Act::Move((cursor + 1).min(last)),
        Char(' ') | Enter => key_at(cursor).map_or(Act::Pass, Act::Toggle),
        _ => Act::Pass,
    }
}

/// Row index under a click, if inside `rows` (the region recorded by the last draw).
pub fn hit(rows: Rect, x: u16, y: u16) -> Option<usize> {
    if rows.width == 0 || rows.height == 0 || !rows.contains((x, y).into()) {
        return None;
    }
    let row = usize::from(y - rows.y);
    (row < usize::from(ROWS)).then_some(row)
}

/// Flip `name`, returning the derived `(animate, mono)` flags to store in the render cache.
pub fn apply(a: &mut Appearance, name: &str) -> Option<(bool, bool)> {
    config::toggle(a, name)?;
    Some((a.enabled(), a.mono_enabled()))
}

pub fn draw(frame: &mut Frame, rows: Rect, a: &Appearance, cursor: usize, p: &Palette) {
    let lines: Vec<Line<'static>> = config::checkboxes(a)
        .iter()
        .zip(KEYS)
        .enumerate()
        .map(|(i, ((_, label, on), keys))| {
            let mut text = Style::default().fg(p.text);
            if i == cursor {
                text = text.add_modifier(Modifier::BOLD);
            }
            Line::from(vec![
                Span::styled(if i == cursor { "▸ " } else { "  " }, text),
                Span::styled(format!("[{}] {label}", if *on { "x" } else { " " }), text),
                Span::styled(format!("  {keys}"), Style::default().fg(p.quiet)),
            ])
        })
        .collect();
    frame.render_widget(
        Paragraph::new(lines).style(Style::default().bg(p.dialog)),
        rows,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyCode;
    use ratatui::{backend::TestBackend, Terminal};

    #[test]
    fn keys_move_and_toggle() {
        assert_eq!(key(0, KeyCode::Down, false), Act::Move(1));
        assert_eq!(key(1, KeyCode::Down, false), Act::Move(1));
        assert_eq!(key(0, KeyCode::Up, false), Act::Move(0));
        assert_eq!(
            key(0, KeyCode::Char(' '), false),
            Act::Toggle("animate_activity")
        );
        assert_eq!(key(1, KeyCode::Enter, false), Act::Toggle("mono"));
        assert_eq!(key(0, KeyCode::Char('m'), true), Act::Toggle("mono"));
        assert_eq!(key(0, KeyCode::Char('x'), false), Act::Pass);
        assert_eq!(key(0, KeyCode::Down, true), Act::Pass);
    }

    #[test]
    fn click_rows() {
        let rows = Rect::new(10, 20, 30, ROWS);
        assert_eq!(hit(rows, 10, 20), Some(0));
        assert_eq!(hit(rows, 39, 21), Some(1));
        assert_eq!(hit(rows, 40, 21), None);
        assert_eq!(hit(rows, 10, 22), None);
        assert_eq!(hit(Rect::default(), 0, 0), None);
    }

    #[test]
    fn apply_updates_flags() {
        let mut a = Appearance::default();
        let (_, mono) = apply(&mut a, "mono").unwrap();
        assert!(mono);
        apply(&mut a, "animate_activity").unwrap();
        assert_eq!(a.animate_activity, Some(false));
        assert!(!a.enabled());
        assert!(apply(&mut a, "nope").is_none());
    }

    #[test]
    fn draw_reflects_state() {
        let p = crate::render::Palette::new(false);
        let mut a = Appearance::default();
        a.animate_activity = Some(false);
        a.mono = Some(true);
        let mut term = Terminal::new(TestBackend::new(40, 2)).unwrap();
        term.draw(|f| draw(f, Rect::new(0, 0, 40, 2), &a, 1, &p))
            .unwrap();
        let text: Vec<String> = (0..2)
            .map(|y| {
                (0..40)
                    .map(|x| term.backend().buffer()[(x, y)].symbol().to_string())
                    .collect()
            })
            .collect();
        assert!(text[0].contains("[ ] Animate activity"), "{}", text[0]);
        assert!(text[1].contains("▸ [x] Monochrome"), "{}", text[1]);
    }
}
