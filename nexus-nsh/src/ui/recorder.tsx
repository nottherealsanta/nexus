// The standalone recorder (plan 54, milestone D, UI-NSH).
//
// The view itself is pure (`src/recorder-view.ts`): this file only wires the
// capture controller, Core's public state and the transcript to it, and draws
// the header entry and the large modal. Opening never opens a microphone or
// creates a recording. Recordings stay in Core's state home; the modal has no
// Save As or browser download.
import { Clipboard, Scroll, Storage, Text, View, effect, keymap, platform, signal } from "nsh";
import { Button } from "nsh/base";
import { Application } from "nsh/desktop";
import { Btn, FOCUS, T, TS } from "./kit";
import { Modal, ModalFooter, ModalTitle } from "./modal";
import { MicrophoneBusyError, microphoneOwner, openRecorder as openWebMicrophone } from "../audio-capture";
import type { MicrophoneOwner, OpenCapture } from "../audio-capture";
import type { PendingRecoveryOutcome, RecoverableSpoolIndex, RecoverableSpoolIndexFactory, RecoverableSpoolOutcome, RecorderCaptureHandlers, RecorderPendingStore, RecorderSpool, RecorderSpoolFactory, RecorderSpoolGuarantee } from "../recorder";
import { RecorderController, createIndexedDbPendingStarts, createIndexedDbRecorderSpool, createIndexedDbRecoveryIndex, createWebRecorderApi, memoryPendingStore, memoryRecorderSpool, memoryRecoveryIndex } from "../recorder";
import { nativeRecorderAvailable, nativeRecorderMic, openNativeRecorderMicrophone } from "../recorder-native";
import {
  applyTranscriptEvent,
  applyTranscriptPage,
  emptyTranscript,
  recorderView,
  recorderText,
  formatElapsed,
} from "../recorder-view";
import type {
  RecorderCoreInput,
  RecorderOwnerSnapshot,
  RecorderRecentRecording,
  RecorderTranscriptPage,
  RecorderView,
  RecorderViewInput,
} from "../recorder-view";
import { followsAfterScroll } from "../scroll-follow";
import {
  coreUrl, recorderAnnouncement, recorderCore, recorderFailure,
  recorderFocus, recorderFollow, recorderHooks, recorderOpen, recorderPage,
  recorderQuitPrompt, recorderRecent, recorderRecentLimit, recorderRecentSelected, recorderScroll,
  recorderSeenInterrupted, recorderSnapshot, recorderSource, recorderStartup, recorderStreamId, recorderTranscript,
  toast, viewReporter, confirmDialog, isNarrow,
} from "../store";
import { newKey, request } from "../core/transport";
import { inCall } from "../core/manager";
import { isDictationBusy } from "../dictation";
import { uiText } from "../gen/manifest";

const text = (key: string, args: Record<string, string | number> = {}): string =>
  uiText(key as Parameters<typeof uiText>[0], args);

/** A modal action id matches its `recorder.*` command so tests and the flow fixture can name it. */
export const RECORDER_ACTION_IDS = [
  "recorder.start", "recorder.minimize", "recorder.stop", "recorder.copy_transcript",
  "recorder.retry", "recorder.recover", "recorder.recent", "recorder.delete",
] as const;

// --- controller wiring --------------------------------------------------------------------

let controller: RecorderController | null = null;
let openedFrom: "header" | "palette" | "previous" = "previous";
let busyHolder: MicrophoneOwner | null = null;
let recorderDeleteWasOpen = false;

/** Mark where the user asked for the modal, so closing returns focus there. */
export function noteRecorderSource(source: "header" | "palette" | "previous") {
  openedFrom = source;
}

let testSeam: Promise<void> | null = null;
let testCaptureMode: "silent" | "no-persist" | "reload" | null = null;

/**
 * A test/screenshot seam, like `nexus.manager.dev`: nothing writes it, and a
 * build without it behaves exactly as before. It lets the focused UI tests
 * exercise start/stop with no microphone or hardware.
 */
function ensureTestSeam(): Promise<void> {
  if (!testSeam) {
    testSeam = Storage.getItem("nexus.recorder.test")
      .then((raw) => {
        testCaptureMode = raw === "silent" ? "silent"
          : raw === "no-persist" ? "no-persist"
            : raw === "reload" ? "reload"
              : null;
      })
      .catch(() => { testCaptureMode = null; });
  }
  return testSeam;
}

export async function loadRecorderTestSeam(): Promise<void> {
  await ensureTestSeam();
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const subtle = (globalThis as unknown as { crypto?: { subtle?: { digest(algorithm: string, data: ArrayBuffer): Promise<ArrayBuffer> } } }).crypto?.subtle;
  if (!subtle) throw new Error("Web Crypto is not available in this client");
  const digest = await subtle.digest("SHA-256", bytes.slice().buffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** A Core or native error that means another local feature holds the microphone. */
function microphoneBusyHolder(error: unknown): MicrophoneOwner | null {
  if (error instanceof MicrophoneBusyError) return error.holder;
  const value = error as { code?: unknown; holder?: unknown; message?: unknown } | null;
  if (value?.code === "microphone_busy") {
    return value.holder === "manager" || value.holder === "dictation" || value.holder === "recorder" ? value.holder : "recorder";
  }
  const message = typeof value?.message === "string" ? value.message : "";
  if (/already in use by/i.test(message)) {
    if (/manager/i.test(message)) return "manager";
    if (/dictation/i.test(message)) return "dictation";
    if (/recorder/i.test(message)) return "recorder";
  }
  return null;
}

function busyOwner(holder: MicrophoneOwner): string {
  return holder === "manager" ? "the Manager's voice" : holder === "recorder" ? "the recorder" : "dictation";
}

function busyReason(holder: MicrophoneOwner): string {
  return text("recorder.busy.mic", { owner: busyOwner(holder) });
}

async function openRecorderMicrophone(
  onSamples: (samples: Float32Array) => void,
  handlers?: RecorderCaptureHandlers,
): Promise<OpenCapture> {
  await ensureTestSeam();
  busyHolder = null;
  if (testCaptureMode === "silent") {
    return { rate: 16_000, recorder: { async stop() {} } };
  }
  try {
    if (platform.os === "web") return await openWebMicrophone(onSamples);
    const native = nativeRecorderMic();
    if (!native || !nativeRecorderAvailable(native)) {
      throw new Error("Continuous microphone capture is not available in this build");
    }
    return await openNativeRecorderMicrophone({ native })(onSamples, handlers);
  } catch (error) {
    busyHolder = microphoneBusyHolder(error);
    throw error;
  }
}

/** A persistent in-memory spool for UI tests; never used without the explicit storage seam. */
function silentTestSpool(): RecorderSpool {
  const spool = memoryRecorderSpool(null);
  return { ...spool, durable: true, guarantee: "persistent", warning: null };
}

const testSpoolFactory: RecorderSpoolFactory = (recordingId) =>
  testCaptureMode === "silent" || testCaptureMode === "reload" ? silentTestSpool() : createIndexedDbRecorderSpool(recordingId);

let pendingGuarantee: RecorderSpoolGuarantee | null = null;

const TEST_RELOAD_ACTIVE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const TEST_RELOAD_LOCKED = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

/** A deterministic discovery index for the reload UI test: one active and one locked spool. */
function reloadTestRecoveryIndex(): RecoverableSpoolIndex {
  const summary = (recordingId: string) => ({
    recordingId, generation: 1, finalSampleCount: null, captureLostSamples: 0, discardedSamples: 0,
  });
  return {
    crossTabLocks: true,
    list() { return [summary(TEST_RELOAD_ACTIVE), summary(TEST_RELOAD_LOCKED)]; },
    async withLock<T>(recordingId: string, run: () => Promise<T>) {
      return recordingId === TEST_RELOAD_LOCKED ? undefined : run();
    },
  };
}

/**
 * The web build discovers IndexedDB spools. A native client will install its
 * own index when the native persistence bridge lands; until then native uses
 * the empty no-op index instead of silently treating browser storage as native.
 */
let nativeRecoveryIndexFactory: RecoverableSpoolIndexFactory | null = null;
export function setNativeRecoveryIndexFactory(factory: RecoverableSpoolIndexFactory | null): void {
  nativeRecoveryIndexFactory = factory;
  if (controller) void controller.listRecoverableSpools().catch(() => {});
}

async function recoveryIndexFactory(): Promise<RecoverableSpoolIndex> {
  if (testCaptureMode === "reload") return reloadTestRecoveryIndex();
  if (testCaptureMode) return memoryRecoveryIndex();
  if (platform.os === "web") return createIndexedDbRecoveryIndex();
  return nativeRecoveryIndexFactory ? nativeRecoveryIndexFactory() : memoryRecoveryIndex();
}

async function pendingStartsFactory(): Promise<RecorderPendingStore> {
  const store = testCaptureMode === "silent" || testCaptureMode === "reload"
    ? memoryPendingStore("", "persistent")
    : testCaptureMode === "no-persist"
      ? memoryPendingStore("Persistent browser storage is required for the recorder", "memory")
      : await createIndexedDbPendingStarts();
  pendingGuarantee = store.guarantee;
  return store;
}

function recorderController(): RecorderController {
  if (controller) return controller;
  controller = new RecorderController({
    openMicrophone: openRecorderMicrophone,
    api: createWebRecorderApi(),
    digest: sha256Hex,
    spool: testSpoolFactory,
    pendingStarts: pendingStartsFactory,
    recoverableSpools: recoveryIndexFactory,
    streamId: () => recorderStreamId() ?? (testCaptureMode ? "nsh-web-test" : null),
    clientId: viewReporter.clientId,
    onChange: (snapshot) => {
      recorderSnapshot.set(snapshot);
      if (snapshot.phase === "interrupted" || snapshot.phase === "failed") recorderSeenInterrupted.set(false);
    },
  });
  return controller;
}

// --- launch recovery -----------------------------------------------------------------------

/** Exact wording the TUI driver uses when Core's bounded withdrawn-key guard is full. */
const RECOVERY_RESTART_REQUIRED = "Core's recorder create guard is full; restart Core before creating another recording";
const RECOVERY_RUNNING = "Recovering recordings…";
/** The platform-neutral recorder storage refusal shared with the TUI. */
const RECORDING_STORAGE_REQUIRED = text("recorder.storage.persistent_required");

function isPersistentStorageRefusal(reason: string | null | undefined): boolean {
  if (!reason) return false;
  return /persistent\s+(browser\s+|local\s+)?storage/i.test(reason)
    || /persistent storage is required/i.test(reason);
}

let recoveryRun: Promise<void> | null = null;

/** Surface one recovery pass without opening the microphone or starting a capture. */
export function applyRecorderRecoveryOutcomes(
  outcomes: readonly PendingRecoveryOutcome[],
  spoolOutcomes: readonly RecoverableSpoolOutcome[] = [],
): void {
  const restartRequired = outcomes.some((outcome) => outcome.restartRequired === true);
  const issues = [...new Set([
    ...outcomes
      .filter((outcome) => outcome.restartRequired || outcome.outcome === "failed" || outcome.outcome === "kept")
      .map((outcome) => outcome.code ?? outcome.outcome),
    ...spoolOutcomes
      .filter((outcome) => outcome.outcome === "failed")
      .map((outcome) => outcome.code ?? "spool_recovery_failed"),
  ])];
  const error = restartRequired
    ? RECOVERY_RESTART_REQUIRED
    : issues.length ? `Recorder recovery could not finish: ${issues.join(", ")}` : null;
  const storagePersistent = pendingGuarantee === null ? null : pendingGuarantee === "persistent";
  const storageReason = storagePersistent === false ? RECORDING_STORAGE_REQUIRED : null;
  recorderStartup.set({
    ready: !restartRequired && storagePersistent !== false,
    recovering: false,
    restartRequired,
    error,
    issues,
    storagePersistent,
    storageReason,
  });
  if (restartRequired) toast("error", RECOVERY_RESTART_REQUIRED);
  else if (error) toast("warning", error);
}

/** The reason Start is unavailable, or null. Recovers pending create intents once. */
export function recorderStartBlockReason(): string | null {
  const startup = recorderStartup();
  if (startup.recovering) return RECOVERY_RUNNING;
  if (startup.restartRequired) return startup.error ?? RECOVERY_RESTART_REQUIRED;
  if (startup.storagePersistent === false) return startup.storageReason ?? RECORDING_STORAGE_REQUIRED;
  return null;
}

/** Run at launch, before Start becomes available. Never opens the microphone. */
export function recoverRecorderAtLaunch(): Promise<void> {
  if (recoveryRun) return recoveryRun;
  const run = (async () => {
    await ensureTestSeam();
    recorderStartup.update((state) => ({ ...state, ready: false, recovering: true, storagePersistent: null, storageReason: null }));
    try {
      const outcomes = await recorderController().recoverPending();
      const spools = await recorderController().recoverSpools();
      applyRecorderRecoveryOutcomes(outcomes, spools);
    } catch (error) {
      applyRecorderRecoveryOutcomes([{
        key: "startup",
        outcome: "failed",
        code: error instanceof Error ? error.message : String(error),
      }]);
    } finally {
      recoveryRun = null;
    }
    // The TUI loads the recorder's current state at startup; the header must be
    // right before the user opens the modal, including an interrupted capture.
    await refreshRecorder(true);
  })();
  recoveryRun = run;
  return run;
}

// --- Core state and transcript -------------------------------------------------------------

function ownerActive(snapshot = recorderSnapshot()): boolean {
  return !!snapshot && (snapshot.phase === "starting" || snapshot.phase === "recording" || snapshot.phase === "stopping");
}

export function recorderOwnsCapture(): boolean {
  return ownerActive();
}

export function recorderIsActive(): boolean {
  const core = recorderCore();
  const state = core?.capture_state;
  return ownerActive() || core?.active === true || state === "starting" || state === "recording" || state === "stopping";
}

function coreRevision(core: RecorderCoreInput | null): number {
  if (!core) return -1;
  const value = (core as unknown as Record<string, unknown>).event_revision ?? core.revision;
  return typeof value === "number" && Number.isFinite(value) ? value : -1;
}

function currentRecordingId(): string | null {
  return recorderSnapshot()?.recordingId ?? recorderCore()?.id ?? recorderTranscript().recordingId ?? null;
}

const CAPTURE_REASON_CODES = new Set([
  "created", "stopped", "lease_expired", "rebound", "recovered",
  "stream_detached", "microphone", "capture_failed", "deleted", "recorder_deleted",
  "interrupted", "failed",
]);

function adoptCore(next: RecorderCoreInput): void {
  if (!next?.id) return;
  const current = recorderCore();
  if (current && current.id === next.id && coreRevision(next) <= coreRevision(current)) return;
  const interrupted = next.capture_state === "interrupted" || next.capture_state === "failed";
  const sameTerminal = !!current && current.id === next.id
    && (current.capture_state === "interrupted" || current.capture_state === "failed")
    && current.capture_state === next.capture_state;
  const merged: RecorderCoreInput = { ...(current?.id === next.id ? current : {}), ...next };
  // A later transcription update carries reason "transcription"; it must not
  // replace the capture interruption that the user needs to understand.
  if (sameTerminal && (!next.reason || !CAPTURE_REASON_CODES.has(next.reason))) {
    merged.reason = current?.reason ?? null;
  }
  recorderCore.set(merged);
  if (!current || current.id !== next.id) {
    recorderTranscript.set(emptyTranscript(next.id, next.generation ?? null));
    recorderSeenInterrupted.set(false);
  } else if (interrupted && current.capture_state !== next.capture_state) {
    recorderSeenInterrupted.set(false);
  }
}

function applyStateEvent(data: RecorderCoreInput): void {
  if (!data?.id) return;
  const current = recorderCore();
  if (current && current.id !== data.id && ownerActive()) return;
  if (!current || current.id !== data.id) {
    if (!data.active && !["starting", "recording", "stopping", "interrupted", "failed"].includes(String(data.capture_state))) return;
  }
  adoptCore(data);
}

function applyDeletedEvent(id: string | null | undefined): void {
  if (!id) return;
  recorderRecent.update((rows) => rows.filter((row) => row.id !== id));
  if (recorderTranscript().recordingId === id) recorderTranscript.set(emptyTranscript());
  if (recorderCore()?.id === id) {
    recorderCore.set(null);
    recorderPage.set("recorder");
  }
}

/** One global recorder event, delivered by sync (live state, live-only segments, deletions). */
export function applyRecorderGlobalEvent(event: { type?: string; data?: any }): void {
  if (!event?.type || !event.type.startsWith("recorder.")) return;
  recorderController().handleRecorderEvent(event);
  const data = event.data ?? {};
  if (event.type === "recorder.state") {
    applyStateEvent(data as RecorderCoreInput);
    return;
  }
  if (event.type === "recorder.segment") {
    const state = recorderTranscript();
    if (state.recordingId !== null && data.id !== state.recordingId) return;
    if (state.recordingId === null && recorderCore()?.id !== data.id) return;
    const base = state.recordingId === null ? emptyTranscript(data.id ?? null, data.generation ?? null) : state;
    const next = applyTranscriptEvent(base, event as Parameters<typeof applyTranscriptEvent>[1]);
    recorderTranscript.set(next);
    if (next.provisional?.text && recorderOpen()) announceRecorder(next.provisional.text, false);
    return;
  }
  if (event.type === "recorder.deleted") {
    applyDeletedEvent(data.id);
  }
}

function mapOwner(snapshot: ReturnType<RecorderController["snapshot"]> | null): RecorderOwnerSnapshot | null {
  if (!snapshot || snapshot.phase === "idle") return null;
  const reason = snapshot.lastError && snapshot.lastError.length ? snapshot.lastError : snapshot.failure;
  const storageRefusal = snapshot.failure === "storage" && isPersistentStorageRefusal(reason);
  return {
    phase: snapshot.phase,
    recording_id: snapshot.recordingId,
    captured_samples: snapshot.capturedSamples,
    pending_samples: snapshot.pendingSamples,
    spool: snapshot.failure === "storage" ? "none" : snapshot.spoolGuarantee,
    recoverable: snapshot.recoverable,
    reason: storageRefusal
      ? RECORDING_STORAGE_REQUIRED
      : snapshot.failure === "microphone" && busyHolder ? busyReason(busyHolder) : reason ? failureText(String(reason)) : null,
    level: snapshot.level,
  };
}

function failureText(reason: string): string {
  if (reason === "recorder_busy") return text("recorder.busy.other_client");
  return reason;
}

function currentTranscriptInput(view: RecorderViewInput["transcript"]): RecorderViewInput["transcript"] {
  const state = recorderTranscript();
  return {
    segments: state.segments,
    provisional: state.provisional,
    dropped: state.dropped,
    transcript_revision: state.revision,
    ...(view ?? {}),
  };
}

/** The exact recorder view for the current signals, shared by the header and the modal. */
export function recorderViewNow(): RecorderView {
  return recorderView({
    owner: mapOwner(recorderSnapshot()),
    core: recorderCore(),
    transcript: currentTranscriptInput(null),
    failure: recorderFailure(),
    recent: { recordings: recorderRecent(), limit: recorderRecentLimit() },
    source: recorderSource(),
    view: {
      modal_open: recorderOpen(),
      page: recorderPage(),
      transcript_follow: recorderFollow(),
      seen_interrupted: recorderSeenInterrupted(),
    },
    width: isNarrow() ? "compact" : "wide",
  }, (key, args) => recorderText(key, args));
}

// --- Core reads ----------------------------------------------------------------------------

function destinationHost(): string {
  const raw = coreUrl();
  if (!raw) {
    try {
      const doc = (globalThis as unknown as { document?: { baseURI?: string } }).document;
      const url = doc?.baseURI ? new URL(doc.baseURI) : null;
      if (url?.hostname) return loopback(url.hostname) ? "this computer" : url.hostname;
    } catch {}
    return "this computer";
  }
  try { const host = new URL(raw).hostname; return loopback(host) ? "this computer" : host; }
  catch { return "this computer"; }
}
const loopback = (host: string) => host === "127.0.0.1" || host === "::1" || host === "[::1]" || host === "localhost";

function refreshSource() {
  recorderSource.set({ device: null, host: destinationHost() });
}

async function fetchTranscript(id: string, generation: number | null): Promise<void> {
  const page = await request<RecorderTranscriptPage>("GET", `/api/recordings/${id}/transcript`, { query: { after: -1, limit: 200 } });
  const base = emptyTranscript(id, generation);
  recorderTranscript.set(applyTranscriptPage(base, { ...page, recording_id: id, generation }));
}

/** Re-read the list and/or the recording the modal shows. Never creates a recording or opens a microphone. */
export async function refreshRecorder(list: boolean): Promise<void> {
  refreshSource();
  try {
    if (list) {
      const result = await request<{ recordings?: RecorderRecentRecording[] }>("GET", "/api/recordings", { query: { limit: recorderRecentLimit() } });
      recorderRecent.set(result.recordings ?? []);
    }
    const current = recorderCore();
    const owner = recorderSnapshot();
    const target = ownerActive() ? owner?.recordingId ?? null
      : current?.active ? current.id
      : current?.id ?? recorderRecent()[0]?.id ?? null;
    if (!target) {
      recorderCore.set(null);
      recorderTranscript.set(emptyTranscript());
      return;
    }
    const core = await request<RecorderCoreInput>("GET", `/api/recordings/${target}`);
    adoptCore(core);
    await fetchTranscript(target, core.generation ?? null);
    recorderFailure.set(null);
  } catch (error) {
    if (recorderOpen()) toast("error", `Recorder: ${error instanceof Error ? error.message : String(error)}`);
  }
}

/** Open the recorder on the current or most recent recording: list, state and transcript only. */
export async function openRecorder(source?: "header" | "palette" | "previous"): Promise<void> {
  if (source) noteRecorderSource(source);
  recorderOpen.set(true);
  recorderPage.set("recorder");
  recorderFailure.set(null);
  recorderFocus.set(recorderViewNow().focus);
  await refreshRecorder(true);
  recorderFocus.set(recorderViewNow().focus);
}

// --- actions -------------------------------------------------------------------------------

async function busyMicrophone(): Promise<boolean> {
  const holder = microphoneOwner() ?? (isDictationBusy() ? "dictation" : inCall() ? "manager" : null);
  if (!holder) return false;
  recorderFailure.set({ kind: "busy", reason: busyReason(holder) });
  return true;
}

export async function startRecorder(): Promise<void> {
  await ensureTestSeam();
  const blocked = recorderStartBlockReason();
  if (blocked) {
    toast("info", blocked);
    recorderFocus.set(recorderViewNow().focus);
    return;
  }
  recorderFailure.set(null);
  if (recorderPage() === "recent") recorderPage.set("recorder");
  if (await busyMicrophone()) {
    recorderFocus.set(recorderViewNow().focus);
    return;
  }
  recorderFocus.set(recorderViewNow().focus);
  busyHolder = null;
  try {
    const controller = recorderController();
    const snapshot = await controller.start();
    recorderSnapshot.set(snapshot);
    const free = controller.freeSeconds;
    if (free !== null && free < 600) {
      toast("warning", text("recorder.low_disk", { t: formatElapsed(Math.floor(free) * 16000) }));
    }
  } catch (error) {
    recorderFailure.set({ kind: "start", reason: error instanceof Error ? error.message : String(error) });
  }
  recorderFocus.set(recorderViewNow().focus);
}

export function stopRecorder(): void {
  void recorderController().stop().then((snapshot) => {
    recorderSnapshot.set(snapshot);
    recorderFocus.set(recorderViewNow().focus);
  }).catch((error) => toast("error", `Recorder: ${error instanceof Error ? error.message : String(error)}`));
}

export async function recoverRecorder(): Promise<void> {
  try {
    const snapshot = await recorderController().recover();
    recorderSnapshot.set(snapshot);
    await refreshRecorder(false);
  } catch (error) {
    toast("error", `Recover unsaved audio: ${error instanceof Error ? error.message : String(error)}`);
  }
  recorderFocus.set(recorderViewNow().focus);
}

function closeRecorder(): void {
  recorderOpen.set(false);
  lastFocusedId = null;
  recorderPage.set("recorder");
  recorderQuitPrompt.set(false);
  if (openedFrom === "header") focusRecorderHeader.fn();
}

/** Esc/close while capturing hides the modal; otherwise it closes. Recent steps back first. */
export function minimizeRecorder(): void {
  if (recorderPage() === "recent") {
    recorderPage.set("recorder");
    recorderFocus.set(recorderViewNow().focus);
    return;
  }
  closeRecorder();
}

export function focusRecorderModal(): void {
  lastFocusedId = null;
  recorderFocus.set(recorderViewNow().focus);
}

/** The header button's focus handle, registered by the header. */
export const focusRecorderHeader: { fn: () => void } = { fn: () => {} };

export function currentRecorderTarget(): string | null {
  if (recorderPage() === "recent") {
    const row = recorderRecent()[recorderRecentSelected()];
    return row?.id ?? null;
  }
  return currentRecordingId();
}

export function askDeleteRecorder(): void {
  const id = currentRecorderTarget();
  if (!id) return;
  if (recorderIsActive()) {
    toast("info", "Stop the recording before deleting it.");
    return;
  }
  confirmDialog.set({
    title: text("recorder.delete.title"),
    body: text("recorder.delete.body"),
    confirm: text("recorder.delete.confirm"),
    destructive: true,
    run: async () => {
      try {
        await request("DELETE", `/api/recordings/${id}`);
        recorderRecent.update((rows) => rows.filter((row) => row.id !== id));
        if (recorderCore()?.id === id) recorderCore.set(null);
        if (recorderTranscript().recordingId === id) recorderTranscript.set(emptyTranscript());
        recorderRecentSelected.update((index) => Math.max(0, Math.min(index, recorderRecent().length - 1)));
        recorderFocus.set(recorderViewNow().focus);
      } catch (error) {
        const code = (error as { code?: string }).code;
        throw new Error(code === "recording_export_active" ? text("recorder.delete.busy") : error instanceof Error ? error.message : String(error));
      }
    },
  });
}

export async function copyRecorderTranscript(): Promise<void> {
  const id = currentRecordingId();
  if (!id) return;
  try {
    const body = await request<{ segments?: { status?: string; text?: string | null }[]; complete?: boolean }>("GET", `/api/recordings/${id}/transcript.json`);
    const parts = (body.segments ?? [])
      .filter((segment) => segment.status !== "skipped" && typeof segment.text === "string" && segment.text.length > 0)
      .map((segment) => segment.text!.trim())
      .filter(Boolean);
    await Clipboard.writeText(parts.join(" "));
    toast("success", body.complete === false ? text("recorder.toast.copied_partial") : text("recorder.toast.copied"));
  } catch (error) {
    toast("error", `Transcript: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export async function retryRecorderTranscription(): Promise<void> {
  const id = currentRecordingId();
  if (!id) return;
  try {
    await request("POST", `/api/recordings/${id}/transcript/retry`, { body: { scope: "failed" }, key: newKey() });
    toast("info", "Retry requested");
  } catch (error) {
    toast("error", `Retry transcription: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export async function showRecentRecordings(): Promise<void> {
  recorderOpen.set(true);
  recorderPage.set("recent");
  recorderRecentSelected.set(0);
  recorderFocus.set("recent.0");
  await refreshRecorder(true);
  recorderFocus.set(recorderViewNow().focus);
}

export async function openRecentRecording(index = recorderRecentSelected()): Promise<void> {
  const row = recorderRecent()[index];
  if (!row) return;
  recorderPage.set("recorder");
  recorderRecentSelected.set(index);
  recorderCore.set({ ...row });
  recorderTranscript.set(emptyTranscript(row.id, null));
  recorderFocus.set(recorderViewNow().focus);
  await refreshRecorder(false);
  recorderFocus.set(recorderViewNow().focus);
}

function selectRecent(delta: number): void {
  const rows = recorderRecent();
  if (!rows.length) return;
  const next = Math.max(0, Math.min(rows.length - 1, recorderRecentSelected() + delta));
  recorderRecentSelected.set(next);
  recorderFocus.set(`recent.${next}`);
}

// --- quit ----------------------------------------------------------------------------------

export function requestRecorderQuit(): void {
  if (!recorderOwnsCapture()) { Application.quit(); return; }
  recorderQuitPrompt.set(true);
}

export function cancelRecorderQuit(): void {
  recorderQuitPrompt.set(false);
  try { Application.cancelQuitRequest(); } catch {}
  focusRecorderModal();
}

export async function confirmRecorderQuit(): Promise<void> {
  if (!recorderOwnsCapture()) { Application.quit(); return; }
  recorderQuitPrompt.set(false);
  const stopping = recorderController().stop();
  await Promise.race([stopping.catch(() => undefined), new Promise((resolve) => setTimeout(resolve, 5_000))]);
  Application.quit();
}

// --- announcements -------------------------------------------------------------------------

let announceAt = 0;
let lastAnnouncement = "";
function announceRecorder(value: string, force = true): void {
  const now = Date.now();
  if (!force && now - announceAt < 3_000) return;
  if (!value || value === lastAnnouncement) return;
  announceAt = now;
  lastAnnouncement = value;
  recorderAnnouncement.set(value);
}

// --- drawing -------------------------------------------------------------------------------

function RecorderHeaderLabel() {
  return (
    <Text class={`${TS.base} font-medium`}>
      {() => {
        const view = recorderViewNow();
        if (view.header.startsWith("● ")) {
          return <><Text class="text-error">●</Text><Text class="text-foreground">{view.header.slice(1)}</Text></>;
        }
        const tone = view.header_tone === "error" ? "text-error" : view.header_tone === "warning" ? "text-warning" : "text-muted-foreground";
        return <Text class={tone}>{view.header}</Text>;
      }}
    </Text>
  );
}

/** The header entry, beside the right sidebar toggle (right of Manager). Desktop/web only; the phone shell has no header. */
export function RecorderHeaderButton(p: { onPress: () => void }) {
  const view = () => recorderViewNow();
  return (
    <Button onPress={p.onPress} testID="open-recorder" accessibilityLabel={view().a11y}
      class={`shrink-0 items-center justify-center rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "h-7"} hover:bg-accent ${FOCUS}`}>
      <RecorderHeaderLabel />
    </Button>
  );
}

const focusRefs = new Map<string, { focus?: () => void }>();
let lastFocusedId: string | null = null;

function focusable(id: string, ref: any) {
  if (ref) focusRefs.set(id, ref);
  else focusRefs.delete(id);
}

function effectiveFocus(view: RecorderView): string {
  const current = recorderFocus();
  if (current && view.button_ids.includes(current as any)) return current;
  if (current && current.startsWith("recent.") && view.state.page === "recent") {
    const index = Number(current.slice("recent.".length));
    if (Number.isInteger(index) && index >= 0 && index < (view.recent?.rows.length ?? 0)) return current;
  }
  if (current === "jump_live" && view.jump_live) return current;
  return view.focus;
}

function ActionRow(p: { view: RecorderView; focus: string; onJumpLive: () => void; startBlocked: string | null }) {
  const press = (id: string) => {
    switch (id) {
      case "recorder.start": void startRecorder(); return;
      case "recorder.minimize": minimizeRecorder(); return;
      case "recorder.stop": stopRecorder(); return;
      case "recorder.copy_transcript": void copyRecorderTranscript(); return;
      case "recorder.retry": void retryRecorderTranscription(); return;
      case "recorder.recover": void recoverRecorder(); return;
      case "recorder.recent": void showRecentRecordings(); return;
      case "recorder.delete": askDeleteRecorder(); return;
      default: return;
    }
  };
  return (
    <View class="flex-row flex-wrap items-center gap-2 pt-3" testID="recorder-actions">
      <Each of={p.view.buttons} key={(button) => button.id}>
        {(button) => {
          const b = () => button();
          const blocked = b().id === "recorder.start" ? p.startBlocked : null;
          return <Btn label={b().label} kind={b().id === p.view.buttons[0]?.id ? "primary" : "default"}
            disabled={!b().enabled || !!blocked} reason={blocked ?? b().reason ?? undefined}
            testID={b().id} initial={p.focus === b().id}
            ref={(node: any) => focusable(b().id, node)}
            onFocus={() => recorderFocus.set(b().id)}
            onPress={() => press(b().id)} />;
        }}
      </Each>
      <If when={p.view.jump_live}>
        <Btn label={recorderText("recorder.jump_live")} testID="jump_live" initial={p.focus === "jump_live"}
          ref={(node: any) => focusable("jump_live", node)}
          onFocus={() => recorderFocus.set("jump_live")}
          onPress={p.onJumpLive} />
      </If>
    </View>
  );
}

function StatusRow(p: { label: string; value: string | null; detail?: string | null; tone?: "normal" | "warning" | "muted"; children?: any }) {
  return (
    <View class="flex-row items-start gap-3 min-h-6" testID={`recorder-status-${p.label.toLowerCase()}`}>
      <Text class={`w-24 shrink-0 ${T.meta}`}>{p.label}</Text>
      <View class="flex-1 min-w-0">
        <Text class={`${TS.base} ${p.tone === "warning" ? "text-warning" : p.tone === "muted" ? "text-muted-foreground" : "text-foreground"}`}>{p.value ?? "—"}</Text>
        <If when={!!p.detail}><Text class={`${TS.base} text-muted-foreground`}>{p.detail!}</Text></If>
        {p.children}
      </View>
    </View>
  );
}

function LevelMeter(p: { level: number }) {
  const bars = () => Math.max(0, Math.min(8, Math.round(p.level * 8)));
  return (
    <View class="flex-row items-end gap-0.5 h-4" testID="recorder-level" accessibilityLabel={`Microphone level ${Math.round(p.level * 100)}%`}>
      <Each of={[0, 1, 2, 3, 4, 5, 6, 7]} key={(index) => index}>
        {(index) => <View class={`w-1 rounded-[1px] ${index() < bars() ? "bg-primary" : "bg-raised"}`} style={{ height: 4 + index() * 1.5 }} />}
      </Each>
    </View>
  );
}

function Transcript(p: { view: RecorderView; onScroll: (event: any) => void; scrollRef: (node: any) => void; top: number }) {
  return (
    <View class="flex-1 min-h-0 mt-3 border-t border-border pt-2" testID="recorder-transcript-wrap">
      <Scroll ref={p.scrollRef} onScroll={p.onScroll} contentOffset={{ y: p.top }}
        style={{ maxHeight: 280 }} testID="recorder-transcript">
        <View class="gap-1 pb-2">
          <If when={p.view.rows.length === 0 && p.view.state.ownership === "owner" && (p.view.state.capture_state === "recording" || p.view.state.capture_state === "starting")}>
            <Text class={`${T.meta} py-2`}>{text("recorder.transcript.listening")}</Text>
          </If>
          <Each of={p.view.rows} key={(row) => `${row.segmentId ?? row.kind}`}>
            {(row) => {
              const r = () => row();
              return (
                <View class="flex-row items-baseline gap-2">
                  <Text class={`${T.meta} shrink-0 w-24`} testID={`recorder-row-time-${r().segmentId ?? r().kind}`}>{r().endLabel ? `${r().startLabel}–${r().endLabel}` : `${r().startLabel}–`}</Text>
                  <Text class={`flex-1 min-w-0 ${TS.base} ${r().tone === "warning" ? "text-warning" : r().tone === "muted" ? "text-muted-foreground" : "text-foreground"} ${r().italic ? "italic" : ""}`}>{r().text}</Text>
                </View>
              );
            }}
          </Each>
        </View>
      </Scroll>
    </View>
  );
}

function RecentPage(p: { view: RecorderView; focus: string }) {
  return (
    <View class="gap-2 mt-3" testID="recorder-recent">
      <Text class={`${T.label}`}>{recorderText("recorder.action.recent")}</Text>
      <If when={!!p.view.recent?.empty}>
        <Text class={`${T.sec} py-2`}>{p.view.recent?.empty ?? ""}</Text>
      </If>
      <Scroll style={{ maxHeight: 300 }} testID="recorder-recent-list">
        <View class="gap-0.5">
          <Each of={p.view.recent?.rows ?? []} key={(row) => row.id}>
            {(row, index) => {
              const r = () => row();
              const i = () => index();
              const selected = () => p.focus === `recent.${i()}`;
              return (
                <View role="button" focusable testID={`recent.${i()}`}
                  accessibilityLabel={r().label}
                  class={`flex-row items-center rounded-(--radius) px-3 min-h-9 ${selected() ? "bg-row-selected" : "hover:bg-accent"} ${FOCUS}`}
                  ref={(node: any) => focusable(`recent.${i()}`, node)}
                  onFocus={() => { recorderRecentSelected.set(i()); recorderFocus.set(`recent.${i()}`); }}
                  onPress={() => { recorderRecentSelected.set(i()); void openRecentRecording(i()); }}>
                  <Text class={`${TS.base} ${selected() ? "text-foreground" : "text-foreground-secondary"}`} style={{ numberOfLines: 1 }}>{r().label}</Text>
                </View>
              );
            }}
          </Each>
        </View>
      </Scroll>
      <If when={!!p.view.recent?.notice}><Text class={T.meta}>{p.view.recent?.notice ?? ""}</Text></If>
    </View>
  );
}

function RecorderBody(p: { view: RecorderView; focus: string; onScroll: (event: any) => void; scrollRef: (node: any) => void; top: number; onJumpLive: () => void; startBlocked: string | null; startupNotice: string | null }) {
  return (
    <View class="gap-3 min-h-0">
      <ModalTitle title={recorderText("recorder.title")} />
      <If when={!!p.view.source}><Text class={T.sec} testID="recorder-source">{p.view.source ?? ""}</Text></If>
      <If when={p.view.state.page === "recent"} else={
        <View class="gap-1.5">
          <View class="flex-row items-center gap-3">
            <View class="flex-1 min-w-0">
              <StatusRow label="Capture" value={p.view.capture} />
            </View>
            <If when={p.view.state.capture_state === "recording" && p.view.state.ownership === "owner"}>
              <LevelMeter level={p.view.state.level} />
            </If>
          </View>
          <StatusRow label="Audio" value={p.view.audio} detail={p.view.audio_detail} tone={p.view.audio_warning ? "warning" : "normal"}>
            <If when={!!p.view.audio_warning}><Text class={`${TS.base} text-warning`}>{p.view.audio_warning!}</Text></If>
          </StatusRow>
          <StatusRow label="Transcript" value={p.view.transcript_status} detail={p.view.transcript_detail}
            tone={p.view.transcript_status?.startsWith(recorderText("recorder.transcript.unavailable")) ? "warning" : "normal"} />
        </View>
      }>
        <RecentPage view={p.view} focus={p.focus} />
      </If>
      <If when={p.view.state.page === "recorder"}>
        <Transcript view={p.view} onScroll={p.onScroll} scrollRef={p.scrollRef} top={p.top} />
      </If>
      <If when={!!p.startupNotice}>
        <Text testID="recorder-startup-notice" accessibilityLiveRegion="polite" class={`${TS.base} text-warning`}>{p.startupNotice!}</Text>
      </If>
      <ActionRow view={p.view} focus={p.focus} onJumpLive={p.onJumpLive} startBlocked={p.startBlocked} />
      <ModalFooter hints={p.view.hint} />
    </View>
  );
}

/** The large recorder modal (idle, recording, stopped, interrupted, Recent). */
export function RecorderModal() {
  const view = () => recorderViewNow();
  const scrollNode = signal<any>(null);
  let lastY = 0;

  void effect(() => { void loadRecorderTestSeam(); });
  void effect(() => { void recoverRecorderAtLaunch(); });
  void effect(() => {
    recorderHooks.event = applyRecorderGlobalEvent;
    recorderHooks.connected = () => {
      void (async () => {
        if (ownerActive()) await recorderController().reconnect();
        if (recorderOpen() || recorderIsActive()) await refreshRecorder(recorderOpen());
      })();
    };
    recorderHooks.closed = () => {};
  });
  void effect(() => {
    let off: (() => void) | null = null;
    try {
      off = Application.on("quitRequest", () => {
        if (!recorderOwnsCapture()) { Application.quit(); return; }
        // On the web, leaving the page is the browser's generic confirmation (approved).
        if (platform.os === "web") return;
        recorderQuitPrompt.set(true);
      });
    } catch { /* an embedded view leaves quitting to its host */ }
    return () => { try { off?.(); } catch {} };
  });
  void effect(() => {
    refreshSource();
  });

  // A delete confirmation closes on Cancel without running its action: put
  // focus back on the Recent row it came from, as the TUI does.
  let confirmWasOpen = recorderDeleteWasOpen;
  void effect(() => {
    const open = !!confirmDialog();
    if (confirmWasOpen && !open && recorderOpen() && recorderPage() === "recent") focusRecorderModal();
    confirmWasOpen = open;
    recorderDeleteWasOpen = open;
  });

  const focus = () => effectiveFocus(view());
  void effect(() => {
    const id = focus();
    if (!recorderOpen() || recorderQuitPrompt()) return;
    if (id === lastFocusedId) return;
    lastFocusedId = id;
    const node = focusRefs.get(id);
    try { node?.focus?.(); } catch { /* the target may be outside a focusable host */ }
  });

  // Announce transitions and final segments; never provisional text or the tick.
  let lastPhase = recorderSnapshot()?.phase ?? "idle";
  let lastCoreState = recorderCore()?.capture_state ?? "";
  void effect(() => {
    const snapshot = recorderSnapshot();
    const phase = snapshot?.phase ?? "idle";
    const coreState = String(recorderCore()?.capture_state ?? "");
    if (phase !== lastPhase) {
      if ((phase === "starting" || phase === "recording") && lastPhase === "idle") announceRecorder(text("recorder.announce.started"));
      if (phase === "stopped") announceRecorder(text("recorder.announce.stopped"));
      if (phase === "interrupted" || phase === "failed") announceRecorder(text("recorder.announce.interrupted"));
      lastPhase = phase;
    }
    if (coreState !== lastCoreState) {
      if (coreState === "stopped" && lastCoreState !== "") announceRecorder(text("recorder.announce.stopped"));
      if (coreState === "interrupted" || coreState === "failed") announceRecorder(text("recorder.announce.interrupted"));
      lastCoreState = coreState;
    }
    const rows = view().rows;
    const finalRow = [...rows].reverse().find((row) => row.kind === "final");
    if (finalRow?.text) announceRecorder(finalRow.text, false);
  });

  const onScroll = (event: any) => {
    const offset = event.offset ?? { x: 0, y: 0 };
    const contentH = event.contentSize?.height ?? 0;
    const viewH = event.size?.height ?? 0;
    const gap = Math.max(0, contentH - (offset.y + viewH));
    const next = followsAfterScroll({ y: offset.y, lastY, gap, following: recorderFollow(), contentH, viewH });
    lastY = offset.y;
    if (next !== recorderFollow()) recorderFollow.set(next);
    const id = currentRecordingId();
    if (id) recorderScroll.update((all) => ({ ...all, [id]: offset.y }));
  };

  const jumpToLive = () => {
    recorderFollow.set(true);
    const id = currentRecordingId();
    if (id) recorderScroll.update((all) => ({ ...all, [id]: 0 }));
    try { scrollNode()?.scrollToEnd?.({ animated: false }); } catch {}
  };

  const scrollRef = (node: any) => {
    scrollNode.set(node);
    if (!node) return;
    const id = currentRecordingId();
    const saved = id ? recorderScroll()[id] ?? 0 : 0;
    if (!recorderFollow() && saved > 0) {
      try { node.scrollTo({ y: saved, animated: false }); } catch {}
    }
  };

  let wasFollowing = recorderFollow();
  void effect(() => {
    const following = recorderFollow();
    if (following && !wasFollowing && recorderOpen() && recorderPage() === "recorder") {
      queueMicrotask(() => { try { scrollNode()?.scrollToEnd?.({ animated: false }); } catch {} });
    }
    wasFollowing = following;
  });

  // Auto-follow only at the bottom. A new final segment never yanks a reader.
  void effect(() => {
    if (!recorderOpen() || recorderPage() !== "recorder") return;
    const rows = view().rows;
    rows.length;
    rows[rows.length - 1]?.text;
    if (!recorderFollow()) return;
    queueMicrotask(() => { try { scrollNode()?.scrollToEnd?.({ animated: false }); } catch {} });
  });

  // Keys inside the modal. Esc is the manifest's recorder rule (minimise or close).
  void effect(() => {
    if (!recorderOpen() || recorderQuitPrompt()) return;
    const offs: (() => void)[] = [];
    const bind = (key: string, fn: () => void) => {
      try { offs.push(keymap.bind(key, fn, { when: "always" })); } catch { /* the platform keeps it */ }
    };
    if (recorderPage() === "recent") {
      bind("down", () => selectRecent(1));
      bind("up", () => selectRecent(-1));
      bind("enter", () => void openRecentRecording());
      bind("delete", () => askDeleteRecorder());
      bind("backspace", () => askDeleteRecorder());
    } else {
      const by = (delta: number) => () => {
        const id = currentRecordingId();
        const at = id ? recorderScroll()[id] ?? lastY : lastY;
        const y = Math.max(0, at + delta);
        lastY = y;
        if (id) recorderScroll.update((all) => ({ ...all, [id]: y }));
        try { scrollNode()?.scrollTo({ y, animated: false }); } catch {}
      };
      bind("down", by(80));
      bind("up", by(-80));
      bind("pagedown", by(300));
      bind("pageup", by(-300));
      bind("home", () => { lastY = 0; try { scrollNode()?.scrollTo({ y: 0, animated: false }); } catch {} });
      bind("end", () => { recorderFollow.set(true); try { scrollNode()?.scrollToEnd?.({ animated: false }); } catch {} });
    }
    return () => offs.forEach((off) => off());
  });

  const top = () => {
    const id = currentRecordingId();
    return id ? recorderScroll()[id] ?? 0 : 0;
  };

  return (
    <>
      <If when={recorderOpen()}>
        <Modal size="large" label={recorderText("recorder.title")} testID="recorder-modal" onCancel={minimizeRecorder} closeOnBackdropPress={false}>
          <View role="dialog" accessibilityLabel={recorderText("recorder.title")} testID="recorder-dialog" class="min-h-0 flex-col">
            <RecorderBody view={view()} focus={focus()} onScroll={onScroll} scrollRef={scrollRef} top={top()} onJumpLive={jumpToLive} startBlocked={recorderStartBlockReason()} startupNotice={recorderStartup().recovering ? RECOVERY_RUNNING : recorderStartup().error ?? recorderStartup().storageReason} />
          </View>
        </Modal>
      </If>
      <If when={recorderQuitPrompt()}>
        <Modal size="small" label={text("recorder.quit.title")} testID="recorder-quit-dialog" onCancel={cancelRecorderQuit} closeOnBackdropPress={false}>
          <View role="alertdialog" accessibilityLabel={text("recorder.quit.title")} class="gap-3">
            <ModalTitle title={text("recorder.quit.title")} />
            <Text class={T.sec}>{text("recorder.quit.body")}</Text>
            <View class="flex-row justify-end gap-2">
              <Btn label={text("recorder.quit.cancel")} initial testID="keep_recording"
                ref={(node: any) => focusable("keep_recording", node)}
                onPress={cancelRecorderQuit} />
              <Btn label={text("recorder.quit.confirm")} kind="danger" testID="stop_and_exit"
                onPress={() => void confirmRecorderQuit()} />
            </View>
          </View>
        </Modal>
      </If>
      <Text testID="recorder-live" accessibilityLiveRegion="polite"
        style={{ position: "absolute", left: -9999, width: 1, height: 1, overflow: "hidden" }}>{recorderAnnouncement()}</Text>
    </>
  );
}
