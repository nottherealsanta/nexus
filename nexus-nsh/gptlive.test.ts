import { describe, expect, test } from "nsh/test";
import { CUSHION_MAX, FADE_SAMPLES, Jitter } from "./src/core/gptlive";

// NSH 0.4.0 has no `toBeCloseTo`: compare within an epsilon instead.
const close = (actual: number, expected: number, eps = 1e-9) => expect(Math.abs(actual - expected) < eps).toBe(true);

describe("GPT-Live jitter schedule", () => {
  test("the first packet starts 120 ms ahead with a fade-in, then packets chain", () => {
    const j = new Jitter();
    const first = j.schedule(1, 0.02);
    expect(first.fadeIn).toBe(true);
    close(first.at, 1.12);
    const next = j.schedule(1.01, 0.02);
    expect(next.fadeIn).toBe(false);
    close(next.at, 1.14);
    expect(FADE_SAMPLES).toBe(120);
  });
  test("running dry mid-speech grows the cushion by 60 ms, capped at 300 ms", () => {
    const j = new Jitter();
    let now = 1;
    j.schedule(now, 0.02);
    now += 0.5; // the playhead ran dry, more audio arrives soon after
    const again = j.schedule(now, 0.02);
    close(j.cushion, 0.18);
    expect(again).toEqual({ at: now + j.cushion, fadeIn: true });
    for (let i = 0; i < 10; i++) { now += 1; j.schedule(now, 0.02); }
    expect(j.cushion).toBe(CUSHION_MAX);
  });
  test("a long silence is the end of an utterance and does not grow the cushion", () => {
    const j = new Jitter();
    j.schedule(1, 0.02);
    j.schedule(10, 0.02);
    close(j.cushion, 0.12);
  });
  test("barge-in keeps the learned cushion without counting as an underrun; a new call starts at 120 ms", () => {
    const j = new Jitter();
    j.schedule(1, 0.02);
    j.schedule(1.5, 0.02);
    j.reset();
    expect(j.nextStart).toBe(0);
    j.schedule(3, 0.02);
    close(j.cushion, 0.18);
    close(new Jitter().cushion, 0.12);
  });
});

// Deterministic Manager-live cancellation: no microphone or socket is left behind by a hang-up during start.
import { microphoneOwner } from "./src/audio-capture";
import { gptLiveOpen, startGptLive, stopGptLive } from "./src/core/gptlive";

type Root = Record<string, any>;

function withEnvironment<T>(env: Record<string, unknown>, run: () => Promise<T>): Promise<T> {
  const root = globalThis as Root;
  const saved = new Map<string, PropertyDescriptor | undefined>();
  for (const [key, value] of Object.entries(env)) {
    saved.set(key, Object.getOwnPropertyDescriptor(root, key));
    Object.defineProperty(root, key, { value, configurable: true, writable: true });
  }
  return run().finally(() => {
    for (const [key, descriptor] of saved) {
      if (descriptor) Object.defineProperty(root, key, descriptor);
      else delete root[key];
    }
  });
}

function liveNodes() {
  const node = () => ({ connect() {}, disconnect() {} });
  let sockets: any[] = [];
  let trackStops = 0;
  let socketCloses = 0;
  class Context {
    sampleRate = 24_000;
    destination = {};
    currentTime = 0;
    audioWorklet = { async addModule() {} };
    async resume() {}
    async close() {}
    createAnalyser() { return { fftSize: 1024, ...node(), getFloatTimeDomainData() {} }; }
    createGain() { return { gain: { value: 0 }, ...node() }; }
    createMediaStreamSource() { return node(); }
    createBuffer() { return { copyToChannel() {} }; }
    createBufferSource() { return { buffer: null, onended: null, ...node(), start() {}, stop() {} }; }
  }
  class Worklet { port = { onmessage: null as any }; connect() {} disconnect() {} }
  class Socket {
    binaryType = "arraybuffer";
    readyState = 0;
    onopen: (() => void) | null = null;
    onclose: ((e: { code: number }) => void) | null = null;
    onerror: (() => void) | null = null;
    onmessage: ((e: { data: unknown }) => void) | null = null;
    constructor(public url: string) { sockets.push(this); }
    send() {}
    close() { socketCloses += 1; this.readyState = 3; }
  }
  const stream = {
    getTracks: () => [{ stop() { trackStops += 1; } }],
  };
  return {
    Context, Worklet, Socket, stream,
    get sockets() { return sockets; },
    get trackStops() { return trackStops; },
    get socketCloses() { return socketCloses; },
    resetSockets() { sockets = []; },
  };
}

const liveEnv = (fake: ReturnType<typeof liveNodes>, getUserMedia: (constraints: unknown) => Promise<any>) => ({
  navigator: { mediaDevices: { getUserMedia } },
  AudioContext: fake.Context,
  webkitAudioContext: undefined,
  AudioWorkletNode: fake.Worklet,
  WebSocket: fake.Socket,
  Blob: class { constructor(public parts: unknown[], public options: unknown) {} },
  URL: { createObjectURL: () => "blob:live", revokeObjectURL() {} },
});

describe("GPT-Live start cancellation", () => {
  test("hang-up while getUserMedia is pending stops the late stream and releases the microphone", async () => {
    const fake = liveNodes();
    let resolveMedia: ((stream: any) => void) | null = null;
    const media = new Promise<any>((resolve) => { resolveMedia = resolve; });
    await withEnvironment(liveEnv(fake, () => media), async () => {
      const start = startGptLive("stream-1", () => {});
      stopGptLive();
      resolveMedia!(fake.stream);
      const error = await start.then(() => null, (e) => e);
      expect((error as Error)?.message).toBe("Voice call cancelled.");
    });
    expect(fake.sockets).toHaveLength(0);
    expect(fake.trackStops).toBe(1);
    expect(microphoneOwner()).toBe(null);
    expect(gptLiveOpen()).toBe(false);
  });

  test("hang-up while the audio socket is opening closes it and releases the microphone", async () => {
    const fake = liveNodes();
    await withEnvironment(liveEnv(fake, async () => fake.stream), async () => {
      const start = startGptLive("stream-2", () => {});
      const end = Date.now() + 2000;
      while (fake.sockets.length === 0 && Date.now() < end) await new Promise((r) => setTimeout(r, 5));
      expect(fake.sockets).toHaveLength(1);
      stopGptLive();
      fake.sockets[0].onopen?.();
      const error = await start.then(() => null, (e) => e);
      expect((error as Error)?.message).toBe("Voice call cancelled.");
    });
    expect(fake.trackStops).toBe(1);
    expect(fake.socketCloses).toBe(1);
    expect(microphoneOwner()).toBe(null);
    expect(gptLiveOpen()).toBe(false);
  });
});
