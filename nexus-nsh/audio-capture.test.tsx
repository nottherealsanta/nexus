import { describe, expect, test } from "nsh/test";
import { CAPTURE_SAMPLE_RATE, StreamingResampler, downmixChannels, resampleMono, rmsLevel, streamedSampleCount } from "./src/audio-capture-logic";

const ODD_BLOCKS = [4093, 1, 257, 999, 2047, 3];

function ramp(length: number): Float32Array {
  const samples = new Float32Array(length);
  for (let i = 0; i < length; i++) samples[i] = ((i % 97) - 48) / 48;
  return samples;
}

function inOddBlocks(samples: Float32Array, sourceRate: number): Float32Array {
  const resampler = new StreamingResampler(sourceRate);
  const pieces: Float32Array[] = [];
  let offset = 0;
  let block = 0;
  while (offset < samples.length) {
    const size = Math.min(ODD_BLOCKS[block++ % ODD_BLOCKS.length], samples.length - offset);
    pieces.push(resampler.process(samples.subarray(offset, offset + size)));
    offset += size;
  }
  pieces.push(resampler.process(new Float32Array()));
  const total = pieces.reduce((sum, piece) => sum + piece.length, 0);
  const joined = new Float32Array(total);
  let at = 0;
  for (const piece of pieces) { joined.set(piece, at); at += piece.length; }
  return joined;
}

async function oneHour(sourceRate: number): Promise<{ emitted: number; expected: number }> {
  const resampler = new StreamingResampler(sourceRate);
  const samples = 3600 * sourceRate;
  const scratch = ramp(4093);
  let fed = 0;
  let block = 0;
  while (fed < samples) {
    const size = Math.min(ODD_BLOCKS[block++ % ODD_BLOCKS.length], samples - fed);
    resampler.process(scratch.subarray(0, size));
    fed += size;
    // The web test runtime bounds one synchronous call; an hour is many calls.
    if (block % 400 === 0) await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return { emitted: resampler.outputSamples, expected: streamedSampleCount(samples, sourceRate) };
}

describe("streaming audio capture primitives", () => {
  test("carries phase across odd blocks: split output equals one whole pass", () => {
    const source = ramp(10_001);
    for (const rate of [44_100, 48_000, 32_000]) {
      const whole = new StreamingResampler(rate).process(source);
      expect(inOddBlocks(source, rate)).toEqual(whole);
    }
  });

  test("one simulated hour at 44.1 kHz in odd blocks has no drift", async () => {
    const { emitted, expected } = await oneHour(44_100);
    expect(expected).toBe(57_600_000);
    expect(emitted).toBe(expected);
  });

  test("one simulated hour at 48 kHz in odd blocks has no drift", async () => {
    const { emitted, expected } = await oneHour(48_000);
    expect(expected).toBe(57_600_000);
    expect(emitted).toBe(expected);
  });

  test("derives 16 kHz sample counts from the input position, not rounded block lengths", () => {
    expect(streamedSampleCount(44_100, 44_100)).toBe(16_000);
    expect(streamedSampleCount(48_000, 48_000)).toBe(16_000);
    expect(streamedSampleCount(0, 44_100)).toBe(0);
    const resampler = new StreamingResampler(44_100);
    expect(resampler.process(new Float32Array(0)).length).toBe(0);
    expect(resampler.process(ramp(44_100)).length).toBe(16_000);
    expect(resampler.outputSamples).toBe(16_000);
    expect(resampler.inputSamples).toBe(44_100);
  });

  test("keeps the one-shot resampler's finished-buffer behaviour", () => {
    const samples = new Float32Array([0, 1, 0, -1]);
    const result = resampleMono(samples, 32_000);
    expect(result.length).toBe(2);
    expect(resampleMono(samples, CAPTURE_SAMPLE_RATE)).toEqual(samples);
    expect(resampleMono(new Float32Array(), 44_100).length).toBe(0);
    expect(() => new StreamingResampler(0)).toThrow("Sample rates must be positive numbers");
  });

  test("downmix and level helpers stay with the capture primitives", () => {
    expect(downmixChannels([new Float32Array([1, 0.5]), new Float32Array([-1, 0.5])])).toEqual(new Float32Array([0, 0.5]));
    expect(rmsLevel(new Float32Array([0, 0]))).toBe(0);
    expect(rmsLevel(new Float32Array([1, -1]))).toBe(1);
  });
});
