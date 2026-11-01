//! A small full-screen editor for checking the vendored editor by hand.
//!
//! `cargo run --release --example editor_demo -- <file>`
//!
//! Esc quits, Ctrl+S writes the file back. This example is a development aid;
//! the application's editor lives in `src/code_editor.rs`.

use std::io;

use crossterm::{
    event::{self, Event, KeyCode, KeyModifiers},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use nexus_tui::code_editor::CodeEditor;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Layout};
use ratatui::style::{Color, Style};
use ratatui::widgets::Block;
use ratatui::Terminal;

fn main() -> io::Result<()> {
    let path = std::env::args()
        .nth(1)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "usage: editor_demo <file>"))?;
    let text = std::fs::read_to_string(&path)?;
    let mut editor = CodeEditor::new(&path, &text, false).map_err(io::Error::other)?;

    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout))?;

    loop {
        terminal.draw(|frame| {
            let [body, status] =
                Layout::vertical([Constraint::Min(0), Constraint::Length(1)]).areas(frame.area());
            frame.render_widget(Block::default(), frame.area());
            editor.render(frame, body);
            let modified = editor.text() != text;
            frame.render_widget(
                ratatui::widgets::Paragraph::new(format!(
                    " {}  {}  ·  {}  ·  esc quit · ctrl+s write",
                    path,
                    editor.language(),
                    if modified { "modified" } else { "saved" }
                ))
                .style(Style::default().fg(Color::Gray)),
                status,
            );
        })?;

        match event::read()? {
            Event::Key(key) if key.kind == event::KeyEventKind::Press => match key.code {
                KeyCode::Esc => break,
                KeyCode::Char('s') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    std::fs::write(&path, editor.text())?;
                }
                _ => {
                    let area = terminal.size()?;
                    let area =
                        ratatui::layout::Rect::new(0, 0, area.width, area.height.saturating_sub(1));
                    editor.key(key, area);
                }
            },
            Event::Mouse(mouse) => {
                let area = terminal.size()?;
                let area =
                    ratatui::layout::Rect::new(0, 0, area.width, area.height.saturating_sub(1));
                editor.mouse(mouse, area);
            }
            _ => {}
        }
    }

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}
