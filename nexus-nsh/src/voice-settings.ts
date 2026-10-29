import { ApiError, newKey, request, sleep } from "./core/transport";
import { confirmDialog, toast } from "./store";
import {
  setVoiceProgress, ttsDownloadProgress, voiceDownloadProgress,
  voiceStatus, voiceStatusError, voiceStatusLoading,
} from "./voice-state";
import type { VoiceModelStatus, VoiceStatus, VoiceTarget } from "./voice-state";

export type VoiceAction = "download" | "remove";

export function formatVoiceSize(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "size unavailable";
  if (bytes < 1_000) return `${Math.round(bytes)} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1_000)} KB`;
  if (bytes < 1_000_000_000) return `${Math.round(bytes / 1_000_000)} MB`;
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(bytes / 1_000_000_000)} GB`;
}

export function voiceActionReason(target: VoiceTarget, action: VoiceAction, model: VoiceModelStatus | null | undefined): string | undefined {
  if (!model) return undefined;
  const name = target === "stt" ? "Speech-to-text model" : "Text-to-speech model";
  if (action === "download") {
    if (model.state === "ready" || model.state === "loaded") return `The ${name.toLowerCase()} is already ready.`;
    if (model.state === "downloading") return "A download is already in progress.";
  } else {
    if (model.state === "absent") return `The ${name.toLowerCase()} is not in the Hugging Face cache.`;
    if (model.state === "downloading") return "A model cannot be removed while it is downloading.";
  }
  return undefined;
}

export function voiceCommandReason(id: string): string | undefined {
  const action = id === "voice.download_stt" ? "download"
    : id === "voice.remove_stt" ? "remove"
      : id === "voice.download_tts" ? "download"
        : id === "voice.remove_tts" ? "remove" : null;
  if (!action) return undefined;
  const target: VoiceTarget = id.endsWith("_tts") ? "tts" : "stt";
  return voiceActionReason(target, action, voiceStatus()?.[target]);
}

let requestSequence = 0;
export async function refreshVoiceStatus(): Promise<VoiceStatus> {
  const sequence = ++requestSequence;
  voiceStatusLoading.set(true);
  try {
    const status = await request<VoiceStatus>("GET", "/api/voice");
    if (sequence === requestSequence) {
      voiceStatus.set(status);
      voiceStatusError.set(null);
    }
    return status;
  } catch (error) {
    if (sequence === requestSequence) voiceStatusError.set(errorMessage(error));
    throw error;
  } finally {
    if (sequence === requestSequence) voiceStatusLoading.set(false);
  }
}

const monitoring = new Set<VoiceTarget>();
const targetLabel = (target: VoiceTarget) => target === "stt" ? "Speech-to-text" : "Text-to-speech";
const modelStatus = (status: VoiceStatus, target: VoiceTarget) => status[target];

/** Refresh status each time Settings opens and resume observing an in-flight download. */
export function refreshVoiceOnSettingsOpen() {
  void refreshVoiceStatus().then((status) => {
    for (const target of ["stt", "tts"] as const) {
      if (modelStatus(status, target).state === "downloading") void monitorDownload(target);
    }
  }).catch(() => {});
}

async function ensureStatus(target: VoiceTarget): Promise<VoiceModelStatus | null> {
  const existing = voiceStatus()?.[target];
  if (existing) return existing;
  try { return (await refreshVoiceStatus())[target]; }
  catch (error) {
    toast("error", `Could not read voice model status: ${errorMessage(error)}`);
    return null;
  }
}

export async function askVoiceDownload(target: VoiceTarget) {
  let model = await ensureStatus(target);
  if (!model) return;
  try { model = (await refreshVoiceStatus())[target]; }
  catch (error) { toast("error", `Could not read voice model status: ${errorMessage(error)}`); return; }
  const reason = voiceActionReason(target, "download", model);
  if (reason) { toast("info", reason); return; }
  confirmDialog.set({
    title: `Download ${targetLabel(target).toLowerCase()} model?`,
    body: `Repo: ${model.repo}\nSize: About ${formatVoiceSize(model.size)}\nSource: Hugging Face\nDestination: the standard Hugging Face cache.\n\nThe model is downloaded only after you confirm.`,
    confirm: "Download",
    destructive: false,
    run: () => startDownload(target, model!),
  });
}

async function startDownload(target: VoiceTarget, model: VoiceModelStatus) {
  const endpoint = `/api/voice/${target}/download`;
  let started: any;
  try {
    started = await request<any>("POST", endpoint, { key: newKey() });
  } catch (error) {
    const latest = await refreshVoiceStatus().catch(() => null);
    if (error instanceof ApiError && error.code === "voice_busy" && latest?.[target].state === "downloading") {
      setVoiceProgress(target, { model: target, state: "downloading", bytes_total: latest[target].size });
      toast("info", `Downloading ${latest[target].repo} from Hugging Face…`);
      void monitorDownload(target);
      return;
    }
    throw error;
  }
  const initialState = started?.state ?? "downloading";
  setVoiceProgress(target, {
    model: target, state: initialState,
    bytes_done: started?.bytes_done ?? 0, bytes_total: started?.bytes_total ?? model.size,
    error: started?.error ?? null,
  });
  voiceStatus.update((status) => status ? {
    ...status,
    [target]: { ...status[target], state: initialState, error: started?.error ?? null },
  } : null);

  let status: VoiceStatus;
  try { status = await refreshVoiceStatus(); }
  catch (error) {
    toast("error", `Download started, but status could not be refreshed: ${errorMessage(error)}`);
    return;
  }
  const current = status[target];
  if (current.state === "downloading") {
    toast("info", `Downloading ${model.repo} from Hugging Face…`);
    void monitorDownload(target);
  } else if (current.state === "ready" || current.state === "loaded") {
    toast("success", `${targetLabel(target)} model is ready`);
    setVoiceProgress(target, null);
  } else if (current.state === "error" || current.state === "unsupported" || current.state === "runtime_missing") {
    toast("error", current.error ?? `${targetLabel(target)} model status: ${current.state}`);
  }
}

async function monitorDownload(target: VoiceTarget) {
  if (monitoring.has(target)) return;
  monitoring.add(target);
  try {
    while (voiceStatus()?.[target]?.state === "downloading") {
      await sleep(800);
      const status = await refreshVoiceStatus();
      const model = status[target];
      if (model.state === "downloading") continue;
      if (model.state === "ready" || model.state === "loaded") {
        toast("success", `${targetLabel(target)} model is ready`);
        setVoiceProgress(target, null);
      } else if (model.state === "error" || model.state === "unsupported" || model.state === "runtime_missing") {
        toast("error", model.error ?? `${targetLabel(target)} model status: ${model.state}`);
      }
    }
  } catch (error) {
    toast("error", `Could not refresh voice model status: ${errorMessage(error)}`);
  } finally {
    monitoring.delete(target);
  }
}

export async function askVoiceRemove(target: VoiceTarget) {
  let model = await ensureStatus(target);
  if (!model) return;
  try { model = (await refreshVoiceStatus())[target]; }
  catch (error) { toast("error", `Could not read voice model status: ${errorMessage(error)}`); return; }
  const reason = voiceActionReason(target, "remove", model);
  if (reason) { toast("info", reason); return; }
  confirmDialog.set({
    title: `Remove ${targetLabel(target).toLowerCase()} model?`,
    body: `Remove only ${model.repo} from the Hugging Face cache?\nOther cached repositories will not be changed.`,
    confirm: "Remove",
    destructive: true,
    run: async () => {
      let result: any;
      try {
        result = await request<any>("DELETE", `/api/voice/${target}`, { key: newKey() });
      } catch (error) {
        await refreshVoiceStatus().catch(() => {});
        throw error;
      }
      try { await refreshVoiceStatus(); }
      catch (error) {
        toast("error", `Removed ${result?.repo ?? model!.repo}, but status could not be refreshed: ${errorMessage(error)}`);
        return;
      }
      toast("success", `Removed ${result?.repo ?? model!.repo} from the Hugging Face cache (${formatVoiceSize(result?.freed)} freed).`);
    },
  });
}

export function voiceInfoLines(target: VoiceTarget): string[] {
  const status = voiceStatus();
  if (!status) {
    const error = voiceStatusError();
    return [voiceStatusLoading() ? "Loading voice model status…" : `Status unavailable${error ? `: ${error}` : ""}`];
  }
  const model = status[target];
  const progress = target === "stt" ? voiceDownloadProgress() : ttsDownloadProgress();
  const lines = [
    `State: ${model.state}${model.state === "downloading" ? progressText(progress) : ""}`,
    `Repo: ${model.repo || "not reported"}`,
    `Size: ${formatVoiceSize(model.size)}`,
    `Cache path: ${model.cache_path || "not in the Hugging Face cache"}`,
  ];
  if (model.error) lines.push(`Error: ${model.error}`);
  if (model.state === "runtime_missing" && !/nexus voice init/i.test(model.error ?? "")) lines.push("Install the optional runtime: run nexus voice init, then restart Core.");
  const refreshError = voiceStatusError();
  if (refreshError) lines.push(`Status refresh failed: ${refreshError}`);
  return lines;
}

function progressText(progress: ReturnType<typeof voiceDownloadProgress>): string {
  if (!progress) return "";
  if (typeof progress.bytes_total === "number" && progress.bytes_total > 0 && typeof progress.bytes_done === "number") {
    const percent = Math.max(0, Math.min(100, Math.round(progress.bytes_done / progress.bytes_total * 100)));
    return ` · ${percent}% (${formatVoiceSize(progress.bytes_done)} / ${formatVoiceSize(progress.bytes_total)})`;
  }
  return " · download in progress";
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
}
