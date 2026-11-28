import { CAPTURE_SAMPLE_RATE, resampleMono } from "./audio-capture-logic";

// Capture primitives live in audio-capture-logic; re-exported so dictation
// callers and tests keep one import site.
export { downmixChannels, hasMicrophoneCapture, resampleMono, rmsLevel } from "./audio-capture-logic";

export const DICTATION_SAMPLE_RATE = CAPTURE_SAMPLE_RATE;
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

export const VOICE_TRANSCRIPT_NOTICE = "[Voice transcript — may contain errors.]";

/** Keep the provenance visible in the draft and in the exact text sent to Core. */
export function appendTranscript(existing: string, transcript: string): string {
  const text = transcript.trim();
  if (!text) return existing;
  const joined = existing ? `${existing}${existing.endsWith("\n") ? "" : "\n"}${text}` : text;
  return joined.startsWith(VOICE_TRANSCRIPT_NOTICE) ? joined : `${VOICE_TRANSCRIPT_NOTICE}\n\n${joined}`;
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
