// Native desktop capture for the standalone recorder (plan 54, milestone A2).
//
// `RecorderMic` is a continuous 16 kHz mono PCM16 source owned by the native
// client. This adapter turns it into the same press-to-start, press-to-stop
// capture shape the web microphone exposes, and also offers a small status
// interface for consumers that can preserve native sample offsets and losses.
//
// Construction, availability checks and device listing never open a
// microphone. `start()` is the only path that calls `RecorderMic.start()`.
// `stop()` stops polling first, asks the native side to close the device, and
// then drains every complete 20 ms frame the ring still holds. Sample offsets
// are absolute: a bounded-ring overflow is reported as an exact missing range,
// never filled with silence or stitched into the next frame.
//
// Controller callback contract (all synchronous, before `stop()` resolves):
// - `onLoss` receives exact overflow ranges, exact end-tail ranges when the
//   native tail count is known but its bytes are not, and an `unknown` end-tail
//   loss when the native stop does not expose tail metadata at all.
// - `onError` receives live read failures plus format, generation, stop and
//   drain failures. `status()` and the stop result retain the same failure.
// The stop result repeats both, so a controller cannot miss an ended capture's
// loss merely because it subscribed late.

import { nativeModule } from "nsh";
import type { RecorderChunk, RecorderMicModule, RecorderStart, RecorderStop } from "nexus/native";
import type { OpenCapture } from "./audio-capture";
import type { RecorderCaptureHandlers } from "./recorder";

/**
 * `RecorderMic` now reports the final unframed samples separately. The fields
 * are optional so this adapter still detects an older native build, where it
 * reports an unknown end-tail loss rather than claiming a lossless recording.
 */
type NativeRecorderStopReport = RecorderStop & {
  tailSamples?: number | null;
  tailBytes?: Uint8Array | null;
};

/** Every RecorderMic frame is 20 ms of 16 kHz mono PCM16. */
export const NATIVE_RECORDER_SAMPLE_RATE = 16_000;
export const NATIVE_RECORDER_FRAME_SAMPLES = 320;
export const NATIVE_RECORDER_POLL_MS = 25;
/** One read is at most 1.28 s of audio; a backlog catches up over later polls. */
export const NATIVE_RECORDER_READ_FRAMES = 64;

export type NativeRecorderPhase = "idle" | "starting" | "recording" | "stopping" | "stopped" | "failed";

export type NativeRecorderFailureStage =
  | "unavailable"
  | "start"
  | "read"
  | "format"
  | "generation"
  | "callback"
  | "stop"
  | "drain";

export interface NativeRecorderFailure {
  stage: NativeRecorderFailureStage;
  message: string;
}

/**
 * A missing range measured in absolute recording samples.
 *
 * `overflow` and `end_tail` are exact. `unknown` means the native build did
 * not expose enough information to count the end tail; consumers must not
 * treat the recording as lossless in that case.
 */
export interface NativeRecorderLoss {
  start: number;
  end: number | null;
  samples: number | null;
  reason: "overflow" | "end_tail" | "unknown";
}

export interface NativeRecorderStatus {
  phase: NativeRecorderPhase;
  generation: number | null;
  sampleRate: number;
  channels: number;
  frameSamples: number;
  /** Absolute position of the next sample the adapter will deliver. */
  nextSampleOffset: number;
  deliveredSamples: number;
  droppedSamples: number;
  /** The native total, available once `stop` has returned. */
  capturedSamples: number | null;
  /** Frames the native ring held when `stop` returned. */
  bufferedFrames: number;
  /** Frames read after `stop`, to check the final drain. */
  drainedSamples: number;
  /** Tail samples the native stop reported, or null when it did not report one. */
  endTailSamples: number | null;
  endTailDeliveredSamples: number;
  endTailLostSamples: number;
  losses: NativeRecorderLoss[];
  error: string | null;
  errorStage: NativeRecorderFailureStage | null;
}

export interface NativeRecorderCaptureStop {
  generation: number | null;
  capturedSamples: number;
  deliveredSamples: number;
  droppedSamples: number;
  nextSampleOffset: number;
  bufferedFrames: number;
  drainedSamples: number;
  endTailSamples: number | null;
  endTailDeliveredSamples: number;
  endTailLostSamples: number;
  losses: NativeRecorderLoss[];
  error: string | null;
}

export type NativeRecorderTimers = {
  setInterval(handler: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
};

export interface NativeRecorderCaptureOptions {
  native: RecorderMicModule;
  /** Called synchronously with each decoded block, in absolute offset order. */
  onSamples(samples: Float32Array): void;
  /** Omitted or `null` uses the host's default input. */
  device?: string | null;
  timers?: NativeRecorderTimers;
  pollMs?: number;
  maxFramesPerRead?: number;
  onLoss?: (loss: NativeRecorderLoss) => void;
  onError?: (failure: NativeRecorderFailure) => void;
  onStatus?: (status: NativeRecorderStatus) => void;
}

const GLOBAL_TIMERS: NativeRecorderTimers = {
  setInterval: (handler, ms) => globalThis.setInterval(handler, ms),
  clearInterval: (handle) => globalThis.clearInterval(handle as ReturnType<typeof setInterval>),
};

let cachedNative: RecorderMicModule | null | undefined;

/** The app registers `RecorderMic` on every target; tests inject a fake instead. */
export function nativeRecorderMic(): RecorderMicModule | null {
  if (cachedNative !== undefined) return cachedNative;
  try {
    cachedNative = nativeModule<{ RecorderMic?: RecorderMicModule }>("nexus/native").RecorderMic ?? null;
  } catch {
    cachedNative = null;
  }
  return cachedNative;
}

/** Whether this build contains a continuous desktop capture path. Never opens a microphone. */
export function nativeRecorderAvailable(native: RecorderMicModule | null = nativeRecorderMic()): boolean {
  if (!native) return false;
  try {
    return native.available() === true;
  } catch {
    return false;
  }
}

/** Input device names for a picker. Never opens a microphone. */
export function nativeRecorderDevices(native: RecorderMicModule | null = nativeRecorderMic()): string[] {
  if (!native) return [];
  try {
    const devices = native.devices();
    return Array.isArray(devices) ? devices.filter((device): device is string => typeof device === "string") : [];
  } catch {
    return [];
  }
}

/** Decode little-endian PCM16 into the mono float blocks the recorder expects. */
export function decodeNativePcm16(bytes: Uint8Array): Float32Array {
  if (bytes.length % 2 !== 0) throw new Error("PCM16 data has an odd byte length");
  const samples = new Float32Array(bytes.length / 2);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let index = 0; index < samples.length; index++) {
    samples[index] = view.getInt16(index * 2, true) / 32768;
  }
  return samples;
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

function finiteInteger(value: unknown, minimum: number): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
}

/**
 * One native capture. It is one-shot: after `stop()` (or a start failure),
 * create a new instance for the next recording.
 */
export class NativeRecorderCapture {
  private readonly native: RecorderMicModule;
  private readonly timers: NativeRecorderTimers;
  private readonly pollMs: number;
  private readonly maxFramesPerRead: number;
  private readonly device: string | null;
  private readonly onSamples: (samples: Float32Array) => void;
  private readonly onLoss?: (loss: NativeRecorderLoss) => void;
  private readonly onError?: (failure: NativeRecorderFailure) => void;
  private readonly onStatus?: (status: NativeRecorderStatus) => void;

  private phase: NativeRecorderPhase = "idle";
  private startInfo: RecorderStart | null = null;
  private generation: number | null = null;
  private timer: unknown = null;
  private startTask: Promise<RecorderStart> | null = null;
  private stopTask: Promise<NativeRecorderCaptureStop> | null = null;
  private stopResult: NativeRecorderCaptureStop | null = null;
  private stopRequested = false;
  private nativeStopCalled = false;
  private nativeStopInfo: NativeRecorderStopReport | null = null;

  private expectedOffset = 0;
  private deliveredSamples = 0;
  private droppedSamples = 0;
  private drainedSamples = 0;
  private bufferedFrames = 0;
  private endTailSamples: number | null = null;
  private endTailDeliveredSamples = 0;
  private endTailLostSamples = 0;
  private losses: NativeRecorderLoss[] = [];
  private failure: NativeRecorderFailure | null = null;
  private reading = false;

  constructor(options: NativeRecorderCaptureOptions) {
    this.native = options.native;
    this.onSamples = options.onSamples;
    this.device = options.device ?? null;
    this.timers = options.timers ?? GLOBAL_TIMERS;
    this.pollMs = options.pollMs ?? NATIVE_RECORDER_POLL_MS;
    this.maxFramesPerRead = options.maxFramesPerRead ?? NATIVE_RECORDER_READ_FRAMES;
    this.onLoss = options.onLoss;
    this.onError = options.onError;
    this.onStatus = options.onStatus;
    if (!Number.isFinite(this.pollMs) || this.pollMs <= 0) throw new Error("pollMs must be positive");
    if (!Number.isSafeInteger(this.maxFramesPerRead) || this.maxFramesPerRead <= 0) {
      throw new Error("maxFramesPerRead must be a positive integer");
    }
  }

  /** The current truth. Reading this never touches the microphone. */
  status(): NativeRecorderStatus {
    return {
      phase: this.phase,
      generation: this.generation,
      sampleRate: this.startInfo?.sampleRate ?? 0,
      channels: this.startInfo?.channels ?? 0,
      frameSamples: this.startInfo?.frameSamples ?? 0,
      nextSampleOffset: this.expectedOffset,
      deliveredSamples: this.deliveredSamples,
      droppedSamples: this.droppedSamples,
      capturedSamples: this.nativeStopInfo?.capturedSamples ?? this.stopResult?.capturedSamples ?? null,
      bufferedFrames: this.bufferedFrames,
      drainedSamples: this.drainedSamples,
      endTailSamples: this.endTailSamples,
      endTailDeliveredSamples: this.endTailDeliveredSamples,
      endTailLostSamples: this.endTailLostSamples,
      losses: this.losses.map((loss) => ({ ...loss })),
      error: this.failure?.message ?? null,
      errorStage: this.failure?.stage ?? null,
    };
  }

  /** The current input level, 0 to 1. Zero before start and after stop. */
  level(): number {
    if (this.phase !== "recording") return 0;
    try {
      const level = this.native.level();
      return typeof level === "number" && Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0;
    } catch {
      return 0;
    }
  }

  /** Open the microphone. This is the first method that asks for permission. */
  start(): Promise<RecorderStart> {
    if (this.startTask) {
      return this.phase === "starting" ? this.startTask : Promise.reject(new Error("The native recorder capture has already been used"));
    }
    if (this.phase !== "idle") return Promise.reject(new Error("The native recorder capture has already been used"));
    const task = this.doStart();
    this.startTask = task;
    return task;
  }

  private async doStart(): Promise<RecorderStart> {
    this.phase = "starting";
    this.publish();
    let opened = false;
    try {
      if (!nativeRecorderAvailable(this.native)) {
        throw new Error("Recorder capture is not available in this build");
      }
      const start = await this.native.start(this.device);
      opened = true;
      this.validateStart(start);
      this.startInfo = start;
      this.generation = start.generation;
      if (!this.stopRequested) this.schedulePolling();
      this.phase = "recording";
      this.publish();
      return start;
    } catch (error) {
      this.recordFailure("start", messageOf(error));
      // A malformed start description still opened the device on the native
      // side; close it before reporting the failure.
      if (opened) {
        this.clearPolling();
        const info = this.callNativeStop();
        if (this.startInfo && this.generation !== null && info) {
          this.drainAfterStop(info);
          this.finishStop(info);
        }
      }
      this.phase = "failed";
      this.publish();
      throw error instanceof Error ? error : new Error(messageOf(error));
    }
  }

  private validateStart(start: RecorderStart) {
    if (!start || typeof start !== "object") throw new Error("The native recorder returned no capture description");
    if (!finiteInteger(start.generation, 1)) throw new Error("The native recorder returned an invalid generation");
    if (!finiteInteger(start.sampleRate, 1) || start.sampleRate !== NATIVE_RECORDER_SAMPLE_RATE) {
      throw new Error(`The native recorder returned an unsupported sample rate: ${String(start.sampleRate)}`);
    }
    if (start.channels !== 1) throw new Error("The native recorder must return mono audio");
    if (!finiteInteger(start.frameSamples, 1) || start.frameSamples !== NATIVE_RECORDER_FRAME_SAMPLES) {
      throw new Error(`The native recorder returned an unsupported frame size: ${String(start.frameSamples)}`);
    }
    if (!finiteInteger(start.maxFrames, 1)) throw new Error("The native recorder returned an invalid read bound");
  }

  private schedulePolling() {
    this.timer = this.timers.setInterval(() => this.poll(), this.pollMs);
  }

  private clearPolling() {
    if (this.timer === null) return;
    this.timers.clearInterval(this.timer);
    this.timer = null;
  }

  private batchFrames(): number {
    const nativeBound = this.startInfo?.maxFrames ?? this.maxFramesPerRead;
    return Math.max(1, Math.min(this.maxFramesPerRead, nativeBound));
  }

  private poll() {
    if (this.phase !== "recording" || this.reading) return;
    const samples = this.readNative("read", false);
    if (samples > 0) this.publish();
  }

  /**
   * Pull one bounded read manually. Polling uses the same path; this exists for
   * consumers that drive capture from their own scheduler and for tests.
   */
  drainNow(): number {
    if (this.phase !== "recording" || this.reading) return 0;
    const samples = this.readNative("read", false);
    if (samples > 0) this.publish();
    return samples;
  }

  /** Stop the device, drain its final frames, and return the exact counts. Idempotent. */
  stop(): Promise<NativeRecorderCaptureStop> {
    if (this.stopTask) return this.stopTask;
    this.stopTask = this.doStop();
    return this.stopTask;
  }

  private async doStop(): Promise<NativeRecorderCaptureStop> {
    this.stopRequested = true;
    if (this.startTask) {
      try {
        await this.startTask;
      } catch {
        // A failed start has no device to release; the failure is already recorded.
      }
    }
    if (this.stopResult) return this.stopResult;
    if (this.generation === null) {
      this.phase = this.phase === "failed" ? "failed" : "stopped";
      this.stopResult = this.stopSnapshot(null);
      this.publish();
      return this.stopResult;
    }

    this.phase = "stopping";
    this.clearPolling();
    this.publish();
    const info = this.callNativeStop();
    if (info) this.drainAfterStop(info);
    this.finishStop(info);
    return this.stopResult!;
  }

  private callNativeStop(): NativeRecorderStopReport | null {
    if (this.nativeStopCalled) return this.nativeStopInfo;
    this.nativeStopCalled = true;
    try {
      const info = this.native.stop();
      this.nativeStopInfo = info;
      if (info && finiteInteger(info.bufferedFrames, 0)) this.bufferedFrames = info.bufferedFrames;
      return info;
    } catch (error) {
      this.recordFailure("stop", messageOf(error));
      return null;
    }
  }

  private drainAfterStop(info: NativeRecorderStopReport) {
    if (this.generation === null) return;
    const buffered = finiteInteger(info.bufferedFrames, 0) ? info.bufferedFrames : 0;
    const batch = this.batchFrames();
    const maxReads = Math.ceil(buffered / batch) + 2;
    let reads = 0;
    while (reads < maxReads) {
      reads += 1;
      const samples = this.readNative("drain", true);
      if (samples <= 0) break;
    }
    const expected = buffered * this.startInfo!.frameSamples;
    if (this.drainedSamples !== expected) {
      this.recordFailure(
        "drain",
        `Native stop reported ${buffered} buffered frames but ${this.drainedSamples / this.startInfo!.frameSamples} were read`,
      );
    }
    this.deliverEndTail(info);
  }

  /**
   * Preserve or report the resampler's final incomplete 20 ms frame. Never
   * pads it to a whole frame; never silently drops it.
   */
  private deliverEndTail(info: NativeRecorderStopReport) {
    const frameSamples = this.startInfo!.frameSamples;
    const bytes = info.tailBytes instanceof Uint8Array ? info.tailBytes : null;
    const byteSamples = bytes && bytes.length % 2 === 0 ? bytes.length / 2 : null;
    const reported = finiteInteger(info.tailSamples, 0) ? info.tailSamples : null;
    this.endTailSamples = reported ?? byteSamples;

    if (this.endTailSamples === null) {
      this.recordLoss(this.expectedOffset, null, null, "unknown");
      return;
    }
    const count = this.endTailSamples;
    if (count === 0) {
      if (bytes && bytes.length) {
        this.recordFailure("format", "Native stop reported an empty end tail with PCM bytes");
        this.recordLoss(this.expectedOffset, null, null, "unknown");
      }
      return;
    }
    if (count >= frameSamples) {
      this.recordFailure("format", `Native end tail has ${count} samples, not less than one frame`);
      this.recordLoss(this.expectedOffset, null, null, "unknown");
      return;
    }
    if (bytes && byteSamples === count) {
      let samples: Float32Array;
      try {
        samples = decodeNativePcm16(bytes);
      } catch (error) {
        this.recordFailure("format", `Native end tail is not valid PCM16: ${messageOf(error)}`);
        this.endTailLostSamples += count;
        this.recordLoss(this.expectedOffset, this.expectedOffset + count, count, "end_tail");
        this.expectedOffset += count;
        return;
      }
      this.endTailDeliveredSamples += count;
      this.deliveredSamples += count;
      this.expectedOffset += count;
      try {
        this.onSamples(samples);
      } catch (error) {
        this.recordFailure("callback", `Recorder audio consumer failed on the end tail: ${messageOf(error)}`);
      }
      return;
    }
    if (bytes && bytes.length > 0 && byteSamples !== null) {
      this.recordFailure("format", `Native end tail has ${count} samples but ${byteSamples} PCM samples`);
    }
    this.endTailLostSamples += count;
    this.recordLoss(this.expectedOffset, this.expectedOffset + count, count, "end_tail");
    this.expectedOffset += count;
  }

  private finishStop(info: NativeRecorderStopReport | null) {
    const frameSamples = this.startInfo!.frameSamples;
    if (info) {
      if (info.generation !== this.generation) {
        this.recordFailure("generation", `Native stop reported generation ${info.generation}, expected ${this.generation}`);
      }
      if (finiteInteger(info.capturedSamples, 0)
        && info.capturedSamples !== this.deliveredSamples + this.droppedSamples + this.endTailLostSamples) {
        this.recordFailure(
          "drain",
          `Native capture counted ${info.capturedSamples} samples but the adapter delivered ${this.deliveredSamples}, dropped ${this.droppedSamples} and lost ${this.endTailLostSamples} at the end tail`,
        );
      }
      if (finiteInteger(info.deliveredSamples, 0)
        && info.deliveredSamples + this.bufferedFrames * frameSamples + this.endTailDeliveredSamples !== this.deliveredSamples) {
        this.recordFailure(
          "drain",
          `Native capture delivered ${info.deliveredSamples} samples plus ${this.bufferedFrames} buffered frames and ${this.endTailDeliveredSamples} tail samples but the adapter delivered ${this.deliveredSamples}`,
        );
      }
      if (finiteInteger(info.droppedSamples, 0) && info.droppedSamples !== this.droppedSamples) {
        this.recordFailure(
          "drain",
          `Native capture dropped ${info.droppedSamples} samples but the adapter reported ${this.droppedSamples}`,
        );
      }
      if (typeof info.error === "string" && info.error) this.recordFailure("stop", info.error);
    }
    this.phase = this.failure ? "failed" : "stopped";
    this.stopResult = this.stopSnapshot(info);
    this.publish();
  }

  private stopSnapshot(info: NativeRecorderStopReport | null): NativeRecorderCaptureStop {
    const capturedSamples = info && finiteInteger(info.capturedSamples, 0)
      ? info.capturedSamples
      : this.deliveredSamples + this.droppedSamples + this.endTailLostSamples;
    return {
      generation: this.generation,
      capturedSamples,
      deliveredSamples: this.deliveredSamples,
      droppedSamples: this.droppedSamples,
      nextSampleOffset: this.expectedOffset,
      bufferedFrames: this.bufferedFrames,
      drainedSamples: this.drainedSamples,
      endTailSamples: this.endTailSamples,
      endTailDeliveredSamples: this.endTailDeliveredSamples,
      endTailLostSamples: this.endTailLostSamples,
      losses: this.losses.map((loss) => ({ ...loss })),
      error: this.failure?.message ?? null,
    };
  }

  private readNative(stage: "read" | "drain", afterStop: boolean): number {
    if (this.generation === null || this.reading) return 0;
    if (!afterStop && this.phase !== "recording") return 0;
    let failed = false;
    this.reading = true;
    try {
      return this.acceptChunk(this.native.read(this.generation, this.batchFrames()), afterStop);
    } catch (error) {
      this.recordFailure(stage === "drain" ? "drain" : "read", messageOf(error));
      failed = true;
      return 0;
    } finally {
      this.reading = false;
      // Release the device outside the read guard so the final drain can run.
      if (failed && !afterStop) this.abortAfterFailure();
    }
  }

  private acceptChunk(chunk: RecorderChunk, afterStop: boolean): number {
    const generation = this.generation!;
    const frameSamples = this.startInfo!.frameSamples;
    if (!chunk || typeof chunk !== "object") throw new Error("The native recorder returned no chunk");
    if (chunk.generation !== generation) {
      this.recordFailure("generation", `Native chunk generation ${chunk.generation} does not match capture ${generation}`);
      throw new Error(`Native chunk generation ${chunk.generation} does not match capture ${generation}`);
    }
    if (!finiteInteger(chunk.samples, 0) || chunk.samples % frameSamples !== 0) {
      throw new Error(`Native chunk sample count ${String(chunk.samples)} is not a whole number of frames`);
    }
    if (!(chunk.bytes instanceof Uint8Array) || chunk.bytes.length !== chunk.samples * 2) {
      throw new Error("Native chunk bytes do not match its sample count");
    }
    if (!finiteInteger(chunk.sampleOffset, 0)) throw new Error("Native chunk has an invalid sample offset");
    if (!finiteInteger(chunk.droppedSamples, 0) || !finiteInteger(chunk.droppedSincePrevious, 0)) {
      throw new Error("Native chunk has invalid drop counts");
    }

    const gap = chunk.sampleOffset - this.expectedOffset;
    if (gap < 0) {
      throw new Error(
        `Native chunk starts at sample ${chunk.sampleOffset}, before the next expected sample ${this.expectedOffset}`,
      );
    }
    if (gap > 0) {
      if (chunk.droppedSincePrevious !== gap) {
        throw new Error(
          `Native capture advanced ${gap} samples but reported ${chunk.droppedSincePrevious} dropped samples`,
        );
      }
      this.recordLoss(
        this.expectedOffset,
        chunk.sampleOffset,
        chunk.sampleOffset - this.expectedOffset,
        "overflow",
      );
    } else if (chunk.droppedSincePrevious > 0) {
      throw new Error("Native capture reported dropped samples without advancing its sample offset");
    }
    if (chunk.droppedSamples !== this.droppedSamples) {
      throw new Error(
        `Native capture reports ${chunk.droppedSamples} dropped samples, but the adapter counted ${this.droppedSamples}`,
      );
    }
    this.expectedOffset = chunk.sampleOffset + chunk.samples;
    if (chunk.samples === 0) return 0;

    let samples: Float32Array;
    try {
      samples = decodeNativePcm16(chunk.bytes);
    } catch (error) {
      throw new Error(`Native chunk is not valid PCM16: ${messageOf(error)}`);
    }
    this.deliveredSamples += chunk.samples;
    if (afterStop) this.drainedSamples += chunk.samples;
    try {
      this.onSamples(samples);
    } catch (error) {
      throw new Error(`Recorder audio consumer failed: ${messageOf(error)}`);
    }
    return chunk.samples;
  }

  private recordLoss(
    start: number,
    end: number | null,
    samples: number | null,
    reason: NativeRecorderLoss["reason"],
  ) {
    const loss: NativeRecorderLoss = { start, end, samples, reason };
    if (reason === "overflow" && samples !== null) this.droppedSamples += samples;
    this.losses.push(loss);
    try {
      this.onLoss?.(loss);
    } catch {
      // A reporting callback cannot change the capture's own accounting.
    }
  }

  private recordFailure(stage: NativeRecorderFailureStage, message: string) {
    if (this.failure) return;
    this.failure = { stage, message };
    try {
      this.onError?.({ ...this.failure });
    } catch {
      // A reporting callback cannot change the capture's own accounting.
    }
  }

  /** A live read failed: release the device and preserve the frames already delivered. */
  private abortAfterFailure() {
    this.clearPolling();
    this.phase = "failed";
    const info = this.callNativeStop();
    if (info) this.drainAfterStop(info);
    this.finishStop(info);
  }

  private publish() {
    try {
      this.onStatus?.(this.status());
    } catch {
      // Status reporting must not break capture or stopping.
    }
  }
}

/** Controller-facing wording for every exact and unknown native loss. */
export function describeNativeLosses(losses: readonly NativeRecorderLoss[]): string {
  const overflow = losses
    .filter((loss) => loss.reason === "overflow" && loss.samples !== null)
    .reduce((sum, loss) => sum + (loss.samples ?? 0), 0);
  const tail = losses
    .filter((loss) => loss.reason === "end_tail" && loss.samples !== null)
    .reduce((sum, loss) => sum + (loss.samples ?? 0), 0);
  const unknown = losses.some((loss) => loss.reason === "unknown" || loss.samples === null);
  const parts: string[] = [];
  if (overflow > 0) parts.push(`${overflow} samples to native buffer overflow`);
  if (tail > 0) parts.push(`${tail} samples at the native end tail`);
  if (unknown) parts.push("an unknown number of samples at the native end tail");
  return `Recorder capture lost ${parts.length ? parts.join(" and ") : "audio"}`;
}

export interface NativeRecorderOpenOptions {
  native: RecorderMicModule;
  device?: string | null;
  timers?: NativeRecorderTimers;
  pollMs?: number;
  maxFramesPerRead?: number;
  onLoss?: (loss: NativeRecorderLoss) => void;
  onError?: (failure: NativeRecorderFailure) => void;
}

/** The web recorder's capture shape plus the richer native status handle. */
export type NativeOpenCapture = OpenCapture & { capture: NativeRecorderCapture };

/** The second handler argument the controller passes to `openMicrophone`. */
export type NativeOpenMicrophone = (
  onSamples: (samples: Float32Array) => void,
  handlers?: RecorderCaptureHandlers,
) => Promise<NativeOpenCapture>;

/**
 * Build the `RecorderDependencies.openMicrophone` shape around `RecorderMic`.
 *
 * The returned function does not touch the microphone; the recorder controller
 * calls it only from its explicit `start()`. The final incomplete native frame
 * is delivered through `onSamples` before `stop()` resolves. Its `stop()`
 * rejects when native capture reported a device error, because `OpenCapture`
 * has no way to carry a failed capture. Losses are rejected only when neither
 * the caller nor the controller supplied a loss handler; when a loss observer
 * is present, the same loss is already reported through it.
 *
 * Both the options callbacks and the controller's optional second-argument
 * handlers are called, in that order, and one throwing callback cannot stop
 * the other from observing the loss or failure.
 */
export function openNativeRecorderMicrophone(
  options: NativeRecorderOpenOptions,
): NativeOpenMicrophone {
  return async (onSamples, handlers) => {
    const observersLoss = options.onLoss !== undefined || handlers?.onLoss !== undefined;
    const callbacks = <T>(value: T, ...list: (((value: T) => void) | undefined)[]) => {
      for (const callback of list) {
        try {
          callback?.(value);
        } catch {
          // A reporting callback cannot hide the event from the other observer.
        }
      }
    };
    const capture = new NativeRecorderCapture({
      ...options,
      onSamples,
      onLoss: (loss) => callbacks(loss, options.onLoss, handlers?.onLoss),
      onError: (failure) => callbacks(failure, options.onError, handlers?.onError),
    });
    const start = await capture.start();
    return {
      rate: start.sampleRate,
      recorder: {
        async stop() {
          const result = await capture.stop();
          if (result.error) throw new Error(result.error);
          if (result.losses.length && !observersLoss) throw new Error(describeNativeLosses(result.losses));
        },
      },
      capture,
    };
  };
}
