import { describe, expect, test } from "nsh/test";
import { commands, handlers } from "./src/commands";
import { formatVoiceSize, voiceActionReason, voiceCommandReason, voiceInfoLines } from "./src/voice-settings";
import { receiveVoiceProgress, ttsDownloadProgress, voiceDownloadProgress, voiceStatus, voiceStatusError, voiceStatusLoading } from "./src/voice-state";
import type { VoiceStatus } from "./src/voice-state";

const sttRepo = "ConwayResearch/Underdog-Bark-3.0-Clean";
const ttsRepo = "gradium/phonon-31m-en";
const status = (): VoiceStatus => ({
  stt: { state: "absent", repo: sttRepo, revision: "main", size: 181_000_000, cache_path: null, error: null },
  tts: { state: "absent", repo: ttsRepo, revision: "main", size: 65_000_000, cache_path: null, error: null },
});

describe("voice settings", () => {
  test("registers the four manifest voice-management commands", () => {
    const ids = ["voice.download_stt", "voice.remove_stt", "voice.download_tts", "voice.remove_tts"];
    expect(ids.map((id) => commands.some((command) => command.id === id))).toEqual([true, true, true, true]);
    expect(ids.map((id) => handlers.has(id))).toEqual([true, true, true, true]);
  });

  test("formats model sizes and disables only meaningless actions", () => {
    expect(formatVoiceSize(181_000_000)).toBe("181 MB");
    expect(formatVoiceSize(65_000_000)).toBe("65 MB");
    expect(formatVoiceSize(null)).toBe("size unavailable");
    expect(voiceActionReason("stt", "download", { ...status().stt, state: "ready" })).toBe("The speech-to-text model is already ready.");
    expect(voiceActionReason("tts", "download", { ...status().tts, state: "downloading" })).toBe("A download is already in progress.");
    expect(voiceActionReason("stt", "remove", status().stt)).toBe("The speech-to-text model is not in the Hugging Face cache.");
    expect(voiceActionReason("tts", "remove", { ...status().tts, state: "downloading" })).toBe("A model cannot be removed while it is downloading.");
    expect(voiceActionReason("stt", "download", status().stt)).toBe(undefined);
  });

  test("palette commands report state-based reasons", () => {
    voiceStatus.set({ ...status(), stt: { ...status().stt, state: "ready" }, tts: { ...status().tts, state: "downloading" } });
    expect(voiceCommandReason("voice.download_stt")).toBe("The speech-to-text model is already ready.");
    expect(voiceCommandReason("voice.remove_tts")).toBe("A model cannot be removed while it is downloading.");
    expect(voiceCommandReason("voice.remove_stt")).toBe(undefined);
    voiceStatus.set(null);
  });

  test("info rows include state, repo, human size, cache path and runtime guidance", () => {
    voiceStatus.set({
      stt: { ...status().stt, state: "downloading" },
      tts: { ...status().tts, state: "runtime_missing", error: "optional speech runtime is not installed" },
    });
    voiceDownloadProgress.set({ model: "stt", state: "downloading", bytes_done: 10_000_000, bytes_total: 181_000_000 });
    const stt = voiceInfoLines("stt").join("\n");
    const tts = voiceInfoLines("tts").join("\n");
    expect(stt.includes("State: downloading · 6% (10 MB / 181 MB)") && stt.includes(sttRepo) && stt.includes("181 MB") && stt.includes("Cache path: not in the Hugging Face cache")).toBe(true);
    expect(tts.includes("runtime_missing") && tts.includes(ttsRepo) && tts.includes("65 MB") && tts.includes("optional speech runtime is not installed") && tts.includes("nexus voice init")).toBe(true);
    voiceStatus.set(null);
    voiceDownloadProgress.set(null);
  });

  test("STT and TTS progress events update separate settings state and model states", () => {
    voiceStatus.set(status());
    receiveVoiceProgress({ model: "stt", state: "downloading", bytes_done: 10, bytes_total: 100 });
    receiveVoiceProgress({ target: "tts", state: "downloading", bytes_done: 20, bytes_total: 200 });
    expect(voiceDownloadProgress()?.bytes_done).toBe(10);
    expect(ttsDownloadProgress()?.bytes_done).toBe(20);
    expect(voiceStatus()?.stt.state).toBe("downloading");
    expect(voiceStatus()?.tts.state).toBe("downloading");
    voiceStatus.set(null);
    voiceDownloadProgress.set(null);
    ttsDownloadProgress.set(null);
  });

  test("a missing status distinguishes loading from refresh errors", () => {
    voiceStatus.set(null);
    voiceStatusLoading.set(true);
    voiceStatusError.set(null);
    expect(voiceInfoLines("stt")).toEqual(["Loading voice model status…"]);
    voiceStatusLoading.set(false);
    voiceStatusError.set("Core unavailable");
    expect(voiceInfoLines("tts")).toEqual(["Status unavailable: Core unavailable"]);
    voiceStatusError.set(null);
  });
});
