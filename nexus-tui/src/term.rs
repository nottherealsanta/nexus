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
    disable_raw_mode, enable_raw_mode, supports_keyboard_enhancement, EnterAlternateScreen,
    LeaveAlternateScreen,
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

/// Restores the terminal when dropped, including on early returns.
pub struct Guard;

impl Drop for Guard {
    fn drop(&mut self) {
        leave();
    }
}
