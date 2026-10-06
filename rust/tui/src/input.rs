//! Input helpers for the terminal loop: the typed-action writers, the grapheme editor key
//! mapping, menu picking, form saving and the OSC 52 base64 (split out of `main.rs`).
use crate::{bridge::Snapshot, editor::Editor};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};
use std::io::{self, Write};

pub const MAX_DRAFT: usize = 1024 * 1024;
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
        KeyCode::Char('a') if control => {
            editor.anchor = Some(0);
            editor.cursor = editor.text.len()
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
