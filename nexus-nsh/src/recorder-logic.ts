// Pure recorder logic (plan 54, D-NSH-web-cap): PCM chunking, the bounded
// spool budget, upload retry classification and capture-gap accounting.
// Nothing here opens a microphone, reads storage or touches the network.

import { CAPTURE_SAMPLE_RATE } from "./audio-capture-logic";

/** The recorder is independent of dictation, but both store the same PCM format. */
export const RECORDER_SAMPLE_RATE = CAPTURE_SAMPLE_RATE;
export const RECORDER_CHUNK_SAMPLES = RECORDER_SAMPLE_RATE; // about one second
export const RECORDER_SPOOL_SECONDS = 30;
export const RECORDER_SPOOL_SAMPLES = RECORDER_SPOOL_SECONDS * RECORDER_SAMPLE_RATE;
/** The daemon rejects a chunk body above 64 000 bytes (two seconds). */
export const RECORDER_MAX_CHUNK_BYTES = 64_000;
export const RECORDER_HEARTBEAT_MS = 5_000;
export const RECORDER_GAP_THRESHOLD_MS = 250;
export const RECORDER_MEMORY_ONLY_WARNING = "Unsaved audio is kept in memory only (≤30 s)";

export const DEFAULT_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];
export const DEFAULT_MAX_UPLOAD_ATTEMPTS = 6;

export type RecorderPhase = "idle" | "starting" | "recording" | "stopping" | "stopped" | "interrupted" | "failed";
export type RecorderFailure =
  | "microphone"
  | "create"
  | "spool_overflow"
  | "storage"
  | "upload"
  | "seal"
  | "lease_lost"
  | "deleted"
  | "disposed"
  | "recovery"
  | "recovery_impossible"
  | "checkpoint"
  | "capture"
  | "internal";

/** One contiguous PCM16 block with its integer sample position in the recording. */
export type PcmChunk = { seq: number; start: number; samples: number; bytes: Uint8Array };

export type RecorderGap = { afterSample: number; missingSamples: number; seconds: number };
export type RecorderLoss = {
  start: number;
  end: number;
  samples: number;
  reason: "spool_overflow" | "upload_failed" | "capture_failed" | "reconciled" | "native_overflow" | "native_after_cut";
};

/** Encode mono floating point samples as little-endian PCM16, matching the WAV encoder. */
export function encodePcm16(samples: Float32Array): Uint8Array {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i++) {
    const sample = Number.isFinite(samples[i]) ? Math.max(-1, Math.min(1, samples[i])) : 0;
    view.setInt16(i * 2, sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767), true);
  }
  return bytes;
}

export function secondsForSamples(samples: number, rate = RECORDER_SAMPLE_RATE): number {
  return rate > 0 ? samples / rate : 0;
}

/**
 * Split a continuous resampled stream into fixed-size PCM chunks.
 *
 * Boundaries are integer sample offsets; a pushed block may span several
 * chunks. `flush()` emits the remaining partial chunk (the recording tail).
 */
export class RecorderChunker {
  readonly sampleRate: number;
  readonly chunkSamples: number;
  private blocks: Float32Array[] = [];
  private pending = 0;
  private emitted = 0;
  private nextSequence = 0;

  constructor(sampleRate = RECORDER_SAMPLE_RATE, chunkSamples = RECORDER_CHUNK_SAMPLES) {
    if (!Number.isInteger(sampleRate) || sampleRate <= 0) throw new Error("Sample rate must be a positive integer");
    if (!Number.isInteger(chunkSamples) || chunkSamples <= 0) throw new Error("Chunk size must be a positive integer");
    this.sampleRate = sampleRate;
    this.chunkSamples = chunkSamples;
  }

  /** Samples accepted so far, including the pending partial chunk. */
  get sampleCount(): number { return this.emitted + this.pending; }

  /** Samples in the pending partial chunk. */
  get pendingSamples(): number { return this.pending; }

  push(input: Float32Array): PcmChunk[] {
    if (!input.length) return [];
    const out: PcmChunk[] = [];
    let offset = 0;
    while (offset < input.length) {
      const take = Math.min(this.chunkSamples - this.pending, input.length - offset);
      // Capture may reuse a block buffer; copy the slice we keep across calls.
      this.blocks.push(input.slice(offset, offset + take));
      this.pending += take;
      offset += take;
      if (this.pending === this.chunkSamples) out.push(this.emit());
    }
    return out;
  }

  /** Emit the partial tail, if any. Called once when capture stops. */
  flush(): PcmChunk[] {
    return this.pending ? [this.emit()] : [];
  }

  private emit(): PcmChunk {
    const joined = new Float32Array(this.pending);
    let at = 0;
    for (const block of this.blocks) {
      joined.set(block, at);
      at += block.length;
    }
    const chunk: PcmChunk = {
      seq: this.nextSequence++,
      start: this.emitted,
      samples: this.pending,
      bytes: encodePcm16(joined),
    };
    this.emitted += this.pending;
    this.pending = 0;
    this.blocks = [];
    return chunk;
  }
}

/** Whether one more chunk fits inside the bounded local spool. */
export function spoolFits(pendingSamples: number, addSamples: number, limit = RECORDER_SPOOL_SAMPLES): boolean {
  return addSamples >= 0 && pendingSamples >= 0 && pendingSamples + addSamples <= limit;
}

/** The codes that mean the capture lease is gone and the microphone must stop. */
export const LEASE_LOSS_CODES: readonly string[] = [
  "lease_expired",
  "not_owner",
  "invalid_lease",
  "not_active",
  "recording_sealed",
  "recording_not_found",
];

export type UploadFailureKind = "retry" | "rebind" | "lease_lost" | "fatal";

/**
 * Classify a chunk upload/heartbeat failure.
 *
 * Network failures, rate limits, backpressure and 5xx responses are retried
 * with the same sequence/content hash. Lease errors stop capture. Validation
 * or conflict errors are fatal: repeating the identical chunk cannot help.
 */
export function classifyUploadFailure(status: number | undefined, code: string | undefined): UploadFailureKind {
  // A detached event stream is recoverable with the still-valid lease. Rebinding
  // is coordinated by the controller so heartbeat and upload never race.
  if (code === "stream_detached") return "rebind";
  if (code && LEASE_LOSS_CODES.includes(code)) return "lease_lost";
  if (status === undefined || status === 0 || status === 408 || status === 425 || status === 429
    || status === 503 || (typeof status === "number" && status >= 500)) return "retry";
  return "fatal";
}

/** A bounded delay for the next identical retry, honouring Retry-After when given. */
export function retryDelay(attempt: number, retryAfterMs?: number | null, delays = DEFAULT_RETRY_DELAYS_MS): number {
  if (typeof retryAfterMs === "number" && Number.isFinite(retryAfterMs) && retryAfterMs >= 0) {
    return Math.min(60_000, Math.round(retryAfterMs));
  }
  if (!delays.length) return 1_000;
  return delays[Math.max(0, Math.min(delays.length - 1, Math.floor(attempt)))];
}

/** Parse a Retry-After response header (seconds form) into milliseconds. */
export function parseRetryAfter(value: string | null | undefined): number | null {
  if (!value) return null;
  const seconds = Number(value.trim());
  return Number.isFinite(seconds) && seconds >= 0 ? Math.round(seconds * 1000) : null;
}

/**
 * Detect capture-callback starvation from sample arrival versus wall clock.
 *
 * The gap is reported, never filled with silence: the recording keeps only the
 * samples the device actually delivered, and `afterSample` is the position
 * where the missing interval would have started.
 */
export class CaptureGapTracker {
  private lastAt: number | null = null;
  private lastSamples = 0;

  constructor(readonly sampleRate = RECORDER_SAMPLE_RATE, readonly thresholdMs = RECORDER_GAP_THRESHOLD_MS) {
    if (sampleRate <= 0) throw new Error("Sample rate must be positive");
  }

  observe(nowMs: number, totalSamples: number): RecorderGap | null {
    if (this.lastAt === null) {
      this.lastAt = nowMs;
      this.lastSamples = totalSamples;
      return null;
    }
    const elapsed = Math.max(0, nowMs - this.lastAt);
    const expected = Math.round((elapsed / 1000) * this.sampleRate);
    const actual = Math.max(0, totalSamples - this.lastSamples);
    const missing = expected - actual;
    const gap: RecorderGap | null = missing > Math.ceil((this.thresholdMs / 1000) * this.sampleRate)
      ? { afterSample: this.lastSamples, missingSamples: missing, seconds: secondsForSamples(missing, this.sampleRate) }
      : null;
    this.lastAt = nowMs;
    this.lastSamples = totalSamples;
    return gap;
  }
}

/** True while the phase can still produce or drain audio. */
export function captureActive(phase: RecorderPhase): boolean {
  return phase === "starting" || phase === "recording" || phase === "stopping";
}
