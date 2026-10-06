//! `cargo run` opens the viewer; see README.md for the commands.
use nexus_mockups::app::App;
use nexus_mockups::shoot;
use ratatui::crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyEventKind, MouseButton, MouseEventKind},
    execute,
};
use std::{path::PathBuf, time::{Duration, Instant}};

fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let flag = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    match args.first().map(|s| s.as_str()) {
        Some("list") => {
            for (i, s) in App::new().screens.iter().enumerate() {
                println!("{:>2}  {:<22} {}  [{}]", i + 1, s.key, s.title, s.states.join(" | "));
            }
            Ok(())
        }
        Some("shoot") => {
            let dir = PathBuf::from(flag("--out").unwrap_or_else(|| "shots".into()));
            let sizes: Vec<usize> = match flag("--size").as_deref() {
                Some("80x24") => vec![1],
                Some("120x36") => vec![2],
                Some("200x50") => vec![3],
                _ => vec![1, 2],
            };
            let themes: Vec<usize> = match flag("--theme").as_deref() {
                Some("dark") => vec![0],
                Some("light") => vec![1],
                Some("mono") => vec![2],
                _ => vec![0, 1, 2],
            };
            let n = shoot::shoot(&dir, flag("--screen").as_deref(), &sizes, &themes)?;
            println!("wrote {n} files to {} (open {}/index.html)", dir.display(), dir.display());
            Ok(())
        }
        Some("screen") => {
            // Print one screen as text: `screen settings-models [--state N] [--theme dark] [--size 80x24]`
            let key = args.get(1).cloned().unwrap_or_default();
            let state = flag("--state").and_then(|s| s.parse().ok()).unwrap_or(0);
            let theme = match flag("--theme").as_deref() { Some("light") => 1, Some("mono") => 2, _ => 0 };
            let size = match flag("--size").as_deref() { Some("80x24") => 1, Some("200x50") => 3, _ => 2 };
            match shoot::render(&key, state, theme, size, false) {
                Some((buf, area)) => {
                    print!("{}", shoot::to_text(&buf, area));
                    Ok(())
                }
                None => {
                    eprintln!("unknown screen {key}; try `list`");
                    std::process::exit(2)
                }
            }
        }
        Some("--help") | Some("-h") | Some("help") => {
            println!("nexus-mockups [list | screen <key> [--state N --theme T --size WxH] | shoot [--screen K --theme T --size S --out DIR]]\nNo arguments opens the interactive viewer (F1 for keys).");
            Ok(())
        }
        _ => run(args.first().cloned()),
    }
}

fn run(start: Option<String>) -> std::io::Result<()> {
    let mut term = ratatui::init();
    execute!(std::io::stdout(), EnableMouseCapture)?;
    let mut app = App::new();
    if let Some(k) = start {
        app.select(&k, 0);
    }
    let result = (|| -> std::io::Result<()> {
        while !app.quit {
            let now = Instant::now();
            app.tick(now);
            term.draw(|f| {
                let area = f.area();
                app.render(f.buffer_mut(), area, now);
            })?;
            if event::poll(Duration::from_millis(100))? {
                match event::read()? {
                    Event::Key(k) if k.kind != KeyEventKind::Release => app.key(k),
                    Event::Mouse(m) => match m.kind {
                        MouseEventKind::Down(MouseButton::Left) => app.click(m.column, m.row),
                        MouseEventKind::Moved => {
                            app.hovered_toast = app.hits.at(m.column, m.row).and_then(|(id, _)| id.strip_prefix("toast:").and_then(|n| n.parse().ok()));
                        }
                        _ => {}
                    },
                    _ => {}
                }
            }
        }
        Ok(())
    })();
    let _ = execute!(std::io::stdout(), DisableMouseCapture);
    ratatui::restore();
    result
}
