import { ApiError, apiUrl, request, sleep } from "./core/transport";
import { nativeModule } from "nsh";
import type { MicrophoneModule } from "nexus/native";
import { openRecorder } from "./audio-capture";
import type { CaptureRecorder } from "./audio-capture";
import { hasMicrophoneCapture, rmsLevel } from "./audio-capture-logic";
import { MAX_RECORDING_SECONDS, MIN_RECORDING_SECONDS, PREVIEW_WINDOW_SECONDS, previewDue, recordingToWav, transitionDictation } from "./dictation-logic";
import type { DictationEvent, DictationPhase } from "./dictation-logic";
import { confirmDialog, toast } from "./store";
import { dictationLevel, dictationPhase, dictationPreview, voiceDownloadProgress } from "./voice-state";

// A static `nexus/native` import makes every test that imports the commands
// (even for a pure helper) load the native module outside the app bundle. Look
// it up at call time instead; the app registers it on every target.
let nativeMic: MicrophoneModule | null | undefined;
function microphone(): MicrophoneModule | null {
  if (nativeMic !== undefined) return nativeMic;
  try {
    nativeMic = nativeModule<{ Microphone: MicrophoneModule }>("nexus/native").Microphone ?? null;
  } catch {
    nativeMic = null;
  }
  return nativeMic;
}

type VoiceModel = { state: string; repo: string; size: number; error?: string | null; bytes_done?: number; bytes_total?: number };
type VoiceStatus = { stt: VoiceModel; tts?: unknown };

type NavigatorLike = { mediaDevices?: { getUserMedia?: unknown } };
type NativeCapture = { started: number; timer: ReturnType<typeof setInterval> };
type Capture = {
  generation: number;
  sampleRate: number;
  chunks: Float32Array[];
  sampleCount: number;
  previousPreviewCount: number;
  recorder: CaptureRecorder;
  insert: (text: string) => void;
  active: boolean;
  cancelled: boolean;
  previewWarningShown: boolean;
  stopPromise?: Promise<void>;
  finalController?: AbortController;
  native?: NativeCapture;
};

let generation = 0;
let capture: Capture | null = null;
let previewInFlight: Promise<void> | null = null;
function move(event: DictationEvent) {
  dictationPhase.set(transitionDictation(dictationPhase(), event));
}

export function isDictationRecording(): boolean {
  return dictationPhase() === "recording" || dictationPhase() === "stopping";
}

export function isDictationBusy(): boolean {
  return dictationPhase() !== "idle";
}

/** Whether this client can open a microphone at all. */
export function microphoneAvailable(): boolean {
  return hasMicrophoneCapture(environment().navigator) || (microphone()?.available() ?? false);
}

function environment(): { navigator?: NavigatorLike } {
  return globalThis as unknown as { navigator?: NavigatorLike };
}

function sampleCount(samples: readonly Float32Array[]): number {
  return samples.reduce((sum, chunk) => sum + chunk.length, 0);
}

function tail(samples: readonly Float32Array[], length: number): Float32Array {
  const count = Math.min(sampleCount(samples), length);
  const out = new Float32Array(count);
  let skip = sampleCount(samples) - count;
  let offset = 0;
  for (const chunk of samples) {
    if (skip >= chunk.length) { skip -= chunk.length; continue; }
    const start = skip;
    skip = 0;
    const take = Math.min(chunk.length - start, count - offset);
    out.set(chunk.subarray(start, start + take), offset);
    offset += take;
    if (offset >= count) break;
  }
  return out;
}

function allSamples(samples: readonly Float32Array[], count: number): Float32Array {
  const out = new Float32Array(count);
  let offset = 0;
  for (const chunk of samples) {
    const take = Math.min(chunk.length, count - offset);
    out.set(chunk.subarray(0, take), offset);
    offset += take;
    if (offset >= count) break;
  }
  return out;
}

async function responseError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let body: any;
  try { body = JSON.parse(text); } catch {}
  return new ApiError(response.status, body?.error ?? (text.slice(0, 200) || `HTTP ${response.status}`), body?.code, body);
}

async function transcribe(wav: Uint8Array, partial: boolean, signal?: AbortSignal): Promise<string> {
  let response: Response;
  try {
    response = await fetch(apiUrl(`/api/voice/transcribe${partial ? "?partial=1" : ""}`), {
      method: "POST",
      headers: { accept: "application/json", "content-type": "audio/wav" },
      body: wav.buffer as ArrayBuffer,
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(0, `network error: ${(error as Error)?.message ?? error}`);
  }
  if (!response.ok) throw await responseError(response);
  const body = await response.json() as { text?: unknown };
  if (typeof body.text !== "string") throw new Error("Core returned no transcript");
  return body.text;
}

function maybePreview(current: Capture) {
  if (!current.active || capture !== current || previewInFlight) return;
  if (!previewDue(current.sampleCount, current.previousPreviewCount, current.sampleRate, false)) return;
  const boundary = current.sampleCount;
  current.previousPreviewCount = boundary;
  const maxWindow = Math.ceil(PREVIEW_WINDOW_SECONDS * current.sampleRate);
  const samples = tail(current.chunks, maxWindow);
  const wav = recordingToWav(samples, current.sampleRate);
  const task = transcribe(wav, true).then((text) => {
    if (current.active && capture === current && current.generation === generation) dictationPreview.set(text.trim());
  }).catch((error) => {
    if (error instanceof ApiError && error.code === "voice_busy") return;
    if (!current.previewWarningShown && current.active && capture === current) {
      current.previewWarningShown = true;
      toast("info", `Live transcription unavailable: ${error instanceof Error ? error.message : String(error)}`);
    }
  }).finally(() => {
    if (previewInFlight === task) previewInFlight = null;
    if (current.active && capture === current) maybePreview(current);
  });
  previewInFlight = task;
}

function addSamples(current: Capture, incoming: Float32Array) {
  if (current.cancelled || capture !== current || (!current.active && dictationPhase() !== "stopping") || !incoming.length) return;
  const maxCount = Math.ceil(MAX_RECORDING_SECONDS * current.sampleRate);
  const remaining = Math.max(0, maxCount - current.sampleCount);
  if (remaining <= 0) {
    void stopDictation();
    return;
  }
  const samples = incoming.length > remaining ? incoming.subarray(0, remaining) : incoming;
  current.chunks.push(samples.slice());
  current.sampleCount += samples.length;
  dictationLevel.set(rmsLevel(samples));
  maybePreview(current);
  if (current.sampleCount >= maxCount) {
    toast("info", "Dictation reached the 120 second limit; transcribing the recording.");
    void stopDictation();
  }
}

function readyState(state: string): boolean { return state === "ready" || state === "loaded"; }

function cue(frequencies: number[]) {
  try {
    const root = globalThis as unknown as { AudioContext?: new () => any; webkitAudioContext?: new () => any };
    const Context = root.AudioContext ?? root.webkitAudioContext;
    if (!Context) return;
    const context = new Context();
    const now = context.currentTime;
    frequencies.forEach((frequency, index) => {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const start = now + index * 0.12;
      oscillator.frequency.value = frequency;
      oscillator.type = "sine";
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.08, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.11);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.12);
    });
    setTimeout(() => void context.close().catch(() => {}), frequencies.length * 120 + 100);
  } catch { /* Audio cues are optional. */ }
}

function beginCapture(insert: (text: string) => void, expectedGeneration: number, loaded = false) {
  void (async () => {
    let loadingCue: ReturnType<typeof setInterval> | undefined;
    try {
      if (!loaded) {
        cue([440]);
        loadingCue = setInterval(() => {
          if (generation !== expectedGeneration || dictationPhase() !== "requesting") {
            clearInterval(loadingCue);
          } else cue([440]);
        }, 1500);
      }
      try {
        await request("POST", "/api/voice/stt/prepare");
      } finally {
        if (loadingCue) clearInterval(loadingCue);
      }
      if (generation !== expectedGeneration || dictationPhase() !== "requesting") return;
      const mic = microphone();
      if (!hasMicrophoneCapture(environment().navigator) && mic?.available()) {
        await mic.start();
        if (generation !== expectedGeneration || dictationPhase() !== "requesting") {
          await mic.cancel();
          return;
        }
        const started = Date.now();
        const timer = setInterval(() => {
          if ((Date.now() - started) / 1000 >= MAX_RECORDING_SECONDS) void stopDictation();
          else dictationLevel.set(mic.level());
        }, 100);
        move("ready");
        capture = {
          generation: expectedGeneration,
          sampleRate: 16_000,
          chunks: [],
          sampleCount: 0,
          previousPreviewCount: 0,
          recorder: { stop: async () => { clearInterval(timer); } },
          insert,
          active: true,
          cancelled: false,
          previewWarningShown: false,
          native: { started, timer },
        };
        dictationPreview.set("");
        dictationLevel.set(0);
        return;
      }
      const opened = await openRecorder((samples) => {
        const current = capture;
        if (current && current.generation === expectedGeneration) addSamples(current, samples);
      }, "dictation");
      if (generation !== expectedGeneration || dictationPhase() !== "requesting") {
        await opened.recorder.stop();
        return;
      }
      cue([660, 880]);
      // Let the cue and the recorder's buffered tail pass before accepting samples.
      await sleep(350);
      if (generation !== expectedGeneration || dictationPhase() !== "requesting") {
        await opened.recorder.stop();
        return;
      }
      move("ready");
      capture = {
        generation: expectedGeneration,
        sampleRate: opened.rate,
        chunks: [],
        sampleCount: 0,
        previousPreviewCount: 0,
        recorder: opened.recorder,
        insert,
        active: true,
        cancelled: false,
        previewWarningShown: false,
      };
      dictationPreview.set("");
      dictationLevel.set(0);
    } catch (error) {
      if (generation !== expectedGeneration) return;
      move("fail");
      const name = (error as { name?: string })?.name;
      if (name === "NotAllowedError" || name === "PermissionDeniedError" || name === "SecurityError") {
        toast("error", "Microphone permission was denied. Allow microphone access for Nexus and try again.");
      } else {
        toast("error", error instanceof Error ? error.message : String(error));
      }
    }
  })();
}

function startDownload(insert: (text: string) => void, expectedGeneration: number) {
  void (async () => {
    try {
      const state = await request<any>("POST", "/api/voice/stt/download");
      if (generation !== expectedGeneration || dictationPhase() !== "downloading") return;
      voiceDownloadProgress.set({ model: "stt", ...state });
      void waitForDownload(insert, expectedGeneration);
    } catch (error) {
      if (generation !== expectedGeneration) return;
      if (error instanceof ApiError && error.code === "voice_busy") {
        try {
          const status = await request<VoiceStatus>("GET", "/api/voice");
          if (status.stt.state === "downloading") {
            voiceDownloadProgress.set({ model: "stt", state: "downloading", bytes_total: status.stt.size });
            void waitForDownload(insert, expectedGeneration);
            return;
          }
        } catch { /* report the original error */ }
      }
      move("fail");
      toast("error", error instanceof ApiError ? error.message : String(error));
    }
  })();
}

async function waitForDownload(insert: (text: string) => void, expectedGeneration: number) {
  while (generation === expectedGeneration && dictationPhase() === "downloading") {
    try {
      const status = await request<VoiceStatus>("GET", "/api/voice");
      if (generation !== expectedGeneration || dictationPhase() !== "downloading") return;
      const model = status.stt;
      const eventProgress = voiceDownloadProgress();
      voiceDownloadProgress.set({
        model: "stt", state: model.state,
        bytes_done: eventProgress?.bytes_done,
        bytes_total: model.size || eventProgress?.bytes_total,
        error: model.error,
      });
      if (readyState(model.state)) {
        move("ready");
        voiceDownloadProgress.set(null);
        beginCapture(insert, expectedGeneration, model.state === "loaded");
        return;
      }
      if (model.state === "error" || model.state === "unsupported" || model.state === "runtime_missing") {
        move("fail");
        toast("error", model.error ?? `Speech recognition is ${model.state}`);
        return;
      }
    } catch (error) {
      if (generation !== expectedGeneration) return;
      move("fail");
      toast("error", error instanceof ApiError ? error.message : String(error));
      return;
    }
    await sleep(800);
  }
}

/** Check STT availability, request download consent if needed, then begin microphone capture. */
export function startDictation(insert: (text: string) => void) {
  if (dictationPhase() !== "idle") return;
  const expectedGeneration = ++generation;
  move("start");
  dictationPreview.set("");
  dictationLevel.set(0);
  void (async () => {
    try {
      const status = await request<VoiceStatus>("GET", "/api/voice");
      if (generation !== expectedGeneration || dictationPhase() !== "checking") return;
      const stt = status.stt;
      if (readyState(stt.state)) {
        move("request");
        beginCapture(insert, expectedGeneration, stt.state === "loaded");
      } else if (stt.state === "absent") {
        move("consent");
        confirmDialog.set({
          title: "Download speech recognition model?",
          body: `Repo: ${stt.repo}\nSize: ${formatModelSize(stt.size)}\n\nThis downloads the model into the Hugging Face cache. The microphone starts after the model is ready.`,
          confirm: "Download",
          destructive: false,
          run: () => {
            if (generation !== expectedGeneration || dictationPhase() !== "consent") return;
            move("download");
            voiceDownloadProgress.set({ model: "stt", state: "downloading", bytes_done: 0, bytes_total: stt.size });
            startDownload(insert, expectedGeneration);
          },
        });
      } else if (stt.state === "downloading") {
        move("download");
        voiceDownloadProgress.set({ model: "stt", state: "downloading", bytes_total: stt.size });
        void waitForDownload(insert, expectedGeneration);
      } else {
        move("fail");
        toast("error", stt.error ?? `Speech recognition is ${stt.state}`);
      }
    } catch (error) {
      if (generation !== expectedGeneration) return;
      move("fail");
      toast("error", error instanceof ApiError ? error.message : String(error));
    }
  })();
}

function formatModelSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "Model size unavailable";
  return `About ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(bytes / 1_000_000)} MB`;
}

/** Stop capture, send a final transcription request, and append only editable composer text. */
export async function stopDictation() {
  const current = capture;
  if (!current || dictationPhase() !== "recording") return;
  move("stop");
  current.active = false;
  let wav: Uint8Array | null = null;
  let duration: number;
  if (current.native) {
    clearInterval(current.native.timer);
    duration = Math.min(MAX_RECORDING_SECONDS, (Date.now() - current.native.started) / 1000);
    try {
      const mic = microphone();
      if (!mic) throw new Error("Microphone capture is not available in this client");
      wav = await mic.stop();
    } catch (error) {
      if (current.generation === generation) {
        move("fail");
        capture = null;
        dictationLevel.set(0);
        toast("error", error instanceof Error ? error.message : String(error));
      }
      return;
    }
  } else {
    await stopRecorder(current);
    cue([660, 440]);
    duration = current.sampleCount / current.sampleRate;
    if (duration >= MIN_RECORDING_SECONDS) wav = recordingToWav(allSamples(current.chunks, current.sampleCount), current.sampleRate);
  }
  if (previewInFlight) await previewInFlight.catch(() => {});
  if (current.cancelled || current.generation !== generation || dictationPhase() !== "stopping") return;
  if (duration < MIN_RECORDING_SECONDS || !wav) {
    move("finish");
    capture = null;
    dictationPreview.set("");
    dictationLevel.set(0);
    toast("info", "Record at least 0.2 seconds of audio to dictate.");
    return;
  }
  const controller = new AbortController();
  current.finalController = controller;
  try {
    let transcript = "";
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        transcript = await transcribe(wav, false, controller.signal);
        break;
      } catch (error) {
        if (!(error instanceof ApiError) || error.code !== "voice_busy" || attempt === 2) throw error;
        await sleep(500 * (attempt + 1));
      }
    }
    if (!current.cancelled && current.generation === generation && transcript.trim()) current.insert(transcript);
    else if (!current.cancelled && current.generation === generation) toast("info", "No speech was recognized.");
  } catch (error) {
    if (!current.cancelled && current.generation === generation) toast("error", error instanceof ApiError ? error.message : String(error));
  } finally {
    if (current.generation === generation && dictationPhase() === "stopping") move("finish");
    if (capture === current) capture = null;
    dictationPreview.set("");
    dictationLevel.set(0);
  }
}

/** Esc cancels the recording and its pending preview/final result; it never sends text. */
export function cancelDictation() {
  const phase: DictationPhase = dictationPhase();
  if (phase === "idle") return;
  if (phase === "requesting") void microphone()?.cancel();
  const current = capture;
  generation++;
  if (current) {
    const wasActive = current.active;
    current.active = false;
    current.cancelled = true;
    current.finalController?.abort();
    if (current.native) {
      clearInterval(current.native.timer);
      void microphone()?.cancel();
    } else {
      void stopRecorder(current).then(() => { if (wasActive) cue([660, 440]); });
    }
  }
  if (phase === "checking" || phase === "consent" || phase === "downloading" || phase === "requesting") move("cancel");
  else if (phase === "recording" || phase === "stopping") move("cancel");
  capture = null;
  confirmDialog.set(null);
  dictationPreview.set("");
  dictationLevel.set(0);
}

function stopRecorder(current: Capture): Promise<void> {
  current.stopPromise ??= current.recorder.stop().catch(() => {});
  return current.stopPromise;
}
