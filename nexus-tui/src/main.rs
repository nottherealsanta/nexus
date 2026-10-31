//! `nexus-tui`: the keyboard-first terminal client for Nexus Core.
//!
//! `nexus-tui`                  run the TUI
//! `nexus-tui --once`           connect, print state once live, exit (no UI)
//! `nexus-tui --dump-frame WxH` connect, render the real shell to text (or SVG), exit
//! See `--help` for the display options.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use crossterm::event::{Event, KeyEvent, KeyEventKind};
use nexus_tui::actions::{self, ActionDone};
use nexus_tui::api::{Event as ApiEvent, History, Message, Snapshot};
use nexus_tui::app::{App, Effect};
use nexus_tui::discovery;
use nexus_tui::frames::{self, Applied};
use nexus_tui::keys;
use nexus_tui::prefs;
use nexus_tui::render;
use nexus_tui::state::{AppState, ConnectionState};
use nexus_tui::svg;
use nexus_tui::sync::{self, ResyncCause, SyncConfig, SyncMsg};
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
  --replay               with --bench: replay a deterministic in-memory stream
                         instead of opening Core; reports burst and paced
                         frames (NEXUS_TUI_REPLAY_MESSAGES, _EVENTS)
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
    replay: bool,
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
        replay: false,
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
            "--replay" => a.replay = true,
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
        if args.replay {
            replay(args.opts, size)
        } else {
            bench(cfg, args.opts, size).await
        }
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

/// Everything the loop owns besides the four channels it wakes on. Splitting
/// these out lets one item's effects be dispatched before the next item is
/// applied.
struct Interactive {
    app: App,
    terminal: term::Term,
    commands: mpsc::UnboundedSender<sync::Command>,
    home: PathBuf,
    done_tx: mpsc::UnboundedSender<ActionDone>,
    image_tx: mpsc::UnboundedSender<(String, Result<Option<nexus_tui::images::Image>, String>)>,
    capture_tx: mpsc::UnboundedSender<nexus_tui::dictation::CaptureEvent>,
    capture: Option<nexus_tui::dictation::CaptureHandle>,
    loading_cue: Option<tokio::task::JoinHandle<()>>,
    program_status: nexus_tui::program_status::Tracker,
}

impl Interactive {
    fn draw(&mut self) -> std::io::Result<()> {
        let terminal = &mut self.terminal;
        let app = &mut self.app;
        term::synchronized_update(&mut std::io::stdout(), || {
            terminal.draw(|f| render::render(f, app)).map(|_| ())
        })
    }

    /// Reconcile program status and run this item's effects, in order. Effects
    /// are the barrier between items; `halt` means the loop must exit now
    /// (Quit), and a suspend or external editor always asks for a redraw.
    fn settle(&mut self) -> std::io::Result<Applied> {
        let mut dirty = false;
        nexus_tui::program_status::emit(self.program_status.reconcile(&self.app.state));
        self.app.persist_prefs();
        for effect in std::mem::take(&mut self.app.effects) {
            match effect {
                Effect::Quit => return Ok(Applied::halt(dirty)),
                Effect::Suspend => {
                    nexus_tui::program_status::emit(self.program_status.release());
                    term::suspend(&mut self.terminal)?;
                    self.app
                        .set_size(self.terminal.size()?.width, self.terminal.size()?.height);
                    nexus_tui::program_status::emit(self.program_status.reconcile(&self.app.state));
                    dirty = true;
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
                    nexus_tui::program_status::emit(self.program_status.release());
                    term::run_external(&mut self.terminal, &editor, &path)?;
                    self.app
                        .set_size(self.terminal.size()?.width, self.terminal.size()?.height);
                    nexus_tui::program_status::emit(self.program_status.reconcile(&self.app.state));
                    dirty = true;
                }
                Effect::Sync(command) => {
                    let _ = self.commands.send(command);
                }
                Effect::Api(call) => {
                    let api = actions::api_for(&self.home);
                    let tx = self.done_tx.clone();
                    tokio::spawn(async move {
                        let _ = tx.send(actions::run(api, call).await);
                    });
                }
                Effect::Dictation(command) => match command {
                    nexus_tui::dictation::CaptureCommand::Start { generation } => {
                        if let Some(old) = self.capture.take() {
                            old.send(nexus_tui::dictation::CaptureCommand::Cancel {
                                generation: old.generation(),
                            });
                            old.join();
                        }
                        self.capture = Some(nexus_tui::dictation::CaptureHandle::start(
                            generation,
                            self.capture_tx.clone(),
                        ));
                    }
                    command => {
                        if let Some(handle) = self.capture.as_ref() {
                            handle.send(command);
                        }
                    }
                },
                Effect::DictationLoadingCue(start) => {
                    if let Some(task) = self.loading_cue.take() {
                        task.abort();
                    }
                    if start {
                        self.loading_cue = Some(tokio::spawn(async {
                            loop {
                                nexus_tui::dictation::play_loading_cue();
                                tokio::time::sleep(Duration::from_millis(1500)).await;
                            }
                        }));
                    }
                }
                Effect::PasteImage(key) => {
                    let tx = self.image_tx.clone();
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
        Ok(Applied::dirty(dirty))
    }

    /// Apply one sync message through the reducer, then its effects. The
    /// program-status accepted-event guard is exactly the reference loop's.
    fn apply_sync_item(&mut self, msg: SyncMsg) -> std::io::Result<Applied> {
        let snapshot = matches!(&msg, sync::SyncMsg::Snapshot { .. });
        let event = match &msg {
            sync::SyncMsg::Event(event) if event.id > self.app.state.model.last_id => {
                Some(event.clone())
            }
            _ => None,
        };
        let mut applied = Applied::dirty(self.app.apply_sync_tracked(msg));
        if snapshot {
            self.program_status.snapshot();
        }
        if let Some(event) = event.filter(|event| self.app.state.model.last_id == event.id) {
            self.program_status.event(&event);
        }
        let settled = self.settle()?;
        applied.dirty |= settled.dirty;
        applied.halt = settled.halt;
        Ok(applied)
    }

    /// One input event. Input is a render boundary: its result is always drawn
    /// before another input is handled, so hit maps and scrolls are current.
    fn apply_input(&mut self, ev: Event) -> std::io::Result<Applied> {
        match ev {
            Event::Key(k) if k.kind != KeyEventKind::Release => self.app.handle_key(k),
            Event::Mouse(m) => self.app.handle_mouse(m),
            Event::Paste(s) => self.app.handle_paste(&s),
            Event::Resize(w, h) => self.app.set_size(w, h),
            _ => {}
        }
        let mut applied = self.settle()?;
        applied.dirty = true;
        Ok(applied)
    }

    fn apply_tick(&mut self, now: Instant) -> std::io::Result<Applied> {
        let mut applied = Applied::dirty(self.app.tick(now));
        let settled = self.settle()?;
        applied.dirty |= settled.dirty;
        applied.halt = settled.halt;
        Ok(applied)
    }

    fn apply_image(
        &mut self,
        key: String,
        image: Result<Option<nexus_tui::images::Image>, String>,
    ) -> std::io::Result<Applied> {
        self.app.apply_image_paste(key, image);
        let mut applied = self.settle()?;
        applied.dirty = true;
        Ok(applied)
    }

    fn apply_done(&mut self, done: ActionDone) -> std::io::Result<Applied> {
        self.app.apply_done(done);
        let mut applied = self.settle()?;
        applied.dirty = true;
        Ok(applied)
    }

    fn apply_capture(
        &mut self,
        event: nexus_tui::dictation::CaptureEvent,
    ) -> std::io::Result<Applied> {
        use nexus_tui::dictation::CaptureEvent;
        let finished = matches!(
            &event,
            CaptureEvent::Stopped { .. }
                | CaptureEvent::Cancelled { .. }
                | CaptureEvent::Failed { .. }
        );
        let generation = match &event {
            CaptureEvent::Started { generation }
            | CaptureEvent::Level { generation, .. }
            | CaptureEvent::Preview { generation, .. }
            | CaptureEvent::Stopped { generation, .. }
            | CaptureEvent::Cancelled { generation }
            | CaptureEvent::CueWarning { generation, .. }
            | CaptureEvent::Failed { generation, .. } => *generation,
        };
        self.app.apply_capture_event(event);
        if finished
            && self
                .capture
                .as_ref()
                .is_some_and(|handle| handle.generation() == generation)
        {
            if let Some(handle) = self.capture.take() {
                handle.join();
            }
        }
        let mut applied = self.settle()?;
        applied.dirty = true;
        Ok(applied)
    }

    fn shutdown(&mut self) {
        if let Some(handle) = self.capture.take() {
            handle.send(nexus_tui::dictation::CaptureCommand::Cancel {
                generation: handle.generation(),
            });
            handle.join();
        }
        if let Some(task) = self.loading_cue.take() {
            task.abort();
        }
        let _ = self.commands.send(sync::Command::Shutdown);
    }
}

enum Wake {
    Input(Option<Event>),
    Sync(Option<SyncMsg>),
    Image(Option<(String, Result<Option<nexus_tui::images::Image>, String>)>),
    Done(Option<Box<ActionDone>>),
    Capture(Option<nexus_tui::dictation::CaptureEvent>),
    Tick,
}

async fn run(
    cfg: SyncConfig,
    home: PathBuf,
    opts: Options,
    over: prefs::Overrides,
) -> std::io::Result<()> {
    let terminal = term::enter()?;
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
    let (image_tx, mut image_rx) =
        mpsc::unbounded_channel::<(String, Result<Option<nexus_tui::images::Image>, String>)>();
    let (capture_tx, mut capture_rx) =
        mpsc::unbounded_channel::<nexus_tui::dictation::CaptureEvent>();
    let mut inter = Interactive {
        app,
        terminal,
        commands: handle.commands.clone(),
        home,
        done_tx,
        image_tx,
        capture_tx,
        capture: None,
        loading_cue: None,
        program_status: nexus_tui::program_status::Tracker::default(),
    };

    let mut tick = tokio::time::interval(frames::TICK_INTERVAL);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    // The first interval tick is ready immediately, as before. Tracking the
    // deadline lets a batch stop before taking more sync work when a tick is
    // due; the tick itself still runs at the next service opportunity.
    let mut next_tick = tokio::time::Instant::now();
    // Auxiliary channels are closed by dropping their senders. On closure the
    // branch is disabled rather than re-polled, so the loop never spins.
    let mut image_open = true;
    let mut done_open = true;
    let mut capture_open = true;

    let mut dirty = true;
    'main: loop {
        if dirty {
            inter.draw()?;
            dirty = false;
        }
        // tokio's select! picks a ready branch at random, so a continuously
        // ready input channel cannot starve sync or the tick (or the reverse).
        // Ready input is also a batch barrier, so a long sync queue cannot
        // delay it by more than the one message already being applied.
        let wake = tokio::select! {
            ev = ev_rx.recv() => Wake::Input(ev),
            msg = handle.messages.recv() => Wake::Sync(msg),
            res = image_rx.recv(), if image_open => Wake::Image(res),
            res = done_rx.recv(), if done_open => Wake::Done(res.map(Box::new)),
            res = capture_rx.recv(), if capture_open => Wake::Capture(res),
            _ = tick.tick() => Wake::Tick,
        };
        match wake {
            Wake::Input(Some(ev)) => {
                let applied = inter.apply_input(ev)?;
                dirty |= applied.dirty;
                if applied.halt {
                    break 'main;
                }
            }
            Wake::Input(None) => break 'main,
            Wake::Sync(Some(first)) => {
                let started = Instant::now();
                let mut failure = None;
                let outcome = frames::run_batch(
                    first,
                    started,
                    Instant::now,
                    || handle.messages.try_recv().ok(),
                    // Ready input, a due tick or an auxiliary reply ends the
                    // batch. Checked before each additional message.
                    || {
                        !ev_rx.is_empty()
                            || (image_open && !image_rx.is_empty())
                            || (done_open && !done_rx.is_empty())
                            || (capture_open && !capture_rx.is_empty())
                            || tokio::time::Instant::now() >= next_tick
                    },
                    |msg| match inter.apply_sync_item(msg) {
                        Ok(applied) => applied,
                        Err(error) => {
                            failure = Some(error);
                            Applied::halt(true)
                        }
                    },
                );
                if let Some(error) = failure {
                    return Err(error);
                }
                dirty |= outcome.dirty;
                if outcome.halt {
                    break 'main;
                }
            }
            Wake::Sync(None) => break 'main,
            Wake::Image(Some((key, image))) => {
                let applied = inter.apply_image(key, image)?;
                dirty |= applied.dirty;
                if applied.halt {
                    break 'main;
                }
            }
            Wake::Image(None) => image_open = false,
            Wake::Done(Some(done)) => {
                let applied = inter.apply_done(*done)?;
                dirty |= applied.dirty;
                if applied.halt {
                    break 'main;
                }
            }
            Wake::Done(None) => done_open = false,
            Wake::Capture(Some(event)) => {
                let applied = inter.apply_capture(event)?;
                dirty |= applied.dirty;
                if applied.halt {
                    break 'main;
                }
            }
            Wake::Capture(None) => capture_open = false,
            Wake::Tick => {
                let applied = inter.apply_tick(Instant::now())?;
                dirty |= applied.dirty;
                next_tick = tokio::time::Instant::now() + frames::TICK_INTERVAL;
                if applied.halt {
                    break 'main;
                }
            }
        }
    }
    inter.shutdown();
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
    if !open_and_load(&mut app, &mut handle, &id).await {
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
    // Switch to the next most recent session and back: the first view is
    // cached, so it shows at once while its history reloads.
    if let Some(other) = app
        .state
        .model
        .sessions_recent()
        .get(1)
        .map(|s| s.id.clone())
    {
        let t = Instant::now();
        if open_and_load(&mut app, &mut handle, &other).await {
            println!("switch_away_ms {:.1}", t.elapsed().as_secs_f64() * 1000.0);
            let t = Instant::now();
            app.open_session(&id);
            let shown = app
                .state
                .model
                .selected
                .as_ref()
                .map_or(0, |v| v.messages.len());
            let _ = render::render_buffer(&mut app, w, h);
            println!(
                "switch_back_first_frame_ms {:.1} (messages shown {shown})",
                t.elapsed().as_secs_f64() * 1000.0
            );
            if open_and_load(&mut app, &mut handle, &id).await {
                println!(
                    "switch_back_history_ms {:.1}",
                    t.elapsed().as_secs_f64() * 1000.0
                );
            }
        }
    }
    let _ = handle.commands.send(sync::Command::Shutdown);
    0
}

const REPLAY_SESSION: &str = "ses_1a17de160eccc428a38";
const REPLAY_GEN: &str = "log_c915bfc9b6d826c4";

fn replay_event(id: i64, kind: &str, data: serde_json::Value) -> ApiEvent {
    ApiEvent {
        id,
        cursor: Some(format!("{REPLAY_GEN}:{id}")),
        seq: Some(id),
        time: 1793113400.0 + id as f64,
        kind: kind.into(),
        session_id: Some(REPLAY_SESSION.into()),
        data,
    }
}

/// 1.2 KB of Markdown and a code fence per message, so a frame pays for the
/// parser and wrapper as a long real session does. With
/// `NEXUS_TUI_REPLAY_TOOLS`, every tenth assistant message also carries an
/// `edit` call and a tool result with a long diff, exercising tool details.
fn replay_history(n: usize) -> History {
    let with_tools = std::env::var_os("NEXUS_TUI_REPLAY_TOOLS").is_some();
    let mut messages = Vec::with_capacity(n);
    let mut seq = 0i64;
    for i in 0..n {
        let mut text = format!("**Message {}**\n\n", i + 1);
        for _ in 0..10 {
            text.push_str(
                "the loader caches each manifest by mtime so edits still show up and a test counts parses; ",
            );
        }
        text.push_str(&format!(
            "\n\n- first item\n- second item\n\n```rust\nfn parse_{i}() -> usize {{\n    {i}\n}}\n```\n"
        ));
        let mut parts = vec![serde_json::json!({"type": "text", "text": text})];
        let tool = with_tools && i % 10 == 9;
        if tool {
            let old: String = (0..80)
                .map(|line| format!("old line {line}: the loader caches each manifest\n"))
                .collect();
            let new: String = (0..90)
                .map(|line| format!("new line {line}: the loader caches each manifest\n"))
                .collect();
            parts.push(serde_json::json!({
                "type": "tool_call", "id": format!("call_replay_{i}"), "name": "edit",
                "arguments": serde_json::json!({
                    "path": format!("src/module_{i}.rs"),
                    "old_string": old, "new_string": new
                }).to_string()
            }));
        }
        messages.push(Message {
            id: format!("msg_replay_{i}"),
            session_id: REPLAY_SESSION.into(),
            seq,
            created: 1793113000.0 + seq as f64,
            role: if i % 2 == 0 { "user" } else { "assistant" }.into(),
            parts,
            delivery: None,
        });
        seq += 1;
        if tool {
            let output: String = (0..120)
                .map(|line| format!("src/module_{i}.rs:{line}: parse_manifest(path)\n"))
                .collect();
            messages.push(Message {
                id: format!("msg_replay_tool_{i}"),
                session_id: REPLAY_SESSION.into(),
                seq,
                created: 1793113000.0 + seq as f64,
                role: "tool".into(),
                parts: vec![serde_json::json!({
                    "type": "tool_result", "call_id": format!("call_replay_{i}"),
                    "output": output, "is_error": false
                })],
                delivery: None,
            });
            seq += 1;
        }
    }
    History {
        session_id: REPLAY_SESSION.into(),
        messages,
        ..Default::default()
    }
}

/// A deterministic stream: one invisible `wire_event` per visible `text_delta`
/// (the reported Core shape), plus reasoning, a final message and summary.
fn replay_stream(target: usize) -> Vec<SyncMsg> {
    let mut out = Vec::new();
    let mut id = 10_000i64;
    out.push(replay_event(
        id,
        "session.status",
        serde_json::json!({"status": "busy", "turn_id": "trn_replay", "origin": "prompt"}),
    ));
    id += 1;
    while out.len() < target {
        out.push(replay_event(
            id,
            "wire_event",
            serde_json::json!({"type": "wire_event", "name": "response.output_text.delta",
                "data": {"delta": "word "}}),
        ));
        id += 1;
        if out.len() >= target {
            break;
        }
        out.push(replay_event(
            id,
            "text_delta",
            serde_json::json!({"type": "text_delta", "text": "word "}),
        ));
        id += 1;
        if id % 21 == 0 && out.len() < target {
            out.push(replay_event(
                id,
                "reasoning_delta",
                serde_json::json!({"type": "reasoning_delta", "text": "thinking "}),
            ));
            id += 1;
        }
    }
    out.push(replay_event(
        id,
        "message.added",
        serde_json::json!({
            "id": "msg_replay_final", "session_id": REPLAY_SESSION,
            "seq": 100_000, "created": 1793114000.0, "role": "assistant",
            "parts": [{"type": "text", "text": "replay complete"}]
        }),
    ));
    id += 1;
    out.push(replay_event(
        id,
        "turn.summary",
        serde_json::json!({"id": "trn_replay", "status": "completed",
            "started": 1793113400.0, "finished": 1793114000.0,
            "start_seq": 100_000, "steps": 1, "stop": "end_turn",
            "ended": {"stop": "end_turn", "steps": 1}}),
    ));
    out.into_iter().map(SyncMsg::Event).collect()
}

fn replay_app(opts: Options, (w, h): (u16, u16), messages: usize) -> App {
    let snapshot: Snapshot =
        serde_json::from_str(include_str!("../fixtures/real/snapshot.json")).expect("fixture");
    let mut state = AppState::new();
    state.apply(SyncMsg::Connection(ConnectionState::Live));
    state.apply(SyncMsg::Snapshot {
        snapshot: Box::new(snapshot),
        cause: ResyncCause::Initial,
    });
    let mut app = App::new(state, Theme::new(opts), (w, h));
    if std::env::var_os("NEXUS_TUI_REPLAY_TOOLS").is_some() {
        app.ui.detail = nexus_tui::ui::Detail::High;
    }
    app.open_session(REPLAY_SESSION);
    app.effects.clear();
    app.apply_sync(SyncMsg::History(Box::new(replay_history(messages))));
    app
}

fn elapsed_ms(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
}

fn timing(label: &str, mut values: Vec<f64>) {
    if values.is_empty() {
        println!("{label} n 0");
        return;
    }
    let n = values.len();
    let mean = values.iter().sum::<f64>() / n as f64;
    values.sort_by(f64::total_cmp);
    println!(
        "{label} mean {mean:.2} p95 {:.2} max {:.2} (n={n})",
        values[n * 95 / 100],
        values[n - 1]
    );
}

/// Draw only when dirty and apply one burst at a time, as the interactive loop
/// does. Returns the frame count and its draw and reducer timings.
fn replay_burst(
    app: &mut App,
    stream: &[SyncMsg],
    (w, h): (u16, u16),
) -> (usize, Vec<f64>, Vec<f64>) {
    let mut queue: VecDeque<SyncMsg> = stream.iter().cloned().collect();
    let mut dirty = true;
    let mut frames = 0;
    let mut draws = Vec::new();
    let mut reduces = Vec::new();
    let clock = Instant::now();
    loop {
        if dirty {
            let start = Instant::now();
            let _ = render::render_buffer(app, w, h);
            draws.push(elapsed_ms(start));
            frames += 1;
            dirty = false;
        }
        let Some(first) = queue.pop_front() else {
            break;
        };
        let outcome = frames::run_batch(
            first,
            clock,
            || clock,
            || queue.pop_front(),
            || false,
            |msg| {
                let start = Instant::now();
                let dirty = app.apply_sync_tracked(msg);
                reduces.push(elapsed_ms(start));
                Applied::dirty(dirty)
            },
        );
        dirty |= outcome.dirty;
    }
    (frames, draws, reduces)
}

/// One event per batch (its arrival found the queue empty), the paced case in
/// which coalescing cannot help.
fn replay_paced(
    app: &mut App,
    stream: &[SyncMsg],
    (w, h): (u16, u16),
) -> (usize, Vec<f64>, Vec<f64>) {
    let mut dirty = true;
    let mut frames = 0;
    let mut draws = Vec::new();
    let mut reduces = Vec::new();
    for msg in stream {
        if dirty {
            let start = Instant::now();
            let _ = render::render_buffer(app, w, h);
            draws.push(elapsed_ms(start));
            frames += 1;
            dirty = false;
        }
        let outcome = frames::run_batch(
            msg.clone(),
            Instant::now(),
            Instant::now,
            || None,
            || false,
            |msg| {
                let start = Instant::now();
                let dirty = app.apply_sync_tracked(msg);
                reduces.push(elapsed_ms(start));
                Applied::dirty(dirty)
            },
        );
        dirty |= outcome.dirty;
    }
    if dirty {
        let start = Instant::now();
        let _ = render::render_buffer(app, w, h);
        draws.push(elapsed_ms(start));
        frames += 1;
    }
    (frames, draws, reduces)
}

/// This process's user and system CPU seconds, for benchmark deltas.
fn cpu_ms() -> (f64, f64) {
    // SAFETY: `getrusage` fills a zeroed struct on success.
    unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) == 0 {
            (
                usage.ru_utime.tv_sec as f64 * 1000.0 + usage.ru_utime.tv_usec as f64 / 1000.0,
                usage.ru_stime.tv_sec as f64 * 1000.0 + usage.ru_stime.tv_usec as f64 / 1000.0,
            )
        } else {
            (0.0, 0.0)
        }
    }
}

/// `--bench WxH --replay`: a deterministic in-memory stream through the
/// production batch policy. No Core and no PTY: this measures scheduling,
/// layout and paint, not terminal output or input latency.
fn replay(opts: Options, (w, h): (u16, u16)) -> i32 {
    let cpu_start = cpu_ms();
    let messages = std::env::var("NEXUS_TUI_REPLAY_MESSAGES")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(200usize);
    let target = std::env::var("NEXUS_TUI_REPLAY_EVENTS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(900usize);
    let stream = replay_stream(target);
    let mut app = replay_app(opts, (w, h), messages);
    let start = Instant::now();
    let _ = render::render_buffer(&mut app, w, h);
    let first = elapsed_ms(start);
    let build_start = Instant::now();
    for _ in 0..10 {
        let _ = nexus_tui::timeline::build(&app, &app.theme, app.frame, 85);
    }
    let build = elapsed_ms(build_start) / 10.0;

    let start = Instant::now();
    let (burst_frames, burst_draws, burst_reduces) = replay_burst(&mut app, &stream, (w, h));
    let burst_total = elapsed_ms(start);
    let burst_cache = app.layout.borrow().stats();
    let burst_entries = app.layout.borrow().len();
    let burst_bytes = app.layout.borrow().bytes();
    let mut paced = replay_app(opts, (w, h), messages);
    let start = Instant::now();
    let (paced_frames, paced_draws, paced_reduces) = replay_paced(&mut paced, &stream, (w, h));
    let paced_total = elapsed_ms(start);
    let paced_cache = paced.layout.borrow().stats();
    let paced_entries = paced.layout.borrow().len();
    let paced_bytes = paced.layout.borrow().bytes();

    println!("replay session {REPLAY_SESSION} size {w}x{h}");
    println!(
        "replay messages {messages} events {} invisible_events {}",
        stream.len(),
        stream
            .iter()
            .filter(|m| matches!(m, SyncMsg::Event(e) if e.kind == "wire_event"))
            .count()
    );
    println!("replay_first_frame_ms {first:.2}");
    println!("timeline_build_ms {build:.2} (width 85)");
    println!(
        "burst frames {burst_frames} applied {} total_ms {burst_total:.1} applied_per_s {:.0}",
        stream.len(),
        stream.len() as f64 / (burst_total / 1000.0)
    );
    timing("burst_draw_ms", burst_draws);
    timing("burst_reduce_ms", burst_reduces);
    println!(
        "burst_cache hits {} misses {} evictions {} entries {burst_entries} bytes {burst_bytes}",
        burst_cache.0, burst_cache.1, burst_cache.2
    );
    println!(
        "paced frames {paced_frames} applied {} total_ms {paced_total:.1} applied_per_s {:.0}",
        stream.len(),
        stream.len() as f64 / (paced_total / 1000.0)
    );
    timing("paced_draw_ms", paced_draws);
    timing("paced_reduce_ms", paced_reduces);
    println!(
        "paced_cache hits {} misses {} evictions {} entries {paced_entries} bytes {paced_bytes}",
        paced_cache.0, paced_cache.1, paced_cache.2
    );
    let cpu_end = cpu_ms();
    println!(
        "cpu_ms user {:.0} sys {:.0}",
        cpu_end.0 - cpu_start.0,
        cpu_end.1 - cpu_start.1
    );
    println!("rss_kib {}", rss_kib().unwrap_or(0));
    0
}

/// Open `id` and apply sync messages until its stored history has been
/// applied (at most 120 s). False when it never arrived.
async fn open_and_load(app: &mut App, handle: &mut sync::SyncHandle, id: &str) -> bool {
    app.open_session(id);
    for e in std::mem::take(&mut app.effects) {
        if let Effect::Sync(c) = e {
            let _ = handle.commands.send(c);
        }
    }
    let deadline = Instant::now() + Duration::from_secs(120);
    while Instant::now() < deadline {
        if let Ok(Some(m)) =
            tokio::time::timeout(Duration::from_millis(200), handle.messages.recv()).await
        {
            let loaded = matches!(&m, SyncMsg::History(h) if h.session_id == id);
            app.apply_sync(m);
            if loaded {
                return true;
            }
        }
    }
    false
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
