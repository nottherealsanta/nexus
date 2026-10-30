import { API_BASE, ApiError, request, sleep } from "./core/transport";
import { downmixChannels, MAX_RECORDING_SECONDS, MIN_RECORDING_SECONDS, PREVIEW_WINDOW_SECONDS, previewDue, recordingToWav, rmsLevel, transitionDictation } from "./dictation-logic";
import type { DictationEvent, DictationPhase } from "./dictation-logic";
import { confirmDialog, toast } from "./store";
import { dictationLevel, dictationPhase, dictationPreview, voiceDownloadProgress } from "./voice-state";

type VoiceModel = { state: string; repo: string; size: number; error?: string | null; bytes_done?: number; bytes_total?: number };
type VoiceStatus = { stt: VoiceModel; tts?: unknown };

type AudioNodeLike = { connect(destination: unknown): unknown; disconnect(): void };
type GainNodeLike = AudioNodeLike & { gain: { value: number } };
type PortLike = { onmessage: ((event: { data: any }) => void) | null; postMessage(message: unknown): void };
type WorkletNodeLike = AudioNodeLike & { port: PortLike };
type ScriptProcessorLike = AudioNodeLike & { onaudioprocess: ((event: any) => void) | null };
type AudioContextLike = {
  sampleRate: number;
  destination: unknown;
  audioWorklet?: { addModule(url: string): Promise<void> };
  createMediaStreamSource(stream: unknown): AudioNodeLike;
  createGain(): GainNodeLike;
  createScriptProcessor?(bufferSize?: number, inputChannels?: number, outputChannels?: number): ScriptProcessorLike;
  resume(): Promise<void>;
  close(): Promise<void>;
};
type AudioContextConstructor = new () => AudioContextLike;
type MediaStreamLike = { getTracks(): { stop(): void }[] };
type Recorder = { stop(): Promise<void> };
type Capture = {
  generation: number;
  sampleRate: number;
  chunks: Float32Array[];
  sampleCount: number;
  previousPreviewCount: number;
  recorder: Recorder;
  insert: (text: string) => void;
  active: boolean;
  cancelled: boolean;
  previewWarningShown: boolean;
  stopPromise?: Promise<void>;
  finalController?: AbortController;
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

function environment() {
  return globalThis as unknown as {
    navigator?: { mediaDevices?: { getUserMedia?: (constraints: unknown) => Promise<MediaStreamLike> } };
    AudioContext?: AudioContextConstructor;
    webkitAudioContext?: AudioContextConstructor;
    AudioWorkletNode?: new (context: object, name: string, options?: unknown) => WorkletNodeLike;
    Blob?: new (parts: string[], options?: { type?: string }) => unknown;
    URL?: { createObjectURL(blob: unknown): string; revokeObjectURL(url: string): void };
  };
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

const WORKLET_SOURCE = `
class NexusDictationProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.used = 0;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === "flush") {
        this.emit();
        this.port.postMessage({ type: "flushed" });
      }
    };
  }
  emit() {
    if (!this.used) return;
    const samples = this.buffer.slice(0, this.used);
    this.buffer = new Float32Array(2048);
    this.used = 0;
    this.port.postMessage({ type: "samples", samples }, [samples.buffer]);
  }
  process(inputs, outputs) {
    for (const channel of (outputs[0] || [])) channel.fill(0);
    const channels = inputs[0] || [];
    const length = channels[0] ? channels[0].length : 0;
    for (let i = 0; i < length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] || 0;
      this.buffer[this.used++] = value / Math.max(1, channels.length);
      if (this.used === this.buffer.length) this.emit();
    }
    return true;
  }
}
registerProcessor("nexus-dictation-capture", NexusDictationProcessor);
`;

async function openRecorder(onSamples: (samples: Float32Array) => void): Promise<{ rate: number; recorder: Recorder }> {
  const root = environment();
  const getUserMedia = root.navigator?.mediaDevices?.getUserMedia;
  const Context = root.AudioContext ?? root.webkitAudioContext;
  if (!getUserMedia) throw new Error("Microphone capture is not available in this client");
  if (!Context) throw new Error("Web Audio is not available in this client");
  const stream = await getUserMedia({ audio: { channelCount: { ideal: 1 }, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  let context: AudioContextLike | null = null;
  let source: AudioNodeLike | null = null;
  let processor: AudioNodeLike | null = null;
  let gain: GainNodeLike | null = null;
  let workletPort: PortLike | null = null;
  let workletUrl: string | null = null;
  try {
    context = new Context();
    await context.resume();
    source = context.createMediaStreamSource(stream);
    gain = context.createGain();
    gain.gain.value = 0;
    gain.connect(context.destination);

    const WorkletNode = root.AudioWorkletNode;
    const urlApi = root.URL;
    if (context.audioWorklet && WorkletNode && root.Blob && urlApi) {
      try {
        workletUrl = urlApi.createObjectURL(new root.Blob([WORKLET_SOURCE], { type: "text/javascript" }));
        await context.audioWorklet.addModule(workletUrl);
        urlApi.revokeObjectURL(workletUrl);
        workletUrl = null;
        const node = new WorkletNode(context as unknown as object, "nexus-dictation-capture", {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        });
        processor = node;
        workletPort = node.port;
        node.port.onmessage = (event) => {
          if (event.data?.type === "samples" && event.data.samples instanceof Float32Array) onSamples(event.data.samples);
          if (event.data?.type === "flushed") flushDone?.();
        };
        source.connect(node);
        node.connect(gain);
        return {
          rate: context.sampleRate,
          recorder: {
            async stop() {
              if (workletPort) {
                await new Promise<void>((resolve) => {
                  let done = false;
                  flushDone = () => { if (!done) { done = true; clearTimeout(timer); resolve(); } };
                  const timer = setTimeout(() => flushDone?.(), 250);
                  workletPort!.postMessage({ type: "flush" });
                });
              }
              source?.disconnect(); processor?.disconnect(); gain?.disconnect();
              stream.getTracks().forEach((track) => track.stop());
              await context?.close();
              context = null;
            },
          },
        };
      } catch {
        if (workletUrl) { try { urlApi?.revokeObjectURL(workletUrl); } catch {} workletUrl = null; }
        try { processor?.disconnect(); } catch {}
        processor = null;
        workletPort = null;
      }
    }

    if (!context.createScriptProcessor) throw new Error("Neither AudioWorklet nor ScriptProcessor capture is available");
    const script = context.createScriptProcessor(4096, 2, 1);
    processor = script;
    script.onaudioprocess = (event: any) => {
      const input = event.inputBuffer;
      const channels: Float32Array[] = [];
      for (let i = 0; i < input.numberOfChannels; i++) channels.push(input.getChannelData(i));
      const mono = downmixChannels(channels);
      const output = event.outputBuffer;
      for (let i = 0; i < output.numberOfChannels; i++) output.getChannelData(i).fill(0);
      onSamples(mono);
    };
    source.connect(script);
    script.connect(gain);
    return {
      rate: context.sampleRate,
      recorder: {
        async stop() {
          script.onaudioprocess = null;
          source?.disconnect(); processor?.disconnect(); gain?.disconnect();
          stream.getTracks().forEach((track) => track.stop());
          await context?.close();
          context = null;
        },
      },
    };
  } catch (error) {
    try { source?.disconnect(); processor?.disconnect(); gain?.disconnect(); } catch {}
    stream.getTracks().forEach((track) => track.stop());
    if (context) { try { await context.close(); } catch {} }
    throw error;
  }
}

let flushDone: (() => void) | null = null;

async function responseError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let body: any;
  try { body = JSON.parse(text); } catch {}
  return new ApiError(response.status, body?.error ?? (text.slice(0, 200) || `HTTP ${response.status}`), body?.code, body);
}

async function transcribe(wav: Uint8Array, partial: boolean, signal?: AbortSignal): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/voice/transcribe${partial ? "?partial=1" : ""}`, {
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
      const opened = await openRecorder((samples) => {
        const current = capture;
        if (current && current.generation === expectedGeneration) addSamples(current, samples);
      });
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
  await stopRecorder(current);
  cue([660, 440]);
  if (previewInFlight) await previewInFlight.catch(() => {});
  if (current.cancelled || current.generation !== generation || dictationPhase() !== "stopping") return;
  const duration = current.sampleCount / current.sampleRate;
  if (duration < MIN_RECORDING_SECONDS) {
    move("finish");
    capture = null;
    dictationPreview.set("");
    dictationLevel.set(0);
    toast("info", "Record at least 0.2 seconds of audio to dictate.");
    return;
  }
  const samples = allSamples(current.chunks, current.sampleCount);
  const wav = recordingToWav(samples, current.sampleRate);
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
  const current = capture;
  generation++;
  if (current) {
    const wasActive = current.active;
    current.active = false;
    current.cancelled = true;
    current.finalController?.abort();
    void stopRecorder(current).then(() => { if (wasActive) cue([660, 440]); });
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
