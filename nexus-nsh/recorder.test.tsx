import { describe, expect, test } from "nsh/test";
import type { RecorderChunk, RecorderMicModule, RecorderStart, RecorderStop } from "nexus/native";
import { NativeRecorderCapture } from "./src/recorder-native";
import { RecorderController, base64url, memoryRecorderSpool } from "./src/recorder";
import type {
  RecorderApi,
  RecorderCaptureFailure,
  RecorderCaptureHandlers,
  RecorderCaptureLoss,
  RecorderDependencies,
  RecorderPublicState,
  RecorderRecoveryRecord,
  RecorderSpoolEntry,
  RecorderSpoolGuarantee,
  RecoverableSpoolSummary,
  PendingRecorderStart,
} from "./src/recorder";
import { RecorderHttpError } from "./src/recorder";
import { RECORDER_MEMORY_ONLY_WARNING } from "./src/recorder-logic";

const RECORDING_ID = "a".repeat(32);
const RECOVERY = base64url(new Uint8Array(32).fill(7));
const wait = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(check: () => boolean, message: string, ms = 3000) {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out: ${message}`);
    await wait(5);
  }
}

function digest(bytes: Uint8Array): string {
  let hash = 2166136261;
  for (const byte of bytes) hash = Math.imul(hash ^ byte, 16777619) >>> 0;
  return hash.toString(16).padStart(8, "0").repeat(8);
}

/** Deterministic synthetic audio; no device is ever opened. */
function speech(seconds: number, rate = 16_000): Float32Array {
  const samples = new Float32Array(Math.round(seconds * rate));
  for (let i = 0; i < samples.length; i++) samples[i] = Math.sin(i / 31) * 0.2;
  return samples;
}

type RigOptions = {
  rate?: number;
  failChunks?: number;
  heartbeatError?: { status: number; code: string } | null;
  blockUploads?: boolean;
  spoolDurable?: boolean;
  spoolPersistence?: RecorderSpoolGuarantee;
  spoolWarning?: string | null;
  spoolLimitSamples?: number;
  onMicStop?: () => void;
  recovery?: RecorderRecoveryRecord | null;
  spoolEntries?: RecorderSpoolEntry[];
  recoveryRecords?: RecoverableSpoolSummary[];
  recoveryLocked?: boolean;
  recoveryLocks?: boolean;
  detachChunks?: number;
  detachHeartbeats?: number;
  blockRebind?: boolean;
  blockCreate?: boolean;
  createError?: string;
  createErrorCode?: string;
  resumeState?: Partial<RecorderPublicState>;
  resumeError?: { status: number; code: string } | null;
  pendingPutError?: string;
  pendingGuarantee?: RecorderSpoolGuarantee;
  lockHeld?: boolean;
  blockSpool?: boolean;
  maxUploadAttempts?: number;
  stopError?: string;
};

function rig(options: RigOptions = {}) {
  let opened = 0;
  let micStopped = 0;
  let emit: ((samples: Float32Array) => void) | null = null;
  let captureHandlers: RecorderCaptureHandlers | null = null;
  let clock = 1_000;
  let durable = 0;
  let serverActive = true;
  let serverSealed = false;
  let serverFinal: number | null = null;
  let failuresLeft = options.failChunks ?? 0;
  let chunkDetachesLeft = options.detachChunks ?? 0;
  let heartbeatDetachesLeft = options.detachHeartbeats ?? 0;
  let stopCalls = 0;
  let stopFinal: number | null = null;
  let heartbeatCalls = 0;
  let getCalls = 0;
  let rebindCalls = 0;
  let recoverStopCalls = 0;
  const recoverChunks: { seq: number; start: number }[] = [];
  let rebindStream = "";
  let streamId = "stream-1";
  const events: string[] = [];
  const createInputs: any[] = [];
  const puts: { seq: number; start: number; body: Uint8Array; sha256: string; stopped: boolean }[] = [];
  const pendingRecords = new Map<string, PendingRecorderStart>();
  const pendingGuarantee = options.pendingGuarantee ?? "persistent";
  const pendings = {
    durable: pendingGuarantee === "persistent",
    guarantee: pendingGuarantee,
    warning: pendingGuarantee === "persistent" ? null : "Persistent browser storage is required for the recorder",
    crossTabLocks: false,
    put(record: PendingRecorderStart) {
      if (options.pendingPutError) throw new Error(options.pendingPutError);
      pendingRecords.set(record.key, { ...record });
    },
    delete(key: string) { pendingRecords.delete(key); },
    list() { return [...pendingRecords.values()].map((record) => ({ ...record })); },
    async withLock<T>(_key: string, run: () => Promise<T>) {
      if (options.lockHeld) return undefined;
      if (pendingGuarantee && pendings.crossTabLocks || options.blockSpool || options.blockCreate) {
        if (lockBusy) return undefined;
        lockBusy = true;
        try { return await run(); } finally { lockBusy = false; }
      }
      return run();
    },
  };
  const spooled = new Map<number, RecorderSpoolEntry>(
    (options.spoolEntries ?? []).map((entry) => [entry.seq, entry]),
  );
  let recoveryMeta: RecorderRecoveryRecord | null = options.recovery ?? null;
  const recoveryRecords = [...(options.recoveryRecords ?? [])];
  let recoveryLocked = options.recoveryLocked ?? false;
  let recoveryLockCalls = 0;
  let releaseUploads: (() => void) | null = null;
  let releaseRebind: (() => void) | null = null;
  let releaseCreate: (() => void) | null = null;
  let releaseSpool: (() => void) | null = null;
  let lockBusy = false;
  const spoolGate = options.blockSpool
    ? new Promise<void>((resolve) => { releaseSpool = resolve; })
    : Promise.resolve();
  const createGate = options.blockCreate
    ? new Promise<void>((resolve) => { releaseCreate = resolve; })
    : Promise.resolve();
  const gate = options.blockUploads
    ? new Promise<void>((resolve) => { releaseUploads = resolve; })
    : Promise.resolve();
  const rebindGate = options.blockRebind
    ? new Promise<void>((resolve) => { releaseRebind = resolve; })
    : Promise.resolve();

  const state = (overrides: Partial<RecorderPublicState> = {}): RecorderPublicState => ({
    id: RECORDING_ID,
    generation: 1,
    durable_offset: durable,
    capture_state: serverActive ? "recording" : serverSealed ? "stopped" : "interrupted",
    active: serverActive,
    sealed: serverSealed,
    final_sample_count: serverFinal,
    complete: serverSealed && serverFinal !== null && durable >= serverFinal,
    holes: 0,
    ...overrides,
  });

  const openMicrophone = async (
    onSamples: (samples: Float32Array) => void,
    handlers?: RecorderCaptureHandlers,
  ) => {
    opened++;
    emit = onSamples;
    captureHandlers = handlers ?? null;
    return {
      rate: options.rate ?? 16_000,
      recorder: {
        async stop() {
          micStopped++;
          events.push("mic-stop");
          options.onMicStop?.();
          if (options.stopError) throw new Error(options.stopError);
        },
      },
    };
  };

  const api: RecorderApi = {
    async create(input) {
      createInputs.push({ ...input });
      events.push(input.mode === "resume" ? "resume" : "create");
      await createGate;
      if (options.createError) throw new RecorderHttpError(400, options.createErrorCode ?? "recorder_create_failed", options.createError);
      if (input.mode === "resume") {
        if (options.resumeError) throw new RecorderHttpError(options.resumeError.status, options.resumeError.code);
        return {
          ...state(),
          lease_token: "lease",
          recovery_token: null,
          recovery: "client",
          ...options.resumeState,
        };
      }
      return {
        ...state(),
        lease_token: "lease",
        recovery_token: null,
        recovery: "client",
      };
    },
    async get() {
      getCalls++;
      return state();
    },
    async putChunk(input) {
      events.push(`put-${input.seq}`);
      puts.push({ seq: input.seq, start: input.start, body: input.body.slice(), sha256: input.sha256, stopped: micStopped > 0 });
      await gate;
      if (chunkDetachesLeft > 0) {
        chunkDetachesLeft--;
        throw new RecorderHttpError(409, "stream_detached");
      }
      if (failuresLeft > 0) {
        failuresLeft--;
        throw new RecorderHttpError(503, "recorder_backpressure");
      }
      durable = Math.max(durable, input.start + Math.floor(input.body.length / 2));
      return { recording: state({ durable_offset: durable }) };
    },
    async heartbeat() {
      heartbeatCalls++;
      if (options.heartbeatError) throw new RecorderHttpError(options.heartbeatError.status, options.heartbeatError.code);
      if (heartbeatDetachesLeft > 0) {
        heartbeatDetachesLeft--;
        throw new RecorderHttpError(409, "stream_detached");
      }
      return state({ durable_offset: durable });
    },
    async rebind(input) {
      rebindCalls++;
      rebindStream = input.streamId;
      await rebindGate;
      return state({ durable_offset: durable });
    },
    async stop(input) {
      stopCalls++;
      stopFinal = input.finalSampleCount;
      events.push("stop");
      serverActive = false;
      serverSealed = true;
      serverFinal = input.finalSampleCount;
      const complete = durable >= input.finalSampleCount;
      return state({
        durable_offset: durable,
        final_sample_count: input.finalSampleCount,
        active: false,
        sealed: true,
        complete,
        holes: complete ? 0 : 1,
        capture_state: "stopped",
      });
    },
    async recoverStop(input) {
      recoverStopCalls++;
      if (serverActive) throw new RecorderHttpError(409, "recording_active");
      serverSealed = true;
      serverFinal = input.finalSampleCount;
      const complete = durable >= input.finalSampleCount;
      return state({
        durable_offset: durable,
        final_sample_count: input.finalSampleCount,
        active: false,
        sealed: true,
        complete,
        holes: complete ? 0 : 1,
        capture_state: "stopped",
      });
    },
    async recoverChunk(input) {
      recoverChunks.push({ seq: input.seq, start: input.start });
      durable = Math.max(durable, input.start + Math.floor(input.body.length / 2));
      return { recording: state({ durable_offset: durable, final_sample_count: serverFinal }) };
    },
  };

  const deps: RecorderDependencies = {
    openMicrophone,
    api,
    digest: async (bytes) => digest(bytes),
    pendingStarts: () => pendings,
    recoverableSpools: () => ({
      crossTabLocks: options.recoveryLocks ?? true,
      list(limit = 50) { return recoveryRecords.slice(0, limit); },
      async withLock<T>(_id: string, run: () => Promise<T>) {
        recoveryLockCalls += 1;
        return recoveryLocked ? undefined : run();
      },
    }),
    randomBytes: () => new Uint8Array(32).fill(7),
    spool: async (recordingId: string) => {
      await spoolGate;
      const guarantee = options.spoolPersistence
        ?? ((options.spoolDurable ?? true) ? "persistent" : "memory");
      return {
      durable: guarantee === "persistent",
      guarantee,
      warning: options.spoolWarning ?? null,
      put(entry: RecorderSpoolEntry) { spooled.set(entry.seq, entry); expect(entry.recording).toBe(recordingId); },
      remove(seq: number) { spooled.delete(seq); },
      clear() { spooled.clear(); },
      load(limitSamples = Number.MAX_SAFE_INTEGER) {
        const out: RecorderSpoolEntry[] = [];
        let used = 0;
        for (const entry of [...spooled.values()].sort((a, b) => a.seq - b.seq)) {
          if (used + entry.samples > limitSamples) break;
          out.push(entry);
          used += entry.samples;
        }
        return out;
      },
      loadRecovery() { return recoveryMeta; },
      saveRecovery(record: RecorderRecoveryRecord) { recoveryMeta = { ...record }; },
      clearRecovery() { recoveryMeta = null; },
      };
    },
    streamId: () => streamId,
    clientId: "client-1",
    now: () => clock,
    heartbeatMs: 0,
    retryDelaysMs: [1, 1, 1],
    spoolLimitSamples: options.spoolLimitSamples,
    maxUploadAttempts: options.maxUploadAttempts,
    newKey: () => "key-1",
    onChange: () => {},
  };

  return {
    deps,
    events,
    puts,
    spooled,
    pendingRecords,
    pendings,
    createInputs,
    recoveryRecords,
    get recoveryLocked() { return recoveryLocked; },
    set recoveryLocked(value: boolean) { recoveryLocked = value; },
    get recoveryLockCalls() { return recoveryLockCalls; },
    get lockBusy() { return lockBusy; },
    releaseSpool() { releaseSpool?.(); },
    get recoveryMeta() { return recoveryMeta; },
    get opened() { return opened; },
    get micStopped() { return micStopped; },
    get stopCalls() { return stopCalls; },
    get stopFinal() { return stopFinal; },
    get heartbeatCalls() { return heartbeatCalls; },
    get getCalls() { return getCalls; },
    get rebindCalls() { return rebindCalls; },
    get rebindStream() { return rebindStream; },
    get recoverStopCalls() { return recoverStopCalls; },
    get recoverChunks() { return recoverChunks; },
    get serverDurable() { return durable; },
    set serverDurable(value: number) { durable = value; },
    set serverActive(value: boolean) { serverActive = value; },
    set serverSealed(value: boolean) { serverSealed = value; },
    set serverFinal(value: number | null) { serverFinal = value; },
    set streamId(value: string) { streamId = value; },
    set clock(value: number) { clock = value; },
    emit(samples: Float32Array) { if (!emit) throw new Error("microphone is not open"); emit(samples); },
    emitLoss(loss: RecorderCaptureLoss) { captureHandlers?.onLoss?.(loss); },
    emitError(failure: RecorderCaptureFailure) { captureHandlers?.onError?.(failure); },
    releaseUploads() { releaseUploads?.(); },
    releaseRebind() { releaseRebind?.(); },
    releaseCreate() { releaseCreate?.(); },
  };
}

describe("NSH web recorder controller", () => {
  test("opens no microphone on construction; capture is not saved before Core acknowledges it", async () => {
    const r = rig({ blockUploads: true });
    const recorder = new RecorderController(r.deps);
    expect(r.opened).toBe(0);
    expect(recorder.snapshot().phase).toBe("idle");
    expect(recorder.snapshot().savedSeconds).toBe(0);

    await recorder.start();
    expect(r.opened).toBe(1);
    expect(recorder.snapshot().phase).toBe("recording");
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "the first upload starts");

    const waiting = recorder.snapshot();
    expect(waiting.level).toBeGreaterThan(0);
    expect(waiting.capturedSamples).toBe(16_000);
    expect(waiting.ackedSamples).toBe(0);
    expect(waiting.savedSeconds).toBe(0);
    expect(waiting.pendingSamples).toBe(16_000);
    expect(waiting.complete).toBe(false);
    expect(r.spooled.has(0)).toBe(true);

    r.releaseUploads();
    await until(() => recorder.snapshot().ackedSamples === 16_000, "the chunk is acknowledged");
    expect(recorder.snapshot().savedSeconds).toBe(1);
  });

  test("does not open the microphone while Core create is delayed", async () => {
    const r = rig({ blockCreate: true });
    const recorder = new RecorderController(r.deps);
    const starting = recorder.start();
    await wait(10);
    expect(r.opened).toBe(0);
    expect(recorder.snapshot().phase).toBe("starting");
    r.releaseCreate();
    await starting;
    expect(r.opened).toBe(1);
    expect(recorder.snapshot().phase).toBe("recording");
  });

  test("does not open the microphone when Core rejects create", async () => {
    const r = rig({ createError: "recording rejected" });
    const recorder = new RecorderController(r.deps);
    const snapshot = await recorder.start();
    expect(r.opened).toBe(0);
    expect(snapshot.phase).toBe("failed");
    expect(snapshot.failure).toBe("create");
    expect(snapshot.lastError).toBe("recording rejected");
    expect(r.micStopped).toBe(0);
  });

  test("chunks one-second blocks at integer offsets and releases the microphone before the final drain", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(2.5));
    await until(() => r.puts.length === 2, "two full chunks upload");

    await recorder.stop();
    const snapshot = recorder.snapshot();
    expect(r.stopCalls).toBe(1);
    expect(r.stopFinal).toBe(40_000);
    expect(snapshot.phase).toBe("stopped");
    expect(snapshot.level).toBe(0);
    expect(snapshot.sealed).toBe(true);
    expect(snapshot.complete).toBe(true);
    expect(snapshot.lossless).toBe(true);
    expect(snapshot.ackedSamples).toBe(40_000);
    expect(snapshot.savedSeconds).toBe(2.5);
    expect(snapshot.capturedSamples).toBe(40_000);

    expect(r.puts.map((put) => (put.seq))).toEqual([0, 1, 2]);
    expect(r.puts.map((put) => put.start)).toEqual([0, 16_000, 32_000]);
    expect(r.puts.map((put) => put.body.length)).toEqual([32_000, 32_000, 16_000]);
    expect(r.puts[2].stopped).toBe(true);

    const micStop = r.events.indexOf("mic-stop");
    const seal = r.events.indexOf("stop");
    expect(micStop).toBeGreaterThanOrEqual(0);
    expect(seal).toBeGreaterThan(micStop);
    expect(r.events.lastIndexOf("put-2")).toBeGreaterThan(micStop);

    await recorder.stop();
    expect(recorder.snapshot().phase).toBe("stopped");
  });

  test("resamples a 44.1 kHz microphone stream into 16 kHz integer offsets", async () => {
    const r = rig({ rate: 44_100 });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(2, 44_100));
    await until(() => r.puts.length === 2, "both resampled chunks upload");

    expect(r.puts.map((put) => put.seq)).toEqual([0, 1]);
    expect(r.puts.map((put) => put.start)).toEqual([0, 16_000]);
    expect(r.puts.map((put) => put.body.length)).toEqual([32_000, 32_000]);
    expect(recorder.snapshot().capturedSamples).toBe(32_000);
  });

  test("retries the identical chunk sequentially before advancing the sequence", async () => {
    const r = rig({ failChunks: 2 });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => recorder.snapshot().ackedSamples === 16_000, "the retried chunk is acknowledged");

    expect(r.puts.map((put) => put.seq)).toEqual([0, 0, 0]);
    expect(new Set(r.puts.map((put) => put.start))).toEqual(new Set([0]));
    expect(new Set(r.puts.map((put) => put.sha256)).size).toBe(1);
    expect(r.puts[0].body).toEqual(r.puts[2].body);

    r.emit(speech(1));
    await until(() => r.puts.some((put) => put.seq === 1), "the next sequence advances");
    expect(r.puts.filter((put) => put.seq === 1)).toHaveLength(1);
  });

  test("a lease loss from the heartbeat stops the microphone and never seals with the dead lease", async () => {
    const r = rig({ heartbeatError: { status: 409, code: "lease_expired" } });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await recorder.heartbeatNow();

    const snapshot = recorder.snapshot();
    expect(snapshot.phase).toBe("interrupted");
    expect(snapshot.lease).toBe("lost");
    expect(snapshot.failure).toBe("lease_lost");
    expect(r.micStopped).toBe(1);
    expect(r.stopCalls).toBe(0);
    expect(snapshot.recoverable).toBe(true);
    expect(r.recoveryMeta?.recoveryToken).toBe(RECOVERY);
    expect(JSON.stringify(snapshot)).not.toContain(RECOVERY);
  });

  test("a recorder.state interruption event stops capture; another generation is ignored", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "capture is running");

    recorder.handleRecorderEvent({
      type: "recorder.state",
      data: { id: RECORDING_ID, generation: 2, active: false, capture_state: "interrupted" },
    });
    expect(recorder.snapshot().phase).toBe("recording");
    expect(r.micStopped).toBe(0);

    recorder.handleRecorderEvent({
      type: "recorder.state",
      data: { id: RECORDING_ID, generation: 1, active: false, capture_state: "interrupted", reason: "lease_expired" },
    });
    await until(() => recorder.snapshot().phase === "interrupted", "the matching interruption stops capture");
    expect(r.micStopped).toBe(1);
    expect(r.stopCalls).toBe(0);
  });

  test("an exhausted spool stops capture and reports the exact unsaved range", async () => {
    const r = rig({ blockUploads: true, spoolLimitSamples: 32_000 });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "the first chunk is in flight");
    r.emit(speech(1));
    await until(() => recorder.snapshot().pendingSamples === 32_000, "the spool reaches its bound");
    r.emit(speech(1));
    await until(() => recorder.snapshot().phase === "stopping" && r.micStopped === 1, "capture stops on overflow");

    const stopped = recorder.snapshot();
    expect(r.micStopped).toBe(1);
    expect(stopped.unsavedSamples).toBe(16_000);
    expect(stopped.losses).toEqual([{ start: 32_000, end: 48_000, samples: 16_000, reason: "spool_overflow" }]);
    expect(stopped.complete).toBe(false);
    expect(stopped.savedSeconds).toBe(0);

    r.releaseUploads();
    await until(() => recorder.snapshot().phase === "stopped", "the bounded spool drains");
    const final = recorder.snapshot();
    expect(final.capturedSamples).toBe(48_000);
    expect(final.ackedSamples).toBe(32_000);
    expect(final.unsavedSamples).toBe(16_000);
    expect(final.complete).toBe(false);
    expect(final.lossless).toBe(false);
    expect(r.stopFinal).toBe(48_000);
  });

  test("reports capture-callback starvation as a sample gap instead of filling silence", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.clock = 1_000;
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "the first block uploads");
    r.clock = 3_500;
    r.emit(speech(0.5));

    const snapshot = recorder.snapshot();
    expect(snapshot.gaps).toHaveLength(1);
    expect(snapshot.gaps[0].afterSample).toBe(16_000);
    expect(snapshot.gaps[0].missingSamples).toBe(32_000);
    expect(snapshot.gaps[0].seconds).toBe(2);
    expect(snapshot.lossless).toBe(false);
  });

  test("uses the exact memory-only warning when the browser cannot provide a durable spool", async () => {
    const r = rig({ spoolDurable: false, spoolWarning: RECORDER_MEMORY_ONLY_WARNING });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    expect(recorder.snapshot().persistentSpool).toBe(false);
    expect(recorder.snapshot().storageWarning).toBe(RECORDER_MEMORY_ONLY_WARNING);
  });

  test("deduplicates same-generation recorder.state revisions before applying offsets or interruption", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    recorder.handleRecorderEvent({
      type: "recorder.state",
      data: { id: RECORDING_ID, generation: 1, revision: 5, durable_offset: 32_000, active: true },
    });
    recorder.handleRecorderEvent({
      type: "recorder.state",
      data: {
        id: RECORDING_ID, generation: 1, revision: 4, durable_offset: 16_000,
        active: false, capture_state: "interrupted", reason: "lease_expired",
      },
    });
    expect(recorder.snapshot().phase).toBe("recording");
    expect(recorder.snapshot().ackedSamples).toBe(32_000);
    expect(r.micStopped).toBe(0);
  });

  test("reconnect rebinds and adopts the authoritative checkpoint, dropping acknowledged spooled chunks", async () => {
    const r = rig({ blockUploads: true });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "the chunk is in flight");
    r.serverDurable = 16_000;
    r.streamId = "stream-2";

    const snapshot = await recorder.reconnect();
    expect(r.getCalls).toBe(1);
    expect(r.rebindCalls).toBe(1);
    expect(r.rebindStream).toBe("stream-2");
    expect(snapshot.ackedSamples).toBe(16_000);
    expect(snapshot.pendingSamples).toBe(0);
    expect(r.spooled.size).toBe(0);
    r.releaseUploads();
  });

  test("reconnect reports an authoritative interruption instead of uploading to a dead lease", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.serverActive = false;
    const snapshot = await recorder.reconnect();
    expect(snapshot.phase).toBe("interrupted");
    expect(snapshot.lease).toBe("lost");
    expect(r.micStopped).toBe(1);
    expect(r.rebindCalls).toBe(0);
    expect(r.stopCalls).toBe(0);
  });

  test("dispose flushes the partial tail into spool metadata, then reload restores and recovers it", async () => {
    const r = rig();
    const first = new RecorderController(r.deps);
    await first.start();
    r.emit(speech(0.5));
    await first.dispose();

    const interrupted = first.snapshot();
    expect(interrupted.phase).toBe("interrupted");
    expect(interrupted.failure).toBe("disposed");
    expect(r.micStopped).toBe(1);
    expect(r.stopCalls).toBe(0);
    expect(interrupted.pendingSamples).toBe(8_000);
    expect(interrupted.capturedSamples).toBe(8_000);
    expect(interrupted.recoverable).toBe(true);
    expect([...r.spooled.values()].map((entry) => [entry.seq, entry.start, entry.samples])).toEqual([[0, 0, 8_000]]);
    expect(r.recoveryMeta).toMatchObject({ recording: RECORDING_ID, finalSampleCount: 8_000 });
    expect(r.recoveryMeta?.recoveryToken).toBe(RECOVERY);
    expect(JSON.stringify(interrupted)).not.toContain(RECOVERY);

    // A reload cannot own the dead capture; the server eventually reports it interrupted.
    r.serverActive = false;
    const second = new RecorderController(r.deps);
    const restored = await second.restore(RECORDING_ID);
    expect(restored.phase).toBe("interrupted");
    expect(restored.ackedSamples).toBe(0);
    expect(restored.pendingSamples).toBe(8_000);
    expect(restored.recoverable).toBe(true);
    expect(JSON.stringify(restored)).not.toContain(RECOVERY);

    const recovered = await second.recover();
    expect(recovered.phase).toBe("stopped");
    expect(recovered.ackedSamples).toBe(8_000);
    expect(recovered.pendingSamples).toBe(0);
    expect(recovered.complete).toBe(true);
    expect(recovered.recoverable).toBe(false);
    expect(r.recoverStopCalls).toBe(1);
    expect(r.recoverChunks).toEqual([{ seq: 0, start: 0 }]);
    expect(r.spooled.size).toBe(0);
    expect(r.recoveryMeta).toBe(null);
    expect(r.stopCalls).toBe(0);
  });

  test("recovery refuses while the recording is still active and keeps the spool", async () => {
    const r = rig();
    const first = new RecorderController(r.deps);
    await first.start();
    r.emit(speech(0.5));
    await first.dispose();

    const second = new RecorderController(r.deps);
    await second.restore(RECORDING_ID);
    const snapshot = await second.recover();
    expect(snapshot.phase).toBe("interrupted");
    expect(snapshot.failure).toBe("recovery_impossible");
    expect(snapshot.lastError).toBe("recording_active");
    expect(snapshot.recoverable).toBe(true);
    expect(r.recoverStopCalls).toBe(0);
    expect(r.recoverChunks).toEqual([]);
    expect(r.spooled.size).toBe(1);
  });

  test("a detached stream rebinds once for concurrent heartbeat and upload failures", async () => {
    const r = rig({ detachChunks: 1, detachHeartbeats: 1, blockRebind: true });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "the chunk upload detects a detached stream");
    const heartbeat = recorder.heartbeatNow();
    await until(() => r.rebindCalls === 1, "one shared rebind is in flight");
    r.releaseRebind();
    await heartbeat;
    await until(() => recorder.snapshot().ackedSamples === 16_000, "the chunk retries after rebind");

    expect(r.rebindCalls).toBe(1);
    expect(r.puts.map((put) => put.seq)).toEqual([0, 0]);
    expect(recorder.snapshot().phase).toBe("recording");
  });

  test("adopting an incomplete sealed recording keeps unacknowledged spooled audio", async () => {
    const r = rig({ blockUploads: true });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "the chunk is in flight");
    r.serverActive = false;
    r.serverSealed = true;
    r.serverFinal = 16_000;

    const adopted = await recorder.reconnect();
    expect(adopted.phase).toBe("stopped");
    expect(adopted.complete).toBe(false);
    expect(adopted.pendingSamples).toBe(16_000);
    expect(adopted.recoverable).toBe(true);
    expect(r.spooled.size).toBe(1);
    expect(r.recoveryMeta?.finalSampleCount).toBe(16_000);

    const recovered = await recorder.recover();
    expect(recovered.phase).toBe("stopped");
    expect(recovered.complete).toBe(true);
    expect(recovered.pendingSamples).toBe(0);
    expect(recovered.recoverable).toBe(false);
    expect(r.recoverChunks).toEqual([{ seq: 0, start: 0 }]);
    r.releaseUploads();
  });

  test("fails closed and retains the chunk when the authoritative checkpoint splits it", async () => {
    const r = rig({ blockUploads: true });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => r.puts.length === 1, "the chunk is in flight");
    r.serverDurable = 8_000;

    const snapshot = await recorder.reconnect();
    expect(snapshot.failure).toBe("checkpoint");
    expect(snapshot.lastError).toBe("chunk_sequence_mismatch");
    expect(snapshot.ackedSamples).toBe(8_000);
    expect(snapshot.pendingSamples).toBe(16_000);
    expect(r.spooled.size).toBe(1);
    expect(r.micStopped).toBe(1);
    expect(r.rebindCalls).toBe(0);
    r.releaseUploads();
  });

  test("keeps a failed sealed spool, then recovers it on the same controller", async () => {
    const r = rig({ failChunks: 10, maxUploadAttempts: 1 });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => recorder.snapshot().phase === "failed", "upload failure seals the capture");

    const failed = recorder.snapshot();
    expect(failed.complete).toBe(false);
    expect(failed.ackedSamples).toBe(0);
    expect(failed.pendingSamples).toBe(16_000);
    expect(failed.recoverable).toBe(true);
    expect(r.spooled.size).toBe(1);
    expect(r.recoveryMeta?.finalSampleCount).toBe(16_000);
    expect(r.stopFinal).toBe(16_000);

    const recovered = await recorder.recover();
    expect(recovered.phase).toBe("stopped");
    expect(recovered.complete).toBe(true);
    expect(recovered.ackedSamples).toBe(16_000);
    expect(recovered.pendingSamples).toBe(0);
    expect(recovered.recoverable).toBe(false);
    expect(r.recoverChunks).toEqual([{ seq: 0, start: 0 }]);
    expect(r.spooled.size).toBe(0);
    expect(r.recoveryMeta).toBe(null);
  });

  test("best-effort or evictable spool storage refuses capture and the microphone", async () => {
    for (const guarantee of ["best-effort", "memory"] as const) {
      const r = rig({ spoolPersistence: guarantee });
      const recorder = new RecorderController(r.deps);
      const snapshot = await recorder.start();
      expect(snapshot.phase).toBe("failed");
      expect(snapshot.failure).toBe("storage");
      expect(snapshot.persistentSpool).toBe(false);
      expect(snapshot.spoolGuarantee).toBe(guarantee);
      expect(r.opened).toBe(0);
    }
  });

  test("a native overflow loss is explicit and prevents a falsely contiguous seal", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => recorder.snapshot().ackedSamples === 16_000, "the delivered audio is saved");
    r.emitLoss({ start: 16_000, end: 19_000, samples: 3_000, reason: "overflow" });

    const live = recorder.snapshot();
    expect(live.captureLostSamples).toBe(3_000);
    expect(live.unsavedSamples).toBe(3_000);
    expect(live.losses).toEqual([{ start: 16_000, end: 19_000, samples: 3_000, reason: "native_overflow" }]);
    expect(live.complete).toBe(false);

    await recorder.stop();
    const stopped = recorder.snapshot();
    expect(r.stopFinal).toBe(19_000);
    expect(stopped.finalSampleCount).toBe(19_000);
    expect(stopped.capturedSamples).toBe(19_000);
    expect(stopped.ackedSamples).toBe(16_000);
    expect(stopped.savedSeconds).toBe(1);
    expect(stopped.unsavedSeconds).toBe(3 / 16);
    expect(stopped.serverComplete).toBe(false);
    expect(stopped.complete).toBe(false);
    expect(stopped.lossless).toBe(false);
  });

  test("cuts capture on the first native loss and never uploads later audio contiguously", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => recorder.snapshot().ackedSamples === 16_000, "pre-loss audio is saved");
    r.emitLoss({ start: 16_000, end: 19_000, samples: 3_000, reason: "overflow" });
    r.emit(speech(1));

    await recorder.stop();
    const snapshot = recorder.snapshot();
    expect(snapshot.discardedSamples).toBe(16_000);
    expect(snapshot.captureLostSamples).toBe(3_000);
    expect(snapshot.capturedSamples).toBe(19_000);
    expect(snapshot.ackedSamples).toBe(16_000);
    expect(snapshot.pendingSamples).toBe(0);
    expect(r.puts.map((put) => [put.seq, put.start])).toEqual([[0, 0]]);
    expect(r.stopFinal).toBe(19_000);
    expect(snapshot.serverComplete).toBe(false);
    expect(snapshot.complete).toBe(false);
    expect(snapshot.lossless).toBe(false);
  });

  test("freezes the sealed endpoint at the first cut and counts later drain loss as discarded", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => recorder.snapshot().ackedSamples === 16_000, "pre-cut audio is saved");
    r.emitLoss({ start: 16_000, end: 19_000, samples: 3_000, reason: "overflow" });
    r.emit(speech(1));
    r.emitLoss({ start: 35_000, end: 37_000, samples: 2_000, kind: "overflow" });

    await recorder.stop();
    const snapshot = recorder.snapshot();
    expect(snapshot.captureLostSamples).toBe(3_000);
    expect(snapshot.discardedSamples).toBe(18_000);
    expect(snapshot.discardedLossSamples).toBe(2_000);
    expect(snapshot.capturedSamples).toBe(19_000);
    expect(snapshot.finalSampleCount).toBe(19_000);
    expect(snapshot.ackedSamples).toBe(16_000);
    expect(r.stopFinal).toBe(19_000);
    expect(r.puts.map((put) => [put.seq, put.start])).toEqual([[0, 0]]);
    expect(snapshot.losses).toEqual([
      { start: 16_000, end: 19_000, samples: 3_000, reason: "native_overflow" },
      { start: 35_000, end: 37_000, samples: 2_000, reason: "native_after_cut" },
    ]);
    expect(snapshot.serverComplete).toBe(false);
    expect(snapshot.complete).toBe(false);
  });

  test("accepts a native end-tail loss shape with an implied end", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => recorder.snapshot().ackedSamples === 16_000, "the delivered audio is saved");
    r.emitLoss({ kind: "end_tail", end: null, samples: 5 });
    await recorder.stop();
    expect(recorder.snapshot().losses).toEqual([
      { start: 16_000, end: 16_005, samples: 5, reason: "native_overflow" },
    ]);
    expect(r.stopFinal).toBe(16_005);
  });

  test("the actual native adapter forwards an overflow loss into the controller", async () => {
    let generation = 0;
    let seq = 0;
    let expected = 0;
    let delivered = 0;
    let dropped = 0;
    let chunks: RecorderChunk[] = [];
    const native: RecorderMicModule = {
      available: () => true,
      devices: () => ["Built-in Microphone"],
      async start(): Promise<RecorderStart> {
        generation += 1;
        return { generation, sampleRate: 16_000, channels: 1, frameSamples: 320, maxFrames: 1500 };
      },
      read(readGeneration: number): RecorderChunk {
        if (readGeneration !== generation) throw new Error("wrong generation");
        const next = chunks.shift();
        if (next) return next;
        return {
          generation, seq: seq++, sampleOffset: expected, samples: 0,
          bytes: new Uint8Array(), droppedSamples: dropped, droppedSincePrevious: 0,
        };
      },
      stop(): RecorderStop {
        return {
          generation, capturedSamples: delivered + dropped, deliveredSamples: delivered,
          droppedSamples: dropped, bufferedFrames: 0, tailSamples: 0, tailBytes: new Uint8Array(), error: null,
        };
      },
      level: () => 0,
    } as RecorderMicModule;
    const frame = (): Uint8Array => {
      const bytes = new Uint8Array(640);
      for (let i = 0; i < 320; i++) new DataView(bytes.buffer).setInt16(i * 2, 4096, true);
      return bytes;
    };
    chunks = [
      { generation: 1, seq: 0, sampleOffset: 0, samples: 320, bytes: frame(), droppedSamples: 0, droppedSincePrevious: 0 },
      { generation: 1, seq: 1, sampleOffset: 960, samples: 320, bytes: frame(), droppedSamples: 640, droppedSincePrevious: 640 },
    ];
    let capture: NativeRecorderCapture | null = null;
    const r = rig();
    const recorder = new RecorderController({
      ...r.deps,
      openMicrophone: async (onSamples, handlers) => {
        capture = new NativeRecorderCapture({
          native,
          timers: { setInterval: () => 1, clearInterval: () => {} },
          onSamples,
          onLoss: handlers?.onLoss,
          onError: handlers?.onError,
        });
        const start = await capture.start();
        return {
          rate: start.sampleRate,
          recorder: {
            async stop() {
              const result = await capture!.stop();
              if (result.error) throw new Error(result.error);
            },
          },
          capture,
        };
      },
    });
    await recorder.start();
    capture!.drainNow();
    delivered += 320;
    expected = 320;
    capture!.drainNow();
    delivered += 320;
    expected = 1280;
    dropped = 640;

    const live = recorder.snapshot();
    expect(live.captureLostSamples).toBe(640);
    expect(live.discardedSamples).toBe(320);
    expect(live.losses).toEqual([{ start: 320, end: 960, samples: 640, reason: "native_overflow" }]);
    expect(live.complete).toBe(false);

    await recorder.stop();
    const stopped = recorder.snapshot();
    expect(r.stopFinal).toBe(960);
    expect(stopped.ackedSamples).toBe(320);
    expect(stopped.serverComplete).toBe(false);
    expect(stopped.complete).toBe(false);
    expect(stopped.lossless).toBe(false);
  });

  test("a native read failure stops capture promptly and never reports a clean seal", async () => {
    const r = rig();
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await until(() => recorder.snapshot().ackedSamples === 16_000, "the delivered audio is saved");
    r.emitError({ stage: "read", message: "native device read failed" });

    await until(() => recorder.snapshot().phase === "stopped", "capture stops after the read failure");
    const snapshot = recorder.snapshot();
    expect(r.micStopped).toBe(1);
    expect(r.stopCalls).toBe(1);
    expect(snapshot.failure).toBe("capture");
    expect(snapshot.lastError).toBe("native device read failed");
    expect(snapshot.serverComplete).toBe(true);
    expect(snapshot.complete).toBe(false);
    expect(snapshot.lossless).toBe(false);
  });

  test("a rejecting native stop is also treated as capture failure, not a clean seal", async () => {
    const r = rig({ stopError: "Recorder capture lost 10 samples to native buffer overflow" });
    const recorder = new RecorderController(r.deps);
    await recorder.start();
    r.emit(speech(1));
    await recorder.stop();
    const snapshot = recorder.snapshot();
    expect(snapshot.failure).toBe("capture");
    expect(snapshot.complete).toBe(false);
    expect(snapshot.serverComplete).toBe(true);
    expect(snapshot.ackedSamples).toBe(16_000);
  });

  test("persists the pending create intent before the POST and deletes it after the spool binds", async () => {
    const r = rig({ blockCreate: true });
    const recorder = new RecorderController(r.deps);
    const starting = recorder.start();
    await wait(10);
    expect(r.opened).toBe(0);
    expect(r.pendingRecords.size).toBe(1);
    expect([...r.pendingRecords.values()][0]).toMatchObject({ state: "creating", recordingId: null });
    expect(r.createInputs[0]).toMatchObject({ mode: "create", recovery: RECOVERY, idempotencyKey: "key-1" });
    expect(JSON.stringify(recorder.snapshot())).not.toContain(RECOVERY);
    r.releaseCreate();
    await starting;
    expect(r.pendingRecords.size).toBe(0);
    expect(r.opened).toBe(1);
  });

  test("best-effort or unavailable pending storage makes no Core call and opens no microphone", async () => {
    for (const guarantee of ["best-effort", "memory"] as const) {
      const r = rig({ pendingGuarantee: guarantee });
      const recorder = new RecorderController(r.deps);
      const snapshot = await recorder.start();
      expect(snapshot.phase).toBe("failed");
      expect(snapshot.failure).toBe("storage");
      expect(r.opened).toBe(0);
      expect(r.events).not.toContain("create");
      expect(r.pendingRecords.size).toBe(0);
    }
  });

  test("the cross-tab lock spans pending visibility through create and blocks recovery interleaving", async () => {
    const r = rig({ blockCreate: true });
    r.pendings.crossTabLocks = true;
    const recorder = new RecorderController(r.deps);
    const starting = recorder.start();
    await until(() => r.pendingRecords.size === 1, "the intent is visible inside the lock");
    expect(r.lockBusy).toBe(true);

    const otherTab = new RecorderController(r.deps);
    const outcomes = await otherTab.recoverPending();
    expect(outcomes[0].outcome).toBe("skipped");
    expect(r.events).not.toContain("resume");
    expect(r.pendingRecords.size).toBe(1);

    r.releaseCreate();
    await starting;
    expect(r.lockBusy).toBe(false);
    expect(r.pendingRecords.size).toBe(0);
    expect(r.opened).toBe(1);
  });

  test("the lock spans create response through durable spool binding and pending deletion", async () => {
    const r = rig({ blockSpool: true });
    r.pendings.crossTabLocks = true;
    const recorder = new RecorderController(r.deps);
    const starting = recorder.start();
    await until(() => r.events.includes("create") && r.pendingRecords.size === 1, "create returned, spool binding pending");
    expect(r.lockBusy).toBe(true);

    const otherTab = new RecorderController(r.deps);
    expect((await otherTab.recoverPending())[0].outcome).toBe("skipped");
    expect(r.events).not.toContain("resume");

    r.releaseSpool();
    await starting;
    expect(r.lockBusy).toBe(false);
    expect(r.pendingRecords.size).toBe(0);
    expect(r.opened).toBe(1);
  });

  test("a pending-intent write failure makes no Core call and opens no microphone", async () => {
    const r = rig({ pendingPutError: "IndexedDB is unavailable" });
    const recorder = new RecorderController(r.deps);
    const snapshot = await recorder.start();
    expect(snapshot.phase).toBe("failed");
    expect(snapshot.failure).toBe("storage");
    expect(r.opened).toBe(0);
    expect(r.events).not.toContain("create");
  });

  test("recoverPending stops an active orphan at its durable offset without opening the microphone", async () => {
    const r = rig({ resumeState: { active: true, capture_state: "interrupted", durable_offset: 0 } });
    const recorder = new RecorderController(r.deps);
    r.pendingRecords.set("pending-1", {
      key: "pending-1", recovery: RECOVERY, clientId: "client-1", title: null,
      createdAt: 1, state: "creating", recordingId: null,
    });
    const outcomes = await recorder.recoverPending();
    expect(outcomes).toEqual([{ key: "pending-1", outcome: "stopped", recordingId: RECORDING_ID }]);
    expect(r.events).toContain("resume");
    expect(r.createInputs[0]).toMatchObject({ mode: "resume", recovery: RECOVERY, idempotencyKey: "pending-1" });
    expect(r.stopCalls).toBe(1);
    expect(r.stopFinal).toBe(0);
    expect(r.opened).toBe(0);
    expect(r.pendingRecords.size).toBe(0);
  });

  test("recoverPending deletes a withdrawn create and keeps a transient one", async () => {
    const withdrawn = rig({ resumeError: { status: 404, code: "create_not_found" } });
    const first = new RecorderController(withdrawn.deps);
    withdrawn.pendingRecords.set("pending-1", {
      key: "pending-1", recovery: RECOVERY, clientId: "client-1", title: null,
      createdAt: 1, state: "creating", recordingId: null,
    });
    expect((await first.recoverPending())[0]).toMatchObject({ outcome: "deleted", code: "create_not_found" });
    expect(withdrawn.pendingRecords.size).toBe(0);

    const transient = rig({ resumeError: { status: 503, code: "recorder_unavailable" } });
    const second = new RecorderController(transient.deps);
    transient.pendingRecords.set("pending-2", {
      key: "pending-2", recovery: RECOVERY, clientId: "client-1", title: null,
      createdAt: 1, state: "creating", recordingId: null,
    });
    expect((await second.recoverPending())[0].outcome).toBe("kept");
    expect(transient.pendingRecords.size).toBe(1);
  });

  test("create_guard_full keeps the pending intent and surfaces restart required", async () => {
    const r = rig({ resumeError: { status: 503, code: "create_guard_full" } });
    const recorder = new RecorderController(r.deps);
    r.pendingRecords.set("pending-guard", {
      key: "pending-guard", recovery: RECOVERY, clientId: "client-1", title: null,
      createdAt: 1, state: "creating", recordingId: null,
    });
    const outcome = (await recorder.recoverPending())[0];
    expect(outcome).toMatchObject({ outcome: "kept", code: "create_guard_full", restartRequired: true });
    expect(r.pendingRecords.size).toBe(1);
    expect(r.opened).toBe(0);
  });

  test("a create_guard_full start keeps its intent and reports that Core must restart", async () => {
    const r = rig({ createError: "guard full", createErrorCode: "create_guard_full" });
    const recorder = new RecorderController(r.deps);
    const snapshot = await recorder.start();
    expect(snapshot.phase).toBe("failed");
    expect(snapshot.lastError).toContain("Core must be restarted");
    expect(r.pendingRecords.size).toBe(1);
    expect(r.opened).toBe(0);
  });

  test("recoverPending skips a pending start held by another tab", async () => {
    const r = rig({ lockHeld: true });
    r.pendings.crossTabLocks = true;
    const recorder = new RecorderController(r.deps);
    r.pendingRecords.set("pending-1", {
      key: "pending-1", recovery: RECOVERY, clientId: "client-1", title: null,
      createdAt: 1, state: "creating", recordingId: null,
    });
    const outcomes = await recorder.recoverPending();
    expect(outcomes[0].outcome).toBe("skipped");
    expect(r.events).not.toContain("resume");
    expect(r.pendingRecords.size).toBe(1);
  });

  test("recoverPending seals an interrupted unsealed create with the client recovery capability", async () => {
    const r = rig({ resumeState: { active: false, sealed: false, capture_state: "interrupted", durable_offset: 0 } });
    r.serverActive = false;
    const recorder = new RecorderController(r.deps);
    r.pendingRecords.set("pending-1", {
      key: "pending-1", recovery: RECOVERY, clientId: "client-1", title: null,
      createdAt: 1, state: "creating", recordingId: null,
    });
    const outcomes = await recorder.recoverPending();
    expect(outcomes[0].outcome).toBe("sealed");
    expect(r.recoverStopCalls).toBe(1);
    expect(r.stopCalls).toBe(0);
    expect(r.opened).toBe(0);
    expect(r.pendingRecords.size).toBe(0);
  });

  test("discovers and recovers a persisted spool after reload without a token leak or microphone", async () => {
    const entry: RecorderSpoolEntry = {
      recording: RECORDING_ID, seq: 0, start: 0, samples: 8_000,
      bytes: new Uint8Array(16_000), sha256: "0".repeat(64),
    };
    const summary: RecoverableSpoolSummary = {
      recordingId: RECORDING_ID, generation: 1, finalSampleCount: 8_000,
      captureLostSamples: 0, discardedSamples: 0,
    };
    const r = rig({
      recovery: {
        recording: RECORDING_ID, generation: 1, clientId: "client-1",
        recoveryToken: RECOVERY, finalSampleCount: 8_000,
      },
      spoolEntries: [entry],
      recoveryRecords: [summary],
    });
    r.serverActive = false;
    r.serverSealed = false;
    r.serverDurable = 0;
    const recorder = new RecorderController(r.deps);
    const listed = await recorder.listRecoverableSpools();
    expect(listed).toEqual([summary]);
    expect(JSON.stringify(listed)).not.toContain(RECOVERY);

    const outcomes = await recorder.recoverSpools();
    expect(outcomes).toEqual([{ recordingId: RECORDING_ID, outcome: "recovered", complete: true }]);
    expect(r.recoveryLockCalls).toBe(1);
    expect(r.recoverStopCalls).toBe(1);
    expect(r.recoverChunks).toEqual([{ seq: 0, start: 0 }]);
    expect(r.opened).toBe(0);
    expect(r.spooled.size).toBe(0);
    expect(r.recoveryMeta).toBe(null);
  });

  test("recoverSpools never touches an active capture or another locked tab", async () => {
    const summary: RecoverableSpoolSummary = {
      recordingId: RECORDING_ID, generation: 1, finalSampleCount: null,
      captureLostSamples: 0, discardedSamples: 0,
    };
    const active = rig({ recoveryRecords: [summary], spoolEntries: [] });
    const first = new RecorderController(active.deps);
    expect((await first.recoverSpools())[0].outcome).toBe("skipped_active");
    expect(active.recoverStopCalls).toBe(0);
    expect(active.recoverChunks).toEqual([]);
    expect(active.opened).toBe(0);

    const locked = rig({ recoveryRecords: [summary], recoveryLocked: true });
    const second = new RecorderController(locked.deps);
    expect((await second.recoverSpools())[0]).toMatchObject({ outcome: "skipped_locked" });
    expect(locked.getCalls).toBe(0);
    expect(locked.recoverStopCalls).toBe(0);
  });

  test("recoverSpools is bounded and refuses without cross-tab locks", async () => {
    const records = Array.from({ length: 60 }, (_, index) => ({
      recordingId: `${String(index).padStart(32, "0")}`,
      generation: 1,
      finalSampleCount: null,
      captureLostSamples: 0,
      discardedSamples: 0,
    }));
    const many = rig({ recoveryRecords: records });
    const recorder = new RecorderController(many.deps);
    const listed = await recorder.listRecoverableSpools(100);
    expect(listed).toHaveLength(50);

    const noLocks = rig({ recoveryRecords: [records[0]], recoveryLocks: false });
    const second = new RecorderController(noLocks.deps);
    expect((await second.recoverSpools())[0]).toMatchObject({
      outcome: "skipped_locked", code: "locks_unavailable",
    });
    expect(noLocks.recoveryLockCalls).toBe(0);
  });

  test("the memory spool bounds restore and retains recovery metadata locally", async () => {
    const spool = memoryRecorderSpool();
    const entry = (seq: number, start: number): RecorderSpoolEntry => ({
      recording: RECORDING_ID, seq, start, samples: 16_000,
      bytes: new Uint8Array(32_000), sha256: "0".repeat(64),
    });
    await spool.put(entry(1, 16_000));
    await spool.put(entry(0, 0));
    await spool.saveRecovery({
      recording: RECORDING_ID, generation: 1, clientId: "client-1",
      recoveryToken: "secret", finalSampleCount: 32_000,
    });
    expect((await spool.load(16_000)).map((item) => item.seq)).toEqual([0]);
    expect((await spool.load()).map((item) => item.seq)).toEqual([0, 1]);
    expect(await spool.loadRecovery()).toMatchObject({ recording: RECORDING_ID, finalSampleCount: 32_000 });
    await spool.clearRecovery();
    expect(await spool.loadRecovery()).toBe(null);
  });
});
