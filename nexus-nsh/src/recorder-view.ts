// Pure recorder view-model for NSH (plan 54, V-NSH).
//
// This module knows no UI, store, network or microphone. It maps a local
// capture snapshot, Core's public recording state, a transcript snapshot and
// the client's view flags to the exact header, status rows, actions, focus and
// Esc outcome in `interface/scenarios/recorder-view.json`. The TUI view-model
// maps the same fixture for its own controller shape.

import { manifest, uiText } from "./gen/manifest";

export const RECORDER_SAMPLE_RATE = 16_000;
const MAX_TRANSCRIPT_SEGMENTS = 5_000;

export type RecorderPhase = "idle" | "starting" | "recording" | "stopping" | "stopped" | "interrupted" | "failed";
export type RecorderCaptureState = "idle" | "starting" | "recording" | "stopping" | "stopped" | "interrupted" | "failed";
export type RecorderSpool = "persistent" | "best-effort" | "memory" | "none";
export type RecorderTone = "secondary" | "error" | "warning";
export type RecorderEsc = "minimize" | "close" | "back";
export type RecorderWidth = "wide" | "compact";
export type RecorderPage = "recorder" | "recent";

export type RecorderButtonId =
  | "recorder.start"
  | "recorder.minimize"
  | "recorder.stop"
  | "recorder.copy_transcript"
  | "recorder.retry"
  | "recorder.recover"
  | "recorder.recent"
  | "recorder.delete";

export type RecorderTextKey =
  | "recorder.title"
  | "recorder.header.idle"
  | "recorder.header.starting"
  | "recorder.header.starting_compact"
  | "recorder.header.recording"
  | "recorder.header.recording_compact"
  | "recorder.header.elsewhere"
  | "recorder.header.elsewhere_compact"
  | "recorder.header.saving"
  | "recorder.header.saving_compact"
  | "recorder.header.finishing"
  | "recorder.header.finishing_compact"
  | "recorder.header.interrupted"
  | "recorder.header.interrupted_compact"
  | "recorder.a11y.idle"
  | "recorder.a11y.starting"
  | "recorder.a11y.recording"
  | "recorder.a11y.elsewhere"
  | "recorder.a11y.saving"
  | "recorder.a11y.finishing"
  | "recorder.a11y.interrupted"
  | "recorder.source.device"
  | "recorder.source.default_device"
  | "recorder.source.destination"
  | "recorder.capture.idle"
  | "recorder.capture.starting"
  | "recorder.capture.recording"
  | "recorder.capture.elsewhere"
  | "recorder.capture.stopping"
  | "recorder.capture.stopped"
  | "recorder.capture.interrupted"
  | "recorder.capture.interrupted_no_reason"
  | "recorder.capture.failed"
  | "recorder.reason.stream_detached"
  | "recorder.reason.lease_expired"
  | "recorder.reason.microphone"
  | "recorder.reason.deleted"
  | "recorder.audio.saved_through"
  | "recorder.audio.saved_detail"
  | "recorder.audio.all_saved"
  | "recorder.audio.saved_finishing"
  | "recorder.audio.memory_only"
  | "recorder.audio.gaps"
  | "recorder.audio.gaps_unknown"
  | "recorder.audio.unsaved"
  | "recorder.transcript.preparing"
  | "recorder.transcript.live"
  | "recorder.transcript.catching_up"
  | "recorder.transcript.complete"
  | "recorder.transcript.unavailable"
  | "recorder.transcript.failed"
  | "recorder.transcript.empty_idle"
  | "recorder.transcript.pending_row"
  | "recorder.transcript.error_row"
  | "recorder.transcript.earlier"
  | "recorder.jump_live"
  | "recorder.action.start"
  | "recorder.action.minimize"
  | "recorder.action.stop"
  | "recorder.action.copy_transcript"
  | "recorder.action.retry"
  | "recorder.action.recover"
  | "recorder.action.recent"
  | "recorder.action.delete"
  | "recorder.recent.empty"
  | "recorder.recent.row"
  | "recorder.recent.limit"
  | "recorder.recent.gaps"
  | "recorder.status.complete"
  | "recorder.status.recording"
  | "recorder.status.interrupted"
  | "recorder.status.transcript_incomplete"
  | "recorder.hint.capturing"
  | "recorder.hint.idle"
  | "recorder.hint.recent";

export type RecorderText = (key: RecorderTextKey, args?: Record<string, string | number>) => string;

/** The strings the view-model reads. `meta` exposes the missing-key dependency to tests. */
export const RECORDER_VIEW_STRINGS: readonly RecorderTextKey[] = [
  "recorder.title", "recorder.header.idle", "recorder.header.starting", "recorder.header.starting_compact",
  "recorder.header.recording", "recorder.header.recording_compact", "recorder.header.elsewhere",
  "recorder.header.elsewhere_compact", "recorder.header.saving", "recorder.header.saving_compact",
  "recorder.header.finishing", "recorder.header.finishing_compact", "recorder.header.interrupted",
  "recorder.header.interrupted_compact", "recorder.a11y.idle", "recorder.a11y.starting", "recorder.a11y.recording",
  "recorder.a11y.elsewhere", "recorder.a11y.saving", "recorder.a11y.finishing", "recorder.a11y.interrupted",
  "recorder.source.device", "recorder.source.default_device", "recorder.source.destination",
  "recorder.capture.idle", "recorder.capture.starting", "recorder.capture.recording", "recorder.capture.elsewhere",
  "recorder.capture.stopping", "recorder.capture.stopped", "recorder.capture.interrupted",
  "recorder.capture.interrupted_no_reason", "recorder.capture.failed",
  "recorder.reason.stream_detached", "recorder.reason.lease_expired", "recorder.reason.microphone",
  "recorder.reason.deleted", "recorder.audio.saved_through", "recorder.audio.saved_detail", "recorder.audio.all_saved",
  "recorder.audio.saved_finishing", "recorder.audio.memory_only", "recorder.audio.gaps",
  "recorder.audio.gaps_unknown", "recorder.audio.unsaved", "recorder.transcript.preparing",
  "recorder.transcript.live", "recorder.transcript.catching_up", "recorder.transcript.complete",
  "recorder.transcript.unavailable", "recorder.transcript.failed", "recorder.transcript.empty_idle",
  "recorder.transcript.pending_row", "recorder.transcript.error_row", "recorder.transcript.earlier",
  "recorder.jump_live", "recorder.action.start", "recorder.action.minimize", "recorder.action.stop",
  "recorder.action.copy_transcript", "recorder.action.retry", "recorder.action.recover",
  "recorder.action.recent", "recorder.action.delete", "recorder.recent.empty", "recorder.recent.row",
  "recorder.recent.limit", "recorder.recent.gaps", "recorder.status.complete", "recorder.status.recording",
  "recorder.status.interrupted", "recorder.status.transcript_incomplete", "recorder.hint.capturing",
  "recorder.hint.idle", "recorder.hint.recent",
];

const sharedStrings = manifest.strings as unknown as Record<string, string | undefined>;

/**
 * Shared strings come from the generated manifest. A missing key is a D0
 * dependency, not something this view-model may invent: report the key and
 * regenerate `src/gen/manifest.ts` instead of adding a literal here.
 */
export function recorderText(key: RecorderTextKey, args: Record<string, string | number> = {}): string {
  if (sharedStrings[key] === undefined) {
    throw new Error(`recorder-view: shared string "${key}" is not in the generated manifest; run the NSH manifest generation`);
  }
  return uiText(key as keyof typeof manifest.strings, args);
}

/** Keys the current generated manifest does not provide. Empty once D0 regenerates it. */
export function missingRecorderStrings(keys: readonly RecorderTextKey[] = RECORDER_VIEW_STRINGS): RecorderTextKey[] {
  return keys.filter((key) => sharedStrings[key] === undefined);
}

export interface RecorderOwnerSnapshot {
  phase: RecorderPhase;
  recording_id: string | null;
  captured_samples: number;
  pending_samples: number;
  spool: RecorderSpool;
  recoverable: boolean;
  reason: string | null;
  level: number;
}

export interface RecorderCoreInput {
  id: string;
  generation?: number | null;
  revision?: number | null;
  capture_state?: string | null;
  active?: boolean;
  durable_offset?: number | null;
  final_sample_count?: number | null;
  complete?: boolean;
  holes?: number | null;
  missing_samples?: number | null;
  owner?: { client_id?: string | null } | null;
  transcription_state?: string | null;
  transcription_progress?: {
    lag_samples?: number | null;
    pending_finals?: number | null;
    failed?: number | null;
    transcript_revision?: number | null;
    reason?: string | null;
  } | null;
  reason?: string | null;
}

export interface RecorderTranscriptSegmentInput {
  segment_id: number;
  revision?: number | null;
  start: number;
  end?: number | null;
  status?: string | null;
  text?: string | null;
}

export interface RecorderTranscriptInput {
  segments?: RecorderTranscriptSegmentInput[] | null;
  provisional?: RecorderTranscriptSegmentInput | null;
  dropped?: number;
  transcript_revision?: number | null;
}

export interface RecorderViewInput {
  owner: RecorderOwnerSnapshot | null;
  core: RecorderCoreInput | null;
  transcript: RecorderTranscriptInput | null;
  failure?: { kind?: string | null; reason?: string | null } | null;
  recent?: { recordings?: RecorderRecentRecording[]; limit?: number } | null;
  source?: { device?: string | null; host?: string | null } | null;
  view: {
    modal_open: boolean;
    page: RecorderPage;
    transcript_follow: boolean;
    seen_interrupted: boolean;
  };
  width: RecorderWidth;
}

export interface RecorderRecentRecording {
  id: string;
  capture_state?: string | null;
  created_at?: string | null;
  durable_offset?: number | null;
  final_sample_count?: number | null;
  transcription_state?: string | null;
  holes?: number | null;
  active?: boolean;
}

export interface RecorderButton {
  id: RecorderButtonId;
  label: string;
  enabled: boolean;
  reason: string | null;
}

export type RecorderTranscriptRowKind = "final" | "pending" | "error" | "provisional" | "earlier";

export interface RecorderTranscriptRow {
  kind: RecorderTranscriptRowKind;
  segmentId: number | null;
  start: number;
  end: number | null;
  startLabel: string;
  endLabel: string | null;
  text: string;
  tone: "final" | "muted" | "warning";
  italic: boolean;
}

export interface RecorderRecentRow {
  id: string;
  label: string;
  recording: RecorderRecentRecording;
}

export interface RecorderRecentView {
  empty: string | null;
  notice: string | null;
  rows: RecorderRecentRow[];
}

export interface RecorderView {
  header: string;
  header_wide: string;
  header_compact: string;
  header_tone: RecorderTone;
  a11y: string;
  source: string | null;
  capture: string | null;
  audio: string | null;
  audio_detail: string | null;
  audio_warning: string | null;
  transcript_status: string | null;
  transcript_detail: string | null;
  rows: RecorderTranscriptRow[];
  jump_live: boolean;
  earlier: boolean;
  buttons: RecorderButton[];
  button_ids: RecorderButtonId[];
  disabled: Partial<Record<RecorderButtonId, string>>;
  focus: string;
  esc: RecorderEsc;
  hint: string;
  recent: RecorderRecentView | null;
  state: {
    page: RecorderPage;
    width: RecorderWidth;
    ownership: "owner" | "viewer" | "none";
    capture_state: RecorderCaptureState;
    level: number;
    memory_only: boolean;
    modal_open: boolean;
  };
}

// Core's `reason` is a code. `input.owner.reason` is already display text.
// The code-to-word mapping is shared wording too, so it comes from `[strings]`;
// D0 must add these keys along with the rest of the recorder strings.
const CORE_REASON_KEYS: Record<string, RecorderTextKey> = {
  stream_detached: "recorder.reason.stream_detached",
  lease_expired: "recorder.reason.lease_expired",
  microphone: "recorder.reason.microphone",
  capture_failed: "recorder.reason.microphone",
  recorder_deleted: "recorder.reason.deleted",
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function sampleRate(rate = RECORDER_SAMPLE_RATE): number {
  return rate > 0 ? rate : RECORDER_SAMPLE_RATE;
}

/** Captured audio duration from integer samples: `mm:ss` below one hour, then `h:mm:ss`. */
export function formatElapsed(samples: number, rate = RECORDER_SAMPLE_RATE): string {
  const seconds = Math.max(0, Math.floor((Number.isFinite(samples) ? samples : 0) / sampleRate(rate)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  const tail = `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  return hours > 0 ? `${hours}:${tail}` : tail;
}

function wholeSeconds(samples: number, rate = RECORDER_SAMPLE_RATE): number {
  return Math.max(0, Math.round((Number.isFinite(samples) ? samples : 0) / sampleRate(rate)));
}

function textValue(value: string | null | undefined): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function localOwner(input: RecorderViewInput): RecorderOwnerSnapshot | null {
  return input.owner && input.owner.phase !== "idle" ? input.owner : null;
}

function coreCapture(core: RecorderCoreInput | null): RecorderCaptureState {
  if (!core) return "idle";
  switch (core.capture_state) {
    case "starting":
    case "recording":
    case "stopping":
    case "stopped":
    case "interrupted":
    case "failed":
      return core.capture_state;
    default:
      return core.active ? "recording" : "stopped";
  }
}

function captureIsActive(state: RecorderCaptureState): boolean {
  return state === "starting" || state === "recording" || state === "stopping";
}

function captureIsTerminalBad(state: RecorderCaptureState): boolean {
  return state === "interrupted" || state === "failed";
}

function reasonFor(core: RecorderCoreInput | null, owner: RecorderOwnerSnapshot | null, text: RecorderText): string {
  const ownerReason = textValue(owner?.reason);
  if (ownerReason) return ownerReason;
  const code = textValue(core?.reason);
  if (!code) return "";
  const key = CORE_REASON_KEYS[code];
  return key ? text(key) : code.replace(/_/g, " ");
}

function failureReason(failure: RecorderViewInput["failure"]): string {
  return textValue(failure?.reason) ?? textValue(failure?.kind)?.replace(/_/g, " ") ?? "";
}

/** Without a reason the row ends at the time; never leave the template's colon hanging. */
function interruptedCapture(t: string, reason: string, text: RecorderText): string {
  return reason
    ? text("recorder.capture.interrupted", { t, reason })
    : text("recorder.capture.interrupted_no_reason", { t });
}

function transcriptionState(core: RecorderCoreInput | null): string | null {
  return textValue(core?.transcription_state);
}

function stoppedFinishing(core: RecorderCoreInput | null): boolean {
  if (!core || coreCapture(core) !== "stopped") return false;
  const state = transcriptionState(core);
  return state === "catching_up" || state === "preparing";
}

interface HeaderChoice {
  wide: string;
  compact: string;
  tone: RecorderTone;
  a11y: string;
}

function headerChoice(input: RecorderViewInput, owner: RecorderOwnerSnapshot | null, core: RecorderCoreInput | null, text: RecorderText): HeaderChoice {
  const seen = input.view.seen_interrupted;
  if (input.view.page === "recent") {
    return {
      wide: text("recorder.title"), compact: text("recorder.title"), tone: "secondary",
      a11y: text("recorder.a11y.idle"),
    };
  }
  if (owner?.phase === "starting") {
    return {
      wide: text("recorder.header.starting"), compact: text("recorder.header.starting_compact"),
      tone: "error", a11y: text("recorder.a11y.starting"),
    };
  }
  if (owner?.phase === "recording") {
    const elapsed = formatElapsed(owner.captured_samples);
    return {
      wide: text("recorder.header.recording", { t: elapsed }),
      compact: text("recorder.header.recording_compact", { t: elapsed }),
      tone: "error", a11y: text("recorder.a11y.recording", { t: elapsed }),
    };
  }
  if (owner?.phase === "stopping") {
    return {
      wide: text("recorder.header.saving"), compact: text("recorder.header.saving_compact"),
      tone: "secondary", a11y: text("recorder.a11y.saving"),
    };
  }
  const coreState = coreCapture(core);
  if (!owner && captureIsActive(coreState) && core) {
    return {
      wide: text("recorder.header.elsewhere"), compact: text("recorder.header.elsewhere_compact"),
      tone: "secondary", a11y: text("recorder.a11y.elsewhere"),
    };
  }
  if (stoppedFinishing(core)) {
    return {
      wide: text("recorder.header.finishing"), compact: text("recorder.header.finishing_compact"),
      tone: "secondary", a11y: text("recorder.a11y.finishing"),
    };
  }
  const interrupted = owner?.phase === "failed" || owner?.phase === "interrupted" || captureIsTerminalBad(coreState);
  if (interrupted && !seen) {
    return {
      wide: text("recorder.header.interrupted"), compact: text("recorder.header.interrupted_compact"),
      tone: "warning", a11y: text("recorder.a11y.interrupted"),
    };
  }
  return {
    wide: text("recorder.header.idle"), compact: text("recorder.header.idle"), tone: "secondary",
    a11y: text("recorder.a11y.idle"),
  };
}

function captureLine(input: RecorderViewInput, owner: RecorderOwnerSnapshot | null, core: RecorderCoreInput | null, text: RecorderText): string | null {
  if (input.view.page === "recent") return null;
  if (owner) {
    switch (owner.phase) {
      case "starting":
        return text("recorder.capture.starting");
      case "recording":
        return text("recorder.capture.recording", { t: formatElapsed(owner.captured_samples) });
      case "stopping":
        return text("recorder.capture.stopping", { t: formatElapsed(owner.captured_samples) });
      case "stopped":
        return text("recorder.capture.stopped", { t: formatElapsed(owner.captured_samples) });
      case "interrupted":
        return interruptedCapture(formatElapsed(owner.captured_samples), reasonFor(core, owner, text), text);
      case "failed":
        return text("recorder.capture.failed", { reason: reasonFor(core, owner, text) });
      default:
        break;
    }
  }
  const state = coreCapture(core);
  if (core) {
    const elapsed = formatElapsed(core.durable_offset ?? core.final_sample_count ?? 0);
    switch (state) {
      case "starting":
      case "recording":
        return text("recorder.capture.elsewhere", { t: elapsed });
      case "stopping":
        return text("recorder.capture.stopping", { t: elapsed });
      case "stopped":
        return text("recorder.capture.stopped", { t: formatElapsed(core.final_sample_count ?? core.durable_offset ?? 0) });
      case "interrupted":
        return interruptedCapture(elapsed, reasonFor(core, null, text), text);
      case "failed":
        return text("recorder.capture.failed", { reason: reasonFor(core, null, text) });
      default:
        break;
    }
  }
  if (input.failure) return text("recorder.capture.failed", { reason: failureReason(input.failure) });
  return text("recorder.capture.idle");
}

interface AudioLines {
  audio: string | null;
  detail: string | null;
  warning: string | null;
}

function gapsLine(core: RecorderCoreInput, text: RecorderText): string {
  const holes = Math.max(0, core.holes ?? 0);
  const missing = core.missing_samples;
  if (typeof missing !== "number" || !Number.isFinite(missing)) {
    return text("recorder.audio.gaps_unknown", { n: holes });
  }
  return text("recorder.audio.gaps", { n: holes, s: wholeSeconds(missing) });
}

function audioLines(input: RecorderViewInput, owner: RecorderOwnerSnapshot | null, core: RecorderCoreInput | null, text: RecorderText): AudioLines {
  if (input.view.page === "recent") return { audio: null, detail: null, warning: null };
  const warning = owner?.spool === "memory" ? text("recorder.audio.memory_only") : null;
  const pending = owner && (owner.phase === "recording" || owner.phase === "stopping" || owner.phase === "starting")
    ? Math.max(0, owner.pending_samples) : 0;

  if (core) {
    if (owner?.phase === "interrupted" && owner.recoverable) {
      return {
        audio: text("recorder.audio.unsaved", { s: wholeSeconds(owner.pending_samples) }),
        detail: text("recorder.audio.saved_detail", { t: formatElapsed(core.durable_offset ?? 0) }),
        warning,
      };
    }
    if (Math.max(0, core.holes ?? 0) > 0) {
      return { audio: gapsLine(core, text), detail: null, warning };
    }
    const state = coreCapture(core);
    if (state === "stopped" || core.complete) {
      if (core.complete) {
        if (stoppedFinishing(core)) return { audio: text("recorder.audio.saved_finishing"), detail: null, warning };
        return { audio: text("recorder.audio.all_saved"), detail: null, warning };
      }
      return {
        audio: text("recorder.audio.saved_through", {
          t: formatElapsed(core.durable_offset ?? 0),
          s: wholeSeconds(pending),
        }),
        detail: null,
        warning,
      };
    }
    if (captureIsActive(state) || state === "interrupted" || state === "failed") {
      return {
        audio: text("recorder.audio.saved_through", {
          t: formatElapsed(core.durable_offset ?? 0),
          s: wholeSeconds(pending),
        }),
        detail: null,
        warning,
      };
    }
  }

  if (owner?.phase === "interrupted" && owner.recoverable) {
    return {
      audio: text("recorder.audio.unsaved", { s: wholeSeconds(owner.pending_samples) }),
      detail: null,
      warning,
    };
  }
  if (owner && (owner.phase === "recording" || owner.phase === "stopping") && pending > 0) {
    return {
      audio: text("recorder.audio.saved_through", { t: formatElapsed(0), s: wholeSeconds(pending) }),
      detail: null,
      warning,
    };
  }
  return { audio: text("recorder.audio.all_saved"), detail: null, warning };
}

interface TranscriptStatus {
  status: string | null;
  detail: string | null;
}

function transcriptStatus(input: RecorderViewInput, owner: RecorderOwnerSnapshot | null, core: RecorderCoreInput | null, text: RecorderText): TranscriptStatus {
  if (input.view.page === "recent") return { status: null, detail: null };
  const state = transcriptionState(core);
  if (core && state) {
    switch (state) {
      case "preparing":
        return { status: text("recorder.transcript.preparing"), detail: null };
      case "live":
        return { status: text("recorder.transcript.live"), detail: null };
      case "catching_up":
        return {
          status: text("recorder.transcript.catching_up", {
            s: wholeSeconds(core.transcription_progress?.lag_samples ?? 0),
          }),
          detail: null,
        };
      case "complete":
        return { status: text("recorder.transcript.complete"), detail: null };
      case "unavailable":
        return {
          status: text("recorder.transcript.unavailable"),
          detail: textValue(core.transcription_progress?.reason),
        };
      case "failed":
        return {
          status: text("recorder.transcript.failed", {
            n: Math.max(1, core.transcription_progress?.failed ?? 0),
          }),
          detail: null,
        };
      default:
        break;
    }
  }
  if (owner?.phase === "starting" || owner?.phase === "recording" || owner?.phase === "stopping") {
    return { status: text("recorder.transcript.preparing"), detail: null };
  }
  return { status: text("recorder.transcript.empty_idle"), detail: null };
}

function sourceLine(input: RecorderViewInput, text: RecorderText): string | null {
  if (!input.source) return null;
  const device = textValue(input.source.device);
  const microphone = device
    ? text("recorder.source.device", { device })
    : text("recorder.source.default_device");
  const host = textValue(input.source.host);
  return host ? `${microphone} · ${text("recorder.source.destination", { host })}` : microphone;
}

// --- Transcript reducer -----------------------------------------------------

export interface RecorderTranscriptSegment {
  segment_id: number;
  revision: number;
  start: number;
  end: number | null;
  status: "final" | "pending" | "error" | "skipped" | "provisional";
  text: string;
}

export interface RecorderTranscriptSegmentLike {
  segment_id: number;
  revision?: number | null;
  start?: number | null;
  end?: number | null;
  status?: string | null;
  text?: string | null;
}

export interface RecorderTranscriptState {
  recordingId: string | null;
  generation: number | null;
  segments: RecorderTranscriptSegment[];
  provisional: RecorderTranscriptSegment | null;
  revision: number | null;
  dropped: number;
}

export interface RecorderTranscriptPage {
  recording_id?: string | null;
  generation?: number | null;
  segments?: RecorderTranscriptSegmentLike[] | null;
  live?: { provisional?: RecorderTranscriptSegmentLike[] | RecorderTranscriptSegmentLike | null } | null;
  transcript_revision?: number | null;
}

export interface RecorderSegmentEvent {
  type: "recorder.segment";
  data: {
    id?: string | null;
    generation?: number | null;
    segment_id: number;
    revision?: number | null;
    start?: number | null;
    end?: number | null;
    status?: string | null;
    text?: string | null;
  };
}

function segmentStatus(status: string | null | undefined): RecorderTranscriptSegment["status"] {
  switch (status) {
    case "final":
    case "pending":
    case "error":
    case "skipped":
    case "provisional":
      return status;
    default:
      return "final";
  }
}

function normaliseSegment(input: RecorderTranscriptSegmentLike): RecorderTranscriptSegment {
  return {
    segment_id: input.segment_id,
    revision: Number.isFinite(input.revision) ? Math.max(0, input.revision ?? 0) : 0,
    start: Number.isFinite(input.start) ? Math.max(0, input.start ?? 0) : 0,
    end: typeof input.end === "number" && Number.isFinite(input.end) ? Math.max(0, input.end) : null,
    status: segmentStatus(input.status),
    text: typeof input.text === "string" ? input.text : "",
  };
}

export function emptyTranscript(recordingId: string | null = null, generation: number | null = null): RecorderTranscriptState {
  return { recordingId, generation, segments: [], provisional: null, revision: null, dropped: 0 };
}

function newer(existing: RecorderTranscriptSegment | undefined, next: RecorderTranscriptSegment): boolean {
  return existing === undefined || next.revision > existing.revision;
}

/** Live segment order is `(segment_id, revision)`, not revision alone. */
function newerTail(existing: RecorderTranscriptSegment | null, next: RecorderTranscriptSegment): boolean {
  if (!existing) return true;
  if (next.segment_id !== existing.segment_id) return next.segment_id > existing.segment_id;
  return next.revision > existing.revision;
}

function trimSegments(segments: RecorderTranscriptSegment[], dropped: number): { segments: RecorderTranscriptSegment[]; dropped: number } {
  if (segments.length <= MAX_TRANSCRIPT_SEGMENTS) return { segments, dropped };
  const remove = segments.length - MAX_TRANSCRIPT_SEGMENTS;
  return { segments: segments.slice(remove), dropped: dropped + remove };
}

/** Apply the durable segment page. The page is authoritative for its provisional tail. */
export function applyTranscriptPage(state: RecorderTranscriptState, page: RecorderTranscriptPage): RecorderTranscriptState {
  const recordingId = page.recording_id !== undefined ? page.recording_id : state.recordingId;
  const generation = page.generation !== undefined ? page.generation : state.generation;
  const sameRecording = recordingId == null || state.recordingId == null || recordingId === state.recordingId;
  const sameGeneration = generation == null || state.generation == null || generation === state.generation;
  const base = sameRecording && sameGeneration ? state : emptyTranscript(recordingId ?? null, generation ?? null);
  const byId = new Map<number, RecorderTranscriptSegment>();
  for (const segment of base.segments) byId.set(segment.segment_id, segment);
  for (const raw of page.segments ?? []) {
    const segment = normaliseSegment(raw);
    if (segment.status === "provisional") continue;
    if (newer(byId.get(segment.segment_id), segment)) byId.set(segment.segment_id, segment);
  }
  const trimmed = trimSegments([...byId.values()].sort((a, b) => a.segment_id - b.segment_id), base.dropped);
  const live = page.live?.provisional;
  const provisionalRaw = Array.isArray(live) ? live[live.length - 1] : live;
  const provisional = provisionalRaw ? normaliseSegment(provisionalRaw) : null;
  return {
    recordingId: recordingId ?? null,
    generation: generation ?? null,
    segments: trimmed.segments,
    provisional: provisional && provisional.status === "provisional" ? provisional : null,
    revision: page.transcript_revision ?? base.revision,
    dropped: trimmed.dropped,
  };
}

/** Apply one live `recorder.segment`, ignoring another recording/generation and stale revisions. */
export function applyTranscriptEvent(state: RecorderTranscriptState, event: RecorderSegmentEvent): RecorderTranscriptState {
  const data = event.data;
  if (state.recordingId !== null && data.id != null && data.id !== state.recordingId) return state;
  if (state.generation !== null && data.generation != null && data.generation !== state.generation) return state;
  const segment = normaliseSegment(data);
  if (segment.status === "provisional") {
    const lastDurable = state.segments[state.segments.length - 1];
    if (lastDurable && segment.segment_id <= lastDurable.segment_id && segment.revision <= lastDurable.revision) return state;
    if (!newerTail(state.provisional, segment)) return state;
    return { ...state, provisional: segment };
  }
  const byId = new Map<number, RecorderTranscriptSegment>();
  for (const existing of state.segments) byId.set(existing.segment_id, existing);
  if (!newer(byId.get(segment.segment_id), segment)) return state;
  byId.set(segment.segment_id, segment);
  const trimmed = trimSegments([...byId.values()].sort((a, b) => a.segment_id - b.segment_id), state.dropped);
  const provisional = state.provisional && state.provisional.segment_id > segment.segment_id
    ? state.provisional : null;
  return { ...state, segments: trimmed.segments, provisional, dropped: trimmed.dropped };
}

function transcriptRows(state: RecorderTranscriptState, follow: boolean, text: RecorderText): {
  rows: RecorderTranscriptRow[]; jumpLive: boolean; earlier: boolean;
} {
  const rows: RecorderTranscriptRow[] = [];
  const earlier = state.dropped > 0;
  if (earlier) {
    rows.push({
      kind: "earlier", segmentId: null, start: 0, end: null, startLabel: "", endLabel: null,
      text: text("recorder.transcript.earlier"), tone: "muted", italic: false,
    });
  }
  for (const segment of state.segments) {
    if (segment.status === "skipped") continue;
    const startLabel = formatElapsed(segment.start);
    if (segment.status === "pending") {
      rows.push({
        kind: "pending", segmentId: segment.segment_id, start: segment.start, end: segment.end,
        startLabel, endLabel: null, text: text("recorder.transcript.pending_row"), tone: "muted", italic: false,
      });
      continue;
    }
    if (segment.status === "error") {
      rows.push({
        kind: "error", segmentId: segment.segment_id, start: segment.start, end: segment.end,
        startLabel, endLabel: null, text: text("recorder.transcript.error_row"), tone: "warning", italic: false,
      });
      continue;
    }
    rows.push({
      kind: "final", segmentId: segment.segment_id, start: segment.start, end: segment.end,
      startLabel, endLabel: segment.end === null ? null : formatElapsed(segment.end),
      text: segment.text, tone: "final", italic: false,
    });
  }
  if (state.provisional) {
    const provisional = state.provisional;
    const body = provisional.text.endsWith("…") ? provisional.text : `${provisional.text}…`;
    rows.push({
      kind: "provisional", segmentId: provisional.segment_id, start: provisional.start, end: null,
      startLabel: formatElapsed(provisional.start), endLabel: null, text: body, tone: "muted", italic: true,
    });
  }
  return { rows, jumpLive: !follow, earlier };
}

// --- Recent page ------------------------------------------------------------

export function formatRecentDate(value: string | null | undefined): string {
  if (!value) return "";
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(value);
  if (!match) return value;
  const month = MONTHS[Number(match[2]) - 1] ?? match[2];
  return `${Number(match[3])} ${month} ${match[4]}:${match[5]}`;
}

function recentStatus(recording: RecorderRecentRecording, text: RecorderText): string {
  const state = recording.capture_state ?? (recording.active ? "recording" : "stopped");
  if (state === "interrupted" || state === "failed") {
    const gaps = Math.max(0, recording.holes ?? 0);
    const base = text("recorder.status.interrupted");
    return gaps > 0 ? `${base} · ${text("recorder.recent.gaps", { n: gaps })}` : base;
  }
  if (state === "starting" || state === "recording" || state === "stopping" || recording.active) {
    return text("recorder.status.recording");
  }
  return recording.transcription_state === "complete"
    ? text("recorder.status.complete")
    : text("recorder.status.transcript_incomplete");
}

function recentView(input: RecorderViewInput, text: RecorderText): RecorderRecentView | null {
  if (input.view.page !== "recent") return null;
  const recordings = input.recent?.recordings ?? [];
  const limit = input.recent?.limit ?? 50;
  const rows = recordings.map((recording) => {
    const date = formatRecentDate(recording.created_at);
    const elapsed = formatElapsed(recording.final_sample_count ?? recording.durable_offset ?? 0);
    return {
      id: recording.id,
      label: text("recorder.recent.row", { date, t: elapsed, status: recentStatus(recording, text) }),
      recording,
    };
  });
  return {
    empty: rows.length === 0 ? text("recorder.recent.empty") : null,
    notice: recordings.length >= limit ? text("recorder.recent.limit") : null,
    rows,
  };
}

// --- Actions, focus, Esc ----------------------------------------------------

const ACTION_LABELS: Record<RecorderButtonId, RecorderTextKey> = {
  "recorder.start": "recorder.action.start",
  "recorder.minimize": "recorder.action.minimize",
  "recorder.stop": "recorder.action.stop",
  "recorder.copy_transcript": "recorder.action.copy_transcript",
  "recorder.retry": "recorder.action.retry",
  "recorder.recover": "recorder.action.recover",
  "recorder.recent": "recorder.action.recent",
  "recorder.delete": "recorder.action.delete",
};

function button(id: RecorderButtonId, text: RecorderText, disabled: Partial<Record<RecorderButtonId, string>>): RecorderButton {
  const reason = disabled[id] ?? null;
  return { id, label: text(ACTION_LABELS[id]), enabled: reason === null, reason };
}

interface Actions {
  buttons: RecorderButton[];
  disabled: Partial<Record<RecorderButtonId, string>>;
  focus: string;
  esc: RecorderEsc;
  hint: string;
}

function actionsFor(input: RecorderViewInput, owner: RecorderOwnerSnapshot | null, core: RecorderCoreInput | null, text: RecorderText, recent: RecorderRecentView | null): Actions {
  const disabled: Partial<Record<RecorderButtonId, string>> = {};
  if (input.view.page === "recent") {
    const ids: RecorderButtonId[] = ["recorder.start"];
    if (recent && recent.rows.length > 0) ids.push("recorder.delete");
    return {
      buttons: ids.map((id) => button(id, text, disabled)),
      disabled,
      focus: recent && recent.rows.length > 0 ? "recent.0" : "recorder.start",
      esc: "back",
      hint: text("recorder.hint.recent"),
    };
  }

  const capturing = owner !== null && (owner.phase === "starting" || owner.phase === "recording" || owner.phase === "stopping");
  const hint = capturing ? text("recorder.hint.capturing") : text("recorder.hint.idle");
  const esc: RecorderEsc = capturing ? "minimize" : "close";

  if (owner?.phase === "starting" || owner?.phase === "recording") {
    const ids: RecorderButtonId[] = ["recorder.minimize", "recorder.stop"];
    return { buttons: ids.map((id) => button(id, text, disabled)), disabled, focus: "recorder.minimize", esc, hint };
  }
  if (owner?.phase === "stopping") {
    disabled["recorder.stop"] = text("recorder.header.saving");
    return {
      buttons: [button("recorder.minimize", text, disabled)], disabled, focus: "recorder.minimize", esc, hint,
    };
  }
  if (owner?.phase === "interrupted" && owner.recoverable) {
    return {
      buttons: [button("recorder.recover", text, disabled)], disabled, focus: "recorder.recover", esc, hint,
    };
  }
  if (owner?.phase === "failed") {
    const ids: RecorderButtonId[] = ["recorder.start", "recorder.recent"];
    return { buttons: ids.map((id) => button(id, text, disabled)), disabled, focus: "recorder.start", esc, hint };
  }

  const state = coreCapture(core);
  if (core && captureIsActive(state)) {
    const ids: RecorderButtonId[] = ["recorder.copy_transcript", "recorder.recent"];
    return { buttons: ids.map((id) => button(id, text, disabled)), disabled, focus: "recorder.copy_transcript", esc, hint };
  }
  if (core && (state === "interrupted" || state === "failed") && !(owner?.phase === "interrupted" && owner.recoverable)) {
    const ids: RecorderButtonId[] = ["recorder.copy_transcript", "recorder.recent"];
    return { buttons: ids.map((id) => button(id, text, disabled)), disabled, focus: "recorder.copy_transcript", esc, hint };
  }

  if (state === "stopped" || (owner?.phase === "stopped")) {
    const failedParts = core?.transcription_progress?.failed ?? 0;
    const retry = transcriptionState(core) === "failed" || failedParts > 0;
    const ids: RecorderButtonId[] = ["recorder.copy_transcript"];
    if (retry) ids.push("recorder.retry");
    ids.push("recorder.recent", "recorder.start");
    return { buttons: ids.map((id) => button(id, text, disabled)), disabled, focus: "recorder.copy_transcript", esc, hint };
  }

  const ids: RecorderButtonId[] = ["recorder.start", "recorder.recent"];
  return { buttons: ids.map((id) => button(id, text, disabled)), disabled, focus: "recorder.start", esc, hint };
}

// --- Entry point ------------------------------------------------------------

function transcriptFromInput(input: RecorderViewInput): RecorderTranscriptState {
  if (!input.transcript) return emptyTranscript();
  let state = emptyTranscript();
  state = applyTranscriptPage(state, {
    segments: input.transcript.segments ?? [],
    live: { provisional: input.transcript.provisional ?? null },
    transcript_revision: input.transcript.transcript_revision ?? null,
  });
  return input.transcript.dropped ? { ...state, dropped: Math.max(0, input.transcript.dropped) } : state;
}

/** Map one shared fixture input to the exact recorder view both desktop clients draw. */
export function recorderView(input: RecorderViewInput, text: RecorderText = recorderText): RecorderView {
  const owner = localOwner(input);
  const core = input.core;
  const header = headerChoice(input, owner, core, text);
  const audio = audioLines(input, owner, core, text);
  const transcription = transcriptStatus(input, owner, core, text);
  const recent = recentView(input, text);
  const actions = actionsFor(input, owner, core, text, recent);
  const transcript = transcriptRows(transcriptFromInput(input), input.view.transcript_follow, text);
  const state = coreCapture(core);
  const ownership: "owner" | "viewer" | "none" = owner ? "owner" : core ? "viewer" : "none";

  return {
    header: input.width === "compact" ? header.compact : header.wide,
    header_wide: header.wide,
    header_compact: header.compact,
    header_tone: header.tone,
    a11y: header.a11y,
    source: sourceLine(input, text),
    capture: captureLine(input, owner, core, text),
    audio: audio.audio,
    audio_detail: audio.detail,
    audio_warning: audio.warning,
    transcript_status: transcription.status,
    transcript_detail: transcription.detail,
    rows: transcript.rows,
    jump_live: transcript.jumpLive,
    earlier: transcript.earlier,
    buttons: actions.buttons,
    button_ids: actions.buttons.map((item) => item.id),
    disabled: actions.disabled,
    focus: actions.focus,
    esc: actions.esc,
    hint: actions.hint,
    recent,
    state: {
      page: input.view.page,
      width: input.width,
      ownership,
      capture_state: owner?.phase ?? state,
      level: owner ? Math.max(0, Math.min(1, owner.level)) : 0,
      memory_only: owner?.spool === "memory",
      modal_open: input.view.modal_open,
    },
  };
}
