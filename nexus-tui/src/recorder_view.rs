//! Pure recorder view-model (plan 54, milestone V-TUI).
//!
//! This maps the local capture snapshot, Core's public recording state, the
//! transcript page and the client's view flags to exactly what the recorder
//! shows: header pill and tone, the three status rows, the action row, initial
//! focus, Esc behaviour, the key hint and the Recent list. It performs no I/O
//! and touches no controller, layout or input state, so both clients can be
//! tested against `interface/scenarios/recorder-view.json` before either UI is
//! wired.
//!
//! Wording comes from `[strings]` through [`Manifest::ui_text`]. The optional
//! `source` input produces the device/destination row; the shared fixture does
//! not exercise it, and the UI wiring supplies the controller device and Core
//! host once capture is connected.

use std::collections::BTreeMap;

use serde::Deserialize;

use crate::manifest::Manifest;

/// Recorder audio is mono 16 kHz PCM16; every offset below is in samples.
pub const SAMPLE_RATE: u64 = 16_000;
/// Durable transcript segments kept for one open recording (over ten hours).
pub const MAX_TRANSCRIPT_SEGMENTS: usize = 5_000;
/// `GET /api/recordings` returns at most this many recordings.
const RECENT_LIMIT: usize = 50;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Width {
    Wide,
    Compact,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Page {
    #[default]
    Recorder,
    Recent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CapturePhase {
    Idle,
    Starting,
    Recording,
    Stopping,
    Stopped,
    Interrupted,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TranscriptionState {
    Preparing,
    Live,
    CatchingUp,
    Failed,
    Complete,
    Unavailable,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
pub enum SpoolKind {
    #[serde(rename = "persistent")]
    Persistent,
    #[serde(rename = "best-effort")]
    BestEffort,
    #[serde(rename = "memory")]
    Memory,
    #[serde(rename = "none")]
    #[default]
    None,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SegmentStatus {
    Provisional,
    Final,
    Pending,
    Error,
    Skipped,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RecorderViewInput {
    #[serde(default)]
    pub owner: Option<OwnerInput>,
    #[serde(default)]
    pub core: Option<CoreInput>,
    #[serde(default)]
    pub transcript: Option<TranscriptInput>,
    #[serde(default)]
    pub failure: Option<FailureInput>,
    #[serde(default)]
    pub recent: Option<RecentInput>,
    #[serde(default)]
    pub source: Option<SourceInput>,
    pub view: ViewInput,
    pub width: Width,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct SourceInput {
    #[serde(default)]
    pub device: Option<String>,
    #[serde(default)]
    pub host: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OwnerInput {
    pub phase: CapturePhase,
    #[serde(default)]
    pub recording_id: Option<String>,
    #[serde(default)]
    pub captured_samples: u64,
    #[serde(default)]
    pub pending_samples: u64,
    #[serde(default)]
    pub spool: SpoolKind,
    #[serde(default)]
    pub recoverable: bool,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub level: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CoreInput {
    pub id: String,
    #[serde(default)]
    pub generation: u64,
    #[serde(default)]
    pub revision: u64,
    pub capture_state: CapturePhase,
    #[serde(default)]
    pub active: bool,
    #[serde(default)]
    pub durable_offset: u64,
    #[serde(default)]
    pub final_sample_count: Option<u64>,
    #[serde(default)]
    pub complete: bool,
    #[serde(default)]
    pub holes: u64,
    #[serde(default)]
    pub missing_samples: Option<u64>,
    #[serde(default)]
    pub owner: Option<CoreOwner>,
    pub transcription_state: TranscriptionState,
    #[serde(default)]
    pub transcription_progress: TranscriptionProgress,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CoreOwner {
    #[serde(default)]
    pub client_id: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct TranscriptionProgress {
    #[serde(default)]
    pub lag_samples: u64,
    #[serde(default)]
    pub pending_finals: u64,
    #[serde(default)]
    pub failed: u64,
    #[serde(default)]
    pub transcript_revision: Option<u64>,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FailureInput {
    pub kind: String,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct RecentInput {
    #[serde(default)]
    pub recordings: Vec<RecentRecording>,
    #[serde(default = "default_recent_limit")]
    pub limit: usize,
}

fn default_recent_limit() -> usize {
    RECENT_LIMIT
}

#[derive(Debug, Clone, Deserialize)]
pub struct RecentRecording {
    #[serde(default)]
    pub id: String,
    pub capture_state: CapturePhase,
    #[serde(default)]
    pub created_at: String,
    #[serde(default)]
    pub durable_offset: u64,
    #[serde(default)]
    pub final_sample_count: Option<u64>,
    pub transcription_state: TranscriptionState,
    #[serde(default)]
    pub holes: u64,
    #[serde(default)]
    pub active: bool,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct TranscriptInput {
    #[serde(default)]
    pub recording_id: Option<String>,
    #[serde(default)]
    pub generation: Option<u64>,
    #[serde(default)]
    pub segments: Vec<SegmentInput>,
    #[serde(default)]
    pub provisional: Option<ProvisionalInput>,
    /// Older durable segments already dropped by the retention cap.
    #[serde(default)]
    pub dropped: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SegmentInput {
    #[serde(default)]
    pub segment_id: u32,
    #[serde(default)]
    pub revision: u64,
    #[serde(default)]
    pub start: u64,
    #[serde(default)]
    pub end: Option<u64>,
    pub status: SegmentStatus,
    #[serde(default)]
    pub text: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ProvisionalInput {
    #[serde(default)]
    pub segment_id: u32,
    #[serde(default)]
    pub revision: u64,
    #[serde(default)]
    pub start: u64,
    #[serde(default)]
    pub text: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ViewInput {
    #[serde(default)]
    pub modal_open: bool,
    #[serde(default)]
    pub page: Page,
    #[serde(default)]
    pub transcript_follow: bool,
    #[serde(default)]
    pub seen_interrupted: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Secondary,
    Error,
    Warning,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EscAction {
    Close,
    Minimize,
    Back,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecorderButton {
    pub id: String,
    pub label: String,
    pub enabled: bool,
    /// Why a button is disabled; the hint row shows it while focused.
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentView {
    pub empty: Option<String>,
    pub notice: Option<String>,
    pub rows: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TranscriptRowKind {
    Earlier,
    Final,
    Pending,
    Error,
    Provisional,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptRow {
    pub kind: TranscriptRowKind,
    pub start: Option<u64>,
    pub end: Option<u64>,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RecorderView {
    /// Header label for `input.width`.
    pub header: String,
    pub header_wide: String,
    pub header_compact: String,
    pub header_tone: Tone,
    pub a11y: String,
    pub source: Option<String>,
    pub capture: Option<String>,
    pub audio: Option<String>,
    /// A second persistence line; never replaces `audio`.
    pub audio_detail: Option<String>,
    pub audio_warning: Option<String>,
    pub transcript_status: Option<String>,
    /// The Core-supplied reason for `Transcript unavailable`, if any.
    pub transcript_detail: Option<String>,
    /// Focusable, actionable buttons in action-row order (the first is primary).
    pub buttons: Vec<RecorderButton>,
    /// Buttons drawn disabled (they take no focus); the reason is the hint.
    pub disabled: Vec<RecorderButton>,
    pub focus: String,
    pub esc: EscAction,
    pub hint: String,
    pub recent: Option<RecentView>,
    pub rows: Vec<TranscriptRow>,
    /// Smoothed level while this client owns the recording.
    pub level: Option<f64>,
    pub follow: bool,
    pub jump_live: bool,
}

/// Format captured samples as `mm:ss` below one hour and `h:mm:ss` from it.
pub fn format_elapsed(samples: u64) -> String {
    let seconds = samples / SAMPLE_RATE;
    let (hours, minutes, seconds) = (seconds / 3_600, (seconds / 60) % 60, seconds % 60);
    if hours > 0 {
        format!("{hours}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes:02}:{seconds:02}")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Idle,
    Starting,
    Recording {
        samples: u64,
    },
    Elsewhere {
        samples: u64,
    },
    Stopping {
        samples: u64,
    },
    Finishing,
    Stopped,
    Interrupted {
        recoverable: bool,
    },
    /// This client's capture failed after it started (or before Core granted it).
    LocalFailed,
    /// Core reports a failed capture this client does not own.
    Failed,
    FailedBeforeCreate,
}

/// Produce everything the recorder UI shows for one snapshot of the contract.
pub fn recorder_view(manifest: &Manifest, input: &RecorderViewInput) -> RecorderView {
    if input.view.page == Page::Recent {
        return recent_view(manifest, input);
    }

    let mode = mode(input);
    let (header_wide, header_compact, header_tone, a11y) = header(manifest, input, mode);
    let header = if input.width == Width::Compact {
        header_compact.clone()
    } else {
        header_wide.clone()
    };
    let (capture, audio, audio_detail, audio_warning) = status(manifest, input, mode);
    let (transcript_status, transcript_detail) = transcript_status(manifest, input);
    let (buttons, disabled, focus, esc, hint) = actions(manifest, input, mode);
    let transcript = input
        .transcript
        .as_ref()
        .map(TranscriptStore::from_page)
        .unwrap_or_default();
    let follow = input.view.transcript_follow;
    RecorderView {
        header,
        header_wide,
        header_compact,
        header_tone,
        a11y,
        source: source_line(manifest, input),
        capture,
        audio,
        audio_detail,
        audio_warning,
        transcript_status: Some(transcript_status),
        transcript_detail,
        buttons,
        disabled,
        focus,
        esc,
        hint,
        recent: None,
        rows: transcript.rows(manifest),
        level: input
            .owner
            .as_ref()
            .filter(|o| o.phase == CapturePhase::Recording)
            .map(|o| o.level),
        follow,
        jump_live: !follow,
    }
}

fn mode(input: &RecorderViewInput) -> Mode {
    let owner = input.owner.as_ref();
    if let Some(owner) = owner {
        match owner.phase {
            CapturePhase::Starting => return Mode::Starting,
            CapturePhase::Recording => {
                return Mode::Recording {
                    samples: owner.captured_samples,
                }
            }
            CapturePhase::Stopping => {
                return Mode::Stopping {
                    samples: owner.captured_samples,
                }
            }
            CapturePhase::Interrupted => {
                return Mode::Interrupted {
                    recoverable: owner.recoverable,
                }
            }
            CapturePhase::Failed => return Mode::LocalFailed,
            CapturePhase::Idle => {}
            CapturePhase::Stopped => {
                return match input.core.as_ref() {
                    Some(core)
                        if core.capture_state == CapturePhase::Stopped
                            && matches!(
                                core.transcription_state,
                                TranscriptionState::CatchingUp | TranscriptionState::Preparing
                            ) =>
                    {
                        Mode::Finishing
                    }
                    _ => Mode::Stopped,
                }
            }
        }
    }
    if let Some(core) = input.core.as_ref() {
        return match core.capture_state {
            CapturePhase::Starting | CapturePhase::Recording | CapturePhase::Stopping => {
                Mode::Elsewhere {
                    samples: core.durable_offset,
                }
            }
            CapturePhase::Stopped => {
                if matches!(
                    core.transcription_state,
                    TranscriptionState::CatchingUp | TranscriptionState::Preparing
                ) {
                    Mode::Finishing
                } else {
                    Mode::Stopped
                }
            }
            CapturePhase::Interrupted => Mode::Interrupted { recoverable: false },
            CapturePhase::Failed => Mode::Failed,
            CapturePhase::Idle => Mode::Idle,
        };
    }
    if owner.is_some_and(|o| o.phase == CapturePhase::Stopped) {
        Mode::Stopped
    } else if input.failure.is_some() {
        Mode::FailedBeforeCreate
    } else {
        Mode::Idle
    }
}

fn header(
    manifest: &Manifest,
    input: &RecorderViewInput,
    mode: Mode,
) -> (String, String, Tone, String) {
    let seen = input.view.seen_interrupted;
    match mode {
        Mode::Starting => (
            manifest.ui_text("recorder.header.starting", &[]),
            manifest.ui_text("recorder.header.starting_compact", &[]),
            Tone::Error,
            manifest.ui_text("recorder.a11y.starting", &[]),
        ),
        Mode::Recording { samples } => {
            let t = format_elapsed(samples);
            (
                manifest.ui_text("recorder.header.recording", &[("t", t.as_str())]),
                manifest.ui_text("recorder.header.recording_compact", &[("t", t.as_str())]),
                Tone::Error,
                manifest.ui_text("recorder.a11y.recording", &[("t", t.as_str())]),
            )
        }
        Mode::Elsewhere { .. } => (
            manifest.ui_text("recorder.header.elsewhere", &[]),
            manifest.ui_text("recorder.header.elsewhere_compact", &[]),
            Tone::Secondary,
            manifest.ui_text("recorder.a11y.elsewhere", &[]),
        ),
        Mode::Stopping { .. } => (
            manifest.ui_text("recorder.header.saving", &[]),
            manifest.ui_text("recorder.header.saving_compact", &[]),
            Tone::Secondary,
            manifest.ui_text("recorder.a11y.saving", &[]),
        ),
        Mode::Finishing => (
            manifest.ui_text("recorder.header.finishing", &[]),
            manifest.ui_text("recorder.header.finishing_compact", &[]),
            Tone::Secondary,
            manifest.ui_text("recorder.a11y.finishing", &[]),
        ),
        Mode::Interrupted { .. } | Mode::LocalFailed | Mode::Failed if !seen => (
            manifest.ui_text("recorder.header.interrupted", &[]),
            manifest.ui_text("recorder.header.interrupted_compact", &[]),
            Tone::Warning,
            manifest.ui_text("recorder.a11y.interrupted", &[]),
        ),
        Mode::Idle
        | Mode::Stopped
        | Mode::Interrupted { .. }
        | Mode::LocalFailed
        | Mode::Failed
        | Mode::FailedBeforeCreate => (
            manifest.ui_text("recorder.header.idle", &[]),
            manifest.ui_text("recorder.header.idle", &[]),
            Tone::Secondary,
            manifest.ui_text("recorder.a11y.idle", &[]),
        ),
    }
}

fn status(
    manifest: &Manifest,
    input: &RecorderViewInput,
    mode: Mode,
) -> (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
) {
    let capture = capture_text(manifest, input, mode);
    let (audio, detail) = audio_text(manifest, input);
    let warning = input
        .owner
        .as_ref()
        .filter(|o| o.spool == SpoolKind::Memory)
        .map(|_| manifest.ui_text("recorder.audio.memory_only", &[]));
    (Some(capture), audio, detail, warning)
}

fn capture_text(manifest: &Manifest, input: &RecorderViewInput, mode: Mode) -> String {
    match mode {
        Mode::Idle => manifest.ui_text("recorder.capture.idle", &[]),
        Mode::Starting => manifest.ui_text("recorder.capture.starting", &[]),
        Mode::Recording { samples } => {
            let t = format_elapsed(samples);
            manifest.ui_text("recorder.capture.recording", &[("t", t.as_str())])
        }
        Mode::Elsewhere { samples } => {
            let t = format_elapsed(samples);
            if input
                .core
                .as_ref()
                .is_some_and(|core| core.capture_state == CapturePhase::Stopping)
            {
                manifest.ui_text("recorder.capture.stopping", &[("t", t.as_str())])
            } else {
                manifest.ui_text("recorder.capture.elsewhere", &[("t", t.as_str())])
            }
        }
        Mode::Stopping { samples } => {
            let t = format_elapsed(samples);
            manifest.ui_text("recorder.capture.stopping", &[("t", t.as_str())])
        }
        Mode::Finishing | Mode::Stopped => {
            let samples = input
                .owner
                .as_ref()
                .filter(|owner| owner.phase == CapturePhase::Stopped)
                .map(|owner| owner.captured_samples)
                .unwrap_or_else(|| stopped_samples(input));
            let t = format_elapsed(samples);
            manifest.ui_text("recorder.capture.stopped", &[("t", t.as_str())])
        }
        Mode::Interrupted { .. } => {
            let t = format_elapsed(interrupted_samples(input));
            let reason = interrupted_reason(manifest, input);
            interrupted_capture_text(manifest, &t, &reason)
        }
        Mode::LocalFailed | Mode::Failed => {
            let reason = failed_reason(manifest, input);
            manifest.ui_text("recorder.capture.failed", &[("reason", reason.as_str())])
        }
        Mode::FailedBeforeCreate => {
            let reason = before_create_reason(input);
            manifest.ui_text("recorder.capture.failed", &[("reason", reason.as_str())])
        }
    }
}

fn audio_text(manifest: &Manifest, input: &RecorderViewInput) -> (Option<String>, Option<String>) {
    let owner = input.owner.as_ref();
    let pending_samples = owner
        .filter(|owner| {
            matches!(
                owner.phase,
                CapturePhase::Starting | CapturePhase::Recording | CapturePhase::Stopping
            )
        })
        .map(|owner| owner.pending_samples)
        .unwrap_or(0);
    let pending_secs = pending_samples / SAMPLE_RATE;
    let owner_pending_secs = owner
        .map(|owner| owner.pending_samples / SAMPLE_RATE)
        .unwrap_or(0);
    let core = input.core.as_ref();
    let holes = core.map(|c| c.holes).unwrap_or(0);
    let missing = core.and_then(|c| c.missing_samples);
    let durable = core.map(|c| c.durable_offset);
    let gaps = || gaps_text(manifest, holes, missing);

    let saved_through = |samples: u64, pending: u64| {
        let t = format_elapsed(samples);
        let pending = pending.to_string();
        manifest.ui_text(
            "recorder.audio.saved_through",
            &[("t", t.as_str()), ("s", pending.as_str())],
        )
    };
    let unsaved = |seconds: u64| {
        let s = seconds.to_string();
        manifest.ui_text("recorder.audio.unsaved", &[("s", s.as_str())])
    };
    let saved_detail = |samples: u64| {
        let t = format_elapsed(samples);
        manifest.ui_text("recorder.audio.saved_detail", &[("t", t.as_str())])
    };

    if let Some(core) = core {
        if owner.is_some_and(|owner| owner.phase == CapturePhase::Interrupted && owner.recoverable)
        {
            return (
                Some(unsaved(owner_pending_secs)),
                Some(saved_detail(core.durable_offset)),
            );
        }
        if holes > 0 {
            return (Some(gaps()), None);
        }
        if core.capture_state == CapturePhase::Stopped || core.complete {
            if core.complete {
                if matches!(
                    core.transcription_state,
                    TranscriptionState::CatchingUp | TranscriptionState::Preparing
                ) {
                    return (
                        Some(manifest.ui_text("recorder.audio.saved_finishing", &[])),
                        None,
                    );
                }
                return (
                    Some(manifest.ui_text("recorder.audio.all_saved", &[])),
                    None,
                );
            }
            return (Some(saved_through(core.durable_offset, pending_secs)), None);
        }
        if matches!(
            core.capture_state,
            CapturePhase::Starting
                | CapturePhase::Recording
                | CapturePhase::Stopping
                | CapturePhase::Interrupted
                | CapturePhase::Failed
        ) {
            return (Some(saved_through(core.durable_offset, pending_secs)), None);
        }
    }

    if owner.is_some_and(|owner| owner.phase == CapturePhase::Interrupted && owner.recoverable) {
        return (Some(unsaved(owner_pending_secs)), None);
    }
    if owner.is_some_and(|owner| {
        matches!(
            owner.phase,
            CapturePhase::Recording | CapturePhase::Stopping
        ) && pending_samples > 0
    }) {
        return (
            Some(saved_through(durable.unwrap_or(0), pending_secs)),
            None,
        );
    }
    (
        Some(manifest.ui_text("recorder.audio.all_saved", &[])),
        None,
    )
}

fn source_line(manifest: &Manifest, input: &RecorderViewInput) -> Option<String> {
    let source = input.source.as_ref()?;
    let device = source.device.as_ref().filter(|device| !device.is_empty());
    let microphone = match device {
        Some(device) => manifest.ui_text("recorder.source.device", &[("device", device.as_str())]),
        None => manifest.ui_text("recorder.source.default_device", &[]),
    };
    match source.host.as_ref().filter(|host| !host.is_empty()) {
        Some(host) => {
            let destination =
                manifest.ui_text("recorder.source.destination", &[("host", host.as_str())]);
            Some(format!("{microphone} · {destination}"))
        }
        None => Some(microphone),
    }
}

fn gaps_text(manifest: &Manifest, holes: u64, missing_samples: Option<u64>) -> String {
    let n = holes.to_string();
    match missing_samples {
        Some(samples) => {
            let s = (samples / SAMPLE_RATE).to_string();
            manifest.ui_text(
                "recorder.audio.gaps",
                &[("n", n.as_str()), ("s", s.as_str())],
            )
        }
        None => manifest.ui_text("recorder.audio.gaps_unknown", &[("n", n.as_str())]),
    }
}

fn transcript_status(manifest: &Manifest, input: &RecorderViewInput) -> (String, Option<String>) {
    if let Some(core) = input.core.as_ref() {
        return match core.transcription_state {
            TranscriptionState::Preparing => {
                (manifest.ui_text("recorder.transcript.preparing", &[]), None)
            }
            TranscriptionState::Live => (manifest.ui_text("recorder.transcript.live", &[]), None),
            TranscriptionState::CatchingUp => {
                let lag = (core.transcription_progress.lag_samples / SAMPLE_RATE).to_string();
                (
                    manifest.ui_text("recorder.transcript.catching_up", &[("s", lag.as_str())]),
                    None,
                )
            }
            TranscriptionState::Failed => {
                let n = core.transcription_progress.failed.max(1).to_string();
                (
                    manifest.ui_text("recorder.transcript.failed", &[("n", n.as_str())]),
                    None,
                )
            }
            TranscriptionState::Complete => {
                (manifest.ui_text("recorder.transcript.complete", &[]), None)
            }
            TranscriptionState::Unavailable => (
                manifest.ui_text("recorder.transcript.unavailable", &[]),
                core.transcription_progress.reason.clone(),
            ),
        };
    }
    match input.owner.as_ref().map(|o| o.phase) {
        Some(CapturePhase::Starting | CapturePhase::Recording | CapturePhase::Stopping) => {
            (manifest.ui_text("recorder.transcript.preparing", &[]), None)
        }
        _ => (
            manifest.ui_text("recorder.transcript.empty_idle", &[]),
            None,
        ),
    }
}

fn actions(
    manifest: &Manifest,
    input: &RecorderViewInput,
    mode: Mode,
) -> (
    Vec<RecorderButton>,
    Vec<RecorderButton>,
    String,
    EscAction,
    String,
) {
    let mut buttons = Vec::new();
    let mut disabled = Vec::new();
    let (focus, esc, hint) = match mode {
        Mode::Starting | Mode::Recording { .. } => {
            buttons.push(button(manifest, "recorder.minimize", true, None));
            buttons.push(button(manifest, "recorder.stop", true, None));
            (
                "recorder.minimize",
                EscAction::Minimize,
                manifest.ui_text("recorder.hint.capturing", &[]),
            )
        }
        Mode::Stopping { .. } => {
            let reason = manifest.ui_text("recorder.header.saving", &[]);
            buttons.push(button(manifest, "recorder.minimize", true, None));
            disabled.push(button(manifest, "recorder.stop", false, Some(reason)));
            (
                "recorder.minimize",
                EscAction::Minimize,
                manifest.ui_text("recorder.hint.capturing", &[]),
            )
        }
        Mode::Elsewhere { .. } | Mode::Interrupted { recoverable: false } | Mode::Failed => {
            buttons.push(button(manifest, "recorder.copy_transcript", true, None));
            buttons.push(button(manifest, "recorder.recent", true, None));
            (
                "recorder.copy_transcript",
                EscAction::Close,
                manifest.ui_text("recorder.hint.idle", &[]),
            )
        }
        Mode::Interrupted { recoverable: true } => {
            buttons.push(button(manifest, "recorder.recover", true, None));
            (
                "recorder.recover",
                EscAction::Close,
                manifest.ui_text("recorder.hint.idle", &[]),
            )
        }
        Mode::Finishing | Mode::Stopped => {
            let core_active = input.core.as_ref().is_some_and(|core| {
                matches!(
                    core.capture_state,
                    CapturePhase::Starting | CapturePhase::Recording | CapturePhase::Stopping
                )
            });
            if core_active {
                buttons.push(button(manifest, "recorder.copy_transcript", true, None));
                buttons.push(button(manifest, "recorder.recent", true, None));
                return (
                    buttons,
                    disabled,
                    "recorder.copy_transcript".to_owned(),
                    EscAction::Close,
                    manifest.ui_text("recorder.hint.idle", &[]),
                );
            }
            buttons.push(button(manifest, "recorder.copy_transcript", true, None));
            let retry = input.core.as_ref().is_some_and(|core| {
                core.transcription_state == TranscriptionState::Failed
                    || core.transcription_progress.failed > 0
            });
            if retry {
                buttons.push(button(manifest, "recorder.retry", true, None));
            }
            buttons.push(button(manifest, "recorder.recent", true, None));
            buttons.push(button(manifest, "recorder.start", true, None));
            (
                "recorder.copy_transcript",
                EscAction::Close,
                manifest.ui_text("recorder.hint.idle", &[]),
            )
        }
        Mode::LocalFailed => {
            buttons.push(button(manifest, "recorder.start", true, None));
            buttons.push(button(manifest, "recorder.recent", true, None));
            (
                "recorder.start",
                EscAction::Close,
                manifest.ui_text("recorder.hint.idle", &[]),
            )
        }
        Mode::Idle | Mode::FailedBeforeCreate => {
            buttons.push(button(manifest, "recorder.start", true, None));
            buttons.push(button(manifest, "recorder.recent", true, None));
            (
                "recorder.start",
                EscAction::Close,
                manifest.ui_text("recorder.hint.idle", &[]),
            )
        }
    };
    (buttons, disabled, focus.to_owned(), esc, hint)
}

fn button(manifest: &Manifest, id: &str, enabled: bool, reason: Option<String>) -> RecorderButton {
    let label = match id {
        "recorder.start" => manifest.ui_text("recorder.action.start", &[]),
        "recorder.minimize" => manifest.ui_text("recorder.action.minimize", &[]),
        "recorder.stop" => manifest.ui_text("recorder.action.stop", &[]),
        "recorder.copy_transcript" => manifest.ui_text("recorder.action.copy_transcript", &[]),
        "recorder.retry" => manifest.ui_text("recorder.action.retry", &[]),
        "recorder.recover" => manifest.ui_text("recorder.action.recover", &[]),
        "recorder.recent" => manifest.ui_text("recorder.action.recent", &[]),
        "recorder.delete" => manifest.ui_text("recorder.action.delete", &[]),
        other => other.to_owned(),
    };
    RecorderButton {
        id: id.to_owned(),
        label,
        enabled,
        reason,
    }
}

fn recent_view(manifest: &Manifest, input: &RecorderViewInput) -> RecorderView {
    let limit = input
        .recent
        .as_ref()
        .map(|r| r.limit.max(1))
        .unwrap_or(RECENT_LIMIT);
    let rows: Vec<String> = input
        .recent
        .as_ref()
        .map(|recent| {
            recent
                .recordings
                .iter()
                .map(|recording| recent_row(manifest, recording))
                .collect()
        })
        .unwrap_or_default();
    let empty = rows
        .is_empty()
        .then(|| manifest.ui_text("recorder.recent.empty", &[]));
    let notice = input
        .recent
        .as_ref()
        .filter(|recent| recent.recordings.len() >= limit)
        .map(|_| manifest.ui_text("recorder.recent.limit", &[]));
    let mut buttons = vec![button(manifest, "recorder.start", true, None)];
    if !rows.is_empty() {
        buttons.push(button(manifest, "recorder.delete", true, None));
    }
    let title = manifest.ui_text("recorder.title", &[]);
    RecorderView {
        header: title.clone(),
        header_wide: title.clone(),
        header_compact: title,
        header_tone: Tone::Secondary,
        a11y: manifest.ui_text("recorder.a11y.idle", &[]),
        source: None,
        capture: None,
        audio: None,
        audio_detail: None,
        audio_warning: None,
        transcript_status: None,
        transcript_detail: None,
        buttons,
        disabled: Vec::new(),
        focus: if rows.is_empty() {
            "recorder.start".to_owned()
        } else {
            "recent.0".to_owned()
        },
        esc: EscAction::Back,
        hint: manifest.ui_text("recorder.hint.recent", &[]),
        recent: Some(RecentView {
            empty,
            notice,
            rows,
        }),
        rows: Vec::new(),
        level: None,
        follow: input.view.transcript_follow,
        jump_live: false,
    }
}

fn recent_row(manifest: &Manifest, recording: &RecentRecording) -> String {
    let date = recent_date(&recording.created_at);
    let samples = recording
        .final_sample_count
        .unwrap_or(recording.durable_offset);
    let t = format_elapsed(samples);
    let mut status = match recording.capture_state {
        CapturePhase::Starting | CapturePhase::Recording | CapturePhase::Stopping => {
            manifest.ui_text("recorder.status.recording", &[])
        }
        CapturePhase::Interrupted | CapturePhase::Failed => {
            manifest.ui_text("recorder.status.interrupted", &[])
        }
        CapturePhase::Stopped | CapturePhase::Idle => {
            if recording.active {
                manifest.ui_text("recorder.status.recording", &[])
            } else if recording.transcription_state == TranscriptionState::Complete {
                manifest.ui_text("recorder.status.complete", &[])
            } else {
                manifest.ui_text("recorder.status.transcript_incomplete", &[])
            }
        }
    };
    if recording.holes > 0 {
        let n = recording.holes.to_string();
        let gaps = manifest.ui_text("recorder.recent.gaps", &[("n", n.as_str())]);
        status.push_str(" · ");
        status.push_str(&gaps);
    }
    manifest.ui_text(
        "recorder.recent.row",
        &[
            ("date", date.as_str()),
            ("t", t.as_str()),
            ("status", status.as_str()),
        ],
    )
}

fn recent_date(created_at: &str) -> String {
    if created_at.len() >= 16 && created_at.is_ascii() {
        let hour = &created_at[11..13];
        let minute = &created_at[14..16];
        if let (Ok(_year), Ok(month), Ok(day)) = (
            created_at[0..4].parse::<u32>(),
            created_at[5..7].parse::<u32>(),
            created_at[8..10].parse::<u32>(),
        ) {
            if (1..=12).contains(&month)
                && (1..=31).contains(&day)
                && &created_at[4..5] == "-"
                && &created_at[7..8] == "-"
                && hour.bytes().all(|b| b.is_ascii_digit())
                && minute.bytes().all(|b| b.is_ascii_digit())
            {
                const MONTHS: [&str; 12] = [
                    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                    "Dec",
                ];
                return format!("{day} {} {hour}:{minute}", MONTHS[(month - 1) as usize]);
            }
        }
    }
    created_at.to_owned()
}

fn stopped_samples(input: &RecorderViewInput) -> u64 {
    input
        .core
        .as_ref()
        .map(|c| c.final_sample_count.unwrap_or(c.durable_offset))
        .or_else(|| input.owner.as_ref().map(|o| o.captured_samples))
        .unwrap_or(0)
}

fn interrupted_samples(input: &RecorderViewInput) -> u64 {
    input
        .owner
        .as_ref()
        .map(|o| o.captured_samples)
        .or_else(|| input.core.as_ref().map(|c| c.durable_offset))
        .unwrap_or(0)
}

fn interrupted_reason(manifest: &Manifest, input: &RecorderViewInput) -> String {
    input
        .owner
        .as_ref()
        .and_then(|o| o.reason.as_deref())
        .filter(|reason| !reason.is_empty())
        .map(str::to_owned)
        .or_else(|| {
            input
                .core
                .as_ref()
                .and_then(|c| c.reason.as_deref())
                .map(|code| core_reason_text(manifest, code))
        })
        .unwrap_or_default()
}

/// Without a reason the row ends at the time; never leave the interrupted
/// template's colon hanging.
fn interrupted_capture_text(manifest: &Manifest, t: &str, reason: &str) -> String {
    if reason.is_empty() {
        manifest.ui_text("recorder.capture.interrupted_no_reason", &[("t", t)])
    } else {
        manifest.ui_text(
            "recorder.capture.interrupted",
            &[("t", t), ("reason", reason)],
        )
    }
}

fn failed_reason(manifest: &Manifest, input: &RecorderViewInput) -> String {
    input
        .owner
        .as_ref()
        .and_then(|o| o.reason.as_deref())
        .filter(|reason| !reason.is_empty())
        .map(str::to_owned)
        .or_else(|| {
            input
                .core
                .as_ref()
                .and_then(|c| c.reason.as_deref())
                .map(|code| core_reason_text(manifest, code))
        })
        .unwrap_or_default()
}

fn before_create_reason(input: &RecorderViewInput) -> String {
    let Some(failure) = input.failure.as_ref() else {
        return String::new();
    };
    if let Some(reason) = failure
        .reason
        .as_deref()
        .filter(|reason| !reason.is_empty())
    {
        return reason.to_owned();
    }
    failure.kind.replace('_', " ")
}

/// Core reports short reason codes; the UI shows the shared words. Owner-side
/// reasons are already display text and take precedence at the call site.
fn core_reason_text(manifest: &Manifest, code: &str) -> String {
    match code {
        "stream_detached" => manifest.ui_text("recorder.reason.stream_detached", &[]),
        "lease_expired" => manifest.ui_text("recorder.reason.lease_expired", &[]),
        "microphone" | "capture_failed" => manifest.ui_text("recorder.reason.microphone", &[]),
        "recorder_deleted" => manifest.ui_text("recorder.reason.deleted", &[]),
        other => other.replace('_', " "),
    }
}

/// The durable transcript page plus at most one provisional tail.
///
/// Durable segments are keyed by segment id and keep the greatest revision. A
/// provisional tail is live-only and is replaced by a later provisional or a
/// final for the same segment, and discarded when a page is re-read after a
/// reconnect.
#[derive(Debug, Default, Clone)]
pub struct TranscriptStore {
    recording_id: Option<String>,
    generation: Option<u64>,
    segments: BTreeMap<u32, SegmentInput>,
    provisional: Option<ProvisionalInput>,
    dropped: bool,
}

impl TranscriptStore {
    pub fn from_page(page: &TranscriptInput) -> Self {
        let mut store = Self::default();
        store.replace_page(page);
        store
    }

    /// A reconnect treats the paged transcript as authoritative for its
    /// provisional row; durable segments merge by greatest revision and any
    /// previously evicted text stays flagged as `Earlier`.
    pub fn replace_page(&mut self, page: &TranscriptInput) {
        if page.recording_id.is_some() {
            self.recording_id = page.recording_id.clone();
        }
        if page.generation.is_some() {
            self.generation = page.generation;
        }
        for segment in &page.segments {
            self.apply(segment);
        }
        self.provisional = page.provisional.clone();
        if page.dropped > 0 {
            self.dropped = true;
        }
    }

    /// Apply one live segment, ignoring another recording or generation first.
    pub fn apply_event(
        &mut self,
        recording_id: Option<&str>,
        generation: Option<u64>,
        segment: &SegmentInput,
    ) {
        if self
            .recording_id
            .as_deref()
            .zip(recording_id)
            .is_some_and(|(current, next)| current != next)
        {
            return;
        }
        if self
            .generation
            .zip(generation)
            .is_some_and(|(current, next)| current != next)
        {
            return;
        }
        self.apply(segment);
    }

    /// Apply one durable segment or live provisional tail. Older revisions are
    /// ignored, including a stale final after a newer one.
    pub fn apply(&mut self, segment: &SegmentInput) {
        if segment.status == SegmentStatus::Provisional {
            if self.provisional.as_ref().is_some_and(|tail| {
                tail.segment_id == segment.segment_id && tail.revision >= segment.revision
            }) || self
                .segments
                .get(&segment.segment_id)
                .is_some_and(|durable| durable.revision >= segment.revision)
                || self
                    .segments
                    .keys()
                    .next_back()
                    .is_some_and(|last| segment.segment_id < *last)
            {
                return;
            }
            self.provisional = Some(ProvisionalInput {
                segment_id: segment.segment_id,
                revision: segment.revision,
                start: segment.start,
                text: segment.text.clone().unwrap_or_default(),
            });
            return;
        }
        if self
            .segments
            .get(&segment.segment_id)
            .is_some_and(|current| current.revision >= segment.revision)
        {
            return;
        }
        self.segments.insert(segment.segment_id, segment.clone());
        if self
            .provisional
            .as_ref()
            .is_some_and(|tail| tail.segment_id <= segment.segment_id)
        {
            self.provisional = None;
        }
        while self.segments.len() > MAX_TRANSCRIPT_SEGMENTS {
            let Some(oldest) = self.segments.keys().next().copied() else {
                break;
            };
            self.segments.remove(&oldest);
            self.dropped = true;
        }
    }

    pub fn len(&self) -> usize {
        self.segments.len()
    }

    pub fn is_empty(&self) -> bool {
        self.segments.is_empty()
    }

    /// Whether the retention cap dropped older durable text.
    pub fn dropped_earlier(&self) -> bool {
        self.dropped
    }

    pub fn provisional(&self) -> Option<&ProvisionalInput> {
        self.provisional.as_ref()
    }

    /// Render the durable rows in order with one provisional tail. Skipped
    /// segments are not drawn; pending and error parts show their shared label.
    pub fn rows(&self, manifest: &Manifest) -> Vec<TranscriptRow> {
        let mut rows = Vec::new();
        if self.dropped {
            rows.push(TranscriptRow {
                kind: TranscriptRowKind::Earlier,
                start: None,
                end: None,
                label: manifest.ui_text("recorder.transcript.earlier", &[]),
            });
        }
        for segment in self.segments.values() {
            let (kind, text) = match segment.status {
                SegmentStatus::Skipped | SegmentStatus::Provisional => continue,
                SegmentStatus::Pending => (
                    TranscriptRowKind::Pending,
                    manifest.ui_text("recorder.transcript.pending_row", &[]),
                ),
                SegmentStatus::Error => (
                    TranscriptRowKind::Error,
                    manifest.ui_text("recorder.transcript.error_row", &[]),
                ),
                SegmentStatus::Final => (
                    TranscriptRowKind::Final,
                    segment.text.clone().unwrap_or_default(),
                ),
            };
            rows.push(TranscriptRow {
                kind,
                start: Some(segment.start),
                end: segment.end,
                label: range_label(segment.start, segment.end, &text),
            });
        }
        if let Some(tail) = self.provisional.as_ref() {
            let ellipsis = if tail.text.ends_with('…') {
                ""
            } else {
                "…"
            };
            rows.push(TranscriptRow {
                kind: TranscriptRowKind::Provisional,
                start: Some(tail.start),
                end: None,
                label: format!("{}–  {}{}", format_elapsed(tail.start), tail.text, ellipsis),
            });
        }
        rows
    }
}

fn range_label(start: u64, end: Option<u64>, text: &str) -> String {
    match end {
        Some(end) => format!(
            "{}–{}  {}",
            format_elapsed(start),
            format_elapsed(end),
            text
        ),
        None => format!("{}–  {}", format_elapsed(start), text),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn manifest() -> Manifest {
        Manifest::load()
    }

    fn scenarios() -> Value {
        let file = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../interface/scenarios/recorder-view.json"
        ))
        .expect("shared recorder-view scenario");
        serde_json::from_str(&file).expect("valid scenario JSON")
    }

    fn tone_name(tone: Tone) -> &'static str {
        match tone {
            Tone::Secondary => "secondary",
            Tone::Error => "error",
            Tone::Warning => "warning",
        }
    }

    fn esc_name(esc: EscAction) -> &'static str {
        match esc {
            EscAction::Close => "close",
            EscAction::Minimize => "minimize",
            EscAction::Back => "back",
        }
    }

    fn expected_text(expect: &Value, key: &str) -> Option<String> {
        match expect.get(key) {
            None | Some(Value::Null) => None,
            Some(value) => Some(value.as_str().unwrap_or_default().to_owned()),
        }
    }

    #[test]
    fn every_shared_recorder_view_case_matches() {
        let manifest = manifest();
        let scenarios = scenarios();
        let cases = scenarios["cases"].as_array().expect("cases");
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().expect("case name");
            let input: RecorderViewInput =
                serde_json::from_value(case["input"].clone()).expect("valid input");
            let expect = &case["expect"];
            let view = recorder_view(&manifest, &input);

            assert_eq!(
                view.header,
                expect["header"].as_str().unwrap(),
                "{name}: header"
            );
            assert_eq!(
                tone_name(view.header_tone),
                expect["header_tone"].as_str().unwrap(),
                "{name}: header_tone"
            );
            assert_eq!(view.a11y, expect["a11y"].as_str().unwrap(), "{name}: a11y");
            for field in [
                "capture",
                "audio",
                "audio_detail",
                "audio_warning",
                "transcript_status",
            ] {
                let got = match field {
                    "capture" => view.capture.clone(),
                    "audio" => view.audio.clone(),
                    "audio_detail" => view.audio_detail.clone(),
                    "audio_warning" => view.audio_warning.clone(),
                    "transcript_status" => view.transcript_status.clone(),
                    _ => unreachable!(),
                };
                assert_eq!(got, expected_text(expect, field), "{name}: {field}");
            }

            let want_buttons: Vec<&str> = expect["buttons"]
                .as_array()
                .unwrap()
                .iter()
                .map(|button| button.as_str().unwrap())
                .collect();
            let got_buttons: Vec<&str> = view
                .buttons
                .iter()
                .map(|button| button.id.as_str())
                .collect();
            assert_eq!(got_buttons, want_buttons, "{name}: buttons");
            for button in &view.buttons {
                assert!(!button.label.is_empty(), "{name}: empty label");
                assert_ne!(button.label, button.id, "{name}: label is the id");
            }

            let mut want_disabled = BTreeMap::new();
            for (id, reason) in expect["disabled"].as_object().unwrap() {
                want_disabled.insert(id.clone(), reason.as_str().unwrap().to_owned());
            }
            let got_disabled: BTreeMap<String, String> = view
                .disabled
                .iter()
                .map(|button| (button.id.clone(), button.reason.clone().unwrap_or_default()))
                .collect();
            assert_eq!(got_disabled, want_disabled, "{name}: disabled");

            assert_eq!(
                view.focus,
                expect["focus"].as_str().unwrap(),
                "{name}: focus"
            );
            assert_eq!(
                esc_name(view.esc),
                expect["esc"].as_str().unwrap(),
                "{name}: esc"
            );
            assert_eq!(view.hint, expect["hint"].as_str().unwrap(), "{name}: hint");

            if let Some(recent) = expect.get("recent") {
                let got = view.recent.as_ref().expect("recent view");
                assert_eq!(
                    got.empty,
                    expected_text(recent, "empty"),
                    "{name}: recent.empty"
                );
                assert_eq!(
                    got.notice,
                    expected_text(recent, "notice"),
                    "{name}: recent.notice"
                );
                let want_rows: Vec<&str> = recent["rows"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|row| row.as_str().unwrap())
                    .collect();
                assert!(
                    got.rows
                        .starts_with(&want_rows.iter().map(|s| s.to_string()).collect::<Vec<_>>()),
                    "{name}: recent rows {:?} do not start with {want_rows:?}",
                    got.rows
                );
            } else {
                assert!(view.recent.is_none(), "{name}: unexpected recent view");
            }
        }
    }

    #[test]
    fn elapsed_uses_samples_and_switches_at_one_hour() {
        assert_eq!(format_elapsed(0), "00:00");
        assert_eq!(format_elapsed(112_000), "00:07");
        assert_eq!(format_elapsed(12_064_000), "12:34");
        assert_eq!(format_elapsed(57_792_000), "1:00:12");
        assert_eq!(format_elapsed(59_568_000), "1:02:03");
    }

    #[test]
    fn transcript_page_renders_range_and_one_provisional_tail() {
        let manifest = manifest();
        let page = TranscriptInput {
            recording_id: None,
            generation: None,
            segments: vec![
                SegmentInput {
                    segment_id: 0,
                    revision: 2,
                    start: 0,
                    end: Some(112_000),
                    status: SegmentStatus::Final,
                    text: Some("Hello there.".into()),
                },
                SegmentInput {
                    segment_id: 1,
                    revision: 1,
                    start: 112_000,
                    end: Some(240_000),
                    status: SegmentStatus::Pending,
                    text: None,
                },
                SegmentInput {
                    segment_id: 2,
                    revision: 1,
                    start: 240_000,
                    end: Some(260_000),
                    status: SegmentStatus::Skipped,
                    text: None,
                },
            ],
            provisional: Some(ProvisionalInput {
                segment_id: 3,
                revision: 1,
                start: 260_000,
                text: "and this is".into(),
            }),
            dropped: 0,
        };
        let store = TranscriptStore::from_page(&page);
        let rows = store.rows(&manifest);
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].label, "00:00–00:07  Hello there.");
        assert_eq!(rows[0].kind, TranscriptRowKind::Final);
        assert_eq!(rows[1].kind, TranscriptRowKind::Pending);
        assert_eq!(
            rows[1].label,
            format!(
                "00:07–00:15  {}",
                manifest.ui_text("recorder.transcript.pending_row", &[])
            )
        );
        assert_eq!(rows[2].kind, TranscriptRowKind::Provisional);
        assert_eq!(rows[2].label, "00:16–  and this is…");
    }

    #[test]
    fn transcript_reducer_keeps_greatest_revision_and_replaces_provisional() {
        let manifest = manifest();
        let mut store = TranscriptStore::default();
        store.apply(&SegmentInput {
            segment_id: 0,
            revision: 1,
            start: 0,
            end: None,
            status: SegmentStatus::Provisional,
            text: Some("Hello there".into()),
        });
        assert_eq!(store.rows(&manifest)[0].label, "00:00–  Hello there…");

        store.apply(&SegmentInput {
            segment_id: 0,
            revision: 1,
            start: 0,
            end: None,
            status: SegmentStatus::Final,
            text: Some("stale".into()),
        });
        // A final at the provisional's revision replaces the tail.
        assert!(store.provisional().is_none());
        assert_eq!(store.rows(&manifest)[0].label, "00:00–  stale");

        store.apply(&SegmentInput {
            segment_id: 0,
            revision: 2,
            start: 0,
            end: Some(112_000),
            status: SegmentStatus::Final,
            text: Some("Hello there.".into()),
        });
        assert_eq!(store.rows(&manifest)[0].label, "00:00–00:07  Hello there.");

        // Older durable revisions are ignored.
        store.apply(&SegmentInput {
            segment_id: 0,
            revision: 1,
            start: 0,
            end: Some(112_000),
            status: SegmentStatus::Final,
            text: Some("older".into()),
        });
        assert_eq!(store.rows(&manifest)[0].label, "00:00–00:07  Hello there.");

        // A newer provisional takes the one tail slot.
        store.apply(&SegmentInput {
            segment_id: 1,
            revision: 1,
            start: 112_000,
            end: None,
            status: SegmentStatus::Provisional,
            text: Some("and this".into()),
        });
        store.apply(&SegmentInput {
            segment_id: 2,
            revision: 1,
            start: 112_000,
            end: None,
            status: SegmentStatus::Provisional,
            text: Some("and this is".into()),
        });
        assert_eq!(store.rows(&manifest).len(), 2);
        assert_eq!(store.rows(&manifest)[1].label, "00:07–  and this is…");
    }

    #[test]
    fn transcript_reconnect_replaces_the_provisional_row() {
        let manifest = manifest();
        let mut store = TranscriptStore::from_page(&TranscriptInput {
            recording_id: None,
            generation: None,
            segments: Vec::new(),
            provisional: Some(ProvisionalInput {
                segment_id: 0,
                revision: 1,
                start: 0,
                text: "live text".into(),
            }),
            dropped: 0,
        });
        assert_eq!(store.rows(&manifest).len(), 1);

        store.replace_page(&TranscriptInput {
            recording_id: None,
            generation: None,
            segments: vec![SegmentInput {
                segment_id: 0,
                revision: 3,
                start: 0,
                end: Some(112_000),
                status: SegmentStatus::Final,
                text: Some("Durable text.".into()),
            }],
            provisional: None,
            dropped: 0,
        });
        assert!(store.provisional().is_none());
        let rows = store.rows(&manifest);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].label, "00:00–00:07  Durable text.");
    }

    #[test]
    fn transcript_cap_keeps_the_newest_five_thousand_and_flags_earlier_text() {
        let manifest = manifest();
        let mut store = TranscriptStore::default();
        for id in 0..=MAX_TRANSCRIPT_SEGMENTS as u32 {
            store.apply(&SegmentInput {
                segment_id: id,
                revision: 1,
                start: u64::from(id) * 16_000,
                end: Some(u64::from(id + 1) * 16_000),
                status: SegmentStatus::Final,
                text: Some(format!("part {id}")),
            });
        }
        assert_eq!(store.len(), MAX_TRANSCRIPT_SEGMENTS);
        assert!(store.dropped_earlier());
        let rows = store.rows(&manifest);
        assert_eq!(rows.len(), MAX_TRANSCRIPT_SEGMENTS + 1);
        assert_eq!(rows[0].kind, TranscriptRowKind::Earlier);
        assert_eq!(
            rows[0].label,
            manifest.ui_text("recorder.transcript.earlier", &[])
        );
        assert_eq!(rows[1].start, Some(16_000));
    }

    #[test]
    fn source_line_uses_shared_device_and_destination_strings() {
        let manifest = manifest();
        let mut input: RecorderViewInput = serde_json::from_value(serde_json::json!({
            "owner": null,
            "core": null,
            "transcript": null,
            "view": {
                "modal_open": true,
                "page": "recorder",
                "transcript_follow": true,
                "seen_interrupted": false
            },
            "width": "wide"
        }))
        .unwrap();
        assert_eq!(source_line(&manifest, &input), None);

        input.source = Some(SourceInput {
            device: Some("MacBook Mic".into()),
            host: Some("this computer".into()),
        });
        assert_eq!(
            source_line(&manifest, &input).as_deref(),
            Some("Microphone: MacBook Mic · Saving to this computer")
        );

        input.source = Some(SourceInput {
            device: None,
            host: None,
        });
        assert_eq!(
            source_line(&manifest, &input).as_deref(),
            Some("Microphone: default input")
        );
    }

    #[test]
    fn unavailable_transcription_keeps_its_core_reason_as_a_second_line() {
        let manifest = manifest();
        let input: RecorderViewInput = serde_json::from_value(serde_json::json!({
            "owner": null,
            "core": {
                "id": "0123456789abcdef0123456789abcdef",
                "capture_state": "recording",
                "active": true,
                "durable_offset": 12064000,
                "final_sample_count": null,
                "complete": false,
                "holes": 0,
                "missing_samples": null,
                "transcription_state": "unavailable",
                "transcription_progress": {
                    "lag_samples": 0,
                    "pending_finals": 0,
                    "failed": 0,
                    "transcript_revision": null,
                    "reason": "model_not_cached"
                },
                "reason": "created"
            },
            "transcript": null,
            "view": {
                "modal_open": true,
                "page": "recorder",
                "transcript_follow": true,
                "seen_interrupted": false
            },
            "width": "wide"
        }))
        .unwrap();
        let view = recorder_view(&manifest, &input);
        assert_eq!(
            view.transcript_status.as_deref(),
            Some("Recording audio · Transcription unavailable")
        );
        assert_eq!(view.transcript_detail.as_deref(), Some("model_not_cached"));
    }

    #[test]
    fn transcript_events_for_another_recording_or_generation_are_ignored() {
        let manifest = manifest();
        let mut store = TranscriptStore::from_page(&TranscriptInput {
            recording_id: Some("a".into()),
            generation: Some(1),
            segments: Vec::new(),
            provisional: None,
            dropped: 0,
        });
        let segment = SegmentInput {
            segment_id: 0,
            revision: 1,
            start: 0,
            end: Some(16_000),
            status: SegmentStatus::Final,
            text: Some("kept".into()),
        };
        store.apply_event(Some("b"), Some(1), &segment);
        store.apply_event(Some("a"), Some(2), &segment);
        assert!(store.is_empty());
        store.apply_event(Some("a"), Some(1), &segment);
        assert_eq!(store.rows(&manifest)[0].label, "00:00–00:01  kept");
    }

    #[test]
    fn owner_reason_falls_back_to_shared_core_reason_keys() {
        let manifest = manifest();
        let mut input: RecorderViewInput = serde_json::from_value(serde_json::json!({
            "owner": {
                "phase": "interrupted",
                "recording_id": "0123456789abcdef0123456789abcdef",
                "captured_samples": 12032000,
                "pending_samples": 192000,
                "spool": "persistent",
                "recoverable": true,
                "reason": null,
                "level": 0.0
            },
            "core": {
                "id": "0123456789abcdef0123456789abcdef",
                "capture_state": "interrupted",
                "active": false,
                "durable_offset": 11840000,
                "final_sample_count": null,
                "complete": false,
                "holes": 0,
                "missing_samples": null,
                "transcription_state": "live",
                "transcription_progress": {
                    "lag_samples": 0,
                    "pending_finals": 0,
                    "failed": 0,
                    "transcript_revision": 3
                },
                "reason": "lease_expired"
            },
            "transcript": null,
            "view": {
                "modal_open": true,
                "page": "recorder",
                "transcript_follow": true,
                "seen_interrupted": false
            },
            "width": "wide"
        }))
        .unwrap();
        let reason = manifest.ui_text("recorder.reason.lease_expired", &[]);
        let capture = manifest.ui_text(
            "recorder.capture.interrupted",
            &[("t", "12:32"), ("reason", reason.as_str())],
        );
        let detail = manifest.ui_text("recorder.audio.saved_detail", &[("t", "12:20")]);
        let view = recorder_view(&manifest, &input);
        assert_eq!(view.capture.as_deref(), Some(capture.as_str()));
        assert_eq!(view.audio_detail.as_deref(), Some(detail.as_str()));

        input.owner.as_mut().unwrap().reason = Some("Owner's own words".into());
        assert_eq!(
            recorder_view(&manifest, &input).capture.as_deref(),
            Some("Interrupted at 12:32: Owner's own words")
        );

        input.owner = None;
        input.core.as_mut().unwrap().reason = Some("future_problem".into());
        assert_eq!(
            recorder_view(&manifest, &input).capture.as_deref(),
            Some("Interrupted at 12:20: future problem")
        );
    }

    #[test]
    fn missing_and_pre_create_reasons_use_the_nsh_fallback() {
        let manifest = manifest();
        let mut input: RecorderViewInput = serde_json::from_value(serde_json::json!({
            "owner": null,
            "core": null,
            "transcript": null,
            "failure": { "kind": "mic_busy", "reason": null },
            "view": {
                "modal_open": true,
                "page": "recorder",
                "transcript_follow": true,
                "seen_interrupted": false
            },
            "width": "wide"
        }))
        .unwrap();
        assert_eq!(before_create_reason(&input), "mic busy");
        assert_eq!(
            recorder_view(&manifest, &input).capture.as_deref(),
            Some("Failed: mic busy")
        );

        input.failure = None;
        assert_eq!(before_create_reason(&input), "");
    }

    #[test]
    fn interrupted_without_a_reason_omits_the_hanging_colon() {
        let manifest = manifest();
        let expected =
            manifest.ui_text("recorder.capture.interrupted_no_reason", &[("t", "00:12")]);
        assert_ne!(expected, "recorder.capture.interrupted_no_reason");

        let mut input: RecorderViewInput = serde_json::from_value(serde_json::json!({
            "owner": {
                "phase": "interrupted",
                "recording_id": "0123456789abcdef0123456789abcdef",
                "captured_samples": 192000,
                "pending_samples": 0,
                "spool": "persistent",
                "recoverable": false,
                "reason": null,
                "level": 0.0
            },
            "core": null,
            "transcript": null,
            "view": {
                "modal_open": true,
                "page": "recorder",
                "transcript_follow": true,
                "seen_interrupted": false
            },
            "width": "wide"
        }))
        .unwrap();
        let owner = recorder_view(&manifest, &input);
        assert_eq!(owner.capture.as_deref(), Some(expected.as_str()));
        assert!(!owner.capture.as_deref().unwrap().trim_end().ends_with(':'));

        input.owner = None;
        input.core = Some(
            serde_json::from_value(serde_json::json!({
                "id": "0123456789abcdef0123456789abcdef",
                "capture_state": "interrupted",
                "active": false,
                "durable_offset": 192000,
                "final_sample_count": null,
                "complete": false,
                "holes": 0,
                "missing_samples": null,
                "transcription_state": "live",
                "transcription_progress": {
                    "lag_samples": 0,
                    "pending_finals": 0,
                    "failed": 0,
                    "transcript_revision": 3
                },
                "reason": null
            }))
            .unwrap(),
        );
        let viewer = recorder_view(&manifest, &input);
        assert_eq!(viewer.capture.as_deref(), Some(expected.as_str()));
        assert!(!viewer.capture.as_deref().unwrap().trim_end().ends_with(':'));

        input.core.as_mut().unwrap().reason = Some("stream_detached".into());
        let reason = manifest.ui_text("recorder.reason.stream_detached", &[]);
        let with_reason = manifest.ui_text(
            "recorder.capture.interrupted",
            &[("t", "00:12"), ("reason", reason.as_str())],
        );
        assert_eq!(
            recorder_view(&manifest, &input).capture.as_deref(),
            Some(with_reason.as_str())
        );
    }
}
