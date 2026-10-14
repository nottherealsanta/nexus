//! Input helpers for the terminal loop: the typed-action writers, the grapheme editor key
//! mapping, menu picking, form saving and the OSC 52 base64 (split out of `main.rs`).
use crate::{bridge::Snapshot, editor::Editor};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};
use std::io::{self, Write};

pub const MAX_DRAFT: usize = 1024 * 1024;
/// Called only after modal, search and composer ownership have been checked.
/// Inspectors do not toggle the focused row's inline disclosure.
pub fn inspector_operation(operation: &Value) -> Option<Value> {
    match operation.get("kind")?.as_str()? {
        "tool_page" | "tool_toggle" | "tool_details" => {
            Some(json!({"kind":"tool_details","id":operation.get("id")?}))
        }
        "context_show" => Some(operation.clone()),
        _ => None,
    }
}

/// Single Enter sends; Shift+Enter and Ctrl+J insert a newline.
pub fn composer_submit(key: KeyEvent) -> bool {
    key.code == KeyCode::Enter
        && !key
            .modifiers
            .intersects(KeyModifiers::SHIFT | KeyModifiers::ALT | KeyModifiers::CONTROL)
}
/// The stable identity of a tool row's inline disclosure (`<call_id>:detail`, the output fold
/// and the change-block folds), or `None` for groups, thoughts, chips, links and the rest,
/// which keep their single-click behaviour. The key is the operation, not a screen cell, so
/// every wrapped line of one header counts as the same target.
pub fn tool_click_key(operation: &Value) -> Option<String> {
    if operation.get("kind")?.as_str()? != "block_toggle" {
        return None;
    }
    let id = operation.get("id")?.as_str()?;
    [":detail", ":output", ":change", ":change-output"]
        .iter()
        .any(|suffix| id.ends_with(suffix))
        .then(|| id.to_owned())
}

/// Tool-row double-click detector (plan D1 §5.1): the second click must hit the same scope
/// and operation key within `MULTI_CLICK`. Cells do not matter, so a wrapped header works.
/// Callers `reset` on any key, scroll, resize or page/session change.
#[derive(Default, Clone, Debug)]
pub struct ClickTracker {
    last: Option<(std::time::Instant, String)>,
}
impl ClickTracker {
    pub const MULTI_CLICK: std::time::Duration = std::time::Duration::from_millis(300);
    /// Record a click; true when it completes a double click (which resets the tracker).
    pub fn click(&mut self, now: std::time::Instant, scope: &str, key: &str) -> bool {
        let id = format!("{scope}\u{0}{key}");
        let hit = self.last.as_ref().is_some_and(|(t, last)| {
            *last == id && now.saturating_duration_since(*t) <= Self::MULTI_CLICK
        });
        self.last = if hit { None } else { Some((now, id)) };
        hit
    }
    pub fn reset(&mut self) {
        self.last = None;
    }
}

/// Double Ctrl+C to quit: the first press arms, a second within `WINDOW` quits.
#[derive(Default, Clone, Debug)]
pub struct DoubleCtrlC {
    armed: Option<std::time::Instant>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CtrlC {
    /// First press: show the "press again to quit" hint.
    Armed,
    Quit,
}
impl DoubleCtrlC {
    pub const WINDOW: std::time::Duration = std::time::Duration::from_secs(2);
    pub fn press(&mut self, now: std::time::Instant) -> CtrlC {
        let again = self
            .armed
            .is_some_and(|t| now.saturating_duration_since(t) <= Self::WINDOW);
        if again {
            self.armed = None;
            CtrlC::Quit
        } else {
            self.armed = Some(now);
            CtrlC::Armed
        }
    }
    /// Any other key disarms.
    pub fn disarm(&mut self) {
        self.armed = None;
    }
    /// Whether the hint should still be shown at `now`.
    pub fn is_armed(&self, now: std::time::Instant) -> bool {
        self.armed
            .is_some_and(|t| now.saturating_duration_since(t) <= Self::WINDOW)
    }
}

pub fn voice_stop(key: KeyCode) -> Value {
    json!({"type":"voice_stop","discard":false,"send":key == KeyCode::Enter})
}
pub fn send(value: Value) -> io::Result<()> {
    let started = std::time::Instant::now();
    let mut out = io::stdout().lock();
    writeln!(out, "{}", value)?;
    let result = out.flush();
    crate::trace::stall("send to host (pipe blocked)", started.elapsed());
    result
}
pub fn action(kind: &str, text: &str) -> io::Result<()> {
    send(json!({"type":kind,"text":text}))
}
pub fn edit(editor: &mut Editor, key: KeyEvent, multiline: bool) {
    let shift = key.modifiers.contains(KeyModifiers::SHIFT);
    let control = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    // Option (Alt) and Control both move by word, as in other terminals; Shift extends.
    let by_word = control || alt;
    match key.code {
        KeyCode::Char(c @ ('b' | 'f')) if alt && !control => {
            editor.select_move(shift);
            editor.word(c == 'f')
        }
        KeyCode::Left => {
            editor.select_move(shift);
            if by_word {
                editor.word(false)
            } else {
                editor.left()
            }
        }
        KeyCode::Right => {
            editor.select_move(shift);
            if by_word {
                editor.word(true)
            } else {
                editor.right()
            }
        }
        KeyCode::Home => {
            editor.select_move(shift);
            if control {
                editor.cursor = 0
            } else {
                editor.home()
            }
        }
        KeyCode::End => {
            editor.select_move(shift);
            if control {
                editor.cursor = editor.text.len()
            } else {
                editor.end()
            }
        }
        KeyCode::Up if multiline => {
            editor.select_move(shift);
            editor.vertical(false)
        }
        KeyCode::Down if multiline => {
            editor.select_move(shift);
            editor.vertical(true)
        }
        KeyCode::Backspace => editor.backspace(),
        KeyCode::Delete => editor.delete(),
        // Terminals send Cmd+Left / Cmd+Right as Ctrl+A / Ctrl+E: line start / end.
        KeyCode::Char('a') if control => {
            editor.select_move(shift);
            editor.home()
        }
        KeyCode::Char('e') if control => {
            editor.select_move(shift);
            editor.end()
        }
        KeyCode::Char('z') if control => {
            if shift {
                editor.redo()
            } else {
                editor.undo()
            }
        }
        KeyCode::Char('y') if control => editor.redo(),
        KeyCode::Enter if multiline => editor.insert("\n"),
        KeyCode::Char('j') if control && multiline => editor.insert("\n"),
        KeyCode::Tab if multiline => editor.insert("    "),
        KeyCode::Char(c) if !control && editor.text.len() < MAX_DRAFT => {
            editor.insert(&c.to_string())
        }
        _ => {}
    }
}
pub fn pick(s: &Snapshot, index: usize, filter: &str) -> io::Result<()> {
    if let Some(item) = s.items.iter().filter(|row| row.matches(filter)).nth(index) {
        if let Some(operation) = &item.operation {
            send(json!({"type":"operation","operation":operation,"generation":s.generation}))
        } else {
            send(json!({"type":"pick","text":item.command,"generation":s.generation}))
        }
    } else {
        Ok(())
    }
}
pub fn toggle(s: &Snapshot, index: usize, filter: &str) -> io::Result<()> {
    if let Some(item) = s
        .items
        .iter()
        .filter(|row| row.matches(filter))
        .nth(index)
        .filter(|item| item.toggle_operation.is_some() && !item.toggle_locked)
    {
        send(
            json!({"type":"operation","operation":item.toggle_operation.as_ref().unwrap(),"generation":s.generation}),
        )
    } else {
        Ok(())
    }
}
pub fn save(s: &Snapshot, editor: &Editor, revision: u64) -> io::Result<()> {
    if let Some(form) = &s.form {
        send(
            json!({"type":"save","form":form.id,"body":editor.text,"revision":revision,"generation":s.generation}),
        )
    } else {
        Ok(())
    }
}
pub fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}
#[cfg(test)]
mod base64_tests {
    #[test]
    fn voice_enter_sends_escape_keeps_and_other_keys_only_stop() {
        use crossterm::event::KeyCode;
        assert_eq!(
            super::voice_stop(KeyCode::Enter),
            serde_json::json!({"type":"voice_stop","discard":false,"send":true})
        );
        assert_eq!(
            super::voice_stop(KeyCode::Esc),
            serde_json::json!({"type":"voice_stop","discard":false,"send":false})
        );
        assert_eq!(
            super::voice_stop(KeyCode::Char('a')),
            serde_json::json!({"type":"voice_stop","discard":false,"send":false})
        );
    }

    #[test]
    fn matches_the_standard_alphabet_with_padding() {
        assert_eq!(super::base64(b""), "");
        assert_eq!(super::base64(b"f"), "Zg==");
        assert_eq!(super::base64(b"fo"), "Zm8=");
        assert_eq!(super::base64(b"foo"), "Zm9v");
        assert_eq!(super::base64("héllo".as_bytes()), "aMOpbGxv");
    }
}

#[cfg(test)]
mod edit_tests {
    use super::*;
    #[test]
    fn click_tracker_uses_stable_keys_not_cells() {
        use std::time::{Duration, Instant};
        let t = Instant::now();
        let ms = Duration::from_millis;
        let mut d = ClickTracker::default();
        // Same operation, any cell (a wrapped header), inside 300 ms.
        assert!(!d.click(t, "s", "a:detail"));
        assert!(d.click(t + ms(299), "s", "a:detail"));
        // A double click resets: the third click starts over.
        assert!(!d.click(t + ms(310), "s", "a:detail"));
        // Too slow.
        assert!(!d.click(t + ms(700), "s", "a:detail"));
        // Different operation or scope never pairs.
        assert!(!d.click(t + ms(750), "s", "b:detail"));
        assert!(!d.click(t + ms(800), "other", "b:detail"));
        // reset cancels a pending click.
        assert!(!d.click(t + ms(900), "s", "a:detail"));
        d.reset();
        assert!(!d.click(t + ms(950), "s", "a:detail"));
    }
    #[test]
    fn tool_click_keys_cover_tool_folds_only() {
        assert_eq!(
            tool_click_key(&json!({"kind":"block_toggle","id":"c1:detail"})),
            Some("c1:detail".into())
        );
        assert!(tool_click_key(&json!({"kind":"block_toggle","id":"c1:change-output"})).is_some());
        assert!(tool_click_key(&json!({"kind":"block_toggle","id":"group-1"})).is_none());
        assert!(tool_click_key(&json!({"kind":"turn_toggle","id":"c1:detail"})).is_none());
        assert!(tool_click_key(&json!({"kind":"agent_page","id":"c1"})).is_none());
    }
    #[test]
    fn double_ctrl_c() {
        use std::time::{Duration, Instant};
        let t = Instant::now();
        let mut c = DoubleCtrlC::default();
        assert_eq!(c.press(t), CtrlC::Armed);
        assert!(c.is_armed(t + Duration::from_secs(1)));
        assert_eq!(c.press(t + Duration::from_secs(1)), CtrlC::Quit);
        assert_eq!(c.press(t), CtrlC::Armed);
        c.disarm();
        assert_eq!(c.press(t + Duration::from_secs(1)), CtrlC::Armed);
        assert_eq!(c.press(t + Duration::from_secs(9)), CtrlC::Armed);
    }
    #[test]
    fn inspector_is_a_separate_action() {
        assert_eq!(
            inspector_operation(&json!({"kind":"tool_toggle","id":"call"})),
            Some(json!({"kind":"tool_details","id":"call"}))
        );
        assert_eq!(
            inspector_operation(&json!({"kind":"context_show","key":"tools"})),
            Some(json!({"kind":"context_show","key":"tools"}))
        );
        assert!(inspector_operation(&json!({"kind":"turn_toggle"})).is_none());
    }
    #[test]
    fn plain_enter_sends_modified_enter_does_not() {
        assert!(composer_submit(KeyEvent::new(
            KeyCode::Enter,
            KeyModifiers::NONE
        )));
        assert!(!composer_submit(KeyEvent::new(
            KeyCode::Enter,
            KeyModifiers::SHIFT
        )));
        assert!(!composer_submit(KeyEvent::new(
            KeyCode::Char('j'),
            KeyModifiers::CONTROL
        )));
    }
    #[test]
    fn shift_with_option_or_control_selects_by_word() {
        for modifier in [KeyModifiers::ALT, KeyModifiers::CONTROL] {
            let mut editor = Editor::default();
            editor.insert("one two three");
            let key = KeyEvent::new(KeyCode::Left, KeyModifiers::SHIFT | modifier);
            edit(&mut editor, key, true);
            assert_eq!(editor.selected(), "three");
            edit(&mut editor, key, true);
            assert_eq!(editor.selected(), "two three");
        }
    }
}
