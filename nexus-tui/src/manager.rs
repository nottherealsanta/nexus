//! The Manager in the TUI (MANAGER_PLAN.md section 10): state and behaviour.
//!
//! The Manager is a voice agent above all sessions. Here it is three layers
//! painted over the app and nothing more: an orb at the bottom centre, a
//! caption above it and at most one card in a semi-transparent modal. This
//! module is the model of those layers and every way into them: the `manager.*`
//! events, the keys taken only while engaged, the voice flow (microphone,
//! transcription, speech), navigation, and the calls to Core. Drawing is
//! `orb.rs` (the orb) and `stage.rs` (caption, field and cards).
//!
//! At rest, with no call, nothing here runs: no key is taken but the Manager's
//! own shortcuts, no tick does work and no frame is requested.

use std::cell::Cell;
use std::collections::VecDeque;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use serde_json::{json, Value};

use crate::actions::{ActionDone, ApiCall};
use crate::api::Event;
use crate::app::{App, Effect, Sound};
use crate::composer::Composer;
use crate::dictation::{CaptureCommand, CaptureEvent};
use crate::orb::{Orb, OrbState};
use crate::sync::{Command as SyncCommand, ManagerCall, ManagerError};
use crate::ui::{Level, Target};

/// Capture generations of the Manager start here, clear of dictation's own.
pub const CAPTURE_BASE: u64 = 1 << 48;

/// A caption fades this long after the exchange goes quiet.
pub const CAPTION_QUIET: Duration = Duration::from_secs(5);
/// And takes this long to go.
pub const CAPTION_FADE: Duration = Duration::from_millis(300);
/// An unfocused card nobody engages collapses to its caption after this.
pub const UNFOCUSED_CARD: Duration = Duration::from_secs(12);
/// The card fades in and rises a few cells over this.
pub const CARD_ENTER: Duration = Duration::from_millis(220);
/// Thinking this long earns a soft cue, so silence never reads as a dropped call.
const THINKING_CUE: Duration = Duration::from_secs(1);
/// A `say` that Core has not answered with a phase change yet still reads as thinking.
const AWAITING: Duration = Duration::from_secs(8);
/// Speech RMS is small; the microphone's reading is scaled so a voice spans the orb's range.
const MIC_GAIN: f32 = 7.0;

pub const VOICE_UNAVAILABLE: &str =
    "Voice isn't set up. Type instead, or set it up in Settings \u{203a} Dictation.";
pub const MANAGER_OFF: &str = "Manager is off. Turn it on in Settings \u{203a} Manager.";
pub const ROW_GONE: &str = "That session no longer exists.";
pub const PLACEHOLDER: &str = "Tell the Manager\u{2026}";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CallPhase {
    #[default]
    Idle,
    Thinking,
    Replying,
}

/// Where the microphone is in its life.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Mic {
    #[default]
    Off,
    /// Asking whether speech recognition is ready.
    Checking,
    /// Capture has been asked for and has not reported yet.
    Opening,
    Listening,
    /// Stop was sent; the recording is on its way.
    Stopping,
    /// The recording is with Core's speech-to-text.
    Transcribing,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Call {
    pub id: String,
    pub holder: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Failure {
    pub code: String,
    pub message: String,
    pub fix: String,
}

/// The words above the orb: what the user said (dim) and the Manager's current sentence.
#[derive(Debug, Clone, PartialEq)]
pub struct Caption {
    pub user: Option<String>,
    pub manager: Option<String>,
    pub since: Instant,
}

#[derive(Debug, Clone)]
pub struct CardState {
    pub id: String,
    pub value: Value,
    pub focused: bool,
    pub shown: Instant,
    /// An unfocused card that nobody engaged has gone back to its caption.
    pub collapsed: bool,
    /// First visible line of a long body.
    pub scroll: usize,
}

#[derive(Debug, Clone, PartialEq)]
struct Sentence {
    turn: String,
    seq: u64,
    text: String,
    /// An unprompted line: caption and speech, never an orb.
    proactive: bool,
}

#[derive(Debug, Clone)]
struct Playing {
    token: u64,
    text: String,
    /// Real audio is being fetched and played; false when the text is just read.
    audio: bool,
    /// The caption for this sentence has been shown.
    shown: bool,
    until: Option<Instant>,
    proactive: bool,
}

/// The privacy notice Core wants acknowledged (`409 consent_required`).
#[derive(Debug, Clone, PartialEq)]
pub struct Consent {
    pub notice: String,
    pub provider: String,
    pub version: Value,
    talk: bool,
    say: Option<String>,
}

/// A view the Manager moved the user away from, so "go back" can return.
#[derive(Debug, Clone, PartialEq)]
pub struct View {
    pub project: Option<String>,
    pub session: Option<String>,
    pub panel: Option<String>,
}

pub struct ManagerUi {
    pub call: Option<Call>,
    pub starting: bool,
    talk_after_start: bool,
    say_after_start: Option<String>,
    early: Vec<Event>,
    pub phase: CallPhase,
    pub error: Option<Failure>,
    pub awaiting: Option<Instant>,
    pub mic: Mic,
    capture_gen: u64,
    capture_seq: u64,
    pub mic_level: f32,
    /// GPT-Live: Core holds the call and this client only carries its audio (`live.rs`).
    pub live_call: bool,
    pub voice_ok: Option<bool>,
    pub speaking: bool,
    queue: VecDeque<Sentence>,
    playing: Option<Playing>,
    muted_turn: Option<String>,
    last_turn: Option<String>,
    token: u64,
    pub playback: crate::speech::Playback,
    tts_ok: bool,
    pub caption: Option<Caption>,
    pub field: Option<Composer>,
    pub card: Option<CardState>,
    /// The privacy notice waiting to be accepted before a call can start.
    pub consent: Option<Consent>,
    pub back: Vec<View>,
    reported: (bool, bool),
    pub orb: Orb,
    /// No audio device is touched: speech is shown as text, the microphone is
    /// pretended. `--dump-frame` and tests set this.
    pub silent: bool,
    /// Dev-only, `NEXUS_TUI_MANAGER_PIN` in `--dump-frame`: the orb is held at a
    /// state and level (`Some(Some(..))`) or held away (`Some(None)`). It is never
    /// read outside `--dump-frame`, so it ships inert.
    pub pin: Option<Option<(OrbState, f32)>>,
    thinking_since: Option<Instant>,
    cued: bool,
    anim_until: Option<Instant>,
    /// Where the typed field's cursor was drawn last, for the terminal cursor.
    pub cursor: Cell<Option<(u16, u16)>>,
}

impl Default for ManagerUi {
    fn default() -> Self {
        Self {
            call: None,
            starting: false,
            talk_after_start: false,
            say_after_start: None,
            early: Vec::new(),
            phase: CallPhase::Idle,
            error: None,
            awaiting: None,
            mic: Mic::Off,
            capture_gen: 0,
            capture_seq: 0,
            mic_level: 0.0,
            live_call: false,
            voice_ok: None,
            speaking: false,
            queue: VecDeque::new(),
            playing: None,
            muted_turn: None,
            last_turn: None,
            token: 0,
            playback: crate::speech::Playback::default(),
            tts_ok: true,
            caption: None,
            field: None,
            card: None,
            consent: None,
            back: Vec::new(),
            reported: (false, false),
            orb: Orb::default(),
            silent: false,
            pin: None,
            thinking_since: None,
            cued: false,
            anim_until: None,
            cursor: Cell::new(None),
        }
    }
}

impl ManagerUi {
    pub fn focused_card(&self) -> bool {
        self.card
            .as_ref()
            .is_some_and(|c| c.focused && !c.collapsed)
    }

    pub fn listening(&self) -> bool {
        matches!(self.mic, Mic::Checking | Mic::Opening | Mic::Listening)
    }

    /// Something of the Manager is on screen or about to be.
    pub fn live(&self) -> bool {
        self.call.is_some()
            || self.starting
            || self.caption.is_some()
            || self.card.is_some()
            || self.consent.is_some()
            || self.orb.visible()
    }

    /// The exchange is still going on: the orb stays and the caption stays.
    fn exchange(&self, now: Instant) -> bool {
        self.listening()
            || matches!(self.mic, Mic::Stopping | Mic::Transcribing)
            || self.speaking
            || self.playing.is_some()
            || !self.queue.is_empty()
            || self.phase != CallPhase::Idle
            || self
                .awaiting
                .is_some_and(|at| now.saturating_duration_since(at) < AWAITING)
            || self.field.is_some()
            || self.focused_card()
    }

    fn touch(&mut self, now: Instant) {
        self.anim_until = Some(now + CARD_ENTER.max(CAPTION_FADE) + Duration::from_millis(60));
    }

    fn set_caption(&mut self, now: Instant, user: Option<String>, manager: Option<String>) {
        self.caption = Some(Caption {
            user,
            manager,
            since: now,
        });
        self.touch(now);
    }

    /// What was said and what was answered, both lines kept apart.
    fn caption_mut(&mut self, now: Instant) -> &mut Caption {
        self.touch(now);
        let caption = self.caption.get_or_insert(Caption {
            user: None,
            manager: None,
            since: now,
        });
        caption.since = now;
        caption
    }

    /// How long reading `text` takes, for a sentence that is shown but not spoken.
    fn reading_time(text: &str) -> Duration {
        let secs = (text.chars().count() as f32 / 14.0).clamp(1.4, 7.0);
        Duration::from_secs_f32(secs)
    }
}

/// One row of a card as the client draws it.
#[derive(Debug, Clone, PartialEq)]
pub struct RowView {
    pub n: usize,
    pub session_id: String,
    pub handle: String,
    pub title: String,
    pub project: String,
    /// What the state is called on screen.
    pub state: String,
    pub kind: RowKind,
    pub why: String,
    /// Board column: 0 needs you, 1 running, 2 finished.
    pub column: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RowKind {
    Needs,
    Running,
    Done,
    Idle,
    Failed,
}

fn kind_of_payload(state: &str) -> (RowKind, &'static str) {
    match state {
        "needs_approval" => (RowKind::Needs, "needs approval"),
        "needs_answer" => (RowKind::Needs, "needs an answer"),
        "waiting" | "needs_you" => (RowKind::Needs, "waiting"),
        "blocked" => (RowKind::Needs, "blocked"),
        "failed" | "error" => (RowKind::Failed, "failed"),
        "working" | "running" => (RowKind::Running, "running"),
        "archived" => (RowKind::Done, "archived"),
        "done" | "finished" | "finished_unread" => (RowKind::Done, "finished"),
        _ => (RowKind::Idle, "idle"),
    }
}

impl App {
    // ---- terms, keys, mouse -----------------------------------------------------

    /// The `manager.*` terms of the manifest.
    pub fn manager_term(&self, name: &str) -> bool {
        let m = &self.ui.manager;
        match name {
            "manager.in_call" => m.call.is_some(),
            "manager.listening" => m.listening(),
            "manager.speaking" => m.speaking,
            "manager.field" => m.field.is_some(),
            "manager.card_focused" => m.focused_card(),
            "manager.engaged" => m.listening() || m.focused_card(),
            _ => false,
        }
    }

    /// Anything for the Manager's keys to act on besides its shortcuts.
    fn manager_engaged_input(&self) -> bool {
        let m = &self.ui.manager;
        m.field.is_some() || m.listening() || m.focused_card() || m.speaking
    }

    /// A key before the app sees it. True when the Manager took it. At rest
    /// only the Manager's own shortcuts are taken; every other key, `esc`
    /// included, goes on to the app as if the Manager were not there.
    pub fn manager_key(&mut self, ev: &KeyEvent, name: &str) -> bool {
        if self.blocking().is_some() {
            return false;
        }
        let shifted = ev.modifiers.contains(KeyModifiers::ALT)
            && match ev.code {
                KeyCode::Char('M') => true,
                KeyCode::Char('m') => ev.modifiers.contains(KeyModifiers::SHIFT),
                _ => false,
            };
        if shifted {
            self.run_command("manager.stop");
            return true;
        }
        if self.ui.manager.consent.is_some() {
            // The notice takes every key until it is accepted or cancelled.
            match name {
                "enter" => self.answer_consent(true),
                "esc" => self.answer_consent(false),
                _ => {}
            }
            return true;
        }
        if name == "alt+m" {
            self.run_command("manager.talk");
            return true;
        }
        if !self.manager_engaged_input() {
            return false;
        }
        if name == "esc" {
            self.escape();
            return true;
        }
        if self.ui.manager.field.is_some() {
            if matches!(name, "ctrl+c" | "ctrl+q" | "ctrl+z") {
                return false;
            }
            if name == "enter" {
                self.execute("manager.field_send");
            } else if let Some(field) = self.ui.manager.field.as_mut() {
                crate::app::edit_buffer(field, ev, name, true);
            }
            return true;
        }
        if self.ui.manager.listening() {
            match name {
                "space" | "enter" => {
                    self.execute("manager.send");
                    return true;
                }
                _ => {
                    if let Some(c) = crate::keys::printable(ev) {
                        self.open_field(Some(c));
                        return true;
                    }
                    return false;
                }
            }
        }
        if self.ui.manager.focused_card() {
            if name == "space" {
                self.execute("manager.card_mic");
                return true;
            }
            if let Some(c) = crate::keys::printable(ev) {
                if let Some(n) = c.to_digit(10).filter(|n| *n >= 1) {
                    if self.card_max_n() > 0 {
                        self.pick_row(n as usize);
                        return true;
                    }
                }
                self.open_field(Some(c));
                return true;
            }
        }
        false
    }

    /// A mouse press on one of the Manager's own targets.
    pub fn manager_mouse(&mut self, ev: &MouseEvent) -> bool {
        if !matches!(ev.kind, MouseEventKind::Down(MouseButton::Left)) {
            return false;
        }
        let Some(target) = self.hits.at(ev.column, ev.row).cloned() else {
            return false;
        };
        match target {
            Target::ManagerOrb => self.run_command("manager.talk"),
            Target::ManagerScrim => self.execute("manager.card_dismiss"),
            Target::ManagerRow(n) => self.pick_row(n),
            Target::ManagerCard => self.focus_card(),
            _ => return false,
        }
        true
    }

    // ---- commands ------------------------------------------------------------------

    pub fn manager_execute(&mut self, id: &str) {
        match id {
            "manager.talk" => self.talk(),
            "manager.start" => self.start_call(false),
            "manager.open_session" => self.open_manager_session(),
            "manager.stop" => self.stop_call(),
            "manager.send" => self.send_recording(),
            "manager.field_send" => self.send_field(),
            "manager.card_mic" => self.open_mic(),
            "manager.listen_cancel" => self.cancel_listening(),
            "manager.speech_stop" => self.stop_speech(),
            "manager.field_close" => self.close_field(),
            "manager.card_dismiss" => self.dismiss_card(),
            "manager.field_open" => self.open_field(None),
            _ => {}
        }
    }

    /// `/manager`, `/manager stop` and `/manager <text>`. True when handled.
    pub fn manager_slash(&mut self, arg: &str) -> bool {
        let arg = arg.trim();
        match arg {
            "" => self.run_command("manager.start"),
            "stop" => self.run_command("manager.stop"),
            text => {
                let now = self.clock;
                if self.ui.manager.call.is_some() {
                    self.say(text.to_owned(), "typed", now);
                } else {
                    self.ui.manager.say_after_start = Some(text.to_owned());
                    self.start_call(false);
                }
            }
        }
        true
    }

    fn call_core(&mut self, tag: &str, path: &str, body: Value) {
        self.effects
            .push(Effect::Sync(SyncCommand::Manager(ManagerCall {
                tag: tag.to_owned(),
                method: "POST",
                path: path.to_owned(),
                body: Some(body),
                with_stream: true,
            })));
    }

    /// GPT-Live mode: Core opens its call, then this client carries the microphone and speaker.
    fn start_live(&mut self) {
        self.ui.manager.live_call = true;
        self.ui.manager.touch(self.clock);
        let Some(stream_id) = crate::sync::current_stream() else {
            self.ui.manager.live_call = false;
            self.ui.manager.error = Some(Failure {
                code: "live_failed".into(),
                message: "Core is not connected, so the voice call could not start.".into(),
                fix: "The call is still open for typing; start it again to retry the voice.".into(),
            });
            return;
        };
        self.effects
            .push(Effect::Api(ApiCall::ManagerLive { stream_id }));
    }

    /// The live call's audio is down on this side: release it and clear what depended on it.
    fn end_live(&mut self) {
        if self.ui.manager.live_call {
            crate::live::close();
            let m = &mut self.ui.manager;
            m.live_call = false;
            m.speaking = false;
            m.mic_level = 0.0;
        }
    }

    /// The Manager button: ask Core for the Manager's current episode, then open it in the inspector (the one place
    /// its full context can be read, since the Manager has no timeline here).
    fn open_manager_session(&mut self) {
        if self.state.core.is_none() {
            self.toast(Level::Info, "Not connected to Core");
            return;
        }
        self.effects
            .push(Effect::Sync(SyncCommand::Manager(ManagerCall {
                tag: "episode".into(),
                method: "GET",
                path: "/api/manager/episode".into(),
                body: None,
                with_stream: false,
            })));
    }

    fn talk(&mut self) {
        let now = self.clock;
        if self.ui.manager.call.is_none() {
            return self.start_call(true);
        }
        if self.ui.manager.live_call {
            // The call listens by itself: the key only interrupts what the voice is saying.
            crate::live::interrupt();
            self.ui.manager.touch(now);
            self.call_core("cancel", "/api/manager/cancel", json!({}));
            return;
        }
        if self
            .ui
            .manager
            .card
            .as_ref()
            .is_some_and(|c| !c.focused || c.collapsed)
        {
            return self.focus_card();
        }
        let m = &self.ui.manager;
        if m.listening() {
            return self.send_recording();
        }
        if matches!(m.mic, Mic::Stopping | Mic::Transcribing) {
            return;
        }
        let busy = m.speaking
            || m.playing.is_some()
            || !m.queue.is_empty()
            || m.phase != CallPhase::Idle
            || m.awaiting.is_some();
        if busy {
            // Barge-in: stop what is being said and the turn if it is safe.
            self.silence(now);
            self.call_core("cancel", "/api/manager/cancel", json!({}));
        }
        self.open_mic();
    }

    fn start_call(&mut self, talk: bool) {
        if self.ui.manager.call.is_some() {
            if talk {
                self.open_mic();
            }
            return;
        }
        let m = &mut self.ui.manager;
        m.talk_after_start |= talk;
        if m.starting {
            return;
        }
        m.starting = true;
        if self.ui.sound && !self.ui.manager.silent {
            self.effects.push(Effect::Sound(Sound::ManagerStart));
        }
        self.call_core("start", "/api/manager/start", json!({ "client_id": "TUI" }));
    }

    fn stop_call(&mut self) {
        if self.ui.manager.call.is_none() {
            return;
        }
        self.call_core("stop", "/api/manager/stop", json!({}));
        self.clear_all();
    }

    /// Drop everything the Manager had on screen and in flight. The orb is
    /// left to ease out.
    fn clear_all(&mut self) {
        self.end_live();
        let now = self.clock;
        let (listening, generation) = {
            let m = &self.ui.manager;
            (
                !matches!(m.mic, Mic::Off | Mic::Transcribing),
                m.capture_gen,
            )
        };
        if listening && generation != 0 {
            self.effects
                .push(Effect::Dictation(CaptureCommand::Cancel { generation }));
        }
        if self.ui.manager.speaking || self.ui.manager.playing.is_some() {
            self.effects.push(Effect::Api(ApiCall::ManagerSpeakStop));
        }
        let m = &mut self.ui.manager;
        m.call = None;
        m.starting = false;
        m.talk_after_start = false;
        m.say_after_start = None;
        m.early.clear();
        m.phase = CallPhase::Idle;
        m.error = None;
        m.awaiting = None;
        m.mic = Mic::Off;
        m.mic_level = 0.0;
        m.speaking = false;
        m.queue.clear();
        m.playing = None;
        m.muted_turn = None;
        m.tts_ok = true;
        m.caption = None;
        m.field = None;
        m.card = None;
        m.back.clear();
        m.reported = (false, false);
        m.thinking_since = None;
        m.cued = false;
        m.touch(now);
    }

    // ---- microphone -------------------------------------------------------------------

    fn open_mic(&mut self) {
        if self.ui.manager.call.is_none() {
            return;
        }
        if self.ui.dictation.phase != crate::dictation::Phase::Idle {
            self.cancel_dictation_for_manager();
        }
        let now = self.clock;
        let m = &mut self.ui.manager;
        m.field = None;
        m.mic_level = 0.0;
        m.mic = Mic::Checking;
        m.touch(now);
        if m.silent {
            m.mic = Mic::Listening;
            return self.sync_activity();
        }
        match m.voice_ok {
            Some(true) => self.launch_mic(),
            Some(false) => self.voice_unavailable(),
            None => {
                m.token += 1;
                let token = m.token;
                self.effects
                    .push(Effect::Api(ApiCall::ManagerVoice { token }));
            }
        }
    }

    fn cancel_dictation_for_manager(&mut self) {
        // Dictation into the composer and the Manager never share the microphone.
        let generation = self.ui.dictation.generation;
        self.effects
            .push(Effect::Dictation(CaptureCommand::Cancel { generation }));
        self.ui.dictation.cancel();
    }

    fn launch_mic(&mut self) {
        let m = &mut self.ui.manager;
        m.capture_seq += 1;
        m.capture_gen = CAPTURE_BASE + m.capture_seq;
        m.mic = Mic::Opening;
        let generation = m.capture_gen;
        self.effects
            .push(Effect::Dictation(CaptureCommand::Start { generation }));
    }

    /// Voice cannot be used: the typed field opens and the caption says why.
    fn voice_unavailable(&mut self) {
        let now = self.clock;
        let m = &mut self.ui.manager;
        m.mic = Mic::Off;
        m.field = Some(Composer::new());
        m.set_caption(now, None, Some(VOICE_UNAVAILABLE.to_owned()));
        self.sync_activity();
    }

    fn send_recording(&mut self) {
        let m = &mut self.ui.manager;
        match m.mic {
            Mic::Listening => {
                m.mic = Mic::Stopping;
                let generation = m.capture_gen;
                if m.silent {
                    // Nothing was recorded; the pretend microphone just closes.
                    m.mic = Mic::Off;
                } else {
                    self.effects
                        .push(Effect::Dictation(CaptureCommand::Stop { generation }));
                }
                self.sync_activity();
            }
            Mic::Checking | Mic::Opening => self.cancel_listening(),
            _ => {}
        }
    }

    fn cancel_listening(&mut self) {
        let m = &mut self.ui.manager;
        if !m.listening() {
            return;
        }
        let generation = m.capture_gen;
        let opened = m.mic != Mic::Checking;
        m.mic = Mic::Off;
        m.mic_level = 0.0;
        if opened && generation != 0 && !m.silent {
            self.effects
                .push(Effect::Dictation(CaptureCommand::Cancel { generation }));
        }
        self.sync_activity();
    }

    /// A capture event for the Manager's own generation.
    pub fn manager_capture(&mut self, event: CaptureEvent) {
        let now = self.clock;
        let generation = match &event {
            CaptureEvent::Started { generation }
            | CaptureEvent::Cancelled { generation }
            | CaptureEvent::Failed { generation, .. }
            | CaptureEvent::CueWarning { generation, .. }
            | CaptureEvent::Level { generation, .. }
            | CaptureEvent::Preview { generation, .. }
            | CaptureEvent::Stopped { generation, .. } => *generation,
        };
        if generation != self.ui.manager.capture_gen {
            return;
        }
        match event {
            CaptureEvent::Started { .. } => {
                if self.ui.manager.mic == Mic::Opening {
                    self.ui.manager.mic = Mic::Listening;
                    self.sync_activity();
                }
            }
            CaptureEvent::Level { level, .. } => {
                self.ui.manager.mic_level = (level * MIC_GAIN).clamp(0.0, 1.0);
            }
            CaptureEvent::Preview { .. } | CaptureEvent::CueWarning { .. } => {}
            CaptureEvent::Stopped { seconds, wav, .. } => {
                let m = &mut self.ui.manager;
                if !matches!(m.mic, Mic::Stopping | Mic::Listening) {
                    return;
                }
                m.mic_level = 0.0;
                if seconds < crate::dictation::MIN_SECONDS {
                    m.mic = Mic::Off;
                } else {
                    m.mic = Mic::Transcribing;
                    m.token += 1;
                    let token = m.token;
                    m.touch(now);
                    self.effects
                        .push(Effect::Api(ApiCall::ManagerTranscribe { wav, token }));
                }
                self.sync_activity();
            }
            CaptureEvent::Cancelled { .. } => {
                let m = &mut self.ui.manager;
                if m.listening() || m.mic == Mic::Stopping {
                    m.mic = Mic::Off;
                    m.mic_level = 0.0;
                }
                self.sync_activity();
            }
            CaptureEvent::Failed { .. } => {
                // The microphone was refused or is missing.
                self.ui.manager.voice_ok = Some(false);
                self.voice_unavailable();
            }
        }
    }

    /// The reply to `ManagerVoice`, `ManagerTranscribe` or `ManagerSpeak`.
    pub fn manager_done(&mut self, done: ActionDone) {
        let now = self.clock;
        match done.call {
            ApiCall::ManagerVoice { token } => {
                if token != self.ui.manager.token || self.ui.manager.mic != Mic::Checking {
                    return;
                }
                let ready = done.result.as_ref().ok().is_some_and(|d| {
                    d.voice.as_ref().is_some_and(|v| {
                        matches!(v["stt"]["state"].as_str(), Some("ready" | "loaded"))
                    })
                });
                self.ui.manager.voice_ok = Some(ready);
                if ready {
                    self.launch_mic();
                } else {
                    self.voice_unavailable();
                }
            }
            ApiCall::ManagerLive { .. } => {
                if !self.ui.manager.live_call {
                    crate::live::close();
                    return;
                }
                if done.result.is_ok() {
                    if self.ui.sound && !self.ui.manager.silent {
                        self.effects.push(Effect::Sound(Sound::LiveConnected));
                    }
                } else if let Err(message) = done.result {
                    // The call stays open for typing; say why the voice did not start.
                    self.ui.manager.live_call = false;
                    self.ui.manager.error = Some(Failure {
                        code: "live_failed".into(),
                        message,
                        fix: "The call is still open for typing; start it again to retry the voice.".into(),
                    });
                }
            }
            ApiCall::ManagerTranscribe { token, .. } => {
                if token != self.ui.manager.token || self.ui.manager.mic != Mic::Transcribing {
                    return;
                }
                self.ui.manager.mic = Mic::Off;
                match done.result {
                    Ok(done) => {
                        let text = done.transcript.unwrap_or_default().trim().to_owned();
                        if text.is_empty() {
                            self.ui.manager.set_caption(
                                now,
                                None,
                                Some("I didn't catch that.".to_owned()),
                            );
                            self.sync_activity();
                        } else {
                            self.say(text, "speech", now);
                        }
                    }
                    Err(_) => {
                        self.ui.manager.voice_ok = Some(false);
                        self.voice_unavailable();
                    }
                }
            }
            ApiCall::ManagerSpeak { token, text, .. } => {
                let Some(playing) = self.ui.manager.playing.clone() else {
                    return;
                };
                if playing.token != token {
                    return;
                }
                if done.result.is_err() {
                    // TTS is not available: show the text for about as long as it takes to read.
                    let m = &mut self.ui.manager;
                    m.tts_ok = false;
                    m.set_caption_manager(now, text);
                    if let Some(p) = m.playing.as_mut() {
                        p.audio = false;
                        p.shown = true;
                        p.until = Some(now + ManagerUi::reading_time(&p.text));
                    }
                    return;
                }
                let m = &mut self.ui.manager;
                if !playing.shown {
                    m.set_caption_manager(now, playing.text);
                }
                m.playing = None;
                self.pump_speech(now);
            }
            _ => {}
        }
        self.sync_activity();
    }

    // ---- say, speech --------------------------------------------------------------------

    fn say(&mut self, text: String, source: &str, now: Instant) {
        let m = &mut self.ui.manager;
        m.set_caption(now, Some(text.clone()), None);
        m.awaiting = Some(now);
        m.muted_turn = None;
        self.call_core(
            "say",
            "/api/manager/say",
            json!({ "text": text, "source": source }),
        );
    }

    /// Stop what is being said here and forget what was queued.
    fn silence(&mut self, now: Instant) {
        let m = &mut self.ui.manager;
        let audio = m.speaking || m.playing.as_ref().is_some_and(|p| p.audio);
        m.queue.clear();
        m.playing = None;
        m.speaking = false;
        m.awaiting = None;
        m.touch(now);
        if audio {
            self.effects.push(Effect::Api(ApiCall::ManagerSpeakStop));
        }
        self.sync_activity();
    }

    /// `esc` while speaking: the speech stops, the turn's work goes on, and the
    /// rest of this turn's sentences are only shown.
    fn stop_speech(&mut self) {
        let now = self.clock;
        let turn = self.ui.manager.last_turn.clone();
        self.silence(now);
        self.ui.manager.muted_turn = turn;
    }

    /// Start the next queued sentence if none is playing.
    fn pump_speech(&mut self, now: Instant) {
        let m = &mut self.ui.manager;
        if m.playing.is_some() {
            return;
        }
        let Some(sentence) = m.queue.pop_front() else {
            m.speaking = false;
            m.touch(now);
            return;
        };
        let muted = m.muted_turn.as_deref() == Some(sentence.turn.as_str());
        m.token += 1;
        let token = m.token;
        if m.silent || !m.tts_ok || muted {
            m.set_caption_manager(now, sentence.text.clone());
            m.playing = Some(Playing {
                token,
                text: sentence.text.clone(),
                audio: false,
                shown: true,
                until: Some(now + ManagerUi::reading_time(&sentence.text)),
                proactive: sentence.proactive,
            });
            return;
        }
        m.playing = Some(Playing {
            token,
            text: sentence.text.clone(),
            audio: true,
            shown: false,
            until: None,
            proactive: sentence.proactive,
        });
        let playback = m.playback.clone();
        self.effects.push(Effect::Api(ApiCall::ManagerSpeak {
            text: sentence.text,
            token,
            playback,
        }));
    }

    // ---- field --------------------------------------------------------------------------

    fn open_field(&mut self, first: Option<char>) {
        if self.ui.manager.call.is_none() {
            return;
        }
        if self.ui.manager.listening() {
            self.cancel_listening();
        }
        let now = self.clock;
        let m = &mut self.ui.manager;
        let mut field = Composer::new();
        if let Some(c) = first {
            field.insert_char(c);
        }
        m.field = Some(field);
        m.touch(now);
    }

    fn send_field(&mut self) {
        let now = self.clock;
        let Some(field) = self.ui.manager.field.take() else {
            return;
        };
        let text = field.text().trim().to_owned();
        if text.is_empty() {
            return;
        }
        self.say(text, "typed", now);
    }

    fn close_field(&mut self) {
        let now = self.clock;
        let m = &mut self.ui.manager;
        m.field = None;
        m.touch(now);
    }

    // ---- cards ------------------------------------------------------------------------------

    fn focus_card(&mut self) {
        let now = self.clock;
        if let Some(card) = self.ui.manager.card.as_mut() {
            card.focused = true;
            card.collapsed = false;
            card.shown = now;
        }
        self.ui.manager.touch(now);
    }

    fn dismiss_card(&mut self) {
        let now = self.clock;
        let Some(card) = self.ui.manager.card.take() else {
            return;
        };
        self.ui.manager.touch(now);
        self.call_core(
            "dismiss",
            "/api/manager/card/dismiss",
            json!({ "card_id": card.id }),
        );
    }

    /// The highest row number on the card on the stage, 0 when it has none.
    pub fn card_max_n(&self) -> usize {
        self.ui.manager.card.as_ref().map_or(0, |c| {
            self.card_rows(&c.value)
                .iter()
                .map(|r| r.n)
                .max()
                .unwrap_or(0)
        })
    }

    /// Rows of a `board` or `sessions` card, drawn from the live store where it
    /// has the session and from the payload where it does not. A board row
    /// moves column with its session.
    pub fn card_rows(&self, card: &Value) -> Vec<RowView> {
        let mut out: Vec<RowView> = Vec::new();
        let mut push = |row: &Value, hint: usize| {
            let id = row["session_id"].as_str().unwrap_or("").to_owned();
            let live = self.state.model.sessions.get(&id);
            let title = live
                .map(|s| s.title.clone())
                .filter(|t| !t.is_empty())
                .unwrap_or_else(|| row["title"].as_str().unwrap_or("").to_owned());
            let (kind, word) = match live {
                Some(_) => match self.session_state(&id) {
                    "needs input" => (RowKind::Needs, "needs input"),
                    "failed" => (RowKind::Failed, "failed"),
                    "running" => (RowKind::Running, "running"),
                    "archived" => (RowKind::Done, "archived"),
                    "finished" => (RowKind::Done, "finished"),
                    _ => (RowKind::Idle, "idle"),
                },
                None => kind_of_payload(row["state"].as_str().unwrap_or("")),
            };
            let project = live
                .and_then(|s| self.state.model.projects.get(&s.project_id))
                .map(|p| p.name.clone())
                .unwrap_or_else(|| row["project"].as_str().unwrap_or("").to_owned());
            let column = match live {
                Some(_) => match kind {
                    RowKind::Needs | RowKind::Failed => 0,
                    RowKind::Running => 1,
                    _ => 2,
                },
                None => hint,
            };
            out.push(RowView {
                n: row["n"].as_u64().unwrap_or(0) as usize,
                session_id: id,
                handle: row["handle"].as_str().unwrap_or("").to_owned(),
                title,
                project,
                state: word.to_owned(),
                kind,
                why: row["why"].as_str().unwrap_or("").to_owned(),
                column,
            });
        };
        match card["type"].as_str() {
            Some("board") => {
                for (index, column) in ["needs_you", "running", "finished"].iter().enumerate() {
                    for row in card["columns"][*column].as_array().into_iter().flatten() {
                        push(row, index);
                    }
                }
            }
            Some("sessions") => {
                for row in card["rows"].as_array().into_iter().flatten() {
                    push(row, 0);
                }
            }
            _ => {}
        }
        out.sort_by_key(|r| r.n);
        out
    }

    /// Open row `n` of the card as a click would, or say it is gone.
    fn pick_row(&mut self, n: usize) {
        let now = self.clock;
        let Some(card) = self.ui.manager.card.as_ref() else {
            return;
        };
        let rows = self.card_rows(&card.value);
        let Some(row) = rows.into_iter().find(|r| r.n == n) else {
            return;
        };
        if !self.state.model.sessions.contains_key(&row.session_id) {
            self.ui
                .manager
                .set_caption(now, None, Some(ROW_GONE.to_owned()));
            return;
        }
        self.open_session(&row.session_id);
        self.dismiss_card();
    }

    // ---- events from Core -----------------------------------------------------------------------

    /// Restore the call after a reconnect: ask Core what it holds.
    pub fn manager_restore(&mut self) {
        if self.ui.manager.call.is_none() {
            return;
        }
        self.effects
            .push(Effect::Sync(SyncCommand::Manager(ManagerCall {
                tag: "restore".into(),
                method: "GET",
                path: "/api/manager".into(),
                body: None,
                with_stream: false,
            })));
    }

    /// A `manager.*` event.
    pub fn manager_event(&mut self, event: &Event) {
        if !event.kind.starts_with("manager.") || event.kind == "manager.acted" {
            return;
        }
        let call_id = event.data["call_id"].as_str().unwrap_or("");
        let mine = self.ui.manager.call.as_ref().map(|c| c.id.clone());
        match mine {
            None => {
                // A call being started: its events can beat the reply.
                if self.ui.manager.starting && self.ui.manager.early.len() < 64 {
                    self.ui.manager.early.push(event.clone());
                }
            }
            Some(id) if id != call_id => {
                if event.kind == "manager.state" && event.data["active"].as_bool() == Some(true) {
                    let holder = event.data["holder"].as_str().unwrap_or("another client");
                    self.moved_to(holder.to_owned());
                }
            }
            Some(_) => self.apply_manager_event(event),
        }
    }

    fn moved_to(&mut self, holder: String) {
        let now = self.clock;
        self.clear_all();
        self.ui
            .manager
            .set_caption(now, None, Some(format!("Moved to {holder}")));
    }

    fn apply_manager_event(&mut self, event: &Event) {
        let now = self.clock;
        let d = &event.data;
        match event.kind.as_str() {
            "manager.state" => {
                if d["active"].as_bool() == Some(false) {
                    return self.clear_all();
                }
                let m = &mut self.ui.manager;
                m.awaiting = None;
                let phase = match d["phase"].as_str() {
                    Some("thinking") => CallPhase::Thinking,
                    Some("replying") => CallPhase::Replying,
                    _ => CallPhase::Idle,
                };
                if phase == CallPhase::Idle {
                    m.thinking_since = None;
                    m.cued = false;
                } else if m.phase == CallPhase::Idle {
                    m.thinking_since = Some(now);
                }
                m.phase = phase;
                if let Some(call) = m.call.as_mut() {
                    if let Some(holder) = d["holder"].as_str() {
                        call.holder = holder.to_owned();
                    }
                }
                m.error = d["error"].is_object().then(|| Failure {
                    code: d["error"]["code"].as_str().unwrap_or("").to_owned(),
                    message: d["error"]["message"].as_str().unwrap_or("").to_owned(),
                    fix: d["error"]["fix"].as_str().unwrap_or("").to_owned(),
                });
                m.touch(now);
            }
            "manager.caption" => {
                let text = d["text"].as_str().unwrap_or("").to_owned();
                let role = d["role"].as_str().unwrap_or("manager");
                let caption = self.ui.manager.caption_mut(now);
                if role == "user" {
                    caption.user = Some(text);
                    caption.manager = None;
                } else {
                    caption.manager = Some(text);
                }
            }
            "manager.speech" => {
                let text = d["text"].as_str().unwrap_or("").trim().to_owned();
                if text.is_empty() {
                    return;
                }
                let turn = d["turn_id"].as_str().unwrap_or("").to_owned();
                let seq = d["seq"].as_u64().unwrap_or(0);
                let m = &mut self.ui.manager;
                m.last_turn = Some(turn.clone());
                m.awaiting = None;
                let at = m
                    .queue
                    .iter()
                    .position(|s| s.turn == turn && s.seq > seq)
                    .unwrap_or(m.queue.len());
                m.queue.insert(
                    at,
                    Sentence {
                        turn,
                        seq,
                        text,
                        proactive: d["proactive"].as_bool() == Some(true),
                    },
                );
                self.pump_speech(now);
            }
            "manager.card" => {
                let m = &mut self.ui.manager;
                m.touch(now);
                match d["card"].as_object() {
                    None => m.card = None,
                    Some(_) => {
                        let value = d["card"].clone();
                        m.card = Some(CardState {
                            id: value["id"].as_str().unwrap_or("").to_owned(),
                            focused: value["focused"].as_bool().unwrap_or(true),
                            value,
                            shown: now,
                            collapsed: false,
                            scroll: 0,
                        });
                        // A new card closes the field: the moment has moved on.
                        m.field = None;
                    }
                }
            }
            "manager.navigate" => self.manager_navigate(d),
            _ => {}
        }
        self.sync_activity();
    }

    /// `manager.navigate`: move the view as a user click would, answer, then report the view.
    fn manager_navigate(&mut self, d: &Value) {
        let now = self.clock;
        let request = d["request_id"].as_str().unwrap_or("").to_owned();
        let back = d["back"].as_bool() == Some(true);
        let before = self.view_now();
        let target = if back {
            self.ui.manager.back.pop().or_else(|| {
                Some(View {
                    project: d["project"].as_str().map(str::to_owned),
                    session: d["session"].as_str().map(str::to_owned),
                    panel: d["panel"].as_str().map(str::to_owned),
                })
            })
        } else {
            Some(View {
                project: d["project"].as_str().map(str::to_owned),
                session: d["session"].as_str().map(str::to_owned),
                panel: d["panel"].as_str().map(str::to_owned),
            })
        };
        let result = match target {
            Some(view) => self.go_to(&view),
            None => Err("There is nowhere to go back to.".to_owned()),
        };
        if result.is_ok() && !back {
            let m = &mut self.ui.manager;
            m.back.push(before);
            if m.back.len() > 8 {
                m.back.remove(0);
            }
        }
        // Navigation closes any open card.
        self.ui.manager.card = None;
        self.ui.manager.touch(now);
        let body = match &result {
            Ok(()) => json!({ "request_id": request, "ok": true }),
            Err(reason) => json!({ "request_id": request, "ok": false, "reason": reason }),
        };
        self.call_core("navigate", "/api/manager/navigate/result", body);
        // The answer first, then the new view.
        self.sync_view();
    }

    fn view_now(&self) -> View {
        let v = self.client_view();
        View {
            project: v.project,
            session: v.session,
            panel: v.panel,
        }
    }

    fn go_to(&mut self, view: &View) -> Result<(), String> {
        if let Some(session) = &view.session {
            if !self.state.model.sessions.contains_key(session) {
                return Err(ROW_GONE.to_owned());
            }
            self.open_session(session);
        } else if let Some(project) = &view.project {
            let Some(index) = self.rail_project_ids().iter().position(|id| id == project) else {
                return Err("That project no longer exists.".to_owned());
            };
            self.rail_activate(index);
        }
        if let Some(panel) = view.panel.as_deref() {
            if matches!(panel, "files" | "file" | "diff") {
                self.execute("sidebar.tab.files");
            }
        }
        Ok(())
    }

    /// The answer to a call made through the supervisor.
    pub fn manager_reply(&mut self, tag: &str, result: Result<Value, ManagerError>) {
        let now = self.clock;
        match tag {
            "start" => {
                let talk = std::mem::take(&mut self.ui.manager.talk_after_start);
                let say = self.ui.manager.say_after_start.take();
                self.ui.manager.starting = false;
                match result {
                    Ok(body) => {
                        self.ui.manager.call = Some(Call {
                            id: body["call_id"].as_str().unwrap_or("").to_owned(),
                            holder: body["holder"].as_str().unwrap_or("").to_owned(),
                        });
                        self.ui.manager.tts_ok = true;
                        for event in std::mem::take(&mut self.ui.manager.early) {
                            self.manager_event(&event);
                        }
                        let mode_live = body["mode"].as_str() == Some("live");
                        if mode_live {
                            self.start_live();
                        }
                        if let Some(text) = say {
                            self.say(text, "typed", now);
                        }
                        if self.ui.manager.live_call {
                            // GPT-Live listens by itself; the talk key only interrupts.
                        } else if talk {
                            self.open_mic();
                        } else if self.ui.manager.silent {
                            // Nothing to announce in a captured frame.
                        } else if let Some(key) = self
                            .manifest
                            .command("manager.talk")
                            .and_then(|c| c.tui_key().map(str::to_owned))
                        {
                            self.toast(Level::Info, format!("Manager is on. {key} to talk."));
                        }
                    }
                    Err(e) if e.code.as_deref() == Some("consent_required") => {
                        self.ui.manager.early.clear();
                        self.ask_consent(&e.body, talk, say);
                    }
                    Err(e) => {
                        self.ui.manager.early.clear();
                        if e.code.as_deref() == Some("manager_disabled") {
                            self.toast(Level::Info, MANAGER_OFF);
                        } else {
                            self.toast(Level::Error, format!("Manager: {}", e.message));
                        }
                    }
                }
            }
            "consent" => {
                let Some(held) = self.ui.manager.consent.take() else {
                    return;
                };
                match result {
                    Ok(_) => {
                        self.ui.manager.say_after_start = held.say;
                        self.start_call(held.talk);
                    }
                    Err(e) => self.toast(Level::Error, format!("Manager: {}", e.message)),
                }
            }
            "say" => {
                if let Err(e) = &result {
                    if e.code.as_deref() == Some("consent_required") {
                        let text = self
                            .ui
                            .manager
                            .caption
                            .as_ref()
                            .and_then(|c| c.user.clone());
                        self.ask_consent(&e.body, false, text);
                        self.ui.manager.awaiting = None;
                        return;
                    }
                }
                if let Err(e) = result {
                    let m = &mut self.ui.manager;
                    m.awaiting = None;
                    m.set_caption(now, None, Some(e.message));
                }
            }
            "episode" => {
                let Ok(body) = result else {
                    self.toast(Level::Error, "Could not read the Manager's session from Core");
                    return;
                };
                // Newest first. The inspector takes the token and the session in its URL fragment.
                let Some(id) = body.get(0).and_then(|e| e["id"].as_str()) else {
                    self.toast(
                        Level::Info,
                        "The Manager has no session yet. Start it with alt+m, then talk to it.",
                    );
                    return;
                };
                let Some(info) = self.state.core.as_ref() else {
                    self.toast(Level::Info, "Not connected to Core");
                    return;
                };
                // The token is read from the daemon's own file at the moment of use, not kept in the app.
                let Some(token) =
                    crate::discovery::read_info(std::path::Path::new(&info.home)).map(|d| d.token)
                else {
                    self.toast(Level::Info, "Could not read Core's token to open the inspector");
                    return;
                };
                // Level 3 (Requests) shows what the model is given: the state snapshot, the recap and each request.
                let url = format!(
                    "{}/inspector#token={token}&session={id}&level=requests",
                    info.url.trim_end_matches('/')
                );
                self.effects.push(Effect::OpenUrl(url));
            }
            "restore" => {
                let Ok(body) = result else { return };
                let mine = self.ui.manager.call.as_ref().map(|c| c.id.clone());
                match body["call"]["call_id"].as_str() {
                    Some(id) if mine.as_deref() == Some(id) => {}
                    Some(_) => {
                        let holder = body["call"]["holder"].as_str().unwrap_or("another client");
                        self.moved_to(holder.to_owned());
                    }
                    None => self.clear_all(),
                }
            }
            _ => {
                // activity, cancel, dismiss, navigate result, stop: best effort.
                if let Err(e) = result {
                    if e.code.as_deref() == Some("not_holder") {
                        self.clear_all();
                    }
                }
            }
        }
        self.sync_activity();
    }

    /// Tell Core when the user starts or stops talking, or the Manager speaking starts or ends.
    fn sync_activity(&mut self) {
        let m = &self.ui.manager;
        if m.call.is_none() {
            return;
        }
        let now = (m.mic == Mic::Listening, m.speaking);
        if now == m.reported {
            return;
        }
        self.ui.manager.reported = now;
        self.call_core(
            "activity",
            "/api/manager/activity",
            json!({ "listening": now.0, "speaking": now.1 }),
        );
    }

    // ---- time -----------------------------------------------------------------------------------

    /// What the orb shows now, `None` when it should not be there.
    pub fn orb_target(&self, now: Instant) -> Option<OrbState> {
        let m = &self.ui.manager;
        if let Some(pin) = m.pin {
            return pin.map(|(state, _)| state);
        }
        m.call.as_ref()?;
        if m.live_call {
            let levels = crate::live::levels();
            if levels.as_ref().is_some_and(|l| l.speech() > 0.04) {
                return Some(OrbState::Speaking);
            }
            if levels.is_some() && m.phase == CallPhase::Idle {
                return Some(OrbState::Listening);
            }
        }
        if m.listening() {
            return Some(OrbState::Listening);
        }
        if m.speaking {
            return Some(OrbState::Speaking);
        }
        if m.phase != CallPhase::Idle
            || m.mic == Mic::Transcribing
            || m.mic == Mic::Stopping
            || m.awaiting
                .is_some_and(|at| now.saturating_duration_since(at) < AWAITING)
            || m.playing.as_ref().is_some_and(|p| !p.proactive)
            || m.queue.iter().any(|s| !s.proactive)
        {
            return Some(OrbState::Thinking);
        }
        if m.field.is_some() || m.focused_card() {
            return Some(OrbState::Idle);
        }
        None
    }

    /// The caption word for a state, where the orb cannot say it itself.
    pub fn orb_word(&self, now: Instant) -> Option<&'static str> {
        let plain = self.theme.opts.reduced_motion
            || self.theme.mono()
            || self.theme.opts.tier != crate::theme::Tier::TrueColor
            || self.theme.opts.ascii;
        if !plain {
            return None;
        }
        self.orb_target(now).and_then(OrbState::word)
    }

    /// Is the connection down while a call is held?
    pub fn manager_reconnecting(&self) -> bool {
        self.ui.manager.call.is_some() && !self.state.connection.is_live()
    }

    /// Do frames need to run? Only while something of the Manager is moving.
    pub fn manager_animating(&self, now: Instant) -> bool {
        let m = &self.ui.manager;
        if self.theme.opts.reduced_motion || m.pin.is_some() || !m.live() {
            return false;
        }
        if m.orb.moving(self.orb_target(now)) {
            return true;
        }
        if m.anim_until.is_some_and(|until| now < until) {
            return true;
        }
        m.caption.as_ref().is_some_and(|c| {
            let age = now.saturating_duration_since(c.since);
            !m.exchange(now) && age >= CAPTION_QUIET && age < CAPTION_QUIET + CAPTION_FADE
        })
    }

    /// Advance the orb to `now`. Called by the renderer before it draws.
    pub fn manager_frame(&mut self, now: Instant) {
        if !self.ui.manager.live() {
            return;
        }
        let target = self.orb_target(now);
        let reduced = self.theme.opts.reduced_motion;
        let m = &mut self.ui.manager;
        if let Some(pin) = m.pin {
            match pin {
                Some((state, level)) => m.orb.pin(state, level),
                None => m.orb = Orb::default(),
            }
            return;
        }
        let live = if m.live_call { crate::live::levels() } else { None };
        let raw = match target {
            Some(OrbState::Listening) => live.as_ref().map_or(m.mic_level, |l| l.mic()),
            Some(OrbState::Speaking) => live
                .as_ref()
                .map_or_else(|| m.playback.level(), |l| l.speech()),
            _ => 0.0,
        };
        m.orb.advance(now, target, raw, reduced);
    }

    /// The slow clock (and the frame clock while animating). True when something changed.
    pub fn manager_tick(&mut self, now: Instant) -> bool {
        if !self.ui.manager.live() {
            return false;
        }
        let mut changed = false;
        if self.ui.manager.live_call {
            if let Some(why) = crate::live::take_lost() {
                self.end_live();
                self.ui.manager.error = Some(Failure {
                    code: "live_lost".into(),
                    message: why,
                    fix: "Start the call again.".into(),
                });
                changed = true;
            }
        }
        // Playback has begun: the sentence is now the caption and the orb speaks.
        if let Some(p) = self.ui.manager.playing.as_ref() {
            if p.audio && !p.shown && self.ui.manager.playback.started() {
                let text = p.text.clone();
                let m = &mut self.ui.manager;
                m.set_caption_manager(now, text);
                m.speaking = !m.playing.as_ref().is_some_and(|p| p.proactive);
                if let Some(p) = m.playing.as_mut() {
                    p.shown = true;
                }
                changed = true;
            }
        }
        // A sentence that is only read moves on when its time is up.
        if self
            .ui
            .manager
            .playing
            .as_ref()
            .is_some_and(|p| !p.audio && p.until.is_some_and(|until| now >= until))
        {
            self.ui.manager.playing = None;
            self.pump_speech(now);
            changed = true;
        }
        let m = &mut self.ui.manager;
        // An unfocused card nobody engaged goes back to its caption.
        if let Some(card) = m.card.as_mut() {
            if !card.focused
                && !card.collapsed
                && now.saturating_duration_since(card.shown) >= UNFOCUSED_CARD
            {
                card.collapsed = true;
                let line = card_caption(&card.value);
                m.set_caption(now, None, line);
                changed = true;
            }
        }
        if m.awaiting
            .is_some_and(|at| now.saturating_duration_since(at) >= AWAITING)
        {
            m.awaiting = None;
            changed = true;
        }
        // The exchange has gone quiet: the caption goes, after a fade.
        if !m.exchange(now) {
            if let Some(caption) = &m.caption {
                if now.saturating_duration_since(caption.since) >= CAPTION_QUIET + CAPTION_FADE {
                    m.caption = None;
                    changed = true;
                } else if now.saturating_duration_since(caption.since) >= CAPTION_QUIET {
                    changed = true;
                }
            }
        }
        // A soft cue once thinking has gone on for a second.
        let thinking = m.phase != CallPhase::Idle && !m.speaking && m.playing.is_none();
        if thinking {
            if let Some(since) = m.thinking_since {
                if !m.cued && now.saturating_duration_since(since) >= THINKING_CUE && !m.silent {
                    m.cued = true;
                    if self.ui.sound {
                        self.effects.push(Effect::Sound(Sound::Thinking));
                    }
                }
            }
        }
        if self.ui.manager.orb.visible() {
            changed = true;
        }
        self.sync_activity();
        changed
    }
}

impl ManagerUi {
    fn set_caption_manager(&mut self, now: Instant, text: String) {
        let caption = self.caption_mut(now);
        caption.manager = Some(text);
    }
}

/// The one line a card is remembered by once it has collapsed.
pub fn card_caption(card: &Value) -> Option<String> {
    let handle = card["handle"].as_str().unwrap_or("A session");
    match card["type"].as_str()? {
        "approval" => Some(format!(
            "{handle}: {}",
            card["summary"].as_str().unwrap_or("needs your approval")
        )),
        "question" => Some(format!(
            "{handle}: {}",
            card["question"].as_str().unwrap_or("has a question")
        )),
        _ => card["title"].as_str().map(str::to_owned),
    }
}

// ---- Settings › Manager ---------------------------------------------------------------------

impl App {
    /// The value shown for a Manager row of Settings, from Core's config.
    pub fn manager_setting_value(&self, id: &str) -> Option<String> {
        let values = &self.state.config.as_ref()?["values"]["manager"];
        match id {
            "manager.enabled" => Some(
                if values["enabled"].as_bool().unwrap_or(false) {
                    "on"
                } else {
                    "off"
                }
                .into(),
            ),
            "manager.mode" => Some(values["mode"].as_str().unwrap_or("pipeline").into()),
            "manager.model" => Some(
                values["model"]
                    .as_str()
                    .unwrap_or("copilot/gpt-6-luna")
                    .into(),
            ),
            "manager.proactive" => Some(values["proactive"].as_str().unwrap_or("attention").into()),
            _ => None,
        }
    }

    pub(crate) fn manager_set_config(&mut self, revision: String, path: &str, value: Value) {
        if !self.writable() {
            self.toast(Level::Warning, crate::app::READ_ONLY);
            return;
        }
        self.effects.push(Effect::Sync(SyncCommand::PatchConfig {
            revision,
            path: path.to_owned(),
            value,
        }));
    }

    /// Left and right, or enter: flip the switch or step through the choices.
    pub(crate) fn manager_setting_change(&mut self, id: &str, step: isize) {
        let Some(revision) = self
            .state
            .config
            .as_ref()
            .and_then(|c| c["revision"].as_str().map(str::to_owned))
        else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        match id {
            "manager.enabled" => {
                let on = self.manager_setting_value(id).as_deref() == Some("on");
                self.manager_set_config(revision, "manager.enabled", json!(!on));
            }
            "manager.proactive" => {
                let choices = ["off", "attention", "all"];
                let now = self.manager_setting_value(id).unwrap_or_default();
                let at = choices.iter().position(|c| *c == now).unwrap_or(1) as isize;
                let next = choices[(at + step.signum()).rem_euclid(3) as usize];
                self.manager_set_config(revision, "manager.proactive", json!(next));
            }
            "manager.mode" => {
                let choices = ["pipeline", "live"];
                let now = self.manager_setting_value(id).unwrap_or_default();
                let at = choices.iter().position(|c| *c == now).unwrap_or(0) as isize;
                let next = choices[(at + step.signum()).rem_euclid(2) as usize];
                self.manager_set_config(revision, "manager.mode", json!(next));
            }
            _ => {}
        }
    }

    pub(crate) fn manager_edit_model(&mut self) {
        let Some(config) = &self.state.config else {
            self.toast(Level::Warning, "Configuration is not loaded");
            return;
        };
        let Some(revision) = config["revision"].as_str().map(str::to_owned) else {
            return;
        };
        let current = self
            .manager_setting_value("manager.model")
            .unwrap_or_default();
        let mut buf = Composer::new();
        buf.set_text(&current);
        let restore = self.restore_point();
        self.ui.edit = Some(crate::ui::TextEdit {
            label: "Manager model (provider/model)".into(),
            target: crate::ui::EditTarget::ManagerModel { revision },
            buf,
            restore,
        });
    }
}

/// `state[:level]` or `none`: how `--dump-frame` holds the orb for a screenshot.
/// States are `listening`, `thinking`, `speaking` and `idle`; the level is 0 to 1.
pub fn parse_pin(spec: &str) -> Option<Option<(OrbState, f32)>> {
    let spec = spec.trim();
    if spec == "none" {
        return Some(None);
    }
    let (name, level) = spec.split_once(':').unwrap_or((spec, "0.6"));
    let state = match name {
        "listening" => OrbState::Listening,
        "thinking" => OrbState::Thinking,
        "speaking" => OrbState::Speaking,
        "idle" => OrbState::Idle,
        _ => return None,
    };
    Some(Some((state, level.parse::<f32>().ok()?.clamp(0.0, 1.0))))
}

impl App {
    /// Show the privacy notice; nothing is sent and no microphone opens until it is accepted.
    fn ask_consent(&mut self, body: &Value, talk: bool, say: Option<String>) {
        let c = &body["consent"];
        self.ui.manager.consent = Some(Consent {
            notice: c["notice"].as_str().unwrap_or("").to_owned(),
            provider: c["provider"].as_str().unwrap_or("").to_owned(),
            version: c["version"].clone(),
            talk,
            say,
        });
    }

    fn answer_consent(&mut self, accept: bool) {
        if !accept {
            // Esc cancels the start and nothing is sent.
            self.ui.manager.consent = None;
            return;
        }
        let Some((version, provider)) = self
            .ui
            .manager
            .consent
            .as_ref()
            .map(|c| (c.version.clone(), c.provider.clone()))
        else {
            return;
        };
        let mut body = json!({ "accept": true, "version": version });
        if provider == "codex" {
            body["provider"] = json!("codex"); // the GPT-Live notice, not the Manager model's
        }
        self.effects
            .push(Effect::Sync(SyncCommand::Manager(ManagerCall {
                tag: "consent".into(),
                method: "POST",
                path: "/api/manager/consent".into(),
                body: Some(body),
                with_stream: false,
            })));
    }
}
