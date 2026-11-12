// GPT-Live call audio (docs/manager-design.md section 5): the client only captures the microphone and plays speech.
//
// Core owns the call. It holds the WebRTC peer to OpenAI, the sideband, the credentials and the Manager; this module
// talks to it over one WebSocket (`/api/manager/live/audio`) in raw PCM16 mono, 24 kHz, both ways. No client runs
// WebRTC, so the same protocol serves the TUI and iOS. The browser's echo cancellation stays on. Not verified against
// the real service. Browsers allow the microphone on HTTPS or localhost only.
import { socketUrl } from "./transport";
import { nativeModule, platform } from "nsh";

type NativeLive = {
  available(): boolean;
  start(url: string): Promise<void>;
  stop(): void;
  interrupt(): void;
  setMuted(muted: boolean): void;
  levels(): { local: number; remote: number; lost?: string | null };
};
function nativeLive(): NativeLive | null {
  if (platform.os !== "ios") return null;
  try { return nativeModule<{ LiveAudio?: NativeLive }>("nexus/native").LiveAudio ?? null; } catch { return null; }
}
let nativeActive = false;
let nativeTimer: ReturnType<typeof setInterval> | null = null;
let generation = 0;

// The NSH host's shared TS environment has no lib.dom: keep the narrow surface used here explicit.
type TrackLike = { stop(): void };
type StreamLike = { getTracks(): TrackLike[] };
type AnalyserLike = { fftSize: number; connect(to: unknown): unknown; getFloatTimeDomainData(into: Float32Array): void };
type PortLike = { onmessage: ((e: { data: any }) => void) | null };
type NodeLike = { connect(to: unknown): unknown; disconnect(): void; port?: PortLike };
type BufferLike = { copyToChannel(from: Float32Array, channel: number): void };
type SourceLike = { buffer: BufferLike | null; onended: (() => void) | null; connect(to: unknown): unknown; start(at: number): void; stop(): void };
type ContextLike = {
  currentTime: number;
  destination: unknown;
  audioWorklet?: { addModule(url: string): Promise<void> };
  createAnalyser(): AnalyserLike;
  createGain(): { gain: { value: number }; connect(to: unknown): unknown };
  createMediaStreamSource(stream: StreamLike): NodeLike;
  createBuffer(channels: number, length: number, rate: number): BufferLike;
  createBufferSource(): SourceLike;
  resume(): Promise<void>;
  close(): Promise<void>;
};
type SocketLike = {
  binaryType: string; readyState: number; onopen: (() => void) | null; onclose: ((e: { code: number }) => void) | null;
  onerror: (() => void) | null; onmessage: ((e: { data: unknown }) => void) | null; send(data: unknown): void; close(): void;
};
type Env = {
  AudioContext?: new (options?: { sampleRate?: number }) => ContextLike;
  webkitAudioContext?: new (options?: { sampleRate?: number }) => ContextLike;
  AudioWorkletNode?: new (context: unknown, name: string, options?: unknown) => NodeLike;
  Blob?: new (parts: string[], options?: { type?: string }) => unknown;
  URL?: { createObjectURL(blob: unknown): string; revokeObjectURL(url: string): void };
  WebSocket?: new (url: string) => SocketLike;
  navigator?: { mediaDevices?: { getUserMedia?: (constraints: unknown) => Promise<StreamLike> } };
};
const env = () => globalThis as unknown as Env;

export const RATE = 24000;
const WORKLET = `
class NexusLiveCapture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Int16Array(480); this.n = 0; }
  process(inputs) {
    const ch = (inputs[0] || [])[0];
    if (ch) for (let i = 0; i < ch.length; i++) {
      const v = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = v < 0 ? v * 32768 : v * 32767;
      if (this.n === this.buf.length) { const out = this.buf.slice(); this.port.postMessage(out.buffer, [out.buffer]); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("nexus-live-capture", NexusLiveCapture);
`;

interface Call {
  socket: SocketLike;
  stream: StreamLike;
  context: ContextLike;
  capture: NodeLike;
  inLevel: AnalyserLike;
  outLevel: AnalyserLike;
  samples: { in: Float32Array; out: Float32Array };
  sources: Set<SourceLike>;
  jitter: Jitter;
  muted: boolean;
}
let call: Call | null = null;

export const gptLiveSupported = () => {
  if (nativeLive()?.available()) return true;
  const e = env();
  return !!(e.AudioContext ?? e.webkitAudioContext) && !!e.AudioWorkletNode && !!e.WebSocket && !!e.Blob && !!e.URL
    && typeof e.navigator?.mediaDevices?.getUserMedia === "function";
};
export const gptLiveOpen = () => call !== null || nativeActive;

function rms(node: AnalyserLike, buffer: Float32Array): number {
  node.getFloatTimeDomainData(buffer);
  let power = 0;
  for (const sample of buffer) power += sample * sample;
  return Math.min(1, Math.sqrt(power / buffer.length) * 3);
}

/** Input (microphone) and output (speech) levels, 0 to 1; read here by the orb, never sent anywhere. */
export function gptLiveLevels(): { local: number; remote: number } {
  if (nativeActive) { const levels = nativeLive()?.levels(); return { local: levels?.local ?? 0, remote: levels?.remote ?? 0 }; }
  return call ? { local: call.muted ? 0 : rms(call.inLevel, call.samples.in), remote: rms(call.outLevel, call.samples.out) } : { local: 0, remote: 0 };
}

export const CUSHION_START = 0.12, CUSHION_STEP = 0.06, CUSHION_MAX = 0.3, UNDERRUN_WINDOW = 1.5, FADE_SAMPLES = RATE * 0.005;

/**
 * Adaptive jitter schedule for contiguous packets. A packet arriving while the playhead is ahead of `currentTime`
 * is chained on; one arriving after it ran dry restarts `cushion` seconds ahead (120 ms at the start of a call) and
 * fades in over 5 ms. Rule: if the playhead ran dry mid-call and more audio arrives within 1.5 s, that was jitter, so
 * the cushion grows by 60 ms up to 300 ms for the rest of the call. A longer silence is a normal end of utterance.
 * `reset` (barge-in) forgets the playhead but keeps the learned cushion; a new call builds a new schedule.
 */
export class Jitter {
  cushion = CUSHION_START;
  nextStart = 0;
  schedule(now: number, duration: number): { at: number; fadeIn: boolean } {
    let at = this.nextStart, fadeIn = false;
    if (this.nextStart <= now) {
      if (this.nextStart > 0 && now - this.nextStart <= UNDERRUN_WINDOW) this.cushion = Math.min(CUSHION_MAX, this.cushion + CUSHION_STEP);
      at = now + this.cushion;
      fadeIn = true;
    }
    this.nextStart = at + duration;
    return { at, fadeIn };
  }
  reset() { this.nextStart = 0; }
}

/** A binary audio frame; a test realm's (or a WebView's) `ArrayBuffer` is not `instanceof` this realm's. */
function isBinary(data: unknown): data is ArrayBuffer {
  return data instanceof ArrayBuffer || Object.prototype.toString.call(data) === "[object ArrayBuffer]";
}

function play(c: Call, data: ArrayBuffer) {
  const pcm = new Int16Array(data, 0, Math.floor(data.byteLength / 2));
  if (!pcm.length) return;
  const floats = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
  const { at, fadeIn } = c.jitter.schedule(c.context.currentTime, floats.length / RATE);
  if (fadeIn) for (let i = 0; i < Math.min(FADE_SAMPLES, floats.length); i++) floats[i] *= (i + 1) / FADE_SAMPLES;
  const buffer = c.context.createBuffer(1, floats.length, RATE);
  buffer.copyToChannel(floats, 0);
  const source = c.context.createBufferSource();
  source.buffer = buffer;
  source.connect(c.outLevel);
  source.onended = () => c.sources.delete(source);
  c.sources.add(source);
  source.start(at);
}

/**
 * Open the call's audio: the microphone to Core and Core's speech to the speaker. `onLost` runs once if the socket
 * closes after it opened. Throws a message fit for a toast; everything is released on failure.
 */
export async function startGptLive(streamId: string, onLost: (why: string) => void): Promise<void> {
  if (call || nativeActive) return;
  const native = nativeLive();
  if (native?.available()) {
    const current = ++generation;
    const url = await socketUrl("/api/manager/live/audio", { stream_id: streamId });
    if (current !== generation) throw new Error("Voice call cancelled.");
    await native.start(url);
    if (current !== generation) { native.stop(); throw new Error("Voice call cancelled."); }
    nativeActive = true;
    nativeTimer = setInterval(() => {
      const why = native.levels().lost;
      if (why) { stopGptLive(); onLost(why); }
    }, 100);
    return;
  }
  if (!gptLiveSupported()) throw new Error("This client cannot capture audio for a voice call.");
  const e = env();
  let stream: StreamLike;
  try {
    stream = await e.navigator!.mediaDevices!.getUserMedia!({ audio: { channelCount: { ideal: 1 }, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch {
    throw new Error("The microphone is blocked. Allow it for this page (it needs HTTPS or localhost) and try again.");
  }
  const Context = (e.AudioContext ?? e.webkitAudioContext)!;
  let context: ContextLike | null = null;
  let socket: SocketLike | null = null;
  try {
    context = new Context({ sampleRate: RATE });
    await context.resume();
    const url = e.URL!.createObjectURL(new e.Blob!([WORKLET], { type: "text/javascript" }));
    try { await context.audioWorklet!.addModule(url); } finally { e.URL!.revokeObjectURL(url); }
    const source = context.createMediaStreamSource(stream);
    const inLevel = context.createAnalyser(); inLevel.fftSize = 1024;
    const outLevel = context.createAnalyser(); outLevel.fftSize = 1024;
    outLevel.connect(context.destination);
    const capture = new e.AudioWorkletNode!(context, "nexus-live-capture", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    const silent = context.createGain(); silent.gain.value = 0;
    source.connect(inLevel);
    source.connect(capture);
    capture.connect(silent); silent.connect(context.destination); // a worklet only runs while it reaches the destination
    const ws = new e.WebSocket!(await socketUrl("/api/manager/live/audio", { stream_id: streamId }));
    socket = ws;
    ws.binaryType = "arraybuffer";
    await new Promise<void>((resolve, reject) => {
      ws.onopen = () => resolve();
      ws.onerror = () => reject(new Error("Could not open the call's audio connection to Core."));
      ws.onclose = (ev) => reject(new Error(ev.code === 4403 || ev.code === 4404 ? "The voice call is not open on Core." : "Core refused the call's audio connection."));
    });
    const next: Call = { socket: ws, stream, context, capture, inLevel, outLevel, sources: new Set(), jitter: new Jitter(), muted: false,
      samples: { in: new Float32Array(inLevel.fftSize), out: new Float32Array(outLevel.fftSize) } };
    call = next;
    capture.port!.onmessage = (m) => { if (!next.muted && ws.readyState === 1 && isBinary(m.data)) ws.send(m.data); };
    ws.onmessage = (m) => { if (isBinary(m.data)) play(next, m.data); };
    ws.onerror = null;
    ws.onclose = () => { if (call === next) { stopGptLive(); onLost("The voice connection to Core closed."); } };
  } catch (error) {
    call = null;
    for (const track of stream.getTracks()) track.stop();
    if (socket) { socket.onclose = null; socket.onerror = null; try { socket.close(); } catch {} }
    void context?.close().catch(() => {});
    throw error;
  }
}

/** Mute or unmute the microphone for the call (the socket stays open). */
export function setGptLiveMuted(muted: boolean) {
  if (nativeActive) { nativeLive()?.setMuted(muted); return; }
  if (!call) return;
  call.muted = muted;
  if (call.socket.readyState === 1) call.socket.send(muted ? "mute" : "unmute");
}

/** Drop whatever speech is queued on the speaker (barge-in). */
export function interruptGptLive() {
  if (nativeActive) { nativeLive()?.interrupt(); return; }
  if (!call) return;
  for (const source of call.sources) { source.onended = null; try { source.stop(); } catch {} }
  call.sources.clear();
  call.jitter.reset();
}

/** Hang up this client's side. Idempotent; Core ends its half when the Manager call stops. */
export function stopGptLive() {
  generation++;
  if (nativeTimer) { clearInterval(nativeTimer); nativeTimer = null; }
  if (nativeActive || platform.os === "ios") nativeLive()?.stop();
  nativeActive = false;
  const c = call;
  call = null;
  if (!c) return;
  c.socket.onclose = null; c.socket.onmessage = null;
  if (c.capture.port) c.capture.port.onmessage = null;
  try { c.socket.close(); } catch {}
  for (const source of c.sources) { source.onended = null; try { source.stop(); } catch {} }
  for (const track of c.stream.getTracks()) track.stop();
  try { c.capture.disconnect(); } catch {}
  void c.context.close().catch(() => {});
}
