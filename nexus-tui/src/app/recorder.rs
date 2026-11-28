//! The recorder's client-side behaviour (plan 54, UI-TUI).
//!
//! The pure view-model in `recorder_view.rs` decides what the recorder shows.
//! This module keeps the client state it combines (this client's capture, Core's
//! authoritative recording, the durable transcript and view flags), turns
//! commands and keys into driver actions, and folds driver/event replies back.
//! It performs no transport or device I/O: `sync.rs` owns the recorder task.

use std::time::{Duration, Instant};

use serde_json::Value;

use super::App;
use super::Effect;
use crate::recorder_view::{
    CapturePhase, CoreInput, CoreOwner, FailureInput, Page, RecentRecording, SegmentInput,
    SegmentStatus, SpoolKind, Tone, TranscriptionProgress, TranscriptionState, Width,
};
use crate::sync::{Command, RecorderAction, RecorderUpdate};
use crate::ui::{Level, OverlayKind, RecorderDialog, RecorderOwner};

/// How long Stop and exit waits for the audio drain before quitting anyway.
const QUIT_DRAIN: Duration = Duration::from_secs(5);
fn phase(value: &str) -> CapturePhase {
    match value {
        "starting" => CapturePhase::Starting,
        "recording" => CapturePhase::Recording,
        "stopping" => CapturePhase::Stopping,
        "stopped" => CapturePhase::Stopped,
        "interrupted" => CapturePhase::Interrupted,
        "failed" => CapturePhase::Failed,
        _ => CapturePhase::Idle,
    }
}

fn transcription(value: &str) -> TranscriptionState {
    match value {
        "live" => TranscriptionState::Live,
        "catching_up" => TranscriptionState::CatchingUp,
        "failed" => TranscriptionState::Failed,
        "complete" => TranscriptionState::Complete,
        "unavailable" => TranscriptionState::Unavailable,
        _ => TranscriptionState::Preparing,
    }
}

fn segment_status(value: &str) -> SegmentStatus {
    match value {
        "provisional" => SegmentStatus::Provisional,
        "final" => SegmentStatus::Final,
        "pending" => SegmentStatus::Pending,
        "error" => SegmentStatus::Error,
        _ => SegmentStatus::Skipped,
    }
}

/// Core's public recording state, as the view-model consumes it.
pub(crate) fn core_from_value(id: &str, value: &Value) -> Option<CoreInput> {
    let value = value.get("recording").unwrap_or(value);
    if !value.is_object() {
        return None;
    }
    let id = value["id"].as_str().unwrap_or(id).to_owned();
    let progress = &value["transcription_progress"];
    let revision = value["event_revision"]
        .as_u64()
        .or_else(|| value["revision"].as_u64())
        .unwrap_or(0);
    Some(CoreInput {
        id,
        generation: value["generation"].as_u64().unwrap_or(0),
        revision,
        capture_state: phase(value["capture_state"].as_str().unwrap_or("idle")),
        active: value["active"].as_bool().unwrap_or(false),
        durable_offset: value["durable_offset"].as_u64().unwrap_or(0),
        final_sample_count: value["final_sample_count"].as_u64(),
        complete: value["complete"].as_bool().unwrap_or(false),
        holes: value["holes"].as_u64().unwrap_or(0),
        missing_samples: value["missing_samples"].as_u64(),
        owner: value["owner"]["client_id"]
            .as_str()
            .or_else(|| value["client_id"].as_str())
            .map(|client_id| CoreOwner {
                client_id: Some(client_id.to_owned()),
            }),
        transcription_state: transcription(
            value["transcription_state"].as_str().unwrap_or("preparing"),
        ),
        transcription_progress: TranscriptionProgress {
            lag_samples: progress["lag_samples"].as_u64().unwrap_or(0),
            pending_finals: progress["pending_finals"].as_u64().unwrap_or(0),
            failed: progress["failed"].as_u64().unwrap_or(0),
            transcript_revision: progress["transcript_revision"]
                .as_u64()
                .or_else(|| value["transcript_revision"].as_u64()),
            reason: progress["reason"]
                .as_str()
                .or_else(|| value["reason"].as_str())
                .map(str::to_owned),
        },
        reason: value["reason"].as_str().map(str::to_owned),
    })
}

fn segment_from_value(value: &Value) -> Option<SegmentInput> {
    Some(SegmentInput {
        segment_id: u32::try_from(value["segment_id"].as_u64()?).ok()?,
        revision: value["revision"].as_u64().unwrap_or(0),
        start: value["start"].as_u64().unwrap_or(0),
        end: value["end"].as_u64(),
        status: segment_status(value["status"].as_str().unwrap_or("final")),
        text: value["text"].as_str().map(str::to_owned),
    })
}

fn recent_from_value(value: &Value) -> Option<RecentRecording> {
    Some(RecentRecording {
        id: value["id"].as_str()?.to_owned(),
        capture_state: phase(value["capture_state"].as_str().unwrap_or("stopped")),
        created_at: value["created_at"].as_str().unwrap_or("").to_owned(),
        durable_offset: value["durable_offset"].as_u64().unwrap_or(0),
        final_sample_count: value["final_sample_count"].as_u64(),
        transcription_state: transcription(
            value["transcription_state"].as_str().unwrap_or("complete"),
        ),
        holes: value["holes"].as_u64().unwrap_or(0),
        active: value["active"].as_bool().unwrap_or(false),
    })
}

fn transcript_input(
    id: &str,
    generation: Option<u64>,
    page: &Value,
) -> Option<crate::recorder_view::TranscriptInput> {
    let segments = page["segments"]
        .as_array()
        .map(|rows| rows.iter().filter_map(segment_from_value).collect())
        .unwrap_or_default();
    let provisional = segment_from_value(&page["live"]["provisional"]).map(|segment| {
        crate::recorder_view::ProvisionalInput {
            segment_id: segment.segment_id,
            revision: segment.revision,
            start: segment.start,
            text: segment.text.unwrap_or_default(),
        }
    });
    Some(crate::recorder_view::TranscriptInput {
        recording_id: Some(id.to_owned()),
        generation,
        segments,
        provisional,
        dropped: 0,
    })
}

impl App {
    /// The shared microphone-busy wording for a refusal from any feature. The
    /// message comes from the capture thread; the owner is read from the shared
    /// guard when it is still held, else parsed from the message.
    pub fn mic_busy_text(&self, message: &str) -> Option<String> {
        let lower = message.to_lowercase();
        if !(lower.contains("microphone") && lower.contains("in use")) {
            return None;
        }
        let owner = match crate::audio_capture::current_mic_owner() {
            Some(crate::audio_capture::MicOwner::Dictation) if self.ui.manager.listening() => {
                "the Manager call".to_owned()
            }
            Some(owner) => owner.label().to_owned(),
            None => message
                .split(" by ")
                .nth(1)
                .map(|owner| owner.trim_end_matches(['.', ' ']).to_owned())
                .unwrap_or_else(|| "another feature".to_owned()),
        };
        Some(
            self.manifest
                .ui_text("recorder.busy.mic", &[("owner", &owner)]),
        )
    }

    /// The platform-neutral persistent-storage refusal shared with NSH. The
    /// manifest owner is adding the shared string; until it lands, the exact
    /// agreed wording keeps the two clients identical.
    pub fn recorder_storage_text(&self) -> String {
        for key in [
            "recorder.storage.required",
            "recorder.audio.storage_required",
        ] {
            if let Some(text) = self.manifest.strings.get(key) {
                return text.clone();
            }
        }
        "Persistent storage is required for recording".to_owned()
    }

    fn recorder_storage_failure(&self, kind: &str, message: &str) -> Option<String> {
        let lower = message.to_lowercase();
        (kind == "spool"
            || lower.contains("persistent storage")
            || lower.contains("recorder spool")
            || lower.contains("could not persist the recorder create intent")
            || (lower.contains("storage")
                && (lower.contains("required") || lower.contains("persist"))))
        .then(|| self.recorder_storage_text())
    }

    /// The capture controller has an active recording (this client is owner).
    pub fn recorder_owner_active(&self) -> bool {
        self.ui
            .recorder
            .owner
            .as_ref()
            .is_some_and(RecorderOwner::active)
    }

    /// Any client has an active capture.
    pub fn recorder_active(&self) -> bool {
        self.recorder_owner_active()
            || self.ui.recorder.core.as_ref().is_some_and(|core| {
                core.active
                    || matches!(
                        core.capture_state,
                        CapturePhase::Starting | CapturePhase::Recording | CapturePhase::Stopping
                    )
            })
    }

    /// The header keeps the entry (compact) below 60 columns when this matters.
    pub fn recorder_header_active(&self) -> bool {
        self.recorder_active()
            || self.ui.recorder.owner.as_ref().is_some_and(|owner| {
                matches!(
                    owner.phase,
                    CapturePhase::Interrupted | CapturePhase::Failed
                )
            })
            || self.ui.recorder.core.as_ref().is_some_and(|core| {
                matches!(
                    core.capture_state,
                    CapturePhase::Interrupted | CapturePhase::Failed
                )
            })
    }

    pub fn recorder_modal(&self) -> bool {
        matches!(
            self.ui.overlay.as_ref().map(|o| &o.kind),
            Some(OverlayKind::Recorder)
        )
    }

    /// Header labels and tone without building the transcript rows.
    pub fn recorder_header_view(&self) -> crate::recorder_view::RecorderView {
        let input = self.ui.recorder.header_input(Width::Wide);
        crate::recorder_view::recorder_view(&self.manifest, &input)
    }

    pub fn recorder_header_labels(&self) -> (String, String, bool) {
        let view = self.recorder_header_view();
        let idle = self.manifest.ui_text("recorder.header.idle", &[]);
        (
            view.header_wide.clone(),
            view.header_compact,
            view.header_wide != idle,
        )
    }

    pub fn recorder_header_tone(&self) -> Tone {
        self.recorder_header_view().header_tone
    }

    /// The complete view-model for the current recorder state.
    pub fn recorder_view(&self, width: Width) -> crate::recorder_view::RecorderView {
        crate::recorder_view::recorder_view(&self.manifest, &self.ui.recorder.view_input(width))
    }

    fn recorder_focus_ids(&self, view: &crate::recorder_view::RecorderView) -> Vec<String> {
        let mut ids: Vec<String> = view
            .buttons
            .iter()
            .map(|button| button.id.clone())
            .collect();
        if view.jump_live {
            ids.push("jump_live".into());
        }
        if self.ui.recorder.page == Page::Recent {
            if let Some(recent) = view.recent.as_ref() {
                let rows: Vec<String> = (0..recent.rows.len())
                    .map(|i| format!("recent.{i}"))
                    .collect();
                let mut all = rows;
                all.extend(ids);
                return all;
            }
        }
        ids
    }

    fn recorder_sync_focus(&mut self) {
        let view = self.recorder_view(Width::Wide);
        let ids = self.recorder_focus_ids(&view);
        if !ids.contains(&self.ui.recorder.focus) {
            self.ui.recorder.focus = view.focus;
        }
    }

    fn recorder_request(&mut self, action: RecorderAction) {
        self.effects.push(Effect::Sync(Command::Recorder(action)));
    }

    fn recorder_fetch_id(&self) -> Option<String> {
        self.ui.recorder.shown_id()
    }

    /// Ask for the recording list, and the shown recording's state and page.
    pub fn recorder_refresh(&mut self, detail: bool) {
        self.recorder_request(RecorderAction::List);
        if detail {
            if let Some(id) = self.recorder_fetch_id() {
                self.recorder_request(RecorderAction::Detail { id: id.clone() });
                self.recorder_request(RecorderAction::Transcript { id });
            }
        }
    }

    /// Open the recorder modal. It never creates a recording or opens the mic.
    pub fn recorder_open(&mut self) {
        if !self.recorder_modal() {
            self.open_overlay(OverlayKind::Recorder);
        }
        self.ui.recorder.page = Page::Recorder;
        self.ui.recorder.dialog = None;
        self.ui.recorder.failure =
            self.ui
                .recorder
                .startup_block
                .clone()
                .map(|reason| FailureInput {
                    kind: "startup".into(),
                    reason: Some(reason),
                });
        if self.ui.recorder.source.host.is_none() {
            self.ui.recorder.source.host = Some("this computer".into());
        }
        let view = self.recorder_view(Width::Wide);
        self.ui.recorder.focus = view.focus;
        self.recorder_refresh(true);
    }

    /// The interrupted pill clears only after the modal has been closed, not as
    /// soon as it opens: the header keeps the real state while it is inspected.
    fn recorder_mark_seen(&mut self) {
        if let Some(owner) = self.ui.recorder.owner.as_ref() {
            if owner.phase == CapturePhase::Interrupted {
                if let Some(id) = owner.recording_id.clone() {
                    self.ui.recorder.seen_interrupted.insert(id);
                }
            }
        }
        if let Some(core) = self.ui.recorder.core.as_ref() {
            if core.capture_state == CapturePhase::Interrupted {
                self.ui.recorder.seen_interrupted.insert(core.id.clone());
            }
        }
    }

    /// Esc, the close affordance and Minimize: back to the recorder page, then
    /// hide the modal. Never stops, discards or deletes.
    pub fn recorder_minimize(&mut self) {
        if self.ui.recorder.page == Page::Recent {
            self.ui.recorder.page = Page::Recorder;
            self.recorder_sync_focus();
            return;
        }
        if self.recorder_modal() {
            self.recorder_mark_seen();
            self.close_overlay();
        }
    }

    /// Start one capture. One idempotency key per press; the mic opens only
    /// after Core grants the lease.
    pub fn recorder_start(&mut self) {
        if self.recorder_active() {
            return;
        }
        if let Some(reason) = self.ui.recorder.startup_block.clone().or_else(|| {
            (self.ui.recorder.pending_starts > 0)
                .then(|| "A previous recording start is still being recovered.".to_owned())
        }) {
            self.ui.recorder.owner = None;
            self.ui.recorder.failure = Some(FailureInput {
                kind: "startup".into(),
                reason: Some(reason),
            });
            self.recorder_sync_focus();
            return;
        }
        // Dictation and the Manager share the microphone owner. Refuse before
        // asking Core, so the shared busy text appears instead of a device error.
        if let Some(owner) = crate::audio_capture::current_mic_owner() {
            let message = format!("The microphone is already in use by {}.", owner.label());
            self.ui.recorder.owner = None;
            self.ui.recorder.failure = Some(FailureInput {
                kind: "mic".into(),
                reason: self.mic_busy_text(&message),
            });
            self.recorder_sync_focus();
            return;
        }
        let key = self
            .ui
            .recorder
            .start_key
            .clone()
            .unwrap_or_else(|| format!("tui:{}", uuid::Uuid::new_v4().simple()));
        self.ui.recorder.start_key = Some(key.clone());
        self.ui.recorder.failure = None;
        self.ui.recorder.owner = Some(RecorderOwner {
            phase: CapturePhase::Starting,
            recording_id: None,
            captured_samples: 0,
            pending_samples: 0,
            spool: SpoolKind::Persistent,
            recoverable: false,
            reason: None,
            level: 0.0,
            capture_open: false,
        });
        self.recorder_request(RecorderAction::Start {
            title: None,
            idempotency_key: key,
        });
        self.recorder_sync_focus();
    }

    pub fn recorder_stop(&mut self) {
        if !self.recorder_owner_active() {
            return;
        }
        self.recorder_request(RecorderAction::Stop);
        if let Some(owner) = self.ui.recorder.owner.as_mut() {
            owner.phase = CapturePhase::Stopping;
            owner.capture_open = false;
        }
        self.recorder_sync_focus();
    }

    pub fn recorder_copy(&mut self) {
        if let Some(id) = self.recorder_fetch_id() {
            self.recorder_request(RecorderAction::CopyTranscript { id });
        }
    }

    pub fn recorder_retry(&mut self) {
        if let Some(id) = self.recorder_fetch_id() {
            self.recorder_request(RecorderAction::Retry { id });
        }
    }

    pub fn recorder_recover(&mut self) {
        self.recorder_request(RecorderAction::Recover);
    }

    pub fn recorder_recent(&mut self) {
        self.ui.recorder.page = Page::Recent;
        self.recorder_request(RecorderAction::List);
        self.recorder_sync_focus();
    }

    /// Delete… asks first. It is refused for an active recording.
    pub fn recorder_delete(&mut self) {
        if self.ui.recorder.page != Page::Recent || self.recorder_active() {
            return;
        }
        let index = self
            .ui
            .recorder
            .focus
            .strip_prefix("recent.")
            .and_then(|n| n.parse::<usize>().ok());
        let Some(id) = index
            .and_then(|index| self.ui.recorder.recent.get(index))
            .map(|recording| recording.id.clone())
        else {
            return;
        };
        self.ui.recorder.dialog = Some(RecorderDialog::Delete { id, button: 1 });
    }

    /// `app.quit`: confirm while this client owns an active capture.
    pub fn recorder_request_quit(&mut self) -> bool {
        if !self.recorder_owner_active() {
            return false;
        }
        if !self.recorder_modal() {
            self.open_overlay(OverlayKind::Recorder);
            self.ui.recorder.page = Page::Recorder;
            self.recorder_sync_focus();
        }
        self.ui.recorder.dialog = Some(RecorderDialog::Quit { button: 1 });
        true
    }

    pub fn recorder_dialog_move(&mut self, back: bool) {
        let next = if back { 1 } else { 0 };
        match self.ui.recorder.dialog.as_mut() {
            Some(RecorderDialog::Quit { button }) => *button = next,
            Some(RecorderDialog::Delete { button, .. }) => *button = next,
            None => {}
        }
    }

    pub fn recorder_dialog_activate(&mut self) {
        let Some(dialog) = self.ui.recorder.dialog.clone() else {
            return;
        };
        match dialog {
            RecorderDialog::Quit { button } => {
                self.ui.recorder.dialog = None;
                if button == 1 {
                    self.recorder_sync_focus();
                    return;
                }
                self.recorder_stop();
                self.ui.recorder.pending_quit = Some(self.clock + QUIT_DRAIN);
            }
            RecorderDialog::Delete { id, button } => {
                self.ui.recorder.dialog = None;
                if button == 1 {
                    self.recorder_sync_focus();
                    return;
                }
                self.recorder_request(RecorderAction::Delete { id });
            }
        }
    }

    pub fn recorder_dialog_cancel(&mut self) {
        self.ui.recorder.dialog = None;
        self.recorder_sync_focus();
    }

    /// The modal's keys while it is open (or its dialog is).
    pub fn recorder_key(&mut self, name: &str) -> bool {
        if self.ui.recorder.dialog.is_some() {
            match name {
                "esc" => self.recorder_dialog_cancel(),
                "left" | "shift+tab" => self.recorder_dialog_move(true),
                "right" | "tab" => self.recorder_dialog_move(false),
                "enter" | "space" => self.recorder_dialog_activate(),
                _ => {}
            }
            return true;
        }
        let view = self.recorder_view(Width::Wide);
        let ids = self.recorder_focus_ids(&view);
        match name {
            "esc" => {
                self.recorder_minimize();
                true
            }
            "tab" | "shift+tab" => {
                if !ids.is_empty() {
                    let at = ids.iter().position(|id| *id == self.ui.recorder.focus);
                    let next = match at {
                        Some(at) => {
                            if name == "shift+tab" {
                                (at + ids.len() - 1) % ids.len()
                            } else {
                                (at + 1) % ids.len()
                            }
                        }
                        None => 0,
                    };
                    self.ui.recorder.focus = ids[next].clone();
                }
                true
            }
            "up" => {
                self.recorder_scroll(-1);
                true
            }
            "down" => {
                self.recorder_scroll(1);
                true
            }
            "pageup" => {
                self.recorder_scroll(-8);
                true
            }
            "pagedown" => {
                self.recorder_scroll(8);
                true
            }
            "home" => {
                self.recorder_scroll_edges(true);
                true
            }
            "end" => {
                self.recorder_scroll_to_live();
                true
            }
            "enter" | "space" => {
                self.recorder_activate_focus();
                true
            }
            _ => false,
        }
    }

    fn recorder_scroll(&mut self, delta: isize) {
        let view = self.recorder_view(Width::Wide);
        if self.ui.recorder.page == Page::Recent {
            let rows = view.recent.as_ref().map_or(0, |recent| recent.rows.len());
            if rows == 0 {
                return;
            }
            let current = self
                .ui
                .recorder
                .focus
                .strip_prefix("recent.")
                .and_then(|n| n.parse::<usize>().ok())
                .unwrap_or(0);
            let next = (current as isize + delta).clamp(0, rows as isize - 1) as usize;
            self.ui.recorder.focus = format!("recent.{next}");
            return;
        }
        let id = self.ui.recorder.shown_id().unwrap_or_default();
        let back = self.ui.recorder.scroll_back.entry(id).or_insert(0);
        if delta < 0 {
            *back = back.saturating_add(delta.unsigned_abs());
        } else {
            *back = back.saturating_sub(delta as usize);
        }
    }

    fn recorder_scroll_edges(&mut self, top: bool) {
        if self.ui.recorder.page == Page::Recent {
            let view = self.recorder_view(Width::Wide);
            let rows = view.recent.as_ref().map_or(0, |recent| recent.rows.len());
            self.ui.recorder.focus = if top || rows == 0 {
                "recent.0".into()
            } else {
                format!("recent.{}", rows - 1)
            };
            return;
        }
        let view = self.recorder_view(Width::Wide);
        let id = self.ui.recorder.shown_id().unwrap_or_default();
        let back = self.ui.recorder.scroll_back.entry(id).or_insert(0);
        *back = if top { view.rows.len() } else { 0 };
    }

    fn recorder_scroll_to_live(&mut self) {
        if self.ui.recorder.page == Page::Recent {
            return;
        }
        let id = self.ui.recorder.shown_id().unwrap_or_default();
        self.ui.recorder.scroll_back.insert(id, 0);
    }

    pub(crate) fn recorder_activate_focus(&mut self) {
        let focus = self.ui.recorder.focus.clone();
        if focus == "jump_live" {
            self.recorder_scroll_to_live();
            return;
        }
        if let Some(index) = focus
            .strip_prefix("recent.")
            .and_then(|n| n.parse::<usize>().ok())
        {
            if let Some(id) = self
                .ui
                .recorder
                .recent
                .get(index)
                .map(|recording| recording.id.clone())
            {
                self.ui.recorder.current_id = Some(id.clone());
                self.ui.recorder.page = Page::Recorder;
                self.ui.recorder.transcript.clear();
                self.recorder_request(RecorderAction::Detail { id: id.clone() });
                self.recorder_request(RecorderAction::Transcript { id });
                self.recorder_sync_focus();
            }
            return;
        }
        self.run_command(&focus);
    }

    /// Fold a driver reply into the UI.
    pub fn recorder_update(&mut self, update: RecorderUpdate) {
        match update {
            RecorderUpdate::Status {
                owner,
                recoverable,
                warnings,
            } => {
                if let Some(id) = owner.recording_id.clone() {
                    if owner.active() || self.ui.recorder.current_id.is_none() {
                        self.ui.recorder.current_id = Some(id);
                    }
                }
                self.ui.recorder.recoverable = recoverable.clone();
                let mut owner = owner;
                owner.recoverable = owner
                    .recording_id
                    .as_ref()
                    .is_some_and(|id| recoverable.contains(id));
                if owner.phase == CapturePhase::Failed {
                    if let Some(reason) = owner.reason.clone() {
                        let lower = reason.to_lowercase();
                        if lower.contains("busy")
                            || lower.contains("another client")
                            || lower.contains("another recorder")
                        {
                            owner.reason =
                                Some(self.manifest.ui_text("recorder.busy.other_client", &[]));
                        } else if let Some(text) = self.mic_busy_text(&reason) {
                            owner.reason = Some(text);
                        }
                        if lower.contains("create guard") || lower.contains("restart core") {
                            self.ui.recorder.startup_block = owner.reason.clone();
                        }
                        if let Some(text) = self.recorder_storage_failure("", &reason) {
                            owner.reason = Some(text.clone());
                            self.ui.recorder.startup_block = Some(text);
                        }
                    }
                }
                if owner.phase == CapturePhase::Failed && owner.recording_id.is_none() {
                    // Nothing was created and the microphone never opened: this
                    // is a failed Start, not an interrupted capture.
                    self.ui.recorder.owner = None;
                    self.ui.recorder.failure = Some(FailureInput {
                        kind: "start".into(),
                        reason: owner.reason.clone(),
                    });
                } else {
                    self.ui.recorder.owner = Some(owner);
                }
                for warning in warnings {
                    let warning = match warning
                        .strip_prefix(crate::recorder_driver::LOW_DISK_PREFIX)
                    {
                        Some(t) => self.manifest.ui_text("recorder.low_disk", &[("t", t)]),
                        None => warning,
                    };
                    self.toast(Level::Warning, warning);
                }
                self.recorder_sync_focus();
            }
            RecorderUpdate::List(result) => match result {
                Ok(values) => {
                    let had_id = self.recorder_fetch_id().is_some() || self.recorder_owner_active();
                    let rows: Vec<RecentRecording> =
                        values.iter().filter_map(recent_from_value).collect();
                    self.ui.recorder.recent = rows;
                    if self.ui.recorder.page == Page::Recorder && !had_id {
                        if let Some(first) = values.first() {
                            let id = first["id"].as_str().unwrap_or_default().to_owned();
                            self.ui.recorder.current_id = Some(id.clone());
                            self.ui.recorder.core = core_from_value(&id, first);
                            self.recorder_request(RecorderAction::Detail { id: id.clone() });
                            self.recorder_request(RecorderAction::Transcript { id });
                        }
                    }
                    if self.ui.recorder.page == Page::Recent
                        && !self.ui.recorder.focus.starts_with("recent.")
                    {
                        self.ui.recorder.focus = self.recorder_view(Width::Wide).focus;
                    }
                    self.recorder_sync_focus();
                }
                Err(error) => self.toast(Level::Warning, error),
            },
            RecorderUpdate::Detail(result) => match result {
                Ok(value) => {
                    let id = value["id"]
                        .as_str()
                        .or(self.ui.recorder.current_id.as_deref())
                        .unwrap_or_default()
                        .to_owned();
                    if let Some(core) = core_from_value(&id, &value) {
                        self.ui.recorder.current_id = Some(core.id.clone());
                        self.ui.recorder.core = Some(core);
                        self.recorder_sync_focus();
                    }
                }
                Err(error) => self.toast(Level::Warning, error),
            },
            RecorderUpdate::Transcript { id, page } => match page {
                Ok(page) => {
                    let generation = self
                        .ui
                        .recorder
                        .core
                        .as_ref()
                        .filter(|core| core.id == id)
                        .map(|core| core.generation)
                        .or(self.ui.recorder.transcript.generation);
                    if let Some(input) = transcript_input(&id, generation, &page) {
                        if self.ui.recorder.transcript.recording_id.as_deref() != Some(id.as_str())
                        {
                            self.ui.recorder.transcript.clear();
                        }
                        self.ui.recorder.transcript.replace_page(&input);
                        self.ui.recorder.current_id = Some(id);
                    }
                }
                Err(error) => self.toast(Level::Warning, error),
            },
            RecorderUpdate::Retry(result) => match result {
                Ok(()) => {
                    if let Some(id) = self.recorder_fetch_id() {
                        self.recorder_request(RecorderAction::Detail { id: id.clone() });
                        self.recorder_request(RecorderAction::Transcript { id });
                    }
                }
                Err(error) => self.toast(Level::Warning, error),
            },
            RecorderUpdate::Error(error) => self.toast(Level::Warning, error),
            RecorderUpdate::Copied { complete, text } => {
                self.effects.push(Effect::Copy(text));
                let key = if complete {
                    "recorder.toast.copied"
                } else {
                    "recorder.toast.copied_partial"
                };
                self.toast(Level::Success, self.manifest.ui_text(key, &[]));
            }
            RecorderUpdate::Deleted { id, result } => match result {
                Ok(()) => {
                    self.ui.recorder.recent.retain(|row| row.id != id);
                    if self.ui.recorder.current_id.as_deref() == Some(id.as_str()) {
                        self.ui.recorder.current_id = None;
                        self.ui.recorder.core = None;
                        self.ui.recorder.transcript.clear();
                    }
                    if self.ui.recorder.page == Page::Recent {
                        self.recorder_request(RecorderAction::List);
                    }
                    self.recorder_sync_focus();
                }
                Err(error) => {
                    let text = if error.contains("recording_export_active") {
                        self.manifest.ui_text("recorder.delete.busy", &[])
                    } else {
                        error
                    };
                    self.toast(Level::Warning, text);
                }
            },
            RecorderUpdate::Recovered(result) => match result {
                Ok(report) => {
                    if report.recovered > 0 {
                        self.recorder_refresh(true);
                    }
                    for issue in report.issues {
                        self.toast(Level::Warning, issue.detail);
                    }
                }
                Err(error) => self.toast(Level::Warning, error),
            },
            RecorderUpdate::Startup {
                result,
                pending,
                ready,
            } => {
                self.ui.recorder.pending_starts = pending;
                let mut block: Option<String> = None;
                let mut recovered = 0;
                match &result {
                    Ok(report) => {
                        recovered = report.recovered;
                        for issue in &report.issues {
                            let detail = self
                                .recorder_storage_failure("", &issue.detail)
                                .unwrap_or_else(|| issue.detail.clone());
                            if block.is_none()
                                && issue.kind
                                    == crate::recorder_driver::RecoveryIssueKind::CoreRestartRequired
                            {
                                block = Some(detail.clone());
                            }
                            self.toast(Level::Warning, detail);
                        }
                    }
                    Err(error) => {
                        let detail = self
                            .recorder_storage_failure("", error)
                            .unwrap_or_else(|| error.clone());
                        self.toast(Level::Warning, detail.clone());
                        block = Some(detail);
                    }
                }
                if ready {
                    self.ui.recorder.startup_block = None;
                    if self
                        .ui
                        .recorder
                        .failure
                        .as_ref()
                        .is_some_and(|failure| failure.kind == "startup")
                    {
                        self.ui.recorder.failure = None;
                    }
                    if self.ui.recorder.owner.as_ref().is_some_and(|owner| {
                        owner.phase == CapturePhase::Failed && owner.recording_id.is_none()
                    }) {
                        self.ui.recorder.owner = None;
                        self.ui.recorder.failure = None;
                    }
                } else {
                    let reason = block
                        .or_else(|| {
                            result.as_ref().ok().and_then(|report| {
                                report.issues.first().map(|issue| issue.detail.clone())
                            })
                        })
                        .unwrap_or_else(|| {
                            "A previous recording start is still being recovered.".to_owned()
                        });
                    let reason = self.recorder_storage_failure("", &reason).unwrap_or(reason);
                    self.ui.recorder.startup_block = Some(reason.clone());
                    if self
                        .ui
                        .recorder
                        .owner
                        .as_ref()
                        .is_none_or(|owner| !owner.active())
                    {
                        self.ui.recorder.owner = None;
                        self.ui.recorder.failure = Some(FailureInput {
                            kind: "startup".into(),
                            reason: Some(reason),
                        });
                    }
                }
                if recovered > 0 || self.recorder_modal() || self.recorder_active() {
                    self.recorder_refresh(true);
                }
                self.recorder_sync_focus();
            }
            RecorderUpdate::Recoverable(rows) => {
                let ids: Vec<String> = rows.iter().map(|row| row.id.clone()).collect();
                self.ui.recorder.recoverable = ids;
                let recoverable = self.ui.recorder.recoverable.clone();
                if let Some(owner) = self.ui.recorder.owner.as_mut() {
                    if owner.phase == CapturePhase::Interrupted {
                        owner.recoverable = owner
                            .recording_id
                            .as_ref()
                            .is_some_and(|id| recoverable.contains(id));
                    }
                } else if let Some(row) = rows.first() {
                    self.ui.recorder.owner = Some(RecorderOwner {
                        phase: CapturePhase::Interrupted,
                        recording_id: Some(row.id.clone()),
                        captured_samples: row.accepted_samples,
                        pending_samples: row.pending_samples,
                        spool: SpoolKind::Persistent,
                        recoverable: true,
                        reason: Some(
                            self.manifest
                                .ui_text("recorder.reason.stream_detached", &[]),
                        ),
                        level: 0.0,
                        capture_open: false,
                    });
                    if self.ui.recorder.current_id.is_none() {
                        self.ui.recorder.current_id = Some(row.id.clone());
                    }
                }
                self.recorder_sync_focus();
            }
            RecorderUpdate::Failed {
                kind,
                message: reason,
            } => {
                let (kind, message) =
                    if let Some(text) = self.recorder_storage_failure(&kind, &reason) {
                        ("storage".to_owned(), text)
                    } else {
                        (kind, reason)
                    };
                if kind == "startup" || kind == "storage" {
                    self.ui.recorder.startup_block = Some(message.clone());
                }
                self.ui.recorder.owner = None;
                self.ui.recorder.failure = Some(FailureInput {
                    kind,
                    reason: Some(message),
                });
                self.recorder_sync_focus();
            }
            RecorderUpdate::Reconnected => {
                // Load the authoritative recorder state on every connection, so
                // an interrupted recording is visible in the header before the
                // modal is ever opened.
                self.recorder_refresh(true);
            }
        }
    }

    /// Apply one global recorder event. Late revisions for another recording
    /// are ignored; a segment keeps the greatest revision.
    pub fn recorder_event(&mut self, kind: &str, data: &Value) {
        match kind {
            "recorder.state" => {
                let Some(id) = data["id"].as_str() else {
                    return;
                };
                let revision = data["revision"].as_u64().unwrap_or(0);
                if self
                    .ui
                    .recorder
                    .core
                    .as_ref()
                    .is_some_and(|core| core.id == id && core.revision > revision)
                {
                    return;
                }
                let Some(core) = core_from_value(id, data) else {
                    return;
                };
                if core.active {
                    if self
                        .ui
                        .recorder
                        .owner
                        .as_ref()
                        .is_some_and(|owner| !owner.active())
                    {
                        self.ui.recorder.owner = None;
                    }
                }
                let previous = self
                    .ui
                    .recorder
                    .core
                    .as_ref()
                    .map(|core| core.capture_state);
                if self.ui.recorder.current_id.is_none() || core.active {
                    self.ui.recorder.current_id = Some(core.id.clone());
                }
                if let Some(owner) = self.ui.recorder.owner.as_mut() {
                    if owner.recording_id.as_deref() == Some(id) {
                        match core.capture_state {
                            CapturePhase::Stopped
                                if owner.active() || owner.phase == CapturePhase::Stopping =>
                            {
                                owner.phase = CapturePhase::Stopped;
                                owner.capture_open = false;
                            }
                            CapturePhase::Interrupted if owner.active() => {
                                owner.phase = CapturePhase::Interrupted;
                                owner.capture_open = false;
                                owner.recoverable = self
                                    .ui
                                    .recorder
                                    .recoverable
                                    .iter()
                                    .any(|id| id == core.id.as_str());
                            }
                            CapturePhase::Failed if owner.active() => {
                                owner.phase = CapturePhase::Failed;
                                owner.capture_open = false;
                            }
                            _ => {}
                        }
                    }
                }
                if matches!(
                    core.capture_state,
                    CapturePhase::Interrupted | CapturePhase::Failed
                ) && !matches!(
                    previous,
                    Some(CapturePhase::Interrupted | CapturePhase::Failed)
                ) {
                    self.toast(
                        Level::Warning,
                        self.manifest.ui_text("recorder.header.interrupted", &[]),
                    );
                }
                self.ui.recorder.core = Some(core);
                self.recorder_sync_focus();
            }
            "recorder.segment" => {
                let Some(id) = data["id"].as_str() else {
                    return;
                };
                let Some(segment) = segment_from_value(data) else {
                    return;
                };
                let generation = data["generation"].as_u64();
                if self.ui.recorder.current_id.is_none() {
                    self.ui.recorder.current_id = Some(id.to_owned());
                }
                if self.ui.recorder.transcript.recording_id.as_deref() != Some(id) {
                    self.ui.recorder.transcript.clear();
                    self.ui.recorder.transcript.recording_id = Some(id.to_owned());
                }
                if self.ui.recorder.transcript.generation.is_none() {
                    self.ui.recorder.transcript.generation = generation;
                }
                self.ui
                    .recorder
                    .transcript
                    .apply_event(Some(id), generation, &segment);
                self.recorder_sync_focus();
            }
            "recorder.deleted" => {
                let Some(id) = data["id"].as_str() else {
                    return;
                };
                self.ui.recorder.recent.retain(|row| row.id != id);
                if self.ui.recorder.current_id.as_deref() == Some(id)
                    || self
                        .ui
                        .recorder
                        .core
                        .as_ref()
                        .is_some_and(|core| core.id == id)
                {
                    self.ui.recorder.current_id = None;
                    self.ui.recorder.core = None;
                    self.ui.recorder.transcript.clear();
                }
                self.ui.recorder.seen_interrupted.remove(id);
                self.recorder_sync_focus();
            }
            _ => {}
        }
    }

    /// Called on every `server.connected`: discard the live-only provisional
    /// tail and re-read the authoritative pages.
    pub fn recorder_reconnected(&mut self) {
        self.recorder_update(RecorderUpdate::Reconnected);
    }

    /// One tick: the Stop and exit drain, and a fast refresh while recording.
    pub fn recorder_tick(&mut self, now: Instant) -> bool {
        let mut changed = false;
        if let Some(at) = self.ui.recorder.pending_quit {
            if !self.recorder_owner_active() || now >= at {
                self.ui.recorder.pending_quit = None;
                self.effects.push(Effect::Quit);
            }
            changed = true;
        }
        changed
    }
}
