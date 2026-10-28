//! `nexus-tui`: the keyboard-first terminal client for Nexus Core.
//!
//! `nexus-tui`                  run the TUI
//! `nexus-tui --once`           connect, print state once live, exit (no UI)
//! `nexus-tui --dump-frame WxH` connect, render the real shell to text, exit
//! See `--help` for the display options.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use crossterm::event::{Event, KeyEventKind};
use nexus_tui::actions::{self, ActionDone};
use nexus_tui::app::{App, Effect};
use nexus_tui::discovery;
use nexus_tui::prefs;
use nexus_tui::render;
use nexus_tui::state::{AppState, ConnectionState};
use nexus_tui::sync::{self, SyncConfig, SyncMsg};
use nexus_tui::term;
use nexus_tui::text::base64;
use nexus_tui::theme::{Mode, Options, Theme, Tier};
use tokio::sync::mpsc;

const HELP: &str = "nexus-tui - terminal client for Nexus Core

USAGE: nexus-tui [options]

Display options (flags override the environment):
  --dark | --light       colour mode (default: COLORFGBG, else dark)
  --mono                 monochrome (also NO_COLOR or TERM=dumb)
  --256 | --16           colour tier when COLORTERM does not say truecolor
  --ascii                ASCII glyphs (also NEXUS10_ASCII=1 or a non-UTF-8 locale)
  --reduced-motion       no spinner animation (also NEXUS10_REDUCED_MOTION=1)

Layout and appearance changes are remembered in $NEXUS10_HOME/tui.json;
flags and the environment override it for the run.

Other:
  --once                 print connection state and sessions once live, then exit
  --dump-frame W,HxW,H   render the shell as text at these sizes, then exit
                         (e.g. 80x24,120x36); add --open first to select the
                         most recent session, --out DIR to write files
  --help
";

struct Args {
    opts: Options,
    over: prefs::Overrides,
    once: bool,
    dump: Vec<(u16, u16)>,
    out: Option<PathBuf>,
    open_first: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut opts = Options::from_env();
    let mut a = Args {
        opts,
        over: prefs::Overrides::default(),
        once: false,
        dump: Vec::new(),
        out: None,
        open_first: false,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--help" | "-h" => {
                print!("{HELP}");
                std::process::exit(0);
            }
            "--dark" => {
                opts.mode = Mode::Dark;
                a.over.mode = true;
            }
            "--light" => {
                opts.mode = Mode::Light;
                a.over.mode = true;
            }
            "--mono" => {
                opts.tier = Tier::Mono;
                a.over.tier = true;
            }
            "--256" => {
                opts.tier = Tier::Ansi256;
                a.over.tier = true;
            }
            "--16" => {
                opts.tier = Tier::Ansi16;
                a.over.tier = true;
            }
            "--ascii" => opts.ascii = true,
            "--reduced-motion" => opts.reduced_motion = true,
            "--once" => a.once = true,
            "--open" => {
                if it.next().as_deref() != Some("first") {
                    return Err("--open takes: first".into());
                }
                a.open_first = true;
            }
            "--out" => a.out = Some(PathBuf::from(it.next().ok_or("--out needs a directory")?)),
            "--dump-frame" => {
                let v = it.next().ok_or("--dump-frame needs sizes such as 80x24")?;
                for s in v.split(',') {
                    let (w, h) = s.split_once('x').ok_or_else(|| format!("bad size {s}"))?;
                    a.dump.push((
                        w.parse().map_err(|_| format!("bad size {s}"))?,
                        h.parse().map_err(|_| format!("bad size {s}"))?,
                    ));
                }
            }
            other => return Err(format!("unknown option {other}; see --help")),
        }
    }
    a.opts = opts;
    Ok(a)
}

#[tokio::main]
async fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("nexus-tui: {e}");
            std::process::exit(2);
        }
    };
    let home = match discovery::home() {
        Ok(h) => h,
        Err(e) => {
            eprintln!("nexus-tui: {e}");
            std::process::exit(2);
        }
    };
    let mut cfg = SyncConfig::new(home.clone());
    cfg.bootstrap = std::env::var_os("NEXUS_TUI_NO_START").is_none();
    let code = if args.once {
        once(cfg).await
    } else if !args.dump.is_empty() {
        dump(cfg, &args).await
    } else {
        match run(cfg, home, args.opts, args.over).await {
            Ok(()) => 0,
            Err(e) => {
                term::leave();
                eprintln!("nexus-tui: {e}");
                1
            }
        }
    };
    std::process::exit(code);
}

// ---- interactive -------------------------------------------------------------

async fn run(
    cfg: SyncConfig,
    home: PathBuf,
    opts: Options,
    over: prefs::Overrides,
) -> std::io::Result<()> {
    let mut terminal = term::enter()?;
    let _guard = term::Guard;
    let size = terminal.size()?;
    let mut app = App::new(AppState::new(), Theme::new(opts), (size.width, size.height));
    app.apply_prefs(&home, prefs::load(&home), over);

    let (ev_tx, mut ev_rx) = mpsc::unbounded_channel::<Event>();
    std::thread::spawn(move || {
        while let Ok(ev) = crossterm::event::read() {
            if ev_tx.send(ev).is_err() {
                break;
            }
        }
    });
    let mut handle = sync::spawn(cfg);
    let (done_tx, mut done_rx) = mpsc::unbounded_channel::<ActionDone>();
    let mut tick = tokio::time::interval(Duration::from_millis(125));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    let mut dirty = true;
    'main: loop {
        if dirty {
            terminal.draw(|f| render::render(f, &mut app))?;
        }
        dirty = true;
        tokio::select! {
            ev = ev_rx.recv() => match ev {
                Some(Event::Key(k)) if k.kind != KeyEventKind::Release => app.handle_key(k),
                Some(Event::Mouse(m)) => app.handle_mouse(m),
                Some(Event::Paste(s)) => app.handle_paste(&s),
                Some(Event::Resize(w, h)) => app.set_size(w, h),
                Some(_) => {}
                None => break 'main,
            },
            msg = handle.messages.recv() => match msg {
                Some(m) => app.apply_sync(m),
                None => break 'main,
            },
            Some(done) = done_rx.recv() => app.apply_done(done),
            _ = tick.tick() => dirty = app.tick(Instant::now()),
        }
        app.persist_prefs();
        for effect in std::mem::take(&mut app.effects) {
            match effect {
                Effect::Quit => break 'main,
                Effect::Suspend => {
                    term::suspend(&mut terminal)?;
                    app.set_size(terminal.size()?.width, terminal.size()?.height);
                }
                Effect::OpenEditor(path) => {
                    let editor = std::env::var("VISUAL")
                        .ok()
                        .filter(|value| !value.is_empty())
                        .or_else(|| {
                            std::env::var("EDITOR")
                                .ok()
                                .filter(|value| !value.is_empty())
                        })
                        .unwrap_or_else(|| "vi".into());
                    term::run_external(&mut terminal, &editor, &path)?;
                    app.set_size(terminal.size()?.width, terminal.size()?.height);
                }
                Effect::Sync(c) => {
                    let _ = handle.commands.send(c);
                }
                Effect::Api(call) => {
                    let api = actions::api_for(&home);
                    let tx = done_tx.clone();
                    tokio::spawn(async move {
                        let _ = tx.send(actions::run(api, call).await);
                    });
                }
                Effect::Copy(text) => {
                    // OSC 52: ask the terminal to put the text on the clipboard.
                    use std::io::Write;
                    let mut out = std::io::stdout();
                    let _ = write!(out, "\x1b]52;c;{}\x07", base64(text.as_bytes()));
                    let _ = out.flush();
                }
                Effect::Notify(text) => term::notify(&text),
                Effect::OpenUrl(url) => {
                    let (cmd, args): (&str, &[&str]) = if cfg!(target_os = "macos") {
                        ("open", &[])
                    } else if cfg!(target_os = "windows") {
                        ("cmd", &["/C", "start", ""])
                    } else {
                        ("xdg-open", &[])
                    };
                    let _ = std::process::Command::new(cmd).args(args).arg(&url).spawn();
                }
            }
        }
    }
    let _ = handle.commands.send(sync::Command::Shutdown);
    Ok(())
}

// ---- headless modes ------------------------------------------------------------

async fn wait_live(handle: &mut sync::SyncHandle, app: &mut App, limit: Duration) -> bool {
    let deadline = Instant::now() + limit;
    while Instant::now() < deadline {
        match tokio::time::timeout(Duration::from_millis(200), handle.messages.recv()).await {
            Ok(Some(m)) => {
                let live_snapshot = matches!(m, SyncMsg::Snapshot { .. });
                app.apply_sync(m);
                if live_snapshot && app.state.connection.is_live() {
                    return true;
                }
            }
            Ok(None) => return false,
            Err(_) => {
                if app.state.connection.is_live() && app.state.snapshots_applied > 0 {
                    return true;
                }
            }
        }
    }
    false
}

async fn dump(cfg: SyncConfig, args: &Args) -> i32 {
    let mut handle = sync::spawn(cfg);
    let mut app = App::new(AppState::new(), Theme::new(args.opts), (120, 36));
    if !wait_live(&mut handle, &mut app, Duration::from_secs(30)).await {
        eprintln!(
            "nexus-tui: Core did not become live: {}",
            app.state.connection.detail()
        );
    }
    if args.open_first {
        let first = app
            .state
            .model
            .sessions_recent()
            .first()
            .map(|s| s.id.clone());
        if let Some(id) = first {
            app.open_session(&id);
            for e in std::mem::take(&mut app.effects) {
                if let Effect::Sync(c) = e {
                    let _ = handle.commands.send(c);
                }
            }
            let before = app.state.snapshots_applied;
            let deadline = Instant::now() + Duration::from_secs(5);
            while app.state.snapshots_applied == before && Instant::now() < deadline {
                if let Ok(Some(m)) =
                    tokio::time::timeout(Duration::from_millis(200), handle.messages.recv()).await
                {
                    app.apply_sync(m);
                }
            }
        }
    }
    for (w, h) in &args.dump {
        app.set_size(*w, *h);
        let rows = render::render_text(&mut app, *w, *h);
        let text = rows.join("\n");
        match &args.out {
            Some(dir) => {
                let _ = std::fs::create_dir_all(dir);
                let name = format!("frame-{w}x{h}.txt");
                if let Err(e) = std::fs::write(dir.join(&name), format!("{text}\n")) {
                    eprintln!("nexus-tui: {e}");
                    return 1;
                }
                println!("wrote {}", dir.join(name).display());
            }
            None => println!("--- {w}x{h} ---\n{text}"),
        }
    }
    let _ = handle.commands.send(sync::Command::Shutdown);
    0
}

async fn once(cfg: SyncConfig) -> i32 {
    let mut handle = sync::spawn(cfg);
    let mut state = AppState::new();
    let mut printed = String::new();
    while let Some(msg) = handle.messages.recv().await {
        state.apply(msg);
        let line = format!(
            "[{}] {}",
            state.connection.label(),
            state.connection.detail()
        );
        if line != printed {
            println!("{line}");
            printed = line;
        }
        if let Some(core) = state.core.as_ref().filter(|_| state.connection.is_live()) {
            println!("core {} pid {} home {}", core.version, core.pid, core.home);
            for b in &state.banners {
                println!("banner: {b:?}");
            }
            for p in state.model.projects.values() {
                println!("project {} {}", p.id, p.name);
            }
            for s in state.model.sessions_recent() {
                println!(
                    "session {} {} [{}]{}",
                    s.id,
                    s.title,
                    s.status.as_deref().unwrap_or("?"),
                    if s.archived { " archived" } else { "" }
                );
            }
            break;
        }
        if matches!(
            state.connection,
            ConnectionState::AuthFailed { .. } | ConnectionState::Incompatible { .. }
        ) {
            let _ = handle.commands.send(sync::Command::Shutdown);
            return 1;
        }
    }
    let _ = handle.commands.send(sync::Command::Shutdown);
    0
}
