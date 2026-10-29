import { signal } from "nsh";
import { API_BASE, ApiError } from "./core/transport";
import { parseWavHeader, Pcm16Decoder, WAV_HEADER_SIZE } from "./speech-format";

export const speechPlaying = signal(false);

// The NSH host's shared TS environment does not include lib.dom's Web Audio declarations.
// Keep the narrow surface used here explicit; browser and desktop provide these APIs.
type AudioBufferLike = { duration: number; getChannelData(channel: number): Float32Array };
type AudioSourceLike = {
  buffer: AudioBufferLike | null;
  onended: (() => void) | null;
  connect(destination: unknown): unknown;
  start(when?: number): void;
  stop(): void;
};
type AudioContextLike = {
  currentTime: number;
  destination: unknown;
  createBuffer(channels: number, length: number, sampleRate: number): AudioBufferLike;
  createBufferSource(): AudioSourceLike;
  resume(): Promise<void>;
  close(): Promise<void>;
};
type AudioContextConstructor = new () => AudioContextLike;

type Playback = {
  controller: AbortController;
  context: AudioContextLike;
  sources: Set<AudioSourceLike>;
  reader?: ReadableStreamDefaultReader<Uint8Array>;
  cancelled: boolean;
  streamEnded: boolean;
  sampleRate: number;
  nextStart: number;
  drained: Promise<void>;
  resolveDrained: () => void;
  finished: Promise<void>;
  resolveFinished: () => void;
  stopRequest?: Promise<void>;
};

let active: Playback | null = null;

function audioContext(): AudioContextLike {
  const root = globalThis as unknown as { AudioContext?: AudioContextConstructor; webkitAudioContext?: AudioContextConstructor };
  const Constructor = root.AudioContext ?? root.webkitAudioContext;
  if (!Constructor) throw new Error("Web Audio is not available in this client");
  return new Constructor();
}

function newPlayback(context: AudioContextLike): Playback {
  let resolveDrained!: () => void;
  let resolveFinished!: () => void;
  const drained = new Promise<void>((resolve) => { resolveDrained = resolve; });
  const finished = new Promise<void>((resolve) => { resolveFinished = resolve; });
  return {
    controller: new AbortController(), context, sources: new Set(), cancelled: false,
    streamEnded: false, sampleRate: 0, nextStart: 0,
    drained, resolveDrained, finished, resolveFinished,
  };
}

function finishIfDrained(playback: Playback) {
  if (playback.streamEnded && playback.sources.size === 0) playback.resolveDrained();
}

function stopNodes(playback: Playback) {
  for (const source of [...playback.sources]) {
    try { source.stop(); } catch { /* it may already have ended */ }
  }
}

function schedule(playback: Playback, samples: Float32Array) {
  if (!samples.length || playback.cancelled) return;
  const buffer = playback.context.createBuffer(1, samples.length, playback.sampleRate);
  buffer.getChannelData(0).set(samples);
  const source = playback.context.createBufferSource();
  source.buffer = buffer;
  source.connect(playback.context.destination);
  playback.sources.add(source);
  source.onended = () => {
    playback.sources.delete(source);
    finishIfDrained(playback);
  };
  const start = Math.max(playback.nextStart, playback.context.currentTime + 0.02);
  playback.nextStart = start + buffer.duration;
  source.start(start);
}

async function responseError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let body: any;
  try { body = JSON.parse(text); } catch {}
  return new ApiError(response.status, body?.error ?? (text.slice(0, 200) || `HTTP ${response.status}`), body?.code, body);
}

async function stopRequest(): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/voice/speak/stop`, {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: "{}",
    });
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error)?.message ?? e}`);
  }
  if (!response.ok) throw await responseError(response);
}

async function streamAnswer(playback: Playback, sessionId: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/voice/speak`, {
      method: "POST",
      headers: { accept: "audio/wav", "content-type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
      signal: playback.controller.signal,
    });
  } catch (e) {
    if (playback.cancelled) return;
    throw new ApiError(0, `network error: ${(e as Error)?.message ?? e}`);
  }
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error("Core returned no audio stream");

  const reader = response.body.getReader();
  playback.reader = reader;
  const header = new Uint8Array(WAV_HEADER_SIZE);
  let headerLength = 0;
  let pcm: Pcm16Decoder | null = null;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (playback.cancelled) return;
      let rest = value;
      if (!pcm) {
        const take = Math.min(WAV_HEADER_SIZE - headerLength, rest.byteLength);
        header.set(rest.subarray(0, take), headerLength);
        headerLength += take;
        rest = rest.subarray(take);
        if (headerLength < WAV_HEADER_SIZE) continue;
        playback.sampleRate = parseWavHeader(header).sampleRate;
        pcm = new Pcm16Decoder();
      }
      if (rest.byteLength) schedule(playback, pcm.push(rest));
    }
  } finally {
    playback.reader = undefined;
    try { reader.releaseLock(); } catch {}
  }
  if (playback.cancelled) return;
  if (!pcm) throw new Error("Core ended the stream before sending a WAV header");
  pcm.finish();
  playback.streamEnded = true;
  finishIfDrained(playback);
  await playback.drained;
}

/** Play one session's latest answer. Concurrent answer playback is deliberately refused. */
export async function speakAnswer(sessionId: string): Promise<void> {
  if (active) throw new ApiError(409, "An answer is already speaking", "speech_busy");
  const playback = newPlayback(audioContext());
  active = playback;
  speechPlaying.set(true);
  try {
    // Create/resume before the first network await so browser and desktop user gestures unlock audio.
    await playback.context.resume();
    await streamAnswer(playback, sessionId);
  } catch (e) {
    if (!playback.cancelled) throw e;
  } finally {
    playback.streamEnded = true;
    if (playback.cancelled) stopNodes(playback);
    try { await playback.context.close(); } catch {}
    if (playback.stopRequest) { try { await playback.stopRequest; } catch {} }
    if (active === playback) {
      active = null;
      speechPlaying.set(false);
    }
    playback.resolveFinished();
  }
}

/** Stop local playback immediately and also stop the daemon's current synthesis. */
export async function stopSpeaking(): Promise<void> {
  const playback = active;
  if (!playback) return;
  playback.cancelled = true;
  playback.controller.abort();
  if (playback.reader) void playback.reader.cancel().catch(() => {});
  stopNodes(playback);
  playback.stopRequest ??= stopRequest();
  const result = await Promise.allSettled([playback.finished, playback.stopRequest]);
  const remote = result[1];
  if (remote.status === "rejected") throw remote.reason;
}
