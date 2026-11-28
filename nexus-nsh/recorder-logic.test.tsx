import { describe, expect, test } from "nsh/test";
import {
  CaptureGapTracker,
  RECORDER_SPOOL_SAMPLES,
  RecorderChunker,
  classifyUploadFailure,
  encodePcm16,
  parseRetryAfter,
  retryDelay,
  spoolFits,
} from "./src/recorder-logic";

function ramp(length: number): Float32Array {
  const samples = new Float32Array(length);
  for (let i = 0; i < length; i++) samples[i] = ((i % 200) - 100) / 100;
  return samples;
}

describe("recorder chunking", () => {
  test("emits one-second chunks with contiguous integer sample offsets", () => {
    const chunker = new RecorderChunker();
    const chunks = chunker.push(ramp(24_000));
    expect(chunks.map((chunk) => chunk.seq)).toEqual([0]);
    expect(chunks[0].start).toBe(0);
    expect(chunks[0].samples).toBe(16_000);
    expect(chunks[0].bytes.length).toBe(32_000);
    expect(chunker.sampleCount).toBe(24_000);
    expect(chunker.pendingSamples).toBe(8_000);

    const tail = chunker.flush();
    expect(tail).toHaveLength(1);
    expect(tail[0].seq).toBe(1);
    expect(tail[0].start).toBe(16_000);
    expect(tail[0].samples).toBe(8_000);
    expect(tail[0].bytes.length).toBe(16_000);
    expect(chunker.flush()).toEqual([]);
  });

  test("splits odd blocks without losing or duplicating samples", () => {
    const chunker = new RecorderChunker();
    const source = ramp(50_000);
    const pieces = [];
    let offset = 0;
    for (const size of [1, 4_093, 257, 999, 2_047, 16_001]) {
      pieces.push(...chunker.push(source.subarray(offset, offset + size)));
      offset += size;
    }
    pieces.push(...chunker.push(source.subarray(offset)));
    pieces.push(...chunker.flush());
    expect(pieces.reduce((sum, chunk) => sum + chunk.samples, 0)).toBe(source.length);
    for (let i = 1; i < pieces.length; i++) {
      expect(pieces[i].seq).toBe(pieces[i - 1].seq + 1);
      expect(pieces[i].start).toBe(pieces[i - 1].start + pieces[i - 1].samples);
      expect(pieces[i].bytes.length).toBe(pieces[i].samples * 2);
    }
  });

  test("encodes clipped little-endian PCM16", () => {
    const bytes = encodePcm16(new Float32Array([-1, -0.5, 0, 0.5, 1, Number.NaN]));
    const view = new DataView(bytes.buffer);
    expect([0, 2, 4, 6, 8, 10].map((at) => view.getInt16(at, true)))
      .toEqual([-32768, -16384, 0, 16384, 32767, 0]);
  });

  test("bounds the spool at exactly thirty seconds of pending audio", () => {
    expect(RECORDER_SPOOL_SAMPLES).toBe(480_000);
    expect(spoolFits(0, 16_000)).toBe(true);
    expect(spoolFits(464_000, 16_000)).toBe(true);
    expect(spoolFits(465_000, 16_000)).toBe(false);
    expect(spoolFits(480_000, 1)).toBe(false);
  });
});

describe("recorder upload retry state", () => {
  test("retries transport, backpressure, rate-limit and 5xx failures with the same chunk", () => {
    expect(classifyUploadFailure(0, undefined)).toBe("retry");
    expect(classifyUploadFailure(undefined, undefined)).toBe("retry");
    expect(classifyUploadFailure(408, undefined)).toBe("retry");
    expect(classifyUploadFailure(429, undefined)).toBe("retry");
    expect(classifyUploadFailure(503, "recorder_backpressure")).toBe("retry");
    expect(classifyUploadFailure(500, undefined)).toBe("retry");
  });

  test("stops on lease loss and rejects validation/conflict errors as fatal", () => {
    for (const code of ["lease_expired", "not_owner", "invalid_lease", "recording_sealed"]) {
      expect(classifyUploadFailure(409, code)).toBe("lease_lost");
    }
    expect(classifyUploadFailure(409, "stream_detached")).toBe("rebind");
    expect(classifyUploadFailure(409, "chunk_conflict")).toBe("fatal");
    expect(classifyUploadFailure(409, "noncontiguous")).toBe("fatal");
    expect(classifyUploadFailure(400, "bad_chunk_size")).toBe("fatal");
    expect(classifyUploadFailure(403, "not_owner")).toBe("lease_lost");
  });

  test("backs off boundedly and honours Retry-After", () => {
    expect(retryDelay(0)).toBe(1_000);
    expect(retryDelay(2)).toBe(4_000);
    expect(retryDelay(99)).toBe(30_000);
    expect(retryDelay(0, 2_500)).toBe(2_500);
    expect(parseRetryAfter("2")).toBe(2_000);
    expect(parseRetryAfter(null)).toBe(null);
  });
});

describe("recorder capture gap accounting", () => {
  test("reports callback starvation in samples and seconds without inserting audio", () => {
    const tracker = new CaptureGapTracker(16_000, 250);
    expect(tracker.observe(1_000, 0)).toBe(null);
    expect(tracker.observe(2_000, 16_000)).toBe(null);
    const gap = tracker.observe(4_500, 24_000);
    expect(gap).toEqual({ afterSample: 16_000, missingSamples: 32_000, seconds: 2 });
  });

  test("ignores sample jitter below the threshold", () => {
    const tracker = new CaptureGapTracker(16_000, 250);
    tracker.observe(0, 0);
    // 0.1 s of wall time without a corresponding block is below 250 ms.
    expect(tracker.observe(1_100, 16_000)).toBe(null);
  });
});
