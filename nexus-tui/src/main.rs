//! `nexus-tui`: the keyboard-first terminal client for Nexus Core.
//!
//! `nexus-tui`                  run the TUI
//! `nexus-tui --once`           connect, print state once live, exit (no UI)
//! `nexus-tui --dump-frame WxH` connect, render the real shell to text (or SVG), exit
//! See `--help` for the display options.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use crossterm::event::{Event, KeyEvent, KeyEventKind};
use nexus_tui::actions::{self, ActionDone};
use nexus_tui::app::{App, Effect};
use nexus_tui::discovery;
use nexus_tui::keys;
use nexus_tui::prefs;
use nexus_tui::render;
use nexus_tui::state::{AppState, ConnectionState};
use nexus_tui::svg;
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
  --keys \"SPEC\"         with --dump-frame: press keys before rendering, space
                         separated: ctrl+p esc down enter alt+2; any other
                         token is typed as text (\"/mo\")
  --svg                  with --dump-frame --out: also write <name>.svg
                         (real colours and styles; truecolor unless --256/--16)
  --name NAME            with --dump-frame --out: file stem (default
                         frame-WxH; with several sizes NAME-WxH)
  --bench WxH            open the most recent session and print load time,
                         RSS and frame times while scrolling (tools/bench.sh)
  --help
";

struct Args {
    opts: Options,
    over: prefs::Overrides,
    once: bool,
    dump: Vec<(u16, u16)>,
    out: Option<PathBuf>,
    open_first: bool,
    svg: bool,
    keys: Vec<Vec<KeyEvent>>,
    name: Option<String>,
    bench: Option<(u16, u16)>,
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
        svg: false,
        keys: Vec::new(),
        name: None,
        bench: None,
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
            "--svg" => a.svg = true,
            "--keys" => a.keys = keys::parse_spec(&it.next().ok_or("--keys needs a spec")?)?,
            "--name" => a.name = Some(it.next().ok_or("--name needs a file stem")?),
            "--out" => a.out = Some(PathBuf::from(it.next().ok_or("--out needs a directory")?)),
            "--bench" => {
                let v = it.next().ok_or("--bench needs a size such as 120x36")?;
                let (w, h) = v.split_once('x').ok_or_else(|| format!("bad size {v}"))?;
                let size = (w.parse(), h.parse());
                let (Ok(w), Ok(h)) = size else {
                    return Err(format!("bad size {v}"));
                };
                a.bench = Some((w, h));
            }
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
    } else if let Some(size) = args.bench {
        bench(cfg, args.opts, size).await
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
    // The launcher runs cargo from the repo, so it passes the directory you started in.
    app.ui.launch_dir = std::env::var("NEXUS_TUI_LAUNCH_DIR")
        .ok()
        .filter(|d| !d.is_empty())
        .or_else(|| {
            std::env::current_dir()
                .ok()
                .map(|d| d.to_string_lossy().into_owned())
        });

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
    let (image_tx, mut image_rx) = mpsc::unbounded_channel();
    let (capture_tx, mut capture_rx) =
        mpsc::unbounded_channel::<nexus_tui::dictation::CaptureEvent>();
    let mut capture: Option<nexus_tui::dictation::CaptureHandle> = None;
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
            Some((key, image)) = image_rx.recv() => app.apply_image_paste(key, image),
            Some(done) = done_rx.recv() => app.apply_done(done),
            Some(event) = capture_rx.recv() => {
                let finished = matches!(
                    &event,
                    nexus_tui::dictation::CaptureEvent::Stopped { .. }
                        | nexus_tui::dictation::CaptureEvent::Cancelled { .. }
                        | nexus_tui::dictation::CaptureEvent::Failed { .. }
                );
                let generation = match &event {
                    nexus_tui::dictation::CaptureEvent::Started { generation }
                    | nexus_tui::dictation::CaptureEvent::Level { generation, .. }
                    | nexus_tui::dictation::CaptureEvent::Preview { generation, .. }
                    | nexus_tui::dictation::CaptureEvent::Stopped { generation, .. }
                    | nexus_tui::dictation::CaptureEvent::Cancelled { generation }
                    | nexus_tui::dictation::CaptureEvent::Failed { generation, .. } => *generation,
                };
                app.apply_capture_event(event);
                if finished && capture.as_ref().is_some_and(|handle| handle.generation() == generation) {
                    if let Some(handle) = capture.take() {
                        handle.join();
                    }
                }
            }
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
                Effect::Dictation(command) => match command {
                    nexus_tui::dictation::CaptureCommand::Start { generation } => {
                        if let Some(old) = capture.take() {
                            old.send(nexus_tui::dictation::CaptureCommand::Cancel {
                                generation: old.generation(),
                            });
                            old.join();
                        }
                        capture = Some(nexus_tui::dictation::CaptureHandle::start(
                            generation,
                            capture_tx.clone(),
                        ));
                    }
                    command => {
                        if let Some(handle) = capture.as_ref() {
                            handle.send(command);
                        }
                    }
                },
                Effect::PasteImage(key) => {
                    let tx = image_tx.clone();
                    tokio::spawn(async move {
                        let _ = tx.send((key, nexus_tui::images::clipboard().await));
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
    if let Some(handle) = capture.take() {
        handle.send(nexus_tui::dictation::CaptureCommand::Cancel {
            generation: handle.generation(),
        });
        handle.join();
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

/// Time given to Core's replies after each `--keys` token.
const KEY_SETTLE: Duration = Duration::from_millis(350);

/// Run the app's effects and apply Core's replies for `limit`, as the
/// interactive loop does between key presses.
async fn pump(
    handle: &mut sync::SyncHandle,
    app: &mut App,
    home: &std::path::Path,
    done_tx: &mpsc::UnboundedSender<ActionDone>,
    done_rx: &mut mpsc::UnboundedReceiver<ActionDone>,
    limit: Duration,
) {
    let deadline = Instant::now() + limit;
    loop {
        for effect in std::mem::take(&mut app.effects) {
            match effect {
                Effect::Sync(c) => {
                    let _ = handle.commands.send(c);
                }
                Effect::Api(call) => {
                    let api = actions::api_for(home);
                    let tx = done_tx.clone();
                    tokio::spawn(async move {
                        let _ = tx.send(actions::run(api, call).await);
                    });
                }
                _ => {}
            }
        }
        let Some(left) = deadline.checked_duration_since(Instant::now()) else {
            break;
        };
        tokio::select! {
            m = handle.messages.recv() => match m {
                Some(m) => app.apply_sync(m),
                None => break,
            },
            Some(done) = done_rx.recv() => app.apply_done(done),
            _ = tokio::time::sleep(left) => {}
        }
        app.tick(Instant::now());
    }
}

async fn dump(cfg: SyncConfig, args: &Args) -> i32 {
    let mut handle = sync::spawn(cfg);
    let mut opts = args.opts;
    if !args.over.tier {
        // Frames are reviewed in colour whatever the terminal running this says.
        opts.tier = Tier::TrueColor;
    }
    let mut app = App::new(AppState::new(), Theme::new(opts), (120, 36));
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
    if !args.keys.is_empty() {
        let (w, h) = args.dump[0];
        app.set_size(w, h);
        let (done_tx, mut done_rx) = mpsc::unbounded_channel::<ActionDone>();
        let home = discovery::home().unwrap_or_default();
        for token in &args.keys {
            // A frame first, so keys that need the last layout (scrolling,
            // mouse targets) see it, as they do in the live app.
            let _ = render::render_buffer(&mut app, w, h);
            for k in token {
                app.handle_key(*k);
            }
            pump(
                &mut handle,
                &mut app,
                &home,
                &done_tx,
                &mut done_rx,
                KEY_SETTLE,
            )
            .await;
        }
        pump(
            &mut handle,
            &mut app,
            &home,
            &done_tx,
            &mut done_rx,
            KEY_SETTLE * 2,
        )
        .await;
    }
    for (w, h) in &args.dump {
        app.set_size(*w, *h);
        let buf = render::render_buffer(&mut app, *w, *h);
        let text = render::buffer_rows(&buf).join("\n");
        match &args.out {
            Some(dir) => {
                let _ = std::fs::create_dir_all(dir);
                let stem = match (&args.name, args.dump.len()) {
                    (Some(n), 1) => n.clone(),
                    (Some(n), _) => format!("{n}-{w}x{h}"),
                    (None, _) => format!("frame-{w}x{h}"),
                };
                let mut files = vec![(format!("{stem}.txt"), format!("{text}\n"))];
                if args.svg {
                    let (fg, bg) = svg::page_colors(opts.mode == Mode::Light);
                    files.push((format!("{stem}.svg"), svg::to_svg(&buf, fg, bg)));
                }
                for (name, body) in files {
                    if let Err(e) = std::fs::write(dir.join(&name), body) {
                        eprintln!("nexus-tui: {e}");
                        return 1;
                    }
                    println!("wrote {}", dir.join(name).display());
                }
            }
            None => println!("--- {w}x{h} ---\n{text}"),
        }
    }
    let _ = handle.commands.send(sync::Command::Shutdown);
    0
}

/// `--bench WxH`: open the most recent session and measure what opening and
/// scrolling a long session costs. Frame time is `render_buffer` (timeline
/// build, layout and painting into a buffer), not terminal output.
async fn bench(cfg: SyncConfig, opts: Options, (w, h): (u16, u16)) -> i32 {
    let mut handle = sync::spawn(cfg);
    let mut app = App::new(AppState::new(), Theme::new(opts), (w, h));
    if !wait_live(&mut handle, &mut app, Duration::from_secs(30)).await {
        eprintln!("nexus-tui: Core did not become live");
        return 1;
    }
    let Some(id) = app
        .state
        .model
        .sessions_recent()
        .first()
        .map(|s| s.id.clone())
    else {
        eprintln!("nexus-tui: no session to open");
        return 1;
    };
    let rss_before = rss_kib();
    let opened = Instant::now();
    app.open_session(&id);
    for e in std::mem::take(&mut app.effects) {
        if let Effect::Sync(c) = e {
            let _ = handle.commands.send(c);
        }
    }
    // Loaded when the selected session's stored history has been applied.
    let deadline = opened + Duration::from_secs(120);
    let mut loaded = false;
    while !loaded && Instant::now() < deadline {
        if let Ok(Some(m)) =
            tokio::time::timeout(Duration::from_millis(200), handle.messages.recv()).await
        {
            loaded = matches!(&m, SyncMsg::History(h) if h.session_id == id);
            app.apply_sync(m);
        }
    }
    if !loaded {
        eprintln!("nexus-tui: the session history did not load");
        return 1;
    }
    let history = opened.elapsed();
    let _ = render::render_buffer(&mut app, w, h);
    let first_frame = opened.elapsed();
    let rss_after = rss_kib();
    // Scroll up a page per frame from the newest end, then back down.
    let page = (h / 2).max(1) as isize;
    let mut frames = Vec::new();
    for i in 0..400 {
        app.scroll_timeline(if i < 200 { -page } else { page });
        let t = Instant::now();
        let _ = render::render_buffer(&mut app, w, h);
        frames.push(t.elapsed().as_secs_f64() * 1000.0);
    }
    let mean = frames.iter().sum::<f64>() / frames.len() as f64;
    frames.sort_by(f64::total_cmp);
    let p95 = frames[frames.len() * 95 / 100];
    let view = app.state.model.selected.as_ref();
    println!("session {id}");
    println!("messages {}", view.map_or(0, |v| v.messages.len()));
    println!("history_ms {:.1}", history.as_secs_f64() * 1000.0);
    println!("first_frame_ms {:.1}", first_frame.as_secs_f64() * 1000.0);
    println!(
        "rss_kib before {} after {}",
        rss_before.unwrap_or(0),
        rss_after.unwrap_or(0)
    );
    println!(
        "frame_ms mean {mean:.2} p95 {p95:.2} max {:.2} (n={})",
        frames.last().copied().unwrap_or(0.0),
        frames.len()
    );
    let _ = handle.commands.send(sync::Command::Shutdown);
    0
}

/// This process's resident set size from `ps`, in KiB.
fn rss_kib() -> Option<u64> {
    let out = std::process::Command::new("ps")
        .args(["-o", "rss=", "-p", &std::process::id().to_string()])
        .output()
        .ok()?;
    String::from_utf8_lossy(&out.stdout).trim().parse().ok()
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
