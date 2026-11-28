//! The recorder UI (plan 54, UI-TUI), driven by the shared
//! `interface/scenarios/recorder-flow.json` contract and a fake Core/driver.

mod common;
use common::*;

use nexus_tui::api::Frame;
use nexus_tui::app::{App, Effect};
use nexus_tui::audio_capture::{acquire_mic, MicOwner};
use nexus_tui::recorder_driver::{RecoveryIssue, RecoveryIssueKind, RecoveryReport};
use nexus_tui::recorder_view::{CapturePhase, SpoolKind, Width};
use nexus_tui::sync::{Command, RecorderUpdate, SyncMsg};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{Level, RecorderDialog, RecorderOwner, Target};
use serde_json::{json, Value};

const ID: &str = "0123456789abcdef0123456789abcdef";

/// Serialize the tests that can touch the process-wide microphone owner.
fn mic_lock() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    LOCK.lock().unwrap_or_else(|error| error.into_inner())
}

fn app() -> App {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 40));
    a.state.config = Some(json!({"values": {"manager": {"enabled": false}}}));
    a.effects.clear();
    a
}

fn owner(phase: CapturePhase, captured: u64, mic_open: bool) -> RecorderOwner {
    RecorderOwner {
        phase,
        recording_id: Some(ID.into()),
        captured_samples: captured,
        pending_samples: 0,
        spool: SpoolKind::Persistent,
        recoverable: false,
        reason: None,
        level: 0.0,
        capture_open: mic_open,
    }
}

/// A fake Core and capture driver: it turns the app's recorder effects into
/// the request list the shared flow asserts, then answers like Core would.
#[derive(Default)]
struct Fake {
    requests: Vec<(String, String)>,
    mic_open: bool,
    owner: Option<RecorderOwner>,
    segments: Vec<Value>,
    recent: Vec<Value>,
    detail: Value,
}

impl Fake {
    fn new() -> Self {
        Self {
            detail: json!({
                "id": ID, "generation": 1, "event_revision": 1,
                "capture_state": "recording", "active": true, "durable_offset": 0,
                "final_sample_count": null, "complete": false, "holes": 0,
                "transcription_state": "live",
                "transcription_progress": {"lag_samples": 0, "pending_finals": 0, "failed": 0},
            }),
            ..Self::default()
        }
    }

    fn status(&self, a: &mut App) {
        if let Some(owner) = self.owner.clone() {
            a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Status {
                owner,
                recoverable: Vec::new(),
                warnings: Vec::new(),
            }));
        }
    }

    fn page(&self) -> Value {
        json!({
            "segments": self.segments,
            "live": {"provisional": Value::Null},
            "state": "live",
            "complete": false,
        })
    }

    fn run(&mut self, a: &mut App) {
        loop {
            let mut actions = Vec::new();
            let mut kept = Vec::new();
            for effect in std::mem::take(&mut a.effects) {
                match effect {
                    Effect::Sync(Command::Recorder(action)) => actions.push(action),
                    other => kept.push(other),
                }
            }
            a.effects = kept;
            if actions.is_empty() {
                break;
            }
            for action in actions {
                match action {
                    nexus_tui::sync::RecorderAction::Start { .. } => {
                        self.requests
                            .push(("POST".into(), "/api/recordings".into()));
                        self.mic_open = true;
                        self.owner = Some(owner(CapturePhase::Recording, 0, true));
                        self.status(a);
                    }
                    nexus_tui::sync::RecorderAction::Stop => {
                        self.requests
                            .push(("POST".into(), format!("/api/recordings/{ID}/stop")));
                        self.mic_open = false;
                        let captured = self
                            .owner
                            .as_ref()
                            .map_or(0, |owner| owner.captured_samples);
                        self.owner = Some(owner(CapturePhase::Stopping, captured, false));
                        self.status(a);
                    }
                    nexus_tui::sync::RecorderAction::List => {
                        self.requests
                            .push(("GET".into(), "/api/recordings?limit=50".into()));
                        a.apply_sync(SyncMsg::Recorder(RecorderUpdate::List(Ok(self
                            .recent
                            .clone()))));
                    }
                    nexus_tui::sync::RecorderAction::Detail { id } => {
                        self.requests
                            .push(("GET".into(), format!("/api/recordings/{id}")));
                        a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Detail(Ok(self
                            .detail
                            .clone()))));
                    }
                    nexus_tui::sync::RecorderAction::Transcript { id } => {
                        self.requests.push((
                            "GET".into(),
                            format!("/api/recordings/{id}/transcript?after=-1&limit=200"),
                        ));
                        a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Transcript {
                            id,
                            page: Ok(self.page()),
                        }));
                    }
                    nexus_tui::sync::RecorderAction::Delete { id } => {
                        self.requests
                            .push(("DELETE".into(), format!("/api/recordings/{id}")));
                        self.recent
                            .retain(|row| row["id"] != Value::String(id.clone()));
                        a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Deleted {
                            id,
                            result: Ok(()),
                        }));
                    }
                    nexus_tui::sync::RecorderAction::Retry { id } => {
                        self.requests.push((
                            "POST".into(),
                            format!("/api/recordings/{id}/transcript/retry"),
                        ));
                    }
                    nexus_tui::sync::RecorderAction::CopyTranscript { .. }
                    | nexus_tui::sync::RecorderAction::Recover
                    | nexus_tui::sync::RecorderAction::Connected => {}
                }
            }
        }
    }
}

fn event(a: &mut App, id: i64, kind: &str, data: Value) -> nexus_tui::api::Event {
    let generation = a.state.model.generation.as_deref().unwrap_or("log");
    let value = json!({"id": id, "type": kind, "session_id": null,
        "cursor": format!("{generation}:{id}"), "time": 1793113317.0, "data": data});
    let Frame::Event(event) = Frame::from_value(kind, None, &value).unwrap() else {
        panic!("event")
    };
    event
}

fn step(fake: &mut Fake, a: &mut App, action: &str) {
    a.execute(action);
    fake.run(a);
}

fn send_event(fake: &mut Fake, a: &mut App, id: i64, kind: &str, data: Value) {
    let segment = kind == "recorder.segment";
    if segment {
        let segment = data["segment_id"].as_u64().unwrap();
        fake.segments
            .retain(|row| row["segment_id"].as_u64() != Some(segment));
        fake.segments.push(data.clone());
        if let Some(owner) = fake.owner.as_mut() {
            owner.captured_samples = data["end"].as_u64().unwrap_or(owner.captured_samples);
        }
    }
    let ev = event(a, id, kind, data);
    a.apply_sync(SyncMsg::Event(ev));
    if segment {
        fake.status(a);
    }
}

fn header(a: &App) -> String {
    a.recorder_view(Width::Wide).header
}

fn dialog_focus(dialog: &RecorderDialog) -> &'static str {
    match dialog {
        RecorderDialog::Quit { button } => {
            if *button == 0 {
                "stop_and_exit"
            } else {
                "keep_recording"
            }
        }
        RecorderDialog::Delete { button, .. } => {
            if *button == 0 {
                "delete"
            } else {
                "cancel"
            }
        }
    }
}

fn assert_requests(fake: &Fake, want: &[(&str, &str)]) {
    let want: Vec<(String, String)> = want
        .iter()
        .map(|(method, path)| (method.to_string(), path.to_string()))
        .collect();
    assert_eq!(fake.requests, want, "requests");
}

fn clear(fake: &mut Fake) {
    fake.requests.clear();
}

#[test]
fn the_recording_flow_matches_the_shared_scenario() {
    let _mic = mic_lock();
    let mut a = app();
    let mut fake = Fake::new();

    // 0. open from the header
    step(&mut fake, &mut a, "recorder.open");
    assert_requests(&fake, &[("GET", "/api/recordings?limit=50")]);
    assert!(a.recorder_modal());
    assert!(!fake.mic_open);
    assert_eq!(a.ui.recorder.focus, "recorder.start");
    assert_eq!(header(&a), "Record");
    clear(&mut fake);

    // 1. start recording
    step(&mut fake, &mut a, "recorder.start");
    assert_requests(&fake, &[("POST", "/api/recordings")]);
    assert!(fake.mic_open);
    assert_eq!(header(&a), "● REC 00:00");
    assert_eq!(a.ui.recorder.focus, "recorder.minimize");
    assert_eq!(
        a.recorder_view(Width::Wide)
            .buttons
            .iter()
            .map(|b| b.id.as_str())
            .collect::<Vec<_>>(),
        ["recorder.minimize", "recorder.stop"]
    );
    clear(&mut fake);

    // 2. live provisional segment
    send_event(
        &mut fake,
        &mut a,
        20,
        "recorder.segment",
        json!({"id": ID, "generation": 1, "segment_id": 0, "revision": 1,
               "start": 0, "end": 3360, "status": "provisional", "text": "Hello there"}),
    );
    let view = a.recorder_view(Width::Wide);
    assert_eq!(view.rows.last().unwrap().label, "00:00–  Hello there…");
    assert!(view.follow);
    assert!(!view.jump_live);
    assert_requests(&fake, &[]);

    // 3. quit while recording asks first
    a.execute("app.quit");
    let dialog = a.ui.recorder.dialog.clone().expect("quit dialog");
    assert_eq!(dialog_focus(&dialog), "keep_recording");
    assert!(a.recorder_modal());
    assert!(fake.mic_open);
    assert_requests(&fake, &[]);
    a.recorder_dialog_cancel();
    assert_eq!(a.ui.recorder.focus, "recorder.minimize");

    // 5. a final segment replaces the provisional tail
    send_event(
        &mut fake,
        &mut a,
        21,
        "recorder.segment",
        json!({"id": ID, "generation": 1, "segment_id": 0, "revision": 2,
               "start": 0, "end": 112000, "status": "final", "text": "Hello there."}),
    );
    assert_eq!(header(&a), "● REC 00:07");
    assert_eq!(
        a.recorder_view(Width::Wide).rows[0].label,
        "00:00–00:07  Hello there."
    );
    clear(&mut fake);

    // 6. esc minimises while recording
    press(&mut a, &["esc"]);
    assert!(!a.recorder_modal());
    assert!(fake.mic_open);
    clear(&mut fake);

    // 7. switching sessions keeps capture
    a.open_session(SESSION);
    assert!(a.recorder_owner_active());
    assert!(fake.mic_open);
    assert_requests(&fake, &[]);

    // 8. typing in the composer keeps capture
    a.ui.focus = nexus_tui::ui::Focus::Composer;
    typing(&mut a, "a note for later");
    assert!(a.recorder_owner_active());
    assert!(fake.mic_open);
    clear(&mut fake);

    // 9. reopen reads the snapshot and transcript again
    step(&mut fake, &mut a, "recorder.open");
    assert_requests(
        &fake,
        &[
            ("GET", "/api/recordings?limit=50"),
            ("GET", "/api/recordings/0123456789abcdef0123456789abcdef"),
            (
                "GET",
                "/api/recordings/0123456789abcdef0123456789abcdef/transcript?after=-1&limit=200",
            ),
        ],
    );
    assert_eq!(header(&a), "● REC 00:07");
    assert_eq!(a.ui.recorder.focus, "recorder.minimize");
    assert_eq!(
        a.recorder_view(Width::Wide).rows[0].label,
        "00:00–00:07  Hello there."
    );
    clear(&mut fake);

    // 10. scroll up shows Jump to live
    press(&mut a, &["up"]);
    let view = a.recorder_view(Width::Wide);
    assert!(!view.follow);
    assert!(view.jump_live);
    assert_requests(&fake, &[]);

    // 11. a new final segment does not yank the scroll
    send_event(
        &mut fake,
        &mut a,
        22,
        "recorder.segment",
        json!({"id": ID, "generation": 1, "segment_id": 1, "revision": 1,
               "start": 112000, "end": 240000, "status": "final",
               "text": "This is still recording."}),
    );
    let view = a.recorder_view(Width::Wide);
    assert_eq!(view.rows.len(), 2);
    assert!(!view.follow);
    assert!(view.jump_live);
    assert_eq!(header(&a), "● REC 00:15");

    // 12. stop releases the microphone and drains
    step(&mut fake, &mut a, "recorder.stop");
    assert_requests(
        &fake,
        &[(
            "POST",
            "/api/recordings/0123456789abcdef0123456789abcdef/stop",
        )],
    );
    assert!(!fake.mic_open);
    assert_eq!(header(&a), "Saving audio…");
    assert_eq!(a.ui.recorder.focus, "recorder.minimize");
    clear(&mut fake);

    // 13. Core reports stopped and the transcript catching up
    send_event(
        &mut fake,
        &mut a,
        23,
        "recorder.state",
        json!({"id": ID, "generation": 1, "revision": 5,
               "capture_state": "stopped", "transcription_state": "catching_up",
               "transcription_progress": {"lag_samples": 112000, "pending_finals": 1,
                                          "failed": 0, "transcript_revision": 2},
               "transcript_revision": 2, "active": false, "durable_offset": 240000,
               "final_sample_count": 240000, "complete": true, "holes": 0,
               "lag_seconds": 7, "reason": "stopped"}),
    );
    let view = a.recorder_view(Width::Wide);
    assert_eq!(view.header, "Finishing transcript");
    assert_eq!(view.capture.as_deref(), Some("Stopped · 00:15"));
    assert_eq!(
        view.audio.as_deref(),
        Some("Audio saved · Finishing transcript")
    );
    assert_eq!(
        view.transcript_status.as_deref(),
        Some("Catching up · 7s behind")
    );
    assert_eq!(
        view.buttons
            .iter()
            .map(|b| b.id.as_str())
            .collect::<Vec<_>>(),
        [
            "recorder.copy_transcript",
            "recorder.recent",
            "recorder.start"
        ]
    );
    assert_eq!(a.ui.recorder.focus, "recorder.copy_transcript");
    assert_requests(&fake, &[]);

    // 14. Core reports the transcript complete
    send_event(
        &mut fake,
        &mut a,
        24,
        "recorder.state",
        json!({"id": ID, "generation": 1, "revision": 6,
               "capture_state": "stopped", "transcription_state": "complete",
               "transcription_progress": {"lag_samples": 0, "pending_finals": 0,
                                          "failed": 0, "transcript_revision": 4},
               "transcript_revision": 4, "active": false, "durable_offset": 240000,
               "final_sample_count": 240000, "complete": true, "holes": 0,
               "lag_seconds": 0, "reason": "transcription"}),
    );
    let view = a.recorder_view(Width::Wide);
    assert_eq!(view.header, "Record");
    assert_eq!(view.audio.as_deref(), Some("Audio saved"));
    assert_eq!(view.transcript_status.as_deref(), Some("Complete"));

    // 15. open Recent recordings
    fake.recent = vec![json!({
        "id": ID, "capture_state": "stopped", "created_at": "2026-11-26T14:02:00",
        "final_sample_count": 240000, "durable_offset": 240000,
        "transcription_state": "complete", "holes": 0,
    })];
    step(&mut fake, &mut a, "recorder.recent");
    assert_requests(&fake, &[("GET", "/api/recordings?limit=50")]);
    assert_eq!(a.ui.recorder.page, nexus_tui::recorder_view::Page::Recent);
    assert_eq!(a.ui.recorder.focus, "recent.0");
    let view = a.recorder_view(Width::Wide);
    assert_eq!(
        view.recent.as_ref().unwrap().rows,
        ["26 Nov 14:02 · 00:15 · Complete"]
    );
    assert_eq!(
        view.buttons
            .iter()
            .map(|b| b.id.as_str())
            .collect::<Vec<_>>(),
        ["recorder.start", "recorder.delete"]
    );
    clear(&mut fake);

    // 16. delete asks first
    step(&mut fake, &mut a, "recorder.delete");
    assert_requests(&fake, &[]);
    let dialog = a.ui.recorder.dialog.clone().expect("delete dialog");
    assert_eq!(dialog_focus(&dialog), "cancel");

    // 17. cancel keeps the recording
    press(&mut a, &["esc"]);
    assert!(a.ui.recorder.dialog.is_none());
    assert_eq!(a.ui.recorder.focus, "recent.0");
    assert_requests(&fake, &[]);

    // 18. esc returns to the recorder page
    press(&mut a, &["esc"]);
    assert_eq!(a.ui.recorder.page, nexus_tui::recorder_view::Page::Recorder);
    assert_eq!(a.ui.recorder.focus, "recorder.copy_transcript");
    assert_requests(&fake, &[]);

    // 19. esc closes when nothing is capturing
    press(&mut a, &["esc"]);
    assert!(!a.recorder_modal());
    assert_requests(&fake, &[]);
}

#[test]
fn opening_never_creates_a_recording_or_opens_the_microphone() {
    let mut a = app();
    let mut fake = Fake::new();
    step(&mut fake, &mut a, "recorder.open");
    assert!(a.recorder_modal());
    assert!(!fake.mic_open);
    assert_eq!(
        fake.requests,
        vec![("GET".into(), "/api/recordings?limit=50".into())]
    );
    assert!(a
        .effects
        .iter()
        .all(|e| !matches!(e, Effect::Sync(Command::Recorder(_)))));
}

#[test]
fn header_order_is_recorder_then_bell_then_manager() {
    let mut a = app();
    a.state.config = Some(json!({"values": {"manager": {"enabled": true}}}));
    a.ui.alerts
        .push(Level::Info, "notice", std::time::Instant::now());
    let _ = draw(&mut a, 120, 40);
    let recorder = a.hits.rect_of(&Target::Recorder).expect("recorder entry");
    let bell = a.hits.rect_of(&Target::Notifications).expect("bell");
    let manager = a.hits.rect_of(&Target::ManagerSession).expect("manager");
    assert!(recorder.right() <= bell.x, "{recorder:?} {bell:?}");
    assert!(bell.right() <= manager.x, "{bell:?} {manager:?}");
}

#[test]
fn the_entry_shows_with_zero_notifications_and_manager_disabled() {
    let mut a = app();
    let (_, rows) = draw(&mut a, 120, 40);
    assert!(rows[0].contains("Record"));
    assert!(a.hits.rect_of(&Target::Recorder).is_some());
    assert!(a.hits.rect_of(&Target::Notifications).is_none());
    assert!(a.hits.rect_of(&Target::ManagerSession).is_none());
}

#[test]
fn an_active_recording_is_visible_at_fifty_columns() {
    let mut a = app();
    a.ui.recorder.owner = Some(owner(CapturePhase::Recording, 112000, true));
    let (_, rows) = draw(&mut a, 50, 24);
    assert!(rows[0].contains("00:07"), "{}", rows[0]);
    assert!(a.hits.rect_of(&Target::Recorder).is_some());

    let mut idle = app();
    let _ = draw(&mut idle, 50, 24);
    assert!(idle.hits.rect_of(&Target::Recorder).is_none());
}

#[test]
fn esc_minimises_while_recording_and_closes_when_stopped() {
    let _mic = mic_lock();
    let mut a = app();
    let mut fake = Fake::new();
    step(&mut fake, &mut a, "recorder.open");
    step(&mut fake, &mut a, "recorder.start");
    press(&mut a, &["esc"]);
    assert!(!a.recorder_modal());
    assert!(a.recorder_owner_active());

    fake.owner = Some(owner(CapturePhase::Stopped, 112000, false));
    fake.status(&mut a);
    step(&mut fake, &mut a, "recorder.open");
    press(&mut a, &["esc"]);
    assert!(!a.recorder_modal());
}

#[test]
fn reopening_keeps_the_transcript_scroll() {
    let _mic = mic_lock();
    let mut a = app();
    let mut fake = Fake::new();
    step(&mut fake, &mut a, "recorder.open");
    step(&mut fake, &mut a, "recorder.start");
    send_event(
        &mut fake,
        &mut a,
        30,
        "recorder.segment",
        json!({"id": ID, "generation": 1, "segment_id": 0, "revision": 1,
               "start": 0, "end": 112000, "status": "final", "text": "Hello."}),
    );
    press(&mut a, &["up"]);
    assert!(!a.recorder_view(Width::Wide).follow);
    press(&mut a, &["esc"]);
    step(&mut fake, &mut a, "recorder.open");
    assert!(!a.recorder_view(Width::Wide).follow);
    assert!(a.recorder_view(Width::Wide).jump_live);
}

#[test]
fn quit_offers_stop_and_exit_or_keep_recording() {
    let _mic = mic_lock();
    let mut a = app();
    let mut fake = Fake::new();
    step(&mut fake, &mut a, "recorder.open");
    step(&mut fake, &mut a, "recorder.start");
    clear(&mut fake);

    a.execute("app.quit");
    let dialog = a.ui.recorder.dialog.clone().expect("quit dialog");
    assert_eq!(dialog_focus(&dialog), "keep_recording");
    a.recorder_dialog_activate();
    assert!(a.ui.recorder.dialog.is_none());
    assert!(fake.mic_open);
    assert!(a.effects.iter().all(|e| !matches!(e, Effect::Quit)));

    a.execute("app.quit");
    a.recorder_dialog_move(false); // Stop and exit
    a.recorder_dialog_activate();
    assert!(a.ui.recorder.pending_quit.is_some());
    assert!(a.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::Recorder(nexus_tui::sync::RecorderAction::Stop))
    )));
}

#[test]
fn quitting_while_minimised_shows_the_confirmation() {
    let _mic = mic_lock();
    let mut a = app();
    let mut fake = Fake::new();
    step(&mut fake, &mut a, "recorder.open");
    step(&mut fake, &mut a, "recorder.start");
    press(&mut a, &["esc"]);
    assert!(!a.recorder_modal());
    clear(&mut fake);
    a.execute("app.quit");
    assert!(a.recorder_modal());
    assert!(a.ui.recorder.dialog.is_some());
    assert!(fake.requests.is_empty());
    a.recorder_dialog_cancel();
    assert!(!a.effects.iter().any(|e| matches!(e, Effect::Quit)));
}

#[test]
fn stop_is_never_the_initial_focus() {
    let mut a = app();
    a.ui.recorder.owner = Some(owner(CapturePhase::Recording, 0, true));
    a.execute("recorder.open");
    assert_eq!(a.ui.recorder.focus, "recorder.minimize");
    assert_ne!(a.ui.recorder.focus, "recorder.stop");
}

#[test]
fn delete_requires_confirmation() {
    let mut a = app();
    let mut fake = Fake::new();
    fake.recent = vec![json!({
        "id": ID, "capture_state": "stopped", "created_at": "2026-11-26T14:02:00",
        "final_sample_count": 240000, "transcription_state": "complete", "holes": 0,
    })];
    fake.detail = json!({
        "id": ID, "generation": 1, "event_revision": 1, "capture_state": "stopped",
        "active": false, "durable_offset": 240000, "final_sample_count": 240000,
        "complete": true, "holes": 0, "transcription_state": "complete",
        "transcription_progress": {"lag_samples": 0, "pending_finals": 0, "failed": 0},
    });
    step(&mut fake, &mut a, "recorder.open");
    step(&mut fake, &mut a, "recorder.recent");
    clear(&mut fake);
    a.execute("recorder.delete");
    assert!(a.ui.recorder.dialog.is_some());
    assert!(fake.requests.is_empty());
    a.recorder_dialog_move(false);
    a.recorder_dialog_activate();
    fake.run(&mut a);
    assert_requests(
        &fake,
        &[
            ("DELETE", "/api/recordings/0123456789abcdef0123456789abcdef"),
            ("GET", "/api/recordings?limit=50"),
        ],
    );
}

#[test]
fn a_spool_left_after_restart_offers_recovery() {
    let mut a = app();
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Recoverable(vec![
        nexus_tui::sync::RecoverableAudio {
            id: ID.into(),
            accepted_samples: 128000,
            pending_samples: 32000,
        },
    ])));
    let view = a.recorder_view(Width::Wide);
    assert_eq!(view.header, "! Recording interrupted");
    assert_eq!(
        view.buttons
            .iter()
            .map(|b| b.id.as_str())
            .collect::<Vec<_>>(),
        ["recorder.recover"]
    );
    a.execute("recorder.recover");
    assert!(a.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::Recorder(nexus_tui::sync::RecorderAction::Recover))
    )));
}

#[test]
fn a_busy_start_shows_the_shared_reason_and_never_opens_the_mic() {
    let mut a = app();
    let mut busy = owner(CapturePhase::Failed, 0, false);
    busy.recording_id = None;
    busy.reason = Some("recorder_busy: another client is recording".into());
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Status {
        owner: busy,
        recoverable: Vec::new(),
        warnings: Vec::new(),
    }));
    let view = a.recorder_view(Width::Wide);
    assert_eq!(view.header, "Record");
    assert_eq!(
        view.capture.as_deref(),
        Some("Failed: Another client is recording. Only one recording can run at a time.")
    );
    assert_eq!(
        view.buttons
            .iter()
            .map(|b| b.id.as_str())
            .collect::<Vec<_>>(),
        ["recorder.start", "recorder.recent"]
    );
}

#[test]
fn reconnect_rereads_the_snapshot_and_transcript() {
    let _mic = mic_lock();
    let mut a = app();
    let mut fake = Fake::new();
    step(&mut fake, &mut a, "recorder.open");
    step(&mut fake, &mut a, "recorder.start");
    clear(&mut fake);
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Reconnected));
    fake.run(&mut a);
    assert_requests(
        &fake,
        &[
            ("GET", "/api/recordings?limit=50"),
            ("GET", "/api/recordings/0123456789abcdef0123456789abcdef"),
            (
                "GET",
                "/api/recordings/0123456789abcdef0123456789abcdef/transcript?after=-1&limit=200",
            ),
        ],
    );
}

#[test]
fn the_level_meter_uses_the_driver_level() {
    let mut a = app();
    let mut recording = owner(CapturePhase::Recording, 112000, true);
    recording.level = 0.75;
    a.ui.recorder.owner = Some(recording);
    a.execute("recorder.open");
    let view = a.recorder_view(Width::Wide);
    assert_eq!(view.level, Some(0.75));
    let (_, rows) = draw(&mut a, 120, 40);
    assert!(
        rows.iter().any(|row| row.contains('\u{2586}')),
        "no lit level cell in:\n{}",
        rows.join("\n")
    );
}

#[test]
fn a_busy_microphone_refuses_start_with_the_shared_text() {
    let _mic = mic_lock();
    let guard = acquire_mic(MicOwner::Dictation).expect("microphone free");
    let mut a = app();
    a.execute("recorder.open");
    a.effects.clear();
    a.execute("recorder.start");
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::Recorder(
            nexus_tui::sync::RecorderAction::Start { .. }
        ))
    )));
    let view = a.recorder_view(Width::Wide);
    assert_eq!(
        view.capture.as_deref(),
        Some("Failed: The microphone is in use by dictation.")
    );
    assert_eq!(
        view.buttons
            .iter()
            .map(|b| b.id.as_str())
            .collect::<Vec<_>>(),
        ["recorder.start", "recorder.recent"]
    );
    a.ui.manager.mic = nexus_tui::manager::Mic::Listening;
    a.execute("recorder.start");
    let view = a.recorder_view(Width::Wide);
    assert_eq!(
        view.capture.as_deref(),
        Some("Failed: The microphone is in use by the Manager call.")
    );
    drop(guard);
}

fn interrupted_detail() -> Value {
    json!({
        "id": ID, "generation": 1, "event_revision": 3,
        "capture_state": "interrupted", "active": false,
        "durable_offset": 192000, "final_sample_count": null,
        "complete": false, "holes": 0, "transcription_state": "live",
        "transcription_progress": {"lag_samples": 0, "pending_finals": 0, "failed": 0},
        "reason": "stream_detached",
    })
}

#[test]
fn startup_loads_interrupted_state_without_opening_the_modal() {
    let mut a = app();
    let mut fake = Fake::new();
    fake.recent = vec![json!({
        "id": ID, "capture_state": "interrupted", "created_at": "2026-11-26T14:02:00",
        "durable_offset": 192000, "final_sample_count": null,
        "transcription_state": "live", "holes": 0,
    })];
    fake.detail = interrupted_detail();
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Reconnected));
    fake.run(&mut a);
    assert!(!a.recorder_modal());
    assert_eq!(
        a.recorder_header_view().header_wide,
        "! Recording interrupted"
    );
    assert_eq!(
        a.ui.recorder.core.as_ref().map(|core| core.capture_state),
        Some(CapturePhase::Interrupted)
    );
    assert_requests(
        &fake,
        &[
            ("GET", "/api/recordings?limit=50"),
            ("GET", "/api/recordings/0123456789abcdef0123456789abcdef"),
            (
                "GET",
                "/api/recordings/0123456789abcdef0123456789abcdef/transcript?after=-1&limit=200",
            ),
        ],
    );
}

#[test]
fn opening_the_modal_keeps_the_interrupted_header_until_closed() {
    let mut a = app();
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Detail(Ok(
        interrupted_detail(),
    ))));
    assert_eq!(
        a.recorder_header_view().header_wide,
        "! Recording interrupted"
    );
    a.execute("recorder.open");
    assert!(a.recorder_modal());
    assert_eq!(
        a.recorder_header_view().header_wide,
        "! Recording interrupted"
    );
    press(&mut a, &["esc"]);
    assert!(!a.recorder_modal());
    assert_eq!(a.recorder_header_view().header_wide, "Record");
}

#[test]
fn unresolved_startup_intent_refuses_start() {
    let _mic = mic_lock();
    let mut a = app();
    let detail =
        "Core's recorder create guard is full; restart Core before creating another recording";
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Startup {
        result: Ok(RecoveryReport {
            attempted: 1,
            recovered: 0,
            truncated: false,
            issues: vec![RecoveryIssue {
                recording_id: None,
                kind: RecoveryIssueKind::CoreRestartRequired,
                detail: detail.into(),
            }],
        }),
        pending: 1,
        ready: false,
    }));
    assert_eq!(a.ui.recorder.pending_starts, 1);
    assert_eq!(a.ui.recorder.startup_block.as_deref(), Some(detail));
    a.execute("recorder.open");
    a.effects.clear();
    a.execute("recorder.start");
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::Recorder(
            nexus_tui::sync::RecorderAction::Start { .. }
        ))
    )));
    let view = a.recorder_view(Width::Wide);
    assert_eq!(
        view.capture.as_deref(),
        Some(format!("Failed: {detail}").as_str())
    );
}

#[test]
fn recovered_empty_recording_is_kept_and_start_is_unblocked() {
    let _mic = mic_lock();
    let mut a = app();
    let mut fake = Fake::new();
    fake.recent = vec![json!({
        "id": ID, "capture_state": "stopped", "created_at": "2026-11-26T14:02:00",
        "durable_offset": 0, "final_sample_count": 0,
        "transcription_state": "complete", "holes": 0,
    })];
    fake.detail = json!({
        "id": ID, "generation": 1, "event_revision": 1, "capture_state": "stopped",
        "active": false, "durable_offset": 0, "final_sample_count": 0,
        "complete": true, "holes": 0, "transcription_state": "complete",
        "transcription_progress": {"lag_samples": 0, "pending_finals": 0, "failed": 0},
    });
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Startup {
        result: Ok(RecoveryReport {
            attempted: 1,
            recovered: 1,
            truncated: false,
            issues: Vec::new(),
        }),
        pending: 0,
        ready: true,
    }));
    fake.run(&mut a);
    assert_eq!(a.ui.recorder.pending_starts, 0);
    assert!(a.ui.recorder.startup_block.is_none());
    assert_eq!(a.ui.recorder.recent.len(), 1);
    assert_eq!(a.ui.recorder.recent[0].final_sample_count, Some(0));
    a.effects.clear();
    a.execute("recorder.start");
    assert!(a.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::Recorder(
            nexus_tui::sync::RecorderAction::Start { .. }
        ))
    )));
}

#[test]
fn persistent_storage_denial_uses_the_shared_neutral_text() {
    let _mic = mic_lock();
    let mut a = app();
    a.apply_sync(SyncMsg::Recorder(RecorderUpdate::Failed {
        kind: "spool".into(),
        message: "could not create the recorder spool: permission denied".into(),
    }));
    assert_eq!(
        a.ui.recorder.startup_block.as_deref(),
        Some("Persistent storage is required for recording")
    );
    a.execute("recorder.open");
    a.effects.clear();
    a.execute("recorder.start");
    assert!(!a.effects.iter().any(|e| matches!(
        e,
        Effect::Sync(Command::Recorder(
            nexus_tui::sync::RecorderAction::Start { .. }
        ))
    )));
    let view = a.recorder_view(Width::Wide);
    assert_eq!(
        view.capture.as_deref(),
        Some("Failed: Persistent storage is required for recording")
    );
}
