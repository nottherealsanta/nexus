// Recorder capture controller for NSH web (plan 54, D-NSH-web-cap).
//
// This is deliberately separate from dictation: construction touches no
// microphone, device or network. `start()` is the only path that opens the
// microphone, and `stop()` releases it before draining the bounded spool.
//
// Audio callbacks only resample, chunk and enqueue. Uploads are sequential,
// idempotent retries of the same sequence/start/bytes/hash. The public state
// distinguishes acknowledged ("saved to Core") samples from pending and
// explicitly lost samples; it never labels unacknowledged audio as saved.

import { StreamingResampler, rmsLevel } from "./audio-capture-logic";
import { openRecorder } from "./audio-capture";
import type { CaptureRecorder, OpenCapture } from "./audio-capture";
import { apiUrl, request } from "./core/transport";
import {
  CaptureGapTracker,
  DEFAULT_MAX_UPLOAD_ATTEMPTS,
  DEFAULT_RETRY_DELAYS_MS,
  RECORDER_GAP_THRESHOLD_MS,
  RECORDER_HEARTBEAT_MS,
  RECORDER_MEMORY_ONLY_WARNING,
  RECORDER_SAMPLE_RATE,
  RECORDER_SPOOL_SAMPLES,
  RecorderChunker,
  classifyUploadFailure,
  parseRetryAfter,
  retryDelay,
  secondsForSamples,
  spoolFits,
} from "./recorder-logic";
import type { PcmChunk, RecorderFailure, RecorderGap, RecorderLoss, RecorderPhase } from "./recorder-logic";

export interface RecorderPublicState {
  id: string;
  generation?: number;
  durable_offset?: number;
  final_sample_count?: number | null;
  capture_state?: string;
  active?: boolean;
  sealed?: boolean;
  complete?: boolean;
  holes?: number;
  [key: string]: unknown;
}

export interface RecorderCreatedState extends RecorderPublicState {
  lease_token?: string | null;
  lease?: string | null;
  recovery_token?: string | null;
  free_seconds?: number | null;
}

export interface RecorderApi {
  create(input: {
    streamId?: string; clientId: string; idempotencyKey: string; title?: string;
    recovery: string; mode?: "create" | "resume";
  }): Promise<RecorderCreatedState>;
  get(recordingId: string): Promise<RecorderPublicState>;
  putChunk(input: {
    recordingId: string; lease: string; streamId: string | null;
    seq: number; start: number; body: Uint8Array; sha256: string;
  }): Promise<{ recording: RecorderPublicState }>;
  heartbeat(input: { recordingId: string; lease: string; streamId: string | null }): Promise<RecorderPublicState>;
  rebind(input: {
    recordingId: string; lease: string; streamId: string; clientId: string;
  }): Promise<RecorderPublicState>;
  stop(input: {
    recordingId: string; lease: string; streamId: string | null; finalSampleCount: number;
  }): Promise<RecorderPublicState>;
  recoverStop(input: {
    recordingId: string; recoveryToken: string; clientId: string;
    streamId: string | null; finalSampleCount: number;
  }): Promise<RecorderPublicState>;
  recoverChunk(input: {
    recordingId: string; recoveryToken: string; clientId: string; streamId: string | null;
    seq: number; start: number; body: Uint8Array; sha256: string;
  }): Promise<{ recording: RecorderPublicState }>;
}

export interface RecorderSpoolEntry {
  recording: string;
  seq: number;
  start: number;
  samples: number;
  bytes: Uint8Array;
  sha256: string;
}

export type RecorderSpoolGuarantee = "persistent" | "best-effort" | "memory" | "none";

/** Local recovery capability. The token is only written to the local spool, never to a snapshot or log. */
export interface RecorderRecoveryRecord {
  recording: string;
  generation: number | null;
  clientId: string;
  recoveryToken: string;
  finalSampleCount: number | null;
  captureLostSamples?: number;
  discardedSamples?: number;
  discardedLossSamples?: number;
  losses?: RecorderLoss[];
}

/** Durable (or memory-only) local copy of chunks that Core has not acknowledged. */
export interface RecorderSpool {
  /** True only when the browser granted persistent storage; best-effort IndexedDB may be evicted. */
  readonly durable: boolean;
  readonly guarantee: RecorderSpoolGuarantee;
  readonly warning: string | null;
  put(entry: RecorderSpoolEntry): Promise<void> | void;
  remove(seq: number): Promise<void> | void;
  clear(): Promise<void> | void;
  /** Bounded restore: entries in sequence order, stopping before the sample budget is exceeded. */
  load(limitSamples?: number): Promise<RecorderSpoolEntry[]> | RecorderSpoolEntry[];
  loadRecovery(): Promise<RecorderRecoveryRecord | null> | RecorderRecoveryRecord | null;
  saveRecovery(record: RecorderRecoveryRecord): Promise<void> | void;
  clearRecovery(): Promise<void> | void;
}

export type RecorderSpoolFactory = (recordingId: string) => Promise<RecorderSpool> | RecorderSpool;

/** A create intent written before the POST so a crash can resume it without opening the microphone. */
export interface PendingRecorderStart {
  key: string;
  recovery: string;
  clientId: string;
  title: string | null;
  createdAt: number;
  state: "creating" | "bound";
  recordingId: string | null;
  attempts?: number;
}

export interface RecorderPendingStore {
  readonly durable: boolean;
  readonly guarantee: RecorderSpoolGuarantee;
  readonly warning: string | null;
  /** Web Locks are required for safe cross-tab recovery; absent means age-based fallback only. */
  readonly crossTabLocks: boolean;
  put(record: PendingRecorderStart): Promise<void> | void;
  delete(key: string): Promise<void> | void;
  list(): Promise<PendingRecorderStart[]> | PendingRecorderStart[];
  /** Run under a cross-tab lock. Undefined means another tab holds it. */
  withLock<T>(key: string, run: () => Promise<T>): Promise<T | undefined>;
}

export type RecorderPendingStoreFactory = () => Promise<RecorderPendingStore> | RecorderPendingStore;

/** Public, token-free metadata for a locally recoverable spool. */
export interface RecoverableSpoolSummary {
  recordingId: string;
  generation: number | null;
  finalSampleCount: number | null;
  captureLostSamples: number;
  discardedSamples: number;
}

export interface RecoverableSpoolIndex {
  readonly crossTabLocks: boolean;
  list(limit?: number): Promise<RecoverableSpoolSummary[]> | RecoverableSpoolSummary[];
  /** Per-recording lock; undefined means another tab is already touching this spool. */
  withLock<T>(recordingId: string, run: () => Promise<T>): Promise<T | undefined>;
}

export type RecoverableSpoolIndexFactory = () => Promise<RecoverableSpoolIndex> | RecoverableSpoolIndex;

export interface RecoverableSpoolOutcome {
  recordingId: string;
  outcome: "recovered" | "cleared" | "skipped_active" | "skipped_locked" | "failed";
  complete?: boolean;
  code?: string;
}

export interface PendingRecoveryOutcome {
  key: string;
  outcome: "deleted" | "bound" | "sealed" | "stopped" | "kept" | "skipped" | "failed";
  recordingId?: string;
  code?: string;
  /** Core's bounded withdrawn-key guard is full; only a Core restart can clear it. */
  restartRequired?: boolean;
}

export type RecorderTimers = {
  setTimeout(handler: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(handler: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
};

export interface RecorderSnapshot {
  phase: RecorderPhase;
  failure: RecorderFailure | null;
  recordingId: string | null;
  generation: number | null;
  lease: "none" | "active" | "lost";
  sampleRate: number;
  /** Current microphone level, 0..1. Zero while idle/stopped. */
  level: number;
  capturedSamples: number;
  captureLostSamples: number;
  discardedSamples: number;
  discardedLossSamples: number;
  ackedSamples: number;
  pendingSamples: number;
  unsavedSamples: number;
  capturedSeconds: number;
  discardedSeconds: number;
  savedSeconds: number;
  pendingSeconds: number;
  unsavedSeconds: number;
  finalSampleCount: number | null;
  sealed: boolean;
  serverComplete: boolean;
  complete: boolean;
  lossless: boolean;
  holes: number;
  persistentSpool: boolean;
  spoolGuarantee: RecorderSpoolGuarantee;
  storageWarning: string | null;
  recoverable: boolean;
  gaps: RecorderGap[];
  losses: RecorderLoss[];
  retry: { sequence: number; attempt: number; delayMs: number } | null;
  lastError: string | null;
}

/** A hole the capture device reports directly, in native/recording sample positions. */
export interface RecorderCaptureLoss {
  /** Native adapters may omit this; the controller then uses the current capture offset. */
  start?: number;
  /** Native end-tail losses may not know the end or the exact sample count. */
  end?: number | null;
  samples?: number | null;
  /** Both the older `reason` spelling and the current `kind` spelling are accepted. */
  reason?: string;
  kind?: string;
}

/** A capture read/device failure. Native adapters report it as soon as it happens. */
export interface RecorderCaptureFailure {
  stage?: string;
  message?: string;
}

export interface RecorderCaptureHandlers {
  onLoss?(loss: RecorderCaptureLoss): void;
  onError?(failure: RecorderCaptureFailure): void;
}

export interface RecorderDependencies {
  openMicrophone(
    onSamples: (samples: Float32Array) => void,
    handlers?: RecorderCaptureHandlers,
  ): Promise<OpenCapture>;
  api: RecorderApi;
  digest(bytes: Uint8Array): Promise<string>;
  spool: RecorderSpoolFactory;
  pendingStarts?: RecorderPendingStoreFactory;
  recoverableSpools?: RecoverableSpoolIndexFactory;
  randomBytes?: (length: number) => Uint8Array;
  streamId(): string | null;
  clientId: string;
  title?: string;
  now?: () => number;
  newKey?: () => string;
  heartbeatMs?: number;
  spoolLimitSamples?: number;
  retryDelaysMs?: number[];
  maxUploadAttempts?: number;
  gapThresholdMs?: number;
  timers?: RecorderTimers;
  onChange?: (snapshot: RecorderSnapshot) => void;
}

type QueuedChunk = PcmChunk & { sha256: string | null; persisted: boolean; attempts: number };
type FailureInfo = { status?: number; code?: string; message?: string; retryAfterMs?: number | null };

const GLOBAL_TIMERS: RecorderTimers = {
  setTimeout: (handler, ms) => globalThis.setTimeout(handler, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  setInterval: (handler, ms) => globalThis.setInterval(handler, ms),
  clearInterval: (handle) => globalThis.clearInterval(handle as ReturnType<typeof setInterval>),
};

const BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/** Browser-safe base64url without padding. */
export function base64url(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i];
    const b = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const c = i + 2 < bytes.length ? bytes[i + 2] : 0;
    out += BASE64URL[a >> 2];
    out += BASE64URL[((a & 3) << 4) | (b >> 4)];
    if (i + 1 < bytes.length) out += BASE64URL[((b & 15) << 2) | (c >> 6)];
    if (i + 2 < bytes.length) out += BASE64URL[c & 63];
  }
  return out;
}

export function secureRandomBytes(length: number): Uint8Array {
  const crypto = (globalThis as unknown as {
    crypto?: { getRandomValues?: (array: Uint8Array) => Uint8Array };
  }).crypto;
  if (!crypto?.getRandomValues) throw new Error("A secure random source is not available in this client");
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return bytes;
}

/** One CSPRNG key per user action; retries of that action reuse it. */
export function newRecorderKey(): string {
  return `nsh-rec-${base64url(secureRandomBytes(18))}`;
}

function failureInfo(error: unknown): FailureInfo {
  if (!error || typeof error !== "object") return { message: String(error) };
  const value = error as { status?: unknown; code?: unknown; message?: unknown; retryAfterMs?: unknown };
  return {
    status: typeof value.status === "number" ? value.status : undefined,
    code: typeof value.code === "string" ? value.code : undefined,
    message: typeof value.message === "string" ? value.message : undefined,
    retryAfterMs: typeof value.retryAfterMs === "number" ? value.retryAfterMs : null,
  };
}

function errorLabel(info: FailureInfo): string {
  if (info.code) return info.code;
  if (info.status !== undefined) return `HTTP ${info.status}`;
  return info.message ?? "network error";
}

/** A bounded, non-persistent spool. Used when IndexedDB/quota cannot hold pending audio. */
export function memoryRecorderSpool(warning: string | null = RECORDER_MEMORY_ONLY_WARNING): RecorderSpool {
  const entries = new Map<number, RecorderSpoolEntry>();
  let recovery: RecorderRecoveryRecord | null = null;
  return {
    durable: false,
    guarantee: "memory",
    warning,
    put(entry) { entries.set(entry.seq, entry); },
    remove(seq) { entries.delete(seq); },
    clear() { entries.clear(); },
    load(limitSamples = Number.MAX_SAFE_INTEGER) {
      const out: RecorderSpoolEntry[] = [];
      let used = 0;
      for (const entry of [...entries.values()].sort((a, b) => a.seq - b.seq)) {
        if (used + entry.samples > limitSamples) break;
        out.push(entry);
        used += entry.samples;
      }
      return out;
    },
    loadRecovery() { return recovery ? { ...recovery } : null; },
    saveRecovery(record) { recovery = { ...record }; },
    clearRecovery() { recovery = null; },
  };
}

/** Memory-only create intents: honest fallback, but cannot close a process crash. */
export function memoryPendingStore(
  warning = RECORDER_MEMORY_ONLY_WARNING,
  guarantee: RecorderSpoolGuarantee = "memory",
): RecorderPendingStore {
  const records = new Map<string, PendingRecorderStart>();
  return {
    durable: false,
    guarantee,
    warning,
    crossTabLocks: false,
    put(record) { records.set(record.key, { ...record }); },
    delete(key) { records.delete(key); },
    list() { return [...records.values()].map((record) => ({ ...record })); },
    async withLock<T>(_key: string, run: () => Promise<T>) { return run(); },
  };
}

/** No-op discovery fallback; persistent builds provide the IndexedDB index. */
export function memoryRecoveryIndex(): RecoverableSpoolIndex {
  return {
    crossTabLocks: false,
    list() { return []; },
    async withLock<T>(_recordingId: string, run: () => Promise<T>) { return run(); },
  };
}

export class RecorderController {
  private readonly deps: RecorderDependencies;
  private readonly timers: RecorderTimers;
  private readonly now: () => number;
  private readonly newKey: () => string;
  private readonly randomBytes: (length: number) => Uint8Array;
  private readonly pendingFactory: RecorderPendingStoreFactory;
  private readonly recoveryIndexFactory: RecoverableSpoolIndexFactory;
  private readonly heartbeatMs: number;
  private readonly spoolLimitSamples: number;
  private readonly retryDelaysMs: number[];
  private readonly maxUploadAttempts: number;
  private readonly gapThresholdMs: number;

  private phase: RecorderPhase = "idle";
  private failure: RecorderFailure | null = null;
  private lastError: string | null = null;
  private run = 0;
  private recordingId: string | null = null;
  private generation: number | null = null;
  private leaseToken: string | null = null;
  private recoveryToken: string | null = null;
  private boundStreamId: string | null = null;
  private eventRevision = -1;
  private recoverable = false;
  private mic: CaptureRecorder | null = null;
  private resampler: StreamingResampler | null = null;
  private chunker = new RecorderChunker();
  private gapTracker: CaptureGapTracker | null = null;
  private spool: RecorderSpool | null = null;
  private pendingStore: RecorderPendingStore | null = null;
  private recoveryIndex: RecoverableSpoolIndex | null = null;
  private pendingKey: string | null = null;
  private persistentSpool = false;
  private storageWarning: string | null = null;
  /** Core's free recording space reported by the last create, in seconds. */
  freeSeconds: number | null = null;
  private storageFaulted = false;

  private accepting = false;
  private releasing = false;
  private stopRequested: string | null = null;
  private stopTask: Promise<RecorderSnapshot> | null = null;
  private stopWork: Promise<RecorderSnapshot> | null = null;
  private stopWaiters: ((snapshot: RecorderSnapshot) => void)[] = [];

  private queue: QueuedChunk[] = [];
  private queuedSamples = 0;
  private pumping: Promise<void> | null = null;
  private uploadFatal = false;
  private leaseLost = false;
  private heartbeatTimer: unknown = null;
  private heartbeatBusy = false;
  private rebindWork: Promise<boolean> | null = null;

  private level = 0;
  private capturedSamples = 0;
  private captureLostSamples = 0;
  private discardedSamples = 0;
  private discardedLossSamples = 0;
  private captureCut = false;
  private captureCutEndpoint: number | null = null;
  private ackedSamples = 0;
  private unsavedSamples = 0;
  private finalSampleCount: number | null = null;
  private sealed = false;
  private serverComplete = false;
  private holes = 0;
  private gaps: RecorderGap[] = [];
  private losses: RecorderLoss[] = [];
  private retry: { sequence: number; attempt: number; delayMs: number } | null = null;

  constructor(dependencies: RecorderDependencies) {
    this.deps = dependencies;
    this.timers = dependencies.timers ?? GLOBAL_TIMERS;
    this.now = dependencies.now ?? Date.now;
    this.newKey = dependencies.newKey ?? newRecorderKey;
    this.randomBytes = dependencies.randomBytes ?? secureRandomBytes;
    this.pendingFactory = dependencies.pendingStarts ?? (() => memoryPendingStore());
    this.recoveryIndexFactory = dependencies.recoverableSpools ?? (() => memoryRecoveryIndex());
    this.heartbeatMs = dependencies.heartbeatMs ?? RECORDER_HEARTBEAT_MS;
    this.spoolLimitSamples = dependencies.spoolLimitSamples ?? RECORDER_SPOOL_SAMPLES;
    this.retryDelaysMs = dependencies.retryDelaysMs ?? DEFAULT_RETRY_DELAYS_MS;
    this.maxUploadAttempts = dependencies.maxUploadAttempts ?? DEFAULT_MAX_UPLOAD_ATTEMPTS;
    this.gapThresholdMs = dependencies.gapThresholdMs ?? RECORDER_GAP_THRESHOLD_MS;
  }

  /** The current truth, for the UI and tests. Construction returns `idle` without a microphone. */
  snapshot(): RecorderSnapshot {
    const complete = this.failure === null && this.serverComplete && this.unsavedSamples === 0
      && this.losses.length === 0 && this.gaps.length === 0;
    return {
      phase: this.phase,
      failure: this.failure,
      recordingId: this.recordingId,
      generation: this.generation,
      lease: this.leaseLost ? "lost" : this.leaseToken ? "active" : "none",
      sampleRate: RECORDER_SAMPLE_RATE,
      level: this.level,
      capturedSamples: this.capturedSamples,
      captureLostSamples: this.captureLostSamples,
      discardedSamples: this.discardedSamples,
      discardedLossSamples: this.discardedLossSamples,
      ackedSamples: this.ackedSamples,
      pendingSamples: this.queuedSamples,
      unsavedSamples: this.unsavedSamples,
      capturedSeconds: secondsForSamples(this.capturedSamples),
      discardedSeconds: secondsForSamples(this.discardedSamples),
      savedSeconds: secondsForSamples(this.ackedSamples),
      pendingSeconds: secondsForSamples(this.queuedSamples),
      unsavedSeconds: secondsForSamples(this.unsavedSamples),
      finalSampleCount: this.finalSampleCount,
      sealed: this.sealed,
      serverComplete: this.serverComplete,
      complete,
      lossless: this.failure === null && this.unsavedSamples === 0
        && this.losses.length === 0 && this.gaps.length === 0,
      holes: this.holes,
      persistentSpool: this.persistentSpool,
      spoolGuarantee: this.spool?.guarantee ?? "none",
      storageWarning: this.storageWarning,
      recoverable: this.recoverable && this.failure !== "deleted",
      gaps: this.gaps.slice(),
      losses: this.losses.slice(),
      retry: this.retry,
      lastError: this.lastError,
    };
  }

  /**
   * Explicit user action: open the microphone and start a Core capture.
   * Creating the controller (or opening a recorder modal) never does this.
   */
  async start(): Promise<RecorderSnapshot> {
    if (this.phase === "starting" || this.phase === "recording" || this.phase === "stopping") {
      throw new Error("A recording is already in progress");
    }
    const streamId = this.deps.streamId();
    if (!streamId) {
      this.phase = "failed";
      this.failure = "create";
      this.lastError = "the event stream is not connected";
      this.publish();
      return this.snapshot();
    }

    this.resetForStart();
    const token = this.run;
    this.phase = "starting";
    this.publish();

    // Persist K and client recovery R before any Core call. A crash after this
    // point can resume the create without opening a microphone.
    let identity: { key: string; recovery: string };
    try {
      identity = { key: this.newKey(), recovery: base64url(this.randomBytes(32)) };
      this.recoveryToken = identity.recovery;
      this.pendingKey = identity.key;
      this.pendingStore = await this.pendingFactory();
      this.storageWarning = this.pendingStore.warning;
      if (this.pendingStore.guarantee !== "persistent") {
        throw new Error(this.pendingStore.warning
          ?? "Persistent browser storage is required for the recorder");
      }
    } catch (error) {
      if (token !== this.run) return this.snapshot();
      this.failure = "storage";
      this.lastError = failureInfo(error).message ?? "persistent local storage is required";
      this.phase = "failed";
      this.publish();
      return this.snapshot();
    }

    // The cross-tab lock is held before the pending record becomes visible and
    // through create, durable spool binding and pending deletion. Recovery in
    // another tab therefore cannot withdraw a live intent or stop live capture.
    let readyForMic = false;
    try {
      const bound = await this.pendingStore.withLock(identity.key, async () => {
        try {
          await this.pendingStore!.put({
            key: identity.key,
            recovery: identity.recovery,
            clientId: this.deps.clientId,
            title: this.deps.title ?? null,
            createdAt: Date.now(),
            state: "creating",
            recordingId: null,
          });
        } catch (error) {
          const tagged = error instanceof Error ? error : new Error(String(error));
          (tagged as Error & { code?: string }).code = "storage";
          throw tagged;
        }
        if (token !== this.run) return null;
        const created = await this.deps.api.create({
          streamId,
          clientId: this.deps.clientId,
          idempotencyKey: identity.key,
          recovery: identity.recovery,
          mode: "create",
          ...(this.deps.title ? { title: this.deps.title } : {}),
        });
        if (token !== this.run) return null;
        if (created.recovery !== "client") {
          throw new Error("Core did not accept the client recovery capability");
        }
        this.recordingId = created.id;
        this.generation = typeof created.generation === "number" ? created.generation : 1;
        this.leaseToken = created.lease_token ?? created.lease ?? null;
        this.boundStreamId = streamId;
        this.freeSeconds = typeof created.free_seconds === "number" ? created.free_seconds : null;
        this.acceptState(created);

        try {
          this.spool = await this.deps.spool(created.id);
        } catch {
          this.spool = memoryRecorderSpool();
        }
        this.persistentSpool = this.spool.durable;
        this.storageWarning = this.spool.warning ?? this.storageWarning;
        if (this.spool.guarantee !== "persistent") {
          await this.settleEmptyRecording(token, "storage",
            "Persistent browser storage is required for the recorder; the microphone was not opened");
          return null;
        }
        if (this.recoveryToken) {
          this.recoverable = true;
          await this.saveRecoveryMetadata(null);
        }
        if (!this.recoverable) {
          await this.settleEmptyRecording(token, "storage",
            "The recovery capability could not be stored locally; the microphone was not opened");
          return null;
        }
        try {
          await this.pendingStore!.delete(identity.key);
          this.pendingKey = null;
        } catch {
          // The pending record remains; recovery will see the spool and remove it.
        }

        if (this.leaseLost || token !== this.run) {
          this.phase = "interrupted";
          this.publish();
          this.resolveStopWaiters();
          return null;
        }
        if (!this.leaseToken) {
          this.failure = "create";
          this.lastError = "the recording is not active";
          this.phase = created.active === false ? "interrupted" : "failed";
          this.publish();
          this.resolveStopWaiters();
          return null;
        }
        if (this.stopRequested) {
          this.phase = "stopping";
          this.publish();
          void this.finishStop(this.stopRequested, token);
          return null;
        }
        readyForMic = true;
        return created;
      });
      if (!bound) return this.snapshot();
    } catch (error) {
      if (token !== this.run) return this.snapshot();
      const info = failureInfo(error);
      this.failure = info.code === "storage" ? "storage" : "create";
      this.lastError = info.code === "create_guard_full"
        ? "Core must be restarted before recording can start (create guard full)"
        : info.message ?? "the recording could not be created";
      this.phase = "failed";
      this.publish();
      this.resolveStopWaiters();
      return this.snapshot();
    }
    if (!readyForMic) return this.snapshot();

    let opened: OpenCapture;
    try {
      opened = await this.deps.openMicrophone(
        (samples) => this.receive(samples, token),
        {
          onLoss: (loss) => this.captureLoss(loss, token),
          onError: (failure) => this.captureFailure(failure, token),
        },
      );
    } catch (error) {
      if (token !== this.run) return this.snapshot();
      await this.settleEmptyRecording(token, "microphone",
        failureInfo(error).message ?? "the microphone could not be opened");
      return this.snapshot();
    }
    if (token !== this.run) {
      await this.releaseCapture(opened.recorder);
      return this.snapshot();
    }
    if (this.stopRequested) {
      await this.releaseCapture(opened.recorder);
      this.phase = "stopping";
      this.publish();
      void this.finishStop(this.stopRequested, token);
      return this.snapshot();
    }

    this.mic = opened.recorder;
    this.resampler = opened.rate === RECORDER_SAMPLE_RATE ? null : new StreamingResampler(opened.rate);
    this.gapTracker = new CaptureGapTracker(RECORDER_SAMPLE_RATE, this.gapThresholdMs);
    this.accepting = true;
    this.phase = "recording";
    this.publish();
    this.startHeartbeat(token);
    this.ensurePump(token);
    return this.snapshot();
  }

  /** Seal the recording created before a microphone/open-storage failure; never leave an empty active lease. */
  private async settleEmptyRecording(
    token: number,
    failure: RecorderFailure,
    message: string,
  ): Promise<void> {
    if (token !== this.run) return;
    this.failure = failure;
    this.lastError = message;
    let settled = !this.recordingId || !this.leaseToken;
    if (this.recordingId && this.leaseToken) {
      try {
        const state = await this.deps.api.stop({
          recordingId: this.recordingId,
          lease: this.leaseToken,
          streamId: this.deps.streamId(),
          finalSampleCount: 0,
        });
        this.acceptState(state);
        this.sealed = true;
        settled = true;
        if (state.complete === true) await this.clearLocalSpool();
        else await this.saveRecoveryMetadata(0);
      } catch (error) {
        const info = failureInfo(error);
        this.lastError = `${message} (${errorLabel(info)})`;
      }
    }
    this.stopHeartbeat();
    if (settled && this.pendingKey) {
      try { await this.pendingStore?.delete(this.pendingKey); this.pendingKey = null; } catch { /* recovery retries */ }
    }
    this.phase = "failed";
    this.publish();
    this.resolveStopWaiters();
  }

  /**
   * Explicit user stop: release the microphone first, then flush the final
   * partial chunk and drain the spool. Repeated stops share one result.
   */
  stop(): Promise<RecorderSnapshot> {
    if (this.phase === "stopping") return this.stopTask ?? Promise.resolve(this.snapshot());
    if (this.phase === "starting") {
      this.stopRequested ??= "user";
      if (!this.stopTask) {
        this.stopTask = new Promise<RecorderSnapshot>((resolve) => this.stopWaiters.push(resolve));
      }
      return this.stopTask;
    }
    if (this.phase === "recording") {
      this.stopTask ??= this.finishStop("user", this.run);
      return this.stopTask;
    }
    return Promise.resolve(this.snapshot());
  }

  /**
   * Reconnect the still-active capture to the current event stream.
   *
   * The daemon snapshot is authoritative: it supplies the durable checkpoint,
   * drops already-acknowledged spooled chunks and, when the stream detached,
   * rebinds the lease before uploads resume. A recording that is no longer
   * active becomes an honest interruption.
   */
  async reconnect(): Promise<RecorderSnapshot> {
    const token = this.run;
    if (!this.recordingId || !this.leaseToken || this.leaseLost) return this.snapshot();
    const ok = await this.ensureRebind(token);
    if (ok && token === this.run) this.ensurePump(token);
    return this.snapshot();
  }

  /** Alias for callers that describe the stream operation rather than the overall reconnect. */
  rebind(): Promise<RecorderSnapshot> {
    return this.reconnect();
  }

  /**
   * Rebind once for any number of concurrent heartbeat/upload failures.
   * Returns true when the lease is bound to the current stream again.
   */
  private ensureRebind(token: number): Promise<boolean> {
    if (token !== this.run || this.leaseLost || !this.recordingId || !this.leaseToken) {
      return Promise.resolve(false);
    }
    if (this.rebindWork) return this.rebindWork;
    const work = this.doEnsureRebind(token).finally(() => {
      if (this.rebindWork === work) this.rebindWork = null;
    });
    this.rebindWork = work;
    return work;
  }

  private async doEnsureRebind(token: number): Promise<boolean> {
    if (token !== this.run || this.leaseLost || !this.recordingId || !this.leaseToken) return false;
    const streamId = this.deps.streamId();
    if (!streamId) {
      this.lastError = "the event stream is not connected";
      this.publish();
      return false;
    }
    try {
      const authoritative = await this.deps.api.get(this.recordingId);
      if (token !== this.run) return false;
      if (!this.acceptAuthoritativeState(authoritative)) {
        await this.interruptCheckpoint(token);
        return false;
      }
      if (authoritative.active === false && authoritative.sealed === true) {
        await this.adoptSealedState(authoritative, token);
        return false;
      }
      if (authoritative.active === false || authoritative.capture_state === "interrupted") {
        await this.loseLease("server_interrupted", token);
        return false;
      }
      const rebound = await this.deps.api.rebind({
        recordingId: this.recordingId,
        lease: this.leaseToken,
        streamId,
        clientId: this.deps.clientId,
      });
      if (token !== this.run) return false;
      if (!this.acceptAuthoritativeState(rebound)) {
        await this.interruptCheckpoint(token);
        return false;
      }
      if (rebound.active === false && rebound.sealed === true) {
        await this.adoptSealedState(rebound, token);
        return false;
      }
      if (rebound.active === false || rebound.capture_state === "interrupted") {
        await this.loseLease("server_interrupted", token);
        return false;
      }
      this.boundStreamId = streamId;
      this.publish();
      return true;
    } catch (error) {
      if (token !== this.run) return false;
      const info = failureInfo(error);
      if (info.code === "recording_not_found") {
        await this.loseLease("deleted", token);
        return false;
      }
      if (classifyUploadFailure(info.status, info.code) === "lease_lost") {
        await this.loseLease(info.code ?? "lease_lost", token);
        return false;
      }
      this.lastError = errorLabel(info);
      this.publish();
      return false;
    }
  }

  /** A checkpoint inside our chunk violates Core's atomic chunk sequence: keep the bytes and stop. */
  private async interruptCheckpoint(token: number) {
    await this.loseLease("checkpoint_mismatch", token);
    this.failure = "checkpoint";
    this.lastError = "chunk_sequence_mismatch";
    this.publish();
  }

  /** Bounded pending create intents, for a UI recovery surface. Reads no microphone. */
  async listPendingStarts(): Promise<PendingRecorderStart[]> {
    const store = await this.pendingStarts();
    return store.list();
  }

  /**
   * Recover create intents left by a crash or dropped response. Never opens a
   * microphone: it withdraws unknown creates, seals active ones at their
   * durable offset, or leaves them for a later pass on transient failures.
   */
  async recoverPending(): Promise<PendingRecoveryOutcome[]> {
    const store = await this.pendingStarts();
    const records = await store.list();
    const outcomes: PendingRecoveryOutcome[] = [];
    for (const record of records) {
      const run = async (): Promise<PendingRecoveryOutcome> => this.recoverOnePending(store, record);
      let outcome: PendingRecoveryOutcome | undefined;
      if (store.crossTabLocks) {
        outcome = await store.withLock(record.key, run);
        if (!outcome) outcome = { key: record.key, outcome: "skipped" };
      } else if (Date.now() - record.createdAt < PENDING_LEASE_FALLBACK_MS) {
        outcome = { key: record.key, outcome: "skipped" };
      } else {
        outcome = await run();
      }
      outcomes.push(outcome);
    }
    return outcomes;
  }

  private async pendingStarts(): Promise<RecorderPendingStore> {
    if (this.pendingStore) return this.pendingStore;
    try {
      this.pendingStore = await this.pendingFactory();
    } catch {
      this.pendingStore = memoryPendingStore();
    }
    return this.pendingStore;
  }

  private async recoverOnePending(
    store: RecorderPendingStore,
    record: PendingRecorderStart,
  ): Promise<PendingRecoveryOutcome> {
    try {
      const state = await this.deps.api.create({
        clientId: record.clientId,
        idempotencyKey: record.key,
        recovery: record.recovery,
        mode: "resume",
        ...(record.title ? { title: record.title } : {}),
      });
      const recordingId = typeof state.id === "string" ? state.id : null;
      if (!recordingId) return { key: record.key, outcome: "kept", code: "no_recording_id" };
      try {
        const spool = await this.deps.spool(recordingId);
        if (await spool.loadRecovery()) {
          // The spool bound first; normal recovery owns this recording.
          await store.delete(record.key);
          return { key: record.key, outcome: "bound", recordingId };
        }
      } catch { /* the local store is optional for this decision */ }
      const durable = typeof state.durable_offset === "number" ? Math.max(0, Math.floor(state.durable_offset)) : 0;
      if (state.active === true) {
        const lease = state.lease_token ?? state.lease ?? null;
        if (!lease) return { key: record.key, outcome: "kept", recordingId, code: "lease_missing" };
        await this.deps.api.stop({
          recordingId, lease, streamId: this.deps.streamId(), finalSampleCount: durable,
        });
        await store.delete(record.key);
        return { key: record.key, outcome: "stopped", recordingId };
      }
      if (state.sealed === true) {
        await store.delete(record.key);
        return { key: record.key, outcome: "deleted", recordingId };
      }
      await this.deps.api.recoverStop({
        recordingId,
        recoveryToken: record.recovery,
        clientId: record.clientId,
        streamId: this.deps.streamId(),
        finalSampleCount: durable,
      });
      await store.delete(record.key);
      return { key: record.key, outcome: "sealed", recordingId };
    } catch (error) {
      const info = failureInfo(error);
      if (info.code === "create_not_found") {
        // The key is withdrawn on Core and locally deleted; never reuse it.
        await store.delete(record.key);
        return { key: record.key, outcome: "deleted", code: info.code };
      }
      if (info.code === "create_guard_full") {
        return { key: record.key, outcome: "kept", code: info.code, restartRequired: true };
      }
      if (info.status === 0 || info.status === undefined || (typeof info.status === "number" && info.status >= 500)) {
        return { key: record.key, outcome: "kept", code: errorLabel(info) };
      }
      return { key: record.key, outcome: "failed", code: errorLabel(info) };
    }
  }

  /** Token-free, bounded local spool discovery. It does not contact Core or open a microphone. */
  async listRecoverableSpools(limit = MAX_RECOVERABLE_SPOOLS): Promise<RecoverableSpoolSummary[]> {
    const index = await this.recoveryIndexFor();
    const bounded = Math.max(1, Math.min(MAX_RECOVERABLE_SPOOLS, Math.floor(limit)));
    const records = await index.list(bounded);
    return records.slice(0, bounded);
  }

  /**
   * Recover discovered spools after a reload without opening a microphone.
   * Each spool is touched only under its cross-tab lock and only when Core says
   * it is not active, so another tab's or owner's live capture is never stopped.
   */
  async recoverSpools(limit = MAX_RECOVERABLE_SPOOLS): Promise<RecoverableSpoolOutcome[]> {
    if (this.phase === "starting" || this.phase === "recording" || this.phase === "stopping") {
      throw new Error("Stop the active recording before recovering local spools");
    }
    const index = await this.recoveryIndexFor();
    const records = await this.listRecoverableSpools(limit);
    const outcomes: RecoverableSpoolOutcome[] = [];
    for (const record of records) {
      if (index.crossTabLocks) {
        const result = await index.withLock(record.recordingId, () => this.recoverOneSpool(record));
        outcomes.push(result ?? { recordingId: record.recordingId, outcome: "skipped_locked" });
      } else {
        // Without a cross-tab lock we cannot prove another tab is not using it.
        outcomes.push({ recordingId: record.recordingId, outcome: "skipped_locked", code: "locks_unavailable" });
      }
    }
    return outcomes;
  }

  private async recoveryIndexFor(): Promise<RecoverableSpoolIndex> {
    if (this.recoveryIndex) return this.recoveryIndex;
    try {
      this.recoveryIndex = await this.recoveryIndexFactory();
    } catch {
      this.recoveryIndex = memoryRecoveryIndex();
    }
    return this.recoveryIndex;
  }

  private async recoverOneSpool(record: RecoverableSpoolSummary): Promise<RecoverableSpoolOutcome> {
    try {
      const state = await this.deps.api.get(record.recordingId);
      if (state.active === true) {
        return { recordingId: record.recordingId, outcome: "skipped_active" };
      }
      if (state.sealed === true && state.complete === true) {
        await this.clearSpoolFor(record.recordingId);
        return { recordingId: record.recordingId, outcome: "cleared", complete: true };
      }
      const restored = await this.restore(record.recordingId);
      if (restored.failure === "recovery_impossible") {
        return { recordingId: record.recordingId, outcome: "failed", code: restored.lastError ?? "recovery_impossible" };
      }
      const recovered = await this.recover();
      if (recovered.phase === "stopped" && recovered.complete) {
        return { recordingId: record.recordingId, outcome: "recovered", complete: true };
      }
      return {
        recordingId: record.recordingId,
        outcome: recovered.failure === "recovery_impossible" ? "failed" : "recovered",
        complete: recovered.complete,
        code: recovered.lastError ?? undefined,
      };
    } catch (error) {
      const info = failureInfo(error);
      if (info.code === "recording_not_found") {
        await this.clearSpoolFor(record.recordingId);
        return { recordingId: record.recordingId, outcome: "cleared", code: info.code };
      }
      return { recordingId: record.recordingId, outcome: "failed", code: errorLabel(info) };
    }
  }

  private async clearSpoolFor(recordingId: string) {
    try {
      const spool = await this.deps.spool(recordingId);
      await spool.clear();
      await spool.clearRecovery();
    } catch { /* stale local metadata is harmless; a later pass retries */ }
  }

  /**
   * Restore a bounded local spool and its recovery capability after a reload.
   * This never opens a microphone; call `recover()` to seal and upload.
   */
  async restore(recordingId: string): Promise<RecorderSnapshot> {
    if (this.phase === "starting" || this.phase === "recording" || this.phase === "stopping") {
      throw new Error("A recording is already in progress");
    }
    this.resetForStart();
    const token = this.run;
    this.phase = "interrupted";
    this.recordingId = recordingId;
    this.publish();

    try {
      this.spool = await this.deps.spool(recordingId);
    } catch {
      this.spool = memoryRecorderSpool();
    }
    this.persistentSpool = this.spool.durable;
    this.storageWarning = this.spool.warning;

    let recovery: RecorderRecoveryRecord | null = null;
    let entries: RecorderSpoolEntry[] = [];
    try {
      recovery = await this.spool.loadRecovery();
      entries = await this.spool.load(this.spoolLimitSamples);
    } catch {
      this.failure = "recovery_impossible";
      this.lastError = "the local spool could not be read";
      this.publish();
      return this.snapshot();
    }
    if (token !== this.run) return this.snapshot();
    if (!recovery || recovery.recording !== recordingId) {
      this.failure = "recovery_impossible";
      this.lastError = "the recovery capability is missing";
      this.publish();
      return this.snapshot();
    }

    this.recoveryToken = recovery.recoveryToken;
    this.generation = recovery.generation;
    this.finalSampleCount = recovery.finalSampleCount;
    this.captureLostSamples = Math.max(0, recovery.captureLostSamples ?? 0);
    this.discardedSamples = Math.max(0, recovery.discardedSamples ?? 0);
    this.discardedLossSamples = Math.max(0, recovery.discardedLossSamples ?? 0);
    this.losses = Array.isArray(recovery.losses) ? recovery.losses.map((loss) => ({ ...loss })) : [];
    this.unsavedSamples = this.losses.reduce((sum, loss) => sum + loss.samples, 0);
    this.recoverable = true;
    this.setQueueFromSpool(entries);
    this.capturedSamples = Math.max(
      this.capturedSamples,
      this.finalSampleCount ?? 0,
      ...entries.map((entry) => entry.start + entry.samples),
    );

    try {
      const state = await this.deps.api.get(recordingId);
      if (token !== this.run) return this.snapshot();
      if (!this.acceptAuthoritativeState(state)) {
        this.recoverable = true;
        this.publish();
        return this.snapshot();
      }
      this.sealed = state.sealed === true;
      this.serverComplete = state.complete === true;
      if (typeof state.final_sample_count === "number") {
        this.finalSampleCount = state.final_sample_count;
      }
      if (state.active === true) this.lastError = "recording_active";
    } catch (error) {
      if (token !== this.run) return this.snapshot();
      const info = failureInfo(error);
      this.lastError = errorLabel(info);
      if (info.code === "recording_not_found") {
        this.failure = "recovery_impossible";
        this.recoverable = false;
      }
    }
    this.publish();
    return this.snapshot();
  }

  /**
   * Recover a restored or interrupted recording: seal its final offset with the
   * recovery token, then upload the contiguous pre-existing spooled prefix.
   * Recovery never starts a microphone or renews capture permission.
   */
  async recover(): Promise<RecorderSnapshot> {
    const token = this.run;
    if (!this.recordingId || !this.recoveryToken || !this.spool) {
      this.failure = "recovery_impossible";
      this.lastError = "the recovery capability is missing";
      this.recoverable = false;
      this.publish();
      return this.snapshot();
    }
    if (this.phase === "starting" || this.phase === "recording" || this.phase === "stopping") {
      throw new Error("Stop the active recording before recovering its spool");
    }
    this.publish();

    let state: RecorderPublicState;
    try {
      state = await this.deps.api.get(this.recordingId);
      if (token !== this.run) return this.snapshot();
      if (!this.acceptAuthoritativeState(state)) {
        return this.recoveryImpossible("chunk_sequence_mismatch", false);
      }
    } catch (error) {
      if (token !== this.run) return this.snapshot();
      return this.recoveryImpossible(errorLabel(failureInfo(error)), false);
    }

    if (state.active === true) return this.recoveryImpossible("recording_active", true);
    this.sealed = state.sealed === true;
    this.serverComplete = state.complete === true;
    if (typeof state.final_sample_count === "number") this.finalSampleCount = state.final_sample_count;

    if (!this.sealed) {
      const final = this.recoveryFinalSampleCount(state);
      let attempt = 0;
      for (;;) {
        try {
          const sealed = await this.deps.api.recoverStop({
            recordingId: this.recordingId,
            recoveryToken: this.recoveryToken,
            clientId: this.deps.clientId,
            streamId: this.deps.streamId(),
            finalSampleCount: final,
          });
          if (token !== this.run) return this.snapshot();
          if (!this.acceptAuthoritativeState(sealed)) {
            return this.recoveryImpossible("chunk_sequence_mismatch", false);
          }
          this.sealed = sealed.sealed !== false;
          this.serverComplete = sealed.complete === true;
          if (typeof sealed.final_sample_count === "number") this.finalSampleCount = sealed.final_sample_count;
          else this.finalSampleCount = final;
          break;
        } catch (error) {
          if (token !== this.run) return this.snapshot();
          const info = failureInfo(error);
          if (info.code === "recording_active") return this.recoveryImpossible(errorLabel(info), true);
          const kind = classifyUploadFailure(info.status, info.code);
          if ((kind === "retry" || kind === "rebind") && attempt < this.maxUploadAttempts) {
            const delay = retryDelay(attempt, info.retryAfterMs, this.retryDelaysMs);
            attempt += 1;
            this.retry = { sequence: -1, attempt, delayMs: delay };
            this.lastError = errorLabel(info);
            this.publish();
            await this.sleep(delay);
            continue;
          }
          return this.recoveryImpossible(errorLabel(info), false);
        }
      }
    }

    const final = this.finalSampleCount ?? this.recoveryFinalSampleCount(state);
    this.finalSampleCount = final;
    this.reconcileCheckpoint(this.ackedSamples);
    for (const entry of [...this.queue]) {
      if (entry.start + entry.samples > final) {
        this.recordLoss(entry, "reconciled");
        this.queue = this.queue.filter((candidate) => candidate !== entry);
        this.queuedSamples -= entry.samples;
        this.removeFromSpool(entry.seq);
      }
    }
    this.queue.sort((a, b) => a.seq - b.seq);
    for (const entry of this.queue) entry.attempts = 0;

    let noncontiguousRetries = 0;
    while (token === this.run && this.queue.length) {
      const entry = this.queue[0];
      if (entry.start !== this.ackedSamples || entry.start + entry.samples > final) {
        return this.recoveryImpossible(
          entry.start + entry.samples > final ? "beyond_sealed" : "noncontiguous_spool",
          false,
        );
      }
      if (!entry.sha256) {
        try {
          entry.sha256 = await this.deps.digest(entry.bytes);
        } catch (error) {
          return this.recoveryImpossible(errorLabel(failureInfo(error)), false);
        }
      }
      try {
        const result = await this.deps.api.recoverChunk({
          recordingId: this.recordingId,
          recoveryToken: this.recoveryToken,
          clientId: this.deps.clientId,
          streamId: this.deps.streamId(),
          seq: entry.seq,
          start: entry.start,
          body: entry.bytes,
          sha256: entry.sha256,
        });
        if (token !== this.run) return this.snapshot();
        this.acceptState(result.recording);
        if (this.queue[0] === entry) {
          this.queue.shift();
          this.queuedSamples -= entry.samples;
        }
        this.removeFromSpool(entry.seq);
        this.publish();
      } catch (error) {
        if (token !== this.run) return this.snapshot();
        const info = failureInfo(error);
        const kind = classifyUploadFailure(info.status, info.code);
        if (info.code === "noncontiguous" && noncontiguousRetries < 2) {
          noncontiguousRetries += 1;
          try {
            const current = await this.deps.api.get(this.recordingId);
            if (token !== this.run) return this.snapshot();
            if (!this.acceptAuthoritativeState(current)) {
              return this.recoveryImpossible("chunk_sequence_mismatch", false);
            }
            continue;
          } catch {
            return this.recoveryImpossible("noncontiguous_spool", false);
          }
        }
        if ((kind === "retry" || kind === "rebind") && entry.attempts < this.maxUploadAttempts) {
          const delay = retryDelay(entry.attempts, info.retryAfterMs, this.retryDelaysMs);
          entry.attempts += 1;
          this.retry = { sequence: entry.seq, attempt: entry.attempts, delayMs: delay };
          this.lastError = errorLabel(info);
          this.publish();
          await this.sleep(delay);
          continue;
        }
        return this.recoveryImpossible(errorLabel(info), false);
      }
    }
    if (token !== this.run) return this.snapshot();

    try {
      const current = await this.deps.api.get(this.recordingId);
      if (token !== this.run) return this.snapshot();
      if (!this.acceptAuthoritativeState(current)) {
        return this.recoveryImpossible("chunk_sequence_mismatch", false);
      }
      this.sealed = current.sealed === true;
      this.serverComplete = current.complete === true;
    } catch { /* the last recovery response still updated the checkpoint */ }
    this.phase = this.sealed ? "stopped" : "interrupted";
    this.failure = null;
    this.lastError = null;
    this.retry = null;
    if (this.serverComplete && this.queue.length === 0) await this.clearLocalSpool();
    else this.recoverable = true;
    this.publish();
    return this.snapshot();
  }

  private async adoptSealedState(state: RecorderPublicState, token: number): Promise<RecorderSnapshot> {
    await this.releaseMicrophone(token);
    const consistent = this.acceptAuthoritativeState(state);
    this.sealed = true;
    this.serverComplete = state.complete === true;
    if (typeof state.final_sample_count === "number") this.finalSampleCount = state.final_sample_count;
    this.phase = "stopped";
    this.failure = null;
    this.stopHeartbeat();
    if (consistent && this.serverComplete && this.queue.length === 0) {
      await this.clearLocalSpool();
    } else if (this.recoveryToken) {
      this.recoverable = true;
      await this.saveRecoveryMetadata(this.finalSampleCount);
    }
    this.publish();
    return this.snapshot();
  }

  /** Stop capture after a lease-loss signal; no further uploads or stop request use the dead lease. */
  async loseLease(reason: string, token = this.run): Promise<RecorderSnapshot> {
    if (token !== this.run) return this.snapshot();
    if (!this.leaseLost) {
      this.leaseLost = true;
      this.failure = reason === "deleted" ? "deleted" : reason === "disposed" ? "disposed" : "lease_lost";
      this.lastError = this.lastError ?? reason;
      this.stopRequested = null;
      this.accepting = false;
      this.uploadFatal = true;
      this.level = 0;
      this.stopHeartbeat();
      if (this.phase !== "stopped" && this.phase !== "failed") this.phase = "interrupted";
      this.publish();
      await this.releaseMicrophone(token);
      this.flushTail(token);
      this.finalSampleCount = this.sealedEndpoint();
      this.capturedSamples = this.sealedEndpoint();
      if (reason === "deleted") {
        this.recoverable = false;
        await this.clearLocalSpool();
      } else {
        this.recoverable = !!this.recoveryToken;
        await this.persistRecoveryQueue();
        await this.saveRecoveryMetadata(this.finalSampleCount);
      }
    }
    if (this.phase !== "stopped" && this.phase !== "failed") this.phase = "interrupted";
    this.publish();
    this.resolveStopWaiters();
    return this.snapshot();
  }

  /** Lease checks outside the heartbeat timer (tests, or the UI after a reconnect). */
  async heartbeatNow(): Promise<void> {
    await this.heartbeat(this.run);
  }

  /**
   * Apply live `recorder.state` / `recorder.deleted` events. Events for another
   * recording or generation are ignored; a lease loss or delete stops capture.
   */
  handleRecorderEvent(event: { type?: string; data?: unknown }): void {
    const data = event?.data;
    if (!data || typeof data !== "object" || !this.recordingId) return;
    const value = data as RecorderPublicState & { reason?: string; id?: string };
    if (value.id !== this.recordingId) return;
    if (this.generation !== null && typeof value.generation === "number" && value.generation !== this.generation) return;
    if (typeof value.revision === "number") {
      // Durable events are at-least-once. A repeated or older revision of the same
      // generation must not move offsets or state backwards.
      if (value.revision <= this.eventRevision) return;
      this.eventRevision = value.revision;
    }

    if (event.type === "recorder.deleted") {
      void this.loseLease("deleted");
      return;
    }
    if (event.type !== "recorder.state") return;
    if (typeof value.durable_offset === "number") {
      this.ackedSamples = Math.max(this.ackedSamples, Math.max(0, Math.floor(value.durable_offset)));
    }
    if (typeof value.holes === "number") this.holes = value.holes;
    if (typeof value.complete === "boolean") this.serverComplete = value.complete;
    if (value.active === false || value.capture_state === "interrupted") {
      if (this.phase === "stopping" || this.phase === "stopped" || this.phase === "failed") {
        this.publish();
        return;
      }
      void this.loseLease(typeof value.reason === "string" ? value.reason : "lease_lost");
      return;
    }
    this.publish();
  }

  /** Release the microphone and forget timers; used by UI teardown and tests. */
  async dispose(): Promise<void> {
    if (this.phase === "starting" || this.phase === "recording" || this.phase === "stopping") {
      // Disposal is an interruption, not an implicit successful stop: release the
      // device, flush the partial tail into the spool and leave recovery possible.
      await this.loseLease("disposed", this.run);
      await this.persistRecoveryQueue();
      await this.saveRecoveryMetadata(this.finalSampleCount);
    } else {
      this.accepting = false;
      this.level = 0;
      this.stopHeartbeat();
      await this.releaseMicrophone();
      if (this.queue.length) await this.persistRecoveryQueue();
    }
    this.run++;
    this.resolveStopWaiters();
  }

  private resetForStart() {
    this.run++;
    this.phase = "idle";
    this.failure = null;
    this.lastError = null;
    this.recordingId = null;
    this.generation = null;
    this.leaseToken = null;
    this.recoveryToken = null;
    this.boundStreamId = null;
    this.eventRevision = -1;
    this.recoverable = false;
    this.mic = null;
    this.resampler = null;
    this.chunker = new RecorderChunker();
    this.gapTracker = null;
    this.spool = null;
    this.pendingStore = null;
    this.recoveryIndex = null;
    this.pendingKey = null;
    this.persistentSpool = false;
    this.storageWarning = null;
    this.storageFaulted = false;
    this.accepting = false;
    this.releasing = false;
    this.stopRequested = null;
    this.stopTask = null;
    this.stopWork = null;
    this.queue = [];
    this.queuedSamples = 0;
    this.pumping = null;
    this.uploadFatal = false;
    this.leaseLost = false;
    this.heartbeatBusy = false;
    this.rebindWork = null;
    this.level = 0;
    this.capturedSamples = 0;
    this.captureLostSamples = 0;
    this.discardedSamples = 0;
    this.discardedLossSamples = 0;
    this.captureCut = false;
    this.captureCutEndpoint = null;
    this.ackedSamples = 0;
    this.unsavedSamples = 0;
    this.finalSampleCount = null;
    this.sealed = false;
    this.serverComplete = false;
    this.holes = 0;
    this.gaps = [];
    this.losses = [];
    this.retry = null;
  }

  private receive(samples: Float32Array, token: number) {
    if (token !== this.run) return;
    if (this.captureCut) {
      // A native loss cuts the timeline. Later blocks must never be spliced
      // onto the pre-gap audio; count them explicitly and discard them.
      this.level = 0;
      if (this.phase === "stopped" || this.phase === "failed" || this.phase === "interrupted") return;
      try {
        const discarded = this.resampler ? this.resampler.process(samples) : samples;
        this.discardedSamples += discarded.length;
      } catch {
        this.discardedSamples += samples.length;
      }
      this.publish();
      return;
    }
    if (!this.accepting && !this.releasing) return;
    let converted: Float32Array;
    try {
      converted = this.resampler ? this.resampler.process(samples) : samples;
    } catch (error) {
      this.internallyFailed(error, token);
      return;
    }
    if (!converted.length) return;
    this.level = rmsLevel(converted);

    const total = this.chunker.sampleCount + converted.length;
    const gap = this.gapTracker?.observe(this.now(), total);
    if (gap) this.gaps.push(gap);

    const chunks = this.chunker.push(converted);
    this.capturedSamples = this.chunker.sampleCount;
    this.enqueue(chunks, token);
    this.publish();
  }

  /** A device-reported hole: keep it explicit, never let a contiguous seal hide it. */
  private captureLoss(loss: RecorderCaptureLoss, token: number) {
    if (token !== this.run) return;
    if (this.phase === "stopped" || this.phase === "failed") return;
    const kind = loss?.kind ?? loss?.reason;
    const start = typeof loss?.start === "number" && Number.isFinite(loss.start)
      ? Math.max(0, Math.floor(loss.start))
      : this.chunker.sampleCount + this.captureLostSamples;
    const statedSamples = typeof loss?.samples === "number" && Number.isFinite(loss.samples)
      ? Math.max(0, Math.floor(loss.samples))
      : null;
    const statedEnd = typeof loss?.end === "number" && Number.isFinite(loss.end)
      ? Math.max(start, Math.floor(loss.end))
      : null;
    const samples = statedSamples ?? (statedEnd !== null ? statedEnd - start : 0);
    if (!samples) {
      this.captureFailure({
        stage: "loss",
        message: kind === "unknown"
          ? "native capture reported a loss with no sample count"
          : "native capture reported an empty loss",
      }, token);
      return;
    }
    const end = statedEnd ?? start + samples;
    if (this.losses.some((entry) =>
      (entry.reason === "native_overflow" || entry.reason === "native_after_cut")
      && entry.start === start && entry.end === end)) return;
    if (!this.captureCut) {
      // Freeze the sealed endpoint at the first cut. Audio after this point is
      // refused, so later losses during stop/drain are post-cut discards.
      this.captureCut = true;
      this.captureCutEndpoint = this.chunker.sampleCount + samples;
      this.captureLostSamples += samples;
      this.unsavedSamples += samples;
      this.finalSampleCount = this.captureCutEndpoint;
      this.losses.push({ start, end, samples, reason: "native_overflow" });
      this.lastError = `native capture lost ${samples} samples`;
      void this.saveRecoveryMetadata(this.captureCutEndpoint);
      this.publish();
      this.requestStop("native_loss", token);
      return;
    }
    this.discardedLossSamples += samples;
    this.discardedSamples += samples;
    this.losses.push({ start, end, samples, reason: "native_after_cut" });
    this.lastError = `native capture reported a later ${samples}-sample loss after the capture cut`;
    void this.saveRecoveryMetadata(this.finalSampleCount ?? this.sealedEndpoint());
    this.publish();
  }

  /** A capture device/read failure must stop promptly; it can never become a clean seal. */
  private captureFailure(failure: RecorderCaptureFailure, token: number) {
    if (token !== this.run) return;
    if (this.phase === "stopped") return;
    this.failure = "capture";
    this.captureCut = true;
    this.lastError = typeof failure?.message === "string" && failure.message
      ? failure.message
      : typeof failure?.stage === "string" ? `native capture failed (${failure.stage})` : "native capture failed";
    this.publish();
    if (this.phase === "recording" || this.phase === "starting") this.requestStop("capture_error", token);
    void this.saveRecoveryMetadata(this.finalSampleCount ?? this.chunker.sampleCount + this.captureLostSamples);
  }

  private enqueue(chunks: PcmChunk[], token: number) {
    for (const chunk of chunks) {
      if (!spoolFits(this.queuedSamples, chunk.samples, this.spoolLimitSamples)) {
        this.recordLoss(chunk, "spool_overflow");
        this.requestStop("spool_overflow", token);
        continue;
      }
      this.queue.push({ ...chunk, sha256: null, persisted: false, attempts: 0 });
      this.queuedSamples += chunk.samples;
    }
    if (this.queue.length) this.ensurePump(token);
  }

  private recordLoss(chunk: PcmChunk, reason: RecorderLoss["reason"]) {
    this.unsavedSamples += chunk.samples;
    this.losses.push({ start: chunk.start, end: chunk.start + chunk.samples, samples: chunk.samples, reason });
  }

  private requestStop(reason: string, token: number) {
    if (token !== this.run || this.leaseLost) return;
    this.stopRequested ??= reason;
    this.accepting = false;
    if (this.phase === "recording") void this.finishStop(reason, token);
  }

  private ensurePump(token: number) {
    if (this.pumping || this.uploadFatal || this.leaseLost || token !== this.run) return;
    if (!this.queue.length) return;
    if (this.phase !== "recording" && this.phase !== "stopping") return;
    this.pumping = this.pumpLoop(token).finally(() => {
      this.pumping = null;
      if (token === this.run && this.queue.length && !this.uploadFatal && !this.leaseLost
        && (this.phase === "recording" || this.phase === "stopping")) {
        this.ensurePump(token);
      }
    });
  }

  private async pumpLoop(token: number): Promise<void> {
    while (token === this.run && !this.uploadFatal && !this.leaseLost) {
      const entry = this.queue[0];
      if (!entry) return;
      if (this.phase !== "recording" && this.phase !== "stopping") return;
      if (!this.recordingId || !this.leaseToken) return;

      if (!entry.sha256) {
        try {
          entry.sha256 = await this.deps.digest(entry.bytes);
        } catch (error) {
          if (token !== this.run) return;
          this.uploadFatal = true;
          this.failure = "upload";
          this.lastError = failureInfo(error).message ?? "the audio hash could not be computed";
          this.markPendingLost("upload_failed");
          this.publish();
          this.requestStop("upload", token);
          return;
        }
      }
      if (token !== this.run) return;
      const sha256 = entry.sha256;

      if (!entry.persisted) {
        try {
          await this.spool?.put({
            recording: this.recordingId, seq: entry.seq, start: entry.start,
            samples: entry.samples, bytes: entry.bytes.slice(), sha256,
          });
          entry.persisted = true;
        } catch (error) {
          if (token !== this.run) return;
          this.storageFault(error, token);
        }
      }
      if (token !== this.run || this.uploadFatal || this.leaseLost) return;

      try {
        const result = await this.deps.api.putChunk({
          recordingId: this.recordingId,
          lease: this.leaseToken,
          streamId: this.deps.streamId(),
          seq: entry.seq,
          start: entry.start,
          body: entry.bytes,
          sha256,
        });
        if (token !== this.run) return;
        if (this.leaseLost) return;
        this.acceptState(result.recording);
        if (this.queue[0] === entry) {
          this.queue.shift();
          this.queuedSamples -= entry.samples;
        }
        this.retry = null;
        this.removeFromSpool(entry.seq);
        this.publish();
      } catch (error) {
        if (token !== this.run) return;
        const info = failureInfo(error);
        const kind = classifyUploadFailure(info.status, info.code);
        if (kind === "lease_lost") {
          this.lastError = errorLabel(info);
          await this.loseLease(info.code ?? "lease_lost", token);
          return;
        }
        if (kind === "rebind") {
          const rebound = await this.ensureRebind(token);
          if (rebound) continue;
          if (this.leaseLost || this.isTerminal()) return;
        }
        if ((kind === "retry" || kind === "rebind") && entry.attempts < this.maxUploadAttempts) {
          const delay = retryDelay(entry.attempts, info.retryAfterMs, this.retryDelaysMs);
          entry.attempts += 1;
          this.retry = { sequence: entry.seq, attempt: entry.attempts, delayMs: delay };
          this.lastError = errorLabel(info);
          this.publish();
          await this.sleep(delay);
          if (token !== this.run || this.leaseLost) return;
          continue;
        }
        this.uploadFatal = true;
        this.failure = "upload";
        this.lastError = errorLabel(info);
        this.publish();
        this.requestStop("upload", token);
        return;
      }
    }
  }

  private markPendingLost(reason: "upload_failed" | "capture_failed" = "upload_failed") {
    const kept: QueuedChunk[] = [];
    for (const entry of this.queue) {
      if (entry.persisted) {
        // The bytes are recoverable from the spool after a sealed capture.
        kept.push(entry);
        continue;
      }
      this.unsavedSamples += entry.samples;
      this.losses.push({ start: entry.start, end: entry.start + entry.samples, samples: entry.samples, reason });
    }
    this.queue = kept;
    this.queuedSamples = kept.reduce((sum, entry) => sum + entry.samples, 0);
  }

  /**
   * Drop spooled chunks Core already has. A checkpoint in the middle of one of
   * our chunks is impossible under Core's atomic chunk sequence: keep those
   * bytes, fail closed and report the invariant violation rather than discarding
   * a suffix that Core may still recover.
   */
  private reconcileCheckpoint(durable: number): boolean {
    const kept: QueuedChunk[] = [];
    let mismatch = false;
    for (const entry of this.queue) {
      const end = entry.start + entry.samples;
      if (end <= durable) {
        this.removeFromSpool(entry.seq);
        continue;
      }
      if (entry.start < durable) {
        mismatch = true;
        kept.push(entry);
        continue;
      }
      kept.push(entry);
    }
    this.queue = kept;
    this.queuedSamples = kept.reduce((sum, entry) => sum + entry.samples, 0);
    this.capturedSamples = Math.max(this.capturedSamples, durable);
    if (mismatch) {
      this.failure = "checkpoint";
      this.lastError = "chunk_sequence_mismatch";
    }
    return !mismatch;
  }

  private storageFault(error: unknown, token: number) {
    if (token !== this.run) return;
    this.storageFaulted = true;
    this.storageWarning = "Unsaved audio could not be stored locally; capture is stopping";
    this.lastError = failureInfo(error).message ?? "the local spool rejected audio";
    this.publish();
    if (this.phase === "recording" || this.phase === "starting") this.requestStop("storage", token);
  }

  private finishStop(reason: string, token: number): Promise<RecorderSnapshot> {
    if (this.stopWork) return this.stopWork;
    this.stopWork = this.doFinishStop(reason, token);
    this.stopTask ??= this.stopWork;
    void this.stopWork.finally(() => this.resolveStopWaiters());
    return this.stopWork;
  }

  private async doFinishStop(reason: string, token: number): Promise<RecorderSnapshot> {
    if (token !== this.run) return this.snapshot();
    if (this.phase === "stopped" || this.phase === "interrupted" || this.phase === "failed" || this.phase === "idle") {
      return this.snapshot();
    }
    this.stopRequested ??= reason;
    this.phase = "stopping";
    this.accepting = false;
    this.level = 0;
    this.publish();

    // Release the device first: the worklet's flush still reaches `receive`.
    await this.releaseMicrophone(token);
    if (token !== this.run) return this.snapshot();
    this.level = 0;

    if (!this.leaseLost) {
      this.flushTail(token);
    }
    this.finalSampleCount = this.sealedEndpoint();
    this.capturedSamples = this.sealedEndpoint();
    this.publish();

    if (this.leaseLost) {
      this.phase = "interrupted";
      this.publish();
      return this.snapshot();
    }

    this.ensurePump(token);
    while (token === this.run && this.queue.length && !this.uploadFatal && !this.leaseLost) {
      await this.sleep(10);
    }
    if (token !== this.run) return this.snapshot();
    if (this.leaseLost) {
      this.phase = "interrupted";
      this.publish();
      return this.snapshot();
    }

    await this.seal(token, this.uploadFatal);
    return this.snapshot();
  }

  private flushTail(token: number) {
    if (token !== this.run) return;
    const tail = this.chunker.flush();
    if (!tail.length) return;
    this.enqueue(tail, token);
    this.capturedSamples = this.chunker.sampleCount;
  }

  private async seal(token: number, failedUpload: boolean): Promise<void> {
    const recordingId = this.recordingId;
    const lease = this.leaseToken;
    if (token !== this.run) return;
    if (!recordingId || !lease) {
      this.phase = failedUpload ? "failed" : "stopped";
      this.publish();
      return;
    }
    const finalSampleCount = this.finalSampleCount ?? this.chunker.sampleCount;
    let attempt = 0;
    while (token === this.run && !this.leaseLost) {
      try {
        const state = await this.deps.api.stop({
          recordingId,
          lease,
          streamId: this.deps.streamId(),
          finalSampleCount,
        });
        if (token !== this.run) return;
        if (this.leaseLost) {
          this.phase = "interrupted";
          this.publish();
          return;
        }
        this.acceptState(state);
        this.sealed = true;
        this.failure = failedUpload ? "upload" : this.failure === "capture" ? "capture" : null;
        this.phase = failedUpload ? "failed" : "stopped";
        this.retry = null;
        this.stopHeartbeat();
        if (this.serverComplete && this.queue.length === 0) {
          await this.clearLocalSpool();
        } else if (this.recoveryToken) {
          this.recoverable = true;
          await this.saveRecoveryMetadata(this.finalSampleCount);
        }
        this.publish();
        return;
      } catch (error) {
        if (token !== this.run) return;
        const info = failureInfo(error);
        const kind = classifyUploadFailure(info.status, info.code);
        if (kind === "lease_lost") {
          this.lastError = errorLabel(info);
          await this.loseLease(info.code ?? "lease_lost", token);
          return;
        }
        if (kind === "rebind") {
          const rebound = await this.ensureRebind(token);
          if (rebound) continue;
          if (this.leaseLost || this.isTerminal()) return;
        }
        if ((kind === "retry" || kind === "rebind") && attempt < this.maxUploadAttempts) {
          const delay = retryDelay(attempt, info.retryAfterMs, this.retryDelaysMs);
          attempt += 1;
          this.retry = { sequence: -1, attempt, delayMs: delay };
          this.lastError = errorLabel(info);
          this.publish();
          await this.sleep(delay);
          continue;
        }
        this.failure = "seal";
        this.lastError = errorLabel(info);
        this.phase = "failed";
        this.stopHeartbeat();
        if (this.recoveryToken) {
          this.recoverable = true;
          await this.saveRecoveryMetadata(finalSampleCount);
        }
        this.publish();
        return;
      }
    }
  }

  private acceptState(state: RecorderPublicState | null | undefined) {
    if (!state) return;
    if (typeof state.generation === "number") this.generation = state.generation;
    const revision = typeof state.event_revision === "number" ? state.event_revision : state.revision;
    if (typeof revision === "number") this.eventRevision = Math.max(this.eventRevision, revision);
    if (typeof state.durable_offset === "number" && Number.isFinite(state.durable_offset)) {
      this.ackedSamples = Math.max(this.ackedSamples, Math.max(0, Math.floor(state.durable_offset)));
    }
    if (typeof state.final_sample_count === "number") this.finalSampleCount = state.final_sample_count;
    if (typeof state.complete === "boolean") this.serverComplete = state.complete;
    if (typeof state.holes === "number") this.holes = state.holes;
  }

  /** The snapshot is authoritative at read time: it may move the checkpoint, never just forward. */
  private acceptAuthoritativeState(state: RecorderPublicState | null | undefined): boolean {
    if (!state) return true;
    this.acceptState(state);
    if (typeof state.durable_offset === "number" && Number.isFinite(state.durable_offset)) {
      this.ackedSamples = Math.max(0, Math.floor(state.durable_offset));
    }
    return this.reconcileCheckpoint(this.ackedSamples);
  }

  private sealedEndpoint(): number {
    return this.captureCutEndpoint ?? this.chunker.sampleCount + this.captureLostSamples;
  }

  private isTerminal(): boolean {
    return this.phase === "stopped" || this.phase === "failed" || this.phase === "interrupted";
  }

  private startHeartbeat(token: number) {
    this.stopHeartbeat();
    if (this.heartbeatMs <= 0) return;
    this.heartbeatTimer = this.timers.setInterval(() => { void this.heartbeat(token); }, this.heartbeatMs);
  }

  private stopHeartbeat() {
    if (this.heartbeatTimer === null) return;
    this.timers.clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  private async heartbeat(token: number) {
    if (token !== this.run || this.heartbeatBusy || this.leaseLost || !this.recordingId || !this.leaseToken) return;
    if (this.phase !== "recording" && this.phase !== "stopping") return;
    this.heartbeatBusy = true;
    try {
      const state = await this.deps.api.heartbeat({
        recordingId: this.recordingId,
        lease: this.leaseToken,
        streamId: this.deps.streamId(),
      });
      if (token !== this.run) return;
      this.acceptState(state);
      if (state.active === false || state.capture_state === "interrupted") {
        await this.loseLease("heartbeat", token);
        return;
      }
      this.publish();
    } catch (error) {
      if (token !== this.run) return;
      const info = failureInfo(error);
      const kind = classifyUploadFailure(info.status, info.code);
      if (kind === "rebind") {
        const rebound = await this.ensureRebind(token);
        if (rebound || this.leaseLost) return;
      }
      if (kind === "lease_lost") {
        this.lastError = errorLabel(info);
        await this.loseLease(info.code ?? "lease_lost", token);
        return;
      }
      this.lastError = errorLabel(info);
      this.publish();
    } finally {
      this.heartbeatBusy = false;
    }
  }

  private removeFromSpool(seq: number) {
    try {
      const pending = this.spool?.remove(seq);
      if (pending && typeof (pending as Promise<void>).catch === "function") {
        void (pending as Promise<void>).catch((error) => {
          this.lastError = failureInfo(error).message ?? "the local spool could not drop an acknowledged chunk";
          this.publish();
        });
      }
    } catch (error) {
      this.lastError = failureInfo(error).message ?? "the local spool could not drop an acknowledged chunk";
    }
  }

  private async persistRecoveryQueue() {
    const spool = this.spool;
    if (!spool || !this.recordingId) return;
    for (const entry of this.queue) {
      if (entry.persisted) continue;
      try {
        entry.sha256 ??= await this.deps.digest(entry.bytes);
        await spool.put({
          recording: this.recordingId, seq: entry.seq, start: entry.start,
          samples: entry.samples, bytes: entry.bytes.slice(), sha256: entry.sha256,
        });
        entry.persisted = true;
      } catch {
        // The spool is best effort after lease loss; pending samples stay reported.
        break;
      }
    }
  }

  private setQueueFromSpool(entries: readonly RecorderSpoolEntry[]) {
    this.queue = [...entries]
      .sort((a, b) => a.seq - b.seq)
      .map((entry) => ({
        seq: entry.seq,
        start: entry.start,
        samples: entry.samples,
        bytes: entry.bytes,
        sha256: entry.sha256 || null,
        persisted: true,
        attempts: 0,
      }));
    this.queuedSamples = this.queue.reduce((sum, entry) => sum + entry.samples, 0);
  }

  private recoveryFinalSampleCount(state: RecorderPublicState): number {
    const candidates: number[] = [this.ackedSamples];
    if (this.finalSampleCount !== null) candidates.push(this.finalSampleCount);
    if (typeof state.final_sample_count === "number") candidates.push(state.final_sample_count);
    for (const entry of this.queue) candidates.push(entry.start + entry.samples);
    return Math.max(...candidates);
  }

  private recoveryImpossible(code: string, retryable: boolean): RecorderSnapshot {
    this.failure = "recovery_impossible";
    this.lastError = code;
    this.phase = "interrupted";
    this.recoverable = retryable;
    this.publish();
    return this.snapshot();
  }

  private async saveRecoveryMetadata(finalSampleCount: number | null) {
    const spool = this.spool;
    if (!spool || !this.recordingId || !this.recoveryToken) return;
    try {
      await spool.saveRecovery({
        recording: this.recordingId,
        generation: this.generation,
        clientId: this.deps.clientId,
        recoveryToken: this.recoveryToken,
        finalSampleCount,
        captureLostSamples: this.captureLostSamples,
        discardedSamples: this.discardedSamples,
        discardedLossSamples: this.discardedLossSamples,
        losses: this.losses.slice(),
      });
    } catch {
      this.storageWarning = "The recovery capability could not be stored locally";
      this.recoverable = false;
    }
  }

  private async clearLocalSpool() {
    this.recoverable = false;
    try {
      await this.spool?.clear();
      await this.spool?.clearRecovery();
    } catch {
      this.storageWarning = "The acknowledged local spool could not be removed";
    }
  }

  private async releaseMicrophone(token?: number) {
    const mic = this.mic;
    this.mic = null;
    if (!mic) return;
    this.releasing = true;
    try {
      await mic.stop();
    } catch (error) {
      if (token === undefined || token === this.run) {
        // Native capture rejects its stop when the device lost audio or read
        // failed. Never let that become a clean contiguous seal.
        this.failure ??= "capture";
        this.lastError = failureInfo(error).message ?? "the microphone did not close cleanly";
      }
    } finally {
      this.releasing = false;
    }
  }

  private async releaseCapture(mic: CaptureRecorder) {
    try { await mic.stop(); } catch { /* a failed start releases best effort */ }
  }

  private internallyFailed(error: unknown, token: number) {
    if (token !== this.run) return;
    this.uploadFatal = true;
    this.failure = "internal";
    this.lastError = failureInfo(error).message ?? "recorder processing failed";
    this.markPendingLost("upload_failed");
    this.publish();
    this.requestStop("internal", token);
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => this.timers.setTimeout(resolve, Math.max(0, ms)));
  }

  private publish() {
    this.deps.onChange?.(this.snapshot());
  }

  private resolveStopWaiters() {
    const snapshot = this.snapshot();
    for (const resolve of this.stopWaiters.splice(0)) resolve(snapshot);
  }
}

// ---------------------------------------------------------------------------------------------------
// Browser defaults. The UI item supplies `clientId` and the current event-stream id; nothing here reads
// the app store or a native module, so the controller can be created and tested in isolation.

export class RecorderHttpError extends Error {
  constructor(public status: number, public code?: string, message?: string, public retryAfterMs: number | null = null) {
    super(message ?? code ?? `HTTP ${status}`);
  }
}

async function responseError(response: Response): Promise<RecorderHttpError> {
  const text = await response.text();
  let body: any = null;
  try { body = JSON.parse(text); } catch {}
  return new RecorderHttpError(
    response.status,
    typeof body?.code === "string" ? body.code : undefined,
    typeof body?.error === "string" ? body.error : (text.slice(0, 200) || `HTTP ${response.status}`),
    parseRetryAfter(response.headers.get("retry-after")),
  );
}

export function createWebRecorderApi(): RecorderApi {
  const base = (recordingId: string) => `/api/recordings/${encodeURIComponent(recordingId)}`;
  return {
    async create({ streamId, clientId, idempotencyKey, title, recovery, mode }) {
      const body: Record<string, unknown> = {
        client_id: clientId,
        ...(title ? { title } : {}),
        ...(mode === "resume" ? { mode: "resume" } : { mode: "create", stream_id: streamId }),
      };
      for (let attempt = 0; attempt < 3; attempt++) {
        const response = await fetch(apiUrl("/api/recordings"), {
          method: "POST",
          headers: {
            accept: "application/json",
            "content-type": "application/json",
            "idempotency-key": idempotencyKey,
            "x-recorder-recovery": recovery,
          },
          body: JSON.stringify(body),
        });
        if (response.ok) return response.json() as Promise<RecorderCreatedState>;
        const error = await responseError(response);
        if (error.status !== 0 && error.status < 500 && error.status !== 429) throw error;
        if (attempt === 2) throw error;
        await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)));
      }
      throw new RecorderHttpError(0, "network_error", "the recording could not be created");
    },
    get: (recordingId) => request<RecorderPublicState>("GET", base(recordingId)),
    async putChunk({ recordingId, lease, streamId, seq, start, body, sha256 }) {
      const headers: Record<string, string> = {
        accept: "application/json",
        "content-type": "application/octet-stream",
        "x-recorder-lease": lease,
        "x-sample-start": String(start),
        "x-content-sha256": sha256,
      };
      if (streamId) headers["x-recorder-stream"] = streamId;
      // The web host adds the daemon token (transport.ts explains the same-origin rule).
      const response = await fetch(apiUrl(`${base(recordingId)}/chunks/${seq}`), {
        method: "PUT",
        headers,
        body: body.slice().buffer,
      });
      if (!response.ok) throw await responseError(response);
      return response.json();
    },
    heartbeat: ({ recordingId, lease, streamId }) => {
      return (async () => {
        const response = await fetch(apiUrl(`${base(recordingId)}/heartbeat`), {
          method: "POST",
          headers: { accept: "application/json", "content-type": "application/json", "x-recorder-lease": lease },
          body: JSON.stringify({ stream_id: streamId ?? undefined }),
        });
        if (!response.ok) throw await responseError(response);
        return response.json() as Promise<RecorderPublicState>;
      })();
    },
    rebind: ({ recordingId, lease, streamId, clientId }) => {
      return (async () => {
        const response = await fetch(apiUrl(`${base(recordingId)}/rebind`), {
          method: "POST",
          headers: { accept: "application/json", "content-type": "application/json", "x-recorder-lease": lease },
          body: JSON.stringify({ stream_id: streamId, client_id: clientId }),
        });
        if (!response.ok) throw await responseError(response);
        return response.json() as Promise<RecorderPublicState>;
      })();
    },
    stop: ({ recordingId, lease, streamId, finalSampleCount }) => {
      return (async () => {
        const response = await fetch(apiUrl(`${base(recordingId)}/stop`), {
          method: "POST",
          headers: { accept: "application/json", "content-type": "application/json", "x-recorder-lease": lease },
          body: JSON.stringify({ stream_id: streamId ?? undefined, final_sample_count: finalSampleCount }),
        });
        if (!response.ok) throw await responseError(response);
        return response.json() as Promise<RecorderPublicState>;
      })();
    },
    recoverStop: ({ recordingId, recoveryToken, clientId, streamId, finalSampleCount }) => {
      return (async () => {
        const response = await fetch(apiUrl(`${base(recordingId)}/recover`), {
          method: "POST",
          headers: { accept: "application/json", "content-type": "application/json", "x-recorder-recovery": recoveryToken },
          body: JSON.stringify({
            client_id: clientId,
            stream_id: streamId ?? undefined,
            final_sample_count: finalSampleCount,
          }),
        });
        if (!response.ok) throw await responseError(response);
        return response.json() as Promise<RecorderPublicState>;
      })();
    },
    async recoverChunk({ recordingId, recoveryToken, clientId, streamId, seq, start, body, sha256 }) {
      const headers: Record<string, string> = {
        accept: "application/json",
        "content-type": "application/octet-stream",
        "x-recorder-recovery": recoveryToken,
        "x-recorder-client": clientId,
        "x-sample-start": String(start),
        "x-content-sha256": sha256,
      };
      if (streamId) headers["x-recorder-stream"] = streamId;
      const response = await fetch(apiUrl(`${base(recordingId)}/recover/chunks/${seq}`), {
        method: "PUT",
        headers,
        body: body.slice().buffer,
      });
      if (!response.ok) throw await responseError(response);
      return response.json();
    },
  };
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const subtle = (globalThis as unknown as { crypto?: { subtle?: { digest(algorithm: string, data: ArrayBuffer): Promise<ArrayBuffer> } } }).crypto?.subtle;
  if (!subtle) throw new Error("Web Crypto is not available in this client");
  const digest = await subtle.digest("SHA-256", bytes.slice().buffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const SPOOL_DB = "nexus-recorder-spool";
const PENDING_LEASE_FALLBACK_MS = 40_000; // 2 x the daemon lease/detach bounds
const MAX_RECOVERABLE_SPOOLS = 50;
const SPOOL_STORE = "chunks";
const RECOVERY_STORE = "recovery";
const STARTS_STORE = "starts";
const SPOOL_MIN_FREE_BYTES = 8 * 1024 * 1024;

async function quotaWarning(): Promise<string | null> {
  try {
    const storage = (globalThis as unknown as { navigator?: { storage?: { estimate?: () => Promise<{ quota?: number; usage?: number }> } } }).navigator?.storage;
    if (!storage?.estimate) return null;
    const { quota, usage } = await storage.estimate();
    if (typeof quota !== "number" || typeof usage !== "number") return null;
    if (quota - usage < SPOOL_MIN_FREE_BYTES) return RECORDER_MEMORY_ONLY_WARNING;
  } catch { /* A missing estimate is not itself a failure. */ }
  return null;
}

type RecorderIdbDatabase = {
  transaction(store: string, mode: "readonly" | "readwrite"): any;
  objectStoreNames: { contains(name: string): boolean };
  createObjectStore(name: string, options?: { keyPath: string | string[] }): unknown;
};
type RecorderIdbFactory = { open(name: string, version?: number): any };

function openSpoolDatabase(): Promise<RecorderIdbDatabase> {
  return new Promise((resolve, reject) => {
    const factory = (globalThis as unknown as { indexedDB?: RecorderIdbFactory }).indexedDB;
    if (!factory) { reject(new Error("IndexedDB is not available")); return; }
    const request = factory.open(SPOOL_DB, 3);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(SPOOL_STORE)) {
        db.createObjectStore(SPOOL_STORE, { keyPath: ["recording", "seq"] });
      }
      if (!db.objectStoreNames.contains(RECOVERY_STORE)) {
        db.createObjectStore(RECOVERY_STORE, { keyPath: "recording" });
      }
      if (!db.objectStoreNames.contains(STARTS_STORE)) {
        db.createObjectStore(STARTS_STORE, { keyPath: "key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB could not be opened"));
  });
}

class IndexedDbRecorderSpool implements RecorderSpool {
  readonly durable: boolean;
  readonly warning: string | null;
  constructor(
    private db: RecorderIdbDatabase,
    private recording: string,
    readonly guarantee: "persistent" | "best-effort",
  ) {
    this.durable = guarantee === "persistent";
    this.warning = guarantee === "persistent"
      ? null
      : "Local audio storage is best-effort and may be evicted";
  }

  put(entry: RecorderSpoolEntry): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(SPOOL_STORE, "readwrite");
      transaction.objectStore(SPOOL_STORE).put({
        recording: entry.recording, seq: entry.seq, start: entry.start, samples: entry.samples,
        bytes: entry.bytes.slice().buffer, sha256: entry.sha256,
      });
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB write failed"));
      transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB write was aborted"));
    });
  }

  remove(seq: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(SPOOL_STORE, "readwrite");
      transaction.objectStore(SPOOL_STORE).delete([this.recording, seq]);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB delete failed"));
    });
  }

  clear(): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(SPOOL_STORE, "readwrite");
      const request = transaction.objectStore(SPOOL_STORE).openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        if ((cursor.value as { recording?: string })?.recording === this.recording) cursor.delete();
        cursor.continue();
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB clear failed"));
    });
  }

  load(limitSamples = RECORDER_SPOOL_SAMPLES): Promise<RecorderSpoolEntry[]> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(SPOOL_STORE, "readonly");
      const entries: RecorderSpoolEntry[] = [];
      let used = 0;
      const request = transaction.objectStore(SPOOL_STORE).openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        const value = cursor.value as {
          recording: string; seq: number; start: number; samples: number;
          bytes: ArrayBuffer | Uint8Array; sha256: string;
        };
        if (value.recording === this.recording) {
          if (used + value.samples > limitSamples) {
            // Stop the bounded read at the first entry beyond the budget.
            return;
          }
          entries.push({
            recording: value.recording,
            seq: value.seq,
            start: value.start,
            samples: value.samples,
            sha256: value.sha256,
            bytes: value.bytes instanceof Uint8Array ? value.bytes.slice() : new Uint8Array(value.bytes.slice(0)),
          });
          used += value.samples;
        }
        cursor.continue();
      };
      transaction.oncomplete = () => resolve(entries.sort((a, b) => a.seq - b.seq));
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB read failed"));
    });
  }

  loadRecovery(): Promise<RecorderRecoveryRecord | null> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(RECOVERY_STORE, "readonly");
      const request = transaction.objectStore(RECOVERY_STORE).get(this.recording);
      request.onsuccess = () => resolve((request.result as RecorderRecoveryRecord | undefined) ?? null);
      request.onerror = () => reject(request.error ?? new Error("IndexedDB recovery read failed"));
    });
  }

  saveRecovery(record: RecorderRecoveryRecord): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(RECOVERY_STORE, "readwrite");
      transaction.objectStore(RECOVERY_STORE).put(record);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB recovery write failed"));
      transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB recovery write was aborted"));
    });
  }

  clearRecovery(): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(RECOVERY_STORE, "readwrite");
      transaction.objectStore(RECOVERY_STORE).delete(this.recording);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB recovery delete failed"));
    });
  }
}

class IndexedDbPendingStore implements RecorderPendingStore {
  readonly durable: boolean;
  readonly guarantee: "persistent" | "best-effort";
  readonly warning: string | null;
  readonly crossTabLocks: boolean;

  constructor(private db: RecorderIdbDatabase, guarantee: "persistent" | "best-effort") {
    this.guarantee = guarantee;
    this.durable = guarantee === "persistent";
    this.warning = guarantee === "persistent" ? null : "Persistent browser storage was not granted";
    const locks = (globalThis as unknown as {
      navigator?: { locks?: { request?: (...args: any[]) => Promise<unknown> } };
    }).navigator?.locks;
    this.crossTabLocks = typeof locks?.request === "function";
  }

  put(record: PendingRecorderStart): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(STARTS_STORE, "readwrite");
      transaction.objectStore(STARTS_STORE).put(record);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB pending write failed"));
      transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB pending write was aborted"));
    });
  }

  delete(key: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(STARTS_STORE, "readwrite");
      transaction.objectStore(STARTS_STORE).delete(key);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB pending delete failed"));
    });
  }

  list(): Promise<PendingRecorderStart[]> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(STARTS_STORE, "readonly");
      const request = transaction.objectStore(STARTS_STORE).getAll();
      request.onsuccess = () => resolve((request.result as PendingRecorderStart[]) ?? []);
      request.onerror = () => reject(request.error ?? new Error("IndexedDB pending read failed"));
    });
  }

  async withLock<T>(key: string, run: () => Promise<T>): Promise<T | undefined> {
    const locks = (globalThis as unknown as {
      navigator?: {
        locks?: {
          request?: (
            name: string,
            options: { ifAvailable: boolean },
            callback: (lock: unknown) => Promise<T | undefined>,
          ) => Promise<T | undefined>;
        };
      };
    }).navigator?.locks;
    if (!locks?.request) return run();
    return locks.request(`nexus-rec-start:${key}`, { ifAvailable: true }, async (lock) => {
      if (!lock) return undefined;
      return run();
    });
  }
}

class IndexedDbRecoveryIndex implements RecoverableSpoolIndex {
  readonly crossTabLocks: boolean;
  constructor(private db: RecorderIdbDatabase) {
    this.crossTabLocks = typeof (globalThis as unknown as {
      navigator?: { locks?: { request?: (...args: any[]) => Promise<unknown> } };
    }).navigator?.locks?.request === "function";
  }

  list(limit = MAX_RECOVERABLE_SPOOLS): Promise<RecoverableSpoolSummary[]> {
    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(RECOVERY_STORE, "readonly");
      const out: RecoverableSpoolSummary[] = [];
      const request = transaction.objectStore(RECOVERY_STORE).openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || out.length >= limit) return;
        const value = cursor.value as RecorderRecoveryRecord;
        if (typeof value?.recording === "string") {
          out.push({
            recordingId: value.recording,
            generation: typeof value.generation === "number" ? value.generation : null,
            finalSampleCount: typeof value.finalSampleCount === "number" ? value.finalSampleCount : null,
            captureLostSamples: Math.max(0, value.captureLostSamples ?? 0),
            discardedSamples: Math.max(0, value.discardedSamples ?? 0),
          });
        }
        cursor.continue();
      };
      transaction.oncomplete = () => resolve(out);
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB recovery list failed"));
    });
  }

  async withLock<T>(recordingId: string, run: () => Promise<T>): Promise<T | undefined> {
    const locks = (globalThis as unknown as {
      navigator?: {
        locks?: {
          request?: (
            name: string,
            options: { ifAvailable: boolean },
            callback: (lock: unknown) => Promise<T | undefined>,
          ) => Promise<T | undefined>;
        };
      };
    }).navigator?.locks;
    if (!locks?.request) return undefined;
    return locks.request(`nexus-rec-spool:${recordingId}`, { ifAvailable: true }, async (lock) => {
      if (!lock) return undefined;
      return run();
    });
  }
}

export async function createIndexedDbRecoveryIndex(): Promise<RecoverableSpoolIndex> {
  try {
    return new IndexedDbRecoveryIndex(await openSpoolDatabase());
  } catch {
    return memoryRecoveryIndex();
  }
}

/** IndexedDB spool for a new recording, or a capped memory-only spool with the exact plan wording. */
async function storageGuarantee(): Promise<"persistent" | "best-effort"> {
  const storage = (globalThis as unknown as {
    navigator?: {
      storage?: {
        persisted?: () => Promise<boolean>;
        persist?: () => Promise<boolean>;
      };
    };
  }).navigator?.storage;
  if (!storage) return "best-effort";
  try { if (storage.persisted && await storage.persisted()) return "persistent"; } catch { /* ask below */ }
  try { if (storage.persist && await storage.persist()) return "persistent"; } catch { /* best effort */ }
  return "best-effort";
}

export async function createIndexedDbRecorderSpool(recordingId: string): Promise<RecorderSpool> {
  if (await quotaWarning()) return memoryRecorderSpool();
  try {
    const guarantee = await storageGuarantee();
    return new IndexedDbRecorderSpool(await openSpoolDatabase(), recordingId, guarantee);
  } catch {
    return memoryRecorderSpool();
  }
}

export async function createIndexedDbPendingStarts(): Promise<RecorderPendingStore> {
  const guarantee = await storageGuarantee();
  if (guarantee !== "persistent" || await quotaWarning()) {
    return memoryPendingStore("Persistent browser storage is required for the recorder", guarantee);
  }
  try {
    const db = await openSpoolDatabase();
    return new IndexedDbPendingStore(db, guarantee);
  } catch {
    return memoryPendingStore("Persistent browser storage is required for the recorder", "memory");
  }
}

export interface WebRecorderOptions {
  clientId: string;
  streamId(): string | null;
  title?: string;
  onChange?: (snapshot: RecorderSnapshot) => void;
  heartbeatMs?: number;
}

/** Wire the real web capture, Core API and IndexedDB spool. Construction still opens no microphone. */
export function createWebRecorder(options: WebRecorderOptions): RecorderController {
  return new RecorderController({
    openMicrophone: (onSamples) => openRecorder(onSamples, "recorder"),
    api: createWebRecorderApi(),
    digest: sha256Hex,
    spool: createIndexedDbRecorderSpool,
    pendingStarts: createIndexedDbPendingStarts,
    recoverableSpools: createIndexedDbRecoveryIndex,
    ...options,
  });
}
