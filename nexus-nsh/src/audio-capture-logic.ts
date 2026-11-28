// Pure audio primitives shared by dictation and any other microphone capture
// (plan 54's recorder). Nothing here touches the DOM or the native module.

/** Every capture path in Nexus stores 16 kHz mono PCM. */
export const CAPTURE_SAMPLE_RATE = 16_000;

type NavigatorLike = { mediaDevices?: { getUserMedia?: unknown } };

/** Native NSH has no `navigator.mediaDevices`; only the web client can capture audio. */
export function hasMicrophoneCapture(navigator: NavigatorLike | undefined): boolean {
  return typeof navigator?.mediaDevices?.getUserMedia === "function";
}

/** Average channel samples into mono, clipping to the shortest channel. */
export function downmixChannels(channels: readonly Float32Array[]): Float32Array {
  if (!channels.length) return new Float32Array();
  const length = Math.min(...channels.map((channel) => channel.length));
  const mono = new Float32Array(length);
  for (let i = 0; i < length; i++) {
    let value = 0;
    for (const channel of channels) value += channel[i];
    mono[i] = value / channels.length;
  }
  return mono;
}

function checkRates(sourceRate: number, targetRate: number) {
  if (!Number.isFinite(sourceRate) || sourceRate <= 0 || !Number.isFinite(targetRate) || targetRate <= 0) {
    throw new Error("Sample rates must be positive numbers");
  }
}

/** Linear sample-rate conversion of a finished buffer. Output length is rounded to the nearest sample. */
export function resampleMono(samples: Float32Array, sourceRate: number, targetRate = CAPTURE_SAMPLE_RATE): Float32Array {
  checkRates(sourceRate, targetRate);
  if (samples.length === 0) return new Float32Array();
  if (sourceRate === targetRate) return samples.slice();
  const length = Math.max(1, Math.round(samples.length * targetRate / sourceRate));
  const output = new Float32Array(length);
  const ratio = sourceRate / targetRate;
  for (let i = 0; i < length; i++) {
    const position = i * ratio;
    const left = Math.min(samples.length - 1, Math.floor(position));
    const right = Math.min(samples.length - 1, left + 1);
    const fraction = Math.min(1, position - left);
    output[i] = samples[left] * (1 - fraction) + samples[right] * fraction;
  }
  return output;
}

/**
 * Block-wise linear resampler for a live capture stream.
 *
 * Unlike `resampleMono`, the read position is carried between blocks as an
 * integer sample index, so splitting a stream into arbitrary (including
 * odd-sized) blocks neither drops nor duplicates samples and produces the same
 * output as one call over the whole stream. Integer phase means no drift: the
 * output count after N input samples is exactly
 * `floor((N - 1) * targetRate / sourceRate) + 1`.
 */
export class StreamingResampler {
  readonly ratio: number;
  private nextOutput = 0;
  private consumed = 0;
  private previous: number | null = null;

  constructor(readonly sourceRate: number, readonly targetRate = CAPTURE_SAMPLE_RATE) {
    checkRates(sourceRate, targetRate);
    this.ratio = sourceRate / targetRate;
  }

  /** Input samples accepted so far. */
  get inputSamples(): number { return this.consumed; }

  /** Output samples emitted so far. */
  get outputSamples(): number { return this.nextOutput; }

  /** Convert the next block, returning the samples that fell inside it. */
  process(block: Float32Array): Float32Array {
    if (!block.length) return new Float32Array();
    const last = block.length - 1;
    const output = new Float32Array(Math.ceil(block.length / this.ratio) + 2);
    let count = 0;
    const whole = Math.floor(this.sourceRate / this.targetRate);
    const stepRemainder = this.sourceRate - whole * this.targetRate;
    let numerator = this.nextOutput * this.sourceRate - this.consumed * this.targetRate;
    let left = Math.floor(numerator / this.targetRate);
    let remainder = numerator - left * this.targetRate;
    // A fractional position at the block's last sample needs the next block's
    // first sample to interpolate, so leave it for the next call.
    while (left < last || (left === last && remainder === 0)) {
      const fraction = remainder / this.targetRate;
      let value: number;
      if (left < 0) {
        const from = this.previous ?? block[0];
        value = from * (1 - fraction) + block[0] * fraction;
      } else {
        const right = Math.min(last, left + 1);
        value = block[left] * (1 - fraction) + block[right] * fraction;
      }
      output[count++] = value;
      this.nextOutput++;
      left += whole;
      remainder += stepRemainder;
      if (remainder >= this.targetRate) { remainder -= this.targetRate; left++; }
    }
    this.previous = block[last];
    this.consumed += block.length;
    return count === output.length ? output : output.subarray(0, count);
  }
}

/** The expected output count for `inputSamples` through `StreamingResampler`. */
export function streamedSampleCount(inputSamples: number, sourceRate: number, targetRate = CAPTURE_SAMPLE_RATE): number {
  checkRates(sourceRate, targetRate);
  if (inputSamples <= 0) return 0;
  return Math.floor((inputSamples - 1) * targetRate / sourceRate) + 1;
}

export function rmsLevel(samples: Float32Array): number {
  if (!samples.length) return 0;
  let power = 0;
  for (const sample of samples) power += sample * sample;
  return Math.min(1, Math.sqrt(power / samples.length));
}
