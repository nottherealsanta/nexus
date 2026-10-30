//! Terminal hygiene: raw mode, alternate screen, mouse, bracketed paste and the
//! kitty keyboard protocol where available; always restored on exit, error,
//! panic and suspend (Ctrl+Z).

use std::io::{self, Stdout, Write};
use std::sync::atomic::{AtomicBool, Ordering};

use crossterm::event::{
    DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
    KeyboardEnhancementFlags, PopKeyboardEnhancementFlags, PushKeyboardEnhancementFlags,
};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, supports_keyboard_enhancement, BeginSynchronizedUpdate,
    EndSynchronizedUpdate, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::{cursor, execute};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

pub type Term = Terminal<CrosstermBackend<Stdout>>;

static ACTIVE: AtomicBool = AtomicBool::new(false);
static ENHANCED: AtomicBool = AtomicBool::new(false);

fn setup() -> io::Result<()> {
    enable_raw_mode()?;
    let mut out = io::stdout();
    execute!(
        out,
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableBracketedPaste
    )?;
    // Lets shift+enter and ctrl+enter be told apart where the terminal can.
    if supports_keyboard_enhancement().unwrap_or(false)
        && execute!(
            out,
            PushKeyboardEnhancementFlags(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES)
        )
        .is_ok()
    {
        ENHANCED.store(true, Ordering::SeqCst);
    }
    ACTIVE.store(true, Ordering::SeqCst);
    Ok(())
}

/// Enter the TUI screen and install the panic hook that undoes it.
pub fn enter() -> io::Result<Term> {
    install_panic_hook();
    setup()?;
    Terminal::new(CrosstermBackend::new(io::stdout()))
}

/// Present a frame atomically in terminals that support synchronized updates.
pub fn synchronized_update<W: Write>(
    out: &mut W,
    draw: impl FnOnce() -> io::Result<()>,
) -> io::Result<()> {
    execute!(out, BeginSynchronizedUpdate)?;
    let result = draw();
    let end = execute!(out, EndSynchronizedUpdate);
    result.and(end)
}

/// Restore the terminal. Idempotent and best effort: it is called from the
/// panic hook, error paths and `Drop`.
pub fn leave() {
    if !ACTIVE.swap(false, Ordering::SeqCst) {
        return;
    }
    let mut out = io::stdout();
    if ENHANCED.swap(false, Ordering::SeqCst) {
        let _ = execute!(out, PopKeyboardEnhancementFlags);
    }
    let _ = execute!(
        out,
        EndSynchronizedUpdate,
        DisableBracketedPaste,
        DisableMouseCapture,
        LeaveAlternateScreen,
        cursor::Show
    );
    let _ = disable_raw_mode();
    let _ = out.flush();
}

fn install_panic_hook() {
    let prev = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        leave();
        prev(info);
    }));
}

/// Ctrl+Z: give the terminal back, stop the process, and take the terminal
/// again when the shell continues it.
pub fn suspend(term: &mut Term) -> io::Result<()> {
    leave();
    // SAFETY: raising a signal on ourselves; the default SIGTSTP action stops
    // the process until the shell sends SIGCONT, then this call returns.
    unsafe {
        libc::raise(libc::SIGTSTP);
    }
    setup()?;
    term.clear()
}

/// Run `$VISUAL`/`$EDITOR` on one path with the terminal returned to the shell,
/// then take the TUI screen back. A failed launch is not fatal.
pub fn run_external(term: &mut Term, editor: &str, path: &str) -> io::Result<()> {
    let mut words = editor.split_whitespace();
    let program = words.next().unwrap_or("vi");
    let args: Vec<&str> = words.collect();
    leave();
    let _ = std::process::Command::new(program)
        .args(args)
        .arg(path)
        .status();
    setup()?;
    term.clear()
}

/// Ask the terminal for an OS notification (best effort, between frames).
pub fn notify(text: &str) {
    let program = std::env::var("TERM_PROGRAM").unwrap_or_default();
    let term = std::env::var("TERM").unwrap_or_default();
    let mut out = io::stdout();
    let _ = out.write_all(notify_sequence(text, &program, &term).as_bytes());
    let _ = out.flush();
}

/// The notification escape sequence for `text`: OSC 777 (`notify;Nexus;text`)
/// for terminals that prefer it, OSC 9 (`text`) otherwise. Control characters
/// are dropped; `;` would end an OSC 777 field, so it becomes `,` there.
pub fn notify_sequence(text: &str, term_program: &str, term: &str) -> String {
    let clean: String = text.chars().filter(|c| !c.is_control()).collect();
    let hint = format!("{term_program} {term}").to_ascii_lowercase();
    let osc777 = ["foot", "kitty", "ghostty", "wezterm"]
        .iter()
        .any(|name| hint.contains(name));
    if osc777 {
        format!("\x1b]777;notify;Nexus;{}\x07", clean.replace(';', ","))
    } else {
        format!("\x1b]9;{clean}\x07")
    }
}

/// Restores the terminal when dropped, including on early returns.
pub struct Guard;

impl Drop for Guard {
    fn drop(&mut self) {
        leave();
    }
}

#[cfg(test)]
mod tests {
    use super::{notify_sequence, synchronized_update};

    #[test]
    fn synchronized_update_ends_even_on_draw_error() {
        let mut output = Vec::new();
        let result = synchronized_update(&mut output, || Err(std::io::Error::other("draw")));
        assert!(result.is_err());
        assert_eq!(output, b"\x1b[?2026h\x1b[?2026l");
    }

    #[test]
    fn osc9_is_the_default() {
        assert_eq!(
            notify_sequence("Nexus: s needs approval", "", "xterm-256color"),
            "\x1b]9;Nexus: s needs approval\x07"
        );
        assert_eq!(
            notify_sequence("x", "iTerm.app", "xterm-256color"),
            "\x1b]9;x\x07"
        );
    }

    #[test]
    fn osc777_for_terminals_that_prefer_it() {
        assert_eq!(
            notify_sequence("done", "WezTerm", "xterm-256color"),
            "\x1b]777;notify;Nexus;done\x07"
        );
        assert_eq!(
            notify_sequence("done", "", "foot"),
            "\x1b]777;notify;Nexus;done\x07"
        );
        assert_eq!(
            notify_sequence("done", "", "xterm-kitty"),
            "\x1b]777;notify;Nexus;done\x07"
        );
        assert_eq!(
            notify_sequence("done", "ghostty", "xterm-ghostty"),
            "\x1b]777;notify;Nexus;done\x07"
        );
    }

    #[test]
    fn control_characters_cannot_end_the_sequence() {
        assert_eq!(notify_sequence("a\x07b\x1b\nc", "", ""), "\x1b]9;abc\x07");
        assert_eq!(
            notify_sequence("a;b\x07", "foot", ""),
            "\x1b]777;notify;Nexus;a,b\x07"
        );
    }
}
