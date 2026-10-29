export const DICTATION_SAMPLE_RATE = 16_000;
export const MIN_RECORDING_SECONDS = 0.2;
export const MAX_RECORDING_SECONDS = 120;
export const PREVIEW_INTERVAL_SECONDS = 0.4;
export const PREVIEW_WINDOW_SECONDS = 20;

export type DictationPhase = "idle" | "checking" | "consent" | "downloading" | "requesting" | "recording" | "stopping";
export type DictationEvent = "start" | "consent" | "download" | "request" | "ready" | "stop" | "finish" | "cancel" | "fail";

const TRANSITIONS: Record<DictationPhase, Partial<Record<DictationEvent, DictationPhase>>> = {
  idle: { start: "checking" },
  checking: { consent: "consent", download: "downloading", request: "requesting", cancel: "idle", fail: "idle" },
  consent: { download: "downloading", cancel: "idle", fail: "idle" },
  downloading: { ready: "requesting", cancel: "idle", fail: "idle" },
  requesting: { ready: "recording", cancel: "idle", fail: "idle" },
  recording: { stop: "stopping", cancel: "idle", fail: "idle" },
  stopping: { finish: "idle", cancel: "idle", fail: "idle" },
};

export function transitionDictation(phase: DictationPhase, event: DictationEvent): DictationPhase {
  const next = TRANSITIONS[phase][event];
  if (!next) throw new Error(`Invalid dictation transition: ${phase} → ${event}`);
  return next;
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

/** Linear sample-rate conversion. Output length is rounded to the nearest sample. */
export function resampleMono(samples: Float32Array, sourceRate: number, targetRate = DICTATION_SAMPLE_RATE): Float32Array {
  if (!Number.isFinite(sourceRate) || sourceRate <= 0 || !Number.isFinite(targetRate) || targetRate <= 0) {
    throw new Error("Sample rates must be positive numbers");
  }
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

/** Encode mono floating-point audio as a standards-compliant 44-byte PCM16 WAV. */
export function encodeWav(samples: Float32Array, sampleRate = DICTATION_SAMPLE_RATE): Uint8Array {
  if (!Number.isInteger(sampleRate) || sampleRate <= 0) throw new Error("Sample rate must be a positive integer");
  const dataBytes = samples.length * 2;
  const wav = new Uint8Array(44 + dataBytes);
  const view = new DataView(wav.buffer);
  const ascii = (text: string, offset: number) => wav.set(new TextEncoder().encode(text), offset);
  ascii("RIFF", 0);
  view.setUint32(4, 36 + dataBytes, true);
  ascii("WAVE", 8);
  ascii("fmt ", 12);
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii("data", 36);
  view.setUint32(40, dataBytes, true);
  for (let i = 0; i < samples.length; i++) {
    const sample = Number.isFinite(samples[i]) ? Math.max(-1, Math.min(1, samples[i])) : 0;
    view.setInt16(44 + i * 2, sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767), true);
  }
  return wav;
}

export function recordingToWav(samples: Float32Array, sourceRate: number): Uint8Array {
  return encodeWav(resampleMono(samples, sourceRate));
}

/** Do not overlap previews or start one before enough audio has accumulated. */
export function previewDue(sampleCount: number, previousPreviewCount: number, sampleRate: number, inFlight: boolean): boolean {
  if (inFlight || sampleRate <= 0) return false;
  const minSamples = Math.ceil(MIN_RECORDING_SECONDS * sampleRate);
  const newSamples = Math.ceil(PREVIEW_INTERVAL_SECONDS * sampleRate);
  return sampleCount >= minSamples && sampleCount - previousPreviewCount >= newSamples;
}

/** Join existing composer text and a transcript without replacing the user's draft. */
export function appendTranscript(existing: string, transcript: string): string {
  const text = transcript.trim();
  if (!text) return existing;
  if (!existing) return text;
  return `${existing}${existing.endsWith("\n") ? "" : "\n"}${text}`;
}

export function rmsLevel(samples: Float32Array): number {
  if (!samples.length) return 0;
  let power = 0;
  for (const sample of samples) power += sample * sample;
  return Math.min(1, Math.sqrt(power / samples.length));
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB"];
  let amount = bytes;
  let unit = -1;
  do { amount /= 1024; unit++; } while (amount >= 1024 && unit < units.length - 1);
  return `${amount.toFixed(amount < 10 ? 1 : 0)} ${units[unit]}`;
}
