import { signal } from "nsh";
import type { DictationPhase } from "./dictation-logic";

export type VoiceTarget = "stt" | "tts";
export type VoiceModelStatus = {
  state: "unsupported" | "runtime_missing" | "absent" | "downloading" | "ready" | "loaded" | "error" | string;
  repo: string;
  revision?: string | null;
  size: number;
  cache_path?: string | null;
  error?: string | null;
};
export type VoiceStatus = { stt: VoiceModelStatus; tts: VoiceModelStatus };

export type VoiceProgress = {
  model?: string;
  target?: string;
  state: string;
  bytes_done?: number;
  bytes_total?: number;
  error?: string | null;
};

export const voiceStatus = signal<VoiceStatus | null>(null);
export const voiceStatusLoading = signal(false);
export const voiceStatusError = signal<string | null>(null);
export const dictationPhase = signal<DictationPhase>("idle");
export const dictationPreview = signal("");
export const dictationLevel = signal(0);
export const voiceDownloadProgress = signal<VoiceProgress | null>(null);
export const ttsDownloadProgress = signal<VoiceProgress | null>(null);

export function setVoiceProgress(target: VoiceTarget, progress: VoiceProgress | null) {
  if (target === "stt") voiceDownloadProgress.set(progress);
  else ttsDownloadProgress.set(progress);
}

export function receiveVoiceProgress(data: unknown) {
  if (!data || typeof data !== "object") return;
  const progress = data as VoiceProgress;
  if (typeof progress.state !== "string") return;
  const target: VoiceTarget | null = progress.target === "tts" || progress.model === "tts"
    ? "tts"
    : progress.model === "stt" ? "stt" : null;
  if (!target) return;
  setVoiceProgress(target, { ...progress, model: target });
  voiceStatus.update((status) => status ? {
    ...status,
    [target]: { ...status[target], state: progress.state, error: progress.error ?? null },
  } : null);
}
